"""桌面版編輯器的端到端：拖放欄位、膠囊選單、快速 Table Calculation、復原、篩選、計算欄位、新增工作表、
雙軸與存檔。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import re
import shutil
import statistics
import threading
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from editor_ui import (
    current_tab,
    menu_item,
    menubar,
    pane_menu,
    sheet_tab,
    side_tab,
    tab_menu,
    tool,
)

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "editor-e2e-token-0123456789"


def chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file() or not chromium(),
    reason="需要建置好的檢視器（web/dist）與 Playwright 的 Chromium",
)


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
    base = f"http://127.0.0.1:{port}"
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    call(base, "/api/desktop/open", {"path": str(source)})
    try:
        yield base
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


MAIN_SVG = ".editor-canvas :is(.chart, .frozen-host) > svg"
"""編輯器中工作表的圖（表格的標題凍結時另有幾份只露出標題的複本，在 .frozen 之下）。"""


def call_bytes(base: str, path: str) -> bytes:
    """GET 的原始內容（圖片等不是 JSON 的回應）。"""
    request = urllib.request.Request(base + path, headers={TOKEN_HEADER: TOKEN})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def test_edit_worksheet_in_viewer(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def pills(page: Page, shelf: str) -> list[str]:
        return [
            t.replace("▾", "").strip()
            for t in page.locator(f'[data-shelf="{shelf}"] .pill .pill-text').all_inner_texts()
        ]

    def menu(page: Page, *labels: str) -> None:
        for i, label in enumerate(labels):
            item = page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text=label).first
            if i < len(labels) - 1:
                item.hover()
            else:
                item.click()

    def table(worksheet: str) -> dict[str, Any]:
        return call(engine, f"/api/workbooks/demo/worksheets/{worksheet}/render", {})["table"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.get_by_role("button", name="編輯工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        assert pills(page, "rows") == ["Region"]
        assert pills(page, "columns") == ["SUM(Sales)"]

        # 拖放：Segment 放到列
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="rows"]')
        )
        settle(page)
        assert pills(page, "rows") == ["Region", "Segment"]
        data = table("ws_sales_by_region")
        assert "f_segment" in data["keys"]

        # 膠囊選單：彙總改成平均，再加上快速 Table Calculation（累計）
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "量值", "平均")
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales)"]
        page.locator('[data-shelf="columns"] .pill', has_text="AVG(Sales)").click()
        menu(page, "快速表計算", "累計")
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales) △"]
        assert any(k.startswith("avg(f_sales)#tc_") for k in table("ws_sales_by_region")["keys"])

        # 復原兩次、重做一次
        tool(page, "復原").click()
        settle(page)
        tool(page, "復原").click()
        settle(page)
        assert pills(page, "columns") == ["SUM(Sales)"]
        tool(page, "重做").click()
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales)"]
        assert "avg(f_sales)" in table("ws_sales_by_region")["keys"]

        # 篩選：排除 Corporate
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="filters"]')
        )
        page.wait_for_selector(".filter-dialog .value-list li")
        page.locator(".value-list label", has_text="Corporate").locator("input").uncheck()
        page.locator(".filter-dialog").get_by_role("button", name="確定").click()
        # 對話框先取得篩選的 id 再套用：關閉後才開始存檔
        expect(page.locator(".filter-dialog")).to_have_count(0)
        expect(page.locator('[data-shelf="filters"] .pill', has_text="Segment")).to_be_visible()
        settle(page)
        data = table("ws_sales_by_region")
        segment = data["keys"].index("f_segment")
        assert {row[segment] for row in data["raw"]} == {"Consumer", "Home Office"}

        # 計算欄位：建立後放到色彩
        pane_menu(page, "建立計算欄位…")
        page.get_by_label("計算欄位名稱").fill("Profit Ratio")
        page.get_by_label("公式").fill("SUM([Profit]) / SUM([Sales])")
        expect(page.locator(".calc-status")).to_contain_text("計算有效")
        page.locator(".calc-dialog").get_by_role("button", name="確定").click()
        field = page.locator(".data-pane .field", has_text="Profit Ratio")
        expect(field).to_be_visible()
        field.drag_to(page.locator('[data-drop="marks.color"]'))
        settle(page)
        assert pills(page, "marks")[0] == "Profit Ratio"

        # 新增工作表：連按兩下加入欄位、改名
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_category"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)
        assert (pills(page, "rows"), pills(page, "columns")) == (["Category"], ["SUM(Sales)"])
        assert len(table("ws_1")["rows"]) == 3
        current_tab(page).dblclick()
        name = page.get_by_label("工作表名稱")
        name.fill("類別銷售")
        name.press("Enter")
        settle(page)
        expect(sheet_tab(page, "類別銷售")).to_have_attribute("aria-selected", "true")

        # 雙軸：Profit 與 Sales 重疊，Measure Names 放到色彩
        page.locator('.data-pane [data-field="f_profit"]').drag_to(
            page.locator('[data-drop="columns"]')
        )
        settle(page)
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Profit)").click()
        menu(page, "雙軸")
        settle(page)
        expect(page.locator(".mark-tabs button")).to_have_count(2)
        sheet = call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]
        assert sheet["panes"][0]["dual"] is True
        assert sheet["marks"]["color"]["field"] == "measure_names"

        # 工作表的格式：列的格線、背景
        menubar(page, "格式", "格式…")
        page.locator(".format-dialog fieldset", has_text="列").get_by_label(
            "格線", exact=True
        ).uncheck()
        page.locator(".format-dialog").get_by_role("button", name="確定").click()
        settle(page)
        sheet = call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]
        assert sheet["row_style"]["grid"] is False and sheet["column_style"]["grid"] is True

        # 存檔後變更寫進報表套件
        target = tmp_path / "edited.tmigx"
        assert call(engine, "/api/desktop/documents")[0]["modified"] is True
        call(engine, "/api/desktop/save", {"workbook": "demo", "path": str(target)})
        with zipfile.ZipFile(target) as archive:
            saved = json.loads(archive.read("report.ir.json"))
        names = [w["name"] for w in saved["worksheets"]]
        assert "類別銷售" in names
        # 完成編輯：回到工作表的檢視
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/s/ws_1")
        page.wait_for_selector(".single-sheet .worksheet svg")
        assert errors == []
        browser.close()


def test_sort_table_calc_wildcard_and_annotations(engine: str) -> None:
    """排序對話框（手動、依欄位）、表計算對話框與以日期層級計算的快速表計算、萬用字元篩選、套用到工作表與註解。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def menu(page: Page, *labels: str) -> None:
        for i, label in enumerate(labels):
            item = page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text=label).first
            if i < len(labels) - 1:
                item.hover()
            else:
                item.click()

    def sheet(worksheet: str = "ws_1") -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{worksheet}")["worksheet"]

    def table(worksheet: str = "ws_1") -> dict[str, Any]:
        return call(engine, f"/api/workbooks/demo/worksheets/{worksheet}/render", {})["table"]

    def values(key: str) -> list[Any]:
        data = table()
        index = data["keys"].index(key)
        return [row[index] for row in data["raw"]]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_region"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)

        # 編輯座標軸：對數刻度、固定的刻度間距
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "編輯座標軸…")
        page.get_by_label("座標軸的刻度").select_option("log")
        page.get_by_label("主要刻度").select_option("fixed")
        page.get_by_label("刻度間距").fill("200000")
        page.locator(".axis-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".axis-dialog")).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)
        (axis,) = sheet()["axes"]
        assert (axis["scale"], axis["ticks"], axis["tick_spacing"], axis["tick_origin"]) == (
            "log",
            True,
            200000.0,
            None,
        )
        assert errors == []
        # 改回線性、自動
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "編輯座標軸…")
        page.get_by_label("座標軸的刻度").select_option("linear")
        page.get_by_label("主要刻度").select_option("auto")
        page.locator(".axis-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".axis-dialog")).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)

        # 排序對話框：手動，把 West 移到最上面
        page.locator('[data-shelf="rows"] .pill', has_text="Region").click()
        menu(page, "排序", "排序…")
        page.get_by_label("排序依據").select_option("manual")
        members = page.locator(".manual-sort .value-list li")
        expect(members).to_have_count(4)
        members.filter(has_text="West").click()
        page.get_by_role("button", name="移到最上面").click()
        page.locator(".sort-dialog").get_by_role("button", name="確定").click()
        settle(page)
        assert values("f_region") == ["West", "Central", "East", "South"]
        (sort,) = sheet()["sorts"]
        assert (sort["kind"], sort["manual_values"][0]) == ("manual", "West")
        # 依欄位：利潤的平均遞減
        page.locator('[data-shelf="rows"] .pill', has_text="Region").click()
        menu(page, "排序", "排序…")
        page.get_by_label("排序依據").select_option("by_field")
        page.get_by_label("排序順序").select_option("desc")
        page.get_by_label("排序的欄位").select_option("f_profit")
        page.get_by_label("排序的彙總").select_option("avg")
        page.locator(".sort-dialog").get_by_role("button", name="確定").click()
        settle(page)
        (sort,) = sheet()["sorts"]
        assert (sort["kind"], sort["order"], sort["by"]["field"], sort["by"]["agg"]) == (
            "by_field",
            "desc",
            "f_profit",
            "avg",
        )

        # 萬用字元篩選：Region 包含 st（East、West）
        page.locator('[data-shelf="rows"] .pill', has_text="Region").click()
        menu(page, "篩選…")
        page.wait_for_selector(".filter-dialog .value-list li")
        page.locator(".filter-dialog").get_by_role("tab", name="萬用字元").click()
        page.get_by_label("比對的文字").fill("ST")
        page.locator(".filter-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".filter-dialog")).to_have_count(0)
        settle(page)
        assert sorted(values("f_region")) == ["East", "West"]
        (flt,) = sheet()["filters"]
        assert flt["condition"] == "CONTAINS(LOWER([Region]), LOWER('ST'))"
        # 重新開啟時回到萬用字元頁，改成排除
        page.locator('[data-shelf="filters"] .pill', has_text="Region").click()
        menu(page, "編輯篩選…")
        expect(page.get_by_label("比對的文字")).to_have_value("ST")
        page.locator(".filter-dialog").get_by_label("排除").check()
        page.locator(".filter-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".filter-dialog")).to_have_count(0)
        settle(page)
        assert sorted(values("f_region")) == ["Central", "South"]

        # 套用到工作表：Sub-Category Profit 也加上這個篩選
        page.locator('[data-shelf="filters"] .pill', has_text="Region").click()
        menu(page, "套用到工作表…")
        page.locator(".apply-filter-dialog label", has_text="Sub-Category Profit").locator(
            "input"
        ).check()
        page.locator(".apply-filter-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".apply-filter-dialog")).to_have_count(0)
        settle(page)
        group = sheet()["filters"][0]["group"]
        assert group
        copies = [f for f in sheet("ws_sub_category_profit")["filters"] if f.get("group") == group]
        assert [f["condition"] for f in copies] == [sheet()["filters"][0]["condition"]]
        # 移除篩選
        page.locator('[data-shelf="filters"] .pill', has_text="Region").click()
        menu(page, "移除")
        settle(page)

        # 註解：選取 East 的標記後加上標記的註解
        # （固定的標題與座標軸是圖的複本，點選的是 .frozen-host 之下原本的圖）
        page.locator(
            ".editor-canvas .frozen-host > svg path[aria-roledescription='bar'][aria-label*='East']"
        ).click()
        page.locator(".selection-bar").get_by_role("button", name="💬 註解…").click()
        page.get_by_role("textbox", name="註解的文字").click()
        page.keyboard.type("東區：")
        # 標記的註解可以插入這個標記的欄位值
        page.get_by_role("toolbar", name="註解的文字的格式").get_by_label("插入").select_option(
            label="SUM(Sales)"
        )
        page.locator(".annotations-dialog").get_by_role("button", name="加上註解").click()
        expect(page.locator(".annotations-dialog table.list")).to_contain_text("東區：<SUM(Sales)>")
        settle(page)
        page.locator(".annotations-dialog").get_by_role("button", name="關閉").click()
        (annotation,) = sheet()["annotations"]
        assert (annotation["kind"], annotation["point"]["mark"]) == ("mark", {"f_region": "East"})
        assert [r.get("text") for r in annotation["text"]] == ["東區：", None]
        assert annotation["text"][1]["use"]["field"] == "f_sales"
        expect(
            page.locator(".editor-canvas .frozen-host > svg text", has_text="東區")
        ).to_have_count(1)

        # 以日期層級計算的快速表計算：列改成年、季
        page.locator('[data-shelf="rows"] .pill', has_text="Region").click()
        menu(page, "移除")
        settle(page)
        for _ in range(2):
            page.locator('.data-pane [data-field="f_order_date"]').drag_to(
                page.locator('[data-drop="rows"]')
            )
            settle(page)
        page.locator('[data-shelf="rows"] .pill').nth(1).click()
        menu(page, "季")
        settle(page)
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "快速表計算", "年初至今總計")
        settle(page)
        (calc,) = sheet()["table_calcs"]
        assert (calc["type"], calc["preset"], calc["restart"]["date"]["unit"]) == (
            "running_total",
            "specific",
            "year",
        )
        assert [u["date"]["unit"] for u in calc["instances"]] == ["year", "quarter"]
        # 表計算對話框：改成差異、相對於第一個、不重新開始
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "編輯表計算…")
        page.get_by_label("計算類型").select_option("difference")
        page.get_by_label("相對於").select_option("first")
        page.get_by_label("重新開始間隔").select_option("")
        page.locator(".table-calc-dialog").get_by_role("button", name="確定").click()
        settle(page)
        (calc,) = sheet()["table_calcs"]
        assert (calc["type"], calc["options"], calc["restart"]) == (
            "difference",
            {"relative_to": "first"},
            None,
        )
        key = next(k for k in table()["keys"] if "#" in k)
        assert float(values(key)[0]) == 0.0
        # 年成長率：計算層級是年
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "快速表計算", "年成長率")
        settle(page)
        (calc,) = sheet()["table_calcs"]
        assert (calc["type"], calc["level"]["date"]["unit"]) == ("percent_difference", "year")
        # 年初至今成長率：每年重新累計，次要計算在年的層級與前一年比較
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "快速表計算", "年初至今成長率")
        settle(page)
        (calc,) = sheet()["table_calcs"]
        assert (calc["type"], calc["restart"]["date"]["unit"]) == ("running_total", "year")
        assert calc["secondary"]["type"] == "percent_difference"
        assert calc["secondary"]["level"]["date"]["unit"] == "year"
        # 表計算的自訂排序順序：沿著計算方向的標記依 Profit 的總和由大到小排列
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "編輯表計算…")
        dialog = page.locator(".table-calc-dialog")
        dialog.get_by_label("表計算的排序順序").select_option("f_profit")
        dialog.get_by_label("表計算排序的方向").select_option("desc")
        dialog.get_by_role("button", name="確定").click()
        settle(page)
        (calc,) = sheet()["table_calcs"]
        assert calc["sort"]["order"] == "desc"
        assert (calc["sort"]["by"]["field"], calc["sort"]["by"]["agg"]) == ("f_profit", "sum")
        # 次要計算自己的計算層級留著
        assert calc["secondary"]["level"]["date"]["unit"] == "year"
        assert (
            call(engine, f"/api/workbooks/demo/worksheets/{sheet()['id']}/render", {})[
                "unsupported"
            ]
            is None
        )
        assert errors == []
        browser.close()


