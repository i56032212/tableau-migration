"""擴充階段的解析（H1–H4、I1–I2、J1–J4、K1–K4、L1–L2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.ir import IR, CategoricalFilter, FieldUse, ParamRef, TupleFilter, iter_zones
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
    assert reasons(ir) == ["相對日期篩選的欄位「Category」不是日期"]


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


def test_table_count_only_in_worksheet_dependencies(tmp_path: Path) -> None:
    """單表資料源的「資料表 (計數)」只寫在工作表的相依清單中（沒有 object-graph，較舊版本的寫法）。"""
    column = f"<column caption='Orders' datatype='integer' name='{TABLE}' role='measure' type='quantitative' />"
    sheets = worksheet(
        "Counts",
        [*BASIC, COUNT],
        rows=ref("none:Region:nk"),
        cols=COUNT_REF,
        deps_extra=column,
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    count = next(f for f in ir.datasources[0].fields if f.name == TABLE)
    assert (count.caption, count.datatype, count.table) == ("Orders", "integer", None)
    assert count.calc is not None and count.calc.formula == "1"
    (use,) = ir.worksheets[0].columns
    assert (use.field, use.agg) == (count.id, "count")


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
    # 醒目提示控制項：工作表上的欄位
    assert (zones["z4"].kind, zones["z4"].worksheet) == ("highlighter", "ws_s")
    assert zones["z4"].field is not None and zones["z4"].field.field == "f_region"


def test_highlight_actions(tmp_path: Path) -> None:
    """醒目提示動作：所有欄位或選定的欄位（field-captions），目標為 dashboard 上的工作表扣掉 exclude。"""
    sheets = worksheet(
        "S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    ) + worksheet("T", BASIC, rows=ref("none:Category:nk"), cols=ref("sum:Sales:qk")).replace(
        "name='S'", "name='T'"
    )
    board = """
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='50000' id='2' name='S' w='100000' x='0' y='0' />
          <zone h='50000' id='3' name='T' w='100000' x='0' y='50000' />
        </zone>
      </zones>
    </dashboard>"""

    def brush(name: str, params: str, activation: str = "on-select") -> str:
        return (
            f"<action caption='{name}' name='[{name}]'><activation type='{activation}' />"
            "<source dashboard='Board' type='sheet' worksheet='S' />"
            f"<command command='tsc:brush'>{params}<param name='target' value='Board' /></command>"
            "</action>"
        )

    actions = (
        brush("All", "<param name='special-fields' value='all' />", "on-hover")
        + brush("Picked", "<param name='field-captions' value='Region,Category' />")
        + brush(
            "Excluded",
            "<param name='special-fields' value='all' /><param name='exclude' value='T' />",
        )
        + brush("Dates", "<param name='special-fields' value='dates-and-times' />")
    )
    ir = parse(
        tmp_path,
        b.workbook(worksheets=sheets, dashboards=board, actions=actions),
    )
    assert reasons(ir) == ["醒目提示動作「Dates」比對的欄位（dates-and-times）v0 不支援"]
    found = {a.caption: a for a in ir.actions}
    assert set(found) == {"All", "Picked", "Excluded"}
    every = found["All"]
    assert (every.kind, every.activation, every.sources, every.targets) == (
        "highlight",
        "hover",
        ["ws_s"],
        ["ws_s", "ws_t"],
    )
    assert every.highlight is not None and every.highlight.fields == []
    assert found["Picked"].highlight is not None
    assert found["Picked"].highlight.fields == ["Region", "Category"]
    assert found["Excluded"].targets == ["ws_s"]
    assert all(
        a.dashboard == ir.dashboards[0].id and a.target_dashboard is None for a in found.values()
    )


@pytest.mark.parametrize(
    "sort",
    [
        f"<sort class='natural' column='{ref('none:Region:nk')}' direction='DESC' />",
        f"<natural-sort column='{ref('none:Region:nk')}' direction='DESC' />",
        # 較舊版本的寫法
        f"<alphabetic-sort column='{ref('none:Region:nk')}' direction='DESC' />",
    ],
)
def test_natural_sort_is_alphabetic(tmp_path: Path, sort: str) -> None:
    sheets = worksheet(
        "S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"), view_extra=sort
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    (sort_ir,) = ir.worksheets[0].sorts
    assert (sort_ir.kind, sort_ir.order) == ("alphabetic", "desc")


def test_old_shelf_sort_and_drop_lines(tmp_path: Path) -> None:
    """較舊版本的依量值排序（<view><rows><shelf-sort>）；選取時才顯示的 Drop Lines 記為略過。"""
    sort = (
        f"<rows><shelf-sort direction='DESC' measure-to-sort-by='{ref('sum:Sales:qk')}'>"
        f"<dimensions-to-sort><dimension>{ref('none:Region:nk')}</dimension></dimensions-to-sort>"
        "</shelf-sort></rows>"
    )
    drop = "<dropline drop-when='selected' haslabel='false' x-axis='true' y-axis='false' />"
    sheets = worksheet(
        "S",
        BASIC,
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=sort,
        pane_extra=drop,
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    (sort_ir,) = ir.worksheets[0].sorts
    assert (sort_ir.field, sort_ir.kind, sort_ir.order) == ("f_region", "by_field", "desc")
    assert sort_ir.by == FieldUse(field="f_sales", agg="sum", continuous=True)
    assert any("Drop Lines" in u.reason for u in ir.ignored)

    always = drop.replace("selected", "always")
    sheets = worksheet(
        "S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"), pane_extra=always
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == ["一律顯示的 Drop Lines v0 不支援"]


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


def test_measure_names_members_with_percent(tmp_path: Path) -> None:
    """Measure Names 的成員（與色彩對照）中，欄位名稱的 % 寫成 \\%。"""
    margin = instance("[Margin %]", "Sum", "[sum:Margin %:qk]", "quantitative")
    sheets = worksheet(
        "KPIs",
        [*MEASURES, margin],
        rows=ref("none:Region:nk"),
        cols=ref(":Measure Names"),
        mark="Automatic",
        encodings=f"<text column='{ref('Multiple Values')}' />",
        view_extra=measure_names_filter("sum:Sales:qk", "sum:Margin \\%:qk"),
    )
    ds = b.extract_datasource(columns=b.calc("Margin %", "Margin %", "[Profit] / [Sales]"))
    ir = parse(tmp_path, b.workbook(datasource=ds, worksheets=sheets))
    assert reasons(ir) == []
    captions = {f.id: f.caption for f in ir.datasources[0].fields}
    used = [(captions[u.field], u.agg) for u in ir.worksheets[0].measure_values]
    assert used == [("Sales", "sum"), ("Margin %", "sum")]


def measure_names_all(excluded: str = "") -> str:
    group = "<groupfilter function='level-members' level='[:Measure Names]' />"
    if excluded:
        group = (
            f"<groupfilter function='except'>{group}<groupfilter function='member' "
            f"level='[:Measure Names]' member='&quot;{ref(excluded)}&quot;' /></groupfilter>"
        )
    return f"<filter class='categorical' column='{ref(':Measure Names')}'>{group}</filter>"


@pytest.mark.parametrize(
    ("excluded", "expected"),
    [
        # 全部：資料源中所有的量值（各以預設的彙總），依顯示名稱排序；放在工具提示上的 AVG(Sales) 不算
        ("", [("f_profit", "sum"), ("f_quantity", "sum"), ("f_sales", "sum")]),
        # 全部，排除 Profit
        ("sum:Profit:qk", [("f_quantity", "sum"), ("f_sales", "sum")]),
    ],
)
def test_measure_names_filter_all(
    tmp_path: Path, excluded: str, expected: list[tuple[str, str]]
) -> None:
    deps = [
        *MEASURES,
        instance("[Quantity]", "Sum", "[sum:Quantity:qk]", "quantitative"),
        instance("[Sales]", "Avg", "[avg:Sales:qk]", "quantitative"),
    ]
    sheets = worksheet(
        "KPIs",
        deps,
        rows=ref("none:Region:nk"),
        cols=ref(":Measure Names"),
        mark="Automatic",
        encodings=(
            f"<text column='{ref('Multiple Values')}' /><tooltip column='{ref('avg:Sales:qk')}' />"
        ),
        view_extra=measure_names_all(excluded),
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    assert [(u.field, u.agg) for u in ir.worksheets[0].measure_values] == expected


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


def test_panes_with_different_detail(tmp_path: Path) -> None:
    """J3：各標記卡的詳細程度可以不同（查詢時各自計算）；座標軸樣式的 fold 也表示雙軸。"""
    panes = measure_pane(
        "sum:Sales:qk", "Bar", encodings=f"<color column='{ref('none:Region:nk')}' />"
    ) + measure_pane("sum:Profit:qk", "Line").replace("id='1'", "id='2'")
    style = (
        "<style><style-rule element='axis'><encoding attr='space' class='0' "
        f"field='{ref('sum:Profit:qk')}' fold='true' scope='rows' type='space' />"
        "</style-rule></style>"
    )
    sheets = worksheet(
        "Mixed",
        MEASURES,
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Category:nk"),
        panes_extra=panes,
    ).replace("<style />", style, 1)
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert reasons(ir) == []
    bar, line = ir.worksheets[0].panes
    assert (bar.dual, line.dual) == (False, True)
    assert bar.marks.color is not None and line.marks.color is None


def donut_sheet(inner: str) -> str:
    zero = instance("[Calculation_0]", "User", "[usr:Calculation_0:qk]", "quantitative")
    deps = (
        "<column caption='Zero' datatype='integer' name='[Calculation_0]' role='measure' "
        "type='quantitative'><calculation class='tableau' formula='MIN(0)' /></column>"
    )
    outer = measure_pane(
        "usr:Calculation_0:qk",
        "Pie",
        encodings=f"<color column='{ref('none:Region:nk')}' /><wedge-size column='{ref('sum:Sales:qk')}' />",
    )
    hole = measure_pane("usr:Calculation_0:qk", "Pie", " y-index='1'", inner).replace(
        "id='1'", "id='2'"
    )
    return worksheet(
        "Donut",
        [*BASIC, zero],
        rows=f"({ref('usr:Calculation_0:qk')} + {ref('usr:Calculation_0:qk')})",
        mark="Pie",
        panes_extra=outer + hole,
        deps_extra=deps,
    )


def test_donut(tmp_path: Path) -> None:
    sheet = donut_sheet(f"<text column='{ref('sum:Sales:qk')}' />")
    ir = parse(tmp_path, b.workbook(worksheets=sheet))
    assert reasons(ir) == []
    outer, hole = ir.worksheets[0].panes
    assert (outer.marks.type, hole.marks.type, hole.dual) == ("pie", "pie", True)
    assert [u.field for u in hole.marks.label] == ["f_sales"]


def test_multi_ring_pie_is_parsed(tmp_path: Path) -> None:
    """內圈有外圈沒有的維度（多層圓餅）：各標記卡各自的詳細程度（查詢時較粗的另外查詢）。"""
    sheet = donut_sheet(f"<color column='{ref('none:Category:nk')}' />")
    ir = parse(tmp_path, b.workbook(worksheets=sheet))
    assert reasons(ir) == []
    assert ir.worksheets[0].panes[1].marks.color is not None


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


def cross_action_workbook(link: str) -> str:
    """來源工作表用 Orders、目標工作表用 Returns（federated.test02）的篩選動作。"""
    second = "federated.test02"
    xml = action_workbook(action_filter(), actions=action().replace("<command", link + "<command"))
    start = xml.index("<worksheet name='Target'>")
    end = xml.index("</worksheet>", start)
    xml = xml[:start] + xml[start:end].replace(DS, second) + xml[end:]
    other = b.extract_datasource(extra=ACTION_GROUPS, name=second, caption="Returns")
    return xml.replace("</datasources>", other + "</datasources>", 1)


def test_filter_action_across_datasources(tmp_path: Path) -> None:
    """來源與目標工作表在不同的資料源（公開範例 Logshark 的 VizqlDesktop）：<link expression> 記下目標資料源
    的欄位對應來源的哪個欄位；目標工作表的動作篩選記下對應的來源欄位，選取時以它取值。"""
    from tmig.planner.state import QueryState, apply_state

    link = (
        "<link caption='Filter Region' delimiter=',' escape='\\' include-null='true' multi-select='true'"
        " expression='tsl:Board?%5Bfederated.test02%5D.%5BRegion%5D~s0=&lt;[Region]~na&gt;' />"
    )
    ir = parse(tmp_path, cross_action_workbook(link))
    assert reasons(ir) == []
    (act,) = ir.actions
    assert (act.sources, act.targets) == (["ws_source"], ["ws_target"])
    source, target = ir.worksheets
    assert (source.datasource, target.datasource) == (ir.datasources[0].id, ir.datasources[1].id)
    (flt,) = target.filters
    assert isinstance(flt, TupleFilter)
    assert ([u.field for u in flt.fields], flt.source_fields) == (["f_region_2"], ["f_region"])
    # 來源工作表的標記以 Orders 的欄位為鍵
    state = QueryState(actions={act.id: [{"f_region": "East", "f_category": "Furniture"}]})
    (effective,) = apply_state(ir, target, state).filters
    assert isinstance(effective, TupleFilter)
    assert ([u.field for u in effective.fields], effective.mode, effective.tuples) == (
        ["f_region_2"],
        "include",
        [["East"]],
    )

    # 沒有欄位對應：列為不支援、不建立動作（目標工作表照常顯示全部）
    ir = parse(tmp_path, cross_action_workbook(""))
    assert reasons(ir) == ["篩選動作「Filter Region」跨資料源，對應的欄位無法解析，v0 不支援"]
    assert ir.actions == []


def test_filter_action_does_not_filter_excluded_sheets(tmp_path: Path) -> None:
    """exclude 參數列出的工作表不受動作篩選：它身上這個動作的篩選維持靜態（不連到動作）。"""
    excluded = action().replace(
        "<param name='target'", "<param name='exclude' value='Target' /><param name='target'"
    )
    ir = parse(tmp_path, action_workbook(action_filter(), actions=excluded))
    (act,) = ir.actions
    assert act.targets == []
    (flt,) = ir.worksheets[1].filters
    assert isinstance(flt, TupleFilter) and flt.action is None


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


def test_action_excluding_every_source_is_ignored(tmp_path: Path) -> None:
    """來源的工作表都被排除：動作不會觸發（Tableau 仍保留），略過而不列為不支援。"""
    source = (
        "<source dashboard='Board' type='sheet'>"
        "<exclude-sheet name='Source' /><exclude-sheet name='Target' /></source>"
    )
    ir = parse(tmp_path, action_workbook(action_filter(), actions=action(source=source)))
    assert reasons(ir) == []
    assert ir.actions == []
    assert any("排除了所有來源工作表" in u.reason for u in ir.ignored)


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


def test_filter_action_reordered_tuples(tmp_path: Path) -> None:
    """較新的版本把值組合寫成巢狀的 crossjoin，外面再以 reorder-dimensionality 標明各欄位的順序。"""
    flt = f"""<filter class='categorical' column='{ref("Action (Region,Category)")}'>
      <groupfilter function='reorder-dimensionality' user:ui-action-filter='[Action1]'>
        <groupfilter function='crossjoin'>
          <groupfilter function='member' level='[Category]' member='&quot;Technology&quot;' />
          <groupfilter function='crossjoin'>
            <groupfilter function='member' level='[Region]' member='&quot;East&quot;' />
          </groupfilter>
        </groupfilter>
        <order><hierarchy name='[Region]' /><hierarchy name='[Category]' /></order>
      </groupfilter>
    </filter>"""
    ir = parse(tmp_path, action_workbook(flt, on_empty="assign"))
    assert reasons(ir) == []
    (tuple_filter,) = ir.worksheets[1].filters
    assert tuple_filter.kind == "tuple"
    assert ([u.field for u in tuple_filter.fields], tuple_filter.tuples) == (
        ["f_region", "f_category"],
        [["East", "Technology"]],
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


# ---------------------------------------------------------------- K1 群組與數值分組


def group_column(
    calc_attrs: str, bins: str, datatype: str = "string", name: str = "Region (group)"
) -> str:
    return (
        f"<column caption='{name}' datatype='{datatype}' name='[{name}]' role='dimension' type='nominal'>"
        f"<calculation class='categorical-bin' {calc_attrs}>{bins}</calculation></column>"
    )


def field_formula(ir: IR, field_id: str) -> str:
    found = next(f for ds in ir.datasources for f in ds.fields if f.id == field_id)
    assert found.calc is not None
    return found.calc.formula


def test_group_formula(tmp_path: Path) -> None:
    bins = (
        "<bin default-name='false' value='&quot;Coasts&quot;'>"
        "<value>&quot;East&quot;</value><value>&quot;West&quot;</value><value>%null%</value></bin>"
        "<bin default-name='false' value='&quot;Say &quot;&quot;hi&quot;&quot;&quot;'>"
        "<value>&quot;North&quot;</value></bin>"
    )
    calcs = group_column("column='[Region]' default='&quot;Other&quot;' new-bin='true'", bins)
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=calcs)))
    assert reasons(ir) == []
    assert field_formula(ir, "f_region_group") == (
        'IF ISNULL([Region]) THEN "Coasts"\n'
        'ELSEIF [Region] IN ("East", "West") THEN "Coasts"\n'
        'ELSEIF [Region] IN ("North") THEN "Say ""hi"""\n'
        'ELSE "Other"\n'
        "END"
    )


