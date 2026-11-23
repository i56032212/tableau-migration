"""Story（故事）：故事點顯示的 dashboard 或工作表，以及記下的篩選動作、篩選與選取。

寫法取自公開報表：Assignment 4（篩選動作的篩選記在每張目標工作表，來源工作表記下選取）、
Practice Activity Number 4（動作刪掉重建後，工作表與故事點仍參照舊動作）、ASIA Cup Match Overview
（故事點顯示工作表、以序號導覽、自訂標題）。
"""

from __future__ import annotations

from pathlib import Path

from tmig.ir import IR, iter_zones
from tmig.planner.state import QueryState, apply_state

from . import builder as b
from .builder import ref
from .test_extensions import ACTION_GROUPS, BASIC, action, action_filter, parse, reasons


def selection(sheet: str, *regions: str) -> str:
    nodes = "".join(
        "<node-selection select-tuples='false'><oriented-node-reference orientation='vertical'>"
        f"<node-reference><fields><field>{ref('none:Region:nk')}</field></fields>"
        f"<multibucket><bucket>&quot;{region}&quot;</bucket></multibucket></node-reference>"
        "<page-reference /></oriented-node-reference></node-selection>"
        for region in regions
    )
    return f"<worksheet name='{sheet}'><selection-collection>{nodes}</selection-collection></worksheet>"


def action_delta(sheet: str, region: str, name: str = "[Action1]") -> str:
    return (
        f"<worksheet name='{sheet}'><filter class='categorical' column='{ref('Action (Region)')}'>"
        f"<groupfilter function='member' level='[Region]' member='&quot;{region}&quot;' "
        f"user:ui-action-filter='{name}' user:ui-enumeration='inclusive' /></filter></worksheet>"
    )


def category_delta(sheet: str, *categories: str) -> str:
    members = "".join(
        f"<groupfilter function='member' level='[none:Category:nk]' member='&quot;{c}&quot;' />"
        for c in categories
    )
    group = (
        f"<groupfilter function='union'>{members}</groupfilter>" if len(categories) > 1 else members
    )
    return (
        f"<worksheet name='{sheet}'><filter class='categorical' column='{ref('none:Category:nk')}'>"
        f"{group}</filter></worksheet>"
    )


def point(
    pid: int, caption: str, sheet: str, captured: str = "", current: str | None = None
) -> str:
    body = f"<capturedDeltas>{captured}</capturedDeltas>" if captured else ""
    if current is not None:
        body = f"<currentDeltas>{current}</currentDeltas>" + body
    return (
        f"<story-point caption='{caption}' captured-sheet='{sheet}' id='{pid}'>{body}</story-point>"
    )


def story(
    points: str,
    title: str = "",
    nav: str = "caption",
    arrows: bool = True,
    active: int = 1,
    title_zone: bool = True,
) -> str:
    options = (
        f"<layout-options><title><formatted-text><run>{title}</run></formatted-text></title>"
        "</layout-options>"
        if title
        else ""
    )
    title_el = (
        "<zone h='4357' id='3' type-v2='title' w='98426' x='787' y='830' />" if title_zone else ""
    )
    return f"""
    <dashboard name='Tour' type='storyboard'>
      {options}
      <size maxheight='964' maxwidth='1016' minheight='964' minwidth='1016' />
      <zones>
        <zone h='100000' id='2' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='98340' id='1' param='vert' type-v2='layout-flow' w='98426' x='787' y='830'>
            {title_el}
            <zone h='8195' id='4' paired-zone-id='5' type-v2='flipboard-nav' w='98426' x='787' y='5187' />
            <zone h='85788' id='5' paired-zone-id='4' type-v2='flipboard' w='98426' x='787' y='13382'>
              <flipboard active-id='{active}' nav-type='{nav}' show-nav-arrows='{str(arrows).lower()}'>
                <story-points>{points}</story-points>
              </flipboard>
            </zone>
          </zone>
        </zone>
      </zones>
    </dashboard>"""


BOARD = """
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' param='horz' type-v2='layout-flow' w='100000' x='0' y='0'>
          <zone h='100000' id='2' name='Source' w='50000' x='0' y='0' />
          <zone h='100000' id='3' name='Target' w='50000' x='50000' y='0' />
        </zone>
      </zones>
    </dashboard>"""


