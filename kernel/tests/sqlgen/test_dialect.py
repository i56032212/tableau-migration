"""第三階段 N2、N3：兩段式執行的拆分與方言轉譯。

實際在 PostgreSQL、Spark 與 MySQL 執行的測試在 test_engines.py；Oracle、SQL Server 與 Snowflake 尚無實機，
以轉譯快照（fixtures/*/sql/<方言>/）審核，並以 SQLGlot 的該方言重新解析確認語法。
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
import sqlglot

from tmig.data.executor import QueryResult, execute
from tmig.data.live import execute_live
from tmig.data.sources import DuckDBExecutor
from tmig.ir import load_ir
from tmig.sqlgen.dialect import DialectError, java_replacement, split, to_dialect
from tmig.sqlgen.query import QueryError, worksheet_sql
from tmig.verify.cases import case_state, load_cases
from tmig.verify.run import ensure_data

from ..engines import as_live

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = sorted(p.parent for p in (ROOT / "fixtures").glob("*/golden.ir.json"))
SHEETS = [(d, ws.id) for d in FIXTURES for ws in load_ir(d / "golden.ir.json").worksheets]


def sheet_id(item: tuple[Path, str]) -> str:
    return f"{item[0].name}/{item[1]}"


# ---------------------------------------------------------------- 拆分


@pytest.mark.parametrize("item", SHEETS, ids=[sheet_id(s) for s in SHEETS])
def test_only_table_calcs_stay_local(item: tuple[Path, str]) -> None:
    """Table Calculation 之外（排序值、TOTAL、合計所需的列）都下推。"""
    directory, worksheet = item
    query = worksheet_sql(load_ir(directory / "golden.ir.json"), worksheet)
    local = [c.name for c in query.ctes if not c.pushdown]
    assert local in ([], ["tc"])
    plan = split(query)
    assert plan.fetches, "至少取回一段下推的結果"
    for fetch in plan.fetches:
        assert fetch.name not in local
    # 取回的都是標記層級的結果，不是逐列的資料
    assert {f.name for f in plan.fetches} <= {"meas", "mf", "sorted", "totals"}


def test_sort_and_total_are_pushed_down() -> None:
    directory = ROOT / "fixtures" / "superstore_demo"
    query = worksheet_sql(load_ir(directory / "golden.ir.json"), "ws_sub_category_profit")
    flags = {c.name: c.pushdown for c in query.ctes}
    assert flags["sort_agg"] and flags["sort"] and flags["sorted"]
    assert not flags["tc"]
    plan = split(query)
    assert [f.name for f in plan.fetches] == ["sorted"]
    assert plan.local_sql.startswith('WITH "tc" AS (')
    # SQL 的說明以下推的界線為準
    assert query.sql.index("↑ 以上可下推") < query.sql.index('"tc" AS (')
    assert query.sql.index('"sorted" AS (') < query.sql.index("↑ 以上可下推")


def _close(a: QueryResult, b: QueryResult) -> bool:
    if a.columns != b.columns or len(a.rows) != len(b.rows):
        return False
    for left, right in zip(a.rows, b.rows, strict=True):
        for x, y in zip(left, right, strict=True):
            if isinstance(x, float) and isinstance(y, float):
                if not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9):
                    return False
            elif x != y:
                return False
    return True


@pytest.mark.parametrize("directory", FIXTURES, ids=[d.name for d in FIXTURES])
def test_two_stage_matches_single_stage(directory: Path) -> None:
    """DuckDB 當作來源資料庫：兩段式執行與整段在 DuckDB 執行的結果相同（浮點數加總順序除外）。"""
    ir = load_ir(directory / "golden.ir.json")
    cases = load_cases(directory / "cases.yaml")
    ensure_data(directory, ir)
    compared = 0
    for sheet in cases.sheets:
        ws = next(w for w in ir.worksheets if w.id == sheet.worksheet)
        db = directory / "data" / f"{ws.datasource}.duckdb"
        executor = DuckDBExecutor(db)
        try:
            for case in cases.cases:
                try:
                    query = worksheet_sql(ir, sheet.worksheet, case_state(ir, case, cases.as_of()))
                except QueryError:
                    continue
                single = execute(db, query.sql)
                double = execute_live(query, executor)
                assert _close(single, double), f"{sheet.worksheet}／{case.name}"
                compared += 1
        finally:
            executor.close()
    assert compared >= len(cases.sheets)


# ---------------------------------------------------------------- 轉譯


DIALECTS = [
    "postgres",
    "databricks",
    "spark",
    "oracle",
    "mysql",
    "tsql",
    "snowflake",
    "redshift",
    "trino",
    "athena",
]
SNAPSHOT_DIALECTS = ["oracle", "tsql", "snowflake", "redshift", "athena"]
"""沒有實機的方言：轉譯結果以快照審核。"""
SNAPSHOT_SCHEMAS = {
    "oracle": ["SALES"],
    "tsql": ["dbo"],
    "snowflake": ["PUBLIC"],
    "redshift": ["public"],
    "athena": ["AwsDataCatalog", "sales"],
}


@pytest.mark.parametrize("dialect", ["duckdb", *DIALECTS])
def test_custom_sql_is_restored(dialect: str) -> None:
    sql = 'SELECT "q"."a" AS "f_a" FROM "__tmig_custom_sql_0" AS "q"'
    custom = "SELECT a FROM sales.orders WHERE region = 'East' -- 只看東區;\n"
    out = to_dialect(sql, dialect, [custom])
    # 原文不經轉譯；換行包住，最後一行的註解不會吃掉右括號
    assert "(\nSELECT a FROM sales.orders WHERE region = 'East' -- 只看東區\n)" in out
    assert "__tmig_custom_sql_" not in out


@pytest.mark.parametrize(
    ("dialect", "region", "since"),
    [
        ("postgres", "'O''Hare'", "DATE '2024-01-01'"),
        ("oracle", "'O''Hare'", "DATE '2024-01-01'"),
        ("databricks", "'O\\'Hare'", "DATE '2024-01-01'"),
        ("mysql", "'O\\'Hare'", "DATE '2024-01-01'"),
        ("snowflake", "'O\\'Hare'", "DATE '2024-01-01'"),
        ("tsql", "'O''Hare'", "CAST('2024-01-01' AS DATE)"),
        ("redshift", "'O''Hare'", "DATE '2024-01-01'"),
        ("trino", "'O''Hare'", "DATE '2024-01-01'"),
        ("athena", "'O''Hare'", "DATE '2024-01-01'"),
    ],
)
def test_custom_sql_parameters(dialect: str, region: str, since: str) -> None:
    """自訂 SQL 中的參數代入目前值（與 Tableau 相同），以該方言的常值寫法。"""
    sql = 'SELECT * FROM "__tmig_custom_sql_0" AS "q"'
    custom = (
        "SELECT * FROM t WHERE region = <[Parameters].[Parameter 2]> "
        "AND d >= <[Parameters].[Start]> AND n <= <[Parameters].[Parameter 1]>"
    )
    parameters = {
        "Parameter 2": ("O'Hare", "string"),
        "Start": ("2024-01-01", "date"),
        "Parameter 1": (5, "integer"),
    }
    out = to_dialect(sql, dialect, [custom], parameters)
    assert f"region = {region} AND d >= {since} AND n <= 5" in out
    with pytest.raises(DialectError, match="找不到目前值"):
        to_dialect(sql, dialect, [custom])


@pytest.mark.parametrize("dialect", DIALECTS)
def test_unknown_functions_are_unsupported(dialect: str) -> None:
    """SQLGlot 對不認得的函式只會原樣輸出；轉譯一律擋下，不送出可能算錯的 SQL。"""
    for sql in ("SELECT strftime(d, '%Y') FROM t", "SELECT list_value(1, 2) FROM t"):
        with pytest.raises(DialectError):
            to_dialect(sql, dialect)


def test_unsupported_dialect() -> None:
    with pytest.raises(DialectError, match="teradata"):
        to_dialect("SELECT 1", "teradata")


def test_postgres_semantics() -> None:
    out = to_dialect(
        "SELECT round(x, CAST(1 AS INTEGER)), date_diff('month', a, b), second(ts), "
        "TRY_CAST(s AS DOUBLE), d + to_months(CAST(1 AS INTEGER)) FROM t",
        "postgres",
    )
    # ROUND 轉成 NUMERIC 才是四捨五入
    assert 'ROUND(CAST("x" AS DECIMAL), CAST(CAST(1 AS INT) AS INT))' in out
    # DATEDIFF 計算月份邊界，不是 AGE 的完整月數
    assert "AGE" not in out and "EXTRACT(MONTH FROM" in out
    assert 'FLOOR(EXTRACT(SECOND FROM "ts"))' in out
    # TRY_CAST 以 pg_input_is_valid 判斷，CAST 在 CASE 外面
    assert "CAST(CASE WHEN PG_INPUT_IS_VALID(" in out
    assert "INTERVAL '1 MONTH'" in out


def test_spark_semantics() -> None:
    out = to_dialect(
        "SELECT dayofweek(d), isodow(d), week(d), date_trunc('day', d), median(x) FROM t "
        "WHERE (FALSE)",
        "databricks",
    )
    assert "(DAYOFWEEK(`d`) - 1)" in out
    assert "(((DAYOFWEEK(`d`) + 5) % 7) + 1)" in out
    assert "WEEKOFYEAR(`d`)" in out
    assert "DATE_TRUNC('DAY', `d`)" in out
    assert "PERCENTILE(`x`, 0.5)" in out
    # WHERE (FALSE) 會被 Spark 當成資料表別名
    assert "WHERE FALSE" in out
    assert to_dialect("SELECT regexp_replace(s, 'a(b)', '<\\1>$', 'g') FROM t", "spark").endswith(
        "REGEXP_REPLACE(`s`, 'a(b)', '<$1>\\\\$') FROM `t`"
    )


def test_java_replacement() -> None:
    assert java_replacement("<\\1>") == "<$1>"
    assert java_replacement("$") == "\\$"
    assert java_replacement("\\\\") == "\\\\"


def test_oracle_structure() -> None:
    out = to_dialect('SELECT "k" AS "__k0", sum(x) AS "__a0" FROM t GROUP BY 1', "oracle")
    assert out == 'SELECT "k" AS "__k0", SUM("x") AS "__a0" FROM "t" GROUP BY "k"'
    out = to_dialect('SELECT *, "__a0" AS "__m0" FROM agg', "oracle")
    assert out == 'SELECT "agg".*, "__a0" AS "__m0" FROM "agg"'
    out = to_dialect(
        "SELECT r.* FROM src r LEFT JOIN lod l0 ON TRUE WHERE (FALSE) AND (x > 1) IN (TRUE)",
        "oracle",
    )
    assert out == (
        'SELECT "r".* FROM "src" "r" LEFT JOIN "lod" "l0" ON 1 = 1 WHERE (1 = 0) AND ("x" > 1)'
    )
    out = to_dialect("SELECT a FROM t WHERE a IS NOT DISTINCT FROM b ORDER BY a LIMIT 5", "oracle")
    assert out == (
        'SELECT "a" FROM "t" WHERE DECODE("a", "b", 1, 0) = 1 ORDER BY "a" FETCH FIRST 5 ROWS ONLY'
    )


def test_oracle_semantics() -> None:
    out = to_dialect(
        "SELECT s || 'x', DATE '2024-01-31' + to_months(CAST(1 AS INTEGER)), "
        "date_trunc('quarter', d), dayofweek(d), quantile_cont(x, 0.25), var_samp(x) FROM t",
        "oracle",
    )
    # Oracle 的 || 把 Null 當成空字串：遇到 Null 改成 Null
    assert "CASE WHEN \"s\" IS NULL OR 'x' IS NULL THEN NULL ELSE \"s\" || 'x' END" in out
    assert "ADD_MONTHS(DATE '2024-01-31', CAST(1 AS NUMBER(19)))" in out
    assert "TRUNC(\"d\", 'Q')" in out
    assert "MOD(TO_NUMBER(TO_CHAR(\"d\", 'J')) + 1, 7)" in out
    assert 'PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY "x")' in out
    # VARIANCE 對單一列得 0；VAR_SAMP 得 Null（與 Tableau 相同）
    assert 'VAR_SAMP("x")' in out
    out = to_dialect("SELECT TRY_CAST(s AS DOUBLE), try_strptime(s, '%d.%m.%Y') FROM t", "oracle")
    assert 'CAST("s" AS DOUBLE PRECISION DEFAULT NULL ON CONVERSION ERROR)' in out
    assert 'TO_TIMESTAMP("s" DEFAULT NULL ON CONVERSION ERROR, \'DD"."MM"."YYYY\')' in out


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT (x > 1) AS flag FROM t",
        "SELECT CAST(b AS BOOLEAN) FROM t",
        "SELECT string_split(s, ',') FROM t",
    ],
)
def test_oracle_unsupported(sql: str) -> None:
    with pytest.raises(DialectError):
        to_dialect(sql, "oracle")


def test_mysql_semantics() -> None:
    out = to_dialect(
        "SELECT round(x, CAST(1 AS INTEGER)), date_diff('month', a, b), dayofweek(d), week(d), "
        "TRY_CAST(s AS DOUBLE), CAST((d + to_days(CAST(1 AS INTEGER))) AS DATE), "
        "regexp_matches(s, 'a'), a IS NOT DISTINCT FROM b, s || 'x' FROM t",
        "mysql",
    )
    # ROUND 轉成 DECIMAL 才是四捨五入
    assert "ROUND(CAST(`x` AS DECIMAL(65, 30)), CAST(1 AS SIGNED))" in out
    # DATEDIFF 計算月份邊界（TIMESTAMPDIFF 是完整的月數）
    assert "TIMESTAMPDIFF" not in out and "EXTRACT(MONTH FROM `b`)" in out
    assert "(DAYOFWEEK(`d`) - 1)" in out and "WEEK(`d`, 3)" in out
    # 沒有 TRY_CAST：不合格式的字串以正規表示式擋下（MySQL 的 CAST 得 0）
    assert "CASE WHEN REGEXP_LIKE(`s`, '^[[:space:]]*" in out
    # 日期加天數以 INTERVAL（MySQL 的日期加整數是數字相加）
    assert "`d` + INTERVAL (CAST(1 AS SIGNED)) DAY" in out
    assert "REGEXP_LIKE(`s`, 'a', 'c')" in out and "`a` <=> `b`" in out
    assert "CONCAT(`s`, 'x')" in out


def test_tsql_semantics() -> None:
    out = to_dialect(
        "SELECT (x > 1) AS f, k, sum(q) FROM t WHERE flag AND TRUE AND a IS NOT DISTINCT FROM b "
        "GROUP BY 1, 2 ORDER BY k NULLS LAST LIMIT 3",
        "tsql",
    )
    # 沒有布林型別：值的位置的條件轉成 BIT，條件位置的 BIT 與 1 比較
    assert "(CAST(CASE WHEN [x] > 1 THEN 1 WHEN NOT ([x] > 1) THEN 0 END AS BIT)) AS [f]" in out
    assert "WHERE [flag] = 1 AND 1 = 1 AND ([a] = [b] OR [a] IS NULL AND [b] IS NULL)" in out
    # 沒有 GROUP BY 位置；LIMIT 是 TOP
    assert "GROUP BY (CAST(CASE WHEN [x] > 1" in out and out.startswith("SELECT TOP 3")
    out = to_dialect(
        "SELECT s || '東部' || t, length(s), CAST(sum(q) AS BIGINT), avg(q), "
        "date_diff('day', a, b), dayofweek(d), date_trunc('month', d), TRY_CAST(s AS DOUBLE) FROM t",
        "tsql",
    )
    # 字串相加遇到 Null 得 Null；非英文的常值為 N'…'
    assert "CONCAT([s], N'東部', [t])" in out and "[s] IS NULL OR N'東部' IS NULL" in out
    # LEN 不計結尾的空白
    assert (
        "(LEN(CASE WHEN [s] IS NULL OR '.' IS NULL THEN NULL ELSE CONCAT([s], '.') END) - 1)" in out
    )
    # 整數的 SUM 先轉 BIGINT、AVG 先轉浮點數
    assert "SUM(CAST([q] AS BIGINT))" in out and "AVG(CAST([q] AS FLOAT))" in out
    assert "DATEDIFF_BIG(DAY, [a], [b])" in out
    # 星期不受 @@DATEFIRST 影響
    assert "DATEDIFF(DAY, CAST('1900-01-07' AS DATE), CAST([d] AS DATE)) % 7" in out
    assert "CAST(DATEFROMPARTS(YEAR([d]), MONTH([d]), 1) AS DATETIME2)" in out
    # 空字串轉數字是 Null（SQL Server 的 TRY_CAST 得 0）
    assert "TRY_CAST(NULLIF(LTRIM(RTRIM([s])), '') AS FLOAT(53))" in out


def test_snowflake_semantics() -> None:
    out = to_dialect(
        "SELECT dayofweek(d), week(d), regexp_matches(s, '(?i)^a'), monthname(d), "
        "TRY_CAST(s AS DOUBLE), epoch(ts), round(x, CAST(2 AS INTEGER)) FROM t",
        "snowflake",
    )
    # 星期與週數以 ISO 的函式計算，不受 WEEK_START 影響
    assert 'MOD(DAYOFWEEKISO("d"), 7)' in out and 'WEEKISO("d")' in out
    # 部分符合（REGEXP_LIKE 要整段符合）；(?i) 改成參數
    assert "(REGEXP_INSTR(\"s\", '^a', 1, 1, 0, 'i') > 0)" in out
    # MONTHNAME 是縮寫：完整的英文名稱
    assert "CASE MONTH(\"d\") WHEN 1 THEN 'January'" in out
    # TRY_CAST 只接受字串
    assert 'TRY_CAST(TO_VARCHAR("s") AS DOUBLE)' in out
    assert 'DATE_PART(EPOCH_MICROSECOND, "ts") / 1000000.0' in out
    assert 'ROUND("x", 2)' in out


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("mysql", "SELECT median(x) FROM t"),
        ("mysql", "SELECT corr(x, y) FROM t"),
        ("mysql", "SELECT string_split(s, ',') FROM t"),
        ("tsql", "SELECT median(x) FROM t"),
        ("tsql", "SELECT regexp_matches(s, 'a') FROM t"),
        ("tsql", "SELECT try_strptime(s, '%Y') FROM t"),
        ("tsql", "SELECT CAST(x AS BOOLEAN) FROM t"),
        ("tsql", "SELECT split_part(s, '-', n) FROM t"),
    ],
)
def test_new_dialects_unsupported(dialect: str, sql: str) -> None:
    with pytest.raises(DialectError):
        to_dialect(sql, dialect)


# ---------------------------------------------------------------- 沒有實機的方言：轉譯快照


@pytest.mark.parametrize("dialect", SNAPSHOT_DIALECTS)
@pytest.mark.parametrize("item", SHEETS, ids=[sheet_id(s) for s in SHEETS])
def test_dialect_snapshot(item: tuple[Path, str], dialect: str) -> None:
    """尚無實機（Oracle、SQL Server、Snowflake）：轉譯結果有任何變動都要人工審核。

    更新快照：TMIG_UPDATE_SNAPSHOTS=1 uv run pytest kernel/tests/sqlgen/test_dialect.py
    """
    directory, worksheet = item
    ir = as_live(load_ir(directory / "golden.ir.json"), dialect, SNAPSHOT_SCHEMAS[dialect])
    query = worksheet_sql(ir, worksheet)
    parts: list[str] = []
    for fetch in split(query).fetches:
        try:
            sql = to_dialect(fetch.sql, dialect)
        except DialectError as exc:
            sql = f"-- 尚不支援：{exc}"
        else:
            # SQLGlot 的該方言能重新解析（語法正確）
            sqlglot.parse_one(sql, read=dialect)
        parts.append(f"-- 取回 {fetch.name}\n{sql}\n")
    text = "\n".join(parts)
    snapshot = directory / "sql" / dialect / f"{worksheet}.sql"
    if os.environ.get("TMIG_UPDATE_SNAPSHOTS") == "1":
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_text(text, encoding="utf-8", newline="\n")
    assert snapshot.is_file(), f"缺少快照 {snapshot}；以 TMIG_UPDATE_SNAPSHOTS=1 產生後人工審核"
    assert text == snapshot.read_text(encoding="utf-8"), f"{worksheet} 的 {dialect} SQL 變了"
