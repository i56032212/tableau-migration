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
