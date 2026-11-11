"""排程管理：排程、萃取更新工作與訂閱在新平台上管理（Tableau 下線後的唯一來源）。

    <報表庫>/
    ├── schedules.json           排程、萃取更新工作與訂閱（格式與 Tableau 匯出的排程檔相同，見 tmig.schedule.model）
    ├── schedules.lock           修改時的鎖（查詢服務與排程器兩個行程都會修改）
    └── schedule-state/          排程器的狀態（tmig.schedule.runner）
        ├── state.json           每個工作的下次執行時間
        ├── runs.jsonl           每次執行的結果
        ├── scheduler.json       排程器最後回報的時間（查詢服務據此顯示排程器是否在執行）
        └── queue/               「立即執行」的要求：查詢服務寫入，排程器取出執行

匯入：``tmig schedule import schedules.json --store <報表庫>``（Tableau Server 匯出的排程檔），之後在站台管理
中修改；排程器 ``tmig schedule serve --store <報表庫>`` 每次檢查時重讀，修改不必重新啟動。

連續失敗（與 Tableau Server 相同）：萃取更新每次失敗寄信給報表的擁有者；萃取更新與訂閱連續失敗
SUSPEND_AFTER 次後暫停（訂閱暫停時寄信給訂閱者），排除問題後由站台管理員繼續。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tmig.catalog import Catalog, Report, report_directories, workbook_url_name
from tmig.schedule.model import ScheduleError, ScheduleFile, zone_info
from tmig.schedule.runner import QUEUE, STATUS, Job, RunRecord, plan_jobs, queued, request_run
from tmig_service.site import SiteStore
from tmig_service.store import ReportStore

if TYPE_CHECKING:
    from tmig_service.datasources import DatasourceStore
    from tmig_service.subscriptions import MailSettings

SCHEDULES = "schedules.json"
LOCK = "schedules.lock"
STATE = "schedule-state"
SCHEDULER = "（排程器）"
SUSPEND_AFTER = 5
"""連續失敗幾次後暫停（與 Tableau Server 的 backgrounder.failure_threshold_for_run_prevention 預設相同）；0 為不暫停。"""
HEARTBEAT_STALE = 180.0
"""排程器超過這麼多秒沒有回報，視為沒有在執行。"""


@contextmanager
def file_lock(path: Path, timeout: float = 15.0, stale: float = 60.0) -> Iterator[None]:
    """跨行程的鎖：以「建立檔案，已經存在就失敗」取得（各作業系統都支援）；超過 stale 秒的鎖視為遺留的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise ScheduleError(f"排程資料被鎖住超過 {timeout:.0f} 秒（{path}）") from None
            time.sleep(0.05)
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        path.unlink(missing_ok=True)


def atomic_replace(source: Path, target: Path, attempts: int = 40) -> None:
    """os.replace；Windows 上目標正被其他行程讀取時（只開一下）稍後再試。"""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def validate_schedules(data: ScheduleFile) -> None:
    """id 不重複、排程名稱不重複、工作與訂閱的排程都存在、時區正確。"""
    if data.timezone:
        zone_info(data.timezone)
    for label, ids in (
        ("排程", [s.id for s in data.schedules]),
        ("萃取更新工作", [t.id for t in data.extract_refreshes]),
        ("訂閱", [s.id for s in data.subscriptions]),
    ):
        if len(ids) != len(set(ids)):
            raise ScheduleError(f"{label}的 id 重複")
    names = [s.name.casefold() for s in data.schedules]
    if len(names) != len(set(names)):
        raise ScheduleError("排程的名稱重複")
    known = {s.id for s in data.schedules}
    for task in data.extract_refreshes:
        if task.schedule not in known:
            raise ScheduleError(f"萃取更新工作 {task.id} 的排程不存在")
    for sub in data.subscriptions:
        if sub.schedule not in known:
            raise ScheduleError(f"訂閱「{sub.subject or sub.id}」的排程不存在")


