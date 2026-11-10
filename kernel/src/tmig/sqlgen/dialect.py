"""兩段式執行的拆分與方言轉譯（第三階段 N2、N3）。

工作表 SQL 以 DuckDB 方言產生（tmig.sqlgen.query）。資料源是 Live 連線時：

- 拆分：不含 Table Calculation 的 CTE（運算順序第 1–6 段，以及依來源資料計算的排序值與 TOTAL）
  下推到來源資料庫；Table Calculation 與輸出一律在 DuckDB 執行。DuckDB 用到的每一段下推結果各取一次
  （Arrow），以同名的資料表交給 DuckDB
- 轉譯：以 SQLGlot 解析 DuckDB 方言，逐一改寫成目標方言中語意相同的寫法（日期的部分與截斷、
  日期加減、DATEDIFF 的邊界計數、星期的編號、字串、正規表示式、百分位數、轉型等），再產生目標方言。
  轉譯後的每個節點都要在該方言的允許清單中，或是改寫規則產生的；其他節點一律列為不支援，
  不送出可能算錯的 SQL（SQLGlot 對不認得的函式不會報錯，只會原樣輸出）
- 自訂 SQL 以佔位資料表表示，產生目標方言後才換回原文（原文不經過轉譯）

PostgreSQL（16 以上）、Spark（Databricks 的引擎，3.4 以上）與 MySQL（8.0 以上）有實跑測試
（tests/sqlgen/test_engines.py）；Oracle（12.2 以上）、SQL Server（2017 以上）與 Snowflake 依官方文件撰寫，
尚未實機驗證：轉譯後的 SQL 以 SQLGlot 轉回 DuckDB、各自特有的函式依文件的語意模擬後執行，並有轉譯快照。

各方言已知與 DuckDB（＝萃取時的結果）不同之處，與 Tableau 直接連該資料庫時相同，不另外處理：
字串的排序與比較依資料庫的定序；Oracle 的空字串即 Null、ADD_MONTHS 在月底的行為；
STR() 轉換小數的格式依資料庫；TODAY()、NOW() 依資料庫的時區。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from tmig.ir.models import Scalar
from tmig.sqlgen.query import QueryError, WorksheetSql
from tmig.sqlgen.source import CUSTOM_SQL_PREFIX

SQLGLOT_NAMES: dict[str, str] = {
    "duckdb": "duckdb",
    "postgres": "postgres",
    "databricks": "databricks",
    "spark": "spark",
    "oracle": "oracle",
    "mysql": "mysql",
    "tsql": "tsql",
    "snowflake": "snowflake",
}
SUPPORTED: frozenset[str] = frozenset(SQLGLOT_NAMES)
"""已有轉譯規則的方言；其他方言（BigQuery、Teradata、Redshift）尚不支援。"""

PLACEHOLDER = re.compile(r"[\"`\[]" + re.escape(CUSTOM_SQL_PREFIX) + r"(\d+)[\"`\]]")
"""自訂 SQL 的佔位資料表（各方言的識別字引號：雙引號、反引號，SQL Server 的方括號）。"""
CUSTOM_SQL_PARAMETER = re.compile(
    r"<\s*\[Parameters\]\s*\.\s*\[((?:[^\]]|\]\])+)\]\s*>", re.IGNORECASE
)
"""自訂 SQL 中的參數：<[Parameters].[Parameter 1]>（內部名稱）。"""

ParameterValues = dict[str, tuple[Scalar, str]]
"""自訂 SQL 的參數：內部名稱（不含方括號）與顯示名稱 → （目前值、資料型別）。"""


class DialectError(QueryError):
    """SQL 無法轉成目標方言（一律視為不支援）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, unsupported=True)


# ---------------------------------------------------------------- 拆分


@dataclass
class Fetch:
    """一段下推到來源資料庫的查詢；結果在 DuckDB 中以 name 為資料表名稱。"""

    name: str
    sql: str
    """DuckDB 方言（轉譯前）。"""


@dataclass
class Split:
    fetches: list[Fetch]
    local_sql: str
    """DuckDB 執行的部分：Table Calculation 的 CTE 與輸出；下推的結果以同名資料表參照。"""


def split(query: WorksheetSql) -> Split:
    remote = [c for c in query.ctes if c.pushdown]
    local = [c for c in query.ctes if not c.pushdown]
    remote_names = [c.name for c in remote]
    local_names = {c.name for c in local}
    bodies = {c.name: c.body for c in remote}
    deps: dict[str, list[str]] = {}
    for cte in remote:
        if _references(cte.body, local_names):
            raise QueryError(f"內部錯誤：下推的 CTE {cte.name} 參照了在 DuckDB 執行的 CTE")
        deps[cte.name] = _references(cte.body, set(remote_names) - {cte.name})
    needed: list[str] = []
    for text in [*(c.body for c in local), query.main]:
        for name in _references(text, set(remote_names)):
            if name not in needed:
                needed.append(name)
    fetches: list[Fetch] = []
    for name in needed:
        closure = _closure(name, deps)
        ordered = [n for n in remote_names if n in closure]
        ctes = ", ".join(f'"{n}" AS ({bodies[n]})' for n in ordered)
        fetches.append(Fetch(name=name, sql=f'WITH {ctes} SELECT * FROM "{name}"'))
    head = ""
    if local:
        head = "WITH " + ", ".join(f'"{c.name}" AS ({c.body})' for c in local) + " "
    return Split(fetches=fetches, local_sql=head + query.main)


def _references(sql: str, names: set[str]) -> list[str]:
    """SQL 中參照到的資料表（只看 names 中的名稱），依出現順序。"""
    if not names:
        return []
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except ParseError as exc:  # pragma: no cover - 產生的 SQL 一定能解析
        raise QueryError(f"內部錯誤：SQL 無法解析：{exc}") from exc
    found: list[str] = []
    for table in tree.find_all(exp.Table):
        if not table.db and table.name in names and table.name not in found:
            found.append(table.name)
    return found