def test_rich_text_title_tooltip_and_labels(engine: str) -> None:
    """格式文字的編輯器：標題（粗體、插入欄位）、自訂工具提示（檢視器滑過標記時顯示）、標籤的選項，
    以及 dashboard 文字物件的格式與參數的值。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]

    def confirm(page: Page, dialog: str) -> None:
        """按下對話框的「確定」，等對話框關閉、變更存好。"""
        page.locator(dialog).get_by_role("button", name="確定").click()
        expect(page.locator(dialog)).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_region"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)
        sales = {
            "field": "f_sales",
            "agg": "sum",
            "date": None,
            "continuous": True,
            "table_calc": None,
        }
        region = {
            "field": "f_region",
            "agg": None,
            "date": None,
            "continuous": False,
            "table_calc": None,
        }

        # 標題：粗體的文字，後面插入 SUM(Sales) 的值
        menubar(page, "工作表", "標題…")
        title = page.get_by_role("textbox", name="標題", exact=True)
        title.click()
        page.keyboard.type("各區業績：")
        page.keyboard.press("Control+A")
        page.get_by_role("toolbar", name="標題的格式").get_by_role("button", name="粗體").click()
        page.keyboard.press("End")
        page.get_by_role("toolbar", name="標題的格式").get_by_label("插入").select_option(
            label="SUM(Sales)"
        )
        confirm(page, ".modal")
        runs = sheet()["title"]
        assert runs[0]["text"] == "各區業績：" and runs[0]["style"]["bold"] is True, runs
        assert runs[1]["use"] == sales
        expect(page.locator(".editor .worksheet-title")).to_contain_text("各區業績：")
        expect(page.locator(".editor .worksheet-title")).to_contain_text("2,")  # 全部資料的合計

        # 工具提示：兩行文字，各插入一個欄位；業績那一行改成紅色
        page.locator(".mark-buttons").get_by_role("button", name="工具提示…").click()
        tip = page.get_by_role("textbox", name="工具提示的文字")
        bar_tools = page.get_by_role("toolbar", name="工具提示的文字的格式")
        tip.click()
        page.keyboard.type("區域：")
        bar_tools.get_by_label("插入").select_option(label="Region")
        page.keyboard.press("Enter")
        page.keyboard.type("業績：")
        bar_tools.get_by_label("插入").select_option(label="SUM(Sales)")
        confirm(page, ".tooltip-dialog")
        text = sheet()["marks"]["tooltip_text"]
        assert [r.get("text") for r in text] == ["區域：", None, "\n業績：", None]
        assert (text[1]["use"], text[3]["use"]) == (region, sales)
        assert sheet()["tooltips"] is True

        # 標籤：顯示、只標示最大值
        page.locator(".mark-buttons").get_by_role("button", name="標籤…").click()
        page.get_by_label("顯示標記標籤").select_option("on")
        page.get_by_label("要標示的標記").select_option("min_max")
        page.get_by_label("標示最小值").uncheck()
        page.get_by_label("標籤的垂直對齊").select_option("top")
        page.get_by_label("標籤的水平對齊").select_option("center")
        confirm(page, ".label-dialog")
        assert (sheet()["marks"]["label_valign"], sheet()["marks"]["cell_align"]) == (
            "top",
            "center",
        )
        labels = sheet()["marks"]["labels"]
        assert (labels["show"], labels["marks"], labels["minimum"], labels["maximum"]) == (
            True,
            "min_max",
            False,
            True,
        )
        # 只有最大的 West 有標籤
        shown = page.locator(".editor-canvas .frozen-host > svg [aria-roledescription='text mark']")
        expect(shown).to_have_count(1)

        # 檢視器：滑過 East 的長條，工具提示是自訂的文字
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/s/ws_1")
        page.wait_for_selector(".single-sheet .worksheet svg")
        page.locator(
            ".single-sheet .frozen-host > svg path[aria-roledescription='bar'][aria-label*='East']"
        ).hover()
        rich = page.locator(".viz-tooltip .viz-tooltip-rich")
        expect(rich).to_contain_text("區域：East")
        expect(rich).to_contain_text("業績：678,781")

        # dashboard 的文字物件：粗體、16pt，後面插入參數的值
        page.goto(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector(".dash-editor .dashboard [data-zone] svg", timeout=30000)
        side_tab(page, "dashboard")
        page.get_by_role("button", name="浮動", exact=True).click()
        page.locator(".object-buttons").get_by_role("button", name="文字").click()
        page.wait_for_selector('[data-saving="false"]')
        side_tab(page, "版面配置")
        page.locator(".dash-props").get_by_role("button", name="編輯文字…").click()
        body = page.get_by_role("textbox", name="文字物件的內容")
        tools = page.get_by_role("toolbar", name="文字物件的內容的格式")
        body.click()
        page.keyboard.press("Control+A")
        page.keyboard.type("前幾名：")
        page.keyboard.press("Control+A")
        tools.get_by_role("button", name="粗體").click()
        tools.get_by_label("字級").select_option("16")
        page.keyboard.press("End")
        tools.get_by_label("插入").select_option(label="參數.前 N 名")
        tools.get_by_label("對齊").select_option("center")
        page.locator(".modal").get_by_role("button", name="確定").click()
        page.wait_for_selector('[data-saving="false"]')
        page.wait_for_timeout(300)
        board = call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]
        zone = next(z for z in board["zones"] if z["kind"] == "text")
        first, second = zone["content"]
        assert (first["text"], first["style"]) == (
            "前幾名：",
            {
                "bold": True,
                "italic": None,
                "underline": None,
                "size": 16.0,
                "color": None,
                "font": None,
                "align": "center",
            },
        )
        assert second["parameter"] == "p_parameter_1" and second["style"]["align"] == "center"
        assert zone["text"] == "前幾名：<參數.前 N 名>"
        shown_text = page.locator(f".dash-editor .dashboard [data-zone='{zone['id']}'] .text")
        expect(shown_text).to_have_text("前幾名：3")
        expect(shown_text.locator("span").first).to_have_css("font-weight", "700")
        assert errors == []
        browser.close()


def test_edit_blend_relationships(engine: str, tmp_path: Path) -> None:
    """編輯混合關係：加入 Excel 的目標資料源後，把它的量值放到檢視上（以同名的 Region 自動連結），再把連結改成
    主要資料源的 Region 對應次要資料源的 Area。"""
    from openpyxl import Workbook
    from playwright.sync_api import Page, expect, sync_playwright

    book = Workbook()
    targets = book.active
    assert targets is not None
    targets.title = "Targets"
    targets.append(["Region", "Area", "Target"])
    # Region 與 Area 相反：連結欄位不同時目標對到不同的地區
    for row in (["East", "West", 100], ["West", "East", 200], ["South", "South", 300]):
        targets.append(row)
    path = tmp_path / "Targets.xlsx"
    book.save(path)
    secondary = call(engine, "/api/edit/demo/datasources", {"path": str(path), "table": "Targets"})[
        "datasource"
    ]
    catalog = call(engine, "/api/edit/demo/catalog")
    fields = {
        f["caption"]: f["id"]
        for d in catalog["datasources"]
        if d["id"] == secondary
        for f in d["fields"]
    }

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]

    def targets_by_region() -> dict[str, Any]:
        table = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})["table"]
        region = table["keys"].index("f_region")
        target = next(i for i, k in enumerate(table["keys"]) if fields["Target"] in k)
        return {row[region]: row[target] for row in table["raw"]}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_region"]').dblclick()
        settle(page)
        # 資料窗格切到 Targets，把 Target 放到欄：混合（以同名的 Region 連結）
        page.get_by_label("資料源", exact=True).select_option(secondary)
        page.locator(f'.data-pane [data-field="{fields["Target"]}"]').dblclick()
        settle(page)
        (blend,) = sheet()["blends"]
        assert [
            (link["primary"]["field"], link["secondary"]["field"]) for link in blend["links"]
        ] == [("f_region", fields["Region"])]
        found = targets_by_region()
        assert (float(found["East"]), float(found["West"])) == (100.0, 200.0)

        # 編輯混合關係：Region 改成對應 Area
        page.locator(".data-pane").get_by_role("button", name="混合關係…").click()
        page.get_by_label("連結 1 的次要資料源欄位").select_option(fields["Area"])
        page.locator(".blends-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".blends-dialog")).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)
        (blend,) = sheet()["blends"]
        assert [
            (link["primary"]["field"], link["secondary"]["field"]) for link in blend["links"]
        ] == [("f_region", fields["Area"])]
        found = targets_by_region()
        assert (float(found["East"]), float(found["West"])) == (200.0, 100.0)
        assert errors == []
        browser.close()


def test_edit_dashboard_and_story(engine: str) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('[data-saving="false"]')
        page.wait_for_timeout(300)

    def board() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]

    def zone_ids(zones: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for zone in zones:
            found.append(zone["id"])
            found.extend(zone_ids(zone["children"]))
        return found

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        page.get_by_role("button", name="編輯 dashboard").click()
        page.wait_for_url(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector(".dash-editor .dashboard [data-zone] svg", timeout=30000)

        # 篩選控制項的「只顯示相關的值」；垂直容器的「平均分配內容」
        def find(zones: list[dict[str, Any]], zone_id: str) -> dict[str, Any]:
            for zone in zones:
                if zone["id"] == zone_id:
                    return zone
                try:
                    return find(zone["children"], zone_id)
                except KeyError:
                    continue
            raise KeyError(zone_id)

        side_tab(page, "版面配置")
        page.locator('[data-tree-zone="z12"]').click()
        relevant = page.locator(".dash-props").get_by_label("只顯示相關的值")
        relevant.click()
        expect(relevant).to_be_checked()
        settle(page)
        assert find(board()["zones"], "z12")["relevant"] is True
        side_tab(page, "版面配置")
        page.locator('[data-tree-zone="z11"]').click()
        evenly = page.locator(".dash-props").get_by_label("平均分配內容")
        evenly.click()
        expect(evenly).to_be_checked()
        settle(page)
        side = find(board()["zones"], "z11")
        heights = [child["h"] for child in side["children"]]
        assert side["distribute"] is True
        assert max(heights) - min(heights) <= 1 and heights[0] > 96, heights

        # 工作表區塊的「顯示說明」：自動產生的說明顯示在工作表的下方
        side_tab(page, "版面配置")
        page.locator('[data-tree-zone="z9"]').click()
        shown = page.locator(".dash-props").get_by_label("顯示說明")
        shown.click()
        expect(shown).to_be_checked()
        settle(page)
        assert find(board()["zones"], "z9")["show_caption"] is True
        expect(
            page.locator('.dash-editor .dashboard [data-zone="z9"] .worksheet-caption')
        ).to_be_visible()
        expect(page.locator(".dash-editor .dashboard .worksheet-caption")).to_have_count(1)

        # 格線：顯示對齊用的格線（編輯器的設定，不存進報表），可以改間距
        assert page.locator(".dash-overlay.with-grid").count() == 0
        page.get_by_label("顯示格線").check()
        expect(page.locator(".dash-overlay.with-grid")).to_have_count(1)
        page.get_by_label("格線大小").fill("20")
        expect(page.locator('.dash-overlay[data-grid="20"]')).to_have_count(1)
        assert "20px" in (page.locator(".dash-overlay").get_attribute("style") or "")
        page.get_by_label("顯示格線").uncheck()
        expect(page.locator(".dash-overlay.with-grid")).to_have_count(0)

        # 選取工作表區塊後移除（版面配置與畫面都選得到）
        side_tab(page, "版面配置")
        page.locator('[data-tree-zone="z10"]').click()
        expect(page.locator('[data-zone-props="z10"]')).to_be_visible()
        page.locator(".dash-props").get_by_role("button", name="移除").click()
        settle(page)
        assert "z10" not in zone_ids(board()["zones"])

        # 以浮動方式加入文字，拖曳移動
        side_tab(page, "dashboard")
        page.get_by_role("button", name="浮動", exact=True).click()
        page.locator(".object-buttons").get_by_role("button", name="文字").click()
        settle(page)
        floating = board()["zones"][1]
        assert floating["kind"] == "text"
        side_tab(page, "版面配置")
        page.get_by_label("文字", exact=True).fill("說明")
        page.locator(".dash-props .prop-title").click()
        settle(page)
        selected = page.locator(".dash-zone.selected")
        box = selected.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + 20, box["y"] + 20)
        page.mouse.down()
        page.mouse.move(box["x"] + 220, box["y"] + 120, steps=6)
        page.mouse.up()
        settle(page)
        moved = board()["zones"][1]
        assert moved["text"] == "說明" and moved["x"] > floating["x"] and moved["y"] > floating["y"]

        # 工作表拖到區塊上：並排加入（分割那個區塊）
        side_tab(page, "dashboard")
        page.get_by_role("button", name="並排", exact=True).click()
        page.locator('.dash-side [data-sheet="ws_sub_category_profit"]').drag_to(
            page.locator('[data-overlay="z9"]')
        )
        settle(page)
        sheets = [z for z in zone_ids(board()["zones"])]
        assert len(sheets) == len(set(sheets))
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(4)

        # 復原（加入的工作表）再重做
        tool(page, "復原").click()
        settle(page)
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(3)
        tool(page, "重做").click()
        settle(page)
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(4)

        # 動作：點選 Sales by Region 的標記篩選 Region Share by Year
        menubar(page, "dashboard", "動作…")
        page.get_by_role("button", name="新增動作").click()
        page.get_by_label("動作名稱").fill("依區域篩選")
        page.locator('[aria-label="來源工作表"] label', has_text="Sales by Region").locator(
            "input"
        ).check()
        page.locator('[aria-label="目標工作表"] label', has_text="Region Share by Year").locator(
            "input"
        ).check()
        page.locator(".action-form").get_by_role("button", name="確定").click()
        expect(page.locator(".modal table.list")).to_contain_text("依區域篩選")
        page.locator(".modal .dialog-actions").get_by_role("button", name="關閉").click()

        # dashboard 的大小
        side_tab(page, "dashboard")
        width = page.get_by_label("dashboard 的寬度")
        page.locator(".dash-canvas").click(position={"x": 5, "y": 5})
        width.fill("1300")
        width.press("Tab")
        settle(page)
        assert board()["size"]["width"] == 1300

        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/d/db_ee909b0c")
        page.wait_for_selector(".dashboard [data-zone] svg")
        actions = call(engine, "/api/edit/demo/outline")["actions"]
        assert [a["caption"] for a in actions] == ["依區域篩選"]

        # Story：新增後編輯故事點
        story = call(engine, "/api/edit/demo/stories", {})["story"]
        page.goto(f"{engine}/w/demo/edit-story/{story['id']}")
        page.wait_for_selector(
            ".story-editor .story-preview .dashboard [data-zone] svg", timeout=30000
        )
        caption = page.get_by_label("故事點 1 的說明")
        caption.fill("全公司概況")
        caption.press("Tab")
        settle(page)
        # 在預覽中改了參數（滑桿）與篩選，「更新故事點」把目前的狀態記下來
        slider = page.locator(
            '.story-preview fieldset[data-control="single_slider"] input[type=range]'
        )
        before = slider.input_value()
        slider.focus()
        page.keyboard.press("ArrowRight")
        expect(slider).not_to_have_value(before)
        after = slider.input_value()
        segment = page.locator(".story-preview fieldset.control", has_text="Segment")
        segment.locator("button").first.click()
        segment.locator("label", has_text="Corporate").locator("input").uncheck()
        page.get_by_role("button", name="更新故事點").click()
        settle(page)
        saved = call(engine, f"/api/edit/demo/stories/{story['id']}")["story"]
        (parameter,) = saved["points"][0]["parameters"].items()
        assert float(parameter[1]) == float(after) != float(before)
        (captured,) = saved["points"][0]["filters"]
        assert (captured["worksheet"], captured["field"]["field"], captured["mode"]) == (
            "ws_region_share_by_year",
            "f_segment",
            "include",
        )
        assert captured["values"] and "Corporate" not in captured["values"]
        # 存檔後預覽回到記下的狀態（滑桿仍是改過的值）；另存為新的故事點
        expect(slider).to_have_value(after)
        page.get_by_role("button", name="另存為新的故事點").click()
        settle(page)
        saved = call(engine, f"/api/edit/demo/stories/{story['id']}")["story"]
        assert [p["parameters"] for p in saved["points"]] == [dict([parameter])] * 2
        assert saved["points"][1]["filters"] == [captured]
        page.locator('.story-editor [data-point="sp1"]').first.click()
        page.get_by_label("新增故事點").select_option("s:ws_monthly_profit")
        settle(page)
        saved = call(engine, f"/api/edit/demo/stories/{story['id']}")["story"]
        assert [p["caption"] for p in saved["points"]] == ["全公司概況", "全公司概況 (2)", ""]
        assert saved["points"][2]["worksheet"] == "ws_monthly_profit"

        # 故事點上的文字物件：新增、輸入文字、在預覽上拖曳移動
        page.locator('.story-editor [data-point="sp1"]').first.click()
        page.get_by_role("button", name="＋ 文字物件").click()
        note_dialog = page.locator(".story-note-dialog")
        editor_box = note_dialog.get_by_role("textbox", name="文字物件的文字")
        editor_box.click()
        page.keyboard.press("Control+A")
        page.keyboard.type("第一季的重點")
        note_dialog.get_by_label("文字物件的左").fill("420")
        note_dialog.get_by_label("文字物件的上").fill("140")
        note_dialog.get_by_role("button", name="確定").click()
        expect(note_dialog).to_have_count(0)
        settle(page)
        saved = call(engine, f"/api/edit/demo/stories/{story['id']}")["story"]
        (note,) = saved["points"][0]["notes"]
        assert "".join(run["text"] for run in note["text"]) == "第一季的重點"
        assert (note["x"], note["y"]) == (420, 140)
        shown_note = page.locator(".story-preview .story-note")
        expect(shown_note).to_have_text("第一季的重點")
        shown_note.scroll_into_view_if_needed()  # 前面操作控制項時預覽可能已經捲動
        box = shown_note.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + 10, box["y"] + 10)
        page.mouse.down()
        page.mouse.move(box["x"] + 70, box["y"] + 50, steps=5)
        page.mouse.up()
        settle(page)
        moved = call(engine, f"/api/edit/demo/stories/{story['id']}")["story"]["points"][0][
            "notes"
        ][0]
        assert (moved["x"], moved["y"]) == (note["x"] + 60, note["y"] + 40)

        # Story 的格式：底色與標題的對齊
        page.get_by_role("button", name="格式…").click()
        format_dialog = page.locator(".story-format-dialog")
        format_dialog.get_by_label("設定Story 的底色").check()
        format_dialog.get_by_label("Story 的底色", exact=True).fill("#eef3f8")
        format_dialog.get_by_label("標題的對齊").select_option("center")
        format_dialog.get_by_role("button", name="確定").click()
        settle(page)
        saved = call(engine, f"/api/edit/demo/stories/{story['id']}")["story"]
        assert saved["format"]["background"] == "#eef3f8"
        assert saved["format"]["title_align"] == "center"

        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/story/{story['id']}")
        expect(page.locator(".story-points button.active")).to_have_text("全公司概況")
        expect(page.locator(".story .story-note")).to_have_text("第一季的重點")
        assert (
            page.evaluate("getComputedStyle(document.querySelector('.story')).backgroundColor")
            == "rgb(238, 243, 248)"
        )

        # 列印的頁面設定：紙張、方向與縮放寫成列印用的樣式，再開啟瀏覽器的列印
        page.evaluate("window.print = () => { window.__printed = true; }")
        page.get_by_label("匯出").select_option("pdf")
        setup = page.locator(".page-setup")
        setup.get_by_label("紙張").select_option("A3")
        setup.get_by_label("方向").select_option("portrait")
        setup.get_by_label("列印縮放").select_option("percent")
        setup.get_by_label("縮放比例").fill("50")
        setup.get_by_role("button", name="列印").click()
        page.wait_for_function("window.__printed === true")
        css = page.evaluate("document.getElementById('tmig-print-setup').textContent")
        assert "size: A3 portrait" in css and "zoom: 0.5000" in css
        assert errors == []
        browser.close()


def test_url_action_loads_the_web_object(engine: str) -> None:
    """內嵌網頁與網址動作：dashboard 加上空白的網頁物件，網址動作的目標指定它；檢視器點選標記後，
    網頁物件載入代入標記值的網址（不開新分頁）。目標改成「新的瀏覽器分頁」後在新分頁開啟。"""
    from playwright.sync_api import Page, Route, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('[data-saving="false"]')
        page.wait_for_timeout(300)

    def board() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]

    def webs(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for zone in zones:
            if zone["kind"] == "web":
                found.append(zone)
            found.extend(webs(zone["children"]))
        return found

    def fulfill(route: Route) -> None:
        # 外部網頁以固定的內容回應（測試環境不連外）
        route.fulfill(status=200, content_type="text/html", body="<p>region page</p>")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1500, "height": 950})
        context.route("https://example.com/**", fulfill)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector(".dash-editor .dashboard [data-zone] svg", timeout=30000)

        # 當作篩選器使用：自動產生篩選動作（來源是這張工作表，目標是 dashboard 上其他的工作表），取消時移除
        def zones(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [z for item in items for z in (item, *zones(item["children"]))]

        source = next(
            z for z in zones(board()["zones"]) if z.get("worksheet") == "ws_sales_by_region"
        )
        side_tab(page, "版面配置")
        page.locator(f'[data-tree-zone="{source["id"]}"]').click()
        # （勾選的狀態依伺服器上的動作：建立之後才變成勾選）
        page.get_by_label("當作篩選器使用").click()
        expect(page.get_by_label("當作篩選器使用")).to_be_checked()
        (generated,) = call(engine, "/api/edit/demo/outline")["actions"]
        assert (generated["kind"], generated["activation"], generated["sources"]) == (
            "filter",
            "select",
            ["ws_sales_by_region"],
        )
        on_board = {z["worksheet"] for z in zones(board()["zones"]) if z["kind"] == "worksheet"}
        assert set(generated["targets"]) == on_board - {"ws_sales_by_region"}
        page.get_by_label("當作篩選器使用").click()
        expect(page.get_by_label("當作篩選器使用")).not_to_be_checked()
        assert call(engine, "/api/edit/demo/outline")["actions"] == []

        # 匯出按鈕（Tableau 的「下載」物件）：格式改成 PowerPoint，再改成顯示／隱藏區塊，最後移除
        side_tab(page, "dashboard")
        page.get_by_role("button", name="浮動", exact=True).click()
        page.locator(".object-buttons").get_by_role("button", name="匯出按鈕").click()
        settle(page)

        def button() -> dict[str, Any]:
            return next(z for z in board()["zones"] if z["kind"] == "button")["button"]

        assert (button()["action"], button()["export"]) == ("export", "pdf")
        side_tab(page, "版面配置")
        page.get_by_label("匯出的格式").select_option("powerpoint")
        settle(page)
        assert button()["export"] == "powerpoint"
        page.get_by_label("按鈕的動作").select_option("toggle")
        settle(page)
        assert button()["action"] == "toggle" and len(button()["zones"]) == 1
        assert button()["export"] is None
        page.locator(".dash-props").get_by_role("button", name="移除").click()
        settle(page)
        assert not any(z["kind"] == "button" for z in board()["zones"])

        # 以浮動方式加入網頁物件，網址留空白（由網址動作載入）
        side_tab(page, "dashboard")
        page.locator(".object-buttons").get_by_role("button", name="網頁").click()
        settle(page)
        side_tab(page, "版面配置")
        url = page.locator(".dash-props").get_by_label("網址")
        url.fill("")
        page.locator(".dash-props .prop-title").click()
        settle(page)
        (web,) = webs(board()["zones"])
        assert web.get("url") is None
        # 拖到右下角（不蓋住來源工作表）
        size = board()["size"]
        selected = page.locator(".dash-zone.selected")
        box = selected.bounding_box()
        canvas = page.locator(".dash-editor .dashboard").bounding_box()
        assert box is not None and canvas is not None
        page.mouse.move(box["x"] + 20, box["y"] + 20)
        page.mouse.down()
        page.mouse.move(
            canvas["x"] + canvas["width"] - box["width"] + 10,
            canvas["y"] + canvas["height"] - box["height"] + 10,
            steps=8,
        )
        page.mouse.up()
        settle(page)
        (web,) = webs(board()["zones"])
        assert web["x"] > size["width"] / 2 and web["y"] > size["height"] / 3

        # 網址動作：選取 Sales by Region 的標記時，把區域代入網址，載入這個網頁物件
        menubar(page, "dashboard", "動作…")
        page.locator("#new-action-kind").select_option("url")
        page.get_by_role("button", name="新增動作").click()
        page.get_by_label("動作名稱").fill("開啟區域頁")
        page.locator('[aria-label="來源工作表"] label', has_text="Sales by Region").locator(
            "input"
        ).check()
        page.locator(".action-form").get_by_label("網址", exact=True).fill(
            "https://example.com/region?name=<Region>"
        )
        target = page.get_by_label("網址目標")
        assert target.locator("option").count() == 3
        target.select_option(f"zone:{web['id']}")
        page.locator(".action-form").get_by_role("button", name="確定").click()
        expect(page.locator(".modal table.list")).to_contain_text("開啟區域頁")
        page.locator(".modal .dialog-actions").get_by_role("button", name="關閉").click()
        (action,) = [
            a for a in call(engine, "/api/workbooks/demo/ir")["actions"] if a["kind"] == "url"
        ]
        assert (action["activation"], action["url"]["target"], action["url"]["zone"]) == (
            "select",
            "zone",
            web["id"],
        )
        assert action["url"]["fields"] == [{"placeholder": "<Region>", "key": "f_region"}]

        # 檢視器：網頁物件原本是空白的；點選 East 後載入網址
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/d/db_ee909b0c")
        page.wait_for_selector(".dashboard [data-zone] svg")
        zone = page.locator(f".dashboard [data-zone='{web['id']}']")
        expect(zone.locator("iframe")).to_have_count(0)
        bar = "svg path[aria-roledescription='bar']"
        page.locator(
            f"[data-worksheet='ws_sales_by_region'] {bar}[aria-label*='East']"
        ).first.click()
        frame = zone.locator("iframe")
        expect(frame).to_have_attribute("data-url", "https://example.com/region?name=East")
        expect(page.frame_locator(f"[data-zone='{web['id']}'] iframe").locator("p")).to_have_text(
            "region page"
        )
        assert len(context.pages) == 1  # 沒有開新分頁
        page.locator(
            f"[data-worksheet='ws_sales_by_region'] {bar}[aria-label*='West']"
        ).first.click()
        expect(frame).to_have_attribute("data-url", "https://example.com/region?name=West")

        # 目標改成「新的瀏覽器分頁」：在新分頁開啟，網頁物件不變
        action["url"].update(target="browser", zone=None)
        call(engine, "/api/edit/demo/actions", {"action": action})
        page.reload()
        page.wait_for_selector(".dashboard [data-zone] svg")
        with context.expect_page() as opened:
            page.locator(
                f"[data-worksheet='ws_sales_by_region'] {bar}[aria-label*='East']"
            ).first.click()
        assert opened.value.url == "https://example.com/region?name=East"
        expect(zone.locator("iframe")).to_have_count(0)
        assert errors == []
        browser.close()


def test_export_image_and_powerpoint(engine: str, tmp_path: Path) -> None:
    """匯出：畫面上的 dashboard 畫成 PNG，PowerPoint 是一張投影片放這張圖（桌面版以存檔對話框問位置）。"""
    from playwright.sync_api import Dialog, expect, sync_playwright

    answers = [str(tmp_path / "dashboard.png"), str(tmp_path / "dashboard")]

    def answer(dialog: Dialog) -> None:
        dialog.accept(answers.pop(0))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("dialog", answer)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        page.wait_for_function(
            "(() => { const items = Array.from(document.querySelectorAll('.worksheet'));"
            " return items.length > 0 && items.every(e => e.dataset.loading === 'false'); })()"
        )
        page.get_by_label("匯出").select_option("image")
        expect(page.locator(".toast")).to_contain_text("已匯出")
        png = (tmp_path / "dashboard.png").read_bytes()
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        assert (width, height) == (2400, 1600)  # 1200×800 的 dashboard，兩倍解析度
        page.locator(".toast").evaluate("element => element.remove()")
        page.get_by_label("匯出").select_option("powerpoint")
        expect(page.locator(".toast")).to_contain_text("已匯出")
        browser.close()
    with zipfile.ZipFile(tmp_path / "dashboard.pptx") as archive:
        names = set(archive.namelist())
        assert {"ppt/presentation.xml", "ppt/slides/slide1.xml", "ppt/media/image1.png"} <= names
        assert archive.read("ppt/media/image1.png").startswith(b"\x89PNG")
        assert archive.testzip() is None


def test_pages_and_disaggregation(engine: str) -> None:
    """頁面架：拖到「頁面」後出現頁面控制項，切換頁只顯示那一頁；檢視器也能切換。
    彙總量值關閉後量值不彙總（每一列資料一個標記）。"""
    from playwright.sync_api import Page, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        page.locator('.data-pane [data-field="f_order_date"]').drag_to(
            page.locator('[data-drop="pages"]')
        )
        control = page.locator(".editor [data-page-control]")
        control.wait_for()
        settle(page)
        # 沒有存過的頁：從第一頁開始，並存回工作表
        page.wait_for_function(
            "() => document.querySelector('.editor [data-page-control] select')?.value === '0'"
        )
        settle(page)
        years = control.locator("option").all_inner_texts()
        assert len(years) >= 2 and years == sorted(years)
        saved = sheet()
        assert saved["pages"]["date"] == {"kind": "part", "unit": "year"}
        assert str(saved["page"]) == years[0]
        control.get_by_role("button", name="下一頁").click()
        settle(page)
        assert str(sheet()["page"]) == years[1]

        # 彙總量值關閉：SUM(Sales) 變成不彙總的 Sales
        side_tab(page, "分析")
        page.get_by_label("彙總量值").uncheck()
        settle(page)
        saved = sheet()
        assert saved["disaggregated"] is True
        assert saved["columns"][0]["agg"] is None
        page.get_by_label("彙總量值").check()
        settle(page)
        assert sheet()["columns"][0]["agg"] == "sum"
        side_tab(page, "資料")

        # 檢視器：頁面控制項切換頁
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        viewer = page.locator(".single-sheet [data-page-control]")
        viewer.wait_for()
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        # 存下的頁（第二頁）在工作表載入後才套用到控制項
        page.wait_for_function(
            "() => document.querySelector('.single-sheet [data-page-control] select')?.value === '1'"
        )
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            viewer.get_by_role("button", name="上一頁").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["pages"] == {"ws_sales_by_region": int(years[0])}
        assert errors == []
        browser.close()


def test_forecast_and_clusters_in_editor(engine: str) -> None:
    """分析：預測（預測選項：正好 6 個月、預測區間）在折線之後加上估計的月份與預測區間；叢集依量值分組並放到
    色彩上（叢集欄位以 Cluster N 顯示）。移除時一併移除色彩上的叢集欄位。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet(worksheet: str) -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{worksheet}")["worksheet"]

    def render(worksheet: str) -> dict[str, Any]:
        return call(engine, f"/api/workbooks/demo/worksheets/{worksheet}/render", {})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        before = render("ws_monthly_profit")["rows"]
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        side_tab(page, "分析")
        page.get_by_role("button", name="＋ 預測…").click()
        dialog = page.locator(".forecast-dialog")
        dialog.get_by_label("正好").check()
        dialog.get_by_label("預測的期數").fill("6")
        dialog.get_by_label("預測的期間單位").select_option("month")
        dialog.get_by_role("button", name="確定").click()
        # 先建立預測指標的欄位，之後才存檔：等對話框關閉（已開始存檔）
        dialog.wait_for(state="detached")
        settle(page)
        saved = sheet("ws_monthly_profit")
        forecast = saved["forecast"]
        assert (forecast["length"], forecast["size"], forecast["unit"]) == ("next", 6, "month")
        assert [m["field"] for m in forecast["measures"]] == ["f_profit"]
        assert forecast["indicator"]
        rendered = render("ws_monthly_profit")
        # 最後一個月改以估計值呈現，另外估計之後的 6 個月
        assert rendered["rows"] == before + 6
        assert any("指數平滑" in n for n in rendered["notes"])
        assert rendered["spec"]["layer"][0]["name"].startswith("__deco_forecast")
        # 預測選項的「以 0 填補缺少的值」（2026-11-21 稽核回報勾了之後沒有預測：查證是存檔完成前就看畫面）：
        # 勾選之後預測還在——估計的月份與預測區間都畫在圖上
        page.get_by_role("button", name="預測 ✓").click()
        dialog = page.locator(".forecast-dialog")
        dialog.get_by_label("以 0 填補缺少的值").check()
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        settle(page)
        assert sheet("ws_monthly_profit")["forecast"]["fill_zeros"] is True
        filled = render("ws_monthly_profit")
        assert filled["rows"] == before + 6
        assert not [n for n in filled["notes"] if n.startswith("預測（")]
        band = page.locator(".editor-canvas svg [class*='__deco_forecast']")
        band.first.wait_for(state="attached")
        expect(page.locator(".status-bar")).to_contain_text(f"{before + 6} 列")
        page.get_by_role("button", name="預測 ✓").click()
        page.locator(".forecast-dialog").get_by_role("button", name="移除預測").click()
        settle(page)
        assert sheet("ws_monthly_profit")["forecast"] is None

        # 叢集：Sub-Category 依檢視中的量值（SUM(Profit) 等）分成 3 群
        page.goto(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)
        side_tab(page, "分析")
        page.get_by_role("button", name="＋ 叢集…").click()
        dialog = page.locator(".cluster-dialog")
        dialog.get_by_label("自動決定叢集數").uncheck()
        dialog.get_by_label("叢集數", exact=True).fill("3")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        settle(page)
        saved = sheet("ws_sub_category_profit")
        cluster = saved["cluster"]
        # 變數預設為檢視中的量值（與 Tableau 相同）
        assert cluster["count"] == 3 and cluster["measures"][0]["field"] == "f_profit"
        assert saved["marks"]["color"]["field"] == cluster["field"]
        spec = render("ws_sub_category_profit")["spec"]
        shown = {
            v for record in spec["data"]["values"] for v in record.values() if isinstance(v, str)
        }
        assert {"Cluster 1", "Cluster 2", "Cluster 3"} <= shown
        page.get_by_role("button", name="叢集 ✓").click()
        page.locator(".cluster-dialog").get_by_role("button", name="移除叢集").click()
        settle(page)
        saved = sheet("ws_sub_category_profit")
        assert saved["cluster"] is None and saved["marks"]["color"] is None
        assert errors == []
        browser.close()


def test_hierarchies_and_drill(engine: str) -> None:
    """資料窗格建立階層（Region → State/Province → City）；欄、列架上的膠囊以 ⊞ 向下切入、⊟ 收合；
    檢視器以工作表標題旁的按鈕切入，數字與存檔時就把下一層放在架上的工作表相同。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]

    def rows() -> list[str]:
        return [u["field"] for u in sheet()["rows"]]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        pane = page.locator(".data-pane")
        # Region 不在階層中：膠囊沒有切入按鈕
        assert page.locator('[data-shelf="rows"] .pill-drill').count() == 0
        region = pane.locator('[data-field="f_region"]')
        region.hover()
        region.get_by_role("button", name="以「Region」建立階層").click()
        dialog = page.locator(".hierarchy-dialog")
        expect(dialog.get_by_label("階層名稱")).to_have_value("Region 階層")
        dialog.get_by_label("階層名稱").fill("Location")
        dialog.get_by_label("加入階層的欄位").select_option(label="City")
        dialog.get_by_role("button", name="加入").click()
        dialog.get_by_label("加入階層的欄位").select_option(label="State/Province")
        dialog.get_by_role("button", name="加入").click()
        dialog.get_by_role("button", name="上移 State/Province").click()
        dialog.get_by_role("button", name="確定").click()
        group = pane.locator('[data-hierarchy="Location"]')
        group.wait_for()
        assert group.locator(".field .field-name").all_inner_texts() == [
            "Region",
            "State/Province",
            "City",
        ]
        catalog = call(engine, "/api/edit/demo/catalog")
        assert catalog["datasources"][0]["hierarchies"] == [
            {"name": "Location", "fields": ["f_region", "f_state_province", "f_city"]}
        ]

        # 膠囊的 ⊞：加入下一層；下一層膠囊再 ⊞ 加入 City；Region 的 ⊟ 收合兩層
        drill = page.locator('[data-shelf="rows"] .pill-drill')
        drill.first.wait_for()
        expect(drill.first).to_have_attribute("aria-label", "向下切入到 State/Province")
        drill.first.click()
        settle(page)
        assert rows() == ["f_region", "f_state_province"]
        expect(drill.first).to_have_attribute("aria-label", "收合 State/Province")
        drill.nth(1).click()
        settle(page)
        assert rows() == ["f_region", "f_state_province", "f_city"]
        drill.first.click()
        settle(page)
        assert rows() == ["f_region"]

        # 檢視器：標題旁的「向下切入到 State/Province」
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        viewer = page.locator(".single-sheet .worksheet")
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            viewer.get_by_role("button", name="向下切入到 State/Province").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["drill"] == {"f_region": 1}
        data = response.value.json()
        assert data["table"]["headers"][:2] == ["Region", "State/Province"]
        expect(viewer.get_by_role("button", name="向上收合 State/Province")).to_be_visible()
        expect(viewer.get_by_role("button", name="向下切入到 City")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            viewer.get_by_role("button", name="向上收合 State/Province").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert "drill" not in body
        assert response.value.json()["table"]["headers"][0] == "Region"

        # 資料窗格：從階層移除、刪除階層
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        city = group.locator('[data-field="f_city"]')
        city.hover()
        city.get_by_role("button", name="從階層移除「City」").click()
        expect(group.locator(".field")).to_have_count(2)
        name = group.locator(".hierarchy-name")
        name.hover()
        name.get_by_role("button", name="刪除階層 Location").click()
        expect(group).to_have_count(0)
        assert call(engine, "/api/edit/demo/catalog")["datasources"][0]["hierarchies"] == []
        assert page.locator('[data-shelf="rows"] .pill-drill').count() == 0
        assert errors == []
        browser.close()


def test_tooltip_sheet_in_editor(engine: str) -> None:
    """標記卡的「工具提示…」：加入工具提示中的工作表（以選定的欄位篩選）；檢視器滑過長條時工具提示中
    顯示以 Region 篩選的 Monthly Profit。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        page.get_by_role("button", name="工具提示…").click()
        dialog = page.locator(".tooltip-dialog")
        dialog.get_by_role("button", name="加入工作表").click()
        dialog.get_by_label("工具提示中的工作表", exact=True).select_option(label="Monthly Profit")
        dialog.get_by_label("最大寬度").fill("280")
        dialog.get_by_label("選定的欄位").check()
        dialog.get_by_label("Region").check()
        dialog.get_by_role("button", name="確定").click()
        settle(page)
        tips = sheet()["marks"]["tooltip_sheets"]
        assert tips == [
            {
                "worksheet": "ws_monthly_profit",
                "max_width": 280,
                "max_height": 300,
                "fields": [
                    {
                        "field": "f_region",
                        "agg": None,
                        "date": None,
                        "continuous": False,
                        "table_calc": None,
                    }
                ],
            }
        ]

        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        # 圖表本身的長條（凍結的欄標題、座標軸是圖表的複本，也有同樣的標記）
        bar = page.locator(".single-sheet svg path[aria-roledescription='bar']:not(.frozen *)").last
        label = bar.get_attribute("aria-label") or ""
        region = label.split("Region: ")[1].split(";")[0]
        with page.expect_response(
            lambda r: "ws_monthly_profit" in r.url and r.url.endswith("/render")
        ) as response:
            bar.hover()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["tooltip"]["source"] == "ws_sales_by_region"
        # 標記的所有維度都送出；只以選定的 Region 篩選
        assert body["tooltip"]["mark"]["f_region"] == region
        tooltip = page.locator(".viz-tooltip")
        expect(tooltip.locator("[data-tooltip-sheet='ws_monthly_profit'] svg")).to_have_count(1)
        west = response.value.json()
        everything = call(engine, "/api/workbooks/demo/worksheets/ws_monthly_profit/render", {})
        assert west["table"]["raw"] != everything["table"]["raw"]

        # 移除工具提示中的工作表
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        page.get_by_role("button", name="工具提示…").click()
        dialog.get_by_role("button", name="移除工具提示中的工作表").click()
        dialog.get_by_role("button", name="確定").click()
        settle(page)
        assert sheet()["marks"]["tooltip_sheets"] == []
        assert errors == []
        browser.close()


