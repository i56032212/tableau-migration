"""排程器（Q2）：依排程檔執行萃取更新與訂閱，並記錄每次執行的結果。

    uv run tmig schedule list schedules.json --fixtures fixtures    # 工作、下次執行時間、對應的報表
    uv run tmig schedule serve schedules.json --fixtures fixtures   # 常駐執行
    uv run tmig schedule tick schedules.json --fixtures fixtures    # 執行一次到期的工作（給作業系統的排程或 Celery beat 呼叫）

- 工作：萃取更新工作（報表的嵌入萃取，或用到某個 Published Data Source 的每份報表）與訂閱（Q3）
- 對應不到報表、排程無法辨識或已暫停的工作不執行，列出原因
- 下次執行時間記在狀態檔（state.json）；停機期間錯過的排程，啟動後補跑一次，再從現在起算下一次
- 同時到期的工作依 Tableau 的優先順序（數字小的先）逐一執行；結果逐行寫入 runs.jsonl
- 「立即執行」：其他行程（查詢服務的站台管理）在狀態目錄的 queue/ 放一個要求，排程器下一次檢查時執行
- 工作的來源可以是函式（source）：每次檢查時重新取得，排程的修改不必重新啟動；工作的排程改變時重新排定
- 常駐時每次檢查寫入 scheduler.json（最後回報的時間），查詢服務據此顯示排程器是否在執行
- 時間一律以參數注入（clock），測試不依賴實際時間
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from tmig.catalog import Catalog, Report
from tmig.schedule.model import ExtractTask, Schedule, ScheduleError, ScheduleFile, Subscription

JobKind = Literal["extract", "subscription", "alert"]
Clock = Callable[[], dt.datetime]
QUEUE = "queue"
"""狀態目錄中「立即執行」的要求。"""
STATUS = "scheduler.json"
"""狀態目錄中排程器最後回報的時間。"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Target:
    """工作的對象：萃取更新為報表與資料源，訂閱為報表與 view。"""

    report: Report
    datasource: str | None = None
    view: str | None = None

    def describe(self) -> str:
        if self.datasource and self.report.name.startswith("pds:"):
            return self.report.label  # 伺服器上的資料源：只有一個資料源
        if self.datasource:
            return f"{self.report.label}／{self.datasource}"
        if self.view:
            return f"{self.report.label}／{self.view}"
        return self.report.label


@dataclass
class Job:
    id: str
    kind: JobKind
    schedule: Schedule | None
    priority: int = 50
    task: ExtractTask | None = None
    subscription: Subscription | None = None
    targets: list[Target] = field(default_factory=list)
    problem: str | None = None
    """不能執行的原因（對應不到報表、排程無法辨識或已暫停）；None 表示可以執行。"""
    suspended: bool = False
    """不能執行只是因為暫停（工作、訂閱或排程）：可以立即執行。"""

    @property
    def runnable(self) -> bool:
        return self.problem is None

    def label(self) -> str:
        if self.task is not None:
            mode = "增量" if self.task.refresh == "incremental" else "完整"
            return f"萃取更新（{mode}）：{self.task.target_name or self.task.content_url or self.task.target_id}"
        assert self.subscription is not None
        sub = self.subscription
        what = sub.view or sub.workbook_name or sub.workbook or ""
        return f"訂閱「{sub.subject}」：{sub.user} ← {what}"