def _closure(name: str, deps: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(deps.get(current, []))
    return seen


# ---------------------------------------------------------------- 轉譯


def to_dialect(
    sql: str,
    dialect: str,
    custom_sql: list[str] | None = None,
    parameters: ParameterValues | None = None,
) -> str:
    """DuckDB 方言的 SQL → 目標方言；自訂 SQL 的佔位資料表換回原文（參數代入目前值）。"""
    custom = [_custom(text, dialect, parameters) for text in (custom_sql or [])]
    if dialect == "duckdb":
        return _restore(sql, custom)
    rewriter = REWRITERS.get(dialect)
    if rewriter is None:
        raise DialectError(f"{dialect} 方言的轉譯尚不支援")
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except ParseError as exc:  # pragma: no cover
        raise QueryError(f"內部錯誤：SQL 無法解析：{exc}") from exc
    tree = rewriter.rewrite(tree)
    rewriter.finish(tree)
    rewriter.validate(tree)
    out = tree.sql(dialect=SQLGLOT_NAMES[dialect], identify=True)
    return _restore(out, custom)


def _custom(text: str, dialect: str, parameters: ParameterValues | None) -> str:
    """自訂 SQL 原文：參數換成該方言的常值（與 Tableau 相同，代入目前值）。"""

    def swap(match: re.Match[str]) -> str:
        name = match.group(1).replace("]]", "]")
        if parameters is None or name not in parameters:
            raise DialectError(f"自訂 SQL 中的參數「{name}」找不到目前值")
        value, datatype = parameters[name]
        return sql_literal(value, datatype, dialect)

    return CUSTOM_SQL_PARAMETER.sub(swap, text).strip().rstrip(";").strip()


def sql_literal(value: Scalar, datatype: str, dialect: str) -> str:
    """參數值 → 來源資料庫的 SQL 常值（日期與時間用 ANSI 的 DATE、TIMESTAMP 常值）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool) or datatype == "boolean":
        if dialect == "oracle":
            return "1" if value else "0"
        if dialect == "tsql":
            return f"CAST({1 if value else 0} AS BIT)"
        return "TRUE" if value else "FALSE"
    if datatype in ("integer", "real") and isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return "NULL"
        return repr(int(value)) if datatype == "integer" else repr(float(value))
    if datatype in ("date", "datetime"):
        text = str(value)[:10] if datatype == "date" else str(value).replace("T", " ")
        if dialect == "tsql":
            # SQL Server 沒有 DATE '…' 的常值寫法；ISO 格式的字串轉型不受語言設定影響
            return f"CAST({_single_quote(text)} AS {'DATE' if datatype == 'date' else 'DATETIME2'})"
        return f"{'DATE' if datatype == 'date' else 'TIMESTAMP'} {_single_quote(text)}"
    text = str(value)
    if dialect in ("spark", "databricks", "mysql", "snowflake"):
        # 字串常值以反斜線跳脫
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if dialect == "tsql" and not text.isascii():
        return "N" + _single_quote(text)
    return _single_quote(text)


def _restore(sql: str, custom: list[str]) -> str:
    def swap(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(custom):
            raise QueryError(f"內部錯誤：找不到第 {index} 段自訂 SQL")
        # 換行包住原文：自訂 SQL 最後一行若是註解，不會吃掉右括號
        return f"(\n{custom[index]}\n)"

    return PLACEHOLDER.sub(swap, sql)


# ---------------------------------------------------------------- 節點工具


def _mark(node: exp.Expr) -> exp.Expr:
    """改寫規則產生的節點：驗證時直接通過（其子節點仍要驗證）。"""
    node.meta["tmig"] = True
    return node


def _fn(name: str, *args: exp.Expr) -> exp.Expr:
    return _mark(exp.Anonymous(this=name, expressions=list(args)))


def _num(value: float) -> exp.Expr:
    return exp.Literal.number(value)


def _str(value: str) -> exp.Expr:
    return exp.Literal.string(value)


def _type(text: str, dialect: str | None = None) -> exp.DataType:
    return exp.DataType.build(text, dialect=dialect)


def _cast(node: exp.Expr, to: str | exp.DataType, dialect: str | None = None) -> exp.Expr:
    target = to if isinstance(to, exp.DataType) else _type(to, dialect)
    return exp.Cast(this=node, to=target)


def _paren(node: exp.Expr) -> exp.Expr:
    if isinstance(node, (exp.Column, exp.Literal, exp.Paren, exp.Func)):
        return node
    return exp.Paren(this=node)


def _extract(field: str, value: exp.Expr) -> exp.Expr:
    return exp.Extract(this=exp.Var(this=field), expression=value)


def _unit(node: exp.Expr) -> str:
    unit = node.args.get("unit")
    if isinstance(unit, (exp.Var, exp.Literal)):
        return unit.name.lower()
    raise DialectError(f"內部錯誤：{node.sql('duckdb')} 沒有單位")


def _anon_name(node: exp.Expr) -> str | None:
    return node.name.lower() if isinstance(node, exp.Anonymous) else None


def _is_array(node: exp.Expr) -> bool:
    return bool(node.meta.get("array"))


def _array(node: exp.Expr) -> exp.Expr:
    node.meta["array"] = True
    return node


INTERVAL_FUNCTIONS = {
    "to_years": "year",
    "to_months": "month",
    "to_hours": "hour",
    "to_minutes": "minute",
    "to_seconds": "second",
    "to_microseconds": "microsecond",
}


def _interval_part(node: exp.Expr) -> tuple[str, exp.Expr] | None:
    """DuckDB 的 to_days(n)、to_months(n) 等 → （單位、數量）。"""
    if isinstance(node, exp.ToDays):
        return "day", node.this
    name = _anon_name(node)
    if name in INTERVAL_FUNCTIONS:
        assert isinstance(node, exp.Anonymous)
        return INTERVAL_FUNCTIONS[name], node.expressions[0]
    return None


def _literal_int(node: exp.Expr | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.name)
        except ValueError:
            return None
    if isinstance(node, exp.Neg):
        inner = _literal_int(node.this)
        return -inner if inner is not None else None
    if isinstance(node, (exp.Cast, exp.Paren)):
        return _literal_int(node.this)
    return None


def _convert_format(text: str, table: dict[str, str], quote: Callable[[str], str]) -> str:
    """strftime 的格式（DATEPARSE 轉換後的寫法）→ 資料庫的日期格式；其他文字以 quote 包成常值。"""
    out: list[str] = []
    literal: list[str] = []

    def flush() -> None:
        if literal:
            out.append(quote("".join(literal)))
            literal.clear()

    i = 0
    while i < len(text):
        if text[i] == "%" and i + 1 < len(text):
            token = text[i : i + 2]
            i += 2
            if token == "%%":
                literal.append("%")
                continue
            if token not in table:
                raise DialectError(f"日期格式 {token} 尚不能轉換")
            flush()
            out.append(table[token])
            continue
        literal.append(text[i])
        i += 1
    flush()
    return "".join(out)


def _format_literal(node: exp.Expr) -> str:
    if not (isinstance(node, exp.Literal) and node.is_string):
        raise DialectError("DATEPARSE 的格式不是常值")
    return node.name


def _double_quote(text: str) -> str:
    return '"' + text.replace('"', '\\"') + '"'


def _single_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ---------------------------------------------------------------- 允許清單

COMMON: frozenset[type[exp.Expr]] = frozenset(
    {
        # 查詢結構
        exp.Select,
        exp.From,
        exp.Where,
        exp.Group,
        exp.Having,
        exp.Order,
        exp.Ordered,
        exp.Limit,
        exp.Join,
        exp.Table,
        exp.TableAlias,
        exp.Subquery,
        exp.Union,
        exp.With,
        exp.CTE,
        exp.Alias,
        exp.Column,
        exp.Identifier,
        exp.Star,
        exp.Literal,
        exp.Null,
        exp.Paren,
        exp.Distinct,
        exp.Exists,
        exp.In,
        exp.Between,
        exp.Case,
        exp.If,
        exp.Var,
        exp.DataType,
        exp.DataTypeParam,
        # 運算子（DuckDB 與各方言語意相同；除法一律先轉成小數，SQLGlot 依方言補轉型）
        exp.Not,
        exp.And,
        exp.Or,
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.Is,
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Neg,
        exp.Cast,
        exp.Coalesce,
        exp.Nullif,
        exp.Interval,
        exp.Extract,
        # 彙總
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.StddevSamp,
        exp.StddevPop,
        exp.Corr,
        exp.CovarSamp,
        exp.CovarPop,
        # 數學
        exp.Abs,
        exp.Ceil,
        exp.Floor,
        exp.Sqrt,
        exp.Exp,
        exp.Ln,
        exp.Sin,
        exp.Cos,
        exp.Tan,
        exp.Atan,
        exp.Asin,
        exp.Acos,
        exp.Atan2,
        exp.Pow,
        exp.Sign,
        exp.Trunc,
        exp.Least,
        exp.Greatest,
        # 字串
        exp.Upper,
        exp.Lower,
        exp.Trim,
        exp.Replace,
        exp.Length,
        exp.Substring,
        exp.StrPosition,
        exp.Chr,
        exp.RegexpLike,
        # 取消彙總量值時每一列資料的編號（ROW_NUMBER() OVER (ORDER BY NULL)）
        exp.Window,
        exp.RowNumber,
    }
)


class Rewriter:
    """一種方言的轉譯規則。

    rewrite() 由下而上逐節點改寫（子節點先改寫）；handlers() 依節點型別、functions() 依 DuckDB 的
    函式名稱（SQLGlot 沒有對應型別的函式）改寫；validate() 檢查轉譯後的每個節點都在允許清單中
    或是改寫規則產生的。
    """

    name = ""
    dialect = ""
    allowed: frozenset[type[exp.Expr]] = frozenset()

    def rewrite(self, tree: exp.Expr) -> exp.Expr:
        return self._visit(tree)

    def _visit(self, node: exp.Expr) -> exp.Expr:
        for key, value in list(node.args.items()):
            if isinstance(value, exp.Expr):
                node.set(key, self._visit(value))
            elif isinstance(value, list):
                node.set(key, [self._visit(v) if isinstance(v, exp.Expr) else v for v in value])
        return self.node(node)

    def node(self, node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Table) and node.name.startswith(CUSTOM_SQL_PREFIX):
            return _mark(node)
        if isinstance(node, (exp.Add, exp.Sub)):
            part = _interval_part(node.expression)
            if part is not None:
                unit, amount = part
                if isinstance(node, exp.Sub):
                    amount = exp.Neg(this=_paren(amount))
                return self.add_interval(node.this, unit, amount)
        handler = self.handlers().get(type(node))
        if handler is not None:
            return handler(node)
        name = _anon_name(node)
        if name is not None:
            assert isinstance(node, exp.Anonymous)
            fn = self.functions().get(name)
            if fn is not None:
                return fn(list(node.expressions))
        return node

    def finish(self, tree: exp.Expr) -> None:
        """整棵樹改寫完成後的調整。"""

    def validate(self, tree: exp.Expr) -> None:
        for node in tree.walk():
            if node.meta.get("tmig") or type(node) in COMMON or type(node) in self.allowed:
                continue
            raise DialectError(f"{_describe(node)} 尚不能轉成 {self.name} 的 SQL")

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        return {}

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {}

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        raise NotImplementedError

    def boundary_diff(
        self,
        unit: str,
        start: exp.Expr,
        end: exp.Expr,
        part: Callable[[str, exp.Expr], exp.Expr],
        days: Callable[[exp.Expr, exp.Expr], exp.Expr],
    ) -> exp.Expr:
        """DATEDIFF：與 DuckDB 的 date_diff 相同，計算跨過的單位邊界數（不是經過的完整單位）。"""

        def delta(field: str) -> exp.Expr:
            return exp.Paren(
                this=exp.Sub(this=part(field, end.copy()), expression=part(field, start.copy()))
            )

        if unit == "year":
            return delta("year")
        if unit == "quarter":
            return exp.Paren(
                this=exp.Add(
                    this=exp.Mul(this=delta("year"), expression=_num(4)),
                    expression=delta("quarter"),
                )
            )
        if unit == "month":
            return exp.Paren(
                this=exp.Add(
                    this=exp.Mul(this=delta("year"), expression=_num(12)),
                    expression=delta("month"),
                )
            )
        if unit not in ("day", "hour", "minute", "second"):
            raise DialectError(f"{self.name} 的 DATEDIFF 不支援單位 {unit}")
        result: exp.Expr = exp.Paren(this=days(start.copy(), end.copy()))
        for field, factor in (("hour", 24), ("minute", 60), ("second", 60)):
            if unit == "day":
                break
            result = exp.Paren(
                this=exp.Add(
                    this=exp.Mul(this=result, expression=_num(factor)), expression=delta(field)
                )
            )
            if unit == field:
                break
        return result


def _describe(node: exp.Expr) -> str:
    if isinstance(node, exp.Anonymous):
        return f"函式 {node.name}"
    if isinstance(node, exp.Func):
        return f"函式 {node.sql_name()}"
    return f"語法 {type(node).__name__}"


# ---------------------------------------------------------------- PostgreSQL


class PostgresRewriter(Rewriter):
    """PostgreSQL 16 以上（TRY_CAST 以 pg_input_is_valid 判斷）。"""

    name = "PostgreSQL"
    dialect = "postgres"
    allowed = frozenset(
        {
            exp.Degrees,
            exp.Radians,
            exp.Pi,
            exp.Unicode,
            exp.Repeat,
            exp.Left,
            exp.Right,
            exp.SplitPart,
            exp.RegexpReplace,
            exp.Median,
            exp.PercentileCont,
            exp.Variance,
            exp.VariancePop,
            exp.IntDiv,
            exp.Mod,
            exp.NullSafeEQ,
            exp.TimestampTrunc,
            exp.DateTrunc,
            exp.TimeToUnix,
            exp.CurrentDate,
            exp.DPipe,
            exp.Boolean,
            exp.ArrayToString,
        }
    )

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit == "microsecond":
            unit, amount = "second", exp.Div(this=_paren(amount), expression=_num(1000000.0))
        interval = exp.Interval(this=_str("1"), unit=exp.Var(this=unit.upper()))
        return exp.Paren(
            this=exp.Add(
                this=_paren(value), expression=exp.Mul(this=_paren(amount), expression=interval)
            )
        )

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def extract(field: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: _extract(field, n.this)

        return {
            exp.Year: extract("YEAR"),
            exp.Quarter: extract("QUARTER"),
            exp.Month: extract("MONTH"),
            exp.Day: extract("DAY"),
            exp.Hour: extract("HOUR"),
            exp.Minute: extract("MINUTE"),
            # EXTRACT(SECOND) 含小數秒；DuckDB 的 second() 是整數秒
            exp.Second: lambda n: exp.Floor(this=_extract("SECOND", n.this)),
            exp.DayOfWeek: extract("DOW"),
            exp.DayOfYear: extract("DOY"),
            exp.Week: extract("WEEK"),
            exp.DayOfWeekIso: extract("ISODOW"),
            exp.Log: self.log,
            # PostgreSQL 的 ROUND(double) 四捨六入五成雙；轉成 NUMERIC 才是四捨五入（與 Tableau 相同）
            exp.Round: lambda n: _cast(
                _fn(
                    "ROUND",
                    _cast(n.this, "DECIMAL"),
                    _cast(n.args.get("decimals") or _num(0), "INT"),
                ),
                "DOUBLE",
            ),
            exp.DateDiff: lambda n: self.boundary_diff(
                _unit(n),
                n.expression,
                n.this,
                lambda field, value: _extract(field.upper(), value),
                lambda start, end: exp.Sub(
                    this=_cast(end, "DATE"), expression=_cast(start, "DATE")
                ),
            ),
            exp.DateFromParts: lambda n: _fn(
                "MAKE_DATE", *(_cast(n.args[k], "INT") for k in ("year", "month", "day"))
            ),
            exp.TimestampFromParts: lambda n: _fn(
                "MAKE_TIMESTAMP",
                *(_cast(n.args[k], "INT") for k in ("year", "month", "day", "hour", "min")),
                _cast(n.args["sec"], "DOUBLE"),
            ),
            exp.Monthname: lambda n: _fn("TO_CHAR", n.this, _str("FMMonth")),
            exp.Dayname: lambda n: _fn("TO_CHAR", n.this, _str("FMDay")),
            exp.TryCast: self.try_cast,
            exp.RegexpExtract: self.regexp_extract,
            exp.Split: lambda n: _array(_fn("STRING_TO_ARRAY", n.this, n.expression)),
            exp.Length: lambda n: _fn("CARDINALITY", n.this) if _is_array(n.this) else n,
            # 字串函式的位置與長度參數在 PostgreSQL 必須是 INT（DuckDB 的 SQL 轉成 BIGINT）
            exp.Left: lambda n: _mark(exp.Left(this=n.this, expression=_int(n.expression))),
            exp.Right: lambda n: _mark(exp.Right(this=n.this, expression=_int(n.expression))),
            exp.Substring: lambda n: _mark(
                exp.Substring(
                    this=n.this,
                    start=_int(n.args.get("start")),
                    length=_int(n.args.get("length")),
                )
            ),
            exp.SplitPart: lambda n: _mark(
                exp.SplitPart(
                    this=n.this,
                    delimiter=n.args.get("delimiter"),
                    part_index=_int(n.args.get("part_index")),
                )
            ),
            exp.Repeat: lambda n: _mark(exp.Repeat(this=n.this, times=_int(n.args.get("times")))),
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: _extract("ISOYEAR", a[0]),
            "now": lambda a: exp.Var(this="LOCALTIMESTAMP"),
            "try_strptime": self.parse_date,
            "list_slice": self.list_slice,
        }

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：格式不符時為 Null。PostgreSQL 的 TO_TIMESTAMP 遇到不符的文字會報錯，
        先以依格式產生的正規表示式檢查；符合樣式但日期不存在（例如 13 月）仍會報錯。"""
        fmt = _format_literal(args[1])
        pattern = "^" + _strftime_regex(fmt) + "$"
        parsed = _cast(
            _fn(
                "TO_TIMESTAMP", args[0].copy(), _str(_convert_format(fmt, PG_FORMAT, _double_quote))
            ),
            "TIMESTAMP",
        )
        matches = _mark(exp.RegexpLike(this=args[0], expression=_str(pattern)))
        return exp.Case(ifs=[exp.If(this=matches, true=parsed)])

    def log(self, node: exp.Expr) -> exp.Expr:
        if node.expression is not None and _literal_int(node.this) == 10:
            # LOG(double) 以 10 為底；LOG(b, x) 只接受 NUMERIC
            return _fn("LOG", node.expression)
        raise DialectError("PostgreSQL 只支援以 10 為底的 LOG")

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        assert isinstance(target, exp.DataType)
        name = target.sql(dialect="postgres").lower()
        text = _cast(node.this, "TEXT")
        valid = _fn("PG_INPUT_IS_VALID", text.copy(), _str(name))
        # 轉型放在 CASE 外面：常值的 CAST 會在規劃查詢時就計算，放在 THEN 裡面擋不住錯誤
        return exp.Cast(this=exp.Case(ifs=[exp.If(this=valid, true=text)]), to=target)

    def regexp_extract(self, node: exp.Expr) -> exp.Expr:
        group = _literal_int(node.args.get("group"))
        if group is None:
            raise DialectError("REGEXP_EXTRACT 的群組不是常值")
        pattern = node.expression
        if group == 0:
            # 整段符合的文字：整個樣式包成第 1 個群組
            pattern = exp.DPipe(
                this=exp.DPipe(this=_str("("), expression=pattern), expression=_str(")")
            )
            group = 1
        match = _fn("REGEXP_MATCH", node.this, pattern)
        # offset=1：索引已經從 1 起算，SQLGlot 不再調整
        return _mark(exp.Bracket(this=exp.Paren(this=match), expressions=[_num(group)], offset=1))

    def list_slice(self, args: list[exp.Expr]) -> exp.Expr:
        array, start, end = args
        if not _is_array(array):
            raise DialectError("list_slice 只支援 FINDNTH 的寫法")
        sliced = exp.Bracket(
            this=exp.Paren(this=array),
            expressions=[_mark(exp.Slice(this=start, expression=end))],
            offset=1,
        )
        return _array(_mark(sliced))


def _int(node: exp.Expr | None) -> exp.Expr | None:
    if node is None:
        return None
    return _cast(node, "INT")


STRFTIME_REGEX = {
    "%Y": r"\d{4}",
    "%y": r"\d{2}",
    "%m": r"\d{1,2}",
    "%d": r"\d{1,2}",
    "%H": r"\d{1,2}",
    "%I": r"\d{1,2}",
    "%M": r"\d{1,2}",
    "%S": r"\d{1,2}",
    "%p": r"[AaPp][Mm]",
    "%b": r"[A-Za-z]{3}",
    "%B": r"[A-Za-z]+",
    "%j": r"\d{1,3}",
    "%f": r"\d{1,6}",
}


def _strftime_regex(text: str) -> str:
    """strftime 的格式 → 檢查文字樣式的正規表示式（POSIX ARE）。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "%" and i + 1 < len(text):
            token = text[i : i + 2]
            i += 2
            if token == "%%":
                out.append("%")
            elif token in STRFTIME_REGEX:
                out.append(STRFTIME_REGEX[token])
            else:
                raise DialectError(f"日期格式 {token} 尚不能轉換")
            continue
        out.append(re.escape(text[i]) if not text[i].isalnum() and text[i] != " " else text[i])
        i += 1
    return "".join(out)


PG_FORMAT = {
    "%Y": "YYYY",
    "%y": "YY",
    "%m": "MM",
    "%d": "DD",
    "%H": "HH24",
    "%I": "HH12",
    "%M": "MI",
    "%S": "SS",
    "%p": "AM",
    "%b": "Mon",
    "%B": "Month",
    "%j": "DDD",
    "%f": "US",
}


# ---------------------------------------------------------------- Spark／Databricks


class SparkRewriter(Rewriter):
    """Spark 3.4 以上與 Databricks（連線時把工作階段的時區設為 UTC）。"""

    allowed = frozenset(
        {
            exp.Year,
            exp.Quarter,
            exp.Month,
            exp.Day,
            exp.Hour,
            exp.Minute,
            exp.Second,
            exp.DayOfYear,
            exp.Log,
            exp.Round,
            exp.Degrees,
            exp.Radians,
            exp.Pi,
            exp.Unicode,
            exp.Repeat,
            exp.Left,
            exp.Right,
            exp.SplitPart,
            exp.Variance,
            exp.VariancePop,
            exp.IntDiv,
            exp.Mod,
            exp.NullSafeEQ,
            exp.CurrentDate,
            exp.TryCast,
            exp.RegexpExtract,
            exp.DPipe,
            exp.Boolean,
            exp.ArrayToString,
        }
    )

    def __init__(self, name: str = "Spark", dialect: str = "spark") -> None:
        self.name = name
        self.dialect = dialect

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        return _fn("TIMESTAMPADD", exp.Var(this=unit.upper()), amount, value)

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def iso_index(value: exp.Expr) -> exp.Expr:
            """星期一為 0、星期日為 6（Spark 的 DAYOFWEEK 星期日為 1）。"""
            return exp.Paren(
                this=exp.Mod(
                    this=exp.Paren(this=exp.Add(this=_fn("DAYOFWEEK", value), expression=_num(5))),
                    expression=_num(7),
                )
            )

        return {
            exp.DayOfWeek: lambda n: exp.Paren(
                this=exp.Sub(this=_fn("DAYOFWEEK", n.this), expression=_num(1))
            ),
            exp.DayOfWeekIso: lambda n: exp.Paren(
                this=exp.Add(this=iso_index(n.this), expression=_num(1))
            ),
            exp.Week: lambda n: _fn("WEEKOFYEAR", n.this),
            # SQLGlot 對 DATE 會產生 TRUNC(d, 'DAY')，Spark 的 TRUNC 不支援 DAY（得 Null）
            exp.TimestampTrunc: lambda n: _fn("DATE_TRUNC", _str(_unit(n).upper()), n.this),
            exp.DateTrunc: lambda n: _fn("DATE_TRUNC", _str(_unit(n).upper()), n.this),
            exp.DateDiff: self.date_diff,
            exp.DateFromParts: lambda n: _fn(
                "MAKE_DATE", *(_cast(n.args[k], "INT") for k in ("year", "month", "day"))
            ),
            exp.TimestampFromParts: lambda n: _fn(
                "MAKE_TIMESTAMP",
                *(_cast(n.args[k], "INT") for k in ("year", "month", "day", "hour", "min")),
                _cast(n.args["sec"], "DECIMAL(18, 6)"),
            ),
            exp.TimeToUnix: lambda n: exp.Div(
                this=_fn("UNIX_MICROS", _cast(n.this, exp.DataType.build("TIMESTAMPLTZ"))),
                expression=_num(1000000.0),
            ),
            exp.Monthname: lambda n: _fn("DATE_FORMAT", n.this, _str("MMMM")),
            exp.Dayname: lambda n: _fn("DATE_FORMAT", n.this, _str("EEEE")),
            exp.RegexpReplace: self.regexp_replace,
            exp.PercentileCont: lambda n: _fn("PERCENTILE", n.this, n.expression),
            exp.Median: lambda n: _fn("PERCENTILE", n.this, _num(0.5)),
            exp.Split: lambda n: _array(_mark(n)),
            exp.Length: lambda n: _fn("SIZE", n.this) if _is_array(n.this) else n,
            exp.Trunc: self.trunc,
            # FROM t WHERE (FALSE)：Spark 會把 WHERE (FALSE) 當成資料表別名與欄位別名清單；
            # 單一值外的括號一律拿掉
            exp.Paren: lambda n: (
                n.this if isinstance(n.this, (exp.Boolean, exp.Column, exp.Literal)) else n
            ),
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": self.isoyear,
            "now": lambda a: _fn("CURRENT_TIMESTAMP"),
            "try_strptime": lambda a: _fn(
                "TRY_TO_TIMESTAMP",
                a[0],
                _str(_convert_format(_format_literal(a[1]), SPARK_FORMAT, _single_quote)),
            ),
            "list_slice": lambda a: _array(_fn("SLICE", *a)),
        }

    def isoyear(self, args: list[exp.Expr]) -> exp.Expr:
        """ISO 年：該週星期四所在的年。"""
        value = args[0]
        index = exp.Mod(
            this=exp.Paren(this=exp.Add(this=_fn("DAYOFWEEK", value.copy()), expression=_num(5))),
            expression=_num(7),
        )
        thursday = _fn(
            "DATE_ADD",
            _cast(value, "DATE"),
            exp.Sub(this=_num(3), expression=exp.Paren(this=index)),
        )
        return _fn("YEAR", thursday)

    def trunc(self, node: exp.Expr) -> exp.Expr:
        # Spark 的 TRUNC 是日期截斷；數字向 0 截斷
        value = node.this
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.LT(this=value.copy(), expression=_num(0)),
                    true=exp.Ceil(this=value.copy()),
                )
            ],
            default=exp.Floor(this=value),
        )

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        def part(field: str, value: exp.Expr) -> exp.Expr:
            return _fn({"day": "DAYOFMONTH"}.get(field, field.upper()), value)

        return self.boundary_diff(
            _unit(node),
            node.expression,
            node.this,
            part,
            lambda start, end: _fn("DATEDIFF", _cast(end, "DATE"), _cast(start, "DATE")),
        )

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        replacement = node.args.get("replacement")
        if not (isinstance(replacement, exp.Literal) and replacement.is_string):
            raise DialectError("REGEXP_REPLACE 的取代字串不是常值")
        return _fn(
            "REGEXP_REPLACE", node.this, node.expression, _str(java_replacement(replacement.name))
        )


def java_replacement(text: str) -> str:
    """RE2 的取代字串（\\1 參照群組、\\\\ 為反斜線）→ Java 的寫法（$1 參照群組、\\$ 為錢字號）。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(f"${nxt}" if nxt.isdigit() else "\\\\" if nxt == "\\" else nxt)
            i += 2
            continue
        out.append("\\$" if char == "$" else char)
        i += 1
    return "".join(out)


SPARK_FORMAT = {
    # 單一字母可解析一或兩位數（與 DuckDB 的 %d 等相同）；兩個字母只接受兩位數
    "%Y": "yyyy",
    "%y": "yy",
    "%m": "M",
    "%d": "d",
    "%H": "H",
    "%I": "h",
    "%M": "m",
    "%S": "s",
    "%p": "a",
    "%b": "MMM",
    "%B": "MMMM",
    "%j": "DDD",
    "%f": "SSSSSS",
}


# ---------------------------------------------------------------- Oracle


class OracleRewriter(Rewriter):
    """Oracle 12.2 以上：依官方文件撰寫，尚未實機驗證。

    連線時設定 NLS（日期與時間戳記的格式、小數點、英文的月份與星期名稱）。Oracle 沒有布林型別
    （23ai 以前），查詢結果中的布林值（例如布林維度）列為不支援。
    """

    name = "Oracle"
    dialect = "oracle"
    allowed = frozenset({exp.Median, exp.Mod, exp.IntDiv})

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit in ("year", "month"):
            months = (
                amount if unit == "month" else exp.Mul(this=_paren(amount), expression=_num(12))
            )
            return _fn("ADD_MONTHS", value, months)
        if unit == "microsecond":
            unit, amount = "second", exp.Div(this=_paren(amount), expression=_num(1000000.0))
        return exp.Paren(
            this=exp.Add(
                this=_paren(value), expression=_fn("NUMTODSINTERVAL", amount, _str(unit.upper()))
            )
        )

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def char(fmt: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: _fn("TO_NUMBER", _fn("TO_CHAR", n.this, _str(fmt)))

        def julian(value: exp.Expr) -> exp.Expr:
            # 儒略日以 7 除的餘數：星期一為 0（不受 NLS_TERRITORY 影響）
            return _fn("TO_NUMBER", _fn("TO_CHAR", value, _str("J")))

        def acos() -> exp.Expr:
            return _fn("ACOS", _num(-1))

        return {
            exp.Year: lambda n: _extract("YEAR", n.this),
            exp.Month: lambda n: _extract("MONTH", n.this),
            exp.Day: lambda n: _extract("DAY", n.this),
            exp.Quarter: char("Q"),
            exp.Hour: char("HH24"),
            exp.Minute: char("MI"),
            exp.Second: char("SS"),
            exp.DayOfYear: char("DDD"),
            exp.Week: char("IW"),
            exp.DayOfWeek: lambda n: _fn(
                "MOD", exp.Add(this=julian(n.this), expression=_num(1)), _num(7)
            ),
            exp.DayOfWeekIso: lambda n: exp.Paren(
                this=exp.Add(this=_fn("MOD", julian(n.this), _num(7)), expression=_num(1))
            ),
            exp.Log: self.log,
            # BINARY_DOUBLE 的 ROUND 四捨六入五成雙；NUMBER 才是四捨五入
            exp.Round: lambda n: _cast(
                _fn(
                    "ROUND",
                    _cast(n.this, "NUMBER", "oracle"),
                    n.args.get("decimals") or _num(0),
                ),
                "DOUBLE",
            ),
            exp.Pi: lambda n: acos(),
            exp.Degrees: lambda n: exp.Div(
                this=exp.Mul(this=_paren(n.this), expression=_num(180)), expression=acos()
            ),
            exp.Radians: lambda n: exp.Div(
                this=exp.Mul(this=_paren(n.this), expression=acos()), expression=_num(180)
            ),
            exp.Unicode: lambda n: _fn("ASCII", n.this),
            exp.TimestampTrunc: self.trunc,
            exp.DateTrunc: self.trunc,
            exp.DateDiff: self.date_diff,
            exp.Monthname: lambda n: _fn(
                "TO_CHAR", n.this, _str("FMMonth"), _str("NLS_DATE_LANGUAGE=ENGLISH")
            ),
            exp.Dayname: lambda n: _fn(
                "TO_CHAR", n.this, _str("FMDay"), _str("NLS_DATE_LANGUAGE=ENGLISH")
            ),
            exp.DateFromParts: self.make_date,
            exp.TimestampFromParts: self.make_timestamp,
            exp.TimeToUnix: self.epoch,
            exp.Left: lambda n: _fn("SUBSTR", n.this, _num(1), n.expression),
            exp.Right: self.right,
            exp.Repeat: lambda n: _fn(
                "RPAD",
                n.this.copy(),
                exp.Mul(this=_fn("LENGTH", n.this.copy()), expression=_paren(n.args["times"])),
                n.this,
            ),
            exp.SplitPart: self.split_part,
            exp.PercentileCont: lambda n: _mark(
                exp.WithinGroup(
                    this=_fn("PERCENTILE_CONT", n.expression),
                    expression=_mark(exp.Order(expressions=[_mark(exp.Ordered(this=n.this))])),
                )
            ),
            exp.Variance: lambda n: _fn("VAR_SAMP", n.this),
            exp.VariancePop: lambda n: _fn("VAR_POP", n.this),
            exp.NullSafeEQ: lambda n: _mark(
                exp.EQ(
                    this=_fn("DECODE", n.this, n.expression, _num(1), _num(0)), expression=_num(1)
                )
            ),
            exp.RegexpExtract: lambda n: _fn(
                "REGEXP_SUBSTR",
                n.this,
                n.expression,
                _num(1),
                _num(1),
                exp.Null(),
                n.args.get("group") or _num(0),
            ),
            exp.RegexpReplace: lambda n: _fn(
                "REGEXP_REPLACE", n.this, n.expression, n.args.get("replacement") or _str("")
            ),
            exp.TryCast: self.try_cast,
            exp.Cast: self.cast,
            exp.CurrentDate: lambda n: _fn("TRUNC", exp.Var(this="CURRENT_DATE")),
            exp.DPipe: self.concat,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: _fn("TO_NUMBER", _fn("TO_CHAR", a[0], _str("IYYY"))),
            "now": lambda a: exp.Var(this="LOCALTIMESTAMP"),
            "try_strptime": self.parse_date,
        }

    def log(self, node: exp.Expr) -> exp.Expr:
        if node.expression is None:
            raise DialectError("Oracle 的 LOG 需要底數")
        return _fn("LOG", node.this, node.expression)

    def concat(self, node: exp.Expr) -> exp.Expr:
        """Oracle 的 || 把 Null 當成空字串；Tableau 的字串相加遇到 Null 得 Null。"""
        left, right = node.this, node.expression
        either_null = exp.Or(
            this=exp.Is(this=left.copy(), expression=exp.Null()),
            expression=exp.Is(this=right.copy(), expression=exp.Null()),
        )
        return exp.Case(
            ifs=[exp.If(this=either_null, true=exp.Null())],
            default=_mark(exp.DPipe(this=left, expression=right)),
        )

    def trunc(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        formats = {
            "year": "YYYY",
            "quarter": "Q",
            "month": "MM",
            "day": "DD",
            "hour": "HH24",
            "minute": "MI",
        }
        if unit in formats:
            return _fn("TRUNC", node.this, _str(formats[unit]))
        if unit == "second":
            return _cast(node.this, "DATE")
        raise DialectError(f"Oracle 的日期截斷不支援單位 {unit}")

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        def part(field: str, value: exp.Expr) -> exp.Expr:
            if field in ("year", "month", "day"):
                return _extract(field.upper(), value)
            fmt = {"quarter": "Q", "hour": "HH24", "minute": "MI", "second": "SS"}[field]
            return _fn("TO_NUMBER", _fn("TO_CHAR", value, _str(fmt)))

        return self.boundary_diff(
            _unit(node),
            node.expression,
            node.this,
            part,
            lambda start, end: exp.Sub(
                this=_fn("TRUNC", _cast(end, "DATE")),
                expression=_fn("TRUNC", _cast(start, "DATE")),
            ),
        )

    def make_date(self, node: exp.Expr) -> exp.Expr:
        y, m, d = (node.args[k] for k in ("year", "month", "day"))

        def pad(value: exp.Expr, width: int) -> exp.Expr:
            return _fn("LPAD", _fn("TO_CHAR", value), _num(width), _str("0"))

        def concat(left: exp.Expr, right: exp.Expr) -> exp.Expr:
            return _mark(exp.DPipe(this=left, expression=right))

        text = concat(concat(concat(concat(pad(y, 4), _str("-")), pad(m, 2)), _str("-")), pad(d, 2))
        return _fn("TO_DATE", text, _str("YYYY-MM-DD"))

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        day = self.make_date(exp.DateFromParts(year=y, month=m, day=d))
        seconds = exp.Add(
            this=exp.Add(
                this=exp.Mul(this=_paren(hh), expression=_num(3600)),
                expression=exp.Mul(this=_paren(mi), expression=_num(60)),
            ),
            expression=_paren(ss),
        )
        return exp.Paren(
            this=exp.Add(
                this=_cast(day, "TIMESTAMP"),
                expression=_fn("NUMTODSINTERVAL", seconds, _str("SECOND")),
            )
        )

    def epoch(self, node: exp.Expr) -> exp.Expr:
        delta = exp.Paren(
            this=exp.Sub(
                this=_cast(node.this, "TIMESTAMP"),
                expression=exp.Var(this="TIMESTAMP '1970-01-01 00:00:00'"),
            )
        )

        def field(name: str, factor: int) -> exp.Expr:
            return exp.Mul(this=_extract(name, delta.copy()), expression=_num(factor))

        return exp.Paren(
            this=exp.Add(
                this=exp.Add(
                    this=exp.Add(this=field("DAY", 86400), expression=field("HOUR", 3600)),
                    expression=field("MINUTE", 60),
                ),
                expression=_extract("SECOND", delta.copy()),
            )
        )

    def right(self, node: exp.Expr) -> exp.Expr:
        n = node.expression
        return exp.Case(
            ifs=[exp.If(this=exp.LTE(this=n.copy(), expression=_num(0)), true=exp.Null())],
            default=_fn("SUBSTR", node.this, exp.Neg(this=_paren(n))),
        )

    def split_part(self, node: exp.Expr) -> exp.Expr:
        delimiter = node.args.get("delimiter")
        index = _literal_int(node.args.get("part_index"))
        if not (isinstance(delimiter, exp.Literal) and delimiter.is_string):
            raise DialectError("Oracle 的 SPLIT 只支援常值的分隔字元")
        if index is None or index < 1:
            raise DialectError("Oracle 的 SPLIT 只支援正整數常值的位置")
        escaped = re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", delimiter.name)
        pattern = f"(.*?)({escaped}|$)"
        return _fn(
            "REGEXP_SUBSTR", node.this, _str(pattern), _num(1), _num(index), exp.Null(), _num(1)
        )

    def _raw(self, node: exp.Expr) -> str:
        """改寫並驗證過的子運算式 → Oracle SQL（用在 SQLGlot 沒有語法節點的寫法）。"""
        self.validate(node)
        return node.sql(dialect="oracle", identify=True)

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        fmt = _convert_format(_format_literal(args[1]), ORACLE_FORMAT, _double_quote)
        value = self._raw(args[0])
        return exp.Var(
            this=f"TO_TIMESTAMP({value} DEFAULT NULL ON CONVERSION ERROR, {_single_quote(fmt)})"
        )

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        target = self._target(node.args["to"]).sql(dialect="oracle")
        value = self._raw(node.this)
        converted = exp.Var(this=f"CAST({value} AS {target} DEFAULT NULL ON CONVERSION ERROR)")
        if node.args["to"].this == exp.DataType.Type.DATE:
            return _fn("TRUNC", converted)
        return converted

    def cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        if not isinstance(target, exp.DataType):
            return node
        kind = target.this
        if kind == exp.DataType.Type.BOOLEAN:
            raise DialectError("Oracle 沒有布林型別，轉成布林值的計算尚不支援")
        value = node.this
        if (
            isinstance(value, exp.Literal)
            and value.is_string
            and kind in (exp.DataType.Type.DATE, *TIMESTAMP_TYPES)
        ):
            keyword = "DATE" if kind == exp.DataType.Type.DATE else "TIMESTAMP"
            text = value.name if keyword == "TIMESTAMP" else value.name[:10]
            return exp.Var(this=f"{keyword} {_single_quote(text)}")
        if _is_predicate(value) and kind in INTEGER_TYPES:
            # 布林值 → 整數：Oracle 的布林運算式只能出現在條件中
            return exp.Case(
                ifs=[
                    exp.If(this=value, true=_num(1)),
                    exp.If(this=exp.Not(this=_paren(value.copy())), true=_num(0)),
                ]
            )
        converted = exp.Cast(this=value, to=self._target(target))
        if kind == exp.DataType.Type.DATE:
            # Oracle 的 DATE 含時間：截斷到日
            return _fn("TRUNC", converted)
        return converted

    def _target(self, target: exp.Expr) -> exp.DataType:
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.CHAR):
            return _type("VARCHAR2(4000)", "oracle")
        if kind in INTEGER_TYPES:
            return _type("NUMBER(19)", "oracle")
        if kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DECIMAL):
            # DOUBLE PRECISION（FLOAT(126)）：十進位的浮點數，與 IEEE 雙精度的差異在比對容許範圍內
            return _type("DOUBLE")
        if kind == exp.DataType.Type.DATE:
            return target
        if kind in TIMESTAMP_TYPES:
            return _type("TIMESTAMP", "oracle")
        raise DialectError(f"Oracle 不支援轉成 {target.sql('duckdb')} 型別")

    def finish(self, tree: exp.Expr) -> None:
        for select in list(tree.find_all(exp.Select)):
            _expand_group_ordinals(select)
            _qualify_star(select)
            if select.args.get("from_") is None and select.args.get("from") is None:
                # Oracle 23ai 以前的 SELECT 一定要有 FROM
                select.set("from_", exp.From(this=exp.Table(this=exp.to_identifier("DUAL"))))
        for boolean in list(tree.find_all(exp.Boolean)):
            if boolean.root() is tree:
                _replace_boolean(boolean)
        # Oracle 的布林運算式只能出現在條件中（WHERE、ON、CASE WHEN、AND／OR／NOT）
        for node in tree.walk():
            if isinstance(node, PREDICATES) and not _condition_position(node):
                raise DialectError(
                    "Oracle 沒有布林值：查詢結果或運算中的布林值（例如布林維度、ISNULL、CONTAINS 的結果）尚不支援"
                )


INTEGER_TYPES = (
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.INT,
    exp.DataType.Type.SMALLINT,
    exp.DataType.Type.TINYINT,
)
TIMESTAMP_TYPES = (
    exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPNTZ,
    exp.DataType.Type.DATETIME,
)
"""DuckDB 的 TIMESTAMP（不含時區）在 SQLGlot 30 解析成 TIMESTAMPNTZ。"""

ORACLE_FORMAT = {
    "%Y": "YYYY",
    "%y": "YY",
    "%m": "MM",
    "%d": "DD",
    "%H": "HH24",
    "%I": "HH12",
    "%M": "MI",
    "%S": "SS",
    "%p": "AM",
    "%b": "MON",
    "%B": "MONTH",
    "%j": "DDD",
    "%f": "FF6",
}

PREDICATES = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.And,
    exp.Or,
    exp.Not,
    exp.Is,
    exp.In,
    exp.Like,
    exp.ILike,
    exp.Between,
    exp.Exists,
    exp.RegexpLike,
    exp.Boolean,
    exp.NullSafeEQ,
)


