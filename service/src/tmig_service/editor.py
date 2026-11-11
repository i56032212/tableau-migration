"""桌面版編輯器的 API：資料窗格、工作表的新增／修改／複製／刪除、計算欄位與公式檢查。

只在桌面版（本機引擎）啟用：變更寫回開啟中報表的 golden.ir.json（工作區中的複本，存檔時才寫成報表套件），
檢視器的查詢立即使用新的 IR。每次修改工作表都送整張工作表（編輯器自己做復原、重做），引擎檢查後換掉。
資料源的階層（drill-path）在資料窗格中建立、改名、改變欄位與刪除。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from tmig.data.executor import ExecutionError
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError
from tmig.edit import (
    EditError,
    add_dashboard,
    add_story,
    add_worksheet,
    catalog,
    check_formula,
    duplicate_dashboard,
    duplicate_worksheet,
    new_id,
    outline,
    remove_action,
    remove_calc,
    remove_dashboard,
    remove_hierarchy,
    remove_parameter,
    remove_story,
    remove_worksheet,
    replace_dashboard,
    replace_story,
    replace_worksheet,
    upsert_action,
    upsert_calc,
    upsert_hierarchy,
    upsert_parameter,
)
from tmig.ir import FieldUse
from tmig.planner.state import QueryState, use_key
from tmig.sqlgen.query import QueryError, custom_sql_parameters, domain_sql, worksheet_sql
from tmig.verify.cases import CasesError, load_cases
from tmig_service.app import Workspace, current_user


class WorksheetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worksheet: dict[str, Any]


class NewWorksheetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    datasource: str | None = None


class FormulaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource: str
    formula: str
    field: str | None = None


class CalcBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource: str
    caption: str
    formula: str
    field: str | None = None


class DomainBody(BaseModel):
    """篩選對話框的可選值或範圍：維度為資料源中的值；量值（彙總或 Table Calculation）為工作表的詳細程度上
    各標記的最小值與最大值（與 Tableau 的量值篩選相同），計算時不套用正在編輯的篩選。"""

    model_config = ConfigDict(extra="forbid")
    use: dict[str, Any]
    kind: Literal["values", "range"] | None = None
    worksheet: str | None = None
    exclude_filter: str | None = None


class HierarchyBody(BaseModel):
    """階層：名稱與由上到下的欄位 id；original 為原本的名稱（改變既有的階層）。"""

    model_config = ConfigDict(extra="forbid")
    name: str
    fields: list[str]
    original: str | None = None


class ParameterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameter: dict[str, Any]


class DashboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dashboard: dict[str, Any]


class NewDashboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    width: int = 1000
    height: int = 800


class StoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    story: dict[str, Any]


class NamedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: dict[str, Any]


class IdBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str
    text: str


def editor_router(on_change: Callable[[str], None] | None = None) -> APIRouter:
    """on_change：報表改過之後呼叫（桌面版記下尚未儲存的變更）。"""
    router = APIRouter(prefix="/api/edit")

    def workspace(request: Request) -> Workspace:
        return request.app.state.workspace

    def edit(request: Request, workbook: str, change: Any) -> Any:
        """套用變更（回傳 (新的 IR, 結果) 或新的 IR），寫回報表；內容不正確時回應 422。"""
        space = workspace(request)
        try:
            result = change(space.get(workbook).ir)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        ir, value = result if isinstance(result, tuple) else (result, None)
        space.save_ir(workbook, ir)
        if on_change is not None:
            on_change(workbook)
        return value

    @router.get("/{workbook}/catalog")
    def get_catalog(workbook: str, request: Request) -> dict[str, Any]:
        return catalog(workspace(request).get(workbook).ir)

    @router.get("/{workbook}/worksheets/{worksheet}")
    def get_worksheet(workbook: str, worksheet: str, request: Request) -> dict[str, Any]:
        """完整的工作表定義（編輯器以它為起點）。"""
        ir = workspace(request).get(workbook).ir
        sheet = next((w for w in ir.worksheets if w.id == worksheet), None)
        if sheet is None:
            raise HTTPException(404, f"找不到工作表「{worksheet}」")
        return {"worksheet": sheet.model_dump(mode="json")}

    @router.post("/{workbook}/domain")
    def post_domain(workbook: str, body: DomainBody, request: Request) -> dict[str, Any]:
        space = workspace(request)
        runtime: DataRuntime = request.app.state.runtime
        loaded = space.get(workbook)
        ir = loaded.ir
        try:
            use = FieldUse.model_validate(body.use)
        except ValidationError as exc:
            raise HTTPException(422, "欄位的設定不正確") from exc
        ds = next((d for d in ir.datasources if any(f.id == use.field for f in d.fields)), None)
        if ds is None:
            raise HTTPException(404, f"找不到欄位「{use.field}」")
        field = next(f for f in ds.fields if f.id == use.field)
        level = field.calc.level if field.calc is not None else None
        measure = use.agg is not None or use.table_calc is not None
        measure = measure or level in ("aggregate", "table_calc")
        user = current_user(request)

        def database() -> Path:
            return space.database(workbook, ds.id)

        try:
            if not measure:
                kind = body.kind or (
                    "range"
                    if use.date is None
                    and (
                        field.datatype in ("date", "datetime")
                        or (field.role == "measure" and field.datatype in ("integer", "real"))
                    )
                    else "values"
                )
                sql = domain_sql(ir, ds.id, use, kind, user)
                result = runtime.run_sql(sql, ds, database, custom_sql_parameters(ir))
                if kind == "range":
                    low, high = result.rows[0] if result.rows else (None, None)
                    return {"kind": "range", "min": _json(low), "max": _json(high)}
                return {"kind": "values", "values": [_json(r[0]) for r in result.rows]}
            if body.worksheet is None:
                raise HTTPException(422, "量值的範圍需要指定工作表")
            sheet = next((w for w in ir.worksheets if w.id == body.worksheet), None)
            if sheet is None:
                raise HTTPException(404, f"找不到工作表「{body.worksheet}」")
            # 不套用正在編輯的篩選；量值不在工作表上時放到工具提示（不改變詳細程度）
            filters = [f for f in sheet.filters if f.id != body.exclude_filter]
            key = use_key(use)
            marks = sheet.marks
            if all(use_key(u) != key for u in [*sheet.rows, *sheet.columns, *marks.uses()]):
                marks = marks.model_copy(update={"tooltip": [*marks.tooltip, use]})
            probe = sheet.model_copy(update={"filters": filters, "marks": marks})
            probe_ir, _ = replace_worksheet(ir, probe)
            query = worksheet_sql(
                probe_ir, sheet.id, QueryState(today=_as_of(loaded.directory)), user
            )
            result = runtime.run(query, ds, database)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        except QueryError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(500, str(exc)) from exc
        index = next((i for i, c in enumerate(query.columns) if c.key == key), None)
        values = [
            row[index]
            for row in result.rows
            if index is not None
            and row[index] is not None
            and not (isinstance(row[index], float) and not math.isfinite(row[index]))
        ]
        if not values:
            return {"kind": "range", "min": None, "max": None}
        return {"kind": "range", "min": _json(min(values)), "max": _json(max(values))}

    @router.put("/{workbook}/worksheets/{worksheet}")
    def put_worksheet(
        workbook: str, worksheet: str, body: WorksheetBody, request: Request
    ) -> dict[str, Any]:
        if body.worksheet.get("id") != worksheet:
            raise HTTPException(422, "工作表的 id 與網址不符")
        sheet = edit(request, workbook, lambda ir: replace_worksheet(ir, body.worksheet))
        return {"worksheet": sheet.model_dump(mode="json")}

    @router.post("/{workbook}/worksheets")
    def post_worksheet(workbook: str, body: NewWorksheetBody, request: Request) -> dict[str, Any]:
        sheet = edit(request, workbook, lambda ir: add_worksheet(ir, body.name, body.datasource))
        return {"worksheet": sheet.model_dump(mode="json")}

    @router.post("/{workbook}/worksheets/{worksheet}/duplicate")
    def post_duplicate(workbook: str, worksheet: str, request: Request) -> dict[str, Any]:
        sheet = edit(request, workbook, lambda ir: duplicate_worksheet(ir, worksheet))
        return {"worksheet": sheet.model_dump(mode="json")}

    @router.delete("/{workbook}/worksheets/{worksheet}")
    def delete_worksheet(workbook: str, worksheet: str, request: Request) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_worksheet(ir, worksheet))
        return {"ok": True}

    @router.post("/{workbook}/formula")
    def post_formula(workbook: str, body: FormulaBody, request: Request) -> dict[str, Any]:
        """公式檢查（不寫回）：型別、計算層級與建議的角色，或錯誤訊息。"""
        ir = workspace(request).get(workbook).ir
        try:
            check = check_formula(ir, body.datasource, body.formula, body.field)
        except EditError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "datatype": check.datatype, "level": check.level, "role": check.role}

    @router.post("/{workbook}/calcs")
    def post_calc(workbook: str, body: CalcBody, request: Request) -> dict[str, Any]:
        field = edit(
            request,
            workbook,
            lambda ir: upsert_calc(ir, body.datasource, body.caption, body.formula, body.field),
        )
        return {"field": field.model_dump(mode="json")}

    @router.delete("/{workbook}/datasources/{datasource}/calcs/{field}")
    def delete_calc(
        workbook: str, datasource: str, field: str, request: Request
    ) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_calc(ir, datasource, field))
        return {"ok": True}

    @router.put("/{workbook}/datasources/{datasource}/hierarchies")
    def put_hierarchy(
        workbook: str, datasource: str, body: HierarchyBody, request: Request
    ) -> dict[str, Any]:
        hierarchy = edit(
            request,
            workbook,
            lambda ir: upsert_hierarchy(ir, datasource, body.name, body.fields, body.original),
        )
        return {"hierarchy": hierarchy.model_dump(mode="json") if hierarchy is not None else None}

    @router.delete("/{workbook}/datasources/{datasource}/hierarchies")
    def delete_hierarchy(
        workbook: str, datasource: str, name: str, request: Request
    ) -> dict[str, bool]:
        """刪除名為 name（查詢參數；名稱可能含斜線）的階層。"""
        edit(request, workbook, lambda ir: remove_hierarchy(ir, datasource, name))
        return {"ok": True}

    @router.post("/{workbook}/parameters")
    def post_parameter(workbook: str, body: ParameterBody, request: Request) -> dict[str, Any]:
        parameter = edit(request, workbook, lambda ir: upsert_parameter(ir, body.parameter))
        return {"parameter": parameter.model_dump(mode="json")}

    @router.delete("/{workbook}/parameters/{parameter}")
    def delete_parameter(workbook: str, parameter: str, request: Request) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_parameter(ir, parameter))
        return {"ok": True}

    @router.get("/{workbook}/outline")
    def get_outline(workbook: str, request: Request) -> dict[str, Any]:
        return outline(workspace(request).get(workbook).ir)

    @router.get("/{workbook}/dashboards/{dashboard}")
    def get_dashboard(workbook: str, dashboard: str, request: Request) -> dict[str, Any]:
        ir = workspace(request).get(workbook).ir
        board = next((d for d in ir.dashboards if d.id == dashboard), None)
        if board is None:
            raise HTTPException(404, f"找不到 dashboard「{dashboard}」")
        return {"dashboard": board.model_dump(mode="json")}

    @router.put("/{workbook}/dashboards/{dashboard}")
    def put_dashboard(
        workbook: str, dashboard: str, body: DashboardBody, request: Request
    ) -> dict[str, Any]:
        if body.dashboard.get("id") != dashboard:
            raise HTTPException(422, "dashboard 的 id 與網址不符")
        board = edit(request, workbook, lambda ir: replace_dashboard(ir, body.dashboard))
        return {"dashboard": board.model_dump(mode="json")}

    @router.post("/{workbook}/dashboards")
    def post_dashboard(workbook: str, body: NewDashboardBody, request: Request) -> dict[str, Any]:
        board = edit(
            request, workbook, lambda ir: add_dashboard(ir, body.name, body.width, body.height)
        )
        return {"dashboard": board.model_dump(mode="json")}

    @router.post("/{workbook}/dashboards/{dashboard}/duplicate")
    def post_duplicate_dashboard(workbook: str, dashboard: str, request: Request) -> dict[str, Any]:
        board = edit(request, workbook, lambda ir: duplicate_dashboard(ir, dashboard))
        return {"dashboard": board.model_dump(mode="json")}

    @router.delete("/{workbook}/dashboards/{dashboard}")
    def delete_dashboard(workbook: str, dashboard: str, request: Request) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_dashboard(ir, dashboard))
        return {"ok": True}

    @router.get("/{workbook}/stories/{story}")
    def get_story(workbook: str, story: str, request: Request) -> dict[str, Any]:
        ir = workspace(request).get(workbook).ir
        found = next((s for s in ir.stories if s.id == story), None)
        if found is None:
            raise HTTPException(404, f"找不到 Story「{story}」")
        return {"story": found.model_dump(mode="json")}

    @router.put("/{workbook}/stories/{story}")
    def put_story(workbook: str, story: str, body: StoryBody, request: Request) -> dict[str, Any]:
        if body.story.get("id") != story:
            raise HTTPException(422, "Story 的 id 與網址不符")
        found = edit(request, workbook, lambda ir: replace_story(ir, body.story))
        return {"story": found.model_dump(mode="json")}

    @router.post("/{workbook}/stories")
    def post_story(workbook: str, body: NamedBody, request: Request) -> dict[str, Any]:
        found = edit(request, workbook, lambda ir: add_story(ir, body.name))
        return {"story": found.model_dump(mode="json")}

    @router.delete("/{workbook}/stories/{story}")
    def delete_story(workbook: str, story: str, request: Request) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_story(ir, story))
        return {"ok": True}

    @router.post("/{workbook}/actions")
    def post_action(workbook: str, body: ActionBody, request: Request) -> dict[str, Any]:
        action = edit(request, workbook, lambda ir: upsert_action(ir, body.action))
        return {"action": action.model_dump(mode="json")}

    @router.delete("/{workbook}/actions/{action}")
    def delete_action(workbook: str, action: str, request: Request) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_action(ir, action))
        return {"ok": True}

    @router.post("/{workbook}/ids")
    def post_id(workbook: str, body: IdBody, request: Request) -> dict[str, str]:
        """新的篩選、Table Calculation 等的 id（與報表中其他 id 不重複）。"""
        return {"id": new_id(workspace(request).get(workbook).ir, body.prefix, body.text)}

    return router


def _as_of(directory: Path) -> str | None:
    """相對日期篩選的基準日（與檢視器相同：cases.yaml 的 today 或抓取預期結果的日期）。"""
    path = directory / "cases.yaml"
    if not path.is_file():
        return None
    try:
        return load_cases(path).as_of()
    except CasesError:
        return None


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return float(value)


__all__ = ["editor_router"]
