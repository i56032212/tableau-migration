"""工作表的顯示大小（標準、符合寬度、符合高度、整個檢視）、列高與欄寬、標題的寬度、列與欄的色帶與
分隔線：解析與呈現。

寫法取自公開報表（Snow Sports、Logshark、HR Dashboards 等）；標準大小的列高 20 像素以 Logshark 報表的
Tableau 畫面量測。測試資料見 tests/sqlgen/test_query.py：區與類別的組合依序為 East｜Furniture、
East｜Technology、North｜Office、South｜Furniture、South｜Office、West｜Furniture、West｜Technology。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from tmig.ir import Banding, Divider, iter_zones
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import ROW_HEIGHT, apply_fit

from ..parser import builder as b
from ..parser.builder import ref, worksheet
from .test_formats import bars, fmt, nested, rule, styled
from .test_text import REGION, SALES, db, parse, render

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
    """離散的欄依內容估計寬度（檢視器在區塊放不下時才固定、可以捲動）；設定了寬度時一律固定。"""
    table = worksheet(
        "Table",
        [REGION, SALES],
        cols=ref("none:Region:nk"),
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    spec = render(parse(tmp_path, table), db).spec
    columns = spec["usermeta"]["sizing"]["columns"]
    assert (columns["count"], columns["fixed"]) == (4, False)
    assert 40 <= columns["step"] <= 80
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

    # 多層的標題：每一層都有設定時合計；連續座標軸的寬度
    both = nested(
        "rows",
        rule(
            "header", fmt("width", "60", "none:Region:nk"), fmt("width", "80", "none:Category:nk")
        ),
    )
    axis = render(parse(tmp_path, both), db).spec["encoding"]["y"]["axis"]
    assert (axis["minExtent"], axis["maxExtent"]) == (140, 140)
    axis_width = bars(rule("axis", fmt("height", "36", "sum:Sales:qk")))
    x = render(parse(tmp_path, axis_width), db).spec["encoding"]["x"]["axis"]
    assert (x["minExtent"], x["maxExtent"]) == (36, 36)


def banded(*rules: str) -> str:
    return nested("rows", *rules)


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


def test_dividers_are_rendered(tmp_path: Path, db: Path) -> None:
    """分隔線畫在第 level 層成員改變處（Region 的各組之間）；level 大於層數時每一列之間。"""
    spec = render(parse(tmp_path, banded(DIVIDER_RULES)), db).spec
    y = spec["encoding"]["y"]
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
    axis = render(parse(tmp_path, banded(every)), db).spec["encoding"]["y"]["axis"]
    assert axis["gridOpacity"]["condition"]["test"].count("｜") == len(NESTED) - 1


def test_banding_is_rendered(tmp_path: Path, db: Path) -> None:
    """色帶：隔列畫上窗格的顏色，列標題（寬度有設定時）畫上標題的顏色；長條維持原本的粗細。"""
    rendered = render(parse(tmp_path, banded(*BANDING_RULES)), db)
    spec: dict[str, Any] = rendered.spec
    values = spec["data"]["values"]
    assert [v["__band_rows"] for v in values] == [False, True, False, True, False, True, False]
    pane, header, body = spec["layer"]
    for layer in (pane, header):
        assert layer["name"].startswith("__ref")  # 檢視器的選取與醒目提示略過
        assert layer["mark"] == {"type": "rect"}
        assert layer["encoding"]["y"]["field"] == "rows_dim"
    assert pane["encoding"]["color"]["condition"]["value"] == "#f4eee8"
    assert header["encoding"]["color"]["condition"]["value"] == "#f3ede7"
    assert (header["encoding"]["x"], header["encoding"]["x2"]) == ({"value": -140}, {"value": 0})
    assert body["mark"]["height"] == {"band": 0.9}
    assert body["encoding"]["y"]["scale"] == {"type": "band", "paddingInner": 0, "paddingOuter": 0}
