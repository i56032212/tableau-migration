"""資料源的來源：萃取的單一資料表，或 Live 連線的資料表、自訂 SQL 與 join 樹（N1、N5），
以及 Relationship 模型（N6，見 tmig.sqlgen.model）。

工作表 SQL 一律以 DuckDB 方言產生。Live 連線與 Relationship 模型時，第一段（base）把每個實體欄位
以欄位 id 為別名選出（projected），之後的運算只以別名參照，各資料表的同名欄位因此不會衝突；
自訂 SQL 以佔位資料表表示，轉成來源資料庫的方言後才換回原文（見 tmig.sqlgen.dialect），原文不經過轉譯。
"""

from __future__ import annotations

from dataclasses import dataclass

from tmig.formula.analyze import Target
from tmig.ir.models import (
    DataField,
    Datasource,
    ExtractConnection,
    FileConnection,
    JoinRelation,
    JoinTerm,
    LiveConnection,
    ModelRelation,
    Relation,
    SqlRelation,
    TableRelation,
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
    from_sql = _relation_sql(relation, custom, columns)
    names = _relation_names(relation)
    default = names[0] if len(names) == 1 else None
    projections: list[str] = []
    for data_field in ds.fields:
        if data_field.column is None:
            continue
        table = data_field.table or default
        if table is None:
            raise SourceError(f"欄位「{data_field.caption}」沒有標明所屬的資料表")
        if table not in names:
            raise SourceError(f"欄位「{data_field.caption}」所屬的資料表「{table}」不在關聯樹中")
        column = f"{ident(table)}.{ident(data_field.column)}"
        if data_field.datatype == "datetime" and datetime_columns_vary(connection):
            column = f"CAST({column} AS TIMESTAMP)"
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

    def column(self, target: Target) -> str:
        table, column = target.id.split("\x1f", 1)
        return f"{ident(table)}.{ident(column)}"

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


def join_formula(text: str, columns: dict[tuple[str, str], str]) -> str:
    """join 計算（Tableau 的公式，以 [資料表].[欄位] 參照實體欄位）→ DuckDB 的 SQL。"""
    from tmig.formula.analyze import Analyzer
    from tmig.formula.lexer import FormulaSyntaxError
    from tmig.formula.parser import parse_formula
    from tmig.formula.types import FormulaSemanticError
    from tmig.sqlgen.expr import ExprCompiler

    try:
        node = parse_formula(text)
        return ExprCompiler(Analyzer(_JoinScope(columns)), _JoinEnv()).row(node)  # type: ignore[arg-type]
    except (FormulaSyntaxError, FormulaSemanticError) as exc:
        raise SourceError(f"join 計算「{text}」：{exc}") from exc


def datetime_columns_vary(connection: object) -> bool:
    """BigQuery 的日期時間欄位可能是 DATETIME（沒有時區）或 TIMESTAMP（UTC 的時間點），兩者不能直接比較、
    加減的函式也不同：base 一律轉成沒有時區的時間（DuckDB 的 TIMESTAMP 即 BigQuery 的 DATETIME，
    TIMESTAMP 依 UTC 換算，與 Tableau 相同）。"""
    return isinstance(connection, LiveConnection) and connection.dialect == "bigquery"


def model_custom_sql(model: ModelRelation) -> list[str]:
    """Relationship 模型中各邏輯資料表的自訂 SQL，依資料表的順序（佔位資料表的編號）。"""
    return [t.relation.sql for t in model.tables if isinstance(t.relation, SqlRelation)]


def _relation_sql(
    relation: Relation, custom: list[str], columns: dict[tuple[str, str], str] | None = None
) -> str:
    """關聯樹 → FROM 子句。columns：各資料表的實體欄位的型別（join 計算的欄位參照用）。"""
    if isinstance(relation, TableRelation):
        table = ".".join(ident(p) for p in relation.table)
        return f"{table} AS {ident(relation.name)}"
    if isinstance(relation, SqlRelation):
        custom.append(relation.sql)
        return f"{ident(CUSTOM_SQL_PREFIX + str(len(custom) - 1))} AS {ident(relation.name)}"
    assert isinstance(relation, JoinRelation)
    keyword = {"inner": "INNER", "left": "LEFT", "right": "RIGHT", "full": "FULL"}[relation.join]
    left = _relation_sql(relation.left, custom, columns)
    right = _relation_sql(relation.right, custom, columns)
    if isinstance(relation.right, JoinRelation):
        right = f"({right})"
    conditions = " AND ".join(
        f"{_term(c.left, columns)} {c.op} {_term(c.right, columns)}" for c in relation.on
    )
    return f"{left} {keyword} JOIN {right} ON {conditions}"


def _term(term: JoinTerm, columns: dict[tuple[str, str], str] | None = None) -> str:
    if term.table is not None and term.column is not None:
        return f"{ident(term.table)}.{ident(term.column)}"
    if term.formula is not None:
        return f"({join_formula(term.formula, columns or {})})"
    value = term.literal
    kind = "string" if isinstance(value, str) else "real" if isinstance(value, float) else "integer"
    return literal(value, kind)


def _relation_names(relation: Relation) -> list[str]:
    if isinstance(relation, JoinRelation):
        return _relation_names(relation.left) + _relation_names(relation.right)
    return [relation.name]
