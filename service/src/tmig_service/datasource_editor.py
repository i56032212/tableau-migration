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
- 資料庫的資料源（Live 連線，桌面版的「連線到伺服器」）：``POST /datasources`` 與 ``POST …/tables``
  改送 ``connection``（連線代號）與 ``table_path``（資料表的限定名稱）或 ``sql``（自訂 SQL）；
  ``GET …/remote-tables`` 列出資料庫中的資料表，``POST …/remote-columns`` 資料表或自訂 SQL 的欄位，
  ``PUT …/custom-sql/<名稱>`` 修改自訂 SQL
- 條件（``on``）的每一邊可以是 ``{"formula": …}``：join 計算與關聯計算（Tableau 的「建立 Join 計算」
  「建立關聯計算」），公式中的欄位以顯示名稱書寫
- 跨資料庫的 join：``POST …/tables`` 的資料庫資料表另送 ``connection``（桌面版的連線代號，或資料源中已有的
  資料庫）時，與檔案或其他資料庫的資料表在本機合併；資料庫的資料源加上檔案也是。``remote-tables``、
  ``remote-columns`` 以 ``connection`` 指定資料源中的哪一個資料庫
- ``PUT …/interpreter``：``{"file": "Data/…", "enabled": …}`` 切換 Excel 檔的資料解譯器
- ``POST …/wildcard-union``：``{"table", "folder", "files", "sheets", "recursive"}`` 萬用字元聯集（資料夾中
  檔名符合模式的檔案複製進報表、接在資料表下面）
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from tmig.authoring import (
    DATA_DIR,
    Calculated,
    Condition,
    InspectedFile,
    WildcardMatch,
    add_datasource,
    add_live_datasource,
    add_live_table,
    add_table,
    add_wildcard_union,
    federate,
    inspect_file,
    join_calc_display,
    leaf_relations,
    link_calc_display,
    pivot,
    remove_table,
    rename_datasource,
    set_custom_sql,
    set_interpreter,
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
    FileColumn,
    FileConnection,
    FileTable,
    JoinRelation,
    LiveConnection,
    ModelRelation,
    PivotRelation,
    ProjectRelation,
    SqlRelation,
    TableRelation,
    UnionRelation,
    UnionWildcard,
    is_federated,
    local_tables,
    source_connections,
)
from tmig.ir.models import Relation
from tmig.package import SOURCES
from tmig_service.app import Workspace

Comparison = Literal["=", "!=", "<", "<=", ">", ">="]


class FormulaSide(BaseModel):
    """條件的一邊是公式（join 計算、關聯計算）。"""

    model_config = ConfigDict(extra="forbid")
    formula: str


SideBody = str | FormulaSide
ConditionBody = tuple[SideBody, Comparison, SideBody]


def conditions(on: list[ConditionBody]) -> list[Condition]:
    def side(value: SideBody) -> str | Calculated:
        return Calculated(value.formula) if isinstance(value, FormulaSide) else value

    return [(side(a), op, side(b)) for a, op, b in on]


class PathBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class NewDatasourceBody(BaseModel):
    """新的資料源：檔案（path、table），或資料庫的連線（connection 與 table_path 或 sql）。"""

    model_config = ConfigDict(extra="forbid")
    path: str | None = None
    table: str | None = None
    connection: str | None = None
    table_path: list[str] | None = None
    sql: str | None = None


class AddTableBody(BaseModel):
    """加入資料表：path 為作者電腦上的檔案（新的檔案），或 file 為報表中已有的檔案（Data/…）；資料庫的資料表
    為 table_path 或 sql，connection 為它的連線（省略時為資料源的資料庫）。"""

    model_config = ConfigDict(extra="forbid")
    path: str | None = None
    file: str | None = None
    table: str | None = None
    table_path: list[str] | None = None
    sql: str | None = None
    connection: str | None = None
    how: Literal["relationship", "join", "union"] = "relationship"
    target: str | None = None
    join: Literal["inner", "left", "right", "full"] = "inner"
    on: list[ConditionBody] = []


class JoinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    join: Literal["inner", "left", "right", "full"]
    on: list[ConditionBody]


class RelationshipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: list[ConditionBody]


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


class SqlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str


class InterpreterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    enabled: bool


