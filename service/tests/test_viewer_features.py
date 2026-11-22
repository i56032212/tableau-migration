"""功能報表的檢視器端到端：篩選群組與全域篩選（H1）、自動尺寸（I1）、擴充圖表（J1–J3）、參考線（J4）、
群組（K1）、篩選條件（K4）、篩選、網址、參數與切換動作（L1、L2）、Story。

需要先建置檢視器（cd web && pnpm install && pnpm build）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import shutil
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tmig.data.executor import execute, format_value
from tmig.ir import load_ir
from tmig.planner.drill import apply_drill
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


def check_numbers(
    ir: Any, worksheet: str, body: dict[str, Any], data: dict[str, Any], approx: bool = False
) -> None:
    """畫面上的數字與以同樣狀態直接查詢的結果相同。approx：浮點數只比到有效位數（合計百分比等
    以視窗函式加總的值，加總的順序不同時最後幾位可能不同）。"""
    query = worksheet_sql(ir, worksheet, QueryState.model_validate(body))
    sheet = next(w for w in ir.worksheets if w.id == worksheet)
    result = execute(FEATURES / "data" / f"{sheet.datasource}.duckdb", query.sql)
    index = {key: i for i, key in enumerate(result.columns)}
    keys = data["table"]["keys"]
    expected = [[format_value(row[index[k]]) for k in keys] for row in result.rows]
    if not approx:
        assert data["table"]["raw"] == expected, worksheet
        return

    def close(a: str, b: str) -> bool:
        try:
            return float(a) == pytest.approx(float(b), rel=1e-12)
        except ValueError:
            return a == b

    assert len(data["table"]["raw"]) == len(expected), worksheet
    for got, want in zip(data["table"]["raw"], expected, strict=True):
        assert all(close(a, b) for a, b in zip(got, want, strict=True)), (worksheet, got, want)


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


@pytest.mark.parametrize("viewport", [(1400, 1000), (1100, 760)])
def test_automatic_dashboard_layout(server: str, viewport: tuple[int, int]) -> None:
    from playwright.sync_api import sync_playwright

    from tmig.ir import iter_zones
    from tmig.render.layout import layout_dashboard

    ir = load_ir(FEATURES / "golden.ir.json")
    board = ir.dashboards[0]
    assert board.sizing == "automatic"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_selector("[data-zone='z7']")
        origin = page.locator(".dashboard").bounding_box()
        assert origin is not None
        # 自動尺寸：寬度占滿可用空間
        page_box = page.locator(".dashboard-wrapper").bounding_box()
        assert page_box is not None
        assert origin["width"] == pytest.approx(page_box["width"], abs=1)
        expected = layout_dashboard(board, origin["width"], origin["height"])
        for zone in iter_zones(board.zones):
            if zone.kind == "container":
                continue
            locator = page.locator(f"[data-zone='{zone.id}']")
            if zone.hidden:
                assert locator.count() == 0, zone.id
                continue
            box = locator.bounding_box()
            assert box is not None, zone.id
            actual = (box["x"] - origin["x"], box["y"] - origin["y"], box["width"], box["height"])
            want = expected[zone.id]
            assert actual == pytest.approx((want.x, want.y, want.w, want.h), abs=1.5), zone.id
        # 固定 212 像素寬的篩選欄
        control = page.locator("[data-zone='z11']").bounding_box()
        assert control is not None
        assert control["width"] == pytest.approx(212, abs=1)
        # 不顯示標題的工作表沒有標題列
        assert page.locator("[data-zone='z9'] .worksheet-title").count() == 0
        assert page.locator("[data-zone='z7'] .worksheet-title").count() == 1
        browser.close()


def test_extended_charts_render(server: str) -> None:
    """Measure Names／Values、雙軸與圓餅都畫得出來，沒有錯誤或「未支援」。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "圖表擴充")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(500)
        assert page.locator(".placeholder.error, .placeholder.unsupported").count() == 0
        for name in (
            "ws_segment_kpis",
            "ws_monthly_sales_vs_profit",
            "ws_sales_and_profit_by_year",
            "ws_category_mix",
        ):
            assert page.locator(f"[data-worksheet='{name}'] svg").count() >= 1, name
        # 文字表的 Measure Names 依 Measure Values 卡的順序；CNTD(Order ID) 顯示別名「訂單數」
        headers = page.eval_on_selector_all(
            "[data-worksheet='ws_segment_kpis'] svg text", "els => els.map(e => e.textContent)"
        )
        order = [h for h in headers if h in ("Sales", "Profit", "訂單數", "利潤率")]
        assert order[:4] == ["Sales", "Profit", "訂單數", "利潤率"]
        assert "CNTD(Order ID)" not in headers
        assert errors == []
        browser.close()


def load_oracle() -> ModuleType:
    """scripts/features_oracle.py：以純 Python 獨立計算的預期結果（不屬於 tmig 套件）。"""
    spec = importlib.util.spec_from_file_location(
        "features_oracle", ROOT / "scripts" / "features_oracle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["features_oracle"] = module
    spec.loader.exec_module(module)
    return module


def same_as_oracle(data: dict[str, Any], expected: tuple[list[str], list[list[Any]]]) -> None:
    """畫面上這張工作表的資料與獨立計算的預期結果相同（數字以相對誤差 1e-9 比對）。"""
    _, rows = expected
    actual = [
        [
            float(cell) if isinstance(want, float) else cell
            for cell, want in zip(row, wanted, strict=True)
        ]
        for row, wanted in zip(data["table"]["raw"], rows, strict=True)
    ]
    assert len(data["table"]["raw"]) == len(rows), data["worksheet"]
    assert actual == [
        [pytest.approx(v, rel=1e-9) if isinstance(v, float) else v for v in r] for r in rows
    ]


def test_filter_actions(server: str) -> None:
    """L1：點選 Region Picker 的地區篩選兩張目標工作表；Ctrl 加選；再點一次或清除鈕清除選取。"""
    from playwright.sync_api import sync_playwright

    oracle = load_oracle()
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    targets = {"ws_category_detail", "ws_segment_detail"}
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(300)
        first = {w: data for w, _, data in renders.items}
        # 沒有選取：Category Detail 顯示全部，Segment Detail 不顯示任何值
        same_as_oracle(first["ws_category_detail"], oracle.selection("ws_category_detail", None))
        assert first["ws_segment_detail"]["rows"] == 0
        assert first["ws_region_category_matrix"]["rows"] == 10  # 排除兩個值組合

        def click(region: str, modifiers: list[str] | None = None) -> dict[str, tuple[Any, Any]]:
            start = len(renders.items)
            bar = "svg path[aria-roledescription='bar']"
            page.locator(
                f"[data-worksheet='ws_region_picker'] {bar}[aria-label$='Region: {region}']"
            ).click(modifiers=modifiers or [])  # type: ignore[arg-type]
            renders.wait_for(page, targets, start)
            page.wait_for_timeout(300)
            latest = {w: (body, data) for w, body, data in renders.items[start:]}
            assert set(latest) == targets  # 來源與其他工作表不重新查詢
            return latest

        latest = click("East")
        for worksheet, (body, data) in latest.items():
            action = f"act_filter_{worksheet.removeprefix('ws_')}"
            assert body["actions"] == {action: [{"f_region": "East"}]}
            same_as_oracle(data, oracle.selection(worksheet, ["East"]))
            check_numbers(ir, worksheet, body, data)
        picker = page.locator("[data-worksheet='ws_region_picker']")
        assert picker.locator(".selection").inner_text().startswith("已選取 1 個標記")

        latest = click("West", ["Control"])
        for worksheet, (body, data) in latest.items():
            assert list(body["actions"].values()) == [[{"f_region": "East"}, {"f_region": "West"}]]
            same_as_oracle(data, oracle.selection(worksheet, ["East", "West"]))

        # 清除鈕：Category Detail 回到全部，Segment Detail 回到不顯示任何值
        start = len(renders.items)
        picker.locator(".selection").click()
        renders.wait_for(page, targets, start)
        page.wait_for_timeout(300)
        latest = {w: (body, data) for w, body, data in renders.items[start:]}
        assert latest["ws_category_detail"][0]["actions"] == {}
        same_as_oracle(
            latest["ws_category_detail"][1], oracle.selection("ws_category_detail", None)
        )
        assert latest["ws_segment_detail"][1]["rows"] == 0
        assert picker.locator(".selection").count() == 0

        # 再點一次已選取的標記＝清除
        click("South")
        latest = click("South")
        assert latest["ws_segment_detail"][1]["rows"] == 0
        assert errors == []
        browser.close()


def test_drill_down_in_dashboard(server: str) -> None:
    """階層 Product：Category Detail 的 Category 向下切入到 Sub-Category，與篩選動作一起作用；
    數字與存檔時就把 Sub-Category 放在架上的工作表相同；收合回到存檔時的樣子。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    sheet = "ws_category_detail"
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        view = page.locator(f"[data-worksheet='{sheet}']")
        # 沒有階層的工作表沒有切入的按鈕（Region Picker 的 Region 不在階層中）
        assert page.locator("[data-worksheet='ws_region_picker'] .drill").count() == 0

        def latest(start: int) -> tuple[dict[str, Any], dict[str, Any]]:
            renders.wait_for(page, {sheet}, start)
            page.wait_for_timeout(300)
            body, data = next((b, d) for w, b, d in reversed(renders.items[start:]) if w == sheet)
            return body, data

        start = len(renders.items)
        view.get_by_role("button", name="向下切入到 Sub-Category").click()
        body, data = latest(start)
        assert body["drill"] == {"f_category": 1}
        assert data["rows"] == 17
        assert data["table"]["headers"][:2] == ["Category", "Sub-Category"]
        check_numbers(apply_drill(ir, sheet, body["drill"]), sheet, body, data)
        expect(view.get_by_role("button", name="向下切入到 Product Name")).to_be_visible()

        # 篩選動作的選取與切入一起作用
        start = len(renders.items)
        page.locator(
            "[data-worksheet='ws_region_picker'] svg path[aria-roledescription='bar']"
            "[aria-label$='Region: East']"
        ).click()
        body, data = latest(start)
        assert body["drill"] == {"f_category": 1}
        assert body["actions"] == {"act_filter_category_detail": [{"f_region": "East"}]}
        check_numbers(apply_drill(ir, sheet, body["drill"]), sheet, body, data)

        start = len(renders.items)
        view.get_by_role("button", name="向上收合 Sub-Category").click()
        body, data = latest(start)
        assert "drill" not in body
        assert data["rows"] == 3
        check_numbers(ir, sheet, body, data)
        assert view.get_by_role("button", name="向上收合 Sub-Category").count() == 0
        assert errors == []
        browser.close()


def test_viz_in_tooltip(server: str) -> None:
    """工具提示中的工作表：滑過 Sales by Segment 的長條，工具提示中顯示以滑過的 Segment 篩選的
    Category Mix；數字與直接以同樣狀態查詢相同；移開後工具提示消失。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = ir.dashboards[0]
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        renders.wait_for(page, SHEETS, 0)
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 3"
        )
        tooltip = page.locator(".viz-tooltip")
        bars = page.locator(
            "[data-worksheet='ws_sales_by_segment'] svg path[aria-roledescription='bar']"
        )
        seen: dict[str, dict[str, Any]] = {}
        for segment in ("Consumer", "Corporate"):
            start = len(renders.items)
            bars.and_(page.locator(f"[aria-label*='Segment: {segment}']")).hover()
            tooltip.wait_for()
            renders.wait_for(page, {"ws_category_mix"}, start)
            page.wait_for_timeout(300)
            body, data = next(
                (b, d) for w, b, d in reversed(renders.items[start:]) if w == "ws_category_mix"
            )
            assert body["tooltip"] == {
                "source": "ws_sales_by_segment",
                "mark": {"f_segment": segment},
            }
            check_numbers(ir, "ws_category_mix", body, data, approx=True)
            seen[segment] = data
            sheet = tooltip.locator("[data-tooltip-sheet='ws_category_mix']")
            expect(sheet.locator(".worksheet[data-loading='false'] svg")).to_have_count(1)
            assert segment in tooltip.inner_text()
            box = sheet.bounding_box()
            assert box is not None and box["width"] <= 320 and box["height"] <= 240
        assert seen["Consumer"]["table"]["raw"] != seen["Corporate"]["table"]["raw"]
        page.mouse.move(5, 5)
        expect(tooltip).to_have_count(0)
        assert errors == []
        browser.close()


