"""檢視器端到端（F3、F4）：dashboard 區塊位置與 IR 相同；切換各組篩選後，畫面上的數字與預期 CSV 一致。

需要先建置檢視器（cd web && pnpm install && pnpm build）與 Playwright 的 Chromium；缺少時略過。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import QueryResult
from tmig.ir import ColorPalette, FieldUse, ZoneStyle, iter_zones, load_ir
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
        # 色彩圖例與圖表的顏色相同：沒有指定顏色時依 Tableau 10（#4e79a7、#f28e2b）的順序
        legend = page.locator("[data-zone='z15'] .legend-item")
        legend.first.wait_for()
        assert legend.all_inner_texts() == ["Furniture", "Technology"]
        swatches = page.locator("[data-zone='z15'] .swatch")
        colors = [s.evaluate("e => getComputedStyle(e).backgroundColor") for s in swatches.all()]
        assert colors == ["rgb(78, 121, 167)", "rgb(242, 142, 43)"]
        browser.close()


def test_continuous_color_legend(tmp_path: Path) -> None:
    """依量值著色時，dashboard 的色彩圖例是色帶與最小值、最大值（分段色彩以實色的區塊呈現）。

    以示範報表為底，「Sales by Region」改為依 SUM(Sales) 著色、以 Red 分成 4 段。"""
    from playwright.sync_api import sync_playwright

    target = tmp_path / "colors"
    target.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", target / "superstore_demo.twbx")
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")
    sheets = []
    for ws in ir.worksheets:
        if ws.id == "ws_sales_by_region":
            palette = ColorPalette(kind="continuous", palette="red_10_0", steps=4)
            marks = ws.marks.model_copy(
                update={"color": FieldUse(field="f_sales", agg="sum"), "color_palette": palette}
            )
            ws = ws.model_copy(update={"marks": marks})
        sheets.append(ws)
    ir = ir.model_copy(update={"worksheets": sheets})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
    board = ir.dashboards[0]
    with running_server(tmp_path, DIST) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{url}/w/colors/d/{board.id}")
        ramp = page.locator("[data-zone='z15'] .legend-ramp")
        ramp.wait_for()
        background = ramp.evaluate("e => getComputedStyle(e).backgroundImage")
        # Red 由淺到深：第一段 #ffbeb2、最後一段 #ae123a，分段處是實色的邊界
        assert background.startswith("linear-gradient")
        assert "rgb(255, 190, 178)" in background and "rgb(174, 18, 58)" in background
        low, high = page.locator("[data-zone='z15'] .legend-range span").all_inner_texts()
        response = page.request.post(
            f"{url}/api/workbooks/colors/worksheets/ws_sales_by_region/render"
        )
        values = response.json()["spec"]["data"]["values"]
        field = response.json()["spec"]["encoding"]["color"]["field"]
        smallest = min(values, key=lambda v: v[field])
        largest = max(values, key=lambda v: v[field])
        assert (low, high) == (smallest[f"{field}_label"], largest[f"{field}_label"])
        browser.close()


def test_zone_styles_and_backgrounds(tmp_path: Path) -> None:
    """dashboard 與區塊的格式：背景、框線、外距與內距；有框線的容器畫在子區塊之下；透明背景的工作表；
    不顯示標題時列數與資料切換只在滑過時顯示；dashboard 上的工作表不畫圖例（圖例是另外的區塊）。"""
    from playwright.sync_api import sync_playwright

    target = tmp_path / "styles"
    target.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", target / "superstore_demo.twbx")
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")
    styles = {
        "z5": ZoneStyle(
            border="solid", border_width=2, border_color="#bd8459", margin=(2, 2, 2, 2)
        ),
        "z7": ZoneStyle(background="#ffffff", margin=(4, 4, 4, 4), padding=(0, 0, 0, 15)),
    }

    def restyle(zones: list[Any]) -> list[Any]:
        updated = []
        for zone in zones:
            changes: dict[str, Any] = {"children": restyle(zone.children)}
            if zone.id in styles:
                changes["style"] = styles[zone.id]
            if zone.id == "z8":
                changes["show_title"] = False
            updated.append(zone.model_copy(update=changes))
        return updated

    board = ir.dashboards[0]
    board = board.model_copy(update={"background": "#f3ede7", "zones": restyle(board.zones)})
    sheets = [
        ws.model_copy(update={"background": "#00000000"}) if ws.id == "ws_sales_by_region" else ws
        for ws in ir.worksheets
    ]
    ir = ir.model_copy(update={"dashboards": [board], "worksheets": sheets})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
    with running_server(tmp_path, DIST) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{url}/w/styles/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )

        def css(selector: str, name: str) -> str:
            return page.locator(selector).first.evaluate(f"e => getComputedStyle(e).{name}")

        assert css(".dashboard", "backgroundColor") == "rgb(243, 237, 231)"
        # 沒有背景與框線的容器不畫；有框線的容器畫出、不攔截滑鼠
        assert page.locator("[data-zone='z3'], [data-zone='z4'], [data-zone='z11']").count() == 0
        assert css("[data-zone='z5']", "pointerEvents") == "none"
        assert css("[data-zone='z5'] > .zone-box", "border") == "2px solid rgb(189, 132, 89)"
        # 外距在區塊之內、框線與背景之外；內容再往內縮內距
        zone = next(z for z in iter_zones(board.zones) if z.id == "z7")
        origin = page.locator(".dashboard").bounding_box()
        box = page.locator("[data-zone='z7'] > .zone-box").bounding_box()
        assert origin is not None and box is not None
        actual = (box["x"] - origin["x"], box["y"] - origin["y"], box["width"], box["height"])
        assert actual == pytest.approx((zone.x + 4, zone.y + 4, zone.w - 8, zone.h - 8), abs=1.5)
        assert css("[data-zone='z7'] > .zone-box", "paddingLeft") == "15px"
        assert css("[data-zone='z7'] > .zone-box", "backgroundColor") == "rgb(255, 255, 255)"
        assert css("[data-zone='z7'] .worksheet", "backgroundColor") == "rgba(0, 0, 0, 0)"
        # 不顯示標題：列數與資料切換只在滑過時顯示
        overlay = "[data-zone='z8'] .worksheet-overlay"
        assert css(overlay, "opacity") == "0"
        page.hover("[data-zone='z8'] .worksheet-body")
        page.wait_for_function(
            f"getComputedStyle(document.querySelector(\"{overlay}\")).opacity === '0.85'"
        )
        # 圖例只畫在圖例區塊（z15）
        assert page.locator(".dashboard .chart .role-legend").count() == 0
        assert page.locator("[data-zone='z15'] .legend-item").count() > 0
        browser.close()


def test_fit_modes_size_and_scroll(tmp_path: Path) -> None:
    """工作表區塊的顯示大小：標準大小的長條圖每一列 20 像素，不拉長填滿區塊、放不下時在區塊內捲動；
    整個檢視填滿區塊。"""
    from playwright.sync_api import sync_playwright

    target = tmp_path / "fits"
    target.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", target / "superstore_demo.twbx")
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")
    changes: dict[str, dict[str, Any]] = {
        "z9": {"fit": "standard"},
        "z10": {"fit": "standard", "h": 150},  # 比各列的高度合計矮：捲動
        "z7": {"fit": "entire_view"},
    }

    def refit(zones: list[Any]) -> list[Any]:
        return [
            z.model_copy(update={"children": refit(z.children), **changes.get(z.id, {})})
            for z in zones
        ]

    board = ir.dashboards[0]
    board = board.model_copy(update={"zones": refit(board.zones)})
    ir = ir.model_copy(update={"dashboards": [board]})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
    with running_server(tmp_path, DIST) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{url}/w/fits/d/{board.id}")
        page.wait_for_function(
            "document.querySelectorAll('.worksheet[data-loading=\"false\"]').length === 4"
        )
        page.wait_for_timeout(500)

        def rows(zone: str) -> list[float]:
            # 縱軸（列標題）的標籤：Vega-Lite 的座標軸群組以 aria-label 標示 X-axis／Y-axis
            tops = page.eval_on_selector_all(
                f"[data-zone='{zone}'] .role-axis[aria-label^='Y-axis'] .role-axis-label text",
                "els => els.map(e => e.getBoundingClientRect().top)",
            )
            return sorted(tops)

        def sizes(zone: str) -> tuple[float, float, float]:
            return page.locator(f"[data-zone='{zone}'] .chart").evaluate(
                "e => [e.querySelector('svg').getBoundingClientRect().height, e.clientHeight,"
                " e.scrollHeight]"
            )

        # 標準大小：每一列 20 像素（文字表與長條圖相同），不拉長填滿區塊
        for zone in ("z9", "z10"):
            assert page.locator(f"[data-zone='{zone}'] .chart").get_attribute("data-scroll") == "y"
            tops = rows(zone)
            assert tops[1] - tops[0] == pytest.approx(20, abs=0.5), zone
        svg, client, _ = sizes("z9")
        assert svg < client - 40
        # 區塊放不下時捲動
        svg, client, scroll = sizes("z10")
        assert scroll > client
        # 整個檢視：4 個區的長條填滿區塊的高度、不捲動
        assert page.locator("[data-zone='z7'] .chart").get_attribute("data-scroll") == "false"
        tops = rows("z7")
        assert tops[1] - tops[0] > 40
        svg, client, _ = sizes("z7")
        assert svg == pytest.approx(client, abs=2)
        browser.close()


def test_datasource_page(server: str) -> None:
    """資料源分頁：連線、資料源篩選、資料格（前 100 列，含總列數）與欄位（公式以顯示名稱呈現）。"""
    from playwright.sync_api import sync_playwright

    ir = load_ir(DEMO / "golden.ir.json")
    ds = ir.datasources[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{server}/w/superstore_demo/d/{ir.dashboards[0].id}")
        page.wait_for_selector(".dashboard")
        # 工具列的切換清單中有資料源
        option = page.locator("select[aria-label='切換檢視'] option", has_text="資料源：")
        assert option.all_inner_texts() == [f"資料源：{ds.caption}"]
        page.select_option("select[aria-label='切換檢視']", f"ds:{ds.id}")
        page.wait_for_selector(".ds-data tbody tr")
        assert page.locator(".ds-header h1").inner_text() == ds.caption
        assert "Tableau 萃取（Hyper）" in page.locator(".ds-side").inner_text()
        assert page.locator(".ds-data tbody tr").count() == 100
        headers = page.locator(".ds-data thead th").all_inner_texts()
        assert any(h.endswith("Order ID") for h in headers)
        assert "共" in page.locator(".ds-count").inner_text()
        page.get_by_role("tab", name="欄位").click()
        rows = page.locator(".ds-fields tbody tr")
        assert rows.count() == len(ds.fields)
        share = rows.filter(has_text="區域占比").inner_text()
        assert "SUM([區域總銷售])" in share and "Calculation_" not in share
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
