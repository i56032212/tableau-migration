"""群組（categorical-bin）、數值分組（bin）與使用者篩選（identity-set）：轉成等價的 Tableau 公式，
之後與一般計算欄位相同處理。

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

import re
from dataclasses import dataclass

from lxml import etree

from tmig.ir.models import Scalar
from tmig.ir.setformula import (
    CONVERT,
    FormulaValueError,
    bin_expression,
    formula_literal,
    group_expression,
    members_condition,
)
from tmig.parser.twbx import children, text_of
from tmig.parser.values import parse_literal

GroupError = FormulaValueError
"""無法轉成公式的群組或集合定義。"""


@dataclass(frozen=True)
class GroupDefinition:
    """群組的定義：來源欄位（公式中的寫法）、各組的名稱與成員、「其他」的名稱（None 為維持原值）。"""

    source: str
    groups: list[tuple[Scalar, list[Scalar]]]
    other: Scalar
    has_other: bool


@dataclass(frozen=True)
class BinDefinition:
    """數值分組的定義：來源（公式）、大小（常值或參數的寫法）與起點。"""

    expression: str
    size: float | None
    size_parameter: str | None
    peg: float


def group_formula(
    column: etree._Element, calc: etree._Element, source_datatype: str | None
) -> tuple[str, GroupDefinition]:
    """categorical-bin → 公式與定義。source_datatype 為來源欄位的型別（不知道時為 None）。"""
    source = calc.get("column")
    if not source:
        raise GroupError("沒有來源欄位")
    datatype = column.get("datatype") or "string"
    if datatype not in CONVERT:
        raise GroupError(f"群組的型別 {datatype} 不支援")
    groups: list[tuple[Scalar, list[Scalar]]] = []
    for bin_el in children(calc, "bin"):
        name = parse_literal(bin_el.get("value"), datatype)
        members = [parse_literal(text_of(v), source_datatype) for v in children(bin_el, "value")]
        if members:
            groups.append((name, members))
    default = calc.get("default")
    other = parse_literal(default, datatype) if default is not None else None
    formula = group_expression(
        source, source_datatype, groups, other, datatype, null_other=default is not None
    )
    return formula, GroupDefinition(source, groups, other, default is not None)


def bin_formula(calc: etree._Element) -> tuple[str, BinDefinition]:
    """bin → 公式（所在區間的下限）與定義。"""
    expression = calc.get("formula")
    parameter = calc.get("size-parameter")
    size = parameter or calc.get("size")
    if not expression or not size:
        raise GroupError("缺少分組的欄位或大小")
    number: float | None = None
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
    definition = BinDefinition(expression, number, size if number is None else None, peg)
    return bin_expression(expression, size, peg), definition


_STRINGS = re.compile(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|//[^\n]*')


def identity_set_level(group: etree._Element) -> str:
    """使用者篩選的層級欄位（例如 [Region]）。"""
    roots = children(group, "groupfilter")
    if len(roots) != 1:
        raise GroupError("結構不明")
    levels = {g.get("level") for g in roots[0].iter("groupfilter") if g.get("level")}
    if len(levels) != 1:
        raise GroupError("必須剛好以一個欄位為層級")
    level = levels.pop()
    assert level is not None
    return level


def identity_set_formula(group: etree._Element, source_datatype: str | None) -> str:
    """Tableau 的使用者篩選（伺服器 → 建立使用者篩選，user:ui-builder='identity-set'）→ 布林公式。

    結構為成員的聯集，每個分支以使用者條件（ISMEMBEROF、ISUSERNAME 等）篩選一組成員：

        intersection(level-members, union(filter(false, …), filter(ISMEMBEROF('A'), member(East)), …))

    轉成 ``ISMEMBEROF("All Users") AND (((ISMEMBEROF('A')) AND (NOT ISNULL([Region]) AND
    [Region] = "East")) OR …)``。成員的比較不含 Null（只會是 TRUE 或 FALSE），Null 是成員時寫成 ISNULL。
    開頭的 ISMEMBEROF("All Users") 對每個登入者都成立：讓公式一定用到使用者函式，即使沒有對應任何使用者，
    這個欄位也屬於列層級權限（網址參數改不了），而且沒有登入者時查詢失敗。source_datatype 為層級欄位的型別。
    """
    level = identity_set_level(group)
    condition = _set_condition(children(group, "groupfilter")[0], level, source_datatype)
    return f'ISMEMBEROF("All Users") AND ({condition})'


def _set_condition(node: etree._Element, level: str, datatype: str | None) -> str:
    function = node.get("function")
    parts = children(node, "groupfilter")
    if function == "level-members":
        return "TRUE"
    if function == "empty-level":
        return "FALSE"
    if function == "member":
        return members_condition([parse_literal(node.get("member"), datatype)], level, datatype)
    if function in ("union", "intersection"):
        union = function == "union"
        members = [p for p in parts if p.get("function") == "member"]
        conditions = [_set_condition(p, level, datatype) for p in parts if p not in members]
        if members:
            values = [parse_literal(m.get("member"), datatype) for m in members]
            if union:
                conditions.insert(0, members_condition(values, level, datatype))
            else:
                conditions[:0] = [members_condition([v], level, datatype) for v in values]
        if not conditions:
            return "FALSE" if union else "TRUE"
        return (
            conditions[0]
            if len(conditions) == 1
            else "(" + (" OR " if union else " AND ").join(conditions) + ")"
        )
    if function == "except":
        if not parts:
            raise GroupError("except 沒有內容")
        kept = _set_condition(parts[0], level, datatype)
        removed = [_set_condition(p, level, datatype) for p in parts[1:]]
        return f"({kept}{''.join(f' AND NOT {r}' for r in removed)})"
    if function == "filter":
        expression = node.get("expression")
        if not expression or "[" in _STRINGS.sub("", expression):
            raise GroupError("條件參照欄位的集合（條件集合）v0 不支援")
        if len(parts) != 1:
            raise GroupError("條件的成員結構不明")
        # 使用者條件在查詢時代入成 TRUE／FALSE（不包成 IFNULL：Oracle 的條件中不能有布林值）
        return f"(({expression}) AND {_set_condition(parts[0], level, datatype)})"
    raise GroupError(f"集合的成員定義（function={function}）不支援")
