"""資料源的來源：萃取的單一資料表，或 Live 連線的資料表、自訂 SQL 與 join 樹（N1、N5），
以及 Relationship 模型（N6，見 tmig.sqlgen.model）。

工作表 SQL 一律以 DuckDB 方言產生。Live 連線與 Relationship 模型時，第一段（base）把每個實體欄位
以欄位 id 為別名選出（projected），之後的運算只以別名參照，各資料表的同名欄位因此不會衝突；
自訂 SQL 以佔位資料表表示，轉成來源資料庫的方言後才換回原文（見 tmig.sqlgen.dialect），原文不經過轉譯。
"""

from __future__ import annotations

from dataclasses import dataclass

from tmig.data import udf
from tmig.formula.analyze import Target
from tmig.ir.models import (
    DataField,
    Datasource,
    ExtractConnection,
    FileConnection,
    JoinCondition,
    JoinRelation,
    JoinTerm,
    LiveConnection,
    ModelRelation,
    PivotRelation,
    ProjectRelation,
    Relation,
    SqlRelation,
    TableRelation,
    UnionRelation,
)
from tmig.sqlgen.sql import ident, literal

CUSTOM_SQL_PREFIX = "__tmig_custom_sql_"
"""自訂 SQL 的佔位資料表名稱前綴：__tmig_custom_sql_0、__tmig_custom_sql_1…"""


class SourceError(ValueError):
    """資料源的來源無法轉成 SQL。"""


@dataclass
class Source:
    """資料源的 FROM 子句與欄位參照方式。"""

    from_sql: str
    """萃取：資料表；Live：base CTE 的完整 SELECT（以欄位 id 為別名）；
    Relationship 模型：空字串（依檢視用到的資料表另外產生）。"""
    projected: bool
    """base CTE 以欄位 id 為別名選出實體欄位（Live 連線與 Relationship 模型）。"""
    custom_sql: list[str]
    """自訂 SQL 原文，依佔位資料表的編號。"""
    model: ModelRelation | None = None

    def column(self, data_field: DataField) -> str:
        """之後各段參照這個實體欄位時用的欄名。"""
        assert data_field.column is not None
        return data_field.id if self.projected else data_field.column


def datasource_source(ds: Datasource) -> Source:
    """萃取為單一資料表（或 Relationship 模型）；Live 連線與檔案型資料源依關聯樹（檔案匯入後的
    資料表為 Files.<name>，與 Live 連線相同以欄位 id 為別名選出）。"""
    connection = ds.connection
    if isinstance(connection, ExtractConnection):
        if connection.model is not None:
            return Source("", projected=True, custom_sql=[], model=connection.model)
        table = ".".join(ident(p) for p in connection.table.split("."))
        return Source(from_sql=table, projected=False, custom_sql=[])
    relation = connection.relation
    if relation is None:
        raise SourceError(f"資料源「{ds.caption}」沒有資料表")
    if isinstance(relation, ModelRelation):
        return Source("", projected=True, custom_sql=model_custom_sql(relation), model=relation)
    if isinstance(connection, FileConnection) and isinstance(relation, SqlRelation):
        raise SourceError(f"資料源「{ds.caption}」的檔案中有自訂 SQL")
    custom: list[str] = []
    columns = {
        (f.table, f.column): f.datatype
        for f in ds.fields
        if f.table is not None and f.column is not None and f.calc is None
    }
    from_sql = _relation_sql(relation, custom, columns, isinstance(connection, LiveConnection))
    aliases = relation_aliases(relation)
    names = list(aliases)
    default = names[0] if len(names) == 1 else None
    projections: list[str] = []
    for data_field in ds.fields:
        if data_field.column is None:
            continue
        table = data_field.table or default
        if table is None:
            raise SourceError(f"欄位「{data_field.caption}」沒有標明所屬的資料表")
        if table not in aliases:
            raise SourceError(f"欄位「{data_field.caption}」所屬的資料表「{table}」不在關聯樹中")
        column = f"{ident(aliases[table])}.{ident(data_field.column)}"
        if data_field.datatype == "datetime" and datetime_columns_vary(connection):
            column = f"CAST({column} AS TIMESTAMP)"
        if data_field.datatype == "spatial" and isinstance(connection, LiveConnection):
            # 資料庫的空間欄位（geography、geometry）在來源資料庫轉成 WKT 文字
            column = f"{udf.GEO_WKT}({column})"
        projections.append(f"{column} AS {ident(data_field.id)}")
    if not projections:
        raise SourceError(f"資料源「{ds.caption}」沒有實體欄位")
    return Source(
        from_sql=f"SELECT {', '.join(projections)} FROM {from_sql}",
        projected=True,
        custom_sql=custom,
    )


