"""第三階段 N2：轉譯後的 SQL 在來源資料庫實跑（PostgreSQL、Spark、MySQL、Trino、Spark Thrift Server），
沒有實機的方言以模擬執行。

- 每個公式函式（test_expr 的 ROW_CASES、AGG_CASES）轉成各方言後執行，結果與 DuckDB 的預期值相同
- 兩份 fixture 的萃取資料載入來源資料庫、資料源改成 Live 連線，每張工作表 × 每組篩選以兩段式執行，
  與預期結果比對（與 tmig verify 相同的比對）
- 篩選控制項的可選值整段在來源資料庫執行

沒有實機的方言（Oracle、SQL Server、Snowflake、Redshift、BigQuery、Teradata、Hive、Impala）：轉譯後的 SQL
以 SQLGlot 轉回 DuckDB、各自特有的函式依官方文件的語意模擬後執行（tests/engines.py），檢查轉譯的結構與
函式對照。BigQuery 另以模擬器（goccy/bigquery-emulator）實跑執行器（連線、型別與 Arrow 轉換）。
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
    BigQueryEmulator,
    HiveEmulator,
    OracleEmulator,
    RedshiftEmulator,
    SnowflakeEmulator,
    SparkExecutor,
    TeradataEmulator,
    TrinoEmulator,
    TSqlEmulator,
    as_live,
    bigquery_address,
    bigquery_client,
    bigquery_load,
    bigquery_retry,
    extract_tables,
    mysql_connect,
    mysql_executor,
    mysql_load,
    mysql_server,
    pg_connect,
    pg_load,
    postgres_cluster,
    postgres_executor,
    spark_session,
    spark_thrift_server,
    trino_address,
    trino_connect,
    trino_load,
)
from ..formula.helpers import analyzer
from .test_expr import AGG_CASES, COLUMNS, ROW_CASES, ROWS, Env

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = sorted(p.parent for p in (ROOT / "fixtures").glob("*/golden.ir.json"))
ENGINES = [
    "postgres",
    "spark",
    "oracle",
    "mysql",
    "tsql",
    "snowflake",
    "redshift",
    "trino",
    "athena",
    "bigquery",
    "teradata",
    "hive",
    "impala",
    "spark_thrift",
]
EMULATED = {"oracle", "tsql", "snowflake", "redshift", "bigquery", "teradata", "hive", "impala"}
"""沒有實機、以 DuckDB 模擬的方言（見 tests/engines.py）。Trino 在 CI 以 Docker 實跑（本機模擬）；
Athena 的 SQL 也在同一台 Trino 執行（Athena engine 3 以 Trino 為基礎）。BigQuery 的模擬器
（goccy/bigquery-emulator）執行公式與工作表的結果不可靠（一元負號、1970 年以前的日期時間等有錯，
查詢多時記憶體持續增加），只用來實跑執行器（test_bigquery_executor）。"""
ORACLE_UNSUPPORTED = {
    # Oracle 的 SQL 沒有陣列（FINDNTH 以陣列計算）；SPLIT 只支援正整數常值的位置
    "FINDNTH('a-b-c', '-', 2)",
    "FINDNTH('a-b-c', '-', 3)",
    "FINDNTH([Category], 'u', 2)",
    "FINDNTH([Customer], 'n', 1)",
    "SPLIT('a-b-c', '-', -1)",
}
"""Oracle 尚不支援的公式；結果是布林值的公式也不支援（Oracle 沒有布林型別）。"""
NO_ARRAYS = {
    "FINDNTH('a-b-c', '-', 2)",
    "FINDNTH('a-b-c', '-', 3)",
    "FINDNTH([Category], 'u', 2)",
    "FINDNTH([Customer], 'n', 1)",
}
NO_STATISTICS = {
    "MEDIAN([Sales])",
    "PERCENTILE([Sales], 0.5)",
    "CORR([Sales], [Quantity])",
    "COVAR([Sales], [Quantity])",
    "COVARP([Sales], [Quantity])",
}
NO_REGEX = {
    "REGEXP_EXTRACT('abc 123', '[a-z]+\\s+(\\d+)')",
    "REGEXP_EXTRACT([Category], 'ni.')",
    "REGEXP_EXTRACT([Category], 'z(\\d)')",
    "REGEXP_EXTRACT_NTH('abc 123', '([a-z]+)\\s+(\\d+)', 2)",
    "REGEXP_EXTRACT_NTH('abc 123', '([a-z]+)\\s+(\\d+)', 0)",
    "REGEXP_MATCH([Category], '^Furn')",
    "REGEXP_MATCH([Category], '^furn')",
    "REGEXP_MATCH([Category], '(?i)^furn')",
    "REGEXP_REPLACE('abc 123', '\\s', '-')",
    "REGEXP_REPLACE('a1b22', '(\\d)', '<$1>')",
    "REGEXP_REPLACE('a1', '\\d', '\\$')",
}
NO_DATEPARSE = {
    "DATEPARSE('dd.MM.yyyy HH:mm', '05.03.2024 14:07')",
    "DATEPARSE('MMM d, yyyy', 'Mar 5, 2024')",
    "DATEPARSE(\"yyyy'年'M'月'd'日'\", '2024年3月5日')",
    "DATEPARSE('yyyy-MM-dd', 'x')",
}
NO_CORRELATION = {
    "CORR([Sales], [Quantity])",
    "COVAR([Sales], [Quantity])",
    "COVARP([Sales], [Quantity])",
}
REGEX_GROUPS = {
    "REGEXP_EXTRACT_NTH('abc 123', '([a-z]+)\\s+(\\d+)', 2)",
    "REGEXP_REPLACE('a1b22', '(\\d)', '<$1>')",
    "REGEXP_REPLACE('a1', '\\d', '\\$')",
}
"""取第 2 個以後的群組、取代字串參照群組（或錢字號）。"""
UNSUPPORTED = {
    # MySQL 沒有陣列，也沒有 MEDIAN、PERCENTILE、CORR、COVAR 的彙總函式
    "mysql": NO_ARRAYS | NO_STATISTICS,
    # Hive 沒有陣列的片段；Impala 另外沒有精確的 MEDIAN、PERCENTILE、CORR、COVAR、負數位置的 SPLIT，
    # DATEPARSE 的格式不能有月份名稱
    "hive": NO_ARRAYS,
    "impala": NO_ARRAYS
    | NO_CORRELATION
    | {
        "MEDIAN([Sales])",
        "PERCENTILE([Sales], 0.5)",
        "SPLIT('a-b-c', '-', -1)",
        "DATEPARSE('MMM d, yyyy', 'Mar 5, 2024')",
    },
    # Redshift 沒有陣列與 CORR、COVAR；正規表示式只能取第 1 個群組、取代字串不能參照群組
    "redshift": NO_ARRAYS | NO_CORRELATION | REGEX_GROUPS,
    # SQL Server 另外沒有正規表示式與 DATEPARSE
    "tsql": NO_ARRAYS | NO_STATISTICS | NO_REGEX | NO_DATEPARSE,
}
"""各方言尚不支援的公式（與 Tableau 連該資料庫相同，函式本身不存在）。"""
ORACLE_UNSUPPORTED_SHEETS = {
    # 集合的 IN/OUT 是布林維度，Top N 集合的成員是布林欄位（Oracle 沒有布林型別）
    "ws_top_customers_vs_others",
    "ws_profitable_sub_categories",
}
"""Oracle、Teradata 尚不支援的工作表：轉譯時列為不支援。"""
NO_BOOLEANS = {"oracle", "teradata"}
"""沒有布林型別的方言：結果是布林值的公式與上面的工作表列為不支援。"""
SELECT_SUBQUERIES = {"hive", "impala"}
"""子查詢只能在條件中的方言：集合的成員（EXISTS）列為不支援的工作表與上面相同。"""
EMULATOR_GAPS = {
    # 模擬時 EXTRACT(SECOND FROM 時間間隔) 以 DuckDB 執行，只取整數秒；Oracle 實機含小數秒
    "DATETIME(FLOAT(#2024-01-01 12:34:56.5#))",
}
"""模擬的 Oracle 與實機不同、無法比對的公式（轉譯照樣檢查）。"""

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


@pytest.fixture(scope="module")
def mysql_params() -> Iterator[dict[str, Any]]:
    """一個 MySQL（臨時的伺服器或 TMIG_TEST_MYSQL），測試的資料庫為 tmig_test。"""
    with mysql_server() as params:
        mysql_connect(params).close()
        yield {**params, "database": "tmig_test"}


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
    if request.param in ("tsql", "snowflake", "redshift", "teradata"):
        schema = {"tsql": "dbo", "snowflake": "PUBLIC", "redshift": "public", "teradata": "sales"}[
            request.param
        ]
        con = duckdb.connect()
        con.execute(f'CREATE SCHEMA "{schema}"')
        con.register("expression_rows", _expression_table())
        con.execute("CREATE TABLE t AS SELECT * FROM expression_rows")
        for name, table in tables.items():
            con.register(f"rows_{name}", table)
            con.execute(f'CREATE TABLE "{schema}"."{name}" AS SELECT * FROM "rows_{name}"')
        emulator = {
            "tsql": TSqlEmulator,
            "snowflake": SnowflakeEmulator,
            "redshift": RedshiftEmulator,
            "teradata": TeradataEmulator,
        }[request.param](con)
        try:
            yield Engine(request.param, emulator, live(request.param, [schema]))
        finally:
            con.close()
        return
    if request.param in ("trino", "athena"):
        address = trino_address()
        if address is None:
            # DuckDB 的記憶體資料庫名稱就是 memory：資料表放在 memory."default"，與 Trino 的寫法相同
            con = duckdb.connect()
            con.execute('CREATE SCHEMA "default"')
            con.register("expression_rows", _expression_table())
            con.execute("CREATE TABLE t AS SELECT * FROM expression_rows")
            for name, table in tables.items():
                con.register(f"rows_{name}", table)
                con.execute(f'CREATE TABLE "default"."{name}" AS SELECT * FROM "rows_{name}"')
            try:
                yield Engine(
                    request.param,
                    TrinoEmulator(con, request.param),
                    live(request.param, ["memory", "default"]),
                )
            finally:
                con.close()
            return
        from tmig.data.sources import TrinoExecutor

        con = trino_connect(address)
        trino_load(con, "t", _expression_table())
        for name, table in tables.items():
            trino_load(con, name, table)
        executor = TrinoExecutor(con)
        executor.dialect = request.param
        try:
            yield Engine(request.param, executor, live(request.param, ["memory", "default"]))
        finally:
            con.close()
        return
    if request.param in ("hive", "impala"):
        con = duckdb.connect()
        con.execute('CREATE SCHEMA "sales"')
        con.register("expression_rows", _expression_table())
        con.execute("CREATE TABLE t AS SELECT * FROM expression_rows")
        for name, table in tables.items():
            con.register(f"rows_{name}", table)
            con.execute(f'CREATE TABLE "sales"."{name}" AS SELECT * FROM "rows_{name}"')
        try:
            yield Engine(
                request.param, HiveEmulator(con, request.param), live(request.param, ["sales"])
            )
        finally:
            con.close()
        return
    if request.param == "spark_thrift":
        from tmig.data.sources import ConnectionConfig, open_executor
        from tmig.ir import LiveConnection

        with spark_thrift_server() as (spark, port):
            spark.createDataFrame(_expression_table()).write.mode("overwrite").saveAsTable("t")
            for name, table in tables.items():
                spark.createDataFrame(table).write.mode("overwrite").saveAsTable(name)
            # 正式的連線方式：連線設定檔 → open_executor（HiveServer2 協定、工作階段的時區為 UTC）
            connection = LiveConnection(
                kind="live",
                connection_id="spark_thrift",
                dbclass="spark",
                dialect="spark",
                server="127.0.0.1",
                port=port,
            )
            raw = {"driver": "spark", "user": "tmig"}
            executor = open_executor(
                connection, {"spark_thrift": ConnectionConfig("spark_thrift", "spark", raw)}
            )
            try:
                yield Engine("spark_thrift", executor, live("spark"))
            finally:
                executor.close()
        return
    if request.param == "bigquery":
        # BigQuery 的專案、資料集對應到 DuckDB 附加的資料庫 tmig 與其中的結構描述 tmig
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS tmig")
        con.execute("CREATE SCHEMA tmig.tmig")
        con.register("expression_rows", _expression_table())
        con.execute("CREATE TABLE t AS SELECT * FROM expression_rows")
        for name, table in tables.items():
            con.register(f"rows_{name}", table)
            con.execute(f'CREATE TABLE tmig.tmig."{name}" AS SELECT * FROM "rows_{name}"')
        try:
            yield Engine("bigquery", BigQueryEmulator(con), live("bigquery", ["tmig", "tmig"]))
        finally:
            con.close()
        return
    if request.param == "mysql":
        import pymysql

        params = request.getfixturevalue("mysql_params")
        con = pymysql.connect(**params, charset="utf8mb4", autocommit=True)
        mysql_load(con, "t", _expression_table())
        for name, table in tables.items():
            mysql_load(con, name, table)
        try:
            yield Engine("mysql", mysql_executor(con), live("mysql"))
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
    if engine.name not in NO_BOOLEANS:
        return formula in UNSUPPORTED.get(engine.name, set())
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
        elif engine.name == "oracle" and formula in EMULATOR_GAPS:
            continue
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
            if engine.name in SELECT_SUBQUERIES and sheet.worksheet in ORACLE_UNSUPPORTED_SHEETS:
                with pytest.raises(DialectError, match="子查詢只能用在條件中"):
                    execute_live(query, engine.executor)
                continue
            if engine.name in NO_BOOLEANS and sheet.worksheet in ORACLE_UNSUPPORTED_SHEETS:
                with pytest.raises(DialectError, match="沒有布林值"):
                    execute_live(query, engine.executor)
                continue
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


def test_mysql_executor_session(mysql_params: dict[str, Any]) -> None:
    """正式的連線方式（連線設定檔 → open_executor）：工作階段為唯讀、UTC、英文的月份與星期名稱。"""
    from tmig.data.sources import ConnectionConfig, open_executor
    from tmig.ir import LiveConnection

    connection = LiveConnection(
        kind="live",
        connection_id="mysql_test",
        dbclass="mysql",
        dialect="mysql",
        server=mysql_params["host"],
        port=mysql_params["port"],
        database=mysql_params["database"],
    )
    raw = {"driver": "mysql", "user": mysql_params["user"], "password": mysql_params["password"]}
    executor = open_executor(
        connection, {"mysql_test": ConnectionConfig("mysql_test", "mysql", raw)}
    )
    try:
        row = executor.fetch(
            "SELECT @@session.time_zone AS tz, @@lc_time_names AS names, "
            "@@transaction_read_only AS ro, MONTHNAME(DATE '2024-02-01') AS m"
        ).to_pylist()[0]
        assert row == {"tz": "+00:00", "names": "en_US", "ro": 1, "m": "February"}
        with pytest.raises(Exception, match="READ ONLY"):
            executor.fetch("CREATE TABLE should_fail (x INT)")
    finally:
        executor.close()


def test_bigquery_executor() -> None:
    """BigQuery 的執行器在模擬器實跑：預設資料集讓查詢可以省略專案、DATETIME 與 DATE 取回成 Arrow 的時間與
    日期、空結果保留欄名；轉譯後的公式也能執行（模擬器以 ZetaSQL 解析，型別錯誤會在這裡發現）。"""
    address = bigquery_address()
    if address is None:
        pytest.skip("沒有 BigQuery 模擬器（TMIG_TEST_BIGQUERY）")
    from google.cloud import bigquery  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

    from tmig.data.sources import BigQueryExecutor

    client = bigquery_client(address)
    rows = [
        {"Region": "East", "Sales": 10.5, "Quantity": 2, "Order Date": dt.date(2024, 1, 31)},
        {"Region": "West", "Sales": 4.0, "Quantity": 1, "Order Date": dt.date(2024, 3, 1)},
        {"Region": None, "Sales": None, "Quantity": None, "Order Date": None},
    ]
    schema = pa.schema(
        [
            ("Region", pa.string()),
            ("Sales", pa.float64()),
            ("Quantity", pa.int64()),
            ("Order Date", pa.date32()),
        ]
    )
    bigquery_load(client, "orders", pa.Table.from_pylist(rows, schema))
    config = bigquery.QueryJobConfig(default_dataset="tmig.tmig")
    executor = BigQueryExecutor(client, config, retry=bigquery_retry())
    try:
        table = executor.fetch(
            "SELECT `Region` AS r, SUM(`Sales`) AS s, COUNT(`Quantity`) AS n, "
            "MAX(`Order Date`) AS d, DATETIME '2024-01-02 03:04:05.5' AS t "
            "FROM `orders` GROUP BY `Region` ORDER BY `Region` NULLS LAST"
        )
        assert table.to_pylist() == [
            {
                "r": "East",
                "s": 10.5,
                "n": 1,
                "d": dt.date(2024, 1, 31),
                "t": dt.datetime(2024, 1, 2, 3, 4, 5, 500000),
            },
            {
                "r": "West",
                "s": 4.0,
                "n": 1,
                "d": dt.date(2024, 3, 1),
                "t": dt.datetime(2024, 1, 2, 3, 4, 5, 500000),
            },
            {
                "r": None,
                "s": None,
                "n": 0,
                "d": None,
                "t": dt.datetime(2024, 1, 2, 3, 4, 5, 500000),
            },
        ]
        assert pa.types.is_timestamp(table.schema.field("t").type)
        empty = executor.fetch("SELECT `Region` AS r FROM `orders` WHERE FALSE")
        assert empty.num_rows == 0 and empty.column_names == ["r"]
        # 轉譯後的公式（彙總、字串、日期）：模擬器以 ZetaSQL 解析與型別檢查
        compiler = ExprCompiler(analyzer(), Env())
        formulas = {
            "SUM([Sales]) / SUM([Quantity])": 14.5 / 3,
            "MAX(UPPER(LEFT([Region], 2)))": "WE",
            "MAX(DATEPART('month', [Order Date]))": 3,
            "COUNTD([Region])": 2,
        }
        for formula, expected in formulas.items():
            sql = compiler.agg(parse_formula(formula))
            out = executor.fetch(to_dialect(f"SELECT {sql} AS v FROM orders AS t", "bigquery"))
            assert _matches(out.column(0).to_pylist()[0], expected), formula
    finally:
        client.close()


def test_trino_executor_session() -> None:
    """正式的連線方式（連線設定檔 → open_executor）：工作階段的時區為 UTC、小數依小數位數轉型。"""
    from tmig.data.sources import ConnectionConfig, open_executor
    from tmig.ir import LiveConnection

    address = trino_address()
    if address is None:
        pytest.skip("沒有 Trino（TMIG_TEST_TRINO）")
    host, _, port = address.partition(":")
    trino_connect(address).close()
    connection = LiveConnection(
        kind="live",
        connection_id="trino_test",
        dbclass="presto",
        dialect="trino",
        server=host,
        port=int(port or 8080),
        database="memory",
        schema_name="default",
    )
    executor = open_executor(
        connection, {"trino_test": ConnectionConfig("trino_test", "trino", {"driver": "trino"})}
    )
    try:
        row = executor.fetch(
            "SELECT current_timezone() AS tz, CAST(12.5 AS DECIMAL(10, 2)) AS d, "
            "CAST(7 AS DECIMAL(10, 0)) AS n, current_catalog AS c"
        ).to_pylist()[0]
        assert row == {"tz": "UTC", "d": 12.5, "n": 7, "c": "memory"}
    finally:
        executor.close()
