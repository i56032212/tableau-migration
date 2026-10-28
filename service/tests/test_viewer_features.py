"""功能報表的檢視器端到端：篩選群組與全域篩選的控制項一次更新所有相關工作表（H1）。

需要先建置檢視器（cd web && pnpm install && pnpm build）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import execute, format_value
from tmig.ir import load_ir
from tmig.planner.state import QueryState
from tmig.sqlgen.query import worksheet_sql
from tmig.verify.run import ensure_data
from tmig_service.background import running_server

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "fixtures" / "superstore_features"
DIST = ROOT / "web" / "dist"
SHEETS = {"ws_sales_by_segment", "ws_profit_by_ship_mode", "ws_orders_by_region"}


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
def server() -> Any:
    with running_server(ROOT / "fixtures", DIST) as url:
        yield url


class Renders:
    """記錄檢視器發出的 render 請求與回應。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def __call__(self, response: Any) -> None:
        if response.url.endswith("/render") and response.ok:
            data = response.json()
            self.items.append((data["worksheet"], response.request.post_data_json or {}, data))

    def wait_for(self, page: Any, worksheets: set[str], since: int, timeout: float = 30) -> None:
        # 同步 API 只在呼叫 Playwright 時處理事件，所以以 wait_for_timeout 等待
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if worksheets <= {w for w, _, _ in self.items[since:]}:
                return
            page.wait_for_timeout(50)
        raise AssertionError(f"沒有重新查詢 {worksheets - {w for w, _, _ in self.items[since:]}}")


def check_numbers(ir: Any, worksheet: str, body: dict[str, Any], data: dict[str, Any]) -> None:
    """畫面上的數字與以同樣狀態直接查詢的結果相同。"""
    query = worksheet_sql(ir, worksheet, QueryState.model_validate(body))
    sheet = next(w for w in ir.worksheets if w.id == worksheet)
    result = execute(FEATURES / "data" / f"{sheet.datasource}.duckdb", query.sql)
    index = {key: i for i, key in enumerate(result.columns)}
    keys = data["table"]["keys"]
    expected = [[format_value(row[index[k]]) for k in keys] for row in result.rows]
    assert data["table"]["raw"] == expected, worksheet


def test_filter_group_and_global_filter_controls(server: str) -> None:
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = ir.dashboards[0]
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        renders.wait_for(page, SHEETS, 0)
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 3"
        )

        # 篩選群組：Segment 套用到 Sales by Segment 與 Profit by Ship Mode
        start = len(renders.items)
        segment = page.locator("[data-zone='z11']")
        segment.locator("button.dropdown").click()
        segment.get_by_label("Corporate").click()
        renders.wait_for(page, {"ws_sales_by_segment", "ws_profit_by_ship_mode"}, start)
        page.wait_for_timeout(500)
        changed = renders.items[start:]
        assert {w for w, _, _ in changed} == {"ws_sales_by_segment", "ws_profit_by_ship_mode"}
        for worksheet, body, data in changed:
            assert body["filters"]["f_segment"] == {"mode": "include", "values": ["Consumer"]}
            check_numbers(ir, worksheet, body, data)

        # 全域篩選：Region 套用到所有工作表
        start = len(renders.items)
        region = page.locator("[data-zone='z12']")
        region.locator("button.dropdown").click()
        region.get_by_label("East").click()
        renders.wait_for(page, SHEETS, start)
        page.wait_for_timeout(500)
        latest = {w: (body, data) for w, body, data in renders.items[start:]}
        assert set(latest) == SHEETS
        for worksheet, (body, data) in latest.items():
            assert body["filters"]["f_region"] == {"mode": "include", "values": ["South", "West"]}
            check_numbers(ir, worksheet, body, data)
        browser.close()
