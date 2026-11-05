"""類別篩選中連續的成員：在篩選清單中連續勾選時，Tableau 記成 range（from–to，含頭尾）。

寫法取自公開報表：Logshark 的 Art（union(member %null%, range)）、Sales Report Week 5
（except(level-members, range) 排除 4–7 月；except(level-members, except(level-members, …)) 等於只保留）、
Netflix Trends Analysis（只有 range 的年份）。測試資料見 test_query.py（地區 East、North、South、West）。
"""

from __future__ import annotations

from pathlib import Path

from tmig.ir import CategoricalFilter, MemberRange
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import QueryState, apply_state

from ..parser import builder as b
from ..parser.builder import ref
from .test_query import Run, db, region_sheet, run

__all__ = ["db", "run"]  # pytest fixture（測試資料庫與執行工作表查詢）


def member(level: str, value: str) -> str:
    return f"<groupfilter function='member' level='[{level}]' member='{value}' />"


def span(level: str, start: str, end: str) -> str:
    return f"<groupfilter from='{start}' function='range' level='[{level}]' to='{end}' />"


def region_filter(body: str) -> str:
    return f"<filter class='categorical' column='{ref('none:Region:nk')}'>{body}</filter>"


REGIONS = "none:Region:nk"
ALL = f"<groupfilter function='level-members' level='[{REGIONS}]' />"


def regions(rows: list[dict[str, object]]) -> list[str]:
    return [str(r["f_region"]) for r in rows]


def test_ranges_in_categorical_filters(run: Run) -> None:
    # 只有 range：East 到 South（依值的大小比較，含頭尾）
    sheet = region_sheet(
        view_extra=region_filter(span(REGIONS, "&quot;East&quot;", "&quot;South&quot;"))
    )
    assert regions(run(sheet)) == ["East", "North", "South"]
    # 成員與 range 的聯集
    body = (
        "<groupfilter function='union'>"
        + member(REGIONS, "&quot;West&quot;")
        + span(REGIONS, "&quot;East&quot;", "&quot;North&quot;")
        + "</groupfilter>"
    )
    assert regions(run(region_sheet(view_extra=region_filter(body)))) == ["East", "North", "West"]
    # 排除 range
    body = f"<groupfilter function='except'>{ALL}{span(REGIONS, '&quot;North&quot;', '&quot;South&quot;')}</groupfilter>"
    assert regions(run(region_sheet(view_extra=region_filter(body)))) == ["East", "West"]
    # 排除「全部扣掉 East、West」＝只保留 East、West
    inner = (
        f"<groupfilter function='except'>{ALL}<groupfilter function='union'>"
        + member(REGIONS, "&quot;East&quot;")
        + member(REGIONS, "&quot;West&quot;")
        + "</groupfilter></groupfilter>"
    )
    body = f"<groupfilter function='except'>{ALL}{inner}</groupfilter>"
    assert regions(run(region_sheet(view_extra=region_filter(body)))) == ["East", "West"]


def test_range_of_date_parts(run: Run) -> None:
    """日期部分的 range（Sales Report Week 5 的月份、Netflix 的年份）。"""
    years = (
        f"<filter class='categorical' column='{ref('yr:Order Date:ok')}'>"
        f"{span('yr:Order Date:ok', '2024', '2030')}</filter>"
    )
    sheet = region_sheet(view_extra=years)
    rows = run(sheet)
    assert sorted(regions(rows)) == ["North", "South", "West"]


def test_ranges_are_parsed_and_replaced(tmp_path: Path) -> None:
    body = (
        "<groupfilter function='union'>"
        + member(REGIONS, "%null%")
        + span(REGIONS, "&quot;East&quot;", "&quot;North&quot;")
        + "</groupfilter>"
    )
    xml = b.workbook(
        b.extract_datasource(), worksheets=region_sheet(view_extra=region_filter(body))
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    (flt,) = ws.filters
    assert isinstance(flt, CategoricalFilter)
    assert (flt.mode, flt.values, flt.ranges) == (
        "include",
        [None],
        [MemberRange(start="East", end="North")],
    )
    # 篩選控制項或網址參數改了這個篩選：以新的值取代，不留原本的 range
    state = QueryState.model_validate({"filters": {"f_region": {"values": ["West"]}}})
    (changed,) = apply_state(ir, ws, state).filters
    assert isinstance(changed, CategoricalFilter)
    assert (changed.values, changed.ranges) == (["West"], [])
