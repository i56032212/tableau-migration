"""萬用字元聯集（Tableau 的 Wildcard union，``<relation type='batch-union'>``）：依檔名（與 Excel 工作表名稱）
的模式找出 .twbx 內的檔案，展開成一般的聯集（每個檔案或工作表一張資料表），各檔案的欄位依它自己的標題列。

XML（2020.4 的公開報表）::

    <relation all='true' connection='excel-direct.x' include-siblings='true' is-recursive='false'
              name='2016+' path='' type='batch-union'>
      <database-filter><expression op='='><expression op='Superstore - *.xlsx' /></expression>
      </database-filter>
      <table-filter />
      <columns header='yes'><column datatype='string' name='Order ID' />…
        <column datatype='string' name='Path' /><column datatype='string' name='Sheet' /></columns>
    </relation>

- database-filter：檔名的模式（``*``、``?`` 為萬用字元，不分大小寫）；table-filter：Excel 工作表名稱的模式
  （空白為全部的工作表）。文字檔每個檔案一張資料表
- 搜尋的範圍：連線的檔案在 .twbx 中所在的資料夾；is-recursive 時含子資料夾（include-siblings 的意義未在公開
  資料中說明，推定為同一層的檔案，一律包含）
- 聯集的欄位：``<columns>`` 中的欄位（名稱與型別），另有 Path（檔案在作者電腦上的路徑）、Sheet（工作表）或
  Table Name；各檔案沒有的欄為 Null。.twbx 中只有封裝時符合模式的檔案（與 Tableau Server 上的報表相同是快照）

展開的結果是 XML：``<relation type='union'>`` 之下每個檔案一個 ``<relation type='table'>``（各自的具名連線
與 ``<columns>``），之後與一般的聯集相同解析（tmig.parser.connections 的 _union）。
"""

from __future__ import annotations

import copy
import csv
import fnmatch
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from lxml import etree

GENERATED = ("Path", "Sheet", "Table Name")
"""聯集另加的來源欄位。"""
WILDCARD_ATTRS = ("tmig-wildcard-files", "tmig-wildcard-sheets", "tmig-wildcard-recursive")
"""展開後的聯集上記下的搜尋條件（解析成 UnionRelation.wildcard）。"""
PATH_ATTR = "tmig-path"
"""展開後各資料表上記下的檔案路徑（作者電腦上的路徑，Path 欄的值）。"""


@dataclass(frozen=True)
class Matched:
    """符合模式的一張資料表：檔案（.twbx 中的路徑）、Excel 的工作表與欄名。"""

    path: str
    sheet: str | None
    columns: list[str]


class WildcardError(ValueError):
    """萬用字元聯集無法展開。"""


def pattern_of(element: etree._Element | None) -> str | None:
    """<database-filter>、<table-filter> 中的模式：``<expression op='='><expression op='模式' />``。"""
    if element is None:
        return None
    for inner in element.iter("expression"):
        if len(inner) == 0:
            text = (inner.get("op") or "").strip()
            if text:
                return text
    return None


def matches(pattern: str | None, name: str) -> bool:
    """萬用字元（*、?）比對，不分大小寫；沒有模式時全部符合。"""
    if not pattern:
        return True
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


def candidates(names: list[str], anchor: str, recursive: bool) -> list[str]:
    """與 anchor（連線的檔案）同一個資料夾（recursive 時含子資料夾）的檔案，依名稱排序。"""
    folder = PurePosixPath(anchor).parent
    found: list[str] = []
    for name in names:
        path = PurePosixPath(name)
        if path.parent == folder or (recursive and folder in path.parents):
            found.append(name)
    return sorted(found, key=str.casefold)


def header_names(cells: Sequence[object]) -> list[str]:
    """標題列 → 欄名：去掉尾端的空白欄；空白的欄名為 F<第幾欄>（與 Tableau 相同），整數不帶小數點。"""
    values: list[str] = []
    for cell in cells:
        if cell is None:
            values.append("")
        elif isinstance(cell, float) and cell.is_integer():
            values.append(str(int(cell)))
        else:
            values.append(str(cell).strip())
    while values and not values[-1]:
        values.pop()
    return [value or f"F{i + 1}" for i, value in enumerate(values)]


def excel_tables(data: bytes, path: str, sheets: str | None) -> list[Matched]:
    """Excel 檔中符合模式的工作表與各自的欄名（第一列）。"""
    found: list[tuple[str, list[str]]] = []
    if path.lower().endswith(".xls"):
        import xlrd

        book = xlrd.open_workbook(file_contents=data)
        for sheet in book.sheets():
            cells = (
                [sheet.cell_value(0, c) or None for c in range(sheet.ncols)] if sheet.nrows else []
            )
            found.append((sheet.name, header_names(cells)))
    else:
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for title in book.sheetnames:
                first = next(book[title].iter_rows(min_row=1, max_row=1, values_only=True), ())
                found.append((title, header_names(list(first))))
        finally:
            book.close()
    return [
        Matched(path=path, sheet=title, columns=names)
        for title, names in found
        if names and matches(sheets, title)
    ]


