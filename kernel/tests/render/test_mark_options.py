"""標記與座標軸的其他選項：堆疊標記的開關、折線上的標記與光暈、色彩上多個欄位的組合、標籤的方向、換行
與長條上的位置、次要刻度，以及各窗格獨立的座標軸範圍。

測試資料見 tests/sqlgen/test_query.py：各區的 SUM(Sales) East 350、North 60、South 100、West 800。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tmig.ir import (
    IR,
    Axis,
    ColorAssignment,
    ColorPalette,
    FieldUse,
    MarkLabels,
    MemberAlias,
)

from ..parser.builder import instance, ref, worksheet
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, parse, render

__all__ = ["db"]  # pytest fixture

MONTH = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")
SALES_USE = FieldUse(field="f_sales", agg="sum", continuous=True)
REGION_COLOR = f"<color column='{ref('none:Region:nk')}' />"


def with_sheet(ir: IR, **update: Any) -> IR:
    sheet = ir.worksheets[0].model_copy(update=update)
    return ir.model_copy(update={"worksheets": [sheet]})


def with_marks(ir: IR, **update: Any) -> IR:
    return with_sheet(ir, marks=ir.worksheets[0].marks.model_copy(update=update))


def layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return spec.get("layer", [spec])


def test_stack_marks_on_and_off(tmp_path: Path, db: Path) -> None:
    """堆疊標記：自動時不寫（長條與區域由 Vega-Lite 堆疊）；關閉為 stack: null（從 0 畫起、互相重疊），
    開啟時折線也依色彩的維度累加。"""
    bars = worksheet(
        "Bars",
        [CATEGORY, REGION, SALES],
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        mark="Bar",
        encodings=REGION_COLOR + f"<text column='{ref('sum:Sales:qk')}' />",
    )
    ir = parse(tmp_path, bars)
    automatic = render(ir, db).spec
    assert "stack" not in automatic["encoding"]["x"]
    # 自動堆疊的長條：標籤在各段內側（文字也堆疊）
    assert any(
        layer.get("encoding", {}).get("x", {}).get("stack") == "zero" for layer in layers(automatic)
    )
    off = render(with_sheet(ir, stack="off"), db).spec
    assert off["encoding"]["x"]["stack"] is None
    # 不堆疊時標籤回到長條的尾端（不是各段內側）
    text = next(layer for layer in layers(off) if layer["mark"]["type"] == "text")
    assert "x" not in text["encoding"] and text["mark"]["align"] == "left"

    (tmp_path / "line").mkdir()
    line = worksheet(
        "Trend",
        [MONTH, SALES, REGION],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=REGION_COLOR,
    )
    ir = parse(tmp_path / "line", line)
    assert "stack" not in render(ir, db).spec["encoding"]["y"]
    assert render(with_sheet(ir, stack="on"), db).spec["encoding"]["y"]["stack"] == "zero"


def test_line_markers_and_halo(tmp_path: Path, db: Path) -> None:
    line = worksheet(
        "Trend",
        [MONTH, SALES],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
    )
    ir = parse(tmp_path, line)
    assert render(ir, db).spec["mark"]["point"] == "transparent"
    marked = render(with_marks(ir, markers="all"), db).spec
    assert marked["mark"]["point"] == {"filled": True, "size": 36}

    (tmp_path / "dots").mkdir()
    dots = worksheet(
        "Dots",
        [REGION, SALES, PROFIT],
        rows=ref("sum:Sales:qk"),
        cols=ref("sum:Profit:qk"),
        mark="Circle",
    )
    ir = parse(tmp_path / "dots", dots)
    assert "stroke" not in render(ir, db).spec["mark"]
    halo = render(with_marks(ir, halo=True), db).spec["mark"]
    assert (halo["stroke"], halo["strokeWidth"]) == ("#ffffff", 1)
    # 另外設定了框線時以框線為準
    bordered = render(with_marks(ir, halo=True, stroke="#000000"), db).spec["mark"]
    assert bordered["stroke"] == "#000000"


def test_several_fields_on_color_are_combined(tmp_path: Path, db: Path) -> None:
    """色彩上有多個維度：以各欄位值的組合著色，圖例的成員為「Region, Category」。"""
    encodings = REGION_COLOR + f"<color column='{ref('none:Category:nk')}' />"
    sheet = worksheet(
        "S", [REGION, CATEGORY, SALES], cols=ref("sum:Sales:qk"), mark="Bar", encodings=encodings
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    marks = ir.worksheets[0].marks
    assert [u.field for u in marks.color_more] == ["f_category"]
    rendered = render(ir, db)
    spec = rendered.spec
    color = spec["encoding"]["color"]
    assert color["field"] == "__color1" and color["title"] == "Region, Category"
    values = spec["data"]["values"]
    combos = {v["__color1"] for v in values}
    assert all(", " in combo for combo in combos) and len(combos) == len(values)
    # 圖例依第一個欄位、再依第二個欄位排序；每個組合一個顏色
    domain = color["scale"]["domain"]
    assert domain == sorted(combos) and len(color["scale"]["range"]) == len(domain)
    # 指定的顏色以組合的文字對應
    first = domain[0]
    palette = ColorPalette(
        kind="discrete", assignments=[ColorAssignment(value=first, color="#123456")]
    )
    painted = render(with_marks(ir, color_palette=palette), db).spec["encoding"]["color"]["scale"]
    assert painted["range"][0] == "#123456"
    # 其他的色彩欄位不在詳細資料中時不作用（依第一個欄位著色）
    plain = render(with_marks(ir, detail=[]), db).spec["encoding"]["color"]
    assert plain["field"] != "__color1"


def test_label_direction_wrap_and_bar_position(tmp_path: Path, db: Path) -> None:
    def label(ir: IR) -> dict[str, Any]:
        spec = render(ir, db).spec
        return next(layer for layer in layers(spec) if layer["mark"]["type"] == "text")

    bars = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        mark="Bar",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    ir = parse(tmp_path, bars)
    end = label(ir)
    assert (end["mark"]["align"], end["mark"]["dx"]) == ("left", 3) and "x" not in end["encoding"]
    # 橫的長條：水平對齊靠左在起點（0），置中在長條的中間（量值的一半）
    start = label(with_marks(ir, cell_align="left"))
    assert start["encoding"]["x"] == {"datum": 0, "type": "quantitative"}
    middle = label(with_marks(ir, cell_align="center"))
    assert middle["mark"]["align"] == "center" and middle["encoding"]["x"]["field"] == "__bar_mid"
    assert middle["transform"][-1]["as"] == "__bar_mid"
    # 文字方向：由下往上時文字轉 270 度，仍在長條尾端的右邊（文字的上緣朝左）
    up = label(with_marks(ir, labels=MarkLabels(show=True, direction="up")))
    assert (up["mark"]["angle"], up["mark"]["align"], up["mark"]["baseline"]) == (
        270,
        "center",
        "top",
    )
    assert up["mark"]["dy"] == 3 and "dx" not in up["mark"]
    down = label(with_marks(ir, labels=MarkLabels(show=True, direction="down")))
    assert (down["mark"]["angle"], down["mark"]["baseline"], down["mark"]["dy"]) == (
        90,
        "bottom",
        -3,
    )

    (tmp_path / "columns").mkdir()
    columns = worksheet(
        "Columns",
        [REGION, SALES],
        rows=ref("sum:Sales:qk"),
        cols=ref("none:Region:nk"),
        mark="Bar",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    ir = parse(tmp_path / "columns", columns)
    top = label(ir)
    assert (top["mark"]["baseline"], top["mark"]["dy"]) == ("bottom", -3)
    # 直的長條：垂直對齊靠下在起點，置中在中間；由下往上的文字從長條的頂端往上寫
    assert label(with_marks(ir, label_valign="bottom"))["encoding"]["y"]["datum"] == 0
    assert label(with_marks(ir, label_valign="center"))["encoding"]["y"]["field"] == "__bar_mid"
    up = label(with_marks(ir, labels=MarkLabels(show=True, direction="up")))
    assert (up["mark"]["align"], up["mark"]["baseline"], up["mark"]["dx"]) == ("left", "middle", 3)

    # 換行：超過寬度的標籤文字在空白處換行（資料中另有換好行的欄）
    (tmp_path / "wrap").mkdir()
    long = worksheet(
        "Dots",
        [REGION, SALES, PROFIT],
        rows=ref("sum:Sales:qk"),
        cols=ref("sum:Profit:qk"),
        mark="Circle",
        encodings=f"<text column='{ref('none:Region:nk')}' />",
    )
    ir = parse(tmp_path / "wrap", long)
    alias = "一個很長很長很長很長很長很長很長很長很長很長的名稱"
    field = next(f for f in ir.datasources[0].fields if f.id == "f_region")
    aliased = field.model_copy(update={"aliases": [MemberAlias(value="East", label=alias)]})
    ds = ir.datasources[0].model_copy(
        update={"fields": [aliased if f.id == "f_region" else f for f in ir.datasources[0].fields]}
    )
    ir = ir.model_copy(update={"datasources": [ds]})
    wrapped = render(with_marks(ir, labels=MarkLabels(show=True, wrap=True)), db).spec
    text = next(layer for layer in layers(wrapped) if layer["mark"]["type"] == "text")
    assert text["encoding"]["text"]["field"] == "__wrap1"
    east = next(v for v in wrapped["data"]["values"] if "\n" in str(v.get("__wrap1")))
    assert east["__wrap1"].replace("\n", "") == alias


def test_minor_ticks(tmp_path: Path, db: Path) -> None:
    bars = worksheet("Bars", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    ir = parse(tmp_path, bars)
    axis = Axis(use=SALES_USE, shelf="columns", tick_spacing=200, minor_spacing=50)
    spec = render(with_sheet(ir, axes=[axis]), db).spec
    found = spec["encoding"]["x"]["axis"]
    # 刻度涵蓋座標軸可能的範圍（各區的合計 1310），每隔 50 一個
    assert found["values"] == [float(v) for v in range(0, 1301, 50)]
    # 主要刻度（200 的倍數）才有文字與格線，次要刻度較短
    assert found["labelExpr"].startswith("abs((datum.value - 0.0) / 200.0")
    assert found["labelExpr"].endswith(": ''")
    assert found["tickSize"]["value"] == 3 and found["tickSize"]["condition"]["value"] == 5
    assert found["gridOpacity"]["value"] == 0
    # 沒有固定的主要刻度時次要刻度不作用
    loose = Axis(use=SALES_USE, shelf="columns", minor_spacing=50)
    assert "values" not in render(with_sheet(ir, axes=[loose]), db).spec["encoding"]["x"].get(
        "axis", {}
    )


def test_independent_axis_ranges(tmp_path: Path, db: Path) -> None:
    """各窗格獨立的座標軸範圍：列架上的量值在每一列的窗格各自決定範圍。"""
    sheet = worksheet(
        "Panes",
        [REGION, MONTH, SALES],
        rows=f"({ref('none:Region:nk')} / {ref('sum:Sales:qk')})",
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
    )
    ir = parse(tmp_path, sheet)
    shared = render(ir, db).spec
    assert shared["resolve"]["scale"]["y"] == "shared"
    axis = Axis(use=SALES_USE, shelf="rows", independent=True)
    free = render(with_sheet(ir, axes=[axis]), db).spec
    assert free["resolve"]["scale"] == {
        "x": "shared",
        "y": "independent",
        "color": "shared",
        "size": "shared",
    }
    assert len(free["vconcat"]) == len(shared["vconcat"]) > 1
