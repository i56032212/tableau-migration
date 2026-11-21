"""文字表的列高跟著多行的文字變高：標籤上有多個欄位（每個欄位一行）、自訂標籤文字中的換行、值本身有換行。

列高寫在 usermeta.sizing.rows.step（檢視器依它設定圖的高度）：預設的字（9pt，12 像素）每行 14 像素，加上與
單行時相同的留白（半個字級，6 像素）——兩行 34 像素、三行 48 像素；只有一行時照舊是 20 像素。整段文字以
列的中線為中心。Tableau 的列高是推定的（文件沒有寫，也還沒有與 Tableau 的畫面對過）。測試資料見
tests/sqlgen/test_query.py：四個區（East、West、South、North）。

2026-11-22 補上的（修多行文字的列高時記下、還沒處理的）：只有一行但字很大的文字（20pt 的字在 20 像素的列中
上下列疊在一起）、各行格式不同的自訂文字的欄寬（固定 40 像素，大的數字蓋到列標題）、沒有列的檢視中同一種格式
的多行文字（整段沒有置中）、圓點與形狀的表格中多行的標籤（列高不變、沒有置中、在標記上方時蓋到標記）。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

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


# ---------------------------------------------------------------- 只有一行的大字


def sized(points: int, value: str = SALES_VALUE, **options: str) -> str:
    """列架上是 Region 的文字表：每一格一行、points pt 的字。"""
    return table(SALES_TEXT, label(f"<run fontsize='{points}'>{value}</run>"), **options)


def test_single_line_in_a_large_font_makes_rows_taller(tmp_path: Path, db: Path) -> None:
    """只有一行、字卻大到放不下預設列高的文字（2026-11-21 記下的：20pt 的字在 20 像素的列中，文字的外框
    36 像素，上下兩列重疊 16 像素）：列高加到與多行相同的算法——一行的高度（字級加 2 像素）加上半個字級的
    留白。20pt（26.67 像素）是 26.67 + 2 + 13.33 = 42 像素，文字照舊在列的中線上（不必移動）。

    放得下預設列高的字（一行占的高度——字級的 1.2 倍——不超過 20 像素，約 12pt 以下）照舊是 20 像素，
    預設的字與修正前完全相同。推定的：Tableau 的文件沒有寫，也還沒有與 Tableau 的畫面對過。"""
    rendered = render(parse(tmp_path, sized(20)), db)
    spec = rendered.spec
    assert spec["mark"]["fontSize"] == 26.67 and "dy" not in spec["mark"]
    assert rows(spec) == {"step": 42} and spec["height"] == {"step": 42}
    assert rendered.notes == []
    for points, step in ((9, 20), (10, 20), (12, 20), (13, 28), (14, 30), (16, 34), (24, 50)):
        assert rows(render(parse(tmp_path, sized(points)), db).spec) == {"step": step}, points
    # 沒有自訂文字、預設的字：與修正前相同
    plain = render(parse(tmp_path, table(SALES_TEXT)), db).spec
    assert rows(plain) == {"step": ROW_HEIGHT} and plain["height"] == {"step": 20}

    # 欄架上有維度、醒目提示表（方塊標記）也一樣；符合高度時列照舊填滿區塊，列高仍寫在 sizing 中
    crossed = render(parse(tmp_path, sized(20, cols=ref("none:Category:nk"))), db).spec
    assert rows(crossed) == {"step": 42}
    highlight = table(
        f"<color column='{ref('sum:Sales:qk')}' />" + SALES_TEXT,
        label(f"<run fontsize='20'>{SALES_VALUE}</run>"),
        cols=ref("none:Category:nk"),
        mark="Square",
    )
    assert rows(render(parse(tmp_path, highlight), db).spec) == {"step": 42}
    filled = copy.deepcopy(spec)
    apply_fit(filled, "fit_height")
    assert filled["height"] == "container" and rows(filled) == {"step": 42}

    # 同一行內格式不同的各段（24pt 的數字接著 16pt 的單位）：依這一行最大的字（32 像素的 1.2 倍加一半）
    mixed = label(f"<run fontsize='24'>{SALES_VALUE}</run><run fontsize='16'>CM</run>")
    spec = render(parse(tmp_path, table(SALES_TEXT, mixed)), db).spec
    assert [layer["mark"]["fontSize"] for layer in spec["layer"]] == [32.0, 21.33]
    assert rows(spec) == {"step": 55}


def test_set_row_height_is_kept_for_a_single_large_line(tmp_path: Path, db: Path) -> None:
    """設定了列高（Format > Cell Size）而只有一行文字時照設定，不論字多大——由作者決定列高，不加大也不寫
    附註（多行的文字放不下時才加大，見 test_set_row_height_is_kept_when_the_text_fits）。"""
    for height in ("30", "60"):
        sheet = styled(sized(20), rule("cell", fmt("height", height)))
        rendered = render(parse(tmp_path, sheet), db)
        assert rows(rendered.spec) == {"step": int(height)} and rendered.notes == []


# ---------------------------------------------------------------- 欄寬依各行的字級估計


def estimated(text: str, pixels: float) -> int:
    """欄寬的估計：每個字 0.55 個字高，加上左右共 16 像素的空白。"""
    return round(len(text) * 0.55 * pixels + 16)


def columns(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["usermeta"]["sizing"]["columns"]


def test_column_width_follows_each_line_and_its_font(tmp_path: Path, db: Path) -> None:
    """文字表的欄寬依每一格的文字估計：各行依自己的字級，取最寬的一行（2026-11-21 記下的：各行格式不同的
    自訂文字每一行是一個圖層，欄寬找不到文字可以估計，固定是最小的 40 像素，16pt 的數字往左蓋到列標題；
    一種格式的大字也一律當成 12 像素估計）。預設的字與修正前完全相同。"""
    # 8pt 的小標題與 16pt 的數字（各行格式不同，每一行一個圖層）：最寬的是 16pt（21.33 像素）的那一行
    lines = label(
        f"<run fontsize='8'>Sales</run><run>&#10;</run>"
        f"<run fontsize='16'>Total: {SALES_VALUE}</run>"
    )
    spec = render(parse(tmp_path, table(SALES_TEXT, lines)), db).spec
    assert [layer["mark"]["fontSize"] for layer in spec["layer"]] == [10.67, 21.33]
    assert spec["data"]["values"][0]["__text0_1"] == "Total: 350"
    wide = estimated("Total: 350", 21.33)
    assert wide == 133 and columns(spec) == {"size": wide}
    assert spec["width"] == wide

    # 欄架上有維度：每一欄都放得下最寬的那一行（比欄標題 Technology 寬）
    crossed = render(parse(tmp_path, table(SALES_TEXT, lines, cols=ref("none:Category:nk"))), db)
    assert columns(crossed.spec) == {"step": wide, "count": 3, "fixed": True}

    # 一種格式的大字（一個文字標記）：依它的字級，不是 12 像素
    spec = render(parse(tmp_path, sized(16, f"Total: {SALES_VALUE}")), db).spec
    assert spec["mark"]["fontSize"] == 21.33 and columns(spec) == {"size": wide}

    # 同一行內格式不同的各段並排：各段的寬度相加（24pt 的數字 32 像素、16pt 的單位 21.33 像素）
    mixed = label(f"<run fontsize='24'>{SALES_VALUE}</run><run fontsize='16'>CM</run>")
    spec = render(parse(tmp_path, table(SALES_TEXT, mixed)), db).spec
    assert columns(spec) == {"size": round(3 * 0.55 * 32.0 + 2 * 0.55 * 21.33 + 16)}
    assert columns(spec)["size"] == 92

    # 預設的字（12 像素）與修正前相同：三位數放得下最小的 40 像素；較長的文字依 12 像素估計
    assert columns(render(parse(tmp_path, table(SALES_TEXT)), db).spec) == {"size": 40}
    typed = label(f"<run>Total: {SALES_VALUE}</run>")
    spec = render(parse(tmp_path, table(SALES_TEXT, typed)), db).spec
    assert columns(spec) == {"size": estimated("Total: 350", 12.0)}


# ---------------------------------------------------------------- 沒有列的檢視：整段置中


def test_multi_line_text_is_centred_without_discrete_rows(tmp_path: Path, db: Path) -> None:
    """沒有離散的列的檢視（KPI、只有欄的文字表）中同一種格式的多行文字（2026-11-21 記下的，2026-11-22 在
    畫面上量過：三行的 KPI 整段的中線比檢視的中線低 14 像素）：Vega 把第一行畫在檢視的中線、其餘往下排。
    整段往上移半段，以檢視的中線為中心——與各行格式不同的文字（每一行一個圖層，本來就整段置中）一致。
    沒有列高可言：照舊填滿區塊。只有一行時不移動。"""

    def kpi(text: str = "") -> dict[str, Any]:
        """只有一格的文字：標籤上是 Sales、Profit 兩個欄位，text 是自訂的標籤文字。"""
        sheet = worksheet(
            "KPI", [SALES, PROFIT], mark="Text", encodings=SALES_TEXT + PROFIT_TEXT, pane_extra=text
        )
        return render(parse(tmp_path, sheet), db).spec

    spec = kpi()
    assert spec["data"]["values"][0]["__text0"] == "1,310\n81"
    assert spec["mark"]["dy"] == -7.0  # 兩行：往上半行（行高 14 像素）
    assert rows(spec) == {} and spec["height"] == "container"

    spec = kpi(label(f"<run>Sales&#10;{SALES_VALUE}&#10;{PROFIT_VALUE}</run>"))
    assert spec["data"]["values"][0]["__text0"] == "Sales\n1,310\n81"
    assert spec["mark"]["dy"] == -14.0

    # 20pt 的兩行：行高是字級加 2 像素（28.67），往上半行
    spec = kpi(label(f"<run fontsize='20'>{SALES_VALUE}&#10;{PROFIT_VALUE}</run>"))
    assert spec["mark"]["fontSize"] == 26.67
    assert spec["mark"]["dy"] == pytest.approx(-14.335, abs=0.006)

    # 只有欄的文字表：每一格兩行，整段以檢視的中線為中心
    across = worksheet(
        "Across",
        [CATEGORY, SALES, PROFIT],
        cols=ref("none:Category:nk"),
        mark="Text",
        encodings=SALES_TEXT + PROFIT_TEXT,
    )
    spec = render(parse(tmp_path, across), db).spec
    assert spec["mark"]["dy"] == -7.0 and rows(spec) == {}
    assert columns(spec)["count"] == 3

    # 只有一行：不移動（與修正前相同）
    single = worksheet("KPI", [SALES], mark="Text", encodings=SALES_TEXT)
    assert "dy" not in render(parse(tmp_path, single), db).spec["mark"]


# ---------------------------------------------------------------- 圓點與形狀的表格


def dots(
    encodings: str = SALES_TEXT + PROFIT_TEXT, valign: str | None = None, **options: str
) -> str:
    """列架上是 Region、每一列一個圓點（或形狀）的表格；valign：標籤在標記的上方（top）或下方（bottom）。"""
    style = (
        "<style><style-rule element='cell'>"
        f"<format attr='vertical-align' value='{valign}' /></style-rule></style>"
        if valign
        else ""
    )
    return table(encodings, style, **{"mark": "Circle", **options})


def label_mark(spec: dict[str, Any]) -> dict[str, Any]:
    """標記標籤的文字標記。"""
    return next(layer["mark"] for layer in spec["layer"] if layer["mark"]["type"] == "text")


def test_dot_table_rows_follow_multi_line_labels(tmp_path: Path, db: Path) -> None:
    """圓點與形狀的表格（每一列一個標記）中多個欄位的標籤（2026-11-21 記下的，2026-11-22 在畫面上量過：
    列高照舊 20 像素，三行的標籤疊在一起、有一半被隱藏，整段的中線比圓點低 14 像素）：標籤的行數依資料
    （標籤上的每個欄位一行），列高與文字表相同跟著變高，在標記右邊的標籤整段以標記的中線為中心。
    只有一個欄位時與修正前相同。"""
    rendered = render(parse(tmp_path, dots()), db)
    spec = rendered.spec
    assert rendered.mark == "circle"
    assert {v["__text0"] for v in spec["data"]["values"]} >= {"350\n45", "60\n6"}
    mark = label_mark(spec)
    assert (mark["align"], mark["baseline"], mark["dx"]) == ("left", "middle", 9.0)
    assert mark["dy"] == -7.0  # 兩行：整段往上半行
    assert rows(spec) == {"step": 34} and spec["height"] == {"step": 34}

    single = render(parse(tmp_path, dots(SALES_TEXT)), db).spec
    assert rows(single) == {"step": ROW_HEIGHT} and "dy" not in label_mark(single)

    # 形狀標記、欄架上有維度也一樣
    shapes = render(parse(tmp_path, dots(mark="Shape", cols=ref("none:Category:nk"))), db)
    assert shapes.mark == "shape" and rows(shapes.spec) == {"step": 34}
    assert label_mark(shapes.spec)["dy"] == -7.0

    # 設定的列高放不下時加大並寫進附註（與文字表相同）；放得下時照設定
    tight = render(parse(tmp_path, styled(dots(), rule("cell", fmt("height", "24")))), db)
    assert rows(tight.spec) == {"step": 34}
    assert tight.notes == [
        "設定的列高（24 像素）放不下多行的文字，列高加大到 34 像素（Tableau 的畫法未查證）"
    ]
    roomy = render(parse(tmp_path, styled(dots(), rule("cell", fmt("height", "60")))), db)
    assert rows(roomy.spec) == {"step": 60} and roomy.notes == []


def test_dot_table_labels_above_and_below_stay_in_their_row(tmp_path: Path, db: Path) -> None:
    """標籤在標記的上方或下方（垂直對齊）：多行的整段文字留在自己的這一列裡，不蓋到標記、上下列與欄標題。
    上方的標籤整段往上移（原本只算自訂標籤文字中的換行，標籤上的多個欄位不算：第二行起蓋在標記上）；
    列高的一半放得下標記的半徑（5）、間距（4）與整段文字（兩行 28 像素）：74 像素。只有一行的照舊。"""
    above = render(parse(tmp_path, dots(valign="top")), db).spec
    mark = label_mark(above)
    assert (mark["align"], mark["baseline"]) == ("center", "bottom")
    assert (
        mark["dy"] == -23.0
    )  # 半徑 5、間距 4，再往上一行（14 像素）：最後一行的下緣在標記上方 4 像素
    assert rows(above) == {"step": 74}

    below = render(parse(tmp_path, dots(valign="bottom")), db).spec
    mark = label_mark(below)
    assert (mark["baseline"], mark["dy"]) == ("top", 9.0)  # 第一行的上緣在標記下方 4 像素
    assert rows(below) == {"step": 74}

    single = render(parse(tmp_path, dots(SALES_TEXT, valign="top")), db).spec
    assert label_mark(single)["dy"] == -9.0 and rows(single) == {"step": ROW_HEIGHT}


def test_dot_table_labels_with_different_line_counts(tmp_path: Path, db: Path) -> None:
    """值本身有換行（各標記的行數不同）：列高依行數最多的標籤，各標記的標籤依自己的行數置中。"""
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
        mark="Circle",
        encodings=f"<text column='{ref('usr:Calculation_1:nk')}' />",
    )
    spec = render(parse(tmp_path, sheet, calcs=calc), db).spec
    mark = label_mark(spec)
    field = next(
        layer["encoding"]["text"]["field"]
        for layer in spec["layer"]
        if layer["mark"]["type"] == "text"
    )
    assert {v[field] for v in spec["data"]["values"]} == {"big\ndeal", "small"}
    assert mark["dy"] == {
        "expr": f"-((length(split(datum[\"{field}\"] || '', '\\n')) - 1) * 14.0) / 2"
    }
    assert rows(spec) == {"step": 34}


def test_labels_above_marks_on_axes_count_label_fields(tmp_path: Path, db: Path) -> None:
    """座標軸上的圓點（散佈圖）：在標記上方的標籤依資料的行數整段往上移（標籤上的兩個欄位是兩行；原本只
    往上一個間距，第二行蓋在標記上）。在標記右邊的標籤位置不變（只有表格中的標籤整段置中）。"""

    def scatter(valign: str | None) -> dict[str, Any]:
        style = (
            "<style><style-rule element='cell'>"
            f"<format attr='vertical-align' value='{valign}' /></style-rule></style>"
            if valign
            else ""
        )
        sheet = worksheet(
            "Scatter",
            [REGION, SALES, PROFIT],
            rows=ref("sum:Sales:qk"),
            cols=ref("sum:Profit:qk"),
            mark="Circle",
            encodings=f"<text column='{ref('none:Region:nk')}' />" + SALES_TEXT,
            pane_extra=style,
        )
        folder = tmp_path / (valign or "side")
        folder.mkdir()
        return label_mark(render(parse(folder, sheet), db).spec)

    above = scatter("top")
    assert (above["baseline"], above["dy"]) == ("bottom", -23.0)
    side = scatter(None)
    assert (side["baseline"], side["dx"]) == ("middle", 9.0) and "dy" not in side
