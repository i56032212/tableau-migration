"""公式語法樹 → SQL（DuckDB 方言）。

三種編譯模式：

- ``row``：列層級運算式；不能含彙總或 Table Calculation
- ``agg``：在 GROUP BY 查詢中計算的彙總運算式；彙總函式的參數以 ``row`` 模式編譯
- ``post``：彙總完成後的運算式；彙總節點與 Table Calculation 節點交給 SqlEnv 換成欄位參照

語意差異的處理（與 Tableau 一致）：

- 除以 0 得 NULL；整數相除得小數
- 兩個參數的 MIN、MAX：任一為 NULL 即為 NULL
- SQRT、LN、LOG 的參數不合法時得 NULL
- 週的計算依資料源的週起始日
"""

from __future__ import annotations

from typing import Protocol

from tmig.formula.analyze import Analyzer, Target
from tmig.formula.ast import (
    Binary,
    Call,
    Case,
    FieldRef,
    If,
    InList,
    LiteralNode,
    Lod,
    Node,
    Unary,
)
from tmig.formula.functions import WEEK_STARTS, lookup, string_literal, window_offset
from tmig.formula.types import FormulaSemanticError
from tmig.sqlgen.sql import cast, literal

Mode = str  # "row" | "agg" | "post"


class SqlEnv(Protocol):
    week_start: str

    def column(self, target: Target) -> str:
        """一般欄位在目前來源中的 SQL。"""
        ...

    def param(self, target: Target) -> str:
        """參數目前值的 SQL 常值。"""
        ...

    def lod(self, node: Lod) -> str:
        """LOD 運算式在目前來源中的欄位參照。"""
        ...

    def aggregate(self, node: Call) -> str:
        """post 模式：彙總節點對應的欄位參照。"""
        ...

    def table_calc(self, node: Call) -> str:
        """post 模式：Table Calculation 節點對應的 SQL。"""
        ...


def week_offset(start: str) -> int:
    return WEEK_STARTS.index(start)


