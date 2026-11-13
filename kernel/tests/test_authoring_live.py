"""連到資料庫的資料源（桌面版的「連線到伺服器」）：列出資料表與欄位（tmig.data.catalog）、新報表、
關聯、join、聯集、樞紐與自訂 SQL（tmig.authoring）；以 DuckDB 當作來源資料庫查詢比對。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.authoring import (
    add_live_datasource,
    add_live_table,
    new_live_workbook,
    pivot,
    remove_table,
    set_custom_sql,
)
from tmig.data.catalog import (
    CONNECTORS,
    list_tables,
    sql_columns,
    table_columns,
    tableau_type,
)
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig, DuckDBExecutor
from tmig.edit import EditError, replace_worksheet
from tmig.ir import (
    IR,
    FileConnection,
    LiveConnection,
    ModelRelation,
    SqlRelation,
    UnionRelation,
    is_federated,
)
from tmig.ir.integrity import check_references
from tmig.sqlgen.query import worksheet_sql

CONNECTION = LiveConnection(
    kind="live",
    connection_id="duckdb_sales",
    dbclass="duckdb",
    dialect="duckdb",
    database="sales",
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "sales.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA crm")
    con.execute(
        "CREATE TABLE crm.orders (order_id VARCHAR, region VARCHAR, order_date DATE, "
        "sales DECIMAL(10, 2), quantity INTEGER, rush BOOLEAN, shipped TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO crm.orders VALUES "
        "('O1', 'East', '2024-01-05', 100.5, 2, TRUE, '2024-01-06 09:30:00'), "
        "('O2', 'West', '2024-02-01', 200, 1, FALSE, NULL), "
        "('O3', 'East', '2024-03-09', 50.25, 3, FALSE, NULL), "
        "('O4', 'South', '2024-04-02', 75, 1, TRUE, NULL)"
    )
    con.execute("CREATE TABLE crm.people (region VARCHAR, manager VARCHAR)")
    con.execute("INSERT INTO crm.people VALUES ('East', 'Ann'), ('West', 'Bob'), ('North', 'Cid')")
    con.execute("CREATE TABLE crm.returns (order_id VARCHAR, returned VARCHAR)")
    con.execute("INSERT INTO crm.returns VALUES ('O1', 'Yes'), ('O4', 'Yes')")
    con.execute(
        "CREATE TABLE crm.orders_2025 (order_id VARCHAR, region VARCHAR, sales DOUBLE, note VARCHAR)"
    )
    con.execute("INSERT INTO crm.orders_2025 VALUES ('P1', 'North', 7, 'new')")
    con.execute("CREATE VIEW crm.east AS SELECT * FROM crm.orders WHERE region = 'East'")
    con.close()
    return path


@pytest.fixture
def runtime(database: Path) -> Any:
    configs = {
        "duckdb_sales": ConnectionConfig(
            "duckdb_sales", "duckdb", {"driver": "duckdb", "path": str(database)}
        )
    }
    with DataRuntime(configs) as runtime:
        yield runtime


def ids(ir: IR, ds: int = 0) -> dict[str, str]:
    return {f.caption: f.id for f in ir.datasources[ds].fields}


def query(ir: IR, runtime: Any, rows: str, measure: str, ds: int = 0) -> dict[Any, Any]:
    assert check_references(ir) == []
    datasource = ir.datasources[ds]
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        datasource=datasource.id,
        rows=[{"field": rows}],
        columns=[{"field": measure, "agg": "sum"}],
    )
    ir, ws = replace_worksheet(ir, sheet)
    result = runtime.run(worksheet_sql(ir, ws.id), datasource, Path("unused"))
    return {row[0]: row[1] for row in result.rows}


def test_catalog(database: Path) -> None:
    executor = DuckDBExecutor(database)
    try:
        tables = list_tables(executor, "duckdb")
        names = [(t.schema, t.name, t.kind) for t in tables]
        assert ("crm", "orders", "table") in names and ("crm", "east", "view") in names
        assert next(t.path for t in list_tables(executor, "duckdb", schema="crm")) == [
            "crm",
            "east",
        ]
        columns = table_columns(executor, "duckdb", ["crm", "orders"])
        assert [(c.name, c.datatype) for c in columns] == [
            ("order_id", "string"),
            ("region", "string"),
            ("order_date", "date"),
            ("sales", "real"),
            ("quantity", "integer"),
            ("rush", "boolean"),
            ("shipped", "datetime"),
        ]
        custom = sql_columns(
            executor,
            "duckdb",
            "SELECT region, SUM(sales) AS total, COUNT(*) AS n FROM crm.orders GROUP BY region;",
        )
        assert [(c.name, c.datatype) for c in custom] == [
            ("region", "string"),
            ("total", "real"),
            ("n", "integer"),
        ]
    finally:
        executor.close()
    assert tableau_type("NUMBER", 0) == "integer" and tableau_type("NUMBER(10,2)") == "real"
    assert tableau_type("character varying(20)") == "string"
    assert tableau_type("timestamp with time zone") == "datetime"
    assert tableau_type("decimal(18,0)") == "integer" and tableau_type("bit") == "boolean"
    # 資料庫的空間欄位：PostGIS、Oracle、SAP HANA、Db2；MySQL 的 point 等只有 MySQL 才算
    # （PostgreSQL 內建的 point 是幾何型別，不是 PostGIS 的空間欄位）
    for name in ("geometry", "GEOGRAPHY", "SDO_GEOMETRY", "ST_GEOMETRY", "ST_POINT"):
        assert tableau_type(name) == "spatial", name
    assert tableau_type("point", None, "mysql") == "spatial"
    assert tableau_type("point", None, "postgres") == "string"
    assert CONNECTORS["postgres"].port == 5432 and CONNECTORS["databricks"].secret == "access_token"


def test_tables_relationships_and_joins(database: Path, runtime: Any) -> None:
    executor = DuckDBExecutor(database)
    columns = {
        name: table_columns(executor, "duckdb", ["crm", name])
        for name in ("orders", "people", "returns", "orders_2025")
    }
    executor.close()
    ir = new_live_workbook("Sales", CONNECTION, ["crm", "orders"], None, columns["orders"])
    ds = ir.datasources[0]
    assert ds.caption == "orders (sales)" and ds.connection.kind == "live"
    f = ids(ir)
    assert query(ir, runtime, f["region"], f["sales"]) == {"East": 150.75, "West": 200, "South": 75}
    # 關聯：people 成為另一張邏輯資料表
    ir = add_live_table(
        ir,
        ds.id,
        ["crm", "people"],
        None,
        columns["people"],
        "relationship",
        on=[(f["region"], "=", "region")],
    )
    assert isinstance(ir.datasources[0].connection.relation, ModelRelation)  # type: ignore[union-attr]
    f = ids(ir)
    assert query(ir, runtime, f["manager"], f["sales"]) == {
        "Ann": 150.75,
        "Bob": 200,
        "Cid": None,
        None: 75,
    }
    # 邏輯資料表 orders 中 join returns
    model = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(model, ModelRelation)
    ir = add_live_table(
        ir,
        ds.id,
        ["crm", "returns"],
        None,
        columns["returns"],
        "join",
        target=model.tables[0].id,
        join="left",
        on=[(f["order_id"], "=", "order_id")],
    )
    f = ids(ir)
    assert query(ir, runtime, f["returned"], f["sales"]) == {"Yes": 175.5, None: 250.25}
    # 加上檔案：改成跨資料庫的 join（見 tests/test_authoring_federated.py）
    from tmig.authoring import add_table, inspect_file

    csv = database.parent / "x.csv"
    csv.write_text("region,goal\nEast,1\n", encoding="utf-8")
    with pytest.raises(EditError, match="要指定關聯或 join 的條件"):
        add_table(ir, ds.id, inspect_file(csv), None, "Data/x.csv")
    joined = add_table(
        ir, ds.id, inspect_file(csv), None, "Data/x.csv", on=[(f["region"], "=", "region")]
    )
    connection = joined.datasources[0].connection
    assert isinstance(connection, FileConnection) and is_federated(connection)
    assert [t.format for t in connection.tables] == ["live", "live", "live", "text"]


def test_union_pivot_and_custom_sql(database: Path, runtime: Any) -> None:
    executor = DuckDBExecutor(database)
    orders = table_columns(executor, "duckdb", ["crm", "orders"])
    later = table_columns(executor, "duckdb", ["crm", "orders_2025"])
    sql = "SELECT region, SUM(sales) AS total FROM crm.orders GROUP BY region"
    custom = sql_columns(executor, "duckdb", sql)
    executor.close()
    ir = new_live_workbook("Sales", CONNECTION, ["crm", "orders"], None, orders)
    ds = ir.datasources[0].id
    # 聯集：沒有的欄為 Null，另有 Table Name
    ir = add_live_table(ir, ds, ["crm", "orders_2025"], None, later, "union")
    assert isinstance(ir.datasources[0].connection.relation, UnionRelation)  # type: ignore[union-attr]
    f = ids(ir)
    assert query(ir, runtime, f["Table Name"], f["sales"]) == {"orders": 425.75, "orders_2025": 7}
    assert query(ir, runtime, f["note"], f["quantity"]) == {None: 7, "new": None}
    ir = remove_table(ir, ds, "orders_2025")
    # 樞紐：sales 與 quantity 轉成列（資料庫中以 UNION ALL 計算）
    ir = pivot(ir, ds, "orders", ["sales", "quantity"])
    f = ids(ir)
    assert query(ir, runtime, f["Pivot Field Names"], f["Pivot Field Values"]) == {
        "sales": 425.75,
        "quantity": 7,
    }
    # 自訂 SQL 的資料源；修改 SQL 後新的欄加入、型別依新的查詢
    ir, added = add_live_datasource(ir, CONNECTION, None, sql, custom)
    assert isinstance(added.connection.relation, SqlRelation)  # type: ignore[union-attr]
    assert added.connection.relation.name == "Custom SQL Query"  # type: ignore[union-attr]
    f = ids(ir, 1)
    assert query(ir, runtime, f["region"], f["total"], ds=1) == {
        "East": 150.75,
        "West": 200,
        "South": 75,
    }
    edited = (
        "SELECT region, SUM(sales) AS total, COUNT(*) AS orders FROM crm.orders GROUP BY region"
    )
    executor = DuckDBExecutor(database)
    new_columns = sql_columns(executor, "duckdb", edited)
    executor.close()
    ir = set_custom_sql(ir, added.id, "Custom SQL Query", edited, new_columns)
    f = ids(ir, 1)
    assert "orders" in f
    assert query(ir, runtime, f["region"], f["orders"], ds=1) == {"East": 2, "West": 1, "South": 1}
    with pytest.raises(EditError, match="不是自訂 SQL"):
        set_custom_sql(ir, ds, "orders", edited, new_columns)
    with pytest.raises(EditError, match="自訂 SQL 不能聯集"):
        add_live_table(ir, added.id, None, sql, custom, "union")
