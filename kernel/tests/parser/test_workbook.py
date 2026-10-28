from __future__ import annotations

from pathlib import Path

import pytest

from tmig.ir import IR, CategoricalFilter, RangeFilter, TopNFilter, iter_zones
from tmig.parser.workbook import WorkbookParseError, parse_workbook

from . import builder as b
from .builder import DS, calc, instance, ref, worksheet


def parse(tmp_path: Path, xml: str, **kwargs: bool) -> IR:
    return parse_workbook(b.write_twb(tmp_path, xml), **kwargs)


def reasons(ir: IR) -> list[str]:
    return [u.reason for u in ir.unsupported]


def field(ir: IR, name: str):
    return next(f for f in ir.datasources[0].fields if f.name == name)


def sheet(ir: IR, index: int = 0):
    return ir.worksheets[index]


BASIC = [
    instance("[Region]", "None", "[none:Region:nk]"),
    instance("[Category]", "None", "[none:Category:nk]"),
    instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
]


# ---------------------------------------------------------------- 資料源與欄位


def test_extract_datasource_fields(tmp_path: Path) -> None:
    ir = parse(tmp_path, b.workbook())
    ds = ir.datasources[0]
    assert ds.id == "ds_orders"
    assert ds.name == DS
    assert ds.connection.hyper_file == "Data/Extracts/test.hyper"
    assert ds.connection.table == "Extract.Extract"
    assert ds.settings.week_start == "sunday"
    assert [f.id for f in ds.fields] == [
        "f_region",
        "f_category",
        "f_order_id",
        "f_order_date",
        "f_sales",
        "f_profit",
        "f_quantity",
    ]
    sales = field(ir, "[Sales]")
    assert (sales.role, sales.datatype, sales.default_agg, sales.column) == (
        "measure",
        "real",
        "sum",
        "Sales",
    )
    region = field(ir, "[Region]")
    assert (region.role, region.default_agg) == ("dimension", None)
    assert ir.unsupported == []


def test_physical_column_from_extract_metadata(tmp_path: Path) -> None:
    inner = b.metadata(rename={"Sales": "Sales1"})
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extract_inner=inner)))
    assert field(ir, "[Sales]").column == "Sales1"


def test_physical_column_from_extract_cols_map(tmp_path: Path) -> None:
    inner = (
        "<cols><map key='[Region]' value='[Extract].[Region (orders)]' />"
        "<map key='[Sales]' value='[Extract].[Sales]' /></cols>"
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extract_inner=inner)))
    assert field(ir, "[Region]").column == "Region (orders)"
    # 萃取連線沒有 metadata-records 時，欄位清單取自來源連線
    assert field(ir, "[Profit]").column == "Profit"


def test_column_overrides(tmp_path: Path) -> None:
    columns = (
        "<column caption='Revenue' datatype='real' default-format='n#,##0' name='[Sales]' role='measure' type='quantitative' aggregation='Avg' />"
        "<column datatype='integer' hidden='true' name='[Quantity]' role='dimension' type='ordinal' />"
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=columns)))
    sales = field(ir, "[Sales]")
    assert (sales.id, sales.caption, sales.format, sales.default_agg) == (
        "f_revenue",
        "Revenue",
        "n#,##0",
        "avg",
    )
    quantity = field(ir, "[Quantity]")
    assert (quantity.hidden, quantity.role, quantity.default_agg) == (True, "dimension", None)


def test_member_aliases_are_unsupported(tmp_path: Path) -> None:
    columns = (
        "<column datatype='string' name='[Region]' role='dimension' type='nominal'>"
        "<aliases><alias key='&quot;East&quot;' value='東區' /></aliases></column>"
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=columns)))
    assert any("成員別名" in r for r in reasons(ir))


def test_direct_hyper_datasource(tmp_path: Path) -> None:
    ds = f"""
    <datasource inline='true' name='simple Extract' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='simple' name='leaf'>
            <connection class='hyper' dbname='Data/Extracts/simple.hyper' schema='Extract' tablename='Extract' />
          </named-connection>
        </named-connections>
        <relation connection='leaf' name='Extract' table='[Extract].[Extract]' type='table' />
        {b.metadata(rename={"Region": "region_col"})}
      </connection>
    </datasource>"""
    ir = parse(tmp_path, b.workbook(ds))
    assert ir.datasources[0].connection.hyper_file == "Data/Extracts/simple.hyper"
    assert field(ir, "[Region]").column == "region_col"