class _JoinScope:
    """join 計算中的欄位參照：[資料表].[欄位] 是實體欄位（型別依資料源的欄位）。"""

    def __init__(self, columns: dict[tuple[str, str], str]) -> None:
        self.columns = columns

    def lookup(self, parts: tuple[str, ...]) -> Target | None:
        if len(parts) != 2 or tuple(parts) not in self.columns:
            return None
        table, column = parts
        datatype = self.columns[(table, column)]
        return Target("field", f"{table}\x1f{column}", datatype, "dimension")  # type: ignore[arg-type]


class _JoinEnv:
    week_start = "sunday"

    def __init__(self, aliases: dict[str, str] | None = None, live: bool = False) -> None:
        self.aliases = aliases or {}
        self.live = live

    def column(self, target: Target) -> str:
        table, column = target.id.split("\x1f", 1)
        sql = f"{ident(self.aliases.get(table, table))}.{ident(column)}"
        if self.live and target.datatype == "spatial":
            # Live 連線：資料庫的空間欄位轉成 WKT（空間函式在 DuckDB 計算，見 tmig.data.live）
            return f"{udf.GEO_WKT}({sql})"
        return sql

    def param(self, target: Target) -> str:
        raise SourceError("join 計算不能用到參數")

    def param_value(self, target: Target) -> object:
        raise SourceError("join 計算不能用到參數")

    def lod(self, node: object) -> str:
        raise SourceError("join 計算不能用到 LOD")

    def aggregate(self, node: object) -> str:
        raise SourceError("join 計算不能彙總")

    def table_calc(self, node: object) -> str:
        raise SourceError("join 計算不能用到 Table Calculation")


def join_formula(
    text: str,
    columns: dict[tuple[str, str], str],
    aliases: dict[str, str] | None = None,
    live: bool = False,
) -> str:
    """join 計算（Tableau 的公式，以 [資料表].[欄位] 參照實體欄位）→ DuckDB 的 SQL；aliases 為資料表
    在 FROM 子句中的別名（在樞紐、聯集的子查詢中時）；live：Live 連線（資料庫的空間欄位轉成 WKT）。"""
    from tmig.formula.analyze import Analyzer
    from tmig.formula.lexer import FormulaSyntaxError
    from tmig.formula.parser import parse_formula
    from tmig.formula.types import FormulaSemanticError
    from tmig.sqlgen.expr import ExprCompiler

    try:
        node = parse_formula(text)
        return ExprCompiler(Analyzer(_JoinScope(columns)), _JoinEnv(aliases, live)).row(node)  # type: ignore[arg-type]
    except (FormulaSyntaxError, FormulaSemanticError) as exc:
        raise SourceError(f"join 計算「{text}」：{exc}") from exc


def join_formula_type(text: str, columns: dict[tuple[str, str], str]) -> str:
    """join 計算的結果型別（例如 MAKEPOINT 為 spatial）。"""
    from tmig.formula.analyze import Analyzer
    from tmig.formula.lexer import FormulaSyntaxError
    from tmig.formula.parser import parse_formula
    from tmig.formula.types import FormulaSemanticError

    try:
        return Analyzer(_JoinScope(columns)).analyze(parse_formula(text)).datatype  # type: ignore[arg-type]
    except (FormulaSyntaxError, FormulaSemanticError) as exc:
        raise SourceError(f"join 計算「{text}」：{exc}") from exc


