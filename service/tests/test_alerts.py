"""資料警示：建立與管理的 API、條件與寄送頻率、以收件人的身分查詢（列層級權限）、連續失敗暫停、
排程器的立即檢查。"""

from __future__ import annotations

import datetime as dt
import shutil
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from conftest import DIRECTORY as RLS_DIRECTORY
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig.catalog import Catalog, Report
from tmig.ir import load_ir
from tmig.schedule.runner import RunRecord, Scheduler, request_run
from tmig_service.alerts import AlertChecker, AlertStore, DataAlert, evaluate_with_runtime, met
from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.schedules import ScheduleStore
from tmig_service.site import SiteStore, from_exports
from tmig_service.store import ReportStore
from tmig_service.subscriptions import MailSettings

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
SHEET = "ws_sales_by_region"
IR = load_ir(DEMO / "golden.ir.json")
BOARD = IR.dashboards[0].id

DIRECTORY = {
    "users": [
        {
            "name": "amy",
            "email": "amy@example.com",
            "site_role": "SiteAdministratorCreator",
            "groups": ["All Users"],
        },
        {"name": "bob", "site_role": "Viewer", "groups": ["All Users"]},
        {
            "name": "carol",
            "email": "carol@example.com",
            "site_role": "Explorer",
            "groups": ["All Users"],
        },
        {"name": "dan", "email": "dan@example.com", "site_role": "Viewer", "groups": ["All Users"]},
    ]
}
PERMISSIONS = {
    "workbooks": [
        {
            "id": "w1",
            "name": "superstore_demo",
            "owner": "amy",
            "rules": [
                {
                    "grantee": {"kind": "group", "name": "All Users"},
                    "capabilities": {"Read": "Allow"},
                },
                {"grantee": {"kind": "user", "name": "dan"}, "capabilities": {"Read": "Deny"}},
            ],
        }
    ]
}


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fixtures")
    target = root / "superstore_demo"
    target.mkdir()
    for name in ("golden.ir.json", "cases.yaml"):
        shutil.copy(DEMO / name, target / name)
    shutil.copytree(DEMO / "data", target / "data")
    return root


def make_app(fixtures: Path, tmp_path: Path) -> FastAPI:
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


def alert_body(**extra: Any) -> dict[str, Any]:
    return {
        "report": "superstore_demo",
        "view": BOARD,
        "worksheet": SHEET,
        "measure": "sum(f_sales)",
        "condition": "above",
        "threshold": 250000,
        **extra,
    }


def test_conditions() -> None:
    assert met("above", 2, 1) and not met("above", 1, 1)
    assert met("above_or_equal", 1, 1) and met("below", 0, 1) and met("below_or_equal", 1, 1)
    assert met("equal", 0.1 + 0.2, 0.3) and not met("equal", 1, 2)