def _is_predicate(node: exp.Expr) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    return isinstance(node, PREDICATES)


def _condition_position(node: exp.Expr) -> bool:
    """節點是否位在條件的位置（WHERE、HAVING、ON、CASE WHEN、AND／OR／NOT 的運算元）。"""
    child, parent = node, node.parent
    while isinstance(parent, exp.Paren):
        child, parent = parent, parent.parent
    if isinstance(parent, (exp.Where, exp.Having, exp.And, exp.Or, exp.Not)):
        return True
    if isinstance(parent, exp.Join):
        return child.arg_key == "on"
    if isinstance(parent, exp.If):
        return child.arg_key == "this"
    return False


def _replace_boolean(node: exp.Boolean) -> None:
    """Oracle 沒有 TRUE／FALSE：條件位置換成 1 = 1／1 = 0；與布林運算式比較的改成運算式本身。"""
    value = bool(node.this)
    if _condition_position(node):
        node.replace(_mark(exp.EQ(this=_num(1), expression=_num(1 if value else 0))))
        return
    parent = node.parent
    if isinstance(parent, (exp.EQ, exp.NEQ)) and _condition_position(parent):
        other = parent.expression if parent.this is node else parent.this
        if _is_predicate(other):
            keep = value if isinstance(parent, exp.EQ) else not value
            parent.replace(_paren(other) if keep else exp.Not(this=_paren(other)))
            return
    if (
        isinstance(parent, exp.In)
        and _condition_position(parent)
        and _is_predicate(parent.this)
        and parent.expressions
        and all(isinstance(e, exp.Boolean) for e in parent.expressions)
    ):
        # 布林篩選：x IN (TRUE)、x IN (FALSE)、x IN (TRUE, FALSE)（Null 都不符合）
        values = {bool(e.this) for e in parent.expressions}
        pred = _paren(parent.this)
        if values == {True}:
            parent.replace(pred)
        elif values == {False}:
            parent.replace(exp.Not(this=pred))
        else:
            parent.replace(exp.Paren(this=exp.Or(this=pred, expression=exp.Not(this=pred.copy()))))
        return
    raise DialectError("Oracle 沒有布林值，這個 TRUE／FALSE 的用法尚不支援")


