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
                # 捲動時固定的標題是原圖的複本，不算
                "els => els.filter(e => !e.closest('.frozen')).map(e => e.getBoundingClientRect().top)",
            )
            return sorted(tops)

        def sizes(zone: str) -> tuple[float, float, float]:
            return page.locator(f"[data-zone='{zone}'] .chart").evaluate(
                "e => [e.querySelector('svg').getBoundingClientRect().height, e.clientHeight,"
                " e.scrollHeight]"
            )

        # 標準大小：每一列 20 像素（文字表與長條圖相同），不拉長填滿區塊；文字表的欄依內容固定寬度
        # （放不下時也可以左右捲動）
        for zone, scroll in (("z9", "both"), ("z10", "y")):
            chart = page.locator(f"[data-zone='{zone}'] .chart")
            assert chart.get_attribute("data-scroll") == scroll, zone
            tops = rows(zone)
            assert tops[1] - tops[0] == pytest.approx(20, abs=0.5), zone
        svg, client, _ = sizes("z9")
        assert svg < client - 40
        # 區塊放不下時捲動；與 Tableau 相同，捲動時欄位標籤固定在上緣、座標軸固定在下緣
        svg, client, scroll = sizes("z10")
        assert scroll > client
        chart = page.locator("[data-zone='z10'] .chart")
        chart.evaluate("e => e.scrollTop = 60")
        page.wait_for_timeout(200)
        box = chart.bounding_box()
        top = page.locator("[data-zone='z10'] .frozen-top").bounding_box()
        bottom = page.locator("[data-zone='z10'] .frozen-bottom").bounding_box()
        assert box is not None and top is not None and bottom is not None
        assert top["y"] == pytest.approx(box["y"], abs=1)
        assert bottom["y"] + bottom["height"] == pytest.approx(box["y"] + box["height"], abs=1)
        # 整個檢視：4 個區的長條填滿區塊的高度、不捲動
        assert page.locator("[data-zone='z7'] .chart").get_attribute("data-scroll") == "false"
        tops = rows("z7")
        assert tops[1] - tops[0] > 40
        svg, client, _ = sizes("z7")
        assert svg == pytest.approx(client, abs=2)
        browser.close()


ROW_LAYOUT = """(sheet) => {
  // 一張工作表的文字表：不同列（場景圖項目的 y 不同）、左右有交集的兩格文字，外框上下重疊超過 1 像素的有幾對；
  // 相鄰兩列相隔多遠（列高）；圖與區塊的高度、能不能捲動、列高是不是保持標準的大小（data-rows）
  const chart = document.querySelector(`[data-worksheet='${sheet}'] .chart`);
  const svg = chart.querySelector(':scope > svg, :scope > .frozen-host > svg');
  const texts = [];
  for (const group of svg.querySelectorAll('g.mark-text.role-mark')) {
    if ([...group.classList].some((name) => name.startsWith('__'))) continue;
    for (const text of group.querySelectorAll(':scope > text'))
      texts.push({ box: text.getBoundingClientRect(), row: text.__data__.y });
  }
  let overlapping = 0;
  for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
    const a = texts[i], b = texts[j];
    if (Math.abs(a.row - b.row) < 0.5) continue;
    const wide = Math.min(a.box.right, b.box.right) - Math.max(a.box.left, b.box.left);
    const tall = Math.min(a.box.bottom, b.box.bottom) - Math.max(a.box.top, b.box.top);
    if (wide > 1 && tall > 1) overlapping++;
  }
  const rows = [...new Set(texts.map((t) => Math.round(t.row * 10) / 10))].sort((a, b) => a - b);
  return {
    texts: texts.length,
    overlapping,
    step: rows.length > 1 ? rows[1] - rows[0] : null,
    textHeight: Math.max(...texts.map((t) => t.box.height)),
    svg: svg.getBoundingClientRect().height,
    client: chart.clientHeight,
    scrollable: chart.scrollHeight - chart.clientHeight,
    scroll: chart.dataset.scroll,
    kept: chart.dataset.rows === 'kept',
    frozen: chart.querySelectorAll('.frozen-top').length,
  };
}"""


