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
from editor_ui import current_tab, menu_item, menubar, sheet_tab, side_tab, tab_menu, tool

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
