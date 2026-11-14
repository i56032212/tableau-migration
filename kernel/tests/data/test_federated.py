"""跨資料庫的 join（Tableau 的 cross-database join）：一個資料源有 Excel 與 PostgreSQL，或兩個不同的
PostgreSQL 資料庫；資料庫的資料表在查詢時取回、與匯入的檔案在本機的 DuckDB 合併（tmig.data.federated）。

以臨時的 PostgreSQL 實跑（沒有 PostgreSQL 時略過；TMIG_REQUIRE_ENGINES=1 時改為失敗），數字與直接計算的
結果比對。"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tmig.data.federated import FederatedCache, referenced_columns, remote_sql, used_columns
from tmig.data.hyper_import import import_workbook
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig
from tmig.ir import (
    IR,
    FileConnection,
    LiveConnection,
    ModelRelation,
    TableRelation,
    has_local_data,
    is_federated,
)
from tmig.ir.federation import single_database, to_federated
from tmig.ir.integrity import check_references
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import QueryState
from tmig.sqlgen.query import preview_sql, worksheet_sql

from ..engines import pg_connect, postgres_cluster
from ..parser import builder as b
from .test_file_sources import (
    EXCEL_CONN,
    ORDERS,
    ORDERS_REL,
    datasource,
    records,
    sheet,
    write_book,
)

TARGETS = [("Region", "string"), ("Target", "real"), ("Manager", "string"), ("Note", "string")]
SALES = [("region", "string"), ("amount", "real"), ("channel", "string")]
CRM = (
    "<named-connection caption='crm' name='postgres.crm'>"
    "<connection authentication='username-password' class='postgres' dbname='crm' "
    "one-time-sql='' port='5432' server='pg.example.com' username='reader' />"
    "</named-connection>"
)
SALES_DB = (
    "<named-connection caption='sales' name='postgres.sales'>"
    "<connection authentication='username-password' class='postgres' dbname='sales' "
    "one-time-sql='' port='5432' server='pg.example.com' username='reader' />"
    "</named-connection>"
)
TARGETS_REL = (
    "<relation connection='postgres.crm' name='targets' table='[public].[targets]' type='table' />"
)
CUSTOM = (
    "<relation connection='postgres.crm' name='Custom SQL Query' type='text'>"
    'SELECT "Region", "Target" FROM public.targets WHERE "Target" &gt;= '
    "&lt;[Parameters].[Parameter 1]&gt; * 100</relation>"
)
CRM_ID = "postgres_pg_example_com_crm"
SALES_ID = "postgres_pg_example_com_sales"


def renamed(columns: list[tuple[str, str]], parent: str, object_id: str = "", **names: str) -> str:
    """metadata-record：與其他資料表同名的欄位，Tableau 在名稱後加上資料表名稱（local-name）。"""
    text = records(columns, parent, object_id)
    for name, local in names.items():
        text = text.replace(
            f"<local-name>[{name}]</local-name>", f"<local-name>[{local}]</local-name>"
        )
    return text


def workbook_xml() -> str:
    # Excel 的 Orders LEFT JOIN PostgreSQL（crm）的 targets
    cross = datasource(
        "federated.cross",
        "Orders+ (Multiple Connections)",
        f"<named-connections>{EXCEL_CONN}{CRM}</named-connections>"
        "<relation join='left' type='join'><clause type='join'><expression op='='>"
        "<expression op='[Orders].[Region]' /><expression op='[targets].[Region]' />"
        f"</expression></clause>{ORDERS_REL}{TARGETS_REL}</relation>"
        f"<metadata-records>{records(ORDERS, 'Orders')}"
        f"{renamed(TARGETS, 'targets', Region='Region (targets)')}</metadata-records>",
    )
    # 兩個 PostgreSQL 資料庫的 Relationship 模型：sales 的 orders 與 crm 的自訂 SQL（含參數）
    sales_rel = "<relation connection='postgres.sales' name='orders' table='[public].[orders]' type='table' />"
    graph = (
        "<object-graph><objects>"
        f"<object caption='orders' id='orders_1'><properties context=''>{sales_rel}</properties>"
        "</object>"
        f"<object caption='Custom SQL Query' id='custom_2'><properties context=''>{CUSTOM}"
        "</properties></object>"
        "</objects><relationships><relationship><expression op='='>"
        "<expression op='[region]' /><expression op='[Region]' /></expression>"
        "<first-end-point object-id='orders_1' /><second-end-point object-id='custom_2' />"
        "</relationship></relationships></object-graph>"
    )
    model = datasource(
        "federated.twodb",
        "orders+ (Multiple Connections)",
        f"<named-connections>{SALES_DB}{CRM}</named-connections>"
        f"<relation type='collection'>{sales_rel}{CUSTOM}</relation>"
        f"<metadata-records>{records(SALES, 'orders', 'orders_1')}"
        f"{records(TARGETS[:2], 'Custom SQL Query', 'custom_2')}</metadata-records>",
        graph,
    )
    # Data Blending 用：只有 Excel 的訂單（主要資料源），與兩個 PostgreSQL 資料庫的 join（沒有檔案）
    orders = datasource(
        "federated.orders",
        "Orders",
        f"<named-connections>{EXCEL_CONN}</named-connections>{ORDERS_REL}"
        f"<metadata-records>{records(ORDERS, 'Orders')}</metadata-records>",
    )
    pgjoin = datasource(
        "federated.pgjoin",
        "orders+targets (Multiple Connections)",
        f"<named-connections>{SALES_DB}{CRM}</named-connections>"
        "<relation join='left' type='join'><clause type='join'><expression op='='>"
        "<expression op='[orders].[region]' /><expression op='[targets].[Region]' />"
        f"</expression></clause>{sales_rel}{TARGETS_REL}</relation>"
        f"<metadata-records>{renamed(SALES, 'orders', region='region (orders)')}"
        f"{records(TARGETS, 'targets')}</metadata-records>",
    )
    sheets = (
        sheet("Cross Targets", "federated.cross", "Region", "Target")
        + sheet("Cross Sales", "federated.cross", "Region", "Sales")
        + sheet("Two Amount", "federated.twodb", "region", "amount")
        + sheet("Two Target", "federated.twodb", "Region", "Target")
        + blend_sheet("Blend Cross", "federated.orders", "Sales", "federated.cross", "Target")
        + blend_sheet("Blend Two", "federated.cross", "Sales", "federated.pgjoin", "amount")
        + blend_sheet("Blend Model", "federated.orders", "Sales", "federated.twodb", "amount")
    )
    return b.workbook(datasource=cross + model + orders + pgjoin, worksheets=sheets)


def blend_sheet(name: str, primary: str, measure: str, secondary: str, other: str) -> str:
    """Data Blending：主要資料源的 Region 與量值，次要資料源（依同名的 Region 自動連結）的量值。"""
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources>
            <datasource caption='Primary' name='{primary}' />
            <datasource caption='Secondary' name='{secondary}' />
          </datasources>
          <datasource-dependencies datasource='{primary}'>
            <column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />
            <column-instance column='[{measure}]' derivation='Sum' name='[sum:{measure}:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
          <datasource-dependencies datasource='{secondary}'>
            <column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />
            <column-instance column='[{other}]' derivation='Sum' name='[sum:{other}:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Bar' /></pane></panes>
        <rows>[{primary}].[none:Region:nk]</rows>
        <cols>([{primary}].[sum:{measure}:qk] + [{secondary}].[sum:{other}:qk])</cols>
      </table>
    </worksheet>"""


