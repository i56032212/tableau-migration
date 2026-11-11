"""檢視者自己的資料（與 Tableau Server 相同）：我的最愛、自訂檢視（含各人的預設檢視）與留言。

    <報表庫>/viewer/
    ├── favorites.json      每個人的我的最愛：報表或其中一個 view
    ├── custom-views.json   自訂檢視：名稱、報表、view、擁有者、公開與否、檢視器的狀態（篩選、參數、選取…）；
    │                       各人在每個 view 的預設檢視
    └── comments.json       每個 view 的留言

修改以檔案鎖排隊（多個查詢服務的行程可以同時修改）；報表刪除後它的資料保留，不影響其他報表。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig_service.schedules import atomic_replace, file_lock

STATE_LIMIT = 256 * 1024
"""自訂檢視的狀態最大的大小（JSON 位元組）。"""
COMMENT_LIMIT = 4000


class ViewerError(ValueError):
    pass


def now_text() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class Favorite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str
    report: str
    view: str | None = Field(
        default=None, description="view 的 id（dashboard、工作表或 Story）；None 為整份報表"
    )
    added_at: str


class CustomView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    report: str
    view: str = Field(description="view 的 id（dashboard、工作表或 Story）")
    name: str = Field(min_length=1, max_length=255)
    owner: str
    public: bool = False
    state: dict[str, Any] = Field(default_factory=dict, description="檢視器的狀態（ViewState）")
    created_at: str
    updated_at: str


class DefaultView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str
    report: str
    view: str
    custom_view: str


class Comment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    report: str
    view: str
    user: str
    text: str = Field(min_length=1, max_length=COMMENT_LIMIT)
    created_at: str


class Favorites(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Favorite] = Field(default_factory=list)


class CustomViews(BaseModel):
    model_config = ConfigDict(extra="forbid")

    views: list[CustomView] = Field(default_factory=list)
    defaults: list[DefaultView] = Field(default_factory=list)


class Comments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Comment] = Field(default_factory=list)


Model = TypeVar("Model", bound=BaseModel)


def same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


class ViewerStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "viewer"
        self._cache: dict[str, tuple[tuple[int, int], BaseModel]] = {}

    def _path(self, name: str) -> Path:
        return self.root / name

    def _read(self, name: str, model: type[Model]) -> Model:
        path = self._path(name)
        try:
            info = path.stat()
        except OSError:
            return model()
        stamp = (info.st_mtime_ns, info.st_size)
        cached = self._cache.get(name)
        if cached is not None and cached[0] == stamp and isinstance(cached[1], model):
            return cached[1]
        try:
            data = model.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise ViewerError(f"{path} 不正確：{exc}") from exc
        self._cache[name] = (stamp, data)
        return data

    def _change(self, name: str, model: type[Model], apply: Callable[[Model], Any]) -> Any:
        with file_lock(self.root / f"{name}.lock"):
            self._cache.pop(name, None)
            data = self._read(name, model).model_copy(deep=True)
            result = apply(data)
            data = model.model_validate(data.model_dump())
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(name)
            temporary = path.with_name(f"{name}.{os.getpid()}.tmp")
            temporary.write_text(data.model_dump_json(indent=2) + "\n", encoding="utf-8")
            atomic_replace(temporary, path)
            self._cache.pop(name, None)
            return result

    # ------------------------------------------------------------ 我的最愛

    def favorites(self, user: str) -> list[Favorite]:
        return [f for f in self._read("favorites.json", Favorites).items if same(f.user, user)]

    def set_favorite(self, user: str, report: str, view: str | None, on: bool) -> list[Favorite]:
        def apply(data: Favorites) -> None:
            data.items = [
                f
                for f in data.items
                if not (same(f.user, user) and f.report == report and f.view == view)
            ]
            if on:
                data.items.append(
                    Favorite(user=user, report=report, view=view, added_at=now_text())
                )

        self._change("favorites.json", Favorites, apply)
        return self.favorites(user)

    # ------------------------------------------------------------ 自訂檢視

    def custom_views(self, report: str, view: str | None = None) -> list[CustomView]:
        data = self._read("custom-views.json", CustomViews)
        return [v for v in data.views if v.report == report and (view is None or v.view == view)]

    def custom_view(self, view_id: str) -> CustomView | None:
        return next(
            (v for v in self._read("custom-views.json", CustomViews).views if v.id == view_id), None
        )

    def default_view(self, user: str, report: str, view: str) -> str | None:
        data = self._read("custom-views.json", CustomViews)
        found = next(
            (
                d
                for d in data.defaults
                if same(d.user, user) and d.report == report and d.view == view
            ),
            None,
        )
        return found.custom_view if found is not None else None

    def save_view(
        self,
        user: str,
        report: str,
        view: str,
        name: str,
        state: dict[str, Any],
        public: bool,
        make_default: bool,
    ) -> CustomView:
        if len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > STATE_LIMIT:
            raise ViewerError("自訂檢視的狀態太大")
        stamp = now_text()

        def apply(data: CustomViews) -> CustomView:
            # 與 Tableau 相同：同一個人在同一個 view 存同名的自訂檢視時取代
            existing = next(
                (
                    v
                    for v in data.views
                    if v.report == report
                    and v.view == view
                    and same(v.owner, user)
                    and same(v.name, name)
                ),
                None,
            )
            if existing is not None:
                saved = existing.model_copy(
                    update={"state": state, "public": public, "updated_at": stamp}
                )
                data.views[data.views.index(existing)] = saved
            else:
                saved = CustomView(
                    id=f"cv{secrets.token_hex(6)}",
                    report=report,
                    view=view,
                    name=name.strip(),
                    owner=user,
                    public=public,
                    state=state,
                    created_at=stamp,
                    updated_at=stamp,
                )
                data.views.append(saved)
            if make_default:
                _set_default(data, user, report, view, saved.id)
            return saved

        result: CustomView = self._change("custom-views.json", CustomViews, apply)
        return result

    def update_view(self, view_id: str, changes: dict[str, Any]) -> CustomView:
        def apply(data: CustomViews) -> CustomView:
            found = next((v for v in data.views if v.id == view_id), None)
            if found is None:
                raise ViewerError("找不到自訂檢視")
            updated = found.model_copy(update={**changes, "updated_at": now_text()})
            data.views[data.views.index(found)] = updated
            return updated

        result: CustomView = self._change("custom-views.json", CustomViews, apply)
        return result

    def delete_view(self, view_id: str) -> None:
        def apply(data: CustomViews) -> None:
            data.views = [v for v in data.views if v.id != view_id]
            data.defaults = [d for d in data.defaults if d.custom_view != view_id]

        self._change("custom-views.json", CustomViews, apply)

    def set_default(self, user: str, report: str, view: str, custom_view: str | None) -> None:
        def apply(data: CustomViews) -> None:
            _set_default(data, user, report, view, custom_view)

        self._change("custom-views.json", CustomViews, apply)

    # ------------------------------------------------------------ 留言

    def comments(self, report: str, view: str) -> list[Comment]:
        data = self._read("comments.json", Comments)
        return [c for c in data.items if c.report == report and c.view == view]

    def add_comment(self, user: str, report: str, view: str, text: str) -> Comment:
        comment = Comment(
            id=f"c{secrets.token_hex(6)}",
            report=report,
            view=view,
            user=user,
            text=text.strip(),
            created_at=now_text(),
        )

        def apply(data: Comments) -> None:
            data.items.append(comment)

        self._change("comments.json", Comments, apply)
        return comment

    def comment(self, comment_id: str) -> Comment | None:
        return next(
            (c for c in self._read("comments.json", Comments).items if c.id == comment_id), None
        )

    def delete_comment(self, comment_id: str) -> None:
        def apply(data: Comments) -> None:
            data.items = [c for c in data.items if c.id != comment_id]

        self._change("comments.json", Comments, apply)


def _set_default(
    data: CustomViews, user: str, report: str, view: str, custom_view: str | None
) -> None:
    data.defaults = [
        d
        for d in data.defaults
        if not (same(d.user, user) and d.report == report and d.view == view)
    ]
    if custom_view is not None:
        data.defaults.append(
            DefaultView(user=user, report=report, view=view, custom_view=custom_view)
        )


__all__ = [
    "Comment",
    "CustomView",
    "Favorite",
    "ViewerError",
    "ViewerStore",
]
