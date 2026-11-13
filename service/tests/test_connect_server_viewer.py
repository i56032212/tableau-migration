"""桌面版的「連線到伺服器」端到端（PostgreSQL）：首頁的「新增報表（連線到伺服器）…」→ 連線對話框登入 →
選資料表建立報表 → 資料源分頁從資料庫的資料表加入關聯（兩邊是關聯計算）→ 新增自訂 SQL、修改 SQL；
另以 Excel 的資料源在 join 的條件中建立 Join 計算。

需要建置好的檢視器（web/dist）、Playwright 的 Chromium 與 PostgreSQL 的執行檔（或 TMIG_TEST_POSTGRES）；
缺少時略過。"""

from __future__ import annotations

import datetime as dt
import json
import sys
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
from tmig_service.credentials import MemoryCredentials
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
TOKEN = "connect-server-e2e-token-0123456789"
sys.path.insert(0, str(ROOT / "kernel"))
from tests.engines import pg_connect, postgres_cluster  # noqa: E402  # pyright: ignore[reportMissingImports]


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


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    with postgres_cluster() as found:
        con = pg_connect(found)
        con.execute("DROP SCHEMA IF EXISTS shop CASCADE")
        con.execute("CREATE SCHEMA shop")
        con.execute("CREATE TABLE shop.orders (order_id TEXT, region TEXT, sales NUMERIC(10, 2))")
        con.execute(
            "INSERT INTO shop.orders VALUES ('O1', 'East', 100.5), ('O2', 'West', 200), "
            "('O3', 'East', 50.25), ('O4', 'South', 75)"
        )
        # 經理表的地區是小寫：要以關聯計算 LOWER([region]) 才對得上
        con.execute("CREATE TABLE shop.people (area TEXT, manager TEXT)")
        con.execute(
            "INSERT INTO shop.people VALUES ('east', 'Ann'), ('west', 'Bob'), ('north', 'Cid')"
        )
        con.close()
        yield found


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[str]:
    port = free_port()
    app = create_desktop_app(
        tmp_path / "workspace",
        TOKEN,
        DIST,
        username="amy",
        settings_dir=tmp_path / "settings",
        credentials=MemoryCredentials(),
    )
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


def call(
    base: str, path: str, body: dict[str, Any] | None = None, method: str | None = None
) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def route(url: str) -> list[str]:
    return [urllib.parse.unquote(p) for p in urllib.parse.urlparse(url).path.split("/") if p]


def render(base: str, workbook: str, datasource: str, rows: str, measure: str) -> dict[str, str]:
    wb = urllib.parse.quote(workbook)
    sheet = call(base, f"/api/edit/{wb}/worksheets/ws_1")["worksheet"]
    sheet.update(
        datasource=datasource, rows=[{"field": rows}], columns=[{"field": measure, "agg": "sum"}]
    )
    call(base, f"/api/edit/{wb}/worksheets/ws_1", {"worksheet": sheet}, "PUT")
    body = call(base, f"/api/workbooks/{wb}/worksheets/ws_1/render", {})
    return {row[0]: row[1] for row in body["table"]["rows"]}


def model(base: str, workbook: str, datasource: str) -> Any:
    return call(base, f"/api/edit/{urllib.parse.quote(workbook)}/datasources/{datasource}/model")