@pytest.fixture(scope="module")
def postgres() -> Iterator[dict[str, Any]]:
    """臨時的 PostgreSQL：crm 資料庫的 targets、sales 資料庫的 orders。回傳連線參數（不含資料庫）。"""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    with postgres_cluster() as dsn:
        admin = pg_connect(dsn)
        for name in ("crm", "sales"):
            admin.execute(f"DROP DATABASE IF EXISTS {name}")
            admin.execute(f"CREATE DATABASE {name}")
        admin.close()
        crm = pg_connect(make_conninfo(dsn, dbname="crm"))
        crm.execute(
            'CREATE TABLE targets ("Region" TEXT, "Target" DOUBLE PRECISION, '
            '"Manager" TEXT, "Note" TEXT)'
        )
        crm.execute(
            "INSERT INTO targets VALUES ('East', 1000, 'Amy', 'x'), ('West', 2000, 'Bo', NULL), "
            "('North', 500, 'Cy', 'y')"
        )
        crm.close()
        sales = pg_connect(make_conninfo(dsn, dbname="sales"))
        sales.execute("CREATE TABLE orders (region TEXT, amount DOUBLE PRECISION, channel TEXT)")
        sales.execute(
            "INSERT INTO orders VALUES ('East', 10, 'web'), ('East', 5.5, 'store'), "
            "('West', 7, 'web'), ('South', 1, 'web')"
        )
        sales.close()
        info = conninfo_to_dict(dsn)
        yield {k: v for k, v in info.items() if k in ("host", "port", "user", "password")}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, IR]:
    folder = tmp_path_factory.mktemp("federated")
    book = folder / "Book.xlsx"
    write_book(book)
    twbx = folder / "report.twbx"
    with zipfile.ZipFile(twbx, "w") as archive:
        archive.writestr("report.twb", workbook_xml())
        archive.write(book, "Data/Book.xlsx")
    ir = parse_workbook(twbx)
    import_workbook(twbx, folder / "data", ir)
    return folder, ir