LIVE = f"""
    <datasource caption='Live' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='db' name='oracle.1'>
            <connection class='oracle' server='db' />
          </named-connection>
        </named-connections>
        <relation connection='oracle.1' name='ORDERS' table='[ORDERS]' type='table' />
        {b.metadata("[ORDERS]")}
      </connection>
    </datasource>"""


def test_live_connection_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkbookParseError, match="Live 連線"):
        parse(tmp_path, b.workbook(LIVE))


def test_lenient_mode_parses_live_connection(tmp_path: Path) -> None:
    ir = parse(tmp_path, b.workbook(LIVE), lenient=True)
    assert ir.datasources[0].connection.hyper_file == "(無)"
    assert any("Live 連線" in r for r in reasons(ir))


def test_published_and_tde_are_rejected(tmp_path: Path) -> None:
    published = f"<datasource caption='P' name='{DS}'><connection class='sqlproxy' /></datasource>"
    with pytest.raises(WorkbookParseError, match="Published Data Source"):
        parse(tmp_path, b.workbook(published))
    tde = b.extract_datasource().replace("class='hyper'", "class='dataengine'")
    with pytest.raises(WorkbookParseError, match=r"舊版 \.tde"):
        parse(tmp_path, b.workbook(tde))


def test_multi_table_extract_is_rejected(tmp_path: Path) -> None:
    inner = (
        "<relation name='A' table='[Extract].[A]' type='table' />"
        "<relation name='B' table='[Extract].[B]' type='table' />"
    )
    xml = b.workbook(b.extract_datasource()).replace(
        "<relation name='Extract' table='[Extract].[Extract]' type='table' />", inner
    )
    with pytest.raises(WorkbookParseError, match="不是單一資料表"):
        parse(tmp_path, xml)


def test_twbx_hyper_must_be_packaged(tmp_path: Path) -> None:
    xml = b.workbook()
    ok = parse_workbook(b.write_twbx(tmp_path, xml, {"Data/Extracts/test.hyper": b"x"}))
    assert ok.datasources[0].connection.hyper_file == "Data/Extracts/test.hyper"
    renamed = xml.replace("Data/Extracts/test.hyper", "C:/Temp/test.hyper")
    by_name = parse_workbook(
        b.write_twbx(tmp_path, renamed, {"Data/Extracts/test.hyper": b"x"}, "b.twbx")
    )
    assert by_name.datasources[0].connection.hyper_file == "Data/Extracts/test.hyper"
    with pytest.raises(WorkbookParseError, match=r"不在 \.twbx 內"):
        parse_workbook(b.write_twbx(tmp_path, xml, {}, "c.twbx"))


def test_week_start_option(tmp_path: Path) -> None:
    ds = b.extract_datasource(extra="<date-options start-of-week='monday' />")
    assert parse(tmp_path, b.workbook(ds)).datasources[0].settings.week_start == "monday"


def test_unknown_datasource_element(tmp_path: Path) -> None:
    ds = b.extract_datasource(extra="<mystery-thing />")
    assert any("未知元素 <mystery-thing>" in r for r in reasons(parse(tmp_path, b.workbook(ds))))


# ---------------------------------------------------------------- 計算欄位


CALCS = "".join(
    [
        calc("Calculation_1", "Margin", "[Profit] / [Sales]"),
        calc("Calculation_2", "Ratio", "SUM([Profit]) / SUM([Sales])"),
        calc("Calculation_3", "Region Sales", "{FIXED [Region] : SUM([Sales])}"),
        calc("Calculation_4", "Rank", "RANK(SUM([Sales]))", "integer"),
        calc("Calculation_5", "Bad", "HEXBINX([Sales], [Profit])", "real", "dimension"),
        calc("Calculation_6", "Uses Param", "[Sales] * [Parameters].[Parameter 1]"),
    ]
)


