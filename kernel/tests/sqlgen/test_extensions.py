"""擴充階段的查詢（H1–H4、J1–J2、K1–K4）：以 test_query 的 9 列資料手算驗證。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import QueryState
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_query import CATEGORY, DATE, REGION, ROWS, SALES, YEAR, Run, by_region


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


@pytest.fixture
def run(tmp_path: Path, db: Path) -> Run:
    return Run(tmp_path, db)


# ---------------------------------------------------------------- H2 資料表筆數欄位


def test_table_count_counts_rows(run: Run) -> None:
    table = "[__tableau_internal_object_id__].[Orders_ABC123]"
    count = instance(table, "Count", "[__tableau_internal_object_id__].[cnt:Orders_ABC123:qk]")
    graph = "<object-graph><objects><object caption='Orders' id='Orders_ABC123' /></objects></object-graph>"
    sheet = worksheet(
        "S",
        [REGION, SALES, count],
        rows=ref("none:Region:nk"),
        cols=ref("__tableau_internal_object_id__].[cnt:Orders_ABC123:qk"),
    )
    rows = run(sheet, ds_extra=graph)
    assert by_region(rows, "count(f_orders_count)") == {
        "East": 3,
        "West": 3,
        "South": 2,
        "North": 1,
    }


def test_shared_view_filter_reaches_query(run: Run) -> None:
    shared = f"""
  <shared-views>
    <shared-view name='{b.DS}'>
      <datasources><datasource caption='Orders' name='{b.DS}' /></datasources>
      <datasource-dependencies datasource='{b.DS}'>
        {instance("[Region]", "None", "[none:Region:nk]")}
      </datasource-dependencies>
      <filter class='categorical' column='{ref("none:Region:nk")}'>
        <groupfilter function='except'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
          <groupfilter function='member' level='[none:Region:nk]' member='&quot;West&quot;' />
        </groupfilter>
      </filter>
    </shared-view>
  </shared-views>"""
    sheet = worksheet(
        "S",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=f"<slices><column>{ref('none:Region:nk')}</column></slices>",
    )
    rows = run(sheet, extra=shared)
    assert by_region(rows, "sum(f_sales)") == {"East": 350.0, "South": 100.0, "North": 60.0}


# ---------------------------------------------------------------- H4、L1 多欄位篩選與篩選動作

ACTION_GROUPS = """
      <group caption='Action (Region)' hidden='true' name='[Action (Region)]' name-style='unqualified' user:auto-column='sheet_link'>
        <groupfilter function='crossjoin'><groupfilter function='level-members' level='[Region]' /></groupfilter>
      </group>"""


def action_run(
    tmp_path: Path,
    db: Path,
    target_filter: str,
    state: QueryState | None,
    on_empty: str = "",
    groups: str = ACTION_GROUPS,
) -> list[dict[str, Any]]:
    """來源工作表（Region × Category）的篩選動作 → 目標工作表（依 Region 的 Sales）。"""
    source = worksheet(
        "Source", [REGION, CATEGORY, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    target = worksheet(
        "Target",
        [REGION, CATEGORY, SALES, YEAR],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=target_filter,
    )
    board = """
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' param='horz' type-v2='layout-flow' w='100000' x='0' y='0'>
          <zone h='100000' id='2' name='Source' w='50000' x='0' y='0' />
          <zone h='100000' id='3' name='Target' w='50000' x='50000' y='0' />
        </zone>
      </zones>
    </dashboard>"""
    param = f"<param name='on-empty' value='{on_empty}' />" if on_empty else ""
    action = (
        "<action caption='Filter' name='[Action1]'><activation auto-clear='true' type='on-select' />"
        "<source dashboard='Board' type='sheet' worksheet='Source' />"
        f"<command command='tsc:tsl-filter'><param name='special-fields' value='all' />{param}"
        "<param name='target' value='Board' /></command></action>"
    )
    xml = b.workbook(
        b.extract_datasource(extra=groups),
        worksheets=source + target,
        dashboards=board,
        actions=action,
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert [a.id for a in ir.actions] == ["act_filter"]
    query = worksheet_sql(ir, "ws_target", state)
    return [
        {k: v for k, v in row.items() if not k.startswith("__")}
        for row in execute(db, query.sql).records()
    ]


def region_action(function: str = "level-members", level: str = "[Region]") -> str:
    attr = f"member='{level}'" if function == "empty-level" else f"level='{level}'"
    return (
        f"<filter class='categorical' column='{ref('Action (Region)')}'>"
        f"<groupfilter function='{function}' {attr} user:ui-action-filter='[Action1]' /></filter>"
    )


def selected(*marks: dict[str, Any]) -> QueryState:
    return QueryState(actions={"act_filter": list(marks)})


ALL_REGIONS = {"East": 350.0, "West": 800.0, "South": 100.0, "North": 60.0}


def test_filter_action_selection(tmp_path: Path, db: Path) -> None:
    flt = region_action()
    assert by_region(action_run(tmp_path, db, flt, None), "sum(f_sales)") == ALL_REGIONS
    # 選取一個標記：來源標記的其他欄位（Category）不在目標的動作篩選中，不影響
    rows = action_run(tmp_path, db, flt, selected({"f_region": "East", "f_category": "Technology"}))
    assert by_region(rows, "sum(f_sales)") == {"East": 350.0}
    rows = action_run(tmp_path, db, flt, selected({"f_region": "East"}, {"f_region": "North"}))
    assert by_region(rows, "sum(f_sales)") == {"East": 350.0, "North": 60.0}
    # 選取的標記沒有目標篩選的欄位：視同沒有選取
    rows = action_run(tmp_path, db, flt, selected({"f_category": "Office"}))
    assert by_region(rows, "sum(f_sales)") == ALL_REGIONS
    # 空的選取＝清除
    assert by_region(action_run(tmp_path, db, flt, selected()), "sum(f_sales)") == ALL_REGIONS


def test_filter_action_exclude_all_until_selected(tmp_path: Path, db: Path) -> None:
    flt = region_action("empty-level")
    assert action_run(tmp_path, db, flt, None, on_empty="none") == []
    rows = action_run(tmp_path, db, flt, selected({"f_region": "West"}), on_empty="none")
    assert by_region(rows, "sum(f_sales)") == {"West": 800.0}


def test_filter_action_on_date_part(tmp_path: Path, db: Path) -> None:
    groups = """
      <group caption='Action (YEAR(Order Date))' hidden='true' name='[Action (YEAR(Order Date))]' name-style='unqualified' user:auto-column='sheet_link'>
        <groupfilter function='crossjoin'><groupfilter function='level-members' level='[yr:Order Date:ok]' /></groupfilter>
      </group>"""
    flt = (
        f"<filter class='categorical' column='{ref('Action (YEAR(Order Date))')}'>"
        "<groupfilter function='level-members' level='[yr:Order Date:ok]' user:ui-action-filter='[Action1]' />"
        "</filter>"
    )
    state = selected({"year(f_order_date)": 2024})
    rows = action_run(tmp_path, db, flt, state, groups=ACTION_GROUPS + groups)
    assert by_region(rows, "sum(f_sales)") == {"West": 500.0, "South": 100.0, "North": 60.0}


def test_exclusions_filter_drops_value_combinations(run: Run) -> None:
    exclusions = f"""<filter class='categorical' column='{ref("Exclusions (Region,Category)")}'>
      <groupfilter function='except' user:ui-enumeration='exclusive'>
        <groupfilter function='crossjoin'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
          <groupfilter function='level-members' level='[none:Category:nk]' />
        </groupfilter>
        <groupfilter function='union'>
          <groupfilter function='crossjoin'>
            <groupfilter function='member' level='[none:Region:nk]' member='&quot;East&quot;' />
            <groupfilter function='member' level='[none:Category:nk]' member='&quot;Furniture&quot;' />
          </groupfilter>
          <groupfilter function='crossjoin'>
            <groupfilter function='member' level='[none:Region:nk]' member='&quot;West&quot;' />
            <groupfilter function='member' level='[none:Category:nk]' member='&quot;Technology&quot;' />
          </groupfilter>
        </groupfilter>
      </groupfilter>
    </filter>"""
    sheet = worksheet(
        "S",
        [REGION, CATEGORY, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=exclusions,
    )
    assert by_region(run(sheet), "sum(f_sales)") == {
        "East": 200.0,
        "West": 300.0,
        "South": 100.0,
        "North": 60.0,
    }


# ---------------------------------------------------------------- K1 群組與數值分組

REGION_GROUP = """<column caption='Region (group)' datatype='string' name='[Region (group)]' role='dimension' type='nominal'>
  <calculation class='categorical-bin' column='[Region]' new-bin='true'{default}>
    <bin default-name='false' value='&quot;Coasts&quot;'>
      <value>&quot;East&quot;</value>
      <value>&quot;West&quot;</value>
    </bin>
  </calculation>
