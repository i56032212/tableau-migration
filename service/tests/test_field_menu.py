"""桌面版編輯器的欄位選單 API：複製、別名、群組、數值分組、集合、預設屬性與刪除，以及資料窗格
（catalog）列出的定義。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

TOKEN = "field-menu-token-0123456789"
ORDERS = (
    "Region,Category,Sales,Quantity\n"
    "East,Furniture,100.5,2\n"
    "West,Office,200,1\n"
    "East,Office,50.25,3\n"
    "South,Tech,75,1\n"
)


@pytest.fixture
def opened(tmp_path: Path) -> tuple[TestClient, str, str]:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, None, username="amy")
    client = TestClient(app, headers={TOKEN_HEADER: TOKEN}, base_url="http://127.0.0.1")
    path = tmp_path / "orders.csv"
    path.write_text(ORDERS, encoding="utf-8")
    info = ok(client.post("/api/desktop/new", json={"path": str(path)}))
    return client, info["document"]["id"], info["datasource"]


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def catalog_fields(client: TestClient, workbook: str) -> dict[str, dict[str, Any]]:
    body = ok(client.get(f"/api/edit/{workbook}/catalog"))
    return {f["caption"]: f for f in body["datasources"][0]["fields"]}


def render(client: TestClient, workbook: str, rows: str, measure: str) -> dict[Any, Any]:
    sheet = ok(client.get(f"/api/edit/{workbook}/worksheets/ws_1"))["worksheet"]
    sheet.update(rows=[{"field": rows}], columns=[{"field": measure, "agg": "sum"}])
    ok(client.put(f"/api/edit/{workbook}/worksheets/ws_1", json={"worksheet": sheet}))
    body = ok(client.post(f"/api/workbooks/{workbook}/worksheets/ws_1/render", json={}))
    return {row[0]: row[1] for row in body["table"]["rows"]}


def clear(client: TestClient, workbook: str) -> None:
    sheet = ok(client.get(f"/api/edit/{workbook}/worksheets/ws_1"))["worksheet"]
    sheet.update(rows=[], columns=[])
    ok(client.put(f"/api/edit/{workbook}/worksheets/ws_1", json={"worksheet": sheet}))


def test_group_bins_and_aliases(opened: tuple[TestClient, str, str]) -> None:
    client, workbook, ds = opened
    base = f"/api/edit/{workbook}/datasources/{ds}"
    fields = catalog_fields(client, workbook)
    region, sales = fields["Region"]["id"], fields["Sales"]["id"]
    group = ok(
        client.post(
            f"{base}/groups",
            json={"source": region, "groups": [{"name": "Coasts", "members": ["East", "West"]}]},
        )
    )["field"]
    assert group["caption"] == "Region (group)"
    assert render(client, workbook, group["id"], sales) == {"Coasts": "350.75", "South": "75"}
    listed = catalog_fields(client, workbook)["Region (group)"]
    assert listed["group"]["source"] == region and listed["kind"] == "calc"
    # 別名：檢視上顯示別名（篩選與計算仍用原始值）
    ok(
        client.put(
            f"{base}/fields/{group['id']}/aliases",
            json={"aliases": [{"value": "Coasts", "label": "沿海"}]},
        )
    )
    assert render(client, workbook, group["id"], sales) == {"沿海": "350.75", "South": "75"}
    # 數值分組：100 一格
    bins = ok(client.post(f"{base}/bins", json={"source": sales, "size": 100}))["field"]
    quantity = fields["Quantity"]["id"]
    assert render(client, workbook, bins["id"], quantity) == {"0": "4", "100": "2", "200": "1"}
    assert client.post(f"{base}/bins", json={"source": sales}).status_code == 422
    # 用到的欄位不能刪
    blocked = client.delete(f"{base}/fields/{bins['id']}")
    assert blocked.status_code == 422 and "用到" in blocked.json()["detail"]
    clear(client, workbook)
    ok(client.delete(f"{base}/fields/{bins['id']}"))
    assert "Sales (bin)" not in catalog_fields(client, workbook)


def test_sets_duplicate_and_properties(opened: tuple[TestClient, str, str]) -> None:
    client, workbook, ds = opened
    base = f"/api/edit/{workbook}/datasources/{ds}"
    fields = catalog_fields(client, workbook)
    region, sales = fields["Region"]["id"], fields["Sales"]["id"]
    fixed = ok(
        client.post(
            f"{base}/sets",
            json={"levels": [region], "rule": {"kind": "members", "members": [["East"]]}},
        )
    )["field"]
    assert render(client, workbook, fixed["id"], sales) == {"In": "150.75", "Out": "275"}
    top = ok(
        client.post(
            f"{base}/sets",
            json={
                "levels": [region],
                "caption": "Top 2",
                "rule": {"kind": "top_n", "direction": "top", "count": 2, "by": "SUM([Sales])"},
            },
        )
    )["field"]
    assert render(client, workbook, top["id"], sales) == {"In": "350.75", "Out": "75"}
    listed = catalog_fields(client, workbook)["Top 2"]
    assert listed["kind"] == "set" and listed["set"]["rule"]["kind"] == "top_n"
    bad = client.post(f"{base}/sets", json={"levels": [region], "rule": {"kind": "nope"}})
    assert bad.status_code == 422
    # 複製、預設屬性
    copy = ok(client.post(f"{base}/fields/{sales}/duplicate"))["field"]
    assert copy["caption"] == "Sales (copy)" and copy["calc"]["formula"] == "[Sales]"
    ok(
        client.put(
            f"{base}/fields/{sales}/properties", json={"comment": "含稅", "format": "n#,##0"}
        )
    )
    listed = catalog_fields(client, workbook)["Sales"]
    assert (listed["comment"], listed["format"]) == ("含稅", "n#,##0")
    ok(client.put(f"{base}/fields/{sales}/properties", json={"comment": None}))
    listed = catalog_fields(client, workbook)["Sales"]
    assert (listed["comment"], listed["format"]) == (None, "n#,##0")
    # 資料表中的欄位只能隱藏
    raw = client.delete(f"{base}/fields/{region}")
    assert raw.status_code == 422 and "只能隱藏" in raw.json()["detail"]