def test_group_without_members_and_on_numbers(tmp_path: Path) -> None:
    empty = group_column("column='[Region]' new-bin='true'", "", name="Empty (group)")
    numbers = group_column(
        "column='[Quantity]' new-bin='true'",
        "<bin value='&quot;Few&quot;'><value>1</value><value>2</value></bin>",
        name="Quantity (group)",
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=empty + numbers)))
    assert reasons(ir) == []
    assert field_formula(ir, "f_empty_group") == "[Region]"
    assert field_formula(ir, "f_quantity_group") == (
        'IF [Quantity] IN (1, 2) THEN "Few"\nELSE STR([Quantity])\nEND'
    )


def test_bin_formula_and_errors(tmp_path: Path) -> None:
    pegged = (
        "<column caption='Sales (bin)' datatype='real' name='[Sales (bin)]' role='dimension' type='ordinal'>"
        "<calculation class='bin' decimals='1' formula='[Sales]' peg='2.5' size='10' /></column>"
    )
    broken = (
        "<column caption='Profit (bin)' datatype='integer' name='[Profit (bin)]' role='dimension' type='ordinal'>"
        "<calculation class='bin' decimals='0' formula='[Profit]' peg='0' size='0' /></column>"
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=pegged + broken)))
    assert field_formula(ir, "f_sales_bin") == "FLOOR((([Sales]) - 2.5) / 10) * 10 + 2.5"
    assert reasons(ir) == ["數值分組「Profit (bin)」：分組大小必須大於 0（0）"]


