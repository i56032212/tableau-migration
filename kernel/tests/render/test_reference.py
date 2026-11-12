"""參考區間、分布與盒鬚圖：底色畫在標記之下、線畫在標記之上，預設的格式依 Tableau 的截圖量測。

測試資料見 tests/sqlgen/test_query.py（每張訂單的銷售額：東 150、200，西 300、500，南 80、20，北 60）。
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any

import pytest

from tmig.render.trends import t_quantile

from ..parser.builder import instance, ref, worksheet
from ..parser.test_extensions import refline
from .test_text import REGION, SALES, db, parse, render
from .test_vegalite import validator

__all__ = ["db"]  # pytest fixture

ORDER = instance("[Order ID]", "None", "[none:Order ID:nk]")
ORDER_SALES = [150.0, 200.0, 300.0, 500.0, 80.0, 20.0, 60.0]


def dots(lines: str, style: str = "") -> str:
    """每個地區一欄、每張訂單一個點（7 個）。"""
    sheet = worksheet(
        "Dots",
        [REGION, ORDER, SALES],
        rows=ref("sum:Sales:qk"),
        cols=ref("none:Region:nk"),
        mark="Circle",
        encodings=f"<lod column='{ref('none:Order ID:nk')}' />",
        pane_extra=lines,
    )
    return sheet.replace("<style />", style, 1) if style else sheet


def layers(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["name"]: item
        for item in spec["layer"]
        if str(item.get("name", "")).startswith("__ref")
    }


def first(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["data"]["values"][0]


def test_reference_band_between_paired_lines(tmp_path: Path, db: Path) -> None:
    """兩條參考線以 paired-id 配成參考區間：之間塗上底色（預設的灰色），兩端預設不畫線。"""
    lines = refline(0, "constant", extra=" paired-id='refline1' value='100'") + refline(
        1, "constant", extra=" paired-id='refline0' value='300'"
    )
    spec = render(parse(tmp_path, dots(lines)), db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    named = layers(spec)
    band = named["__ref1_band"]
    assert spec["layer"][0] is band  # 畫在標記之下
    assert band["mark"] == {"type": "rect", "color": "#e0e0e0", "tooltip": False}
    assert (band["encoding"]["y"]["field"], band["encoding"]["y2"]["field"]) == (
        "__ref0_0",
        "__ref1_0",
    )
    assert "x" not in band["encoding"]  # 橫跨整個窗格
    assert not any(name in named for name in ("__ref0_0", "__ref1_0"))
    assert (first(spec)["__ref0_0"], first(spec)["__ref1_0"]) == (100, 300)
    # 格式設定的底色與線
    style = (
        "<style><style-rule element='refband'><format attr='fill-color' id='refline1' value='#ffcc00' />"
        "</style-rule><style-rule element='refline'><format attr='line-visibility' id='refline0' value='on' />"
        "</style-rule></style>"
    )
    spec = render(parse(tmp_path, dots(lines, style)), db).spec
    named = layers(spec)
    assert named["__ref1_band"]["mark"]["color"] == "#ffcc00"
    assert named["__ref0_0"]["mark"]["type"] == "rule" and "__ref1_0" not in named


def test_distributions(tmp_path: Path, db: Path) -> None:
    """百分位數、分位數、信賴區間與百分比：各值之間由淺到深塗上底色；值與獨立計算的結果相同。"""
    lines = "".join(
        [
            refline(
                0,
                "percentile",
                values="<reference-line-value percentile='25' /><reference-line-value percentile='75' />",
            ),
            refline(1, "quantiles", extra=" tile-count='4' fill-above='true' fill-below='true'"),
            refline(2, "confidence", extra=" probability='95'"),
            refline(
                3,
                "average",
                values="<reference-line-value percentage='50' /><reference-line-value percentage='100' />",
            ),
            refline(4, "medianconfidence", extra=" probability='90'"),
        ]
    )
    rendered = render(parse(tmp_path, dots(lines)), db)
    spec = rendered.spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    row = first(spec)
    ordered = sorted(ORDER_SALES)
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    assert (row["__ref0_0"], row["__ref0_1"]) == pytest.approx((quartiles[0], quartiles[2]))
    assert [row[f"__ref1_{i}"] for i in range(3)] == pytest.approx(quartiles)
    mean = statistics.fmean(ORDER_SALES)
    margin = t_quantile(0.975, 6) * statistics.stdev(ORDER_SALES) / math.sqrt(7)
    assert (row["__ref2_0"], row["__ref2_1"]) == pytest.approx((mean - margin, mean + margin))
    assert (row["__ref3_0"], row["__ref3_1"]) == pytest.approx((mean / 2, mean))
    # 中位數的 90% 信賴區間（7 個值：第 2 小到第 6 小，涵蓋機率 0.875 不足時往外一格到 0.984）
    assert (row["__ref4_0"], row["__ref4_1"]) == (ordered[0], ordered[6])
    named = layers(spec)
    assert {f"__ref1_fill{i}" for i in range(2)} | {"__ref1_above", "__ref1_below"} <= set(named)
    shades = [named[f"__ref1_fill{i}"]["mark"]["color"] for i in range(2)]
    assert shades == ["#f6f6f6", "#eeeeee"]  # 由內往外變深，最外側為 #dddddd
    assert named["__ref1_above"]["mark"]["color"] == "#dddddd"
    assert named["__ref1_above"]["encoding"]["y2"] == {"value": 0}
    assert named["__ref1_below"]["encoding"]["y2"] == {"value": "height"}
    # 分布預設不畫線；自動的標籤為各值的名稱
    assert not any(name.startswith("__ref0_0") and not name.endswith("label") for name in named)
    labels = {k: v["encoding"]["text"]["value"] for k, v in named.items() if k.endswith("label")}
    assert labels["__ref0_0_label"] == "25th Percentile"
    assert labels["__ref3_1_label"] == "100% of Average"
    assert labels["__ref2_0_label"] == "Lower Confidence Interval"


def test_box_plot(tmp_path: Path, db: Path) -> None:
    """盒鬚圖（每個地區）：四分位數以線性內插，鬚延伸到 1.5 倍四分位距內最遠的值；畫在標記之下。"""
    box = refline(
        0,
        "average",
        "per-cell",
        extra=" boxplot-mark-exclusion='false' boxplot-whisker-type='standard'",
    )
    spec = render(parse(tmp_path, dots(box)), db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    by_region: dict[str, dict[str, Any]] = {}
    for v in spec["data"]["values"]:
        by_region.setdefault(v["c0_label"], v)
    east = by_region["East"]
    assert [east[f"__ref0_{k}"] for k in ("q1", "median", "q3", "low", "high")] == [
        162.5,
        175.0,
        187.5,
        150.0,
        200.0,
    ]
    named = layers(spec)
    names = ["__ref0_whisker_low", "__ref0_whisker_high", "__ref0_box_low", "__ref0_box_high"]
    assert [item.get("name") for item in spec["layer"][:4]] == names
    lower = named["__ref0_box_low"]
    assert lower["mark"] == {
        "type": "bar",
        "color": "#e6e6e6",
        "tooltip": False,
        "width": {"band": 0.5},
    }
    assert lower["encoding"]["x"]["field"] == "c0_label"
    assert lower["transform"][0]["groupby"] == ["c0", "c0_label"]
    assert named["__ref0_whisker_low"]["encoding"]["x"]["bandPosition"] == 0.5
    # 隱藏底下的標記：只顯示鬚以外的標記
    hidden = box.replace("boxplot-mark-exclusion='false'", "boxplot-mark-exclusion='true'")
    spec = render(parse(tmp_path, dots(hidden)), db).spec
    main = next(item for item in spec["layer"] if not str(item.get("name", "")).startswith("__ref"))
    assert main["encoding"]["opacity"]["condition"]["test"] == 'datum["__ref0_out"]'
    # 最小、最大值的鬚
    minmax = box.replace("'standard'", "'minmax'")
    spec = render(parse(tmp_path, dots(minmax)), db).spec
    west = next(v for v in spec["data"]["values"] if v["c0_label"] == "West")
    assert (west["__ref0_low"], west["__ref0_high"]) == (300.0, 500.0)


def test_line_formats_and_labels(tmp_path: Path, db: Path) -> None:
    """線的顏色、粗細與點線（點的長度與線寬相同）；標籤的數字格式。"""
    style = (
        "<style><style-rule element='refline'>"
        "<format attr='stroke-color' id='refline0' value='#898989' />"
        "<format attr='line-pattern-only' id='refline0' value='dotted' />"
        "<format attr='text-format' id='refline0' value='n#,##0.0' />"
        "</style-rule></style>"
    )
    lines = refline(0, "average", label="value")
    spec = render(parse(tmp_path, dots(lines, style)), db).spec
    named = layers(spec)
    assert named["__ref0_0"]["mark"] == {
        "type": "rule",
        "color": "#898989",
        "strokeWidth": 2,
        "strokeDash": [2, 2],
    }
    mean = statistics.fmean(ORDER_SALES)
    assert named["__ref0_0_label"]["encoding"]["text"]["value"] == f"{mean:,.1f}"


def test_reference_lines_on_each_measure(tmp_path: Path, db: Path) -> None:
    """同一架上有兩個量值（各一張圖）：參考線畫在自己的量值的那張圖上，與它共用刻度。"""
    profit = instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative")
    sheet = worksheet(
        "Two",
        [REGION, SALES, profit],
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Region:nk"),
        pane_extra=refline(0, "average", axis="sum:Profit:qk")
        + refline(1, "constant", extra=" value='100'"),
    )
    rendered = render(parse(tmp_path, sheet), db)
    assert not [n for n in rendered.notes if "參考線" in n]
    sales_chart, profit_chart = rendered.spec["vconcat"]
    names = lambda chart: {  # noqa: E731
        layer.get("name") for layer in chart.get("layer", []) if layer.get("name")
    }
    assert "__ref0_0" in names(profit_chart) and "__ref0_0" not in names(sales_chart)
    assert "__ref1_0" in names(sales_chart)
    profit_key = next(
        k for k, v in rendered.spec["usermeta"]["measures"].items() if v == "sum(f_profit)"
    )
    numbers = [r[profit_key] for r in rendered.spec["data"]["values"]]
    assert first(rendered.spec)["__ref0_0"] == pytest.approx(sum(numbers) / len(numbers))