def test_calc_levels_and_ast(tmp_path: Path) -> None:
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=CALCS)))
    levels = {f.caption: f.calc.level for f in ir.datasources[0].fields if f.calc is not None}
    assert levels == {
        "Margin": "row",
        "Ratio": "aggregate",
        "Region Sales": "lod",
        "Rank": "table_calc",
        "Bad": None,
        "Uses Param": "row",
    }
    margin = field(ir, "[Calculation_1]")
    assert margin.id == "f_margin"
    assert margin.column is None
    assert margin.calc is not None and margin.calc.ast is not None
    left = margin.calc.ast["left"]
    assert (left["node"], left["target"], left["id"]) == ("field", "field", "f_profit")
    param = field(ir, "[Calculation_6]")
    assert param.calc is not None and param.calc.ast is not None
    assert param.calc.ast["right"]["id"] == "p_top_n"
    assert any("Bad" in r and "HEXBINX" in r for r in reasons(ir))


def test_group_calcs_and_sets(tmp_path: Path) -> None:
    # 群組（K1）轉成公式；集合與其他特殊類型的計算欄位仍不支援
    columns = (
        "<column caption='Region Group' datatype='string' name='[Region (group)]' role='dimension' type='nominal'>"
        "<calculation class='categorical-bin' column='[Region]' new-bin='true' /></column>"
        "<column caption='Odd' datatype='string' name='[Odd]' role='dimension' type='nominal'>"
        "<calculation class='mystery' /></column>"
    )
    extra = "<group caption='Top Customers' name='[Set 1]' name-style='unqualified' user:ui-builder='filter-group' />"
    ir = parse(tmp_path, b.workbook(b.extract_datasource(columns=columns, extra=extra)))
    assert reasons(ir) == ["計算欄位「Odd」的類型 mystery（集合等）v0 不支援"]
    assert field(ir, "[Region (group)]").calc is not None
    assert any("集合或合併欄位" in u.reason for u in ir.ignored)


# ---------------------------------------------------------------- 參數


def test_parameters(tmp_path: Path) -> None:
    ir = parse(tmp_path, b.workbook())
    top, pick, start = ir.parameters
    assert (top.id, top.caption, top.datatype, top.value) == ("p_top_n", "Top N", "integer", 5)
    assert top.allowed.model_dump() == {"kind": "range", "min": 1, "max": 10, "step": 1}
    assert pick.allowed.model_dump() == {"kind": "list", "values": ["East", "West"]}
    assert pick.value == "East"
    assert (start.allowed.kind, start.value) == ("all", "2024-01-01")


# ---------------------------------------------------------------- 工作表


def basic_sheet(**kwargs: str) -> str:
    defaults = {
        "rows": ref("none:Region:nk"),
        "cols": ref("sum:Sales:qk"),
        "encodings": f"<color column='{ref('none:Category:nk')}' />",
    }
    defaults.update(kwargs)
    return worksheet("Sheet A", BASIC, **defaults)


def test_shelves_and_marks(tmp_path: Path) -> None:
    ir = parse(tmp_path, b.workbook(worksheets=basic_sheet()))
    ws = sheet(ir)
    assert (ws.id, ws.datasource) == ("ws_sheet_a", "ds_orders")
    assert [u.model_dump() for u in ws.rows] == [
        {"field": "f_region", "agg": None, "date": None, "continuous": False, "table_calc": None}
    ]
    assert ws.columns[0].agg == "sum" and ws.columns[0].continuous
    assert ws.marks.type == "bar"
    assert ws.marks.color is not None and ws.marks.color.field == "f_category"
    assert ir.unsupported == []


def test_nested_dimensions_and_dates(tmp_path: Path) -> None:
    instances = [
        *BASIC,
        instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal"),
        instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative"),
    ]
    xml = b.workbook(
        worksheets=worksheet(
            "Dates",
            instances,
            rows=f"({ref('none:Region:nk')} / {ref('yr:Order Date:ok')})",
            cols=ref("tmn:Order Date:qk"),
        )
    )
    ws = sheet(parse(tmp_path, xml))
    assert [(u.field, u.date and u.date.model_dump()) for u in ws.rows] == [
        ("f_region", None),
        ("f_order_date", {"kind": "part", "unit": "year"}),
    ]
    assert ws.columns[0].date is not None and ws.columns[0].date.kind == "trunc"
    assert ws.columns[0].continuous


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"encodings": f"<geometry column='{ref('none:Category:nk')}' />"}, "標記編碼 <geometry>"),
        ({"pane_extra": "<reference-line axis-column='x' />"}, "參考線"),
        ({"view_extra": "<strange-element />"}, "未知元素 <strange-element>"),
        ({"mark": "Map"}, "標記類型 Map"),
    ],
)
def test_worksheet_unsupported(tmp_path: Path, kwargs: dict[str, str], message: str) -> None:
    ir = parse(tmp_path, b.workbook(worksheets=basic_sheet(**kwargs)))
    assert any(message in r for r in reasons(ir)), reasons(ir)


