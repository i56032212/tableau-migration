"""檢視的「停止」（Tableau 處理互動太久時出現的停止）：檢視器與編輯器以 ``X-Tmig-Query`` 標頭標出一次查詢，
停止時 ``POST /api/queries/{代號}/cancel``，正在執行的查詢中斷、還沒開始的不再執行（tmig.data.cancel）。

- 只能停止自己的查詢：代號以登入者區分（沒有身分驗證時都是同一個）
- 停止比查詢先到（查詢還在排隊）：記下來，查詢開始時直接停止；記錄保留一分鐘
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from tmig.data import cancel
from tmig.planner.identity import Identity

QUERY_HEADER = "x-tmig-query"
"""查詢代號的標頭（檢視器每次查詢產生一個隨機的代號）。"""

QUERY_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
"""查詢代號的格式。"""

KEEP_SECONDS = 60.0
"""先到的停止保留多久（秒）。"""

KEEP_LIMIT = 1000
"""先到的停止最多記幾個。"""


def owner_of(user: Identity | None) -> str:
    """查詢的擁有者：登入者（沒有身分驗證時為空字串）。"""
    if user is None:
        return ""
    return f"{user.domain or ''}\\{user.username}"


class RunningQueries:
    """執行中的查詢：(擁有者, 代號) → 停止的範圍。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[tuple[str, str], cancel.CancelScope] = {}
        self._early: dict[tuple[str, str], float] = {}

    @contextmanager
    def track(self, user: Identity | None, query_id: str | None) -> Iterator[None]:
        """這段程式執行的查詢可以由 stop() 停止；沒有代號（或格式不對）時不追蹤。"""
        if not query_id or not QUERY_ID.match(query_id):
            yield
            return
        key = (owner_of(user), query_id)
        scope = cancel.CancelScope()
        with self._lock:
            self._prune()
            if self._early.pop(key, None) is not None:
                scope.cancel()
            self._running[key] = scope
        try:
            with cancel.scope(scope):
                yield
        finally:
            with self._lock:
                if self._running.get(key) is scope:
                    del self._running[key]

    def stop(self, user: Identity | None, query_id: str) -> bool:
        """停止查詢；查詢還沒開始時記下來（回傳 False）。"""
        if not QUERY_ID.match(query_id):
            return False
        key = (owner_of(user), query_id)
        with self._lock:
            scope = self._running.get(key)
            if scope is None:
                self._prune()
                if len(self._early) < KEEP_LIMIT:
                    self._early[key] = time.monotonic()
                return False
        scope.cancel()
        return True

    def _prune(self) -> None:
        limit = time.monotonic() - KEEP_SECONDS
        for key in [k for k, at in self._early.items() if at < limit]:
            del self._early[key]


__all__ = ["QUERY_HEADER", "QUERY_ID", "RunningQueries", "owner_of"]
