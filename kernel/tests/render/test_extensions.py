"""擴充圖表的 Vega-Lite 規格（J1–J4）。"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute
from tmig.ir import load_ir
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import pane_sql, totals_sql, worksheet_sql
from tmig.verify.run import ensure_data

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from ..parser.test_extensions import refline
from ..sqlgen.test_query import ROWS
from .test_vegalite import validator

ROOT = Path(__file__).resolve().parents[3]
FEATURES = ROOT / "fixtures" / "superstore_features"


@cache
def features() -> Any:
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    return ir


def render_features(worksheet_id: str) -> dict[str, Any]:
    ir = features()
    query = worksheet_sql(ir, worksheet_id)
    result = execute(FEATURES / "data" / "ds_orders.duckdb", query.sql)
    rendered = build_spec(ir, worksheet_id, query.columns, result)
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered.spec


def test_measure_values_text_table_is_folded() -> None:
    spec = render_features("ws_segment_kpis")
    values = spec["data"]["values"]
    assert len(values) == 12  # 3 個客群 × 4 個量值
    # CNTD(Order ID) 在 Measure Names 中的別名是「訂單數」
    assert [v["__mn"] for v in values[:4]] == ["Sales", "Profit", "訂單數", "利潤率"]
    assert values[3]["__mv_label"].endswith("%")  # 各量值用自己的格式
    assert spec["encoding"]["x"]["field"] == "__mn"
    assert spec["encoding"]["text"]["field"] == "__mv_label"


def test_measure_values_on_axis_with_measure_names_color() -> None:
    spec = render_features("ws_monthly_sales_vs_profit")
    assert spec["mark"]["type"] == "line"
    assert spec["encoding"]["y"]["field"] == "__mv"
    assert spec["encoding"]["color"]["field"] == "__mn"
    assert len(spec["data"]["values"]) == 24


def test_dual_axis_layers_with_independent_scales() -> None:
    spec = render_features("ws_sales_and_profit_by_year")
    assert spec["resolve"] == {"scale": {"y": "independent"}}
    first, second = spec["layer"]
    assert first["layer"][0]["mark"]["type"] == "bar"
    assert second["mark"]["type"] == "line"
    assert first["encoding"]["y"]["title"] == "Sales"
    assert second["encoding"]["y"]["title"] == "Profit"


def test_pie_spec() -> None:
    spec = render_features("ws_category_mix")
    assert spec["usermeta"]["mark"] == "pie"
    arc, label = spec["layer"]
    assert arc["mark"]["type"] == "arc"
    assert spec["encoding"]["theta"]["type"] == "quantitative"
    assert spec["encoding"]["order"] == {"field": "__row"}
    assert label["transform"][0]["as"] == "__pie_label"
    assert all(v["c2_label"].endswith("%") for v in spec["data"]["values"])


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "orders.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Category" VARCHAR, "Order ID" VARCHAR, '
            '"Order Date" DATE, "Sales" DOUBLE, "Profit" DOUBLE, "Quantity" BIGINT)'
        )
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', ROWS)
    return path


def test_separate_axes_are_stacked(tmp_path: Path, db: Path) -> None:
    deps = [
        instance("[Region]", "None", "[none:Region:nk]"),
        instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
        instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
    ]
    sheet = worksheet(
        "Two",
        deps,
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Region:nk"),
        mark="Bar",
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == []
    query = worksheet_sql(ir, ir.worksheets[0].id)
    rendered = build_spec(ir, ir.worksheets[0].id, query.columns, execute(db, query.sql))
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    top, bottom = rendered.spec["vconcat"]
    assert top["encoding"]["y"]["title"] == "Sales"
    assert bottom["encoding"]["y"]["title"] == "Profit"
    assert "width" not in rendered.spec  # 由檢視器依區塊大小設定每張圖的寬高


# ---------------------------------------------------------------- J4 參考線


def layers(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """參考線的圖層（多層列標題等裝飾圖層以 __deco 為名，不列入）。"""
    return {
        layer["name"]: layer for layer in spec["layer"] if layer.get("name", "").startswith("__ref")
    }


def test_reference_lines_per_pane_and_constant() -> None:
    """每個類別（窗格）的平均畫成每個標記上的短線；整張表的常數線橫跨全圖並加上標籤。"""
    spec = render_features("ws_sub_category_sales")
    values = spec["data"]["values"]
    by_category: dict[str, list[float]] = {}
    for v in values:
        by_category.setdefault(v["c0"], []).append(v["c2"])
    for v in values:
        members = by_category[v["c0"]]
        assert v["__ref0_0"] == pytest.approx(sum(members) / len(members))
        assert v["__ref1_0"] == 100000
    named = layers(spec)
    assert set(named) == {"__ref0_0", "__ref1_0", "__ref1_0_label"}
    assert named["__ref0_0"]["mark"]["type"] == "tick"
    assert named["__ref0_0"]["encoding"]["x"]["field"] == "__ref0_0"
    assert named["__ref0_0"]["encoding"]["y"]["field"] == "rows_dim"
    assert named["__ref0_0"]["transform"] == [{"calculate": "-1", "as": "__row"}]  # 不參與選取
    assert named["__ref1_0"]["mark"]["type"] == "rule"
    assert named["__ref1_0_label"]["encoding"]["text"] == {"value": "目標 100,000"}
    # 標籤與 Tableau 相同寫在窗格的左側
    assert named["__ref1_0_label"]["mark"]["align"] == "left"
    assert spec["layer"][0]["mark"]["type"] == "bar"
    # 預設與 Tableau 相同為 2 像素的灰色實線
    assert named["__ref1_0"]["mark"] == {"type": "rule", "color": "#b4b4b4", "strokeWidth": 2}


def test_reference_line_median_and_stdev() -> None:
    spec = render_features("ws_region_sales_bands")
    sales = sorted(v["c1"] for v in spec["data"]["values"])
    mean = sum(sales) / len(sales)
    sd = (sum((x - mean) ** 2 for x in sales) / len(sales)) ** 0.5  # 母體標準差
    first = spec["data"]["values"][0]
    assert first["__ref0_0"] == pytest.approx((sales[1] + sales[2]) / 2)
    assert (first["__ref1_0"], first["__ref1_1"]) == (
        pytest.approx(mean - sd),
        pytest.approx(mean + sd),
    )
    named = layers(spec)
    # 標準差的分布：平均 ± 1 個標準差之間塗上底色、不畫線（與 Tableau 的預設相同），畫在長條之下
    assert set(named) == {"__ref0_0", "__ref0_0_label", "__ref1_fill0"}
    assert named["__ref0_0_label"]["encoding"]["text"]["value"] == f"{first['__ref0_0']:,.0f}"
    band = named["__ref1_fill0"]
    assert spec["layer"][0] is band
    assert band["mark"]["type"] == "rect"
    assert (band["encoding"]["y"]["field"], band["encoding"]["y2"]["field"]) == (
        "__ref1_0",
        "__ref1_1",
    )


def render_lines(tmp_path: Path, db: Path, sheet: str, **parameters: Any) -> Any:
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == []
    query = worksheet_sql(ir, ir.worksheets[0].id)
    rendered = build_spec(
        ir, ir.worksheets[0].id, query.columns, execute(db, query.sql), parameters or None
    )
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered


def test_reference_line_scopes_parameters_and_percentiles(tmp_path: Path, db: Path) -> None:
    """欄架為 Region、Category：窗格是每個地區（最內層的 Category 切出儲存格）。"""
    deps = [
        instance("[Region]", "None", "[none:Region:nk]"),
        instance("[Category]", "None", "[none:Category:nk]"),
        instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
    ]
    lines = "".join(
        [
            refline(0, "average", value="[Parameters].[Parameter 1]"),
            refline(
                1,
                "percentile",
                "per-pane",
                values="<reference-line-value percentile='25' />"
                "<reference-line-value percentile='75' />",
            ),
            refline(2, "total"),
            refline(3, "average", "per-cell"),
        ]
    )
    sheet = worksheet(
        "Lines",
        deps,
        rows=ref("sum:Sales:qk"),
        cols=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        pane_extra=lines,
    )
    rendered = render_lines(tmp_path, db, sheet)
    values = {(v["c0"], v["c1"]): v for v in rendered.spec["data"]["values"]}
    assert {v["__ref0_0"] for v in values.values()} == {5}  # 參數 Top N 的預設值
    east, west, north = (
        values["East", "Furniture"],
        values["West", "Technology"],
        values["North", "Office"],
    )
    assert (east["__ref1_0"], east["__ref1_1"]) == (162.5, 187.5)  # 150、200 的四分位數
    assert (west["__ref1_0"], west["__ref1_1"]) == (350, 450)
    assert (north["__ref1_0"], north["__ref1_1"]) == (60, 60)
    assert {v["__ref2_0"] for v in values.values()} == {1310}
    assert all(v["__ref3_0"] == v["c2"] for v in values.values())  # 每個儲存格只有一個標記
    assert rendered.notes == []
    again = render_lines(tmp_path, db, sheet, p_top_n=7)
    assert {v["__ref0_0"] for v in again.spec["data"]["values"]} == {7}


def test_reference_line_total_of_average_is_not_drawn(tmp_path: Path, db: Path) -> None:
    deps = [
        instance("[Region]", "None", "[none:Region:nk]"),
        instance("[Sales]", "Avg", "[avg:Sales:qk]", "quantitative"),
    ]
    sheet = worksheet(
        "Average",
        deps,
        rows=ref("avg:Sales:qk"),
        cols=ref("none:Region:nk"),
        pane_extra=refline(0, "total", axis="avg:Sales:qk"),
    )
    rendered = render_lines(tmp_path, db, sheet)
    assert "layer" not in rendered.spec
    assert rendered.notes == ["參考線 refline0 的合計需要重新彙總整體資料，沒有畫出"]


# ---------------------------------------------------------------- J3 甜甜圈與各標記卡的詳細程度


def render_with_panes(worksheet_id: str) -> tuple[Any, dict[int, Any], dict[str, Any]]:
    ir = features()
    database = FEATURES / "data" / "ds_orders.duckdb"
    query = worksheet_sql(ir, worksheet_id)
    result = execute(database, query.sql)
    queries = pane_sql(ir, worksheet_id, query)
    panes = {i: (q.columns, execute(database, q.sql)) for i, q in queries.items()}
    rendered = build_spec(ir, worksheet_id, query.columns, result, None, panes or None)
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return query, queries, rendered.spec


def test_pane_sql_only_for_coarser_panes() -> None:
    _, donut, _ = render_with_panes("ws_category_donut")
    assert list(donut) == [1]  # 中間的洞：沒有 Category
    assert {c.key for c in donut[1].columns} == {"f_zero", "sum(f_sales)"}
    _, mixed, _ = render_with_panes("ws_region_sales_and_segment_profit")
    assert list(mixed) == [0]  # 長條：沒有 Segment
    assert {c.key for c in mixed[0].columns} == {"f_region", "sum(f_sales)"}
    _, same, _ = render_with_panes("ws_sales_and_profit_by_year")
    assert same == {}


def test_donut_spec() -> None:
    _, _, spec = render_with_panes("ws_category_donut")
    ring, center = spec["layer"]
    arc = ring["layer"][0]["mark"] if "layer" in ring else ring["mark"]
    assert arc["type"] == "arc" and "innerRadius" in arc
    sales = next(n for n, key in spec["usermeta"]["measures"].items() if key == "sum(f_sales)")
    total = sum(v[sales] for v in spec["data"]["values"])  # 外圈各類別的銷售額
    assert center["encoding"]["text"]["value"] == f"{total:,.0f}"
    assert center["data"] == {"values": [{}]}
    assert len(spec["data"]["values"]) == 3


def test_totals_draw_coarser_panes_at_worksheet_detail() -> None:
    """有總計時，較粗的標記卡不另外查詢（總計只依工作表的詳細程度查詢），長條也畫出總計。"""
    worksheet_id = "ws_region_sales_and_segment_profit"
    ir = features()
    ir = ir.model_copy(
        update={
            "worksheets": [
                w.model_copy(update={"totals": ["columns"]}) if w.id == worksheet_id else w
                for w in ir.worksheets
            ]
        }
    )
    database = FEATURES / "data" / "ds_orders.duckdb"
    query = worksheet_sql(ir, worksheet_id)
    panes = {
        i: (q.columns, execute(database, q.sql))
        for i, q in pane_sql(ir, worksheet_id, query).items()
    }
    totals = [(t, execute(database, t.query.sql)) for t in totals_sql(ir, worksheet_id, query)]
    result = execute(database, query.sql)
    rendered = build_spec(ir, worksheet_id, query.columns, result, None, panes, totals)
    spec = rendered.spec
    assert spec is not None
    assert [e.message for e in validator().iter_errors(spec)] == []
    assert rendered.notes == ["有總計時，各標記卡一律以工作表的詳細程度呈現"]
    bars, _line = spec["layer"]
    assert "data" not in bars
    assert rendered.rows == 12
    # 總計欄：去掉欄架的 Region，折線標記卡上的 Segment 保留（每個客群一個總計）
    region = next(n for n, key in spec["usermeta"]["dimensions"].items() if key == "f_region")
    segment = next(n for n, key in spec["usermeta"]["dimensions"].items() if key == "f_segment")
    grand = [v for v in spec["data"]["values"] if v.get("__total")]
    assert {v[f"{region}_label"] for v in grand} == {"Grand Total"}
    assert sorted(v[segment] for v in grand) == ["Consumer", "Corporate", "Home Office"]


def test_dual_axis_with_coarser_pane() -> None:
    _, _, spec = render_with_panes("ws_region_sales_and_segment_profit")
    bars, line = spec["layer"]
    bar_values = bars["data"]["values"]
    assert len(bar_values) == 4  # 各地區一根長條
    assert {v["__row"] for v in bar_values} == {-1}  # 不參與選取
    assert len(spec["data"]["values"]) == 12  # 折線：地區 × 客群
    assert "data" not in line
    assert line["encoding"]["color"]["title"] == "Segment"
    tooltip = {t["field"] for t in bars["encoding"]["tooltip"]}
    assert tooltip and all(field in bar_values[0] for field in tooltip)  # 只列出長條資料中有的欄位
