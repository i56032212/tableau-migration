"""第三階段 N7：萃取層——從來源資料庫更新萃取（完整、增量、原子替換）與資料比對（階段二）。

報表的萃取來自 Oracle 的 ORDERS left join PEOPLE（地區相同），萃取篩選排除 South；萃取中兩張表的
同名欄位 REGION 存成 REGION 與 REGION1（Tableau 的寫法，metadata-record 的 family 與 remote-alias
記下來源）。Tableau 的萃取（.hyper）以同一份資料建立，作為比對的基準。

來源資料庫以 DuckDB 檔、PostgreSQL、Spark 與模擬的 Oracle 執行；增量與修改資料的情境以 DuckDB 檔與
PostgreSQL 執行。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
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

from tmig.cli import main
from tmig.data.executor import execute_query
from tmig.data.extract_refresh import (
    RefreshError,
    compare_extract,
    refresh_extract,
    refresh_plan,
)
from tmig.data.hyper_import import import_workbook
from tmig.data.sources import DuckDBExecutor, Executor
from tmig.ir import IR, ExtractConnection, JoinRelation, LiveConnection
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..engines import (
    OracleEmulator,
    SparkExecutor,
    pg_connect,
    pg_load,
    postgres_cluster,
    postgres_executor,
    spark_session,
)
from ..parser import builder as b
from ..parser.builder import DS, instance, ref, worksheet

ORDERS = pa.table(
    {
        "ROW_ID": pa.array([1, 2, 3, 4, 5], pa.int64()),
        "ORDER_ID": ["A1", "A2", "A3", "A4", "A5"],
        "REGION": ["East", "East", "West", "South", None],
        "SALES": [100.0, 50.0, 70.0, 30.0, 10.0],
        "QUANTITY": pa.array([1, 2, 3, 4, 5], pa.int64()),
        "ORDER_DATE": [dt.date(2024, 1, d) for d in range(1, 6)],
    }
)
PEOPLE = pa.table({"REGION": ["East", "West", "Central"], "MANAGER": ["Ann", "Bob", "Cid"]})

# 萃取中的欄名、IR 的欄位名稱、型別、聚合、來源資料表、來源欄名
COLUMNS = [
    ("ROW_ID", "Row ID", "integer", "Sum", "ORDERS", "ROW_ID"),
    ("ORDER_ID", "Order ID", "string", "Count", "ORDERS", "ORDER_ID"),
    ("REGION", "Region", "string", "Count", "ORDERS", "REGION"),
    ("SALES", "Sales", "real", "Sum", "ORDERS", "SALES"),
    ("QUANTITY", "Quantity", "integer", "Sum", "ORDERS", "QUANTITY"),
    ("ORDER_DATE", "Order Date", "date", "Year", "ORDERS", "ORDER_DATE"),
    ("REGION1", "Region (People)", "string", "Count", "PEOPLE", "REGION"),
    ("MANAGER", "Manager", "string", "Count", "PEOPLE", "MANAGER"),
]
# Tableau 的萃取：join 後排除 South
SNAPSHOT = [
    [1, "A1", "East", 100.0, 1, dt.date(2024, 1, 1), "East", "Ann"],
    [2, "A2", "East", 50.0, 2, dt.date(2024, 1, 2), "East", "Ann"],
    [3, "A3", "West", 70.0, 3, dt.date(2024, 1, 3), "West", "Bob"],
    [5, "A5", None, 10.0, 5, dt.date(2024, 1, 5), None, None],
]
ORACLE = (
    "<connection authentication='username-password' class='oracle' port='1521' "
    "schema='SALES' server='ora.example.com' service='ORCLPDB' />"
)
JOIN = """
  <relation join='left' type='join'>
    <clause type='join'>
      <expression op='='>
        <expression op='[ORDERS].[REGION]' />
        <expression op='[PEOPLE].[REGION]' />
      </expression>
    </clause>
    <relation connection='db.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />
    <relation connection='db.1' name='PEOPLE' table='[SALES].[PEOPLE]' type='table' />
  </relation>"""


def live_records() -> str:
    items = [
        f"""<metadata-record class='column'>
          <remote-name>{source}</remote-name><remote-type>129</remote-type>
          <local-name>[{local}]</local-name><parent-name>[{table}]</parent-name>
          <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
        </metadata-record>"""
        for _, local, datatype, agg, table, source in COLUMNS
    ]
    return f"<metadata-records>{''.join(items)}</metadata-records>"


def extract_records() -> str:
    items = [
        f"""<metadata-record class='column'>
          <remote-name>{name}</remote-name><remote-type>129</remote-type>
          <local-name>[{local}]</local-name><parent-name>[Extract]</parent-name>
          <remote-alias>{source}</remote-alias><family>{table}</family>
          <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
        </metadata-record>"""
        for name, local, datatype, agg, table, source in COLUMNS
    ]
    return f"<metadata-records>{''.join(items)}</metadata-records>"


EXCLUDE_SOUTH = """
      <filter class='categorical' column='[Region]'>
        <groupfilter function='except'>
          <groupfilter function='level-members' level='[Region]' />
          <groupfilter function='member' level='[Region]' member='&quot;South&quot;' />
        </groupfilter>
      </filter>"""


def datasource(count: str = "-1", increment: bool = True) -> str:
    refresh = (
        "<refresh increment-key='[Row ID]' incremental-updates='true'>"
        "<refresh-event increment-value='%null%' refresh-type='create' rows-inserted='4' /></refresh>"
        if increment
        else ""
    )
    return f"""
    <datasource caption='Orders' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='db' name='db.1'>{ORACLE}</named-connection>
        </named-connections>
        {JOIN}
        {live_records()}
      </connection>
      <extract count='{count}' enabled='true' units='records'>
        <connection class='hyper' dbname='Data/Extracts/orders.hyper' schema='Extract' tablename='Extract'>
          <relation name='Extract' table='[Extract].[Extract]' type='table' />
          {refresh}
          {extract_records()}
        </connection>
        {EXCLUDE_SOUTH}
      </extract>
      <aliases enabled='yes' />
    </datasource>"""


def sheet() -> str:
    return worksheet(
        "Sales by Manager",
        [
            instance("[Manager]", "None", "[none:Manager:nk]"),
            instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
        ],
        rows=ref("none:Manager:nk"),
        cols=ref("sum:Sales:qk"),
    )


def make_hyper(path: Path, log_dir: Path, rows: list[list[Any]]) -> None:
    types = {
        "integer": SqlType.big_int(),
        "string": SqlType.text(),
        "real": SqlType.double(),
        "date": SqlType.date(),
    }
    definition = TableDefinition(
        TableName("Extract", "Extract"),
        [TableDefinition.Column(c[0], types[c[2]]) for c in COLUMNS],
    )
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        conn.catalog.create_table(definition)
        with Inserter(conn, definition) as inserter:
            inserter.add_rows(rows)
            inserter.execute()


@dataclass
class Report:
    twbx: Path
    ir: IR
    snapshot: Path
    """Tableau 萃取匯入的 DuckDB 檔（比對基準）。"""


def build_report(tmp: Path, datasource_xml: str | None = None) -> Report:
    tmp.mkdir(parents=True, exist_ok=True)
    hyper = tmp / "orders.hyper"
    make_hyper(hyper, tmp, SNAPSHOT)
    xml = b.workbook(datasource=datasource_xml or datasource(), worksheets=sheet())
    twbx = b.write_twbx(tmp, xml, {"Data/Extracts/orders.hyper": hyper.read_bytes()})
    ir = parse_workbook(twbx)
    assert ir.unsupported == []
    snapshot_dir = tmp / "snapshot"
    (result,) = import_workbook(twbx, snapshot_dir, ir)
    assert result.ok, result.to_json()
    return Report(twbx, ir, snapshot_dir / f"{ir.datasources[0].id}.duckdb")


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> Report:
    return build_report(tmp_path_factory.mktemp("refresh"))


# ---------------------------------------------------------------- 解析


def test_parse_refresh_info(report: Report) -> None:
    ds = report.ir.datasources[0]
    connection = ds.connection
    assert isinstance(connection, ExtractConnection)
    source, refresh = connection.source, connection.refresh
    assert isinstance(source, LiveConnection) and source.dialect == "oracle"
    assert isinstance(source.relation, JoinRelation)
    assert refresh is not None and refresh.row_limit is None
    fields = {f.id: f for f in ds.fields}
    mapping = {fields[c.field].column: (c.relation, c.column) for c in refresh.columns}
    # 萃取的 REGION1 是 PEOPLE 的 REGION
    assert mapping["REGION1"] == ("PEOPLE", "REGION")
    assert mapping["REGION"] == ("ORDERS", "REGION")
    assert refresh.increment is not None and fields[refresh.increment].caption == "Row ID"
    assert [f.stage for f in ds.filters] == ["extract"]


def test_plan_sql(report: Report) -> None:
    plan = refresh_plan(report.ir, report.ir.datasources[0].id)
    (table,) = plan.tables
    assert (table.schema, table.table) == ("Extract", "Extract")
    assert [name for name, _ in table.columns] == [c[0] for c in COLUMNS]
    sql = table.sql()
    assert '"PEOPLE"."REGION" AS' in sql and "LEFT JOIN" in sql
    assert "'South'" in sql  # 萃取篩選
    incremental = table.sql(after=3)
    assert incremental.endswith("> 3)") or "> 3" in incremental


# ---------------------------------------------------------------- 來源資料庫


@dataclass
class Source:
    name: str
    executor: Callable[[], Executor]
    """每次更新開一個執行器（DuckDB 檔在修改資料時要先關閉）。"""
    replace: Callable[[str, pa.Table], None]
    """以新的內容取代來源資料表（模擬來源資料庫的資料變動）。"""


@pytest.fixture(params=["duckdb", "postgres", "spark", "oracle"])
def source(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Source]:
    tables = {"ORDERS": ORDERS, "PEOPLE": PEOPLE}
    if request.param in ("duckdb", "oracle"):
        path = tmp_path / "source.duckdb"
        con = duckdb.connect(str(path) if request.param == "duckdb" else ":memory:")
        con.execute('CREATE SCHEMA "SALES"')

        def load(name: str, table: pa.Table) -> None:
            con.register("rows", table)
            con.execute(f'CREATE OR REPLACE TABLE "SALES"."{name}" AS SELECT * FROM rows')
            con.unregister("rows")

        for name, table in tables.items():
            load(name, table)
        if request.param == "oracle":
            yield Source("oracle", lambda: OracleEmulator(con), load)
            con.close()
            return
        con.close()

        def replace(name: str, table: pa.Table) -> None:
            with duckdb.connect(str(path)) as writer:
                writer.register("rows", table)
                writer.execute(f'CREATE OR REPLACE TABLE "SALES"."{name}" AS SELECT * FROM rows')

        yield Source("duckdb", lambda: DuckDBExecutor(path), replace)
        return
    if request.param == "postgres":
        with postgres_cluster() as dsn:
            con = pg_connect(dsn)
            for name, table in tables.items():
                pg_load(con, name, table, "SALES")

            def pg_replace(name: str, table: pa.Table) -> None:
                con.execute(f'DROP TABLE "SALES"."{name}"')
                pg_load(con, name, table, "SALES")

            # 每次更新各自連線：執行器關閉時會關閉連線，工作階段也設為唯讀
            yield Source("postgres", lambda: postgres_executor(pg_connect(dsn)), pg_replace)
            con.close()
        return
    with spark_session() as spark:
        for name, table in tables.items():
            spark.createDataFrame(table).createOrReplaceTempView(name)

        def spark_replace(name: str, table: pa.Table) -> None:
            spark.createDataFrame(table).createOrReplaceTempView(name)

        yield Source("spark", lambda: SparkExecutor(spark), spark_replace)


def for_source(ir: IR, source: Source) -> IR:
    """Spark 的暫存檢視沒有結構描述。"""
    if source.name != "spark":
        return ir
    ds = ir.datasources[0]
    connection = ds.connection
    assert isinstance(connection, ExtractConnection) and connection.source is not None
    relation = connection.source.relation
    assert isinstance(relation, JoinRelation)
    left, right = relation.left, relation.right
    relation = relation.model_copy(
        update={
            "left": left.model_copy(update={"table": ["ORDERS"]}),
            "right": right.model_copy(update={"table": ["PEOPLE"]}),
        }
    )
    connection = connection.model_copy(
        update={"source": connection.source.model_copy(update={"relation": relation})}
    )
    return ir.model_copy(update={"datasources": [ds.model_copy(update={"connection": connection})]})


def refresh(report: Report, source: Source, out: Path, incremental: bool = False) -> Any:
    executor = source.executor()
    try:
        ir = for_source(report.ir, source)
        return refresh_extract(ir, ir.datasources[0].id, executor, out, incremental=incremental)
    finally:
        executor.close()


def table_rows(path: Path) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(path), read_only=True) as con:
        return con.execute('SELECT * FROM "Extract"."Extract" ORDER BY "ROW_ID"').fetchall()


def test_full_refresh_matches_tableau_extract(
    report: Report, source: Source, tmp_path: Path
) -> None:
    out = tmp_path / "data" / f"{report.ir.datasources[0].id}.duckdb"
    result = refresh(report, source, out)
    assert result.mode == "full"
    assert [(t.table, t.rows) for t in result.tables] == [("Extract", 4)]
    assert [list(r) for r in table_rows(out)] == SNAPSHOT
    # 型別沿用 .hyper 匯入時的型別：沒有現有檔案時依 IR 的資料型別，與匯入的一致
    compare = compare_extract(report.ir, report.ir.datasources[0].id, out, report.snapshot)
    assert compare.ok, [c for t in compare.tables for c in t.checks if not c.ok]
    # 同一張工作表在新萃取與 Tableau 萃取上的結果相同
    query = worksheet_sql(report.ir, "Sales by Manager")
    assert execute_query(out, query).rows == execute_query(report.snapshot, query).rows
    assert (out.parent / f"{report.ir.datasources[0].id}.refresh.json").is_file()


@pytest.mark.parametrize("source", ["duckdb", "postgres"], indirect=True)
def test_incremental_and_changed_source(report: Report, source: Source, tmp_path: Path) -> None:
    ds_id = report.ir.datasources[0].id
    out = tmp_path / "data" / f"{ds_id}.duckdb"
    out.parent.mkdir()
    out.write_bytes(report.snapshot.read_bytes())  # 從 Tableau 的萃取開始
    # 來源新增 A6（West）、A7（South，被萃取篩選排除），並修改了舊的 A1
    added = pa.table(
        {
            "ROW_ID": pa.array([6, 7], pa.int64()),
            "ORDER_ID": ["A6", "A7"],
            "REGION": ["West", "South"],
            "SALES": [40.0, 99.0],
            "QUANTITY": pa.array([6, 7], pa.int64()),
            "ORDER_DATE": [dt.date(2024, 1, 6), dt.date(2024, 1, 7)],
        }
    )
    changed = pa.concat_tables([ORDERS, added]).to_pylist()
    changed[0]["SALES"] = 999.0
    source.replace("ORDERS", pa.Table.from_pylist(changed, schema=ORDERS.schema))

    result = refresh(report, source, out, incremental=True)
    assert result.mode == "incremental"
    assert [(t.rows, t.added) for t in result.tables] == [(5, 1)]
    rows = table_rows(out)
    assert [r[1] for r in rows] == ["A1", "A2", "A3", "A5", "A6"]
    assert rows[0][3] == 100.0  # 增量更新只附加新的列，不更新舊的列
    assert rows[-1][6:] == ("West", "Bob")

    # 完整更新後 A1 的修改也進來；與 Tableau 的萃取比對，列數與 SALES 的總和、最大值不同
    refresh(report, source, out)
    compare = compare_extract(report.ir, ds_id, out, report.snapshot)
    assert not compare.ok
    bad = {(c.column, c.stat) for t in compare.tables for c in t.checks if not c.ok}
    assert {(None, "rows"), ("SALES", "sum"), ("SALES", "max"), ("ROW_ID", "max")} <= bad
    assert ("REGION", "min") not in bad


def test_failed_refresh_keeps_old_file(report: Report, tmp_path: Path) -> None:
    class Broken:
        dialect = "duckdb"

        def fetch(self, sql: str) -> pa.Table:
            raise RuntimeError("連線中斷")

        def close(self) -> None:
            return None

    out = tmp_path / f"{report.ir.datasources[0].id}.duckdb"
    out.write_bytes(report.snapshot.read_bytes())
    before = out.read_bytes()
    with pytest.raises(RefreshError, match="連線中斷"):
        refresh_extract(report.ir, report.ir.datasources[0].id, Broken(), out)
    assert out.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [out.name]


def test_unrefreshable_extracts(tmp_path: Path, report: Report) -> None:
    ds_id = report.ir.datasources[0].id
    with pytest.raises(RefreshError, match="增量更新需要現有的萃取檔"):
        refresh_extract(
            report.ir,
            ds_id,
            DuckDBExecutor(report.snapshot),
            tmp_path / "x.duckdb",
            incremental=True,
        )
    sampled = build_report(tmp_path / "sampled", datasource(count="100"))
    with pytest.raises(RefreshError, match="只取前 100 筆"):
        refresh_plan(sampled.ir, ds_id)
    # 來源是檔案：沒有可更新的來源
    textscan = datasource().replace(
        ORACLE, "<connection class='textscan' directory='C:/data' filename='orders.csv' />"
    )
    files = build_report(tmp_path / "files", textscan)
    with pytest.raises(RefreshError, match="來源無法解析"):
        refresh_plan(files.ir, ds_id)


# ---------------------------------------------------------------- Relationship 模型


def test_model_extract_refresh(tmp_path: Path) -> None:
    from ..sqlgen import test_relationships as rel

    hyper = tmp_path / "model.hyper"
    rel.make_hyper(hyper, tmp_path)
    xml = b.workbook(datasource=rel.datasource(extract=True), worksheets=rel.base_sheet())
    twbx = b.write_twbx(tmp_path, xml, {"Data/Extracts/model.hyper": hyper.read_bytes()})
    ir = parse_workbook(twbx)
    ds = ir.datasources[0]
    connection = ds.connection
    assert isinstance(connection, ExtractConnection) and connection.refresh is not None
    source = tmp_path / "source.duckdb"
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE SCHEMA "SALES"')
        for name, table in rel.TABLES.items():
            con.register("rows", table)
            con.execute(f'CREATE TABLE "SALES"."{name}" AS SELECT * FROM rows')
            con.unregister("rows")
    snapshot = tmp_path / "snapshot"
    import_workbook(twbx, snapshot, ir)
    out = tmp_path / "data" / f"{ds.id}.duckdb"
    executor = DuckDBExecutor(source)
    try:
        result = refresh_extract(ir, ds.id, executor, out)
    finally:
        executor.close()
    assert [(t.table, t.rows) for t in result.tables] == [
        (object_id, len(rel.TABLES[name])) for name, (_, object_id) in rel.OBJECTS.items()
    ]
    compare = compare_extract(ir, ds.id, out, snapshot / f"{ds.id}.duckdb")
    assert compare.ok, [c for t in compare.tables for c in t.checks if not c.ok]


# ---------------------------------------------------------------- 命令列


def test_cli_refresh_and_compare(
    tmp_path: Path,
    report: Report,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.duckdb"
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE SCHEMA "SALES"')
        for name, table in {"ORDERS": ORDERS, "PEOPLE": PEOPLE}.items():
            con.register("rows", table)
            con.execute(f'CREATE TABLE "SALES"."{name}" AS SELECT * FROM rows')
            con.unregister("rows")
    connection = report.ir.datasources[0].connection
    assert isinstance(connection, ExtractConnection) and connection.source is not None
    config = tmp_path / "connections.toml"
    config.write_text(
        f'[connections.{connection.source.connection_id}]\ndriver = "duckdb"\n'
        f'path = "{source.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TMIG_CONNECTIONS", str(config))
    data = tmp_path / "data"
    assert main(["extract", "refresh", str(report.twbx), "--out-dir", str(data)]) == 0
    assert "完整更新" in capsys.readouterr().out
    assert main(["extract", "compare", str(report.twbx), "--data-dir", str(data)]) == 0
    out = capsys.readouterr().out
    assert "一致" in out
    assert (data / f"{report.ir.datasources[0].id}.compare.json").is_file()


def test_verify_notes_refreshed_data(report: Report, tmp_path: Path) -> None:
    from tmig.verify.run import ensure_data

    ds_id = report.ir.datasources[0].id
    data = tmp_path / "data"
    data.mkdir()
    (data / f"{ds_id}.duckdb").write_bytes(report.snapshot.read_bytes())
    assert ensure_data(tmp_path, report.ir) == []
    (data / f"{ds_id}.refresh.json").write_text('{"finished": "2026-10-30T01:00:00+00:00"}')
    (note,) = ensure_data(tmp_path, report.ir)
    assert "從來源更新的萃取（2026-10-30T01:00:00+00:00）" in note
    # 重新匯入快照後，更新的紀錄移除
    import_workbook(report.twbx, data, report.ir)
    assert not (data / f"{ds_id}.refresh.json").exists()
    assert ensure_data(tmp_path, report.ir) == []


@pytest.mark.parametrize("source", ["duckdb"], indirect=True)
def test_refresh_keeps_case_insensitive_collation(
    report: Report, source: Source, tmp_path: Path
) -> None:
    """從來源更新萃取時沿用匯入 .hyper 時的定序（大小寫不分的文字欄位比較時不分大小寫）。"""
    import json

    ds_id = report.ir.datasources[0].id
    out = tmp_path / "data" / f"{ds_id}.duckdb"
    out.parent.mkdir()
    imported = {
        "table": {
            "schema": "Extract",
            "table": "Extract",
            "columns": [{"name": "REGION", "collation": "en_US_CI"}],
        }
    }
    (out.parent / f"{ds_id}.import.json").write_text(json.dumps(imported), encoding="utf-8")
    refresh(report, source, out)
    with duckdb.connect(str(out), read_only=True) as con:
        west = 'SELECT count(*) FROM "Extract"."Extract" WHERE "REGION" = \'west\''
        (count,) = con.execute(west).fetchone() or (0,)
    assert count == sum(1 for r in SNAPSHOT if r[2] == "West")
