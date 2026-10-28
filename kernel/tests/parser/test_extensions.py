"""擴充階段的解析（H1–H4、I1、J1–J3、K1、L1）。"""

from __future__ import annotations

from pathlib import Path

from tmig.ir import IR, CategoricalFilter, iter_zones
from tmig.parser.workbook import parse_workbook

from . import builder as b
from .builder import DS, instance, ref, worksheet


def parse(tmp_path: Path, xml: str, **kwargs: bool) -> IR:
    return parse_workbook(b.write_twb(tmp_path, xml), **kwargs)


def reasons(ir: IR) -> list[str]:
    return [u.reason for u in ir.unsupported]


BASIC = [
    instance("[Region]", "None", "[none:Region:nk]"),
    instance("[Category]", "None", "[none:Category:nk]"),
    instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
]


def slices(*instances: str) -> str:
    return "<slices>" + "".join(f"<column>{ref(i)}</column>" for i in instances) + "</slices>"


# ---------------------------------------------------------------- H1 全域篩選與篩選群組


SHARED = f"""
  <shared-views>
    <shared-view name='{DS}'>
      <datasources><datasource caption='Orders' name='{DS}' /></datasources>
      <datasource-dependencies datasource='{DS}'>
        <column datatype='string' name='[Region]' role='dimension' type='nominal' />
        {instance("[Region]", "None", "[none:Region:nk]")}
        {instance("[Category]", "None", "[none:Category:nk]")}
      </datasource-dependencies>
      <filter class='categorical' column='{ref("none:Region:nk")}'>
        <groupfilter function='except' user:ui-enumeration='exclusive'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
          <groupfilter function='member' level='[none:Region:nk]' member='&quot;Central&quot;' />
        </groupfilter>
      </filter>
      <filter class='categorical' column='{ref("none:Category:nk")}'>
        <groupfilter function='level-members' level='[none:Category:nk]' />
      </filter>
    </shared-view>
  </shared-views>"""


def test_shared_view_filters_apply_through_slices(tmp_path: Path) -> None:
    sheets = worksheet(
        "With Region",
        BASIC,
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=slices("none:Region:nk"),
    ) + worksheet("No Slices", BASIC, rows=ref("none:Category:nk"), cols=ref("sum:Sales:qk"))
    ir = parse(tmp_path, b.workbook(worksheets=sheets, extra=SHARED))
    assert reasons(ir) == []
    with_region, no_slices = ir.worksheets
    (flt,) = with_region.filters
    assert isinstance(flt, CategoricalFilter)
    assert (flt.field.field, flt.mode, flt.values) == ("f_region", "exclude", ["Central"])
    assert flt.stage == "dimension"
    assert flt.group == "shared:ds_orders:f_region"
    assert no_slices.filters == []


def test_local_filter_overrides_shared_filter(tmp_path: Path) -> None:
    local = f"""<filter class='categorical' column='{ref("none:Region:nk")}' filter-group='3'>
          <groupfilter function='member' level='[none:Region:nk]' member='&quot;East&quot;' />
        </filter>"""
    sheets = worksheet(
        "Local",
        BASIC,
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=local + slices("none:Region:nk", "none:Category:nk"),
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets, extra=SHARED))
    region, category = ir.worksheets[0].filters
    assert isinstance(region, CategoricalFilter)
    assert (region.values, region.group) == (["East"], "group:3")
    assert isinstance(category, CategoricalFilter)
    assert (category.mode, category.group) == ("all", "shared:ds_orders:f_category")


def test_shared_filter_failure_is_reported_once(tmp_path: Path) -> None:
    shared = SHARED.replace(
        f"<filter class='categorical' column='{ref('none:Category:nk')}'>\n        <groupfilter function='level-members' level='[none:Category:nk]' />\n      </filter>",
        f"<filter class='relative-date' column='{ref('none:Category:nk')}' />",
    )
    assert shared != SHARED
    sheets = "".join(
        worksheet(
            name,
            BASIC,
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            view_extra=slices("none:Category:nk"),
        )
        for name in ("A", "B")
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets, extra=shared))
    assert reasons(ir) == ["相對日期篩選 v0 不支援"]


