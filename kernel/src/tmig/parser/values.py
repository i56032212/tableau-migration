"""Tableau 在 XML 中的常值寫法：參數值、篩選成員、範圍上下限。

- ``%null%`` → None
- ``"文字"`` → 字串（內部的雙引號寫成兩個）
- ``#2024-01-01#``、``#2024-01-01 08:00:00#`` → ISO 日期或日期時間字串
- ``true``、``false`` → 布林
- 數字 → int 或 float
"""

from __future__ import annotations

import re

from tmig.ir.models import Scalar

_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_DATE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?)?$"
)


class LiteralError(ValueError):
    pass


def parse_date(text: str, datatype: str | None) -> str:
    match = _DATE.match(text.strip())
    if match is None:
        raise LiteralError(f"無法辨識的日期「{text}」")
    year, month, day, hh, mm, ss = match.groups()
    iso = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if datatype == "date" or hh is None:
        if datatype == "datetime":
            return f"{iso}T00:00:00"
        return iso
    seconds = ss or "00"
    if "." in seconds:
        whole, frac = seconds.split(".")
        seconds = f"{int(whole):02d}.{frac}"
    else:
        seconds = f"{int(seconds):02d}"
    return f"{iso}T{int(hh):02d}:{mm}:{seconds}"


def parse_literal(text: str | None, datatype: str | None = None) -> Scalar:
    """解析 Tableau 常值。datatype 為欄位或參數的型別，用來決定數字與日期的形式。"""
    if text is None:
        return None
    raw = text.strip()
    if raw == "%null%":
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('""', '"')
    if len(raw) >= 2 and raw[0] == raw[-1] == "#":
        return parse_date(raw[1:-1], datatype)
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if datatype in ("date", "datetime") and _DATE.match(raw):
        return parse_date(raw, datatype)
    if _INT.match(raw):
        value = int(raw)
        return float(value) if datatype == "real" else value
    if _FLOAT.match(raw):
        number = float(raw)
        if datatype == "integer" and number.is_integer():
            return int(number)
        return number
    if datatype == "string":
        return raw
    raise LiteralError(f"無法辨識的常值「{text}」")