def test_group_definition_in_worksheet_and_missing_values(tmp_path: Path) -> None:
    """工作表的 datasource-dependencies 也會帶群組定義；「顯示缺少的值」不影響數字。"""
    calcs = group_column(
        "column='[Region]' new-bin='true'",
        "<bin value='&quot;Coasts&quot;'><value>&quot;East&quot;</value></bin>",
    )
    group = instance("[Region (group)]", "None", "[none:Region (group):nk]")
    sheet = worksheet(
        "S",
        [*BASIC, group],
        rows=ref("none:Region (group):nk"),
        cols=ref("sum:Sales:qk"),
        deps_extra=calcs,
        table_extra=f"<show-full-range><column>{ref('Region (group)')}</column></show-full-range>",
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=calcs), worksheets=sheet))
    assert reasons(ir) == []
    assert ir.worksheets[0].rows[0].field == "f_region_group"
    assert any("顯示缺少的值" in u.reason for u in ir.ignored)


# ---------------------------------------------------------------- K3 差異與差異百分比


def test_difference_quick_calcs(tmp_path: Path) -> None:
    def diff(kind: str, prefix: str, mode: str, value: str) -> str:
        return instance(
            "[Sales]",
            "Sum",
            f"[{prefix}:sum:Sales:qk]",
            "quantitative",
            f"<table-calc diff-options='{mode}' ordering-type='Columns' type='{kind}'>"
            f"<address><value>{value}</value></address></table-calc>",
        )

    sheet = worksheet(
        "S",
        [
            *BASIC,
            diff("Difference", "diff", "Relative", "-1"),
            diff("PctDiff", "pcdf", "Absolute", "First"),
        ],
        rows=ref("none:Region:nk"),
        cols=ref("diff:sum:Sales:qk"),
        encodings=f"<text column='{ref('pcdf:sum:Sales:qk')}' />",
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheet), lenient=True)
    assert reasons(ir) == ["差異計算的比較對象（Absolute：First）尚不支援"]
    (tc,) = ir.worksheets[0].table_calcs
    assert (tc.type, tc.preset, tc.options) == ("difference", "table_down", {"offset": -1})


