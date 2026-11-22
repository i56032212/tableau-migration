"""表頭的框線、合計與總計的框線（格式＞框線）：解析與呈現。

.twb 的寫法沒有樣本（推定）：表頭的框線與儲存格、窗格的相同（element='header' 的 border-style、border-width、
border-color）；合計、總計的框線是同樣的格式加上 data-class（subtotal 為合計、total 為總計，與網底的寫法相同）
與 scope（rows、cols）。測試資料見 tests/sqlgen/test_query.py；區與類別的組合依序為 East｜Furniture、
East｜Technology、North｜Office、South｜Furniture、South｜Office、West｜Furniture、West｜Technology。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tmig.ir import Border, TotalBorder
from tmig.render.vegalite import main_encoding

from .test_formats import fmt, nested, rule
from .test_subtotals import nested as subtotaled
from .test_text import db, parse, render, render_with_totals, with_totals

__all__ = ["db"]  # pytest fixture

HEADER_BORDER = rule(
    "header", fmt("border-style", "solid"), fmt("border-width", "2"), fmt("border-color", "#00AA00")
)
NO_DIVIDERS = rule(
    "table-div",
    fmt("line-visibility", "off", scope="rows"),
    fmt("line-visibility", "off", scope="cols"),
)
NO_HEADER_DIVIDERS = rule(
    "header-div",
    fmt("line-visibility", "off", scope="rows"),
    fmt("line-visibility", "off", scope="cols"),
)


def total(kind: str, attr: str, value: str, scope: str | None = "rows") -> str:
    where = f" scope='{scope}'" if scope else ""
    return f"<format attr='{attr}' data-class='{kind}'{where} value='{value}' />"


TOTAL_BORDERS = rule(
    "pane",
    total("total", "border-style", "solid"),
    total("total", "border-width", "3"),
    total("total", "border-color", "#FF0000"),
    total("subtotal", "border-style", "dashed"),
    total("subtotal", "border-color", "#0000FF"),
)


def layers(spec: dict[str, Any], prefix: str = "__deco__divider_rows") -> dict[str, dict[str, Any]]:
    return {
        layer["name"]: layer
        for layer in spec.get("layer", [])
        if str(layer.get("name", "")).startswith(prefix)
    }


def edge_test(edges: list[str]) -> str:
    return f"indexof({json.dumps(edges, ensure_ascii=False)}, datum.value) >= 0"


def test_header_and_total_borders_are_parsed(tmp_path: Path) -> None:
    """表頭的框線（原本只記在 ignored、沒有畫）；合計與總計的框線依 scope 分列與欄，沒有寫 scope 時兩者都套用，
    窗格、儲存格、表頭的設定擇一（依這個順序）；樣式是 none 時沒有框線。"""
    ir = parse(tmp_path, nested("rows", HEADER_BORDER, TOTAL_BORDERS))
    assert ir.unsupported == []
    assert not any("框線" in u.reason for u in ir.ignored)
    ws = ir.worksheets[0]
    assert ws.header_border == Border(width=2, color="#00aa00")
    assert ws.total_borders == [
        TotalBorder(shelf="rows", kind="grand_total", border=Border(width=3, color="#ff0000")),
        TotalBorder(
            shelf="rows", kind="subtotal", border=Border(pattern="dashed", color="#0000ff")
        ),
    ]
    both = rule("cell", total("total", "border-style", "dotted", scope=None))
    off = rule("header", total("subtotal", "border-style", "none", scope="cols"))
    ws = parse(tmp_path, nested("rows", both, off)).worksheets[0]
    assert ws.total_borders == [
        TotalBorder(shelf="columns", kind="grand_total", border=Border(pattern="dotted")),
        TotalBorder(shelf="rows", kind="grand_total", border=Border(pattern="dotted")),
    ]
    assert parse(tmp_path, nested("rows")).worksheets[0].header_border is None


def test_row_header_borders(tmp_path: Path, db: Path) -> None:
    """表頭的框線：列標題區域中每一列之間，從改變的那一層所在的欄畫到窗格（同一個 Region 中只切開 Category
    的欄）；標題的分隔線（預設在 Region 改變處）已經畫的位置不重複畫（分隔線優先）。"""
    spec = render(parse(tmp_path, nested("rows", HEADER_BORDER)), db).spec
    found = layers(spec)
    border = found["__deco__divider_rows_border"]
    assert border["mark"] == {
        "type": "rule",
        "color": "#00aa00",
        "strokeWidth": 2,
        "x": {"expr": "datum['__divider_rows_border']"},
        "x2": 0,
    }
    assert border["encoding"]["y"]["bandPosition"] == 1
    values = spec["data"]["values"]
    starts = {v["rows_dim"]: v["__divider_rows_border"] for v in values}
    divided = {v["rows_dim"] for v in values if v["__divider_rows"] < 0}
    assert divided == {"East｜Technology", "North｜Office", "South｜Office"}
    # Region 不變的列之間：只切開 Category 的欄；Region 改變處由分隔線畫；最後一列不畫
    inner = {"East｜Furniture", "South｜Furniture", "West｜Furniture"}
    assert {k for k, v in starts.items() if v < 0} == inner
    assert len({starts[k] for k in inner}) == 1
    assert all(starts[k] == 0 for k in divided | {"West｜Technology"})

    # 關掉標題的分隔線：每一列之間都是表頭的框線，Region 改變處從 Region 的欄畫起
    spec = render(parse(tmp_path, nested("rows", HEADER_BORDER, NO_HEADER_DIVIDERS)), db).spec
    values = spec["data"]["values"]
    starts = {v["rows_dim"]: v["__divider_rows_border"] for v in values}
    assert sum(1 for v in starts.values() if v < 0) == len(values) - 1
    assert starts["East｜Technology"] < starts["East｜Furniture"] < 0


def test_column_header_borders(tmp_path: Path, db: Path) -> None:
    """欄標題的表頭框線：以座標軸的刻度畫在每一欄之間；標題的分隔線（Region 改變處）優先。"""
    spec = render(parse(tmp_path, nested("cols", HEADER_BORDER, mark="Text")), db).spec
    axis = main_encoding(spec)["x"]["axis"]  # type: ignore[index]
    divided = ["East｜Technology", "North｜Office", "South｜Office"]
    assert axis["tickColor"] == {
        "condition": {"test": edge_test(divided), "value": "#d4d4d4"},
        "value": "#00aa00",
    }
    assert axis["tickWidth"] == {"condition": {"test": edge_test(divided), "value": 1}, "value": 2}
    assert axis["tickBand"] == "extent" and axis["ticks"] is True
    assert axis["tickOpacity"]["condition"]["test"].count("｜") == 6  # 每一欄之間（最後一欄以外）


def test_total_borders_are_rendered(tmp_path: Path, db: Path) -> None:
    """合計與總計的框線：畫在合計（Region 的小計）與總計的列的兩側（前一列的尾端與它自己的尾端），窗格的格線
    與標題區域的線都畫；分隔線關掉時看得到，總計優先於合計。"""
    sheet = with_totals(subtotaled("none:Region:nk"), "rows").replace(
        "<style />", "<style>" + TOTAL_BORDERS + NO_DIVIDERS + NO_HEADER_DIVIDERS + "</style>", 1
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    spec = render_with_totals(ir, db).spec
    values = spec["data"]["values"]
    order = list(dict.fromkeys(v["rows_dim"] for v in values))
    subtotals = [v for v in order if v.endswith("｜Total")]
    assert len(subtotals) == 4 and order[-2] == "West｜Total"
    axis = main_encoding(spec)["y"]["axis"]  # type: ignore[index]
    grand = [order[-2]]  # 總計的列在最後：只畫它上面的線
    # 合計的列：上面的線（前一列的尾端）與下面的線（它自己的尾端）
    sub = [order[order.index(s) - 1] for s in subtotals] + subtotals
    assert axis["gridColor"] == {
        "condition": {"test": edge_test(grand), "value": "#ff0000"},
        "value": "#0000ff",
    }
    assert axis["gridWidth"] == {"condition": {"test": edge_test(grand), "value": 3}, "value": 1}
    assert axis["gridDash"] == {
        "condition": {"test": edge_test(grand), "value": []},
        "value": [4, 2],
    }
    drawn = axis["gridOpacity"]["condition"]["test"]
    assert all(json.dumps(e, ensure_ascii=False) in drawn for e in [*grand, *sub])
    # 標題區域：總計與合計各一個圖層（同一個位置只畫總計的）
    found = layers(spec)
    assert found["__deco__divider_rows_total0"]["mark"]["color"] == "#ff0000"
    assert found["__deco__divider_rows_total1"]["mark"]["strokeDash"] == [4, 2]
    first = {v["rows_dim"]: v["__divider_rows_total0"] for v in values}
    second = {v["rows_dim"]: v["__divider_rows_total1"] for v in values}
    assert first["West｜Total"] < 0 and second["West｜Total"] == 0
    assert second["East｜Technology"] < 0 and second["East｜Total"] < 0
