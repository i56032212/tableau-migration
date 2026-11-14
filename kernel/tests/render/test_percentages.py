"""百分比（Tableau 的「分析＞百分比」）：量值顯示成占範圍合計的比例；合計在範圍的詳細程度重新彙總。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute
from tmig.ir import IR
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import (
    GRAND_TOTAL,
    QueryError,
    percentages,
    totals_sql,
    worksheet_sql,
)

from ..parser.builder import instance, ref, worksheet
from ..sqlgen.test_query import CATEGORY, REGION, SALES
from .test_text import db, parse, validator, with_totals

__all__ = ["db"]  # pytest fixture（測試資料庫）

AVG = instance("[Sales]", "Avg", "[avg:Sales:qk]", "quantitative")


def table(mode: str, measure: str = "sum:Sales:qk", extra: str = "") -> str:
    """Region × Category 的文字表，量值以百分比顯示（mode 為 .twb 的寫法）。"""
    return worksheet(
        "Share",
        [REGION, CATEGORY, SALES, AVG],
        rows=ref("none:Region:nk"),
        cols=ref("none:Category:nk"),
        mark="Text",
        encodings=f"<text column='{ref(measure)}' />{extra}",
        view_extra=f"<percentages enabled='true' mode='{mode}' />",
    )


def run(ir: IR, db: Path) -> tuple[Any, dict[tuple[Any, ...], float], list[Any]]:
    """與檢視器相同：工作表的查詢、總計，再換成百分比。回傳（工作表的查詢、標記的值、總計）。"""
    ws = ir.worksheets[0]
    main = worksheet_sql(ir, ws.id)
    marks = execute(db, main.sql)
    totals = [(t, execute(db, t.query.sql)) for t in totals_sql(ir, ws.id, main, marks=marks)]
    result, totals = percentages(ir, ws.id, main, marks, totals, lambda q: execute(db, q.sql))
    measure = next(c.key for c in main.columns if c.role == "measure")
    values = {(r["f_region"], r["f_category"]): r[measure] for r in result.records()}
    rendered = build_spec(ir, ws.id, main.columns, result, totals=totals)
    assert rendered.spec is not None, rendered.unsupported
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered, values, totals


@pytest.mark.parametrize(
    ("mode", "ir_mode", "whole"),
    [
        ("table", "table", 1310),
        ("column", "column", 530),  # Furniture：150 + 80 + 300
        ("row", "row", 350),  # East：150 + 200
        ("pane", "pane", 1310),  # 列與欄各只有一個維度：窗格是整個表
        ("row-in-pane", "row_in_pane", 350),
        ("column-in-pane", "column_in_pane", 530),
        ("cell-in-pane", "cell", 150),
    ],
)
def test_percent_of_each_scope(
    tmp_path: Path, db: Path, mode: str, ir_mode: str, whole: float
) -> None:
    ir = parse(tmp_path, table(mode))
    assert ir.unsupported == []
    assert ir.worksheets[0].percentages == ir_mode
    rendered, values, _ = run(ir, db)
    assert values["East", "Furniture"] == pytest.approx(150 / whole)
    # 以百分比顯示，欄位名稱註明
    record = next(
        v
        for v in rendered.spec["data"]["values"]
        if v["c0_label"] == "East" and v["c1_label"] == "Furniture"
    )
    assert record["c2_label"] == f"{150 / whole:.2%}"
    assert rendered.spec["usermeta"]["measures"] == {"c2": "sum(f_sales)"}


def test_percent_uses_the_aggregation_of_the_scope(tmp_path: Path, db: Path) -> None:
    """合計在範圍的詳細程度重新彙總：平均的百分比是「這一格的平均 ÷ 整個表的平均」，不是各格平均的加總。"""
    ir = parse(tmp_path, table("table", "avg:Sales:qk"))
    _, values, _ = run(ir, db)
    with duckdb.connect(str(db), read_only=True) as con:
        cell, overall = con.execute(
            'SELECT avg("Sales") FILTER (WHERE "Region" = \'East\' AND "Category" = \'Furniture\'), '
            'avg("Sales") FROM "Extract"."Extract"'
        ).fetchone()  # type: ignore[misc]
    assert values["East", "Furniture"] == pytest.approx(cell / overall)


def test_percent_with_totals_and_mark_dimensions(tmp_path: Path, db: Path) -> None:
    """總計與小計的列也是百分比（欄的百分比：總計列是 100%）；標記卡上的維度不屬於範圍。"""
    ir = parse(tmp_path, with_totals(table("column"), "rows", "cols"))
    _, values, totals = run(ir, db)
    assert values["West", "Technology"] == pytest.approx(500 / 700)
    by_shelves = {t.shelves: r.records() for t, r in totals}
    # 欄總計（每一欄的合計）：占這一欄的 100%
    assert {r["f_category"]: r["sum(f_sales)"] for r in by_shelves["rows",]} == {
        "Furniture": pytest.approx(1.0),
        "Office": pytest.approx(1.0),
        "Technology": pytest.approx(1.0),
    }
    # 列總計（每一列的合計）：範圍中的欄維度已經去掉，是占整個表的比例
    assert {r["f_region"]: r["sum(f_sales)"] for r in by_shelves["columns",]}["East"] == (
        pytest.approx(350 / 1310)
    )
    (corner,) = by_shelves["rows", "columns"]
    assert corner["sum(f_sales)"] == pytest.approx(1.0)

    # 色彩上的維度：長條的每一段占整列的比例
    bars = worksheet(
        "Stacked",
        [REGION, CATEGORY, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<color column='{ref('none:Category:nk')}' />",
        view_extra="<percentages enabled='true' mode='row' />",
    )
    (tmp_path / "bars").mkdir()
    ir = parse(tmp_path / "bars", bars)
    rendered, values, _ = run(ir, db)
    assert values["East", "Technology"] == pytest.approx(200 / 350)
    assert rendered.spec["encoding"]["x"]["axis"]["format"].endswith("%") or "%" in str(
        rendered.spec["encoding"]["x"]["axis"]
    )


def test_percent_with_table_calculation_is_unsupported(tmp_path: Path, db: Path) -> None:
    calc = instance("[Sales]", "Sum", "[pcto:sum:Sales:qk]", "quantitative").replace(
        "/>", "><table-calc ordering-type='Rows' type='PctTotal' /></column-instance>", 1
    )
    sheet = worksheet(
        "Calc",
        [REGION, calc],
        rows=ref("none:Region:nk"),
        cols=ref("pcto:sum:Sales:qk"),
        view_extra="<percentages enabled='true' mode='table' />",
    )
    ir = parse(tmp_path, sheet)
    ws = ir.worksheets[0]
    if ir.unsupported or not ws.table_calcs:
        pytest.skip("這個寫法的 Table Calculation 沒有解析成功")
    main = worksheet_sql(ir, ws.id)
    with pytest.raises(QueryError, match="Table Calculation"):
        percentages(ir, ws.id, main, execute(db, main.sql), [], lambda q: execute(db, q.sql))


def test_grand_total_label_unchanged(tmp_path: Path, db: Path) -> None:
    ir = parse(tmp_path, with_totals(table("table"), "rows"))
    rendered, _, _ = run(ir, db)
    labels = {v["c0_label"] for v in rendered.spec["data"]["values"]}
    assert GRAND_TOTAL in labels