def test_pinned_tooltip_shows_the_tooltip_sheets(server: str) -> None:
    """點選標記後釘住的工具提示也有工具提示中的工作表（Viz in Tooltip），與滑過時相同：以點選的那個標記篩選，
    數字與直接以同樣狀態查詢相同。觸控的裝置上點標記（沒有滑過）也看得到——原本釘住的工具提示只有文字與命令
    按鈕，觸控的裝置上因此完全看不到工具提示中的工作表。框選多個標記時沒有單一的標記，不顯示工作表。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    source = "ws_sales_by_segment"
    bars = f"[data-worksheet='{source}'] svg path[aria-roledescription='bar']:not(.frozen *)"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        desktop = browser.new_page(viewport={"width": 1400, "height": 1000})
        phone = browser.new_context(**p.devices["Pixel 5"]).new_page()
        errors: list[str] = []
        for page in (desktop, phone):
            renders = Renders()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("response", renders)
            page.goto(f"{server}/w/superstore_features/s/{source}")
            page.wait_for_selector(f".worksheet[data-worksheet='{source}'][data-loading='false']")
            page.wait_for_function(LOADED)
            pinned = page.locator(".viz-tooltip.pinned")
            for segment in ("Corporate", "Consumer"):
                start = len(renders.items)
                bar = page.locator(f"{bars}[aria-label$='Segment: {segment}']")
                if page is desktop:
                    bar.click()
                else:
                    bar.tap()  # 觸控：直接點，沒有滑過
                expect(pinned).to_have_count(1)
                expect(pinned.get_by_role("button", name="✓ 只保留")).to_be_visible()
                renders.wait_for(page, {"ws_category_mix"}, start)
                body, data = next(
                    (b, d) for w, b, d in reversed(renders.items[start:]) if w == "ws_category_mix"
                )
                assert body["tooltip"] == {"source": source, "mark": {"f_segment": segment}}
                check_numbers(ir, "ws_category_mix", body, data, approx=True)
                sheet = pinned.locator("[data-tooltip-sheet='ws_category_mix']")
                expect(sheet.locator(".worksheet[data-loading='false'] svg")).to_have_count(1)
                assert segment in pinned.inner_text()
                box = sheet.bounding_box()
                assert box is not None and box["width"] <= 320 and box["height"] <= 240
                # 整個工具提示都在畫面之內
                frame = pinned.bounding_box()
                size = page.viewport_size
                assert frame is not None and size is not None
                assert frame["x"] >= 0 and frame["x"] + frame["width"] <= size["width"], frame
                page.keyboard.press("Escape")
                expect(pinned).to_have_count(0)

        # 框選兩個標記：釘住的工具提示是「N 個標記」，沒有工具提示中的工作表
        first = desktop.locator(f"{bars}[aria-label$='Segment: Consumer']").bounding_box()
        last = desktop.locator(f"{bars}[aria-label$='Segment: Corporate']").bounding_box()
        assert first is not None and last is not None
        desktop.mouse.move(first["x"] + 4, first["y"] + first["height"] / 2)
        desktop.mouse.down()
        desktop.mouse.move(last["x"] + 30, last["y"] + last["height"] / 2, steps=8)
        desktop.mouse.up()
        pinned = desktop.locator(".viz-tooltip.pinned")
        expect(pinned).to_contain_text("2 個標記")
        expect(pinned.locator("[data-tooltip-sheet]")).to_have_count(0)
        assert errors == []
        browser.close()


def test_phone_layout(server: str) -> None:
    """裝置專屬版面：手機大小的畫面（或網址的 :device=phone）改用「篩選動作」dashboard 的手機版面——
    各工作表由上到下、寬度符合畫面；篩選動作照常作用。桌面大小的畫面用預設版面。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    loaded = "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(loaded)
        assert page.locator(".dashboard").get_attribute("data-device") == "phone"
        boxes = [
            page.locator(f"[data-zone='{z}']").bounding_box() for z in ("z7", "z8", "z10", "z11")
        ]
        assert all(b is not None for b in boxes)
        page_width = page.evaluate("document.documentElement.clientWidth")
        for upper, lower in itertools.pairwise(boxes):
            assert upper is not None and lower is not None
            assert lower["y"] >= upper["y"] + upper["height"] - 1  # 由上到下
            assert abs(lower["x"] - upper["x"]) < 1 and abs(lower["width"] - upper["width"]) < 1
        first = boxes[0]
        assert first is not None and first["height"] == 300 and first["width"] < page_width
        # 篩選動作照常作用
        start = len(renders.items)
        page.locator(
            "[data-worksheet='ws_region_picker'] svg path[aria-roledescription='bar']"
            "[aria-label$='Region: East']:not(.frozen *)"
        ).click()
        renders.wait_for(page, {"ws_category_detail", "ws_segment_detail"}, start)
        latest = {w: body for w, body, _ in renders.items[start:]}
        assert latest["ws_category_detail"]["actions"] == {
            "act_filter_category_detail": [{"f_region": "East"}]
        }
        page.close()

        desktop = browser.new_page(viewport={"width": 1300, "height": 900})
        desktop.goto(f"{server}/w/superstore_features/d/{board.id}")
        desktop.wait_for_function(loaded)
        assert desktop.locator(".dashboard").get_attribute("data-device") == "default"
        desktop.goto(f"{server}/w/superstore_features/d/{board.id}?:device=phone")
        desktop.wait_for_function(loaded)
        assert desktop.locator(".dashboard").get_attribute("data-device") == "phone"
        assert errors == []
        browser.close()


