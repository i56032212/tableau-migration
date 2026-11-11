"""排程管理：排程、萃取更新工作與訂閱在新平台上管理；排程器讀報表庫中的排程資料、處理「立即執行」，
連續失敗時暫停並寄信通知。"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import threading
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig.cli import main
from tmig.package import write_package
from tmig.schedule.model import Schedule, ScheduleError, ScheduleFile
from tmig.schedule.runner import Job, Scheduler, queued, request_run
from tmig.verify.run import ensure_data, load_fixture_ir
from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.schedules import FailurePolicy, ScheduleStore, StoreJobs
from tmig_service.site import SiteStore
from tmig_service.store import ReportStore
from tmig_service.subscriptions import MailSettings

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TPE = ZoneInfo("Asia/Taipei")

DIRECTORY = {
    "source": "test",
    "users": [
        {
            "name": "amy",
            "email": "amy@example.com",
            "site_role": "SiteAdministratorCreator",
            "groups": ["All Users"],
        },
        {"name": "bob", "site_role": "Viewer", "groups": ["All Users", "Sales"]},
        {
            "name": "carol",
            "email": "carol@example.com",
            "site_role": "Creator",
            "groups": ["All Users", "Sales"],
        },
        {"name": "dan", "email": "dan@example.com", "site_role": "Viewer", "groups": ["All Users"]},
    ],
}
PERMISSIONS = {
    "projects": [
        {"id": "default", "name": "預設"},
        {
            "id": "p-sales",
            "name": "Sales",
            "workbook_defaults": [
                {"grantee": {"kind": "group", "name": "Sales"}, "capabilities": {"Read": "Allow"}}
            ],
        },
    ]
}
DAILY = {"kind": "daily", "start": "06:00:00"}
MONDAY = {"kind": "weekly", "start": "08:00:00", "weekdays": ["Monday"]}


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("demo")
    shutil.copy(DEMO, work / "demo.twbx")
    ir = load_fixture_ir(work)
    ensure_data(work, ir)
    target = tmp_path_factory.mktemp("package") / "demo.tmigx"
    write_package(target, ir, work)
    return target


def make_app(tmp_path: Path) -> FastAPI:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    settings = AuthSettings(
        mode="dev",
        directory=Directory.model_validate(DIRECTORY),
        permissions=Permissions.model_validate(PERMISSIONS),
    )
    return create_app(
        fixtures, tmp_path / "no-web", auth=settings, store=ReportStore(tmp_path / "store")
    )


def login(app: FastAPI, user: str) -> TestClient:
    client = TestClient(app)
    assert client.post("/auth/dev", json={"username": user}).status_code == 200
    return client


def publish(client: TestClient, package: Path, project: str = "p-sales") -> str:
    response = client.post(
        "/api/reports/publish",
        params={"name": "Superstore", "project": project},
        content=package.read_bytes(),
        headers={"Content-Type": "application/vnd.tmig.tmigx"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["report"]["id"])


def test_schedule_store_lock_and_validation(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    assert store.data().schedules == []

    def add(prefix: str) -> None:
        for n in range(15):

            def apply(data: ScheduleFile, n: int = n) -> None:
                data.schedules.append(
                    Schedule.model_validate(
                        {"id": f"{prefix}{n}", "name": f"{prefix}{n}", "frequency": DAILY}
                    )
                )

            store.change("amy", "schedule.create", f"{prefix}{n}", apply, log=False)

    # 兩個執行緒（模擬查詢服務與排程器）同時修改：每一筆都留下來
    threads = [threading.Thread(target=add, args=(p,)) for p in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ScheduleStore(tmp_path).data().schedules) == 30
    assert not (tmp_path / "schedules.lock").exists()

    def duplicate(data: ScheduleFile) -> None:
        data.schedules.append(
            Schedule.model_validate({"id": "a0x", "name": "A0", "frequency": DAILY})
        )

    with pytest.raises(ScheduleError, match="名稱重複"):
        store.change("amy", "schedule.create", "x", duplicate)
    assert len(store.data().schedules) == 30


def test_admin_manages_schedules_and_tasks(tmp_path: Path, package: Path) -> None:
    app = make_app(tmp_path)
    amy = login(app, "amy")
    report = publish(amy, package)
    assert login(app, "bob").get("/api/admin/schedules").status_code == 403
    # 沒有時區時不能新增排程
    early = amy.post("/api/admin/schedules", json={"name": "每天", "frequency": DAILY})
    assert early.status_code == 422 and "時區" in early.json()["detail"]
    assert (
        amy.patch("/api/admin/schedule-settings", json={"timezone": "Mars/Base"}).status_code == 422
    )
    assert (
        amy.patch("/api/admin/schedule-settings", json={"timezone": "Asia/Taipei"}).status_code
        == 200
    )
    daily = amy.post(
        "/api/admin/schedules", json={"name": "每天 6 點", "kind": "Extract", "frequency": DAILY}
    )
    assert daily.status_code == 201, daily.text
    assert daily.json()["description"] == "每天 06:00" and daily.json()["next_run"]
    assert daily.json()["execution"] == "Parallel"
    weekly = amy.post(
        "/api/admin/schedules",
        json={
            "name": "每週一",
            "kind": "Subscription",
            "frequency": MONDAY,
            "priority": 20,
            "execution": "Serial",
        },
    ).json()
    assert weekly["execution"] == "Serial"
    assert (
        amy.post("/api/admin/schedules", json={"name": "每天 6 點", "frequency": DAILY}).status_code
        == 422
    )
    bad = amy.post(
        "/api/admin/schedules",
        json={"name": "壞的", "frequency": {"kind": "weekly", "start": "08:00"}},
    )
    assert bad.status_code == 422

    # 萃取更新工作：報表要有萃取型的資料源；排程的類型要相符
    wrong = amy.post("/api/admin/extract-tasks", json={"report": report, "schedule": weekly["id"]})
    assert wrong.status_code == 422 and "不是萃取更新的排程" in wrong.json()["detail"]
    task = amy.post(
        "/api/admin/extract-tasks",
        json={"report": report, "schedule": daily.json()["id"], "refresh": "incremental"},
    )
    assert task.status_code == 201, task.text
    body = task.json()
    assert body["targets"] == ["Superstore／ds_orders"]
    assert body["problem"] is None and body["next_run"]
    overview = amy.get("/api/admin/schedules").json()
    assert overview["timezone"] == "Asia/Taipei"
    assert (overview["workers"], overview["default_workers"]) == (None, 2)
    assert overview["scheduler"]["running"] is False

    # 同時執行的工作數上限：排程器每次檢查時重讀；null 改回預設值
    settings = amy.patch("/api/admin/schedule-settings", json={"workers": 4})
    assert settings.status_code == 200 and settings.json()["workers"] == 4
    assert ScheduleStore(tmp_path / "store").data().workers == 4
    assert ScheduleStore(tmp_path / "store").data().timezone == "Asia/Taipei"  # 時區不變
    for bad_settings in ({"workers": 0}, {"workers": 33}, {}, {"timezone": None}):
        assert amy.patch("/api/admin/schedule-settings", json=bad_settings).status_code == 422, (
            bad_settings
        )
    assert (
        login(app, "bob").patch("/api/admin/schedule-settings", json={"workers": 8}).status_code
        == 403
    )
    assert (
        amy.patch("/api/admin/schedule-settings", json={"workers": None}).json()["workers"] is None
    )
    serial = amy.patch(f"/api/admin/schedules/{weekly['id']}", json={"execution": "Parallel"})
    assert serial.json()["execution"] == "Parallel"
    assert [s["tasks"] for s in overview["schedules"]] == [1, 0]
    assert any(r["id"] == report and r["datasources"] for r in overview["reports"])

    # 立即執行：排入排程器的佇列
    run = amy.post(f"/api/admin/jobs/{body['job']}/run")
    assert run.status_code == 202
    assert [q["job"] for q in queued(tmp_path / "store" / "schedule-state")] == [body["job"]]
    assert login(app, "dan").post(f"/api/admin/jobs/{body['job']}/run").status_code == 403

    # 排程還有工作時不能刪除；暫停、繼續工作
    busy = amy.delete(f"/api/admin/schedules/{daily.json()['id']}")
    assert busy.status_code == 422 and "還有 1 個工作" in busy.json()["detail"]
    paused = amy.patch(f"/api/admin/extract-tasks/{body['id']}", json={"suspended": True}).json()
    assert paused["suspended"] is True and paused["next_run"] is None
    edited = amy.patch(
        f"/api/admin/extract-tasks/{body['id']}", json={"suspended": False, "refresh": "full"}
    ).json()
    assert (edited["suspended"], edited["refresh"]) == (False, "full")
    # 改排程的頻率
    changed = amy.patch(
        f"/api/admin/schedules/{daily.json()['id']}",
        json={
            "frequency": {
                "kind": "hourly",
                "start": "08:00:00",
                "end": "18:00:00",
                "every_hours": 2,
            }
        },
    ).json()
    assert changed["description"] == "每天 08:00–18:00 每 2 小時"
    assert amy.delete(f"/api/admin/extract-tasks/{body['id']}").status_code == 204
    assert amy.delete(f"/api/admin/schedules/{daily.json()['id']}").status_code == 204
    actions = [c["action"] for c in amy.get("/api/admin/changes").json()]
    assert actions[:4] == ["schedule.delete", "task.delete", "schedule.update", "task.update"]
    assert "task.run" in actions and "schedule.settings" in actions


def test_self_service_subscriptions(tmp_path: Path, package: Path) -> None:
    app = make_app(tmp_path)
    amy = login(app, "amy")
    report = publish(amy, package)
    amy.patch("/api/admin/schedule-settings", json={"timezone": "Asia/Taipei"})
    weekly = amy.post(
        "/api/admin/schedules", json={"name": "每週一", "kind": "Subscription", "frequency": MONDAY}
    ).json()
    extract = amy.post(
        "/api/admin/schedules", json={"name": "每天", "kind": "Extract", "frequency": DAILY}
    ).json()
    bob = login(app, "bob")
    options = bob.get("/api/subscription-options", params={"report": report}).json()
    assert [s["name"] for s in options["schedules"]] == ["每週一"]  # 只列出訂閱的排程
    assert (options["email"], options["can_subscribe_others"]) == (None, False)
    # 沒有權限檢視報表的人看不到
    assert (
        login(app, "dan").get("/api/subscription-options", params={"report": report}).status_code
        == 404
    )

    view = {"report": report, "view": "銷售總覽", "schedule": weekly["id"], "subject": "週報"}
    no_mail = bob.post("/api/subscriptions", json=view)
    assert no_mail.status_code == 422 and "email" in no_mail.json()["detail"]
    amy.patch("/api/admin/users/bob", json={"email": "bob@example.com"})
    created = bob.post("/api/subscriptions", json=view)
    assert created.status_code == 201, created.text
    mine = created.json()["subscriptions"][0]
    assert (mine["user"], mine["view"], mine["schedule_name"]) == ("bob", "銷售總覽", "每週一")
    assert mine["problem"] is None and mine["next_run"]
    assert (
        bob.post("/api/subscriptions", json={**view, "schedule": extract["id"]}).status_code == 422
    )
    assert bob.post("/api/subscriptions", json={**view, "view": "沒有"}).status_code == 422
    # 只有擁有者、Project Leader 與管理員可以替別人訂閱；訂閱者要有權限
    assert bob.post("/api/subscriptions", json={**view, "users": ["carol"]}).status_code == 403
    denied = amy.post("/api/subscriptions", json={**view, "users": ["dan"]})
    assert denied.status_code == 422 and "沒有檢視這個 view 的權限" in denied.json()["detail"]
    others = amy.post(
        "/api/subscriptions",
        json={**view, "view": None, "users": ["carol"], "attach_pdf": True, "page_size": "A3"},
    )
    assert others.status_code == 201, others.text
    carol_sub = others.json()["subscriptions"][0]
    assert (carol_sub["content_kind"], carol_sub["page_size"]) == ("workbook", "A3")

    listed = bob.get("/api/subscriptions").json()
    assert [s["id"] for s in listed["subscriptions"]] == [mine["id"]]
    updated = bob.patch(
        f"/api/subscriptions/{mine['id']}", json={"subject": "我的週報", "suspended": True}
    )
    assert updated.status_code == 200 and updated.json()["suspended"] is True
    assert (
        bob.patch(f"/api/subscriptions/{carol_sub['id']}", json={"subject": "x"}).status_code == 403
    )
    assert (
        bob.patch(f"/api/subscriptions/{mine['id']}", json={"attach_image": False}).status_code
        == 422
    )
    assert bob.delete(f"/api/subscriptions/{carol_sub['id']}").status_code == 403
    assert bob.delete(f"/api/subscriptions/{mine['id']}").status_code == 204
    overview = amy.get("/api/admin/schedules").json()
    assert [s["user"] for s in overview["subscriptions"]] == ["carol"]


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def test_scheduler_reads_store_queue_and_suspends(tmp_path: Path, package: Path) -> None:
    """排程器（報表庫模式）：修改立即生效、處理立即執行、連續失敗暫停並寄信。"""
    app = make_app(tmp_path)
    amy = login(app, "amy")
    report = publish(amy, package)
    amy.patch("/api/admin/schedule-settings", json={"timezone": "Asia/Taipei"})
    daily = amy.post(
        "/api/admin/schedules", json={"name": "每天", "kind": "Extract", "frequency": DAILY}
    ).json()
    weekly = amy.post(
        "/api/admin/schedules", json={"name": "每週一", "kind": "Subscription", "frequency": MONDAY}
    ).json()
    amy.patch(f"/api/admin/reports/{report}", json={"owner": "carol"})
    task = amy.post(
        "/api/admin/extract-tasks", json={"report": report, "schedule": daily["id"]}
    ).json()
    sub = amy.post(
        "/api/subscriptions",
        json={"report": report, "view": "銷售總覽", "schedule": weekly["id"], "users": ["amy"]},
    ).json()["subscriptions"][0]

    root = tmp_path / "store"
    schedules = ScheduleStore(root)
    sent: list[EmailMessage] = []
    mail = MailSettings(
        host="smtp.invalid", sender="bi@example.com", site_url="https://bi.example.com"
    )
    policy = FailurePolicy(
        schedules, SiteStore(root), mail, threshold=3, send=lambda m, _: sent.append(m)
    )
    calls: list[str] = []

    def failing(job: Job) -> str:
        calls.append(job.id)
        raise RuntimeError("來源資料庫無法連線")

    clock = Clock(dt.datetime(2026, 11, 2, 5, 0, tzinfo=TPE))  # 星期一
    source = StoreJobs(schedules, ReportStore(root), SiteStore(root), tmp_path / "fixtures")
    scheduler = Scheduler(
        [],
        "UTC",
        {"extract": failing, "subscription": failing},
        schedules.state_dir,
        clock,
        source=source,
        on_result=policy,
    )
    assert scheduler.tick() == []
    assert scheduler.due[task["job"]] == dt.datetime(2026, 11, 2, 6, 0, tzinfo=TPE)

    # 改排程的時間：排程器下一次檢查就依新的時間
    amy.patch(
        f"/api/admin/schedules/{daily['id']}",
        json={"frequency": {"kind": "daily", "start": "05:30:00"}},
    )
    scheduler.tick()
    assert scheduler.due[task["job"]] == dt.datetime(2026, 11, 2, 5, 30, tzinfo=TPE)

    # 立即執行（站台管理排入的）：記下觸發的人；失敗寄信給報表的擁有者
    assert amy.post(f"/api/admin/jobs/{task['job']}/run").status_code == 202
    records = scheduler.tick()
    assert [(r.job, r.trigger, r.requested_by, r.status) for r in records] == [
        (task["job"], "manual", "amy", "failed")
    ]
    assert queued(schedules.state_dir) == []
    assert sent[-1]["To"] == "carol@example.com" and "萃取更新失敗" in sent[-1]["Subject"]
    assert "連續失敗：1 次" in sent[-1].get_content()

    # 排程到期又失敗兩次：連續失敗 3 次後暫停，不再排程
    clock.now = dt.datetime(2026, 11, 2, 5, 31, tzinfo=TPE)
    scheduler.tick()
    clock.now = dt.datetime(2026, 11, 3, 5, 31, tzinfo=TPE)
    scheduler.tick()
    data = schedules.data()
    assert (
        data.extract_refreshes[0].consecutive_failures,
        data.extract_refreshes[0].suspended,
    ) == (3, True)
    assert "已暫停" in sent[-1].get_content()
    clock.now = dt.datetime(2026, 11, 4, 5, 31, tzinfo=TPE)
    assert [r.job for r in scheduler.tick()] == []
    overview = amy.get("/api/admin/schedules").json()
    assert overview["extract_tasks"][0]["suspended"] is True
    assert overview["extract_tasks"][0]["last_run"]["status"] == "failed"
    assert "task.suspend" in [c["action"] for c in amy.get("/api/admin/changes").json()]

    # 暫停中的工作也可以立即執行（確認問題排除了）；繼續後重新計算連續失敗
    assert amy.post(f"/api/admin/jobs/{task['job']}/run").status_code == 202
    scheduler.handlers["extract"] = lambda job: "已更新 10 列"
    assert [r.status for r in scheduler.tick()] == ["ok"]
    assert schedules.data().extract_refreshes[0].consecutive_failures == 0
    amy.patch(f"/api/admin/extract-tasks/{task['id']}", json={"suspended": False})
    assert schedules.data().extract_refreshes[0].suspended is False

    # 訂閱連續失敗：暫停時寄信給訂閱者（每次失敗不寄）
    before = len(sent)
    for _ in range(3):
        request_run(schedules.state_dir, sub["job"], "amy")
        scheduler.tick()
    subject = [m["Subject"] for m in sent[before:]]
    assert subject == ["訂閱已暫停：銷售總覽"]
    assert schedules.data().subscriptions[0].suspended is True
    # 執行紀錄與排程器的狀態
    # 執行紀錄：11/2（星期一）08:00 排程到期的一次，加上三次立即執行
    runs = amy.get("/api/admin/runs", params={"job": sub["job"]}).json()
    assert [r["trigger"] for r in runs] == ["manual", "manual", "manual", "schedule"]
    assert all(r["status"] == "failed" for r in runs)


def test_scheduler_heartbeat(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    clock = Clock(dt.datetime(2026, 11, 2, 5, 0, tzinfo=dt.UTC))
    scheduler = Scheduler([], "UTC", {}, store.state_dir, clock, workers=3)
    sleeps: list[float] = []
    scheduler.serve(sleep=sleeps.append, stop=lambda: len(sleeps) >= 1, poll=5)
    assert sleeps == [5]
    status = store.status(clock.now + dt.timedelta(seconds=30))
    assert status["running"] is True and status["queued"] == []
    assert (status["workers"], status["active"], status["waiting"]) == (3, [], 0)
    assert store.status(clock.now + dt.timedelta(hours=1))["running"] is False


def test_cli_store_mode(tmp_path: Path, package: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)
    report = publish(login(app, "amy"), package)
    exported: dict[str, Any] = {
        "source": "https://tableau.example.com",
        "timezone": "Asia/Taipei",
        "schedules": [{"id": "s1", "name": "每天", "kind": "Extract", "frequency": DAILY}],
        "extract_refreshes": [
            {
                "id": "t1",
                "schedule": "s1",
                "target_kind": "workbook",
                "target_name": "superstore_demo",
            },
        ],
    }
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps(exported), encoding="utf-8")
    store = tmp_path / "store"
    assert main(["schedule", "import", str(path), "--store", str(store)]) == 0
    assert "1 個排程、1 個萃取更新工作、0 個訂閱" in capsys.readouterr().out
    assert main(["schedule", "import", str(path), "--store", str(store)]) == 1
    assert "--replace" in capsys.readouterr().err
    assert (
        main(["schedule", "list", "--store", str(store), "--fixtures", str(tmp_path / "fixtures")])
        == 0
    )
    out = capsys.readouterr().out
    # 匯入的工作依報表名稱對應到發布的報表
    assert "extract:t1" in out and "Superstore／ds_orders" in out and report
    assert main(["schedule", "list", str(path), "--store", str(store)]) == 1
    assert "二選一" in capsys.readouterr().err
    assert main(["schedule", "tick", "--store", str(store), "--workers", "0"]) == 1
    assert "--workers 必須是 1 到 32" in capsys.readouterr().err