class WildcardBody(BaseModel):
    """萬用字元聯集：資料夾（作者電腦上的完整路徑）、檔名與工作表名稱的模式、是否含子資料夾。"""

    model_config = ConfigDict(extra="forbid")
    table: str
    folder: str
    files: str
    sheets: str | None = None
    recursive: bool = False


class RemoteColumnsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: list[str] | None = None
    sql: str | None = None
    connection: str | None = None


def local_file(raw: str) -> Path:
    """作者電腦上的檔案（Esri File Geodatabase 可以是 .gdb 資料夾）。"""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, f"檔案必須是完整路徑：{raw}")
    if not path.is_file() and not (path.suffix.lower() == ".gdb" and path.is_dir()):
        raise HTTPException(404, f"找不到檔案：{path}")
    return path


def inspect(path: Path, interpreter: bool = False) -> InspectedFile:
    try:
        return inspect_file(path, interpreter)
    except EditError as exc:
        raise HTTPException(422, str(exc)) from exc


def connection_view(connection: LiveConnection) -> dict[str, Any]:
    return {
        "connection_id": connection.connection_id,
        "dbclass": connection.dbclass,
        "server": connection.server,
        "port": connection.port,
        "database": connection.database,
        "schema": connection.schema_name,
    }


def interpreted_files(ds: Datasource) -> set[str]:
    """資料源中使用資料解譯器的 Excel 檔（報表中的路徑）。"""
    connection = ds.connection
    if not isinstance(connection, FileConnection):
        return set()
    return {t.path for t in connection.tables if t.interpreted}


def copy_source(directory: Path, source: InspectedFile) -> str:
    """把檔案（與 Shapefile 的同名檔案）複製到報表目錄的 sources/Data/，回傳報表中的路徑 Data/<檔名>。
    同名但內容不同的檔案加上序號。"""
    folder = directory / SOURCES / DATA_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = source.path.name
    stem, suffix = source.path.stem, source.path.suffix
    if source.path.is_dir():
        # File Geodatabase 的資料夾：整個資料夾複製（同名時加上序號）
        n = 1
        while (folder / name).exists():
            n += 1
            name = f"{stem} ({n}){suffix}"
        shutil.copytree(source.path, folder / name)
        return f"{DATA_DIR}/{name}"
    n = 1
    while (folder / name).exists() and (folder / name).read_bytes() != source.path.read_bytes():
        n += 1
        name = f"{stem} ({n}){suffix}"
    shutil.copy2(source.path, folder / name)
    new_stem = Path(name).stem
    for sidecar in source.sidecars:
        shutil.copy2(sidecar, folder / f"{new_stem}{sidecar.suffix.lower()}")
    return f"{DATA_DIR}/{name}"


def packaged_source(directory: Path, path: str, interpreter: bool = False) -> InspectedFile:
    """報表中已有的檔案（sources/Data/…）；interpreter 時 Excel 依資料解譯器找標題列。"""
    root = (directory / SOURCES).resolve()
    found = (root / path).resolve()
    gdb = found.suffix.lower() == ".gdb" and found.is_dir()
    if not found.is_relative_to(root) or not (found.is_file() or gdb):
        raise HTTPException(
            404, f"報表中沒有檔案 {path}（以前存的報表套件沒有存入檔案時，請重新選擇檔案）"
        )
    return inspect(found, interpreter)


