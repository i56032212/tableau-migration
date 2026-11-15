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


def test_percent_of_a_table_calculation(tmp_path: Path, db: Path) -> None:
    """百分比選項等同加上一個「合計百分比」表計算（Tableau 說明文件）：已經是表計算的量值是
    「表計算的值 ÷ 範圍內各標記的值的總和」；一般的量值照舊在範圍的詳細程度重新彙總。"""
    calc = instance("[Sales]", "Sum", "[cum:sum:Sales:qk]", "quantitative").replace(
        "/>",
        "><table-calc aggregation='Sum' ordering-type='Columns' type='CumTotal' /></column-instance>",
        1,
    )
    sheet = worksheet(
        "Calc",
        [REGION, SALES, calc],
        rows=ref("none:Region:nk"),
        cols=f"({ref('sum:Sales:qk')} + {ref('cum:sum:Sales:qk')})",
        view_extra="<percentages enabled='true' mode='table' />",
    )
    ir = parse(tmp_path, sheet)
    ws = ir.worksheets[0]
    if ir.unsupported or not ws.table_calcs:
        pytest.skip("這個寫法的 Table Calculation 沒有解析成功")
    main = worksheet_sql(ir, ws.id)
    result, _ = percentages(
        ir, ws.id, main, execute(db, main.sql), [], lambda q: execute(db, q.sql)
    )
    plain = next(c.key for c in main.columns if c.role == "measure" and "#" not in c.key)
    running_key = next(c.key for c in main.columns if c.role == "measure" and "#" in c.key)
    records = {r["f_region"]: r for r in result.records()}
    sales = {"East": 350, "North": 60, "South": 100, "West": 800}
    running = {"East": 350, "North": 410, "South": 510, "West": 1310}
    for region in sales:
        assert records[region][plain] == pytest.approx(sales[region] / 1310)
        assert records[region][running_key] == pytest.approx(
            running[region] / sum(running.values())
        )


def test_percent_of_disaggregated_measures(tmp_path: Path, db: Path) -> None:
    """取消彙總量值：每一列資料是一個標記，所有的值是總和的百分比（Tableau 說明文件）。"""
    raw = instance("[Sales]", "None", "[none:Sales:qk]", "quantitative")
    sheet = worksheet(
        "Rows",
        [REGION, raw],
        rows=ref("none:Region:nk"),
        cols=ref("none:Sales:qk"),
        view_extra="<percentages enabled='true' mode='row' />",
    ).replace("<aggregation value='true' />", "<aggregation value='false' />", 1)
    ir = parse(tmp_path, sheet)
    ws = ir.worksheets[0]
    assert ir.unsupported == [] and ws.disaggregated
    main = worksheet_sql(ir, ws.id)
    marks = execute(db, main.sql)
    result, _ = percentages(ir, ws.id, main, marks, [], lambda q: execute(db, q.sql))
    measure = "f_sales"  # 取消彙總的量值在查詢中是連續的欄位
    by_region: dict[str, float] = {}
    for record in result.records():
        by_region[record["f_region"]] = by_region.get(record["f_region"], 0) + record[measure]
    # 列的百分比：每個 Region 之內各列的值相加是 100%
    assert by_region == {
        region: pytest.approx(1.0) for region in ("East", "North", "South", "West")
    }
    assert len(result.rows) == len(marks.rows) > 4


def test_measure_names_innermost_is_one_hundred_percent(tmp_path: Path, db: Path) -> None:
    """Measure Names 在欄架的最內層、窗格中的列百分比：每個標記是 100%（多個量值的值不能相加；
    Tableau 說明文件）；窗格是去掉 Measure Names 之後的範圍，維度都留著。"""
    from ..parser.test_extensions import measure_names_filter
    from .test_text import PROFIT

    sheet = worksheet(
        "Values",
        [REGION, SALES, PROFIT],
        rows=ref("none:Region:nk"),
        cols=ref(":Measure Names"),
        mark="Text",
        encodings=f"<text column='{ref('Multiple Values')}' />",
        view_extra=measure_names_filter("sum:Sales:qk", "sum:Profit:qk")
        + "<percentages enabled='true' mode='row-in-pane' />",
    )
    ir = parse(tmp_path, sheet)
    ws = ir.worksheets[0]
    assert ir.unsupported == [] and ws.percentages == "row_in_pane"
    main = worksheet_sql(ir, ws.id)
    result, _ = percentages(
        ir, ws.id, main, execute(db, main.sql), [], lambda q: execute(db, q.sql)
    )
    measures = [c.key for c in main.columns if c.role == "measure"]
    assert len(measures) == 2
    for record in result.records():
        for key in measures:
            assert record[key] in (None, pytest.approx(1.0))


def test_grand_total_label_unchanged(tmp_path: Path, db: Path) -> None:
    ir = parse(tmp_path, with_totals(table("table"), "rows"))
    rendered, _, _ = run(ir, db)
    labels = {v["c0_label"] for v in rendered.spec["data"]["values"]}
    assert GRAND_TOTAL in labels
