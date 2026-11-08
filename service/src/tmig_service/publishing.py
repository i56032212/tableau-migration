"""發布與版本（D0）：報表庫的 API。

- ``GET  /api/projects``：Project 與能不能發布到它
- ``GET  /api/reports``：報表庫中看得到的報表（最新版本、能不能下載、能不能覆蓋）
- ``GET  /api/reports/{id}``：報表與每一版
- ``POST /api/reports/publish?project=&name=&comment=&report=&base_version=&overwrite=&force=``：
  請求本文為報表套件（.tmigx）
- ``GET  /api/reports/{id}/versions/{n}/package``：下載某一版的報表套件（n 可以是 latest）
- ``POST /api/reports/{id}/versions/{n}/restore``：回復某一版（發布一個內容相同的新版本）
- ``GET  /api/reports/{id}/compare?base=&target=``：兩版的差異（只有名稱）

發布的回應：

- 200：``{"report": …, "version": …, "problems": […]}``（problems 只剩提醒）
- 409 ``exists``：同一個 Project 已有同名的報表；要覆蓋時加 overwrite=true
- 409 ``conflict``：作者開始修改的版本（base_version）之後，伺服器上又有新版本；列出兩邊的差異，
  作者決定覆蓋時加 force=true
- 422 ``checks``：伺服器的檢查沒有通過；``package``：不是正確的報表套件
- 403：沒有權限（detail 為原因）

錯誤的 detail 為 ``{"error": 種類, "message": 說明, …}``。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from tmig.data.runtime import DataRuntime
from tmig.diff import diff_ir
from tmig.ir import IR
from tmig.package import PackageError
from tmig.planner.identity import Identity
from tmig_service.access import Rights
from tmig_service.checks import check_report, errors
from tmig_service.store import ConflictError, ReportRecord, ReportStore, Staged

MAX_UPLOAD_ENV = "TMIG_MAX_UPLOAD_MB"
DEFAULT_MAX_UPLOAD_MB = 2048
NAME_LIMIT = 255
COMMENT_LIMIT = 2000
MEDIA_TYPE = "application/vnd.tmig.tmigx"


def max_upload_bytes() -> int:
    value = os.environ.get(MAX_UPLOAD_ENV)
    return (int(value) if value and value.isdigit() else DEFAULT_MAX_UPLOAD_MB) << 20


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = ""


def failure(status: int, error: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"error": error, "message": message, **extra})


def publishing_router(
    store: ReportStore,
    rights: Rights,
    runtime: DataRuntime,
    loaded_ir: Callable[[str], tuple[Path, IR]],
    taken: Callable[[], list[str]],
    current_user: Callable[[Request], Identity | None],
) -> APIRouter:
    """loaded_ir：報表 id → 目前版本的目錄與 IR（查詢服務的快取）；taken：已經用掉的報表 id（含 fixtures）。"""
    router = APIRouter()

    def summary(record: ReportRecord, user: Identity | None) -> dict[str, Any]:
        latest = record.latest
        return {
            "id": record.id,
            "name": record.name,
            "project": record.project,
            "project_name": rights.project_path(record.project),
            "owner": record.owner,
            "version": latest.version,
            "published_at": latest.published_at,
            "publisher": latest.publisher,
            "comment": latest.comment,
            "url": f"/w/{record.id}",
            "can_download": rights.download(user, record).allowed,
            "can_overwrite": rights.overwrite(user, record).allowed,
        }

    def visible(record: ReportRecord, user: Identity | None) -> bool:
        directory, ir = loaded_ir(record.id)
        return rights.read(user, record, directory, ir).allowed

    def require(report_id: str, user: Identity | None) -> ReportRecord:
        record = store.get(report_id)
        if record is None or not visible(record, user):
            raise HTTPException(404, f"找不到報表「{report_id}」")
        return record

    def version_number(record: ReportRecord, version: str) -> int:
        if version == "latest":
            return record.latest.version
        if not version.isdigit() or record.version(int(version)) is None:
            raise HTTPException(404, f"報表「{record.name}」沒有第 {version} 版")
        return int(version)

    def stage(package: Path) -> Staged:
        try:
            return store.stage(package)
        except PackageError as exc:
            package.unlink(missing_ok=True)
            raise failure(422, "package", str(exc)) from exc

    def conflict(record: ReportRecord, base: int | None, uploaded: IR) -> HTTPException:
        latest = record.latest
        latest_ir = store.read_ir(record, latest.version)
        base_ir = store.read_ir(record, base) if base is not None and record.version(base) else None
        started = f"，你的報表是從第 {base} 版開始改的" if base is not None else ""
        return failure(
            409,
            "conflict",
            f"伺服器上已有較新的第 {latest.version} 版（{latest.publisher}，{latest.published_at}）{started}",
            latest=latest.model_dump(),
            base_version=base,
            server_changes=[c.to_dict() for c in diff_ir(base_ir, latest_ir)] if base_ir else [],
            local_changes=[c.to_dict() for c in diff_ir(base_ir, uploaded)] if base_ir else [],
            overwrite_changes=[c.to_dict() for c in diff_ir(latest_ir, uploaded)],
        )

    async def receive(request: Request, target: Path) -> None:
        limit = max_upload_bytes()
        size = 0
        try:
            with target.open("wb") as sink:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise failure(413, "too_large", f"報表套件超過上限 {limit >> 20} MB")
                    sink.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if size == 0:
            target.unlink(missing_ok=True)
            raise failure(422, "package", "沒有收到報表套件")

    def checked(staged: Staged, user: Identity | None) -> list[dict[str, Any]]:
        """伺服器的檢查：有錯誤時丟掉套件並回應 422；通過時回傳剩下的提醒。"""
        problems = check_report(staged.ir, staged.directory, runtime, user, "server")
        if errors(problems):
            store.discard(staged)
            raise failure(
                422,
                "checks",
                "伺服器的檢查沒有通過",
                problems=[p.to_dict() for p in problems],
            )
        return [p.to_dict() for p in problems]

    @router.get("/api/projects")
    def projects(request: Request) -> list[dict[str, Any]]:
        user = current_user(request)
        found = [
            {
                "id": p.id,
                "name": p.name,
                "path": rights.project_path(p.id),
                "can_publish": rights.publish(user, p.id).allowed,
            }
            for p in rights.projects()
        ]
        return sorted(found, key=lambda p: p["path"])

    @router.get("/api/reports")
    def reports(request: Request) -> list[dict[str, Any]]:
        user = current_user(request)
        return [summary(r, user) for r in store.records() if visible(r, user)]

    @router.get("/api/reports/{report_id}")
    def report(report_id: str, request: Request) -> dict[str, Any]:
        user = current_user(request)
        record = require(report_id, user)
        return {
            **summary(record, user),
            "versions": [v.model_dump() for v in reversed(record.versions)],
        }

    @router.post("/api/reports/publish")
    async def publish(
        request: Request,
        name: str = "",
        project: str | None = None,
        comment: str = "",
        report: str | None = None,
        base_version: int | None = None,
        overwrite: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        user = await run_in_threadpool(current_user, request)
        name, comment = name.strip(), comment.strip()
        record = None
        tableau = None
        if report:
            record = store.get(report)
            if record is None:
                raise HTTPException(404, f"找不到報表「{report}」")
        else:
            if not name or len(name) > NAME_LIMIT or any(ord(c) < 32 for c in name):
                raise failure(422, "name", f"報表名稱不能是空的、不能超過 {NAME_LIMIT} 個字")
            if not project or rights.project(project) is None:
                raise failure(422, "project", f"找不到 Project「{project or ''}」")
            record = store.find(project, name)
            if record is None:
                tableau = rights.tableau_workbook(project, name)
            where = rights.project_path(project)
            if record is not None and not overwrite:
                raise failure(
                    409,
                    "exists",
                    f"「{where}」中已有報表「{record.name}」",
                    report=summary(record, user),
                )
            if tableau is not None and not overwrite:
                raise failure(
                    409,
                    "exists",
                    f"Tableau Server 的「{where}」中已有 workbook「{tableau.name}」："
                    "發布後由這份報表取代，沿用它的權限",
                    report=None,
                )
        if len(comment) > COMMENT_LIMIT:
            raise failure(422, "comment", f"版本說明不能超過 {COMMENT_LIMIT} 個字")
        if record is not None:
            access = rights.overwrite(user, record)
        elif tableau is not None:
            access = rights.takeover(user, tableau, str(project))
        else:
            access = rights.publish(user, str(project))
        if not access.allowed:
            raise HTTPException(403, access.reason or "沒有權限")
        upload = store.upload_path()
        await receive(request, upload)
        staged = await run_in_threadpool(stage, upload)
        problems = await run_in_threadpool(checked, staged, user)
        assert user is not None  # 沒有登入時 rights 已經拒絕
        try:
            if record is not None:
                if base_version is not None and base_version != record.latest.version and not force:
                    raise conflict(record, base_version, staged.ir)
                committed = store.commit(
                    staged,
                    report_id=record.id,
                    name=record.name,
                    project=record.project,
                    owner=record.owner,
                    publisher=user.username,
                    comment=comment,
                    expected=record.latest.version,
                )
            else:
                assert project is not None
                entry = rights.permissions.project(project) if rights.permissions else None
                committed = store.commit(
                    staged,
                    report_id=store.new_id(name, taken()),
                    name=name,
                    project=project,
                    owner=tableau.owner if tableau and tableau.owner else user.username,
                    publisher=user.username,
                    comment=comment,
                    expected=None,
                    rules=entry.workbook_defaults if entry and tableau is None else (),
                    tableau_workbook=tableau.id if tableau else None,
                )
        except ConflictError as exc:
            store.discard(staged)
            if exc.record is None:
                raise HTTPException(409, "報表在發布期間被移除，請重新整理後再試") from exc
            raise conflict(exc.record, base_version, staged.ir) from exc
        except HTTPException:
            store.discard(staged)
            raise
        return {
            "report": summary(committed, user),
            "version": committed.latest.model_dump(),
            "problems": problems,
        }

    @router.get("/api/reports/{report_id}/versions/{version}/package")
    def package(report_id: str, version: str, request: Request) -> FileResponse:
        user = current_user(request)
        record = require(report_id, user)
        access = rights.download(user, record)
        if not access.allowed:
            raise HTTPException(403, access.reason or "沒有下載的權限")
        number = version_number(record, version)
        return FileResponse(
            store.package(record, number),
            media_type=MEDIA_TYPE,
            filename=f"{record.name}.tmigx",
            headers={"X-Tmig-Report": record.id, "X-Tmig-Version": str(number)},
        )

    @router.post("/api/reports/{report_id}/versions/{version}/restore")
    def restore(
        report_id: str, version: str, request: Request, body: RestoreRequest | None = None
    ) -> dict[str, Any]:
        user = current_user(request)
        record = require(report_id, user)
        access = rights.overwrite(user, record)
        if not access.allowed or user is None:
            raise HTTPException(403, access.reason or "沒有權限")
        number = version_number(record, version)
        upload = store.upload_path()
        shutil.copyfile(store.package(record, number), upload)
        staged = stage(upload)
        problems = checked(staged, user)
        note = (body.comment.strip() if body else "") or f"回復第 {number} 版"
        try:
            committed = store.commit(
                staged,
                report_id=record.id,
                name=record.name,
                project=record.project,
                owner=record.owner,
                publisher=user.username,
                comment=note[:COMMENT_LIMIT],
                expected=record.latest.version,
                restored_from=number,
            )
        except ConflictError as exc:
            store.discard(staged)
            raise failure(409, "conflict", "報表在回復期間有了新的版本，請重新整理後再試") from exc
        return {
            "report": summary(committed, user),
            "version": committed.latest.model_dump(),
            "problems": problems,
        }

    @router.get("/api/reports/{report_id}/compare")
    def compare(report_id: str, base: int, target: int, request: Request) -> dict[str, Any]:
        user = current_user(request)
        record = require(report_id, user)
        for number in (base, target):
            if record.version(number) is None:
                raise HTTPException(404, f"報表「{record.name}」沒有第 {number} 版")
        changes = diff_ir(store.read_ir(record, base), store.read_ir(record, target))
        return {"base": base, "target": target, "changes": [c.to_dict() for c in changes]}

    return router


__all__ = ["MAX_UPLOAD_ENV", "publishing_router"]
