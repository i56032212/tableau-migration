"""故事點記下的其他差異（第三批）：欄列架上加上的欄位、量值的範圍篩選、Top N 篩選的變更、移除的篩選，與
工作表上沒有欄位實例的篩選欄位。

寫法取自 GitHub 上公開的 .twb（故事點的 <capturedDeltas>／<currentDeltas>，見 README「已知限制與待驗證」）：
<columns> 的 removed＋added index='0'（fashion_retail_sales、Jian Sun_Tableau File：週改成季、月改成週）、
quantitative 的 sum:／attr:／cnt: 範圍（RIS_Strategy Outcome 等）、Top N 的 end／order（以參數為 N、
「一般」頁選了一個城市）、<filter delta-type='removed'>（International Breweries Analysis、mental health
project：篩選動作的篩選），以及資料源層級的欄位實例（Book1：none:number_of_strikes:ok）。

數字以 test_query 的資料驗證：各區銷售 East 350、West 800、South 100、North 60。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tmig.data.executor import execute
from tmig.ir import IR, FieldUse, StoryShelfField
from tmig.ir.models import DateDerivation, RangeFilter, TopNFilter
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import StoryRef
from tmig.sqlgen.query import worksheet_sql
from tmig.story import apply_story, apply_story_sheet

from ..sqlgen.test_query import db
from . import builder as b
from .builder import instance, ref
from .test_extensions import ACTION_GROUPS, action, reasons
from .test_story import BOARD, point, story

__all__ = ["db"]  # pytest fixture

DEPS = [
    instance("[Region]", "None", "[none:Region:nk]"),
    instance("[Category]", "None", "[none:Category:nk]"),
    instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
    instance("[Order Date]", "Week-Trunc", "[twk:Order Date:qk]", "quantitative"),
]
QUARTER = instance("[Order Date]", "Quarter-Trunc", "[tqr:Order Date:qk]", "quantitative")
DS_INSTANCE = "<column-instance column='[Quantity]' derivation='None' name='[none:Quantity:ok]' pivot='key' type='ordinal' />"
SALES_RANGE = (
    f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
    "<min>0</min></filter>"
)
TOP_REGIONS = (
    f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
    "<groupfilter count='2' end='top' function='end' units='records' user:ui-marker='end' "
    "user:ui-top-by-field='true'>"
    "<groupfilter direction='DESC' expression='SUM([Sales])' function='order' user:ui-marker='order'>"
    "<groupfilter function='level-members' level='[none:Region:nk]' user:ui-enumeration='all' />"
    "</groupfilter></groupfilter></filter>"
)
CATEGORY_FILTER = (
    f"<filter class='categorical' column='{ref('none:Category:nk')}'>"
    "<groupfilter function='member' level='[none:Category:nk]' member='&quot;Furniture&quot;' />"
    "</filter>"
)
SAVED_ACTION = (
    f"<filter class='categorical' column='{ref('Action (Region)')}'>"
    "<groupfilter function='member' level='[Region]' member='&quot;West&quot;' "
    "user:ui-action-filter='[Action1]' /></filter>"
)


def workbook(stories: str) -> str:
    """Source：各區的長條；Target：各區的長條（量值的範圍篩選、Top 2 的地區、類別的篩選與篩選動作的
    篩選）；Weekly：各週的折線。"""
    sheets = (
        b.worksheet("Source", DEPS, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
        + b.worksheet(
            "Target",
            DEPS,
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            view_extra=SALES_RANGE + TOP_REGIONS + CATEGORY_FILTER + SAVED_ACTION,
        )
        + b.worksheet(
            "Weekly", DEPS, rows=ref("sum:Sales:qk"), cols=ref("twk:Order Date:qk"), mark="Line"
        )
    )
    return b.workbook(
        b.extract_datasource(extra=ACTION_GROUPS + DS_INSTANCE),
        worksheets=sheets,
        dashboards=BOARD + stories,
        actions=action(),
    )


def parse(tmp_path: Path, xml: str) -> IR:
    return parse_workbook(b.write_twb(tmp_path, xml))


def sheet(ir: IR, name: str) -> Any:
    return next(w for w in ir.worksheets if w.name == name)


def rows(ir: IR, db: Path, name: str, point_id: str = "sp1") -> dict[str, Any]:
    """以故事點查詢工作表（與查詢服務相同：apply_story 之後查詢）：地區 → SUM(Sales)。"""
    ws = sheet(ir, name)
    applied = apply_story(ir, ws.id, StoryRef(story=ir.stories[0].id, point=point_id))
    result = execute(db, worksheet_sql(applied, ws.id).sql)
    return {r["f_region"]: r["sum(f_sales)"] for r in result.records()}


# ---------------------------------------------------------------- 欄列架


def test_story_adds_a_field_at_the_index(tmp_path: Path) -> None:
    """<columns>：移除週、在位置 0 加上季（故事點中改了日期的層級）。"""
    deltas = (
        "<worksheet name='Weekly'>"
        f"<datasource-dependencies datasource='{b.DS}'>{QUARTER}</datasource-dependencies>"
        "<columns>"
        f"<column delta-type='removed' field='{ref('twk:Order Date:qk')}' />"
        f"<column delta-type='added' field='{ref('tqr:Order Date:qk')}' index='0' />"
        "</columns></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Quarterly", "Weekly", deltas))))
    assert reasons(ir) == []
    (state,) = ir.stories[0].points[0].sheets
    quarter = FieldUse(
        field="f_order_date", date=DateDerivation(kind="trunc", unit="quarter"), continuous=True
    )
    assert state.added_columns == [StoryShelfField(use=quarter, index=0)]
    applied = apply_story_sheet(sheet(ir, "Weekly"), state)
    assert applied.columns == [quarter]
    assert applied.rows == sheet(ir, "Weekly").rows


def test_added_field_position_and_unsupported_additions(tmp_path: Path) -> None:
    """位置超過架上的欄位數時加在最後；已在架上的不重複加；工作表沒有的 Table Calculation 欄位不支援。"""
    deltas = (
        "<worksheet name='Source'>"
        f"<datasource-dependencies datasource='{b.DS}'>"
        + instance(
            "[Sales]",
            "Sum",
            "[pcto:sum:Sales:qk]",
            "quantitative",
            "<table-calc ordering-type='Columns' type='PctTotal' />",
        )
        + "</datasource-dependencies><rows>"
        f"<column delta-type='added' field='{ref('none:Category:nk')}' index='5' />"
        f"<column delta-type='added' field='{ref('none:Region:nk')}' index='0' />"
        f"<column delta-type='added' field='{ref('pcto:sum:Sales:qk')}' index='0' />"
        "</rows></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "More", "Board", deltas))))
    assert reasons(ir) == [
        "Story「Tour」的故事點「More」記下的加在欄列架上、工作表沒有的 Table Calculation 欄位尚不支援"
    ]
    (state,) = ir.stories[0].points[0].sheets
    applied = apply_story_sheet(sheet(ir, "Source"), state)
    assert [u.field for u in applied.rows] == ["f_region", "f_category"]


# ---------------------------------------------------------------- 篩選


def test_story_measure_range_replaces_the_sheet_filter(tmp_path: Path, db: Path) -> None:
    """量值（SUM(Sales)）的範圍：取代工作表上的範圍篩選（沿用它的 id）。工作表：Top 2 的地區（West 800、
    East 350）中 Furniture 的銷售（West 300、East 150）；故事點只留 SUM(Sales) 在 200 到 1000 之間的。"""
    deltas = (
        "<worksheet name='Target'>"
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>200.0</min><max>1000.0</max></filter></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Big", "Board", deltas))))
    assert reasons(ir) == []
    (state,) = ir.stories[0].points[0].sheets
    (flt,) = state.filters
    assert isinstance(flt, RangeFilter)
    assert (flt.stage, flt.min, flt.max) == ("measure", 200.0, 1000.0)
    target = sheet(ir, "Target")
    original = next(f for f in target.filters if isinstance(f, RangeFilter))
    replaced = next(
        f for f in apply_story_sheet(target, state).filters if isinstance(f, RangeFilter)
    )
    assert (replaced.id, replaced.min, replaced.max) == (original.id, 200.0, 1000.0)
    assert rows(ir, db, "Target", "sp1") == {"West": 300.0}


def test_story_top_n_change_and_new_filter(tmp_path: Path, db: Path) -> None:
    """Top N 的變更（N 改成 1、「一般」頁只選 East）以工作表的寫法解析，取代工作表上的 Top N；工作表上
    沒有的量值篩選（ATTR、COUNT 也一樣）加在工作表上。"""
    deltas = (
        "<worksheet name='Target'>"
        f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
        "<groupfilter count='1' end='top' function='end' units='records' user:ui-marker='end' "
        "user:ui-top-by-field='true'>"
        "<groupfilter direction='DESC' expression='SUM([Sales])' function='order' user:ui-marker='order'>"
        "<groupfilter function='member' level='[none:Region:nk]' member='&quot;East&quot;' "
        "user:ui-enumeration='inclusive' />"
        "</groupfilter></groupfilter></filter>"
        f"<filter column='{ref('Action (Region)')}' delta-type='removed' /></worksheet>"
        "<worksheet name='Source'>"
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<max>200.0</max></filter></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "East", "Board", deltas))))
    assert reasons(ir) == []
    target, source = ir.stories[0].points[0].sheets
    (top,) = target.filters
    assert isinstance(top, TopNFilter)
    assert (top.count, top.mode, top.values) == (1, "include", ["East"])
    assert rows(ir, db, "Target") == {"East": 150.0}
    assert len(apply_story_sheet(sheet(ir, "Source"), source).filters) == 1
    assert rows(ir, db, "Source") == {"South": 100.0, "North": 60.0}


def test_story_removed_filters(tmp_path: Path, db: Path) -> None:
    """<filter delta-type='removed'>：故事點中沒有這個篩選——類別的篩選從工作表移除，篩選動作的篩選與沒有
    選取時相同（顯示所有的值）；工作表上沒有的略過。"""
    deltas = (
        "<worksheet name='Target'>"
        f"<filter column='{ref('none:Category:nk')}' delta-type='removed' />"
        f"<filter column='{ref('Action (Region)')}' delta-type='removed' />"
        "</worksheet><worksheet name='Source'>"
        f"<filter column='{ref('none:Category:nk')}' delta-type='removed' />"
        "</worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "All", "Board", deltas))))
    assert reasons(ir) == []
    (state,) = ir.stories[0].points[0].sheets
    target = sheet(ir, "Target")
    category = next(f for f in target.filters if f.kind == "categorical")
    assert state.removed_filters == [category.id]
    assert state.cleared_actions == ["act_filter_region"]
    assert any("故事點移除的篩選不在工作表上" in u.reason for u in ir.ignored)
    applied = apply_story_sheet(target, state)
    assert all(f.id != category.id for f in applied.filters)
    (action_filter,) = [f for f in applied.filters if f.kind == "tuple"]
    assert (action_filter.mode, action_filter.tuples) == ("all", [])
    # 工作表：Top 2 的地區中 Furniture、篩選動作存檔時是 West；故事點：Top 2 的地區的全部銷售
    assert rows(ir, db, "Target") == {"West": 800.0, "East": 350.0}


def test_story_filter_on_a_datasource_instance(tmp_path: Path) -> None:
    """篩選的欄位實例只在資料源層級（工作表上沒有，例如 none:Quantity:ok）：照常記成故事點的篩選。"""
    deltas = (
        "<worksheet name='Source'>"
        f"<filter class='categorical' column='{ref('none:Quantity:ok')}'>"
        "<groupfilter function='union'>"
        "<groupfilter function='member' level='[none:Quantity:ok]' member='1' />"
        "<groupfilter function='member' level='[none:Quantity:ok]' member='2' />"
        "</groupfilter></filter></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Few", "Board", deltas))))
    assert reasons(ir) == []
    (flt,) = ir.stories[0].points[0].filters
    assert (flt.worksheet, flt.field, flt.mode, flt.values) == (
        sheet(ir, "Source").id,
        FieldUse(field="f_quantity"),
        "include",
        [1, 2],
    )