def test_multiple_panes_must_match(tmp_path: Path) -> None:
    same = (
        "<pane id='1'><mark class='Bar' /><encodings><color column='"
        + ref("none:Category:nk")
        + "' /></encodings></pane>"
    )
    ir = parse(tmp_path, b.workbook(worksheets=basic_sheet(panes_extra=same)))
    assert ir.unsupported == []
    other = "<pane id='1'><mark class='Line' /></pane>"
    ir = parse(tmp_path, b.workbook(worksheets=basic_sheet(panes_extra=other)))
    assert any("多個標記卡" in r for r in reasons(ir))


def test_blending_is_unsupported(tmp_path: Path) -> None:
    sources = f"<datasource name='{DS}' /><datasource name='federated.other' />"
    ir = parse(tmp_path, b.workbook(worksheets=basic_sheet(datasources=sources)))
    assert ir.worksheets == []
    assert any("Data Blending" in r for r in reasons(ir))


def test_ad_hoc_calc_in_worksheet(tmp_path: Path) -> None:
    deps = calc("Calculation_99", "Double Sales", "[Sales] * 2")
    instances = [
        *BASIC,
        instance("[Calculation_99]", "Sum", "[sum:Calculation_99:qk]", "quantitative"),
    ]
    xml = b.workbook(
        worksheets=worksheet(
            "Adhoc",
            instances,
            rows=ref("none:Region:nk"),
            cols=ref("sum:Calculation_99:qk"),
            deps_extra=deps,
        )
    )
    ir = parse(tmp_path, xml)
    added = field(ir, "[Calculation_99]")
    assert added.calc is not None and added.calc.level == "row"
    assert sheet(ir).columns[0].field == added.id


# ---------------------------------------------------------------- 篩選


def filters_sheet(view_extra: str, instances: list[str] | None = None) -> str:
    return worksheet(
        "Filters",
        instances
        or [
            *BASIC,
            instance("[Order Date]", "None", "[none:Order Date:qk]", "quantitative"),
            instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal"),
        ],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=view_extra,
    )


def member(level: str, value: str) -> str:
    return f"<groupfilter function='member' level='[{level}]' member='{value}' />"


def test_categorical_filters(tmp_path: Path) -> None:
    include = (
        f"<filter class='categorical' column='{ref('none:Category:nk')}'>"
        "<groupfilter function='union' user:ui-enumeration='inclusive'>"
        f"{member('none:Category:nk', '&quot;Furniture&quot;')}{member('none:Category:nk', '&quot;Technology&quot;')}"
        "</groupfilter></filter>"
    )
    exclude = (
        f"<filter class='categorical' column='{ref('none:Region:nk')}' context='true'>"
        "<groupfilter function='except' user:ui-enumeration='exclusive'>"
        "<groupfilter function='level-members' level='[none:Region:nk]' />"
        f"{member('none:Region:nk', '&quot;East&quot;')}</groupfilter></filter>"
    )
    year = (
        f"<filter class='categorical' column='{ref('yr:Order Date:ok')}'>"
        f"{member('yr:Order Date:ok', '2024')}</filter>"
    )
    all_values = (
        f"<filter class='categorical' column='{ref('none:Category:nk')}'>"
        "<groupfilter function='level-members' level='[none:Category:nk]' user:ui-enumeration='all' /></filter>"
    )
    ir = parse(
        tmp_path, b.workbook(worksheets=filters_sheet(include + exclude + year + all_values))
    )
    f1, f2, f3, f4 = sheet(ir).filters
    assert isinstance(f1, CategoricalFilter)
    assert (f1.stage, f1.mode, f1.values) == ("dimension", "include", ["Furniture", "Technology"])
    assert isinstance(f2, CategoricalFilter)
    assert (f2.stage, f2.mode, f2.values) == ("context", "exclude", ["East"])
    assert isinstance(f3, CategoricalFilter)
    assert (f3.field.date and f3.field.date.unit, f3.values) == ("year", [2024])
    assert isinstance(f4, CategoricalFilter) and f4.mode == "all"
    assert len({f.id for f in sheet(ir).filters}) == 4


