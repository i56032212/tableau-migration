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

PostgreSQL（16 以上）、Spark（Databricks 的引擎，3.4 以上；另以 Spark Thrift Server 走 HiveServer2 協定實跑）、
MySQL（8.0 以上）、Trino（CI 以 Docker 執行；Athena 的 SQL 也在同一台執行）與 BigQuery（CI 以 ZetaSQL 的模擬器執行）
有實跑測試（tests/sqlgen/test_engines.py）；Oracle（12.2 以上）、SQL Server（2017 以上）、Snowflake、Redshift、
Teradata（16.20 以上）、Hive（2.3 以上）、Impala（3.3 以上）、Db2（11.5 以上）與 SAP HANA（2.0）依官方文件撰寫，
尚未實機驗證：轉譯後的 SQL 以 SQLGlot
轉回 DuckDB、各自特有的函式依文件的語意模擬後執行，並有轉譯快照。

各方言已知與 DuckDB（＝萃取時的結果）不同之處，與 Tableau 直接連該資料庫時相同，不另外處理：
字串的排序與比較依資料庫的定序；Oracle 的空字串即 Null、ADD_MONTHS 在月底的行為；
STR() 轉換小數的格式依資料庫；TODAY()、NOW() 依資料庫的時區。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import ClassVar

import sqlglot
from sqlglot import exp
from sqlglot.dialects.hive import Hive
from sqlglot.dialects.oracle import Oracle
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import ParseError

from tmig.data import udf
from tmig.formula.functions import icu_features
from tmig.ir.models import Scalar
from tmig.sqlgen.query import QueryError, WorksheetSql
from tmig.sqlgen.source import CUSTOM_SQL_PREFIX


class TmigHive(Hive):
    """產生 Hive、Impala 的 SQL：Null 也相等的比較寫成 <=>（Hive 0.9 起；IS NOT DISTINCT FROM 到 Hive 3 才有）。
    SQLGlot 以類別名稱登錄方言（tmighive）；解析與 Hive 相同。"""

    class Generator(Hive.Generator):
        TRANSFORMS: ClassVar[dict[type[exp.Expr], Callable[..., str]]] = {
            **Hive.Generator.TRANSFORMS,
            exp.NullSafeEQ: lambda self, e: self.binary(e, "<=>"),
        }


class TmigDb2(Oracle):
    """產生 Db2 的 SQL：以 SQLGlot 的 Oracle 寫法為基礎（雙引號、FETCH FIRST n ROWS ONLY、資料表別名），
    型別改成 Db2 的名稱。SQLGlot 以類別名稱登錄方言（tmigdb2）；解析與 Oracle 相同。"""

    class Generator(Oracle.Generator):
        TYPE_MAPPING: ClassVar = {
            **Oracle.Generator.TYPE_MAPPING,
            exp.DataType.Type.TINYINT: "SMALLINT",
            exp.DataType.Type.INT: "INTEGER",
            exp.DataType.Type.BIGINT: "BIGINT",
            exp.DataType.Type.DECIMAL: "DECIMAL",
            exp.DataType.Type.DOUBLE: "DOUBLE",
            exp.DataType.Type.FLOAT: "DOUBLE",
            exp.DataType.Type.VARCHAR: "VARCHAR",
            exp.DataType.Type.NVARCHAR: "VARCHAR",
            exp.DataType.Type.TEXT: "VARCHAR",
        }


class TmigHana(Postgres):
    """產生 SAP HANA 的 SQL：以 SQLGlot 的 PostgreSQL 寫法為基礎（雙引號、LIMIT），型別改成 HANA 的名稱。
    SQLGlot 以類別名稱登錄方言（tmighana）；解析與 PostgreSQL 相同。"""

    class Generator(Postgres.Generator):
        TYPE_MAPPING: ClassVar = {
            **Postgres.Generator.TYPE_MAPPING,
            exp.DataType.Type.TINYINT: "SMALLINT",
            exp.DataType.Type.DOUBLE: "DOUBLE",
            exp.DataType.Type.FLOAT: "DOUBLE",
            exp.DataType.Type.VARCHAR: "NVARCHAR",
            exp.DataType.Type.NVARCHAR: "NVARCHAR",
            exp.DataType.Type.TEXT: "NVARCHAR",
            exp.DataType.Type.TIMESTAMPNTZ: "TIMESTAMP",
        }


SQLGLOT_NAMES: dict[str, str] = {
    "duckdb": "duckdb",
    "postgres": "postgres",
    "redshift": "redshift",
    "databricks": "databricks",
    "spark": "spark",
    "oracle": "oracle",
    "mysql": "mysql",
    "tsql": "tsql",
    "snowflake": "snowflake",
    "trino": "trino",
    "athena": "athena",
    "bigquery": "bigquery",
    "teradata": "teradata",
    "hive": "tmighive",
    "impala": "tmighive",
    "db2": "tmigdb2",
    "hana": "tmighana",
}
SUPPORTED: frozenset[str] = frozenset(SQLGLOT_NAMES)
"""已有轉譯規則的方言；其他方言尚不支援。"""

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


def underlying_query(query: WorksheetSql, limit: int | None = None) -> WorksheetSql:
    """完整資料（基礎資料列）的查詢：只帶 query.underlying 用到的 CTE（都可以下推），工作表本身的查詢不變。

    與工作表的查詢一樣以 DuckDB 執行（萃取），或以 split 兩段式執行（Live）。不支援時拋出 QueryError。"""
    if not query.underlying:
        raise QueryError("這張工作表不支援完整資料（例如 Data Blending）")
    main = query.underlying + (f"\nLIMIT {int(limit)}" if limit else "")
    names = {c.name for c in query.ctes}
    deps = {c.name: _references(c.body, names - {c.name}) for c in query.ctes}
    needed: set[str] = set()
    for name in _references(main, names):
        needed |= _closure(name, deps)
    ctes = [c for c in query.ctes if c.name in needed]
    head = "WITH RECURSIVE" if query.sql.lstrip().startswith("WITH RECURSIVE") else "WITH"
    body = ",\n".join(f'"{c.name}" AS (\n  {c.body}\n)' for c in ctes)
    sql = f"{head}\n{body}\n{main}" if ctes else main
    return replace(query, sql=sql, main=main, ctes=ctes, columns=[], guards=[])


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
    out = rewriter.text(tree.sql(dialect=SQLGLOT_NAMES[dialect], identify=True))
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
        if dialect == "bigquery" and datatype == "datetime":
            # BigQuery 的 TIMESTAMP 是含時區的時間；沒有時區的是 DATETIME
            return f"DATETIME {_single_quote(text)}"
        if dialect == "tsql":
            # SQL Server 沒有 DATE '…' 的常值寫法；ISO 格式的字串轉型不受語言設定影響
            return f"CAST({_single_quote(text)} AS {'DATE' if datatype == 'date' else 'DATETIME2'})"
        if dialect in ("hive", "impala", "db2"):
            # 舊版的 Hive、Impala 與 Db2 沒有 DATE '…'、TIMESTAMP '…' 的常值寫法；字串轉型都支援
            return f"CAST({_single_quote(text)} AS {'DATE' if datatype == 'date' else 'TIMESTAMP'})"
        return f"{'DATE' if datatype == 'date' else 'TIMESTAMP'} {_single_quote(text)}"
    text = str(value)
    if dialect in ("spark", "databricks", "mysql", "snowflake", "bigquery", "hive", "impala"):
        # 字串常值以反斜線跳脫
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if dialect == "tsql" and not text.isascii():
        return "N" + _single_quote(text)
    if dialect == "redshift":
        # Redshift 的字串常值中反斜線是跳脫字元
        return _single_quote(text.replace("\\", "\\\\"))
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


def _template(text: str, dialect: str, **parts: exp.Expr) -> exp.Expr:
    """目標方言的運算式樣板：{名稱} 換成（已改寫的）節點。樣板本身的節點都標記為改寫規則產生的，
    換進去的節點照樣驗證。"""
    tree = sqlglot.parse_one(text.format(**{k: f"__tmig_{k}__" for k in parts}), read=dialect)
    for node in tree.walk():
        _mark(node)
    for column in list(tree.find_all(exp.Column)):
        key = column.name
        if key.startswith("__tmig_") and key.endswith("__"):
            column.replace(exp.Paren(this=parts[key[7:-2]].copy()))
    return tree


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
    icu_regex: frozenset[str] = frozenset()
    """來源資料庫的正規表示式支援的 ICU 語法（RE2 不支援的，見 tmig.formula.functions.icu_features）。"""

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
        if name in udf.SPATIAL_FUNCTIONS:
            raise DialectError(
                f"空間資料（MAKEPOINT 等空間函式、空間檔案的形狀）在 Live 連線的 {self.name} 尚未支援"
            )
        if name in udf.REGEX_FUNCTIONS:
            assert isinstance(node, exp.Anonymous)
            return self._icu_regex(name, list(node.expressions))
        if name is not None:
            assert isinstance(node, exp.Anonymous)
            fn = self.functions().get(name)
            if fn is not None:
                return fn(list(node.expressions))
        return node

    def _icu_regex(self, name: str, args: list[exp.Expr]) -> exp.Expr:
        """用到 RE2 不支援的語法（前後查看等）的正規表示式：來源資料庫支援時轉成它的正規表示式函式
        （與一般的寫法相同的改寫），否則不能轉譯。"""
        pattern = args[1]
        features = (
            icu_features(pattern.name)
            if isinstance(pattern, exp.Literal) and pattern.is_string
            else ["不是常值的樣式"]
        )
        missing = [f for f in dict.fromkeys(features) if f not in self.icu_regex]
        if missing:
            raise DialectError(f"正規表示式用到{'、'.join(missing)}，{self.name} 不支援")
        if name == udf.MATCH:
            return self.node(exp.RegexpLike(this=args[0], expression=args[1]))
        if name == udf.EXTRACT:
            # 沒有符合時為 Null（與一般的寫法相同）
            matched = self.node(exp.RegexpLike(this=args[0].copy(), expression=args[1].copy()))
            extracted = self.node(
                exp.RegexpExtract(this=args[0], expression=args[1], group=args[2])
            )
            return exp.Case(ifs=[exp.If(this=matched, true=extracted)])
        replacement = args[2]
        if isinstance(replacement, exp.Literal) and replacement.is_string:
            replacement = exp.Literal.string(_icu_to_re2(replacement.name))
        # 與一般的寫法（regexp_replace(…, 'g')）相同的語法節點
        replaced = sqlglot.parse_one("regexp_replace(a, b, c, 'g')", read="duckdb")
        replaced.set("this", args[0])
        replaced.set("expression", args[1])
        replaced.set("replacement", replacement)
        return self.node(replaced)

    def finish(self, tree: exp.Expr) -> None:
        """整棵樹改寫完成後的調整。"""

    def text(self, sql: str) -> str:
        """產生的 SQL 文字的最後調整（SQLGlot 沒有語法節點的寫法）。"""
        return sql

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


ICU_ALL = frozenset(
    {"前向查看", "否定前向查看", "後向查看", "否定後向查看", "原子群組", "佔有量詞", "反向參照"}
)
"""ICU、Java、PCRE、Oniguruma 的正規表示式都支援的語法（RE2 都不支援）。"""


