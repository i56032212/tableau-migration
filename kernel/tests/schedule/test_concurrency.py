"""排程器的同時執行：上限（workers）、Serial 的排程、同一份報表的萃取更新與訂閱、常駐時不被長的工作擋住。"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from tmig.catalog import Catalog, Report
from tmig.ir import load_ir
from tmig.ir.models import PublishedSource
from tmig.schedule.model import DEFAULT_WORKERS, ScheduleFile
from tmig.schedule.runner import (
    STATUS,
    Job,
    Scheduler,
    claims,
    plan_jobs,
    request_run,
    select_runs,
)

ROOT = Path(__file__).resolve().parents[3]
TPE = ZoneInfo("Asia/Taipei")
DEMO_IR = load_ir(ROOT / "fixtures" / "superstore_demo" / "golden.ir.json")
WAIT = 10.0
"""等其他執行緒的上限（秒）：正常情況下遠小於這個時間。"""


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=TPE)


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def catalog(tmp_path: Path, *names: str) -> Catalog:
    return Catalog([Report(tmp_path / name, DEMO_IR, name) for name in names])


def schedule_file(
    tasks: list[dict[str, object]],
    subscriptions: list[dict[str, object]] | None = None,
    *,
    serial: bool = False,
) -> ScheduleFile:
    return ScheduleFile.model_validate(
        {
            "timezone": "Asia/Taipei",
            "schedules": [
                {
                    "id": "daily",
                    "name": "每天 6 點",
                    "frequency": {"kind": "daily", "start": "06:00:00"},
                },
                {
                    "id": "serial",
                    "name": "每天 6 點（一個接一個）",
                    "execution": "Serial" if serial else "Parallel",
                    "frequency": {"kind": "daily", "start": "06:00:00"},
                },
            ],
            "extract_refreshes": tasks,
            "subscriptions": subscriptions or [],
        }
    )


def task(name: str, schedule: str = "daily", priority: int | None = None) -> dict[str, object]:
    return {
        "id": f"t{name}",
        "schedule": schedule,
        "target_kind": "workbook",
        "content_url": name,
        "priority": priority,
    }


def subscription(name: str, report: str, priority_schedule: str = "daily") -> dict[str, object]:
    return {
        "id": f"u{name}",
        "user": "alice",
        "schedule": priority_schedule,
        "content_kind": "view",
        "workbook": report,
        "view": "Sales by Region",
    }


class Tracker:
    """記下每個工作開始與結束的時間（time.perf_counter：Windows 上 monotonic 的解析度只有 15 毫秒），
    以及同時執行的最大數量。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started: dict[str, float] = {}
        self.ended: dict[str, float] = {}
        self.events: dict[str, threading.Event] = {}

    def event(self, name: str) -> threading.Event:
        with self.lock:
            return self.events.setdefault(name, threading.Event())

    def enter(self, job: Job) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started[job.id] = time.perf_counter()
        self.event(f"started:{job.id}").set()

    def leave(self, job: Job) -> None:
        with self.lock:
            self.active -= 1
            self.ended[job.id] = time.perf_counter()

    def overlap(self, a: str, b: str) -> bool:
        return self.started[a] < self.ended[b] and self.started[b] < self.ended[a]


def test_runs_at_most_workers_jobs_at_once(tmp_path: Path) -> None:
    tracker = Tracker()
    two = threading.Event()

    def handler(job: Job) -> str:
        tracker.enter(job)
        with tracker.lock:
            if tracker.active >= 2:
                two.set()
        # 前兩個工作互相等到對方也開始：證明確實同時執行
        assert two.wait(WAIT)
        time.sleep(0.02)
        tracker.leave(job)
        return "完成"

    names = "ABCDE"
    clock = Clock(at(2, 5))
    scheduler = Scheduler(
        plan_jobs(schedule_file([task(n) for n in names]), catalog(tmp_path, *names)),
        "Asia/Taipei",
        {"extract": handler},
        tmp_path / "state",
        clock,
        workers=2,
    )
    assert scheduler.workers == 2
    assert scheduler.tick() == []
    clock.now = at(2, 6)
    records = scheduler.tick()
    assert sorted(r.job for r in records) == [f"extract:t{n}" for n in names]
    assert all(r.status == "ok" for r in records)
    assert tracker.peak == 2  # 不超過上限
    # 紀錄依開始的順序；全部完成後排定下一次
    assert [r.job for r in records[:2]] == ["extract:tA", "extract:tB"]
    assert all(scheduler.due[f"extract:t{n}"] == at(3, 6) for n in names)
    log = (tmp_path / "state" / "runs.jsonl").read_text("utf-8").splitlines()
    assert len(log) == 5 and all(json.loads(line)["status"] == "ok" for line in log)
    assert scheduler.running == [] and scheduler.pending == []


