"""檔案型資料源的樞紐（pivot）、聯集（union）與合併不相符的欄位（project）：解析、匯入與查詢。

XML 依公開報表的寫法：樞紐見 2020.4 的報表（`<relation type='pivot'>` 的 `<tag>`、`<groups>`），聯集與
合併的欄位見 2019.1 的報表（`<relation type='union'>` 外面包 `<relation type='project'>`，新欄位的公式寫在
`<bindings>`，聯集的欄位與 Table Name 的 parent-name 是聯集的名稱）。

- Wide：Excel 的一張寬表，2019、2020、2021 三欄轉成「Year」「Sales」兩欄（Null 的值也保留成一列）
- Quarters：Excel 的 Q1、Q2 兩張工作表上下相接，Product（Q1）與 Item（Q2）合併成一欄，Q2 才有的 Note
  在 Q1 的列為 Null；Sheet、Table Name 是每一列的來源
- Regions：兩個 CSV 檔的聯集，Path 是檔案在作者電腦上的路徑
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_workbook
from tmig.ir import IR, FileConnection, PivotRelation, ProjectRelation, UnionRelation
from tmig.ir.integrity import check_references
from tmig.parser.workbook import parse_workbook
from tmig.render.datasource import datasource_summary
from tmig.sqlgen.query import preview_sql, worksheet_sql

from ..parser import builder as b

TYPES = {"string": 130, "real": 5, "integer": 20}


def write_book(path: Path) -> None:
    book = Workbook()
    wide = book.active
    assert wide is not None
    wide.title = "Wide"
    wide.append(["Region", "2019", "2020", "2021"])
    wide.append(["East", 10, 20, None])
    wide.append(["West", 5, 6, 7])
    wide.append(["North", None, 1, 2])
    q1 = book.create_sheet("Q1")
    q1.append(["Region", "Sales", "Product"])
    q1.append(["East", 10.5, "Chair"])
    q1.append(["West", 20, "Desk"])
    q2 = book.create_sheet("Q2")
    q2.append(["Region", "Sales", "Item", "Note"])
    q2.append(["East", 1, "Lamp", "promo"])
    q2.append(["South", 2, None, None])
    book.save(path)


EAST = "Region,Amount\r\nEast,100\r\nEast,50\r\n"
WEST = "Region,Amount\r\nWest,7\r\n"


def record(name: str, datatype: str, parent: str, ordinal: int) -> str:
    agg = "Sum" if datatype in ("real", "integer") else "Count"
    return f"""<metadata-record class='column'>
      <remote-name>{name}</remote-name><remote-type>{TYPES[datatype]}</remote-type>
      <local-name>[{name}]</local-name><parent-name>[{parent}]</parent-name>
      <remote-alias>{name}</remote-alias><ordinal>{ordinal}</ordinal>
      <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
    </metadata-record>"""


def columns(items: list[tuple[str, str]], attrs: str) -> str:
    cols = "".join(
        f"<column datatype='{t}' name='{n}' ordinal='{i}' />" for i, (n, t) in enumerate(items)
    )
    return f"<columns {attrs}>{cols}</columns>"


EXCEL = (
    "<named-connections><named-connection caption='Book' name='excel-direct.book'>"
    "<connection class='excel-direct' cleaning='no' compat='no' filename='Data/Book.xlsx' "
    "interpretationMode='0' validate='no' /></named-connection></named-connections>"
)


def sheet_rel(name: str, items: list[tuple[str, str]], rows: int) -> str:
    end = chr(ord("A") + len(items) - 1)
    grid = f"A1:{end}{rows}:no:A1:{end}{rows}:0"
    return (
        f"<relation connection='excel-direct.book' name='{name}' table='[{name}$]' type='table'>"
        + columns(items, f"gridOrigin='{grid}' header='yes' outcome='6'")
        + "</relation>"
    )


def datasource(name: str, caption: str, body: str, extra: str = "") -> str:
    return f"""
    <datasource caption='{caption}' inline='true' name='{name}' version='18.1'>
      <connection class='federated'>{body}</connection>
      <aliases enabled='yes' />
      {extra}
    </datasource>"""


def wide_datasource() -> str:
    years = ["2019", "2020", "2021"]
    pivot = (
        "<relation name='Pivot' type='pivot'><columns>"
        "<column datatype='string' name='Pivot Field Names' />"
        "<column datatype='integer' name='Pivot Field Values' /></columns>"
        "<tag name='Pivot Field Names'>"
        + "".join(f"<value name='[{y}]' />" for y in years)
        + "</tag><groups><group name='Pivot Field Values'>"
        + "".join(f"<field name='[Wide].[{y}]' />" for y in years)
        + "</group></groups>"
        + sheet_rel("Wide", [("Region", "string")] + [(y, "integer") for y in years], 4)
        + "</relation>"
    )
    records = (
        record("Region", "string", "Wide", 0)
        + record("Pivot Field Names", "string", "Pivot", 1)
        + record("Pivot Field Values", "integer", "Pivot", 2)
    )
    # 使用者把樞紐的兩欄改名為 Year、Sales（只改欄位的顯示名稱）
    extra = (
        "<column caption='Year' datatype='string' name='[Pivot Field Names]' role='dimension' "
        "type='nominal' /><column caption='Sales' datatype='integer' name='[Pivot Field Values]' "
        "role='measure' type='quantitative' />"
    )
    return datasource(
        "federated.wide",
        "Wide",
        f"{EXCEL}{pivot}<metadata-records>{records}</metadata-records>",
        extra,
    )


def quarters_datasource() -> str:
    union = (
        "<relation all='true' name='Q1+' type='union'><columns header='yes' />"
        + sheet_rel("Q1", [("Region", "string"), ("Sales", "real"), ("Product", "string")], 3)
        + sheet_rel(
            "Q2",
            [("Region", "string"), ("Sales", "real"), ("Item", "string"), ("Note", "string")],
            3,
        )
        + "</relation>"
    )
    binding = (
        "IF NOT ISNULL([Q1+].[Product]) THEN [Q1+].[Product]&#10;"
        "ELSEIF NOT ISNULL([Q1+].[Item]) THEN [Q1+].[Item]&#10;ELSE NULL&#10;END"
    )
    project = (
        "<relation type='project'><columns>"
        "<column caption='Product' hidden='true' name='[Q1+].[Product]' />"
        "<column caption='Item' hidden='true' name='[Q1+].[Item]' />"
        "<column name='[Project].[Product &amp; Item]' project-type='coalesce' /></columns>"
        f"<bindings><binding expression='{binding}' name='[Project].[Product &amp; Item]' />"
        f"</bindings>{union}</relation>"
    )
    records = (
        record("Product &amp; Item", "string", "Project", 0)
        + record("Region", "string", "Q1+", 1)
        + record("Sales", "real", "Q1+", 2)
        + record("Note", "string", "Q1+", 3)
        + record("Sheet", "string", "Q1+", 4)
        + record("Table Name", "string", "Q1+", 5)
    )
    return datasource(
        "federated.quarters",
        "Quarters",
        f"{EXCEL}{project}<metadata-records>{records}</metadata-records>",
    )


def regions_datasource() -> str:
    text = "character-set='UTF-8' header='yes' locale='en_US' separator=','"
    parts = "".join(
        f"<relation connection='textscan.u' name='{n}.csv' table='[{n}#csv]' type='table'>"
        + columns([("Region", "string"), ("Amount", "real")], text)
        + "</relation>"
        for n in ("east_2024", "west_2024")
    )
    union = f"<relation all='true' name='east_2024.csv+' type='union'><columns header='yes' />{parts}</relation>"
    records = (
        record("Region", "string", "east_2024.csv+", 0)
        + record("Amount", "real", "east_2024.csv+", 1)
        + record("Path", "string", "east_2024.csv+", 2)
        + record("Table Name", "string", "east_2024.csv+", 3)
    )
    named = (
        "<named-connections><named-connection caption='east_2024' name='textscan.u'>"
        "<connection class='textscan' directory='C:\\Data\\sales' filename='east_2024.csv' />"
        "</named-connection></named-connections>"
    )
    return datasource(
        "federated.regions",
        "Regions",
        f"{named}{union}<metadata-records>{records}</metadata-records>",
    )


def model_datasource() -> str:
    """Relationship 模型：邏輯資料表「Quarters」是 Q1、Q2 的聯集，「Wide」是樞紐，以地區關聯。"""
    q1 = sheet_rel("Q1", [("Region", "string"), ("Sales", "real"), ("Product", "string")], 3)
    q2 = sheet_rel(
        "Q2", [("Region", "string"), ("Sales", "real"), ("Item", "string"), ("Note", "string")], 3
    )
    union = (
        f"<relation all='true' name='Q1+' type='union'><columns header='yes' />{q1}{q2}</relation>"
    )
    wide = wide_datasource()
    pivot = wide[wide.index("<relation name='Pivot'") : wide.index("<metadata-records>")]
    graph = (
        "<object-graph><objects>"
        f"<object caption='Quarters' id='Quarters_1'><properties context=''>{union}</properties></object>"
        f"<object caption='Wide' id='Wide_2'><properties context=''>{pivot}</properties></object>"
        "</objects><relationships><relationship><expression op='='>"
        "<expression op='[Region]' /><expression op='[Region (Wide)]' /></expression>"
        "<first-end-point object-id='Quarters_1' /><second-end-point object-id='Wide_2' />"
        "</relationship></relationships></object-graph>"
    )

    def with_object(text: str, object_id: str) -> str:
        return text.replace(
            "</metadata-record>", f"<object-id>[{object_id}]</object-id></metadata-record>"
        )

    records = with_object(
        record("Region", "string", "Q1+", 0)
        + record("Sales", "real", "Q1+", 1)
        + record("Table Name", "string", "Q1+", 2),
        "Quarters_1",
    ) + with_object(
        record("Region", "string", "Wide", 3).replace("[Region]</local", "[Region (Wide)]</local")
        + record("Pivot Field Names", "string", "Pivot", 4)
        + record("Pivot Field Values", "integer", "Pivot", 5),
        "Wide_2",
    )
    return datasource(
        "federated.model",
        "Model",
        f"{EXCEL}<relation type='collection'>{union}{pivot}</relation>"
        f"<metadata-records>{records}</metadata-records>",
        graph,
    )


def sheet(name: str, ds: str, dim: str, measure: str, agg: str = "Sum") -> str:
    prefix = {"Sum": "sum", "Count": "cnt"}[agg]
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources><datasource name='{ds}' /></datasources>
          <datasource-dependencies datasource='{ds}'>
            <column-instance column='[{dim}]' derivation='None' name='[none:{dim}:nk]' pivot='key' type='nominal' />
            <column-instance column='[{measure}]' derivation='{agg}' name='[{prefix}:{measure}:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Bar' /></pane></panes>
        <rows>[{ds}].[none:{dim}:nk]</rows>
        <cols>[{ds}].[{prefix}:{measure}:qk]</cols>
      </table>
    </worksheet>"""


