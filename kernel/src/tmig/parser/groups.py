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

import math
import re

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


def members_condition(values: list[Scalar], level: str, datatype: str | None) -> str:
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
