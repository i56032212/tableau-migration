"""Live 連線的兩段式執行（第三階段 N3）。

工作表查詢拆成兩段（tmig.sqlgen.dialect.split）：不含 Table Calculation 的部分轉成來源資料庫的方言、
在來源資料庫執行，結果（Arrow）以同名的資料表交給記憶體中的 DuckDB，再執行 Table Calculation 與輸出。
結果與萃取時相同，是 QueryResult。
"""

from __future__ import annotations

import datetime as dt

import duckdb

from tmig.data.executor import ExecutionError, QueryResult
from tmig.data.sources import Executor
from tmig.sqlgen.dialect import split, to_dialect
from tmig.sqlgen.query import OutputColumn, WorksheetSql


def execute_live(query: WorksheetSql, executor: Executor) -> QueryResult:
    """兩段式執行一張工作表的查詢。轉譯不了的 SQL 以 DialectError（QueryError）回報。

    日期型別的輸出欄轉成日期：Oracle 的 DATE 含時間，取回的是午夜的日期時間。
    """
    plan = split(query)
    remote = [
        (fetch.name, to_dialect(fetch.sql, executor.dialect, query.custom_sql))
        for fetch in plan.fetches
    ]
    con = duckdb.connect()
    try:
        for name, sql in remote:
            try:
                table = executor.fetch(sql)
            except Exception as exc:
                raise ExecutionError(f"來源資料庫執行失敗（{name}）：{exc}") from exc
            con.register(name, table)
        return _coerce(_run(con, plan.local_sql), query.columns)
    finally:
        con.close()


def execute_remote(
    sql: str, executor: Executor, custom_sql: list[str] | None = None
) -> QueryResult:
    """整段查詢在來源資料庫執行（例如篩選控制項的可選值）。"""
    translated = to_dialect(sql, executor.dialect, custom_sql)
    try:
        table = executor.fetch(translated)
    except Exception as exc:
        raise ExecutionError(f"來源資料庫執行失敗：{exc}") from exc
    con = duckdb.connect()
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


__all__ = ["execute_live", "execute_remote"]
