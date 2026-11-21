"""桌面版編輯器的工作區排列（與 Tableau Desktop 相同）的端到端：功能表列開啟並執行命令、圖示工具列、
左側側欄的「資料」「分析」分頁、標記卡的屬性按鈕、下方的分頁列（列出工作表並切換、右鍵選單、新增），
dashboard 編輯器側欄的「dashboard」「版面配置」分頁，以及 Story 與資料來源頁面共用的功能表列與分頁列。
需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from editor_ui import (
    current_tab,
    menu_item,
    menubar,
    rename_tab,
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
TOKEN = "workspace-e2e-token-0123456789"


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


def call(base: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


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


def test_workspace_arrangement(engine: str) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def sheet(worksheet: str = "ws_sales_by_region") -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{worksheet}")["worksheet"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)

        # 由上到下：功能表列、工具列、主體（側欄、卡片欄、檢視）、分頁列、狀態列；沒有檢視器的工具列
        expect(page.locator(".page > .toolbar")).to_have_count(0)
        parts = page.locator(".editor > *").evaluate_all(
            "nodes => nodes.map((n) => n.className.split(' ')[0])"
        )
        assert parts[:5] == ["menu-bar", "ws-toolbar", "ws-body", "sheet-tabs", "status-bar"]
        columns = page.locator(".editor > .ws-body > *").evaluate_all(
            "nodes => nodes.map((n) => n.className.split(' ')[0])"
        )
        assert columns == ["side-pane", "cards-pane", "view-pane"]
        assert page.locator(".cards-pane [data-shelf]").evaluate_all(
            "nodes => nodes.map((n) => n.dataset.shelf)"
        ) == ["pages", "filters", "marks"]
        assert page.locator(".view-pane > .shelf").evaluate_all(
            "nodes => nodes.map((n) => n.dataset.shelf)"
        ) == ["columns", "rows"]

        # 功能表列：文字的功能表；開啟「分析＞總計」執行「顯示列總計」，再開啟時已勾選
        bar = page.locator(".menu-bar")
        assert bar.get_by_role("menuitem").all_inner_texts() == [
            "檔案",
            "資料",
            "工作表",
            "dashboard",
            "Story",
            "分析",
            "地圖",
            "格式",
            "伺服器",
            "視窗",
        ]
        assert sheet()["totals"] == []
        menubar(page, "分析", "總計", "顯示列總計")
        settle(page)
        assert sheet()["totals"] == ["columns"]
        expect(page.locator(".popup-menu")).to_have_count(0)
        expect(menu_item(page, "分析", "總計", "顯示列總計")).to_have_attribute(
            "aria-checked", "true"
        )
        page.keyboard.press("Escape")
        expect(page.locator(".popup-menu")).to_have_count(0)
        # 選單開著時滑到另一個功能表就換過去；再按一次同一個功能表關閉
        bar.get_by_role("menuitem", name="檔案", exact=True).click()
        expect(page.locator(".popup-menu .menu-label", has_text="另存新檔…")).to_be_visible()
        bar.get_by_role("menuitem", name="格式", exact=True).hover()
        expect(page.locator(".popup-menu .menu-label", has_text="複製格式")).to_be_visible()
        expect(page.locator(".popup-menu .menu-label", has_text="另存新檔…")).to_have_count(0)
        bar.get_by_role("menuitem", name="格式", exact=True).click()
        expect(page.locator(".popup-menu")).to_have_count(0)

        # 工具列：一列圖示按鈕（名稱在 aria-label 與提示文字），最右邊是「智慧顯示」
        toolbar = page.get_by_role("toolbar", name="工作表編輯")
        names = toolbar.get_by_role("button").evaluate_all(
            "nodes => nodes.map((n) => n.getAttribute('aria-label') ?? n.innerText.trim())"
        )
        assert names == [
            "復原",
            "重做",
            "儲存",
            "新增資料源…",
            "新增工作表",
            "複製",
            "清除",
            "交換",
            "遞增排序",
            "遞減排序",
            "智慧顯示",
        ]
        assert toolbar.locator(".tool-button svg.icon").count() >= 10
        assert tool(page, "儲存").get_attribute("title") == "儲存"
        expect(toolbar.get_by_label("顯示大小")).to_have_value("standard")
        tool(page, "復原").click()
        settle(page)
        assert sheet()["totals"] == []

        # 側欄的分頁：資料（資料源、搜尋、維度在上、量值在下）與分析（摘要、模型、自訂）
        tabs = page.locator(".side-pane .side-tabs").get_by_role("tab")
        assert tabs.all_inner_texts() == ["資料", "分析"]
        expect(page.locator(".data-pane")).to_be_visible()
        sections = page.locator(".data-pane .pane-section").all_inner_texts()
        assert [text.split()[0] for text in sections] == ["維度", "量值", "參數"]
        side_tab(page, "分析")
        expect(page.locator(".data-pane")).to_be_hidden()
        analytics = page.locator(".analytics-pane")
        assert analytics.locator(".pane-section").all_inner_texts() == [
            "摘要",
            "模型",
            "自訂",
            "選項",
        ]
        for name in ["總計…", "＋ 趨勢線…", "＋ 預測…", "＋ 叢集…", "＋ 參考線…", "＋ 註解…"]:
            expect(analytics.get_by_role("button", name=name)).to_be_visible()
        analytics.get_by_label("顯示欄總計").check()
        settle(page)
        assert sheet()["totals"] == ["rows"]
        side_tab(page, "資料")
        expect(analytics).to_have_count(0)
        expect(page.locator('.data-pane [data-field="f_segment"]')).to_be_visible()

        # 標記卡：標記類型、三個一列的屬性按鈕（放下欄位的目標）、卡上的膠囊前面是所在屬性的圖示
        card = page.locator('.cards-pane [data-shelf="marks"]')
        expect(card.get_by_label("標記類型")).to_be_visible()
        assert card.locator(".mark-buttons .mark-button").all_inner_texts() == [
            "色彩",
            "大小",
            "標籤",
            "詳細資料",
            "工具提示",
        ]
        boxes = [b.bounding_box() for b in card.locator(".mark-buttons .mark-button").all()]
        assert all(b is not None for b in boxes)
        tops = [round(b["y"]) for b in boxes if b is not None]
        assert tops[0] == tops[1] == tops[2] and tops[3] == tops[4] and tops[3] > tops[0]
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="marks.detail"]')
        )
        settle(page)
        assert [u["field"] for u in sheet()["marks"]["detail"]] == ["f_segment"]
        detail = card.locator('.mark-pills [data-slot="detail"]')
        expect(detail.locator(".pill-text")).to_have_text("Segment")
        expect(detail.locator(".mark-pill-icon svg")).to_be_visible()
        # 「形狀」的按鈕只有形狀的標記才有
        card.get_by_label("標記類型").select_option("shape")
        settle(page)
        expect(card.locator('.mark-buttons [data-drop="marks.shape"]')).to_be_visible()

        # 分頁列：最左邊是「資料來源」，接著每張工作表與 dashboard 一個分頁，目前的分頁標示出來
        tabs = page.locator(".sheet-tabs").get_by_role("tab")
        assert tabs.all_inner_texts() == [
            "資料來源",
            "Sales by Region",
            "Monthly Profit",
            "Region Share by Year",
            "Sub-Category Profit",
            "銷售總覽",
        ]
        expect(current_tab(page)).to_have_text("Sales by Region")
        assert page.locator(".sheet-tab-add").get_by_role("button").evaluate_all(
            "nodes => nodes.map((n) => n.getAttribute('aria-label'))"
        ) == ["新增工作表", "新增 dashboard", "新增 Story"]
        expect(page.locator(".status-bar")).to_contain_text(re.compile(r"摘要資料 \d+ 列"))
        # 按分頁切換到另一張工作表的編輯器
        sheet_tab(page, "Monthly Profit").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        expect(current_tab(page)).to_have_text("Monthly Profit")
        expect(page.locator(".editor")).to_have_attribute("data-editor", "ws_monthly_profit")
        # 分頁的右鍵選單：重新命名（可以復原）
        tab_menu(page, current_tab(page), "重新命名")
        entry = page.get_by_label("工作表名稱")
        entry.fill("每月利潤")
        entry.press("Enter")
        settle(page)
        expect(sheet_tab(page, "每月利潤")).to_have_attribute("aria-selected", "true")
        assert sheet("ws_monthly_profit")["name"] == "每月利潤"
        tool(page, "復原").click()
        settle(page)
        expect(sheet_tab(page, "Monthly Profit")).to_have_attribute("aria-selected", "true")
        # 隱藏工作表：分頁以斜體標示；復原之後分頁跟著改回來
        tab_menu(page, current_tab(page), "隱藏工作表")
        settle(page)
        expect(current_tab(page)).to_have_class(re.compile(r"\bhidden-sheet\b"))
        assert sheet("ws_monthly_profit")["hidden"] is True
        tool(page, "復原").click()
        settle(page)
        expect(current_tab(page)).not_to_have_class(re.compile(r"\bhidden-sheet\b"))
        assert sheet("ws_monthly_profit").get("hidden") is not True
        # 另一個分頁（不是目前的）也可以用右鍵選單改名
        tab_menu(page, sheet_tab(page, "Sub-Category Profit"), "重新命名")
        entry = page.get_by_label("工作表名稱")
        entry.fill("子類別利潤")
        entry.press("Enter")
        expect(sheet_tab(page, "子類別利潤")).to_be_visible()
        assert sheet("ws_sub_category_profit")["name"] == "子類別利潤"
        # 分頁列的「新增工作表」
        page.locator(".sheet-tab-add").get_by_role("button", name="新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        expect(current_tab(page)).to_have_text("工作表 1")
        expect(page.locator(".status-bar")).to_have_text("")

        # dashboard：同樣的功能表列、工具列與分頁列；側欄是「dashboard」與「版面配置」，右邊沒有屬性窗格
        sheet_tab(page, "銷售總覽").click()
        page.wait_for_url(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector('.dash-editor[data-saving="false"]')
        expect(current_tab(page)).to_have_text("銷售總覽")
        assert page.locator(".dash-editor > .ws-body > *").evaluate_all(
            "nodes => nodes.map((n) => n.className.split(' ')[0])"
        ) == ["dash-side", "dash-canvas"]
        side = page.locator(".dash-side")
        assert side.get_by_role("tab").all_inner_texts() == ["dashboard", "版面配置"]
        assert side.locator(".pane-section").all_inner_texts() == [
            "裝置版面",
            "大小",
            "工作表",
            "物件",
            "篩選、參數與圖例",
        ]
        expect(side.get_by_label("dashboard 的寬度")).to_have_value("1200")
        expect(side.locator('[data-sheet="ws_sales_by_region"]')).to_be_visible()
        expect(
            side.get_by_role("group", name="新物件的放置方式").get_by_role("button")
        ).to_have_text(["並排", "浮動"])
        assert page.locator(".dash-editor .menu-bar").get_by_role("menuitem").all_inner_texts() == [
            "檔案",
            "資料",
            "工作表",
            "dashboard",
            "Story",
            "伺服器",
            "視窗",
        ]
        side_tab(page, "版面配置")
        expect(side.locator('[data-sheet="ws_sales_by_region"]')).to_have_count(0)
        expect(side.locator(".dash-props")).to_contain_text("點選畫面上的區塊")
        assert side.locator(".pane-section").all_inner_texts() == ["項目階層", "格線"]
        side.locator(".layout-tree li", has_text="Region Share by Year").first.click()
        expect(side.locator(".dash-props [data-zone-props]")).to_be_visible()
        expect(side.get_by_label("區塊的工作表")).to_have_value("ws_region_share_by_year")
        # 選取項目的屬性在項目階層的上面
        props = side.locator(".dash-props").bounding_box()
        tree = side.locator(".layout-tree").bounding_box()
        assert props is not None and tree is not None and props["y"] < tree["y"]

        # Story：分頁列的「新增 Story」；同樣的功能表列與分頁列
        page.locator(".sheet-tab-add").get_by_role("button", name="新增 Story").click()
        page.wait_for_url(re.compile(r"/w/demo/edit-story/"))
        page.wait_for_selector('.story-editor[data-saving="false"]')
        expect(current_tab(page)).to_have_class(re.compile(r"\btab-story\b"))
        expect(page.get_by_role("toolbar", name="Story 編輯")).to_be_visible()
        expect(menu_item(page, "Story", "格式…")).to_be_visible()
        page.keyboard.press("Escape")

        # 資料來源：最左邊的分頁；同樣的功能表列與分頁列，「完成」回到檢視
        sheet_tab(page, "資料來源").click()
        page.wait_for_url(f"{engine}/w/demo/edit-ds/ds_orders")
        expect(page.locator(".ds-workspace .ds-editor")).to_be_visible()
        expect(sheet_tab(page, "資料來源")).to_have_attribute("aria-selected", "true")
        expect(page.locator(".ds-workspace .menu-bar").get_by_role("menuitem").first).to_have_text(
            "檔案"
        )
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/ds/ds_orders")
        expect(page.locator(".page > .toolbar")).to_be_visible()
        assert errors == []
        browser.close()


def test_new_dashboard_takes_dropped_sheets_and_home_has_the_menu_bar(engine: str) -> None:
    """新增的 dashboard：把兩張工作表拖進空白的畫布，兩張都有大小（第二張加入時，容器尾端的留白不能把
    還沒有大小的新區塊算進去）。桌面版不在編輯器裡的頁面（報表清單）也有功能表列——外殼沒有原生選單，
    檔案與伺服器的指令只在這一排；檔案指令的快捷鍵由頁面處理。"""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        def settle() -> None:
            page.wait_for_selector('[data-saving="false"]')
            page.wait_for_load_state("networkidle")

        # 報表清單：功能表列有檔案與伺服器（說明只有在外殼中才有項目）
        bar = page.locator(".menu-bar")
        expect(bar.get_by_role("menuitem")).to_have_text(["檔案", "伺服器"])
        expect(bar.get_by_role("button", name="完成")).to_have_count(0)
        bar.get_by_role("menuitem", name="伺服器").click()
        expect(page.locator(".popup-menu li[role=menuitem]")).to_have_text(
            ["登入…", "從伺服器開啟…", "發布…", "發布資料源…", "登出"]
        )
        page.keyboard.press("Escape")
        # 還沒開啟報表時按 Ctrl+S：提示先開啟一份報表（快捷鍵有作用）
        page.keyboard.press("Control+s")
        expect(page.get_by_text("請先開啟一份報表")).to_be_visible()

        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle()
        page.locator(".sheet-tab-add").get_by_role("button", name="新增 dashboard").click()
        page.wait_for_selector(".dash-editor")
        settle()
        box = page.locator(".dash-editor .dashboard").bounding_box()
        assert box is not None
        for sheet, at in (("ws_sales_by_region", 0.5), ("ws_monthly_profit", 0.8)):
            page.locator(f'.dash-side [data-sheet="{sheet}"]').hover()
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * at, box["y"] + box["height"] / 2, steps=12)
            page.mouse.up()
            settle()
        sizes = page.locator(".dash-editor .dashboard [data-zone]").evaluate_all(
            "nodes => nodes.map((n) => [n.getBoundingClientRect().width, n.getBoundingClientRect().height])"
        )
        assert len(sizes) == 2 and all(w > 100 and h > 100 for w, h in sizes), sizes
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(2)
        assert errors == []
        browser.close()


def test_sheet_tabs_delete_rename_and_scroll(engine: str) -> None:
    """分頁列（2026-11-21 稽核）：連按兩下「還不是目前的」分頁會切過去並開始改名（原本只切換）；刪除目前的
    工作表之後留在編輯器、開啟左邊的分頁（原本被丟到檢視器），與 Tableau 相同不先問（刪除可以復原，見
    test_deleted_sheets_come_back_with_undo）；分頁很多時目前的分頁捲進看得到的範圍（原本停在最左邊），
    按看得到的分頁時分頁列不跳動。"""
    from playwright.sync_api import Dialog, Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def names() -> list[str]:
        return [w["name"] for w in call(engine, "/api/workbooks/demo/ir")["worksheets"]]

    def tab_box(page: Page) -> dict[str, Any]:
        """目前的分頁在分頁列中的位置：是否完整在看得到的範圍內，與分頁列捲動的位置。"""
        return page.locator(".sheet-tab-list").evaluate(
            """box => {
              const tab = box.querySelector('.sheet-tab.current');
              const shown = box.getBoundingClientRect();
              const at = tab ? tab.getBoundingClientRect() : null;
              return {
                name: tab ? tab.innerText : null,
                visible: !!at && at.left >= shown.left - 1 && at.right <= shown.right + 1,
                scroll: Math.round(box.scrollLeft),
                overflow: box.scrollWidth > box.clientWidth,
              };
            }"""
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        asked: list[str] = []
        accept = [True]

        def answer(dialog: Dialog) -> None:
            asked.append(dialog.message)
            if accept[0]:
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", answer)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)

        # 連按兩下還不是目前的分頁：切過去，並進入改名（輸入框裡是原來的名稱）
        sheet_tab(page, "Monthly Profit").dblclick()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_monthly_profit")
        entry = page.locator(".sheet-tabs .renaming input")
        expect(entry).to_have_value("Monthly Profit")
        expect(entry).to_be_focused()
        entry.fill("每月利潤")
        entry.press("Enter")
        expect(current_tab(page)).to_have_text("每月利潤")
        settle(page)
        assert "每月利潤" in names() and "Monthly Profit" not in names()
        # 只按一下：切換分頁，不改名
        sheet_tab(page, "Sales by Region").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        page.wait_for_timeout(900)  # 等第二下的時間過去
        expect(page.locator(".sheet-tabs .renaming")).to_have_count(0)
        expect(current_tab(page)).to_have_text("Sales by Region")

        # 刪除目前的工作表：不先問（可以復原），刪掉之後留在編輯器，開啟左邊的分頁；訊息上可以復原
        for _ in range(2):
            tool(page, "新增工作表").click()
            page.wait_for_selector(".editor-canvas .placeholder")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_2")
        settle(page)
        accept[0] = False  # 萬一還跳出確認，按取消：就不會刪除
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        assert asked == []
        expect(current_tab(page)).to_have_text("工作表 1")
        expect(page.locator(".editor .menu-bar")).to_be_visible()
        expect(sheet_tab(page, "工作表 2")).to_have_count(0)
        assert "工作表 2" not in names()
        deleted = page.locator(".toast", has_text="已刪除工作表「工作表 2」")
        expect(deleted.get_by_role("button", name="復原")).to_be_visible()
        deleted.evaluate("element => element.remove()")
        # 刪除不是目前的分頁：留在原來的分頁。新的工作表是「工作表 3」：剛刪除的「工作表 2」還可以復原，
        # 名稱與 id 留給它
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_3")
        settle(page)
        tab_menu(page, sheet_tab(page, "工作表 1"), "刪除")
        expect(sheet_tab(page, "工作表 1")).to_have_count(0)
        assert page.url == f"{engine}/w/demo/edit/ws_3"
        expect(current_tab(page)).to_have_text("工作表 3")
        page.locator(".toast", has_text="已刪除工作表「工作表 1」").evaluate("e => e.remove()")
        # dashboard 還在用的工作表刪不掉：說明原因，留在原來的分頁
        tab_menu(page, sheet_tab(page, "Sales by Region"), "刪除")
        expect(page.locator(".toast")).to_contain_text("不能刪除")
        assert "Sales by Region" in names() and page.url == f"{engine}/w/demo/edit/ws_3"
        assert asked == []

        # 分頁很多：開啟最右邊的 dashboard，目前的分頁捲進看得到的範圍
        for _ in range(40):
            call(engine, "/api/edit/demo/worksheets", {})
        page.goto(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector('.dash-editor[data-saving="false"]')
        expect(current_tab(page)).to_have_text("銷售總覽")
        box = tab_box(page)
        assert box["overflow"] and box["visible"] and box["scroll"] > 0, box
        # 開啟中間的工作表（重新載入頁面）：也捲得到
        page.goto(f"{engine}/w/demo/edit/ws_20")
        settle(page)
        box = tab_box(page)
        assert box["name"] == "工作表 20" and box["visible"] and box["scroll"] > 0, box
        # 按另一個看得到的分頁：切過去，分頁列留在原來的位置
        sheet_tab(page, "工作表 15").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_15")
        settle(page)
        after = tab_box(page)
        assert after["name"] == "工作表 15" and after["visible"], after
        assert after["scroll"] == box["scroll"], (box, after)
        # 新增工作表（加在所有工作表的最後、畫面之外）：捲過去
        tool(page, "新增工作表").click()
        page.wait_for_selector(".editor-canvas .placeholder")
        settle(page)
        latest = tab_box(page)
        assert latest["visible"] and latest["scroll"] > after["scroll"], latest
        assert errors == []
        browser.close()


def test_deleted_sheets_come_back_with_undo(engine: str) -> None:
    """刪除工作表、dashboard、Story 可以復原（Tableau：刪除之後按 Ctrl+Z，它原樣回到原本的位置）。原本編輯的
    歷程是每個分頁各自的，刪除之後沒有東西可以復原。

    - 刪除不先問；右下角的訊息上有「復原」，工具列的復原與 Ctrl+Z 也可以
    - 復原之後報表的定義與刪除之前完全相同（id、名稱、定義、分頁的位置、其他工作表的工具提示），編輯器回到
      放回來的分頁；重做（工具列、Ctrl+Y）是再刪一次
    - 復原之後又改過報表就不能重做；刪除別的分頁時人留在原來的分頁，Ctrl+Z 先復原那次刪除，不是這個分頁
      之前的變更
    - 放不回去時（之後新增了同名的工作表）說明原因，報表不變"""
    from playwright.sync_api import Dialog, Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.workspace[data-saving="false"], .ds-workspace')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder, '
            ".dash-editor .dash-canvas, .story-editor .story-preview, .ds-workspace .ws-scroll",
            timeout=30000,
        )

    def ir() -> dict[str, Any]:
        return call(engine, "/api/workbooks/demo/ir")

    def names() -> list[str]:
        return [w["name"] for w in ir()["worksheets"]]

    def put(worksheet: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{worksheet['id']}",
            data=json.dumps({"worksheet": worksheet}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            assert response.status == 200

    def use(field: str, agg: str | None = None) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": None,
            "continuous": agg is not None,
            "table_calc": None,
        }

    # 「工作表 1」有內容（列、欄），在另一張工作表的工具提示裡，後面還有「工作表 2」
    first = call(engine, "/api/edit/demo/worksheets", {})["worksheet"]
    call(engine, "/api/edit/demo/worksheets", {})
    first["rows"] = [use("f_segment")]
    first["columns"] = [use("f_sales", "sum")]
    put(first)
    host = call(engine, "/api/edit/demo/worksheets/ws_monthly_profit")["worksheet"]
    host["marks"]["tooltip_sheets"] = [{"worksheet": "ws_1", "max_width": 260}]
    put(host)
    before = ir()
    order = [w["id"] for w in before["worksheets"]]
    assert order[-2:] == ["ws_1", "ws_2"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        asked: list[str] = []

        def answer(dialog: Dialog) -> None:
            asked.append(dialog.message)
            dialog.dismiss()  # 還跳出確認的話按取消：就不會刪除

        def tabs() -> list[str]:
            return page.locator(".sheet-tab-list .sheet-tab").all_inner_texts()

        def toast(text: str) -> Any:
            return page.locator(".toast", has_text=text)

        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", answer)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        expect(tool(page, "復原")).to_be_disabled()

        # 刪除目前的分頁：不問，開啟左邊的分頁；訊息與工具列都可以復原
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)
        assert asked == []
        gone = ir()
        assert [w["id"] for w in gone["worksheets"]] == [i for i in order if i != "ws_1"]
        monthly = next(w for w in gone["worksheets"] if w["id"] == "ws_monthly_profit")
        assert monthly["marks"]["tooltip_sheets"] == []
        expect(toast("已刪除工作表「工作表 1」").get_by_role("button", name="復原")).to_be_visible()
        expect(tool(page, "復原")).to_be_enabled()
        assert tool(page, "復原").get_attribute("title") == "復原刪除工作表「工作表 1」（Ctrl+Z）"
        expect(tool(page, "重做")).to_be_disabled()

        # Ctrl+Z：回到原本的位置（「工作表 2」之前），定義、工具提示都與刪除之前相同；編輯器開啟它
        page.keyboard.press("Control+z")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        assert ir() == before
        expect(current_tab(page)).to_have_text("工作表 1")
        assert tabs()[-3:-1] == ["工作表 1", "工作表 2"]
        expect(page.locator('[data-shelf="rows"] .pill')).to_have_count(1)
        expect(toast("已刪除工作表")).to_have_count(0)
        expect(toast("已復原工作表「工作表 1」")).to_be_visible()
        expect(tool(page, "復原")).to_be_disabled()

        # 重做（工具列）：再刪一次，結果與第一次相同；再以工具列復原
        expect(tool(page, "重做")).to_be_enabled()
        assert tool(page, "重做").get_attribute("title") == "重做刪除工作表「工作表 1」（Ctrl+Y）"
        tool(page, "重做").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)
        assert ir() == gone
        tool(page, "復原").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        assert ir() == before

        # 復原之後改了報表（交換欄與列）：不能再重做刪除；Ctrl+Z 復原的是這個變更
        expect(tool(page, "重做")).to_be_enabled()
        tool(page, "交換").click()
        settle(page)
        expect(tool(page, "重做")).to_be_disabled()
        swapped = ir()
        sheet = next(w for w in swapped["worksheets"] if w["id"] == "ws_1")
        assert sheet["rows"][0]["field"] == "f_sales"
        page.keyboard.press("Control+y")
        page.wait_for_timeout(400)
        assert ir() == swapped and page.url == f"{engine}/w/demo/edit/ws_1"
        page.keyboard.press("Control+z")
        expect(tool(page, "復原")).to_be_disabled()
        settle(page)
        assert ir() == before

        # 人在「工作表 1」（剛做了一個變更）時刪除別的分頁：Ctrl+Z 先復原那次刪除，這個分頁的變更還在
        tool(page, "交換").click()
        settle(page)
        tab_menu(page, sheet_tab(page, "工作表 2"), "刪除")
        expect(sheet_tab(page, "工作表 2")).to_have_count(0)
        assert page.url == f"{engine}/w/demo/edit/ws_1" and "工作表 2" not in names()
        page.keyboard.press("Control+z")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_2")
        settle(page)
        assert ir() == swapped
        expect(current_tab(page)).to_have_text("工作表 2")

        # 訊息上的「復原」
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        assert "工作表 2" not in names()
        toast("已刪除工作表「工作表 2」").get_by_role("button", name="復原").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_2")
        settle(page)
        assert ir() == swapped

        # 刪除之後新增工作表：新的是「工作表 3」（「工作表 2」的名稱與 id 留給還可以復原的那一張），
        # 所以在新的分頁上按 Ctrl+Z，刪除的那一張照樣回到原本的位置
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_3")
        settle(page)
        expect(current_tab(page)).to_have_text("工作表 3")
        page.keyboard.press("Control+z")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_2")
        settle(page)
        assert tabs()[-4:-1] == ["工作表 1", "工作表 2", "工作表 3"]
        with_third = ir()
        assert [w for w in with_third["worksheets"] if w["id"] != "ws_3"] == swapped["worksheets"]

        # 放不回去：刪除「工作表 2」之後把別的工作表改成同一個名稱——說明原因，報表不變；
        # 把名稱改回去之後就能復原
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        rename_tab(page, sheet_tab(page, "工作表 3"), "工作表 2")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_3")
        settle(page)
        expect(current_tab(page)).to_have_text("工作表 2")
        sheet_tab(page, "工作表 1").click()  # 換分頁：改名不在這個分頁的歷程裡
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        page.wait_for_timeout(900)  # 等連按兩下的時間過去
        taken = ir()
        page.keyboard.press("Control+z")
        expect(page.locator(".toast.error")).to_contain_text("已經有同名的工作表")
        assert ir() == taken and page.url == f"{engine}/w/demo/edit/ws_1"
        page.locator(".toast.error").evaluate("element => element.remove()")
        rename_tab(page, sheet_tab(page, "工作表 2"), "工作表 3")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_3")
        settle(page)
        expect(current_tab(page)).to_have_text("工作表 3")
        # 剛改的名稱是這個分頁最近的一步：第一次 Ctrl+Z 復原它，第二次才是那次刪除——所以到別的分頁再按
        sheet_tab(page, "工作表 1").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        page.wait_for_timeout(900)
        tool(page, "復原").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_2")
        settle(page)
        assert ir() == with_third

        # dashboard：分頁的右鍵選單刪除，Ctrl+Z 放回來
        page.locator(".sheet-tab-add").get_by_role("button", name="新增 dashboard").click()
        page.wait_for_url(re.compile(r"/w/demo/edit-d/"))
        settle(page)
        board = page.url.rsplit("/", 1)[1]
        with_board = ir()
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_url(f"{engine}/w/demo/edit-d/db_ee909b0c")
        settle(page)
        assert board not in [d["id"] for d in ir()["dashboards"]]
        expect(toast("已刪除 dashboard「儀表板 1」")).to_be_visible()
        page.keyboard.press("Control+z")
        page.wait_for_url(f"{engine}/w/demo/edit-d/{board}")
        settle(page)
        assert ir() == with_board
        expect(current_tab(page)).to_have_text("儀表板 1")

        # Story：功能表的「刪除 Story」也不先問、留在編輯器；工具列的復原放回來
        page.locator(".sheet-tab-add").get_by_role("button", name="新增 Story").click()
        page.wait_for_url(re.compile(r"/w/demo/edit-story/"))
        settle(page)
        story = page.url.rsplit("/", 1)[1]
        with_story = ir()
        menubar(page, "Story", "刪除 Story")
        page.wait_for_url(f"{engine}/w/demo/edit-d/{board}")
        settle(page)
        assert ir()["stories"] == []
        tool(page, "復原").click()
        page.wait_for_url(f"{engine}/w/demo/edit-story/{story}")
        settle(page)
        assert ir() == with_story

        # 資料來源分頁：刪除別的分頁之後，這裡的工具列也可以復原
        sheet_tab(page, "資料來源").click()
        page.wait_for_url(re.compile(r"/w/demo/edit-ds/"))
        settle(page)
        tab_menu(page, sheet_tab(page, "工作表 1"), "刪除")
        expect(sheet_tab(page, "工作表 1")).to_have_count(0)
        assert "工作表 1" not in names()
        tool(page, "復原").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        assert ir() == with_story
        assert asked == [] and errors == []
        browser.close()


def test_editor_follows_engine_changes_to_the_open_sheet(engine: str) -> None:
    """引擎在編輯器之外改了「正在編輯的」工作表時，編輯器跟上，不拿手上的舊定義存回去。

    引擎的一個操作會連帶改到別的工作表：刪除工作表時其他工作表的工具提示不再有它，刪除 dashboard 時它的動作在
    目標工作表上的篩選一併移除，計算欄位從彙總改成列層級時用到它的膠囊加上 SUM。原本編輯器不知道：

    - 刪除「不是目前的」工作表或 dashboard 之後，目前這張工作表的每個變更都存檔失敗（422「工作表的參照
      不正確」），換一次分頁才恢復；重做刪除也一樣
    - 計算欄位改成列層級之後，下一個變更把引擎加上的 SUM 蓋掉（量值被當成維度），沒有任何訊息

    現在引擎改過之後編輯器立刻跟上；歷程裡的每一步也套上引擎的改變，所以這張工作表自己的復原與重做照常
    可用，刪除也仍然可以復原（報表回到與刪除之前完全相同）。改別的分頁的名稱（不會動到目前的工作表）之後
    照常可以編輯。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.workspace[data-saving="false"], .ds-workspace')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder, '
            ".dash-editor .dash-canvas, .story-editor .story-preview, .ds-workspace .ws-scroll",
            timeout=30000,
        )

    def ir() -> dict[str, Any]:
        return call(engine, "/api/workbooks/demo/ir")

    def sheet(worksheet: str) -> dict[str, Any]:
        return next(w for w in ir()["worksheets"] if w["id"] == worksheet)

    def put(worksheet: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{engine}/api/edit/demo/worksheets/{worksheet['id']}",
            data=json.dumps({"worksheet": worksheet}).encode(),
            headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            assert response.status == 200

    def use(field: str, agg: str | None = None) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": None,
            "continuous": agg is not None,
            "table_calc": None,
        }

    def fields(uses: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
        return [(u["field"], u["agg"]) for u in uses]

    # 「工作表 1」在 Monthly Profit 的工具提示裡；「動作用」dashboard 的篩選動作以 Region Share by Year 為目標；
    # 「比率」的欄是彙總層級的計算欄位「利潤率」
    first = call(engine, "/api/edit/demo/worksheets", {})["worksheet"]
    first["rows"] = [use("f_segment")]
    first["columns"] = [use("f_sales", "sum")]
    put(first)
    host = call(engine, "/api/edit/demo/worksheets/ws_monthly_profit")["worksheet"]
    host["marks"]["tooltip_sheets"] = [{"worksheet": "ws_1", "max_width": 260}]
    put(host)
    board = call(engine, "/api/edit/demo/dashboards", {"name": "動作用"})["dashboard"]
    action = call(
        engine,
        "/api/edit/demo/actions",
        {
            "action": {
                "caption": "依區域",
                "kind": "filter",
                "activation": "select",
                "dashboard": board["id"],
                "sources": ["ws_sales_by_region"],
                "targets": ["ws_region_share_by_year"],
                "clearing": "show_all",
                "fields": ["f_region"],
            }
        },
    )["action"]
    catalog = call(engine, "/api/edit/demo/catalog")["datasources"][0]["fields"]
    ratio = next(f["id"] for f in catalog if f["caption"] == "利潤率")
    rate = call(engine, "/api/edit/demo/worksheets", {"name": "比率"})["worksheet"]
    rate["rows"] = [use("f_segment")]
    rate["columns"] = [{**use(ratio), "continuous": True}]
    put(rate)
    before = ir()
    monthly = sheet("ws_monthly_profit")
    rows, columns = fields(monthly["rows"]), fields(monthly["columns"])

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        failed: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "response",
            lambda r: (
                failed.append(f"{r.request.method} {r.url} {r.status}")
                if "/api/edit/" in r.url and r.status >= 400
                else None
            ),
        )

        def no_error() -> None:
            expect(page.locator(".toast.error")).to_have_count(0)
            assert failed == [] and errors == []

        def swap() -> None:
            tool(page, "交換").click()
            settle(page)

        def open_tab(name: str, path: str) -> None:
            sheet_tab(page, name).click()
            page.wait_for_url(f"{engine}/w/demo/{path}")
            settle(page)
            page.wait_for_timeout(700)  # 連按兩下（改名）的等待時間過去

        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)

        # 這張工作表先做一個變更再復原：歷程裡有一步可以重做（它是刪除之前的定義，工具提示裡還有「工作表 1」）
        swap()
        tool(page, "復原").click()
        settle(page)
        assert ir() == before
        # 刪除不是目前的「工作表 1」：引擎把它從這張工作表的工具提示拿掉，編輯器跟上
        tab_menu(page, sheet_tab(page, "工作表 1"), "刪除")
        expect(sheet_tab(page, "工作表 1")).to_have_count(0)
        settle(page)
        assert sheet("ws_monthly_profit")["marks"]["tooltip_sheets"] == []
        # 這張工作表自己的重做照常可用（原本：422「工作表的參照不正確」）
        assert tool(page, "重做").get_attribute("title") == "重做（Ctrl+Y）"
        tool(page, "重做").click()
        settle(page)
        no_error()
        swapped = sheet("ws_monthly_profit")
        assert (fields(swapped["rows"]), fields(swapped["columns"])) == (columns, rows)
        assert swapped["marks"]["tooltip_sheets"] == []
        # 新的變更也存得進去
        swap()
        no_error()
        assert fields(sheet("ws_monthly_profit")["rows"]) == rows
        # Ctrl+Z：先是這張工作表自己的兩步，再來是那次刪除——報表回到與刪除之前完全相同，
        # 這張工作表的工具提示裡又有「工作表 1」
        for _ in range(2):
            page.keyboard.press("Control+z")
            settle(page)
        no_error()
        assert sheet("ws_monthly_profit")["marks"]["tooltip_sheets"] == []
        assert page.url == f"{engine}/w/demo/edit/ws_monthly_profit"
        page.keyboard.press("Control+z")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        assert ir() == before

        # 重做刪除（人在工具提示裡有它的那張工作表上）：一樣跟上
        open_tab("Monthly Profit", "edit/ws_monthly_profit")
        assert tool(page, "重做").get_attribute("title") == "重做刪除工作表「工作表 1」（Ctrl+Y）"
        tool(page, "重做").click()
        expect(sheet_tab(page, "工作表 1")).to_have_count(0)
        settle(page)
        swap()
        no_error()
        assert fields(sheet("ws_monthly_profit")["rows"]) == columns
        assert sheet("ws_monthly_profit")["marks"]["tooltip_sheets"] == []
        page.keyboard.press("Control+z")
        settle(page)
        page.keyboard.press("Control+z")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        settle(page)
        assert ir() == before

        # 刪除 dashboard：它的篩選動作在目標工作表（正在編輯的這一張）上的篩選一併移除
        open_tab("Region Share by Year", "edit/ws_region_share_by_year")
        target = sheet("ws_region_share_by_year")
        assert [f["id"] for f in target["filters"] if f.get("action") == action["id"]]
        tab_menu(page, sheet_tab(page, "動作用"), "刪除")
        expect(sheet_tab(page, "動作用")).to_have_count(0)
        settle(page)
        swap()
        no_error()  # 原本：422「篩選動作…不存在」
        changed = sheet("ws_region_share_by_year")
        assert fields(changed["rows"]) == fields(target["columns"])
        assert all(f.get("action") is None for f in changed["filters"]) and ir()["actions"] == []
        page.keyboard.press("Control+z")
        settle(page)
        page.keyboard.press("Control+z")
        page.wait_for_url(f"{engine}/w/demo/edit-d/{board['id']}")
        settle(page)
        assert ir() == before

        # 改別的分頁的名稱、隱藏別的工作表（不會動到正在編輯的工作表）之後照常可以編輯
        open_tab("比率", f"edit/{rate['id']}")
        tab_menu(page, sheet_tab(page, "工作表 1"), "重新命名")
        entry = page.locator(".sheet-tabs .renaming input")
        entry.fill("細分")
        entry.press("Enter")
        expect(sheet_tab(page, "細分")).to_have_count(1)
        tab_menu(page, sheet_tab(page, "細分"), "隱藏工作表")
        expect(page.locator(".sheet-tabs .sheet-tab.hidden-sheet")).to_have_text("細分")
        settle(page)
        assert page.url == f"{engine}/w/demo/edit/{rate['id']}"

        # 計算欄位從彙總改成列層級：引擎替膠囊加上 SUM，編輯器的膠囊跟著變
        swap()  # 歷程裡較早的一步：「利潤率」還在欄架上
        no_error()
        assert fields(sheet(rate["id"])["rows"]) == [(ratio, None)]
        page.locator(f'.data-pane [data-field="{ratio}"]').click(button="right")
        page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text="編輯").first.click()
        page.get_by_label("公式").fill("[Profit] / [Sales]")
        expect(page.locator(".calc-status")).to_contain_text("列層級")
        page.locator(".calc-dialog").get_by_role("button", name="確定").click()
        expect(page.locator(".calc-dialog")).to_have_count(0)
        settle(page)
        assert fields(sheet(rate["id"])["rows"]) == [(ratio, "sum")]
        expect(page.locator('[data-shelf="rows"] .pill .pill-text')).to_have_text("SUM(利潤率)")
        # 復原這張工作表較早的一步（交換之前）：膠囊回到欄架上，引擎加上的 SUM 還在
        # （原本：存回手上的舊定義，量值沒有彙總、被當成維度）
        tool(page, "復原").click()
        settle(page)
        no_error()
        back = sheet(rate["id"])
        assert (fields(back["rows"]), fields(back["columns"])) == (
            [("f_segment", None)],
            [(ratio, "sum")],
        )
        expect(page.locator('[data-shelf="columns"] .pill .pill-text')).to_have_text("SUM(利潤率)")
        # 再做一個變更：存的仍是引擎調整過的膠囊
        swap()
        no_error()
        assert fields(sheet(rate["id"])["rows"]) == [(ratio, "sum")]
        browser.close()
