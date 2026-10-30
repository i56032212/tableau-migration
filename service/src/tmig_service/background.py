"""在背景執行緒啟動查詢服務（測試與 compare-screens 使用）。"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from tmig_service.app import create_app
from tmig_service.auth import Authenticator, AuthSettings


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def running_server(
    fixtures: Path,
    web_dist: Path | None = None,
    port: int | None = None,
    auth: AuthSettings | Authenticator | None = None,
    usage_dir: Path | None = None,
) -> Iterator[str]:
    """啟動服務並回傳網址；離開時關閉。auth 省略時不驗證身分（不讀環境變數 TMIG_AUTH）。"""
    chosen = port or free_port()
    config = uvicorn.Config(
        create_app(fixtures, web_dist, auth=auth or AuthSettings(), usage_dir=usage_dir),
        host="127.0.0.1",
        port=chosen,
        log_level="warning",
        proxy_headers=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("查詢服務沒有在 30 秒內啟動")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{chosen}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
