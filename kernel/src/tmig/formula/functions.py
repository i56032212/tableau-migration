"""公式函式目錄：每個函式的種類、參數個數與回傳型別規則。

這裡列出的函式都有 SQL 對應（``tmig.sqlgen.expr``）與 DuckDB 實跑測試，
等同 v0 函式集；``tmig.formula.catalog.V0_FUNCTIONS`` 由這裡產生。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from tmig.formula.ast import LiteralNode, Node
from tmig.formula.types import (
    DataType,
    FormulaSemanticError,
    Info,
    is_numeric,
    is_temporal,
    unify,
)

FunctionKind = Literal["row", "aggregate", "table_calc"]
Checker = Callable[[list[Info], list[Node], int], DataType]

DATE_PARTS = (
    "year",
    "quarter",
    "month",
    "dayofyear",
    "day",
    "weekday",
    "week",
    "hour",
    "minute",
    "second",
)
TRUNC_PARTS = ("year", "quarter", "month", "week", "day", "hour", "minute", "second")
ADD_PARTS = ("year", "quarter", "month", "week", "day", "hour", "minute", "second")
WEEK_STARTS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    kind: FunctionKind
    min_args: int
    max_args: int
    check: Checker


# ---------------------------------------------------------------- 參數檢查工具


def _need(cond: bool, message: str, pos: int) -> None:
    if not cond:
        raise FormulaSemanticError(message, pos)


def _numeric(name: str, index: int) -> Callable[[list[Info], int], None]:
    def check(args: list[Info], pos: int) -> None:
        _need(is_numeric(args[index].datatype), f"{name} 的第 {index + 1} 個參數必須是數字", pos)

    return check


def string_literal(node: Node, name: str, allowed: tuple[str, ...], pos: int) -> str:
    """日期部分等必須是字串常值的參數。"""
    if not (isinstance(node, LiteralNode) and node.kind == "string"):
        raise FormulaSemanticError(f"{name} 的日期部分必須是字串常值", pos, unsupported=True)
    value = str(node.value).lower()
    if value not in allowed:
        raise FormulaSemanticError(
            f"{name} 不支援「{node.value}」，只支援 {'、'.join(allowed)}", pos, unsupported=True
        )
    return value


def int_literal(node: Node, name: str, pos: int) -> int:
    if not (isinstance(node, LiteralNode) and node.kind == "integer"):
        raise FormulaSemanticError(f"{name} 的這個參數必須是整數常值", pos, unsupported=True)
    return int(node.value)


# ---------------------------------------------------------------- 回傳型別規則


def _same_numeric(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        _numeric(name, 0)(args, pos)
        return "integer" if args[0].datatype == "integer" else "real"

    return check


def _real_numeric(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        for i in range(len(args)):
            _numeric(name, i)(args, pos)
        return "real"

    return check


def _fixed(result: DataType, *arg_rules: str) -> Callable[[str], Checker]:
    """回傳固定型別；arg_rules 依序為 any、num、str、date、bool。"""

    def build(name: str) -> Checker:
        def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
            for i, rule in enumerate(arg_rules):
                if i >= len(args):
                    break
                t = args[i].datatype
                if rule == "num":
                    _need(is_numeric(t), f"{name} 的第 {i + 1} 個參數必須是數字", pos)
                elif rule == "str":
                    _need(t in ("string", "null"), f"{name} 的第 {i + 1} 個參數必須是字串", pos)
                elif rule == "date":
                    _need(is_temporal(t), f"{name} 的第 {i + 1} 個參數必須是日期", pos)
                elif rule == "bool":
                    _need(t in ("boolean", "null"), f"{name} 的第 {i + 1} 個參數必須是布林值", pos)
            return result

        return check

    return build


def _first_type(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        return args[0].datatype

    return check


def _unify_all(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        return unify([a.datatype for a in args], pos, name)

    return check


def _iif(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        _need(args[0].datatype in ("boolean", "null"), "IIF 的第 1 個參數必須是布林值", pos)
        return unify([a.datatype for a in args[1:]], pos, "IIF 的結果")

    return check


def _zn(name: str) -> Checker:
    return _same_numeric(name)


def _round(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        _numeric(name, 0)(args, pos)
        if len(args) == 2:
            _numeric(name, 1)(args, pos)
        return "integer" if args[0].datatype == "integer" else "real"

    return check


def _div(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        for i in range(2):
            _need(args[i].datatype in ("integer", "null"), "DIV 的參數必須是整數", pos)
        return "integer"

    return check


def _datepart(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        string_literal(nodes[0], name, DATE_PARTS, pos)
        _need(is_temporal(args[1].datatype), f"{name} 的第 2 個參數必須是日期", pos)
        if len(nodes) == 3:
            string_literal(nodes[2], name, WEEK_STARTS, pos)
        return "integer"

    return check


def _datetrunc(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        string_literal(nodes[0], name, TRUNC_PARTS, pos)
        t = args[1].datatype
        _need(is_temporal(t), f"{name} 的第 2 個參數必須是日期", pos)
        if len(nodes) == 3:
            string_literal(nodes[2], name, WEEK_STARTS, pos)
        return "date" if t == "date" else "datetime"

    return check


def _dateadd(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        string_literal(nodes[0], name, ADD_PARTS, pos)
        _need(args[1].datatype in ("integer", "null"), "DATEADD 的第 2 個參數必須是整數", pos)
        _need(is_temporal(args[2].datatype), "DATEADD 的第 3 個參數必須是日期", pos)
        return "datetime"

    return check


def _datediff(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        string_literal(nodes[0], name, TRUNC_PARTS, pos)
        _need(
            is_temporal(args[1].datatype) and is_temporal(args[2].datatype),
            "DATEDIFF 的日期參數必須是日期",
            pos,
        )
        if len(nodes) == 4:
            string_literal(nodes[3], name, WEEK_STARTS, pos)
        return "integer"

    return check


def _percentile(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        _numeric(name, 0)(args, pos)
        node = nodes[1]
        _need(
            isinstance(node, LiteralNode)
            and node.kind in ("integer", "real")
            and 0 <= node.value <= 1,
            "PERCENTILE 的第 2 個參數必須是 0 到 1 的數字常值",
            pos,
        )
        return "real"

    return check


def _window(name: str) -> Checker:
    """WINDOW_*(expr, [start, end])；start、end 為整數常值或 FIRST()、LAST()。"""

    base = name.removeprefix("WINDOW_")

    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        if len(nodes) == 2:
            raise FormulaSemanticError(f"{name} 必須同時指定起點與終點", pos)
        for node in nodes[1:]:
            window_offset(node, name, pos)
        return _window_result(base, args[0].datatype, name, pos)

    return check


def window_offset(node: Node, name: str, pos: int) -> int | None:
    """視窗位移：整數常值；FIRST() 與 LAST() 回傳 None（表示無界）。"""
    from tmig.formula.ast import Call

    if isinstance(node, Call) and node.name in ("FIRST", "LAST") and not node.args:
        return None
    if isinstance(node, LiteralNode) and node.kind == "integer":
        return int(node.value)
    raise FormulaSemanticError(
        f"{name} 的視窗位移只支援整數常值、FIRST() 或 LAST()", pos, unsupported=True
    )


def _window_result(base: str, t: str, name: str, pos: int) -> DataType:
    if base == "COUNT":
        return "integer"
    if base in ("SUM",):
        _need(is_numeric(t), f"{name} 的參數必須是數字", pos)
        return "integer" if t == "integer" else "real"
    if base in ("AVG", "MEDIAN"):
        _need(is_numeric(t), f"{name} 的參數必須是數字", pos)
        return "real"
    return t  # type: ignore[return-value]


def _running(name: str) -> Checker:
    base = name.removeprefix("RUNNING_")

    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        return _window_result(base, args[0].datatype, name, pos)

    return check


def _rank(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        if len(nodes) == 2:
            string_literal(nodes[1], name, ("asc", "desc"), pos)
        return "integer"

    return check


def _lookup(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        int_literal(nodes[1], name, pos)
        return args[0].datatype

    return check


def _total(name: str) -> Checker:
    def check(args: list[Info], nodes: list[Node], pos: int) -> DataType:
        return args[0].datatype

    return check


def _sum(name: str) -> Checker:
    return _same_numeric(name)


# ---------------------------------------------------------------- 目錄

_ROW: list[tuple[str, int, int, Callable[[str], Checker]]] = [
    ("IIF", 3, 4, _iif),
    ("ZN", 1, 1, _zn),
    ("IFNULL", 2, 2, _unify_all),
    ("ISNULL", 1, 1, _fixed("boolean", "any")),
    ("ABS", 1, 1, _same_numeric),
    ("CEILING", 1, 1, _fixed("integer", "num")),
    ("FLOOR", 1, 1, _fixed("integer", "num")),
    ("ROUND", 1, 2, _round),
    ("SQRT", 1, 1, _real_numeric),
    ("POWER", 2, 2, _real_numeric),
    ("EXP", 1, 1, _real_numeric),
    ("LN", 1, 1, _real_numeric),
    ("LOG", 1, 2, _real_numeric),
    ("SIGN", 1, 1, _fixed("integer", "num")),
    ("DIV", 2, 2, _div),
    ("LEN", 1, 1, _fixed("integer", "str")),
    ("LEFT", 2, 2, _fixed("string", "str", "num")),
    ("RIGHT", 2, 2, _fixed("string", "str", "num")),
    ("MID", 2, 3, _fixed("string", "str", "num", "num")),
    ("UPPER", 1, 1, _fixed("string", "str")),
    ("LOWER", 1, 1, _fixed("string", "str")),
    ("TRIM", 1, 1, _fixed("string", "str")),
    ("LTRIM", 1, 1, _fixed("string", "str")),
    ("RTRIM", 1, 1, _fixed("string", "str")),
    ("CONTAINS", 2, 2, _fixed("boolean", "str", "str")),
    ("STARTSWITH", 2, 2, _fixed("boolean", "str", "str")),
    ("ENDSWITH", 2, 2, _fixed("boolean", "str", "str")),
    ("FIND", 2, 3, _fixed("integer", "str", "str", "num")),
    ("REPLACE", 3, 3, _fixed("string", "str", "str", "str")),
    ("SPLIT", 3, 3, _fixed("string", "str", "str", "num")),
    ("YEAR", 1, 1, _fixed("integer", "date")),
    ("QUARTER", 1, 1, _fixed("integer", "date")),
    ("MONTH", 1, 1, _fixed("integer", "date")),
    ("DAY", 1, 1, _fixed("integer", "date")),
    ("WEEK", 1, 1, _fixed("integer", "date")),
    ("DATEPART", 2, 3, _datepart),
    ("DATETRUNC", 2, 3, _datetrunc),
    ("DATEADD", 3, 3, _dateadd),
    ("DATEDIFF", 3, 4, _datediff),
    ("MAKEDATE", 3, 3, _fixed("date", "num", "num", "num")),
    ("TODAY", 0, 0, _fixed("date")),
    ("NOW", 0, 0, _fixed("datetime")),
    ("INT", 1, 1, _fixed("integer", "any")),
    ("FLOAT", 1, 1, _fixed("real", "any")),
    ("STR", 1, 1, _fixed("string", "any")),
    ("DATE", 1, 1, _fixed("date", "any")),
    ("DATETIME", 1, 1, _fixed("datetime", "any")),
]

_AGGREGATE: list[tuple[str, int, int, Callable[[str], Checker]]] = [
    ("SUM", 1, 1, _sum),
    ("AVG", 1, 1, _real_numeric),
    ("MIN", 1, 1, _first_type),
    ("MAX", 1, 1, _first_type),
    ("COUNT", 1, 1, _fixed("integer", "any")),
    ("COUNTD", 1, 1, _fixed("integer", "any")),
    ("MEDIAN", 1, 1, _real_numeric),
    ("ATTR", 1, 1, _first_type),
    ("STDEV", 1, 1, _real_numeric),
    ("STDEVP", 1, 1, _real_numeric),
    ("VAR", 1, 1, _real_numeric),
    ("VARP", 1, 1, _real_numeric),
    ("PERCENTILE", 2, 2, _percentile),
]

_TABLE_CALC: list[tuple[str, int, int, Callable[[str], Checker]]] = [
    *[(f"RUNNING_{b}", 1, 1, _running) for b in ("SUM", "AVG", "MIN", "MAX", "COUNT")],
    *[(f"WINDOW_{b}", 1, 3, _window) for b in ("SUM", "AVG", "MIN", "MAX", "COUNT")],
    ("TOTAL", 1, 1, _total),
    ("INDEX", 0, 0, _fixed("integer")),
    ("FIRST", 0, 0, _fixed("integer")),
    ("LAST", 0, 0, _fixed("integer")),
    ("SIZE", 0, 0, _fixed("integer")),
    ("RANK", 1, 2, _rank),
    ("RANK_DENSE", 1, 2, _rank),
    ("RANK_MODIFIED", 1, 2, _rank),
    ("RANK_UNIQUE", 1, 2, _rank),
    ("LOOKUP", 2, 2, _lookup),
]


def _build() -> dict[str, FunctionSpec]:
    registry: dict[str, FunctionSpec] = {}
    for kind, entries in (("row", _ROW), ("aggregate", _AGGREGATE), ("table_calc", _TABLE_CALC)):
        for name, lo, hi, factory in entries:
            registry[name] = FunctionSpec(name, kind, lo, hi, factory(name))  # type: ignore[arg-type]
    # 兩個參數的 MIN、MAX 是列層級的「取較小／較大值」，不是彙總
    registry["MIN2"] = FunctionSpec("MIN", "row", 2, 2, _unify_all("MIN"))
    registry["MAX2"] = FunctionSpec("MAX", "row", 2, 2, _unify_all("MAX"))
    return registry


REGISTRY: dict[str, FunctionSpec] = _build()


def lookup(name: str, arg_count: int) -> FunctionSpec | None:
    if name in ("MIN", "MAX") and arg_count == 2:
        return REGISTRY[f"{name}2"]
    return REGISTRY.get(name)


def implemented_names() -> frozenset[str]:
    return frozenset(spec.name for spec in REGISTRY.values())
