"""Live 連線的樞紐（pivot）、聯集（union）與合併不相符的欄位（project）：轉成來源資料庫的 SQL 實跑。

樞紐與聯集是子查詢（UNION ALL），欄位依名稱選出、轉成 Tableau 記下的型別；合併的欄位是 Tableau 公式
（IF NOT ISNULL … ELSEIF …）轉成的 SQL。在 DuckDB 檔、PostgreSQL、Spark 與模擬的 Oracle、SQL Server 上
執行，結果與手算相同。Live 連線的聯集不記下各資料表的欄位，視為每張都有聯集的所有欄位。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from tmig.data.executor import QueryResult
from tmig.data.live import execute_live
from tmig.data.sources import DuckDBExecutor, Executor
from tmig.ir import IR, LiveConnection, PivotRelation, ProjectRelation, UnionRelation
from tmig.ir.models import ModelRelation, Relation, TableRelation
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..engines import (
    OracleEmulator,
    SparkExecutor,
    TSqlEmulator,
    pg_connect,
    pg_load,
    postgres_cluster,
    postgres_executor,
    spark_session,
)
from ..parser import builder as b

WIDE = pa.table(
    {
        "REGION": ["East", "West", "North"],
        "Y2019": pa.array([10, 5, None], pa.int64()),
        "Y2020": pa.array([20, 6, 1], pa.int64()),
    }
)
Q1 = pa.table({"REGION": ["East", "West"], "SALES": [10.5, 20.0], "PRODUCT": ["Chair", None]})
Q2 = pa.table({"REGION": ["East", "South"], "SALES": [1.0, 2.0], "PRODUCT": [None, "Desk"]})
TABLES = {"WIDE": WIDE, "Q1": Q1, "Q2": Q2}
ORACLE = (
    "<connection authentication='username-password' class='oracle' port='1521' "
    "schema='SALES' server='ora.example.com' service='ORCLPDB' />"
)


def record(name: str, datatype: str, parent: str) -> str:
    agg = "Sum" if datatype in ("real", "integer") else "Count"
    return f"""<metadata-record class='column'>
      <remote-name>{name}</remote-name><remote-type>129</remote-type>
      <local-name>[{name}]</local-name><parent-name>[{parent}]</parent-name>
      <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
    </metadata-record>"""


def datasource(name: str, relation: str, records: str, internal: str) -> str:
    return f"""
    <datasource caption='{name}' inline='true' name='{internal}' version='18.1'>
      <connection class='federated'>
        <named-connections><named-connection caption='db' name='db.1'>{ORACLE}</named-connection>
        </named-connections>
        {relation}
        <metadata-records>{records}</metadata-records>
      </connection>
      <aliases enabled='yes' />
    </datasource>"""


def table(name: str) -> str:
    return f"<relation connection='db.1' name='{name}' table='[SALES].[{name}]' type='table' />"


PIVOT = (
    "<relation name='Pivot' type='pivot'><columns>"
    "<column datatype='string' name='Pivot Field Names' />"
    "<column datatype='integer' name='Pivot Field Values' /></columns>"
    "<tag name='Pivot Field Names'><value name='[Y2019]' /><value name='[Y2020]' /></tag>"
    "<groups><group name='Pivot Field Values'><field name='[WIDE].[Y2019]' />"
    f"<field name='[WIDE].[Y2020]' /></group></groups>{table('WIDE')}</relation>"
)
MERGE = (
    "IF NOT ISNULL([Q1+].[PRODUCT]) THEN [Q1+].[PRODUCT]&#10;"
    "ELSEIF NOT ISNULL([Q1+].[REGION]) THEN [Q1+].[REGION]&#10;ELSE NULL&#10;END"
)
UNION = (
    "<relation type='project'><columns>"
    "<column name='[Project].[PRODUCT &amp; REGION]' project-type='coalesce' /></columns>"
    f"<bindings><binding expression='{MERGE}' name='[Project].[PRODUCT &amp; REGION]' /></bindings>"
    f"<relation all='true' name='Q1+' type='union'><columns header='yes' />{table('Q1')}"
    f"{table('Q2')}</relation></relation>"
)


def workbook() -> str:
    wide = datasource(
        "Wide",
        PIVOT,
        record("REGION", "string", "WIDE")
        + record("Pivot Field Names", "string", "Pivot")
        + record("Pivot Field Values", "integer", "Pivot"),
        b.DS,
    )
    quarters = datasource(
        "Quarters",
        UNION,
        record("REGION", "string", "Q1+")
        + record("SALES", "real", "Q1+")
        + record("Table Name", "string", "Q1+")
        + record("PRODUCT &amp; REGION", "string", "Project"),
        "federated.quarters",
    )
    sheets = b.worksheet(
        "Base",
        [b.instance("[REGION]", "None", "[none:REGION:nk]")],
        rows=b.ref("none:REGION:nk"),
    )
    return b.workbook(wide + quarters, worksheets=sheets, params=False)


@pytest.fixture(scope="module")
def ir(tmp_path_factory: pytest.TempPathFactory) -> IR:
    parsed = parse_workbook(b.write_twb(tmp_path_factory.mktemp("transforms"), workbook()))
    assert parsed.unsupported == [], [u.reason for u in parsed.unsupported]
    return parsed


@dataclass
class Engine:
    name: str
    executor: Executor
    spark: bool = False


@pytest.fixture(scope="module", params=["duckdb", "postgres", "spark", "oracle", "tsql"])
def engine(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Engine]:
    if request.param in ("duckdb", "oracle", "tsql"):
        path = tmp_path_factory.mktemp("source") / "source.duckdb"
        con = duckdb.connect(str(path) if request.param == "duckdb" else ":memory:")
        con.execute('CREATE SCHEMA "SALES"')
        for name, data in TABLES.items():
            con.register(f"rows_{name}", data)
            con.execute(f'CREATE TABLE "SALES"."{name}" AS SELECT * FROM "rows_{name}"')
        if request.param != "duckdb":
            emulator = OracleEmulator(con) if request.param == "oracle" else TSqlEmulator(con)
            yield Engine(request.param, emulator)
            con.close()
            return
        con.close()
        executor = DuckDBExecutor(path)
        yield Engine("duckdb", executor)
        executor.close()
        return
    if request.param == "postgres":
        with postgres_cluster() as dsn:
            con = pg_connect(dsn)
            for name, data in TABLES.items():
                pg_load(con, name, data, "SALES")
            yield Engine("postgres", postgres_executor(con))
            con.close()
        return
    with spark_session() as spark:
        for name, data in TABLES.items():
            spark.createDataFrame(data).createOrReplaceTempView(name)
        yield Engine("spark", SparkExecutor(spark), spark=True)


def _no_schema(relation: Relation) -> Relation:
    """Spark 的暫存檢視沒有結構描述。"""
    if isinstance(relation, TableRelation):
        return relation.model_copy(update={"table": relation.table[-1:]})
    if isinstance(relation, PivotRelation | ProjectRelation):
        source = _no_schema(relation.source)
        return relation.model_copy(update={"source": source})
    if isinstance(relation, UnionRelation):
        parts = [p.model_copy(update={"relation": _no_schema(p.relation)}) for p in relation.parts]
        return relation.model_copy(update={"parts": parts})
    return relation


def for_engine(ir: IR, engine: Engine) -> IR:
    if not engine.spark:
        return ir
    datasources = []
    for ds in ir.datasources:
        connection = ds.connection
        assert isinstance(connection, LiveConnection) and connection.relation is not None
        relation = connection.relation
        assert not isinstance(relation, ModelRelation)
        changed = connection.model_copy(update={"relation": _no_schema(relation)})
        datasources.append(ds.model_copy(update={"connection": changed}))
    return ir.model_copy(update={"datasources": datasources})


def run(
    ir: IR, engine: Engine, datasource: str, dim: str, measure: str, agg: str
) -> dict[Any, Any]:
    ds = next(d for d in ir.datasources if d.caption == datasource)
    fields = {f.caption: f.id for f in ds.fields}
    base = ir.worksheets[0]
    ws = base.model_copy(
        update={
            "datasource": ds.id,
            "rows": [b_use(fields[dim])],
            "columns": [b_use(fields[measure], agg)],
        }
    )
    changed = for_engine(ir.model_copy(update={"worksheets": [ws]}), engine)
    result: QueryResult = execute_live(worksheet_sql(changed, ws.id), engine.executor)
    return {row[0]: _plain(row[1]) for row in result.rows}


def b_use(field: str, agg: str | None = None) -> Any:
    from tmig.ir import FieldUse

    return FieldUse(field=field, agg=agg)  # type: ignore[arg-type]


def _plain(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def test_parse(ir: IR) -> None:
    wide, quarters = ir.datasources
    assert isinstance(wide.connection, LiveConnection)
    assert isinstance(wide.connection.relation, PivotRelation)
    assert isinstance(quarters.connection, LiveConnection)
    project = quarters.connection.relation
    assert isinstance(project, ProjectRelation) and isinstance(project.source, UnionRelation)
    # Live 連線的聯集沒有記下各資料表的欄位
    assert [p.columns for p in project.source.parts] == [None, None]


def test_pivot(ir: IR, engine: Engine) -> None:
    by_year = run(ir, engine, "Wide", "Pivot Field Names", "Pivot Field Values", "sum")
    assert by_year == {"Y2019": 15, "Y2020": 27}
    # Null 的值也保留成一列
    rows = run(ir, engine, "Wide", "Pivot Field Names", "REGION", "count")
    assert rows == {"Y2019": 3, "Y2020": 3}


def test_union(ir: IR, engine: Engine) -> None:
    assert run(ir, engine, "Quarters", "Table Name", "SALES", "sum") == {"Q1": 30.5, "Q2": 3}
    merged = run(ir, engine, "Quarters", "PRODUCT & REGION", "SALES", "sum")
    assert merged == {"Chair": 10.5, "Desk": 2, "East": 1, "West": 20}


def test_source_sql(ir: IR, tmp_path: Path) -> None:
    """子查詢依欄名選出用到的欄、轉成記下的型別。"""
    ds = ir.datasources[0]
    from tmig.sqlgen.source import datasource_source

    sql = datasource_source(ds).from_sql
    assert sql.count("UNION ALL") == 1
    assert 'CAST("WIDE"."Y2019" AS BIGINT) AS "Pivot Field Values"' in sql
