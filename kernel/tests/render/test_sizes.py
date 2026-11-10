"""工作表的顯示大小（標準、符合寬度、符合高度、整個檢視）、列高與欄寬、標題的寬度、列與欄的色帶與
分隔線：解析與呈現。

寫法取自公開報表（Snow Sports、Logshark、HR Dashboards 等）；標準大小的列高 20 像素以 Logshark 報表的
Tableau 畫面量測。測試資料見 tests/sqlgen/test_query.py：區與類別的組合依序為 East｜Furniture、
East｜Technology、North｜Office、South｜Furniture、South｜Office、West｜Furniture、West｜Technology。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tmig.ir import Banding, Divider, iter_zones
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import ROW_HEIGHT, apply_fit, main_encoding

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_formats import MONTH, bars, fmt, nested, rule, styled
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, parse, render

__all__ = ["db"]  # pytest fixture

NESTED = [
    "East｜Furniture",
    "East｜Technology",
    "North｜Office",
    "South｜Furniture",
    "South｜Office",
    "West｜Furniture",
    "West｜Technology",
]


def window(kind: str, name: str, zoom: str | None = None, points: str = "") -> str:
    viewpoint = (
        f"<viewpoint><zoom type='{zoom}' /></viewpoint>" if kind == "worksheet" and zoom else ""
    )
    if kind == "dashboard":
        viewpoint = f"<viewpoints>{points}</viewpoints>"
    return f"<window class='{kind}' name='{name}'>{viewpoint}</window>"


def point(sheet: str, zoom: str | None) -> str:
    inner = f"<zoom type='{zoom}' />" if zoom else ""
    return f"<viewpoint name='{sheet}'>{inner}</viewpoint>"


def test_fit_modes_come_from_windows(tmp_path: Path) -> None:
    """工作表視窗與 dashboard 視窗中各工作表的顯示大小；dashboard 中沒有 <viewpoint> 的依工作表本身。"""
    sheets = "".join(
        worksheet(name, [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
        for name in ("A", "B", "C", "D")
    )
    zones = "".join(
        f"<zone h='25000' id='{i + 2}' name='{name}' w='100000' x='0' y='{i * 25000}' />"
        for i, name in enumerate("ABCD")
    )
    board = (
        "<dashboard name='Board'><size maxheight='800' maxwidth='1000' minheight='800' "
        "minwidth='1000' /><zones><zone h='100000' id='1' type-v2='layout-basic' w='100000' "
        f"x='0' y='0'>{zones}</zone></zones></dashboard>"
    )
    windows = (
        "<windows>"
        + window("worksheet", "A", "entire-view")
        + window("worksheet", "B", "fit-width")
        + window("worksheet", "C")
        + window("worksheet", "D", "fit-height")
        + window(
            "dashboard",
            "Board",
            points=point("A", None) + point("B", "fit-height") + point("C", "entire-view"),
        )
        + "</windows>"
    )
    xml = b.workbook(worksheets=sheets, dashboards=board, extra=windows)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert [w.fit for w in ir.worksheets] == ["entire_view", "fit_width", "standard", "fit_height"]
    fits = {z.worksheet: z.fit for z in iter_zones(ir.dashboards[0].zones) if z.worksheet}
    # 沒有 zoom 的 viewpoint 是標準；沒有 viewpoint 的（D）依工作表本身
    assert fits == {"ws_a": "standard", "ws_b": "fit_height", "ws_c": "entire_view", "ws_d": None}


def test_standard_rows_keep_their_height(tmp_path: Path, db: Path) -> None:
    """標準大小：離散的列每一列 20 像素（超出區塊時捲動），連續的座標軸填滿寬度。"""
    rendered = render(parse(tmp_path, bars()), db)
    spec = rendered.spec
    assert spec["usermeta"]["fit"] == "standard"
    assert spec["usermeta"]["sizing"] == {"rows": {"step": ROW_HEIGHT}, "columns": {}}
    assert (spec["width"], spec["height"]) == ("container", {"step": 20})
    assert spec["autosize"] == {"type": "fit-x", "contains": "padding"}

    # dashboard 區塊的設定不同時，檢視器以同樣的規則重算
    fills = copy.deepcopy(spec)
    apply_fit(fills, "entire_view")
    assert (fills["width"], fills["height"], fills["autosize"]["type"]) == (
        "container",
        "container",
        "fit",
    )
    apply_fit(fills, "fit_width")
    assert fills["height"] == {"step": 20}
    apply_fit(fills, "fit_height")
    assert fills["height"] == "container"


def test_cell_sizes(tmp_path: Path, db: Path) -> None:
    """Format > Cell Size：整張表的列高、離散欄位成員的大小（優先），連續座標軸的大小。"""
    sheet = bars(rule("cell", fmt("height", "31")))
    assert render(parse(tmp_path, sheet), db).spec["height"] == {"step": 31}

    sheet = bars(rule("cell", fmt("height", "31"), fmt("height", "44", "none:Region:nk")))
    ir = parse(tmp_path, sheet)
    assert [(a.use.field, a.shelf, a.cell) for a in ir.worksheets[0].axes] == [
        ("f_region", "rows", 44)
    ]
    assert render(ir, db).spec["height"] == {"step": 44}

    # 欄的寬度（連續座標軸，標準大小時固定這個寬度）
    spec = render(parse(tmp_path, bars(rule("cell", fmt("width", "300")))), db).spec
    assert spec["usermeta"]["sizing"]["columns"] == {"size": 300}
    assert spec["width"] == 300
    assert spec["autosize"]["type"] == "pad"


def test_discrete_columns_have_a_natural_width(tmp_path: Path, db: Path) -> None:
    """離散的欄依內容估計寬度：文字表與 Tableau 相同固定這個寬度（標準大小時不拉寬填滿）；其他圖在
    區塊放不下時才固定、可以捲動（檢視器）；設定了寬度時一律固定。"""
    table = worksheet(
        "Table",
        [REGION, SALES],
        cols=ref("none:Region:nk"),
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    spec = render(parse(tmp_path, table), db).spec
    columns = spec["usermeta"]["sizing"]["columns"]
    assert (columns["count"], columns["fixed"]) == (4, True)
    assert 40 <= columns["step"] <= 80
    assert spec["width"] == {"step": columns["step"]}

    vertical = worksheet(
        "Bars", [REGION, SALES], cols=ref("none:Region:nk"), rows=ref("sum:Sales:qk")
    )
    spec = render(parse(tmp_path, vertical), db).spec
    columns = spec["usermeta"]["sizing"]["columns"]
    assert (columns["count"], columns["fixed"]) == (4, False)
    assert spec["width"] == "container"

    sized = styled(table, rule("cell", fmt("width", "120", "none:Region:nk")))
    spec = render(parse(tmp_path, sized), db).spec
    assert spec["usermeta"]["sizing"]["columns"] == {"step": 120, "count": 4, "fixed": True}
    assert spec["width"] == {"step": 120}


def test_header_width(tmp_path: Path, db: Path) -> None:
    """拖曳調整過的列標題寬度：標題區域固定這個寬度，較長的成員截斷。"""
    sheet = bars(rule("header", fmt("width", "108", "none:Region:nk")))
    ir = parse(tmp_path, sheet)
    assert [(a.shelf, a.size) for a in ir.worksheets[0].axes] == [("rows", 108)]
    axis = render(ir, db).spec["encoding"]["y"]["axis"]
    assert (axis["minExtent"], axis["maxExtent"], axis["labelLimit"]) == (108, 108, 100)

    # 多層的標題：每一層一欄、依設定的寬度，合計為標題區域的寬度；連續座標軸的寬度
    both = nested(
        "rows",
        rule(
            "header", fmt("width", "60", "none:Region:nk"), fmt("width", "80", "none:Category:nk")
        ),
    )
    spec = render(parse(tmp_path, both), db).spec
    axis = main_encoding(spec)["y"]["axis"]  # type: ignore[index]
    assert (axis["minExtent"], axis["maxExtent"]) == (140, 140)
    headers = header_layers(spec)
    assert [(h["encoding"]["x"], h["mark"]["limit"]) for h in headers.values()] == [
        ({"value": -136}, 52),
        ({"value": -76}, 72),
    ]
    axis_width = bars(rule("axis", fmt("height", "36", "sum:Sales:qk")))
    x = render(parse(tmp_path, axis_width), db).spec["encoding"]["x"]["axis"]
    assert (x["minExtent"], x["maxExtent"]) == (36, 36)


def banded(*rules: str) -> str:
    return nested("rows", *rules)


def header_layers(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """多層列標題的圖層（不含欄位標籤），依層的順序。"""
    found: dict[str, dict[str, Any]] = {}

    def visit(node: dict[str, Any]) -> None:
        name = node.get("name", "")
        if name.startswith("__deco__header") and not name.endswith("_title"):
            found[name] = node
        for child in node.get("layer", []):
            visit(child)

    visit(spec)
    return found


def test_multi_level_row_headers(tmp_path: Path, db: Path) -> None:
    """多層的列標題每一層一欄：外層的成員只寫在各組的第一列，寬度依內容估計；縱軸不再畫標籤。"""
    spec = render(parse(tmp_path, banded()), db).spec
    values = spec["data"]["values"]
    assert [v["__header0"] for v in values] == ["East", "", "North", "South", "", "West", ""]
    assert [v["__header1"] for v in values] == [n.split("｜")[1] for n in NESTED]
    headers = header_layers(spec)
    assert list(headers) == ["__deco__header0", "__deco__header1"]
    for level, layer in enumerate(headers.values()):
        assert layer["encoding"]["text"] == {"field": f"__header{level}"}
        assert layer["encoding"]["y"]["field"] == "rows_dim"
        assert layer["transform"] == [{"calculate": "-1", "as": "__row"}]  # 不能選取
        assert (layer["mark"]["type"], layer["mark"]["align"]) == ("text", "left")
    y = main_encoding(spec)["y"]  # type: ignore[index]
    total = y["axis"]["minExtent"]
    first = headers["__deco__header0"]["encoding"]["x"]["value"]
    second = headers["__deco__header1"]["encoding"]["x"]["value"]
    assert first == -total + 4 and first < second < 0
    assert (y["axis"]["labels"], y["axis"]["ticks"], y["axis"]["title"]) == (False, False, None)
    titles = [layer for layer in spec["layer"] if layer.get("name", "").endswith("_title")]
    assert [t["encoding"]["text"]["value"] for t in titles] == ["Region", "Category"]

    # 成員的對齊（Snow Sports 報表的 Insight Date、Resort 置中）
    centered = banded(rule("label", fmt("text-align", "center", "none:Region:nk")))
    spec = render(parse(tmp_path, centered), db).spec
    region = header_layers(spec)["__deco__header0"]
    assert region["mark"]["align"] == "center"
    assert region["encoding"]["x"] == {"value": -total + (second - first) / 2}  # 欄的中間
    assert header_layers(spec)["__deco__header1"]["mark"]["align"] == "left"


DIVIDER_RULES = rule(
    "table-div",
    fmt("line-visibility", "on", scope="rows"),
    fmt("stroke-color", "#BD8459", scope="rows"),
    fmt("stroke-size", "2", scope="rows"),
    fmt("div-level", "1", scope="rows"),
    fmt("line-visibility", "off", scope="cols"),
)
BANDING_RULES = (
    rule("table", fmt("band-size", "1", scope="rows"), fmt("band-size", "0", scope="cols")),
    rule(
        "pane",
        fmt("band-color", "#F4EEE8", scope="rows"),
        fmt("band-color", "#eeeeee", scope="cols"),
    ),
    rule(
        "header",
        fmt("band-color", "#F3EDE7", scope="rows"),
        fmt("width", "60", "none:Region:nk"),
        fmt("width", "80", "none:Category:nk"),
    ),
)


def test_banding_and_dividers_are_parsed(tmp_path: Path) -> None:
    ir = parse(tmp_path, banded(DIVIDER_RULES, *BANDING_RULES))
    ws = ir.worksheets[0]
    assert ws.row_style.divider == Divider(color="#bd8459", width=2, level=1)
    assert ws.row_style.banding == Banding(pane="#f4eee8", header="#f3ede7", size=1)
    # 欄：分隔線關閉、色帶的列數為 0
    assert (ws.column_style.divider, ws.column_style.banding) == (None, None)

    # 只寫了顏色（沒有關閉）也畫；關閉、寬度 0、第 0 層都不畫；透明的色帶不畫
    color_only = rule("table-div", fmt("stroke-color", "#123456", scope="rows"))
    assert parse(tmp_path, banded(color_only)).worksheets[0].row_style.divider == Divider(
        color="#123456"
    )
    for off in ("line-visibility='off'", "stroke-size='0'", "div-level='0'"):
        attr, value = off.split("=")
        rules = rule(
            "table-div",
            fmt("stroke-color", "#123456", scope="rows"),
            fmt(attr, value.strip("'"), scope="rows"),
        )
        assert parse(tmp_path, banded(rules)).worksheets[0].row_style.divider is None, off
    clear = rule("pane", fmt("band-color", "#00000000", scope="rows"))
    assert parse(tmp_path, banded(clear)).worksheets[0].row_style.banding is None


def test_default_dividers_are_parsed(tmp_path: Path) -> None:
    """沒有寫分隔線時與 Tableau 相同：窗格（table-div）與標題（header-div）都畫，淺灰、最內層以外的
    每一層；兩者可以分開關閉，標題的線沒有寫顏色時與窗格相同，層數共用窗格的 div-level。"""
    style = parse(tmp_path, banded()).worksheets[0].row_style
    assert (style.divider, style.header_divider) == (Divider(), Divider())
    assert Divider().level is None

    header_off = rule("header-div", fmt("line-visibility", "off", scope="rows"))
    style = parse(tmp_path, banded(header_off)).worksheets[0].row_style
    assert (style.divider, style.header_divider) == (Divider(), None)

    colored = (
        rule(
            "table-div",
            fmt("stroke-color", "#BD8459", scope="rows"),
            fmt("div-level", "2", scope="rows"),
            fmt("line-visibility", "off", scope="cols"),
        ),
        rule("header-div", fmt("stroke-size", "2", scope="rows")),
    )
    ws = parse(tmp_path, banded(*colored)).worksheets[0]
    assert ws.row_style.divider == Divider(color="#bd8459", level=2)
    assert ws.row_style.header_divider == Divider(color="#bd8459", width=2, level=2)
    # 欄：只關掉窗格的線
    assert (ws.column_style.divider, ws.column_style.header_divider) == (None, Divider())

    zero = rule("table-div", fmt("div-level", "0", scope="rows"))
    style = parse(tmp_path, banded(zero)).worksheets[0].row_style
    assert (style.divider, style.header_divider) == (None, None)


def test_dividers_are_rendered(tmp_path: Path, db: Path) -> None:
    """分隔線畫在第 level 層成員改變處（Region 的各組之間）；level 大於層數時每一列之間。"""
    spec = render(parse(tmp_path, banded(DIVIDER_RULES)), db).spec
    y = main_encoding(spec)["y"]  # type: ignore[index]
    assert [v["rows_dim"] for v in spec["data"]["values"]] == NESTED
    axis = y["axis"]
    assert (axis["grid"], axis["tickBand"], axis["gridColor"], axis["gridWidth"]) == (
        True,
        "extent",
        "#bd8459",
        2,
    )
    # 格線在各列的尾端：下一列屬於另一個 Region 的列
    edges = ["East｜Technology", "North｜Office", "South｜Office"]
    assert axis["gridOpacity"]["condition"]["test"] == (
        f"indexof({str(edges).replace(chr(39), chr(34))}, datum.value) >= 0"
    )
    assert y["scale"] == {"type": "band", "paddingInner": 0, "paddingOuter": 0}

    every = DIVIDER_RULES.replace("value='1' scope='rows'", "value='5' scope='rows'")
    axis = main_encoding(render(parse(tmp_path, banded(every)), db).spec)["y"]["axis"]  # type: ignore[index]
    assert axis["gridOpacity"]["condition"]["test"].count("｜") == len(NESTED) - 1


ORDER = instance("[Order ID]", "None", "[none:Order ID:nk]")


def three_levels(*rules: str) -> str:
    """Region、Category、Order ID 三層的列標題，欄為 SUM(Sales)。"""
    dims = " / ".join(ref(f"none:{name}:nk") for name in ("Region", "Category", "Order ID"))
    sheet = worksheet(
        "Three", [REGION, CATEGORY, ORDER, SALES], rows=dims, cols=ref("sum:Sales:qk")
    )
    return styled(sheet, *rules)


def test_default_dividers_are_rendered(tmp_path: Path, db: Path) -> None:
    """預設的分隔線（最內層以外的每一層）：窗格的線畫在 Region 或 Category 改變處；標題區域的線從
    改變的那一層所在的欄畫起（同一個 Region 中只切開 Category 以內的欄）；長條維持原本的粗細。"""
    spec = render(parse(tmp_path, three_levels()), db).spec
    values = spec["data"]["values"]
    assert [v["rows_dim"] for v in values] == [
        "East｜Furniture｜O1",
        "East｜Technology｜O2",
        "North｜Office｜O7",
        "South｜Furniture｜O5",
        "South｜Office｜O6",
        "West｜Furniture｜O3",
        "West｜Technology｜O4",
    ]
    main = spec["layer"][0]
    axis = main["encoding"]["y"]["axis"]
    assert axis["gridColor"] == "#d4d4d4"
    # 每一列都有不同的 Region｜Category：最後一列以外都是邊界
    edges = json.dumps([v["rows_dim"] for v in values][:-1], ensure_ascii=False)
    assert axis["gridOpacity"]["condition"]["test"] == f"indexof({edges}, datum.value) >= 0"
    assert main["mark"]["height"] == {"band": 0.75}

    headers = header_layers(spec)
    region, category = (headers[f"__deco__header{i}"]["encoding"]["x"]["value"] - 4 for i in (0, 1))
    assert region < category < 0
    assert [v["__divider_rows"] for v in values] == [
        category,  # East｜Furniture → East｜Technology：Region 的欄不切開
        region,
        region,
        category,
        region,
        category,
        0,  # 最後一列不是邊界
    ]
    [line] = [
        layer for layer in spec["layer"] if layer.get("name") == "__deco__divider_rows_header"
    ]
    assert line["mark"] == {"type": "rule", "color": "#d4d4d4", "strokeWidth": 1}
    assert line["encoding"]["y"]["bandPosition"] == 1  # 線在這一列的尾端
    assert line["encoding"]["x2"] == {"value": 0}

    # 兩層：分隔 Region；只有一層時預設不畫
    spec = render(parse(tmp_path, banded()), db).spec
    test = main_encoding(spec)["y"]["axis"]["gridOpacity"]["condition"]["test"]  # type: ignore[index]
    assert test == (
        'indexof(["East｜Technology", "North｜Office", "South｜Office"], datum.value) >= 0'
    )
    single = main_encoding(render(parse(tmp_path, bars()), db).spec)
    assert single is not None and "gridOpacity" not in single["y"].get("axis", {})

    # 關掉標題的線：只畫窗格的線
    off = rule("header-div", fmt("line-visibility", "off", scope="rows"))
    spec = render(parse(tmp_path, three_levels(off)), db).spec
    assert not any(layer.get("name", "").startswith("__deco__divider") for layer in spec["layer"])
    assert "gridOpacity" in main_encoding(spec)["y"]["axis"]  # type: ignore[index]


def test_banding_is_rendered(tmp_path: Path, db: Path) -> None:
    """色帶：隔列畫上窗格的顏色，列標題（寬度有設定時）畫上標題的顏色；長條維持原本的粗細。"""
    rendered = render(parse(tmp_path, banded(*BANDING_RULES)), db)
    spec: dict[str, Any] = rendered.spec
    values = spec["data"]["values"]
    assert [v["__band_rows"] for v in values] == [False, True, False, True, False, True, False]
    pane, header, body = spec["layer"]
    for layer in (pane, header):
        assert layer["name"].startswith("__deco")  # 檢視器的選取與醒目提示略過
        assert layer["mark"] == {"type": "rect"}
        assert layer["encoding"]["y"]["field"] == "rows_dim"
    assert pane["encoding"]["color"]["condition"]["value"] == "#f4eee8"
    assert header["encoding"]["color"]["condition"]["value"] == "#f3ede7"
    # 標題的色帶橫跨各層標題的欄（60 + 80）
    assert (header["encoding"]["x"], header["encoding"]["x2"]) == ({"value": -140}, {"value": 0})
    main = body["layer"][0]  # 主圖之後是多層列標題的圖層
    assert main["mark"]["height"] == {"band": 0.75}
    assert main["encoding"]["y"]["scale"] == {"type": "band", "paddingInner": 0, "paddingOuter": 0}


# ---------------------------------------------------------------- 標記的大小


def sized_mark(mark: str, size: str | None = None, rows: str = "", cols: str = "") -> str:
    """標記卡設定了「大小」（size）的工作表。"""
    style = (
        "<style><style-rule element='mark'>"
        f"<format attr='size' value='{size}' /></style-rule></style>"
        if size is not None
        else ""
    )
    return worksheet(
        "Sized",
        [REGION, SALES, PROFIT, MONTH],
        rows=rows or ref("none:Region:nk"),
        cols=cols or ref("sum:Sales:qk"),
        mark=mark,
        pane_extra=style,
    )


def test_mark_size_is_parsed(tmp_path: Path) -> None:
    assert parse(tmp_path, sized_mark("Bar")).worksheets[0].marks.mark_size is None
    marks = parse(tmp_path, sized_mark("Bar", "1.1424309015274048")).worksheets[0].marks
    assert marks.mark_size == pytest.approx(1.1424309)
    assert parse(tmp_path, sized_mark("Bar", "abc")).worksheets[0].marks.mark_size is None


def test_bar_thickness(tmp_path: Path, db: Path) -> None:
    """長條的粗細與 Tableau 相同：沒有設定大小時占列高的 75%，大小 s 時為 s / 2（最多填滿）；
    列之間沒有額外的空隙（比例以整列計算）。直條的寬度相同。"""
    spec = render(parse(tmp_path, sized_mark("Bar")), db).spec
    assert spec["mark"]["height"] == {"band": 0.75}
    assert spec["encoding"]["y"]["scale"] == {"type": "band", "paddingInner": 0, "paddingOuter": 0}
    for size, band in (("1", 0.5), ("1.1424309015274048", 0.5712), ("3", 1)):
        spec = render(parse(tmp_path, sized_mark("Bar", size)), db).spec
        assert spec["mark"]["height"] == {"band": band}, size
    vertical = sized_mark("Bar", "1", rows=ref("sum:Sales:qk"), cols=ref("none:Region:nk"))
    spec = render(parse(tmp_path, vertical), db).spec
    assert spec["mark"]["width"] == {"band": 0.5} and "height" not in spec["mark"]
    assert spec["encoding"]["x"]["scale"]["paddingInner"] == 0


def test_point_and_line_sizes(tmp_path: Path, db: Path) -> None:
    """圓點的直徑：預設 10 像素，大小 s 時為 s 倍（Vega 的 size 是外框的面積）；折線預設 3 像素寬。"""
    scatter = {"rows": ref("sum:Profit:qk"), "cols": ref("sum:Sales:qk")}
    spec = render(parse(tmp_path, sized_mark("Circle", **scatter)), db).spec
    assert spec["mark"]["size"] == 100
    spec = render(parse(tmp_path, sized_mark("Circle", "1.637", **scatter)), db).spec
    assert spec["mark"]["size"] == pytest.approx(16.37**2, abs=0.01)
    timeline = {"rows": ref("sum:Sales:qk"), "cols": ref("tmn:Order Date:qk")}
    spec = render(parse(tmp_path, sized_mark("Line", **timeline)), db).spec
    assert spec["mark"]["strokeWidth"] == 3
    spec = render(parse(tmp_path, sized_mark("Line", "2", **timeline)), db).spec
    assert spec["mark"]["strokeWidth"] == 6
