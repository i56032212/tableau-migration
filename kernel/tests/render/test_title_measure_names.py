"""標題中的 Measure Names 與 Measure Values（推定）。

Tableau 2021.2 的說明文件沒有寫它們在標題中的值（「Titles」的 Insert 選單只說可以插入欄位值，
https://help.tableau.com/v2021.2/pro/desktop/en-us/formatting_specific_titlecaption.htm ，2026-11-23 查閱）。
依標題中其他欄位的規則：Measure Names 與維度相同，涵蓋的量值只有一個時是它的名稱（別名照樣套用）、多個時是
*；Measure Values 只有一個量值時是那個量值在全部資料的彙總，多個時是 *。

資料見 tests/sqlgen/test_query.py 的 ROWS（銷售合計 1,310、7 張訂單）。
"""

from __future__ import annotations

from pathlib import Path

from tmig.sqlgen.query import title_sql, worksheet_sql

from ..parser.builder import DS, ref, worksheet
from .test_text import db
from .test_titles import ORDERS, REGION, SALES, esc, parse, title, titled

__all__ = ["db"]  # pytest fixture（測試資料庫）

NAMES = f"[{DS}].[:Measure Names]"
VALUES = f"[{DS}].[Multiple Values]"
TOKENS = f"&lt;{NAMES}&gt; = &lt;{VALUES}&gt;"


def measure_table(members: list[str], text: str = TOKENS) -> str:
    """各區的 Measure Values 文字表，Measure Names 篩選選取 members（欄位實例）。"""
    selected = "".join(
        f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref(m)}&quot;' />"
        for m in members
    )
    sheet = worksheet(
        "KPIs",
        [REGION, SALES, ORDERS],
        rows=ref("none:Region:nk"),
        cols=NAMES,
        mark="Text",
        encodings=f"<text column='{VALUES}' />",
        view_extra=(
            f"<filter class='categorical' column='{NAMES}'>"
            f"<groupfilter function='union' user:op='manual'>{selected}</groupfilter></filter>"
        ),
    )
    return titled(sheet, text)


def test_one_measure_shows_its_name_and_total(tmp_path: Path, db: Path) -> None:
    """Measure Values 只有 SUM(Sales)：Measure Names 是「Sales」，Measure Values 是全部資料的合計 1,310
    （與檢視的地區無關，與標題中的其他量值相同）。"""
    sheet = measure_table(["sum:Sales:qk"])
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert title(ir, db) == "Sales = 1,310"


def test_several_measures_show_an_asterisk(tmp_path: Path, db: Path) -> None:
    """兩個量值：Measure Names 與 Measure Values 各有多個值，與維度相同顯示 *。"""
    sheet = measure_table(["sum:Sales:qk", "ctd:Order ID:qk"])
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert title(ir, db) == "* = *"
    # 量值不另外查詢（多個時不需要值）
    ws = ir.worksheets[0]
    queries = title_sql(ir, ws.id, worksheet_sql(ir, ws.id))
    assert queries is not None and queries.measures is None


def test_measure_names_alias_and_other_fields(tmp_path: Path, db: Path) -> None:
    """Measure Names 的別名照樣套用；與標題中的其他量值一起時各自計算。"""
    columns = (
        "<column datatype='string' name='[:Measure Names]' role='dimension' type='nominal'>"
        f"<aliases><alias key='&quot;{ref('ctd:Order ID:qk')}&quot;' value='訂單數' /></aliases>"
        "</column>"
    )
    text = f"{TOKENS}（{esc('sum:Sales:qk')}）"
    sheet = measure_table(["ctd:Order ID:qk"], text)
    ir = parse(tmp_path, sheet, columns)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert title(ir, db) == "訂單數 = 7（1,310）"


def test_measure_names_over_several_axes(tmp_path: Path, db: Path) -> None:
    """沒有 Measure Values 卡（兩個量值各自一個軸、色彩是 Measure Names）：Measure Names 涵蓋欄架上的量值，
    多個時顯示 *。"""
    sheet = worksheet(
        "Axes",
        [REGION, SALES, ORDERS],
        rows=ref("none:Region:nk"),
        cols=f"({ref('sum:Sales:qk')} + {ref('ctd:Order ID:qk')})",
        encodings=f"<color column='{NAMES}' />",
    )
    ir = parse(tmp_path, titled(sheet, f"By &lt;{NAMES}&gt;"))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert title(ir, db) == "By *"