def test_top_n_filters(tmp_path: Path) -> None:
    def top(count: str, end: str, direction: str) -> str:
        return (
            f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
            f"<groupfilter count='{count}' end='{end}' function='end' units='records'>"
            f"<groupfilter direction='{direction}' expression='SUM([Sales])' function='order'>"
            "<groupfilter function='level-members' level='[none:Region:nk]' />"
            "</groupfilter></groupfilter></filter>"
        )

    ir = parse(
        tmp_path,
        b.workbook(worksheets=filters_sheet(top("[Parameters].[Parameter 1]", "top", "DESC"))),
    )
    flt = sheet(ir).filters[0]
    assert isinstance(flt, TopNFilter)
    assert (flt.stage, flt.direction, flt.by) == ("top_n", "top", "SUM([Sales])")
    assert flt.count.model_dump() == {"parameter": "p_top_n"}  # type: ignore[union-attr]
    for end, direction, expected in [
        ("bottom", "DESC", "bottom"),
        ("top", "ASC", "bottom"),
        ("bottom", "ASC", "top"),
    ]:
        ir = parse(tmp_path, b.workbook(worksheets=filters_sheet(top("3", end, direction))))
        flt = sheet(ir).filters[0]
        assert isinstance(flt, TopNFilter)
        assert (flt.count, flt.direction) == (3, expected)


def test_range_filters(tmp_path: Path) -> None:
    dates = (
        f"<filter class='quantitative' column='{ref('none:Order Date:qk')}' included-values='in-range-or-null'>"
        "<min>#2023-01-01#</min><max>#2024-12-31 00:00:00#</max></filter>"
    )
    measure = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>1000</min></filter>"
    )
    ir = parse(tmp_path, b.workbook(worksheets=filters_sheet(dates + measure)))
    f1, f2 = sheet(ir).filters
    assert isinstance(f1, RangeFilter) and isinstance(f2, RangeFilter)
    assert (f1.stage, f1.min, f1.max, f1.include_null) == (
        "dimension",
        "2023-01-01",
        "2024-12-31",
        True,
    )
    assert (f2.stage, f2.min, f2.max) == ("measure", 1000.0, None)


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        (
            f"<filter class='relative-date' column='{ref('none:Order Date:qk')}' period-type='year' />",
            "相對日期",
        ),
        (
            f"<filter class='categorical' column='{ref('none:Region:nk')}'><groupfilter function='filter' expression='SUM([Sales]) &gt; 0' /></filter>",
            "function=filter",
        ),
        (
            f"<filter class='categorical' column='{ref('sum:Sales:qk')}'><groupfilter function='level-members' /></filter>",
            "量值上的類別篩選",
        ),
        (
            f"<filter class='categorical' column='{ref('none:Missing:nk')}'><groupfilter function='level-members' /></filter>",
            "找不到欄位實例",
        ),
    ],
)
def test_unsupported_filters(tmp_path: Path, xml: str, message: str) -> None:
    ir = parse(tmp_path, b.workbook(worksheets=filters_sheet(xml)))
    assert sheet(ir).filters == []
    assert any(message in r for r in reasons(ir)), reasons(ir)


def test_datasource_filter(tmp_path: Path) -> None:
    extra = (
        "<column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />"
        "<filter class='categorical' column='[none:Region:nk]' filter-group='2'>"
        "<groupfilter function='member' level='[none:Region:nk]' member='&quot;West&quot;' /></filter>"
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extra=extra)))
    flt = ir.datasources[0].filters[0]
    assert isinstance(flt, CategoricalFilter)
    assert (flt.stage, flt.values, flt.field.field) == ("datasource", ["West"], "f_region")


# ---------------------------------------------------------------- 排序