def test_annotation_is_drawn(server: str) -> None:
    """註解：Region Picker 上 West 的長條有標記的註解（文字代入地區、畫線連到文字），不影響選取。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{server}/w/superstore_features/s/ws_region_picker")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"] svg')
        text = page.locator(".single-sheet svg text:not(.frozen *)", has_text="最高的地區：West")
        text.first.wait_for()
        assert text.count() == 1
        assert errors == []
        browser.close()


MARKS_JS = """(sheet) => Array.from(document.querySelectorAll(
    `[data-worksheet='${sheet}'] svg [aria-roledescription='text mark'],`
    + `[data-worksheet='${sheet}'] svg [aria-roledescription='bar']`))
  .map((e) => [e.getAttribute('aria-label'), e.getAttribute('opacity')])"""


def opacities(page: Any, sheet: str) -> dict[str, str | None]:
    """工作表上每個標記的說明（aria-label）→ 不透明度（沒有淡化時為 None）。"""
    return dict(page.evaluate(MARKS_JS, sheet))


def lit(page: Any, sheet: str) -> set[str] | None:
    """醒目的標記（其餘淡化）；沒有醒目提示時為 None。"""
    found = opacities(page, sheet)
    if all(v is None for v in found.values()):
        return None
    return {label for label, v in found.items() if v == "1"}


def test_highlight_actions_and_highlighter(server: str) -> None:
    """L3：點選 Region Picker 的地區時 Region Category Matrix 中同一個地區的格子醒目（只比對 Region，
    不重新查詢）；醒目提示控制項輸入 Category；「篩選動作」dashboard 上滑過矩陣的格子時，其他工作表中
    同一個地區、類別的標記醒目（所有欄位）。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "醒目提示")
    matrix = "ws_region_category_matrix"
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 2"
        )
        page.wait_for_timeout(300)
        assert lit(page, matrix) is None
        before = len(renders.items)
        bar = "svg path[aria-roledescription='bar']"
        east = page.locator(
            f"[data-worksheet='ws_region_picker'] {bar}[aria-label$='Region: East']"
        )
        east.click()
        page.wait_for_timeout(400)
        shown = lit(page, matrix)
        assert shown is not None and shown and all("Region: East" in label for label in shown)
        assert len(shown) == 2  # 矩陣排除了 (East, Technology)
        assert len(renders.items) == before  # 醒目提示不重新查詢
        east.click()  # 再點一次＝清除選取，醒目提示跟著清除
        page.wait_for_timeout(400)
        assert lit(page, matrix) is None

        # 醒目提示控制項：輸入部分文字，名稱包含它的類別醒目；清空即取消
        box = page.get_by_label("醒目提示 Category")
        box.fill("furn")
        page.wait_for_timeout(400)
        shown = lit(page, matrix)
        assert shown is not None and len(shown) == 3
        assert all("Category: Furniture" in label for label in shown)
        box.fill("")
        page.wait_for_timeout(400)
        assert lit(page, matrix) is None

        # 滑過時執行、比對所有欄位的醒目提示動作
        filters = next(d for d in ir.dashboards if d.name == "篩選動作")
        page.goto(f"{server}/w/superstore_features/d/{filters.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(300)
        cell = page.locator(
            f"[data-worksheet='{matrix}'] svg [aria-roledescription='text mark']"
            "[aria-label^='Category: Furniture; Region: Central']"
        )
        cell.hover()
        page.wait_for_timeout(400)
        picker = lit(page, "ws_region_picker")  # 長條與它的標籤
        assert picker and all("Region: Central" in label for label in picker)
        page.mouse.move(1, 1)
        page.wait_for_timeout(700)
        assert lit(page, "ws_region_picker") is None
        assert errors == []
        browser.close()


def test_groups_and_bins_render(server: str) -> None:
    """K1：群組與數值分組的 dashboard 畫得出來；分組依下限排序，群組名稱取代成員。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "群組與分組")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 3"
        )
        page.wait_for_timeout(300)
        assert page.locator(".placeholder.error, .placeholder.unsupported").count() == 0

        def texts(worksheet: str) -> list[str]:
            return page.eval_on_selector_all(
                f"[data-worksheet='{worksheet}'] svg text", "els => els.map(e => e.textContent)"
            )

        assert {"Coasts", "Central", "South"} <= set(texts("ws_sales_by_region_group"))
        # 群組「其他」的成員別名：Other 顯示為 Standard
        assert {"Fast", "Standard"} <= set(texts("ws_sales_by_ship_speed"))
        assert "Other" not in texts("ws_sales_by_ship_speed")
        # x 軸的分組標籤依下限排序（之後的 0 是 y 軸的刻度）
        bins = [t for t in texts("ws_sales_by_quantity_bin") if t in {"0", "3", "6", "9", "12"}]
        assert bins[:5] == ["0", "3", "6", "9", "12"]
        assert errors == []
        browser.close()


def test_sets_render(server: str) -> None:
    """集合：IN/OUT 顯示為 In／Out（或欄位實例上的別名），數字與獨立計算的結果相同。"""
    from playwright.sync_api import sync_playwright

    oracle = load_oracle()
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "集合")
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(300)
        assert page.locator(".placeholder.error, .placeholder.unsupported").count() == 0

        def texts(worksheet: str) -> list[str]:
            return page.eval_on_selector_all(
                f"[data-worksheet='{worksheet}'] svg text", "els => els.map(e => e.textContent)"
            )

        assert {"In", "Out"} <= set(texts("ws_top_customers_vs_others"))
        # 依集合著色：dashboard 上的工作表與 Tableau 相同不畫圖例，別名顯示在資料表（與工具提示）中
        sheet = page.locator("[data-worksheet='ws_profitable_sub_categories']")
        sheet.get_by_role("button", name="資料").click()
        assert {"獲利", "虧損"} <= set(sheet.locator(".data-table td").all_inner_texts())
        rows = oracle.load_rows()
        default = oracle.Case("default", {})
        latest = {w: data for w, _, data in renders.items}
        for worksheet, compute in [
            ("ws_top_customers_vs_others", oracle.top_customers_vs_others),
            ("ws_key_sub_categories_by_category", oracle.key_sub_categories),
            ("ws_key_sales_by_region", oracle.key_sales_by_region),
        ]:
            _, expected = compute(rows, default)
            # 資料表的原始值是文字：IN/OUT 為 true／false（與 Tableau 相同 In 在前），整數照寫
            raw_text = {"In": "true", "Out": "false"}
            raw = [
                [v if isinstance(v, float) else raw_text.get(str(v), str(v)) for v in r]
                for r in expected
            ]
            raw.sort(key=lambda r: (r[0] != "true", str(r[0])))
            same_as_oracle(latest[worksheet], (_, raw))
        assert errors == []
        browser.close()


def test_set_action(server: str) -> None:
    """集合動作：點選 Profitable Sub-Categories 的子類別時，Key Sub-Categories 換成選取的子類別，
    用到這個集合的工作表隨之改變；再點一次清除選取時加入所有值（show-all）。"""
    from playwright.sync_api import sync_playwright

    oracle = load_oracle()
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "集合")
    targets = {"ws_key_sub_categories_by_category", "ws_key_sales_by_region"}
    renders = Renders()
    rows = oracle.load_rows()
    default = oracle.Case("default", {})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(300)

        def click(sub_category: str) -> dict[str, tuple[Any, Any]]:
            start = len(renders.items)
            page.locator(
                "[data-worksheet='ws_profitable_sub_categories'] "
                f"svg path[aria-roledescription='bar'][aria-label*='Sub-Category: {sub_category}']"
            ).click()
            renders.wait_for(page, targets, start)
            page.wait_for_timeout(300)
            return {w: (body, data) for w, body, data in renders.items[start:]}

        def check(latest: dict[str, tuple[Any, Any]], members: set[str] | None) -> None:
            for worksheet, compute in [
                ("ws_key_sub_categories_by_category", oracle.key_sub_categories),
                ("ws_key_sales_by_region", oracle.key_sales_by_region),
            ]:
                _, expected = compute(rows, default, members)
                same_as_oracle(latest[worksheet][1], (_, expected))

        latest = click("Tables")
        for worksheet in targets:
            body = latest[worksheet][0]
            assert body["sets"] == {
                "f_key_sub_categories": {"mode": "include", "members": [["Tables"]]}
            }
        check(latest, {"Tables"})
        # 再點一次＝清除選取：加入所有值
        latest = click("Tables")
        assert latest["ws_key_sales_by_region"][0]["sets"] == {
            "f_key_sub_categories": {"mode": "exclude", "members": []}
        }
        check(latest, None)
        assert errors == []
        browser.close()


def test_condition_filters_render(server: str) -> None:
    """K4：相對日期與條件篩選的工作表畫得出來，篩選控制項顯示目前的設定。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "篩選條件")
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(300)
        assert page.locator(".placeholder.error, .placeholder.unsupported").count() == 0
        rows = {w: data["rows"] for w, _, data in renders.items}
        assert rows["ws_recent_sales_by_category"] == 3
        assert rows["ws_top_sub_categories"] == 3
        assert rows["ws_son_customers_by_region"] >= 1
        relative = page.locator("[data-zone='z14'] .fixed-filter").inner_text()
        assert relative == "最近 3 個月"
        condition = page.locator("[data-zone='z15'] .fixed-filter").inner_text()
        assert "CONTAINS" in condition
        # Top N 篩選的控制項：勾選 Phones 改變排名前的選取（存檔時排除 Phones），仍取前 3 名
        before = next(d for w, _, d in renders.items if w == "ws_top_sub_categories")
        assert "Phones" not in [row[0] for row in before["table"]["raw"]]
        start = len(renders.items)
        control = page.locator("[data-zone='z17']")
        control.locator("button.dropdown").click()
        control.get_by_label("Phones").click()
        renders.wait_for(page, {"ws_top_sub_categories"}, start)
        page.wait_for_timeout(300)
        body, data = next(
            (b, d) for w, b, d in reversed(renders.items[start:]) if w == "ws_top_sub_categories"
        )
        assert data["rows"] == 3
        assert "Phones" in [row[0] for row in data["table"]["raw"]]
        check_numbers(ir, "ws_top_sub_categories", body, data, approx=True)
        assert errors == []
        browser.close()


def test_url_parameter_and_navigate_actions(server: str) -> None:
    """L2：網址動作在選單中以標記的值代入網址；參數動作設定參數；切換動作切到另一個 dashboard。"""
    from playwright.sync_api import sync_playwright

    oracle = load_oracle()
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    filters = next(d for d in ir.dashboards if d.name == "篩選動作")
    params = next(d for d in ir.dashboards if d.name == "參數動作")
    renders = Renders()
    bar = "svg path[aria-roledescription='bar']"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)

        # 網址動作（選單）：點選 East 後選單中的連結
        page.goto(f"{server}/w/superstore_features/d/{filters.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.locator(
            f"[data-worksheet='ws_region_picker'] {bar}[aria-label$='Region: East']"
        ).click()
        link = page.locator(".action-menu a[role='menuitem']")
        link.wait_for()
        href = link.get_attribute("href") or ""
        assert href.startswith("https://example.com/search?region=East&sales=678781.24")
        assert link.inner_text() == "Search Region"

        # 參數動作：點選 West 把 Selected Region 設成 West
        page.goto(f"{server}/w/superstore_features/d/{params.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 2"
        )
        start = len(renders.items)
        page.locator(
            f"[data-worksheet='ws_region_picker'] {bar}[aria-label$='Region: West']"
        ).click()
        renders.wait_for(page, {"ws_selected_region_sales"}, start)
        page.wait_for_timeout(300)
        body, data = next(
            (b, d) for w, b, d in reversed(renders.items) if w == "ws_selected_region_sales"
        )
        assert body["parameters"] == {"p_selected_region": "West"}
        same_as_oracle(data, oracle.parameter_action("West"))
        control = page.locator("[data-zone='z9'] select")
        assert control.input_value() == '"West"'
        # 參數的「顯示為」：選項顯示別名，值仍是原始值
        assert control.locator("option:checked").inner_text() == "西區"
        assert control.locator("option").all_inner_texts() == ["中區", "東區", "南區", "西區"]

        # 切換動作（選單）：點選 Selected Region Sales 的標記，選單中切到「篩選動作」
        page.locator(f"[data-worksheet='ws_selected_region_sales'] {bar}").first.click()
        page.locator(
            ".action-menu button[role='menuitem']", has_text="Go to Filter Actions"
        ).click()
        page.wait_for_url(f"**/d/{filters.id}*")
        assert errors == []
        browser.close()


