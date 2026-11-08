"""以參數或運算式決定的層級：日期部分以參數切換（日／週／月）、LOD 的維度是運算式、參數放在文字上。

測試資料見 test_query.py：各區銷售 East 350、West 800、South 100、North 60；
月份（不分年）的合計：1 月 650、2 月 280、3 月 380。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb
import pytest

from tmig.formula.analyze import Target
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.planner.state import QueryState
from tmig.sqlgen.expr import ExprCompiler
from tmig.sqlgen.sql import literal

from ..formula import helpers
from ..parser.builder import calc, instance, ref
from .test_expr import Env
from .test_query import Run, by_region, db, region_sheet, run

__all__ = ["db", "run"]  # pytest fixture（測試資料庫與執行工作表查詢）


class ParameterEnv(Env):
    def __init__(self, value: object) -> None:
        super().__init__()
        self.value = value

    def param(self, target: Target) -> str:
        return literal(self.value, target.datatype)

    def param_value(self, target: Target) -> object:
        return self.value


@pytest.fixture
def date_part(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(helpers.PARAMS, "Parameter 9", ("Date Part", "string"))


def evaluate(formula: str, value: object) -> Any:
    sql = ExprCompiler(helpers.analyzer(), ParameterEnv(value)).row(parse_formula(formula))
    with duckdb.connect() as con:
        return con.execute(
            f"SELECT {sql} FROM (SELECT DATE '2024-02-15' AS \"Order Date\") t"
        ).fetchone()[0]  # type: ignore[index]


@pytest.mark.parametrize(
    ("part", "expected"),
    [("day", dt.date(2024, 2, 15)), ("week", dt.date(2024, 2, 11)), ("month", dt.date(2024, 2, 1))],
)
def test_datetrunc_with_parameter_date_part(date_part: None, part: str, expected: dt.date) -> None:
    assert evaluate("DATETRUNC([Parameters].[Date Part], [Order Date])", part) == expected


def test_other_date_functions_with_parameter_date_part(date_part: None) -> None:
    assert evaluate("DATEPART([Parameters].[Date Part], [Order Date])", "month") == 2
    assert evaluate("DATEADD([Parameters].[Date Part], -1, [Order Date])", "month") == dt.datetime(
        2024, 1, 15
    )
    assert evaluate("DATEDIFF([Parameters].[Date Part], #2024-01-01#, [Order Date])", "month") == 1
    assert evaluate("DATENAME([Parameters].[Date Part], [Order Date])", "month") == "February"


def test_parameter_date_part_must_be_a_valid_string(date_part: None) -> None:
    with pytest.raises(FormulaSemanticError, match="目前的值「fortnight」") as error:
        evaluate("DATETRUNC([Parameters].[Date Part], [Order Date])", "fortnight")
    assert error.value.unsupported
    # 分析計算欄位時就擋下非字串的參數
    with pytest.raises(FormulaSemanticError, match="日期部分參數必須是字串"):
        helpers.analyzer().analyze(parse_formula("DATETRUNC([Parameters].[Top N], [Order Date])"))


CALCS = calc("Calculation_60", "East Split", "{FIXED [Region] = 'East' : SUM([Sales])}") + calc(
    "Calculation_61", "Month Sales", "{FIXED MONTH([Order Date]) : SUM([Sales])}"
)
INSTANCES = [
    instance(f"[Calculation_{n}]", "Max", f"[max:Calculation_{n}:qk]", "quantitative")
    for n in (60, 61)
]


def test_lod_dimensions_can_be_expressions(run: Run) -> None:
    """FIXED 的維度可以是運算式：比較的結果（是否為 East）、函式（不分年的月份）。"""
    measures = "".join(f"<text column='{ref(f'max:Calculation_{n}:qk')}' />" for n in (60, 61))
    rows = run(region_sheet(INSTANCES, encodings=measures), CALCS)
    assert by_region(rows, "max(f_east_split)") == {
        "East": 350,
        "West": 960,
        "South": 960,
        "North": 960,
    }
    # 各區的列所在月份中，月份合計最大的：East（1、2 月）、West（3、1 月）、South（2、3 月）、North（3 月）
    assert by_region(rows, "max(f_month_sales)") == {
        "East": 650,
        "West": 650,
        "South": 380,
        "North": 380,
    }


def test_parameter_on_text_shows_its_current_value(run: Run) -> None:
    """參數放在文字上（顯示目前的選擇）：每個標記都是參數目前的值，切換參數時跟著變。"""
    sheet = region_sheet(encodings="<text column='[Parameters].[Parameter 2]' />")
    rows = run(sheet)
    assert by_region(rows, "f_region_pick") == dict.fromkeys(
        ("East", "West", "South", "North"), "East"
    )
    switched = run(sheet, state=QueryState(parameters={"p_region_pick": "West"}))
    assert set(by_region(switched, "f_region_pick").values()) == {"West"}
