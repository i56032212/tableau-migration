"""資料警示的端到端：在檢視器的工具列建立警示，在「我的訂閱」與站台管理的「工作」中看到與管理。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_alerts import BOARD, DEMO, DIRECTORY, PERMISSIONS

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig_service.alerts import AlertStore
from tmig_service.auth import AuthSettings
from tmig_service.background import running_server
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


def test_create_and_manage_alert(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    target = tmp_path / "fixtures" / "superstore_demo"
    target.mkdir(parents=True)
    for name in ("golden.ir.json", "cases.yaml"):
        shutil.copy(DEMO / name, target / name)
    shutil.copytree(DEMO / "data", target / "data")
    settings = AuthSettings(
        mode="dev",
        directory=Directory.model_validate(DIRECTORY),
        permissions=Permissions.model_validate(PERMISSIONS),
    )
    store = ReportStore(tmp_path / "store")
    with (
        running_server(tmp_path / "fixtures", DIST, auth=settings, store=store) as url,
        sync_playwright() as p,
    ):
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option("select[aria-label='切換使用者']", "carol")
        page.goto(f"{url}/w/superstore_demo/d/{BOARD}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.get_by_role("button", name="警示", exact=True).click()
        dialog = page.get_by_role("dialog", name="建立資料警示")
        dialog.get_by_label("工作表").select_option(label="Sales by Region")
        expect(dialog.get_by_label("量值")).to_contain_text("Sales")
        expect(dialog.locator(".hint").first).to_contain_text("目前的範圍")
        dialog.get_by_label("條件").select_option("above_or_equal")
        dialog.get_by_label("門檻").fill("250000")
        dialog.get_by_label("寄送頻率").select_option("daily")
        dialog.get_by_role("button", name="建立").click()
        expect(page.locator(".toolbar .success")).to_contain_text(
            "已建立警示：Sales 高於或等於 250,000"
        )
        (alert,) = AlertStore(tmp_path / "store").data().alerts
        assert (alert.worksheet, alert.measure, alert.frequency, alert.recipients) == (
            "ws_sales_by_region",
            "sum(f_sales)",
            "daily",
            ["carol"],
        )

        # 我的訂閱：資料警示的清單，可以暫停
        page.goto(f"{url}/subscriptions")
        row = page.locator(f"tr[data-alert='{alert.id}']")
        expect(row).to_contain_text("Sales 高於或等於 250,000")
        expect(row).to_contain_text("最多每天一次")
        row.get_by_role("button", name="暫停").click()
        expect(row).to_contain_text("暫停")
        alert_after = AlertStore(tmp_path / "store").get(alert.id)
        assert alert_after is not None and alert_after.suspended is True

        # 站台管理員在「工作」中看到全部的警示
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option(".userbar select", "amy")
        page.goto(f"{url}/admin/tasks")
        admin_row = page.locator(f"tr[data-alert='{alert.id}']")
        expect(admin_row).to_contain_text("carol")
        admin_row.get_by_role("button", name="繼續").click()
        expect(admin_row).to_contain_text("正常")
        browser.close()
