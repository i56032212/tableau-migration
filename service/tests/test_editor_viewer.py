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
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        assert pills(page, "columns") == ["SUM(Sales)"]
        page.get_by_role("button", name="↷ 重做").click()
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
        page.get_by_role("button", name="建立計算欄位…").click()
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
        page.get_by_role("button", name="新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_category"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)
        assert (pills(page, "rows"), pills(page, "columns")) == (["Category"], ["SUM(Sales)"])
        assert len(table("ws_1")["rows"]) == 3
        name = page.get_by_label("工作表名稱")
        name.fill("類別銷售")
        name.press("Enter")
        settle(page)
        expect(page.locator(".toolbar select")).to_contain_text("工作表：類別銷售")

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
        page.get_by_role("button", name="格式…").click()
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
        page.get_by_role("button", name="新增工作表").click()
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
        page.get_by_role("button", name="新增工作表").click()
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
        page.get_by_role("button", name="標題…").click()
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
        page.get_by_label("以浮動方式加入").check()
        page.locator(".object-buttons").get_by_role("button", name="文字").click()
        page.wait_for_selector('[data-saving="false"]')
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
        page.get_by_role("button", name="新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_region"]').dblclick()
        settle(page)
        # 資料窗格切到 Targets，把 Target 放到欄：混合（以同名的 Region 連結）
        page.get_by_label("資料源").select_option(secondary)
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

        page.locator('[data-tree-zone="z12"]').click()
        relevant = page.locator(".dash-props").get_by_label("只顯示相關的值")
        relevant.click()
        expect(relevant).to_be_checked()
        settle(page)
        assert find(board()["zones"], "z12")["relevant"] is True
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
        page.locator('[data-tree-zone="z10"]').click()
        expect(page.locator('[data-zone-props="z10"]')).to_be_visible()
        page.locator(".dash-props").get_by_role("button", name="移除").click()
        settle(page)
        assert "z10" not in zone_ids(board()["zones"])

        # 以浮動方式加入文字，拖曳移動
        page.get_by_label("以浮動方式加入").check()
        page.locator(".object-buttons").get_by_role("button", name="文字").click()
        settle(page)
        floating = board()["zones"][1]
        assert floating["kind"] == "text"
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
        page.get_by_label("以浮動方式加入").uncheck()
        page.locator('.dash-side [data-sheet="ws_sub_category_profit"]').drag_to(
            page.locator('[data-overlay="z9"]')
        )
        settle(page)
        sheets = [z for z in zone_ids(board()["zones"])]
        assert len(sheets) == len(set(sheets))
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(4)

        # 復原（加入的工作表）再重做
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(3)
        page.get_by_role("button", name="↷ 重做").click()
        settle(page)
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(4)

        # 動作：點選 Sales by Region 的標記篩選 Region Share by Year
        page.get_by_role("button", name="動作…").click()
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
        page.get_by_label("以浮動方式加入").check()
        page.locator(".object-buttons").get_by_role("button", name="匯出按鈕").click()
        settle(page)

        def button() -> dict[str, Any]:
            return next(z for z in board()["zones"] if z["kind"] == "button")["button"]

        assert (button()["action"], button()["export"]) == ("export", "pdf")
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
        page.locator(".object-buttons").get_by_role("button", name="網頁").click()
        settle(page)
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
        page.get_by_role("button", name="動作…").click()
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
        page.get_by_label("彙總量值").uncheck()
        settle(page)
        saved = sheet()
        assert saved["disaggregated"] is True
        assert saved["columns"][0]["agg"] is None
        page.get_by_label("彙總量值").check()
        settle(page)
        assert sheet()["columns"][0]["agg"] == "sum"

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
    from playwright.sync_api import Page, sync_playwright

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
        page.get_by_role("button", name="預測 ✓").click()
        page.locator(".forecast-dialog").get_by_role("button", name="移除預測").click()
        settle(page)
        assert sheet("ws_monthly_profit")["forecast"] is None

        # 叢集：Sub-Category 依檢視中的量值（SUM(Profit) 等）分成 3 群
        page.goto(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)
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
        # 只剩德州與德州的城市；矩形選取全部：兩個圖層的標記都選到
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        page.wait_for_selector('.single-sheet .chart[data-zoomable="true"]', timeout=30000)
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
        page.get_by_role("button", name="新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_region"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)

        # 總計：欄總計放在上方，Sales 的總計改為各標記的平均
        page.get_by_role("button", name="總計…").click()
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
        page.get_by_role("button", name="總計…").click()
        page.locator(".totals-dialog").get_by_label("SUM(Sales)的總計使用").select_option("")
        confirm(page, ".totals-dialog")
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
        page.get_by_role("button", name="說明…").click()
        page.locator(".caption-dialog").get_by_label("顯示說明").check()
        confirm(page, ".caption-dialog")
        assert sheet()["caption_shown"] is True and sheet()["caption"] == []
        note = page.locator(".editor .worksheet-caption")
        expect(note).to_contain_text("每個 Region 的 Sales")
        expect(note).to_contain_text("檢視依 Category 篩選")
        page.get_by_role("button", name="說明…").click()
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
        page.get_by_role("button", name="複製格式").click()
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        before = call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]
        page.get_by_role("button", name="貼上格式").click()
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
        page.get_by_role("button", name="格式…").click()
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
        page.get_by_role("button", name="新增工作表").click()
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
        page.get_by_label("堆疊標記", exact=True).select_option("off")
        settle(page)
        assert sheet()["stack"] == "off"
        assert spec()["encoding"][measure]["stack"] is None
        page.get_by_label("堆疊標記", exact=True).select_option("")
        settle(page)
        assert sheet()["stack"] is None

        # 色彩上的多個欄位：Segment 放到詳細資料，再從膠囊選單「也用於色彩」
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="marks.detail"]')
        )
        settle(page)
        page.locator('[data-drop="marks.detail"] .pill').click()
        menu(page, "也用於色彩（組合著色）")
        settle(page)
        assert [u["field"] for u in sheet()["marks"]["color_more"]] == ["f_segment"]
        expect(page.locator('[data-drop="marks.detail"] .pill-slot.also-color')).to_have_count(1)
        color = spec()["encoding"]["color"]
        assert color["field"] == "__color1" and color["title"] == "Region, Segment"
        assert all(", " in member for member in color["scale"]["domain"])
        # 再選一次即拿掉
        page.locator('[data-drop="marks.detail"] .pill').click()
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
        page.get_by_role("button", name="新增工作表").click()
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
        page.get_by_role("button", name="格式…").click()
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
        page.get_by_role("button", name="檢視資料…").click()
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
        page.get_by_role("button", name="新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        for field in ("f_region", "f_sales"):
            page.locator(f'.data-pane [data-field="{field}"]').dblclick()
            settle(page)
        natural = regions()
        assert natural == sorted(natural)

        # 工具列的排序：Region 依 SUM(Sales) 由大到小、由小到大，再清除
        page.get_by_role("button", name="遞減排序").click()
        settle(page)
        sort = sheet()["sorts"][0]
        assert (sort["field"], sort["kind"], sort["order"]) == ("f_region", "by_field", "desc")
        assert sort["by"]["field"] == "f_sales" and sort["nested"] is False
        descending = regions()
        page.get_by_role("button", name="遞增排序").click()
        settle(page)
        assert regions() == descending[::-1]
        page.get_by_role("button", name="清除排序").click()
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
        page.get_by_label("隱藏工作表").check()
        settle(page)
        page.get_by_label("分頁顏色").fill("#ff8800")
        settle(page)
        page.wait_for_timeout(700)
        saved = sheet()
        assert (saved["hidden"], saved["tab_color"]) == (True, "#ff8800")
        option = page.locator("select.view-select option", has_text="工作表 1")
        if option.count():
            assert "（隱藏）" in (option.first.text_content() or "")

        # 複製成交叉資料表：開啟新的工作表（文字表）
        page.get_by_role("button", name="複製成交叉資料表").click()
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
        page.get_by_role("button", name="新增工作表").click()
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

        # 摘要卡：各量值的總和、平均等
        page.get_by_label("顯示摘要").click()  # 存檔之後才勾選
        expect(page.get_by_label("顯示摘要")).to_be_checked()
        settle(page)
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
        page.get_by_role("button", name="格式…", exact=True).click()
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
        page.get_by_role("button", name="清除格式").click()
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
        page.get_by_role("button", name="匯入工作表…").click()
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
        assert {item["width"] for item in drawn} == {"10"}
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
        page.locator('[data-drop="marks.shape"] .pill').first.click()
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
