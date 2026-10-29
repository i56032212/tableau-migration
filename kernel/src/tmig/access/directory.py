"""使用者目錄（第三階段 P2）：Tableau Server 上的使用者與群組（SDD：使用者與群組同名對應）。

登入的帳號（反向代理的標頭、OIDC 的宣告，或開發模式選的使用者）依名稱對應到目錄中的使用者：
USERNAME() 用目錄中的名稱（大小寫與 Tableau 相同），ISMEMBEROF() 用目錄中的群組，也就是報表公式中寫的
Tableau 群組名稱。目錄檔（JSON）由 tools/tableau_server 從 Tableau Server 匯出（P3），也可以手寫：

    {
      "source": "https://tableau.example.com（site：sales）",
      "exported_at": "2026-10-30T00:00:00+00:00",
      "users": [
        {"name": "alice", "full_name": "Alice Wang", "domain": "local",
         "email": "alice@example.com", "site_role": "Viewer", "groups": ["All Users", "Sales"]}
      ]
    }

群組的寫法與 ISMEMBEROF 的引數相同：本機群組寫名稱，Active Directory 群組寫「網域\\名稱」。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig.planner.identity import Identity


class DirectoryError(ValueError):
    pass


class DirectoryUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Tableau Server 上的使用者名稱（USERNAME() 的值）")
    full_name: str | None = None
    domain: str | None = Field(default=None, description="使用者的網域；本機帳號為 local")
    email: str | None = None
    site_role: str | None = Field(default=None, description="Tableau 的站台角色，例如 Viewer")
    groups: list[str] = Field(default_factory=list)

    def identity(self) -> Identity:
        return Identity(
            username=self.name,
            full_name=self.full_name,
            domain=self.domain,
            groups=tuple(self.groups),
            site_role=self.site_role,
        )


class Directory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, description="匯出來源（伺服器與 site）")
    exported_at: str | None = None
    users: list[DirectoryUser] = Field(default_factory=list)

    def find(self, login: str) -> DirectoryUser | None:
        """依登入的帳號找使用者：名稱不分大小寫；可以寫成「網域\\名稱」；找不到時比對 email。

        同名的使用者在多個網域時，沒有寫網域就無法判斷，回傳 None。
        """
        text = login.strip()
        domain: str | None = None
        if "\\" in text:
            domain, text = text.split("\\", 1)
        key = text.casefold()
        matches = [
            u
            for u in self.users
            if u.name.casefold() == key
            and (domain is None or (u.domain or "").casefold() == domain.casefold())
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
        by_email = [u for u in self.users if u.email and u.email.casefold() == text.casefold()]
        return by_email[0] if len(by_email) == 1 else None


def login_name(raw: str, strip_domain: bool) -> str:
    """登入帳號的寫法 → 目錄中的名稱：strip_domain 時去掉 email 的網域或 Windows 網域
    （alice@corp.example.com、CORP\\alice → alice）。"""
    text = raw.strip()
    if not strip_domain:
        return text
    if "\\" in text:
        text = text.split("\\", 1)[1]
    if "@" in text:
        text = text.split("@", 1)[0]
    return text


def load_directory(path: str | Path) -> Directory:
    source = Path(path)
    if not source.is_file():
        raise DirectoryError(f"找不到使用者目錄 {source}")
    try:
        return Directory.model_validate(json.loads(source.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise DirectoryError(f"使用者目錄 {source} 格式錯誤：{exc}") from exc


__all__ = ["Directory", "DirectoryError", "DirectoryUser", "load_directory", "login_name"]
