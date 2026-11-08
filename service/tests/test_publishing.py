"""報表庫：發布、版本、比較、回復、下載、伺服器的檢查與權限（D0）。"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmig.access.permissions import Permissions
from tmig.package import write_package
from tmig.verify.run import ensure_data, load_fixture_ir
from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.store import ReportStore

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """示範報表的套件：v1 原樣；v2 改了第一張工作表的名稱；broken 缺少萃取的資料。"""
    work = tmp_path_factory.mktemp("demo")
    shutil.copy(DEMO, work / "demo.twbx")
    ir = load_fixture_ir(work)
    ensure_data(work, ir)
    out = tmp_path_factory.mktemp("packages")
    first = out / "v1.tmigx"
    write_package(first, ir, work)
    sheet = ir.worksheets[0]
    renamed = sheet.model_copy(update={"name": f"{sheet.name}（新）"})
    second = out / "v2.tmigx"
    write_package(second, ir.model_copy(update={"worksheets": [renamed, *ir.worksheets[1:]]}), work)
    broken = out / "broken.tmigx"
    with zipfile.ZipFile(first) as source, zipfile.ZipFile(broken, "w") as target:
        for item in source.infolist():
            if not item.filename.startswith("data/"):
                target.writestr(item, source.read(item.filename))
    return {"v1": first, "v2": second, "broken": broken}


def make_app(
    tmp_path: Path,
    permissions: Permissions | None = None,
    users: tuple[str, ...] = ("amy", "bob"),
    settings: AuthSettings | None = None,
) -> FastAPI:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    settings = settings or AuthSettings(
        mode="dev", dev_users=list(users), require_directory=False, permissions=permissions
    )
    return create_app(
        fixtures, tmp_path / "no-web", auth=settings, store=ReportStore(tmp_path / "store")
    )


def client_for(app: FastAPI, user: str | None) -> TestClient:
    client = TestClient(app)
    if user is not None:
        assert client.post("/auth/dev", json={"username": user}).status_code == 200
    return client


def publish(client: TestClient, package: Path, **params: str | int) -> httpx.Response:
    return client.post(
        "/api/reports/publish",
        params=params,
        content=package.read_bytes(),
        headers={"Content-Type": "application/vnd.tmig.tmigx"},
    )


def test_publish_view_compare_restore_download(tmp_path: Path, packages: dict[str, Path]) -> None:
    app = make_app(tmp_path)
    amy = client_for(app, "amy")
    response = publish(
        amy, packages["v1"], name="Superstore Demo", project="default", comment="第一版"
    )
    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert (report["id"], report["version"], report["publisher"]) == ("superstore_demo", 1, "amy")
    assert response.json()["version"]["comment"] == "第一版"

    # 檢視器列出發布的報表，查詢用套件中的資料
    listed = {w["id"]: w for w in amy.get("/api/workbooks").json()}
    assert listed["superstore_demo"]["published"]["version"] == 1
    ir = amy.get("/api/workbooks/superstore_demo/ir").json()
    sheet = ir["worksheets"][0]["id"]
    rendered = amy.post(f"/api/workbooks/superstore_demo/worksheets/{sheet}/render", json={})
    assert rendered.status_code == 200 and rendered.json()["rows"] > 0

    # 同名（不分大小寫）再發布：要明確覆蓋
    again = publish(amy, packages["v2"], name="superstore demo", project="default")
    assert again.status_code == 409 and again.json()["detail"]["error"] == "exists"
    second = publish(
        amy, packages["v2"], name="Superstore Demo", project="default", overwrite="true"
    )
    assert second.status_code == 200 and second.json()["version"]["version"] == 2
    detail = amy.get("/api/reports/superstore_demo").json()
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    names = [w["name"] for w in amy.get("/api/workbooks/superstore_demo/ir").json()["worksheets"]]
    assert names[0].endswith("（新）")

    # 比較：只列名稱
    compared = amy.get("/api/reports/superstore_demo/compare", params={"base": 1, "target": 2})
    changes = compared.json()["changes"]
    assert [(c["kind"], c["change"]) for c in changes] == [("worksheet", "changed")]
    assert changes[0]["text"].startswith("修改工作表")

    # 回復第一版：發布一個內容相同的新版本，舊的版本不動
    restored = amy.post("/api/reports/superstore_demo/versions/1/restore", json={})
    assert restored.status_code == 200, restored.text
    version = restored.json()["version"]
    assert (version["version"], version["restored_from"], version["comment"]) == (
        3,
        1,
        "回復第 1 版",
    )
    same = amy.get("/api/reports/superstore_demo/compare", params={"base": 1, "target": 3})
    assert same.json()["changes"] == []

    # 下載某一版：與上傳的檔案相同
    download = amy.get("/api/reports/superstore_demo/versions/2/package")
    assert download.status_code == 200
    assert download.content == packages["v2"].read_bytes()
    assert download.headers["x-tmig-version"] == "2"
    latest = amy.get("/api/reports/superstore_demo/versions/latest/package")
    assert latest.headers["x-tmig-version"] == "3"
    assert amy.get("/api/reports/superstore_demo/versions/9/package").status_code == 404
    # 解開的目錄只保留最近兩版
    current = tmp_path / "store" / "reports" / "superstore_demo" / "current"
    assert sorted(p.name for p in current.iterdir()) == ["2", "3"]


def test_conflict_lists_both_sides(tmp_path: Path, packages: dict[str, Path]) -> None:
    app = make_app(tmp_path)
    amy, bob = client_for(app, "amy"), client_for(app, "bob")
    assert publish(amy, packages["v1"], name="Demo", project="default").status_code == 200
    # bob 從第 1 版改了一張工作表的名稱並發布
    assert publish(bob, packages["v2"], report="demo", base_version=1).status_code == 200
    # amy 也是從第 1 版開始改的：伺服器上已有較新的版本
    stale = publish(amy, packages["v1"], report="demo", base_version=1, comment="amy 的修改")
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "conflict" and detail["latest"]["publisher"] == "bob"
    assert [c["change"] for c in detail["server_changes"]] == ["changed"]
    assert detail["local_changes"] == []
    assert [c["change"] for c in detail["overwrite_changes"]] == ["changed"]
    # 作者決定覆蓋
    forced = publish(amy, packages["v1"], report="demo", base_version=1, force="true")
    assert forced.status_code == 200 and forced.json()["version"]["version"] == 3
    # 從最新的版本開始改的：沒有衝突
    assert publish(amy, packages["v2"], report="demo", base_version=3).status_code == 200


def test_server_checks_and_bad_packages(tmp_path: Path, packages: dict[str, Path]) -> None:
    app = make_app(tmp_path)
    amy = client_for(app, "amy")
    broken = publish(amy, packages["broken"], name="Broken", project="default")
    assert broken.status_code == 422
    detail = broken.json()["detail"]
    assert detail["error"] == "checks"
    assert all(p["level"] == "error" for p in detail["problems"]) and detail["problems"]
    garbage = amy.post(
        "/api/reports/publish", params={"name": "X", "project": "default"}, content=b"no"
    )
    assert garbage.status_code == 422 and garbage.json()["detail"]["error"] == "package"
    empty = amy.post(
        "/api/reports/publish", params={"name": "X", "project": "default"}, content=b""
    )
    assert empty.status_code == 422
    unknown = publish(amy, packages["v1"], name="X", project="nowhere")
    assert unknown.status_code == 422 and unknown.json()["detail"]["error"] == "project"
    assert publish(amy, packages["v1"], name="", project="default").status_code == 422
    # 沒有留下任何東西
    assert amy.get("/api/reports").json() == []
    store = tmp_path / "store"
    assert not any((store / "uploads").iterdir())
    assert not (store / "staging").exists() or not any((store / "staging").iterdir())


def test_publishing_needs_sign_in(tmp_path: Path, packages: dict[str, Path]) -> None:
    app = make_app(tmp_path, settings=AuthSettings())
    anonymous = client_for(app, None)
    response = publish(anonymous, packages["v1"], name="Demo", project="default")
    assert response.status_code == 403 and "需要登入" in response.json()["detail"]
    assert anonymous.get("/api/projects").json()[0]["can_publish"] is False


def test_permissions_follow_tableau(tmp_path: Path, packages: dict[str, Path]) -> None:
    permissions = Permissions.model_validate(
        {
            "projects": [
                {
                    "id": "p1",
                    "name": "Sales",
                    "rules": [
                        {
                            "grantee": {"kind": "user", "name": "amy"},
                            "capabilities": {"Write": "Allow"},
                        }
                    ],
                    "workbook_defaults": [
                        {
                            "grantee": {"kind": "group", "name": "All Users"},
                            "capabilities": {"Read": "Allow"},
                        }
                    ],
                },
                {"id": "p2", "name": "華北", "parent": "p1"},
            ],
            "workbooks": [
                {
                    "id": "w1",
                    "name": "Legacy",
                    "project": "p1",
                    "owner": "dave",
                    "rules": [
                        {
                            "grantee": {"kind": "user", "name": "bob"},
                            "capabilities": {
                                "Read": "Allow",
                                "ExportXml": "Allow",
                                "Write": "Allow",
                            },
                        },
                        {
                            "grantee": {"kind": "user", "name": "amy"},
                            "capabilities": {"Write": "Allow"},
                        },
                    ],
                }
            ],
        }
    )
    app = make_app(tmp_path, permissions, users=("amy", "bob", "carol"))
    amy, bob, carol = client_for(app, "amy"), client_for(app, "bob"), client_for(app, "carol")
    projects = {p["id"]: p for p in amy.get("/api/projects").json()}
    assert projects["p1"]["can_publish"] and not projects["p2"]["can_publish"]
    assert projects["p2"]["path"] == "Sales / 華北"
    assert not any(p["can_publish"] for p in bob.get("/api/projects").json())
    assert publish(bob, packages["v1"], name="Bob 的報表", project="p1").status_code == 403

    published = publish(amy, packages["v1"], name="Superstore Demo", project="p1")
    assert published.status_code == 200, published.text
    report = published.json()["report"]
    assert report["owner"] == "amy" and report["can_download"] and report["can_overwrite"]
    # bob 可以看（Project 的 workbook 預設權限），不能下載、覆蓋或回復
    seen = {r["id"]: r for r in bob.get("/api/reports").json()}[report["id"]]
    assert not seen["can_download"] and not seen["can_overwrite"]
    assert bob.get(f"/api/reports/{report['id']}/versions/1/package").status_code == 403
    assert bob.post(f"/api/reports/{report['id']}/versions/1/restore", json={}).status_code == 403
    assert publish(bob, packages["v2"], report=report["id"]).status_code == 403

    # 同名的 Tableau workbook：要明確取代；取代後沿用它的擁有者與權限
    legacy = publish(amy, packages["v1"], name="Legacy", project="p1")
    assert legacy.status_code == 409 and "Tableau" in legacy.json()["detail"]["message"]
    taken = publish(amy, packages["v1"], name="Legacy", project="p1", overwrite="true")
    assert taken.status_code == 200, taken.text
    legacy_report = taken.json()["report"]
    assert legacy_report["owner"] == "dave"
    by_bob = {r["id"]: r for r in bob.get("/api/reports").json()}[legacy_report["id"]]
    # bob 在 Tableau 上可以下載；有 workbook 的覆蓋權限但沒有 Project 的發布權限，仍不能覆蓋
    assert by_bob["can_download"] and not by_bob["can_overwrite"]
    # carol 看不到（Tableau 的 workbook 沒有給她權限）
    assert legacy_report["id"] not in {r["id"] for r in carol.get("/api/reports").json()}
    assert carol.get(f"/api/workbooks/{legacy_report['id']}/ir").status_code == 404
