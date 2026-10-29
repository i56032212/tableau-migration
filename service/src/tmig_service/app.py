"""查詢服務（F2）：FastAPI＋DuckDB，只接受篩選與參數狀態，從不接受 SQL。

    uv run tmig serve                  # 預設讀 fixtures/，http://127.0.0.1:8000

API：

- ``GET  /api/workbooks``：fixtures 中的報表
- ``GET  /api/workbooks/{wb}/ir``：IR
- ``POST /api/workbooks/{wb}/worksheets/{ws}/render``：依篩選與參數狀態查詢，回傳 Vega-Lite 規格（資料內嵌）與結果列數
- ``POST /api/workbooks/{wb}/fields/{field}/domain``：篩選器的可選值，或最小值與最大值
- ``GET  /api/workbooks/{wb}/cases``：cases.yaml 的篩選組合（已轉成篩選狀態），供檢視器切換
- ``GET  /api/workbooks/{wb}/files/{path}``：.twbx 內的圖片（圖片區塊與圖片按鈕）

其餘路徑提供檢視器（web/dist）。

萃取的資料源查詢報表目錄下的 DuckDB 檔；Live 連線依連線設定檔（環境變數 TMIG_CONNECTIONS，
預設為目前目錄的 connections.toml）連到來源資料庫，以兩段式執行（第三階段 N3、N4）。
"""

from __future__ import annotations

import mimetypes
import threading
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict

from tmig.data.executor import ExecutionError, format_value
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError
from tmig.ir import IR
from tmig.planner.state import QueryState, StateError, parse_key
from tmig.render.format import display_format
from tmig.render.format import format_value as display_value
from tmig.render.vegalite import build_spec, caption
from tmig.sqlgen.query import (
    QueryError,
    custom_sql_parameters,
    domain_sql,
    pane_sql,
    worksheet_sql,
)
from tmig.verify.cases import CasesError, case_state, load_cases
from tmig.verify.run import ensure_data, load_fixture_ir

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


class Workspace:
    """fixtures 目錄：每個含 golden.ir.json（或 .twbx）的子目錄是一份報表。"""

    def __init__(self, fixtures: Path) -> None:
        self.fixtures = fixtures
        self._cache: dict[str, Loaded] = {}
        self._lock = threading.Lock()
        self._data_ready: set[str] = set()

    def names(self) -> list[str]:
        if not self.fixtures.is_dir():
            return []
        return sorted(
            p.name
            for p in self.fixtures.iterdir()
            if p.is_dir() and ((p / "golden.ir.json").is_file() or any(p.glob("*.twbx")))
        )

    def get(self, name: str) -> Loaded:
        if name not in self.names():
            raise HTTPException(404, f"找不到報表「{name}」")
        directory = self.fixtures / name
        golden = directory / "golden.ir.json"
        stamp = golden.stat().st_mtime if golden.is_file() else 0.0
        with self._lock:
            cached = self._cache.get(name)
            if cached is None or cached.stamp != stamp:
                cached = Loaded(directory, load_fixture_ir(directory), stamp)
                self._cache[name] = cached
                self._data_ready.discard(name)
        return cached

    def database(self, name: str, datasource: str) -> Path:
        loaded = self.get(name)
        with self._lock:
            if name not in self._data_ready:
                ensure_data(loaded.directory, loaded.ir)
                self._data_ready.add(name)
        return loaded.directory / "data" / f"{datasource}.duckdb"


