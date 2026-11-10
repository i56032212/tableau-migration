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