def test_alert_api(demo: Path, tmp_path: Path) -> None:
    app = make_app(demo, tmp_path)
    carol = login(app, "carol")
    options = carol.post(
        "/api/alert-options", json={"report": "superstore_demo", "worksheet": SHEET}
    ).json()
    sales = next(m for m in options["measures"] if m["key"] == "sum(f_sales)")
    assert sales["name"] == "Sales" and 150000 < sales["min"] < sales["max"] < 300000
    assert options["can_add_others"] is False and options["email"] == "carol@example.com"
    assert carol.post("/api/alerts", json=alert_body(measure="nope")).status_code == 422
    created = carol.post("/api/alerts", json=alert_body(subject="業績很好", frequency="daily"))
    assert created.status_code == 201, created.text
    alert = created.json()
    assert (alert["owner"], alert["recipients"], alert["description"]) == (
        "carol",
        ["carol"],
        "Sales 高於 250,000",
    )
    # 沒有 email 的人收不到；不能替別人建立
    bob = login(app, "bob")
    assert bob.post("/api/alerts", json=alert_body()).status_code == 422
    assert bob.post("/api/alerts", json=alert_body(recipients=["carol"])).status_code == 403
    # 管理員（報表的擁有者）可以加入收件人；收件人要能檢視報表
    amy = login(app, "amy")
    denied = amy.post("/api/alerts", json=alert_body(recipients=["amy", "dan"]))
    assert denied.status_code == 422 and "沒有檢視" in denied.json()["detail"]
    shared = amy.post(
        "/api/alerts",
        json=alert_body(recipients=["amy", "carol"], condition="below", threshold=100),
    ).json()
    mine = carol.get("/api/alerts").json()["alerts"]
    assert {(a["id"], a["mine"]) for a in mine} == {(alert["id"], True), (shared["id"], False)}
    # 只有建立的人與管理員可以修改；收件人可以不再收到
    assert carol.patch(f"/api/alerts/{shared['id']}", json={"threshold": 1}).status_code == 403
    assert carol.post(f"/api/alerts/{shared['id']}/leave").json()["recipients"] == ["amy"]
    updated = carol.patch(
        f"/api/alerts/{alert['id']}", json={"threshold": 260000, "suspended": True}
    ).json()
    assert updated["threshold"] == 260000 and updated["suspended"] is True
    assert carol.post(f"/api/alerts/{alert['id']}/check").status_code == 202
    assert login(app, "bob").get("/api/admin/alerts").status_code == 403
    assert len(amy.get("/api/admin/alerts").json()["alerts"]) == 2
    assert carol.delete(f"/api/alerts/{alert['id']}").status_code == 204
    actions = [c["action"] for c in amy.get("/api/admin/changes").json()]
    assert actions[:2] == ["alert.delete", "alert.update"]


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def test_checker_frequency_and_failures(tmp_path: Path) -> None:
    store = AlertStore(tmp_path)
    site = SiteStore(tmp_path)
    site.initialize(from_exports(None, Directory.model_validate(DIRECTORY)), actor="test")
    alert = store.create(
        {
            "owner": "carol",
            "recipients": ["carol", "amy"],
            "report": "superstore_demo",
            "view": BOARD,
            "worksheet": SHEET,
            "measure": "sum(f_sales)",
            "measure_name": "Sales",
            "condition": "above",
            "threshold": 100,
            "frequency": "daily",
        }
    )
    values: dict[str, list[tuple[str, float]]] = {
        "carol": [("West", 250.0), ("East", 50.0)],
        "amy": [("West", 90.0)],
    }
    broken = {"on": False}

    def evaluate(report: Report, item: DataAlert, user: str) -> list[tuple[str, float]]:
        if broken["on"]:
            raise RuntimeError("來源資料庫無法連線")
        return values[user]

    sent: list[EmailMessage] = []
    clock = Clock(dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.UTC))
    catalog = Catalog([Report(DEMO, IR, key="superstore_demo", title="Superstore")])
    checker = AlertChecker(
        store,
        site,
        lambda: catalog,
        lambda: [],
        MailSettings(
            host="smtp.invalid", sender="bi@example.com", site_url="https://bi.example.com"
        ),
        evaluate=evaluate,
        clock=clock,
        threshold=2,
        send=lambda message, _: sent.append(message),
    )
    (record,) = checker([])
    assert record.status == "ok" and "已寄給 carol" in record.detail
    # 只寄給條件成立的收件人，郵件只列出成立的標記
    assert [m["To"] for m in sent] == ["carol@example.com"]
    body = sent[0].get_content()
    assert (
        "West：250" in body
        and "East" not in body
        and f"https://bi.example.com/w/superstore_demo/d/{BOARD}" in body
    )
    # 一小時內不再檢查；萃取更新後立即檢查，但「最多每天一次」這天已經寄過
    clock.now += dt.timedelta(minutes=30)
    assert checker([]) == []
    refreshed = RunRecord("extract:t1", "extract", "", "", "", "ok", "")
    checker.refreshed = lambda records: {"superstore_demo"} if records else set()  # type: ignore[method-assign]
    (again,) = checker([refreshed])
    assert again.detail == "條件成立，這段期間已經寄過" and len(sent) == 1
    clock.now += dt.timedelta(days=1)
    checker([])
    assert len(sent) == 2
    # 查詢失敗：連續失敗 2 次後暫停並通知建立的人
    broken["on"] = True
    clock.now += dt.timedelta(hours=2)
    assert checker([])[0].status == "failed"
    clock.now += dt.timedelta(hours=2)
    checker([])
    current = store.get(alert.id)
    assert current is not None and current.suspended and current.consecutive_failures == 2
    assert (
        sent[-1]["Subject"].startswith("資料警示已暫停") and sent[-1]["To"] == "carol@example.com"
    )
    clock.now += dt.timedelta(hours=2)
    assert checker([]) == []  # 暫停中不檢查


def test_evaluate_with_row_level_security(fixtures: Path, tmp_path: Path) -> None:
    """每位收件人以自己的身分查詢：alice 只看得到 East，dave（Managers）看得到全部。"""
    site = SiteStore(tmp_path)
    site.initialize(from_exports(None, RLS_DIRECTORY), actor="test")
    report = Catalog.load(fixtures).report("secure")
    assert report is not None
    evaluate = evaluate_with_runtime(site)
    alert = DataAlert(
        id="a1",
        owner="dave",
        recipients=["alice", "dave"],
        report="secure",
        view=SHEET,
        worksheet=SHEET,
        measure="sum(f_sales)",
        measure_name="Sales",
        condition="above",
        threshold=0,
        created_at="2026-11-01T00:00:00+00:00",
    )
    alice = evaluate(report, alert, "alice")
    dave = evaluate(report, alert, "dave")
    assert alice and all(label.startswith("East") for label, _ in alice)
    assert {label.split("／")[0] for label, _ in dave} > {"East"}