def test_sorts(tmp_path: Path) -> None:
    sorts = (
        f"<computed-sort column='{ref('none:Region:nk')}' direction='DESC' using='{ref('sum:Sales:qk')}' />"
        f"<manual-sort column='{ref('none:Category:nk')}' direction='ASC'><dictionary>"
        "<bucket>&quot;Technology&quot;</bucket><bucket>&quot;Furniture&quot;</bucket><bucket>%all%</bucket>"
        "</dictionary></manual-sort>"
        f"<sort class='alphabetic' column='{ref('none:Region:nk')}' direction='ASC' />"
        "<shelf-sorts>"
        f"<shelf-sort-v2 dimension-to-sort='{ref('none:Category:nk')}' direction='DESC' measure-to-sort-by='{ref('sum:Sales:qk')}' shelf='rows' />"
        "</shelf-sorts>"
    )
    ws = sheet(parse(tmp_path, b.workbook(worksheets=basic_sheet(view_extra=sorts))))
    computed, manual, alphabetic, shelf = ws.sorts
    assert (computed.field, computed.kind, computed.order) == ("f_region", "by_field", "desc")
    assert computed.by is not None and computed.by.agg == "sum"
    assert (manual.kind, manual.manual_values) == ("manual", ["Technology", "Furniture"])
    assert alphabetic.kind == "alphabetic"
    assert (shelf.field, shelf.kind) == ("f_category", "by_field")


# ---------------------------------------------------------------- Table Calculation


def tc_sheet(table_calc: str, encodings: str | None = None, extra: list[str] | None = None) -> str:
    instances = [
        *BASIC,
        instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal"),
        instance("[Sales]", "Sum", "[cum:sum:Sales:qk]", "quantitative", table_calc),
        *(extra or []),
    ]
    return worksheet(
        "Calcs",
        instances,
        rows=ref("none:Region:nk"),
        cols=f"{ref('yr:Order Date:ok')} / {ref('sum:Sales:qk')}",
        encodings=encodings
        if encodings is not None
        else f"<color column='{ref('none:Category:nk')}' /><text column='{ref('cum:sum:Sales:qk')}' />",
    )


@pytest.mark.parametrize(
    ("table_calc", "preset", "addressing", "inferred"),
    [
        (
            "<table-calc ordering-type='Columns' type='CumTotal' />",
            "table_down",
            ["f_region", "f_category"],
            False,
        ),
        (
            "<table-calc ordering-type='Rows' type='CumTotal' />",
            "table_across",
            ["f_order_date", "f_category"],
            False,
        ),
        ("<table-calc type='CumTotal' />", "table_across", ["f_order_date", "f_category"], True),
        (
            f"<table-calc ordering-field='[{DS}].[Region]' ordering-type='Field' type='CumTotal' />",
            "specific",
            ["f_region"],
            False,
        ),
        (
            f"<table-calc ordering-type='Field' type='CumTotal'><order field='[{DS}].[Category]' /><order field='{ref('none:Region:nk')}' /></table-calc>",
            "specific",
            ["f_category", "f_region"],
            False,
        ),
    ],
)
def test_table_calc_addressing(
    tmp_path: Path, table_calc: str, preset: str, addressing: list[str], inferred: bool
) -> None:
    ws = sheet(parse(tmp_path, b.workbook(worksheets=tc_sheet(table_calc))))
    (tc,) = ws.table_calcs
    assert (tc.type, tc.preset, tc.addressing, tc.inferred) == (
        "running_total",
        preset,
        addressing,
        inferred,
    )
    assert tc.options == {"aggregation": "sum"}
    assert ws.marks.label[0].table_calc == tc.id


def test_rank_and_percent_of_total(tmp_path: Path) -> None:
    rank = "<table-calc ordering-type='Columns' rank-options='Dense,Ascending' type='Rank' />"
    ws = sheet(parse(tmp_path, b.workbook(worksheets=tc_sheet(rank))))
    assert ws.table_calcs[0].type == "rank"
    assert ws.table_calcs[0].options == {"rank_type": "dense", "direction": "asc"}
    pct = "<table-calc ordering-type='Columns' type='PctTotal' />"
    ws = sheet(parse(tmp_path, b.workbook(worksheets=tc_sheet(pct))))
    assert ws.table_calcs[0].type == "percent_of_total"