def story_workbook(
    stories: str, target_filter: str | None = None, actions: str | None = None
) -> str:
    sheets = b.worksheet(
        "Source", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    ) + b.worksheet(
        "Target",
        BASIC,
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=action_filter() if target_filter is None else target_filter,
    )
    return b.workbook(
        b.extract_datasource(extra=ACTION_GROUPS),
        worksheets=sheets,
        dashboards=BOARD + stories,
        actions=action() if actions is None else actions,
    )


def test_story_points(tmp_path: Path) -> None:
    points = "".join(
        [
            point(1, "Everything", "Board"),
            point(2, "East", "Board", action_delta("Target", "East") + selection("Source", "East")),
            point(3, "Two categories", "Target", category_delta("Target", "Furniture", "Office")),
        ]
    )
    ir = parse(tmp_path, story_workbook(story(points, title="Sales tour", active=2)))
    assert reasons(ir) == []
    (tour,) = ir.stories
    assert (tour.id, tour.name, tour.title, tour.navigation, tour.arrows, tour.active) == (
        "st_tour",
        "Tour",
        "Sales tour",
        "caption",
        True,
        "sp2",
    )
    assert (tour.size.width, tour.size.height) == (1016, 964)
    everything, east, two = tour.points
    assert (everything.id, everything.caption, everything.dashboard, everything.worksheet) == (
        "sp1",
        "Everything",
        "db_board",
        None,
    )
    assert (everything.actions, everything.filters, everything.selections) == ([], [], [])
    # 篩選動作的篩選：以動作與選取的標記記下（目標工作表依動作決定）；來源工作表的選取另外記下
    assert [(a.action, a.marks) for a in east.actions] == [
        ("act_filter_region", [{"f_region": "East"}])
    ]
    assert [(s.worksheet, s.marks) for s in east.selections] == [
        ("ws_source", [{"f_region": "East"}])
    ]
    # 工作表篩選：只保留記下的值
    assert (two.dashboard, two.worksheet) == (None, "ws_target")
    (flt,) = two.filters
    assert (flt.worksheet, flt.field.field, flt.mode, flt.values) == (
        "ws_target",
        "f_category",
        "include",
        ["Furniture", "Office"],
    )


def test_story_filter_replaces_the_sheet_filter(tmp_path: Path) -> None:
    """故事點的工作表篩選與網址參數相同：取代同一欄位的篩選。"""
    points = point(1, "Furniture", "Target", category_delta("Target", "Furniture"))
    ir = parse(tmp_path, story_workbook(story(points)))
    (flt,) = ir.stories[0].points[0].filters
    sheet = ir.worksheets[1]
    state = QueryState.model_validate(
        {"filters": {flt.field.field: {"mode": flt.mode, "values": flt.values}}}
    )
    effective = apply_state(ir, sheet, state)
    assert [(f.kind, getattr(f, "values", None)) for f in effective.filters] == [
        ("tuple", None),
        ("categorical", ["Furniture"]),
    ]


def test_one_action_recorded_on_every_target(tmp_path: Path) -> None:
    """同一個篩選動作在每張目標工作表各記一次（值相同）：只留一筆。"""
    deltas = action_delta("Target", "West") + action_delta("Source", "West")
    ir = parse(tmp_path, story_workbook(story(point(1, "West", "Board", deltas))))
    assert reasons(ir) == []
    assert [(a.action, a.marks) for a in ir.stories[0].points[0].actions] == [
        ("act_filter_region", [{"f_region": "West"}])
    ]


def test_current_deltas_win(tmp_path: Path) -> None:
    """擷取後又改過、沒有更新故事點：開啟報表時顯示改過的狀態（currentDeltas）。"""
    points = point(
        1,
        "Changed",
        "Target",
        captured=category_delta("Target", "Furniture"),
        current=category_delta("Target", "Office"),
    )
    ir = parse(tmp_path, story_workbook(story(points)))
    assert reasons(ir) == []
    assert ir.stories[0].points[0].filters[0].values == ["Office"]
    # 改回預設狀態（沒有任何差異）時 currentDeltas 是空的
    points = point(
        1, "Reverted", "Target", captured=category_delta("Target", "Furniture"), current=""
    )
    ir = parse(tmp_path, story_workbook(story(points)))
    assert ir.stories[0].points[0].filters == []