def plan_jobs(schedules: ScheduleFile, catalog: Catalog) -> list[Job]:
    """排程檔 → 工作：對應報表、檢查排程。"""
    jobs: list[Job] = []
    for task in schedules.extract_refreshes:
        schedule = schedules.schedule(task.schedule)
        job = Job(
            id=f"extract:{task.id}",
            kind="extract",
            schedule=schedule,
            priority=task.priority or (schedule.priority if schedule else None) or 50,
            task=task,
        )
        if task.report is not None:
            report = catalog.report(task.report)
            if report is not None:
                job.targets = [Target(report, ds) for ds in report.extract_datasources()]
                if not job.targets:
                    job.problem = f"報表「{report.label}」沒有萃取型的資料源"
            else:
                job.problem = f"找不到報表「{task.report}」"
        elif task.target_kind == "workbook":
            report = catalog.workbook(task.content_url, task.target_name)
            if report is not None:
                job.targets = [Target(report, ds) for ds in report.extract_datasources()]
                if not job.targets:
                    job.problem = f"報表「{report.name}」沒有萃取型的資料源"
        else:
            job.targets = [
                Target(report, ds)
                for report, ds in catalog.published(task.content_url, task.target_name)
            ]
        if not job.targets and job.problem is None:
            job.problem = (
                f"找不到對應的報表（{task.target_kind}：{task.content_url or task.target_name}）"
            )
        if task.suspended and job.problem is None:
            job.problem = "萃取更新工作已暫停" + (
                f"（連續失敗 {task.consecutive_failures} 次）" if task.consecutive_failures else ""
            )
            job.suspended = True
        jobs.append(_check_schedule(job))
    for sub in schedules.subscriptions:
        schedule = schedules.schedule(sub.schedule)
        job = Job(
            id=f"subscription:{sub.id}",
            kind="subscription",
            schedule=schedule,
            priority=(schedule.priority if schedule else None) or 50,
            subscription=sub,
        )
        report = (
            catalog.report(sub.report)
            if sub.report is not None
            else catalog.workbook(sub.workbook, sub.workbook_name)
        )
        if report is None:
            job.problem = f"找不到對應的報表（{sub.report or sub.workbook or sub.workbook_name}）"
        elif sub.content_kind == "view":
            if not any(v == sub.view for v in _view_names(report)):
                job.problem = f"報表「{report.name}」沒有 view「{sub.view}」"
            else:
                job.targets = [Target(report, view=sub.view)]
        else:
            job.targets = [Target(report)]
        if sub.suspended and job.problem is None:
            job.problem = "訂閱已暫停"
            job.suspended = True
        jobs.append(_check_schedule(job))
    return jobs


def _view_names(report: Report) -> list[str]:
    return [d.name for d in report.ir.dashboards] + [w.name for w in report.ir.worksheets]


def _check_schedule(job: Job) -> Job:
    schedule = job.schedule
    if schedule is None:
        job.problem, job.suspended = job.problem or "找不到排程", False
    elif schedule.frequency is None:
        if job.problem is None or job.suspended:
            job.problem = f"排程「{schedule.name}」無法辨識：{schedule.problem or '沒有頻率'}"
            job.suspended = False
    elif schedule.state != "Active" and job.problem is None:
        job.problem = f"排程「{schedule.name}」已暫停"
        job.suspended = True
    return job


def _signature(job: Job, timezone: str) -> str:
    """工作的排程（頻率與時區）：改變時重新排定下次執行時間。"""
    assert job.schedule is not None and job.schedule.frequency is not None
    return f"{timezone}|{job.schedule.frequency.model_dump_json()}"


# ---------------------------------------------------------------- 執行


@dataclass
class RunRecord:
    job: str
    kind: JobKind
    due: str
    started: str
    finished: str
    status: Literal["ok", "failed"]
    detail: str
    trigger: Literal["schedule", "manual"] = "schedule"
    requested_by: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


def request_run(state_dir: Path, job: str, user: str) -> str:
    """排入「立即執行」（其他行程呼叫）：排程器下一次檢查時執行；回傳要求的 id。"""
    queue = state_dir / QUEUE
    queue.mkdir(parents=True, exist_ok=True)
    request = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(3)}"
    payload = {"id": request, "job": job, "user": user, "requested_at": utc_now().isoformat()}
    temporary = queue / f"{request}.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, queue / f"{request}.json")
    return request


