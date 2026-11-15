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
        # 顯示集合：單獨檢視工作表時有集合控制項，勾選 West 後查詢帶著新的成員
        menu("Region Set", "顯示集合")
        page.wait_for_selector('.editor[data-saving="false"]')
        for caption in ("Region", "Region Set"):
            pane.locator(f'li[data-field="{ids[caption]}"]').dblclick()
            page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_timeout(300)
        sheet = call(engine, f"/api/edit/{workbook}/worksheets/ws_1")["worksheet"]
        assert [(c["kind"], c["field"]["field"]) for c in sheet["controls"]] == [
            ("set", ids["Region Set"])
        ]
        page.goto(f"{engine}/w/{workbook}/s/ws_1")
        control = page.locator('.single-sheet .sheet-controls fieldset[data-control="set"]')
        control.locator("label").nth(2).wait_for()
        boxes = control.locator("input[type=checkbox]")
        # East（別名「東部」）、South、West：集合的成員是 East
        assert [boxes.nth(i).is_checked() for i in range(3)] == [True, False, False]
        with page.expect_response(
            lambda r: r.url.endswith("/worksheets/ws_1/render") and r.request.method == "POST"
        ) as rendered:
            control.locator("label", has_text="West").locator("input").check()
        posted = rendered.value.request.post_data_json
        assert posted is not None
        assert posted["sets"][ids["Region Set"]] == {
            "mode": "include",
            "members": [["East"], ["West"]],
        }
        assert rendered.value.ok
        page.goto(f"{engine}/w/{workbook}/edit/ws_1")
        page.wait_for_selector('.editor[data-saving="false"]')
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


CODES = (
    "Region,Category,Code,Sales,Quantity\n"
    "East,Furniture,A-10,100.5,2\n"
    "West,Office,B-20,200,1\n"
    "East,Office,C-30,50.25,3\n"
    "South,Tech,D-40,75,1\n"
)


