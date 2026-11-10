"""字型：Tableau 的預設字型（9pt、標題 15pt）、活頁簿與工作表的字型設定、欄位與標記標籤的字型。

預設值依 Tableau 的畫面量測（見 tmig.render.fonts）。測試資料見 tests/sqlgen/test_query.py。
"""

from __future__ import annotations

from pathlib import Path

from tmig.ir import FontStyle
from tmig.parser.workbook import parse_workbook
from tmig.render.fonts import css_family, pixels, resolve

from ..parser import builder as b
from ..parser.builder import ref, worksheet
from .test_formats import bars, fmt, rule, styled
from .test_text import REGION, SALES, db, parse, render

__all__ = ["db"]  # pytest fixture


def test_font_names_and_sizes() -> None:
    """Tableau 隨附的字型每個粗細是一個名稱；其他字型名稱結尾的粗細；pt → 像素。"""
    family, weight = css_family("Tableau Semibold")
    assert family.startswith('"Tableau Semibold", "Benton Sans"') and weight == 600
    assert css_family("Segoe UI Semibold") == ('"Segoe UI Semibold", "Segoe UI", sans-serif', 600)
    assert css_family("Calibri") == ('"Calibri", sans-serif', 400)
    assert (pixels(9), pixels(15), pixels(8)) == (12, 20, 10.67)
    # 後面的覆寫前面的；粗體為 700，明確不粗體時用字型本身的粗細
    font = resolve(FontStyle(family="Calibri", size=10), FontStyle(bold=True))
    assert (font.size, font.weight, font.family) == (13.33, 700, '"Calibri", sans-serif')
    assert resolve(FontStyle(family="Tableau Semibold", bold=False)).weight == 600
    assert resolve(weight=600).weight == 600 and resolve().weight == 400


def test_fonts_are_parsed(tmp_path: Path) -> None:
    """活頁簿的 all／worksheet 在前、工作表的覆寫；label 加上 field 為這個欄位；標記卡的 datalabel。"""
    sheet = styled(
        worksheet(
            "Bars",
            [REGION, SALES],
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            pane_extra=(
                "<style><style-rule element='datalabel'>"
                "<format attr='font-size' value='7' /><format attr='font-weight' value='bold' />"
                "</style-rule></style>"
            ),
        ),
        rule("worksheet", fmt("font-size", "8")),
        rule(
            "label",
            fmt("font-size", "10", "none:Region:nk"),
            fmt("font-family", "Tableau Semibold", "none:Region:nk"),
        ),
        rule("cell", fmt("font-style", "italic")),
        rule("title", fmt("font-size", "12")),
    )
    workbook_style = (
        "<style><style-rule element='all'><format attr='font-family' value='Trebuchet MS' />"
        "</style-rule><style-rule element='worksheet'><format attr='font-size' value='11' />"
        "<format attr='font-weight' value='bold' /></style-rule></style>"
    )
    xml = b.workbook(b.extract_datasource(), sheet).replace(
        "<datasources>", workbook_style + "<datasources>", 1
    )
    ws = parse_workbook(b.write_twb(tmp_path, xml)).worksheets[0]
    assert ws.fonts["worksheet"] == FontStyle(family="Trebuchet MS", size=8, bold=True)
    assert ws.fonts["cell"] == FontStyle(italic=True)
    assert ws.fonts["title"] == FontStyle(size=12)
    assert [a.font for a in ws.axes] == [FontStyle(family="Tableau Semibold", size=10)]
    assert ws.marks.label_font == FontStyle(size=7, bold=True)


def test_default_fonts_render(tmp_path: Path, db: Path) -> None:
    """沒有設定時與 Tableau 相同：刻度、標題成員與座標軸標題 12 像素，刻度與成員灰色、標題深灰、
    欄位標籤半粗體，工作表標題 20 像素。"""
    spec = render(parse(tmp_path, bars()), db).spec
    axis = spec["config"]["axis"]
    assert (axis["labelFontSize"], axis["titleFontSize"]) == (12, 12)
    assert (axis["labelColor"], axis["titleColor"]) == ("#555555", "#333333")
    assert (axis["labelFontWeight"], axis["titleFontWeight"]) == (400, 400)
    assert spec["config"]["font"].startswith('"Tableau Book"')
    y = spec["encoding"]["y"]["axis"]
    assert (y["titleFontSize"], y["titleFontWeight"], y["labelFontSize"]) == (12, 600, 12)
    title = spec["usermeta"]["title_font"]
    assert (title["size"], title["weight"]) == (20, 400)
    assert spec["usermeta"]["title_color"] == "#333333"


def test_configured_fonts_render(tmp_path: Path, db: Path) -> None:
    """工作表的字型（8pt、Calibri）、欄位的標題字型、標記標籤的字型與標題的字型。"""
    sheet = styled(
        worksheet(
            "Bars",
            [REGION, SALES],
            rows=ref("none:Region:nk"),
            cols=ref("sum:Sales:qk"),
            encodings=f"<text column='{ref('sum:Sales:qk')}' />",
            pane_extra=(
                "<style><style-rule element='datalabel'>"
                "<format attr='font-size' value='7' /></style-rule></style>"
            ),
        ),
        rule("worksheet", fmt("font-size", "8"), fmt("font-family", "Calibri")),
        rule(
            "label",
            fmt("font-size", "10", "none:Region:nk"),
            fmt("font-weight", "bold", "none:Region:nk"),
        ),
        rule("title", fmt("font-size", "11")),
    )
    spec = render(parse(tmp_path, sheet), db).spec
    assert spec["config"]["axis"]["titleFontSize"] == 10.67
    assert spec["config"]["font"] == '"Calibri", sans-serif'
    y = spec["encoding"]["y"]["axis"]
    assert (y["labelFontSize"], y["labelFontWeight"]) == (13.33, 700)
    label = next(layer for layer in spec["layer"] if layer["mark"]["type"] == "text")
    assert label["mark"]["fontSize"] == 9.33
    assert spec["usermeta"]["title_font"]["size"] == 14.67
    assert spec["usermeta"]["title_font"]["family"] == '"Calibri", sans-serif'
