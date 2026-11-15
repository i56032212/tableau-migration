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
  join、聯集（union）或把欄樞紐（pivot）成列。條件的一邊可以是公式（Calculated）：join 計算存成以
  [資料表].[欄位] 參照實體欄位的公式（JoinTerm.formula），關聯計算存成所屬邏輯資料表上隱藏的計算欄位
  （DataField.relationship_calc，公式以欄位的內部名稱參照）；公式中的欄位以顯示名稱（或欄名）書寫
- 欄位（update_field）：改名（顯示名稱）、隱藏、角色、預設彙總與資料類型（檔案的欄位以新的型別重新匯入）
- 資料解譯器（inspect_file 的 interpreter、set_interpreter）：Excel 的工作表上面有標題、說明的列時，找出
  標題列（至少兩格有值、都是文字或整數，下一個有值的列至少一半的欄有值），之前的列與之後空白的列不算
- 萬用字元聯集（add_wildcard_union）：資料夾中檔名（與 Excel 工作表名稱）符合模式的資料表接在資料表下面，
  另有 Path（檔案的路徑）與 Sheet；檔案複製進報表（建立時的快照）
- 跨資料庫的 join（Tableau 的 cross-database join）：檔案的資料源加上資料庫的資料表（add_live_table 指定
  connection），或資料庫的資料源加上檔案、另一個資料庫的資料表（先以 federate 改成跨資料庫的 join）；
  資料庫的資料表在查詢時取回、與檔案在本機合併（tmig.data.federated）。移除資料表後只剩同一個資料庫時改回
  一般的 Live 連線

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

from tmig.data.file_import import excel_bounds, named_ranges
from tmig.edit import (
    EDITOR_REF,
    EditError,
    _datasource,
    _replace_datasource,
    _unique_id,
    _validated,
    add_worksheet,
    check_formula,
    check_unused,
    prune_ad_hoc,
)
from tmig.ir import (
    FILE_SCHEMA,
    IR,
    IR_VERSION,
    LIVE_PATH_PREFIX,
    DataField,
    Datasource,
    DatasourceSettings,
    FileColumn,
    FileConnection,
    FileTable,
    JoinCondition,
    JoinRelation,
    JoinTerm,
    LiveConnection,
    LogicalTable,
    ModelRelation,
    PivotColumn,
    PivotRelation,
    ProjectRelation,
    Relationship,
    RelationshipCondition,
    SqlRelation,
    TableRelation,
    UnionColumn,
    UnionPart,
    UnionRelation,
    UnionWildcard,
    Workbook,
)
from tmig.ir.federation import map_leaves, single_database, to_federated
from tmig.ir.models import Aggregation, GeoRole, Relation

CUSTOM_SQL = "Custom SQL Query"
"""自訂 SQL 在關聯樹中的名稱（與 Tableau 相同，重複時加上序號）。"""
DATA_DIR = "Data"
"""檔案在報表中的資料夾：FileTable.path 為 Data/<檔名>（與 .twbx 相同的寫法）。"""
SAMPLE_ROWS = 10_000
"""判斷型別時讀的列數（匯入時仍讀全部的列，不符合型別的值成為 Null）。"""
EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls")
TEXT_SUFFIXES = (".csv", ".tsv", ".txt", ".tab")
SPATIAL_SUFFIXES = (
    ".shp",
    ".zip",
    ".geojson",
    ".json",
    ".kml",
    ".kmz",
    ".topojson",
    ".tab",
    ".mif",
    ".gdb",
)
"""空間檔案（.gdb 是 Esri File Geodatabase 的資料夾，.tab、.mif 是 MapInfo）。"""
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
Comparison = Literal["=", "!=", "<", "<=", ">", ">=", "intersects"]
"""條件的比較；intersects 是空間 join（兩邊都是空間欄位或傳回形狀的 join 計算，只能用在 join）。"""


@dataclass(frozen=True)
class Calculated:
    """join 或關聯條件的一邊是公式（Tableau 的 join 計算、關聯計算）；欄位以顯示名稱或欄名參照。"""

    formula: str


Side = str | Calculated
"""條件的一邊：欄位 id（新資料表的一邊為欄名），或公式。"""
Condition = tuple[Side, Comparison, Side]


# ---------------------------------------------------------------- 檢查檔案


@dataclass
class InspectedTable:
    """檔案中的一張資料表：Excel 的工作表、文字檔本身或空間檔案的圖層。"""

    name: str
    columns: list[FileColumn]
    rows: int
    sheet: str | None = None
    layer: str | None = None
    interpreted: bool = False
    """資料解譯器找出的標題列（Excel）。"""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sheet": self.sheet,
            "layer": self.layer,
            "rows": self.rows,
            "interpreted": self.interpreted,
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


def inspect_file(path: Path, interpreter: bool = False) -> InspectedFile:
    """檔案的資料表、欄名、型別與列數；interpreter 時 Excel 與文字檔依資料解譯器找標題列。
    Excel 的具名範圍也列為資料表（Tableau 的連線畫面同樣列出）。"""
    suffix = path.suffix.lower()
    if suffix == ".gdb" and path.is_dir():
        return _inspect_spatial(path, b"")
    if not path.is_file():
        raise EditError(f"找不到檔案：{path}")
    data = path.read_bytes()
    if suffix == ".tab" and data.lstrip()[:6].lower() == b"!table":
        # .tab 可以是 Tab 分隔的文字檔或 MapInfo 的表格（第一行是 !table）
        return _inspect_spatial(path, data)
    if suffix in EXCEL_SUFFIXES:
        return InspectedFile(path, "excel", _inspect_excel(path.name, data, interpreter))
    if suffix in TEXT_SUFFIXES:
        table, separator, charset = _inspect_text(path.name, data, interpreter)
        return InspectedFile(path, "text", [table], separator, charset)
    if suffix in SPATIAL_SUFFIXES:
        return _inspect_spatial(path, data)
    raise EditError(
        f"不認得的檔案類型：{path.name}（支援 Excel、文字檔與 Shapefile、GeoJSON、KML、TopoJSON、"
        "Esri File Geodatabase、MapInfo）"
    )


def _inspect_excel(name: str, data: bytes, interpreter: bool = False) -> list[InspectedTable]:
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
        # 具名範圍：也是一張資料表（名稱與工作表相同的不列）
        for range_name, (title, cells) in sorted(named_ranges(book).items()):
            if range_name in book.sheetnames or title not in book.sheetnames:
                continue
            first_col, first_row, last_col, last_row = excel_bounds(cells)
            grid = [
                list(cells_row)
                for cells_row in book[title].iter_rows(
                    min_row=first_row,
                    max_row=last_row,
                    min_col=first_col,
                    max_col=last_col,
                    values_only=True,
                )
            ]
            if grid:
                grids.append((range_name, grid[: SAMPLE_ROWS + 1], max(len(grid) - 1, 0)))
        book.close()
    tables: list[InspectedTable] = []
    for title, grid, rows in grids:
        if interpreter:
            header = interpret_header(grid)
            if header is not None:
                body = [r for r in grid[header + 1 :] if not all(_blank(v) for v in r)]
                columns = _columns(grid[header], body, excel=True)
                if columns:
                    tables.append(
                        InspectedTable(title, columns, len(body), sheet=title, interpreted=True)
                    )
                continue
        if not grid or not any(not _blank(v) for v in grid[0]):
            continue  # 空白的工作表
        columns = _columns(grid[0], grid[1:], excel=True)
        if columns:
            tables.append(InspectedTable(title, columns, rows, sheet=title))
    if not tables:
        raise EditError(f"{name} 中沒有資料（每張工作表的第一列都是空白）")
    return tables


