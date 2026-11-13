"""桌面版的跨資料庫的 join（tmig.authoring）：檔案的資料源加上資料庫的資料表、資料庫的資料源加上檔案或另一個
資料庫的資料表；移除後只剩同一個資料庫時改回 Live 連線。以 DuckDB 當作兩個來源資料庫，匯入檔案後查詢比對。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from openpyxl import Workbook

from tmig.authoring import (
    add_live_table,
    add_table,
    federate,
    inspect_file,
    new_live_workbook,
    new_workbook,
    remove_table,
    set_custom_sql,
    update_field,
)
from tmig.data.hyper_import import import_sources
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig
from tmig.edit import EditError, replace_worksheet
from tmig.ir import (
    IR,
    FileColumn,
    FileConnection,
    LiveConnection,
    ModelRelation,
    TableRelation,
    has_local_data,
    is_federated,
)
from tmig.ir.integrity import check_references
from tmig.sqlgen.query import worksheet_sql

SALES = LiveConnection(
    kind="live", connection_id="duckdb_sales", dbclass="duckdb", dialect="duckdb", database="sales"
)
CRM = LiveConnection(
    kind="live", connection_id="duckdb_crm", dbclass="duckdb", dialect="duckdb", database="crm"
)
ORDERS = [
    FileColumn(name="order_id", datatype="string"),
    FileColumn(name="region", datatype="string"),
    FileColumn(name="sales", datatype="real"),
]
MANAGERS = [
    FileColumn(name="region", datatype="string"),
    FileColumn(name="manager", datatype="string"),
]


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    sales = duckdb.connect(str(tmp_path / "sales.duckdb"))
    sales.execute("CREATE SCHEMA s")
    sales.execute("CREATE TABLE s.orders (order_id VARCHAR, region VARCHAR, sales DOUBLE)")
    sales.execute(
        "INSERT INTO s.orders VALUES ('O1', 'East', 100.5), ('O2', 'West', 200), "
        "('O3', 'East', 50.25), ('O4', 'South', 75)"
    )
    sales.close()
    crm = duckdb.connect(str(tmp_path / "crm.duckdb"))
    crm.execute("CREATE SCHEMA c")
    crm.execute("CREATE TABLE c.managers (region VARCHAR, manager VARCHAR)")
    crm.execute("INSERT INTO c.managers VALUES ('East', 'Ann'), ('West', 'Bob'), ('North', 'Cid')")
    crm.execute("CREATE TABLE c.targets (region VARCHAR, target DOUBLE)")
    crm.execute("INSERT INTO c.targets VALUES ('East', 1000), ('West', 2000), ('South', 500)")
    crm.close()
    data = tmp_path / "sources" / "Data"
    data.mkdir(parents=True)
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Targets"
    for row in (("Region", "Target"), ("East", 10.0), ("West", 20.0), ("North", 5.0)):
        sheet.append(row)
    book.save(data / "targets.xlsx")
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Orders"
    for row in (
        ("Order", "Region", "Amount"),
        ("A", "East", 1.5),
        ("B", "West", 2.0),
        ("C", "East", 3.0),
    ):
        sheet.append(row)
    book.save(data / "orders.xlsx")
    return tmp_path


@pytest.fixture
def runtime(folder: Path) -> Iterator[DataRuntime]:
    configs = {
        key: ConnectionConfig(
            key, "duckdb", {"driver": "duckdb", "path": str(folder / f"{db}.duckdb")}
        )
        for key, db in (("duckdb_sales", "sales"), ("duckdb_crm", "crm"))
    }
    with DataRuntime(configs) as found:
        yield found


def ids(ir: IR) -> dict[str, str]:
    return {f.caption: f.id for f in ir.datasources[0].fields}


def query(ir: IR, runtime: DataRuntime, folder: Path, rows: str, measure: str) -> dict[Any, Any]:
    assert check_references(ir) == []
    ds = ir.datasources[0]
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        datasource=ds.id, rows=[{"field": rows}], columns=[{"field": measure, "agg": "sum"}]
    )
    ir, ws = replace_worksheet(ir, sheet)
    if has_local_data(ds.connection):
        (result,) = import_sources(folder / "sources", folder / "data", ir, only=[ds.id])
        assert result.ok, result.to_json()
    found = runtime.run(worksheet_sql(ir, ws.id), ds, folder / "data" / f"{ds.id}.duckdb")
    return {row[0]: row[1] for row in found.rows}


def test_database_plus_file(folder: Path, runtime: DataRuntime) -> None:
    """資料庫的資料源加上 Excel 的資料表（關聯）：改成跨資料庫的 join；移除後改回 Live 連線。"""
    ir = new_live_workbook("Sales", SALES, ["s", "orders"], None, ORDERS)
    ds = ir.datasources[0].id
    f = ids(ir)
    targets = inspect_file(folder / "sources" / "Data" / "targets.xlsx")
    ir = add_table(
        ir, ds, targets, "Targets", "Data/targets.xlsx", on=[(f["region"], "=", "Region")]
    )
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection) and is_federated(connection)
    orders, sheet = connection.tables
    assert (orders.format, orders.table, sheet.format) == ("live", ["s", "orders"], "excel")
    assert orders.connection is not None and orders.connection.connection_id == "duckdb_sales"
    assert [c.name for c in orders.columns] == ["order_id", "region", "sales"]
    f = ids(ir)
    assert query(ir, runtime, folder, f["region"], f["sales"]) == {
        "East": 150.75,
        "South": 75.0,
        "West": 200.0,
    }
    # 關聯：Targets 依自己的詳細程度彙總（North 沒有訂單也在）
    assert query(ir, runtime, folder, f["Region (Targets)"], f["Target"]) == {
        "East": 10.0,
        "North": 5.0,
        "West": 20.0,
    }
    top = connection.relation
    assert isinstance(top, ModelRelation)
    ir = remove_table(ir, ds, top.tables[1].id)
    back = ir.datasources[0].connection
    assert isinstance(back, LiveConnection) and back.connection_id == "duckdb_sales"
    assert back.relation == TableRelation(kind="table", name="orders", table=["s", "orders"])


def test_file_plus_databases(folder: Path, runtime: DataRuntime) -> None:
    """Excel 的資料源 LEFT JOIN 資料庫的資料表，再以關聯加上另一段自訂 SQL；修改自訂 SQL 與資料類型。"""
    source = inspect_file(folder / "sources" / "Data" / "orders.xlsx")
    ir = new_workbook("Orders", source, "Orders", "Data/orders.xlsx")
    ds = ir.datasources[0].id
    f = ids(ir)
    with pytest.raises(EditError, match="要指定資料庫的連線"):
        add_live_table(
            ir, ds, ["c", "managers"], None, MANAGERS, how="join", on=[(f["Region"], "=", "region")]
        )
    # 跨連線的聯集：不行
    with pytest.raises(EditError, match="同一個連線"):
        add_live_table(
            ir, ds, ["c", "targets"], None, MANAGERS, how="union", target=None, connection=CRM
        )
    ir = add_live_table(
        ir,
        ds,
        ["c", "managers"],
        None,
        MANAGERS,
        how="join",
        join="left",
        on=[(f["Region"], "=", "region")],
        connection=CRM,
    )
    f = ids(ir)
    assert query(ir, runtime, folder, f["manager"], f["Amount"]) == {"Ann": 4.5, "Bob": 2.0}
    # 自訂 SQL（另一個資料庫的資料表也可以，這裡是 crm 的 targets）
    columns = [
        FileColumn(name="region", datatype="string"),
        FileColumn(name="target", datatype="real"),
    ]
    ir = add_live_table(
        ir,
        ds,
        None,
        "SELECT region, target FROM c.targets",
        columns,
        on=[(f["Region"], "=", "region")],
        connection=CRM,
    )
    f = ids(ir)
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection)
    custom = next(t for t in connection.tables if t.sql is not None)
    target = next(i for caption, i in f.items() if caption.startswith("target"))
    region = next(i for caption, i in f.items() if caption.startswith("region (Custom"))
    assert query(ir, runtime, folder, region, target) == {
        "East": 1000.0,
        "South": 500.0,
        "West": 2000.0,
    }
    ir = set_custom_sql(
        ir, ds, custom.name, "SELECT region, target * 2 AS target FROM c.targets", columns
    )
    assert query(ir, runtime, folder, region, target) == {
        "East": 2000.0,
        "South": 1000.0,
        "West": 4000.0,
    }
    # 資料庫的欄位也可以變更資料類型（取回後轉換）
    ir = update_field(ir, ds, target, datatype="integer")
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection)
    custom = next(t for t in connection.tables if t.sql is not None)
    assert [c.datatype for c in custom.columns] == ["string", "integer"]
    assert query(ir, runtime, folder, region, target) == {"East": 2000, "South": 1000, "West": 4000}


def test_two_databases(folder: Path, runtime: DataRuntime) -> None:
    """資料庫的資料源 join 另一個資料庫的資料表：不需要匯入檔案；移除後改回 Live 連線。"""
    ir = new_live_workbook("Sales", SALES, ["s", "orders"], None, ORDERS)
    ds = ir.datasources[0].id
    f = ids(ir)
    # 同一個資料庫（省略 connection 或相同的連線）仍是 Live 連線
    same = add_live_table(
        ir,
        ds,
        ["s", "orders"],
        None,
        ORDERS,
        how="join",
        on=[(f["order_id"], "=", "order_id")],
        connection=SALES,
    )
    assert isinstance(same.datasources[0].connection, LiveConnection)
    ir = add_live_table(
        ir,
        ds,
        ["c", "managers"],
        None,
        MANAGERS,
        how="join",
        on=[(f["region"], "=", "region")],
        connection=CRM,
    )
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection) and not has_local_data(connection)
    f = ids(ir)
    assert query(ir, runtime, folder, f["manager"], f["sales"]) == {"Ann": 150.75, "Bob": 200.0}
    ir = remove_table(ir, ds, "managers")
    back = ir.datasources[0].connection
    assert isinstance(back, LiveConnection) and back.connection_id == "duckdb_sales"
    assert query(ir, runtime, folder, f["region"], f["sales"]) == {
        "East": 150.75,
        "South": 75.0,
        "West": 200.0,
    }


def test_federate_uses_given_columns(folder: Path) -> None:
    ir = new_live_workbook("Sales", SALES, ["s", "orders"], None, ORDERS[:2])
    ds = ir.datasources[0].id
    # 從資料庫取得的欄（比資料源的欄位多）優先
    federated = federate(ir, ds, {"orders": ORDERS})
    connection = federated.datasources[0].connection
    assert isinstance(connection, FileConnection)
    assert [c.name for c in connection.tables[0].columns] == ["order_id", "region", "sales"]
    # 沒有提供時取自資料源的欄位
    plain = federate(ir, ds).datasources[0].connection
    assert isinstance(plain, FileConnection)
    assert [c.name for c in plain.tables[0].columns] == ["order_id", "region"]
    assert federate(federated, ds) == federated
