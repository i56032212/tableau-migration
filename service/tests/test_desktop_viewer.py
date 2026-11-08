"""桌面版的檢視器端到端（D0）：帶權杖開啟、開啟 .twbx、存成報表套件、外殼選單的入口。

沒有桌面版外殼（Tauri）時，選檔對話框改成輸入路徑（window.prompt），這裡以 Playwright 回答。
需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import shutil
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from tmig_service.background import free_port
from tmig_service.desktop import create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "viewer-test-token-0123456789"


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
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_open_view_and_save(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import Dialog, expect, sync_playwright

    folder = tmp_path / "報表"
    folder.mkdir()
    source = folder / "demo.twbx"
    shutil.copy(DEMO, source)
    target = folder / "demo.tmigx"
    answers: list[str] = []
    asked: list[str] = []

    def answer(dialog: Dialog) -> None:
        asked.append(dialog.default_value)
        dialog.accept(answers.pop(0))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.on("dialog", answer)
        # 沒有權杖：拒絕
        assert page.goto(f"{engine}/").status == 401  # type: ignore[union-attr]
        page.goto(f"{engine}/?token={TOKEN}")
        assert page.url == f"{engine}/"
        expect(page.get_by_role("heading", name="報表桌面版")).to_be_visible()
        expect(page.locator(".userbar")).to_contain_text("amy")

        answers.append(str(source))
        page.get_by_role("button", name="開啟檔案…").click()
        page.wait_for_url(f"{engine}/w/demo/d/*")
        page.wait_for_selector(".dashboard [data-zone] svg, .dashboard [data-zone] table")

        # 還沒存過：先問位置（預設與原始檔同一個資料夾）
        answers.append(str(target))
        page.locator(".toolbar").get_by_role("button", name="儲存", exact=True).click()
        expect(page.locator(".toast")).to_contain_text("已儲存")
        assert asked[-1] == str(folder / "demo.tmigx")
        with zipfile.ZipFile(target) as archive:
            assert "data/ds_orders.duckdb" in archive.namelist()
        first = target.stat().st_mtime_ns

        # 存過之後「儲存」直接覆蓋，不再詢問
        time.sleep(0.05)
        page.locator(".toast").first.evaluate("element => element.remove()")
        prompts = len(asked)
        page.locator(".toolbar").get_by_role("button", name="儲存", exact=True).click()
        expect(page.locator(".toast")).to_contain_text("已儲存")
        assert len(asked) == prompts
        assert target.stat().st_mtime_ns >= first

        # 外殼的選單呼叫的入口
        assert page.evaluate("typeof window.tmigDesktop.saveAs") == "function"
        page.evaluate("window.tmigDesktop.close()")
        page.wait_for_url(f"{engine}/")
        expect(page.get_by_text("還沒有開啟的報表")).to_be_visible()

        # 開啟存好的報表套件，再以工具列的「關閉」關掉
        answers.append(str(target))
        page.get_by_role("button", name="開啟檔案…").click()
        page.wait_for_url(f"{engine}/w/demo/d/*")
        page.wait_for_selector(".dashboard [data-zone] svg, .dashboard [data-zone] table")
        page.locator(".toolbar").get_by_role("button", name="關閉", exact=True).click()
        page.wait_for_url(f"{engine}/")
        expect(page.get_by_text("還沒有開啟的報表")).to_be_visible()
        browser.close()
