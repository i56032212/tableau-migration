"""Live 連線與關聯樹（N1）：來源資料庫的連線資訊、資料表、自訂 SQL 與 join。

- 只記連線資訊（類型、主機、連接埠、資料庫、結構描述、Databricks 的 HTTP 路徑），不記帳號、密碼或權杖；
  部署環境依 connection_id 提供憑證
- 連線類型對應 SQLGlot 的方言；對應不到的類型 dialect 為 None，報表列為不支援
- Initial SQL（連線的 one-time-sql）記在 LiveConnection.initial_sql，開啟連線時執行（tmig.data.initial_sql）；
  用到 [TableauServerUser] 時每個登入者各自一條連線、代入平台登入的帳號
- 依檢視者的身分查詢資料庫的設定列為不支援：要求檢視者輸入自己的帳號、以檢視者的身分模擬；
  Windows 整合驗證、Kerberos 與 OAuth 由功能盤點列為需注意（伺服器上可能以服務帳號連線）
- 關聯樹：資料表（type='table'）、自訂 SQL（type='text'）與 join（type='join'，條件為欄位或常值的比較、
  join 計算或空間 join，以 AND 連接）、樞紐（type='pivot'）、聯集（type='union'）與合併不相符的欄位
  （type='project'）；其他寫法列為不支援
"""

from __future__ import annotations

import hashlib
import re

from lxml import etree

from tmig.formula.ast import FieldRef, walk
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.ir.models import (
    Dialect,
    JoinCondition,
    JoinRelation,
    JoinTerm,
    LiveConnection,
    PivotColumn,
    PivotRelation,
    ProjectColumn,
    ProjectRelation,
    Relation,
    SqlRelation,
    TableRelation,
    UnionColumn,
    UnionPart,
    UnionRelation,
    UnionWildcard,
)
from tmig.parser.context import ParseContext
from tmig.parser.ids import slugify, strip_brackets
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import child, children, local_name
from tmig.parser.wildcard import WILDCARD_ATTRS

UNION_TYPES = frozenset({"string", "integer", "real", "boolean", "date", "datetime", "spatial"})
"""聯集中的資料表記下的欄位型別。"""

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
    "presto": "trino",
    "trino": "trino",
    "athena": "athena",
    "bigquery": "bigquery",
    "teradata": "teradata",
    "hadoophive": "hive",
    "hive": "hive",
    "hortonworkshadoophive": "hive",
    "maprhadoophive": "hive",
    "awshadoophive": "hive",
    "impala": "impala",
    "db2": "db2",
    "saphana": "hana",
    # Amazon Aurora for MySQL、Google Cloud SQL（MySQL）：MySQL 的協定與 SQL
    "aurora": "mysql",
    "googlecloudsql": "mysql",
}
"""Tableau 的連線類型 → SQLGlot 方言（類型名稱依公開資料整理，待真實報表確認）。"""

EXECUTABLE: frozenset[str] = frozenset(
    {
        "oracle",
        "databricks",
        "spark",
        "postgres",
        "redshift",
        "tsql",
        "mysql",
        "snowflake",
        "trino",
        "athena",
        "bigquery",
        "teradata",
        "hive",
        "impala",
        "db2",
        "hana",
    }
)
"""已有方言轉譯與執行器的方言（與 tmig.sqlgen.dialect.SUPPORTED 一致，測試會檢查）。"""

FILE_CLASSES = frozenset(
    {"excel-direct", "textscan", "excel", "csv", "hyper", "dataengine", "ogrdirect"}
)
"""檔案型的連線類型（ogrdirect 是 Shapefile、GeoJSON、KML 等空間檔案）。"""

INITIAL_SQL_PARAM = re.compile(r"\[([A-Za-z]+)\]")
INITIAL_SQL_USER = {
    "tableauserveruser": "[TableauServerUser]",
    "tableauserveruserfull": "[TableauServerUserFull]",
}
"""Initial SQL 中代表檢視者的參數（Tableau Server 執行時代入登入者的帳號）。"""
INTEGRATED_AUTH = ("sspi", "kerberos", "integrated", "windows", "gssapi", "negotiate")
"""以作業系統或網域身分登入資料庫的驗證方式（Windows 整合驗證、Kerberos）。"""
NO_AUTH = frozenset({"auth-none", "none", "noauth", "no-auth", "anonymous"})

JOIN_KINDS = {"inner": "inner", "left": "left", "right": "right", "full": "full"}
COMPARISONS = frozenset({"=", "!=", "<", "<=", ">", ">="})
OPERATORS = {"<>": "!="}
"""Tableau 的比較運算子的其他寫法（不等於寫成 <>）。"""
NUMBER = re.compile(r"-?\d+(\.\d+)?")


def initial_sql_users(text: str) -> list[str]:
    """Initial SQL 中代表檢視者的參數，例如 [TableauServerUser]。"""
    found = {
        INITIAL_SQL_USER[m.group(1).lower()]
        for m in INITIAL_SQL_PARAM.finditer(text)
        if m.group(1).lower() in INITIAL_SQL_USER
    }
    return sorted(found)


