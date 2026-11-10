"""檔案型資料源（沒有萃取的 Excel、文字檔）：解析、匯入 DuckDB 與查詢。

測試用的 .twbx 內有一個 Excel 檔（Orders、Returns 兩張工作表）與一個德文地區設定、windows-1252 編碼、
以分號分隔的 CSV；資料源包含單一工作表、兩張工作表的 join、Relationship 模型與文字檔。
"""

from __future__ import annotations

import datetime as dt
import json
import zipfile
from pathlib import Path
from typing import Any

import duckdb
import pytest
from openpyxl import Workbook

from tmig.census import run_census
from tmig.data.executor import execute_query
from tmig.data.file_import import convert
from tmig.data.hyper_import import import_workbook
from tmig.ir import IR, FileConnection, JoinRelation, ModelRelation, TableRelation
from tmig.ir.integrity import check_references
from tmig.parser.workbook import parse_workbook
from tmig.render.datasource import datasource_summary
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b

ORDERS = [
    ("Order ID", "string"),
    ("Region", "string"),
    ("Order Date", "date"),
    ("Sales", "real"),
    ("Quantity", "integer"),
    ("Postal Code", "string"),
    ("Shipped", "boolean"),
]
RETURNS = [("Order ID", "string"), ("Returned", "string")]
TARGETS = [("Region", "string"), ("Target", "real"), ("Month", "date"), ("Note", "string")]
TYPES = {"string": 130, "real": 5, "integer": 20, "date": 7, "boolean": 11}


def write_book(path: Path) -> None:
    book = Workbook()
    orders = book.active
    assert orders is not None
    orders.title = "Orders"
    orders.append([name for name, _ in ORDERS])
    orders.append(["O1", "East", dt.datetime(2024, 1, 15), 100.5, 2, 10024, True])
    orders.append(["O2", "West", dt.datetime(2024, 2, 1), 200.0, 1, "02134", False])
    orders.append(["O3", "East", dt.datetime(2024, 3, 10), 50.25, 3, 98101, True])
    # 空白的地區、文字的金額（不是數字，成為 Null）
    orders.append(["O4", None, dt.datetime(2024, 3, 11), "n/a", 1, None, None])
    returns = book.create_sheet("Returns")
    returns.append([name for name, _ in RETURNS])
    returns.append(["O1", "Yes"])
    returns.append(["O3", "Yes"])
    book.save(path)


CSV = "Region;Target;Month;Note\r\nEast;1.234,5;15.01.2024;Über\r\nWest;2.000;01.02.2024;\r\n"


def columns_xml(columns: list[tuple[str, str]], attrs: str) -> str:
    items = "".join(
        f"<column datatype='{t}' name='{n}' ordinal='{i}' />" for i, (n, t) in enumerate(columns)
    )
    return f"<columns {attrs}>{items}</columns>"


def records(columns: list[tuple[str, str]], parent: str, object_id: str = "") -> str:
    found = []
    for i, (name, datatype) in enumerate(columns):
        # 與 Orders 同名的欄位，Tableau 在名稱後加上資料表名稱
        local = f"{name} ({parent})" if parent == "Returns" and name == "Order ID" else name
        found.append(
            f"""<metadata-record class='column'>
              <remote-name>{name}</remote-name><remote-type>{TYPES[datatype]}</remote-type>
              <local-name>[{local}]</local-name><parent-name>[{parent}]</parent-name>
              <remote-alias>{name}</remote-alias><ordinal>{i}</ordinal>
              <local-type>{datatype}</local-type>
              <aggregation>{"Sum" if datatype in ("real", "integer") else "Count"}</aggregation>
              {f"<object-id>[{object_id}]</object-id>" if object_id else ""}
            </metadata-record>"""
        )
    return "".join(found)


EXCEL_CONN = (
    "<named-connection caption='Book' name='excel-direct.book'>"
    "<connection class='excel-direct' cleaning='no' compat='no' filename='Data/Book.xlsx' "
    "interpretationMode='0' validate='no' /></named-connection>"
)
ORDERS_REL = (
    "<relation connection='excel-direct.book' name='Orders' table='[Orders$]' type='table'>"
    + columns_xml(ORDERS, "gridOrigin='A1:G5:no:A1:G5:0' header='yes' outcome='6'")
    + "</relation>"
)
RETURNS_REL = (
    "<relation connection='excel-direct.book' name='Returns' table='[Returns$]' type='table'>"
    + columns_xml(RETURNS, "gridOrigin='A1:B3:no:A1:B3:0' header='yes' outcome='6'")
    + "</relation>"
)


def datasource(name: str, caption: str, body: str, extra: str = "") -> str:
    return f"""
    <datasource caption='{caption}' inline='true' name='{name}' version='18.1'>
      <connection class='federated'>
        {body}
      </connection>
      {extra}
      <aliases enabled='yes' />
    </datasource>"""


