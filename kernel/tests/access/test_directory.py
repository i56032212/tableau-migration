"""使用者目錄（P2）：登入的帳號對應到 Tableau Server 的使用者與群組。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.access.directory import (
    Directory,
    DirectoryError,
    DirectoryUser,
    load_directory,
    login_name,
)
from tmig.planner.identity import Identity

DIRECTORY = Directory(
    users=[
        DirectoryUser(
            name="Alice",
            full_name="Alice Wang",
            domain="local",
            email="alice.w@example.com",
            groups=["All Users", "Sales"],
        ),
        DirectoryUser(name="sam", domain="CORP", groups=["CORP\\Finance"]),
        DirectoryUser(name="sam", domain="local", groups=["All Users"]),
    ]
)


def test_find_by_name_domain_and_email() -> None:
    alice = DIRECTORY.find("alice")
    assert alice is not None and alice.name == "Alice"
    assert alice.identity() == Identity(
        username="Alice", full_name="Alice Wang", domain="local", groups=("All Users", "Sales")
    )
    assert DIRECTORY.find(" ALICE.W@example.com ") is alice
    # 同名的使用者在兩個網域：要寫網域才能判斷
    assert DIRECTORY.find("sam") is None
    corp = DIRECTORY.find("corp\\SAM")
    assert corp is not None and corp.groups == ["CORP\\Finance"]
    assert DIRECTORY.find("nobody") is None


def test_login_name() -> None:
    assert login_name("alice@corp.example.com", True) == "alice"
    assert login_name("CORP\\alice", True) == "alice"
    assert login_name("CORP\\alice", False) == "CORP\\alice"
    assert login_name("  bob ", False) == "bob"


def test_load_directory(tmp_path: Path) -> None:
    path = tmp_path / "directory.json"
    path.write_text(DIRECTORY.model_dump_json(), encoding="utf-8")
    assert load_directory(path) == DIRECTORY
    with pytest.raises(DirectoryError, match="找不到使用者目錄"):
        load_directory(tmp_path / "none.json")
    path.write_text('{"users": [{"name": ""}]}', encoding="utf-8")
    with pytest.raises(DirectoryError, match="格式錯誤"):
        load_directory(path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(DirectoryError, match="格式錯誤"):
        load_directory(path)