def model_view(ir: IR, ds: Datasource, directory: Path) -> dict[str, Any]:
    """資料源分頁的資料模型：檔案（與其中可以加入的資料表）、邏輯資料表（關聯樹）、關聯與欄位。"""
    connection = ds.connection
    live = isinstance(connection, LiveConnection) and connection.relation is not None
    editable = isinstance(connection, FileConnection) or live
    files: list[dict[str, Any]] = []
    logical: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    fields = {f.id: f for f in ds.fields}

    def link_formula(field_id: str) -> str | None:
        found = fields.get(field_id)
        return (
            link_calc_display(ds, found) if found is not None and found.relationship_calc else None
        )

    remote: dict[str, FileTable] = {}
    if isinstance(connection, FileConnection):
        root = directory / SOURCES
        interpreted = interpreted_files(ds)
        remote = {t.name: t for t in connection.tables if t.format == "live"}
        for path in dict.fromkeys(t.path for t in local_tables(connection)):
            entry: dict[str, Any] = {
                "path": path,
                "name": Path(path).name,
                "available": False,
                "interpreted": path in interpreted,
            }
            local = root / path
            if local.is_file() or (local.suffix.lower() == ".gdb" and local.is_dir()):
                try:
                    found = inspect_file(local, path in interpreted)
                    entry.update(
                        available=True,
                        format=found.format,
                        tables=[t.to_json() for t in found.tables],
                    )
                except EditError as exc:
                    entry["error"] = str(exc)
            files.append(entry)
    if isinstance(connection, FileConnection | LiveConnection) and connection.relation is not None:
        top = connection.relation
        if isinstance(top, ModelRelation):
            for table in top.tables:
                logical.append(
                    {
                        "id": table.id,
                        "caption": table.caption,
                        "relation": relation_view(
                            table.relation,
                            lambda text, owner=table.id: join_calc_display(ds, owner, text),
                            remote,
                        ),
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
                                # 關聯計算：以顯示名稱參照欄位的公式
                                "left_formula": link_formula(c.left),
                                "right_formula": link_formula(c.right),
                            }
                            for c in rel.on
                        ],
                    }
                )
        else:
            logical.append(
                {
                    "id": None,
                    "caption": ds.caption,
                    "relation": relation_view(
                        top, lambda text: join_calc_display(ds, None, text), remote
                    ),
                }
            )
    server = connection_view(connection) if isinstance(connection, LiveConnection) else None
    return {
        "datasource": {
            "id": ds.id,
            "caption": ds.caption,
            "kind": connection.kind,
            "federated": is_federated(connection),
        },
        "editable": editable,
        "connection": server,
        # 資料庫的連線：Live 連線本身，或跨資料庫的 join 中的各資料庫
        "connections": [connection_view(c) for c in source_connections(connection)]
        if editable
        else [],
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
            if not f.relationship_calc
        ],
    }