def datetime_columns_vary(connection: object) -> bool:
    """BigQuery 的日期時間欄位可能是 DATETIME（沒有時區）或 TIMESTAMP（UTC 的時間點），兩者不能直接比較、
    加減的函式也不同：base 一律轉成沒有時區的時間（DuckDB 的 TIMESTAMP 即 BigQuery 的 DATETIME，
    TIMESTAMP 依 UTC 換算，與 Tableau 相同）。"""
    return isinstance(connection, LiveConnection) and connection.dialect == "bigquery"


def model_custom_sql(model: ModelRelation) -> list[str]:
    """Relationship 模型中各邏輯資料表的自訂 SQL，依資料表的順序（內含 join 時依 join 樹中的順序，
    即佔位資料表的編號）。"""
    return [leaf.sql for t in model.tables for leaf in sql_leaves(t.relation)]


def sql_leaves(relation: Relation) -> list[SqlRelation]:
    """關聯樹中的自訂 SQL，依 FROM 子句中的順序。"""
    if isinstance(relation, JoinRelation):
        return sql_leaves(relation.left) + sql_leaves(relation.right)
    if isinstance(relation, PivotRelation | ProjectRelation):
        return sql_leaves(relation.source)
    if isinstance(relation, UnionRelation):
        return [leaf for part in relation.parts for leaf in sql_leaves(part.relation)]
    return [relation] if isinstance(relation, SqlRelation) else []


def relation_aliases(relation: Relation) -> dict[str, str]:
    """關聯樹中的資料表名稱 → FROM 子句中參照它的別名：資料表、自訂 SQL 是自己；樞紐、聯集與 project
    是子查詢，裡面的資料表與它自己都以子查詢的別名（它的名稱）參照。"""
    if isinstance(relation, JoinRelation):
        return {**relation_aliases(relation.left), **relation_aliases(relation.right)}
    if isinstance(relation, PivotRelation | ProjectRelation):
        inner = relation_aliases(relation.source)
        return {**dict.fromkeys(inner, relation.name), relation.name: relation.name}
    if isinstance(relation, UnionRelation):
        inner = {name for part in relation.parts for name in relation_aliases(part.relation)}
        return {**dict.fromkeys(inner, relation.name), relation.name: relation.name}
    return {relation.name: relation.name}


SQL_TYPES = {
    "string": "VARCHAR",
    "integer": "BIGINT",
    "real": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "spatial": "VARCHAR",
}
"""IR 的型別 → DuckDB 的型別（樞紐、聯集的欄位一律轉成記下的型別）。"""
UNION_SOURCES = ("Table Name", "Sheet", "Path")
"""聯集另加的來源欄位：資料表的名稱、Excel 的工作表、檔案的路徑。"""


def from_clause(
    relation: Relation,
    custom: list[str],
    columns: dict[tuple[str, str], str] | None = None,
    live: bool = False,
) -> str:
    """關聯樹 → FROM 子句（見 _relation_sql）；自訂 SQL 依序加進 custom，佔位資料表的編號接在後面。"""
    return _relation_sql(relation, custom, columns, live)


def transform_select(
    relation: PivotRelation | UnionRelation | ProjectRelation,
    custom: list[str],
    needed: dict[str, str],
) -> str:
    """Relationship 模型的邏輯資料表是樞紐、聯集或 project 時的子查詢（SELECT）：選出 needed 中的欄
    （欄名 → 型別），自訂 SQL 依序加進 custom。"""
    return _FromBuilder(relation, custom, {}).transform(relation, needed)


SPATIAL_KEY_PREFIX = "__tmig_geo_"
"""空間 join 的形狀與範圍：在資料表的子查詢中先算好（每列一次），join 條件只比較這些欄。"""


def _relation_sql(
    relation: Relation,
    custom: list[str],
    columns: dict[tuple[str, str], str] | None = None,
    live: bool = False,
) -> str:
    """關聯樹 → FROM 子句。columns：各資料表的實體欄位的型別（join 計算的欄位參照用）；live：Live 連線。"""
    return _FromBuilder(relation, custom, columns or {}, live).sql(relation)


