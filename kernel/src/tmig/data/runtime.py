"""查詢的執行環境（第三階段 N3、N4）：依資料源的連線決定在哪裡執行。

- 萃取：DuckDB 檔（呼叫端提供路徑，例如 <報表目錄>/data/<資料源 id>.duckdb）
- Live：兩段式執行（tmig.data.live）；連線依連線設定檔（tmig.data.sources）開啟，之後重複使用，
  同一個連線一次只執行一段查詢

查詢服務、tmig verify 與 tmig query 都透過這裡執行查詢。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from tmig.data.executor import QueryResult, execute
from tmig.data.live import execute_live, execute_remote
from tmig.data.sources import (
    ConnectionConfig,
    Executor,
    SourceConnectionError,
    load_connections,
    open_executor,
)
from tmig.ir.models import Datasource, LiveConnection
from tmig.sqlgen.query import QueryError, WorksheetSql
from tmig.sqlgen.source import datasource_source

Database = Path | Callable[[], Path]
"""萃取的 DuckDB 檔；可以是函式（只有萃取的資料源才需要時再準備，例如由 .twbx 匯入）。"""


class DataRuntime:
    def __init__(self, configs: dict[str, ConnectionConfig] | None = None) -> None:
        self._configs = configs
        self._executors: dict[str, Executor] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    @property
    def configs(self) -> dict[str, ConnectionConfig]:
        """連線設定檔（第一次用到 Live 連線時才讀取）。"""
        with self._lock:
            if self._configs is None:
                self._configs = load_connections()
            return self._configs

    def run(self, query: WorksheetSql, datasource: Datasource, database: Database) -> QueryResult:
        """執行一張工作表的查詢（萃取或 Live）。"""
        connection = datasource.connection
        if isinstance(connection, LiveConnection):
            with self._executor(connection) as executor:
                return execute_live(query, executor)
        return execute(_path(database), query.sql)

    def run_sql(self, sql: str, datasource: Datasource, database: Database) -> QueryResult:
        """執行資料源上的一段查詢（例如篩選控制項的可選值）；Live 連線時整段在來源資料庫執行。"""
        connection = datasource.connection
        if isinstance(connection, LiveConnection):
            custom = datasource_source(datasource).custom_sql
            with self._executor(connection) as executor:
                return execute_remote(sql, executor, custom)
        return execute(_path(database), sql)

    @contextmanager
    def _executor(self, connection: LiveConnection) -> Iterator[Executor]:
        key = connection.connection_id
        with self._lock:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            executor = self._executors.get(key)
            if executor is None:
                executor = open_executor(connection, self.configs)
                self._executors[key] = executor
            try:
                yield executor
            except (SourceConnectionError, QueryError):
                # 轉譯不了或設定不完整：連線本身沒有問題
                raise
            except Exception:
                # 來源資料庫執行失敗，連線可能已中斷：下次重新連線
                self._executors.pop(key, None)
                _close(executor)
                raise

    def close(self) -> None:
        with self._lock:
            executors = list(self._executors.values())
            self._executors.clear()
        for executor in executors:
            _close(executor)

    def __enter__(self) -> DataRuntime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _path(database: Database) -> Path:
    return database() if callable(database) else database


def _close(executor: Executor) -> None:
    # 關閉失敗不影響結果
    with contextlib.suppress(Exception):
        executor.close()


__all__ = ["DataRuntime", "Database"]
