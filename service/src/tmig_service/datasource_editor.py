"""桌面版的資料源編輯（Tableau Desktop 的「連線到資料」與「資料來源」分頁）：檢查檔案、新增資料源、
加入資料表（關聯、join、聯集）、樞紐與欄位的設定。

只在桌面版（本機引擎）啟用：選取的檔案（作者電腦上的完整路徑）複製到報表目錄的 sources/Data/，IR 中記
Data/<檔名>（與 .twbx 相同的寫法），匯入後與 .twbx 內的檔案相同在本機查詢；存成報表套件時檔案一起存入。
每次變更後重新匯入那個資料源（DuckDB 檔），查詢立即使用新的資料。

API（都在 /api/edit/<報表> 下）：

- ``POST /inspect-file``：``{"path": …}`` → 檔案中的資料表、欄名、型別與列數
- ``POST /datasources``：``{"path": …, "table": …}`` → 新增資料源
- ``GET  /datasources/<資料源>/model``：資料模型（檔案、邏輯資料表、join、聯集、樞紐、關聯與欄位）
- ``POST /datasources/<資料源>/tables``：加入資料表（``how``：relationship、join、union）
- ``DELETE /datasources/<資料源>/tables/<名稱>``：移除資料表
- ``PUT  /datasources/<資料源>/joins/<資料表>``、``PUT /datasources/<資料源>/relationships/<邏輯資料表>``
- ``POST /datasources/<資料源>/pivot``、``DELETE /datasources/<資料源>/pivot/<名稱>``
- ``PUT  /datasources/<資料源>/fields/<欄位>``：顯示名稱、隱藏、角色、預設彙總、資料類型
- ``PUT  /datasources/<資料源>``：資料源的名稱
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from tmig.authoring import (
    DATA_DIR,
    InspectedFile,
    add_datasource,
    add_table,
    inspect_file,
    pivot,
    remove_table,
    rename_datasource,
    set_join,
    set_relationship,
    unpivot,
    update_field,
)
from tmig.data.hyper_import import HyperImportError, import_sources
from tmig.edit import EditError
from tmig.ir import (
    IR,
    Datasource,
    FileConnection,
    JoinRelation,
    ModelRelation,
    PivotRelation,
    ProjectRelation,
    UnionRelation,
)
from tmig.ir.models import Relation
from tmig.package import SOURCES
from tmig_service.app import Workspace

Comparison = Literal["=", "!=", "<", "<=", ">", ">="]


class PathBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class NewDatasourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    table: str | None = None


class AddTableBody(BaseModel):
    """加入資料表：path 為作者電腦上的檔案（新的檔案），或 file 為報表中已有的檔案（Data/…）。"""

    model_config = ConfigDict(extra="forbid")
    path: str | None = None
    file: str | None = None
    table: str | None = None
    how: Literal["relationship", "join", "union"] = "relationship"
    target: str | None = None
    join: Literal["inner", "left", "right", "full"] = "inner"
    on: list[tuple[str, Comparison, str]] = []


class JoinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    join: Literal["inner", "left", "right", "full"]
    on: list[tuple[str, Comparison, str]]


class RelationshipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: list[tuple[str, Comparison, str]]


class PivotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    columns: list[str]


class FieldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str | None = None
    hidden: bool | None = None
    role: Literal["dimension", "measure"] | None = None
    default_agg: str | None = None
    datatype: Literal["string", "integer", "real", "boolean", "date", "datetime"] | None = None


class CaptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str


def local_file(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, f"檔案必須是完整路徑：{raw}")
    if not path.is_file():
        raise HTTPException(404, f"找不到檔案：{path}")
    return path


def inspect(path: Path) -> InspectedFile:
    try:
        return inspect_file(path)
    except EditError as exc:
        raise HTTPException(422, str(exc)) from exc


def copy_source(directory: Path, source: InspectedFile) -> str:
    """把檔案（與 Shapefile 的同名檔案）複製到報表目錄的 sources/Data/，回傳報表中的路徑 Data/<檔名>。
    同名但內容不同的檔案加上序號。"""
    folder = directory / SOURCES / DATA_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = source.path.name
    stem, suffix = source.path.stem, source.path.suffix
    n = 1
    while (folder / name).exists() and (folder / name).read_bytes() != source.path.read_bytes():
        n += 1
        name = f"{stem} ({n}){suffix}"
    shutil.copy2(source.path, folder / name)
    new_stem = Path(name).stem
    for sidecar in source.sidecars:
        shutil.copy2(sidecar, folder / f"{new_stem}{sidecar.suffix.lower()}")
    return f"{DATA_DIR}/{name}"


def packaged_source(directory: Path, path: str) -> InspectedFile:
    """報表中已有的檔案（sources/Data/…）。"""
    root = (directory / SOURCES).resolve()
    found = (root / path).resolve()
    if not found.is_relative_to(root) or not found.is_file():
        raise HTTPException(
            404, f"報表中沒有檔案 {path}（以前存的報表套件沒有存入檔案時，請重新選擇檔案）"
        )
    return inspect(found)


def model_view(ir: IR, ds: Datasource, directory: Path) -> dict[str, Any]:
    """資料源分頁的資料模型：檔案（與其中可以加入的資料表）、邏輯資料表（關聯樹）、關聯與欄位。"""
    connection = ds.connection
    editable = isinstance(connection, FileConnection)
    files: list[dict[str, Any]] = []
    logical: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    fields = {f.id: f for f in ds.fields}
    if isinstance(connection, FileConnection):
        root = directory / SOURCES
        for path in dict.fromkeys(t.path for t in connection.tables):
            entry: dict[str, Any] = {"path": path, "name": Path(path).name, "available": False}
            local = root / path
            if local.is_file():
                try:
                    found = inspect_file(local)
                    entry.update(
                        available=True,
                        format=found.format,
                        tables=[t.to_json() for t in found.tables],
                    )
                except EditError as exc:
                    entry["error"] = str(exc)
            files.append(entry)
        top = connection.relation
        if isinstance(top, ModelRelation):
            for table in top.tables:
                logical.append(
                    {
                        "id": table.id,
                        "caption": table.caption,
                        "relation": relation_view(table.relation),
                    }
                )
            for rel in top.relationships:
                relationships.append(
                    {
                        "left": rel.left,
                        "right": rel.right,
                        "on": [
                            {
                                "left": c.left,
                                "op": c.op,
                                "right": c.right,
                                "left_caption": fields[c.left].caption
                                if c.left in fields
                                else c.left,
                                "right_caption": fields[c.right].caption
                                if c.right in fields
                                else c.right,
                            }
                            for c in rel.on
                        ],
                    }
                )
        else:
            assert top is not None
            logical.append({"id": None, "caption": ds.caption, "relation": relation_view(top)})
    return {
        "datasource": {"id": ds.id, "caption": ds.caption, "kind": connection.kind},
        "editable": editable,
        "files": files,
        "logical": logical,
        "relationships": relationships,
        "fields": [
            {
                "id": f.id,
                "caption": f.caption,
                "column": f.column,
                "table": f.table,
                "physical_table": f.physical_table,
                "datatype": f.datatype,
                "role": f.role,
                "default_agg": f.default_agg,
                "hidden": f.hidden,
                "calc": f.calc.formula if f.calc is not None else None,
            }
            for f in ds.fields
        ],
    }


def relation_view(relation: Relation) -> dict[str, Any]:
    if isinstance(relation, JoinRelation):
        return {
            "kind": "join",
            "join": relation.join,
            "left": relation_view(relation.left),
            "right": relation_view(relation.right),
            "on": [
                {
                    "left": [c.left.table, c.left.column],
                    "op": c.op,
                    "right": [c.right.table, c.right.column],
                }
                for c in relation.on
            ],
        }
    if isinstance(relation, UnionRelation):
        return {
            "kind": "union",
            "name": relation.name,
            "parts": [relation_view(p.relation) for p in relation.parts],
        }
    if isinstance(relation, PivotRelation):
        return {
            "kind": "pivot",
            "name": relation.name,
            "source": relation_view(relation.source),
            "columns": [c.column for c in relation.columns],
        }
    if isinstance(relation, ProjectRelation):
        return {"kind": "project", "name": relation.name, "source": relation_view(relation.source)}
    return {"kind": relation.kind, "name": relation.name}


def datasource_editor_router(on_change: Callable[[str], None] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/edit")

    def workspace(request: Request) -> Workspace:
        return request.app.state.workspace

    def apply(
        request: Request, workbook: str, datasource: str | None, change: Callable[[IR], IR]
    ) -> IR:
        """套用變更、寫回報表，並重新匯入資料源（DuckDB 檔）。"""
        space = workspace(request)
        loaded = space.get(workbook)
        try:
            ir = change(loaded.ir)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        if datasource is not None:
            reimport(loaded.directory, ir, datasource)
        space.save_ir(workbook, ir)
        if on_change is not None:
            on_change(workbook)
        return ir

    def reimport(directory: Path, ir: IR, datasource: str) -> None:
        ds = next((d for d in ir.datasources if d.id == datasource), None)
        if ds is None or not isinstance(ds.connection, FileConnection):
            return
        try:
            (result,) = import_sources(
                directory / SOURCES, directory / "data", ir, only=[datasource]
            )
        except (HyperImportError, KeyError, OSError) as exc:
            raise HTTPException(422, f"匯入資料失敗：{exc}") from exc
        if not result.ok:
            missing = "、".join(result.missing_columns) or "自檢不通過"
            raise HTTPException(422, f"匯入資料失敗：{missing}")

    @router.post("/{workbook}/inspect-file")
    def inspect_endpoint(workbook: str, body: PathBody, request: Request) -> dict[str, Any]:
        workspace(request).get(workbook)
        return inspect(local_file(body.path)).to_json()

    @router.post("/{workbook}/datasources")
    def new_datasource(workbook: str, body: NewDatasourceBody, request: Request) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        source = inspect(local_file(body.path))
        package_path = copy_source(loaded.directory, source)
        created: dict[str, str] = {}

        def change(ir: IR) -> IR:
            updated, ds = add_datasource(ir, source, body.table, package_path)
            created["id"] = ds.id
            return updated

        space = workspace(request)
        try:
            ir = change(loaded.ir)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        reimport(loaded.directory, ir, created["id"])
        space.save_ir(workbook, ir)
        if on_change is not None:
            on_change(workbook)
        return {"datasource": created["id"]}

    @router.get("/{workbook}/datasources/{datasource}/model")
    def get_model(workbook: str, datasource: str, request: Request) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ds = next((d for d in loaded.ir.datasources if d.id == datasource), None)
        if ds is None:
            raise HTTPException(404, f"找不到資料源 {datasource}")
        return model_view(loaded.ir, ds, loaded.directory)

    @router.post("/{workbook}/datasources/{datasource}/tables")
    def add_table_endpoint(
        workbook: str, datasource: str, body: AddTableBody, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        if body.path:
            source = inspect(local_file(body.path))
            package_path = copy_source(loaded.directory, source)
        elif body.file:
            source = packaged_source(loaded.directory, body.file)
            package_path = body.file
        else:
            raise HTTPException(422, "要指定檔案（path 或 file）")
        ir = apply(
            request,
            workbook,
            datasource,
            lambda ir: add_table(
                ir,
                datasource,
                source,
                body.table,
                package_path,
                body.how,
                body.target,
                body.join,
                list(body.on),
            ),
        )
        ds = next(d for d in ir.datasources if d.id == datasource)
        return model_view(ir, ds, loaded.directory)

    @router.delete("/{workbook}/datasources/{datasource}/tables/{table}")
    def remove_table_endpoint(
        workbook: str, datasource: str, table: str, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ir = apply(request, workbook, datasource, lambda ir: remove_table(ir, datasource, table))
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.put("/{workbook}/datasources/{datasource}/joins/{table}")
    def set_join_endpoint(
        workbook: str, datasource: str, table: str, body: JoinBody, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ir = apply(
            request,
            workbook,
            datasource,
            lambda ir: set_join(ir, datasource, table, body.join, list(body.on)),
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.put("/{workbook}/datasources/{datasource}/relationships/{logical}")
    def set_relationship_endpoint(
        workbook: str, datasource: str, logical: str, body: RelationshipBody, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ir = apply(
            request,
            workbook,
            None,
            lambda ir: set_relationship(ir, datasource, logical, list(body.on)),
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.post("/{workbook}/datasources/{datasource}/pivot")
    def pivot_endpoint(
        workbook: str, datasource: str, body: PivotBody, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ir = apply(
            request, workbook, None, lambda ir: pivot(ir, datasource, body.table, body.columns)
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.delete("/{workbook}/datasources/{datasource}/pivot/{name}")
    def unpivot_endpoint(
        workbook: str, datasource: str, name: str, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ir = apply(request, workbook, None, lambda ir: unpivot(ir, datasource, name))
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.put("/{workbook}/datasources/{datasource}/fields/{field}")
    def field_endpoint(
        workbook: str, datasource: str, field: str, body: FieldBody, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        reimport_needed = body.datatype is not None
        ir = apply(
            request,
            workbook,
            datasource if reimport_needed else None,
            lambda ir: update_field(
                ir,
                datasource,
                field,
                caption=body.caption,
                hidden=body.hidden,
                role=body.role,
                default_agg=body.default_agg,
                datatype=body.datatype,
            ),
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.put("/{workbook}/datasources/{datasource}")
    def rename_endpoint(
        workbook: str, datasource: str, body: CaptionBody, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ir = apply(
            request, workbook, None, lambda ir: rename_datasource(ir, datasource, body.caption)
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    return router


__all__ = ["copy_source", "datasource_editor_router", "inspect", "local_file", "model_view"]