def test_fit_height_keeps_rows_readable_when_the_zone_is_too_short(tmp_path: Path) -> None:
    """符合高度、整個檢視時列平分區塊的高度；區塊太矮、把列壓到相鄰兩列的文字互相蓋住時不再壓——改用標準大小
    的列高，放不下的在區塊內上下捲動（欄標題固定）。文字沒有互相蓋住的照舊填滿：只是被壓得比標準的列高小一點
    的不受影響。

    原本一律填滿：17 列、每格三行的文字表在 592 像素高的檢視中每列只有 33 像素，44 像素高的文字上下疊在一起
    （16 對、最多疊 11 像素）。Tableau 怎麼處理沒有查到文件，這是平台自己的做法（README「已知限制與待驗證」）。"""
    from playwright.sync_api import sync_playwright

    from tmig.edit import add_worksheet, replace_worksheet

    target = tmp_path / "rows"
    target.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", target / "superstore_demo.twbx")
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")

    def use(field: str, agg: str | None = None) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": None,
            "continuous": agg is not None,
            "table_calc": None,
        }

    # Sub-Category（17 列）的文字表，標籤上一到三個量值（每個量值一行）
    measures = [use("f_sales", "sum"), use("f_profit", "sum"), use("f_quantity", "sum")]
    sheets: dict[str, str] = {}
    for name, lines, fit in (
        ("三行", 3, "entire_view"),
        ("三行符合高度", 3, "fit_height"),
        ("兩行", 2, "entire_view"),
        ("一行", 1, "entire_view"),
    ):
        ir, created = add_worksheet(ir, name, None)
        data = created.model_dump(mode="json")
        data["rows"] = [use("f_sub_category")]
        data["marks"]["label"] = measures[:lines]
        data["fit"] = fit
        ir, saved = replace_worksheet(ir, data)
        sheets[name] = saved.id

    # dashboard 上很矮的區塊（顯示大小由區塊指定）：也是三行的文字表
    def refit(zones: list[Any]) -> list[Any]:
        return [
            z.model_copy(
                update={
                    "children": refit(z.children),
                    **(
                        {"worksheet": sheets["三行"], "fit": "entire_view"} if z.id == "z10" else {}
                    ),
                }
            )
            for z in zones
        ]

    board = ir.dashboards[0]
    board = board.model_copy(update={"zones": refit(board.zones)})
    ir = ir.model_copy(update={"dashboards": [board]})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")

    with running_server(tmp_path, DIST) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def standard(name: str) -> int:
            """標準大小的列高（規格的 usermeta.sizing.rows.step）。"""
            response = page.request.post(
                f"{url}/api/workbooks/rows/worksheets/{sheets[name]}/render"
            )
            return response.json()["spec"]["usermeta"]["sizing"]["rows"]["step"]

        def layout(name: str, path: str | None = None) -> dict[str, Any]:
            sheet = sheets[name]
            page.goto(f"{url}/w/rows/{path or 's/' + sheet}")
            page.wait_for_selector(f"[data-worksheet='{sheet}'][data-loading='false']")
            page.wait_for_selector(f"[data-worksheet='{sheet}'] .chart[data-drawn='true']")
            page.wait_for_timeout(300)
            return page.evaluate(ROW_LAYOUT, sheet)

        assert (standard("一行"), standard("兩行"), standard("三行")) == (20, 34, 48)

        # 三行、整個檢視：填滿的話每列比文字矮——列高保持標準的 48 像素，文字不重疊，上下捲動、欄標題固定
        for name, scroll in (("三行", "y"), ("三行符合高度", "both")):
            found = layout(name)
            assert found["texts"] == 17 and found["overlapping"] == 0, (name, found)
            assert found["kept"] and found["step"] == pytest.approx(48, abs=0.1), (name, found)
            assert found["textHeight"] <= 48 and found["scroll"] == scroll, (name, found)
            assert found["scrollable"] > 100 and found["frozen"] == 1, (name, found)

        # 兩行：填滿時每列比標準的列高（34 像素）小一點，但文字沒有互相蓋住——照舊填滿，不捲動
        found = layout("兩行")
        assert found["texts"] == 17 and found["overlapping"] == 0, found
        assert not found["kept"] and found["scroll"] == "false", found
        assert found["textHeight"] < found["step"] < 34, found
        assert found["svg"] == pytest.approx(found["client"], abs=2) and found["scrollable"] <= 1

        # 一行：列被拉高到填滿，照舊
        found = layout("一行")
        assert found["overlapping"] == 0 and not found["kept"] and found["scroll"] == "false", found
        assert found["step"] > 20 and found["svg"] == pytest.approx(found["client"], abs=2)

        # dashboard 上很矮的區塊：列高保持，區塊內捲動
        found = layout("三行", f"d/{board.id}")
        assert found["texts"] == 17 and found["overlapping"] == 0, found
        assert found["kept"] and found["step"] == pytest.approx(48, abs=0.1), found
        assert found["scrollable"] > 100 and found["frozen"] == 1, found
        assert errors == []
        browser.close()


