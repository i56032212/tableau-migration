"""註解（標記、點、區域）：解析 <annotations> 並畫在圖上（只影響圖表，不影響數字）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from tmig.data.executor import execute
from tmig.ir import FieldUse
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