def test_dynamic_parameters_in_editor(engine: str) -> None:
    """參數對話框的「開啟報表時的值」與「開啟報表時取自欄位」：選了欄位就以目前的資料填入，存成動態參數；
    檢視器取得的參數是以資料取值後的。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    calc = call(
        engine,
        "/api/edit/demo/calcs",
        {"datasource": "ds_orders", "caption": "Latest", "formula": "{MAX([Order Date])}"},
    )["field"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)

        page.locator("button[title='建立參數']").click()
        dialog = page.locator(".parameter-dialog")
        dialog.get_by_label("參數名稱").fill("As Of")
        dialog.get_by_label("參數的資料類型").select_option("date")
        dialog.get_by_label("開啟報表時的值").select_option(calc["id"])
        expect(dialog.get_by_label("參數的目前值")).to_have_value("2024-12-30")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)

        page.locator("button[title='建立參數']").click()
        dialog.get_by_label("參數名稱").fill("Region Choice")
        dialog.get_by_label("參數的資料類型").select_option("string")
        dialog.get_by_role("radio", name="清單").check()
        dialog.get_by_label("開啟報表時取自欄位").select_option("f_region")
        expect(dialog.get_by_label("允許的值清單")).to_have_value("Central\nEast\nSouth\nWest")
        expect(dialog.get_by_label("參數的目前值")).to_have_value("Central")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)

        catalog = call(engine, "/api/edit/demo/catalog")
        params = {p["caption"]: p for p in catalog["parameters"]}
        assert params["As Of"]["value_field"] == calc["id"]
        assert params["As Of"]["value"] == "2024-12-30"
        assert params["Region Choice"]["domain_field"] == "f_region"
        assert params["Region Choice"]["allowed"]["values"] == ["Central", "East", "South", "West"]
        viewer = {p["caption"]: p for p in call(engine, "/api/workbooks/demo/ir")["parameters"]}
        assert viewer["As Of"]["value"] == "2024-12-30"
        assert errors == []
        browser.close()


def test_device_layouts_in_dashboard_editor(engine: str) -> None:
    """dashboard 編輯器的裝置版面：新增手機版面（預設版面上的物件由上到下排列）、在手機版面上移除區塊
    只影響手機版面、刪除手機版面；檢視器在手機大小的畫面上用手機版面。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('[data-saving="false"]')
        page.wait_for_timeout(300)

    def board() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]

    def sheets(zones: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for zone in zones:
            if zone["kind"] == "worksheet":
                found.append(zone["worksheet"])
            found.extend(sheets(zone["children"]))
        return found

    default_sheets = sheets(board()["zones"])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector(".dash-editor .dashboard [data-zone] svg", timeout=30000)
        page.get_by_label("新增裝置版面").select_option("phone")
        settle(page)
        expect(page.get_by_label("裝置版面", exact=True)).to_have_value("phone")
        (phone,) = board()["layouts"]
        assert (phone["device"], phone["sizing"], phone["size"]["width"]) == (
            "phone",
            "fit_width",
            375,
        )
        assert sheets(phone["zones"]) == default_sheets
        stage = page.locator(".dash-stage")
        box = stage.bounding_box()
        assert box is not None and box["width"] <= 376
        # 手機版面上移除第一個工作表：只影響手機版面
        first = next(
            z for z in phone["zones"][0]["children"][0]["children"] if z["kind"] == "worksheet"
        )
        side_tab(page, "版面配置")
        page.locator(f'[data-tree-zone="{first["id"]}"]').click()
        expect(page.locator(f'[data-zone-props="{first["id"]}"]')).to_be_visible()
        page.locator(".dash-props").get_by_role("button", name="移除").click()
        settle(page)
        saved = board()
        assert sheets(saved["layouts"][0]["zones"]) == default_sheets[1:]
        assert sheets(saved["zones"]) == default_sheets

        # 檢視器：手機大小的畫面用手機版面
        phone_page = browser.new_page(viewport={"width": 390, "height": 844})
        phone_page.goto(f"{engine}/?token={TOKEN}")
        phone_page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        phone_page.wait_for_selector(
            ".dashboard[data-device='phone'] [data-zone] svg", timeout=30000
        )
        phone_page.close()

        side_tab(page, "dashboard")
        page.get_by_role("button", name="刪除手機版面").click()
        settle(page)
        assert board()["layouts"] == []
        expect(page.get_by_label("裝置版面", exact=True)).to_have_value("default")
        assert errors == []
        browser.close()


def test_maps_in_editor(engine: str) -> None:
    """地圖：連按兩下地理角色的欄位建立地圖（Latitude／Longitude (generated) 放到欄列架、州的形狀填色）；
    地圖卡改變背景地圖；資料窗格指定城市的地理角色。底圖與形狀從 /api/geo/ 取得。"""
    from playwright.sync_api import Page, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Map"})["worksheet"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        shapes: list[int] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "response",
            lambda r: shapes.append(r.status) if "/api/geo/" in r.url else None,
        )
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        page.wait_for_selector('.editor[data-saving="false"]')
        page.locator('.data-pane li[data-field="f_state_province"]').dblclick()
        settle(page)
        saved = call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]
        assert [u["field"] for u in saved["rows"]] == ["latitude_generated"]
        assert [u["field"] for u in saved["columns"]] == ["longitude_generated"]
        assert [u["field"] for u in saved["marks"]["detail"]] == ["f_state_province"]
        assert saved["marks"]["geometry"]["field"] == "geometry_generated"
        page.wait_for_function(
            "document.querySelectorAll('.editor .worksheet svg path').length > 40", timeout=30000
        )
        assert shapes and all(status == 200 for status in shapes)
        card = page.locator(".map-card")
        card.get_by_label("背景地圖").select_option("dark")
        settle(page)
        saved = call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]
        assert saved["map"]["style"] == "dark"
        page.locator('.data-pane li[data-field="f_city"]').hover()
        page.get_by_role("button", name="「City」的地理角色").click()
        dialog = page.locator(".geo-dialog")
        dialog.get_by_label("地理角色").select_option("city")
        dialog.get_by_role("button", name="確定").click()
        page.wait_for_selector(".geo-dialog", state="detached")
        catalog = call(engine, "/api/edit/demo/catalog")["datasources"][0]
        assert next(f for f in catalog["fields"] if f["id"] == "f_city")["geo_role"] == "city"
        assert page.locator('.data-pane li[data-field="latitude_generated"]').count() == 1

        # 新增標記圖層：原本的標記卡成為圖層 1，新的圖層 2 放上城市（圓點畫在州的填色上面）
        card.get_by_role("button", name="＋ 標記圖層").click()
        settle(page)
        tabs = page.locator(".marks-card .mark-tabs button")
        assert tabs.all_inner_texts() == ["全部", "圖層 1", "圖層 2"]
        assert tabs.nth(2).get_attribute("aria-selected") == "true"
        page.locator('.data-pane li[data-field="f_city"]').drag_to(
            page.locator('[data-drop="marks.detail"]')
        )
        settle(page)
        saved = call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]
        assert len(saved["panes"]) == 2 and saved["marks"]["detail"] == []
        assert [u["field"] for u in saved["panes"][0]["marks"]["detail"]] == ["f_state_province"]
        assert [u["field"] for u in saved["panes"][1]["marks"]["detail"]] == ["f_city"]
        rendered = call(engine, f"/api/workbooks/demo/worksheets/{created['id']}/render", {})
        assert rendered["unsupported"] is None, rendered
        # 圖層 2 下移成為最下面的圖層，再移除：只剩一個圖層時回到單一張標記卡
        page.locator(".layer-actions").get_by_role("button", name="下移").click()
        settle(page)
        saved = call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]
        assert [u["field"] for u in saved["panes"][0]["marks"]["detail"]] == ["f_city"]
        page.locator(".layer-actions").get_by_role("button", name="移除圖層").click()
        settle(page)
        saved = call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]
        assert saved["panes"] == []
        assert [u["field"] for u in saved["marks"]["detail"]] == ["f_state_province"]

        # 檢視器的地圖選取工具：矩形拖曳選取範圍內的標記，工具提示釘住（可以只保留、排除）
        page.goto(f"{engine}/w/demo/s/{created['id']}")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        page.wait_for_selector('.single-sheet .chart[data-zoomable="true"]', timeout=30000)
        tools = page.locator(".single-sheet .map-tools")
        tools.get_by_role("button", name="矩形選取").click()
        overlay = page.locator(".single-sheet .select-overlay")
        box = overlay.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + 5, box["y"] + 5)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=5)
        page.mouse.move(box["x"] + box["width"] - 5, box["y"] + box["height"] - 5, steps=5)
        page.mouse.up()
        pinned = page.locator(".viz-tooltip.pinned")
        pinned.wait_for()
        text = pinned.inner_text()
        found = re.search(r"(\d+) 個標記", text)
        assert found is not None and int(found.group(1)) > 10, text
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            pinned.get_by_role("button", name="✓ 只保留").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["marks"][0]["mode"] == "keep" and len(body["marks"][0]["marks"]) > 10
        # 半徑選取：從中心拖曳出一個圓
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        page.wait_for_selector('.single-sheet .chart[data-zoomable="true"]', timeout=30000)
        tools.get_by_role("button", name="半徑選取").click()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 60, cy + 40, steps=5)
        page.mouse.up()
        tools.get_by_role("button", name="半徑選取").click()  # 再按一次回到平移
        assert page.locator(".single-sheet .select-overlay").count() == 0

        # 兩個標記圖層（州的填色、城市的圓點）：兩層的標記都可以選取，各自只有自己的維度
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        settle(page)
        page.locator(".map-card").get_by_role("button", name="＋ 標記圖層").click()
        settle(page)
        page.locator('.data-pane li[data-field="f_city"]').drag_to(
            page.locator('[data-drop="marks.detail"]')
        )
        settle(page)
        page.goto(f"{engine}/w/demo/s/{created['id']}")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        page.wait_for_selector('.single-sheet .chart[data-zoomable="true"]', timeout=30000)
        # 德州的形狀上沒有被城市蓋住的一點（底圖的州名不擋住標記）
        point = page.evaluate(
            """() => {
              const paths = Array.from(document.querySelectorAll('.single-sheet svg .mark-shape.role-mark path'));
              const texas = paths.find((p) => p.__data__ && p.__data__.datum && p.__data__.datum.__row >= 0
                && Object.values(p.__data__.datum).includes('Texas'));
              if (!texas) return null;
              const box = texas.getBoundingClientRect();
              for (let i = 1; i < 20; i++) for (let j = 1; j < 20; j++) {
                const x = box.left + (box.width * i) / 20, y = box.top + (box.height * j) / 20;
                if (document.elementFromPoint(x, y) === texas) return [x, y];
              }
              return null;
            }"""
        )
        assert point is not None
        page.mouse.click(point[0], point[1])
        pinned.wait_for()
        assert "Texas" in pinned.inner_text()
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            pinned.get_by_role("button", name="✓ 只保留").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["marks"] == [{"mode": "keep", "marks": [{"f_state_province": "Texas"}]}]
        # 只剩德州與德州的城市；矩形選取全部：兩個圖層的標記都選到。要等新的地圖畫好（只剩德州的形狀）：重畫要等
        # 更細的底圖下載，在那之前 data-loading、data-zoomable 還是舊圖的值，框選落在還沒畫好的地圖上什麼都選不到
        # （2026-11-23 完整測試中電腦忙時出現一次；網路延遲 1.5 秒時每次都如此）
        page.wait_for_function(
            """() => {
              const chart = document.querySelector('.single-sheet .chart');
              if (!chart || chart.dataset.drawn !== 'true' || chart.dataset.zoomable !== 'true') return false;
              const states = Array.from(document.querySelectorAll('.single-sheet svg .mark-shape.role-mark path'))
                .filter((p) => p.__data__ && p.__data__.datum && p.__data__.datum.__row >= 0);
              return states.length > 0 && states.every((p) => Object.values(p.__data__.datum).includes('Texas'));
            }""",
            timeout=60000,
        )
        tools.get_by_role("button", name="矩形選取").click()
        box = page.locator(".single-sheet .select-overlay").bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + 5, box["y"] + 5)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] - 5, box["y"] + box["height"] - 5, steps=8)
        page.mouse.up()
        pinned.wait_for()
        found = re.search(r"(\d+) 個標記", pinned.inner_text())
        assert found is not None and int(found.group(1)) > 5, pinned.inner_text()
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            pinned.get_by_role("button", name="✕ 排除").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert {tuple(sorted(m)) for m in body["marks"][1]["marks"]} == {
            ("f_city",),
            ("f_state_province",),
        }
        assert response.value.json()["unsupported"] is None
        assert errors == []
        browser.close()


def test_overlapping_labels_are_hidden(engine: str) -> None:
    """標記標籤互相重疊時與 Tableau 相同只顯示先畫的：檢視器依畫好的位置隱藏其餘的標籤（清空文字），
    留下的標籤不重疊；允許重疊時全部顯示。"""
    from playwright.sync_api import sync_playwright

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Labels"})["worksheet"]

    def use(field: str, agg: str | None = None) -> dict[str, Any]:
        return {"field": field, "agg": agg, "date": None, "continuous": agg is not None}

    sheet = {
        **created,
        "rows": [use("f_profit", "sum")],
        "columns": [use("f_sales", "sum")],
        "marks": {
            **created["marks"],
            "type": "circle",
            "detail": [use("f_city")],
            "label": [use("f_city")],
        },
    }
    request = urllib.request.Request(
        f"{engine}/api/edit/demo/worksheets/{created['id']}",
        data=json.dumps({"worksheet": sheet}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200

    # 高度以字級為準（與檢視器的判斷相同）：瀏覽器量到的是字型的行框，含字型本身的行距，
    # 高度依作業系統的字型而異（Windows 的 Segoe UI 12px 是 16px），行框相疊時字並沒有重疊
    script = """() => {
        const texts = [...document.querySelectorAll('.worksheet g[class*="__label_"] text')];
        const shown = texts.filter((t) => t.textContent);
        const boxes = shown.map((t) => {
          const r = t.getBoundingClientRect();
          const pad = Math.max(0, (r.height - parseFloat(getComputedStyle(t).fontSize)) / 2);
          return { left: r.left, right: r.right, top: r.top + pad, bottom: r.bottom - pad };
        });
        let overlaps = 0;
        for (let i = 0; i < boxes.length; i++)
          for (let j = i + 1; j < boxes.length; j++) {
            const a = boxes[i], b = boxes[j];
            if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1)
              overlaps++;
          }
        return { total: texts.length, shown: shown.length, overlaps };
    }"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        page.wait_for_selector('.editor .worksheet [data-drawn="true"]', timeout=30000)
        found = page.evaluate(script)
        assert found["total"] > 100 and 0 < found["shown"] < found["total"]
        assert found["overlaps"] == 0
        assert errors == []
        browser.close()


def test_totals_percentages_line_type_colors_and_sheet_controls(engine: str) -> None:
    """總計的位置與「總計使用」、百分比、線條類型、個別成員的顏色，以及工作表上的篩選控制項
    （顯示篩選、只顯示相關的值）在檢視器中的樣子。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def menu(page: Page, label: str) -> None:
        page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text=label).first.click()

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]

    def records() -> list[dict[str, Any]]:
        response = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})
        assert response["unsupported"] is None, response
        return response["spec"]["data"]["values"]

    def confirm(page: Page, dialog: str) -> None:
        page.locator(dialog).get_by_role("button", name="確定").click()
        expect(page.locator(dialog)).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_region"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)

        # 總計：欄總計放在上方，Sales 的總計改為各標記的平均
        menubar(page, "分析", "總計", "總計…")
        dialog = page.locator(".totals-dialog")
        dialog.get_by_label("顯示欄總計").check()
        dialog.get_by_label("欄總計移至頂端").check()
        dialog.get_by_label("SUM(Sales)的總計使用").select_option("avg")
        confirm(page, ".totals-dialog")
        saved = sheet()
        assert (saved["totals"], saved["totals_top"], saved["totals_left"]) == (
            ["rows"],
            True,
            False,
        )
        assert [(v["use"]["field"], v["agg"]) for v in saved["visual_totals"]] == [
            ("f_sales", "avg")
        ]
        rows = records()
        assert rows[0]["__total"] is True and rows[0]["c0_label"] == "Grand Total"
        marks = [r["c1"] for r in rows if not r.get("__total")]
        assert rows[0]["c1"] == pytest.approx(sum(marks) / len(marks))

        # 百分比：占整個表的比例（總計使用改回自動：總計是 100%）
        menubar(page, "分析", "總計", "總計…")
        page.locator(".totals-dialog").get_by_label("SUM(Sales)的總計使用").select_option("")
        confirm(page, ".totals-dialog")
        side_tab(page, "分析")
        page.get_by_label("百分比", exact=True).select_option("table")
        settle(page)
        assert sheet()["percentages"] == "table"
        rows = records()
        assert sum(r["c1"] for r in rows if not r.get("__total")) == pytest.approx(1.0)
        assert rows[0]["c1"] == pytest.approx(1.0)
        assert rows[1]["c1_label"].endswith("%")
        page.get_by_label("百分比", exact=True).select_option("")
        settle(page)
        assert sheet()["percentages"] is None

        # 下拉線：一律顯示（長條圖只有量值的座標軸有），再關掉
        page.get_by_label("下拉線", exact=True).select_option("always")
        settle(page)
        assert sheet()["drop_lines"] == {"x": True, "y": True, "when": "always", "labels": False}
        response = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})
        names = [layer.get("name") for layer in response["spec"].get("layer", [])]
        assert any(str(name).startswith("__deco_drop") for name in names), names
        page.get_by_label("下拉線", exact=True).select_option("")
        settle(page)
        assert sheet()["drop_lines"] is None
        side_tab(page, "資料")

        # 個別成員的顏色：Region 放到色彩，East 指定紅色
        page.locator('.data-pane [data-field="f_region"]').drag_to(
            page.locator('[data-drop="marks.color"]')
        )
        settle(page)
        page.locator(".mark-buttons").get_by_role("button", name="色彩…").click()
        swatch = page.locator(".colors-dialog").get_by_label("East的顏色")
        expect(swatch).to_be_visible()
        swatch.fill("#ff0000")
        confirm(page, ".colors-dialog")
        palette = sheet()["marks"]["color_palette"]
        assert palette["kind"] == "discrete" and palette["palette"] is None
        assert palette["assignments"] == [{"value": "East", "color": "#ff0000"}]
        expect(
            page.locator(
                ".editor-canvas .frozen-host > svg path[aria-roledescription='bar'][aria-label*='East']"
            ).first
        ).to_have_attribute("fill", "#ff0000")

        # 形狀：標記類型改成形狀，Region 放到形狀，East 指定星號
        page.get_by_label("標記類型").select_option("shape")
        settle(page)
        page.locator('.data-pane [data-field="f_region"]').drag_to(
            page.locator('[data-drop="marks.shape"]')
        )
        settle(page)
        page.locator(".mark-buttons").get_by_role("button", name="形狀…").click()
        page.locator(".shapes-dialog").get_by_label("East的形狀").select_option("s:asterisk")
        confirm(page, ".shapes-dialog")
        assert sheet()["marks"]["shape_assignments"] == [
            {"shape": "asterisk", "image": None, "value": "East"}
        ]
        shapes = records()  # 規格中形狀的比例尺：East 是星號（路徑），其餘依預設的順序
        assert shapes

        # 線條類型：線、階梯
        page.get_by_label("標記類型").select_option("line")
        settle(page)
        page.locator(".mark-buttons").get_by_role("button", name="路徑…").click()
        page.get_by_label("線條類型").select_option("step")
        settle(page)
        assert sheet()["marks"]["line_type"] == "step"
        page.get_by_label("線條類型").select_option("jump")
        settle(page)
        assert sheet()["marks"]["line_type"] == "jump"
        page.get_by_label("標記類型").select_option("bar")
        settle(page)

        # 顯示篩選：Category（只選 Furniture）與 Sub-Category（只顯示相關的值）
        for field, keep in (("f_category", "Furniture"), ("f_sub_category", None)):
            page.locator(f'.data-pane [data-field="{field}"]').drag_to(
                page.locator('[data-drop="filters"]')
            )
            page.wait_for_selector(".filter-dialog .value-list li")
            if keep is not None:
                for name in ("Office Supplies", "Technology"):
                    page.locator(".value-list label", has_text=name).locator("input").uncheck()
            page.locator(".filter-dialog").get_by_role("button", name="確定").click()
            expect(page.locator(".filter-dialog")).to_have_count(0)
            page.wait_for_timeout(300)
            settle(page)
        pills = page.locator('[data-shelf="filters"] .pill')
        for index in (0, 1):  # Category、Sub-Category
            pills.nth(index).click()
            menu(page, "顯示篩選")
            settle(page)
        page.locator('[data-shelf="filters"] .pill', has_text="Sub-Category").click()
        menu(page, "只顯示相關的值")
        settle(page)
        controls = sheet()["controls"]
        assert [(c["field"]["field"], c["relevant"]) for c in controls] == [
            ("f_category", False),
            ("f_sub_category", True),
        ]

        # 巢狀排序：列架加上 Sub-Category，依業績遞減、在每個 Region 之內各自排序
        page.locator('.data-pane [data-field="f_sub_category"]').drag_to(
            page.locator('[data-drop="rows"]')
        )
        settle(page)
        page.locator('[data-shelf="rows"] .pill', has_text="Sub-Category").click()
        page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text="排序").first.hover()
        menu(page, "排序…")
        page.get_by_label("排序依據").select_option("by_field")
        page.get_by_label("排序順序").select_option("desc")
        page.locator(".sort-dialog").get_by_label("巢狀").check()
        confirm(page, ".sort-dialog")
        (sort,) = sheet()["sorts"]
        assert (sort["field"], sort["kind"], sort["nested"]) == ("f_sub_category", "by_field", True)
        page.locator('[data-shelf="rows"] .pill', has_text="Sub-Category").click()
        menu(page, "移除")
        settle(page)
        assert [u["field"] for u in sheet()["rows"]] == ["f_region"]

        # 說明：顯示說明（自動產生的文字），再改成自訂的文字
        menubar(page, "工作表", "說明…")
        page.locator(".caption-dialog").get_by_label("顯示說明").check()
        confirm(page, ".caption-dialog")
        assert sheet()["caption_shown"] is True and sheet()["caption"] == []
        note = page.locator(".editor .worksheet-caption")
        expect(note).to_contain_text("每個 Region 的 Sales")
        expect(note).to_contain_text("檢視依 Category 篩選")
        menubar(page, "工作表", "說明…")
        page.get_by_role("textbox", name="說明的文字").click()
        page.keyboard.type("各區的業績（家具）")
        confirm(page, ".caption-dialog")
        assert [r["text"] for r in sheet()["caption"]] == ["各區的業績（家具）"]
        expect(note).to_have_text("各區的業績（家具）")

        # 複製、貼上格式：ws_1 的背景與文字顏色貼到 Sales by Region（欄位與篩選不變）
        styled = {**sheet(), "background": "#112233", "text_color": "#eeeeee"}
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/ws_1",
            data=json.dumps({"worksheet": styled}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()
        page.reload()
        settle(page)
        menubar(page, "格式", "複製格式")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        before = call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]
        menubar(page, "格式", "貼上格式")
        page.wait_for_timeout(300)
        settle(page)
        after = call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]
        assert (after["background"], after["text_color"]) == ("#112233", "#eeeeee")
        assert (after["rows"], after["filters"]) == (before["rows"], before["filters"])
        page.goto(f"{engine}/w/demo/edit/ws_1")
        settle(page)

        # 次要計算：業績的累計，再做合計百分比
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        page.locator(
            ".popup-menu li[role=menuitem] > .menu-label", has_text="快速表計算"
        ).first.hover()
        menu(page, "累計")
        settle(page)
        page.locator('[data-shelf="columns"] .pill').first.click()
        menu(page, "編輯表計算…")
        page.get_by_label("次要計算的類型").select_option("percent_of_total")
        confirm(page, ".table-calc-dialog")
        (calc,) = sheet()["table_calcs"]
        assert (calc["type"], calc["secondary"]["type"]) == ("running_total", "percent_of_total")
        key = next(
            k
            for k in call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})["table"]["keys"]
            if "#" in k
        )
        table = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})["table"]
        shares = [float(row[table["keys"].index(key)]) for row in table["raw"]]
        assert sum(shares) == pytest.approx(1.0)

        # 格式：儲存格大小、對齊，並套用到所有工作表（整份報表一致）
        menubar(page, "格式", "格式…")
        page.get_by_label("列的儲存格大小").fill("28")
        page.get_by_label("儲存格的水平對齊").select_option("center")
        page.get_by_label("標題的垂直對齊").select_option("center")
        page.get_by_role("button", name="套用到所有工作表").click()
        expect(page.locator(".toast", has_text="已把格式套用到其他").first).to_be_visible()
        settle(page)
        formatted = sheet()
        assert formatted["row_style"]["cell"] == 28
        assert (formatted["cell_align"], formatted["label_valign"]) == ("center", "center")
        other = call(engine, "/api/edit/demo/worksheets/ws_monthly_profit")["worksheet"]
        assert other["row_style"]["cell"] == 28 and other["background"] == "#112233"
        assert other["rows"] and other["cell_align"] == "center"

        # 檢視器：工作表旁邊有兩個控制項；Sub-Category 只列出 Furniture 的子類別
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/s/ws_1")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        panel = page.locator(".single-sheet .sheet-controls")
        expect(panel.locator("fieldset.control")).to_have_count(2)
        sub = panel.locator("fieldset.control", has_text="Sub-Category")
        expect(sub.locator("li, option, label").filter(has_text="Chairs").first).to_be_visible()
        expect(sub).not_to_contain_text("Phones")
        # 改選 Technology：Sub-Category 的可選值跟著變
        category = panel.locator("fieldset.control", has_text="Category").first
        category.locator("label", has_text="Technology").locator("input").check()
        expect(sub).to_contain_text("Phones")
        assert errors == []
        browser.close()


