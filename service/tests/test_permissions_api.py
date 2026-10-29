"""權限（P3）：查詢服務依 Tableau Server 匯出的權限開放報表、view 與報表定義。

以加上列層級權限的示範報表（conftest 的 secure）為例：報表在 Project「Sales」中，
alice 有檢視權限、Managers 群組另有下載 workbook 的權限，carol 沒有任何權限。
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.access.directory import Directory, DirectoryUser
from tmig.access.permissions import Permissions
from tmig_service import create_app
from tmig_service.auth import AuthError, AuthSettings, load_auth

ROOT = Path(__file__).resolve().parents[2]
WB = "/api/workbooks/secure"
DIRECTORY = Directory(
    users=[
        DirectoryUser(name="alice", groups=["All Users"], site_role="Viewer"),
        DirectoryUser(name="bob", groups=["All Users"], site_role="Explorer"),
        DirectoryUser(name="carol", groups=["All Users"], site_role="Viewer"),
        DirectoryUser(name="dave", groups=["All Users", "Managers"], site_role="Explorer"),
        DirectoryUser(name="root", groups=["All Users"], site_role="SiteAdministratorCreator"),
        DirectoryUser(name="gone", groups=["All Users", "Managers"], site_role="Unlicensed"),
    ]
)


def rules(*items: tuple[str, str, dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {"grantee": {"kind": kind, "name": name}, "capabilities": caps}
        for kind, name, caps in items
    ]


def permissions(show_tabs: bool = True, name: str = "superstore_demo") -> Permissions:
    views = [
        {"id": "v1", "name": "銷售總覽", "rules": rules(("user", "alice", {"Read": "Allow"}))},
        {"id": "v2", "name": "Monthly Profit", "rules": rules(("user", "bob", {"Read": "Allow"}))},
    ]
    return Permissions.model_validate(
        {
            "projects": [{"id": "p1", "name": "Sales"}],
            "workbooks": [
                {
                    "id": "w1",
                    "name": name,
                    "project": "p1",
                    "show_tabs": show_tabs,
                    "rules": rules(
                        ("user", "alice", {"Read": "Allow"}),
                        ("user", "bob", {"Read": "Allow"}),
                        ("group", "Managers", {"Read": "Allow", "ExportXml": "Allow"}),
                    ),
                    "views": [] if show_tabs else views,
                }
            ],
        }
    )


def client(fixtures: Path, granted: Permissions) -> TestClient:
    settings = AuthSettings(mode="dev", directory=DIRECTORY, permissions=granted)
    return TestClient(create_app(fixtures, ROOT / "no-web-dist", auth=settings))


def login(c: TestClient, name: str) -> TestClient:
    assert c.post("/auth/dev", json={"username": name}).status_code == 200
    return c


def test_workbook_permissions(fixtures: Path) -> None:
    c = client(fixtures, permissions())
    carol = login(c, "carol")
    assert carol.get("/api/workbooks").json() == []
    for path in ("/ir", "/cases", "/files/x.png"):
        assert carol.get(f"{WB}{path}").status_code == 404
    response = carol.post(f"{WB}/worksheets/ws_sales_by_region/render")
    assert response.status_code == 404 and "找不到報表" in response.json()["detail"]
    assert carol.post(f"{WB}/fields/f_category/domain").status_code == 404

    alice = login(c, "alice")
    [summary] = alice.get("/api/workbooks").json()
    assert summary["id"] == "secure" and len(summary["worksheets"]) == 4
    rows = alice.post(f"{WB}/worksheets/ws_sales_by_region/render").json()["table"]["rows"]
    assert {r[0] for r in rows} == {"East"}  # 權限與列層級權限同時套用


def test_definition_needs_download_permission(fixtures: Path) -> None:
    c = client(fixtures, permissions())
    ir = login(c, "alice").get(f"{WB}/ir").json()
    fields = {f["id"]: f for ds in ir["datasources"] for f in ds["fields"]}
    assert fields["f_visible"]["calc"] is None  # 沒有下載權限：看不到公式（含權限的規則）
    assert ir["datasources"][0]["connection"] == {"kind": "extract"}
    assert ir["datasources"][0]["filters"] == []
    assert len(ir["dashboards"]) == 1 and len(ir["worksheets"]) == 4

    ir = login(c, "dave").get(f"{WB}/ir").json()
    fields = {f["id"]: f for ds in ir["datasources"] for f in ds["fields"]}
    assert "USERNAME()" in fields["f_visible"]["calc"]["formula"]


def test_admins_and_unlicensed(fixtures: Path) -> None:
    c = client(fixtures, permissions(name="other workbook"))
    # 權限檔中沒有這份報表：只有管理員看得到
    assert login(c, "alice").get("/api/workbooks").json() == []
    assert [w["id"] for w in login(c, "root").get("/api/workbooks").json()] == ["secure"]
    c = client(fixtures, permissions())
    assert login(c, "gone").get("/api/workbooks").json() == []


def test_view_permissions(fixtures: Path) -> None:
    c = client(fixtures, permissions(show_tabs=False))
    bob = login(c, "bob")
    [summary] = bob.get("/api/workbooks").json()
    assert summary["dashboards"] == []
    assert summary["worksheets"] == [{"id": "ws_monthly_profit", "name": "Monthly Profit"}]
    assert bob.post(f"{WB}/worksheets/ws_monthly_profit/render").status_code == 200
    response = bob.post(f"{WB}/worksheets/ws_sales_by_region/render")
    assert response.status_code == 404
    ir = bob.get(f"{WB}/ir").json()
    assert ir["dashboards"] == [] and [w["id"] for w in ir["worksheets"]] == ["ws_monthly_profit"]

    # dashboard 的權限涵蓋其中的工作表
    alice = login(c, "alice")
    [summary] = alice.get("/api/workbooks").json()
    assert [d["name"] for d in summary["dashboards"]] == ["銷售總覽"]
    assert len(summary["worksheets"]) == 4
    assert alice.post(f"{WB}/worksheets/ws_sales_by_region/render").status_code == 200


def test_permissions_config(tmp_path: Path) -> None:
    (tmp_path / "directory.json").write_text(DIRECTORY.model_dump_json(), encoding="utf-8")
    (tmp_path / "permissions.json").write_text(
        json.dumps(permissions().model_dump(mode="json")), encoding="utf-8"
    )
    config = tmp_path / "auth.toml"
    config.write_text(
        '[auth]\nmode = "dev"\ndirectory = "directory.json"\npermissions = "permissions.json"\n',
        encoding="utf-8",
    )
    settings = load_auth(config)
    assert settings.permissions is not None and settings.permissions.workbooks[0].id == "w1"
    config.write_text('[auth]\nmode = "none"\npermissions = "permissions.json"\n', encoding="utf-8")
    with pytest.raises(AuthError, match="權限需要登入者的身分"):
        load_auth(config)
    config.write_text(
        '[auth]\nmode = "dev"\ndirectory = "directory.json"\npermissions = "x.json"\n',
        encoding="utf-8",
    )
    with pytest.raises(AuthError, match="找不到權限檔"):
        load_auth(config)
