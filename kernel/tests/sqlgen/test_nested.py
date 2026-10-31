"""巢狀 Table Calculation：視窗函式不能巢狀，內層先算成欄位、外層再引用（分層計算）。

寫法取自公開報表：HR 人資的 RANK([% Total Hired])、WINDOW_MAX([% Total Hired]) = [% Total Hired]
（% Total Hired 本身含 TOTAL，且巢狀計算的方向與外層不同）、Amazon Prime 的 WINDOW_MAX(INDEX() - 1)。
測試資料與手算結果見 test_query.py（各區銷售 East 350、West 800、South 100、North 60）。
"""

from __future__ import annotations

from typing import Any

import pytest

from tmig.parser.workbook import parse_workbook

from ..parser import builder as b
from ..parser.builder import DS, calc, instance, ref, worksheet
from .test_query import CATEGORY, REGION, SALES, Run, db, run

__all__ = ["db", "run"]  # pytest fixture（測試資料庫與執行工作表查詢）

SHARE = calc("Calculation_41", "Share", "SUM([Sales]) / TOTAL(SUM([Sales]))")
CALCS = "".join(
    [
        calc("Calculation_40", "Running Rank", "RUNNING_SUM(RANK(SUM([Sales])))", "integer"),
        SHARE,
        calc("Calculation_42", "Top", "RANK([Calculation_41])", "integer"),
        calc("Calculation_43", "Idx", "INDEX() - 1", "integer"),
        calc("Calculation_44", "Max Idx", "WINDOW_MAX([Calculation_43])", "integer"),
        calc(
            "Calculation_45",
            "Order",
            "RANK_UNIQUE([Calculation_44] + [Calculation_43], 'asc')",
            "integer",
        ),
        calc(
            "Calculation_46", "Best", "WINDOW_MAX([Calculation_41]) = [Calculation_41]", "boolean"
        ),
    ]
)


def tc(n: int, *settings: str) -> str:
    """計算欄位的欄位實例；settings 為 <table-calc> 的屬性（第一個是自己的，其餘是巢狀計算）。"""
    body = "".join(f"<table-calc {s} />" for s in settings)
    return instance(f"[Calculation_{n}]", "User", f"[usr:Calculation_{n}:qk]", "quantitative", body)


def text(n: int) -> str:
    return f"<text column='{ref(f'usr:Calculation_{n}:qk')}' />"


def by_region(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {r["f_region"]: r[key] for r in rows}


def test_table_calc_inside_a_table_calc(run: Run) -> None:
    """同一個公式中巢狀：RANK 先算成欄位，RUNNING_SUM 再依同一個計算方向累加。"""
    sheet = worksheet(
        "S",
        [REGION, SALES, tc(40, "ordering-type='Columns'")],
        rows=ref("none:Region:nk"),
        mark="Text",
        encodings=text(40),
    )
    rows = run(sheet, CALCS)
    # 銷售排名 West 1、East 2、South 3、North 4；依 Region 的順序累加
    assert [(r["f_region"], r["f_running_rank#tc_formula_running_rank"]) for r in rows] == [
        ("East", 2),
        ("North", 6),
        ("South", 9),
        ("West", 10),
    ]


def matrix(*settings: str, n: int = 42) -> str:
    return worksheet(
        "S",
        [REGION, CATEGORY, SALES, tc(n, *settings)],
        rows=ref("none:Region:nk"),
        cols=ref("none:Category:nk"),
        mark="Text",
        encodings=text(n),
    )


def cells(rows: list[dict[str, Any]], key: str) -> dict[tuple[str, str], Any]:
    return {(r["f_region"], r["f_category"]): r[key] for r in rows}


def test_nested_calc_uses_its_own_direction(run: Run) -> None:
    """巢狀計算各自的計算方向：Share 在各區內跨類別計算（合計是該區的銷售），
    外層的 RANK 再於各類別內依地區排名。"""
    nested = f"field='[{DS}].[Calculation_41]' ordering-type='Rows'"
    rows = run(matrix("ordering-type='Columns'", nested), CALCS)
    # Share：East 150/350、200/350，West 300/800、500/800，South 80/100、20/100，North 60/60
    assert cells(rows, "f_top#tc_formula_top") == {
        ("East", "Furniture"): 2,
        ("West", "Furniture"): 3,
        ("South", "Furniture"): 1,
        ("East", "Technology"): 2,
        ("West", "Technology"): 1,
        ("North", "Office"): 1,
        ("South", "Office"): 2,
    }
    # 沒有巢狀計算的設定時與外層相同：Share 改為各類別內的占比，排名隨之不同
    rows = run(matrix("ordering-type='Columns'"), CALCS)
    assert cells(rows, "f_top#tc_formula_top") == {
        ("East", "Furniture"): 2,
        ("West", "Furniture"): 1,
        ("South", "Furniture"): 3,
        ("East", "Technology"): 2,
        ("West", "Technology"): 1,
        ("North", "Office"): 1,
        ("South", "Office"): 2,
    }


def test_window_of_a_nested_total(run: Run) -> None:
    """HR 人資的寫法：WINDOW_MAX([% Total Hired]) = [% Total Hired]，標出各類別中占比最高的地區。"""
    nested = f"field='[{DS}].[Calculation_41]' ordering-type='Rows'"
    rows = run(matrix("ordering-type='Columns'", nested, n=46), CALCS)
    best = {k for k, v in cells(rows, "f_best#tc_formula_best").items() if v}
    assert best == {("South", "Furniture"), ("West", "Technology"), ("North", "Office")}


def test_three_levels_of_nesting(run: Run) -> None:
    """Amazon Prime 的寫法：INDEX() - 1 → WINDOW_MAX(…) → RANK_UNIQUE(…)，分三層計算。"""
    sheet = worksheet(
        "S",
        [REGION, SALES, tc(45, "ordering-type='Columns'")],
        rows=ref("none:Region:nk"),
        mark="Text",
        encodings=text(45),
    )
    rows = run(sheet, CALCS)
    # Idx 0–3、Max Idx 都是 3：3、4、5、6 由小到大排名
    assert by_region(rows, "f_order#tc_formula_order") == {
        "East": 1,
        "North": 2,
        "South": 3,
        "West": 4,
    }


def test_nested_settings_are_parsed(tmp_path: Any) -> None:
    nested = f"field='[{DS}].[Calculation_41]' ordering-type='Rows'"
    xml = b.workbook(
        b.extract_datasource(columns=CALCS),
        worksheets=matrix("ordering-type='Columns'", nested),
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == []
    (top,) = [t for t in ir.worksheets[0].table_calcs if t.id == "tc_formula_top"]
    assert top.preset == "table_down" and top.addressing == ["f_region"]
    (share,) = top.nested
    assert (share.field, share.preset, share.addressing) == (
        "f_share",
        "table_across",
        ["f_category"],
    )


@pytest.mark.parametrize("bad", ["field='[ds].[Nope]' ordering-type='Rows'"])
def test_unknown_nested_field_is_flagged(tmp_path: Any, bad: str) -> None:
    xml = b.workbook(
        b.extract_datasource(columns=CALCS), worksheets=matrix("ordering-type='Columns'", bad)
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert [u.reason for u in ir.unsupported] == ["巢狀計算的欄位 [ds].[Nope] 找不到"]
