"""檢視者的工具（端到端）：搜尋、復原／重做／還原、自訂檢視（預設檢視）、下載交叉資料表與資料、留言、
我的最愛與最近檢視。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook
from test_viewer_tools import BOARD, DEMO, DIRECTORY, PERMISSIONS

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
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


def test_viewer_tools(tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    fixtures = tmp_path / "fixtures"
    target = fixtures / "superstore_demo"
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
        running_server(
            fixtures, DIST, auth=settings, usage_dir=tmp_path / "usage", store=store
        ) as url,
        sync_playwright() as p,
    ):
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000}, accept_downloads=True
        )
        page = context.new_page()
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option("select[aria-label='切換使用者']", "carol")

        # 搜尋：只列出名稱符合的 view
        page.get_by_label("搜尋報表").fill("monthly")
        expect(page.locator(".workbooks li .wb-links a")).to_have_text(["Monthly Profit"])
        page.get_by_label("搜尋報表").fill("沒有這個")
        expect(page.locator("text=找不到「沒有這個」")).to_be_visible()
        page.get_by_label("搜尋報表").fill("")

        page.goto(f"{url}/w/superstore_demo/d/{BOARD}")
        loaded = "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        page.wait_for_function(loaded)
        category = page.locator("fieldset.control", has_text="Category")
        before = category.locator("button.dropdown").text_content() or ""
        undo = page.get_by_role("button", name="復原")
        redo = page.get_by_role("button", name="重做")
        expect(undo).to_be_disabled()

        # 改篩選 → 復原 → 重做 → 還原
        category.locator("button.dropdown").click()
        category.get_by_label("Office Supplies").click()
        page.keyboard.press("Escape")
        expect(category.locator("button.dropdown")).not_to_have_text(before)
        changed = category.locator("button.dropdown").text_content() or ""
        expect(undo).to_be_enabled()
        undo.click()
        expect(category.locator("button.dropdown")).to_have_text(before)
        redo.click()
        expect(category.locator("button.dropdown")).to_have_text(changed)
        page.get_by_role("button", name="還原").click()
        expect(category.locator("button.dropdown")).to_have_text(before)
        redo_after_revert = redo.is_enabled()
        assert redo_after_revert is False
        page.get_by_role("button", name="重新整理").click()
        page.wait_for_function(loaded)

        # 自訂檢視：存成預設檢視，重新開啟時套用
        category.locator("button.dropdown").click()
        category.get_by_label("Office Supplies").click()
        page.keyboard.press("Escape")
        page.get_by_role("button", name="自訂檢視").click()
        page.get_by_role("button", name="儲存目前的檢視…").click()
        page.get_by_label("自訂檢視的名稱").fill("含辦公用品")
        page.get_by_label("設為我的預設檢視").check()
        page.get_by_label("公開（其他看得到這份報表的人也看得到）").check()
        page.get_by_role("dialog", name="儲存自訂檢視").get_by_role("button", name="儲存").click()
        expect(page.locator("tr[data-custom-view]")).to_contain_text("含辦公用品")
        page.keyboard.press("Escape")
        page.reload()
        page.wait_for_function(loaded)
        expect(page.locator(".custom-view-name")).to_have_text("自訂檢視：含辦公用品")
        expect(category.locator("button.dropdown")).to_have_text(changed)

        # 下載交叉資料表（Excel）
        page.select_option("select[aria-label='匯出']", "crosstab")
        dialog = page.get_by_role("dialog", name="下載交叉資料表")
        dialog.get_by_label("工作表").select_option(label="Sales by Region")
        with page.expect_download() as download:
            dialog.get_by_role("button", name="下載").click()
        saved = tmp_path / "crosstab.xlsx"
        download.value.save_as(saved)
        assert download.value.suggested_filename == "Sales by Region.xlsx"
        sheet = load_workbook(saved).active
        assert sheet is not None
        rows = list(sheet.iter_rows(values_only=True))
        assert rows[0][0] == "Region" and len(rows) > 1
        expect(page.locator(".toolbar .success")).to_contain_text("已下載")

        # 檢視資料：摘要與完整資料
        page.select_option("select[aria-label='匯出']", "data")
        dialog = page.get_by_role("dialog", name="檢視資料")
        expect(dialog.locator(".data-preview tbody tr").first).to_be_visible()
        dialog.get_by_role("button", name="完整資料").click()
        expect(dialog.locator(".hint")).to_contain_text("只顯示前 1000 列")
        with page.expect_download() as download:
            dialog.get_by_role("button", name="下載 CSV").click()
        assert download.value.suggested_filename.endswith(".csv")
        page.keyboard.press("Escape")

        # 留言與我的最愛
        page.get_by_role("button", name="留言").click()
        page.get_by_label("新增留言").fill("Office Supplies 的利潤偏低")
        page.get_by_role("button", name="送出").click()
        expect(page.locator(".comments li")).to_contain_text("Office Supplies 的利潤偏低")
        page.get_by_role("dialog", name="留言").get_by_role("button", name="關閉").last.click()
        expect(page.get_by_role("button", name="留言（1）")).to_be_visible()
        page.get_by_role("button", name="☆ 我的最愛").click()
        expect(page.get_by_role("button", name="★ 我的最愛")).to_be_visible()

        page.goto(f"{url}/")
        favorites = page.get_by_role("region", name="我的最愛")
        expect(favorites).to_contain_text("superstore_demo／銷售總覽")
        expect(page.get_by_role("region", name="最近檢視")).to_contain_text(
            "superstore_demo／銷售總覽"
        )
        favorites.get_by_role("link").click()
        page.wait_for_function(loaded)

        # bob 沒有下載資料的權限：匯出中沒有資料與交叉資料表；看得到公開的自訂檢視與留言，但不能留言
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option(".userbar select", "bob")
        page.goto(f"{url}/w/superstore_demo/d/{BOARD}")
        page.wait_for_function(loaded)
        options = page.locator("select[aria-label='匯出'] option").all_inner_texts()
        assert "交叉資料表…" not in options and "資料…" not in options and "圖片（PNG）" in options
        page.get_by_role("button", name="自訂檢視（1）").click()
        expect(page.locator("tr[data-custom-view]")).to_contain_text("carol")
        page.keyboard.press("Escape")
        page.get_by_role("button", name="留言（1）").click()
        expect(page.get_by_label("新增留言")).to_have_count(0)
        browser.close()


def test_published_name_local_time_and_saved_view_name(tmp_path: Path) -> None:
    """發布的名稱出現在首頁、搜尋與檢視器的標題列（稽核 A11）；留言的時間以檢視者電腦的時區顯示
    （C19：原本是不標示的 UTC）；剛儲存的自訂檢視的名稱立刻出現在工具列（C14：原本要重新整理）。"""
    import datetime as dt
    from zoneinfo import ZoneInfo

    from playwright.sync_api import expect, sync_playwright

    from tmig.package import write_package
    from tmig.verify.run import ensure_data, load_fixture_ir

    fixtures = tmp_path / "fixtures"
    target = fixtures / "superstore_demo"
    target.mkdir(parents=True)
    for name in ("golden.ir.json", "cases.yaml"):
        shutil.copy(DEMO / name, target / name)
    shutil.copytree(DEMO / "data", target / "data")
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", work / "demo.twbx")
    packed = load_fixture_ir(work)
    ensure_data(work, packed)
    package = tmp_path / "demo.tmigx"
    write_package(package, packed, work)
    settings = AuthSettings(
        mode="dev",
        directory=Directory.model_validate(DIRECTORY),
        permissions=Permissions.model_validate(PERMISSIONS),
    )
    store = ReportStore(tmp_path / "store")
    loaded = "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
    with (
        running_server(
            fixtures, DIST, auth=settings, usage_dir=tmp_path / "usage", store=store
        ) as url,
        sync_playwright() as p,
    ):
        browser = p.chromium.launch()
        # 檢視者的電腦在台北（UTC+8）：伺服器記的是 UTC
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000}, timezone_id="Asia/Taipei", locale="zh-TW"
        )
        page = context.new_page()
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option("select[aria-label='切換使用者']", "amy")

        # 同一個檔案發布成兩份不同名稱的報表
        ids: dict[str, str] = {}
        for name in ("季度銷售", "華北週報"):
            published = page.request.post(
                f"{url}/api/reports/publish",
                params={"name": name, "project": "default"},
                data=package.read_bytes(),
                headers={"Content-Type": "application/vnd.tmig.tmigx"},
            )
            assert published.ok, published.text()
            ids[name] = published.json()["report"]["id"]
        page.goto(f"{url}/")
        names = page.locator(".workbooks .wb-name")
        expect(names).to_have_text(["季度銷售", "華北週報", "superstore_demo"])
        page.get_by_label("搜尋報表").fill("華北")
        expect(names).to_have_text(["華北週報"])
        page.get_by_label("搜尋報表").fill("")
        page.goto(f"{url}/w/{ids['季度銷售']}")
        page.wait_for_function(loaded)
        expect(page.locator(".toolbar .wb")).to_have_text("季度銷售")
        # 我的最愛與最近檢視也是發布的名稱
        page.get_by_role("button", name="☆ 我的最愛").click()
        expect(page.get_by_role("button", name="★ 我的最愛")).to_be_visible()
        page.goto(f"{url}/")
        expect(page.get_by_role("region", name="我的最愛")).to_contain_text("季度銷售／銷售總覽")
        expect(page.get_by_role("region", name="最近檢視")).to_contain_text("季度銷售／銷售總覽")

        # carol（可以留言、可以存自訂檢視）
        with page.expect_navigation():
            page.select_option(".userbar select", "carol")
        page.goto(f"{url}/w/superstore_demo/d/{BOARD}")
        page.wait_for_function(loaded)
        page.get_by_role("button", name="留言").click()
        page.get_by_label("新增留言").fill("時間是當地時間")
        page.get_by_role("button", name="送出").click()
        stamp = page.locator(".comments li time")
        expect(stamp).to_have_count(1)
        comments = page.request.get(
            f"{url}/api/workbooks/superstore_demo/views/{BOARD}/comments"
        ).json()["comments"]
        created = dt.datetime.fromisoformat(comments[0]["created_at"])
        assert created.utcoffset() == dt.timedelta(0)  # 伺服器記的是 UTC
        local = created.astimezone(ZoneInfo("Asia/Taipei"))
        expected = f"{local.year}/{local.month}/{local.day} {local:%H:%M:%S}"
        expect(stamp).to_have_text(expected)
        assert stamp.get_attribute("datetime") == comments[0]["created_at"]
        assert f"{created:%H:%M}" not in (stamp.text_content() or "")  # 不是 UTC 的時刻
        page.get_by_role("dialog", name="留言").get_by_role("button", name="關閉").last.click()

        # 存成自訂檢視：工具列立刻顯示名稱
        expect(page.locator(".custom-view-name")).to_have_count(0)
        page.get_by_role("button", name="自訂檢視").click()
        page.get_by_role("button", name="儲存目前的檢視…").click()
        page.get_by_label("自訂檢視的名稱").fill("我的檢視")
        page.get_by_role("dialog", name="儲存自訂檢視").get_by_role("button", name="儲存").click()
        expect(page.locator("tr[data-custom-view]")).to_contain_text("我的檢視")
        expect(page.locator(".custom-view-name")).to_have_text("自訂檢視：我的檢視")
        browser.close()
