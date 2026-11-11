"""站台管理：Project、使用者、群組、報表與權限在新平台上修改，修改後立即生效。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig.cli import main
from tmig.package import write_package
from tmig.verify.run import ensure_data, load_fixture_ir
from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.site import SiteStore
from tmig_service.store import ReportStore

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"

DIRECTORY = {
    "source": "https://tableau.example.com（site：sales）",
    "users": [
        {
            "name": "amy",
            "email": "amy@example.com",
            "site_role": "SiteAdministratorCreator",
            "groups": ["All Users"],
        },
        {"name": "bob", "site_role": "Viewer", "groups": ["All Users", "Sales"]},
        {"name": "carol", "site_role": "Creator", "groups": ["All Users", "Sales"]},
    ],
}
PERMISSIONS = {
    "projects": [
        {
            "id": "default",
            "name": "預設",
            "rules": [
                {
                    "grantee": {"kind": "group", "name": "All Users"},
                    "capabilities": {"Write": "Allow"},
                }
            ],
        },
        {
            "id": "p-sales",
            "name": "Sales",
            "owner": "amy",
            "workbook_defaults": [
                {"grantee": {"kind": "group", "name": "Sales"}, "capabilities": {"Read": "Allow"}}
            ],
        },
    ],
    "workbooks": [
        {
            "id": "w-legacy",
            "name": "舊報表",
            "content_url": "Legacy",
            "project": "p-sales",
            "owner": "carol",
            "show_tabs": False,
            "rules": [
                {"grantee": {"kind": "group", "name": "Sales"}, "capabilities": {"Read": "Allow"}}
            ],
            "views": [{"id": "v1", "name": "Overview", "rules": []}],
        }
    ],
}


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
    response = client.post("/auth/dev", json={"username": user})
    assert response.status_code == 200, response.text
    return client


def test_site_is_imported_on_first_start(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    site = SiteStore(tmp_path / "store")
    assert site.exists()
    data = site.data()
    assert [p.name for p in data.projects] == ["預設", "Sales"]
    assert [g.name for g in data.groups] == ["Sales"]
    assert login(app, "bob").get("/api/admin/site").status_code == 403
    amy = login(app, "amy")
    assert amy.get("/api/me").json()["user"]["admin"] is True
    body = amy.get("/api/admin/site").json()
    assert body["source"] == "https://tableau.example.com（site：sales）"
    assert {g["name"]: g["members"] for g in body["groups"]} == {"Sales": ["bob", "carol"]}
    assert body["workbooks"][0]["project_path"] == "Sales"
    assert "ChangePermissions" in body["workbook_capabilities"]


def test_projects(tmp_path: Path) -> None:
    amy = login(make_app(tmp_path), "amy")
    created = amy.post("/api/admin/projects", json={"name": "華北", "parent": "p-sales"})
    assert created.status_code == 201, created.text
    north = created.json()
    assert (north["path"], north["owner"]) == ("Sales / 華北", "amy")
    # 新的 Project 沿用上層的權限
    assert north["workbook_defaults"][0]["grantee"]["name"] == "Sales"
    assert (
        amy.post("/api/admin/projects", json={"name": "華北", "parent": "p-sales"}).status_code
        == 422
    )
    # 不能形成迴圈；可以移到最上層、改名
    loop = amy.patch("/api/admin/projects/p-sales", json={"parent": north["id"], "move": True})
    assert loop.status_code == 422 and "迴圈" in loop.json()["detail"]
    moved = amy.patch(
        f"/api/admin/projects/{north['id']}", json={"parent": None, "move": True, "name": "North"}
    )
    assert moved.json()["path"] == "North"
    locked = amy.patch(
        f"/api/admin/projects/{north['id']}", json={"content_permissions": "LockedToProject"}
    )
    assert locked.json()["content_permissions"] == "LockedToProject"
    bad = amy.patch(
        f"/api/admin/projects/{north['id']}",
        json={
            "rules": [
                {"grantee": {"kind": "group", "name": "Nobody"}, "capabilities": {"Read": "Allow"}}
            ]
        },
    )
    assert bad.status_code == 422 and "Nobody" in bad.json()["detail"]
    # 有內容的 Project 不能刪除
    busy = amy.delete("/api/admin/projects/p-sales")
    assert busy.status_code == 422 and "舊報表" in busy.json()["detail"]
    assert amy.delete(f"/api/admin/projects/{north['id']}").status_code == 204
    assert [c["action"] for c in amy.get("/api/admin/changes").json()][:2] == [
        "project.delete",
        "project.update",
    ]


def test_users_and_groups_take_effect_immediately(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    amy = login(app, "amy")
    created = amy.post(
        "/api/admin/users",
        json={
            "name": "dave",
            "email": "dave@example.com",
            "site_role": "Explorer",
            "groups": ["Sales"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["groups"] == ["All Users", "Sales"]
    dave = login(app, "dave")
    assert dave.get("/api/me").json()["user"]["site_role"] == "Explorer"
    assert amy.post("/api/admin/users", json={"name": "Dave"}).status_code == 422
    # 改了站台角色，下一個請求就生效
    assert dave.get("/api/admin/site").status_code == 403
    amy.patch("/api/admin/users/dave", json={"site_role": "SiteAdministratorExplorer"})
    assert dave.get("/api/admin/site").status_code == 200
    assert amy.patch("/api/admin/users/amy", json={"site_role": "Viewer"}).status_code == 422
    # 群組：建立、改名（權限規則一併改名）、刪除
    group = amy.post("/api/admin/groups", json={"name": "Marketing", "members": ["bob"]})
    assert group.json()["members"] == ["bob"]
    renamed = amy.patch("/api/admin/groups/Sales", json={"name": "Sales TW"})
    assert renamed.json()["name"] == "Sales TW" and "ISMEMBEROF" in renamed.json()["warning"]
    site = amy.get("/api/admin/site").json()
    sales = next(p for p in site["projects"] if p["id"] == "p-sales")
    assert sales["workbook_defaults"][0]["grantee"]["name"] == "Sales TW"
    assert {u["name"]: u["groups"] for u in site["users"]}["bob"] == [
        "All Users",
        "Marketing",
        "Sales TW",
    ]
    assert amy.delete("/api/admin/groups/Marketing").status_code == 204
    assert login(app, "bob").get("/api/me").json()["user"]["groups"] == ["All Users", "Sales TW"]
    # 擁有內容的使用者不能刪除；刪除的使用者不能再登入
    owner = amy.delete("/api/admin/users/carol")
    assert owner.status_code == 422 and "舊報表" in owner.json()["detail"]
    assert amy.delete("/api/admin/users/dave").status_code == 204
    assert TestClient(app).post("/auth/dev", json={"username": "dave"}).status_code == 403


def test_reports_move_rename_permissions_and_delete(tmp_path: Path, package: Path) -> None:
    app = make_app(tmp_path)
    amy = login(app, "amy")
    published = amy.post(
        "/api/reports/publish",
        params={"name": "Superstore", "project": "default"},
        content=package.read_bytes(),
        headers={"Content-Type": "application/vnd.tmig.tmigx"},
    )
    assert published.status_code == 200, published.text
    report = published.json()["report"]["id"]
    bob = login(app, "bob")
    carol = login(app, "carol")
    assert all(w["id"] != report for w in bob.get("/api/workbooks").json())
    # 只有管理員可以改名、移動、換擁有者；移到 Sales、交給 carol
    assert carol.patch(f"/api/admin/reports/{report}", json={"name": "x"}).status_code == 403
    moved = amy.patch(
        f"/api/admin/reports/{report}",
        json={"project": "p-sales", "owner": "carol", "name": "銷售"},
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["project_path"], moved.json()["owner"]) == ("Sales", "carol")
    # 擁有者可以設定權限：開放給 Sales 群組檢視
    rules = [{"grantee": {"kind": "group", "name": "Sales"}, "capabilities": {"Read": "Allow"}}]
    shared = carol.patch(f"/api/admin/reports/{report}", json={"rules": rules})
    assert shared.status_code == 200, shared.text
    assert any(w["id"] == report and w["name"] for w in bob.get("/api/workbooks").json())
    assert bob.patch(f"/api/admin/reports/{report}", json={"rules": []}).status_code == 403
    # Project 鎖定權限時不能在報表上設定
    amy.patch("/api/admin/projects/p-sales", json={"content_permissions": "LockedToProject"})
    assert carol.patch(f"/api/admin/reports/{report}", json={"rules": []}).status_code == 409
    # 沒有刪除權限的人不能刪；刪除後報表移到 trash
    assert bob.delete(f"/api/admin/reports/{report}").status_code == 403
    assert carol.delete(f"/api/admin/reports/{report}").status_code == 204
    assert all(w["id"] != report for w in amy.get("/api/workbooks").json())
    assert len(list((tmp_path / "store" / "trash").iterdir())) == 1
    actions = [c["action"] for c in amy.get("/api/admin/changes").json()]
    assert actions[:3] == ["report.delete", "project.update", "report.update"]


def test_tableau_workbook_permissions(tmp_path: Path) -> None:
    amy = login(make_app(tmp_path), "amy")
    rules = [
        {
            "grantee": {"kind": "user", "name": "bob"},
            "capabilities": {"Read": "Allow", "ExportData": "Deny"},
        }
    ]
    updated = amy.patch(
        "/api/admin/workbooks/w-legacy",
        json={"rules": rules, "views": [{"name": "Overview", "rules": rules}], "owner": "amy"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["views"][0]["rules"][0]["capabilities"] == {
        "Read": "Allow",
        "ExportData": "Deny",
    }
    missing = amy.patch(
        "/api/admin/workbooks/w-legacy", json={"views": [{"name": "Nope", "rules": []}]}
    )
    assert missing.status_code == 422


def test_site_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "directory.json").write_text(json.dumps(DIRECTORY), encoding="utf-8")
    (tmp_path / "permissions.json").write_text(json.dumps(PERMISSIONS), encoding="utf-8")
    store = tmp_path / "store"
    args = [
        "site",
        "import",
        "--store",
        str(store),
        "--directory",
        str(tmp_path / "directory.json"),
    ]
    assert main([*args, "--permissions", str(tmp_path / "permissions.json")]) == 0
    assert "2 個 Project、1 份 workbook 的權限、3 位使用者、1 個群組" in capsys.readouterr().out
    assert main(args) == 1
    assert "--replace" in capsys.readouterr().err
    assert main([*args, "--replace"]) == 0
    assert main(["site", "show", "--store", str(store)]) == 0
    assert "站台管理員：amy" in capsys.readouterr().out
    fresh = tmp_path / "fresh"
    assert main(["site", "init", "--store", str(fresh), "--admin", "root"]) == 0
    assert SiteStore(fresh).data().users[0].site_role == "SiteAdministratorCreator"
