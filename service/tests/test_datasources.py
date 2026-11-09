"""資料源分頁的 API：連線、資料表、欄位與資料源篩選要「下載 workbook」的權限；資料預覽另外要
「檢視基礎資料」，並與工作表相同套用列層級權限。

以 conftest 的 secure（示範報表加上資料源篩選 [Visible]：負責人是登入者或屬於 Managers 群組；
East → alice、West → bob、其他 → carol）為例。
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

from tmig.access.directory import Directory, DirectoryUser
from tmig.access.permissions import Permissions
from tmig_service import create_app
from tmig_service.auth import AuthSettings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = Directory(
    users=[
        DirectoryUser(name="alice", groups=["All Users"], site_role="Viewer"),
        DirectoryUser(name="bob", groups=["All Users"], site_role="Explorer"),
        DirectoryUser(name="carol", groups=["All Users"], site_role="Viewer"),
        DirectoryUser(name="dave", groups=["All Users", "Managers"], site_role="Explorer"),
    ]
)


def rules(*items: tuple[str, str, dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {"grantee": {"kind": kind, "name": name}, "capabilities": caps}
        for kind, name, caps in items
    ]


def login(c: TestClient, name: str) -> TestClient:
    assert c.post("/auth/dev", json={"username": name}).status_code == 200
    return c


WB = "/api/workbooks/secure"
PREVIEW = f"{WB}/datasources/ds_orders/preview"


def permissions() -> Permissions:
    """alice 只能檢視；bob 另可下載 workbook；Managers（dave）另可檢視基礎資料。"""
    return Permissions.model_validate(
        {
            "projects": [{"id": "p1", "name": "Sales"}],
            "workbooks": [
                {
                    "id": "w1",
                    "name": "superstore_demo",
                    "project": "p1",
                    "rules": rules(
                        ("user", "alice", {"Read": "Allow"}),
                        ("user", "bob", {"Read": "Allow", "ExportXml": "Allow"}),
                        (
                            "group",
                            "Managers",
                            {"Read": "Allow", "ExportXml": "Allow", "ViewUnderlyingData": "Allow"},
                        ),
                    ),
                }
            ],
        }
    )


def client(fixtures: Path, granted: Permissions | None = None) -> TestClient:
    settings = AuthSettings(mode="dev", directory=DIRECTORY, permissions=granted)
    return TestClient(create_app(fixtures, ROOT / "no-web-dist", auth=settings))


def regions(response: dict[str, Any]) -> set[str]:
    index = next(i for i, c in enumerate(response["columns"]) if c["caption"] == "Region")
    return {row[index] for row in response["rows"]}


def test_datasource_page_needs_download_and_underlying_data(fixtures: Path) -> None:
    c = client(fixtures, permissions())
    alice = login(c, "alice")
    assert alice.get(f"{WB}/datasources").status_code == 403
    assert alice.get(PREVIEW).status_code == 403

    bob = login(client(fixtures, permissions()), "bob")
    summary = bob.get(f"{WB}/datasources")
    assert summary.status_code == 200
    [ds] = summary.json()
    assert ds["id"] == "ds_orders" and ds["connection"]["kind"] == "extract"
    assert any(f["text"].startswith("Visible") for f in ds["filters"])
    # 沒有「檢視基礎資料」：看得到定義，看不到原始列
    response = bob.get(PREVIEW)
    assert response.status_code == 403 and "檢視基礎資料" in response.json()["detail"]

    dave = login(client(fixtures, permissions()), "dave")
    response = dave.get(PREVIEW, params={"limit": 50})
    assert response.status_code == 200
    body = response.json()
    assert (body["limit"], len(body["rows"])) == (50, 50)
    assert body["total"] > 50
    # Managers 看得到全部的區
    assert len(regions(dave.get(PREVIEW, params={"limit": 1000}).json())) == 4


def test_preview_applies_row_level_security(fixtures: Path) -> None:
    """沒有權限檔時登入的人都可以開；列層級權限照樣套用：alice 只看得到 East。"""
    alice = login(client(fixtures), "alice")
    body = alice.get(PREVIEW, params={"limit": 1000}).json()
    assert regions(body) == {"East"}
    carol = login(client(fixtures), "carol")
    assert regions(carol.get(PREVIEW, params={"limit": 1000}).json()) == {"Central", "South"}
    # 列數上限
    assert alice.get(PREVIEW, params={"limit": 5000}).json()["limit"] == 1000
    assert alice.get(f"{WB}/datasources/nope/preview").status_code == 404