def test_dashboard_control_for_shared_filter(tmp_path: Path) -> None:
    sheets = worksheet(
        "With Region",
        BASIC,
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=slices("none:Region:nk"),
    )
    board = f"""
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='50000' id='2' name='With Region' w='100000' x='0' y='0' />
          <zone h='50000' id='3' mode='checkdropdown' name='With Region' param='{ref("none:Region:nk")}' type-v2='filter' w='100000' x='0' y='50000' />
        </zone>
      </zones>
    </dashboard>"""
    ir = parse(tmp_path, b.workbook(worksheets=sheets, dashboards=board, extra=SHARED))
    assert reasons(ir) == []
    control = next(z for z in iter_zones(ir.dashboards[0].zones) if z.id == "z3")
    assert control.kind == "filter"
    assert control.field is not None and control.field.field == "f_region"


# ---------------------------------------------------------------- H2 資料表筆數欄位

TABLE = "[__tableau_internal_object_id__].[Orders_ABC123]"
COUNT = instance(TABLE, "Count", "[__tableau_internal_object_id__].[cnt:Orders_ABC123:qk]")
COUNT_REF = ref("__tableau_internal_object_id__].[cnt:Orders_ABC123:qk")


def test_table_count_from_object_graph(tmp_path: Path) -> None:
    graph = "<object-graph><objects><object caption='Orders' id='Orders_ABC123' /></objects></object-graph>"
    sheets = worksheet("Counts", [*BASIC, COUNT], rows=ref("none:Region:nk"), cols=COUNT_REF)
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extra=graph), worksheets=sheets))
    assert reasons(ir) == []
    count = next(f for f in ir.datasources[0].fields if f.name == TABLE)
    assert (count.id, count.caption, count.role, count.datatype) == (
        "f_orders_count",
        "Orders",
        "measure",
        "integer",
    )
    assert count.calc is not None and count.calc.formula == "1"
    (use,) = ir.worksheets[0].columns
    assert (use.field, use.agg) == ("f_orders_count", "count")


def test_table_count_from_table_column(tmp_path: Path) -> None:
    column = f"<column caption='Orders' datatype='table' name='{TABLE}' role='measure' type='quantitative' />"
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=column)))
    assert reasons(ir) == []
    assert any(f.name == TABLE and f.caption == "Orders" for f in ir.datasources[0].fields)


# ---------------------------------------------------------------- H3 不影響數字的元素


def test_blank_worksheet_becomes_blank_zone(tmp_path: Path) -> None:
    blank = """
    <worksheet name='Spacer'>
      <table>
        <view><datasources /><aggregation value='true' /></view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Automatic' /></pane></panes>
        <rows /><cols />
      </table>
    </worksheet>"""
    board = """
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones><zone h='100000' id='2' name='Spacer' w='100000' x='0' y='0' /></zones>
    </dashboard>"""
    ir = parse(tmp_path, b.workbook(worksheets=blank, dashboards=board))
    assert reasons(ir) == []
    assert ir.worksheets == []
    (zone,) = ir.dashboards[0].zones
    assert zone.kind == "blank"
    assert any("空白工作表" in u.reason for u in ir.ignored)


def test_mark_labels_and_semantic_values_do_not_block(tmp_path: Path) -> None:
    deps = "<column datatype='string' name='[Region]' role='dimension' type='nominal'><semantic-values><semantic-value key='[Country].[Name]' value='&quot;US&quot;' /></semantic-values></column>"
    sheets = worksheet(
        "S",
        BASIC,
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        deps_extra=deps,
        table_extra="<mark-labels><mark-label id='0' /></mark-labels>",
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    assert any("標記標籤" in u.reason for u in ir.ignored)


def test_control_modes_and_highlighter(tmp_path: Path) -> None:
    flt = f"""<filter class='categorical' column='{ref("none:Region:nk")}'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
        </filter>"""
    sheets = worksheet(
        "S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"), view_extra=flt
    )
    board = f"""
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='30000' id='2' mode='typeinlist' name='S' param='{ref("none:Region:nk")}' type-v2='filter' w='50000' x='0' y='0' />
          <zone h='30000' id='3' mode='list' param='[Parameters].[Parameter 2]' type-v2='paramctrl' w='50000' x='50000' y='0' />
          <zone h='30000' id='4' name='S' param='{ref("none:Region:nk")}' type-v2='highlighter' w='50000' x='0' y='30000' />
        </zone>
      </zones>
    </dashboard>"""
    ir = parse(tmp_path, b.workbook(worksheets=sheets, dashboards=board))
    assert reasons(ir) == []
    zones = {z.id: z for z in iter_zones(ir.dashboards[0].zones)}
    assert (zones["z2"].kind, zones["z2"].control) == ("filter", "type_in")
    assert (zones["z3"].kind, zones["z3"].control) == ("parameter", "single_list")
    assert zones["z4"].kind == "blank"


