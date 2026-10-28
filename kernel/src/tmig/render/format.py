"""Tableau 數字格式字串 → 顯示文字與 d3-format（F1）。

支援起步階段會遇到的寫法：

- ``n#,##0``、``#,##0.00``：數字（千分位、小數位數）
- ``p0.0%``：百分比
- ``c"$"#,##0``、``c"NT$"#,##0.00``：貨幣（引號內為符號）
- 以分號分隔的正負數格式只取第一段；沒有格式時依 Tableau 的「自動」格式近似處理
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


@dataclass(frozen=True)
class NumberFormat:
    prefix: str = ""
    suffix: str = ""
    decimals: int | None = None
    """None 表示自動。"""
    grouping: bool = True
    percent: bool = False


PERCENT_TABLE_CALCS = frozenset({"percent_of_total", "percent_difference"})
PERCENT_DEFAULT = "p0.00%"
"""百分比類的快速 Table Calculation 在 Tableau 預設以百分比、兩位小數顯示。"""


def display_format(field_format: str | None, table_calc_type: str | None) -> str | None:
    """量值的顯示格式：百分比類的快速 Table Calculation 改以百分比顯示，其餘沿用欄位格式。"""
    if table_calc_type in PERCENT_TABLE_CALCS:
        return PERCENT_DEFAULT
    return field_format


def parse_format(text: str | None) -> NumberFormat | None:
    if not text:
        return None
    pattern = text.split(";")[0]
    if len(pattern) > 1 and pattern[0] in "npcC*":
        pattern = pattern[1:]
    numeric = None
    for match in re.finditer(r'"[^"]*"|([#0,]+(?:\.[#0]+)?%?)', pattern):
        if match.group(1):
            numeric = match
            break
    if numeric is None:
        return None
    before, after = pattern[: numeric.start()], pattern[numeric.end() :]
    if re.match(r"^[eE][+-]?0", after):
        return None  # 科學記號：交給自動格式
    spec = numeric.group(1)
    digits = spec.rstrip("%")
    return NumberFormat(
        prefix=before.replace('"', ""),
        suffix=after.replace('"', ""),
        decimals=len(digits.split(".")[1]) if "." in digits else 0,
        grouping="," in digits,
        percent=spec.endswith("%"),
    )


def _round(number: float, decimals: int) -> Decimal:
    """四捨五入（.5 一律進位，與 Tableau、Excel 的顯示相同），以最短十進位表示為準。"""
    return Decimal(repr(number)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _digits(number: Decimal, decimals: int, grouping: bool) -> str:
    text = f"{abs(number):{',' if grouping else ''}.{decimals}f}"
    return text


def format_number(
    value: float | int | Decimal, fmt: NumberFormat | None, integer: bool = False
) -> str:
    number = float(value)
    if fmt is None:
        if integer or number.is_integer():
            rounded = _round(number, 0)
            return f"{'-' if rounded < 0 else ''}{_digits(rounded, 0, True)}"
        decimals = 0 if abs(number) >= 1000 else 2 if abs(number) >= 1 else 4
        rounded = _round(number, decimals)
        text = _digits(rounded, decimals, True)
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{'-' if rounded < 0 else ''}{text}"
    if fmt.percent:
        number *= 100
    decimals = fmt.decimals if fmt.decimals is not None else 2
    rounded = _round(number, decimals)
    sign = "-" if rounded < 0 else ""
    return f"{sign}{fmt.prefix}{_digits(rounded, decimals, fmt.grouping)}{'%' if fmt.percent else ''}{fmt.suffix}"


def d3_format(fmt: NumberFormat | None) -> str | None:
    """座標軸用的 d3-format；貨幣符號只保留 $，其他符號放在標題。"""
    if fmt is None:
        return None
    decimals = fmt.decimals if fmt.decimals is not None else 2
    currency = "$" if fmt.prefix == "$" else ""
    grouping = "," if fmt.grouping else ""
    kind = "%" if fmt.percent else "f"
    return f"{currency}{grouping}.{decimals}{kind}"


DATE_LABELS = {
    "year": "%Y",
    "quarter": "%Y Q%q",
    "month": "%Y-%m",
    "week": "%Y-%m-%d",
    "day": "%Y-%m-%d",
}


def format_value(
    value: Any, datatype: str, fmt: str | None = None, trunc: str | None = None
) -> str:
    """給標籤與工具提示用的顯示文字。"""
    if value is None:
        return "Null"
    if isinstance(value, bool):
        return "True" if value else "False"
    if datatype in ("integer", "real") and isinstance(value, (int, float, Decimal)):
        return format_number(value, parse_format(fmt), integer=datatype == "integer")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        if trunc == "year":
            return str(value.year)
        if trunc == "quarter":
            return f"{value.year} Q{(value.month - 1) // 3 + 1}"
        if trunc == "month":
            return f"{value.year}-{value.month:02d}"
        return value.isoformat()
    return str(value)