def test_workers_setting_is_read_each_time(tmp_path: Path) -> None:
    setting: list[int | None] = [None]
    scheduler = Scheduler([], "UTC", {}, tmp_path, Clock(at(2, 5)), workers=lambda: setting[0])
    assert scheduler.workers == DEFAULT_WORKERS  # 沒有設定時為預設值
    setting[0] = 6
    assert scheduler.workers == 6
    setting[0] = 1000
    assert scheduler.workers == 32  # 上限


def test_serial_schedule_runs_one_after_another(tmp_path: Path) -> None:
    tracker = Tracker()

    def handler(job: Job) -> str:
        tracker.enter(job)
        # Serial 排程的第一個工作與 Parallel 排程的兩個工作互相等到對方也開始：證明同時執行
        together = {"extract:tA", "extract:tD", "extract:tE"}
        if job.id in together:
            for other in together - {job.id}:
                assert tracker.event(f"started:{other}").wait(WAIT)
        time.sleep(0.02)
        tracker.leave(job)
        return "完成"

    data = schedule_file(
        [task("A", "serial"), task("B", "serial"), task("C", "serial"), task("D"), task("E")],
        serial=True,
    )
    clock = Clock(at(2, 5))
    scheduler = Scheduler(
        plan_jobs(data, catalog(tmp_path, *"ABCDE")),
        "Asia/Taipei",
        {"extract": handler},
        tmp_path / "state",
        clock,
        workers=4,
    )
    scheduler.tick()
    clock.now = at(2, 6)
    records = scheduler.tick()
    assert len(records) == 5 and all(r.status == "ok" for r in records), records
    serial = ["extract:tA", "extract:tB", "extract:tC"]
    assert sorted(serial, key=tracker.started.__getitem__) == serial  # 依優先順序與 id
    for i, a in enumerate(serial):
        for b in serial[i + 1 :]:
            assert not tracker.overlap(a, b), (a, b)
    assert tracker.overlap("extract:tD", "extract:tE")
    assert tracker.overlap("extract:tA", "extract:tD")


def test_subscription_waits_for_refresh_of_same_report(tmp_path: Path) -> None:
    tracker = Tracker()

    def refresh(job: Job) -> str:
        tracker.enter(job)
        # 萃取更新執行中：別份報表的訂閱照常同時執行
        assert tracker.event("started:subscription:uB").wait(WAIT)
        time.sleep(0.05)
        tracker.leave(job)
        return "已更新"

    def send(job: Job) -> str:
        tracker.enter(job)
        if job.id == "subscription:uB":
            # 與萃取更新互相等到對方開始：證明同時執行
            assert tracker.event("started:extract:tA").wait(WAIT)
        time.sleep(0.02)
        tracker.leave(job)
        return "已寄出"

    data = schedule_file(
        [task("A", priority=10), task("A2", priority=90) | {"content_url": "A"}],
        [subscription("A", "A"), subscription("B", "B")],
    )
    clock = Clock(at(2, 5))
    scheduler = Scheduler(
        plan_jobs(data, catalog(tmp_path, "A", "B")),
        "Asia/Taipei",
        {"extract": refresh, "subscription": send},
        tmp_path / "state",
        clock,
        workers=4,
    )
    scheduler.tick()
    clock.now = at(2, 6)
    records = scheduler.tick()
    assert sorted(r.job for r in records) == [
        "extract:tA",
        "extract:tA2",
        "subscription:uA",
        "subscription:uB",
    ]
    assert all(r.status == "ok" for r in records), [r.detail for r in records]
    # 同一份報表：訂閱等排在前面的萃取更新完成，排在後面的萃取更新等訂閱寄出（不插隊）
    assert tracker.started["subscription:uA"] >= tracker.ended["extract:tA"]
    assert tracker.started["extract:tA2"] >= tracker.ended["subscription:uA"]
    assert tracker.overlap("extract:tA", "subscription:uB")


