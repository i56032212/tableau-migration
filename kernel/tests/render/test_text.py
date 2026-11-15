"""公開報表實測補上的顯示：工作表上的數字格式、自訂標籤與標題、總計、醒目提示表與文字表。

格式與寫法取自公開的 Tableau 報表（Car Sales、HR Analytics、Stock Market、Sales Performance）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute
from tmig.ir import IR, Pane, TextRun, TextStyle
from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import build_spec, main_encoding, worksheet_title
from tmig.sqlgen.query import GRAND_TOTAL, totals_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import DS, instance, ref, worksheet
from ..sqlgen.test_query import ROWS
from .test_vegalite import validator

REGION = instance("[Region]", "None", "[none:Region:nk]")
CATEGORY = instance("[Category]", "None", "[none:Category:nk]")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
PROFIT = instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative")
DISCRETE_SALES = instance("[Sales]", "Sum", "[sum:Sales:ok]", "ordinal")


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "orders.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Category" VARCHAR, "Order ID" VARCHAR, '
            '"Order Date" DATE, "Sales" DOUBLE, "Profit" DOUBLE, "Quantity" BIGINT)'
        )
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', ROWS)
    return path


def parse(tmp_path: Path, sheets: str, calcs: str = "") -> IR:
    xml = b.workbook(b.extract_datasource(columns=calcs), worksheets=sheets)
    return parse_workbook(b.write_twb(tmp_path, xml))


def render(ir: IR, db: Path, parameters: dict[str, Any] | None = None) -> Any:
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    rendered = build_spec(ir, ws.id, query.columns, execute(db, query.sql), parameters)
    assert rendered.spec is not None, rendered.unsupported
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered


def render_with_totals(ir: IR, db: Path) -> Any:
    """與檢視器相同：工作表的查詢之外，另外查詢總計，附加在圖表資料的最後。"""
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    totals = [(t, execute(db, t.query.sql)) for t in totals_sql(ir, ws.id, query)]
    rendered = build_spec(ir, ws.id, query.columns, execute(db, query.sql), totals=totals)
    assert rendered.spec is not None, rendered.unsupported
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered


def label(xml: str) -> str:
    """<customized-label> 的格式化文字（每段 <run>；& < > 已跳脫）。"""
    return f"<customized-label><formatted-text>{xml}</formatted-text></customized-label>"


def with_style(sheet: str, *items: tuple[str, str, str]) -> str:
    """工作表的 <style>（Tableau 的 <table> 只有一個 <style>）換成依欄位設定的 text-format。"""
    return sheet.replace("<style />", cell_formats(*items), 1)


def cell_formats(*items: tuple[str, str, str]) -> str:
    rules: dict[str, list[str]] = {}
    for element, inst, value in items:
        rules.setdefault(element, []).append(
            f"<format attr='text-format' field='{ref(inst)}' value='{value}' />"
        )
    body = "".join(
        f"<style-rule element='{element}'>{''.join(fmts)}</style-rule>"
        for element, fmts in rules.items()
    )
    return f"<style>{body}</style>"


# ---------------------------------------------------------------- 解析


def test_sheet_formats_labels_and_title_are_parsed(tmp_path: Path) -> None:
    kpi = worksheet(
        "KPI",
        [SALES, PROFIT],
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' /><text column='{ref('sum:Profit:qk')}' />",
        pane_extra=label(
            "<run>Total Sales&#10;</run>"
            f"<run fontsize='20'>&lt;{ref('sum:Sales:qk')}&gt;</run>"
            "<run> |&#198; Profit </run>"
            # 參照被拆成三段（Tableau 的寫法之一）
            f"<run>&lt;</run><run>{ref('sum:Profit:qk')}</run><run>&gt;</run>"
            "<run>&#198;&#10;on &lt;Sheet Name&gt; &lt;Unknown&gt;&#10;&#10;</run>"
        ),
    )
    kpi = with_style(
        kpi, ("cell", "sum:Sales:qk", 'c"$"#,##0,.0K'), ("label", "sum:Profit:qk", "p0.0%")
    ).replace(
        "<table>",
        "<layout-options><title><formatted-text>"
        "<run>&lt;[Parameters].[Parameter 2]&gt; sales</run>"
        "</formatted-text></title></layout-options><table>",
    )
    ir = parse(tmp_path, kpi)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert ws.formats == {"sum(f_profit)": "p0.0%", "sum(f_sales)": 'c"$"#,##0,.0K'}
    sales, profit = ws.marks.label
    # 各段的格式保留（KPI 的大數字）
    assert ws.marks.label_text == [
        TextRun(text="Total Sales\n"),
        TextRun(use=sales, style=TextStyle(size=20)),
        TextRun(text=" | Profit "),
        TextRun(use=profit),
        TextRun(text="\non KPI <Unknown>"),
    ]
    region_pick = next(p.id for p in ir.parameters if p.caption == "Region Pick")
    assert ws.title == [TextRun(parameter=region_pick), TextRun(text=" sales")]


def test_custom_tooltip_text(tmp_path: Path, db: Path) -> None:
    """自訂的工具提示（<customized-tooltip>）：文字、欄位值與參數值各段帶著格式；工具提示中的工作表
    （<Sheet …>）不算文字；不在檢視中的欄位不顯示。<tooltip-style tooltip-mode='none'> 為不顯示工具提示。"""
    tooltip = (
        "<customized-tooltip><formatted-text>"
        f"<run bold='true'>&lt;{ref('none:Region:nk')}&gt;</run>"
        "<run>&#198;&#10;Sales: </run>"
        f"<run fontcolor='#ff0000'>&lt;{ref('sum:Sales:qk')}&gt;</run>"
        f"<run> (&lt;[Parameters].[Parameter 2]&gt;) &lt;{ref('sum:Profit:qk')}&gt;</run>"
        "<run>&#198;&#10;&lt;Sheet name=&quot;Other&quot; maxwidth=&quot;300&quot; "
        "maxheight=&quot;300&quot; filter=&quot;&lt;All Fields&gt;&quot;&gt;</run>"
        "</formatted-text></customized-tooltip>"
    )
    sheet = worksheet(
        "Tips",
        [REGION, SALES, PROFIT],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        pane_extra=tooltip,
    )
    other = worksheet(
        "Other", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    ir = parse(tmp_path, sheet + other)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    region, sales = ws.rows[0], ws.columns[0]
    region_pick = next(p.id for p in ir.parameters if p.caption == "Region Pick")
    assert ws.tooltips is True
    # SUM(Profit) 不在檢視中：不顯示；工具提示中的工作表另外記下
    assert ws.marks.tooltip_text == [
        TextRun(use=region, style=TextStyle(bold=True)),
        TextRun(text="\nSales: "),
        TextRun(use=sales, style=TextStyle(color="#ff0000")),
        TextRun(text=" ("),
        TextRun(parameter=region_pick),
        TextRun(text=") "),
    ]
    assert [t.worksheet for t in ws.marks.tooltip_sheets] == [ir.worksheets[1].id]
    assert any("自訂工具提示用到不在檢視中的欄位" in i.reason for i in ir.ignored)
    spec = render(ir, db).spec
    template = spec["usermeta"]["tooltip"]
    assert template["show"] is True
    assert [r.get("text") for r in template["runs"]] == [
        None,
        "\nSales: ",
        None,
        " (",
        "East",
        ") ",
    ]
    assert template["runs"][0]["style"] == {"bold": True}
    # value 是原始值的資料欄（Null 時不顯示），column 是顯示文字的資料欄
    assert spec["usermeta"]["dimensions"][template["runs"][0]["value"]] == "f_region"
    assert spec["usermeta"]["measures"][template["runs"][2]["value"]] == "sum(f_sales)"
    assert template["runs"][2]["style"] == {"color": "#ff0000"}
    # 顯示文字的資料欄在資料中
    values = spec["data"]["values"]
    assert all(r["column"] in values[0] for r in template["runs"] if "column" in r)
    # 參數的值依目前的狀態
    changed = render(ir, db, {region_pick: "West"}).spec["usermeta"]["tooltip"]
    assert [r.get("text") for r in changed["runs"]][4] == "West"

    # 不顯示工具提示；沒有自訂文字也沒有關閉時不寫 usermeta.tooltip
    hidden = sheet.replace(tooltip, "").replace(
        "</table>", "<tooltip-style tooltip-mode='none' /></table>"
    )
    ir = parse(tmp_path, hidden)
    assert ir.unsupported == [] and ir.worksheets[0].tooltips is False
    assert render(ir, db).spec["usermeta"]["tooltip"] == {"show": False, "runs": []}
    ir = parse(tmp_path, sheet.replace(tooltip, ""))
    assert "tooltip" not in render(ir, db).spec["usermeta"]

    # 暫留時顯示（tooltip-mode='sticky'）、不包含命令按鈕（show-buttons='false'）
    sticky = sheet.replace("<customized-tooltip>", "<customized-tooltip show-buttons='false'>")
    sticky = sticky.replace("</table>", "<tooltip-style tooltip-mode='sticky' /></table>")
    ir = parse(tmp_path, sticky + other)
    ws = ir.worksheets[0]
    assert (ws.tooltips, ws.tooltip_mode, ws.tooltip_commands, ws.tooltip_select) == (
        True,
        "hover",
        False,
        True,
    )
    template = render(ir, db).spec["usermeta"]["tooltip"]
    assert (template["mode"], template["commands"]) == ("hover", False) and "select" not in template
    # 只有選項、沒有自訂文字：範本只帶選項
    plain = ir.model_copy(deep=True)
    plain.worksheets[0].marks.tooltip_text = []
    plain.worksheets[0].tooltip_select = False
    assert render(plain, db).spec["usermeta"]["tooltip"] == {
        "show": True,
        "runs": [],
        "mode": "hover",
        "commands": False,
        "select": False,
    }


def test_tooltip_text_per_marks_card(tmp_path: Path, db: Path) -> None:
    """各標記卡各自的自訂工具提示：文字不只一種時，每一層的工具提示欄位清單帶著標記卡的記號，
    範本列出各標記卡的文字；只有一種時所有的標記用同一份。"""
    from tmig.render.vegalite import TOOLTIP_CARD

    sheet = worksheet(
        "Two",
        [REGION, SALES, PROFIT],
        rows=ref("none:Region:nk"),
        cols=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    # 兩個量值各自一張標記卡（Tableau 的 <panes> 各有自己的 <customized-tooltip>）
    ws.panes = [
        Pane(measure=use, axis="columns", marks=ws.marks.model_copy(deep=True))
        for use in ws.columns
    ]
    assert len(ws.panes) == 2
    ws.panes[0].marks.tooltip_text = [TextRun(text="Sales of "), TextRun(use=ws.rows[0])]
    same = render(ir, db).spec
    assert "cards" not in same["usermeta"]["tooltip"]

    def titles(spec: dict[str, Any]) -> list[str]:
        found: list[str] = []

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                for item in node.get("encoding", {}).get("tooltip", []) or []:
                    if str(item.get("title", "")).startswith(TOOLTIP_CARD):
                        found.append(item["title"])
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(spec)
        return sorted(set(found))

    assert titles(same) == []
    ws.panes[1].marks.tooltip_text = [TextRun(text="Profit of "), TextRun(use=ws.rows[0])]
    spec = render(ir, db).spec
    template = spec["usermeta"]["tooltip"]
    assert sorted(template["cards"]) == ["1", "2"]
    assert template["cards"]["1"][0] == {"text": "Sales of "}
    assert template["cards"]["2"][0] == {"text": "Profit of "}
    assert titles(spec) == [f"{TOOLTIP_CARD}1", f"{TOOLTIP_CARD}2"]


def test_sheet_name_title_is_the_default(tmp_path: Path) -> None:
    sheet = worksheet(
        "Sales", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    titled = sheet.replace(
        "<table>",
        "<layout-options><title><formatted-text><run>&lt;Sheet Name&gt;</run>"
        "</formatted-text></title></layout-options><table>",
    )
    assert parse(tmp_path, titled).worksheets[0].title == []
    # 標題中的欄位值（以整張工作表計算，見 test_titles.py）
    with_field = sheet.replace(
        "<table>",
        "<layout-options><title><formatted-text>"
        f"<run>Sales for &lt;{ref('none:Region:nk')}&gt;</run>"
        "</formatted-text></title></layout-options><table>",
    )
    ir = parse(tmp_path, with_field)
    assert ir.unsupported == []
    region = ir.worksheets[0].rows[0]
    assert ir.worksheets[0].title == [TextRun(text="Sales for "), TextRun(use=region)]


def test_label_fields_must_be_on_the_card(tmp_path: Path) -> None:
    sheet = worksheet(
        "KPI",
        [SALES, PROFIT],
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=label(f"<run>&lt;{ref('sum:Profit:qk')}&gt;</run>"),
    )
    ir = parse(tmp_path, sheet)
    assert [u.reason for u in ir.unsupported] == [
        "工作表「KPI」的自訂標籤用到不在標記卡上的欄位 Profit"
    ]
    assert ir.worksheets[0].marks.label_text == []


def with_totals(sheet: str, *shelves: str) -> str:
    """欄列架加上總計（Tableau 的 <rows total='true'>、<cols total='true'>）。"""
    for shelf in shelves:
        sheet = sheet.replace(f"<{shelf}>", f"<{shelf} total='true'>", 1)
    return sheet


def matrix(mark: str = "Text", encodings: str = "") -> str:
    """Region × Category 的交叉表（文字表或醒目提示表），標記為 Sales 的合計。"""
    return worksheet(
        "Matrix",
        [REGION, CATEGORY, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("none:Category:nk"),
        mark=mark,
        encodings=encodings or f"<text column='{ref('sum:Sales:qk')}' />",
    )


def test_grand_totals_are_parsed(tmp_path: Path) -> None:
    ir = parse(tmp_path, with_totals(matrix(), "rows", "cols"))
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert ws.totals == ["rows", "columns"]
    assert ws.totals_colored is False
    # 色彩的「包含總計」
    colored = with_totals(matrix(), "rows").replace(
        "<style />",
        "<style><style-rule element='mark'><encoding attr='color' "
        f"field='{ref('sum:Sales:qk')}' include-totals='true' type='interpolated' />"
        "</style-rule></style>",
    )
    ws = parse(tmp_path, colored).worksheets[0]
    assert ws.totals == ["rows"] and ws.totals_colored is True


def test_grand_totals_need_discrete_headers(tmp_path: Path) -> None:
    """架上只有量值（例如長條圖的 SUM(Sales)）時沒有總計可加：Tableau 也不顯示，列為略過。"""
    bars = worksheet("Bars", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    ir = parse(tmp_path, with_totals(bars, "cols"))
    assert ir.unsupported == []
    assert ir.worksheets[0].totals == []
    assert [i.reason for i in ir.ignored] == [
        "工作表「Bars」的總計：架上沒有離散的維度，沒有總計可加"
    ]
    # 列架上的維度可以加總計（長條圖最下方多一條 Grand Total）
    assert parse(tmp_path, with_totals(bars, "rows")).worksheets[0].totals == ["rows"]


def test_visual_totals_are_parsed(tmp_path: Path) -> None:
    """Total Using（畫面上各標記的值再彙總）：記在工作表上，總計照樣顯示。"""
    visual = (
        "<column-instance column='[Sales]' derivation='Avg' name='[avg:Sales:vtsum:qk]' "
        "pivot='key' type='quantitative' visual-totals='Sum' />"
    )
    sheet = worksheet(
        "Matrix",
        [REGION, CATEGORY, visual],
        rows=ref("none:Region:nk"),
        cols=ref("none:Category:nk"),
        mark="Text",
        encodings=f"<text column='{ref('avg:Sales:vtsum:qk')}' />",
    )
    ir = parse(tmp_path, with_totals(sheet, "rows"))
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert ws.totals == ["rows"]
    assert [v.agg for v in ws.visual_totals] == ["sum"]
    # 沒有總計時 Total Using 沒有作用
    assert parse(tmp_path, sheet).worksheets[0].visual_totals == []


# ---------------------------------------------------------------- 呈現


def test_kpi_label_text_and_scaled_format(tmp_path: Path, db: Path) -> None:
    kpi = worksheet(
        "KPI",
        [SALES, PROFIT],
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' /><text column='{ref('sum:Profit:qk')}' />",
        pane_extra=label(
            f"<run>Total Sales&#10;&lt;{ref('sum:Sales:qk')}&gt; | Profit "
            f"&lt;{ref('sum:Profit:qk')}&gt;</run>"
        ),
    )
    kpi = with_style(
        kpi,
        ("cell", "sum:Sales:qk", 'c" $"#,##0,.00K;" $"-#,##0,.00K'),
        ("cell", "sum:Profit:qk", "p0.0%"),
    )
    spec = render(parse(tmp_path, kpi), db).spec
    (record,) = spec["data"]["values"]
    # Sales 合計 1310 → $1.31K；Profit 合計 81 以百分比格式顯示 8100.0%
    assert record["__text0"] == "Total Sales\n $1.31K | Profit 8100.0%"
    assert spec["encoding"]["text"] == {"field": "__text0", "type": "nominal"}
    assert spec["mark"]["lineBreak"] == "\n"


def test_null_fields_in_labels_show_nothing(tmp_path: Path, db: Path) -> None:
    """與 Tableau 相同：標籤中 Null 的欄位不顯示（例如只在負值時才有的 ▼ 符號）。"""
    calc = b.calc(
        "Calculation_1", "Down", 'IF SUM([Profit]) < 0 THEN "▼" END', "string", "dimension"
    )
    down = instance("[Calculation_1]", "User", "[usr:Calculation_1:nk]")
    sheet = worksheet(
        "Arrows",
        [REGION, PROFIT, down],
        rows=ref("none:Region:nk"),
        mark="Text",
        encodings=f"<text column='{ref('usr:Calculation_1:nk')}' /><text column='{ref('sum:Profit:qk')}' />",
        pane_extra=label(
            f"<run>&lt;{ref('usr:Calculation_1:nk')}&gt;&lt;{ref('sum:Profit:qk')}&gt;</run>"
        ),
    )
    spec = render(parse(tmp_path, sheet, calcs=calc), db).spec
    texts = {v["c0"]: v["__text0"] for v in spec["data"]["values"]}
    assert texts == {"East": "45", "West": "20", "South": "10", "North": "6"}


def test_label_on_bars_and_sheet_format_overrides_field_format(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=label(
            f"<run>&lt;{ref('none:Region:nk')}&gt;: &lt;{ref('sum:Sales:qk')}&gt;</run>"
        ),
    )
    sheet = with_style(sheet, ("cell", "sum:Sales:qk", 'c"$"#,##0,.0K'))
    calc = "<column caption='Sales' datatype='real' default-format='n#,##0.00' name='[Sales]' role='measure' type='quantitative' />"
    spec = render(parse(tmp_path, sheet, calcs=calc), db).spec
    _bars, text = spec["layer"]
    # 互相重疊的標籤由檢視器隱藏（清空文字）
    assert text["name"].startswith("__label_c")
    assert text["encoding"]["text"] == {
        "condition": {"test": "indexof(__label_hidden, datum.__lid) >= 0", "value": ""},
        "field": "__text0",
    }
    assert {"name": "__label_hidden", "value": []} in spec["params"]
    assert text["mark"]["lineBreak"] == "\n"
    west = next(v for v in spec["data"]["values"] if v["c0"] == "West")
    assert west["c1_label"] == "$0.8K" and west["__text0"] == "West: $0.8K"
    # 座標軸的單位換算以 labelExpr 呈現（d3-format 表達不了）
    assert "labelExpr" in spec["encoding"]["x"]["axis"]


def test_measure_names_without_values_does_not_fail(tmp_path: Path, db: Path) -> None:
    """Measure Names 在欄架上、Measure Values 卡是空的（股市報表的明細表）：不畫 Measure Names。"""
    sheet = worksheet(
        "Detail",
        [REGION, DISCRETE_SALES],
        rows=f"({ref('none:Region:nk')} / {ref('sum:Sales:ok')})",
        cols=f"[{DS}].[:Measure Names]",
        mark="Text",
    )
    rendered = render(parse(tmp_path, sheet), db)
    assert rendered.notes == ["Measure Names 沒有對應的量值（Measure Values 卡是空的），不顯示"]
    encoding = main_encoding(rendered.spec)
    assert encoding is not None and "x" not in encoding
    # 列架上離散的量值是標題（表格的一欄），不是座標軸
    assert rendered.mark == "text"
    assert encoding["y"]["field"] == "rows_dim"
    west = next(v for v in rendered.spec["data"]["values"] if v["c0"] == "West")
    assert west["rows_dim"] == "West｜800"
    assert (west["__header0"], west["__header1"]) == ("West", "800")


def test_highlight_table_and_scrolling_text_table(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "Highlight",
        [REGION, CATEGORY, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("none:Category:nk"),
        mark="Square",
        encodings=f"<color column='{ref('sum:Sales:qk')}' /><text column='{ref('sum:Sales:qk')}' />",
    )
    rendered = render(parse(tmp_path, sheet), db)
    spec = rendered.spec
    assert rendered.mark == "square"
    rect, text = spec["layer"]
    assert rect["mark"]["type"] == "rect"
    assert spec["encoding"]["color"]["type"] == "quantitative"
    assert text["encoding"]["text"]["field"].endswith("_label")
    assert text["encoding"]["color"] == {"value": "#1f2328"}
    # 標準大小的固定列高（20 像素）：列數多時由檢視器在區塊內捲動
    assert spec["height"] == {"step": 20}
    assert spec["autosize"] == {"type": "fit-x", "contains": "padding"}

    colored_text = sheet.replace("<mark class='Square' />", "<mark class='Text' />")
    spec = render(parse(tmp_path, colored_text), db).spec
    assert spec["mark"]["type"] == "text"
    # 與 Tableau 相同，文字也依自動色盤（Blue）著色，範圍為最小值到最大值
    scale = spec["encoding"]["color"]["scale"]
    assert (scale["range"][0], scale["range"][-1]) == ("#b9ddf1", "#2a5783")
    numbers = [v["c2"] for v in spec["data"]["values"]]
    assert (scale["domain"][0], scale["domain"][-1]) == (min(numbers), max(numbers))


def test_title_uses_current_parameter_value(tmp_path: Path) -> None:
    sheet = worksheet(
        "Sales", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    titled = sheet.replace(
        "<table>",
        "<layout-options><title><formatted-text>"
        "<run>&lt;[Parameters].[Parameter 2]&gt; by Region</run>"
        "</formatted-text></title></layout-options><table>",
    )
    ir = parse(tmp_path, titled)
    ws = ir.worksheets[0]
    region_pick = next(p.id for p in ir.parameters if p.caption == "Region Pick")
    assert worksheet_title(ir, ws) == "East by Region"
    assert worksheet_title(ir, ws, {region_pick: "West"}) == "West by Region"
    plain = parse(tmp_path, sheet)
    assert worksheet_title(plain, plain.worksheets[0]) == "Sales"


def test_grand_totals_in_text_table(tmp_path: Path, db: Path) -> None:
    """總計列在最下方、總計欄在最右邊，右下角是全部的合計；標題為 Grand Total。"""
    rendered = render_with_totals(parse(tmp_path, with_totals(matrix(), "rows", "cols")), db)
    spec = rendered.spec
    # 列數只算工作表的標記（7 個 Region × Category 的組合），不含總計
    assert rendered.rows == 7 and spec["usermeta"]["rows"] == 7
    values = spec["data"]["values"]
    marks, totals = values[:7], values[7:]
    assert all("__total" not in v for v in marks)
    assert [v["__row"] for v in marks] == list(range(7))
    # 總計不參與選取
    assert all(v["__total"] is True and v["__row"] == -1 for v in totals)
    cells = {(v["c0_label"], v["c1_label"]): v["c2"] for v in totals}
    assert cells == {
        # 總計列（列架的總計）：每個 Category 的合計
        (GRAND_TOTAL, "Furniture"): 530,
        (GRAND_TOTAL, "Technology"): 700,
        (GRAND_TOTAL, "Office"): 80,
        # 總計欄（欄架的總計）：每個 Region 的合計
        ("East", GRAND_TOTAL): 350,
        ("West", GRAND_TOTAL): 800,
        ("South", GRAND_TOTAL): 100,
        ("North", GRAND_TOTAL): 60,
        (GRAND_TOTAL, GRAND_TOTAL): 1310,
    }
    # 總計的原始值欄是空的（篩選與動作不會用到），顯示文字依量值的格式
    corner = next(v for v in totals if v["c0_label"] == v["c1_label"] == GRAND_TOTAL)
    assert corner["c0"] is None and corner["c1"] is None and corner["c2_label"] == "1,310"
    # 列依資料的順序；欄依查詢算出的名次、與列無關（第一列 East 沒有 Office，Office 仍排在
    # Technology 之前，與 Tableau 相同）。Grand Total 在各軸的最後
    assert spec["encoding"]["y"]["sort"] is None
    assert spec["encoding"]["x"]["sort"] == ["Furniture", "Office", "Technology", GRAND_TOTAL]
    assert all("__order" not in v for v in values)


def test_grand_totals_are_not_colored_by_default(tmp_path: Path, db: Path) -> None:
    """與 Tableau 的預設相同：醒目提示表的總計不上色，色階的範圍也只看各標記的值。"""
    encodings = f"<color column='{ref('sum:Sales:qk')}' /><text column='{ref('sum:Sales:qk')}' />"
    sheet = with_totals(matrix("Square", encodings), "rows", "cols")
    color = render_with_totals(parse(tmp_path, sheet), db).spec["encoding"]["color"]
    assert (color["scale"]["domain"][0], color["scale"]["domain"][-1]) == (20, 500)
    assert color["condition"] == {"test": "datum['__total'] === true", "value": "transparent"}
    colored = sheet.replace(
        "<style />",
        "<style><style-rule element='mark'><encoding attr='color' "
        f"field='{ref('sum:Sales:qk')}' include-totals='true' type='interpolated' />"
        "</style-rule></style>",
    )
    color = render_with_totals(parse(tmp_path, colored), db).spec["encoding"]["color"]
    # 包含總計：總計也上色，色階的範圍到總計的 1,310
    assert "condition" not in color
    assert (color["scale"]["domain"][0], color["scale"]["domain"][-1]) == (20, 1310)


def test_grand_totals_with_nested_headers_and_bars(tmp_path: Path, db: Path) -> None:
    year = instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal")
    nested = worksheet(
        "Nested",
        [REGION, CATEGORY, SALES, year],
        rows=f"({ref('yr:Order Date:ok')} / {ref('none:Region:nk')})",
        cols=ref("sum:Sales:qk"),
    )
    rendered = render_with_totals(parse(tmp_path, with_totals(nested, "rows")), db)
    values = rendered.spec["data"]["values"]
    # 列架上兩層的標題合併成一欄：總計列整列都是 Grand Total
    (total,) = [v for v in values if v.get("__total")]
    assert total["rows_dim"] == GRAND_TOTAL
    assert total["c2"] == 1310
    assert values[-1] is total
    assert rendered.mark == "bar"