def test_datasource_filters_in_datasource_editor(engine: str) -> None:
    """資料來源分頁的資料源篩選：新增（與工作表的篩選同一個對話框）、編輯時看得到全部的值、移除。"""
    from playwright.sync_api import expect, sync_playwright

    def filters() -> list[dict[str, Any]]:
        return call(engine, "/api/edit/demo/datasources/ds_orders/filters")["filters"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit-ds/ds_orders")
        section = page.locator(".dse-filters")
        expect(section.locator(".dse-filter-list li")).to_have_count(
            1
        )  # .twb 原有的 Country/Region
        expect(section).to_contain_text("United States")

        # 新增：Region 只保留 East、West
        section.get_by_label("新增資料源篩選").select_option(label="Region")
        page.wait_for_selector(".filter-dialog .value-list li")
        expect(page.locator(".filter-dialog").get_by_role("tab", name="頂部")).to_have_count(0)
        for name in ("Central", "South"):
            page.locator(".value-list label", has_text=name).locator("input").uncheck()
        page.locator(".filter-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".filter-dialog")).to_have_count(0)
        expect(section.locator(".dse-filter-list li")).to_have_count(2)
        saved = filters()
        assert [(f["field"]["field"], f["stage"]) for f in saved] == [
            ("f_country_region", "datasource"),
            ("f_region", "datasource"),
        ]
        assert (saved[1]["mode"], saved[1]["values"]) == ("include", ["East", "West"])
        # 工作表的資料也跟著篩選
        table = call(engine, "/api/workbooks/demo/worksheets/ws_region_share_by_year/render", {})[
            "table"
        ]
        regions = {row[table["keys"].index("f_region")] for row in table["raw"]}
        assert regions == {"East", "West"}

        # 編輯：可選值是全部四個區域（不套用它自己），勾選的是 East、West
        section.locator(".dse-filter-list li", has_text="Region").last.get_by_role(
            "button", name="編輯"
        ).click()
        page.wait_for_selector(".filter-dialog .value-list li")
        expect(page.locator(".filter-dialog .value-list li")).to_have_count(4)
        expect(
            page.locator(".value-list label", has_text="Central").locator("input")
        ).not_to_be_checked()
        expect(page.locator(".value-list label", has_text="East").locator("input")).to_be_checked()
        page.locator(".filter-dialog").get_by_role("button", name="取消").click()

        # 移除
        section.locator(".dse-filter-list li", has_text="East").get_by_role(
            "button", name="移除"
        ).click()
        expect(section.locator(".dse-filter-list li")).to_have_count(1)
        assert [f["field"]["field"] for f in filters()] == ["f_country_region"]
        assert errors == []
        browser.close()


def test_extract_settings_in_datasource_editor(tmp_path: Path) -> None:
    """資料來源分頁的「連線：即時／萃取」：即時改成萃取（從來源建立資料）、萃取篩選的可選值取自來源、
    立即更新萃取、增量更新的欄位、改回即時。來源資料庫以 DuckDB 檔模擬。"""
    from playwright.sync_api import expect, sync_playwright

    from tmig.data.sources import ConnectionConfig
    from tmig.ir import LiveConnection, TableRelation

    port = free_port()
    app = create_desktop_app(tmp_path / "workspace", TOKEN, DIST, username="amy")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "本機引擎沒有啟動"
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        demo = tmp_path / "demo.twbx"
        shutil.copy(DEMO, demo)
        call(base, "/api/desktop/open", {"path": str(demo)})
        call(base, "/api/workbooks/demo/worksheets/ws_sales_by_region/render", {})  # 匯入萃取的資料
        space = app.state.workspace
        loaded = space.get("demo")
        source = tmp_path / "source.duckdb"
        shutil.copy(loaded.directory / "data" / "ds_orders.duckdb", source)
        connection = LiveConnection(
            kind="live",
            connection_id="sales_duckdb",
            dbclass="oracle",
            dialect="oracle",
            relation=TableRelation(kind="table", name="Orders", table=["Extract", "Extract"]),
        )
        live = loaded.ir.model_copy(
            update={
                "datasources": [
                    ds.model_copy(update={"connection": connection}) for ds in loaded.ir.datasources
                ]
            }
        )
        space.save_ir("demo", live)
        config = ConnectionConfig(
            "sales_duckdb", "duckdb", {"driver": "duckdb", "path": str(source)}
        )
        app.state.runtime.add_configs({"sales_duckdb": config})

        def info() -> dict[str, Any]:
            return call(base, "/api/edit/demo/datasources/ds_orders/extract")

        def regions() -> set[str]:
            table = call(base, "/api/workbooks/demo/worksheets/ws_region_share_by_year/render", {})[
                "table"
            ]
            return {row[table["keys"].index("f_region")] for row in table["raw"]}

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/?token={TOKEN}")
            page.goto(f"{base}/w/demo/edit-ds/ds_orders")
            section = page.locator(".dse-extract")
            expect(section).to_have_attribute("data-mode", "live")
            expect(section.get_by_label("即時")).to_be_checked()

            # 即時 → 萃取：從來源建立資料
            section.get_by_label("萃取", exact=True).click()
            expect(page.locator('.dse-extract[data-mode="extract"]')).to_be_visible(timeout=30000)
            assert info()["mode"] == "extract" and info()["refreshed"]
            assert len(regions()) == 4

            # 萃取篩選：Region 只留 East（可選值是來源的四個區域），立即更新後才生效
            section = page.locator(".dse-extract")
            section.get_by_label("新增萃取篩選").select_option(label="Region")
            page.wait_for_selector(".filter-dialog .value-list li")
            expect(page.locator(".filter-dialog .value-list li")).to_have_count(4)
            for name in ("Central", "South", "West"):
                page.locator(".value-list label", has_text=name).locator("input").uncheck()
            page.locator(".filter-dialog").get_by_role("button", name="確定").click()
            expect(page.locator(".filter-dialog")).to_have_count(0)
            expect(section.locator(".dse-filter-list li")).to_have_count(1)
            assert len(regions()) == 4  # 還沒有更新萃取
            section.get_by_role("button", name="立即更新萃取").click()
            expect(page.locator(".toast", has_text="已更新萃取").first).to_be_visible(timeout=30000)
            assert regions() == {"East"}
            # 再編輯這個萃取篩選：可選值仍是來源的四個區域（萃取中只剩 East）
            section = page.locator(".dse-extract")
            section.locator(".dse-filter-list li").get_by_role("button", name="編輯").click()
            page.wait_for_selector(".filter-dialog .value-list li")
            expect(page.locator(".filter-dialog .value-list li")).to_have_count(4)
            page.locator(".filter-dialog").get_by_role("button", name="取消").click()

            # 增量更新的欄位
            section.get_by_label("增量更新的欄位").select_option(label="Order Date")
            expect(page.locator(".toast", has_text="已儲存萃取設定").first).to_be_visible()
            assert info()["increment"] == "f_order_date"
            page.locator(".dse-extract").get_by_role("button", name="增量更新", exact=True).click()
            expect(page.locator(".toast", has_text="已增量更新萃取").first).to_be_visible(
                timeout=30000
            )

            # 只取前 N 筆、彙總可見維度的資料（勾選後增量更新的欄位清掉）
            idle = '.dse-extract[data-busy="false"]'
            rows = page.locator(".dse-extract").get_by_label("只取前 N 筆")
            rows.fill("7")
            rows.blur()
            expect(page.locator(".toast", has_text="下次更新時生效").first).to_be_visible()
            page.wait_for_selector(idle)
            assert info()["top_rows"] == 7
            page.locator(".dse-extract").get_by_role("button", name="立即更新萃取").click()
            page.wait_for_timeout(200)
            page.wait_for_selector(idle, timeout=30000)
            report = space.get("demo").directory / "data" / "ds_orders.refresh.json"
            assert json.loads(report.read_text(encoding="utf-8"))["tables"][0]["rows"] == 7
            aggregate = page.locator(".dse-extract").get_by_label("彙總可見維度的資料")
            aggregate.click()
            expect(aggregate).to_be_checked()
            page.wait_for_selector(idle)
            assert (info()["aggregate"], info()["increment"]) == (True, None)
            aggregate.click()
            expect(aggregate).not_to_be_checked()
            page.wait_for_selector(idle)

            # 萃取 → 即時
            page.locator(".dse-extract").get_by_label("即時").click()
            expect(page.locator('.dse-extract[data-mode="live"]')).to_be_visible(timeout=30000)
            assert info()["mode"] == "live" and len(regions()) == 4
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_stack_color_effects_labels_axis_and_custom_format(engine: str) -> None:
    """堆疊標記、色彩上的多個欄位、光暈與折線的標記、標籤的方向與換行、次要刻度與各窗格獨立的範圍，
    以及自訂的數字格式字串。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def menu(page: Page, label: str) -> None:
        page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text=label).first.click()

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]

    def spec() -> dict[str, Any]:
        response = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})
        assert response["unsupported"] is None, response
        return response["spec"]

    def confirm(page: Page, dialog: str) -> None:
        page.locator(dialog).get_by_role("button", name="確定").click()
        expect(page.locator(dialog)).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        for field in ("f_category", "f_sales"):
            page.locator(f'.data-pane [data-field="{field}"]').dblclick()
            settle(page)
        page.locator('.data-pane [data-field="f_region"]').drag_to(
            page.locator('[data-drop="marks.color"]')
        )
        settle(page)

        # 堆疊標記：關閉後長條不堆疊（stack: null）
        measure = "x" if spec()["encoding"]["x"].get("type") == "quantitative" else "y"
        assert "stack" not in spec()["encoding"][measure]
        side_tab(page, "分析")
        page.get_by_label("堆疊標記", exact=True).select_option("off")
        settle(page)
        assert sheet()["stack"] == "off"
        assert spec()["encoding"][measure]["stack"] is None
        page.get_by_label("堆疊標記", exact=True).select_option("")
        settle(page)
        assert sheet()["stack"] is None
        side_tab(page, "資料")

        # 色彩上的多個欄位：Segment 放到詳細資料，再從膠囊選單「也用於色彩」
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="marks.detail"]')
        )
        settle(page)
        page.locator('.mark-pills [data-slot="detail"] .pill').click()
        menu(page, "也用於色彩（組合著色）")
        settle(page)
        assert [u["field"] for u in sheet()["marks"]["color_more"]] == ["f_segment"]
        expect(page.locator('.mark-pills .pill-slot.also-color[data-slot="detail"]')).to_have_count(
            1
        )
        color = spec()["encoding"]["color"]
        assert color["field"] == "__color1" and color["title"] == "Region, Segment"
        assert all(", " in member for member in color["scale"]["domain"])
        # 再選一次即拿掉
        page.locator('.mark-pills [data-slot="detail"] .pill').click()
        menu(page, "也用於色彩（組合著色）")
        settle(page)
        assert sheet()["marks"]["color_more"] == []

        # 色彩的效果：光暈與折線上的標記
        page.locator(".mark-buttons").get_by_role("button", name="色彩…").click()
        dialog = page.locator(".colors-dialog")
        dialog.get_by_label("光暈").check()
        dialog.get_by_label("折線上的標記").select_option("all")
        confirm(page, ".colors-dialog")
        marks = sheet()["marks"]
        assert (marks["halo"], marks["markers"]) == (True, "all")

        # 標籤：顯示、由下往上、換行
        page.locator(".mark-buttons").get_by_role("button", name="標籤…").click()
        dialog = page.locator(".label-dialog")
        dialog.get_by_label("顯示標記標籤").select_option("on")
        dialog.get_by_label("標籤的文字方向").select_option("up")
        dialog.get_by_label("換行").check()
        confirm(page, ".label-dialog")
        labels = sheet()["marks"]["labels"]
        assert (labels["show"], labels["direction"], labels["wrap"]) == (True, "up", True)
        texts = [
            layer["mark"] for layer in spec().get("layer", []) if layer["mark"]["type"] == "text"
        ]
        assert texts and all(mark["angle"] == 270 for mark in texts)

        # 編輯座標軸：固定的主要刻度加上次要刻度，各窗格獨立的範圍
        page.locator(
            f'.shelf[data-shelf="{"columns" if measure == "x" else "rows"}"] .pill'
        ).click()
        menu(page, "編輯座標軸…")
        dialog = page.locator(".axis-dialog")
        dialog.get_by_label("各窗格獨立的範圍").check()
        dialog.get_by_label("主要刻度", exact=True).select_option("fixed")
        dialog.get_by_label("刻度間距").fill("200000")
        dialog.get_by_label("次要刻度的大小").fill("50000")
        confirm(page, ".axis-dialog")
        axis = sheet()["axes"][0]
        assert (axis["tick_spacing"], axis["minor_spacing"], axis["independent"]) == (
            200000,
            50000,
            True,
        )
        drawn = spec()["encoding"][measure]["axis"]
        assert drawn["values"][:3] == [0, 50000, 100000] and drawn["tickSize"]["value"] == 3

        # 自訂的數字格式字串：以千為單位、加上 K
        page.locator(
            f'.shelf[data-shelf="{"columns" if measure == "x" else "rows"}"] .pill'
        ).click()
        menu(page, "格式…")
        dialog = page.locator(".format-dialog")
        dialog.get_by_label("數字格式").select_option("custom")
        dialog.get_by_label("自訂格式字串").fill('#,##0,"K"')
        confirm(page, ".format-dialog")
        assert sheet()["formats"] == {"sum(f_sales)": '#,##0,"K"'}
        values = spec()["data"]["values"]
        label = next(
            k for k in values[0] if k.endswith("_label") and str(values[0][k]).endswith("K")
        )
        assert all(str(v[label]).endswith("K") for v in values)
        # 再開啟時認得是自訂的格式
        page.locator(
            f'.shelf[data-shelf="{"columns" if measure == "x" else "rows"}"] .pill'
        ).click()
        menu(page, "格式…")
        dialog = page.locator(".format-dialog")
        assert dialog.get_by_label("數字格式").input_value() == "custom"
        assert dialog.get_by_label("自訂格式字串").input_value() == '#,##0,"K"'
        dialog.get_by_role("button", name="取消").click()
        assert errors == []
        browser.close()


def test_filter_tabs_control_options_missing_values_and_view_data(engine: str) -> None:
    """量值篩選的「至少」與「特殊值」分頁、篩選控制項的自訂（套用按鈕）、顯示遺漏的值、包含在工具提示中、
    格式的特殊值與表格版面，以及編輯器的「檢視資料」。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def menu(page: Page, *labels: str) -> None:
        for label in labels[:-1]:
            page.locator(
                ".popup-menu li[role=menuitem] > .menu-label", has_text=label
            ).first.hover()
        page.locator(
            ".popup-menu li[role=menuitem] > .menu-label", has_text=labels[-1]
        ).first.click()

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]

    def table() -> list[list[Any]]:
        response = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})
        assert response["unsupported"] is None, response
        return response["table"]["rows"]

    def confirm(page: Page, dialog: str) -> None:
        page.locator(dialog).get_by_role("button", name="確定").click()
        expect(page.locator(dialog)).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        for field in ("f_region", "f_sales"):
            page.locator(f'.data-pane [data-field="{field}"]').dblclick()
            settle(page)
        assert len(table()) == 4

        # 量值篩選的「至少」分頁：只有下限
        page.locator('.shelf[data-shelf="columns"] .pill').click()
        menu(page, "篩選…")
        dialog = page.locator(".filter-dialog")
        dialog.get_by_role("tab", name="至少").click()
        expect(dialog.get_by_label("最大值")).to_have_count(0)
        dialog.get_by_label("最小值").fill("600000")
        confirm(page, ".filter-dialog")
        saved = sheet()["filters"][0]
        assert (saved["kind"], saved["min"], saved["max"]) == ("range", 600000, None)
        kept = {row[0] for row in table()}
        assert 0 < len(kept) < 4

        # 格式：顯示空的列與 Null 值的文字——被濾掉的地區仍有標題，儲存格顯示「—」
        menubar(page, "格式", "格式…")
        dialog = page.locator(".format-dialog")
        dialog.get_by_label("顯示空的列").check()
        dialog.get_by_label("Null 值的文字").fill("—")
        dialog.get_by_label("Null 值的標記").select_option("connect")
        confirm(page, ".format-dialog")
        saved = sheet()
        assert (saved["show_empty"], saved["null_text"], saved["null_marks"]) == (
            ["rows"],
            "—",
            "connect",
        )
        rows = table()
        assert (
            len(rows) == 4
            and {row[0] for row in rows if row[1] == "—"} == {row[0] for row in rows} - kept
        )

        # 檢視資料：摘要是每個標記一列，完整資料是基礎資料列
        menubar(page, "分析", "檢視資料…")
        dialog = page.get_by_role("dialog")
        dialog.locator(".data-preview tbody tr").nth(3).wait_for()
        assert dialog.locator(".data-preview tbody tr").count() == 4
        dialog.get_by_role("button", name="完整資料").click()
        dialog.locator(".data-preview thead th", has_text="Order ID").wait_for()
        assert dialog.locator(".data-preview tbody tr").count() > 4
        dialog.get_by_label("關閉").click()
        expect(page.get_by_role("dialog")).to_have_count(0)

        # 篩選改成「特殊值＞所有值」（不再濾掉），再顯示 Region 的篩選控制項並自訂
        page.locator('[data-drop="filters"] .pill').click()
        menu(page, "編輯篩選…")
        dialog = page.locator(".filter-dialog")
        dialog.get_by_role("tab", name="特殊值").click()
        dialog.get_by_label("所有值").check()
        confirm(page, ".filter-dialog")
        saved = sheet()["filters"][0]
        assert (saved["min"], saved["max"], saved["include_null"], saved["only_null"]) == (
            None,
            None,
            True,
            False,
        )
        page.locator('.data-pane [data-field="f_region"]').drag_to(
            page.locator('[data-drop="filters"]')
        )
        page.wait_for_selector(".filter-dialog .value-list li")
        confirm(page, ".filter-dialog")
        region_pill = page.locator('[data-drop="filters"] .pill', has_text="Region")
        region_pill.click()
        menu(page, "顯示篩選")
        settle(page)
        region_pill.click()
        menu(page, "自訂", "顯示套用按鈕")
        settle(page)
        region_pill.click()
        menu(page, "自訂", "顯示「全部」值")
        settle(page)
        control = next(c for c in sheet()["controls"] if c["field"]["field"] == "f_region")
        assert control["options"]["show_apply"] is True and control["options"]["show_all"] is False

        # 顯示遺漏的值與包含在工具提示中：欄換成月份（日期的部分）
        page.locator('.data-pane [data-field="f_order_date"]').drag_to(
            page.locator('.shelf[data-shelf="rows"] .shelf-drop')
        )
        settle(page)
        date_pill = page.locator('.shelf[data-shelf="rows"] .pill').last
        date_pill.click()
        menu(page, "顯示遺漏的值")
        settle(page)
        date_pill.click()
        menu(page, "包含在工具提示中")
        settle(page)
        saved = sheet()
        assert [u["field"] for u in saved["show_missing"]] == ["f_order_date"]
        assert [u["field"] for u in saved["tooltip_excluded"]] == ["f_order_date"]
        # 膠囊移走之後設定一併拿掉
        date_pill.click()
        menu(page, "移除")
        settle(page)
        saved = sheet()
        assert saved["show_missing"] == [] and saved["tooltip_excluded"] == []

        # 檢視器：套用按鈕——勾選之後按「套用」才篩選；沒有「（全部）」的選項
        page.goto(f"{engine}/w/demo/s/ws_1")
        control_box = page.locator(
            '.single-sheet .sheet-controls fieldset[data-control="multi_list"]'
        )
        control_box.locator("label", has_text="East").wait_for()
        assert control_box.locator("label", has_text="（全部）").count() == 0
        apply = control_box.get_by_role("button", name="套用")
        expect(apply).to_be_disabled()
        control_box.locator("label", has_text="East").locator("input").uncheck()
        expect(apply).to_be_enabled()
        with page.expect_response(
            lambda r: r.url.endswith("/worksheets/ws_1/render") and r.request.method == "POST"
        ) as rendered:
            apply.click()
        posted = rendered.value.request.post_data_json
        assert posted is not None
        chosen = posted["filters"]["f_region"]
        assert chosen["mode"] == "include" and "East" not in chosen["values"]
        assert len(chosen["values"]) == 3
        assert errors == []
        browser.close()


def test_toolbar_sort_manual_drag_hidden_sheet_and_crosstab(engine: str) -> None:
    """工具列的排序、手動排序清單的拖曳、隱藏工作表與分頁顏色、複製成交叉資料表。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def menu(page: Page, *labels: str) -> None:
        for label in labels[:-1]:
            page.locator(
                ".popup-menu li[role=menuitem] > .menu-label", has_text=label
            ).first.hover()
        page.locator(
            ".popup-menu li[role=menuitem] > .menu-label", has_text=labels[-1]
        ).first.click()

    def sheet(worksheet: str = "ws_1") -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{worksheet}")["worksheet"]

    def regions(worksheet: str = "ws_1") -> list[str]:
        response = call(engine, f"/api/workbooks/demo/worksheets/{worksheet}/render", {})
        assert response["unsupported"] is None, response
        return [row[0] for row in response["table"]["rows"]]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1700, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        for field in ("f_region", "f_sales"):
            page.locator(f'.data-pane [data-field="{field}"]').dblclick()
            settle(page)
        natural = regions()
        assert natural == sorted(natural)

        # 工具列的排序：Region 依 SUM(Sales) 由大到小、由小到大，再清除
        tool(page, "遞減排序").click()
        settle(page)
        sort = sheet()["sorts"][0]
        assert (sort["field"], sort["kind"], sort["order"]) == ("f_region", "by_field", "desc")
        assert sort["by"]["field"] == "f_sales" and sort["nested"] is False
        descending = regions()
        tool(page, "遞增排序").click()
        settle(page)
        assert regions() == descending[::-1]
        menubar(page, "工作表", "清除", "排序")
        settle(page)
        assert sheet()["sorts"] == [] and regions() == natural

        # 手動排序：在清單中把最後一個成員拖到最前面
        page.locator('.shelf[data-shelf="rows"] .pill').click()
        menu(page, "排序", "排序…")
        dialog = page.locator(".sort-dialog")
        dialog.get_by_label("排序依據").select_option("manual")
        items = dialog.locator(".manual-sort .value-list li")
        items.nth(len(natural) - 1).wait_for()
        items.nth(len(natural) - 1).drag_to(items.nth(0))
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)
        expected = [natural[-1], *natural[:-1]]
        assert sheet()["sorts"][0]["manual_values"] == expected and regions() == expected

        # 隱藏工作表與分頁顏色
        tab_menu(page, current_tab(page), "隱藏工作表")
        settle(page)
        tab_menu(page, current_tab(page), "分頁顏色…")
        color = page.get_by_role("dialog", name="分頁顏色")
        color.get_by_label("分頁顏色").fill("#ff8800")
        color.get_by_role("button", name="確定").click()
        settle(page)
        page.wait_for_timeout(700)
        saved = sheet()
        assert (saved["hidden"], saved["tab_color"]) == (True, "#ff8800")
        # 分頁列：隱藏的工作表以斜體標示，提示文字註明（隱藏）
        expect(current_tab(page)).to_have_class(re.compile(r"\bhidden-sheet\b"))
        assert "（隱藏）" in (current_tab(page).get_attribute("title") or "")

        # 複製成交叉資料表：開啟新的工作表（文字表）
        menubar(page, "工作表", "複製成交叉資料表")
        page.wait_for_url(re.compile(r"/w/demo/edit/ws_1_"))
        settle(page)
        copy_id = page.url.rsplit("/", 1)[-1].split("?")[0]
        copy = sheet(copy_id)
        assert copy["marks"]["type"] == "text" and copy["hidden"] is False
        assert [u["field"] for u in copy["rows"]] == ["f_region"]
        assert [u["field"] for u in copy["marks"]["label"]] == ["f_sales"]
        assert regions(copy_id) == expected  # 排序照原樣
        assert errors == []
        browser.close()


def test_tooltip_options_describe_models_annotations_format_and_page_history(engine: str) -> None:
    """工具提示的選項（暫留時、命令按鈕）、描述趨勢模型與描述叢集（存成群組）、拖曳註解、下拉線的值標籤、
    摘要卡、分隔線的細部與清除格式、頁面的顯示歷程，以及檢視器的簡報模式。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]

    def render() -> dict[str, Any]:
        response = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})
        assert response["unsupported"] is None, response
        return response

    def layers() -> list[str]:
        """圖中所有圖層的名稱（含巢狀的圖層）。"""
        names: list[str] = []

        def visit(node: dict[str, Any]) -> None:
            for layer in node.get("layer", []):
                names.append(str(layer.get("name", "")))
                visit(layer)

        visit(render()["spec"])
        return names

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/ws_1",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()

    def confirm(page: Page, dialog: str) -> None:
        page.locator(dialog).get_by_role("button", name="確定").click()
        expect(page.locator(dialog)).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)

    def use(field: str, **extra: Any) -> dict[str, Any]:
        base = {"field": field, "agg": None, "date": None, "continuous": False, "table_calc": None}
        return {**base, **extra}

    sales = use("f_sales", agg="sum", continuous=True)
    profit = use("f_profit", agg="sum", continuous=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        # 散佈圖：業績對利潤、每個子類別一個圓點，有趨勢線與一個點的註解
        base = sheet()
        base.update(columns=[sales], rows=[profit])
        base["marks"].update(type="circle", detail=[use("f_sub_category")], trend={"fit": "linear"})
        base["annotations"] = [
            {
                "kind": "point",
                "text": [{"text": "注意這裡"}],
                "point": {"x": 100000, "x_axis": sales, "y": 10000, "y_axis": profit},
                "offset": [0, 0],
            }
        ]
        put(base)
        page.reload()
        settle(page)

        # 工具提示：暫留時顯示、不包含命令按鈕
        page.locator(".mark-buttons").get_by_role("button", name="工具提示…").click()
        dialog = page.locator(".tooltip-dialog")
        dialog.get_by_label("工具提示出現的方式").select_option("hover")
        dialog.get_by_label("包含命令按鈕").uncheck()
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)
        saved = sheet()
        assert (saved["tooltip_mode"], saved["tooltip_commands"], saved["tooltip_select"]) == (
            "hover",
            False,
            True,
        )
        assert render()["spec"]["usermeta"]["tooltip"]["mode"] == "hover"

        # 描述趨勢模型：公式、R 平方與 P 值
        side_tab(page, "分析")
        page.get_by_role("button", name="描述趨勢模型…").click()
        described = page.locator('.describe-dialog[data-describe="trend"]')
        expect(described.locator("table.describe-table[data-trend]")).to_have_count(1)
        expect(described).to_contain_text("R 平方")
        expect(described).to_contain_text("建立模型的觀測數")
        # 各係數：斜率（X）在前、截距在後，各有值、標準誤、t 值與 P 值；數字與回應中的模型相同
        terms = described.locator("table[data-trend-terms] tbody tr")
        expect(terms).to_have_count(2)
        assert [row.locator("td").first.inner_text() for row in terms.all()] == ["X", "截距"]
        (model,) = render()["spec"]["usermeta"]["trends"]
        slope = model["coefficients"][0]
        assert slope["power"] == 1 and slope["standard_error"] > 0
        assert slope["t"] == pytest.approx(slope["value"] / slope["standard_error"])
        assert slope["p"] == pytest.approx(model["p"])  # 一個自變數：斜率的 t 檢定即模型的 F 檢定
        cells = [c.replace(",", "") for c in terms.first.locator("td").all_inner_texts()]
        assert float(cells[1]) == pytest.approx(slope["value"], rel=1e-4)
        assert float(cells[2]) == pytest.approx(slope["standard_error"], rel=1e-4)
        assert float(cells[3]) == pytest.approx(slope["t"], rel=1e-4)
        described.get_by_role("button", name="關閉").click()

        # 叢集：兩群；描述叢集並存成群組
        page.get_by_role("button", name="＋ 叢集…").click()
        cluster = page.locator(".cluster-dialog")
        cluster.get_by_label("自動決定叢集數").uncheck()
        cluster.get_by_role("spinbutton", name="叢集數").fill("2")
        confirm(page, ".cluster-dialog")
        assert sheet()["cluster"]["count"] == 2
        page.get_by_role("button", name="描述叢集…").click()
        described = page.locator('.describe-dialog[data-describe="cluster"]')
        expect(described).to_contain_text("各叢集的中心")
        expect(described.locator("table.describe-table").nth(1).locator("tbody tr")).to_have_count(
            2
        )
        described.get_by_role("button", name="存成群組").click()
        expect(page.locator(".toast", has_text="已建立群組").first).to_be_visible()
        described.get_by_role("button", name="關閉").click()
        catalog = call(engine, "/api/edit/demo/catalog")["datasources"][0]["fields"]
        group = next(f for f in catalog if f["caption"] == "Sub-Category（叢集）")
        assert group["role"] == "dimension"

        # 拖曳註解的文字：文字相對於點的位置跟著改變
        assert "__deco_annotation0" in layers()
        note = page.locator('.editor .worksheet g[class*="__deco_annotation0"] text')
        note.first.wait_for()
        box = note.first.bounding_box()
        assert box is not None
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 20, y - 15, steps=4)
        page.mouse.move(x + 40, y - 30, steps=4)
        page.mouse.up()
        page.wait_for_timeout(300)
        settle(page)
        assert sheet()["annotations"][0]["offset"] == [40, -30]

        # 下拉線與座標軸上的值標籤
        page.get_by_label("下拉線", exact=True).select_option("always")
        settle(page)
        page.get_by_label("下拉線的值標籤").click()
        expect(page.get_by_label("下拉線的值標籤")).to_be_checked()
        settle(page)
        assert sheet()["drop_lines"]["labels"] is True
        assert any(name.startswith("__deco_drop") and name.endswith("_label") for name in layers())
        side_tab(page, "資料")

        # 摘要卡：各量值的總和、平均等
        menubar(page, "工作表", "顯示摘要")  # 存檔之後才勾選
        settle(page)
        expect(menu_item(page, "工作表", "顯示摘要")).to_have_attribute("aria-checked", "true")
        page.keyboard.press("Escape")
        assert sheet()["summary_shown"] is True
        card = page.locator(".editor .summary-card")
        expect(card.locator("tbody tr")).to_have_count(2)
        expect(card).to_contain_text("中位數")
        # 更多：選用的統計量（樣本標準差、四分位數、樣本偏度與超額峰度），與這裡依定義算出的相同
        expect(card.locator("[data-stat]")).to_have_count(0)
        card.get_by_role("button", name="更多").click()
        expect(card.locator("thead th")).to_have_count(11)
        table = render()["table"]
        for key in ("sum(f_sales)", "sum(f_profit)"):
            at = table["keys"].index(key)
            values = sorted(float(row[at]) for row in table["raw"] if row[at] not in ("", None))
            n, mean, deviation = len(values), statistics.fmean(values), statistics.stdev(values)
            first, _, third = statistics.quantiles(values, n=4, method="inclusive")
            z = [(v - mean) / deviation for v in values]
            skewness = n / ((n - 1) * (n - 2)) * sum(v**3 for v in z)
            kurtosis = n * (n + 1) / ((n - 1) * (n - 2) * (n - 3)) * sum(v**4 for v in z) - 3 * (
                n - 1
            ) ** 2 / ((n - 2) * (n - 3))
            row = card.locator(f'tbody tr[data-measure="{key}"]')
            shown = {
                name: float(row.locator(f'[data-stat="{name}"]').inner_text().replace(",", ""))
                for name in (
                    "deviation",
                    "first-quartile",
                    "third-quartile",
                    "skewness",
                    "kurtosis",
                )
            }
            assert n > 4 and shown == {
                "deviation": pytest.approx(deviation, abs=0.006),
                "first-quartile": pytest.approx(first, abs=0.006),
                "third-quartile": pytest.approx(third, abs=0.006),
                "skewness": pytest.approx(skewness, abs=0.006),
                "kurtosis": pytest.approx(kurtosis, abs=0.006),
            }
        card.get_by_role("button", name="較少").click()
        expect(card.locator("[data-stat]")).to_have_count(0)

        # 格式：分隔線的細部；清除格式後回到預設
        menubar(page, "格式", "格式…")
        page.get_by_label("列分隔線的線寬").fill("3")
        page.get_by_label("列分隔線的層級").fill("2")
        # 線的樣式：分隔線的虛線，格線與座標軸線的線寬與樣式
        page.get_by_label("列分隔線的樣式").select_option("dashed")
        page.get_by_label("欄格線的線寬").fill("2")
        page.get_by_label("欄格線的樣式").select_option("dotted")
        page.get_by_label("列座標軸線的樣式").select_option("dashed")
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        page.wait_for_timeout(300)
        settle(page)
        divider = sheet()["row_style"]["divider"]
        assert (divider["width"], divider["level"], divider["pattern"]) == (3, 2, "dashed")
        assert sheet()["row_style"]["header_divider"]["width"] == 3
        assert sheet()["column_style"]["grid_line"] == {
            "color": None,
            "width": 2,
            "pattern": "dotted",
        }
        assert sheet()["row_style"]["axis_line"] == {"color": None, "width": 1, "pattern": "dashed"}
        assert (sheet()["row_style"]["grid_line"], sheet()["column_style"]["axis_line"]) == (
            None,
            None,
        )

        def axes() -> tuple[dict[str, Any], dict[str, Any]]:
            """散佈圖的橫軸與縱軸（主要圖層的 encoding）。"""

            def find(node: dict[str, Any]) -> dict[str, Any] | None:
                encoding = node.get("encoding") or {}
                if "axis" in encoding.get("x", {}) and "axis" in encoding.get("y", {}):
                    return encoding
                for layer in node.get("layer", []):
                    found = find(layer)
                    if found is not None:
                        return found
                return None

            encoding = find(render()["spec"])
            assert encoding is not None
            return encoding["x"]["axis"], encoding["y"]["axis"]

        x_axis, y_axis = axes()
        # 橫軸（欄）的格線：2 像素的點線（0 的那一條仍是零線）；縱軸（列）的座標軸線是虛線
        assert (x_axis["gridWidth"]["value"], x_axis["gridDash"]["value"]) == (2, [2, 1])
        assert y_axis["domainDash"] == [4, 2] and y_axis["gridDash"]["value"] == []
        # 畫面上的格線也是點線
        dashes = page.evaluate(
            """() => [...document.querySelectorAll('.editor .worksheet .role-axis-grid line')]
                 .map((line) => line.getAttribute('stroke-dasharray') || '')"""
        )
        assert "2,1" in dashes
        menubar(page, "格式", "清除格式")
        page.wait_for_timeout(300)
        settle(page)
        divider = sheet()["row_style"]["divider"]
        assert (divider["width"], divider["level"], divider["pattern"]) == (1, None, "solid")
        assert sheet()["column_style"]["grid_line"] is None
        assert sheet()["row_style"]["axis_line"] is None
        x_axis, y_axis = axes()
        assert x_axis["gridDash"]["value"] == [] and "domainDash" not in y_axis

        # 頁面的顯示歷程：第二頁時畫出前一頁的標記與軌跡
        page.locator('.data-pane [data-field="f_order_date"]').drag_to(
            page.locator('[data-drop="pages"]')
        )
        control = page.locator(".editor [data-page-control]")
        control.wait_for()
        page.wait_for_function(
            "() => document.querySelector('.editor [data-page-control] select')?.value === '0'"
        )
        settle(page)
        page.get_by_label("顯示歷程").select_option("both")
        settle(page)
        assert sheet()["page_history"]["show"] == "both"
        assert not any(name.startswith("__deco_history") for name in layers())  # 第一頁沒有歷程
        control.get_by_role("button", name="下一頁").click()
        settle(page)
        names = layers()
        assert "__deco_history" in names and "__deco_trail" in names
        page.get_by_label("顯示歷程").select_option("")
        settle(page)
        assert sheet()["page_history"] is None

        # 檢視器的簡報模式：只留檢視，可以切換前後一張，Esc 結束
        page.goto(f"{engine}/w/demo/s/ws_1")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        page.get_by_role("button", name="簡報模式").click()
        expect(page.locator(".page.presenting")).to_have_count(1)
        expect(page.locator(".page > .toolbar")).to_be_hidden()
        bar = page.locator(".presentation-bar")
        expect(bar).to_be_visible()
        before = page.url
        bar.get_by_role("button", name="下一張").click()
        page.wait_for_function("(url) => window.location.href !== url", arg=before)
        expect(page.locator(".page.presenting")).to_have_count(1)
        page.keyboard.press("Escape")
        expect(page.locator(".page.presenting")).to_have_count(0)
        expect(page.locator(".page > .toolbar")).to_be_visible()
        assert errors == []
        browser.close()