def interpret_header(grid: list[list[Any]]) -> int | None:
    """資料解譯器（Tableau 的 Data Interpreter）找標題列：第一個至少兩格有值、有值的格都是文字或整數（例如
    年份）且有值的格數至少是最寬的列的一半，下一個有值的列也至少一半的欄有值的列；找不到時為 None。
    Tableau 的判斷方式沒有公開，這是推定的規則（以公開報表的來源檔驗證）。"""
    filled = [sum(not _blank(v) for v in row) for row in grid]
    widest = max(filled, default=0)
    for index, row in enumerate(grid):
        count = filled[index]
        if count < 2 or count * 2 < widest:
            continue
        values = [v for v in row if not _blank(v)]
        if not all(
            isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool)) for v in values
        ):
            continue
        following = next((j for j in range(index + 1, len(grid)) if filled[j] > 0), None)
        if following is None or filled[following] * 2 < count:
            continue
        return index
    return None


def _inspect_text(
    name: str, data: bytes, interpreter: bool = False
) -> tuple[InspectedTable, str, str]:
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
    stem = PurePosixPath(name).name
    if interpreter:
        # 資料解譯器：檔案上面有標題、說明的列時，找出標題列，之前的列與空白的列不算
        header = interpret_header(grid)
        if header is not None:
            body = [r for r in grid[header + 1 :] if not all(_blank(v) for v in r)]
            columns = _columns(grid[header], body, excel=False)
            if columns:
                rows = len(body) if count <= SAMPLE_ROWS + 1 else max(count - header - 1, 0)
                table = InspectedTable(stem, columns, rows, interpreted=True)
                return table, separator, charset
    columns = _columns(grid[0], grid[1:], excel=False)
    return InspectedTable(stem, columns, max(count - 1, 0)), separator, charset


def _inspect_spatial(path: Path, data: bytes) -> InspectedFile:
    from tmig.data.ogr_files import (
        GDB_SUFFIX,
        MAPINFO_MIF,
        MAPINFO_TAB,
        OgrError,
        gdb_in_zip,
        ogr_layers,
    )
    from tmig.data.spatial_files import (
        GEOMETRY_COLUMN,
        SHAPEFILE_PARTS,
        SpatialFileError,
        read_spatial,
    )

    files = {path.name: data}
    sidecars: list[Path] = []
    suffix = path.suffix.lower()
    if suffix == GDB_SUFFIX:
        # File Geodatabase 的資料夾：其中的每個檔案（複製到報表時整個資料夾一起複製）
        files = {
            f"{path.name}/{f.relative_to(path).as_posix()}": f.read_bytes()
            for f in sorted(path.rglob("*"))
            if f.is_file()
        }
    if suffix in (".tab", ".mif"):
        # MapInfo 的同名檔案（.dat、.map、.id、.ind 或 .mid）
        group = MAPINFO_TAB if suffix == ".tab" else MAPINFO_MIF
        for candidate in sorted(path.parent.iterdir()):
            if (
                candidate != path
                and candidate.is_file()
                and candidate.stem.lower() == path.stem.lower()
                and candidate.suffix.lower() in group
            ):
                files[path.stem + candidate.suffix.lower()] = candidate.read_bytes()
                sidecars.append(candidate)
    if suffix == ".shp":
        for ext in SHAPEFILE_PARTS[1:]:
            for candidate in (path.with_suffix(ext), path.with_suffix(ext.upper())):
                if candidate.is_file():
                    files[candidate.name if candidate.suffix == ext else path.stem + ext] = (
                        candidate.read_bytes()
                    )
                    sidecars.append(candidate)
                    break
    layers: list[str | None] = [None]
    if suffix == ".zip":
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
        shapes = [n for n in names if n.lower().endswith(".shp")]
        layers = [PurePosixPath(n).stem for n in shapes] or [None]
        if gdb_in_zip(names):
            try:
                layers = list(ogr_layers(files, path.name)) or [None]
            except OgrError as exc:
                raise EditError(str(exc)) from exc
    if suffix == GDB_SUFFIX:
        try:
            layers = list(ogr_layers(files, path.name)) or [None]
        except OgrError as exc:
            raise EditError(str(exc)) from exc
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
            name=name,
            format="excel",
            path=package_path,
            sheet=found.sheet,
            interpreted=found.interpreted,
            columns=found.columns,
        )
    if source.format == "text":
        return FileTable(
            name=name,
            format="text",
            path=package_path,
            separator=source.separator,
            charset=source.charset,
            locale=TEXT_LOCALE,
            interpreted=found.interpreted,
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


# ---------------------------------------------------------------- 資料庫（Live 連線）


def live_datasource(
    taken: set[str],
    connection: LiveConnection,
    table: list[str] | None,
    sql: str | None,
    columns: list[FileColumn],
) -> Datasource:
    """連到資料庫的資料源（Tableau 的「連線到伺服器」）：一張資料表或自訂 SQL。connection 是連線本身
    （relation 會換成這張資料表），憑證不在其中（依 connection_id 由連線設定檔提供）。"""
    new = _live_new_table(None, table, sql, columns)
    caption = f"{new.name} ({connection.database})" if connection.database else new.name
    ds_id = _fresh(taken, "ds_" + _slug(caption))
    fields = _fields(taken | {ds_id}, new.columns, new.name, set())
    return Datasource(
        id=ds_id,
        name=f"federated.{secrets.token_hex(14)}",
        caption=caption,
        connection=connection.model_copy(update={"relation": new.relation}),
        settings=DatasourceSettings(week_start="sunday"),
        fields=fields,
        source_ref=EDITOR_REF,
    )


def new_live_workbook(
    name: str,
    connection: LiveConnection,
    table: list[str] | None,
    sql: str | None,
    columns: list[FileColumn],
) -> IR:
    """連到資料庫的新報表：一個資料源與空白的「工作表 1」。"""
    ds = live_datasource(set(), connection, table, sql, columns)
    workbook = Workbook(
        id="wb_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "report"),
        name=name.strip() or "報表",
        tableau_version=None,
        source_ref=EDITOR_REF,
    )
    ir = IR(ir_version=IR_VERSION, workbook=workbook, datasources=[ds])  # type: ignore[arg-type]
    ir, _ = add_worksheet(ir, None, ds.id)
    return ir


def add_live_datasource(
    ir: IR,
    connection: LiveConnection,
    table: list[str] | None,
    sql: str | None,
    columns: list[FileColumn],
) -> tuple[IR, Datasource]:
    """報表加上連到資料庫的資料源。"""
    ds = live_datasource(_ids(ir), connection, table, sql, columns)
    updated = ir.model_copy(update={"datasources": [*ir.datasources, ds]})
    return _validated(updated, "新增資料源失敗"), ds


def set_custom_sql(ir: IR, datasource: str, name: str, sql: str, columns: list[FileColumn]) -> IR:
    """修改自訂 SQL（Tableau 的「編輯自訂 SQL 查詢」）：沿用名稱相同的欄位（型別依新的查詢），新的欄
    加入成欄位；不再有的欄移除（工作表或其他欄位用到時退回）。"""
    ds = _datasource(ir, datasource)
    connection = _editable(ds)
    if isinstance(connection, FileConnection):
        return _set_remote_sql(ir, ds, connection, name, sql, columns)
    top = _relation_of(connection)
    owner, current = _owner_of(top, name)
    relation = _find_relation(current, name)
    if not isinstance(relation, SqlRelation):
        raise EditError(f"「{name}」不是自訂 SQL")
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise EditError("自訂 SQL 是空的")
    if not columns:
        raise EditError("自訂 SQL 沒有傳回任何欄")
    fields = _resync(ir, ds, name, columns, owner, isinstance(current, JoinRelation))
    changed = _replace_leaf(current, name, relation.model_copy(update={"sql": text}))
    connection = _with(connection, None, _replace_logical(top, owner, changed))
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "修改自訂 SQL 失敗")


