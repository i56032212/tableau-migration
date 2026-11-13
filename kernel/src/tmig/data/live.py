"""Live 連線的兩段式執行（第三階段 N3）。

工作表查詢拆成兩段（tmig.sqlgen.dialect.split）：不含 Table Calculation 的部分轉成來源資料庫的方言、
在來源資料庫執行，結果（Arrow）以同名的資料表交給記憶體中的 DuckDB，再執行 Table Calculation 與輸出。
結果與萃取時相同，是 QueryResult。

空間函式（MAKEPOINT 等）在 DuckDB 計算：通常只有之後的部分改在 DuckDB（split 的 localize_spatial）；讀資料表
的那一段就用到空間函式時（例如 Relationship 模型中邏輯資料表的計算欄位），改為取回查詢用到的資料表（只取
用到的欄），以原本的名稱交給 DuckDB 執行整段查詢（execute_rows）。
"""

from __future__ import annotations

import contextlib
import datetime as dt

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from tmig.data import udf
from tmig.data.executor import ExecutionError, QueryResult, check_guard, finish
from tmig.data.sources import Executor
from tmig.sqlgen.dialect import SPATIAL_CALL, DialectError, ParameterValues, split, to_dialect
from tmig.sqlgen.query import OutputColumn, WorksheetSql
from tmig.sqlgen.source import CUSTOM_SQL_PREFIX
from tmig.sqlgen.sql import ident


def execute_live(query: WorksheetSql, executor: Executor) -> QueryResult:
    """執行一張工作表的查詢（兩段式）；讀資料表的那一段就用到空間函式時改以 execute_rows 執行。"""
    try:
        return _execute_split(query, executor)
    except DialectError:
        if not any(SPATIAL_CALL.search(c.body) for c in query.ctes if c.pushdown):
            raise
        return execute_rows(query, executor)


def execute_rows(query: WorksheetSql, executor: Executor) -> QueryResult:
    """取回查詢讀的每張資料表（只取用到的欄；自訂 SQL 取整段的結果），以原本的名稱（"結構描述"."資料表"）交給
    記憶體中的 DuckDB，整段查詢在 DuckDB 執行（與萃取相同）。"""
    referenced = source_tables(query.sql, prune=False)
    used = source_tables(query.sql)
    spatial = spatial_columns(query.sql)
    con = udf.register(duckdb.connect())

    def fetch(sql: str, parts: tuple[str, ...]) -> pa.Table:
        translated = to_dialect(sql, executor.dialect, query.custom_sql, query.parameters)
        try:
            return executor.fetch(translated)
        except Exception as exc:
            raise ExecutionError(f"來源資料庫執行失敗（{'.'.join(parts)}）：{exc}") from exc

    try:
        schemas: set[tuple[str, ...]] = set()
        for index, (parts, columns) in enumerate(referenced.items()):
            name = parts[-1]
            alias = f"__tmig_rows_{index}"
            source = ".".join(ident(p) for p in parts)
            wanted = used.get(parts, columns)
            shapes = spatial.get(parts, set())
            if name.startswith(CUSTOM_SQL_PREFIX):
                con.register(alias, fetch(f"SELECT * FROM {source} AS {ident('t')}", parts))
            elif columns is None or wanted is None:
                if shapes:
                    raise DialectError(
                        f"資料表 {'.'.join(parts)} 的空間欄位用在空間 join 中，Live 連線尚不支援"
                    )
                con.register(alias, fetch(f"SELECT * FROM {source}", parts))
            else:
                keep = sorted(wanted or columns)[: None if wanted else 1]
                # 空間欄位在來源資料庫轉成 WKT 文字（查詢中的 tmig_geo_wkt 在 DuckDB 原樣傳回）
                items = ", ".join(
                    f"{udf.GEO_WKT}({ident(c)}) AS {ident(c)}" if c in shapes else ident(c)
                    for c in keep
                )
                rows = fetch(f"SELECT {items} FROM {source}", parts)
                rest = sorted(columns - set(keep))
                if rest:
                    # 查詢用不到的欄（例如 base 中其他的欄位）：同型別的 Null（取 0 列得到型別）
                    empty = fetch(
                        f"SELECT {', '.join(ident(c) for c in rest)} FROM {source} WHERE 1 = 0",
                        parts,
                    )
                    con.register(f"{alias}_rows", rows)
                    con.register(f"{alias}_empty", empty)
                    con.execute(
                        f"CREATE VIEW {ident(alias)} AS SELECT * FROM {ident(alias + '_rows')} "
                        f"UNION ALL BY NAME SELECT * FROM {ident(alias + '_empty')}"
                    )
                else:
                    con.register(alias, rows)
            if len(parts) == 3 and parts[:1] not in schemas:
                con.execute(f"ATTACH ':memory:' AS {ident(parts[0])}")
                schemas.add(parts[:1])
            if len(parts) > 1 and parts[:-1] not in schemas:
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {'.'.join(ident(p) for p in parts[:-1])}")
                schemas.add(parts[:-1])
            target = ".".join(ident(p) for p in parts)
            con.execute(f"CREATE VIEW {target} AS SELECT * FROM {ident(alias)}")
        for guard in query.guards:
            check_guard(guard, _run(con, guard.sql).rows)
        return finish(query, _coerce(_run(con, query.sql), query.columns))
    finally:
        con.close()


