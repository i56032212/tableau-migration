"""桌面版編輯器的 IR 操作（tmig.edit）：工作表的新增、修改、複製、刪除，計算欄位與參數。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.edit import (
    EditError,
    add_analytics_field,
    add_dashboard,
    add_story,
    add_worksheet,
    catalog,
    check_formula,
    duplicate_dashboard,
    duplicate_worksheet,
    new_id,
    outline,
    remove_action,
    remove_calc,
    remove_dashboard,
    remove_hierarchy,
    remove_parameter,
    remove_story,
    remove_worksheet,
    replace_dashboard,
    replace_story,
    replace_worksheet,
    upsert_action,
    upsert_calc,
    upsert_hierarchy,
    upsert_parameter,
)
from tmig.ir import IR, FieldUse, load_ir
from tmig.sqlgen.query import worksheet_sql

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "golden.ir.json"


@pytest.fixture
def ir() -> IR:
    return load_ir(DEMO)


def test_catalog_lists_fields_parameters_and_palettes(ir: IR) -> None:
    data = catalog(ir)
    fields = {f["id"]: f for f in data["datasources"][0]["fields"]}
    assert fields["f_sales"]["role"] == "measure"
    assert fields["f_region"]["kind"] == "column"
    calc = fields["f_calc_5418932014720188416"]
    assert calc["kind"] == "calc" and calc["level"] == "table_calc" and calc["formula"]
    assert data["parameters"][0]["id"] == "p_parameter_1"
    assert data["parameters"][0]["allowed"]["kind"] in ("all", "list", "range")
    assert any(p["name"] == "tableau10_10_0" for p in data["palettes"])


def test_check_formula_reports_level_and_role(ir: IR) -> None:
    check = check_formula(ir, "ds_orders", "SUM([Profit]) / SUM([Sales])")
    assert (check.datatype, check.level, check.role) == ("real", "aggregate", "measure")
    assert check_formula(ir, "ds_orders", "[Region] + ' / ' + [Segment]").role == "dimension"
    assert check_formula(ir, "ds_orders", "RUNNING_SUM(SUM([Sales]))").level == "table_calc"
    assert check_formula(ir, "ds_orders", "{FIXED [Region] : SUM([Sales])}").level == "lod"
    with pytest.raises(EditError, match="公式有誤"):
        check_formula(ir, "ds_orders", "SUM([Nope])")
    with pytest.raises(EditError, match="公式有誤"):
        check_formula(ir, "ds_orders", "SUM([Sales]")


def test_upsert_calc_creates_and_updates(ir: IR) -> None:
    ir, field = upsert_calc(ir, "ds_orders", "Profit Ratio 2", "SUM([Profit]) / SUM([Sales])")
    assert field.id == "f_profit_ratio_2" and field.role == "measure"
    assert field.calc is not None and field.calc.level == "aggregate" and field.calc.ast
    assert field.name.startswith("[Calculation_")
    # 修改：保留 id 與內部名稱
    ir, updated = upsert_calc(ir, "ds_orders", "Margin", "SUM([Profit])", field.id)
    assert (updated.id, updated.name) == (field.id, field.name)
    assert updated.caption == "Margin"
    with pytest.raises(EditError, match="已經有名為"):
        upsert_calc(ir, "ds_orders", "Sales", "1")
    # 新的計算欄位可以放到工作表上查詢
    sheet = next(w for w in ir.worksheets if w.id == "ws_sales_by_region")
    sheet = sheet.model_copy(
        update={"columns": [FieldUse(field=field.id, continuous=True)], "filters": []}
    )
    ir, _ = replace_worksheet(ir, sheet)
    sql = worksheet_sql(ir, "ws_sales_by_region").sql
    assert "f_profit" in sql


def test_analytics_fields(ir: IR) -> None:
    """叢集欄位每次新增一個（Cluster N 的別名）；Forecast Indicator 一個資料源只有一個。放到工作表上查詢後
    才填值：叢集依量值分組，預測加上估計的列。"""
    from tmig.data.executor import execute_query
    from tmig.ir.models import ClusterSpec, Forecast
    from tmig.verify.run import ensure_data

    ir, cluster = add_analytics_field(ir, "ds_orders", "cluster")
    assert (cluster.caption, cluster.datatype, cluster.role) == ("Clusters", "integer", "dimension")
    assert cluster.aliases[0].label == "Not Clustered" and cluster.aliases[3].label == "Cluster 3"
    ir, second = add_analytics_field(ir, "ds_orders", "cluster")
    assert second.id != cluster.id and second.caption == "Clusters 2"
    ir, indicator = add_analytics_field(ir, "ds_orders", "forecast_indicator")
    again, same = add_analytics_field(ir, "ds_orders", "forecast_indicator")
    assert same.id == indicator.id and again is ir

    sheet = next(w for w in ir.worksheets if w.id == "ws_sub_category_profit")
    profit = sheet.columns[0]
    marks = sheet.marks.model_copy(update={"color": FieldUse(field=cluster.id)})
    sheet = sheet.model_copy(
        update={
            "marks": marks,
            "cluster": ClusterSpec(field=cluster.id, measures=[profit], count=2),
        }
    )
    ir, _ = replace_worksheet(ir, sheet)
    ensure_data(DEMO.parent, ir)
    database = DEMO.parent / "data" / "ds_orders.duckdb"
    rows = execute_query(database, worksheet_sql(ir, sheet.id)).records()
    assert {r[next(k for k in r if "clusters" in k)] for r in rows} == {1, 2}

    trend = next(w for w in ir.worksheets if w.id == "ws_monthly_profit")
    measure = trend.rows[0]
    trend = trend.model_copy(
        update={
            "forecast": Forecast(measures=[measure], indicator=indicator.id, length="next", size=3)
        }
    )
    plain = execute_query(database, worksheet_sql(ir, "ws_monthly_profit")).rows
    ir, _ = replace_worksheet(ir, trend)
    result = execute_query(database, worksheet_sql(ir, "ws_monthly_profit"))
    assert len(result.rows) == len(plain) + 3  # 最後一期改以估計值呈現，另外估計之後的 3 期


def test_calc_in_use_cannot_be_removed_or_broken(ir: IR) -> None:
    ir, field = upsert_calc(ir, "ds_orders", "Rows", "[Sales] * 2")
    sheet = next(w for w in ir.worksheets if w.id == "ws_sales_by_region")
    sheet = sheet.model_copy(
        update={"columns": [FieldUse(field=field.id, agg="sum", continuous=True)]}
    )
    ir, _ = replace_worksheet(ir, sheet)
    with pytest.raises(EditError, match="用到"):
        remove_calc(ir, "ds_orders", field.id)
    # 改成彙總的公式：工作表上的膠囊不再加彙總（AGG(…)）；改成 Table Calculation 時加上計算方向
    agg_ir, _ = upsert_calc(ir, "ds_orders", "Rows", "SUM([Sales]) * 2", field.id)
    sheet = next(w for w in agg_ir.worksheets if w.id == "ws_sales_by_region")
    assert sheet.columns[0].agg is None
    worksheet_sql(agg_ir, sheet.id)
    tc_ir, _ = upsert_calc(agg_ir, "ds_orders", "Rows", "RUNNING_SUM(SUM([Sales]))", field.id)
    sheet = next(w for w in tc_ir.worksheets if w.id == "ws_sales_by_region")
    tc = next(t for t in sheet.table_calcs if t.id == sheet.columns[0].table_calc)
    assert tc.type == "formula" and tc.preset == "table_across"
    worksheet_sql(tc_ir, sheet.id)
    # 改回列層級：量值的膠囊加上 SUM，公式的 Table Calculation 移除
    row_ir, _ = upsert_calc(tc_ir, "ds_orders", "Rows", "[Sales] * 3", field.id)
    sheet = next(w for w in row_ir.worksheets if w.id == "ws_sales_by_region")
    assert (sheet.columns[0].agg, sheet.columns[0].table_calc) == ("sum", None)
    assert all(t.id != tc.id for t in sheet.table_calcs)
    unused, other = upsert_calc(ir, "ds_orders", "Unused", "[Profit] + 1")
    assert all(
        f.id != other.id for f in remove_calc(unused, "ds_orders", other.id).datasources[0].fields
    )


def test_replace_worksheet_validates(ir: IR) -> None:
    data = next(w for w in ir.worksheets if w.id == "ws_sales_by_region").model_dump(mode="json")
    data["rows"] = [{"field": "f_segment"}]
    ir2, sheet = replace_worksheet(ir, data)
    assert sheet.rows[0].field == "f_segment"
    assert next(w for w in ir2.worksheets if w.id == sheet.id).rows[0].field == "f_segment"
    with pytest.raises(EditError, match="參照不正確"):
        replace_worksheet(ir, {**data, "rows": [{"field": "f_nope"}]})
    with pytest.raises(EditError, match="內容不正確"):
        replace_worksheet(ir, {**data, "rows": [{"field": "f_segment", "agg": "bogus"}]})
    with pytest.raises(EditError, match="已經有名為"):
        replace_worksheet(ir, {**data, "name": "Monthly Profit"})
    with pytest.raises(EditError, match="找不到工作表"):
        replace_worksheet(ir, {**data, "id": "ws_nope"})


def test_add_duplicate_and_remove_worksheets(ir: IR) -> None:
    ir, sheet = add_worksheet(ir, None, None)
    assert sheet.name == "工作表 1" and sheet.datasource == "ds_orders"
    ir, again = add_worksheet(ir, None, None)
    assert again.name == "工作表 2"
    ir, copy = duplicate_worksheet(ir, "ws_sales_by_region")
    assert copy.name == "Sales by Region (2)"
    source = next(w for w in ir.worksheets if w.id == "ws_sales_by_region")
    # 篩選與 Table Calculation 的 id 在整份報表中唯一
    assert {f.id for f in copy.filters}.isdisjoint({f.id for f in source.filters})
    assert len(worksheet_sql(ir, copy.id).sql) > 0
    ir = remove_worksheet(ir, copy.id)
    assert all(w.id != copy.id for w in ir.worksheets)
    # dashboard 上的工作表不能刪
    with pytest.raises(EditError, match="不能刪除"):
        remove_worksheet(ir, "ws_sales_by_region")


def test_parameters(ir: IR) -> None:
    ir, created = upsert_parameter(
        ir,
        {
            "caption": "Top Count",
            "datatype": "integer",
            "allowed": {"kind": "range", "min": 1, "max": 20, "step": 1},
            "value": 5,
        },
    )
    assert created.id == "p_top_count" and created.name.startswith("[Parameter ")
    with pytest.raises(EditError, match="不在允許的範圍"):
        upsert_parameter(ir, {**created.model_dump(mode="json"), "value": 50})
    ir, edited = upsert_parameter(ir, {**created.model_dump(mode="json"), "value": 7})
    assert edited.value == 7 and edited.name == created.name
    ir = remove_parameter(ir, created.id)
    assert all(p.id != created.id for p in ir.parameters)
    # 計算欄位使用中的參數不能刪
    with pytest.raises(EditError, match="不能刪除"):
        remove_parameter(ir, "p_parameter_1")


def test_new_id_is_unique(ir: IR) -> None:
    taken = new_id(ir, "ws", "Sales by Region")
    assert taken == "ws_sales_by_region_2"
    assert new_id(ir, "tc", "累計 銷售") == "tc_x"


FEATURES = ROOT / "fixtures" / "superstore_features" / "golden.ir.json"


def test_outline(ir: IR) -> None:
    data = outline(ir)
    sheet = next(w for w in data["worksheets"] if w["id"] == "ws_sales_by_region")
    assert {f["kind"] for f in sheet["filters"]} == {"categorical", "top_n"}
    assert [d["field"] for d in sheet["dimensions"]] == ["f_region", "f_category"]
    assert sheet["legends"] == ["color"]
    assert data["dashboards"][0]["id"] == "db_ee909b0c"
    assert data["parameters"][0]["id"] == "p_parameter_1"


def test_dashboards(ir: IR) -> None:
    ir, board = add_dashboard(ir, None)
    assert board.name == "儀表板 1"
    root = board.zones[0]
    assert (root.layout, root.children[0].layout) == ("tiled", "vertical")
    data = board.model_dump(mode="json")
    flow = data["zones"][0]["children"][0]
    flow["children"].append(
        {
            "id": "z3",
            "kind": "worksheet",
            "x": 0,
            "y": 0,
            "w": 1000,
            "h": 800,
            "worksheet": "ws_monthly_profit",
            "source_ref": "/editor",
        }
    )
    ir, board = replace_dashboard(ir, data)
    assert board.zones[0].children[0].children[0].worksheet == "ws_monthly_profit"
    bad = board.model_dump(mode="json")
    bad["zones"][0]["children"][0]["children"][0]["worksheet"] = "ws_nope"
    with pytest.raises(EditError, match="參照不正確"):
        replace_dashboard(ir, bad)
    ir, copy = duplicate_dashboard(ir, board.id)
    assert copy.name == "儀表板 1 (2)" and copy.zones == board.zones
    ir = remove_dashboard(ir, copy.id)
    assert all(d.id != copy.id for d in ir.dashboards)


def test_dashboard_in_story_cannot_be_removed() -> None:
    ir = load_ir(FEATURES)
    story = ir.stories[0]
    target = next(p.dashboard for p in story.points if p.dashboard)
    assert target is not None
    with pytest.raises(EditError, match="Story"):
        remove_dashboard(ir, target)


def test_stories(ir: IR) -> None:
    ir, story = add_story(ir, None)
    assert story.name == "故事 1" and story.points[0].dashboard == "db_ee909b0c"
    data = story.model_dump(mode="json")
    data["points"].append({"id": "sp2", "caption": "月利潤", "worksheet": "ws_monthly_profit"})
    ir, story = replace_story(ir, data)
    assert [p.caption for p in story.points] == ["", "月利潤"]
    with pytest.raises(EditError, match="內容不正確"):
        replace_story(ir, {**data, "points": []})
    ir = remove_story(ir, story.id)
    assert ir.stories == []


def test_filter_action_keeps_target_filters_in_sync(ir: IR) -> None:
    from tmig.planner.state import QueryState

    ir, action = upsert_action(
        ir,
        {
            "caption": "依區域篩選",
            "kind": "filter",
            "activation": "select",
            "dashboard": "db_ee909b0c",
            "sources": ["ws_sales_by_region"],
            "targets": ["ws_region_share_by_year"],
            "clearing": "show_all",
            "fields": ["f_region"],
        },
    )
    target = next(w for w in ir.worksheets if w.id == "ws_region_share_by_year")
    linked = [f for f in target.filters if getattr(f, "action", None) == action.id]
    assert len(linked) == 1 and [u.field for u in linked[0].fields] == ["f_region"]  # type: ignore[union-attr]
    state = QueryState(actions={action.id: [{"f_region": "West"}]})
    sql = worksheet_sql(ir, "ws_region_share_by_year", state).sql
    assert "'West'" in sql
    # 目標改成另一張工作表：原本的篩選移除
    ir, action = upsert_action(
        ir,
        {**action.model_dump(mode="json"), "targets": ["ws_sub_category_profit"], "fields": []},
    )
    target = next(w for w in ir.worksheets if w.id == "ws_region_share_by_year")
    assert not [f for f in target.filters if getattr(f, "action", None) == action.id]
    other = next(w for w in ir.worksheets if w.id == "ws_sub_category_profit")
    assert [f for f in other.filters if getattr(f, "action", None) == action.id]
    ir = remove_action(ir, action.id)
    other = next(w for w in ir.worksheets if w.id == "ws_sub_category_profit")
    assert not [f for f in other.filters if getattr(f, "action", None) == action.id]
    assert ir.actions == []


def test_hierarchies(ir: IR) -> None:
    """建立、改名、改變欄位與刪除階層；一個欄位只能在一個階層中，只能加入維度。"""
    ir, created = upsert_hierarchy(ir, "ds_orders", " Product ", ["f_category", "f_sub_category"])
    assert created is not None and created.name == "Product"
    ir, _ = upsert_hierarchy(ir, "ds_orders", "Location", ["f_region", "f_state_province"])
    hierarchies = catalog(ir)["datasources"][0]["hierarchies"]
    assert hierarchies == [
        {"name": "Product", "fields": ["f_category", "f_sub_category"]},
        {"name": "Location", "fields": ["f_region", "f_state_province"]},
    ]
    # 改名並加入 Product Name；Region 移到 Product（從 Location 移除），順序依傳入的欄位
    ir, changed = upsert_hierarchy(
        ir,
        "ds_orders",
        "Products",
        ["f_category", "f_sub_category", "f_product_name", "f_region"],
        original="Product",
    )
    assert changed is not None and changed.name == "Products"
    ds = ir.datasources[0]
    assert [(h.name, h.fields) for h in ds.hierarchies] == [
        ("Products", ["f_category", "f_sub_category", "f_product_name", "f_region"]),
        ("Location", ["f_state_province"]),
    ]
    with pytest.raises(EditError, match="已經有名為「Location」的階層"):
        upsert_hierarchy(ir, "ds_orders", "Location", ["f_city"], original="Products")
    with pytest.raises(EditError, match="不是維度"):
        upsert_hierarchy(ir, "ds_orders", "Bad", ["f_sales"])
    with pytest.raises(EditError, match="找不到欄位"):
        upsert_hierarchy(ir, "ds_orders", "Bad", ["f_nope"])
    with pytest.raises(EditError, match="請輸入階層的名稱"):
        upsert_hierarchy(ir, "ds_orders", "  ", ["f_city"])
    # 移走最後一個欄位的階層一起刪除；沒有欄位時刪除階層
    ir, _ = upsert_hierarchy(ir, "ds_orders", "Products", ["f_state_province"], original="Products")
    assert [(h.name, h.fields) for h in ir.datasources[0].hierarchies] == [
        ("Products", ["f_state_province"])
    ]
    ir, removed = upsert_hierarchy(ir, "ds_orders", "Products", [], original="Products")
    assert removed is None and ir.datasources[0].hierarchies == []
    ir, _ = upsert_hierarchy(ir, "ds_orders", "Location", ["f_region"])
    ir = remove_hierarchy(ir, "ds_orders", "Location")
    assert ir.datasources[0].hierarchies == []
    with pytest.raises(EditError, match="找不到階層"):
        remove_hierarchy(ir, "ds_orders", "Location")


def test_removed_calc_leaves_its_hierarchy(ir: IR) -> None:
    ir, field = upsert_calc(ir, "ds_orders", "Region Upper", "UPPER([Region])")
    assert field.role == "dimension"
    ir, _ = upsert_hierarchy(ir, "ds_orders", "Geo", [field.id, "f_city"])
    ir = remove_calc(ir, "ds_orders", field.id)
    assert [(h.name, h.fields) for h in ir.datasources[0].hierarchies] == [("Geo", ["f_city"])]
    # 改成彙總的公式（量值）時也從階層移除
    ir, field = upsert_calc(ir, "ds_orders", "Flag", "IF [Region] = 'East' THEN 'E' END")
    ir, _ = upsert_hierarchy(ir, "ds_orders", "Geo", ["f_city", field.id], original="Geo")
    ir, field = upsert_calc(ir, "ds_orders", "Flag", "SUM([Sales])", field.id)
    assert [(h.name, h.fields) for h in ir.datasources[0].hierarchies] == [("Geo", ["f_city"])]


def test_tooltip_sheets_follow_removed_worksheets(ir: IR) -> None:
    """工具提示中的工作表：刪除目標工作表時一併從工具提示移除；不能放這張工作表本身。"""
    from tmig.ir import TooltipSheet

    ir, target = add_worksheet(ir, "Tooltip Target", None)
    sheet = next(w for w in ir.worksheets if w.id == "ws_sales_by_region")
    marks = sheet.marks.model_copy(
        update={"tooltip_sheets": [TooltipSheet(worksheet=target.id, max_width=200)]}
    )
    ir, saved = replace_worksheet(ir, sheet.model_copy(update={"marks": marks}))
    assert saved.marks.tooltip_sheets[0].worksheet == target.id
    ir = remove_worksheet(ir, target.id)
    sheet = next(w for w in ir.worksheets if w.id == "ws_sales_by_region")
    assert sheet.marks.tooltip_sheets == []
    itself = sheet.marks.model_copy(update={"tooltip_sheets": [TooltipSheet(worksheet=sheet.id)]})
    with pytest.raises(EditError, match="tooltip_sheets"):
        replace_worksheet(ir, sheet.model_copy(update={"marks": itself}))


def test_dynamic_parameter_fields(ir: IR) -> None:
    """參數的「開啟報表時的值」與允許值取自欄位；彙總的計算欄位不行。"""
    ir, latest = upsert_calc(ir, "ds_orders", "Latest", "{MAX([Order Date])}")
    ir, param = upsert_parameter(
        ir,
        {
            "caption": "As Of",
            "datatype": "date",
            "value": "2024-01-01",
            "allowed": {"kind": "all"},
            "value_field": latest.id,
        },
    )
    assert param.value_field == latest.id
    assert (
        next(p for p in catalog(ir)["parameters"] if p["id"] == param.id)["value_field"]
        == latest.id
    )
    ir, region = upsert_parameter(
        ir,
        {
            "caption": "Region Choice",
            "datatype": "string",
            "value": "East",
            "allowed": {"kind": "list", "values": ["East", "West"]},
            "domain_field": "f_region",
        },
    )
    assert region.domain_field == "f_region"
    with pytest.raises(EditError, match="彙總的計算欄位"):
        ir2, agg = upsert_calc(ir, "ds_orders", "Max Date", "MAX([Order Date])")
        upsert_parameter(ir2, {**param.model_dump(mode="json"), "value_field": agg.id})
    with pytest.raises(EditError, match="清單或範圍"):
        upsert_parameter(ir, {**region.model_dump(mode="json"), "allowed": {"kind": "all"}})


def test_device_layouts_are_validated(ir: IR) -> None:
    """裝置版面：每種裝置一個；版面中的工作表必須存在；刪除工作表時版面中用到的也不能刪。"""
    from tmig.ir import DeviceLayout, Size, Zone

    board = ir.dashboards[0]
    phone = DeviceLayout(
        device="phone", sizing="fit_width", size=Size(width=375, height=900), zones=board.zones
    )
    ir, saved = replace_dashboard(ir, board.model_copy(update={"layouts": [phone]}))
    assert [layout.device for layout in saved.layouts] == ["phone"]
    with pytest.raises(EditError, match="同一種裝置只能有一個版面"):
        replace_dashboard(ir, saved.model_copy(update={"layouts": [phone, phone]}))
    missing = Zone(
        id="z900", kind="worksheet", worksheet="ws_nope", x=0, y=0, w=10, h=10, source_ref="/editor"
    )
    broken = phone.model_copy(update={"zones": [*phone.zones, missing]})
    with pytest.raises(EditError, match="ws_nope"):
        replace_dashboard(ir, saved.model_copy(update={"layouts": [broken]}))


def test_geography(ir: IR) -> None:
    """資料窗格的地理角色與編輯位置：地名的角色是文字或整數的欄位，緯度、經度是數字；取消角色時一併
    清除指定的地名。"""
    from tmig.edit import set_geo_defaults, set_geography
    from tmig.ir.models import GeoAlias, GeoSettings

    alias = GeoAlias(value="Calif.", location="California")
    updated = set_geography(ir, "ds_orders", "f_city", "city", [alias])
    city = next(f for f in updated.datasources[0].fields if f.id == "f_city")
    assert (city.geo_role, city.geo_aliases) == ("city", [alias])
    kept = set_geography(updated, "ds_orders", "f_city", "city")
    assert next(f for f in kept.datasources[0].fields if f.id == "f_city").geo_aliases == [alias]
    cleared = set_geography(updated, "ds_orders", "f_city", None)
    city = next(f for f in cleared.datasources[0].fields if f.id == "f_city")
    assert (city.geo_role, city.geo_aliases) == (None, [])
    for field, role in (("f_sales", "state"), ("f_city", "latitude"), ("f_nope", "city")):
        with pytest.raises(EditError):
            set_geography(ir, "ds_orders", field, role)  # type: ignore[arg-type]
    with pytest.raises(EditError):
        set_geography(ir, "ds_orders", "f_city", "city", [alias, alias])
    lat = set_geography(ir, "ds_orders", "f_discount", "latitude")
    assert next(f for f in lat.datasources[0].fields if f.id == "f_discount").geo_role == "latitude"
    taiwan = set_geo_defaults(ir, "ds_orders", " 台灣 ", "")
    assert taiwan.datasources[0].geo == GeoSettings(country="台灣")
    assert set_geo_defaults(taiwan, "ds_orders", None, None).datasources[0].geo is None
