"""第三階段 N5：多表的 Live 連線——join 樹與自訂 SQL，以手算的結果驗證。

- join：ORDERS left join PEOPLE（地區相同且 ACTIVE = 1）。與 Tableau 的實體 join 相同，一對多時訂單會重複計算
- 兩張表有同名欄位（REGION）：各自成為不同的欄位（Region、Region (People)）
- 自訂 SQL：原文不經轉譯；其中的參數代入目前值

在 DuckDB 檔（當作來源資料庫）、PostgreSQL、Spark 與模擬的 Oracle 上執行；沒有 PostgreSQL 或 Spark 時略過。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from tmig.data.executor import QueryResult
from tmig.data.live import execute_live
from tmig.data.sources import DuckDBExecutor, Executor
from tmig.ir import IR, JoinRelation, LiveConnection, Relation, SqlRelation, TableRelation
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import QueryState
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
        "ORDER_ID": ["A1", "A2", "A3", "A4", "A5"],
        "REGION": ["East", "East", "West", "South", None],
        "SALES": [100.0, 50.0, 70.0, 30.0, 10.0],
        "QUANTITY": pa.array([1, 2, 3, 4, 5], pa.int64()),
    }
)
PEOPLE = pa.table(
    {
        "REGION": ["East", "West", "West", "Central", "East"],
        "MANAGER": ["Ann", "Bob", "Bill", "Cid", "Eve"],
        "ACTIVE": pa.array([1, 1, 0, 1, 1], pa.int64()),
    }
)

ORACLE = (
    "<connection authentication='username-password' class='oracle' port='1521' "
    "schema='SALES' server='ora.example.com' service='ORCLPDB' />"
)


def record(remote: str, local: str, parent: str, datatype: str, agg: str) -> str:
    return f"""<metadata-record class='column'>
      <remote-name>{remote}</remote-name><remote-type>129</remote-type>
      <local-name>[{local}]</local-name><parent-name>[{parent}]</parent-name>
      <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
    </metadata-record>"""


def datasource(relation: str, records: list[str]) -> str:
    return f"""
    <datasource caption='Orders' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='db' name='db.1'>{ORACLE}</named-connection>
        </named-connections>
        {relation}
        <metadata-records>{"".join(records)}</metadata-records>
      </connection>
      <aliases enabled='yes' />
    </datasource>"""


JOIN = """
  <relation join='left' type='join'>
    <clause type='join'>
      <expression op='AND'>
        <expression op='='>
          <expression op='[ORDERS].[REGION]' />
          <expression op='[PEOPLE].[REGION]' />
        </expression>
        <expression op='='>
          <expression op='[PEOPLE].[ACTIVE]' />
          <expression op='1' />
        </expression>
      </expression>
    </clause>
    <relation connection='db.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />
    <relation connection='db.1' name='PEOPLE' table='[SALES].[PEOPLE]' type='table' />
  </relation>"""
JOIN_RECORDS = [
    record("ORDER_ID", "Order ID", "ORDERS", "string", "Count"),
    record("REGION", "Region", "ORDERS", "string", "Count"),
    record("SALES", "Sales", "ORDERS", "real", "Sum"),
    record("QUANTITY", "Quantity", "ORDERS", "integer", "Sum"),
    record("REGION", "Region (People)", "PEOPLE", "string", "Count"),
    record("MANAGER", "Manager", "PEOPLE", "string", "Count"),
    record("ACTIVE", "Active", "PEOPLE", "integer", "Sum"),
]
CUSTOM_RECORDS = [
    record("REGION", "REGION", "Custom SQL Query", "string", "Count"),
    record("TOTAL_SALES", "TOTAL_SALES", "Custom SQL Query", "real", "Sum"),
    record("ORDERS", "ORDERS", "Custom SQL Query", "integer", "Sum"),
]
# 自訂 SQL 是來源資料庫的原文：各引擎的寫法不同（Spark 以反引號或不加引號）
CUSTOM_SQL = (
    'SELECT "REGION", SUM("SALES") AS "TOTAL_SALES", COUNT(*) AS "ORDERS" '
    'FROM "SALES"."ORDERS" WHERE "REGION" <> <[Parameters].[Parameter 2]> GROUP BY "REGION"'
)
CUSTOM_SQL_SPARK = (
    "SELECT REGION, SUM(SALES) AS TOTAL_SALES, COUNT(*) AS ORDERS "
    "FROM ORDERS WHERE REGION <> <[Parameters].[Parameter 2]> GROUP BY REGION"
)


def sheet(name: str, dim: str, measures: list[tuple[str, str, str]]) -> str:
    """一個維度在列、量值在欄的長條圖；measures 為（欄位、衍生、實例名稱）。"""
    instances = [instance(f"[{dim}]", "None", f"[none:{dim}:nk]")]
    instances += [instance(f"[{f}]", d, f"[{n}]", "quantitative") for f, d, n in measures]
    cols = " + ".join(ref(n) for _, _, n in measures)
    return worksheet(name, instances, rows=ref(f"none:{dim}:nk"), cols=cols)


@pytest.fixture(scope="module")
def workbooks(tmp_path_factory: pytest.TempPathFactory) -> tuple[IR, IR]:
    tmp = tmp_path_factory.mktemp("multitable")
    join_xml = b.workbook(
        datasource=datasource(JOIN, JOIN_RECORDS),
        worksheets=sheet("Sales by Manager", "Manager", [("Sales", "Sum", "sum:Sales:qk")])
        + sheet(
            "Sales by Region",
            "Region",
            [("Sales", "Sum", "sum:Sales:qk"), ("Order ID", "CountD", "ctd:Order ID:qk")],
        )
        + sheet(
            "Quantity by People Region",
            "Region (People)",
            [("Quantity", "Sum", "sum:Quantity:qk")],
        ),
    )
    relation = (
        f"<relation connection='db.1' name='Custom SQL Query' type='text'>"
        f"{CUSTOM_SQL.replace('<', '&lt;').replace('>', '&gt;')}</relation>"
    )
    custom_xml = b.workbook(
        datasource=datasource(relation, CUSTOM_RECORDS),
        worksheets=sheet(
            "Totals by Region",
            "REGION",
            [("TOTAL_SALES", "Sum", "sum:TOTAL_SALES:qk"), ("ORDERS", "Sum", "sum:ORDERS:qk")],
        ),
    )
    join_ir = parse_workbook(b.write_twb(tmp, join_xml, "join.twb"))
    custom_ir = parse_workbook(b.write_twb(tmp, custom_xml, "custom.twb"))
    assert join_ir.unsupported == [] and custom_ir.unsupported == []
    return join_ir, custom_ir


@dataclass
class Engine:
    name: str
    executor: Executor
    spark: bool = False


@pytest.fixture(scope="module", params=["duckdb", "postgres", "spark", "oracle"])
def engine(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Engine]:
    if request.param in ("duckdb", "oracle"):
        path = tmp_path_factory.mktemp("source") / "source.duckdb"
        con = duckdb.connect(str(path) if request.param == "duckdb" else ":memory:")
        con.execute('CREATE SCHEMA "SALES"')
        for name, table in (("ORDERS", ORDERS), ("PEOPLE", PEOPLE)):
            con.register(f"rows_{name}", table)
            con.execute(f'CREATE TABLE "SALES"."{name}" AS SELECT * FROM "rows_{name}"')
        if request.param == "oracle":
            yield Engine("oracle", OracleEmulator(con))
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
            pg_load(con, "ORDERS", ORDERS, "SALES")
            pg_load(con, "PEOPLE", PEOPLE, "SALES")
            yield Engine("postgres", postgres_executor(con))
            con.close()
        return
    with spark_session() as spark:
        spark.createDataFrame(ORDERS).createOrReplaceTempView("ORDERS")
        spark.createDataFrame(PEOPLE).createOrReplaceTempView("PEOPLE")
        yield Engine("spark", SparkExecutor(spark), spark=True)


def for_engine(ir: IR, engine: Engine) -> IR:
    """Spark 的暫存檢視沒有結構描述；自訂 SQL 換成 Spark 的寫法。"""
    if not engine.spark:
        return ir

    def convert(relation: Relation) -> Relation:
        if isinstance(relation, TableRelation):
            return relation.model_copy(update={"table": relation.table[-1:]})
        if isinstance(relation, SqlRelation):
            return relation.model_copy(update={"sql": CUSTOM_SQL_SPARK})
        assert isinstance(relation, JoinRelation)
        return relation.model_copy(
            update={"left": convert(relation.left), "right": convert(relation.right)}
        )

    datasources = []
    for ds in ir.datasources:
        connection = ds.connection
        assert isinstance(connection, LiveConnection)
        assert isinstance(connection.relation, TableRelation | SqlRelation | JoinRelation)
        connection = connection.model_copy(update={"relation": convert(connection.relation)})
        datasources.append(ds.model_copy(update={"connection": connection}))
    return ir.model_copy(update={"datasources": datasources})


def run(
    ir: IR, engine: Engine, worksheet_name: str, state: QueryState | None = None
) -> QueryResult:
    return execute_live(
        worksheet_sql(for_engine(ir, engine), worksheet_name, state), engine.executor
    )


def by_dim(result: QueryResult, column: int = 1) -> dict[Any, Any]:
    return {row[0]: row[column] for row in result.rows}


def test_join_tree(workbooks: tuple[IR, IR], engine: Engine) -> None:
    join_ir, _ = workbooks
    # 一對多：東區有兩位經理，東區的訂單各算兩次（與 Tableau 的實體 join 相同）；
    # 不在職的 Bill 不符合 join 條件；南區與沒有地區的訂單沒有對應的經理
    assert by_dim(run(join_ir, engine, "Sales by Manager")) == {
        "Ann": 150.0,
        "Bob": 70.0,
        "Eve": 150.0,
        None: 40.0,
    }
    region = run(join_ir, engine, "Sales by Region")
    assert by_dim(region) == {"East": 300.0, "South": 30.0, "West": 70.0, None: 10.0}
    # 不重複計數不受重複的列影響
    assert by_dim(region, 2) == {"East": 2, "South": 1, "West": 1, None: 1}
    # 兩張表的同名欄位是不同的欄位：依 PEOPLE 的地區分組
    assert by_dim(run(join_ir, engine, "Quantity by People Region")) == {
        "East": 6,
        "West": 3,
        None: 9,
    }


def test_custom_sql_with_parameters(workbooks: tuple[IR, IR], engine: Engine) -> None:
    _, custom_ir = workbooks
    # 參數 Region Pick 的預設值為 East：自訂 SQL 排除東區（沒有地區的列也不符合 <>）
    result = run(custom_ir, engine, "Totals by Region")
    assert by_dim(result) == {"South": 30.0, "West": 70.0}
    assert by_dim(result, 2) == {"South": 1, "West": 1}
    param = next(p for p in custom_ir.parameters if p.caption == "Region Pick")
    west = QueryState(parameters={param.id: "West"})
    assert by_dim(run(custom_ir, engine, "Totals by Region", west)) == {
        "East": 150.0,
        "South": 30.0,
    }
