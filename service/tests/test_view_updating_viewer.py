"""檢視更新中的標示的端到端：地圖「只保留」之後重新查詢、等更細的底圖與形狀下載才畫好，這段時間裡檢視標示
「更新中」、蓋住檢視不接受滑鼠操作（與 Tableau 處理互動時相同），畫好之後標示拿掉，框選照常。以 Chromium 的
網路延遲模擬（CDP 的 Network.emulateNetworkConditions，每個請求晚 1.5 秒）放慢查詢與下載。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "view-updating-e2e-token-0123456789"


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


def call(
    base: str, path: str, body: dict[str, Any] | None = None, method: str | None = None
) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp_path = tmp_path_factory.mktemp("view_updating")
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
    base = f"http://127.0.0.1:{port}"
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    call(base, "/api/desktop/open", {"path": str(source)})
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# 某一州的形狀（有資料的標記）的外框中心
STATE_CENTER = """(name) => {
  const paths = Array.from(document.querySelectorAll('.single-sheet svg .mark-shape.role-mark path'));
  const found = paths.find((p) => p.__data__ && p.__data__.datum && p.__data__.datum.__row >= 0
    && Object.values(p.__data__.datum).includes(name));
  if (!found) return null;
  const r = found.getBoundingClientRect();
  return [r.left + r.width / 2, r.top + r.height / 2];
}"""


def rectangle(page: Any, x0: float, y0: float, x1: float, y1: float) -> None:
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
    page.mouse.move(x1, y1, steps=4)
    page.mouse.up()


def test_map_shows_updating_until_redrawn(engine: str) -> None:
    """州的填色地圖（檢視器）：以矩形選取選德州、按「✓ 只保留」。網路每個請求晚 1.5 秒：重新查詢、下載底圖與形狀
    的這段時間裡，工作表標為更新中（data-updating）、檢視上有「更新中…」的標示蓋住地圖——這時在地圖上框選不會
    選到任何東西、也不釘住工具提示。新的地圖（只剩德州）畫好之後標示拿掉，框選整張地圖選到德州。"""
    from playwright.sync_api import sync_playwright

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Updating map"})["worksheet"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        # 連按兩下有地理角色的欄位建立地圖（州的形狀填色）
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        page.wait_for_selector('.editor[data-saving="false"]')
        page.locator('.data-pane li[data-field="f_state_province"]').dblclick()
        page.wait_for_function(
            "document.querySelectorAll('.editor .worksheet svg path').length > 40", timeout=30000
        )
        page.wait_for_selector('.editor[data-saving="false"]')

        page.goto(f"{engine}/w/demo/s/{created['id']}")
        page.wait_for_selector('.single-sheet .chart[data-zoomable="true"][data-drawn="true"]')
        worksheet = page.locator(".single-sheet .worksheet")
        assert worksheet.get_attribute("data-updating") == "false"
        tools = page.locator(".single-sheet .map-tools")
        tools.get_by_role("button", name="矩形選取").click()
        center = page.evaluate(STATE_CENTER, "Texas")
        assert center is not None
        rectangle(page, center[0] - 4, center[1] - 4, center[0] + 4, center[1] + 4)
        pinned = page.locator(".viz-tooltip.pinned")
        pinned.wait_for()
        assert re.search(r"1 個標記", pinned.inner_text()), pinned.inner_text()

        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 1500, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        started = time.monotonic()
        pinned.get_by_role("button", name="✓ 只保留").click()
        indicator = page.locator(".single-sheet .view-updating")
        indicator.wait_for(state="visible", timeout=5000)
        assert time.monotonic() - started < 3  # 查詢還沒回來就標示
        assert worksheet.get_attribute("data-updating") == "true"
        assert "更新中" in indicator.inner_text()

        # 更新中：框選落在標示上，不選取
        overlay = page.locator(".single-sheet .worksheet-body").bounding_box()
        assert overlay is not None
        rectangle(
            page,
            overlay["x"] + 10,
            overlay["y"] + 10,
            overlay["x"] + overlay["width"] - 10,
            overlay["y"] + overlay["height"] - 10,
        )
        page.wait_for_timeout(300)
        assert pinned.count() == 0
        assert indicator.is_visible()

        # 畫好之後：標示拿掉，只剩德州；框選整張地圖選到它
        indicator.wait_for(state="detached", timeout=60000)
        assert worksheet.get_attribute("data-updating") == "false"
        cdp.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        states = page.evaluate(
            """() => Array.from(document.querySelectorAll('.single-sheet svg .mark-shape.role-mark path'))
              .filter((p) => p.__data__ && p.__data__.datum && p.__data__.datum.__row >= 0)
              .map((p) => Object.values(p.__data__.datum).includes('Texas'))"""
        )
        assert states and all(states)
        box = page.locator(".single-sheet .select-overlay").bounding_box()
        assert box is not None
        rectangle(
            page,
            box["x"] + 5,
            box["y"] + 5,
            box["x"] + box["width"] - 5,
            box["y"] + box["height"] - 5,
        )
        pinned.wait_for()
        assert re.search(r"1 個標記", pinned.inner_text()), pinned.inner_text()
        assert errors == []
        browser.close()