def test_scheduler_runs_alert_checks(tmp_path: Path) -> None:
    store = AlertStore(tmp_path)
    schedules = ScheduleStore(tmp_path)
    alert = store.create(
        {
            "owner": "carol",
            "recipients": ["carol"],
            "report": "superstore_demo",
            "view": BOARD,
            "worksheet": SHEET,
            "measure": "sum(f_sales)",
            "measure_name": "Sales",
            "condition": "above",
            "threshold": 1,
        }
    )
    catalog = Catalog([Report(DEMO, IR, key="superstore_demo")])
    checker = AlertChecker(
        store, None, lambda: catalog, lambda: [], None, evaluate=lambda r, a, u: [("West", 2.0)]
    )
    scheduler = Scheduler(
        [],
        "UTC",
        {},
        schedules.state_dir,
        after_tick=checker,
        extras={"alert": checker.run_request},
    )
    first = scheduler.tick()
    assert [(r.job, r.trigger) for r in first] == [(f"alert:{alert.id}", "schedule")]
    assert scheduler.tick() == []  # 一小時內不再檢查
    request_run(schedules.state_dir, f"alert:{alert.id}", "carol")
    request_run(schedules.state_dir, "alert:nope", "carol")
    records = scheduler.tick()
    assert [(r.job, r.trigger, r.status) for r in records] == [
        (f"alert:{alert.id}", "manual", "ok"),
        ("alert:nope", "manual", "failed"),
    ]
    runs = schedules.runs()
    assert [r["job"] for r in runs][:2] == ["alert:nope", f"alert:{alert.id}"]


def test_moved_reports_are_named_by_the_report_not_the_folder(
    fixtures: Path, tmp_path: Path
) -> None:
    """移轉的報表放在 fixtures 的目錄裡（這裡是 secure，報表本身叫 superstore_demo）：資料警示的信、
    「我的訂閱」與萃取更新工作列出的報表名稱是報表本身的名稱，不是目錄名稱這個內部的代號
    （稽核 G7：警示信寫「secure」；G10：我的訂閱寫「secure／銷售總覽」）。"""
    from tmig_service.schedules import store_reports

    # 排程器（報表庫模式）的報表目錄：警示與失敗通知的信用它的名稱
    catalog = store_reports(ReportStore(tmp_path / "scheduler"), None, fixtures)
    report = catalog.report("secure")
    assert report is not None and (report.name, report.label) == ("secure", "superstore_demo")
    root = tmp_path / "alerts"
    site = SiteStore(root)
    site.initialize(from_exports(None, Directory.model_validate(DIRECTORY)), actor="test")
    store = AlertStore(root)
    store.create(
        {
            "owner": "carol",
            "recipients": ["carol"],
            "report": "secure",
            "view": BOARD,
            "worksheet": SHEET,
            "measure": "sum(f_sales)",
            "measure_name": "Sales",
            "condition": "above",
            "threshold": 100,
        }
    )
    sent: list[EmailMessage] = []
    checker = AlertChecker(
        store,
        site,
        lambda: catalog,
        lambda: [],
        MailSettings(
            host="smtp.invalid", sender="bi@example.com", site_url="https://bi.example.com"
        ),
        evaluate=lambda r, a, u: [("West", 250.0)],
        send=lambda message, _: sent.append(message),
    )
    (record,) = checker([])
    assert record.status == "ok", record.detail
    body = sent[0].get_content()
    assert body.startswith("「superstore_demo」的 Sales 高於 100：")
    # 連結仍然用報表的 id（網址）
    assert f"https://bi.example.com/w/secure/d/{BOARD}" in body

    # 查詢服務：我的訂閱與萃取更新工作
    settings = AuthSettings(mode="dev", directory=Directory.model_validate(DIRECTORY))
    app = create_app(
        fixtures, tmp_path / "no-web", auth=settings, store=ReportStore(tmp_path / "store")
    )
    amy = login(app, "amy")
    amy.patch("/api/admin/schedule-settings", json={"timezone": "Asia/Taipei"})
    weekly = amy.post(
        "/api/admin/schedules",
        json={
            "name": "每週一",
            "kind": "Subscription",
            "frequency": {"kind": "weekly", "start": "08:00:00", "weekdays": ["Monday"]},
        },
    ).json()
    daily = amy.post(
        "/api/admin/schedules",
        json={
            "name": "每天",
            "kind": "Extract",
            "frequency": {"kind": "daily", "start": "06:00:00"},
        },
    ).json()
    created = amy.post(
        "/api/subscriptions",
        json={"report": "secure", "view": IR.dashboards[0].name, "schedule": weekly["id"]},
    )
    assert created.status_code == 201, created.text
    mine = amy.get("/api/subscriptions").json()["subscriptions"]
    assert [(s["report_id"], s["report_name"]) for s in mine] == [("secure", "superstore_demo")]
    whole = amy.post(
        "/api/subscriptions", json={"report": "secure", "view": None, "schedule": weekly["id"]}
    )
    assert whole.json()["subscriptions"][0]["subject"] == "superstore_demo"
    task = amy.post("/api/admin/extract-tasks", json={"report": "secure", "schedule": daily["id"]})
    assert task.status_code == 201, task.text
    assert task.json()["targets"] == ["superstore_demo／ds_orders"]
    assert task.json()["label"] == "萃取更新（完整）：superstore_demo"
    names = {r["id"]: r["name"] for r in amy.get("/api/admin/schedules").json()["reports"]}
    assert names == {"secure": "superstore_demo"}
