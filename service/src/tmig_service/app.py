"""查詢服務（F2）：FastAPI＋DuckDB，只接受篩選與參數狀態，從不接受 SQL。

    uv run tmig serve                  # 預設讀 fixtures/，http://127.0.0.1:8000

API：

- ``GET  /api/workbooks``：fixtures 中的報表
- ``GET  /api/workbooks/{wb}/ir``：IR
- ``POST /api/workbooks/{wb}/worksheets/{ws}/render``：依篩選與參數狀態查詢，回傳 Vega-Lite 規格（資料內嵌）與結果列數
- ``POST /api/workbooks/{wb}/fields/{field}/domain``：篩選器的可選值，或最小值與最大值
- ``GET  /api/workbooks/{wb}/cases``：cases.yaml 的篩選組合（已轉成篩選狀態），供檢視器切換
- ``GET  /api/workbooks/{wb}/files/{path}``：報表的圖片（圖片區塊與圖片按鈕；.twbx 內或報表套件的 files/）
- ``GET  /api/me``：目前的登入者與身分驗證模式；``/auth/*``：登入、登出與開發模式的切換使用者
- ``POST /api/workbooks/{wb}/views/{view}/visit``：檢視器開啟一個 dashboard、工作表或 Story（使用紀錄，R1）
- ``GET  /api/usage?days=90``：每份報表與 view 的檢視次數、使用者數與最後一次檢視（有權限檔時限管理員）
- 報表庫（D0，``tmig serve --store``）：發布與版本（見 tmig_service.publishing）、桌面版登入
  （見 tmig_service.desktop_auth）；發布的報表與 fixtures 的報表一起列在 /api/workbooks

其餘路徑提供檢視器（web/dist）。

身分驗證（P2，見 tmig_service.auth）：設定之後報表的 API 都需要登入者，查詢以登入者的身分套用列層級權限；
篩選器的可選值只提供報表上有篩選控制項的欄位。權限（P3，見 tmig_service.access）：設定權限檔之後，
報表、view 與報表定義依 Tableau Server 匯出的權限開放。

萃取的資料源查詢報表目錄下的 DuckDB 檔；Live 連線依連線設定檔（環境變數 TMIG_CONNECTIONS，
預設為目前目錄的 connections.toml）連到來源資料庫，以兩段式執行（第三階段 N3、N4）。
"""

from __future__ import annotations

import datetime as dt
import gzip
import logging
import math
import mimetypes
import os
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from tmig.access.permissions import ADMIN_ROLES, decide
from tmig.catalog import Catalog, Report, workbook_url_name
from tmig.data.executor import ExecutionError, format_value
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError
from tmig.ir import IR, LiveConnection, Worksheet, iter_zones, save_ir
from tmig.package import read_report_file
from tmig.planner.drill import apply_drill, drill_info
from tmig.planner.dynamic import dynamic_fields, resolve_parameters
from tmig.planner.identity import Identity, protected_fields
from tmig.planner.state import QueryState, StateError, parse_key, with_viewport
from tmig.render.aliases import alias_key, member_aliases
from tmig.render.datasource import datasource_summary
from tmig.render.format import fiscal_label
from tmig.render.format import format_value as display_value
from tmig.render.missing import COLUMN_ORDER, pad_empty, pad_result
from tmig.render.vegalite import (
    after_groups,
    build_spec,
    caption,
    fiscal_starts,
    title_values,
    value_format,
    worksheet_caption,
    worksheet_title,
)
from tmig.sqlgen.dialect import underlying_query
from tmig.sqlgen.query import (
    PREVIEW_LIMIT,
    QueryError,
    custom_sql_parameters,
    domain_sql,
    empty_headers_sql,
    pane_sql,
    percentages,
    preview_sql,
    relevant_domain_sql,
    title_sql,
    totals_sql,
    visual_totals,
    worksheet_sql,
)
from tmig.verify.cases import CasesError, case_state, load_cases
from tmig.verify.run import ensure_data, load_fixture_ir
from tmig_service.access import Authorizer, Grant, PermissionsSource, Rights, viewer_ir
from tmig_service.auth import Authenticator, AuthSettings, auth_router, load_auth
from tmig_service.datasources import DatasourceRecord, DatasourceStore, uses_published
from tmig_service.downloads import (
    CSV_TYPE,
    FULL_DATA_LIMIT,
    XLSX_TYPE,
    csv_bytes,
    file_name,
    xlsx_bytes,
)
from tmig_service.site import SiteStore, from_exports
from tmig_service.store import STORE_ENV, ReportRecord, ReportStore
from tmig_service.usage import UsageLog, Visit, summarize
from tmig_service.viewer_store import ViewerStore

REPO_ROOT = Path(__file__).resolve().parents[3]


IMAGE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp"})


