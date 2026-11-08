"""第三階段 N6：Relationship 模型，以手算的結果驗證。

資料（SALES 結構描述）：

- ORDERS（量值表）：O1 C1 P1 100、O2 C1 P2 50、O3 C2 P1 70、O4 C3 P3 30、O5 C9 P2 10、O6 Null P4 20
  （C9 不在 CUSTOMERS；P4 不在 PRODUCTS）
- CUSTOMERS（每個客戶一列）：C1 Ann R1、C2 Bob R2、C3 Cid R1、C4 Dee R3（Dee 沒有訂單）
- REGIONS（接在 CUSTOMERS 之下）：R1 East、R2 West、R3 South、R4 North（North 沒有客戶）
- PRODUCTS：P1 Furniture、P1 Office（同一個產品兩個類別：一對多）、P2 Office、P3 Tech、P5 Toys

關聯：ORDERS.CUST_ID = CUSTOMERS.CUST_ID、ORDERS.PROD_ID = PRODUCTS.PROD_ID、
CUSTOMERS.REGION_ID = REGIONS.REGION_ID。

與 Tableau 相同的語意：量值不因一對多重複計算（每一組維度值只算一次）、沒有對應的量值列以 Null 呈現、
維度表中沒有量值的成員也列出、只有維度時取內部連接、只用到其他資料表欄位的篩選以「有關聯且符合」
（沒有關聯時以 Null 判斷）套用、FIXED LOD 在維度篩選之前計算；量值來自多張資料表時各自彙總再依維度
合併（完整外部連接）。

在 DuckDB 檔（當作來源資料庫）、PostgreSQL、Spark 與模擬的 Oracle 上執行；另以 .hyper 萃取驗證匯入與查詢。
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
from tmig.data.runtime import DataRuntime
from tmig.data.sources import DuckDBExecutor, Executor
from tmig.ir import (
    IR,
    CategoricalFilter,
    ExtractConnection,
    FieldUse,
    LiveConnection,
    ModelRelation,
    TableRelation,
    TopNFilter,
)
from tmig.ir.models import DataField, Sort
from tmig.parser.workbook import NoDatasourceError, parse_workbook
from tmig.planner.state import FilterValue, QueryState
from tmig.sqlgen.query import QueryError, domain_sql, worksheet_sql

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
        "ORDER_ID": ["O1", "O2", "O3", "O4", "O5", "O6"],
        "CUST_ID": ["C1", "C1", "C2", "C3", "C9", None],
        "PROD_ID": ["P1", "P2", "P1", "P3", "P2", "P4"],
        "SALES": [100.0, 50.0, 70.0, 30.0, 10.0, 20.0],
        "QTY": pa.array([1, 2, 3, 4, 5, 6], pa.int64()),
    }
)
CUSTOMERS = pa.table(
    {
        "CUST_ID": ["C1", "C2", "C3", "C4"],
        "NAME": ["Ann", "Bob", "Cid", "Dee"],
        "REGION_ID": ["R1", "R2", "R1", "R3"],
    }
)
REGIONS = pa.table(
    {"REGION_ID": ["R1", "R2", "R3", "R4"], "REGION": ["East", "West", "South", "North"]}
)
PRODUCTS = pa.table(
    {
        "PROD_ID": ["P1", "P1", "P2", "P3", "P5"],
        "CATEGORY": ["Furniture", "Office", "Office", "Tech", "Toys"],
    }
)
TABLES = {"ORDERS": ORDERS, "CUSTOMERS": CUSTOMERS, "REGIONS": REGIONS, "PRODUCTS": PRODUCTS}
OBJECTS = {
    "ORDERS": ("Orders", "ORDERS_1A"),
    "CUSTOMERS": ("Customers", "CUSTOMERS_2B"),
    "REGIONS": ("Regions", "REGIONS_3C"),
    "PRODUCTS": ("Products", "PRODUCTS_4D"),
}
COLUMNS = {
    "ORDERS": [
        ("ORDER_ID", "Order ID", "string", "Count"),
        ("CUST_ID", "Cust ID", "string", "Count"),
        ("PROD_ID", "Prod ID", "string", "Count"),
        ("SALES", "Sales", "real", "Sum"),
        ("QTY", "Qty", "integer", "Sum"),
    ],
    "CUSTOMERS": [
        ("CUST_ID", "Cust ID (Customers)", "string", "Count"),
        ("NAME", "Name", "string", "Count"),
        ("REGION_ID", "Region ID", "string", "Count"),
    ],
    "REGIONS": [
        ("REGION_ID", "Region ID (Regions)", "string", "Count"),
        ("REGION", "Region", "string", "Count"),
    ],
    "PRODUCTS": [
        ("PROD_ID", "Prod ID (Products)", "string", "Count"),
        ("CATEGORY", "Category", "string", "Count"),
    ],
}
RELATIONSHIPS = [
    ("Cust ID", "Cust ID (Customers)", "ORDERS", "CUSTOMERS"),
    ("Prod ID", "Prod ID (Products)", "ORDERS", "PRODUCTS"),
    ("Region ID", "Region ID (Regions)", "CUSTOMERS", "REGIONS"),
]
ORACLE = (
    "<connection authentication='username-password' class='oracle' port='1521' "
    "schema='SALES' server='ora.example.com' service='ORCLPDB' />"
)
FCP = "_.fcp.ObjectModelEncapsulateLegacy.true..."
BUYERS = (
    "{FIXED [Prod ID], IF [Category] = 'Office' THEN 'Office' ELSE 'Other' END :"
    " COUNTD([Cust ID (Customers)])}"
)
"""買過該產品的客戶數（依產品與是否為 Office 類別）：量值在 CUSTOMERS，維度在 ORDERS 與 PRODUCTS
（與 Snow Sports 的 Available 相同的寫法；Snow 以布林值分組，Oracle 沒有布林值，這裡改用字串）。"""


def records(extract: bool) -> str:
    items = []
    for table, columns in COLUMNS.items():
        object_id = OBJECTS[table][1]
        parent = object_id if extract else table
        for remote, local, datatype, agg in columns:
            items.append(
                f"""<metadata-record class='column'>
              <remote-name>{remote}</remote-name><remote-type>129</remote-type>
              <local-name>[{local}]</local-name><parent-name>[{parent}]</parent-name>
              <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
              <{FCP}object-id>[{object_id}]</{FCP}object-id>
            </metadata-record>"""
            )
    return f"<metadata-records>{''.join(items)}</metadata-records>"


def object_graph(extract: bool) -> str:
    objects = []
    for table, (caption, object_id) in OBJECTS.items():
        props = (
            f"<properties context=''><relation connection='db.1' name='{table}' "
            f"table='[SALES].[{table}]' type='table' /></properties>"
        )
        if extract:
            props += (
                f"<properties context='extract'><relation name='{object_id}' "
                f"table='[Extract].[{object_id}]' type='table' /></properties>"
            )
        objects.append(f"<object caption='{caption}' id='{object_id}'>{props}</object>")
    rels = []
    for left, right, first, second in RELATIONSHIPS:
        rels.append(
            f"""<relationship>
              <expression op='='><expression op='[{left}]' /><expression op='[{right}]' /></expression>
              <first-end-point object-id='{OBJECTS[first][1]}' />
              <second-end-point object-id='{OBJECTS[second][1]}' />
            </relationship>"""
        )
    return (
        f"<{FCP}object-graph><objects>{''.join(objects)}</objects>"
        f"<relationships>{''.join(rels)}</relationships></{FCP}object-graph>"
    )


def datasource(extract: bool = False) -> str:
    collection = "".join(
        f"<relation connection='db.1' name='{t}' table='[SALES].[{t}]' type='table' />"
        for t in TABLES
    )
    extract_xml = ""
    if extract:
        relations = "".join(
            f"<relation name='{object_id}' table='[Extract].[{object_id}]' type='table' />"
            for _, object_id in OBJECTS.values()
        )
        extract_xml = f"""
      <extract count='-1' enabled='true' units='records'>
        <connection class='hyper' dbname='Data/Extracts/model.hyper' schema='Extract' tablename='Extract'>
          <{FCP}relation type='collection'>{relations}</{FCP}relation>
          {records(True)}
        </connection>
      </extract>"""
    return f"""
    <datasource caption='Sales Model' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='db' name='db.1'>{ORACLE}</named-connection>
        </named-connections>
        <{FCP}relation type='collection'>{collection}</{FCP}relation>
        {records(False)}
      </connection>
      {extract_xml}
      <aliases enabled='yes' />
      {b.calc("Calculation_1", "Cust Total", "{FIXED [Cust ID]: SUM([Sales])}")}
      {b.calc("Calculation_2", "Name Category", "[Name] + [Category]", "string", "dimension")}
      {b.calc("Calculation_3", "Ann Sales", "IF [Name] = 'Ann' THEN [Sales] END")}
      {b.calc("Calculation_4", "Buyers", BUYERS, "integer")}
      {b.calc("Calculation_5", "Open Buyers", "MAX([Buyers]) - COUNTD([Cust ID])", "integer")}
      {object_graph(extract)}
    </datasource>"""


def base_sheet() -> str:
    return worksheet(
        "Base",
        [instance("[Cust ID]", "None", "[none:Cust ID:nk]")],
        rows=ref("none:Cust ID:nk"),
    )


@pytest.fixture(scope="module")
def model_ir(tmp_path_factory: pytest.TempPathFactory) -> IR:
    tmp = tmp_path_factory.mktemp("relationships")
    ir = parse_workbook(
        b.write_twb(tmp, b.workbook(datasource=datasource(), worksheets=base_sheet()))
    )
    assert ir.unsupported == []
    return ir


def ids(ir: IR) -> dict[str, str]:
    """欄位顯示名稱 → 欄位 id。"""
    return {f.caption: f.id for f in ir.datasources[0].fields}


def dim(ir: IR, caption: str) -> FieldUse:
    return FieldUse(field=ids(ir)[caption])


def measure(ir: IR, caption: str, agg: str = "sum") -> FieldUse:
    return FieldUse(field=ids(ir)[caption], agg=agg)  # type: ignore[arg-type]


def view(
    ir: IR,
    rows: list[FieldUse],
    cols: list[FieldUse] | None = None,
    filters: list[Any] | None = None,
    sorts: list[Sort] | None = None,
) -> IR:
    """以基本工作表為底，換上欄列架、篩選與排序。"""
    base = ir.worksheets[0]
    ws = base.model_copy(
        update={
            "rows": rows,
            "columns": cols or [],
            "filters": filters or [],
            "sorts": sorts or [],
        }
    )
    return ir.model_copy(update={"worksheets": [ws]})


def categorical(
    ir: IR, caption: str, mode: str, values: list[Any], stage: str = "dimension"
) -> CategoricalFilter:
    return CategoricalFilter(
        kind="categorical",
        id=f"flt_{caption.lower().replace(' ', '_')}",
        field=dim(ir, caption),
        stage=stage,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        values=values,
        source_ref="test",
    )


# ---------------------------------------------------------------- 解析


def test_parse_live_model(model_ir: IR) -> None:
    ds = model_ir.datasources[0]
    connection = ds.connection
    assert isinstance(connection, LiveConnection)
    model = connection.relation
    assert isinstance(model, ModelRelation)
    assert [(t.caption, t.id) for t in model.tables] == list(OBJECTS.values())
    relations = [t.relation for t in model.tables]
    assert all(isinstance(r, TableRelation) for r in relations)
    assert [r.table for r in relations if isinstance(r, TableRelation)] == [
        ["SALES", t] for t in TABLES
    ]
    fields = {f.caption: f for f in ds.fields}
    assert fields["Cust ID"].table == "ORDERS_1A"
    assert fields["Cust ID (Customers)"].table == "CUSTOMERS_2B"
    assert fields["Region"].table == "REGIONS_3C"
    # 每張邏輯資料表的筆數欄位屬於該資料表
    assert fields["Orders"].table == "ORDERS_1A" and fields["Orders"].column is None
    pairs = [
        (r.left, r.right, fields_of(ds.fields, r.on[0].left), fields_of(ds.fields, r.on[0].right))
        for r in model.relationships
    ]
    assert pairs == [
        ("ORDERS_1A", "CUSTOMERS_2B", "Cust ID", "Cust ID (Customers)"),
        ("ORDERS_1A", "PRODUCTS_4D", "Prod ID", "Prod ID (Products)"),
        ("CUSTOMERS_2B", "REGIONS_3C", "Region ID", "Region ID (Regions)"),
    ]


def fields_of(fields: list[DataField], field_id: str) -> str:
    return next(f.caption for f in fields if f.id == field_id)


def unsupported_reasons(tmp_path: Path, xml: str) -> list[str]:
    with pytest.raises(NoDatasourceError) as info:
        parse_workbook(b.write_twb(tmp_path, b.workbook(datasource=xml, worksheets=base_sheet())))
    return [u.reason for u in info.value.unsupported]


def test_live_logical_table_with_join_unsupported(tmp_path: Path) -> None:
    # 邏輯資料表內含實體層的 join：Live 連線尚不支援（萃取已存成單一資料表，可以使用）
    plain = (
        "<properties context=''><relation connection='db.1' name='PRODUCTS' "
        "table='[SALES].[PRODUCTS]' type='table' /></properties>"
    )
    joined = (
        "<properties context=''><relation join='inner' type='join'><clause type='join'>"
        "<expression op='='><expression op='[PRODUCTS].[PROD_ID]' />"
        "<expression op='[PRICES].[PROD_ID]' /></expression></clause>"
        "<relation connection='db.1' name='PRODUCTS' table='[SALES].[PRODUCTS]' type='table' />"
        "<relation connection='db.1' name='PRICES' table='[SALES].[PRICES]' type='table' />"
        "</relation></properties>"
    )
    xml = datasource()
    assert xml.count(plain) == 1
    reasons = unsupported_reasons(tmp_path, xml.replace(plain, joined))
    assert any("「Products」內含 join（實體層），Live 連線尚不支援" in r for r in reasons)


def test_calculated_relationship_unsupported(tmp_path: Path) -> None:
    plain = "<expression op='[Cust ID]' />"
    calculated = "<expression op='UPPER'><expression op='[Cust ID]' /></expression>"
    xml = datasource()
    assert xml.count(plain) == 1
    reasons = unsupported_reasons(tmp_path, xml.replace(plain, calculated))
    assert any("關聯條件不是欄位比較" in r for r in reasons)


# ---------------------------------------------------------------- 引擎


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
        for name, table in TABLES.items():
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
            for name, table in TABLES.items():
                pg_load(con, name, table, "SALES")
            yield Engine("postgres", postgres_executor(con))
            con.close()
        return
    with spark_session() as spark:
        for name, table in TABLES.items():
            spark.createDataFrame(table).createOrReplaceTempView(name)
        yield Engine("spark", SparkExecutor(spark), spark=True)


def for_engine(ir: IR, engine: Engine) -> IR:
    """Spark 的暫存檢視沒有結構描述。"""
    if not engine.spark:
        return ir
    datasources = []
    for ds in ir.datasources:
        connection = ds.connection
        assert isinstance(connection, LiveConnection)
        model = connection.relation
        assert isinstance(model, ModelRelation)
        tables = []
        for logical in model.tables:
            relation = logical.relation
            assert isinstance(relation, TableRelation)
            relation = relation.model_copy(update={"table": relation.table[-1:]})
            tables.append(logical.model_copy(update={"relation": relation}))
        model = model.model_copy(update={"tables": tables})
        connection = connection.model_copy(update={"relation": model})
        datasources.append(ds.model_copy(update={"connection": connection}))
    return ir.model_copy(update={"datasources": datasources})


def run(ir: IR, engine: Engine, state: QueryState | None = None) -> QueryResult:
    query = worksheet_sql(for_engine(ir, engine), ir.worksheets[0].id, state)
    return execute_live(query, engine.executor)


def rows_of(result: QueryResult) -> dict[Any, Any]:
    """第一欄 → 其餘各欄（只有一欄時為該值）。"""
    out: dict[Any, Any] = {}
    for row in result.rows:
        rest = tuple(_plain(v) for v in row[1:])
        out[row[0]] = rest[0] if len(rest) == 1 else rest
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ---------------------------------------------------------------- 查詢


def test_single_table(model_ir: IR, engine: Engine) -> None:
    # 只用到 ORDERS：只查那一張
    ir = view(model_ir, [dim(model_ir, "Cust ID")], [measure(model_ir, "Sales")])
    assert rows_of(run(ir, engine)) == {"C1": 150, "C2": 70, "C3": 30, "C9": 10, None: 20}
    notes = [d for n in worksheet_sql(ir, "Base").plan for d in n.detail]
    assert "Relationship 模型：只用到「Orders」" in notes


def test_dimension_from_related_table(model_ir: IR, engine: Engine) -> None:
    # 沒有對應客戶的訂單（C9、Null）在 Null；沒有訂單的 Dee 也列出（量值為 Null）
    ir = view(model_ir, [dim(model_ir, "Name")], [measure(model_ir, "Sales")])
    assert rows_of(run(ir, engine)) == {
        "Ann": 150,
        "Bob": 70,
        "Cid": 30,
        "Dee": None,
        None: 30,
    }


def test_nested_related_table(model_ir: IR, engine: Engine) -> None:
    # ORDERS → CUSTOMERS → REGIONS；沒有訂單的 South、沒有客戶的 North 也列出
    ir = view(model_ir, [dim(model_ir, "Region")], [measure(model_ir, "Sales")])
    assert rows_of(run(ir, engine)) == {
        "East": 180,
        "West": 70,
        "South": None,
        "North": None,
        None: 30,
    }


def test_one_to_many_not_duplicated(model_ir: IR, engine: Engine) -> None:
    # P1 有兩個類別：O1、O3 各算在 Furniture 與 Office 一次（不是兩次）；P4 沒有對應在 Null
    ir = view(model_ir, [dim(model_ir, "Category")], [measure(model_ir, "Sales")])
    assert rows_of(run(ir, engine)) == {
        "Furniture": 170,
        "Office": 230,
        "Tech": 30,
        "Toys": None,
        None: 20,
    }


def test_table_count_field(model_ir: IR, engine: Engine) -> None:
    # Orders 的筆數欄位：各客戶的訂單數
    ir = view(model_ir, [dim(model_ir, "Name")], [measure(model_ir, "Orders")])
    assert rows_of(run(ir, engine)) == {"Ann": 2, "Bob": 1, "Cid": 1, "Dee": None, None: 2}


def test_dimension_only_inner_join(model_ir: IR, engine: Engine) -> None:
    # 只有維度：只列出存在的組合（經由 ORDERS 內部連接）
    ir = view(model_ir, [dim(model_ir, "Name"), dim(model_ir, "Category")])
    combos = {(r[0], r[1]) for r in run(ir, engine).rows}
    assert combos == {
        ("Ann", "Furniture"),
        ("Ann", "Office"),
        ("Bob", "Furniture"),
        ("Bob", "Office"),
        ("Cid", "Tech"),
    }
    # 只用到一張資料表：所有值
    names = run(view(model_ir, [dim(model_ir, "Name")]), engine).rows
    assert [r[0] for r in names] == ["Ann", "Bob", "Cid", "Dee"]


def test_filter_on_related_table_only(model_ir: IR, engine: Engine) -> None:
    # 篩選只用到 PRODUCTS：保留「有關聯、且關聯到的列中有符合者」的訂單，不重複計算
    sales = [measure(model_ir, "Sales")]
    office = view(
        model_ir,
        [dim(model_ir, "Cust ID")],
        sales,
        [categorical(model_ir, "Category", "include", ["Office"])],
    )
    assert rows_of(run(office, engine)) == {"C1": 150, "C2": 70, "C9": 10}
    # 包含 Null：沒有對應產品的 O6 以 Null 判斷而保留
    tech = view(
        model_ir,
        [dim(model_ir, "Cust ID")],
        sales,
        [categorical(model_ir, "Category", "include", ["Tech", None])],
    )
    assert rows_of(run(tech, engine)) == {"C3": 30, None: 20}
    # 排除 Furniture：P1 也有 Office，O1、O3 仍保留；O6 的 Null 不在排除清單中
    no_furniture = view(
        model_ir,
        [dim(model_ir, "Cust ID")],
        sales,
        [categorical(model_ir, "Category", "exclude", ["Furniture"])],
    )
    assert rows_of(run(no_furniture, engine)) == {"C1": 150, "C2": 70, "C3": 30, "C9": 10, None: 20}


def test_fixed_lod_before_dimension_filter(model_ir: IR, engine: Engine) -> None:
    # FIXED LOD 在維度篩選之前計算：C1 的總額 150 不受 Furniture 篩選影響；內容篩選則先套用
    cols = [measure(model_ir, "Sales"), measure(model_ir, "Cust Total", "min")]
    furniture = categorical(model_ir, "Category", "include", ["Furniture"])
    ir = view(model_ir, [dim(model_ir, "Cust ID")], cols, [furniture])
    assert rows_of(run(ir, engine)) == {"C1": (100, 150), "C2": (70, 70)}
    context = furniture.model_copy(update={"stage": "context"})
    ir = view(model_ir, [dim(model_ir, "Cust ID")], cols, [context])
    assert rows_of(run(ir, engine)) == {"C1": (100, 100), "C2": (70, 70)}


def test_filter_on_view_field_of_related_table(model_ir: IR, engine: Engine) -> None:
    # 篩選的欄位也在檢視中：直接篩選連接後的列（O1 在 Office 的那一列保留）
    state = QueryState(
        filters={ids(model_ir)["Category"]: FilterValue(mode="include", values=["Office"])}
    )
    ir = view(model_ir, [dim(model_ir, "Category")], [measure(model_ir, "Sales")])
    assert rows_of(run(ir, engine, state)) == {"Office": 230}


def test_top_n_on_related_field(model_ir: IR, engine: Engine) -> None:
    top = TopNFilter(
        kind="top_n",
        id="flt_top",
        field=dim(model_ir, "Name"),
        stage="top_n",
        direction="top",
        count=2,
        by="SUM([Sales])",
        source_ref="test",
    )
    ir = view(model_ir, [dim(model_ir, "Name")], [measure(model_ir, "Sales")], [top])
    assert rows_of(run(ir, engine)) == {"Ann": 150, "Bob": 70}


def test_guard_passes_for_many_to_one(model_ir: IR, engine: Engine) -> None:
    # 依 Cust ID 排序（層級不含 Name）：CUSTOMERS 每個客戶一列，檢查通過，排序值不重複計算
    sort = Sort(
        field=ids(model_ir)["Cust ID"], kind="by_field", order="desc", by=measure(model_ir, "Sales")
    )
    ir = view(
        model_ir,
        [dim(model_ir, "Cust ID"), dim(model_ir, "Name")],
        [measure(model_ir, "Sales")],
        sorts=[sort],
    )
    query = worksheet_sql(ir, "Base")
    assert len(query.guards) == 1
    result = run(ir, engine)
    assert [(r[0], r[1], _plain(r[2])) for r in result.rows] == [
        ("C1", "Ann", 150),
        ("C2", "Bob", 70),
        ("C3", "Cid", 30),
        (None, None, 20),
        ("C9", None, 10),
    ]


def test_guard_blocks_one_to_many(model_ir: IR, engine: Engine) -> None:
    # 依 Cust ID 排序但檢視含 Category：P1 有兩個類別，排序值會重複計算 → 列為不支援
    sort = Sort(
        field=ids(model_ir)["Cust ID"], kind="by_field", order="desc", by=measure(model_ir, "Sales")
    )
    ir = view(
        model_ir,
        [dim(model_ir, "Cust ID"), dim(model_ir, "Category")],
        [measure(model_ir, "Sales")],
        sorts=[sort],
    )
    with pytest.raises(QueryError, match="不是多對一") as info:
        run(ir, engine)
    assert info.value.unsupported


def test_guard_not_needed_for_duplicate_safe_aggregates(model_ir: IR, engine: Engine) -> None:
    # 與上一個相同的檢視，但量值與排序都是 MAX：重複的列不影響結果，不需要檢查
    sort = Sort(
        field=ids(model_ir)["Cust ID"],
        kind="by_field",
        order="desc",
        by=measure(model_ir, "Sales", "max"),
    )
    ir = view(
        model_ir,
        [dim(model_ir, "Cust ID"), dim(model_ir, "Category")],
        [measure(model_ir, "Sales", "max")],
        sorts=[sort],
    )
    assert worksheet_sql(ir, "Base").guards == []
    assert [(r[0], r[1], _plain(r[2])) for r in run(ir, engine).rows] == [
        ("C1", "Furniture", 100),
        ("C1", "Office", 100),
        ("C2", "Furniture", 70),
        ("C2", "Office", 70),
        ("C3", "Tech", 30),
        (None, None, 20),
        ("C9", "Office", 10),
    ]


def test_guard_not_needed_for_relationship_keys(model_ir: IR, engine: Engine) -> None:
    # 分支只用到關聯欄位本身（Cust ID (Customers)）：一個關聯值只對應一個值，排序值不會重複計算
    sort = Sort(
        field=ids(model_ir)["Cust ID"], kind="by_field", order="desc", by=measure(model_ir, "Sales")
    )
    ir = view(
        model_ir,
        [dim(model_ir, "Cust ID"), dim(model_ir, "Cust ID (Customers)")],
        [measure(model_ir, "Sales")],
        sorts=[sort],
    )
    assert worksheet_sql(ir, "Base").guards == []
    assert [(r[0], r[1], _plain(r[2])) for r in run(ir, engine).rows] == [
        ("C1", "C1", 150),
        ("C2", "C2", 70),
        ("C3", "C3", 30),
        (None, None, 20),
        ("C9", None, 10),
    ]


# ---------------------------------------------------------------- 量值來自多張資料表


def test_measures_from_two_tables(model_ir: IR, engine: Engine) -> None:
    """各量值在自己的資料表彙總，再依維度合併（完整外部連接）：任一邊有的維度值都列出，沒有該資料表的
    列時它的量值為 Null（COUNTD 與筆數也是 Null，不是 0）。"""
    cols = [measure(model_ir, "Sales"), measure(model_ir, "Customers")]
    # 維度在 CUSTOMERS：沒有訂單的 Dee 有客戶數、沒有銷售；沒有對應客戶的訂單（C9、Null）在 Null
    by_name = view(model_ir, [dim(model_ir, "Name")], cols)
    assert rows_of(run(by_name, engine)) == {
        "Ann": (150, 1),
        "Bob": (70, 1),
        "Cid": (30, 1),
        "Dee": (None, 1),
        None: (30, None),
    }
    # 維度在兩邊共同的維度表 REGIONS：沒有客戶的 North 也列出（另以維度查詢）
    by_region = view(model_ir, [dim(model_ir, "Region")], cols)
    assert rows_of(run(by_region, engine)) == {
        "East": (180, 2),
        "West": (70, 1),
        "South": (None, 1),
        "North": (None, None),
        None: (30, None),
    }
    # 維度在 ORDERS：C9 不在 CUSTOMERS；沒有訂單的 Dee 算在 Null（與沒有客戶的 O6 同一個標記）
    by_cust = view(
        model_ir,
        [dim(model_ir, "Cust ID")],
        [measure(model_ir, "Sales"), measure(model_ir, "Name", "countd")],
    )
    assert rows_of(run(by_cust, engine)) == {
        "C1": (150, 1),
        "C2": (70, 1),
        "C3": (30, 1),
        "C9": (10, None),
        None: (20, 1),
    }
    query = worksheet_sql(by_name, "Base")
    assert query.guards == []
    notes = [d for n in query.plan for d in n.detail]
    assert any("量值來自「Orders」、「Customers」" in n for n in notes)


def test_fixed_lod_with_measure_from_another_table(model_ir: IR, engine: Engine) -> None:
    """FIXED LOD 的量值在 CUSTOMERS（Buyers：買過該產品的客戶數），維度在 ORDERS 與 PRODUCTS；
    檢視依 ORDERS 的 Prod ID，Open Buyers = MAX(Buyers) - COUNTD(訂單的 Cust ID)。

    LOD 以 CUSTOMERS 為量值表：C1 →（P1 Furniture）（P1 Office）（P2 Office）、C2 →（P1 Furniture）
    （P1 Office）、C3 →（P3 Tech）、C4 沒有訂單 →（Null、Null）；（P1、Other）2、（P1、Office）2、
    （P2、Office）1、（P3、Other）1、（Null、Other）1。檢視的彙總都屬於 ORDERS，標記只來自 ORDERS 的列：
    P4 沒有對應的產品（P4、Other），不在 LOD 的結果中，Buyers 為 Null。
    """
    cols = [
        measure(model_ir, "Cust ID", "countd"),
        measure(model_ir, "Buyers", "max"),
        FieldUse(field=ids(model_ir)["Open Buyers"]),
    ]
    ir = view(model_ir, [dim(model_ir, "Prod ID")], cols)
    assert rows_of(run(ir, engine)) == {
        "P1": (2, 2, 0),
        "P2": (2, 1, -1),
        "P3": (1, 1, 0),
        "P4": (0, None, None),
    }
    # 只用到 REGIONS 的維度篩選：兩部分各以自己的半連接套用；FIXED LOD 在維度篩選之前計算
    east = categorical(model_ir, "Region", "include", ["East"])
    filtered = view(model_ir, [dim(model_ir, "Prod ID")], cols, [east])
    assert rows_of(run(filtered, engine)) == {"P1": (1, 2, 1), "P2": (1, 1, 0), "P3": (1, 1, 0)}
    assert worksheet_sql(filtered, "Base").guards == []
    # 改為內容篩選：LOD 只算 East 的客戶（C1、C3）
    context = east.model_copy(update={"stage": "context"})
    ir = view(model_ir, [dim(model_ir, "Prod ID")], cols, [context])
    assert rows_of(run(ir, engine)) == {"P1": (1, 1, 0), "P2": (1, 1, 0), "P3": (1, 1, 0)}


def test_top_n_with_measures_from_two_tables(model_ir: IR, engine: Engine) -> None:
    # 依 SUM(Sales) 取前 2 名（只以 ORDERS 的列排名）；成員的篩選套用到兩部分的列
    top = TopNFilter(
        kind="top_n",
        id="flt_top",
        field=dim(model_ir, "Name"),
        stage="top_n",
        direction="top",
        count=2,
        by="SUM([Sales])",
        source_ref="test",
    )
    cols = [measure(model_ir, "Sales"), measure(model_ir, "Customers")]
    ir = view(model_ir, [dim(model_ir, "Name")], cols, [top])
    assert rows_of(run(ir, engine)) == {"Ann": (150, 1), "Bob": (70, 1)}


# ---------------------------------------------------------------- 不支援


def test_aggregate_mixing_two_tables_unsupported(model_ir: IR) -> None:
    # 同一個彙總用到 CUSTOMERS 與 ORDERS 的欄位（Tableau 以列層級連接後再彙總）
    ir = view(model_ir, [dim(model_ir, "Region")], [measure(model_ir, "Ann Sales")])
    with pytest.raises(QueryError, match="同時用到多張資料表（「Customers」、「Orders」）") as info:
        worksheet_sql(ir, "Base")
    assert info.value.unsupported


def test_filter_across_branches_unsupported(model_ir: IR) -> None:
    # Name + Category 跨兩個分支，又不在檢視中
    flt = categorical(model_ir, "Name Category", "include", ["AnnOffice"])
    ir = view(model_ir, [dim(model_ir, "Cust ID")], [measure(model_ir, "Sales")], [flt])
    with pytest.raises(QueryError, match="多個分支") as info:
        worksheet_sql(ir, "Base")
    assert info.value.unsupported


def test_domain_values(model_ir: IR) -> None:
    # 篩選控制項的可選值：該欄位所在的資料表
    sql = domain_sql(model_ir, model_ir.datasources[0].id, dim(model_ir, "Category"), "values")
    assert "PRODUCTS" in sql and "ORDERS" not in sql


# ---------------------------------------------------------------- 萃取


def make_hyper(path: Path, log_dir: Path) -> None:
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

    types = {"string": SqlType.text(), "real": SqlType.double(), "integer": SqlType.big_int()}
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        for name, table in TABLES.items():
            definition = TableDefinition(
                TableName("Extract", OBJECTS[name][1]),
                [TableDefinition.Column(c[0], types[c[2]]) for c in COLUMNS[name]],
            )
            conn.catalog.create_table(definition)
            with Inserter(conn, definition) as inserter:
                inserter.add_rows([list(r.values()) for r in table.to_pylist()])
                inserter.execute()


def test_extract_model(tmp_path: Path) -> None:
    from tmig.data.hyper_import import import_workbook

    hyper = tmp_path / "model.hyper"
    make_hyper(hyper, tmp_path)
    xml = b.workbook(datasource=datasource(extract=True), worksheets=base_sheet())
    twbx = b.write_twbx(tmp_path, xml, {"Data/Extracts/model.hyper": hyper.read_bytes()})
    ir = parse_workbook(twbx)
    assert ir.unsupported == []
    ds = ir.datasources[0]
    assert isinstance(ds.connection, ExtractConnection) and ds.connection.model is not None
    out = tmp_path / "data"
    (result,) = import_workbook(twbx, out, ir)
    assert result.ok, result.to_json()
    assert [t.table for t in result.tables] == [o[1] for o in OBJECTS.values()]
    assert [t.rows for t in result.tables] == [6, 4, 4, 5]

    database = out / f"{ds.id}.duckdb"
    with DataRuntime(configs={}) as runtime:

        def query(state_ir: IR) -> dict[Any, Any]:
            sql = worksheet_sql(state_ir, "Base")
            return rows_of(runtime.run(sql, ds, database))

        assert query(view(ir, [dim(ir, "Name")], [measure(ir, "Sales")])) == {
            "Ann": 150,
            "Bob": 70,
            "Cid": 30,
            "Dee": None,
            None: 30,
        }
        assert query(view(ir, [dim(ir, "Category")], [measure(ir, "Sales")])) == {
            "Furniture": 170,
            "Office": 230,
            "Tech": 30,
            "Toys": None,
            None: 20,
        }
        # 量值來自兩張資料表
        two = [measure(ir, "Sales"), measure(ir, "Customers")]
        assert query(view(ir, [dim(ir, "Name")], two)) == {
            "Ann": (150, 1),
            "Bob": (70, 1),
            "Cid": (30, 1),
            "Dee": (None, 1),
            None: (30, None),
        }
        sort = Sort(
            field=ids(ir)["Cust ID"], kind="by_field", order="desc", by=measure(ir, "Sales")
        )
        blocked = view(
            ir, [dim(ir, "Cust ID"), dim(ir, "Category")], [measure(ir, "Sales")], sorts=[sort]
        )
        with pytest.raises(QueryError, match="不是多對一"):
            query(blocked)
