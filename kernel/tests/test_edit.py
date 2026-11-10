"""桌面版編輯器的 IR 操作（tmig.edit）：工作表的新增、修改、複製、刪除，計算欄位與參數。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.edit import (
    EditError,
    add_worksheet,
    catalog,
    check_formula,
    duplicate_worksheet,
    new_id,
    remove_calc,
    remove_parameter,
    remove_worksheet,
    replace_worksheet,
    upsert_calc,
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


def test_calc_in_use_cannot_be_removed_or_broken(ir: IR) -> None:
    ir, field = upsert_calc(ir, "ds_orders", "Rows", "[Sales] * 2")
    sheet = next(w for w in ir.worksheets if w.id == "ws_sales_by_region")
    sheet = sheet.model_copy(
        update={"columns": [FieldUse(field=field.id, agg="sum", continuous=True)]}
    )
    ir, _ = replace_worksheet(ir, sheet)
    with pytest.raises(EditError, match="還在使用中"):
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
