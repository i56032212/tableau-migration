"""公式分析用的資料型別與計算層級。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DataType = Literal["integer", "real", "string", "boolean", "date", "datetime", "null"]
"""null 表示型別未定，只會出現在 NULL 常值。"""

Level = Literal["const", "row", "agg", "tc"]
"""const：常值或參數；row：列層級；agg：彙總；tc：Table Calculation。"""

NUMERIC: frozenset[str] = frozenset({"integer", "real"})
TEMPORAL: frozenset[str] = frozenset({"date", "datetime"})


class FormulaSemanticError(ValueError):
    """公式語法正確，但語意不合法或尚不支援。"""

    def __init__(self, message: str, pos: int = 0, *, unsupported: bool = False) -> None:
        super().__init__(f"{message}（位置 {pos}）")
        self.message = message
        self.pos = pos
        self.unsupported = unsupported
        """True 表示 Tableau 允許、但本平台尚未支援；False 表示公式本身不合法。"""


@dataclass(frozen=True, slots=True)
class Info:
    datatype: DataType
    level: Level
    has_lod: bool = False

    @property
    def is_numeric(self) -> bool:
        return self.datatype in NUMERIC or self.datatype == "null"


def is_numeric(t: str) -> bool:
    return t in NUMERIC or t == "null"


def is_temporal(t: str) -> bool:
    return t in TEMPORAL or t == "null"


def unify(types: list[str], pos: int, what: str) -> DataType:
    """多個分支或參數合併後的型別；整數與小數合併為小數，日期與日期時間合併為日期時間。"""
    known = {t for t in types if t != "null"}
    if not known:
        return "null"
    if len(known) == 1:
        return known.pop()  # type: ignore[return-value]
    if known <= NUMERIC:
        return "real"
    if known <= TEMPORAL:
        return "datetime"
    raise FormulaSemanticError(f"{what}的型別不一致：{'、'.join(sorted(known))}", pos)


def comparable(a: str, b: str) -> bool:
    if "null" in (a, b) or a == b:
        return True
    return {a, b} <= NUMERIC or {a, b} <= TEMPORAL


def combine_levels(levels: list[Level], pos: int) -> Level:
    """運算子與一般函式的參數層級合併規則：彙總與非彙總不能混用。"""
    present = {lv for lv in levels if lv != "const"}
    if not present:
        return "const"
    if present == {"row"}:
        return "row"
    if "row" in present:
        raise FormulaSemanticError("不能混用彙總與非彙總的參數", pos)
    if "tc" in present:
        return "tc"
    return "agg"
