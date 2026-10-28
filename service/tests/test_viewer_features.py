"""功能報表的檢視器端到端：篩選群組與全域篩選（H1）、自動尺寸（I1）、擴充圖表（J1–J3）、參考線（J4）、
群組（K1）、篩選條件（K4）、篩選、網址、參數與切換動作（L1、L2）。

需要先建置檢視器（cd web && pnpm install && pnpm build）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType
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
        # 文字表的 Measure Names 依 Measure Values 卡的順序
        headers = page.eval_on_selector_all(
            "[data-worksheet='ws_segment_kpis'] svg text", "els => els.map(e => e.textContent)"
        )
        order = [h for h in headers if h in ("Sales", "Profit", "CNTD(Order ID)", "利潤率")]
        assert order[:4] == ["Sales", "Profit", "CNTD(Order ID)", "利潤率"]
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
        assert {"Fast", "Other"} <= set(texts("ws_sales_by_ship_speed"))
        # x 軸的分組標籤依下限排序（之後的 0 是 y 軸的刻度）
        bins = [t for t in texts("ws_sales_by_quantity_bin") if t in {"0", "3", "6", "9", "12"}]
        assert bins[:5] == ["0", "3", "6", "9", "12"]
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
        assert page.locator("[data-zone='z9'] select").input_value() == '"West"'

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
        assert count("ws_region_sales_bands", "rule mark") == 3  # 中位數與平均 ± 1 個標準差
        assert errors == []
        browser.close()
