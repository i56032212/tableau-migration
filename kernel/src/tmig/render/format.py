"""Tableau 數字格式字串 → 顯示文字與座標軸格式（F1）。

支援的寫法（與 Excel 的自訂格式相同）：

- ``n#,##0``、``#,##0.00``：數字（千分位、小數位數）
- ``p0.0%``：百分比
- ``c"$"#,##0``、``c"NT$"#,##0.00``：貨幣（引號內為符號）
- 單位換算：數字部分結尾的每個逗號除以 1000，例如 ``#,##0,.00K`` 以千為單位、``#,##0,,.00M`` 以百萬為單位
- 以分號分隔的區段：第一段用於正數（與零），第二段用於負數（以絕對值套用，負號寫在區段中），
  第三段用於零；只有一段時負數在最前面加負號
- ``C1033``：地區設定的標準貨幣格式（目前只認得 1033 美國英文：``$``、兩位小數）
- 沒有格式時依 Tableau 的「自動」格式近似處理
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
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
    scale: int = 0
    """單位換算：除以 1000 的次數（數字部分結尾的逗號數）。"""
    negative: NumberFormat | None = None
    """負數的區段：以絕對值套用，負號等符號在這段的前後綴中。"""
    zero: NumberFormat | None = None
    """零的區段。"""
    literal: str | None = None
    """區段只有文字、沒有數字（例如零顯示成「—」）：直接顯示這段文字。"""


PERCENT_TABLE_CALCS = frozenset({"percent_of_total", "percent_difference"})
PERCENT_DEFAULT = "p0.00%"
"""百分比類的快速 Table Calculation 在 Tableau 預設以百分比、兩位小數顯示。"""

_NUMERIC = re.compile(r'"[^"]*"|([#0,]+(?:\.[#0]*)?,*%?)')
_LOCALE_CURRENCY = re.compile(r"^C(\d+)%?$")
LOCALE_CURRENCY: dict[str, NumberFormat] = {
    "1033": NumberFormat(prefix="$", decimals=2, negative=NumberFormat(prefix="$-", decimals=2)),
}
"""地區設定的標準貨幣格式（Tableau 的「貨幣（標準）」）；負數依 Tableau 美國英文的顯示（$-13.38）。"""


def display_format(field_format: str | None, table_calc_type: str | None) -> str | None:
    """量值的顯示格式：百分比類的快速 Table Calculation 改以百分比顯示，其餘沿用欄位格式。"""
    if table_calc_type in PERCENT_TABLE_CALCS:
        return PERCENT_DEFAULT
    return field_format


def _sections(text: str) -> list[str]:
    """以分號拆成區段（引號內的分號不算）。"""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == ";" and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _section(pattern: str, *, literal: bool = False) -> NumberFormat | None:
    """一個區段；literal：沒有數字時當成只顯示文字的區段（負數與零的區段）。"""
    numeric = None
    for match in _NUMERIC.finditer(pattern):
        if match.group(1) and any(c in match.group(1) for c in "#0"):
            numeric = match
            break
    if numeric is None:
        return NumberFormat(literal=pattern.replace('"', "")) if literal else None
    before, after = pattern[: numeric.start()], pattern[numeric.end() :]
    if re.match(r"^[eE][+-]?0", after):
        return None  # 科學記號：交給自動格式
    spec = numeric.group(1)
    percent = spec.endswith("%")
    digits = spec.rstrip("%")
    integer, _, fraction = digits.partition(".")
    # 結尾的逗號（數字部分之後，或小數之後）是單位換算，其餘的逗號是千分位
    whole = integer.rstrip(",")
    scale = len(integer) - len(whole)
    places = fraction.rstrip(",")
    scale += len(fraction) - len(places)
    return NumberFormat(
        prefix=before.replace('"', ""),
        suffix=after.replace('"', ""),
        decimals=len(places),
        grouping="," in whole,
        percent=percent,
        scale=scale,
    )


def parse_format(text: str | None) -> NumberFormat | None:
    if not text:
        return None
    locale = _LOCALE_CURRENCY.match(text)
    if locale:
        return LOCALE_CURRENCY.get(locale.group(1))
    sections = _sections(text)
    head = sections[0]
    if len(head) > 1 and head[0] in "npcC*":
        head = head[1:]
    first = _section(head)
    if first is None:
        return None
    negative = _section(sections[1], literal=True) if len(sections) > 1 and sections[1] else None
    zero = _section(sections[2], literal=True) if len(sections) > 2 and sections[2] else None
    return replace(first, negative=negative, zero=zero)


def _round(number: float, decimals: int) -> Decimal:
    """四捨五入（.5 一律進位，與 Tableau、Excel 的顯示相同），以最短十進位表示為準。"""
    return Decimal(repr(number)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _digits(number: Decimal, decimals: int, grouping: bool) -> str:
    text = f"{abs(number):{',' if grouping else ''}.{decimals}f}"
    return text


def _apply(number: float, fmt: NumberFormat, signed: bool) -> str:
    if fmt.literal is not None:
        return fmt.literal
    if fmt.percent:
        number *= 100
    if fmt.scale:
        number /= 1000**fmt.scale
    decimals = fmt.decimals if fmt.decimals is not None else 2
    rounded = _round(number, decimals)
    sign = "-" if signed and rounded < 0 else ""
    percent = "%" if fmt.percent else ""
    return f"{sign}{fmt.prefix}{_digits(rounded, decimals, fmt.grouping)}{percent}{fmt.suffix}"


def format_number(
    value: float | int | Decimal,
    fmt: NumberFormat | None,
    integer: bool = False,
    fixed: bool = False,
) -> str:
    """fixed：自動格式時小數位數固定（這一欄有不是整數的值；與 Tableau 相同，同一欄的小數位數一致，
    例如 0.00 與 14.10），不去掉結尾的 0。"""
    number = float(value)
    if fmt is None and fixed and not integer and abs(number) < 1000:
        rounded = _round(number, 2)
        return f"{'-' if rounded < 0 else ''}{_digits(rounded, 2, True)}"
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
    if number < 0 and fmt.negative is not None:
        return _apply(-number, fmt.negative, signed=False)
    if number == 0 and fmt.zero is not None:
        return _apply(0.0, fmt.zero, signed=False)
    return _apply(number, fmt, signed=True)


def d3_format(fmt: NumberFormat | None) -> str | None:
    """座標軸用的 d3-format；貨幣符號只保留 $，其他符號放在標題。"""
    if fmt is None:
        return None
    decimals = fmt.decimals if fmt.decimals is not None else 2
    currency = "$" if fmt.prefix == "$" else ""
    grouping = "," if fmt.grouping else ""
    kind = "%" if fmt.percent else "f"
    return f"{currency}{grouping}.{decimals}{kind}"


def _vega_string(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _label_expr(fmt: NumberFormat, value: str) -> str:
    decimals = fmt.decimals if fmt.decimals is not None else 2
    spec = f"{',' if fmt.grouping else ''}.{decimals}{'%' if fmt.percent else 'f'}"
    scaled = f"{value} / {1000**fmt.scale}" if fmt.scale else value
    return f"{_vega_string(fmt.prefix)} + format({scaled}, '{spec}') + {_vega_string(fmt.suffix)}"


def axis_format(fmt: NumberFormat | None) -> dict[str, str] | None:
    """座標軸的格式：d3-format 表達得了時用 format，否則（單位換算、其他前後綴）以 labelExpr 組出標籤。"""
    if fmt is None:
        return None
    if not fmt.scale and fmt.prefix in ("", "$") and not fmt.suffix and fmt.negative is None:
        number = d3_format(fmt)
        return {"format": number} if number else None
    positive = _label_expr(fmt, "abs(datum.value)")
    if fmt.negative is not None and fmt.negative.literal is not None:
        negative = _vega_string(fmt.negative.literal)
    elif fmt.negative is not None:
        negative = _label_expr(fmt.negative, "abs(datum.value)")
    else:
        negative = f"'-' + {positive}"
    return {"labelExpr": f"datum.value < 0 ? {negative} : {positive}"}


_DATE_TOKEN = re.compile(
    r'"[^"]*"|yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|nn|n|ss|s|AM/PM|am/pm|q', re.IGNORECASE
)
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def format_date(value: dt.date, fmt: str) -> str | None:
    """Tableau 的日期格式（例如 ``*dddd, mmmm d, yyyy``、``mm/dd/yyyy``）→ 文字；月份與星期為英文名稱。

    m 一律為月份、n 為分鐘（Tableau 的寫法）；不是日期格式（含數字格式的 # 或 0）時回傳 None。
    """
    pattern = fmt.removeprefix("*")
    bare = re.sub(r'"[^"]*"', "", pattern)
    if not pattern or re.search(r"[#0]", bare) or not re.search(r"[ymd]", bare, re.IGNORECASE):
        return None
    moment = value if isinstance(value, dt.datetime) else dt.datetime.combine(value, dt.time())
    hour12 = moment.hour % 12 or 12
    twelve = bool(re.search(r"am/pm", bare, re.IGNORECASE))
    values = {
        "yyyy": f"{moment.year:04d}",
        "yy": f"{moment.year % 100:02d}",
        "mmmm": MONTHS[moment.month - 1],
        "mmm": MONTHS[moment.month - 1][:3],
        "mm": f"{moment.month:02d}",
        "m": str(moment.month),
        "dddd": WEEKDAYS[moment.weekday()],
        "ddd": WEEKDAYS[moment.weekday()][:3],
        "dd": f"{moment.day:02d}",
        "d": str(moment.day),
        "hh": f"{hour12 if twelve else moment.hour:02d}",
        "h": str(hour12 if twelve else moment.hour),
        "nn": f"{moment.minute:02d}",
        "n": str(moment.minute),
        "ss": f"{moment.second:02d}",
        "s": str(moment.second),
        "am/pm": "AM" if moment.hour < 12 else "PM",
        "q": str((moment.month - 1) // 3 + 1),
    }
    parts: list[str] = []
    position = 0
    for match in _DATE_TOKEN.finditer(pattern):
        parts.append(pattern[position : match.start()])
        token = match.group(0)
        parts.append(token[1:-1] if token.startswith('"') else values[token.lower()])
        position = match.end()
    parts.append(pattern[position:])
    return "".join(parts)


def fiscal_label(value: Any, kind: str, unit: str, start: int) -> str | None:
    """會計年度的年、季的顯示文字（Tableau 加上 FY）：年的部分「FY 2005」、年的截斷「FY 2005」、季的截斷
    「FY 2005 Q1」（季的部分與月、週、日不變）。值已依會計年度計算（tmig.sqlgen.query.fiscal_node）：
    截斷是會計年度（季）的第一天，會計年度以結束的年份命名。start 為 1（日曆年）時回傳 None。"""
    if start == 1 or value is None:
        return None
    if kind == "part" and unit == "year":
        return f"FY {value}"
    if kind == "trunc" and unit in ("year", "quarter") and isinstance(value, dt.date):
        year = value.year + (value.month + 12 - start) // 12
        if unit == "year":
            return f"FY {year}"
        return f"FY {year} Q{(value.month - start) % 12 // 3 + 1}"
    return None


DATE_LABELS = {
    "year": "%Y",
    "quarter": "%Y Q%q",
    "month": "%Y-%m",
    "week": "%Y-%m-%d",
    "day": "%Y-%m-%d",
}


def format_value(
    value: Any,
    datatype: str,
    fmt: str | None = None,
    trunc: str | None = None,
    fixed: bool = False,
) -> str:
    """給標籤與工具提示用的顯示文字。fixed 見 format_number。"""
    if value is None:
        return "Null"
    if isinstance(value, bool):
        return "True" if value else "False"
    if datatype in ("integer", "real") and isinstance(value, (int, float, Decimal)):
        return format_number(value, parse_format(fmt), integer=datatype == "integer", fixed=fixed)
    if isinstance(value, dt.date) and fmt:
        text = format_date(value, fmt)
        if text is not None:
            return text
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
