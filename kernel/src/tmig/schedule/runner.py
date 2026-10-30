"""排程器（Q2）：依排程檔執行萃取更新與訂閱，並記錄每次執行的結果。

    uv run tmig schedule list schedules.json --fixtures fixtures    # 工作、下次執行時間、對應的報表
    uv run tmig schedule serve schedules.json --fixtures fixtures   # 常駐執行
    uv run tmig schedule tick schedules.json --fixtures fixtures    # 執行一次到期的工作（給作業系統的排程或 Celery beat 呼叫）

- 工作：萃取更新工作（報表的嵌入萃取，或用到某個 Published Data Source 的每份報表）與訂閱（Q3）
- 對應不到報表、排程無法辨識或已暫停的工作不執行，列出原因
- 下次執行時間記在狀態檔（state.json）；停機期間錯過的排程，啟動後補跑一次，再從現在起算下一次
- 同時到期的工作依 Tableau 的優先順序（數字小的先）逐一執行；結果逐行寫入 runs.jsonl
- 時間一律以參數注入（clock），測試不依賴實際時間
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from tmig.catalog import Catalog, Report
from tmig.schedule.model import ExtractTask, Schedule, ScheduleError, ScheduleFile, Subscription

JobKind = Literal["extract", "subscription"]
Clock = Callable[[], dt.datetime]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Target:
    """工作的對象：萃取更新為報表與資料源，訂閱為報表與 view。"""

    report: Report
    datasource: str | None = None
    view: str | None = None

    def describe(self) -> str:
        if self.datasource:
            return f"{self.report.name}／{self.datasource}"
        if self.view:
            return f"{self.report.name}／{self.view}"
        return self.report.name


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
        if task.target_kind == "workbook":
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
        report = catalog.workbook(sub.workbook, sub.workbook_name)
        if report is None:
            job.problem = f"找不到對應的報表（{sub.workbook or sub.workbook_name}）"
        elif sub.content_kind == "view":
            if not any(v == sub.view for v in _view_names(report)):
                job.problem = f"報表「{report.name}」沒有 view「{sub.view}」"
            else:
                job.targets = [Target(report, view=sub.view)]
        else:
            job.targets = [Target(report)]
        if sub.suspended and job.problem is None:
            job.problem = "訂閱已暫停"
        jobs.append(_check_schedule(job))
    return jobs


def _view_names(report: Report) -> list[str]:
    return [d.name for d in report.ir.dashboards] + [w.name for w in report.ir.worksheets]


def _check_schedule(job: Job) -> Job:
    if job.problem is not None:
        return job
    schedule = job.schedule
    if schedule is None:
        job.problem = "找不到排程"
    elif schedule.frequency is None:
        job.problem = f"排程「{schedule.name}」無法辨識：{schedule.problem or '沒有頻率'}"
    elif schedule.state != "Active":
        job.problem = f"排程「{schedule.name}」已暫停"
    return job


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

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


class Handler(Protocol):
    def __call__(self, job: Job) -> str:
        """執行工作，回傳說明；失敗時丟出例外。"""
        ...


class Scheduler:
    """依排程執行工作；狀態（每個工作的下次執行時間）與執行紀錄存在 state_dir。"""

    def __init__(
        self,
        jobs: list[Job],
        timezone: str,
        handlers: dict[JobKind, Handler],
        state_dir: Path,
        clock: Clock = utc_now,
    ) -> None:
        self.jobs = [j for j in jobs if j.runnable]
        self.timezone = timezone
        self.handlers = handlers
        self.state_dir = state_dir
        self.clock = clock
        self.state_path = state_dir / "state.json"
        self.log_path = state_dir / "runs.jsonl"
        self.due: dict[str, dt.datetime] = self._load_state()

    def _load_state(self) -> dict[str, dt.datetime]:
        if not self.state_path.is_file():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {k: dt.datetime.fromisoformat(v) for k, v in raw.get("next", {}).items()}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = {"next": {k: v.isoformat() for k, v in sorted(self.due.items())}}
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def next_due(self, job: Job, after: dt.datetime) -> dt.datetime:
        assert job.schedule is not None and job.schedule.frequency is not None
        return job.schedule.frequency.next_run(after, self.timezone)

    def tick(self) -> list[RunRecord]:
        """執行到期的工作（優先順序數字小的先），回傳執行紀錄；第一次看到的工作只排定下次執行時間。"""
        now = self.clock()
        due: list[tuple[int, str, Job]] = []
        changed = False
        for job in self.jobs:
            when = self.due.get(job.id)
            if when is None:
                self.due[job.id] = self.next_due(job, now)
                changed = True
            elif when <= now:
                due.append((job.priority, job.id, job))
        records: list[RunRecord] = []
        for _, _, job in sorted(due, key=lambda item: (item[0], item[1])):
            records.append(self.run(job, self.due[job.id]))
            # 錯過多次時只補跑一次：下一次從現在起算
            self.due[job.id] = self.next_due(job, max(self.clock(), self.due[job.id]))
            changed = True
        if changed:
            self._save_state()
        return records

    def run(self, job: Job, due: dt.datetime | None = None) -> RunRecord:
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
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as log:
            log.write(record.to_json() + "\n")
        return record

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
    ) -> None:
        """常駐：執行到期的工作，等到下一個工作到期（最多 60 秒檢查一次）。"""
        while not stop():
            for record in self.tick():
                if on_record is not None:
                    on_record(record)
            sleep(self.seconds_until_next())


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
    "Handler",
    "Job",
    "JobKind",
    "RunRecord",
    "Scheduler",
    "Target",
    "extract_handler",
    "last_runs",
    "plan_jobs",
    "utc_now",
]
