from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tmig.ir import IR, dumps_ir, iter_zones, load_ir

EXAMPLE = Path(__file__).parent / "data" / "example.ir.json"

Doc = dict[str, Any]


@pytest.fixture
def doc() -> Doc:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def validate(doc: Doc) -> IR:
    return IR.model_validate(doc)


def errors_of(doc: Doc) -> str:
    with pytest.raises(ValidationError) as info:
        validate(doc)
    return str(info.value)


def ws(doc: Doc, index: int = 0) -> Doc:
    return doc["worksheets"][index]


def zones(doc: Doc) -> list[Doc]:
    return doc["dashboards"][0]["zones"][0]["children"]


def test_example_is_valid() -> None:
    ir = load_ir(EXAMPLE)
    assert ir.workbook.id == "wb_sales"
    assert [w.id for w in ir.worksheets] == ["ws_sales_by_region", "ws_monthly_profit"]
    assert len(list(iter_zones(ir.dashboards[0].zones))) == 7


def test_dump_is_canonical() -> None:
    assert dumps_ir(load_ir(EXAMPLE)) == EXAMPLE.read_text(encoding="utf-8")


def test_dump_keeps_chinese(doc: Doc) -> None:
    doc["workbook"]["name"] = "銷售總覽"
    assert "銷售總覽" in dumps_ir(validate(doc))


def test_rejects_unknown_fields(doc: Doc) -> None:
    doc["datasources"][0]["fields"][0]["colour"] = "red"
    assert "colour" in errors_of(doc)


def test_rejects_other_ir_version(doc: Doc) -> None:
    doc["ir_version"] = "0.2"
    assert "ir_version" in errors_of(doc)


@pytest.mark.parametrize("bad_id", ["WS_sales", "1st_sheet", "ws-sales", "工作表", "w" * 65, ""])
def test_id_pattern(doc: Doc, bad_id: str) -> None:
    ws(doc)["id"] = bad_id
    assert "worksheets.0.id" in errors_of(doc)


def test_ids_are_unique_across_entities(doc: Doc) -> None:
    ws(doc, 1)["id"] = "f_sales"
    assert "id「f_sales」與 datasources[ds_orders].fields[f_sales] 重複" in errors_of(doc)


def test_zone_ids_are_unique_within_dashboard(doc: Doc) -> None:
    zones(doc)[1]["id"] = "z2"
    assert "zones[z2]：區塊 id 重複" in errors_of(doc)


def test_same_zone_id_in_two_dashboards_is_allowed(doc: Doc) -> None:
    second = copy.deepcopy(doc["dashboards"][0])
    second["id"] = "db_copy"
    doc["dashboards"].append(second)
    validate(doc)


Mutation = Callable[[Doc], None]


def _set(path: list[str | int], value: Any) -> Mutation:
    def apply(doc: Doc) -> None:
        target: Any = doc
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return apply


DANGLING: list[tuple[str, Mutation, str]] = [
    (
        "worksheet datasource",
        _set(["worksheets", 0, "datasource"], "ds_missing"),
        "資料源「ds_missing」不存在",
    ),
    (
        "shelf field",
        _set(["worksheets", 0, "rows", 0, "field"], "f_missing"),
        "rows[0]：欄位「f_missing」不在資料源「ds_orders」中",
    ),
    (
        "table calc on shelf",
        _set(["worksheets", 0, "marks", "tooltip", 0, "table_calc"], "tc_missing"),
        "Table Calculation「tc_missing」不存在",
    ),
    (
        "top n parameter",
        _set(["worksheets", 0, "filters", 1, "count", "parameter"], "p_missing"),
        "參數「p_missing」不存在",
    ),
    (
        "sort field",
        _set(["worksheets", 0, "sorts", 0, "field"], "f_missing"),
        "sorts[0]：欄位「f_missing」",
    ),
    (
        "table calc addressing",
        _set(["worksheets", 0, "table_calcs", 0, "addressing"], ["f_missing"]),
        "addressing：欄位「f_missing」",
    ),
    (
        "zone worksheet",
        _set(["dashboards", 0, "zones", 0, "children", 1, "worksheet"], "ws_missing"),
        "工作表「ws_missing」不存在",
    ),
    (
        "zone parameter",
        _set(["dashboards", 0, "zones", 0, "children", 4, "parameter"], "p_missing"),
        "參數「p_missing」不存在",
    ),
    (
        "filter zone field",
        _set(["dashboards", 0, "zones", 0, "children", 3, "field", "field"], "f_missing"),
        "zones[z5].field：欄位「f_missing」",
    ),
]