def test_import_worksheets_dialog(engine: str, tmp_path: Path) -> None:
    """匯入工作表（匯入活頁簿、書籤、在活頁簿之間複製貼上）：從工作區中開啟的另一份報表選取工作表匯入，
    匯入後直接開啟該工作表，數字與來源報表相同。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    features = ROOT / "fixtures" / "superstore_features" / "superstore_features.twbx"
    copied = tmp_path / "features.twbx"
    shutil.copy(features, copied)
    source = call(engine, "/api/desktop/open", {"path": str(copied)})["document"]["id"]
    listing = {w["id"]: w for w in call(engine, "/api/workbooks")}
    first = listing[source]["worksheets"][0]
    expected = call(engine, f"/api/workbooks/{source}/worksheets/{first['id']}/render", {})
    before = call(engine, "/api/edit/demo/outline")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        menubar(page, "檔案", "匯入工作表…")
        dialog = page.locator(".import-dialog")
        dialog.get_by_label("來源報表").select_option(source)
        submit = dialog.get_by_role("button", name="匯入", exact=True)
        expect(submit).to_be_disabled()  # 還沒有選取工作表
        dialog.get_by_role("list", name="要匯入的工作表").get_by_label(
            first["name"], exact=True
        ).check()
        submit.click()
        expect(dialog).to_have_count(0)
        after = call(engine, "/api/edit/demo/outline")
        added = [
            w for w in after["worksheets"] if w["id"] not in {x["id"] for x in before["worksheets"]}
        ]
        assert len(added) == 1
        page.wait_for_url(f"{engine}/w/demo/edit/{added[0]['id']}")
        settle(page)
        data = call(engine, f"/api/workbooks/demo/worksheets/{added[0]['id']}/render", {})
        assert data["unsupported"] is None
        assert data["table"]["raw"] == expected["table"]["raw"]
        assert errors == []
        browser.close()


def test_custom_image_shapes_in_editor_and_viewer(engine: str, tmp_path: Path) -> None:
    """自訂的圖片形狀：在「形狀…」把圖片加入調色盤、指定給成員（指定調色盤），圖表上畫出圖片（色彩上有欄位時
    套用各標記的顏色）、形狀的圖例由檢視器畫；圖片上可以點選標記；形狀上沒有欄位時是所有標記的形狀。"""
    import numpy as np
    from playwright.sync_api import Page, expect, sync_playwright

    from tmig.render.density import _png

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def picture(name: str) -> Path:
        # 透明的背景上一個黑色的方塊（套用顏色後方塊是那個顏色、背景仍然透明）
        pixels = np.zeros((16, 16, 4), dtype=np.uint8)
        pixels[4:12, 4:12] = (0, 0, 0, 255)
        path = tmp_path / name
        path.write_bytes(_png(pixels))
        return path

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Icons"})["worksheet"]
    ws = created["id"]

    def sheet() -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{ws}")["worksheet"]

    def measure(field: str) -> dict[str, Any]:
        return {"field": field, "agg": "sum", "date": None, "continuous": True, "table_calc": None}

    region = {
        "field": "f_region",
        "agg": None,
        "date": None,
        "continuous": False,
        "table_calc": None,
    }
    body = sheet()
    body.update(rows=[measure("f_sales")], columns=[measure("f_profit")])
    body["marks"].update(type="shape", shape=region, color=region, detail=[region])
    request = urllib.request.Request(
        f"{engine}/api/edit/demo/worksheets/{ws}",
        data=json.dumps({"worksheet": body}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200

    images = "svg g.mark-image image"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{ws}")
        settle(page)
        page.locator(".mark-buttons").get_by_role("button", name="形狀…").click()
        dialog = page.locator(".shapes-dialog")
        dialog.get_by_label("新調色盤的名稱").fill("KPI")
        dialog.get_by_label("形狀的圖片檔").set_input_files(
            [str(picture("up.png")), str(picture("down.png"))]
        )
        expect(dialog.locator(".shape-grid img")).to_have_count(2)
        assert call(engine, "/api/edit/demo/shapes")["palettes"] == [
            {"name": "KPI", "images": ["Shapes/KPI/down.png", "Shapes/KPI/up.png"]}
        ]
        # 指定調色盤：圖片依序指定給各成員；West 再改回內建的菱形
        dialog.get_by_role("button", name="指定調色盤").click()
        expect(dialog.get_by_label("East的形狀")).to_have_value("i:Shapes/KPI/up.png")
        dialog.get_by_label("West的形狀").select_option("s:diamond")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)
        settle(page)
        assigned = {
            a["value"]: (a["shape"], a["image"]) for a in sheet()["marks"]["shape_assignments"]
        }
        assert assigned == {
            "Central": ("circle", "Shapes/KPI/down.png"),
            "East": ("circle", "Shapes/KPI/up.png"),
            "South": ("circle", "Shapes/KPI/down.png"),
            "West": ("diamond", None),
        }
        # 圖表：三個成員畫圖片、West 畫菱形；圖片是 data URL（匯出時不必另外載入）
        chart = page.locator(".editor-canvas .chart")
        expect(chart.locator(images)).to_have_count(3)
        spec = call(engine, f"/api/workbooks/demo/worksheets/{ws}/render", {})["spec"]
        colors = dict(
            zip(
                spec["encoding"]["color"]["scale"]["domain"],
                spec["encoding"]["color"]["scale"]["range"],
                strict=True,
            )
        )
        # 色彩上有欄位：圖片不透明的部分換成各標記的顏色，透明的部分不變
        drawn = page.evaluate(
            """async () => {
              const nodes = Array.from(document.querySelectorAll('.editor-canvas .chart svg g.mark-image image'));
              const out = [];
              for (const node of nodes) {
                const href = node.getAttribute('href') || node.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
                const image = new Image();
                await new Promise((resolve) => { image.onload = resolve; image.src = href; });
                const canvas = document.createElement('canvas');
                canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
                const context = canvas.getContext('2d');
                context.drawImage(image, 0, 0);
                const center = Array.from(context.getImageData(8, 8, 1, 1).data);
                const corner = Array.from(context.getImageData(0, 0, 1, 1).data);
                out.push({data: href.startsWith('data:image/png'), center, corner, width: node.getAttribute('width')});
              }
              return out;
            }"""
        )
        assert len(drawn) == 3 and all(item["data"] for item in drawn)
        assert all(item["corner"][3] == 0 for item in drawn)
        wanted = {
            tuple(int(colors[name][i : i + 2], 16) for i in (1, 3, 5))
            for name in ("Central", "East", "South")
        }
        assert {tuple(item["center"][:3]) for item in drawn} == wanted
        assert all(item["center"][3] == 255 for item in drawn)
        # 圖片的邊長：預設大小是 30 像素（IMAGE_DIAMETER）
        assert {item["width"] for item in drawn} == {"30"}
        # 形狀的圖例：有圖片時由檢視器畫（三張圖片與一個內建的形狀）
        legend = page.locator(".editor-canvas .shape-legend")
        expect(legend.locator("img.shape-swatch")).to_have_count(3)
        expect(legend.locator("svg.shape-swatch")).to_have_count(1)
        expect(legend.locator(".legend-item")).to_have_text(["Central", "East", "South", "West"])
        # 色彩與形狀是同一個欄位：合併的圖例，圖片與內建的形狀都帶著成員的顏色
        expect(legend.locator('img.shape-swatch[data-tinted="true"]')).to_have_count(3)
        west = legend.locator("svg.shape-swatch path").evaluate("e => getComputedStyle(e).fill")
        assert west == "rgb({}, {}, {})".format(
            *(int(colors["West"][i : i + 2], 16) for i in (1, 3, 5))
        )

        # 檢視器：點圖片選到標記（看不見的點接收事件），工具提示釘住
        page.goto(f"{engine}/w/demo/s/{ws}")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        viewer = page.locator(".single-sheet .chart")
        expect(viewer.locator(images)).to_have_count(3)
        box = viewer.locator(images).first.bounding_box()
        assert box is not None
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        pinned = page.locator(".viz-tooltip.pinned")
        pinned.wait_for()
        assert any(name in pinned.inner_text() for name in ("Central", "East", "South"))
        page.keyboard.press("Escape")

        # dashboard 的形狀圖例區塊：各成員的形狀（圖片與內建的形狀）
        board = call(engine, "/api/edit/demo/dashboards", {"name": "Icons Board"})["dashboard"]
        board["zones"][0]["children"][0]["children"] = [
            {"id": "z3", "kind": "worksheet", "x": 4, "y": 4, "w": 700, "h": 600, "worksheet": ws,
             "source_ref": "/editor"},
            {"id": "z4", "kind": "legend", "x": 704, "y": 4, "w": 292, "h": 600, "worksheet": ws,
             "legend": "shape", "source_ref": "/editor"},
        ]  # fmt: skip
        update = urllib.request.Request(
            f"{engine}/api/edit/demo/dashboards/{board['id']}",
            data=json.dumps({"dashboard": board}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(update, timeout=60) as response:
            assert response.status == 200
        page.goto(f"{engine}/w/demo/d/{board['id']}")
        zone_legend = page.locator(".dashboard .shape-legend")
        expect(zone_legend.locator(".legend-item")).to_have_text(
            ["Central", "East", "South", "West"], timeout=30000
        )
        expect(zone_legend.locator("img.shape-swatch")).to_have_count(3)
        expect(page.locator(".dashboard .chart").locator(images)).to_have_count(3)
        assert (
            page.locator(".dashboard .worksheet-body .shape-legend").count() == 0
        )  # 圖表裡不再畫一次

        # 形狀上沒有欄位：所有標記的形狀（圖片），再改成內建的叉號
        page.goto(f"{engine}/w/demo/edit/{ws}")
        settle(page)
        page.locator('.mark-pills [data-slot="shape"] .pill').first.click()
        page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text="移除").first.click()
        settle(page)
        assert sheet()["marks"]["shape"] is None
        page.locator(".mark-buttons").get_by_role("button", name="形狀…").click()
        dialog.get_by_label("所有標記的形狀").select_option("i:Shapes/KPI/up.png")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)
        settle(page)
        assert sheet()["marks"]["mark_shape"] == {"shape": "circle", "image": "Shapes/KPI/up.png"}
        expect(chart.locator(images)).to_have_count(4)
        page.locator(".mark-buttons").get_by_role("button", name="形狀…").click()
        dialog.get_by_label("所有標記的形狀").select_option("s:times")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)
        settle(page)
        assert sheet()["marks"]["mark_shape"] == {"shape": "times", "image": None}
        expect(chart.locator(images)).to_have_count(0)
        assert errors == []
        browser.close()


def test_null_indicator(engine: str) -> None:
    """「N 個 Null」指示器：連續座標軸上的值是 Null 的標記沒有畫出時，檢視的右下角顯示個數；編輯器中點一下
    可以改成在預設位置顯示資料，或隱藏指示器。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def use(field: str, **extra: Any) -> dict[str, Any]:
        base = {"field": field, "agg": None, "date": None, "continuous": False, "table_calc": None}
        return {**base, **extra}

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{body['id']}",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()

    # 業績不到各區平均的區沒有值（Null）：各區一個長條，Null 的區沒有畫出
    made = call(
        engine,
        "/api/edit/demo/calcs",
        {
            "datasource": "ds_orders",
            "caption": "大區的業績",
            "formula": "IF SUM([Sales]) > 500000 THEN SUM([Sales]) END",
        },
    )["field"]
    created = call(engine, "/api/edit/demo/worksheets", {"name": "Nulls"})["worksheet"]
    sheet = {**created, "rows": [use("f_region")], "columns": [use(made["id"], continuous=True)]}
    put(sheet)

    def current() -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]

    def render() -> dict[str, Any]:
        return call(engine, f"/api/workbooks/demo/worksheets/{created['id']}/render", {})

    table = render()["table"]
    at = table["keys"].index(made["id"])
    expected = sum(1 for row in table["raw"] if row[at] in ("", None))
    assert 0 < expected < len(table["raw"]), table["raw"]
    assert render()["spec"]["usermeta"]["nulls"] == expected

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 850})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        # 檢視器：只顯示個數，沒有選單
        page.goto(f"{engine}/w/demo/s/{created['id']}")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        badge = page.locator(".single-sheet .null-indicator")
        expect(badge).to_have_text(f"{expected} 個 Null")
        expect(badge.locator("button")).to_have_count(0)
        # 編輯器：點指示器 →「在預設位置顯示資料」，Null 的標記畫在 0，指示器消失
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        settle(page)
        badge = page.locator(".editor .null-indicator")
        expect(badge).to_have_text(f"{expected} 個 Null")
        badge.get_by_role("button").click()
        badge.get_by_role("menuitem", name="在預設位置顯示資料").click()
        page.wait_for_timeout(300)
        settle(page)
        assert current()["null_marks"] == "zero"
        expect(page.locator(".editor .null-indicator")).to_have_count(0)
        assert "nulls" not in render()["spec"]["usermeta"]
        # 格式對話框改回隱藏：指示器回來；再從指示器的選單隱藏它
        menubar(page, "格式", "格式…")
        page.get_by_label("Null 值的標記").select_option("")
        expect(page.get_by_label("Null 值的指示器")).to_be_checked()
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        page.wait_for_timeout(300)
        settle(page)
        badge = page.locator(".editor .null-indicator")
        expect(badge).to_have_text(f"{expected} 個 Null")
        badge.get_by_role("button").click()
        badge.get_by_role("menuitem", name="隱藏指示器").click()
        page.wait_for_timeout(300)
        settle(page)
        saved = current()
        assert (saved["null_marks"], saved["null_indicator"]) == (None, False)
        expect(page.locator(".editor .null-indicator")).to_have_count(0)
        assert errors == []
        browser.close()


