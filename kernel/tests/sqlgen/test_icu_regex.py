"""正規表示式中 RE2（DuckDB）不支援、ICU（Tableau）支援的語法：以 ICU 相容的函式計算（tmig.data.udf），
Live 連線的來源資料庫支援時轉成它的正規表示式函式。"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from tmig.data import udf
from tmig.data.executor import execute
from tmig.formula.parser import parse_formula
from tmig.sqlgen.dialect import DialectError, to_dialect
from tmig.sqlgen.expr import ExprCompiler

from ..formula.helpers import analyzer
from .test_expr import Env

# Logshark 的 Art：秒數取三位有效數字（小數點前的位數多於三位時保留到小數點前）
SECONDS = r"REGEXP_EXTRACT([Customer], '(\d\.?\d\.?\d(\d+(?=\.))?)')"


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("icu") / "t.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE TABLE t ("Customer" VARCHAR)')
        con.executemany(
            "INSERT INTO t VALUES (?)", [("1.234",), ("12345.678",), ("abc",), ("abd",), (None,)]
        )
    return path


def values(db: Path, formula: str) -> list[object]:
    sql = ExprCompiler(analyzer(), Env()).row(parse_formula(formula))
    return [row[0] for row in execute(db, f"SELECT {sql} FROM t").rows]


def test_lookahead_runs_in_duckdb(db: Path) -> None:
    assert values(db, SECONDS) == ["1.23", "12345", None, None, None]
    assert values(db, "REGEXP_MATCH([Customer], 'b(?=c)')") == [False, False, True, False, None]
    assert values(db, "REGEXP_MATCH([Customer], 'b(?!c)')") == [False, False, False, True, None]
    assert values(db, "REGEXP_EXTRACT_NTH([Customer], '(?<=a)(b)(c)?', 2)") == [
        None,
        None,
        "c",
        "",
        None,
    ]
    # 取代字串以 ICU 的寫法（$1 參照群組，\$ 是 $）
    assert values(db, "REGEXP_REPLACE([Customer], '(a)(?=b)', '[$1\\$]')") == [
        "1.234",
        "12345.678",
        "[a$]bc",
        "[a$]bd",
        None,
    ]
    # 反向參照
    assert values(db, r"REGEXP_MATCH([Customer], '(\d)\1')") == [False, False, False, False, None]


def test_plain_patterns_stay_in_re2() -> None:
    sql = ExprCompiler(analyzer(), Env()).row(parse_formula("REGEXP_MATCH([Customer], 'b+c')"))
    assert "regexp_matches" in sql and udf.MATCH not in sql


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgres", "REGEXP_MATCH"),
        ("mysql", "REGEXP_SUBSTR"),
        ("databricks", "REGEXP_EXTRACT"),
        ("trino", "REGEXP_EXTRACT"),
        ("hive", "REGEXP_EXTRACT"),
    ],
)
def test_live_sources_with_lookahead(dialect: str, expected: str) -> None:
    sql = ExprCompiler(analyzer(), Env()).row(parse_formula(SECONDS))
    out = to_dialect(f'SELECT {sql} AS v FROM "t"', dialect)
    assert expected in out and "(?=" in out and udf.EXTRACT.upper() not in out.upper()


@pytest.mark.parametrize("dialect", ["oracle", "snowflake", "bigquery", "tsql", "impala"])
def test_live_sources_without_lookahead(dialect: str) -> None:
    sql = ExprCompiler(analyzer(), Env()).row(parse_formula(SECONDS))
    with pytest.raises(DialectError, match="前向查看"):
        to_dialect(f'SELECT {sql} AS v FROM "t"', dialect)