class Spy:
    """記下取回資料庫資料的 SQL。"""

    def __init__(self, runtime: DataRuntime) -> None:
        self.runtime = runtime
        self.calls: list[tuple[str, str]] = []
        self.original = runtime.fetch

    def __call__(self, connection: LiveConnection, sql: str) -> pa.Table:
        self.calls.append((connection.connection_id, sql))
        return self.original(connection, sql)


def runtime_for(postgres: dict[str, Any]) -> DataRuntime:
    configs = {
        key: ConnectionConfig(key, "postgres", {**postgres, "database": database})
        for key, database in ((CRM_ID, "crm"), (SALES_ID, "sales"))
    }
    return DataRuntime(configs)


def run(
    runtime: DataRuntime, ir: IR, folder: Path, worksheet: str, state: QueryState | None = None
) -> dict[Any, Any]:
    ws = next(w for w in ir.worksheets if w.name == worksheet)
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    result = runtime.run(worksheet_sql(ir, ws.id, state), ds, folder / "data" / f"{ds.id}.duckdb")
    return {row[0]: row[1] for row in result.rows}


def test_parse(built: tuple[Path, IR]) -> None:
    _, ir = built
    assert ir.unsupported == []
    assert check_references(ir) == []
    cross, model, *_ = ir.datasources
    connection = cross.connection
    assert isinstance(connection, FileConnection) and is_federated(connection)
    assert has_local_data(connection)
    excel, targets = connection.tables
    assert (excel.format, targets.format) == ("excel", "live")
    assert targets.connection is not None and targets.connection.connection_id == CRM_ID
    assert targets.connection.relation is None
    assert (targets.table, targets.path) == (["public", "targets"], f"live:{CRM_ID}")
    assert [c.name for c in targets.columns] == ["Region", "Target", "Manager", "Note"]
    region = next(f for f in cross.fields if f.caption == "Region (targets)")
    assert (region.table, region.column) == ("targets", "Region")
    # 兩個資料庫：只有資料庫的資料表，不需要匯入
    connection = model.connection
    assert isinstance(connection, FileConnection) and not has_local_data(connection)
    assert isinstance(connection.relation, ModelRelation)
    orders, custom = connection.tables
    assert orders.connection is not None and orders.connection.connection_id == SALES_ID
    assert custom.connection is not None and custom.connection.connection_id == CRM_ID
    assert custom.sql is not None and "<[Parameters].[Parameter 1]>" in custom.sql
    assert custom.table is None