def queued(state_dir: Path) -> list[dict[str, Any]]:
    """排隊中的「立即執行」（先排的在前）。"""
    queue = state_dir / QUEUE
    if not queue.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(queue.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and "job" in item:
            items.append(item)
    return items


class Handler(Protocol):
    def __call__(self, job: Job) -> str:
        """執行工作，回傳說明；失敗時丟出例外。"""
        ...


class Scheduler:
    """依排程執行工作；狀態（每個工作的下次執行時間）與執行紀錄存在 state_dir。

    source：每次檢查時重新取得（工作, 時區）；on_result：每次執行後呼叫（例如記下連續失敗、寄信通知）。"""

    def __init__(
        self,
        jobs: list[Job],
        timezone: str,
        handlers: dict[JobKind, Handler],
        state_dir: Path,
        clock: Clock = utc_now,
        *,
        source: Callable[[], tuple[list[Job], str | None]] | None = None,
        on_result: Callable[[Job, RunRecord], Any] | None = None,
        after_tick: Callable[[list[RunRecord]], list[RunRecord]] | None = None,
        extras: dict[str, Callable[[str, str | None], RunRecord]] | None = None,
    ) -> None:
        self.all_jobs = list(jobs)
        self.jobs = [j for j in jobs if j.runnable]
        self.timezone = timezone
        self.handlers = handlers
        self.state_dir = state_dir
        self.clock = clock
        self.source = source
        self.on_result = on_result
        self.after_tick = after_tick
        """每次檢查之後（例如資料警示：萃取剛更新的報表與每小時的檢查），回傳的紀錄也寫入 runs.jsonl。"""
        self.extras = extras or {}
        """「立即執行」中其他種類的工作：id 的前綴（例如 alert）→ 執行並回傳紀錄。"""
        self.state_path = state_dir / "state.json"
        self.log_path = state_dir / "runs.jsonl"
        self.started = clock()
        self.due: dict[str, dt.datetime] = {}
        self.keys: dict[str, str] = {}
        self._load_state()
        self.refresh()

    def refresh(self) -> None:
        """有工作的來源時重新取得工作與時區（沒有時區時不排程）。"""
        if self.source is None:
            return
        jobs, timezone = self.source()
        self.all_jobs = list(jobs)
        if timezone:
            self.timezone = timezone
            self.jobs = [j for j in jobs if j.runnable]
        else:
            self.jobs = []

    def _load_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.due = {k: dt.datetime.fromisoformat(v) for k, v in raw.get("next", {}).items()}
            self.keys = {str(k): str(v) for k, v in raw.get("keys", {}).items()}
        except (OSError, ValueError, AttributeError):
            self.due, self.keys = {}, {}

    def _save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.source is not None:
            # 刪除的工作不再保留
            known = {j.id for j in self.all_jobs}
            self.due = {k: v for k, v in self.due.items() if k in known}
            self.keys = {k: v for k, v in self.keys.items() if k in known}
        data = {
            "next": {k: v.isoformat() for k, v in sorted(self.due.items())},
            "keys": dict(sorted(self.keys.items())),
        }
        temp = self.state_path.with_name(f"state.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def next_due(self, job: Job, after: dt.datetime) -> dt.datetime:
        assert job.schedule is not None and job.schedule.frequency is not None
        return job.schedule.frequency.next_run(after, self.timezone)

    def tick(self) -> list[RunRecord]:
        """執行「立即執行」的要求與到期的工作（優先順序數字小的先），回傳執行紀錄；
        第一次看到的工作（或排程改變的工作）只排定下次執行時間。"""
        self.refresh()
        records = self.run_requests()
        now = self.clock()
        due: list[tuple[int, str, Job]] = []
        changed = False
        for job in self.jobs:
            key = _signature(job, self.timezone)
            when = self.due.get(job.id)
            if when is None or self.keys.get(job.id, key) != key:
                self.due[job.id] = self.next_due(job, now)
                self.keys[job.id] = key
                changed = True
                continue
            if job.id not in self.keys:
                self.keys[job.id] = key
                changed = True
            if when <= now:
                due.append((job.priority, job.id, job))
        for _, _, job in sorted(due, key=lambda item: (item[0], item[1])):
            records.append(self.run(job, self.due[job.id]))
            # 錯過多次時只補跑一次：下一次從現在起算
            self.due[job.id] = self.next_due(job, max(self.clock(), self.due[job.id]))
            changed = True
        if changed:
            self._save_state()
        if self.after_tick is not None:
            try:
                extra = self.after_tick(records)
            except Exception as exc:  # 檢查之後的工作失敗不影響排程器
                print(f"檢查之後的工作失敗：{type(exc).__name__}: {exc}", file=sys.stderr)
                extra = []
            for record in extra:
                self._log(record)
            records.extend(extra)
        return records

    def run(
        self,
        job: Job,
        due: dt.datetime | None = None,
        *,
        trigger: Literal["schedule", "manual"] = "schedule",
        requested_by: str | None = None,
    ) -> RunRecord:
        """立即執行一個工作並記錄結果（也供手動觸發使用）。"""
        started = self.clock()
        handler = self.handlers.get(job.kind)
        try:
            if handler is None:
                raise ScheduleError(f"沒有處理 {job.kind} 工作的程式")
            detail = handler(job)
            status: Literal["ok", "failed"] = "ok"
        except Exception as exc:  # 一個工作失敗不影響其他工作
            detail, status = f"{type(exc).__name__}: {exc}", "failed"
        record = RunRecord(
            job=job.id,
            kind=job.kind,
            due=(due or started).isoformat(),
            started=started.isoformat(),
            finished=self.clock().isoformat(),
            status=status,
            detail=detail,
            trigger=trigger,
            requested_by=requested_by,
        )
        self._log(record)
        if self.on_result is not None:
            try:
                self.on_result(job, record)
            except Exception as exc:  # 記錄結果的程式失敗不影響排程器
                print(f"無法記下 {job.id} 的結果：{type(exc).__name__}: {exc}", file=sys.stderr)
        return record

    def _log(self, record: RunRecord) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as log:
            log.write(record.to_json() + "\n")

    def run_requests(self) -> list[RunRecord]:
        """執行排隊中的「立即執行」（先排的先執行）；找不到或不能執行的工作記為失敗。"""
        queue = self.state_dir / QUEUE
        if not queue.is_dir():
            return []
        records: list[RunRecord] = []
        for path in sorted(queue.glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                path.unlink()
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            job_id = str(request.get("job", ""))
            user = request.get("user")
            extra = self.extras.get(job_id.partition(":")[0])
            if extra is not None:
                record = extra(job_id, user)
                self._log(record)
                records.append(record)
                continue
            job = next((j for j in self.all_jobs if j.id == job_id), None)
            if job is None or (job.problem is not None and not job.suspended):
                now = self.clock().isoformat()
                reason = f"找不到工作 {job_id}" if job is None else f"不能執行：{job.problem}"
                record = RunRecord(
                    job=job_id,
                    kind=job.kind if job is not None else "extract",
                    due=now,
                    started=now,
                    finished=now,
                    status="failed",
                    detail=reason,
                    trigger="manual",
                    requested_by=user,
                )
                self._log(record)
                records.append(record)
                continue
            records.append(self.run(job, trigger="manual", requested_by=user))
        return records

    def beat(self) -> None:
        """寫入排程器最後回報的時間（查詢服務據此顯示排程器是否在執行）。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "pid": os.getpid(),
            "started_at": self.started.isoformat(),
            "heartbeat_at": self.clock().isoformat(),
            "jobs": len(self.jobs),
            "timezone": self.timezone,
        }
        path = self.state_dir / STATUS
        temp = path.with_name(f"scheduler.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def seconds_until_next(self, limit: float = 60.0) -> float:
        now = self.clock()
        pending = [
            when for job_id, when in self.due.items() if any(j.id == job_id for j in self.jobs)
        ]
        if not pending:
            return limit
        return max(0.0, min(limit, (min(pending) - now).total_seconds()))

    def serve(
        self,
        sleep: Callable[[float], Any] = time.sleep,
        stop: Callable[[], bool] = lambda: False,
        on_record: Callable[[RunRecord], Any] | None = None,
        poll: float = 60.0,
    ) -> None:
        """常駐：執行到期的工作，等到下一個工作到期（最多 poll 秒檢查一次，也檢查「立即執行」）。"""
        while not stop():
            self.beat()
            for record in self.tick():
                if on_record is not None:
                    on_record(record)
            sleep(self.seconds_until_next(poll))


def last_runs(state_dir: Path) -> dict[str, dict[str, Any]]:
    """每個工作最後一次的執行紀錄。"""
    path = state_dir / "runs.jsonl"
    result: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and "job" in item:
            result[str(item["job"])] = item
    return result


# ---------------------------------------------------------------- 萃取更新


def extract_handler(config: Path | str | None = None) -> Handler:
    """萃取更新：依連線設定檔連到來源資料庫，更新每個對象的 DuckDB 檔（N7，原子替換）。"""

    def handle(job: Job) -> str:
        from tmig.data.extract_refresh import refresh_extract, refresh_plan
        from tmig.data.sources import load_connections, open_executor

        assert job.task is not None
        configs = load_connections(config)
        parts: list[str] = []
        for target in job.targets:
            assert target.datasource is not None
            ir = target.report.ir
            plan = refresh_plan(ir, target.datasource)
            executor = open_executor(plan.source, configs)
            try:
                result = refresh_extract(
                    ir,
                    target.datasource,
                    executor,
                    target.report.directory / "data" / f"{target.datasource}.duckdb",
                    incremental=job.task.refresh == "incremental",
                )
            finally:
                executor.close()
            rows = sum(t.rows for t in result.tables)
            parts.append(f"{target.describe()} {rows} 列")
        return "；".join(parts)

    return handle


__all__ = [
    "QUEUE",
    "STATUS",
    "Handler",
    "Job",
    "JobKind",
    "RunRecord",
    "Scheduler",
    "Target",
    "extract_handler",
    "last_runs",
    "plan_jobs",
    "queued",
    "request_run",
    "utc_now",
]