def text_table(data: bytes, path: str, separator: str, charset: str) -> Matched:
    """文字檔的欄名（第一列）。"""
    codec = "utf-8-sig" if charset.replace("_", "-").lower() in ("utf-8", "utf8") else charset
    try:
        text = data.decode(codec)
    except (LookupError, UnicodeDecodeError) as exc:
        raise WildcardError(f"{path} 不是 {charset} 編碼：{exc}") from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator)
    first = next(reader, [])
    return Matched(path=path, sheet=None, columns=header_names(list(first)))


def expand(
    element: etree._Element,
    source: etree._Element,
    anchor: str,
    names: list[str],
    read: Callable[[str], bytes],
    prefix: str,
) -> tuple[etree._Element, dict[str, etree._Element]]:
    """batch-union 的元素 → 一般聯集的元素，與各資料表用到的具名連線（名稱 → <connection>）。

    source 是 batch-union 的連線，anchor 是它的檔案在 .twbx 中的路徑；names 是 .twbx 中的檔案，read 讀取其中
    一個檔案；prefix 是新的具名連線的前綴。"""
    files = pattern_of(element.find("database-filter"))
    sheets = pattern_of(element.find("table-filter"))
    recursive = element.get("is-recursive") == "true"
    columns_el = element.find("columns")
    declared = {
        c.get("name", ""): c.get("datatype", "string")
        for c in (columns_el.iter("column") if columns_el is not None else [])
        if c.get("name")
    }
    text = source.get("class") == "textscan"
    separator = (columns_el.get("separator") if columns_el is not None else None) or ","
    separator = "\t" if separator == "\\t" else separator
    charset = (columns_el.get("character-set") if columns_el is not None else None) or "UTF-8"
    found: list[Matched] = []
    for name in candidates(names, anchor, recursive):
        if not matches(files, PurePosixPath(name).name):
            continue
        if text:
            found.append(text_table(read(name), name, separator, charset))
            continue
        if not name.lower().endswith((".xlsx", ".xlsm", ".xls")):
            continue
        try:
            found.extend(excel_tables(read(name), name, sheets))
        except Exception as exc:  # 壞掉的檔案：與 Tableau 相同無法連線，列為不支援
            raise WildcardError(f"{name} 不是可讀取的 Excel 檔：{exc}") from exc
    if not found:
        raise WildcardError(f"萬用字元聯集「{files or '*'}」在 .twbx 中找不到符合的檔案")
    union = etree.Element("relation")
    union.set("name", element.get("name") or "Union")
    union.set("type", "union")
    union.set(WILDCARD_ATTRS[0], files or "*")
    union.set(WILDCARD_ATTRS[1], sheets or "")
    union.set(WILDCARD_ATTRS[2], "true" if recursive else "false")
    connections: dict[str, etree._Element] = {}
    taken: set[str] = set()
    folder = _original_folder(source)
    base_dir = PurePosixPath(anchor).parent
    for i, item in enumerate(found):
        key = f"{prefix}.{i}"
        connection = copy.deepcopy(source)
        packaged = PurePosixPath(item.path)
        if text:
            connection.set("directory", str(packaged.parent))
            connection.set("filename", packaged.name)
        else:
            connection.set("filename", item.path)
        connections[key] = connection
        label = item.sheet or packaged.name
        name = label if label not in taken else f"{label} ({packaged.name})"
        n = 2
        while name in taken:
            name = f"{label} ({packaged.name} {n})"
            n += 1
        taken.add(name)
        part = etree.SubElement(union, "relation")
        part.set("connection", key)
        part.set("name", name)
        part.set("type", "table")
        part.set(PATH_ATTR, _join(folder, str(packaged.relative_to(base_dir))))
        if text:
            stem, dot, ext = packaged.name.rpartition(".")
            part.set("table", f"[{stem}#{ext}]" if dot else f"[{packaged.name}]")
        else:
            part.set("table", f"[{item.sheet}$]")
        cols = etree.SubElement(part, "columns")
        cols.set("header", "yes")
        if columns_el is not None:
            for attr in ("separator", "character-set", "locale"):
                value = columns_el.get(attr)
                if value:
                    cols.set(attr, value)
        for ordinal, column in enumerate(item.columns):
            col = etree.SubElement(cols, "column")
            col.set("datatype", declared.get(column, "string"))
            col.set("name", column)
            col.set("ordinal", str(ordinal))
    return union, connections


def _original_folder(source: etree._Element) -> str:
    """連線的檔案在作者電腦上的資料夾（Path 欄的值的前段）。"""
    directory = (source.get("directory") or "").rstrip("/\\")
    if directory:
        return directory
    filename = source.get("filename") or ""
    cut = max(filename.rfind("/"), filename.rfind("\\"))
    return filename[:cut] if cut >= 0 else ""


def _join(folder: str, relative: str) -> str:
    if not folder:
        return relative
    slash = "\\" if "\\" in folder else "/"
    return folder + slash + relative.replace("/", slash)


__all__ = [
    "GENERATED",
    "PATH_ATTR",
    "WILDCARD_ATTRS",
    "Matched",
    "WildcardError",
    "candidates",
    "excel_tables",
    "expand",
    "header_names",
    "matches",
    "pattern_of",
    "text_table",
]
