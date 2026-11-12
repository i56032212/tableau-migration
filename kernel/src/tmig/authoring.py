"""建立資料源與編輯資料模型（桌面版的「連線到資料」與資料源分頁的編輯，Tableau Desktop 的「資料來源」分頁）。

- 檢查檔案（inspect_file）：Excel（.xlsx、.xlsm、.xls）的各工作表、文字檔（CSV、TSV、TXT）、空間檔案
  （Shapefile、GeoJSON、KML、TopoJSON）的欄名、型別與列數。型別依儲存格的值判斷：都是整數為 integer、
  都是數字為 real、都是日期（沒有時間）為 date、有時間為 datetime、都是 TRUE／FALSE 為 boolean，其他為
  string；文字檔依 en_US 的寫法解讀數字與日期，字元集依序試 UTF-8、Big5（cp950）與 Latin-1，分隔字元自動判斷
- 資料源（new_workbook、add_datasource）：檔案型連線（FileConnection），每張資料表匯入成 Files.<名稱>；
  欄位與 Tableau 相同，數字（與空間欄位）為量值、其他為維度；欄名是 Country、State、City、Postal Code、
  Latitude、Longitude 等時給地理角色
- 資料模型（add_table、remove_table、set_join、set_relationship、pivot、unpivot）：與 Tableau 2020.2 起的
  資料來源分頁相同，第二張資料表預設以關聯（Relationship）加入成另一張邏輯資料表；也可以在邏輯資料表中
  join、聯集（union）或把欄樞紐（pivot）成列
- 欄位（update_field）：改名（顯示名稱）、隱藏、角色、預設彙總與資料類型（檔案的欄位以新的型別重新匯入）

所有函式都不改動傳入的 IR，回傳新的 IR；有工作表或計算欄位用到的欄位不能移除、改型別或角色。
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from tmig.edit import (
    EDITOR_REF,
    EditError,
    _datasource,
    _replace_datasource,
    _validated,
    add_worksheet,
)
from tmig.formula.ast import FieldRef, walk
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.ir import (
    FILE_SCHEMA,
    IR,
    IR_VERSION,
    DataField,
    Datasource,
    DatasourceSettings,
    FileColumn,
    FileConnection,
    FileTable,
    JoinCondition,
    JoinRelation,
    JoinTerm,
    LogicalTable,
    ModelRelation,
    PivotColumn,
    PivotRelation,
    Relationship,
    RelationshipCondition,
    TableRelation,
    UnionColumn,
    UnionPart,
    UnionRelation,
    Workbook,
)
from tmig.ir.models import Aggregation, GeoRole, Relation

DATA_DIR = "Data"
"""檔案在報表中的資料夾：FileTable.path 為 Data/<檔名>（與 .twbx 相同的寫法）。"""
SAMPLE_ROWS = 10_000
"""判斷型別時讀的列數（匯入時仍讀全部的列，不符合型別的值成為 Null）。"""
EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls")
TEXT_SUFFIXES = (".csv", ".tsv", ".txt", ".tab")
SPATIAL_SUFFIXES = (".shp", ".zip", ".geojson", ".json", ".kml", ".kmz", ".topojson")
TEXT_LOCALE = "en_US"
CHARSETS = ("UTF-8", "cp950", "latin-1")
"""文字檔的字元集依序嘗試：UTF-8（含 BOM）、Big5（Windows 的 950）、Latin-1。"""
GEO_NAMES: dict[str, GeoRole] = {
    "country": "country",
    "country/region": "country",
    "state": "state",
    "state/province": "state",
    "province": "state",
    "county": "county",
    "city": "city",
    "postal code": "zip",
    "zip": "zip",
    "zip code": "zip",
    "zipcode": "zip",
    "latitude": "latitude",
    "lat": "latitude",
    "longitude": "longitude",
    "long": "longitude",
    "lon": "longitude",
    "lng": "longitude",
}
"""欄名（不分大小寫）→ 地理角色：Tableau 依欄名自動給的地理角色。"""
JoinType = Literal["inner", "left", "right", "full"]
Comparison = Literal["=", "!=", "<", "<=", ">", ">="]


# ---------------------------------------------------------------- 檢查檔案


@dataclass
class InspectedTable:
    """檔案中的一張資料表：Excel 的工作表、文字檔本身或空間檔案的圖層。"""

    name: str
    columns: list[FileColumn]
    rows: int
    sheet: str | None = None
    layer: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sheet": self.sheet,
            "layer": self.layer,
            "rows": self.rows,
            "columns": [c.model_dump(mode="json") for c in self.columns],
        }


@dataclass
class InspectedFile:
    """檢查過的檔案。"""

    path: Path
    format: Literal["excel", "text", "spatial"]
    tables: list[InspectedTable]
    separator: str = ","
    charset: str = "UTF-8"
    sidecars: list[Path] = field(default_factory=list)
    """Shapefile 的 .shx、.dbf、.prj、.cpg：與 .shp 一起複製到報表中。"""

    def table(self, name: str | None) -> InspectedTable:
        if name is None:
            return self.tables[0]
        found = next((t for t in self.tables if t.name == name), None)
        if found is None:
            raise EditError(f"{self.path.name} 中沒有資料表「{name}」")
        return found

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "format": self.format,
            "separator": self.separator,
            "charset": self.charset,
            "tables": [t.to_json() for t in self.tables],
        }


def inspect_file(path: Path) -> InspectedFile:
    """檔案的資料表、欄名、型別與列數。"""
    suffix = path.suffix.lower()
    if not path.is_file():
        raise EditError(f"找不到檔案：{path}")
    data = path.read_bytes()
    if suffix in EXCEL_SUFFIXES:
        return InspectedFile(path, "excel", _inspect_excel(path.name, data))
    if suffix in TEXT_SUFFIXES:
        table, separator, charset = _inspect_text(path.name, data)
        return InspectedFile(path, "text", [table], separator, charset)
    if suffix in SPATIAL_SUFFIXES:
        return _inspect_spatial(path, data)
    raise EditError(
        f"不認得的檔案類型：{path.name}（支援 Excel、文字檔與 Shapefile、GeoJSON、KML、TopoJSON）"
    )


def _inspect_excel(name: str, data: bytes) -> list[InspectedTable]:
    grids: list[tuple[str, list[list[Any]], int]] = []
    if name.lower().endswith(".xls"):
        import xlrd

        try:
            book = xlrd.open_workbook(file_contents=data)
        except Exception as exc:
            raise EditError(f"{name} 不是可讀取的 Excel 檔：{exc}") from exc
        for sheet in book.sheets():
            grid: list[list[Any]] = []
            for r in range(min(sheet.nrows, SAMPLE_ROWS + 1)):
                row: list[Any] = []
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        row.append(xlrd.xldate.xldate_as_datetime(float(cell.value), book.datemode))
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        row.append(bool(cell.value))
                    elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
                        row.append(None)
                    else:
                        value = cell.value
                        row.append(
                            int(value) if isinstance(value, float) and value.is_integer() else value
                        )
                grid.append(row)
            grids.append((sheet.name, grid, max(sheet.nrows - 1, 0)))
    else:
        import openpyxl

        try:
            book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise EditError(f"{name} 不是可讀取的 Excel 檔：{exc}") from exc
        for title in book.sheetnames:
            grid = list[list[Any]]()
            count = 0
            for cells in book[title].iter_rows(values_only=True):
                if count <= SAMPLE_ROWS:
                    grid.append(list(cells))
                count += 1
            while grid and all(_blank(v) for v in grid[-1]):
                grid.pop()
                count -= 1
            grids.append((title, grid, max(count - 1, 0)))
        book.close()
    tables: list[InspectedTable] = []
    for title, grid, rows in grids:
        if not grid or not any(not _blank(v) for v in grid[0]):
            continue  # 空白的工作表
        columns = _columns(grid[0], grid[1:], excel=True)
        if columns:
            tables.append(InspectedTable(title, columns, rows, sheet=title))
    if not tables:
        raise EditError(f"{name} 中沒有資料（每張工作表的第一列都是空白）")
    return tables


def _inspect_text(name: str, data: bytes) -> tuple[InspectedTable, str, str]:
    text: str | None = None
    charset = CHARSETS[0]
    for candidate in CHARSETS:
        codec = "utf-8-sig" if candidate == "UTF-8" else candidate
        try:
            text = data.decode(codec)
            charset = candidate
            break
        except UnicodeDecodeError:
            continue
    assert text is not None  # latin-1 一定解得開
    sample = text[:65536]
    separator = "\t" if name.lower().endswith((".tsv", ".tab")) else ","
    with contextlib.suppress(csv.Error):
        separator = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator)
    grid: list[list[Any]] = []
    count = 0
    for row in reader:
        if count <= SAMPLE_ROWS:
            grid.append(list(row))
        count += 1
    while grid and all(_blank(v) for v in grid[-1]):
        grid.pop()
        count -= 1
    if not grid:
        raise EditError(f"{name} 是空的")
    columns = _columns(grid[0], grid[1:], excel=False)
    stem = PurePosixPath(name).name
    return InspectedTable(stem, columns, max(count - 1, 0)), separator, charset


def _inspect_spatial(path: Path, data: bytes) -> InspectedFile:
    from tmig.data.spatial_files import (
        GEOMETRY_COLUMN,
        SHAPEFILE_PARTS,
        SpatialFileError,
        read_spatial,
    )

    files = {path.name: data}
    sidecars: list[Path] = []
    if path.suffix.lower() == ".shp":
        for ext in SHAPEFILE_PARTS[1:]:
            for candidate in (path.with_suffix(ext), path.with_suffix(ext.upper())):
                if candidate.is_file():
                    files[candidate.name if candidate.suffix == ext else path.stem + ext] = (
                        candidate.read_bytes()
                    )
                    sidecars.append(candidate)
                    break
    layers: list[str | None] = [None]
    if path.suffix.lower() == ".zip":
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            shapes = [n for n in archive.namelist() if n.lower().endswith(".shp")]
        layers = [PurePosixPath(n).stem for n in shapes] or [None]
    tables: list[InspectedTable] = []
    for layer in layers:
        try:
            found = read_spatial(files, path.name, layer)
        except SpatialFileError as exc:
            raise EditError(str(exc)) from exc
        columns: list[FileColumn] = []
        for column in found.columns:
            if column == GEOMETRY_COLUMN:
                columns.append(FileColumn(name=column, datatype="spatial"))
                continue
            values = [row.get(column) for row in found.rows[:SAMPLE_ROWS]]
            columns.append(FileColumn(name=column, datatype=_infer(values, excel=True)))  # type: ignore[arg-type]
        name = layer or path.name
        tables.append(InspectedTable(name, columns, len(found.rows), layer=layer))
    return InspectedFile(path, "spatial", tables, sidecars=sidecars)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _columns(header: list[Any], rows: list[list[Any]], excel: bool) -> list[FileColumn]:
    """標題列 → 欄位（空白的標題為 F1、F2…，重複的加上序號），型別依下面各列的值判斷。"""
    width = len(header)
    while (
        width
        and _blank(header[width - 1])
        and all(width - 1 >= len(r) or _blank(r[width - 1]) for r in rows)
    ):
        width -= 1  # 尾端全部空白的欄
    columns: list[FileColumn] = []
    taken: set[str] = set()
    for i in range(width):
        raw = header[i] if i < len(header) else None
        name = (str(raw).strip() if not _blank(raw) else "") or f"F{i + 1}"
        if isinstance(raw, float) and raw.is_integer():
            name = str(int(raw))
        base, n = name, 1
        while name.casefold() in taken:
            n += 1
            name = f"{base}{n}"
        taken.add(name.casefold())
        values = [r[i] if i < len(r) else None for r in rows]
        columns.append(FileColumn(name=name, datatype=_infer(values, excel)))  # type: ignore[arg-type]
    return columns


def _infer(values: list[Any], excel: bool) -> str:
    """一欄的型別：Excel 依儲存格的型別、文字檔依文字能否解讀成數字、日期或布林值。"""
    from tmig.data.file_import import convert

    present = [v for v in values if not _blank(v)]
    if not present:
        return "string"
    if excel or any(not isinstance(v, str) for v in present):
        if all(isinstance(v, bool) for v in present):
            return "boolean"
        if all(isinstance(v, int) and not isinstance(v, bool) for v in present):
            return "integer"
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
            if all(float(v).is_integer() for v in present) and any(
                isinstance(v, int) for v in present
            ):
                return "integer"
            return "real"
        if all(isinstance(v, (dt.date, dt.datetime)) for v in present):
            stamps = [v for v in present if isinstance(v, dt.datetime)]
            return "datetime" if any(v.time() != dt.time() for v in stamps) else "date"
        # Excel 依儲存格的型別：文字的儲存格（即使寫的是數字或 Yes）為字串
        return "string"
    texts = [str(v) for v in present]
    for candidate in ("integer", "real", "boolean", "date", "datetime"):
        if candidate == "boolean":
            # 文字檔只有 true／false（不分大小寫）是布林值；Yes、Y、1 等是字串或數字
            if all(v.strip().casefold() in ("true", "false") for v in texts):
                return "boolean"
            continue
        if all(convert(v, candidate, TEXT_LOCALE)[1] for v in texts):
            if candidate == "date" and any(
                convert(v, "datetime", TEXT_LOCALE)[0].time() != dt.time() for v in texts
            ):
                return "datetime"
            return candidate
    return "string"


# ---------------------------------------------------------------- 資料源


def file_table(source: InspectedFile, table: str | None, package_path: str, name: str) -> FileTable:
    """檔案中的一張資料表 → FileTable（name：關聯樹中的名稱）。"""
    found = source.table(table)
    if source.format == "excel":
        return FileTable(
            name=name, format="excel", path=package_path, sheet=found.sheet, columns=found.columns
        )
    if source.format == "text":
        return FileTable(
            name=name,
            format="text",
            path=package_path,
            separator=source.separator,
            charset=source.charset,
            locale=TEXT_LOCALE,
            columns=found.columns,
        )
    return FileTable(
        name=name, format="spatial", path=package_path, layer=found.layer, columns=found.columns
    )


def new_workbook(name: str, source: InspectedFile, table: str | None, package_path: str) -> IR:
    """新的報表：一個資料源（檔案中的一張資料表）與一張空白的工作表（與 Tableau 連線後的「工作表 1」相同）。"""
    ds = _new_datasource(set(), source, table, package_path)
    workbook = Workbook(
        id="wb_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "report"),
        name=name.strip() or "報表",
        tableau_version=None,
        source_ref=EDITOR_REF,
    )
    ir = IR(ir_version=IR_VERSION, workbook=workbook, datasources=[ds])  # type: ignore[arg-type]
    ir, _ = add_worksheet(ir, None, ds.id)
    return ir


def add_datasource(
    ir: IR, source: InspectedFile, table: str | None, package_path: str
) -> tuple[IR, Datasource]:
    """報表加上新的資料源（Tableau 的「資料＞新增資料源」）。"""
    taken = {f.id for d in ir.datasources for f in d.fields} | {d.id for d in ir.datasources}
    ds = _new_datasource(taken | _ids(ir), source, table, package_path)
    updated = ir.model_copy(update={"datasources": [*ir.datasources, ds]})
    return _validated(updated, "新增資料源失敗"), ds


def _ids(ir: IR) -> set[str]:
    from tmig.edit import _all_ids

    return _all_ids(ir)


def _new_datasource(
    taken: set[str], source: InspectedFile, table: str | None, package_path: str
) -> Datasource:
    found = source.table(table)
    name = found.name
    stem = source.path.stem
    caption = f"{found.name} ({stem})" if source.format == "excel" else found.name
    ds_id = _fresh(taken, "ds_" + _slug(caption))
    fields = _fields(taken | {ds_id}, found.columns, name, set())
    connection = FileConnection(
        kind="file",
        tables=[file_table(source, table, package_path, name)],
        relation=TableRelation(kind="table", name=name, table=[FILE_SCHEMA, name]),
    )
    return Datasource(
        id=ds_id,
        name=f"federated.{secrets.token_hex(14)}",
        caption=caption,
        connection=connection,
        settings=DatasourceSettings(week_start="sunday"),
        fields=fields,
        source_ref=EDITOR_REF,
    )


def _fields(
    taken: set[str],
    columns: list[FileColumn],
    table: str,
    captions: set[str],
    physical: str | None = None,
    label: str | None = None,
) -> list[DataField]:
    """資料表的欄位：與已有的欄位同名時加上「 (資料表)」（與 Tableau 相同；label 為資料表的顯示名稱）。"""
    found: list[DataField] = []
    for column in columns:
        caption = column.name
        if caption.casefold() in captions:
            caption = f"{column.name} ({label or physical or table})"
        captions.add(caption.casefold())
        field_id = _fresh(taken, "f_" + _slug(caption))
        taken.add(field_id)
        found.append(_raw_field(field_id, caption, column, table, physical))
    return found


def _measure_agg(datatype: str, geo: GeoRole | None, role: str) -> Aggregation | None:
    """量值的預設彙總：與 Tableau 相同，緯度、經度為平均（AVG），其他數字為總和，文字與日期為計數。"""
    if role != "measure":
        return None
    if geo in ("latitude", "longitude"):
        return "avg"
    return "sum" if datatype in ("integer", "real") else "count"


def _raw_field(
    field_id: str, caption: str, column: FileColumn, table: str, physical: str | None
) -> DataField:
    numeric = column.datatype in ("integer", "real")
    spatial = column.datatype == "spatial"
    role = "measure" if numeric or spatial else "dimension"
    geo = GEO_NAMES.get(column.name.strip().casefold()) if not spatial else None
    if geo in ("latitude", "longitude") and not numeric:
        geo = None
    if geo is not None and geo not in ("latitude", "longitude"):
        role = "dimension"
    return DataField(
        id=field_id,
        name=f"[{caption}]",
        caption=caption,
        role=role,  # type: ignore[arg-type]
        datatype=column.datatype,
        default_agg="collect" if spatial else _measure_agg(column.datatype, geo, role),
        column=column.name,
        table=table,
        physical_table=physical,
        geo_role=geo,
        source_ref=EDITOR_REF,
    )


# ---------------------------------------------------------------- 資料模型


def add_table(
    ir: IR,
    datasource: str,
    source: InspectedFile,
    table: str | None,
    package_path: str,
    how: Literal["relationship", "join", "union"] = "relationship",
    target: str | None = None,
    join: JoinType = "inner",
    on: list[tuple[str, Comparison, str]] | None = None,
) -> IR:
    """資料源加上檔案中的一張資料表：

    - relationship：成為另一張邏輯資料表，以關聯連到 target（邏輯資料表 id，省略時為第一張）
    - join：在 target 邏輯資料表（不是 Relationship 模型時為整個資料源）中與新的資料表 join
    - union：與 target 的資料表上下相接

    on：條件（已有的欄位 id、比較、新資料表的欄名）；relationship 與 join 至少要一個條件。"""
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    found = source.table(table)
    name = _table_name(connection, found.name)
    added = file_table(source, table, package_path, name)
    if how != "union" and not on:
        raise EditError("要指定關聯或 join 的條件（兩邊的欄位）")
    tables = [*connection.tables, added]
    taken = _ids(ir)
    captions = {f.caption.casefold() for f in ds.fields}
    model = connection.relation if isinstance(connection.relation, ModelRelation) else None
    if how == "relationship":
        if model is None:
            ds, first = _to_model(ds, taken)
            logicals, relationships = [first], []
        else:
            logicals, relationships = list(model.tables), list(model.relationships)
        logical_id = _fresh(taken, _slug(name) + "_" + secrets.token_hex(4))
        new_fields = _fields(taken, added.columns, logical_id, captions, label=name)
        parent = target or logicals[0].id
        if parent not in {t.id for t in logicals}:
            raise EditError(f"找不到邏輯資料表 {parent}")
        links = _relationship_conditions(ds, parent, new_fields, added, on or [])
        logical = LogicalTable(
            id=logical_id,
            caption=name,
            relation=TableRelation(kind="table", name=name, table=[FILE_SCHEMA, name]),
        )
        built = ModelRelation(
            kind="model",
            tables=[*logicals, logical],
            relationships=[
                *relationships,
                Relationship(left=parent, right=logical_id, on=links),
            ],
        )
        connection = connection.model_copy(update={"tables": tables, "relation": built})
        ds = ds.model_copy(update={"connection": connection, "fields": [*ds.fields, *new_fields]})
        return _validated(_replace_datasource(ir, ds), "加入資料表失敗")
    # join、union：在一張邏輯資料表（或整個資料源）之內
    logical = _logical(model, target)
    current = logical.relation if logical is not None else connection.relation
    assert not isinstance(current, ModelRelation)
    owner = logical.id if logical is not None else None
    if how == "join":
        new_fields = _fields(taken, added.columns, owner or name, captions, name if owner else None)
        conditions: list[JoinCondition] = []
        for field_id, op, column in on or []:
            existing = _field(ds, field_id)
            if not _in_relation(existing, owner):
                raise EditError(f"欄位「{existing.caption}」不在要 join 的資料表中")
            if column not in {c.name for c in added.columns}:
                raise EditError(f"「{name}」沒有欄位 {column}")
            # Relationship 模型中還沒有 join 的邏輯資料表：欄位所屬的是它的資料表（或聯集、樞紐）
            left_table = (
                _single_name(current)
                if owner is not None and not isinstance(current, JoinRelation)
                else _physical_of(existing)
            )
            conditions.append(
                JoinCondition(
                    left=JoinTerm(table=left_table, column=existing.column),
                    op=op,
                    right=JoinTerm(table=name, column=column),
                )
            )
        right = TableRelation(kind="table", name=name, table=[FILE_SCHEMA, name])
        relation: Relation = JoinRelation(
            kind="join", join=join, left=current, right=right, on=conditions
        )
        fields = list(ds.fields)
        if owner is not None and not isinstance(current, JoinRelation):
            # 邏輯資料表第一次內含 join：已有的欄位記下所屬的實體資料表
            fields = [
                f.model_copy(update={"physical_table": _single_name(current)})
                if f.table == owner and f.column is not None
                else f
                for f in fields
            ]
        ds = ds.model_copy(update={"fields": [*fields, *new_fields]})
    else:
        relation, ds = _union(ds, current, owner, added, taken)
    connection = connection.model_copy(
        update={
            "tables": tables,
            "relation": _replace_logical(connection.relation, owner, relation),
        }
    )
    ds = ds.model_copy(update={"connection": connection})
    return _validated(_replace_datasource(ir, ds), "加入資料表失敗")


def _union(
    ds: Datasource,
    current: Relation,
    owner: str | None,
    added: FileTable,
    taken: set[str],
) -> tuple[Relation, Datasource]:
    """在資料表下面接上另一張資料表：欄位依名稱對應，另加 Table Name（Excel 另有 Sheet、文字檔另有 Path）。"""
    connection = _file_connection(ds)
    by_name = {t.name: t for t in connection.tables}
    if isinstance(current, UnionRelation):
        union = current
        parts = [*current.parts]
    elif isinstance(current, TableRelation) and current.name in by_name:
        first = by_name[current.name]
        union = UnionRelation(
            kind="union",
            name=_union_name(ds, current.name),
            parts=[_part(current, first)],
        )
        parts = list(union.parts)
    else:
        raise EditError("只有單一資料表（或已經是聯集）才能再接上其他資料表")
    new_part = _part(
        TableRelation(kind="table", name=added.name, table=[FILE_SCHEMA, added.name]), added
    )
    parts.append(new_part)
    union = union.model_copy(update={"parts": parts})
    table_ref = owner or union.name
    fields = list(ds.fields)
    if not isinstance(current, UnionRelation):
        # 原來的欄位改為聯集的欄位（Relationship 模型中仍屬於邏輯資料表）
        old = _single_name(current)
        fields = [
            f.model_copy(update={"table": table_ref})
            if f.table == old and f.column is not None
            else f
            for f in fields
        ]
    present = {f.column for f in fields if f.table == table_ref and f.column is not None}
    captions = {f.caption.casefold() for f in fields}
    extra: list[FileColumn] = [c for c in added.columns if c.name not in present]
    sources = ["Table Name"]
    formats = {by_name[p.relation.name].format for p in parts if p.relation.name in by_name} | {
        added.format
    }
    if "excel" in formats:
        sources.insert(0, "Sheet")
    if "text" in formats:
        sources.insert(0, "Path")
    extra += [
        FileColumn(name=s, datatype="string")
        for s in sources
        if s not in present and s not in {c.name for c in extra}
    ]
    new_fields = _fields(taken, extra, table_ref, captions)
    # 型別不一致的欄位：取能容納所有值的型別
    from tmig.sqlgen.source import _common_type

    types: dict[str, set[str]] = {}
    for part in parts:
        for column in part.columns or []:
            types.setdefault(column.name, set()).add(column.datatype)
    merged = [
        f.model_copy(update={"datatype": _common_type(types[f.column])})
        if f.table == table_ref
        and f.column in types
        and f.datatype != _common_type(types[f.column])
        else f
        for f in [*fields, *new_fields]
    ]
    return union, ds.model_copy(update={"fields": merged})


def _part(relation: TableRelation, table: FileTable) -> UnionPart:
    return UnionPart(
        relation=relation,
        columns=[UnionColumn(name=c.name, datatype=c.datatype) for c in table.columns],
        sheet=table.sheet if table.format == "excel" else None,
        path=table.path if table.format == "text" else None,
    )


def _union_name(ds: Datasource, first: str) -> str:
    base = f"{first}+"
    names = _relation_names(_file_connection(ds).relation)
    name, n = base, 1
    while name in names:
        n += 1
        name = f"{first}+{n}"
    return name


def remove_table(ir: IR, datasource: str, table: str) -> IR:
    """移除資料表（最後加入的 join、聯集的一張，或 Relationship 模型的一張邏輯資料表）；它的欄位不能
    有工作表或計算欄位用到。table：關聯樹中的資料表名稱或邏輯資料表 id。"""
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    top = connection.relation
    if isinstance(top, ModelRelation) and table in {t.id for t in top.tables}:
        return _remove_logical(ir, ds, top, table)
    owner, relation = _owner_of(top, table)
    if isinstance(relation, JoinRelation):
        if _single_name(relation.right) != table:
            raise EditError(f"只能移除最後加入的資料表（「{_single_name(relation.right)}」）")
        removed = [
            f
            for f in ds.fields
            if f.column is not None and _physical_of(f) == table and _in_relation(f, owner)
        ]
        _check_unused(ir, ds, removed)
        replacement: Relation = relation.left
        fields = [f for f in ds.fields if f not in removed]
        if owner is not None and not isinstance(replacement, JoinRelation):
            fields = [
                f.model_copy(update={"physical_table": None}) if f.table == owner else f
                for f in fields
            ]
    elif isinstance(relation, UnionRelation):
        parts = [p for p in relation.parts if p.relation.name != table]
        if len(parts) == len(relation.parts):
            raise EditError(f"聯集中沒有資料表「{table}」")
        ref = owner or relation.name
        # 留下的資料表都沒有的欄（只在移除的資料表中的欄、不再需要的 Sheet、Path 等）一併移除
        types: dict[str, set[str]] = {}
        for part in parts:
            for column in part.columns or []:
                types.setdefault(column.name, set()).add(column.datatype)
        kept = set(types)
        if len(parts) > 1:
            kept.add("Table Name")
            if any(p.sheet is not None for p in parts):
                kept.add("Sheet")
            if any(p.path is not None for p in parts):
                kept.add("Path")
        removed = [
            f
            for f in ds.fields
            if f.table == ref and f.column is not None and f.calc is None and f.column not in kept
        ]
        _check_unused(ir, ds, removed)
        from tmig.sqlgen.source import _common_type

        def narrowed(f: DataField) -> DataField:
            if f.table != ref or f.column not in types:
                return f
            datatype = _common_type(types[f.column])
            return f if f.datatype == datatype else f.model_copy(update={"datatype": datatype})

        fields = [narrowed(f) for f in ds.fields if f not in removed]
        if len(parts) > 1:
            replacement = relation.model_copy(update={"parts": parts})
        else:
            (last,) = parts
            replacement = last.relation
            fields = [
                f.model_copy(update={"table": owner or last.relation.name}) if f.table == ref else f
                for f in fields
            ]
    else:
        raise EditError(f"找不到可以移除的資料表「{table}」")
    tables = [t for t in connection.tables if t.name != table]
    connection = connection.model_copy(
        update={"tables": tables, "relation": _replace_logical(top, owner, replacement)}
    )
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "移除資料表失敗")


def _remove_logical(ir: IR, ds: Datasource, model: ModelRelation, logical: str) -> IR:
    edges = [r for r in model.relationships if logical in (r.left, r.right)]
    if len(edges) > 1:
        raise EditError("這張邏輯資料表還連著其他資料表，先移除它們")
    removed = [f for f in ds.fields if f.table == logical]
    _check_unused(ir, ds, [f for f in removed if f.column is not None])
    _check_unused(ir, ds, [f for f in removed if f.column is None])
    gone = next(t for t in model.tables if t.id == logical)
    tables = [t for t in model.tables if t.id != logical]
    relationships = [r for r in model.relationships if logical not in (r.left, r.right)]
    fields = [f for f in ds.fields if f not in removed]
    connection = _file_connection(ds)
    leaves = _leaf_names(gone.relation)
    file_tables = [t for t in connection.tables if t.name not in leaves]
    if len(tables) == 1:
        # 只剩一張邏輯資料表：不再是 Relationship 模型
        (only,) = tables
        relation: Any = only.relation
        fields = [_from_model(f, only) for f in fields]
    else:
        relation = model.model_copy(update={"tables": tables, "relationships": relationships})
    connection = connection.model_copy(update={"tables": file_tables, "relation": relation})
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "移除資料表失敗")


def set_join(
    ir: IR,
    datasource: str,
    table: str,
    join: JoinType,
    on: list[tuple[str, Comparison, str]],
) -> IR:
    """改變資料表 join 的方式與條件（條件：已有的欄位 id、比較、這張資料表的欄名）。"""
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    owner, relation = _owner_of(connection.relation, table)
    if not isinstance(relation, JoinRelation) or _single_name(relation.right) != table:
        raise EditError(f"「{table}」不是 join 的資料表")
    if not on:
        raise EditError("join 至少要一個條件")
    columns = {f.column for f in ds.fields if _physical_of(f) == table and _in_relation(f, owner)}
    conditions: list[JoinCondition] = []
    for field_id, op, column in on:
        existing = _field(ds, field_id)
        if _physical_of(existing) == table or not _in_relation(existing, owner):
            raise EditError(f"欄位「{existing.caption}」不在 join 的另一邊")
        if column not in columns:
            raise EditError(f"「{table}」沒有欄位 {column}")
        conditions.append(
            JoinCondition(
                left=JoinTerm(table=_physical_of(existing), column=existing.column),
                op=op,
                right=JoinTerm(table=table, column=column),
            )
        )
    changed = relation.model_copy(update={"join": join, "on": conditions})
    connection = connection.model_copy(
        update={"relation": _replace_logical(connection.relation, owner, changed)}
    )
    ds = ds.model_copy(update={"connection": connection})
    return _validated(_replace_datasource(ir, ds), "修改 join 失敗")


def set_relationship(
    ir: IR, datasource: str, logical: str, on: list[tuple[str, Comparison, str]]
) -> IR:
    """改變邏輯資料表的關聯條件（條件：另一張資料表的欄位 id、比較、這張資料表的欄位 id）。"""
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    model = connection.relation
    if not isinstance(model, ModelRelation):
        raise EditError("資料源不是 Relationship 模型")
    index = next((i for i, r in enumerate(model.relationships) if r.right == logical), None)
    if index is None:
        raise EditError(f"邏輯資料表 {logical} 沒有關聯到其他資料表")
    rel = model.relationships[index]
    if not on:
        raise EditError("關聯至少要一個條件")
    conditions: list[RelationshipCondition] = []
    for left_id, op, right_id in on:
        left, right = _field(ds, left_id), _field(ds, right_id)
        if left.table != rel.left or right.table != rel.right:
            raise EditError(f"欄位「{left.caption}」「{right.caption}」不是這個關聯兩邊的資料表")
        conditions.append(RelationshipCondition(left=left.id, op=op, right=right.id))
    relationships = list(model.relationships)
    relationships[index] = rel.model_copy(update={"on": conditions})
    connection = connection.model_copy(
        update={"relation": model.model_copy(update={"relationships": relationships})}
    )
    ds = ds.model_copy(update={"connection": connection})
    return _validated(_replace_datasource(ir, ds), "修改關聯失敗")


def pivot(ir: IR, datasource: str, table: str, columns: list[str]) -> IR:
    """把資料表的幾個欄樞紐成列（Tableau 資料來源分頁的「樞紐」）：Pivot Field Names 是欄名、
    Pivot Field Values 是值；被樞紐的欄位不能有工作表或計算欄位用到。"""
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    owner, relation = _owner_of(connection.relation, table)
    if not isinstance(relation, TableRelation):
        raise EditError("只有單一資料表可以樞紐")
    by_column = {
        f.column: f for f in ds.fields if f.column is not None and _belongs(f, owner, table)
    }
    if len(columns) < 1 or any(c not in by_column for c in columns):
        raise EditError("要選取資料表中的欄位來樞紐")
    pivoted = [by_column[c] for c in columns]
    _check_unused(ir, ds, pivoted)
    types = {f.datatype for f in pivoted}
    from tmig.sqlgen.source import _common_type

    datatype = _common_type(types)
    names = _relation_names(connection.relation)
    name, n = "Pivot", 1
    while name in names:
        n += 1
        name = f"Pivot {n}"
    changed = PivotRelation(
        kind="pivot",
        name=name,
        source=relation,
        names="Pivot Field Names",
        values="Pivot Field Values",
        datatype=datatype,  # type: ignore[arg-type]
        columns=[PivotColumn(column=c, label=c) for c in columns],
    )
    taken = _ids(ir)
    captions = {f.caption.casefold() for f in ds.fields if f not in pivoted}
    ref = owner or name
    added = _fields(
        taken,
        [
            FileColumn(name="Pivot Field Names", datatype="string"),
            FileColumn(name="Pivot Field Values", datatype=datatype),  # type: ignore[arg-type]
        ],
        ref,
        captions,
    )
    added[0] = added[0].model_copy(update={"role": "dimension", "default_agg": None})
    fields = [f for f in ds.fields if f not in pivoted] + added
    connection = connection.model_copy(
        update={"relation": _replace_logical(connection.relation, owner, changed)}
    )
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "樞紐失敗")


def unpivot(ir: IR, datasource: str, name: str) -> IR:
    """移除樞紐：被樞紐的欄恢復成欄位；樞紐產生的兩個欄位不能有工作表或計算欄位用到。"""
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    owner, relation = _owner_of(connection.relation, name)
    if not isinstance(relation, PivotRelation):
        raise EditError(f"「{name}」不是樞紐")
    ref = owner or name
    generated = [
        f for f in ds.fields if f.table == ref and f.column in (relation.names, relation.values)
    ]
    _check_unused(ir, ds, generated)
    source = relation.source
    by_name = {t.name: t for t in connection.tables}
    columns = by_name[source.name].columns if source.name in by_name else []
    restored_columns = [c for c in columns if c.name in {p.column for p in relation.columns}]
    taken = _ids(ir)
    captions = {f.caption.casefold() for f in ds.fields if f not in generated}
    restored = _fields(taken, restored_columns, owner or source.name, captions)
    fields = [f for f in ds.fields if f not in generated] + restored
    connection = connection.model_copy(
        update={"relation": _replace_logical(connection.relation, owner, source)}
    )
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "移除樞紐失敗")


# ---------------------------------------------------------------- 欄位


def update_field(
    ir: IR,
    datasource: str,
    field_id: str,
    caption: str | None = None,
    hidden: bool | None = None,
    role: Literal["dimension", "measure"] | None = None,
    default_agg: str | None = None,
    datatype: str | None = None,
) -> IR:
    """欄位的顯示名稱、隱藏、角色、預設彙總與資料類型（檔案的欄位以新的型別重新匯入）。"""
    ds = _datasource(ir, datasource)
    target = _field(ds, field_id)
    update: dict[str, Any] = {}
    if caption is not None:
        caption = caption.strip()
        if not caption:
            raise EditError("欄位名稱不能是空的")
        if any(f.caption.casefold() == caption.casefold() and f.id != field_id for f in ds.fields):
            raise EditError(f"已經有名為「{caption}」的欄位")
        update["caption"] = caption
    if hidden is not None:
        update["hidden"] = hidden
    if role is not None and role != target.role:
        _check_unused(ir, ds, [target])
        update["role"] = role
        update["default_agg"] = _measure_agg(target.datatype, target.geo_role, role)
    if default_agg is not None:
        if (update.get("role") or target.role) != "measure":
            raise EditError("只有量值有預設的彙總")
        update["default_agg"] = default_agg
    connection = ds.connection
    if datatype is not None and datatype != target.datatype:
        if not isinstance(connection, FileConnection) or target.column is None:
            raise EditError("只有檔案的欄位可以變更資料類型")
        if datatype not in ("string", "integer", "real", "boolean", "date", "datetime"):
            raise EditError(f"不認得的資料類型 {datatype}")
        _check_unused(ir, ds, [target])
        table = _file_of(connection, target)
        tables = [
            t.model_copy(
                update={
                    "columns": [
                        c.model_copy(update={"datatype": datatype})
                        if c.name == target.column
                        else c
                        for c in t.columns
                    ]
                }
            )
            if t.name == table
            else t
            for t in connection.tables
        ]
        if tables == connection.tables:
            raise EditError(f"欄位「{target.caption}」不能變更資料類型（不是資料表中的欄位）")
        connection = connection.model_copy(update={"tables": tables})
        numeric = datatype in ("integer", "real")
        geo = target.geo_role
        if not numeric and geo in ("latitude", "longitude"):
            geo = None
        update["datatype"] = datatype
        update["role"] = "measure" if numeric else "dimension"
        update["default_agg"] = _measure_agg(datatype, geo, update["role"])
        update["geo_role"] = geo
    if not update:
        return ir
    fields = [f.model_copy(update=update) if f.id == field_id else f for f in ds.fields]
    ds = ds.model_copy(update={"fields": fields, "connection": connection})
    return _validated(_replace_datasource(ir, ds), "修改欄位失敗")


def rename_datasource(ir: IR, datasource: str, caption: str) -> IR:
    ds = _datasource(ir, datasource)
    caption = caption.strip()
    if not caption:
        raise EditError("資料源名稱不能是空的")
    return _validated(
        _replace_datasource(ir, ds.model_copy(update={"caption": caption})), "改名失敗"
    )


# ---------------------------------------------------------------- 工具


def _file_connection(ds: Datasource) -> FileConnection:
    connection = ds.connection
    if not isinstance(connection, FileConnection):
        raise EditError(
            f"資料源「{ds.caption}」不是檔案的資料源（Live 連線與萃取尚不能在這裡編輯）"
        )
    return connection


def _field(ds: Datasource, field_id: str) -> DataField:
    found = next((f for f in ds.fields if f.id == field_id), None)
    if found is None:
        raise EditError(f"找不到欄位 {field_id}")
    return found


def _fresh(taken: set[str], base: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", base.lower())[:56].strip("_") or "x"
    if not base[0].isalpha():
        base = "x_" + base
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    taken.add(name)
    return name


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "x"


def _table_name(connection: FileConnection, base: str) -> str:
    """關聯樹中的資料表名稱不重複（同一個檔案加入兩次時加上序號）。"""
    names = {t.name for t in connection.tables} | _relation_names(connection.relation)
    name, n = base, 1
    while name in names:
        n += 1
        name = f"{base} ({n})"
    return name


def _relation_names(relation: Any) -> set[str]:
    if isinstance(relation, ModelRelation):
        return {name for t in relation.tables for name in _relation_names(t.relation)}
    if isinstance(relation, JoinRelation):
        return _relation_names(relation.left) | _relation_names(relation.right)
    if isinstance(relation, PivotRelation):
        return {relation.name, relation.source.name}
    if isinstance(relation, UnionRelation):
        return {relation.name} | {p.relation.name for p in relation.parts}
    return {relation.name}


def _leaf_names(relation: Relation) -> set[str]:
    if isinstance(relation, JoinRelation):
        return _leaf_names(relation.left) | _leaf_names(relation.right)
    if isinstance(relation, PivotRelation):
        return {relation.source.name}
    if isinstance(relation, UnionRelation):
        return {p.relation.name for p in relation.parts}
    return {getattr(relation, "name", "")}


def _single_name(relation: Relation) -> str:
    """單一資料表（或聯集、樞紐）在關聯樹中的名稱。"""
    if isinstance(relation, JoinRelation):
        raise EditError("join 的一邊不是單一資料表")
    return relation.name


def _logical(model: ModelRelation | None, target: str | None) -> LogicalTable | None:
    if model is None:
        return None
    if target is None:
        return model.tables[0]
    found = next((t for t in model.tables if t.id == target), None)
    if found is None:
        raise EditError(f"找不到邏輯資料表 {target}")
    return found


def _replace_logical(top: Any, owner: str | None, relation: Relation) -> Any:
    if owner is None:
        return relation
    assert isinstance(top, ModelRelation)
    tables = [
        t.model_copy(update={"relation": relation}) if t.id == owner else t for t in top.tables
    ]
    return top.model_copy(update={"tables": tables})


def _owner_of(top: Any, table: str) -> tuple[str | None, Relation]:
    """含有這張資料表（或聯集、樞紐）的邏輯資料表 id（不是 Relationship 模型時為 None）與它的關聯樹。"""
    if isinstance(top, ModelRelation):
        for logical in top.tables:
            if table in _relation_names(logical.relation):
                return logical.id, logical.relation
        raise EditError(f"找不到資料表「{table}」")
    if table not in _relation_names(top):
        raise EditError(f"找不到資料表「{table}」")
    return None, top


def _physical_of(f: DataField) -> str | None:
    return f.physical_table or f.table


def _file_of(connection: FileConnection, f: DataField) -> str | None:
    """欄位所在的檔案中的資料表（單一資料表或 join 的一邊；聯集、樞紐的欄位為 None）。"""
    top = connection.relation
    if isinstance(top, ModelRelation):
        logical = next((t for t in top.tables if t.id == f.table), None)
        if logical is None:
            return None
        if isinstance(logical.relation, JoinRelation):
            return f.physical_table
        return logical.relation.name if isinstance(logical.relation, TableRelation) else None
    names = {t.name for t in connection.tables}
    return f.table if f.table in names else None


def _in_relation(f: DataField, owner: str | None) -> bool:
    return f.column is not None and (owner is None or f.table == owner)


def _belongs(f: DataField, owner: str | None, table: str) -> bool:
    return (f.table == owner) if owner is not None else (f.table == table)


def _to_model(ds: Datasource, taken: set[str]) -> tuple[Datasource, LogicalTable]:
    """單一的資料表（或 join、聯集、樞紐）改成 Relationship 模型的第一張邏輯資料表。"""
    connection = _file_connection(ds)
    relation = connection.relation
    assert not isinstance(relation, ModelRelation)
    caption = relation.name if not isinstance(relation, JoinRelation) else _leftmost(relation)
    logical_id = _fresh(taken, _slug(caption) + "_" + secrets.token_hex(4))
    joined = isinstance(relation, JoinRelation)
    fields = [
        f.model_copy(update={"table": logical_id, "physical_table": f.table if joined else None})
        if f.column is not None or f.table is not None
        else f
        for f in ds.fields
    ]
    logical = LogicalTable(id=logical_id, caption=caption, relation=relation)
    return ds.model_copy(update={"fields": fields}), logical


def _from_model(f: DataField, only: LogicalTable) -> DataField:
    """Relationship 模型只剩一張邏輯資料表：欄位改回屬於實體的資料表。"""
    if f.table != only.id:
        return f
    relation = only.relation
    if isinstance(relation, JoinRelation):
        return f.model_copy(update={"table": f.physical_table, "physical_table": None})
    if isinstance(relation, PivotRelation):
        generated = f.column in (relation.names, relation.values)
        return f.model_copy(update={"table": relation.name if generated else relation.source.name})
    return f.model_copy(update={"table": relation.name})


def _leftmost(relation: Relation) -> str:
    while isinstance(relation, JoinRelation):
        relation = relation.left
    return relation.name


def _relationship_conditions(
    ds: Datasource,
    parent: str,
    new_fields: list[DataField],
    added: FileTable,
    on: list[tuple[str, Comparison, str]],
) -> list[RelationshipCondition]:
    by_column = {f.column: f for f in new_fields}
    conditions: list[RelationshipCondition] = []
    for field_id, op, column in on:
        existing = _field(ds, field_id)
        if existing.table != parent or existing.column is None:
            raise EditError(f"欄位「{existing.caption}」不在要關聯的資料表中")
        if column not in by_column:
            raise EditError(f"「{added.name}」沒有欄位 {column}")
        conditions.append(
            RelationshipCondition(left=existing.id, op=op, right=by_column[column].id)
        )
    return conditions


def _check_unused(ir: IR, ds: Datasource, fields: list[DataField]) -> None:
    """欄位沒有工作表、計算欄位、集合、階層或篩選用到。"""
    ids = {f.id for f in fields}
    if not ids:
        return
    names = {f.name for f in fields}
    for other in ds.fields:
        if other.id in ids or other.calc is None:
            continue
        try:
            node = parse_formula(other.calc.formula)
        except FormulaSyntaxError:
            continue
        refs = {f"[{n.parts[-1]}]" for n in walk(node) if isinstance(n, FieldRef)}
        if refs & names:
            raise EditError(f"計算欄位「{other.caption}」用到這些欄位，先修改它")
    for hierarchy in ds.hierarchies:
        if ids & set(hierarchy.fields):
            raise EditError(f"階層「{hierarchy.name}」用到這些欄位，先移除它們")
    places: list[tuple[str, Any]] = [(w.name, w.model_dump(mode="json")) for w in ir.worksheets]
    places += [("資料源篩選", f.model_dump(mode="json")) for f in ds.filters]
    places += [(a.name, a.model_dump(mode="json")) for a in ir.actions]
    for where, data in places:
        if _mentions(data, ids):
            raise EditError(f"「{where}」用到這些欄位，先移除它們")


def _mentions(value: Any, ids: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_mentions(v, ids) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, ids) for v in value)
    return isinstance(value, str) and value in ids


__all__ = [
    "DATA_DIR",
    "InspectedFile",
    "InspectedTable",
    "add_datasource",
    "add_table",
    "file_table",
    "inspect_file",
    "new_workbook",
    "pivot",
    "remove_table",
    "rename_datasource",
    "set_join",
    "set_relationship",
    "unpivot",
    "update_field",
]
