"""桌面版編輯器的欄位選單（端到端）：資料窗格的欄位按右鍵 → 建立群組、數值分組、集合，別名、重新命名
與隱藏；建立的欄位放到檢視上查詢。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
TOKEN = "field-menu-e2e-token-0123456789"
ORDERS = (
    "Region,Category,Sales,Quantity\n"
    "East,Furniture,100.5,2\n"
    "West,Office,200,1\n"
    "East,Office,50.25,3\n"
    "South,Tech,75,1\n"
)


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


def test_field_menu(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    path = tmp_path / "orders.csv"
    path.write_text(ORDERS, encoding="utf-8")
    created = call(engine, "/api/desktop/new", {"path": str(path)})
    workbook = created["document"]["id"]

    def fields() -> dict[str, dict[str, Any]]:
        body = call(engine, f"/api/edit/{workbook}/catalog")
        return {f["caption"]: f for f in body["datasources"][0]["fields"]}

    ids = {caption: f["id"] for caption, f in fields().items()}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/{workbook}/edit/ws_1")
        page.wait_for_selector('.editor[data-saving="false"]')
        pane = page.locator(".data-pane")

        def menu(caption: str, *path: str) -> None:
            pane.locator(f'li[data-field="{ids[caption]}"]').click(button="right")
            popup = page.locator(".popup-menu")
            popup.wait_for()

            def item(label: str) -> Any:
                exact = re.compile("^" + re.escape(label) + "$")
                return popup.locator(".menu-label").filter(has_text=exact).first

            for label in path[:-1]:
                item(label).hover()
            item(path[-1]).click()

        # 建立群組：勾選 East、West → 群組所選的成員 → 改名為 Coasts
        menu("Region", "建立", "群組…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("East", exact=True).check()
        dialog.get_by_label("West", exact=True).check()
        dialog.get_by_role("button", name="群組所選的成員").click()
        dialog.get_by_label("群組名稱").fill("Coasts")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        group = pane.locator("li.field", has_text="Region (group)")
        group.wait_for()
        assert "📎" in group.inner_text()
        ids.update({c: f["id"] for c, f in fields().items()})
        # 放到檢視上：連按兩下群組（列）與 Sales（欄）
        group.dblclick()
        page.wait_for_selector('.editor[data-saving="false"]')
        pane.locator(f'li[data-field="{ids["Sales"]}"]').dblclick()
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        body = call(engine, f"/api/workbooks/{workbook}/worksheets/ws_1/render", {})
        assert {row[0]: row[1] for row in body["table"]["rows"]} == {
            "Coasts": "350.75",
            "South": "75",
        }
        # 數值分組：大小 100
        menu("Sales", "建立", "數值分組…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("分組的大小").fill("100")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        pane.locator("li.field", has_text="Sales (bin)").wait_for()
        assert fields()["Sales (bin)"]["bin"]["size"] == 100
        # 別名：East → 東部
        menu("Region", "別名…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("East 的別名").fill("東部")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        page.wait_for_function("!document.querySelector('[role=dialog]')")
        assert fields()["Region"]["aliases"] == [{"value": "East", "label": "東部"}]
        # 集合：East
        menu("Region", "建立", "集合…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("East", exact=True).check()
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        pane.locator("li.field", has_text="Region Set").wait_for()
        assert fields()["Region Set"]["set"]["rule"]["members"] == [["East"]]
        # 合併集合：再建立一個 West 的集合，從 Region Set 的選單與它合併（聯集）
        menu("Region", "建立", "集合…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("集合的名稱").fill("West Set")
        dialog.get_by_label("West", exact=True).check()
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        pane.locator("li.field", has_text="West Set").wait_for()
        ids.update({caption: f["id"] for caption, f in fields().items()})
        menu("Region Set", "建立合併集合…")
        dialog = page.get_by_role("dialog")
        assert dialog.get_by_label("左側的集合").input_value() == ids["Region Set"]
        assert dialog.get_by_label("右側的集合").input_value() == ids["West Set"]
        dialog.get_by_label("集合的名稱").fill("East or West")
        dialog.get_by_label("兩個集合中的所有成員").check()
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        pane.locator("li.field", has_text="East or West").wait_for()
        assert fields()["East or West"]["set"]["rule"] == {
            "kind": "combined",
            "left": ids["Region Set"],
            "right": ids["West Set"],
            "op": "union",
        }
        # 重新命名、隱藏
        menu("Quantity", "重新命名…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("欄位名稱").fill("數量")
        dialog.get_by_label("欄位名稱").press("Enter")
        dialog.wait_for(state="detached")
        pane.locator("li.field", has_text="數量").wait_for()
        menu("Category", "隱藏")
        pane.locator(f'li[data-field="{ids["Category"]}"]').wait_for(state="detached")
        assert fields()["Category"]["hidden"] is True
        assert errors == []
        browser.close()


def test_selection_commands(engine: str, tmp_path: Path) -> None:
    """檢視上選取標記：排除、以選取的成員建立群組；多個維度時「只保留」是依值組合的篩選。"""
    from playwright.sync_api import sync_playwright

    path = tmp_path / "orders.csv"
    path.write_text(ORDERS, encoding="utf-8")
    created = call(engine, "/api/desktop/new", {"path": str(path)})
    workbook = created["document"]["id"]
    catalog = call(engine, f"/api/edit/{workbook}/catalog")["datasources"][0]["fields"]
    ids = {f["caption"]: f["id"] for f in catalog}

    def sheet() -> dict[str, Any]:
        return call(engine, f"/api/edit/{workbook}/worksheets/ws_1")["worksheet"]

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/{workbook}/worksheets/ws_1",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).read()

    current = sheet()
    current.update(
        rows=[{"field": ids["Region"]}],
        columns=[{"field": ids["Sales"], "agg": "sum", "continuous": True}],
    )
    put(current)
    # 圖本身的 SVG（捲動時固定的標題是另外的複本，不接受點選）
    bar = ":is(.chart, .frozen-host) > svg path[aria-roledescription='bar']"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/{workbook}/edit/ws_1")
        page.wait_for_selector(f".editor {bar}", timeout=30000)
        page.locator(f".editor {bar}[aria-label*='East']").click()
        toolbar = page.get_by_role("toolbar", name="選取的標記")
        assert "已選取 1 個標記" in toolbar.inner_text()
        toolbar.get_by_role("button", name="✕ 排除").click()
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_function(f'document.querySelectorAll("{bar}").length === 2')
        (excluded,) = sheet()["filters"]
        assert (excluded["kind"], excluded["mode"], excluded["values"]) == (
            "categorical",
            "exclude",
            ["East"],
        )
        # 以選取的 West、South 建立群組（Ctrl 加選）
        page.locator(f".editor {bar}[aria-label*='West']").click()
        page.locator(f".editor {bar}[aria-label*='South']").click(modifiers=["Control"])
        assert "已選取 2 個標記" in toolbar.inner_text()
        toolbar.get_by_role("button", name="📎 群組…").click()
        dialog = page.get_by_role("dialog")
        assert dialog.get_by_label("群組名稱").input_value() == "West, South"
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        fields = call(engine, f"/api/edit/{workbook}/catalog")["datasources"][0]["fields"]
        group = next(f for f in fields if f["caption"] == "Region (group)")
        assert group["group"]["groups"] == [{"name": "West, South", "members": ["West", "South"]}]
        # 兩個維度：只保留是依值組合的篩選
        current = sheet()
        current.update(rows=[{"field": ids["Region"]}, {"field": ids["Category"]}], filters=[])
        put(current)
        page.reload()
        page.wait_for_selector(f".editor {bar}", timeout=30000)
        page.locator(f".editor {bar}[aria-label*='Office']").first.click()
        page.get_by_role("toolbar", name="選取的標記").get_by_role(
            "button", name="✓ 只保留"
        ).click()
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_function(f'document.querySelectorAll("{bar}").length === 1')
        (kept,) = sheet()["filters"]
        assert kept["kind"] == "tuple" and kept["mode"] == "include"
        assert [u["field"] for u in kept["fields"]] == [ids["Region"], ids["Category"]]
        assert len(kept["tuples"]) == 1
        assert "包含（Region、Category）" in page.locator('[data-shelf="filters"]').inner_text()
        assert errors == []
        browser.close()