class DomainRequest(BaseModel):
    """worksheet：篩選控制項「只顯示相關的值」——只列出套用這張工作表上其他篩選（state 為檢視器目前的狀態）
    之後還有資料的值。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["values", "range"] | None = None
    worksheet: str | None = None
    state: QueryState | None = None
    scope: Literal["relevant", "context"] = "relevant"
    """context：「內容中的所有值」——只套用工作表的內容篩選。"""


logger = logging.getLogger(__name__)
DYNAMIC_TTL = 60.0
"""動態參數取值的結果快取的秒數（Tableau 在開啟報表時取值）。"""


@dataclass
class Loaded:
    directory: Path
    ir: IR
    stamp: float
    published: ReportRecord | None = None
    """報表庫的報表（發布到伺服器的）；fixtures 的報表為 None。"""


class Workspace:
    """fixtures 目錄：每個含 golden.ir.json（或 .twbx、.twb）的子目錄是一份報表；
    有報表庫時再加上發布的報表（目前的版本；與 fixtures 同名時以報表庫為準）。"""

    def __init__(self, fixtures: Path, store: ReportStore | None = None) -> None:
        self.fixtures = fixtures
        self.store = store
        self.datasources = DatasourceStore(store.root) if store is not None else None
        """伺服器上的 Published Data Source：報表連到它時改用它的萃取。"""
        self._cache: dict[str, Loaded] = {}
        self._lock = threading.Lock()
        self._data_ready: set[str] = set()

    def fixture_names(self) -> list[str]:
        if not self.fixtures.is_dir():
            return []
        return sorted(
            p.name
            for p in self.fixtures.iterdir()
            if p.is_dir()
            and ((p / "golden.ir.json").is_file() or any(p.glob("*.twbx")) or any(p.glob("*.twb")))
        )

    def names(self) -> list[str]:
        found = set(self.fixture_names())
        if self.store is not None:
            found.update(self.store.ids())
        return sorted(found)

    def get(self, name: str) -> Loaded:
        record = self.store.get(name) if self.store is not None else None
        if record is not None:
            assert self.store is not None
            directory = self.store.directory(record)
            stamp = float(record.latest.version)
        else:
            if name not in self.fixture_names():
                raise HTTPException(404, f"找不到報表「{name}」")
            directory = self.fixtures / name
            golden = directory / "golden.ir.json"
            stamp = golden.stat().st_mtime if golden.is_file() else 0.0
        with self._lock:
            cached = self._cache.get(name)
            if cached is None or cached.stamp != stamp or cached.directory != directory:
                cached = Loaded(directory, load_fixture_ir(directory), stamp, record)
                self._cache[name] = cached
                self._data_ready.discard(name)
            elif cached.published is not record:
                # 同一版的報表在站台管理中改了名稱、Project、擁有者或權限：換上新的紀錄
                cached = Loaded(cached.directory, cached.ir, cached.stamp, record)
                self._cache[name] = cached
        return cached

    def save_ir(self, name: str, ir: IR) -> Loaded:
        """桌面版編輯器的變更：寫回報表目錄的 golden.ir.json，並換掉快取（之後的查詢用新的 IR）。"""
        loaded = self.get(name)
        if loaded.published is not None:
            raise HTTPException(409, "伺服器上的報表不能直接編輯")
        golden = loaded.directory / "golden.ir.json"
        save_ir(ir, golden)
        updated = Loaded(loaded.directory, ir, golden.stat().st_mtime, None)
        with self._lock:
            self._cache[name] = updated
        return updated

    def forget(self, name: str) -> None:
        """報表目錄移除後（桌面版關閉報表）丟掉快取。"""
        with self._lock:
            self._cache.pop(name, None)
            self._data_ready.discard(name)

    def published(self, loaded: Loaded, datasource: str) -> DatasourceRecord | None:
        """報表的資料源連到伺服器上的 Published Data Source（萃取取自 .tdsx）時，回傳那個資料源。"""
        if self.datasources is None:
            return None
        found = next((d for d in loaded.ir.datasources if d.id == datasource), None)
        url = uses_published(found) if found is not None else None
        return self.datasources.find(url) if url else None

    def database(self, name: str, datasource: str) -> Path:
        loaded = self.get(name)
        shared = self.published(loaded, datasource)
        if shared is not None and shared.extract and self.datasources is not None:
            path = self.datasources.data_path(shared)
            if path.is_file():
                return path
        with self._lock:
            if name not in self._data_ready:
                ensure_data(loaded.directory, loaded.ir)
                self._data_ready.add(name)
        return loaded.directory / "data" / f"{datasource}.duckdb"


def open_site(store: ReportStore, authenticator: Authenticator) -> SiteStore | None:
    """報表庫的站台資料：還沒有時以身分驗證設定中 Tableau 匯出的權限與使用者目錄建立（第一次啟動）；
    都沒有時（沒有權限檔、沒有使用者目錄）不管理站台，與先前相同不限制登入的人。"""
    site = SiteStore(store.root)
    settings = authenticator.settings
    if not site.exists():
        if settings.permissions is None and settings.directory is None:
            return None
        site.initialize(
            from_exports(settings.permissions, settings.directory), actor="（第一次啟動時匯入）"
        )
    authenticator.site = site
    return site


class UnderlyingBody(BaseModel):
    """檢視資料的「完整資料」：檢視器目前的狀態與最多的列數。"""

    model_config = ConfigDict(extra="forbid")

    state: QueryState | None = None
    limit: int = PREVIEW_LIMIT


class DownloadBody(BaseModel):
    """下載：交叉資料表、摘要資料或完整資料，CSV 或 Excel。"""

    model_config = ConfigDict(extra="forbid")

    state: QueryState | None = None
    kind: Literal["crosstab", "summary", "underlying"] = "crosstab"
    format: Literal["csv", "xlsx"] = "csv"


def workspace_catalog(workspace: Workspace, site: SiteStore | None) -> Catalog:
    """排程的對象：查詢服務看得到的報表（fixtures 與報表庫；IR 用查詢服務的快取）與伺服器上的資料源。"""
    from tmig_service.schedules import datasource_reports

    linked = {w.id: w for w in site.data().workbooks} if site is not None else {}
    reports: list[Report] = []
    for name in workspace.names():
        try:
            loaded = workspace.get(name)
        except Exception:  # 解析不了的報表不能排程，其他報表照常
            continue
        record = loaded.published
        if record is not None:
            workbook = linked.get(record.tableau_workbook or "")
            reports.append(
                Report(
                    loaded.directory,
                    loaded.ir,
                    workbook.content_url if workbook is not None else None,
                    key=name,
                    owner=record.owner,
                    title=record.name,
                )
            )
        else:
            reports.append(
                Report(loaded.directory, loaded.ir, workbook_url_name(loaded.directory), key=name)
            )
    sources = datasource_reports(workspace.datasources) if workspace.datasources is not None else []
    return Catalog(reports, sources)


@lru_cache(maxsize=64)
def shape_file(path: str) -> tuple[bytes, bytes] | None:
    """地圖形狀檔（tmig.geo.SHAPES 之下的 .json）的內容與 gzip 壓縮後的內容；路徑不合法時為 None。"""
    from tmig.geo import SHAPES

    target = (SHAPES / path).resolve()
    if (
        target.suffix != ".json"
        or not target.is_relative_to(SHAPES.resolve())
        or not target.is_file()
    ):
        return None
    raw = target.read_bytes()
    return raw, gzip.compress(raw, 6)


def current_user(request: Request) -> Identity | None:
    """目前請求的登入者（依 app.state.authenticator）；none 模式為 None，其他模式沒有登入時回應 401。"""
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator.identity(request)


User = Annotated[Identity | None, Depends(current_user)]


def controlled_fields(ir: IR) -> set[str]:
    """報表上有篩選控制項與醒目提示控制項的欄位 id，以及工作表頁面架上的欄位（頁面控制項列出各頁）。"""
    found = {
        zone.field.field
        for dashboard in ir.dashboards
        for zone in iter_zones(dashboard.zones)
        if zone.kind in ("filter", "highlighter") and zone.field is not None
    }
    found |= {
        control.field.field
        for ws in ir.worksheets
        for control in ws.controls
        if control.kind == "filter" and control.field is not None
    }
    # 集合控制項列出集合所在維度的值
    set_fields = {
        zone.field.field
        for dashboard in ir.dashboards
        for zone in iter_zones(dashboard.zones)
        if zone.kind == "set" and zone.field is not None
    } | {
        control.field.field
        for ws in ir.worksheets
        for control in ws.controls
        if control.kind == "set" and control.field is not None
    }
    found |= {
        f.set.levels[0].field
        for ds in ir.datasources
        for f in ds.fields
        if f.id in set_fields and f.set is not None
    }
    return found | {ws.pages.field for ws in ir.worksheets if ws.pages is not None}


def viz_tooltips(ir: IR, sheet: Worksheet, grant: Grant) -> list[dict[str, Any]]:
    """工作表的工具提示中的工作表（Viz in Tooltip）：檢視器滑過標記時以標記的值查詢它們。"""
    names = {w.id: w.name for w in ir.worksheets}
    found: list[dict[str, Any]] = []
    for marks in (sheet.marks, *(p.marks for p in sheet.panes)):
        for tip in marks.tooltip_sheets:
            if any(t["worksheet"] == tip.worksheet for t in found):
                continue
            if not grant.worksheet_allowed(ir, tip.worksheet):
                continue
            found.append(
                {
                    "worksheet": tip.worksheet,
                    "name": names.get(tip.worksheet, tip.worksheet),
                    "max_width": tip.max_width,
                    "max_height": tip.max_height,
                }
            )
    return found


def create_app(
    fixtures: Path | None = None,
    web_dist: Path | None = None,
    runtime: DataRuntime | None = None,
    auth: AuthSettings | Authenticator | None = None,
    usage_dir: Path | None = None,
    routers: Sequence[APIRouter] = (),
    store: ReportStore | None = None,
) -> FastAPI:
    """auth：身分驗證的設定；省略時依環境變數 TMIG_AUTH 讀取（沒有設定時不驗證）。
    usage_dir：使用紀錄的目錄；省略時依環境變數 TMIG_USAGE_DIR，都沒有時不記錄。
    routers：額外的 API（例如桌面版的開啟與儲存），排在檢視器的頁面之前。
    store：報表庫（發布、版本與桌面版登入）；沒有時不提供這些 API（例如桌面版的本機引擎）。"""
    workspace = Workspace(fixtures or REPO_ROOT / "fixtures", store)
    usage_path = usage_dir or (
        Path(os.environ["TMIG_USAGE_DIR"]) if os.environ.get("TMIG_USAGE_DIR") else None
    )
    usage = UsageLog(usage_path) if usage_path is not None else None
    dist = web_dist if web_dist is not None else REPO_ROOT / "web" / "dist"
    data = runtime or DataRuntime()
    authenticator = auth if isinstance(auth, Authenticator) else Authenticator(auth or load_auth())
    site = open_site(store, authenticator) if store is not None else None
    permissions: PermissionsSource = (
        site.permissions if site is not None else authenticator.settings.permissions
    )
    authorizer = Authorizer(permissions)
    rights = Rights(permissions)
    viewer_store = ViewerStore(store.root) if store is not None else None
    """我的最愛、自訂檢視與留言（有報表庫時）。"""

    def require_connect(loaded: Loaded, datasources: list[str], user: Identity | None) -> None:
        """連到伺服器上的 Published Data Source 的資料源：要那個資料源的「連線」權限（與 Tableau 相同）。"""
        from tmig_service.datasource_api import may

        for datasource in datasources:
            shared = workspace.published(loaded, datasource)
            if shared is not None and not may(rights.permissions, user, shared, "Connect"):
                raise HTTPException(403, f"沒有連線到資料源「{shared.name}」的權限")

    def grant_for(loaded: Loaded, user: Identity | None) -> Grant:
        if loaded.published is not None:
            return rights.read(user, loaded.published, loaded.directory, loaded.ir)
        return authorizer.grant(user, loaded.directory, loaded.ir)

    def granted(wb: str, user: Identity | None) -> tuple[Loaded, Grant]:
        """報表與登入者的存取；沒有權限時與報表不存在相同，回應 404。動態參數以目前的資料取值。"""
        loaded = workspace.get(wb)
        grant = grant_for(loaded, user)
        if not grant.allowed:
            raise HTTPException(404, f"找不到報表「{wb}」")
        resolved = dynamic_ir(wb, loaded, user)
        if resolved is not loaded.ir:
            loaded = Loaded(loaded.directory, resolved, loaded.stamp, loaded.published)
        return loaded, grant

    dynamic_cache: dict[tuple[Any, ...], tuple[float, IR]] = {}
    dynamic_lock = threading.Lock()

    def dynamic_ir(wb: str, loaded: Loaded, user: Identity | None) -> IR:
        """動態參數（開啟報表時取自欄位的目前值與允許值）以目前的資料取值後的 IR。

        同一份報表、同一份萃取與使用者（只有用到列層級權限的資料源才分使用者）的結果快取一分鐘；
        查詢失敗或沒有連線權限時維持存檔時的值。"""
        fields = dynamic_fields(loaded.ir)
        if not fields:
            return loaded.ir
        sources = sorted(
            d.id for d in loaded.ir.datasources if any(f.id in fields for f in d.fields)
        )
        try:
            require_connect(loaded, sources, user)
        except HTTPException:
            return loaded.ir
        stamps: list[float] = []
        personal = False
        for source in sources:
            datasource = next(d for d in loaded.ir.datasources if d.id == source)
            personal = personal or bool(protected_fields(loaded.ir, datasource))
            if not isinstance(datasource.connection, LiveConnection):
                path = workspace.database(wb, source)
                stamps.append(path.stat().st_mtime if path.is_file() else 0.0)
        who = (
            (
                user.username,
                user.domain,
                tuple(sorted(user.groups)),
                tuple(sorted(user.attributes.items())),
            )
            if personal and user is not None
            else None
        )
        key = (wb, loaded.stamp, tuple(stamps), who)
        now = time.monotonic()
        with dynamic_lock:
            cached = dynamic_cache.get(key)
            if cached is not None and now - cached[0] < DYNAMIC_TTL:
                return cached[1]

        def run(query: Any, datasource: Any) -> Any:
            return data.run(query, datasource, lambda: workspace.database(wb, datasource.id))

        resolved, notes = resolve_parameters(loaded.ir, run, user)
        for note in notes:
            logger.info("報表 %s：%s", wb, note)
        with dynamic_lock:
            if len(dynamic_cache) > 256:
                dynamic_cache.clear()
            dynamic_cache[key] = (now, resolved)
        return resolved

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        data.close()

    app = FastAPI(title="Tableau 移轉查詢服務", version="0.1.0", lifespan=lifespan)
    app.state.authenticator = authenticator
    app.state.workspace = workspace
    app.state.runtime = data
    app.include_router(auth_router(authenticator))
    for router in routers:
        app.include_router(router)
    if store is not None:
        from tmig_service.desktop_auth import TokenStore, desktop_auth_router
        from tmig_service.publishing import publishing_router

        authenticator.tokens = TokenStore(store.root / "desktop-tokens.json")
        app.include_router(desktop_auth_router(authenticator, authenticator.tokens))

        def loaded_ir(report_id: str) -> tuple[Path, IR]:
            loaded = workspace.get(report_id)
            return loaded.directory, loaded.ir

        app.include_router(
            publishing_router(
                store, rights, data, loaded_ir, workspace.names, authenticator.identity
            )
        )

        def viewer_granted(wb: str, user: Identity | None) -> tuple[IR, Grant, str | None]:
            """報表、登入者的權限與報表的擁有者（報表庫的報表，或 Tableau 匯出的權限中的擁有者）。"""
            loaded, grant = granted(wb, user)
            owner = loaded.published.owner if loaded.published is not None else None
            if owner is None and authorizer.permissions is not None:
                entry = authorizer.entry(loaded.directory, loaded.ir)
                owner = entry.owner if entry is not None else None
            return loaded.ir, grant, owner

        def manages(user: Identity, report_id: str) -> bool:
            """站台管理員、報表的擁有者，或報表所在 Project（與上層）的擁有者與 Project Leader。"""
            if (user.site_role or "") in ADMIN_ROLES:
                return True
            try:
                loaded = workspace.get(report_id)
            except HTTPException:
                return False
            name = user.username.casefold()
            record = loaded.published
            if record is not None:
                if record.owner.casefold() == name:
                    return True
                project: str | None = record.project
            else:
                entry = (
                    authorizer.entry(loaded.directory, loaded.ir)
                    if authorizer.permissions is not None
                    else None
                )
                if entry is None:
                    return False
                if (entry.owner or "").casefold() == name:
                    return True
                project = entry.project
            permissions = rights.permissions
            if project is None or permissions is None:
                return False
            return any(
                (p.owner or "").casefold() == name or decide(p.rules, user, "ProjectLeader")
                for p in permissions.ancestors(project)
            )

        from tmig_service.datasource_api import datasource_router

        def used_by(content_url: str, user: Identity | None) -> list[dict[str, Any]]:
            """用到某個 Published Data Source 的報表（登入者看得到的）。"""
            found: list[dict[str, Any]] = []
            for name in workspace.names():
                try:
                    loaded, _ = granted(name, user)
                except Exception:
                    continue
                if any(
                    (uses_published(d) or "").casefold() == content_url.casefold()
                    for d in loaded.ir.datasources
                ):
                    title = (
                        loaded.published.name
                        if loaded.published is not None
                        else loaded.ir.workbook.name
                    )
                    found.append({"id": name, "name": title})
            return found

        def can_publish(user: Identity | None, project: str) -> tuple[bool, str | None]:
            access = rights.publish(user, project)
            return access.allowed, access.reason

        assert workspace.datasources is not None
        app.include_router(
            datasource_router(
                workspace.datasources,
                site,
                lambda: rights.permissions,
                authenticator.identity,
                used_by,
                can_publish,
                store.root / "uploads",
            )
        )
        if viewer_store is not None:
            from tmig_service.viewer_api import viewer_router

            app.include_router(viewer_router(viewer_store, authenticator.identity, viewer_granted))
        if site is not None:
            from tmig_service.admin import admin_router
            from tmig_service.schedule_api import schedule_router
            from tmig_service.schedules import ScheduleStore

            def report_access(report: str, user: Identity) -> Grant | None:
                """某人對某份報表的權限（訂閱者要能檢視訂閱的報表）；報表不存在時為 None。"""
                try:
                    loaded = workspace.get(report)
                except HTTPException:
                    return None
                return grant_for(loaded, user)

            from tmig_service.alert_api import alert_router
            from tmig_service.alerts import AlertStore

            def alert_measures(
                report: str, worksheet: str, state: dict[str, Any], user: Identity
            ) -> list[dict[str, Any]]:
                """工作表中可以設定警示的量值（數字的量值欄）與目前的範圍。"""
                try:
                    query_state = QueryState.model_validate(state) if state else None
                except ValidationError as exc:
                    raise HTTPException(422, f"篩選的狀態不正確：{exc}") from exc
                result = render(report, worksheet, user, query_state)
                table = result.get("table") or {}
                found: list[dict[str, Any]] = []
                for i, (key, header, role) in enumerate(
                    zip(
                        table.get("keys", []),
                        table.get("headers", []),
                        table.get("roles", []),
                        strict=False,
                    )
                ):
                    if role != "measure":
                        continue
                    values: list[float] = []
                    for row in table.get("raw", []):
                        try:
                            values.append(float(row[i]))
                        except (TypeError, ValueError):
                            continue
                    if values:
                        found.append(
                            {"key": key, "name": header, "min": min(values), "max": max(values)}
                        )
                return found

            app.include_router(admin_router(site, store, rights, authenticator.identity))
            app.include_router(
                alert_router(
                    AlertStore(store.root),
                    ScheduleStore(store.root),
                    site,
                    authenticator.identity,
                    viewer_granted,
                    alert_measures,
                    manages,
                )
            )
            app.include_router(
                schedule_router(
                    ScheduleStore(store.root),
                    site,
                    store,
                    rights,
                    authenticator.identity,
                    lambda: workspace_catalog(workspace, site),
                    report_access,
                )
            )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/geo/{version}/{path:path}")
    def geo_shape(version: str, path: str, request: Request) -> Response:
        """地圖的形狀與底圖（TopoJSON，平台內建的公開地理資料，不需登入）：網址含資料的版本，
        瀏覽器可以長期快取；支援 gzip 時回傳壓縮過的內容。"""
        content = shape_file(path)
        if content is None:
            raise HTTPException(404, f"找不到地圖形狀「{path}」")
        raw, packed = content
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "Vary": "Accept-Encoding",
        }
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return Response(packed, media_type="application/json", headers=headers)
        return Response(raw, media_type="application/json", headers=headers)

    @app.post("/api/workbooks/{wb}/views/{view_id}/visit", status_code=204)
    def visit(wb: str, view_id: str, user: User) -> Response:
        """檢視器開啟一個 dashboard、工作表或 Story（切換故事點不算）；使用者取自登入者，
        沒有權限的檢視回應 404 不記錄。"""
        loaded, grant = granted(wb, user)
        ir = loaded.ir
        board = next((d for d in ir.dashboards if d.id == view_id), None)
        sheet = next((w for w in ir.worksheets if w.id == view_id), None)
        story = next((s for s in ir.stories if s.id == view_id), None)
        if board is not None and (
            grant.dashboard_allowed(board.name) or board.id in grant.story_dashboards(ir)
        ):
            name, kind = board.name, "dashboard"
        elif sheet is not None and grant.worksheet_allowed(ir, sheet.id):
            name, kind = sheet.name, "worksheet"
        elif story is not None and grant.story_allowed(story.name):
            name, kind = story.name, "story"
        else:
            raise HTTPException(404, f"找不到 view「{view_id}」")
        if usage is not None:
            usage.record(
                Visit(
                    time=dt.datetime.now(dt.UTC),
                    user=user.username if user is not None else None,
                    workbook=wb,
                    workbook_name=ir.workbook.name,
                    view=name,
                    view_id=view_id,
                    kind=kind,  # type: ignore[arg-type]
                )
            )
        return Response(status_code=204)

    @app.get("/api/usage")
    def usage_summary(user: User, days: int = 90) -> dict[str, Any]:
        """期間內每份報表與 view 的使用量（沒有檢視的報表也列出）；有權限檔時只有管理員可以看。"""
        if authorizer.permissions is not None and (
            user is None or (user.site_role or "") not in ADMIN_ROLES
        ):
            raise HTTPException(403, "使用紀錄只有管理員可以看")
        if days < 1:
            raise HTTPException(422, "days 至少為 1")
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        names = {name: workspace.get(name).ir.workbook.name for name in workspace.names()}
        visits = usage.visits(since) if usage is not None else iter(())
        return {
            "since": since.isoformat(),
            "recording": usage is not None,
            "workbooks": [u.to_dict() for u in summarize(visits, names)],
        }

    @app.get("/api/workbooks")
    def workbooks(user: User) -> list[dict[str, Any]]:
        result = []
        for name in workspace.names():
            loaded = workspace.get(name)
            ir = loaded.ir
            grant = grant_for(loaded, user)
            if not grant.allowed:
                continue
            record = loaded.published
            result.append(
                {
                    "id": name,
                    "published": None
                    if record is None
                    else {
                        "project": record.project,
                        "project_name": rights.project_path(record.project),
                        "version": record.latest.version,
                        "published_at": record.latest.published_at,
                        "publisher": record.latest.publisher,
                        "can_download": rights.download(user, record).allowed,
                        "can_overwrite": rights.overwrite(user, record).allowed,
                    },
                    "workbook": ir.workbook.id,
                    "name": ir.workbook.name,
                    "dashboards": [
                        {"id": d.id, "name": d.name}
                        for d in ir.dashboards
                        if grant.dashboard_allowed(d.name)
                    ],
                    "worksheets": [
                        {"id": w.id, "name": w.name, "hidden": w.hidden}
                        for w in ir.worksheets
                        if grant.worksheet_allowed(ir, w.id)
                    ],
                    "stories": [
                        {"id": s.id, "name": s.name}
                        for s in ir.stories
                        if grant.story_allowed(s.name)
                    ],
                    "unsupported": len(ir.unsupported),
                }
            )
        return result

    @app.get("/api/workbooks/{wb}/ir")
    def workbook_ir(wb: str, user: User) -> dict[str, Any]:
        loaded, grant = granted(wb, user)
        return viewer_ir(loaded.ir, grant)

    @app.get("/api/workbooks/{wb}/cases")
    def cases(wb: str, user: User) -> list[dict[str, Any]]:
        loaded, _ = granted(wb, user)
        path = loaded.directory / "cases.yaml"
        if not path.is_file():
            return []
        try:
            spec = load_cases(path)
            return [
                {
                    "name": case.name,
                    "note": case.note,
                    "url_params": case.url_params,
                    "state": case_state(loaded.ir, case, spec.as_of()).model_dump(mode="json"),
                }
                for case in spec.cases
            ]
        except CasesError as exc:
            raise HTTPException(422, str(exc)) from exc

    def as_of(directory: Path) -> str | None:
        """相對日期篩選的預設基準日：cases.yaml 的 today 或抓取預期結果的日期（與計算比對相同）。"""
        path = directory / "cases.yaml"
        if not path.is_file():
            return None
        try:
            return load_cases(path).as_of()
        except CasesError:
            return None

    @app.get("/api/workbooks/{wb}/files/{path:path}")
    def package_file(wb: str, path: str, user: User) -> Response:
        """報表的圖片：.twbx 內或報表套件解開的 files/；只提供圖片，路徑須與封裝內的檔名完全相同
        （反斜線視為斜線）。"""
        loaded, _ = granted(wb, user)
        if Path(path).suffix.lower() not in IMAGE_TYPES:
            raise HTTPException(404, "只提供圖片檔")
        content = read_report_file(loaded.directory, path)
        if content is None:
            raise HTTPException(404, f"找不到檔案「{path}」")
        media = mimetypes.guess_type(path)[0] or "application/octet-stream"
        # 報表內的 SVG 可能含指令碼：直接開啟時也不執行
        headers = {
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content, media_type=media, headers=headers)

    @app.post("/api/workbooks/{wb}/worksheets/{ws}/render")
    def render(wb: str, ws: str, user: User, state: QueryState | None = None) -> dict[str, Any]:
        loaded, grant = granted(wb, user)
        ir = loaded.ir
        if not grant.worksheet_allowed(ir, ws):
            raise HTTPException(404, f"找不到工作表「{ws}」")
        state = state or QueryState()
        if state.today is None:
            state = state.model_copy(update={"today": as_of(loaded.directory)})
        target = next((w for w in ir.worksheets if w.id == ws), None)
        # 階層的切入：以切入後的欄、列架查詢與繪製；切入的控制項依存檔的工作表
        drill = drill_info(ir, target, state.drill) if target is not None else []
        tooltip_sheets = viz_tooltips(ir, target, grant) if target is not None else []
        ir = apply_drill(ir, ws, state.drill)
        # 地圖平移、縮放後的顯示範圍（只影響繪製）
        ir = with_viewport(ir, ws, state.viewport)
        empty = {
            "worksheet": ws,
            "name": target.name if target else ws,
            "title": target.name if target else ws,
            "rows": 0,
            "spec": None,
            "unsupported": None,
            "notes": [],
        }
        if target is not None and not [
            *target.rows,
            *target.columns,
            *target.marks.uses(),
            *(u for pane in target.panes for u in pane.marks.uses()),
        ]:
            # 空白的工作表（例如編輯器中剛新增的）：與 Tableau 相同什麼都不畫
            return empty
        try:
            query = worksheet_sql(ir, ws, state, user)
        except StateError as exc:
            raise HTTPException(422, str(exc)) from exc
        except QueryError as exc:
            if exc.unsupported:
                return {
                    "worksheet": ws,
                    "rows": 0,
                    "spec": None,
                    "unsupported": str(exc),
                    "notes": [],
                }
            raise HTTPException(422, str(exc)) from exc
        if not query.columns:
            # 沒有任何欄位要查詢（例如只有 Measure Names、還沒有量值）
            return empty
        sheet = next(w for w in ir.worksheets if w.id == query.worksheet)
        datasource = next(d for d in ir.datasources if d.id == sheet.datasource)
        require_connect(loaded, [sheet.datasource], user)

        def database() -> Path:
            return workspace.database(wb, sheet.datasource)

        # 詳細程度較粗的標記卡（例如甜甜圈中間的合計）各自查詢，只用於圖表；查不了時以工作表的資料呈現
        notes: list[str] = []
        try:
            pane_queries = pane_sql(ir, sheet.id, query, state, user)
        except QueryError as exc:
            pane_queries = {}
            notes.append(f"各標記卡無法另外查詢，以工作表的詳細程度呈現：{exc}")
        try:
            # 總計（Grand Total）與標題中的欄位值（例如 KPI 卡標題中的合計）也是報表上的數字：
            # 算不出來時整張工作表列為不支援，不默默省略
            result = data.run(query, datasource, database)
            # 與量值篩選一起使用時，總計只合計通過篩選的標記（以工作表的結果限定）
            total_queries = totals_sql(ir, sheet.id, query, state, user, result)
            title_queries = title_sql(ir, sheet.id, query, state, user)
            panes = {
                i: (q.columns, data.run(q, datasource, database)) for i, q in pane_queries.items()
            }
            totals = [(t, data.run(t.query, datasource, database)) for t in total_queries]
            totals = visual_totals(sheet, query, result, totals)
            if sheet.percentages is not None:
                # 百分比（分析＞百分比）：量值換成占範圍合計的比例（合計另外查詢）
                result, totals = percentages(
                    ir,
                    sheet.id,
                    query,
                    result,
                    totals,
                    lambda q: data.run(q, datasource, database),
                    state,
                    user,
                )
                if panes:
                    panes = {}
                    notes.append("使用百分比時，各標記卡一律以工作表的詳細程度呈現")
            title_fields: dict[str, str] = {}
            if title_queries is not None:
                measures = title_queries.measures
                title_fields = title_values(
                    ir,
                    sheet,
                    title_queries,
                    data.run(measures, datasource, database) if measures is not None else None,
                    {
                        key: data.run(q, datasource, database)
                        for key, q in title_queries.dimensions.items()
                    },
                    (query.columns, result),
                )
        except QueryError as exc:
            # 總計尚不支援的寫法，或 Live 連線下推的 SQL 轉不成來源資料庫的方言
            if not exc.unsupported:
                raise HTTPException(422, str(exc)) from exc
            return {
                "worksheet": sheet.id,
                "name": sheet.name,
                "title": worksheet_title(ir, sheet, query.state.parameters),
                "rows": 0,
                "spec": None,
                "unsupported": str(exc),
                "notes": notes,
            }
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(500, str(exc)) from exc
        rendered_notes: list[str] = []
        # 顯示遺漏的值：圖表與資料表用同一份補好的結果
        result = pad_result(ir, sheet, query.columns, result, query.state.parameters)
        # 表格版面的「顯示空的列／欄」：這一架的成員組合另外查詢，補上沒有標記的
        roles = {c.key: c.role for c in query.columns}
        for shelf in sheet.show_empty:
            try:
                headers = empty_headers_sql(ir, sheet.id, shelf, state, user)
                if headers is None:
                    continue
                found = data.run(headers, datasource, database)
            except (QueryError, ExecutionError, SourceConnectionError) as exc:
                rendered_notes.append(
                    f"顯示空的{'列' if shelf == 'rows' else '欄'}：查不到成員（{exc}）"
                )
                continue
            keys = [c.key for c in headers.columns if c.role == "dimension"]
            at = [found.columns.index(key) for key in keys]
            leading = shelf == "rows" or COLUMN_ORDER not in result.columns
            result = pad_empty(
                result, keys, [tuple(row[i] for i in at) for row in found.rows], roles, leading
            )
        rendered = build_spec(
            ir,
            sheet.id,
            query.columns,
            result,
            query.state.parameters,
            panes or None,
            totals or None,
        )
        rendered.notes.extend([*notes, *rendered_notes])
        if query.analytics is not None:
            # 叢集與預測在查詢之後計算：估計不了時的說明（例如資料不足以預測）
            rendered.notes.extend(query.analytics.notes)
        fields = {f.id: f for ds in ir.datasources for f in ds.fields}
        # 空間資料（形狀的 WKT）不放進資料表（地圖上已畫出，文字很長）
        shown = [
            c
            for c in query.columns
            if c.role != "sort" and c.use is not None and c.datatype != "spatial"
        ]
        index = {key: i for i, key in enumerate(result.columns)}
        starts = fiscal_starts(ir)
        # 成員別名：資料表與圖表相同，顯示別名
        aliases = {
            c.key: member_aliases(fields[c.use.field], c.use)
            for c in shown
            if c.use is not None and c.role == "dimension"
        }

        def cells(row: Any, positions: dict[str, int], labels: dict[str, str]) -> list[str]:
            out: list[str] = []
            for column in shown:
                assert column.use is not None
                if column.key in labels:
                    out.append(labels[column.key])
                    continue
                value = row[positions[column.key]] if column.key in positions else None
                use = column.use
                alias = aliases.get(column.key, {}).get(alias_key(value))
                fy = (
                    fiscal_label(value, use.date.kind, use.date.unit, starts.get(use.field, 1))
                    if use.date is not None
                    else None
                )
                if alias is not None:
                    out.append(alias)
                elif fy is not None:
                    # 會計年度的年、季：與 Tableau 相同加上 FY
                    out.append(fy)
                elif use.date is not None and use.date.kind == "part" and value is not None:
                    out.append(str(value))
                elif value is None and column.role == "measure" and sheet.null_text is not None:
                    out.append(sheet.null_text)  # 特殊值的文字
                else:
                    trunc = use.date.unit if use.date is not None else None
                    fmt = value_format(sheet, fields[use.field], use)
                    out.append(display_value(value, column.datatype, fmt, trunc))
            return out

        table_rows = [cells(row, index, {}) for row in result.rows]
        # 與圖表相同：各架的小計緊接在它的組之後，總計與交叉處在最後
        for total, total_result in totals:
            positions = {key: i for i, key in enumerate(total_result.columns)}
            labels = {key: total.label(key) for key in total.dropped}
            added = [cells(row, positions, labels) for row in total_result.rows]
            if total.group is None:
                table_rows += added
                continue
            at = [i for i, c in enumerate(shown) if c.key in total.group]
            table_rows = after_groups(table_rows, added, lambda r, at=at: tuple(r[i] for i in at))
        return {
            "worksheet": sheet.id,
            "name": sheet.name,
            "title": worksheet_title(ir, sheet, query.state.parameters, title_fields),
            # 說明的文字一律附上：單獨檢視依工作表的設定顯示，dashboard 的區塊各自決定
            "caption": worksheet_caption(ir, sheet, query.state.parameters, force=True),
            "caption_shown": sheet.caption_shown,
            "rows": rendered.rows,
            "mark": rendered.mark,
            "spec": rendered.spec,
            "unsupported": rendered.unsupported,
            "notes": rendered.notes,
            "applied": query.state.applied,
            "skipped": query.state.skipped,
            "drill": drill,
            "tooltip_sheets": tooltip_sheets,
            "table": {
                "keys": [c.key for c in shown],
                "headers": [caption(fields[c.use.field], c.use, sheet) for c in shown if c.use],
                "rows": table_rows,
                "raw": [[format_value(row[index[c.key]]) for c in shown] for row in result.rows],
                "roles": [c.role for c in shown],
            },
        }

    @app.get("/api/workbooks/{wb}/access")
    def workbook_access(wb: str, user: User) -> dict[str, bool]:
        """檢視器上依權限顯示的功能：下載圖片／PDF、資料與交叉資料表、完整資料、報表、自訂檢視與留言。"""
        _, grant = granted(wb, user)
        return {
            "export_image": grant.can("ExportImage"),
            "export_data": grant.can("ExportData"),
            "underlying_data": grant.can("ViewUnderlyingData"),
            "download_workbook": grant.can("ExportXml"),
            "share_view": grant.can("ShareView"),
            "view_comments": grant.can("ViewComments"),
            "add_comment": grant.can("AddComment"),
            "store": viewer_store is not None and user is not None,
        }

    def underlying_rows(
        loaded: Loaded,
        wb: str,
        ws: str,
        state: QueryState | None,
        user: Identity | None,
        limit: int,
    ) -> tuple[list[str], list[list[Any]], bool]:
        """工作表的完整資料：前 limit 列與是否還有更多。"""
        state = state or QueryState()
        if state.today is None:
            state = state.model_copy(update={"today": as_of(loaded.directory)})
        ir = apply_drill(loaded.ir, ws, state.drill)
        try:
            query = underlying_query(worksheet_sql(ir, ws, state, user), limit + 1)
        except StateError as exc:
            raise HTTPException(422, str(exc)) from exc
        except QueryError as exc:
            raise HTTPException(422, str(exc)) from exc
        sheet = next(w for w in ir.worksheets if w.id == query.worksheet)
        datasource = next(d for d in ir.datasources if d.id == sheet.datasource)
        require_connect(loaded, [sheet.datasource], user)
        try:
            result = data.run(query, datasource, lambda: workspace.database(wb, sheet.datasource))
        except QueryError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(500, str(exc)) from exc
        keep = [i for i, name in enumerate(result.columns) if not name.startswith("__")]
        rows = [[row[i] for i in keep] for row in result.rows[:limit]]
        return [result.columns[i] for i in keep], rows, len(result.rows) > limit

    @app.post("/api/workbooks/{wb}/worksheets/{ws}/underlying")
    def underlying(wb: str, ws: str, body: UnderlyingBody, user: User) -> dict[str, Any]:
        """檢視資料的「完整資料」：工作表的基礎資料列（要「檢視基礎資料」的權限）。"""
        loaded, grant = granted(wb, user)
        if not grant.worksheet_allowed(loaded.ir, ws) or not any(
            w.id == ws for w in loaded.ir.worksheets
        ):
            raise HTTPException(404, f"找不到工作表「{ws}」")
        if not grant.can("ViewUnderlyingData"):
            raise HTTPException(403, "沒有檢視完整資料的權限（檢視基礎資料）")
        limit = max(1, min(body.limit, 100_000))
        columns, rows, truncated = underlying_rows(loaded, wb, ws, body.state, user, limit)
        return {
            "columns": columns,
            "rows": [[_cell(v) for v in row] for row in rows],
            "truncated": truncated,
            "limit": limit,
        }

    @app.post("/api/workbooks/{wb}/worksheets/{ws}/download")
    def download(wb: str, ws: str, body: DownloadBody, user: User) -> Response:
        """下載交叉資料表（Excel、CSV）、摘要資料或完整資料（tmig_service.downloads）。"""
        loaded, grant = granted(wb, user)
        sheet = next((w for w in loaded.ir.worksheets if w.id == ws), None)
        if sheet is None or not grant.worksheet_allowed(loaded.ir, ws):
            raise HTTPException(404, f"找不到工作表「{ws}」")
        if body.kind == "underlying":
            if not grant.can("ViewUnderlyingData"):
                raise HTTPException(403, "沒有下載完整資料的權限（檢視基礎資料）")
            headers, rows, _ = underlying_rows(loaded, wb, ws, body.state, user, FULL_DATA_LIMIT)
        else:
            if not grant.can("ExportData"):
                raise HTTPException(403, "沒有下載資料的權限（下載摘要資料）")
            result = render(wb, ws, user, body.state)
            table = result.get("table")
            if result.get("unsupported") or not table:
                raise HTTPException(422, "這張工作表沒有可以下載的資料")
            headers = list(table["headers"])
            rows = table["rows"] if body.kind == "crosstab" else table["raw"]
        if body.format == "xlsx":
            content, media = xlsx_bytes(sheet.name, headers, rows), XLSX_TYPE
        else:
            content, media = csv_bytes(headers, rows), CSV_TYPE
        return Response(
            content,
            media_type=media,
            headers={"Content-Disposition": file_name(sheet.name, f".{body.format}")},
        )

    @app.get("/api/recents")
    def recents(user: User, limit: int = 12) -> list[dict[str, Any]]:
        """最近檢視：登入者 90 天內開啟過的 view（依使用紀錄，最新的在前；沒有權限了的不列出）。"""
        if usage is None or user is None:
            raise HTTPException(404, "沒有使用紀錄")
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=90)
        latest: dict[tuple[str, str], Visit] = {}
        for visit in usage.visits(since):
            if not visit.view_id or (visit.user or "").casefold() != user.username.casefold():
                continue
            key = (visit.workbook, visit.view_id)
            if key not in latest or visit.time > latest[key].time:
                latest[key] = visit
        result: list[dict[str, Any]] = []
        for visit in sorted(latest.values(), key=lambda v: v.time, reverse=True):
            try:
                loaded, _ = granted(visit.workbook, user)
            except HTTPException:
                continue
            result.append(
                {
                    "report": visit.workbook,
                    "view": visit.view_id,
                    "view_name": visit.view,
                    "report_name": loaded.ir.workbook.name,
                    "kind": visit.kind,
                    "time": visit.time.isoformat(),
                }
            )
            if len(result) >= max(1, min(limit, 50)):
                break
        return result

    @app.get("/api/workbooks/{wb}/datasources")
    def datasources(wb: str, user: User) -> list[dict[str, Any]]:
        """資料源分頁：連線、資料表與關聯、欄位與資料源篩選（與完整的報表定義相同要下載 workbook
        的權限）。"""
        loaded, grant = granted(wb, user)
        if not grant.full_definition:
            raise HTTPException(403, "沒有檢視資料源的權限（需要「下載 workbook」的權限）")
        return [datasource_summary(ds, loaded.ir.parameters) for ds in loaded.ir.datasources]

    @app.get("/api/workbooks/{wb}/datasources/{ds}/preview")
    def datasource_preview(
        wb: str, ds: str, user: User, table: str | None = None, limit: int = PREVIEW_LIMIT
    ) -> dict[str, Any]:
        """資料源分頁的資料預覽：前 limit 列原始資料（套用資料源篩選與列層級權限）；萃取另外回傳
        篩選後的總列數。要下載 workbook 與檢視基礎資料的權限。"""
        loaded, grant = granted(wb, user)
        if not (grant.full_definition and grant.underlying_data):
            raise HTTPException(
                403, "沒有檢視資料源資料的權限（需要「下載 workbook」與「檢視基礎資料」的權限）"
            )
        ir = loaded.ir
        datasource = next((d for d in ir.datasources if d.id == ds), None)
        if datasource is None:
            raise HTTPException(404, f"找不到資料源「{ds}」")
        limit = max(1, min(limit, PREVIEW_LIMIT))
        require_connect(loaded, [ds], user)

        def database() -> Path:
            return workspace.database(wb, ds)

        try:
            preview = preview_sql(ir, ds, table, limit, user)
            parameters = custom_sql_parameters(ir)
            result = data.run_sql(preview.sql, datasource, database, parameters)
            total: int | None = None
            if datasource.connection.kind in ("extract", "file"):
                counted = data.run_sql(preview.count_sql, datasource, database, parameters)
                total = int(counted.rows[0][0]) if counted.rows else 0
        except QueryError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(500, str(exc)) from exc
        return {
            "datasource": ds,
            "table": preview.table,
            "columns": [asdict(c) for c in preview.columns],
            "rows": [[_cell(v) for v in row] for row in result.rows],
            "total": total,
            "limit": limit,
        }

    @app.post("/api/workbooks/{wb}/fields/{field}/domain")
    def domain(
        wb: str,
        field: str,
        user: User,
        request: Annotated[DomainRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        loaded, grant = granted(wb, user)
        ir = loaded.ir
        try:
            use = parse_key(field)
        except StateError as exc:
            raise HTTPException(422, str(exc)) from exc
        ds = next((d for d in ir.datasources if any(f.id == use.field for f in d.fields)), None)
        if ds is None:
            raise HTTPException(404, f"找不到欄位「{use.field}」")
        if use.field not in controlled_fields(ir):
            # 與 Tableau 相同，只有放了篩選控制項的欄位會列出可選值（避免列舉其他欄位的值）
            raise HTTPException(403, f"欄位「{use.field}」在報表上沒有篩選控制項")
        data_field = next(f for f in ds.fields if f.id == use.field)
        kind: Literal["values", "range"]
        if request is not None and request.kind is not None:
            kind = request.kind
        elif (use.date is None and data_field.datatype in ("date", "datetime")) or (
            use.date is None
            and data_field.role == "measure"
            and data_field.datatype in ("integer", "real")
        ):
            kind = "range"
        else:
            kind = "values"
        require_connect(loaded, [ds.id], user)
        relevant = request.worksheet if request is not None and kind == "values" else None
        if relevant is not None:
            # 只顯示相關的值：以工作表上其他的篩選查詢這個欄位的值
            sheet = next((w for w in ir.worksheets if relevant in (w.id, w.name)), None)
            if sheet is None or not grant.worksheet_allowed(ir, sheet.id):
                raise HTTPException(404, f"找不到工作表「{relevant}」")
            assert request is not None
            try:
                query = relevant_domain_sql(
                    ir, sheet.id, use, request.state, user, request.scope == "context"
                )
                source = next(d for d in ir.datasources if d.id == sheet.datasource)
                found = data.run(query, source, lambda: workspace.database(wb, sheet.datasource))
            except StateError as exc:
                raise HTTPException(422, str(exc)) from exc
            except QueryError as exc:
                raise HTTPException(422, str(exc)) from exc
            except SourceConnectionError as exc:
                raise HTTPException(503, str(exc)) from exc
            except ExecutionError as exc:
                raise HTTPException(500, str(exc)) from exc
            return {"kind": "values", "values": [_json(r[0]) for r in found.rows]}
        try:
            sql = domain_sql(ir, ds.id, use, kind, user)
            result = data.run_sql(
                sql, ds, lambda: workspace.database(wb, ds.id), custom_sql_parameters(ir)
            )
        except QueryError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(500, str(exc)) from exc
        if kind == "range":
            low, high = result.rows[0] if result.rows else (None, None)
            return {"kind": "range", "min": _json(low), "max": _json(high)}
        return {"kind": "values", "values": [_json(r[0]) for r in result.rows]}

    if dist.is_dir():
        index_html = dist / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404, "找不到 API")
            target = (dist / path).resolve()
            if path and target.is_file() and dist.resolve() in target.parents:
                return FileResponse(target)
            return FileResponse(index_html)

    return app


def _cell(value: Any) -> Any:
    """預覽的一格：無限大與 NaN 不是合法的 JSON，以 Null 表示；其餘同 _json。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return _json(value)


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return float(value)


