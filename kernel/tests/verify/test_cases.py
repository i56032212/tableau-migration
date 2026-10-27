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