def sheet(name: str, ds: str, dim: str, measure: str) -> str:
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources><datasource caption='{name}' name='{ds}' /></datasources>
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


def workbook_xml() -> str:
    single = datasource(
        "federated.orders",
        "Orders",
        f"<named-connections>{EXCEL_CONN}</named-connections>{ORDERS_REL}"
        f"<metadata-records>{records(ORDERS, 'Orders')}</metadata-records>",
    )
    joined = datasource(
        "federated.joined",
        "Orders+Returns",
        f"<named-connections>{EXCEL_CONN}</named-connections>"
        "<relation join='inner' type='join'><clause type='join'><expression op='='>"
        "<expression op='[Orders].[Order ID]' /><expression op='[Returns].[Order ID]' />"
        f"</expression></clause>{ORDERS_REL}{RETURNS_REL}</relation>"
        f"<metadata-records>{records(ORDERS, 'Orders')}{records(RETURNS, 'Returns')}</metadata-records>",
    )
    graph = (
        "<object-graph><objects>"
        f"<object caption='Orders' id='Orders_1'><properties context=''>{ORDERS_REL}</properties></object>"
        f"<object caption='Returns' id='Returns_2'><properties context=''>{RETURNS_REL}</properties></object>"
        "</objects><relationships><relationship><expression op='='>"
        "<expression op='[Order ID]' /><expression op='[Order ID (Returns)]' /></expression>"
        "<first-end-point object-id='Orders_1' /><second-end-point object-id='Returns_2' />"
        "</relationship></relationships></object-graph>"
    )
    model = datasource(
        "federated.model",
        "Model",
        f"<named-connections>{EXCEL_CONN}</named-connections>"
        "<relation type='collection'>" + ORDERS_REL + RETURNS_REL + "</relation>"
        f"<metadata-records>{records(ORDERS, 'Orders', 'Orders_1')}"
        f"{records(RETURNS, 'Returns', 'Returns_2')}</metadata-records>",
        graph,
    )
    text = datasource(
        "federated.targets",
        "Targets",
        "<named-connections><named-connection caption='targets' name='textscan.t'>"
        "<connection class='textscan' directory='Data' filename='targets.csv' /></named-connection>"
        "</named-connections>"
        "<relation connection='textscan.t' name='targets.csv' table='[targets#csv]' type='table'>"
        + columns_xml(
            TARGETS, "character-set='windows-1252' header='yes' locale='de_DE' separator=';'"
        )
        + "</relation>"
        f"<metadata-records>{records(TARGETS, 'targets.csv')}</metadata-records>",
    )
    sheets = (
        sheet("Single", "federated.orders", "Region", "Sales")
        + sheet("Joined", "federated.joined", "Region", "Quantity")
        + sheet("Model", "federated.model", "Region", "Sales")
        + sheet("Text", "federated.targets", "Region", "Target")
    )
    return b.workbook(single + joined + model + text, worksheets=sheets, params=False)


@pytest.fixture(scope="module")
def twbx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("files")
    book = folder / "Book.xlsx"
    write_book(book)
    path = folder / "files.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("files.twb", workbook_xml())
        archive.write(book, "Data/Book.xlsx")
        archive.writestr("Data/targets.csv", CSV.encode("windows-1252"))
    return path


@pytest.fixture(scope="module")
def parsed(twbx: Path) -> IR:
    return parse_workbook(twbx)


