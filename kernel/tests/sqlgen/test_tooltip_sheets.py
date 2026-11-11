"""工具提示中的工作表（Viz in Tooltip）：來源工作表的工具提示文字中的 <Sheet name=…>，目標工作表上
Tableau 自動產生的工具提示篩選；在工具提示中以滑過的標記篩選目標工作表。測試資料見 test_query。"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from tmig.data.executor import execute
from tmig.ir import IR, DateDerivation, FieldUse, TooltipSheet, TupleFilter
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import QueryState, StateError, TooltipMark
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import ref, worksheet
from .test_query import CATEGORY, REGION, SALES, YEAR, db

__all__ = ["db"]  # pytest fixture

GROUP = """
      <group caption='Tooltip (Source)' hidden='true' name='[Tooltip (Source)]' name-style='unqualified'
             user:auto-column='sheet_link' user:ui-vit-column='true'>
        <groupfilter function='crossjoin'>
          <groupfilter function='level-members' level='[Region]' />
        </groupfilter>
      </group>"""
TARGET_FILTER = f"""
      <filter class='categorical' column='{ref("Tooltip (Source)")}'>
        <groupfilter function='crossjoin' user:ui-enumeration='all' user:ui-marker='enumerate'>
          <groupfilter function='level-members' level='[Region]' />
        </groupfilter>
      </filter>"""


def tooltip(name: str = "Target", filter_: str = "<All Fields>", size: str = "300") -> str:
    tag = f'<Sheet name="{name}" maxwidth="{size}" maxheight="200" filter="{filter_}">'
    return f"""
        <customized-tooltip>
          <formatted-text>
            <run>Region: </run>
            <run>{escape("<" + ref("none:Region:nk") + ">")}</run>
            <run>Æ&#10;</run>
            <run>{escape(tag)}</run>
          </formatted-text>
        </customized-tooltip>"""


def parsed(
    tmp_path: Path,
    tip: str,
    rows: str = "",
    dashboards: str = "",
) -> IR:
    source = worksheet(
        "Source",
        [REGION, SALES, YEAR],
        rows=rows or ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        pane_extra=tip,
    )
    target = worksheet(
        "Target",
        [CATEGORY, SALES],
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=TARGET_FILTER,
    )
    xml = b.workbook(
        b.extract_datasource(extra=GROUP), worksheets=source + target, dashboards=dashboards
    )
    return parse_workbook(b.write_twb(tmp_path, xml))


def categories(ir: IR, db: Path, state: QueryState | None = None) -> dict[str, float]:
    target = next(w for w in ir.worksheets if w.name == "Target")
    result = execute(db, worksheet_sql(ir, target.id, state).sql)
    return {r[0]: r[1] for r in result.rows}


def test_tooltip_sheet_filters_target_by_hovered_mark(tmp_path: Path, db: Path) -> None:
    ir = parsed(tmp_path, tooltip(size="320"))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert ir.ignored == []
    source, target = ir.worksheets
    assert source.marks.tooltip_sheets == [
        TooltipSheet(worksheet=target.id, max_width=320, max_height=200)
    ]
    (flt,) = target.filters
    assert isinstance(flt, TupleFilter) and flt.tooltip and flt.mode == "all"
    # 直接檢視：顯示全部
    assert categories(ir, db) == {"Furniture": 530.0, "Office": 80.0, "Technology": 700.0}
    # 滑過 West 的標記：只有 West 的資料
    state = QueryState(tooltip=TooltipMark(source=source.id, mark={"f_region": "West"}))
    assert categories(ir, db, state) == {"Furniture": 300.0, "Technology": 500.0}
    # 標記的量值等不是維度的欄位鍵不影響
    state = QueryState(
        tooltip=TooltipMark(source=source.id, mark={"f_region": "East", "sum(f_sales)": 350.0})
    )
    assert categories(ir, db, state) == {"Furniture": 150.0, "Technology": 200.0}


def test_selected_fields(tmp_path: Path, db: Path) -> None:
    """filter 屬性列出的欄位（日期含層級）：只以這些欄位篩選。"""
    rows = f"({ref('none:Region:nk')} / {ref('yr:Order Date:ok')})"
    ir = parsed(tmp_path, tooltip(filter_="<YEAR(Order Date)>"), rows=rows)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    source = ir.worksheets[0]
    (tip,) = source.marks.tooltip_sheets
    assert tip.fields == [
        FieldUse(field="f_order_date", date=DateDerivation(kind="part", unit="year"))
    ]
    mark = {"f_region": "West", "year(f_order_date)": 2024}
    state = QueryState(tooltip=TooltipMark(source=source.id, mark=mark))
    # 2024 年的所有地區（不以 Region 篩選）
    assert categories(ir, db, state) == {"Furniture": 80.0, "Office": 80.0, "Technology": 500.0}


def test_unknown_filter_field_is_flagged(tmp_path: Path) -> None:
    ir = parsed(tmp_path, tooltip(filter_="<Category>"))
    assert [u.reason for u in ir.unsupported] == [
        "工作表「Source」工具提示中的工作表「Target」以「Category」篩選，但這個欄位不在檢視上"
    ]
    assert ir.worksheets[0].marks.tooltip_sheets == []


def test_dashboard_or_missing_sheet(tmp_path: Path) -> None:
    board = """
      <dashboard name='Board'>
        <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' />
        <zones><zone h='100000' id='1' name='Target' w='100000' x='0' y='0' /></zones>
      </dashboard>"""
    ir = parsed(tmp_path, tooltip(name="Board"), dashboards=board)
    assert [u.reason for u in ir.unsupported] == [
        "工作表「Source」的工具提示中放了 dashboard 或 Story「Board」，只能放工作表"
    ]
    ir = parsed(tmp_path, tooltip(name="Gone"))
    assert ir.unsupported == []
    assert [u.reason for u in ir.ignored] == [
        "工作表「Source」工具提示中的工作表「Gone」不存在，Tableau 不顯示"
    ]
    assert ir.worksheets[0].marks.tooltip_sheets == []


def test_tooltip_state_must_match_a_tooltip_sheet(tmp_path: Path) -> None:
    ir = parsed(tmp_path, tooltip())
    source, target = ir.worksheets
    state = QueryState(tooltip=TooltipMark(source=target.id, mark={"f_region": "West"}))
    with pytest.raises(StateError, match="不在"):
        worksheet_sql(ir, source.id, state)
