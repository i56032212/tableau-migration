"""第三階段 N2：轉譯後的 SQL 在 PostgreSQL、Spark（Databricks 的引擎）與模擬的 Oracle 實跑。

- 每個公式函式（test_expr 的 ROW_CASES、AGG_CASES）轉成各方言後執行，結果與 DuckDB 的預期值相同
- 兩份 fixture 的萃取資料載入來源資料庫、資料源改成 Live 連線，每張工作表 × 每組篩選以兩段式執行，
  與預期結果比對（與 tmig verify 相同的比對）
- 篩選控制項的可選值整段在來源資料庫執行

沒有 PostgreSQL 或 Spark 時略過（見 tests/engines.py）。Oracle 沒有實機：轉譯後的 SQL 以 SQLGlot 轉回
DuckDB、Oracle 特有的函式依官方文件的語意模擬後執行（OracleEmulator），檢查轉譯的結構與函式對照。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from tmig.data.executor import execute
from tmig.data.live import execute_live, execute_remote
from tmig.data.sources import Executor
from tmig.formula.parser import parse_formula
from tmig.ir import IR, FieldUse, load_ir
from tmig.sqlgen.dialect import DialectError, to_dialect
from tmig.sqlgen.expr import ExprCompiler
from tmig.sqlgen.query import domain_sql, worksheet_sql
from tmig.sqlgen.source import datasource_source
from tmig.verify.cases import case_state, load_cases
from tmig.verify.compare import compare, dimension_aliases
from tmig.verify.mapping import load_mapping

from ..engines import (
    OracleEmulator,
    SparkExecutor,
    as_live,
    extract_tables,
    pg_connect,
    pg_load,
    postgres_cluster,
    postgres_executor,
    spark_session,
)
from ..formula.helpers import analyzer
from .test_expr import AGG_CASES, COLUMNS, ROW_CASES, ROWS, Env

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = sorted(p.parent for p in (ROOT / "fixtures").glob("*/golden.ir.json"))
ENGINES = ["postgres", "spark", "oracle"]
ORACLE_UNSUPPORTED = {
    # Oracle 的 SQL 沒有陣列（FINDNTH 以陣列計算）；SPLIT 只支援正整數常值的位置
    "FINDNTH('a-b-c', '-', 2)",
    "FINDNTH('a-b-c', '-', 3)",
    "FINDNTH([Category], 'u', 2)",
    "FINDNTH([Customer], 'n', 1)",
    "SPLIT('a-b-c', '-', -1)",
}
"""Oracle 尚不支援的公式；結果是布林值的公式也不支援（Oracle 沒有布林型別）。"""

EXPR_SCHEMA = pa.schema(
    [
        ("Order ID", pa.string()),
        ("Sales", pa.float64()),
        ("Profit", pa.float64()),
        ("Discount", pa.float64()),
        ("Quantity", pa.int64()),
        ("Region", pa.string()),
        ("Category", pa.string()),
        ("Customer", pa.string()),
        ("Order Date", pa.date32()),
        ("Ship Time", pa.timestamp("us")),
        ("Returned", pa.bool_()),
    ]
)


@dataclass
class Engine:
    name: str
    executor: Executor
    fixtures: dict[Path, IR]
    """fixture 目錄 → 資料源改成 Live 連線的 IR。"""


def _expression_table() -> pa.Table:
    return pa.Table.from_pylist([dict(zip(COLUMNS, row, strict=True)) for row in ROWS], EXPR_SCHEMA)


@pytest.fixture(scope="module", params=ENGINES)
def engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    # 兩份 fixture 的資料源 id 相同：資料表名稱加上 fixture 名稱
    tables = {
        f"{d.name}__{name}": table
        for d in FIXTURES
        for name, table in extract_tables(d, load_ir(d / "golden.ir.json")).items()
    }

    def live(dialect: str, schema: list[str] | None = None) -> dict[Path, IR]:
        return {
            d: as_live(load_ir(d / "golden.ir.json"), dialect, schema, f"{d.name}__")
            for d in FIXTURES
        }

    if request.param == "oracle":
        con = duckdb.connect()
        con.execute("CREATE SCHEMA SALES")
        con.register("expression_rows", _expression_table())
        con.execute("CREATE TABLE t AS SELECT * FROM expression_rows")
        for name, table in tables.items():
            con.register(f"rows_{name}", table)
            con.execute(f'CREATE TABLE SALES."{name}" AS SELECT * FROM "rows_{name}"')
        try:
            yield Engine(
                "oracle",
                OracleEmulator(con),
                live("oracle", ["SALES"]),
            )
        finally:
            con.close()
        return
    if request.param == "postgres":
        with postgres_cluster() as dsn:
            con = pg_connect(dsn)
            pg_load(con, "t", _expression_table())
            for name, table in tables.items():
                pg_load(con, name, table)
            try:
                yield Engine(
                    "postgres",
                    postgres_executor(con),
                    live("postgres"),
                )
            finally:
                con.close()
        return
    with spark_session() as spark:
        spark.createDataFrame(_expression_table()).createOrReplaceTempView("t")
        for name, table in tables.items():
            spark.createDataFrame(table).createOrReplaceTempView(name)
        yield Engine(
            "spark",
            SparkExecutor(spark),
            live("databricks"),
        )


def _matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, float) and actual is not None:
        return actual == pytest.approx(expected)
    if type(expected) is dt.date and isinstance(actual, dt.datetime):
        # Oracle 的 DATE 含時間：日期以午夜的日期時間取回
        return actual == dt.datetime.combine(expected, dt.time())
    return bool(actual == expected)


def _expect_unsupported(engine: Engine, formula: str) -> bool:
    if engine.name != "oracle":
        return False
    return (
        formula in ORACLE_UNSUPPORTED
        or analyzer().analyze(parse_formula(formula)).datatype == "boolean"
    )


def _value(engine: Engine, sql: str) -> Any:
    table = engine.executor.fetch(to_dialect(sql, engine.executor.dialect))
    return table.column(0).to_pylist()[0]


def test_expressions(engine: Engine) -> None:
    """每個公式函式在來源資料庫的結果與 DuckDB 的預期值相同。"""
    problems: list[str] = []
    cases = [
        (
            formula,
            ExprCompiler(analyzer(), Env()).row(parse_formula(formula)),
            f" WHERE \"Order ID\" = '{order_id}'",
            expected,
        )
        for formula, order_id, expected in ROW_CASES
    ] + [
        (formula, ExprCompiler(analyzer(), Env()).agg(parse_formula(formula)), "", expected)
        for formula, expected in AGG_CASES
    ]
    for formula, sql, where, expected in cases:
        unsupported = _expect_unsupported(engine, formula)
        try:
            actual = _value(engine, f"SELECT {sql} AS v FROM t{where}")
        except DialectError:
            if not unsupported:
                problems.append(f"{formula}：轉譯不了")
            continue
        except Exception as exc:
            problems.append(f"{formula}{where}：{str(exc).splitlines()[0][:200]}")
            continue
        if unsupported:
            problems.append(f"{formula}：預期轉譯不了，卻能執行")
        elif not _matches(actual, expected):
            problems.append(f"{formula}{where}：{actual!r}，預期 {expected!r}")
    assert problems == [], f"{engine.name}：\n" + "\n".join(problems)


@pytest.mark.parametrize("directory", FIXTURES, ids=[d.name for d in FIXTURES])
def test_fixture_checks(engine: Engine, directory: Path) -> None:
    """每張工作表 × 每組篩選：下推到來源資料庫的兩段式執行與預期結果一致。"""
    ir = engine.fixtures[directory]
    cases = load_cases(directory / "cases.yaml")
    mapping = load_mapping(directory / "mapping.yaml")
    failures: list[str] = []
    for sheet in cases.sheets:
        for case in cases.cases:
            query = worksheet_sql(ir, sheet.worksheet, case_state(ir, case, cases.as_of()))
            result = execute_live(query, engine.executor)
            expected = directory / "expected" / sheet.worksheet / f"{case.name}.csv"
            comparison = compare(
                expected.read_text(encoding="utf-8-sig"),
                result,
                query.columns,
                mapping.sheets[sheet.worksheet],
                dimension_aliases(ir, query.columns),
            )
            if not comparison.ok:
                failures.append(f"{sheet.worksheet}／{case.name}：{comparison.errors[:2]}")
    assert failures == []


@pytest.mark.parametrize("kind", ["values", "range", "frequency"])
def test_domain_queries(engine: Engine, kind: str) -> None:
    """篩選控制項的可選值整段在來源資料庫執行，與萃取時相同。"""
    directory = ROOT / "fixtures" / "superstore_features"
    live = engine.fixtures[directory]
    extract = load_ir(directory / "golden.ir.json")
    ds = live.datasources[0]
    for field in ("f_region", "f_segment", "f_order_date"):
        use = FieldUse(field=field)
        remote = execute_remote(
            domain_sql(live, ds.id, use, kind),  # type: ignore[arg-type]
            engine.executor,
            datasource_source(ds).custom_sql,
        )
        local = execute(
            directory / "data" / f"{ds.id}.duckdb",
            domain_sql(extract, ds.id, use, kind),  # type: ignore[arg-type]
        )
        assert remote.rows == local.rows, field