class _FromBuilder:
    """關聯樹的 FROM 子句。空間 join（Intersects）的兩邊若只用到一張資料表的欄位，形狀與它的範圍在
    那張資料表的子查詢中先算好（SELECT *, 形狀, 範圍）：DuckDB 會在每個候選的配對上重算 join 條件中的
    運算式，在 join 之前算好時，MAKEPOINT 與範圍每列只算一次。

    Live 連線（live）：兩邊都是資料庫的空間欄位時，與 Tableau 相同以該資料庫的 Intersects 在資料庫計算
    （tmig_geo_db_intersects，轉譯時換成 PostGIS 的 ST_Intersects 等）；一邊是 join 計算（例如 MAKEPOINT）時
    在 DuckDB 計算，資料庫的空間欄位轉成 WKT（取回資料表後在本機執行，見 tmig.data.live.execute_rows）。"""

    def __init__(
        self,
        relation: Relation,
        custom: list[str],
        columns: dict[tuple[str, str], str],
        live: bool = False,
    ) -> None:
        self.custom = custom
        self.columns = columns
        self.live = live
        self.aliases = relation_aliases(relation)
        # 空間 join 的形狀只在直接出現在 FROM 中的資料表先算（樞紐、聯集裡的資料表不算）
        self.leaves = {name for name, alias in self.aliases.items() if name == alias} & set(
            _relation_names(relation)
        )
        self.needed: dict[str, dict[str, str]] = {}
        """樞紐、聯集與 project 的子查詢要選出的欄：別名 → 欄名 → 型別（欄位與 join 條件用到的）。"""
        self.transforms = _transform_names(relation)
        for (table, column), datatype in columns.items():
            alias = self._transform_alias(table)
            if alias is not None:
                self.needed.setdefault(alias, {}).setdefault(column, datatype)
        self._join_columns(relation)
        self.keys: dict[str, list[tuple[str, str]]] = {}
        """資料表 → 先算好的形狀（欄名、運算式）；範圍是欄名加上 _box。"""
        self.replaced: dict[int, tuple[str, str]] = {}
        """join 條件中的一邊（id(JoinTerm)）→ 先算好的形狀與範圍的參照。"""
        self._collect(relation)

    def _collect(self, relation: Relation) -> None:
        if not isinstance(relation, JoinRelation):
            return
        self._collect(relation.left)
        self._collect(relation.right)
        for condition in relation.on:
            if condition.op != "intersects" or self._native(condition):
                continue
            for term in (condition.left, condition.right):
                leaf = self._leaf(term)
                if leaf is None:
                    continue
                shape = f"{SPATIAL_KEY_PREFIX}{len(self.replaced)}"
                expression = _term(term, self.columns, live=self.live)
                self.keys.setdefault(leaf, []).append((shape, expression))
                self.replaced[id(term)] = (
                    f"{ident(leaf)}.{ident(shape)}",
                    f"{ident(leaf)}.{ident(shape + '_box')}",
                )

    def _native(self, condition: JoinCondition) -> bool:
        """Live 連線的空間 join，兩邊都是資料庫的空間欄位：在資料庫以它的 Intersects 計算。"""
        return self.live and all(
            term.table is not None
            and term.column is not None
            and self.columns.get((term.table, term.column)) == "spatial"
            for term in (condition.left, condition.right)
        )

    def _leaf(self, term: JoinTerm) -> str | None:
        """join 條件的一邊只用到的那張資料表（欄位或只參照一張資料表的 join 計算）。"""
        if term.table is not None:
            return term.table if term.table in self.leaves else None
        if term.formula is None:
            return None
        from tmig.formula.ast import FieldRef, walk
        from tmig.formula.lexer import FormulaSyntaxError
        from tmig.formula.parser import parse_formula

        try:
            node = parse_formula(term.formula)
        except FormulaSyntaxError:
            return None
        tables = {n.parts[0] for n in walk(node) if isinstance(n, FieldRef) and len(n.parts) == 2}
        if len(tables) != 1:
            return None
        (table,) = tables
        return table if table in self.leaves else None

    def sql(self, relation: Relation) -> str:
        if isinstance(relation, TableRelation):
            table = ".".join(ident(p) for p in relation.table)
            return self._keyed(f"{table} AS {ident(relation.name)}", relation.name)
        if isinstance(relation, SqlRelation):
            self.custom.append(relation.sql)
            placeholder = ident(CUSTOM_SQL_PREFIX + str(len(self.custom) - 1))
            return self._keyed(f"{placeholder} AS {ident(relation.name)}", relation.name)
        if isinstance(relation, PivotRelation | UnionRelation | ProjectRelation):
            needed = self.needed.get(relation.name, {})
            return f"({self.transform(relation, needed)}) AS {ident(relation.name)}"
        assert isinstance(relation, JoinRelation)
        keyword = {"inner": "INNER", "left": "LEFT", "right": "RIGHT", "full": "FULL"}[
            relation.join
        ]
        left = self.sql(relation.left)
        right = self.sql(relation.right)
        if isinstance(relation.right, JoinRelation):
            right = f"({right})"
        conditions = " AND ".join(self._condition(c) for c in relation.on)
        return f"{left} {keyword} JOIN {right} ON {conditions}"

    def _join_columns(self, relation: Relation) -> None:
        """join 條件用到樞紐、聯集與 project 的欄時，子查詢也要選出那一欄。"""
        if not isinstance(relation, JoinRelation):
            return
        self._join_columns(relation.left)
        self._join_columns(relation.right)
        for condition in relation.on:
            for term in (condition.left, condition.right):
                refs = [(term.table, term.column)] if term.table and term.column else []
                if term.formula is not None:
                    refs += _formula_refs(term.formula)
                for table, column in refs:
                    alias = self._transform_alias(str(table))
                    if alias is not None:
                        datatype = self.columns.get((str(table), str(column)), "string")
                        self.needed.setdefault(alias, {}).setdefault(str(column), datatype)

    def _transform_alias(self, table: str) -> str | None:
        """資料表在樞紐、聯集或 project 的子查詢中（或就是它）時，子查詢的別名。"""
        alias = self.aliases.get(table)
        if alias is None or (alias == table and table not in self.transforms):
            return None
        return alias

    def transform(
        self, relation: PivotRelation | UnionRelation | ProjectRelation, needed: dict[str, str]
    ) -> str:
        """樞紐、聯集與 project 的子查詢（SELECT，不含別名）：選出 needed 中的欄（依欄名）。"""
        if isinstance(relation, PivotRelation):
            return self._pivot(relation, needed)
        if isinstance(relation, UnionRelation):
            return self._union(relation, needed)
        return self._project(relation, needed)

    def _source(self, relation: Relation, needed: dict[str, str]) -> tuple[str, str]:
        """樞紐、project 的來源：（FROM 中的寫法、參照它的別名）。"""
        if isinstance(relation, TableRelation | SqlRelation):
            return self.sql(relation), relation.name
        if isinstance(relation, PivotRelation | UnionRelation | ProjectRelation):
            return f"({self.transform(relation, needed)}) AS {ident(relation.name)}", relation.name
        raise SourceError("樞紐與 project 的來源是 join，尚不支援")

    def _pivot(self, relation: PivotRelation, needed: dict[str, str]) -> str:
        """欄轉成列：來源的每一列對每個樞紐的欄位各一列（UNION ALL，Null 也保留）。"""
        pivoted = {c.column for c in relation.columns}
        keep = {c: t for c, t in needed.items() if c not in (relation.names, relation.values)}
        if pivoted & set(keep):
            raise SourceError(
                f"樞紐「{relation.name}」的欄位 {sorted(pivoted & set(keep))} 已轉成列"
            )
        from_sql, alias = self._source(relation.source, keep)
        kept = [f"{ident(alias)}.{ident(c)} AS {ident(c)}" for c in keep]
        value_type = SQL_TYPES[relation.datatype]
        selects = [
            "SELECT "
            + ", ".join(
                [
                    *kept,
                    f"{literal(c.label, 'string')} AS {ident(relation.names)}",
                    f"CAST({ident(alias)}.{ident(c.column)} AS {value_type}) "
                    f"AS {ident(relation.values)}",
                ]
            )
            + f" FROM {from_sql}"
            for c in relation.columns
        ]
        return " UNION ALL ".join(selects)

    def _union(self, relation: UnionRelation, needed: dict[str, str]) -> str:
        """資料表上下相接：欄位依名稱對應，沒有這一欄的資料表為 Null；Table Name、Sheet、Path 是來源。"""
        wanted = dict(needed)
        if not wanted:
            wanted = {"Table Name": "string"}  # 只算列數時也要有一欄
        selects: list[str] = []
        for part in relation.parts:
            from_sql = self.sql(part.relation)
            alias = part.relation.name
            known = {c.name: c.datatype for c in part.columns} if part.columns is not None else None
            items: list[str] = []
            for column, datatype in wanted.items():
                sql_type = SQL_TYPES[datatype]
                if column in UNION_SOURCES and (known is None or column not in known):
                    value = {
                        "Table Name": part.relation.name,
                        "Sheet": part.sheet,
                        "Path": part.path,
                    }[column]
                    text = literal(value, "string") if value is not None else "NULL"
                    items.append(f"CAST({text} AS {sql_type}) AS {ident(column)}")
                elif known is None or column in known:
                    items.append(
                        f"CAST({ident(alias)}.{ident(column)} AS {sql_type}) AS {ident(column)}"
                    )
                else:
                    items.append(f"CAST(NULL AS {sql_type}) AS {ident(column)}")
            selects.append(f"SELECT {', '.join(items)} FROM {from_sql}")
        return " UNION ALL ".join(selects)

    def _project(self, relation: ProjectRelation, needed: dict[str, str]) -> str:
        """在來源上加上計算的欄位（合併不相符的欄位）；來源的欄位照樣帶著。"""
        computed = {c.name: c for c in relation.columns}
        source_needed = {c: t for c, t in needed.items() if c not in computed}
        source_name = relation.source.name if not isinstance(relation.source, JoinRelation) else ""
        types: dict[tuple[str, str], str] = {}
        for column in relation.columns:
            if column.name not in needed:
                continue
            for table, ref in _formula_refs(column.formula):
                datatype = self._source_type(relation.source, str(ref))
                source_needed.setdefault(str(ref), datatype)
                types[(str(table), str(ref))] = datatype
        from_sql, alias = self._source(relation.source, source_needed)
        items = [f"{ident(alias)}.{ident(c)} AS {ident(c)}" for c in source_needed]
        for name, column in computed.items():
            if name not in needed:
                continue
            # 公式以 [來源].[欄位] 參照：來源的子查詢以來源的名稱為別名
            formula = join_formula(
                column.formula, {**types, **{(source_name, k): v for k, v in source_needed.items()}}
            )
            items.append(f"CAST(({formula}) AS {SQL_TYPES[needed[name]]}) AS {ident(name)}")
        if not items:
            items = [f"1 AS {ident('__one')}"]
        return f"SELECT {', '.join(items)} FROM {from_sql}"

    def _source_type(self, relation: Relation, column: str) -> str:
        """來源中一欄的型別：聯集依各資料表記下的型別（不一致時取能容納所有值的型別），其他為字串。"""
        if isinstance(relation, UnionRelation):
            found = {
                c.datatype
                for part in relation.parts
                for c in part.columns or []
                if c.name == column
            }
            return _common_type(found)
        if isinstance(relation, JoinRelation):
            return "string"
        for (table, name), datatype in self.columns.items():
            if name == column and self.aliases.get(table) == relation.name:
                return datatype
        return "string"

    def _keyed(self, from_sql: str, name: str) -> str:
        keys = self.keys.get(name)
        if not keys:
            return from_sql
        shapes = ", ".join(f"{expression} AS {ident(alias)}" for alias, expression in keys)
        boxes = ", ".join(
            f"{udf.GEO_BOUNDS}({ident(alias)}) AS {ident(alias + '_box')}" for alias, _ in keys
        )
        inner = f"(SELECT *, {shapes} FROM {from_sql}) AS {ident(name)}"
        return f"(SELECT *, {boxes} FROM {inner}) AS {ident(name)}"

    def _condition(self, condition: JoinCondition) -> str:
        if condition.op != "intersects":
            left = _term(condition.left, self.columns, self.aliases)
            right = _term(condition.right, self.columns, self.aliases)
            return f"{left} {condition.op} {right}"
        if self._native(condition):
            left = _term(condition.left, self.columns, self.aliases)
            right = _term(condition.right, self.columns, self.aliases)
            return f"{udf.GEO_DB_INTERSECTS}({left}, {right})"
        sides: list[tuple[str, str]] = []
        for term in (condition.left, condition.right):
            found = self.replaced.get(id(term))
            if found is None:
                expression = _term(term, self.columns, self.aliases, self.live)
                found = (expression, f"{udf.GEO_BOUNDS}({expression})")
            sides.append(found)
        (left, left_box), (right, right_box) = sides
        return spatial_join(left, right, left_box, right_box)