def _icu_to_re2(text: str) -> str:
    from tmig.sqlgen.expr import icu_replacement

    return icu_replacement(text)


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
    # PostgreSQL 的 ARE：前後查看與反向參照（沒有原子群組與佔有量詞）
    icu_regex = frozenset({"前向查看", "否定前向查看", "後向查看", "否定後向查看", "反向參照"})
    allowed = frozenset(
        {
            exp.Degrees,
            exp.Radians,
            exp.Pi,
            exp.Rand,
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


# ---------------------------------------------------------------- Amazon Redshift


REDSHIFT_NUMBER = r"^[[:space:]]*[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][-+]?[0-9]+)?[[:space:]]*$"
REDSHIFT_INTEGER = r"^[[:space:]]*[-+]?[0-9]+[[:space:]]*$"
REDSHIFT_DATE = (
    r"^[[:space:]]*[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
    r"([ T]([01]?[0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?)?[[:space:]]*$"
)
"""Redshift 沒有 TRY_CAST：轉型前以正規表示式檢查文字（數字、整數、ISO 格式的日期時間）；
日期另外檢查月與日存在（見 RedshiftRewriter.try_cast）。"""


class RedshiftRewriter(PostgresRewriter):
    """Amazon Redshift：PostgreSQL 8 系的語法加上 Redshift 自己的函式（DATEADD、GETDATE、REGEXP_* 等）。
    依官方文件撰寫，尚未實機驗證（測試以 SQLGlot 轉回 DuckDB 模擬執行）。

    沒有陣列、IS NOT DISTINCT FROM、TRY_CAST 與 CORR／COVAR；正規表示式以 PCRE 解讀（參數 p），
    取群組只支援第 1 個群組，取代字串不能參照群組。字串常值中的反斜線要寫成兩個（SQLGlot 處理）。
    工作階段的時區為 UTC（Redshift 的預設）。
    """

    name = "Redshift"
    dialect = "redshift"
    icu_regex = ICU_ALL  # 以 PCRE 解讀（參數 p）
    allowed = (PostgresRewriter.allowed - {exp.NullSafeEQ, exp.ArrayToString, exp.Log}) | {
        exp.Unicode
    }

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        """DATEADD 的數量必須是整數：微秒以整數微秒加上。"""
        if unit == "microsecond":
            amount = _fn("ROUND", _paren(amount))
        return _fn("DATEADD", exp.Var(this=unit.upper()), _cast(amount, "BIGINT"), value)

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        base = super().handlers()
        return {
            **base,
            # 星期一為 1（DOW 是星期日為 0）
            exp.DayOfWeekIso: lambda n: self._iso_dow(n.this),
            exp.Log: self.log,
            exp.Round: lambda n: _cast(
                _fn(
                    "ROUND",
                    _cast(n.this, _type("DECIMAL(38, 10)", "redshift")),
                    _cast(n.args.get("decimals") or _num(0), "INT"),
                ),
                "DOUBLE",
            ),
            exp.DateFromParts: lambda n: self.make_date(
                n.args["year"], n.args["month"], n.args["day"]
            ),
            exp.TimestampFromParts: self.make_timestamp,
            exp.Monthname: lambda n: exp.Trim(this=_fn("TO_CHAR", n.this, _str("Month"))),
            exp.Dayname: lambda n: exp.Trim(this=_fn("TO_CHAR", n.this, _str("Day"))),
            exp.TryCast: self.try_cast,
            exp.RegexpLike: lambda n: exp.GT(
                this=_fn(
                    "REGEXP_INSTR", n.this, n.expression, _num(1), _num(1), _num(0), _str("p")
                ),
                expression=_num(0),
            ),
            exp.RegexpExtract: self.regexp_extract,
            exp.RegexpReplace: self.regexp_replace,
            exp.Split: self.no_arrays,
            exp.NullSafeEQ: lambda n: exp.Paren(
                this=exp.Or(
                    this=exp.EQ(this=n.this.copy(), expression=n.expression.copy()),
                    expression=exp.And(
                        this=exp.Is(this=n.this, expression=exp.Null()),
                        expression=exp.Is(this=n.expression, expression=exp.Null()),
                    ),
                )
            ),
            exp.Corr: self.no_aggregate,
            exp.CovarSamp: self.no_aggregate,
            exp.CovarPop: self.no_aggregate,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            **super().functions(),
            # ISO 年：該週星期四所在的年
            "isoyear": lambda a: _extract(
                "YEAR",
                _fn(
                    "DATEADD",
                    exp.Var(this="DAY"),
                    exp.Sub(this=_num(4), expression=self._iso_dow(a[0].copy())),
                    a[0],
                ),
            ),
            "now": lambda a: _fn("GETDATE"),
            "list_slice": lambda a: self.no_arrays(a[0]),
        }

    def _iso_dow(self, value: exp.Expr) -> exp.Expr:
        dow = _extract("DOW", value)
        shifted = exp.Mod(this=_paren(exp.Add(this=dow, expression=_num(6))), expression=_num(7))
        return exp.Paren(this=exp.Add(this=shifted, expression=_num(1)))

    def no_arrays(self, node: exp.Expr) -> exp.Expr:
        raise DialectError("Redshift 沒有陣列（SPLIT 的負數位置、FINDNTH）")

    def no_aggregate(self, node: exp.Expr) -> exp.Expr:
        raise DialectError(f"Redshift 沒有 {_describe(node).removeprefix('函式 ')} 彙總函式")

    def log(self, node: exp.Expr) -> exp.Expr:
        if node.expression is not None and _literal_int(node.this) == 10:
            return _fn("LOG", node.expression)  # Redshift 的 LOG 以 10 為底
        raise DialectError("Redshift 只支援以 10 為底的 LOG")

    def make_date(self, y: exp.Expr, m: exp.Expr, d: exp.Expr) -> exp.Expr:
        """2000-01-01 加上年、月、日的差（Redshift 沒有由年月日組成日期的函式）。"""
        start = _cast(_str("2000-01-01"), "DATE")
        years = _fn(
            "DATEADD",
            exp.Var(this="YEAR"),
            _cast(exp.Sub(this=_paren(y), expression=_num(2000)), "INT"),
            start,
        )
        months = _fn(
            "DATEADD",
            exp.Var(this="MONTH"),
            _cast(exp.Sub(this=_paren(m), expression=_num(1)), "INT"),
            years,
        )
        days = _fn(
            "DATEADD",
            exp.Var(this="DAY"),
            _cast(exp.Sub(this=_paren(d), expression=_num(1)), "INT"),
            months,
        )
        return _cast(days, "DATE")

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        day = _cast(self.make_date(y, m, d), "TIMESTAMP")
        seconds = exp.Add(
            this=exp.Add(
                this=exp.Mul(this=_paren(hh), expression=_num(3600)),
                expression=exp.Mul(this=_paren(mi), expression=_num(60)),
            ),
            expression=_paren(ss),
        )
        whole = exp.Floor(this=_paren(seconds))
        # 整數秒與剩下的微秒分開加（DATEADD 的數量是 32 位元整數）
        with_seconds = _fn("DATEADD", exp.Var(this="SECOND"), _cast(whole, "INT"), day)
        micros = _fn(
            "ROUND",
            exp.Mul(
                this=_paren(exp.Sub(this=_paren(seconds.copy()), expression=whole.copy())),
                expression=_num(1000000),
            ),
        )
        return _fn("DATEADD", exp.Var(this="MICROSECOND"), _cast(micros, "INT"), with_seconds)

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in INTEGER_TYPES:
            pattern = REDSHIFT_INTEGER
        elif kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DECIMAL):
            pattern = REDSHIFT_NUMBER
        elif kind == exp.DataType.Type.DATE or kind in TIMESTAMP_TYPES:
            pattern = REDSHIFT_DATE
        else:
            raise DialectError(f"Redshift 不支援轉成 {target.sql('duckdb')} 的 TRY_CAST")
        text = _cast(node.this, "TEXT")
        valid = _mark(exp.RegexpLike(this=text.copy(), expression=_str(pattern)))
        value: exp.Expr = text.copy()
        if pattern == REDSHIFT_DATE:
            # 月與日要存在：TO_DATE（非嚴格模式）會把 2024-13-05、2024-02-30 進位成別的日期
            day = _fn(
                "SPLIT_PART",
                _fn("SPLIT_PART", exp.Trim(this=text.copy()), _str(" "), _num(1)),
                _str("T"),
                _num(1),
            )
            parsed = _fn("TO_DATE", day.copy(), _str("YYYY-MM-DD"))

            def part(index: int, field: str) -> exp.Expr:
                piece = _cast(_fn("SPLIT_PART", day.copy(), _str("-"), _num(index)), "INT")
                return exp.EQ(this=_extract(field, parsed.copy()), expression=piece)

            exists = exp.And(this=part(2, "MONTH"), expression=part(3, "DAY"))
            value = exp.Case(ifs=[exp.If(this=exists, true=value)])
        # 轉型放在 CASE 外面：常值的 CAST 會在規劃查詢時就計算，放在 THEN 裡面擋不住錯誤
        return exp.Cast(this=exp.Case(ifs=[exp.If(this=valid, true=value)]), to=target)

    def regexp_extract(self, node: exp.Expr) -> exp.Expr:
        group = _literal_int(node.args.get("group"))
        if group not in (0, 1):
            raise DialectError("Redshift 的 REGEXP_EXTRACT 只能取整段或第 1 個群組")
        flags = "p" if group == 0 else "pe"
        return _fn("REGEXP_SUBSTR", node.this, node.expression, _num(1), _num(1), _str(flags))

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        replacement = node.args.get("replacement") or _str("")
        if not (isinstance(replacement, exp.Literal) and replacement.is_string):
            raise DialectError("REGEXP_REPLACE 的取代字串不是常值")
        text = replacement.name
        if "\\" in text or "$" in text:
            raise DialectError("Redshift 的 REGEXP_REPLACE 取代字串不能參照群組")
        return _fn("REGEXP_REPLACE", node.this, node.expression, _str(text), _num(1), _str("p"))


# ---------------------------------------------------------------- Spark／Databricks


class SparkRewriter(Rewriter):
    """Spark 3.4 以上與 Databricks（連線時把工作階段的時區設為 UTC）。"""

    icu_regex = ICU_ALL  # Java 的正規表示式

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
            exp.Rand,
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
            exp.Rand: lambda n: _fn("DBMS_RANDOM.VALUE"),
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
    """節點是否位在條件的位置（WHERE、HAVING、QUALIFY、ON、CASE WHEN、AND／OR／NOT 的運算元）。"""
    child, parent = node, node.parent
    while isinstance(parent, exp.Paren):
        child, parent = parent, parent.parent
    if isinstance(parent, (exp.Where, exp.Having, exp.Qualify, exp.And, exp.Or, exp.Not)):
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
    icu_regex = ICU_ALL  # MySQL 8 以 ICU 執行
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
            exp.Rand,
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
            exp.Rand: lambda n: _fn(
                "UNIFORM", _cast(_num(0), "DOUBLE"), _cast(_num(1), "DOUBLE"), _fn("RANDOM")
            ),
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


# ---------------------------------------------------------------- Trino／Presto、Amazon Athena


TRINO_FORMAT = {
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
"""strftime 的格式 → Trino 的 date_parse 格式（MySQL 的寫法：%i 是分鐘、%M 是月份名稱）。"""

_TRINO_POSITION = "(1 + CAST({p} AS DOUBLE) * (CARDINALITY(tmig_v) - 1))"
TRINO_PERCENTILE = (
    "ELEMENT_AT(TRANSFORM(ARRAY[ARRAY_SORT(ARRAY_AGG({x}) FILTER (WHERE {x} IS NOT NULL))], "
    "tmig_v -> CASE WHEN CARDINALITY(tmig_v) = 0 THEN NULL "
    f"ELSE ELEMENT_AT(tmig_v, CAST(FLOOR({_TRINO_POSITION}) AS BIGINT)) "
    f"+ ({_TRINO_POSITION} - FLOOR({_TRINO_POSITION})) "
    f"* (ELEMENT_AT(tmig_v, CAST(CEIL({_TRINO_POSITION}) AS BIGINT)) "
    f"- ELEMENT_AT(tmig_v, CAST(FLOOR({_TRINO_POSITION}) AS BIGINT))) END), 1)"
)
"""精確的百分位數（與 DuckDB 的 quantile_cont 相同的線性內插）：不是 Null 的值排序成陣列，取第
1 + p × (n − 1) 個位置前後兩個值內插。Trino 只有近似的 approx_percentile。"""

TIME_UNITS = ("hour", "minute", "second", "microsecond")


def _not_negative(node: exp.Expr) -> bool:
    """確定不是負數的數量：非負的常值或字串長度（STARTSWITH、ENDSWITH 的寫法）。"""
    while isinstance(node, (exp.Cast, exp.Paren)):
        node = node.this
    fixed = _literal_int(node)
    return (fixed is not None and fixed >= 0) or isinstance(node, exp.Length)


def _known_timestamp(value: exp.Expr) -> bool:
    """確定是 TIMESTAMP 的值：轉成 TIMESTAMP 的轉型，或對它加減的結果。"""
    if value.meta.get("timestamp"):
        return True
    return isinstance(value, exp.Cast) and value.to.this in TIMESTAMP_TYPES


class TrinoRewriter(Rewriter):
    """Trino（原 PrestoSQL）、Starburst 與 PrestoDB 共同支援的寫法；Tableau 的 Presto 連線用這一套。
    Amazon Athena（engine 3 以 Trino 為基礎）規則相同（AthenaRewriter）。連線時把工作階段的時區設為 UTC。

    - 時間一律以 TIMESTAMP（毫秒）表示：PrestoDB 與 Athena 不支援 TIMESTAMP(p)；date_add 最小的單位是
      毫秒（沒有微秒），日期加減時間單位前先轉成 TIMESTAMP（DATE 不能加減小時）
    - 星期：DAY_OF_WEEK 是 ISO（星期一為 1）；週數 WEEK 與 ISO 年 YEAR_OF_WEEK 與 DuckDB 相同
    - DATEDIFF 以年、季、月、日、時、分、秒的邊界計數（date_diff 是經過的完整單位）
    - 沒有 make_date、left／right 與精確的百分位數：以 date_add、substr 與排序後的陣列內插計算
    - split_part 超出範圍為 Null（DuckDB 為空字串）、不接受負數的位置：以 element_at 取
    - 正規表示式以 Joni（Java 的寫法）解讀；取代字串的群組參照為 $1、錢字號寫成 \\$
    """

    name = "Trino"
    dialect = "trino"
    icu_regex = ICU_ALL  # 預設以 Joni（Oniguruma）執行
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
            exp.Week,
            exp.TimeToUnix,
            exp.Pi,
            exp.Rand,
            exp.Degrees,
            exp.Radians,
            exp.Round,
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

    def __init__(self, name: str = "Trino", dialect: str = "trino") -> None:
        self.name = name
        self.dialect = dialect

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit in ("second", "microsecond"):
            # 秒與微秒換成毫秒（date_add 最小的單位）
            factor = _num(1000) if unit == "second" else _num(0.001)
            millis = _fn("ROUND", exp.Mul(this=_paren(amount), expression=factor))
            unit, amount = "millisecond", millis
        if unit in (*TIME_UNITS, "millisecond"):
            value = self._timestamp(value)
        result = _fn("DATE_ADD", _str(unit), _cast(amount, "BIGINT"), value)
        if _known_timestamp(value):
            result.meta["timestamp"] = True
        return result

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        return {
            # 星期日為 0（DAY_OF_WEEK 是 ISO：星期一為 1、星期日為 7）
            exp.DayOfWeek: lambda n: exp.Paren(
                this=exp.Mod(this=_fn("DAY_OF_WEEK", n.this), expression=_num(7))
            ),
            exp.TimestampTrunc: self.date_trunc,
            exp.DateTrunc: self.date_trunc,
            exp.DateDiff: self.date_diff,
            exp.DateFromParts: lambda n: self.make_date(
                n.args["year"], n.args["month"], n.args["day"]
            ),
            exp.TimestampFromParts: self.make_timestamp,
            exp.Monthname: lambda n: _names(_fn("MONTH", n.this), MONTH_NAMES),
            exp.Dayname: lambda n: _names(_fn("DAY_OF_WEEK", n.this), DAY_NAMES),
            exp.Log: self.log,
            # CODEPOINT 只接受一個字元的 VARCHAR(1)
            exp.Unicode: lambda n: _fn(
                "CODEPOINT", _cast(_fn("SUBSTR", n.this, _num(1), _num(1)), "VARCHAR(1)")
            ),
            # REPEAT 是陣列函式：重複的元素串接成字串
            exp.Repeat: lambda n: _fn(
                "ARRAY_JOIN",
                _fn("REPEAT", n.this, _cast(n.args["times"], "INTEGER")),
                _str(""),
            ),
            exp.Left: self.left,
            exp.Right: self.right,
            exp.SplitPart: self.split_part,
            exp.Split: lambda n: _array(_fn("SPLIT", n.this, n.expression)),
            exp.Length: lambda n: _fn("CARDINALITY", n.this) if _is_array(n.this) else n,
            exp.RegexpReplace: self.regexp_replace,
            exp.Median: lambda n: self.percentile(n.this, _num(0.5)),
            exp.PercentileCont: lambda n: self.percentile(n.this, n.expression),
            exp.DataType: self.datatype,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: _fn("YEAR_OF_WEEK", a[0]),
            "now": lambda a: exp.Var(this="LOCALTIMESTAMP"),
            "try_strptime": self.parse_date,
            "list_slice": self.list_slice,
        }

    def _timestamp(self, value: exp.Expr) -> exp.Expr:
        """要加減時間單位或取時間部分的值：先轉成 TIMESTAMP（已知是時間的不再轉）。"""
        return value if _known_timestamp(value) else _cast(value, "TIMESTAMP")

    def datatype(self, node: exp.Expr) -> exp.Expr:
        # DuckDB 的 DECIMAL 預設為 (18, 3)；Trino 為 (38, 0)
        decimal = isinstance(node, exp.DataType) and node.this == exp.DataType.Type.DECIMAL
        if decimal and not node.expressions:
            return _type("DECIMAL(18, 3)")
        return node

    def date_trunc(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        value = node.this
        if unit in TIME_UNITS:
            value = self._timestamp(value)
        return _fn("DATE_TRUNC", _str(unit), value)

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        def part(field: str, value: exp.Expr) -> exp.Expr:
            return _fn(field.upper(), self._timestamp(value) if field in TIME_UNITS else value)

        return self.boundary_diff(
            _unit(node),
            node.expression,
            node.this,
            part,
            lambda start, end: _fn(
                "DATE_DIFF", _str("day"), _cast(start, "DATE"), _cast(end, "DATE")
            ),
        )

    def make_date(self, y: exp.Expr, m: exp.Expr, d: exp.Expr) -> exp.Expr:
        """2000-01-01 依序加上年、月、日的差（Trino 沒有由年月日組成日期的函式）。"""
        value: exp.Expr = _cast(_str("2000-01-01"), "DATE")
        for unit, part, origin in (("year", y, 2000), ("month", m, 1), ("day", d, 1)):
            if _literal_int(part) == origin:
                continue
            offset = _cast(exp.Sub(this=_paren(part), expression=_num(origin)), "BIGINT")
            value = _fn("DATE_ADD", _str(unit), offset, value)
        return value

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        seconds = exp.Add(
            this=exp.Add(
                this=exp.Mul(this=_paren(hh), expression=_num(3600)),
                expression=exp.Mul(this=_paren(mi), expression=_num(60)),
            ),
            expression=_paren(ss),
        )
        return self.add_interval(_cast(self.make_date(y, m, d), "TIMESTAMP"), "second", seconds)

    def log(self, node: exp.Expr) -> exp.Expr:
        if node.expression is None:
            return _fn("LOG10", node.this)  # DuckDB 的 log(x) 以 10 為底
        base = _literal_int(node.this)
        if base in (2, 10):
            return _fn(f"LOG{base}", node.expression)
        return exp.Div(this=exp.Ln(this=node.expression), expression=exp.Ln(this=node.this))

    def left(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        head = _fn("SUBSTR", text.copy(), _num(1), _cast(count.copy(), "BIGINT"))
        if _not_negative(count):
            return head
        # DuckDB 的 left 數量為負數時去掉右邊的字元
        trimmed = _fn(
            "SUBSTR",
            text,
            _num(1),
            _fn("GREATEST", exp.Add(this=exp.Length(this=text.copy()), expression=count), _num(0)),
        )
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count.copy(), expression=_num(0)), true=trimmed)],
            default=head,
        )

    def right(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        start = exp.Add(
            this=exp.Sub(this=exp.Length(this=text.copy()), expression=_paren(count.copy())),
            expression=_num(1),
        )
        # 數量超過長度時取整個字串（substr 的起點小於 1 時從結尾算起）
        tail = _fn("SUBSTR", text.copy(), _fn("GREATEST", start, _num(1)))
        if _not_negative(count):
            return tail
        # DuckDB 的 right 數量為負數時去掉左邊的字元
        trimmed = _fn("SUBSTR", text, exp.Sub(this=_num(1), expression=_paren(count.copy())))
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count, expression=_num(0)), true=trimmed)],
            default=tail,
        )

    def split_part(self, node: exp.Expr) -> exp.Expr:
        text, delimiter = node.this, node.args["delimiter"]
        index = node.args["part_index"]
        fixed = _literal_int(index)
        if fixed is not None and fixed > 0:
            part = _fn("SPLIT_PART", text.copy(), delimiter.copy(), _cast(index.copy(), "BIGINT"))
        else:
            # 負數從結尾算起（element_at）；0 與超出範圍為空字串
            part = _fn(
                "ELEMENT_AT",
                _fn("SPLIT", text.copy(), delimiter.copy()),
                exp.Nullif(this=index.copy(), expression=_num(0)),
            )
        known = exp.And(
            this=exp.And(
                this=exp.Not(this=exp.Is(this=text, expression=exp.Null())),
                expression=exp.Not(this=exp.Is(this=delimiter, expression=exp.Null())),
            ),
            expression=exp.Not(this=exp.Is(this=index, expression=exp.Null())),
        )
        return exp.Coalesce(
            this=part, expressions=[exp.Case(ifs=[exp.If(this=known, true=_str(""))])]
        )

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        replacement = node.args.get("replacement") or _str("")
        if not (isinstance(replacement, exp.Literal) and replacement.is_string):
            raise DialectError("REGEXP_REPLACE 的取代字串不是常值")
        return _fn(
            "REGEXP_REPLACE", node.this, node.expression, _str(java_replacement(replacement.name))
        )

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：date_parse 不符合格式時報錯，以 TRY 包住得 Null。"""
        fmt = _convert_format(
            _format_literal(args[1]), TRINO_FORMAT, lambda s: s.replace("%", "%%")
        )
        return _cast(_fn("TRY", _fn("DATE_PARSE", args[0], _str(fmt))), "TIMESTAMP")

    def list_slice(self, args: list[exp.Expr]) -> exp.Expr:
        """DuckDB 的 list_slice（1 起算、含結尾）→ SLICE（起點、長度）。"""
        array, start, end = args
        if not _is_array(array):
            raise DialectError("list_slice 只支援 FINDNTH 的寫法")
        length = _fn(
            "GREATEST",
            exp.Add(
                this=exp.Sub(this=_paren(end), expression=_paren(start.copy())), expression=_num(1)
            ),
            _num(0),
        )
        return _array(_fn("SLICE", array, start, length))

    def percentile(self, value: exp.Expr, fraction: exp.Expr) -> exp.Expr:
        return _template(TRINO_PERCENTILE, "trino", x=value, p=fraction)


# ---------------------------------------------------------------- Google BigQuery


BIGQUERY_FORMAT = {
    "%Y": "%Y",
    "%y": "%y",
    "%m": "%m",
    "%d": "%d",
    "%H": "%H",
    "%I": "%I",
    "%M": "%M",
    "%S": "%S",
    "%p": "%p",
    "%b": "%b",
    "%B": "%B",
    "%j": "%j",
}
"""strftime 的格式 → BigQuery 的 PARSE_DATETIME 格式（秒的小數另外處理）。"""

BIGQUERY_UNITS = {
    "year": "YEAR",
    "quarter": "QUARTER",
    "month": "MONTH",
    "week": "ISOWEEK",
    "day": "DAY",
    "hour": "HOUR",
    "minute": "MINUTE",
    "second": "SECOND",
    "millisecond": "MILLISECOND",
    "microsecond": "MICROSECOND",
}
"""DuckDB 的日期單位 → BigQuery（DuckDB 的週從星期一開始，即 ISOWEEK）。"""

_BIGQUERY_SORTED = "ARRAY_AGG({x} IGNORE NULLS ORDER BY {x})"
_BIGQUERY_POSITION = "(CAST({p} AS FLOAT64) * (ARRAY_LENGTH(ARRAY_AGG({x} IGNORE NULLS)) - 1))"
BIGQUERY_PERCENTILE = (
    f"{_BIGQUERY_SORTED}[SAFE_OFFSET(CAST(FLOOR({_BIGQUERY_POSITION}) AS INT64))] "
    f"+ ({_BIGQUERY_POSITION} - FLOOR({_BIGQUERY_POSITION})) "
    f"* ({_BIGQUERY_SORTED}[SAFE_OFFSET(CAST(CEIL({_BIGQUERY_POSITION}) AS INT64))] "
    f"- {_BIGQUERY_SORTED}[SAFE_OFFSET(CAST(FLOOR({_BIGQUERY_POSITION}) AS INT64))])"
)
"""精確的百分位數（與 DuckDB 的 quantile_cont 相同的線性內插）：PERCENTILE_CONT 在 BigQuery 只是視窗函式，
彙總時以排序後的 ARRAY_AGG 取第 p × (n − 1) 個位置（0 起算）前後兩個值內插。"""


class BigQueryRewriter(Rewriter):
    """Google BigQuery（GoogleSQL）。

    - 沒有時區的時間一律是 DATETIME（DuckDB 的 TIMESTAMP）；Live 連線的 base 把日期時間欄位轉成 DATETIME
      （BigQuery 的 TIMESTAMP 欄位依 UTC 換算，見 tmig.sqlgen.source），日期加減、截斷與 DATEDIFF 一律以
      DATETIME_ADD、DATETIME_TRUNC、DATETIME_DIFF 計算（DATE 可以隱含轉成 DATETIME；結果與 DuckDB 相同是時間）
    - DATETIME_DIFF 計算跨過的單位邊界數，與 DuckDB 的 date_diff 相同
    - 星期：EXTRACT(DAYOFWEEK) 是星期日為 1；週數、ISO 年為 ISOWEEK、ISOYEAR
    - 沒有 PI、DEGREES、RADIANS：以 ACOS(-1) 計算
    - REGEXP_EXTRACT 沒有群組參數（樣式有一個群組時取該群組）：要取的群組以外的群組改成不擷取的 (?:…)
    - MEDIAN、PERCENTILE 以排序後的 ARRAY_AGG 內插（精確值；APPROX_QUANTILES 是近似值）
    - DATEPARSE 以 SAFE.PARSE_DATETIME，不符合格式時為 Null
    """

    name = "BigQuery"
    dialect = "bigquery"
    allowed = frozenset(
        {
            exp.Year,
            exp.Quarter,
            exp.Month,
            exp.Day,
            exp.DayOfYear,
            exp.DayOfWeekIso,
            exp.Log,
            exp.Round,
            exp.Repeat,
            exp.Unicode,
            exp.Variance,
            exp.VariancePop,
            exp.IntDiv,
            exp.Mod,
            exp.NullSafeEQ,
            exp.CurrentDate,
            exp.TryCast,
            exp.DPipe,
            exp.Boolean,
            exp.ArrayToString,
            exp.DateFromParts,
        }
    )

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit == "second":
            # 秒可能有小數：換成微秒
            amount = _fn("ROUND", exp.Mul(this=_paren(amount), expression=_num(1000000)))
            unit = "microsecond"
        interval = exp.Interval(
            this=_cast(amount, "INT64", "bigquery"), unit=exp.Var(this=BIGQUERY_UNITS[unit])
        )
        return self._datetime_result(_fn("DATETIME_ADD", self._datetime(value), _mark(interval)))

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def time_part(field: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: _extract(field, self._datetime(n.this))

        return {
            exp.Hour: time_part("HOUR"),
            exp.Minute: time_part("MINUTE"),
            exp.Second: time_part("SECOND"),
            # EXTRACT(DAYOFWEEK) 是星期日為 1
            exp.DayOfWeek: lambda n: exp.Paren(
                this=exp.Sub(this=_extract("DAYOFWEEK", n.this), expression=_num(1))
            ),
            exp.Week: lambda n: _extract("ISOWEEK", n.this),
            exp.TimestampTrunc: self.date_trunc,
            exp.DateTrunc: self.date_trunc,
            exp.DateDiff: self.date_diff,
            exp.TimestampFromParts: self.make_timestamp,
            exp.TimeToUnix: lambda n: exp.Div(
                this=_fn("UNIX_MICROS", _fn("TIMESTAMP", self._datetime(n.this))),
                expression=_num(1000000.0),
            ),
            exp.Monthname: lambda n: _names(_extract("MONTH", n.this), MONTH_NAMES),
            exp.Dayname: lambda n: _names(self._iso_dow(n.this), DAY_NAMES),
            exp.Pi: lambda n: self._pi(),
            exp.Rand: lambda n: _fn("RAND"),
            exp.Degrees: lambda n: exp.Paren(
                this=exp.Div(
                    this=exp.Mul(this=_paren(n.this), expression=_num(180)), expression=self._pi()
                )
            ),
            exp.Radians: lambda n: exp.Paren(
                this=exp.Div(
                    this=exp.Mul(this=_paren(n.this), expression=self._pi()), expression=_num(180)
                )
            ),
            exp.Left: self.left,
            exp.Right: self.right,
            exp.SplitPart: self.split_part,
            exp.Split: lambda n: _array(_fn("SPLIT", n.this, n.expression)),
            exp.Length: lambda n: _fn("ARRAY_LENGTH", n.this) if _is_array(n.this) else n,
            exp.RegexpExtract: self.regexp_extract,
            exp.RegexpReplace: self.regexp_replace,
            exp.Median: lambda n: self.percentile(n.this, _num(0.5)),
            exp.PercentileCont: lambda n: self.percentile(n.this, n.expression),
            exp.Window: self.window,
            exp.Div: self.divide,
        }

    def divide(self, node: exp.Expr) -> exp.Expr:
        """除法的運算元不能是常值 Null（BigQuery 無法決定型別）：轉成 FLOAT64 的 Null。"""
        for key in ("this", "expression"):
            operand = node.args.get(key)
            inner = operand
            while isinstance(inner, exp.Paren):
                inner = inner.this
            if isinstance(inner, exp.Null):
                node.set(key, _cast(exp.Null(), "FLOAT64", "bigquery"))
        return node

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: _extract("ISOYEAR", a[0]),
            "now": lambda a: self._datetime_result(_fn("CURRENT_DATETIME")),
            "try_strptime": self.parse_date,
            "list_slice": self.list_slice,
        }

    def _datetime(self, value: exp.Expr) -> exp.Expr:
        """要以 DATETIME 計算的值：轉成 DATETIME（已知是 DATETIME 的不再轉）。"""
        if value.meta.get("timestamp") or (
            isinstance(value, exp.Cast) and value.to.this in TIMESTAMP_TYPES
        ):
            return value
        return self._datetime_result(_cast(value, "DATETIME", "bigquery"))

    def _datetime_result(self, node: exp.Expr) -> exp.Expr:
        node.meta["timestamp"] = True
        return node

    def _pi(self) -> exp.Expr:
        return _fn("ACOS", _num(-1))

    def window(self, node: exp.Expr) -> exp.Expr:
        """視窗的排序不能是常值：ORDER BY NULL（每一列的編號，順序不拘）改成不排序。"""
        order = node.args.get("order")
        if isinstance(order, exp.Order) and all(
            isinstance(item, exp.Ordered) and isinstance(item.this, exp.Null)
            for item in order.expressions
        ):
            node.set("order", None)
        return node

    def _iso_dow(self, value: exp.Expr) -> exp.Expr:
        """星期一為 1、星期日為 7。"""
        shifted = _fn(
            "MOD", exp.Add(this=_extract("DAYOFWEEK", value), expression=_num(5)), _num(7)
        )
        return exp.Paren(this=exp.Add(this=shifted, expression=_num(1)))

    def date_trunc(self, node: exp.Expr) -> exp.Expr:
        unit = BIGQUERY_UNITS.get(_unit(node))
        if unit is None:
            raise DialectError(f"BigQuery 不支援截斷到 {_unit(node)}")
        return self._datetime_result(
            _fn("DATETIME_TRUNC", self._datetime(node.this), exp.Var(this=unit))
        )

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        if unit not in ("year", "quarter", "month", "day", "hour", "minute", "second"):
            raise DialectError(f"BigQuery 的 DATEDIFF 不支援單位 {unit}")
        return _fn(
            "DATETIME_DIFF",
            self._datetime(node.this),
            self._datetime(node.expression),
            exp.Var(this=BIGQUERY_UNITS[unit]),
        )

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        day = self._datetime_result(
            _fn(
                "DATETIME",
                _mark(
                    exp.DateFromParts(
                        year=_cast(y, "INT64", "bigquery"),
                        month=_cast(m, "INT64", "bigquery"),
                        day=_cast(d, "INT64", "bigquery"),
                    )
                ),
            )
        )
        seconds = exp.Add(
            this=exp.Add(
                this=exp.Mul(this=_paren(hh), expression=_num(3600)),
                expression=exp.Mul(this=_paren(mi), expression=_num(60)),
            ),
            expression=_paren(ss),
        )
        return self.add_interval(day, "second", seconds)

    def left(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        head = _mark(exp.Left(this=text.copy(), expression=count.copy()))
        if _not_negative(count):
            return head
        # 數量為負數時去掉右邊的字元（DuckDB 的 left）；BigQuery 的 LEFT 不接受負數
        trimmed = _mark(
            exp.Left(
                this=text.copy(),
                expression=_fn(
                    "GREATEST", exp.Add(this=exp.Length(this=text), expression=count), _num(0)
                ),
            )
        )
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count.copy(), expression=_num(0)), true=trimmed)],
            default=head,
        )

    def right(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        tail = _mark(exp.Right(this=text.copy(), expression=count.copy()))
        if _not_negative(count):
            return tail
        # 數量為負數時去掉左邊的字元（DuckDB 的 right）
        trimmed = _mark(
            exp.Right(
                this=text.copy(),
                expression=_fn(
                    "GREATEST", exp.Add(this=exp.Length(this=text), expression=count), _num(0)
                ),
            )
        )
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count.copy(), expression=_num(0)), true=trimmed)],
            default=tail,
        )

    def split_part(self, node: exp.Expr) -> exp.Expr:
        text, delimiter = node.this, node.args["delimiter"]
        index = node.args["part_index"]
        fixed = _literal_int(index)
        forward = "SPLIT({s}, {d})[SAFE_ORDINAL({i})]"
        backward = "ARRAY_REVERSE(SPLIT({s}, {d}))[SAFE_ORDINAL(-{i})]"
        if fixed is not None and fixed > 0:
            part = _template(forward, "bigquery", s=text, d=delimiter, i=index)
        elif fixed is not None and fixed < 0:
            part = _template(backward, "bigquery", s=text, d=delimiter, i=index)
        else:
            part = _template(
                f"CASE WHEN {{i}} > 0 THEN {forward} WHEN {{i}} < 0 THEN {backward} END",
                "bigquery",
                s=text,
                d=delimiter,
                i=index,
            )
        known = exp.And(
            this=exp.And(
                this=exp.Not(this=exp.Is(this=text, expression=exp.Null())),
                expression=exp.Not(this=exp.Is(this=delimiter, expression=exp.Null())),
            ),
            expression=exp.Not(this=exp.Is(this=index, expression=exp.Null())),
        )
        return exp.Coalesce(
            this=part, expressions=[exp.Case(ifs=[exp.If(this=known, true=_str(""))])]
        )

    def list_slice(self, args: list[exp.Expr]) -> exp.Expr:
        array, start, end = args
        if not _is_array(array):
            raise DialectError("list_slice 只支援 FINDNTH 的寫法")
        # UNNEST(Null) 沒有列、ARRAY(…) 得空陣列：Null 的陣列保持 Null（與 DuckDB 的 list_slice 相同）
        sliced = _template(
            "IF({a} IS NULL, NULL, ARRAY(SELECT tmig_e FROM UNNEST({a}) AS tmig_e "
            "WITH OFFSET AS tmig_o WHERE tmig_o BETWEEN {s} - 1 AND {e} - 1 ORDER BY tmig_o))",
            "bigquery",
            a=array,
            s=start,
            e=end,
        )
        return _array(sliced)

    def regexp_extract(self, node: exp.Expr) -> exp.Expr:
        pattern = node.expression
        group = _literal_int(node.args.get("group"))
        if not (isinstance(pattern, exp.Literal) and pattern.is_string) or group is None:
            raise DialectError("BigQuery 的 REGEXP_EXTRACT 樣式與群組必須是常值")
        if group > _capture_groups(pattern.name):
            raise DialectError(f"REGEXP_EXTRACT 的樣式沒有第 {group} 個群組")
        return _fn("REGEXP_EXTRACT", node.this, _str(_keep_group(pattern.name, group)))

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        # RE2 的取代字串（\1）與 DuckDB 相同；BigQuery 一律取代所有符合的地方
        return _fn(
            "REGEXP_REPLACE", node.this, node.expression, node.args.get("replacement") or _str("")
        )

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：SAFE.PARSE_DATETIME 不符合格式時為 Null（秒的小數 %S.%f 寫成 %E*S）。"""
        text = _format_literal(args[1])
        if "%f" in text.replace("%S.%f", ""):
            raise DialectError("BigQuery 的 DATEPARSE 只支援接在秒後面的小數")
        parts = text.split("%S.%f")
        fmt = "%E*S".join(
            _convert_format(p, BIGQUERY_FORMAT, lambda s: s.replace("%", "%%")) for p in parts
        )
        return self._datetime_result(_fn("SAFE.PARSE_DATETIME", _str(fmt), args[0]))

    def percentile(self, value: exp.Expr, fraction: exp.Expr) -> exp.Expr:
        return _template(BIGQUERY_PERCENTILE, "bigquery", x=value, p=fraction)


