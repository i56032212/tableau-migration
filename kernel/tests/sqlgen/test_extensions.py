"""擴充階段的查詢（H1–H4、J1–J2、K1–K4）：以 test_query 的 9 列資料手算驗證。"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_query import REGION, ROWS, SALES, Run, by_region


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
