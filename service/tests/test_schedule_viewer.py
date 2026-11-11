"""排程管理與訂閱的端到端：管理員在站台管理中設定時區、新增排程與萃取更新、立即執行；
使用者在檢視器的工具列訂閱，在「我的訂閱」中修改與取消。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_schedule_admin import DIRECTORY, PERMISSIONS

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig.package import write_package
from tmig.schedule.runner import Scheduler
from tmig.verify.run import ensure_data, load_fixture_ir
from tmig_service.auth import AuthSettings
from tmig_service.background import running_server
from tmig_service.schedules import FailurePolicy, ScheduleStore, StoreJobs
from tmig_service.site import SiteStore
from tmig_service.store import ReportStore

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"


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


def test_schedules_and_subscriptions(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    work = tmp_path / "demo"
    work.mkdir()
    shutil.copy(DEMO, work / "demo.twbx")
    ir = load_fixture_ir(work)
    ensure_data(work, ir)
    package = tmp_path / "demo.tmigx"
    write_package(package, ir, work)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    directory = Directory.model_validate(
        {
            **DIRECTORY,
            "users": [
                {**u, "email": u.get("email") or f"{u['name']}@example.com"}
                for u in DIRECTORY["users"]
            ],
        }
    )
    settings = AuthSettings(
        mode="dev", directory=directory, permissions=Permissions.model_validate(PERMISSIONS)
    )
    store = ReportStore(tmp_path / "store")
    with running_server(fixtures, DIST, auth=settings, store=store) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option("select[aria-label='切換使用者']", "amy")
        published = page.request.post(
            f"{url}/api/reports/publish?name=Superstore&project=p-sales",
            data=package.read_bytes(),
            headers={"Content-Type": "application/vnd.tmig.tmigx"},
        )
        assert published.ok, published.text()
        report = published.json()["report"]["id"]

        # 排程：先設定時區，再新增每天 6 點的萃取更新排程與每週一的訂閱排程
        page.goto(f"{url}/admin/schedules")
        page.get_by_label("排程的時區").fill("Asia/Taipei")
        page.get_by_role("button", name="儲存").click()
        expect(page.locator(".success")).to_have_text("已設定時區")
        expect(page.locator(".scheduler-status")).to_contain_text("排程器沒有在執行")
        page.get_by_role("button", name="新增排程").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("名稱").fill("每天 6 點")
        dialog.get_by_label("開始時間").fill("06:00")
        dialog.get_by_role("button", name="儲存").click()
        expect(page.locator("tr[data-schedule]", has_text="每天 6 點")).to_contain_text(
            "每天 06:00"
        )
        page.get_by_role("button", name="新增排程").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("名稱").fill("每週一早上")
        dialog.get_by_label("類型").select_option("Subscription")
        dialog.get_by_label("頻率").select_option("weekly")
        dialog.get_by_label("開始時間").fill("08:30")
        dialog.get_by_role("button", name="儲存").click()
        expect(page.locator("tr[data-schedule]", has_text="每週一早上")).to_contain_text(
            "每週一 08:30"
        )

        # 工作：新增萃取更新、立即執行（排入佇列）
        page.click("nav.tabs >> text=工作")
        page.get_by_role("button", name="新增萃取更新").click()
        page.get_by_role("dialog").get_by_role("button", name="新增").click()
        row = page.locator("tr[data-task]")
        expect(row).to_contain_text("Superstore／ds_orders")
        row.get_by_role("button", name="立即執行").click()
        expect(page.locator(".success")).to_contain_text("已排入佇列")
        expect(page.locator(".scheduler-status")).to_contain_text("1 個工作排隊中")

        # 排程器（報表庫模式）執行佇列中的工作：結果出現在工作與執行紀錄中
        root = tmp_path / "store"
        schedules = ScheduleStore(root)
        scheduler = Scheduler(
            [],
            "UTC",
            {"extract": lambda job: "已更新 ds_orders 9994 列"},
            schedules.state_dir,
            source=StoreJobs(schedules, ReportStore(root), SiteStore(root), fixtures),
            on_result=FailurePolicy(schedules, SiteStore(root)),
        )
        scheduler.beat()
        assert [r.status for r in scheduler.tick()] == ["ok"]
        page.reload()
        expect(page.locator("tr[data-task]")).to_contain_text("成功")
        expect(page.locator(".scheduler-status")).to_contain_text("排程器執行中")
        page.locator("tr[data-task]").get_by_role("button", name="紀錄").click()
        expect(page.locator(".admin-table")).to_contain_text("已更新 ds_orders 9994 列")
        expect(page.locator(".admin-table")).to_contain_text("立即執行（amy）")

        # bob 在檢視器的工具列訂閱 dashboard
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option(".userbar select", "bob")
        page.goto(f"{url}/w/{report}")
        page.wait_for_selector(".dashboard")
        page.get_by_role("button", name="訂閱").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_label("排程")).to_contain_text("每週一早上")
        dialog.get_by_label("主旨").fill("每週銷售")
        dialog.get_by_label("格式").select_option("both")
        dialog.get_by_label("紙張方向").select_option("Landscape")
        dialog.get_by_role("button", name="訂閱").click()
        expect(page.locator(".toolbar .success")).to_have_text("已訂閱")
        sub = schedules.data().subscriptions[0]
        assert (sub.user, sub.view, sub.attach_pdf, sub.page_orientation) == (
            "bob",
            "銷售總覽",
            True,
            "Landscape",
        )

        # 我的訂閱：改主旨、暫停、取消訂閱
        page.goto(f"{url}/")
        page.click("text=我的訂閱（1）")
        row = page.locator("tr[data-subscription]")
        expect(row).to_contain_text("每週銷售")
        expect(row).to_contain_text("圖片與 PDF")
        row.get_by_role("button", name="修改").click()
        page.get_by_role("dialog").get_by_label("主旨").fill("週報")
        page.get_by_role("dialog").get_by_role("button", name="儲存").click()
        expect(row).to_contain_text("週報")
        row.get_by_role("button", name="暫停").click()
        # 「暫停」也是按鈕的文字：等到按鈕變成「繼續」（修改完成、清單重新讀取）才檢查
        expect(row.get_by_role("button", name="繼續")).to_be_visible()
        assert schedules.data().subscriptions[0].suspended is True
        row.get_by_role("button", name="取消訂閱").click()
        page.get_by_role("dialog").get_by_role("button", name="取消訂閱").click()
        expect(page.locator("text=還沒有訂閱")).to_be_visible()
        assert schedules.data().subscriptions == []
        browser.close()