# ---------------------------------------------------------------- Apache Hive、Impala


HIVE_PERCENTILE = (
    "SORT_ARRAY(COLLECT_LIST({x}))[CAST(FLOOR({pos}) AS INT)] "
    "+ ({pos} - FLOOR({pos})) "
    "* (SORT_ARRAY(COLLECT_LIST({x}))[CAST(CEIL({pos}) AS INT)] "
    "- SORT_ARRAY(COLLECT_LIST({x}))[CAST(FLOOR({pos}) AS INT)])"
).replace("{pos}", "(CAST({p} AS DOUBLE) * (SIZE(COLLECT_LIST({x})) - 1))")
"""精確的百分位數（與 DuckDB 的 quantile_cont 相同的線性內插）：Hive 的 percentile 只接受整數、
percentile_approx 是近似值；不是 Null 的值排序成陣列（0 起算）取前後兩個值內插。"""


class HiveRewriter(Rewriter):
    """Apache Hive 2.3 以上（HiveServer2；Cloudera、Hortonworks、MapR、Amazon EMR 的 Hive）。
    依官方文件撰寫，尚未實機驗證（測試以 SQLGlot 轉回 DuckDB 模擬執行）。

    - 星期、ISO 年以與 1900-01-01（星期一）相隔的天數計算（DATEDIFF、PMOD）；週數為 WEEKOFYEAR（ISO）
    - 日期加減：時間單位以秒數加減（TIMESTAMP 與 DOUBLE 互轉），月份以月初的 ADD_MONTHS 加上日數再取月底
      （Hive 的 ADD_MONTHS 遇到月底時取月底，與 DuckDB 不同）
    - 截斷以 TRUNC（月、年）、TO_DATE（日）與 DATE_FORMAT（時、分、秒）
    - 沒有 ATAN2、數字的 TRUNC、陣列的片段：ATAN2 以 ATAN 與象限計算，FINDNTH 列為不支援
    - 字串串接以 CONCAT；SPLIT 的分隔字元是正規表示式（以 \\Q…\\E 包住）；正規表示式為 Java 的寫法
    - VARIANCE 是母體變異數：樣本變異數為 VAR_SAMP；MEDIAN、PERCENTILE 以排序後的 COLLECT_LIST 內插
    - 轉型失敗時為 Null（TRY_CAST 即 CAST）；GROUP BY 不以位置分組；子查詢只能在條件中
    - 連線時把工作階段的時區設為 UTC（hive.local.time.zone）
    """

    name = "Hive"
    dialect = "hive"
    icu_regex = ICU_ALL  # Java 的正規表示式
    allowed = frozenset(
        {
            exp.Year,
            exp.Quarter,
            exp.Month,
            exp.Day,
            exp.Pi,
            exp.Rand,
            exp.Degrees,
            exp.Radians,
            exp.Log,
            exp.Round,
            exp.Repeat,
            exp.VariancePop,
            exp.IntDiv,
            exp.Mod,
            exp.NullSafeEQ,
            exp.CurrentDate,
            exp.Boolean,
            exp.RegexpExtract,
        }
    )

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit in ("year", "month"):
            months = (
                amount if unit == "month" else exp.Mul(this=_paren(amount), expression=_num(12))
            )
            return self._add_months(value, months)
        # 秒數以 DOUBLE 計算：INT 相乘會溢位（Hive 不報錯）
        factor = {
            "day": 86400.0,
            "hour": 3600.0,
            "minute": 60.0,
            "second": 1.0,
            "microsecond": 0.000001,
        }[unit]
        seconds = exp.Mul(this=_paren(amount), expression=_num(factor))
        return self._seconds_later(value, seconds)

    def _timestamp(self, value: exp.Expr) -> exp.Expr:
        if isinstance(value, exp.Cast) and value.to.this in TIMESTAMP_TYPES:
            return value
        return _cast(value, "TIMESTAMP")

    def _epoch(self, value: exp.Expr) -> exp.Expr:
        """自 1970-01-01 起的秒數（含小數）。"""
        return _cast(self._timestamp(value), "DOUBLE")

    def _seconds_later(self, value: exp.Expr, seconds: exp.Expr) -> exp.Expr:
        moved = exp.Add(this=self._epoch(value), expression=_paren(seconds))
        return _cast(moved, "TIMESTAMP")

    def _add_months(self, value: exp.Expr, months: exp.Expr) -> exp.Expr:
        """月初加上月數再加回日數，超過月底時取月底；一天中的時間保留。"""
        first = _fn("ADD_MONTHS", _fn("TRUNC", value.copy(), _str("MM")), _cast(months, "INT"))
        day = _cast(
            _fn(
                "DATE_ADD",
                first.copy(),
                exp.Sub(this=exp.Day(this=value.copy()), expression=_num(1)),
            ),
            "DATE",
        )
        last = _cast(_fn("LAST_DAY", first), "DATE")
        moved = _fn("LEAST", day, last)
        midnight = _cast(_fn("TO_DATE", value.copy()), "TIMESTAMP")
        time_of_day = exp.Sub(this=self._epoch(value), expression=self._epoch(midnight))
        return self._seconds_later(_cast(moved, "TIMESTAMP"), time_of_day)

    def _days_since(self, value: exp.Expr, origin: str) -> exp.Expr:
        return _fn("DATEDIFF", _fn("TO_DATE", value), _str(origin))

    def _weekday(self, value: exp.Expr, origin: str) -> exp.Expr:
        return _fn("PMOD", self._days_since(value, origin), _num(7))

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def time_part(name: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: _fn(name, self._timestamp(n.this))

        return {
            exp.Hour: time_part("HOUR"),
            exp.Minute: time_part("MINUTE"),
            exp.Second: time_part("SECOND"),
            exp.DayOfYear: lambda n: exp.Paren(
                this=exp.Add(
                    this=_fn(
                        "DATEDIFF",
                        _fn("TO_DATE", n.this.copy()),
                        _fn("TRUNC", n.this, _str("YYYY")),
                    ),
                    expression=_num(1),
                )
            ),
            exp.Week: lambda n: _fn("WEEKOFYEAR", n.this),
            exp.DayOfWeek: lambda n: self._weekday(n.this, "1900-01-07"),
            exp.DayOfWeekIso: lambda n: exp.Paren(
                this=exp.Add(this=self._weekday(n.this, "1900-01-01"), expression=_num(1))
            ),
            exp.Monthname: lambda n: _names(exp.Month(this=n.this), MONTH_NAMES),
            exp.Dayname: lambda n: _names(
                exp.Paren(
                    this=exp.Add(this=self._weekday(n.this, "1900-01-01"), expression=_num(1))
                ),
                DAY_NAMES,
            ),
            exp.TimestampTrunc: self.date_trunc,
            exp.DateTrunc: self.date_trunc,
            exp.DateDiff: self.date_diff,
            exp.DateFromParts: lambda n: self.make_date(
                n.args["year"], n.args["month"], n.args["day"]
            ),
            exp.TimestampFromParts: self.make_timestamp,
            exp.TimeToUnix: lambda n: self._epoch(n.this),
            exp.Atan2: self.atan2,
            exp.Trunc: lambda n: exp.Case(
                ifs=[
                    exp.If(
                        this=exp.LT(this=n.this.copy(), expression=_num(0)),
                        true=exp.Ceil(this=n.this.copy()),
                    )
                ],
                default=exp.Floor(this=n.this),
            ),
            exp.Unicode: lambda n: _fn("ASCII", n.this),
            exp.DPipe: lambda n: _fn("CONCAT", n.this, n.expression),
            exp.Left: self.left,
            exp.Right: self.right,
            exp.SplitPart: self.split_part,
            exp.Split: lambda n: _array(_fn("SPLIT", n.this, self._literal_regex(n.expression))),
            exp.Length: lambda n: _fn("SIZE", n.this) if _is_array(n.this) else n,
            exp.RegexpReplace: self.regexp_replace,
            exp.Variance: lambda n: _fn("VAR_SAMP", n.this),
            exp.TryCast: lambda n: _mark(exp.Cast(this=n.this, to=n.args["to"])),
            exp.Median: lambda n: self.percentile(n.this, _num(0.5)),
            exp.PercentileCont: lambda n: self.percentile(n.this, n.expression),
            exp.DataType: self.datatype,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: exp.Year(this=self._thursday(a[0])),
            "now": lambda a: exp.Var(this="CURRENT_TIMESTAMP"),
            "try_strptime": self.parse_date,
            "list_slice": lambda a: self.no_arrays(),
        }

    def no_arrays(self) -> exp.Expr:
        raise DialectError(f"{self.name} 沒有取陣列片段的函式（FINDNTH）")

    def _thursday(self, value: exp.Expr) -> exp.Expr:
        return _fn(
            "DATE_ADD",
            _fn("TO_DATE", value.copy()),
            exp.Sub(this=_num(3), expression=self._weekday(value, "1900-01-01")),
        )

    def datatype(self, node: exp.Expr) -> exp.Expr:
        # DuckDB 的 DECIMAL 預設為 (18, 3)；Hive 為 (10, 0)
        decimal = isinstance(node, exp.DataType) and node.this == exp.DataType.Type.DECIMAL
        if decimal and not node.expressions:
            return _type("DECIMAL(18, 3)")
        return node

    def date_trunc(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        value = node.this
        if unit == "day":
            return _cast(_fn("TO_DATE", value), "TIMESTAMP")
        if unit in ("month", "year"):
            return _cast(
                _fn("TRUNC", value, _str("MM" if unit == "month" else "YYYY")), "TIMESTAMP"
            )
        if unit == "quarter":
            months = exp.Mul(
                this=exp.Paren(
                    this=exp.Sub(this=exp.Quarter(this=value.copy()), expression=_num(1))
                ),
                expression=_num(3),
            )
            return _cast(_fn("ADD_MONTHS", _fn("TRUNC", value, _str("YYYY")), months), "TIMESTAMP")
        formats = {
            "hour": "yyyy-MM-dd HH:00:00",
            "minute": "yyyy-MM-dd HH:mm:00",
            "second": "yyyy-MM-dd HH:mm:ss",
        }
        if unit in formats:
            return _cast(
                _fn("DATE_FORMAT", self._timestamp(value), _str(formats[unit])), "TIMESTAMP"
            )
        raise DialectError(f"{self.name} 的日期截斷不支援單位 {unit}")

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        def part(field: str, value: exp.Expr) -> exp.Expr:
            if field in ("hour", "minute", "second"):
                return _fn(field.upper(), self._timestamp(value))
            return {"year": exp.Year, "quarter": exp.Quarter, "month": exp.Month}[field](this=value)

        return self.boundary_diff(_unit(node), node.expression, node.this, part, self._day_diff)

    def _day_diff(self, start: exp.Expr, end: exp.Expr) -> exp.Expr:
        return _fn("DATEDIFF", _fn("TO_DATE", end), _fn("TO_DATE", start))

    def make_date(self, y: exp.Expr, m: exp.Expr, d: exp.Expr) -> exp.Expr:
        def pad(value: exp.Expr, width: int) -> exp.Expr:
            return _fn("LPAD", _cast(value, "STRING", "hive"), _num(width), _str("0"))

        text = _fn("CONCAT", pad(y, 4), _str("-"), pad(m, 2), _str("-"), pad(d, 2))
        return _cast(text, "DATE")

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        seconds = exp.Add(
            this=exp.Add(
                this=exp.Mul(this=_paren(hh), expression=_num(3600.0)),
                expression=exp.Mul(this=_paren(mi), expression=_num(60.0)),
            ),
            expression=_paren(ss),
        )
        return self._seconds_later(_cast(self.make_date(y, m, d), "TIMESTAMP"), seconds)

    def atan2(self, node: exp.Expr) -> exp.Expr:
        """ATAN2(y, x)：以 ATAN(y / x) 依象限調整。"""
        y, x = node.this, node.expression
        base = exp.Atan(this=exp.Div(this=_paren(y.copy()), expression=_paren(x.copy())))
        pi = exp.Pi()

        def when(cond: exp.Expr, value: exp.Expr) -> exp.If:
            return exp.If(this=cond, true=value)

        zero = _num(0)
        return exp.Case(
            ifs=[
                when(exp.GT(this=x.copy(), expression=zero.copy()), base.copy()),
                when(
                    exp.And(
                        this=exp.LT(this=x.copy(), expression=zero.copy()),
                        expression=exp.GTE(this=y.copy(), expression=zero.copy()),
                    ),
                    exp.Add(this=base.copy(), expression=pi.copy()),
                ),
                when(
                    exp.And(
                        this=exp.LT(this=x.copy(), expression=zero.copy()),
                        expression=exp.LT(this=y.copy(), expression=zero.copy()),
                    ),
                    exp.Sub(this=base.copy(), expression=pi.copy()),
                ),
                when(
                    exp.And(
                        this=exp.EQ(this=x.copy(), expression=zero.copy()),
                        expression=exp.GT(this=y.copy(), expression=zero.copy()),
                    ),
                    exp.Div(this=pi.copy(), expression=_num(2)),
                ),
                when(
                    exp.And(
                        this=exp.EQ(this=x.copy(), expression=zero.copy()),
                        expression=exp.LT(this=y.copy(), expression=zero.copy()),
                    ),
                    exp.Neg(this=exp.Paren(this=exp.Div(this=pi.copy(), expression=_num(2)))),
                ),
                when(
                    exp.And(
                        this=exp.EQ(this=x, expression=zero.copy()),
                        expression=exp.EQ(this=y, expression=zero.copy()),
                    ),
                    _num(0.0),
                ),
            ]
        )

    def left(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        head = _fn("SUBSTR", text.copy(), _num(1), count.copy())
        if _not_negative(count):
            return head
        trimmed = _fn(
            "SUBSTR",
            text.copy(),
            _num(1),
            _fn("GREATEST", exp.Add(this=exp.Length(this=text), expression=count.copy()), _num(0)),
        )
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count.copy(), expression=_num(0)), true=trimmed)],
            default=head,
        )

    def right(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        start = exp.Add(
            this=exp.Sub(this=exp.Length(this=text.copy()), expression=_paren(count.copy())),
            expression=_num(1),
        )
        tail = _fn("SUBSTR", text.copy(), _fn("GREATEST", start, _num(1)))
        if _not_negative(count):
            return tail
        trimmed = _fn("SUBSTR", text, exp.Sub(this=_num(1), expression=_paren(count.copy())))
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count, expression=_num(0)), true=trimmed)],
            default=tail,
        )

    def _literal_regex(self, delimiter: exp.Expr) -> exp.Expr:
        """SPLIT 的分隔字元是正規表示式：以 \\Q…\\E 包住（照字面比對）。"""
        return _fn("CONCAT", _str("\\Q"), delimiter, _str("\\E"))

    def split_part(self, node: exp.Expr) -> exp.Expr:
        text, delimiter = node.this, node.args["delimiter"]
        index = node.args["part_index"]
        fixed = _literal_int(index)
        if fixed is None or fixed == 0:
            raise DialectError(f"{self.name} 的 SPLIT 只支援非 0 的常值位置")
        parts = _fn("SPLIT", text.copy(), self._literal_regex(delimiter.copy()))
        if fixed > 0:
            position: exp.Expr = _num(fixed - 1)
        else:
            position = exp.Add(this=_fn("SIZE", parts.copy()), expression=_num(fixed))
        part = _mark(exp.Bracket(this=parts, expressions=[position], offset=0, safe=True))
        known = exp.And(
            this=exp.Not(this=exp.Is(this=text, expression=exp.Null())),
            expression=exp.Not(this=exp.Is(this=delimiter, expression=exp.Null())),
        )
        return exp.Coalesce(
            this=part, expressions=[exp.Case(ifs=[exp.If(this=known, true=_str(""))])]
        )

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        replacement = node.args.get("replacement") or _str("")
        if not (isinstance(replacement, exp.Literal) and replacement.is_string):
            raise DialectError("REGEXP_REPLACE 的取代字串不是常值")
        return _fn(
            "REGEXP_REPLACE", node.this, node.expression, _str(java_replacement(replacement.name))
        )

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：UNIX_TIMESTAMP 不符合格式時為 Null（精確到秒）。"""
        if "%f" in _format_literal(args[1]):
            raise DialectError(f"{self.name} 的 DATEPARSE 不支援秒的小數")
        fmt = _convert_format(_format_literal(args[1]), SPARK_FORMAT, _single_quote)
        seconds = _fn("UNIX_TIMESTAMP", args[0], _str(fmt))
        return _cast(_fn("FROM_UNIXTIME", seconds, _str("yyyy-MM-dd HH:mm:ss")), "TIMESTAMP")

    def percentile(self, value: exp.Expr, fraction: exp.Expr) -> exp.Expr:
        return _template(HIVE_PERCENTILE, "hive", x=value, p=fraction)

    def finish(self, tree: exp.Expr) -> None:
        for select in list(tree.find_all(exp.Select)):
            _expand_group_ordinals(select)
        for node in list(tree.find_all(exp.Exists)):
            if node.root() is tree and not _condition_position(node):
                raise DialectError(f"{self.name} 的子查詢只能用在條件中（例如集合的成員）")


class ImpalaRewriter(HiveRewriter):
    """Apache Impala 3.3 以上（Cloudera）。依官方文件撰寫，尚未實機驗證（測試以 SQLGlot 轉回 DuckDB 模擬執行）。

    與 Hive 相同的規則，差異：

    - 時間的加減以 DAYS_ADD、MONTHS_ADD、MICROSECONDS_ADD 等函式（保留一天中的時間）；月份的加減從月初加起，
      再取不超過月底的日（Impala 的 MONTHS_ADD 遇到月底時取月底，與 DuckDB 不同）
    - 截斷以 TRUNC（年、季、月、日、時、分）與 DATE_TRUNC（秒）；沒有 DATE_FORMAT
    - 自 1970-01-01 起的秒數以 UNIX_TIMESTAMP（UTC）加上毫秒；今天為 CURRENT_DATE()、現在為 NOW()
    - 正規表示式是 RE2（取代字串的群組參照與 DuckDB 相同是 \\1）；REGEXP_EXTRACT 一定要給群組
    - 有 ATAN2、SPLIT_PART、TRUNCATE、DAYOFYEAR；沒有陣列、精確的 MEDIAN、PERCENTILE 與 CORR、COVAR（列為不支援）
    - DATEPARSE 的格式不支援月份名稱、上下午與含英文字母的文字（格式中的其他文字照字面比對，不加引號）
    - LENGTH 計算位元組（非 ASCII 字元與 Tableau 不同）
    - 連線時把工作階段的時區設為 UTC（TIMEZONE 查詢選項）
    """

    name = "Impala"
    dialect = "impala"
    icu_regex: frozenset[str] = frozenset()  # RE2

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit in ("year", "month"):
            return super().add_interval(value, unit, amount)
        if unit in ("second", "microsecond"):
            factor = 1000000 if unit == "second" else 1
            micros = _cast(
                _fn("ROUND", exp.Mul(this=_paren(amount), expression=_num(factor))), "BIGINT"
            )
            return _fn("MICROSECONDS_ADD", self._timestamp(value), micros)
        name = {"day": "DAYS_ADD", "hour": "HOURS_ADD", "minute": "MINUTES_ADD"}[unit]
        return _fn(name, self._timestamp(value), _cast(amount, "BIGINT"))

    def _seconds_later(self, value: exp.Expr, seconds: exp.Expr) -> exp.Expr:
        return self.add_interval(value, "second", seconds)

    def _add_months(self, value: exp.Expr, months: exp.Expr) -> exp.Expr:
        """同一時間的月初加上月數，再加回日數（不超過該月的最後一天）。"""
        ts = self._timestamp(value)
        back = _cast(exp.Sub(this=exp.Day(this=ts.copy()), expression=_num(1)), "BIGINT")
        first = _fn("MONTHS_ADD", _fn("DAYS_SUB", ts.copy(), back), _cast(months, "INT"))
        day = _fn("LEAST", exp.Day(this=ts), exp.Day(this=_fn("LAST_DAY", first.copy())))
        return _fn("DAYS_ADD", first, _cast(exp.Sub(this=day, expression=_num(1)), "BIGINT"))

    def _epoch(self, value: exp.Expr) -> exp.Expr:
        """自 1970-01-01 起的秒數：UNIX_TIMESTAMP 是整數秒（UTC），加上毫秒。"""
        ts = self._timestamp(value)
        millis = exp.Div(this=_fn("MILLISECOND", ts.copy()), expression=_num(1000.0))
        return exp.Paren(this=exp.Add(this=_fn("UNIX_TIMESTAMP", ts), expression=millis))

    def _days_since(self, value: exp.Expr, origin: str) -> exp.Expr:
        # DATEDIFF 不計一天中的時間
        return _fn("DATEDIFF", self._timestamp(value), _cast(_str(origin), "TIMESTAMP"))

    def _day_diff(self, start: exp.Expr, end: exp.Expr) -> exp.Expr:
        return _fn("DATEDIFF", self._timestamp(end), self._timestamp(start))

    def _thursday(self, value: exp.Expr) -> exp.Expr:
        offset = _cast(
            exp.Sub(this=_num(3), expression=self._weekday(value.copy(), "1900-01-01")), "BIGINT"
        )
        return _fn("DAYS_ADD", _fn("TRUNC", self._timestamp(value), _str("DD")), offset)

    def date_trunc(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        value = self._timestamp(node.this)
        codes = {
            "year": "YYYY",
            "quarter": "Q",
            "month": "MM",
            "day": "DD",
            "hour": "HH",
            "minute": "MI",
        }
        if unit in codes:
            return _fn("TRUNC", value, _str(codes[unit]))
        if unit == "second":
            return _fn("DATE_TRUNC", _str("SECOND"), value)
        raise DialectError(f"Impala 的日期截斷不支援單位 {unit}")

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        return {
            **super().handlers(),
            exp.Atan2: lambda n: _mark(n),
            exp.Trunc: lambda n: _fn("TRUNCATE", n.this),
            exp.DayOfYear: lambda n: _fn("DAYOFYEAR", self._timestamp(n.this)),
            exp.SplitPart: self.split_part,
            exp.Split: lambda n: self.no_arrays(),
            exp.RegexpReplace: lambda n: _fn(
                "REGEXP_REPLACE", n.this, n.expression, n.args.get("replacement") or _str("")
            ),
            exp.RegexpExtract: lambda n: _fn(
                "REGEXP_EXTRACT", n.this, n.expression, n.args.get("group") or _num(0)
            ),
            exp.CurrentDate: lambda n: _fn("CURRENT_DATE"),
            exp.Median: self.no_statistics,
            exp.PercentileCont: self.no_statistics,
            exp.Corr: self.no_correlation,
            exp.CovarSamp: self.no_correlation,
            exp.CovarPop: self.no_correlation,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {**super().functions(), "now": lambda a: _fn("NOW")}

    def no_statistics(self, node: exp.Expr) -> exp.Expr:
        raise DialectError("Impala 沒有精確的 MEDIAN、PERCENTILE（只有近似的 APPX_MEDIAN）")

    def no_correlation(self, node: exp.Expr) -> exp.Expr:
        raise DialectError("Impala 沒有 CORR、COVAR、COVARP")

    def split_part(self, node: exp.Expr) -> exp.Expr:
        index = _literal_int(node.args["part_index"])
        if index is None or index < 1:
            raise DialectError("Impala 的 SPLIT 只支援正整數常值的位置")
        return _fn(
            "SPLIT_PART",
            node.this,
            node.args["delimiter"],
            _cast(node.args["part_index"], "BIGINT"),
        )

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：TO_TIMESTAMP 不符合格式時為 Null（格式中的文字照字面比對，不能含英文字母）。"""

        def literal(text: str) -> str:
            if re.search(r"[A-Za-z']", text):
                raise DialectError(f"Impala 的 DATEPARSE 格式不能含英文字母或引號的文字：{text}")
            return text

        fmt = _convert_format(_format_literal(args[1]), IMPALA_FORMAT, literal)
        return _fn("TO_TIMESTAMP", args[0], _str(fmt))


