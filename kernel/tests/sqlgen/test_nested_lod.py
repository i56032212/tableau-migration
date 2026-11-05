"""巢狀 LOD：LOD 的運算式中再用 LOD。

與 Tableau 相同：內層的 INCLUDE／EXCLUDE 相對於外層 LOD 的維度（不是檢視的維度）；外層的彙總中
只含內層的值時，每個（外層維度、內層維度）的組合算一次，與檢視彙總 LOD 時相同（內層 FIXED 的維度
與外層無關時，各外層成員取它出現過的內層成員）。外層或自己有 FIXED 的在維度篩選前計算。

測試資料見 test_query.py：訂單 O1 150、O2 200（East）、O3 300、O4 500（West）、O5 80、O6 20（South）、
O7 60（North）；類別合計 Furniture 530、Technology 700、Office 80。
"""

from __future__ import annotations

from pathlib import Path

from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import calc, instance, ref
from .test_query import Run, by_region, categorical, db, region_sheet, run

__all__ = ["db", "run"]  # pytest fixture（測試資料庫與執行工作表查詢）

CALCS = "".join(
    [
        calc(
            "Calculation_40",
            "Avg Order",
            "{FIXED [Region] : AVG({INCLUDE [Order ID] : SUM([Sales])})}",
        ),
        calc(
            "Calculation_41",
            "Category Totals",
            "{FIXED [Region] : SUM({FIXED [Category] : SUM([Sales])})}",
        ),
        calc(
            "Calculation_42",
            "Top Order",
            "{EXCLUDE [Category] : MAX({INCLUDE [Order ID] : SUM([Sales])})}",
        ),
        calc(
            "Calculation_43",
            "Region Fixed",
            "{EXCLUDE [Category] : SUM({FIXED [Region] : SUM([Sales])})}",
        ),
        calc(
            "Calculation_44",
            "Share Of Best",
            "SUM([Sales]) / SUM({FIXED [Category] : MAX({FIXED [Region], [Category] : SUM([Sales])})})",
        ),
    ]
)
INSTANCES = [
    instance(f"[Calculation_{n}]", "Sum", f"[sum:Calculation_{n}:qk]", "quantitative")
    for n in (40, 41, 42, 43)
] + [instance("[Calculation_44]", "User", "[usr:Calculation_44:qk]", "quantitative")]
NESTED = f"{ref('none:Region:nk')} / {ref('none:Category:nk')}"


def sheet(measure: str, rows: str = ref("none:Region:nk"), view_extra: str = "") -> str:
    return region_sheet(
        INSTANCES,
        rows=rows,
        encodings=f"<text column='{ref(measure)}' />",
        view_extra=view_extra,
    )


def test_include_inside_fixed_inherits_outer_dimensions(run: Run) -> None:
    """每區的平均訂單金額：內層的 INCLUDE [Order ID] 相對於外層的 [Region]，每張訂單算一次
    （逐列平均會是 East (150 + 150 + 200) / 3）。"""
    measure = "sum:Calculation_40:qk"
    rows = run(sheet(measure), CALCS)
    assert by_region(rows, "sum(f_avg_order)") == {
        "East": 175,
        "North": 60,
        "South": 50,
        "West": 400,
    }
    # FIXED 在維度篩選前計算：只留 Furniture 時 East 仍含 Technology 的訂單
    furniture = categorical("none:Category:nk", ["Furniture"])
    rows = run(sheet(measure, view_extra=furniture), CALCS)
    assert by_region(rows, "sum(f_avg_order)") == {"East": 175, "South": 50, "West": 400}
    # 內容篩選則先套用
    context = categorical("none:Category:nk", ["Furniture"], context=True)
    rows = run(sheet(measure, view_extra=context), CALCS)
    assert by_region(rows, "sum(f_avg_order)") == {"East": 150, "South": 80, "West": 300}


def test_inner_fixed_counts_each_combination_once(run: Run) -> None:
    """內層 FIXED [Category] 與外層 [Region] 無關：每區加總它出現過的類別的全公司合計。"""
    rows = run(sheet("sum:Calculation_41:qk"), CALCS)
    assert by_region(rows, "sum(f_category_totals)") == {
        "East": 530 + 700,
        "North": 80,
        "South": 530 + 80,
        "West": 530 + 700,
    }


def test_relative_inside_relative_respects_dimension_filters(run: Run) -> None:
    """EXCLUDE [Category] 之內的 INCLUDE [Order ID]：每區最大的訂單，在維度篩選後計算。"""
    measure = "sum:Calculation_42:qk"
    rows = run(sheet(measure, NESTED), CALCS)
    result = {(r["f_region"], r["f_category"]): r["sum(f_top_order)"] for r in rows}
    assert result == {
        ("East", "Furniture"): 200,
        ("East", "Technology"): 200,
        ("North", "Office"): 60,
        ("South", "Furniture"): 80,
        ("South", "Office"): 80,
        ("West", "Furniture"): 500,
        ("West", "Technology"): 500,
    }
    furniture = categorical("none:Category:nk", ["Furniture"])
    rows = run(sheet(measure, NESTED, furniture), CALCS)
    result = {(r["f_region"], r["f_category"]): r["sum(f_top_order)"] for r in rows}
    assert result == {
        ("East", "Furniture"): 150,
        ("South", "Furniture"): 80,
        ("West", "Furniture"): 300,
    }


def test_fixed_inside_relative_ignores_dimension_filters(run: Run) -> None:
    """EXCLUDE 之內的 FIXED [Region] 仍在維度篩選前計算（只留 Furniture 時 East 仍是 350）。"""
    furniture = categorical("none:Category:nk", ["Furniture"])
    rows = run(sheet("sum:Calculation_43:qk", NESTED, furniture), CALCS)
    result = {(r["f_region"], r["f_category"]): r["sum(f_region_fixed)"] for r in rows}
    assert result == {
        ("East", "Furniture"): 350,
        ("South", "Furniture"): 100,
        ("West", "Furniture"): 800,
    }


def test_nested_lod_in_view_measure(run: Run) -> None:
    """檢視的量值中用到巢狀 LOD：各區的銷售額 ÷ 該類別最好的一區的銷售額。"""
    rows = run(sheet("usr:Calculation_44:qk", NESTED), CALCS)
    result = {(r["f_region"], r["f_category"]): r["f_share_of_best"] for r in rows}
    # Furniture 最好的是 West 300、Technology 是 West 500、Office 是 North 60
    assert result[("East", "Furniture")] == 150 / 300
    assert result[("West", "Technology")] == 1.0
    assert result[("South", "Office")] == 20 / 60


def test_inner_lods_are_attached_first(tmp_path: Path) -> None:
    """內層先併回每一列（ctx_ext），外層再以併好的資料計算（ctx_ext_1）。"""
    xml = b.workbook(b.extract_datasource(columns=CALCS), worksheets=sheet("sum:Calculation_40:qk"))
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == []
    query = worksheet_sql(ir, ir.worksheets[0].id)
    names = [c.name for c in query.ctes]
    assert names.index("ctx_ext") < names.index("lod_agg") < names.index("ctx_ext_1")
    assert all(c.stage == 3 for c in query.ctes if c.name.startswith(("lod", "ctx_ext")))
