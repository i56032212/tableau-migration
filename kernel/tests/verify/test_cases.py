from __future__ import annotations

from pathlib import Path

import pytest

from tmig.ir import load_ir
from tmig.verify.cases import CasesError, CaseSpec, case_state, load_cases, split_values

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "fixtures" / "superstore_demo"


@pytest.fixture(scope="module")
def ir():
    return load_ir(DEMO / "golden.ir.json")


def test_demo_cases_file() -> None:
    cases = load_cases(DEMO / "cases.yaml")
    assert cases.workbook == "wb_superstore_demo"
    assert [c.name for c in cases.cases] == [
        "default",
        "office_supplies",
        "year_2024",
        "consumer_top2",
    ]
    assert [s.worksheet for s in cases.sheets] == [
        "ws_sales_by_region",
        "ws_monthly_profit",
        "ws_region_share_by_year",
        "ws_sub_category_profit",
    ]
    with pytest.raises(CasesError, match="找不到篩選組合"):
        cases.case("nope")


def test_url_params_to_state(ir) -> None:
    state = case_state(
        ir,
        CaseSpec(
            name="x",
            url_params={
                "Category": "Furniture,Office Supplies",
                "YEAR(Order Date)": "2023,2024",
                "前 N 名": 2,
            },
        ),
    )
    assert state.filters["f_category"].values == ["Furniture", "Office Supplies"]
    assert state.filters["year(f_order_date)"].values == [2023, 2024]
    assert state.parameters == {"p_parameter_1": 2}


def test_dates_and_escaped_commas(ir) -> None:
    state = case_state(
        ir, CaseSpec(name="x", url_params={"Order Date": "2024-01-02,2024-03-04 00:00:00"})
    )
    assert state.filters["f_order_date"].values == ["2024-01-02", "2024-03-04"]
    assert split_values(r"a\,b,c") == ["a,b", "c"]


def test_names_are_case_sensitive(ir) -> None:
    with pytest.raises(CasesError, match="大小寫須相符"):
        case_state(ir, CaseSpec(name="x", url_params={"category": "Furniture"}))


def test_invalid_values(ir) -> None:
    with pytest.raises(CasesError, match="不是 integer"):
        case_state(ir, CaseSpec(name="x", url_params={"Quantity": "many"}))


def test_url_params_apply_to_every_datasource(tmp_path: Path) -> None:
    """與 Tableau 相同：網址參數套用到所有資料源中同名的欄位，每張工作表只取自己資料源的欄位。"""
    from tmig.parser.workbook import parse_workbook
    from tmig.planner.state import apply_state

    from ..parser import builder as b

    second = "federated.test02"
    sheet = b.worksheet(
        "{name}",
        [
            b.instance("[Region]", "None", "[none:Region:nk]"),
            b.instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
        ],
        rows=b.ref("none:Region:nk"),
        cols=b.ref("sum:Sales:qk"),
    )
    sheets = sheet.replace("{name}", "Orders") + sheet.replace("{name}", "Returns").replace(
        b.DS, second
    )
    datasources = b.extract_datasource() + b.extract_datasource(name=second, caption="Returns")
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(datasources, sheets)))
    assert ir.unsupported == []
    first_ds, second_ds = ir.datasources
    region_ids = [
        next(f.id for f in ds.fields if f.caption == "Region") for ds in (first_ds, second_ds)
    ]
    assert region_ids[0] != region_ids[1]

    state = case_state(
        ir, CaseSpec(name="x", url_params={"Region": "East", "YEAR(Order Date)": 2024})
    )
    assert [state.filters[i].values for i in region_ids] == [["East"], ["East"]]
    assert len([k for k in state.filters if k.startswith("year(")]) == 2
    for worksheet, ds in zip(ir.worksheets, (first_ds, second_ds), strict=True):
        assert worksheet.datasource == ds.id
        effective = apply_state(ir, worksheet, state)
        own = {f.id for f in ds.fields}
        assert effective.applied and all(
            k.split("(")[-1].rstrip(")") in own for k in effective.applied
        )
        assert len(effective.skipped) == 2  # 另一個資料源的欄位
