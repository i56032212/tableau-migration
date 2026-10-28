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
