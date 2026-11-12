"""建立資料源與編輯資料模型（tmig.authoring，桌面版的「連線到資料」與資料源分頁的編輯）：檢查檔案的
型別、新報表與資料源的欄位、加入資料表（關聯、join、聯集）、樞紐、欄位的設定；每一步匯入後以 DuckDB
查詢，與手算的結果比對。"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from tmig.authoring import (
    add_datasource,
    add_table,
    inspect_file,
    new_workbook,
    pivot,
    remove_table,
    set_join,
    set_relationship,
    unpivot,
    update_field,
)
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_sources
from tmig.edit import EditError, replace_worksheet
from tmig.ir import (
    IR,
    FileConnection,
    JoinRelation,
    ModelRelation,
    PivotRelation,
    TableRelation,
    UnionRelation,
)
from tmig.ir.integrity import check_references
from tmig.sqlgen.query import worksheet_sql

PACKAGE = "Data/Sales.xlsx"


def write_book(path: Path) -> Path:
    book = Workbook()
    orders = book.active
    assert orders is not None
    orders.title = "Orders"
    orders.append(
        ["Order ID", "Region", "Order Date", "Shipped", "Sales", "Quantity", "Rush", None]
    )
    orders.append(
        ["O1", "East", dt.datetime(2024, 1, 5), dt.datetime(2024, 1, 6, 9, 30), 100.5, 2, True]
    )
    orders.append(
        ["O2", "West", dt.datetime(2024, 2, 1), dt.datetime(2024, 2, 3, 14), 200, 1, False]
    )
    orders.append(["O3", "East", dt.datetime(2024, 3, 9), None, 50.25, 3, False])
    orders.append(["O4", "South", dt.datetime(2024, 4, 2), dt.datetime(2024, 4, 2, 8), 75, 1, True])
    people = book.create_sheet("People")
    people.append(["Region", "Manager", "Area"])
    for row in (["East", "Ann", "West"], ["West", "Bob", "East"], ["North", "Cid", "South"]):
        people.append(row)
    returns = book.create_sheet("Returns")
    returns.append(["Order ID", "Returned"])
    returns.append(["O1", "Yes"])
    returns.append(["O4", "Yes"])
    later = book.create_sheet("Orders 2025")
    later.append(["Order ID", "Region", "Order Date", "Sales", "Note"])
    later.append(["P1", "North", dt.datetime(2025, 1, 1), 7, "new"])
    book.create_sheet("Blank")  # 空白的工作表不列出
    book.save(path)
    return path


@pytest.fixture
def book(tmp_path: Path) -> Path:
    return write_book(tmp_path / "Sales.xlsx")


@pytest.fixture
def sources(tmp_path: Path, book: Path) -> Path:
    """報表的 sources 資料夾：檔案在 Data/（與 FileTable.path 相同）。"""
    folder = tmp_path / "sources"
    (folder / "Data").mkdir(parents=True)
    shutil.copy(book, folder / PACKAGE)
    return folder


def ids(ir: IR, ds: str | None = None) -> dict[str, str]:
    found = next(d for d in ir.datasources if ds is None or d.id == ds)
    return {f.caption: f.id for f in found.fields}


def place(ir: IR, rows: str, measure: str, agg: str = "sum", ds: str | None = None) -> IR:
    """工作表 1 放上 rows（維度）與 agg(measure)。"""
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        datasource=ds or ir.datasources[0].id,
        rows=[{"field": rows}],
        columns=[{"field": measure, "agg": agg}],
    )
    return replace_worksheet(ir, sheet)[0]


def query(
    ir: IR,
    sources: Path,
    out: Path,
    rows: str,
    measure: str,
    agg: str = "sum",
    ds: str | None = None,
) -> dict[Any, Any]:
    """工作表 1 放上 rows 與 agg(measure)，匯入後查詢（第一欄 → 第二欄）。"""
    datasource = ds or ir.datasources[0].id
    assert check_references(ir) == []
    ir = place(ir, rows, measure, agg, datasource)
    ws = ir.worksheets[0]
    if out.exists():
        shutil.rmtree(out)
    results = import_sources(sources, out, ir, only=[datasource])
    assert all(r.ok for r in results), [r.missing_columns for r in results]
    result = execute_query(out / f"{datasource}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in result.rows}


def clear(ir: IR) -> IR:
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[], columns=[])
    return replace_worksheet(ir, sheet)[0]


def test_inspect_excel(book: Path) -> None:
    inspected = inspect_file(book)
    assert inspected.format == "excel"
    assert [t.name for t in inspected.tables] == ["Orders", "People", "Returns", "Orders 2025"]
    orders = inspected.table("Orders")
    assert (orders.rows, orders.sheet) == (4, "Orders")
    # 尾端沒有標題、也沒有值的欄不列出；日期沒有時間為 date，有時間為 datetime
    assert [(c.name, c.datatype) for c in orders.columns] == [
        ("Order ID", "string"),
        ("Region", "string"),
        ("Order Date", "date"),
        ("Shipped", "datetime"),
        ("Sales", "real"),
        ("Quantity", "integer"),
        ("Rush", "boolean"),
    ]
    # Excel 依儲存格的型別：文字的 Yes 是字串（只有 TRUE／FALSE 的布林儲存格是布林值）
    assert [c.datatype for c in inspected.table("Returns").columns] == ["string", "string"]
    with pytest.raises(EditError, match="沒有資料表"):
        inspected.table("Missing")


def test_inspect_text(tmp_path: Path) -> None:
    # Big5（cp950）、分號分隔：與 Tableau 相同自動判斷
    path = tmp_path / "targets.csv"
    path.write_bytes(
        "地區;目標;日期;啟用\n東部;1234.5;2024-01-05;TRUE\n西部;7;2024-02-01;FALSE\n".encode(
            "cp950"
        )
    )
    inspected = inspect_file(path)
    assert (inspected.format, inspected.separator, inspected.charset) == ("text", ";", "cp950")
    (table,) = inspected.tables
    assert (table.name, table.rows) == ("targets.csv", 2)
    assert [(c.name, c.datatype) for c in table.columns] == [
        ("地區", "string"),
        ("目標", "real"),
        ("日期", "date"),
        ("啟用", "boolean"),
    ]
    with pytest.raises(EditError, match="找不到檔案"):
        inspect_file(tmp_path / "nothing.csv")
    (tmp_path / "notes.docx").write_bytes(b"x")
    with pytest.raises(EditError, match="不認得的檔案類型"):
        inspect_file(tmp_path / "notes.docx")


def test_new_workbook_fields(tmp_path: Path) -> None:
    path = tmp_path / "stores.csv"
    path.write_text(
        "City,Country,Latitude,Longitude,Postal Code,Sales\n"
        "Taipei,Taiwan,25.03,121.56,100,5.5\nTainan,Taiwan,22.99,120.21,700,3\n",
        encoding="utf-8",
    )
    ir = new_workbook("Stores", inspect_file(path), None, "Data/stores.csv")
    assert check_references(ir) == []
    (ds,) = ir.datasources
    assert ds.caption == "stores.csv" and isinstance(ds.connection, FileConnection)
    assert [w.datasource for w in ir.worksheets] == [ds.id]
    fields = {f.caption: f for f in ds.fields}
    # 與 Tableau 相同：數字為量值（緯度、經度預設平均），有地理角色的郵遞區號是維度
    assert (fields["City"].role, fields["City"].geo_role) == ("dimension", "city")
    assert (fields["Country"].role, fields["Country"].geo_role) == ("dimension", "country")
    latitude = fields["Latitude"]
    assert (latitude.role, latitude.geo_role, latitude.default_agg) == (
        "measure",
        "latitude",
        "avg",
    )
    assert fields["Longitude"].default_agg == "avg"
    postal = fields["Postal Code"]
    assert (postal.datatype, postal.role, postal.geo_role, postal.default_agg) == (
        "integer",
        "dimension",
        "zip",
        None,
    )
    assert (fields["Sales"].role, fields["Sales"].default_agg) == ("measure", "sum")


def test_relationship_and_join(book: Path, sources: Path, tmp_path: Path) -> None:
    out = tmp_path / "data"
    source = inspect_file(book)
    ir = new_workbook("Sales", source, "Orders", PACKAGE)
    ds = ir.datasources[0].id
    assert ir.datasources[0].caption == "Orders (Sales)"
    f = ids(ir)
    assert query(ir, sources, out, f["Region"], f["Sales"]) == {
        "East": 150.75,
        "West": 200,
        "South": 75,
    }
    # 關聯：People 成為另一張邏輯資料表；同名的 Region 改為「Region (People)」
    ir = add_table(
        ir, ds, source, "People", PACKAGE, "relationship", on=[(f["Region"], "=", "Region")]
    )
    model = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(model, ModelRelation) and len(model.tables) == 2
    f = ids(ir)
    assert "Region (People)" in f
    # 沒有對應的訂單（South）在 Null 之下，沒有訂單的經理（Cid）也列出（Tableau 關聯的語意）
    assert query(ir, sources, out, f["Manager"], f["Sales"]) == {
        "Ann": 150.75,
        "Bob": 200,
        "Cid": None,
        None: 75,
    }
    # 改變關聯的條件：Region = Area
    people = model.tables[1].id
    ir = set_relationship(ir, ds, people, [(f["Region"], "=", f["Area"])])
    assert query(ir, sources, out, f["Manager"], f["Sales"]) == {
        "Ann": 200,
        "Bob": 150.75,
        "Cid": 75,
    }
    ir = set_relationship(ir, ds, people, [(f["Region"], "=", f["Region (People)"])])
    with pytest.raises(EditError, match="不是這個關聯兩邊"):
        set_relationship(ir, ds, people, [(f["Manager"], "=", f["Region"])])
    # 邏輯資料表 Orders 中 inner join Returns：只有退貨的訂單
    orders = model.tables[0].id
    ir = add_table(
        ir,
        ds,
        source,
        "Returns",
        PACKAGE,
        "join",
        target=orders,
        on=[(f["Order ID"], "=", "Order ID")],
    )
    model = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(model, ModelRelation) and isinstance(model.tables[0].relation, JoinRelation)
    f = ids(ir)
    assert query(ir, sources, out, f["Manager"], f["Sales"]) == {
        "Ann": 100.5,
        "Bob": None,
        "Cid": None,
        None: 75,
    }
    ir = set_join(ir, ds, "Returns", "left", [(f["Order ID"], "=", "Order ID")])
    assert query(ir, sources, out, f["Returned"], f["Sales"]) == {"Yes": 175.5, None: 250.25}
    with pytest.raises(EditError, match="沒有欄位"):
        set_join(ir, ds, "Returns", "left", [(f["Order ID"], "=", "Missing")])
    # 工作表用到的欄位所在的資料表不能移除；不用之後移除，回到兩張邏輯資料表
    with pytest.raises(EditError, match="用到"):
        remove_table(place(ir, f["Returned"], f["Sales"]), ds, "Returns")
    ir = remove_table(clear(ir), ds, "Returns")
    model = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(model, ModelRelation) and isinstance(model.tables[0].relation, TableRelation)
    assert "Returned" not in ids(ir)
    ir = remove_table(ir, ds, people)
    assert isinstance(ir.datasources[0].connection.relation, TableRelation)  # type: ignore[union-attr]
    assert "Manager" not in ids(ir)
    f = ids(ir)
    assert query(ir, sources, out, f["Region"], f["Sales"]) == {
        "East": 150.75,
        "West": 200,
        "South": 75,
    }


def test_union_pivot_and_fields(book: Path, sources: Path, tmp_path: Path) -> None:
    out = tmp_path / "data"
    source = inspect_file(book)
    ir = new_workbook("Sales", source, "Orders", PACKAGE)
    ds = ir.datasources[0].id
    # 聯集：欄位依名稱對應，Orders 2025 沒有的欄為 Null，另有 Sheet 與 Table Name
    ir = add_table(ir, ds, source, "Orders 2025", PACKAGE, "union")
    assert isinstance(ir.datasources[0].connection.relation, UnionRelation)  # type: ignore[union-attr]
    f = ids(ir)
    assert query(ir, sources, out, f["Sheet"], f["Sales"]) == {"Orders": 425.75, "Orders 2025": 7}
    assert query(ir, sources, out, f["Note"], f["Quantity"]) == {None: 7, "new": None}
    ir = remove_table(clear(ir), ds, "Orders 2025")
    assert isinstance(ir.datasources[0].connection.relation, TableRelation)  # type: ignore[union-attr]
    assert {"Sheet", "Table Name", "Note"}.isdisjoint(ids(ir))
    # 樞紐：Sales 與 Quantity 轉成列（Pivot Field Names、Pivot Field Values）
    ir = pivot(ir, ds, "Orders", ["Sales", "Quantity"])
    assert isinstance(ir.datasources[0].connection.relation, PivotRelation)  # type: ignore[union-attr]
    f = ids(ir)
    assert "Sales" not in f
    assert query(ir, sources, out, f["Pivot Field Names"], f["Pivot Field Values"]) == {
        "Sales": 425.75,
        "Quantity": 7,
    }
    with pytest.raises(EditError, match="用到"):
        unpivot(place(ir, f["Pivot Field Names"], f["Pivot Field Values"]), ds, "Pivot")
    ir = unpivot(clear(ir), ds, "Pivot")
    f = ids(ir)
    assert "Sales" in f and "Pivot Field Names" not in f
    # 欄位：改名、隱藏；名稱不能重複；改資料類型後重新匯入（Quantity 成為文字的維度）
    ir = update_field(ir, ds, f["Quantity"], caption="數量", hidden=True)
    renamed = next(x for x in ir.datasources[0].fields if x.id == f["Quantity"])
    assert (renamed.caption, renamed.hidden) == ("數量", True)
    with pytest.raises(EditError, match="已經有名為"):
        update_field(ir, ds, f["Quantity"], caption="sales")
    ir = update_field(ir, ds, f["Quantity"], datatype="string")
    quantity = next(x for x in ir.datasources[0].fields if x.id == f["Quantity"])
    assert (quantity.datatype, quantity.role, quantity.default_agg) == ("string", "dimension", None)
    assert query(ir, sources, out, f["Quantity"], f["Sales"]) == {"1": 275, "2": 100.5, "3": 50.25}
    # 角色：文字改成量值時預設計數
    ir = update_field(clear(ir), ds, f["Region"], role="measure")
    region = next(x for x in ir.datasources[0].fields if x.id == f["Region"])
    assert (region.role, region.default_agg) == ("measure", "count")
    with pytest.raises(EditError, match="只有量值"):
        update_field(ir, ds, f["Order ID"], default_agg="sum")


def test_add_datasource(book: Path, sources: Path, tmp_path: Path) -> None:
    ir = new_workbook("Sales", inspect_file(book), "Orders", PACKAGE)
    csv = tmp_path / "targets.csv"
    csv.write_text("Region,Target\nEast,300\nWest,150\n", encoding="utf-8")
    shutil.copy(csv, sources / "Data" / "targets.csv")
    ir, added = add_datasource(ir, inspect_file(csv), None, "Data/targets.csv")
    assert [d.caption for d in ir.datasources] == ["Orders (Sales)", "targets.csv"]
    # 兩個資料源的欄位 id 不重複
    first, second = (set(ids(ir, d.id).values()) for d in ir.datasources)
    assert first.isdisjoint(second)
    f = ids(ir, added.id)
    assert query(ir, sources, tmp_path / "data", f["Region"], f["Target"], ds=added.id) == {
        "East": 300,
        "West": 150,
    }
