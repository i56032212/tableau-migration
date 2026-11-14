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
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from tmig.data.executor import ExecutionError
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError
from tmig.edit import (
    EditError,
    add_analytics_field,
    add_dashboard,
    add_story,
    add_worksheet,
    apply_filter_to_worksheets,
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
    set_geo_defaults,
    set_geography,
    upsert_action,
    upsert_calc,
    upsert_hierarchy,
    upsert_parameter,
)
from tmig.field_edit import (
    create_combined,
    create_folder,
    duplicate_field,
    move_to_folder,
    remove_field,
    remove_folder,
    rename_folder,
    set_aliases,
    set_date_properties,
    set_field_properties,
    upsert_bins,
    upsert_group,
    upsert_set,
)
from tmig.ir import FieldUse, MemberAlias, ParamRef, TupleFilter
from tmig.ir.models import GeoAlias, GeoRole, Scalar, SetRule
from tmig.planner.dynamic import parameter_values
from tmig.planner.state import QueryState, use_key
from tmig.sqlgen.query import QueryError, custom_sql_parameters, domain_sql, worksheet_sql
from tmig.verify.cases import CasesError, load_cases
from tmig_service.app import Workspace, current_user


class WorksheetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worksheet: dict[str, Any]


class ApplyFilterBody(BaseModel):
    """篩選套用到的其他工作表（空的表示只套用到這張工作表）。"""

    model_config = ConfigDict(extra="forbid")
    targets: list[str]


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


class AnalyticsFieldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource: str
    kind: Literal["cluster", "forecast_indicator"]


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


class GeographyBody(BaseModel):
    """欄位的地理角色（None 取消）與編輯位置中指定的地名（None 時不變）。"""

    model_config = ConfigDict(extra="forbid")
    role: GeoRole | None = None
    aliases: list[GeoAlias] | None = None


class GeoDefaultsBody(BaseModel):
    """編輯位置的預設國家、州（空的表示不指定）。"""

    model_config = ConfigDict(extra="forbid")
    country: str | None = None
    state: str | None = None


class FieldValuesBody(BaseModel):
    """參數對話框的「開啟報表時」：欄位目前的值（轉成參數的型別）。"""

    model_config = ConfigDict(extra="forbid")
    field: str
    datatype: Literal["string", "integer", "real", "boolean", "date", "datetime"] | None = None


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


class AliasesBody(BaseModel):
    """維度成員的別名（空白的別名表示不設定）。"""

    model_config = ConfigDict(extra="forbid")
    aliases: list[MemberAlias]


class GroupItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    members: list[Scalar]


class GroupBody(BaseModel):
    """群組：來源欄位、名稱（空白時為「<來源> (group)」）、各組與「其他」；field 為修改的群組。"""

    model_config = ConfigDict(extra="forbid")
    source: str
    caption: str = ""
    groups: list[GroupItemBody]
    other: str | None = None
    field: str | None = None


class BinsBody(BaseModel):
    """數值分組：大小為數字（size）或參數（parameter）；field 為修改的數值分組。"""

    model_config = ConfigDict(extra="forbid")
    source: str
    caption: str = ""
    size: float | None = None
    parameter: str | None = None
    field: str | None = None


class SetBody(BaseModel):
    """集合：維度（固定成員可以多個）、名稱與規則（members、condition、top_n）；field 為修改的集合。"""

    model_config = ConfigDict(extra="forbid")
    levels: list[str]
    caption: str = ""
    rule: dict[str, Any]
    field: str | None = None


class PropertiesBody(BaseModel):
    """預設屬性：只改送來的項目（null 為清除；會計年度起始月 null 為沿用資料源）。"""

    model_config = ConfigDict(extra="forbid")
    format: str | None = None
    comment: str | None = None
    fiscal_year_start: int | None = Field(default=None, ge=1, le=12)


class DatePropertiesBody(BaseModel):
    """資料源的日期屬性：只改送來的項目。"""

    model_config = ConfigDict(extra="forbid")
    week_start: (
        Literal["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"] | None
    ) = None
    fiscal_year_start: int | None = Field(default=None, ge=1, le=12)


class CombinedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[str] = Field(min_length=2)


class FolderBody(BaseModel):
    """建立資料夾（name 與欄位）、改名（rename）或移動欄位（folder 為 null 時移出資料夾）。"""

    model_config = ConfigDict(extra="forbid")
    name: str
    fields: list[str] = []


class FolderRenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class MoveFieldsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[str] = Field(min_length=1)
    folder: str | None = None


