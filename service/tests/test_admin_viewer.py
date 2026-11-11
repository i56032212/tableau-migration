"""站台管理的端到端：管理員在畫面上新增 Project、設定權限、新增使用者與群組，看修改紀錄；
不是管理員的人看不到站台管理。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_site_admin import DIRECTORY, PERMISSIONS

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig_service.auth import AuthSettings
from tmig_service.background import running_server
from tmig_service.site import SiteStore
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


def test_admin_page(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
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
        page.click("text=站台管理")
        page.wait_for_selector("tr[data-project='p-sales']")
        expect(page.locator("tr[data-workbook='w-legacy']")).to_contain_text("舊報表")

        # 在 Sales 底下新增 Project
        page.locator("tr[data-project='p-sales']").get_by_role(
            "button", name="新增子 Project"
        ).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("名稱").fill("華北")
        dialog.get_by_role("button", name="確定").click()
        expect(page.locator("tr.project-row", has_text="華北")).to_be_visible()
        north = page.locator("tr.project-row", has_text="華北").get_attribute("data-project")
        assert north is not None

        # Project 的權限：Sales 群組可以發布
        page.locator(f"tr[data-project='{north}']").get_by_role("button", name="權限").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("新增對象").first.select_option(label="群組：Sales")
        dialog.get_by_role("button", name="新增", exact=True).first.click()
        dialog.get_by_label("Sales 範本").first.select_option("發布")
        expect(dialog.get_by_label("Sales 檢視").first).to_have_value("Allow")
        dialog.get_by_label("Sales Project Leader").select_option("Deny")
        # 報表的預設權限：套用「探索」範本後拒絕下載完整資料，範本顯示為自訂
        defaults = dialog.locator("table.permissions").nth(1)
        defaults.get_by_label("Sales 範本").select_option("探索")
        defaults.get_by_label("Sales 下載完整資料").select_option("Deny")
        expect(defaults.get_by_label("Sales 範本")).to_have_value("自訂")
        dialog.get_by_role("button", name="儲存").click()
        expect(page.locator(".success")).to_have_text("已更新權限")
        project = next(p for p in SiteStore(tmp_path / "store").data().projects if p.id == north)
        assert [r.capabilities for r in project.rules if r.grantee.name == "Sales"] == [
            {"Read": "Allow", "Write": "Allow", "ProjectLeader": "Deny"}
        ]
        sales = next(r for r in project.workbook_defaults if r.grantee.name == "Sales")
        assert sales.capabilities == {
            "Read": "Allow",
            "Filter": "Allow",
            "ViewComments": "Allow",
            "AddComment": "Allow",
            "ExportImage": "Allow",
            "ExportData": "Allow",
            "ViewUnderlyingData": "Deny",
            "ShareView": "Allow",
            "ExportXml": "Allow",
        }

        # 使用者：新增 dave（Explorer）
        page.click("nav.tabs >> text=使用者")
        page.click("text=新增使用者")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("使用者名稱").fill("dave")
        dialog.get_by_label("email").fill("dave@example.com")
        dialog.get_by_label("站台角色").select_option("Explorer")
        dialog.get_by_role("button", name="新增").click()
        expect(page.locator("tr[data-user='dave']")).to_contain_text("dave@example.com")
        # 不能改自己的站台角色
        expect(page.get_by_label("amy 的站台角色")).to_be_disabled()

        # 群組：新增 Marketing，加入 bob
        page.click("nav.tabs >> text=群組")
        page.click("text=新增群組")
        page.get_by_role("dialog").get_by_label("名稱").fill("Marketing")
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        row = page.locator("tr[data-group='Marketing']")
        expect(row).to_contain_text("0 位")
        row.get_by_role("button", name="成員").click()
        page.get_by_role("dialog").get_by_label("bob").check()
        page.get_by_role("dialog").get_by_role("button", name="儲存").click()
        expect(row).to_contain_text("1 位")

        # 修改紀錄
        page.click("nav.tabs >> text=修改紀錄")
        log = page.locator(".admin-table")
        expect(log).to_contain_text("建立 Project")
        expect(log).to_contain_text("新增使用者")
        expect(log).to_contain_text("修改群組")
        page.screenshot(path=str(tmp_path / "admin.png"))

        # 不是管理員：首頁沒有站台管理，網址直接打開也看不到
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option(".userbar select", "bob")
        page.wait_for_selector("text=Tableau 移轉檢視器")
        assert page.locator("text=站台管理").count() == 0
        page.goto(f"{url}/admin")
        page.wait_for_selector("text=站台管理只有站台管理員可以使用")
        browser.close()
