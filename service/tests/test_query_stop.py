"""檢視的「停止」（POST /api/queries/{代號}/cancel）：render 以 X-Tmig-Query 標出的查詢在執行中被中斷
（DuckDB 的 interrupt）、回應 409；停止比查詢先到時查詢不執行；只能停止自己的查詢。"""

from __future__ import annotations

import threading
import time
import warnings
from pathlib import Path
from typing import Any

import duckdb

from tmig.data.executor import QueryResult, execute
from tmig.data.runtime import Database, DataRuntime
from tmig.ir.models import Datasource
from tmig.planner.identity import Identity
from tmig.sqlgen.query import WorksheetSql
from tmig_service.app import create_app
from tmig_service.query_stop import RunningQueries

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
RENDER = "/api/workbooks/superstore_demo/worksheets/ws_sales_by_region/render"
SLOW = "SELECT sum(a.range * b.range) FROM range(100000) a, range(100000) b"


class SlowRuntime(DataRuntime):
    """工作表的查詢換成要跑很久的 DuckDB 查詢（萃取的執行方式）。"""

    def __init__(self, path: Path) -> None:
        super().__init__({})
        self.path = path
        self.started = threading.Event()
        self.finished = threading.Event()

    def run(self, query: WorksheetSql, datasource: Datasource, database: Database) -> QueryResult:
        self.started.set()
        try:
            return execute(self.path, SLOW)
        finally:
            self.finished.set()


def slow_client(tmp_path: Path) -> tuple[TestClient, SlowRuntime]:
    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).close()
    runtime = SlowRuntime(path)
    return TestClient(create_app(ROOT / "fixtures", ROOT / "no-web-dist", runtime=runtime)), runtime


def test_stop_interrupts_running_render(tmp_path: Path) -> None:
    client, runtime = slow_client(tmp_path)
    query = "3f2b8c1e-0000-4a5b-9c7d-112233445566"
    outcome: dict[str, Any] = {}

    def render() -> None:
        outcome["response"] = client.post(RENDER, json={}, headers={"X-Tmig-Query": query})

    thread = threading.Thread(target=render, daemon=True)
    thread.start()
    assert runtime.started.wait(30)
    time.sleep(0.3)
    stopped = time.monotonic()
    assert client.post(f"/api/queries/{query}/cancel").status_code == 204
    thread.join(timeout=30)
    assert not thread.is_alive(), "停止之後 render 沒有結束"
    assert time.monotonic() - stopped < 10
    response = outcome["response"]
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "已停止查詢"


def test_stop_before_the_query_starts(tmp_path: Path) -> None:
    """停止先到（查詢還在排隊）：之後的 render 不執行查詢；其他代號照常不受影響。"""
    client, runtime = slow_client(tmp_path)
    query = "aaaaaaaa-1111-2222-3333-444444444444"
    assert client.post(f"/api/queries/{query}/cancel").status_code == 204
    started = time.monotonic()
    response = client.post(RENDER, json={}, headers={"X-Tmig-Query": query})
    assert response.status_code == 409, response.text
    assert time.monotonic() - started < 10
    assert runtime.started.is_set() and runtime.finished.is_set()  # 到了查詢就停下
    # 格式不對的代號：不記錄、不追蹤
    assert client.post("/api/queries/x/cancel").status_code == 204


def test_only_the_owner_can_stop() -> None:
    from tmig.data import cancel

    running = RunningQueries()
    amy = Identity(username="amy")
    bob = Identity(username="bob")
    query = "bbbbbbbb-1111-2222-3333-444444444444"
    with running.track(amy, query):
        assert running.stop(bob, query) is False
        with cancel.watching(None):  # 沒有被停止：照常開始
            pass
        assert running.stop(amy, query) is True
        try:
            with cancel.watching(None):
                raise AssertionError("停止之後不應開始新的查詢")
        except cancel.QueryCancelled:
            pass
    # bob 的停止記成「先到的停止」：bob 自己的同一個代號開始時才停止，與 amy 無關
    with running.track(amy, query), cancel.watching(None):
        pass