@pytest.fixture(scope="module")
def data(twbx: Path, parsed: IR, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("data")
    import_workbook(twbx, out, parsed)
    return out


def connection(ir: IR, caption: str) -> FileConnection:
    found = next(ds for ds in ir.datasources if ds.caption == caption).connection
    assert isinstance(found, FileConnection)
    return found


def rows(ir: IR, data: Path, name: str) -> dict[Any, Any]:
    ws = next(w for w in ir.worksheets if w.name == name)
    result = execute_query(data / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in result.rows}


def test_parse_file_connections(parsed: IR) -> None:
    assert parsed.unsupported == [], [u.reason for u in parsed.unsupported]
    assert check_references(parsed) == []
    single = connection(parsed, "Orders")
    (table,) = single.tables
    assert (table.format, table.path, table.sheet, table.cells, table.header) == (
        "excel",
        "Data/Book.xlsx",
        "Orders",
        "A1:G5",
        True,
    )
    assert [(c.name, c.datatype) for c in table.columns] == ORDERS
    assert single.relation == TableRelation(kind="table", name="Orders", table=["Files", "Orders"])
    joined = connection(parsed, "Orders+Returns")
    assert isinstance(joined.relation, JoinRelation)
    assert [t.name for t in joined.tables] == ["Orders", "Returns"]
    assert isinstance(connection(parsed, "Model").relation, ModelRelation)
    (text,) = connection(parsed, "Targets").tables
    assert (text.format, text.path, text.separator, text.charset, text.locale) == (
        "text",
        "Data/targets.csv",
        ";",
        "windows-1252",
        "de_DE",
    )


def test_import_typed_tables(twbx: Path, parsed: IR, data: Path) -> None:
    single = next(ds for ds in parsed.datasources if ds.caption == "Orders")
    report = json.loads((data / f"{single.id}.import.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["missing_columns"] == []
    assert report["table"]["rows"] == 4
    assert report["table"]["warnings"] == ["欄位「Sales」有 1 個值不是 real，成為 Null（例如 n/a）"]
    with duckdb.connect(str(data / f"{single.id}.duckdb"), read_only=True) as con:
        types = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'Files' AND table_name = 'Orders' ORDER BY ordinal_position"
        ).fetchall()
        values = con.execute('SELECT * FROM "Files"."Orders" ORDER BY "Order ID"').fetchall()
    assert [t for _, t in types] == [
        "VARCHAR",
        "VARCHAR",
        "DATE",
        "DOUBLE",
        "BIGINT",
        "VARCHAR",
        "BOOLEAN",
    ]
    assert values == [
        ("O1", "East", dt.date(2024, 1, 15), 100.5, 2, "10024", True),
        ("O2", "West", dt.date(2024, 2, 1), 200.0, 1, "02134", False),
        ("O3", "East", dt.date(2024, 3, 10), 50.25, 3, "98101", True),
        ("O4", None, dt.date(2024, 3, 11), None, 1, None, None),
    ]


def test_queries_on_file_sources(parsed: IR, data: Path) -> None:
    assert rows(parsed, data, "Single") == {"East": 150.75, "West": 200.0, None: None}
    # inner join：只有退貨的訂單（O1、O3）
    assert rows(parsed, data, "Joined") == {"East": 5}
    # Relationship 模型：只用到 Orders 的欄位時不受 Returns 影響
    assert rows(parsed, data, "Model") == {"East": 150.75, "West": 200.0, None: None}
    # 德文地區設定：1.234,5 → 1234.5、2.000 → 2000；windows-1252 的 Ü
    assert rows(parsed, data, "Text") == {"East": 1234.5, "West": 2000.0}


def test_text_dates_and_charset(parsed: IR, data: Path) -> None:
    text = next(ds for ds in parsed.datasources if ds.caption == "Targets")
    with duckdb.connect(str(data / f"{text.id}.duckdb"), read_only=True) as con:
        values = con.execute('SELECT * FROM "Files"."targets.csv" ORDER BY "Region"').fetchall()
    assert values == [
        ("East", 1234.5, dt.date(2024, 1, 15), "Über"),
        ("West", 2000.0, dt.date(2024, 2, 1), None),
    ]


def test_datasource_page_and_census(twbx: Path, parsed: IR) -> None:
    single = next(ds for ds in parsed.datasources if ds.caption == "Orders")
    summary = datasource_summary(single)
    assert summary["connection"] == {
        "kind": "file",
        "type": "Microsoft Excel",
        "files": ["Data/Book.xlsx"],
    }
    assert summary["tables"][0]["physical"][0]["source"] == "Book.xlsx：Orders"
    report = run_census(twbx)
    access = next(c for c in report.checks if c.key == "data_access")
    assert access.status == "pass"
    assert "Data/Book.xlsx（檔案，匯入時讀進資料庫）" in access.detail


@pytest.mark.parametrize(
    ("value", "datatype", "locale", "expected"),
    [
        ("1,234.5", "real", "en_US", 1234.5),
        ("1 234,5", "real", "fr_FR", 1234.5),
        ("(12)", "integer", None, -12),
        ("1/15/2024", "date", "en_US", dt.date(2024, 1, 15)),
        ("15/01/2024", "date", "en_GB", dt.date(2024, 1, 15)),
        ("2024/1/15 13:05:00", "datetime", "zh_TW", dt.datetime(2024, 1, 15, 13, 5)),
        ("yes", "boolean", None, True),
        (45306.5, "datetime", None, dt.datetime(2024, 1, 15, 12, 0)),
        (10024.0, "string", None, "10024"),
        ("   ", "integer", None, None),
    ],
)
def test_convert(value: Any, datatype: str, locale: str | None, expected: Any) -> None:
    assert convert(value, datatype, locale) == (expected, True)


def test_unparseable_values_become_null() -> None:
    assert convert("abc", "date", "en_US") == (None, False)
    assert convert("1.5", "integer", None) == (None, False)
