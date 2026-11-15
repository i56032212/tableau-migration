"""顯示遺漏的值：日期與數值分組在最小值到最大值之間補上沒有資料的成員；包含在工具提示中。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from tmig.data.executor import QueryResult, execute
from tmig.ir import IR, DateDerivation, FieldUse
from tmig.render.missing import missing_domain, pad_missing
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from ..parser.builder import instance, ref, worksheet
from .test_text import REGION, SALES, db, parse, render

__all__ = ["db"]  # pytest fixture

MONTH = FieldUse(field="f_month", date=DateDerivation(kind="part", unit="month"))
REGION_USE = FieldUse(field="f_region")
ROLES = {"f_region": "dimension", "month(f_month)": "dimension", "sum(f_sales)": "measure"}


def sheet_with(rows: list[FieldUse], columns: list[FieldUse]) -> Any:
    from tmig.ir import Marks, Worksheet

    return Worksheet(
        id="ws",
        name="S",
        datasource="ds",
        rows=rows,
        columns=columns,
        marks=Marks(type="text"),
        show_missing=[MONTH],
        source_ref="/x",
    )


def test_domains() -> None:
    part = FieldUse(field="f", date=DateDerivation(kind="part", unit="month"))
    assert missing_domain(part, None, [3, None, 6, 5], {}) == [3, 4, 5, 6]
    month = FieldUse(field="f", date=DateDerivation(kind="trunc", unit="month"))
    dates = [dt.date(2023, 11, 1), dt.date(2024, 2, 1)]
    assert missing_domain(month, None, dates, {}) == [
        dt.date(2023, 11, 1),
        dt.date(2023, 12, 1),
        dt.date(2024, 1, 1),
        dt.date(2024, 2, 1),
    ]
    quarter = FieldUse(field="f", date=DateDerivation(kind="trunc", unit="quarter"))
    assert (
        len(missing_domain(quarter, None, [dt.date(2023, 1, 1), dt.date(2024, 1, 1)], {}) or [])
        == 5
    )
    week = FieldUse(field="f", date=DateDerivation(kind="trunc", unit="week"))
    assert (
        len(missing_domain(week, None, [dt.date(2024, 1, 7), dt.date(2024, 1, 28)], {}) or []) == 4
    )
    # 一般的維度（不是日期或數值分組）不能補
    assert missing_domain(FieldUse(field="f"), None, ["a", "c"], {}) is None


def test_rows_are_inserted_in_place() -> None:
    """列：Region、欄：MONTH——每個地區在整個檢視的月份範圍（1 到 4）補上沒有資料的月份；補上的列在
    依排序應該在的位置，量值是空的，欄標題的名次在前後之間。"""
    columns = ["f_region", "month(f_month)", "sum(f_sales)", "__order(columns)"]
    rows = [
        ("East", 1, 10.0, 1),
        ("East", 4, 40.0, 2),
        ("West", 1, 5.0, 1),
        ("West", 4, 7.0, 2),
    ]
    notes: list[str] = []
    ws = sheet_with([REGION_USE], [MONTH])
    padded = pad_missing(ws, {}, ROLES, QueryResult(columns, rows), {}, notes)
    assert [r[:3] for r in padded.rows] == [
        ("East", 1, 10.0),
        ("East", 2, None),
        ("East", 3, None),
        ("East", 4, 40.0),
        ("West", 1, 5.0),
        ("West", 2, None),
        ("West", 3, None),
        ("West", 4, 7.0),
    ]
    order = [r[3] for r in padded.rows[:4]]
    assert (
        order == sorted(order) and order[0] == 1 and order[3] == 2 and 1 < order[1] < order[2] < 2
    )
    # 兩個地區補上的同一個月份是同一欄（名次相同）
    assert padded.rows[1][3] == padded.rows[5][3]
    assert notes == []
    # 再補一次不會多出列
    assert pad_missing(ws, {}, ROLES, padded, {}, notes).rows == padded.rows


def test_outer_dimension_and_descending_order() -> None:
    """月份在外層（列：MONTH、Region）且由大到小排序：補上的月份整組放在前後的月份之間，
    每個地區各一列。"""
    columns = ["month(f_month)", "f_region", "sum(f_sales)"]
    rows = [(4, "East", 40.0), (4, "West", 7.0), (1, "East", 10.0)]
    ws = sheet_with([MONTH, REGION_USE], [])
    padded = pad_missing(ws, {}, ROLES, QueryResult(columns, rows), {}, [])
    assert padded.rows == [
        (4, "East", 40.0),
        (4, "West", 7.0),
        (3, "East", None),
        (3, "West", None),
        (2, "East", None),
        (2, "West", None),
        (1, "East", 10.0),
        (1, "West", None),
    ]


def test_show_missing_values_in_a_view(tmp_path: Path, db: Path) -> None:
    """折線圖：欄為連續的月份、列為 SUM(Sales)；開啟之後沒有資料的月份多一個空的標記（折線在這裡斷開）。"""
    month = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")
    line = worksheet(
        "Trend",
        [month, SALES, REGION],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
    )
    ir = parse(tmp_path, line)
    plain = render(ir, db).spec["data"]["values"]
    ws = ir.worksheets[0]
    use = ws.columns[0]
    shown: IR = ir.model_copy(
        update={"worksheets": [ws.model_copy(update={"show_missing": [use]})]}
    )
    query = worksheet_sql(shown, ws.id)
    rendered = build_spec(shown, ws.id, query.columns, execute(db, query.sql))
    assert rendered.spec is not None
    values = rendered.spec["data"]["values"]
    months = [v["c0"] for v in values]
    assert len(values) > len(plain) and months == sorted(months)
    first, last = dt.date.fromisoformat(months[0][:10]), dt.date.fromisoformat(months[-1][:10])
    assert len(values) == (last.year - first.year) * 12 + last.month - first.month + 1
    empty = [v for v in values if v["c1"] is None]
    assert len(empty) == len(values) - len(plain)

    # 包含在工具提示中：取消之後自動產生的工具提示不列出這個欄位
    hidden = ir.model_copy(
        update={"worksheets": [ws.model_copy(update={"tooltip_excluded": [use]})]}
    )
    titles = [t["title"] for t in render(hidden, db).spec["encoding"]["tooltip"]]
    assert titles == ["Sales"]
    assert len(render(ir, db).spec["encoding"]["tooltip"]) == 2


def test_special_values_text_and_null_marks(tmp_path: Path, db: Path) -> None:
    """特殊值：Null 的量值在標籤上顯示指定的文字；座標軸上的 Null 可以連接前後的點或畫在 0。"""
    month = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")
    line = worksheet(
        "Trend",
        [month, SALES, REGION],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
    )
    ir = parse(tmp_path, line)
    ws = ir.worksheets[0]

    def with_sheet(**update: Any) -> IR:
        return ir.model_copy(update={"worksheets": [ws.model_copy(update=update)]})

    assert "invalid" not in render(ir, db).spec["mark"]
    assert render(with_sheet(null_marks="connect"), db).spec["mark"]["invalid"] == "filter"
    assert render(with_sheet(null_marks="zero"), db).spec["mark"]["invalid"] == "show"
    # 補上的月份量值是 Null：標籤的文字是特殊值的文字
    padded = with_sheet(show_missing=[ws.columns[0]], null_text="無資料")
    values = render(padded, db).spec["data"]["values"]
    empty = [v for v in values if v["c1"] is None]
    assert empty and all(v["c1_label"] == "無資料" for v in empty)
    assert all(v["c1_label"] != "無資料" for v in values if v["c1"] is not None)


def test_pad_empty_headers() -> None:
    """顯示空的列／欄：資料中有、檢視中沒有標記的成員組合補上空的列。"""
    from tmig.render.missing import pad_empty

    columns = ["f_region", "month(f_month)", "sum(f_sales)", "__order(columns)"]
    rows = [("East", 1, 10.0, 1), ("East", 3, 40.0, 2), ("West", 3, 7.0, 2)]
    result = QueryResult(columns, rows)
    # 列：依值排序時補在依值應該在的位置（North 在 East、West 之間），量值是空的
    regions = [("East",), ("North",), ("West",)]
    padded = pad_empty(result, ["f_region"], regions, ROLES, leading=True)
    assert [r[0] for r in padded.rows] == ["East", "East", "North", "West"]
    north = padded.rows[2]
    assert north[2] is None and north[1] == 1 and north[3] == 1  # 其他維度沿用第一列的值
    # 欄：接在資料的最後，欄標題的名次在前後之間（2 月在 1 月與 3 月之間）
    months = [(1,), (2,), (3,), (4,)]
    padded = pad_empty(result, ["month(f_month)"], months, ROLES, leading=False)
    assert [r[:2] for r in padded.rows[3:]] == [("East", 2), ("East", 4)]
    february, april = padded.rows[3][3], padded.rows[4][3]
    assert 1 < february < 2 < april
    # 原有的組合不是依值排序時接在最後
    unsorted = QueryResult(columns, [("West", 3, 7.0, 2), ("East", 1, 10.0, 1)])
    padded = pad_empty(unsorted, ["f_region"], regions, ROLES, leading=True)
    assert [r[0] for r in padded.rows] == ["West", "East", "North"]
    # 沒有缺的組合時原樣回傳
    assert pad_empty(result, ["f_region"], [("East",), ("West",)], ROLES, leading=True) is result