def _set_remote_sql(
    ir: IR,
    ds: Datasource,
    connection: FileConnection,
    name: str,
    sql: str,
    columns: list[FileColumn],
) -> IR:
    """跨資料庫的 join 中資料庫的自訂 SQL：換掉 SQL 與欄。"""
    found = next((t for t in connection.tables if t.name == name), None)
    if found is None or found.sql is None:
        raise EditError(f"「{name}」不是自訂 SQL")
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise EditError("自訂 SQL 是空的")
    if not columns:
        raise EditError("自訂 SQL 沒有傳回任何欄")
    owner, current = _owner_of(_relation_of(connection), name)
    fields = _resync(ir, ds, name, columns, owner, isinstance(current, JoinRelation))
    tables = [
        t.model_copy(update={"sql": text, "columns": list(columns)}) if t.name == name else t
        for t in connection.tables
    ]
    connection = connection.model_copy(update={"tables": tables})
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "修改自訂 SQL 失敗")


def _resync(
    ir: IR,
    ds: Datasource,
    name: str,
    columns: list[FileColumn],
    owner: str | None,
    joined: bool,
) -> list[DataField]:
    """資料表（自訂 SQL 或檔案的資料表）換成新的欄：名稱相同的欄位沿用（型別依新的欄），新的欄加入成欄位，
    不再有的欄移除（工作表或其他欄位用到時退回）。回傳資料源的新欄位清單。"""

    def mine(f: DataField) -> bool:
        if f.column is None:
            return False
        if f.physical_table is not None:
            return f.physical_table == name
        return f.table == (owner or name) if not joined or owner is None else False

    own = {str(f.column): f for f in ds.fields if mine(f)}
    by_name = {c.name: c for c in columns}
    removed = [f for column, f in own.items() if column not in by_name]
    _check_unused(ir, ds, removed)
    fields: list[DataField] = []
    for f in ds.fields:
        if f in removed:
            continue
        found = by_name.get(str(f.column)) if mine(f) else None
        if found is not None and found.datatype != f.datatype:
            numeric = found.datatype in ("integer", "real")
            role = (
                f.role
                if numeric == (f.datatype in ("integer", "real"))
                else ("measure" if numeric else "dimension")
            )
            f = f.model_copy(
                update={
                    "datatype": found.datatype,
                    "role": role,
                    "default_agg": _measure_agg(found.datatype, f.geo_role, role),
                }
            )
        fields.append(f)
    taken = _ids(ir)
    captions = {f.caption.casefold() for f in fields}
    added = [c for c in columns if c.name not in own]
    if owner is not None:
        new_fields = _fields(taken, added, owner, captions, name if joined else None, label=name)
    else:
        new_fields = _fields(taken, added, name, captions)
    return [*fields, *new_fields]


def set_interpreter(ir: IR, datasource: str, path: str, source: InspectedFile) -> IR:
    """切換 Excel 檔的資料解譯器（Tableau 連線下的「使用資料解譯器」）：source 是依新的設定重新檢查的檔案；
    檔案中各資料表的欄換成新的欄（名稱相同的欄位沿用）。聯集與樞紐中的資料表不能切換。"""
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    targets = [t for t in connection.tables if t.path == path and t.format in ("excel", "text")]
    if not targets:
        raise EditError(f"資料源中沒有 Excel 檔或文字檔 {path}")
    top = _relation_of(connection)
    tables = list(connection.tables)
    fields = list(ds.fields)
    for target in targets:
        found = (
            source.tables[0]
            if target.format == "text"
            else next((t for t in source.tables if t.sheet == target.sheet), None)
        )
        if found is None:
            raise EditError(f"{source.path.name} 中沒有工作表「{target.sheet}」")
        owner, current = _owner_of(top, target.name)
        if not _plain_leaf(current, target.name):
            raise EditError(f"「{target.name}」在聯集或樞紐中，不能切換資料解譯器")
        joined = isinstance(current, JoinRelation)
        fields = _resync(
            ir, ds.model_copy(update={"fields": fields}), target.name, found.columns, owner, joined
        )
        tables = [
            t.model_copy(
                update={
                    "columns": found.columns,
                    "interpreted": found.interpreted,
                    "cells": None,
                    "header": True,
                }
            )
            if t.name == target.name
            else t
            for t in tables
        ]
    connection = connection.model_copy(update={"tables": tables})
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "切換資料解譯器失敗")


def _plain_leaf(relation: Relation, name: str) -> bool:
    """name 是關聯樹中（只經過 join）的一張資料表。"""
    if isinstance(relation, TableRelation):
        return relation.name == name
    if isinstance(relation, JoinRelation):
        return _plain_leaf(relation.left, name) or _plain_leaf(relation.right, name)
    return False


@dataclass(frozen=True)
class WildcardMatch:
    """萬用字元聯集找到的一張資料表：檢查過的檔案、其中的資料表、報表中的路徑與作者電腦上的路徑。"""

    source: InspectedFile
    table: str | None
    package_path: str
    original: str


def add_wildcard_union(
    ir: IR,
    datasource: str,
    table: str,
    matches: list[WildcardMatch],
    wildcard: UnionWildcard,
    original: str | None = None,
) -> IR:
    """萬用字元聯集（Tableau 的「萬用字元聯集」）：table（關聯樹中的一張資料表，或已有的聯集）下面接上
    matches，另有 Path（作者電腦上的路徑；original 是 table 本身的檔案的路徑）與 Sheet、Table Name。"""
    if not matches:
        raise EditError(f"「{wildcard.files}」沒有符合的檔案")
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    owner, _ = _owner_of(_relation_of(connection), table)
    for match in matches:
        ir = add_table(
            ir, datasource, match.source, match.table, match.package_path, "union", owner
        )
    ds = _datasource(ir, datasource)
    connection = _file_connection(ds)
    top = _relation_of(connection)
    owner, current = _owner_of(top, table)
    if not isinstance(current, UnionRelation):
        raise EditError("萬用字元聯集失敗：資料表沒有成為聯集")
    by_table = {t.name: t for t in connection.tables}
    originals = {m.package_path: m.original for m in matches}
    parts = []
    for part in current.parts:
        found = by_table.get(part.relation.name)
        path = originals.get(found.path) if found is not None else None
        if path is None and found is not None:
            path = original or found.path
        parts.append(part.model_copy(update={"path": path}))
    union = current.model_copy(update={"parts": parts, "wildcard": wildcard})
    table_ref = owner or union.name
    fields = list(ds.fields)
    present = {f.column for f in fields if f.table == table_ref and f.column is not None}
    if "Path" not in present:
        taken = _ids(ir)
        captions = {f.caption.casefold() for f in fields}
        fields += _fields(taken, [FileColumn(name="Path", datatype="string")], table_ref, captions)
    connection = _with(connection, None, _replace_logical(top, owner, union))
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "萬用字元聯集失敗")


def _find_relation(relation: Relation, name: str) -> Relation | None:
    """關聯樹中名稱為 name 的資料表、自訂 SQL（或聯集、樞紐）。"""
    if getattr(relation, "name", None) == name:
        return relation
    if isinstance(relation, JoinRelation):
        return _find_relation(relation.left, name) or _find_relation(relation.right, name)
    if isinstance(relation, PivotRelation):
        return _find_relation(relation.source, name)
    return None


def _replace_leaf(relation: Relation, name: str, replacement: Relation) -> Relation:
    if getattr(relation, "name", None) == name:
        return replacement
    if isinstance(relation, JoinRelation):
        return relation.model_copy(
            update={
                "left": _replace_leaf(relation.left, name, replacement),
                "right": _replace_leaf(relation.right, name, replacement),
            }
        )
    if isinstance(relation, PivotRelation):
        return relation.model_copy(
            update={"source": _replace_leaf(relation.source, name, replacement)}
        )
    return relation


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
    on: list[Condition] | None = None,
) -> IR:
    """資料源加上檔案中的一張資料表：

    - relationship：成為另一張邏輯資料表，以關聯連到 target（邏輯資料表 id，省略時為第一張）
    - join：在 target 邏輯資料表（不是 Relationship 模型時為整個資料源）中與新的資料表 join
    - union：與 target 的資料表上下相接

    on：條件（已有的欄位 id、比較、新資料表的欄名）；relationship 與 join 至少要一個條件。"""
    ds = _datasource(ir, datasource)
    if isinstance(_editable(ds), LiveConnection):
        # 資料庫的資料源加上檔案：改成跨資料庫的 join
        ir = federate(ir, datasource)
        ds = _datasource(ir, datasource)
    connection = _editable(ds)
    assert isinstance(connection, FileConnection)
    found = source.table(table)
    name = _table_name(connection, found.name)
    added = file_table(source, table, package_path, name)
    new = NewTable(
        name,
        TableRelation(kind="table", name=name, table=[FILE_SCHEMA, name]),
        list(added.columns),
        added,
    )
    return _add_table(ir, ds, new, how, target, join, on)