def test_worksheet_points_and_numbers(tmp_path: Path) -> None:
    """ASIA Cup 的寫法：故事點顯示工作表、沒有說明文字、以序號導覽；沒有標題區塊時不顯示標題。"""
    points = point(1, "", "Source") + point(2, "", "Target")
    ir = parse(
        tmp_path,
        story_workbook(story(points, nav="number", arrows=False, active=9, title_zone=False)),
    )
    assert reasons(ir) == []
    (tour,) = ir.stories
    assert (tour.title, tour.navigation, tour.arrows, tour.active) == (None, "number", False, None)
    assert [(p.id, p.caption, p.worksheet) for p in tour.points] == [
        ("sp1", "", "ws_source"),
        ("sp2", "", "ws_target"),
    ]
    # 有標題區塊、沒有自訂文字：以名稱為標題
    ir = parse(tmp_path, story_workbook(story(points)))
    assert ir.stories[0].title == "Tour"


def stale_workbook(stories: str, actions: str | None = None) -> str:
    """Practice Activity 的寫法：動作刪掉重建（[Action1]），目標工作表與故事點仍參照舊動作（[Action9]）。"""
    stale = action_filter("member").replace(
        "level='[Region]'", "level='[Region]' member='&quot;East&quot;'"
    )
    stale = stale.replace("[Action1]", "[Action9]")
    return story_workbook(stories, target_filter=stale, actions=actions)


def test_stale_action_is_linked_to_the_action_writing_the_sheet(tmp_path: Path) -> None:
    deltas = action_delta("Target", "West", "[Action9]") + selection("Source", "West")
    ir = parse(tmp_path, stale_workbook(story(point(1, "West", "Board", deltas))))
    assert reasons(ir) == []
    # 工作表上的篩選維持記下的值（East），選取來源的地區時由目前的動作取代
    (flt,) = ir.worksheets[1].filters
    assert flt.kind == "tuple" and (flt.tuples, flt.action) == ([["East"]], "act_filter_region")
    assert ir.actions[0].targets == ["ws_target"]
    assert [(a.action, a.marks) for a in ir.stories[0].points[0].actions] == [
        ("act_filter_region", [{"f_region": "West"}])
    ]


def test_stale_action_with_several_candidates_stays_static(tmp_path: Path) -> None:
    second = action().replace("[Action1]", "[Action2]").replace("Filter Region", "Filter Again")
    deltas = action_delta("Target", "West", "[Action9]")
    ir = parse(
        tmp_path, stale_workbook(story(point(1, "West", "Board", deltas)), action() + second)
    )
    (flt,) = ir.worksheets[1].filters
    assert flt.kind == "tuple" and flt.action is None
    assert reasons(ir) == ["Story「Tour」的故事點「West」記下的篩選動作尚不支援"]


def test_unsupported_story_states(tmp_path: Path) -> None:
    range_delta = (
        f"<worksheet name='Target'><filter column='{ref('sum:Sales:qk')}' delta-type='edited' />"
        "</worksheet>"
    )
    points = "".join(
        [
            point(1, "Gone", "Deleted Sheet"),
            point(2, "Range", "Target", range_delta),
            point(3, "Sort", "Target", "<worksheet name='Target'><sort /></worksheet>"),
            point(4, "Ghost", "Target", "<worksheet name='Ghost'><sort /></worksheet>"),
            point(5, "Param", "Target", "<parameter-value />"),
        ]
    )
    ir = parse(tmp_path, story_workbook(story(points)))
    assert reasons(ir) == [
        "Story「Tour」的故事點「Gone」顯示的「Deleted Sheet」無法移轉",
        # 認不得的篩選變更；排序缺少欄位（排序本身見 test_story_states.py）
        "Story「Tour」的故事點「Range」記下的篩選的變更（edited）尚不支援",
        "Story「Tour」的故事點「Sort」記下的排序的欄位（缺少欄位）尚不支援",
        "Story「Tour」的故事點「Ghost」記下的工作表「Ghost」的狀態尚不支援",
        "Story「Tour」的故事點「Param」記下的狀態 <parameter-value>尚不支援",
    ]
    assert [p.id for p in ir.stories[0].points] == ["sp2", "sp3", "sp4", "sp5"]


