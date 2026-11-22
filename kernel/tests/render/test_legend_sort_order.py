"""圖例的順序依欄位的排序：依欄位的值排序的色彩圖例，與形狀圖例的各種排序。

Tableau 的圖例列出成員的順序與欄位的排序相同，在圖例上排序也改變標記的順序
（https://help.tableau.com/v2021.2/pro/desktop/en-us/sortgroup_sorting_computed_howto.htm ，2026-11-23 查閱）；
各成員的顏色與形狀屬於成員，不隨排序改變。

測試資料見 tests/sqlgen/test_query.py：各區的 SUM(Sales) East 350、North 60、South 100、West 800；
各區、類別的 SUM(Sales) East／Furniture 150、East／Technology 200、North／Office 60、South／Furniture 80、
South／Office 20、West／Furniture 300、West／Technology 500。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tmig.ir import IR
from tmig.ir.models import Scalar, Sort
from tmig.render.vegalite import main_encoding

from ..parser.builder import instance, ref, worksheet
from .test_text import CATEGORY, REGION, SALES, db, parse, render

__all__ = ["db"]  # pytest fixture

QUANTITY = instance("[Quantity]", "Sum", "[sum:Quantity:qk]", "quantitative")


def with_sorts(ir: IR, db: Path, *sorts: Sort) -> dict[str, Any]:
    sheet = ir.worksheets[0].model_copy(update={"sorts": list(sorts)})
    return render(ir.model_copy(update={"worksheets": [sheet]}), db).spec


def stacked_bars(tmp_path: Path) -> IR:
    """各類別的長條依地區堆疊（色彩是地區）。"""
    sheet = worksheet(
        "Stacked",
        [REGION, CATEGORY, SALES],
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<color column='{ref('none:Region:nk')}' />",
    )
    return parse(tmp_path, sheet)


def shaped_points(tmp_path: Path, color: bool = False) -> IR:
    """各地區的 SUM(Sales) 與 SUM(Quantity) 的散布圖，形狀是地區（color：色彩也是地區）。"""
    encodings = f"<shape column='{ref('none:Region:nk')}' />"
    if color:
        encodings += f"<color column='{ref('none:Region:nk')}' />"
    sheet = worksheet(
        "Shapes",
        [REGION, SALES, QUANTITY],
        rows=ref("sum:Quantity:qk"),
        cols=ref("sum:Sales:qk"),
        mark="Shape",
        encodings=encodings,
    )
    return parse(tmp_path, sheet)


def scale_pairs(spec: dict[str, Any], channel: str) -> list[tuple[Any, Any]]:
    encoding = main_encoding(spec)
    assert encoding is not None
    scale = encoding[channel]["scale"]
    return list(zip(scale["domain"], scale["range"], strict=True))


def test_color_legend_follows_sort_by_field(tmp_path: Path, db: Path) -> None:
    """依欄位的值排序（SUM(Sales) 遞減、遞增）：圖例依各區的銷售額排列，堆疊依它，顏色不變。"""
    ir = stacked_bars(tmp_path)
    ws = ir.worksheets[0]
    region = ws.marks.color
    assert region is not None
    member = with_sorts(ir, db)
    colors = dict(scale_pairs(member, "color"))
    assert list(colors) == ["East", "North", "South", "West"]

    by = ws.columns[0]
    descending = with_sorts(ir, db, Sort(field=region.field, kind="by_field", order="desc", by=by))
    assert [label for label, _ in scale_pairs(descending, "color")] == [
        "West",
        "East",
        "South",
        "North",
    ]
    assert all(colors[label] == color for label, color in scale_pairs(descending, "color"))
    # 堆疊依圖例的順序（與手動排序相同的作法）
    assert descending["encoding"]["order"] == {"field": "__legend_order", "type": "quantitative"}
    field = descending["encoding"]["color"]["field"]
    stacked = {
        r[field]: r["__legend_order"] for r in descending["data"]["values"] if "__legend_order" in r
    }
    assert stacked == {"West": 0, "East": 1, "South": 2, "North": 3}

    ascending = with_sorts(ir, db, Sort(field=region.field, kind="by_field", order="asc", by=by))
    assert [label for label, _ in scale_pairs(ascending, "color")] == [
        "North",
        "South",
        "East",
        "West",
    ]
    assert all(colors[label] == color for label, color in scale_pairs(ascending, "color"))


def test_nested_sort_without_one_order_keeps_member_order(tmp_path: Path, db: Path) -> None:
    """巢狀排序、各窗格的順序不同時（同一個地區在各類別中的排序值不同）沒有單一的順序：圖例依成員排列。
    色彩欄位不在欄列架上時，巢狀排序沒有外層，與一般的依欄位排序相同。"""
    sheet = worksheet(
        "Nested",
        [REGION, CATEGORY, SALES],
        rows=f"({ref('none:Category:nk')} / {ref('none:Region:nk')})",
        cols=ref("sum:Sales:qk"),
        encodings=f"<color column='{ref('none:Region:nk')}' />",
    )
    ir = parse(tmp_path, sheet)
    ws = ir.worksheets[0]
    region, sales = ws.rows[1].field, ws.columns[0]
    nested = with_sorts(
        ir, db, Sort(field=region, kind="by_field", order="desc", by=sales, nested=True)
    )
    assert [label for label, _ in scale_pairs(nested, "color")] == [
        "East",
        "North",
        "South",
        "West",
    ]
    flat = with_sorts(ir, db, Sort(field=region, kind="by_field", order="desc", by=sales))
    assert [label for label, _ in scale_pairs(flat, "color")] == ["West", "East", "South", "North"]

    outer = stacked_bars(tmp_path)
    color = outer.worksheets[0].marks.color
    assert color is not None
    by = outer.worksheets[0].columns[0]
    alone = with_sorts(
        outer, db, Sort(field=color.field, kind="by_field", order="desc", by=by, nested=True)
    )
    assert [label for label, _ in scale_pairs(alone, "color")] == [
        "West",
        "East",
        "South",
        "North",
    ]


def test_shape_legend_follows_sorts(tmp_path: Path, db: Path) -> None:
    """形狀的圖例（Vega 的比例尺與檢視器自己畫的 usermeta.shape_legend）依欄位的排序排列：手動、遞減的
    字母順序與依欄位的值；各成員的形狀不變。"""
    ir = shaped_points(tmp_path)
    ws = ir.worksheets[0]
    shape = ws.marks.shape
    assert shape is not None
    member = with_sorts(ir, db)
    shapes = dict(scale_pairs(member, "shape"))
    assert list(shapes) == ["East", "North", "South", "West"]

    def check(spec: dict[str, Any], expected: list[str]) -> None:
        pairs = scale_pairs(spec, "shape")
        assert [label for label, _ in pairs] == expected
        assert all(shapes[label] == path for label, path in pairs)
        entries = spec["usermeta"]["shape_legend"]["entries"]
        assert [e["label"] for e in entries] == expected
        legend = {e["label"]: e["path"] for e in member["usermeta"]["shape_legend"]["entries"]}
        assert all(legend[e["label"]] == e["path"] for e in entries)

    check(member, ["East", "North", "South", "West"])
    manual = ["South", "West", "North", "East"]
    sort = Sort(field=shape.field, kind="manual", manual_values=list[Scalar](manual))
    check(with_sorts(ir, db, sort), manual)
    check(
        with_sorts(ir, db, Sort(field=shape.field, kind="alphabetic", order="desc")),
        ["West", "South", "North", "East"],
    )
    by = ws.columns[0]
    check(
        with_sorts(ir, db, Sort(field=shape.field, kind="by_field", order="desc", by=by)),
        ["West", "East", "South", "North"],
    )


def test_shared_color_and_shape_legend_keep_one_order(tmp_path: Path, db: Path) -> None:
    """色彩與形狀是同一個欄位（合成一個圖例）：兩個比例尺的順序相同，合併的圖例帶著各成員原本的顏色。"""
    ir = shaped_points(tmp_path, color=True)
    ws = ir.worksheets[0]
    shape = ws.marks.shape
    assert shape is not None
    member = with_sorts(ir, db)
    colors = dict(scale_pairs(member, "color"))
    spec = with_sorts(
        ir, db, Sort(field=shape.field, kind="by_field", order="desc", by=ws.columns[0])
    )
    order = ["West", "East", "South", "North"]
    assert [label for label, _ in scale_pairs(spec, "color")] == order
    assert [label for label, _ in scale_pairs(spec, "shape")] == order
    entries = spec["usermeta"]["shape_legend"]["entries"]
    assert [(e["label"], e["color"]) for e in entries] == [
        (label, colors[label]) for label in order
    ]