def _expand_group_ordinals(select: exp.Select) -> None:
    """GROUP BY 1 → GROUP BY 第一個輸出欄的運算式（Oracle 不支援以位置分組）。"""
    group = select.args.get("group")
    if group is None:
        return
    items: list[exp.Expr] = []
    for item in group.expressions:
        position = _literal_int(item) if isinstance(item, exp.Literal) else None
        if position is not None:
            projection = select.expressions[position - 1]
            source = projection.this if isinstance(projection, exp.Alias) else projection
            items.append(source.copy())
        else:
            items.append(item)
    group.set("expressions", items)


def _qualify_star(select: exp.Select) -> None:
    """SELECT *, x → SELECT t.*, x（Oracle 的 * 與其他欄位並列時要加資料表名稱）。"""
    stars = [e for e in select.expressions if isinstance(e, exp.Star)]
    if not stars or len(select.expressions) == 1:
        return
    source = select.args.get("from_") or select.args.get("from")
    table = source.this if source is not None else None
    if not isinstance(table, exp.Table) or select.args.get("joins"):
        raise DialectError("Oracle：* 與其他欄位並列且來源不只一張表，尚不支援")
    name = table.alias_or_name
    select.set(
        "expressions",
        [
            exp.Column(this=exp.Star(), table=exp.to_identifier(name))
            if isinstance(e, exp.Star)
            else e
            for e in select.expressions
        ],
    )