def create_app(
    fixtures: Path | None = None,
    web_dist: Path | None = None,
    runtime: DataRuntime | None = None,
) -> FastAPI:
    workspace = Workspace(fixtures or REPO_ROOT / "fixtures")
    dist = web_dist if web_dist is not None else REPO_ROOT / "web" / "dist"
    data = runtime or DataRuntime()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        data.close()

    app = FastAPI(title="Tableau 移轉查詢服務", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/workbooks")
    def workbooks() -> list[dict[str, Any]]:
        result = []
        for name in workspace.names():
            ir = workspace.get(name).ir
            result.append(
                {
                    "id": name,
                    "workbook": ir.workbook.id,
                    "name": ir.workbook.name,
                    "dashboards": [{"id": d.id, "name": d.name} for d in ir.dashboards],
                    "worksheets": [{"id": w.id, "name": w.name} for w in ir.worksheets],
                    "unsupported": len(ir.unsupported),
                }
            )
        return result

    @app.get("/api/workbooks/{wb}/ir")
    def workbook_ir(wb: str) -> dict[str, Any]:
        return workspace.get(wb).ir.model_dump(mode="json")

    @app.get("/api/workbooks/{wb}/cases")
    def cases(wb: str) -> list[dict[str, Any]]:
        loaded = workspace.get(wb)
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
    def package_file(wb: str, path: str) -> Response:
        """.twbx 內的圖片；只提供圖片，路徑須與封裝內的檔名完全相同（反斜線視為斜線）。"""
        loaded = workspace.get(wb)
        if Path(path).suffix.lower() not in IMAGE_TYPES:
            raise HTTPException(404, "只提供圖片檔")
        for package in sorted(loaded.directory.glob("*.twbx")):
            with zipfile.ZipFile(package) as archive:
                names = {n.replace("\\", "/"): n for n in archive.namelist()}
                if path in names:
                    media = mimetypes.guess_type(path)[0] or "application/octet-stream"
                    # 報表內的 SVG 可能含指令碼：直接開啟時也不執行
                    headers = {
                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                        "X-Content-Type-Options": "nosniff",
                    }
                    return Response(archive.read(names[path]), media_type=media, headers=headers)
        raise HTTPException(404, f"找不到檔案「{path}」")

    @app.post("/api/workbooks/{wb}/worksheets/{ws}/render")
    def render(wb: str, ws: str, state: QueryState | None = None) -> dict[str, Any]:
        loaded = workspace.get(wb)
        ir = loaded.ir
        state = state or QueryState()
        if state.today is None:
            state = state.model_copy(update={"today": as_of(loaded.directory)})
        try:
            query = worksheet_sql(ir, ws, state)
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
            pane_queries = pane_sql(ir, sheet.id, query, state)
        except QueryError as exc:
            pane_queries = {}
            notes.append(f"各標記卡無法另外查詢，以工作表的詳細程度呈現：{exc}")
        try:
            result = data.run(query, datasource, database)
            panes = {
                i: (q.columns, data.run(q, datasource, database)) for i, q in pane_queries.items()
            }
        except QueryError as exc:
            # Live 連線：下推的 SQL 轉不成來源資料庫的方言
            if not exc.unsupported:
                raise HTTPException(422, str(exc)) from exc
            return {
                "worksheet": sheet.id,
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
            ir, sheet.id, query.columns, result, query.state.parameters, panes or None
        )
        rendered.notes.extend(notes)
        fields = {f.id: f for ds in ir.datasources for f in ds.fields}
        shown = [c for c in query.columns if c.role != "sort" and c.use is not None]
        index = {key: i for i, key in enumerate(result.columns)}
        table_rows = []
        for row in result.rows:
            cells = []
            for column in shown:
                assert column.use is not None
                value = row[index[column.key]]
                use = column.use
                if use.date is not None and use.date.kind == "part" and value is not None:
                    cells.append(str(value))
                else:
                    trunc = use.date.unit if use.date is not None else None
                    tc = next((t for t in sheet.table_calcs if t.id == use.table_calc), None)
                    fmt = display_format(fields[use.field].format, tc.type if tc else None)
                    cells.append(display_value(value, column.datatype, fmt, trunc))
            table_rows.append(cells)
        return {
            "worksheet": sheet.id,
            "name": sheet.name,
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
        wb: str, field: str, request: Annotated[DomainRequest | None, Body()] = None
    ) -> dict[str, Any]:
        loaded = workspace.get(wb)
        ir = loaded.ir
        try:
            use = parse_key(field)
        except StateError as exc:
            raise HTTPException(422, str(exc)) from exc
        ds = next((d for d in ir.datasources if any(f.id == use.field for f in d.fields)), None)
        if ds is None:
            raise HTTPException(404, f"找不到欄位「{use.field}」")
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
            sql = domain_sql(ir, ds.id, use, kind)
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
    fixtures: Path, host: str = "127.0.0.1", port: int = 8000, web_dist: Path | None = None
) -> None:
    import uvicorn

    uvicorn.run(create_app(fixtures, web_dist), host=host, port=port)
