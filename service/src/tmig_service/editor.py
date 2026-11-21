"""桌面版編輯器的 API：資料窗格、工作表的新增／修改／複製／刪除、計算欄位與公式檢查。

只在桌面版（本機引擎）啟用：變更寫回開啟中報表的 golden.ir.json（工作區中的複本，存檔時才寫成報表套件），
檢視器的查詢立即使用新的 IR。每次修改工作表都送整張工作表（編輯器自己做復原、重做），引擎檢查後換掉。
資料源的階層（drill-path）在資料窗格中建立、改名、改變欄位與刪除。
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from tmig.data.executor import ExecutionError
from tmig.data.extract_refresh import RefreshError, refresh_extract, refresh_plan
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError, open_executor
from tmig.edit import (
    EditError,
    add_analytics_field,
    add_dashboard,
    add_story,
    add_worksheet,
    apply_filter_to_worksheets,
    catalog,
    check_formula,
    duplicate_as_crosstab,
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
    replace_references,
    replace_story,
    replace_worksheet,
    set_datasource_filters,
    set_geo_defaults,
    set_geography,
    upsert_action,
    upsert_calc,
    upsert_hierarchy,
    upsert_parameter,
)
from tmig.extract_edit import (
    extract_info,
    set_extract_increment,
    set_extract_options,
    switch_to_extract,
    switch_to_live,
)
from tmig.field_edit import (
    create_combined,
    create_folder,
    create_user_filter,
    duplicate_field,
    move_to_folder,
    remove_field,
    remove_folder,
    rename_folder,
    set_aliases,
    set_date_properties,
    set_field_properties,
    split_field,
    upsert_bins,
    upsert_group,
    upsert_set,
)
from tmig.ir import (
    IR,
    FieldUse,
    FileConnection,
    MemberAlias,
    ParamRef,
    TupleFilter,
    has_local_data,
)
from tmig.ir.models import SHAPE_FOLDER, GeoAlias, GeoRole, Scalar, SetRule, shape_image_error
from tmig.merge import import_from
from tmig.package import SOURCES, member_path, read_report_file, shape_images
from tmig.planner.dynamic import parameter_values
from tmig.planner.state import QueryState, use_key
from tmig.shapes import MAX_SHAPE_BYTES, palettes
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


class DatasourceFiltersBody(BaseModel):
    """資料源篩選：完整的篩選清單（取代原本的資料源篩選；萃取篩選不變）。"""

    model_config = ConfigDict(extra="forbid")
    filters: list[dict[str, Any]]


class ExtractSettingsBody(BaseModel):
    """萃取設定：增量更新的欄位（None 為只能完整更新）。"""

    model_config = ConfigDict(extra="forbid")
    increment: str | None = None
    top_rows: int | None = Field(default=None, gt=0)
    """只取前 N 筆（None 為全部）。"""
    aggregate: bool = False
    """彙總可見維度的資料。"""


class ImportBody(BaseModel):
    """匯入另一份報表的工作表：source 是工作區中另一份報表的 id；worksheets 省略時整份匯入。"""

    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1)
    worksheets: list[str] | None = None


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp")

SHAPE_UPLOAD_LIMIT = 100
"""一次加入的形狀圖片數量上限。"""
IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    ".webp": (b"RIFF",),
}
"""圖片檔開頭的位元組（副檔名與內容要相符；SVG 是文字，另外檢查）。"""


class ShapeFile(BaseModel):
    """要加入的形狀圖片：檔名與內容（base64）。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)


class ShapesBody(BaseModel):
    """把圖片加入自訂的形狀調色盤（Tableau 的 My Tableau Repository\\Shapes\\<調色盤>）。"""

    model_config = ConfigDict(extra="forbid")
    palette: str = Field(min_length=1, max_length=60)
    files: list[ShapeFile] = Field(min_length=1, max_length=SHAPE_UPLOAD_LIMIT)


IMAGE_FOLDER = "Image"
"""dashboard 的圖片物件選的圖片在報表的 files/ 下的資料夾（Tableau 的 .twbx 也放在 Image/）。"""


