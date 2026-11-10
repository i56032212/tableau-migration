"""取消彙總量值（Aggregate Measures 關閉）：量值不彙總，每一列資料是一個標記。

資料為 test_query 的訂單；East／Furniture 有兩筆（Sales 100、50），West／Technology 有兩筆。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from tmig.data.executor import execute
from tmig.ir import IR, FieldUse
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import QueryError, totals_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_query import REGION, ROWS

RAW_SALES = instance("[Sales]", "None", "[none:Sales:qk]", "quantitative")
RAW_PROFIT = instance("[Profit]", "None", "[none:Profit:qk]", "quantitative")


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "orders.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Category" VARCHAR, '
            '"Order ID" VARCHAR, "Order Date" DATE, "Sales" DOUBLE, "Profit" DOUBLE, "Quantity" BIGINT)'
        )
        rows = [*ROWS, ROWS[0]]  # 完全相同的一列：仍是兩個標記
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
    return path


def scatter(tmp_path: Path, aggregated: bool = False) -> IR:
    sheet = worksheet(
        "Scatter",
        [REGION, RAW_SALES, RAW_PROFIT],
        rows=ref("none:Profit:qk"),
        cols=ref("none:Sales:qk"),
        mark="Circle",
        encodings=f"<color column='{ref('none:Region:nk')}' />",
    )
    if not aggregated:
        sheet = sheet.replace("<aggregation value='true' />", "<aggregation value='false' />")
    return parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))


def test_each_row_is_a_mark(tmp_path: Path, db: Path) -> None:
    ir = scatter(tmp_path)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert ws.disaggregated
    assert ws.columns == [FieldUse(field="f_sales", continuous=True)]
    query = worksheet_sql(ir, ws.id)
    result = execute(db, query.sql)
    # 每一列資料一個標記：10 列（含完全相同的兩列），不依值合併
    assert len(result.rows) == len(ROWS) + 1
    assert [c.key for c in query.columns] == ["f_profit", "f_sales", "f_region"]
    assert "__row_id" not in result.columns
    east = sorted(r[1] for r in result.rows if r[2] == "East")
    assert east == [50.0, 100.0, 100.0, 200.0]
    rendered = build_spec(ir, ws.id, query.columns, result)
    assert rendered.spec is not None
    assert rendered.spec["mark"]["type"] == "circle"
    assert rendered.spec["encoding"]["x"]["type"] == "quantitative"


def test_aggregated_dimension_pills_are_grouped(tmp_path: Path, db: Path) -> None:
    """彙總開啟時把量值當成維度（[none:Sales:qk]）：相同的值合併成一個標記。"""
    ir = scatter(tmp_path, aggregated=True)
    ws = ir.worksheets[0]
    assert not ws.disaggregated
    result = execute(db, worksheet_sql(ir, ws.id).sql)
    assert len(result.rows) == len(ROWS)


def test_only_dimensions_are_grouped(tmp_path: Path, db: Path) -> None:
    """只有維度時與 Tableau 相同，相同的組合只畫一個標記。"""
    ir = scatter(tmp_path)
    ws = ir.worksheets[0]
    only = ws.model_copy(update={"rows": [], "columns": [], "marks": ws.marks})
    ir = ir.model_copy(update={"worksheets": [only]})
    query = worksheet_sql(ir, ws.id)
    assert "__row_id" not in query.sql
    assert sorted(r[0] for r in execute(db, query.sql).rows) == ["East", "North", "South", "West"]


def test_totals_are_unsupported(tmp_path: Path) -> None:
    ir = scatter(tmp_path)
    ws = ir.worksheets[0].model_copy(update={"totals": ["rows"]})
    ir = ir.model_copy(update={"worksheets": [ws]})
    with pytest.raises(QueryError, match="取消彙總"):
        totals_sql(ir, ws.id, worksheet_sql(ir, ws.id))


@pytest.mark.parametrize(
    "dialect",
    [
        "postgres",
        "databricks",
        "oracle",
        "mysql",
        "tsql",
        "snowflake",
        "redshift",
        "trino",
        "athena",
    ],
)
def test_live_connections_translate(tmp_path: Path, dialect: str) -> None:
    """Live 連線：每一列的編號（ROW_NUMBER）下推到來源資料庫。"""
    from tmig.sqlgen.dialect import split, to_dialect

    from ..engines import as_live

    ir = as_live(scatter(tmp_path), dialect)
    query = worksheet_sql(ir, ir.worksheets[0].id)
    for fetch in split(query).fetches:
        assert "ROW_NUMBER() OVER (ORDER BY" in to_dialect(fetch.sql, dialect)
