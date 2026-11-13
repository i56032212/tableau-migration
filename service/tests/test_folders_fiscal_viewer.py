"""資料窗格的資料夾、合併欄位與會計年度（端到端）：欄位選單「資料夾＞建立資料夾…」、把欄位拖到資料夾的
名稱上、「建立＞合併欄位…」、資料窗格的「日期屬性…」與日期欄位的「預設屬性＞會計年度起始…」；放到檢視上
查詢，年的標題加上 FY。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import re
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
TOKEN = "folders-fiscal-e2e-token-0123456789"
ORDERS = (
    "Order Date,Region,Product,Sales\n"
    "2004-03-31,East,Chair,1\n"
    "2004-04-01,East,Desk,2\n"
    "2004-06-15,West,Chair,4\n"
    "2004-07-01,West,Lamp,8\n"
    "2005-01-10,East,Lamp,16\n"
    "2005-04-02,South,Desk,32\n"
)


def chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not (DIST / "index.html").is_file() or not chromium(),
        reason="需要建置好的檢視器（web/dist）與 Playwright 的 Chromium",
    ),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


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


def test_folders_combined_and_fiscal_year(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    path = tmp_path / "orders.csv"
    path.write_text(ORDERS, encoding="utf-8")
    created = call(engine, "/api/desktop/new", {"path": str(path)})
    workbook = created["document"]["id"]

    def catalog() -> dict[str, Any]:
        return call(engine, f"/api/edit/{workbook}/catalog")["datasources"][0]

    ids = {f["caption"]: f["id"] for f in catalog()["fields"]}

    def render(rows: dict[str, Any]) -> dict[str, str]:
        sheet = call(engine, f"/api/edit/{workbook}/worksheets/ws_1")["worksheet"]
        sheet.update(rows=[rows], columns=[{"field": ids["Sales"], "agg": "sum"}])
        call(engine, f"/api/edit/{workbook}/worksheets/ws_1", {"worksheet": sheet}, "PUT")
        body = call(engine, f"/api/workbooks/{workbook}/worksheets/ws_1/render", {})
        return {row[0]: row[1] for row in body["table"]["rows"]}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/{workbook}/edit/ws_1")
        page.wait_for_selector('.editor[data-saving="false"]')
        pane = page.locator(".data-pane")

        def menu(caption: str, *path: str) -> None:
            pane.locator(f'li[data-field="{ids[caption]}"]').click(button="right")
            popup = page.locator(".popup-menu")
            popup.wait_for()

            def item(label: str) -> Any:
                exact = re.compile("^" + re.escape(label) + "$")
                return popup.locator(".menu-label").filter(has_text=exact).first

            for label in path[:-1]:
                item(label).hover()
            item(path[-1]).click()

        # 資料夾：Region 建立資料夾「Places」
        menu("Region", "資料夾", "建立資料夾…")
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("資料夾名稱").fill("Places")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        folder = pane.locator('li.folder[data-folder="Places"]')
        folder.wait_for()
        folder.locator(f'li[data-field="{ids["Region"]}"]').wait_for()
        # 把 Product 拖到資料夾的名稱上
        pane.locator(f'li[data-field="{ids["Product"]}"]').drag_to(folder.locator(".folder-name"))
        folder.locator(f'li[data-field="{ids["Product"]}"]').wait_for()
        assert catalog()["folders"] == [
            {"name": "Places", "fields": [ids["Region"], ids["Product"]], "role": None}
        ]
        # 從資料夾移除
        menu("Product", "資料夾", "從資料夾移除")
        folder.locator(f'li[data-field="{ids["Product"]}"]').wait_for(state="detached")
        # 合併欄位：Region 與 Product
        menu("Region", "建立", "合併欄位…")
        dialog = page.get_by_role("dialog", name="建立合併欄位")
        dialog.get_by_label("Product", exact=True).check()
        assert "Region & Product (Combined)" in dialog.inner_text()
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        combined = pane.locator("li.field", has_text="Region & Product (Combined)")
        combined.wait_for()
        assert "🔗" in combined.inner_text()
        field = next(
            f for f in catalog()["fields"] if f["caption"] == "Region & Product (Combined)"
        )
        assert field["combined"] == [ids["Region"], ids["Product"]]
        assert render({"field": field["id"]})["West, Lamp"] == "8"
        # 日期屬性：會計年度從四月開始
        pane.get_by_role("button", name="日期屬性…").click()
        dialog = page.get_by_role("dialog", name=re.compile("^日期屬性"))
        dialog.get_by_label("會計年度的起始月").select_option("4")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        year = {"field": ids["Order Date"], "date": {"kind": "part", "unit": "year"}}
        assert render(year) == {"FY 2004": "1", "FY 2005": "30", "FY 2006": "32"}
        # 欄位自訂：七月
        menu("Order Date", "預設屬性", "會計年度起始…")
        dialog = page.get_by_role("dialog")
        assert "沿用資料源（四月）" in dialog.inner_text()
        dialog.get_by_label("會計年度的起始月").select_option("7")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        assert render(year) == {"FY 2004": "7", "FY 2005": "56"}
        # 檢視的標題（欄標題）顯示 FY
        page.goto(f"{engine}/w/{workbook}/edit/ws_1")
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        assert "FY 2005" in page.locator(".editor .worksheet").inner_text()
        # 刪除資料夾：欄位回到最上層
        folder = pane.locator('li.folder[data-folder="Places"]')
        folder.locator(".folder-name").hover()
        folder.get_by_role("button", name="刪除資料夾 Places").click()
        folder.wait_for(state="detached")
        assert catalog()["folders"] == []
        assert errors == []
        browser.close()
