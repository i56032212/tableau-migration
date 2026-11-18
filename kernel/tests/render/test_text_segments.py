"""同一行內格式不同的自訂文字（例如 24pt 的數字接著 16pt 的單位）：分成各段、每一段一個文字圖層，
水平的位置由檢視器量出寬度後填入（usermeta.text_segments）。測試資料見 tests/sqlgen/test_query.py。"""

from __future__ import annotations

from pathlib import Path

from ..parser.builder import ref, worksheet
from .test_text import SALES, db, parse, render
from .test_vegalite import validator

__all__ = ["db"]  # pytest fixture


def kpi(label: str, style: str = "") -> str:
    """只有一格的文字（KPI）：自訂的標籤文字。"""
    return worksheet(
        "KPI",
        [SALES],
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=f"<customized-label><formatted-text>{label}</formatted-text></customized-label>"
        + style,
    )


VALUE = f"&lt;{ref('sum:Sales:qk')}&gt;"


def test_mixed_sizes_on_one_line_are_split_into_segments(tmp_path: Path, db: Path) -> None:
    """24pt 的數字接著 16pt 的單位：兩段各一個文字圖層、各用自己的字級，共用同一條基線；
    位移的欄位由檢視器填入，各段的字型寫在 usermeta.text_segments。"""
    label = f"<run bold='true' fontsize='24'>{VALUE}</run><run bold='true' fontsize='16'>CM</run>"
    ir = parse(tmp_path, kpi(label))
    assert ir.unsupported == []
    spec = render(ir, db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    (group,) = spec["usermeta"]["text_segments"]
    first, second = group["segments"]
    assert (first["field"], first["dx"]) == ("__text0_0_0", "__text0_0_0_dx")
    assert (second["field"], second["dx"]) == ("__text0_0_1", "__text0_0_1_dx")
    assert first["font"].startswith("700 32.0px ") and second["font"].startswith("700 21.33px ")
    big, small = spec["layer"]
    assert (big["mark"]["fontSize"], small["mark"]["fontSize"]) == (32.0, 21.33)
    assert big["encoding"]["text"]["field"] == "__text0_0_0"
    assert small["encoding"]["text"]["field"] == "__text0_0_1"
    for layer, segment in ((big, first), (small, second)):
        mark = layer["mark"]
        # 靠左畫在檢視器算出的位移上；兩段同一條基線
        assert (mark["align"], mark["baseline"]) == ("left", "alphabetic")
        assert mark["dy"] == big["mark"]["dy"]
        assert f'datum["{segment["dx"]}"]' in mark["dx"]["expr"]
    record = spec["data"]["values"][0]
    assert record["__text0_0_1"] == "CM"
    assert record["__text0"] == record["__text0_0_0"] + "CM"
    # 這一行的對齊：只有一格的文字置中（定位點在這一行的中間）
    assert group["align"] == "center"


def test_alignment_colors_and_lines(tmp_path: Path, db: Path) -> None:
    """靠左對齊的儲存格定位點在左邊；各段的顏色依各自的格式；多行時只有格式不同的那一行分段。"""
    label = (
        "<run fontsize='9'>Sales</run><run>\n</run>"
        f"<run fontsize='24' fontcolor='#bd8459'>{VALUE}</run>"
        "<run fontsize='12' fontcolor='#47679a'> EUR</run>"
    )
    left = (
        "<style><style-rule element='cell'><format attr='text-align' value='left' />"
        "</style-rule></style>"
    )
    spec = render(parse(tmp_path, kpi(label, left)), db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    (group,) = spec["usermeta"]["text_segments"]
    assert group["align"] == "left" and len(group["segments"]) == 2
    title, value, unit = spec["layer"]
    # 第一行只有一種格式：整行一個圖層，沒有位移的運算式
    assert title["encoding"]["text"]["field"] == "__text0_0"
    assert not isinstance(title["mark"].get("dx"), dict)
    assert (value["mark"]["color"], unit["mark"]["color"]) == ("#bd8459", "#47679a")
    assert (value["mark"]["fontSize"], unit["mark"]["fontSize"]) == (32.0, 16.0)
    assert value["mark"]["dy"] == unit["mark"]["dy"] > title["mark"]["dy"]
    assert spec["data"]["values"][0]["__text0_1_1"] == " EUR"


def test_single_format_text_is_not_segmented(tmp_path: Path, db: Path) -> None:
    """整行同一種格式（或只有空白不同）：照舊是一個文字標記，沒有 text_segments。"""
    label = f"<run fontsize='24'>{VALUE}</run><run fontsize='24'> CM</run>"
    spec = render(parse(tmp_path, kpi(label)), db).spec
    assert "text_segments" not in spec["usermeta"]
    assert spec["mark"]["type"] == "text"
