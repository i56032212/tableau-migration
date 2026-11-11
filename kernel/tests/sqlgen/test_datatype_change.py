"""變更資料類型（datatype-customized）：萃取中仍是原本的型別，查詢時以 Tableau 的型別轉換函式轉換。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.parser.workbook import parse_workbook

from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet
from .test_query import SALES, Run, db, run

__all__ = ["db", "run"]  # pytest fixture


def customized(datatype: str, role: str = "dimension", kind: str = "nominal") -> str:
    """萃取中是整數的 Quantity 改成 datatype。"""
    return (
        f"<column datatype='{datatype}' datatype-customized='true' name='[Quantity]' "
        f"role='{role}' type='{kind}' />"
    )


def test_number_changed_to_string(run: Run) -> None:
    quantity = instance("[Quantity]", "None", "[none:Quantity:nk]")
    sheet = worksheet(
        "S", [quantity, SALES], rows=ref("none:Quantity:nk"), cols=ref("sum:Sales:qk")
    )
    rows = run(sheet, customized("string"))
    assert {r["f_quantity"]: r["sum(f_sales)"] for r in rows} == {
        "1": 480.0,
        "2": 130.0,
        "3": 300.0,
        "4": 400.0,
    }


def test_number_changed_to_date(run: Run) -> None:
    # 數字轉日期：1899-12-30 起算的天數（與 DATE() 相同），例如 1 是 1899-12-31
    days = calc("Calculation_1", "Days", "DATEDIFF('day', #1899-12-30#, [Quantity])", "integer")
    inst = instance("[Calculation_1]", "Sum", "[sum:Calculation_1:qk]", "quantitative")
    year = instance("[Quantity]", "Year", "[yr:Quantity:ok]", "ordinal")
    sheet = worksheet(
        "S", [inst, year], rows=ref("yr:Quantity:ok"), cols=ref("sum:Calculation_1:qk")
    )
    rows = run(sheet, customized("date", kind="ordinal") + days)
    assert {r["year(f_quantity)"]: r["sum(f_days)"] for r in rows} == {1899: 5, 1900: 11}


def test_parsed_as_hidden_source_and_conversion(tmp_path: Path) -> None:
    xml = b.workbook(b.extract_datasource(columns=customized("real", "measure", "quantitative")))
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == []
    fields = {f.name: f for f in ir.datasources[0].fields}
    visible, source = fields["[Quantity]"], fields["[Quantity (原始型別)]"]
    assert visible.calc is not None and visible.calc.formula == "FLOAT([Quantity (原始型別)])"
    assert (visible.datatype, visible.column, visible.hidden) == ("real", None, False)
    assert (source.datatype, source.column, source.hidden) == ("integer", "Quantity", True)
    assert any("變更了資料類型（integer → real）" in u.reason for u in ir.ignored)


@pytest.mark.parametrize("datatype", ["integer", "boolean"])
def test_unchanged_or_unsupported_conversion_keeps_the_column(
    tmp_path: Path, datatype: str
) -> None:
    # 與來源相同的型別不需要轉換；轉成布林值沒有對應的轉換函式，照原樣當成欄位
    xml = b.workbook(b.extract_datasource(columns=customized(datatype)))
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    fields = {f.name: f for f in ir.datasources[0].fields}
    assert fields["[Quantity]"].column == "Quantity" and "[Quantity (原始型別)]" not in fields
