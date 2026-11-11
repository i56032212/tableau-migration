"""站台資料：Project、Tableau 匯入的 workbook 權限、使用者與群組，在新平台上管理（Tableau 下線後的唯一來源）。

    <報表庫>/
    ├── site.json            站台資料（Project 的權限、使用者、群組）
    └── site-changes.jsonl   每次修改的紀錄：時間、修改的人、動作與對象（不記錄修改前後的值）

第一次啟動時以 Tableau Server 匯出的檔案（[auth] 的 directory、permissions）建立，之後以站台管理的 API
（tmig_service.admin）修改；要重新從 Tableau 匯入時用 ``tmig site import --replace``。權限的判斷規則
與 Tableau 相同（tmig.access.permissions），使用者與群組的寫法與匯出的目錄相同（tmig.access.directory）。

寫入先寫暫存檔再取代，讀取依檔案的修改時間快取；同一個行程內以鎖排隊，多個行程同時修改時後寫的
以修改當下讀到的內容為準（各自重讀後套用，不會蓋掉別人寫入的整份資料）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig.access.directory import Directory, DirectoryUser
from tmig.access.permissions import Permissions, ProjectEntry, WorkbookEntry

SITE = "site.json"
CHANGES = "site-changes.jsonl"
ALL_USERS = "All Users"
"""每個使用者都在的群組（與 Tableau 相同；ISMEMBEROF("All Users") 一律成立），不能刪除或改名。"""

SITE_ROLES = (
    "SiteAdministratorCreator",
    "SiteAdministratorExplorer",
    "Creator",
    "ExplorerCanPublish",
    "Explorer",
    "Viewer",
    "Unlicensed",
)
"""Tableau 2021.2 的站台角色（ServerAdministrator 只在匯入的資料中出現）。"""

WORKBOOK_CAPABILITIES = {
    "Read": "檢視",
    "Filter": "篩選",
    "ViewComments": "檢視留言",
    "AddComment": "新增留言",
    "ExportImage": "下載圖片／PDF",
    "ExportData": "下載摘要資料",
    "ViewUnderlyingData": "下載完整資料",
    "ShareView": "分享自訂檢視",
    "ExportXml": "下載報表",
    "Write": "覆蓋",
    "Delete": "刪除",
    "ChangePermissions": "設定權限",
}
"""報表（workbook）的能力：Tableau 的名稱 → 畫面上的名稱。"""
PROJECT_CAPABILITIES = {
    "Read": "檢視",
    "Write": "發布",
    "ProjectLeader": "Project Leader",
}
CONTENT_PERMISSIONS = ("ManagedByOwner", "LockedToProject", "LockedToProjectWithoutNested")


class SiteError(ValueError):
    """站台資料不正確（例如 Project 的上層形成迴圈）。"""


class GroupEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, description="ISMEMBEROF 的寫法：本機群組為名稱，AD 群組為「網域\\名稱」"
    )
    description: str = ""


class SiteData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, description="最初匯入的來源（Tableau Server 與 site）")
    imported_at: str | None = None
    updated_at: str | None = None
    projects: list[ProjectEntry] = Field(default_factory=list)
    workbooks: list[WorkbookEntry] = Field(
        default_factory=list,
        description="Tableau 匯入的 workbook 與權限（移轉的報表、第一次發布時對應同名的 workbook）",
    )
    users: list[DirectoryUser] = Field(default_factory=list)
    groups: list[GroupEntry] = Field(default_factory=list)

    def permissions(self) -> Permissions:
        return Permissions(
            source=self.source,
            exported_at=self.updated_at or self.imported_at,
            projects=self.projects,
            workbooks=self.workbooks,
        )

    def directory(self) -> Directory:
        return Directory(source=self.source, exported_at=self.updated_at, users=self.users)

    # ------------------------------------------------------------ 查找

    def project(self, project_id: str) -> ProjectEntry | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def user(self, name: str, domain: str | None = None) -> DirectoryUser | None:
        key = name.casefold()
        found = [
            u
            for u in self.users
            if u.name.casefold() == key
            and (domain is None or (u.domain or "").casefold() == domain.casefold())
        ]
        return found[0] if len(found) == 1 else None

    def group(self, name: str) -> GroupEntry | None:
        key = name.casefold()
        return next((g for g in self.groups if g.name.casefold() == key), None)

    def members(self, group: str) -> list[DirectoryUser]:
        key = group.casefold()
        if key == ALL_USERS.casefold():
            return list(self.users)
        return [u for u in self.users if key in {g.casefold() for g in u.groups}]

    def children(self, project_id: str) -> list[ProjectEntry]:
        return [p for p in self.projects if p.parent == project_id]


def now_text() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def validate_site(data: SiteData) -> None:
    """Project 的 id 不重複、上層存在且沒有迴圈；使用者與群組的名稱不重複；使用者的群組都存在。"""
    ids = [p.id for p in data.projects]
    if len(ids) != len(set(ids)):
        raise SiteError("Project 的 id 重複")
    known = set(ids)
    for project in data.projects:
        if project.parent is not None and project.parent not in known:
            raise SiteError(f"Project「{project.name}」的上層 Project 不存在")
        if project.content_permissions not in CONTENT_PERMISSIONS:
            raise SiteError(f"Project「{project.name}」的權限管理方式不正確")
        seen = {project.id}
        parent = project.parent
        while parent is not None:
            if parent in seen:
                raise SiteError(f"Project「{project.name}」的上層形成迴圈")
            seen.add(parent)
            found = data.project(parent)
            parent = found.parent if found is not None else None
    siblings: dict[tuple[str | None, str], str] = {}
    for project in data.projects:
        key = (project.parent, project.name.casefold())
        if key in siblings:
            raise SiteError(f"同一層已經有名為「{project.name}」的 Project")
        siblings[key] = project.id
    users = [(u.name.casefold(), (u.domain or "").casefold()) for u in data.users]
    if len(users) != len(set(users)):
        raise SiteError("使用者重複（名稱與網域相同）")
    groups = [g.name.casefold() for g in data.groups]
    if len(groups) != len(set(groups)):
        raise SiteError("群組名稱重複")
    if ALL_USERS.casefold() in groups:
        raise SiteError(f"「{ALL_USERS}」是每個使用者都在的群組，不需要另外建立")
    allowed = set(groups) | {ALL_USERS.casefold()}
    for user in data.users:
        if user.site_role is not None and user.site_role not in (
            *SITE_ROLES,
            "ServerAdministrator",
        ):
            raise SiteError(f"使用者 {user.name} 的站台角色 {user.site_role} 不正確")
        missing = [g for g in user.groups if g.casefold() not in allowed]
        if missing:
            raise SiteError(f"使用者 {user.name} 的群組不存在：{'、'.join(missing)}")


def from_exports(
    permissions: Permissions | None, directory: Directory | None, source: str | None = None
) -> SiteData:
    """Tableau Server 匯出的權限與使用者目錄 → 站台資料（群組取自使用者所屬的群組）。"""
    names: dict[str, str] = {}
    for user in directory.users if directory is not None else []:
        for group in user.groups:
            if group.casefold() != ALL_USERS.casefold():
                names.setdefault(group.casefold(), group)
    projects = list(permissions.projects) if permissions is not None else []
    if not projects:
        projects = [ProjectEntry(id="default", name="預設")]
    data = SiteData(
        source=source
        or (permissions.source if permissions is not None else None)
        or (directory.source if directory is not None else None),
        imported_at=now_text(),
        projects=projects,
        workbooks=list(permissions.workbooks) if permissions is not None else [],
        users=list(directory.users) if directory is not None else [],
        groups=[GroupEntry(name=n) for _, n in sorted(names.items())],
    )
    validate_site(data)
    return data


class SiteStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / SITE
        self.log = root / CHANGES
        self._lock = threading.RLock()
        self._cache: tuple[int, SiteData] | None = None

    def exists(self) -> bool:
        return self.path.is_file()

    def data(self) -> SiteData:
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError as exc:
            raise SiteError(f"找不到站台資料 {self.path}") from exc
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            data = SiteData.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise SiteError(f"站台資料 {self.path} 不正確：{exc}") from exc
        self._cache = (stamp, data)
        return data

    def permissions(self) -> Permissions:
        return self.data().permissions()

    def directory(self) -> Directory:
        return self.data().directory()

    def initialize(self, data: SiteData, actor: str, replace: bool = False) -> SiteData:
        """建立站台資料（已經存在時要 replace）。"""
        with self._lock:
            if self.exists() and not replace:
                raise SiteError("站台資料已經存在；要重新匯入時加 --replace")
            validate_site(data)
            self._write(data)
            self._record(actor, "import", data.source or "", {})
            return data

    def change(
        self,
        actor: str,
        action: str,
        target: str,
        apply: Callable[[SiteData], None],
        detail: dict[str, Any] | None = None,
    ) -> SiteData:
        """讀取目前的資料、套用修改、檢查後寫入，並記一筆修改紀錄。apply 拋出 SiteError 時不寫入。"""
        with self._lock:
            data = self.data().model_copy(deep=True)
            apply(data)
            data.updated_at = now_text()
            try:
                data = SiteData.model_validate(data.model_dump())
            except ValidationError as exc:
                raise SiteError(str(exc)) from exc
            validate_site(data)
            self._write(data)
            self._record(actor, action, target, detail or {})
            return data

    def changes(self, limit: int = 200) -> list[dict[str, Any]]:
        """最近的修改紀錄（新的在前）。"""
        if not self.log.is_file():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(entries) >= limit:
                break
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def record(
        self, actor: str, action: str, target: str, detail: dict[str, Any] | None = None
    ) -> None:
        """報表庫中報表的修改（移動、改名、刪除）也記在同一份紀錄。"""
        with self._lock:
            self._record(actor, action, target, detail or {})

    def _write(self, data: SiteData) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{SITE}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(data.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        self._cache = None

    def _record(self, actor: str, action: str, target: str, detail: dict[str, Any]) -> None:
        entry = {"time": now_text(), "user": actor, "action": action, "target": target, **detail}
        with self.log.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(entry, ensure_ascii=False) + "\n")


def new_project_id() -> str:
    return f"p{secrets.token_hex(6)}"


__all__ = [
    "ALL_USERS",
    "CONTENT_PERMISSIONS",
    "PROJECT_CAPABILITIES",
    "SITE_ROLES",
    "WORKBOOK_CAPABILITIES",
    "GroupEntry",
    "SiteData",
    "SiteError",
    "SiteStore",
    "from_exports",
    "new_project_id",
    "validate_site",
]