# ---------------------------------------------------------------- MySQL


class MySQLRewriter(Rewriter):
    """MySQL 8.0 以上（有實跑測試）。連線時設定工作階段的時區為 UTC、月份與星期名稱為英文（en_US）。

    布林值是 1／0：取回後依輸出欄的型別轉回布林（tmig.data.live）。沒有 MEDIAN、PERCENTILE、CORR、COVAR
    的彙總函式，列為不支援（與 Tableau 連 MySQL 相同）。字串的比較依資料庫的定序（預設不分大小寫）。
    """

    name = "MySQL"
    dialect = "mysql"
    allowed = frozenset(
        {
            exp.Year,
            exp.Quarter,
            exp.Month,
            exp.Day,
            exp.Hour,
            exp.Minute,
            exp.Second,
            exp.DayOfYear,
            exp.Monthname,
            exp.Dayname,
            exp.Left,
            exp.Right,
            exp.Repeat,
            exp.Unicode,
            exp.DPipe,
            exp.Pi,
            exp.Degrees,
            exp.Radians,
            exp.Variance,
            exp.VariancePop,
            exp.Mod,
            exp.IntDiv,
            exp.NullSafeEQ,
            exp.CurrentDate,
            exp.Boolean,
            exp.Log,
        }
    )

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        interval = exp.Interval(this=_paren(amount), unit=exp.Var(this=unit.upper()))
        return exp.Paren(this=exp.Add(this=_paren(value), expression=interval))

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        return {
            exp.DayOfWeek: lambda n: exp.Paren(
                this=exp.Sub(this=_fn("DAYOFWEEK", n.this), expression=_num(1))
            ),
            exp.DayOfWeekIso: lambda n: exp.Paren(
                this=exp.Add(this=_fn("WEEKDAY", n.this), expression=_num(1))
            ),
            # 模式 3：ISO 週（星期一開始、第一週含 1 月 4 日）
            exp.Week: lambda n: _fn("WEEK", n.this, _num(3)),
            # MySQL 的 ROUND(DOUBLE) 依 C 函式庫（多半是四捨六入五成雙）；轉成 DECIMAL 才是四捨五入
            exp.Round: lambda n: _cast(
                _fn(
                    "ROUND",
                    _cast(n.this, _type("DECIMAL(65, 30)", "mysql")),
                    n.args.get("decimals") or _num(0),
                ),
                "DOUBLE",
            ),
            exp.TimestampTrunc: self.trunc,
            exp.DateTrunc: self.trunc,
            exp.DateDiff: lambda n: self.boundary_diff(
                _unit(n),
                n.expression,
                n.this,
                lambda field, value: _extract(field.upper(), value),
                lambda start, end: _fn("DATEDIFF", end, start),
            ),
            exp.DateFromParts: self.make_date,
            exp.TimestampFromParts: self.make_timestamp,
            exp.TimeToUnix: lambda n: exp.Div(
                this=_fn(
                    "TIMESTAMPDIFF",
                    exp.Var(this="MICROSECOND"),
                    _str("1970-01-01 00:00:00"),
                    n.this,
                ),
                expression=_num(1000000.0),
            ),
            exp.TryCast: self.try_cast,
            exp.Cast: self.cast,
            exp.Chr: lambda n: _mark(
                exp.Var(
                    this=f"CONVERT(CHAR({self._raw(n.expressions[0])} USING utf32) USING utf8mb4)"
                )
            ),
            exp.SplitPart: self.split_part,
            exp.RegexpLike: lambda n: _fn("REGEXP_LIKE", n.this, n.expression, _str("c")),
            exp.RegexpExtract: self.regexp_extract,
            exp.RegexpReplace: self.regexp_replace,
            exp.Corr: self.no_aggregate,
            exp.CovarSamp: self.no_aggregate,
            exp.CovarPop: self.no_aggregate,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: exp.IntDiv(
                this=_fn("YEARWEEK", a[0], _num(3)), expression=_num(100)
            ),
            "now": lambda a: _fn("NOW", _num(6)),
            "try_strptime": self.parse_date,
        }

    def no_aggregate(self, node: exp.Expr) -> exp.Expr:
        raise DialectError(f"MySQL 沒有 {_describe(node).removeprefix('函式 ')} 彙總函式")

    def _raw(self, node: exp.Expr) -> str:
        self.validate(node)
        return node.sql(dialect="mysql", identify=True)

    def trunc(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        value = node.this
        formats = {
            "year": "%Y-01-01",
            "month": "%Y-%m-01",
            "day": "%Y-%m-%d",
            "hour": "%Y-%m-%d %H:00:00",
            "minute": "%Y-%m-%d %H:%i:00",
            "second": "%Y-%m-%d %H:%i:%s",
        }
        if unit == "quarter":
            # 該季第一個月的 1 日：年初加上（季 - 1）× 3 個月
            year_start = _fn("MAKEDATE", _fn("YEAR", value.copy()), _num(1))
            months = exp.Mul(
                this=_paren(exp.Sub(this=_fn("QUARTER", value), expression=_num(1))),
                expression=_num(3),
            )
            return _cast(
                self.add_interval(year_start, "month", months), _type("DATETIME(6)", "mysql")
            )
        if unit not in formats:
            raise DialectError(f"MySQL 的日期截斷不支援單位 {unit}")
        # 與 DuckDB 相同一律是時間戳記（需要日期時，產生的 SQL 另外轉型）
        text = _fn("DATE_FORMAT", value, _str(formats[unit]))
        return _cast(text, _type("DATETIME(6)", "mysql"))

    def make_date(self, node: exp.Expr) -> exp.Expr:
        """年初加上（月 - 1）個月、再加上（日 - 1）天（MySQL 的 MAKEDATE 是年與一年中的第幾天）。"""
        y, m, d = (node.args[k] for k in ("year", "month", "day"))
        start = _fn("MAKEDATE", y, _num(1))
        months = self.add_interval(start, "month", exp.Sub(this=_paren(m), expression=_num(1)))
        return self.add_interval(months, "day", exp.Sub(this=_paren(d), expression=_num(1)))

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        day = _cast(
            self.make_date(exp.DateFromParts(year=y, month=m, day=d)),
            _type("DATETIME(6)", "mysql"),
        )
        micros = _fn(
            "ROUND",
            exp.Mul(
                this=_paren(
                    exp.Add(
                        this=exp.Add(
                            this=exp.Mul(this=_paren(hh), expression=_num(3600)),
                            expression=exp.Mul(this=_paren(mi), expression=_num(60)),
                        ),
                        expression=_paren(ss),
                    )
                ),
                expression=_num(1000000),
            ),
        )
        return self.add_interval(day, "microsecond", micros)

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        """MySQL 沒有 TRY_CAST：字串轉成數字時不合格式的得 0，先以正規表示式檢查；
        轉成日期、時間時不合格式的本來就是 Null。"""
        target = node.args["to"]
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, *INTEGER_TYPES):
            text = node.this
            number = _str(
                r"^[[:space:]]*[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][-+]?[0-9]+)?[[:space:]]*$"
            )
            valid = _mark(exp.RegexpLike(this=text.copy(), expression=number))
            return exp.Case(
                ifs=[exp.If(this=valid, true=self.cast(exp.Cast(this=text, to=target)))]
            )
        return self.cast(exp.Cast(this=node.this, to=target))

    def cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        if not isinstance(target, exp.DataType):
            return node
        kind = target.this
        value = node.this
        if kind == exp.DataType.Type.BOOLEAN:
            # 數字或布林值 → 布林：不是 0 為真（Null 仍是 Null）
            return exp.Paren(this=exp.NEQ(this=_paren(value), expression=_num(0)))
        if kind in INTEGER_TYPES:
            return _mark(exp.Cast(this=value, to=_type("SIGNED", "mysql")))
        if kind in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.CHAR):
            return _mark(exp.Cast(this=value, to=_type("CHAR", "mysql")))
        if kind in TIMESTAMP_TYPES:
            return _mark(exp.Cast(this=value, to=_type("DATETIME(6)", "mysql")))
        if kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DATE):
            return node
        if kind == exp.DataType.Type.DECIMAL:
            return node
        raise DialectError(f"MySQL 不支援轉成 {target.sql('duckdb')} 型別")

    def split_part(self, node: exp.Expr) -> exp.Expr:
        """SUBSTRING_INDEX 取第 n 段；超過段數時為空字串（與 DuckDB 的 split_part 相同）。"""
        text, delimiter = node.this, node.args["delimiter"]
        index = node.args["part_index"]
        count = exp.Add(
            this=exp.Div(
                this=_paren(
                    exp.Sub(
                        this=_fn("CHAR_LENGTH", text.copy()),
                        expression=_fn(
                            "CHAR_LENGTH", _fn("REPLACE", text.copy(), delimiter.copy(), _str(""))
                        ),
                    )
                ),
                expression=_fn("CHAR_LENGTH", delimiter.copy()),
            ),
            expression=_num(1),
        )
        n = _paren(index)
        inner = _fn("SUBSTRING_INDEX", text, delimiter.copy(), n.copy())
        positive = _fn("SUBSTRING_INDEX", inner.copy(), delimiter.copy(), _num(-1))
        negative = _fn("SUBSTRING_INDEX", inner, delimiter.copy(), _num(1))
        out_of_range = exp.Or(
            this=exp.EQ(this=n.copy(), expression=_num(0)),
            expression=exp.GT(this=_fn("ABS", n.copy()), expression=_paren(count)),
        )
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.Or(
                        this=exp.Is(this=text.copy(), expression=exp.Null()),
                        expression=exp.Is(this=n.copy(), expression=exp.Null()),
                    ),
                    true=exp.Null(),
                ),
                exp.If(this=out_of_range, true=_str("")),
                exp.If(this=exp.GT(this=n.copy(), expression=_num(0)), true=positive),
            ],
            default=negative,
        )

    def regexp_extract(self, node: exp.Expr) -> exp.Expr:
        """MySQL 的 REGEXP_SUBSTR 只取整段符合的文字：取群組時，把符合的文字再以樣式換成該群組。"""
        group = _literal_int(node.args.get("group"))
        if group is None:
            raise DialectError("REGEXP_EXTRACT 的群組不是常值")
        pattern = node.expression
        matched = _fn("REGEXP_SUBSTR", node.this, pattern.copy(), _num(1), _num(1), _str("c"))
        if group == 0:
            return matched
        return _fn(
            "REGEXP_REPLACE", matched, pattern, _str(f"${group}"), _num(1), _num(0), _str("c")
        )

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        replacement = node.args.get("replacement") or _str("")
        if not (isinstance(replacement, exp.Literal) and replacement.is_string):
            raise DialectError("REGEXP_REPLACE 的取代字串不是常值")
        # ICU 的取代字串與 Java 相同：$1 為群組、\$ 為錢字號
        text = _str(java_replacement(replacement.name))
        return _fn("REGEXP_REPLACE", node.this, node.expression, text, _num(1), _num(0), _str("c"))

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：STR_TO_DATE 不符合格式時為 Null。"""
        fmt = _convert_format(
            _format_literal(args[1]), MYSQL_FORMAT, lambda t: t.replace("%", "%%")
        )
        return _cast(_fn("STR_TO_DATE", args[0], _str(fmt)), _type("DATETIME(6)", "mysql"))


MYSQL_FORMAT = {
    "%Y": "%Y",
    "%y": "%y",
    "%m": "%m",
    "%d": "%d",
    "%H": "%H",
    "%I": "%h",
    "%M": "%i",
    "%S": "%s",
    "%p": "%p",
    "%b": "%b",
    "%B": "%M",
    "%j": "%j",
    "%f": "%f",
}
"""strftime 的格式 → MySQL 的 STR_TO_DATE 格式（分鐘是 %i、月份名稱是 %M）。"""


# ---------------------------------------------------------------- Snowflake


MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _names(number: exp.Expr, names: tuple[str, ...]) -> exp.Expr:
    """1 起算的編號 → 英文名稱（DuckDB 的 monthname、dayname 是完整的英文名稱）。"""
    return exp.Case(
        this=number,
        ifs=[exp.If(this=_num(i), true=_str(name)) for i, name in enumerate(names, start=1)],
    )


class SnowflakeRewriter(Rewriter):
    """Snowflake：依官方文件撰寫，尚未實機驗證（測試以 SQLGlot 轉回 DuckDB 模擬執行）。

    連線時把工作階段的時區（TIMEZONE）設為 UTC。識別字一律加引號，與 .twb 中的名稱相同（區分大小寫）。
    星期與週數一律以 ISO 的函式計算（DAYOFWEEKISO、WEEKISO），不受 WEEK_START 等工作階段參數影響。
    """

    name = "Snowflake"
    dialect = "snowflake"
    allowed = frozenset(
        {
            exp.Year,
            exp.Quarter,
            exp.Month,
            exp.Day,
            exp.Hour,
            exp.Minute,
            exp.Second,
            exp.DayOfYear,
            exp.DayOfWeekIso,
            exp.TimestampTrunc,
            exp.DateTrunc,
            exp.DateDiff,
            exp.DateFromParts,
            exp.Left,
            exp.Right,
            exp.Repeat,
            exp.Unicode,
            exp.DPipe,
            exp.SplitPart,
            exp.Split,
            exp.ArrayToString,
            exp.Pi,
            exp.Degrees,
            exp.Radians,
            exp.Median,
            exp.PercentileCont,
            exp.Variance,
            exp.VariancePop,
            exp.Mod,
            exp.IntDiv,
            exp.NullSafeEQ,
            exp.CurrentDate,
            exp.Boolean,
            exp.Log,
        }
    )

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        return _fn("DATEADD", exp.Var(this=unit.upper()), amount, value)

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        return {
            exp.DayOfWeek: lambda n: _fn("MOD", _fn("DAYOFWEEKISO", n.this), _num(7)),
            exp.Week: lambda n: _fn("WEEKISO", n.this),
            exp.Round: self.round,
            exp.TimestampFromParts: self.make_timestamp,
            exp.Monthname: lambda n: _names(_fn("MONTH", n.this), MONTH_NAMES),
            exp.Dayname: lambda n: _names(_fn("DAYOFWEEKISO", n.this), DAY_NAMES),
            exp.TimeToUnix: lambda n: exp.Div(
                this=_fn("DATE_PART", exp.Var(this="EPOCH_MICROSECOND"), n.this),
                expression=_num(1000000.0),
            ),
            # Snowflake 的 TRY_CAST 只接受字串
            exp.TryCast: lambda n: _mark(
                exp.TryCast(this=_fn("TO_VARCHAR", n.this), to=n.args["to"], requires_string=True)
            ),
            exp.RegexpLike: self.regexp_like,
            exp.RegexpExtract: self.regexp_extract,
            exp.RegexpReplace: lambda n: _fn(
                "REGEXP_REPLACE",
                n.this,
                n.expression,
                n.args.get("replacement") or _str(""),
                _num(1),
                _num(0),
                _str("c"),
            ),
            exp.Length: lambda n: _fn("ARRAY_SIZE", n.this) if _is_array(n.this) else n,
            exp.Split: lambda n: _array(n),
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: _fn("YEAROFWEEKISO", a[0]),
            "now": lambda a: _cast(_fn("CURRENT_TIMESTAMP"), _type("TIMESTAMP_NTZ", "snowflake")),
            "try_strptime": self.parse_date,
            "list_slice": self.list_slice,
        }

    def round(self, node: exp.Expr) -> exp.Expr:
        """小數位數以常值寫出（CAST(1 AS INTEGER) → 1）。"""
        digits = node.args.get("decimals")
        fixed = _literal_int(digits) if digits is not None else 0
        if digits is not None and fixed is None:
            raise DialectError("Snowflake 的 ROUND 小數位數必須是常值")
        return _fn("ROUND", node.this, _num(fixed or 0))

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        whole = _fn("FLOOR", ss.copy())
        nanos = _fn(
            "ROUND",
            exp.Mul(
                this=_paren(exp.Sub(this=_paren(ss), expression=whole.copy())),
                expression=_num(1000000000),
            ),
        )
        return _fn("TIMESTAMP_NTZ_FROM_PARTS", y, m, d, hh, mi, whole, nanos)

    def regexp_like(self, node: exp.Expr) -> exp.Expr:
        """部分符合（DuckDB 的 regexp_matches）：REGEXP_INSTR > 0（Snowflake 的 REGEXP_LIKE 要整段符合）。
        Snowflake 不支援樣式中的 (?i)：開頭的 (?i) 改成不分大小寫的參數。"""
        pattern = node.expression
        flags = "c"
        if (
            isinstance(pattern, exp.Literal)
            and pattern.is_string
            and pattern.name.startswith("(?i)")
        ):
            pattern, flags = _str(pattern.name[4:]), "i"
        position = _fn("REGEXP_INSTR", node.this, pattern, _num(1), _num(1), _num(0), _str(flags))
        return exp.Paren(this=exp.GT(this=position, expression=_num(0)))

    def regexp_extract(self, node: exp.Expr) -> exp.Expr:
        group = _literal_int(node.args.get("group"))
        if group is None:
            raise DialectError("REGEXP_EXTRACT 的群組不是常值")
        args = [node.this, node.expression, _num(1), _num(1), _str("c" if group == 0 else "ce")]
        if group > 0:
            args.append(_num(group))
        return _fn("REGEXP_SUBSTR", *args)

    def list_slice(self, args: list[exp.Expr]) -> exp.Expr:
        """DuckDB 的 list_slice（1 起算、含結尾）→ ARRAY_SLICE（0 起算、不含結尾）。"""
        array, start, end = args
        if not _is_array(array):
            raise DialectError("list_slice 只支援 FINDNTH 的寫法")
        begin = exp.Sub(this=_paren(start), expression=_num(1))
        return _array(_fn("ARRAY_SLICE", array, begin, end))

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：TRY_TO_TIMESTAMP_NTZ 不符合格式時為 Null。"""
        fmt = _convert_format(_format_literal(args[1]), SNOWFLAKE_FORMAT, _double_quote)
        return _fn("TRY_TO_TIMESTAMP_NTZ", args[0], _str(fmt))


