"""伺服器上的 Published Data Source：發布 .tdsx（新版本）、權限（檢視、連線、下載）、報表改用伺服器上的萃取、
萃取更新一次所有報表都看到新的資料。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig.catalog import Catalog
from tmig.parser.published import merge_published
from tmig.schedule.model import ScheduleFile
from tmig.schedule.runner import plan_jobs
from tmig.verify.run import ensure_data, load_fixture_ir
from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.datasources import DatasourceStore, uses_published
from tmig_service.schedules import store_reports
from tmig_service.site import SiteStore
from tmig_service.store import ReportStore

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kernel"))
from tests.parser import builder as b  # noqa: E402  # pyright: ignore[reportMissingImports]
from tests.parser.test_published import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    make_hyper,
    sheet,
    sqlproxy_datasource,
    tds,
    write_tdsx,
)

DIRECTORY = {
    "users": [
        {"name": "amy", "site_role": "SiteAdministratorCreator", "groups": ["All Users"]},
        {"name": "bob", "site_role": "Viewer", "groups": ["All Users"]},
        {"name": "carol", "site_role": "Creator", "groups": ["All Users", "Analysts"]},
    ]
}
PERMISSIONS = {
    "projects": [
        {
            "id": "default",
            "name": "預設",
            "rules": [
                {
                    "grantee": {"kind": "group", "name": "Analysts"},
                    "capabilities": {"Read": "Allow", "Write": "Allow"},
                }
            ],
        }
    ],
    "workbooks": [
        {
            "id": "w1",
            "name": "report",
            "content_url": "report",
            "project": "default",
            "rules": [
                {
                    "grantee": {"kind": "group", "name": "All Users"},
                    "capabilities": {"Read": "Allow"},
                }
            ],
        }
    ],
}


@pytest.fixture(scope="module")
def tdsx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("pds")
    make_hyper(work / "pds.hyper", work)
    return write_tdsx(work / "OrdersPDS.tdsx", tds(True), work / "pds.hyper")


@pytest.fixture
def fixtures(tmp_path: Path, tdsx: Path) -> Path:
    """報表 report：連到 OrdersPDS（已經以 N8 併入 .tdsx 的定義與萃取）。"""
    root = tmp_path / "fixtures"
    target = root / "report"
    target.mkdir(parents=True)
    published = tmp_path / "published"
    published.mkdir()
    shutil.copy(tdsx, published / "OrdersPDS.tdsx")
    twb = b.write_twb(
        tmp_path, b.workbook(datasource=sqlproxy_datasource(), worksheets=sheet()), "report.twb"
    )
    merge_published(twb, published, target / "report.twbx")
    ir = load_fixture_ir(target)
    ensure_data(target, ir)
    (target / "cases.yaml").write_text(
        "tableau_views_url: https://tableau.example.com/views/report/x\n", encoding="utf-8"
    )
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


def publish(client: TestClient, tdsx: Path, name: str = "Orders") -> dict:
    response = client.post(
        "/api/datasources/publish",
        params={"name": name, "project": "default"},
        content=tdsx.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def total_sales(client: TestClient, ws: str) -> float:
    result = client.post(f"/api/workbooks/report/worksheets/{ws}/render", json={}).json()
    assert result["table"], result
    index = next(
        i
        for i, key in enumerate(result["table"]["keys"])
        if key.startswith("sum(") and "sales" in key.lower()
    )
    return sum(float(row[index]) for row in result["table"]["raw"])


def test_publish_permissions_and_shared_data(fixtures: Path, tmp_path: Path, tdsx: Path) -> None:
    app = make_app(fixtures, tmp_path)
    bob = login(app, "bob")
    carol = login(app, "carol")
    # 發布要 Project 的發布權限（Viewer 不能發布）
    denied = bob.post(
        "/api/datasources/publish", params={"name": "Orders"}, content=tdsx.read_bytes()
    )
    assert denied.status_code == 403
    first = publish(carol, tdsx)
    assert (first["content_url"], first["version"], first["extract"]) == ("OrdersPDS", 1, True)
    assert first["connection"] == "萃取（oracle）"
    assert [r["id"] for r in first["used_by"]] == ["report"]
    # 再發布同一個網址名稱：新的版本
    second = publish(carol, tdsx)
    assert (second["id"], second["version"]) == (first["id"], 2)
    detail = carol.get(f"/api/datasources/{first['id']}").json()
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert "Profit Ratio" in [f["caption"] for f in detail["fields"]]
    download = carol.get(f"/api/datasources/{first['id']}/versions/1/download")
    assert download.status_code == 200 and download.content == tdsx.read_bytes()

    # 報表改用伺服器上的萃取：把伺服器上的資料改掉，報表的數字跟著改變
    report_ir = load_fixture_ir(fixtures / "report")
    (datasource,) = report_ir.datasources
    assert uses_published(datasource) == "OrdersPDS"
    ws = report_ir.worksheets[0].id
    before = total_sales(bob, ws)
    record = DatasourceStore(tmp_path / "store").get(first["id"])
    assert record is not None
    with duckdb.connect(str(DatasourceStore(tmp_path / "store").data_path(record))) as con:
        con.execute('UPDATE "Extract"."Extract" SET "Sales" = "Sales" * 2')
    assert total_sales(bob, ws) == pytest.approx(before * 2)

    # 沒有「連線」的權限：看不到用到它的報表的資料
    amy = login(app, "amy")
    rules = [
        {
            "grantee": {"kind": "group", "name": "All Users"},
            "capabilities": {"Read": "Allow", "Connect": "Deny"},
        }
    ]
    assert bob.patch(f"/api/datasources/{first['id']}", json={"rules": rules}).status_code == 403
    assert amy.patch(f"/api/datasources/{first['id']}", json={"rules": rules}).status_code == 200
    blocked = bob.post(f"/api/workbooks/report/worksheets/{ws}/render", json={})
    assert blocked.status_code == 403 and "連線" in blocked.json()["detail"]
    assert bob.get(f"/api/datasources/{first['id']}").json()["fields"] == []
    # 看不到的資料源不列出、不能下載；擁有者不受權限規則限制
    hidden = [
        {
            "grantee": {"kind": "group", "name": "Analysts"},
            "capabilities": {"Read": "Allow", "Connect": "Allow"},
        }
    ]
    amy.patch(f"/api/datasources/{first['id']}", json={"rules": hidden})
    assert bob.get("/api/datasources").json() == []
    assert bob.get(f"/api/datasources/{first['id']}/versions/1/download").status_code == 404
    assert carol.get(f"/api/datasources/{first['id']}/versions/1/download").status_code == 200
    # 改名、移動、換擁有者只有管理員；刪除後報表改回自己的資料
    assert carol.patch(f"/api/datasources/{first['id']}", json={"name": "x"}).status_code == 403
    assert (
        amy.patch(
            f"/api/datasources/{first['id']}", json={"name": "訂單", "owner": "carol"}
        ).json()["name"]
        == "訂單"
    )
    assert carol.delete(f"/api/datasources/{first['id']}").status_code == 204
    assert carol.get("/api/datasources").json() == []
    assert total_sales(carol, ws) == pytest.approx(before)
    actions = [c["action"] for c in amy.get("/api/admin/changes").json()]
    assert actions[:3] == ["datasource.delete", "datasource.update", "datasource.update"]


def test_extract_refresh_targets_server_datasource(
    fixtures: Path, tmp_path: Path, tdsx: Path
) -> None:
    """萃取更新的對象是 Published Data Source 時：伺服器上有就更新伺服器上的那一份（一次），不再更新各報表的。"""
    app = make_app(fixtures, tmp_path)
    publish(login(app, "carol"), tdsx)
    schedules = ScheduleFile.model_validate(
        {
            "timezone": "Asia/Taipei",
            "schedules": [
                {"id": "s1", "name": "每天", "frequency": {"kind": "daily", "start": "06:00:00"}}
            ],
            "extract_refreshes": [
                {
                    "id": "t1",
                    "schedule": "s1",
                    "target_kind": "datasource",
                    "content_url": "OrdersPDS",
                }
            ],
        }
    )
    reports = ReportStore(tmp_path / "store")
    site = SiteStore(tmp_path / "store")
    with_server = store_reports(reports, site, fixtures)
    (job,) = plan_jobs(schedules, with_server)
    assert [t.describe() for t in job.targets] == ["資料源：Orders"]
    without = Catalog.load(fixtures)
    (job,) = plan_jobs(schedules, without)
    assert [t.report.name for t in job.targets] == ["report"]


def test_rejects_unsafe_content_url(tmp_path: Path) -> None:
    """.tds 的網址名稱也是檔名：路徑字元一律拒絕，不會寫到報表庫之外。"""
    from tmig_service.datasources import DatasourceError

    evil = tds(False).replace("id='OrdersPDS'", "id='../../escape'")
    path = write_tdsx(tmp_path / "evil.tdsx", evil, None)
    with pytest.raises(DatasourceError, match="網址名稱"):
        DatasourceStore(tmp_path / "store").publish(
            path, name="x", project="default", owner="amy", publisher="amy"
        )
    assert not (tmp_path / "escape.tdsx").exists()
    assert not (tmp_path / "store" / "datasources").exists()


def test_alerts_use_server_datasource(fixtures: Path, tmp_path: Path, tdsx: Path) -> None:
    """資料警示與查詢服務相同：用伺服器上的資料源的萃取；收件人沒有「連線」的權限時不會收到。"""
    from tmig_service.alerts import DataAlert, evaluate_with_runtime

    app = make_app(fixtures, tmp_path)
    first = publish(login(app, "carol"), tdsx)
    root = tmp_path / "store"
    store = DatasourceStore(root)
    record = store.get(first["id"])
    assert record is not None
    with duckdb.connect(str(store.data_path(record))) as con:
        con.execute('UPDATE "Extract"."Extract" SET "Sales" = "Sales" * 10')
    bob = login(app, "bob")
    report = Catalog.load(fixtures).report("report")
    assert report is not None
    ws = report.ir.worksheets[0].id
    keys = bob.post(f"/api/workbooks/report/worksheets/{ws}/render", json={}).json()["table"][
        "keys"
    ]
    measure = next(k for k in keys if k.startswith("sum(") and "sales" in k.lower())
    alert = DataAlert(
        id="a1",
        owner="bob",
        recipients=["bob"],
        report="report",
        view=ws,
        worksheet=ws,
        measure=measure,
        measure_name="Sales",
        condition="above",
        threshold=0,
        created_at="2026-11-01T00:00:00+00:00",
    )
    evaluate = evaluate_with_runtime(SiteStore(root))
    marks = evaluate(report, alert, "bob")
    assert sum(v for _, v in marks) == pytest.approx(total_sales(bob, ws))
    rules = [
        {
            "grantee": {"kind": "group", "name": "All Users"},
            "capabilities": {"Read": "Allow", "Connect": "Deny"},
        }
    ]
    assert (
        login(app, "amy")
        .patch(f"/api/datasources/{first['id']}", json={"rules": rules})
        .status_code
        == 200
    )
    assert evaluate(report, alert, "bob") == []
