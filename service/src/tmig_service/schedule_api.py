"""排程管理與訂閱的 API（tmig_service.schedules）。

站台管理員：

- ``GET    /api/admin/schedules``：排程、萃取更新工作、訂閱、排程器的狀態與可以排程的報表
- ``PATCH  /api/admin/schedule-settings``：排程的時區、同時執行的工作數上限（workers；null 為預設值）
- ``POST   /api/admin/schedules``、``PATCH|DELETE /api/admin/schedules/{id}``：排程（名稱、類型、頻率、優先順序、
  執行方式（Parallel／Serial）、暫停）
- ``POST   /api/admin/extract-tasks``、``PATCH|DELETE /api/admin/extract-tasks/{id}``：萃取更新工作
- ``POST   /api/admin/jobs/{job}/run``：立即執行（排入排程器的佇列；報表的擁有者也可以立即更新自己報表的萃取）
- ``GET    /api/admin/runs?job=&limit=``：執行紀錄

每個登入者（與 Tableau 的「訂閱」相同）：

- ``GET    /api/subscription-options?report=``：可以選的訂閱排程、能不能替別人訂閱
- ``GET    /api/subscriptions``：我的訂閱
- ``POST   /api/subscriptions``：訂閱一個 view 或整份報表（訂閱者要能檢視這份報表，並且在站台中有 email；
  報表的擁有者、Project Leader 與站台管理員可以替別人訂閱）
- ``PATCH|DELETE /api/subscriptions/{id}``：訂閱者本人與站台管理員（報表的擁有者也可以刪除）
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tmig.access.permissions import ADMIN_ROLES, decide
from tmig.catalog import Catalog
from tmig.planner.identity import Identity
from tmig.schedule.model import (
    DEFAULT_WORKERS,
    MAX_WORKERS,
    ExtractTask,
    Frequency,
    Schedule,
    ScheduleError,
    ScheduleFile,
    Subscription,
    zone_info,
)
from tmig.schedule.runner import Job, plan_jobs
from tmig_service.access import Grant, Rights
from tmig_service.schedules import SUSPEND_AFTER, ScheduleStore
from tmig_service.site import SiteStore
from tmig_service.store import ReportStore

NAME_LIMIT = 255
PAPER_SIZES = ("A3", "A4", "A5", "Legal", "Letter", "Ledger", "Tabloid", "Unspecified")


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_LIMIT)
    kind: Literal["Extract", "Subscription"] | None = None
    priority: int | None = Field(default=None, ge=1, le=100)
    execution: Literal["Parallel", "Serial"] | None = None
    frequency: Frequency | None = None
    state: Literal["Active", "Suspended"] | None = None


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = Field(default=None, min_length=1)
    workers: int | None = Field(
        default=None, ge=1, le=MAX_WORKERS, description="同時執行的工作數上限；null 為預設值"
    )


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: str = Field(min_length=1)
    schedule: str = Field(min_length=1)
    refresh: Literal["full", "incremental"] = "full"
    priority: int | None = Field(default=None, ge=1, le=100)


class TaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: str | None = None
    refresh: Literal["full", "incremental"] | None = None
    priority: int | None = Field(default=None, ge=1, le=100)
    suspended: bool | None = None


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: str = Field(min_length=1)
    view: str | None = Field(default=None, description="view 名稱；None 為整份報表")
    subject: str = Field(default="", max_length=NAME_LIMIT)
    message: str | None = Field(default=None, max_length=5000)
    schedule: str = Field(min_length=1)
    attach_image: bool = True
    attach_pdf: bool = False
    page_size: str | None = None
    page_orientation: Literal["Portrait", "Landscape"] | None = None
    send_if_view_empty: bool = True
    users: list[str] | None = Field(default=None, description="訂閱者；省略時為自己")


class SubscriptionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=NAME_LIMIT)
    message: str | None = Field(default=None, max_length=5000)
    schedule: str | None = None
    attach_image: bool | None = None
    attach_pdf: bool | None = None
    page_size: str | None = None
    page_orientation: Literal["Portrait", "Landscape"] | None = None
    send_if_view_empty: bool | None = None
    suspended: bool | None = None


def _bad(message: str, status: int = 422) -> HTTPException:
    return HTTPException(status, message)


def schedule_router(
    schedules: ScheduleStore,
    site: SiteStore,
    store: ReportStore,
    rights: Rights,
    current_user: Callable[[Request], Identity | None],
    catalog: Callable[[], Catalog],
    access: Callable[[str, Identity], Grant | None],
) -> APIRouter:
    """catalog：可以排程的報表（fixtures 與報表庫，與查詢服務相同）；access：某人對某份報表的權限
    （報表不存在時為 None）。"""
    router = APIRouter()

    def actor(request: Request) -> Identity:
        user = current_user(request)
        if user is None:
            raise HTTPException(403, "需要登入")
        return user

    def is_admin(user: Identity) -> bool:
        return (user.site_role or "") in ADMIN_ROLES

    def admin(request: Request) -> Identity:
        user = actor(request)
        if not is_admin(user):
            raise HTTPException(403, "排程管理只有站台管理員可以使用")
        return user

    def change(
        user: Identity,
        action: str,
        target: str,
        apply: Callable[[ScheduleFile], None],
        detail: dict[str, Any] | None = None,
    ) -> ScheduleFile:
        try:
            return schedules.change(user.username, action, target, apply, detail)
        except ScheduleError as exc:
            raise _bad(str(exc)) from exc

    def owns(user: Identity, report_id: str) -> bool:
        """報表的擁有者或所在 Project（與上層）的擁有者、Project Leader。"""
        record = store.get(report_id)
        name = user.username.casefold()
        if record is not None:
            if record.owner.casefold() == name:
                return True
            project_id = record.project
        else:
            data = site.data()
            report = catalog().report(report_id)
            workbook = (
                next(
                    (
                        w
                        for w in data.workbooks
                        if (report.url_name and w.content_url == report.url_name)
                        or w.name == report.ir.workbook.name
                    ),
                    None,
                )
                if report is not None
                else None
            )
            if workbook is None:
                return False
            if (workbook.owner or "").casefold() == name:
                return True
            if workbook.project is None:
                return False
            project_id = workbook.project
        permissions = rights.permissions
        if permissions is None:
            return False
        return any(
            (p.owner or "").casefold() == name or decide(p.rules, user, "ProjectLeader")
            for p in permissions.ancestors(project_id)
        )

    # ------------------------------------------------------------ 檢視用的資料

    def jobs(data: ScheduleFile) -> dict[str, Job]:
        return {j.id: j for j in plan_jobs(data, catalog())}

    def next_run(job: Job | None, data: ScheduleFile, now: dt.datetime) -> str | None:
        if job is None or not job.runnable or not data.timezone:
            return None
        assert job.schedule is not None and job.schedule.frequency is not None
        try:
            return job.schedule.frequency.next_run(now, data.timezone).isoformat()
        except ScheduleError:
            return None

    def schedule_json(schedule: Schedule, data: ScheduleFile, now: dt.datetime) -> dict[str, Any]:
        upcoming = None
        if schedule.frequency is not None and data.timezone and schedule.state == "Active":
            try:
                upcoming = schedule.frequency.next_run(now, data.timezone).isoformat()
            except ScheduleError:
                upcoming = None
        return {
            **schedule.model_dump(mode="json"),
            "description": schedule.frequency.describe() if schedule.frequency else None,
            "next_run": upcoming,
            "tasks": sum(1 for t in data.extract_refreshes if t.schedule == schedule.id),
            "subscriptions": sum(1 for s in data.subscriptions if s.schedule == schedule.id),
        }

    def task_json(
        task: ExtractTask,
        data: ScheduleFile,
        planned: dict[str, Job],
        last: dict[str, Any],
        now: dt.datetime,
    ) -> dict[str, Any]:
        job = planned.get(f"extract:{task.id}")
        schedule = data.schedule(task.schedule)
        targets = job.targets if job is not None else []
        return {
            **task.model_dump(mode="json"),
            "job": f"extract:{task.id}",
            "label": job.label() if job is not None else task.id,
            "schedule_name": schedule.name if schedule else None,
            "reports": sorted({t.report.name for t in targets}),
            "targets": [t.describe() for t in targets],
            "problem": job.problem if job is not None else None,
            "next_run": next_run(job, data, now),
            "last_run": last.get(f"extract:{task.id}"),
        }

    def subscription_json(
        sub: Subscription,
        data: ScheduleFile,
        planned: dict[str, Job],
        last: dict[str, Any],
        now: dt.datetime,
    ) -> dict[str, Any]:
        job = planned.get(f"subscription:{sub.id}")
        schedule = data.schedule(sub.schedule)
        report = job.targets[0].report if job is not None and job.targets else None
        return {
            **sub.model_dump(mode="json"),
            "job": f"subscription:{sub.id}",
            "schedule_name": schedule.name if schedule else None,
            "report_id": report.name if report is not None else sub.report,
            "report_name": report.label if report is not None else sub.workbook_name,
            "problem": job.problem if job is not None else None,
            "next_run": next_run(job, data, now),
            "last_run": last.get(f"subscription:{sub.id}"),
        }

    def reports_json() -> list[dict[str, Any]]:
        records = {r.id: r for r in store.records()}
        result: list[dict[str, Any]] = [
            {
                "id": source.name,
                "name": source.label,
                "published": True,
                "datasources": [
                    {"id": ds.id, "caption": ds.caption or ds.id} for ds in source.ir.datasources
                ],
                "views": [],
            }
            for source in catalog().sources
        ]
        for report in catalog().reports:
            ir = report.ir
            record = records.get(report.name)
            result.append(
                {
                    "id": report.name,
                    "name": report.label,
                    "published": record is not None,
                    "datasources": [
                        {"id": ds.id, "caption": ds.caption or ds.id}
                        for ds in ir.datasources
                        if ds.id in report.extract_datasources()
                    ],
                    "views": [d.name for d in ir.dashboards] + [w.name for w in ir.worksheets],
                }
            )
        return result

    @router.get("/api/admin/schedules")
    def overview(request: Request) -> dict[str, Any]:
        admin(request)
        data = schedules.data()
        now = dt.datetime.now(dt.UTC)
        planned = jobs(data)
        last = schedules.last_runs()
        return {
            "timezone": data.timezone,
            "workers": data.workers,
            "default_workers": DEFAULT_WORKERS,
            "max_workers": MAX_WORKERS,
            "source": data.source,
            "scheduler": schedules.status(now),
            "suspend_after": SUSPEND_AFTER,
            "schedules": [schedule_json(s, data, now) for s in data.schedules],
            "extract_tasks": [
                task_json(t, data, planned, last, now) for t in data.extract_refreshes
            ],
            "subscriptions": [
                subscription_json(s, data, planned, last, now) for s in data.subscriptions
            ],
            "reports": reports_json(),
        }

    @router.patch("/api/admin/schedule-settings")
    def settings(body: SettingsBody, request: Request) -> dict[str, Any]:
        user = admin(request)
        fields = body.model_fields_set
        if not fields:
            raise _bad("沒有要修改的設定")
        if "timezone" in fields:
            if body.timezone is None:
                raise _bad("排程的時區不能清除")
            try:
                zone_info(body.timezone)
            except ScheduleError as exc:
                raise _bad(str(exc)) from exc

        def apply(data: ScheduleFile) -> None:
            if body.timezone is not None:
                data.timezone = body.timezone
            if "workers" in fields:
                data.workers = body.workers  # 排程器下一次檢查時就依新的上限

        target = body.timezone or f"workers={body.workers or '預設'}"
        data = change(user, "schedule.settings", target, apply, {"fields": sorted(fields)})
        return {
            "timezone": data.timezone,
            "workers": data.workers,
            "default_workers": DEFAULT_WORKERS,
        }

    # ------------------------------------------------------------ 排程

    def check_schedule_body(body: ScheduleBody, creating: bool) -> None:
        if creating and (body.name is None or body.frequency is None):
            raise _bad("排程需要名稱與頻率")

    @router.post("/api/admin/schedules", status_code=201)
    def create_schedule(body: ScheduleBody, request: Request) -> dict[str, Any]:
        user = admin(request)
        check_schedule_body(body, creating=True)
        schedule_id = f"s{secrets.token_hex(6)}"

        def apply(data: ScheduleFile) -> None:
            if not data.timezone:
                raise ScheduleError("先設定排程的時區")
            assert body.name is not None
            data.schedules.append(
                Schedule(
                    id=schedule_id,
                    name=body.name.strip(),
                    kind=body.kind or "Extract",
                    state=body.state or "Active",
                    priority=body.priority or 50,
                    execution=body.execution or "Parallel",
                    frequency=body.frequency,
                )
            )

        data = change(user, "schedule.create", body.name or "", apply)
        created = data.schedule(schedule_id)
        assert created is not None
        return schedule_json(created, data, dt.datetime.now(dt.UTC))

    @router.patch("/api/admin/schedules/{schedule_id}")
    def update_schedule(schedule_id: str, body: ScheduleBody, request: Request) -> dict[str, Any]:
        user = admin(request)

        def apply(data: ScheduleFile) -> None:
            schedule = data.schedule(schedule_id)
            if schedule is None:
                raise ScheduleError("找不到排程")
            updates: dict[str, Any] = {
                k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None
            }
            if "name" in updates:
                updates["name"] = str(updates["name"]).strip()
            if body.frequency is not None:
                updates["frequency"] = body.frequency
                updates["problem"] = None
            data.schedules[data.schedules.index(schedule)] = schedule.model_copy(update=updates)

        fields = sorted(body.model_dump(exclude_unset=True))
        data = change(user, "schedule.update", schedule_id, apply, {"fields": fields})
        updated = data.schedule(schedule_id)
        assert updated is not None
        return schedule_json(updated, data, dt.datetime.now(dt.UTC))

    @router.delete("/api/admin/schedules/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: str, request: Request) -> None:
        user = admin(request)

        def apply(data: ScheduleFile) -> None:
            schedule = data.schedule(schedule_id)
            if schedule is None:
                raise ScheduleError("找不到排程")
            used = sum(1 for t in data.extract_refreshes if t.schedule == schedule_id) + sum(
                1 for s in data.subscriptions if s.schedule == schedule_id
            )
            if used:
                raise ScheduleError(
                    f"排程「{schedule.name}」還有 {used} 個工作，先移到其他排程或刪除"
                )
            data.schedules.remove(schedule)

        change(user, "schedule.delete", schedule_id, apply)

    # ------------------------------------------------------------ 萃取更新工作

    def check_schedule(data: ScheduleFile, schedule_id: str, kind: str) -> Schedule:
        schedule = data.schedule(schedule_id)
        if schedule is None:
            raise ScheduleError("找不到排程")
        if schedule.kind not in (None, kind):
            label = "萃取更新" if kind == "Extract" else "訂閱"
            raise ScheduleError(f"排程「{schedule.name}」不是{label}的排程")
        return schedule

    @router.post("/api/admin/extract-tasks", status_code=201)
    def create_task(body: TaskCreate, request: Request) -> dict[str, Any]:
        user = actor(request)
        if not is_admin(user) and not owns(user, body.report):
            raise HTTPException(403, "只有站台管理員與報表的擁有者可以排程萃取更新")
        report = catalog().report(body.report)
        if report is None:
            raise _bad(f"找不到報表「{body.report}」")
        if not report.extract_datasources():
            raise _bad(f"報表「{report.label}」沒有萃取型的資料源")
        source = body.report.startswith("pds:")
        if source and not is_admin(user):
            raise HTTPException(403, "資料源的萃取更新只有站台管理員可以排程")
        task_id = f"t{secrets.token_hex(6)}"

        def apply(data: ScheduleFile) -> None:
            check_schedule(data, body.schedule, "Extract")
            data.extract_refreshes.append(
                # 伺服器上的資料源：對象是 Published Data Source（更新伺服器上的那一份）
                ExtractTask(
                    id=task_id,
                    schedule=body.schedule,
                    refresh=body.refresh,
                    target_kind="datasource",
                    target_name=report.label,
                    content_url=report.url_name,
                    priority=body.priority,
                )
                if source
                else ExtractTask(
                    id=task_id,
                    schedule=body.schedule,
                    refresh=body.refresh,
                    target_kind="workbook",
                    target_name=report.label,
                    report=body.report,
                    priority=body.priority,
                )
            )

        data = change(user, "task.create", body.report, apply)
        task = next(t for t in data.extract_refreshes if t.id == task_id)
        return task_json(task, data, jobs(data), {}, dt.datetime.now(dt.UTC))

    def task_owner_or_admin(user: Identity, task: ExtractTask, data: ScheduleFile) -> None:
        if is_admin(user):
            return
        job = jobs(data).get(f"extract:{task.id}")
        reports = {t.report.name for t in job.targets} if job is not None else set()
        if not reports or not all(owns(user, r) for r in reports):
            raise HTTPException(403, "只有站台管理員與報表的擁有者可以修改這個萃取更新工作")

    @router.patch("/api/admin/extract-tasks/{task_id}")
    def update_task(task_id: str, body: TaskPatch, request: Request) -> dict[str, Any]:
        user = actor(request)
        current = next((t for t in schedules.data().extract_refreshes if t.id == task_id), None)
        if current is None:
            raise HTTPException(404, "找不到萃取更新工作")
        task_owner_or_admin(user, current, schedules.data())

        def apply(data: ScheduleFile) -> None:
            task = next((t for t in data.extract_refreshes if t.id == task_id), None)
            if task is None:
                raise ScheduleError("找不到萃取更新工作")
            updates: dict[str, Any] = {}
            if body.schedule is not None:
                check_schedule(data, body.schedule, "Extract")
                updates["schedule"] = body.schedule
            if body.refresh is not None:
                updates["refresh"] = body.refresh
            if "priority" in body.model_fields_set:
                updates["priority"] = body.priority
            if body.suspended is not None:
                updates["suspended"] = body.suspended
                if not body.suspended:
                    updates["consecutive_failures"] = 0  # 與 Tableau 相同：繼續時重新計算連續失敗
            data.extract_refreshes[data.extract_refreshes.index(task)] = task.model_copy(
                update=updates
            )

        fields = sorted(body.model_dump(exclude_unset=True))
        data = change(user, "task.update", task_id, apply, {"fields": fields})
        task = next(t for t in data.extract_refreshes if t.id == task_id)
        return task_json(task, data, jobs(data), schedules.last_runs(), dt.datetime.now(dt.UTC))

    @router.delete("/api/admin/extract-tasks/{task_id}", status_code=204)
    def delete_task(task_id: str, request: Request) -> None:
        user = actor(request)
        current = next((t for t in schedules.data().extract_refreshes if t.id == task_id), None)
        if current is None:
            raise HTTPException(404, "找不到萃取更新工作")
        task_owner_or_admin(user, current, schedules.data())

        def apply(data: ScheduleFile) -> None:
            data.extract_refreshes = [t for t in data.extract_refreshes if t.id != task_id]

        change(user, "task.delete", task_id, apply)

    @router.post("/api/admin/jobs/{job_id}/run", status_code=202)
    def run_now(job_id: str, request: Request) -> dict[str, Any]:
        user = actor(request)
        data = schedules.data()
        job = jobs(data).get(job_id)
        if job is None:
            raise HTTPException(404, f"找不到工作 {job_id}")
        if not is_admin(user):
            reports = {t.report.name for t in job.targets}
            if job.kind != "extract" or not reports or not all(owns(user, r) for r in reports):
                raise HTTPException(403, "只有站台管理員與報表的擁有者可以立即執行")
        if job.problem is not None and not job.suspended:
            raise _bad(f"{job.label()} 不能執行：{job.problem}")
        request_id = schedules.request_run(job_id, user.username)
        SiteStore(schedules.root).record(user.username, "task.run", job_id, {})
        return {"queued": request_id, "scheduler": schedules.status()}

    @router.get("/api/admin/runs")
    def runs(request: Request, job: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        admin(request)
        return schedules.runs(max(1, min(limit, 2000)), job)

    # ------------------------------------------------------------ 訂閱

    def subscription_schedules(data: ScheduleFile) -> list[dict[str, Any]]:
        now = dt.datetime.now(dt.UTC)
        return [
            schedule_json(s, data, now)
            for s in data.schedules
            if s.kind in (None, "Subscription") and s.state == "Active" and s.frequency is not None
        ]

    def site_user(name: str) -> Any:
        login = name.split("\\", 1)
        user = site.data().user(login[-1], login[0] if len(login) == 2 else None)
        if user is None:
            raise _bad(f"「{name}」不是站台的使用者")
        return user

    def may_subscribe_others(user: Identity, report_id: str) -> bool:
        return is_admin(user) or owns(user, report_id)

    @router.get("/api/subscription-options")
    def options(report: str, request: Request) -> dict[str, Any]:
        user = actor(request)
        grant = access(report, user)
        if grant is None or not grant.allowed:
            raise HTTPException(404, f"找不到報表「{report}」")
        data = schedules.data()
        others = may_subscribe_others(user, report)
        me = site.data().user(user.username, user.domain) or site.data().user(user.username)
        return {
            "username": user.username,
            "schedules": subscription_schedules(data),
            "timezone": data.timezone,
            "email": me.email if me is not None else None,
            "can_subscribe_others": others,
            "users": [u.name for u in site.data().users if u.email] if others else [],
        }

    def mine(user: Identity, sub: Subscription) -> bool:
        return sub.user.casefold() == user.username.casefold()

    @router.get("/api/subscriptions")
    def my_subscriptions(request: Request) -> dict[str, Any]:
        user = actor(request)
        data = schedules.data()
        planned = jobs(data)
        last = schedules.last_runs()
        now = dt.datetime.now(dt.UTC)
        return {
            "timezone": data.timezone,
            "schedules": subscription_schedules(data),
            "subscriptions": [
                subscription_json(s, data, planned, last, now)
                for s in data.subscriptions
                if mine(user, s)
            ],
        }

    def view_allowed(grant: Grant, report_id: str, view: str | None) -> bool:
        report = catalog().report(report_id)
        if report is None:
            return False
        ir = report.ir
        if view is None:
            return True
        board = next((d for d in ir.dashboards if d.name == view), None)
        if board is not None:
            return grant.dashboard_allowed(board.name)
        sheet = next((w for w in ir.worksheets if w.name == view), None)
        return sheet is not None and grant.worksheet_allowed(ir, sheet.id)

    @router.post("/api/subscriptions", status_code=201)
    def subscribe(body: SubscriptionCreate, request: Request) -> dict[str, Any]:
        user = actor(request)
        report = catalog().report(body.report)
        grant = access(body.report, user)
        if report is None or grant is None or not grant.allowed:
            raise HTTPException(404, f"找不到報表「{body.report}」")
        if body.view is not None and not any(
            v == body.view
            for v in [d.name for d in report.ir.dashboards] + [w.name for w in report.ir.worksheets]
        ):
            raise _bad(f"報表中沒有 view「{body.view}」")
        if not body.attach_image and not body.attach_pdf:
            raise _bad("訂閱要附圖片或 PDF（或兩者）")
        if body.page_size is not None and body.page_size not in PAPER_SIZES:
            raise _bad("紙張大小不正確")
        names = body.users or [user.username]
        if any(
            n.casefold() != user.username.casefold() for n in names
        ) and not may_subscribe_others(user, body.report):
            raise HTTPException(403, "只有報表的擁有者、Project Leader 與站台管理員可以替別人訂閱")
        subscribers = []
        for name in dict.fromkeys(names):
            target = site_user(name)
            if not target.email:
                raise _bad(f"{target.name} 在站台中沒有 email，無法訂閱")
            target_grant = access(body.report, target.identity())
            if (
                target_grant is None
                or not target_grant.allowed
                or not view_allowed(target_grant, body.report, body.view)
            ):
                raise _bad(f"{target.name} 沒有檢視這個 view 的權限")
            subscribers.append(target)
        created: list[str] = []

        def apply(data: ScheduleFile) -> None:
            check_schedule(data, body.schedule, "Subscription")
            for target in subscribers:
                sub_id = f"u{secrets.token_hex(6)}"
                created.append(sub_id)
                data.subscriptions.append(
                    Subscription(
                        id=sub_id,
                        subject=body.subject.strip() or (body.view or report.label),
                        message=body.message or None,
                        user=target.name,
                        user_domain=target.domain,
                        schedule=body.schedule,
                        content_kind="view" if body.view is not None else "workbook",
                        report=body.report,
                        workbook_name=report.label,
                        view=body.view,
                        attach_image=body.attach_image,
                        attach_pdf=body.attach_pdf,
                        page_size=body.page_size,
                        page_orientation=body.page_orientation,
                        send_if_view_empty=body.send_if_view_empty,
                    )
                )

        data = change(
            user,
            "subscription.create",
            body.report,
            apply,
            {"users": [s.name for s in subscribers]},
        )
        planned = jobs(data)
        now = dt.datetime.now(dt.UTC)
        return {
            "subscriptions": [
                subscription_json(s, data, planned, {}, now)
                for s in data.subscriptions
                if s.id in created
            ]
        }

    def find_subscription(sub_id: str) -> Subscription:
        sub = next((s for s in schedules.data().subscriptions if s.id == sub_id), None)
        if sub is None:
            raise HTTPException(404, "找不到訂閱")
        return sub

    @router.patch("/api/subscriptions/{sub_id}")
    def update_subscription(
        sub_id: str, body: SubscriptionPatch, request: Request
    ) -> dict[str, Any]:
        user = actor(request)
        current = find_subscription(sub_id)
        if not is_admin(user) and not mine(user, current):
            raise HTTPException(403, "只有訂閱者本人與站台管理員可以修改訂閱")
        if body.page_size is not None and body.page_size not in PAPER_SIZES:
            raise _bad("紙張大小不正確")

        def apply(data: ScheduleFile) -> None:
            sub = next((s for s in data.subscriptions if s.id == sub_id), None)
            if sub is None:
                raise ScheduleError("找不到訂閱")
            updates = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
            if body.schedule is not None:
                check_schedule(data, body.schedule, "Subscription")
            elif "schedule" in updates:
                del updates["schedule"]
            if body.suspended is False:
                updates["consecutive_failures"] = 0
            merged = sub.model_copy(update=updates)
            if not merged.attach_image and not merged.attach_pdf:
                raise ScheduleError("訂閱要附圖片或 PDF（或兩者）")
            data.subscriptions[data.subscriptions.index(sub)] = merged

        fields = sorted(body.model_dump(exclude_unset=True))
        data = change(user, "subscription.update", sub_id, apply, {"fields": fields})
        sub = next(s for s in data.subscriptions if s.id == sub_id)
        return subscription_json(
            sub, data, jobs(data), schedules.last_runs(), dt.datetime.now(dt.UTC)
        )

    @router.delete("/api/subscriptions/{sub_id}", status_code=204)
    def unsubscribe(sub_id: str, request: Request) -> None:
        user = actor(request)
        current = find_subscription(sub_id)
        report_id = current.report
        if not (is_admin(user) or mine(user, current) or (report_id and owns(user, report_id))):
            raise HTTPException(403, "只有訂閱者本人、報表的擁有者與站台管理員可以刪除訂閱")

        def apply(data: ScheduleFile) -> None:
            data.subscriptions = [s for s in data.subscriptions if s.id != sub_id]

        change(user, "subscription.delete", sub_id, apply, {"user": current.user})

    return router


__all__ = ["schedule_router"]