SNOWFLAKE_FORMAT = {
    "%Y": "YYYY",
    "%y": "YY",
    "%m": "MM",
    "%d": "DD",
    "%H": "HH24",
    "%I": "HH12",
    "%M": "MI",
    "%S": "SS",
    "%p": "AM",
    "%b": "MON",
    "%B": "MMMM",
    "%f": "FF6",
}
"""strftime 的格式 → Snowflake 的日期格式（文字以雙引號包住）。"""


# ---------------------------------------------------------------- SQL Server


TSQL_EPOCH = "CAST('1970-01-01' AS DATETIME2)"
TSQL_SUNDAY = "CAST('1900-01-07' AS DATE)"
"""1900-01-07 是星期日：與它相隔的天數除以 7 的餘數就是星期（不受 @@DATEFIRST 影響）。"""
TSQL_MONDAY = "CAST('1900-01-01' AS DATE)"
SPLIT_LIMIT = 10
"""SQL Server 的 SPLIT 以巢狀的 CHARINDEX 取第 n 段：位置是常值且不超過這個數。"""


class TSqlRewriter(Rewriter):
    """SQL Server 2017 以上與 Azure SQL Database：依官方文件撰寫，尚未實機驗證（測試以 SQLGlot 轉回
    DuckDB 模擬執行）。

    連線時設定 SET LANGUAGE us_english（英文的月份與星期名稱）；星期以與某個星期日相隔的天數計算，
    不受 @@DATEFIRST 影響。SQL Server 沒有布林型別：查詢結果或運算中的布林值（例如布林維度）列為不支援。
    非英文字元的字串常值以 N'…' 表示（nvarchar），轉成字串一律是 NVARCHAR(MAX)，中文不會因資料庫的定序
    變成問號。沒有正規表示式、DATEPARSE、MEDIAN／PERCENTILE 的彙總與 CORR／COVAR，列為不支援
    （與 Tableau 連 SQL Server 相同）。字串的比較依資料庫的定序（預設不分大小寫）。
    """

    name = "SQL Server"
    dialect = "tsql"
    allowed = frozenset(
        {
            exp.Year,
            exp.Month,
            exp.Day,
            exp.Left,
            exp.Right,
            exp.Repeat,
            exp.Unicode,
            exp.Pi,
            exp.Mod,
            exp.IntDiv,
            exp.Log,
        }
    )

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        # DATEADD 的數量是 INT：微秒拆成天、秒與微秒（一天的微秒數超過 INT 的範圍）
        if unit == "microsecond":
            total = _paren(amount)
            days = exp.IntDiv(this=total.copy(), expression=_num(86400000000))
            seconds = exp.Mod(
                this=_paren(exp.IntDiv(this=total.copy(), expression=_num(1000000))),
                expression=_num(86400),
            )
            micros = exp.Mod(this=total, expression=_num(1000000))
            shifted = self._dateadd("day", days, _cast(value, "DATETIME2", "tsql"))
            return self._dateadd("microsecond", micros, self._dateadd("second", seconds, shifted))
        if unit in ("hour", "minute", "second"):
            # DATE 不能加時、分、秒：先轉成 DATETIME2（DuckDB 的結果也是時間戳記）
            value = _cast(value, "DATETIME2", "tsql")
        return self._dateadd(unit, amount, value)

    def _dateadd(self, unit: str, amount: exp.Expr, value: exp.Expr) -> exp.Expr:
        return _fn("DATEADD", exp.Var(this=unit.upper()), amount, value)

    def _part(self, unit: str, value: exp.Expr) -> exp.Expr:
        return _fn("DATEPART", exp.Var(this=unit.upper()), value)

    def _days_since(self, anchor: str, value: exp.Expr) -> exp.Expr:
        return _fn("DATEDIFF", exp.Var(this="DAY"), exp.Var(this=anchor), _cast(value, "DATE"))

    def _weekday(self, anchor: str, value: exp.Expr) -> exp.Expr:
        """與 anchor（星期日或星期一）相隔的天數除以 7 的餘數（0–6）；早於 anchor 時也是 0–6。"""
        return exp.Paren(
            this=exp.Mod(
                this=_paren(
                    exp.Add(
                        this=exp.Mod(this=self._days_since(anchor, value), expression=_num(7)),
                        expression=_num(7),
                    )
                ),
                expression=_num(7),
            )
        )

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def part(unit: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: self._part(unit, n.this)

        def floated(name: str) -> Callable[[exp.Expr], exp.Expr]:
            # SQL Server 的 DEGREES、RADIANS 結果與參數同型別（整數參數得整數）
            return lambda n: _fn(name, _cast(n.this, "DOUBLE"))

        return {
            exp.Quarter: part("quarter"),
            exp.Hour: part("hour"),
            exp.Minute: part("minute"),
            exp.Second: part("second"),
            exp.DayOfYear: part("dayofyear"),
            exp.Week: part("iso_week"),
            exp.DayOfWeek: lambda n: self._weekday(TSQL_SUNDAY, n.this),
            exp.DayOfWeekIso: lambda n: exp.Paren(
                this=exp.Add(this=self._weekday(TSQL_MONDAY, n.this), expression=_num(1))
            ),
            exp.TimestampTrunc: self.trunc,
            exp.DateTrunc: self.trunc,
            exp.DateDiff: lambda n: _fn(
                "DATEDIFF_BIG", exp.Var(this=_unit(n).upper()), n.expression, n.this
            ),
            exp.DateFromParts: lambda n: _fn(
                "DATEFROMPARTS", *(n.args[k] for k in ("year", "month", "day"))
            ),
            exp.TimestampFromParts: self.make_timestamp,
            exp.Monthname: lambda n: _fn("DATENAME", exp.Var(this="MONTH"), n.this),
            exp.Dayname: lambda n: _fn("DATENAME", exp.Var(this="WEEKDAY"), n.this),
            exp.TimeToUnix: lambda n: exp.Div(
                this=_cast(
                    _fn(
                        "DATEDIFF_BIG",
                        exp.Var(this="MICROSECOND"),
                        exp.Var(this=TSQL_EPOCH),
                        _cast(n.this, "DATETIME2", "tsql"),
                    ),
                    "DOUBLE",
                ),
                expression=_num(1000000.0),
            ),
            exp.CurrentDate: lambda n: _cast(_fn("GETDATE"), "DATE"),
            exp.TryCast: self.try_cast,
            exp.Cast: self.cast,
            exp.DPipe: self.concat,
            exp.Length: self.length,
            exp.Substring: self.substring,
            exp.SplitPart: self.split_part,
            exp.Chr: lambda n: _fn("NCHAR", n.expressions[0]),
            exp.Pow: lambda n: _fn("POWER", _cast(n.this, "DOUBLE"), n.expression),
            exp.Degrees: floated("DEGREES"),
            exp.Radians: floated("RADIANS"),
            exp.Avg: lambda n: _mark(exp.Avg(this=_cast(n.this, "DOUBLE"))),
            # SQL Server 的 ROUND 一定要有小數位數
            exp.Round: lambda n: _fn("ROUND", n.this, n.args.get("decimals") or _num(0)),
            exp.Variance: lambda n: _fn("VAR", n.this),
            exp.VariancePop: lambda n: _fn("VARP", n.this),
            exp.StddevSamp: lambda n: _fn("STDEV", n.this),
            exp.StddevPop: lambda n: _fn("STDEVP", n.this),
            exp.Corr: self.unsupported("CORR"),
            exp.CovarSamp: self.unsupported("COVAR"),
            exp.CovarPop: self.unsupported("COVARP"),
            exp.RegexpLike: self.unsupported("正規表示式"),
            exp.RegexpExtract: self.unsupported("正規表示式"),
            exp.RegexpReplace: self.unsupported("正規表示式"),
            exp.NullSafeEQ: lambda n: exp.Paren(
                this=exp.Or(
                    this=exp.EQ(this=n.this.copy(), expression=n.expression.copy()),
                    expression=exp.And(
                        this=exp.Is(this=n.this, expression=exp.Null()),
                        expression=exp.Is(this=n.expression, expression=exp.Null()),
                    ),
                )
            ),
            exp.Literal: self.literal,
            exp.Window: self.window,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            # ISO 週的年：該週星期四所在的年
            "isoyear": lambda a: _fn(
                "YEAR",
                self._dateadd(
                    "day",
                    exp.Sub(this=_num(3), expression=self._weekday(TSQL_MONDAY, a[0].copy())),
                    _cast(a[0], "DATE"),
                ),
            ),
            "now": lambda a: _cast(_fn("GETDATE"), "DATETIME2", "tsql"),
        }

    @staticmethod
    def unsupported(what: str) -> Callable[[exp.Expr], exp.Expr]:
        def raise_(node: exp.Expr) -> exp.Expr:
            raise DialectError(f"SQL Server 沒有 {what}")

        return raise_

    def window(self, node: exp.Expr) -> exp.Expr:
        """視窗的排序不能是常值：ORDER BY NULL 改成 ORDER BY (SELECT NULL)。"""
        order = node.args.get("order")
        if isinstance(order, exp.Order):
            for item in order.expressions:
                if isinstance(item, exp.Ordered) and isinstance(item.this, exp.Null):
                    item.set("this", _mark(exp.Var(this="(SELECT NULL)")))
                    # 與 SQL Server 預設的 Null 順序相同，不另外產生排序 Null 的運算式
                    item.set("nulls_first", True)
        return node

    def literal(self, node: exp.Expr) -> exp.Expr:
        """非 ASCII 的字串常值寫成 N'…'（nvarchar）。"""
        assert isinstance(node, exp.Literal)
        if node.is_string and not node.name.isascii():
            return _mark(exp.National(this=node.name))
        return node

    def trunc(self, node: exp.Expr) -> exp.Expr:
        """與 DuckDB 相同一律是時間戳記（需要日期時，產生的 SQL 另外轉型）。"""
        unit = _unit(node)
        if unit in ("year", "quarter", "month", "day"):
            return _cast(self._trunc_date(unit, node.this), "DATETIME2", "tsql")
        return self._trunc_time(unit, node.this)

    def _trunc_date(self, unit: str, value: exp.Expr) -> exp.Expr:
        if unit == "year":
            return _fn("DATEFROMPARTS", _fn("YEAR", value), _num(1), _num(1))
        if unit == "quarter":
            month = exp.Add(
                this=exp.Mul(
                    this=_paren(
                        exp.Sub(this=self._part("quarter", value.copy()), expression=_num(1))
                    ),
                    expression=_num(3),
                ),
                expression=_num(1),
            )
            return _fn("DATEFROMPARTS", _fn("YEAR", value), month, _num(1))
        if unit == "month":
            return _fn("DATEFROMPARTS", _fn("YEAR", value.copy()), _fn("MONTH", value), _num(1))
        return _cast(value, "DATE")

    def _trunc_time(self, unit: str, value: exp.Expr) -> exp.Expr:
        seconds = {"hour": 3600, "minute": 60, "second": 1}
        if unit not in seconds:
            raise DialectError(f"SQL Server 的日期截斷不支援單位 {unit}")
        # 當天零時加上整點（分、秒）：DATEDIFF 的秒數在一天之內不會超過 INT
        midnight = _cast(_cast(value.copy(), "DATE"), "DATETIME2", "tsql")
        elapsed = _fn("DATEDIFF", exp.Var(this="SECOND"), midnight.copy(), value)
        whole = exp.Mul(
            this=_paren(exp.IntDiv(this=elapsed, expression=_num(seconds[unit]))),
            expression=_num(seconds[unit]),
        )
        return self._dateadd("second", whole, midnight)

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        whole = _fn("FLOOR", ss.copy())
        micros = _fn(
            "ROUND",
            exp.Mul(
                this=_paren(exp.Sub(this=_paren(ss), expression=whole.copy())),
                expression=_num(1000000),
            ),
            _num(0),
        )
        return _fn("DATETIME2FROMPARTS", y, m, d, hh, mi, whole, micros, _num(6))

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        """空字串（或只有空白）轉成數字或日期時，SQL Server 的 TRY_CAST 得 0 或 1900-01-01；改為 Null。"""
        target = self._target(node.args["to"])
        trimmed = _fn("NULLIF", _fn("LTRIM", _fn("RTRIM", node.this)), _str(""))
        return _mark(exp.TryCast(this=trimmed, to=target))

    def cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        if not isinstance(target, exp.DataType):
            return node
        kind = target.this
        value = node.this
        if kind == exp.DataType.Type.BOOLEAN:
            raise DialectError("SQL Server 沒有布林型別，轉成布林值的計算尚不支援")
        if _is_predicate(value) and kind in INTEGER_TYPES:
            return exp.Case(
                ifs=[
                    exp.If(this=value, true=_num(1)),
                    exp.If(this=exp.Not(this=_paren(value.copy())), true=_num(0)),
                ]
            )
        if kind == exp.DataType.Type.BIGINT and isinstance(value, exp.Sum):
            # 整數的 SUM 在 SQL Server 是 INT，超過範圍會溢位：先轉成 BIGINT 再加總
            return _mark(exp.Sum(this=_mark(exp.Cast(this=value.this, to=_type("BIGINT")))))
        return _mark(exp.Cast(this=value, to=self._target(target)))

    def _target(self, target: exp.Expr) -> exp.DataType:
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.CHAR):
            return _type("NVARCHAR(MAX)", "tsql")
        if kind in INTEGER_TYPES:
            return _type("BIGINT") if kind == exp.DataType.Type.BIGINT else _type("INT")
        if kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT):
            return _type("FLOAT(53)", "tsql")
        if kind == exp.DataType.Type.DECIMAL:
            return target
        if kind == exp.DataType.Type.DATE:
            return target
        if kind in TIMESTAMP_TYPES:
            return _type("DATETIME2", "tsql")
        raise DialectError(f"SQL Server 不支援轉成 {target.sql('duckdb')} 型別")

    def concat(self, node: exp.Expr) -> exp.Expr:
        """字串相加：遇到 Null 得 Null（與 Tableau 相同；CONCAT 把 Null 當成空字串）。
        連續相加併成一個 CONCAT（每個運算元只出現兩次，不會越接越長）。"""
        operands: list[exp.Expr] = []
        for side in (node.this, node.expression):
            inner = side
            while isinstance(inner, exp.Paren):
                inner = inner.this
            operands += [o.copy() for o in inner.meta.get("concat", [])] or [side]
        any_null: exp.Expr = exp.Is(this=operands[0].copy(), expression=exp.Null())
        for operand in operands[1:]:
            any_null = exp.Or(
                this=any_null, expression=exp.Is(this=operand.copy(), expression=exp.Null())
            )
        case = exp.Case(
            ifs=[exp.If(this=any_null, true=exp.Null())],
            default=_fn("CONCAT", *operands),
        )
        case.meta["concat"] = [o.copy() for o in operands]
        return case

    def length(self, node: exp.Expr) -> exp.Expr:
        """LEN 不計結尾的空白：加上一個字元再減一。"""
        if _is_array(node.this):
            raise DialectError("SQL Server 沒有陣列（FINDNTH 等以陣列計算的函式）")
        padded = self.concat(exp.DPipe(this=node.this, expression=_str(".")))
        return exp.Paren(this=exp.Sub(this=_fn("LEN", padded), expression=_num(1)))

    def substring(self, node: exp.Expr) -> exp.Expr:
        """SQL Server 的 SUBSTRING 一定要有長度。"""
        length = node.args.get("length") or _num(2147483647)
        return _fn("SUBSTRING", node.this, node.args["start"], length)

    def split_part(self, node: exp.Expr) -> exp.Expr:
        """第 n 段：逐段以 CHARINDEX 找分隔字元（位置須為常值）；倒數第 n 段把字串反轉後取第 n 段。"""
        text, delimiter = node.this, node.args["delimiter"]
        index = _literal_int(node.args.get("part_index"))
        if index is None or index == 0 or abs(index) > SPLIT_LIMIT:
            raise DialectError(f"SQL Server 的 SPLIT 只支援 ±1 到 ±{SPLIT_LIMIT} 的常值位置")
        if not (isinstance(delimiter, exp.Literal) and delimiter.is_string and delimiter.name):
            raise DialectError("SQL Server 的 SPLIT 只支援常值的分隔字元")
        if index < 0:
            reversed_text = _fn("REVERSE", text)
            reversed_delimiter = _str(delimiter.name[::-1])
            return _fn("REVERSE", self._nth(reversed_text, reversed_delimiter, -index))
        return self._nth(text, delimiter, index)

    def _nth(self, text: exp.Expr, delimiter: exp.Expr, index: int) -> exp.Expr:
        width = _num(len(delimiter.name))
        rest: exp.Expr = text
        for _ in range(index - 1):
            position = _fn("CHARINDEX", delimiter.copy(), rest.copy())
            rest = exp.Case(
                ifs=[
                    exp.If(
                        this=exp.GT(this=position, expression=_num(0)),
                        true=_fn(
                            "SUBSTRING",
                            rest.copy(),
                            exp.Add(this=position.copy(), expression=width.copy()),
                            _num(2147483647),
                        ),
                    )
                ]
            )
        end = _fn(
            "CHARINDEX",
            delimiter.copy(),
            _fn("CONCAT", rest.copy(), delimiter.copy()),
        )
        part = _fn("LEFT", rest.copy(), exp.Sub(this=end, expression=_num(1)))
        # 原字串是 Null 得 Null；段數不夠得空字串
        return exp.Case(
            ifs=[
                exp.If(this=exp.Is(this=text.copy(), expression=exp.Null()), true=exp.Null()),
                exp.If(this=exp.Is(this=rest.copy(), expression=exp.Null()), true=_str("")),
            ],
            default=part,
        )

    def finish(self, tree: exp.Expr) -> None:
        for select in list(tree.find_all(exp.Select)):
            _expand_group_ordinals(select)
        _bit_booleans(tree)


