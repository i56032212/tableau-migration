"""編輯器的端到端：格式對話框的表頭框線、合計與總計的框線（儲存格與窗格的框線旁邊）。存進工作表，畫面上的
列標題區域有表頭的框線、總計的列上面有總計的框線。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；
缺少時略過。"""

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
from editor_ui import menubar

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "borders-e2e-token-0123456789"


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


def call(base: str, path: str, body: dict[str, Any] | None = None, method: str = "") -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def use(field: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": field,
        "agg": None,
        "date": None,
        "continuous": False,
        "table_calc": None,
        **extra,
    }


def test_header_and_total_borders_in_format_dialog(engine: str) -> None:
    """Region 的長條圖加上列的總計：格式對話框勾「表頭的框線」（綠色、2 像素）與「列的總計的框線」（紅色、
    3 像素），存進工作表；列標題區域有表頭的框線（每一區之間），總計的列上面是紅色的線。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Borders"})["worksheet"]
    body = {
        **created,
        "rows": [use("f_region")],
        "columns": [use("f_sales", agg="sum", continuous=True)],
        "totals": ["rows"],
    }
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, "PUT")

    def current() -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]

    def layers(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for layer in node.get("layer", []):
            name = str(layer.get("name", ""))
            if name.startswith("__deco__divider_rows"):
                found[name] = layer
            found.update(layers(layer))
        return found

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        settle(page)
        menubar(page, "格式", "格式…")
        dialog = page.locator(".format-dialog")
        expect(dialog.get_by_label("表頭框線的樣式")).to_have_count(0)  # 沒有勾選時不顯示細部
        dialog.get_by_label("表頭的框線").check()
        dialog.get_by_label("表頭框線的線寬").fill("2")
        dialog.get_by_label("表頭框線的顏色").fill("#00aa00")
        dialog.get_by_label("列的總計的框線").check()
        dialog.get_by_label("列的總計框線的線寬").fill("3")
        dialog.get_by_label("列的總計框線的顏色").fill("#ff0000")
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        page.wait_for_timeout(300)
        settle(page)
        saved = current()
        assert saved["header_border"] == {"pattern": "solid", "width": 2, "color": "#00aa00"}
        assert saved["total_borders"] == [
            {
                "shelf": "rows",
                "kind": "grand_total",
                "border": {"pattern": "solid", "width": 3, "color": "#ff0000"},
            }
        ]
        rendered = call(engine, f"/api/workbooks/demo/worksheets/{created['id']}/render", {})
        found = layers(rendered["spec"])
        assert found["__deco__divider_rows_border"]["mark"]["color"] == "#00aa00"
        assert found["__deco__divider_rows_total0"]["mark"]["color"] == "#ff0000"
        # 畫面上：標題區域的線（rule）——表頭的框線在各區之間，總計的列上面一條紅線
        drawn = page.evaluate(
            """() => {
                 const svg = document.querySelector('.editor .worksheet .frozen-host > svg')
                   ?? document.querySelector('.editor .worksheet svg');
                 const rules = (name) => [...svg.querySelectorAll(`[class*='${name}'] line, [class*='${name}'] path`)]
                   .filter((el) => el.getAttribute('stroke-opacity') !== '0');
                 return {
                   border: rules('__deco__divider_rows_border').length,
                   total: rules('__deco__divider_rows_total0').map((el) => el.getAttribute('stroke')),
                 };
               }"""
        )
        assert drawn["border"] == 3, drawn  # 四區之間（最後一區與總計之間是總計的線）
        assert drawn["total"] == ["#ff0000"], drawn
        # 重新開啟對話框：設定還在
        menubar(page, "格式", "格式…")
        dialog = page.locator(".format-dialog")
        expect(dialog.get_by_label("表頭的框線")).to_be_checked()
        expect(dialog.get_by_label("列的總計的框線")).to_be_checked()
        expect(dialog.get_by_label("欄的合計的框線")).not_to_be_checked()
        dialog.get_by_label("列的總計的框線").uncheck()
        page.get_by_role("dialog").get_by_role("button", name="確定").click()
        page.wait_for_timeout(300)
        settle(page)
        assert current()["total_borders"] == []
        assert errors == []
        browser.close()
