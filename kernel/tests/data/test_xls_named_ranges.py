"""舊版 Excel（.xls）的具名範圍：與 .xlsx 相同，連線畫面列為資料表，資料是它所在工作表的那一塊範圍（範圍以外的
列與欄不算）；匯入後以 DuckDB 查詢比對。測試用的 .xls 由 xls_book 寫出（沒有寫 .xls 的套件）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import xlrd

from tmig.authoring import inspect_file, new_workbook
from tmig.data.executor import execute_query
from tmig.data.file_import import xls_named_ranges
from tmig.data.hyper_import import import_sources
from tmig.edit import replace_worksheet
from tmig.ir.integrity import check_references
from tmig.sqlgen.query import worksheet_sql

from .xls_book import biff8, xls_bytes

SHEETS: dict[str, list[list[Any]]] = {
    "Sheet1": [
        ["Notes above the table", None, None],
        [None, None, None],
        [None, "Region", "Sales"],
        [None, "East", 100.5],
        [None, "West", 200.0],
        [None, "South", 75.0],
        [None, None, None],
        [None, "Total", 375.5],
    ],
    "Lookup": [["Code", "Name"], ["E", "East"]],
}
NAMES = {
    "SalesData": (0, 2, 5, 1, 2),  # Sheet1!$B$3:$C$6
    "Lookup": (1, 0, 1, 0, 1),  # 名稱與工作表相同：不另外列
}


def book(tmp_path: Path) -> Path:
    path = tmp_path / "Ranges.xls"
    path.write_bytes(xls_bytes(SHEETS, NAMES))
    return path


def test_xls_named_ranges_are_read() -> None:
    """活頁簿層級的具名範圍 → 工作表與範圍（A1 的寫法）；不是一塊參照的名稱不算。"""
    found = xls_named_ranges(xlrd.open_workbook(file_contents=xls_bytes(SHEETS, NAMES)))
    assert found == {"SalesData": ("Sheet1", "B3:C6"), "Lookup": ("Lookup", "A1:B2")}
    # 沒有包在複合文件中的 BIFF 資料流（很舊的檔案）也一樣
    plain = xlrd.open_workbook(file_contents=biff8(SHEETS, {"Only": (1, 1, 1, 0, 1)}))
    assert xls_named_ranges(plain) == {"Only": ("Lookup", "A2:B2")}


def test_xls_named_range_is_a_table(tmp_path: Path) -> None:
    """連線畫面列出具名範圍（名稱與工作表相同的不列）；以它建立的資料源匯入那一塊範圍的資料。"""
    path = book(tmp_path)
    inspected = inspect_file(path)
    assert [t.name for t in inspected.tables] == ["Sheet1", "Lookup", "SalesData"]
    table = inspected.table("SalesData")
    assert (table.rows, table.sheet) == (3, "SalesData")
    assert [(c.name, c.datatype) for c in table.columns] == [
        ("Region", "string"),
        ("Sales", "real"),
    ]

    (tmp_path / "sources" / "Data").mkdir(parents=True)
    (tmp_path / "sources" / "Data" / "Ranges.xls").write_bytes(path.read_bytes())
    ir = new_workbook("Ranges", inspected, "SalesData", "Data/Ranges.xls")
    assert check_references(ir) == []
    ds = ir.datasources[0]
    f = {field.caption: field.id for field in ds.fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        datasource=ds.id,
        rows=[{"field": f["Region"]}],
        columns=[{"field": f["Sales"], "agg": "sum"}],
    )
    ir, ws = replace_worksheet(ir, sheet)
    (result,) = import_sources(tmp_path / "sources", tmp_path / "data", ir, only=[ds.id])
    assert result.ok, result.to_json()
    found = execute_query(tmp_path / "data" / f"{ds.id}.duckdb", worksheet_sql(ir, ws.id))
    # 範圍以外的 Total 那一列不算
    assert {row[0]: row[1] for row in found.rows} == {"East": 100.5, "South": 75.0, "West": 200.0}
