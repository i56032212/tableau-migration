"""桌面版的檢視器端到端（D0）：帶權杖開啟、開啟 .twbx、存成報表套件、外殼選單的入口。

沒有桌面版外殼（Tauri）時，選檔對話框改成輸入路徑（window.prompt），這裡以 Playwright 回答。
需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from editor_ui import current_tab, menubar, sheet_tab, tab_menu, tool

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


SHELL_CONFIRM = """
window.__confirms = [];
window.__answer = false;
// 與桌面版外殼相同：Tauri 的對話框外掛把 window.confirm 換成非同步的（回傳 Promise，不是布林值）
window.confirm = async (message) => {
  window.__confirms.push(String(message));
  await new Promise((resolve) => setTimeout(resolve, 30));
  if (window.__answer === 'blocked') throw new Error('dialog.message not allowed by ACL');
  return window.__answer;
};
"""


def test_confirmations_wait_for_the_shell_dialog(engine: str, tmp_path: Path) -> None:
    """桌面版外殼裡的確認（2026-11-21 在真正的視窗中發現：刪除工作表、關閉還沒儲存的報表都沒有問就做了）。
    外殼的 window.confirm 是非同步的，回傳 Promise——Promise 是真值，`if (!window.confirm(…))` 等於沒有問。
    這裡把 window.confirm 換成與外殼相同的非同步版本：回答取消（或對話框出不來）時不刪除、不關閉，
    回答確定才做。"""
    from playwright.sync_api import expect, sync_playwright

    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)

    def sheets(page: Any) -> list[str]:
        ir = page.evaluate("async () => (await (await fetch('/api/workbooks/demo/ir')).json())")
        return [w["name"] for w in ir["worksheets"]]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(SHELL_CONFIRM)
        page.goto(f"{engine}/?token={TOKEN}")
        opened = page.evaluate(
            """async (path) => (await fetch('/api/desktop/open', {method: 'POST',
              headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path})})).status""",
            str(source),
        )
        assert opened == 200
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        page.wait_for_selector('.editor[data-saving="false"]')
        # 新增一張工作表來刪（dashboard 用到的工作表不能刪）
        tool(page, "新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector('.editor[data-saving="false"]')
        expect(current_tab(page)).to_have_text("工作表 1")
        before = sheets(page)
        assert "工作表 1" in before

        # 對話框出不來（外殼沒有允許）：當作取消，不刪除
        page.evaluate("window.__answer = 'blocked'")
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_function("window.__confirms.length === 1")
        page.wait_for_timeout(500)
        assert sheets(page) == before
        expect(sheet_tab(page, "工作表 1")).to_be_visible()

        # 回答取消：不刪除
        page.evaluate("window.__answer = false")
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_function("window.__confirms.length === 2")
        page.wait_for_timeout(500)
        assert sheets(page) == before
        assert "刪除之後不能復原" in page.evaluate("window.__confirms[1]")

        # 回答確定：刪除，留在編輯器（開啟左邊的分頁）
        page.evaluate("window.__answer = true")
        tab_menu(page, current_tab(page), "刪除")
        page.wait_for_url(f"{engine}/w/demo/edit/ws_sub_category_profit")
        page.wait_for_selector('.editor[data-saving="false"]')
        assert sheets(page) == [name for name in before if name != "工作表 1"]

        # 報表改過、還沒儲存：關閉前先問；取消時不關閉
        page.evaluate("window.__answer = false")
        asked = page.evaluate("window.__confirms.length")
        menubar(page, "檔案", "關閉")
        page.wait_for_function(f"window.__confirms.length === {asked + 1}")
        page.wait_for_timeout(500)
        assert "尚未儲存的變更" in page.evaluate("window.__confirms.at(-1)")
        assert page.url == f"{engine}/w/demo/edit/ws_sub_category_profit"
        assert (
            page.evaluate(
                "async () => (await (await fetch('/api/desktop/documents')).json()).length"
            )
            == 1
        )
        # 確定：關閉，回到報表清單
        page.evaluate("window.__answer = true")
        menubar(page, "檔案", "關閉")
        page.wait_for_url(f"{engine}/")
        expect(page.get_by_text("還沒有開啟的報表")).to_be_visible()
        assert errors == []
        browser.close()


def test_no_synchronous_confirm_in_the_web_app() -> None:
    """前端不直接呼叫 window.confirm：桌面版外殼中它是非同步的，同步的寫法等於沒有問（見 web/src/confirm.ts）。"""
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{number}"
        for path in sorted((ROOT / "web" / "src").rglob("*.ts*"))
        if path.name != "confirm.ts"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"(?<![\w.])(?:window\.)?confirm\(", line)
        and not line.lstrip().startswith(("//", "*", "/*"))
    ]
    assert offenders == [], f"改用 confirmBox（web/src/confirm.ts）：{offenders}"