@dataclass(frozen=True)
class NewTable:
    """要加入資料源的資料表：關聯樹中的名稱、關聯（檔案為 [Files, 名稱]，資料庫為限定名稱或自訂 SQL）、
    欄位，以及檔案的 FileTable（資料庫為 None）。"""

    name: str
    relation: TableRelation | SqlRelation
    columns: list[FileColumn]
    file: FileTable | None = None


def add_live_table(
    ir: IR,
    datasource: str,
    table: list[str] | None,
    sql: str | None,
    columns: list[FileColumn],
    how: Literal["relationship", "join", "union"] = "relationship",
    target: str | None = None,
    join: JoinType = "inner",
    on: list[Condition] | None = None,
    connection: LiveConnection | None = None,
) -> IR:
    """資料源加上資料庫的一張資料表（table：限定名稱的各段）或自訂 SQL；欄位由呼叫端從資料庫取得
    （tmig.data.catalog）。加入的方式與條件同 add_table；自訂 SQL 不能聯集。

    connection：資料表所在的連線。省略（或與資料源的 Live 連線相同）時是同一個資料庫；檔案的資料源或另一個
    資料庫時成為跨資料庫的 join（資料庫的資料源先以 federate 轉換）。"""
    ds = _datasource(ir, datasource)
    current = _editable(ds)
    same = isinstance(current, LiveConnection) and (
        connection is None or connection.connection_id == current.connection_id
    )
    if same:
        assert isinstance(current, LiveConnection)
        new = _live_new_table(current, table, sql, columns)
        if how == "union" and not isinstance(new.relation, TableRelation):
            raise EditError("自訂 SQL 不能聯集")
        return _add_table(ir, ds, new, how, target, join, on)
    if connection is None:
        raise EditError(f"資料源「{ds.caption}」是檔案的資料源，要指定資料庫的連線")
    if connection.dialect is None:
        raise EditError(f"不支援的連線類型 {connection.dbclass}")
    if isinstance(current, LiveConnection):
        ir = federate(ir, datasource)
        ds = _datasource(ir, datasource)
    files = _editable(ds)
    assert isinstance(files, FileConnection)
    new = _remote_new_table(files, connection, table, sql, columns)
    if how == "union" and new.file is not None and new.file.sql is not None:
        raise EditError("自訂 SQL 不能聯集")
    return _add_table(ir, ds, new, how, target, join, on)


def _remote_new_table(
    files: FileConnection,
    connection: LiveConnection,
    table: list[str] | None,
    sql: str | None,
    columns: list[FileColumn],
) -> NewTable:
    """跨資料庫的 join 中資料庫的資料表：關聯樹中為 Files.<名稱>，查詢時從資料庫取回。"""
    plain = _live_new_table(None, table, sql, columns)
    name = _table_name(files, plain.name)
    relation = plain.relation
    remote = FileTable(
        name=name,
        format="live",
        path=LIVE_PATH_PREFIX + connection.connection_id,
        connection=connection.model_copy(update={"relation": None}),
        table=list(relation.table) if isinstance(relation, TableRelation) else None,
        sql=relation.sql if isinstance(relation, SqlRelation) else None,
        columns=list(columns),
    )
    return NewTable(
        name,
        TableRelation(kind="table", name=name, table=[FILE_SCHEMA, name]),
        list(columns),
        remote,
    )


def leaf_relations(relation: Any) -> list[TableRelation | SqlRelation]:
    """關聯樹中的資料表與自訂 SQL（依出現的順序）。"""
    found: list[TableRelation | SqlRelation] = []

    def keep(leaf: TableRelation | SqlRelation) -> Relation:
        found.append(leaf)
        return leaf

    map_leaves(relation, keep)
    return found


def federate(ir: IR, datasource: str, columns: dict[str, list[FileColumn]] | None = None) -> IR:
    """資料庫（Live 連線）的資料源改成跨資料庫的 join，之後可以加入檔案或其他資料庫的資料表：原本的資料表與
    自訂 SQL 成為資料庫的資料表（查詢時取回）。columns：各資料表（關聯樹中的名稱）的欄，由呼叫端從資料庫
    取得；沒有提供的資料表取自資料源的欄位。已經是檔案的資料源時原樣回傳。"""
    ds = _datasource(ir, datasource)
    connection = _editable(ds)
    if isinstance(connection, FileConnection):
        return ir
    if connection.dialect is None:
        raise EditError(f"不支援的連線類型 {connection.dbclass}")

    def of(name: str) -> list[FileColumn]:
        if columns is not None and columns.get(name):
            return list(columns[name])
        return _leaf_columns(ds, connection, name)

    try:
        federated = to_federated(connection, of)
    except ValueError as exc:
        raise EditError(str(exc)) from exc
    ds = ds.model_copy(update={"connection": federated})
    return _validated(_replace_datasource(ir, ds), "改成跨資料庫的 join 失敗")


def _leaf_columns(ds: Datasource, connection: LiveConnection, name: str) -> list[FileColumn]:
    """資料表的欄（沒有從資料庫取得時）：聯集中的資料表依記下的欄，其他依屬於它的欄位。"""
    top = _relation_of(connection)
    owner, current = _owner_of(top, name)
    for relation in _walk(current):
        if isinstance(relation, UnionRelation):
            for part in relation.parts:
                if part.relation.name == name and part.columns:
                    return [FileColumn(name=c.name, datatype=c.datatype) for c in part.columns]
        if isinstance(relation, PivotRelation) and relation.source.name == name:
            raise EditError(f"資料表「{name}」已樞紐，請先登入資料庫再加入其他連線的資料表")
    joined = isinstance(current, JoinRelation)
    found: dict[str, FileColumn] = {}
    for f in ds.fields:
        if f.column is None or f.calc is not None:
            continue
        if f.physical_table is not None:
            mine = f.physical_table == name
        else:
            mine = f.table == (owner or name) if not joined or owner is None else False
        if mine:
            found.setdefault(f.column, FileColumn(name=f.column, datatype=f.datatype))
    return list(found.values())


def _walk(relation: Relation) -> list[Relation]:
    if isinstance(relation, JoinRelation):
        return [relation, *_walk(relation.left), *_walk(relation.right)]
    if isinstance(relation, PivotRelation | ProjectRelation):
        return [relation, *_walk(relation.source)]
    return [relation]


def _settle(ds: Datasource) -> Datasource:
    """跨資料庫的 join 只剩同一個資料庫的資料表（沒有檔案）時改回一般的 Live 連線（整段查詢下推到資料庫）。"""
    connection = ds.connection
    if not isinstance(connection, FileConnection):
        return ds
    single = single_database(connection)
    return ds if single is None else ds.model_copy(update={"connection": single})


def _live_new_table(
    connection: LiveConnection | None,
    table: list[str] | None,
    sql: str | None,
    columns: list[FileColumn],
) -> NewTable:
    if not columns:
        raise EditError("資料表沒有欄位")
    if (table is None) == (sql is None):
        raise EditError("要指定資料表或自訂 SQL 其中之一")
    if table is not None:
        if not table or any(not part for part in table):
            raise EditError("資料表的名稱不正確")
        base = table[-1]
        name = _table_name(connection, base) if connection is not None else base
        relation: TableRelation | SqlRelation = TableRelation(
            kind="table", name=name, table=list(table)
        )
    else:
        text = (sql or "").strip().rstrip(";").strip()
        if not text:
            raise EditError("自訂 SQL 是空的")
        name = _table_name(connection, CUSTOM_SQL) if connection is not None else CUSTOM_SQL
        relation = SqlRelation(kind="sql", name=name, sql=text)
    return NewTable(name, relation, list(columns))