def workbook_xml() -> str:
    sheets = (
        sheet("By Year", "federated.wide", "Pivot Field Names", "Pivot Field Values")
        + sheet("By Region", "federated.wide", "Region", "Pivot Field Values")
        + sheet("Rows per Year", "federated.wide", "Pivot Field Names", "Region", "Count")
        + sheet("By Sheet", "federated.quarters", "Sheet", "Sales")
        + sheet("By Table", "federated.quarters", "Table Name", "Region", "Count")
        + sheet("By Note", "federated.quarters", "Note", "Sales")
        + sheet("By Product", "federated.quarters", "Product &amp; Item", "Sales")
        + sheet("By Path", "federated.regions", "Path", "Amount")
        + sheet("By File", "federated.regions", "Table Name", "Amount")
        + sheet("Model Sales", "federated.model", "Region (Wide)", "Sales")
        + sheet("Model Pivot", "federated.model", "Table Name", "Pivot Field Values")
    )
    datasources = (
        wide_datasource() + quarters_datasource() + regions_datasource() + model_datasource()
    )
    return b.workbook(datasources, worksheets=sheets, params=False)


@pytest.fixture(scope="module")
def twbx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("transforms")
    book = folder / "Book.xlsx"
    write_book(book)
    path = folder / "transforms.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("transforms.twb", workbook_xml())
        archive.write(book, "Data/Book.xlsx")
        archive.writestr("Data/sales/east_2024.csv", EAST)
        archive.writestr("Data/sales/west_2024.csv", WEST)
    return path


