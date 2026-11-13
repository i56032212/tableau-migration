"""跨資料庫的 join（Tableau 的 cross-database join：一個資料源有多個連線，例如 Excel 與 SQL Server、
兩個不同的資料庫）：與 Tableau 相同，各連線的資料取到本機、在本機的引擎中合併（這裡是 DuckDB）。

- 檔案（Excel、文字檔、.hyper、空間檔案）在匯入時已讀進資料源的 DuckDB 檔（Files.<名稱>）
- 資料庫的資料表與自訂 SQL（FileTable.format 為 live）在查詢時從各自的資料庫取回：資料表只取查詢用到的欄，
  自訂 SQL 取整段的結果，查詢沒有用到的資料表不取（例如 Relationship 模型中檢視沒有用到的邏輯資料表）；型別
  轉成記下的型別，以 Files.<名稱> 的檢視提供給查詢，之後整段查詢與檔案的資料源相同在 DuckDB 執行
- 取回的資料在 TTL 之內重複使用（預設 60 秒，環境變數 TMIG_FEDERATED_TTL；0 為不重複使用），例如 dashboard
  的各張工作表：同一張資料表要的欄都已取回時直接使用；同時要同一張資料表時只取一次
- 憑證依各連線的設定（connection_id），與 Live 連線相同
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from tmig.data import udf
from tmig.data.executor import ExecutionError, QueryResult, check_guard, finish
from tmig.ir.models import (
    FILE_SCHEMA,
    FileColumn,
    FileConnection,
    FileTable,
    LiveConnection,
    local_tables,
    remote_tables,
)
from tmig.sqlgen.dialect import ParameterValues, to_dialect
from tmig.sqlgen.query import WorksheetSql
from tmig.sqlgen.source import CUSTOM_SQL_PREFIX, SQL_TYPES
from tmig.sqlgen.sql import ident, string

Fetch = Callable[[LiveConnection, str], pa.Table]
"""在來源資料庫執行一段（該方言的）SQL。"""
LOCAL = "__tmig_local"
"""匯入的檔案（資料源的 DuckDB 檔）附加的名稱。"""


def referenced_columns(
    sqls: Iterable[str], connection: FileConnection
) -> dict[str, set[str]] | None:
    """查詢用到的資料表與欄（名稱一律小寫）：以 sqlglot 補上欄位的所屬（SELECT * 依記下的欄展開）、去掉 CTE
    與子查詢中沒有用到的欄之後，各段參照 Files.<名稱> 的欄。有 SELECT * 留下或分析不了時為 None（取全部的欄）。"""
    from sqlglot.optimizer.pushdown_projections import pushdown_projections
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import traverse_scope

    schema: dict[str, object] = {
        FILE_SCHEMA: {t.name: {c.name: "VARCHAR" for c in t.columns} for t in connection.tables}
    }
    files = FILE_SCHEMA.casefold()
    found: dict[str, set[str]] = {}
    try:
        for sql in sqls:
            for tree in sqlglot.parse(sql, read="duckdb"):
                if tree is None:
                    continue
                tree = qualify(
                    tree, schema=schema, dialect="duckdb", validate_qualify_columns=False
                )
                tree = pushdown_projections(tree)
                for scope in traverse_scope(tree):
                    tables = {
                        alias: source.name.casefold()
                        for alias, source in scope.sources.items()
                        if isinstance(source, exp.Table)
                        and source.db.casefold() == files
                        and not source.catalog
                    }
                    for name in tables.values():
                        found.setdefault(name, set())
                    selects = getattr(scope.expression, "selects", [])
                    if tables and any(isinstance(e, exp.Star) or e.is_star for e in selects):
                        return None
                    for column in scope.columns:
                        name = tables.get(column.table)
                        if name is not None:
                            found[name].add(column.name.casefold())
    except (SqlglotError, RecursionError, KeyError, ValueError):
        return None
    return found


def used_columns(
    table: FileTable, referenced: dict[str, set[str]] | None
) -> list[FileColumn] | None:
    """一張資料庫的資料表要取回的欄：查詢用到的欄（一欄都沒有時取第一欄，例如只算列數）；自訂 SQL 與分析
    不了時為全部的欄；查詢沒有用到這張資料表時為 None（不取回）。"""
    if referenced is None:
        return list(table.columns)
    names = referenced.get(table.name.casefold())
    if names is None:
        return None
    if table.sql is not None:
        return list(table.columns)
    return [c for c in table.columns if c.name.casefold() in names] or list(table.columns[:1])


def remote_sql(
    table: FileTable,
    columns: list[FileColumn] | None = None,
    parameters: ParameterValues | None = None,
) -> str:
    """取回一張資料庫的資料表的 SQL（該資料庫的方言）：資料表只取 columns（省略時為全部的欄），自訂 SQL 取
    整段的結果（參數代入目前值）。"""
    connection = table.connection
    if connection is None or connection.dialect is None:
        dbclass = connection.dbclass if connection is not None else "（未標示）"
        raise ExecutionError(f"資料表「{table.name}」的連線類型 {dbclass} 尚不支援")
    if table.sql is not None:
        placeholder = ident(CUSTOM_SQL_PREFIX + "0")
        return to_dialect(
            f"SELECT * FROM {placeholder} AS {ident('t')}",
            connection.dialect,
            [table.sql],
            parameters,
        )
    parts = table.table or [table.name]
    items = ", ".join(
        # 資料庫的空間欄位（geography、geometry）在來源資料庫轉成 WKT 文字
        f"{udf.GEO_WKT}({ident(c.name)}) AS {ident(c.name)}"
        if c.datatype == "spatial"
        else ident(c.name)
        for c in (columns if columns is not None else table.columns)
    )
    source = ".".join(ident(p) for p in parts)
    return to_dialect(f"SELECT {items} FROM {source}", connection.dialect)


@dataclass
class _Entry:
    stamp: float
    columns: frozenset[str]
    data: pa.Table


class FederatedCache:
    """取回的資料庫資料在 TTL 之內重複使用：同一張資料表要的欄都已取回時直接使用（之後要更多的欄時，連同已取回
    的欄一起重新取回），自訂 SQL 依代入參數後的 SQL；同時要同一張資料表時只取一次。"""

    def __init__(self, ttl: float | None = None) -> None:
        if ttl is None:
            try:
                ttl = float(os.environ.get("TMIG_FEDERATED_TTL", "60"))
            except ValueError:
                ttl = 60.0
        self.ttl = ttl
        self._items: dict[tuple[str, str], _Entry] = {}
        self._pending: dict[tuple[str, str], threading.Lock] = {}
        self._lock = threading.Lock()

    def table(
        self,
        table: FileTable,
        columns: list[FileColumn],
        fetch: Fetch,
        parameters: ParameterValues | None = None,
    ) -> pa.Table:
        connection = table.connection
        assert connection is not None
        if table.sql is not None:
            key = (connection.connection_id, remote_sql(table, None, parameters))
        else:
            key = (connection.connection_id, "\x1f".join(table.table or [table.name]))
        wanted = frozenset(c.name for c in columns)
        with self._lock:
            found = self._fresh(key, wanted)
            if found is not None:
                return found
            pending = self._pending.setdefault(key, threading.Lock())
        with pending:
            with self._lock:
                found = self._fresh(key, wanted)
                entry = self._items.get(key)
                previous = entry.columns if entry is not None else frozenset()
            if found is not None:
                return found
            union = wanted | previous
            ordered = [c for c in table.columns if c.name in union]
            data = fetch(connection, remote_sql(table, ordered, parameters))
            with self._lock:
                if self.ttl > 0:
                    self._items[key] = _Entry(time.monotonic(), frozenset(union), data)
                self._pending.pop(key, None)
            return data

    def _fresh(self, key: tuple[str, str], wanted: frozenset[str]) -> pa.Table | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stamp > self.ttl:
            self._items.pop(key, None)
            return None
        return entry.data if wanted <= entry.columns else None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def federated_connection(
    database: Path | None,
    connection: FileConnection,
    fetch: Fetch,
    cache: FederatedCache | None = None,
    sqls: Iterable[str] = (),
    parameters: ParameterValues | None = None,
) -> duckdb.DuckDBPyConnection:
    """記憶體中的 DuckDB：Files.<名稱> 是匯入的檔案（附加資料源的 DuckDB 檔）與取回的資料庫資料表。
    sqls：要執行的查詢（資料表只取其中用到的欄）。"""
    texts = list(sqls)
    con = udf.register(duckdb.connect())
    try:
        con.execute(f"CREATE SCHEMA {ident(FILE_SCHEMA)}")
        local = local_tables(connection)
        if local:
            if database is None or not database.is_file():
                raise ExecutionError(f"找不到資料檔 {database}；請先匯入資料源")
            con.execute(f"ATTACH {string(str(database))} AS {ident(LOCAL)} (READ_ONLY)")
            for table in local:
                name = ident(table.name)
                con.execute(
                    f"CREATE VIEW {ident(FILE_SCHEMA)}.{name} AS "
                    f"SELECT * FROM {ident(LOCAL)}.{ident(FILE_SCHEMA)}.{name}"
                )
        referenced = referenced_columns(texts, connection) if texts else None
        for index, table in enumerate(remote_tables(connection)):
            assert table.connection is not None
            columns = used_columns(table, referenced)
            if columns is None:
                continue  # 查詢沒有用到
            try:
                arrow = (
                    cache.table(table, columns, fetch, parameters)
                    if cache is not None
                    else fetch(table.connection, remote_sql(table, columns, parameters))
                )
            except ExecutionError:
                raise
            except Exception as exc:
                raise ExecutionError(f"來源資料庫執行失敗（{table.name}）：{exc}") from exc
            alias = f"__tmig_remote_{index}"
            con.register(alias, arrow)
            names = {name.casefold(): name for name in arrow.column_names}
            items: list[str] = []
            for column in table.columns:
                # 沒有取回的欄（查詢用不到，例如 base 中其他的欄位）是同型別的 Null
                found = names.get(column.name.casefold())
                value = ident(found) if found is not None else "NULL"
                items.append(
                    f"CAST({value} AS {SQL_TYPES[column.datatype]}) AS {ident(column.name)}"
                )
            con.execute(
                f"CREATE VIEW {ident(FILE_SCHEMA)}.{ident(table.name)} AS "
                f"SELECT {', '.join(items)} FROM {ident(alias)}"
            )
        return con
    except Exception:
        con.close()
        raise


def _run(con: duckdb.DuckDBPyConnection, sql: str) -> QueryResult:
    try:
        cursor = con.execute(sql)
        description = cursor.description or []
        rows = cursor.fetchall()
    except duckdb.Error as exc:
        raise ExecutionError(f"SQL 執行失敗：{exc}") from exc
    return QueryResult(columns=[d[0] for d in description], rows=[tuple(r) for r in rows])


def execute_federated(
    query: WorksheetSql,
    database: Path | None,
    connection: FileConnection,
    fetch: Fetch,
    cache: FederatedCache | None = None,
) -> QueryResult:
    """執行一張工作表的查詢（跨資料庫的 join）：先做查詢附帶的檢查，再執行查詢。Data Blending 的次要資料源
    是同一目錄中以資料源 id 為檔名的 DuckDB 檔。"""
    sqls = [query.sql, *(g.sql for g in query.guards)]
    con = federated_connection(database, connection, fetch, cache, sqls, query.parameters)
    try:
        for guard in query.guards:
            check_guard(guard, _run(con, guard.sql).rows)
        if query.attach:
            folder = database.parent if database is not None else None
            for alias, ds in query.attach.items():
                if folder is None:
                    raise ExecutionError("Data Blending 的次要資料源找不到資料檔")
                other = folder / f"{ds}.duckdb"
                if not other.is_file():
                    raise ExecutionError(f"找不到資料檔 {other}；請先匯入資料源")
                con.execute(f"ATTACH {string(str(other))} AS {ident(alias)} (READ_ONLY)")
        return finish(query, _run(con, query.sql))
    finally:
        con.close()


def execute_federated_sql(
    sql: str,
    database: Path | None,
    connection: FileConnection,
    fetch: Fetch,
    cache: FederatedCache | None = None,
    parameters: ParameterValues | None = None,
) -> QueryResult:
    """執行資料源上的一段查詢（例如篩選控制項的可選值、資料的預覽）。"""
    con = federated_connection(database, connection, fetch, cache, [sql], parameters)
    try:
        return _run(con, sql)
    finally:
        con.close()


__all__ = [
    "FederatedCache",
    "Fetch",
    "execute_federated",
    "execute_federated_sql",
    "federated_connection",
    "referenced_columns",
    "remote_sql",
    "used_columns",
]
