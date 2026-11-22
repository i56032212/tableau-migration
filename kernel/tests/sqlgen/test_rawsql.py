"""直接送給資料庫的函式（RAWSQL_*、RAWSQLAGG_*）：Live 連線時 SQL 原文代入參數後原樣送到來源資料庫；萃取、檔案
與在平台的引擎計算的部分不支援。

- SQL 的文字：轉成各方言之後原文一字不改（反斜線、引號、資料庫特有的寫法），參數照常轉譯後代入 %1、%2…
- DuckDB 當作來源資料庫（Live 連線，方言 duckdb）：兩段式執行，數字與一般的公式相同
- PostgreSQL、MySQL：kernel/tests/engines.py 的實跑環境（沒有引擎時略過）
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import execute_query
from tmig.data.live import execute_live
from tmig.data.sources import DuckDBExecutor
from tmig.ir import IR, LiveConnection, TableRelation
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen import rawsql
from tmig.sqlgen.dialect import DialectError, split, to_dialect
from tmig.sqlgen.query import QueryError, worksheet_sql

from ..engines import (
    mysql_connect,
    mysql_executor,
    mysql_load,
    mysql_server,
    pg_connect,
    pg_load,
    postgres_cluster,
    postgres_executor,
)
from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_query import REGION, ROWS, db

__all__ = ["db"]  # pytest fixture

TRICKY = "CASE WHEN %1 LIKE 'E%' THEN 'e\\x' || '''' ELSE %2 END"
"""原文中的引號、反斜線與 LIKE 的 %（不是 %n）都要原樣送出。"""


def live_xml(calcs: str) -> str:
    """Live 連線（PostgreSQL）的 Orders：資料表 "Extract"."Extract"（與 test_query 的 DuckDB 檔相同）。"""
    return f"""
    <datasource caption='Orders' inline='true' name='{b.DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='pg' name='postgres.1'>
            <connection authentication='username-password' class='postgres' dbname='sales'
              port='5432' server='pg.example.com' username='reader' />
          </named-connection>
        </named-connections>
        <relation connection='postgres.1' name='Extract' table='[Extract].[Extract]' type='table' />
        {b.metadata("[Extract]")}
      </connection>
      <aliases enabled='yes' />
      {calcs}
    </datasource>"""


CALCS = (
    b.calc("Calculation_double", "Double Sales", 'RAWSQL_REAL("%1 * 2", [Sales])')
    + b.calc(
        "Calculation_avg", "Raw Avg", 'RAWSQLAGG_REAL("SUM(%1) / COUNT(%2)", [Sales], [Sales])'
    )
    + b.calc(
        "Calculation_tricky",
        "Tricky",
        f'RAWSQL_STR("{TRICKY}", [Region], [Category])',
        "string",
        "dimension",
    )
)


def sheet(rows: str, cols: str, deps: list[str]) -> str:
    return worksheet("Raw", deps, rows=rows, cols=cols)


DEPS = [
    REGION,
    instance("[Calculation_double]", "Sum", "[sum:Calculation_double:qk]", "quantitative"),
    instance("[Calculation_avg]", "User", "[usr:Calculation_avg:qk]", "quantitative"),
    instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
]


def parse(tmp_path: Path, datasource: str, sheets: str = "") -> IR:
    measures = (
        f"({ref('sum:Calculation_double:qk')} + {ref('usr:Calculation_avg:qk')} + "
        f"{ref('sum:Sales:qk')})"
    )
    xml = b.workbook(datasource, worksheets=sheets or sheet(ref("none:Region:nk"), measures, DEPS))
    return parse_workbook(b.write_twb(tmp_path, xml))


def duckdb_live(ir: IR) -> IR:
    """方言改成 duckdb（DuckDB 當作來源資料庫；SQL 原文也是 DuckDB 的寫法）。"""

    def convert(ds: Any) -> Any:
        assert isinstance(ds.connection, LiveConnection)
        connection = ds.connection.model_copy(
            update={
                "dialect": "duckdb",
                "relation": TableRelation(
                    kind="table", name="Extract", table=["Extract", "Extract"]
                ),
            }
        )
        return ds.model_copy(update={"connection": connection})

    return ir.model_copy(update={"datasources": [convert(ds) for ds in ir.datasources]})


def test_rawsql_on_live_connections_is_supported(tmp_path: Path) -> None:
    """Live 連線的資料源：RAWSQL_*、RAWSQLAGG_* 可以分析（型別依函式、RAWSQLAGG 是彙總）。"""
    ir = parse(tmp_path, live_xml(CALCS))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    fields = {f.caption: f for f in ir.datasources[0].fields}
    assert fields["Double Sales"].calc is not None and fields["Double Sales"].calc.level == "row"
    assert fields["Raw Avg"].calc is not None and fields["Raw Avg"].calc.level == "aggregate"


def test_rawsql_on_extracts_is_unsupported(tmp_path: Path) -> None:
    """萃取（與檔案）：在平台的引擎計算，Tableau 這時的行為沒有文件——列為不支援，說明原因。"""
    ir = parse(tmp_path, b.extract_datasource(columns=CALCS))
    reasons = [u.reason for u in ir.unsupported]
    assert any("Double Sales" in r and "RAWSQL_REAL" in r and "Live 連線" in r for r in reasons), (
        reasons
    )
    assert any("Raw Avg" in r and "RAWSQLAGG_REAL" in r for r in reasons), reasons


@pytest.mark.parametrize(
    ("formula", "message"),
    [
        ("RAWSQL_REAL([Region], [Sales])", "字串常值"),
        ('RAWSQL_REAL("%1 + %2", [Sales])', "%2"),
        ('RAWSQL_REAL("%1", SUM([Sales]))', "彙總"),
        ('RAWSQL_SPATIAL("%1", [Region])', "RAWSQL_SPATIAL"),
    ],
)
def test_rawsql_mistakes_are_explained(tmp_path: Path, formula: str, message: str) -> None:
    """SQL 不是字串常值、%n 超過參數個數、參數是彙總、RAWSQL_SPATIAL：不支援或公式錯誤，說明原因。"""
    calcs = b.calc("Calculation_bad", "Bad", formula)
    deps = [
        REGION,
        instance("[Calculation_bad]", "Sum", "[sum:Calculation_bad:qk]", "quantitative"),
    ]
    ir = parse(
        tmp_path, live_xml(calcs), sheet(ref("none:Region:nk"), ref("sum:Calculation_bad:qk"), deps)
    )
    reasons = [u.reason for u in ir.unsupported if "Bad" in u.reason]
    assert reasons and message in reasons[0], reasons


DIALECT_SQL = {
    "postgres": '"f_sales"',
    "mysql": "`f_sales`",
    "snowflake": '"f_sales"',
    "tsql": "[f_sales]",
    "oracle": '"f_sales"',
}
"""各方言中 Sales 欄的參照（資料列的 CTE 中的欄名）。"""


def with_dialect(ir: IR, dialect: str) -> IR:
    datasources = []
    for ds in ir.datasources:
        assert isinstance(ds.connection, LiveConnection)
        connection = ds.connection.model_copy(update={"dialect": dialect})
        datasources.append(ds.model_copy(update={"connection": connection}))
    return ir.model_copy(update={"datasources": datasources})


def translated(ir: IR, dialect: str) -> str:
    """工作表的查詢下推到來源資料庫的部分，轉成 dialect 之後的 SQL。"""
    target = with_dialect(ir, dialect)
    query = worksheet_sql(target, target.worksheets[0].id)
    # 工作表的 SQL（DuckDB 方言）中是編碼過的函式名稱，沒有原文
    assert rawsql.CALL.search(query.sql) and "LIKE 'E%'" not in query.sql
    return "\n".join(to_dialect(f.sql, dialect) for f in split(query).fetches)


@pytest.mark.parametrize("dialect", sorted(DIALECT_SQL))
def test_rawsql_text_survives_translation(tmp_path: Path, dialect: str) -> None:
    """轉成來源資料庫的方言：原文一字不改（引號、反斜線、LIKE 的 %），參數轉成該方言的欄位參照後代入 %1、
    %2…；RAWSQLAGG 的彙總在 GROUP BY 的查詢中。"""
    column = DIALECT_SQL[dialect]
    joined = translated(parse(tmp_path, live_xml(CALCS)), dialect)
    assert "tmig_rawsql" not in joined.lower() and "__tmig_raw_" not in joined
    assert f"({column} * 2)" in joined, joined
    assert f"(SUM({column}) / COUNT({column}))" in joined, joined

    deps = [instance("[Calculation_tricky]", "None", "[none:Calculation_tricky:nk]"), *DEPS[1:]]
    (tmp_path / "tricky").mkdir()
    tricky = parse(
        tmp_path / "tricky",
        live_xml(CALCS),
        sheet(ref("none:Calculation_tricky:nk"), ref("sum:Sales:qk"), deps),
    )
    assert tricky.unsupported == [], [u.reason for u in tricky.unsupported]
    joined = translated(tricky, dialect)
    expected = TRICKY.replace("%1", column.replace("sales", "region")).replace(
        "%2", column.replace("sales", "category")
    )
    assert f"({expected})" in joined, joined


def test_rawsql_bool_needs_boolean_values(tmp_path: Path) -> None:
    """RAWSQL_BOOL：沒有布林值的資料庫（SELECT 中不能直接放條件）不支援，說明原因。"""
    calcs = b.calc(
        "Calculation_big", "Big", 'RAWSQL_BOOL("%1 > 100", [Sales])', "boolean", "dimension"
    )
    deps = [instance("[Calculation_big]", "None", "[none:Calculation_big:nk]"), DEPS[3]]
    ir = parse(
        tmp_path, live_xml(calcs), sheet(ref("none:Calculation_big:nk"), ref("sum:Sales:qk"), deps)
    )
    assert ir.unsupported == []
    query = worksheet_sql(ir, ir.worksheets[0].id)
    fetches = split(query).fetches
    assert any('(("f_sales" > 100)) AS "__k0"' in to_dialect(f.sql, "postgres") for f in fetches)
    with pytest.raises(DialectError, match="RAWSQL_BOOL"):
        for fetch in fetches:
            to_dialect(fetch.sql, "tsql")


@pytest.fixture
def live_db(tmp_path: Path, db: Path) -> Iterator[tuple[IR, DuckDBExecutor]]:
    ir = duckdb_live(parse(tmp_path, live_xml(CALCS)))
    executor = DuckDBExecutor(db)
    try:
        yield ir, executor
    finally:
        executor.close()


def test_rawsql_runs_on_the_source_database(live_db: tuple[IR, DuckDBExecutor]) -> None:
    """DuckDB 當作來源資料庫：RAWSQL_REAL("%1 * 2", [Sales]) 的總和是銷售的兩倍，RAWSQLAGG_REAL 的
    SUM(%1) / COUNT(%2) 是每一列的平均。"""
    ir, executor = live_db
    query = worksheet_sql(ir, ir.worksheets[0].id)
    result = execute_live(query, executor)
    keys = [c.key for c in query.columns]
    region = keys.index("f_region")
    double, average = keys.index("sum(f_double_sales)"), keys.index("f_raw_avg")
    sales = keys.index("sum(f_sales)")
    expected: dict[str, list[float]] = {}
    for row in ROWS:
        expected.setdefault(row[0], []).append(row[4])
    for row in result.rows:
        values = expected[row[region]]
        assert row[double] == pytest.approx(2 * sum(values))
        assert row[sales] == pytest.approx(sum(values))
        assert row[average] == pytest.approx(sum(values) / len(values))
    assert len(result.rows) == len(expected)


EVERY_TYPE = {
    # 名稱：（公式、型別、在工具提示上的彙總）
    "Raw Int": ('RAWSQLAGG_INT("CAST(SUM(%1) AS INTEGER)", [Quantity])', "integer", "usr"),
    "Raw Date": ('RAWSQLAGG_DATE("MAX(%1)", [Order Date])', "date", "usr"),
    "Raw Time": (
        'RAWSQLAGG_DATETIME("CAST(MIN(%1) AS TIMESTAMP)", [Order Date])',
        "datetime",
        "usr",
    ),
    "Raw Text": ('RAWSQLAGG_STR("MAX(%1)", [Category])', "string", "usr"),
    "Raw Flag": ('RAWSQLAGG_BOOL("SUM(%1) > 300", [Sales])', "boolean", "usr"),
    "Row Int": ('RAWSQL_INT("%1 * 10", [Quantity])', "integer", "sum"),
    "Row Date": ('RAWSQL_DATE("CAST(%1 AS DATE)", [Order Date])', "date", "max"),
    "Row Time": ('RAWSQL_DATETIME("CAST(%1 AS TIMESTAMP)", [Order Date])', "datetime", "max"),
}
DERIVATIONS = {"usr": "User", "sum": "Sum", "max": "Max"}


def test_every_rawsql_type_runs(tmp_path: Path, db: Path) -> None:
    """每一種 RAWSQL_*、RAWSQLAGG_*（DuckDB 當作來源資料庫）：結果與依資料直接算的相同。"""
    import datetime as dt

    calcs, deps, tips = "", [REGION], ""
    for i, (name, (formula, datatype, prefix)) in enumerate(EVERY_TYPE.items()):
        calcs += b.calc(f"Calculation_{i}", name, formula, datatype)
        inst = f"{prefix}:Calculation_{i}:qk"
        deps.append(
            instance(f"[Calculation_{i}]", DERIVATIONS[prefix], f"[{inst}]", "quantitative")
        )
        tips += f"<tooltip column='{ref(inst)}' />"
    sheet = worksheet("Types", deps, rows=ref("none:Region:nk"), encodings=tips)
    ir = parse(tmp_path, live_xml(calcs), sheet)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ir = duckdb_live(ir)
    query = worksheet_sql(ir, ir.worksheets[0].id)
    executor = DuckDBExecutor(db)
    try:
        result = execute_live(query, executor)
    finally:
        executor.close()
    keys = [c.key for c in query.columns]
    fields = {f.caption: f.id for f in ir.datasources[0].fields}

    def at(name: str) -> int:
        return next(i for i, k in enumerate(keys) if fields[name] in k)

    by_region: dict[str, list[tuple[Any, ...]]] = {}
    for row in ROWS:
        by_region.setdefault(row[0], []).append(row)
    region = keys.index("f_region")
    for row in result.rows:
        rows = by_region[row[region]]
        assert row[at("Raw Int")] == sum(r[6] for r in rows)
        assert row[at("Raw Date")] == max(r[3] for r in rows)
        assert row[at("Raw Time")] == dt.datetime.combine(min(r[3] for r in rows), dt.time())
        assert row[at("Raw Text")] == max(r[1] for r in rows)
        assert row[at("Raw Flag")] == (sum(r[4] for r in rows) > 300)
        assert row[at("Row Int")] == 10 * sum(r[6] for r in rows)
        assert row[at("Row Date")] == max(r[3] for r in rows)
        assert row[at("Row Time")] == dt.datetime.combine(max(r[3] for r in rows), dt.time())
    assert len(result.rows) == len(by_region)


def test_rawsql_never_runs_in_duckdb(db: Path, live_db: tuple[IR, DuckDBExecutor]) -> None:
    """平台的引擎（DuckDB）計算的部分不能有直接送給資料庫的函式：萃取（整段在 DuckDB 執行）、Live 連線中
    在 DuckDB 計算的部分（Table Calculation 之後、空間函式）——列為不支援並說明原因，不把 DuckDB 不認得的
    函式送出去。"""
    from dataclasses import replace

    ir, _ = live_db
    query = worksheet_sql(ir, ir.worksheets[0].id)
    with pytest.raises(QueryError, match="萃取與檔案") as error:
        execute_query(db, query)
    assert error.value.unsupported
    # 彙總的 CTE 改在 DuckDB 計算（例如之後有空間函式）：轉譯之前就列為不支援
    local = replace(
        query, ctes=[replace(c, pushdown=c.name in ("base", "src")) for c in query.ctes]
    )
    with pytest.raises(QueryError, match="RAWSQL") as error:
        split(local)
    assert error.value.unsupported


def test_rawsql_template_round_trip() -> None:
    """SQL 原文編在函式名稱中（十六進位），轉譯之後換回；參數中又有直接送給資料庫的函式時一層層換回。"""
    name = rawsql.function_name("RAWSQL_STR", TRICKY)
    assert rawsql.parse_name(name) == ("s", TRICKY)
    inner = rawsql.placeholder("lower(x)")
    outer = rawsql.placeholder(rawsql.substitute("upper(%1)", [inner]))
    assert rawsql.restore(f"SELECT {outer}") == "SELECT (upper((lower(x))))"
    with pytest.raises(QueryError, match="%3"):
        rawsql.substitute("%3", ["a"])


# ---------------------------------------------------------------- 實際的資料庫


def _engine_ir(tmp_path: Path, schema: list[str], table: str) -> IR:
    ir = parse(tmp_path, live_xml(CALCS))
    ds = ir.datasources[0]
    assert isinstance(ds.connection, LiveConnection)
    relation = TableRelation(kind="table", name="Extract", table=[*schema, table])
    return ir.model_copy(
        update={
            "datasources": [
                ds.model_copy(
                    update={"connection": ds.connection.model_copy(update={"relation": relation})}
                )
            ]
        }
    )


def _check(ir: IR, executor: Any, dialect: str) -> None:
    ds = ir.datasources[0]
    assert isinstance(ds.connection, LiveConnection)
    target = ir.model_copy(
        update={
            "datasources": [
                ds.model_copy(
                    update={"connection": ds.connection.model_copy(update={"dialect": dialect})}
                )
            ]
        }
    )
    query = worksheet_sql(target, target.worksheets[0].id)
    result = execute_live(query, executor)
    keys = [c.key for c in query.columns]
    double, sales = keys.index("sum(f_double_sales)"), keys.index("sum(f_sales)")
    for row in result.rows:
        assert float(row[double]) == pytest.approx(2 * float(row[sales]))


def _rows_table() -> Any:
    import pyarrow as pa

    names = ["Region", "Category", "Order ID", "Order Date", "Sales", "Profit", "Quantity"]
    return pa.table({n: [r[i] for r in ROWS] for i, n in enumerate(names)})


def test_rawsql_on_postgres(tmp_path: Path) -> None:
    """PostgreSQL（kernel/tests/engines.py：本機的 PostgreSQL；沒有時略過）。"""
    with postgres_cluster() as dsn:
        con = pg_connect(dsn)
        pg_load(con, "Extract", _rows_table(), "Extract")
        _check(_engine_ir(tmp_path, ["Extract"], "Extract"), postgres_executor(con), "postgres")
        con.close()


def test_rawsql_on_mysql(tmp_path: Path) -> None:
    """MySQL（kernel/tests/engines.py：本機的 MySQL；沒有時略過）。"""
    with mysql_server() as params:
        con = mysql_connect(params)
        mysql_load(con, "rawsql_orders", _rows_table())
        _check(_engine_ir(tmp_path, ["tmig_test"], "rawsql_orders"), mysql_executor(con), "mysql")
        con.close()
