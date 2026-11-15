"""桌面版的資料源編輯（「連線到資料」與資料源分頁）的 API：以 Excel 建立新的報表、加入資料表（關聯、
join、聯集）、樞紐、欄位的設定，與存成報表套件後再開啟。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

TOKEN = "datasource-editor-token-0123456789"


def write_book(path: Path) -> None:
    book = Workbook()
    orders = book.active
    assert orders is not None
    orders.title = "Orders"
    orders.append(["Order ID", "Region", "Order Date", "Sales", "Quantity"])
    orders.append(["O1", "East", dt.datetime(2024, 1, 5), 100.5, 2])
    orders.append(["O2", "West", dt.datetime(2024, 2, 1), 200, 1])
    orders.append(["O3", "East", dt.datetime(2024, 3, 9), 50.25, 3])
    people = book.create_sheet("People")
    people.append(["Region", "Manager"])
    people.append(["East", "Ann"])
    people.append(["West", "Bob"])
    people.append(["North", "Cid"])
    returns = book.create_sheet("Returns")
    returns.append(["Order ID", "Returned"])
    returns.append(["O1", "Yes"])
    later = book.create_sheet("Orders 2025")
    later.append(["Order ID", "Region", "Order Date", "Sales", "Quantity"])
    later.append(["P1", "North", dt.datetime(2025, 1, 1), 7, 1])
    book.save(path)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, None, username="amy")
    return TestClient(app, headers={TOKEN_HEADER: TOKEN}, base_url="http://127.0.0.1")


@pytest.fixture
def book(tmp_path: Path) -> Path:
    path = tmp_path / "Sales.xlsx"
    write_book(path)
    return path


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def ids(model: dict[str, Any]) -> dict[str, str]:
    return {f["caption"]: f["id"] for f in model["fields"]}


def render(
    client: TestClient, workbook: str, datasource: str, rows: str, cols: str, agg: str
) -> dict[Any, Any]:
    """以編輯器的 API 改工作表，查詢結果（第一欄 → 第二欄）。"""
    sheet = ok(client.get(f"/api/edit/{workbook}/worksheets/ws_1"))["worksheet"]
    sheet.update(
        datasource=datasource,
        rows=[{"field": rows}],
        columns=[{"field": cols, "agg": agg}],
    )
    ok(client.put(f"/api/edit/{workbook}/worksheets/ws_1", json={"worksheet": sheet}))
    body = ok(client.post(f"/api/workbooks/{workbook}/worksheets/ws_1/render", json={}))
    return {plain(row[0]): plain(row[1]) for row in body["table"]["rows"]}


def plain(value: Any) -> Any:
    """資料表中的值（顯示的文字）：數字轉回數字，空白為 None。"""
    if value in (None, "", "Null"):
        return None
    try:
        number = float(str(value).replace(",", ""))
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def test_new_workbook_and_model(client: TestClient, book: Path, tmp_path: Path) -> None:
    info = ok(client.post("/api/desktop/new", json={"path": str(book), "table": "Orders"}))
    workbook = info["document"]["id"]
    datasource = info["datasource"]
    assert info["worksheet"] == "ws_1"
    inspected = ok(client.post(f"/api/edit/{workbook}/inspect-file", json={"path": str(book)}))
    assert [t["name"] for t in inspected["tables"]] == [
        "Orders",
        "People",
        "Returns",
        "Orders 2025",
    ]
    model = ok(client.get(f"/api/edit/{workbook}/datasources/{datasource}/model"))
    assert model["editable"] is True
    assert model["files"][0]["path"] == "Data/Sales.xlsx" and model["files"][0]["available"]
    fields = ids(model)
    assert render(client, workbook, datasource, fields["Region"], fields["Sales"], "sum") == {
        "East": 150.75,
        "West": 200,
    }
    # 關聯：People 成為另一張邏輯資料表（同名的 Region 改為「Region (People)」）
    model = ok(
        client.post(
            f"/api/edit/{workbook}/datasources/{datasource}/tables",
            json={
                "file": "Data/Sales.xlsx",
                "table": "People",
                "how": "relationship",
                "on": [[fields["Region"], "=", "Region"]],
            },
        )
    )
    assert len(model["logical"]) == 2 and len(model["relationships"]) == 1
    fields = ids(model)
    assert "Region (People)" in fields
    by_manager = render(client, workbook, datasource, fields["Manager"], fields["Sales"], "sum")
    assert by_manager == {"Ann": 150.75, "Bob": 200, "Cid": None}
    # 關聯的效能選項（基數、參考完整性）：記下設定，查詢的結果不變
    link = model["relationships"][0]
    assert link["options"] == {
        "left_unique": False,
        "right_unique": False,
        "left_complete": False,
        "right_complete": False,
    }
    options = {**link["options"], "right_unique": True, "left_complete": True}
    model = ok(
        client.put(
            f"/api/edit/{workbook}/datasources/{datasource}/relationships/{link['right']}",
            json={"on": [[fields["Region"], "=", fields["Region (People)"]]], "options": options},
        )
    )
    assert model["relationships"][0]["options"] == options
    assert (
        render(client, workbook, datasource, fields["Manager"], fields["Sales"], "sum")
        == by_manager
    )
    # 邏輯資料表 Orders 中 join Returns（只有退貨的訂單）
    orders = model["logical"][0]["id"]
    model = ok(
        client.post(
            f"/api/edit/{workbook}/datasources/{datasource}/tables",
            json={
                "file": "Data/Sales.xlsx",
                "table": "Returns",
                "how": "join",
                "target": orders,
                "on": [[fields["Order ID"], "=", "Order ID"]],
            },
        )
    )
    assert model["logical"][0]["relation"]["kind"] == "join"
    fields = ids(model)
    assert render(client, workbook, datasource, fields["Manager"], fields["Sales"], "sum") == {
        "Ann": 100.5,
        "Bob": None,
        "Cid": None,
    }
    # 改成 left join：全部的訂單
    model = ok(
        client.put(
            f"/api/edit/{workbook}/datasources/{datasource}/joins/Returns",
            json={"join": "left", "on": [[fields["Order ID"], "=", "Order ID"]]},
        )
    )
    assert render(client, workbook, datasource, fields["Manager"], fields["Sales"], "sum") == {
        "Ann": 150.75,
        "Bob": 200,
        "Cid": None,
    }
    # 欄位：改名、隱藏；工作表用到的欄位不能移除
    model = ok(
        client.put(
            f"/api/edit/{workbook}/datasources/{datasource}/fields/{fields['Quantity']}",
            json={"caption": "數量", "hidden": True},
        )
    )
    renamed = next(f for f in model["fields"] if f["id"] == fields["Quantity"])
    assert (renamed["caption"], renamed["hidden"]) == ("數量", True)
    people = model["logical"][1]["id"]
    blocked = client.delete(f"/api/edit/{workbook}/datasources/{datasource}/tables/{people}")
    assert blocked.status_code == 422 and "用到" in blocked.json()["detail"]
    # 存成報表套件、關閉後再開啟：資料與來源檔案都在
    target = tmp_path / "Sales.tmigx"
    ok(client.post("/api/desktop/save", json={"workbook": workbook, "path": str(target)}))
    ok(client.post("/api/desktop/close", json={"workbook": workbook}))
    reopened = ok(client.post("/api/desktop/open", json={"path": str(target)}))["document"]["id"]
    model = ok(client.get(f"/api/edit/{reopened}/datasources/{datasource}/model"))
    assert model["files"][0]["available"] is True
    assert render(client, reopened, datasource, fields["Manager"], fields["Sales"], "sum") == {
        "Ann": 150.75,
        "Bob": 200,
        "Cid": None,
    }


def test_union_pivot_and_types(client: TestClient, book: Path) -> None:
    info = ok(client.post("/api/desktop/new", json={"path": str(book), "table": "Orders"}))
    workbook, datasource = info["document"]["id"], info["datasource"]
    model = ok(
        client.post(
            f"/api/edit/{workbook}/datasources/{datasource}/tables",
            json={"file": "Data/Sales.xlsx", "table": "Orders 2025", "how": "union"},
        )
    )
    assert model["logical"][0]["relation"]["kind"] == "union"
    fields = ids(model)
    assert render(client, workbook, datasource, fields["Sheet"], fields["Sales"], "sum") == {
        "Orders": 350.75,
        "Orders 2025": 7,
    }
    # 移除聯集的一張：回到單一的資料表（Sheet、Table Name 一併移除）
    sheet = ok(client.get(f"/api/edit/{workbook}/worksheets/ws_1"))["worksheet"]
    sheet.update(rows=[], columns=[])
    ok(client.put(f"/api/edit/{workbook}/worksheets/ws_1", json={"worksheet": sheet}))
    model = ok(client.delete(f"/api/edit/{workbook}/datasources/{datasource}/tables/Orders 2025"))
    assert model["logical"][0]["relation"]["kind"] == "table"
    assert "Sheet" not in ids(model)
    # 樞紐：Sales 與 Quantity 轉成列
    model = ok(
        client.post(
            f"/api/edit/{workbook}/datasources/{datasource}/pivot",
            json={"table": "Orders", "columns": ["Sales", "Quantity"]},
        )
    )
    fields = ids(model)
    pivoted = render(
        client,
        workbook,
        datasource,
        fields["Pivot Field Names"],
        fields["Pivot Field Values"],
        "sum",
    )
    assert pivoted == {"Quantity": 6, "Sales": 350.75}
    sheet = ok(client.get(f"/api/edit/{workbook}/worksheets/ws_1"))["worksheet"]
    sheet.update(rows=[], columns=[])
    ok(client.put(f"/api/edit/{workbook}/worksheets/ws_1", json={"worksheet": sheet}))
    model = ok(client.delete(f"/api/edit/{workbook}/datasources/{datasource}/pivot/Pivot"))
    fields = ids(model)
    assert "Sales" in fields and "Pivot Field Names" not in fields
    # 資料類型：Quantity 改成文字（重新匯入），成為維度
    model = ok(
        client.put(
            f"/api/edit/{workbook}/datasources/{datasource}/fields/{fields['Quantity']}",
            json={"datatype": "string"},
        )
    )
    quantity = next(f for f in model["fields"] if f["id"] == fields["Quantity"])
    assert (quantity["datatype"], quantity["role"]) == ("string", "dimension")
    assert render(client, workbook, datasource, fields["Quantity"], fields["Sales"], "sum") == {
        1: 200,
        2: 100.5,
        3: 50.25,
    }


def test_add_datasource_and_errors(client: TestClient, book: Path, tmp_path: Path) -> None:
    info = ok(client.post("/api/desktop/new", json={"path": str(book), "table": "Orders"}))
    workbook = info["document"]["id"]
    csv = tmp_path / "targets.csv"
    csv.write_text("Region;Target\nEast;300\nWest;150\n", encoding="utf-8")
    created = ok(client.post(f"/api/edit/{workbook}/datasources", json={"path": str(csv)}))
    model = ok(client.get(f"/api/edit/{workbook}/datasources/{created['datasource']}/model"))
    fields = ids(model)
    assert render(
        client, workbook, created["datasource"], fields["Region"], fields["Target"], "sum"
    ) == {
        "East": 300,
        "West": 150,
    }
    catalog = ok(client.get(f"/api/edit/{workbook}/catalog"))
    assert [d["caption"] for d in catalog["datasources"]] == ["Orders (Sales)", "targets.csv"]
    missing = client.post("/api/desktop/new", json={"path": str(tmp_path / "nothing.xlsx")})
    assert missing.status_code == 404
    unknown = tmp_path / "notes.docx"
    unknown.write_bytes(b"x")
    assert client.post("/api/desktop/new", json={"path": str(unknown)}).status_code == 422
    relative = client.post(f"/api/edit/{workbook}/inspect-file", json={"path": "Sales.xlsx"})
    assert relative.status_code == 422
