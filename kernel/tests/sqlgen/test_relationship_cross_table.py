"""Relationship 模型中跨資料表的計算（一個計算用到多張邏輯資料表的欄位），以手算的結果驗證。

Tableau 的語意（Tableau 2021.2 說明文件「How Analysis Works for Data Sources that Use Relationships」的
疑難排解）：計算的定義域是它的輸入的內部連接——用到的資料表以關聯內部連接，在連接後的每一列計算，
沒有對應的列不參與（與 IF、IFNULL 一起用時結果因此可能出乎意料）。

資料（SALES 結構描述）：

- ORDERS：O1 C1 P1 1 100、O2 C1 P2 2 50、O3 C2 P1 3 70、O4 C3 P3 4 30、O5 C9 P2 5 10、O6 Null P4 6 20
  （C9 不在 CUSTOMERS；P4 不在 PRODUCTS）
- CUSTOMERS：C1 Ann R1、C2 Bob R2、C3 Cid R1、C4 Dee R3（Dee 沒有訂單）
- REGIONS：R1 East、R2 West、R3 South、R4 North
- PRODUCTS：P1 Furniture 5、P1 Office 5（同一個產品兩列）、P2 Office 7、P3 Tech 11、P5 Toys 13

ORDERS 與 PRODUCTS 的內部連接（Amount = Qty × Price）：O1 兩列各 5、O2 14、O3 兩列各 15、O4 44、O5 35，
O6 沒有對應的產品不在其中；合計 133。

在 DuckDB 檔（當作來源資料庫）、PostgreSQL 與 MySQL 上執行（沒有 PostgreSQL、MySQL 時略過）。
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
from tmig.ir import IR, CategoricalFilter, FieldUse
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..engines import (
    mysql_connect,
    mysql_executor,
    mysql_load,
    mysql_server,
    pg_connect,
    pg_load,
    postgres_cluster,
    postgres_executor,
)
from ..parser import builder as b
from ..parser.builder import DS, instance, ref, worksheet

TABLES = {
    "ORDERS": pa.table(
        {
            "ORDER_ID": ["O1", "O2", "O3", "O4", "O5", "O6"],
            "CUST_ID": ["C1", "C1", "C2", "C3", "C9", None],
            "PROD_ID": ["P1", "P2", "P1", "P3", "P2", "P4"],
            "SALES": [100.0, 50.0, 70.0, 30.0, 10.0, 20.0],
            "QTY": pa.array([1, 2, 3, 4, 5, 6], pa.int64()),
        }
    ),
    "CUSTOMERS": pa.table(
        {
            "CUST_ID": ["C1", "C2", "C3", "C4"],
            "NAME": ["Ann", "Bob", "Cid", "Dee"],
            "REGION_ID": ["R1", "R2", "R1", "R3"],
        }
    ),
    "REGIONS": pa.table(
        {"REGION_ID": ["R1", "R2", "R3", "R4"], "REGION": ["East", "West", "South", "North"]}
    ),
    "PRODUCTS": pa.table(
        {
            "PROD_ID": ["P1", "P1", "P2", "P3", "P5"],
            "CATEGORY": ["Furniture", "Office", "Office", "Tech", "Toys"],
            "PRICE": [5.0, 5.0, 7.0, 11.0, 13.0],
        }
    ),
}
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
        ("PRICE", "Price", "real", "Sum"),
    ],
}
RELATIONSHIPS = [
    ("Cust ID", "Cust ID (Customers)", "ORDERS", "CUSTOMERS"),
    ("Prod ID", "Prod ID (Products)", "ORDERS", "PRODUCTS"),
    ("Region ID", "Region ID (Regions)", "CUSTOMERS", "REGIONS"),
]
FCP = "_.fcp.ObjectModelEncapsulateLegacy.true..."
CALCS = [
    ("Calculation_1", "Amount", "[Qty] * [Price]", "real", "measure"),
    ("Calculation_2", "Ann Sales", "IF [Name] = 'Ann' THEN [Sales] END", "real", "measure"),
    ("Calculation_3", "East Sales", "IF [Region] = 'East' THEN [Sales] END", "real", "measure"),
    ("Calculation_4", "Pick", "IFNULL([Category], [Order ID])", "string", "dimension"),
    ("Calculation_5", "Name Category", "[Name] + [Category]", "string", "dimension"),
    ("Calculation_6", "Big East", "[Region] = 'East' OR [Sales] > 60", "boolean", "dimension"),
    ("Calculation_7", "Cust Amount", "{FIXED [Cust ID] : SUM([Amount])}", "real", "measure"),
]


def datasource() -> str:
    collection = "".join(
        f"<relation connection='db.1' name='{t}' table='[SALES].[{t}]' type='table' />"
        for t in TABLES
    )
    records = []
    for table, columns in COLUMNS.items():
        for remote, local, datatype, agg in columns:
            records.append(
                f"""<metadata-record class='column'>
              <remote-name>{remote}</remote-name><remote-type>129</remote-type>
              <local-name>[{local}]</local-name><parent-name>[{table}]</parent-name>
              <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
              <{FCP}object-id>[{OBJECTS[table][1]}]</{FCP}object-id>
            </metadata-record>"""
            )
    objects = "".join(
        f"<object caption='{caption}' id='{object_id}'><properties context=''>"
        f"<relation connection='db.1' name='{table}' table='[SALES].[{table}]' type='table' />"
        "</properties></object>"
        for table, (caption, object_id) in OBJECTS.items()
    )
    rels = "".join(
        f"""<relationship>
          <expression op='='><expression op='[{left}]' /><expression op='[{right}]' /></expression>
          <first-end-point object-id='{OBJECTS[first][1]}' />
          <second-end-point object-id='{OBJECTS[second][1]}' />
        </relationship>"""
        for left, right, first, second in RELATIONSHIPS
    )
    calcs = "".join(b.calc(*c) for c in CALCS)
    return f"""
    <datasource caption='Sales Model' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='db' name='db.1'>
            <connection authentication='username-password' class='postgres' dbname='sales'
              port='5432' server='pg.example.com' />
          </named-connection>
        </named-connections>
        <{FCP}relation type='collection'>{collection}</{FCP}relation>
        <metadata-records>{"".join(records)}</metadata-records>
      </connection>
      <aliases enabled='yes' />
      {calcs}
      <{FCP}object-graph><objects>{objects}</objects><relationships>{rels}</relationships>
      </{FCP}object-graph>
    </datasource>"""


@pytest.fixture(scope="module")
def model_ir(tmp_path_factory: pytest.TempPathFactory) -> IR:
    sheet = worksheet(
        "Base",
        [instance("[Cust ID]", "None", "[none:Cust ID:nk]")],
        rows=ref("none:Cust ID:nk"),
    )
    tmp = tmp_path_factory.mktemp("cross_table")
    ir = parse_workbook(b.write_twb(tmp, b.workbook(datasource=datasource(), worksheets=sheet)))
    assert ir.unsupported == []
    return ir


@dataclass
class Engine:
    name: str
    executor: Executor


@pytest.fixture(scope="module", params=["duckdb", "postgres", "mysql"])
def engine(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Engine]:
    if request.param == "duckdb":
        path = tmp_path_factory.mktemp("source") / "source.duckdb"
        con = duckdb.connect(str(path))
        con.execute('CREATE SCHEMA "SALES"')
        for name, table in TABLES.items():
            con.register(f"rows_{name}", table)
            con.execute(f'CREATE TABLE "SALES"."{name}" AS SELECT * FROM "rows_{name}"')
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
    with mysql_server() as params:
        # 資料表在 SALES 資料庫（MySQL 的結構描述即資料庫）
        con = mysql_connect(params, "SALES")
        for name, table in TABLES.items():
            mysql_load(con, name, table)
        yield Engine("mysql", mysql_executor(con))
        con.close()


def ids(ir: IR) -> dict[str, str]:
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
) -> IR:
    ws = ir.worksheets[0].model_copy(
        update={"rows": rows, "columns": cols or [], "filters": filters or [], "sorts": []}
    )
    return ir.model_copy(update={"worksheets": [ws]})


def categorical(
    ir: IR, caption: str, values: list[Any], stage: str = "dimension"
) -> CategoricalFilter:
    return CategoricalFilter(
        kind="categorical",
        id=f"flt_{caption.lower().replace(' ', '_')}",
        field=dim(ir, caption),
        stage=stage,  # type: ignore[arg-type]
        mode="include",
        values=values,
        source_ref="test",
    )


def run(ir: IR, engine: Engine) -> QueryResult:
    return execute_live(worksheet_sql(ir, "Base"), engine.executor)


def rows_of(result: QueryResult) -> dict[Any, Any]:
    """第一欄 → 其餘各欄（只有一欄時為該值）。"""
    out: dict[Any, Any] = {}
    for row in result.rows:
        rest = tuple(_plain(v) for v in row[1:])
        out[_plain(row[0])] = rest[0] if len(rest) == 1 else rest
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def notes(ir: IR) -> list[str]:
    return [d for n in worksheet_sql(ir, "Base").plan for d in n.detail]


# ---------------------------------------------------------------- 彙總中的跨資料表計算


def test_aggregate_over_inner_join(model_ir: IR, engine: Engine) -> None:
    """SUM([Qty] * [Price])：在 ORDERS 與 PRODUCTS 內部連接後的每一列計算——P1 有兩列，O1、O3 各算兩次；
    沒有對應產品的 O6 不參與（它的 Cust ID 是 Null，維度的成員另外查詢，量值為 Null）。"""
    amount = [measure(model_ir, "Amount")]
    total = view(model_ir, [], amount)
    assert [_plain(r[0]) for r in run(total, engine).rows] == [133]
    by_cust = view(model_ir, [dim(model_ir, "Cust ID")], amount)
    assert rows_of(run(by_cust, engine)) == {"C1": 24, "C2": 30, "C3": 44, "C9": 35, None: None}
    assert "Relationship 模型：量值在「Orders」與「Products」的內部連接彙總" in notes(by_cust)


def test_dimension_inside_and_outside_the_join(model_ir: IR, engine: Engine) -> None:
    amount = [measure(model_ir, "Amount")]
    # 維度在 PRODUCTS（內部連接中的資料表）：沒有訂單的 Toys 另以維度表查詢列出
    by_category = view(model_ir, [dim(model_ir, "Category")], amount)
    assert rows_of(run(by_category, engine)) == {
        "Furniture": 20,
        "Office": 69,
        "Tech": 44,
        "Toys": None,
    }
    # 維度在 CUSTOMERS（接在內部連接的 ORDERS 上的分支）：C9 的 O5 在 Null；沒有訂單的 Dee 也列出
    by_name = view(model_ir, [dim(model_ir, "Name")], amount)
    assert rows_of(run(by_name, engine)) == {
        "Ann": 24,
        "Bob": 30,
        "Cid": 44,
        "Dee": None,
        None: 35,
    }


def test_with_measure_from_one_table(model_ir: IR, engine: Engine) -> None:
    """SUM(Sales) 在 ORDERS 彙總、SUM(Amount) 在內部連接後的列彙總，依維度合併：O6（沒有對應的產品）的
    銷售在 Null，Amount 沒有這一列。"""
    cols = [measure(model_ir, "Sales"), measure(model_ir, "Amount")]
    ir = view(model_ir, [dim(model_ir, "Category")], cols)
    assert rows_of(run(ir, engine)) == {
        "Furniture": (170, 20),
        "Office": (230, 69),
        "Tech": (30, 44),
        "Toys": (None, None),
        None: (20, None),
    }
    assert any("量值來自「Orders」、「Orders」與「Products」的內部連接" in n for n in notes(ir))


def test_condition_on_another_table(model_ir: IR, engine: Engine) -> None:
    """SUM(IF [Name] = 'Ann' THEN [Sales] END)：只有 ORDERS 與 CUSTOMERS 都有的列（C9 與 Null 的訂單不在
    其中）；Bob 的 West 有標記但值為 Null，沒有客戶的 South、沒有客戶的 North 另以維度表列出。"""
    ir = view(model_ir, [dim(model_ir, "Region")], [measure(model_ir, "Ann Sales")])
    assert rows_of(run(ir, engine)) == {"East": 150, "West": None, "South": None, "North": None}


def test_tables_on_the_path_are_joined(model_ir: IR, engine: Engine) -> None:
    """IF [Region] = 'East' THEN [Sales] END：REGIONS 經 CUSTOMERS 連到 ORDERS，三張資料表內部連接
    （推定：路徑上的資料表也以內部連接）。O5（C9）、O6 沒有客戶不參與；P4 另以維度查詢列出。"""
    ir = view(model_ir, [dim(model_ir, "Prod ID")], [measure(model_ir, "East Sales")])
    assert rows_of(run(ir, engine)) == {"P1": 100, "P2": 50, "P3": 30, "P4": None}
    assert "Relationship 模型：量值在「Orders」、「Customers」與「Regions」的內部連接彙總" in notes(
        ir
    )


def test_fixed_lod_over_inner_join(model_ir: IR, engine: Engine) -> None:
    """{FIXED [Cust ID] : SUM([Amount])}：各客戶在內部連接後的列的合計，併回每一列（ORDERS 的列也有）；
    Cust ID 為 Null 的 O6 不在內部連接中，LOD 為 Null。"""
    cols = [measure(model_ir, "Sales"), measure(model_ir, "Cust Amount", "min")]
    ir = view(model_ir, [dim(model_ir, "Cust ID")], cols)
    assert rows_of(run(ir, engine)) == {
        "C1": (150, 24),
        "C2": (70, 30),
        "C3": (30, 44),
        "C9": (10, 35),
        None: (20, None),
    }


def test_filter_on_branch_of_the_join(model_ir: IR, engine: Engine) -> None:
    """只用到 REGIONS 的維度篩選（East）：內部連接後的列以半連接保留有關聯且符合者（O3 是 West、O5 沒有
    客戶）。"""
    east = categorical(model_ir, "Region", ["East"])
    ir = view(model_ir, [dim(model_ir, "Category")], [measure(model_ir, "Amount")], [east])
    assert rows_of(run(ir, engine)) == {"Furniture": 5, "Office": 19, "Tech": 44}


# ---------------------------------------------------------------- 列層級的跨資料表計算


def test_row_level_dimension_outside_its_domain_is_null(model_ir: IR, engine: Engine) -> None:
    """IFNULL([Category], [Order ID]) 當維度：定義域是 ORDERS 與 PRODUCTS 的內部連接，沒有對應產品的 O6
    不在其中，它的值是 Null（不是 IFNULL 補上的 O6）；O6 的銷售照樣保留在 Null（推定）。"""
    ir = view(model_ir, [dim(model_ir, "Pick")], [measure(model_ir, "Sales", "max")])
    assert rows_of(run(ir, engine)) == {"Furniture": 100, "Office": 100, "Tech": 30, None: 20}


def test_filter_using_measure_table_and_branch(model_ir: IR, engine: Engine) -> None:
    """[Region] = 'East' OR [Sales] > 60（REGIONS 與 ORDERS）當篩選：訂單保留「關聯到的客戶與區域中有一組
    使條件成立者」；O3 是 West 但銷售 70、O5 與 O6 沒有客戶（在定義域之外，條件不成立）。"""
    flt = categorical(model_ir, "Big East", [True])
    ir = view(model_ir, [dim(model_ir, "Cust ID")], [measure(model_ir, "Sales")], [flt])
    assert rows_of(run(ir, engine)) == {"C1": 150, "C2": 70, "C3": 30}
    assert any("篩選 flt_big_east 用到「Customers」、「Regions」" in n for n in notes(ir))


def test_filter_across_two_branches(model_ir: IR, engine: Engine) -> None:
    """[Name] + [Category]（CUSTOMERS 與 PRODUCTS，經 ORDERS 相連）當篩選：兩個分支的列的組合中有一組
    使條件成立的訂單保留；選 Null 時保留在定義域之外的訂單（C9 的 O5、沒有客戶與產品的 O6）。"""
    office = categorical(model_ir, "Name Category", ["AnnOffice"])
    ir = view(model_ir, [dim(model_ir, "Cust ID")], [measure(model_ir, "Sales")], [office])
    assert rows_of(run(ir, engine)) == {"C1": 150}
    nulls = categorical(model_ir, "Name Category", [None])
    ir = view(model_ir, [dim(model_ir, "Cust ID")], [measure(model_ir, "Sales")], [nulls])
    assert rows_of(run(ir, engine)) == {"C9": 10, None: 20}
