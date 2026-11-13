"""桌面版的資料解譯器與萬用字元聯集（tmig.authoring）：Excel 的工作表上面有標題、說明的列時以資料解譯器
找出標題列；萬用字元聯集把資料夾中檔名符合模式的檔案接在資料表下面。匯入後以 DuckDB 查詢比對。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from tmig.authoring import (
    WildcardMatch,
    add_wildcard_union,
    inspect_file,
    interpret_header,
    new_workbook,
    set_interpreter,
)
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_sources
from tmig.edit import EditError, replace_worksheet
from tmig.ir import IR, FileConnection, UnionRelation, UnionWildcard
from tmig.ir.integrity import check_references
from tmig.sqlgen.query import worksheet_sql


def messy(path: Path) -> Path:
    """第 1 列是報表標題、第 2 列空白、第 3 列是說明，第 4 列才是標題列（C 欄沒有標題），之後有空白的列。"""
    book = Workbook()
    ws = book.active
    assert ws is not None
    ws.title = "Sales"
    ws["A1"] = "Quarterly sales report"
    ws["A3"] = "All figures in USD"
    for col, value in enumerate(["Region", "Year", None, "Sales"], 1):
        ws.cell(4, col, value)
    for row, values in enumerate(
        [
            ("East", 2023, None, 100.5),
            ("West", 2023, None, 200.0),
            (None, None, None, None),
            ("East", 2024, None, 50.25),
        ],
        5,
    ):
        for col, value in enumerate(values, 1):
            if value is not None:
                ws.cell(row, col, value)
    book.save(path)
    return path


def ids(ir: IR) -> dict[str, str]:
    return {f.caption: f.id for f in ir.datasources[0].fields}


def query(ir: IR, folder: Path, rows: str, measure: str) -> dict[Any, Any]:
    assert check_references(ir) == []
    ds = ir.datasources[0]
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        datasource=ds.id, rows=[{"field": rows}], columns=[{"field": measure, "agg": "sum"}]
    )
    ir, ws = replace_worksheet(ir, sheet)
    (result,) = import_sources(folder / "sources", folder / "data", ir, only=[ds.id])
    assert result.ok, result.to_json()
    found = execute_query(folder / "data" / f"{ds.id}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in found.rows}


def test_interpret_header() -> None:
    grid: list[list[Any]] = [
        [None, None, "NEW PC REGISTRATIONS", None],
        [None, None, None, None],
        ["REGIONS", "COUNTRIES", "2005", 2006],
        [None, None, None, None],
        ["EUROPE", "AUSTRIA", 307915, 308594],
    ]
    assert interpret_header(grid) == 2
    # 一般的工作表：第一列就是標題列
    assert interpret_header([["Region", "Sales"], ["East", 1.5]]) == 0
    # 只有數字（不是標題）
    assert interpret_header([[1.5, 2.5], [3.5, 4.5]]) is None


def test_data_interpreter(tmp_path: Path) -> None:
    book = messy(tmp_path / "Report.xlsx")
    (tmp_path / "sources" / "Data").mkdir(parents=True)
    (tmp_path / "sources" / "Data" / "Report.xlsx").write_bytes(book.read_bytes())
    plain = inspect_file(book)
    assert [c.name for c in plain.tables[0].columns][:1] == ["Quarterly sales report"]
    cleaned = inspect_file(book, interpreter=True)
    (table,) = cleaned.tables
    assert table.interpreted and table.rows == 3
    assert [(c.name, c.datatype) for c in table.columns] == [
        ("Region", "string"),
        ("Year", "integer"),
        ("F3", "string"),
        ("Sales", "real"),
    ]
    # 不使用時建立的報表，再切換成使用資料解譯器
    ir = new_workbook("Report", plain, "Sales", "Data/Report.xlsx")
    ir = set_interpreter(ir, ir.datasources[0].id, "Data/Report.xlsx", cleaned)
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection) and connection.tables[0].interpreted
    f = ids(ir)
    assert "Quarterly sales report" not in f
    assert query(ir, tmp_path, f["Region"], f["Sales"]) == {"East": 150.75, "West": 200.0}
    # 再切換回來：沒有用到的欄位換回原本的欄
    ir = set_interpreter(ir, ir.datasources[0].id, "Data/Report.xlsx", plain)
    assert "Quarterly sales report" in ids(ir)
    with pytest.raises(EditError, match="沒有 Excel 檔"):
        set_interpreter(ir, ir.datasources[0].id, "Data/Other.xlsx", plain)


def orders(path: Path, rows: list[tuple[str, float]], note: bool = False) -> Path:
    book = Workbook()
    ws = book.active
    assert ws is not None
    ws.title = "Orders"
    ws.append(["Region", "Sales", *(["Note"] if note else [])])
    for region, sales in rows:
        ws.append([region, sales, *(["late"] if note else [])])
    book.save(path)
    return path


def test_wildcard_union(tmp_path: Path) -> None:
    folder = tmp_path / "author"
    folder.mkdir()
    first = orders(folder / "Sales 2023.xlsx", [("East", 10), ("West", 20)])
    second = orders(folder / "Sales 2024.xlsx", [("East", 5)], note=True)
    data = tmp_path / "sources" / "Data"
    data.mkdir(parents=True)
    for path in (first, second):
        (data / path.name).write_bytes(path.read_bytes())
    ir = new_workbook("Sales", inspect_file(first), "Orders", "Data/Sales 2023.xlsx")
    ds = ir.datasources[0].id
    ir = add_wildcard_union(
        ir,
        ds,
        "Orders",
        [WildcardMatch(inspect_file(second), "Orders", "Data/Sales 2024.xlsx", str(second))],
        UnionWildcard(files="Sales *.xlsx"),
        original=str(first),
    )
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection)
    union = connection.relation
    assert isinstance(union, UnionRelation) and union.wildcard is not None
    assert [p.path for p in union.parts] == [str(first), str(second)]
    f = ids(ir)
    assert query(ir, tmp_path, f["Path"], f["Sales"]) == {str(first): 30, str(second): 5}
    assert query(ir, tmp_path, f["Note"], f["Sales"]) == {None: 30, "late": 5}
    with pytest.raises(EditError, match="沒有符合的檔案"):
        add_wildcard_union(ir, ds, "Orders", [], UnionWildcard(files="Nothing*"))
