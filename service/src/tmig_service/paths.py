"""檢視器（web/dist）的位置。

從原始碼執行時是 repo 的 web/dist；安裝版的伺服器（deploy/windows）不在 repo 裡，由服務主程式以環境變數
TMIG_WEB_DIST 指定。指令的 --web 優先於環境變數。
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_DIST_ENV = "TMIG_WEB_DIST"


def default_web_dist() -> Path:
    """--web 沒有指定時的檢視器目錄：環境變數 TMIG_WEB_DIST，沒有時為 repo 的 web/dist。"""
    value = os.environ.get(WEB_DIST_ENV)
    return Path(value) if value else REPO_ROOT / "web" / "dist"
