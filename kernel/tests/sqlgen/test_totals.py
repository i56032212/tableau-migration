"""總計（Grand Total）：與 Tableau 的「自動」相同，在去掉該架上離散維度的詳細程度重新彙總底層資料。

測試資料與手算結果見 test_query.py（各區銷售 East 350、West 800、South 100、North 60，合計 1310；
各類別 Furniture 530、Technology 700、Office 80）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import execute
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import QueryError, worksheet_sql
from tmig.sqlgen.query import totals_sql as make_totals

from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet
from .test_query import CATEGORY, REGION, SALES, categorical, db, top_n

__all__ = ["db"]  # pytest fixture（module scope 的測試資料庫）

AVG_SALES = instance("[Sales]", "Avg", "[avg:Sales:qk]", "quantitative")
CTD_CATEGORY = instance("[Category]", "CountD", "[ctd:Category:qk]", "quantitative")
ORDER_AVG = instance("[Calculation_1]", "User", "[usr:Calculation_1:qk]", "quantitative")
CALCS = calc("Calculation_1", "Order Avg", "AVG({INCLUDE [Order ID] : SUM([Sales])})")


def text(*instances: str) -> str:
    return "".join(f"<text column='{ref(i)}' />" for i in instances)


def sheet(
    rows: str = "none:Region:nk",
    cols: str = "",
    encodings: str = "",
    totals: tuple[str, ...] = ("rows",),
    view_extra: str = "",
    instances: list[str] | None = None,
) -> str:
    xml = worksheet(
        "S",
        instances or [REGION, CATEGORY, SALES, AVG_SALES, CTD_CATEGORY, ORDER_AVG],
        rows=ref(rows),
        cols=ref(cols) if cols else "",
        mark="Text",
        encodings=encodings or text("sum:Sales:qk"),
        view_extra=view_extra,
    )
    for shelf in totals:
        xml = xml.replace(f"<{shelf}>", f"<{shelf} total='true'>", 1)
    return xml


def run_totals(tmp_path: Path, db: Path, xml: str) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    """各種總計的查詢結果（去掉 __ 開頭的內部欄），以加上總計的架為鍵。"""
    ir = parse_workbook(
        b.write_twb(tmp_path, b.workbook(b.extract_datasource(columns=CALCS), worksheets=xml))
    )
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    main = worksheet_sql(ir, ws.id)
    result: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for total in make_totals(ir, ws.id, main):
        rows = execute(db, total.query.sql).records()
        result[total.shelves] = [
            {k: v for k, v in r.items() if not k.startswith("__")} for r in rows
        ]
    return result


def test_totals_reaggregate_the_underlying_rows(tmp_path: Path, db: Path) -> None:
    """平均是所有資料列的平均、相異計數是整體的相異數，不是各列結果再加總或平均。"""
    encodings = text("sum:Sales:qk", "avg:Sales:qk", "ctd:Category:qk", "usr:Calculation_1:qk")
    totals = run_totals(tmp_path, db, sheet(encodings=encodings))
    ((total,),) = totals.values()
    assert total == {
        "sum(f_sales)": 1310,
        "avg(f_sales)": pytest.approx(1310 / 9),
        # 各區的相異類別數加起來是 7，整體只有 3 種
        "countd(f_category)": 3,
        # INCLUDE 相對於總計的詳細程度：7 張訂單的平均
        "f_order_avg": pytest.approx(1310 / 7),
    }


def test_row_column_and_corner_totals(tmp_path: Path, db: Path) -> None:
    xml = sheet(cols="none:Category:nk", totals=("rows", "cols"))
    totals = run_totals(tmp_path, db, xml)
    assert list(totals) == [("rows",), ("columns",), ("rows", "columns")]
    # 總計列（去掉列架的 Region）：每個 Category 的合計
    assert {r["f_category"]: r["sum(f_sales)"] for r in totals[("rows",)]} == {
        "Furniture": 530,
        "Office": 80,
        "Technology": 700,
    }
    # 總計欄（去掉欄架的 Category）：每個 Region 的合計
    assert {r["f_region"]: r["sum(f_sales)"] for r in totals[("columns",)]} == {
        "East": 350,
        "North": 60,
        "South": 100,
        "West": 800,
    }
    assert totals[("rows", "columns")] == [{"sum(f_sales)": 1310}]


def test_marks_card_dimensions_stay_in_totals(tmp_path: Path, db: Path) -> None:
    """標記卡上的維度（例如顏色）不在架上，總計仍依它分開。"""
    encodings = f"<color column='{ref('none:Category:nk')}' />" + text("sum:Sales:qk")
    totals = run_totals(tmp_path, db, sheet(encodings=encodings))
    assert {r["f_category"]: r["sum(f_sales)"] for r in totals[("rows",)]} == {
        "Furniture": 530,
        "Office": 80,
        "Technology": 700,
    }


def test_filters_apply_to_totals(tmp_path: Path, db: Path) -> None:
    only = categorical("none:Category:nk", ["Furniture", "Technology"])
    totals = run_totals(tmp_path, db, sheet(view_extra=only))
    assert totals[("rows",)] == [{"sum(f_sales)": 1230}]
    # 前 N 名：總計只合計留下來的 West 與 East
    top = top_n("none:Region:nk", "2")
    totals = run_totals(tmp_path, db, sheet(view_extra=top))
    assert totals[("rows",)] == [{"sum(f_sales)": 1150}]


def test_sort_on_the_totaled_dimension_is_dropped(tmp_path: Path, db: Path) -> None:
    sort = (
        f"<computed-sort column='{ref('none:Region:nk')}' direction='DESC' "
        f"using='{ref('sum:Sales:qk')}' />"
    )
    totals = run_totals(tmp_path, db, sheet(view_extra=sort))
    assert totals[("rows",)] == [{"sum(f_sales)": 1310}]


def test_no_totals_without_the_setting(tmp_path: Path, db: Path) -> None:
    assert run_totals(tmp_path, db, sheet(totals=())) == {}


def test_unsupported_totals_are_refused(tmp_path: Path) -> None:
    """總計中的 Table Calculation、與量值篩選一起使用：算不出與 Tableau 相同的數字，列為不支援。"""
    running = instance(
        "[Sales]",
        "Sum",
        "[cum:sum:Sales:qk]",
        "quantitative",
        "<table-calc ordering-type='Rows' type='CumTotal' />",
    )
    measure_filter = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>300</min></filter>"
    )
    cases = {
        "總計中的 Table Calculation 尚不支援": sheet(
            encodings=text("cum:sum:Sales:qk"), instances=[REGION, SALES, running]
        ),
        "總計與量值篩選一起使用尚不支援": sheet(view_extra=measure_filter),
    }
    for message, xml in cases.items():
        ir = parse_workbook(
            b.write_twb(tmp_path, b.workbook(b.extract_datasource(), worksheets=xml))
        )
        ws = ir.worksheets[0]
        main = worksheet_sql(ir, ws.id)
        with pytest.raises(QueryError, match=message) as info:
            make_totals(ir, ws.id, main)
        assert info.value.unsupported