def test_filled_width_leaves_room_for_the_scrollbar(tmp_path: Path) -> None:
    """寬度填滿區塊而上下捲動的文字表（符合寬度；整個檢視時區塊太矮、列高保持）：捲軸占寬度的環境裡（Windows 上
    的瀏覽器、桌面版的視窗）捲軸在圖畫好之後才出現，圖要照剩下的寬度重排——最右邊的數字不被捲軸蓋住。

    原本圖照沒有捲軸時的寬度畫（2026-11-22 在桌面版的視窗裡量到：圖寬 1038 像素、可用的寬度 1023 像素，17 格
    靠右的數字都超出 6.5 像素、最後一位被捲軸蓋住）。測試用的瀏覽器預設不顯示捲軸（--hide-scrollbars），這裡
    拿掉這個參數。"""
    from playwright.sync_api import sync_playwright

    from tmig.edit import add_worksheet, replace_worksheet

    target = tmp_path / "rows"
    target.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", target / "superstore_demo.twbx")
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")

    def use(field: str, agg: str | None = None) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": None,
            "continuous": agg is not None,
            "table_calc": None,
        }

    measures = [use("f_sales", "sum"), use("f_profit", "sum"), use("f_quantity", "sum")]
    sheets: dict[str, str] = {}
    for name, fit in (("符合寬度", "fit_width"), ("整個檢視", "entire_view")):
        ir, created = add_worksheet(ir, name, None)
        data = created.model_dump(mode="json")
        data["rows"] = [use("f_sub_category")]
        data["marks"]["label"] = measures
        data["fit"] = fit
        ir, saved = replace_worksheet(ir, data)
        sheets[name] = saved.id
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")

    measure = """(sheet) => {
      const chart = document.querySelector(`[data-worksheet='${sheet}'] .chart`);
      const svg = chart.querySelector(':scope > svg, :scope > .frozen-host > svg');
      const visible = chart.getBoundingClientRect().left + chart.clientWidth;
      const rights = [];
      for (const group of svg.querySelectorAll('g.mark-text.role-mark')) {
        if ([...group.classList].some((name) => name.startsWith('__'))) continue;
        for (const text of group.querySelectorAll(':scope > text'))
          rights.push(text.getBoundingClientRect().right);
      }
      return {
        texts: rights.length,
        beyond: Math.max(...rights) - visible,
        scrollbar: chart.offsetWidth - chart.clientWidth,
        svg: svg.getBoundingClientRect().width,
        client: chart.clientWidth,
        scroll: chart.dataset.scroll,
        scrollable: chart.scrollHeight - chart.clientHeight,
      };
    }"""

    with running_server(tmp_path, DIST) as url, sync_playwright() as p:
        browser = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        for name in ("符合寬度", "整個檢視"):
            sheet = sheets[name]
            page.goto(f"{url}/w/rows/s/{sheet}")
            page.wait_for_selector(f"[data-worksheet='{sheet}'][data-loading='false']")
            page.wait_for_selector(f"[data-worksheet='{sheet}'] .chart[data-drawn='true']")
            page.wait_for_timeout(300)
            found = page.evaluate(measure, sheet)
            assert found["texts"] == 17 and found["scroll"] == "y", (name, found)
            assert found["scrollable"] > 100, (name, found)
            if found["scrollbar"] == 0:
                pytest.skip("這個環境的捲軸不占寬度，測不到")
            # 圖的寬度是扣掉捲軸之後的寬度，靠右的數字都在看得到的範圍裡
            assert found["svg"] == pytest.approx(found["client"], abs=1), (name, found)
            assert found["beyond"] <= 0.5, (name, found)
        assert errors == []
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
