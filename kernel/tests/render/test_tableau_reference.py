"""依 Tableau 的畫面（Snow Sports 報表的截圖）修正的行為：文字的顏色、參考區間、圖片的大小、
座標軸的刻度與範圍、數字的自動格式、活頁簿的預設字型。測試資料見 tests/sqlgen/test_query.py。"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from tmig.ir import FieldUse, Marks, ReferenceLine
from tmig.parser.formats import workbook_text
from tmig.render.format import format_number
from tmig.render.vegalite import (
    DATE_TICK_LABEL,
    TICK_COUNT_Y,
    _cell_extent,
    _image_side,
    _Placement,
)

from ..parser.builder import ref, worksheet
from .test_sizes import sized_mark
from .test_text import REGION, SALES, db, parse, render
from .test_text_segments import VALUE, kpi

__all__ = ["db"]  # pytest fixture

COPPER = (
    "<style><style-rule element='mark'><format attr='mark-color' value='#bd8459' />"
    "</style-rule></style>"
)


def test_text_color_format_wins_over_mark_color(tmp_path: Path, db: Path) -> None:
    """文字標記的顏色：各段自己的顏色（fontcolor）最優先；活頁簿（工作表）設定了預設的文字顏色時，
    沒有指定顏色的文字用它，不跟著標記卡的顏色；都沒有時才是標記卡的顏色。"""
    plain = f"<run fontsize='24'>{VALUE}</run>"
    ir = parse(tmp_path, kpi(plain, COPPER))
    assert ir.worksheets[0].marks.mark_color == "#bd8459"
    assert render(ir, db).spec["mark"]["color"] == "#bd8459"  # 只有標記卡的顏色

    themed = ir.model_copy(
        update={"worksheets": [ir.worksheets[0].model_copy(update={"text_color": "#101e47"})]}
    )
    assert render(themed, db).spec["mark"]["color"] == "#101e47"

    # 兩行：第一行沒有指定顏色（用預設的文字顏色，不沿用第二行的），第二行有自己的顏色
    two = (
        f"<run fontsize='15'>{VALUE}</run><run>\n</run>"
        "<run fontsize='20' fontcolor='#bd8459'>2026</run>"
    )
    ir = parse(tmp_path, kpi(two, COPPER))
    themed = ir.model_copy(
        update={"worksheets": [ir.worksheets[0].model_copy(update={"text_color": "#101e47"})]}
    )
    first, second = render(themed, db).spec["layer"]
    assert (first["mark"]["color"], second["mark"]["color"]) == ("#101e47", "#bd8459")
    # 沒有預設的文字顏色：沒有指定顏色的那一行是標記卡的顏色，有指定的照自己的
    recolored = two.replace("#bd8459", "#47679a")
    first, second = render(parse(tmp_path, kpi(recolored, COPPER)), db).spec["layer"]
    assert (first["mark"]["color"], second["mark"]["color"]) == ("#bd8459", "#47679a")


def test_typed_alignment_wins_over_cell_alignment(tmp_path: Path, db: Path) -> None:
    """自訂文字各段的對齊（fontalignment）優先於儲存格的對齊：置中時不靠左。"""
    left = (
        "<style><style-rule element='cell'><format attr='text-align' value='left' />"
        "</style-rule></style>"
    )
    centered = f"<run fontalignment='1' fontsize='16'>{VALUE}</run>"
    assert "align" not in render(parse(tmp_path, kpi(centered, left)), db).spec["mark"]
    plain = f"<run fontsize='16'>{VALUE}</run>"
    assert render(parse(tmp_path, kpi(plain, left)), db).spec["mark"]["align"] == "left"


def line(**values: object) -> ReferenceLine:
    return ReferenceLine(
        id="refline0",
        axis=FieldUse(field="f_date"),
        formula="constant",
        constant="2026-12-01",
        scope="table",
        source_ref="/test",
        **values,  # type: ignore[arg-type]
    )


def test_reference_label_placement() -> None:
    """橫軸上的參考區間（兩條縱向的線配成的）：標籤預設在窗格的下緣、線的右側，顏色是工作表的文字
    顏色；單一條縱向的線照舊在上緣；設定了對齊與字級時依設定。橫向的參考線在窗格的左側、線的上方。"""
    place = _Placement({"x": {"field": "c0", "type": "temporal"}}, "x", [], 1)
    single = place.label("n", "f", "Today", line(), None)
    assert single["encoding"]["y"] == {"value": 0}
    assert (single["mark"]["baseline"], single["mark"]["dy"]) == ("top", 0)
    found = place.label("n", "f", "PEAK SEASON", line(label_size=8, paired="refline1"), "#101e47")
    mark = found["mark"]
    assert found["encoding"]["y"] == {"value": "height"}
    assert (mark["baseline"], mark["align"], mark["dx"], mark["dy"]) == ("bottom", "left", 3, -3)
    assert (mark["color"], mark["fontSize"]) == ("#101e47", 10.67)
    found = place.label("n", "f", "T", line(label_align="left", label_valign="top"), None)
    assert found["encoding"]["y"] == {"value": 0}
    assert (found["mark"]["baseline"], found["mark"]["align"]) == ("top", "right")

    across = _Placement({"y": {"field": "c1", "type": "quantitative"}}, "y", [], 1)
    found = across.label("n", "f", "Average")
    assert found["encoding"]["x"] == {"value": 0}
    assert (found["mark"]["baseline"], found["mark"]["align"]) == ("bottom", "left")
    found = across.label("n", "f", "Average", line(label_align="right", label_valign="bottom"))
    assert found["encoding"]["x"] == {"value": "width"}
    assert (found["mark"]["baseline"], found["mark"]["align"]) == ("top", "right")


def test_image_size_is_relative_to_the_cell() -> None:
    """圖片形狀的大小：欄列都沒有連續的座標軸時相對於儲存格較短的一邊（大小 s 時為 s / 2，最多填滿），
    有連續的座標軸時是固定的像素。"""
    ir_marks = Marks(type="shape", mark_size=0.7796)
    discrete = {"x": {"field": "d", "type": "ordinal"}}
    assert _cell_extent(discrete) == "min(bandwidth('x'), height)"
    assert _image_side(ir_marks, discrete) == "min(bandwidth('x'), height) * 0.3898"
    assert _cell_extent({}) == "min(width, height)"
    scatter = {"x": {"field": "m", "type": "quantitative"}, "y": {"field": "d", "type": "ordinal"}}
    assert _cell_extent(scatter) is None
    assert _image_side(ir_marks, scatter) == 23.39


def test_axis_ticks_and_time_range(tmp_path: Path, db: Path) -> None:
    """縱軸的刻度比 Vega 的預設稀；自動的時間軸兩端各留資料範圍的 5%，線不貼到邊緣。"""
    timeline = {"rows": ref("sum:Sales:qk"), "cols": ref("tmn:Order Date:qk")}
    spec = render(parse(tmp_path, sized_mark("Line", **timeline)), db).spec
    assert spec["encoding"]["y"]["axis"]["tickCount"] == {"expr": TICK_COUNT_Y}
    moments = sorted(v["c0"] for v in spec["data"]["values"])
    scale = spec["encoding"]["x"]["scale"]
    assert scale["nice"] is False and len(scale["domain"]) == 2
    low, high = scale["domain"]
    assert isinstance(low, float) and isinstance(high, float)
    assert low < high and "Jan 01" not in DATE_TICK_LABEL  # 年初的刻度只寫年份
    assert moments  # 有資料
    # 長條不留白（長條有自己的寬度）
    bars = render(parse(tmp_path, sized_mark("Bar", **timeline)), db).spec
    assert "domain" not in (bars["encoding"]["x"].get("scale") or {})


def test_horizontal_bar_labels_get_room(tmp_path: Path, db: Path) -> None:
    """橫向長條尾端的標籤：座標軸多留標籤的寬度，最長的長條的標籤不會被切掉。"""
    sheet = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        mark="Bar",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra="<style><style-rule element='mark'><format attr='mark-labels-show' value='true' />"
        "</style-rule></style>",
    )
    spec = render(parse(tmp_path, sheet), db).spec
    scale = spec["encoding"]["x"]["scale"]
    assert scale["nice"] is False
    top = max(v["c1"] for v in spec["data"]["values"])
    # 最大值進位到六位有效數字（規格不隨彙總的浮點數尾數變動）
    limit = float(scale["domainMax"]["expr"].split(" * width")[0])
    assert top <= limit <= top * 1.00001
    assert " * width / max(width - " in scale["domainMax"]["expr"]


def test_automatic_number_format_is_consistent_per_column() -> None:
    """自動格式：這一欄有不是整數的值時，整欄用一致的小數位數（0.00 與 14.10），不去掉結尾的 0。"""
    assert format_number(0.0, None, fixed=True) == "0.00"
    assert format_number(14.1, None, fixed=True) == "14.10"
    assert format_number(-6.7, None, fixed=True) == "-6.70"
    assert format_number(1234.5, None, fixed=True) == format_number(1234.5, None)
    assert format_number(14.1, None) == "14.1" and format_number(3, None, integer=True) == "3"


def test_workbook_text_defaults() -> None:
    """活頁簿格式的「全部」字型：dashboard 的文字物件與標題沒有指定字型、顏色時用它。"""
    style = etree.fromstring(
        "<style><style-rule element='worksheet'><format attr='font-weight' value='normal' />"
        "</style-rule><style-rule element='all'><format attr='font-family' value='Roboto' />"
        "<format attr='color' value='#101e47' /></style-rule></style>"
    )
    assert workbook_text(style) == ("Roboto", "#101e47")
    assert workbook_text(None) == (None, None)
