"""IR v0 的資料模型。

IR 是唯一的報表定義：解析器寫入，其他模組只讀，任何模組都不回頭讀 .twb。
規則見 Spec「IR v0 資料合約」：

- 每個實體帶穩定的 ``id`` 與 ``source_ref``（在 .twb 中的 XPath）
- 欄位一律以 ``id`` 參照；計算欄位同時保存原文 ``formula`` 與語法樹 ``ast``
- dashboard 座標存換算後的像素值
- 無法辨識的元素放進 ``unsupported``，不得默默丟棄
- 欄位異動必須升 ``ir_version``，並重新產生 ``kernel/schema/``

跨實體的參照（欄位、參數、工作表、Table Calculation）由 ``integrity`` 檢查。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

IR_VERSION = "0.1"

Id = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="穩定 id：小寫英數字與底線，以字母開頭，最長 64 字元",
    ),
]
SourceRef = Annotated[str, Field(min_length=1, description="此實體在 .twb 中的 XPath")]
Scalar = StrictBool | StrictInt | StrictFloat | StrictStr | None
"""常值。日期與時間一律以 ISO 8601 字串表示。"""

DataType = Literal["string", "integer", "real", "boolean", "date", "datetime"]
Role = Literal["dimension", "measure"]
Aggregation = Literal["sum", "avg", "min", "max", "count", "countd"]
DatePart = Literal["year", "quarter", "month", "week", "day", "hour", "minute", "second"]
WeekDay = Literal["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
CalcLevel = Literal["row", "aggregate", "lod", "table_calc"]
FilterStage = Literal[
    "extract", "datasource", "context", "top_n", "dimension", "measure", "table_calc"
]
"""篩選所屬的運算階段，依 Tableau 的運算順序排列。"""


class IRModel(BaseModel):
    """所有 IR 實體的基底：不允許未定義的欄位。"""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 欄位使用


class DateDerivation(IRModel):
    """日期欄位作為維度時的衍生方式。"""

    kind: Literal["part", "trunc"] = Field(
        description="part＝日期部分（如 YEAR 得到 2025），trunc＝截斷到單位（如年-月）"
    )
    unit: DatePart


class FieldUse(IRModel):
    """欄列架、標記編碼、篩選、排序與控制項中的一次欄位使用。"""

    field: Id = Field(description="欄位 id")
    agg: Aggregation | None = Field(default=None, description="彙總方式；None 表示不彙總")
    date: DateDerivation | None = Field(default=None, description="日期衍生；只用於日期維度")
    continuous: bool = Field(default=False, description="True＝連續（綠色），False＝離散（藍色）")
    table_calc: Id | None = Field(default=None, description="套用的 Table Calculation id")

    @model_validator(mode="after")
    def _agg_or_date(self) -> Self:
        if self.agg is not None and self.date is not None:
            raise ValueError("agg 與 date 不能同時設定")
        return self


class ParamRef(IRModel):
    """以參數取代常值，例如 Top N 的 N。"""

    parameter: Id


# ---------------------------------------------------------------- 資料源


class Calc(IRModel):
    """計算欄位。解析器只填 formula；ast 與 level 由公式引擎（B2）補上。"""

    formula: str = Field(min_length=1, description="公式原文")
    ast: dict[str, Any] | None = Field(default=None, description="語法樹，格式由公式引擎定義")
    level: CalcLevel | None = Field(
        default=None, description="計算層級，決定在運算順序的哪一步執行"
    )


class DataField(IRModel):
    """資料源中的一個欄位，可以是原始欄位或計算欄位。"""

    id: Id
    name: str = Field(min_length=1, description="Tableau 內部名稱，例如 [Calculation_123]")
    caption: str = Field(min_length=1, description="顯示名稱")
    role: Role
    datatype: DataType
    default_agg: Aggregation | None = None
    format: str | None = Field(default=None, description="Tableau 數字或日期格式字串原文")
    calc: Calc | None = None
    source_ref: SourceRef


class ExtractConnection(IRModel):
    """資料位於 .twbx 內的 .hyper 檔（嵌入式 Extract）。"""

    kind: Literal["extract"]
    hyper_file: str = Field(min_length=1, description=".twbx 內的路徑，一律用 /")
    table: str = Field(min_length=1, description="資料表，例如 Extract.Extract")


class DatasourceSettings(IRModel):
    week_start: WeekDay = Field(description="週起始日，影響週數與 DATETRUNC('week')")


# ---------------------------------------------------------------- 篩選


class CategoricalFilter(IRModel):
    """以值清單篩選。mode 為 all 時不過濾。"""

    kind: Literal["categorical"]
    id: Id
    field: FieldUse
    stage: FilterStage
    mode: Literal["include", "exclude", "all"]
    values: list[Scalar] = Field(default_factory=list)
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage not in ("extract", "datasource", "context", "dimension"):
            raise ValueError(f"categorical 篩選不能屬於 {self.stage} 階段")
        if self.mode == "all" and self.values:
            raise ValueError("mode 為 all 時 values 必須為空")
        return self


class RangeFilter(IRModel):
    """以範圍篩選；min、max 為 None 表示該側不設限。"""

    kind: Literal["range"]
    id: Id
    field: FieldUse
    stage: FilterStage
    min: Scalar = None
    max: Scalar = None
    include_null: bool = False
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage == "top_n":
            raise ValueError("range 篩選不能屬於 top_n 階段")
        return self


class TopNFilter(IRModel):
    """依某個彙總值取前 N 或後 N。"""

    kind: Literal["top_n"]
    id: Id
    field: FieldUse = Field(description="被篩選的維度")
    stage: FilterStage
    direction: Literal["top", "bottom"]
    count: Annotated[StrictInt, Field(gt=0)] | ParamRef
    by: FieldUse = Field(description="排名依據的彙總值")
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage not in ("context", "top_n"):
            raise ValueError(f"top_n 篩選不能屬於 {self.stage} 階段")
        return self


Filter = Annotated[CategoricalFilter | RangeFilter | TopNFilter, Field(discriminator="kind")]


class Datasource(IRModel):
    id: Id
    name: str = Field(min_length=1, description="Tableau 內部名稱")
    caption: str = Field(min_length=1)
    connection: ExtractConnection
    settings: DatasourceSettings
    fields: list[DataField] = Field(min_length=1)
    filters: list[Filter] = Field(default_factory=list, description="萃取篩選與資料源篩選")
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        for flt in self.filters:
            if flt.stage not in ("extract", "datasource"):
                raise ValueError(f"資料源上的篩選 {flt.id} 只能屬於 extract 或 datasource 階段")
        return self


# ---------------------------------------------------------------- 參數


class AllowedAll(IRModel):
    kind: Literal["all"]


class AllowedList(IRModel):
    kind: Literal["list"]
    values: list[Scalar] = Field(min_length=1)


class AllowedRange(IRModel):
    kind: Literal["range"]
    min: Scalar = None
    max: Scalar = None
    step: StrictInt | StrictFloat | None = None


Allowed = Annotated[AllowedAll | AllowedList | AllowedRange, Field(discriminator="kind")]


class Parameter(IRModel):
    id: Id
    name: str = Field(min_length=1, description="Tableau 內部名稱，例如 [Parameter 1]")
    caption: str = Field(min_length=1)
    datatype: DataType
    allowed: Allowed
    value: Scalar = Field(description="目前值")
    source_ref: SourceRef


# ---------------------------------------------------------------- 工作表

TableCalcType = Literal[
    "running_total",
    "percent_of_total",
    "rank",
    "difference",
    "percent_difference",
    "moving",
    "formula",
]
TableCalcPreset = Literal[
    "table_across",
    "table_down",
    "table_across_then_down",
    "table_down_then_across",
    "pane_across",
    "pane_down",
    "pane_across_then_down",
    "pane_down_then_across",
    "cell",
    "specific",
]
MarkType = Literal[
    "automatic",
    "bar",
    "line",
    "area",
    "circle",
    "square",
    "shape",
    "text",
    "pie",
    "gantt",
    "map",
    "polygon",
]


class TableCalc(IRModel):
    """Table Calculation。type 為 formula 表示計算欄位內含 Table Calculation 函式。"""

    id: Id
    type: TableCalcType
    preset: TableCalcPreset = Field(description="Tableau 的計算方向設定")
    addressing: list[Id] = Field(default_factory=list, description="解析後的計算方向欄位，依序")
    inferred: bool = Field(default=False, description="計算方向是否依 Tableau 預設規則推定")
    options: dict[str, Scalar] = Field(default_factory=dict, description="例如 rank 的方式與方向")
    source_ref: SourceRef


class Sort(IRModel):
    field: Id = Field(description="被排序的維度欄位")
    kind: Literal["by_field", "alphabetic", "manual", "data_source"]
    order: Literal["asc", "desc"] = "asc"
    by: FieldUse | None = Field(default=None, description="kind 為 by_field 時的排序依據")
    manual_values: list[Scalar] = Field(default_factory=list, description="kind 為 manual 時的順序")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.kind == "by_field") != (self.by is not None):
            raise ValueError("只有 kind 為 by_field 時必須、也只能指定 by")
        if (self.kind == "manual") != bool(self.manual_values):
            raise ValueError("只有 kind 為 manual 時必須、也只能指定 manual_values")
        return self


class Marks(IRModel):
    type: MarkType
    color: FieldUse | None = None
    size: FieldUse | None = None
    label: list[FieldUse] = Field(default_factory=list)
    detail: list[FieldUse] = Field(default_factory=list)
    tooltip: list[FieldUse] = Field(default_factory=list)


class Worksheet(IRModel):
    id: Id
    name: str = Field(min_length=1)
    datasource: Id
    rows: list[FieldUse] = Field(default_factory=list)
    columns: list[FieldUse] = Field(default_factory=list)
    marks: Marks
    filters: list[Filter] = Field(default_factory=list)
    sorts: list[Sort] = Field(default_factory=list)
    table_calcs: list[TableCalc] = Field(default_factory=list)
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        for flt in self.filters:
            if flt.stage in ("extract", "datasource"):
                raise ValueError(f"工作表上的篩選 {flt.id} 不能屬於 {flt.stage} 階段")
        return self


# ---------------------------------------------------------------- dashboard

ZoneKind = Literal[
    "container",
    "worksheet",
    "filter",
    "parameter",
    "legend",
    "text",
    "image",
    "title",
    "blank",
]
ControlType = Literal[
    "single_dropdown",
    "multi_dropdown",
    "single_list",
    "multi_list",
    "range_slider",
    "single_slider",
    "date_range",
    "type_in",
]

_ZONE_ATTRS = ("worksheet", "field", "parameter", "control", "legend", "text", "image")
_ZONE_REQUIRED: dict[str, tuple[str, ...]] = {
    "container": (),
    "worksheet": ("worksheet",),
    "filter": ("worksheet", "field", "control"),
    "parameter": ("parameter", "control"),
    "legend": ("worksheet", "legend"),
    "text": ("text",),
    "image": ("image",),
    "title": (),
    "blank": (),
}
_ZONE_OPTIONAL: dict[str, tuple[str, ...]] = {"title": ("text",)}


class Zone(IRModel):
    """dashboard 上的一個區塊；座標為換算後的像素值。"""

    id: Id
    kind: ZoneKind
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=0)
    h: int = Field(ge=0)
    worksheet: Id | None = Field(default=None, description="工作表、篩選、圖例區塊所屬的工作表")
    field: FieldUse | None = Field(default=None, description="篩選控制項的欄位")
    parameter: Id | None = Field(default=None, description="參數控制項的參數")
    control: ControlType | None = None
    legend: Literal["color", "size", "shape"] | None = None
    text: str | None = None
    image: str | None = Field(default=None, description=".twbx 內的圖片路徑")
    children: list[Zone] = Field(default_factory=list, description="只有 container 可以有子區塊")
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        required = _ZONE_REQUIRED[self.kind]
        allowed = set(required) | set(_ZONE_OPTIONAL.get(self.kind, ()))
        missing = [name for name in required if getattr(self, name) is None]
        extra = [n for n in _ZONE_ATTRS if n not in allowed and getattr(self, n) is not None]
        problems: list[str] = []
        if missing:
            problems.append(f"缺少 {', '.join(missing)}")
        if extra:
            problems.append(f"不該有 {', '.join(extra)}")
        if self.children and self.kind != "container":
            problems.append("只有 container 可以有子區塊")
        if problems:
            raise ValueError(f"{self.kind} 區塊{'；'.join(problems)}")
        return self


class Size(IRModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Dashboard(IRModel):
    id: Id
    name: str = Field(min_length=1)
    size: Size = Field(description="固定尺寸，單位為像素")
    zones: list[Zone] = Field(default_factory=list)
    source_ref: SourceRef


# ---------------------------------------------------------------- 根


class Workbook(IRModel):
    id: Id
    name: str = Field(min_length=1)
    tableau_version: str | None = None
    source_ref: SourceRef


class Unsupported(IRModel):
    """解析器不認得或 v0 不支援的元素。有任何一筆，報表就進人工佇列。"""

    source_ref: SourceRef
    reason: str = Field(min_length=1)
    element: str | None = Field(default=None, description="XML 標籤名稱")
    xml: str | None = Field(default=None, description="原始 XML 片段，可截斷")


class IR(IRModel):
    """一份 workbook 的完整 IR。"""

    ir_version: Literal["0.1"]
    workbook: Workbook
    datasources: list[Datasource] = Field(min_length=1)
    parameters: list[Parameter] = Field(default_factory=list)
    worksheets: list[Worksheet] = Field(default_factory=list)
    dashboards: list[Dashboard] = Field(default_factory=list)
    unsupported: list[Unsupported] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        from tmig.ir.integrity import check_references

        errors = check_references(self)
        if errors:
            raise ValueError("IR 參照錯誤：\n- " + "\n- ".join(errors))
        return self
