"""資料解譯器（Data Interpreter）與萬用字元聯集（Wildcard union）：解析、匯入與查詢。

- 資料解譯器：XML 依公開報表（2019 的 Personal Vehicle Sales，connection cleaning='yes'、
  gridOrigin='A2:O150:no:A2:O150:1'）：範圍從第 2 列起，上面有標題與說明的列，第 5 列是標題列（空白的
  標題為 F3、F4、F5），之後有一列空白
- 萬用字元聯集：XML 依公開報表（2020.4 的 Superstore wildcard union，type='batch-union'、
  database-filter 的模式 Superstore - *.xlsx、columns 另有 Path 與 Sheet）
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from tmig.data.executor import execute_query
from tmig.data.hyper_import import HyperImportError, import_workbook
from tmig.ir import IR, FileConnection, UnionRelation
from tmig.ir.integrity import check_references
from tmig.parser.workbook import NoDatasourceError, parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b

TYPES = {"string": 130, "real": 5, "integer": 20}


def record(name: str, datatype: str, parent: str, ordinal: int) -> str:
    agg = "Sum" if datatype in ("real", "integer") else "Count"
    return f"""<metadata-record class='column'>
      <remote-name>{name}</remote-name><remote-type>{TYPES[datatype]}</remote-type>
      <local-name>[{name}]</local-name><parent-name>[{parent}]</parent-name>
      <remote-alias>{name}</remote-alias><ordinal>{ordinal}</ordinal>
      <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
    </metadata-record>"""


def sheet(name: str, ds: str, dim: str, measure: str) -> str:
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources><datasource name='{ds}' /></datasources>
          <datasource-dependencies datasource='{ds}'>
            <column-instance column='[{dim}]' derivation='None' name='[none:{dim}:nk]' pivot='key' type='nominal' />
            <column-instance column='[{measure}]' derivation='Sum' name='[sum:{measure}:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Bar' /></pane></panes>
        <rows>[{ds}].[none:{dim}:nk]</rows>
        <cols>[{ds}].[sum:{measure}:qk]</cols>
      </table>
    </worksheet>"""


def rows(ir: IR, data: Path, name: str) -> dict[Any, Any]:
    ws = next(w for w in ir.worksheets if w.name == name)
    result = execute_query(data / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in result.rows}


# ---------------------------------------------------------------- 資料解譯器


def interpreted_book(path: Path) -> None:
    """與公開報表的來源相同的版面：第 2、4 列是標題與說明，第 5 列是標題列，第 6 列空白。"""
    book = Workbook()
    ws = book.active
    assert ws is not None
    ws.title = "pc_sales"
    ws.cell(2, 4, "NEW PC REGISTRATIONS OR SALES")
    ws.cell(4, 4, "Estimated figures")
    for col, value in enumerate(["REGIONS", "COUNTRIES", None, "2005", 2006], 1):
        ws.cell(5, col, value)
    data = [
        ("EUROPE", "AUSTRIA", 307915, 308594),
        ("EUROPE", "BELGIUM", 480088, 526141),
        ("AFRICA", "MOROCCO", 56183, 75295),
    ]
    for r, (region, country, a, b2) in enumerate(data, 7):
        ws.cell(r, 1, region)
        ws.cell(r, 2, country)
        ws.cell(r, 4, a)
        ws.cell(r, 5, b2)
    book.save(path)


def interpreter_datasource() -> str:
    cols = [
        ("REGIONS", "string"),
        ("COUNTRIES", "string"),
        ("F3", "real"),
        ("2005", "integer"),
        ("2006", "integer"),
    ]
    columns = "".join(
        f"<column datatype='{t}' name='{n}' ordinal='{i}' />" for i, (n, t) in enumerate(cols)
    )
    records = "".join(record(n, t, "pc_sales", i) for i, (n, t) in enumerate(cols))
    return f"""
    <datasource caption='Sales' inline='true' name='federated.sales' version='18.1'>
      <connection class='federated'>
        <named-connections><named-connection caption='Book' name='excel-direct.book'>
          <connection class='excel-direct' cleaning='yes' compat='no' filename='Data/Book.xlsx'
            interpretationMode='1' validate='no' /></named-connection></named-connections>
        <relation connection='excel-direct.book' name='pc_sales' table='[pc_sales$]' type='table'>
          <columns gridOrigin='A2:E9:no:A2:E9:1' header='yes' lost='0' outcome='1' processed='20'>{columns}</columns>
        </relation>
        <metadata-records>{records}</metadata-records>
      </connection>
      <aliases enabled='yes' />
    </datasource>"""


def test_data_interpreter(tmp_path: Path) -> None:
    book = tmp_path / "Book.xlsx"
    interpreted_book(book)
    path = tmp_path / "interpreter.twbx"
    xml = b.workbook(
        interpreter_datasource(),
        worksheets=sheet("By Region", "federated.sales", "REGIONS", "2005"),
        params=False,
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("interpreter.twb", xml)
        archive.write(book, "Data/Book.xlsx")
    ir = parse_workbook(path)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert check_references(ir) == []
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection)
    (table,) = connection.tables
    assert table.interpreted and table.cells == "A2:E9"
    out = tmp_path / "data"
    results = import_workbook(path, out, ir)
    assert all(r.ok for r in results), [r.to_json() for r in results]
    # 標題、說明與空白的列不算：三列資料
    assert rows(ir, out, "By Region") == {"EUROPE": 307915 + 480088, "AFRICA": 56183}