IMPALA_FORMAT = {
    "%Y": "yyyy",
    "%y": "yy",
    "%m": "MM",
    "%d": "dd",
    "%H": "HH",
    "%M": "mm",
    "%S": "ss",
    "%f": "SSSSSS",
}
"""strftime 的格式 → Impala 的日期格式（不支援月份名稱與上下午）。"""


# ---------------------------------------------------------------- Teradata


TERADATA_TEXT = "VARCHAR(4000)"
"""轉成字串時的型別；產生 SQL 後補上 CHARACTER SET UNICODE（見 TeradataRewriter.text）。"""
TERADATA_MONDAY = "DATE '1900-01-01'"
TERADATA_SUNDAY = "DATE '1900-01-07'"
"""與這兩天相隔的天數除以 7 的餘數就是星期（不受系統的週起始日設定影響）。"""
TERADATA_TRUNC = {"year": "YYYY", "quarter": "Q", "month": "MM", "day": "DD"}


class TeradataRewriter(OracleRewriter):
    """Teradata Vantage（16.20 以上）：依官方文件撰寫，尚未實機驗證（測試以 SQLGlot 轉回 DuckDB 模擬執行）。

    Teradata 14 起有與 Oracle 相容的函式（TO_CHAR、TO_TIMESTAMP、ADD_MONTHS、NUMTODSINTERVAL、DECODE、
    INSTR、RPAD、REGEXP_*），沿用 Oracle 的規則，差異如下：

    - 沒有布林型別：查詢結果中的布林值（布林維度、集合的成員）列為不支援（與 Oracle 相同）
    - DATE 沒有時間；日期加減一律轉成 TIMESTAMP(6) 計算（DATE 只能加減天數，NUMTODSINTERVAL 最多 9999 天：
      天數以 DATE 加減，一天內的時間另外以秒加上）
    - 星期、ISO 週與年以與 1900-01-01（星期一）相隔的天數計算，不受系統的週起始日設定影響
    - SUBSTR 的負數起點不是從結尾算起；字串長度為 CHARACTER_LENGTH；REPLACE 為 OREPLACE
    - REGEXP_SUBSTR 沒有群組參數：取群組時先取整段符合的文字，再以錨定的樣式換成該群組
    - LIMIT 在子查詢與 CTE 中改成 QUALIFY ROW_NUMBER()；ATAN2 的參數順序是 (x, y)
    - TRYCAST（16.20 起）只接受字串；文字轉時間依序試 TIMESTAMP(6)、TIMESTAMP(0)、DATE
    - 連線時把工作階段的時區設為 UTC、日期格式設為 ANSIDATE（YYYY-MM-DD）
    """

    name = "Teradata"
    dialect = "teradata"
    allowed = frozenset({exp.Median, exp.Mod, exp.IntDiv})

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit in ("year", "month"):
            months = (
                amount if unit == "month" else exp.Mul(this=_paren(amount), expression=_num(12))
            )
            return _fn("ADD_MONTHS", value, _cast(months, "INTEGER"))
        if unit == "day":
            # DATE + 整數天數（沒有 9999 天的上限），再加上原本一天中的時間（確定是日期的不加）
            moved = exp.Add(this=self._date(value.copy()), expression=_cast(amount, "INTEGER"))
            day = _cast(moved, "TIMESTAMP(6)", "teradata")
            if _known_date(value):
                return day
            time = self._interval(self._seconds_of_day(value), "SECOND")
            return exp.Paren(this=exp.Add(this=day, expression=time))
        if unit == "microsecond":
            unit, amount = "second", exp.Div(this=_paren(amount), expression=_num(1000000.0))
        return exp.Paren(
            this=exp.Add(
                this=self._timestamp(value), expression=self._interval(amount, unit.upper())
            )
        )

    def _interval(self, amount: exp.Expr, unit: str) -> exp.Expr:
        return _fn("NUMTODSINTERVAL", amount, _str(unit))

    def _timestamp(self, value: exp.Expr) -> exp.Expr:
        if isinstance(value, exp.Cast) and value.to.this in TIMESTAMP_TYPES:
            return value
        return _cast(value, "TIMESTAMP(6)", "teradata")

    def _seconds_of_day(self, value: exp.Expr) -> exp.Expr:
        ts = self._timestamp(value)
        return exp.Add(
            this=exp.Add(
                this=exp.Mul(this=_extract("HOUR", ts.copy()), expression=_num(3600)),
                expression=exp.Mul(this=_extract("MINUTE", ts.copy()), expression=_num(60)),
            ),
            expression=_extract("SECOND", ts),
        )

    def _date(self, value: exp.Expr) -> exp.Expr:
        return (
            value
            if isinstance(value, exp.Cast) and value.to.this == exp.DataType.Type.DATE
            else _cast(value, "DATE")
        )

    def _days_since(self, value: exp.Expr, origin: str) -> exp.Expr:
        return exp.Paren(this=exp.Sub(this=self._date(value), expression=exp.Var(this=origin)))

    def _weekday(self, value: exp.Expr, origin: str) -> exp.Expr:
        """與基準日相隔的天數除以 7 的餘數（負數時補成正數）。"""
        mod = exp.Mod(this=self._days_since(value, origin), expression=_num(7))
        return exp.Paren(
            this=exp.Mod(
                this=exp.Paren(this=exp.Add(this=mod, expression=_num(7))), expression=_num(7)
            )
        )

    def _thursday(self, value: exp.Expr) -> exp.Expr:
        """ISO 週的星期四（決定 ISO 年與週數）。"""
        weekday = _cast(self._weekday(value, TERADATA_MONDAY), "INTEGER")
        return exp.Paren(
            this=exp.Add(
                this=exp.Sub(this=self._date(value.copy()), expression=weekday),
                expression=_num(3),
            )
        )

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        base = super().handlers()

        def time_part(field: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: _extract(field, self._timestamp(n.this))

        def iso_week(value: exp.Expr) -> exp.Expr:
            thursday = self._thursday(value)
            year_start = _fn("TRUNC", thursday.copy(), _str("YYYY"))
            weeks = _int_div(exp.Paren(this=exp.Sub(this=thursday, expression=year_start)), 7)
            return exp.Paren(this=exp.Add(this=weeks, expression=_num(1)))

        return {
            **base,
            exp.Rand: self._random,
            exp.Quarter: lambda n: _quarter(n.this),
            exp.Hour: time_part("HOUR"),
            exp.Minute: time_part("MINUTE"),
            exp.Second: lambda n: _cast(
                _fn("FLOOR", _extract("SECOND", self._timestamp(n.this))), "INTEGER"
            ),
            exp.DayOfYear: lambda n: exp.Paren(
                this=exp.Add(
                    this=exp.Sub(
                        this=self._date(n.this.copy()),
                        expression=_fn("TRUNC", self._date(n.this), _str("YYYY")),
                    ),
                    expression=_num(1),
                )
            ),
            exp.Week: lambda n: iso_week(n.this),
            exp.DayOfWeek: lambda n: self._weekday(n.this, TERADATA_SUNDAY),
            exp.DayOfWeekIso: lambda n: exp.Paren(
                this=exp.Add(this=self._weekday(n.this, TERADATA_MONDAY), expression=_num(1))
            ),
            exp.Monthname: lambda n: _names(_extract("MONTH", n.this), MONTH_NAMES),
            exp.Dayname: lambda n: _names(
                exp.Paren(
                    this=exp.Add(this=self._weekday(n.this, TERADATA_MONDAY), expression=_num(1))
                ),
                DAY_NAMES,
            ),
            exp.Atan2: lambda n: _fn("ATAN2", n.expression, n.this),
            # ** 的優先順序高於加減：底數與指數一律加括號（SQLGlot 不會補）
            exp.Pow: lambda n: _mark(
                exp.Pow(this=exp.Paren(this=n.this), expression=exp.Paren(this=n.expression))
            ),
            exp.Length: lambda n: _fn("CHARACTER_LENGTH", n.this),
            exp.Replace: lambda n: _fn(
                "OREPLACE", n.this, n.expression, n.args.get("replacement") or _str("")
            ),
            exp.Left: self.left,
            exp.Right: self.right,
            exp.Repeat: lambda n: _fn(
                "RPAD",
                n.this.copy(),
                exp.Mul(
                    this=_fn("CHARACTER_LENGTH", n.this.copy()), expression=_paren(n.args["times"])
                ),
                n.this,
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
            exp.CurrentDate: lambda n: exp.Var(this="CURRENT_DATE"),
            exp.DPipe: lambda n: _mark(n),
            # NUMBER 會轉成 DECIMAL（預設 (5, 0)）：以 DECIMAL(38, 10) 計算
            exp.Round: lambda n: _cast(
                _fn(
                    "ROUND",
                    _cast(n.this, "DECIMAL(38, 10)", "teradata"),
                    _cast(n.args.get("decimals") or _num(0), "INTEGER"),
                ),
                "FLOAT",
                "teradata",
            ),
            # 整數除法：轉成浮點數相除後向 0 截斷（不依賴浮點數轉整數時的捨入）
            exp.IntDiv: lambda n: _cast(
                _fn(
                    "TRUNC",
                    exp.Div(
                        this=_cast(n.this, "FLOAT", "teradata"), expression=_paren(n.expression)
                    ),
                ),
                "BIGINT",
            ),
            # Null 的排序與 Teradata 預設相同（最小），不另外寫 NULLS FIRST
            exp.PercentileCont: lambda n: _mark(
                exp.WithinGroup(
                    this=_fn("PERCENTILE_CONT", n.expression),
                    expression=_mark(
                        exp.Order(expressions=[_mark(exp.Ordered(this=n.this, nulls_first=True))])
                    ),
                )
            ),
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            **super().functions(),
            "isoyear": lambda a: _extract("YEAR", self._thursday(a[0])),
            "now": lambda a: _cast(
                exp.Var(this="CURRENT_TIMESTAMP(6)"), "TIMESTAMP(6)", "teradata"
            ),
        }

    def _random(self, node: exp.Expr) -> exp.Expr:
        """[0, 1) 的隨機數：Teradata 的 RANDOM(下限, 上限) 傳回整數，且不能放在彙總函式或視窗函式中。"""
        if node.find_ancestor(exp.AggFunc, exp.Window) is not None:
            raise DialectError("Teradata 的 RANDOM 不能放在彙總函式中")
        value = _cast(_fn("RANDOM", _num(0), _num(2147483646)), "DOUBLE")
        return exp.Paren(this=exp.Div(this=value, expression=_num(2147483647)))

    def log(self, node: exp.Expr) -> exp.Expr:
        if node.expression is None or _literal_int(node.this) == 10:
            return _fn("LOG", node.expression or node.this)  # Teradata 的 LOG 以 10 為底
        return exp.Div(this=exp.Ln(this=node.expression), expression=exp.Ln(this=node.this))

    def trunc(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        if unit in TERADATA_TRUNC:
            # TRUNC 只接受 DATE：截斷後轉回時間（與 DuckDB 的 date_trunc 相同是時間）
            return _cast(
                _fn("TRUNC", self._date(node.this), _str(TERADATA_TRUNC[unit])),
                "TIMESTAMP(6)",
                "teradata",
            )
        if unit in ("hour", "minute", "second"):
            ts = self._timestamp(node.this)
            seconds: exp.Expr = exp.Mul(this=_extract("HOUR", ts.copy()), expression=_num(3600))
            if unit in ("minute", "second"):
                seconds = exp.Add(
                    this=seconds,
                    expression=exp.Mul(this=_extract("MINUTE", ts.copy()), expression=_num(60)),
                )
            if unit == "second":
                seconds = exp.Add(
                    this=seconds, expression=_fn("FLOOR", _extract("SECOND", ts.copy()))
                )
            midnight = _cast(_cast(ts, "DATE"), "TIMESTAMP(6)", "teradata")
            return exp.Paren(
                this=exp.Add(this=midnight, expression=self._interval(seconds, "SECOND"))
            )
        raise DialectError(f"Teradata 的日期截斷不支援單位 {unit}")

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        def part(field: str, value: exp.Expr) -> exp.Expr:
            if field in ("year", "month", "day"):
                return _extract(field.upper(), value)
            if field == "quarter":
                return _quarter(value)
            return _extract(field.upper(), self._timestamp(value))

        return self.boundary_diff(
            _unit(node),
            node.expression,
            node.this,
            part,
            lambda start, end: exp.Sub(this=self._date(end), expression=self._date(start)),
        )

    def make_date(self, node: exp.Expr) -> exp.Expr:
        """Teradata 的 DATE 內部是 (年 − 1900) × 10000 + 月 × 100 + 日 的整數。"""
        y, m, d = (node.args[k] for k in ("year", "month", "day"))
        number = exp.Add(
            this=exp.Add(
                this=exp.Mul(
                    this=exp.Paren(this=exp.Sub(this=_paren(y), expression=_num(1900))),
                    expression=_num(10000),
                ),
                expression=exp.Mul(this=_paren(m), expression=_num(100)),
            ),
            expression=_paren(d),
        )
        return _cast(_cast(number, "INTEGER"), "DATE")

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
                this=_cast(day, "TIMESTAMP(6)", "teradata"),
                expression=self._interval(seconds, "SECOND"),
            )
        )

    def epoch(self, node: exp.Expr) -> exp.Expr:
        days = _cast(self._days_since(node.this.copy(), "DATE '1970-01-01'"), "BIGINT")
        return exp.Paren(
            this=exp.Add(
                this=exp.Mul(this=days, expression=_num(86400)),
                expression=self._seconds_of_day(node.this),
            )
        )

    def left(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        head = _fn("SUBSTR", text.copy(), _num(1), count.copy())
        if _not_negative(count):
            return head
        # 數量為負數時去掉右邊的字元（DuckDB 的 left）；Teradata 的 SUBSTR 不接受負數的長度
        trimmed = _fn(
            "SUBSTR",
            text.copy(),
            _num(1),
            _fn(
                "GREATEST",
                exp.Add(this=_fn("CHARACTER_LENGTH", text), expression=count.copy()),
                _num(0),
            ),
        )
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count.copy(), expression=_num(0)), true=trimmed)],
            default=head,
        )

    def right(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        start = exp.Add(
            this=exp.Sub(
                this=_fn("CHARACTER_LENGTH", text.copy()), expression=_paren(count.copy())
            ),
            expression=_num(1),
        )
        # SUBSTR 的起點小於 1 不是從結尾算起：數量超過長度時取整個字串
        tail = _fn("SUBSTR", text.copy(), _fn("GREATEST", start, _num(1)))
        if _not_negative(count):
            return tail
        trimmed = _fn("SUBSTR", text, exp.Sub(this=_num(1), expression=_paren(count.copy())))
        return exp.Case(
            ifs=[exp.If(this=exp.LT(this=count, expression=_num(0)), true=trimmed)],
            default=tail,
        )

    def split_part(self, node: exp.Expr) -> exp.Expr:
        """第 i 段：第 i − 1 個與第 i 個分隔字元之間（INSTR 的第四個參數是第幾次出現）。"""
        text, delimiter = node.args["this"], node.args["delimiter"]
        index = _literal_int(node.args.get("part_index"))
        if index is None or index < 1:
            raise DialectError("Teradata 的 SPLIT 只支援正整數常值的位置")

        def instr(occurrence: int) -> exp.Expr:
            return _fn("INSTR", text.copy(), delimiter.copy(), _num(1), _num(occurrence))

        if index == 1:
            start: exp.Expr = _num(1)
        else:
            start = exp.Add(
                this=instr(index - 1), expression=_fn("CHARACTER_LENGTH", delimiter.copy())
            )
        end = exp.Case(
            ifs=[
                exp.If(
                    this=exp.EQ(this=instr(index), expression=_num(0)),
                    true=exp.Add(this=_fn("CHARACTER_LENGTH", text.copy()), expression=_num(1)),
                )
            ],
            default=instr(index),
        )
        part = _fn("SUBSTR", text.copy(), start.copy(), exp.Sub(this=end, expression=_paren(start)))
        if index == 1:
            return part
        # 分隔字元不夠：空字串（與 DuckDB 的 split_part 相同）
        return exp.Case(
            ifs=[exp.If(this=exp.EQ(this=instr(index - 1), expression=_num(0)), true=_str(""))],
            default=part,
        )

    def _regex(self, pattern: exp.Expr) -> tuple[exp.Expr, str]:
        """開頭的 (?i) 換成不分大小寫的比對參數。"""
        if (
            isinstance(pattern, exp.Literal)
            and pattern.is_string
            and pattern.name.startswith("(?i)")
        ):
            return _str(pattern.name[4:]), "i"
        return pattern, "c"

    def regexp_like(self, node: exp.Expr) -> exp.Expr:
        pattern, flags = self._regex(node.expression)
        position = _fn("REGEXP_INSTR", node.this, pattern, _num(1), _num(1), _num(0), _str(flags))
        return exp.Paren(this=exp.GT(this=position, expression=_num(0)))

    def regexp_extract(self, node: exp.Expr) -> exp.Expr:
        pattern, flags = self._regex(node.expression)
        group = _literal_int(node.args.get("group"))
        if group is None:
            raise DialectError("REGEXP_EXTRACT 的群組不是常值")
        whole = _fn("REGEXP_SUBSTR", node.this, pattern.copy(), _num(1), _num(1), _str(flags))
        if group == 0:
            return whole
        if not (isinstance(pattern, exp.Literal) and pattern.is_string):
            raise DialectError("Teradata 的 REGEXP_EXTRACT 取群組時樣式必須是常值")
        if group > 8:
            raise DialectError("Teradata 的 REGEXP_EXTRACT 只能取第 1 到第 8 個群組")
        # 整段符合的文字以錨定的樣式換成第 group 個群組（外層多一個群組）
        anchored = _str(f"^({pattern.name})$")
        return _fn(
            "REGEXP_REPLACE", whole, anchored, _str(f"\\{group + 1}"), _num(1), _num(1), _str(flags)
        )

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：TO_TIMESTAMP 不符合格式時報錯，先以依格式產生的正規表示式檢查（日期不存在仍會報錯）。"""
        fmt = _format_literal(args[1])
        pattern = "^" + _strftime_regex(fmt).replace("\\d", "[0-9]") + "$"
        parsed = _fn(
            "TO_TIMESTAMP", args[0].copy(), _str(_convert_format(fmt, ORACLE_FORMAT, _double_quote))
        )
        matches = exp.EQ(
            this=_fn("REGEXP_SIMILAR", args[0], _str(pattern), _str("c")), expression=_num(1)
        )
        return exp.Case(ifs=[exp.If(this=matches, true=_cast(parsed, "TIMESTAMP(6)", "teradata"))])

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        assert isinstance(target, exp.DataType)
        value = node.this
        if target.this in TIMESTAMP_TYPES:
            # 有小數秒、沒有小數秒、只有日期的文字
            attempts = [
                _mark(exp.TryCast(this=value.copy(), to=_type("TIMESTAMP(6)", "teradata"))),
                _cast(
                    _mark(exp.TryCast(this=value.copy(), to=_type("TIMESTAMP(0)", "teradata"))),
                    "TIMESTAMP(6)",
                    "teradata",
                ),
                _cast(_mark(exp.TryCast(this=value, to=_type("DATE"))), "TIMESTAMP(6)", "teradata"),
            ]
            return exp.Coalesce(this=attempts[0], expressions=attempts[1:])
        return _mark(exp.TryCast(this=value, to=self._target(target)))

    def cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        if not isinstance(target, exp.DataType):
            return node
        kind = target.this
        if kind == exp.DataType.Type.BOOLEAN:
            raise DialectError("Teradata 沒有布林型別，轉成布林值的計算尚不支援")
        value = node.this
        if (
            isinstance(value, exp.Literal)
            and value.is_string
            and kind in (exp.DataType.Type.DATE, *TIMESTAMP_TYPES)
        ):
            keyword = "DATE" if kind == exp.DataType.Type.DATE else "TIMESTAMP"
            text = value.name if keyword == "TIMESTAMP" else value.name[:10]
            if keyword == "TIMESTAMP" and "." not in text:
                text += ".000000" if len(text) > 10 else " 00:00:00.000000"
            return exp.Var(this=f"{keyword} {_single_quote(text)}")
        if _is_predicate(value) and kind in INTEGER_TYPES:
            return exp.Case(
                ifs=[
                    exp.If(this=value, true=_num(1)),
                    exp.If(this=exp.Not(this=_paren(value.copy())), true=_num(0)),
                ]
            )
        return _mark(exp.Cast(this=value, to=self._target(target)))

    def _target(self, target: exp.Expr) -> exp.DataType:
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.CHAR):
            return _type(TERADATA_TEXT, "teradata")
        if kind in INTEGER_TYPES:
            return _type("BIGINT" if kind == exp.DataType.Type.BIGINT else "INTEGER", "teradata")
        if kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT):
            return _type("FLOAT", "teradata")
        if kind == exp.DataType.Type.DECIMAL:
            return target if target.expressions else _type("DECIMAL(18, 3)", "teradata")
        if kind == exp.DataType.Type.DATE:
            return target
        if kind in TIMESTAMP_TYPES:
            return _type("TIMESTAMP(6)", "teradata")
        raise DialectError(f"Teradata 不支援轉成 {target.sql('duckdb')} 型別")

    def finish(self, tree: exp.Expr) -> None:
        for select in list(tree.find_all(exp.Select)):
            _qualify_star(select)
            if select is not tree and select.args.get("limit") is not None:
                _limit_to_qualify(select)
        for boolean in list(tree.find_all(exp.Boolean)):
            if boolean.root() is tree:
                _replace_boolean(boolean)
        for node in tree.walk():
            if isinstance(node, PREDICATES) and not _condition_position(node):
                raise DialectError(
                    "Teradata 沒有布林值：查詢結果或運算中的布林值（例如布林維度、ISNULL、CONTAINS 的結果）尚不支援"
                )

    def text(self, sql: str) -> str:
        return sql.replace(TERADATA_TEXT, f"{TERADATA_TEXT} CHARACTER SET UNICODE")


def _known_date(value: exp.Expr) -> bool:
    """確定是日期（沒有時間）的值：轉成 DATE 的轉型或 DATE 常值。"""
    while isinstance(value, exp.Paren):
        value = value.this
    if isinstance(value, exp.Cast):
        return value.to.this == exp.DataType.Type.DATE
    return isinstance(value, exp.Var) and value.name.startswith("DATE '")


def _int_div(value: exp.Expr, divisor: int) -> exp.Expr:
    """Teradata 的 INTEGER / INTEGER 是向 0 截斷的整數除法（typed：SQLGlot 不再先轉成浮點數）。"""
    return _mark(exp.Div(this=value, expression=_num(divisor), typed=True))


def _quarter(value: exp.Expr) -> exp.Expr:
    month = exp.Paren(this=exp.Add(this=_extract("MONTH", value), expression=_num(2)))
    return exp.Paren(this=_int_div(month, 3))


def _limit_to_qualify(select: exp.Select) -> None:
    """子查詢與 CTE 中的 ORDER BY … LIMIT n → QUALIFY ROW_NUMBER() OVER (ORDER BY …) <= n
    （Teradata 的子查詢不能排序；TOP 只能用在最外層）。"""
    limit = select.args["limit"]
    count = limit.expression if isinstance(limit, exp.Limit) else limit
    order = select.args.get("order")
    window = _mark(
        exp.Window(
            this=exp.RowNumber(),
            order=order.copy() if isinstance(order, exp.Order) else None,
        )
    )
    select.set("qualify", _mark(exp.Qualify(this=exp.LTE(this=window, expression=count.copy()))))
    select.set("limit", None)
    select.set("order", None)


def _capture_groups(pattern: str) -> int:
    """正規表示式（RE2）中擷取的群組數。"""
    return _keep_group(pattern, -1, count=True).count("(") if pattern else 0


def _keep_group(pattern: str, keep: int, count: bool = False) -> str:
    """只保留第 keep 個擷取的群組（0：都不保留），其他的群組改成不擷取的 (?:…)；
    count=True 時只回傳每個擷取群組一個「(」（計數用）。跳脫字元與字元類別中的括號不算。"""
    out: list[str] = []
    i = 0
    group = 0
    in_class = False
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            if not count:
                out.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            if pattern.startswith("[:", i):
                end = pattern.find(":]", i + 2)
                if end >= 0:
                    if not count:
                        out.append(pattern[i : end + 2])
                    i = end + 2
                    continue
            if char == "]":
                in_class = False
            if not count:
                out.append(char)
            i += 1
            continue
        if char == "[":
            in_class = True
            start = i
            i += 1
            if pattern[i : i + 1] == "^":
                i += 1
            if pattern[i : i + 1] == "]":
                i += 1
            if not count:
                out.append(pattern[start:i])
            continue
        if char == "(":
            named = pattern.startswith("(?P<", i) or pattern.startswith("(?<", i)
            if pattern.startswith("(?", i) and not named:
                if not count:
                    out.append(char)
                i += 1
                continue
            group += 1
            opener_end = pattern.index(">", i) + 1 if named else i + 1
            if count:
                out.append("(")
            elif group == keep:
                out.append(pattern[i:opener_end])
            else:
                out.append("(?:")
            i = opener_end
            continue
        if not count:
            out.append(char)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- IBM Db2


DB2_TEXT = "VARCHAR(4000)"
DB2_EPOCH_DAYS = 719163
"""DAYS('1970-01-01')：DAYS() 是自 0001-01-01 起的第幾天（0001-01-01 為 1）。"""
DB2_FORMAT = {
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
"""strftime 的格式 → Db2 的 TIMESTAMP_FORMAT（格式中的文字只能是分隔字元）。"""
DB2_SEPARATORS = set("-./,':; ()")


def _units() -> exp.Expr:
    """字串函式的單位：以字元（Unicode 的碼位）計算；預設的 OCTETS 以位元組計算，中文會被切斷。"""
    return exp.Var(this="CODEUNITS32")


class Db2Rewriter(OracleRewriter):
    """IBM Db2 for Linux、UNIX and Windows 11.5 以上：依官方文件撰寫，尚未實機驗證
    （測試以 SQLGlot 轉回 DuckDB 模擬執行）。

    沿用 Oracle 的規則（沒有布林值：查詢結果中的布林值列為不支援；PERCENTILE_CONT WITHIN GROUP、MEDIAN、
    FETCH FIRST），差異：

    - 字串以字元計算：SUBSTRING、CHARACTER_LENGTH、LOCATE_IN_STRING、LEFT、RIGHT 加上 CODEUNITS32
      （預設以位元組計算，中文會被切斷）；|| 遇到 Null 得 Null
    - 日期加減以 ADD_DAYS、ADD_HOURS、ADD_MINUTES、ADD_SECONDS；月份從同一時間的月初以 ADD_MONTHS 加起，
      再取不超過月底的日（Db2 的 ADD_MONTHS 遇到月底時取月底，與 DuckDB 不同）
    - 天數差以 DAYS() 相減（DATE 相減得到的是 yyyymmdd 格式的期間）；自 1970 年起的秒數以 DAYS、
      MIDNIGHT_SECONDS 與 MICROSECOND 計算
    - 星期、週數為 DAYOFWEEK、DAYOFWEEK_ISO、WEEK_ISO；ISO 年取該週的星期四；截斷以 TRUNC（TRUNC_TIMESTAMP）
    - ATAN2 的參數順序是 (x, y)；沒有 PI：以 ACOS(-1)；LOG 是自然對數
    - DATEPARSE 以 TIMESTAMP_FORMAT，先以依格式產生的正規表示式檢查（不符合時報錯）；文字轉數字、日期也
      先以正規表示式檢查（沒有 TRY_CAST）
    - Null 也相等的比較為 IS NOT DISTINCT FROM；SELECT 沒有 FROM 時補上 SYSIBM.SYSDUMMY1
    - 取消彙總的列編號為 ROW_NUMBER() OVER ()（Db2 的視窗排序不能是常值 Null）
    """

    name = "Db2"
    dialect = "db2"
    allowed = frozenset({exp.Median, exp.Mod, exp.NullSafeEQ, exp.Repeat})

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit in ("year", "month"):
            months = (
                amount if unit == "month" else exp.Mul(this=_paren(amount), expression=_num(12))
            )
            return self._add_months(value, months)
        if unit == "day":
            return _fn("ADD_DAYS", value, _cast(amount, "INTEGER"))
        if unit in ("hour", "minute"):
            return _fn(f"ADD_{unit.upper()}S", self._timestamp(value), _cast(amount, "INTEGER"))
        if unit == "microsecond":
            amount = exp.Div(this=_paren(amount), expression=_num(1000000.0))
        # ADD_SECONDS 的秒數可以有小數（到微秒）
        seconds = _cast(amount, "DECIMAL(31, 6)", "tmigdb2")
        return _fn("ADD_SECONDS", self._timestamp(value), seconds)

    def _timestamp(self, value: exp.Expr) -> exp.Expr:
        if isinstance(value, exp.Cast) and value.to.this in TIMESTAMP_TYPES:
            return value
        return _cast(value, "TIMESTAMP")

    def _add_months(self, value: exp.Expr, months: exp.Expr) -> exp.Expr:
        """同一時間的月初加上月數，再加回日數（不超過該月的最後一天）。"""
        back = exp.Sub(this=_num(1), expression=_fn("DAY", value.copy()))
        first = _fn("ADD_MONTHS", _fn("ADD_DAYS", value.copy(), back), _cast(months, "INTEGER"))
        day = _fn("LEAST", _fn("DAY", value), _fn("DAY", _fn("LAST_DAY", first.copy())))
        return _fn("ADD_DAYS", first, exp.Sub(this=day, expression=_num(1)))

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def native(name: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: _fn(name, n.this)

        return {
            **super().handlers(),
            exp.Rand: lambda n: _fn("RAND"),
            exp.Quarter: native("QUARTER"),
            exp.Hour: native("HOUR"),
            exp.Minute: native("MINUTE"),
            exp.Second: native("SECOND"),
            exp.DayOfYear: native("DAYOFYEAR"),
            exp.Week: native("WEEK_ISO"),
            exp.DayOfWeek: lambda n: exp.Paren(
                this=exp.Sub(this=_fn("DAYOFWEEK", n.this), expression=_num(1))
            ),
            exp.DayOfWeekIso: native("DAYOFWEEK_ISO"),
            exp.Monthname: lambda n: _names(_fn("MONTH", n.this), MONTH_NAMES),
            exp.Dayname: lambda n: _names(_fn("DAYOFWEEK_ISO", n.this), DAY_NAMES),
            exp.Log: self.log,
            exp.Round: lambda n: _cast(
                _fn(
                    "ROUND",
                    _cast(n.this, "DECIMAL(31, 10)", "tmigdb2"),
                    _cast(n.args.get("decimals") or _num(0), "INTEGER"),
                ),
                "DOUBLE",
            ),
            exp.Trunc: lambda n: _fn("TRUNC", n.this, n.args.get("decimals") or _num(0)),
            exp.Atan2: lambda n: _fn("ATAN2", n.expression, n.this),
            exp.IntDiv: lambda n: _cast(
                _fn(
                    "TRUNC",
                    exp.Div(this=_cast(n.this, "DOUBLE"), expression=_paren(n.expression)),
                    _num(0),
                ),
                "BIGINT",
            ),
            exp.Unicode: lambda n: _fn("ASCII", n.this),
            exp.TimestampTrunc: self.trunc,
            exp.DateTrunc: self.trunc,
            exp.TimeToUnix: self.epoch,
            exp.Substring: self.substring,
            exp.Length: lambda n: _fn("CHARACTER_LENGTH", n.this, _units()),
            exp.StrPosition: self.position,
            exp.Left: self.left,
            exp.Right: self.right,
            exp.Repeat: lambda n: _mark(n),
            exp.Variance: lambda n: _fn("VARIANCE_SAMP", n.this),
            exp.VariancePop: lambda n: _fn("VARIANCE", n.this),
            exp.StddevPop: lambda n: _fn("STDDEV", n.this),
            exp.CovarSamp: lambda n: _fn("COVARIANCE_SAMP", n.this, n.expression),
            exp.CovarPop: lambda n: _fn("COVARIANCE", n.this, n.expression),
            exp.Corr: lambda n: _fn("CORRELATION", n.this, n.expression),
            exp.NullSafeEQ: lambda n: _mark(n),
            exp.RegexpExtract: lambda n: _fn(
                "REGEXP_SUBSTR",
                n.this,
                n.expression,
                _num(1),
                _num(1),
                _str("c"),
                n.args.get("group") or _num(0),
            ),
            exp.RegexpReplace: self.regexp_replace,
            exp.CurrentDate: lambda n: exp.Var(this="CURRENT_DATE"),
            exp.DPipe: lambda n: _mark(n),
            exp.Window: self.window,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            **super().functions(),
            "isoyear": lambda a: _fn("YEAR", self._thursday(a[0])),
            "now": lambda a: exp.Var(this="CURRENT_TIMESTAMP"),
        }

    def _thursday(self, value: exp.Expr) -> exp.Expr:
        """ISO 週的星期四（決定 ISO 年）。"""
        return _fn(
            "ADD_DAYS",
            value.copy(),
            exp.Sub(this=_num(4), expression=_fn("DAYOFWEEK_ISO", value)),
        )

    def log(self, node: exp.Expr) -> exp.Expr:
        if node.expression is None:
            raise DialectError("Db2 的 LOG 需要底數")
        # SQLGlot 的 Log(this=底數, expression=真數)；Db2 的 LOG 是自然對數
        return exp.Div(this=_fn("LN", node.expression), expression=_fn("LN", node.this))

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
            ts = self._timestamp(node.this)
            return _fn("ADD_SECONDS", _fn("TRUNC", ts.copy(), _str("MI")), _fn("SECOND", ts))
        raise DialectError(f"Db2 的日期截斷不支援單位 {unit}")

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        def part(field: str, value: exp.Expr) -> exp.Expr:
            if field in ("year", "month", "day"):
                return _extract(field.upper(), value)
            return _fn(field.upper(), value)

        return self.boundary_diff(
            _unit(node),
            node.expression,
            node.this,
            part,
            lambda start, end: exp.Sub(this=_fn("DAYS", end), expression=_fn("DAYS", start)),
        )

    def make_date(self, node: exp.Expr) -> exp.Expr:
        y, m, d = (node.args[k] for k in ("year", "month", "day"))

        def pad(value: exp.Expr, width: int) -> exp.Expr:
            text = _cast(_cast(value, "INTEGER"), "VARCHAR(11)", "tmigdb2")
            return _fn("LPAD", text, _num(width), _str("0"))

        def concat(left: exp.Expr, right: exp.Expr) -> exp.Expr:
            return _mark(exp.DPipe(this=left, expression=right))

        text = concat(concat(concat(concat(pad(y, 4), _str("-")), pad(m, 2)), _str("-")), pad(d, 2))
        return _mark(exp.Cast(this=text, to=_type("DATE")))

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
        return self.add_interval(_cast(day, "TIMESTAMP"), "second", seconds)

    def epoch(self, node: exp.Expr) -> exp.Expr:
        ts = self._timestamp(node.this)
        days = exp.Sub(this=_fn("DAYS", ts.copy()), expression=_num(DB2_EPOCH_DAYS))
        micros = exp.Div(this=_fn("MICROSECOND", ts.copy()), expression=_num(1000000.0))
        return exp.Paren(
            this=exp.Add(
                this=exp.Add(
                    this=exp.Mul(this=_paren(days), expression=_num(86400)),
                    expression=_fn("MIDNIGHT_SECONDS", ts),
                ),
                expression=micros,
            )
        )

    def substring(self, node: exp.Expr) -> exp.Expr:
        start = node.args.get("start") or _num(1)
        length = node.args.get("length")
        if length is None:
            return _fn("SUBSTRING", node.this, start, _units())
        return _fn("SUBSTRING", node.this, start, length, _units())

    def position(self, node: exp.Expr) -> exp.Expr:
        start = node.args.get("position") or _num(1)
        return _fn("LOCATE_IN_STRING", node.this, node.args["substr"], start, _num(1), _units())

    def left(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        head = _fn("LEFT", text.copy(), _fn("GREATEST", count.copy(), _num(0)), _units())
        if _not_negative(count):
            return head
        # 負數：去掉結尾的字元
        keep = _fn(
            "GREATEST",
            exp.Add(this=_fn("CHARACTER_LENGTH", text.copy(), _units()), expression=count.copy()),
            _num(0),
        )
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.LT(this=count.copy(), expression=_num(0)),
                    true=_fn("LEFT", text, keep, _units()),
                )
            ],
            default=head,
        )

    def right(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        length = _fn("CHARACTER_LENGTH", text.copy(), _units())
        tail = _fn(
            "RIGHT",
            text.copy(),
            _fn("LEAST", _fn("GREATEST", count.copy(), _num(0)), length.copy()),
            _units(),
        )
        if _not_negative(count):
            return tail
        keep = _fn("GREATEST", exp.Add(this=length, expression=count.copy()), _num(0))
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.LT(this=count.copy(), expression=_num(0)),
                    true=_fn("RIGHT", text, keep, _units()),
                )
            ],
            default=tail,
        )

    def split_part(self, node: exp.Expr) -> exp.Expr:
        delimiter = node.args.get("delimiter")
        index = _literal_int(node.args.get("part_index"))
        if not (isinstance(delimiter, exp.Literal) and delimiter.is_string):
            raise DialectError("Db2 的 SPLIT 只支援常值的分隔字元")
        if index is None or index < 1:
            raise DialectError("Db2 的 SPLIT 只支援正整數常值的位置")
        escaped = re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", delimiter.name)
        pattern = f"(.*?)({escaped}|$)"
        return _fn(
            "REGEXP_SUBSTR", node.this, _str(pattern), _num(1), _num(index), _str("c"), _num(1)
        )

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        replacement = node.args.get("replacement") or _str("")
        if not (isinstance(replacement, exp.Literal) and replacement.is_string):
            raise DialectError("REGEXP_REPLACE 的取代字串不是常值")
        # Db2 的正規表示式是 ICU：取代字串的群組參照是 $1（與 Java 相同）
        return _fn(
            "REGEXP_REPLACE", node.this, node.expression, _str(java_replacement(replacement.name))
        )

    def window(self, node: exp.Expr) -> exp.Expr:
        """ROW_NUMBER() OVER (ORDER BY NULL) → OVER ()：Db2 的排序不能是沒有型別的 Null。"""
        order = node.args.get("order")
        if order is not None and all(
            isinstance(o, exp.Ordered) and isinstance(o.this, exp.Null) for o in order.expressions
        ):
            node.set("order", None)
        return node

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：TIMESTAMP_FORMAT 不符合格式時報錯，先以依格式產生的正規表示式檢查。"""
        fmt = _format_literal(args[1])

        def literal(text: str) -> str:
            if not set(text) <= DB2_SEPARATORS:
                raise DialectError(f"Db2 的 DATEPARSE 格式中只能有分隔字元：{text}")
            return text

        pattern = "^" + _strftime_regex(fmt).replace("\\d", "[0-9]") + "$"
        parsed = _fn(
            "TIMESTAMP_FORMAT", args[0].copy(), _str(_convert_format(fmt, DB2_FORMAT, literal))
        )
        matches = _mark(exp.RegexpLike(this=args[0], expression=_str(pattern)))
        return exp.Case(ifs=[exp.If(this=matches, true=parsed)])

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in INTEGER_TYPES:
            pattern = REDSHIFT_INTEGER
        elif kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DECIMAL):
            pattern = REDSHIFT_NUMBER
        elif kind == exp.DataType.Type.DATE or kind in TIMESTAMP_TYPES:
            pattern = DB2_DATE
        else:
            raise DialectError(f"Db2 不支援轉成 {target.sql('duckdb')} 的 TRY_CAST")
        text = _cast(node.this, DB2_TEXT, "tmigdb2")
        valid = _mark(
            exp.RegexpLike(this=text.copy(), expression=_str(pattern.replace("[[:space:]]", "\\s")))
        )
        value: exp.Expr = exp.Trim(this=text)
        if pattern == DB2_DATE:
            # 只有日期的文字補上時間；月與日不存在時（2024-02-30）Db2 報錯，與 TRY_CAST 不同，仍待實機確認
            value = _fn("TIMESTAMP", value)
        return exp.Case(
            ifs=[exp.If(this=valid, true=_mark(exp.Cast(this=value, to=self._target(target))))]
        )

    def cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        if not isinstance(target, exp.DataType):
            return node
        kind = target.this
        if kind == exp.DataType.Type.BOOLEAN:
            raise DialectError("Db2 的查詢結果沒有布林值，轉成布林值的計算尚不支援")
        value = node.this
        if _is_predicate(value) and kind in INTEGER_TYPES:
            return exp.Case(
                ifs=[
                    exp.If(this=value, true=_num(1)),
                    exp.If(this=exp.Not(this=_paren(value.copy())), true=_num(0)),
                ]
            )
        return _mark(exp.Cast(this=value, to=self._target(target)))

    def _target(self, target: exp.Expr) -> exp.DataType:
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.CHAR):
            return _type(DB2_TEXT, "tmigdb2")
        if kind in INTEGER_TYPES:
            return _type("BIGINT")
        if kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT):
            return _type("DOUBLE")
        if kind == exp.DataType.Type.DECIMAL:
            return target if target.expressions else _type("DECIMAL(18, 3)")
        if kind == exp.DataType.Type.DATE:
            return target
        if kind in TIMESTAMP_TYPES:
            return _type("TIMESTAMP")
        raise DialectError(f"Db2 不支援轉成 {target.sql('duckdb')} 型別")

    def finish(self, tree: exp.Expr) -> None:
        for select in list(tree.find_all(exp.Select)):
            _expand_group_ordinals(select)
            _qualify_star(select)
            if select.args.get("from_") is None and select.args.get("from") is None:
                select.set(
                    "from_",
                    exp.From(
                        this=exp.Table(
                            this=exp.to_identifier("SYSDUMMY1"), db=exp.to_identifier("SYSIBM")
                        )
                    ),
                )
        for boolean in list(tree.find_all(exp.Boolean)):
            if boolean.root() is tree:
                _replace_boolean(boolean)
        for node in tree.walk():
            if isinstance(node, PREDICATES) and not _condition_position(node):
                raise DialectError(
                    "Db2 的查詢結果沒有布林值：查詢結果或運算中的布林值（例如布林維度、ISNULL、CONTAINS 的結果）尚不支援"
                )