def serve(
    fixtures: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    web_dist: Path | None = None,
    auth: Path | None = None,
    usage_dir: Path | None = None,
    store: Path | None = None,
) -> None:
    """auth：身分驗證設定檔；省略時讀環境變數 TMIG_AUTH。usage_dir：使用紀錄的目錄（預設為報表目錄旁的 usage）。
    store：報表庫（發布的報表與每一版）；省略時讀環境變數 TMIG_STORE，都沒有時為報表目錄旁的 store。"""
    import sys

    import uvicorn

    settings = load_auth(auth)
    if settings.mode == "none":
        print(
            "注意：未設定身分驗證（TMIG_AUTH），只適合本機檢視；用到使用者函式的報表無法查詢",
            file=sys.stderr,
        )
    elif settings.mode == "dev":
        print("注意：開發模式，任何人都可以切換成任何使用者，不可用於正式環境", file=sys.stderr)
    # 不依 X-Forwarded-For 改寫來源位址：反向代理模式以實際連線的位址判斷是否信任
    usage = usage_dir or (
        Path(os.environ["TMIG_USAGE_DIR"])
        if os.environ.get("TMIG_USAGE_DIR")
        else fixtures.resolve().parent / "usage"
    )
    store_root = store or (
        Path(os.environ[STORE_ENV])
        if os.environ.get(STORE_ENV)
        else fixtures.resolve().parent / "store"
    )
    uvicorn.run(
        create_app(
            fixtures, web_dist, auth=settings, usage_dir=usage, store=ReportStore(store_root)
        ),
        host=host,
        port=port,
        proxy_headers=False,
    )
