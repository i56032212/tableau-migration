"""畫圖的附註裡稱呼東西的方式（tmig.render.names）：與使用者在畫面上看到的相同，不寫內部的代號。

2026-11-21 起附註會顯示給作者與看報表的人（檢視器、編輯器左下角的標示）；原本的寫法是「參考線 refline0 …」
「預測（sum(f_profit)）…」「…的標記類型 pie 尚未支援」。這裡固定每一種附註的文字：欄位是膠囊上的寫法
（SUM(Sales)）、參考線是「分析」分頁清單上的寫法、註解是它的文字、標記類型是選單上的名稱。預測的附註見
tests/sqlgen/test_analytics.py，地圖的見 test_maps.py。測試資料見 tests/sqlgen/test_query.py。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

from tmig.data.executor import execute
from tmig.ir import IR, Annotation, AnnotationPoint, FieldUse, TextRun, Worksheet
from tmig.ir.models import (
    MEASURE_VALUES,
    SPECIAL_FIELDS,
    DateDerivation,
    MapStyle,
    Marks,
    MarkType,
    Pane,
    ParamRef,
    ReferenceLine,
    TableCalc,
)
from tmig.parser.workbook import parse_workbook
from tmig.render.names import (
    MAP_STYLE_NAMES,
    MARK_NAMES,
    REFERENCE_NAMES,
    SPECIAL_NAMES,
    annotation_name,
    mark_name,
    pill_name,
    reference_name,
)
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_extensions import db
from .test_vegalite import validator

__all__ = ["db"]  # pytest fixture

REGION = instance("[Region]", "None", "[none:Region:nk]")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
PROFIT = instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative")
SUM_SALES = FieldUse(field="f_sales", agg="sum", continuous=True)
SUM_PROFIT = FieldUse(field="f_profit", agg="sum", continuous=True)
INTERNAL = re.compile(r"\bf_[a-z_]+\b|refline\d|\b(sum|avg|min|max)\(|\bws_|__")
"""附註裡不該出現的內部代號：欄位 id（f_sales）、參考線的編號（refline0）、輸出欄名（sum(f_sales)）、
工作表 id 與資料欄的名稱。"""


def bars(tmp_path: Path) -> IR:
    """長條圖：列架上是 Region，欄架上是 SUM(Sales)。"""
    sheet = worksheet(
        "Bars", [REGION, SALES, PROFIT], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


def notes_of(ir: IR, db: Path, **changes: Any) -> list[str]:
    """工作表改掉幾個欄位之後畫圖的附註（查詢照改過的工作表）。"""
    sheet: Worksheet = ir.worksheets[0].model_copy(update=changes)
    patched = ir.model_copy(update={"worksheets": [sheet]})
    query = worksheet_sql(patched, sheet.id)
    rendered = build_spec(patched, sheet.id, query.columns, execute(db, query.sql))
    assert rendered.spec is not None, rendered.unsupported
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered.notes


def line(**settings: Any) -> ReferenceLine:
    base: dict[str, Any] = {
        "id": "refline7",
        "axis": SUM_SALES,
        "value": SUM_SALES,
        "formula": "average",
        "scope": "table",
        "source_ref": "test",
    }
    return ReferenceLine.model_validate({**base, **settings})


def test_names_are_written_as_on_screen(tmp_path: Path) -> None:
    """欄位是膠囊上的寫法、參考線是「分析」分頁清單上的寫法、註解是它的文字（與註解對話框的清單相同）、
    標記類型與背景地圖的樣式是選單上的名稱。"""
    ir = bars(tmp_path)
    fields = {f.id: f for d in ir.datasources for f in d.fields}
    # 欄位：彙總與日期的層級寫在前面，維度只有名稱；Tableau 產生的欄位用它的名稱
    assert pill_name(SUM_SALES, fields) == "SUM(Sales)"
    assert pill_name(FieldUse(field="f_profit", agg="avg", continuous=True), fields) == (
        "AVG(Profit)"
    )
    assert pill_name(FieldUse(field="f_order_id", agg="countd"), fields) == "CNTD(Order ID)"
    assert pill_name(FieldUse(field="f_region"), fields) == "Region"
    year = FieldUse(field="f_order_date", date=DateDerivation(kind="part", unit="year"))
    assert pill_name(year, fields) == "YEAR(Order Date)"
    assert pill_name(FieldUse(field=MEASURE_VALUES), fields) == "Measure Values"

    # 參考線：計算與所在座標軸的量值；參考區間、分布與盒鬚圖各有寫法
    assert reference_name(line(), fields) == "平均：SUM(Sales)"
    assert reference_name(line(formula="constant", value=None, constant=100), fields) == (
        "常數：SUM(Sales)"
    )
    assert reference_name(line(formula="min", paired="refline8"), fields) == (
        "最小值（區間）：SUM(Sales)"
    )
    spread = line(kind="distribution", formula="percentile", percentiles=[60, 80])
    assert reference_name(spread, fields) == "分布（百分位數）：SUM(Sales)"
    assert reference_name(line(kind="boxplot", formula="quantiles"), fields) == (
        "盒鬚圖：SUM(Sales)"
    )

    # 註解：它的文字，欄位與參數寫成 <名稱>；換行寫成空白，太長時截短；沒有文字時是清單上的第幾個
    typed = Annotation(
        kind="mark",
        text=[
            TextRun(text="東區：\n"),
            TextRun(use=SUM_SALES),
            TextRun(text=" / "),
            TextRun(parameter=ir.parameters[0].id),
        ],
        point=AnnotationPoint(mark={"f_region": "East"}),
    )
    caption = ir.parameters[0].caption
    assert annotation_name(typed, 0, fields, ir.parameters) == (
        f"註解「東區： <SUM(Sales)> / <{caption}>」"
    )
    long = typed.model_copy(update={"text": [TextRun(text="一二三四五六七八九十" * 4)]})
    assert annotation_name(long, 0, fields) == f"註解「{'一二三四五六七八九十' * 3}…」"
    assert annotation_name(typed.model_copy(update={"text": []}), 2, fields) == "第 3 個註解"

    # 標記類型與背景地圖的樣式：選單上的名稱
    assert (mark_name("pie"), mark_name("gantt"), mark_name("bar")) == ("圓餅", "甘特條", "長條")
    assert MARK_NAMES["polygon"] == "多邊形" and MAP_STYLE_NAMES["satellite"] == "衛星"


def test_every_ir_value_has_a_name() -> None:
    """IR 裡每一種標記類型、背景地圖的樣式與參考線的計算都有名稱：以後加了新的種類而忘了取名字時，附註裡
    不會又冒出內部的代號（或在畫圖時出錯）。"""
    formulas = get_args(ReferenceLine.model_fields["formula"].annotation)
    assert set(get_args(MarkType)) == set(MARK_NAMES)
    assert set(get_args(MapStyle)) == set(MAP_STYLE_NAMES)
    assert set(formulas) == set(REFERENCE_NAMES)
    assert set(SPECIAL_FIELDS) == set(SPECIAL_NAMES)


def test_quick_table_calculation_pill_has_a_delta(tmp_path: Path) -> None:
    """快速 Table Calculation 的膠囊後面有「△」（與編輯器的膠囊相同）；計算欄位裡的 Table Calculation 沒有。"""
    ir = bars(tmp_path)
    fields = {f.id: f for d in ir.datasources for f in d.fields}
    calcs = [
        TableCalc(id="tc_running", type="running_total", preset="table_across", source_ref="test"),
        TableCalc(id="tc_formula", type="formula", preset="table_across", source_ref="test"),
    ]
    running = SUM_SALES.model_copy(update={"table_calc": "tc_running"})
    assert pill_name(running, fields, calcs) == "SUM(Sales) △"
    formula = SUM_SALES.model_copy(update={"table_calc": "tc_formula"})
    assert pill_name(formula, fields, calcs) == "SUM(Sales)"
    assert pill_name(running, fields) == "SUM(Sales)"


def test_reference_line_notes_name_the_line(tmp_path: Path, db: Path) -> None:
    """參考線畫不出來時的附註以「分析」分頁清單上的寫法稱呼它，不寫它的編號（原本是「參考線 refline7 …」）。"""
    ir = bars(tmp_path)
    # 量值不在座標軸上：參考線在 SUM(Profit) 的軸上，檢視裡只有 SUM(Sales) 的軸
    off_axis = line(axis=SUM_PROFIT, value=SUM_PROFIT)
    assert notes_of(ir, db, reference_lines=[off_axis]) == [
        "參考線「平均：SUM(Profit)」的量值不在座標軸上，沒有畫出"
    ]
    # 值不在查詢結果中：參數的值不是數字
    text = next(p for p in ir.parameters if p.datatype == "string")
    by_parameter = line(value=ParamRef(parameter=text.id), formula="constant")
    assert notes_of(ir, db, reference_lines=[by_parameter]) == [
        "參考線「常數：SUM(Sales)」的值不在查詢結果中，沒有畫出"
    ]
    # 盒鬚圖在不存在的軸上
    box = line(kind="boxplot", formula="quantiles", axis=SUM_PROFIT, value=SUM_PROFIT)
    (note,) = notes_of(ir, db, reference_lines=[box])
    assert note == "參考線「盒鬚圖：SUM(Profit)」的量值不在座標軸上，沒有畫出"
    assert not INTERNAL.search(note)


def test_unsupported_mark_note_names_the_measure_and_the_mark(tmp_path: Path, db: Path) -> None:
    """同一架上有多個量值、其中一個量值的標記類型畫不了：附註寫膠囊上的量值與選單上的標記類型
    （原本是「Profit 的標記類型 pie 尚未支援，以長條呈現」）。"""
    sheet = worksheet(
        "Two",
        [REGION, SALES, PROFIT],
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Region:nk"),
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    panes = [
        Pane(measure=SUM_SALES, axis="rows", marks=Marks(type="bar")),
        Pane(measure=SUM_PROFIT, axis="rows", marks=Marks(type="pie")),
    ]
    notes = notes_of(ir, db, panes=panes)
    assert notes == ["SUM(Profit) 的標記類型「圓餅」尚未支援，以長條呈現"]
    assert not INTERNAL.search(notes[0])


def test_annotation_notes_quote_the_annotation(tmp_path: Path, db: Path) -> None:
    """註解畫不出來時的附註引用它的文字（與註解對話框的清單相同），沒有文字時才寫第幾個
    （原本是「註解 1 的標記…」）。"""
    ir = bars(tmp_path)
    missing = Annotation(
        kind="mark",
        text=[TextRun(text="看這裡：\n"), TextRun(use=SUM_SALES)],
        point=AnnotationPoint(mark={"f_region": "Mars"}),
    )
    discrete = FieldUse(field="f_region")
    # 點與區域的位置在離散的座標上（平台還不會畫）
    point = Annotation(
        kind="point",
        text=[TextRun(text="目標")],
        point=AnnotationPoint(x=500, x_axis=SUM_SALES, y="East", y_axis=discrete),
    )
    area = Annotation(
        kind="area",
        text=[],
        top_left=AnnotationPoint(x=200, x_axis=SUM_SALES, y="East", y_axis=discrete),
        bottom_right=AnnotationPoint(x=400, x_axis=SUM_SALES, y="West", y_axis=discrete),
    )
    notes = notes_of(ir, db, annotations=[missing, point, area])
    assert notes == [
        "註解「看這裡： <SUM(Sales)>」的標記不在目前的資料中，沒有畫出",
        "註解「目標」的位置不在這張圖的座標軸上，沒有畫出",
        "第 3 個註解的區域不在這張圖的座標軸上，沒有畫出",
    ]
    assert not any(INTERNAL.search(note) for note in notes)