def test_ad_hoc_calculation_and_more_field_commands(engine: str, tmp_path: Path) -> None:
    """架上輸入臨時計算；欄位選單的預設屬性（排序、總計使用）、分割、取代參照、描述與變更資料類型。"""
    from playwright.sync_api import sync_playwright

    path = tmp_path / "codes.csv"
    path.write_text(CODES, encoding="utf-8")
    workbook = call(engine, "/api/desktop/new", {"path": str(path)})["document"]["id"]

    def fields() -> dict[str, dict[str, Any]]:
        body = call(engine, f"/api/edit/{workbook}/catalog")
        return {f["caption"]: f for f in body["datasources"][0]["fields"]}

    def sheet() -> dict[str, Any]:
        return call(engine, f"/api/edit/{workbook}/worksheets/ws_1")["worksheet"]

    def table() -> dict[Any, Any]:
        body = call(engine, f"/api/workbooks/{workbook}/worksheets/ws_1/render", {})
        return {row[0]: row[1] for row in body["table"]["rows"]}

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

        def settle() -> None:
            page.wait_for_timeout(200)
            page.wait_for_selector('.editor[data-saving="false"]')

        def pick(label: str) -> Any:
            exact = re.compile("^" + re.escape(label) + "$")
            return page.locator(".popup-menu .menu-label").filter(has_text=exact).first

        def menu(caption: str, *path: str) -> None:
            pane.locator(f'li[data-field="{ids[caption]}"]').click(button="right")
            page.locator(".popup-menu").wait_for()
            for label in path[:-1]:
                pick(label).hover()
            pick(path[-1]).click()

        def type_on(shelf: str, formula: str) -> None:
            drop = page.locator(f'.shelf[data-shelf="{shelf}"] .shelf-drop')
            box = drop.bounding_box()
            assert box is not None
            drop.dblclick(position={"x": box["width"] - 8, "y": box["height"] / 2})
            entry = page.get_by_label("在架上輸入公式")
            entry.fill(formula)
            entry.press("Enter")
            settle()

        def confirm() -> None:
            dialog = page.get_by_role("dialog")
            dialog.get_by_role("button", name="確定").click()
            dialog.wait_for(state="detached")
            settle()

        # 在列架輸入 [Region]：只是一個欄位，直接用它；在欄架輸入公式：建立臨時計算
        type_on("rows", "[Region]")
        type_on("columns", "SUM([Sales]) / SUM([Quantity])")
        current = sheet()
        assert current["rows"][0]["field"] == ids["Region"]
        adhoc = fields()["SUM([Sales]) / SUM([Quantity])"]
        assert adhoc["ad_hoc"] is True and current["columns"][0]["field"] == adhoc["id"]
        assert table() == {"East": "30.15", "South": "75", "West": "200"}
        # 臨時計算只在架上：資料窗格不列出，膠囊上是公式
        assert pane.locator("li.field", has_text="SUM([Sales])").count() == 0
        pill = page.locator('.shelf[data-shelf="columns"] .pill')
        assert pill.get_attribute("data-pill") == "SUM([Sales]) / SUM([Quantity])"
        # 在架上編輯：改成 SUM([Sales])，回到一般的欄位
        pill.click()
        pick("在架上編輯…").click()
        entry = page.get_by_label("在架上輸入公式")
        assert entry.input_value() == "SUM([Sales]) / SUM([Quantity])"
        entry.fill("SUM([Sales])")
        entry.press("Enter")
        settle()
        column = sheet()["columns"][0]
        assert (column["field"], column["agg"]) == (ids["Sales"], "sum")
        # 公式有誤時顯示原因，架上不變
        type_on("columns", "SUM([Nope])")
        page.locator(".toast.error").wait_for()
        assert len(sheet()["columns"]) == 1

        # 預設屬性＞排序：字母順序遞減，用到這個欄位的工作表跟著排序
        menu("Region", "預設屬性", "排序…")
        page.get_by_role("dialog").get_by_label("字母順序：遞減").check()
        confirm()
        assert fields()["Region"]["default_sort"]["order"] == "desc"
        assert [(s["field"], s["order"]) for s in sheet()["sorts"]] == [(ids["Region"], "desc")]
        assert list(table()) == ["West", "South", "East"]
        # 預設屬性＞總計使用
        menu("Sales", "預設屬性", "總計使用", "平均")
        page.wait_for_function("!document.querySelector('.popup-menu')")
        settle()
        assert fields()["Sales"]["total_using"] == "avg"
        # 預設屬性＞色彩：East 指定顏色，之後放到色彩上的工作表沿用
        menu("Region", "預設屬性", "色彩…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("East的預設顏色").fill("#ff0000")
        confirm()
        assert fields()["Region"]["default_color"]["assignments"] == [
            {"value": "East", "color": "#ff0000"}
        ]

        # 轉換＞分割：依資料中最常見的分隔符號自動分割
        menu("Code", "轉換", "分割…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="自動分割").click()
        dialog.wait_for(state="detached")
        pane.locator("li.field", has_text="Code - Split 2").wait_for()
        made = fields()
        assert made["Code - Split 1"]["formula"] == 'TRIM( SPLIT( [Code], "-", 1 ) )'
        assert made["Code - Split 2"]["formula"].endswith('"-", 2 ) )')
        ids.update({caption: f["id"] for caption, f in made.items()})

        # 取代參照：工作表上的 Region 換成 Category（預設排序是 Region 的，不跟著過去）
        menu("Region", "取代參照…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("取代成的欄位").select_option(ids["Category"])
        confirm()
        assert sheet()["rows"][0]["field"] == ids["Category"]
        page.locator('.shelf[data-shelf="rows"] .pill[data-pill="Category"]').wait_for()

        # 描述：型別與值域
        menu("Sales", "描述…")
        dialog = page.get_by_role("dialog")
        assert "數字（小數）" in dialog.inner_text() and "連續的量值" in dialog.inner_text()
        dialog.get_by_role("button", name="載入").click()
        dialog.get_by_text("50.25 到 200").wait_for()
        dialog.locator("button.primary").click()
        dialog.wait_for(state="detached")

        # 變更資料類型：沒有用到的檔案欄位改成字串
        menu("Quantity", "變更資料類型", "字串")
        page.wait_for_function("!document.querySelector('.popup-menu')")
        deadline = time.monotonic() + 10  # 欄位的變更不經過工作表的儲存，等引擎套用
        while fields()["Quantity"]["datatype"] != "string" and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert fields()["Quantity"]["datatype"] == "string"
        settle()
        assert errors == []
        browser.close()
