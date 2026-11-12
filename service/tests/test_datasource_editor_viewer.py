"""桌面版的資料源編輯端到端：新增報表（連線到資料）→ 選 Excel 的工作表 → 資料源分頁加入資料表（關聯、
join）→ 改欄位名稱 → 前往工作表，以連按兩下欄位建立檢視。

沒有桌面版外殼（Tauri）時，選檔對話框改成輸入路徑（window.prompt），這裡以 Playwright 回答。
需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from openpyxl import Workbook

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
TOKEN = "datasource-e2e-token-0123456789"


def chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not (DIST / "index.html").is_file() or not chromium(),
        reason="需要建置好的檢視器（web/dist）與 Playwright 的 Chromium",
    ),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


def write_book(path: Path) -> Path:
    book = Workbook()
    orders = book.active
    assert orders is not None
    orders.title = "Orders"
    orders.append(["Order ID", "Region", "Order Date", "Sales", "Quantity"])
    for i, (region, sales) in enumerate(
        [("East", 100.5), ("West", 200), ("East", 50.25), ("South", 75)], 1
    ):
        orders.append([f"O{i}", region, dt.datetime(2024, i, 5), sales, i])
    people = book.create_sheet("People")
    people.append(["Region", "Manager"])
    for row in (["East", "Ann"], ["West", "Bob"], ["North", "Cid"]):
        people.append(row)
    returns = book.create_sheet("Returns")
    returns.append(["Order ID", "Returned"])
    returns.append(["O1", "Yes"])
    returns.append(["O4", "Yes"])
    book.save(path)
    return path


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[str]:
    port = free_port()
    app = create_desktop_app(tmp_path / "workspace", TOKEN, DIST, username="amy")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("本機引擎沒有在 30 秒內啟動")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def call(base: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def route(url: str) -> list[str]:
    """網址 /w/<報表>/<種類>/<id> 的各段（解碼後）。"""
    return [urllib.parse.unquote(p) for p in urllib.parse.urlparse(url).path.split("/") if p]


def test_new_workbook_relationship_and_worksheet(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import Dialog, sync_playwright

    book = write_book(tmp_path / "Sales.xlsx")
    asked: list[str] = []

    def answer(dialog: Dialog) -> None:
        asked.append(dialog.message)
        if dialog.type == "prompt":
            dialog.accept(str(book))
        else:
            dialog.accept()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", answer)
        page.goto(f"{engine}/?token={TOKEN}")
        page.get_by_role("button", name="新增報表（連線到資料）…").click()
        # 檔案中有三張工作表：選擇要用的一張（預設第一張 Orders）
        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        assert "Orders" in dialog.inner_text() and "People" in dialog.inner_text()
        dialog.get_by_role("button", name="確定").click()
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        page.wait_for_selector(".dse-preview .ds-data tbody tr")
        assert any("資料檔" in message for message in asked)
        parts = route(page.url)
        assert parts[0] == "w" and parts[2] == "edit-ds"
        workbook, datasource = parts[1], parts[3]
        assert page.locator(".dse-preview .ds-data tbody tr").count() == 4
        # 加入 People：預設為關聯，條件自動配對名稱相同的 Region
        page.locator('.dse-add[data-table="People"]').click()
        add = page.get_by_role("dialog")
        add.wait_for()
        assert add.get_by_label("左邊的欄位").input_value() != ""
        assert add.get_by_label("右邊的欄位").input_value() == "Region"
        add.get_by_role("button", name="加入", exact=True).click()
        page.wait_for_selector(".dse-relationship")
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        assert page.locator(".dse-logical").count() == 2
        # 欄位改名：Sales → 銷售額
        caption = page.get_by_label("Sales 的名稱", exact=True)
        caption.fill("銷售額")
        caption.press("Enter")
        page.wait_for_selector('tr[data-field] input[aria-label="銷售額 的名稱"]')
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        model = call(
            engine, f"/api/edit/{urllib.parse.quote(workbook)}/datasources/{datasource}/model"
        )
        fields = {f["caption"]: f for f in model["fields"]}
        assert "銷售額" in fields and "Sales" not in fields
        assert "Region (People)" in fields
        # 前往工作表：資料窗格有新的欄位；連按兩下 Manager（列）與銷售額（欄）
        page.get_by_role("button", name="前往工作表").click()
        page.wait_for_selector('.editor[data-saving="false"]', timeout=30000)
        pane = page.locator(".data-pane")
        assert "銷售額" in pane.inner_text() and "Manager" in pane.inner_text()
        pane.locator(f'li[data-field="{fields["Manager"]["id"]}"]').dblclick()
        page.wait_for_selector('.editor[data-saving="false"]')
        pane.locator(f'li[data-field="{fields["銷售額"]["id"]}"]').dblclick()
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        sheet_id = route(page.url)[3]
        sheet = call(engine, f"/api/edit/{urllib.parse.quote(workbook)}/worksheets/{sheet_id}")[
            "worksheet"
        ]
        assert [u["field"] for u in sheet["rows"]] == [fields["Manager"]["id"]]
        assert [u["field"] for u in sheet["columns"]] == [fields["銷售額"]["id"]]
        body = call(
            engine,
            f"/api/workbooks/{urllib.parse.quote(workbook)}/worksheets/{sheet_id}/render",
            {},
        )
        # 與 Tableau 的關聯相同：沒有對應的訂單（South）在 Null 之下，沒有訂單的經理（Cid）也列出
        assert body["table"]["rows"] == [
            ["Ann", "150.75"],
            ["Bob", "200"],
            ["Cid", "Null"],
            ["Null", "75"],
        ]
        assert errors == []
        browser.close()


def test_join_in_logical_table(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    book = write_book(tmp_path / "Sales.xlsx")
    created = call(engine, "/api/desktop/new", {"path": str(book), "table": "Orders"})
    workbook, datasource = created["document"]["id"], created["datasource"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/{urllib.parse.quote(workbook)}/edit-ds/{datasource}")
        page.wait_for_selector(".dse-preview .ds-data tbody tr")
        # Join Returns（預設 inner join：只有退貨的兩張訂單）
        page.locator('.dse-add[data-table="Returns"]').click()
        add = page.get_by_role("dialog")
        add.wait_for()
        add.get_by_label("Join（同一張邏輯資料表）").check()
        assert add.get_by_label("右邊的欄位").input_value() == "Order ID"
        add.get_by_role("button", name="加入", exact=True).click()
        joined = page.locator('.dse-join-right[data-join="Returns"]')
        joined.wait_for()
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        page.wait_for_function(
            "document.querySelectorAll('.dse-preview .ds-data tbody tr').length === 2"
        )
        # 改成 left join：全部四張訂單
        page.get_by_label("Returns 的 join 類型").select_option("left")
        joined.get_by_role("button", name="套用").click()
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        page.wait_for_function(
            "document.querySelectorAll('.dse-preview .ds-data tbody tr').length === 4"
        )
        model = call(
            engine, f"/api/edit/{urllib.parse.quote(workbook)}/datasources/{datasource}/model"
        )
        relation = model["logical"][0]["relation"]
        assert (relation["kind"], relation["join"]) == ("join", "left")
        # 移除 Returns：回到單一的資料表
        joined.get_by_role("button", name="移除").click()
        page.wait_for_selector('.dse-join-right[data-join="Returns"]', state="detached")
        model = call(
            engine, f"/api/edit/{urllib.parse.quote(workbook)}/datasources/{datasource}/model"
        )
        assert model["logical"][0]["relation"]["kind"] == "table"
        assert "Returned" not in {f["caption"] for f in model["fields"]}
        assert errors == []
        browser.close()