def relation_view(
    relation: Relation,
    display: Callable[[str], str] | None = None,
    remote: dict[str, FileTable] | None = None,
) -> dict[str, Any]:
    """關聯樹；display 把 join 計算的公式改寫成以顯示名稱參照欄位（省略時原樣）。remote：跨資料庫的 join 中
    資料庫的資料表（資料表與自訂 SQL 依原本的樣子呈現，另有所在的連線）。"""
    shown = display or (lambda text: text)
    if isinstance(relation, TableRelation):
        found = (remote or {}).get(relation.name)
        if found is not None and found.connection is not None:
            connection = found.connection.connection_id
            if found.sql is not None:
                return {
                    "kind": "sql",
                    "name": relation.name,
                    "sql": found.sql,
                    "connection": connection,
                }
            return {"kind": "table", "name": relation.name, "connection": connection}
    if isinstance(relation, JoinRelation):
        return {
            "kind": "join",
            "join": relation.join,
            "left": relation_view(relation.left, display, remote),
            "right": relation_view(relation.right, display, remote),
            "on": [
                {
                    "left": [c.left.table, c.left.column],
                    "op": c.op,
                    "right": [c.right.table, c.right.column],
                    "left_formula": shown(c.left.formula) if c.left.formula else None,
                    "right_formula": shown(c.right.formula) if c.right.formula else None,
                }
                for c in relation.on
            ],
        }
    if isinstance(relation, UnionRelation):
        return {
            "kind": "union",
            "name": relation.name,
            "parts": [relation_view(p.relation, display, remote) for p in relation.parts],
            "wildcard": relation.wildcard.model_dump(mode="json") if relation.wildcard else None,
        }
    if isinstance(relation, PivotRelation):
        return {
            "kind": "pivot",
            "name": relation.name,
            "source": relation_view(relation.source, display, remote),
            "columns": [c.column for c in relation.columns],
        }
    if isinstance(relation, ProjectRelation):
        return {
            "kind": "project",
            "name": relation.name,
            "source": relation_view(relation.source, display, remote),
        }
    if isinstance(relation, SqlRelation):
        return {"kind": "sql", "name": relation.name, "sql": relation.sql}
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
            # 跨資料庫的 join 只有資料庫的資料表時沒有要匯入的檔案
            results = import_sources(directory / SOURCES, directory / "data", ir, only=[datasource])
        except (HyperImportError, KeyError, OSError) as exc:
            raise HTTPException(422, f"匯入資料失敗：{exc}") from exc
        for result in results:
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
        created: dict[str, str] = {}
        if body.connection is not None:
            connection = desktop_connection(request, body.connection)
            columns = remote_columns(request, connection, body.table_path, body.sql)

            def change(ir: IR) -> IR:
                updated, ds = add_live_datasource(
                    ir, connection, body.table_path, body.sql, columns
                )
                created["id"] = ds.id
                return updated

        else:
            if not body.path:
                raise HTTPException(422, "要指定檔案（path）或資料庫的連線（connection）")
            source = inspect(local_file(body.path))
            package_path = copy_source(loaded.directory, source)

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
        ds = next((d for d in loaded.ir.datasources if d.id == datasource), None)
        if ds is None:
            raise HTTPException(404, f"找不到資料源 {datasource}")
        current = ds.connection
        if body.table_path is not None or body.sql is not None:
            # 資料庫的資料表：同一個資料庫，或跨資料庫的 join（檔案的資料源、另一個資料庫）
            connection = resolve_connection(request, ds, body.connection)
            columns = remote_columns(request, connection, body.table_path, body.sql)
            other = not (
                isinstance(current, LiveConnection)
                and current.connection_id == connection.connection_id
            )
            columns_of = federated_columns(request, ds) if other else None

            def change(ir: IR) -> IR:
                if columns_of is not None:
                    ir = federate(ir, datasource, columns_of)
                return add_live_table(
                    ir,
                    datasource,
                    body.table_path,
                    body.sql,
                    columns,
                    body.how,
                    body.target,
                    body.join,
                    conditions(body.on),
                    connection if other else None,
                )

            ir = apply(request, workbook, datasource, change)
            return model_view(
                ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
            )
        if body.path:
            source = inspect(local_file(body.path))
            package_path = copy_source(loaded.directory, source)
        elif body.file:
            source = packaged_source(
                loaded.directory, body.file, body.file in interpreted_files(ds)
            )
            package_path = body.file
        else:
            raise HTTPException(422, "要指定檔案（path 或 file）")
        # 資料庫的資料源加上檔案：先改成跨資料庫的 join（各資料表的欄從資料庫取得）
        columns_of = federated_columns(request, ds) if isinstance(current, LiveConnection) else None

        def add_file(ir: IR) -> IR:
            if columns_of is not None:
                ir = federate(ir, datasource, columns_of)
            return add_table(
                ir,
                datasource,
                source,
                body.table,
                package_path,
                body.how,
                body.target,
                body.join,
                conditions(body.on),
            )

        ir = apply(request, workbook, datasource, add_file)
        ds = next(d for d in ir.datasources if d.id == datasource)
        return model_view(ir, ds, loaded.directory)

    @router.put("/{workbook}/datasources/{datasource}/interpreter")
    def interpreter_endpoint(
        workbook: str, datasource: str, body: InterpreterBody, request: Request
    ) -> dict[str, Any]:
        """切換 Excel 檔的資料解譯器：依新的設定重新檢查報表中的檔案，資料表的欄換成新的欄。"""
        loaded = workspace(request).get(workbook)
        source = packaged_source(loaded.directory, body.file, body.enabled)
        ir = apply(
            request,
            workbook,
            datasource,
            lambda ir: set_interpreter(ir, datasource, body.file, source),
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.post("/{workbook}/datasources/{datasource}/wildcard-union")
    def wildcard_endpoint(
        workbook: str, datasource: str, body: WildcardBody, request: Request
    ) -> dict[str, Any]:
        """萬用字元聯集：資料夾中檔名（與工作表名稱）符合模式的資料表複製進報表、接在資料表下面。"""
        loaded = workspace(request).get(workbook)
        ds = next((d for d in loaded.ir.datasources if d.id == datasource), None)
        if ds is None or not isinstance(ds.connection, FileConnection):
            raise HTTPException(404, f"找不到檔案的資料源 {datasource}")
        target = next((t for t in ds.connection.tables if t.name == body.table), None)
        if target is None:
            # 已經是聯集：以聯集中的第一張資料表為準
            target = next(
                (
                    t
                    for t in ds.connection.tables
                    if _in_union(ds.connection.relation, body.table, t)
                ),
                None,
            )
        if target is None:
            raise HTTPException(404, f"找不到資料表「{body.table}」")
        matches, original = wildcard_matches(body, target)
        copied = [
            WildcardMatch(m.source, m.table, copy_source(loaded.directory, m.source), m.original)
            for m in matches
        ]
        wildcard = UnionWildcard(
            files=body.files.strip(),
            sheets=(body.sheets or "").strip() or None,
            recursive=body.recursive,
        )
        ir = apply(
            request,
            workbook,
            datasource,
            lambda ir: add_wildcard_union(ir, datasource, body.table, copied, wildcard, original),
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    @router.get("/{workbook}/datasources/{datasource}/remote-tables")
    def remote_tables_endpoint(
        workbook: str,
        datasource: str,
        request: Request,
        schema: str | None = None,
        connection_key: Annotated[str | None, Query(alias="connection")] = None,
    ) -> dict[str, Any]:
        """資料庫中可以加入的資料表（schema 省略時為連線預設的結構描述）；connection：資料源中的哪一個資料庫
        （跨資料庫的 join），或桌面版的另一個連線。"""
        connection = resolve_connection(
            request, find_datasource(request, workbook, datasource), connection_key
        )
        return {"tables": list_remote_tables(request, connection, schema)}

    @router.post("/{workbook}/datasources/{datasource}/remote-columns")
    def remote_columns_endpoint(
        workbook: str, datasource: str, body: RemoteColumnsBody, request: Request
    ) -> dict[str, Any]:
        """資料表或自訂 SQL 的欄位（加入資料表前選擇關聯、join 的欄位）。"""
        connection = resolve_connection(
            request, find_datasource(request, workbook, datasource), body.connection
        )
        columns = remote_columns(request, connection, body.table, body.sql)
        return {"columns": [c.model_dump(mode="json") for c in columns]}

    @router.put("/{workbook}/datasources/{datasource}/custom-sql/{name}")
    def custom_sql_endpoint(
        workbook: str, datasource: str, name: str, body: SqlBody, request: Request
    ) -> dict[str, Any]:
        loaded = workspace(request).get(workbook)
        ds = find_datasource(request, workbook, datasource)
        connection = custom_sql_connection(ds, name)
        columns = remote_columns(request, connection, None, body.sql)
        ir = apply(
            request,
            workbook,
            datasource,
            lambda ir: set_custom_sql(ir, datasource, name, body.sql, columns),
        )
        return model_view(
            ir, next(d for d in ir.datasources if d.id == datasource), loaded.directory
        )

    def find_datasource(request: Request, workbook: str, datasource: str) -> Datasource:
        loaded = workspace(request).get(workbook)
        ds = next((d for d in loaded.ir.datasources if d.id == datasource), None)
        if ds is None:
            raise HTTPException(404, f"找不到資料源 {datasource}")
        return ds

    def federated_columns(request: Request, ds: Datasource) -> dict[str, list[FileColumn]]:
        """資料庫的資料源改成跨資料庫的 join 時各資料表的欄（從資料庫取得；取不到時取自資料源的欄位）。"""
        connection = ds.connection
        if not isinstance(connection, LiveConnection) or connection.relation is None:
            return {}
        found: dict[str, list[FileColumn]] = {}
        for leaf in leaf_relations(connection.relation):
            table = list(leaf.table) if isinstance(leaf, TableRelation) else None
            sql = leaf.sql if isinstance(leaf, SqlRelation) else None
            try:
                found[leaf.name] = remote_columns(request, connection, table, sql)
            except HTTPException:
                continue
        return found

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
            lambda ir: set_join(ir, datasource, table, body.join, conditions(body.on)),
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
            lambda ir: set_relationship(ir, datasource, logical, conditions(body.on)),
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


def _in_union(relation: Any, name: str, table: Any) -> bool:
    """name 是含有 table 的聯集（萬用字元聯集再加入時，以聯集的名稱指定）。"""
    if isinstance(relation, ModelRelation):
        return any(_in_union(t.relation, name, table) for t in relation.tables)
    if isinstance(relation, UnionRelation):
        return relation.name == name and any(p.relation.name == table.name for p in relation.parts)
    return False


def wildcard_matches(body: WildcardBody, target: Any) -> tuple[list[WildcardMatch], str | None]:
    """資料夾中符合模式的資料表（不含目標資料表本身的檔案與工作表）與目標檔案在資料夾中的路徑。"""
    from tmig.parser.wildcard import matches

    folder = Path(body.folder).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise HTTPException(422, f"資料夾必須是完整路徑且存在：{body.folder}")
    pattern = body.files.strip()
    if not pattern:
        raise HTTPException(422, "要輸入檔名的模式，例如 Sales *.xlsx")
    paths = sorted(
        folder.rglob("*") if body.recursive else folder.iterdir(), key=lambda p: str(p).casefold()
    )
    excel = target.format == "excel"
    suffixes = (".xlsx", ".xlsm", ".xls") if excel else (".csv", ".tsv", ".txt", ".tab")
    own_name = Path(target.path).name
    original: str | None = None
    found: list[WildcardMatch] = []
    for path in paths:
        if (
            not path.is_file()
            or path.suffix.lower() not in suffixes
            or not matches(pattern, path.name)
        ):
            continue
        # 目標的檔案使用資料解譯器時，接上的檔案也使用
        source = inspect(path, bool(getattr(target, "interpreted", False)))
        for table in source.tables:
            if excel and not matches(body.sheets, table.sheet or ""):
                continue
            if path.name == own_name and (not excel or table.sheet == target.sheet):
                original = str(path)  # 目標資料表本身
                continue
            found.append(WildcardMatch(source, table.name, "", str(path)))
    if not found:
        raise HTTPException(422, f"「{folder}」中沒有其他符合「{pattern}」的檔案")
    return found, original


def resolve_connection(request: Request, ds: Datasource, key: str | None) -> LiveConnection:
    """資料庫的連線：省略時為資料源的 Live 連線；資料源中已有的資料庫（跨資料庫的 join），或桌面版的連線。"""
    connection = ds.connection
    known = source_connections(connection)
    if key is None:
        if isinstance(connection, LiveConnection):
            return connection.model_copy(update={"relation": None})
        raise HTTPException(422, f"資料源「{ds.caption}」不是資料庫的連線，要指定連線")
    found = next((c for c in known if c.connection_id == key), None)
    if found is not None:
        return found.model_copy(update={"relation": None})
    return desktop_connection(request, key)


def custom_sql_connection(ds: Datasource, name: str) -> LiveConnection:
    """自訂 SQL 所在的連線：資料庫的資料源本身，或跨資料庫的 join 中那一段自訂 SQL 的連線。"""
    connection = ds.connection
    if isinstance(connection, LiveConnection):
        return connection
    if isinstance(connection, FileConnection):
        found = next((t for t in connection.tables if t.name == name and t.sql is not None), None)
        if found is not None and found.connection is not None:
            return found.connection
    raise HTTPException(422, f"「{name}」不是資料庫的自訂 SQL")


def desktop_connection(request: Request, key: str) -> LiveConnection:
    """桌面版的連線（連線對話框建立的）：沒有資料表的 LiveConnection。"""
    registry = getattr(request.app.state, "connections", None)
    if registry is None:
        raise HTTPException(404, "只有桌面版可以連線到資料庫")
    return registry.connection(key)


def remote_columns(
    request: Request, connection: LiveConnection, table: list[str] | None, sql: str | None
) -> Any:
    from tmig_service.desktop_connections import remote_columns as columns

    return columns(request, connection, table, sql)


def list_remote_tables(
    request: Request, connection: LiveConnection, schema: str | None
) -> list[dict[str, Any]]:
    from tmig.data.catalog import CatalogError, list_tables
    from tmig.data.sources import SourceConnectionError

    runtime = request.app.state.runtime
    dialect = connection.dialect
    if dialect is None:
        raise HTTPException(422, f"不支援的連線類型 {connection.dbclass}")

    class _Fetcher:
        def fetch(self, sql: str) -> Any:
            return runtime.fetch(connection, sql)

    try:
        found = list_tables(
            _Fetcher(), dialect, connection.database, schema or connection.schema_name
        )
    except CatalogError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SourceConnectionError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"無法列出資料表：{exc}") from exc
    return [t.to_json() for t in found]