def test_more_quick_calcs_and_directions(tmp_path: Path) -> None:
    """百分位數、百分比、與成員比較的差異、複合成長率；窗格與儲存格的方向、計算層級與重新開始間隔。"""

    def calc_instance(prefix: str, body: str) -> str:
        return instance("[Sales]", "Sum", f"[{prefix}:sum:Sales:qk]", "quantitative", body)

    month = instance("[Order Date]", "Month", "[mn:Order Date:ok]", "ordinal")
    year = instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal")
    date = f"[{b.DS}].[Order Date]"
    calcs = [
        calc_instance(
            "pcrk",
            "<table-calc ordering-type='Rows' rank-options='Competition,Descending' type='PctRank' />",
        ),
        calc_instance(
            "pcva",
            "<table-calc diff-options='Fixed' ordering-field='[federated.test01].[none:Region:nk]' "
            "ordering-type='Field' type='PctValue'><address><value>East</value></address></table-calc>",
        ),
        calc_instance(
            "pcdf",
            "<table-calc diff-options='Fixed,Compounded' ordering-type='Rows' type='PctDiff'>"
            "<address><value>%first%</value></address></table-calc>",
        ),
        calc_instance(
            "cum", "<table-calc aggregation='Sum' ordering-type='RowInPane' type='CumTotal' />"
        ),
        calc_instance(
            "diff",
            f"<table-calc diff-options='Relative' level-address='{ref('yr:Order Date:ok')}' "
            f"level-break='{date}' ordering-field='{date}' ordering-type='Field' type='Difference'>"
            "<address><value>-1</value></address></table-calc>",
        ),
    ]
    encodings = "".join(
        f"<tooltip column='{ref(f'{p}:sum:Sales:qk')}' />"
        for p in ("pcrk", "pcva", "pcdf", "cum", "diff")
    )
    sheet = worksheet(
        "S",
        [*BASIC, month, year, *calcs],
        rows=ref("none:Region:nk"),
        cols=f"({ref('yr:Order Date:ok')} / {ref('mn:Order Date:ok')})",
        mark="Text",
        encodings=encodings,
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheet))
    assert reasons(ir) == []
    calcs_by_type = {tc.type: tc for tc in ir.worksheets[0].table_calcs}
    assert calcs_by_type["percentile"].options == {"direction": "desc"}
    member = calcs_by_type["percent_from"]
    assert member.options == {"relative_to": "member", "member": "East", "member_field": "f_region"}
    compounded = calcs_by_type["percent_difference"]
    assert compounded.options == {"compounded": True, "relative_to": "first"}
    pane = calcs_by_type["running_total"]
    assert pane.preset == "pane_across"
    assert [(u.field, u.date and u.date.unit) for u in pane.instances] == [
        ("f_order_date", "month")
    ]
    level = calcs_by_type["difference"]
    assert (
        level.level is not None and level.level.date is not None and level.level.date.unit == "year"
    )
    assert level.restart == FieldUse(field="f_order_date")
    # 以參數決定比較對象：相對位置
    sheet = worksheet(
        "S",
        [
            *BASIC,
            calc_instance(
                "diff",
                "<table-calc diff-options='Relative,Parameter' ordering-type='Rows' type='Difference'>"
                "<address><value>[Parameters].[Parameter 1]</value></address></table-calc>",
            ),
        ],
        rows=ref("none:Region:nk"),
        cols=ref("diff:sum:Sales:qk"),
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheet))
    assert reasons(ir) == []
    (tc,) = ir.worksheets[0].table_calcs
    assert tc.options == {"parameter": "[Parameters].[Parameter 1]"}


# ---------------------------------------------------------------- L2 網址、參數與切換動作