def test_unsupported_table_calcs(tmp_path: Path) -> None:
    ir = parse(
        tmp_path,
        b.workbook(worksheets=tc_sheet("<table-calc ordering-type='Columns' type='Difference' />")),
    )
    assert any("Difference" in r for r in reasons(ir))
    ir = parse(
        tmp_path,
        b.workbook(worksheets=tc_sheet("<table-calc ordering-type='PaneDown' type='CumTotal' />")),
    )
    assert any("ordering-type=PaneDown" in r for r in reasons(ir))


def test_formula_table_calc_without_settings_is_inferred(tmp_path: Path) -> None:
    columns = calc("Calculation_4", "Rank", "RANK(SUM([Sales]))", "integer")
    extra = [instance("[Calculation_4]", "User", "[usr:Calculation_4:qk]", "quantitative")]
    xml = b.workbook(
        b.extract_datasource(columns=columns),
        worksheets=tc_sheet(
            "<table-calc ordering-type='Columns' type='CumTotal' />",
            encodings=f"<text column='{ref('usr:Calculation_4:qk')}' />",
            extra=extra,
        ),
    )
    ws = sheet(parse(tmp_path, xml))
    formula = next(tc for tc in ws.table_calcs if tc.type == "formula")
    assert (formula.preset, formula.inferred, formula.addressing) == (
        "table_across",
        True,
        ["f_order_date"],
    )
    assert ws.marks.label[0].table_calc == formula.id


def test_table_calc_filter_stage(tmp_path: Path) -> None:
    flt = (
        f"<filter class='quantitative' column='{ref('cum:sum:Sales:qk')}' included-values='in-range'>"
        "<max>100</max></filter>"
    )
    instances = [
        *BASIC,
        instance(
            "[Sales]",
            "Sum",
            "[cum:sum:Sales:qk]",
            "quantitative",
            "<table-calc ordering-type='Columns' type='CumTotal' />",
        ),
    ]
    xml = b.workbook(
        worksheets=worksheet(
            "TC filter",
            instances,
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            view_extra=flt,
        )
    )
    ws = sheet(parse(tmp_path, xml))
    (f,) = ws.filters
    assert isinstance(f, RangeFilter)
    assert (f.stage, f.field.table_calc) == ("table_calc", ws.table_calcs[0].id)


# ---------------------------------------------------------------- dashboard 與動作


def zone(zid: int, x: int, y: int, w: int, h: int, attrs: str = "", inner: str = "") -> str:
    return f"<zone h='{h}' id='{zid}' w='{w}' x='{x}' y='{y}' {attrs}>{inner}</zone>"


def dashboard(
    zones: str,
    size: str = "<size maxheight='800' maxwidth='1000' minheight='800' minwidth='1000' sizing-mode='fixed' />",
) -> str:
    return f"<dashboard name='Overview'><style />{size}<zones>{zones}</zones></dashboard>"


