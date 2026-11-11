"""檢視者的下載（與 Tableau Server 的「下載」相同）：交叉資料表（Excel、CSV）、摘要資料與完整資料（CSV）。

- 交叉資料表：工作表的資料表，與檢視器的「資料」相同（別名、數字與日期格式、小計與總計）；Excel 中看起來是
  數字的（千分位、百分比、小數、負數的括號）存成數字並套用相同的格式，可以直接計算
- 摘要資料：每個標記一列、未格式化的值
- 完整資料：工作表的基礎資料列（套用資料源篩選、列層級權限、工作表的篩選，只含仍在檢視中的標記的資料列）

CSV 為 UTF-8（含 BOM，Excel 直接開啟中文不會亂碼）。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

CSV_TYPE = "text/csv; charset=utf-8"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FULL_DATA_LIMIT = 1_000_000
"""完整資料最多下載的列數。"""

_NUMBER = re.compile(
    r"^(?P<neg>[-−])?(?P<open>\()?(?P<prefix>[$¥€£]|NT\$|US\$)?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?(?P<pct>%)?(?P<close>\))?$"
)


def excel_value(text: str) -> tuple[Any, str | None]:
    """顯示的文字 → Excel 的值與數字格式：看起來是數字時存成數字，其餘維持文字（例如 1.2K、日期）。"""
    match = _NUMBER.match(text.strip())
    if match is None or bool(match.group("open")) != bool(match.group("close")):
        return text, None
    integer = match.group("int")
    fraction = match.group("frac") or ""
    number: float | int = (
        float(integer.replace(",", "") + fraction) if fraction else int(integer.replace(",", ""))
    )
    negative = bool(match.group("neg")) or bool(match.group("open"))
    if negative:
        number = -number
    decimals = len(fraction) - 1 if fraction else 0
    pattern = ("#,##0" if "," in integer else "0") + ("." + "0" * decimals if decimals else "")
    if match.group("pct"):
        number = number / 100
        pattern += "%"
    prefix = match.group("prefix")
    if prefix:
        pattern = f'"{prefix}"{pattern}'
    if match.group("open"):
        pattern = f"{pattern};({pattern})"
    return number, pattern


def _plain(value: Any) -> Any:
    """CSV 與 Excel 的值：日期與時間寫成 ISO 格式，Decimal 轉成數字。"""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def csv_bytes(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_plain(v) for v in row])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def sheet_title(name: str) -> str:
    """Excel 工作表的名稱：最多 31 字，不能有 []:*?/\\。"""
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", name).strip() or "Sheet1"
    return cleaned[:31]


def xlsx_bytes(title: str, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = sheet_title(title)
    sheet.append(list(headers))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    widths = [len(h) for h in headers]
    for row in rows:
        values: list[Any] = []
        patterns: list[str | None] = []
        for index, item in enumerate(row):
            value, pattern = excel_value(item) if isinstance(item, str) else (_plain(item), None)
            values.append(value)
            patterns.append(pattern)
            if index < len(widths):
                widths[index] = max(widths[index], len(str(item)) if item is not None else 0)
        sheet.append(values)
        line = sheet.max_row
        for column, pattern in enumerate(patterns, start=1):
            if pattern:
                sheet.cell(row=line, column=column).number_format = pattern
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = min(60, max(8, width * 1.6 + 2))
    sheet.freeze_panes = "A2"
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def file_name(name: str, extension: str) -> str:
    """下載的檔名（Content-Disposition，RFC 5987 的 UTF-8 寫法）。"""
    from urllib.parse import quote

    safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "data"
    return f"attachment; filename*=UTF-8''{quote(safe + extension)}"


__all__ = [
    "CSV_TYPE",
    "FULL_DATA_LIMIT",
    "XLSX_TYPE",
    "csv_bytes",
    "excel_value",
    "file_name",
    "sheet_title",
    "xlsx_bytes",
]