def spatial_join(
    left: str, right: str, left_box: str | None = None, right_box: str | None = None
) -> str:
    """空間 join（Intersects）：兩邊的範圍（[west, south, east, north]）重疊且形狀相交
    （tmig.geo.spatial.intersects）。範圍的不等式讓 DuckDB 以範圍 join 縮小配對。"""
    a = left_box or f"{udf.GEO_BOUNDS}({left})"
    b = right_box or f"{udf.GEO_BOUNDS}({right})"
    return (
        f"({a}[1] <= {b}[3] AND {b}[1] <= {a}[3] AND {a}[2] <= {b}[4] AND {b}[2] <= {a}[4] "
        f"AND {udf.GEO_INTERSECTS}({left}, {right}))"
    )


def _term(
    term: JoinTerm,
    columns: dict[tuple[str, str], str] | None = None,
    aliases: dict[str, str] | None = None,
    live: bool = False,
) -> str:
    """join 條件的一邊；live：在 DuckDB 計算的空間 join（資料庫的空間欄位轉成 WKT）。"""
    if term.table is not None and term.column is not None:
        table = (aliases or {}).get(term.table, term.table)
        sql = f"{ident(table)}.{ident(term.column)}"
        if live and (columns or {}).get((term.table, term.column)) == "spatial":
            return f"{udf.GEO_WKT}({sql})"
        return sql
    if term.formula is not None:
        return f"({join_formula(term.formula, columns or {}, aliases, live)})"
    value = term.literal
    kind = "string" if isinstance(value, str) else "real" if isinstance(value, float) else "integer"
    return literal(value, kind)


