"""動態參數（Tableau 2020.1 起）：開啟報表時參數的目前值取自欄位（default-value-field），允許值的清單或
範圍取自欄位的值（source-field）。測試資料見 test_query。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tmig.data.executor import execute
from tmig.ir import IR
from tmig.parser.workbook import parse_workbook
from tmig.planner.dynamic import resolve_parameters
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import DS, calc, instance, ref, worksheet
from .test_query import REGION, db

__all__ = ["db"]  # pytest fixture

LATEST = (
    "<column caption='Latest' datatype='date' name='[Calculation_1]' role='dimension' "
    "type='ordinal'><calculation class='tableau' formula='{MAX([Order Date])}' /></column>"
)
LATEST_AGG = (
    "<column caption='Latest Agg' datatype='date' name='[Calculation_2]' role='dimension' "
    "type='ordinal'><calculation class='tableau' formula='MAX([Order Date])' /></column>"
)
ON_DAY = calc(
    "Calculation_3",
    "Sales On Day",
    "IF [Order Date] = [Parameters].[Parameter 1] THEN [Sales] END",
)


def parameters(value_field: str = "Calculation_1", list_field: str = "Region") -> str:
    return f"""
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      <column caption='As Of' datatype='date' default-value-field='[{DS}].[{value_field}]'
              name='[Parameter 1]' param-domain-type='any' role='measure' type='quantitative'
              value='#2023-01-01#'>
        <calculation class='tableau' formula='#2023-01-01#' />
      </column>
      <column caption='Region Choice' datatype='string' name='[Parameter 2]' param-domain-type='list'
              role='measure' source-field='[{DS}].[{list_field}]' type='nominal' value='&quot;East&quot;'>
        <calculation class='tableau' formula='&quot;East&quot;' />
        <members><member value='&quot;East&quot;' /></members>
      </column>
      <column caption='Quantity Pick' datatype='integer' name='[Parameter 3]' param-domain-type='range'
              role='measure' source-field='[{DS}].[Quantity]' type='quantitative' value='2'>
        <calculation class='tableau' formula='2' />
        <range granularity='1' max='10' min='0' />
      </column>
    </datasource>"""


def parsed(tmp_path: Path, params: str) -> IR:
    sheet = worksheet(
        "S",
        [REGION, instance("[Calculation_3]", "Sum", "[sum:Calculation_3:qk]", "quantitative")],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Calculation_3:qk"),
    )
    source = params + b.extract_datasource(columns=LATEST + LATEST_AGG + ON_DAY)
    xml = b.workbook(source, worksheets=sheet, params=False)
    return parse_workbook(b.write_twb(tmp_path, xml))


def runner(db: Path) -> Any:
    return lambda query, datasource: execute(db, query.sql)


def test_values_and_domains_come_from_fields(tmp_path: Path, db: Path) -> None:
    ir = parsed(tmp_path, parameters())
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    as_of, region, quantity = ir.parameters
    latest = next(f for f in ir.datasources[0].fields if f.caption == "Latest")
    assert as_of.value_field == latest.id and as_of.domain_field is None
    assert region.domain_field == "f_region" and region.value_field is None
    assert quantity.domain_field == "f_quantity"
    resolved, notes = resolve_parameters(ir, runner(db))
    assert notes == []
    as_of, region, quantity = resolved.parameters
    assert as_of.value == "2024-03-15"
    assert region.value == "East"
    assert region.allowed.model_dump() == {
        "kind": "list",
        "values": ["East", "North", "South", "West"],
    }
    assert quantity.allowed.model_dump() == {"kind": "range", "min": 1, "max": 4, "step": 1}
    # 查詢以開啟報表時的值計算：只有 2024-03-15 的訂單（North 60）
    sheet = resolved.worksheets[0]
    rows = execute(db, worksheet_sql(resolved, sheet.id).sql).rows
    assert {r[0]: r[1] for r in rows if r[1] is not None} == {"North": 60.0}
    # 存檔時的值：2023-01-01 沒有訂單
    rows = execute(db, worksheet_sql(ir, sheet.id).sql).rows
    assert all(r[1] is None for r in rows)
    # 沒有動態參數：原本的 IR
    plain = ir.model_copy(
        update={
            "parameters": [
                p.model_copy(update={"value_field": None, "domain_field": None})
                for p in ir.parameters
            ]
        }
    )
    assert resolve_parameters(plain, runner(db)) == (plain, [])


def test_several_values_keep_the_saved_value(tmp_path: Path, db: Path) -> None:
    ir = parsed(tmp_path, parameters(value_field="Order Date"))
    assert ir.unsupported == []
    resolved, notes = resolve_parameters(ir, runner(db))
    assert resolved.parameters[0].value == "2023-01-01"
    assert notes == ["動態參數「As Of」的目前值欄位有 7 個值，維持存檔時的值"]


def test_failed_query_keeps_saved_values(tmp_path: Path, db: Path) -> None:
    ir = parsed(tmp_path, parameters())

    def broken(query: Any, datasource: Any) -> Any:
        raise RuntimeError("連不上")

    resolved, notes = resolve_parameters(ir, broken)
    assert resolved.parameters == ir.parameters
    assert len(notes) == 3 and all("查詢失敗，維持存檔時的值" in n for n in notes)


def test_unusable_fields_are_flagged(tmp_path: Path) -> None:
    ir = parsed(tmp_path, parameters(value_field="Calculation_2", list_field="Nope"))
    assert sorted(u.reason for u in ir.unsupported) == sorted(
        [
            "動態參數「As Of」的目前值取自彙總的計算欄位「Latest Agg」，"
            "必須是與檢視無關的單一值（例如 FIXED 的 LOD 運算式）",
            f"動態參數「Region Choice」的允許值取自欄位 [{DS}].[Nope]，找不到這個欄位",
        ]
    )
    ir = parsed(tmp_path, parameters(value_field="Region"))
    assert [u.reason for u in ir.unsupported] == [
        "動態參數「As Of」（date）的目前值取自型別不同的欄位「Region」（string）"
    ]