def test_excel_and_postgres(built: tuple[Path, IR], postgres: dict[str, Any]) -> None:
    folder, ir = built
    with runtime_for(postgres) as runtime:
        spy = Spy(runtime)
        runtime.fetch = spy  # type: ignore[method-assign]
        # Orders：O1 East 100.5、O2 West 200、O3 East 50.25、O4（沒有地區）"n/a"；LEFT JOIN 依地區
        assert run(runtime, ir, folder, "Cross Targets") == {
            "East": 2000.0,
            "West": 2000.0,
            None: None,
        }
        assert run(runtime, ir, folder, "Cross Sales") == {
            "East": 150.75,
            "West": 200.0,
            None: None,
        }
        # 只取用到的欄（Manager、Note 沒有用到）；第二張工作表只用到 join 的欄，取回的資料在 TTL 之內重複使用
        assert spy.calls == [(CRM_ID, 'SELECT "Region", "Target" FROM "public"."targets"')]
        # 資料的預覽（全部的欄）
        preview = preview_sql(ir, ir.datasources[0].id, None, 10)
        ds = ir.datasources[0]
        found = runtime.run_sql(preview.sql, ds, folder / "data" / f"{ds.id}.duckdb")
        assert len(found.rows) == 4
        assert any("Manager" in sql for _, sql in spy.calls[1:])
        # TTL 為 0：每次都重新取回
        runtime.federated = FederatedCache(0)
        before = len(spy.calls)
        run(runtime, ir, folder, "Cross Targets")
        run(runtime, ir, folder, "Cross Targets")
        assert len(spy.calls) == before + 2


def test_two_databases(built: tuple[Path, IR], postgres: dict[str, Any]) -> None:
    folder, ir = built
    with runtime_for(postgres) as runtime:
        # Relationship 模型：orders（sales 資料庫）與自訂 SQL（crm 資料庫，Target >= 參數 × 100）
        assert run(runtime, ir, folder, "Two Amount") == {
            "East": 15.5,
            "South": 1.0,
            "West": 7.0,
        }
        assert run(runtime, ir, folder, "Two Target") == {
            "East": 1000.0,
            "North": 500.0,
            "West": 2000.0,
        }
        # 參數改成 15：自訂 SQL 代入新的值（Target >= 1500）
        param = next(p for p in ir.parameters if p.caption == "Top N")
        state = QueryState(parameters={param.id: 15})
        assert run(runtime, ir, folder, "Two Target", state) == {"West": 2000.0}


def test_used_columns() -> None:
    from tmig.ir import FileColumn, FileTable

    live = LiveConnection(kind="live", connection_id="x", dbclass="postgres", dialect="postgres")
    table = FileTable(
        name="t",
        format="live",
        path="live:x",
        connection=live,
        table=["public", "t"],
        columns=[FileColumn(name=n, datatype="string") for n in ("A", "b", "Unused")],
    )
    other = table.model_copy(update={"name": "u"})
    connection = FileConnection(
        kind="file",
        tables=[table, other],
        relation=TableRelation(kind="table", name="t", table=["Files", "t"]),
    )

    def used(sql: str, which: FileTable = table) -> list[str] | None:
        found = used_columns(which, referenced_columns([sql], connection))
        return None if found is None else [c.name for c in found]

    # CTE 中沒有用到的欄不算（base 選出全部的欄，之後只用到 a、b）
    sql = (
        'WITH base AS (SELECT "t"."A" AS x, "t"."b" AS y, "t"."Unused" AS z FROM "Files"."t" AS "t") '
        "SELECT x, count(*) FROM (SELECT * FROM base) AS s WHERE y = 'k' GROUP BY 1"
    )
    assert used(sql) == ["A", "b"]
    assert used(sql, other) is None  # 沒有用到的資料表不取回
    # SELECT * 依記下的欄展開；只算列數時取第一欄
    assert used('SELECT * FROM "Files"."t"') == ["A", "b", "Unused"]
    assert used('SELECT count(*) FROM "Files"."t"') == ["A"]
    # 分析不了：全部的欄
    assert used_columns(table, None) == table.columns
    assert remote_sql(table, table.columns[:1]) == 'SELECT "A" FROM "public"."t"'