def test_story_without_points_is_flagged(tmp_path: Path) -> None:
    ir = parse(tmp_path, story_workbook(story(point(1, "Gone", "Deleted Sheet"))))
    assert reasons(ir) == [
        "Story「Tour」的故事點「Gone」顯示的「Deleted Sheet」無法移轉",
        "Story「Tour」沒有可以移轉的故事點",
    ]
    assert ir.stories == []


def check(ir: IR) -> None:
    from tmig.ir.integrity import check_references

    assert check_references(ir) == []


def test_story_references_are_checked(tmp_path: Path) -> None:
    points = point(1, "East", "Board", action_delta("Target", "East") + selection("Source", "East"))
    ir = parse(tmp_path, story_workbook(story(points)))
    check(ir)
    from tmig.ir.integrity import check_references

    broken = ir.model_copy(deep=True)
    broken.stories[0].active = "sp9"
    broken.stories[0].points[0].actions[0].action = "act_missing"
    broken.stories[0].points[0].selections[0].worksheet = "ws_missing"
    assert check_references(broken) == [
        "stories[st_tour].active：故事點「sp9」不存在",
        "stories[st_tour].points[sp1].actions：動作「act_missing」不存在",
        "stories[st_tour].points[sp1]：工作表「ws_missing」不存在",
    ]


def test_parameter_and_text_states(tmp_path: Path) -> None:
    """故事點記下的參數值（<datasource name='Parameters'>）與改過的 dashboard 文字物件（<dashboard><zone>）。"""
    board = BOARD.replace(
        "<zone h='100000' id='3' name='Target' w='50000' x='50000' y='0' />",
        "<zone h='50000' id='3' name='Target' w='50000' x='50000' y='0' />"
        "<zone h='50000' id='4' type-v2='text' w='50000' x='50000' y='50000'>"
        "<formatted-text><run>原本的說明</run></formatted-text></zone>",
    )
    deltas = (
        "<dashboard name='Board'><zone id='4'><formatted-text>"
        "<run bold='true'>東區 </run><run>&lt;[Parameters].[Parameter 2]&gt;</run>"
        "</formatted-text></zone></dashboard>"
        "<datasource name='Parameters'>"
        "<column name='[Parameter 1]' value='3' />"
        "<column name='[Parameters].[Parameter 2]' value='&quot;West&quot;' />"
        "</datasource>"
    )
    xml = story_workbook(story(point(1, "West", "Board", deltas)))
    xml = xml.replace(BOARD, board)
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    (only,) = ir.stories[0].points
    assert only.parameters == {"p_top_n": 3, "p_region_pick": "West"}
    runs = only.texts["z4"]
    assert [(r.text, r.parameter, r.style.bold if r.style else None) for r in runs] == [
        ("東區 ", None, True),
        (None, "p_region_pick", None),
    ]


def test_navigation_to_a_story(tmp_path: Path) -> None:
    """導覽按鈕與切換動作的目標可以是 Story（Story 的視窗類別也是 dashboard）。"""
    window = "00000000-0000-0000-0000-0000000000B7"
    button = (
        "<zone h='10000' id='9' type-v2='dashboard-object' w='10000' x='0' y='0'>"
        f"<button action='tabdoc:goto-sheet window-id=&quot;{{{window}}}&quot;'>"
        "<button-visual-state><caption>看故事</caption></button-visual-state></button></zone>"
    )
    board = BOARD.replace("</zone>\n      </zones>", f"{button}</zone>\n      </zones>")
    nav = (
        "<nav-action caption='Go Tour' name='[Action3]'>"
        "<activation type='on-select' />"
        "<source dashboard='Board' type='sheet' worksheet='Source' />"
        "<params><param name='target' value='Tour' /></params></nav-action>"
    )
    xml = story_workbook(story(point(1, "Everything", "Board")), actions=action() + nav)
    xml = xml.replace(BOARD, board).replace(
        "<windows />",
        f"<windows><window class='dashboard' name='Tour'><simple-id uuid='{{{window}}}' /></window></windows>",
    )
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    zones = {z.id: z for z in iter_zones(ir.dashboards[0].zones)}
    found = zones["z9"].button
    assert found is not None
    assert (found.target_story, found.target_dashboard, found.caption) == (
        "st_tour",
        None,
        "看故事",
    )
    go = next(a for a in ir.actions if a.kind == "navigate")
    assert (go.target_story, go.target_dashboard, go.target_worksheet) == ("st_tour", None, None)
