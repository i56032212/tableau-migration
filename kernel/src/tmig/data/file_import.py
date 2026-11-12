"""檔案型資料源的匯入：.twbx 內的 Excel、文字檔（沒有萃取）依 Tableau 讀到的欄名與型別讀進 DuckDB。

每張資料表匯入成 Files.<資料表名稱>（見 tmig.ir.models.FileConnection），之後與萃取相同在本機查詢。

- 欄位依順序對應：欄名取 Tableau 記下的名稱（沒有標題列時為 F1、F2…），型別依 Tableau 判定的型別
- Excel：.xlsx／.xlsm 以 openpyxl、.xls 以 xlrd 讀取儲存格的值（公式取 Excel 存下的計算結果）；
  範圍依 Tableau 記下的範圍（gridOrigin），沒有時為整張工作表；範圍尾端全部是空白的列不算
- 文字檔：依記下的分隔字元、字元集與地區設定解讀；數字的小數點與千分位、日期的寫法依地區設定
- 空白的值為 Null；不符合型別的值（例如日期欄中的文字）成為 Null，並在匯入結果中列出筆數與例子

匯入自檢：DuckDB 中的列數、各欄非空值數與數值、日期的總和、最小值、最大值，與讀進來的值逐一比對。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import os
import re
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from tmig.data.hyper_import import (
    ColumnInfo,
    HyperImportError,
    StatCheck,
    TableImport,
    _quote,
    _same,
    _stat_sql,
    _text,
    export_hyper_tables,
    load_hyper_export,
)
from tmig.ir.models import (
    FILE_SCHEMA,
    DataField,
    FileConnection,
    FileTable,
    JoinRelation,
    ModelRelation,
    PivotRelation,
    ProjectRelation,
    Relation,
    UnionRelation,
)

ARROW_TYPES = {
    "string": pa.string(),
    "integer": pa.int64(),
    "real": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "datetime": pa.timestamp("us"),
    "spatial": pa.string(),
}
DUCKDB_TYPES = {
    "string": "VARCHAR",
    "integer": "BIGINT",
    "real": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "spatial": "VARCHAR",
}
STAT_TAGS = {
    "string": "TEXT",
    "integer": "BIG_INT",
    "real": "DOUBLE",
    "boolean": "BOOL",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "spatial": "TABGEOGRAPHY",
}
"""依 Tableau 型別對應的統計項目（與 Hyper 匯入相同的分類；空間資料只比對非空值數）。"""

COMMA_DECIMAL = ("de", "es", "it", "pt", "nl", "id", "tr", "da", "el", "ro", "hr", "sl", "sr")
SPACE_THOUSANDS = (
    "fr",
    "ru",
    "sv",
    "pl",
    "cs",
    "fi",
    "nb",
    "no",
    "sk",
    "uk",
    "hu",
    "bg",
    "lt",
    "lv",
    "et",
)
"""小數點是逗號的語言（千分位為句點，或空白）；其餘地區小數點為句點、千分位為逗號。"""
EXCEL_EPOCH = dt.datetime(1899, 12, 30)
"""Excel 的日期序號從 1899-12-30 起算（1900 日期系統，含 Lotus 1-2-3 的閏年誤差修正）。"""
TRUE_TEXT = frozenset({"true", "t", "yes", "y", "1"})
FALSE_TEXT = frozenset({"false", "f", "no", "n", "0"})
EXAMPLES = 3


class FileImportError(HyperImportError):
    """檔案讀不了，或內容與 Tableau 記下的欄位不符。"""


# ---------------------------------------------------------------- 匯入


def import_file_tables(
    archive: zipfile.ZipFile, connection: FileConnection, out_path: Path
) -> list[TableImport]:
    """把檔案型資料源的每張資料表匯入 out_path（DuckDB 檔，Files.<name>），並做匯入自檢。"""
    tmp_out = out_path.with_name(out_path.name + ".tmp")
    tmp_out.unlink(missing_ok=True)
    books: dict[str, Any] = {}
    results: list[TableImport] = []
    with tempfile.TemporaryDirectory() as work, duckdb.connect(str(tmp_out)) as con:
        con.execute(f"CREATE SCHEMA {_quote(FILE_SCHEMA)}")
        hyper = _hyper_exports(archive, connection, Path(work))
        for table in connection.tables:
            if table.format == "hyper":
                results.append(load_hyper_export(con, hyper[table.name], FILE_SCHEMA, table.name))
                continue
            if table.format == "spatial":
                raw = spatial_rows(archive, table)
                values, warnings = typed_columns(raw, table)
                results.append(_load(con, table, values, warnings))
                continue
            data = read_member(archive, table.path)
            if table.format == "excel":
                raw = excel_rows(data, table, books)
            else:
                raw = text_rows(data, table)
            values, warnings = typed_columns(raw, table)
            results.append(_load(con, table, values, warnings))
    os.replace(tmp_out, out_path)
    return results


def _hyper_exports(
    archive: zipfile.ZipFile, connection: FileConnection, work: Path
) -> dict[str, Any]:
    """.hyper 檔中的資料表匯出成 Parquet（每個檔案啟動一次 Hyper），以關聯樹中的資料表名稱為鍵。"""
    by_path: dict[str, list[FileTable]] = {}
    for table in connection.tables:
        if table.format == "hyper":
            by_path.setdefault(table.path, []).append(table)
    found: dict[str, Any] = {}
    for i, (path, tables) in enumerate(by_path.items()):
        hyper_path = work / f"source{i}.hyper"
        hyper_path.write_bytes(read_member(archive, path))
        log_dir = work / f"log{i}"
        log_dir.mkdir()
        refs: list[tuple[str, str]] = []
        for table in tables:
            parts = table.table or ["Extract", "Extract"]
            refs.append((parts[0], parts[-1]) if len(parts) > 1 else ("public", parts[0]))
        exported, _ = export_hyper_tables(hyper_path, list(dict.fromkeys(refs)), log_dir)
        by_ref = {(e.schema, e.table): e for e in exported}
        for table, ref in zip(tables, refs, strict=True):
            found[table.name] = by_ref[ref]
    return found


def read_member(archive: zipfile.ZipFile, path: str) -> bytes:
    names = {n.replace("\\", "/"): n for n in archive.namelist()}
    wanted = path.replace("\\", "/").lstrip("./")
    if wanted not in names:
        raise FileImportError(f"{Path(archive.filename or '').name} 內找不到 {path}")
    return archive.read(names[wanted])


def spatial_rows(archive: zipfile.ZipFile, table: FileTable) -> list[list[Any]]:
    """空間檔案（Shapefile、GeoJSON、KML、TopoJSON）的各列，依 Tableau 記下的欄名（不分大小寫）取值；
    Geometry 是 WKT 文字（tmig.data.spatial_files）。"""
    from tmig.data.spatial_files import SpatialFileError, read_spatial, sidecar_paths

    names = [n.replace("\\", "/") for n in archive.namelist()]
    files = {p: read_member(archive, p) for p in sidecar_paths(names, table.path)}
    files.setdefault(table.path, read_member(archive, table.path))
    try:
        found = read_spatial(files, table.path, table.layer)
    except SpatialFileError as exc:
        raise FileImportError(str(exc)) from exc
    lowered = {name.lower(): name for name in found.columns}
    missing = [c.name for c in table.columns if c.name.lower() not in lowered]
    if missing:
        raise FileImportError(
            f"{table.path} 中沒有 Tableau 記下的欄位：{'、'.join(missing)}"
            f"（檔案中有：{'、'.join(found.columns)}）"
        )
    keys = [lowered[c.name.lower()] for c in table.columns]
    return [[row.get(key) for key in keys] for row in found.rows]


def file_missing_columns(
    connection: FileConnection, fields: Sequence[DataField], tables: Sequence[TableImport]
) -> list[str]:
    """IR 的實體欄位不在所屬資料表的欄位中（樞紐、聯集與合併的欄位依它們產生的欄判斷）。"""
    by_name = {t.table: {c.name for c in t.columns} for t in tables}
    top = connection.relation
    available: dict[str, set[str]] = {}
    owner: dict[str, Relation] = {}
    if isinstance(top, ModelRelation):
        for logical in top.tables:
            owner[logical.id] = logical.relation
            available.update(_available(logical.relation, by_name))
    elif top is not None:
        available = _available(top, by_name)
    default = connection.tables[0].name if len(connection.tables) == 1 else None
    missing: list[str] = []
    for data_field in fields:
        if data_field.column is None:
            continue
        relation = owner.get(data_field.table or "")
        if relation is not None and not isinstance(relation, JoinRelation):
            # 邏輯資料表：資料表或樞紐、聯集（欄位以欄名參照它產生的欄）
            names = set().union(*_available(relation, by_name).values())
            table = data_field.table
        else:
            table = data_field.physical_table or data_field.table or default
            names = available.get(table or "", set())
        if data_field.column not in names:
            missing.append(f"{data_field.id}（{table}.{data_field.column}）")
    return sorted(missing)


def _available(relation: Relation, by_name: dict[str, set[str]]) -> dict[str, set[str]]:
    """關聯樹中各名稱（資料表、樞紐、聯集、project）可以參照的欄。"""
    if isinstance(relation, JoinRelation):
        return {**_available(relation.left, by_name), **_available(relation.right, by_name)}
    if isinstance(relation, PivotRelation):
        pivoted = {c.column for c in relation.columns}
        source = by_name.get(relation.source.name, set())
        return {
            relation.source.name: source - pivoted,
            relation.name: {relation.names, relation.values},
        }
    if isinstance(relation, UnionRelation):
        columns = {"Table Name", "Sheet", "Path"}
        for part in relation.parts:
            columns |= by_name.get(part.relation.name, set())
        return {relation.name: columns}
    if isinstance(relation, ProjectRelation):
        inner = _available(relation.source, by_name)
        return {**inner, relation.name: {c.name for c in relation.columns}}
    return {relation.name: by_name.get(relation.name, set())}


def _load(
    con: duckdb.DuckDBPyConnection,
    table: FileTable,
    values: list[list[Any]],
    warnings: list[str],
) -> TableImport:
    arrays = {
        column.name: pa.array(items, type=ARROW_TYPES[column.datatype])
        for column, items in zip(table.columns, values, strict=True)
    }
    arrow = pa.table(arrays)
    target = f"{_quote(FILE_SCHEMA)}.{_quote(table.name)}"
    columns = [
        ColumnInfo(
            name=c.name,
            hyper_type=f"{table.format}:{c.datatype}",
            duckdb_type=DUCKDB_TYPES[c.datatype],
            collation=None,
        )
        for c in table.columns
    ]
    tags = {c.name: STAT_TAGS[c.datatype] for c in table.columns}
    con.register("__tmig_file", arrow)
    try:
        select = ", ".join(
            f"CAST({_quote(c.name)} AS {DUCKDB_TYPES[c.datatype]}) AS {_quote(c.name)}"
            for c in table.columns
        )
        con.execute(f"CREATE TABLE {target} AS SELECT {select} FROM __tmig_file")
        source_sql, labels = _stat_sql(columns, tags, "__tmig_file")
        source = con.execute(source_sql).fetchall()[0]
    finally:
        con.unregister("__tmig_file")
    sql, _ = _stat_sql(columns, tags, target)
    loaded = con.execute(sql).fetchall()[0]
    assert source is not None and loaded is not None
    checks = [
        StatCheck(
            column=column,
            stat=stat,
            hyper=_text(a),
            duckdb=_text(b),
            ok=_same(stat, tags.get(column or "", ""), a, b),
        )
        for (column, stat), a, b in zip(labels, source, loaded, strict=True)
    ]
    return TableImport(
        schema=FILE_SCHEMA,
        table=table.name,
        rows=arrow.num_rows,
        columns=columns,
        checks=checks,
        warnings=warnings,
    )


# ---------------------------------------------------------------- Excel


def excel_rows(data: bytes, table: FileTable, books: dict[str, Any]) -> list[list[Any]]:
    """工作表中範圍內的各列（不含標題列），每列依欄的順序取 len(table.columns) 欄。"""
    if table.path.lower().endswith(".xls"):
        grid = _xls_grid(data, table, books)
    else:
        grid = _xlsx_grid(data, table, books)
    rows = grid[1:] if table.header else grid
    width = len(table.columns)
    rows = [(list(r) + [None] * width)[:width] for r in rows]
    while rows and all(v is None or v == "" for v in rows[-1]):
        rows.pop()  # 範圍尾端全部是空白的列
    return rows


def _bounds(cells: str | None) -> tuple[int, int, int | None, int | None]:
    """A1:U10195 → (起始欄, 起始列, 結束欄, 結束列)，從 1 起算；None 表示到最後。"""
    if not cells:
        return 1, 1, None, None
    match = re.fullmatch(r"\$?([A-Za-z]+)\$?(\d+)(?::\$?([A-Za-z]+)\$?(\d+))?", cells.strip())
    if match is None:
        raise FileImportError(f"無法辨識的 Excel 範圍 {cells}")

    def column(letters: str) -> int:
        number = 0
        for letter in letters.upper():
            number = number * 26 + ord(letter) - ord("A") + 1
        return number

    first_col, first_row = column(match.group(1)), int(match.group(2))
    if match.group(3) is None:
        return first_col, first_row, None, None
    return first_col, first_row, column(match.group(3)), int(match.group(4))


def _xlsx_grid(data: bytes, table: FileTable, books: dict[str, Any]) -> list[list[Any]]:
    import openpyxl

    book = books.get(table.path)
    if book is None:
        try:
            book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise FileImportError(f"{table.path} 不是可讀取的 Excel 檔：{exc}") from exc
        books[table.path] = book
    sheet = _sheet(book.sheetnames, table)
    worksheet = book[sheet]
    first_col, first_row, last_col, last_row = _bounds(table.cells)
    if last_col is None:
        last_col = first_col + len(table.columns) - 1
    grid: list[list[Any]] = []
    for row in worksheet.iter_rows(
        min_row=first_row,
        max_row=last_row,
        min_col=first_col,
        max_col=last_col,
        values_only=True,
    ):
        grid.append(list(row))
    return grid


def _xls_grid(data: bytes, table: FileTable, books: dict[str, Any]) -> list[list[Any]]:
    import xlrd

    book = books.get(table.path)
    if book is None:
        try:
            book = xlrd.open_workbook(file_contents=data)
        except Exception as exc:
            raise FileImportError(f"{table.path} 不是可讀取的 Excel 檔：{exc}") from exc
        books[table.path] = book
    sheet = book.sheet_by_name(_sheet(book.sheet_names(), table))
    first_col, first_row, last_col, last_row = _bounds(table.cells)
    last_row = min(last_row or sheet.nrows, sheet.nrows)
    last_col = min(last_col or (first_col + len(table.columns) - 1), sheet.ncols)
    grid: list[list[Any]] = []
    for r in range(first_row - 1, last_row):
        row: list[Any] = []
        for c in range(first_col - 1, last_col):
            cell = sheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                row.append(xlrd.xldate.xldate_as_datetime(float(cell.value), book.datemode))
            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                row.append(bool(cell.value))
            elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
                row.append(None)
            else:
                row.append(cell.value)
        grid.append(row)
    return grid


def _sheet(names: Iterable[str], table: FileTable) -> str:
    names = list(names)
    wanted = table.sheet or (names[0] if names else "")
    if wanted in names:
        return wanted
    folded = {n.casefold(): n for n in names}
    if wanted.casefold() in folded:
        return folded[wanted.casefold()]
    raise FileImportError(
        f"{table.path} 中找不到工作表「{wanted}」（有：{'、'.join(names)}）；具名範圍尚不支援"
    )


# ---------------------------------------------------------------- 文字檔


def text_rows(data: bytes, table: FileTable) -> list[list[Any]]:
    codec = (
        "utf-8-sig"
        if table.charset.replace("_", "-").lower() in ("utf-8", "utf8")
        else table.charset
    )
    try:
        text = data.decode(codec)
    except LookupError as exc:
        raise FileImportError(f"{table.path} 的字元集 {table.charset} 無法辨識") from exc
    except UnicodeDecodeError as exc:
        raise FileImportError(f"{table.path} 不是 {table.charset} 編碼：{exc}") from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=table.separator)
    rows: list[list[Any]] = [list(r) for r in reader]
    if table.header and rows:
        rows = rows[1:]
    width = len(table.columns)
    rows = [(r + [None] * width)[:width] for r in rows]
    while rows and all(v is None or v == "" for v in rows[-1]):
        rows.pop()
    return rows


# ---------------------------------------------------------------- 型別


def typed_columns(rows: list[list[Any]], table: FileTable) -> tuple[list[list[Any]], list[str]]:
    """依 Tableau 的型別轉換各欄的值；回傳各欄的值與警告（不符合型別而成為 Null 的值）。"""
    columns: list[list[Any]] = [[] for _ in table.columns]
    failed: list[list[str]] = [[] for _ in table.columns]
    counts = [0] * len(table.columns)
    for row in rows:
        for i, (column, value) in enumerate(zip(table.columns, row, strict=True)):
            converted, ok = convert(value, column.datatype, table.locale)
            columns[i].append(converted)
            if not ok:
                counts[i] += 1
                if len(failed[i]) < EXAMPLES:
                    failed[i].append(str(value))
    warnings = [
        f"欄位「{column.name}」有 {count} 個值不是 {column.datatype}，成為 Null（例如 {'、'.join(failed[i])}）"
        for i, (column, count) in enumerate(zip(table.columns, counts, strict=True))
        if count
    ]
    return columns, warnings


def convert(value: Any, datatype: str, locale: str | None) -> tuple[Any, bool]:
    """一個儲存格的值 → Tableau 型別的值；回傳（值, 是否成功）。空白為 Null（成功）。"""
    if value is None:
        return None, True
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None, True
        if datatype in ("string", "spatial"):
            return value, True
        parsed = _parse_text(text, datatype, locale)
        return parsed, parsed is not None
    if datatype == "string":
        return _as_text(value), True
    if isinstance(value, bool):
        if datatype == "boolean":
            return value, True
        if datatype in ("integer", "real"):
            return (int(value) if datatype == "integer" else float(value)), True
        return None, False
    if isinstance(value, (int, float)):
        if datatype == "integer":
            return (int(value) if float(value).is_integer() else round(value)), True
        if datatype == "real":
            return float(value), True
        if datatype == "boolean":
            return value != 0, True
        moment = _serial(float(value))
        return (moment.date() if datatype == "date" else moment), True
    if isinstance(value, dt.datetime):
        if datatype == "date":
            return value.date(), True
        if datatype == "datetime":
            return value.replace(tzinfo=None), True
        return None, False
    if isinstance(value, dt.date):
        if datatype == "date":
            return value, True
        if datatype == "datetime":
            return dt.datetime.combine(value, dt.time()), True
        return None, False
    if isinstance(value, dt.time) and datatype == "datetime":
        return dt.datetime.combine(EXCEL_EPOCH.date(), value), True
    return None, False


def _as_text(value: Any) -> str:
    """數字等儲存格放在文字欄：整數不帶小數點（10024 而不是 10024.0）。"""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    return str(value)


def _serial(number: float) -> dt.datetime:
    """Excel 的日期序號 → 日期時間（精確到微秒）。"""
    return EXCEL_EPOCH + dt.timedelta(microseconds=round(number * 86400 * 1_000_000))


def _separators(locale: str | None) -> tuple[str, tuple[str, ...]]:
    """地區設定 → (小數點, 千分位)。"""
    language = (locale or "en_US").replace("-", "_").split("_")[0].lower()
    if language == "de" and (locale or "").upper().endswith("CH"):
        return ".", ("'", "’")
    if language in COMMA_DECIMAL:
        return ",", (".", " ", " ")
    if language in SPACE_THOUSANDS:
        return ",", (" ", " ", " ")
    return ".", (",",)


def _number(text: str, locale: str | None) -> float | None:
    decimal, thousands = _separators(locale)
    cleaned = text
    for mark in thousands:
        cleaned = cleaned.replace(mark, "")
    if decimal != ".":
        cleaned = cleaned.replace(decimal, ".")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None  # NaN、無限大不是 Tableau 的數字
    return -number if negative else number


ISO_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)
US_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%y",
    "%m/%d/%y %H:%M",
    "%m/%d/%y %I:%M %p",
    "%m-%d-%Y",
)
DAY_FIRST_FORMATS = (
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d.%m.%Y",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d.%m.%y",
)
TEXT_MONTH_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y")
"""英文月份名稱的寫法（Tableau 依地區設定也認得）。"""


def _date_formats(locale: str | None) -> tuple[str, ...]:
    language = (locale or "en_US").replace("-", "_")
    if language.startswith(("en_US", "en_PH")) or language in ("en", "es_US"):
        return ISO_FORMATS + US_FORMATS + TEXT_MONTH_FORMATS
    if language.startswith(("zh", "ja", "ko", "hu", "lt", "sv")):
        return ISO_FORMATS + TEXT_MONTH_FORMATS + US_FORMATS
    return ISO_FORMATS + DAY_FIRST_FORMATS + TEXT_MONTH_FORMATS


def _parse_text(text: str, datatype: str, locale: str | None) -> Any:
    if datatype == "integer":
        number = _number(text, locale)
        if number is None or not number.is_integer():
            return None
        return int(number)
    if datatype == "real":
        return _number(text, locale)
    if datatype == "boolean":
        folded = text.casefold()
        if folded in TRUE_TEXT:
            return True
        if folded in FALSE_TEXT:
            return False
        return None
    for fmt in _date_formats(locale):
        try:
            moment = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.date() if datatype == "date" else moment
    return None


__all__ = [
    "FileImportError",
    "convert",
    "excel_rows",
    "file_missing_columns",
    "import_file_tables",
    "read_member",
    "text_rows",
    "typed_columns",
]
