"""查詢的中途停止（檢視器、編輯器「更新中」的停止；Tableau 處理互動太久時可以停止）。

呼叫端以 ``scope(CancelScope())`` 標出一次請求；期間執行的查詢以 ``watching(中斷的方法)`` 登記，另一個執行緒呼叫
``CancelScope.cancel()`` 時中斷正在執行的查詢（萃取：DuckDB 連線的 interrupt；Live：執行器有 cancel 時呼叫它，
例如 PostgreSQL 的取消要求），之後要開始的查詢不再執行。停止之後查詢的錯誤一律改成 ``QueryCancelled``。

驅動程式沒有取消的方法時（MySQL 等），已送出的查詢在來源資料庫照常跑完、結果丟掉（與 Tableau 的「放棄」相同），
之後的查詢不再執行。沒有標出範圍時（指令列、排程）什麼都不做。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class QueryCancelled(RuntimeError):
    """查詢被停止。"""


class CancelScope:
    """一次請求中的查詢：cancel() 中斷正在執行的、不再執行之後的。可以從別的執行緒呼叫。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interrupts: list[Callable[[], None]] = []
        self.cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            interrupts = list(self._interrupts)
        for interrupt in interrupts:
            # 中斷失敗（連線已經關閉等）不影響停止：查詢跑完之後照樣改成 QueryCancelled
            with contextlib.suppress(Exception):
                interrupt()


_current: ContextVar[CancelScope | None] = ContextVar("tmig_cancel_scope", default=None)


@contextmanager
def scope(target: CancelScope) -> Iterator[CancelScope]:
    """這段程式（同一個執行緒）執行的查詢屬於 target。"""
    token = _current.set(target)
    try:
        yield target
    finally:
        _current.reset(token)


@contextmanager
def watching(interrupt: Callable[[], None] | None) -> Iterator[None]:
    """執行一段查詢的期間登記中斷它的方法（None：沒有辦法中斷，只在停止之後不再開始）。已經停止時不執行；
    停止之後這段查詢的錯誤改成 QueryCancelled。"""
    current = _current.get()
    if current is None:
        yield
        return
    with current._lock:
        if current.cancelled:
            raise QueryCancelled("已停止查詢")
        if interrupt is not None:
            current._interrupts.append(interrupt)
    try:
        yield
    except Exception as exc:
        if current.cancelled and not isinstance(exc, QueryCancelled):
            raise QueryCancelled("已停止查詢") from exc
        raise
    finally:
        if interrupt is not None:
            with current._lock:
                current._interrupts.remove(interrupt)


__all__ = ["CancelScope", "QueryCancelled", "scope", "watching"]