DB2_DATE = (
    r"^\s*[0-9]{4}-(0?[1-9]|1[0-2])-(0?[1-9]|[12][0-9]|3[01])"
    r"([ T]([01]?[0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?)?\s*$"
)
"""文字轉日期時間前的檢查（ISO 格式）。"""


# ---------------------------------------------------------------- SAP HANA


HANA_TEXT = "NVARCHAR(5000)"
HANA_FORMAT = {
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
    "%f": "FF6",
}
"""strftime 的格式 → HANA 的 TO_TIMESTAMP（格式中的文字只能是分隔字元）。"""
HANA_SEPARATORS = set("-./,:; ")


class HanaRewriter(Rewriter):
    """SAP HANA 2.0（SPS05 以上，含 HANA Cloud）：依官方文件撰寫，尚未實機驗證（測試以 SQLGlot 轉回 DuckDB
    模擬執行）。

    - 布林值：比較等條件不能直接當成值，查詢結果中的布林值寫成 CASE WHEN … THEN TRUE WHEN NOT … THEN FALSE END；
      條件中的 TRUE／FALSE 換成 1 = 1／1 = 0；Null 也相等的比較寫成 = 或兩者皆 Null
    - 日期加減以 ADD_DAYS、ADD_MONTHS（月底的行為與 DuckDB 相同）、ADD_SECONDS；天數差為 DAYS_BETWEEN；
      截斷以月初、年初的日期與時、分、秒相加；自 1970 年起的秒數以 NANO100_BETWEEN
    - 星期為 WEEKDAY（星期一為 0）；ISO 週數與年取自 ISOWEEK（'2024-W05'）；季以月份計算（QUARTER 是文字）
    - 字串以字元計算；LOCATE 的參數是（字串、要找的字）；沒有 REPEAT：以 RPAD
    - 正規表示式（PCRE）：LIKE_REGEXPR、SUBSTRING_REGEXPR、REPLACE_REGEXPR（樣式須為常值）
    - ATAN2 為 (y, x)；沒有 PI：以 ACOS(-1)；LOG(底數, 真數)
    - DATEPARSE 以 TO_TIMESTAMP，先以依格式產生的正規表示式檢查（不符合時報錯）；文字轉數字、日期也先以
      正規表示式檢查（沒有 TRY_CAST）
    - 沒有 COVAR、COVARP；SELECT 沒有 FROM 時補上 DUMMY；GROUP BY 不以位置分組
    - 取消彙總的列編號為 ROW_NUMBER() OVER ()
    """

    name = "SAP HANA"
    dialect = "hana"
    allowed = frozenset({exp.Median, exp.Year, exp.Month, exp.Boolean})

    def add_interval(self, value: exp.Expr, unit: str, amount: exp.Expr) -> exp.Expr:
        if unit in ("year", "month"):
            months = (
                amount if unit == "month" else exp.Mul(this=_paren(amount), expression=_num(12))
            )
            return _fn("ADD_MONTHS", value, _cast(months, "INTEGER"))
        if unit == "day":
            return _fn("ADD_DAYS", value, _cast(amount, "INTEGER"))
        factor = {"hour": 3600, "minute": 60, "second": 1, "microsecond": 0.000001}[unit]
        seconds = amount if factor == 1 else exp.Mul(this=_paren(amount), expression=_num(factor))
        return _fn(
            "ADD_SECONDS", self._timestamp(value), _cast(seconds, "DECIMAL(31, 7)", "tmighana")
        )

    def _timestamp(self, value: exp.Expr) -> exp.Expr:
        if isinstance(value, exp.Cast) and value.to.this in TIMESTAMP_TYPES:
            return value
        return _cast(value, "TIMESTAMP")

    def _date(self, value: exp.Expr) -> exp.Expr:
        if isinstance(value, exp.Cast) and value.to.this == exp.DataType.Type.DATE:
            return value
        return _cast(value, "DATE")

    def handlers(self) -> dict[type[exp.Expr], Callable[[exp.Expr], exp.Expr]]:
        def native(name: str) -> Callable[[exp.Expr], exp.Expr]:
            return lambda n: _fn(name, n.this)

        def iso_week(value: exp.Expr, start: int, length: int) -> exp.Expr:
            return _cast(
                _fn("SUBSTRING", _fn("ISOWEEK", value), _num(start), _num(length)), "INTEGER"
            )

        acos = lambda: _fn("ACOS", _num(-1))  # noqa: E731

        return {
            exp.Day: native("DAYOFMONTH"),
            exp.Quarter: lambda n: exp.Paren(
                this=exp.Add(
                    this=_cast(
                        _fn(
                            "FLOOR",
                            exp.Div(
                                this=exp.Paren(
                                    this=exp.Sub(this=_fn("MONTH", n.this), expression=_num(1))
                                ),
                                expression=_num(3.0),
                            ),
                        ),
                        "INTEGER",
                    ),
                    expression=_num(1),
                )
            ),
            exp.Hour: lambda n: _fn("HOUR", self._timestamp(n.this)),
            exp.Minute: lambda n: _fn("MINUTE", self._timestamp(n.this)),
            exp.Second: lambda n: _cast(
                _fn("FLOOR", _fn("SECOND", self._timestamp(n.this))), "INTEGER"
            ),
            exp.DayOfYear: native("DAYOFYEAR"),
            exp.Week: lambda n: iso_week(n.this, 7, 2),
            exp.DayOfWeek: lambda n: _fn(
                "MOD", exp.Add(this=_fn("WEEKDAY", n.this), expression=_num(1)), _num(7)
            ),
            exp.DayOfWeekIso: lambda n: exp.Paren(
                this=exp.Add(this=_fn("WEEKDAY", n.this), expression=_num(1))
            ),
            exp.Monthname: lambda n: _names(_fn("MONTH", n.this), MONTH_NAMES),
            exp.Dayname: lambda n: _names(
                exp.Paren(this=exp.Add(this=_fn("WEEKDAY", n.this), expression=_num(1))), DAY_NAMES
            ),
            exp.TimestampTrunc: self.date_trunc,
            exp.DateTrunc: self.date_trunc,
            exp.DateDiff: self.date_diff,
            exp.DateFromParts: lambda n: self.make_date(
                n.args["year"], n.args["month"], n.args["day"]
            ),
            exp.TimestampFromParts: self.make_timestamp,
            exp.TimeToUnix: lambda n: exp.Paren(
                this=exp.Div(
                    this=_fn(
                        "NANO100_BETWEEN",
                        _cast(_str("1970-01-01 00:00:00"), "TIMESTAMP"),
                        self._timestamp(n.this),
                    ),
                    expression=_num(10000000.0),
                )
            ),
            exp.Log: lambda n: (
                _fn("LOG", n.this, n.expression) if n.expression is not None else self.no_base()
            ),
            exp.Round: lambda n: _fn(
                "ROUND", n.this, _cast(n.args.get("decimals") or _num(0), "INTEGER")
            ),
            exp.Trunc: lambda n: _toward_zero(n.this),
            exp.Pow: lambda n: _fn("POWER", n.this, n.expression),
            exp.Mod: lambda n: _fn("MOD", n.this, n.expression),
            exp.IntDiv: lambda n: _cast(
                _toward_zero(
                    exp.Div(this=_cast(n.this, "DOUBLE"), expression=_paren(n.expression))
                ),
                "BIGINT",
            ),
            exp.Pi: lambda n: acos(),
            exp.Rand: lambda n: _fn("RAND"),
            exp.Degrees: lambda n: exp.Paren(
                this=exp.Div(
                    this=exp.Mul(this=_paren(n.this), expression=_num(180)), expression=acos()
                )
            ),
            exp.Radians: lambda n: exp.Paren(
                this=exp.Div(
                    this=exp.Mul(this=_paren(n.this), expression=acos()), expression=_num(180)
                )
            ),
            exp.Unicode: native("UNICODE"),
            exp.Chr: lambda n: _fn("NCHAR", *n.expressions),
            exp.Substring: lambda n: _fn(
                "SUBSTRING",
                n.this,
                n.args.get("start") or _num(1),
                *([n.args["length"]] if n.args.get("length") else []),
            ),
            exp.Length: native("LENGTH"),
            exp.StrPosition: lambda n: _fn(
                "LOCATE", n.this, n.args["substr"], n.args.get("position") or _num(1)
            ),
            exp.Left: self.left,
            exp.Right: self.right,
            exp.Repeat: lambda n: _fn(
                "RPAD",
                _str(""),
                _fn(
                    "GREATEST",
                    exp.Mul(this=_fn("LENGTH", n.this.copy()), expression=_paren(n.args["times"])),
                    _num(0),
                ),
                n.this,
            ),
            exp.SplitPart: self.split_part,
            exp.PercentileCont: lambda n: _mark(
                exp.WithinGroup(
                    this=_fn("PERCENTILE_CONT", n.expression),
                    expression=_mark(exp.Order(expressions=[_mark(exp.Ordered(this=n.this))])),
                )
            ),
            exp.Variance: native("VAR_SAMP"),
            exp.VariancePop: native("VAR_POP"),
            exp.CovarSamp: self.no_covariance,
            exp.CovarPop: self.no_covariance,
            exp.NullSafeEQ: lambda n: exp.Paren(
                this=exp.Or(
                    this=exp.EQ(this=n.this.copy(), expression=n.expression.copy()),
                    expression=exp.Paren(
                        this=exp.And(
                            this=exp.Is(this=n.this, expression=exp.Null()),
                            expression=exp.Is(this=n.expression, expression=exp.Null()),
                        )
                    ),
                )
            ),
            exp.RegexpExtract: self.regexp_extract,
            exp.RegexpReplace: self.regexp_replace,
            exp.TryCast: self.try_cast,
            exp.Cast: self.cast,
            exp.CurrentDate: lambda n: exp.Var(this="CURRENT_DATE"),
            exp.DPipe: lambda n: _mark(n),
            exp.Window: self.window,
        }

    def functions(self) -> dict[str, Callable[[list[exp.Expr]], exp.Expr]]:
        return {
            "isoyear": lambda a: _cast(_fn("LEFT", _fn("ISOWEEK", a[0]), _num(4)), "INTEGER"),
            "now": lambda a: exp.Var(this="CURRENT_TIMESTAMP"),
            "try_strptime": self.parse_date,
        }

    def no_base(self) -> exp.Expr:
        raise DialectError("SAP HANA 的 LOG 需要底數")

    def no_covariance(self, node: exp.Expr) -> exp.Expr:
        raise DialectError("SAP HANA 沒有 COVAR、COVARP")

    def date_trunc(self, node: exp.Expr) -> exp.Expr:
        unit = _unit(node)
        value = node.this
        if unit in ("year", "month", "quarter"):
            day = _fn("DAYOFYEAR" if unit == "year" else "DAYOFMONTH", value.copy())
            first = _fn("ADD_DAYS", self._date(value.copy()), exp.Sub(this=_num(1), expression=day))
            if unit == "quarter":
                back = _fn("MOD", exp.Sub(this=_fn("MONTH", value), expression=_num(1)), _num(3))
                first = _fn("ADD_MONTHS", first, exp.Neg(this=_paren(back)))
            return _cast(first, "TIMESTAMP")
        if unit == "day":
            return _cast(self._date(value), "TIMESTAMP")
        if unit not in ("hour", "minute", "second"):
            raise DialectError(f"SAP HANA 的日期截斷不支援單位 {unit}")
        ts = self._timestamp(value)
        seconds: exp.Expr = exp.Mul(this=_fn("HOUR", ts.copy()), expression=_num(3600))
        if unit in ("minute", "second"):
            seconds = exp.Add(
                this=seconds, expression=exp.Mul(this=_fn("MINUTE", ts.copy()), expression=_num(60))
            )
        if unit == "second":
            seconds = exp.Add(this=seconds, expression=_fn("FLOOR", _fn("SECOND", ts.copy())))
        midnight = _cast(self._date(ts), "TIMESTAMP")
        return _fn("ADD_SECONDS", midnight, seconds)

    def date_diff(self, node: exp.Expr) -> exp.Expr:
        handlers = self.handlers()

        def part(field: str, value: exp.Expr) -> exp.Expr:
            if field in ("year", "month"):
                return _extract(field.upper(), value)
            kind = {
                "quarter": exp.Quarter,
                "hour": exp.Hour,
                "minute": exp.Minute,
                "second": exp.Second,
            }
            return handlers[kind[field]](kind[field](this=value))

        return self.boundary_diff(
            _unit(node),
            node.expression,
            node.this,
            part,
            lambda start, end: _fn("DAYS_BETWEEN", self._date(start), self._date(end)),
        )

    def make_date(self, y: exp.Expr, m: exp.Expr, d: exp.Expr) -> exp.Expr:
        def pad(value: exp.Expr, width: int) -> exp.Expr:
            return _fn("LPAD", _fn("TO_VARCHAR", _cast(value, "INTEGER")), _num(width), _str("0"))

        def concat(left: exp.Expr, right: exp.Expr) -> exp.Expr:
            return _mark(exp.DPipe(this=left, expression=right))

        text = concat(concat(concat(concat(pad(y, 4), _str("-")), pad(m, 2)), _str("-")), pad(d, 2))
        return _fn("TO_DATE", text, _str("YYYY-MM-DD"))

    def make_timestamp(self, node: exp.Expr) -> exp.Expr:
        keys = ("year", "month", "day", "hour", "min", "sec")
        y, m, d, hh, mi, ss = (node.args.get(k) or _num(0) for k in keys)
        seconds = exp.Add(
            this=exp.Add(
                this=exp.Mul(this=_paren(hh), expression=_num(3600)),
                expression=exp.Mul(this=_paren(mi), expression=_num(60)),
            ),
            expression=_paren(ss),
        )
        return self.add_interval(_cast(self.make_date(y, m, d), "TIMESTAMP"), "second", seconds)

    def left(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        head = _fn("LEFT", text.copy(), _fn("GREATEST", count.copy(), _num(0)))
        if _not_negative(count):
            return head
        keep = _fn(
            "GREATEST", exp.Add(this=_fn("LENGTH", text.copy()), expression=count.copy()), _num(0)
        )
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.LT(this=count.copy(), expression=_num(0)), true=_fn("LEFT", text, keep)
                )
            ],
            default=head,
        )

    def right(self, node: exp.Expr) -> exp.Expr:
        text, count = node.this, node.expression
        tail = _fn("RIGHT", text.copy(), _fn("GREATEST", count.copy(), _num(0)))
        if _not_negative(count):
            return tail
        keep = _fn(
            "GREATEST", exp.Add(this=_fn("LENGTH", text.copy()), expression=count.copy()), _num(0)
        )
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.LT(this=count.copy(), expression=_num(0)),
                    true=_fn("RIGHT", text, keep),
                )
            ],
            default=tail,
        )

    def _raw(self, node: exp.Expr) -> str:
        """改寫並驗證過的子運算式 → HANA SQL（用在 SQLGlot 沒有語法節點的寫法）。"""
        self.validate(node)
        return node.sql(dialect="tmighana", identify=True)

    def _pattern(self, node: exp.Expr, what: str) -> str:
        if not (isinstance(node, exp.Literal) and node.is_string):
            raise DialectError(f"SAP HANA 的 {what} 樣式必須是常值")
        return _single_quote(node.name)

    def split_part(self, node: exp.Expr) -> exp.Expr:
        delimiter = node.args.get("delimiter")
        index = _literal_int(node.args.get("part_index"))
        if not (isinstance(delimiter, exp.Literal) and delimiter.is_string):
            raise DialectError("SAP HANA 的 SPLIT 只支援常值的分隔字元")
        if index is None or index < 1:
            raise DialectError("SAP HANA 的 SPLIT 只支援正整數常值的位置")
        escaped = re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", delimiter.name)
        pattern = _single_quote(f"(.*?)({escaped}|$)")
        text = self._raw(node.this)
        # 沒有第 n 段時 SUBSTRING_REGEXPR 為 Null；DuckDB 的 split_part 為空字串（字串不是 Null 時）
        part = exp.Var(this=f"SUBSTRING_REGEXPR({pattern} IN {text} OCCURRENCE {index} GROUP 1)")
        known = exp.Not(this=exp.Is(this=node.this.copy(), expression=exp.Null()))
        return exp.Coalesce(
            this=part, expressions=[exp.Case(ifs=[exp.If(this=known, true=_str(""))])]
        )

    def regexp_extract(self, node: exp.Expr) -> exp.Expr:
        group = _literal_int(node.args.get("group") or _num(0))
        if group is None:
            raise DialectError("SAP HANA 的 REGEXP_EXTRACT 群組必須是常值")
        pattern = self._pattern(node.expression, "REGEXP_EXTRACT")
        suffix = f" GROUP {group}" if group else ""
        return exp.Var(this=f"SUBSTRING_REGEXPR({pattern} IN {self._raw(node.this)}{suffix})")

    def regexp_replace(self, node: exp.Expr) -> exp.Expr:
        replacement = node.args.get("replacement") or _str("")
        if not (isinstance(replacement, exp.Literal) and replacement.is_string):
            raise DialectError("REGEXP_REPLACE 的取代字串不是常值")
        pattern = self._pattern(node.expression, "REGEXP_REPLACE")
        # HANA 的取代字串以 \1 參照群組（與 DuckDB 相同）
        text = self._raw(node.this)
        return exp.Var(
            this=f"REPLACE_REGEXPR({pattern} IN {text} WITH {_single_quote(replacement.name)} OCCURRENCE ALL)"
        )

    def window(self, node: exp.Expr) -> exp.Expr:
        """ROW_NUMBER() OVER (ORDER BY NULL) → OVER ()。"""
        order = node.args.get("order")
        if order is not None and all(
            isinstance(o, exp.Ordered) and isinstance(o.this, exp.Null) for o in order.expressions
        ):
            node.set("order", None)
        return node

    def parse_date(self, args: list[exp.Expr]) -> exp.Expr:
        """DATEPARSE：TO_TIMESTAMP 不符合格式時報錯，先以依格式產生的正規表示式檢查。"""
        fmt = _format_literal(args[1])

        def literal(text: str) -> str:
            if not set(text) <= HANA_SEPARATORS:
                raise DialectError(f"SAP HANA 的 DATEPARSE 格式中只能有分隔字元：{text}")
            return text

        pattern = "^" + _strftime_regex(fmt).replace("\\d", "[0-9]") + "$"
        parsed = _fn(
            "TO_TIMESTAMP", args[0].copy(), _str(_convert_format(fmt, HANA_FORMAT, literal))
        )
        matches = _mark(exp.RegexpLike(this=args[0], expression=_str(pattern)))
        return exp.Case(ifs=[exp.If(this=matches, true=parsed)])

    def try_cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in INTEGER_TYPES:
            pattern = REDSHIFT_INTEGER
        elif kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DECIMAL):
            pattern = REDSHIFT_NUMBER
        elif kind == exp.DataType.Type.DATE or kind in TIMESTAMP_TYPES:
            pattern = HANA_DATE
        else:
            raise DialectError(f"SAP HANA 不支援轉成 {target.sql('duckdb')} 的 TRY_CAST")
        text = _cast(node.this, HANA_TEXT, "tmighana")
        valid = _mark(exp.RegexpLike(this=text.copy(), expression=_str(pattern)))
        value: exp.Expr = exp.Trim(this=text)
        return exp.Case(
            ifs=[exp.If(this=valid, true=_mark(exp.Cast(this=value, to=self._target(target))))]
        )

    def cast(self, node: exp.Expr) -> exp.Expr:
        target = node.args["to"]
        if not isinstance(target, exp.DataType):
            return node
        value = node.this
        if target.this == exp.DataType.Type.BOOLEAN and not _is_predicate(value):
            raise DialectError("SAP HANA 不能把數字或文字轉成布林值")
        if target.this == exp.DataType.Type.BOOLEAN:
            return value
        if _is_predicate(value) and target.this in INTEGER_TYPES:
            return exp.Case(
                ifs=[
                    exp.If(this=value, true=_num(1)),
                    exp.If(this=exp.Not(this=_paren(value.copy())), true=_num(0)),
                ]
            )
        return _mark(exp.Cast(this=value, to=self._target(target)))

    def _target(self, target: exp.Expr) -> exp.DataType:
        assert isinstance(target, exp.DataType)
        kind = target.this
        if kind in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.CHAR):
            return _type(HANA_TEXT, "tmighana")
        if kind in INTEGER_TYPES:
            return _type("BIGINT")
        if kind in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT):
            return _type("DOUBLE")
        if kind == exp.DataType.Type.DECIMAL:
            return target if target.expressions else _type("DECIMAL(18, 3)")
        if kind == exp.DataType.Type.DATE:
            return target
        if kind in TIMESTAMP_TYPES:
            return _type("TIMESTAMP")
        raise DialectError(f"SAP HANA 不支援轉成 {target.sql('duckdb')} 型別")

    def finish(self, tree: exp.Expr) -> None:
        for select in list(tree.find_all(exp.Select)):
            _expand_group_ordinals(select)
            _qualify_star(select)
            if select.args.get("from_") is None and select.args.get("from") is None:
                select.set("from_", exp.From(this=exp.Table(this=exp.to_identifier("DUMMY"))))
        for boolean in list(tree.find_all(exp.Boolean)):
            if boolean.root() is tree and _condition_position(boolean):
                _replace_boolean(boolean)
        # 條件不能直接當成值：查詢結果或運算中的布林值寫成 CASE（保留 Null）
        for node in list(tree.walk()):
            if (
                isinstance(node, PREDICATES)
                and not isinstance(node, exp.Boolean)
                and node.root() is tree
                and not _condition_position(node)
                and not _inside_predicate(node)
            ):
                node.replace(_boolean_value(node.copy()))
        # LIKE_REGEXPR 沒有語法節點：條件都已在條件的位置，換成文字
        for node in list(tree.find_all(exp.RegexpLike)):
            if node.root() is tree:
                pattern = self._pattern(node.expression, "REGEXP_MATCH")
                node.replace(exp.Var(this=f"({self._raw(node.this)}) LIKE_REGEXPR {pattern}"))


