"""標題中的欄位值：與 Tableau 相同，以整張工作表計算。

- 量值是篩選後全部資料的彙總，與檢視的維度無關：KPI 卡常見的寫法是標題放合計、下面畫每月的走勢線
  （公開報表「Sales & Customer Dashboards」的 KPI Sales：Total Sales <SUM(CY Sales)>，欄架是月份）
- 維度只有一個值時顯示它（例如篩選到一個地區），多個值時顯示 *（與 ATTR 相同）
- Table Calculation 的值與量值篩選尚不支援
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import execute
from tmig.ir import IR, TextRun
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import title_values, worksheet_title
from tmig.sqlgen.query import QueryError, title_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from ..sqlgen.test_query import categorical
from .test_text import db

__all__ = ["db"]  # pytest fixture（測試資料庫）

REGION = instance("[Region]", "None", "[none:Region:nk]")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
ORDERS = instance("[Order ID]", "CountD", "[ctd:Order ID:qk]", "quantitative")
MONTH = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")
YEAR = instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal")


def titled(sheet: str, text: str) -> str:
    return sheet.replace(
        "<table>",
        f"<layout-options><title><formatted-text><run>{text}</run>"
        "</formatted-text></title></layout-options><table>",
    )


def kpi(text: str, view_extra: str = "") -> str:
    """每月的走勢線，標題放合計（KPI 卡）。"""
    sheet = worksheet(
        "KPI",
        [REGION, SALES, ORDERS, MONTH, YEAR],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=f"<lod column='{ref('ctd:Order ID:qk')}' /><lod column='{ref('none:Region:nk')}' />",
        view_extra=view_extra,
    )
    return titled(sheet, text)


def parse(tmp_path: Path, sheet: str, columns: str = "") -> IR:
    xml = b.workbook(b.extract_datasource(columns=columns), worksheets=sheet)
    return parse_workbook(b.write_twb(tmp_path, xml))


def title(ir: IR, db: Path) -> str:
    ws = ir.worksheets[0]
    main = worksheet_sql(ir, ws.id)
    queries = title_sql(ir, ws.id, main)
    if queries is None:
        return worksheet_title(ir, ws)
    measures = execute(db, queries.measures.sql) if queries.measures is not None else None
    dimensions: dict[str, Any] = {k: execute(db, q.sql) for k, q in queries.dimensions.items()}
    values = title_values(
        ir, ws, queries, measures, dimensions, (main.columns, execute(db, main.sql))
    )
    return worksheet_title(ir, ws, None, values)


def esc(token: str) -> str:
    return f"&lt;{ref(token)}&gt;"


def test_title_fields_are_parsed(tmp_path: Path) -> None:
    ir = parse(tmp_path, kpi(f"Total Sales {esc('sum:Sales:qk')} in {esc('none:Region:nk')}"))
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    sales, region = ws.rows[0], ws.marks.detail[1]
    assert ws.title == [
        TextRun(text="Total Sales "),
        TextRun(use=sales),
        TextRun(text=" in "),
        TextRun(use=region),
    ]


def test_measure_in_title_is_the_whole_view_total(tmp_path: Path, db: Path) -> None:
    """KPI 卡：標題中的 SUM(Sales) 是全部的合計，不是某個月份的值。"""
    text = f"Total Sales {esc('sum:Sales:qk')}（{esc('ctd:Order ID:qk')} orders）"
    assert title(parse(tmp_path, kpi(text)), db) == "Total Sales 1,310（7 orders）"
    east = kpi(text, categorical("none:Region:nk", ["East"]))
    assert title(parse(tmp_path, east), db) == "Total Sales 350（2 orders）"


def test_dimension_in_title_shows_its_only_value(tmp_path: Path, db: Path) -> None:
    text = f"Sales in {esc('none:Region:nk')}"
    one = kpi(text, categorical("none:Region:nk", ["West"]))
    assert title(parse(tmp_path, one), db) == "Sales in West"
    # 多個值時與 ATTR 相同顯示 *
    assert title(parse(tmp_path, kpi(text)), db) == "Sales in *"
    # 成員別名照樣套用
    alias = (
        "<column datatype='string' name='[Region]' role='dimension' type='nominal'>"
        "<aliases><alias key='&quot;West&quot;' value='西區' /></aliases></column>"
    )
    assert title(parse(tmp_path, one, alias), db) == "Sales in 西區"


def test_date_part_in_title(tmp_path: Path, db: Path) -> None:
    """公開報表「World Indicators」的寫法：Currently filtered to <YEAR(Date)>。"""
    year = (
        f"<filter class='categorical' column='{ref('yr:Order Date:ok')}'>"
        f"<groupfilter function='member' level='[yr:Order Date:ok]' member='2024' /></filter>"
    )
    text = f"Currently filtered to {esc('yr:Order Date:ok')}"
    assert title(parse(tmp_path, kpi(text, year)), db) == "Currently filtered to 2024"


def test_unsupported_title_values(tmp_path: Path) -> None:
    rank = instance(
        "[Sales]",
        "Sum",
        "[rank:sum:Sales:qk]",
        "quantitative",
        "<table-calc ordering-type='Rows' type='Rank' />",
    )
    sheet = worksheet(
        "KPI",
        [REGION, SALES, rank],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{ref('rank:sum:Sales:qk')}' />",
    )
    ir = parse(tmp_path, titled(sheet, f"Rank {esc('rank:sum:Sales:qk')}"))
    assert [u.reason for u in ir.unsupported] == [
        "工作表「KPI」的標題用到「Sales」的 Table Calculation 值，尚不支援"
    ]
    assert ir.worksheets[0].title == [TextRun(text="Rank ")]
    # 與量值篩選一起使用：Tableau 只計算通過篩選的標記
    measure_filter = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>100</min></filter>"
    )
    ir = parse(tmp_path, kpi(f"Total {esc('sum:Sales:qk')}", measure_filter))
    ws = ir.worksheets[0]
    with pytest.raises(QueryError, match="標題中的欄位值與量值篩選一起使用尚不支援"):
        title_sql(ir, ws.id, worksheet_sql(ir, ws.id))


def test_dimension_in_view_follows_measure_filters(tmp_path: Path, db: Path) -> None:
    """標題中的維度在檢視中時直接取工作表查詢結果中的值，量值篩選也照樣生效。"""
    at_least_300 = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>300</min></filter>"
    )
    # 各月 × 地區的銷售中只有 West 的兩個月份（300、500）通過篩選
    sheet = kpi(f"Sales in {esc('none:Region:nk')}", at_least_300)
    assert title(parse(tmp_path, sheet), db) == "Sales in West"
