"""權限（第三階段 P3）：Tableau Server 的 Project、workbook、view 權限，依 Tableau 的規則判斷登入者看不看得到。

權限檔（JSON）由 tools/tableau_server 的 permissions 匯出：

    {
      "source": "https://tableau.example.com（site：sales）",
      "exported_at": "2026-10-30T00:00:00+00:00",
      "projects": [
        {"id": "p1", "name": "Sales", "parent": null, "owner": "amy",
         "content_permissions": "LockedToProject",
         "rules": [{"grantee": {"kind": "group", "name": "Sales"}, "capabilities": {"ProjectLeader": "Allow"}}],
         "workbook_defaults": [{"grantee": {"kind": "group", "name": "All Users"}, "capabilities": {"Read": "Allow"}}]}
      ],
      "workbooks": [
        {"id": "w1", "name": "銷售總覽", "content_url": "SalesOverview", "project": "p1", "owner": "amy",
         "show_tabs": true, "rules": [...],
         "views": [{"id": "v1", "name": "Overview", "view_url_name": "Overview", "rules": [...]}]}
      ]
    }

判斷的順序（Tableau 的「權限評估」）：

1. 站台或伺服器管理員：全部允許；未授權（Unlicensed）的使用者：全部拒絕
2. 內容的擁有者、所在 Project 或上層 Project 的擁有者與 Project Leader：全部允許
3. 使用者本人的規則：拒絕就拒絕、允許就允許（優先於群組）
4. 所屬群組的規則：任一群組拒絕就拒絕，否則任一群組允許就允許
5. 都沒有設定：拒絕

Project 鎖定權限（LockedToProject）時，workbook 的權限一律是該 Project 的 workbook 預設權限
（上層 Project 鎖定時也套用到巢狀的 Project；LockedToProjectWithoutNested 不套用到巢狀的 Project）。
workbook 不以分頁顯示工作表（show_tabs 為 false）時，各 view 另有檢視權限（Read）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig.planner.identity import Identity

ADMIN_ROLES = frozenset(
    {
        "ServerAdministrator",
        "SiteAdministrator",
        "SiteAdministratorCreator",
        "SiteAdministratorExplorer",
    }
)
NO_ACCESS_ROLES = frozenset({"Unlicensed"})
Capability = str
"""Tableau 的能力名稱，例如 Read（檢視）、ExportXml（下載 workbook）、ExportData（下載摘要資料）。"""
Mode = Literal["Allow", "Deny"]


class PermissionsError(ValueError):
    pass


class Grantee(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["user", "group"]
    name: str = Field(min_length=1, description="使用者名稱，或 ISMEMBEROF 寫法的群組名稱")
    domain: str | None = Field(default=None, description="使用者的網域（同名的使用者在多個網域時）")


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grantee: Grantee
    capabilities: dict[Capability, Mode] = Field(default_factory=dict)


class ViewEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    view_url_name: str | None = None
    rules: list[Rule] = Field(default_factory=list)


class WorkbookEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    content_url: str | None = None
    project: str | None = Field(default=None, description="Project id")
    owner: str | None = Field(default=None, description="擁有者的使用者名稱")
    show_tabs: bool = True
    rules: list[Rule] = Field(default_factory=list)
    views: list[ViewEntry] = Field(default_factory=list)


class ProjectEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    parent: str | None = None
    owner: str | None = None
    content_permissions: str = Field(
        default="ManagedByOwner",
        description="LockedToProject、LockedToProjectWithoutNested 或 ManagedByOwner",
    )
    rules: list[Rule] = Field(
        default_factory=list, description="Project 本身的權限（含 ProjectLeader）"
    )
    workbook_defaults: list[Rule] = Field(default_factory=list, description="workbook 的預設權限")


class Access(BaseModel):
    """登入者對一份 workbook 的存取：views 為 None 表示所有 view 都可以看。"""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    views: frozenset[str] | None = None
    """可以看的 view 名稱（show_tabs 為 false 時逐一判斷）。"""
    reason: str = ""


class Permissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    exported_at: str | None = None
    projects: list[ProjectEntry] = Field(default_factory=list)
    workbooks: list[WorkbookEntry] = Field(default_factory=list)

    def project(self, project_id: str | None) -> ProjectEntry | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def ancestors(self, project_id: str | None) -> list[ProjectEntry]:
        """Project 本身與上層 Project（由近到遠）。"""
        chain: list[ProjectEntry] = []
        seen: set[str] = set()
        current = self.project(project_id)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = self.project(current.parent)
        return chain

    def workbook(
        self, content_url: str | None = None, name: str | None = None
    ) -> WorkbookEntry | None:
        """依網址名稱（優先）或名稱找 workbook；名稱重複時無法判斷，回傳 None。"""
        if content_url:
            found = [w for w in self.workbooks if w.content_url == content_url]
            if len(found) == 1:
                return found[0]
        if name:
            found = [w for w in self.workbooks if w.name == name]
            if len(found) == 1:
                return found[0]
        return None

    def workbook_rules(self, workbook: WorkbookEntry) -> list[Rule]:
        """workbook 實際適用的規則：Project 鎖定權限時為該 Project 的 workbook 預設權限。"""
        for depth, project in enumerate(self.ancestors(workbook.project)):
            if project.content_permissions == "LockedToProject" or (
                depth == 0 and project.content_permissions == "LockedToProjectWithoutNested"
            ):
                return project.workbook_defaults
        return workbook.rules

    def _is_leader(self, user: Identity, project_id: str | None) -> bool:
        for project in self.ancestors(project_id):
            if _matches_user(project.owner, user):
                return True
            if decide(project.rules, user, "ProjectLeader"):
                return True
        return False

    def access(
        self, user: Identity, workbook: WorkbookEntry, capability: Capability = "Read"
    ) -> Access:
        role = user.site_role or ""
        if role in NO_ACCESS_ROLES:
            return Access(allowed=False, reason="未授權的使用者（Unlicensed）")
        if role in ADMIN_ROLES:
            return Access(allowed=True, reason="管理員")
        if _matches_user(workbook.owner, user) or self._is_leader(user, workbook.project):
            return Access(allowed=True, reason="擁有者或 Project Leader")
        allowed = decide(self.workbook_rules(workbook), user, capability)
        if not allowed:
            return Access(allowed=False, reason="沒有權限")
        if capability != "Read" or workbook.show_tabs or not workbook.views:
            # 下載 workbook 等能力只在 workbook 層級判斷
            return Access(allowed=True)
        views = frozenset(v.name for v in workbook.views if decide(v.rules, user, capability))
        return Access(
            allowed=bool(views), views=views, reason="" if views else "沒有任何 view 的權限"
        )


def _matches_user(name: str | None, user: Identity, domain: str | None = None) -> bool:
    if not name or name.casefold() != user.username.casefold():
        return False
    return domain is None or user.domain is None or domain.casefold() == user.domain.casefold()


def decide(rules: list[Rule], user: Identity, capability: Capability) -> bool:
    """使用者本人的規則優先；其次任一群組拒絕就拒絕、任一群組允許就允許；都沒有時拒絕。"""
    groups = {g.casefold() for g in user.groups} | {"all users"}
    personal: list[Mode] = []
    grouped: list[Mode] = []
    for rule in rules:
        mode = rule.capabilities.get(capability)
        if mode is None:
            continue
        grantee = rule.grantee
        if grantee.kind == "user":
            if _matches_user(grantee.name, user, grantee.domain):
                personal.append(mode)
        elif grantee.name.casefold() in groups:
            grouped.append(mode)
    if personal:
        return "Deny" not in personal
    if "Deny" in grouped:
        return False
    return "Allow" in grouped


def load_permissions(path: str | Path) -> Permissions:
    source = Path(path)
    if not source.is_file():
        raise PermissionsError(f"找不到權限檔 {source}")
    try:
        return Permissions.model_validate(json.loads(source.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise PermissionsError(f"權限檔 {source} 格式錯誤：{exc}") from exc


__all__ = [
    "ADMIN_ROLES",
    "Access",
    "Grantee",
    "Permissions",
    "PermissionsError",
    "ProjectEntry",
    "Rule",
    "ViewEntry",
    "WorkbookEntry",
    "decide",
    "load_permissions",
]