def test_url_action(tmp_path: Path) -> None:
    link = (
        "<link caption='Search' delimiter=';' expression='https://example.com/?r=&lt;[Region]&gt;"
        "&amp;s=&lt;SUM([Sales])&gt;&amp;p=&lt;[Parameters].[Parameter 2]&gt;&amp;x=&lt;[Nope]&gt;' "
        "url-escape='false' />"
    )
    actions = (
        "<action caption='Search' name='[Action9]'>"
        "<source dashboard='Board' type='sheet' worksheet='Source' />"
        f"{link}</action>"
    )
    ir = parse(tmp_path, action_workbook(action_filter(), actions=actions))
    assert reasons(ir) == []
    (act,) = ir.actions
    assert (act.kind, act.activation, act.sources) == ("url", "menu", ["ws_source"])
    assert act.url is not None
    assert [(f.placeholder, f.key) for f in act.url.fields] == [
        ("<[Region]>", "f_region"),
        ("<SUM([Sales])>", "sum(f_sales)"),
        ("<[Parameters].[Parameter 2]>", "param:p_region_pick"),
    ]
    assert (act.url.escape, act.url.delimiter) == (False, ";")
    assert any("<[Nope]> 找不到欄位" in u.reason for u in ir.ignored)


def test_parameter_and_navigate_actions(tmp_path: Path) -> None:
    actions = f"""
      <nav-action caption='Go Detail' name='[Action7]'>
        <source dashboard='Board' type='sheet' worksheet='Target' />
        <params><param name='target' value='Detail' /></params>
      </nav-action>
      <nav-action caption='Go Sheet' name='[Action8]'>
        <activation type='on-select' />
        <source dashboard='Board' type='sheet' worksheet='Target' />
        <params><param name='target' value='Source' /></params>
      </nav-action>
      <edit-parameter-action caption='Pick Region' name='[Action5]'>
        <activation type='on-select' />
        <source dashboard='Board' type='sheet' worksheet='Source' />
        <agg-type type='sum' />
        <clear-option type='assign-fixed-value' value='&quot;West&quot;' />
        <params>
          <param name='source-field' value='{ref("none:Region:nk")}' />
          <param name='target-parameter' value='[Parameters].[Parameter 2]' />
        </params>
      </edit-parameter-action>
      <edit-group-action caption='Set Members' name='[Action6]'>
        <source dashboard='Board' type='sheet' worksheet='Source' />
      </edit-group-action>
      <weird-action name='[Action10]' />"""
    ir = parse(tmp_path, action_workbook(action_filter(), actions=actions))
    assert reasons(ir) == [
        "集合動作「Set Members」的目標 （未指定） 不是可以移轉的固定成員集合",
        "動作清單中的未知元素 <weird-action>",
    ]
    nav, sheet_nav, param = ir.actions
    assert (nav.kind, nav.activation, nav.target_dashboard, nav.target_worksheet) == (
        "navigate",
        "menu",
        "db_detail",
        None,
    )
    assert (sheet_nav.activation, sheet_nav.target_worksheet) == ("select", "ws_source")
    assert param.parameter is not None
    assert param.parameter.model_dump() == {
        "parameter": "p_region_pick",
        "field": "f_region",
        "aggregation": "sum",
        "clearing": "assign",
        "clear_value": "West",
    }


def test_newer_action_formats(tmp_path: Path) -> None:
    """較新的版本：切換動作的目標寫在 sheet；來源為所有工作表（type='all'）扣掉排除的。"""
    actions = f"""
      <nav-action caption='Go' name='[Action11]'>
        <activation type='on-select' />
        <source dashboard='Board' type='sheet' worksheet='Target' />
        <params><param name='sheet' value='Detail' /></params>
      </nav-action>
      <edit-parameter-action caption='Everywhere' name='[Action12]'>
        <activation type='on-select' />
        <source type='all'><exclude-sheet name='Target' /></source>
        <agg-type type='attr' />
        <params>
          <param name='source-field' value='{ref("none:Region:nk")}' />
          <param name='target-parameter' value='[Parameters].[Parameter 2]' />
        </params>
      </edit-parameter-action>"""
    ir = parse(tmp_path, action_workbook(action_filter(), actions=actions))
    assert reasons(ir) == []
    nav, param = ir.actions
    assert (nav.kind, nav.target_dashboard) == ("navigate", "db_detail")
    assert param.sources == ["ws_source"]


