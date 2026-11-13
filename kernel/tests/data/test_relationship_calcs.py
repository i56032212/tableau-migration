"""關聯計算與 join 計算：Tableau 2020.2 起關聯條件的一邊可以是公式（object-graph 的
<expression op='LOWER(LEFT([Region],1))' />，見 Tableau Server 處理過的報表），解析成所屬邏輯資料表上隱藏的
計算欄位；桌面版以 tmig.authoring 建立、修改關聯計算與 join 計算。匯入 Excel 後以 DuckDB 查詢比對。"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from tmig.authoring import (
    Calculated,
    add_table,
    inspect_file,
    join_calc_display,
    link_calc_display,
    new_workbook,
    remove_table,
    set_join,
    set_relationship,
)
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_sources, import_workbook
from tmig.edit import EditError, catalog, replace_worksheet
from tmig.ir import IR, JoinRelation, ModelRelation
from tmig.ir.integrity import check_references
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b

TYPES = {"string": 130, "real": 5, "integer": 20}


def write_book(path: Path) -> Path:
    book = Workbook()
    orders = book.active
    assert orders is not None
    orders.title = "Orders"
    orders.append(["Region", "Sales", "Code"])
    for row in (["East", 100, "E1"], ["West", 20, "W1"], ["East", 5, "E2"], ["South", 7, "S1"]):
        orders.append(row)
    managers = book.create_sheet("Managers")
    managers.append(["Key", "Manager"])
    for row in (["e", "Ann"], ["w", "Bob"], ["n", "Cid"]):
        managers.append(row)
    return_book = book.create_sheet("Codes")
    return_book.append(["Prefix", "Label"])
    for row in (["E", "Eastern"], ["W", "Western"]):
        return_book.append(row)
    book.save(path)
    return path


def record(name: str, datatype: str, parent: str, ordinal: int, object_id: str) -> str:
    agg = "Sum" if datatype in ("real", "integer") else "Count"
    return f"""<metadata-record class='column'>
      <remote-name>{name}</remote-name><remote-type>{TYPES[datatype]}</remote-type>
      <local-name>[{name}]</local-name><parent-name>[{parent}]</parent-name>
      <remote-alias>{name}</remote-alias><ordinal>{ordinal}</ordinal>
      <local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
      <object-id>[{object_id}]</object-id>
    </metadata-record>"""


def sheet_rel(name: str, items: list[tuple[str, str]], rows: int) -> str:
    end = chr(ord("A") + len(items) - 1)
    grid = f"A1:{end}{rows}:no:A1:{end}{rows}:0"
    cols = "".join(
        f"<column datatype='{t}' name='{n}' ordinal='{i}' />" for i, (n, t) in enumerate(items)
    )
    return (
        f"<relation connection='excel-direct.book' name='{name}' table='[{name}$]' type='table'>"
        f"<columns gridOrigin='{grid}' header='yes' outcome='6'>{cols}</columns></relation>"
    )


def model_datasource(condition: str) -> str:
    orders = sheet_rel("Orders", [("Region", "string"), ("Sales", "real"), ("Code", "string")], 5)
    managers = sheet_rel("Managers", [("Key", "string"), ("Manager", "string")], 4)
    graph = (
        "<object-graph><objects>"
        f"<object caption='Orders' id='Orders_1'><properties context=''>{orders}</properties></object>"
        f"<object caption='Managers' id='Managers_2'><properties context=''>{managers}</properties>"
        "</object></objects><relationships><relationship>"
        f"{condition}"
        "<first-end-point object-id='Orders_1' /><second-end-point object-id='Managers_2' />"
        "</relationship></relationships></object-graph>"
    )
    records = (
        record("Region", "string", "Orders", 0, "Orders_1")
        + record("Sales", "real", "Orders", 1, "Orders_1")
        + record("Code", "string", "Orders", 2, "Orders_1")
        + record("Key", "string", "Managers", 3, "Managers_2")
        + record("Manager", "string", "Managers", 4, "Managers_2")
    )
    excel = (
        "<named-connections><named-connection caption='Book' name='excel-direct.book'>"
        "<connection class='excel-direct' cleaning='no' compat='no' filename='Data/Book.xlsx' "
        "interpretationMode='0' validate='no' /></named-connection></named-connections>"
    )
    return f"""
    <datasource caption='Model' inline='true' name='federated.model' version='18.1'>
      <connection class='federated'>{excel}<relation type='collection'>{orders}{managers}</relation>
        <metadata-records>{records}</metadata-records></connection>
      <aliases enabled='yes' />
      {graph}
    </datasource>"""


def sheet(name: str, dim: str, measure: str) -> str:
    ds = "federated.model"
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


def package(folder: Path, condition: str) -> Path:
    book = write_book(folder / "Book.xlsx")
    path = folder / "calcs.twbx"
    xml = b.workbook(
        model_datasource(condition),
        worksheets=sheet("By Manager", "Manager", "Sales"),
        params=False,
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("calcs.twb", xml)
        archive.write(book, "Data/Book.xlsx")
    return path


def rows(ir: IR, data: Path, name: str) -> dict[Any, Any]:
    ws = next(w for w in ir.worksheets if w.name == name)
    result = execute_query(data / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in result.rows}


def test_parse_relationship_calculation(tmp_path: Path) -> None:
    condition = (
        "<expression op='='><expression op='LOWER(LEFT([Region],1))' />"
        "<expression op='[Key]' /></expression>"
    )
    path = package(tmp_path, condition)
    ir = parse_workbook(path)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert check_references(ir) == []
    ds = ir.datasources[0]
    calcs = [f for f in ds.fields if f.relationship_calc]
    assert len(calcs) == 1 and calcs[0].hidden and calcs[0].table == "Orders_1"
    assert calcs[0].calc is not None and calcs[0].calc.formula == "LOWER(LEFT([Region],1))"
    model = ds.connection.relation  # type: ignore[union-attr]
    assert isinstance(model, ModelRelation)
    assert model.relationships[0].on[0].left == calcs[0].id
    # 資料窗格不顯示關聯計算
    shown = {f["id"] for f in catalog(ir)["datasources"][0]["fields"]}
    assert calcs[0].id not in shown
    out = tmp_path / "data"
    results = import_workbook(path, out, ir)
    assert all(r.ok for r in results), [r.to_json() for r in results]
    # 量值在 Orders 彙總；South 沒有對應的經理（Null），Cid 沒有訂單（與 Tableau 相同也列出）
    assert rows(ir, out, "By Manager") == {"Ann": 105, "Bob": 20, "Cid": None, None: 7}


def test_relationship_calculation_unsupported_cases(tmp_path: Path) -> None:
    """公式用到兩張資料表的欄位時不是關聯計算，列為不支援。"""
    condition = (
        "<expression op='='><expression op='[Region] + [Key]' />"
        "<expression op='[Key]' /></expression>"
    )
    from tmig.parser.workbook import NoDatasourceError

    with pytest.raises(NoDatasourceError, match="關聯條件"):
        parse_workbook(package(tmp_path, condition))


def ids(ir: IR) -> dict[str, str]:
    return {f.caption: f.id for f in ir.datasources[0].fields}


def query(ir: IR, data: Path, rows_field: str, measure: str) -> dict[Any, Any]:
    assert check_references(ir) == []
    ds = ir.datasources[0]
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        datasource=ds.id, rows=[{"field": rows_field}], columns=[{"field": measure, "agg": "sum"}]
    )
    ir, ws = replace_worksheet(ir, sheet)
    sources = data / "sources"
    out = data / "out"
    (result,) = import_sources(sources, out, ir, only=[ds.id])
    assert result.ok, result.to_json()
    found = execute_query(out / f"{ds.id}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in found.rows}


@pytest.fixture
def authored(tmp_path: Path) -> tuple[IR, Path, Any]:
    book = write_book(tmp_path / "Book.xlsx")
    sources = tmp_path / "data" / "sources" / "Data"
    sources.mkdir(parents=True)
    (sources / "Book.xlsx").write_bytes(book.read_bytes())
    source = inspect_file(book)
    return new_workbook("Calcs", source, "Orders", "Data/Book.xlsx"), tmp_path / "data", source


def test_author_relationship_calculation(authored: tuple[IR, Path, Any]) -> None:
    ir, data, source = authored
    ds = ir.datasources[0].id
    f = ids(ir)
    # 關聯：Orders 的 LOWER(LEFT([Region], 1)) = Managers 的 Key
    ir = add_table(
        ir,
        ds,
        source,
        "Managers",
        "Data/Book.xlsx",
        "relationship",
        on=[(Calculated("LOWER(LEFT([Region], 1))"), "=", "Key")],
    )
    f = ids(ir)
    assert query(ir, data, f["Manager"], f["Sales"]) == {
        "Ann": 105,
        "Bob": 20,
        "Cid": None,
        None: 7,
    }
    calcs = [x for x in ir.datasources[0].fields if x.relationship_calc]
    assert len(calcs) == 1 and calcs[0].calc is not None
    assert calcs[0].calc.formula == "LOWER(LEFT([Region], 1))"
    assert link_calc_display(ir.datasources[0], calcs[0]) == "LOWER(LEFT([Region], 1))"
    model = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(model, ModelRelation)
    managers = model.tables[1].id
    # 改成兩邊都是公式：UPPER([Key]) = LEFT([Code], 1)；不再用到的關聯計算一併移除
    ir = set_relationship(
        ir,
        ds,
        managers,
        [(Calculated("LEFT([Code], 1)"), "=", Calculated("UPPER([Key])"))],
    )
    calcs = [x for x in ir.datasources[0].fields if x.relationship_calc]
    assert sorted(c.calc.formula for c in calcs if c.calc) == ["LEFT([Code], 1)", "UPPER([Key])"]
    f = ids(ir)
    assert query(ir, data, f["Manager"], f["Sales"]) == {
        "Ann": 105,
        "Bob": 20,
        "Cid": None,
        None: 7,
    }
    # 改名之後公式仍然有效（以內部名稱參照），顯示時用新的名稱
    from tmig.authoring import update_field

    ir = update_field(ir, ds, f["Code"], caption="Order Code")
    left = next(c for c in ir.datasources[0].fields if c.relationship_calc and c.table != managers)
    assert link_calc_display(ir.datasources[0], left) == "LEFT([Order Code], 1)"
    # 公式只能用到這一邊的資料表
    with pytest.raises(EditError, match="只能用到"):
        set_relationship(ir, ds, managers, [(Calculated("[Manager]"), "=", f["Key"])])
    with pytest.raises(EditError, match="不能彙總"):
        set_relationship(ir, ds, managers, [(Calculated("SUM([Sales])"), "=", f["Key"])])
    # 移除 Managers：關聯計算一併移除
    ir = remove_table(ir, ds, managers)
    assert not [x for x in ir.datasources[0].fields if x.relationship_calc]


def test_author_join_calculation(authored: tuple[IR, Path, Any]) -> None:
    ir, data, source = authored
    ds = ir.datasources[0].id
    f = ids(ir)
    # join：LEFT([Code], 1) = Codes 的 Prefix（左側 join）
    ir = add_table(
        ir,
        ds,
        source,
        "Codes",
        "Data/Book.xlsx",
        "join",
        join="left",
        on=[(Calculated("LEFT([Code], 1)"), "=", "Prefix")],
    )
    f = ids(ir)
    assert query(ir, data, f["Label"], f["Sales"]) == {"Eastern": 105, "Western": 20, None: 7}
    relation = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(relation, JoinRelation)
    term = relation.on[0].left
    assert term.formula == "LEFT([Orders].[Code], 1)"
    assert join_calc_display(ir.datasources[0], None, term.formula) == "LEFT([Code], 1)"
    # 右邊也可以是公式：LEFT([Region], 1) = UPPER([Prefix])
    ir = set_join(
        ir,
        ds,
        "Codes",
        "inner",
        [(Calculated("LEFT([Region], 1)"), "=", Calculated("UPPER([Prefix])"))],
    )
    relation = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(relation, JoinRelation)
    assert relation.on[0].right.formula == "UPPER([Codes].[Prefix])"
    f = ids(ir)
    assert query(ir, data, f["Label"], f["Sales"]) == {"Eastern": 105, "Western": 20}
    with pytest.raises(EditError, match="不在"):
        set_join(ir, ds, "Codes", "inner", [(Calculated("[Label]"), "=", "Prefix")])
    with pytest.raises(EditError, match="沒有欄位"):
        set_join(ir, ds, "Codes", "inner", [(f["Region"], "=", Calculated("[Region]"))])
