"""檢視器的身分驗證（P2）端到端：開發模式先選使用者，切換使用者後圖表依列層級權限改變。

需要先建置檢視器（cd web && pnpm install && pnpm build）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import DIRECTORY

from tmig.ir import load_ir
from tmig_service.auth import AuthSettings
from tmig_service.background import running_server

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
SHEET = "ws_sales_by_region"


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


@pytest.fixture(scope="module")
def server(fixtures: Path) -> Any:
    with running_server(fixtures, DIST, auth=AuthSettings(mode="dev", directory=DIRECTORY)) as url:
        yield url


def test_dev_mode_user_switching(server: str, fixtures: Path) -> None:
    from playwright.sync_api import sync_playwright

    board = load_ir(fixtures / "secure" / "golden.ir.json").dashboards[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        regions: dict[str, set[str]] = {}

        def on_response(response: Any) -> None:
            if response.url.endswith(f"/worksheets/{SHEET}/render") and response.ok:
                regions["last"] = {row[0] for row in response.json()["table"]["rows"]}

        page.on("response", on_response)
        page.goto(f"{server}/w/secure/d/{board.id}")
        # 還沒選使用者：先顯示開發模式的使用者清單，不查詢
        page.wait_for_selector("text=開發模式")
        assert "last" not in regions

        def choose(user: str) -> set[str]:
            regions.clear()
            with page.expect_navigation():
                page.select_option("select[aria-label='切換使用者']", user)
            page.wait_for_function(
                "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
            )
            page.wait_for_timeout(300)
            return regions["last"]

        assert choose("alice") == {"East"}
        assert page.locator(".userbar select").input_value() == "alice"
        assert choose("carol") == {"Central", "South"}
        assert choose("dave") == {"Central", "East", "West"}  # 前 3 名

        # 登出後回到選使用者的畫面
        with page.expect_navigation():
            page.click(".userbar button")
        page.wait_for_selector("text=開發模式")
        browser.close()


def test_viewer_with_permissions(fixtures: Path) -> None:
    """沒有下載權限時報表定義不含公式與連線，檢視器照常顯示；沒有權限的人看不到報表。"""
    from playwright.sync_api import sync_playwright

    from tmig.access.directory import Directory, DirectoryUser
    from tmig.access.permissions import Permissions

    directory = Directory(
        users=[
            DirectoryUser(name="alice", groups=["All Users"], site_role="Viewer"),
            DirectoryUser(name="carol", groups=["All Users"], site_role="Viewer"),
        ]
    )
    granted = Permissions.model_validate(
        {
            "workbooks": [
                {
                    "id": "w1",
                    "name": "superstore_demo",
                    "rules": [
                        {
                            "grantee": {"kind": "user", "name": "alice"},
                            "capabilities": {"Read": "Allow"},
                        }
                    ],
                }
            ]
        }
    )
    settings = AuthSettings(mode="dev", directory=directory, permissions=granted)
    board = load_ir(fixtures / "secure" / "golden.ir.json").dashboards[0]
    with running_server(fixtures, DIST, auth=settings) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{url}/")
        with page.expect_navigation():
            page.select_option("select[aria-label='切換使用者']", "alice")
        page.wait_for_selector(".workbooks li")
        page.goto(f"{url}/w/secure/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        assert page.locator(".placeholder.error").count() == 0
        with page.expect_navigation():
            page.select_option(".userbar select", "carol")
        page.goto(f"{url}/")
        page.wait_for_selector("text=沒有可以檢視的報表")
        page.goto(f"{url}/w/secure/d/{board.id}")
        page.wait_for_selector("text=載入失敗")
        browser.close()