</column>"""


def bin_column(name: str, formula: str, size: str) -> str:
    size_attr = f"size-parameter='{size}'" if size.startswith("[") else f"size='{size}'"
    return (
        f"<column aggregation='None' caption='{name} (bin)' datatype='integer' name='[{name} (bin)]' "
        "role='dimension' type='ordinal'>"
        f"<calculation class='bin' decimals='0' formula='{formula}' peg='0' {size_attr} /></column>"
    )


def counts(rows: list[dict[str, Any]], key: str) -> dict[Any, Any]:
    return {r[key]: r["count(f_order_id)"] for r in rows}


ORDERS = instance("[Order ID]", "Count", "[cnt:Order ID:qk]", "quantitative")


def test_group_keeps_ungrouped_members(run: Run) -> None:
    group = instance("[Region (group)]", "None", "[none:Region (group):nk]")
    sheet = worksheet(
        "S", [group, SALES], rows=ref("none:Region (group):nk"), cols=ref("sum:Sales:qk")
    )
    rows = run(sheet, calcs=REGION_GROUP.format(default=""))
    assert {r["f_region_group"]: r["sum(f_sales)"] for r in rows} == {
        "Coasts": 1150.0,
        "South": 100.0,
        "North": 60.0,
    }


def test_group_with_other(run: Run) -> None:
    group = instance("[Region (group)]", "None", "[none:Region (group):nk]")
    sheet = worksheet(
        "S", [group, SALES], rows=ref("none:Region (group):nk"), cols=ref("sum:Sales:qk")
    )
    rows = run(sheet, calcs=REGION_GROUP.format(default=" default='&quot;Other&quot;'"))
    assert {r["f_region_group"]: r["sum(f_sales)"] for r in rows} == {
        "Coasts": 1150.0,
        "Other": 160.0,
    }


def test_bins_take_lower_bound(run: Run) -> None:
    sales_bin = instance("[Sales (bin)]", "None", "[none:Sales (bin):ok]", "ordinal")
    profit_bin = instance("[Profit (bin)]", "None", "[none:Profit (bin):ok]", "ordinal")
    calcs = bin_column("Sales", "[Sales]", "100") + bin_column("Profit", "[Profit]", "25")
    sheet = worksheet(
        "S", [sales_bin, ORDERS], rows=ref("none:Sales (bin):ok"), cols=ref("cnt:Order ID:qk")
    )
    rows = run(sheet, calcs=calcs)
    assert counts(rows, "f_sales_bin") == {0: 4, 100: 2, 200: 1, 300: 1, 400: 1}
    # 負數往下取整：-5 與 -20 都在 [-25, 0)
    sheet = worksheet(
        "S", [profit_bin, ORDERS], rows=ref("none:Profit (bin):ok"), cols=ref("cnt:Order ID:qk")
    )
    assert counts(run(sheet, calcs=calcs), "f_profit_bin") == {-25: 2, 0: 5, 25: 2}


def test_bin_size_from_parameter(run: Run) -> None:
    quantity_bin = instance("[Quantity (bin)]", "None", "[none:Quantity (bin):ok]", "ordinal")
    calcs = bin_column("Quantity", "[Quantity]", "[Parameters].[Parameter 1]")
    sheet = worksheet(
        "S",
        [quantity_bin, ORDERS],
        rows=ref("none:Quantity (bin):ok"),
        cols=ref("cnt:Order ID:qk"),
    )
    assert counts(run(sheet, calcs=calcs), "f_quantity_bin") == {0: 9}  # Top N 預設 5
    state = QueryState(parameters={"p_top_n": 2})
    assert counts(run(sheet, calcs=calcs, state=state), "f_quantity_bin") == {0: 5, 2: 3, 4: 1}


# ---------------------------------------------------------------- K4 條件、萬用字元、相對日期與 Top N 的選取


def region_filtered(run: Run, flt: str, state: QueryState | None = None, calcs: str = "") -> dict:
    sheet = worksheet(
        "S",
        [REGION, CATEGORY, SALES, DATE],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=flt,
    )
    return by_region(run(sheet, calcs=calcs, state=state), "sum(f_sales)")


def condition(
    expression: str,
    inner: str = "<groupfilter function='level-members' level='[none:Region:nk]' />",
) -> str:
    from xml.sax.saxutils import quoteattr

    return (
        f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
        f"<groupfilter expression={quoteattr(expression)} function='filter'>{inner}</groupfilter></filter>"
    )


def test_wildcard_condition(run: Run) -> None:
    assert region_filtered(run, condition("CONTAINS([Region], 'st')")) == {
        "East": 350.0,
        "West": 800.0,
    }


def test_aggregate_condition_runs_before_dimension_filters(run: Run) -> None:
    # 每區總銷售額（不受 Category 篩選影響）大於 200 的是 East 350、West 800
    flt = condition("SUM([Sales]) > 200") + (
        f"<filter class='categorical' column='{ref('none:Category:nk')}'>"
        "<groupfilter function='member' level='[none:Category:nk]' member='&quot;Furniture&quot;' />"
        "</filter>"
    )
    assert region_filtered(run, flt) == {"East": 150.0, "West": 300.0}
    # 條件與一般選取同時設定：先排除 West
    inner = (
        "<groupfilter function='except'><groupfilter function='level-members' level='[none:Region:nk]' />"
        "<groupfilter function='member' level='[none:Region:nk]' member='&quot;West&quot;' /></groupfilter>"
    )
    assert region_filtered(run, condition("SUM([Sales]) > 90", inner)) == {
        "East": 350.0,
        "South": 100.0,
    }


def relative(first: int, last: int, period: str, future: str = "true") -> str:
    return (
        f"<filter class='relative-date' column='{ref('none:Order Date:qk')}' first-period='{first}' "
        f"include-future='{future}' include-null='false' last-period='{last}' period-type-v2='{period}' />"
    )


def test_relative_date(run: Run) -> None:
    today = QueryState(today="2024-03-20")
    # 最近 2 個月：2024-02-01 到 2024-03-31
    assert region_filtered(run, relative(-1, 0, "month"), today) == {"South": 100.0, "North": 60.0}
    # 不含基準日之後：2024-03-10 為基準日時不含 03-15
    early = QueryState(today="2024-03-10")
    assert region_filtered(run, relative(-1, 0, "month", "false"), early) == {"South": 100.0}
    # 去年
    assert region_filtered(run, relative(-1, -1, "year"), today) == {"East": 350.0, "West": 300.0}
    # 上週與本週（週日起算）：2024-01-14 到 2024-01-27
    wednesday = QueryState(today="2024-01-24")
    assert region_filtered(run, relative(-1, 0, "week"), wednesday) == {"West": 500.0}


def test_top_n_within_selection(run: Run) -> None:
    inner = "".join(
        f"<groupfilter function='member' level='[none:Region:nk]' member='&quot;{r}&quot;' />"
        for r in ("East", "South", "North")
    )
    flt = (
        f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
        "<groupfilter count='1' end='top' function='end' units='records'>"
        "<groupfilter direction='DESC' expression='SUM([Sales])' function='order'>"
        f"<groupfilter function='union'>{inner}</groupfilter>"
        "</groupfilter></groupfilter></filter>"
    )
    assert region_filtered(run, flt) == {"East": 350.0}


def test_categorical_filter_on_discrete_aggregate(run: Run) -> None:
    calcs = b.calc(
        "Calculation_90",
        "Size",
        'IF SUM([Sales]) > 200 THEN "Big" ELSE "Small" END',
        "string",
        "dimension",
    )
    size = instance("[Calculation_90]", "User", "[usr:Calculation_90:nk]")
    flt = (
        f"<filter class='categorical' column='{ref('usr:Calculation_90:nk')}'>"
        "<groupfilter function='member' level='[usr:Calculation_90:nk]' member='&quot;Big&quot;' /></filter>"
    )
    sheet = worksheet(
        "S",
        [REGION, SALES, size],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=flt,
    )
    assert by_region(run(sheet, calcs=calcs), "sum(f_sales)") == {"East": 350.0, "West": 800.0}


def test_intersection_with_empty_level_keeps_nothing(run: Run) -> None:
    flt = (
        f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
        "<groupfilter function='intersection'>"
        "<groupfilter function='empty-level' member='[none:Region:nk]' />"
        "<groupfilter function='level-members' level='[none:Region:nk]' />"
        "</groupfilter></filter>"
    )
    assert region_filtered(run, flt) == {}