def test_dashboard_text_with_parameters_and_formatting(tmp_path: Path) -> None:
    """文字物件與標題：參數換成參數的值（content 中的參數），保留各段的格式與換行；<Sheet Name> 為 dashboard 名稱。"""
    board = """
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='20000' id='2' type-v2='text' w='50000' x='0' y='0'>
            <formatted-text>
              <run bold='true' fontsize='15'>REVENUE</run>
              <run>Æ&#10;</run>
              <run fontalignment='1' fontcolor='#47679a' fontname='Tableau Bold' fontsize='10'><![CDATA[THIS <[Parameters].[Parameter 2]> - <[Parameters].[Nope]>]]></run>
            </formatted-text>
          </zone>
          <zone h='10000' id='3' type-v2='title' w='50000' x='50000' y='0'>
            <formatted-text><run>&lt;Sheet Name&gt;</run></formatted-text>
          </zone>
          <zone h='10000' id='4' type-v2='text' w='50000' x='0' y='20000'>
            <formatted-text><run>Plain note</run></formatted-text>
          </zone>
          <zone h='10000' id='6' type-v2='text' w='50000' x='50000' y='20000'>
            <formatted-text><run>Æ&#10;</run></formatted-text>
          </zone>
          <zone h='70000' id='5' name='Source' w='100000' x='0' y='30000' />
        </zone>
      </zones>
    </dashboard>"""
    sheets = worksheet("Source", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    ir = parse(tmp_path, b.workbook(worksheets=sheets, dashboards=board))
    zones = {z.source_ref.rsplit("/", 1)[-1]: z for z in iter_zones(ir.dashboards[0].zones)}
    text = next(z for z in iter_zones(ir.dashboards[0].zones) if z.kind == "text" and z.content)
    runs = [(r.text, r.parameter) for r in text.content]
    assert runs == [
        ("REVENUE", None),
        ("\n", None),
        ("THIS ", None),
        (None, "p_region_pick"),
        (" - <[Parameters].[Nope]>", None),
    ]
    assert text.content[0].style is not None and text.content[0].style.bold
    assert text.content[0].style.size == 15
    style = text.content[3].style
    assert style is not None
    assert (style.color, style.font, style.align, style.size) == (
        "#47679a",
        "Tableau Bold",
        "center",
        10,
    )
    title = next(z for z in iter_zones(ir.dashboards[0].zones) if z.kind == "title")
    assert title.content == [] and title.text == "Board"
    plain = [z for z in iter_zones(ir.dashboards[0].zones) if z.kind == "text" and not z.content]
    assert [z.text for z in plain] == ["Plain note"]
    # 空白的文字物件（只有換行）：空的區塊，與 Tableau 相同
    assert [z.kind for z in iter_zones(ir.dashboards[0].zones) if z.x == 400 and z.y == 120] == [
        "blank"
    ]
    assert zones


# ---------------------------------------------------------------- J4 參考線


def refline(
    n: int,
    formula: str,
    scope: str = "per-table",
    label: str = "automatic",
    extra: str = "",
    values: str = "",
    axis: str = "sum:Sales:qk",
    value: str | None = None,
) -> str:
    head = (
        f"<reference-line axis-column='{ref(axis)}' enable-instant-analytics='false' "
        f"formula='{formula}' id='refline{n}' label-type='{label}' scope='{scope}' "
        f"value-column='{value or ref(axis)}' z-order='{n + 1}'{extra}"
    )
    return head + (f">{values}</reference-line>" if values else " />")


def test_reference_lines(tmp_path: Path) -> None:
    lines = "".join(
        [
            refline(0, "average", "per-pane", "custom", " label='Avg &lt;Value&gt;'"),
            refline(1, "constant", extra=" value='1000'"),
            refline(2, "average", value="[Parameters].[Parameter 1]"),
            refline(
                3,
                "stdev",
                "per-cell",
                "none",
                " symmetric='true' type='population'",
                "<reference-line-value factor='2' /><reference-line-value factor='1' />",
            ),
            refline(
                4,
                "percentile",
                values="<reference-line-value percentile='25' />"
                "<reference-line-value percentile='75' />",
            ),
            refline(5, "max", label="computation", extra=" paired-id='refline6'"),
            refline(6, "median", value=ref("sum:Profit:qk")),
        ]
    )
    sheet = worksheet(
        "Lines",
        MEASURES,
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        cols=ref("sum:Sales:qk"),
        pane_extra=lines,
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheet))
    assert reasons(ir) == []
    got = [
        (
            line.id,
            line.formula,
            line.scope,
            line.label,
            line.label_text,
            line.constant,
            line.percentiles,
            line.factors,
            line.sample,
        )
        for line in ir.worksheets[0].reference_lines
    ]
    assert got == [
        ("refline0", "average", "pane", "custom", "Avg <Value>", None, [], [], True),
        ("refline1", "constant", "table", "automatic", None, 1000.0, [], [], True),
        ("refline2", "average", "table", "automatic", None, None, [], [], True),
        ("refline3", "stdev", "cell", "none", None, None, [], [-2.0, -1.0, 1.0, 2.0], False),
        ("refline4", "percentile", "table", "automatic", None, None, [25.0, 75.0], [], True),
        ("refline5", "max", "table", "computation", None, None, [], [], True),
        ("refline6", "median", "table", "automatic", None, None, [], [], True),
    ]
    first, constant, param, *_, median = ir.worksheets[0].reference_lines
    assert (first.axis.field, first.axis.agg) == ("f_sales", "sum")
    assert first.value == first.axis
    assert constant.value is None
    assert param.value is not None and param.value.model_dump() == {"parameter": "p_top_n"}
    assert median.value is not None and median.value.model_dump()["field"] == "f_profit"
    lines_by_id = {line.id: line for line in ir.worksheets[0].reference_lines}
    assert [lines_by_id[f"refline{i}"].kind for i in range(7)] == [
        "line",
        "line",
        "line",
        "distribution",
        "distribution",
        "line",
        "line",
    ]
    # 參考區間：refline5 與 refline6 之間塗上底色（refline6 沒有寫 paired-id 也一樣）
    assert lines_by_id["refline5"].paired == "refline6"
    assert ir.ignored == []


def test_reference_distributions_bands_and_box_plots(tmp_path: Path) -> None:
    """分位數、信賴區間、百分比分布、盒鬚圖與線條、底色的格式（<table><style> 中依 id 的 refline、refband）。"""
    lines = "".join(
        [
            refline(0, "quantiles", extra=" tile-count='4' fill-above='true'"),
            refline(1, "confidence", extra=" probability='90' paired-distribution-id='refline2'"),
            refline(
                2,
                "average",
                values="<reference-line-value percentage='50' /><reference-line-value percentage='100' />",
            ),
            refline(3, "medianconfidence", extra=" probability='95'"),
            refline(
                4,
                "average",
                "per-cell",
                extra=" boxplot-mark-exclusion='true' boxplot-whisker-type='minmax'",
            ),
        ]
    )
    style = (
        "<style><style-rule element='refline'>"
        "<format attr='stroke-color' id='refline2' value='#898989' />"
        "<format attr='line-pattern-only' id='refline2' value='dotted' />"
        "<format attr='stroke-size' id='refline2' value='3' />"
        "<format attr='text-format' id='refline2' value='n#,##0' />"
        "<format attr='fill-above' id='refline0' value='#ff000080' />"
        "<format attr='line-visibility' id='refline1' value='off' />"
        "</style-rule><style-rule element='refband'>"
        "<format attr='fill-color' id='refline0' value='#123456' />"
        "</style-rule></style>"
    )
    sheet = worksheet(
        "Lines",
        BASIC,
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        pane_extra=lines,
    ).replace("<style />", style, 1)
    ir = parse(tmp_path, b.workbook(worksheets=sheet))
    assert reasons(ir) == [] and ir.ignored == []
    quantiles, confidence, percentages, median, box = ir.worksheets[0].reference_lines
    assert (quantiles.formula, quantiles.kind, quantiles.tiles) == ("quantiles", "distribution", 4)
    assert (quantiles.fill_above, quantiles.above_color, quantiles.fill_color) == (
        True,
        "#ff000080",
        "#123456",
    )
    assert (confidence.formula, confidence.probability, confidence.line_visible) == (
        "confidence",
        90.0,
        False,
    )
    assert (percentages.kind, percentages.percentages) == ("distribution", [50.0, 100.0])
    assert (percentages.line_color, percentages.line_pattern, percentages.line_width) == (
        "#898989",
        "dotted",
        3,
    )
    assert percentages.label_format == "n#,##0"
    assert median.formula == "median_confidence"
    assert (box.kind, box.whiskers, box.hide_marks, box.scope) == (
        "boxplot",
        "minmax",
        True,
        "cell",
    )