class ScheduleStore:
    """報表庫中的排程資料；修改以檔案鎖排隊（查詢服務與排程器都會修改），修改紀錄與站台管理共用。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / SCHEDULES
        self.lock_path = root / LOCK
        self.state_dir = root / STATE
        self._cache: tuple[tuple[int, int], ScheduleFile] | None = None

    def exists(self) -> bool:
        return self.path.is_file()

    def stamp(self) -> tuple[int, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def data(self) -> ScheduleFile:
        """目前的排程資料；還沒有時為空的。"""
        stamp = self.stamp()
        if stamp is None:
            return ScheduleFile()
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            data = ScheduleFile.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise ScheduleError(f"排程資料 {self.path} 不正確：{exc}") from exc
        self._cache = (stamp, data)
        return data

    def initialize(self, data: ScheduleFile, actor: str, replace: bool = False) -> ScheduleFile:
        with file_lock(self.lock_path):
            if self.exists() and not replace:
                raise ScheduleError("排程資料已經存在；要重新匯入時加 --replace")
            validate_schedules(data)
            self._write(data)
        detail = {
            "schedules": len(data.schedules),
            "extract_refreshes": len(data.extract_refreshes),
            "subscriptions": len(data.subscriptions),
        }
        SiteStore(self.root).record(actor, "schedule.import", data.source or "", detail)
        return data

    def change(
        self,
        actor: str,
        action: str,
        target: str,
        apply: Callable[[ScheduleFile], None],
        detail: dict[str, Any] | None = None,
        log: bool = True,
    ) -> ScheduleFile:
        """讀取目前的資料、套用修改、檢查後寫入；apply 拋出 ScheduleError 時不寫入。"""
        with file_lock(self.lock_path):
            self._cache = None
            data = self.data().model_copy(deep=True)
            apply(data)
            try:
                data = ScheduleFile.model_validate(data.model_dump())
            except ValidationError as exc:
                raise ScheduleError(str(exc)) from exc
            validate_schedules(data)
            self._write(data)
        if log:
            SiteStore(self.root).record(actor, action, target, detail or {})
        return data

    def _write(self, data: ScheduleFile) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{SCHEDULES}.{os.getpid()}.tmp")
        temporary.write_text(data.model_dump_json(indent=2) + "\n", encoding="utf-8")
        atomic_replace(temporary, self.path)
        self._cache = None

    # ------------------------------------------------------------ 排程器的狀態

    def runs(self, limit: int = 200, job: str | None = None) -> list[dict[str, Any]]:
        """最近的執行紀錄（新的在前）。"""
        path = self.state_dir / "runs.jsonl"
        if not path.is_file():
            return []
        result: list[dict[str, Any]] = []
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or (job is not None and item.get("job") != job):
                continue
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def last_runs(self) -> dict[str, dict[str, Any]]:
        last: dict[str, dict[str, Any]] = {}
        for item in reversed(self.runs(limit=100_000)):
            last[str(item.get("job"))] = item
        return last

    def status(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """排程器是否在執行（最後回報的時間）與排隊中的「立即執行」。"""
        pending = queued(self.state_dir)
        try:
            data = json.loads((self.state_dir / STATUS).read_text(encoding="utf-8"))
            beat = dt.datetime.fromisoformat(data["heartbeat_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return {"running": False, "heartbeat_at": None, "queued": pending}
        age = ((now or dt.datetime.now(dt.UTC)) - beat).total_seconds()
        return {**data, "running": age < HEARTBEAT_STALE, "queued": pending}

    def request_run(self, job: str, actor: str) -> str:
        """排入「立即執行」：排程器下一次檢查時（幾秒內）執行。"""
        return request_run(self.state_dir, job, actor)


# ---------------------------------------------------------------- 報表目錄


def store_reports(reports: ReportStore, site: SiteStore | None, fixtures: Path | None) -> Catalog:
    """排程的對象：fixtures 的報表與報表庫中發布的報表（與 fixtures 同名時以報表庫為準，與查詢服務相同）。"""
    from tmig.verify.run import load_fixture_ir
    from tmig_service.datasources import DatasourceStore

    linked = {w.id: w for w in site.data().workbooks} if site is not None and site.exists() else {}
    items: list[Report] = []
    records = reports.records()
    published = {r.id for r in records}
    for directory in report_directories(fixtures) if fixtures is not None else []:
        if directory.name not in published:
            items.append(
                Report(directory, load_fixture_ir(directory), workbook_url_name(directory))
            )
    for record in records:
        directory = reports.directory(record)
        workbook = linked.get(record.tableau_workbook or "")
        items.append(
            Report(
                directory,
                load_fixture_ir(directory),
                workbook.content_url if workbook is not None else None,
                key=record.id,
                owner=record.owner,
                title=record.name,
            )
        )
    return Catalog(items, datasource_reports(DatasourceStore(reports.root)))


def datasource_reports(store: DatasourceStore) -> list[Report]:
    """伺服器上萃取型的 Published Data Source（萃取更新的對象）。"""
    found: list[Report] = []
    for record in store.records():
        if not record.extract:
            continue
        try:
            ir = store.ir(record)
        except Exception:  # 讀不了的資料源不能排程，其他照常
            continue
        found.append(
            Report(
                store.directory(record),
                ir,
                record.content_url,
                key=f"pds:{record.id}",
                owner=record.owner,
                title=f"資料源：{record.name}",
            )
        )
    return found


class StoreJobs:
    """排程器在報表庫模式下的工作：排程資料、發布的報表或 fixtures 改變時重新規劃。"""

    def __init__(
        self,
        schedules: ScheduleStore,
        reports: ReportStore,
        site: SiteStore | None,
        fixtures: Path | None,
    ) -> None:
        self.schedules = schedules
        self.reports = reports
        self.site = site
        self.fixtures = fixtures
        self._catalog_key: tuple[Any, ...] | None = None
        self._catalog = Catalog()
        self._key: tuple[Any, ...] | None = None
        self._jobs: list[Job] = []

    def catalog(self) -> Catalog:
        from tmig_service.datasources import DatasourceStore

        key = (
            tuple((r.id, r.latest.version, r.tableau_workbook) for r in self.reports.records()),
            tuple((r.id, r.latest.version) for r in DatasourceStore(self.reports.root).records()),
            tuple(
                (
                    d.name,
                    (d / "golden.ir.json").stat().st_mtime_ns
                    if (d / "golden.ir.json").is_file()
                    else 0,
                )
                for d in (report_directories(self.fixtures) if self.fixtures is not None else [])
            ),
        )
        if key != self._catalog_key:
            self._catalog = store_reports(self.reports, self.site, self.fixtures)
            self._catalog_key = key
        return self._catalog

    def __call__(self) -> tuple[list[Job], str | None]:
        catalog = self.catalog()
        key = (self.schedules.stamp(), self._catalog_key)
        if key != self._key:
            self._jobs = plan_jobs(self.schedules.data(), catalog)
            self._key = key
        return self._jobs, self.schedules.data().timezone


# ---------------------------------------------------------------- 連續失敗


class FailurePolicy:
    """排程器每次執行後：記下連續失敗的次數，到達門檻時暫停工作；寄信通知（有郵件設定時）。"""

    def __init__(
        self,
        schedules: ScheduleStore,
        site: SiteStore | None,
        mail: MailSettings | None = None,
        threshold: int = SUSPEND_AFTER,
        send: Callable[[EmailMessage, MailSettings], None] | None = None,
    ) -> None:
        self.schedules = schedules
        self.site = site
        self.mail = mail
        self.threshold = threshold
        if send is None:
            from tmig_service.subscriptions import send_message

            send = send_message
        self.send = send

    def __call__(self, job: Job, record: RunRecord) -> None:
        kind, _, item_id = job.id.partition(":")
        failed = record.status == "failed"
        if kind == "extract" and not failed:
            self._refreshed(job, record)
        current = self._entry(self.schedules.data(), kind, item_id)
        if current is None or (not failed and current.consecutive_failures == 0):
            return
        result: dict[str, Any] = {"count": 0, "suspended": False}

        def apply(data: ScheduleFile) -> None:
            entry = self._entry(data, kind, item_id)
            if entry is None:
                return
            count = entry.consecutive_failures + 1 if failed else 0
            updates: dict[str, Any] = {"consecutive_failures": count}
            if failed and self.threshold and count >= self.threshold and not entry.suspended:
                updates["suspended"] = True
                result["suspended"] = True
            result["count"] = count
            items: list[Any] = data.extract_refreshes if kind == "extract" else data.subscriptions
            items[items.index(entry)] = entry.model_copy(update=updates)

        self.schedules.change(SCHEDULER, "task.result", job.id, apply, log=False)
        if result["suspended"]:
            SiteStore(self.schedules.root).record(
                SCHEDULER, "task.suspend", job.id, {"failures": result["count"]}
            )
        if failed:
            try:
                self.notify(job, record, result["count"], result["suspended"])
            except Exception as exc:  # 通知寄不出去不影響排程器
                print(f"無法寄出 {job.id} 失敗的通知：{exc}", file=sys.stderr)

    def _refreshed(self, job: Job, record: RunRecord) -> None:
        """伺服器上的資料源更新了萃取：記下時間（資料源頁面顯示）。"""
        from tmig_service.datasources import DatasourceStore

        store = DatasourceStore(self.schedules.root)
        for target in job.targets:
            if target.report.name.startswith("pds:"):
                try:
                    store.update(target.report.name[4:], {"refreshed_at": record.finished})
                except Exception as exc:  # 資料源剛被刪除等
                    print(f"無法記下資料源的萃取更新時間：{exc}", file=sys.stderr)

    @staticmethod
    def _entry(data: ScheduleFile, kind: str, item_id: str) -> Any:
        items: list[Any] = data.extract_refreshes if kind == "extract" else data.subscriptions
        return next((i for i in items if i.id == item_id), None)

    def _email(self, name: str | None) -> str | None:
        if not name or self.site is None or not self.site.exists():
            return None
        user = self.site.data().user(name)
        return user.email if user is not None else None

    def _owner(self, report: Report) -> str | None:
        if report.owner:
            return report.owner
        if self.site is None or not self.site.exists():
            return None
        for workbook in self.site.data().workbooks:
            if (report.url_name and workbook.content_url == report.url_name) or (
                workbook.name == report.ir.workbook.name
            ):
                return workbook.owner
        return None

    def notify(self, job: Job, record: RunRecord, count: int, suspended: bool) -> None:
        if self.mail is None:
            return
        link = f"{self.mail.site_url}/admin/tasks" if self.mail.site_url else "/admin/tasks"
        if job.kind == "extract":
            # 與 Tableau 相同：萃取更新每次失敗都寄給報表的擁有者
            owners = {o for o in (self._owner(t.report) for t in job.targets) if o}
            recipients = sorted({e for e in (self._email(o) for o in owners) if e})
            subject = f"萃取更新失敗：{'、'.join(t.describe() for t in job.targets) or job.label()}"
        else:
            # 訂閱：連續失敗暫停時寄給訂閱者
            if not suspended or job.subscription is None:
                return
            email = self._email(job.subscription.user)
            recipients = [email] if email else []
            subject = f"訂閱已暫停：{job.subscription.subject or job.label()}"
        if not recipients:
            return
        lines = [
            f"{job.label()}",
            f"時間：{record.started}",
            f"錯誤：{record.detail}",
            f"連續失敗：{count} 次",
        ]
        if suspended:
            lines.append(
                f"已連續失敗 {count} 次，這個工作已暫停；排除問題後請站台管理員在「站台管理 → 工作」中繼續。"
            )
        lines.append(link)
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.mail.sender
        message["To"] = ", ".join(recipients)
        message.set_content("\n".join(lines) + "\n")
        self.send(message, self.mail)


__all__ = [
    "HEARTBEAT_STALE",
    "LOCK",
    "QUEUE",
    "SCHEDULER",
    "SCHEDULES",
    "STATE",
    "SUSPEND_AFTER",
    "FailurePolicy",
    "ScheduleStore",
    "StoreJobs",
    "atomic_replace",
    "file_lock",
    "store_reports",
    "validate_schedules",
]
