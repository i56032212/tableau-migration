"""公式 → SQL 的 DuckDB 實跑測試：每個 v0 函式至少一個案例。"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Iterator
from typing import Any

import duckdb
import pytest

from tmig.formula.analyze import Target
from tmig.formula.ast import Call, Lod
from tmig.formula.catalog import is_table_calc_function
from tmig.formula.functions import implemented_names
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.sqlgen.expr import USER_FUNCTIONS, ExprCompiler
from tmig.sqlgen.sql import ident

from ..formula.helpers import BASE, analyzer, slug

ROWS = [
    # Order ID, Sales, Profit, Discount, Quantity, Region, Category, Customer, Order Date, Ship Time, Returned
    (
        "A1",
        100.0,
        20.0,
        0.1,
        3,
        "East",
        "Furniture",
        "Ann",
        dt.date(2024, 2, 15),
        dt.datetime(2024, 2, 17, 10, 30),
        True,
    ),
    (
        "A2",
        50.5,
        -5.0,
        0.0,
        1,
        "West",
        "Technology",
        "Bob",
        dt.date(2023, 12, 31),
        dt.datetime(2024, 1, 2, 8, 0),
        False,
    ),
    ("A3", 200.0, None, 0.2, 7, "East", "Technology", "Ann", dt.date(2024, 1, 6), None, None),
    (
        "A4",
        0.0,
        0.0,
        0.0,
        -4,
        "South",
        "Furniture",
        None,
        dt.date(2024, 1, 7),
        dt.datetime(2024, 3, 1, 0, 0),
        False,
    ),
]
COLUMNS = [
    "Order ID",
    "Sales",
    "Profit",
    "Discount",
    "Quantity",
    "Region",
    "Category",
    "Customer",
    "Order Date",
    "Ship Time",
    "Returned",
]
COLUMN_BY_ID = {slug(name): name for name in BASE}
SALES = [row[1] for row in ROWS]
QUANTITY = [float(row[4]) for row in ROWS]


class Env:
    def __init__(self, week_start: str = "sunday") -> None:
        self.week_start = week_start

    def column(self, target: Target) -> str:
        return "t." + ident(COLUMN_BY_ID[target.id])

    def param(self, target: Target) -> str:
        return "10"

    def param_value(self, target: Target) -> object:
        return 10

    def lod(self, node: Lod) -> str:
        raise AssertionError("not used")

    def aggregate(self, node: Call) -> str:
        raise AssertionError("not used")

    def table_calc(self, node: Call) -> str:
        raise AssertionError("not used")


@pytest.fixture(scope="module")
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE t ("
        '"Order ID" VARCHAR, "Sales" DOUBLE, "Profit" DOUBLE, "Discount" DOUBLE, "Quantity" BIGINT, '
        '"Region" VARCHAR, "Category" VARCHAR, "Customer" VARCHAR, "Order Date" DATE, '
        '"Ship Time" TIMESTAMP, "Returned" BOOLEAN)'
    )
    connection.executemany(f"INSERT INTO t VALUES ({', '.join('?' * len(COLUMNS))})", ROWS)
    yield connection
    connection.close()


def row_value(
    con: duckdb.DuckDBPyConnection, formula: str, order_id: str, week_start: str = "sunday"
) -> Any:
    sql = ExprCompiler(analyzer(), Env(week_start)).row(parse_formula(formula))
    result = con.execute(f'SELECT {sql} FROM t WHERE "Order ID" = ?', [order_id]).fetchone()
    assert result is not None
    return result[0]


def agg_value(con: duckdb.DuckDBPyConnection, formula: str) -> Any:
    sql = ExprCompiler(analyzer(), Env()).agg(parse_formula(formula))
    result = con.execute(f"SELECT {sql} FROM t").fetchone()
    assert result is not None
    return result[0]


ROW_CASES: list[tuple[str, str, Any]] = [
    # 運算子
    ("[Quantity] + 1", "A1", 4),
    ("[Quantity] / 2", "A1", 1.5),
    ("[Sales] / 0", "A1", None),
    ("[Quantity] % 2", "A3", 1),
    ("[Quantity] % 0", "A3", None),
    ("2 ^ 3", "A1", 8.0),
    ("-[Profit]", "A2", 5.0),
    ("[Region] + '-' + [Category]", "A1", "East-Furniture"),
    ("[Customer] + 'x'", "A4", None),
    ("[Order Date] + 1", "A2", dt.date(2024, 1, 1)),
    ("[Order Date] - 31", "A1", dt.date(2024, 1, 15)),
    ("[Sales] > 60 AND [Region] = 'East'", "A1", True),
    ("[Sales] > 60 OR [Region] = 'East'", "A2", False),
    ("NOT [Returned]", "A2", True),
    ("[Region] <> 'East'", "A2", True),
    ("[Region] IN ('East', 'South')", "A4", True),
    ("IF [Profit] > 0 THEN 'gain' ELSEIF [Profit] < 0 THEN 'loss' ELSE 'flat' END", "A2", "loss"),
    ("IF [Profit] > 0 THEN 'gain' ELSE 'other' END", "A3", "other"),
    ("IF [Profit] > 0 THEN 'gain' END", "A4", None),
    ("CASE [Region] WHEN 'East' THEN 1 WHEN 'West' THEN 2 ELSE 0 END", "A2", 2),
    ("CASE [Region] WHEN 'North' THEN 1 END", "A2", None),
    ("[Parameters].[Top N] + 1", "A1", 11),
    # 邏輯
    ("IIF([Profit] > 0, 'y', 'n')", "A1", "y"),
    ("IIF([Profit] > 0, 'y', 'n')", "A3", None),
    ("IIF([Profit] > 0, 'y', 'n', 'unknown')", "A3", "unknown"),
    ("ZN([Profit])", "A3", 0),
    ("IFNULL([Customer], 'N/A')", "A4", "N/A"),
    ("ISNULL([Profit])", "A3", True),
    # 數字
    ("ABS([Profit])", "A2", 5.0),
    ("CEILING([Sales])", "A2", 51),
    ("FLOOR([Sales])", "A2", 50),
    ("ROUND([Sales])", "A2", 51.0),
    ("ROUND(-2.5)", "A1", -3.0),
    ("ROUND([Sales] / 3, 2)", "A1", 33.33),
    ("SQRT([Sales])", "A1", 10.0),
    ("SQRT([Profit])", "A2", None),
    ("POWER([Quantity], 2)", "A1", 9.0),
    # 邊長 1 的六角形（頂點朝上）：(2, 0.2) 較靠近 (√3, 0)，(1, 2) 較靠近 (√3 / 2, 1.5)
    ("HEXBINX(2, 0.2)", "A1", pytest.approx(3**0.5)),
    ("HEXBINY(2, 0.2)", "A1", 0.0),
    ("HEXBINX(1, 2)", "A1", pytest.approx(3**0.5 / 2)),
    ("HEXBINY(1, 2)", "A1", 1.5),
    ("HEXBINX(NULL, 2)", "A1", None),
    ("EXP(0)", "A1", 1.0),
    ("LN([Profit])", "A4", None),
    ("LOG(1000)", "A1", 3.0),
    ("LOG(8, 2)", "A1", 3.0),
    ("SIGN([Profit])", "A2", -1),
    ("DIV(7, 2)", "A1", 3),
    ("DIV(-7, 2)", "A1", -3),
    ("DIV(7, 0)", "A1", None),
    ("MIN([Sales], [Profit])", "A1", 20.0),
    ("MAX([Sales], [Profit])", "A3", None),
    # 字串
    ("LEN([Region])", "A1", 4),
    ("LEFT([Category], 3)", "A1", "Fur"),
    ("RIGHT([Category], 3)", "A1", "ure"),
    ("MID([Category], 2, 3)", "A1", "urn"),
    ("MID([Category], 5)", "A1", "iture"),
    ("UPPER([Region])", "A1", "EAST"),
    ("LOWER([Region])", "A1", "east"),
    ("TRIM('  a ')", "A1", "a"),
    ("LTRIM('  a ')", "A1", "a "),
    ("RTRIM('  a ')", "A1", "  a"),
    ("CONTAINS([Category], 'nit')", "A1", True),
    ("CONTAINS([Category], 'NIT')", "A1", False),
    ("STARTSWITH([Category], 'Fur')", "A1", True),
    ("ENDSWITH([Category], 'ology')", "A2", True),
    ("FIND([Category], 'n')", "A1", 4),
    ("FIND([Category], 'z')", "A1", 0),
    ("FIND([Category], 'u', 3)", "A1", 7),
    ("REPLACE([Category], 'Furn', 'Furn-')", "A1", "Furn-iture"),
    ("SPLIT('a-b-c', '-', 2)", "A1", "b"),
    ("SPLIT('a-b-c', '-', -1)", "A1", "c"),
    # 日期
    ("YEAR([Order Date])", "A1", 2024),
    ("QUARTER([Order Date])", "A1", 1),
    ("MONTH([Order Date])", "A1", 2),
    ("DAY([Order Date])", "A1", 15),
    ("WEEK([Order Date])", "A3", 1),
    ("WEEK([Order Date])", "A4", 2),
    ("DATEPART('weekday', [Order Date])", "A4", 1),
    ("DATEPART('weekday', [Order Date], 'monday')", "A4", 7),
    ("DATEPART('week', [Order Date], 'monday')", "A4", 1),
    ("DATEPART('dayofyear', [Order Date])", "A1", 46),
    ("DATEPART('hour', [Ship Time])", "A1", 10),
    ("DATETRUNC('month', [Order Date])", "A1", dt.date(2024, 2, 1)),
    ("DATETRUNC('quarter', [Order Date])", "A1", dt.date(2024, 1, 1)),
    ("DATETRUNC('week', [Order Date])", "A1", dt.date(2024, 2, 11)),
    ("DATETRUNC('week', [Order Date], 'monday')", "A1", dt.date(2024, 2, 12)),
    ("DATETRUNC('day', [Ship Time])", "A1", dt.datetime(2024, 2, 17)),
    ("DATETRUNC('week', [Ship Time])", "A1", dt.datetime(2024, 2, 11)),
    ("DATEADD('month', 1, #2024-01-31#)", "A1", dt.datetime(2024, 2, 29)),
    ("DATEADD('quarter', -1, [Order Date])", "A1", dt.datetime(2023, 11, 15)),
    ("DATEADD('week', 2, [Order Date])", "A1", dt.datetime(2024, 2, 29)),
    ("DATEADD('hour', 3, [Ship Time])", "A1", dt.datetime(2024, 2, 17, 13, 30)),
    ("DATEDIFF('day', [Order Date], [Ship Time])", "A1", 2),
    ("DATEDIFF('month', [Order Date], [Ship Time])", "A2", 1),
    ("DATEDIFF('year', [Order Date], [Ship Time])", "A2", 1),
    ("DATEDIFF('week', #2024-02-10#, #2024-02-11#)", "A1", 1),
    ("DATEDIFF('week', #2024-02-10#, #2024-02-11#, 'monday')", "A1", 0),
    ("MAKEDATE(2024, 2, 29)", "A1", dt.date(2024, 2, 29)),
    # 轉型
    ("INT([Sales] / 3)", "A1", 33),
    ("INT(-3.7)", "A1", -3),
    ("INT('12.9')", "A1", 12),
    ("INT([Returned])", "A1", 1),
    ("FLOAT('2.5')", "A1", 2.5),
    ("FLOAT('x')", "A1", None),
    ("STR([Quantity])", "A1", "3"),
    ("DATE('2024-03-05')", "A1", dt.date(2024, 3, 5)),
    ("DATE([Ship Time])", "A1", dt.date(2024, 2, 17)),
    ("DATETIME('2024-03-05 10:00:00')", "A1", dt.datetime(2024, 3, 5, 10)),
    # 日期與數值：1899-12-30 為第 0 天，小數部分是一天中的時間
    ("FLOAT([Order Date])", "A1", 45337.0),
    ("FLOAT(#2024-01-01 12:00:00#)", "A1", 45292.5),
    ("INT([Order Date])", "A1", 45337),
    ("INT(#2024-01-01 18:00:00#)", "A1", 45292),
    ("DATE(45292.75)", "A1", dt.date(2024, 1, 1)),
    ("DATETIME(45292.5)", "A1", dt.datetime(2024, 1, 1, 12)),
    ("DATETIME(FLOAT(#2024-01-01 12:34:56.5#))", "A1", dt.datetime(2024, 1, 1, 12, 34, 56, 500000)),
    # TabMon 的寫法：時間捨去到 15 分鐘（一天 96 格）
    (
        "DATETIME(INT(FLOAT(#2024-01-01 10:47:00#) * 96) / 96)",
        "A1",
        dt.datetime(2024, 1, 1, 10, 45),
    ),
    # K2 字串
    ("ASCII([Region])", "A1", 69),
    ("ASCII('')", "A1", None),
    ("CHAR(65)", "A1", "A"),
    ("FINDNTH('a-b-c', '-', 2)", "A1", 4),
    ("FINDNTH('a-b-c', '-', 3)", "A1", 0),
    ("FINDNTH([Category], 'u', 2)", "A1", 7),
    ("FINDNTH([Customer], 'n', 1)", "A4", None),
    ("SPACE(2) + 'x'", "A1", "  x"),
    ("SPACE(-1) + 'x'", "A1", "x"),
    ("REGEXP_EXTRACT('abc 123', '[a-z]+\\s+(\\d+)')", "A1", "123"),
    ("REGEXP_EXTRACT([Category], 'ni.')", "A1", "nit"),
    ("REGEXP_EXTRACT([Category], 'z(\\d)')", "A1", None),
    ("REGEXP_EXTRACT_NTH('abc 123', '([a-z]+)\\s+(\\d+)', 2)", "A1", "123"),
    ("REGEXP_EXTRACT_NTH('abc 123', '([a-z]+)\\s+(\\d+)', 0)", "A1", "abc 123"),
    ("REGEXP_MATCH([Category], '^Furn')", "A1", True),
    ("REGEXP_MATCH([Category], '^furn')", "A1", False),
    ("REGEXP_MATCH([Category], '(?i)^furn')", "A1", True),
    ("REGEXP_REPLACE('abc 123', '\\s', '-')", "A1", "abc-123"),
    ("REGEXP_REPLACE('a1b22', '(\\d)', '<$1>')", "A1", "a<1>b<2><2>"),
    ("REGEXP_REPLACE('a1', '\\d', '\\$')", "A1", "a$"),
    # K2 數學
    ("PI()", "A1", math.pi),
    ("DEGREES(PI())", "A1", 180.0),
    ("RADIANS(180)", "A1", math.pi),
    ("SIN(0)", "A1", 0.0),
    ("COS(0)", "A1", 1.0),
    ("TAN(0)", "A1", 0.0),
    ("COT(PI() / 4)", "A1", 1.0),
    ("COT(0)", "A1", None),
    ("ASIN(1)", "A1", math.pi / 2),
    ("ASIN(2)", "A1", None),
    ("ACOS(1)", "A1", 0.0),
    ("ATAN(1)", "A1", math.pi / 4),
    ("ATAN2(1, 1)", "A1", math.pi / 4),
    ("SQUARE([Quantity])", "A1", 9.0),
    # K2 日期
    ("[Ship Time] - [Order Date]", "A1", 2.4375),
    ("[Order Date] - #2024-01-01#", "A1", 45),
    ("[Order Date] - [Order Date]", "A3", 0),
    ("MAKETIME(10, 30, 0)", "A1", dt.datetime(1899, 12, 30, 10, 30)),
    ("MAKEDATETIME([Order Date], MAKETIME(10, 30, 0))", "A1", dt.datetime(2024, 2, 15, 10, 30)),
    ("DATENAME('month', [Order Date])", "A1", "February"),
    ("DATENAME('weekday', [Order Date])", "A1", "Thursday"),
    ("DATENAME('year', [Order Date])", "A1", "2024"),
    ("DATENAME('quarter', [Order Date])", "A1", "1"),
    ("DATENAME('week', [Order Date], 'monday')", "A4", "1"),
    ("DATEPARSE('dd.MM.yyyy HH:mm', '05.03.2024 14:07')", "A1", dt.datetime(2024, 3, 5, 14, 7)),
    ("DATEPARSE('MMM d, yyyy', 'Mar 5, 2024')", "A1", dt.datetime(2024, 3, 5)),
    ("DATEPARSE(\"yyyy'年'M'月'd'日'\", '2024年3月5日')", "A1", dt.datetime(2024, 3, 5)),
    ("DATEPARSE('yyyy-MM-dd', 'x')", "A1", None),
    ("ISDATE('2024-03-05')", "A1", True),
    ("ISDATE('2024-13-05')", "A1", False),
    ("ISDATE([Customer])", "A4", None),
    ("ISOYEAR(#2021-01-01#)", "A1", 2020),
    ("ISOWEEK(#2021-01-01#)", "A1", 53),
    ("ISOWEEKDAY([Order Date])", "A1", 4),
    ("ISOQUARTER(#2021-01-01#)", "A1", 4),
    ("ISOQUARTER([Order Date])", "A1", 1),
    # DuckDB 的 least／greatest 略過 Null：Null 的結果仍要是 Null
    ("ISOQUARTER([Ship Time])", "A3", None),
    ("SPACE(INT([Profit]))", "A3", None),
    ("FLOAT([Quantity])", "A1", 3.0),
]


@pytest.mark.parametrize(("formula", "order_id", "expected"), ROW_CASES)
def test_row_functions(
    con: duckdb.DuckDBPyConnection, formula: str, order_id: str, expected: Any
) -> None:
    actual = row_value(con, formula, order_id)
    if isinstance(expected, float) and actual is not None:
        assert actual == pytest.approx(expected)
    else:
        assert actual == expected


@pytest.mark.parametrize(
    ("formula", "order_id", "week_start", "expected"),
    [
        ("WEEK([Order Date])", "A4", "monday", 1),
        ("DATETRUNC('week', [Order Date])", "A4", "monday", dt.date(2024, 1, 1)),
    ],
)
def test_datasource_week_start(
    con: duckdb.DuckDBPyConnection, formula: str, order_id: str, week_start: str, expected: Any
) -> None:
    assert row_value(con, formula, order_id, week_start) == expected


AGG_CASES: list[tuple[str, Any]] = [
    ("SUM([Sales])", 350.5),
    ("SUM([Quantity])", 7),
    ("AVG([Profit])", 5.0),
    ("MIN([Order Date])", dt.date(2023, 12, 31)),
    ("MAX([Region])", "West"),
    ("COUNT([Profit])", 3),
    ("COUNT([Customer])", 3),
    ("COUNTD([Customer])", 2),
    ("MEDIAN([Sales])", 75.25),
    ("ATTR([Region])", None),
    ("STDEV([Quantity])", pytest.approx((62.75 / 3) ** 0.5)),
    ("STDEVP([Quantity])", pytest.approx((62.75 / 4) ** 0.5)),
    ("VAR([Quantity])", pytest.approx(62.75 / 3)),
    ("VARP([Quantity])", pytest.approx(62.75 / 4)),
    ("PERCENTILE([Sales], 0.5)", 75.25),
    ("SUM([Profit]) / SUM([Sales])", pytest.approx(15.0 / 350.5)),
    ("SUM(IIF([Region] = 'East', [Sales], 0))", 300.0),
    ("COUNTD(IF [Profit] > 0 THEN [Order ID] END)", 1),
    ("CORR([Sales], [Quantity])", pytest.approx(statistics.correlation(SALES, QUANTITY))),
    ("COVAR([Sales], [Quantity])", pytest.approx(statistics.covariance(SALES, QUANTITY))),
    ("COVARP([Sales], [Quantity])", pytest.approx(statistics.covariance(SALES, QUANTITY) * 3 / 4)),
]


@pytest.mark.parametrize(("formula", "expected"), AGG_CASES)
def test_aggregates(con: duckdb.DuckDBPyConnection, formula: str, expected: Any) -> None:
    assert agg_value(con, formula) == expected


def test_attr_single_value(con: duckdb.DuckDBPyConnection) -> None:
    sql = ExprCompiler(analyzer(), Env()).agg(parse_formula("ATTR([Region])"))
    assert con.execute(f"SELECT {sql} FROM t WHERE \"Region\" = 'East'").fetchone() == ("East",)


def test_every_row_and_aggregate_function_is_tested() -> None:
    tested = {n for f, _, _ in ROW_CASES for n in _calls(f)} | {
        n for f, _ in AGG_CASES for n in _calls(f)
    }
    # Table Calculation 在 test_query 以整張工作表實跑；使用者函式在 test_rls 以不同的登入者實跑
    table_calcs = {name for name in implemented_names() if is_table_calc_function(name)}
    nondeterministic = {"TODAY", "NOW", "RANDOM"}
    missing = implemented_names() - tested - table_calcs - USER_FUNCTIONS - nondeterministic
    assert missing == set()


def _calls(formula: str) -> set[str]:
    from tmig.formula.ast import walk

    return {n.name for n in walk(parse_formula(formula)) if isinstance(n, Call)}


@pytest.mark.parametrize(
    ("formula", "mode", "message"),
    [
        ("SUM([Sales])", "row", "不能用在列層級"),
        ("[Sales]", "agg", "需要彙總"),
        ("RANK(SUM([Sales]))", "agg", "只能在彙總之後計算"),
    ],
)
def test_mode_errors(formula: str, mode: str, message: str) -> None:
    compiler = ExprCompiler(analyzer(), Env())
    with pytest.raises(FormulaSemanticError) as exc:
        compiler.compile(parse_formula(formula), mode)
    assert message in exc.value.message


def test_today_and_now_run(con: duckdb.DuckDBPyConnection) -> None:
    assert isinstance(row_value(con, "TODAY()", "A1"), dt.date)
    assert isinstance(row_value(con, "NOW()", "A1"), dt.datetime)


def test_random_runs(con: duckdb.DuckDBPyConnection) -> None:
    """RANDOM()：每一列各自的 [0, 1) 隨機數（型別是實數）。"""
    analyzed = analyzer().analyze(parse_formula("RANDOM()"))
    assert analyzed.datatype == "real"
    sql = ExprCompiler(analyzer(), Env()).row(parse_formula("RANDOM() * 10"))
    values = [row[0] for row in con.execute(f"SELECT {sql} FROM t").fetchall()]
    assert all(0 <= v < 10 for v in values)
    assert len(set(values)) > 1