def spatial_columns(sql: str) -> dict[tuple[str, ...], set[str]]:
    """查詢中以 tmig_geo_wkt 轉換的空間欄位（資料表 → 欄名）。"""
    from sqlglot.optimizer.scope import traverse_scope

    found: dict[tuple[str, ...], set[str]] = {}
    for scope in traverse_scope(sqlglot.parse_one(sql, read="duckdb")):
        tables = {
            alias: tuple(p for p in (source.catalog, source.db, source.name) if p)
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table)
        }
        for call in scope.expression.find_all(exp.Anonymous):
            if str(call.this).lower() != udf.GEO_WKT or not call.expressions:
                continue
            column = call.expressions[0]
            if isinstance(column, exp.Column) and column.table in tables:
                found.setdefault(tables[column.table], set()).add(column.name)
    return found


def source_tables(sql: str, prune: bool = True) -> dict[tuple[str, ...], set[str] | None]:
    """查詢讀的來源資料表（限定名稱的各段，含自訂 SQL 的佔位資料表）與用到的欄；None 為全部的欄
    （SELECT * 或認不出所屬的欄）。prune：去掉 CTE 中沒有用到的欄之後才算（否則是 SQL 中參照的所有欄）。"""
    from sqlglot.errors import SqlglotError
    from sqlglot.optimizer.pushdown_projections import pushdown_projections
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import traverse_scope

    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except ParseError as exc:  # pragma: no cover - 產生的 SQL 一定能解析
        raise ExecutionError(f"內部錯誤：SQL 無法解析：{exc}") from exc
    if prune:
        # 去掉 CTE 中沒有用到的欄（名稱的大小寫不變：不依 DuckDB 的規則改成小寫）；分析不了時不去掉
        with contextlib.suppress(SqlglotError, RecursionError, KeyError, ValueError):
            tree = pushdown_projections(
                qualify(tree.copy(), validate_qualify_columns=False, quote_identifiers=False)
            )
    found: dict[tuple[str, ...], set[str] | None] = {}
    for scope in traverse_scope(tree):
        tables = {
            alias: tuple(p for p in (source.catalog, source.db, source.name) if p)
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table)
        }
        for parts in tables.values():
            found.setdefault(parts, set())
        if not tables:
            continue
        selects = getattr(scope.expression, "selects", [])
        everything = any(isinstance(e, exp.Star) or e.is_star for e in selects)
        for column in scope.columns:
            owner = tables.get(column.table) if column.table else None
            if owner is None and not column.table:
                # 沒有標明所屬：只有一個來源時就是它，否則不知道是哪一張（全部的欄）
                single = list(tables.values()) if len(scope.sources) == 1 else []
                if not single:
                    everything = True
                    continue
                owner = single[0]
            columns = found.get(owner) if owner is not None else None
            if columns is not None:
                columns.add(column.name)
        if everything:
            for parts in tables.values():
                found[parts] = None
    return found


def _execute_split(query: WorksheetSql, executor: Executor) -> QueryResult:
    """兩段式執行一張工作表的查詢。轉譯不了的 SQL 以 DialectError（QueryError）回報。

    查詢附帶的檢查（Relationship 模型的一對多）先在來源資料庫執行，不通過時以 QueryError 回報。

    日期型別的輸出欄轉成日期：Oracle 的 DATE 含時間，取回的是午夜的日期時間。
    """
    for guard in query.guards:
        sql = to_dialect(guard.sql, executor.dialect, query.custom_sql, query.parameters)
        try:
            table = executor.fetch(sql)
        except Exception as exc:
            raise ExecutionError(f"來源資料庫執行失敗（檢查）：{exc}") from exc
        check_guard(guard, [tuple(row.values()) for row in table.to_pylist()])
    plan = split(query)
    remote = [
        (fetch.name, to_dialect(fetch.sql, executor.dialect, query.custom_sql, query.parameters))
        for fetch in plan.fetches
    ]
    con = udf.register(duckdb.connect())
    try:
        for name, sql in remote:
            try:
                table = executor.fetch(sql)
            except Exception as exc:
                raise ExecutionError(f"來源資料庫執行失敗（{name}）：{exc}") from exc
            con.register(name, table)
        return finish(query, _coerce(_run(con, plan.local_sql), query.columns))
    finally:
        con.close()


def execute_remote(
    sql: str,
    executor: Executor,
    custom_sql: list[str] | None = None,
    parameters: ParameterValues | None = None,
) -> QueryResult:
    """整段查詢在來源資料庫執行（例如篩選控制項的可選值）。"""
    translated = to_dialect(sql, executor.dialect, custom_sql, parameters)
    try:
        table = executor.fetch(translated)
    except Exception as exc:
        raise ExecutionError(f"來源資料庫執行失敗：{exc}") from exc
    con = udf.register(duckdb.connect())
    try:
        con.register("remote", table)
        return _run(con, "SELECT * FROM remote")
    finally:
        con.close()


def _coerce(result: QueryResult, columns: list[OutputColumn]) -> QueryResult:
    types = {c.key: c.datatype for c in columns}
    dates = [i for i, name in enumerate(result.columns) if types.get(name) == "date"]
    if not dates:
        return result
    rows = [
        tuple(
            value.date() if i in dates and isinstance(value, dt.datetime) else value
            for i, value in enumerate(row)
        )
        for row in result.rows
    ]
    return QueryResult(columns=result.columns, rows=rows)


def _run(con: duckdb.DuckDBPyConnection, sql: str) -> QueryResult:
    try:
        cursor = con.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchall()
    except duckdb.Error as exc:
        raise ExecutionError(f"SQL 執行失敗：{exc}") from exc
    return QueryResult(columns=columns, rows=[tuple(r) for r in rows])


__all__ = ["execute_live", "execute_remote", "execute_rows", "source_tables", "spatial_columns"]
