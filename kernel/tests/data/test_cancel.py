"""查詢的中途停止（tmig.data.cancel）：萃取的 DuckDB 查詢以 interrupt 中斷、Live 的執行器以它的 cancel 中斷
（DuckDB 檔的 Live 連線；PostgreSQL 有引擎時實跑），停止之後不再開始新的查詢；沒有標出範圍時不受影響。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data import cancel
from tmig.data.executor import execute
from tmig.data.runtime import DataRuntime
from tmig.data.sources import PostgresExecutor
from tmig.ir.models import LiveConnection

from ..engines import pg_connect, postgres_cluster
from .test_sources import duckdb_configs, live_features

SLOW = "SELECT sum(a.range * b.range) FROM range(100000) a, range(100000) b"
"""沒有中斷時要跑很久（一百億列）的查詢。"""


def database(tmp_path: Path) -> Path:
    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).close()
    return path


def stop_while_running(scope: cancel.CancelScope, work: Callable[[], Any]) -> tuple[Any, float]:
    """在另一個執行緒以 scope 執行 work，查詢開始之後停止；回傳結果（或例外）與停止之後多久結束。"""
    outcome: list[Any] = []

    def run() -> None:
        with cancel.scope(scope):
            try:
                outcome.append(work())
            except Exception as exc:
                outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not scope._interrupts:  # 查詢開始（登記了中斷的方法）
        assert time.monotonic() < deadline and thread.is_alive()
        time.sleep(0.01)
    time.sleep(0.2)
    stopped = time.monotonic()
    scope.cancel()
    thread.join(timeout=30)
    assert not thread.is_alive(), "停止之後查詢沒有結束"
    return outcome[0], time.monotonic() - stopped


def test_extract_query_is_interrupted(tmp_path: Path) -> None:
    path = database(tmp_path)
    result, waited = stop_while_running(cancel.CancelScope(), lambda: execute(path, SLOW))
    assert isinstance(result, cancel.QueryCancelled), result
    assert waited < 10


def test_cancelled_scope_starts_nothing(tmp_path: Path) -> None:
    path = database(tmp_path)
    scope = cancel.CancelScope()
    scope.cancel()
    with cancel.scope(scope), pytest.raises(cancel.QueryCancelled):
        execute(path, "SELECT 1")
    # 範圍之外照常執行
    assert execute(path, "SELECT 1").rows == [(1,)]


def test_live_executor_is_cancelled() -> None:
    """DuckDB 檔的 Live 連線：執行器的 cancel 中斷查詢；連線之後重新開啟，照常查詢。"""
    live = live_features()
    connection = live.datasources[0].connection
    assert isinstance(connection, LiveConnection)
    with DataRuntime(duckdb_configs()) as runtime:
        result, waited = stop_while_running(
            cancel.CancelScope(), lambda: runtime.fetch(connection, SLOW)
        )
        assert isinstance(result, cancel.QueryCancelled), result
        assert waited < 10
        assert runtime.fetch(connection, "SELECT 42 AS x").column("x").to_pylist() == [42]


def test_postgres_query_is_cancelled() -> None:
    """PostgreSQL（有引擎時實跑）：取消要求中斷 pg_sleep。"""
    with postgres_cluster() as dsn:
        executor = PostgresExecutor(pg_connect(dsn))
        try:

            def work() -> Any:
                with cancel.watching(executor.cancel):
                    return executor.fetch("SELECT pg_sleep(30)")

            result, waited = stop_while_running(cancel.CancelScope(), work)
            assert isinstance(result, cancel.QueryCancelled), result
            assert waited < 10
            assert executor.fetch("SELECT 1 AS x").column("x").to_pylist() == [1]
        finally:
            executor.close()
