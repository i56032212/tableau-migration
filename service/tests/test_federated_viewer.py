"""桌面版的跨資料庫的 join（端到端，PostgreSQL 與 Excel）：

- Excel 的資料源在資料源分頁「加入連線：資料庫…」登入 PostgreSQL，把資料庫的資料表以關聯加進來
- 資料庫的資料源（連線到伺服器建立的）加上 Excel 檔，改成跨資料庫的 join；移除後改回 Live 連線

需要建置好的檢視器（web/dist）、Playwright 的 Chromium 與 PostgreSQL 的執行檔（或 TMIG_TEST_POSTGRES）；
缺少時略過。"""

from __future__ import annotations

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
TOKEN = "federated-e2e-token-0123456789"
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


def targets(path: Path) -> Path:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Targets"
    for row in (("Region", "Target"), ("East", 1000), ("West", 2000), ("North", 500)):
        sheet.append(row)
    book.save(path)
    return path


def test_file_datasource_adds_database_table(dsn: str, engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    params = dict(part.split("=", 1) for part in dsn.split())
    created = call(
        engine,
        "/api/desktop/new",
        {"path": str(targets(tmp_path / "Targets.xlsx")), "table": "Targets"},
    )
    workbook, datasource = created["document"]["id"], created["datasource"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/{urllib.parse.quote(workbook)}/edit-ds/{datasource}")
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        # 加入連線：資料庫（新的連線，登入 PostgreSQL）
        page.get_by_role("group", name="加入連線").get_by_role("button", name="資料庫…").click()
        dialog = page.get_by_role("dialog", name="加入連線：資料庫")
        dialog.wait_for()
        dialog.get_by_label("資料庫的類型").select_option("postgres")
        dialog.get_by_label("伺服器").fill(params["host"])
        dialog.get_by_label("連接埠").fill(params["port"])
        dialog.get_by_label("資料庫", exact=True).fill(params["dbname"])
        dialog.get_by_label("結構描述").fill("shop")
        dialog.get_by_label("使用者名稱").fill(params["user"])
        dialog.get_by_label("密碼", exact=True).fill("pw")
        dialog.get_by_role("button", name="登入").click()
        dialog.wait_for(state="detached")
        # 左邊多了資料庫的連線與其中的資料表；加入 orders（以地區關聯）
        pane = page.locator(".dse-server")
        pane.locator('.dse-add[data-table="shop.orders"]').click()
        add = page.get_by_role("dialog", name="加入資料表：shop.orders")
        add.wait_for()
        fields = {x["caption"]: x["id"] for x in model(engine, workbook, datasource)["fields"]}
        add.get_by_label("左邊的欄位").select_option(fields["Region"])
        add.get_by_label("右邊的欄位").select_option("region")
        add.get_by_role("button", name="加入", exact=True).click()
        page.wait_for_selector(".dse-relationship")
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        shown = model(engine, workbook, datasource)
        assert shown["datasource"]["kind"] == "file" and shown["datasource"]["federated"]
        (connection,) = shown["connections"]
        assert connection["dbclass"] == "postgres" and connection["schema"] == "shop"
        assert page.locator(".ds-badge").inner_text() == "跨資料庫"
        # 資料庫的資料表標示所在的連線
        page.locator('.dse-table[data-table="orders"] .dse-source').wait_for()
        f = {x["caption"]: x["id"] for x in shown["fields"]}
        assert render(engine, workbook, datasource, f["Region"], f["Target"]) == {
            "East": "1,000",
            "North": "500",
            "West": "2,000",
        }
        # 與 Excel 的 Region 同名（不分大小寫）：Tableau 在名稱後加上資料表名稱
        assert "region (orders)" in f
        found = render(engine, workbook, datasource, f["region (orders)"], f["sales"])
        assert found == {"East": "150.75", "South": "75", "West": "200"}
        assert errors == []
        browser.close()


def test_database_datasource_adds_file(dsn: str, engine: str, tmp_path: Path) -> None:
    params = dict(part.split("=", 1) for part in dsn.split())
    registered = call(
        engine,
        "/api/desktop/connections",
        {
            "dbclass": "postgres",
            "server": params["host"],
            "port": int(params["port"]),
            "database": params["dbname"],
            "schema": "shop",
            "user": params["user"],
            "password": "pw",
        },
    )
    key = registered["connection"]["id"]
    created = call(
        engine, "/api/desktop/new-live", {"connection": key, "table_path": ["shop", "orders"]}
    )
    workbook, datasource = created["document"]["id"], created["datasource"]
    base = f"/api/edit/{urllib.parse.quote(workbook)}/datasources/{datasource}"
    f = {x["caption"]: x["id"] for x in model(engine, workbook, datasource)["fields"]}
    shown = call(
        engine,
        f"{base}/tables",
        {
            "path": str(targets(tmp_path / "Targets.xlsx")),
            "table": "Targets",
            "how": "join",
            "join": "left",
            "on": [[f["region"], "=", "Region"]],
        },
    )
    assert shown["datasource"]["kind"] == "file" and shown["datasource"]["federated"]
    relation = shown["logical"][0]["relation"]
    assert relation["kind"] == "join" and relation["left"]["connection"] == key
    f = {x["caption"]: x["id"] for x in shown["fields"]}
    assert render(engine, workbook, datasource, f["region"], f["Target"]) == {
        "East": "2,000",
        "South": "Null",
        "West": "2,000",
    }
    # 資料庫中的資料表仍可列出、加入（同一個資料庫）
    tables = call(engine, f"{base}/remote-tables?connection={key}")["tables"]
    assert any(t["path"] == ["shop", "orders"] for t in tables)
    assert render(engine, workbook, datasource, f["region"], f["sales"]) == {
        "East": "150.75",
        "South": "75",
        "West": "200",
    }
    # 移除 Excel 的資料表（工作表已不用它的欄位）：改回 Live 連線
    shown = call(engine, f"{base}/tables/Targets", method="DELETE")
    assert shown["datasource"]["kind"] == "live" and not shown["datasource"]["federated"]
    assert render(engine, workbook, datasource, f["region"], f["sales"]) == {
        "East": "150.75",
        "South": "75",
        "West": "200",
    }
