"""Live 連線與關聯樹（N1）：來源資料庫的連線資訊、資料表、自訂 SQL 與 join。

- 只記連線資訊（類型、主機、連接埠、資料庫、結構描述、Databricks 的 HTTP 路徑），不記帳號、密碼或權杖；
  部署環境依 connection_id 提供憑證
- 連線類型對應 SQLGlot 的方言；對應不到的類型 dialect 為 None，報表列為不支援
- 關聯樹：資料表（type='table'）、自訂 SQL（type='text'）與 join（type='join'，條件為欄位或常值的比較，
  以 AND 連接）；
  union、Relationship 模型等其他寫法列為不支援
"""

from __future__ import annotations

import hashlib
import re

from lxml import etree

from tmig.ir.models import (
    Dialect,
    JoinCondition,
    JoinRelation,
    JoinTerm,
    LiveConnection,
    Relation,
    SqlRelation,
    TableRelation,
)
from tmig.parser.context import ParseContext
from tmig.parser.ids import slugify, strip_brackets
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import child, children, local_name

DIALECTS: dict[str, Dialect] = {
    "oracle": "oracle",
    "databricks": "databricks",
    "spark": "spark",
    "postgres": "postgres",
    "greenplum": "postgres",
    "redshift": "redshift",
    "sqlserver": "tsql",
    "azure_sql_dw": "tsql",
    "azure_sqldb": "tsql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "teradata": "teradata",
}
"""Tableau 的連線類型 → SQLGlot 方言（類型名稱依公開資料整理，待真實報表確認）。"""

EXECUTABLE: frozenset[str] = frozenset({"oracle", "databricks", "spark", "postgres"})
"""已有方言轉譯與執行器的方言（與 tmig.sqlgen.dialect.SUPPORTED 一致，測試會檢查）。"""

FILE_CLASSES = frozenset({"excel-direct", "textscan", "excel", "csv", "hyper", "dataengine"})
JOIN_KINDS = {"inner": "inner", "left": "left", "right": "right", "full": "full"}
COMPARISONS = frozenset({"=", "!=", "<", "<=", ">", ">="})
NUMBER = re.compile(r"-?\d+(\.\d+)?")


def connection_id(dbclass: str, server: str | None, database: str | None) -> str:
    """連線代號：類型、主機與資料庫轉成小寫英數字；太長或轉不出來時以雜湊代替。"""
    parts = [p for p in (dbclass, server, database) if p]
    slug = "_".join(s for s in (slugify(p) for p in parts) if s)
    if not slug or len(slug) > 60:
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
        slug = f"{slugify(dbclass) or 'conn'}_{digest}"
    return slug


def live_connection(
    ctx: ParseContext,
    named: etree._Element,
    relation: etree._Element | None,
    caption: str,
) -> LiveConnection | None:
    """具名連線的 <connection> 與關聯樹 → LiveConnection；不支援時列入 unsupported 並回傳 None。"""
    dbclass = named.get("class", "")
    if dbclass in FILE_CLASSES:
        ctx.flag(named, f"資料源「{caption}」連的是檔案（{dbclass}），檔案型 Live 連線尚不支援")
        return None
    dialect = DIALECTS.get(dbclass)
    if dialect is None:
        ctx.flag(named, f"資料源「{caption}」的連線類型 {dbclass or '（未標示）'} 尚不支援")
    elif dialect not in EXECUTABLE:
        ctx.flag(named, f"資料源「{caption}」的連線類型 {dbclass} 尚無 SQL 轉譯與執行器")
    server = named.get("server") or None
    database = named.get("service") or named.get("v-catalog") or named.get("dbname") or None
    port_text = named.get("port") or ""
    tree = parse_relation(ctx, relation, caption) if relation is not None else None
    if relation is not None and tree is None:
        return None
    return LiveConnection(
        kind="live",
        connection_id=connection_id(dbclass, server, database),
        dbclass=dbclass,
        dialect=dialect,
        server=server,
        port=int(port_text) if port_text.isdigit() else None,
        database=database,
        schema_name=named.get("schema") or None,
        http_path=named.get("v-http-path") or None,
        authentication=named.get("authentication") or None,
        relation=tree,
    )


def parse_relation(ctx: ParseContext, element: etree._Element, caption: str) -> Relation | None:
    kind = element.get("type", "")
    name = strip_brackets(element.get("name", "")) or kind
    if kind == "table":
        parts = qualified_parts(element.get("table", ""))
        if not parts:
            ctx.flag(element, f"資料源「{caption}」的資料表 {element.get('table')} 無法解析")
            return None
        return TableRelation(kind="table", name=name, table=parts)
    if kind == "text":
        sql = (element.text or "").strip()
        if not sql:
            ctx.flag(element, f"資料源「{caption}」的自訂 SQL 是空的")
            return None
        return SqlRelation(kind="sql", name=name, sql=sql)
    if kind == "join":
        join = JOIN_KINDS.get(element.get("join", "inner"))
        sides = [r for r in children(element) if local_name(r) == "relation"]
        if join is None or len(sides) != 2:
            ctx.flag(element, f"資料源「{caption}」的 join（{element.get('join')}）無法解析")
            return None
        left = parse_relation(ctx, sides[0], caption)
        right = parse_relation(ctx, sides[1], caption)
        if left is None or right is None:
            return None
        clause = child(element, "clause")
        condition = child(clause, "expression")
        on = _conditions(condition) if condition is not None else None
        if not on:
            ctx.flag(
                clause if clause is not None else element,
                f"資料源「{caption}」的 join 條件不是欄位比較（例如含計算），尚不支援",
            )
            return None
        return JoinRelation(kind="join", join=join, left=left, right=right, on=on)  # type: ignore[arg-type]
    ctx.flag(element, f"資料源「{caption}」的資料表關聯方式 {kind or '（未標示）'} 尚不支援")
    return None


def _conditions(expression: etree._Element) -> list[JoinCondition] | None:
    """<expression op='='>[A].[x]、[B].[y]</expression>，或以 op='AND' 連接的多個比較。"""
    op = expression.get("op", "")
    operands = children(expression, "expression")
    if op.upper() == "AND":
        result: list[JoinCondition] = []
        for item in operands:
            found = _conditions(item)
            if found is None:
                return None
            result.extend(found)
        return result
    if op not in COMPARISONS or len(operands) != 2:
        return None
    left, right = _term(operands[0]), _term(operands[1])
    if left is None or right is None or (left.table is None and right.table is None):
        return None
    return [JoinCondition(left=left, op=op, right=right)]  # type: ignore[arg-type]


def _term(expression: etree._Element) -> JoinTerm | None:
    """[T].[欄位]、數字或字串常值；含函式的運算式（join 計算）回傳 None。"""
    if children(expression):
        return None
    text = expression.get("op", "")
    parts = qualified_parts(text)
    if len(parts) == 2 and text.startswith("["):
        return JoinTerm(table=parts[0], column=parts[1])
    if NUMBER.fullmatch(text):
        return JoinTerm(literal=float(text) if "." in text else int(text))
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return JoinTerm(literal=text[1:-1])
    return None


def relation_tables(relation: Relation | None) -> list[str]:
    """關聯樹中所有資料表與自訂 SQL 的名稱，依出現順序。"""
    if relation is None:
        return []
    if isinstance(relation, JoinRelation):
        return relation_tables(relation.left) + relation_tables(relation.right)
    return [relation.name]
