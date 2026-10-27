"""檢視器端到端（F3、F4）：dashboard 區塊位置與 IR 相同；切換各組篩選後，畫面上的數字與預期 CSV 一致。

需要先建置檢視器（cd web && pnpm install && pnpm build）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import QueryResult
from tmig.ir import iter_zones, load_ir
from tmig.planner.state import QueryState
from tmig.sqlgen.query import OutputColumn, worksheet_sql
from tmig.verify.cases import load_cases
from tmig.verify.compare import compare
from tmig.verify.mapping import load_mapping
from tmig_service.background import running_server

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
DIST = ROOT / "web" / "dist"


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


def typed(raw: str, column: OutputColumn) -> Any:
    if raw == "":
        return None
    if column.datatype == "integer":
        return int(raw)
    if column.datatype == "real":
        return float(raw)
    if column.datatype == "boolean":
        return raw == "true"
    return raw


def test_dashboard_zones_match_ir(server: str) -> None:
    from playwright.sync_api import sync_playwright

    ir = load_ir(DEMO / "golden.ir.json")
    board = ir.dashboards[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{server}/w/superstore_demo/d/{board.id}")
        page.wait_for_selector(".dashboard")
        origin = page.locator(".dashboard").bounding_box()
        assert origin is not None
        assert round(origin["width"]) == board.size.width
        assert round(origin["height"]) == board.size.height
        for zone in iter_zones(board.zones):
            if zone.kind == "container":
                continue
            box = page.locator(f"[data-zone='{zone.id}']").bounding_box()
            assert box is not None, zone.id
            actual = (box["x"] - origin["x"], box["y"] - origin["y"], box["width"], box["height"])
            assert actual == pytest.approx((zone.x, zone.y, zone.w, zone.h), abs=1.5), zone.id
        browser.close()


def test_cases_show_expected_numbers(server: str) -> None:
    from playwright.sync_api import sync_playwright

    ir = load_ir(DEMO / "golden.ir.json")
    cases = load_cases(DEMO / "cases.yaml")
    mapping = load_mapping(DEMO / "mapping.yaml")
    board = ir.dashboards[0]
    checked = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        for case in cases.cases:
            captured: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            calls: list[str] = []

            def on_response(
                response: Any, captured: dict[str, Any] = captured, calls: list[str] = calls
            ) -> None:
                if response.url.endswith("/render") and response.ok:
                    body = response.request.post_data_json or {}
                    data = response.json()
                    captured[data["worksheet"]] = (body, data)
                    calls.append(data["worksheet"])

            page.on("response", on_response)
            page.goto(f"{server}/w/superstore_demo/d/{board.id}?case={case.name}")
            page.wait_for_function(
                "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
            )
            page.wait_for_timeout(300)
            page.remove_listener("response", on_response)
            assert set(captured) == {s.worksheet for s in cases.sheets}, case.name
            # 篩選組合套用後才查詢：每張工作表只查一次
            assert sorted(calls) == sorted(captured), (case.name, calls)
            for sheet in cases.sheets:
                body, data = captured[sheet.worksheet]
                state = QueryState.model_validate(body)
                columns = worksheet_sql(ir, sheet.worksheet, state).columns
                by_key = {c.key: c for c in columns}
                keys = data["table"]["keys"]
                result = QueryResult(
                    columns=keys,
                    rows=[
                        tuple(typed(v, by_key[k]) for v, k in zip(row, keys, strict=True))
                        for row in data["table"]["raw"]
                    ],
                )
                expected = (DEMO / "expected" / sheet.worksheet / f"{case.name}.csv").read_text(
                    encoding="utf-8"
                )
                comparison = compare(expected, result, columns, mapping.sheets[sheet.worksheet])
                assert comparison.ok, (
                    case.name,
                    sheet.worksheet,
                    json.dumps(comparison.__dict__, default=str)[:500],
                )
                checked += 1
        browser.close()
    assert checked == 16
