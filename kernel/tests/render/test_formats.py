"""工作表與 dashboard 的格式：座標軸（顯示、標題、範圍、反轉、同步）、格線與座標軸線、離散欄位的
標題與欄位標籤、背景與文字顏色、多邊形，以及 dashboard 區塊的框線、外距與內距。

寫法取自公開報表（HR Dashboards、TabMon、Snow Sports 等）。測試資料見 tests/sqlgen/test_query.py：
各區的 SUM(Sales) East 350、North 60、South 100、West 800。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tmig.ir import Axis, FieldUse, ShelfStyle, ZoneStyle, iter_zones
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import main_encoding

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from ..parser.test_extensions import measure_names_filter
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, parse, render

__all__ = ["db"]  # pytest fixture

MONTH = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")
SALES_USE = FieldUse(field="f_sales", agg="sum", continuous=True)


def styled(sheet: str, *rules: str) -> str:
    """工作表的 <style> 換成這些 <style-rule>。"""
    return sheet.replace("<style />", "<style>" + "".join(rules) + "</style>", 1)


def rule(element: str, *items: str) -> str:
    return f"<style-rule element='{element}'>{''.join(items)}</style-rule>"


def fmt(attr: str, value: str, field: str | None = None, scope: str | None = None) -> str:
    extra = (f" field='{ref(field)}'" if field else "") + (f" scope='{scope}'" if scope else "")
    return f"<format attr='{attr}' value='{value}'{extra} />"


def space(field: str, scope: str, **attrs: str) -> str:
    extra = "".join(f" {k.replace('_', '-')}='{v}'" for k, v in attrs.items())
    return (
        f"<encoding attr='space' class='0' field='{ref(field)}' field-type='quantitative' "
        f"scope='{scope}' type='space'{extra} />"
    )


def bars(*rules: str) -> str:
    sheet = worksheet(
        "Bars", [REGION, SALES, PROFIT], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    return styled(sheet, *rules)


# ---------------------------------------------------------------- 座標軸

DARK_BARS = bars(
    rule(
        "axis",
        fmt("title", "Revenue", "sum:Sales:qk", "cols"),
        space(
            "sum:Sales:qk", "cols", range_type="fixed", min="-100", max="900", domain_expand="false"
        ),
        # 不在欄列架上的欄位（移除前留下的設定）與參數不算
        space("sum:Profit:qk", "rows", range_type="fixedmin", min="0"),
        "<format attr='display' class='0' field='[Parameters].[Parameter 1]' scope='cols' value='false' />",
        fmt("stroke-size", "0", scope="cols"),
        fmt("tick-color", "#00000000"),
    ),
    rule("gridline", fmt("line-visibility", "off", scope="rows")),
    rule("table", fmt("background-color", "#1B1B1B")),
    rule("worksheet", fmt("color", "#F5F5F5"), fmt("display-field-labels", "false", scope="rows")),
    rule("label", fmt("color", "#ffcc00", "none:Region:nk")),
)


def test_axis_lines_and_colors_are_parsed(tmp_path: Path) -> None:
    ir = parse(tmp_path, DARK_BARS)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    region = FieldUse(field="f_region")
    assert ws.axes == [
        Axis(
            use=SALES_USE,
            shelf="columns",
            title="Revenue",
            start=-100.0,
            end=900.0,
            include_zero=False,
        ),
        Axis(use=region, shelf="rows", label_color="#ffcc00"),
    ]
    # 座標軸線只有欄的不畫；刻度兩個方向都不畫（沒有 scope）；格線只有列的不畫
    assert ws.column_style == ShelfStyle(axis=False, ticks=False)
    assert ws.row_style == ShelfStyle(grid=False, ticks=False, field_labels=False)
    assert (ws.background, ws.text_color) == ("#1b1b1b", "#f5f5f5")


def test_axis_lines_and_colors_are_rendered(tmp_path: Path, db: Path) -> None:
    spec = render(parse(tmp_path, DARK_BARS), db).spec
    x, y = spec["encoding"]["x"], spec["encoding"]["y"]
    # 固定範圍：不延伸到整數刻度、不必包含 0；範圍外的標記裁切
    assert x["scale"] == {"domain": [-100.0, 900.0], "nice": False, "zero": False}
    assert spec["mark"]["clip"] is True
    assert x["axis"]["title"] == "Revenue"
    assert (x["axis"]["domain"], x["axis"]["ticks"]) == (False, False)
    # 格線照畫（零線另以 0 的格線畫出）
    assert x["axis"]["grid"] is True and "gridOpacity" not in x["axis"]
    # 離散的列標題：欄位標籤不顯示、成員的文字顏色（與 Tableau 相同畫成一欄、靠左、不畫刻度）
    axis = y["axis"]
    assert (axis["title"], axis["labelColor"]) == (None, "#ffcc00")
    assert (axis["labelAlign"], axis["ticks"]) == ("left", False)
    assert axis["minExtent"] == axis["maxExtent"] == axis["labelPadding"] + 4
    assert spec["background"] == "#1b1b1b"
    assert spec["config"]["axis"]["labelColor"] == "#f5f5f5"
    assert spec["config"]["legend"]["titleColor"] == "#f5f5f5"
    assert spec["usermeta"]["background"] == "#1b1b1b"
    assert spec["usermeta"]["title_color"] == "#f5f5f5"


def test_hidden_reversed_and_date_range_axes(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "Trend",
        [MONTH, SALES],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
    )
    sheet = styled(
        sheet,
        rule(
            "axis",
            fmt("display", "false", "sum:Sales:qk", "rows"),
            space("sum:Sales:qk", "rows", reverse="true"),
            space("tmn:Order Date:qk", "cols", range_type="fixedmin", min="#2023-02-01#"),
        ),
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    spec = render(ir, db).spec
    x, y = spec["encoding"]["x"], spec["encoding"]["y"]
    # 隱藏的座標軸與 Tableau 相同保留格線
    shown = {k: y["axis"][k] for k in ("labels", "ticks", "domain", "title")}
    assert shown == {"labels": False, "ticks": False, "domain": False, "title": None}
    assert y["scale"] == {"reverse": True}
    start = datetime(2023, 2, 1, tzinfo=UTC).timestamp() * 1000
    assert x["type"] == "temporal"
    assert x["scale"] == {"domainMin": start, "nice": False}  # 時間軸沒有 zero


def test_axis_on_measure_values(tmp_path: Path, db: Path) -> None:
    """Measure Values 的座標軸寫成 [Multiple Values]；空的標題表示不顯示標題。"""
    sheet = worksheet(
        "Values",
        [REGION, SALES, PROFIT],
        rows=ref("Multiple Values"),
        cols=ref("none:Region:nk"),
        encodings=f"<color column='{ref(':Measure Names')}' />",
        view_extra=measure_names_filter("sum:Sales:qk", "sum:Profit:qk"),
    )
    sheet = styled(sheet, rule("axis", fmt("title", "", "Multiple Values", "rows")))
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    assert [(a.use.field, a.shelf, a.title) for a in ir.worksheets[0].axes] == [
        ("measure_values", "rows", "")
    ]
    y = render(ir, db).spec["encoding"]["y"]
    assert y["axis"]["title"] is None


# ---------------------------------------------------------------- 雙軸

PANE = (
    "<pane id='{id}' y-axis-name='{axis}'{extra}><view><breakdown value='auto' /></view>"
    "<mark class='{mark}' /><encodings /></pane>"
)


def dual(synchronized: bool) -> str:
    panes = PANE.format(id=1, axis=ref("sum:Sales:qk"), extra="", mark="Bar") + PANE.format(
        id=2, axis=ref("sum:Profit:qk"), extra=" y-index='1'", mark="Line"
    )
    sheet = worksheet(
        "Dual",
        [CATEGORY, SALES, PROFIT],
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Category:nk"),
        mark="Automatic",
        panes_extra=panes,
    )
    sync = {"synchronized": "true"} if synchronized else {}
    return styled(sheet, rule("axis", space("sum:Profit:qk", "rows", fold="true", **sync)))


def test_synchronized_dual_axis_shares_the_scale(tmp_path: Path, db: Path) -> None:
    for synchronized, resolve in ((True, "shared"), (False, "independent")):
        ir = parse(tmp_path, dual(synchronized))
        assert ir.unsupported == []
        assert [a.synchronized for a in ir.worksheets[0].axes] == [synchronized][
            : int(synchronized)
        ]
        spec = render(ir, db).spec
        assert spec["resolve"] == {"scale": {"y": resolve}}


def test_log_scale_and_fixed_ticks(tmp_path: Path, db: Path) -> None:
    """編輯座標軸的刻度：對數刻度、固定間距的主要刻度（起點預設 0）、不顯示主要刻度。"""
    log = bars(rule("axis", space("sum:Sales:qk", "cols", scale="log")))
    ir = parse(tmp_path, log)
    assert ir.unsupported == []
    (axis,) = ir.worksheets[0].axes
    assert (axis.scale, axis.ticks, axis.tick_spacing) == ("log", True, None)
    x = main_encoding(render(ir, db).spec)["x"]  # type: ignore[index]
    assert x["scale"] == {"type": "log", "zero": False}

    # 固定間距 250（起點 100）：100、350、600；資料的範圍 0 到各區的合計 1310
    fixed = bars(
        rule("axis", space("sum:Sales:qk", "cols", major_spacing="250", major_origin="100"))
    )
    ir = parse(tmp_path, fixed)
    (axis,) = ir.worksheets[0].axes
    assert (axis.scale, axis.tick_spacing, axis.tick_origin) == ("linear", 250.0, 100.0)
    x = main_encoding(render(ir, db).spec)["x"]  # type: ignore[index]
    assert x["axis"]["values"] == [100.0, 350.0, 600.0, 850.0, 1100.0]
    assert "type" not in x.get("scale", {})
    # 固定的範圍 0 到 500：刻度只到 500
    ranged = bars(
        rule(
            "axis",
            space(
                "sum:Sales:qk", "cols", major_spacing="200", range_type="fixed", min="0", max="500"
            ),
        )
    )
    x = main_encoding(render(parse(tmp_path, ranged), db).spec)["x"]  # type: ignore[index]
    assert x["axis"]["values"] == [0.0, 200.0, 400.0]

    # 不顯示主要刻度；日期單位的間距（major-units）依自動
    hidden = bars(
        rule(
            "axis",
            space(
                "sum:Sales:qk", "cols", major_show="false", major_spacing="3", major_units="months"
            ),
        )
    )
    ir = parse(tmp_path, hidden)
    (axis,) = ir.worksheets[0].axes
    assert (axis.ticks, axis.tick_spacing) == (False, None)
    x = main_encoding(render(ir, db).spec)["x"]  # type: ignore[index]
    assert (x["axis"]["labels"], x["axis"]["ticks"]) == (False, False)
    assert "values" not in x["axis"]


# ---------------------------------------------------------------- 離散欄位的標題


def nested(shelf: str, *rules: str, mark: str = "Bar") -> str:
    """Region、Category 兩層的離散標題在 shelf（rows／cols）上，另一架為 SUM(Sales)（或文字表）。"""
    dims = f"{ref('none:Region:nk')} / {ref('none:Category:nk')}"
    other = ref("sum:Sales:qk") if mark != "Text" else ""
    shelves = {"rows": dims, "cols": other} if shelf == "rows" else {"rows": other, "cols": dims}
    encodings = f"<text column='{ref('sum:Sales:qk')}' />" if mark == "Text" else ""
    sheet = worksheet(
        "Nested", [REGION, CATEGORY, SALES], mark=mark, encodings=encodings, **shelves
    )
    return styled(sheet, *rules)


def test_hidden_header_levels(tmp_path: Path, db: Path) -> None:
    """「顯示標題」關閉：只顯示其他層的成員與欄位標籤；全部關閉時不顯示標題。"""
    hide = rule("label", fmt("display", "false", "none:Category:nk"))
    ir = parse(tmp_path, nested("rows", hide))
    assert ir.unsupported == []
    assert [(a.use.field, a.shelf, a.show) for a in ir.worksheets[0].axes] == [
        ("f_category", "rows", False)
    ]
    spec = render(ir, db).spec
    y = main_encoding(spec)["y"]  # type: ignore[index]
    assert y["field"] == "rows_dim"  # 位置仍依兩層區分
    # 只畫 Region 一欄（各組的第一列），欄位標籤寫在欄的上方
    headers = {
        layer["name"]: layer
        for layer in spec["layer"]
        if layer.get("name", "").startswith("__deco__header")
    }
    assert set(headers) == {"__deco__header0", "__deco__header0_title"}
    assert [v["__header0"] for v in spec["data"]["values"]] == [
        "East",
        "",
        "North",
        "South",
        "",
        "West",
        "",
    ]
    assert headers["__deco__header0_title"]["encoding"]["text"] == {"value": "Region"}
    assert "__header1" not in spec["data"]["values"][0]
    assert (y["axis"]["labels"], y["axis"]["title"]) == (False, None)

    both = rule(
        "label",
        fmt("display", "false", "none:Region:nk"),
        fmt("display", "false", "none:Category:nk"),
    )
    y = render(parse(tmp_path, nested("rows", both)), db).spec["encoding"]["y"]
    shown = {k: y["axis"][k] for k in ("labels", "ticks", "domain", "title")}
    assert shown == {"labels": False, "ticks": False, "domain": False, "title": None}
    assert y["axis"]["grid"] is True  # 窗格的分隔線照畫（Tableau 的預設）


def test_single_row_header(tmp_path: Path, db: Path) -> None:
    """單層的列標題與 Tableau 相同畫成一欄：成員靠左、不畫刻度，欄位標籤橫寫在欄的上方；寬度依內容
    （含欄位標籤）估計；成員的對齊（text-align）照設定。"""
    y = render(parse(tmp_path, bars()), db).spec["encoding"]["y"]
    axis = y["axis"]
    width = axis["minExtent"]
    assert axis["maxExtent"] == width and 32 <= width <= 80
    assert (axis["labelAlign"], axis["labelPadding"], axis["ticks"]) == ("left", width - 4, False)
    assert axis["labelLimit"] == width - 8
    assert (axis["title"], axis["titleAngle"], axis["titleAlign"]) == ("Region", 0, "left")
    assert (axis["titleX"], axis["titleY"], axis["titleBaseline"]) == (-width + 4, -4, "bottom")

    for align, padding in (("center", width / 2), ("right", 4)):
        aligned = bars(rule("label", fmt("text-align", align, "none:Region:nk")))
        ir = parse(tmp_path, aligned)
        assert [a.align for a in ir.worksheets[0].axes] == [align]
        axis = render(ir, db).spec["encoding"]["y"]["axis"]
        assert (axis["labelAlign"], axis["labelPadding"]) == (align, padding)
    auto = parse(tmp_path, bars(rule("label", fmt("text-align", "auto", "none:Region:nk"))))
    assert auto.worksheets[0].axes == []  # 預設（靠左）


def test_hidden_header_levels_in_text_tables(tmp_path: Path, db: Path) -> None:
    """文字表的多層欄標題每一層一行（外層在上），外層的成員合併成一格、寫在各組的中間；隱藏的層不佔一行。"""
    hide = rule("label", fmt("display", "false", "none:Category:nk"))
    spec = render(parse(tmp_path, nested("cols", hide, mark="Text")), db).spec
    axis = main_encoding(spec)["x"]["axis"]  # type: ignore[index]
    assert (axis["labels"], axis["minExtent"], axis["title"]) == (False, 15 + 4, "Region")
    names = {layer.get("name") for layer in spec["layer"]}
    assert "__deco__colheader0" in names and "__deco__colheader1" not in names

    spec = render(parse(tmp_path, nested("cols", mark="Text")), db).spec
    assert main_encoding(spec)["x"]["axis"]["minExtent"] == 2 * 15 + 4  # type: ignore[index]
    values = spec["data"]["values"]
    columns = list(dict.fromkeys(v["cols_dim"] for v in values))
    assert columns[:2] == ["East｜Furniture", "East｜Technology"]
    # East 有兩欄：寫在兩欄之間（第一欄的尾端）；每一欄只寫一次
    east = [v for v in values if v["cols_dim"] == "East｜Furniture"]
    assert [v["__colheader0_mid"] for v in east].count("East") == 1
    north = [v for v in values if v["cols_dim"] == "North｜Office"]
    assert [v["__colheader0"] for v in north].count("North") == 1
    mid = next(layer for layer in spec["layer"] if layer.get("name") == "__deco__colheader0_mid")
    assert mid["encoding"]["x"]["bandPosition"] == 1
    assert mid["mark"]["y"] == -(4 + 15)  # 外層在最內層的上面一行


# ---------------------------------------------------------------- 文字表的對齊與欄位標籤


def crosstab(*rules: str, cols: bool = True, rows: bool = True, pane: str = "") -> str:
    """Region 為列、Category 為欄（cols／rows=False 時沒有）、SUM(Sales) 為文字的文字表。"""
    sheet = worksheet(
        "Crosstab",
        [REGION, CATEGORY, SALES],
        rows=ref("none:Region:nk") if rows else "",
        cols=ref("none:Category:nk") if cols else "",
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=pane,
    )
    return styled(sheet, *rules)


def pane_align(value: str) -> str:
    """標記卡的 Format > 窗格的對齊（寫在 <pane> 的 <style> 中）。"""
    return (
        "<style><style-rule element='cell'>"
        f"<format attr='text-align' value='{value}' /></style-rule></style>"
    )


def test_cell_alignment(tmp_path: Path, db: Path) -> None:
    """文字表的儲存格與 Tableau 相同數字靠右（對齊各欄的右緣）；設定的對齊（Format > 窗格）優先；
    沒有欄時對齊整張圖的右緣。標題沒有刻度。"""
    ir = parse(tmp_path, crosstab())
    assert ir.worksheets[0].cell_align is None
    spec = render(ir, db).spec
    x = spec["encoding"]["x"]
    assert (spec["mark"]["align"], spec["mark"]["dx"]) == ("right", -4)
    assert x["bandPosition"] == 1
    assert x["scale"] == {"type": "band", "paddingInner": 0, "paddingOuter": 0}
    assert x["axis"]["ticks"] is False and spec["encoding"]["y"]["axis"]["ticks"] is False

    left = parse(tmp_path, crosstab(rule("cell", fmt("text-align", "left"))))
    assert left.worksheets[0].cell_align == "left"
    spec = render(left, db).spec
    assert (spec["mark"]["align"], spec["mark"]["dx"]) == ("left", 4)
    assert spec["encoding"]["x"]["bandPosition"] == 0

    center = render(parse(tmp_path, crosstab(rule("cell", fmt("text-align", "center")))), db).spec
    assert "align" not in center["mark"] and "bandPosition" not in center["encoding"]["x"]

    single = render(parse(tmp_path, crosstab(cols=False)), db).spec
    assert single["mark"]["x"] == {"expr": "width"}
    assert single["mark"]["align"] == "right"

    # 標記卡的對齊（Snow Sports 報表的寫法）優先於工作表的
    ir = parse(
        tmp_path, crosstab(rule("cell", fmt("text-align", "left")), pane=pane_align("center"))
    )
    assert ir.worksheets[0].marks.cell_align == "center"
    assert "align" not in render(ir, db).spec["mark"]
    # 只有一格（KPI）：沒有設定時置中，設定了靠左時對齊左緣
    kpi = render(parse(tmp_path, crosstab(cols=False, rows=False)), db).spec
    assert "align" not in kpi["mark"] and "x" not in kpi["mark"]
    kpi = render(
        parse(tmp_path, crosstab(cols=False, rows=False, pane=pane_align("left"))), db
    ).spec
    assert (kpi["mark"]["align"], kpi["mark"]["x"], kpi["mark"]["dx"]) == ("left", 0, 4)


def test_field_label_background(tmp_path: Path, db: Path) -> None:
    """欄位標籤的背景（Snow Sports 報表的表格）：列標題上方一列、橫跨標題的欄，畫在最下層。"""
    ir = parse(tmp_path, crosstab(rule("field-labels", fmt("background-color", "#F3EDE7"))))
    assert ir.worksheets[0].field_label_background == "#f3ede7"
    spec = render(ir, db).spec
    background, body = spec["layer"]
    width = body["encoding"]["y"]["axis"]["minExtent"]
    assert background["mark"] == {
        "type": "rect",
        "color": "#f3ede7",
        "x": -width,
        "x2": 0,
        "y": -20,
        "y2": 0,
    }
    assert body["encoding"]["y"]["axis"]["zindex"] == 1  # 欄位標籤畫在背景之上
    # 不顯示欄位標籤時不畫；透明的背景不算
    hidden = crosstab(
        rule("field-labels", fmt("background-color", "#F3EDE7")),
        rule("worksheet", fmt("display-field-labels", "false", scope="rows")),
    )
    assert "layer" not in render(parse(tmp_path, hidden), db).spec
    clear = crosstab(rule("field-labels", fmt("background-color", "#00000000")))
    assert parse(tmp_path, clear).worksheets[0].field_label_background is None


# ---------------------------------------------------------------- 文字顏色


def test_text_colors(tmp_path: Path, db: Path) -> None:
    """文字表的儲存格（cell）、標記標籤（標記卡的 datalabel）與活頁簿的預設文字顏色。"""
    table = worksheet(
        "Table",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    spec = render(parse(tmp_path, styled(table, rule("cell", fmt("color", "#123456")))), db).spec
    assert spec["mark"]["color"] == "#123456"

    labelled = worksheet(
        "Labels",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=(
            "<style><style-rule element='datalabel'>"
            "<format attr='color' value='#AA0000' /></style-rule></style>"
        ),
    )
    xml = b.workbook(
        b.extract_datasource(),
        worksheets=labelled,
        extra=(
            "<style><style-rule element='all'><format attr='color' value='#333333' />"
            "</style-rule></style>"
        ),
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    ws = ir.worksheets[0]
    assert (ws.marks.label_color, ws.text_color) == ("#aa0000", "#333333")
    spec = render(ir, db).spec
    _, label = spec["layer"]
    assert label["encoding"]["color"] == {"value": "#aa0000"}
    assert spec["config"]["axis"]["labelColor"] == "#333333"


def test_workbook_line_defaults(tmp_path: Path) -> None:
    """活頁簿的格式（Format > Workbook）不畫格線：每張工作表的預設，工作表可以再打開。"""
    workbook_style = (
        "<style><style-rule element='gridline'><format attr='line-visibility' value='off' />"
        "</style-rule><style-rule element='zeroline'><format attr='line-visibility' value='off' />"
        "</style-rule></style>"
    )
    plain = bars()
    reopened = bars(rule("gridline", fmt("line-visibility", "on", scope="cols")))
    xml = b.workbook(
        b.extract_datasource(),
        worksheets=plain + reopened.replace("name='Bars'", "name='Bars 2'"),
        extra=workbook_style,
    )
    first, second = parse_workbook(b.write_twb(tmp_path, xml)).worksheets
    assert (first.row_style.grid, first.column_style.grid) == (False, False)
    assert (second.row_style.grid, second.column_style.grid) == (False, True)


# ---------------------------------------------------------------- 多邊形


def polygon(path: bool = True, stroke: bool = False) -> str:
    encodings = f"<lod column='{ref('none:Region:nk')}' />"
    if path:
        encodings += f"<path column='{ref('none:Category:nk')}' />"
    style = (
        "<style><style-rule element='mark'><format attr='mark-color' value='#FFFFFF' />"
        "<format attr='mark-transparency' value='65' />"
        f"<format attr='has-stroke' value='{'true' if stroke else 'false'}' />"
        "<format attr='stroke-color' value='#101E47' /></style-rule></style>"
    )
    return worksheet(
        "Poly",
        [REGION, CATEGORY, SALES, PROFIT],
        rows=ref("sum:Profit:qk"),
        cols=ref("sum:Sales:qk"),
        mark="Polygon",
        encodings=encodings,
        pane_extra=style,
    )


def test_polygons(tmp_path: Path, db: Path) -> None:
    """多邊形：依路徑的順序連接各點並填滿，每個區（詳細資料）一個多邊形；沒有路徑時依資料的順序。"""
    ir = parse(tmp_path, polygon())
    assert ir.unsupported == []
    assert ir.worksheets[0].marks.type == "polygon"
    rendered = render(ir, db)
    assert rendered.mark == "polygon"
    spec = rendered.spec
    mark: dict[str, Any] = spec["mark"]
    assert mark == {
        "type": "line",
        "tooltip": True,
        "filled": True,
        "color": "#ffffff",
        "opacity": round(65 / 255, 4),
    }
    encoding = spec["encoding"]
    names = {c["title"]: c["field"] for c in encoding["tooltip"]}
    assert encoding["order"] == {"field": names["Category"].removesuffix("_label")}
    assert {"field": names["Region"].removesuffix("_label"), "type": "nominal"} in encoding[
        "detail"
    ]
    # 各區的頂點：East 有 Furniture、Technology 兩點
    east = [v for v in spec["data"]["values"] if v[names["Region"]] == "East"]
    assert len(east) == 2

    spec = render(parse(tmp_path, polygon(path=False, stroke=True)), db).spec
    assert spec["mark"]["order"] is None and "order" not in spec["encoding"]
    assert (spec["mark"]["stroke"], spec["mark"]["strokeWidth"]) == ("#101e47", 1)


# ---------------------------------------------------------------- dashboard


def test_dashboard_background_and_zone_styles(tmp_path: Path) -> None:
    sheet = worksheet("S", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))

    def style(**formats: str) -> str:
        items = "".join(
            f"<format attr='{k.replace('_', '-')}' value='{v}' />" for k, v in formats.items()
        )
        return f"<zone-style>{items}</zone-style>"

    defaults = style(border_color="#000000", border_style="none", border_width="0", margin="4")
    board = f"""
    <dashboard name='Board'>
      <style><style-rule element='table'><format attr='background-color' value='#F3EDE7' />
      </style-rule></style>
      <size maxheight='800' maxwidth='1000' minheight='800' minwidth='1000' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          {style(border_color="#000000", border_style="none", border_width="0", margin="0")}
          <zone h='50000' id='2' param='vert' type-v2='layout-flow' w='50000' x='0' y='0'>
            {style(border_color="#BD8459", border_style="solid", border_width="2", margin="8", padding="10")}
            <zone h='40000' id='3' name='S' w='40000' x='1250' y='2500'>{defaults}</zone>
          </zone>
          <zone h='10000' id='4' type-v2='text' w='10000' x='60000' y='0'>
            {style(margin="0", margin_top="6", padding_left="15", background_color="#FFFFFF")}
          </zone>
          <zone h='10000' id='5' type-v2='empty' w='10000' x='60000' y='20000'>
            {style(border_style="solid", border_width="0", border_color="#FF0000")}
          </zone>
        </zone>
        <zone h='10000' id='6' type-v2='empty' w='10000' x='80000' y='80000' />
      </zones>
    </dashboard>"""
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet, dashboards=board)))
    dashboard = ir.dashboards[0]
    assert dashboard.background == "#f3ede7"
    zones = {z.id: z.style for z in iter_zones(dashboard.zones)}
    assert zones["z1"] is None  # 外距 0、沒有框線：與沒有寫相同
    assert zones["z2"] == ZoneStyle(
        border="solid",
        border_width=2,
        border_color="#bd8459",
        margin=(8, 8, 8, 8),
        padding=(10, 10, 10, 10),
    )
    assert zones["z3"] == ZoneStyle(margin=(4, 4, 4, 4))
    assert zones["z4"] == ZoneStyle(
        background="#ffffff", margin=(6, 0, 0, 0), padding=(0, 0, 0, 15)
    )
    assert zones["z5"] is None  # 寬度 0 的框線不畫
    assert zones["z6"] is None  # 浮動的區塊沒有寫：沒有外距


# ---------------------------------------------------------------- 零線、標籤顏色、標題的位置與方向


def test_zero_lines(tmp_path: Path, db: Path) -> None:
    """零線：沒有設定時與 Tableau 相同畫 1 像素的灰色點線（0 的格線）；關閉、改成實線；格線關閉時只畫零線。"""
    x = render(parse(tmp_path, bars()), db).spec["encoding"]["x"]["axis"]
    zero = "datum.value === 0"
    assert x["gridColor"] == {"condition": {"test": zero, "value": "#cbcbcb"}, "value": "#f0f0f0"}
    assert x["gridDash"] == {"condition": {"test": zero, "value": [2, 1]}, "value": []}
    off = bars(rule("zeroline", fmt("line-visibility", "off")))
    assert "gridDash" not in render(parse(tmp_path, off), db).spec["encoding"]["x"].get("axis", {})
    solid = bars(
        rule(
            "zeroline",
            fmt("line-pattern-only", "solid", scope="cols"),
            fmt("stroke-color", "#000000"),
        ),
        rule("gridline", fmt("line-visibility", "off")),
    )
    ws = parse(tmp_path, solid).worksheets[0]
    assert ws.column_style.zero_line is not None and ws.column_style.zero_line.pattern == "solid"
    x = render(parse(tmp_path, solid), db).spec["encoding"]["x"]["axis"]
    assert x["gridDash"]["condition"]["value"] == []
    assert x["gridOpacity"] == {"condition": {"test": zero, "value": 1}, "value": 0}


def test_label_color_matches_marks(tmp_path: Path, db: Path) -> None:
    """標記標籤「與標記顏色相符」：依欄位著色時沿用色彩的 encoding，否則為標記的顏色。"""
    style = (
        "<style><style-rule element='datalabel'><format attr='color-mode' value='match' />"
        "</style-rule></style>"
    )
    sheet = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=style,
    )
    ir = parse(tmp_path, sheet)
    assert ir.worksheets[0].marks.label_match is True
    label = render(ir, db).spec["layer"][1]
    assert label["encoding"]["color"] == {"value": "#4e79a7"}
    colored = sheet.replace(
        f"<text column='{ref('sum:Sales:qk')}' />",
        f"<color column='{ref('none:Region:nk')}' /><text column='{ref('sum:Sales:qk')}' />",
    )
    spec = render(parse(tmp_path, colored), db).spec
    assert spec["layer"][1]["encoding"]["color"] == spec["encoding"]["color"]


def test_header_vertical_alignment_and_orientation(tmp_path: Path, db: Path) -> None:
    """外層的標題成員置中（奇數列的組在中間那一列，偶數列的組畫在中間兩列之間）、置底；直寫的標題。"""
    centered = nested("rows", rule("label", fmt("vertical-align", "center")))
    spec = render(parse(tmp_path, centered), db).spec
    values = spec["data"]["values"]
    # East、South、West 各兩列（中間兩列之間），North 一列
    assert [v["__header0"] for v in values] == ["", "", "North", "", "", "", ""]
    assert [v["__header0_mid"] for v in values] == ["East", "", "", "South", "", "West", ""]
    mid = next(layer for layer in spec["layer"] if layer.get("name") == "__deco__header0_mid")
    assert mid["encoding"]["y"]["bandPosition"] == 1
    bottom = nested("rows", rule("label", fmt("vertical-align", "bottom", "none:Region:nk")))
    values = render(parse(tmp_path, bottom), db).spec["data"]["values"]
    assert [v["__header0"] for v in values] == ["", "East", "North", "", "South", "", "West"]
    rotated = bars(rule("label", fmt("text-orientation", "-90", "none:Region:nk")))
    ir = parse(tmp_path, rotated)
    assert [a.angle for a in ir.worksheets[0].axes] == [-90]
    axis = render(ir, db).spec["encoding"]["y"]["axis"]
    assert (axis["labelAngle"], axis["labelAlign"]) == (-90, "center")
    assert axis["minExtent"] < 40  # 一行字的寬度


def test_step_and_jump_lines(tmp_path: Path, db: Path) -> None:
    """線條類型（標記卡「路徑」）：階梯是 step 內插（量值在橫軸時先垂直），跳躍只畫到下一個點的水平線。"""

    def line(kind: str, across: bool = True, color: bool = False) -> str:
        measure, date = ref("sum:Sales:qk"), ref("tmn:Order Date:qk")
        return worksheet(
            "Trend",
            [MONTH, SALES, REGION],
            rows=measure if across else date,
            cols=date if across else measure,
            mark="Line",
            encodings=f"<color column='{ref('none:Region:nk')}' />" if color else "",
            pane_extra="<style><style-rule element='mark'>"
            f"<format attr='line-interpolation' value='{kind}' /></style-rule></style>",
        )

    for n, (kind, across, expected) in enumerate(
        [("step", True, "step-after"), ("step", False, "step-before"), ("linear", True, None)]
    ):
        (tmp_path / str(n)).mkdir()
        ir = parse(tmp_path / str(n), line(kind, across))
        assert ir.unsupported == []
        assert ir.worksheets[0].marks.line_type == kind
        spec = render(ir, db).spec
        assert spec["mark"].get("interpolate") == expected

    (tmp_path / "jump").mkdir()
    ir = parse(tmp_path / "jump", line("jump", color=True))
    assert ir.worksheets[0].marks.line_type == "jump"
    spec = render(ir, db).spec
    segments, points = spec["layer"]
    assert segments["mark"]["type"] == "rule" and points["mark"]["type"] == "point"
    window, valid = segments["transform"]
    x = spec["encoding"]["x"]["field"]
    assert window["window"] == [{"op": "lead", "field": x, "as": "__next"}]
    assert window["groupby"] == [spec["encoding"]["color"]["field"]]  # 各條線各自找下一個點
    assert window["sort"] == [{"field": x}]
    assert segments["encoding"] == {"x2": {"field": "__next"}}
    assert "__next" in valid["filter"]


def test_shape_palette(tmp_path: Path, db: Path) -> None:
    """形狀的調色盤（編輯形狀）：指定的形狀優先，其餘的成員依 Tableau 預設調色盤的順序；成員依值排序。"""
    region = ref("none:Region:nk")
    sheet = worksheet(
        "Shapes",
        [REGION, CATEGORY, SALES, PROFIT],
        rows=ref("sum:Sales:qk"),
        cols=ref("sum:Profit:qk"),
        mark="Shape",
        encodings=f"<shape column='{region}' /><lod column='{ref('none:Category:nk')}' />",
    )
    sheet = sheet.replace(
        "<style />",
        "<style><style-rule element='mark'>"
        f"<encoding attr='shape' field='{region}' type='shape'>"
        "<map to='diamond'><bucket>&quot;West&quot;</bucket></map>"
        "<map to=':filled/star'><bucket>&quot;East&quot;</bucket></map>"
        "<map to=':Arrows/up'><bucket>&quot;North&quot;</bucket></map>"
        "</encoding></style-rule></style>",
        1,
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    marks = ir.worksheets[0].marks
    assert [(a.value, a.shape) for a in marks.shape_assignments] == [
        ("West", "diamond"),
        ("East", "asterisk"),
    ]
    assert any(":Arrows/up" in item.reason for item in ir.ignored)
    scale = render(ir, db).spec["encoding"]["shape"]["scale"]
    assert scale["domain"] == ["East", "North", "South", "West"]
    east, north, south, west = scale["range"]
    assert east.startswith("M0,-1")  # 星號以路徑畫出
    assert (north, south, west) == ("square", "cross", "diamond")  # 其餘依預設的順序：方形、加號