def test_render_notes_are_shown(engine: str) -> None:
    """畫圖的附註（render 回應的 notes）原本在編輯器與檢視器都不顯示：預測估計不出來時，圖上沒有預測、也看不到
    原因。現在檢視的左下角有一個標示，點了列出各則。

    - 預測估計不出來（只有兩年的資料、預設忽略最後一期）：編輯器列出原因與計算方式的說明，檢視器只列出原因
    - 預測估計得出來：只剩計算方式的說明——編輯器列出來，檢視器不標示（圖上沒有少東西）"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{body['id']}",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()

    def forecast(unit: str) -> dict[str, Any]:
        """Monthly Profit 的時間改成 unit、加上預測；回傳 render 的回應。"""
        body = call(engine, "/api/edit/demo/worksheets/ws_monthly_profit")["worksheet"]
        body["columns"][0]["date"]["unit"] = unit
        body["forecast"] = {"measures": [{"field": "f_profit", "agg": "sum"}]}
        put(body)
        return call(engine, "/api/workbooks/demo/worksheets/ws_monthly_profit/render", {})

    caveat = "預測的估計值以平台的指數平滑計算（與 Tableau 相同的模型），可能與 Tableau 有些微差異"
    reason = "預測：扣掉忽略的最後 1 期之後，資料不到兩期，估計不出來"
    # 年：只有兩年的資料，估計不出來——回應的附註有原因，warnings 只有原因（不含計算方式的說明）
    failed = forecast("year")
    assert failed["rows"] == 2 and failed["notes"] == [caveat, reason]
    assert failed["warnings"] == [reason]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        notes = page.locator(".notes-list li")

        # 編輯器：標示「1 項未能呈現」，點了列出原因（在前）與計算方式的說明；按 Esc 收起來
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        badge = page.locator(".editor .notes-indicator")
        expect(badge).to_have_text("⚠ 1 項未能呈現")
        expect(notes).to_have_count(0)
        badge.get_by_role("button").click()
        assert notes.all_inner_texts() == [reason, caveat]
        assert notes.evaluate_all("items => items.map(i => i.dataset.kind)") == ["warning", "info"]
        page.keyboard.press("Escape")
        expect(notes).to_have_count(0)

        # 檢視器（單張工作表）：同樣的標示，只列出原因；點別的地方收起來
        page.goto(f"{engine}/w/demo/s/ws_monthly_profit")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        badge = page.locator(".single-sheet .notes-indicator")
        expect(badge).to_have_text("⚠ 1 項未能呈現")
        badge.get_by_role("button").click()
        assert notes.all_inner_texts() == [reason]
        page.locator(".toolbar .wb").click()
        expect(notes).to_have_count(0)

        # dashboard 上的工作表：標示在那一格，清單浮在畫面上、不被區塊裁切
        page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        page.wait_for_function(
            "document.querySelectorAll('.dashboard .worksheet[data-loading=\"false\"]').length === 4"
        )
        expect(page.locator(".dashboard .notes-indicator")).to_have_count(1)
        badge = page.locator(".dashboard [data-worksheet='ws_monthly_profit'] .notes-indicator")
        badge.get_by_role("button").click()
        expect(notes).to_have_text([reason])
        shown = page.locator(".notes-list").bounding_box()
        zone = page.locator(".dashboard [data-worksheet='ws_monthly_profit']").bounding_box()
        assert shown is not None and zone is not None
        assert shown["width"] > 200 and shown["y"] >= 0 and shown["x"] >= 0
        assert shown["y"] + shown["height"] <= zone["y"] + zone["height"]
        assert page.evaluate(
            """() => { const list = document.querySelector('.notes-list').getBoundingClientRect();
              const hit = document.elementFromPoint(list.left + 12, list.top + list.height / 2);
              return hit !== null && hit.closest('.notes-list') !== null; }"""
        )
        # 列印（匯出 PDF、訂閱）不印標示
        page.emulate_media(media="print")
        expect(badge).to_be_hidden()
        page.emulate_media(media="screen")

        # 月：估計得出來，只剩計算方式的說明——編輯器列出來，檢視器不標示
        fine = forecast("month")
        assert fine["rows"] > 12 and fine["notes"] == [caveat] and fine["warnings"] == []
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        badge = page.locator(".editor .notes-indicator")
        expect(badge).to_have_text("ⓘ 1 則附註")
        badge.get_by_role("button").click()
        assert notes.all_inner_texts() == [caveat]
        badge.get_by_role("button").click()
        expect(notes).to_have_count(0)
        page.goto(f"{engine}/w/demo/s/ws_monthly_profit")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        page.wait_for_selector(".single-sheet svg [class*='__deco_forecast']", state="attached")
        expect(page.locator(".notes-indicator")).to_have_count(0)
        assert errors == []
        browser.close()


def test_box_selection_and_escape_in_the_editor(engine: str) -> None:
    """編輯器的檢視上拖曳是框選（原本編輯器裡的拖曳只用來移動註解，檢視上不能框選）：方框碰到的標記都選取，
    選取之後的命令（只保留、排除、群組、建立集合）與點選時相同；按住 Ctrl 加選；按 Esc 取消選取（Tableau：
    Esc 取消選取）；按在註解上拖曳仍是移動註解，不是框選。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        page.wait_for_selector('.editor .chart[data-drawn="true"]')

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sub_category_profit")["worksheet"]

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{body['id']}",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()

    original = sheet()
    bars = ".editor-canvas svg path[aria-roledescription='bar']:not(.frozen *)"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)

        def rows() -> list[dict[str, Any]]:
            """畫面上由上到下的長條：子類別的名稱與位置。"""
            found = page.locator(bars).evaluate_all(
                """nodes => nodes.map(n => { const r = n.getBoundingClientRect();
                  return { label: n.getAttribute('aria-label'), left: r.left, right: r.right,
                           y: r.top + r.height / 2 }; })"""
            )
            for item in found:
                matched = re.search(r"Sub-Category: ([^;]+)", item["label"])
                assert matched is not None, item
                item["name"] = matched.group(1)
            return sorted(found, key=lambda item: item["y"])

        names = [item["name"] for item in rows()]
        assert len(names) >= 6 and len(set(names)) == len(names), names
        # 在第一個長條上加一個標記的註解（之後確認拖曳它是移動、不是框選）
        noted = {
            **original,
            "annotations": [
                {
                    "kind": "mark",
                    "text": [{"text": "看這裡"}],
                    "point": {"mark": {"f_sub_category": names[0]}},
                    "offset": [60, 0],
                }
            ],
        }
        put(noted)
        page.reload()
        settle(page)
        # 註解加上之後長條的順序可能不同（重新讀取）
        names = [item["name"] for item in rows()]

        def drag(first: int, last: int) -> None:
            """從第 first 個長條拖到第 last 個（由上到下的順序）：方框跨過 0 的那條線，中間的長條
            不論正負都碰得到。"""
            found = rows()
            zero = max(item["left"] for item in found)
            start = (zero - 4, found[first]["y"])
            end = (zero + 4, found[last]["y"])
            page.mouse.move(*start)
            page.mouse.down()
            page.mouse.move((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, steps=4)
            page.mouse.move(*end, steps=4)

        bar = page.locator(".selection-bar")
        assert page.locator(".editor-canvas .chart").get_attribute("data-selectable") == "true"

        # 從第 2 個長條拖到第 4 個：拖曳中畫出方框，放開後三個標記都選取
        drag(1, 3)
        expect(page.locator(".editor-canvas .select-overlay.passive rect")).to_have_count(1)
        page.mouse.up()
        expect(bar).to_contain_text("已選取 3 個標記")
        expect(page.locator(".editor-canvas .select-overlay")).to_have_count(0)
        # 只保留：加上這三個成員的篩選（與點選之後的「只保留」相同）；Ctrl+Z 復原
        bar.get_by_role("button", name="✓ 只保留").click()
        page.wait_for_timeout(300)
        settle(page)
        kept = [f for f in sheet()["filters"] if f not in original["filters"]]
        assert len(kept) == 1 and kept[0]["mode"] == "include", kept
        assert kept[0]["field"]["field"] == "f_sub_category"
        assert sorted(kept[0]["values"]) == sorted(names[1:4])
        expect(page.locator(bars)).to_have_count(3)
        page.keyboard.press("Control+z")
        expect(page.locator(bars)).to_have_count(len(names))
        settle(page)
        assert sheet()["filters"] == original["filters"]

        # 按住 Ctrl 再框選：加進原本的選取；排除：這些成員以外的留下
        drag(0, 1)
        page.mouse.up()
        expect(bar).to_contain_text("已選取 2 個標記")
        page.keyboard.down("Control")
        drag(4, 5)
        page.mouse.up()
        page.keyboard.up("Control")
        expect(bar).to_contain_text("已選取 4 個標記")
        bar.get_by_role("button", name="✕ 排除").click()
        page.wait_for_timeout(300)
        settle(page)
        excluded = [f for f in sheet()["filters"] if f not in original["filters"]]
        assert excluded[0]["mode"] == "exclude"
        assert sorted(excluded[0]["values"]) == sorted([*names[0:2], *names[4:6]])
        page.keyboard.press("Control+z")
        expect(page.locator(bars)).to_have_count(len(names))
        settle(page)

        # 按 Esc：取消選取（選取的命令列收起來）；框到空白處也是
        drag(2, 3)
        page.mouse.up()
        expect(bar).to_contain_text("已選取 2 個標記")
        page.keyboard.press("Escape")
        expect(bar).to_have_count(0)
        page.locator(bars).nth(2).click()
        expect(bar).to_contain_text("已選取 1 個標記")
        page.keyboard.press("Escape")
        expect(bar).to_have_count(0)

        # 群組、建立集合：對話框裡是框選的成員
        drag(1, 2)
        page.mouse.up()
        expect(bar).to_contain_text("已選取 2 個標記")
        bar.get_by_role("button", name="📎 群組…").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("建立群組")
        for name in names[1:3]:
            expect(dialog.locator(".group-list")).to_contain_text(name)
        # 對話框開著時按 Esc：關掉的是對話框，檢視上的選取還在
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.wait_for_timeout(200)
        expect(bar).to_contain_text("已選取 2 個標記")
        bar.get_by_role("button", name="◎ 建立集合…").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("集合")
        for name in names[1:3]:
            expect(dialog).to_contain_text(name)
        dialog.get_by_role("button", name="取消", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.keyboard.press("Escape")
        expect(bar).to_have_count(0)

        # 按在註解上拖曳：移動註解（文字相對於標記的位置改變），不是框選
        note = page.locator('.editor .worksheet g[class*="__deco_annotation0"] text')
        note.first.wait_for()
        box = note.first.bounding_box()
        assert box is not None
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 15, y + 10, steps=4)
        page.mouse.move(x + 30, y + 20, steps=4)
        expect(page.locator(".editor-canvas .select-overlay")).to_have_count(0)
        page.mouse.up()
        page.wait_for_timeout(300)
        settle(page)
        assert sheet()["annotations"][0]["offset"] == [90, 20]
        expect(bar).to_have_count(0)
        assert errors == []
        browser.close()


def test_cell_and_pane_borders(engine: str) -> None:
    """格式對話框的框線：儲存格的框線畫在每一列之間、窗格的框線是外框；存進工作表、畫面上的格線與外框跟著變，
    清除格式後回到沒有框線。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def use(field: str, **extra: Any) -> dict[str, Any]:
        base = {"field": field, "agg": None, "date": None, "continuous": False, "table_calc": None}
        return {**base, **extra}

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Borders"})["worksheet"]
    body = {
        **created,
        "rows": [use("f_region")],
        "columns": [use("f_sales", agg="sum", continuous=True)],
    }
    request = urllib.request.Request(
        f"{engine}/api/edit/demo/worksheets/{created['id']}",
        data=json.dumps({"worksheet": body}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(request, timeout=60).close()

    def current() -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]

    def spec() -> dict[str, Any]:
        response = call(engine, f"/api/workbooks/demo/worksheets/{created['id']}/render", {})
        assert response["unsupported"] is None, response
        return response["spec"]

    assert spec()["config"]["view"] == {"stroke": None}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        settle(page)
        menubar(page, "格式", "格式…")
        dialog = page.locator(".format-dialog")
        expect(dialog.get_by_label("儲存格框線的樣式")).to_have_count(0)  # 沒有勾選時不顯示細部
        dialog.get_by_label("儲存格的框線").check()
        dialog.get_by_label("儲存格框線的樣式").select_option("dotted")
        dialog.get_by_label("儲存格框線的顏色").fill("#ff0000")
        dialog.get_by_label("窗格的框線").check()
        dialog.get_by_label("窗格框線的線寬").fill("2")
        dialog.get_by_label("窗格框線的顏色").fill("#0000ff")
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        page.wait_for_timeout(300)
        settle(page)
        saved = current()
        assert saved["cell_border"] == {"pattern": "dotted", "width": 1, "color": "#ff0000"}
        assert saved["pane_border"] == {"pattern": "solid", "width": 2, "color": "#0000ff"}
        rendered = spec()
        # 外框是窗格的框線；各區之間是儲存格的框線（紅色的點線）
        assert rendered["config"]["view"] == {"stroke": "#0000ff", "strokeWidth": 2}

        def y_axis(node: dict[str, Any]) -> dict[str, Any] | None:
            axis = (node.get("encoding") or {}).get("y", {}).get("axis")
            if isinstance(axis, dict) and "gridOpacity" in axis:
                return axis
            for layer in node.get("layer", []):
                found = y_axis(layer)
                if found is not None:
                    return found
            return None

        axis = y_axis(rendered)
        assert axis is not None
        assert (axis["gridColor"], axis["gridWidth"], axis["gridDash"]) == ("#ff0000", 1, [2, 1])
        # 畫面上（主圖）：各區之間有 3 條看得見的紅色點線（頭尾兩條不畫），外框是藍色
        drawn = page.evaluate(
            """() => {
                 // 主圖（另有捲動時固定標題用的複本 .frozen）
                 const svg = document.querySelector('.editor .worksheet .frozen-host > svg')
                   ?? document.querySelector('.editor .worksheet svg');
                 const lines = [...svg.querySelectorAll('.role-axis-grid line')]
                   .filter((line) => line.getAttribute('stroke') === '#ff0000')
                   .map((line) => [line.getAttribute('stroke-dasharray'), line.getAttribute('opacity')]);
                 const frames = [...svg.querySelectorAll('path.background')]
                   .map((path) => [path.getAttribute('stroke'), path.getAttribute('stroke-width')])
                   .filter(([stroke]) => stroke);
                 return { lines, frames, charts: document.querySelectorAll('.editor .worksheet svg').length };
               }"""
        )
        visible = [dash for dash, opacity in drawn["lines"] if opacity != "0"]
        assert visible == ["2,1", "2,1", "2,1"], drawn
        assert len(drawn["lines"]) - len(visible) == 2, drawn
        assert drawn["frames"] == [["#0000ff", "2"]], drawn
        # 清除格式：回到沒有框線
        menubar(page, "格式", "清除格式")
        page.wait_for_timeout(300)
        settle(page)
        assert (current()["cell_border"], current()["pane_border"]) == (None, None)
        assert spec()["config"]["view"] == {"stroke": None}
        assert errors == []
        browser.close()


def test_text_segments_and_bundled_font(engine: str) -> None:
    """同一行內格式不同的文字（24pt 的數字接著 16pt 的單位）：兩段緊接著畫在同一條基線上，小的單位不會用
    大的字級畫；隨檢視器提供的 Roboto 字型在報表指定它時載入。"""
    from playwright.sync_api import sync_playwright

    created = call(engine, "/api/edit/demo/worksheets", {"name": "KPI"})["worksheet"]
    sales = {
        "field": "f_sales",
        "agg": "sum",
        "date": None,
        "continuous": True,
        "table_calc": None,
    }
    body = {
        **created,
        "fonts": {"worksheet": {"family": "Roboto"}},
        "marks": {
            **created["marks"],
            "type": "text",
            "label": [sales],
            "label_text": [
                {"use": sales, "style": {"bold": True, "size": 24.0}},
                {"text": "CM", "style": {"bold": True, "size": 16.0}},
            ],
        },
    }
    request = urllib.request.Request(
        f"{engine}/api/edit/demo/worksheets/{created['id']}",
        data=json.dumps({"worksheet": body}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(request, timeout=60).close()
    script = """() => {
        const svg = document.querySelector('.single-sheet .worksheet .frozen-host > svg')
          ?? document.querySelector('.single-sheet .worksheet svg');
        const texts = [...svg.querySelectorAll('text')].filter((t) => t.textContent.trim());
        const roboto = [...document.fonts].filter((f) => f.family.replace(/["']/g, '') === 'Roboto');
        return {
          texts: texts.map((t) => {
            const r = t.getBoundingClientRect();
            return { text: t.textContent, left: r.left, right: r.right, size: t.getAttribute('font-size'),
                     y: (t.getAttribute('transform') || '').split(',')[1],
                     family: getComputedStyle(t).fontFamily };
          }),
          faces: roboto.length,
          loaded: roboto.filter((f) => f.status === 'loaded').map((f) => f.weight),
        };
    }"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 700})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/{created['id']}")
        page.wait_for_selector('.single-sheet .worksheet [data-drawn="true"]', timeout=30000)
        found = page.evaluate(script)
        number, unit = found["texts"]
        assert unit["text"] == "CM" and number["text"].replace(",", "").replace(".", "").isdigit()
        assert (number["size"], unit["size"]) == ("32px", "21.33px")
        # 單位緊接在數字的右邊（差不到 1 像素）、同一條基線
        assert abs(unit["left"] - number["right"]) < 1.0, found
        assert number["y"] == unit["y"]
        # 隨附的 Roboto：五個字重／樣式都有，用到的粗體已經載入
        assert found["faces"] == 5 and "700" in found["loaded"], found
        assert number["family"].startswith("Roboto")
        assert errors == []
        browser.close()


def test_dashboard_text_defaults_and_untitled_control(engine: str) -> None:
    """dashboard 的文字物件沒有指定字型、顏色時用活頁簿格式的「全部」字型；關掉標題的參數控制項
    （show-title='false'）只顯示控制項本身。"""
    from playwright.sync_api import expect, sync_playwright

    board = call(engine, "/api/edit/demo/dashboards", {"name": "Text Board"})["dashboard"]
    board["text_font"] = "Roboto"
    board["text_color"] = "#101e47"
    board["zones"][0]["children"][0]["children"] = [
        {"id": "z3", "kind": "text", "x": 4, "y": 4, "w": 400, "h": 60, "text": "REVENUE",
         "source_ref": "/editor"},
        {"id": "z4", "kind": "parameter", "x": 4, "y": 64, "w": 200, "h": 60,
         "parameter": "p_parameter_1", "control": "type_in", "show_title": False,
         "source_ref": "/editor"},
        {"id": "z5", "kind": "parameter", "x": 4, "y": 124, "w": 200, "h": 60,
         "parameter": "p_parameter_1", "control": "type_in", "source_ref": "/editor"},
    ]  # fmt: skip
    update = urllib.request.Request(
        f"{engine}/api/edit/demo/dashboards/{board['id']}",
        data=json.dumps({"dashboard": board}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(update, timeout=60) as response:
        assert response.status == 200
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/d/{board['id']}")
        text = page.locator(".dashboard .text", has_text="REVENUE")
        text.wait_for(timeout=30000)
        style = text.evaluate("e => [getComputedStyle(e).color, getComputedStyle(e).fontFamily]")
        assert style[0] == "rgb(16, 30, 71)", style
        assert style[1].replace('"', "").startswith("Roboto"), style
        controls = page.locator(".dashboard fieldset.control")
        expect(controls).to_have_count(2)
        shown = controls.locator("legend").evaluate_all(
            "list => list.map((e) => getComputedStyle(e).display !== 'none')"
        )
        assert shown == [False, True]  # 第一個關掉了標題
        assert errors == []
        browser.close()


def test_export_data_and_crosstab_in_desktop(engine: str, tmp_path: Path) -> None:
    """桌面版匯出資料（CSV）與交叉資料表（Excel）：問位置後由本機引擎寫出檔案（2026-11-21 稽核：引擎只收
    .png、.pptx、.pdf，這兩種一律失敗）；寫不出去時對話框裡說明原因。編輯器的「檢視資料」也能下載 CSV。"""
    from playwright.sync_api import Dialog, Page, expect, sync_playwright

    answers: list[str] = []

    def answer(dialog: Dialog) -> None:
        dialog.accept(answers.pop(0))

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", answer)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.wait_for_selector('.worksheet[data-loading="false"]')

        # 資料：下載 CSV（摘要）
        answers.append(str(tmp_path / "summary.csv"))
        page.get_by_label("匯出").select_option("data")
        dialog = page.get_by_role("dialog")
        dialog.locator(".data-preview tbody tr").first.wait_for()
        dialog.get_by_role("button", name="下載 CSV").click()
        expect(dialog.locator(".success")).to_contain_text(str(tmp_path / "summary.csv"))
        expect(dialog.locator(".error")).to_have_count(0)
        summary = (tmp_path / "summary.csv").read_text(encoding="utf-8-sig").splitlines()
        assert summary[0].split(",")[:2] == ["Region", "Category"] and len(summary) == 7
        # 寫不出去（資料夾不存在）：對話框裡說明原因，不帶 HTTP 狀態碼；之前的成功訊息拿掉
        answers.append(str(tmp_path / "nope" / "summary.csv"))
        dialog.get_by_role("button", name="下載 CSV").click()
        failed = dialog.get_by_role("alert")
        expect(failed).to_have_text(f"無法下載：資料夾不存在：{tmp_path / 'nope'}")
        expect(dialog.locator(".success")).to_have_count(0)
        assert not (tmp_path / "nope").exists()
        dialog.get_by_label("關閉").click()

        # 交叉資料表：Excel
        answers.append(str(tmp_path / "crosstab"))  # 沒有寫副檔名：補上 .xlsx
        page.get_by_label("匯出").select_option("crosstab")
        page.get_by_role("dialog").get_by_role("button", name="下載", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(page.get_by_text(f"已下載：{tmp_path / 'crosstab.xlsx'}")).to_be_visible()
        with zipfile.ZipFile(tmp_path / "crosstab.xlsx") as archive:
            assert "xl/workbook.xml" in archive.namelist() and archive.testzip() is None

        # 編輯器的「檢視資料」：下載 CSV
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        answers.append(str(tmp_path / "view.csv"))
        menubar(page, "分析", "檢視資料…")
        dialog = page.get_by_role("dialog")
        dialog.locator(".data-preview tbody tr").first.wait_for()
        dialog.get_by_role("button", name="下載 CSV").click()
        expect(dialog.locator(".success")).to_contain_text(str(tmp_path / "view.csv"))
        # 同樣的列（數字是另一次查詢的浮點數加總，尾數可能不同，只比對維度）
        view = (tmp_path / "view.csv").read_text(encoding="utf-8-sig").splitlines()
        assert [line.split(",")[:2] for line in view] == [line.split(",")[:2] for line in summary]
        assert errors == [] and answers == []
        browser.close()


def test_measure_values_adds_measure_names(engine: str) -> None:
    """Measure Values 拖到標籤（2026-11-21 稽核：8 個量值的數字疊在同一格）：檢視中沒有 Measure Names 時自動
    加上——列架上有維度而欄架上沒有時加到欄架，其餘加到列架（欄、列架上都有維度時加到列架，與 Tableau 說明
    文件的例子相同）——每個量值各占一格。之後把 Measure Names 拿掉，畫出來的數字仍然各在各的位置（不顯示
    標題）。Measure Values 放到座標軸上時 Measure Names 加到另一架。"""
    from playwright.sync_api import Page, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def pills(page: Page, shelf: str) -> list[str]:
        return [
            t.replace("▾", "").strip()
            for t in page.locator(f'[data-shelf="{shelf}"] .pill .pill-text').all_inner_texts()
        ]

    def sheet(worksheet: str) -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{worksheet}")["worksheet"]

    def numbers(page: Page) -> list[tuple[float, float, float, float]]:
        """檢視上每個數字（文字標記）的外框：左、上、右、下（凍結標題用的那幾份複本、多層列標題的文字
        不算）。"""
        marks = f"{MAIN_SVG} .mark-text.role-mark:not([class*='__deco']) text"
        page.wait_for_selector(marks)
        boxes = page.locator(marks).evaluate_all(
            "nodes => nodes.map((n) => { const r = n.getBoundingClientRect();"
            " return [r.left, r.top, r.right, r.bottom]; })"
        )
        return [(left, top, right, bottom) for left, top, right, bottom in boxes]

    def overlapping(boxes: list[tuple[float, float, float, float]]) -> int:
        """互相蓋到的數字有幾對（外框重疊超過 1 像素）。"""
        count = 0
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                wide = min(a[2], b[2]) - max(a[0], b[0])
                tall = min(a[3], b[3]) - max(a[1], b[1])
                count += wide > 1 and tall > 1
        return count

    def new_sheet(page: Page, number: int) -> str:
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_{number}")
        page.wait_for_selector(".editor-canvas .placeholder")
        return f"ws_{number}"

    def drag(page: Page, field: str, target: str) -> None:
        page.locator(f'.data-pane [data-field="{field}"]').drag_to(
            page.locator(f'[data-drop="{target}"]')
        )
        settle(page)

    def remove_pill(page: Page, shelf: str, name: str) -> None:
        page.locator(f'[data-shelf="{shelf}"] .pill', has_text=name).click()
        page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text="移除").first.click()
        settle(page)

    def notes(worksheet: str) -> list[str]:
        return call(engine, f"/api/workbooks/demo/worksheets/{worksheet}/render", {})["notes"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)

        # 列架上有維度、欄架上沒有：Measure Names 加到欄架
        ws = new_sheet(page, 1)
        drag(page, "f_category", "rows")
        drag(page, "measure_values", "marks.label")
        assert pills(page, "rows") == ["Category"] and pills(page, "columns") == ["Measure Names"]
        saved = sheet(ws)
        assert [u["field"] for u in saved["columns"]] == ["measure_names"]
        measures = len(saved["measure_values"])
        assert measures > 2  # 資料源中所有的量值
        shown = numbers(page)
        assert len(shown) == 3 * measures and overlapping(shown) == 0  # 3 個類別，沒有疊在一起的
        rendered = call(engine, f"/api/workbooks/demo/worksheets/{ws}/render", {})
        assert rendered["spec"]["encoding"]["x"]["axis"].get("labels") is not False  # 有欄標題
        assert not any("Measure Names" in note for note in rendered["notes"])

        # 把 Measure Names 拿掉（Tableau 可以這樣做）：數字仍然各占一格，只是沒有欄標題
        remove_pill(page, "columns", "Measure Names")
        assert pills(page, "columns") == [] and sheet(ws)["columns"] == []
        spread = numbers(page)
        assert len(spread) == 3 * measures and overlapping(spread) == 0
        rendered = call(engine, f"/api/workbooks/demo/worksheets/{ws}/render", {})
        assert rendered["spec"]["encoding"]["x"]["axis"]["labels"] is False
        assert any("Measure Names" in note for note in rendered["notes"])

        # 欄、列架上都沒有維度：Measure Names 加到列架
        ws = new_sheet(page, 2)
        drag(page, "measure_values", "marks.label")
        assert pills(page, "rows") == ["Measure Names"] and pills(page, "columns") == []
        shown = numbers(page)
        assert len(shown) == measures and overlapping(shown) == 0

        # 欄、列架上都有維度：Measure Names 加到列架的最後（各類別之下每個量值一列），各欄不擠在一起
        ws = new_sheet(page, 3)
        drag(page, "f_category", "rows")
        drag(page, "f_region", "columns")
        drag(page, "measure_values", "marks.label")
        assert pills(page, "rows") == ["Category", "Measure Names"]
        assert pills(page, "columns") == ["Region"]
        shown = numbers(page)
        assert len(shown) == 3 * 4 * measures and overlapping(shown) == 0
        assert len({round(box[2] / 10) for box in shown}) == 4  # 四個地區各一欄（數字靠右對齊）
        # 拿掉 Measure Names：畫圖時一樣排在列的方向
        remove_pill(page, "rows", "Measure Names")
        assert pills(page, "rows") == ["Category"]
        spread = numbers(page)
        assert len(spread) == 3 * 4 * measures and overlapping(spread) == 0
        assert len({round(box[1]) for box in spread}) == 3 * measures  # 每個類別之下每個量值一列
        assert len({round(box[2] / 10) for box in spread}) == 4
        assert any("Measure Names" in note for note in notes(ws))

        # 只有欄架上有維度：Measure Names 加到列架
        ws = new_sheet(page, 4)
        drag(page, "f_region", "columns")
        drag(page, "measure_values", "marks.label")
        assert pills(page, "rows") == ["Measure Names"] and pills(page, "columns") == ["Region"]
        shown = numbers(page)
        assert len(shown) == 4 * measures and overlapping(shown) == 0

        # Measure Values 放到列架（座標軸）：Measure Names 加到欄架，每個量值一根長條
        ws = new_sheet(page, 5)
        drag(page, "measure_values", "rows")
        assert pills(page, "rows") == ["Measure Values"]
        assert pills(page, "columns") == ["Measure Names"]
        # 放到欄架：加到列架（已有的維度之後）
        ws = new_sheet(page, 6)
        drag(page, "f_category", "rows")
        drag(page, "measure_values", "columns")
        assert pills(page, "rows") == ["Category", "Measure Names"]
        assert pills(page, "columns") == ["Measure Values"]

        # 檢視中已經有 Measure Names（在色彩上）：不再加
        ws = new_sheet(page, 7)
        drag(page, "measure_names", "marks.color")
        drag(page, "measure_values", "rows")
        assert pills(page, "rows") == ["Measure Values"] and pills(page, "columns") == []
        assert errors == []
        browser.close()


def test_multi_line_labels_make_text_table_rows_taller(engine: str) -> None:
    """標籤上有多個欄位（每個欄位一行）的文字表（2026-11-21 稽核時記下的：列高不跟著變高，上下列的文字疊在
    一起）：列高跟著行數變高——各格的文字互不重疊、各列等距（就是規格中的列高）、整段文字以列標題的中線為中心。
    欄架上加上維度、顯示大小換成符合寬度、符合高度與整個檢視（後兩者的列照舊填滿檢視）、自訂的標籤文字（行數依
    文字中的換行）、設定的列高比文字矮時也一樣。只有一行時列高照舊是 20 像素。"""
    from itertools import pairwise

    from playwright.sync_api import Page, expect, sync_playwright

    ws = "ws_1"
    marks = f"{MAIN_SVG} .mark-text.role-mark:not([class*='__deco']) text"

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def drag(page: Page, field: str, target: str) -> None:
        page.locator(f'.data-pane [data-field="{field}"]').drag_to(
            page.locator(f'[data-drop="{target}"]')
        )
        settle(page)

    def sheet() -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{ws}")["worksheet"]

    def rendered() -> dict[str, Any]:
        response = call(engine, f"/api/workbooks/demo/worksheets/{ws}/render", {})
        assert response["unsupported"] is None, response
        return response

    def row_step() -> int:
        """規格中每一列的高度（usermeta.sizing.rows.step）。"""
        return rendered()["spec"]["usermeta"]["sizing"]["rows"]["step"]

    def steps(boxes: list[tuple[float, float, float, float]]) -> list[float]:
        """相鄰兩列文字上緣的距離（各列等距時只有一個值）。"""
        tops = sorted({round(box[1], 1) for box in boxes})
        return sorted({round(b - a, 1) for a, b in pairwise(tops)})

    def cells(
        page: Page, lines: int, count: int, step: float | None = None
    ) -> list[tuple[float, float, float, float]]:
        """檢視上每一格文字（文字標記）的外框：左、上、右、下。等檢視重畫成 count 格、每一格 lines 行
        （給了 step 時各列的距離也都是它）再量；等不到就失敗。"""
        page.wait_for_selector(marks)
        deadline = time.monotonic() + 10
        while True:
            found = page.locator(marks).evaluate_all(
                "nodes => nodes.map((n) => { const r = n.getBoundingClientRect();"
                " return [r.left, r.top, r.right, r.bottom,"
                " Math.max(n.querySelectorAll('tspan').length, 1)]; })"
            )
            boxes = [(left, top, right, bottom) for left, top, right, bottom, _ in found]
            state = (len(found), {n for *_, n in found}, steps(boxes) if step is not None else [])
            if state == (count, {lines}, [step] if step is not None else []):
                return boxes
            assert time.monotonic() < deadline, state
            page.wait_for_timeout(100)

    def overlapping(boxes: list[tuple[float, float, float, float]]) -> int:
        """互相蓋到的文字有幾對（外框重疊超過 1 像素）。"""
        count = 0
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                wide = min(a[2], b[2]) - max(a[0], b[0])
                tall = min(a[3], b[3]) - max(a[1], b[1])
                count += wide > 1 and tall > 1
        return count

    def off_center(page: Page, boxes: list[tuple[float, float, float, float]]) -> float:
        """各列的文字（外框的中線）與列標題的中線最多差幾像素。"""
        members = {v["c0_label"] for v in rendered()["spec"]["data"]["values"]}
        labels = page.locator(f"{MAIN_SVG} .role-axis-label text").evaluate_all(
            "nodes => nodes.map((n) => { const r = n.getBoundingClientRect();"
            " return [n.textContent, (r.top + r.bottom) / 2]; })"
        )
        heads = sorted(y for text, y in labels if text in members)
        middles = sorted({round((box[1] + box[3]) / 2, 1) for box in boxes})
        assert len(heads) == len(middles) == 17, (len(heads), len(middles))
        return max(abs(a - b) for a, b in zip(heads, middles, strict=True))

    def confirm(page: Page, dialog: str) -> None:
        page.locator(dialog).get_by_role("button", name="確定").click()
        expect(page.locator(dialog)).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)

    def choose_fit(page: Page, mode: str) -> None:
        """工具列的顯示大小；等圖依新的大小重畫（填滿的方向與檢視的區塊同寬、同高）。"""
        page.get_by_label("顯示大小").select_option(mode)
        page.wait_for_timeout(300)
        settle(page)
        assert sheet()["fit"] == mode
        page.wait_for_function(
            """([selector, wide, tall]) => {
              const svg = document.querySelector(selector);
              const chart = svg && svg.closest('.chart');
              if (!chart) return false;
              const box = svg.getBoundingClientRect();
              return (!wide || box.width >= chart.clientWidth - 2)
                && (!tall || box.height >= chart.clientHeight - 2);
            }""",
            arg=[
                MAIN_SVG,
                mode in ("fit_width", "entire_view"),
                mode in ("fit_height", "entire_view"),
            ],
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1200})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/{ws}")
        page.wait_for_selector(".editor-canvas .placeholder")

        # 標籤上只有一個欄位：每一列 20 像素（與修正前相同）
        drag(page, "f_sub_category", "rows")
        drag(page, "f_sales", "marks.label")
        single = cells(page, 1, 17, step=20)
        assert overlapping(single) == 0 and row_step() == 20
        centered = off_center(page, single)

        # 標籤上三個欄位：每一格三行，列跟著變高——不重疊、各列等距、整段文字仍以列標題為中心
        drag(page, "f_profit", "marks.label")
        drag(page, "f_quantity", "marks.label")
        assert [u["field"] for u in sheet()["marks"]["label"]] == [
            "f_sales",
            "f_profit",
            "f_quantity",
        ]
        shown = cells(page, 3, 17)
        assert overlapping(shown) == 0
        tall = row_step()
        assert tall > 20 and steps(shown) == [tall]
        assert all(bottom - top <= tall for _, top, _, bottom in shown)  # 文字的高度在列高之內
        assert abs(off_center(page, shown) - centered) <= 1

        # 欄架上加上維度：四欄、每一列仍是同樣的高度
        drag(page, "f_region", "columns")
        crossed = cells(page, 3, 17 * 4, step=tall)
        assert overlapping(crossed) == 0 and row_step() == tall
        assert len({round(box[2] / 10) for box in crossed}) == 4

        # 顯示大小：符合寬度時列高不變；符合高度與整個檢視時列填滿檢視（不捲動），文字仍在各自的列中
        choose_fit(page, "fit_width")
        wide = cells(page, 3, 17 * 4, step=tall)
        assert overlapping(wide) == 0
        chart = page.locator(".editor-canvas .chart").first
        for mode in ("fit_height", "entire_view"):
            choose_fit(page, mode)
            assert rendered()["spec"]["height"] == "container" and row_step() == tall
            filled = cells(page, 3, 17 * 4)
            assert overlapping(filled) == 0, mode
            gaps = steps(filled)  # 各列等距：列高是檢視的高度平分，差不到 1 像素
            assert gaps[-1] - gaps[0] <= 1 and gaps[0] >= max(b - t for _, t, _, b in filled), mode
            assert chart.evaluate("n => n.scrollHeight - n.clientHeight") <= 1, mode  # 不捲動
            assert abs(off_center(page, filled) - centered) <= 1, mode
        choose_fit(page, "standard")

        # 自訂的標籤文字（標籤的編輯器）：兩行——行數依文字中的換行，不是標籤上欄位的數目
        page.locator(".mark-buttons").get_by_role("button", name="標籤…").click()
        page.get_by_role("textbox", name="標籤的文字").click()
        tools = page.get_by_role("toolbar", name="標籤的文字的格式")
        page.keyboard.type("業績 ")
        tools.get_by_label("插入").select_option(label="SUM(Sales)")
        page.keyboard.press("Enter")
        page.keyboard.type("利潤 ")
        tools.get_by_label("插入").select_option(label="SUM(Profit)")
        confirm(page, ".label-dialog")
        text = sheet()["marks"]["label_text"]
        assert [r.get("text") for r in text] == ["業績 ", None, "\n利潤 ", None]
        lower = row_step()
        assert 20 < lower < tall
        typed = cells(page, 2, 17 * 4, step=lower)
        assert overlapping(typed) == 0

        # 設定的列高（格式＞儲存格大小）比兩行的文字矮：列高加大到放得下；比文字高時照設定
        menubar(page, "格式", "格式…")
        page.get_by_label("列的儲存格大小").fill("24")
        confirm(page, ".format-dialog")
        assert sheet()["row_style"]["cell"] == 24
        tight = cells(page, 2, 17 * 4, step=lower)
        assert overlapping(tight) == 0 and row_step() == lower
        assert any("列高" in note for note in rendered()["notes"])
        menubar(page, "格式", "格式…")
        page.get_by_label("列的儲存格大小").fill("60")
        confirm(page, ".format-dialog")
        roomy = cells(page, 2, 17 * 4, step=60)
        assert overlapping(roomy) == 0 and row_step() == 60
        assert not any("列高" in note for note in rendered()["notes"])
        assert errors == []
        browser.close()


def test_mark_annotation_keeps_sorted_bars_in_order(engine: str) -> None:
    """排序過的長條圖加上標記的註解之後長條的順序不變（2026-11-21 記下的：示範報表的 Sub-Category Profit 依利潤
    由大到小，加上一個標記的註解之後變成依名稱排列——註解的圖層在離散的座標上少了與主圖相同的排序）：長條仍依
    利潤由大到小，註解畫在它的標記所在的那一列；再加上一條參考線也一樣。"""
    from playwright.sync_api import Page, sync_playwright

    ws = "ws_sub_category_profit"
    bars = ".editor-canvas svg path[aria-roledescription='bar']:not(.frozen *)"

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        page.wait_for_selector('.editor .chart[data-drawn="true"]')

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{body['id']}",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()

    def rows(page: Page) -> list[tuple[str, float]]:
        """畫面上由上到下的長條：子類別的名稱與中線的位置。"""
        found = page.locator(bars).evaluate_all(
            """nodes => nodes.map(n => { const r = n.getBoundingClientRect();
              return [n.getAttribute('aria-label'), r.top + r.height / 2]; })"""
        )
        named: list[tuple[str, float]] = []
        for label, y in sorted(found, key=lambda item: item[1]):
            matched = re.search(r"Sub-Category: ([^;]+)", label)
            assert matched is not None, label
            named.append((matched.group(1), y))
        return named

    original = call(engine, f"/api/edit/demo/worksheets/{ws}")["worksheet"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{ws}")
        settle(page)
        names = [name for name, _ in rows(page)]
        spec = call(engine, f"/api/workbooks/demo/worksheets/{ws}/render", {})["spec"]
        # 依利潤由大到小（查詢排好的順序），不是依名稱
        assert names == [v["c0"] for v in spec["data"]["values"]]
        assert len(names) >= 6 and names != sorted(names)

        # 第三個長條加上標記的註解：順序不變，註解的文字在那個長條的那一列
        noted = {
            **original,
            "annotations": [
                {
                    "kind": "mark",
                    "text": [{"text": "看這裡"}],
                    "point": {"mark": {"f_sub_category": names[2]}},
                    "offset": [60, 0],
                }
            ],
        }
        put(noted)
        page.reload()
        settle(page)
        text = page.locator(f"{MAIN_SVG} g[class*='__deco_annotation0'] text").first
        text.wait_for()
        shown = rows(page)
        assert [name for name, _ in shown] == names
        box = text.bounding_box()
        assert box is not None and text.text_content() == "看這裡"
        assert abs(box["y"] + box["height"] / 2 - shown[2][1]) <= 2

        # 再加上一條參考線（整張表的平均）：長條的順序仍然不變
        profit = original["columns"][0]
        lined = {
            **noted,
            "reference_lines": [
                {"id": "refline_e2e", "axis": profit, "value": profit}
                | {"formula": "average", "scope": "table", "source_ref": "/editor"}
            ],
        }
        put(lined)
        page.reload()
        settle(page)
        page.wait_for_selector(f"{MAIN_SVG} g[class*='__ref0_0']", state="attached")
        assert [name for name, _ in rows(page)] == names
        assert errors == []
        browser.close()


def test_render_notes_name_things_as_on_screen(engine: str) -> None:
    """畫圖的附註不寫內部的代號（2026-11-21 記下的：附註開始顯示給使用者之後，裡面是「參考線 ref_1 …」
    「預測（sum(f_profit)）：…」）：參考線是「分析」分頁清單上的寫法、欄位是膠囊上的寫法、註解是它的文字。
    編輯器與檢視器列出的附註裡都找不到欄位 id、參考線的編號與輸出欄名。"""
    from playwright.sync_api import Page, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{body['id']}",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()

    def notes_of(ws: str) -> list[str]:
        return call(engine, f"/api/workbooks/demo/worksheets/{ws}/render", {})["notes"]

    internal = re.compile(r"\bf_[a-z_]+\b|refline|\b(sum|avg)\(|\bws_")
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}
    # Sub-Category Profit：一條在 SUM(Sales) 軸上的參考線（檢視裡沒有這個軸）、一個標記不在資料中的註解
    bars = call(engine, "/api/edit/demo/worksheets/ws_sub_category_profit")["worksheet"]
    bars["reference_lines"] = [
        {"id": "refline_e2e", "axis": sales, "value": sales}
        | {"formula": "average", "scope": "table", "source_ref": "/editor"}
    ]
    bars["annotations"] = [
        {"kind": "mark", "text": [{"text": "看這裡"}]}
        | {"point": {"mark": {"f_sub_category": "沒有這個子類別"}}, "offset": [60, 0]}
    ]
    put(bars)
    line_note = "參考線「平均：SUM(Sales)」的量值不在座標軸上，沒有畫出"
    mark_note = "註解「看這裡」的標記不在目前的資料中，沒有畫出"
    assert notes_of("ws_sub_category_profit") == [line_note, mark_note]
    # Monthly Profit：兩年的資料估計不了自訂的模型（趨勢：加法）
    monthly = call(engine, "/api/edit/demo/worksheets/ws_monthly_profit")["worksheet"]
    monthly["columns"][0]["date"]["unit"] = "year"
    monthly["forecast"] = {"measures": [{"field": "f_profit", "agg": "sum"}], "ignore_last": 0}
    monthly["forecast"] |= {"model": "custom", "trend": "additive", "season": "none"}
    put(monthly)
    forecast_note = "預測（SUM(Profit)）：資料不足以估計自訂的模型（趨勢：加法、季節：無）"
    assert forecast_note in notes_of("ws_monthly_profit")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        listed = page.locator(".notes-list li")

        # 編輯器：附註裡參考線的寫法與「分析」分頁清單上的那一項相同
        page.goto(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)
        side_tab(page, "分析")
        item = page.locator(".analytics-list li > span").first.inner_text().strip()
        assert item == "平均：SUM(Sales)"
        page.locator(".editor .notes-indicator").get_by_role("button").click()
        assert listed.all_inner_texts() == [line_note, mark_note]
        assert f"「{item}」" in line_note

        # 編輯器：預測的附註以膠囊上的寫法稱呼量值
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        pills = page.locator('[data-shelf="rows"] .pill .pill-text').all_inner_texts()
        pill = pills[0].replace("▾", "").strip()
        assert pill == "SUM(Profit)"
        page.locator(".editor .notes-indicator").get_by_role("button").click()
        shown = listed.all_inner_texts()
        assert forecast_note in shown and f"（{pill}）" in forecast_note

        # 檢視器：看報表的人看到的也是同樣的文字
        page.goto(f"{engine}/w/demo/s/ws_sub_category_profit")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        page.locator(".single-sheet .notes-indicator").get_by_role("button").click()
        viewed = listed.all_inner_texts()
        assert viewed == [line_note, mark_note]
        assert not [note for note in [*shown, *viewed] if internal.search(note)]
        assert errors == []
        browser.close()


TEXT_LAYOUT_BOXES = """([selector]) => {
  const svg = document.querySelector(selector);
  if (!svg) return null;
  const box = (n) => { const r = n.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const marks = [...svg.querySelectorAll('g.mark-text.role-mark:not([class*="__deco"]) text')];
  return {
    view: box(svg),
    texts: marks.map((n) => [...box(n), Math.max(n.querySelectorAll('tspan').length, 1)]),
    symbols: [...svg.querySelectorAll('g.mark-symbol.role-mark path')].map(box),
    headers: [...svg.querySelectorAll('g.role-axis-label text')].map(box),
  };
}"""
"""檢視上的文字標記（含標記標籤；每一個的外框與行數）、圓點、標題成員的外框與整張圖的外框。"""


def _text_layout(engine: str, page: Any, name: str, **sheet: Any) -> dict[str, Any]:
    """新增一張工作表、設成 sheet（marks 只寫要改的），在編輯器中畫出來之後量：回傳 render 回應的 sizing
    （rows、columns）、附註與畫面上各元素的外框（texts 是 [左, 上, 右, 下]，lines 是各文字的行數）。"""
    created = call(engine, "/api/edit/demo/worksheets", {"name": name})["worksheet"]
    marks = {**created["marks"], **sheet.pop("marks", {})}
    request = urllib.request.Request(
        f"{engine}/api/edit/demo/worksheets/{created['id']}",
        data=json.dumps({"worksheet": {**created, **sheet, "marks": marks}}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(request, timeout=60).close()
    page.goto(f"{engine}/w/demo/edit/{created['id']}")
    page.wait_for_selector('.editor[data-saving="false"]')
    page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
    page.wait_for_selector('.editor .chart[data-drawn="true"]')
    page.wait_for_timeout(300)
    found = page.evaluate(TEXT_LAYOUT_BOXES, [MAIN_SVG])
    assert found is not None, name
    response = call(engine, f"/api/workbooks/demo/worksheets/{created['id']}/render", {})
    assert response["unsupported"] is None, response
    return {
        **response["spec"]["usermeta"]["sizing"],
        "notes": response["notes"],
        "view": found["view"],
        "texts": [box[:4] for box in found["texts"]],
        "lines": [box[4] for box in found["texts"]],
        "symbols": found["symbols"],
        "headers": found["headers"],
    }


def _overlapping(boxes: list[list[float]]) -> int:
    """互相蓋到的外框有幾對（重疊超過 1 像素）。"""
    count = 0
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            wide = min(a[2], b[2]) - max(a[0], b[0])
            tall = min(a[3], b[3]) - max(a[1], b[1])
            count += wide > 1 and tall > 1
    return count


def _middle(box: list[float]) -> float:
    return (box[1] + box[3]) / 2


def test_large_single_line_text_makes_rows_taller(engine: str) -> None:
    """文字表中只有一行的大字（2026-11-21 記下的：20pt 的字在 20 像素的列中，文字的外框 36 像素、上下兩列
    重疊 16 像素）：列跟著字變高——文字在自己的列裡、互不重疊，仍以列標題的中線為中心。預設的字照舊是
    20 像素的列。"""
    from itertools import pairwise

    from playwright.sync_api import sync_playwright

    sub, region = {"field": "f_sub_category"}, {"field": "f_region"}
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}

    def steps(boxes: list[list[float]]) -> list[float]:
        """相鄰兩列文字上緣的距離（各列等距時只有一個值）。"""
        tops = sorted({round(box[1], 1) for box in boxes})
        return sorted({round(b - a, 1) for a, b in pairwise(tops)})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1300})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        # 預設的字：每一列 20 像素（與修正前相同）
        plain = _text_layout(
            engine, page, "plain", rows=[sub], marks={"type": "text", "label": [sales]}
        )
        assert plain["rows"] == {"step": 20} and steps(plain["texts"]) == [20]
        assert len(plain["texts"]) == 17 and _overlapping(plain["texts"]) == 0

        # 20pt 的字：列跟著變高（42 像素），文字的外框（約 36 像素）在自己的列裡、互不重疊，
        # 仍以列標題的中線為中心
        marks = {"type": "text", "label": [sales], "label_font": {"size": 20}}
        large = _text_layout(engine, page, "large", rows=[sub], marks=marks)
        assert large["rows"] == {"step": 42} and steps(large["texts"]) == [42]
        heights = {round(box[3] - box[1]) for box in large["texts"]}
        assert len(large["texts"]) == 17 and all(30 <= h <= 42 for h in heights), heights
        assert _overlapping(large["texts"]) == 0 and large["notes"] == []
        heads = sorted(_middle(box) for box in large["headers"])
        middles = sorted(_middle(box) for box in large["texts"])
        # 外框的中線隨字型的上下伸部略有出入（這台電腦上差 2.5 像素）；列高是 42 像素
        assert max(abs(a - b) for a, b in zip(heads, middles, strict=True)) <= 5

        # 欄架上有維度：每一格的大字上下都不互相蓋到
        crossed = _text_layout(engine, page, "crossed", rows=[sub], columns=[region], marks=marks)
        assert crossed["rows"] == {"step": 42} and len(crossed["texts"]) == 17 * 4
        assert steps(crossed["texts"]) == [42]
        assert errors == []
        browser.close()


def test_text_table_columns_fit_each_line_and_its_font(engine: str) -> None:
    """文字表的欄寬依每一格的文字估計（2026-11-21 記下的：各行格式不同的自訂文字欄寬固定 40 像素，16pt 的
    數字往左蓋到列標題；一種格式的大字也一律當成 12 像素估計）：各行依自己的字級、取最寬的一行——文字不
    蓋到列標題，欄架上有維度時各格的文字左右不互相蓋到。"""
    from playwright.sync_api import sync_playwright

    sub, region = {"field": "f_sub_category"}, {"field": "f_region"}
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}
    per_line = [
        {"text": "Sales", "style": {"size": 8}},
        {"text": "\n"},
        {"use": sales, "style": {"size": 16}},
    ]

    def over_headers(found: dict[str, Any]) -> float:
        """文字往左超出列標題右緣幾像素（沒有蓋到時是 0）。"""
        right = max(box[2] for box in found["headers"])
        return max(0.0, right - min(box[0] for box in found["texts"]))

    def widest(boxes: list[list[float]]) -> float:
        return max(box[2] - box[0] for box in boxes)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1300})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        # 各行格式不同的自訂文字（8pt 的小標題、16pt 的數字）：欄寬放得下 16pt 的數字，不蓋到列標題
        marks = {"type": "text", "label": [sales], "label_text": per_line}
        lined = _text_layout(engine, page, "lined", rows=[sub], marks=marks)
        assert len(lined["texts"]) == 17 * 2
        assert lined["columns"]["size"] >= widest(lined["texts"]) > 40
        assert over_headers(lined) == 0
        # 欄架上有維度：各格的數字（較高的那一行）左右不互相蓋到
        lined = _text_layout(engine, page, "lined2", rows=[sub], columns=[region], marks=marks)
        numbers = [box for box in lined["texts"] if box[3] - box[1] > 20]
        assert len(numbers) == 17 * 4 and _overlapping(numbers) == 0
        assert lined["columns"]["step"] >= widest(numbers)

        # 一種格式的大字（20pt）：欄寬依 20pt 估計，不蓋到列標題；欄架上有維度時各格互不重疊
        marks = {"type": "text", "label": [sales], "label_font": {"size": 20}}
        large = _text_layout(engine, page, "large", rows=[sub], marks=marks)
        assert large["columns"]["size"] >= widest(large["texts"]) and over_headers(large) == 0
        crossed = _text_layout(engine, page, "crossed", rows=[sub], columns=[region], marks=marks)
        assert len(crossed["texts"]) == 17 * 4 and _overlapping(crossed["texts"]) == 0
        assert crossed["columns"]["step"] >= widest(crossed["texts"])

        # 預設的字：欄寬與修正前相同
        plain = _text_layout(
            engine, page, "plain", rows=[sub], marks={"type": "text", "label": [sales]}
        )
        assert plain["columns"] == {"size": 62} and over_headers(plain) == 0
        assert errors == []
        browser.close()


