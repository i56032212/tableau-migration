"""集合與群組的成員 → 等價的 Tableau 公式。

解析器以它把群組、固定成員的集合轉成計算欄位；集合動作改變成員時，查詢以同樣的方式重新產生公式
（成員的值來自檢視器，所以常值一律檢查型別與格式，不會改變公式的結構）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Literal

from tmig.ir.models import DataField, FieldSet, FieldUse, Scalar


class FormulaValueError(ValueError):
    """無法寫成公式常值的值，或無法產生公式的定義。"""


DATE_FUNCTIONS = {
    "year": "YEAR({})",
    "quarter": "QUARTER({})",
    "month": "MONTH({})",
    "day": "DAY({})",
    "week": "DATEPART('week', {})",
    "weekday": "DATEPART('weekday', {})",
    "hour": "DATEPART('hour', {})",
    "minute": "DATEPART('minute', {})",
    "second": "DATEPART('second', {})",
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?)?$")


def formula_literal(value: Scalar, datatype: str | None = None) -> str:
    """值 → Tableau 公式中的常值。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FormulaValueError(f"無法寫成公式的數值 {value}")
        return repr(value)
    if datatype in ("date", "datetime"):
        if not _DATE.match(value):
            raise FormulaValueError(f"無法辨識的日期「{value}」")
        return f"#{value}#"
    return '"' + value.replace('"', '""') + '"'


def members_condition(values: Sequence[Scalar], level: str, datatype: str | None) -> str:
    """成員清單 → 不含 Null 的條件（只會是 TRUE 或 FALSE）。"""
    present = [v for v in values if v is not None]
    conditions: list[str] = []
    if len(present) != len(values):
        conditions.append(f"ISNULL({level})")
    if present:
        listed = ", ".join(formula_literal(v, datatype) for v in present)
        test = f"{level} = {listed}" if len(present) == 1 else f"{level} IN ({listed})"
        conditions.append(f"(NOT ISNULL({level}) AND {test})")
    return conditions[0] if len(conditions) == 1 else "(" + " OR ".join(conditions) + ")"


def level_expr(name: str, use: FieldUse) -> str:
    """欄位使用 → 公式中的寫法（日期部分寫成函式）；name 為欄位的內部名稱，例如 [Region]。"""
    if use.date is None:
        return name
    if use.date.kind == "part":
        return DATE_FUNCTIONS[use.date.unit].format(name)
    return f"DATETRUNC('{use.date.unit}', {name})"


def members_formula(
    levels: Sequence[tuple[str, str]],
    members: Sequence[Sequence[Scalar]],
    mode: Literal["include", "exclude"],
) -> str:
    """成員清單 → 布林公式。levels 為各層級（公式中的寫法、值的型別）；Null 也可以是成員。"""
    if any(len(row) != len(levels) for row in members):
        raise FormulaValueError("成員的值與集合的層級數量不同")
    if len(levels) == 1:
        expr, datatype = levels[0]
        values = [row[0] for row in members]
        member = members_condition(values, expr, datatype) if values else "FALSE"
    else:
        rows = [
            " AND ".join(
                members_condition([value], expr, datatype)
                for (expr, datatype), value in zip(levels, row, strict=True)
            )
            for row in members
        ]
        member = "(" + " OR ".join(f"({r})" for r in rows) + ")" if rows else "FALSE"
    return f"NOT ({member})" if mode == "exclude" else member


CONVERT = {
    "string": "STR",
    "integer": "INT",
    "real": "FLOAT",
    "date": "DATE",
    "datetime": "DATETIME",
}
"""群組的型別與來源不同時，維持原值的成員以這些函式轉型。"""


def group_expression(
    source: str,
    source_datatype: str | None,
    groups: Sequence[tuple[Scalar, Sequence[Scalar]]],
    other: Scalar,
    datatype: str = "string",
    null_other: bool = False,
) -> str:
    """群組 → 公式：列出的成員（Null 也可以）對應到群組的名稱，其餘成員為 other（None 時維持原值，
    型別不同時轉型；null_other 時為 Null）。source 為來源欄位在公式中的寫法，例如 [Region]。

        IF ISNULL([Result]) THEN "DRAW"
        ELSEIF [Result] IN ("Lose", "Lose D/L") THEN "LOSE"
        ELSE [Result]
        END
    """
    if datatype not in CONVERT:
        raise FormulaValueError(f"群組的型別 {datatype} 不支援")
    if other is not None or null_other:
        otherwise = formula_literal(other, datatype)
    elif source_datatype is None or source_datatype == datatype:
        otherwise = source
    else:
        otherwise = f"{CONVERT[datatype]}({source})"
    conditions: list[str] = []
    # Null 是成員時先比對（第一個含 Null 的群組）
    null_group = [name for name, members in groups if None in members][:1]
    if null_group:
        conditions.append(f"ISNULL({source}) THEN {formula_literal(null_group[0], datatype)}")
    for name, members in groups:
        values = [m for m in members if m is not None]
        if values:
            listed = ", ".join(formula_literal(v, source_datatype) for v in values)
            conditions.append(f"{source} IN ({listed}) THEN {formula_literal(name, datatype)}")
    if not conditions:
        return otherwise  # 沒有任何群組：每個成員維持原值
    lines = [f"IF {conditions[0]}", *[f"ELSEIF {c}" for c in conditions[1:]]]
    return "\n".join([*lines, f"ELSE {otherwise}", "END"])


def bin_expression(expression: str, size: str, peg: float = 0) -> str:
    """數值分組 → 公式：所在區間的下限。size 為公式中的大小（常值或 [Parameters].[…]）。

    FLOOR(([Age]) / [Parameters].[Age (bin) Parameter]) * [Parameters].[Age (bin) Parameter]
    """
    if peg == 0:
        return f"FLOOR(({expression}) / {size}) * {size}"
    anchor = formula_literal(int(peg) if float(peg).is_integer() else peg)
    return f"FLOOR((({expression}) - {anchor}) / {size}) * {size} + {anchor}"


def set_levels(fields: Sequence[DataField], field_set: FieldSet) -> list[tuple[str, str]]:
    """集合各層級在公式中的寫法與值的型別（日期部分為整數）。"""
    by_id = {f.id: f for f in fields}
    levels: list[tuple[str, str]] = []
    for use in field_set.levels:
        level = by_id[use.field]
        part = use.date is not None and use.date.kind == "part"
        levels.append((level_expr(level.name, use), "integer" if part else level.datatype))
    return levels


__all__ = [
    "CONVERT",
    "DATE_FUNCTIONS",
    "FormulaValueError",
    "bin_expression",
    "formula_literal",
    "group_expression",
    "level_expr",
    "members_condition",
    "members_formula",
    "set_levels",
]