def test_reference_lines_not_drawn(tmp_path: Path) -> None:
    """新平台畫不出來的參考線記為略過（不影響數字），找不到參數則不支援。"""
    lines = "".join(
        [
            refline(3, "average", axis="Multiple Values"),
            refline(4, "average", value="[Parameters].[Nope]"),
            refline(5, "percentile", values="<reference-line-value percentile='150' />"),
            refline(6, "mode"),
        ]
    )
    sheet = worksheet(
        "Lines",
        BASIC,
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        pane_extra=lines,
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheet), lenient=True)
    assert ir.worksheets[0].reference_lines == []
    assert reasons(ir) == [
        "參考線的參數 [Parameters].[Nope] 找不到",
        "參考線的百分位數、標準差倍數、百分比、分位數或信賴水準無法解析",
    ]
    ignored = [u.reason for u in ir.ignored]
    assert "參考線、參考區間或分布（mode，per-table）新平台不畫（不影響數字）" in ignored
    assert "Measure Values 軸上的參考線新平台不畫（不影響數字）" in ignored


# ---------------------------------------------------------------- K3 移動計算


def moving_sheet(*calcs: str) -> str:
    instances = [
        *BASIC,
        *(
            instance("[Sales]", "Sum", f"[m{i}:sum:Sales:qk]", "quantitative").replace(
                " />", f">{calc}</column-instance>"
            )
            for i, calc in enumerate(calcs)
        ),
    ]
    encodings = "".join(
        f"<tooltip column='{ref(f'm{i}:sum:Sales:qk')}' />" for i in range(len(calcs))
    )
    return worksheet(
        "Moving",
        instances,
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=encodings,
    )


def test_moving_calculations(tmp_path: Path) -> None:
    sheet = moving_sheet(
        "<table-calc aggregation='Avg' from='-2' ordering-type='Columns' to='0' "
        "type='WindowTotal' window-options='IncludeCurrent' />",
        "<table-calc aggregation='Median' from='-1' ordering-type='Columns' to='1' "
        "type='WindowTotal' window-options='IncludeCurrent,NullIfIncomplete' />",
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheet))
    assert reasons(ir) == []
    assert [(tc.type, tc.preset, tc.options) for tc in ir.worksheets[0].table_calcs] == [
        (
            "moving",
            "table_down",
            {"aggregation": "avg", "start": -2, "end": 0, "null_if_incomplete": False},
        ),
        (
            "moving",
            "table_down",
            {"aggregation": "median", "start": -1, "end": 1, "null_if_incomplete": True},
        ),
    ]


def test_moving_calculation_unsupported_settings(tmp_path: Path) -> None:
    sheet = moving_sheet(
        "<table-calc aggregation='CountD' from='-2' ordering-type='Columns' to='0' type='WindowTotal' />",
        "<table-calc aggregation='Sum' from='1' ordering-type='Columns' to='0' type='WindowTotal' />",
        "<table-calc aggregation='Sum' from='x' ordering-type='Columns' to='0' type='WindowTotal' />",
    )
    ir = parse(tmp_path, b.workbook(worksheets=sheet), lenient=True)
    assert reasons(ir) == [
        "移動計算的設定（countd，-2 到 0）v0 不支援",
        "移動計算的設定（sum，1 到 0）v0 不支援",
        "移動計算的設定（sum，x 到 0）v0 不支援",
    ]


# ---------------------------------------------------------------- I2 dashboard 物件


WINDOW_IDS = {
    "Detail": "00000000-0000-0000-0000-0000000000D1",
    "Target": "00000000-0000-0000-0000-0000000000A1",
}


def windows(extra: str = "") -> str:
    rows = [
        f"<window class='dashboard' name='Detail'><simple-id uuid='{{{WINDOW_IDS['Detail']}}}' /></window>",
        f"<window class='worksheet' name='Target'><simple-id uuid='{{{WINDOW_IDS['Target']}}}' /></window>",
        "<window class='storyboard' name='Story'><simple-id uuid='{00000000-0000-0000-0000-0000000000E1}' /></window>",
    ]
    return "<windows>" + "".join(rows) + extra + "</windows>"


def object_zone(zid: int, button: str) -> str:
    return (
        f"<zone h='10000' id='{zid}' type-v2='dashboard-object' w='10000' x='0' y='0'>"
        f"<button {button}</button></zone>"
    )


def goto(window: str, state: str = "") -> str:
    return f"action='tabdoc:goto-sheet window-id=&quot;{{{window}}}&quot;'>{state}"


def board(name: str, zones: str) -> str:
    return f"""
    <dashboard name='{name}'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones><zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>{zones}</zone></zones>
    </dashboard>"""