def test_reference_lines_render(server: str) -> None:
    """J4：參考線畫在圖上（窗格的平均為每個標記上的短線，整張表的線橫跨全圖），只有一條時顯示標籤。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "參考線")
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 2"
        )
        page.wait_for_timeout(300)
        assert page.locator(".placeholder.error, .placeholder.unsupported").count() == 0
        specs = {w: data["spec"] for w, _, data in renders.items}

        def texts(worksheet: str) -> list[str]:
            return page.eval_on_selector_all(
                f"[data-worksheet='{worksheet}'] svg text", "els => els.map(e => e.textContent)"
            )

        def count(worksheet: str, role: str) -> int:
            return page.locator(
                f"[data-worksheet='{worksheet}'] svg [aria-roledescription='{role}']"
            ).count()

        assert "目標 100,000" in texts("ws_sub_category_sales")
        assert count("ws_sub_category_sales", "tick") == 17  # 每個子類別一段
        median = specs["ws_region_sales_bands"]["data"]["values"][0]["__ref0_0"]
        assert f"{median:,.0f}" in texts("ws_region_sales_bands")
        # 中位數的線；平均 ± 1 個標準差的分布與 Tableau 的預設相同只塗底色、不畫線
        assert count("ws_region_sales_bands", "rule mark") == 1
        assert count("ws_region_sales_bands", "rect mark") == 1
        assert errors == []
        browser.close()


def test_dashboard_objects(server: str) -> None:
    """I2：圖片、顯示／隱藏篩選欄的按鈕、匯出 PDF（瀏覽器列印）、切換到 dashboard 與工作表的按鈕、網頁區塊。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    home = next(d for d in ir.dashboards if d.name == "首頁")
    target = next(d for d in ir.dashboards if d.name == "篩選動作")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{server}/w/superstore_features/d/{home.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 1"
        )
        assert page.locator(".placeholder.error, .placeholder.unsupported").count() == 0
        # 圖片區塊與圖片按鈕都從 .twbx 取得
        page.wait_for_function(
            "[...document.querySelectorAll('.zone img')].every(i => i.complete && i.naturalWidth > 0)"
        )
        assert page.locator(".zone img").count() == 3  # 標誌、工作表按鈕、顯示／隱藏按鈕
        assert (
            page.locator("[data-zone='z13'] iframe").get_attribute("src") == "https://example.com/"
        )

        # 篩選欄預設隱藏；按鈕切換顯示與隱藏，圖片隨之改變
        toggle = page.locator("[data-zone='z9'] button")
        assert page.locator("[data-zone='z12']").count() == 0
        assert toggle.get_attribute("data-shown") == "false"
        assert (toggle.locator("img").get_attribute("src") or "").endswith("show-filters.png")
        toggle.click()
        page.locator("[data-zone='z12'] .control").wait_for()
        assert toggle.get_attribute("data-shown") == "true"
        assert (toggle.locator("img").get_attribute("src") or "").endswith("hide-filters.png")
        toggle.click()
        page.wait_for_function("!document.querySelector(\"[data-zone='z12']\")")

        # 匯出 PDF：以瀏覽器列印
        page.evaluate("window.print = () => { window.__printed = true; }")
        page.locator("[data-zone='z8'] button").click()
        assert page.evaluate("window.__printed") is True

        # 圖片按鈕切換到工作表，文字按鈕切換到 dashboard
        page.locator("[data-zone='z7'] button").click()
        page.wait_for_url("**/s/ws_region_sales_bands**")
        page.go_back()
        page.locator("[data-zone='z6'] button").click()
        page.wait_for_url(f"**/d/{target.id}**")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        assert errors == []
        browser.close()


def test_donut_and_mixed_detail(server: str) -> None:
    """J3：甜甜圈中間標示合計；長條（各地區）與依客群分色的折線疊成雙軸，各自以自己的詳細程度查詢。"""
    from playwright.sync_api import sync_playwright

    oracle = load_oracle()
    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "甜甜圈")
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 2"
        )
        page.wait_for_timeout(300)
        assert page.locator(".placeholder.error, .placeholder.unsupported").count() == 0
        latest = {w: data for w, _, data in renders.items}
        assert latest["ws_category_donut"]["rows"] == 3  # 資料表以所有維度的組合（各類別）為準
        assert latest["ws_region_sales_and_segment_profit"]["rows"] == 12

        def texts(worksheet: str) -> list[str]:
            return page.eval_on_selector_all(
                f"[data-worksheet='{worksheet}'] svg text", "els => els.map(e => e.textContent)"
            )

        _, body = oracle.category_donut(oracle.load_rows(), oracle.Case("default", {}))
        total = sum(row[2] for row in body)
        assert f"{total:,.0f}" in texts("ws_category_donut")
        bars = page.locator(
            "[data-worksheet='ws_region_sales_and_segment_profit'] svg [aria-roledescription='bar']"
        )
        assert bars.count() == 4  # 各地區一根，不因折線的客群而拆開
        assert errors == []
        browser.close()


