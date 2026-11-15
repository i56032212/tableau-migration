"""資料警示（與 Tableau Server 的 data-driven alert 相同）：工作表的量值高於或低於門檻時寄信。

    <報表庫>/alerts.json      警示：條件、門檻、頻率、收件人、建立時的篩選與參數

- 建立：檢視器工具列的「警示」，選擇工作表中的量值、條件（高於、高於或等於、低於、低於或等於、等於）與門檻；
  能檢視報表的人可以為自己建立，報表的擁有者、Project Leader 與站台管理員可以加入其他收件人
- 檢查：排程器（報表庫模式）在報表的萃取更新成功後檢查用到它的警示，其餘每小時檢查一次（Live 連線）
- 每位收件人以自己的身分查詢（列層級權限照套），只有條件在自己看得到的資料中成立時才收到，
  郵件中只列出自己看得到的標記
- 頻率：「資料更新時」為每次檢查時條件成立就寄；每小時、每天、每週為條件持續成立時最多寄一次
- 連續失敗（查詢失敗）SUSPEND_AFTER 次後暫停並寄信給建立的人；繼續時重新計算
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import secrets
import sys
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig.catalog import Catalog, Report
from tmig.schedule.runner import Job, RunRecord
from tmig_service.schedules import SUSPEND_AFTER, atomic_replace, file_lock
from tmig_service.site import SiteStore

if TYPE_CHECKING:
    from tmig_service.subscriptions import MailSettings, Renderer

ALERTS = "alerts.json"
Condition = Literal["above", "above_or_equal", "below", "below_or_equal", "equal"]
Frequency = Literal["data", "hourly", "daily", "weekly"]
CONDITIONS: dict[str, str] = {
    "above": "高於",
    "above_or_equal": "高於或等於",
    "below": "低於",
    "below_or_equal": "低於或等於",
    "equal": "等於",
}
FREQUENCIES: dict[str, str] = {
    "data": "資料更新時",
    "hourly": "最多每小時一次",
    "daily": "最多每天一次",
    "weekly": "最多每週一次",
}
THROTTLE = {"data": 0, "hourly": 3600, "daily": 86400, "weekly": 7 * 86400}
CHECK_INTERVAL = 3600
"""萃取沒有更新時（例如 Live 連線）多久檢查一次（秒）。"""
MARK_LIMIT = 20
"""郵件中最多列出的標記。"""


class AlertError(ValueError):
    pass


def now_text(clock: Callable[[], dt.datetime] | None = None) -> str:
    return (clock() if clock else dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")


class DataAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str = ""
    owner: str = Field(description="建立的人（可以修改與刪除）")
    recipients: list[str] = Field(
        default_factory=list, description="收件人（建立的人自己收到時也列在這裡）"
    )
    report: str
    view: str = Field(description="建立時的 view（dashboard 或工作表）id：郵件中的連結")
    worksheet: str
    measure: str = Field(description="工作表查詢的量值欄（例如 sum(f_sales)）")
    measure_name: str
    condition: Condition
    threshold: float
    frequency: Frequency = "data"
    state: dict[str, Any] = Field(
        default_factory=dict, description="建立時的篩選與參數（QueryState）"
    )
    suspended: bool = False
    consecutive_failures: int = 0
    last_checked: str | None = None
    last_sent: dict[str, str] = Field(default_factory=dict, description="收件人 → 最後寄出的時間")
    created_at: str

    def describe(self) -> str:
        return f"{self.measure_name} {CONDITIONS[self.condition]} {number_text(self.threshold)}"


class AlertFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts: list[DataAlert] = Field(default_factory=list)


def number_text(value: float) -> str:
    if math.isfinite(value) and float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def met(condition: str, value: float, threshold: float) -> bool:
    if condition == "above":
        return value > threshold
    if condition == "above_or_equal":
        return value >= threshold
    if condition == "below":
        return value < threshold
    if condition == "below_or_equal":
        return value <= threshold
    return math.isclose(value, threshold, rel_tol=1e-9, abs_tol=1e-9)


class AlertStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / ALERTS
        self._cache: tuple[tuple[int, int], AlertFile] | None = None

    def data(self) -> AlertFile:
        try:
            info = self.path.stat()
        except OSError:
            return AlertFile()
        stamp = (info.st_mtime_ns, info.st_size)
        if self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        try:
            data = AlertFile.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise AlertError(f"資料警示 {self.path} 不正確：{exc}") from exc
        self._cache = (stamp, data)
        return data

    def get(self, alert_id: str) -> DataAlert | None:
        return next((a for a in self.data().alerts if a.id == alert_id), None)

    def change(self, apply: Callable[[AlertFile], Any]) -> Any:
        with file_lock(self.root / f"{ALERTS}.lock"):
            self._cache = None
            data = self.data().model_copy(deep=True)
            result = apply(data)
            data = AlertFile.model_validate(data.model_dump())
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{ALERTS}.{os.getpid()}.tmp")
            temporary.write_text(data.model_dump_json(indent=2) + "\n", encoding="utf-8")
            atomic_replace(temporary, self.path)
            self._cache = None
            return result

    def create(self, alert: dict[str, Any]) -> DataAlert:
        created = DataAlert.model_validate(
            {"id": f"a{secrets.token_hex(6)}", "created_at": now_text(), **alert}
        )
        self.change(lambda data: data.alerts.append(created))
        return created

    def update(self, alert_id: str, changes: dict[str, Any]) -> DataAlert:
        def apply(data: AlertFile) -> DataAlert:
            found = next((a for a in data.alerts if a.id == alert_id), None)
            if found is None:
                raise AlertError("找不到資料警示")
            updated = DataAlert.model_validate({**found.model_dump(), **changes})
            data.alerts[data.alerts.index(found)] = updated
            return updated

        result: DataAlert = self.change(apply)
        return result

    def delete(self, alert_id: str) -> None:
        def apply(data: AlertFile) -> None:
            data.alerts = [a for a in data.alerts if a.id != alert_id]

        self.change(apply)


# ---------------------------------------------------------------- 檢查


Evaluate = Callable[[Report, DataAlert, str], list[tuple[str, float]]]
"""以某位收件人的身分查詢警示的工作表：（標記的說明, 量值）。"""


def evaluate_with_runtime(site: SiteStore | None) -> Evaluate:
    """以查詢服務相同的方式查詢（萃取或 Live；以收件人的身分套用列層級權限）。"""
    from tmig.data import initial_sql
    from tmig.data.runtime import DataRuntime
    from tmig.planner.state import QueryState
    from tmig.sqlgen.query import worksheet_sql
    from tmig.verify.run import ensure_data
    from tmig_service.datasource_api import may
    from tmig_service.datasources import DatasourceStore, uses_published

    runtime = DataRuntime()

    def evaluate(report: Report, alert: DataAlert, user: str) -> list[tuple[str, float]]:
        if site is None or not site.exists():
            raise AlertError("沒有站台資料：無法以收件人的身分查詢")
        member = site.data().user(user)
        if member is None:
            raise AlertError(f"收件人 {user} 不是站台的使用者")
        ir = report.ir
        state = QueryState.model_validate(alert.state)
        query = worksheet_sql(ir, alert.worksheet, state, member.identity())
        sheet = next(w for w in ir.worksheets if w.id == query.worksheet)
        datasource = next(d for d in ir.datasources if d.id == sheet.datasource)

        # 連到伺服器上的 Published Data Source：與查詢服務相同改用它的萃取，收件人要有「連線」的權限
        sources = DatasourceStore(site.root)
        shared = None
        url = uses_published(datasource)
        if url:
            shared = sources.find(url)
        if shared is not None and not may(
            site.data().permissions(), member.identity(), shared, "Connect"
        ):
            return []

        def database() -> Path:
            if shared is not None and shared.extract:
                path = sources.data_path(shared)
                if path.is_file():
                    return path
            ensure_data(report.directory, ir)
            return report.directory / "data" / f"{sheet.datasource}.duckdb"

        # 來源連線的 Initial SQL 也以收件人代入
        identity = member.identity()
        session = initial_sql.server_session(identity.username, identity.domain, ir.workbook.name)
        with initial_sql.use(session):
            result = runtime.run(query, datasource, database)
        if alert.measure not in result.columns:
            raise AlertError(f"工作表中已經沒有量值 {alert.measure_name}")
        index = result.columns.index(alert.measure)
        dims = [
            i
            for i, column in enumerate(query.columns)
            if column.role == "dimension" and column.key in result.columns
        ]
        marks: list[tuple[str, float]] = []
        for row in result.rows:
            value = row[index]
            if value is None:
                continue
            label = "／".join("Null" if row[i] is None else str(row[i]) for i in dims) or "合計"
            marks.append((label, float(value)))
        return marks

    return evaluate


class AlertChecker:
    """排程器每次檢查之後：檢查萃取剛更新的報表的警示，其餘每小時檢查一次；條件成立時寄信。"""

    def __init__(
        self,
        alerts: AlertStore,
        site: SiteStore | None,
        catalog: Callable[[], Catalog],
        jobs: Callable[[], list[Job]],
        mail: MailSettings | None,
        evaluate: Evaluate | None = None,
        renderer: Renderer | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        threshold: int = SUSPEND_AFTER,
        send: Callable[[EmailMessage, MailSettings], None] | None = None,
    ) -> None:
        self.alerts = alerts
        self.site = site
        self.catalog = catalog
        self.jobs = jobs
        self.mail = mail
        self.evaluate = evaluate or evaluate_with_runtime(site)
        self.renderer = renderer
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.threshold = threshold
        if send is None:
            from tmig_service.subscriptions import send_message

            send = send_message
        self.send = send

    def refreshed(self, records: list[RunRecord]) -> set[str]:
        """這次萃取更新成功的報表。"""
        done = {r.job for r in records if r.kind == "extract" and r.status == "ok"}
        if not done:
            return set()
        targets = [t.report for j in self.jobs() if j.id in done for t in j.targets]
        names = {r.name for r in targets}
        sources = {
            r.url_name.casefold() for r in targets if r.name.startswith("pds:") and r.url_name
        }
        if sources:
            from tmig_service.datasources import uses_published

            for report in self.catalog().reports:
                if any(
                    (uses_published(ds) or "").casefold() in sources for ds in report.ir.datasources
                ):
                    names.add(report.name)
        return names

    def due(self, alert: DataAlert, refreshed: set[str], now: dt.datetime) -> bool:
        if alert.suspended:
            return False
        if alert.report in refreshed or alert.last_checked is None:
            return True
        last = dt.datetime.fromisoformat(alert.last_checked)
        return (now - last).total_seconds() >= CHECK_INTERVAL

    def __call__(self, records: list[RunRecord]) -> list[RunRecord]:
        refreshed = self.refreshed(records)
        now = self.clock()
        checked: list[RunRecord] = []
        for alert in self.alerts.data().alerts:
            if self.due(alert, refreshed, now):
                checked.append(self.check(alert, now))
        return checked

    def run_request(self, job: str, user: str | None) -> RunRecord:
        """排程器佇列中「立即檢查」的要求（alert:<id>）。"""
        alert = self.alerts.get(job.partition(":")[2])
        if alert is None:
            now = self.clock().isoformat()
            return RunRecord(
                job=job,
                kind="alert",
                due=now,
                started=now,
                finished=now,
                status="failed",
                detail="找不到資料警示",
                trigger="manual",
                requested_by=user,
            )
        return self.check(alert, trigger="manual", requested_by=user)

    def check(
        self,
        alert: DataAlert,
        now: dt.datetime | None = None,
        *,
        trigger: Literal["schedule", "manual"] = "schedule",
        requested_by: str | None = None,
    ) -> RunRecord:
        """檢查一個警示（也供立即檢查使用），記下結果並寄信。"""
        now = now or self.clock()
        started = now.isoformat()
        report = self.catalog().report(alert.report)
        sent: dict[str, str] = {}
        try:
            if report is None:
                raise AlertError(f"找不到報表「{alert.report}」")
            matched: dict[str, list[tuple[str, float]]] = {}
            for user in dict.fromkeys(alert.recipients):
                marks = [
                    m
                    for m in self.evaluate(report, alert, user)
                    if met(alert.condition, m[1], alert.threshold)
                ]
                if marks:
                    matched[user] = marks
            throttled = 0
            for user, marks in matched.items():
                last = alert.last_sent.get(user)
                if (
                    last is not None
                    and (now - dt.datetime.fromisoformat(last)).total_seconds()
                    < THROTTLE[alert.frequency]
                ):
                    throttled += 1
                    continue
                if self.deliver(alert, report, user, marks):
                    sent[user] = now.isoformat()
            if sent:
                detail = f"條件成立（{len(matched)} 位）；已寄給 {'、'.join(sent)}"
            elif not matched:
                detail = "條件不成立"
            elif throttled == len(matched):
                detail = "條件成立，這段期間已經寄過"
            elif self.mail is None:
                detail = "條件成立；沒有郵件設定（--mail），沒有寄出"
            else:
                detail = "條件成立；收件人在站台中沒有 email，沒有寄出"
            status: Literal["ok", "failed"] = "ok"
        except Exception as exc:  # 一個警示失敗不影響其他警示
            detail, status = f"{type(exc).__name__}: {exc}", "failed"
        self._record(alert, status, now, sent)
        record = RunRecord(
            job=f"alert:{alert.id}",
            kind="alert",
            due=started,
            started=started,
            finished=self.clock().isoformat(),
            status=status,
            detail=detail,
            trigger=trigger,
            requested_by=requested_by,
        )
        return record

    def _record(
        self, alert: DataAlert, status: str, now: dt.datetime, sent: dict[str, str]
    ) -> None:
        suspended: dict[str, bool] = {"now": False}

        def apply(data: AlertFile) -> None:
            found = next((a for a in data.alerts if a.id == alert.id), None)
            if found is None:
                return
            failures = 0 if status == "ok" else found.consecutive_failures + 1
            updates: dict[str, Any] = {
                "last_checked": now.isoformat(),
                "consecutive_failures": failures,
                "last_sent": {**found.last_sent, **sent},
            }
            if failures and self.threshold and failures >= self.threshold and not found.suspended:
                updates["suspended"] = True
                suspended["now"] = True
            data.alerts[data.alerts.index(found)] = found.model_copy(update=updates)

        self.alerts.change(apply)
        if suspended["now"]:
            SiteStore(self.alerts.root).record("（排程器）", "alert.suspend", alert.id, {})
            try:
                self._mail_suspended(alert)
            except Exception as exc:  # 通知寄不出去不影響排程器
                print(f"無法寄出資料警示 {alert.id} 暫停的通知：{exc}", file=sys.stderr)

    def _email(self, user: str) -> str | None:
        if self.site is None or not self.site.exists():
            return None
        member = self.site.data().user(user)
        return member.email if member is not None else None

    def _link(self, alert: DataAlert, report: Report) -> str:
        kind = "d" if any(d.id == alert.view for d in report.ir.dashboards) else "s"
        path = f"/w/{report.name}/{kind}/{alert.view}"
        return (
            f"{self.mail.site_url}{path}" if self.mail is not None and self.mail.site_url else path
        )

    def deliver(
        self, alert: DataAlert, report: Report, user: str, marks: list[tuple[str, float]]
    ) -> bool:
        if self.mail is None:
            return False
        email = self._email(user)
        if not email:
            return False
        message = EmailMessage()
        message["Subject"] = alert.subject or f"資料警示：{alert.describe()}"
        message["From"] = self.mail.sender
        message["To"] = email
        lines = [
            f"「{report.label}」的 {alert.describe()}：",
            *(f"  {label}：{number_text(value)}" for label, value in marks[:MARK_LIMIT]),
        ]
        if len(marks) > MARK_LIMIT:
            lines.append(f"  …共 {len(marks)} 個標記")
        lines.append(self._link(alert, report))
        message.set_content("\n".join(lines) + "\n")
        if self.renderer is not None:
            try:
                kind = "d" if any(d.id == alert.view for d in report.ir.dashboards) else "s"
                name = next(
                    (
                        v.name
                        for v in [*report.ir.dashboards, *report.ir.worksheets]
                        if v.id == alert.view
                    ),
                    alert.view,
                )
                (view,) = self.renderer.render(
                    user, [(name, f"/w/{report.name}/{kind}/{alert.view}")]
                )
                if view.png is not None:
                    message.add_attachment(
                        view.png, maintype="image", subtype="png", filename=f"{name}.png"
                    )
            except Exception as exc:  # 圖片產生失敗時仍寄出文字
                print(f"資料警示 {alert.id} 的圖片無法產生：{exc}", file=sys.stderr)
        self.send(message, self.mail)
        return True

    def _mail_suspended(self, alert: DataAlert) -> None:
        if self.mail is None:
            return
        email = self._email(alert.owner)
        if not email:
            return
        message = EmailMessage()
        message["Subject"] = f"資料警示已暫停：{alert.subject or alert.describe()}"
        message["From"] = self.mail.sender
        message["To"] = email
        message.set_content(
            f"資料警示「{alert.subject or alert.describe()}」連續 {self.threshold} 次無法檢查，已暫停；"
            "排除問題後在「我的訂閱」中繼續。\n"
        )
        self.send(message, self.mail)


def alert_json(alert: DataAlert, user: str | None = None) -> dict[str, Any]:
    data = json.loads(alert.model_dump_json())
    data["description"] = alert.describe()
    data["frequency_name"] = FREQUENCIES[alert.frequency]
    if user is not None:
        data["mine"] = alert.owner.casefold() == user.casefold()
    return data


__all__ = [
    "CONDITIONS",
    "FREQUENCIES",
    "AlertChecker",
    "AlertError",
    "AlertStore",
    "DataAlert",
    "alert_json",
    "evaluate_with_runtime",
    "met",
]