def test_buttons_and_web_zones(tmp_path: Path) -> None:
    text_state = (
        "<button-visual-state><caption>Go</caption><tooltip-text>Open detail</tooltip-text>"
        "<button-caption-font-style fontcolor='#ffffff' fontname='Tableau Bold' fontsize='12' />"
        "<format attr='background-color' value='#00aa00' /></button-visual-state>"
    )
    image_state = (
        "<button-visual-state><image-path>Image\\go.png</image-path></button-visual-state>"
    )
    toggle = (
        "action=''><toggle-action>tabdoc:toggle-button-click-action window-id=&quot;{X}&quot; "
        "zone-id=&quot;5&quot; zone-ids=[6, 7]</toggle-action>"
        "<button-visual-state><image-path>Image/hide.png</image-path></button-visual-state>"
        "<button-visual-state><image-path>Image/show.png</image-path></button-visual-state>"
    )
    export = (
        "action='' button-click-action-metadata='pdf'><export-button-action>"
        "tabdoc:abstract-dashboard-button-export-wrapper dashboard-button-export-type=&quot;pdf&quot; "
        "dashboarddoc-id=&quot;{X}&quot;</export-button-action>"
        "<button-visual-state><image-path>Image/pdf.png</image-path></button-visual-state>"
    )
    zones = "".join(
        [
            object_zone(2, goto(WINDOW_IDS["Detail"], text_state)),
            object_zone(3, goto(WINDOW_IDS["Target"].lower(), image_state)),
            object_zone(4, export),
            object_zone(5, toggle),
            "<zone h='10000' id='6' type-v2='text' w='10000' x='0' y='0' hidden-by-user='true'>"
            "<formatted-text><run>Note</run></formatted-text></zone>",
            "<zone h='10000' id='7' type-v2='empty' w='10000' x='0' y='0' />",
            "<zone h='10000' id='8' param='https://example.com/' type-v2='web' w='10000' x='0' y='0' />",
            "<zone h='10000' id='9' type-v2='web' w='10000' x='0' y='0' />",
        ]
    )
    sheet = worksheet("Target", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    xml = b.workbook(
        worksheets=sheet, dashboards=board("Home", zones) + board("Detail", "")
    ).replace("<windows />", windows())
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    home, detail = ir.dashboards
    by_id = {z.id: z for z in iter_zones(home.zones)}
    nav, sheet_nav, export_button, toggle_button = (by_id[f"z{i}"].button for i in range(2, 6))
    assert nav is not None and nav.model_dump(exclude_none=True) == {
        "action": "navigate",
        "target_dashboard": detail.id,
        "zones": [],
        "caption": "Go",
        "tooltip": "Open detail",
        "color": "#ffffff",
        "background": "#00aa00",
    }
    assert sheet_nav is not None
    assert (sheet_nav.target_worksheet, sheet_nav.image) == ("ws_target", "Image/go.png")
    assert export_button is not None
    assert (export_button.action, export_button.export) == ("export", "pdf")
    assert toggle_button is not None
    assert (toggle_button.zones, toggle_button.image, toggle_button.image_hidden) == (
        ["z6", "z7"],
        "Image/hide.png",
        "Image/show.png",
    )
    assert by_id["z6"].hidden
    assert (by_id["z8"].kind, by_id["z8"].url) == ("web", "https://example.com/")
    assert by_id["z9"].kind == "blank"
    assert any("沒有指定網址的網頁區塊" in u.reason for u in ir.ignored)


def test_button_targets_that_cannot_migrate(tmp_path: Path) -> None:
    zones = "".join(
        [
            object_zone(2, goto("00000000-0000-0000-0000-0000000000FF")),
            object_zone(3, goto("00000000-0000-0000-0000-0000000000E1")),
            object_zone(4, goto(WINDOW_IDS["Detail"])),  # Detail 是 Story，不會成為 dashboard
            object_zone(
                5,
                "action=''><toggle-action>tabdoc:toggle-button-click-action zone-ids=[99]</toggle-action>",
            ),
            object_zone(
                6,
                "action=''><export-button-action>dashboard-button-export-type=&quot;crosstab&quot;"
                "</export-button-action>",
            ),
            object_zone(7, "action='tabdoc:something-else'>"),
            "<zone h='10000' id='8' type-v2='dashboard-object' w='10000' x='0' y='0' />",
        ]
    )
    story = """
    <dashboard name='Detail' type='storyboard'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones />
    </dashboard>"""
    xml = b.workbook(dashboards=board("Home", zones) + story).replace("<windows />", windows())
    ir = parse(tmp_path, xml, lenient=True)
    assert sorted(reasons(ir)) == sorted(
        [
            "導覽按鈕的目標視窗找不到",
            "導覽按鈕的目標 dashboard 無法移轉",
            "導覽按鈕的目標 Story 無法移轉",
            "匯出按鈕的格式 crosstab v0 不支援",
            "按鈕的動作 tabdoc:something-else v0 不支援",
            "dashboard 物件（不是按鈕）v0 不支援",
            "Story「Detail」沒有可以移轉的故事點",
        ]
    )
    home = ir.dashboards[0]
    assert all(z.kind in ("container", "blank") for z in iter_zones(home.zones))
    # 顯示／隱藏的區塊不存在（例如已刪除）：Tableau 上按了也沒有作用，改為空白區塊並記在略過的元素
    assert any(
        u.reason == "顯示／隱藏按鈕的區塊 z99 不在此 dashboard 上（Tableau 上按了也沒有作用）"
        for u in ir.ignored
    )


def test_group_and_bin_definitions_and_comments(tmp_path: Path) -> None:
    """群組、數值分組的定義另外記下（編輯器的「編輯群組」）；欄位的註解（<desc>）。"""
    bins = (
        "<bin value='&quot;Coasts&quot;'><value>&quot;East&quot;</value><value>%null%</value></bin>"
        "<bin value='&quot;North&quot;'><value>&quot;North&quot;</value></bin>"
    )
    group = group_column("column='[Region]' default='&quot;Other&quot;' new-bin='true'", bins)
    sized = (
        "<column caption='Sales (bin)' datatype='real' name='[Sales (bin)]' role='dimension' "
        "type='ordinal'><calculation class='bin' decimals='1' formula='[Sales]' peg='2.5' size='10' />"
        "</column>"
    )
    by_parameter = (
        "<column caption='Qty (bin)' datatype='integer' name='[Qty (bin)]' role='dimension' "
        "type='ordinal'><calculation class='bin' decimals='0' formula='[Quantity]' peg='0' "
        "size-parameter='[Parameters].[Parameter 1]' /></column>"
    )
    commented = (
        "<column datatype='string' name='[Region]' role='dimension' type='nominal'>"
        "<desc><formatted-text><run>銷售</run><run fontname='Arial'>區域</run></formatted-text>"
        "</desc></column>"
    )
    columns = group + sized + by_parameter + commented
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=columns)))
    assert reasons(ir) == []
    fields = {f.id: f for ds in ir.datasources for f in ds.fields}
    found = fields["f_region_group"].group
    assert found is not None and found.source == "f_region" and found.other == "Other"
    assert [(g.name, g.members) for g in found.groups] == [
        ("Coasts", ["East", None]),
        ("North", ["North"]),
    ]
    sales_bin = fields["f_sales_bin"].bin
    assert sales_bin is not None
    assert (sales_bin.source, sales_bin.size, sales_bin.peg) == ("f_sales", 10.0, 2.5)
    qty_bin = fields["f_qty_bin"].bin
    top_n = next(p for p in ir.parameters if p.caption == "Top N")
    assert qty_bin is not None and qty_bin.size == ParamRef(parameter=top_n.id)
    assert fields["f_region"].comment == "銷售區域"
