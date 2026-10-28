"""擴充圖表的 Vega-Lite 規格（J1–J3）。"""

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
from tmig.sqlgen.query import worksheet_sql
from tmig.verify.run import ensure_data

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
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
    assert [v["__mn"] for v in values[:4]] == ["Sales", "Profit", "CNTD(Order ID)", "利潤率"]
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