def test_data_interpreter_without_header(tmp_path: Path) -> None:
    """記下的欄名在檔案中找不到時：匯入失敗並說明，不默默讀錯列。"""
    book = tmp_path / "Book.xlsx"
    other = Workbook()
    ws = other.active
    assert ws is not None
    ws.title = "pc_sales"
    ws.append(["nothing", "here"])
    other.save(book)
    path = tmp_path / "interpreter.twbx"
    xml = b.workbook(
        interpreter_datasource(),
        worksheets=sheet("By Region", "federated.sales", "REGIONS", "2005"),
        params=False,
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("interpreter.twb", xml)
        archive.write(book, "Data/Book.xlsx")
    ir = parse_workbook(path)
    with pytest.raises(HyperImportError, match="資料解譯器的標題列"):
        import_workbook(path, tmp_path / "data", ir)


# ---------------------------------------------------------------- 萬用字元聯集


def superstore(
    path: Path, sheet_name: str, rows_: list[tuple[str, str, float]], extra: bool = False
) -> None:
    book = Workbook()
    ws = book.active
    assert ws is not None
    ws.title = sheet_name
    header = ["Order ID", "Region", "Sales"] + (["Note"] if extra else [])
    ws.append(header)
    for row in rows_:
        ws.append([*row, "late"] if extra else list(row))
    book.save(path)


def wildcard_datasource(pattern: str = "Superstore - *.xlsx") -> str:
    cols = [
        ("Order ID", "string"),
        ("Region", "string"),
        ("Sales", "real"),
        ("Note", "string"),
        ("Path", "string"),
        ("Sheet", "string"),
    ]
    columns = "".join(f"<column datatype='{t}' name='{n}' />" for n, t in cols)
    records = "".join(record(n, t, "2016+", i) for i, (n, t) in enumerate(cols))
    return f"""
    <datasource caption='Orders' inline='true' name='federated.orders' version='18.1'>
      <connection class='federated'>
        <named-connections><named-connection caption='Superstore - 2016' name='excel-direct.w'>
          <connection class='excel-direct' cleaning='no' compat='no'
            filename='C:/Users/amy/Wildcard Union/Superstore - 2016.xlsx' interpretationMode='0'
            validate='no' /></named-connection></named-connections>
        <relation all='true' connection='excel-direct.w' include-siblings='true' is-recursive='false'
          name='2016+' path='' type='batch-union'>
          <database-filter><expression op='='><expression op='{pattern}' /></expression></database-filter>
          <table-filter />
          <columns header='yes'>{columns}</columns>
        </relation>
        <metadata-records>{records}</metadata-records>
      </connection>
      <aliases enabled='yes' />
    </datasource>"""


@pytest.fixture
def wildcard_twbx(tmp_path: Path) -> Path:
    folder = tmp_path / "src"
    folder.mkdir()
    superstore(folder / "2016.xlsx", "Orders", [("O1", "East", 10), ("O2", "West", 20)])
    superstore(folder / "2017.xlsx", "Orders", [("O3", "East", 5)], extra=True)
    superstore(folder / "other.xlsx", "Orders", [("X", "North", 1000)])
    path = tmp_path / "wildcard.twbx"
    xml = b.workbook(
        wildcard_datasource(),
        worksheets=sheet("By Region", "federated.orders", "Region", "Sales")
        + sheet("By Path", "federated.orders", "Path", "Sales")
        + sheet("By Note", "federated.orders", "Note", "Sales"),
        params=False,
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("wildcard.twb", xml)
        archive.write(folder / "2016.xlsx", "Data/Wildcard Union/Superstore - 2016.xlsx")
        archive.write(folder / "2017.xlsx", "Data/Wildcard Union/Superstore - 2017.xlsx")
        archive.write(folder / "other.xlsx", "Data/Wildcard Union/Other.xlsx")
    return path


def test_wildcard_union(wildcard_twbx: Path, tmp_path: Path) -> None:
    ir = parse_workbook(wildcard_twbx)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert check_references(ir) == []
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection)
    union = connection.relation
    assert isinstance(union, UnionRelation)
    assert union.wildcard is not None and union.wildcard.files == "Superstore - *.xlsx"
    # 符合模式的兩個檔案（Other.xlsx 不算）
    assert [t.path for t in connection.tables] == [
        "Data/Wildcard Union/Superstore - 2016.xlsx",
        "Data/Wildcard Union/Superstore - 2017.xlsx",
    ]
    assert [p.sheet for p in union.parts] == ["Orders", "Orders"]
    out = tmp_path / "data"
    results = import_workbook(wildcard_twbx, out, ir)
    assert all(r.ok for r in results), [r.to_json() for r in results]
    assert rows(ir, out, "By Region") == {"East": 15, "West": 20}
    # Path：檔案在作者電腦上的路徑；2016 沒有 Note 欄（Null）
    assert rows(ir, out, "By Path") == {
        "C:/Users/amy/Wildcard Union/Superstore - 2016.xlsx": 30,
        "C:/Users/amy/Wildcard Union/Superstore - 2017.xlsx": 5,
    }
    assert rows(ir, out, "By Note") == {None: 30, "late": 5}


def test_wildcard_union_without_matches(tmp_path: Path) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    superstore(folder / "2016.xlsx", "Orders", [("O1", "East", 10)])
    path = tmp_path / "wildcard.twbx"
    xml = b.workbook(
        wildcard_datasource("Nothing *.xlsx"),
        worksheets=sheet("By Region", "federated.orders", "Region", "Sales"),
        params=False,
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("wildcard.twb", xml)
        archive.write(folder / "2016.xlsx", "Data/Wildcard Union/Superstore - 2016.xlsx")
    with pytest.raises(NoDatasourceError, match="找不到符合的檔案"):
        parse_workbook(path)
