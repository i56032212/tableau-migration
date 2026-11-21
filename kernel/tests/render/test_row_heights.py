"""文字表的列高跟著多行的文字變高：標籤上有多個欄位（每個欄位一行）、自訂標籤文字中的換行、值本身有換行。

列高寫在 usermeta.sizing.rows.step（檢視器依它設定圖的高度）：預設的字（9pt，12 像素）每行 14 像素，加上與
單行時相同的留白（半個字級，6 像素）——兩行 34 像素、三行 48 像素；只有一行時照舊是 20 像素。整段文字以
列的中線為中心。Tableau 的列高是推定的（文件沒有寫，也還沒有與 Tableau 的畫面對過）。測試資料見
tests/sqlgen/test_query.py：四個區（East、West、South、North）。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from tmig.render.vegalite import ROW_HEIGHT, apply_fit

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_formats import fmt, rule, styled
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, label, parse, render

__all__ = ["db"]  # pytest fixture

SALES_TEXT = f"<text column='{ref('sum:Sales:qk')}' />"
PROFIT_TEXT = f"<text column='{ref('sum:Profit:qk')}' />"
SALES_VALUE = f"&lt;{ref('sum:Sales:qk')}&gt;"
PROFIT_VALUE = f"&lt;{ref('sum:Profit:qk')}&gt;"


def table(encodings: str = SALES_TEXT + PROFIT_TEXT, text: str = "", **options: str) -> str:
    """列架上是 Region 的文字表：標籤上的欄位（encodings）與自訂的標籤文字（text）。"""
    settings: dict[str, Any] = {"rows": ref("none:Region:nk"), "mark": "Text", **options}
    return worksheet(
        "Table", [REGION, CATEGORY, SALES, PROFIT], encodings=encodings, pane_extra=text, **settings
    )


def rows(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["usermeta"]["sizing"]["rows"]


def test_fields_on_separate_lines_make_rows_taller(tmp_path: Path, db: Path) -> None:
    """標籤上的兩個欄位各占一行：列高 34 像素（兩行各 14 像素加上 6 像素的留白），整段文字往上移半行、
    以列的中線為中心；加上欄的維度也一樣。只有一個欄位時與修正前相同（20 像素、文字不移動）。"""
    spec = render(parse(tmp_path, table()), db).spec
    assert {v["__text0"] for v in spec["data"]["values"]} >= {"350\n45", "60\n6"}
    assert rows(spec) == {"step": 34}
    assert spec["height"] == {"step": 34}
    assert (spec["mark"]["lineBreak"], spec["mark"]["fontSize"]) == ("\n", 12.0)
    assert spec["mark"]["dy"] == -7.0

    crossed = render(parse(tmp_path, table(cols=ref("none:Category:nk"))), db).spec
    assert rows(crossed) == {"step": 34} and crossed["mark"]["dy"] == -7.0
    columns = crossed["usermeta"]["sizing"]["columns"]
    assert (columns["count"], columns["fixed"]) == (3, True)  # 欄寬照舊依內容、固定

    single = render(parse(tmp_path, table(SALES_TEXT)), db).spec
    assert rows(single) == {"step": ROW_HEIGHT} and single["height"] == {"step": 20}
    assert "dy" not in single["mark"]


def test_taller_rows_follow_the_fit_mode(tmp_path: Path, db: Path) -> None:
    """顯示大小：標準與符合寬度時每一列固定加高後的列高；符合高度與整個檢視時列照舊填滿區塊（不固定），
    加高的列高仍寫在 sizing 中，dashboard 的區塊改成標準時檢視器用得到。"""
    ir = parse(tmp_path, table())
    spec = render(ir, db).spec
    for fit, height in (
        ("fit_width", {"step": 34}),
        ("fit_height", "container"),
        ("entire_view", "container"),
        ("standard", {"step": 34}),
    ):
        fitted = copy.deepcopy(spec)
        apply_fit(fitted, fit)
        assert fitted["height"] == height, fit

    filled = ir.model_copy(
        update={"worksheets": [ir.worksheets[0].model_copy(update={"fit": "entire_view"})]}
    )
    spec = render(filled, db).spec
    assert (spec["width"], spec["height"]) == ("container", "container")
    assert spec["autosize"]["type"] == "fit"
    assert rows(spec) == {"step": 34} and spec["mark"]["dy"] == -7.0


def test_custom_label_text_lines_set_the_row_height(tmp_path: Path, db: Path) -> None:
    """自訂的標籤文字：行數依文字中的換行（不是標籤上欄位的數目）。三行同一種格式是一個文字標記，列高
    48 像素、往上移一行；各行格式不同時每一行一個圖層（本來就整段置中），列高依各行的字級。"""
    three = label(f"<run>Sales&#10;{SALES_VALUE}&#10;{PROFIT_VALUE}</run>")
    spec = render(parse(tmp_path, table(text=three)), db).spec
    assert spec["data"]["values"][0]["__text0"] == "Sales\n350\n45"
    assert rows(spec) == {"step": 48} and spec["height"] == {"step": 48}
    assert spec["mark"]["dy"] == -14.0

    # 一行的自訂文字：兩個欄位寫在同一行，列高不變
    one = label(f"<run>{SALES_VALUE} / {PROFIT_VALUE}</run>")
    spec = render(parse(tmp_path, table(text=one)), db).spec
    assert spec["data"]["values"][0]["__text0"] == "350 / 45"
    assert rows(spec) == {"step": ROW_HEIGHT} and "dy" not in spec["mark"]

    # 8pt 的小標題與 20pt 的數字：行高各是字級的 1.2 倍（12.8、32 像素），留白是大字的一半（13.33 像素）
    mixed = label(
        f"<run fontsize='8'>Sales</run><run>&#10;</run><run fontsize='20'>{SALES_VALUE}</run>"
    )
    spec = render(parse(tmp_path, table(text=mixed)), db).spec
    title, value = spec["layer"]
    assert (title["mark"]["fontSize"], value["mark"]["fontSize"]) == (10.67, 26.67)
    assert rows(spec) == {"step": 59} and spec["height"] == {"step": 59}
    assert (title["mark"]["dy"], value["mark"]["dy"]) == (-16.0, 6.4)  # 位置與修正前相同

    # 沒有列的單格文字（KPI）：沒有列高可言，照舊填滿區塊
    kpi = worksheet("KPI", [SALES, PROFIT], mark="Text", encodings=SALES_TEXT, pane_extra=mixed)
    spec = render(parse(tmp_path, kpi), db).spec
    assert rows(spec) == {} and spec["height"] == "container"


def test_set_row_height_is_kept_when_the_text_fits(tmp_path: Path, db: Path) -> None:
    """設定的列高（Format > Cell Size）：放得下多行的文字時照設定；放不下時加大到放得下，並寫進附註
    （Tableau 怎麼畫沒有查證）。只有一行的文字照設定，不檢查放不放得下（與修正前相同）。"""
    roomy = styled(table(), rule("cell", fmt("height", "60")))
    rendered = render(parse(tmp_path, roomy), db)
    assert rows(rendered.spec) == {"step": 60} and rendered.spec["mark"]["dy"] == -7.0
    assert rendered.notes == []

    tight = styled(table(), rule("cell", fmt("height", "24")))
    rendered = render(parse(tmp_path, tight), db)
    assert rows(rendered.spec) == {"step": 34} and rendered.spec["height"] == {"step": 34}
    assert rendered.notes == [
        "設定的列高（24 像素）放不下多行的文字，列高加大到 34 像素（Tableau 的畫法未查證）"
    ]

    # 這個欄位成員的大小（優先於整張表的）也一樣
    member = styled(
        table(), rule("cell", fmt("height", "60"), fmt("height", "30", "none:Region:nk"))
    )
    assert rows(render(parse(tmp_path, member), db).spec) == {"step": 34}

    single = styled(table(SALES_TEXT), rule("cell", fmt("height", "12")))
    rendered = render(parse(tmp_path, single), db)
    assert rows(rendered.spec) == {"step": 12} and rendered.notes == []


def test_values_with_line_breaks(tmp_path: Path, db: Path) -> None:
    """值本身有換行（各列的行數不同）：列高依行數最多的那一格，各格依自己的行數置中。"""
    calc = b.calc(
        "Calculation_1",
        "Size",
        'IF SUM([Sales]) > 300 THEN "big" + CHAR(10) + "deal" ELSE "small" END',
        "string",
        "dimension",
    )
    size = instance("[Calculation_1]", "User", "[usr:Calculation_1:nk]")
    sheet = worksheet(
        "Sizes",
        [REGION, SALES, size],
        rows=ref("none:Region:nk"),
        mark="Text",
        encodings=f"<text column='{ref('usr:Calculation_1:nk')}' />",
    )
    spec = render(parse(tmp_path, sheet, calcs=calc), db).spec
    field = spec["encoding"]["text"]["field"]
    texts = {v["c0"]: v[field] for v in spec["data"]["values"]}
    assert texts == {"East": "big\ndeal", "West": "big\ndeal", "South": "small", "North": "small"}
    assert rows(spec) == {"step": 34}
    assert spec["mark"]["dy"] == {
        "expr": f"-(length(split(datum[\"{field}\"] || '', '\\n')) - 1) * 7.0"
    }


def test_highlight_table_rows_grow_too(tmp_path: Path, db: Path) -> None:
    """醒目提示表（方塊標記）每一格的文字與文字表相同：兩行時列高 34 像素、整段置中。"""
    sheet = table(
        f"<color column='{ref('sum:Sales:qk')}' />" + SALES_TEXT + PROFIT_TEXT,
        cols=ref("none:Category:nk"),
        mark="Square",
    )
    rendered = render(parse(tmp_path, sheet), db)
    assert rendered.mark == "square"
    rect, text = rendered.spec["layer"]
    assert rect["mark"]["type"] == "rect"
    assert (text["mark"]["type"], text["mark"]["dy"]) == ("text", -7.0)
    assert rows(rendered.spec) == {"step": 34} and rendered.spec["height"] == {"step": 34}