def viewer_identity_problems(named: etree._Element, caption: str) -> list[str]:
    """Live 連線依檢視者自己的資料庫帳號查詢的設定：平台以服務帳號連線，原本依人不同的資料移轉後每個人
    都看得到，所以列為不支援。

    - Tableau Server 上要求檢視者輸入自己的資料庫帳號（workgroup-auth-mode=prompt）或以檢視者的身分模擬

    Initial SQL 不在此列：平台開啟連線時會執行，[TableauServerUser] 代入平台登入的帳號（功能盤點列為需注意：
    平台的帳號要與資料庫端認得的身分相同）。
    """
    problems: list[str] = []
    mode = (named.get("workgroup-auth-mode") or "").strip()
    if mode.lower() == "prompt":
        problems.append(
            f"資料源「{caption}」在 Tableau Server 上要求檢視者輸入自己的資料庫帳號；"
            "平台以唯讀服務帳號連線，資料庫端依人不同的權限不會生效"
        )
    elif "imperson" in mode.lower():
        problems.append(
            f"資料源「{caption}」以檢視者的身分模擬（{mode}）查詢資料庫；"
            "平台以唯讀服務帳號連線，資料庫端依人不同的權限不會生效"
        )
    return problems


IMPALA_PORT = "21050"


def _impala_server(named: etree._Element) -> bool:
    """Cloudera Hadoop 連線連到 Impala（連線的類型或方言標示 Impala，或 Impala 的 HiveServer2 連接埠 21050）。"""
    if named.get("port") == IMPALA_PORT:
        return True
    return any(
        "impala" in str(value or "").lower()
        for key, value in named.attrib.items()
        if "type" in str(key) or "dialect" in str(key)
    )


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
    if dbclass == "hadoophive" and _impala_server(named):
        dialect = "impala"
    if dialect is None:
        ctx.flag(named, f"資料源「{caption}」的連線類型 {dbclass or '（未標示）'} 尚不支援")
    elif dialect not in EXECUTABLE:
        ctx.flag(named, f"資料源「{caption}」的連線類型 {dbclass} 尚無 SQL 轉譯與執行器")
    for problem in viewer_identity_problems(named, caption):
        ctx.flag(named, problem)
    server = named.get("server") or None
    database = (
        named.get("service")
        or named.get("v-catalog")
        or named.get("dbname")
        or named.get("catalog")
        or named.get("project")
        or named.get("CATALOG")
        or None
    )
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
        schema_name=named.get("schema") or named.get("SCHEMA") or None,
        http_path=named.get("v-http-path") or None,
        authentication=named.get("authentication") or None,
        initial_sql=(named.get("one-time-sql") or "").strip() or None,
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
    if kind == "pivot":
        return _pivot(ctx, element, caption)
    if kind == "union":
        return _union(ctx, element, caption)
    if kind == "project":
        return _project(ctx, element, caption)
    ctx.flag(element, f"資料源「{caption}」的資料表關聯方式 {kind or '（未標示）'} 尚不支援")
    return None


def _inner_relations(element: etree._Element) -> list[etree._Element]:
    return [r for r in children(element) if local_name(r) == "relation"]


def _pivot(ctx: ParseContext, element: etree._Element, caption: str) -> PivotRelation | None:
    """<relation type='pivot'>：<columns> 是名稱欄與值欄，<tag> 的 <value> 是名稱欄的值，<groups> 的
    <field> 是被樞紐的來源欄位（依序對應）。"""
    name = strip_brackets(element.get("name", "")) or "Pivot"
    inner = _inner_relations(element)
    if len(inner) != 1:
        ctx.flag(element, f"資料源「{caption}」的樞紐（pivot）沒有剛好一個來源")
        return None
    source = parse_relation(ctx, inner[0], caption)
    if source is None:
        return None
    if not isinstance(source, TableRelation | SqlRelation):
        ctx.flag(element, f"資料源「{caption}」的樞紐（pivot）的來源是 {source.kind}，尚不支援")
        return None
    declared = [c for c in children(child(element, "columns"), "column") if c.get("name")]
    tag = child(element, "tag")
    group = child(child(element, "groups"), "group")
    labels = [strip_brackets(v.get("name", "")) for v in children(tag, "value")]
    fields = [qualified_parts(f.get("name", "")) for f in children(group, "field")]
    names = (tag.get("name") if tag is not None else None) or "Pivot Field Names"
    values = (group.get("name") if group is not None else None) or "Pivot Field Values"
    datatype = next(
        (c.get("datatype", "") for c in declared if strip_brackets(c.get("name", "")) == values),
        "",
    )
    if (
        not fields
        or len(labels) != len(fields)
        or any(len(f) != 2 or f[0] != source.name for f in fields)
        or datatype not in ("string", "integer", "real", "boolean", "date", "datetime")
    ):
        ctx.flag(element, f"資料源「{caption}」的樞紐（pivot）無法解析")
        return None
    return PivotRelation(
        kind="pivot",
        name=name,
        source=source,
        names=names,
        values=values,
        datatype=datatype,  # type: ignore[arg-type]
        columns=[
            PivotColumn(column=f[1], label=label) for f, label in zip(fields, labels, strict=True)
        ],
    )


