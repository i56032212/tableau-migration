"""桌面版存放伺服器權杖的地方（Windows 的認證管理員在 CI 的 Windows 上另外測試）。"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from tmig_service.credentials import (
    FileCredentials,
    MemoryCredentials,
    default_credentials,
    selftest,
    target_for,
)


def test_memory_and_file_credentials(tmp_path: Path) -> None:
    selftest(MemoryCredentials())
    path = tmp_path / "settings" / "credentials.json"
    store = FileCredentials(path)
    selftest(store)
    store.set(target_for("https://bi.example.com"), "amy", "tmigd_abc")
    assert FileCredentials(path).get(target_for("https://bi.example.com")) == "tmigd_abc"
    assert store.get(target_for("https://other.example.com")) is None
    store.delete(target_for("https://other.example.com"))  # 沒有時不出錯


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 用認證管理員")
def test_file_credentials_only_readable_by_owner(tmp_path: Path) -> None:
    store = default_credentials(tmp_path)
    assert isinstance(store, FileCredentials)
    store.set(target_for("https://bi.example.com"), "amy", "secret")
    mode = stat.S_IMODE(os.stat(tmp_path / "credentials.json").st_mode)
    assert mode == 0o600
