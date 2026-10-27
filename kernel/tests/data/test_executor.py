from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from tmig.data.executor import ExecutionError, execute, format_value


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute(
            "CREATE TABLE t AS SELECT * FROM (VALUES ('East', 1.5), ('West', NULL)) v(region, sales)"
        )
    return path


def test_execute_returns_named_columns(db: Path) -> None:
    result = execute(db, 'SELECT region AS "f_region", sales AS "sum(f_sales)" FROM t ORDER BY 1')
    assert result.columns == ["f_region", "sum(f_sales)"]
    assert result.rows == [("East", 1.5), ("West", None)]
    assert result.records()[0] == {"f_region": "East", "sum(f_sales)": 1.5}
    assert result.to_csv() == "f_region,sum(f_sales)\nEast,1.5\nWest,\n"


def test_execute_is_read_only(db: Path) -> None:
    with pytest.raises(ExecutionError, match="SQL 執行失敗"):
        execute(db, "DELETE FROM t")


def test_missing_database(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="import-hyper"):
        execute(tmp_path / "none.duckdb", "SELECT 1")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (True, "true"),
        (0.1 + 0.2, "0.30000000000000004"),
        (Decimal("1.50"), "1.50"),
        (dt.date(2024, 1, 2), "2024-01-02"),
        (dt.datetime(2024, 1, 2, 3, 4, 5), "2024-01-02T03:04:05"),
        (7, "7"),
    ],
)
def test_format_value(value: object, text: str) -> None:
    assert format_value(value) == text