def _union(ctx: ParseContext, element: etree._Element, caption: str) -> UnionRelation | None:
    """<relation type='union'>：上下相接的資料表（同一個連線），每張的 <columns> 記下它的欄位。"""
    name = strip_brackets(element.get("name", "")) or "Union"
    parts: list[UnionPart] = []
    for inner in _inner_relations(element):
        relation = parse_relation(ctx, inner, caption)
        if relation is None:
            return None
        if not isinstance(relation, TableRelation | SqlRelation):
            ctx.flag(inner, f"資料源「{caption}」的聯集中有 {relation.kind}，尚不支援")
            return None
        declared = [
            UnionColumn(name=c.get("name", ""), datatype=c.get("datatype", ""))  # type: ignore[arg-type]
            for c in children(child(inner, "columns"), "column")
            if c.get("name") and c.get("datatype") in UNION_TYPES
        ]
        table = strip_brackets(inner.get("table", ""))
        parts.append(
            UnionPart(
                relation=relation,
                columns=declared or None,
                sheet=table[:-1] if table.endswith("$") else None,
            )
        )
    if not parts:
        ctx.flag(element, f"資料源「{caption}」的聯集（union）沒有資料表")
        return None
    files = element.get(WILDCARD_ATTRS[0])
    wildcard = (
        UnionWildcard(
            files=files,
            sheets=element.get(WILDCARD_ATTRS[1]) or None,
            recursive=element.get(WILDCARD_ATTRS[2]) == "true",
        )
        if files
        else None
    )
    return UnionRelation(kind="union", name=name, parts=parts, wildcard=wildcard)


def _project(ctx: ParseContext, element: etree._Element, caption: str) -> ProjectRelation | None:
    """<relation type='project'>：合併不相符的欄位等，在來源上加上計算的欄位（<bindings> 的公式）。"""
    inner = _inner_relations(element)
    if len(inner) != 1:
        ctx.flag(element, f"資料源「{caption}」的 project 沒有剛好一個來源")
        return None
    source = parse_relation(ctx, inner[0], caption)
    if source is None:
        return None
    columns: list[ProjectColumn] = []
    project = strip_brackets(element.get("name", "")) or "Project"
    for binding in children(child(element, "bindings"), "binding"):
        parts = qualified_parts(binding.get("name", ""))
        formula = (binding.get("expression") or "").replace("\r\n", "\n")
        if len(parts) != 2 or not formula.strip():
            ctx.flag(binding, f"資料源「{caption}」的 project 欄位 {binding.get('name')} 無法解析")
            return None
        project = parts[0]
        columns.append(ProjectColumn(name=parts[1], formula=formula))
    if not columns:
        ctx.flag(element, f"資料源「{caption}」的 project 沒有計算的欄位")
        return None
    return ProjectRelation(kind="project", name=project, source=source, columns=columns)


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
    op = OPERATORS.get(op, op)
    if op.lower() == "intersects":
        # 空間 join：兩邊的形狀（空間欄位或 MAKEPOINT 等 join 計算）在空間上相交
        op = "intersects"
    if (op not in COMPARISONS and op != "intersects") or len(operands) != 2:
        return None
    left, right = _term(operands[0]), _term(operands[1])
    if left is None or right is None or (_constant(left) and _constant(right)):
        return None
    return [JoinCondition(left=left, op=op, right=right)]  # type: ignore[arg-type]


def _constant(term: JoinTerm) -> bool:
    return term.table is None and term.formula is None


def _term(expression: etree._Element) -> JoinTerm | None:
    """[T].[欄位]、數字或字串常值，或 join 計算（公式寫在 op 中，以 [T].[欄位] 參照實體欄位）。"""
    if children(expression):
        return None
    text = expression.get("op", "")
    parts = qualified_parts(text)
    if len(parts) == 2 and text.startswith("[") and text.endswith("]"):
        return JoinTerm(table=parts[0], column=parts[1])
    if NUMBER.fullmatch(text):
        return JoinTerm(literal=float(text) if "." in text else int(text))
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"" and text.count(text[0]) == 2:
        return JoinTerm(literal=text[1:-1])
    try:
        node = parse_formula(text)
    except FormulaSyntaxError:
        return None
    refs = [n for n in walk(node) if isinstance(n, FieldRef)]
    if not refs or any(len(r.parts) != 2 for r in refs):
        return None  # join 計算只能參照 [資料表].[欄位]
    return JoinTerm(formula=text.replace("\r\n", "\n"))


def relation_tables(relation: Relation | None) -> list[str]:
    """關聯樹中所有資料表與自訂 SQL 的名稱，依出現順序。"""
    if relation is None:
        return []
    if isinstance(relation, JoinRelation):
        return relation_tables(relation.left) + relation_tables(relation.right)
    if isinstance(relation, PivotRelation | ProjectRelation):
        return [relation.name, *relation_tables(relation.source)]
    if isinstance(relation, UnionRelation):
        return [relation.name]
    return [relation.name]