def test_single_database_round_trip(built: tuple[Path, IR]) -> None:
    """跨資料庫的 join 只剩同一個資料庫時改回 Live 連線；Live 連線也能轉成跨資料庫的 join。"""
    _, ir = built
    model = ir.datasources[1].connection
    assert isinstance(model, FileConnection)
    assert single_database(model) is None  # 兩個資料庫
    only = model.model_copy(
        update={
            "tables": [
                t.model_copy(update={"connection": model.tables[1].connection})
                for t in model.tables
            ]
        }
    )
    live = single_database(only)
    assert isinstance(live, LiveConnection) and isinstance(live.relation, ModelRelation)
    leaves = [t.relation for t in live.relation.tables]
    assert [leaf.kind for leaf in leaves] == ["table", "sql"]
    back = to_federated(live, lambda name: next(t for t in only.tables if t.name == name).columns)
    assert [t.format for t in back.tables] == ["live", "live"]
    assert single_database(back) == live


def blended(runtime: DataRuntime, ir: IR, folder: Path, worksheet: str) -> dict[Any, Any]:
    ws = next(w for w in ir.worksheets if w.name == worksheet)
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    query = worksheet_sql(ir, ws.id)
    result = runtime.run(query, ds, folder / "data" / f"{ds.id}.duckdb")
    return {row[0]: tuple(row[1:]) for row in result.rows}


def test_blending_with_cross_database_secondary(
    built: tuple[Path, IR], postgres: dict[str, Any]
) -> None:
    """Data Blending 的次要資料源是跨資料庫的 join：次要資料源依連結欄位（Region）彙總後 LEFT JOIN；資料庫的
    資料表只取次要資料源的查詢用到的欄。"""
    folder, ir = built
    blend = next(w for w in ir.worksheets if w.name == "Blend Cross")
    assert [b.datasource for b in blend.blends] == [ir.datasources[0].id]
    query = worksheet_sql(ir, blend.id)
    # 次要資料源的查詢另外執行（跨資料庫的 join），結果以資料表 __s0 併入
    (stage,) = query.stages
    assert (stage.name, stage.datasource.id) == ("__s0", ir.datasources[0].id)
    with runtime_for(postgres) as runtime:
        spy = Spy(runtime)
        runtime.fetch = spy  # type: ignore[method-assign]
        # 主要資料源（只有 Excel）：O1 East 100.5、O2 West 200、O3 East 50.25、O4（沒有地區）"n/a"；
        # 次要資料源：訂單 LEFT JOIN 目標，依地區彙總目標（East 兩張訂單 × 1000）
        assert blended(runtime, ir, folder, "Blend Cross") == {
            "East": (150.75, 2000.0),
            "West": (200.0, 2000.0),
            None: (None, None),
        }
        assert spy.calls == [(CRM_ID, 'SELECT "Region", "Target" FROM "public"."targets"')]
        # 主要資料源也是跨資料庫的 join，次要資料源是兩個 PostgreSQL 資料庫的 join（沒有檔案）：
        # 訂單（sales）LEFT JOIN 目標（crm），依目標的地區彙總金額（South 沒有目標，在 Null 之下）
        assert blended(runtime, ir, folder, "Blend Two") == {
            "East": (150.75, 15.5),
            "West": (200.0, 7.0),
            None: (None, 1.0),
        }
        assert (SALES_ID, 'SELECT "region", "amount" FROM "public"."orders"') in spy.calls


def test_blending_with_relationship_model_secondary(
    built: tuple[Path, IR], postgres: dict[str, Any]
) -> None:
    """次要資料源是 Relationship 模型（兩個 PostgreSQL 資料庫的 orders 與自訂 SQL）：與 Tableau 相同另外查詢，
    依連結欄位（自訂 SQL 的 Region）以關聯的語意彙總 amount，再與主要資料源合併。"""
    folder, ir = built
    blend = next(w for w in ir.worksheets if w.name == "Blend Model")
    query = worksheet_sql(ir, blend.id)
    assert [stage.name for stage in query.stages] == ["__s0"]
    with runtime_for(postgres) as runtime:
        # 依關聯：East 15.5、West 7；South 的訂單沒有對應的地區（Null，連結欄位的 Null 視為相等）
        assert blended(runtime, ir, folder, "Blend Model") == {
            "East": (150.75, 15.5),
            "West": (200.0, 7.0),
            None: (None, 1.0),
        }
