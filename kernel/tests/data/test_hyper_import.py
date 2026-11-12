"""Hyper 匯入（C1）：型別對應、資料一致與匯入自檢。"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from tableauhyperapi import (
    Connection,
    CreateMode,
    HyperProcess,
    Inserter,
    SqlType,
    TableDefinition,
    TableName,
    Telemetry,
)

from tmig.data.hyper_import import HyperImportError, StatCheck, _same, duckdb_type, import_workbook
from tmig.parser.workbook import parse_workbook

from ..parser import builder as b

ROWS = [
    [1, Decimal("10.50"), 1.25, "East", dt.date(2024, 1, 1), dt.datetime(2024, 1, 1, 8, 30), True],
    [2, Decimal("-3.10"), None, "West", dt.date(2024, 2, 29), None, False],
    [None, None, 2.5, None, None, dt.datetime(2023, 12, 31, 23, 59, 59), None],
]


def make_hyper(path: Path, log_dir: Path) -> None:
    table = TableDefinition(
        TableName("Extract", "Extract"),
        [
            TableDefinition.Column("Id", SqlType.big_int()),
            TableDefinition.Column("Amount", SqlType.numeric(10, 2)),
            TableDefinition.Column("Ratio", SqlType.double()),
            TableDefinition.Column("Region", SqlType.text()),
            TableDefinition.Column("Order Date", SqlType.date()),
            TableDefinition.Column("Shipped At", SqlType.timestamp()),
            TableDefinition.Column("Flag", SqlType.bool()),
        ],
    )
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        conn.catalog.create_table(table)
        with Inserter(conn, table) as inserter:
            inserter.add_rows(ROWS)
            inserter.execute()


@pytest.fixture(scope="module")
def imported(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list]:
    tmp = tmp_path_factory.mktemp("hyper")
    hyper = tmp / "test.hyper"
    make_hyper(hyper, tmp)
    twbx = b.write_twbx(tmp, b.workbook(), {"Data/Extracts/test.hyper": hyper.read_bytes()})
    ir = parse_workbook(twbx, lenient=False)
    out = tmp / "data"
    return out, import_workbook(twbx, out, ir)


def test_import_matches_hyper(imported: tuple[Path, list]) -> None:
    out, results = imported
    (result,) = results
    assert result.datasource == "ds_orders"
    assert result.table.rows == 3
    assert result.table.ok, [c for c in result.table.checks if not c.ok]
    types = {c.name: c.duckdb_type for c in result.table.columns}
    assert types == {
        "Id": "BIGINT",
        "Amount": "DECIMAL(10, 2)",
        "Ratio": "DOUBLE",
        "Region": "VARCHAR",
        "Order Date": "DATE",
        "Shipped At": "TIMESTAMP",
        "Flag": "BOOLEAN",
    }
    with duckdb.connect(str(out / "ds_orders.duckdb"), read_only=True) as con:
        rows = con.execute('SELECT * FROM "Extract"."Extract" ORDER BY "Id" NULLS LAST').fetchall()
    assert [list(r) for r in rows] == ROWS
    report = json.loads((out / "ds_orders.import.json").read_text(encoding="utf-8"))
    assert report["table"]["ok"] is True
    assert report["ok"] is False  # IR 的欄位不在這個測試用 .hyper 裡，見下一個測試
    assert len(report["hyper_sha256"]) == 64


def test_ir_columns_missing_from_extract(imported: tuple[Path, list]) -> None:
    # 測試用 workbook 的欄位（Category、Sales…）多數不在這個 .hyper 裡，匯入結果要列出來
    _, results = imported
    assert "f_category（Category）" in results[0].missing_columns
    assert not results[0].ok


def test_unsupported_types_raise() -> None:
    with pytest.raises(HyperImportError, match="沒有定義對應"):
        duckdb_type(SqlType.bytes())
    with pytest.raises(HyperImportError):
        duckdb_type(SqlType.geography())
    assert duckdb_type(SqlType.numeric(18, 4)) == "DECIMAL(18, 4)"
    assert duckdb_type(SqlType.varchar(10)) == "VARCHAR"


def test_stat_comparison_rules() -> None:
    assert _same("sum", "DOUBLE", 1.0000000001, 1.0)
    assert not _same("sum", "DOUBLE", 1.001, 1.0)
    assert not _same("min", "DOUBLE", 1.0000000001, 1.0)
    assert _same("sum", "NUMERIC", Decimal("1.10"), Decimal("1.1"))
    assert not _same("sum", "BIG_INT", 3, 4)
    assert _same("min", "DATE", dt.date(2024, 1, 1), dt.date(2024, 1, 1))
    assert not _same("max", "TEXT", None, "a")
    assert StatCheck(column=None, stat="rows", hyper="1", duckdb="1", ok=True).ok


def test_case_insensitive_collation_is_kept(tmp_path: Path) -> None:
    """Tableau 的萃取多半是大小寫不分的定序（en_US_CI）：DuckDB 以 NOCASE 定序保留，比較、篩選與分組與
    Tableau 相同（公開報表 Credit Card Complaints 以 'In Progress' 比對資料中的 'In progress'）。"""
    table = TableDefinition(
        TableName("Extract", "Extract"),
        [
            TableDefinition.Column("Status", SqlType.text(), collation="en_US_CI"),
            TableDefinition.Column("Code", SqlType.text()),
            TableDefinition.Column("N", SqlType.big_int()),
        ],
    )
    hyper = tmp_path / "ci.hyper"
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(tmp_path)}
        ) as process,
        Connection(process.endpoint, hyper, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        conn.catalog.create_table(table)
        with Inserter(conn, table) as inserter:
            inserter.add_rows(
                [["In progress", "a", 1], ["In Progress", "A", 2], ["Closed", "b", 4]]
            )
            inserter.execute()
    twbx = b.write_twbx(tmp_path, b.workbook(), {"Data/Extracts/test.hyper": hyper.read_bytes()})
    (result,) = import_workbook(twbx, tmp_path / "data", parse_workbook(twbx))
    assert result.table.ok, [c for c in result.table.checks if not c.ok]
    assert {c.name: c.collation for c in result.table.columns}["Status"] == "en_US_CI"
    assert not any("Status" in w for w in result.table.warnings)
    with duckdb.connect(str(tmp_path / "data" / "ds_orders.duckdb"), read_only=True) as con:
        status = 'SELECT sum("N") FROM "Extract"."Extract" WHERE "Status" = \'In Progress\''
        assert con.execute(status).fetchall() == [(3,)]
        groups = 'SELECT count(*) FROM (SELECT "Status" FROM "Extract"."Extract" GROUP BY 1)'
        assert con.execute(groups).fetchall() == [(2,)]
        # 沒有定序的欄位仍分大小寫
        code = 'SELECT sum("N") FROM "Extract"."Extract" WHERE "Code" = \'a\''
        assert con.execute(code).fetchall() == [(1,)]
