"""檢視「更新中」的停止的端到端（Tableau：處理互動太久時出現可以停止的對話框；停止之後檢視暫停，像暫停自動更新
一樣，可以復原、繼續更新，或不更新繼續操作）。以 Chromium 的網路延遲模擬（CDP 的 Network.emulateNetworkConditions，
每個請求晚 2.5 秒）放慢查詢：按「停止」之後查詢的請求中止、送出停止伺服器上查詢的請求（同一個查詢代號），檢視
停在之前的結果、標示「已停止更新」；「繼續更新」重新查詢；復原回到畫面上的狀態時不查詢、恢復更新。dashboard 上
停止的是整個互動：所有工作表一起暫停、一起繼續更新。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import shutil
import sys
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from editor_ui import tool

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "view-stop-e2e-token-0123456789"
LATENCY = 2500


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


@pytest.fixture()
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
    base = f"http://127.0.0.1:{port}"
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    call(base, "/api/desktop/open", {"path": str(source)})
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Requests:
    """頁面送出的 render 與停止的請求、中止的請求。"""

    def __init__(self) -> None:
        self.renders: list[tuple[str, str]] = []  # (工作表, 查詢代號)
        self.stops: list[str] = []
        self.aborted: list[str] = []  # 中止的 render 的查詢代號

    def on_request(self, request: Any) -> None:
        url = request.url
        if "/render" in url and "/worksheets/" in url:
            sheet = url.split("/worksheets/")[1].split("/")[0]
            self.renders.append((sheet, request.headers.get("x-tmig-query", "")))
        elif "/api/queries/" in url and url.endswith("/cancel"):
            self.stops.append(url.split("/api/queries/")[1].split("/")[0])

    def on_failed(self, request: Any) -> None:
        if "/render" in request.url and "ERR_ABORTED" in (request.failure or ""):
            self.aborted.append(request.headers.get("x-tmig-query", ""))

    def watch(self, page: Any) -> None:
        page.on("request", self.on_request)
        page.on("requestfailed", self.on_failed)


def latency(cdp: Any, ms: int) -> None:
    cdp.send(
        "Network.emulateNetworkConditions",
        {"offline": False, "latency": ms, "downloadThroughput": -1, "uploadThroughput": -1},
    )


# 長條（有資料列的 rect 標記）的 Sub-Category 名稱，由上而下
BARS = """() => {
  const out = [];
  document.querySelectorAll('.single-sheet .chart > svg g.mark-rect.role-mark > path, .single-sheet .chart > .frozen-host > svg g.mark-rect.role-mark > path').forEach((p) => {
    const d = p.__data__ && p.__data__.datum;
    if (!d || typeof d.__row !== 'number') return;
    const name = Object.entries(d).find(([k]) => k.endsWith('_label') && typeof d[k] === 'string' && /^[A-Z][a-z]/.test(d[k]));
    if (name) out.push([p.getBoundingClientRect().top, name[1]]);
  });
  return out.sort((a, b) => a[0] - b[0]).map((x) => x[1]);
}"""


def exclude_top_bar(page: Any) -> str:
    """點最上面的長條、按「✕ 排除」；回傳它的名稱。"""
    top = page.evaluate(BARS)[0]
    box = page.evaluate(
        """(name) => {
          const p = [...document.querySelectorAll('.single-sheet .chart svg g.mark-rect.role-mark > path')].find((e) => e.__data__ && e.__data__.datum && Object.values(e.__data__.datum).includes(name));
          const r = p.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2];
        }""",
        top,
    )
    page.mouse.click(box[0], box[1])
    pinned = page.locator(".viz-tooltip.pinned")
    pinned.wait_for()
    pinned.get_by_role("button", name="✕ 排除").click()
    return str(top)


def test_stop_suspends_the_view_until_resumed_or_undone(engine: str) -> None:
    """單獨檢視的長條圖：網路每個請求晚 2.5 秒。排除最上面的 Copiers 之後「更新中…」有「停止」；按下去之後
    render 的請求中止、以同一個查詢代號送出停止，檢視停在之前的結果（Copiers 還在、淡化），標示「已停止更新」——
    之後原本的回應也不會畫上去。「繼續更新」重新查詢，Copiers 不見。再排除最上面的長條、停止之後按工具列的復原：
    回到畫面上的狀態，不再查詢、恢復更新（之後的排除照常更新）。"""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        requests = Requests()
        requests.watch(page)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/ws_sub_category_profit")
        page.wait_for_selector(".single-sheet .chart[data-drawn='true']")
        worksheet = page.locator(".single-sheet .worksheet")
        before = page.evaluate(BARS)
        assert before[0] == "Copiers"
        assert all(q for _, q in requests.renders)  # 每次查詢都有代號

        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        latency(cdp, LATENCY)
        exclude_top_bar(page)
        indicator = page.locator(".single-sheet .view-updating")
        indicator.wait_for(state="visible", timeout=5000)
        pending = requests.renders[-1][1]
        indicator.get_by_role("button", name="停止").click()
        indicator.wait_for(state="detached", timeout=2000)
        suspended = page.locator(".single-sheet .view-suspended")
        expect(suspended).to_contain_text("已停止更新")
        expect(worksheet).to_have_attribute("data-stale", "true")
        expect(worksheet).to_have_attribute("data-updating", "false")
        deadline = time.monotonic() + 5
        while pending not in requests.aborted or pending not in requests.stops:
            assert time.monotonic() < deadline, (requests.aborted, requests.stops)
            page.wait_for_timeout(100)
        # 原本的回應晚到也不畫上去：仍是之前的結果
        page.wait_for_timeout(LATENCY + 800)
        assert page.evaluate(BARS) == before
        expect(worksheet).to_have_attribute("data-stale", "true")

        # 繼續更新：重新查詢，Copiers 不見
        count = len(requests.renders)
        suspended.get_by_role("button", name="繼續更新").click()
        expect(suspended).to_have_count(0)
        # 重新查詢在按下之後的下一次更新才送出：先等它送出，再等畫好（只等「不在更新中」會在送出之前就成立，
        # 2026-11-24 的完整測試中電腦忙時讀到舊的長條）
        deadline = time.monotonic() + 10
        while len(requests.renders) < count + 1:
            assert time.monotonic() < deadline, "繼續更新之後沒有重新查詢"
            page.wait_for_timeout(100)
        page.wait_for_function(
            "() => { const w = document.querySelector('.single-sheet .worksheet'); return w.dataset.updating === 'false' && w.dataset.suspended === 'false'; }",
            timeout=30000,
        )
        page.wait_for_function(f"!({BARS})().includes('Copiers')", timeout=30000)
        assert len(requests.renders) == count + 1
        after = page.evaluate(BARS)
        assert "Copiers" not in after and after[0] == before[1]

        # 再排除一個、停止，然後復原：回到畫面上的狀態，不查詢、恢復更新
        exclude_top_bar(page)
        indicator.wait_for(state="visible", timeout=5000)
        indicator.get_by_role("button", name="停止").click()
        expect(worksheet).to_have_attribute("data-stale", "true")
        count = len(requests.renders)
        page.locator(".toolbar").get_by_role("button", name="復原", exact=True).click()
        expect(worksheet).to_have_attribute("data-suspended", "false")
        expect(suspended).to_have_count(0)
        page.wait_for_timeout(800)
        assert len(requests.renders) == count
        assert page.evaluate(BARS) == after
        assert worksheet.get_attribute("data-updating") == "false"

        # 恢復之後照常更新
        latency(cdp, 0)
        exclude_top_bar(page)
        deadline = time.monotonic() + 30
        while page.evaluate(BARS)[0] == after[0]:
            assert time.monotonic() < deadline
            page.wait_for_timeout(100)
        assert len(requests.renders) == count + 1
        assert errors == []
        browser.close()


def test_stop_on_a_dashboard_pauses_every_sheet(engine: str) -> None:
    """dashboard：改 Category 的篩選之後 Sales by Region 更新中，按「停止」——四張工作表都暫停（停止的是整個
    互動）。之後改 Segment 的篩選，Region Share by Year 不查詢、標示已停止更新。按其中一張的「繼續更新」，兩張都以
    目前的篩選重新查詢、恢復。"""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        requests = Requests()
        requests.watch(page)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        page.wait_for_function(
            'document.querySelectorAll(\'.dashboard .worksheet[data-loading="false"][data-updating="false"]\').length === 4',
            timeout=30000,
        )
        sheets = page.locator(".dashboard .worksheet")

        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        latency(cdp, LATENCY)
        category = page.locator("[data-zone='z12']")
        category.locator("button.dropdown").click()
        category.get_by_label("Office Supplies").click()
        page.keyboard.press("Escape")
        region = page.locator("[data-zone='z7']")
        region.locator(".view-updating").wait_for(state="visible", timeout=5000)
        region.locator(".view-updating").get_by_role("button", name="停止").click()
        for i in range(4):
            expect(sheets.nth(i)).to_have_attribute("data-suspended", "true")
        expect(region.locator(".view-suspended")).to_be_visible()
        share = page.locator("[data-zone='z9']")
        expect(share.locator(".view-suspended")).to_have_count(0)

        # 暫停中改 Segment：Region Share by Year 不查詢，標示已停止更新
        count = len(requests.renders)
        segment = page.locator("[data-zone='z14']")
        segment.locator("button.dropdown").click()
        segment.get_by_label("Home Office").click()
        page.keyboard.press("Escape")
        expect(share.locator(".view-suspended")).to_be_visible()
        page.wait_for_timeout(500)
        assert len(requests.renders) == count

        # 繼續更新：兩張都重新查詢，全部恢復
        share.locator(".view-suspended").get_by_role("button", name="繼續更新").click()
        page.wait_for_function(
            'document.querySelectorAll(\'.dashboard .worksheet[data-suspended="false"][data-updating="false"]\').length === 4',
            timeout=30000,
        )
        expect(page.locator(".dashboard .view-suspended")).to_have_count(0)
        assert {sheet for sheet, _ in requests.renders[count:]} == {
            "ws_sales_by_region",
            "ws_region_share_by_year",
        }
        latency(cdp, 0)
        assert errors == []
        browser.close()


def test_stop_in_the_editor_resumes_on_the_next_edit(engine: str) -> None:
    """桌面版編輯器：交換欄與列之後更新中按「停止」，檢視停在之前的圖、標示已停止更新；再改一次工作表（再交換
    回來）就恢復更新（與 Tableau Desktop 停止查詢之後改變檢視就重新查詢相同），以新的定義查詢、畫好。"""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        requests = Requests()
        requests.watch(page)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            ".editor .worksheet[data-loading='false'] .chart[data-drawn='true']", timeout=30000
        )
        worksheet = page.locator(".editor .worksheet")

        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        latency(cdp, LATENCY)
        tool(page, "交換").click()
        indicator = page.locator(".editor .view-updating")
        indicator.wait_for(state="visible", timeout=15000)
        indicator.get_by_role("button", name="停止").click()
        expect(worksheet).to_have_attribute("data-stale", "true")
        expect(page.locator(".editor .view-suspended")).to_contain_text("已停止更新")

        latency(cdp, 0)
        count = len(requests.renders)
        tool(page, "交換").click()
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_function(
            "() => { const w = document.querySelector('.editor .worksheet'); return w.dataset.suspended === 'false' && w.dataset.updating === 'false' && w.dataset.loading === 'false'; }",
            timeout=30000,
        )
        expect(page.locator(".editor .view-suspended")).to_have_count(0)
        assert len(requests.renders) > count
        assert errors == []
        browser.close()
