"""標記標籤：顯示與否、沒有欄位時的預設標籤、最小值／最大值、線條兩端、位置與重疊，以及折線的分段。

設定的寫法取自公開報表（Sales Performance 的 Sales Trends、HR Dashboards、Spotify Insights、TabMon），
位置與畫法依 Tableau 的畫面（HR Dashboards 的預覽圖）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tmig.ir import MarkLabels
from tmig.render.vegalite import main_encoding

from ..parser.builder import instance, ref, worksheet
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, label, parse, render

__all__ = ["db"]

MONTH = instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")
YEAR = instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal")
MONTH_PART = instance("[Order Date]", "Month", "[mn:Order Date:ok]", "ordinal")
QUANTITY = instance("[Quantity]", "Sum", "[sum:Quantity:qk]", "quantitative")


def labels(**formats: str) -> str:
    """標記卡的 <style>：mark-labels-* 的設定（底線換成連字號）。"""
    items = "".join(
        f"<format attr='mark-labels-{name.replace('_', '-')}' value='{value}' />"
        for name, value in formats.items()
    )
    return f"<style><style-rule element='mark'>{items}</style-rule></style>"


def trend(pane_extra: str, encodings: str = "", instances: list[str] | None = None) -> str:
    return worksheet(
        "Trend",
        instances or [MONTH, SALES, REGION],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=encodings,
        pane_extra=pane_extra,
    )


def label_layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        layer for layer in spec.get("layer", []) if str(layer.get("name", "")).startswith("__label")
    ]


def test_label_settings_are_parsed(tmp_path: Path) -> None:
    sheet = trend(
        labels(show="true", mode="range", range_min="false", range_scope="multi-mark", cull="false")
    )
    marks = parse(tmp_path, sheet).worksheets[0].marks
    assert marks.labels == MarkLabels(
        show=True, marks="min_max", scope="line", minimum=False, overlap=True
    )
    sheet = trend(labels(show="true", mode="line-ends", line_last="false"))
    assert parse(tmp_path, sheet).worksheets[0].marks.labels == MarkLabels(
        show=True, marks="line_ends", last=False
    )
    # 預設的設定（顯示、全部、不允許重疊）只留 show
    sheet = trend(labels(show="true", mode="all", cull="true"))
    assert parse(tmp_path, sheet).worksheets[0].marks.labels == MarkLabels(show=True)
    assert parse(tmp_path, trend("")).worksheets[0].marks.labels is None


def test_hidden_labels_and_default_label(tmp_path: Path, db: Path) -> None:
    def bars(pane_extra: str, encodings: str = "") -> dict[str, Any]:
        sheet = worksheet(
            "Bars",
            [REGION, SALES],
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            encodings=encodings,
            pane_extra=pane_extra,
        )
        return render(parse(tmp_path, sheet), db).spec

    text = f"<text column='{ref('sum:Sales:qk')}' />"
    # 標籤上有欄位、關閉了標記標籤：不畫
    assert "layer" not in bars(labels(show="false"), text)
    # 開啟了標記標籤、標籤上沒有欄位：與 Tableau 相同標示座標軸上的量值
    spec = bars(labels(show="true"))
    (label,) = label_layers(spec)
    sales = next(k for k, v in spec["usermeta"]["measures"].items() if v == "sum(f_sales)")
    assert label["encoding"]["text"]["field"] == f"{sales}_label"
    # 沒有設定：標籤上有欄位才畫
    assert "layer" not in bars("")
    assert len(label_layers(bars("", text))) == 1


def test_min_max_labels(tmp_path: Path, db: Path) -> None:
    spec = render(parse(tmp_path, trend(labels(show="true", mode="range"))), db).spec
    high, low = label_layers(spec)
    # 最大值寫在點的右上、最小值在右下
    assert high["mark"]["baseline"] == "bottom" and low["mark"]["baseline"] == "top"
    assert high["mark"]["align"] == low["mark"]["align"] == "left"
    flag = high["transform"][0]["filter"].split("]")[0].removeprefix("datum[")
    flag = flag.strip('"')
    values = spec["data"]["values"]
    sales = next(k for k, v in spec["usermeta"]["measures"].items() if v == "sum(f_sales)")
    flagged = {r[flag]: r[sales] for r in values if r.get(flag)}
    numbers = [r[sales] for r in values]
    assert flagged == {"max": max(numbers), "min": min(numbers)}
    # 每條線（multi-mark）各自的最大值，不標最小值
    sheet = trend(
        labels(show="true", mode="range", range_scope="multi-mark", range_min="false"),
        f"<color column='{ref('none:Region:nk')}' />",
    )
    spec = render(parse(tmp_path, sheet), db).spec
    (layer,) = label_layers(spec)
    flag = layer["transform"][0]["filter"].removeprefix("datum[").split("]")[0].strip('"')
    region = next(k for k, v in spec["usermeta"]["dimensions"].items() if v == "f_region")
    marked = [r for r in spec["data"]["values"] if r.get(flag)]
    assert sorted(r[region] for r in marked) == ["East", "North", "South", "West"]
    assert {r[flag] for r in marked} == {"max"}


def test_line_end_labels(tmp_path: Path, db: Path) -> None:
    sheet = trend(
        labels(show="true", mode="line-ends", line_last="false"),
        f"<color column='{ref('none:Region:nk')}' />",
    )
    spec = render(parse(tmp_path, sheet), db).spec
    (layer,) = label_layers(spec)
    flag = layer["transform"][0]["filter"].removeprefix("datum[").split("]")[0].strip('"')
    region = next(k for k, v in spec["usermeta"]["dimensions"].items() if v == "f_region")
    month = next(k for k, v in spec["usermeta"]["dimensions"].items() if "f_order_date" in v)
    firsts = {r[region]: r[month] for r in spec["data"]["values"] if r.get(flag)}
    earliest: dict[str, Any] = {}
    for record in spec["data"]["values"]:
        current = earliest.get(record[region])
        if current is None or record[month] < current:
            earliest[record[region]] = record[month]
    assert firsts == earliest


def test_label_size_and_position_on_points(tmp_path: Path, db: Path) -> None:
    def scatter(pane_extra: str = "") -> dict[str, Any]:
        sheet = worksheet(
            "Scatter",
            [REGION, SALES, PROFIT, QUANTITY],
            rows=ref("sum:Profit:qk"),
            cols=ref("sum:Sales:qk"),
            mark="Circle",
            encodings=(
                f"<size column='{ref('sum:Quantity:qk')}' />"
                f"<text column='{ref('none:Region:nk')}' /><lod column='{ref('none:Region:nk')}' />"
            ),
            pane_extra=pane_extra,
        )
        return render(parse(tmp_path, sheet), db).spec

    spec = scatter()
    (layer,) = label_layers(spec)
    # 大小上的欄位不改變標籤的字級；標籤在圓點的右邊（與 Tableau 相同）
    assert layer["encoding"]["size"] == {"value": layer["mark"]["fontSize"]}
    assert layer["mark"]["align"] == "left" and layer["mark"]["baseline"] == "middle"
    centered = scatter(
        "<style><style-rule element='cell'><format attr='text-align' value='center' />"
        "</style-rule></style>"
    )
    (layer,) = label_layers(centered)
    assert layer["mark"]["align"] == "center" and "dx" not in layer["mark"]


def test_bar_labels_with_measure_color_are_not_stacked(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "Bars",
        [CATEGORY, SALES, PROFIT],
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=(
            f"<color column='{ref('sum:Profit:qk')}' /><text column='{ref('sum:Sales:qk')}' />"
        ),
    )
    spec = render(parse(tmp_path, sheet), db).spec
    (layer,) = label_layers(spec)
    # 依量值的連續色彩不分段：標籤在長條的尾端外側、不是白色
    assert layer["mark"]["align"] == "left" and "x" not in layer["encoding"]
    assert layer["encoding"]["color"]["value"] != "#ffffff"


@pytest.mark.parametrize("path", [True, False])
def test_lines_break_between_panes(tmp_path: Path, db: Path, path: bool) -> None:
    """折線不跨窗格（與 Tableau 相同）：有路徑時列上的維度都分開各條線（啞鈴圖）；沒有路徑時沿著最內層的
    維度連接、外層的維度分開（離散的年、月）。"""
    if path:
        sheet = worksheet(
            "Barbell",
            [REGION, CATEGORY, SALES],
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            mark="Line",
            encodings=f"<path column='{ref('none:Category:nk')}' />",
        )
        expected = ["f_region"]
    else:
        sheet = worksheet(
            "Months",
            [YEAR, MONTH_PART, SALES],
            rows=ref("sum:Sales:qk"),
            cols=f"({ref('yr:Order Date:ok')} / {ref('mn:Order Date:ok')})",
            mark="Line",
        )
        expected = ["year(f_order_date)"]
    spec = render(parse(tmp_path, sheet), db).spec
    dimensions = spec["usermeta"]["dimensions"]
    encoding = main_encoding(spec)
    assert encoding is not None
    detail = [d["field"] for d in encoding.get("detail", [])]
    assert [dimensions[f] for f in detail if f in dimensions] == expected


def test_kpi_lines_with_their_own_styles(tmp_path: Path, db: Path) -> None:
    """自訂標籤文字中各行的格式不同（KPI 的小標題與大數字）：每一行一個文字圖層，依字級由上而下排列。"""
    sheet = worksheet(
        "KPI",
        [SALES],
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=label(
            "<run bold='true' fontcolor='#333333' fontsize='8'>Total Sales</run><run>&#198;&#10;</run>"
            f"<run fontsize='20'>&lt;{ref('sum:Sales:qk')}&gt;</run>"
        ),
    )
    spec = render(parse(tmp_path, sheet), db).spec
    title, value = spec["layer"]
    assert title["mark"]["fontSize"] < value["mark"]["fontSize"]
    assert title["mark"]["fontWeight"] == 700 and title["mark"]["color"] == "#333333"
    assert title["mark"]["dy"] < 0 < value["mark"]["dy"]
    record = spec["data"]["values"][0]
    assert record[title["encoding"]["text"]["field"]] == "Total Sales"
    assert record[value["encoding"]["text"]["field"]] == "1,310"


def test_line_colored_by_measure_is_one_line(tmp_path: Path, db: Path) -> None:
    """依量值（連續色彩）著色的折線：Vega-Lite 會依顏色的值分開各條線，改以色階中間的顏色畫成一條線。"""
    sheet = trend("", f"<color column='{ref('sum:Sales:qk')}' />")
    rendered = render(parse(tmp_path, sheet), db)
    assert "color" not in rendered.spec["encoding"]
    assert rendered.spec["mark"]["color"].startswith("#")
    assert any("依量值著色的折線" in note for note in rendered.notes)


def test_kpi_text_has_no_padding(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "KPI", [SALES], mark="Text", encodings=f"<text column='{ref('sum:Sales:qk')}' />"
    )
    assert render(parse(tmp_path, sheet), db).spec["padding"] == 0


def test_title_background(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "KPI", [SALES], mark="Text", encodings=f"<text column='{ref('sum:Sales:qk')}' />"
    )
    sheet = sheet.replace(
        "<style />",
        "<style><style-rule element='title'><format attr='background-color' value='#094ba2' />"
        "</style-rule></style>",
        1,
    )
    ir = parse(tmp_path, sheet)
    assert ir.worksheets[0].title_background == "#094ba2"
    assert render(ir, db).spec["usermeta"]["title_background"] == "#094ba2"