class ImageBody(BaseModel):
    """dashboard 的圖片物件選的圖片檔：檔名與內容（base64）。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)


def shape_palettes(directory: Path) -> list[dict[str, Any]]:
    """報表可以用的自訂形狀調色盤（files/Shapes/ 與 .twbx／.twb 內嵌的圖片）。"""
    return [{"name": name, "images": images} for name, images in palettes(directory).items()]


def shape_upload(directory: Path, body: ShapesBody) -> list[str]:
    """把圖片寫進報表的 files/Shapes/<調色盤>/；回傳加入的圖片路徑。名稱或內容不正確時 ValueError。"""
    palette = body.palette.strip()
    if not palette or palette in (".", "..") or any(c in palette for c in '/\\:*?"<>|'):
        raise ValueError(f'調色盤的名稱不能有 / \\ : * ? " < > | 等字元：{body.palette}')
    written: list[tuple[str, bytes]] = []
    for item in body.files:
        name = item.name.strip()
        path = f"{SHAPE_FOLDER}/{palette}/{name}"
        problem = shape_image_error(path)
        if problem is not None or any(c in name for c in '/\\:*?"<>|'):
            raise ValueError(problem or f"圖片的檔名不正確：{item.name}")
        written.append((path, image_bytes(name, item.content)))
    for path, content in written:
        relative = member_path(path)
        assert relative is not None
        target = directory.joinpath("files", *relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return [path for path, _ in written]


def image_bytes(name: str, encoded: str) -> bytes:
    """上傳的圖片（base64）的內容：不是 base64、空的、太大，或內容與副檔名不符時 ValueError。"""
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"圖片 {name} 的內容不是 base64") from exc
    if not content or len(content) > MAX_SHAPE_BYTES:
        raise ValueError(f"圖片 {name} 是空的或超過 {MAX_SHAPE_BYTES // 1024 // 1024} MB")
    suffix = Path(name).suffix.lower()
    if suffix == ".svg":
        if b"<svg" not in content[:4096].lower():
            raise ValueError(f"{name} 不是 SVG 圖片")
    elif not content.startswith(IMAGE_SIGNATURES.get(suffix, ())):
        raise ValueError(f"{name} 的內容不是 {suffix} 圖片")
    return content


def image_upload(directory: Path, body: ImageBody) -> str:
    """dashboard 的圖片物件選的圖片檔寫進報表的 files/Image/；回傳圖片路徑（Image/<檔名>），存檔、發布時
    跟著報表。報表裡已經有同名的圖片時：內容相同就用它，不同就另取檔名（logo (2).png），不蓋掉其他圖片物件
    正在用的圖片。檔名或內容不正確時 ValueError。"""
    name = body.name.strip()
    stem, suffix = Path(name).stem, Path(name).suffix
    if (
        not stem
        or name in (".", "..")
        or any(c in name for c in '/\\:*?"<>|')
        or suffix.lower() not in IMAGE_SUFFIXES
    ):
        raise ValueError(f"圖片的檔名不正確（要是 {'、'.join(IMAGE_SUFFIXES)} 檔）：{body.name}")
    content = image_bytes(name, body.content)
    numbered = 1
    while True:
        filename = name if numbered == 1 else f"{stem} ({numbered}){suffix}"
        path = f"{IMAGE_FOLDER}/{filename}"
        # 已有的圖片：報表的 files/ 與 .twbx 內的都算
        existing = read_report_file(directory, path)
        if existing is None:
            target = directory / "files" / IMAGE_FOLDER / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return path
        if existing == content:
            return path
        numbered += 1


def source_copies(
    source_root: Path, target_root: Path, paths: list[str]
) -> list[tuple[Path, Path]]:
    """匯入資料源時要複製的來源檔案：（來源報表中的檔案, 目標報表中的位置）。路徑是 .twb 中寫的，不可信：
    解析後不在各自的 sources 資料夾之內的（例如 ../../x）不複製。File Geodatabase 是資料夾，整個複製。
    兩份報表有同一個路徑但內容不同的檔案時 ValueError（不能蓋掉目標報表的資料）。"""
    source_root, target_root = source_root.resolve(), target_root.resolve()
    found: list[tuple[Path, Path]] = []
    for path in dict.fromkeys(paths):
        origin = (source_root / path).resolve()
        copied = (target_root / path).resolve()
        inside = origin.is_relative_to(source_root) and copied.is_relative_to(target_root)
        if not inside or origin == source_root or not origin.exists():
            continue
        if origin.is_file() and copied.is_file() and copied.read_bytes() != origin.read_bytes():
            raise ValueError(f"兩份報表都有來源檔案 {path} 但內容不同，無法匯入這個資料源")
        found.append((origin, copied))
    return found


def _image_paths(value: Any) -> list[str]:
    """內容中的圖片路徑（圖片區塊、圖片按鈕）。"""
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif (
            isinstance(item, str)
            and item.lower().endswith(IMAGE_SUFFIXES)
            and item not in found
            and not item.lower().startswith(("http://", "https://", "data:"))
        ):
            found.append(item)

    walk(value)
    return found


class ExtractModeBody(BaseModel):
    """即時與萃取的切換：改成萃取時立即從來源建立萃取的資料。"""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["live", "extract"]


class ExtractRefreshBody(BaseModel):
    """立即更新萃取：完整更新，或只附加增量欄位較新的列。"""

    model_config = ConfigDict(extra="forbid")
    incremental: bool = False


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
    """計算欄位；ad_hoc 為直接在架上輸入的臨時計算（名稱就是公式，caption 不用給）。"""

    model_config = ConfigDict(extra="forbid")
    datasource: str
    caption: str = ""
    formula: str
    field: str | None = None
    ad_hoc: bool = False


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
    from_source: bool = False
    """萃取篩選的對話框：可選值取自萃取的來源（萃取中只有通過萃取篩選的資料）。"""


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


class UserFilterEntryBody(BaseModel):
    """使用者篩選中的一個對象：使用者名稱或群組，與看得到的成員。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["user", "group"]
    name: str
    members: list[Scalar]