def test_multi_line_text_is_centred_without_rows(engine: str) -> None:
    """沒有列的檢視（KPI、只有欄的文字表）中同一種格式的多行文字（2026-11-21 記下的，2026-11-22 量過：三行
    的 KPI 整段的中線比檢視的中線低 14 像素——第一行在中間，其餘往下排）：整段以檢視的中線為中心，與只有
    一行時在同一個地方。"""
    from playwright.sync_api import sync_playwright

    region = {"field": "f_region"}
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}
    profit = {"field": "f_profit", "agg": "sum", "continuous": True}
    quantity = {"field": "f_quantity", "agg": "sum", "continuous": True}
    three = [sales, profit, quantity]

    def offsets(found: dict[str, Any]) -> list[float]:
        """各段文字的中線相對於整張圖的中線。"""
        middle = _middle(found["view"])
        return sorted(_middle(box) - middle for box in found["texts"])

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1300})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        # KPI（欄列架上沒有欄位）：三行的文字整段的中線與只有一行時在同一個地方
        one = _text_layout(engine, page, "kpi1", marks={"type": "text", "label": [sales]})
        many = _text_layout(engine, page, "kpi3", marks={"type": "text", "label": three})
        assert (one["lines"], many["lines"]) == ([1], [3])
        assert abs(offsets(many)[0] - offsets(one)[0]) <= 1.5
        # 只有欄的文字表：每一格三行，也一樣
        one = _text_layout(
            engine, page, "across1", columns=[region], marks={"type": "text", "label": [sales]}
        )
        many = _text_layout(
            engine, page, "across3", columns=[region], marks={"type": "text", "label": three}
        )
        assert set(many["lines"]) == {3} and len(many["texts"]) == 4
        assert max(abs(a - b) for a, b in zip(offsets(many), offsets(one), strict=True)) <= 1.5
        assert errors == []
        browser.close()


def test_dot_table_rows_follow_multi_line_labels(engine: str) -> None:
    """圓點的表格（每一列一個圓點）中多個欄位的標籤（2026-11-21 記下的，2026-11-22 量過：列高照舊 20 像素，
    三行的標籤疊在一起、有一半因為重疊被隱藏，整段的中線比圓點低 14 像素；在標記上方時第二行起蓋在標記上）：
    列跟著標籤變高，每個標籤都畫出來、互不重疊，整段的中線與圓點對齊；在標記上方或下方的標籤不蓋到標記。"""
    from playwright.sync_api import sync_playwright

    sub = {"field": "f_sub_category"}
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}
    profit = {"field": "f_profit", "agg": "sum", "continuous": True}
    quantity = {"field": "f_quantity", "agg": "sum", "continuous": True}
    three = [sales, profit, quantity]

    def beside(found: dict[str, Any]) -> list[tuple[list[float], list[float]]]:
        """每個圓點與它的標籤（由上到下配對）。"""
        assert len(found["symbols"]) == len(found["texts"]) == 17
        return list(
            zip(
                sorted(found["symbols"], key=_middle),
                sorted(found["texts"], key=_middle),
                strict=True,
            )
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        # 一個欄位的標籤照舊（20 像素的列）
        single = _text_layout(
            engine, page, "dots1", rows=[sub], marks={"type": "circle", "label": [sales]}
        )
        assert single["rows"] == {"step": 20}
        level = [_middle(text) - _middle(dot) for dot, text in beside(single)]

        # 三個欄位：列跟著變高（48 像素），17 個標籤都畫出來（沒有因為重疊被隱藏）、互不重疊，整段的
        # 中線與圓點的中線對齊（與一個欄位時相同），標籤在圓點的右邊
        dots = _text_layout(
            engine, page, "dots3", rows=[sub], marks={"type": "circle", "label": three}
        )
        assert dots["rows"] == {"step": 48}
        assert dots["lines"] == [3] * 17 and _overlapping(dots["texts"]) == 0
        centred = [_middle(text) - _middle(dot) for dot, text in beside(dots)]
        assert max(abs(c - level[0]) for c in centred) <= 1.5
        assert all(text[0] >= dot[2] for dot, text in beside(dots))

        # 標籤在標記的上方：整段在圓點之上（最後一行的下緣不低於圓點的上緣），不蓋到任何圓點或其他標籤
        marks = {"type": "circle", "label": three, "label_valign": "top"}
        above = _text_layout(engine, page, "above", rows=[sub], marks=marks)
        assert above["lines"] == [3] * 17
        assert _overlapping([*above["texts"], *above["symbols"]]) == 0
        for dot, text in beside(above):
            assert text[3] <= dot[1] + 1 and dot[1] - text[3] <= 8
        # 標籤在標記的下方也一樣
        marks = {"type": "circle", "label": three, "label_valign": "bottom"}
        below = _text_layout(engine, page, "below", rows=[sub], marks=marks)
        assert below["lines"] == [3] * 17
        assert _overlapping([*below["texts"], *below["symbols"]]) == 0
        assert all(text[1] >= dot[3] - 1 for dot, text in beside(below))
        assert errors == []
        browser.close()


def test_point_and_area_annotations_on_discrete_rows(engine: str) -> None:
    """點與區域的註解的座標在離散的座標軸上（2026-11-21 記下的，2026-11-22 在畫面上重現：示範報表的
    Sub-Category Profit 以 API 加上「列在某個子類別」的點或區域，圖上什麼都沒有，附註是「…的位置不在這張圖
    的座標軸上，沒有畫出」）：點的文字畫在那個子類別的那一列，區域的方框從第一列的上緣到最後一列的下緣
    （不碰到上下相鄰的長條），長條的順序不變、沒有附註。"""
    from playwright.sync_api import Page, sync_playwright

    ws = "ws_sub_category_profit"
    bars = ".editor-canvas svg path[aria-roledescription='bar']:not(.frozen *)"

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        page.wait_for_selector('.editor .chart[data-drawn="true"]')

    def put(body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{body['id']}",
            data=json.dumps({"worksheet": body}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=60).close()

    def rows(page: Page) -> list[tuple[str, float, float]]:
        """畫面上由上到下的長條：子類別的名稱、上緣與下緣。"""
        found = page.locator(bars).evaluate_all(
            """nodes => nodes.map(n => { const r = n.getBoundingClientRect();
              return [n.getAttribute('aria-label'), r.top, r.bottom]; })"""
        )
        named: list[tuple[str, float, float]] = []
        for label, top, bottom in sorted(found, key=lambda item: item[1]):
            matched = re.search(r"Sub-Category: ([^;]+)", label)
            assert matched is not None, label
            named.append((matched.group(1), top, bottom))
        return named

    original = call(engine, f"/api/edit/demo/worksheets/{ws}")["worksheet"]
    profit, category = original["columns"][0], original["rows"][0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{ws}")
        settle(page)
        names = [name for name, _, _ in rows(page)]
        assert len(names) >= 6 and names != sorted(names)  # 依利潤排序，不是依名稱

        def at(name: str, x: float) -> dict[str, Any]:
            return {"x": x, "x_axis": profit, "y": name, "y_axis": category}

        noted = {
            **original,
            "annotations": [
                {"kind": "point", "text": [{"text": "這一列"}], "point": at(names[2], 20000)}
                | {"offset": [30, 0]},
                {"kind": "area", "text": [{"text": "這幾列"}]}
                | {"top_left": at(names[1], 10000), "bottom_right": at(names[4], 30000)},
            ],
        }
        put(noted)
        page.reload()
        settle(page)
        text = page.locator(f"{MAIN_SVG} g[class*='__deco_annotation0'] text").first
        text.wait_for()
        shown = rows(page)
        assert [name for name, _, _ in shown] == names
        response = call(engine, f"/api/workbooks/demo/worksheets/{ws}/render", {})
        assert response["notes"] == []
        # 點：文字在第三個長條的那一列
        box = text.bounding_box()
        assert box is not None and text.text_content() == "這一列"
        middle = (shown[2][1] + shown[2][2]) / 2
        assert abs(box["y"] + box["height"] / 2 - middle) <= 2
        # 區域：方框涵蓋第二到第五個長條，不碰到第一個與第六個
        rect = page.locator(f"{MAIN_SVG} g[class*='__deco_annotation1_box'] path").first
        area = rect.bounding_box()
        assert area is not None
        top, bottom = area["y"], area["y"] + area["height"]
        assert shown[0][2] <= top <= shown[1][1] and shown[4][2] <= bottom <= shown[5][1]
        # 文字在這四列的中間（第三、第四個長條之間）
        label = page.locator(f"{MAIN_SVG} g[class*='__deco_annotation1'] text").last
        placed = label.bounding_box()
        assert placed is not None and label.text_content() == "這幾列"
        assert abs(placed["y"] + placed["height"] / 2 - (shown[2][2] + shown[3][1]) / 2) <= 2
        assert errors == []
        browser.close()


def test_dot_table_single_line_labels_above_and_below(engine: str) -> None:
    """圓點與形狀的表格（每一列一個標記、預設 20 像素的列）中只有一行的標籤在標記的上方或下方（2026-11-22
    在畫面上量過：17 個標籤中 16 個蓋到上一列或下一列的圓點，最多 10 像素）：與多行的相同，列加高到標籤
    留在自己的這一列裡（46 像素），不蓋到任何標記或其他標籤；標籤在右邊（預設）的照舊 20 像素。"""
    from playwright.sync_api import sync_playwright

    sub = {"field": "f_sub_category"}
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1300})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        for mark, valign in (("circle", "top"), ("circle", "bottom"), ("shape", "top")):
            marks = {"type": mark, "label": [sales], "label_valign": valign}
            found = _text_layout(engine, page, f"{mark}_{valign}", rows=[sub], marks=marks)
            assert found["rows"] == {"step": 46}, (mark, valign)
            assert len(found["texts"]) == len(found["symbols"]) == 17
            assert _overlapping([*found["texts"], *found["symbols"]]) == 0, (mark, valign)
            pairs = zip(
                sorted(found["symbols"], key=_middle),
                sorted(found["texts"], key=_middle),
                strict=True,
            )
            for dot, text in pairs:
                # 在標記之上（之下），整行在這一列裡：標記在列的中線，上下各 23 像素（文字的外框含字型
                # 上下的留白，比行高 14 像素多 2 像素）
                if valign == "top":
                    assert text[3] <= dot[1] + 1 and text[1] >= _middle(dot) - 23 - 3
                else:
                    assert text[1] >= dot[3] - 1 and text[3] <= _middle(dot) + 23 + 3

        side = _text_layout(
            engine, page, "circle_side", rows=[sub], marks={"type": "circle", "label": [sales]}
        )
        assert side["rows"] == {"step": 20}
        assert _overlapping([*side["texts"], *side["symbols"]]) == 0
        assert errors == []
        browser.close()


def test_multi_line_labels_on_bars_lines_and_points(engine: str) -> None:
    """長條、折線與散佈圖上多行的標籤（標籤上的兩個欄位；2026-11-21 記下的，2026-11-22 在畫面上量過：Vega
    把第一行放在單行時的位置、其餘往下排——橫的長條上整段比長條的中線低 7 像素，直的長條上方的第二行蓋到
    長條 11 像素，折線點上方的第二行蓋到點 10 像素，散佈圖標記旁的整段比圓點低 7 像素）：整段放在單行時
    的位置——以長條或圓點的中線為中心；在直的長條與折線點之上的，最後一行在長條、點的上方。"""
    from playwright.sync_api import sync_playwright

    region = {"field": "f_region"}
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}
    profit = {"field": "f_profit", "agg": "sum", "continuous": True}
    year = {"field": "f_order_date", "date": {"kind": "part", "unit": "year"}}
    two = [sales, profit]

    def bars(page: Any) -> list[list[float]]:
        return page.locator(f"{MAIN_SVG} path[aria-roledescription='bar']").evaluate_all(
            """nodes => nodes.map(n => { const r = n.getBoundingClientRect();
              return [r.left, r.top, r.right, r.bottom]; })"""
        )

    def centre_x(box: list[float]) -> float:
        return (box[0] + box[2]) / 2

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        # 橫的長條：整段的中線與長條的中線對齊
        marks = {"type": "bar", "label": two}
        found = _text_layout(engine, page, "hbars2", rows=[region], columns=[sales], marks=marks)
        assert found["lines"] == [2] * 4
        pairs = zip(
            sorted(bars(page), key=_middle), sorted(found["texts"], key=_middle), strict=True
        )
        for bar, text in pairs:
            assert abs(_middle(text) - _middle(bar)) <= 1.5

        # 直的長條：整段在長條的上端之上，不蓋到長條
        found = _text_layout(engine, page, "vbars2", rows=[sales], columns=[region], marks=marks)
        assert found["lines"] == [2] * 4
        shown = bars(page)
        pairs = zip(sorted(shown, key=centre_x), sorted(found["texts"], key=centre_x), strict=True)
        for bar, text in pairs:
            assert 1 <= bar[1] - text[3] <= 5
        assert _overlapping([*shown, *found["texts"]]) == 0

        # 折線：整段在點之上（透明的點：每個標記都有一個）
        marks = {"type": "line", "label": two}
        found = _text_layout(engine, page, "lines2", rows=[sales], columns=[year], marks=marks)
        assert found["lines"] == [2] * 4
        for text in found["texts"]:
            point = min(found["symbols"], key=lambda s: abs(centre_x(s) - centre_x(text)))
            assert text[3] <= _middle(point) - 2

        # 散佈圖：標記右邊的整段以圓點的中線為中心
        marks = {"type": "circle", "label": [region, sales], "detail": [region]}
        found = _text_layout(engine, page, "scatter2", rows=[sales], columns=[profit], marks=marks)
        assert found["lines"] == [2] * 4
        for text in found["texts"]:
            dot = min(
                found["symbols"],
                key=lambda s: abs(s[2] + 4 - text[0]) + abs(_middle(s) - _middle(text)),
            )
            assert abs(_middle(text) - _middle(dot)) <= 1.5
        assert errors == []
        browser.close()