def test_connect_server_relationship_calc_and_custom_sql(dsn: str, engine: str) -> None:
    from playwright.sync_api import sync_playwright

    params = dict(part.split("=", 1) for part in dsn.split())
    secret = "pw-typed-in-dialog"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.get_by_role("button", name="新增報表（連線到伺服器）…").click()
        dialog = page.get_by_role("dialog", name="新增報表：連線到伺服器")
        dialog.wait_for()
        dialog.get_by_label("資料庫的類型").select_option("postgres")
        dialog.get_by_label("伺服器").fill(params["host"])
        dialog.get_by_label("連接埠").fill(params["port"])
        dialog.get_by_label("資料庫", exact=True).fill(params["dbname"])
        dialog.get_by_label("結構描述").fill("shop")
        dialog.get_by_label("使用者名稱").fill(params["user"])
        dialog.get_by_label("密碼", exact=True).fill(secret)
        dialog.get_by_role("button", name="登入").click()
        # 連線成功：列出 shop 中的資料表，選 orders
        tables = dialog.get_by_role("list", name="資料表")
        tables.wait_for()
        assert "orders" in tables.inner_text() and "people" in tables.inner_text()
        tables.get_by_text("orders").click()
        dialog.get_by_role("button", name="確定").click()
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        page.wait_for_function(
            "document.querySelectorAll('.dse-preview .ds-data tbody tr').length === 4",
            timeout=30000,
        )
        parts = route(page.url)
        assert parts[0] == "w" and parts[2] == "edit-ds"
        workbook, datasource = parts[1], parts[3]
        # 左邊的連線：資料庫中的資料表；加入 people，兩邊都建立關聯計算
        page.locator('.dse-server .dse-add[data-table="shop.people"]').click()
        add = page.get_by_role("dialog", name="加入資料表：shop.people")
        add.wait_for()
        add.get_by_label("左邊的欄位").select_option("__calc__")
        add.get_by_label("左邊的欄位的計算").fill("LOWER([region])")
        add.get_by_label("右邊的欄位").select_option("__calc__")
        add.get_by_label("右邊的欄位的計算").fill("TRIM([area])")
        add.get_by_role("button", name="加入", exact=True).click()
        page.wait_for_selector(".dse-relationship")
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        shown = model(engine, workbook, datasource)
        (relationship,) = shown["relationships"]
        assert relationship["on"][0]["left_formula"] == "LOWER([region])"
        assert relationship["on"][0]["right_formula"] == "TRIM([area])"
        # 關聯計算不在欄位清單中
        f = {x["caption"]: x["id"] for x in shown["fields"]}
        assert set(f) == {"order_id", "region", "sales", "area", "manager"}
        # 關聯的條件中顯示公式（可以修改）
        assert page.locator(
            '.dse-relationship input[aria-label="左邊的欄位的計算"]'
        ).input_value() == ("LOWER([region])")
        assert render(engine, workbook, datasource, f["manager"], f["sales"]) == {
            "Ann": "150.75",
            "Bob": "200",
            "Cid": "Null",
            "Null": "75",
        }
        # 新增自訂 SQL：每個地區的訂單數，以 region 關聯
        page.get_by_role("button", name="新增自訂 SQL…").click()
        custom = page.get_by_role("dialog", name="新增自訂 SQL")
        custom.wait_for()
        custom.get_by_label("自訂 SQL").fill(
            "SELECT region AS code, COUNT(*) AS n FROM shop.orders GROUP BY region"
        )
        custom.get_by_role("button", name="檢查欄位").click()
        custom.get_by_text("code（字串）").wait_for()
        custom.get_by_role("button", name="下一步").click()
        add = page.get_by_role("dialog", name="加入資料表：自訂 SQL")
        add.wait_for()
        add.get_by_label("左邊的欄位").select_option(f["region"])
        add.get_by_label("右邊的欄位").select_option("code")
        add.get_by_role("button", name="加入", exact=True).click()
        page.wait_for_selector('.dse-sql[data-sql="Custom SQL Query"]')
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        # 修改 SQL：多一欄 total
        page.locator('.dse-sql[data-sql="Custom SQL Query"]').get_by_role(
            "button", name="編輯 SQL…"
        ).click()
        edit = page.get_by_role("dialog", name="編輯自訂 SQL：Custom SQL Query")
        edit.wait_for()
        edit.get_by_label("自訂 SQL").fill(
            "SELECT region AS code, COUNT(*) AS n, SUM(sales) AS total FROM shop.orders GROUP BY region"
        )
        edit.get_by_role("button", name="套用").click()
        edit.wait_for(state="detached")
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        f = {x["caption"]: x["id"] for x in model(engine, workbook, datasource)["fields"]}
        assert "total" in f and "n" in f
        assert render(engine, workbook, datasource, f["code"], f["total"]) == {
            "East": "150.75",
            "South": "75",
            "West": "200",
        }
        # 密碼不在設定檔中
        assert errors == []
        browser.close()


def write_book(path: Path) -> Path:
    book = Workbook()
    orders = book.active
    assert orders is not None
    orders.title = "Orders"
    orders.append(["Order ID", "Region", "Order Date", "Sales"])
    for i, (region, sales) in enumerate([("East", 100.5), ("West", 200), ("East", 50.25)], 1):
        orders.append([f"O{i}", region, dt.datetime(2024, i, 5), sales])
    returns = book.create_sheet("Returns")
    returns.append(["Order", "Returned"])
    returns.append(["o1", "Yes"])
    returns.append(["o3", "Yes"])
    book.save(path)
    return path


def test_join_calculation(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    book = write_book(tmp_path / "Sales.xlsx")
    created = call(engine, "/api/desktop/new", {"path": str(book), "table": "Orders"})
    workbook, datasource = created["document"]["id"], created["datasource"]
    f = {x["caption"]: x["id"] for x in model(engine, workbook, datasource)["fields"]}
    # 退貨表的訂單編號是小寫：先以欄位 join（對不上）
    call(
        engine,
        f"/api/edit/{urllib.parse.quote(workbook)}/datasources/{datasource}/tables",
        {
            "file": "Data/Sales.xlsx",
            "table": "Returns",
            "how": "join",
            "join": "left",
            "on": [[f["Order ID"], "=", "Order"]],
        },
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/{urllib.parse.quote(workbook)}/edit-ds/{datasource}")
        joined = page.locator('.dse-join-right[data-join="Returns"]')
        joined.wait_for()
        # 右邊改成 Join 計算 UPPER([Order])
        joined.get_by_label("右邊的欄位").select_option("__calc__")
        formula = joined.get_by_label("右邊的欄位的計算")
        assert formula.input_value() == "[Order]"
        formula.fill("UPPER([Order])")
        joined.get_by_role("button", name="套用").click()
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        page.wait_for_function(
            "[...document.querySelectorAll('.dse-preview .ds-data tbody td')]"
            ".filter(td => td.textContent === 'Yes').length === 2",
            timeout=30000,
        )
        relation = model(engine, workbook, datasource)["logical"][0]["relation"]
        assert relation["on"][0]["right_formula"] == "UPPER([Order])"
        f = {x["caption"]: x["id"] for x in model(engine, workbook, datasource)["fields"]}
        assert render(engine, workbook, datasource, f["Returned"], f["Sales"]) == {
            "Yes": "150.75",
            "Null": "200",
        }
        # 公式有誤時說明原因、不套用
        joined.get_by_label("右邊的欄位的計算").fill("UPPER([Nope])")
        joined.get_by_role("button", name="套用").click()
        page.get_by_role("alert").filter(has_text="沒有欄位").wait_for()
        assert errors == []
        browser.close()
