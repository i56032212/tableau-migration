"""群組（categorical-bin）與數值分組（bin）：轉成等價的 Tableau 公式，之後與一般計算欄位相同處理。

群組：列出的成員對應到群組名稱；其餘成員在有「其他」（default）時歸入其他，否則維持原值。
Null 也可以是成員（%null%）。

    IF ISNULL([Result]) THEN "DRAW"
    ELSEIF [Result] IN ("Lose", "Lose D/L") THEN "LOSE"
    ELSE [Result]
    END

數值分組：值為所在區間的下限，區間由 peg 起、每 size 一格（size 可以是參數）。

    FLOOR(([Age]) / [Parameters].[Age (bin) Parameter]) * [Parameters].[Age (bin) Parameter]
"""

from __future__ import annotations

import math

from lxml import etree

from tmig.ir.models import Scalar
from tmig.parser.twbx import children, text_of
from tmig.parser.values import parse_literal

CONVERT = {
    "string": "STR",
    "integer": "INT",
    "real": "FLOAT",
    "date": "DATE",
    "datetime": "DATETIME",
}


class GroupError(ValueError):
    pass


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
            raise GroupError(f"無法寫成公式的數值 {value}")
        return repr(value)
    if datatype in ("date", "datetime"):
        return f"#{value}#"
    return '"' + value.replace('"', '""') + '"'


def group_formula(column: etree._Element, calc: etree._Element, source_datatype: str | None) -> str:
    """categorical-bin → 公式。source_datatype 為來源欄位的型別（不知道時為 None）。"""
    source = calc.get("column")
    if not source:
        raise GroupError("沒有來源欄位")
    datatype = column.get("datatype") or "string"
    if datatype not in CONVERT:
        raise GroupError(f"群組的型別 {datatype} 不支援")
    branches: list[tuple[list[Scalar], Scalar]] = []
    null_group: list[Scalar] = []
    for bin_el in children(calc, "bin"):
        name = parse_literal(bin_el.get("value"), datatype)
        members = [parse_literal(text_of(v), source_datatype) for v in children(bin_el, "value")]
        if any(m is None for m in members) and not null_group:
            null_group.append(name)
        values: list[Scalar] = [m for m in members if m is not None]
        if values:
            branches.append((values, name))

    default = calc.get("default")
    if default is not None:
        otherwise = formula_literal(parse_literal(default, datatype), datatype)
    elif source_datatype is None or source_datatype == datatype:
        otherwise = source
    else:
        otherwise = f"{CONVERT[datatype]}({source})"

    conditions: list[str] = []
    if null_group:
        conditions.append(f"ISNULL({source}) THEN {formula_literal(null_group[0], datatype)}")
    for values, name in branches:
        listed = ", ".join(formula_literal(v, source_datatype) for v in values)
        conditions.append(f"{source} IN ({listed}) THEN {formula_literal(name, datatype)}")
    if not conditions:
        return otherwise  # 沒有任何群組：每個成員維持原值
    lines = [f"IF {conditions[0]}", *[f"ELSEIF {c}" for c in conditions[1:]]]
    return "\n".join([*lines, f"ELSE {otherwise}", "END"])


def bin_formula(calc: etree._Element) -> str:
    """bin → 公式：所在區間的下限。"""
    expression = calc.get("formula")
    size = calc.get("size-parameter") or calc.get("size")
    if not expression or not size:
        raise GroupError("缺少分組的欄位或大小")
    if not size.startswith("["):
        try:
            number = float(size)
        except ValueError as exc:
            raise GroupError(f"無法辨識的分組大小「{size}」") from exc
        if number <= 0:
            raise GroupError(f"分組大小必須大於 0（{size}）")
        size = formula_literal(int(number) if number.is_integer() else number)
    try:
        peg = float(calc.get("peg") or "0")
    except ValueError as exc:
        raise GroupError(f"無法辨識的分組起點「{calc.get('peg')}」") from exc
    if peg == 0:
        return f"FLOOR(({expression}) / {size}) * {size}"
    anchor = formula_literal(int(peg) if peg.is_integer() else peg)
    return f"FLOOR((({expression}) - {anchor}) / {size}) * {size} + {anchor}"
