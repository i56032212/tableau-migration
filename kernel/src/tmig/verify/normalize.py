"""預期 CSV 的值正規化（E3）。

Tableau 匯出的值可能帶格式（千分位、百分比、貨幣、K／M 單位、地區化日期），
正規化後才能與查詢結果比較：

- 數值：去掉千分位與貨幣符號，百分比除以 100，括號表示負數，K／M／B 乘回單位；
  同時記下顯示到小數第幾位，供「顯示值」比對使用
- 日期：ISO、M/D/YYYY、YYYY/M/D、January 2024、2024年1月2日 等格式轉成 ISO
- 空值：空字串、Null、%null% 一律視為 NULL
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

NULL_TOKENS = frozenset({"", "null", "%null%", "(null)", "nan", "空值"})
MONTHS = {
    name.lower(): i
    for i, names in enumerate(
        [
            ("January", "Jan"),
            ("February", "Feb"),
            ("March", "Mar"),
            ("April", "Apr"),
            ("May", "May"),
            ("June", "Jun"),
            ("July", "Jul"),
            ("August", "Aug"),
            ("September", "Sep", "Sept"),
            ("October", "Oct"),
            ("November", "Nov"),
            ("December", "Dec"),
        ],
        start=1,
    )
    for name in names
}
UNITS = {"k": Decimal(1000), "m": Decimal(1000000), "b": Decimal(1000000000)}
_CURRENCY = re.compile(r"^(?:NT\$|US\$|HK\$|\$|€|£|¥|￥|NT|TWD|USD)\s*|\s*(?:元|TWD|USD)$")
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class NormalizeError(ValueError):
    pass


@dataclass(frozen=True)
class NumberText:
    value: float
    decimals: int | None
    """顯示到小數第幾位；科學記號時為 None。"""
    formatted: bool
    """是否帶千分位、百分比、貨幣或單位等格式。"""


def is_null(text: str | None) -> bool:
    return text is None or text.strip().lower() in NULL_TOKENS


def parse_number(text: str) -> NumberText:
    raw = text.strip()
    t = raw.replace("\u2212", "-").replace("\u00a0", "").replace(" ", "")
    formatted = False
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, formatted, t = True, True, t[1:-1]
    if t[:1] in ("-", "+"):
        negative ^= t[0] == "-"
        t = t[1:]
    stripped = _CURRENCY.sub("", t)
    if stripped != t:
        formatted, t = True, stripped
        if t[:1] == "-":
            negative, t = not negative, t[1:]
    percent = t.endswith("%")
    if percent:
        formatted, t = True, t[:-1]
    unit = Decimal(1)
    if t and t[-1].lower() in UNITS and _NUMBER.match(t[:-1].replace(",", "")):
        unit, formatted, t = UNITS[t[-1].lower()], True, t[:-1]
    if "," in t:
        formatted, t = True, t.replace(",", "")
    if not _NUMBER.match(t):
        raise NormalizeError(f"「{text}」不是數字")
    try:
        number = Decimal(t)
    except InvalidOperation as exc:
        raise NormalizeError(f"「{text}」不是數字") from exc
    decimals: int | None = None
    if "e" not in t.lower():
        decimals = len(t.split(".")[1]) if "." in t else 0
    if percent:
        number /= 100
        decimals = None if decimals is None else decimals + 2
    if unit != 1:
        number *= unit
        decimals = None if decimals is None else decimals - (len(str(unit)) - 1)
    return NumberText(float(-number if negative else number), decimals, formatted)


def parse_date(text: str) -> str:
    """各種日期寫法 → ISO 日期（或日期時間）字串。"""
    t = text.strip()
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", t)
    if iso:
        return _iso(*iso.groups())
    us = re.match(
        r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?)?$", t
    )
    if us:
        month, day, year, hh, mm, ss, ampm = us.groups()
        hour = _hour(hh, ampm)
        return _iso(year, month, day, hour, mm, ss)
    slash = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", t)
    if slash:
        return _iso(*slash.groups())
    zh = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?$", t)
    if zh:
        year, month, day = zh.groups()
        return _iso(year, month, day or "1")
    month_year = re.match(r"^([A-Za-z]+)\.?,?\s+(\d{4})$", t)
    if month_year and month_year.group(1).lower() in MONTHS:
        return _iso(month_year.group(2), str(MONTHS[month_year.group(1).lower()]), "1")
    month_day_year = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})$", t)
    if month_day_year and month_day_year.group(1).lower() in MONTHS:
        return _iso(
            month_day_year.group(3),
            str(MONTHS[month_day_year.group(1).lower()]),
            month_day_year.group(2),
        )
    quarter = re.match(
        r"^(?:Q([1-4])\s+(\d{4})|(\d{4})\s*Q([1-4])|(\d{4})\s*年\s*第\s*([1-4])\s*季)$", t
    )
    if quarter:
        groups = [g for g in quarter.groups() if g]
        if len(groups[0]) == 4:
            year, q = groups[0], groups[1]
        else:
            q, year = groups[0], groups[1]
        return _iso(year, str((int(q) - 1) * 3 + 1), "1")
    year_only = re.match(r"^(\d{4})$", t)
    if year_only:
        return _iso(year_only.group(1), "1", "1")
    raise NormalizeError(f"「{text}」不是可辨識的日期")


def _hour(hh: str | None, ampm: str | None) -> str | None:
    if hh is None:
        return None
    hour = int(hh)
    if ampm:
        pm = ampm.lower() == "pm"
        hour = hour % 12 + (12 if pm else 0)
    return str(hour)


def _iso(
    year: str,
    month: str,
    day: str,
    hh: str | None = None,
    mm: str | None = None,
    ss: str | None = None,
) -> str:
    try:
        date = dt.date(int(year), int(month), int(day))
    except ValueError as exc:
        raise NormalizeError(f"{year}-{month}-{day} 不是有效日期") from exc
    if hh is None or (int(hh) == 0 and int(mm or 0) == 0 and int(ss or 0) == 0):
        return date.isoformat()
    return dt.datetime(
        date.year, date.month, date.day, int(hh), int(mm or 0), int(ss or 0)
    ).isoformat()


def parse_bool(text: str) -> bool:
    t = text.strip().lower()
    if t in ("true", "t", "yes", "1", "真", "是"):
        return True
    if t in ("false", "f", "no", "0", "假", "否"):
        return False
    raise NormalizeError(f"「{text}」不是布林值")


def normalize_actual(value: Any, datatype: str) -> Any:
    """查詢結果的值 → 比較用的值。"""
    if value is None:
        return None
    if datatype in ("integer", "real"):
        return float(value)
    if datatype in ("date", "datetime"):
        if isinstance(value, dt.datetime):
            if value.time() == dt.time(0):
                return value.date().isoformat()
            return value.isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        return parse_date(str(value))
    if datatype == "boolean":
        return bool(value)
    return str(value)
