"""其他函式：PREVIOUS_VALUE（遞迴的 Table Calculation）、視窗的起訖為運算式、HEXBINX／HEXBINY。

測試資料見 test_query.py（依 Region 排序的銷售額：East 350、North 60、South 100、West 800）。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from ..parser.builder import calc, instance, ref
from .test_query import Run, approx, by_region, db, region_sheet, run, tc_instance

__all__ = ["db", "run"]  # pytest fixtures

CALCS = "".join(
    [
        calc("Calculation_60", "Product", "SUM([Sales]) * PREVIOUS_VALUE(1)"),
        calc(
            "Calculation_61",
            "Streak",
            "IF SUM([Sales]) > 100 THEN PREVIOUS_VALUE(0) + 1 ELSE 0 END",
            "integer",
        ),
        calc("Calculation_62", "From Second", "WINDOW_SUM(SUM([Sales]), FIRST() + 1, 0)"),
        calc(
            "Calculation_63",
            "Param Window",
            "WINDOW_AVG(SUM([Sales]), -[Parameters].[Parameter 1], 0)",
        ),
        calc("Calculation_64", "Ahead", "WINDOW_MAX(SUM([Sales]), 1, LAST())"),
        calc("Calculation_65", "Hex X", "HEXBINX([Sales] / 100, [Profit] / 10)", "real"),
        calc("Calculation_66", "Hex Y", "HEXBINY([Sales] / 100, [Profit] / 10)", "real"),
        calc("Calculation_67", "Carry", "IFNULL(LOOKUP(SUM([Sales]), -1), PREVIOUS_VALUE(0))"),
    ]
)


def rows(run: Run, numbers: tuple[int, ...], **kwargs: str) -> dict[str, dict[str, Any]]:
    inst = [tc_instance(n) for n in numbers]
    enc = "".join(f"<tooltip column='{ref(f'usr:Calculation_{n}:qk')}' />" for n in numbers)
    result = run(region_sheet(inst, encodings=enc, **kwargs), CALCS)
    return {r["f_region"]: r for r in result}


def col(by: dict[str, dict[str, Any]], caption: str) -> dict[str, Any]:
    first = next(iter(by.values()))
    key = next(k for k in first if k.endswith(f"#tc_formula_{caption}"))
    return {region: row[key] for region, row in by.items()}


def test_previous_value(run: Run) -> None:
    """PREVIOUS_VALUE：前一列的這個計算的值，第一列為參數（連乘、連續次數）。"""
    by = rows(run, (60, 61, 67))
    assert col(by, "product") == approx(
        {
            "East": 350.0,
            "North": 350.0 * 60,
            "South": 350.0 * 60 * 100,
            "West": 350.0 * 60 * 100 * 800,
        }
    )
    assert col(by, "streak") == {"East": 1, "North": 0, "South": 0, "West": 1}
    # 前一個沒有值時沿用這個計算前一列的值（第一列為 0）
    assert col(by, "carry") == {"East": 0.0, "North": 350.0, "South": 60.0, "West": 100.0}


def test_previous_value_restarts_per_partition(run: Run) -> None:
    """依欄架的 Category 分割：每個分割各自從第一列開始。"""
    inst = [tc_instance(60)]
    sheet = region_sheet(
        inst,
        cols=ref("none:Category:nk"),
        encodings=f"<text column='{ref('usr:Calculation_60:qk')}' />",
    )
    result = run(sheet, CALCS)
    key = next(k for k in result[0] if k.endswith("#tc_formula_product"))
    got = {(r["f_category"], r["f_region"]): r[key] for r in result}
    # Furniture：East 150、South 80、West 300；Technology：East 200、West 500；Office：North 60、South 20
    assert got == approx(
        {
            ("Furniture", "East"): 150.0,
            ("Furniture", "South"): 150.0 * 80,
            ("Furniture", "West"): 150.0 * 80 * 300,
            ("Technology", "East"): 200.0,
            ("Technology", "West"): 200.0 * 500,
            ("Office", "North"): 60.0,
            ("Office", "South"): 60.0 * 20,
        }
    )


def test_window_bounds_as_expressions(run: Run) -> None:
    """視窗的起訖可以是運算式（FIRST() + 1、參數）；起點在終點之後時為 Null。"""
    by = rows(run, (62, 63, 64))
    assert col(by, "from_second") == {"East": None, "North": 60, "South": 160, "West": 960}
    assert col(by, "param_window") == approx(
        {"East": 350.0, "North": 205.0, "South": 170.0, "West": 327.5}
    )
    assert col(by, "ahead") == {"East": 800, "North": 800, "South": 800, "West": None}


def hexbin(x: float, y: float) -> tuple[float, float]:
    """邊長 1 的六角形（頂點朝上）：兩組錯開的矩形格點中較近的中心（與 d3-hexbin 相同）。"""
    w, h = math.sqrt(3), 3.0
    ax, ay = w * round(x / w), h * round(y / h)
    bx, by = w * (math.floor(x / w) + 0.5), h * math.floor(y / h) + 1.5
    if (x - ax) ** 2 + (y - ay) ** 2 <= (x - bx) ** 2 + (y - by) ** 2:
        return ax, ay
    return bx, by


def test_hexbin(run: Run) -> None:
    """HEXBINX／HEXBINY：點對應的六角形中心（列層級，這裡以 MIN 彙總每個地區）。"""
    inst = [
        instance("[Calculation_65]", "Min", "[min:Calculation_65:qk]", "quantitative"),
        instance("[Calculation_66]", "Min", "[min:Calculation_66:qk]", "quantitative"),
    ]
    enc = (
        f"<tooltip column='{ref('min:Calculation_65:qk')}' />"
        f"<tooltip column='{ref('min:Calculation_66:qk')}' />"
    )
    result = run(region_sheet(inst, encodings=enc), CALCS)
    # 每列的 (Sales / 100, Profit / 10)：見 test_query.ROWS
    points = {
        "East": [(1.0, 1.0), (0.5, -0.5), (2.0, 4.0)],
        "West": [(3.0, 3.0), (4.0, -2.0), (1.0, 1.0)],
        "South": [(0.8, 0.8), (0.2, 0.2)],
        "North": [(0.6, 0.6)],
    }
    xs = by_region(result, "min(f_hex_x)")
    ys = by_region(result, "min(f_hex_y)")
    for region, pts in points.items():
        centers = [hexbin(x, y) for x, y in pts]
        assert xs[region] == pytest.approx(min(c[0] for c in centers))
        assert ys[region] == pytest.approx(min(c[1] for c in centers))
