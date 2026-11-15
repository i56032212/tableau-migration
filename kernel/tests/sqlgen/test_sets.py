"""集合的查詢：與 Tableau 的運算順序相同，Top N 與條件集合以內容篩選後、維度篩選前的資料決定成員。

測試資料見 test_query.py：各區銷售 East 350、West 800、South 100、North 60；
利潤 East 45、West 20、South 10、North 6；Office 類別只有 South 20、North 60。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import execute
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import QueryState
from tmig.sqlgen.query import QueryError, domain_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet
from ..parser.test_sets import group, member, top_n
from .test_query import CATEGORY, REGION, SALES, Run, categorical, db, run

__all__ = ["db", "run"]  # pytest fixture（測試資料庫與執行工作表查詢）

TOP = instance("[Top Regions]", "InOut", "[io:Top Regions:nk]")
PROFITABLE = instance("[Profitable]", "InOut", "[io:Profitable:nk]")
CONDITION = group(
    "Profitable",
    "<groupfilter expression='SUM([Profit]) &gt; 30' function='filter'>"
    "<groupfilter function='level-members' level='[Region]' /></groupfilter>",
)
COASTS = group(
    "Coasts",
    "<groupfilter function='union'>"
    + member("[Region]", "East")
    + member("[Region]", "West")
    + "</groupfilter>",
)


def membership(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {r["f_region"]: r[key] for r in rows}


def in_out_sheet(io: str, extra: str = "") -> str:
    """各區的銷售額，IN/OUT 放在顏色。"""
    name = "io:Top Regions:nk" if io == TOP else "io:Profitable:nk"
    return worksheet(
        "S",
        [REGION, CATEGORY, SALES, io],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<color column='{ref(name)}' />",
        view_extra=extra,
    )


def test_top_n_set(run: Run) -> None:
    rows = run(in_out_sheet(TOP), ds_extra=top_n("Top Regions"))
    assert membership(rows, "f_top_regions") == {
        "East": True,
        "West": True,
        "South": False,
        "North": False,
    }


def test_set_is_computed_before_dimension_filters(run: Run) -> None:
    """維度篩選（Office）不影響成員：仍是全部資料中銷售最高的 West、East，South、North 都是 Out。"""
    office = categorical("none:Category:nk", ["Office"])
    rows = run(in_out_sheet(TOP, office), ds_extra=top_n("Top Regions"))
    assert membership(rows, "f_top_regions") == {"South": False, "North": False}
    # 內容篩選在集合之前：Office 中銷售最高的 North（60）、South（20）
    context = categorical("none:Category:nk", ["Office"], context=True)
    rows = run(in_out_sheet(TOP, context), ds_extra=top_n("Top Regions"))
    assert membership(rows, "f_top_regions") == {"South": True, "North": True}


def test_top_n_count_from_parameter(run: Run) -> None:
    groups = top_n("Top Regions", count="[Parameters].[Parameter 1]")
    rows = run(in_out_sheet(TOP), ds_extra=groups)  # 參數預設 5：全部都在集合中
    assert set(membership(rows, "f_top_regions").values()) == {True}
    state = QueryState(parameters={"p_top_n": 1})
    rows = run(in_out_sheet(TOP), ds_extra=groups, state=state)
    assert [r["f_region"] for r in rows if r["f_top_regions"]] == ["West"]


def test_condition_set(run: Run) -> None:
    rows = run(in_out_sheet(PROFITABLE), ds_extra=CONDITION)
    assert membership(rows, "f_profitable") == {
        "East": True,
        "West": False,
        "South": False,
        "North": False,
    }


def test_member_set_on_the_filter_shelf_and_in_a_calc(run: Run) -> None:
    """集合放在篩選架上只保留 In；計算欄位中的集合是布林值。"""
    sheet = worksheet(
        "S",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=f"<filter class='categorical' column='{ref('Coasts')}' />",
    )
    rows = run(sheet, ds_extra=COASTS)
    assert sorted(r["f_region"] for r in rows) == ["East", "West"]

    coast_sales = calc("Calculation_1", "Coast Sales", "IF [Coasts] THEN [Sales] END")
    key = "sum:Calculation_1:qk"
    sheet = worksheet(
        "S",
        [CATEGORY, instance("[Calculation_1]", "Sum", f"[{key}]", "quantitative")],
        rows=ref("none:Category:nk"),
        cols=ref(key),
    )
    rows = run(sheet, calcs=coast_sales, ds_extra=COASTS)
    assert {r["f_category"]: r["sum(f_coast_sales)"] for r in rows} == {
        "Furniture": pytest.approx(450.0),
        "Technology": pytest.approx(700.0),
        "Office": None,
    }


def test_top_n_set_inside_a_calc(run: Run) -> None:
    top_sales = calc("Calculation_2", "Top Sales", "IF [Top Regions] THEN [Sales] ELSE 0 END")
    key = "sum:Calculation_2:qk"
    sheet = worksheet(
        "S",
        [CATEGORY, instance("[Calculation_2]", "Sum", f"[{key}]", "quantitative")],
        rows=ref("none:Category:nk"),
        cols=ref(key),
    )
    rows = run(sheet, calcs=top_sales, ds_extra=top_n("Top Regions"))
    assert {r["f_category"]: r["sum(f_top_sales)"] for r in rows} == {
        "Furniture": pytest.approx(450.0),
        "Technology": pytest.approx(700.0),
        "Office": pytest.approx(0.0),
    }


def parse(tmp_path: Path, sheet: str, groups: str, calcs: str = ""):
    xml = b.workbook(b.extract_datasource(columns=calcs, extra=groups), worksheets=sheet)
    return parse_workbook(b.write_twb(tmp_path, xml))


def test_sets_before_their_stage_are_unsupported(tmp_path: Path) -> None:
    """內容篩選與 FIXED LOD 在集合之前計算，用到 Top N 或條件集合時列為不支援。"""
    context = f"<filter class='categorical' column='{ref('Top Regions')}' context='true' />"
    ir = parse(tmp_path, in_out_sheet(TOP, context), top_n("Top Regions"))
    with pytest.raises(QueryError, match="集合「Top Regions」在這個運算階段還不能使用") as err:
        worksheet_sql(ir, ir.worksheets[0].id)
    assert err.value.unsupported

    fixed = calc(
        "Calculation_3", "Fixed", "{FIXED [Category] : MAX(IF [Top Regions] THEN 1 ELSE 0 END)}"
    )
    key = "sum:Calculation_3:qk"
    sheet = worksheet(
        "S",
        [CATEGORY, instance("[Calculation_3]", "Sum", f"[{key}]", "quantitative")],
        rows=ref("none:Category:nk"),
        cols=ref(key),
    )
    ir = parse(tmp_path, sheet, top_n("Top Regions"), fixed)
    with pytest.raises(QueryError, match="還不能使用"):
        worksheet_sql(ir, ir.worksheets[0].id)


def test_domain_of_a_computed_set(tmp_path: Path, db: Path) -> None:
    ir = parse(tmp_path, in_out_sheet(TOP), top_n("Top Regions"))
    use = ir.worksheets[0].marks.color
    assert use is not None
    result = execute(db, domain_sql(ir, ir.datasources[0].id, use, "values"))
    assert [row[0] for row in result.rows] == [False, True]
    with pytest.raises(QueryError, match="只有 In、Out"):
        domain_sql(ir, ir.datasources[0].id, use, "range")


# ---------------------------------------------------------------- 集合動作改變成員


def coast_sheet() -> str:
    """各區的銷售額，只保留 Coasts 中的地區。"""
    return worksheet(
        "S",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=f"<filter class='categorical' column='{ref('Coasts')}' />",
    )


def test_set_values_from_set_actions(run: Run) -> None:
    """集合動作改變後的成員只影響這次查詢：取代、全部扣掉列出的（加入所有值時為空清單）。"""
    state = QueryState.model_validate({"sets": {"f_coasts": {"members": [["South"]]}}})
    rows = run(coast_sheet(), ds_extra=COASTS, state=state)
    assert [r["f_region"] for r in rows] == ["South"]
    state = QueryState.model_validate({"sets": {"f_coasts": {"mode": "exclude", "members": []}}})
    rows = run(coast_sheet(), ds_extra=COASTS, state=state)
    assert [r["f_region"] for r in rows] == ["East", "North", "South", "West"]
    state = QueryState.model_validate(
        {"sets": {"f_coasts": {"mode": "exclude", "members": [["West"], [None]]}}}
    )
    rows = run(coast_sheet(), ds_extra=COASTS, state=state)
    assert [r["f_region"] for r in rows] == ["East", "North", "South"]
    # 其他資料源的集合略過
    state = QueryState.model_validate({"sets": {"f_elsewhere": {"members": [["X"]]}}})
    assert len(run(coast_sheet(), ds_extra=COASTS, state=state)) == 2


def test_invalid_set_values(tmp_path: Path) -> None:
    ir = parse(tmp_path, coast_sheet(), COASTS + top_n("Top Regions"))
    ws = ir.worksheets[0].id
    for sets, message in [
        ({"f_coasts": {"members": [["East", "Furniture"]]}}, "數量不同"),
        ({"f_coasts": {"members": [[float("inf")]]}}, "無法寫成公式的數值"),
    ]:
        with pytest.raises(QueryError, match=message):
            worksheet_sql(ir, ws, QueryState.model_validate({"sets": sets}))


def test_set_values_select_the_candidates_of_a_top_n_set(run: Run) -> None:
    """集合控制項改變 Top N 集合「一般」分頁的選取：先選取成員，再從中取前 N 名（原本是 West、East）。"""
    state = QueryState.model_validate(
        {
            "sets": {
                "f_top_regions": {"mode": "include", "members": [["East"], ["South"], ["North"]]}
            }
        }
    )
    rows = run(in_out_sheet(TOP), ds_extra=top_n("Top Regions"), state=state)
    assert membership(rows, "f_top_regions") == {
        "East": True,
        "West": False,
        "South": True,
        "North": False,
    }
    # 沒有排除任何成員等同「全部」：回到原本的前 N 名
    state = QueryState.model_validate(
        {"sets": {"f_top_regions": {"mode": "exclude", "members": []}}}
    )
    rows = run(in_out_sheet(TOP), ds_extra=top_n("Top Regions"), state=state)
    assert membership(rows, "f_top_regions") == {
        "East": True,
        "West": True,
        "South": False,
        "North": False,
    }
