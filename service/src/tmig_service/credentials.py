"""桌面版存放伺服器權杖的地方：Windows 為認證管理員（Credential Manager），其他系統為只有自己能讀的檔案
（只供開發用）。權杖不寫進設定檔、工作區或報表套件；登出時刪除。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Protocol

TARGET_PREFIX = "tmig-desktop:"


def target_for(server: str) -> str:
    """認證管理員中的名稱：tmig-desktop:<伺服器網址>。"""
    return TARGET_PREFIX + server


class CredentialStore(Protocol):
    def get(self, target: str) -> str | None: ...
    def set(self, target: str, user: str, secret: str) -> None: ...
    def delete(self, target: str) -> None: ...


class MemoryCredentials:
    """測試用。"""

    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str]] = {}

    def get(self, target: str) -> str | None:
        item = self.items.get(target)
        return item[1] if item is not None else None

    def set(self, target: str, user: str, secret: str) -> None:
        self.items[target] = (user, secret)

    def delete(self, target: str) -> None:
        self.items.pop(target, None)


class FileCredentials:
    """非 Windows 的開發環境：JSON 檔，權限 0600（只有自己能讀）。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as sink:
            json.dump(data, sink)
        os.replace(temporary, self.path)

    def get(self, target: str) -> str | None:
        entry = self._load().get(target)
        return entry.get("secret") if isinstance(entry, dict) else None

    def set(self, target: str, user: str, secret: str) -> None:
        data = self._load()
        data[target] = {"user": user, "secret": secret}
        self._save(data)

    def delete(self, target: str) -> None:
        data = self._load()
        if data.pop(target, None) is not None:
            self._save(data)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    """只存在這台電腦（不隨漫遊設定檔同步），只有這個 Windows 使用者讀得到。"""
    ERROR_NOT_FOUND = 1168

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _cred_write = _advapi.CredWriteW
    _cred_write.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _cred_write.restype = wintypes.BOOL
    _cred_read = _advapi.CredReadW
    _cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    _cred_read.restype = wintypes.BOOL
    _cred_delete = _advapi.CredDeleteW
    _cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _cred_delete.restype = wintypes.BOOL
    _cred_free = _advapi.CredFree
    _cred_free.argtypes = [ctypes.c_void_p]
    _cred_free.restype = None

    def _failure(action: str) -> OSError:
        error = ctypes.get_last_error()
        return OSError(error, f"無法{action}認證管理員：{ctypes.FormatError(error)}")

    class WindowsCredentials:
        """Windows 的認證管理員（一般認證）。"""

        def get(self, target: str) -> str | None:
            pointer = ctypes.POINTER(_CREDENTIAL)()
            if not _cred_read(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
                if ctypes.get_last_error() == ERROR_NOT_FOUND:
                    return None
                raise _failure("讀取")
            try:
                credential = pointer.contents
                blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
                return blob.decode("utf-8")
            finally:
                _cred_free(pointer)

        def set(self, target: str, user: str, secret: str) -> None:
            data = secret.encode("utf-8")
            blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            credential = _CREDENTIAL()
            credential.Type = CRED_TYPE_GENERIC
            credential.TargetName = target
            credential.UserName = user
            credential.CredentialBlobSize = len(data)
            credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            credential.Persist = CRED_PERSIST_LOCAL_MACHINE
            if not _cred_write(ctypes.byref(credential), 0):
                raise _failure("寫入")

        def delete(self, target: str) -> None:
            deleted = _cred_delete(target, CRED_TYPE_GENERIC, 0)
            if not deleted and ctypes.get_last_error() != ERROR_NOT_FOUND:
                raise _failure("刪除")


def default_credentials(directory: Path) -> CredentialStore:
    if sys.platform == "win32":
        return WindowsCredentials()
    return FileCredentials(directory / "credentials.json")


def selftest(store: CredentialStore) -> None:
    """寫入、讀回、覆蓋、刪除一筆測試用的認證（Windows 的 CI 用來確認認證管理員可以用）。"""
    target = target_for("https://selftest.invalid")
    store.delete(target)
    assert store.get(target) is None, "測試用的認證刪不掉"
    store.set(target, "selftest", "第一組權杖")
    assert store.get(target) == "第一組權杖", "讀回的權杖不同"
    store.set(target, "selftest", "tmigd_second")
    assert store.get(target) == "tmigd_second", "覆蓋後讀回的權杖不同"
    store.delete(target)
    assert store.get(target) is None, "刪除後還讀得到"


__all__ = [
    "CredentialStore",
    "FileCredentials",
    "MemoryCredentials",
    "default_credentials",
    "selftest",
    "target_for",
]
