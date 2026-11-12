"""SQL 片段工具：識別字與常值的引號處理。產生的 SQL 為 DuckDB 方言。"""

from __future__ import annotations

import math
from typing import Any


def ident(name: str) -> str:
    """以雙引號包住識別字。"""
    return '"' + name.replace('"', '""') + '"'


def qualified(*parts: str) -> str:
    return ".".join(ident(p) for p in parts)


def string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def literal(value: Any, kind: str) -> str:
    """把常值轉成 SQL。kind 為公式的常值種類或資料型別。"""
    if value is None or kind == "null":
        return "NULL"
    if kind == "boolean":
        return "TRUE" if value else "FALSE"
    if kind == "integer":
        return str(int(value))
    if kind == "real":
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return "NULL"
        text = repr(number)
        return f"CAST({text} AS DOUBLE)"
    if kind == "string":
        return string(str(value))
    if kind == "date":
        return f"DATE {string(str(value)[:10])}"
    if kind == "datetime":
        return f"TIMESTAMP {string(str(value).replace('T', ' '))}"
    raise ValueError(f"未知的常值種類：{kind}")


def null_safe_equal(left: str, right: str) -> str:
    """NULL 也視為相等的比較，用在以維度連接。"""
    return f"({left} IS NOT DISTINCT FROM {right})"


SQL_TYPES = {
    "integer": "BIGINT",
    "real": "DOUBLE",
    "string": "VARCHAR",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "spatial": "VARCHAR",
}
"""IR 的型別 → DuckDB 的型別（空間資料是 WKT 文字）。"""


def cast(expr: str, datatype: str) -> str:
    sql_type = SQL_TYPES.get(datatype)
    if sql_type is None:
        return expr
    return f"CAST({expr} AS {sql_type})"
