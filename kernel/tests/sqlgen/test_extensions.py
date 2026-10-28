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
from .test_query import CATEGORY, REGION, ROWS, SALES, YEAR, Run, by_region


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
