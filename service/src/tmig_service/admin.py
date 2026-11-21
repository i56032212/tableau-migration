"""站台管理（Tableau 下線後在新平台上管理 Project、報表、使用者、群組與權限）的 API。

- ``GET    /api/admin/site``：站台的全部資料與畫面需要的選項（站台角色、能力的名稱）
- ``POST   /api/admin/projects``、``PATCH /api/admin/projects/{id}``、``DELETE /api/admin/projects/{id}``
- ``POST   /api/admin/users``、``PATCH /api/admin/users/{name}?domain=``、``DELETE /api/admin/users/{name}?domain=``
- ``POST   /api/admin/groups``、``PATCH /api/admin/groups/{name}``、``DELETE /api/admin/groups/{name}``
- ``PATCH  /api/admin/reports/{id}``：報表庫的報表改名、移到其他 Project、換擁有者、設定權限
- ``DELETE /api/admin/reports/{id}``：刪除報表（含每一版；移到報表庫的 trash/，可以由管理員救回）
- ``PATCH  /api/admin/workbooks/{id}``：Tableau 匯入的 workbook（移轉的報表）的擁有者、Project 與權限
- ``GET    /api/admin/changes``：修改紀錄

站台管理員（站台角色為 SiteAdministrator*、ServerAdministrator）可以使用全部；報表的擁有者、所在 Project
（與上層）的擁有者與 Project Leader，以及有「設定權限」（ChangePermissions）的人可以設定報表的權限，
有「刪除」（Delete）的人可以刪除報表（與 Tableau 相同；Project 鎖定權限時只能在 Project 上設定）。

規則（rules）的寫法與 Tableau 匯出的權限相同：``[{"grantee": {"kind": "group", "name": "Sales"},
"capabilities": {"Read": "Allow", "ExportData": "Deny"}}]``；沒寫的能力為「未設定」。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tmig.access.directory import DirectoryUser
from tmig.access.permissions import ADMIN_ROLES, ProjectEntry, Rule, decide
from tmig.planner.identity import Identity
from tmig_service.access import Rights
from tmig_service.site import (
    ALL_USERS,
    CONTENT_PERMISSIONS,
    PROJECT_CAPABILITIES,
    SITE_ROLES,
    WORKBOOK_CAPABILITIES,
    GroupEntry,
    SiteData,
    SiteError,
    SiteStore,
    new_project_id,
)
from tmig_service.store import ReportRecord, ReportStore

NAME_LIMIT = 255
CAPABILITY = re.compile(r"^[A-Za-z]{1,64}$")
_EMAIL_PART = r"[^@\s<>()\[\],;:\\\"]+"
EMAIL = re.compile(rf"{_EMAIL_PART}@(?!\.)(?:{_EMAIL_PART})(?<!\.)")
"""email 的樣子：一個 @、兩邊都有內容、沒有空白與分隔符號，網域不以點開頭或結尾。公司內部沒有點的網域
（user@mailhost）也接受；只擋下明顯不是信箱的字串，不驗證信箱存不存在。"""


def checked_email(value: str | None) -> str | None:
    """站台管理中填的 email：去掉前後的空白，空的為 None；不像信箱的（例如 not-an-email）不接受——
    訂閱與資料警示的信要寄到這裡，存進去之後才在寄信時失敗就太晚了。"""
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > 254 or EMAIL.fullmatch(text) is None or ".." in text.partition("@")[2]:
        raise SiteError(f"email「{text}」的格式不正確（要像 name@example.com）")
    return text


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_LIMIT)
    parent: str | None = None
    owner: str | None = None
    content_permissions: str = "ManagedByOwner"


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_LIMIT)
    parent: str | None = None
    move: bool = Field(
        default=False, description="parent 有值或要移到最上層（parent 為 null）時設為 true"
    )
    owner: str | None = None
    content_permissions: str | None = None
    rules: list[Rule] | None = None
    workbook_defaults: list[Rule] | None = None


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_LIMIT)
    full_name: str | None = None
    domain: str | None = None
    email: str | None = None
    site_role: str = "Viewer"
    groups: list[str] = Field(default_factory=list)


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    email: str | None = None
    site_role: str | None = None
    groups: list[str] | None = None


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_LIMIT)
    description: str = ""
    members: list[str] = Field(
        default_factory=list, description="使用者名稱（同名在多個網域時寫成 網域\\名稱）"
    )


class GroupPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_LIMIT)
    description: str | None = None
    members: list[str] | None = None


class ReportPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_LIMIT)
    project: str | None = None
    owner: str | None = None
    rules: list[Rule] | None = None


class ViewRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rules: list[Rule]


class WorkbookPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = None
    owner: str | None = None
    rules: list[Rule] | None = None
    views: list[ViewRules] | None = None


def _bad(message: str, status: int = 422) -> HTTPException:
    return HTTPException(status, message)


def _split_login(login: str) -> tuple[str | None, str]:
    if "\\" in login:
        domain, name = login.split("\\", 1)
        return domain, name
    return None, login


def admin_router(
    site: SiteStore,
    store: ReportStore,
    rights: Rights,
    current_user: Callable[[Request], Identity | None],
) -> APIRouter:
    router = APIRouter()

    def actor(request: Request) -> Identity:
        user = current_user(request)
        if user is None:
            raise HTTPException(403, "站台管理需要登入")
        return user

    def is_admin(user: Identity) -> bool:
        return (user.site_role or "") in ADMIN_ROLES

    def admin(request: Request) -> Identity:
        user = actor(request)
        if not is_admin(user):
            raise HTTPException(403, "站台管理只有站台管理員可以使用")
        return user

    def change(
        user: Identity,
        action: str,
        target: str,
        apply: Callable[[SiteData], None],
        detail: dict[str, Any] | None = None,
    ) -> SiteData:
        try:
            return site.change(user.username, action, target, apply, detail)
        except SiteError as exc:
            raise _bad(str(exc)) from exc

    # ------------------------------------------------------------ 檢查

    def check_user(data: SiteData, name: str | None, what: str) -> None:
        if name is None:
            return
        domain, plain = _split_login(name)
        if data.user(plain, domain) is None:
            raise _bad(f"{what}「{name}」不是站台的使用者")

    def check_rules(
        data: SiteData, rules: list[Rule], allowed: dict[str, str] | None = None
    ) -> None:
        for rule in rules:
            grantee = rule.grantee
            if grantee.kind == "user":
                if data.user(grantee.name, grantee.domain) is None:
                    raise _bad(f"權限的對象「{grantee.name}」不是站台的使用者")
            elif (
                grantee.name.casefold() != ALL_USERS.casefold() and data.group(grantee.name) is None
            ):
                raise _bad(f"權限的對象「{grantee.name}」不是站台的群組")
            for capability in rule.capabilities:
                if not CAPABILITY.match(capability):
                    raise _bad(f"能力的名稱「{capability}」不正確")
                if allowed is not None and capability not in allowed:
                    raise _bad(f"Project 沒有「{capability}」這個能力")
        keys = [
            (r.grantee.kind, r.grantee.name.casefold(), (r.grantee.domain or "").casefold())
            for r in rules
        ]
        if len(keys) != len(set(keys)):
            raise _bad("同一個對象只能有一筆權限")

    def project_path(data: SiteData, project_id: str) -> str:
        names: list[str] = []
        seen: set[str] = set()
        current = data.project(project_id)
        while current is not None and current.id not in seen:
            names.append(current.name)
            seen.add(current.id)
            current = data.project(current.parent) if current.parent else None
        return " / ".join(reversed(names)) if names else project_id

    def owned(data: SiteData, name: str) -> list[str]:
        key = name.casefold()
        found = [f"Project「{p.name}」" for p in data.projects if (p.owner or "").casefold() == key]
        found += [f"報表「{r.name}」" for r in store.records() if r.owner.casefold() == key]
        found += [f"報表「{w.name}」" for w in data.workbooks if (w.owner or "").casefold() == key]
        return found

    def locked(data: SiteData, project_id: str) -> bool:
        """報表的權限由 Project 決定（Project 或會套用到巢狀 Project 的上層鎖定權限）。"""
        chain = data.permissions().ancestors(project_id)
        for depth, project in enumerate(chain):
            if project.content_permissions == "LockedToProject" or (
                depth == 0 and project.content_permissions == "LockedToProjectWithoutNested"
            ):
                return True
        return False

    def report_json(data: SiteData, record: ReportRecord) -> dict[str, Any]:
        linked = next((w for w in data.workbooks if w.id == record.tableau_workbook), None)
        return {
            "id": record.id,
            "name": record.name,
            "project": record.project,
            "project_path": project_path(data, record.project),
            "owner": record.owner,
            "version": record.latest.version,
            "published_at": record.latest.published_at,
            "tableau_workbook": record.tableau_workbook if linked is not None else None,
            "rules": [
                r.model_dump() for r in (linked.rules if linked is not None else record.rules)
            ],
            "locked": locked(data, record.project),
        }

    def site_json(data: SiteData) -> dict[str, Any]:
        published = {r.tableau_workbook for r in store.records() if r.tableau_workbook}
        return {
            "source": data.source,
            "imported_at": data.imported_at,
            "updated_at": data.updated_at,
            "site_roles": list(SITE_ROLES),
            "workbook_capabilities": WORKBOOK_CAPABILITIES,
            "project_capabilities": PROJECT_CAPABILITIES,
            "content_permissions": list(CONTENT_PERMISSIONS),
            "projects": [
                {
                    **p.model_dump(),
                    "path": project_path(data, p.id),
                    "children": len(data.children(p.id)),
                }
                for p in data.projects
            ],
            "users": [{**u.model_dump(), "owns": len(owned(data, u.name))} for u in data.users],
            "groups": [
                {**g.model_dump(), "members": [u.name for u in data.members(g.name)]}
                for g in data.groups
            ],
            "reports": [report_json(data, r) for r in store.records()],
            "workbooks": [
                {
                    **w.model_dump(),
                    "project_path": project_path(data, w.project) if w.project else None,
                    "published": w.id in published,
                    "locked": locked(data, w.project) if w.project else False,
                }
                for w in data.workbooks
            ],
        }

    @router.get("/api/admin/site")
    def get_site(request: Request) -> dict[str, Any]:
        admin(request)
        return site_json(site.data())

    @router.get("/api/admin/changes")
    def get_changes(request: Request, limit: int = 200) -> list[dict[str, Any]]:
        admin(request)
        return site.changes(max(1, min(limit, 2000)))

    # ------------------------------------------------------------ Project

    @router.post("/api/admin/projects", status_code=201)
    def create_project(body: ProjectCreate, request: Request) -> dict[str, Any]:
        user = admin(request)
        project_id = new_project_id()

        def apply(data: SiteData) -> None:
            if body.parent is not None and data.project(body.parent) is None:
                raise SiteError("上層 Project 不存在")
            check_user(data, body.owner, "擁有者")
            if body.content_permissions not in CONTENT_PERMISSIONS:
                raise SiteError("權限管理方式不正確")
            # 與 Tableau 相同：新的 Project 沿用上層（最上層時沿用「預設」Project）的權限
            template = data.project(body.parent) if body.parent else data.project("default")
            data.projects.append(
                ProjectEntry(
                    id=project_id,
                    name=body.name.strip(),
                    parent=body.parent,
                    owner=body.owner or user.username,
                    content_permissions=body.content_permissions,
                    rules=list(template.rules) if template else [],
                    workbook_defaults=list(template.workbook_defaults) if template else [],
                )
            )

        data = change(user, "project.create", body.name, apply)
        project = data.project(project_id)
        assert project is not None
        return {**project.model_dump(), "path": project_path(data, project_id)}

    @router.patch("/api/admin/projects/{project_id}")
    def update_project(project_id: str, body: ProjectPatch, request: Request) -> dict[str, Any]:
        user = admin(request)

        def apply(data: SiteData) -> None:
            project = data.project(project_id)
            if project is None:
                raise SiteError("找不到 Project")
            updates: dict[str, Any] = {}
            if body.name is not None:
                updates["name"] = body.name.strip()
            if body.move:
                if body.parent is not None and data.project(body.parent) is None:
                    raise SiteError("上層 Project 不存在")
                if body.parent == project_id:
                    raise SiteError("Project 不能放在自己裡面")
                updates["parent"] = body.parent
            if body.owner is not None:
                check_user(data, body.owner, "擁有者")
                updates["owner"] = body.owner
            if body.content_permissions is not None:
                if body.content_permissions not in CONTENT_PERMISSIONS:
                    raise SiteError("權限管理方式不正確")
                updates["content_permissions"] = body.content_permissions
            if body.rules is not None:
                check_rules(data, body.rules, PROJECT_CAPABILITIES)
                updates["rules"] = body.rules
            if body.workbook_defaults is not None:
                check_rules(data, body.workbook_defaults)
                updates["workbook_defaults"] = body.workbook_defaults
            index = data.projects.index(project)
            data.projects[index] = project.model_copy(update=updates)

        fields = sorted(k for k, v in body.model_dump(exclude_unset=True).items() if k != "move")
        data = change(user, "project.update", project_id, apply, {"fields": fields})
        project = data.project(project_id)
        assert project is not None
        return {**project.model_dump(), "path": project_path(data, project_id)}

    @router.delete("/api/admin/projects/{project_id}", status_code=204)
    def delete_project(project_id: str, request: Request) -> None:
        user = admin(request)

        def apply(data: SiteData) -> None:
            project = data.project(project_id)
            if project is None:
                raise SiteError("找不到 Project")
            content = [f"Project「{p.name}」" for p in data.children(project_id)]
            content += [f"報表「{r.name}」" for r in store.records() if r.project == project_id]
            content += [f"報表「{w.name}」" for w in data.workbooks if w.project == project_id]
            if content:
                raise SiteError(f"Project 中還有內容，先移走或刪除：{'、'.join(content[:10])}")
            if len(data.projects) == 1:
                raise SiteError("站台至少要有一個 Project")
            data.projects.remove(project)

        change(user, "project.delete", project_id, apply)

    # ------------------------------------------------------------ 使用者

    def find_user(data: SiteData, name: str, domain: str | None) -> DirectoryUser:
        found = data.user(name, domain)
        if found is None:
            raise SiteError(f"找不到使用者 {name}")
        return found

    def check_groups(data: SiteData, groups: list[str]) -> list[str]:
        result: list[str] = []
        for group in groups:
            if group.casefold() == ALL_USERS.casefold():
                continue
            entry = data.group(group)
            if entry is None:
                raise SiteError(f"群組「{group}」不存在")
            if entry.name not in result:
                result.append(entry.name)
        return result

    @router.post("/api/admin/users", status_code=201)
    def create_user(body: UserCreate, request: Request) -> dict[str, Any]:
        user = admin(request)
        try:
            email = checked_email(body.email)
        except SiteError as exc:
            raise _bad(str(exc)) from exc

        def apply(data: SiteData) -> None:
            if data.user(body.name, body.domain or None) is not None or (
                body.domain is None
                and any(u.name.casefold() == body.name.casefold() for u in data.users)
            ):
                raise SiteError(f"使用者 {body.name} 已經存在")
            if body.site_role not in SITE_ROLES:
                raise SiteError("站台角色不正確")
            data.users.append(
                DirectoryUser(
                    name=body.name.strip(),
                    full_name=body.full_name or None,
                    domain=body.domain or None,
                    email=email,
                    site_role=body.site_role,
                    groups=_ordered(check_groups(data, body.groups)),
                )
            )

        data = change(user, "user.create", body.name, apply)
        created = data.user(body.name, body.domain or None)
        assert created is not None
        return created.model_dump()

    @router.patch("/api/admin/users/{name}")
    def update_user(
        name: str, body: UserPatch, request: Request, domain: str | None = None
    ) -> dict[str, Any]:
        user = admin(request)

        def apply(data: SiteData) -> None:
            target = find_user(data, name, domain)
            updates: dict[str, Any] = {}
            if body.full_name is not None:
                updates["full_name"] = body.full_name or None
            if body.email is not None:
                updates["email"] = checked_email(body.email)
            if body.site_role is not None:
                if body.site_role not in SITE_ROLES:
                    raise SiteError("站台角色不正確")
                if (
                    target.name.casefold() == user.username.casefold()
                    and body.site_role not in ADMIN_ROLES
                ):
                    raise SiteError("不能拿掉自己的站台管理員角色（請另一位管理員修改）")
                updates["site_role"] = body.site_role
            if body.groups is not None:
                updates["groups"] = _ordered(check_groups(data, body.groups))
            data.users[data.users.index(target)] = target.model_copy(update=updates)

        fields = sorted(body.model_dump(exclude_unset=True))
        data = change(user, "user.update", name, apply, {"fields": fields})
        return find_user(data, name, domain).model_dump()

    def store_rules(fn: Callable[[list[Rule]], list[Rule]]) -> None:
        """報表庫中各報表自己的權限（沒有沿用 Tableau workbook 權限的報表）。"""
        for record in store.records():
            rules = fn(record.rules)
            if rules != record.rules:
                store.update(record.id, {"rules": rules})

    @router.delete("/api/admin/users/{name}", status_code=204)
    def delete_user(name: str, request: Request, domain: str | None = None) -> None:
        user = admin(request)

        def apply(data: SiteData) -> None:
            target = find_user(data, name, domain)
            if target.name.casefold() == user.username.casefold():
                raise SiteError("不能刪除自己")
            content = owned(data, target.name)
            if content:
                raise SiteError(f"{target.name} 還擁有內容，先換擁有者：{'、'.join(content[:10])}")
            data.users.remove(target)
            _drop_grantee(data, "user", target.name)

        change(user, "user.delete", name, apply)
        store_rules(lambda rules: _without(rules, "user", name))

    # ------------------------------------------------------------ 群組

    def member_users(data: SiteData, members: list[str]) -> list[DirectoryUser]:
        found: list[DirectoryUser] = []
        for login in members:
            domain, plain = _split_login(login)
            user = data.user(plain, domain)
            if user is None:
                raise SiteError(f"找不到使用者 {login}")
            found.append(user)
        return found

    def set_members(data: SiteData, group: str, members: list[DirectoryUser]) -> None:
        wanted = {(u.name.casefold(), (u.domain or "").casefold()) for u in members}
        key = group.casefold()
        for index, user in enumerate(data.users):
            inside = (user.name.casefold(), (user.domain or "").casefold()) in wanted
            groups = [g for g in user.groups if g.casefold() != key]
            if inside:
                groups.append(group)
            data.users[index] = user.model_copy(update={"groups": _ordered(groups)})

    @router.post("/api/admin/groups", status_code=201)
    def create_group(body: GroupCreate, request: Request) -> dict[str, Any]:
        user = admin(request)

        def apply(data: SiteData) -> None:
            name = body.name.strip()
            if data.group(name) is not None or name.casefold() == ALL_USERS.casefold():
                raise SiteError(f"群組「{name}」已經存在")
            data.groups.append(GroupEntry(name=name, description=body.description))
            set_members(data, name, member_users(data, body.members))

        data = change(user, "group.create", body.name, apply)
        return {
            "name": body.name.strip(),
            "members": [u.name for u in data.members(body.name.strip())],
        }

    @router.patch("/api/admin/groups/{name}")
    def update_group(name: str, body: GroupPatch, request: Request) -> dict[str, Any]:
        user = admin(request)
        result: dict[str, Any] = {}

        def apply(data: SiteData) -> None:
            group = data.group(name)
            if group is None:
                raise SiteError(f"找不到群組「{name}」")
            current = group.name
            if body.name is not None and body.name.strip() != current:
                renamed = body.name.strip()
                if data.group(renamed) is not None or renamed.casefold() == ALL_USERS.casefold():
                    raise SiteError(f"群組「{renamed}」已經存在")
                for index, member in enumerate(data.users):
                    data.users[index] = member.model_copy(
                        update={
                            "groups": _ordered(
                                [
                                    renamed if g.casefold() == current.casefold() else g
                                    for g in member.groups
                                ]
                            )
                        }
                    )
                _rename_grantee(data, current, renamed)
                group = group.model_copy(update={"name": renamed})
                current = renamed
                result["warning"] = (
                    "報表公式中的 ISMEMBEROF 以群組名稱判斷：用到舊名稱的列層級權限要一併修改"
                )
            if body.description is not None:
                group = group.model_copy(update={"description": body.description})
            data.groups[[g.name.casefold() for g in data.groups].index(name.casefold())] = group
            if body.members is not None:
                set_members(data, current, member_users(data, body.members))
            result["name"] = current

        data = change(
            user,
            "group.update",
            name,
            apply,
            {"fields": sorted(body.model_dump(exclude_unset=True))},
        )
        if result["name"].casefold() != name.casefold():
            store_rules(lambda rules: _renamed(rules, name, result["name"]))
        return {**result, "members": [u.name for u in data.members(result["name"])]}

    @router.delete("/api/admin/groups/{name}", status_code=204)
    def delete_group(name: str, request: Request) -> None:
        user = admin(request)

        def apply(data: SiteData) -> None:
            group = data.group(name)
            if group is None:
                raise SiteError(f"找不到群組「{name}」")
            data.groups.remove(group)
            set_members(data, group.name, [])
            _drop_grantee(data, "group", group.name)

        change(user, "group.delete", name, apply)
        store_rules(lambda rules: _without(rules, "group", name))

    # ------------------------------------------------------------ 報表

    def leader(user: Identity, project_id: str) -> bool:
        permissions = rights.permissions
        if permissions is None:
            return False
        for project in permissions.ancestors(project_id):
            if (project.owner or "").casefold() == user.username.casefold():
                return True
            if decide(project.rules, user, "ProjectLeader"):
                return True
        return False

    def may(user: Identity, record: ReportRecord, capability: str) -> bool:
        if is_admin(user) or record.owner.casefold() == user.username.casefold():
            return True
        if leader(user, record.project):
            return True
        permissions = rights.permissions
        return (
            permissions is not None
            and permissions.access(user, rights.entry(record), capability).allowed
        )

    @router.patch("/api/admin/reports/{report_id}")
    def update_report(report_id: str, body: ReportPatch, request: Request) -> dict[str, Any]:
        user = actor(request)
        record = store.get(report_id)
        if record is None:
            raise HTTPException(404, f"找不到報表「{report_id}」")
        structural = body.name is not None or body.project is not None or body.owner is not None
        if structural and not is_admin(user):
            raise HTTPException(403, "改名、移動與換擁有者只有站台管理員可以做")
        if body.rules is not None and not may(user, record, "ChangePermissions"):
            raise HTTPException(403, "沒有設定這份報表權限的權限")
        data = site.data()
        project = body.project or record.project
        if data.project(project) is None:
            raise _bad("Project 不存在")
        if body.owner is not None:
            check_user(data, body.owner, "擁有者")
        name = body.name.strip() if body.name is not None else record.name
        clash = store.find(project, name)
        if clash is not None and clash.id != record.id:
            raise _bad(f"Project 中已經有名為「{name}」的報表", 409)
        if body.rules is not None:
            if locked(data, project):
                raise _bad("這個 Project 鎖定權限：報表的權限要在 Project 上設定", 409)
            check_rules(data, body.rules)
        linked = next((w for w in data.workbooks if w.id == record.tableau_workbook), None)
        updates: dict[str, Any] = {"name": name, "project": project}
        if body.owner is not None:
            updates["owner"] = body.owner
        if body.rules is not None and linked is None:
            updates["rules"] = body.rules
        updated = store.update(record.id, updates)
        if body.rules is not None and linked is not None:
            # 沿用 Tableau workbook 權限的報表：權限存在站台資料
            def apply(site_data: SiteData) -> None:
                entry = next(w for w in site_data.workbooks if w.id == linked.id)
                site_data.workbooks[site_data.workbooks.index(entry)] = entry.model_copy(
                    update={"rules": body.rules}
                )

            data = change(user, "report.permissions", report_id, apply)
        else:
            site.record(
                user.username,
                "report.update",
                report_id,
                {"fields": sorted(body.model_dump(exclude_unset=True))},
            )
        return report_json(data, updated)

    @router.delete("/api/admin/reports/{report_id}", status_code=204)
    def delete_report(report_id: str, request: Request) -> None:
        user = actor(request)
        record = store.get(report_id)
        if record is None:
            raise HTTPException(404, f"找不到報表「{report_id}」")
        if not may(user, record, "Delete"):
            raise HTTPException(403, "沒有刪除這份報表的權限")
        store.remove(record.id)
        site.record(user.username, "report.delete", report_id, {"name": record.name})

    @router.patch("/api/admin/workbooks/{workbook_id}")
    def update_workbook(workbook_id: str, body: WorkbookPatch, request: Request) -> dict[str, Any]:
        user = admin(request)

        def apply(data: SiteData) -> None:
            entry = next((w for w in data.workbooks if w.id == workbook_id), None)
            if entry is None:
                raise SiteError("找不到 workbook")
            updates: dict[str, Any] = {}
            if body.project is not None:
                if data.project(body.project) is None:
                    raise SiteError("Project 不存在")
                updates["project"] = body.project
            if body.owner is not None:
                check_user(data, body.owner, "擁有者")
                updates["owner"] = body.owner
            if body.rules is not None:
                check_rules(data, body.rules)
                updates["rules"] = body.rules
            if body.views is not None:
                views = {v.name: v for v in entry.views}
                for item in body.views:
                    if item.name not in views:
                        raise SiteError(f"workbook 中沒有 view「{item.name}」")
                    check_rules(data, item.rules)
                    views[item.name] = views[item.name].model_copy(update={"rules": item.rules})
                updates["views"] = list(views.values())
            data.workbooks[data.workbooks.index(entry)] = entry.model_copy(update=updates)

        data = change(
            user,
            "workbook.update",
            workbook_id,
            apply,
            {"fields": sorted(body.model_dump(exclude_unset=True))},
        )
        entry = next(w for w in data.workbooks if w.id == workbook_id)
        return entry.model_dump()

    return router


def _ordered(groups: list[str]) -> list[str]:
    """使用者的群組：All Users 在前，其餘依名稱排序。"""
    rest = sorted({g for g in groups if g.casefold() != ALL_USERS.casefold()}, key=str.casefold)
    return [ALL_USERS, *rest]


def _without(rules: list[Rule], kind: str, name: str) -> list[Rule]:
    key = name.casefold()
    return [r for r in rules if not (r.grantee.kind == kind and r.grantee.name.casefold() == key)]


def _renamed(rules: list[Rule], old: str, new: str) -> list[Rule]:
    key = old.casefold()
    return [
        r.model_copy(update={"grantee": r.grantee.model_copy(update={"name": new})})
        if r.grantee.kind == "group" and r.grantee.name.casefold() == key
        else r
        for r in rules
    ]


def _drop_grantee(data: SiteData, kind: str, name: str) -> None:
    """刪除使用者或群組時一併拿掉它的權限規則。"""
    _each_rules(data, lambda rules: _without(rules, kind, name))


def _rename_grantee(data: SiteData, old: str, new: str) -> None:
    _each_rules(data, lambda rules: _renamed(rules, old, new))


def _each_rules(data: SiteData, fn: Callable[[list[Rule]], list[Rule]]) -> None:
    for index, project in enumerate(data.projects):
        data.projects[index] = project.model_copy(
            update={"rules": fn(project.rules), "workbook_defaults": fn(project.workbook_defaults)}
        )
    for index, workbook in enumerate(data.workbooks):
        data.workbooks[index] = workbook.model_copy(
            update={
                "rules": fn(workbook.rules),
                "views": [v.model_copy(update={"rules": fn(v.rules)}) for v in workbook.views],
            }
        )


__all__ = ["admin_router"]