def _relation_names(relation: Relation) -> list[str]:
    """直接出現在 FROM 中的資料表、自訂 SQL 與子查詢（樞紐、聯集、project）的名稱。"""
    if isinstance(relation, JoinRelation):
        return _relation_names(relation.left) + _relation_names(relation.right)
    return [relation.name]


def _transform_names(relation: Relation) -> set[str]:
    """關聯樹中的樞紐、聯集與 project 的名稱。"""
    if isinstance(relation, JoinRelation):
        return _transform_names(relation.left) | _transform_names(relation.right)
    if isinstance(relation, PivotRelation | ProjectRelation):
        return {relation.name} | _transform_names(relation.source)
    if isinstance(relation, UnionRelation):
        return {relation.name}
    return set()


def _formula_refs(formula: str) -> list[tuple[str, str]]:
    """公式中以 [資料表].[欄位] 參照的欄位。"""
    from tmig.formula.ast import FieldRef, walk
    from tmig.formula.lexer import FormulaSyntaxError
    from tmig.formula.parser import parse_formula

    try:
        node = parse_formula(formula)
    except FormulaSyntaxError:
        return []
    return [
        (n.parts[0], n.parts[1])
        for n in walk(node)
        if isinstance(n, FieldRef) and len(n.parts) == 2
    ]


def _common_type(types: set[str]) -> str:
    """聯集中同名的欄位型別不一致時（例如有的資料表是數字、有的是文字），取能容納所有值的型別；
    沒有記下型別時為字串。"""
    if not types:
        return "string"
    if len(types) == 1:
        return next(iter(types))
    if types <= {"integer", "real"}:
        return "real"
    if types <= {"date", "datetime"}:
        return "datetime"
    return "string"