def _toward_zero(value: exp.Expr) -> exp.Expr:
    """去掉小數（往 0 的方向）。"""
    return exp.Case(
        ifs=[
            exp.If(
                this=exp.LT(this=value.copy(), expression=_num(0)), true=exp.Ceil(this=value.copy())
            )
        ],
        default=exp.Floor(this=value),
    )


def _inside_predicate(node: exp.Expr) -> bool:
    """節點是否是另一個布林運算的運算元（AND、OR、NOT、比較的兩邊另行處理）。"""
    parent = node.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    return isinstance(parent, (exp.And, exp.Or, exp.Not))


def _boolean_value(node: exp.Expr) -> exp.Expr:
    """條件 → 布林值（Null 時為 Null）。"""
    return exp.Case(
        ifs=[
            exp.If(this=node, true=exp.true()),
            exp.If(this=exp.Not(this=_paren(node.copy())), true=exp.false()),
        ]
    )


HANA_DATE = (
    r"^[[:space:]]*[0-9]{4}-(0?[1-9]|1[0-2])-(0?[1-9]|[12][0-9]|3[01])"
    r"([ T]([01]?[0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?)?[[:space:]]*$"
)
"""文字轉日期時間前的檢查（ISO 格式）。"""


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
            exp.Rand: lambda n: _fn("RAND", _fn("CHECKSUM", _fn("NEWID"))),
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
    "redshift": RedshiftRewriter(),
    "databricks": SparkRewriter("Databricks", "databricks"),
    "spark": SparkRewriter(),
    "oracle": OracleRewriter(),
    "mysql": MySQLRewriter(),
    "snowflake": SnowflakeRewriter(),
    "tsql": TSqlRewriter(),
    "trino": TrinoRewriter(),
    "athena": TrinoRewriter("Athena", "athena"),
    "bigquery": BigQueryRewriter(),
    "teradata": TeradataRewriter(),
    "hive": HiveRewriter(),
    "impala": ImpalaRewriter(),
    "db2": Db2Rewriter(),
    "hana": HanaRewriter(),
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
    "underlying_query",
]