def test_dashboard_zones(tmp_path: Path) -> None:
    flt = (
        f"<filter class='categorical' column='{ref('none:Category:nk')}'>"
        "<groupfilter function='level-members' level='[none:Category:nk]' /></filter>"
    )
    inner = "".join(
        [
            zone(2, 0, 0, 100000, 10000, "type-v2='title'"),
            zone(3, 0, 10000, 80000, 90000, "name='Sheet A'"),
            zone(
                4,
                80000,
                10000,
                20000,
                10000,
                f"mode='checkdropdown' name='Sheet A' param='{ref('none:Category:nk')}' type-v2='filter'",
            ),
            zone(
                5,
                80000,
                20000,
                20000,
                10000,
                "mode='slider' param='[Parameters].[Parameter 1]' type-v2='paramctrl'",
            ),
            zone(
                6,
                80000,
                30000,
                20000,
                10000,
                "param='[Parameters].[Parameter 2]' type-v2='paramctrl'",
            ),
            zone(
                7,
                80000,
                40000,
                20000,
                20000,
                f"name='Sheet A' param='{ref('none:Category:nk')}' type-v2='color'",
            ),
            zone(
                8,
                80000,
                60000,
                20000,
                10000,
                "type-v2='text'",
                "<formatted-text><run>Hello </run><run>world</run></formatted-text>",
            ),
            zone(9, 80000, 70000, 20000, 10000, "param='Image/logo.png' type-v2='bitmap'"),
            zone(10, 80000, 80000, 20000, 10000, "type-v2='empty'"),
            zone(11, 80000, 90000, 20000, 10000, "type-v2='web'"),
        ]
    )
    root = zone(
        1,
        0,
        0,
        100000,
        100000,
        "_.fcp.SetMembershipControl.false...type='layout-basic' _.fcp.SetMembershipControl.true...type-v2='layout-basic'",
        inner,
    )
    xml = b.workbook(worksheets=basic_sheet(view_extra=flt), dashboards=dashboard(root))
    ir = parse(tmp_path, xml)
    (db,) = ir.dashboards
    assert (db.id, db.size.width, db.size.height) == ("db_overview", 1000, 800)
    zones = {z.id: z for z in iter_zones(db.zones)}
    assert zones["z1"].kind == "container"
    assert (zones["z2"].kind, zones["z2"].text) == ("title", "Overview")
    assert (zones["z3"].kind, zones["z3"].worksheet) == ("worksheet", "ws_sheet_a")
    assert (zones["z3"].x, zones["z3"].y, zones["z3"].w, zones["z3"].h) == (0, 80, 800, 720)
    assert (zones["z4"].kind, zones["z4"].control) == ("filter", "multi_dropdown")
    assert zones["z4"].field is not None and zones["z4"].field.field == "f_category"
    assert (zones["z5"].kind, zones["z5"].parameter, zones["z5"].control) == (
        "parameter",
        "p_top_n",
        "single_slider",
    )
    assert zones["z6"].control == "single_dropdown"
    assert (zones["z7"].kind, zones["z7"].legend) == ("legend", "color")
    assert (zones["z8"].kind, zones["z8"].text) == ("text", "Hello world")
    assert (zones["z9"].kind, zones["z9"].image) == ("image", "Image/logo.png")
    assert zones["z10"].kind == "blank"
    assert zones["z11"].kind == "blank"
    assert reasons(ir) == ["dashboard 區塊類型 web v0 不支援"]
    assert ir.unsupported[0].source_ref == zones["z11"].source_ref


def test_automatic_dashboard_is_supported(tmp_path: Path) -> None:
    xml = b.workbook(
        worksheets=basic_sheet(),
        dashboards=dashboard(
            zone(1, 0, 0, 100000, 100000, "name='Sheet A'"), size="<size sizing-mode='automatic' />"
        ),
    )
    ir = parse(tmp_path, xml)
    board = ir.dashboards[0]
    assert (board.size.width, board.size.height, board.sizing) == (1366, 768, "automatic")
    assert reasons(ir) == []
    assert any("自動尺寸" in u.reason for u in ir.ignored)


def test_zone_for_missing_worksheet(tmp_path: Path) -> None:
    xml = b.workbook(
        worksheets=basic_sheet(), dashboards=dashboard(zone(1, 0, 0, 100000, 100000, "name='Nope'"))
    )
    ir = parse(tmp_path, xml)
    assert ir.dashboards[0].zones[0].kind == "blank"
    assert any("Nope" in r for r in reasons(ir))


def test_actions(tmp_path: Path) -> None:
    actions = (
        "<action caption='Highlight 1' name='[Action1]'><activation type='on-select' />"
        "<source dashboard='Overview' type='sheet' /><command command='tsc:brush' /></action>"
        "<action caption='Filter 1' name='[Action2]'><activation type='on-select' />"
        "<source dashboard='Overview' type='sheet' /><command command='tsc:tsl-filter' /></action>"
    )
    ir = parse(tmp_path, b.workbook(worksheets=basic_sheet(), actions=actions))
    # 篩選動作的來源 dashboard 不存在：無法轉成 IR
    assert reasons(ir) == ["篩選動作「Filter 1」的來源工作表無法移轉"]
    assert ir.actions == []
    assert any("醒目提示" in u.reason for u in ir.ignored)


def test_unknown_workbook_element(tmp_path: Path) -> None:
    ir = parse(tmp_path, b.workbook(extra="<shiny-new-feature />"))
    assert any("<shiny-new-feature>" in r for r in reasons(ir))


def test_ids_are_unique_for_duplicate_names(tmp_path: Path) -> None:
    sheets = basic_sheet() + basic_sheet().replace("Sheet A", "Sheet-A")
    ir = parse(tmp_path, b.workbook(worksheets=sheets))
    assert [w.id for w in ir.worksheets] == ["ws_sheet_a", "ws_sheet_a_2"]
