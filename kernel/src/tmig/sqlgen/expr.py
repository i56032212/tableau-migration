"""公式語法樹 → SQL（DuckDB 方言）。

三種編譯模式：

- ``row``：列層級運算式；不能含彙總或 Table Calculation
- ``agg``：在 GROUP BY 查詢中計算的彙總運算式；彙總函式的參數以 ``row`` 模式編譯
- ``post``：彙總完成後的運算式；彙總節點與 Table Calculation 節點交給 SqlEnv 換成欄位參照

語意差異的處理（與 Tableau 一致）：

- 除以 0 得 NULL；整數相除得小數
- 兩個參數的 MIN、MAX：任一為 NULL 即為 NULL
- SQRT、LN、LOG、ASIN、ACOS、COT 的參數不合法時得 NULL
- 週的計算依資料源的週起始日
- REGEXP_EXTRACT 沒有符合時得 NULL；取代字串的 $1 轉成 RE2 的 \\1
- 日期相減得相差的天數（兩邊都是日期時為整數）
- DATENAME 的月份與星期為英文名稱（Tableau 依工作簿的地區設定）
"""

from __future__ import annotations

import re
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
from tmig.formula.functions import (
    WEEK_STARTS,
    int_literal,
    lookup,
    string_literal,
    strptime_format,
    window_offset,
)
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
            rt = self.datatype(node.right)
            if lt in ("date", "datetime") and rt in ("date", "datetime"):
                if lt == rt == "date":
                    return f"CAST(date_diff('day', {right}, {left}) AS BIGINT)"
                seconds = f"(epoch(CAST({left} AS TIMESTAMP)) - epoch(CAST({right} AS TIMESTAMP)))"
                return f"({seconds} / 86400.0)"
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
        if name in ("CORR", "COVAR", "COVARP"):
            y = self.row(node.args[1])
            fn = {"CORR": "corr", "COVAR": "covar_samp", "COVARP": "covar_pop"}[name]
            return f"{fn}({x}, {y})"
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
        if name == "ASCII":
            return (
                f"(CASE WHEN length({a[0]}) = 0 THEN NULL ELSE CAST(unicode({a[0]}) AS BIGINT) END)"
            )
        if name == "CHAR":
            return f"chr(CAST({a[0]} AS INTEGER))"
        if name == "FINDNTH":
            parts = f"string_split({a[0]}, {a[1]})"
            n = f"CAST({a[2]} AS BIGINT)"
            position = f"length(array_to_string(list_slice({parts}, 1, {n}), {a[1]})) + 1"
            return (
                f"CAST((CASE WHEN length({a[1]}) = 0 OR {n} < 1 OR len({parts}) - 1 < {n} THEN 0 "
                f"ELSE {position} END) AS BIGINT)"
            )
        if name == "SPACE":
            # DuckDB 的 greatest 略過 Null；SPACE(Null) 應為 Null
            return f"(CASE WHEN {a[0]} IS NULL THEN NULL ELSE repeat(' ', CAST(greatest({a[0]}, 0) AS BIGINT)) END)"
        if name == "REGEXP_MATCH":
            return f"regexp_matches({a[0]}, {a[1]})"
        if name in ("REGEXP_EXTRACT", "REGEXP_EXTRACT_NTH"):
            if name == "REGEXP_EXTRACT_NTH":
                group = int_literal(node.args[2], name, node.pos)
            else:
                group = 1 if _capture_groups(node.args[1]) else 0
            # 沒有符合時為 Null（DuckDB 回傳空字串）
            extracted = f"regexp_extract({a[0]}, {a[1]}, {group})"
            return f"(CASE WHEN regexp_matches({a[0]}, {a[1]}) THEN {extracted} END)"
        if name == "REGEXP_REPLACE":
            replacement = node.args[2]
            if isinstance(replacement, LiteralNode) and replacement.kind == "string":
                repl = literal(icu_replacement(str(replacement.value)), "string")
            else:
                repl = a[2]
            return f"regexp_replace({a[0]}, {a[1]}, {repl}, 'g')"
        if name == "PI":
            return "pi()"
        if name in ("DEGREES", "RADIANS", "SIN", "COS", "TAN", "ATAN"):
            return f"{name.lower()}({a[0]})"
        if name in ("ASIN", "ACOS"):
            return f"(CASE WHEN {a[0]} BETWEEN -1 AND 1 THEN {name.lower()}({a[0]}) END)"
        if name == "ATAN2":
            return f"atan2({a[0]}, {a[1]})"
        if name == "COT":
            return f"(CASE WHEN tan({a[0]}) = 0 THEN NULL ELSE 1 / tan({a[0]}) END)"
        if name == "SQUARE":
            return f"(CAST({a[0]} AS DOUBLE) * CAST({a[0]} AS DOUBLE))"
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
            return f"CAST(({a[2]} + {interval(part, a[1])}) AS TIMESTAMP)"
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
        if name == "MAKEDATETIME":
            time = f"CAST({a[1]} AS TIMESTAMP)"
            return (
                f"(CAST(CAST({a[0]} AS DATE) AS TIMESTAMP) + ({time} - date_trunc('day', {time})))"
            )
        if name == "MAKETIME":
            # Tableau 的時間是 1899-12-30 當天的日期時間
            return (
                f"make_timestamp(1899, 12, 30, CAST({a[0]} AS BIGINT), CAST({a[1]} AS BIGINT), "
                f"CAST({a[2]} AS DOUBLE))"
            )
        if name == "DATENAME":
            part = self._part(node, 0, _DATEPART)
            if part == "month":
                return f"monthname({a[1]})"
            if part == "weekday":
                return f"dayname({a[1]})"
            return f"CAST({self.date_part(part, a[1], self._week_start(node, 2))} AS VARCHAR)"
        if name == "DATEPARSE":
            fmt = node.args[0]
            assert isinstance(fmt, LiteralNode)
            pattern = strptime_format(str(fmt.value), node.pos)
            return f"try_strptime({a[1]}, {literal(pattern, 'string')})"
        if name == "ISDATE":
            return f"(CASE WHEN {a[0]} IS NULL THEN NULL ELSE TRY_CAST({a[0]} AS TIMESTAMP) IS NOT NULL END)"
        if name == "ISOYEAR":
            return f"CAST(isoyear({a[0]}) AS BIGINT)"
        if name == "ISOWEEK":
            return f"CAST(week({a[0]}) AS BIGINT)"
        if name == "ISOWEEKDAY":
            return f"CAST(isodow({a[0]}) AS BIGINT)"
        if name == "ISOQUARTER":
            # ISO 週 1–13 為第 1 季、14–26 第 2 季、27–39 第 3 季、40 以後第 4 季
            week = f"week({a[0]})"
            return f"CAST((CASE WHEN {week} > 52 THEN 4 ELSE ({week} - 1) // 13 + 1 END) AS BIGINT)"
        if name == "TODAY":
            return "current_date"
        if name == "NOW":
            return "CAST(now() AS TIMESTAMP)"
        if name == "INT":
            t = self.datatype(node.args[0])
            if t == "string":
                return f"CAST(trunc(TRY_CAST({a[0]} AS DOUBLE)) AS BIGINT)"
            if t == "boolean":
                # 布林值先轉成 INTEGER（PostgreSQL 不能直接轉成 BIGINT）
                return f"CAST(CAST({a[0]} AS INTEGER) AS BIGINT)"
            if t in ("date", "datetime"):
                raise FormulaSemanticError("尚不支援把日期轉成整數", node.pos, unsupported=True)
            return f"CAST(trunc({a[0]}) AS BIGINT)"
        if name == "FLOAT":
            t = self.datatype(node.args[0])
            if t == "boolean":
                return f"CAST(CAST({a[0]} AS INTEGER) AS DOUBLE)"
            if t in ("date", "datetime"):
                raise FormulaSemanticError("尚不支援把日期轉成小數", node.pos, unsupported=True)
            if t in ("integer", "real"):
                return f"CAST({a[0]} AS DOUBLE)"
            return f"TRY_CAST({a[0]} AS DOUBLE)"
        if name == "STR":
            return f"CAST({a[0]} AS VARCHAR)"
        if name in ("DATE", "DATETIME"):
            # 日期之間的轉換不會失敗；只有字串等其他型別需要 TRY_CAST（無法轉換時為 Null）
            target = "DATE" if name == "DATE" else "TIMESTAMP"
            if self.datatype(node.args[0]) in ("date", "datetime"):
                return f"CAST({a[0]} AS {target})"
            return f"TRY_CAST({a[0]} AS {target})"
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


def _capture_groups(node: Node) -> int:
    """樣式常值中的擷取群組數；不是常值時當作有一個群組（Tableau 的範例都有）。"""
    if not (isinstance(node, LiteralNode) and node.kind == "string"):
        return 1
    try:
        return re.compile(str(node.value)).groups
    except re.error:
        return 1


def icu_replacement(text: str) -> str:
    """ICU 的取代字串（$1 參照群組、\\ 跳脫）→ RE2 的寫法（\\1 參照群組）。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\\\\" if nxt == "\\" else nxt)
            i += 2
            continue
        if char == "$" and i + 1 < len(text) and text[i + 1].isdigit():
            j = i + 1
            while j < len(text) and text[j].isdigit():
                j += 1
            out.append("\\" + text[i + 1 : j])
            i = j
            continue
        out.append("\\\\" if char == "\\" else char)
        i += 1
    return "".join(out)


def interval(part: str, amount: str) -> str:
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


__all__ = ["ExprCompiler", "SqlEnv", "interval", "week_offset", "window_offset"]
