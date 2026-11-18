"""編輯器端到端測試共用的操作：功能表列、工具列、側欄的分頁、資料窗格的選單與下方的分頁列。

編輯器的排列與 Tableau Desktop 相同：命令在功能表列（檔案、資料、工作表、分析、格式…）或圖示工具列，
左側的側欄有分頁（工作表：資料／分析；dashboard：dashboard／版面配置），下方是分頁列。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


def menu_item(page: Page, menu: str, *labels: str) -> Locator:
    """開啟功能表列的選單（例如「分析」），依序停留在子選單上，回傳最後一個項目。"""
    page.locator(".menu-bar").get_by_role("menuitem", name=menu, exact=True).click()
    item = popup_item(page, labels[0])
    for label in labels[1:]:
        item.hover()
        item = popup_item(page, label)
    return item


def menubar(page: Page, menu: str, *labels: str) -> None:
    """執行功能表列的命令：menubar(page, "分析", "總計", "總計…")。"""
    menu_item(page, menu, *labels).click()


def popup_item(page: Page, label: str) -> Locator:
    """開啟的選單中名稱完全相同的項目。"""
    return page.locator(
        ".popup-menu li[role=menuitem]",
        has=page.locator("> .menu-label", has_text=re.compile(f"^{re.escape(label)}$")),
    ).first


def tool(page: Page, name: str) -> Locator:
    """工具列上的按鈕（圖示按鈕的名稱在 aria-label）。"""
    return page.locator(".ws-toolbar").get_by_role("button", name=name, exact=True)


def side_tab(page: Page, name: str) -> None:
    """切換左側側欄的分頁（工作表：資料／分析；dashboard：dashboard／版面配置）。"""
    page.locator(".side-tabs").get_by_role("tab", name=name, exact=True).click()


def pane_menu(page: Page, label: str) -> None:
    """資料窗格搜尋框旁邊的選單（建立計算欄位、參數、資料夾，日期屬性…）。"""
    page.get_by_role("button", name="資料窗格的選單").click()
    popup_item(page, label).click()


def sheet_tab(page: Page, name: str) -> Locator:
    """下方分頁列中的分頁（工作表、dashboard、Story 的名稱，或「資料來源」）。"""
    return page.locator(".sheet-tabs").get_by_role("tab", name=name, exact=True)


def current_tab(page: Page) -> Locator:
    """下方分頁列中目前的分頁。"""
    return page.locator(".sheet-tabs .sheet-tab.current")


def tab_menu(page: Page, tab: Locator, label: str) -> None:
    """分頁的右鍵選單中的命令（重新命名、複製、隱藏工作表、分頁顏色…、刪除）。"""
    tab.click(button="right")
    popup_item(page, label).click()


def rename_tab(page: Page, tab: Locator, name: str) -> None:
    """連按兩下分頁改名。"""
    tab.dblclick()
    entry = page.locator(".sheet-tabs .renaming input")
    entry.fill(name)
    entry.press("Enter")
