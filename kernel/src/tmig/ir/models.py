"""IR 的資料模型（v0.3）。

IR 是唯一的報表定義：解析器寫入，其他模組只讀，任何模組都不回頭讀 .twb。
規則見 Spec「IR v0 資料合約」：

- 每個實體帶穩定的 ``id`` 與 ``source_ref``（在 .twb 中的 XPath）
- 欄位一律以 ``id`` 參照；計算欄位同時保存原文 ``formula`` 與語法樹 ``ast``
- dashboard 座標存換算後的像素值
- 無法辨識的元素放進 ``unsupported``，不得默默丟棄；不影響數字、刻意略過的元素放進 ``ignored``
- 欄位異動必須升 ``ir_version``，並重新產生 ``kernel/schema/``；擴充階段內的相容增補（新增選填欄位）
  沿用 0.3，但同樣要重新產生 Schema 與 golden IR

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

IR_VERSION = "0.3"

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
Aggregation = Literal[
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "countd",
    "median",
    "attr",
    "stdev",
    "stdevp",
    "var",
    "varp",
]
DatePart = Literal["year", "quarter", "month", "week", "weekday", "day", "hour", "minute", "second"]
WeekDay = Literal["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
CalcLevel = Literal["row", "aggregate", "lod", "table_calc"]
MEASURE_NAMES = "measure_names"
MEASURE_VALUES = "measure_values"
SPECIAL_FIELDS = frozenset({MEASURE_NAMES, MEASURE_VALUES})
"""Measure Names／Values：不是資料源的欄位，只出現在工作表的欄列架與標記卡。"""

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


class MemberAlias(IRModel):
    """別名：維度成員或參數值的顯示名稱（Tableau 的成員別名、參數的「顯示為」）。"""

    value: Scalar = Field(description="原始值；Null 的別名為 null")
    label: str = Field(description="顯示的名稱")


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
    hidden: bool = False
    column: str | None = Field(default=None, description="萃取資料表中的實體欄位；計算欄位為 None")
    table: str | None = Field(
        default=None,
        description="Live 連線時欄位所屬的資料表（關聯樹中資料表的名稱）；萃取只有一張表，為 None",
    )
    calc: Calc | None = None
    aliases: list[MemberAlias] = Field(
        default_factory=list,
        description="成員別名：只改變維度成員的顯示（標題、標籤、篩選控制項），"
        "篩選、排序與計算仍用原始值",
    )
    source_ref: SourceRef

    @model_validator(mode="after")
    def _column_or_calc(self) -> Self:
        if (self.column is None) == (self.calc is None):
            raise ValueError("欄位必須剛好有 column（一般欄位）或 calc（計算欄位）其中之一")
        return self


Dialect = Literal[
    "oracle",
    "databricks",
    "spark",
    "postgres",
    "redshift",
    "tsql",
    "mysql",
    "snowflake",
    "bigquery",
    "teradata",
    "duckdb",
]
"""SQL 方言（SQLGlot 的方言名稱）。"""


class TableRelation(IRModel):
    """來源資料庫中的一張資料表；name 是它在關聯樹中的名稱（欄位以它區分所屬的資料表）。"""

    kind: Literal["table"]
    name: str = Field(min_length=1)
    table: list[str] = Field(min_length=1, description="限定名稱的各段，例如 [HR, EMPLOYEES]")


class SqlRelation(IRModel):
    """自訂 SQL：以來源資料庫的方言寫成，原文照送。"""

    kind: Literal["sql"]
    name: str = Field(min_length=1)
    sql: str = Field(min_length=1)


class JoinTerm(IRModel):
    """join 條件的一邊：資料表的實體欄位，或常值（例如 1 = [T].[dummy] 這種全部配對的寫法）。"""

    table: str | None = Field(default=None, description="資料表名稱；常值時為 None")
    column: str | None = None
    literal: Scalar = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.table is None) != (self.column is None):
            raise ValueError("欄位必須同時有 table 與 column")
        if self.table is not None and self.literal is not None:
            raise ValueError("欄位與常值只能擇一")
        return self


class JoinCondition(IRModel):
    """join 條件：左右兩邊以 op 比較。"""

    left: JoinTerm
    op: Literal["=", "!=", "<", "<=", ">", ">="] = "="
    right: JoinTerm


class JoinRelation(IRModel):
    kind: Literal["join"]
    join: Literal["inner", "left", "right", "full"]
    left: Relation
    right: Relation
    on: list[JoinCondition] = Field(min_length=1, description="以 AND 連接")


Relation = Annotated[TableRelation | SqlRelation | JoinRelation, Field(discriminator="kind")]
JoinRelation.model_rebuild()


class LogicalTable(IRModel):
    """Relationship 模型的邏輯資料表（object-graph 的 object）。"""

    id: str = Field(
        min_length=1, description="Tableau 的 object id，欄位的 table 以它表示所屬的邏輯資料表"
    )
    caption: str = Field(min_length=1)
    relation: Relation = Field(description="實體的資料表、自訂 SQL 或 join 樹")


class RelationshipCondition(IRModel):
    """關聯條件：兩張邏輯資料表的欄位（欄位 id）以 op 比較。"""

    left: str = Field(min_length=1, description="left 資料表的欄位 id")
    op: Literal["=", "!=", "<", "<=", ">", ">="] = "="
    right: str = Field(min_length=1, description="right 資料表的欄位 id")


class Relationship(IRModel):
    left: str = Field(min_length=1, description="邏輯資料表 id")
    right: str = Field(min_length=1, description="邏輯資料表 id")
    on: list[RelationshipCondition] = Field(min_length=1, description="以 AND 連接")


class ModelRelation(IRModel):
    """Relationship 模型（Tableau 2020.2 以後的資料模型）：多張邏輯資料表以關聯連成一棵樹。

    查詢時只取檢視用到的資料表；量值在自己的資料表彙總，不因其他資料表的一對多而重複計算。
    """

    kind: Literal["model"]
    name: str = Field(default="model", min_length=1)
    tables: list[LogicalTable] = Field(min_length=2)
    relationships: list[Relationship] = Field(min_length=1)


TopRelation = Annotated[
    TableRelation | SqlRelation | JoinRelation | ModelRelation, Field(discriminator="kind")
]


class LiveConnection(IRModel):
    """資料在來源資料庫（Live 連線，或萃取的來源）。只記連線資訊，不含帳號、密碼或權杖：
    憑證由部署環境依 connection_id 提供。"""

    kind: Literal["live"]
    connection_id: str = Field(
        pattern=r"^[a-z0-9_]+$", description="連線代號：部署環境以它找對應的憑證"
    )
    dbclass: str = Field(min_length=1, description="Tableau 的連線類型，例如 oracle、databricks")
    dialect: Dialect | None = Field(default=None, description="None＝尚不支援的連線類型")
    server: str | None = None
    port: int | None = None
    database: str | None = Field(
        default=None, description="資料庫；Oracle 為服務名稱，Databricks 為 catalog"
    )
    schema_name: str | None = Field(default=None, description="預設的結構描述")
    http_path: str | None = Field(default=None, description="Databricks 的 SQL warehouse 路徑")
    authentication: str | None = Field(default=None, description="Tableau 記錄的驗證方式")
    relation: TopRelation | None = Field(
        default=None, description="資料表、自訂 SQL、join 樹或 Relationship 模型"
    )


class SourceColumn(IRModel):
    """萃取中的一欄在來源的位置（萃取 metadata-record 的 family 與 remote-alias）。"""

    field: Id = Field(description="欄位 id；萃取中的欄名為該欄位的 column")
    relation: str | None = Field(
        default=None, description="來源關聯樹中的資料表（relation 的 name）；單一資料表時可省略"
    )
    column: str = Field(min_length=1, description="來源的欄名")


class ExtractRefresh(IRModel):
    """萃取層從來源資料庫重建萃取時需要的資訊（N7）；來源連線見 ExtractConnection.source。"""

    columns: list[SourceColumn] = Field(min_length=1)
    increment: Id | None = Field(
        default=None,
        description="增量更新的欄位（Tableau 的 increment-key）；None 表示只能完整更新",
    )
    row_limit: int | None = Field(
        default=None, description="萃取只取前 N 筆或抽樣（extract count）；None 表示全部"
    )


class ExtractConnection(IRModel):
    """資料位於 .twbx 內的 .hyper 檔（嵌入式 Extract）。"""

    kind: Literal["extract"]
    hyper_file: str = Field(min_length=1, description=".twbx 內的路徑，一律用 /")
    table: str = Field(min_length=1, description="資料表，例如 Extract.Extract")
    model: ModelRelation | None = Field(
        default=None,
        description="Relationship 模型的萃取：每張邏輯資料表一張資料表（table 為第一張）",
    )
    source: LiveConnection | None = Field(
        default=None, description="萃取的來源資料庫（萃取層定時更新時使用）"
    )
    refresh: ExtractRefresh | None = Field(
        default=None, description="萃取各欄在來源的位置與更新方式；來源無法解析時為 None"
    )


Connection = Annotated[ExtractConnection | LiveConnection, Field(discriminator="kind")]


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
    group: str | None = Field(
        default=None,
        description="篩選群組：同一群組的篩選一起變更（Tableau 的「套用到選取的工作表」與全域篩選）",
    )
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        # measure、table_calc：以離散的彙總計算欄位篩選（每個標記的值是否在清單中）
        if self.stage == "top_n":
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
    group: str | None = Field(
        default=None,
        description="篩選群組：同一群組的篩選一起變更（Tableau 的「套用到選取的工作表」與全域篩選）",
    )
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage == "top_n":
            raise ValueError("range 篩選不能屬於 top_n 階段")
        return self


class TopNFilter(IRModel):
    """依某個彙總運算式取前 N 或後 N；mode、values 為排名前先選取的成員（篩選對話框的「一般」頁）。"""

    kind: Literal["top_n"]
    id: Id
    field: FieldUse = Field(description="被篩選的維度")
    stage: FilterStage
    direction: Literal["top", "bottom"]
    count: Annotated[StrictInt, Field(gt=0)] | ParamRef
    by: str = Field(min_length=1, description="排名依據的 Tableau 彙總公式，例如 SUM([Sales])")
    mode: Literal["include", "exclude", "all"] = "all"
    values: list[Scalar] = Field(default_factory=list)
    group: str | None = Field(
        default=None,
        description="篩選群組：同一群組的篩選一起變更（Tableau 的「套用到選取的工作表」與全域篩選）",
    )
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage not in ("context", "top_n"):
            raise ValueError(f"top_n 篩選不能屬於 {self.stage} 階段")
        return self


class ConditionFilter(IRModel):
    """依條件篩選維度的成員（篩選對話框的「萬用字元」與「條件」頁）。

    condition 為 Tableau 公式：列層級公式逐列判斷（萬用字元，例如 CONTAINS([Region], "a")）；
    彙總公式在該維度的層級計算（例如 SUM([Sales]) > 100），與 Top N 一樣以內容篩選後、
    其他維度篩選前的資料計算。mode、values 為先選取的成員。
    """

    kind: Literal["condition"]
    id: Id
    field: FieldUse = Field(description="被篩選的維度")
    stage: FilterStage
    condition: str = Field(min_length=1, description="條件的 Tableau 公式")
    mode: Literal["include", "exclude", "all"] = "all"
    values: list[Scalar] = Field(default_factory=list)
    group: str | None = Field(default=None, description="篩選群組（見 CategoricalFilter.group）")
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage not in ("extract", "datasource", "context", "dimension"):
            raise ValueError(f"condition 篩選不能屬於 {self.stage} 階段")
        if self.mode == "all" and self.values:
            raise ValueError("mode 為 all 時 values 必須為空")
        return self


RelativePeriod = Literal["year", "quarter", "month", "week", "day"]


class RelativeDateFilter(IRModel):
    """相對日期：基準日所在期間往前（負數）或往後的 first 到 last 個期間，例如最近 3 個月為 -2 到 0。

    基準日為 anchor；沒有 anchor 時為查詢時的今天（計算比對時為抓取預期結果的日期）。
    """

    kind: Literal["relative_date"]
    id: Id
    field: FieldUse
    stage: FilterStage
    period: RelativePeriod
    first: StrictInt
    last: StrictInt
    include_future: bool = Field(default=True, description="false 時只保留到基準日（含）")
    include_null: bool = False
    anchor: str | None = Field(default=None, description="固定的基準日（ISO 日期）")
    group: str | None = Field(default=None, description="篩選群組（見 CategoricalFilter.group）")
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage not in ("extract", "datasource", "context", "dimension"):
            raise ValueError(f"relative_date 篩選不能屬於 {self.stage} 階段")
        if self.first > self.last:
            raise ValueError("first 不能大於 last")
        return self


class TupleFilter(IRModel):
    """依多個欄位的值組合篩選：篩選動作在目標工作表上的篩選、排除（Exclude）與只保留（Keep Only）。

    mode 為 all 時不過濾；include 而 tuples 為空表示全部排除（篩選動作清除時「排除所有值」）。
    """

    kind: Literal["tuple"]
    id: Id
    fields: list[FieldUse] = Field(min_length=1)
    stage: FilterStage
    mode: Literal["include", "exclude", "all"]
    tuples: list[list[Scalar]] = Field(default_factory=list)
    action: Id | None = Field(
        default=None, description="篩選動作的 id：選取來源工作表的標記時，以選取的值取代 tuples"
    )
    group: str | None = Field(default=None, description="篩選群組（見 CategoricalFilter.group）")
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.stage not in ("context", "dimension"):
            raise ValueError(f"tuple 篩選不能屬於 {self.stage} 階段")
        if self.mode == "all" and self.tuples:
            raise ValueError("mode 為 all 時 tuples 必須為空")
        if any(len(t) != len(self.fields) for t in self.tuples):
            raise ValueError("每個 tuple 的長度必須與 fields 相同")
        return self


Filter = Annotated[
    CategoricalFilter
    | RangeFilter
    | TopNFilter
    | TupleFilter
    | ConditionFilter
    | RelativeDateFilter,
    Field(discriminator="kind"),
]
FieldFilter = CategoricalFilter | RangeFilter | TopNFilter | ConditionFilter | RelativeDateFilter
"""單一欄位的篩選（TupleFilter 以外）。"""


class PublishedSource(IRModel):
    """Published Data Source：定義已由 .tdsx 併入報表（N8），原本在 Tableau Server 上。"""

    content_url: str = Field(min_length=1, description="資料源在 Tableau Server 上的網址名稱")
    site: str | None = None
    revision: str | None = None


class Datasource(IRModel):
    id: Id
    name: str = Field(min_length=1, description="Tableau 內部名稱")
    caption: str = Field(min_length=1)
    connection: Connection
    settings: DatasourceSettings
    fields: list[DataField] = Field(min_length=1)
    filters: list[Filter] = Field(default_factory=list, description="萃取篩選與資料源篩選")
    published: PublishedSource | None = Field(
        default=None, description="來自 Published Data Source（已併入）；嵌入報表的資料源為 None"
    )
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
    aliases: list[MemberAlias] = Field(
        default_factory=list, description="參數值的顯示名稱（控制項與標題中顯示）"
    )
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


class MeasureAlias(IRModel):
    """Measure Names 的別名（資料源中設定）：量值在 Measure Names 的標題與圖例中顯示的名稱。"""

    use: FieldUse = Field(description="Measure Values 卡上的量值")
    label: str


class TextRun(IRModel):
    """自訂文字的一段：文字、欄位的值（依格式顯示）或參數的值；依序串接成標籤或標題。"""

    text: str | None = None
    use: FieldUse | None = None
    parameter: Id | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if sum(v is not None for v in (self.text, self.use, self.parameter)) != 1:
            raise ValueError("text、use、parameter 必須、也只能指定一個")
        return self


class Marks(IRModel):
    type: MarkType
    color: FieldUse | None = None
    size: FieldUse | None = None
    label: list[FieldUse] = Field(default_factory=list)
    label_text: list[TextRun] = Field(
        default_factory=list,
        description="自訂的標籤文字（Tableau 的「標籤＞文字」）；空的表示依 label 的欄位顯示",
    )
    detail: list[FieldUse] = Field(default_factory=list)
    tooltip: list[FieldUse] = Field(default_factory=list)
    shape: FieldUse | None = None
    angle: FieldUse | None = Field(default=None, description="圓餅每一塊的角度（wedge-size）")
    path: FieldUse | None = Field(default=None, description="折線或多邊形的連接順序")

    def uses(self) -> list[FieldUse]:
        """標記卡上的所有欄位使用。"""
        singles = [self.color, self.size, self.shape, self.angle, self.path]
        return [u for u in singles if u is not None] + [*self.label, *self.detail, *self.tooltip]


class Pane(IRModel):
    """個別量值的標記卡：同一架上有多個量值（各自一個軸）或雙軸時，每個量值可以有自己的標記設定。"""

    measure: FieldUse = Field(description="這張標記卡對應的軸上量值")
    axis: Literal["rows", "columns"]
    dual: bool = Field(default=False, description="雙軸中的第二個軸（與前一個量值重疊）")
    marks: Marks


class ReferenceLine(IRModel):
    """參考線：在量值軸上畫出常數、參數或標記值的彙總（平均、總和、最小、最大、中位數、百分位數），
    或平均 ± 標準差的倍數。只影響圖表，不影響數字。"""

    id: str = Field(min_length=1, description="工作表內的編號，例如 refline0")
    axis: FieldUse = Field(description="畫在哪個量值的軸上")
    value: FieldUse | ParamRef | None = Field(
        default=None, description="計算的量值；參數時為參數的值；formula 為 constant 時為 None"
    )
    formula: Literal[
        "constant", "average", "sum", "total", "min", "max", "median", "percentile", "stdev"
    ]
    constant: Scalar = None
    percentiles: list[Annotated[float, Field(ge=0, le=100)]] = Field(
        default_factory=list,
        description="formula 為 percentile 時，每個百分位數（0 到 100）各畫一條線",
    )
    factors: list[float] = Field(
        default_factory=list,
        description="formula 為 stdev 時，平均加上幾倍標準差（例如 -1、1），每個倍數各畫一條線",
    )
    sample: bool = Field(default=True, description="標準差為樣本（true）或母體")
    scope: Literal["table", "pane", "cell"]
    label: Literal["none", "automatic", "value", "computation", "custom"] = "automatic"
    label_text: str | None = Field(
        default=None, description="label 為 custom 時的文字，<Value> 代入值"
    )
    source_ref: SourceRef


class Worksheet(IRModel):
    id: Id
    name: str = Field(min_length=1)
    datasource: Id
    rows: list[FieldUse] = Field(default_factory=list)
    columns: list[FieldUse] = Field(default_factory=list)
    marks: Marks
    panes: list[Pane] = Field(
        default_factory=list, description="個別量值的標記卡；空的表示所有量值共用 marks"
    )
    measure_values: list[FieldUse] = Field(
        default_factory=list, description="Measure Values 卡上的量值，依顯示順序"
    )
    measure_aliases: list[MeasureAlias] = Field(
        default_factory=list, description="Measure Names 的別名：量值在 Measure Names 中的顯示名稱"
    )
    filters: list[Filter] = Field(default_factory=list)
    sorts: list[Sort] = Field(default_factory=list)
    table_calcs: list[TableCalc] = Field(default_factory=list)
    reference_lines: list[ReferenceLine] = Field(default_factory=list)
    formats: dict[str, str] = Field(
        default_factory=dict,
        description="工作表上設定的數字格式（欄位鍵 → Tableau 格式字串），優先於欄位的預設格式",
    )
    title: list[TextRun] = Field(
        default_factory=list,
        description="自訂的工作表標題（文字與參數的值）；空的表示以工作表名稱為標題",
    )
    totals: list[Literal["rows", "columns"]] = Field(
        default_factory=list,
        description="總計（Grand Total）：rows＝列架加上總計列（每一欄的合計，在最下方），"
        "columns＝欄架加上總計欄（每一列的合計，在最右邊）",
    )
    totals_colored: bool = Field(
        default=False,
        description="依量值著色時總計也上色、納入色階的範圍（Tableau 色彩的「包含總計」）；"
        "預設與 Tableau 相同，總計不上色",
    )
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        for flt in self.filters:
            if flt.stage in ("extract", "datasource"):
                raise ValueError(f"工作表上的篩選 {flt.id} 不能屬於 {flt.stage} 階段")
        uses = [*self.rows, *self.columns, *self.marks.uses()]
        uses += [u for pane in self.panes for u in pane.marks.uses()]
        if any(u.field == MEASURE_VALUES for u in uses) and not self.measure_values:
            raise ValueError("用到 Measure Values 時必須列出 measure_values")
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
    "button",
    "web",
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
    "relative_date",
    "condition",
]
"""篩選控制項；relative_date、condition 為顯示目前的相對日期或條件（檢視器尚不能修改）。"""

_ZONE_ATTRS = (
    "worksheet",
    "field",
    "parameter",
    "control",
    "legend",
    "text",
    "image",
    "button",
    "url",
)
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
    "button": ("button",),
    "web": ("url",),
}


class Button(IRModel):
    """dashboard 按鈕（I2）：切換到 dashboard 或工作表、顯示或隱藏區塊、匯出。"""

    action: Literal["navigate", "toggle", "export"]
    target_dashboard: Id | None = Field(default=None, description="navigate：目標 dashboard")
    target_worksheet: Id | None = Field(default=None, description="navigate：目標工作表")
    zones: list[Id] = Field(
        default_factory=list, description="toggle：顯示或隱藏的區塊（同一個 dashboard 上）"
    )
    export: Literal["pdf", "image", "powerpoint"] | None = Field(
        default=None, description="export：匯出的格式"
    )
    caption: str | None = Field(default=None, description="文字按鈕的文字")
    tooltip: str | None = None
    image: str | None = Field(
        default=None, description="圖片按鈕在 .twbx 內的圖片路徑；toggle 時為區塊顯示中的圖片"
    )
    image_hidden: str | None = Field(default=None, description="toggle：區塊隱藏時的圖片")
    color: str | None = Field(default=None, description="文字顏色，例如 #ffffff")
    background: str | None = Field(default=None, description="背景顏色")

    @model_validator(mode="after")
    def _check(self) -> Self:
        targets = [t for t in (self.target_dashboard, self.target_worksheet) if t is not None]
        if (self.action == "navigate") != (len(targets) == 1) or len(targets) > 1:
            raise ValueError("navigate 按鈕必須、也只能指定一個目標 dashboard 或工作表")
        if (self.action == "toggle") != bool(self.zones):
            raise ValueError("只有 toggle 按鈕必須、也只能指定 zones")
        if (self.action == "export") != (self.export is not None):
            raise ValueError("只有 export 按鈕必須、也只能指定 export")
        return self


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
    button: Button | None = None
    url: str | None = Field(default=None, description="網頁區塊的網址")
    children: list[Zone] = Field(default_factory=list, description="只有 container 可以有子區塊")
    layout: Literal["tiled", "horizontal", "vertical"] | None = Field(
        default=None,
        description="container 的排列：tiled＝子區塊各自定位（layout-basic），horizontal／vertical＝沿水平或垂直方向排列（layout-flow）",
    )
    fixed: int | None = Field(
        default=None, ge=0, description="固定大小（像素）：沿父容器排列方向的寬或高，視窗縮放時不變"
    )
    hidden: bool = Field(default=False, description="使用者隱藏的區塊（例如以按鈕切換顯示的容器）")
    show_title: bool = Field(default=True, description="工作表區塊是否顯示標題")
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
    size: Size = Field(description="區塊座標換算時的尺寸（像素）；固定尺寸即為 dashboard 的大小")
    sizing: Literal["fixed", "automatic", "range"] = Field(
        default="fixed",
        description="fixed＝固定尺寸；automatic＝隨視窗縮放；range＝隨視窗縮放但限制在 min_size 與 max_size 之間",
    )
    min_size: Size | None = None
    max_size: Size | None = None
    zones: list[Zone] = Field(default_factory=list)
    source_ref: SourceRef


# ---------------------------------------------------------------- 根


class Workbook(IRModel):
    id: Id
    name: str = Field(min_length=1)
    tableau_version: str | None = None
    source_ref: SourceRef


class UrlField(IRModel):
    """網址動作的網址中的一個欄位參照，例如 <[Customer Name]>。"""

    placeholder: str = Field(min_length=1, description="網址中的原文，例如 <[Customer Name]>")
    key: str = Field(
        min_length=1, description="欄位鍵，例如 f_customer_name、sum(f_sales)；參數為 param:<id>"
    )


class UrlAction(IRModel):
    """網址動作：以選取的標記的值代入網址後開啟。"""

    template: str = Field(min_length=1, description="網址，欄位以 <[欄位]> 參照")
    fields: list[UrlField] = Field(default_factory=list, description="網址中的欄位參照")
    escape: bool = Field(default=True, description="代入的值是否以網址編碼")
    delimiter: str = Field(default=",", description="選取多個標記時值的分隔字元")


class ParameterAction(IRModel):
    """參數動作：選取標記時，把標記的欄位值（多個標記時依 aggregation 彙總）設給參數。"""

    parameter: Id
    field: str = Field(min_length=1, description="來源欄位的欄位鍵")
    aggregation: Literal["attr", "sum", "avg", "min", "max", "count", "countd"] = "attr"
    clearing: Literal["keep", "assign"] = Field(
        default="keep", description="清除選取時：保留參數目前的值，或設成 clear_value"
    )
    clear_value: Scalar = None


class Action(IRModel):
    """Dashboard 的動作：選取（或滑過）來源工作表的標記時執行。

    - filter：以標記的欄位值篩選目標工作表；目標工作表上對應的篩選是 action 指向這個動作的
      TupleFilter，清除選取時依 clearing 處理
    - url：以標記的欄位值代入網址後開啟
    - parameter：把標記的欄位值設給參數
    - navigate：切換到 target_dashboard 或 target_worksheet
    """

    id: Id
    name: str = Field(min_length=1, description="Tableau 內部名稱，例如 [Action1_…]")
    caption: str
    kind: Literal["filter", "url", "parameter", "navigate"]
    url: UrlAction | None = None
    parameter: ParameterAction | None = None
    activation: Literal["select", "hover", "menu"]
    dashboard: Id | None = Field(
        default=None,
        description="動作所屬的 dashboard，只在這個 dashboard 上執行；None 表示工作表層級的動作（任何地方都執行）",
    )
    sources: list[Id] = Field(min_length=1, description="來源工作表")
    targets: list[Id] = Field(default_factory=list, description="目標工作表")
    target_dashboard: Id | None = Field(
        default=None,
        description="目標是另一個 dashboard 時，執行動作後切換到該 dashboard",
    )
    target_worksheet: Id | None = Field(
        default=None, description="切換動作的目標是工作表時，切換到該工作表"
    )
    clearing: Literal["show_all", "exclude_all", "keep"] = Field(
        default="show_all",
        description="清除選取時：顯示所有值、排除所有值、保留篩選的值",
    )
    single_select: bool = Field(default=False, description="只在選取單一標記時執行")
    source_ref: SourceRef

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.kind == "url") != (self.url is not None):
            raise ValueError("網址動作必須、也只有網址動作能指定 url")
        if (self.kind == "parameter") != (self.parameter is not None):
            raise ValueError("參數動作必須、也只有參數動作能指定 parameter")
        if self.kind != "filter" and self.targets:
            raise ValueError("只有篩選動作有目標工作表")
        if self.kind == "navigate" and (self.target_dashboard is None) == (
            self.target_worksheet is None
        ):
            raise ValueError("切換動作必須指定 target_dashboard 或 target_worksheet 其中之一")
        return self


class Unsupported(IRModel):
    """解析器不認得或 v0 不支援的元素。有任何一筆，報表就進人工佇列。"""

    source_ref: SourceRef
    reason: str = Field(min_length=1)
    element: str | None = Field(default=None, description="XML 標籤名稱")
    xml: str | None = Field(default=None, description="原始 XML 片段，可截斷")


class IR(IRModel):
    """一份 workbook 的完整 IR。"""

    ir_version: Literal["0.3"]
    workbook: Workbook
    datasources: list[Datasource] = Field(min_length=1)
    parameters: list[Parameter] = Field(default_factory=list)
    worksheets: list[Worksheet] = Field(default_factory=list)
    dashboards: list[Dashboard] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    unsupported: list[Unsupported] = Field(default_factory=list)
    ignored: list[Unsupported] = Field(
        default_factory=list, description="刻意略過、不影響數字的元素，例如格式、註解、醒目提示動作"
    )

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        from tmig.ir.integrity import check_references

        errors = check_references(self)
        if errors:
            raise ValueError("IR 參照錯誤：\n- " + "\n- ".join(errors))
        return self
