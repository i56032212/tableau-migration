"""註解（標記、點、區域）：解析 <annotations> 並畫在圖上（只影響圖表，不影響數字）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from tmig.data.executor import execute
from tmig.ir import Annotation, FieldUse
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_extensions import db
from .test_vegalite import sort_conflicts, validator

__all__ = ["db"]  # pytest fixture

DEPS = [
    instance("[Region]", "None", "[none:Region:nk]"),
    instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
    instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
]


def coordinate(x: float, y: float) -> str:
    return (
        "<visual-coordinate class='axis'>"
        f"<x-coord axis-value='{x}'><node-reference><axis>{ref('sum:Sales:qk')}</axis></node-reference></x-coord>"
        f"<y-coord axis-value='{y}'><node-reference><axis>{ref('sum:Profit:qk')}</axis></node-reference></y-coord>"
        "<page-reference /></visual-coordinate>"
    )


ANNOTATIONS = f"""
  <annotations>
    <annotation class='area' id='1' text-pinx='0.5' text-piny='0.5'>
      <formatted-text><run bold='true'>Best regions</run></formatted-text>
      <top-left>{coordinate(250, 50)}</top-left>
      <bottom-right>{coordinate(900, 10)}</bottom-right>
      <text x='0.5' y='0.5' />
    </annotation>
    <annotation class='point' id='2'>
      <formatted-text><run>Target</run></formatted-text>
      <point>{coordinate(600, 30)}</point>
      <body x='30' y='-20' />
    </annotation>
    <annotation class='mark' id='3'>
      <formatted-text>
        <run>{escape("<" + ref("none:Region:nk") + ">")}</run>
        <run>Æ&#10;</run>
        <run>{escape("<" + ref("sum:Sales:qk") + ">")}</run>
      </formatted-text>
      <point>
        <visual-coordinate class='mark'>
          <tuple-reference>
            <tuple-descriptor>
              <pane-descriptor><x-fields><field>{ref("sum:Sales:qk")}</field></x-fields><y-fields><field>{ref("sum:Profit:qk")}</field></y-fields></pane-descriptor>
              <columns><field>{ref("none:Region:nk")}</field><field>{ref("sum:Sales:qk")}</field></columns>
            </tuple-descriptor>
            <tuple><value>&quot;West&quot;</value><value>800.0</value></tuple>
          </tuple-reference>
        </visual-coordinate>
      </point>
      <body x='-40' y='25' />
    </annotation>
  </annotations>"""


def render(tmp_path: Path, db: Path, extra: str = ANNOTATIONS) -> Any:
    sheet = worksheet(
        "Scatter",
        DEPS,
        rows=ref("sum:Profit:qk"),
        cols=ref("sum:Sales:qk"),
        mark="Circle",
        encodings=f"<lod column='{ref('none:Region:nk')}' />",
        table_extra=extra,
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    query = worksheet_sql(ir, ir.worksheets[0].id)
    rendered = build_spec(ir, ir.worksheets[0].id, query.columns, execute(db, query.sql))
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return ir, rendered


def named(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {layer.get("name", ""): layer for layer in spec.get("layer", [])}


def test_annotations_are_parsed(tmp_path: Path, db: Path) -> None:
    ir, _ = render(tmp_path, db)
    area, point, mark = ir.worksheets[0].annotations
    assert area.kind == "area" and [r.text for r in area.text] == ["Best regions"]
    assert area.top_left is not None and area.top_left.x == 250.0 and area.top_left.y == 50.0
    assert area.top_left.x_axis == FieldUse(field="f_sales", agg="sum", continuous=True)
    assert point.kind == "point" and point.offset == (30, -20)
    assert mark.kind == "mark" and mark.offset == (-40, 25)
    assert mark.point is not None and mark.point.mark == {"f_region": "West"}


def test_annotations_are_drawn(tmp_path: Path, db: Path) -> None:
    _, rendered = render(tmp_path, db)
    spec = rendered.spec
    layers = named(spec)
    values = spec["data"]["values"]
    # 區域：方框與置中的文字（第一列資料帶著座標）
    box = layers["__deco_annotation0_box"]
    assert box["mark"]["type"] == "rect"
    first = next(v for v in values if v.get("__deco_annotation0"))
    assert first["__deco_annotation0"] == "Best regions"
    assert (first["__deco_annotation0_a_x"], first["__deco_annotation0_b_x"]) == (250.0, 900.0)
    assert first["__deco_annotation0_xmid"] == 575.0
    # 點：偏移時畫線連到文字
    line = layers["__deco_annotation1_line"]
    assert (line["mark"]["x2Offset"], line["mark"]["y2Offset"]) == (30, -20)
    assert layers["__deco_annotation1"]["mark"]["dx"] == 34
    # 標記：West 那一列的文字代入欄位值
    west = next(v for v in values if v.get("__deco_annotation2"))
    assert west["__deco_annotation2"].startswith("West\n")
    text = layers["__deco_annotation2"]
    assert text["mark"]["align"] == "right"
    assert text["encoding"]["x"]["field"] == spec["layer"][0]["encoding"]["x"]["field"]
    assert rendered.notes == []


def axis_point(x: float) -> str:
    """只有橫軸（Sales）的值的位置：長條圖上的點與區域的註解。"""
    return (
        "<visual-coordinate class='axis'>"
        f"<x-coord axis-value='{x}'><node-reference><axis>{ref('sum:Sales:qk')}</axis></node-reference></x-coord>"
        "<page-reference /></visual-coordinate>"
    )


SORTED_BAR_ANNOTATIONS = f"""
  <annotations>
    <annotation class='mark' id='1'>
      <formatted-text><run>看這裡</run></formatted-text>
      <point>
        <visual-coordinate class='mark'>
          <tuple-reference>
            <tuple-descriptor>
              <pane-descriptor><x-fields><field>{ref("sum:Sales:qk")}</field></x-fields></pane-descriptor>
              <columns><field>{ref("none:Region:nk")}</field><field>{ref("sum:Sales:qk")}</field></columns>
            </tuple-descriptor>
            <tuple><value>&quot;South&quot;</value><value>100.0</value></tuple>
          </tuple-reference>
        </visual-coordinate>
      </point>
      <body x='60' y='0' />
    </annotation>
    <annotation class='point' id='2'>
      <formatted-text><run>目標</run></formatted-text>
      <point>{axis_point(500)}</point>
      <body x='20' y='-10' />
    </annotation>
    <annotation class='area' id='3' text-pinx='0.5' text-piny='0.5'>
      <formatted-text><run>區間</run></formatted-text>
      <top-left>{axis_point(200)}</top-left>
      <bottom-right>{axis_point(400)}</bottom-right>
      <text x='0.5' y='0.5' />
    </annotation>
  </annotations>"""


def test_annotations_keep_the_sort_order_of_a_discrete_axis(tmp_path: Path, db: Path) -> None:
    """排序過的長條圖加上註解（2026-11-21 記下的：示範報表的 Sub-Category Profit 依利潤由大到小，加上一個
    標記的註解之後長條變成依名稱排列）。標記的註解畫在那個標記所在的列上——圖層的 y 與主圖是同一個欄位，
    排序也要與主圖相同（sort: null，依查詢排好的順序）；少了它，Vega-Lite 合併各圖層的定義域時整個座標軸
    改成依名稱排列。點與區域的註解只用到連續的座標軸，沒有離散的座標。"""
    by_sales = (
        f"<computed-sort column='{ref('none:Region:nk')}' direction='DESC' "
        f"using='{ref('sum:Sales:qk')}' />"
    )

    def bars(extra: str) -> Any:
        sheet = worksheet(
            "Bars",
            DEPS,
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            view_extra=by_sales,
            table_extra=extra,
        )
        ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
        assert ir.unsupported == [], [u.reason for u in ir.unsupported]
        query = worksheet_sql(ir, ir.worksheets[0].id)
        rendered = build_spec(ir, ir.worksheets[0].id, query.columns, execute(db, query.sql))
        assert [e.message for e in validator().iter_errors(rendered.spec)] == []
        return rendered

    plain = bars("").spec
    order = [v["c0"] for v in plain["data"]["values"]]
    assert order == ["West", "East", "South", "North"]  # 依業績由大到小，不是依名稱
    assert plain["encoding"]["y"]["sort"] is None

    rendered = bars(SORTED_BAR_ANNOTATIONS)
    assert rendered.notes == []
    spec = rendered.spec
    assert [v["c0"] for v in spec["data"]["values"]] == order
    layers = named(spec)
    main = spec["layer"][0]["encoding"]["y"]
    assert (main["field"], main["sort"]) == ("c0_label", None)
    # 標記的註解：連到文字的線與文字都在 South 那一列（格子的中間），排序與主圖相同
    for name in ("__deco_annotation0_line", "__deco_annotation0"):
        y = layers[name]["encoding"]["y"]
        assert (y["field"], y["type"], y["bandPosition"]) == ("c0_label", "ordinal", 0.5)
        assert "sort" in y and y["sort"] is None, name
    south = next(v for v in spec["data"]["values"] if v.get("__deco_annotation0"))
    assert (south["c0"], south["__deco_annotation0"]) == ("South", "看這裡")
    # 點與區域的註解只有橫軸的位置
    assert "y" not in layers["__deco_annotation1"]["encoding"]
    assert "y" not in layers["__deco_annotation2_box"]["encoding"]
    assert sort_conflicts(spec) == []


def test_mark_not_in_data(tmp_path: Path, db: Path) -> None:
    extra = ANNOTATIONS.replace("&quot;West&quot;", "&quot;Mars&quot;")
    _, rendered = render(tmp_path, db, extra)
    assert "__deco_annotation2" not in named(rendered.spec)
    # 附註引用註解的文字（欄位寫成 <膠囊上的名稱>），不寫它是第幾個
    assert rendered.notes == ["註解「<Region> <SUM(Sales)>」的標記不在目前的資料中，沒有畫出"]


CATEGORY = instance("[Category]", "None", "[none:Category:nk]")
SALES = FieldUse(field="f_sales", agg="sum", continuous=True)
REGION_USE = FieldUse(field="f_region")
CATEGORY_USE = FieldUse(field="f_category")


def annotated(tmp_path: Path, db: Path, sheet: str, annotations: list[dict[str, Any]]) -> Any:
    """工作表加上 API 做的註解（Tableau 的註解只記連續座標軸上的值，離散座標軸上的座標解析不出來）。"""
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    noted = [Annotation.model_validate(a) for a in annotations]
    ws = ir.worksheets[0].model_copy(update={"annotations": noted})
    ir = ir.model_copy(update={"worksheets": [ws]})
    query = worksheet_sql(ir, ws.id)
    rendered = build_spec(ir, ws.id, query.columns, execute(db, query.sql))
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered


def point(text: str, **at: Any) -> dict[str, Any]:
    return {"kind": "point", "text": [{"text": text}], "point": at, "offset": [30, 0]}


def area(text: str, top_left: dict[str, Any], bottom_right: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "area",
        "text": [{"text": text}],
        "top_left": top_left,
        "bottom_right": bottom_right,
    }


def test_point_and_area_on_a_discrete_axis(tmp_path: Path, db: Path) -> None:
    """點與區域的座標在離散的座標軸上（2026-11-21 記下的：一律是「…的位置不在這張圖的座標軸上，沒有畫出」
    ——比對的是原始值的欄，離散的座標軸用的是顯示文字的欄）。點畫在那個成員的格子中間，區域從第一格的
    起點到最後一格的終點（兩個角落的先後不拘），文字在這幾格的中間；長條的順序不變。"""
    by_sales = (
        f"<computed-sort column='{ref('none:Region:nk')}' direction='DESC' "
        f"using='{ref('sum:Sales:qk')}' />"
    )
    sheet = worksheet(
        "Bars", DEPS, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"), view_extra=by_sales
    )
    on_south = {"x": 500, "x_axis": SALES, "y": "South", "y_axis": REGION_USE}
    east = {"x": 200, "x_axis": SALES, "y": "East", "y_axis": REGION_USE}
    north = {"x": 400, "x_axis": SALES, "y": "North", "y_axis": REGION_USE}
    west = {"y": "West", "y_axis": REGION_USE}
    rendered = annotated(
        tmp_path,
        db,
        sheet,
        [
            point("點", **on_south),
            area("三格", east, north),
            area("反過來", north, east),
            area("兩格", west, {"y": "East", "y_axis": REGION_USE}),
            point("只有列", y="North", y_axis=REGION_USE),
        ],
    )
    assert rendered.notes == []
    spec = rendered.spec
    values = spec["data"]["values"]
    assert [v["c0"] for v in values] == ["West", "East", "South", "North"]  # 依業績，不是依名稱
    layers = named(spec)
    first = values[0]
    main = spec["layer"][0]["encoding"]["y"]
    assert (main["field"], main["sort"]) == ("c0_label", None)

    # 點：South 那一格的中間；橫軸仍是座標軸上的值。連到文字的線也在同一個位置
    for name in ("__deco_annotation0_line", "__deco_annotation0"):
        y = layers[name]["encoding"]["y"]
        assert (y["field"], y["type"], y["bandPosition"]) == (
            "__deco_annotation0_y",
            "ordinal",
            0.5,
        )
        assert "sort" in y and y["sort"] is None
        assert layers[name]["encoding"]["x"]["field"] == "__deco_annotation0_x"
    assert layers["__deco_annotation0_line"]["encoding"]["y2"]["bandPosition"] == 0.5
    assert (first["__deco_annotation0_y"], first["__deco_annotation0_x"]) == ("South", 500)

    # 區域：East 的起點到 North 的終點（兩個角落反過來也一樣）；文字在三格的中間（South 的中間）
    for name in ("__deco_annotation1", "__deco_annotation2"):
        box = layers[f"{name}_box"]["encoding"]
        assert (box["y"]["field"], box["y"]["bandPosition"]) == (f"{name}_a_y", 0)
        assert box["y"]["sort"] is None
        assert box["y2"] == {"field": f"{name}_b_y", "bandPosition": 1}
        assert (first[f"{name}_a_y"], first[f"{name}_b_y"]) == ("East", "North")
        text = layers[name]["encoding"]["y"]
        assert (text["field"], text["bandPosition"]) == (f"{name}_ymid", 0.5)
        assert first[f"{name}_ymid"] == "South"
        # 橫軸照舊是兩個值之間
        assert box["x2"] == {"field": f"{name}_b_x"}
        assert first[f"{name}_xmid"] == 300
    # 兩格（偶數）：文字在兩格的交界——前一格的終點；沒有橫軸的座標時方框橫跨整張圖
    box = layers["__deco_annotation3_box"]["encoding"]
    assert "x" not in box
    assert (first["__deco_annotation3_a_y"], first["__deco_annotation3_b_y"]) == ("West", "East")
    text = layers["__deco_annotation3"]["encoding"]["y"]
    assert (text["bandPosition"], first["__deco_annotation3_ymid"]) == (1, "West")
    # 只有列的點
    only = layers["__deco_annotation4"]["encoding"]
    assert "x" not in only and first["__deco_annotation4_y"] == "North"
    assert sort_conflicts(spec) == []


def test_discrete_annotation_not_in_data(tmp_path: Path, db: Path) -> None:
    """離散座標軸上的座標不在目前的資料中：不畫，附註說明（不把不存在的成員加進座標軸）。"""
    sheet = worksheet("Bars", DEPS, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    mars = {"y": "Mars", "y_axis": REGION_USE}
    rendered = annotated(
        tmp_path, db, sheet, [point("火星", **mars), area("到火星", {**mars, "y": "East"}, mars)]
    )
    assert rendered.notes == [
        "註解「火星」的位置不在目前的資料中，沒有畫出",
        "註解「到火星」的區域不在目前的資料中，沒有畫出",
    ]
    spec = rendered.spec
    assert not [name for name in named(spec) if name.startswith("__deco_annotation")]
    assert "Mars" not in json.dumps(spec["data"]["values"])


def test_annotations_on_multi_level_headers(tmp_path: Path, db: Path) -> None:
    """多層的列標題（「外層｜內層」）：外層的值占它底下相鄰的幾格，點在這幾格的中間、區域涵蓋這幾格；
    內層的值在好幾組裡都有（不相鄰）時不畫，附註說明。"""
    sheet = worksheet(
        "Nested",
        [*DEPS, CATEGORY],
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        cols=ref("sum:Sales:qk"),
    )
    rendered = annotated(
        tmp_path,
        db,
        sheet,
        [
            point("南區", y="South", y_axis=REGION_USE),
            point("辦公", y="Office", y_axis=CATEGORY_USE),
            area(
                "東到北", {"y": "East", "y_axis": REGION_USE}, {"y": "North", "y_axis": REGION_USE}
            ),
        ],
    )
    spec = rendered.spec
    values = spec["data"]["values"]
    order = [v["rows_dim"] for v in values]
    assert order[:5] == [
        "East｜Furniture",
        "East｜Technology",
        "North｜Office",
        "South｜Furniture",
        "South｜Office",
    ]
    assert rendered.notes == ["註解「辦公」的位置在標題上不只一處，沒有畫出"]
    layers = named(spec)
    first = values[0]
    # South 占兩格：兩格的交界（前一格的終點）
    y = layers["__deco_annotation0"]["encoding"]["y"]
    assert (y["field"], y["type"], y["bandPosition"]) == ("__deco_annotation0_y", "ordinal", 1)
    assert first["__deco_annotation0_y"] == "South｜Furniture"
    assert "__deco_annotation1" not in layers
    # East 的兩格到 North 的一格：三格，文字在中間那一格
    box = layers["__deco_annotation2_box"]["encoding"]
    assert (box["y"]["bandPosition"], box["y2"]["bandPosition"]) == (0, 1)
    assert (first["__deco_annotation2_a_y"], first["__deco_annotation2_b_y"]) == (
        "East｜Furniture",
        "North｜Office",
    )
    assert first["__deco_annotation2_ymid"] == "East｜Technology"
    assert sort_conflicts(spec) == []


def test_annotations_follow_the_column_order(tmp_path: Path, db: Path) -> None:
    """列與欄都有離散維度的表格：欄標題依查詢算出的名次排列（不是資料列的順序），註解在欄標題上的位置
    依同一個順序；格子裡沒有標記（West 沒有 Office）也畫得出來。註解的圖層用自己的欄位、不寫排序
    （sort: null 的圖層不參與排序；另一個欄位寫上同樣的排序陣列反而會被 Vega-Lite 當成衝突）。"""
    sheet = worksheet(
        "Table",
        [*DEPS, CATEGORY],
        rows=ref("none:Region:nk"),
        cols=ref("none:Category:nk"),
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    office = {"x": "Office", "x_axis": CATEGORY_USE}
    rendered = annotated(
        tmp_path,
        db,
        sheet,
        [
            point("空格", **office, y="West", y_axis=REGION_USE),
            area("兩欄", {"x": "Furniture", "x_axis": CATEGORY_USE}, office),
        ],
    )
    assert rendered.notes == []
    spec = rendered.spec
    values = spec["data"]["values"]
    main = spec["layer"][0]["encoding"]["x"]
    assert main["sort"] == ["Furniture", "Office", "Technology"]
    # 資料列的順序不同：Technology 比 Office 先出現
    assert list(dict.fromkeys(v[main["field"]] for v in values)) != main["sort"]
    assert not [v for v in values if v["c0"] == "West" and v[main["field"]] == "Office"]
    layers = named(spec)
    first = values[0]
    where = layers["__deco_annotation0"]["encoding"]
    assert (where["x"]["field"], where["y"]["field"]) == (
        "__deco_annotation0_x",
        "__deco_annotation0_y",
    )
    assert (where["x"]["sort"], where["x"]["bandPosition"]) == (None, 0.5)
    assert (first["__deco_annotation0_x"], first["__deco_annotation0_y"]) == ("Office", "West")
    # Furniture 到 Office 是相鄰的兩欄：文字在兩欄的交界（不是資料列順序中間的 Technology）
    box = layers["__deco_annotation1_box"]["encoding"]
    assert (first["__deco_annotation1_a_x"], first["__deco_annotation1_b_x"]) == (
        "Furniture",
        "Office",
    )
    assert (box["x"]["bandPosition"], box["x2"]["bandPosition"]) == (0, 1)
    text = layers["__deco_annotation1"]["encoding"]["x"]
    assert (first["__deco_annotation1_xmid"], text["bandPosition"]) == ("Furniture", 1)