def _add_table(
    ir: IR,
    ds: Datasource,
    new: NewTable,
    how: Literal["relationship", "join", "union"],
    target: str | None,
    join: JoinType,
    on: list[Condition] | None,
) -> IR:
    connection = _editable(ds)
    name = new.name
    if how != "union" and not on:
        raise EditError("要指定關聯或 join 的條件（兩邊的欄位）")
    tables = [*_tables(connection), *([new.file] if new.file is not None else [])]
    taken = _ids(ir)
    captions = {f.caption.casefold() for f in ds.fields}
    top = _relation_of(connection)
    model = top if isinstance(top, ModelRelation) else None
    if how == "relationship":
        if model is None:
            ds, first = _to_model(ds, taken)
            logicals, relationships = [first], []
        else:
            logicals, relationships = list(model.tables), list(model.relationships)
        logical_id = _fresh(taken, _slug(name) + "_" + secrets.token_hex(4))
        new_fields = _fields(taken, new.columns, logical_id, captions, label=name)
        parent = target or logicals[0].id
        if parent not in {t.id for t in logicals}:
            raise EditError(f"找不到邏輯資料表 {parent}")
        links, calcs = _relationship_conditions(
            ds, parent, new_fields, logical_id, name, on or [], taken
        )
        new_fields = [*new_fields, *calcs]
        logical = LogicalTable(id=logical_id, caption=name, relation=new.relation)
        built = ModelRelation(
            kind="model",
            tables=[*logicals, logical],
            relationships=[
                *relationships,
                Relationship(left=parent, right=logical_id, on=links),
            ],
        )
        connection = _with(connection, tables, built)
        ds = ds.model_copy(update={"connection": connection, "fields": [*ds.fields, *new_fields]})
        return _validated(_replace_datasource(ir, ds), "加入資料表失敗")
    # join、union：在一張邏輯資料表（或整個資料源）之內
    logical = _logical(model, target)
    current = logical.relation if logical is not None else top
    assert not isinstance(current, ModelRelation)
    owner = logical.id if logical is not None else None
    if how == "join":
        new_fields = _fields(taken, new.columns, owner or name, captions, name if owner else None)
        first_join = owner is not None and not isinstance(current, JoinRelation)

        def left_table(f: DataField) -> str | None:
            # Relationship 模型中還沒有 join 的邏輯資料表：欄位所屬的是它的資料表（或聯集、樞紐）
            return _single_name(current) if first_join else _physical_of(f)

        conditions = _join_conditions(
            [f for f in ds.fields if _in_relation(f, owner)],
            left_table,
            name,
            {c.name: c.datatype for c in new.columns},
            {f.caption: str(f.column) for f in new_fields},
            on or [],
            "要 join 的資料表",
        )
        relation: Relation = JoinRelation(
            kind="join", join=join, left=current, right=new.relation, on=conditions
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
        relation, ds = _union(ds, current, owner, new, taken)
    connection = _with(
        connection, tables, _replace_logical(_relation_of(connection), owner, relation)
    )
    ds = ds.model_copy(update={"connection": connection})
    return _validated(_replace_datasource(ir, ds), "加入資料表失敗")


def _union(
    ds: Datasource,
    current: Relation,
    owner: str | None,
    new: NewTable,
    taken: set[str],
) -> tuple[Relation, Datasource]:
    """在資料表下面接上另一張資料表：欄位依名稱對應，另加 Table Name（Excel 另有 Sheet、文字檔另有 Path）。"""
    connection = _editable(ds)
    by_name = {t.name: t for t in _tables(connection)}
    live = isinstance(connection, LiveConnection)
    if isinstance(current, UnionRelation):
        union = current
        parts = [*current.parts]
    elif isinstance(current, TableRelation) and (current.name in by_name or live):
        if live:
            # 資料庫的資料表：欄位取自資料源中這張資料表的欄位
            own = [
                f for f in ds.fields if f.column is not None and _belongs(f, owner, current.name)
            ]
            first_part = UnionPart(
                relation=current,
                columns=[UnionColumn(name=str(f.column), datatype=f.datatype) for f in own],
            )
        else:
            first_part = _part(current, by_name[current.name])
        union = UnionRelation(
            kind="union",
            name=_union_name(ds, current.name),
            parts=[first_part],
        )
        parts = list(union.parts)
    else:
        raise EditError("只有單一資料表（或已經是聯集）才能再接上其他資料表")
    if not isinstance(new.relation, TableRelation):
        raise EditError("自訂 SQL 不能聯集")
    # 跨資料庫的 join：聯集的資料表要在同一個連線（同一個資料庫，或都是檔案）
    sources = {
        _source_of(by_name[p.relation.name]) for p in parts if p.relation.name in by_name
    } | ({_source_of(new.file)} if new.file is not None else set())
    if len(sources) > 1:
        raise EditError("聯集的資料表要在同一個連線（同一個資料庫，或都是檔案）")
    new_part = (
        _part(new.relation, new.file)
        if new.file is not None
        else UnionPart(
            relation=new.relation,
            columns=[UnionColumn(name=c.name, datatype=c.datatype) for c in new.columns],
        )
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
    extra: list[FileColumn] = [c for c in new.columns if c.name not in present]
    sources = ["Table Name"]
    formats = {by_name[p.relation.name].format for p in parts if p.relation.name in by_name} | (
        {new.file.format} if new.file is not None else set()
    )
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


def _source_of(table: FileTable) -> str:
    """資料表所在的連線：資料庫的連線代號，檔案一律為 file。"""
    if table.format == "live" and table.connection is not None:
        return table.connection.connection_id
    return "file"


def _part(relation: TableRelation, table: FileTable) -> UnionPart:
    return UnionPart(
        relation=relation,
        columns=[UnionColumn(name=c.name, datatype=c.datatype) for c in table.columns],
        sheet=table.sheet if table.format == "excel" else None,
        path=table.path if table.format == "text" else None,
    )


def _union_name(ds: Datasource, first: str) -> str:
    base = f"{first}+"
    names = _relation_names(_relation_of(_editable(ds)))
    name, n = base, 1
    while name in names:
        n += 1
        name = f"{first}+{n}"
    return name


def remove_table(ir: IR, datasource: str, table: str) -> IR:
    """移除資料表（最後加入的 join、聯集的一張，或 Relationship 模型的一張邏輯資料表）；它的欄位不能
    有工作表或計算欄位用到。table：關聯樹中的資料表名稱或邏輯資料表 id。"""
    ds = _datasource(ir, datasource)
    connection = _editable(ds)
    top = _relation_of(connection)
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
    tables = [t for t in _tables(connection) if t.name != table]
    connection = _with(connection, tables, _replace_logical(top, owner, replacement))
    ds = _settle(ds.model_copy(update={"connection": connection, "fields": fields}))
    return _validated(_replace_datasource(ir, ds), "移除資料表失敗")


def _remove_logical(ir: IR, ds: Datasource, model: ModelRelation, logical: str) -> IR:
    edges = [r for r in model.relationships if logical in (r.left, r.right)]
    if len(edges) > 1:
        raise EditError("這張邏輯資料表還連著其他資料表，先移除它們")
    removed = [f for f in ds.fields if f.table == logical]
    # 關聯計算隨著關聯移除（只用到所屬資料表的欄位）
    plain = ds.model_copy(update={"fields": [f for f in ds.fields if not f.relationship_calc]})
    _check_unused(ir, plain, [f for f in removed if f.column is not None])
    _check_unused(ir, plain, [f for f in removed if f.column is None and not f.relationship_calc])
    gone = next(t for t in model.tables if t.id == logical)
    tables = [t for t in model.tables if t.id != logical]
    relationships = [r for r in model.relationships if logical not in (r.left, r.right)]
    fields = [f for f in ds.fields if f not in removed]
    connection = _editable(ds)
    leaves = _leaf_names(gone.relation)
    file_tables = [t for t in _tables(connection) if t.name not in leaves]
    if len(tables) == 1:
        # 只剩一張邏輯資料表：不再是 Relationship 模型
        (only,) = tables
        relation: Any = only.relation
        fields = [_from_model(f, only) for f in fields]
    else:
        relation = model.model_copy(update={"tables": tables, "relationships": relationships})
    connection = _with(connection, file_tables, relation)
    ds = _settle(ds.model_copy(update={"connection": connection, "fields": fields}))
    return _validated(_replace_datasource(ir, _without_stale_links(ds)), "移除資料表失敗")


def set_join(
    ir: IR,
    datasource: str,
    table: str,
    join: JoinType,
    on: list[Condition],
) -> IR:
    """改變資料表 join 的方式與條件（條件：已有的欄位 id 或 join 計算、比較、這張資料表的欄名或
    join 計算）。"""
    ds = _datasource(ir, datasource)
    connection = _editable(ds)
    owner, relation = _owner_of(_relation_of(connection), table)
    if not isinstance(relation, JoinRelation) or _single_name(relation.right) != table:
        raise EditError(f"「{table}」不是 join 的資料表")
    if not on:
        raise EditError("join 至少要一個條件")
    right = [f for f in ds.fields if _physical_of(f) == table and _in_relation(f, owner)]
    conditions = _join_conditions(
        [f for f in ds.fields if _in_relation(f, owner) and _physical_of(f) != table],
        _physical_of,
        table,
        {str(f.column): f.datatype for f in right},
        {f.caption: str(f.column) for f in right},
        on,
        "join 的另一邊",
    )
    changed = relation.model_copy(update={"join": join, "on": conditions})
    connection = _with(connection, None, _replace_logical(_relation_of(connection), owner, changed))
    ds = ds.model_copy(update={"connection": connection})
    return _validated(_replace_datasource(ir, ds), "修改 join 失敗")


def set_relationship(
    ir: IR,
    datasource: str,
    logical: str,
    on: list[Condition],
    options: dict[str, bool] | None = None,
) -> IR:
    """改變邏輯資料表的關聯條件（條件：另一張資料表的欄位 id 或關聯計算、比較、這張資料表的欄位 id 或
    關聯計算）；不再用到的關聯計算一併移除。options：效能選項（left_unique、right_unique、left_complete、
    right_complete），省略時不變。"""
    ds = _datasource(ir, datasource)
    connection = _editable(ds)
    model = _relation_of(connection)
    if not isinstance(model, ModelRelation):
        raise EditError("資料源不是 Relationship 模型")
    index = next((i for i, r in enumerate(model.relationships) if r.right == logical), None)
    if index is None:
        raise EditError(f"邏輯資料表 {logical} 沒有關聯到其他資料表")
    rel = model.relationships[index]
    if not on:
        raise EditError("關聯至少要一個條件")
    taken = _ids(ir)
    created: list[DataField] = []
    conditions: list[RelationshipCondition] = []
    for left_side, op, right_side in on:
        if op == "intersects":
            raise EditError("關聯的條件不能用 Intersects（空間 join 請用 join）")
        left = _link_side(ds, left_side, rel.left, created, taken)
        right = _link_side(ds, right_side, rel.right, created, taken)
        conditions.append(RelationshipCondition(left=left.id, op=op, right=right.id))
    relationships = list(model.relationships)
    known = ("left_unique", "right_unique", "left_complete", "right_complete")
    changed = {k: bool(v) for k, v in (options or {}).items() if k in known}
    relationships[index] = rel.model_copy(update={"on": conditions, **changed})
    connection = _with(connection, None, model.model_copy(update={"relationships": relationships}))
    ds = ds.model_copy(update={"connection": connection, "fields": [*ds.fields, *created]})
    return _validated(_replace_datasource(ir, _without_stale_links(ds)), "修改關聯失敗")


def pivot(ir: IR, datasource: str, table: str, columns: list[str]) -> IR:
    """把資料表的幾個欄樞紐成列（Tableau 資料來源分頁的「樞紐」）：Pivot Field Names 是欄名、
    Pivot Field Values 是值；被樞紐的欄位不能有工作表或計算欄位用到。"""
    ds = _datasource(ir, datasource)
    connection = _editable(ds)
    owner, relation = _owner_of(_relation_of(connection), table)
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
    names = _relation_names(_relation_of(connection))
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
    connection = _with(connection, None, _replace_logical(_relation_of(connection), owner, changed))
    ds = ds.model_copy(update={"connection": connection, "fields": fields})
    return _validated(_replace_datasource(ir, ds), "樞紐失敗")


def unpivot(ir: IR, datasource: str, name: str) -> IR:
    """移除樞紐：被樞紐的欄恢復成欄位；樞紐產生的兩個欄位不能有工作表或計算欄位用到。"""
    ds = _datasource(ir, datasource)
    connection = _editable(ds)
    owner, relation = _owner_of(_relation_of(connection), name)
    if not isinstance(relation, PivotRelation):
        raise EditError(f"「{name}」不是樞紐")
    ref = owner or name
    generated = [
        f for f in ds.fields if f.table == ref and f.column in (relation.names, relation.values)
    ]
    _check_unused(ir, ds, generated)
    source = relation.source
    by_name = {t.name: t for t in _tables(connection)}
    if source.name in by_name:
        columns = by_name[source.name].columns
    else:
        # 資料庫的資料表：被樞紐的欄以樞紐的型別恢復
        columns = [FileColumn(name=c.column, datatype=relation.datatype) for c in relation.columns]
    restored_columns = [c for c in columns if c.name in {p.column for p in relation.columns}]
    taken = _ids(ir)
    captions = {f.caption.casefold() for f in ds.fields if f not in generated}
    restored = _fields(taken, restored_columns, owner or source.name, captions)
    fields = [f for f in ds.fields if f not in generated] + restored
    connection = _with(connection, None, _replace_logical(_relation_of(connection), owner, source))
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
    if role is not None or datatype is not None:
        ir = prune_ad_hoc(ir, datasource)  # 沒有用到的臨時計算不擋住欄位的變更
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
    if (
        datatype is not None
        and datatype != target.datatype
        and not (isinstance(connection, FileConnection) and target.column is not None)
    ):
        # 萃取與資料庫的欄位：來源仍是原本的型別，查詢時轉換（與解析 .twb 的 datatype-customized 相同）
        if update:
            ir = update_field(ir, datasource, field_id, caption, hidden, role, default_agg)
        return _convert_type(ir, datasource, field_id, datatype)
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


CONVERSIONS = {
    "date": "DATE",
    "datetime": "DATETIME",
    "integer": "INT",
    "real": "FLOAT",
    "string": "STR",
}
"""變更資料類型的轉換函式（與 Tableau 的型別轉換函式相同）。"""
RAW_SUFFIX = " (原始型別)"


def _raw_of(ds: Datasource, target: DataField) -> DataField | None:
    """已經變更過資料類型的欄位（公式為轉換函式包住隱藏的原始欄位）→ 原始欄位。"""
    if target.calc is None:
        return None
    match = re.fullmatch(r"[A-Z]+\((\[.*\])\)", target.calc.formula.strip())
    if match is None or not match.group(1).endswith(RAW_SUFFIX + "]"):
        return None
    return next((f for f in ds.fields if f.name == match.group(1) and f.column is not None), None)


def _convert_type(ir: IR, datasource: str, field_id: str, datatype: str) -> IR:
    """萃取或資料庫欄位的資料類型：以隱藏的原始欄位加上轉換它的計算欄位表示（欄位的 id 不變）；
    改回原本的型別時還原成一般的欄位。"""
    ds = _datasource(ir, datasource)
    target = _field(ds, field_id)
    raw = _raw_of(ds, target)
    if target.column is None and raw is None:
        raise EditError(f"「{target.caption}」是計算欄位，資料類型由公式決定")
    if datatype not in CONVERSIONS:
        raise EditError(f"這個欄位不能轉換成 {datatype}（可以轉成字串、數字、日期）")
    _check_unused(ir, ds, [target])
    numeric = datatype in ("integer", "real")
    role = target.role if numeric else "dimension"
    geo = target.geo_role if numeric or target.geo_role not in ("latitude", "longitude") else None
    common = {
        "datatype": datatype,
        "role": role,
        "default_agg": _measure_agg(datatype, geo, role),
        "geo_role": geo,
        "format": None,
        "aliases": [],
    }
    if raw is not None and raw.datatype == datatype:
        # 改回原本的型別：還原成一般的欄位，移除隱藏的原始欄位
        restored = target.model_copy(
            update={
                **common,
                "calc": None,
                "column": raw.column,
                "table": raw.table,
                "physical_table": raw.physical_table,
            }
        )
        fields = [restored if f.id == field_id else f for f in ds.fields if f.id != raw.id]
        return _validated(
            _replace_datasource(ir, ds.model_copy(update={"fields": fields})), "修改欄位失敗"
        )
    fields = list(ds.fields)
    if raw is None:
        name = f"[{target.name[1:-1]}{RAW_SUFFIX}]"
        raw = target.model_copy(
            update={
                "id": _unique_id(ir, f"{target.id}_source"),
                "name": name,
                "caption": f"{target.caption}（原始型別）",
                "hidden": True,
                "aliases": [],
                "geo_role": None,
                "geo_aliases": [],
                "comment": None,
            }
        )
        fields.append(raw)
    formula = f"{CONVERSIONS[datatype]}({raw.name})"
    probe = ds.model_copy(update={"fields": [f for f in fields if f.id != field_id]})
    check = check_formula(_replace_datasource(ir, probe), datasource, formula)
    converted = target.model_copy(
        update={
            **common,
            "calc": check.calc,
            "column": None,
            "table": None,
            "physical_table": None,
        }
    )
    fields = [converted if f.id == field_id else f for f in fields]
    return _validated(
        _replace_datasource(ir, ds.model_copy(update={"fields": fields})), "修改欄位失敗"
    )


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
        raise EditError(f"資料源「{ds.caption}」不是檔案的資料源")
    return connection


def _editable(ds: Datasource) -> FileConnection | LiveConnection:
    """可以編輯資料表的資料源：檔案，或資料庫的 Live 連線（萃取與已發布的資料源不行）。"""
    connection = ds.connection
    if isinstance(connection, FileConnection):
        return connection
    if isinstance(connection, LiveConnection) and connection.relation is not None:
        return connection
    raise EditError(
        f"資料源「{ds.caption}」是萃取或已發布的資料源，資料表在原本的連線中，不能在這裡編輯"
    )


def _tables(connection: FileConnection | LiveConnection) -> list[FileTable]:
    return list(connection.tables) if isinstance(connection, FileConnection) else []


def _relation_of(connection: FileConnection | LiveConnection) -> Any:
    relation = connection.relation
    if relation is None:
        raise EditError("資料源沒有資料表")
    return relation


def _with(
    connection: FileConnection | LiveConnection, tables: list[FileTable] | None, relation: Any
) -> FileConnection | LiveConnection:
    """換掉連線的關聯樹（檔案另有資料表的清單）。"""
    update: dict[str, Any] = {"relation": relation}
    if isinstance(connection, FileConnection) and tables is not None:
        update["tables"] = tables
    return connection.model_copy(update=update)


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


def _table_name(connection: FileConnection | LiveConnection, base: str) -> str:
    """關聯樹中的資料表名稱不重複（同一個檔案加入兩次時加上序號）。"""
    names = {t.name for t in _tables(connection)} | (
        _relation_names(connection.relation) if connection.relation is not None else set()
    )
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
    relation = _relation_of(_editable(ds))
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
    logical_id: str,
    table: str,
    on: list[Condition],
    taken: set[str],
) -> tuple[list[RelationshipCondition], list[DataField]]:
    """新的邏輯資料表的關聯條件（已有資料表的欄位 id 或關聯計算、比較、新資料表的欄名或關聯計算）；
    另回傳新建的關聯計算欄位。"""
    by_column = {f.column: f for f in new_fields}
    created: list[DataField] = []
    conditions: list[RelationshipCondition] = []
    for left_side, op, right_side in on:
        if op == "intersects":
            raise EditError("關聯的條件不能用 Intersects（空間 join 請用 join）")
        existing = _link_side(ds, left_side, parent, created, taken)
        if isinstance(right_side, Calculated):
            pool = ds.model_copy(update={"fields": [*ds.fields, *new_fields]})
            other = _link_calc(pool, logical_id, right_side.formula, created, taken, table)
        else:
            if right_side not in by_column:
                raise EditError(f"「{table}」沒有欄位 {right_side}")
            other = by_column[right_side]
        conditions.append(RelationshipCondition(left=existing.id, op=op, right=other.id))
    return conditions, created


def _link_side(
    ds: Datasource, side: Side, table: str, created: list[DataField], taken: set[str]
) -> DataField:
    """關聯條件的一邊：table 的欄位（實體欄位或已有的關聯計算），或公式（關聯計算）。"""
    if not isinstance(side, Calculated):
        found = _field(ds, side)
        if found.table != table or (found.column is None and not found.relationship_calc):
            raise EditError(f"欄位「{found.caption}」不在要關聯的資料表中")
        return found
    return _link_calc(ds, table, side.formula, created, taken)


def _link_calc(
    ds: Datasource,
    table: str,
    formula: str,
    created: list[DataField],
    taken: set[str],
    label: str | None = None,
) -> DataField:
    """關聯計算：公式只能用到 table 的實體欄位，存成以欄位內部名稱參照的隱藏計算欄位；同一張資料表的同一個
    公式共用一個欄位。"""
    from tmig.formula.analyze import Analyzer
    from tmig.formula.lexer import FormulaSyntaxError
    from tmig.formula.parser import parse_formula
    from tmig.formula.types import FormulaSemanticError
    from tmig.ir.models import Calc
    from tmig.ir.scope import DatasourceScope

    text = formula.strip()
    if not text:
        raise EditError("關聯計算的公式是空的")
    pool = [*ds.fields, *created]
    own = [f for f in pool if f.table == table and f.column is not None]
    caption = label or _logical_caption(ds, table)

    def canonical(parts: tuple[str, ...]) -> str | None:
        if len(parts) != 1:
            raise EditError(f"關聯計算只能用到「{caption}」的欄位（不能用 {'.'.join(parts)}）")
        (name,) = parts
        found = _named(own, name)
        if found is None:
            raise EditError(f"關聯計算只能用到「{caption}」的欄位（沒有 [{name}]）")
        return found.name

    try:
        stored = _rewrite_refs(text, canonical)
        node = parse_formula(stored)
        info = Analyzer(DatasourceScope(ds.name, pool, [])).analyze(node)
    except (FormulaSyntaxError, FormulaSemanticError) as exc:
        raise EditError(f"關聯計算「{text}」有誤：{getattr(exc, 'message', exc)}") from exc
    if info.level not in ("row", "const") or info.has_lod:
        raise EditError("關聯計算不能彙總、用到 LOD 或 Table Calculation")
    if info.level == "const":
        raise EditError("關聯計算要用到資料表的欄位")
    if info.datatype not in ("string", "integer", "real", "boolean", "date", "datetime"):
        raise EditError(f"關聯計算的結果不能是 {info.datatype}")
    for f in pool:
        if f.relationship_calc and f.table == table and f.calc and f.calc.formula == stored:
            return f
    count = 1 + max(
        (
            int(m.group(1))
            for f in pool
            if (m := re.match(r"\[Relationship Calculation (\d+)\]$", f.name))
        ),
        default=0,
    )
    made = DataField(
        id=_fresh(taken, f"f_relationship_calc_{count}"),
        name=f"[Relationship Calculation {count}]",
        caption=text,
        role="dimension",
        datatype=info.datatype,  # type: ignore[arg-type]
        hidden=True,
        calc=Calc(formula=stored),
        table=table,
        relationship_calc=True,
        source_ref=EDITOR_REF,
    )
    created.append(made)
    return made


def _logical_caption(ds: Datasource, table: str) -> str:
    relation = getattr(ds.connection, "relation", None)
    if isinstance(relation, ModelRelation):
        return next((t.caption for t in relation.tables if t.id == table), table)
    return table


def _named(fields: list[DataField], name: str) -> DataField | None:
    """公式中的 [名稱]：顯示名稱、內部名稱或欄名（依序）。"""
    for match in (
        lambda f: f.caption == name,
        lambda f: f.name == _bracket(name),
        lambda f: f.column == name,
    ):
        found = [f for f in fields if match(f)]
        if len(found) == 1:
            return found[0]
    return None


def _bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _rewrite_refs(formula: str, replace: Any) -> str:
    """改寫公式中的欄位參照：replace（參照的各段 → 新的寫法；None 不變）。"""
    from tmig.formula.lexer import tokenize

    tokens = tokenize(formula)
    out: list[str] = []
    last = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind != "field":
            i += 1
            continue
        parts = [token.value]
        end = token
        if (
            i + 2 < len(tokens)
            and tokens[i + 1].kind == "punct"
            and tokens[i + 1].value == "."
            and tokens[i + 2].kind == "field"
        ):
            parts.append(tokens[i + 2].value)
            end = tokens[i + 2]
            i += 2
        new = replace(tuple(parts))
        if new is not None:
            out.append(formula[last : token.pos])
            out.append(new)
            last = end.pos + len(end.text)
        i += 1
    out.append(formula[last:])
    return "".join(out)


def link_calc_display(ds: Datasource, data_field: DataField) -> str:
    """關聯計算以顯示名稱參照欄位的公式（資料源分頁顯示、修改用）。"""
    if data_field.calc is None:
        return data_field.caption
    by_name = {f.name: f for f in ds.fields}

    def shown(parts: tuple[str, ...]) -> str | None:
        found = by_name.get(_bracket(parts[0])) if len(parts) == 1 else None
        return _bracket(found.caption) if found is not None else None

    try:
        return _rewrite_refs(data_field.calc.formula, shown)
    except ValueError:
        return data_field.calc.formula


def _without_stale_links(ds: Datasource) -> Datasource:
    """移除沒有關聯用到的關聯計算欄位。"""
    relation = getattr(ds.connection, "relation", None)
    used = (
        {fid for rel in relation.relationships for c in rel.on for fid in (c.left, c.right)}
        if isinstance(relation, ModelRelation)
        else set()
    )
    kept = [f for f in ds.fields if not f.relationship_calc or f.id in used]
    return ds if len(kept) == len(ds.fields) else ds.model_copy(update={"fields": kept})


def _join_conditions(
    left_fields: list[DataField],
    left_table: Any,
    right_table: str,
    right_columns: dict[str, str],
    right_captions: dict[str, str],
    on: list[Condition],
    other_side: str,
) -> list[JoinCondition]:
    """join 的條件：左邊是已有的欄位（id）或 join 計算，右邊是新資料表的欄名或 join 計算。join 計算的
    欄位以顯示名稱或欄名書寫，存成 [資料表].[欄位]。"""
    from tmig.sqlgen.source import SourceError, join_formula

    types: dict[tuple[str, str], str] = {(right_table, c): t for c, t in right_columns.items()}
    for f in left_fields:
        table = left_table(f)
        if table is not None and f.column is not None:
            types[(str(table), f.column)] = f.datatype

    def left_ref(parts: tuple[str, ...]) -> str | None:
        if len(parts) == 2:
            if (parts[0], parts[1]) in types and parts[0] != right_table:
                return None
            raise EditError(f"join 計算不能用到 {'.'.join(parts)}")
        found = _named(left_fields, parts[0])
        if found is None or found.column is None:
            raise EditError(f"join 計算中的 [{parts[0]}] 不在{other_side}")
        return f"{_bracket(str(left_table(found)))}.{_bracket(found.column)}"

    def right_ref(parts: tuple[str, ...]) -> str | None:
        if len(parts) == 2:
            if parts[0] == right_table and parts[1] in right_columns:
                return None
            raise EditError(f"join 計算不能用到 {'.'.join(parts)}")
        column = right_captions.get(parts[0], parts[0])
        if column not in right_columns:
            raise EditError(f"「{right_table}」沒有欄位 {parts[0]}")
        return f"{_bracket(right_table)}.{_bracket(column)}"

    def spatial(term: JoinTerm) -> bool:
        if term.formula is not None:
            from tmig.sqlgen.source import join_formula_type

            try:
                return join_formula_type(term.formula, types) == "spatial"
            except SourceError:
                return False
        return types.get((str(term.table), str(term.column))) == "spatial"

    def condition(left: Side, op: Comparison, right: Side) -> JoinCondition:
        found = JoinCondition(left=term(left, True), op=op, right=term(right, False))
        if op == "intersects" and not (spatial(found.left) and spatial(found.right)):
            raise EditError("Intersects（空間 join）的兩邊都要是空間欄位或傳回形狀的 join 計算")
        return found

    def term(side: Side, left: bool) -> JoinTerm:
        if isinstance(side, Calculated):
            text = side.formula.strip()
            if not text:
                raise EditError("join 計算的公式是空的")
            try:
                stored = _rewrite_refs(text, left_ref if left else right_ref)
                join_formula(stored, types)
            except EditError:
                raise
            except SourceError as exc:
                raise EditError(str(exc)) from exc
            except ValueError as exc:
                raise EditError(f"join 計算「{text}」有誤：{getattr(exc, 'message', exc)}") from exc
            return JoinTerm(formula=stored)
        if left:
            existing = next((f for f in left_fields if f.id == side), None)
            if existing is None or existing.column is None:
                raise EditError(f"欄位 {side} 不在{other_side}")
            return JoinTerm(table=left_table(existing), column=existing.column)
        if side not in right_columns:
            raise EditError(f"「{right_table}」沒有欄位 {side}")
        return JoinTerm(table=right_table, column=side)

    return [condition(left, op, right) for left, op, right in on]


def join_calc_display(ds: Datasource, owner: str | None, formula: str) -> str:
    """join 計算以顯示名稱參照欄位的公式（[資料表].[欄位] → [顯示名稱]，資料源分頁顯示、修改用）。"""
    columns = {
        (str(_physical_of(f)), str(f.column)): f for f in ds.fields if _in_relation(f, owner)
    }

    def shown(parts: tuple[str, ...]) -> str | None:
        found = columns.get((parts[0], parts[1])) if len(parts) == 2 else None
        return _bracket(found.caption) if found is not None else None

    try:
        return _rewrite_refs(formula, shown)
    except ValueError:
        return formula


def _check_unused(ir: IR, ds: Datasource, fields: list[DataField]) -> None:
    """欄位沒有工作表、計算欄位、集合、階層或篩選用到（見 tmig.edit.check_unused）。"""
    check_unused(ir, ds, fields)


__all__ = [
    "DATA_DIR",
    "Calculated",
    "Condition",
    "InspectedFile",
    "InspectedTable",
    "WildcardMatch",
    "add_datasource",
    "add_table",
    "add_wildcard_union",
    "file_table",
    "inspect_file",
    "interpret_header",
    "join_calc_display",
    "link_calc_display",
    "new_workbook",
    "pivot",
    "remove_table",
    "rename_datasource",
    "set_interpreter",
    "set_join",
    "set_relationship",
    "unpivot",
    "update_field",
]
