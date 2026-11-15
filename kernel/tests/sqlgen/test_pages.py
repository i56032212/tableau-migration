"""頁面架：每一頁是頁面欄位的一個值，檢視只顯示目前這一頁；目前的頁取自工作表視窗的 <current-page>。"""

from __future__ import annotations

from pathlib import Path

from tmig.data.executor import execute
from tmig.ir import IR, DateDerivation, FieldUse
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import QueryState
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import ref, worksheet
from .test_query import REGION, SALES, YEAR, db

__all__ = ["db"]  # pytest fixture

WINDOW = """
  <windows>
    <window class='worksheet' name='Paged'>
      <viewpoint>
        <current-page><multibucket><bucket>2024</bucket></multibucket></current-page>
      </viewpoint>
    </window>
  </windows>"""


def paged(tmp_path: Path, current: bool = True, dashboard: str = "") -> IR:
    sheet = worksheet(
        "Paged",
        [REGION, SALES, YEAR],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        table_extra=f"<pages><column>{ref('yr:Order Date:ok')}</column></pages>",
    )
    xml = b.workbook(worksheets=sheet, dashboards=dashboard)
    if current:
        xml = xml.replace("  <windows />", WINDOW)
    return parse_workbook(b.write_twb(tmp_path, xml))


def regions(ir: IR, db: Path, state: QueryState | None = None) -> dict[str, float]:
    ws = ir.worksheets[0]
    result = execute(db, worksheet_sql(ir, ws.id, state).sql)
    return {r[0]: r[1] for r in result.rows}


def test_current_page_filters_the_view(tmp_path: Path, db: Path) -> None:
    ir = paged(tmp_path)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert ws.pages == FieldUse(field="f_order_date", date=DateDerivation(kind="part", unit="year"))
    assert ws.page == 2024
    # 2024 年：West 500、South 100、North 60（East 只有 2023 年的訂單）
    assert regions(ir, db) == {"North": 60.0, "South": 100.0, "West": 500.0}
    # 檢視器切換到 2023 年
    state = QueryState(pages={ws.id: 2023})
    assert regions(ir, db, state) == {"East": 350.0, "West": 300.0}


def test_without_current_page(tmp_path: Path, db: Path) -> None:
    """存檔時沒有記錄目前的頁：查詢不篩選（檢視器依可選值切到第一頁）。"""
    ir = paged(tmp_path, current=False)
    ws = ir.worksheets[0]
    assert ws.pages is not None and ws.page is None
    assert set(regions(ir, db)) == {"East", "North", "South", "West"}


def test_page_control_zone(tmp_path: Path) -> None:
    zones = """
      <dashboard name='Board'>
        <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' />
        <zones>
          <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
            <zone h='80000' id='2' name='Paged' w='100000' x='0' y='0' />
            <zone h='20000' id='3' name='Paged' type-v2='currpage' w='100000' x='0' y='80000' />
          </zone>
        </zones>
      </dashboard>"""
    ir = paged(tmp_path, dashboard=zones)
    assert ir.unsupported == []
    page = next(z for z in ir.dashboards[0].zones[0].children if z.kind == "page")
    assert page.worksheet == ir.worksheets[0].id


def test_page_history(tmp_path: Path, db: Path) -> None:
    """頁面的顯示歷程：目前這一頁之前的幾頁一併查詢（頁面欄位加到詳細資料），畫成淡化的標記與軌跡；
    主圖與列數只有目前這一頁。第一頁沒有歷程。"""
    from tmig.ir import PageHistory
    from tmig.planner.state import with_page_history
    from tmig.render.vegalite import build_spec

    ir = paged(tmp_path)
    ws = ir.worksheets[0]
    assert ws.pages is not None
    ws.page_history = PageHistory(show="both")
    state = QueryState()
    shown = with_page_history(ir, ws.id, state, [2023, 2024])
    changed = shown.worksheets[0]
    assert changed.page_history is not None and changed.page_history.previous == [2023]
    assert changed.marks.detail[-1] == ws.pages and changed.page == 2024
    assert ir.worksheets[0].marks.detail == []  # 存檔的報表不變
    query = worksheet_sql(shown, ws.id, state)
    result = execute(db, query.sql)
    assert len(result.rows) == 5  # 2023 年兩個地區、2024 年三個
    spec = build_spec(shown, ws.id, query.columns, result).spec
    assert spec is not None
    trail, history, main = spec["layer"]
    assert (trail["name"], history["name"]) == ("__deco_trail", "__deco_history")
    region = main["encoding"]["y"]["field"]
    assert {v[region] for v in spec["data"]["values"]} == {"North", "South", "West"}
    assert spec["usermeta"]["rows"] == 3
    assert {v[region] for v in history["data"]["values"]} == {"East", "West"}
    assert history["mark"]["opacity"] == 0.3 and history["mark"]["tooltip"] is False
    # 軌跡：同一個地區依頁面的順序連線（West 兩年都有）
    assert len(trail["data"]["values"]) == 5 and trail["mark"]["type"] == "line"
    raw = region.removesuffix("_label")  # 以原始值的欄辨識同一個標記
    assert trail["encoding"]["detail"] == [{"field": raw, "type": "nominal"}]
    orders = {(v[region], v["__page"]) for v in trail["data"]["values"]}
    assert {("West", 0), ("West", 1), ("East", 0), ("North", 1)} <= orders

    # 只顯示標記、不淡化
    ws.page_history = PageHistory(show="marks", fade=False)
    shown = with_page_history(ir, ws.id, state, [2023, 2024])
    query = worksheet_sql(shown, ws.id, state)
    spec = build_spec(shown, ws.id, query.columns, execute(db, query.sql)).spec
    assert spec is not None
    only, _ = spec["layer"]
    assert only["name"] == "__deco_history" and "opacity" not in only["mark"]

    # 第一頁、或找不到目前的頁：沒有歷程，與沒有設定時相同
    first = QueryState(pages={ws.id: 2023})
    assert with_page_history(ir, ws.id, first, [2023, 2024]) is ir
    assert with_page_history(ir, ws.id, state, []) is ir
    # 顯示之前幾頁
    ws.page_history = PageHistory(length=1)
    limited = with_page_history(ir, ws.id, state, [2021, 2022, 2023, 2024]).worksheets[0]
    assert limited.page_history is not None and limited.page_history.previous == [2023]