def test_natural_sort_is_alphabetic(tmp_path: Path) -> None:
    sort = f"<sort class='natural' column='{ref('none:Region:nk')}' direction='DESC' />"
    sheets = worksheet(
        "S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"), view_extra=sort
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    (sort_ir,) = ir.worksheets[0].sorts
    assert (sort_ir.kind, sort_ir.order) == ("alphabetic", "desc")


# ---------------------------------------------------------------- I1 自動與範圍尺寸


def test_range_dashboard_and_zone_layout(tmp_path: Path) -> None:
    sheets = worksheet("S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    board = """
    <dashboard name='Board'>
      <size maxheight='800' maxwidth='1200' minheight='600' minwidth='900' sizing-mode='range' />
      <zones>
        <zone h='100000' id='1' param='horz' type-v2='layout-flow' w='100000' x='0' y='0'>
          <zone h='100000' id='2' name='S' show-title='false' w='80000' x='0' y='0' />
          <zone fixed-size='240' h='100000' id='3' is-fixed='true' param='vert' type-v2='layout-flow' w='20000' x='80000' y='0'>
            <zone h='50000' hidden-by-user='true' id='4' type-v2='empty' w='20000' x='80000' y='0' />
          </zone>
        </zone>
      </zones>
    </dashboard>"""
    ir = parse(tmp_path, b.workbook(worksheets=sheets, dashboards=board))
    assert reasons(ir) == []
    dashboard = ir.dashboards[0]
    assert dashboard.sizing == "range"
    assert (dashboard.size.width, dashboard.size.height) == (1200, 800)
    assert dashboard.min_size is not None and dashboard.max_size is not None
    assert (dashboard.min_size.width, dashboard.max_size.height) == (900, 800)
    zones = {z.id: z for z in iter_zones(dashboard.zones)}
    assert zones["z1"].layout == "horizontal"
    assert (zones["z2"].show_title, zones["z2"].fixed) == (False, None)
    assert (zones["z3"].layout, zones["z3"].fixed, zones["z3"].w) == ("vertical", 240, 240)
    assert zones["z4"].hidden is True


# ---------------------------------------------------------------- J1 Measure Names／Values

PROFIT = instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative")
MEASURES = [*BASIC, PROFIT]


def measure_names_filter(*instances: str, order: tuple[str, ...] | None = None) -> str:
    members = "".join(
        f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref(i)}&quot;' />"
        for i in instances
    )
    buckets = "".join(f"<bucket>&quot;{ref(i)}&quot;</bucket>" for i in (order or instances))
    return (
        f"<filter class='categorical' column='{ref(':Measure Names')}'><groupfilter function='union'>{members}</groupfilter></filter>"
        f"<manual-sort column='{ref(':Measure Names')}' direction='ASC'><dictionary>{buckets}</dictionary></manual-sort>"
    )


def test_measure_names_and_values(tmp_path: Path) -> None:
    sheets = worksheet(
        "KPIs",
        MEASURES,
        rows=ref("none:Region:nk"),
        cols=ref(":Measure Names"),
        mark="Automatic",
        encodings=f"<text column='{ref('Multiple Values')}' />",
        view_extra=measure_names_filter(
            "sum:Sales:qk", "sum:Profit:qk", order=("sum:Profit:qk", "sum:Sales:qk")
        ),
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    sheet = ir.worksheets[0]
    assert [u.field for u in sheet.columns] == ["measure_names"]
    assert [u.field for u in sheet.marks.label] == ["measure_values"]
    assert [(u.field, u.agg) for u in sheet.measure_values] == [
        ("f_profit", "sum"),
        ("f_sales", "sum"),
    ]
    assert sheet.filters == [] and sheet.sorts == []


def test_measure_values_without_names_filter_is_unsupported(tmp_path: Path) -> None:
    sheets = worksheet(
        "KPIs",
        MEASURES,
        rows=ref("none:Region:nk"),
        encodings=f"<text column='{ref('Multiple Values')}' />",
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert any("Measure Names 篩選" in r for r in reasons(ir))


# ---------------------------------------------------------------- J2 多個量值與雙軸


def measure_pane(measure: str, mark: str, extra: str = "", encodings: str = "") -> str:
    return (
        f"<pane id='1' y-axis-name='{ref(measure)}'{extra}><view><breakdown value='auto' /></view>"
        f"<mark class='{mark}' /><encodings>{encodings}</encodings></pane>"
    )


def test_dual_axis_panes(tmp_path: Path) -> None:
    panes = measure_pane("sum:Sales:qk", "Bar") + measure_pane(
        "sum:Profit:qk", "Line", " y-index='1'"
    ).replace("id='1'", "id='2'")
    sheets = worksheet(
        "Dual",
        MEASURES,
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Category:nk"),
        mark="Automatic",
        panes_extra=panes,
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    sheet = ir.worksheets[0]
    assert [(u.field, u.agg) for u in sheet.rows] == [("f_sales", "sum"), ("f_profit", "sum")]
    assert [(p.measure.field, p.axis, p.dual, p.marks.type) for p in sheet.panes] == [
        ("f_sales", "rows", False, "bar"),
        ("f_profit", "rows", True, "line"),
    ]


def test_panes_with_different_detail_are_unsupported(tmp_path: Path) -> None:
    panes = measure_pane(
        "sum:Sales:qk", "Bar", encodings=f"<color column='{ref('none:Region:nk')}' />"
    ) + measure_pane("sum:Profit:qk", "Line").replace("id='1'", "id='2'")
    sheets = worksheet(
        "Mixed",
        MEASURES,
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Category:nk"),
        panes_extra=panes,
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert any("詳細程度不同" in r for r in reasons(ir))


# ---------------------------------------------------------------- J3 圓餅、形狀、路徑


def test_pie_shape_and_path_encodings(tmp_path: Path) -> None:
    pie = worksheet(
        "Pie",
        MEASURES,
        mark="Pie",
        encodings=(
            f"<color column='{ref('none:Category:nk')}' />"
            f"<wedge-size column='{ref('sum:Sales:qk')}' />"
            f"<text column='{ref('none:Category:nk')}' />"
        ),
    )
    shapes = worksheet(
        "Shapes",
        MEASURES,
        rows=ref("sum:Profit:qk"),
        cols=ref("sum:Sales:qk"),
        mark="Shape",
        encodings=f"<shape column='{ref('none:Category:nk')}' /><path column='{ref('none:Region:nk')}' />",
    )
    ir = parse(tmp_path, b.workbook(worksheets=pie + shapes))
    assert reasons(ir) == []
    pie_marks, shape_marks = ir.worksheets[0].marks, ir.worksheets[1].marks
    assert pie_marks.type == "pie"
    assert pie_marks.angle is not None and pie_marks.angle.field == "f_sales"
    assert shape_marks.type == "shape"
    assert shape_marks.shape is not None and shape_marks.shape.field == "f_category"
    assert shape_marks.path is not None and shape_marks.path.field == "f_region"


# ---------------------------------------------------------------- H4、L1 多欄位篩選與篩選動作

ACTION_GROUPS = """
      <group caption='Action (Region)' hidden='true' name='[Action (Region)]' name-style='unqualified' user:auto-column='sheet_link'>
        <groupfilter function='crossjoin'><groupfilter function='level-members' level='[Region]' /></groupfilter>
      </group>
      <group hidden='true' name='[Exclusions (Region,Category)]' name-style='unqualified' user:auto-column='exclude'>
        <groupfilter function='crossjoin'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
          <groupfilter function='level-members' level='[none:Category:nk]' />
        </groupfilter>
      </group>"""


def action_filter(function: str = "level-members") -> str:
    # 清除時排除所有值（empty-level）的層級寫在 member 屬性
    level = "member='[Region]'" if function == "empty-level" else "level='[Region]'"
    return (
        f"<filter class='categorical' column='{ref('Action (Region)')}'>"
        f"<groupfilter function='{function}' {level} user:ui-action-filter='[Action1]' />"
        "</filter>"
    )


def action(
    on_empty: str = "",
    source: str = "<source dashboard='Board' type='sheet' worksheet='Source' />",
    target: str = "Board",
    activation: str = "on-select",
    single_select: str = "",
) -> str:
    param = f"<param name='on-empty' value='{on_empty}' />" if on_empty else ""
    if single_select:
        param += f"<param name='single-select' value='{single_select}' />"
    return (
        f"<action caption='Filter Region' name='[Action1]'><activation auto-clear='true' type='{activation}' />"
        f"{source}"
        f"<command command='tsc:tsl-filter'><param name='special-fields' value='all' />{param}"
        f"<param name='target' value='{target}' /></command></action>"
    )


def action_workbook(target_filter: str, on_empty: str = "", actions: str | None = None) -> str:
    sheets = worksheet(
        "Source", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    ) + worksheet(
        "Target",
        BASIC,
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=target_filter,
    )
    board = """
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' param='horz' type-v2='layout-flow' w='100000' x='0' y='0'>
          <zone h='100000' id='2' name='Source' w='50000' x='0' y='0' />
          <zone h='100000' id='3' name='Target' w='50000' x='50000' y='0' />
        </zone>
      </zones>
    </dashboard>
    <dashboard name='Detail'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones><zone h='100000' id='4' name='Target' w='100000' x='0' y='0' /></zones>
    </dashboard>"""
    return b.workbook(
        b.extract_datasource(extra=ACTION_GROUPS),
        worksheets=sheets,
        dashboards=board,
        actions=action(on_empty) if actions is None else actions,
    )


def test_filter_action_and_target_filter(tmp_path: Path) -> None:
    ir = parse(tmp_path, action_workbook(action_filter()))
    assert reasons(ir) == []
    (act,) = ir.actions
    assert (act.id, act.kind, act.activation, act.clearing) == (
        "act_filter_region",
        "filter",
        "select",
        "show_all",
    )
    assert (act.sources, act.targets) == (["ws_source"], ["ws_target"])
    assert (act.dashboard, act.target_dashboard, act.single_select) == ("db_board", None, False)
    (flt,) = ir.worksheets[1].filters
    assert flt.kind == "tuple"
    assert ([u.field for u in flt.fields], flt.mode, flt.tuples, flt.action) == (
        ["f_region"],
        "all",
        [],
        "act_filter_region",
    )


def test_filter_action_exclude_all_when_cleared(tmp_path: Path) -> None:
    xml = action_workbook(action_filter("empty-level"), on_empty="none")
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    assert ir.actions[0].clearing == "exclude_all"
    (flt,) = ir.worksheets[1].filters
    assert flt.kind == "tuple" and (flt.mode, flt.tuples) == ("include", [])


def test_filter_action_sources_and_navigation(tmp_path: Path) -> None:
    # 來源：dashboard 上扣掉 <exclude-sheet> 的工作表；目標是另一個 dashboard 時執行後切換過去
    source = "<source dashboard='Board' type='sheet'><exclude-sheet name='Target' /></source>"
    xml = action_workbook(
        action_filter(),
        actions=action(source=source, target="Detail", activation="on-hover", single_select="true"),
    )
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    (act,) = ir.actions
    assert (
        act.activation,
        act.sources,
        act.dashboard,
        act.target_dashboard,
        act.single_select,
    ) == (
        "hover",
        ["ws_source"],
        "db_board",
        "db_detail",
        True,
    )


def test_filter_action_from_datasource(tmp_path: Path) -> None:
    # 工作表層級的動作：來源是使用該資料源的所有工作表，任何 dashboard 都執行
    source = (
        f"<source datasource='{b.DS}' type='datasource'><exclude-sheet name='Target' /></source>"
    )
    ir = parse(tmp_path, action_workbook(action_filter(), actions=action(source=source)))
    assert reasons(ir) == []
    (act,) = ir.actions
    assert (act.sources, act.dashboard, act.target_dashboard) == (["ws_source"], None, "db_board")


def test_filter_action_multi_field_exclude_all(tmp_path: Path) -> None:
    flt = (
        f"<filter class='categorical' column='{ref('Action (Region,Category)')}'>"
        "<groupfilter function='crossjoin' user:ui-action-filter='[Action1]' user:ui-enumeration='inclusive'>"
        "<groupfilter function='empty-level' member='[Region]' />"
        "<groupfilter function='empty-level' member='[Category]' />"
        "</groupfilter></filter>"
    )
    ir = parse(tmp_path, action_workbook(flt, on_empty="none"))
    assert reasons(ir) == []
    (tuple_filter,) = ir.worksheets[1].filters
    assert tuple_filter.kind == "tuple"
    assert ([u.field for u in tuple_filter.fields], tuple_filter.mode, tuple_filter.tuples) == (
        ["f_region", "f_category"],
        "include",
        [],
    )


def test_filter_action_saved_selection(tmp_path: Path) -> None:
    # 存檔時有選取：多個標記以 union 列出各自的值組合
    flt = f"""<filter class='categorical' column='{ref("Action (Region,Category)")}'>
      <groupfilter function='union' user:ui-action-filter='[Action1]'>
        <groupfilter function='crossjoin'>
          <groupfilter function='member' level='[Region]' member='&quot;East&quot;' />
          <groupfilter function='member' level='[Category]' member='&quot;Technology&quot;' />
        </groupfilter>
        <groupfilter function='crossjoin'>
          <groupfilter function='member' level='[Region]' member='&quot;West&quot;' />
          <groupfilter function='member' level='[Category]' member='%null%' />
        </groupfilter>
      </groupfilter>
    </filter>"""
    ir = parse(tmp_path, action_workbook(flt, on_empty="assign"))
    assert reasons(ir) == []
    assert ir.actions[0].clearing == "keep"
    (tuple_filter,) = ir.worksheets[1].filters
    assert tuple_filter.kind == "tuple"
    assert (tuple_filter.mode, tuple_filter.tuples) == (
        "include",
        [["East", "Technology"], ["West", None]],
    )


def test_filter_action_unsupported_variants(tmp_path: Path) -> None:
    # 清除選取的方式不認得：動作不轉成 IR，目標工作表的篩選維持預設狀態
    ir = parse(tmp_path, action_workbook(action_filter(), on_empty="weird"))
    assert reasons(ir) == ["篩選動作「Filter Region」清除選取的方式 weird v0 不支援"]
    assert ir.actions == []
    (flt,) = ir.worksheets[1].filters
    assert flt.kind == "tuple" and flt.action is None  # 維持 .twb 中的預設狀態


def test_exclusions_filter(tmp_path: Path) -> None:
    exclusions = f"""<filter class='categorical' column='{ref("Exclusions (Region,Category)")}'>
      <groupfilter function='except' user:ui-enumeration='exclusive'>
        <groupfilter function='crossjoin'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
          <groupfilter function='level-members' level='[none:Category:nk]' />
        </groupfilter>
        <groupfilter function='crossjoin'>
          <groupfilter function='member' level='[none:Region:nk]' member='&quot;East&quot;' />
          <groupfilter function='member' level='[none:Category:nk]' member='%null%' />
        </groupfilter>
      </groupfilter>
    </filter>"""
    sheets = worksheet(
        "S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"), view_extra=exclusions
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extra=ACTION_GROUPS), worksheets=sheets))
    assert reasons(ir) == []
    (flt,) = ir.worksheets[0].filters
    assert flt.kind == "tuple"
    assert ([u.field for u in flt.fields], flt.mode, flt.tuples, flt.action) == (
        ["f_region", "f_category"],
        "exclude",
        [["East", None]],
        None,
    )


def test_action_filter_level_from_instance_name(tmp_path: Path) -> None:
    flt = (
        f"<filter class='categorical' column='{ref('Action (YEAR(Order Date))')}'>"
        "<groupfilter function='level-members' level='[yr:Order Date:ok]' user:ui-action-filter='[Action1]' />"
        "</filter>"
    )
    sheets = worksheet(
        "S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"), view_extra=flt
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    (tuple_filter,) = ir.worksheets[0].filters
    assert tuple_filter.kind == "tuple"
    (use,) = tuple_filter.fields
    assert use.field == "f_order_date" and use.date is not None and use.date.unit == "year"