class ExprCompiler:
    def __init__(self, analyzer: Analyzer, env: SqlEnv) -> None:
        self.analyzer = analyzer
        self.env = env

    # ------------------------------------------------------------ 入口

    def row(self, node: Node) -> str:
        return self.compile(node, "row")

    def agg(self, node: Node) -> str:
        return self.compile(node, "agg")

    def post(self, node: Node) -> str:
        return self.compile(node, "post")

    def datatype(self, node: Node) -> str:
        return self.analyzer.analyze(node).datatype

    # ------------------------------------------------------------ 遞迴

    def compile(self, node: Node, mode: Mode) -> str:
        match node:
            case LiteralNode(kind=kind, value=value):
                return literal(value, kind)
            case FieldRef():
                return self._field(node, mode)
            case Unary(op=op, operand=operand):
                inner = self.compile(operand, mode)
                return f"(- {inner})" if op == "-" else f"(NOT {inner})"
            case Binary():
                return self._binary(node, mode)
            case InList(operand=operand, values=values):
                items = ", ".join(self.compile(v, mode) for v in values)
                return f"({self.compile(operand, mode)} IN ({items}))"
            case If(branches=branches, else_=else_):
                parts = [
                    f"WHEN {self.compile(c, mode)} THEN {self.compile(t, mode)}"
                    for c, t in branches
                ]
                tail = f" ELSE {self.compile(else_, mode)}" if else_ is not None else ""
                return f"(CASE {' '.join(parts)}{tail} END)"
            case Case(subject=subject, whens=whens, else_=else_):
                parts = [
                    f"WHEN {self.compile(w, mode)} THEN {self.compile(t, mode)}" for w, t in whens
                ]
                tail = f" ELSE {self.compile(else_, mode)}" if else_ is not None else ""
                return f"(CASE {self.compile(subject, mode)} {' '.join(parts)}{tail} END)"
            case Lod():
                if mode != "row":
                    raise FormulaSemanticError("LOD 只能出現在列層級的位置", node.pos)
                return self.env.lod(node)
            case Call():
                return self._call(node, mode)

    def _field(self, node: FieldRef, mode: Mode) -> str:
        target = self.analyzer.resolve(node)
        if target.kind == "param":
            return self.env.param(target)
        if target.formula is not None:
            return "(" + self.compile(self.analyzer.calc_ast(target), mode) + ")"
        if mode != "row":
            raise FormulaSemanticError(f"{node.text} 是列層級的欄位，這裡需要彙總", node.pos)
        return self.env.column(target)

    def _binary(self, node: Binary, mode: Mode) -> str:
        op = node.op
        left = self.compile(node.left, mode)
        right = self.compile(node.right, mode)
        lt = self.datatype(node.left)
        if op in ("AND", "OR", "=", "!=", "<", "<=", ">", ">="):
            sql_op = "<>" if op == "!=" else op
            return f"({left} {sql_op} {right})"
        if op == "+":
            if lt == "string" or self.datatype(node.right) == "string":
                return f"({left} || {right})"
            if lt in ("date", "datetime"):
                return self._add_days(left, right, lt)
            return f"({left} + {right})"
        if op == "-":
            if lt in ("date", "datetime"):
                return self._add_days(left, f"(- {right})", lt)
            return f"({left} - {right})"
        if op == "*":
            return f"({left} * {right})"
        if op == "/":
            return f"(CAST({left} AS DOUBLE) / NULLIF(CAST({right} AS DOUBLE), 0))"
        if op == "%":
            return f"({left} % NULLIF({right}, 0))"
        if op == "^":
            return f"pow({left}, {right})"
        raise FormulaSemanticError(f"不支援的運算子 {op}", node.pos, unsupported=True)

    def _add_days(self, value: str, days: str, datatype: str) -> str:
        if datatype == "date":
            return f"({value} + CAST({days} AS INTEGER))"
        return f"({value} + to_microseconds(CAST(round({days} * 86400000000) AS BIGINT)))"

    # ------------------------------------------------------------ 函式

    def _call(self, node: Call, mode: Mode) -> str:
        spec = lookup(node.name, len(node.args))
        if spec is None:
            raise FormulaSemanticError(f"尚不支援函式 {node.name}", node.pos, unsupported=True)
        if spec.kind == "table_calc":
            if mode != "post":
                raise FormulaSemanticError(f"{node.name} 只能在彙總之後計算", node.pos)
            return self.env.table_calc(node)
        if spec.kind == "aggregate":
            if mode == "post":
                return self.env.aggregate(node)
            if mode != "agg":
                raise FormulaSemanticError(f"{node.name} 不能用在列層級", node.pos)
            return self._aggregate(node)
        args = [self.compile(arg, mode) for arg in node.args]
        return self._row_function(node, args)

    def _aggregate(self, node: Call) -> str:
        name = node.name
        x = self.row(node.args[0])
        if name == "SUM":
            if self.datatype(node.args[0]) == "integer":
                return f"CAST(sum({x}) AS BIGINT)"
            return f"sum({x})"
        if name == "AVG":
            return f"avg({x})"
        if name == "MIN":
            return f"min({x})"
        if name == "MAX":
            return f"max({x})"
        if name == "COUNT":
            return f"count({x})"
        if name == "COUNTD":
            return f"count(DISTINCT {x})"
        if name == "MEDIAN":
            return f"median({x})"
        if name == "ATTR":
            return f"(CASE WHEN min({x}) IS NOT DISTINCT FROM max({x}) THEN min({x}) END)"
        if name == "STDEV":
            return f"stddev_samp({x})"
        if name == "STDEVP":
            return f"stddev_pop({x})"
        if name == "VAR":
            return f"var_samp({x})"
        if name == "VARP":
            return f"var_pop({x})"
        if name == "PERCENTILE":
            p = node.args[1]
            assert isinstance(p, LiteralNode)
            return f"quantile_cont({x}, {float(p.value)!r})"
        raise FormulaSemanticError(f"尚不支援彙總函式 {name}", node.pos, unsupported=True)

    def _week_start(self, node: Call, index: int) -> int:
        if len(node.args) > index:
            return week_offset(string_literal(node.args[index], node.name, WEEK_STARTS, node.pos))
        return week_offset(self.env.week_start)

    def _part(self, node: Call, index: int, allowed: tuple[str, ...]) -> str:
        return string_literal(node.args[index], node.name, allowed, node.pos)

    def _row_function(self, node: Call, a: list[str]) -> str:
        name = node.name
        nargs = len(node.args)
        if name == "IIF":
            unknown = a[3] if nargs == 4 else "NULL"
            return (
                f"(CASE WHEN {a[0]} THEN {a[1]} WHEN NOT ({a[0]}) THEN {a[2]} ELSE {unknown} END)"
            )
        if name in ("ZN",):
            return f"COALESCE({a[0]}, 0)"
        if name == "IFNULL":
            return f"COALESCE({a[0]}, {a[1]})"
        if name == "ISNULL":
            return f"({a[0]} IS NULL)"
        if name == "ABS":
            return f"abs({a[0]})"
        if name == "CEILING":
            return f"CAST(ceil({a[0]}) AS BIGINT)"
        if name == "FLOOR":
            return f"CAST(floor({a[0]}) AS BIGINT)"
        if name == "ROUND":
            digits = a[1] if nargs == 2 else "0"
            return f"round({a[0]}, CAST({digits} AS INTEGER))"
        if name == "SQRT":
            return f"(CASE WHEN {a[0]} < 0 THEN NULL ELSE sqrt({a[0]}) END)"
        if name == "POWER":
            return f"pow({a[0]}, {a[1]})"
        if name == "EXP":
            return f"exp({a[0]})"
        if name == "LN":
            return f"(CASE WHEN {a[0]} <= 0 THEN NULL ELSE ln({a[0]}) END)"
        if name == "LOG":
            if nargs == 1:
                return f"(CASE WHEN {a[0]} <= 0 THEN NULL ELSE log10({a[0]}) END)"
            return f"(CASE WHEN {a[0]} <= 0 OR {a[1]} <= 0 OR {a[1]} = 1 THEN NULL ELSE ln({a[0]}) / ln({a[1]}) END)"
        if name == "SIGN":
            return f"CAST(sign({a[0]}) AS BIGINT)"
        if name == "DIV":
            return f"(CASE WHEN {a[1]} = 0 THEN NULL ELSE CAST(trunc(CAST({a[0]} AS DOUBLE) / {a[1]}) AS BIGINT) END)"
        if name in ("MIN", "MAX"):
            fn = "least" if name == "MIN" else "greatest"
            return f"(CASE WHEN {a[0]} IS NULL OR {a[1]} IS NULL THEN NULL ELSE {fn}({a[0]}, {a[1]}) END)"
        if name == "LEN":
            return f"length({a[0]})"
        if name == "LEFT":
            return f"left({a[0]}, CAST({a[1]} AS BIGINT))"
        if name == "RIGHT":
            return f"right({a[0]}, CAST({a[1]} AS BIGINT))"
        if name == "MID":
            if nargs == 3:
                return f"substring({a[0]}, CAST({a[1]} AS BIGINT), CAST({a[2]} AS BIGINT))"
            return f"substring({a[0]}, CAST({a[1]} AS BIGINT))"
        if name in ("UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM"):
            return f"{name.lower()}({a[0]})"
        if name == "CONTAINS":
            return f"(strpos({a[0]}, {a[1]}) > 0)"
        if name == "STARTSWITH":
            return f"(left({a[0]}, length({a[1]})) = {a[1]})"
        if name == "ENDSWITH":
            return f"(right({a[0]}, length({a[1]})) = {a[1]})"
        if name == "FIND":
            if nargs == 2:
                return f"CAST(strpos({a[0]}, {a[1]}) AS BIGINT)"
            start = f"CAST({a[2]} AS BIGINT)"
            found = f"strpos(substring({a[0]}, {start}), {a[1]})"
            return f"CAST((CASE WHEN {found} = 0 THEN 0 ELSE {found} + {start} - 1 END) AS BIGINT)"
        if name == "REPLACE":
            return f"replace({a[0]}, {a[1]}, {a[2]})"
        if name == "SPLIT":
            return f"split_part({a[0]}, {a[1]}, CAST({a[2]} AS BIGINT))"
        if name in ("YEAR", "QUARTER", "MONTH", "DAY"):
            return f"CAST({name.lower()}({a[0]}) AS BIGINT)"
        if name == "WEEK":
            return self.week_number(a[0], week_offset(self.env.week_start))
        if name == "DATEPART":
            return self.date_part(self._part(node, 0, _DATEPART), a[1], self._week_start(node, 2))
        if name == "DATETRUNC":
            datatype = self.datatype(node.args[1])
            return self.date_trunc(
                self._part(node, 0, _TRUNC), a[1], datatype, self._week_start(node, 2)
            )
        if name == "DATEADD":
            part = self._part(node, 0, _TRUNC)
            return f"CAST(({a[2]} + {_interval(part, a[1])}) AS TIMESTAMP)"
        if name == "DATEDIFF":
            part = self._part(node, 0, _TRUNC)
            if part == "week":
                ws = self._week_start(node, 3)
                start = self.date_trunc("week", a[1], "datetime", ws)
                end = self.date_trunc("week", a[2], "datetime", ws)
                return f"CAST(date_diff('day', {start}, {end}) // 7 AS BIGINT)"
            return f"CAST(date_diff('{part}', {a[1]}, {a[2]}) AS BIGINT)"
        if name == "MAKEDATE":
            return (
                f"make_date(CAST({a[0]} AS BIGINT), CAST({a[1]} AS BIGINT), CAST({a[2]} AS BIGINT))"
            )
        if name == "TODAY":
            return "current_date"
        if name == "NOW":
            return "CAST(now() AS TIMESTAMP)"
        if name == "INT":
            t = self.datatype(node.args[0])
            if t == "string":
                return f"CAST(trunc(TRY_CAST({a[0]} AS DOUBLE)) AS BIGINT)"
            if t == "boolean":
                return f"CAST({a[0]} AS BIGINT)"
            if t in ("date", "datetime"):
                raise FormulaSemanticError("尚不支援把日期轉成整數", node.pos, unsupported=True)
            return f"CAST(trunc({a[0]}) AS BIGINT)"
        if name == "FLOAT":
            t = self.datatype(node.args[0])
            if t == "boolean":
                return f"CAST(CAST({a[0]} AS INTEGER) AS DOUBLE)"
            if t in ("date", "datetime"):
                raise FormulaSemanticError("尚不支援把日期轉成小數", node.pos, unsupported=True)
            return f"TRY_CAST({a[0]} AS DOUBLE)"
        if name == "STR":
            return f"CAST({a[0]} AS VARCHAR)"
        if name == "DATE":
            return f"TRY_CAST({a[0]} AS DATE)"
        if name == "DATETIME":
            return f"TRY_CAST({a[0]} AS TIMESTAMP)"
        raise FormulaSemanticError(f"尚不支援函式 {name}", node.pos, unsupported=True)

    # ------------------------------------------------------------ 日期工具（規劃器也會用）

    def week_number(self, value: str, ws: int) -> str:
        jan1 = f"make_date(year({value}), 1, 1)"
        offset = f"((dayofweek({jan1}) - {ws} + 7) % 7)"
        return f"CAST(((dayofyear({value}) - 1 + {offset}) // 7) + 1 AS BIGINT)"

    def date_part(self, part: str, value: str, ws: int) -> str:
        if part == "week":
            return self.week_number(value, ws)
        if part == "weekday":
            return f"CAST(((dayofweek({value}) - {ws} + 7) % 7) + 1 AS BIGINT)"
        return f"CAST({part}({value}) AS BIGINT)"

    def date_trunc(self, part: str, value: str, datatype: str, ws: int) -> str:
        if part == "week":
            offset = f"((dayofweek({value}) - {ws} + 7) % 7)"
            if datatype == "date":
                return f"(CAST({value} AS DATE) - CAST({offset} AS INTEGER))"
            return f"(date_trunc('day', {value}) - to_days(CAST({offset} AS INTEGER)))"
        truncated = f"date_trunc('{part}', {value})"
        return cast(truncated, "date") if datatype == "date" else cast(truncated, "datetime")


_DATEPART = (
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
_TRUNC = ("year", "quarter", "month", "week", "day", "hour", "minute", "second")


def _interval(part: str, amount: str) -> str:
    n = f"CAST({amount} AS INTEGER)"
    return {
        "year": f"to_years({n})",
        "quarter": f"to_months({n} * 3)",
        "month": f"to_months({n})",
        "week": f"to_days({n} * 7)",
        "day": f"to_days({n})",
        "hour": f"to_hours({n})",
        "minute": f"to_minutes({n})",
        "second": f"to_seconds({n})",
    }[part]


__all__ = ["ExprCompiler", "SqlEnv", "week_offset", "window_offset"]
