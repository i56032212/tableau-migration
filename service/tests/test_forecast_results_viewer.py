"""編輯器的端到端：量值膠囊選單的「預測結果」（實際值與預測值、趨勢、精度、精度 %、品質、上層／較低預測區間、
無）與描述預測的摘要（初始、相對於初始值的變化、季節影響、貢獻）。需要建置好的檢視器（web/dist）與 Playwright
的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from editor_ui import popup_item, side_tab

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "forecast-e2e-token-0123456789"


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
    base = f"http://127.0.0.1:{port}"
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    call(base, "/api/desktop/open", {"path": str(source)})
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def call(base: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def test_forecast_result_menu_and_summary(engine: str) -> None:
    """Monthly Profit 加上預測（正好 6 個月）之後：SUM(Profit) 的膠囊選單有「預測結果」，預設勾「實際值與預測值」；
    改成「上層預測區間」時膠囊寫出結果類型，只有估計的月份有值；改成「無」時不預測（預測仍開著）；改回之後
    「描述預測」的摘要列出初始（估計值 ± 精度）、相對於初始值的變化、貢獻。移除預測時膠囊上的結果類型一併清除。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_monthly_profit")["worksheet"]

    def pill(page: Page) -> Any:
        return page.locator('[data-shelf="rows"] .pill .pill-text').first

    def choose(page: Page, label: str) -> None:
        page.locator('[data-shelf="rows"] .pill').first.click()
        popup_item(page, "預測結果").hover()
        popup_item(page, label).click()
        settle(page)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        # 沒有預測時膠囊選單沒有「預測結果」
        page.locator('[data-shelf="rows"] .pill').first.click()
        expect(page.locator(".popup-menu .menu-label", has_text="預測結果")).to_have_count(0)
        page.keyboard.press("Escape")

        side_tab(page, "分析")
        page.get_by_role("button", name="＋ 預測…").click()
        dialog = page.locator(".forecast-dialog")
        dialog.get_by_label("正好").check()
        dialog.get_by_label("預測的期數").fill("6")
        dialog.get_by_label("預測的期間單位").select_option("month")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        settle(page)

        # 選單：預設是實際值與預測值
        page.locator('[data-shelf="rows"] .pill').first.click()
        popup_item(page, "預測結果").hover()
        expect(popup_item(page, "實際值與預測值")).to_have_attribute("aria-checked", "true")
        expect(popup_item(page, "趨勢")).to_have_attribute("aria-checked", "false")
        page.keyboard.press("Escape")

        choose(page, "上層預測區間")
        expect(pill(page)).to_contain_text("SUM(Profit)（上層預測區間）")
        saved = sheet()
        assert saved["rows"][0].get("forecast") == "upper"
        assert [m.get("forecast") for m in saved["forecast"]["measures"]] == ["upper"]
        table = call(engine, "/api/workbooks/demo/worksheets/ws_monthly_profit/render", {})["table"]
        at = table["keys"].index("sum(f_profit)~upper")
        assert (
            sum(1 for row in table["raw"] if row[at] != "") == 7
        )  # 忽略的最後一個月與之後的 6 個月

        # 無：不預測，預測仍開著
        choose(page, "無")
        saved = sheet()
        assert saved["rows"][0].get("forecast") == "none"
        assert saved["forecast"] is not None and saved["forecast"]["measures"] == []
        expect(pill(page)).to_have_text("SUM(Profit)")

        choose(page, "實際值與預測值")
        assert sheet()["rows"][0].get("forecast") is None

        # 描述預測的摘要
        page.get_by_role("button", name="描述預測…").click()
        summary = page.locator("[data-forecast-summary]")
        expect(summary).to_be_visible()
        row = summary.locator("tbody tr").first
        expect(row.locator("[data-initial]")).to_contain_text("±")
        expect(row.locator("[data-change]")).to_contain_text("–")
        trend = row.locator("[data-contribution-trend]").inner_text()
        season = row.locator("[data-contribution-season]").inner_text()
        assert float(trend.rstrip("%")) + float(season.rstrip("%")) == pytest.approx(100, abs=0.2)
        # 摘要與模型的數字不在中間斷行（桌面版視窗裡看到 15,984.6 ± 6,100.53 被拆成好幾行、Profit 拆成兩行）
        wrapped = page.evaluate(
            """() => [...document.querySelectorAll('.describe-dialog .describe-table.wide td')]
              .filter((td) => td.textContent.trim() !== '')
              .filter((td) => {
                const range = document.createRange();
                range.selectNodeContents(td);
                const tops = new Set([...range.getClientRects()].map((r) => Math.round(r.top)));
                return tops.size > 1;
              })
              .map((td) => td.textContent.trim())"""
        )
        assert wrapped == []
        # 對話框依表的寬度放寬（原本固定 560 像素、摘要要橫向捲動才看得完）：摘要整張在對話框裡
        widths = page.evaluate(
            """() => [document.querySelector('.modal:has(.describe-dialog)').getBoundingClientRect().width,
              document.querySelector('.describe-dialog').clientWidth,
              document.querySelector('[data-forecast-summary]').getBoundingClientRect().width]"""
        )
        assert widths[0] > 560 and widths[2] <= widths[1], widths
        page.locator(".describe-dialog").get_by_role("button", name="關閉").click()

        # 移除預測：膠囊上的結果類型一併清除
        choose(page, "較低預測區間")
        assert sheet()["rows"][0].get("forecast") == "lower"
        page.get_by_role("button", name="預測 ✓").click()
        page.locator(".forecast-dialog").get_by_role("button", name="移除預測").click()
        settle(page)
        saved = sheet()
        assert saved["forecast"] is None and saved["rows"][0].get("forecast") is None
        expect(pill(page)).to_have_text("SUM(Profit)")
        assert errors == []
        browser.close()


def test_forecast_options_keep_the_aggregate_by(engine: str) -> None:
    """預測選項的「彙總依據」（Tableau 的 Aggregate by，從 .twb 解析成 Forecast.aggregate_by）：對話框顯示目前的
    設定，選項是「自動」與檢視的時間單位、比它細的單位；按「確定」時照樣存下（原本對話框重建預測的設定時把它丟掉，
    從 Tableau 開啟的報表在編輯器裡存一次預測選項就改回自動）；改成「自動」時拿掉。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_monthly_profit")["worksheet"]

    body = sheet()
    body["columns"][0]["date"]["unit"] = "year"
    body["forecast"] = {
        "measures": [{"field": "f_profit", "agg": "sum", "continuous": True}],
        "aggregate_by": "month",
    }
    request = urllib.request.Request(
        f"{engine}/api/edit/demo/worksheets/ws_monthly_profit",
        data=json.dumps({"worksheet": body}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(request, timeout=60).close()
    assert sheet()["forecast"]["aggregate_by"] == "month"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_monthly_profit")
        settle(page)
        side_tab(page, "分析")

        page.get_by_role("button", name="預測 ✓").click()
        dialog = page.locator(".forecast-dialog")
        choice = dialog.get_by_label("彙總依據")
        expect(choice).to_have_value("month")
        options = choice.locator("option").all_inner_texts()
        assert options == ["自動", "年", "季", "月", "週", "日", "小時"]
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        settle(page)
        assert sheet()["forecast"].get("aggregate_by") == "month"

        page.get_by_role("button", name="預測 ✓").click()
        dialog.get_by_label("彙總依據").select_option("")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        settle(page)
        assert sheet()["forecast"].get("aggregate_by") is None
        assert errors == []
        browser.close()
