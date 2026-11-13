"""跨資料庫的 join 與一般連線之間的轉換（關聯樹的資料表換成匯入或取回的 Files.<名稱>，或換回來源的
資料表與自訂 SQL）。

- to_federated：Live 連線的資料源加上檔案或另一個資料庫的資料表時，原本的資料表與自訂 SQL 成為跨資料庫的
  join 中資料庫的資料表（FileTable.format 為 live）
- single_database：跨資料庫的 join 只剩同一個資料庫的資料表（沒有檔案）時，改回一般的 Live 連線（整段查詢
  下推到資料庫，與 Tableau 只有一個連線時相同）
"""

from __future__ import annotations

from collections.abc import Callable

from tmig.ir.models import (
    FILE_SCHEMA,
    LIVE_PATH_PREFIX,
    FileColumn,
    FileConnection,
    FileTable,
    JoinRelation,
    LiveConnection,
    ModelRelation,
    PivotRelation,
    ProjectRelation,
    Relation,
    SqlRelation,
    TableRelation,
    TopRelation,
    UnionRelation,
)


def map_leaves(
    relation: TopRelation, leaf: Callable[[TableRelation | SqlRelation], Relation]
) -> TopRelation:
    """關聯樹中每張資料表（與自訂 SQL）換成 leaf 的結果，其餘的結構不變。"""
    if isinstance(relation, ModelRelation):
        tables = [
            t.model_copy(update={"relation": _map(t.relation, leaf)}) for t in relation.tables
        ]
        return relation.model_copy(update={"tables": tables})
    return _map(relation, leaf)


def _map(relation: Relation, leaf: Callable[[TableRelation | SqlRelation], Relation]) -> Relation:
    if isinstance(relation, JoinRelation):
        return relation.model_copy(
            update={"left": _map(relation.left, leaf), "right": _map(relation.right, leaf)}
        )
    if isinstance(relation, PivotRelation | ProjectRelation):
        return relation.model_copy(update={"source": _map(relation.source, leaf)})
    if isinstance(relation, UnionRelation):
        parts = [p.model_copy(update={"relation": _map(p.relation, leaf)}) for p in relation.parts]
        return relation.model_copy(update={"parts": parts})
    if isinstance(relation, TableRelation | SqlRelation):
        return leaf(relation)
    return relation


def to_federated(
    connection: LiveConnection, columns: Callable[[str], list[FileColumn]]
) -> FileConnection:
    """Live 連線 → 跨資料庫的 join：每張資料表（與自訂 SQL）成為資料庫的資料表。columns：關聯樹中資料表的
    名稱 → 它的欄（名稱與型別）。"""
    if connection.relation is None:
        raise ValueError("連線沒有資料表")
    base = connection.model_copy(update={"relation": None})
    tables: list[FileTable] = []

    def leaf(relation: TableRelation | SqlRelation) -> Relation:
        found = columns(relation.name)
        if not found:
            raise ValueError(f"資料表「{relation.name}」沒有欄位")
        tables.append(
            FileTable(
                name=relation.name,
                format="live",
                path=LIVE_PATH_PREFIX + connection.connection_id,
                connection=base,
                table=list(relation.table) if isinstance(relation, TableRelation) else None,
                sql=relation.sql if isinstance(relation, SqlRelation) else None,
                columns=found,
            )
        )
        return TableRelation(kind="table", name=relation.name, table=[FILE_SCHEMA, relation.name])

    relation = map_leaves(connection.relation, leaf)
    return FileConnection(kind="file", tables=tables, relation=relation)


def single_database(connection: FileConnection) -> LiveConnection | None:
    """跨資料庫的 join 中只有同一個資料庫的資料表（沒有檔案）時的 Live 連線；否則為 None。"""
    tables = {t.name: t for t in connection.tables}
    if not tables or any(t.format != "live" or t.connection is None for t in tables.values()):
        return None
    ids = {t.connection.connection_id for t in tables.values() if t.connection is not None}
    if len(ids) != 1:
        return None

    def leaf(relation: TableRelation | SqlRelation) -> Relation:
        found = tables.get(relation.name)
        if found is None or not isinstance(relation, TableRelation):
            return relation
        if found.sql is not None:
            return SqlRelation(kind="sql", name=relation.name, sql=found.sql)
        return TableRelation(kind="table", name=relation.name, table=list(found.table or []))

    first = next(iter(tables.values())).connection
    assert first is not None
    return first.model_copy(update={"relation": map_leaves(connection.relation, leaf)})


__all__ = ["map_leaves", "single_database", "to_federated"]