def test_story(server: str) -> None:
    """Story：開啟時停在第一個故事點（預設狀態）；「東區」以記下的選取執行兩個篩選動作、來源工作表顯示選取；
    「個人客戶」顯示 Sales by Segment 只看 Consumer（擷取後改過、沒有更新故事點的狀態）。"""
    from playwright.sync_api import sync_playwright

    oracle = load_oracle()
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    story = next(s for s in ir.stories if s.name == "銷售故事")
    targets = {"ws_category_detail", "ws_segment_detail"}
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 1100})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/story/{story.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(300)
        assert page.locator(".story-title").inner_text() == "銷售故事：從全部地區看到東區"
        points = page.locator(".story-points button")
        assert points.all_inner_texts() == ["全部地區", "東區", "個人客戶"]
        assert points.nth(0).get_attribute("aria-current") == "step"
        first = {w: data for w, _, data in renders.items}
        same_as_oracle(first["ws_category_detail"], oracle.selection("ws_category_detail", None))
        assert first["ws_segment_detail"]["rows"] == 0
        picker = page.locator("[data-worksheet='ws_region_picker']")
        assert picker.locator(".selection").count() == 0

        def show(caption: str, worksheets: set[str]) -> dict[str, tuple[Any, Any]]:
            start = len(renders.items)
            page.locator(".story-points button", has_text=caption).click()
            renders.wait_for(page, worksheets, start)
            page.wait_for_timeout(300)
            return {w: (body, data) for w, body, data in renders.items[start:]}

        latest = show("東區", targets)
        assert set(latest) == targets  # 篩選沒有改變的工作表不重新查詢
        for worksheet, (body, data) in latest.items():
            action = f"act_filter_{worksheet.removeprefix('ws_')}"
            assert body["actions"] == {action: [{"f_region": "East"}]}
            same_as_oracle(data, oracle.selection(worksheet, ["East"]))
            check_numbers(ir, worksheet, body, data)
        assert picker.locator(".selection").inner_text().startswith("已選取 1 個標記")

        start = len(renders.items)
        page.get_by_role("button", name="下一個故事點").click()
        renders.wait_for(page, {"ws_sales_by_segment"}, start)
        page.wait_for_timeout(300)
        [(body, data)] = [(b, d) for w, b, d in renders.items[start:] if w == "ws_sales_by_segment"]
        assert body["filters"] == {"f_segment": {"mode": "include", "values": ["Consumer"]}}
        consumer = oracle.Case("story", {"Segment": "Consumer"})
        same_as_oracle(data, oracle.sales_by_segment(oracle.load_rows(), consumer))
        assert page.get_by_role("button", name="下一個故事點").is_disabled()

        # 回到第一個故事點：沒有選取，Category Detail 顯示全部、Segment Detail 不顯示任何值
        latest = show("全部地區", targets)
        assert all(body["actions"] == {} for body, _ in latest.values())
        same_as_oracle(
            latest["ws_category_detail"][1], oracle.selection("ws_category_detail", None)
        )
        assert latest["ws_segment_detail"][1]["rows"] == 0
        assert page.locator("[data-worksheet='ws_region_picker'] .selection").count() == 0
        assert page.get_by_role("button", name="上一個故事點").is_disabled()
        assert errors == []
        browser.close()


def test_tooltip_commands_keep_only_and_select_by_category(server: str) -> None:
    """工具提示的命令按鈕與依類別選取：點選標記後工具提示釘住，可以「只保留」「排除」（以標記的維度值
    篩選這張工作表），點工具提示中的維度值選取這個值的所有標記；工作表上方可以清除。"""
    from playwright.sync_api import expect, sync_playwright

    renders = Renders()
    sheet = "ws_sub_category_sales"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/s/{sheet}")
        renders.wait_for(page, {sheet}, 0)
        page.wait_for_selector(f".worksheet[data-worksheet='{sheet}'][data-loading='false']")
        total = renders.items[-1][2]["rows"]
        # 捲動時固定的標題各有一份圖的複本：只看主要的那一份
        bars = page.locator(
            f"[data-worksheet='{sheet}'] .frozen-host > svg path[aria-roledescription='bar']"
        )
        expect(bars).to_have_count(total)
        pinned = page.locator(".viz-tooltip.pinned")

        # 點選一個標記：工具提示釘住，有命令按鈕；按 Esc 放開
        chairs = bars.and_(page.locator("[aria-label*='Sub-Category: Chairs']"))
        chairs.hover()
        page.locator(".viz-tooltip").wait_for()
        chairs.click()
        pinned.wait_for()
        expect(pinned.get_by_role("button", name="✓ 只保留")).to_be_visible()
        page.keyboard.press("Escape")
        expect(pinned).to_have_count(0)

        # 依類別選取：點工具提示中的 Category 值，選取這個類別的所有標記（4 個）
        chairs.hover()
        chairs.click()
        pinned.wait_for()
        pinned.locator(".viz-tooltip-category", has_text="Furniture").click()
        expect(pinned.locator(".viz-tooltip-commands")).to_contain_text("4 個標記")

        # 只保留：這張工作表只剩這些標記
        start = len(renders.items)
        pinned.get_by_role("button", name="✓ 只保留").click()
        renders.wait_for(page, {sheet}, start)
        _, body, data = renders.items[-1]
        assert [m["mode"] for m in body["marks"]] == ["keep"]
        assert {m["f_category"] for m in body["marks"][0]["marks"]} == {"Furniture"}
        assert data["rows"] == 4
        expect(bars).to_have_count(4)
        expect(pinned).to_have_count(0)

        # 再排除其中一個
        tables = bars.and_(page.locator("[aria-label*='Sub-Category: Tables']"))
        tables.hover()
        tables.click()
        pinned.wait_for()
        start = len(renders.items)
        pinned.get_by_role("button", name="✕ 排除").click()
        renders.wait_for(page, {sheet}, start)
        _, body, data = renders.items[-1]
        assert [m["mode"] for m in body["marks"]] == ["keep", "exclude"]
        assert data["rows"] == 3
        expect(bars).to_have_count(3)

        # 清除只保留與排除
        start = len(renders.items)
        page.locator(f"[data-worksheet='{sheet}'] .worksheet-meta .kept").click()
        renders.wait_for(page, {sheet}, start)
        assert "marks" not in renders.items[-1][1] and renders.items[-1][2]["rows"] == total
        assert errors == []
        browser.close()


def test_tap_pins_the_same_tooltip_as_hover(server: str) -> None:
    """觸控的裝置上點標記（點之前沒有滑過）：釘住的工具提示與桌面上滑過同一個標記時的相同——欄位的名稱、
    格式化的值、工作表自訂的工具提示文字（含格式），單張工作表與 dashboard 上都一樣。

    原本釘住的內容取自「上一次滑過時留下的工具提示」，觸控時沒有滑過，於是列出標記的內部欄位鍵
    （「f_category　Furniture」），也沒有量值（Chromium 的手機模擬上量到的）。"""
    from playwright.sync_api import Locator, expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    bars = "svg path[aria-roledescription='bar']:not(.frozen *)"
    # 頁面、工作表、標記、工具提示裡要有的文字：一般的工具提示（各欄位一列）、自訂文字的工具提示、
    # dashboard 上有篩選動作的工作表
    cases = [
        (
            "s/ws_sub_category_sales",
            "ws_sub_category_sales",
            "[aria-label*='Furniture｜Chairs']",
            ["Category Furniture", "Sub-Category Chairs", "Sales 328,449"],
        ),
        (
            "s/ws_sales_by_segment",
            "ws_sales_by_segment",
            "[aria-label$='Segment: Consumer']",
            ["Segment: Consumer"],
        ),
        (
            f"d/{board.id}",
            "ws_region_picker",
            "[aria-label$='Region: East']",
            ["Region East", "Sales 678,781"],
        ),
    ]

    def lines(tooltip: Locator) -> str:
        """工具提示的文字（不含命令按鈕與工具提示中的工作表），空白合併成一個。"""
        return " ".join(
            tooltip.locator(".viz-tooltip-text, .viz-tooltip-rich").inner_text().split()
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        desktop = browser.new_page(viewport={"width": 1400, "height": 1000})
        phone = browser.new_context(**p.devices["Pixel 5"]).new_page()
        errors: list[str] = []
        for page in (desktop, phone):
            page.on("pageerror", lambda exc: errors.append(str(exc)))
        for path, sheet, mark, expected in cases:
            shown: list[str] = []
            for page in (desktop, phone):
                page.goto(f"{server}/w/superstore_features/{path}")
                page.wait_for_selector(
                    f".worksheet[data-worksheet='{sheet}'][data-loading='false']"
                )
                page.wait_for_function(LOADED)
                target = page.locator(f"[data-worksheet='{sheet}'] {bars}{mark}")
                if page is desktop:
                    target.hover()
                    tooltip = page.locator(".viz-tooltip")
                else:
                    # 觸控：直接點，沒有滑過。工具提示釘住，上面有命令按鈕
                    target.tap()
                    tooltip = page.locator(".viz-tooltip.pinned")
                    expect(tooltip.get_by_role("button", name="✓ 只保留")).to_be_visible()
                expect(tooltip).to_have_count(1)
                shown.append(lines(tooltip))
                if sheet == "ws_sales_by_segment":
                    # 自訂的工具提示文字連同格式：值是粗體
                    value = tooltip.locator(".viz-tooltip-rich span", has_text="Consumer")
                    expect(value).to_have_css("font-weight", "700")
            hovered, tapped = shown
            assert tapped == hovered, (path, shown)
            assert all(text in tapped for text in expected), (path, tapped)
            assert "f_" not in tapped, (path, tapped)  # 內部的欄位鍵
        assert errors == []
        browser.close()


LOADED = "document.querySelectorAll('.worksheet[data-loading=\"true\"]').length === 0"
ALL_FOUR = "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"


def test_multi_dropdown_floats_above_the_dashboard_and_closes(server: str) -> None:
    """多選下拉的選單浮在 dashboard 上面，不被篩選區塊的高度裁切（稽核 B2：17 個值只露出 37 像素）；
    點別的地方或按 Esc 收起來（B3），勾選時不收。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "篩選條件")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_selector(".worksheet")
        page.wait_for_function(LOADED)
        control = page.locator("fieldset.control", has_text="Sub-Category").first
        button = control.locator("button.dropdown")
        options = control.locator(".options")
        expect(options).to_have_count(0)
        button.click()
        control.get_by_label("Phones").wait_for()
        box = options.evaluate(
            """e => {
              const r = e.getBoundingClientRect();
              const zone = e.closest('.zone-box').getBoundingClientRect();
              const b = e.parentElement.querySelector('button.dropdown').getBoundingClientRect();
              // 選單下緣那一點最上層的元素是不是選單自己（被區塊裁切時不是）
              const top = document.elementFromPoint(r.left + r.width / 2, r.bottom - 8);
              return { top: r.top, bottom: r.bottom, height: r.height, zone: zone.bottom,
                       button: b.bottom, left: r.left, buttonLeft: b.left, onTop: e.contains(top) };
            }"""
        )
        # 選單在按鈕正下方、超出篩選區塊，而且看得到（17 個值加搜尋框：高度遠超過區塊剩下的 40 像素）
        assert box["top"] >= box["button"] and box["top"] - box["button"] < 4
        assert abs(box["left"] - box["buttonLeft"]) < 1
        assert box["bottom"] > box["zone"] + 100 and box["height"] >= 200
        assert box["onTop"] is True
        # 勾選：選單不收；點 dashboard 的其他地方：收起來
        control.get_by_label("Phones").click()
        page.wait_for_function(LOADED)
        expect(options).to_be_visible()
        page.locator(".worksheet-title").first.click()
        expect(options).to_have_count(0)
        # 再開啟，焦點在別處時按 Esc：收起來
        button.click()
        expect(options).to_be_visible()
        page.locator("body").press("Escape")
        expect(options).to_have_count(0)
        # 按鈕本身仍然可以開關
        button.click()
        expect(options).to_be_visible()
        button.click()
        expect(options).to_have_count(0)
        assert errors == []
        browser.close()


def test_drag_selects_the_marks_in_the_rectangle(server: str) -> None:
    """框選（稽核 B24）：在圖上拖曳出方框，方框碰到的標記都選取（與點選相同，觸發篩選動作），不會把座標軸的
    文字反白；按住 Ctrl 加選；框到空白處取消選取；框選之後點選照常。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    targets = {"ws_category_detail", "ws_segment_detail"}
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(ALL_FOUR)
        page.wait_for_timeout(300)
        picker = page.locator("[data-worksheet='ws_region_picker']")
        selector = "svg path[aria-roledescription='bar']"

        def bar(region: str) -> dict[str, float]:
            found = picker.locator(f"{selector}[aria-label$='Region: {region}']").bounding_box()
            assert found is not None
            return {
                "x": found["x"],
                "y": found["y"],
                "width": found["width"],
                "height": found["height"],
            }

        def drag(start: tuple[float, float], end: tuple[float, float]) -> None:
            page.mouse.move(*start)
            page.mouse.down()
            page.mouse.move((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, steps=4)
            page.mouse.move(*end, steps=4)

        def selected(count: int) -> dict[str, tuple[Any, Any]]:
            start = len(renders.items)
            page.mouse.up()
            renders.wait_for(page, targets, start)
            page.wait_for_timeout(300)
            expect(picker.locator(".selection")).to_contain_text(f"已選取 {count} 個標記")
            return {w: (body, data) for w, body, data in renders.items[start:]}

        central, east, south, west = (bar(r) for r in ("Central", "East", "South", "West"))
        # 從 East 的長條中間拖到 South 的長條中間：兩個標記都碰到方框（Central、West 沒有）
        drag(
            (east["x"] + 6, east["y"] + east["height"] / 2),
            (south["x"] + 40, south["y"] + south["height"] / 2),
        )
        # 拖曳中：畫出方框，沒有選到任何文字，也不顯示滑過標記的工具提示
        expect(picker.locator(".select-overlay.passive rect")).to_have_count(1)
        assert page.evaluate("String(window.getSelection())") == ""
        assert page.evaluate("document.body.classList.contains('box-selecting')") is True
        latest = selected(2)
        for body, _ in latest.values():
            assert list(body["actions"].values()) == [[{"f_region": "East"}, {"f_region": "South"}]]
        assert latest["ws_segment_detail"][1]["rows"] > 0
        expect(picker.locator(".select-overlay")).to_have_count(0)
        assert page.evaluate("document.body.classList.contains('box-selecting')") is False
        assert page.evaluate("String(window.getSelection())") == ""

        # 按住 Ctrl 框選：加進原本的選取
        page.keyboard.down("Control")
        drag((west["x"] + 6, west["y"] + 4), (west["x"] + 60, west["y"] + west["height"] - 4))
        latest = selected(3)
        page.keyboard.up("Control")
        assert list(latest["ws_category_detail"][0]["actions"].values()) == [
            [{"f_region": "East"}, {"f_region": "South"}, {"f_region": "West"}]
        ]

        # 框到沒有標記的地方（最短的長條右邊的空白）：取消選取
        start = len(renders.items)
        drag(
            (south["x"] + south["width"] + 60, south["y"] + 6),
            (south["x"] + south["width"] + 90, south["y"] + south["height"] - 6),
        )
        page.mouse.up()
        renders.wait_for(page, targets, start)
        page.wait_for_timeout(300)
        expect(picker.locator(".selection")).to_have_count(0)
        assert renders.items[-1][1].get("actions", {}) == {}

        # 框選之後點選照常（滑鼠只動了一兩個像素也算點選）
        start = len(renders.items)
        central = bar("Central")
        page.mouse.move(central["x"] + 20, central["y"] + central["height"] / 2)
        page.mouse.down()
        page.mouse.move(central["x"] + 21, central["y"] + central["height"] / 2 + 1)
        page.mouse.up()
        renders.wait_for(page, targets, start)
        expect(picker.locator(".selection")).to_contain_text("已選取 1 個標記")
        assert list(renders.items[-1][1]["actions"].values()) == [[{"f_region": "Central"}]]
        assert errors == []
        browser.close()


def test_escape_clears_the_selection(server: str) -> None:
    """按 Esc 取消選取（Tableau：Esc 取消選取的標記；原本 Esc 只收起釘住的工具提示，選取與篩選動作都還在）：
    點選、框選的選取都清除，篩選動作跟著清除。dashboard 上兩張工作表都有選取時，一次取消最近選取的那一張。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    targets = {"ws_category_detail", "ws_segment_detail"}
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(ALL_FOUR)
        page.wait_for_timeout(300)
        picker = page.locator("[data-worksheet='ws_region_picker']")
        bars = "svg path[aria-roledescription='bar']:not(.frozen *)"

        # 點選一個標記（篩選動作篩選兩張目標工作表）→ Esc：選取與篩選動作都清除
        start = len(renders.items)
        picker.locator(f"{bars}[aria-label$='Region: East']").click()
        renders.wait_for(page, targets, start)
        expect(picker.locator(".selection")).to_contain_text("已選取 1 個標記")
        # 兩張目標工作表各有自己的篩選動作（回應的先後不一定）
        for _, body, _ in renders.items[start:]:
            assert list(body["actions"].values()) == [[{"f_region": "East"}]]
        start = len(renders.items)
        page.keyboard.press("Escape")
        renders.wait_for(page, targets, start)
        expect(picker.locator(".selection")).to_have_count(0)
        expect(page.locator(".viz-tooltip.pinned")).to_have_count(0)
        assert all(body.get("actions", {}) == {} for _, body, _ in renders.items[start:])

        # 框選兩個標記 → Esc
        east = picker.locator(f"{bars}[aria-label$='Region: East']").bounding_box()
        south = picker.locator(f"{bars}[aria-label$='Region: South']").bounding_box()
        assert east is not None and south is not None
        start = len(renders.items)
        page.mouse.move(east["x"] + 6, east["y"] + east["height"] / 2)
        page.mouse.down()
        page.mouse.move(south["x"] + 40, south["y"] + south["height"] / 2, steps=8)
        page.mouse.up()
        renders.wait_for(page, targets, start)
        expect(picker.locator(".selection")).to_contain_text("已選取 2 個標記")
        start = len(renders.items)
        page.keyboard.press("Escape")
        renders.wait_for(page, targets, start)
        expect(picker.locator(".selection")).to_have_count(0)
        assert all(body.get("actions", {}) == {} for _, body, _ in renders.items[start:])
        # 沒有選取時按 Esc：什麼都不做
        start = len(renders.items)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        assert len(renders.items) == start

        # 兩張工作表都有選取（Region Picker 的選取、Category Detail 上釘住的工具提示）：
        # 第一次 Esc 取消最近的那一張，第二次才是前一張
        start = len(renders.items)
        picker.locator(f"{bars}[aria-label$='Region: West']").click()
        renders.wait_for(page, targets, start)
        expect(picker.locator(".selection")).to_contain_text("已選取 1 個標記")
        page.wait_for_function(ALL_FOUR)
        page.wait_for_timeout(300)
        detail = page.locator("[data-worksheet='ws_category_detail']")
        detail.locator(bars).first.click()
        pinned = page.locator(".viz-tooltip.pinned")
        expect(pinned).to_have_count(1)
        expect(picker.locator(".selection")).to_contain_text("已選取 1 個標記")
        start = len(renders.items)
        page.keyboard.press("Escape")
        expect(pinned).to_have_count(0)
        page.wait_for_timeout(500)
        expect(picker.locator(".selection")).to_contain_text("已選取 1 個標記")
        assert len(renders.items) == start
        page.keyboard.press("Escape")
        renders.wait_for(page, targets, start)
        expect(picker.locator(".selection")).to_have_count(0)
        assert errors == []
        browser.close()


def test_escape_in_presentation_mode_only_leaves_presentation(server: str) -> None:
    """簡報模式中按一次 Esc 只做一件事：離開簡報模式（Tableau 的使用經驗；官方文件沒有寫 Esc，哪個優先是
    推定的，見 README「已知限制與待驗證」），標記的選取留著；離開之後再按一次才取消選取。

    原本按鍵送到頁面上的時候（沒有全螢幕的環境——桌面版的外殼、不允許全螢幕的瀏覽器）同一下 Esc 既離開簡報
    模式又取消選取，篩選動作跟著清除、目標工作表重新查詢。開著選單時 Esc 是選單的：收起選單，簡報模式不結束。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    filters = next(d for d in ir.dashboards if d.name == "篩選條件")
    targets = {"ws_category_detail", "ws_segment_detail"}
    no_fullscreen = (
        "Element.prototype.requestFullscreen = () => Promise.reject(new Error('不允許全螢幕'));"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        errors: list[str] = []
        # 不允許全螢幕，以及無頭的 Chromium 自己的全螢幕（按鍵一樣送到頁面上）
        for fullscreen in (False, True):
            page = browser.new_page(viewport={"width": 1300, "height": 900})
            if not fullscreen:
                page.add_init_script(no_fullscreen)
            renders = Renders()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("response", renders)
            page.goto(f"{server}/w/superstore_features/d/{board.id}")
            page.wait_for_function(ALL_FOUR)
            page.wait_for_timeout(300)
            picker = page.locator("[data-worksheet='ws_region_picker']")
            start = len(renders.items)
            picker.locator(
                "svg path[aria-roledescription='bar']:not(.frozen *)[aria-label$='Region: East']"
            ).click()
            renders.wait_for(page, targets, start)
            page.wait_for_function(ALL_FOUR)
            expect(picker.locator(".selection")).to_contain_text("已選取 1 個標記")

            page.get_by_role("button", name="簡報模式").click()
            presenting = page.locator(".page.presenting")
            expect(presenting).to_have_count(1)
            if not fullscreen:
                assert page.evaluate("document.fullscreenElement === null")
            # 第一次 Esc：離開簡報模式，選取與篩選動作都還在（沒有重新查詢）
            start = len(renders.items)
            page.keyboard.press("Escape")
            expect(presenting).to_have_count(0)
            page.wait_for_timeout(500)
            expect(picker.locator(".selection")).to_contain_text("已選取 1 個標記")
            assert len(renders.items) == start, fullscreen
            # 第二次 Esc：取消選取
            page.keyboard.press("Escape")
            renders.wait_for(page, targets, start)
            expect(picker.locator(".selection")).to_have_count(0)
            page.close()

        # 簡報模式中開著多選下拉的選單：Esc 收起選單、簡報模式不結束；再按一次才離開
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.add_init_script(no_fullscreen)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{server}/w/superstore_features/d/{filters.id}")
        page.wait_for_selector(".worksheet")
        page.wait_for_function(LOADED)
        page.get_by_role("button", name="簡報模式").click()
        presenting = page.locator(".page.presenting")
        expect(presenting).to_have_count(1)
        control = page.locator("fieldset.control", has_text="Sub-Category").first
        control.locator("button.dropdown").click()
        options = control.locator(".options")
        expect(options).to_be_visible()
        page.locator("body").press("Escape")
        expect(options).to_have_count(0)
        page.wait_for_timeout(300)
        expect(presenting).to_have_count(1)
        page.locator("body").press("Escape")
        expect(presenting).to_have_count(0)
        assert errors == []
        browser.close()


def test_presentation_mode_shortcuts(server: str) -> None:
    """簡報模式的快捷鍵（Tableau 的快捷鍵表：「Switch in and out of Presentation Mode」是 F7、Ctrl+H）：在檢視的
    頁面上按 F7 或 Ctrl+H 進入簡報模式，再按一次離開；Esc 照舊離開。按鍵標成已處理（瀏覽器自己的 F7、Ctrl+H
    不跟著出現）。正在輸入文字、開著對話框時不處理——那一下按鍵是輸入框、對話框的。原本這兩個鍵沒有作用。

    測試送出的按鍵不經過瀏覽器自己的快捷鍵處理：瀏覽器的視窗裡這兩個鍵會不會先被瀏覽器拿走，這裡測不到
    （README「已知限制與待驗證」）。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "醒目提示")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        # 記下每一下按鍵最後有沒有被標成已處理
        page.add_init_script(
            "window.__keys = []; window.addEventListener('keydown', (e) => e.key !== 'Control' &&"
            " window.__keys.push((e.ctrlKey ? 'Ctrl+' : '') + e.key + ':' + e.defaultPrevented));"
        )
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_selector(".worksheet")
        page.wait_for_function(LOADED)
        presenting = page.locator(".page.presenting")

        # F7、Ctrl+H：進入，再按一次離開；可以交替使用，Esc 也離開
        for enter, leave in (
            ("F7", "F7"),
            ("Control+h", "Control+h"),
            ("F7", "Control+h"),
            ("Control+h", "Escape"),
        ):
            expect(presenting).to_have_count(0)
            page.keyboard.press(enter)
            expect(presenting).to_have_count(1)
            expect(page.locator(".presentation-bar")).to_be_visible()
            page.keyboard.press(leave)
            expect(presenting).to_have_count(0)
            expect(page.locator(".page > .toolbar")).to_be_visible()
        handled = page.evaluate("window.__keys")
        assert handled.count("F7:true") == 3 and handled.count("Ctrl+h:true") == 4, handled
        assert not [key for key in handled if key.endswith(":false") and "Escape" not in key], (
            handled
        )

        # 正在輸入文字（醒目提示控制項的搜尋框）：不處理，按鍵留給輸入框
        search = page.get_by_label("醒目提示 Category")
        search.click()
        search.press("F7")
        search.press("Control+h")
        page.wait_for_timeout(300)
        expect(presenting).to_have_count(0)
        assert page.evaluate("window.__keys.slice(-2)") == ["F7:false", "Ctrl+h:false"]
        # 焦點在不是輸入文字的控制項上（切換檢視的下拉選單）：照常
        page.get_by_label("切換檢視").focus()
        page.keyboard.press("F7")
        expect(presenting).to_have_count(1)
        page.keyboard.press("F7")
        expect(presenting).to_have_count(0)

        # 開著對話框（下載交叉資料表）：不處理
        page.get_by_label("匯出").select_option(label="交叉資料表…")
        expect(page.locator(".modal-backdrop")).to_have_count(1)
        page.locator(".modal-backdrop .modal header h2").click()
        page.keyboard.press("F7")
        page.keyboard.press("Control+h")
        page.wait_for_timeout(300)
        expect(presenting).to_have_count(0)
        expect(page.locator(".modal-backdrop")).to_have_count(1)
        assert errors == []
        browser.close()


def test_presentation_mode_survives_rapid_toggling(server: str) -> None:
    """全螢幕的要求是非同步的，進入、離開簡報模式可能比它快：連按 F7（或 Ctrl+H、進入之後馬上按 Esc），最後要
    不在簡報模式、也不留在全螢幕；離開之後馬上再進入，上一次結束全螢幕的通知不能把這一次踢出去；真的進入全螢幕
    之後，全螢幕被結束（使用者按 Esc，瀏覽器自己收下）照樣離開簡報模式。

    原本（第五批的第一版）連按時畫面留在全螢幕、卻不是簡報模式（無頭的 Chromium 上 6 次中 3–6 次）；全螢幕的要求
    還沒有結果時收到的「不在全螢幕」一律不理會，於是 `test_presentation_mode_shortcuts` 在電腦忙的時候偶爾停在
    簡報模式裡。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "醒目提示")
    state = (
        "() => [document.querySelectorAll('.page.presenting').length, !!document.fullscreenElement]"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_selector(".worksheet")
        page.wait_for_function(LOADED)
        presenting = page.locator(".page.presenting")

        # 進入之後馬上離開（不等全螢幕）：不在簡報模式，也不留在全螢幕
        for enter, leave in (("F7", "F7"), ("Control+h", "Control+h"), ("F7", "Escape")):
            for _ in range(3):
                page.keyboard.press(enter)
                page.keyboard.press(leave)
                page.wait_for_timeout(800)
                assert page.evaluate(state) == [0, False], (enter, leave)

        # 進入、等全螢幕；離開之後馬上再進入：上一次結束全螢幕的通知不把這一次踢出去
        for _ in range(3):
            page.keyboard.press("F7")
            page.wait_for_function("document.fullscreenElement !== null")
            page.keyboard.press("F7")
            page.keyboard.press("F7")
            page.wait_for_timeout(800)
            assert page.evaluate(state) == [1, True]
            page.keyboard.press("F7")
            page.wait_for_function("document.fullscreenElement === null")
            expect(presenting).to_have_count(0)

        # 真的進入全螢幕之後，全螢幕在頁面之外結束（使用者按 Esc 時瀏覽器自己收下）：離開簡報模式
        page.keyboard.press("Control+h")
        page.wait_for_function("document.fullscreenElement !== null")
        page.evaluate("document.exitFullscreen()")
        expect(presenting).to_have_count(0)
        assert page.evaluate(state) == [0, False]
        assert errors == []
        browser.close()


def test_loading_title_is_the_sheet_name_and_empty_view_is_blank(server: str) -> None:
    """查詢還沒回來時工作表的標題是工作表的名稱，不是內部的代號（稽核 B35：ws_region_picker）；
    沒有資料的工作表（篩選動作的目標，還沒有選取）是空白的，不畫座標軸與「0.000000」（B33）。"""
    from playwright.sync_api import Route, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    names = {w.id: w.name for w in ir.worksheets}
    sheets = ["ws_region_picker", "ws_category_detail", "ws_segment_detail"]
    sheets.append("ws_region_category_matrix")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        # 先扣住查詢的請求（還在「載入中…」），看完標題再放行
        held: list[Route] = []
        page.route("**/render", lambda route: held.append(route))
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        # 四張都在「載入中…」
        page.wait_for_function("document.querySelectorAll('.worksheet .placeholder').length === 4")
        titles = page.evaluate(
            "Object.fromEntries([...document.querySelectorAll('.worksheet')].map(w =>"
            " [w.dataset.worksheet, w.querySelector('.worksheet-title')?.textContent ?? null]))"
        )
        assert titles == {sheet: names[sheet] for sheet in sheets}
        assert "ws_" not in page.locator(".dashboard").inner_text()
        for route in held:
            route.continue_()
        page.wait_for_function(ALL_FOUR + " && !document.querySelector('[data-drawn=\"false\"]')")
        detail = page.locator("[data-worksheet='ws_segment_detail']")
        assert "0 列" in detail.locator(".worksheet-meta").inner_text()
        assert detail.locator(".chart svg text").count() == 0
        assert detail.locator(".chart svg .role-axis").count() == 0
        # 有資料的工作表照常畫出座標軸
        assert page.locator("[data-worksheet='ws_region_picker'] .chart svg .role-axis").count() > 0
        assert errors == []
        browser.close()


def test_axis_tick_labels_do_not_run_together(server: str) -> None:
    """連續座標軸的刻度文字之間留有空隙（稽核 B32：「90,000100,000」連成一串）。"""
    from playwright.sync_api import sync_playwright

    sheet = "ws_sub_category_sales"
    renders = Renders()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("response", renders)
        page.goto(f"{server}/w/superstore_features/s/{sheet}")
        renders.wait_for(page, {sheet}, 0)
        start = len(renders.items)
        page.get_by_role("button", name="向下切入到 Product Name").click()
        renders.wait_for(page, {sheet}, start)
        page.wait_for_function(
            "document.querySelector('.worksheet').dataset.loading === 'false'"
            " && document.querySelector('.chart').dataset.drawn === 'true'"
        )
        page.wait_for_timeout(500)
        labels = page.evaluate(
            """() => [...document.querySelectorAll('.chart .frozen-host > svg .role-axis-label text')]
              .filter(t => /^[0-9,]+$/.test(t.textContent) && t.__data__.opacity !== 0)
              .map(t => { const r = t.getBoundingClientRect(); return [t.textContent, r.left, r.right]; })
              .sort((a, b) => a[1] - b[1])"""
        )
        assert len(labels) >= 4 and labels[-1][0] == "100,000"
        gaps = [after[1] - before[2] for before, after in itertools.pairwise(labels)]
        assert min(gaps) >= 3, (labels, gaps)
        browser.close()


def test_print_and_capture_show_only_the_view(server: str) -> None:
    """列印（匯出 PDF、訂閱的 PDF）與訂閱擷取的圖片只有檢視本身：不印工具列、每張工作表的列數與「資料」
    切換、切入的連結，頁面的灰色底也不印（稽核 G6、C9）。"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    board = next(d for d in ir.dashboards if d.name == "篩選動作")
    hidden = "items => items.every(e => getComputedStyle(e).display === 'none')"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        page.goto(f"{server}/w/superstore_features/d/{board.id}")
        page.wait_for_function(ALL_FOUR)
        meta = page.locator(".worksheet-meta")
        expect(meta.first).to_be_visible()
        expect(page.locator(".toolbar")).to_be_visible()
        # 訂閱擷取畫面時在 <html> 加上 capture
        page.evaluate("document.documentElement.classList.add('capture')")
        assert meta.evaluate_all(hidden)
        page.evaluate("document.documentElement.classList.remove('capture')")
        expect(meta.first).to_be_visible()
        # 列印
        page.emulate_media(media="print")
        assert meta.evaluate_all(hidden)
        expect(page.locator(".toolbar")).to_be_hidden()
        background = page.evaluate(
            "[getComputedStyle(document.documentElement).backgroundColor,"
            " getComputedStyle(document.body).backgroundColor]"
        )
        assert background == ["rgb(255, 255, 255)", "rgb(255, 255, 255)"]
        expect(page.locator(".dashboard")).to_be_visible()
        browser.close()


BOX = """selector => {
  const element = document.querySelector(selector);
  if (!element) return null;
  const r = element.getBoundingClientRect();
  return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
}"""


def test_phone_sized_viewer_keeps_the_screen_for_the_view(server: str, tmp_path: Path) -> None:
    """手機（390×844，稽核 I6）：原本工具列折成好幾列、占掉大半個畫面，隨畫面調整大小的 dashboard 被壓到
    358×609、圖表只剩 59 像素寬。

    - 工具列收成一列（回報表清單、切換檢視、「更多」），其餘的工具按「更多」才展開
    - 沒有手機版面的 dashboard 以作者設計的尺寸顯示預設版面，在自己的區塊裡左右捲動（工具列不跟著捲走）；
      有手機版面的照樣用手機版面
    - 單張工作表的控制項排在圖的下面；Story 的標題與導覽留在畫面的寬度之內
    - 桌面與平板大小的畫面不變：沒有「更多」，工具全部排出來，dashboard 照樣隨畫面調整大小"""
    from playwright.sync_api import expect, sync_playwright

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    overview = next(d for d in ir.dashboards if d.name == "功能總覽")
    assert overview.sizing == "automatic" and not overview.layouts
    assert (overview.size.width, overview.size.height) == (1366, 768)
    fixed = next(d for d in ir.dashboards if d.name == "圖表擴充")
    assert fixed.sizing == "fixed" and not fixed.layouts
    phone = next(d for d in ir.dashboards if d.name == "篩選動作")
    assert [layout.device for layout in phone.layouts] == ["phone"]
    story = ir.stories[0]
    loaded = "document.querySelectorAll('.worksheet[data-loading=\"true\"]').length === 0"

    # 單張工作表加上控制項的報表（示範報表的複本：Sales by Region 顯示參數的控制項）
    demo = ROOT / "fixtures" / "superstore_demo"
    target = tmp_path / "fixtures" / "controls"
    target.mkdir(parents=True)
    for name in ("superstore_demo.twbx", "cases.yaml"):
        shutil.copy(demo / name, target / name)
    shutil.copytree(demo / "data", target / "data")
    report = load_ir(demo / "golden.ir.json").model_dump(mode="json")
    sheet = next(w for w in report["worksheets"] if w["id"] == "ws_sales_by_region")
    sheet["controls"] = [
        {"kind": "parameter", "parameter": report["parameters"][0]["id"], "control": "type_in"}
    ]
    (target / "golden.ir.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def box(selector: str) -> dict[str, float]:
            found = page.evaluate(BOX, selector)
            assert found is not None, selector
            return found

        def no_page_scroll() -> None:
            # 整頁不左右捲動：工具列留在原位
            widths = page.evaluate(
                "[document.documentElement.scrollWidth, document.documentElement.clientWidth,"
                " document.querySelector('.page').scrollWidth, document.querySelector('.page').clientWidth]"
            )
            assert widths[0] == widths[1] == 390 and widths[2] == widths[3], widths

        # 沒有手機版面、隨畫面調整大小的 dashboard：工具列一列，dashboard 是設計的尺寸
        page.goto(f"{server}/w/superstore_features/d/{overview.id}")
        page.wait_for_selector(".worksheet[data-loading='false']")
        page.wait_for_function(loaded)
        toolbar = box(".toolbar")
        assert toolbar["height"] <= 40 and toolbar["width"] <= 390, toolbar
        more = page.get_by_role("button", name="更多工具")
        expect(more).to_be_visible()
        expect(more).to_have_attribute("aria-expanded", "false")
        expect(page.get_by_label("切換檢視")).to_be_visible()
        expect(page.locator(".toolbar").get_by_role("link", name="報表")).to_be_visible()
        for hidden in (".toolbar .history", ".toolbar .cases", ".toolbar .wb"):
            expect(page.locator(hidden)).to_be_hidden()
        assert page.locator(".dashboard").get_attribute("data-device") == "default"
        board = box(".dashboard")
        # 寬度是設計的 1366（不是畫面的 374），高度不小於設計的 768（畫面比它高時填滿）
        assert board["width"] == 1366 and 768 <= board["height"] <= 844 and board["y"] <= 50, board
        charts = page.locator(".dashboard .worksheet .chart svg").evaluate_all(
            "nodes => nodes.filter(n => !n.closest('.frozen')).map(n => n.getBoundingClientRect().width)"
        )
        assert len(charts) == 3 and min(charts) > 300, charts
        wrapper = page.locator(".dashboard-wrapper")
        assert wrapper.evaluate("e => [e.clientWidth, e.scrollWidth]") == [374, 1366]
        no_page_scroll()
        wrapper.evaluate("e => { e.scrollLeft = 600; }")
        assert wrapper.evaluate("e => e.scrollLeft") == 600
        assert box(".toolbar")["x"] == toolbar["x"] and box(".dashboard")["x"] == board["x"] - 600

        # 「更多」：展開其餘的工具（報表的名稱、復原與重做、匯出、篩選組合），再按一次收起來
        more.click()
        expect(more).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".toolbar .history")).to_be_visible()
        expect(page.locator(".toolbar").get_by_label("匯出")).to_be_visible()
        expect(page.locator(".toolbar .wb")).to_have_text("superstore_features")
        # 第一列不變：「更多」還在切換檢視的右邊、同一列
        assert abs(box(".toolbar-more")["y"] - box(".view-switch")["y"]) < 6
        assert box(".toolbar-more")["x"] >= box(".view-switch")["right"]
        assert box(".toolbar")["height"] > 60
        more.click()
        expect(page.locator(".toolbar .history")).to_be_hidden()
        assert box(".toolbar")["height"] <= 40

        # 展開之後切換到另一個檢視：收起來。固定大小、沒有手機版面的 dashboard 照原尺寸，在區塊裡捲動
        more.click()
        page.get_by_label("切換檢視").select_option(f"d:{fixed.id}")
        page.wait_for_url(f"{server}/w/superstore_features/d/{fixed.id}")
        page.wait_for_selector(".worksheet[data-loading='false']")
        expect(more).to_have_attribute("aria-expanded", "false")
        board = box(".dashboard")
        assert (board["width"], board["height"]) == (fixed.size.width, fixed.size.height)
        assert wrapper.evaluate("e => e.scrollWidth > e.clientWidth")
        no_page_scroll()

        # 有手機版面：用手機版面，寬度在畫面之內
        page.goto(f"{server}/w/superstore_features/d/{phone.id}")
        page.wait_for_function(ALL_FOUR)
        assert page.locator(".dashboard").get_attribute("data-device") == "phone"
        board = box(".dashboard")
        assert board["width"] <= 390 and board["right"] <= 390, board
        assert wrapper.evaluate("e => e.scrollWidth <= e.clientWidth")
        no_page_scroll()

        # 單張工作表：在畫面的寬度之內，圖在工具列下面
        page.goto(f"{server}/w/superstore_features/s/ws_sales_by_segment")
        page.wait_for_selector(".single-sheet .worksheet[data-loading='false']")
        single = box(".single-sheet")
        assert single["right"] <= 390 and single["y"] <= 50 and single["bottom"] <= 844, single
        no_page_scroll()

        # Story：標題與故事點的導覽在畫面的寬度之內（原本整個 Story 是 1016 寬，導覽要捲到右邊才看得到）
        page.goto(f"{server}/w/superstore_features/story/{story.id}")
        page.wait_for_selector(".story .worksheet[data-loading='false']")
        page.wait_for_function(loaded)
        assert box(".story")["width"] <= 390 and box(".story-nav")["right"] <= 390
        assert box(".story-title")["right"] <= 390
        next_point = page.get_by_role("button", name="下一個故事點")
        if next_point.count():
            assert box(".story-arrow:last-of-type")["right"] <= 390
        no_page_scroll()
        page.close()

        # 單張工作表有控制項：控制項排在圖的下面，圖的寬度不被擠掉（原本控制項占 210 像素，圖只剩 140）
        with running_server(tmp_path / "fixtures", DIST) as other:
            narrow = browser.new_page(viewport={"width": 390, "height": 844})
            narrow.on("pageerror", lambda exc: errors.append(str(exc)))
            narrow.goto(f"{other}/w/controls/s/ws_sales_by_region")
            narrow.wait_for_selector(".single-sheet .worksheet[data-loading='false']")
            main = narrow.evaluate(BOX, ".single-sheet .sheet-main")
            controls = narrow.evaluate(BOX, ".single-sheet .sheet-controls")
            assert main["width"] >= 340 and controls["y"] >= main["bottom"], (main, controls)
            assert controls["right"] <= 390
            narrow.close()
            # 桌面的寬度：控制項在圖的右邊（不變）
            wide = browser.new_page(viewport={"width": 1440, "height": 900})
            wide.goto(f"{other}/w/controls/s/ws_sales_by_region")
            wide.wait_for_selector(".single-sheet .worksheet[data-loading='false']")
            main = wide.evaluate(BOX, ".single-sheet .sheet-main")
            controls = wide.evaluate(BOX, ".single-sheet .sheet-controls")
            assert controls["x"] >= main["right"] and controls["width"] == 210, (main, controls)
            wide.close()

        # 桌面與平板大小的畫面不變：沒有「更多」，工具都在，dashboard 隨畫面調整大小、不捲動
        for size in ((1440, 900), (1024, 768), (768, 1024)):
            desktop = browser.new_page(viewport={"width": size[0], "height": size[1]})
            desktop.on("pageerror", lambda exc: errors.append(str(exc)))
            desktop.goto(f"{server}/w/superstore_features/d/{overview.id}")
            desktop.wait_for_selector(".worksheet[data-loading='false']")
            expect(desktop.get_by_role("button", name="更多工具")).to_be_hidden()
            for shown in (".toolbar .history", ".toolbar .cases", ".toolbar .wb", ".toolbar .sep"):
                expect(desktop.locator(shown)).to_be_visible()
            board = desktop.evaluate(BOX, ".dashboard")
            wrapper_width = desktop.locator(".dashboard-wrapper").evaluate("e => e.clientWidth")
            assert board["width"] == wrapper_width == size[0] - 32, (size, board)
            assert (
                desktop.locator(".page").evaluate("e => getComputedStyle(e).paddingLeft") == "16px"
            )
            if size == (1440, 900):
                # 工具列一列半（62 像素）、dashboard 從 86 像素開始：與修改之前相同
                assert desktop.evaluate(BOX, ".toolbar")["height"] == 62 and board["y"] == 86
            desktop.goto(f"{server}/w/superstore_features/story/{story.id}")
            desktop.wait_for_selector(".story .worksheet[data-loading='false']")
            assert desktop.evaluate(BOX, ".story")["width"] == story.size.width
            desktop.close()
        assert errors == []
        browser.close()