class UserFilterBody(BaseModel):
    """建立使用者篩選：來源的維度、名稱（空白時為「<來源> 的使用者篩選」）與各對象看得到的成員。"""

    model_config = ConfigDict(extra="forbid")
    source: str
    caption: str = ""
    entries: list[UserFilterEntryBody]


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
    default_color: dict[str, Any] | None = None
    default_shapes: list[dict[str, Any]] | None = None
    default_sort: dict[str, Any] | None = None
    total_using: Literal["sum", "avg", "min", "max"] | None = None


class SplitBody(BaseModel):
    """分割：分隔符號、要建立的欄位數；from_end 為從最後面取。"""

    model_config = ConfigDict(extra="forbid")
    separator: str = Field(min_length=1)
    count: int = Field(ge=1, le=10)
    from_end: bool = False


class ReplaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: str


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
        if body.from_source:
            # 萃取的來源：把這個資料源當作即時連線查詢（來源無法解析時仍以萃取的資料）
            try:
                ir = switch_to_live(ir, ds.id)
                ds = next(d for d in ir.datasources if d.id == ds.id)
            except EditError:
                pass
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
                # 正在編輯的是資料源篩選時，可選值不套用它自己（其他的資料源篩選照樣套用）
                scoped = ir
                if body.exclude_filter and any(f.id == body.exclude_filter for f in ds.filters):
                    without = ds.model_copy(
                        update={"filters": [f for f in ds.filters if f.id != body.exclude_filter]}
                    )
                    scoped = ir.model_copy(
                        update={
                            "datasources": [without if d.id == ds.id else d for d in ir.datasources]
                        }
                    )
                sql = domain_sql(scoped, ds.id, use, kind, user)
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

    @router.get("/{workbook}/datasources/{datasource}/filters")
    def get_datasource_filters(
        workbook: str,
        datasource: str,
        request: Request,
        stage: Literal["datasource", "extract"] = "datasource",
    ) -> dict[str, Any]:
        """資料源篩選（資料來源分頁的「篩選條件」）；stage=extract 時是萃取篩選。"""
        ir = workspace(request).get(workbook).ir
        ds = next((d for d in ir.datasources if d.id == datasource), None)
        if ds is None:
            raise HTTPException(404, f"找不到資料源「{datasource}」")
        return {"filters": [f.model_dump(mode="json") for f in ds.filters if f.stage == stage]}

    @router.put("/{workbook}/datasources/{datasource}/filters")
    def put_datasource_filters(
        workbook: str,
        datasource: str,
        body: DatasourceFiltersBody,
        request: Request,
        stage: Literal["datasource", "extract"] = "datasource",
    ) -> dict[str, Any]:
        saved = edit(
            request,
            workbook,
            lambda ir: set_datasource_filters(ir, datasource, body.filters, stage),
        )
        return {"filters": [f.model_dump(mode="json") for f in saved]}

    # ------------------------------------------------------------ 萃取設定

    def extract_state(request: Request, workbook: str, datasource: str) -> dict[str, Any]:
        """萃取設定與上次從來源更新的時間（<資料源>.refresh.json）。"""
        loaded = workspace(request).get(workbook)
        try:
            info = extract_info(loaded.ir, datasource)
        except EditError as exc:
            raise HTTPException(404, str(exc)) from exc
        report = loaded.directory / "data" / f"{datasource}.refresh.json"
        refreshed: str | None = None
        if info["mode"] == "extract" and report.is_file():
            try:
                refreshed = json.loads(report.read_text(encoding="utf-8")).get("finished")
            except (OSError, ValueError):
                refreshed = None
        return {**info, "refreshed": refreshed}

    def run_refresh(
        request: Request, workbook: str, datasource: str, ir: IR, incremental: bool
    ) -> dict[str, Any]:
        """以 ir 的萃取設定從來源重建（或增量更新）這個資料源的資料檔；失敗時資料檔不變。"""
        runtime: DataRuntime = request.app.state.runtime
        loaded = workspace(request).get(workbook)
        try:
            plan = refresh_plan(ir, datasource)
            executor = open_executor(plan.source, runtime.configs)
            try:
                result = refresh_extract(
                    ir,
                    datasource,
                    executor,
                    loaded.directory / "data" / f"{datasource}.duckdb",
                    incremental=incremental,
                )
            finally:
                executor.close()
        except RefreshError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ExecutionError as exc:
            raise HTTPException(500, str(exc)) from exc
        return result.to_json()

    @router.get("/{workbook}/datasources/{datasource}/extract")
    def get_extract(workbook: str, datasource: str, request: Request) -> dict[str, Any]:
        """萃取設定：模式（live、extract、file）、能不能切換、增量更新的欄位、萃取篩選與上次更新的時間。"""
        return extract_state(request, workbook, datasource)

    @router.put("/{workbook}/datasources/{datasource}/extract")
    def put_extract(
        workbook: str, datasource: str, body: ExtractSettingsBody, request: Request
    ) -> dict[str, Any]:
        def change(ir: IR) -> IR:
            # 先清掉增量更新再設定彙總（兩者不能並用），最後設定增量更新的欄位
            cleared = set_extract_increment(ir, datasource, None) if body.aggregate else ir
            options = set_extract_options(
                cleared, datasource, top_rows=body.top_rows, aggregate=body.aggregate
            )
            return set_extract_increment(options, datasource, body.increment)

        edit(request, workbook, change)
        return extract_state(request, workbook, datasource)

    @router.post("/{workbook}/datasources/{datasource}/extract/mode")
    def post_extract_mode(
        workbook: str, datasource: str, body: ExtractModeBody, request: Request
    ) -> dict[str, Any]:
        """即時與萃取的切換。改成萃取時先從來源建立萃取的資料，成功後才存檔（失敗時維持即時連線）。"""
        space = workspace(request)
        ir = space.get(workbook).ir
        try:
            changed = (
                switch_to_extract(ir, datasource)
                if body.mode == "extract"
                else switch_to_live(ir, datasource)
            )
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        refreshed = None
        if body.mode == "extract":
            refreshed = run_refresh(request, workbook, datasource, changed, False)
        space.save_ir(workbook, changed)
        if on_change is not None:
            on_change(workbook)
        return {**extract_state(request, workbook, datasource), "result": refreshed}

    @router.post("/{workbook}/datasources/{datasource}/extract/refresh")
    def post_extract_refresh(
        workbook: str, datasource: str, body: ExtractRefreshBody, request: Request
    ) -> dict[str, Any]:
        """立即更新萃取（Tableau 的「資料＞萃取＞重新整理」）：套用目前的萃取篩選，從來源取回資料。"""
        ir = workspace(request).get(workbook).ir
        result = run_refresh(request, workbook, datasource, ir, body.incremental)
        return {**extract_state(request, workbook, datasource), "result": result}

    @router.post("/{workbook}/import")
    def post_import(workbook: str, body: ImportBody, request: Request) -> dict[str, Any]:
        """匯入另一份報表的工作表（Tableau 的「匯入活頁簿」、書籤與在活頁簿之間複製貼上工作表）：
        worksheets 省略時整份匯入（含 dashboard、Story 與動作）。資料源的資料、來源檔案與圖片一併複製。"""
        space = workspace(request)
        if body.source == workbook:
            raise HTTPException(422, "不能從同一份報表匯入")
        source = space.get(body.source)
        target = space.get(workbook)
        try:
            result = import_from(target.ir, source.ir, body.worksheets)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        copies: list[tuple[Path, Path]] = []
        for old, new in result.datasources.items():
            ds = next(d for d in source.ir.datasources if d.id == old)
            if has_local_data(ds.connection):
                try:
                    data_file = space.database(body.source, old)  # 還沒有匯入過的先匯入
                except (ExecutionError, FileNotFoundError) as exc:
                    raise HTTPException(422, f"來源報表的資料無法匯入：{exc}") from exc
                if data_file.is_file():
                    copies.append((data_file, target.directory / "data" / f"{new}.duckdb"))
                report = data_file.with_name(f"{old}.refresh.json")
                if report.is_file():
                    copies.append((report, target.directory / "data" / f"{new}.refresh.json"))
            if isinstance(ds.connection, FileConnection):
                # 檔案型的資料源：來源檔案也複製（之後可以重新匯入、切換資料解譯器）
                try:
                    copies += source_copies(
                        source.directory / SOURCES,
                        target.directory / SOURCES,
                        [t.path for t in ds.connection.tables],
                    )
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
        for origin, copied in copies:
            copied.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_dir():
                shutil.copytree(origin, copied, dirs_exist_ok=True)
            else:
                shutil.copy2(origin, copied)
        # 圖片：匯入的工作表與資料源用到的自訂圖片形狀；整份匯入時另有 dashboard 用到的圖片。
        # 目標報表已經有同一個路徑的圖片時用它的（不蓋掉）
        images = set(shape_images(result.ir))
        if body.worksheets is None:
            images.update(_image_paths([d.model_dump(mode="json") for d in source.ir.dashboards]))
        for path in sorted(images):
            content = read_report_file(source.directory, path)
            relative = member_path(path)
            if content is None or relative is None:
                continue
            copied = target.directory.joinpath("files", *relative.parts)
            if not copied.is_file() and read_report_file(target.directory, path) is None:
                copied.parent.mkdir(parents=True, exist_ok=True)
                copied.write_bytes(content)
        space.save_ir(workbook, result.ir)
        if on_change is not None:
            on_change(workbook)
        return result.to_json()

    @router.get("/{workbook}/shapes")
    def get_shapes(workbook: str, request: Request) -> dict[str, Any]:
        """自訂的形狀調色盤（Tableau「編輯形狀」的「選取形狀調色盤」）：報表中的圖片形狀。"""
        return {"palettes": shape_palettes(workspace(request).get(workbook).directory)}

    @router.post("/{workbook}/shapes")
    def post_shapes(workbook: str, body: ShapesBody, request: Request) -> dict[str, Any]:
        """把圖片加入自訂的形狀調色盤：存在報表的 files/Shapes/<調色盤>/，存檔、發布時跟著報表。"""
        loaded = workspace(request).get(workbook)
        if loaded.published is not None:
            raise HTTPException(409, "伺服器上的報表不能直接編輯")
        try:
            added = shape_upload(loaded.directory, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"added": added, "palettes": shape_palettes(loaded.directory)}

    @router.post("/{workbook}/images")
    def post_image(workbook: str, body: ImageBody, request: Request) -> dict[str, str]:
        """dashboard 的圖片物件選的圖片檔：存進報表（files/Image/），回傳圖片區塊要寫的路徑。"""
        loaded = workspace(request).get(workbook)
        if loaded.published is not None:
            raise HTTPException(409, "伺服器上的報表不能直接編輯")
        try:
            return {"path": image_upload(loaded.directory, body)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/{workbook}/worksheets")
    def post_worksheet(workbook: str, body: NewWorksheetBody, request: Request) -> dict[str, Any]:
        sheet = edit(request, workbook, lambda ir: add_worksheet(ir, body.name, body.datasource))
        return {"worksheet": sheet.model_dump(mode="json")}

    @router.post("/{workbook}/worksheets/{worksheet}/duplicate")
    def post_duplicate(workbook: str, worksheet: str, request: Request) -> dict[str, Any]:
        sheet = edit(request, workbook, lambda ir: duplicate_worksheet(ir, worksheet))
        return {"worksheet": sheet.model_dump(mode="json")}

    @router.post("/{workbook}/worksheets/{worksheet}/crosstab")
    def post_crosstab(workbook: str, worksheet: str, request: Request) -> dict[str, Any]:
        """複製成交叉資料表：新的工作表是這個檢視的文字表（見 tmig.edit.duplicate_as_crosstab）。"""
        sheet = edit(request, workbook, lambda ir: duplicate_as_crosstab(ir, worksheet))
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
            lambda ir: upsert_calc(
                ir, body.datasource, body.caption, body.formula, body.field, body.ad_hoc
            ),
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
        """預設屬性：數字或日期格式、註解、色彩、形狀、排序與總計使用（只改送來的項目）。"""
        changes = {key: getattr(body, key) for key in body.model_fields_set}
        edit(request, workbook, lambda ir: set_field_properties(ir, datasource, field, **changes))
        return {"ok": True}

    @router.post("/{workbook}/datasources/{datasource}/fields/{field}/split")
    def post_split(
        workbook: str, datasource: str, field: str, body: SplitBody, request: Request
    ) -> dict[str, Any]:
        """分割、自訂分割：建立「<名稱> - Split N」的計算欄位（見 tmig.field_edit.split_field）。"""
        made = edit(
            request,
            workbook,
            lambda ir: split_field(
                ir, datasource, field, body.separator, body.count, body.from_end
            ),
        )
        return {"fields": [f.model_dump(mode="json") for f in made]}

    @router.post("/{workbook}/datasources/{datasource}/fields/{field}/replace")
    def post_replace_references(
        workbook: str, datasource: str, field: str, body: ReplaceBody, request: Request
    ) -> dict[str, bool]:
        """取代參照：用到這個欄位的地方改用另一個欄位（見 tmig.edit.replace_references）。"""
        edit(
            request,
            workbook,
            lambda ir: replace_references(ir, datasource, field, body.replacement),
        )
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

    @router.post("/{workbook}/datasources/{datasource}/user-filters")
    def post_user_filter(
        workbook: str, datasource: str, body: UserFilterBody, request: Request
    ) -> dict[str, Any]:
        """建立使用者篩選（Tableau 的「伺服器＞建立使用者篩選」）：布林的計算欄位，登入者看得到的成員為 TRUE。"""
        entries = [(e.kind, e.name, e.members) for e in body.entries]
        created = edit(
            request,
            workbook,
            lambda ir: create_user_filter(ir, datasource, body.source, body.caption, entries),
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
