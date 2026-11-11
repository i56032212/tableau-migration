"""檢視者的我的最愛、自訂檢視與留言的 API（有報表庫時；tmig_service.viewer_store）。

- ``GET  /api/favorites``、``PUT /api/favorites``：我的最愛（報表或 view）
- ``GET  /api/workbooks/{wb}/views/{view}/custom-views``：看得到的自訂檢視（自己的與公開的）與我的預設檢視
- ``POST /api/workbooks/{wb}/views/{view}/custom-views``：儲存自訂檢視（公開要「分享自訂檢視」的權限）
- ``PATCH|DELETE /api/custom-views/{id}``：擁有者與站台管理員
- ``PUT  /api/workbooks/{wb}/views/{view}/default-view``：我的預設檢視（開啟 view 時套用）
- ``GET|POST /api/workbooks/{wb}/views/{view}/comments``：留言（「檢視留言」「新增留言」的權限）
- ``DELETE /api/comments/{id}``：留言的人、報表的擁有者與站台管理員
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
from tmig_service.viewer_store import COMMENT_LIMIT, CustomView, ViewerError, ViewerStore, same


class FavoriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: str = Field(min_length=1)
    view: str | None = None
    on: bool = True


class CustomViewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    state: dict[str, Any] = Field(default_factory=dict)
    public: bool = False
    make_default: bool = False


class CustomViewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    public: bool | None = None


class DefaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_view: str | None = None


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=COMMENT_LIMIT)


def viewer_router(
    store: ViewerStore,
    current_user: Callable[[Request], Identity | None],
    granted: Callable[[str, Identity | None], tuple[IR, Grant, str | None]],
) -> APIRouter:
    """granted：報表、登入者的權限與報表的擁有者；沒有權限時與報表不存在相同，回應 404。"""
    router = APIRouter()

    def actor(request: Request) -> Identity:
        user = current_user(request)
        if user is None:
            raise HTTPException(403, "需要登入")
        return user

    def is_admin(user: Identity) -> bool:
        return (user.site_role or "") in ADMIN_ROLES

    def view_of(ir: IR, grant: Grant, view: str) -> str:
        """view 的名稱（dashboard、工作表或 Story）；沒有權限時 404。"""
        board = next((d for d in ir.dashboards if d.id == view), None)
        if board is not None and (
            grant.dashboard_allowed(board.name) or board.id in grant.story_dashboards(ir)
        ):
            return board.name
        sheet = next((w for w in ir.worksheets if w.id == view), None)
        if sheet is not None and grant.worksheet_allowed(ir, sheet.id):
            return sheet.name
        story = next((s for s in ir.stories if s.id == view), None)
        if story is not None and grant.story_allowed(story.name):
            return story.name
        raise HTTPException(404, f"找不到 view「{view}」")

    def check(
        request: Request, wb: str, view: str | None = None
    ) -> tuple[Identity, IR, Grant, str | None]:
        user = actor(request)
        ir, grant, owner = granted(wb, user)
        if view is not None:
            view_of(ir, grant, view)
        return user, ir, grant, owner

    # ------------------------------------------------------------ 我的最愛

    @router.get("/api/favorites")
    def favorites(request: Request) -> list[dict[str, Any]]:
        user = actor(request)
        result: list[dict[str, Any]] = []
        for item in store.favorites(user.username):
            try:
                ir, grant, _ = granted(item.report, user)
            except HTTPException:
                continue  # 報表刪除了或沒有權限了：不列出
            name = None
            if item.view is not None:
                try:
                    name = view_of(ir, grant, item.view)
                except HTTPException:
                    continue
            result.append({**item.model_dump(), "report_name": ir.workbook.name, "view_name": name})
        return result

    @router.put("/api/favorites")
    def set_favorite(body: FavoriteBody, request: Request) -> list[dict[str, Any]]:
        user, _, _, _ = check(request, body.report, body.view)
        store.set_favorite(user.username, body.report, body.view, body.on)
        return favorites(request)

    # ------------------------------------------------------------ 自訂檢視

    def visible(user: Identity, item: CustomView) -> bool:
        return item.public or same(item.owner, user.username) or is_admin(user)

    def view_json(user: Identity, item: CustomView) -> dict[str, Any]:
        return {**item.model_dump(), "mine": same(item.owner, user.username)}

    @router.get("/api/workbooks/{wb}/views/{view}/custom-views")
    def custom_views(wb: str, view: str, request: Request) -> dict[str, Any]:
        user, _, grant, _ = check(request, wb, view)
        items = [v for v in store.custom_views(wb, view) if visible(user, v)]
        default = store.default_view(user.username, wb, view)
        if default is not None and not any(v.id == default for v in items):
            default = None
        return {
            "views": [view_json(user, v) for v in items],
            "default": default,
            "can_share": grant.can("ShareView"),
        }

    @router.post("/api/workbooks/{wb}/views/{view}/custom-views", status_code=201)
    def save_view(wb: str, view: str, body: CustomViewCreate, request: Request) -> dict[str, Any]:
        user, _, grant, _ = check(request, wb, view)
        if body.public and not grant.can("ShareView"):
            raise HTTPException(403, "沒有公開自訂檢視的權限（分享自訂檢視）")
        try:
            saved = store.save_view(
                user.username, wb, view, body.name, body.state, body.public, body.make_default
            )
        except ViewerError as exc:
            raise HTTPException(422, str(exc)) from exc
        return view_json(user, saved)

    def own_view(request: Request, view_id: str) -> tuple[Identity, CustomView, Grant]:
        user = actor(request)
        item = store.custom_view(view_id)
        if item is None:
            raise HTTPException(404, "找不到自訂檢視")
        _, grant, _ = granted(item.report, user)
        if not (same(item.owner, user.username) or is_admin(user)):
            raise HTTPException(403, "只有自訂檢視的擁有者與站台管理員可以修改")
        return user, item, grant

    @router.patch("/api/custom-views/{view_id}")
    def update_view(view_id: str, body: CustomViewPatch, request: Request) -> dict[str, Any]:
        user, item, grant = own_view(request, view_id)
        if body.public and not item.public and not grant.can("ShareView"):
            raise HTTPException(403, "沒有公開自訂檢視的權限（分享自訂檢視）")
        changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        return view_json(user, store.update_view(view_id, changes))

    @router.delete("/api/custom-views/{view_id}", status_code=204)
    def delete_view(view_id: str, request: Request) -> None:
        own_view(request, view_id)
        store.delete_view(view_id)

    @router.put("/api/workbooks/{wb}/views/{view}/default-view")
    def set_default(wb: str, view: str, body: DefaultBody, request: Request) -> dict[str, Any]:
        user, _, _, _ = check(request, wb, view)
        if body.custom_view is not None:
            item = store.custom_view(body.custom_view)
            if item is None or item.report != wb or item.view != view or not visible(user, item):
                raise HTTPException(404, "找不到自訂檢視")
        store.set_default(user.username, wb, view, body.custom_view)
        return {"default": body.custom_view}

    # ------------------------------------------------------------ 留言

    @router.get("/api/workbooks/{wb}/views/{view}/comments")
    def comments(wb: str, view: str, request: Request) -> dict[str, Any]:
        user, _, grant, owner = check(request, wb, view)
        if not grant.can("ViewComments"):
            raise HTTPException(403, "沒有檢視留言的權限")
        manage = is_admin(user) or (owner is not None and same(owner, user.username))
        return {
            "can_add": grant.can("AddComment"),
            "comments": [
                {**c.model_dump(), "can_delete": manage or same(c.user, user.username)}
                for c in store.comments(wb, view)
            ],
        }

    @router.post("/api/workbooks/{wb}/views/{view}/comments", status_code=201)
    def add_comment(wb: str, view: str, body: CommentBody, request: Request) -> dict[str, Any]:
        user, _, grant, _ = check(request, wb, view)
        if not grant.can("AddComment"):
            raise HTTPException(403, "沒有新增留言的權限")
        if not body.text.strip():
            raise HTTPException(422, "留言不能是空白")
        return {
            **store.add_comment(user.username, wb, view, body.text).model_dump(),
            "can_delete": True,
        }

    @router.delete("/api/comments/{comment_id}", status_code=204)
    def delete_comment(comment_id: str, request: Request) -> None:
        user = actor(request)
        item = store.comment(comment_id)
        if item is None:
            raise HTTPException(404, "找不到留言")
        _, _, owner = granted(item.report, user)
        if not (
            same(item.user, user.username)
            or is_admin(user)
            or (owner is not None and same(owner, user.username))
        ):
            raise HTTPException(403, "只有留言的人、報表的擁有者與站台管理員可以刪除留言")
        store.delete_comment(comment_id)

    return router


__all__ = ["viewer_router"]