def _tsql_condition(node: exp.Expr) -> bool:
    """節點是否位在條件的位置；簡單 CASE（CASE x WHEN 值 …）的 WHEN 是比較的值，不是條件。"""
    child, parent = node, node.parent
    while isinstance(parent, exp.Paren):
        child, parent = parent, parent.parent
    if isinstance(parent, exp.If) and child.arg_key == "this":
        case = parent.parent
        return not (isinstance(case, exp.Case) and case.args.get("this") is not None)
    return _condition_position(node)


def _bit(predicate: exp.Expr) -> exp.Expr:
    """值的位置的條件 → BIT：真為 1、假為 0、不明為 Null。"""
    case = exp.Case(
        ifs=[
            exp.If(this=predicate.copy(), true=_num(1)),
            exp.If(this=exp.Not(this=_paren(predicate.copy())), true=_num(0)),
        ]
    )
    return _mark(exp.Cast(this=case, to=_type("BIT", "tsql")))


def _bit_booleans(tree: exp.Expr) -> None:
    """SQL Server 沒有布林型別：布林值以 BIT 表示。值的位置的條件轉成 BIT，條件位置的 BIT 值與 1 比較，
    TRUE／FALSE 在條件位置寫成 1 = 1／1 = 0、在值的位置寫成 BIT 常值。"""
    for node in list(tree.walk()):
        if (
            isinstance(node, PREDICATES)
            and not isinstance(node, exp.Boolean)
            and node.root() is tree
            and node.parent is not None
            and not _tsql_condition(node)
        ):
            node.replace(_bit(node))
    for node in list(tree.walk()):
        if isinstance(node, (exp.Paren, exp.Boolean)) or node.root() is not tree:
            continue
        if node.parent is not None and _tsql_condition(node) and not isinstance(node, PREDICATES):
            node.replace(_mark(exp.EQ(this=_paren(node.copy()), expression=_num(1))))
    for node in list(tree.find_all(exp.Boolean)):
        if node.root() is not tree:
            continue
        value = 1 if node.this else 0
        if _tsql_condition(node):
            node.replace(_mark(exp.EQ(this=_num(1), expression=_num(value))))
        else:
            node.replace(_mark(exp.Cast(this=_num(value), to=_type("BIT", "tsql"))))


REWRITERS: dict[str, Rewriter] = {
    "postgres": PostgresRewriter(),
    "databricks": SparkRewriter("Databricks", "databricks"),
    "spark": SparkRewriter(),
    "oracle": OracleRewriter(),
    "mysql": MySQLRewriter(),
    "snowflake": SnowflakeRewriter(),
    "tsql": TSqlRewriter(),
}


__all__ = [
    "SUPPORTED",
    "DialectError",
    "Fetch",
    "ParameterValues",
    "Split",
    "java_replacement",
    "split",
    "sql_literal",
    "to_dialect",
]
