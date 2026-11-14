"""小計（Subtotal）：<subtotals> 列出的維度的每個值之後加一列（或一欄）合計，與總計相同在去掉同一架上
之後維度的詳細程度重新彙總（Tableau 的「自動」）。寫法取自公開報表 Retail Analysis 的 Sheet 7
（列架 Category／Sub-Category、欄架 Segment／Region，Category 與 Segment 有小計，兩架都有總計）。

測試資料見 tests/sqlgen/test_query.py：East 350（Furniture 150、Technology 200）、West 800
（Furniture 300、Technology 500）、South 100（Furniture 80、Office 20）、North 60（Office 60）。
"""

from __future__ import annotations

from pathlib import Path

from tmig.data.executor import execute
from tmig.render.vegalite import main_encoding
from tmig.sqlgen.query import GRAND_TOTAL, SUBTOTAL, totals_sql, worksheet_sql

from ..parser.builder import DS, ref, worksheet
from ..sqlgen.test_query import CATEGORY, REGION, SALES, YEAR
from .test_text import db, parse, render_with_totals, with_totals

__all__ = ["db"]  # pytest fixture（測試資料庫）


def nested(*subtotals: str) -> str:
    """列架 Region／Category 的文字表，Sales 的合計；subtotals 為有小計的欄位實例。"""
    sheet = worksheet(
        "Nested",
        [REGION, CATEGORY, SALES],
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    if subtotals:
        columns = "".join(f"<column>{ref(s)}</column>" for s in subtotals)
        sheet = sheet.replace("<rows>", f"<subtotals>{columns}</subtotals><rows>", 1)
    return sheet


def test_subtotals_are_parsed(tmp_path: Path) -> None:
    ir = parse(tmp_path, nested("none:Region:nk"))
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert [u.field for u in ws.subtotals] == ["f_region"]
    # 最內層的維度沒有小計；不在架上的維度（之前放過、已移走）略過
    ir = parse(tmp_path, nested("none:Category:nk", "none:Segment:nk"))
    assert ir.unsupported == [] and ir.worksheets[0].subtotals == []


def test_subtotals_before_measure_names_are_unsupported(tmp_path: Path) -> None:
    """同一架上之後只有 Measure Names（公開範例 geocoding 的 What If Forecast）：Tableau 的小計怎麼畫
    未確認，列為不支援，不默默省略。"""
    sheet = worksheet(
        "Measures",
        [REGION, SALES],
        rows=f"({ref('none:Region:nk')} / [{DS}].[:Measure Names])",
        mark="Text",
        encodings=f"<text column='[{DS}].[Multiple Values]' />",
        view_extra=(
            f"<filter class='categorical' column='[{DS}].[:Measure Names]'>"
            f"<groupfilter function='member' level='[:Measure Names]' "
            f"member='&quot;{ref('sum:Sales:qk')}&quot;' /></filter>"
        ),
    )
    sheet = sheet.replace(
        "<rows>", f"<subtotals><column>{ref('none:Region:nk')}</column></subtotals><rows>", 1
    )
    ir = parse(tmp_path, sheet)
    assert [u.reason for u in ir.unsupported] == [
        "工作表「Measures」的小計：「Region」之後只有 Measure Names 或離散的量值，v0 不支援"
    ]
    assert ir.worksheets[0].subtotals == []


def test_subtotal_queries(tmp_path: Path, db: Path) -> None:
    ir = parse(tmp_path, with_totals(nested("none:Region:nk"), "rows"))
    ws = ir.worksheets[0]
    main = worksheet_sql(ir, ws.id)
    totals = totals_sql(ir, ws.id, main)
    assert [(t.shelves, t.dropped, t.group) for t in totals] == [
        (("rows",), ["f_category"], ["f_region"]),
        (("rows",), ["f_region", "f_category"], None),
    ]
    assert totals[0].labels == {"f_category": SUBTOTAL}
    assert totals[1].label("f_region") == GRAND_TOTAL
    rows = execute(db, totals[0].query.sql).records()
    assert {r["f_region"]: r["sum(f_sales)"] for r in rows} == {
        "East": 350,
        "North": 60,
        "South": 100,
        "West": 800,
    }


def test_subtotals_follow_their_groups(tmp_path: Path, db: Path) -> None:
    """小計緊接在各組之後（表頭依資料的順序），總計在最後；小計與總計都不參與選取。"""
    ir = parse(tmp_path, with_totals(nested("none:Region:nk"), "rows"))
    rendered = render_with_totals(ir, db)
    assert rendered.rows == 7  # 只算工作表的標記
    values = rendered.spec["data"]["values"]
    order = [(v["rows_dim"], v["c2"]) for v in values]
    assert order == [
        ("East｜Furniture", 150),
        ("East｜Technology", 200),
        ("East｜Total", 350),
        ("North｜Office", 60),
        ("North｜Total", 60),
        ("South｜Furniture", 80),
        ("South｜Office", 20),
        ("South｜Total", 100),
        ("West｜Furniture", 300),
        ("West｜Technology", 500),
        ("West｜Total", 800),
        (GRAND_TOTAL, 1310),
    ]
    assert all(v["__row"] == -1 for v in values if v.get("__total"))


def test_column_subtotals_cross_row_totals(tmp_path: Path, db: Path) -> None:
    """欄的小計（每個 Category 之後的 Total 欄）與兩架的總計：總計列在小計欄也有值；
    欄依各自的排序排列（與列無關），小計緊接在它的組之後，Grand Total 在最後。"""
    sheet = worksheet(
        "Cross",
        [REGION, CATEGORY, SALES, YEAR],
        rows=ref("none:Region:nk"),
        cols=f"({ref('none:Category:nk')} / {ref('yr:Order Date:ok')})",
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    sheet = sheet.replace(
        "<rows>", f"<subtotals><column>{ref('none:Category:nk')}</column></subtotals><rows>", 1
    )
    ir = parse(tmp_path, with_totals(sheet, "rows", "cols"))
    ws = ir.worksheets[0]
    assert [u.field for u in ws.subtotals] == ["f_category"]
    totals = totals_sql(ir, ws.id, worksheet_sql(ir, ws.id))
    year = "year(f_order_date)"
    assert [(t.shelves, t.labels, t.group) for t in totals] == [
        (("columns",), {year: SUBTOTAL}, ["f_region", "f_category"]),
        (("rows",), {"f_region": GRAND_TOTAL}, None),
        (("columns",), {"f_category": GRAND_TOTAL, year: GRAND_TOTAL}, None),
        (("rows", "columns"), {"f_region": GRAND_TOTAL, year: SUBTOTAL}, None),
        (
            ("rows", "columns"),
            {"f_region": GRAND_TOTAL, "f_category": GRAND_TOTAL, year: GRAND_TOTAL},
            None,
        ),
    ]
    spec = render_with_totals(ir, db).spec
    # 第一列 East 沒有 2024 年的 Furniture：仍排在 Furniture 之下、小計之前（與 Tableau 相同）
    x = main_encoding(spec)["x"]  # type: ignore[index]
    assert x["sort"] == [
        "Furniture｜2023",
        "Furniture｜2024",
        "Furniture｜Total",
        "Office｜2024",
        "Office｜Total",
        "Technology｜2023",
        "Technology｜2024",
        "Technology｜Total",
        GRAND_TOTAL,
    ]
    # 多層的欄標題每一層一行（外層合併成一格），座標軸只留下欄位標籤
    assert x["axis"]["labels"] is False
    furniture = {
        v["__colheader0"] for v in spec["data"]["values"] if v["cols_dim"] == "Furniture｜2024"
    }
    assert "Furniture" in furniture  # Furniture 有三欄：寫在中間那一欄
    values = spec["data"]["values"]
    assert list(dict.fromkeys(v["c0_label"] for v in values)) == [
        "East",
        "North",
        "South",
        "West",
        GRAND_TOTAL,
    ]
    cells = {(v["c0_label"], v["cols_dim"]): v["c3"] for v in values}
    assert cells["South", "Office｜Total"] == 20
    assert cells[GRAND_TOTAL, "Furniture｜2023"] == 450
    assert cells[GRAND_TOTAL, "Furniture｜Total"] == 530
    assert cells[GRAND_TOTAL, "Technology｜Total"] == 700
    assert cells["West", GRAND_TOTAL] == 800
    assert cells[GRAND_TOTAL, GRAND_TOTAL] == 1310


def test_nested_subtotals(tmp_path: Path, db: Path) -> None:
    """兩層小計（Region 與 Category）：較內層的小計在前；標題中去掉的維度只寫一次 Total。"""
    sheet = worksheet(
        "Nested",
        [REGION, CATEGORY, SALES, YEAR],
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')} / {ref('yr:Order Date:ok')})",
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    columns = "".join(f"<column>{ref(s)}</column>" for s in ("none:Region:nk", "none:Category:nk"))
    sheet = sheet.replace("<rows>", f"<subtotals>{columns}</subtotals><rows>", 1)
    ir = parse(tmp_path, with_totals(sheet, "rows"))
    assert [u.field for u in ir.worksheets[0].subtotals] == ["f_region", "f_category"]
    values = render_with_totals(ir, db).spec["data"]["values"]
    assert [
        (v["rows_dim"], v["c3"]) for v in values if v["rows_dim"].startswith(("East", "G"))
    ] == [
        ("East｜Furniture｜2023", 150),
        ("East｜Furniture｜Total", 150),
        ("East｜Technology｜2023", 200),
        ("East｜Technology｜Total", 200),
        ("East｜Total", 350),
        (GRAND_TOTAL, 1310),
    ]
    assert [v["rows_dim"] for v in values].index("South｜Total") == [
        v["rows_dim"] for v in values
    ].index("West｜Furniture｜2023") - 1


def test_totals_on_top_and_left(tmp_path: Path, db: Path) -> None:
    """欄總計在上方（<rows onTop='true'>）、列總計在左側（<cols onLeft='true'>）：總計與小計在各組之前。"""
    sheet = with_totals(nested("none:Region:nk"), "rows").replace(
        "<rows total='true'>", "<rows total='true' onTop='true'>", 1
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    assert ir.worksheets[0].totals_top and not ir.worksheets[0].totals_left
    values = render_with_totals(ir, db).spec["data"]["values"]
    assert [v["rows_dim"] for v in values] == [
        GRAND_TOTAL,
        "East｜Total",
        "East｜Furniture",
        "East｜Technology",
        "North｜Total",
        "North｜Office",
        "South｜Total",
        "South｜Furniture",
        "South｜Office",
        "West｜Total",
        "West｜Furniture",
        "West｜Technology",
    ]

    cross = worksheet(
        "Cross",
        [REGION, CATEGORY, SALES, YEAR],
        rows=ref("none:Region:nk"),
        cols=f"({ref('none:Category:nk')} / {ref('yr:Order Date:ok')})",
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    cross = cross.replace(
        "<rows>", f"<subtotals><column>{ref('none:Category:nk')}</column></subtotals><rows>", 1
    )
    cross = with_totals(cross, "rows", "cols").replace(
        "<cols total='true'>", "<cols total='true' onLeft='true'>", 1
    )
    (tmp_path / "left").mkdir()
    ir = parse(tmp_path / "left", cross)
    assert ir.worksheets[0].totals_left and not ir.worksheets[0].totals_top
    spec = render_with_totals(ir, db).spec
    assert main_encoding(spec)["x"]["sort"] == [  # type: ignore[index]
        GRAND_TOTAL,
        "Furniture｜Total",
        "Furniture｜2023",
        "Furniture｜2024",
        "Office｜Total",
        "Office｜2024",
        "Technology｜Total",
        "Technology｜2023",
        "Technology｜2024",
    ]
    # 列的總計仍在最下方
    assert list(dict.fromkeys(v["c0_label"] for v in spec["data"]["values"]))[-1] == GRAND_TOTAL

    # 只有欄維度：表頭依資料的順序，總計欄放在最前面
    only = worksheet(
        "Only",
        [CATEGORY, SALES],
        cols=ref("none:Category:nk"),
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
    )
    only = with_totals(only, "cols").replace(
        "<cols total='true'>", "<cols total='true' onLeft='true'>", 1
    )
    (tmp_path / "only").mkdir()
    ir = parse(tmp_path / "only", only)
    values = render_with_totals(ir, db).spec["data"]["values"]
    assert [v["c0_label"] for v in values] == [GRAND_TOTAL, "Furniture", "Office", "Technology"]