def test_dot_and_highlight_table_columns_fit_their_text(engine: str) -> None:
    """欄架上有維度的圓點表格與醒目提示表的欄寬（2026-11-22 在畫面上量過：只依欄標題估計——圓點右邊 12pt
    的標籤蓋到右邊那一欄的圓點 7 像素；醒目提示表 20pt 的數字 92 像素寬、欄寬 89 像素）：圓點的標籤不蓋到
    別的圓點與標籤，醒目提示表每一格的文字在自己的色塊裡。"""
    from playwright.sync_api import sync_playwright

    sub, region = {"field": "f_sub_category"}, {"field": "f_region"}
    sales = {"field": "f_sales", "agg": "sum", "continuous": True}

    def cells(page: Any) -> list[list[float]]:
        return page.locator(f"{MAIN_SVG} g.mark-rect.role-mark path").evaluate_all(
            """nodes => nodes.map(n => { const r = n.getBoundingClientRect();
              return [r.left, r.top, r.right, r.bottom]; })"""
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        # 圓點表格：12pt 的標籤在圓點右邊，不蓋到右邊那一欄的圓點或標籤
        marks = {"type": "circle", "label": [sales], "label_font": {"size": 12}}
        dots = _text_layout(engine, page, "dots_cols", rows=[sub], columns=[region], marks=marks)
        assert len(dots["texts"]) == len(dots["symbols"]) == 17 * 4
        assert _overlapping([*dots["texts"], *dots["symbols"]]) == 0
        assert dots["columns"]["step"] > 62  # 原本只依欄標題（Central）估計

        # 醒目提示表：20pt 的數字在自己的色塊裡（左右不超出）
        marks = {"type": "square", "label": [sales], "color": sales, "label_font": {"size": 20}}
        table = _text_layout(engine, page, "highlight", rows=[region], columns=[sub], marks=marks)
        blocks = cells(page)
        assert len(table["texts"]) == len(blocks) == 4 * 17
        for text in table["texts"]:
            centre = ((text[0] + text[2]) / 2, _middle(text))
            block = next(
                b for b in blocks if b[0] <= centre[0] <= b[2] and b[1] <= centre[1] <= b[3]
            )
            assert block[0] - 1 <= text[0] and text[2] <= block[2] + 1, (text, block)
        assert errors == []
        browser.close()


def test_dual_axis_with_trend_lines(engine: str) -> None:
    """雙軸加趨勢線（2026-11-21 稽核：整張圖變成「查詢失敗：Duplicate data set name: "__ref_trend_pathgroup"」）：
    連續的月份 × SUM(Sales)、SUM(Profit)，Profit 設成雙軸後加上趨勢線——圖照樣畫出來，兩個量值各一條趨勢線。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def menu(page: Page, label: str) -> None:
        page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text=label).first.click()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_order_date"]').drag_to(
            page.locator('[data-drop="columns"]')
        )
        settle(page)
        page.locator('[data-shelf="columns"] .pill').click()
        menu(page, "月（連續）")
        settle(page)
        for field in ("f_sales", "f_profit"):
            page.locator(f'.data-pane [data-field="{field}"]').drag_to(
                page.locator('[data-drop="rows"]')
            )
            settle(page)
        page.locator('[data-shelf="rows"] .pill', has_text="SUM(Profit)").click()
        menu(page, "雙軸")
        settle(page)
        menubar(page, "分析", "趨勢線…")
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        settle(page)

        # 圖畫出來了（不是錯誤訊息），兩條趨勢線各在自己的圖層
        expect(page.locator(".editor-canvas .placeholder")).to_have_count(0)
        expect(page.locator(".editor-canvas")).not_to_contain_text("查詢失敗")
        page.wait_for_selector(f"{MAIN_SVG} .mark-line.role-mark path")
        saved = call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]
        assert saved["marks"]["trend"] is not None and saved["panes"][0]["dual"] is True
        rendered = call(engine, "/api/workbooks/demo/worksheets/ws_1/render", {})
        assert rendered["unsupported"] is None
        trends = rendered["spec"]["usermeta"]["trends"]
        assert [t["group"] for t in trends] == [["Sales"], ["Profit"]]
        # 畫面上：兩個量值的折線加上兩條趨勢線，趨勢線的顏色與自己的量值相同
        strokes = page.locator(f"{MAIN_SVG} .mark-line.role-mark path").evaluate_all(
            "nodes => nodes.map((n) => getComputedStyle(n).stroke)"
        )
        assert len(strokes) == 4 and len(set(strokes)) == 2, strokes
        assert all(strokes.count(stroke) == 2 for stroke in strokes), strokes
        assert errors == []
        browser.close()


def test_top_filter_lists_field_captions(engine: str) -> None:
    """篩選對話框「頂部」的「依」（2026-11-21 稽核：計算欄位列出的是 [Calculation_1368…] 這種內部名稱）：
    選單上是欄位的名稱（與膠囊相同的寫法），存檔的公式仍然用內部名稱；再開啟時顯示選的那一項。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)
        before = len(
            call(engine, "/api/edit/demo/worksheets/ws_sub_category_profit")["worksheet"]["filters"]
        )
        page.locator('.data-pane [data-field="f_sub_category"]').drag_to(
            page.locator('[data-drop="filters"]')
        )
        dialog = page.locator(".filter-dialog")
        dialog.locator(".value-list li").first.wait_for()
        dialog.get_by_role("tab", name="頂部").click()
        by = dialog.get_by_label("排名依據")
        labels = by.locator("option").all_inner_texts()
        assert labels[:3] == ["（選擇量值）", "SUM(Sales)", "SUM(Quantity)"]
        assert "利潤率" in labels and "SUM(區域總銷售)" in labels
        assert not [text for text in labels if "Calculation_" in text or "[" in text], labels
        dialog.get_by_label("數量").fill("3")
        by.select_option(label="利潤率")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)
        settle(page)
        saved = call(engine, "/api/edit/demo/worksheets/ws_sub_category_profit")["worksheet"]
        assert len(saved["filters"]) == before + 1
        top = saved["filters"][-1]
        assert (top["kind"], top["count"]) == ("top_n", 3)
        assert top["by"] == "[Calculation_1368249927221915648]"  # 存的是公式（內部名稱）
        rows = call(engine, "/api/workbooks/demo/worksheets/ws_sub_category_profit/render", {})
        assert rows["unsupported"] is None and rows["rows"] <= 3

        # 再開啟：選單停在「利潤率」
        page.locator('[data-drop="filters"] .pill', has_text="Sub-Category").click()
        page.locator(
            ".popup-menu li[role=menuitem] > .menu-label", has_text="編輯篩選…"
        ).first.click()
        dialog = page.locator(".filter-dialog")
        by = dialog.get_by_label("排名依據")
        expect(by).to_have_value("[Calculation_1368249927221915648]")
        assert by.locator("option:checked").inner_text() == "利潤率"
        assert errors == []
        browser.close()


def test_dashboard_image_object_asks_for_an_image(engine: str, tmp_path: Path) -> None:
    """dashboard 的「圖片」物件（2026-11-21 稽核：一按就加入一個指向 image.png 的壞圖，請求 404）：先開
    「編輯圖片物件」選圖片——電腦上的檔案或報表中已有的圖片——按確定才加入；取消不加入。圖片存進報表，
    存成報表套件時跟著走；找不到圖片的區塊不顯示破圖。"""
    import struct
    import zlib

    from playwright.sync_api import Dialog, Page, expect, sync_playwright

    def png(color: tuple[int, int, int]) -> bytes:
        def chunk(kind: bytes, data: bytes) -> bytes:
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

        rows = b"".join(b"\x00" + bytes(color) * 40 for _ in range(20))
        header = struct.pack(">IIBBBBB", 40, 20, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b"")
        )

    logo, other = tmp_path / "logo.png", tmp_path / "other" / "logo.png"
    other.parent.mkdir()
    logo.write_bytes(png((200, 30, 30)))
    other.write_bytes(png((30, 30, 200)))
    (tmp_path / "notes.png").write_bytes(b"not an image")

    def images() -> list[str | None]:
        board = call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]
        found: list[str | None] = []

        def walk(zones: list[dict[str, Any]]) -> None:
            for zone in zones:
                if zone["kind"] == "image":
                    found.append(zone["image"])
                walk(zone.get("children") or [])

        walk(board["zones"])
        return found

    def settle(page: Page) -> None:
        page.wait_for_selector('.dash-editor[data-saving="false"]')
        page.wait_for_load_state("networkidle")

    target = tmp_path / "with-image.tmigx"

    def answer(dialog: Dialog) -> None:
        dialog.accept(str(target))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        missing: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", answer)
        page.on(
            "response",
            lambda r: missing.append(r.url) if r.status == 404 and "/files/" in r.url else None,
        )
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit-d/db_ee909b0c")
        settle(page)
        add = page.locator(".object-buttons").get_by_role("button", name="圖片", exact=True)

        # 按「圖片」：先選圖片，還沒有加入任何區塊；沒有選圖片不能按確定；取消不加入
        add.click()
        dialog = page.get_by_role("dialog", name="編輯圖片物件")
        confirm = dialog.get_by_role("button", name="確定")
        expect(confirm).to_be_disabled()
        assert images() == []
        dialog.get_by_role("button", name="取消").click()
        expect(dialog).to_have_count(0)
        settle(page)
        assert images() == [] and page.locator(".dashboard .zone-image").count() == 0

        # 選電腦上的圖片檔：按確定後存進報表、加入圖片物件，圖片顯示得出來
        add.click()
        dialog.get_by_label("圖片檔").set_input_files(str(logo))
        expect(dialog.locator(".image-preview img")).to_be_visible()
        confirm.click()
        expect(dialog).to_have_count(0)
        settle(page)
        assert images() == ["Image/logo.png"]
        shown = page.locator(".dashboard img.zone-image")
        expect(shown).to_have_count(1)
        page.wait_for_function(
            "(() => { const i = document.querySelector('.dashboard img.zone-image');"
            " return i && i.complete && i.naturalWidth === 40; })()"
        )

        # 內容不是圖片的檔案：說明原因，不加入
        add.click()
        dialog.get_by_label("圖片檔").set_input_files(str(tmp_path / "notes.png"))
        confirm.click()
        expect(dialog.get_by_role("alert")).to_contain_text("無法加入圖片")
        dialog.get_by_role("button", name="取消").click()
        assert images() == ["Image/logo.png"]

        # 再加一個：可以選報表中已有的圖片
        add.click()
        choice = dialog.get_by_role("option", name="logo.png")
        choice.click()
        expect(choice).to_have_attribute("aria-selected", "true")
        confirm.click()
        expect(dialog).to_have_count(0)
        settle(page)
        assert images() == ["Image/logo.png", "Image/logo.png"]

        # 版面配置的屬性：換成另一張同名、內容不同的圖片——另取檔名，不蓋掉另一個區塊用的圖片
        side_tab(page, "版面配置")
        props = page.locator(".zone-props")
        expect(props.locator(".image-name")).to_have_text("logo.png")
        props.get_by_role("button", name="選擇圖片…").click()
        dialog.get_by_label("圖片檔").set_input_files(str(other))
        confirm.click()
        expect(dialog).to_have_count(0)
        settle(page)
        assert sorted(images()) == ["Image/logo (2).png", "Image/logo.png"]  # type: ignore[type-var]
        expect(props.locator(".image-name")).to_have_text("logo (2).png")
        assert call_bytes(engine, "/api/workbooks/demo/files/Image/logo.png") == logo.read_bytes()
        assert (
            call_bytes(engine, "/api/workbooks/demo/files/Image/logo%20(2).png")
            == other.read_bytes()
        )
        assert missing == []

        # 存成報表套件：兩張圖片都在裡面
        tool_bar = page.locator(".ws-toolbar")
        tool_bar.get_by_role("button", name="儲存", exact=True).click()
        expect(page.locator(".toast")).to_contain_text("已儲存")
        with zipfile.ZipFile(target) as archive:
            assert {"files/Image/logo.png", "files/Image/logo (2).png"} <= set(archive.namelist())

        # 報表中找不到的圖片（例如 .twb 指向的檔案不在了）：檢視時是空的，不是破圖；編輯器寫出檔名
        board = call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]

        def repoint(zones: list[dict[str, Any]]) -> None:
            for zone in zones:
                if zone["kind"] == "image":
                    zone["image"] = "Image/gone.png"
                repoint(zone.get("children") or [])

        repoint(board["zones"])
        update = urllib.request.Request(
            f"{engine}/api/edit/demo/dashboards/db_ee909b0c",
            data=json.dumps({"dashboard": board}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(update, timeout=60) as response:
            assert response.status == 200
        page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        gone = page.locator(".dashboard .zone-image.missing")
        expect(gone).to_have_count(2)
        expect(page.locator(".dashboard img.zone-image")).to_have_count(0)
        assert gone.first.evaluate("e => getComputedStyle(e, '::after').content") == "none"
        page.goto(f"{engine}/w/demo/edit-d/db_ee909b0c")
        settle(page)
        gone = page.locator(".dash-canvas .zone-image.missing")
        expect(gone).to_have_count(2)
        label = gone.first.evaluate("e => getComputedStyle(e, '::after').content")
        assert "找不到圖片：Image/gone.png" in label
        assert errors == []
        browser.close()


def test_fit_height_rows_are_not_squeezed_into_each_other(engine: str) -> None:
    """編輯器裡符合高度、整個檢視的文字表（檢視的高度隨視窗）：放得下時列填滿檢視；視窗變矮、列被壓到相鄰兩列的
    文字互相蓋住時，列高保持標準的大小、檢視上下捲動；視窗再變高，回到填滿。只是被壓得比標準的列高小一點、
    文字沒有互相蓋住的時候照舊填滿。原本一律填滿：17 列三行的文字表在 694 像素高的檢視中每列 39 像素，
    44 像素高的文字上下疊在一起。"""
    from playwright.sync_api import Page, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def use(field: str, agg: str | None = None) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": None,
            "continuous": agg is not None,
            "table_calc": None,
        }

    def put(worksheet: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{worksheet['id']}",
            data=json.dumps({"worksheet": worksheet}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            assert response.status == 200

    measure = """() => {
      const chart = document.querySelector('.editor-canvas .chart');
      const svg = chart.querySelector(':scope > svg, :scope > .frozen-host > svg');
      const texts = [];
      for (const group of svg.querySelectorAll('g.mark-text.role-mark')) {
        if ([...group.classList].some((name) => name.startsWith('__'))) continue;
        for (const text of group.querySelectorAll(':scope > text'))
          texts.push({ box: text.getBoundingClientRect(), row: text.__data__.y });
      }
      let overlapping = 0;
      for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
        const a = texts[i], b = texts[j];
        if (Math.abs(a.row - b.row) < 0.5) continue;
        const wide = Math.min(a.box.right, b.box.right) - Math.max(a.box.left, b.box.left);
        const tall = Math.min(a.box.bottom, b.box.bottom) - Math.max(a.box.top, b.box.top);
        if (wide > 1 && tall > 1) overlapping++;
      }
      const rows = [...new Set(texts.map((t) => Math.round(t.row * 10) / 10))].sort((a, b) => a - b);
      return {
        texts: texts.length,
        overlapping,
        step: rows.length > 1 ? rows[1] - rows[0] : null,
        textHeight: Math.max(...texts.map((t) => t.box.height)),
        client: chart.clientHeight,
        scrollable: chart.scrollHeight - chart.clientHeight,
        kept: chart.dataset.rows === 'kept',
      };
    }"""

    # Sub-Category（17 列）的文字表，標籤上三個量值（每格三行），顯示大小是整個檢視
    sheet = call(engine, "/api/edit/demo/worksheets", {})["worksheet"]
    sheet["rows"] = [use("f_sub_category")]
    sheet["marks"]["label"] = [
        use("f_sales", "sum"),
        use("f_profit", "sum"),
        use("f_quantity", "sum"),
    ]
    sheet["fit"] = "entire_view"
    put(sheet)
    rendered = call(engine, f"/api/workbooks/demo/worksheets/{sheet['id']}/render", {})
    assert rendered["spec"]["usermeta"]["sizing"]["rows"]["step"] == 48

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1200})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def layout(
            kept: bool, height: int | None = None, scroll: str | None = None
        ) -> dict[str, Any]:
            """量檢視的排列；給了 height 時先把視窗改成這個高度。等檢視重畫成預期的樣子（列高保持或填滿；
            給了 scroll 時連同捲動的方向）再量，等不到就失敗。"""
            if height is not None:
                page.set_viewport_size({"width": 1500, "height": height})
            page.wait_for_function(
                """([kept, scroll]) => {
                  const chart = document.querySelector('.editor-canvas .chart[data-drawn="true"]');
                  const svg = chart && chart.querySelector(':scope > svg, :scope > .frozen-host > svg');
                  if (!svg || (chart.dataset.rows === 'kept') !== kept) return false;
                  if (scroll !== null && chart.dataset.scroll !== scroll) return false;
                  // 填滿時圖與檢視同高；保持列高時圖比檢視高
                  const fills = Math.abs(svg.getBoundingClientRect().height - chart.clientHeight) <= 2;
                  return fills !== kept;
                }""",
                arg=[kept, scroll],
            )
            return page.evaluate(measure)

        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{sheet['id']}")
        settle(page)

        # 視窗夠高：列填滿檢視（每列比標準的 48 像素高），不捲動
        found = layout(kept=False)
        assert found["texts"] == 17 and found["overlapping"] == 0, found
        assert found["step"] > 48 and found["scrollable"] <= 1, found
        # 視窗變矮（檢視 694 像素）：填滿的話每列 39 像素、比 44 像素的文字矮——列高保持 48 像素，上下捲動
        found = layout(kept=True, height=900)
        assert found["overlapping"] == 0 and found["step"] == pytest.approx(48, abs=0.1), found
        assert found["scrollable"] > 100, found
        # 檢視 794 像素：每列 45 像素，比標準的列高小一點，但文字沒有互相蓋住——照舊填滿
        found = layout(kept=False, height=1000)
        assert found["overlapping"] == 0 and found["scrollable"] <= 1, found
        assert found["textHeight"] <= found["step"] < 48, found
        # 更矮，然後回到夠高：保持列高，再回到填滿
        found = layout(kept=True, height=700)
        assert found["overlapping"] == 0 and found["step"] == pytest.approx(48, abs=0.1), found
        found = layout(kept=False, height=1200)
        assert found["overlapping"] == 0 and found["step"] > 48 and found["scrollable"] <= 1, found

        # 符合高度（工具列的顯示大小）在矮的視窗中也一樣
        page.set_viewport_size({"width": 1500, "height": 900})
        page.get_by_label("顯示大小").select_option("fit_height")
        settle(page)
        found = layout(kept=True, scroll="both")  # 符合高度時寬度不填滿：兩個方向都可以捲動
        assert found["overlapping"] == 0 and found["step"] == pytest.approx(48, abs=0.1), found
        assert errors == []
        browser.close()


def test_filter_range_default_arriving_late_keeps_what_was_typed(engine: str) -> None:
    """量值篩選對話框的預設範圍（資料的最小值、最大值）是另一個查詢，機器忙的時候在使用者開始輸入之後才回來：
    這時使用者改過、或正在裡面（點進去、全選了準備輸入）的方塊不填預設值，打的字原樣留下；其他的方塊照常填上。

    原本最小值的方塊只要還是空的就填進資料的最小值：點進去（或像 Playwright 的 fill 一樣全選）之後範圍才到，
    預設值填進方塊、游標跑到最後，接著打的「600000」接在後面，變成「391721.905…600000」，存成資料的最小值
    （test_filter_tabs_control_options_missing_values_and_view_data 偶爾失敗的原因）。測試把範圍的回應扣住，
    在指定的步驟之後才放行，不靠時間。"""
    from playwright.sync_api import Page, Route, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def use(field: str, agg: str | None = None) -> dict[str, Any]:
        return {"field": field, "agg": agg, "date": None, "continuous": agg is not None}

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Late"})["worksheet"]
    ws = created["id"]
    request = urllib.request.Request(
        f"{engine}/api/edit/demo/worksheets/{ws}",
        data=json.dumps(
            {
                "worksheet": {
                    **created,
                    "rows": [use("f_region")],
                    "columns": [use("f_sales", "sum")],
                }
            }
        ).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200
    bounds = call(
        engine,
        "/api/edit/demo/domain",
        {"use": use("f_sales", "sum"), "kind": "range", "worksheet": ws},
    )
    least, most = float(bounds["min"]), float(bounds["max"])
    assert least < 600000 < most

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        held: list[Route] = []

        def hold(route: Route) -> None:
            body = route.request.post_data_json or {}
            if body.get("kind") == "range":
                held.append(route)  # 扣住範圍的查詢，等 release() 才放行
            else:
                route.continue_()

        page.route("**/api/edit/demo/domain", hold)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{ws}")
        settle(page)
        dialog = page.locator(".filter-dialog")
        low = dialog.get_by_label("最小值")
        high = dialog.get_by_label("最大值")

        def open_dialog(tab: str = "值範圍") -> None:
            page.locator('.shelf[data-shelf="columns"] .pill').click()
            page.locator(
                ".popup-menu li[role=menuitem] > .menu-label", has_text="篩選…"
            ).first.click()
            expect(dialog).to_have_count(1)
            dialog.get_by_role("tab", name=tab).click()
            deadline = time.monotonic() + 30
            while not held:
                assert time.monotonic() < deadline, "對話框沒有查詢資料的範圍"
                page.wait_for_timeout(20)
            expect(low).to_have_value("")

        def release() -> None:
            # 範圍的提示與方塊的預設值在同一次畫面更新：看到提示時方塊已經是範圍到了之後的樣子
            held.pop().continue_()
            expect(dialog.locator(".hint", has_text="資料的範圍")).to_be_visible()

        def cancel() -> None:
            page.locator(".modal-backdrop").get_by_role("button", name="取消").click()
            expect(dialog).to_have_count(0)

        # 沒有動過：兩個方塊都填上資料的範圍（原本的行為）
        open_dialog()
        release()
        assert (float(low.input_value()), float(high.input_value())) == (least, most)
        cancel()

        # 點進最小值的方塊之後範圍才到，接著一個字一個字打
        open_dialog()
        low.click()
        release()
        page.keyboard.type("600000")
        expect(low).to_have_value("600000")
        assert float(high.input_value()) == most  # 沒有點進去的方塊照常填上
        cancel()

        # 打到一半範圍到了，再打完
        open_dialog()
        low.click()
        page.keyboard.type("600")
        release()
        page.keyboard.type("000")
        expect(low).to_have_value("600000")
        cancel()

        # 打了又刪掉（留空＝不限），焦點離開之後範圍才到：仍是空的
        open_dialog()
        low.click()
        page.keyboard.type("6")
        page.keyboard.press("Backspace")
        page.locator(".modal header h2").click()
        release()
        expect(low).to_have_value("")
        assert float(high.input_value()) == most
        cancel()

        # 「至少」分頁，像 fill 一樣：點進去、全選，範圍到了之後才插入文字；存起來的下限是 600000
        open_dialog("至少")
        low.focus()
        low.evaluate("e => e.select()")
        release()
        page.keyboard.insert_text("600000")
        expect(low).to_have_value("600000")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)
        page.wait_for_timeout(300)
        settle(page)
        saved = call(engine, f"/api/edit/demo/worksheets/{ws}")["worksheet"]["filters"]
        assert [(f["kind"], f["min"], f["max"]) for f in saved] == [("range", 600000, None)]
        assert errors == []
        browser.close()
