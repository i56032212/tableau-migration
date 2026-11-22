"""標題中的 Table Calculation 值，與標題中的欄位值和量值篩選一起使用。

- Table Calculation 依檢視的計算方向計算，取各標記的值：Tableau 知識庫「Adding Totals to a Title」
  （001458099）的寫法——在每個標記重複的合計（TOTAL()、WINDOW_SUM()）放在標記卡上，再插入標題。各標記的值
  都相同時顯示它；不同時 Tableau 顯示這些值的範圍，寫法無從確認，列為不支援
- 與量值篩選一起使用：標題中的量值與不在檢視中的維度只計算通過篩選的標記（與總計相同的做法，推定）

資料見 tests/sqlgen/test_query.py 的 ROWS（全部 1,310；West 300 與 400＋100）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.data.executor import execute
from tmig.ir import IR
from tmig.render.vegalite import title_values, worksheet_title
from tmig.sqlgen.query import QueryError, title_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_text import db
from .test_titles import MONTH, ORDERS, REGION, SALES, esc, parse, titled

__all__ = ["db"]  # pytest fixture（測試資料庫）

CATEGORY = instance("[Category]", "None", "[none:Category:nk]")
TOTAL = instance(
    "[Calculation_total]",
    "User",
    "[usr:Calculation_total:qk]",
    "quantitative",
    "<table-calc ordering-type='Columns' />",
)
RANK = instance(
    "[Sales]",
    "Sum",
    "[rank:sum:Sales:qk]",
    "quantitative",
    "<table-calc ordering-type='Columns' type='Rank' />",
)
TOTAL_CALC = b.calc("Calculation_total", "Total Sales", "WINDOW_SUM(SUM([Sales]))")


def render_title(ir: IR, db: Path) -> str:
    ws = ir.worksheets[0]
    main = worksheet_sql(ir, ws.id)
    result = execute(db, main.sql)
    queries = title_sql(ir, ws.id, main, marks=result)
    assert queries is not None
    measures = execute(db, queries.measures.sql) if queries.measures is not None else None
    dimensions = {k: execute(db, q.sql) for k, q in queries.dimensions.items()}
    values = title_values(ir, ws, queries, measures, dimensions, (main.columns, result))
    return worksheet_title(ir, ws, None, values)


def by_region(title: str, extra: list[str], encodings: str) -> str:
    """各地區的銷售（地區在列上，計算方向為表格（向下））。"""
    sheet = worksheet(
        "Regions",
        [REGION, SALES, *extra],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=encodings,
    )
    return titled(sheet, title)


def test_table_calculation_repeated_on_every_mark(tmp_path: Path, db: Path) -> None:
    """WINDOW_SUM(SUM([Sales])) 在標記卡上（表格向下：每個地區都是 1,310），標題顯示這個值。"""
    sheet = by_region(
        f"Sales {esc('usr:Calculation_total:qk')}",
        [TOTAL],
        f"<lod column='{ref('usr:Calculation_total:qk')}' />",
    )
    ir = parse(tmp_path, sheet, TOTAL_CALC)
    assert ir.unsupported == []
    assert render_title(ir, db) == "Sales 1,310"


def test_table_calculation_varying_by_mark_is_unsupported(tmp_path: Path, db: Path) -> None:
    """排名在各地區不同：Tableau 顯示範圍（寫法未確認），列為不支援。"""
    sheet = by_region(
        f"Rank {esc('rank:sum:Sales:qk')}", [RANK], f"<text column='{ref('rank:sum:Sales:qk')}' />"
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    with pytest.raises(QueryError, match="在各標記的值不同") as info:
        render_title(ir, db)
    assert info.value.unsupported


def test_title_values_with_measure_filter(tmp_path: Path, db: Path) -> None:
    """每月 × 地區的 KPI 走勢線，量值篩選 SUM(Sales) ≥ 400：只有 West 2024 年 1 月（500）通過。標題中的
    合計與訂單數只算這個標記（不是全部的 1,310），不在檢視中的 Category 也只看這個標記的資料。"""
    at_least_400 = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>400</min></filter>"
    )
    sheet = worksheet(
        "KPI",
        [REGION, SALES, ORDERS, MONTH, CATEGORY],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=f"<lod column='{ref('ctd:Order ID:qk')}' /><lod column='{ref('none:Region:nk')}' />",
        view_extra=at_least_400,
    )
    text = (
        f"Total {esc('sum:Sales:qk')}（{esc('ctd:Order ID:qk')} orders）"
        f" {esc('none:Region:nk')} {esc('none:Category:nk')}"
    )
    ir = parse(tmp_path, titled(sheet, text))
    assert ir.unsupported == []
    assert render_title(ir, db) == "Total 500（1 orders） West Technology"
