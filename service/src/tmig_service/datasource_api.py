"""伺服器上的 Published Data Source 的 API（tmig_service.datasources）。

- ``GET    /api/datasources``：看得到的資料源（Read），與用到它的報表
- ``POST   /api/datasources/publish?name=&project=&comment=``：發布 .tdsx（要 Project 的發布權限；
  同一個網址名稱已經有時為新版本，要那個資料源的「覆蓋」）
- ``GET    /api/datasources/{id}``：連線、欄位、每一版
- ``PATCH  /api/datasources/{id}``：改名、移到其他 Project、換擁有者（站台管理員）；權限（管理員、擁有者、
  Project Leader、有「設定權限」的人）
- ``DELETE /api/datasources/{id}``：刪除（移到報表庫的 trash/；管理員、擁有者、有「刪除」的人）
- ``GET    /api/datasources/{id}/versions/{n}/download``：下載 .tdsx（「下載」）
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from tmig.access.permissions import ADMIN_ROLES, Permissions, Rule, decide
from tmig.planner.identity import Identity
from tmig_service.datasources import (
    DATASOURCE_CAPABILITIES,
    DatasourceError,
    DatasourceRecord,
    DatasourceStore,
)
from tmig_service.site import SiteStore

UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024


class DatasourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    project: str | None = None
    owner: str | None = None
    rules: list[Rule] | None = None


def may(
    permissions: Permissions | None,
    user: Identity | None,
    record: DatasourceRecord,
    capability: str,
) -> bool:
    """資料源的能力：管理員、擁有者、所在 Project（與上層）的擁有者與 Project Leader 全部可以；其餘依規則。
    沒有權限檔時不限制。"""
    if permissions is None:
        return True
    if user is None:
        return False
    if (user.site_role or "") in ADMIN_ROLES or record.owner.casefold() == user.username.casefold():
        return True
    for project in permissions.ancestors(record.project):
        if (project.owner or "").casefold() == user.username.casefold() or decide(
            project.rules, user, "ProjectLeader"
        ):
            return True
    return decide(record.rules, user, capability)


def datasource_router(
    store: DatasourceStore,
    site: SiteStore | None,
    permissions: Callable[[], Permissions | None],
    current_user: Callable[[Request], Identity | None],
    used_by: Callable[[str, Identity | None], list[dict[str, Any]]],
    can_publish: Callable[[Identity | None, str], tuple[bool, str | None]],
    upload_dir: Path,
) -> APIRouter:
    """used_by：用到某個網址名稱的資料源、登入者看得到的報表；can_publish：能不能發布到某個 Project。"""
    router = APIRouter()

    def user_of(request: Request) -> Identity | None:
        return current_user(request)

    def check(record: DatasourceRecord, user: Identity | None, capability: str) -> None:
        if not may(permissions(), user, record, capability):
            if capability == "Read":
                raise HTTPException(404, "找不到資料源")
            label = DATASOURCE_CAPABILITIES.get(capability, capability)
            raise HTTPException(403, f"沒有這個資料源的「{label}」權限")

    def find(datasource_id: str, user: Identity | None) -> DatasourceRecord:
        record = store.get(datasource_id)
        if record is None:
            raise HTTPException(404, "找不到資料源")
        check(record, user, "Read")
        return record

    def project_path(project_id: str) -> str:
        current = permissions()
        if current is None:
            return project_id
        names: list[str] = []
        for project in reversed(current.ancestors(project_id)):
            names.append(project.name)
        return " / ".join(names) or project_id

    def record_json(record: DatasourceRecord, user: Identity | None) -> dict[str, Any]:
        current = permissions()
        return {
            **record.model_dump(exclude={"versions"}),
            "version": record.latest.version,
            "published_at": record.latest.published_at,
            "project_path": project_path(record.project),
            "used_by": used_by(record.content_url, user),
            "can": {
                capability: may(current, user, record, capability)
                for capability in DATASOURCE_CAPABILITIES
            },
        }

    @router.get("/api/datasources")
    def listing(request: Request) -> list[dict[str, Any]]:
        user = user_of(request)
        return [
            record_json(r, user) for r in store.records() if may(permissions(), user, r, "Read")
        ]

    @router.get("/api/datasources/{datasource_id}")
    def detail(datasource_id: str, request: Request) -> dict[str, Any]:
        user = user_of(request)
        record = find(datasource_id, user)
        ir = store.ir(record)
        datasource = ir.datasources[0]
        return {
            **record_json(record, user),
            "versions": [v.model_dump() for v in reversed(record.versions)],
            "fields": [
                {
                    "caption": f.caption,
                    "role": f.role,
                    "datatype": f.datatype,
                    "formula": f.calc.formula if f.calc is not None else None,
                    "hidden": f.hidden,
                }
                for f in datasource.fields
                if not f.id.startswith("__")
            ]
            if may(permissions(), user, record, "Connect")
            else [],
            "capabilities": DATASOURCE_CAPABILITIES,
        }

    @router.post("/api/datasources/publish")
    async def publish(
        request: Request, name: str, project: str = "default", comment: str = ""
    ) -> dict[str, Any]:
        user = user_of(request)
        allowed, reason = can_publish(user, project)
        if not allowed:
            raise HTTPException(403, reason or "沒有發布到這個 Project 的權限")
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"pds-{secrets.token_hex(8)}.tdsx"
        size = 0
        try:
            with target.open("wb") as sink:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > UPLOAD_LIMIT:
                        raise HTTPException(413, "資料源太大")
                    sink.write(chunk)
            if size == 0:
                raise HTTPException(422, "沒有收到 .tdsx")
            from tmig_service.datasources import _content_url, slugify

            try:
                content_url = _content_url(target) or slugify(name)
            except DatasourceError as exc:
                raise HTTPException(422, str(exc)) from exc
            existing = store.find(content_url)
            if existing is not None:
                # 覆蓋：要資料源所在 Project 的發布權限與資料源的「覆蓋」（與 Tableau 相同）
                allowed, reason = can_publish(user, existing.project)
                if not allowed:
                    raise HTTPException(403, reason or "沒有發布到這個 Project 的權限")
                check(existing, user, "Write")
            owner = user.username if user is not None else "（沒有登入）"
            try:
                record = store.publish(
                    target,
                    name=existing.name if existing is not None else name,
                    project=existing.project if existing is not None else project,
                    owner=existing.owner if existing is not None else owner,
                    publisher=owner,
                    comment=comment,
                )
            except DatasourceError as exc:
                raise HTTPException(422, str(exc)) from exc
        finally:
            target.unlink(missing_ok=True)
        if site is not None and site.exists():
            site.record(owner, "datasource.publish", record.id, {"version": record.latest.version})
        return record_json(record, user)

    @router.patch("/api/datasources/{datasource_id}")
    def update(datasource_id: str, body: DatasourcePatch, request: Request) -> dict[str, Any]:
        user = user_of(request)
        record = find(datasource_id, user)
        admin = user is not None and (user.site_role or "") in ADMIN_ROLES
        if (
            body.name is not None or body.project is not None or body.owner is not None
        ) and not admin:
            raise HTTPException(403, "改名、移動與換擁有者只有站台管理員可以做")
        if body.rules is not None:
            check(record, user, "ChangePermissions")
        changes: dict[str, Any] = {}
        if body.name is not None:
            changes["name"] = body.name.strip()
        if body.project is not None:
            current = permissions()
            if current is not None and current.project(body.project) is None:
                raise HTTPException(422, "Project 不存在")
            changes["project"] = body.project
        if body.owner is not None:
            if site is not None and site.exists() and site.data().user(body.owner) is None:
                raise HTTPException(422, f"擁有者「{body.owner}」不是站台的使用者")
            changes["owner"] = body.owner
        if body.rules is not None:
            for rule in body.rules:
                unknown = [c for c in rule.capabilities if c not in DATASOURCE_CAPABILITIES]
                if unknown:
                    raise HTTPException(422, f"資料源沒有「{'、'.join(unknown)}」這個能力")
            changes["rules"] = body.rules
        updated = store.update(datasource_id, changes)
        if site is not None and site.exists() and user is not None:
            site.record(
                user.username, "datasource.update", datasource_id, {"fields": sorted(changes)}
            )
        return record_json(updated, user)

    @router.delete("/api/datasources/{datasource_id}", status_code=204)
    def delete(datasource_id: str, request: Request) -> None:
        user = user_of(request)
        record = find(datasource_id, user)
        check(record, user, "Delete")
        store.remove(record.id)
        if site is not None and site.exists() and user is not None:
            site.record(user.username, "datasource.delete", datasource_id, {"name": record.name})

    @router.get("/api/datasources/{datasource_id}/versions/{version}/download")
    def download(datasource_id: str, version: int, request: Request) -> FileResponse:
        user = user_of(request)
        record = find(datasource_id, user)
        check(record, user, "ExportXml")
        path = store.package(record, version)
        if not path.is_file():
            raise HTTPException(404, "找不到這一版")
        return FileResponse(
            path, media_type="application/octet-stream", filename=f"{record.content_url}.tdsx"
        )

    return router


__all__ = ["datasource_router", "may"]
