"""發布的端到端（D0）：桌面版的檢視器登入伺服器（在瀏覽器中核准）、開啟 .twbx、發布、再發布（覆蓋同一份）；
伺服器的檢視器看版本、比較、回復舊版。

伺服器與本機引擎各以 uvicorn 執行；「系統瀏覽器」是另一個瀏覽器環境，以 amy 登入伺服器後按「允許」。
需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import queue
import re
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.background import free_port
from tmig_service.credentials import MemoryCredentials
from tmig_service.desktop import create_desktop_app
from tmig_service.store import ReportStore

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "publish-viewer-token-0123456789"


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


class Running:
    def __init__(self, app: FastAPI) -> None:
        self.port = free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", proxy_headers=False
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("服務沒有在 30 秒內啟動")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def servers(tmp_path: Path) -> Iterator[tuple[str, str, queue.Queue[str]]]:
    fixtures = tmp_path / "server-fixtures"
    fixtures.mkdir()
    settings = AuthSettings(mode="dev", dev_users=["amy", "bob"], require_directory=False)
    server = Running(
        create_app(fixtures, DIST, auth=settings, store=ReportStore(tmp_path / "store"))
    )
    opened: queue.Queue[str] = queue.Queue()
    engine = Running(
        create_desktop_app(
            tmp_path / "workspace",
            TOKEN,
            DIST,
            username="amy",
            settings_dir=tmp_path / "settings",
            credentials=MemoryCredentials(),
            opener=opened.put,
        )
    )
    try:
        yield server.url, engine.url, opened
    finally:
        engine.stop()
        server.stop()


def test_publish_from_desktop_and_versions_on_server(
    servers: tuple[str, str, queue.Queue[str]], tmp_path: Path
) -> None:
    from playwright.sync_api import Dialog, expect, sync_playwright

    server, engine, opened = servers
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)

    def answer(dialog: Dialog) -> None:
        dialog.accept(str(source))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        desktop = browser.new_page(viewport={"width": 1400, "height": 1000})
        desktop.on("dialog", answer)
        desktop.goto(f"{engine}/?token={TOKEN}")
        expect(desktop.get_by_role("heading", name="報表桌面版")).to_be_visible()

        # 登入伺服器：開啟系統瀏覽器，在伺服器的核准頁按「允許」
        desktop.get_by_label("伺服器網址").fill(server)
        desktop.get_by_role("button", name="登入伺服器").click()
        authorize = opened.get(timeout=30)
        system = browser.new_context(viewport={"width": 1400, "height": 1000})
        assert system.request.post(f"{server}/auth/dev", data={"username": "amy"}).ok
        approval = system.new_page()
        approval.goto(authorize)
        expect(approval.get_by_role("heading")).to_have_text("允許報表桌面版登入？")
        approval.get_by_role("button", name="允許").click()
        approval.wait_for_url(f"{engine}/desktop-auth/callback*")
        expect(approval.get_by_role("heading")).to_have_text("已登入")
        expect(desktop.locator(".server-panel")).to_contain_text(f"已登入 {server}")

        # 開啟 .twbx 並發布
        desktop.get_by_role("button", name="開啟檔案…").click()
        desktop.wait_for_url(f"{engine}/w/demo/d/*")
        desktop.locator(".toolbar").get_by_role("button", name="發布…").click()
        dialog = desktop.get_by_role("dialog", name="發布到伺服器")
        dialog.get_by_label("報表名稱").fill("銷售示範")
        dialog.get_by_label("版本說明").fill("第一版")
        dialog.get_by_role("button", name="發布", exact=True).click()
        expect(dialog).to_contain_text("已發布「銷售示範」第 1 版", timeout=60_000)
        dialog.locator(".dialog-actions").get_by_role("button", name="關閉").click()

        # 再發布：預設覆蓋同一份
        desktop.locator(".toolbar").get_by_role("button", name="發布…").click()
        expect(dialog.get_by_role("radio", name=re.compile("覆蓋「銷售示範」"))).to_be_checked()
        dialog.get_by_label("版本說明").fill("第二版")
        dialog.get_by_role("button", name="發布", exact=True).click()
        expect(dialog).to_contain_text("第 2 版", timeout=60_000)
        dialog.locator(".dialog-actions").get_by_role("button", name="關閉").click()

        # 發布資料源：在資料源分頁發布，報表改用發布的資料源
        desktop.goto(f"{engine}/w/demo/ds/ds_orders")
        expect(desktop.locator(".ds-badge.published")).to_have_count(0)
        desktop.locator(".ds-header").get_by_role("button", name="發布到伺服器…").click()
        sources = desktop.get_by_role("dialog", name="發布資料源")
        expect(sources.get_by_label("資料源", exact=True)).to_have_value("ds_orders")
        expect(sources.get_by_label("報表改用發布的資料源")).to_be_checked()
        sources.get_by_label("資料源名稱").fill("訂單")
        sources.get_by_role("button", name="發布", exact=True).click()
        expect(sources).to_contain_text("已發布資料源「訂單」第 1 版", timeout=60_000)
        expect(sources).to_contain_text("報表已改用發布的資料源")
        sources.locator(".dialog-actions").get_by_role("button", name="關閉").click()
        expect(desktop.locator(".ds-badge.published")).to_be_visible()  # 畫面重新載入報表
        # 再發布：預設覆蓋同一個資料源
        desktop.locator(".toolbar").get_by_role("button", name="發布資料源…").click()
        expect(sources.get_by_role("radio", name=re.compile("覆蓋「訂單」"))).to_be_checked()
        sources.get_by_role("button", name="發布", exact=True).click()
        expect(sources).to_contain_text("已發布資料源「訂單」第 2 版", timeout=60_000)
        sources.locator(".dialog-actions").get_by_role("button", name="關閉").click()

        # 伺服器的檢視器：版本清單、比較、回復舊版
        viewer = system.new_page()
        viewer.goto(f"{server}/w/report")
        viewer.wait_for_selector(".dashboard [data-zone] svg, .dashboard [data-zone] table")
        viewer.get_by_role("button", name="版本（第 2 版）").click()
        versions = viewer.get_by_role("dialog")
        expect(versions).to_contain_text("第一版")
        expect(versions).to_contain_text("第二版")
        versions.get_by_role("button", name="比較", exact=True).click()
        expect(versions).to_contain_text("兩版的報表定義相同")
        versions.get_by_role("button", name="回復", exact=True).click()
        versions.get_by_role("button", name="確定回復第 1 版").click()
        expect(viewer.get_by_role("button", name="版本（第 3 版）")).to_be_visible()

        # 伺服器的資料源頁面：桌面版發布的資料源
        viewer.goto(f"{server}/datasources")
        expect(viewer.locator("body")).to_contain_text("訂單")

        # 伺服器的檢視器：桌面版的登入，可以撤銷
        viewer.goto(f"{server}/")
        viewer.get_by_role("button", name="桌面版的登入").click()
        tokens = viewer.get_by_role("dialog", name="桌面版的登入")
        expect(tokens.get_by_role("button", name="撤銷")).to_have_count(1)
        browser.close()