def test_claims_and_selection() -> None:
    root = Path("/reports")
    pds = Report(root / "orders", DEMO_IR, "OrdersPDS", key="pds:orders")
    source = DEMO_IR.datasources[0]
    linked_ir = DEMO_IR.model_copy(
        update={
            "datasources": [
                source.model_copy(update={"published": PublishedSource(content_url="OrdersPDS")}),
                *DEMO_IR.datasources[1:],
            ]
        }
    )
    sales = Report(root / "sales", linked_ir, "Sales")
    data = ScheduleFile.model_validate(
        {
            "timezone": "UTC",
            "schedules": [
                {"id": "d", "name": "每天", "frequency": {"kind": "daily", "start": "06:00:00"}}
            ],
            "extract_refreshes": [
                {"id": "p", "schedule": "d", "target_kind": "workbook", "report": "pds:orders"}
            ],
            "subscriptions": [
                {
                    "id": "s",
                    "user": "amy",
                    "schedule": "d",
                    "content_kind": "workbook",
                    "report": "sales",
                }
            ],
        }
    )
    jobs = {j.id: j for j in plan_jobs(data, Catalog([sales], sources=[pds]))}
    writes, reads = claims(jobs["extract:p"])
    assert writes == {"job:extract:p", f"report:{root / 'orders'}", "pds:orderspds"}
    assert reads == set()
    writes, reads = claims(jobs["subscription:s"])
    assert writes == {"job:subscription:s"}
    assert reads == {f"report:{root / 'sales'}", "pds:orderspds"}  # 報表連到伺服器上的資料源

    scheduler = Scheduler(list(jobs.values()), "UTC", {}, Path("/nonexistent"), Clock(at(2, 6)))
    refresh = scheduler._new_run(jobs["extract:p"], at(2, 6), "schedule", None)
    send = scheduler._new_run(jobs["subscription:s"], at(2, 6), "schedule", None)
    again = scheduler._new_run(jobs["subscription:s"], None, "manual", "amy")
    # 立即執行排在最前面：資料源的更新等它（訂閱讀取資料源），同一個訂閱排程到期的那次也等它
    assert select_runs([refresh, send, again], [], 3) == [again]
    # 同時到期：萃取更新（id 在前）先，訂閱等它完成
    assert select_runs([send, refresh], [], 3) == [refresh]
    assert select_runs([send], [refresh], 3) == []
    assert select_runs([send], [], 3) == [send]
    assert select_runs([send, refresh], [], 0) == []


def test_serve_keeps_checking_while_a_long_job_runs(tmp_path: Path) -> None:
    release = threading.Event()
    started = threading.Event()
    ran: list[str] = []

    def handler(job: Job) -> str:
        if job.id == "extract:tA":
            started.set()
            assert release.wait(WAIT)
        return "完成"

    clock = Clock(at(2, 6) - dt.timedelta(milliseconds=10))
    state = tmp_path / "state"
    scheduler = Scheduler(
        plan_jobs(schedule_file([task("A"), task("B")]), catalog(tmp_path, "A", "B")),
        "Asia/Taipei",
        {"extract": handler},
        state,
        clock,
        workers=2,
    )
    beats: list[dict[str, object]] = []
    sleeps: list[float] = []
    loops = [0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += dt.timedelta(seconds=seconds)

    def stop() -> bool:
        loops[0] += 1
        if loops[0] > 20_000:  # 保險：不應該發生
            release.set()
            return True
        path = state / STATUS
        status = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        if started.is_set() and not release.is_set():
            # 長的工作執行中：排程器照常檢查（心跳記下執行中的工作、立即執行照常執行）
            if status.get("active") and not beats:
                beats.append(status)
                request_run(state, "extract:tB", "amy")
            elif ran.count("extract:tB") >= 2:
                release.set()
        return release.is_set() and len(ran) >= 3 and not scheduler.running

    scheduler.serve(sleep=sleep, stop=stop, on_record=lambda r: ran.append(r.job), poll=0.01)
    assert loops[0] < 20_000
    assert sleeps[0] == 0.01  # 下一次在 06:00
    assert ran == ["extract:tB", "extract:tB", "extract:tA"]  # 立即執行的 tB 在 tA 執行中完成
    (status,) = beats
    assert status["workers"] == 2
    assert "extract:tA" in [a["job"] for a in status["active"]]  # type: ignore[union-attr]
    assert scheduler.due["extract:tA"] == at(3, 6)
    assert scheduler.running == [] and scheduler.pending == []
    runs = [json.loads(line) for line in (state / "runs.jsonl").read_text("utf-8").splitlines()]
    assert [(r["job"], r["trigger"]) for r in runs] == [
        ("extract:tB", "schedule"),
        ("extract:tB", "manual"),
        ("extract:tA", "schedule"),
    ]
