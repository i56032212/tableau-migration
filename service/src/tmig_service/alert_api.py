"""資料警示的 API（tmig_service.alerts）。

- ``POST   /api/alert-options``：工作表中可以設定警示的量值與目前的範圍、能不能加入其他收件人
- ``GET    /api/alerts``：我建立的與我收到的警示
- ``POST   /api/alerts``：建立警示（要能檢視報表；收件人要能檢視報表並在站台中有 email；
  報表的擁有者、Project Leader 與站台管理員可以加入其他收件人）
- ``PATCH|DELETE /api/alerts/{id}``：建立的人與站台管理員
- ``POST   /api/alerts/{id}/leave``：收件人不再收到
- ``POST   /api/alerts/{id}/check``：立即檢查（排入排程器的佇列）
- ``GET    /api/admin/alerts``：站台管理員看全部的警示
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tmig.access.permissions import ADMIN_ROLES
from tmig.ir import IR
from tmig.planner.identity import Identity
from tmig_service.access import Grant
from tmig_service.alerts import (
    CONDITIONS,
    FREQUENCIES,
    AlertError,
    AlertStore,
    Condition,
    DataAlert,
    Frequency,
    alert_json,
)
from tmig_service.schedules import ScheduleStore
from tmig_service.site import SiteStore


class OptionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: str = Field(min_length=1)
    worksheet: str = Field(min_length=1)
    state: dict[str, Any] = Field(default_factory=dict)


class AlertCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: str = Field(min_length=1)
    view: str = Field(min_length=1)
    worksheet: str = Field(min_length=1)
    measure: str = Field(min_length=1)
    condition: Condition
    threshold: float
    frequency: Frequency = "data"
    subject: str = Field(default="", max_length=255)
    recipients: list[str] | None = None
    state: dict[str, Any] = Field(default_factory=dict)


class AlertPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=255)
    condition: Condition | None = None
    threshold: float | None = None
    frequency: Frequency | None = None
    recipients: list[str] | None = None
    suspended: bool | None = None


Measures = Callable[[str, str, dict[str, Any], Identity], list[dict[str, Any]]]
"""報表、工作表、狀態與登入者 → 可以設定警示的量值：key、name、min、max。"""


def alert_router(
    alerts: AlertStore,
    schedules: ScheduleStore,
    site: SiteStore,
    current_user: Callable[[Request], Identity | None],
    granted: Callable[[str, Identity | None], tuple[IR, Grant, str | None]],
    measures: Measures,
    owns: Callable[[Identity, str], bool],
) -> APIRouter:
    router = APIRouter()

    def actor(request: Request) -> Identity:
        user = current_user(request)
        if user is None:
            raise HTTPException(403, "需要登入")
        return user

    def is_admin(user: Identity) -> bool:
        return (user.site_role or "") in ADMIN_ROLES

    def same(a: str, b: str) -> bool:
        return a.casefold() == b.casefold()

    def sheet_allowed(ir: IR, grant: Grant, worksheet: str) -> None:
        if not any(w.id == worksheet for w in ir.worksheets) or not grant.worksheet_allowed(
            ir, worksheet
        ):
            raise HTTPException(404, f"找不到工作表「{worksheet}」")

    def options_for(
        user: Identity, report: str, worksheet: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        ir, grant, _ = granted(report, user)
        sheet_allowed(ir, grant, worksheet)
        others = is_admin(user) or owns(user, report)
        me = site.data().user(user.username, user.domain) or site.data().user(user.username)
        return {
            "username": user.username,
            "measures": measures(report, worksheet, state, user),
            "conditions": CONDITIONS,
            "frequencies": FREQUENCIES,
            "email": me.email if me is not None else None,
            "can_add_others": others,
            "users": [u.name for u in site.data().users if u.email] if others else [],
        }

    @router.post("/api/alert-options")
    def options(body: OptionsBody, request: Request) -> dict[str, Any]:
        return options_for(actor(request), body.report, body.worksheet, body.state)

    def involved(user: Identity, alert: DataAlert) -> bool:
        return same(alert.owner, user.username) or any(
            same(r, user.username) for r in alert.recipients
        )

    @router.get("/api/alerts")
    def mine(request: Request) -> dict[str, Any]:
        user = actor(request)
        return {
            "alerts": [
                alert_json(a, user.username) for a in alerts.data().alerts if involved(user, a)
            ],
        }

    @router.get("/api/admin/alerts")
    def all_alerts(request: Request) -> dict[str, Any]:
        user = actor(request)
        if not is_admin(user):
            raise HTTPException(403, "只有站台管理員可以看全部的資料警示")
        last = schedules.last_runs()
        return {
            "alerts": [
                {**alert_json(a, user.username), "last_run": last.get(f"alert:{a.id}")}
                for a in alerts.data().alerts
            ]
        }

    def check_recipients(
        user: Identity, report: str, worksheet: str, names: list[str]
    ) -> list[str]:
        """收件人：站台的使用者、有 email、能檢視這張工作表。"""
        result: list[str] = []
        for name in dict.fromkeys(names):
            member = site.data().user(name)
            if member is None:
                raise HTTPException(422, f"「{name}」不是站台的使用者")
            if not member.email:
                raise HTTPException(422, f"{member.name} 在站台中沒有 email，無法收到警示")
            try:
                ir, grant, _ = granted(report, member.identity())
                sheet_allowed(ir, grant, worksheet)
            except HTTPException:
                raise HTTPException(422, f"{member.name} 沒有檢視這張工作表的權限") from None
            result.append(member.name)
        return result

    @router.post("/api/alerts", status_code=201)
    def create(body: AlertCreate, request: Request) -> dict[str, Any]:
        user = actor(request)
        found = options_for(user, body.report, body.worksheet, body.state)
        measure = next((m for m in found["measures"] if m["key"] == body.measure), None)
        if measure is None:
            raise HTTPException(422, "工作表中沒有這個量值")
        ir, _, _ = granted(body.report, user)
        if not any(v.id == body.view for v in [*ir.dashboards, *ir.worksheets]):
            raise HTTPException(422, "報表中沒有這個 view")
        names = body.recipients if body.recipients is not None else [user.username]
        others = [n for n in names if not same(n, user.username)]
        if others and not found["can_add_others"]:
            raise HTTPException(
                403, "只有報表的擁有者、Project Leader 與站台管理員可以加入其他收件人"
            )
        recipients = check_recipients(user, body.report, body.worksheet, names) if names else []
        if not recipients:
            raise HTTPException(422, "警示至少要有一位收件人")
        owner_member = site.data().user(user.username)
        owner = owner_member.name if owner_member is not None else user.username
        created = alerts.create(
            {
                "subject": body.subject.strip(),
                "owner": owner,
                "recipients": recipients,
                "report": body.report,
                "view": body.view,
                "worksheet": body.worksheet,
                "measure": body.measure,
                "measure_name": measure["name"],
                "condition": body.condition,
                "threshold": body.threshold,
                "frequency": body.frequency,
                "state": body.state,
            }
        )
        SiteStore(schedules.root).record(
            user.username, "alert.create", created.id, {"report": body.report}
        )
        return alert_json(created, user.username)

    def own(request: Request, alert_id: str) -> tuple[Identity, DataAlert]:
        user = actor(request)
        alert = alerts.get(alert_id)
        if alert is None:
            raise HTTPException(404, "找不到資料警示")
        if not (same(alert.owner, user.username) or is_admin(user)):
            raise HTTPException(403, "只有建立的人與站台管理員可以修改資料警示")
        return user, alert

    @router.patch("/api/alerts/{alert_id}")
    def update(alert_id: str, body: AlertPatch, request: Request) -> dict[str, Any]:
        user, alert = own(request, alert_id)
        changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        if body.recipients is not None:
            if not body.recipients:
                raise HTTPException(422, "警示至少要有一位收件人")
            others = [n for n in body.recipients if not same(n, alert.owner)]
            if others and not (is_admin(user) or owns(user, alert.report)):
                raise HTTPException(
                    403, "只有報表的擁有者、Project Leader 與站台管理員可以加入其他收件人"
                )
            changes["recipients"] = check_recipients(
                user, alert.report, alert.worksheet, body.recipients
            )
        if body.suspended is False:
            changes["consecutive_failures"] = 0
        if "subject" in changes:
            changes["subject"] = str(changes["subject"]).strip()
        try:
            updated = alerts.update(alert_id, changes)
        except AlertError as exc:
            raise HTTPException(422, str(exc)) from exc
        SiteStore(schedules.root).record(
            user.username, "alert.update", alert_id, {"fields": sorted(changes)}
        )
        return alert_json(updated, user.username)

    @router.delete("/api/alerts/{alert_id}", status_code=204)
    def delete(alert_id: str, request: Request) -> None:
        user, _ = own(request, alert_id)
        alerts.delete(alert_id)
        SiteStore(schedules.root).record(user.username, "alert.delete", alert_id, {})

    @router.post("/api/alerts/{alert_id}/leave")
    def leave(alert_id: str, request: Request) -> dict[str, Any]:
        user = actor(request)
        alert = alerts.get(alert_id)
        if alert is None or not any(same(r, user.username) for r in alert.recipients):
            raise HTTPException(404, "找不到資料警示")
        updated = alerts.update(
            alert_id, {"recipients": [r for r in alert.recipients if not same(r, user.username)]}
        )
        return alert_json(updated, user.username)

    @router.post("/api/alerts/{alert_id}/check", status_code=202)
    def check_now(alert_id: str, request: Request) -> dict[str, Any]:
        user, _ = own(request, alert_id)
        request_id = schedules.request_run(f"alert:{alert_id}", user.username)
        return {"queued": request_id, "scheduler": schedules.status()}

    return router


__all__ = ["alert_router"]
