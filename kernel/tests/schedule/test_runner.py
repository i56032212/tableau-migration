"""排程器（Q2）：依排程執行萃取更新與訂閱並記錄結果；時間一律以參數注入。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from tmig.catalog import Catalog, Report
from tmig.cli import main
from tmig.ir import ExtractConnection, load_ir
from tmig.schedule.model import ScheduleFile
from tmig.schedule.runner import Job, Scheduler, extract_handler, last_runs, plan_jobs

from ..data.test_extract_refresh import ORDERS, PEOPLE, build_report

ROOT = Path(__file__).resolve().parents[3]
TPE = ZoneInfo("Asia/Taipei")
DEMO_IR = load_ir(ROOT / "fixtures" / "superstore_demo" / "golden.ir.json")


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=TPE)


SCHEDULES = ScheduleFile.model_validate(
    {
        "timezone": "Asia/Taipei",
        "schedules": [
            {
                "id": "daily",
                "name": "每天 6 點",
                "frequency": {"kind": "daily", "start": "06:00:00"},
            },
            {
                "id": "hourly",
                "name": "上午每 2 小時",
                "priority": 10,
                "frequency": {
                    "kind": "hourly",
                    "start": "06:00:00",
                    "end": "12:00:00",
                    "every_hours": 2,
                },
            },
            {
                "id": "paused",
                "name": "暫停的",
                "state": "Suspended",
                "frequency": {"kind": "daily", "start": "01:00:00"},
            },
            {"id": "broken", "name": "壞掉的", "problem": "不支援的排程頻率 'Yearly'"},
        ],
        "extract_refreshes": [
            {
                "id": "t1",
                "schedule": "daily",
                "target_kind": "workbook",
                "content_url": "SalesOverview",
                "priority": 70,
            },
            {
                "id": "t2",
                "schedule": "hourly",
                "target_kind": "workbook",
                "content_url": "SalesOverview",
                "refresh": "incremental",
            },
            {
                "id": "t3",
                "schedule": "daily",
                "target_kind": "datasource",
                "content_url": "NoSuchPDS",
            },
            {
                "id": "t4",
                "schedule": "paused",
                "target_kind": "workbook",
                "content_url": "SalesOverview",
            },
            {
                "id": "t5",
                "schedule": "broken",
                "target_kind": "workbook",
                "content_url": "SalesOverview",
            },
        ],
        "subscriptions": [
            {
                "id": "u1",
                "user": "alice",
                "subject": "週報",
                "schedule": "daily",
                "content_kind": "view",
                "workbook": "SalesOverview",
                "view": "Sales by Region",
            },
            {
                "id": "u2",
                "user": "bob",
                "schedule": "daily",
                "content_kind": "view",
                "workbook": "SalesOverview",
                "view": "Nope",
            },
            {
                "id": "u3",
                "user": "carol",
                "schedule": "daily",
                "content_kind": "workbook",
                "workbook": "SalesOverview",
                "suspended": True,
            },
            {
                "id": "u4",
                "user": "dave",
                "schedule": "daily",
                "content_kind": "workbook",
                "workbook": "Elsewhere",
            },
        ],
    }
)


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    return Catalog([Report(tmp_path / "sales", DEMO_IR, "SalesOverview")])


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def test_plan_jobs(catalog: Catalog) -> None:
    jobs = {j.id: j for j in plan_jobs(SCHEDULES, catalog)}
    assert [t.describe() for t in jobs["extract:t1"].targets] == ["sales／ds_orders"]
    assert jobs["extract:t1"].runnable and jobs["extract:t1"].priority == 70
    assert jobs["extract:t2"].label() == "萃取更新（增量）：SalesOverview"
    assert jobs["extract:t3"].problem == "找不到對應的報表（datasource：NoSuchPDS）"
    assert jobs["extract:t4"].problem == "排程「暫停的」已暫停"
    assert jobs["extract:t5"].problem == "排程「壞掉的」無法辨識：不支援的排程頻率 'Yearly'"
    assert jobs["subscription:u1"].runnable
    assert jobs["subscription:u1"].label() == "訂閱「週報」：alice ← Sales by Region"
    assert jobs["subscription:u2"].problem == "報表「sales」沒有 view「Nope」"
    assert jobs["subscription:u3"].problem == "訂閱已暫停"
    assert jobs["subscription:u4"].problem == "找不到對應的報表（Elsewhere）"


def test_ticks_run_due_jobs_in_priority_order(tmp_path: Path, catalog: Catalog) -> None:
    clock = Clock(at(2, 5, 0))
    calls: list[str] = []

    def handler(job: Job) -> str:
        calls.append(job.id)
        if job.id == "subscription:u1":
            raise RuntimeError("SMTP 無法連線")
        return f"{job.id} 完成"

    jobs = plan_jobs(SCHEDULES, catalog)
    scheduler = Scheduler(
        jobs, "Asia/Taipei", {"extract": handler, "subscription": handler}, tmp_path, clock
    )
    assert scheduler.tick() == []  # 第一次只排定下次執行時間
    assert scheduler.due["extract:t1"] == at(2, 6)
    assert scheduler.seconds_until_next() == 60  # 最多等 60 秒就重新檢查
    assert scheduler.seconds_until_next(limit=7200) == 3600

    clock.now = at(2, 6, 0)
    records = scheduler.tick()
    # 同時到期：優先順序數字小的先（每 2 小時的 10、訂閱 50、每天 6 點的 70）
    assert [r.job for r in records] == ["extract:t2", "subscription:u1", "extract:t1"]
    assert [r.status for r in records] == ["ok", "failed", "ok"]
    assert records[1].detail == "RuntimeError: SMTP 無法連線"
    assert scheduler.due["extract:t2"] == at(2, 8)
    assert scheduler.due["extract:t1"] == at(3, 6)
    assert scheduler.due["subscription:u1"] == at(3, 6)  # 失敗也排定下一次

    clock.now = at(2, 9, 30)
    assert [r.job for r in scheduler.tick()] == ["extract:t2"]
    assert scheduler.due["extract:t2"] == at(2, 10)

    # 停機三天：錯過的排程補跑一次，下一次從現在起算
    clock.now = at(5, 7, 0)
    assert sorted(r.job for r in scheduler.tick()) == [
        "extract:t1",
        "extract:t2",
        "subscription:u1",
    ]
    assert scheduler.due["extract:t2"] == at(5, 8)
    assert scheduler.due["extract:t1"] == at(6, 6)

    # 狀態存在 state.json：重新啟動後沿用
    again = Scheduler(jobs, "Asia/Taipei", {"extract": handler}, tmp_path, clock)
    assert again.due == scheduler.due
    log = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text("utf-8").splitlines()]
    assert len(log) == 7
    assert log[0]["due"] == at(2, 6).isoformat() and log[0]["status"] == "ok"
    last = last_runs(tmp_path)
    assert last["subscription:u1"]["status"] == "failed"
    assert last["extract:t2"]["due"] == at(2, 10).isoformat()  # 補跑的那次記錄錯過的到期時間


def test_manual_run_and_missing_handler(tmp_path: Path, catalog: Catalog) -> None:
    clock = Clock(at(2, 5))
    jobs = plan_jobs(SCHEDULES, catalog)
    scheduler = Scheduler(jobs, "Asia/Taipei", {}, tmp_path, clock)
    job = next(j for j in jobs if j.id == "extract:t1")
    record = scheduler.run(job)
    assert record.status == "failed" and "沒有處理 extract 工作的程式" in record.detail
    assert record.due == at(2, 5).isoformat()


def test_serve_loop(tmp_path: Path, catalog: Catalog) -> None:
    clock = Clock(at(2, 5, 59))
    ran: list[str] = []
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += dt.timedelta(seconds=seconds)

    scheduler = Scheduler(
        plan_jobs(SCHEDULES, catalog),
        "Asia/Taipei",
        {"extract": lambda job: "ok", "subscription": lambda job: "ok"},
        tmp_path,
        clock,
    )
    scheduler.serve(
        sleep=sleep, stop=lambda: len(sleeps) >= 3, on_record=lambda r: ran.append(r.job)
    )
    assert sleeps[0] == 60  # 下一次在 1 分鐘後
    assert sorted(ran) == ["extract:t1", "extract:t2", "subscription:u1"]


# ---------------------------------------------------------------- 萃取更新（實際更新 DuckDB 檔）


def test_extract_job_refreshes_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixtures = tmp_path / "fixtures"
    report = build_report(fixtures / "orders")
    (fixtures / "orders" / "cases.yaml").write_text(
        "workbook: w\ntableau_views_url: https://tableau.example.com/#/site/s/views/OrdersReport\n"
        "sheets: []\ncases: []\n",
        encoding="utf-8",
    )
    for extra in ("snapshot", "orders.hyper"):
        target = fixtures / "orders" / extra
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    source = tmp_path / "source.duckdb"
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE SCHEMA "SALES"')
        for name, table in {"ORDERS": ORDERS, "PEOPLE": PEOPLE}.items():
            con.register("rows", table)
            con.execute(f'CREATE TABLE "SALES"."{name}" AS SELECT * FROM rows')
            con.unregister("rows")
    connection = report.ir.datasources[0].connection
    assert isinstance(connection, ExtractConnection) and connection.source is not None
    config = tmp_path / "connections.toml"
    config.write_text(
        f'[connections.{connection.source.connection_id}]\ndriver = "duckdb"\npath = "{source.as_posix()}"\n',
        encoding="utf-8",
    )
    schedules = ScheduleFile.model_validate(
        {
            "timezone": "Asia/Taipei",
            "schedules": [
                {"id": "s", "name": "每天", "frequency": {"kind": "daily", "start": "06:00:00"}}
            ],
            "extract_refreshes": [
                {
                    "id": "t",
                    "schedule": "s",
                    "target_kind": "workbook",
                    "content_url": "OrdersReport",
                }
            ],
        }
    )
    jobs = plan_jobs(schedules, Catalog.load(fixtures))
    (job,) = jobs
    assert job.runnable, job.problem
    clock = Clock(at(2, 5))
    state = tmp_path / "state"
    scheduler = Scheduler(jobs, "Asia/Taipei", {"extract": extract_handler(config)}, state, clock)
    scheduler.tick()
    clock.now = at(2, 6)
    (record,) = scheduler.tick()
    assert record.status == "ok", record.detail
    ds = report.ir.datasources[0].id
    assert record.detail == f"orders／{ds} 4 列"  # 萃取篩選排除 South
    data = fixtures / "orders" / "data" / f"{ds}.duckdb"
    with duckdb.connect(str(data), read_only=True) as con:
        assert con.execute('SELECT count(*) FROM "Extract"."Extract"').fetchone() == (4,)

    # 命令列：列出工作、以指定時間執行到期的工作、立即執行一個工作
    path = tmp_path / "schedules.json"
    path.write_text(schedules.model_dump_json(), encoding="utf-8")
    common = [
        "--fixtures",
        str(fixtures),
        "--state-dir",
        str(tmp_path / "cli-state"),
        "--config",
        str(config),
    ]
    assert main(["schedule", "list", str(path), *common, "--now", "2026-11-02T05:00:00+08:00"]) == 0
    assert main(["schedule", "tick", str(path), *common, "--now", "2026-11-02T05:00:00+08:00"]) == 0
    assert main(["schedule", "tick", str(path), *common, "--now", "2026-11-02T06:00:00+08:00"]) == 0
    assert main(["schedule", "run", str(path), *common, "--job", "extract:t"]) == 0
    assert main(["schedule", "run", str(path), *common, "--job", "extract:none"]) == 1
    assert len(last_runs(tmp_path / "cli-state")) == 1