@pytest.mark.parametrize(("case", "mutate", "message"), DANGLING, ids=[c[0] for c in DANGLING])
def test_dangling_references(doc: Doc, case: str, mutate: Mutation, message: str) -> None:
    mutate(doc)
    assert message in errors_of(doc)


def test_table_calc_not_allowed_in_datasource_filter(doc: Doc) -> None:
    flt = copy.deepcopy(ws(doc)["filters"][0])
    flt.update(id="flt_ds", stage="datasource")
    flt["field"]["table_calc"] = "tc_running_sales"
    doc["datasources"][0]["filters"].append(flt)
    assert "此處不能套用 Table Calculation" in errors_of(doc)


@pytest.mark.parametrize(
    ("index", "changes", "message"),
    [
        (3, {"control": None}, "filter 區塊缺少 control"),
        (1, {"text": "多餘"}, "worksheet 區塊不該有 text"),
        (4, {"worksheet": "ws_sales_by_region"}, "parameter 區塊不該有 worksheet"),
        (5, {"legend": None}, "legend 區塊缺少 legend"),
    ],
)
def test_zone_kind_rules(doc: Doc, index: int, changes: Doc, message: str) -> None:
    zones(doc)[index].update(changes)
    assert message in errors_of(doc)


def test_only_containers_have_children(doc: Doc) -> None:
    zones(doc)[1]["children"] = [copy.deepcopy(zones(doc)[2])]
    assert "只有 container 可以有子區塊" in errors_of(doc)


def test_title_zone_text_is_optional(doc: Doc) -> None:
    zones(doc)[0]["text"] = None
    validate(doc)


@pytest.mark.parametrize(
    ("index", "stage", "message"),
    [
        (0, "measure", "categorical 篩選不能屬於 measure 階段"),
        (1, "dimension", "top_n 篩選不能屬於 dimension 階段"),
        (0, "datasource", "工作表上的篩選 flt_category 不能屬於 datasource 階段"),
    ],
)
def test_filter_stages(doc: Doc, index: int, stage: str, message: str) -> None:
    ws(doc)["filters"][index]["stage"] = stage
    assert message in errors_of(doc)


def test_datasource_filters_only_extract_or_datasource(doc: Doc) -> None:
    flt = copy.deepcopy(ws(doc)["filters"][0])
    flt["id"] = "flt_ds"
    doc["datasources"][0]["filters"].append(flt)
    assert "只能屬於 extract 或 datasource 階段" in errors_of(doc)


def test_range_filter_cannot_be_top_n_stage(doc: Doc) -> None:
    ws(doc, 1)["filters"][0]["stage"] = "top_n"
    assert "range 篩選不能屬於 top_n 階段" in errors_of(doc)


def test_categorical_all_mode_has_no_values(doc: Doc) -> None:
    ws(doc)["filters"][0]["mode"] = "all"
    assert "mode 為 all 時 values 必須為空" in errors_of(doc)


def test_top_n_count_must_be_positive(doc: Doc) -> None:
    ws(doc)["filters"][1]["count"] = 0
    assert "count" in errors_of(doc)
    ws(doc)["filters"][1]["count"] = 5
    validate(doc)


@pytest.mark.parametrize(
    "changes",
    [
        {"by": None},
        {"kind": "alphabetic"},
        {"kind": "manual", "by": None},
    ],
)
def test_sort_rules(doc: Doc, changes: Doc) -> None:
    ws(doc)["sorts"][0].update(changes)
    assert "sorts.0" in errors_of(doc)


def test_manual_sort(doc: Doc) -> None:
    ws(doc)["sorts"][0].update(kind="manual", by=None, manual_values=["West", "East"])
    validate(doc)


def test_field_use_cannot_have_agg_and_date(doc: Doc) -> None:
    ws(doc, 1)["columns"][0]["agg"] = "min"
    assert "agg 與 date 不能同時設定" in errors_of(doc)


def test_scalars_are_strict(doc: Doc) -> None:
    ws(doc)["filters"][0]["values"] = ["Furniture", 3, 2.5, True, None]
    validate(doc)
    ws(doc)["filters"][0]["values"] = [{"name": "Furniture"}]
    assert "values" in errors_of(doc)


def test_unsupported_entries_are_kept(doc: Doc) -> None:
    doc["unsupported"].append(
        {
            "source_ref": "/workbook/worksheets/worksheet[3]",
            "reason": "雙軸",
            "element": "rows",
            "xml": "<rows>([a] + [b])</rows>",
        }
    )
    assert validate(doc).unsupported[0].reason == "雙軸"
