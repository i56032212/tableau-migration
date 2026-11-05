"""工作表查詢（D1–D4）：以手算結果驗證 Tableau 的運算順序。

測試資料（9 列）：

| Region | Category   | Order ID | Order Date | Sales | Profit |
|--------|------------|----------|------------|-------|--------|
| East   | Furniture  | O1       | 2023-01-15 | 100   | 10     |
| East   | Furniture  | O1       | 2023-01-15 | 50    | -5     |
| East   | Technology | O2       | 2023-02-10 | 200   | 40     |
| West   | Furniture  | O3       | 2023-03-05 | 300   | 30     |
| West   | Technology | O4       | 2024-01-20 | 400   | -20    |
| West   | Technology | O4       | 2024-01-20 | 100   | 10     |
| South  | Furniture  | O5       | 2024-02-14 | 80    | 8      |
| South  | Office     | O6       | 2024-03-01 | 20    | 2      |
| North  | Office     | O7       | 2024-03-15 | 60    | 6      |

各區銷售：East 350、West 800、South 100、North 60，合計 1310。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import FilterValue, QueryState
from tmig.sqlgen.query import QueryError, worksheet_sql

from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet

ROWS = [
    ("East", "Furniture", "O1", dt.date(2023, 1, 15), 100.0, 10.0, 1),
    ("East", "Furniture", "O1", dt.date(2023, 1, 15), 50.0, -5.0, 2),
    ("East", "Technology", "O2", dt.date(2023, 2, 10), 200.0, 40.0, 1),
    ("West", "Furniture", "O3", dt.date(2023, 3, 5), 300.0, 30.0, 3),
    ("West", "Technology", "O4", dt.date(2024, 1, 20), 400.0, -20.0, 4),
    ("West", "Technology", "O4", dt.date(2024, 1, 20), 100.0, 10.0, 1),
    ("South", "Furniture", "O5", dt.date(2024, 2, 14), 80.0, 8.0, 2),
    ("South", "Office", "O6", dt.date(2024, 3, 1), 20.0, 2.0, 1),
    ("North", "Office", "O7", dt.date(2024, 3, 15), 60.0, 6.0, 1),
]

REGION = instance("[Region]", "None", "[none:Region:nk]")
CATEGORY = instance("[Category]", "None", "[none:Category:nk]")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
YEAR = instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal")
MONTH = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")
DATE = instance("[Order Date]", "None", "[none:Order Date:qk]", "quantitative")


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "orders.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Category" VARCHAR, "Order ID" VARCHAR, '
            '"Order Date" DATE, "Sales" DOUBLE, "Profit" DOUBLE, "Quantity" BIGINT)'
        )
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', ROWS)
    return path


class Run:
    def __init__(self, tmp_path: Path, db: Path) -> None:
        self.tmp_path = tmp_path
        self.db = db

    def __call__(
        self,
        sheet: str,
        calcs: str = "",
        ds_extra: str = "",
        state: QueryState | None = None,
        extra: str = "",
    ) -> list[dict[str, Any]]:
        xml = b.workbook(
            b.extract_datasource(columns=calcs, extra=ds_extra), worksheets=sheet, extra=extra
        )
        ir = parse_workbook(b.write_twb(self.tmp_path, xml))
        assert ir.unsupported == [], [u.reason for u in ir.unsupported]
        query = worksheet_sql(ir, ir.worksheets[0].id, state)
        result = execute(self.db, query.sql)
        return [
            {k: v for k, v in row.items() if not k.startswith("__")} for row in result.records()
        ]


@pytest.fixture
def run(tmp_path: Path, db: Path) -> Run:
    return Run(tmp_path, db)


def by_region(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {r["f_region"]: r[key] for r in rows}


def approx(value: dict[str, Any]) -> dict[str, Any]:
    return {k: pytest.approx(v) if isinstance(v, float) else v for k, v in value.items()}


def region_sheet(extra: list[str] | None = None, **kwargs: str) -> str:
    defaults = {"rows": ref("none:Region:nk"), "cols": ref("sum:Sales:qk")}
    defaults.update(kwargs)
    return worksheet("S", [REGION, CATEGORY, SALES, YEAR, MONTH, DATE, *(extra or [])], **defaults)


def categorical(inst: str, values: list[str], mode: str = "include", context: bool = False) -> str:
    members = "".join(
        f"<groupfilter function='member' level='[{inst}]' member='&quot;{v}&quot;' />"
        for v in values
    )
    ctx = " context='true'" if context else ""
    if mode == "exclude":
        body = (
            f"<groupfilter function='except'><groupfilter function='level-members' level='[{inst}]' />"
            f"{members}</groupfilter>"
        )
    else:
        body = f"<groupfilter function='union'>{members}</groupfilter>"
    return f"<filter class='categorical' column='{ref(inst)}'{ctx}>{body}</filter>"


def top_n(
    inst: str, count: str, end: str = "top", by: str = "SUM([Sales])", context: bool = False
) -> str:
    ctx = " context='true'" if context else ""
    return (
        f"<filter class='categorical' column='{ref(inst)}'{ctx}>"
        f"<groupfilter count='{count}' end='{end}' function='end' units='records'>"
        f"<groupfilter direction='DESC' expression='{by}' function='order'>"
        f"<groupfilter function='level-members' level='[{inst}]' /></groupfilter></groupfilter></filter>"
    )


# ---------------------------------------------------------------- D1 維度、彙總、維度篩選、排序


def test_sum_by_region(run: Run) -> None:
    rows = run(region_sheet())
    assert by_region(rows, "sum(f_sales)") == {"East": 350, "North": 60, "South": 100, "West": 800}


def test_dimension_filters(run: Run) -> None:
    include = run(
        region_sheet(view_extra=categorical("none:Category:nk", ["Furniture", "Technology"]))
    )
    assert by_region(include, "sum(f_sales)") == {"East": 350, "South": 80, "West": 800}
    exclude = run(region_sheet(view_extra=categorical("none:Category:nk", ["Office"], "exclude")))
    assert by_region(exclude, "sum(f_sales)") == by_region(include, "sum(f_sales)")


def test_date_dimensions_and_range_filter(run: Run) -> None:
    rows = run(region_sheet(rows=ref("yr:Order Date:ok")))
    assert {r["year(f_order_date)"]: r["sum(f_sales)"] for r in rows} == {2023: 650, 2024: 660}
    flt = (
        f"<filter class='quantitative' column='{ref('none:Order Date:qk')}' included-values='in-range'>"
        "<min>#2023-02-01#</min><max>#2024-01-31#</max></filter>"
    )
    rows = run(region_sheet(rows=ref("tmn:Order Date:qk"), view_extra=flt))
    assert {r["trunc_month(f_order_date)"]: r["sum(f_sales)"] for r in rows} == {
        dt.date(2023, 2, 1): 200,
        dt.date(2023, 3, 1): 300,
        dt.date(2024, 1, 1): 500,
    }


def test_aggregations_and_aggregate_calc(run: Run) -> None:
    calcs = calc("Calculation_1", "Ratio", "SUM([Profit]) / SUM([Sales])")
    extra = [
        instance("[Calculation_1]", "User", "[usr:Calculation_1:qk]", "quantitative"),
        instance("[Order ID]", "CountD", "[ctd:Order ID:qk]", "quantitative"),
        instance("[Sales]", "Avg", "[avg:Sales:qk]", "quantitative"),
    ]
    encodings = (
        f"<text column='{ref('usr:Calculation_1:qk')}' /><tooltip column='{ref('ctd:Order ID:qk')}' />"
        f"<tooltip column='{ref('avg:Sales:qk')}' />"
    )
    rows = run(region_sheet(extra, encodings=encodings), calcs)
    assert by_region(rows, "f_ratio") == approx(
        {"East": 45 / 350, "North": 0.1, "South": 0.1, "West": 0.025}
    )
    assert by_region(rows, "countd(f_order_id)") == {"East": 2, "North": 1, "South": 2, "West": 2}
    assert by_region(rows, "avg(f_sales)") == approx(
        {"East": 350 / 3, "North": 60, "South": 50, "West": 800 / 3}
    )


def test_datasource_filter(run: Run) -> None:
    extra = (
        "<column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />"
        "<filter class='categorical' column='[none:Region:nk]'><groupfilter function='except'>"
        "<groupfilter function='level-members' level='[none:Region:nk]' />"
        "<groupfilter function='member' level='[none:Region:nk]' member='&quot;North&quot;' /></groupfilter></filter>"
    )
    rows = run(region_sheet(), ds_extra=extra)
    assert set(by_region(rows, "sum(f_sales)")) == {"East", "South", "West"}


def test_sort_by_measure_orders_output(run: Run) -> None:
    sort = f"<computed-sort column='{ref('none:Region:nk')}' direction='DESC' using='{ref('sum:Sales:qk')}' />"
    rows = run(region_sheet(view_extra=sort))
    assert [r["f_region"] for r in rows] == ["West", "East", "South", "North"]


def test_state_overrides_and_adds_filters(run: Run) -> None:
    sheet = region_sheet(view_extra=categorical("none:Category:nk", ["Furniture"]))
    state = QueryState(
        filters={
            "f_category": FilterValue(mode="include", values=["Office"]),
            "year(f_order_date)": FilterValue(values=[2024]),
        }
    )
    rows = run(sheet, state=state)
    assert by_region(rows, "sum(f_sales)") == {"North": 60, "South": 20}


# ---------------------------------------------------------------- D2 內容篩選、量值篩選、Top N


def test_top_n_ignores_dimension_filters(run: Run) -> None:
    filters = top_n("none:Region:nk", "2") + categorical("none:Category:nk", ["Furniture"])
    rows = run(region_sheet(view_extra=filters))
    # 前 2 名（West、East）以全部資料排名，再套用維度篩選
    assert by_region(rows, "sum(f_sales)") == {"East": 150, "West": 300}


def test_context_filter_applies_before_top_n(run: Run) -> None:
    dimension = top_n("none:Region:nk", "2") + categorical("none:Category:nk", ["Office"])
    assert run(region_sheet(view_extra=dimension)) == []
    context = top_n("none:Region:nk", "2") + categorical(
        "none:Category:nk", ["Office"], context=True
    )
    rows = run(region_sheet(view_extra=context))
    assert by_region(rows, "sum(f_sales)") == {"North": 60, "South": 20}


def test_bottom_n_and_parameter_count(run: Run) -> None:
    rows = run(region_sheet(view_extra=top_n("none:Region:nk", "1", end="bottom")))
    assert by_region(rows, "sum(f_sales)") == {"North": 60}
    rows = run(region_sheet(view_extra=top_n("none:Region:nk", "[Parameters].[Parameter 1]")))
    assert len(rows) == 4  # Top N 參數預設為 5，超過成員數時全部保留
    state = QueryState(parameters={"p_top_n": 1})
    rows = run(
        region_sheet(view_extra=top_n("none:Region:nk", "[Parameters].[Parameter 1]")), state=state
    )
    assert by_region(rows, "sum(f_sales)") == {"West": 800}


def test_measure_filter(run: Run) -> None:
    flt = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>300</min></filter>"
    )
    rows = run(region_sheet(view_extra=flt))
    assert by_region(rows, "sum(f_sales)") == {"East": 350, "West": 800}


# ---------------------------------------------------------------- D3 FIXED、INCLUDE、EXCLUDE

LOD_CALCS = "".join(
    [
        calc("Calculation_10", "Region Sales", "{FIXED [Region] : SUM([Sales])}"),
        calc("Calculation_11", "Share", "SUM([Sales]) / SUM([Calculation_10])"),
        calc("Calculation_12", "Order Avg", "AVG({INCLUDE [Order ID] : SUM([Sales])})"),
        calc("Calculation_13", "Excl", "SUM({EXCLUDE [Category] : SUM([Sales])})"),
        calc("Calculation_14", "Total", "SUM([Sales]) / SUM({SUM([Sales])})"),
        calc("Calculation_15", "Above Min", "SUM([Sales] - {FIXED [Region] : MIN([Sales])})"),
    ]
)
LOD_INSTANCES = [
    instance("[Calculation_10]", "Sum", "[sum:Calculation_10:qk]", "quantitative"),
    *[
        instance(f"[Calculation_{n}]", "User", f"[usr:Calculation_{n}:qk]", "quantitative")
        for n in (11, 12, 13, 14, 15)
    ],
]


def lod_sheet(rows: str, encodings: str, view_extra: str = "") -> str:
    return region_sheet(LOD_INSTANCES, rows=rows, encodings=encodings, view_extra=view_extra)


def test_fixed_ignores_dimension_filter_but_respects_context(run: Run) -> None:
    enc = f"<text column='{ref('sum:Calculation_10:qk')}' />"
    rows = run(
        lod_sheet(ref("none:Region:nk"), enc, categorical("none:Category:nk", ["Furniture"])),
        LOD_CALCS,
    )
    assert by_region(rows, "sum(f_region_sales)") == {"East": 350, "South": 100, "West": 800}
    rows = run(
        lod_sheet(
            ref("none:Region:nk"), enc, categorical("none:Category:nk", ["Furniture"], context=True)
        ),
        LOD_CALCS,
    )
    assert by_region(rows, "sum(f_region_sales)") == {"East": 150, "South": 80, "West": 300}


def test_fixed_coarser_than_view_is_replicated(run: Run) -> None:
    enc = f"<text column='{ref('sum:Calculation_10:qk')}' /><tooltip column='{ref('usr:Calculation_11:qk')}' />"
    rows = run(lod_sheet(f"{ref('none:Region:nk')} / {ref('none:Category:nk')}", enc), LOD_CALCS)
    result = {
        (r["f_region"], r["f_category"]): (r["sum(f_region_sales)"], r["f_share"]) for r in rows
    }
    assert result[("East", "Furniture")] == (350, pytest.approx(150 / 350))
    assert result[("West", "Technology")] == (800, pytest.approx(500 / 800))
    assert result[("North", "Office")] == (60, pytest.approx(1.0))


def test_include_averages_over_orders(run: Run) -> None:
    enc = f"<text column='{ref('usr:Calculation_12:qk')}' />"
    rows = run(lod_sheet(ref("none:Region:nk"), enc), LOD_CALCS)
    assert by_region(rows, "f_order_avg") == {"East": 175, "North": 60, "South": 50, "West": 400}


def test_exclude_respects_dimension_filters(run: Run) -> None:
    enc = f"<text column='{ref('usr:Calculation_13:qk')}' />"
    rows = run(
        lod_sheet(
            f"{ref('none:Region:nk')} / {ref('none:Category:nk')}",
            enc,
            categorical("none:Category:nk", ["Furniture", "Technology"]),
        ),
        LOD_CALCS,
    )
    result = {(r["f_region"], r["f_category"]): r["f_excl"] for r in rows}
    assert result == {
        ("East", "Furniture"): 350,
        ("East", "Technology"): 350,
        ("South", "Furniture"): 80,
        ("West", "Furniture"): 800,
        ("West", "Technology"): 800,
    }


def test_fixed_without_dims_and_row_level_mix(run: Run) -> None:
    enc = f"<text column='{ref('usr:Calculation_14:qk')}' /><tooltip column='{ref('usr:Calculation_15:qk')}' />"
    rows = run(lod_sheet(ref("none:Region:nk"), enc), LOD_CALCS)
    assert by_region(rows, "f_total") == approx(
        {"East": 350 / 1310, "North": 60 / 1310, "South": 100 / 1310, "West": 800 / 1310}
    )
    # 含列層級欄位時逐列計算：每列減去該區最小值後加總
    assert by_region(rows, "f_above_min") == {"East": 200, "North": 0, "South": 60, "West": 500}


def test_relative_lod_outside_the_view_is_unsupported(tmp_path: Path) -> None:
    """INCLUDE／EXCLUDE 相對於檢視的維度。依欄位排序、Top N 的排名依據、彙總的條件篩選用到它時，
    Tableau 在各自的層級計算（公開範例 Logshark 的 Apache），尚不支援：整張工作表列為不支援，
    不默默以檢視的層級計算。"""
    order_avg = ref("usr:Calculation_12:qk")
    sort = (
        f"<computed-sort column='{ref('none:Region:nk')}' direction='DESC' using='{order_avg}' />"
    )
    top = top_n("none:Region:nk", "2", by="[Calculation_13]")
    condition = (
        f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
        "<groupfilter expression='[Calculation_13] &gt; 100' function='filter'>"
        "<groupfilter function='level-members' level='[none:Region:nk]' /></groupfilter></filter>"
    )
    for view_extra, message in (
        (sort, "INCLUDE／EXCLUDE LOD 只能用在檢視的量值"),
        (top, "Top N 的排名依據含 INCLUDE／EXCLUDE LOD，尚不支援"),
        (condition, "條件篩選含 INCLUDE／EXCLUDE LOD，尚不支援"),
    ):
        sheet = lod_sheet(ref("none:Region:nk"), f"<text column='{order_avg}' />", view_extra)
        xml = b.workbook(b.extract_datasource(columns=LOD_CALCS), worksheets=sheet)
        ir = parse_workbook(b.write_twb(tmp_path, xml))
        assert ir.unsupported == []
        with pytest.raises(QueryError, match=message) as info:
            worksheet_sql(ir, ir.worksheets[0].id)
        assert info.value.unsupported


# ---------------------------------------------------------------- D4 Table Calculation

TC_CALCS = "".join(
    [
        calc("Calculation_20", "Rank", "RANK(SUM([Sales]))", "integer"),
        calc("Calculation_21", "Index", "INDEX()", "integer"),
        calc("Calculation_22", "First", "FIRST()", "integer"),
        calc("Calculation_23", "Last", "LAST()", "integer"),
        calc("Calculation_24", "Size", "SIZE()", "integer"),
        calc("Calculation_25", "Win", "WINDOW_SUM(SUM([Sales]), -1, 1)"),
        calc("Calculation_26", "Prev", "LOOKUP(SUM([Sales]), -1)"),
        calc("Calculation_27", "Avg Total", "TOTAL(AVG([Sales]))"),
        calc("Calculation_28", "Run Avg", "RUNNING_AVG(SUM([Sales]))"),
        calc("Calculation_29", "Dense", "RANK_DENSE(COUNTD([Order ID]))", "integer"),
        calc("Calculation_30", "Modified", "RANK_MODIFIED(COUNTD([Order ID]))", "integer"),
        calc("Calculation_31", "Unique", "RANK_UNIQUE(COUNTD([Order ID]))", "integer"),
        calc("Calculation_32", "Competition", "RANK(COUNTD([Order ID]))", "integer"),
        calc("Calculation_33", "Window Avg", "WINDOW_AVG(SUM([Sales]))"),
        calc("Calculation_34", "Window Median", "WINDOW_MEDIAN(SUM([Sales]))"),
        calc("Calculation_35", "Window Stdev", "WINDOW_STDEV(SUM([Sales]), FIRST(), LAST())"),
        calc("Calculation_36", "Window Varp", "WINDOW_VARP(SUM([Sales]))"),
        calc("Calculation_37", "Window Pct", "WINDOW_PERCENTILE(SUM([Sales]), 0.25, -1, 1)"),
        calc("Calculation_38", "Window Corr", "WINDOW_CORR(SUM([Sales]), SUM([Profit]))"),
        calc("Calculation_39", "Window Stdevp", "WINDOW_STDEVP(SUM([Sales]))"),
        calc("Calculation_40", "Window Var", "WINDOW_VAR(SUM([Sales]))"),
        calc("Calculation_41", "Window Covar", "WINDOW_COVAR(SUM([Sales]), SUM([Profit]))"),
        calc("Calculation_42", "Window Covarp", "WINDOW_COVARP(SUM([Sales]), SUM([Profit]))"),
        calc("Calculation_43", "Run Sum", "RUNNING_SUM(SUM([Sales]))"),
        calc("Calculation_44", "Run Count", "RUNNING_COUNT(SUM([Sales]))", "integer"),
        calc("Calculation_45", "Run Min", "RUNNING_MIN(SUM([Sales]))"),
        calc("Calculation_46", "Run Max", "RUNNING_MAX(SUM([Sales]))"),
        calc("Calculation_47", "Window Count", "WINDOW_COUNT(SUM([Sales]))", "integer"),
        calc("Calculation_48", "Window Min", "WINDOW_MIN(SUM([Sales]), -1, 0)"),
        calc("Calculation_49", "Window Max", "WINDOW_MAX(SUM([Sales]), 0, 1)"),
    ]
)


def tc_instance(n: int, ordering: str = "Columns") -> str:
    return instance(
        f"[Calculation_{n}]",
        "User",
        f"[usr:Calculation_{n}:qk]",
        "quantitative",
        f"<table-calc ordering-type='{ordering}' />",
    )


def quick(kind: str, name: str, extra: str = "") -> str:
    return instance(
        "[Sales]",
        "Sum",
        f"[{name}:sum:Sales:qk]",
        "quantitative",
        f"<table-calc ordering-type='Columns' type='{kind}'{extra} />",
    )


def tc_rows(run: Run, instances: list[str], encodings: str, **kwargs: str) -> list[dict[str, Any]]:
    return run(region_sheet(instances, encodings=encodings, **kwargs), TC_CALCS)


def test_running_total_follows_sort(run: Run) -> None:
    inst = [quick("CumTotal", "cum")]
    enc = f"<text column='{ref('cum:sum:Sales:qk')}' />"
    rows = tc_rows(run, inst, enc)
    key = "sum(f_sales)#tc_running_total_sales"
    assert [(r["f_region"], r[key]) for r in rows] == [
        ("East", 350),
        ("North", 410),
        ("South", 510),
        ("West", 1310),
    ]
    sort = f"<computed-sort column='{ref('none:Region:nk')}' direction='DESC' using='{ref('sum:Sales:qk')}' />"
    rows = tc_rows(run, inst, enc, view_extra=sort)
    assert [(r["f_region"], r[key]) for r in rows] == [
        ("West", 800),
        ("East", 1150),
        ("South", 1250),
        ("North", 1310),
    ]


def test_percent_of_total_and_rank(run: Run) -> None:
    inst = [
        quick("PctTotal", "pcto"),
        quick("Rank", "rank", " rank-options='Competition,Descending'"),
    ]
    enc = f"<text column='{ref('pcto:sum:Sales:qk')}' /><tooltip column='{ref('rank:sum:Sales:qk')}' />"
    rows = tc_rows(run, inst, enc)
    assert by_region(rows, "sum(f_sales)#tc_percent_of_total_sales") == approx(
        {"East": 350 / 1310, "North": 60 / 1310, "South": 100 / 1310, "West": 800 / 1310}
    )
    assert by_region(rows, "sum(f_sales)#tc_rank_sales") == {
        "East": 2,
        "North": 4,
        "South": 3,
        "West": 1,
    }


def diff_instance(kind: str, name: str, offset: int) -> str:
    return instance(
        "[Sales]",
        "Sum",
        f"[{name}:sum:Sales:qk]",
        "quantitative",
        f"<table-calc diff-options='Relative' ordering-type='Columns' type='{kind}'>"
        f"<address><value>{offset}</value></address></table-calc>",
    )


def test_difference_and_percent_difference(run: Run) -> None:
    # 依 Region 排序：East 350、North 60、South 100、West 800
    inst = [diff_instance("Difference", "diff", -1), diff_instance("PctDiff", "pcdf", -1)]
    enc = f"<text column='{ref('diff:sum:Sales:qk')}' /><tooltip column='{ref('pcdf:sum:Sales:qk')}' />"
    rows = tc_rows(run, inst, enc)
    assert by_region(rows, "sum(f_sales)#tc_difference_sales") == {
        "East": None,
        "North": -290.0,
        "South": 40.0,
        "West": 700.0,
    }
    assert by_region(rows, "sum(f_sales)#tc_percent_difference_sales") == approx(
        {"East": None, "North": -290 / 350, "South": 40 / 60, "West": 7.0}
    )
    # 與下一個比較
    rows = tc_rows(
        run,
        [diff_instance("Difference", "diff", 1)],
        f"<text column='{ref('diff:sum:Sales:qk')}' />",
    )
    assert by_region(rows, "sum(f_sales)#tc_difference_sales") == {
        "East": 290.0,
        "North": -40.0,
        "South": -700.0,
        "West": None,
    }


def moving_instance(name: str, aggregation: str, start: int, end: int, options: str) -> str:
    return instance(
        "[Sales]",
        "Sum",
        f"[{name}:sum:Sales:qk]",
        "quantitative",
        f"<table-calc aggregation='{aggregation}' from='{start}' ordering-type='Columns' "
        f"to='{end}' type='WindowTotal' window-options='{options}' />",
    )


def test_moving_calculations(run: Run) -> None:
    # 依 Region 排序：East 350、North 60、South 100、West 800
    inst = [
        moving_instance("mavg", "Avg", -2, 0, "IncludeCurrent"),
        moving_instance("msum", "Sum", -1, 1, "IncludeCurrent,NullIfIncomplete"),
    ]
    enc = f"<text column='{ref('mavg:sum:Sales:qk')}' /><tooltip column='{ref('msum:sum:Sales:qk')}' />"
    rows = tc_rows(run, inst, enc)
    assert by_region(rows, "sum(f_sales)#tc_moving_sales") == approx(
        {"East": 350, "North": 205, "South": 170, "West": 320}
    )
    assert by_region(rows, "sum(f_sales)#tc_moving_sales_2") == {
        "East": None,  # 前面沒有值，不足時為 Null
        "North": 510,
        "South": 960,
        "West": None,
    }
    rows = tc_rows(
        run,
        [moving_instance("mmax", "Max", 0, 1, "IncludeCurrent")],
        f"<text column='{ref('mmax:sum:Sales:qk')}' />",
    )
    assert by_region(rows, "sum(f_sales)#tc_moving_sales") == {
        "East": 350,
        "North": 100,
        "South": 800,
        "West": 800,  # 視窗超出範圍時只取現有的標記
    }


def test_table_across_with_rows_only_is_one_partition_per_mark(run: Run) -> None:
    inst = [tc_instance(20, "Rows")]
    rows = tc_rows(run, inst, f"<text column='{ref('usr:Calculation_20:qk')}' />")
    assert set(by_region(rows, "f_rank#tc_formula_rank").values()) == {1}


def test_partitioning_by_columns(run: Run) -> None:
    inst = [quick("CumTotal", "cum")]
    sheet = region_sheet(
        inst, cols=ref("none:Category:nk"), encodings=f"<text column='{ref('cum:sum:Sales:qk')}' />"
    )
    rows = run(sheet, TC_CALCS)
    result = {
        (r["f_category"], r["f_region"]): r["sum(f_sales)#tc_running_total_sales"] for r in rows
    }
    assert result == {
        ("Furniture", "East"): 150,
        ("Furniture", "South"): 230,
        ("Furniture", "West"): 530,
        ("Technology", "East"): 200,
        ("Technology", "West"): 700,
        ("Office", "North"): 60,
        ("Office", "South"): 80,
    }


def test_window_functions(run: Run) -> None:
    numbers = (21, 22, 23, 24, 25, 26, 27, 28, 33)
    inst = [tc_instance(n) for n in numbers]
    enc = "".join(f"<tooltip column='{ref(f'usr:Calculation_{n}:qk')}' />" for n in numbers)
    rows = tc_rows(run, inst, enc)
    by = {r["f_region"]: r for r in rows}

    def col(n: int, caption: str) -> dict[str, Any]:
        key = next(k for k in rows[0] if k.endswith(f"#tc_formula_{caption}"))
        return {region: by[region][key] for region in by}

    assert col(21, "index") == {"East": 1, "North": 2, "South": 3, "West": 4}
    assert col(22, "first") == {"East": 0, "North": -1, "South": -2, "West": -3}
    assert col(23, "last") == {"East": 3, "North": 2, "South": 1, "West": 0}
    assert col(24, "size") == {"East": 4, "North": 4, "South": 4, "West": 4}
    assert col(25, "win") == {"East": 410, "North": 510, "South": 960, "West": 900}
    assert col(26, "prev") == {"East": None, "North": 350, "South": 60, "West": 100}
    assert col(27, "avg_total") == approx({r: 1310 / 9 for r in by})
    assert col(28, "run_avg") == approx({"East": 350, "North": 205, "South": 170, "West": 327.5})
    assert col(33, "window_avg") == approx({r: 327.5 for r in by})


def test_window_statistics(run: Run) -> None:
    import statistics

    numbers = tuple(range(34, 43))
    inst = [tc_instance(n) for n in numbers]
    enc = "".join(f"<tooltip column='{ref(f'usr:Calculation_{n}:qk')}' />" for n in numbers)
    rows = tc_rows(run, inst, enc)
    by = {r["f_region"]: r for r in rows}

    def col(caption: str) -> dict[str, Any]:
        key = next(k for k in rows[0] if k.endswith(f"#tc_formula_{caption}"))
        return {region: by[region][key] for region in by}

    # 依 Region 排序：East 350、North 60、South 100、West 800；Profit：45、6、10、20
    sales = [350.0, 60.0, 100.0, 800.0]
    profit = [45.0, 6.0, 10.0, 20.0]
    every = lambda value: approx(dict.fromkeys(by, value))  # noqa: E731
    assert col("window_median") == every(225.0)
    assert col("window_stdev") == every(statistics.stdev(sales))
    assert col("window_stdevp") == every(statistics.pstdev(sales))
    assert col("window_var") == every(statistics.variance(sales))
    assert col("window_varp") == every(statistics.pvariance(sales))
    assert col("window_corr") == every(statistics.correlation(sales, profit))
    assert col("window_covar") == every(statistics.covariance(sales, profit))
    assert col("window_covarp") == every(statistics.covariance(sales, profit) * 3 / 4)
    # 前後各一列的 25 百分位數（線性內插）：East [350, 60] → 132.5；North [350, 60, 100] → 80
    assert col("window_pct") == approx({"East": 132.5, "North": 80.0, "South": 80.0, "West": 275.0})


def test_running_and_window_basics(run: Run) -> None:
    numbers = tuple(range(43, 50))
    inst = [tc_instance(n) for n in numbers]
    enc = "".join(f"<tooltip column='{ref(f'usr:Calculation_{n}:qk')}' />" for n in numbers)
    rows = tc_rows(run, inst, enc)
    by = {r["f_region"]: r for r in rows}

    def col(caption: str) -> dict[str, Any]:
        key = next(k for k in rows[0] if k.endswith(f"#tc_formula_{caption}"))
        return {region: by[region][key] for region in by}

    assert col("run_sum") == {"East": 350, "North": 410, "South": 510, "West": 1310}
    assert col("run_count") == {"East": 1, "North": 2, "South": 3, "West": 4}
    assert col("run_min") == {"East": 350, "North": 60, "South": 60, "West": 60}
    assert col("run_max") == {"East": 350, "North": 350, "South": 350, "West": 800}
    assert col("window_count") == {"East": 4, "North": 4, "South": 4, "West": 4}
    assert col("window_min") == {"East": 350, "North": 60, "South": 60, "West": 100}
    assert col("window_max") == {"East": 350, "North": 100, "South": 800, "West": 800}


def test_every_table_calc_function_is_tested() -> None:
    import re
    from pathlib import Path

    from tmig.formula.catalog import is_table_calc_function
    from tmig.formula.functions import implemented_names

    text = Path(__file__).read_text(encoding="utf-8")
    names = [n for n in implemented_names() if is_table_calc_function(n)]
    assert [n for n in names if not re.search(rf"\b{n}\(", text)] == []


def test_rank_ties(run: Run) -> None:
    numbers = (29, 30, 31, 32)
    inst = [tc_instance(n) for n in numbers]
    enc = "".join(f"<tooltip column='{ref(f'usr:Calculation_{n}:qk')}' />" for n in numbers)
    rows = tc_rows(run, inst, enc)
    by = {r["f_region"]: r for r in rows}

    def col(caption: str) -> dict[str, Any]:
        key = next(k for k in rows[0] if caption in k)
        return {region: by[region][key] for region in by}

    # 訂單數：East 2、South 2、West 2、North 1
    assert col("tc_formula_dense") == {"East": 1, "North": 2, "South": 1, "West": 1}
    assert col("tc_formula_modified") == {"East": 3, "North": 4, "South": 3, "West": 3}
    assert col("tc_formula_unique") == {"East": 1, "North": 4, "South": 2, "West": 3}
    assert col("tc_formula_competition") == {"East": 1, "North": 4, "South": 1, "West": 1}


def test_table_calc_filter_keeps_values(run: Run) -> None:
    flt = (
        f"<filter class='quantitative' column='{ref('usr:Calculation_20:qk')}' included-values='in-range'>"
        "<max>2</max></filter>"
    )
    inst = [tc_instance(20)]
    rows = tc_rows(run, inst, f"<text column='{ref('usr:Calculation_20:qk')}' />", view_extra=flt)
    assert by_region(rows, "f_rank#tc_formula_rank") == {"West": 1, "East": 2}


def test_measure_filter_runs_before_table_calc(run: Run) -> None:
    flt = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>100</min></filter>"
    )
    inst = [quick("CumTotal", "cum")]
    rows = tc_rows(run, inst, f"<text column='{ref('cum:sum:Sales:qk')}' />", view_extra=flt)
    assert [(r["f_region"], r["sum(f_sales)#tc_running_total_sales"]) for r in rows] == [
        ("East", 350),
        ("South", 450),
        ("West", 1250),
    ]


# ---------------------------------------------------------------- 邊界情況


def test_dimension_from_fixed(run: Run) -> None:
    calcs = calc(
        "Calculation_40",
        "Size",
        'IF {FIXED [Region] : SUM([Sales])} > 300 THEN "Big" ELSE "Small" END',
        "string",
        "dimension",
    )
    sheet = region_sheet(
        [instance("[Calculation_40]", "None", "[none:Calculation_40:nk]")],
        rows=ref("none:Calculation_40:nk"),
    )
    rows = run(sheet, calcs)
    assert {r["f_size"]: r["sum(f_sales)"] for r in rows} == {"Big": 1150, "Small": 160}
    # FIXED 維度上的維度篩選
    flt = categorical("none:Calculation_40:nk", ["Big"])
    rows = run(
        region_sheet(
            [instance("[Calculation_40]", "None", "[none:Calculation_40:nk]")], view_extra=flt
        ),
        calcs,
    )
    assert {r["f_region"] for r in rows} == {"East", "West"}


def test_measure_filter_on_hidden_aggregate_calc(run: Run) -> None:
    calcs = calc("Calculation_41", "Ratio", "SUM([Profit]) / SUM([Sales])")
    flt = (
        f"<filter class='quantitative' column='{ref('usr:Calculation_41:qk')}' included-values='in-range'>"
        "<min>0.1</min></filter>"
    )
    sheet = region_sheet(
        [instance("[Calculation_41]", "User", "[usr:Calculation_41:qk]", "quantitative")],
        view_extra=flt,
    )
    rows = run(sheet, calcs)
    # 比率：East 0.1286、West 0.025、South 0.1、North 0.1
    assert {r["f_region"] for r in rows} == {"East", "South", "North"}
    assert "f_ratio" not in rows[0]


def test_top_n_by_fixed_with_context(run: Run) -> None:
    calcs = calc("Calculation_42", "RS", "{FIXED [Region] : SUM([Sales])}")
    flt = top_n("none:Region:nk", "1", by="MAX([Calculation_42])") + categorical(
        "none:Category:nk", ["Office"], context=True
    )
    rows = run(region_sheet(view_extra=flt), calcs)
    # 內容篩選後 FIXED：North 60、South 20 → 第一名 North
    assert {r["f_region"]: r["sum(f_sales)"] for r in rows} == {"North": 60}


def test_percent_of_total_with_partition(run: Run) -> None:
    inst = [quick("PctTotal", "pcto")]
    sheet = region_sheet(
        inst,
        cols=ref("none:Category:nk"),
        encodings=f"<text column='{ref('pcto:sum:Sales:qk')}' />",
    )
    rows = run(sheet)
    got = {
        (r["f_category"], r["f_region"]): round(r["sum(f_sales)#tc_percent_of_total_sales"], 6)
        for r in rows
    }
    assert got[("Furniture", "East")] == round(150 / 530, 6)
    assert got[("Office", "North")] == round(60 / 80, 6)


def test_exclude_as_dimension_is_unsupported(run: Run) -> None:
    calcs = calc("Calculation_43", "Ex", "{EXCLUDE [Category] : SUM([Sales])}", "real", "dimension")
    sheet = region_sheet(
        [instance("[Calculation_43]", "None", "[none:Calculation_43:qk]", "quantitative")],
        rows=ref("none:Calculation_43:qk"),
    )
    with pytest.raises(QueryError):
        run(sheet, calcs)


# ---------------------------------------------------------------- 錯誤處理


def test_unknown_worksheet(tmp_path: Path) -> None:
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=region_sheet())))
    with pytest.raises(QueryError, match="找不到工作表"):
        worksheet_sql(ir, "nope")


def test_sql_has_stage_comments_and_pushdown_boundary(tmp_path: Path) -> None:
    inst = [quick("CumTotal", "cum")]
    sheet = region_sheet(inst, encodings=f"<text column='{ref('cum:sum:Sales:qk')}' />")
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    query = worksheet_sql(ir, ir.worksheets[0].id)
    assert "-- 第 1 段" in query.sql
    assert "以上可下推到來源資料庫" in query.sql
    assert query.sql.index("以上可下推") < query.sql.index("-- 第 7 段")
    assert [n.stage for n in query.plan] == [1, 6, 7]
