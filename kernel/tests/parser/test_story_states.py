"""故事點記下的其他狀態：排序、欄列架、註解、格式（座標軸、Null 的指示器）、頁面架目前的頁，以及篩選的
「所有的值」、排除、範圍、多個欄位的篩選動作與值組合的選取。

寫法取自 GitHub 上公開的 .twb（故事點的 <capturedDeltas>／<currentDeltas>）：computed-sort（emissions 的
basic）、natural-sort 與 text-orientation（Sheet 2）、clear-shelf-sort-v2（Profit 9）、rows 的 removed
（Feuille 2）、annotations 的 added／edited、style 的 axis／table／annotation、current-page、
datasource-dependencies、level-members 與 except、quantitative 的 in-range-or-null、crossjoin 的篩選動作、
tuple-selection。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.ir import IR, FieldUse, Sort, StorySheet
from tmig.ir.integrity import check_references
from tmig.ir.models import Axis, MapOptions, MapViewport
from tmig.planner.state import StoryRef
from tmig.story import StoryStateError, apply_story, apply_story_sheet

from . import builder as b
from .builder import instance, ref
from .test_extensions import ACTION_GROUPS, BASIC, action, action_filter, parse, reasons
from .test_story import BOARD, point, story

DEPS = [
    *BASIC,
    instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal"),
    instance("[Quantity]", "None", "[none:Quantity:qk]", "quantitative"),
]

ANNOTATIONS = f"""
  <annotations>
    <annotation class='point' id='0'>
      <formatted-text><run>Peak</run></formatted-text>
      <point><visual-coordinate class='axis'>
        <x-coord axis-value='100.0'><node-reference><axis>{ref("sum:Sales:qk")}</axis></node-reference></x-coord>
        <page-reference />
      </visual-coordinate></point>
      <body x='10' y='-10' />
    </annotation>
    <annotation class='point' id='1'>
      <formatted-text><run>Gone</run></formatted-text>
      <point><visual-coordinate class='axis'>
        <x-coord axis-value='50.0'><node-reference><axis>{ref("sum:Sales:qk")}</axis></node-reference></x-coord>
        <page-reference />
      </visual-coordinate></point>
    </annotation>
  </annotations>"""


def annotation(aid: int, delta: str, text: str, x: float = 200.0) -> str:
    return (
        f"<annotation class='point' delta-type='{delta}' id='{aid}'>"
        f"<formatted-text><run>{text}</run></formatted-text>"
        "<point><visual-coordinate class='axis'>"
        f"<x-coord axis-value='{x}'><node-reference><axis>{ref('sum:Sales:qk')}</axis>"
        "</node-reference></x-coord><page-reference /></visual-coordinate></point></annotation>"
    )


def workbook(stories: str, target_filter: str | None = None) -> str:
    """Source：各區的長條（列架上的 Category 與 Region 以座標軸排序）；Target：各類別的長條（頁面架是
    年、有註解，工作表上有篩選動作的篩選）。"""
    shelf_sort = (
        "<shelf-sorts><shelf-sort-v2 dimension-to-sort='"
        + ref("none:Region:nk")
        + "' direction='DESC' is-on-innermost-dimension='true' measure-to-sort-by='"
        + ref("sum:Sales:qk")
        + "' shelf='rows' /></shelf-sorts>"
    )
    sheets = b.worksheet(
        "Source",
        BASIC,
        rows=f"({ref('none:Category:nk')} / {ref('none:Region:nk')})",
        cols=ref("sum:Sales:qk"),
        view_extra=shelf_sort,
    ) + b.worksheet(
        "Target",
        DEPS,
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=action_filter() if target_filter is None else target_filter,
        table_extra=f"<pages><column>{ref('yr:Order Date:ok')}</column></pages>" + ANNOTATIONS,
    )
    return b.workbook(
        b.extract_datasource(extra=ACTION_GROUPS),
        worksheets=sheets,
        dashboards=BOARD + stories,
        actions=action(),
    )


def only_sheet(ir: IR) -> StorySheet:
    (state,) = ir.stories[0].points[0].sheets
    return state


def sheet(ir: IR, name: str) -> object:
    return next(w for w in ir.worksheets if w.name == name)


# ---------------------------------------------------------------- 排序與欄列架


def test_story_sorts_and_cleared_shelf_sort(tmp_path: Path) -> None:
    """排序的差異取代同一個欄位的排序；clear-shelf-sort-v2 清除欄列架的（巢狀）排序。"""
    deltas = (
        "<worksheet name='Target'>"
        f"<computed-sort column='{ref('none:Category:nk')}' direction='DESC' using='{ref('sum:Sales:qk')}' />"
        "</worksheet><worksheet name='Source'>"
        f"<natural-sort column='{ref('none:Category:nk')}' direction='DESC' />"
        "<shelf-sort-deltas><clear-shelf-sorts>"
        f"<clear-shelf-sort-v2 dimension='{ref('none:Region:nk')}' is-on-innermost-dimension='true' shelf='rows' />"
        "</clear-shelf-sorts></shelf-sort-deltas></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Sorted", "Board", deltas))))
    assert reasons(ir) == []
    target, source = ir.stories[0].points[0].sheets
    sales = FieldUse(field="f_sales", agg="sum", continuous=True)
    assert target.worksheet == "ws_target"
    assert target.sorts == [Sort(field="f_category", kind="by_field", order="desc", by=sales)]
    assert source.sorts == [Sort(field="f_category", kind="alphabetic", order="desc")]
    assert source.cleared_sorts == ["f_region"]
    assert check_references(ir) == []

    # 套用：Source 原本以座標軸排序 Region（巢狀），故事點中清除、Category 改成遞減的字母順序
    ws = next(w for w in ir.worksheets if w.id == "ws_source")
    assert [(s.field, s.kind, s.nested) for s in ws.sorts] == [("f_region", "by_field", True)]
    applied = apply_story_sheet(ws, source)
    assert [(s.field, s.kind, s.order) for s in applied.sorts] == [
        ("f_category", "alphabetic", "desc")
    ]


def test_unknown_sort_deltas_are_flagged(tmp_path: Path) -> None:
    deltas = (
        "<worksheet name='Source'><shelf-sort-deltas><add-shelf-sorts /></shelf-sort-deltas>"
        f"<sort class='mystery' column='{ref('none:Category:nk')}' /></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Odd", "Board", deltas))))
    assert reasons(ir) == [
        "Story「Tour」的故事點「Odd」記下的欄列架的排序 <add-shelf-sorts>尚不支援",
        "Story「Tour」的故事點「Odd」記下的排序（sort mystery）尚不支援",
    ]


def test_story_removes_a_field_from_the_shelf(tmp_path: Path) -> None:
    """<rows><column delta-type='removed'>：故事點中這個欄位不在列架上；新增欄位（位置不明）列為不支援。"""
    removed = (
        f"<worksheet name='Source'><rows><column delta-type='removed' field='{ref('none:Category:nk')}' />"
        "</rows></worksheet>"
    )
    added = (
        f"<worksheet name='Source'><cols><column delta-type='added' field='{ref('none:Category:nk')}' />"
        "</cols></worksheet>"
    )
    ir = parse(
        tmp_path,
        workbook(story(point(1, "Fewer", "Board", removed) + point(2, "More", "Board", added))),
    )
    assert reasons(ir) == ["Story「Tour」的故事點「More」記下的欄列架的變更（added）尚不支援"]
    state = only_sheet(ir)
    assert state.removed_rows == [FieldUse(field="f_category")]
    ws = next(w for w in ir.worksheets if w.id == "ws_source")
    assert [u.field for u in apply_story_sheet(ws, state).rows] == ["f_region"]


# ---------------------------------------------------------------- 註解、格式與頁


def test_story_annotations_are_merged_with_the_sheet(tmp_path: Path) -> None:
    """新增、修改（同 id 取代）與刪除的註解套在工作表的註解上。"""
    deltas = (
        "<worksheet name='Target'><annotations>"
        + annotation(0, "edited", "Peak (edited)")
        + annotation(1, "removed", "")
        + annotation(5, "added", "New", 300.0)
        + "</annotations></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Notes", "Target", deltas))))
    assert reasons(ir) == []
    ws = next(w for w in ir.worksheets if w.id == "ws_target")
    assert ["".join(r.text or "" for r in a.text) for a in ws.annotations] == ["Peak", "Gone"]
    state = only_sheet(ir)
    assert state.annotations is not None
    texts = ["".join(r.text or "" for r in a.text) for a in state.annotations]
    assert texts == ["Peak (edited)", "New"]
    assert [a.point.x for a in state.annotations if a.point is not None] == [200.0, 300.0]
    applied = apply_story_sheet(ws, state)
    assert applied.annotations == state.annotations


def test_story_style_changes_axes_and_null_indicator(tmp_path: Path) -> None:
    """格式的差異：座標軸的固定範圍與標題、標題成員的文字方向、「N 個 Null」的指示器；註解的外觀略過，
    其他元素列為不支援。"""
    deltas = (
        "<worksheet name='Target'><style delta-type='added'>"
        "<style-rule element='axis'>"
        f"<encoding attr='space' class='0' field='{ref('sum:Sales:qk')}' field-type='quantitative' "
        "max='1000.0' min='0.0' range-type='fixed' scope='cols' type='space' />"
        f"<format attr='title' class='0' field='{ref('sum:Sales:qk')}' scope='cols' value='Revenue' />"
        "</style-rule>"
        "<style-rule element='label'>"
        f"<format attr='text-orientation' field='{ref('none:Category:nk')}' value='-90' />"
        "</style-rule>"
        "<style-rule element='table'><format attr='show-null-value-warning' value='false' /></style-rule>"
        "<style-rule element='annotation'><format attr='fill-color' id='0' value='#ffffff' /></style-rule>"
        "<style-rule element='quick-filter'><format attr='color' value='#000000' /></style-rule>"
        "</style></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Styled", "Target", deltas))))
    assert reasons(ir) == ["Story「Tour」的故事點「Styled」記下的格式（quick-filter）尚不支援"]
    assert any("註解的外觀" in u.reason for u in ir.ignored)
    state = only_sheet(ir)
    by_field = {(a.use.field, a.shelf): a for a in state.axes}
    sales = by_field[("f_sales", "columns")]
    assert (sales.start, sales.end, sales.title) == (0.0, 1000.0, "Revenue")
    assert by_field[("f_category", "rows")].angle == -90
    assert state.null_indicator is False
    ws = next(w for w in ir.worksheets if w.id == "ws_target")
    assert ws.axes == [] and ws.null_indicator is True
    applied = apply_story_sheet(ws, state)
    assert {(a.use.field, a.start, a.end) for a in applied.axes} >= {("f_sales", 0.0, 1000.0)}
    assert applied.null_indicator is False


def test_story_current_page(tmp_path: Path) -> None:
    """<current-page>：頁面架目前的頁記在 StoryPoint.pages（檢視器的頁面狀態）。"""
    deltas = (
        "<worksheet name='Target'><current-page><multibucket><bucket>2024</bucket></multibucket>"
        "</current-page></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Year", "Target", deltas))))
    assert reasons(ir) == []
    first = ir.stories[0].points[0]
    assert first.pages == {"ws_target": 2024}
    assert first.sheets == []
    assert check_references(ir) == []
    broken = ir.model_copy(deep=True)
    broken.stories[0].points[0].pages = {"ws_source": 2024}
    assert check_references(broken) == [
        "stories[st_tour].points[sp1].pages：工作表「ws_source」沒有頁面架"
    ]


# ---------------------------------------------------------------- 篩選與選取


def test_story_filters_all_exclude_and_range(tmp_path: Path) -> None:
    """所有的值（level-members）、排除（except）、維度的範圍（in-range-or-null 保留 Null）與「全部」的範圍；
    量值（彙總）的範圍篩選不能由故事點改變。"""
    deltas = (
        "<worksheet name='Target'>"
        f"<filter class='categorical' column='{ref('none:Category:nk')}'>"
        "<groupfilter function='level-members' level='[none:Category:nk]' user:ui-enumeration='all' />"
        "</filter>"
        f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
        "<groupfilter function='except' user:ui-enumeration='exclusive'>"
        "<groupfilter function='level-members' level='[none:Region:nk]' />"
        "<groupfilter function='member' level='[none:Region:nk]' member='%null%' /></groupfilter>"
        "</filter>"
        f"<filter class='quantitative' column='{ref('yr:Order Date:ok')}' included-values='in-range-or-null'>"
        "<min>2023</min><max>2024</max></filter>"
        f"<filter class='quantitative' column='{ref('none:Quantity:qk')}' included-values='all' />"
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>10</min></filter>"
        "</worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Filters", "Target", deltas))))
    assert reasons(ir) == ["Story「Tour」的故事點「Filters」記下的篩選尚不支援"]
    filters = ir.stories[0].points[0].filters
    assert [(f.field.field, f.mode, f.values, f.min, f.max, f.include_null) for f in filters] == [
        ("f_category", "exclude", [], None, None, None),
        ("f_region", "exclude", [None], None, None, None),
        ("f_order_date", "range", [], 2023, 2024, True),
        ("f_quantity", "exclude", [], None, None, None),
    ]


def test_story_action_filters_on_two_fields_and_cleared(tmp_path: Path) -> None:
    """多個欄位的篩選動作（crossjoin）記成值組合的標記；沒有選取（level-members）時：工作表上存檔時有值的
    動作篩選改成所有的值（cleared_actions），工作表上沒有這個篩選時略過。"""
    two = (
        "<worksheet name='Target'>"
        f"<filter class='categorical' column='{ref('Action (Region)')}'>"
        "<groupfilter function='crossjoin' user:ui-action-filter='[Action1]'>"
        "<groupfilter function='member' level='[Region]' member='&quot;East&quot;' />"
        "<groupfilter function='union'>"
        "<groupfilter function='member' level='[Category]' member='&quot;Furniture&quot;' />"
        "<groupfilter function='member' level='[Category]' member='&quot;Office&quot;' />"
        "</groupfilter></groupfilter></filter></worksheet>"
    )
    cleared = (
        f"<worksheet name='Target'><filter class='categorical' column='{ref('Action (Region)')}'>"
        "<groupfilter function='level-members' level='[Region]' user:ui-action-filter='[Action1]' />"
        "</filter></worksheet>"
        f"<worksheet name='Source'><filter class='categorical' column='{ref('Action (Region)')}'>"
        "<groupfilter function='level-members' level='[Region]' /></filter></worksheet>"
    )
    saved = (
        f"<filter class='categorical' column='{ref('Action (Region)')}'>"
        "<groupfilter function='member' level='[Region]' member='&quot;West&quot;' "
        "user:ui-action-filter='[Action1]' /></filter>"
    )
    points = point(1, "Two", "Board", two) + point(2, "Cleared", "Board", cleared)
    ir = parse(tmp_path, workbook(story(points), target_filter=saved))
    assert reasons(ir) == []
    first, second = ir.stories[0].points
    assert [(a.action, a.marks) for a in first.actions] == [
        (
            "act_filter_region",
            [
                {"f_region": "East", "f_category": "Furniture"},
                {"f_region": "East", "f_category": "Office"},
            ],
        )
    ]
    assert second.actions == []
    (state,) = second.sheets
    assert (state.worksheet, state.cleared_actions) == ("ws_target", ["act_filter_region"])
    assert any("工作表上也沒有這個篩選" in u.reason for u in ir.ignored)
    ws = next(w for w in ir.worksheets if w.id == "ws_target")
    (flt,) = [f for f in apply_story_sheet(ws, state).filters if f.kind == "tuple"]
    assert (flt.mode, flt.tuples, flt.action) == ("all", [], "act_filter_region")


def test_story_tuple_selection_and_dependencies(tmp_path: Path) -> None:
    """<tuple-selection>：標記以其中的維度表示（量值略過）；只選取座標軸時不選取標記。
    <datasource-dependencies> 的欄位實例給其他差異使用。"""
    deltas = (
        "<worksheet name='Source'>"
        "<datasource-dependencies datasource='" + b.DS + "'>"
        "<column-instance column='[Order Date]' derivation='Month' name='[mn:Order Date:ok]' "
        "pivot='key' type='ordinal' /></datasource-dependencies>"
        f"<filter class='categorical' column='{ref('mn:Order Date:ok')}'>"
        "<groupfilter function='member' level='[mn:Order Date:ok]' member='3' /></filter>"
        "<selection-collection><tuple-selection><tuple-reference><tuple-descriptor>"
        f"<columns><field>{ref('none:Region:nk')}</field><field>{ref('sum:Sales:qk')}</field></columns>"
        "</tuple-descriptor><tuple><value>&quot;West&quot;</value><value>800.0</value></tuple>"
        "</tuple-reference></tuple-selection>"
        "<node-selection select-tuples='false'><oriented-node-reference orientation='horizontal'>"
        f"<node-reference><axis>{ref('sum:Sales:qk')}</axis></node-reference><page-reference />"
        "</oriented-node-reference></node-selection></selection-collection>"
        "</worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Picked", "Board", deltas))))
    assert reasons(ir) == []
    first = ir.stories[0].points[0]
    assert [(s.worksheet, s.marks) for s in first.selections] == [
        ("ws_source", [{"f_region": "West"}])
    ]
    (flt,) = first.filters
    assert (flt.field.field, flt.field.date, flt.values) == (
        "f_order_date",
        flt.field.date,
        [3],
    )
    assert flt.field.date is not None and flt.field.date.unit == "month"
    assert any("座標軸選取" in u.reason for u in ir.ignored)


# ---------------------------------------------------------------- 套用與檢查


def test_apply_story_replaces_only_the_named_worksheet(tmp_path: Path) -> None:
    deltas = (
        f"<worksheet name='Target'><computed-sort column='{ref('none:Category:nk')}' "
        f"direction='ASC' using='{ref('sum:Sales:qk')}' /></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Sorted", "Board", deltas))))
    ref_ = StoryRef(story="st_tour", point="sp1")
    applied = apply_story(ir, "ws_target", ref_)
    target = next(w for w in applied.worksheets if w.id == "ws_target")
    assert [(s.field, s.order) for s in target.sorts] == [("f_category", "asc")]
    assert apply_story(ir, "ws_source", ref_) is ir  # 故事點沒有記下 Source 的狀態
    assert apply_story(ir, "ws_target", None) is ir
    with pytest.raises(StoryStateError):
        apply_story(ir, "ws_target", StoryRef(story="st_tour", point="sp9"))


def test_apply_story_sheet_axes_and_viewport() -> None:
    """座標軸的設定取代同一個欄位、同一個架的；地圖的顯示範圍放進 map（其他地圖設定不變）。"""
    from tmig.ir import Marks, Worksheet

    sales = FieldUse(field="f_sales", agg="sum", continuous=True)
    ws = Worksheet(
        id="ws",
        name="Map",
        datasource="ds",
        marks=Marks(type="circle"),
        axes=[Axis(use=sales, shelf="columns", title="Sales"), Axis(use=sales, shelf="rows")],
        map=MapOptions(style="dark", washout=0.5),
        source_ref="/x",
    )
    state = StorySheet(
        worksheet="ws",
        axes=[Axis(use=sales, shelf="columns", start=0, end=10)],
        viewport=MapViewport(west=1, south=2, east=3, north=4),
    )
    applied = apply_story_sheet(ws, state)
    assert [(a.shelf, a.title, a.start, a.end) for a in applied.axes] == [
        ("columns", None, 0, 10),
        ("rows", None, None, None),
    ]
    assert applied.map is not None
    assert (applied.map.style, applied.map.washout, applied.map.viewport) == (
        "dark",
        0.5,
        MapViewport(west=1, south=2, east=3, north=4),
    )


def test_story_sheet_references_are_checked(tmp_path: Path) -> None:
    deltas = (
        f"<worksheet name='Target'><computed-sort column='{ref('none:Category:nk')}' "
        f"direction='ASC' using='{ref('sum:Sales:qk')}' /></worksheet>"
    )
    ir = parse(tmp_path, workbook(story(point(1, "Sorted", "Board", deltas))))
    assert check_references(ir) == []
    broken = ir.model_copy(deep=True)
    state = broken.stories[0].points[0].sheets[0]
    state.sorts[0].by = FieldUse(field="f_missing", agg="sum")
    broken.stories[0].points[0].sheets.append(StorySheet(worksheet="ws_missing"))
    assert check_references(broken) == [
        "stories[st_tour].points[sp1].sheets[ws_target].sorts：欄位「f_missing」不在資料源「ds_orders」中",
        "stories[st_tour].points[sp1].sheets：工作表「ws_missing」不存在",
    ]
