"""伺服器上的 Published Data Source（端到端）：管理員在站台管理中發布 .tdsx、設定權限，資料源頁面列出用到它的
報表，排程管理可以為資料源新增萃取更新。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_published_datasources import DIRECTORY, PERMISSIONS, fixtures, tdsx  # noqa: F401

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig_service.auth import AuthSettings
from tmig_service.background import running_server
from tmig_service.datasources import DatasourceStore
from tmig_service.store import ReportStore

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"


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


def test_publish_and_manage_datasource(fixtures: Path, tdsx: Path, tmp_path: Path) -> None:  # noqa: F811
    from playwright.sync_api import expect, sync_playwright

    settings = AuthSettings(
        mode="dev",
        directory=Directory.model_validate(DIRECTORY),
        permissions=Permissions.model_validate(PERMISSIONS),
    )
    store = ReportStore(tmp_path / "store")
    with running_server(fixtures, DIST, auth=settings, store=store) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option("select[aria-label='切換使用者']", "amy")

        # 站台管理：發布資料源
        page.goto(f"{url}/admin")
        page.get_by_role("button", name="發布資料源…").click()
        dialog = page.get_by_role("dialog", name="發布資料源")
        dialog.get_by_label("資料源檔案").set_input_files(str(tdsx))
        expect(dialog.get_by_label("資料源名稱")).to_have_value("OrdersPDS")
        dialog.get_by_label("資料源名稱").fill("訂單")
        dialog.get_by_role("button", name="發布").click()
        expect(page.locator(".success")).to_contain_text("已發布資料源「訂單」第 1 版")
        row = page.locator("tr[data-datasource]")
        expect(row).to_contain_text("訂單")
        (record,) = DatasourceStore(tmp_path / "store").records()
        assert (record.name, record.project, record.owner) == ("訂單", "default", "amy")

        # 權限：Analysts 套用「探索」範本
        row.get_by_role("button", name="權限").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("新增對象").select_option(label="群組：Analysts")
        dialog.get_by_role("button", name="新增", exact=True).click()
        dialog.get_by_label("Analysts 範本").select_option("探索")
        dialog.get_by_role("button", name="儲存").click()
        expect(page.locator(".success")).to_have_text("已更新權限")
        updated = DatasourceStore(tmp_path / "store").get(record.id)
        assert updated is not None
        analysts = next(r for r in updated.rules if r.grantee.name == "Analysts")
        assert analysts.capabilities == {"Read": "Allow", "Connect": "Allow", "ExportXml": "Allow"}

        # 資料源頁面：用到它的報表
        page.goto(f"{url}/")
        page.click("text=資料源（1）")
        listed = page.locator(f"tr[data-datasource='{record.id}']")
        expect(listed).to_contain_text("萃取（oracle）")
        expect(listed.get_by_role("link", name="report")).to_be_visible()
        listed.get_by_role("button", name="訂單").click()
        expect(page.get_by_role("dialog")).to_contain_text("Profit Ratio")
        page.keyboard.press("Escape")

        # 排程管理：為資料源新增萃取更新（更新伺服器上的那一份）
        page.goto(f"{url}/admin/schedules")
        page.get_by_label("排程的時區").fill("Asia/Taipei")
        page.get_by_role("button", name="儲存").click()
        page.get_by_role("button", name="新增排程").click()
        page.get_by_role("dialog").get_by_label("名稱").fill("每天")
        page.get_by_role("dialog").get_by_role("button", name="儲存").click()
        page.click("nav.tabs >> text=工作")
        page.get_by_role("button", name="新增萃取更新").click()
        page.get_by_role("dialog").get_by_label("報表").select_option(label="資料源：訂單")
        page.get_by_role("dialog").get_by_role("button", name="新增").click()
        expect(page.locator("tr[data-task]")).to_have_text(re.compile("^資料源：訂單每天"))
        browser.close()
