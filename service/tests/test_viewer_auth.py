"""檢視器的身分驗證（P2）端到端：開發模式先選使用者，切換使用者後圖表依列層級權限改變。

需要先建置檢視器（cd web && pnpm install && pnpm build）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import json
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
def usage(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("usage")


@pytest.fixture(scope="module")
def server(fixtures: Path, usage: Path) -> Any:
    settings = AuthSettings(mode="dev", directory=DIRECTORY)
    with running_server(fixtures, DIST, auth=settings, usage_dir=usage) as url:
        yield url


def test_dev_mode_user_switching(server: str, fixtures: Path, usage: Path) -> None:
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
        # 使用紀錄：每次開啟 dashboard 記一次，使用者為當時的登入者
        visits = [
            json.loads(line)
            for path in usage.glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert [(v["user"], v["view"]) for v in visits] == [
            ("alice", board.name),
            ("carol", board.name),
            ("dave", board.name),
        ]

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


def test_proxy_mode_explains_an_account_that_is_not_a_site_user(fixtures: Path) -> None:
    """反向代理（SSO 閘道）模式：閘道已經驗證過身分、但帳號不在使用者目錄中時，畫面說明這個帳號沒有使用權限
    並寫出帳號（稽核 A3：原本寫「沒有登入者：請經由公司的登入入口開啟這個網站」，讓人以為沒登入）；
    真的沒有帶帳號（沒有經過登入入口）時才是沒有登入者。"""
    import ipaddress

    from playwright.sync_api import expect, sync_playwright

    from tmig_service.auth import ProxySettings

    settings = AuthSettings(
        mode="proxy",
        directory=DIRECTORY,
        proxy=ProxySettings(
            user_header="X-Forwarded-User",
            trusted_proxies=[ipaddress.ip_network("127.0.0.1/32")],
        ),
    )
    board = load_ir(fixtures / "secure" / "golden.ir.json").dashboards[0]
    with running_server(fixtures, DIST, auth=settings) as url, sync_playwright() as p:
        browser = p.chromium.launch()

        def open_as(user: str | None) -> Any:
            headers = {"X-Forwarded-User": user} if user else {}
            context = browser.new_context(
                viewport={"width": 1400, "height": 1000}, extra_http_headers=headers
            )
            page = context.new_page()
            page.goto(f"{url}/w/secure/d/{board.id}")
            return page

        # 不在目錄中的帳號：說明帳號沒有權限
        page = open_as("zed")
        message = page.locator("main.error")
        expect(message).to_contain_text("這個帳號沒有使用這個網站的權限")
        expect(message).to_contain_text("帳號 zed 不在使用者目錄中")
        expect(message).to_contain_text("請向站台管理員申請加入")
        expect(message).not_to_contain_text("沒有登入者")
        assert page.locator(".worksheet").count() == 0
        # 沒有帶帳號：沒有登入者
        page = open_as(None)
        expect(page.locator("main.error")).to_have_text(
            "沒有登入者：請經由公司的登入入口開啟這個網站"
        )
        # 目錄中的帳號：照常開啟，右上角是他的名稱
        page = open_as("alice")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        expect(page.locator(".userbar .user")).to_have_text("Alice Wang")
        browser.close()
