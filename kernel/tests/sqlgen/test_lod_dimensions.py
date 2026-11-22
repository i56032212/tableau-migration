"""LOD 的維度本身含 LOD（世代分析）：{FIXED [First Order Month] : COUNTD([Customer ID])}，其中
[First Order Month] 是 {FIXED [Customer ID] : MIN(DATETRUNC('month', [Order Date]))}。

Tableau 2021.2 說明文件：LOD 的維度可以是「任何結果是維度的運算式」
（https://help.tableau.com/v2021.2/pro/desktop/en-us/calculations_calculatedfields_lod.htm ）；FIXED 的結果
可以是維度，INCLUDE／EXCLUDE 的結果一律是量值
（https://help.tableau.com/v2021.2/pro/desktop/en-us/calculations_calculatedfields_lod_overview.htm ）；
FIXED 在維度篩選之前、內容篩選之後計算，INCLUDE／EXCLUDE 在維度篩選之後
（https://help.tableau.com/v2021.2/pro/desktop/en-us/calculations_calculatedfields_lod_filters.htm ，
皆 2026-11-23 查閱）。所以維度中的 FIXED 先以內容篩選後的資料算好，外層再依它的值分組。

資料（顧客、區隔、訂單日期、銷售）：

| Customer ID | Segment   | Order Date | Sales |
|-------------|-----------|------------|-------|
| C1          | Consumer  | 2023-01-05 | 100   |
| C1          | Consumer  | 2023-02-10 | 50    |
| C2          | Corporate | 2023-01-20 | 200   |
| C2          | Corporate | 2023-03-02 | 30    |
| C3          | Consumer  | 2023-02-14 | 80    |
| C4          | Corporate | 2023-02-25 | 40    |
| C4          | Corporate | 2023-03-30 | 60    |
| C5          | Consumer  | 2023-03-08 | 10    |
| C6          | Consumer  | 2023-01-12 | 70    |

第一次下單的月份：C1、C2、C6 為 1 月，C3、C4 為 2 月，C5 為 3 月；各世代的顧客數 3、2、1。

在 DuckDB 上執行；同一份資料另以 Live 連線在 PostgreSQL、MySQL 上執行（沒有引擎時略過）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from tmig.data.executor import execute
from tmig.data.live import execute_live
from tmig.data.sources import Executor
from tmig.ir import IR, LiveConnection, TableRelation
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import QueryError, worksheet_sql

from ..engines import (
    as_live,
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
from ..parser.builder import DS, calc, instance, ref, worksheet
from .test_relationship_cross_table import engine

__all__ = ["engine"]  # pytest fixture（Relationship 模型的來源資料庫：DuckDB、PostgreSQL、MySQL）

COLUMNS = [
    ("Customer ID", "string", "Count"),
    ("Segment", "string", "Count"),
    ("Order Date", "date", "Year"),
    ("Sales", "real", "Sum"),
]
ROWS = [
    ("C1", "Consumer", dt.date(2023, 1, 5), 100.0),
    ("C1", "Consumer", dt.date(2023, 2, 10), 50.0),
    ("C2", "Corporate", dt.date(2023, 1, 20), 200.0),
    ("C2", "Corporate", dt.date(2023, 3, 2), 30.0),
    ("C3", "Consumer", dt.date(2023, 2, 14), 80.0),
    ("C4", "Corporate", dt.date(2023, 2, 25), 40.0),
    ("C4", "Corporate", dt.date(2023, 3, 30), 60.0),
    ("C5", "Consumer", dt.date(2023, 3, 8), 10.0),
    ("C6", "Consumer", dt.date(2023, 1, 12), 70.0),
]
JAN, FEB, MAR = dt.date(2023, 1, 1), dt.date(2023, 2, 1), dt.date(2023, 3, 1)

CALCS = "".join(
    [
        calc(
            "Calculation_1",
            "First Order Month",
            "{FIXED [Customer ID] : MIN(DATETRUNC('month', [Order Date]))}",
            "date",
            "dimension",
        ),
        calc(
            "Calculation_2",
            "Cohort Size",
            "{FIXED [First Order Month] : COUNTD([Customer ID])}",
            "integer",
        ),
        calc(
            "Calculation_3",
            "Retention",
            "COUNTD([Customer ID]) / MIN([Cohort Size])",
        ),
        calc(
            "Calculation_4",
            "Cohort Sales",
            "AVG({INCLUDE [First Order Month] : SUM([Sales])})",
        ),
        calc(
            "Calculation_5",
            "Share Of Segment",
            "SUM([Sales]) / SUM({EXCLUDE [First Order Month] : SUM([Sales])})",
        ),
        calc(
            "Calculation_6",
            "Smallest Cohort",
            "{FIXED [Segment] : MIN({FIXED [First Order Month] : COUNTD([Customer ID])})}",
            "integer",
        ),
        calc(
            "Calculation_7",
            "Cohort Quarter Sales",
            "{FIXED DATETRUNC('quarter', [First Order Month]) : SUM([Sales])}",
        ),
        calc(
            "Calculation_8",
            "Cohort Size Dim",
            "{FIXED [First Order Month] : COUNTD([Customer ID])}",
            "integer",
            "dimension",
        ),
        calc(
            "Calculation_9",
            "Big Spender",
            "{FIXED {INCLUDE [Customer ID] : SUM([Sales])} : COUNTD([Customer ID])}",
            "integer",
        ),
        calc(
            "Calculation_10",
            "Fixed Cohort Sales",
            "{FIXED [Segment] : AVG({INCLUDE [First Order Month] : SUM([Sales])})}",
        ),
    ]
)
FIRST_MONTH = instance("[Calculation_1]", "None", "[none:Calculation_1:ok]", "ordinal")
COHORT_SIZE = instance("[Calculation_2]", "Min", "[min:Calculation_2:qk]", "quantitative")
RETENTION = instance("[Calculation_3]", "User", "[usr:Calculation_3:qk]", "quantitative")
COHORT_SALES = instance("[Calculation_4]", "User", "[usr:Calculation_4:qk]", "quantitative")
SHARE = instance("[Calculation_5]", "User", "[usr:Calculation_5:qk]", "quantitative")
SMALLEST = instance("[Calculation_6]", "Min", "[min:Calculation_6:qk]", "quantitative")
QUARTER_SALES = instance("[Calculation_7]", "Min", "[min:Calculation_7:qk]", "quantitative")
SIZE_DIM = instance("[Calculation_8]", "None", "[none:Calculation_8:ok]", "ordinal")
BIG = instance("[Calculation_9]", "Min", "[min:Calculation_9:qk]", "quantitative")
FIXED_SALES = instance("[Calculation_10]", "Min", "[min:Calculation_10:qk]", "quantitative")
SEGMENT = instance("[Segment]", "None", "[none:Segment:nk]")
ORDER_MONTH = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:ok]", "ordinal")
ORDER_DATE = instance("[Order Date]", "None", "[none:Order Date:qk]", "quantitative")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
CUSTOMERS = instance("[Customer ID]", "CountD", "[ctd:Customer ID:qk]", "quantitative")
INSTANCES = [
    FIRST_MONTH,
    COHORT_SIZE,
    RETENTION,
    COHORT_SALES,
    SHARE,
    SMALLEST,
    QUARTER_SALES,
    SIZE_DIM,
    BIG,
    FIXED_SALES,
    SEGMENT,
    ORDER_MONTH,
    ORDER_DATE,
    SALES,
    CUSTOMERS,
]


def datasource() -> str:
    def records(parent: str) -> str:
        return (
            "<metadata-records>"
            + "".join(
                f"""<metadata-record class='column'>
              <remote-name>{name}</remote-name><remote-type>129</remote-type>
              <local-name>[{name}]</local-name><parent-name>{parent}</parent-name>
              <ordinal>{i}</ordinal><local-type>{datatype}</local-type>
              <aggregation>{agg}</aggregation>
            </metadata-record>"""
                for i, (name, datatype, agg) in enumerate(COLUMNS)
            )
            + "</metadata-records>"
        )

    return f"""
    <datasource caption='Orders' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='src' name='textscan.1'>
            <connection class='textscan' directory='C:/data' filename='orders.csv' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.1' name='orders.csv' table='[orders#csv]' type='table' />
        {records("[orders.csv]")}
      </connection>
      <extract count='-1' enabled='true' units='records'>
        <connection class='hyper' dbname='Data/Extracts/test.hyper' schema='Extract' tablename='Extract'>
          <relation name='Extract' table='[Extract].[Extract]' type='table' />
          {records("[Extract]")}
        </connection>
      </extract>
      <aliases enabled='yes' />
      {CALCS}
    </datasource>"""


TABLE = pa.table(
    {
        "Customer ID": [r[0] for r in ROWS],
        "Segment": [r[1] for r in ROWS],
        "Order Date": pa.array([r[2] for r in ROWS], pa.date32()),
        "Sales": [r[3] for r in ROWS],
    }
)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cohort") / "orders.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.register("rows", TABLE)
        con.execute('CREATE TABLE "Extract"."Extract" AS SELECT * FROM "rows"')
    return path


def sheet(rows: str, cols: str = "", encodings: str = "", view_extra: str = "") -> str:
    return worksheet(
        "Cohort", INSTANCES, rows=rows, cols=cols, encodings=encodings, view_extra=view_extra
    )


def parse(tmp_path: Path, xml_sheet: str) -> IR:
    ir = parse_workbook(
        b.write_twb(tmp_path, b.workbook(datasource=datasource(), worksheets=xml_sheet))
    )
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


def run(tmp_path: Path, db: Path, xml_sheet: str) -> list[dict[str, Any]]:
    ir = parse(tmp_path, xml_sheet)
    result = execute(db, worksheet_sql(ir, ir.worksheets[0].id).sql)
    return [{k: v for k, v in r.items() if not k.startswith("__")} for r in result.records()]


def text(*insts: str) -> str:
    return "".join(f"<text column='{ref(i)}' />" for i in insts)


def categorical(inst: str, values: list[str], context: bool = False) -> str:
    members = "".join(
        f"<groupfilter function='member' level='[{inst}]' member='&quot;{v}&quot;' />"
        for v in values
    )
    ctx = " context='true'" if context else ""
    return (
        f"<filter class='categorical' column='{ref(inst)}'{ctx}>"
        f"<groupfilter function='union'>{members}</groupfilter></filter>"
    )


def from_february(context: bool = False) -> str:
    """訂單日期 2023-02-01 起的篩選（不含 1 月的訂單）。"""
    ctx = " context='true'" if context else ""
    return (
        f"<filter class='quantitative' column='{ref('none:Order Date:qk')}'{ctx} "
        "included-values='in-range'><min>#2023-02-01#</min></filter>"
    )


FIRST = "f_first_order_month"


# ---------------------------------------------------------------- FIXED 的維度是 FIXED


def test_cohort_size_by_first_order_month(tmp_path: Path, db: Path) -> None:
    """各世代的顧客數：依第一次下單的月份（FIXED）分組再計數。"""
    rows = run(
        tmp_path, db, sheet(ref("none:Calculation_1:ok"), encodings=text("min:Calculation_2:qk"))
    )
    assert {r[FIRST]: r["min(f_cohort_size)"] for r in rows} == {JAN: 3, FEB: 2, MAR: 1}


def test_retention_grid(tmp_path: Path, db: Path) -> None:
    """世代 × 訂單月份：當月仍下單的顧客 ÷ 世代的顧客數。"""
    rows = run(
        tmp_path,
        db,
        sheet(
            ref("none:Calculation_1:ok"),
            ref("tmn:Order Date:ok"),
            text("usr:Calculation_3:qk"),
        ),
    )
    got = {(r[FIRST], r["trunc_month(f_order_date)"]): r["f_retention"] for r in rows}
    assert got == {
        (JAN, JAN): 1.0,
        (JAN, FEB): pytest.approx(1 / 3),
        (JAN, MAR): pytest.approx(1 / 3),
        (FEB, FEB): 1.0,
        (FEB, MAR): 0.5,
        (MAR, MAR): 1.0,
    }


def test_dimension_filter_does_not_change_the_cohorts(tmp_path: Path, db: Path) -> None:
    """維度篩選（只留 Consumer）在兩層 FIXED 之後：世代與世代的顧客數不變（1 月仍是 3 人），檢視的計數只算
    Consumer（1 月 C1、C6）。"""
    rows = run(
        tmp_path,
        db,
        sheet(
            ref("none:Calculation_1:ok"),
            encodings=text("min:Calculation_2:qk", "ctd:Customer ID:qk"),
            view_extra=categorical("none:Segment:nk", ["Consumer"]),
        ),
    )
    got = {r[FIRST]: (r["min(f_cohort_size)"], r["countd(f_customer_id)"]) for r in rows}
    assert got == {JAN: (3, 2), FEB: (2, 1), MAR: (1, 1)}
    # 訂單日期的維度篩選也一樣：第一次下單的月份仍以全部的訂單計算（C1、C2、C6 仍屬 1 月的世代）
    rows = run(
        tmp_path,
        db,
        sheet(
            ref("none:Calculation_1:ok"),
            encodings=text("min:Calculation_2:qk"),
            view_extra=from_february(),
        ),
    )
    assert {r[FIRST]: r["min(f_cohort_size)"] for r in rows} == {JAN: 3, FEB: 2, MAR: 1}


def test_context_filter_applies_to_both_levels(tmp_path: Path, db: Path) -> None:
    """內容篩選在兩層 FIXED 之前：只留 Consumer 時世代的顧客數是 2、1、1；只留 2 月起的訂單時第一次下單的
    月份也重算（C1 改為 2 月、C2 改為 3 月，只在 1 月下單的 C6 不見了）。"""
    rows = run(
        tmp_path,
        db,
        sheet(
            ref("none:Calculation_1:ok"),
            encodings=text("min:Calculation_2:qk"),
            view_extra=categorical("none:Segment:nk", ["Consumer"], context=True),
        ),
    )
    assert {r[FIRST]: r["min(f_cohort_size)"] for r in rows} == {JAN: 2, FEB: 1, MAR: 1}
    rows = run(
        tmp_path,
        db,
        sheet(
            ref("none:Calculation_1:ok"),
            encodings=text("min:Calculation_2:qk"),
            view_extra=from_february(context=True),
        ),
    )
    # 2 月：C1、C3、C4；3 月：C2、C5
    assert {r[FIRST]: r["min(f_cohort_size)"] for r in rows} == {FEB: 3, MAR: 2}


def test_lod_with_lod_dimension_as_view_dimension(tmp_path: Path, db: Path) -> None:
    """世代的顧客數本身當維度：3 人的世代（C1、C2、C6）銷售 450、2 人（C3、C4）180、1 人（C5）10。"""
    rows = run(tmp_path, db, sheet(ref("none:Calculation_8:ok"), ref("sum:Sales:qk")))
    assert {r["f_cohort_size_dim"]: r["sum(f_sales)"] for r in rows} == {3: 450, 2: 180, 1: 10}
    # 以它篩選（維度篩選；值在 FIXED 時已算好）
    flt = (
        f"<filter class='categorical' column='{ref('none:Calculation_8:ok')}'>"
        "<groupfilter function='member' level='[none:Calculation_8:ok]' member='2' /></filter>"
    )
    rows = run(tmp_path, db, sheet(ref("none:Segment:nk"), ref("sum:Sales:qk"), view_extra=flt))
    assert {r["f_segment"]: r["sum(f_sales)"] for r in rows} == {"Consumer": 80, "Corporate": 100}


def test_expression_over_lod_dimension(tmp_path: Path, db: Path) -> None:
    """維度是含 LOD 的運算式：DATETRUNC('quarter', [First Order Month])——三個世代都在第一季，合計 640。"""
    rows = run(tmp_path, db, sheet(ref("none:Segment:nk"), encodings=text("min:Calculation_7:qk")))
    assert {r["f_segment"]: r["min(f_cohort_quarter_sales)"] for r in rows} == {
        "Consumer": 640,
        "Corporate": 640,
    }


def test_fixed_with_lod_dimension_nested_in_fixed(tmp_path: Path, db: Path) -> None:
    """外層 FIXED [Segment] 的運算式中的 FIXED，維度是 FIXED：每個區隔中最小的世代（Consumer 有 3 月的 C5：
    1；Corporate 的 C2、C4 在 1、2 月：2）。"""
    rows = run(tmp_path, db, sheet(ref("none:Segment:nk"), encodings=text("min:Calculation_6:qk")))
    assert {r["f_segment"]: r["min(f_smallest_cohort)"] for r in rows} == {
        "Consumer": 1,
        "Corporate": 2,
    }


# ---------------------------------------------------------------- INCLUDE／EXCLUDE 的維度是 FIXED


def test_include_by_first_order_month(tmp_path: Path, db: Path) -> None:
    """AVG({INCLUDE [First Order Month] : SUM([Sales])})：各區隔中每個世代的銷售再平均（Consumer：1 月
    C1 + C6 = 220、2 月 80、3 月 10 → 103.33；Corporate：1 月 230、2 月 100 → 165）。INCLUDE 在維度篩選後
    計算，世代仍以全部的訂單決定：2 月起的訂單中 Consumer 的 1 月世代只剩 C1 的 50。"""
    rows = run(tmp_path, db, sheet(ref("none:Segment:nk"), encodings=text("usr:Calculation_4:qk")))
    assert {r["f_segment"]: r["f_cohort_sales"] for r in rows} == {
        "Consumer": pytest.approx(310 / 3),
        "Corporate": 165,
    }
    rows = run(
        tmp_path,
        db,
        sheet(
            ref("none:Segment:nk"),
            encodings=text("usr:Calculation_4:qk"),
            view_extra=from_february(),
        ),
    )
    # Consumer：1 月 50、2 月 80、3 月 10；Corporate：1 月 30、2 月 100
    assert {r["f_segment"]: r["f_cohort_sales"] for r in rows} == {
        "Consumer": pytest.approx(140 / 3),
        "Corporate": 65,
    }


def test_include_with_lod_dimension_inside_fixed(tmp_path: Path, db: Path) -> None:
    """外層 FIXED [Segment] 之內的 INCLUDE [First Order Month]：內層相對於外層的 [Segment]（不是檢視），
    與檢視層級的 Cohort Sales 相同是 103.33、165，但整段在維度篩選前計算——2 月起的訂單的維度篩選不改變它。"""
    expected = {"Consumer": pytest.approx(310 / 3), "Corporate": 165}
    for extra in ("", from_february()):
        rows = run(
            tmp_path,
            db,
            sheet(
                ref("none:Segment:nk"), encodings=text("min:Calculation_10:qk"), view_extra=extra
            ),
        )
        assert {r["f_segment"]: r["min(f_fixed_cohort_sales)"] for r in rows} == expected


def test_sort_and_top_n_by_include_over_cohorts(tmp_path: Path, db: Path) -> None:
    """依欄位排序與 Top N 的依據是 INCLUDE [First Order Month]：相對於排序（篩選）的維度另外計算，世代仍是
    先算好的 FIXED（Corporate 165 大於 Consumer 103.33）。"""
    by = ref("usr:Calculation_4:qk")
    sort = f"<computed-sort column='{ref('none:Segment:nk')}' direction='DESC' using='{by}' />"
    rows = run(
        tmp_path,
        db,
        sheet(
            f"{ref('none:Segment:nk')} / {ref('none:Calculation_1:ok')}",
            ref("sum:Sales:qk"),
            view_extra=sort,
        ),
    )
    assert list(dict.fromkeys(r["f_segment"] for r in rows)) == ["Corporate", "Consumer"]
    top = (
        f"<filter class='categorical' column='{ref('none:Segment:nk')}'>"
        "<groupfilter count='1' end='top' function='end' units='records'>"
        "<groupfilter direction='DESC' expression='[Calculation_4]' function='order'>"
        "<groupfilter function='level-members' level='[none:Segment:nk]' /></groupfilter>"
        "</groupfilter></filter>"
    )
    rows = run(tmp_path, db, sheet(ref("none:Segment:nk"), ref("sum:Sales:qk"), view_extra=top))
    assert {r["f_segment"]: r["sum(f_sales)"] for r in rows} == {"Corporate": 330}


def test_exclude_lod_dimension_of_the_view(tmp_path: Path, db: Path) -> None:
    """EXCLUDE [First Order Month]：檢視是區隔 × 世代，去掉世代即各區隔的合計（Consumer 310、Corporate 330）。"""
    rows = run(
        tmp_path,
        db,
        sheet(
            f"{ref('none:Segment:nk')} / {ref('none:Calculation_1:ok')}",
            encodings=text("usr:Calculation_5:qk"),
        ),
    )
    got = {(r["f_segment"], r[FIRST]): r["f_share_of_segment"] for r in rows}
    assert got == {
        ("Consumer", JAN): pytest.approx(220 / 310),
        ("Consumer", FEB): pytest.approx(80 / 310),
        ("Consumer", MAR): pytest.approx(10 / 310),
        ("Corporate", JAN): pytest.approx(230 / 330),
        ("Corporate", FEB): pytest.approx(100 / 330),
    }


# ---------------------------------------------------------------- 查詢的結構與不支援的寫法


def test_dimension_lod_is_attached_first(tmp_path: Path) -> None:
    """維度中的 FIXED 先併回每一列（ctx_ext），外層再依它分組（ctx_ext_1），都在第 3 段（維度篩選前）。"""
    ir = parse(
        tmp_path, sheet(ref("none:Calculation_1:ok"), encodings=text("min:Calculation_2:qk"))
    )
    query = worksheet_sql(ir, ir.worksheets[0].id)
    names = [c.name for c in query.ctes]
    assert names.index("ctx_ext") < names.index("ctx_ext_1")
    assert all(c.stage == 3 for c in query.ctes if c.name.startswith(("lod", "ctx_ext")))


def test_include_in_lod_dimension_is_unsupported(tmp_path: Path) -> None:
    """維度中的 INCLUDE／EXCLUDE：Tableau 說明它們的結果是量值，相對於哪些維度沒有文件，列為不支援；訊息
    以畫面上的名稱寫出運算式。"""
    ir = parse(tmp_path, sheet(ref("none:Segment:nk"), encodings=text("min:Calculation_9:qk")))
    with pytest.raises(QueryError) as info:
        worksheet_sql(ir, ir.worksheets[0].id)
    assert info.value.unsupported
    message = str(info.value)
    assert "LOD 的維度中有 INCLUDE 的 LOD 運算式" in message
    assert "[Customer ID]" in message and "Calculation_" not in message


# ---------------------------------------------------------------- Relationship 模型與 Data Blending

MODEL_CALCS = b.calc(
    "Calculation_21",
    "Cust Orders",
    "{FIXED [Cust ID] : COUNTD([Order ID])}",
    "integer",
    "dimension",
) + b.calc("Calculation_22", "Sales By Order Count", "{FIXED [Cust Orders] : SUM([Sales])}")


def test_relationship_model(tmp_path: Path, engine: Any) -> None:
    """Relationship 模型（test_relationship_cross_table 的資料）：各顧客的訂單數（FIXED [Cust ID]：C1 兩張，
    C2、C3、C9 與 Cust ID 為 Null 的 O6 各一張），再依訂單數合計銷售（2 張：150；1 張：70 + 30 + 10 + 20 = 130）。
    以 CUSTOMERS 的 [Name] 為檢視的維度時，每張訂單的值依關聯到 Name 再取最小（沒有訂單的 Dee 為 Null；
    沒有對應顧客的 O5、O6 在 Null）。"""
    from .test_relationship_cross_table import datasource as model_datasource
    from .test_relationship_cross_table import dim, measure, rows_of, view

    xml = model_datasource().replace(
        "<aliases enabled='yes' />", "<aliases enabled='yes' />" + MODEL_CALCS, 1
    )
    base = worksheet(
        "Base", [instance("[Cust ID]", "None", "[none:Cust ID:nk]")], rows=ref("none:Cust ID:nk")
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(datasource=xml, worksheets=base)))
    assert ir.unsupported == []
    by_count = view(
        ir,
        [dim(ir, "Cust Orders")],
        [measure(ir, "Sales"), measure(ir, "Sales By Order Count", "min")],
    )
    result = execute_live(worksheet_sql(by_count, "Base"), engine.executor)
    assert rows_of(result) == {2: (150, 150), 1: (130, 130)}
    by_name = view(ir, [dim(ir, "Name")], [measure(ir, "Sales By Order Count", "min")])
    result = execute_live(worksheet_sql(by_name, "Base"), engine.executor)
    assert rows_of(result) == {"Ann": 150, "Bob": 130, "Cid": 130, "Dee": None, None: 130}


@pytest.fixture(scope="module")
def blend_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("blend_lod_dimensions")


def test_data_blending(tmp_path: Path, blend_folder: Path) -> None:
    """Data Blending（test_blend 的訂單與目標）：主要資料源的 {FIXED [Region Category] : COUNTD([Region])}
    在主要資料源的查詢中計算（各區第一個類別：East、West、South 為 Furniture，North 為 Office → 3、3、3、1）；
    次要資料源的 {FIXED [Target Start] : SUM([Target])} 在次要資料源的查詢中計算（各區最早的目標日期：East、
    West 為 2023-01-01，合計 400 + 900 = 1300；South、Central 為 2024-01-01，50 + 999 = 1049），依連結的 Region
    併入（North 沒有目標：Null）。"""
    from tmig.data.executor import execute_query

    from .test_blend import blended, fields, target_ref
    from .test_blend import parse as parse_blend
    from .test_query import REGION
    from .test_query import SALES as ORDER_SALES

    primary = b.calc(
        "Calculation_31",
        "Region Category",
        "{FIXED [Region] : MIN([Category])}",
        "string",
        "dimension",
    ) + b.calc("Calculation_32", "Regions In Group", "{FIXED [Region Category] : COUNTD([Region])}")
    secondary = b.calc(
        "Calculation_t1",
        "Target Start",
        "{FIXED [Region] : MIN([Order Date])}",
        "date",
        "dimension",
    ) + b.calc("Calculation_t2", "Target Cohort", "{FIXED [Target Start] : SUM([Target])}")
    in_group = instance("[Calculation_32]", "Min", "[min:Calculation_32:qk]", "quantitative")
    cohort = instance("[Calculation_t2]", "Min", "[min:Calculation_t2:qk]", "quantitative")
    encodings = (
        f"<text column='{ref('min:Calculation_32:qk')}' />"
        f"<text column='{target_ref('min:Calculation_t2:qk')}' />"
    )
    xml_sheet = blended(
        ref("none:Region:nk"),
        instances=[REGION, ORDER_SALES, in_group],
        secondary=[cohort],
        encodings=encodings,
    )
    ir = parse_blend(
        tmp_path, blend_folder, xml_sheet, columns=primary, secondary_columns=secondary
    )
    assert ir.unsupported == []
    ids = fields(ir)
    ws = ir.worksheets[0]
    found = execute_query(
        blend_folder / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id)
    ).records()
    got = {
        r[ids["Region"]]: (
            r[f"min({ids['Regions In Group']})"],
            r[f"min({ids['Targets.Target Cohort']})"],
        )
        for r in found
    }
    assert got == {
        "East": (3, 1300),
        "West": (3, 1300),
        "South": (3, 1049),
        "North": (1, None),
    }


# ---------------------------------------------------------------- 來源資料庫（Live 連線）


@pytest.fixture(scope="module", params=["postgres", "mysql"])
def source(request: pytest.FixtureRequest) -> Iterator[tuple[str, Executor]]:
    if request.param == "postgres":
        with postgres_cluster() as dsn:
            con = pg_connect(dsn)
            pg_load(con, "orders", TABLE)
            try:
                yield "postgres", postgres_executor(con)
            finally:
                con.close()
        return
    with mysql_server() as params:
        con = mysql_connect(params)
        mysql_load(con, "orders", TABLE)
        try:
            yield "mysql", mysql_executor(con)
        finally:
            con.close()


def live(ir: IR, dialect: str) -> IR:
    """萃取改成 Live 連線，連到來源資料庫的 orders 資料表。"""
    converted = as_live(ir, dialect)
    ds = converted.datasources[0]
    connection = ds.connection
    assert isinstance(connection, LiveConnection) and isinstance(connection.relation, TableRelation)
    relation = connection.relation.model_copy(update={"table": ["orders"]})
    connection = connection.model_copy(update={"relation": relation})
    return converted.model_copy(
        update={"datasources": [ds.model_copy(update={"connection": connection})]}
    )


@pytest.mark.parametrize(
    "dialect", ["postgres", "mysql", "tsql", "oracle", "snowflake", "bigquery"]
)
def test_cohorts_translate(tmp_path: Path, dialect: str) -> None:
    """兩層 FIXED 整段下推：各方言都能轉譯（沒有引擎的環境也檢查得到）。"""
    from tmig.sqlgen.dialect import split, to_dialect

    ir = parse(
        tmp_path,
        sheet(ref("none:Calculation_1:ok"), ref("tmn:Order Date:ok"), text("usr:Calculation_3:qk")),
    )
    query = worksheet_sql(live(ir, dialect), ir.worksheets[0].id)
    fetches = split(query).fetches
    assert fetches
    for fetch in fetches:
        assert to_dialect(fetch.sql, dialect)


def test_cohorts_on_source_databases(tmp_path: Path, source: tuple[str, Executor]) -> None:
    """世代的顧客數與留存率在來源資料庫計算（兩層 FIXED 都下推）。"""
    dialect, executor = source
    ir = parse(
        tmp_path,
        sheet(
            ref("none:Calculation_1:ok"),
            ref("tmn:Order Date:ok"),
            text("usr:Calculation_3:qk", "min:Calculation_2:qk"),
            categorical("none:Segment:nk", ["Consumer"], context=True),
        ),
    )
    query = worksheet_sql(live(ir, dialect), ir.worksheets[0].id)
    rows = execute_live(query, executor).records()

    def day(value: Any) -> dt.date:
        return value.date() if isinstance(value, dt.datetime) else value

    got = {
        (day(r[FIRST]), day(r["trunc_month(f_order_date)"])): (
            float(r["f_retention"]),
            int(r["min(f_cohort_size)"]),
        )
        for r in rows
    }
    # Consumer 的世代：1 月 C1、C6（2 月只有 C1 再下單）、2 月 C3、3 月 C5
    assert got == {
        (JAN, JAN): (1.0, 2),
        (JAN, FEB): (0.5, 2),
        (FEB, FEB): (1.0, 1),
        (MAR, MAR): (1.0, 1),
    }