SET_RULE: TypeAdapter[Any] = TypeAdapter(SetRule)


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

    @router.post("/{workbook}/field-values")
    def post_field_values(workbook: str, body: FieldValuesBody, request: Request) -> dict[str, Any]:
        """欄位目前的不重複值（含 LOD 的計算欄位；最多 1000 個）：動態參數的目前值與允許值。"""
        space = workspace(request)
        runtime: DataRuntime = request.app.state.runtime
        ir = space.get(workbook).ir
        if not any(f.id == body.field for d in ir.datasources for f in d.fields):
            raise HTTPException(404, f"找不到欄位「{body.field}」")

        def run(query: Any, datasource: Any) -> Any:
            return runtime.run(query, datasource, lambda: space.database(workbook, datasource.id))

        try:
            values = parameter_values(ir, body.field, body.datatype, run, current_user(request))
        except QueryError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(500, str(exc)) from exc
        return {"values": [_json(v) for v in values[:1000]], "more": len(values) > 1000}

    @router.put("/{workbook}/worksheets/{worksheet}")
    def put_worksheet(
        workbook: str, worksheet: str, body: WorksheetBody, request: Request
    ) -> dict[str, Any]:
        if body.worksheet.get("id") != worksheet:
            raise HTTPException(422, "工作表的 id 與網址不符")
        sheet = edit(request, workbook, lambda ir: replace_worksheet(ir, body.worksheet))
        return {"worksheet": sheet.model_dump(mode="json")}

    @router.get("/{workbook}/worksheets/{worksheet}/filters/{filter_id}/apply")
    def get_apply_filter(
        workbook: str, worksheet: str, filter_id: str, request: Request
    ) -> dict[str, Any]:
        """這個篩選可以套用到的其他工作表（同一個資料源）與目前是否已套用（同一個篩選群組）。"""
        ir = workspace(request).get(workbook).ir
        sheet = next((w for w in ir.worksheets if w.id == worksheet), None)
        found = next((f for f in sheet.filters if f.id == filter_id), None) if sheet else None
        if sheet is None or found is None or isinstance(found, TupleFilter):
            raise HTTPException(404, "找不到這個篩選")
        return {
            "worksheets": [
                {
                    "id": other.id,
                    "name": other.name,
                    "applied": found.group is not None
                    and any(
                        not isinstance(f, TupleFilter) and f.group == found.group
                        for f in other.filters
                    ),
                }
                for other in ir.worksheets
                if other.id != sheet.id and other.datasource == sheet.datasource
            ]
        }

    @router.post("/{workbook}/worksheets/{worksheet}/filters/{filter_id}/apply")
    def post_apply_filter(
        workbook: str, worksheet: str, filter_id: str, body: ApplyFilterBody, request: Request
    ) -> dict[str, Any]:
        """把篩選套用到其他工作表（選取的工作表、使用此資料源的所有工作表），或改回只套用到這張。"""
        sheet = edit(
            request,
            workbook,
            lambda ir: apply_filter_to_worksheets(ir, worksheet, filter_id, body.targets),
        )
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

    @router.post("/{workbook}/analytics-fields")
    def post_analytics_field(
        workbook: str, body: AnalyticsFieldBody, request: Request
    ) -> dict[str, Any]:
        """分析的欄位：新的叢集欄位，或資料源的 Forecast Indicator（見 tmig.edit.add_analytics_field）。"""
        field = edit(
            request, workbook, lambda ir: add_analytics_field(ir, body.datasource, body.kind)
        )
        return {"field": field.model_dump(mode="json")}

    @router.delete("/{workbook}/datasources/{datasource}/calcs/{field}")
    def delete_calc(
        workbook: str, datasource: str, field: str, request: Request
    ) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_calc(ir, datasource, field))
        return {"ok": True}

    @router.post("/{workbook}/datasources/{datasource}/fields/{field}/duplicate")
    def post_duplicate_field(
        workbook: str, datasource: str, field: str, request: Request
    ) -> dict[str, Any]:
        """複製欄位：「<名稱> (copy)」（見 tmig.field_edit.duplicate_field）。"""
        copy = edit(request, workbook, lambda ir: duplicate_field(ir, datasource, field))
        return {"field": copy.model_dump(mode="json")}

    @router.put("/{workbook}/datasources/{datasource}/fields/{field}/aliases")
    def put_aliases(
        workbook: str, datasource: str, field: str, body: AliasesBody, request: Request
    ) -> dict[str, bool]:
        edit(request, workbook, lambda ir: set_aliases(ir, datasource, field, body.aliases))
        return {"ok": True}

    @router.put("/{workbook}/datasources/{datasource}/fields/{field}/properties")
    def put_properties(
        workbook: str, datasource: str, field: str, body: PropertiesBody, request: Request
    ) -> dict[str, bool]:
        """預設的數字或日期格式與註解（只改送來的項目）。"""
        changes = {key: getattr(body, key) for key in body.model_fields_set}
        edit(request, workbook, lambda ir: set_field_properties(ir, datasource, field, **changes))
        return {"ok": True}

    @router.put("/{workbook}/datasources/{datasource}/date-properties")
    def put_date_properties(
        workbook: str, datasource: str, body: DatePropertiesBody, request: Request
    ) -> dict[str, bool]:
        """資料源的日期屬性：週起始日與會計年度起始月（見 tmig.field_edit.set_date_properties）。"""
        changes = {
            key: getattr(body, key)
            for key in body.model_fields_set
            if getattr(body, key) is not None
        }
        edit(request, workbook, lambda ir: set_date_properties(ir, datasource, **changes))
        return {"ok": True}

    @router.post("/{workbook}/datasources/{datasource}/combined")
    def post_combined(
        workbook: str, datasource: str, body: CombinedBody, request: Request
    ) -> dict[str, Any]:
        """合併欄位：兩個以上的維度（見 tmig.field_edit.create_combined）。"""
        field = edit(request, workbook, lambda ir: create_combined(ir, datasource, body.fields))
        return {"field": field.model_dump(mode="json")}

    @router.post("/{workbook}/datasources/{datasource}/folders")
    def post_folder(
        workbook: str, datasource: str, body: FolderBody, request: Request
    ) -> dict[str, bool]:
        edit(request, workbook, lambda ir: create_folder(ir, datasource, body.name, body.fields))
        return {"ok": True}

    @router.put("/{workbook}/datasources/{datasource}/folders/{folder}")
    def put_folder(
        workbook: str, datasource: str, folder: str, body: FolderRenameBody, request: Request
    ) -> dict[str, bool]:
        edit(request, workbook, lambda ir: rename_folder(ir, datasource, folder, body.name))
        return {"ok": True}

    @router.delete("/{workbook}/datasources/{datasource}/folders/{folder}")
    def delete_folder(
        workbook: str, datasource: str, folder: str, request: Request
    ) -> dict[str, bool]:
        edit(request, workbook, lambda ir: remove_folder(ir, datasource, folder))
        return {"ok": True}

    @router.put("/{workbook}/datasources/{datasource}/folder-items")
    def put_folder_items(
        workbook: str, datasource: str, body: MoveFieldsBody, request: Request
    ) -> dict[str, bool]:
        """欄位移到資料夾（folder 為 null 時移出資料夾）。"""
        edit(
            request,
            workbook,
            lambda ir: move_to_folder(ir, datasource, body.fields, body.folder),
        )
        return {"ok": True}

    @router.delete("/{workbook}/datasources/{datasource}/fields/{field}")
    def delete_field(
        workbook: str, datasource: str, field: str, request: Request
    ) -> dict[str, bool]:
        """刪除計算欄位、群組、數值分組或集合（用到時 422 並說明用到的地方）。"""
        edit(request, workbook, lambda ir: remove_field(ir, datasource, field))
        return {"ok": True}

    @router.post("/{workbook}/datasources/{datasource}/groups")
    def post_group(
        workbook: str, datasource: str, body: GroupBody, request: Request
    ) -> dict[str, Any]:
        groups = [(g.name, g.members) for g in body.groups]
        created = edit(
            request,
            workbook,
            lambda ir: upsert_group(
                ir, datasource, body.source, body.caption, groups, body.other, body.field
            ),
        )
        return {"field": created.model_dump(mode="json")}

    @router.post("/{workbook}/datasources/{datasource}/bins")
    def post_bins(
        workbook: str, datasource: str, body: BinsBody, request: Request
    ) -> dict[str, Any]:
        if (body.size is None) == (body.parameter is None):
            raise HTTPException(422, "數值分組的大小要指定數字或參數其中之一")
        size: float | ParamRef = (
            ParamRef(parameter=body.parameter) if body.parameter is not None else body.size or 0
        )
        created = edit(
            request,
            workbook,
            lambda ir: upsert_bins(ir, datasource, body.source, body.caption, size, body.field),
        )
        return {"field": created.model_dump(mode="json")}

    @router.post("/{workbook}/datasources/{datasource}/sets")
    def post_set(workbook: str, datasource: str, body: SetBody, request: Request) -> dict[str, Any]:
        try:
            rule = SET_RULE.validate_python(body.rule)
        except ValidationError as exc:
            raise HTTPException(422, f"集合的規則不正確：{exc.errors()[0].get('msg', '')}") from exc
        created = edit(
            request,
            workbook,
            lambda ir: upsert_set(ir, datasource, body.levels, body.caption, rule, body.field),
        )
        return {"field": created.model_dump(mode="json")}

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

    @router.put("/{workbook}/datasources/{datasource}/fields/{field}/geography")
    def put_geography(
        workbook: str, datasource: str, field: str, body: GeographyBody, request: Request
    ) -> dict[str, bool]:
        """欄位的地理角色與編輯位置中指定的地名。"""
        edit(
            request,
            workbook,
            lambda ir: set_geography(ir, datasource, field, body.role, body.aliases),
        )
        return {"ok": True}

    @router.put("/{workbook}/datasources/{datasource}/geography")
    def put_geo_defaults(
        workbook: str, datasource: str, body: GeoDefaultsBody, request: Request
    ) -> dict[str, bool]:
        """編輯位置的預設國家、州。"""
        edit(
            request, workbook, lambda ir: set_geo_defaults(ir, datasource, body.country, body.state)
        )
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