@pytest.fixture(scope="module")
def parsed(twbx: Path) -> IR:
    ir = parse_workbook(twbx)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


@pytest.fixture(scope="module")
def data(twbx: Path, parsed: IR, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("transforms-data")
    results = import_workbook(twbx, out, parsed)
    assert all(r.ok for r in results), [r.to_json() for r in results]
    return out


def connection(ir: IR, caption: str) -> FileConnection:
    found = next(ds for ds in ir.datasources if ds.caption == caption).connection
    assert isinstance(found, FileConnection)
    return found


def rows(ir: IR, data: Path, name: str) -> dict[Any, Any]:
    ws = next(w for w in ir.worksheets if w.name == name)
    result = execute_query(data / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in result.rows}


def test_parse(parsed: IR) -> None:
    assert check_references(parsed) == []
    pivot = connection(parsed, "Wide").relation
    assert isinstance(pivot, PivotRelation)
    assert (pivot.names, pivot.values, pivot.datatype) == (
        "Pivot Field Names",
        "Pivot Field Values",
        "integer",
    )
    assert [(c.column, c.label) for c in pivot.columns] == [
        ("2019", "2019"),
        ("2020", "2020"),
        ("2021", "2021"),
    ]
    project = connection(parsed, "Quarters").relation
    assert isinstance(project, ProjectRelation) and isinstance(project.source, UnionRelation)
    assert [c.name for c in project.columns] == ["Product & Item"]
    assert [p.sheet for p in project.source.parts] == ["Q1", "Q2"]
    regions = connection(parsed, "Regions")
    union = regions.relation
    assert isinstance(union, UnionRelation)
    assert [p.path for p in union.parts] == [
        "C:\\Data\\sales\\east_2024.csv",
        "C:\\Data\\sales\\west_2024.csv",
    ]
    assert [t.path for t in regions.tables] == [
        "Data/sales/east_2024.csv",
        "Data/sales/west_2024.csv",
    ]


def test_pivot(parsed: IR, data: Path) -> None:
    # 每個地區每一年各一列：Null 的值（East 2021、North 2019）也保留成一列
    assert rows(parsed, data, "By Year") == {"2019": 15, "2020": 27, "2021": 9}
    assert rows(parsed, data, "By Region") == {"East": 30, "North": 3, "West": 18}
    assert rows(parsed, data, "Rows per Year") == {"2019": 3, "2020": 3, "2021": 3}


def test_union_and_merged_fields(parsed: IR, data: Path) -> None:
    assert rows(parsed, data, "By Sheet") == {"Q1": 30.5, "Q2": 3}
    assert rows(parsed, data, "By Table") == {"Q1": 2, "Q2": 2}
    # 只有 Q2 有 Note：Q1 的列為 Null
    assert rows(parsed, data, "By Note") == {"promo": 1, None: 32.5}
    # Product（Q1）與 Item（Q2）合併：取第一個不是 Null 的值
    assert rows(parsed, data, "By Product") == {"Chair": 10.5, "Desk": 20, "Lamp": 1, None: 2}


def test_text_union(parsed: IR, data: Path) -> None:
    assert rows(parsed, data, "By Path") == {
        "C:\\Data\\sales\\east_2024.csv": 150,
        "C:\\Data\\sales\\west_2024.csv": 7,
    }
    assert rows(parsed, data, "By File") == {"east_2024.csv": 150, "west_2024.csv": 7}


def test_datasource_page(parsed: IR, data: Path) -> None:
    import duckdb

    wide = next(ds for ds in parsed.datasources if ds.caption == "Wide")
    summary = datasource_summary(wide)
    (table,) = summary["tables"]
    assert [t["name"] for t in table["physical"]] == ["Wide", "Pivot"]
    assert table["physical"][1]["source"].startswith("樞紐：2019、2020、2021")
    preview = preview_sql(parsed, wide.id)
    with duckdb.connect(str(data / f"{wide.id}.duckdb"), read_only=True) as con:
        assert con.execute(preview.count_sql).fetchone() == (9,)
    quarters = next(ds for ds in parsed.datasources if ds.caption == "Quarters")
    physical = datasource_summary(quarters)["tables"][0]["physical"]
    assert [t["name"] for t in physical] == ["Q1", "Q2", "Q1+", "Project"]


def test_model_with_union_and_pivot(parsed: IR, data: Path) -> None:
    """Relationship 模型的邏輯資料表是聯集與樞紐：量值在聯集（Quarters）彙總，維度來自樞紐（Wide）。"""
    import json

    model = next(ds for ds in parsed.datasources if ds.caption == "Model")
    report = json.loads((data / f"{model.id}.import.json").read_text(encoding="utf-8"))
    assert (report["ok"], report["missing_columns"]) == (True, [])
    # South 不在 Wide（Null）、North 沒有銷售（量值為 Null）
    assert rows(parsed, data, "Model Sales") == {"East": 11.5, "West": 20, "North": None, None: 2}
    # 量值在樞紐（East 30、West 18、North 3）：依關聯到的聯集的資料表（Table Name）彙總，
    # East 在 Q1、Q2 都有（各算一次）、West 只在 Q1、North 沒有對應（Null）
    assert rows(parsed, data, "Model Pivot") == {"Q1": 48, "Q2": 30, None: 3}
