"""階層的切入（tmig.planner.drill）：資料源的階層（<drill-paths>）與日期的階層。

檢視器向下切入時在欄、列架上加入下一層（日期值改成下一層的截斷），向上收合存檔時顯示的下層；
查詢與存檔時就把那些欄位放在架上的工作表相同。測試資料見 test_query。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from tmig.data.executor import execute
from tmig.ir import IR, DateDerivation, FieldUse, Hierarchy
from tmig.parser.workbook import parse_workbook
from tmig.planner.drill import apply_drill, chains, drill_info
from tmig.planner.state import QueryState
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_query import CATEGORY, MONTH, REGION, SALES, YEAR, db

__all__ = ["db"]  # pytest fixture

ORDER = instance("[Order ID]", "None", "[none:Order ID:nk]")
QUARTER = instance("[Order Date]", "Quarter", "[qr:Order Date:ok]", "ordinal")
DRILL = """
  <drill-paths>
    <drill-path name='Location'>
      <field>[Region]</field>
      <field>[Category]</field>
      <field>[Removed Field]</field>
      <field>[Order ID]</field>
    </drill-path>
    <drill-path name='Again'>
      <field>[Category]</field>
    </drill-path>
  </drill-paths>"""


def parsed(tmp_path: Path, rows: str, cols: str = "", extra: str = DRILL) -> IR:
    sheet = worksheet(
        "S",
        [REGION, CATEGORY, ORDER, SALES, YEAR, QUARTER, MONTH],
        rows=rows,
        cols=cols or ref("sum:Sales:qk"),
    )
    xml = b.workbook(b.extract_datasource(extra=extra), worksheets=sheet)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


def shelf(ir: IR, drill: dict[str, int], name: str = "rows") -> list[str]:
    sheet = apply_drill(ir, ir.worksheets[0].id, drill).worksheets[0]
    uses = sheet.rows if name == "rows" else sheet.columns
    return [u.field if u.date is None else f"{u.date.kind}:{u.date.unit}" for u in uses]


def totals(ir: IR, db: Path, drill: dict[str, int]) -> list[tuple[object, ...]]:
    state = QueryState(drill=drill)
    query = worksheet_sql(apply_drill(ir, ir.worksheets[0].id, drill), ir.worksheets[0].id, state)
    rows = execute(db, query.sql).records()
    keys = [c.key for c in query.columns if not c.key.startswith("__")]
    return sorted(tuple(r[k] for k in keys) for r in rows)


def test_parser_reads_drill_paths(tmp_path: Path) -> None:
    """找不到的欄位略過；一個欄位只算在第一個階層（第二個階層因此沒有欄位，不列出）。"""
    ir = parsed(tmp_path, ref("none:Region:nk"))
    assert ir.datasources[0].hierarchies == [
        Hierarchy(name="Location", fields=["f_region", "f_category", "f_order_id"])
    ]


def test_drill_down_and_up_a_hierarchy(tmp_path: Path, db: Path) -> None:
    ir = parsed(tmp_path, ref("none:Region:nk"))
    ws = ir.worksheets[0]
    assert shelf(ir, {}) == ["f_region"]
    assert shelf(ir, {"f_region": 1}) == ["f_region", "f_category"]
    assert shelf(ir, {"f_region": 2}) == ["f_region", "f_category", "f_order_id"]
    assert shelf(ir, {"f_region": 9}) == ["f_region", "f_category", "f_order_id"]
    assert shelf(ir, {"f_region": -1}) == ["f_region"]
    # 其他工作表不變；沒有切入時回傳原本的 IR
    assert apply_drill(ir, ws.id, {}) is ir
    assert apply_drill(ir, ws.id, {"f_region": 0}) is ir
    # 數字與存檔時就把 Category 放在架上的工作表相同
    assert totals(ir, db, {"f_region": 1}) == [
        ("East", "Furniture", 150.0),
        ("East", "Technology", 200.0),
        ("North", "Office", 60.0),
        ("South", "Furniture", 80.0),
        ("South", "Office", 20.0),
        ("West", "Furniture", 300.0),
        ("West", "Technology", 500.0),
    ]
    info = drill_info(ir, ws, {"f_region": 1})
    assert info == [
        {
            "key": "f_region",
            "shelf": "rows",
            "label": "Category",
            "head": "Region",
            "next": "Order ID",
            "delta": 1,
            "can_down": True,
            "can_up": True,
        }
    ]


def test_collapse_levels_saved_on_the_shelf(tmp_path: Path, db: Path) -> None:
    """存檔時已經顯示 Region、Category：收合後只剩 Region，數字與只有 Region 的工作表相同。"""
    ir = parsed(tmp_path, f"({ref('none:Region:nk')} / {ref('none:Category:nk')})")
    (chain,) = chains(ir, ir.worksheets[0])
    assert (chain.saved, chain.deepest, chain.shown) == (1, 2, 1)
    assert shelf(ir, {"f_region": -1}) == ["f_region"]
    assert totals(ir, db, {"f_region": -1}) == [
        ("East", 350.0),
        ("North", 60.0),
        ("South", 100.0),
        ("West", 800.0),
    ]
    assert shelf(ir, {"f_region": 1}) == ["f_region", "f_category", "f_order_id"]


def test_level_already_on_the_shelves_is_not_added_twice(tmp_path: Path) -> None:
    """Order ID 已經在欄上：Region 只能切入到 Category。"""
    ir = parsed(tmp_path, ref("none:Region:nk"), cols=ref("none:Order ID:nk"))
    assert shelf(ir, {"f_region": 5}) == ["f_region", "f_category"]
    by_key = {c["key"]: c for c in drill_info(ir, ir.worksheets[0], {"f_region": 1})}
    assert by_key["f_region"]["can_down"] is False
    # 最下層的 Order ID 沒有可以切入的下一層：不列出控制項
    assert "f_order_id" not in by_key


def test_date_parts_expand_and_date_values_replace(tmp_path: Path, db: Path) -> None:
    """日期部分 YEAR 向下切入加入 QUARTER；日期值（截斷到月）改成截斷到日。"""
    ir = parsed(tmp_path, ref("yr:Order Date:ok"), cols=ref("tmn:Order Date:qk"))
    ws = ir.worksheets[0]
    assert shelf(ir, {"year(f_order_date)": 1}) == ["part:year", "part:quarter"]
    assert shelf(ir, {"year(f_order_date)": 3}) == [
        "part:year",
        "part:quarter",
        "part:month",
        "part:day",
    ]
    assert shelf(ir, {"trunc_month(f_order_date)": 1}, "columns") == ["trunc:day"]
    assert shelf(ir, {"trunc_month(f_order_date)": 5}, "columns") == ["trunc:day"]
    columns = apply_drill(ir, ws.id, {"trunc_month(f_order_date)": 1}).worksheets[0].columns
    assert columns == [
        FieldUse(
            field="f_order_date", date=DateDerivation(kind="trunc", unit="day"), continuous=True
        )
    ]
    info = {c["key"]: c for c in drill_info(ir, ws, {"trunc_month(f_order_date)": 1})}
    assert info["trunc_month(f_order_date)"]["label"] == "DAY(Order Date)"
    assert info["trunc_month(f_order_date)"]["can_down"] is False
    assert info["trunc_month(f_order_date)"]["can_up"] is True
    assert info["year(f_order_date)"]["next"] == "QUARTER(Order Date)"
    # 欄架是連續的日期值、沒有量值：查詢的是各個標記的日期
    assert totals(ir, db, {"year(f_order_date)": 1, "trunc_month(f_order_date)": -1}) == [
        (2023, 1, dt.date(2023, 1, 1)),
        (2023, 1, dt.date(2023, 2, 1)),
        (2023, 1, dt.date(2023, 3, 1)),
        (2024, 1, dt.date(2024, 1, 1)),
        (2024, 1, dt.date(2024, 2, 1)),
        (2024, 1, dt.date(2024, 3, 1)),
    ]
    assert totals(ir, db, {"trunc_month(f_order_date)": 1}) == [
        (2023, dt.date(2023, 1, 15)),
        (2023, dt.date(2023, 2, 10)),
        (2023, dt.date(2023, 3, 5)),
        (2024, dt.date(2024, 1, 20)),
        (2024, dt.date(2024, 2, 14)),
        (2024, dt.date(2024, 3, 1)),
        (2024, dt.date(2024, 3, 15)),
    ]


def test_dimensions_outside_hierarchies_have_no_drill(tmp_path: Path) -> None:
    ir = parsed(tmp_path, ref("none:Region:nk"), extra="")
    assert ir.datasources[0].hierarchies == []
    assert drill_info(ir, ir.worksheets[0], {"f_region": 1}) == []
    assert apply_drill(ir, ir.worksheets[0].id, {"f_region": 1}) is ir
