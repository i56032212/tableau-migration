"""桌面版編輯器的端到端：拖放欄位、膠囊選單、快速 Table Calculation、復原、篩選、計算欄位、新增工作表、
雙軸與存檔。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
import zipfile
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
TOKEN = "editor-e2e-token-0123456789"


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


def test_edit_worksheet_in_viewer(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def pills(page: Page, shelf: str) -> list[str]:
        return [
            t.replace("▾", "").strip()
            for t in page.locator(f'[data-shelf="{shelf}"] .pill .pill-text').all_inner_texts()
        ]

    def menu(page: Page, *labels: str) -> None:
        for i, label in enumerate(labels):
            item = page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text=label).first
            if i < len(labels) - 1:
                item.hover()
            else:
                item.click()

    def table(worksheet: str) -> dict[str, Any]:
        return call(engine, f"/api/workbooks/demo/worksheets/{worksheet}/render", {})["table"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.get_by_role("button", name="編輯工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        assert pills(page, "rows") == ["Region"]
        assert pills(page, "columns") == ["SUM(Sales)"]

        # 拖放：Segment 放到列
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="rows"]')
        )
        settle(page)
        assert pills(page, "rows") == ["Region", "Segment"]
        data = table("ws_sales_by_region")
        assert "f_segment" in data["keys"]

        # 膠囊選單：彙總改成平均，再加上快速 Table Calculation（累計）
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "量值", "平均")
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales)"]
        page.locator('[data-shelf="columns"] .pill', has_text="AVG(Sales)").click()
        menu(page, "快速表計算", "累計")
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales) △"]
        assert any(k.startswith("avg(f_sales)#tc_") for k in table("ws_sales_by_region")["keys"])

        # 復原兩次、重做一次
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        assert pills(page, "columns") == ["SUM(Sales)"]
        page.get_by_role("button", name="↷ 重做").click()
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales)"]
        assert "avg(f_sales)" in table("ws_sales_by_region")["keys"]

        # 篩選：排除 Corporate
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="filters"]')
        )
        page.wait_for_selector(".filter-dialog .value-list li")
        page.locator(".value-list label", has_text="Corporate").locator("input").uncheck()
        page.locator(".filter-dialog").get_by_role("button", name="確定").click()
        settle(page)
        assert "Segment" in pills(page, "filters")
        data = table("ws_sales_by_region")
        segment = data["keys"].index("f_segment")
        assert {row[segment] for row in data["raw"]} == {"Consumer", "Home Office"}

        # 計算欄位：建立後放到色彩
        page.get_by_role("button", name="建立計算欄位…").click()
        page.get_by_label("計算欄位名稱").fill("Profit Ratio")
        page.get_by_label("公式").fill("SUM([Profit]) / SUM([Sales])")
        expect(page.locator(".calc-status")).to_contain_text("計算有效")
        page.locator(".calc-dialog").get_by_role("button", name="確定").click()
        field = page.locator(".data-pane .field", has_text="Profit Ratio")
        expect(field).to_be_visible()
        field.drag_to(page.locator('[data-drop="marks.color"]'))
        settle(page)
        assert pills(page, "marks")[0] == "Profit Ratio"

        # 新增工作表：連按兩下加入欄位、改名
        page.get_by_role("button", name="新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_category"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)
        assert (pills(page, "rows"), pills(page, "columns")) == (["Category"], ["SUM(Sales)"])
        assert len(table("ws_1")["rows"]) == 3
        name = page.get_by_label("工作表名稱")
        name.fill("類別銷售")
        name.press("Enter")
        settle(page)
        expect(page.locator(".toolbar select")).to_contain_text("工作表：類別銷售")

        # 雙軸：Profit 與 Sales 重疊，Measure Names 放到色彩
        page.locator('.data-pane [data-field="f_profit"]').drag_to(
            page.locator('[data-drop="columns"]')
        )
        settle(page)
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Profit)").click()
        menu(page, "雙軸")
        settle(page)
        expect(page.locator(".mark-tabs button")).to_have_count(2)
        sheet = call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]
        assert sheet["panes"][0]["dual"] is True
        assert sheet["marks"]["color"]["field"] == "measure_names"

        # 存檔後變更寫進報表套件
        target = tmp_path / "edited.tmigx"
        assert call(engine, "/api/desktop/documents")[0]["modified"] is True
        call(engine, "/api/desktop/save", {"workbook": "demo", "path": str(target)})
        with zipfile.ZipFile(target) as archive:
            saved = json.loads(archive.read("report.ir.json"))
        names = [w["name"] for w in saved["worksheets"]]
        assert "類別銷售" in names
        # 完成編輯：回到工作表的檢視
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/s/ws_1")
        page.wait_for_selector(".single-sheet .worksheet svg")
        assert errors == []
        browser.close()
