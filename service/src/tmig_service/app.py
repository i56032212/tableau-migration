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
import mimetypes
import os
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict

from tmig.access.permissions import ADMIN_ROLES
from tmig.data.executor import ExecutionError, format_value
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError
from tmig.ir import IR, iter_zones
from tmig.package import read_report_file
from tmig.planner.identity import Identity
from tmig.planner.state import QueryState, StateError, parse_key
from tmig.render.aliases import alias_key, member_aliases
from tmig.render.format import format_value as display_value
from tmig.render.vegalite import (
    after_groups,
    build_spec,
    caption,
    title_values,
    value_format,
    worksheet_title,
)
from tmig.sqlgen.query import (
    QueryError,
    custom_sql_parameters,
    domain_sql,
    pane_sql,
    title_sql,
    totals_sql,
    worksheet_sql,
)
from tmig.verify.cases import CasesError, case_state, load_cases
from tmig.verify.run import ensure_data, load_fixture_ir
from tmig_service.access import Authorizer, Grant, Rights, viewer_ir
from tmig_service.auth import Authenticator, AuthSettings, auth_router, load_auth
from tmig_service.store import STORE_ENV, ReportRecord, ReportStore
from tmig_service.usage import UsageLog, Visit, summarize

REPO_ROOT = Path(__file__).resolve().parents[3]


IMAGE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp"})


class DomainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["values", "range"] | None = None


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
        return cached

    def forget(self, name: str) -> None:
        """報表目錄移除後（桌面版關閉報表）丟掉快取。"""
        with self._lock:
            self._cache.pop(name, None)
            self._data_ready.discard(name)

    def database(self, name: str, datasource: str) -> Path:
        loaded = self.get(name)
        with self._lock:
            if name not in self._data_ready:
                ensure_data(loaded.directory, loaded.ir)
                self._data_ready.add(name)
        return loaded.directory / "data" / f"{datasource}.duckdb"


def current_user(request: Request) -> Identity | None:
    """目前請求的登入者（依 app.state.authenticator）；none 模式為 None，其他模式沒有登入時回應 401。"""
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator.identity(request)


User = Annotated[Identity | None, Depends(current_user)]


def controlled_fields(ir: IR) -> set[str]:
    """報表上有篩選控制項的欄位 id。"""
    return {
        zone.field.field
        for dashboard in ir.dashboards
        for zone in iter_zones(dashboard.zones)
        if zone.kind == "filter" and zone.field is not None
    }


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
    authorizer = Authorizer(authenticator.settings.permissions)
    rights = Rights(authenticator.settings.permissions)

    def grant_for(loaded: Loaded, user: Identity | None) -> Grant:
        if loaded.published is not None:
            return rights.read(user, loaded.published, loaded.directory, loaded.ir)
        return authorizer.grant(user, loaded.directory, loaded.ir)

    def granted(wb: str, user: Identity | None) -> tuple[Loaded, Grant]:
        """報表與登入者的存取；沒有權限時與報表不存在相同，回應 404。"""
        loaded = workspace.get(wb)
        grant = grant_for(loaded, user)
        if not grant.allowed:
            raise HTTPException(404, f"找不到報表「{wb}」")
        return loaded, grant

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

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
                        {"id": w.id, "name": w.name}
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
        sheet = next(w for w in ir.worksheets if w.id == query.worksheet)
        datasource = next(d for d in ir.datasources if d.id == sheet.datasource)

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
            total_queries = totals_sql(ir, sheet.id, query, state, user)
            title_queries = title_sql(ir, sheet.id, query, state, user)
            result = data.run(query, datasource, database)
            panes = {
                i: (q.columns, data.run(q, datasource, database)) for i, q in pane_queries.items()
            }
            totals = [(t, data.run(t.query, datasource, database)) for t in total_queries]
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
        rendered = build_spec(
            ir,
            sheet.id,
            query.columns,
            result,
            query.state.parameters,
            panes or None,
            totals or None,
        )
        rendered.notes.extend(notes)
        fields = {f.id: f for ds in ir.datasources for f in ds.fields}
        shown = [c for c in query.columns if c.role != "sort" and c.use is not None]
        index = {key: i for i, key in enumerate(result.columns)}
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
                if alias is not None:
                    out.append(alias)
                elif use.date is not None and use.date.kind == "part" and value is not None:
                    out.append(str(value))
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
            "rows": rendered.rows,
            "mark": rendered.mark,
            "spec": rendered.spec,
            "unsupported": rendered.unsupported,
            "notes": rendered.notes,
            "applied": query.state.applied,
            "skipped": query.state.skipped,
            "table": {
                "keys": [c.key for c in shown],
                "headers": [caption(fields[c.use.field], c.use, sheet) for c in shown if c.use],
                "rows": table_rows,
                "raw": [[format_value(row[index[c.key]]) for c in shown] for row in result.rows],
            },
        }

    @app.post("/api/workbooks/{wb}/fields/{field}/domain")
    def domain(
        wb: str,
        field: str,
        user: User,
        request: Annotated[DomainRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        loaded, _ = granted(wb, user)
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
