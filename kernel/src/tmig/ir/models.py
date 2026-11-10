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


class SetMembers(IRModel):
    """固定成員的集合：列出的成員在集合中（mode 為 exclude 時，列出的以外在集合中）。

    查詢時以解析器產生的布林公式（DataField.calc）計算；成員另外列在這裡，供集合動作改變成員時使用。"""

    kind: Literal["members"]
    mode: Literal["include", "exclude"] = "include"
    members: list[list[Scalar]] = Field(
        default_factory=list, description="每個成員在各層級的值（與 levels 對應）"
    )


class SetTop(IRModel):
    """Top N 集合：依彙總公式排名，取前（或後）N 個成員；mode、values 為排名前先選取的成員。"""

    kind: Literal["top_n"]
    direction: Literal["top", "bottom"]
    count: Annotated[StrictInt, Field(gt=0)] | ParamRef
    by: str = Field(min_length=1, description="排名依據的 Tableau 彙總公式，例如 SUM([Sales])")
    mode: Literal["include", "exclude", "all"] = "all"
    values: list[Scalar] = Field(default_factory=list)


class SetCondition(IRModel):
    """條件集合：彙總條件成立的成員，例如 SUM([Profit]) > 0；mode、values 為先選取的成員。"""

    kind: Literal["condition"]
    condition: str = Field(min_length=1, description="Tableau 彙總公式（布林）")
    mode: Literal["include", "exclude", "all"] = "all"
    values: list[Scalar] = Field(default_factory=list)


SetRule = Annotated[SetMembers | SetTop | SetCondition, Field(discriminator="kind")]


class FieldSet(IRModel):
    """集合：維度成員（或成員的組合）是否在集合中，值為 TRUE／FALSE（Tableau 顯示為 In／Out）。

    Top N 與條件集合與 Top N、條件篩選相同，以內容篩選後、維度篩選前的資料決定成員；
    固定成員的集合只看成員本身。"""

    levels: list[FieldUse] = Field(min_length=1, description="集合所在的維度")
    rule: SetRule


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
    set: FieldSet | None = Field(
        default=None,
        description="集合（布林）：固定成員的集合同時有 calc（成員的公式），"
        "Top N 與條件集合沒有 calc，查詢時另外計算成員",
    )
    aliases: list[MemberAlias] = Field(
        default_factory=list,
        description="成員別名：只改變維度成員的顯示（標題、標籤、篩選控制項），"
        "篩選、排序與計算仍用原始值",
    )
    source_ref: SourceRef

    @model_validator(mode="after")
    def _column_or_calc(self) -> Self:
        computed = self.set is not None and self.set.rule.kind != "members"
        if sum(x for x in (self.column is not None, self.calc is not None, computed)) != 1:
            raise ValueError(
                "欄位必須剛好有 column（一般欄位）、calc（計算欄位）或 Top N、條件集合其中之一"
            )
        if self.set is not None and self.datatype != "boolean":
            raise ValueError("集合的型別必須是 boolean")
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


class MemberRange(IRModel):
    """連續的一段成員（含頭尾）：在篩選清單中連續勾選時，Tableau 記成從 start 到 end。"""

    start: Scalar
    end: Scalar


class CategoricalFilter(IRModel):
    """以值清單篩選。mode 為 all 時不過濾。"""

    kind: Literal["categorical"]
    id: Id
    field: FieldUse
    stage: FilterStage
    mode: Literal["include", "exclude", "all"]
    values: list[Scalar] = Field(default_factory=list)
    ranges: list[MemberRange] = Field(
        default_factory=list,
        description="另外列出的連續成員（與 values 一起只保留或排除），依值的大小比較",
    )
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
        if self.mode == "all" and (self.values or self.ranges):
            raise ValueError("mode 為 all 時 values、ranges 必須為空")
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
    "percentile",
    "difference",
    "percent_difference",
    "percent_from",
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


class NestedTableCalc(IRModel):
    """巢狀計算：公式中參照的另一個 Table Calculation 計算欄位，在這裡的計算方向
    （Tableau「編輯 Table Calculation」中「巢狀計算」各自的設定）。"""

    field: Id = Field(description="被參照的 Table Calculation 計算欄位")
    preset: TableCalcPreset
    addressing: list[Id] = Field(default_factory=list, description="解析後的計算方向欄位，依序")
    instances: list[FieldUse] = Field(
        default_factory=list,
        description="定址的欄位實例，依序；有值時以它為準（同一個欄位有多個實例時只定址其中幾個，"
        "例如窗格的計算只定址最內層的月、不含年）",
    )
    inferred: bool = False


class TableCalc(IRModel):
    """Table Calculation。type 為 formula 表示計算欄位內含 Table Calculation 函式。"""

    id: Id
    type: TableCalcType
    preset: TableCalcPreset = Field(description="Tableau 的計算方向設定")
    addressing: list[Id] = Field(default_factory=list, description="解析後的計算方向欄位，依序")
    instances: list[FieldUse] = Field(
        default_factory=list,
        description="定址的欄位實例，依序；有值時以它為準（同一個欄位有多個實例時只定址其中幾個，"
        "例如窗格的計算只定址最內層的月、不含年）",
    )
    level: FieldUse | None = Field(
        default=None,
        description="「計算層級」（level-address）：定址中比它更內層的維度改為分割，例如年增率在年的層級",
    )
    restart: FieldUse | None = Field(
        default=None,
        description="「重新開始間隔」（level-break）：定址中它與它之前的維度改為分割，例如年初至今",
    )
    inferred: bool = Field(default=False, description="計算方向是否依 Tableau 預設規則推定")
    options: dict[str, Scalar] = Field(default_factory=dict, description="例如 rank 的方式與方向")
    nested: list[NestedTableCalc] = Field(
        default_factory=list,
        description="巢狀計算各自的計算方向；公式參照的 Table Calculation 計算欄位沒有列出時與外層相同",
    )
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


class TextStyle(IRModel):
    """一段文字的格式（Tableau 格式化文字中 run 的屬性）；沒有寫的沿用預設。"""

    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    size: float | None = Field(default=None, gt=0, description="字級（pt）")
    color: str | None = Field(default=None, description="#RRGGBB")
    font: str | None = Field(default=None, description="字型名稱，例如 Tableau Bold")
    align: Literal["left", "center", "right"] | None = Field(default=None, description="段落的對齊")


class TextRun(IRModel):
    """自訂文字的一段：文字、欄位的值（依格式顯示）或參數的值；依序串接成標籤或標題。"""

    text: str | None = None
    use: FieldUse | None = None
    parameter: Id | None = None
    style: TextStyle | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if sum(v is not None for v in (self.text, self.use, self.parameter)) != 1:
            raise ValueError("text、use、parameter 必須、也只能指定一個")
        return self


HexColor = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}([0-9a-f]{2})?$")]
"""色彩：#rrggbb，可加上不透明度 #rrggbbaa（小寫）。"""

FontElement = Literal[
    "worksheet", "label", "cell", "field-labels", "title", "legend", "legend-title", "tooltip"
]
"""工作表上可以設定字型的元素（Tableau 的 Format > Font）：worksheet 為整張工作表的預設，label 為標題
成員與座標軸的刻度，cell 為文字表的儲存格，field-labels 為欄位標籤，title 為工作表標題。"""


class FontStyle(IRModel):
    """字型的設定；沒有寫的沿用上一層（活頁簿 → 工作表 → 元素 → 欄位）與 Tableau 的預設。"""

    family: str | None = Field(default=None, description="字型名稱，例如 Tableau Book、Arial")
    size: float | None = Field(default=None, gt=0, description="字級（pt）")
    bold: bool | None = None
    italic: bool | None = None


class ColorAssignment(IRModel):
    """離散色彩中指定給一個值的顏色。"""

    value: Scalar = Field(
        description="欄位的值（Null 為 None，日期為 ISO 8601 字串）；Measure Names 時為量值的欄位鍵"
    )
    color: HexColor


class ColorPalette(IRModel):
    """顏色欄位的色彩設定（Tableau 的「編輯色彩」）。

    離散：指定給各值的顏色，其餘的值依 palette 的順序。連續：內建色盤（palette）或自訂的顏色（colors），
    可以反轉、分段，並指定範圍的起訖與發散色盤的中心。"""

    kind: Literal["discrete", "continuous"]
    palette: str | None = Field(
        default=None, description="Tableau 內建色盤的內部名稱，例如 tableau10_10_0、blue_10_0"
    )
    assignments: list[ColorAssignment] = Field(default_factory=list)
    colors: list[HexColor] = Field(
        default_factory=list, description="自訂的連續色盤，由小到大；空的表示用 palette"
    )
    diverging: bool = Field(default=False, description="自訂的連續色盤是發散型（有中心）")
    reverse: bool = False
    steps: int | None = Field(default=None, ge=2, description="分段色彩的段數；None 為連續")
    start: float | None = Field(default=None, description="範圍的起點；None 依資料的最小值")
    end: float | None = Field(default=None, description="範圍的終點；None 依資料的最大值")
    center: float | None = Field(default=None, description="發散色盤的中心；None 為 0")
    full_range: bool = Field(
        default=False,
        description="發散色盤兩端都用到最深的顏色（Use Full Color Range）；"
        "預設離中心較近的一端只用到相同比例的深淺",
    )


class Marks(IRModel):
    type: MarkType
    color: FieldUse | None = None
    color_palette: ColorPalette | None = Field(
        default=None, description="顏色欄位的色彩設定；None 依 Tableau 的自動色彩"
    )
    mark_color: HexColor | None = Field(
        default=None, description="沒有顏色欄位時標記的顏色（標記卡上的「色彩」）"
    )
    opacity: float | None = Field(
        default=None, ge=0, le=1, description="標記的不透明度；None 為不透明"
    )
    stroke: HexColor | None = Field(
        default=None, description="標記的框線顏色（色彩的「效果＞框線」）；None 為沒有框線"
    )
    label_font: FontStyle | None = Field(
        default=None, description="標記標籤的字型（標記卡的 datalabel）；None 依工作表"
    )
    label_match: bool = Field(
        default=False, description="標記標籤的顏色與標記相符（datalabel 的 color-mode='match'）"
    )
    trend: TrendLine | None = Field(default=None, description="趨勢線；None 為沒有")
    mark_size: float | None = Field(
        default=None,
        gt=0,
        description="標記卡「大小」滑桿的值（.twb 的 size）；None 為 Tableau 的預設（長條占列高的 75%）",
    )
    size: FieldUse | None = None
    label: list[FieldUse] = Field(default_factory=list)
    label_text: list[TextRun] = Field(
        default_factory=list,
        description="自訂的標籤文字（Tableau 的「標籤＞文字」）；空的表示依 label 的欄位顯示",
    )
    label_color: HexColor | None = Field(
        default=None, description="標記標籤的文字顏色；None 依工作表的文字顏色"
    )
    cell_align: Literal["left", "center", "right"] | None = Field(
        default=None,
        description="文字表儲存格中文字的水平對齊（Format > 窗格的對齊）；None 依工作表的設定或預設",
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
    """參考線、參考區間、分布與盒鬚圖（分析的「參考線」）：在量值軸上畫出常數、參數或標記值的彙總
    （平均、總和、最小、最大、中位數），或一組值的分布（百分位數、平均 ± 標準差的倍數、分位數、
    信賴區間、彙總值的百分比），分布的各值之間塗上底色。兩條參考線以 paired 配成參考區間（之間塗上
    底色）。kind 為 boxplot 時是盒鬚圖。只影響圖表，不影響數字。"""

    id: str = Field(min_length=1, description="工作表內的編號，例如 refline0")
    axis: FieldUse = Field(description="畫在哪個量值的軸上")
    value: FieldUse | ParamRef | None = Field(
        default=None, description="計算的量值；參數時為參數的值；formula 為 constant 時為 None"
    )
    formula: Literal[
        "constant",
        "average",
        "sum",
        "total",
        "min",
        "max",
        "median",
        "percentile",
        "stdev",
        "quantiles",
        "confidence",
        "median_confidence",
    ]
    kind: Literal["line", "distribution", "boxplot"] = Field(
        default="line", description="參考線（一個值）、分布（多個值，之間塗上底色）或盒鬚圖"
    )
    constant: Scalar = None
    percentiles: list[Annotated[float, Field(ge=0, le=100)]] = Field(
        default_factory=list,
        description="formula 為 percentile 時，每個百分位數（0 到 100）各畫一條線",
    )
    factors: list[float] = Field(
        default_factory=list,
        description="formula 為 stdev 時，平均加上幾倍標準差（例如 -1、1），每個倍數各畫一條線",
    )
    percentages: list[float] = Field(
        default_factory=list,
        description="彙總值的百分比分布（例如平均的 50%、100%），每個百分比各一個值",
    )
    tiles: int | None = Field(default=None, ge=2, description="formula 為 quantiles 時的分位數個數")
    probability: float = Field(default=95, gt=0, lt=100, description="信賴區間的信賴水準（%）")
    sample: bool = Field(default=True, description="標準差為樣本（true）或母體")
    scope: Literal["table", "pane", "cell"]
    label: Literal["none", "automatic", "value", "computation", "custom"] = "automatic"
    label_text: str | None = Field(
        default=None, description="label 為 custom 時的文字，<Value> 代入值"
    )
    label_format: str | None = Field(
        default=None, description="標籤中值的數字格式（Tableau 的格式字串）；None 依量值的格式"
    )
    paired: str | None = Field(
        default=None, description="參考區間：與這條參考線（id）之間塗上底色（paired-id）"
    )
    fill_above: bool = Field(default=False, description="最大的值以上塗上底色（fill-above）")
    fill_below: bool = Field(default=False, description="最小的值以下塗上底色（fill-below）")
    line_visible: bool | None = Field(
        default=None, description="畫出線；None 依預設（參考線畫、分布與參考區間不畫）"
    )
    line_color: HexColor | None = None
    line_width: int | None = Field(default=None, ge=1)
    line_pattern: Literal["solid", "dashed", "dotted"] | None = None
    fill_color: HexColor | None = Field(
        default=None, description="參考區間與分布的底色（refband 的 fill-color）；None 依預設的灰色"
    )
    above_color: HexColor | None = Field(default=None, description="fill-above 的顏色")
    below_color: HexColor | None = Field(default=None, description="fill-below 的顏色")
    whiskers: Literal["standard", "minmax"] = Field(
        default="standard",
        description="盒鬚圖的鬚：standard 延伸到 1.5 倍四分位距內最遠的值，minmax 到最小與最大值",
    )
    hide_marks: bool = Field(
        default=False, description="盒鬚圖隱藏底下的標記（離群值除外，boxplot-mark-exclusion）"
    )
    source_ref: SourceRef


class Axis(IRModel):
    """一個欄位在欄架或列架上的座標軸（連續欄位）或標題（離散欄位的成員）設定：Tableau 的
    「編輯座標軸」與「顯示標題」。沒有寫的依預設（顯示、以欄位名稱為標題、範圍依資料）。"""

    use: FieldUse
    shelf: Literal["rows", "columns"]
    show: bool = Field(default=True, description="顯示座標軸或標題（Show Header）")
    title: str | None = Field(
        default=None, description="自訂的座標軸標題；空字串表示不顯示標題，None 依欄位名稱"
    )
    start: Scalar = Field(
        default=None, description="固定範圍的起點（數值，或日期的 ISO 8601 字串）；None 依資料"
    )
    end: Scalar = Field(default=None, description="固定範圍的終點；None 依資料")
    reverse: bool = Field(default=False, description="反轉座標軸")
    include_zero: bool = Field(default=True, description="自動範圍包含 0（Tableau 的預設）")
    synchronized: bool = Field(
        default=False, description="雙軸與前一個軸同步（Synchronize Axis）：兩個量值使用相同的刻度"
    )
    label_color: HexColor | None = Field(
        default=None, description="座標軸刻度或標題（離散欄位的成員）文字的顏色；None 依工作表"
    )
    size: int | None = Field(
        default=None,
        ge=0,
        description="標題或座標軸區域的寬度（列）或高度（欄），像素（拖曳標題邊界調整的大小）；None 依內容",
    )
    cell: int | None = Field(
        default=None,
        ge=1,
        description="離散欄位每個成員的高度（列）或寬度（欄），像素（Format > Cell Size）；None 依預設",
    )
    align: Literal["left", "center", "right"] | None = Field(
        default=None, description="列標題成員的水平對齊；None 依預設（靠左）"
    )
    font: FontStyle | None = Field(
        default=None, description="標題成員或座標軸刻度的字型（這個欄位的設定）；None 依工作表"
    )
    valign: Literal["top", "center", "bottom"] | None = Field(
        default=None,
        description="外層的標題成員在各組中的垂直位置；None 依工作表的設定或預設（上）",
    )
    angle: int | None = Field(
        default=None, description="標題成員的文字方向（度，-90 為由下往上）；None 為橫寫"
    )


class Banding(IRModel):
    """列或欄的色帶（格式設定的「網底＞列色帶、欄色帶」）：每 size 列（欄）為一段、隔段上色，
    窗格與標題可以不同顏色（None 為不上色）。"""

    pane: HexColor | None = None
    header: HexColor | None = None
    size: int = Field(default=1, ge=1, description="每一段的列（欄）數")
    level: int | None = Field(
        default=None,
        ge=1,
        description="以前幾層標題的組為單位上色（band-level）；None 或大於層數時為每一列（欄）",
    )


class TrendLine(IRModel):
    """趨勢線（分析的「趨勢線」）：以標記在連續座標軸上的位置做迴歸（縱軸的值對橫軸的值），每個窗格一條，
    依顏色分組時每個顏色一條；畫在資料的範圍內。"""

    fit: Literal["linear", "log", "exp", "power", "polynomial"] = "linear"
    degree: int = Field(default=3, ge=2, le=8, description="多項式的次數")
    by_color: bool = Field(default=True, description="依顏色分組（exclude-color='false'）")
    intercept: bool = Field(
        default=True, description="模型含截距；False 為「通過原點」（exclude-intercept='true'）"
    )
    confidence_bands: bool = Field(
        default=False, description="畫出 95% 信賴區間的上下兩條線（enable-confidence-bands）"
    )
    tooltips: bool = Field(
        default=True, description="停在線上時顯示模型的公式、R 平方與 P 值（enable-tooltips）"
    )
    excluded: list[FieldUse] = Field(
        default_factory=list,
        description="不分組的因子（<excluded-factors>）：這些維度的各個值共用一個模型",
    )
    color: HexColor | None = Field(default=None, description="線的顏色；None 依顏色分組或預設")
    width: int = Field(default=1, ge=1, description="線寬（像素）")
    pattern: Literal["solid", "dashed", "dotted"] = "solid"


class ZeroLine(IRModel):
    """零線（格式設定的「線條＞零線」）：連續座標軸的範圍包含 0 時畫在 0 的位置。Tableau 預設畫
    （1 像素的灰色點線，依 Tableau 的畫面量測）。"""

    color: HexColor = "#cbcbcb"
    width: int = Field(default=1, ge=1, description="線寬（像素）")
    pattern: Literal["solid", "dashed", "dotted"] = "dotted"


class Divider(IRModel):
    """列或欄的分隔線（格式設定的「框線＞列分隔線、欄分隔線」）：畫在前 level 層（1 為最外層）標題
    的成員改變處；level 為 None 時依 Tableau 的預設，即最內層以外的每一層（只有一層標題時不畫）；
    level 大於標題的層數時為每一列（欄）之間。"""

    color: HexColor = "#d4d4d4"
    width: int = Field(default=1, ge=1, description="線寬（像素）")
    level: int | None = Field(
        default=None,
        ge=1,
        description="分隔第幾層的標題；None 為最內層以外的每一層（Tableau 的預設）",
    )


class ShelfStyle(IRModel):
    """一個方向（列＝縱軸、欄＝橫軸）的格式：格線、座標軸線與刻度是否顯示（Tableau 的格式設定），
    離散欄位的欄位標籤（標題上方或左側的欄位名稱）是否顯示，以及大小、色帶與分隔線。"""

    grid: bool = True
    axis: bool = True
    ticks: bool = True
    field_labels: bool = Field(default=True, description="顯示欄位標籤（Show Field Labels）")
    cell: int | None = Field(
        default=None,
        ge=1,
        description="每一列的高度（列）或每一欄的寬度（欄），像素（Format > Cell Size）；離散的標題沒有"
        "設定時依預設，連續的座標軸沒有設定時填滿",
    )
    banding: Banding | None = None
    divider: Divider | None = Field(
        default_factory=Divider, description="窗格的分隔線（Tableau 預設畫；None 為不畫）"
    )
    header_divider: Divider | None = Field(
        default_factory=Divider, description="標題的分隔線（Tableau 預設畫；None 為不畫）"
    )
    zero_line: ZeroLine | None = Field(
        default_factory=ZeroLine, description="零線（Tableau 預設畫；None 為不畫）"
    )


FitMode = Literal["standard", "fit_width", "fit_height", "entire_view"]
"""工作表的顯示大小（Tableau 工具列的「調整」）：standard＝離散的列與欄維持固定的大小（超出時捲動），
連續的座標軸填滿；fit_width／fit_height 另外讓寬度／高度符合區塊；entire_view 兩者都符合。"""


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
    subtotals: list[FieldUse] = Field(
        default_factory=list,
        description="小計：欄列架上這些離散維度的每個值之後加一列（或一欄）合計，在去掉同一架上"
        "之後維度的詳細程度重新彙總（Tableau 的「自動」）",
    )
    axes: list[Axis] = Field(
        default_factory=list, description="座標軸與標題的設定（沒有設定的欄位依預設）"
    )
    row_style: ShelfStyle = Field(
        default_factory=ShelfStyle, description="列（縱軸）的格線、座標軸線、刻度與欄位標籤"
    )
    column_style: ShelfStyle = Field(
        default_factory=ShelfStyle, description="欄（橫軸）的格線、座標軸線、刻度與欄位標籤"
    )
    background: HexColor | None = Field(
        default=None, description="工作表的背景顏色；可以透明（#rrggbb00），None 為白色"
    )
    text_color: HexColor | None = Field(
        default=None,
        description="工作表文字的預設顏色（格式設定的「工作表」字型，或活頁簿的預設）：座標軸、"
        "標題、圖例與標記上的文字；None 為 Tableau 的預設",
    )
    cell_color: HexColor | None = Field(
        default=None, description="文字表儲存格中文字的顏色（「窗格」字型）；None 依 text_color"
    )
    cell_align: Literal["left", "center", "right"] | None = Field(
        default=None,
        description="文字表儲存格中文字的水平對齊（工作表 <style> 中的設定，標記卡的 cell_align 優先）；"
        "None 依 Tableau 的預設（有標題的文字表數字靠右、文字靠左，只有一格時置中）",
    )
    label_valign: Literal["top", "center", "bottom"] | None = Field(
        default=None, description="標題成員的垂直對齊（整張工作表的設定）；None 為上"
    )
    fonts: dict[FontElement, FontStyle] = Field(
        default_factory=dict,
        description="各元素的字型（活頁簿的設定在前、工作表的覆寫）；沒有設定的元素依 Tableau 的預設",
    )
    field_label_background: HexColor | None = Field(
        default=None, description="欄位標籤的背景顏色（列標題上方的欄位名稱）；None 為不上色"
    )
    fit: FitMode = Field(default="standard", description="工作表本身（工作表分頁）的顯示大小")
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


class ZoneStyle(IRModel):
    """dashboard 區塊的格式：背景、框線、外距（框線之外）與內距（框線之內），單位為像素。

    區塊的座標是含外距的範圍：背景與框線畫在外距之內，內容再往內縮內距（容器中的子區塊已依此
    定位）。沒有寫的部分為 0、沒有框線、透明的背景；Tableau 為並排的區塊寫出外距 4，浮動的區塊與
    容器通常不寫（沒有外距）。"""

    background: HexColor | None = None
    border: Literal["none", "solid", "dashed", "dotted"] = "none"
    border_width: int = Field(default=0, ge=0)
    border_color: HexColor | None = None
    margin: tuple[int, int, int, int] = Field(
        default=(0, 0, 0, 0), description="外距：上、右、下、左"
    )
    padding: tuple[int, int, int, int] = Field(
        default=(0, 0, 0, 0), description="內距：上、右、下、左"
    )


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
    content: list[TextRun] = Field(
        default_factory=list,
        description="文字物件與標題的內容：依序的文字（換行為 \\n）與參數目前的值，附各段的格式；"
        "空的表示只有 text",
    )
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
    style: ZoneStyle | None = Field(
        default=None, description="區塊的格式（背景、框線、外距與內距）；None 表示都沒有"
    )
    fit: FitMode | None = Field(
        default=None, description="工作表區塊中的顯示大小；None 依工作表本身的設定"
    )
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
    background: HexColor | None = Field(
        default=None, description="dashboard 的背景顏色；None 為白色"
    )
    zones: list[Zone] = Field(default_factory=list)
    source_ref: SourceRef


# ---------------------------------------------------------------- Story


Mark = dict[str, Scalar]
"""一個標記：欄位鍵 → 值（與 tmig.planner.state.QueryState.actions 相同的寫法）。"""


class StoryAction(IRModel):
    """故事點記下的篩選動作選取：以這些標記執行動作（篩選目標工作表）。"""

    action: Id
    marks: list[Mark] = Field(min_length=1)


class StoryFilter(IRModel):
    """故事點記下的工作表篩選：只保留（或排除）列出的值。"""

    worksheet: Id
    field: FieldUse
    mode: Literal["include", "exclude"]
    values: list[Scalar] = Field(default_factory=list)


class StorySelection(IRModel):
    """故事點記下的選取：工作表上醒目顯示的標記。"""

    worksheet: Id
    marks: list[Mark] = Field(min_length=1)


class StoryPoint(IRModel):
    """故事點：顯示一個 dashboard 或工作表，以及與其預設狀態不同的篩選與選取。

    狀態取自擷取時記下的差異；擷取後又改過但沒有更新故事點時，取改過的狀態（與開啟報表時相同）。"""

    id: Id
    caption: str = Field(default="", description="導覽上的說明文字；空的表示以序號導覽")
    dashboard: Id | None = None
    worksheet: Id | None = None
    actions: list[StoryAction] = Field(default_factory=list)
    filters: list[StoryFilter] = Field(default_factory=list)
    selections: list[StorySelection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.dashboard is None) == (self.worksheet is None):
            raise ValueError("故事點必須剛好顯示一個 dashboard 或工作表")
        return self


class Story(IRModel):
    """Story（故事）：依序切換的故事點。"""

    id: Id
    name: str = Field(min_length=1)
    title: str | None = Field(
        default=None, description="標題的文字；None 表示不顯示標題（沒有標題區塊）"
    )
    size: Size
    navigation: Literal["caption", "number", "dots"] = Field(
        default="caption", description="導覽的樣式：說明文字、序號或圓點"
    )
    arrows: bool = Field(default=True, description="導覽兩側是否有上一個、下一個的箭頭")
    points: list[StoryPoint] = Field(min_length=1)
    active: Id | None = Field(
        default=None, description="開啟時顯示的故事點（儲存報表時停留的故事點）；None 表示第一個"
    )
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


class SetAction(IRModel):
    """集合動作：選取標記時，以標記在集合各層級的值改變集合的成員（固定成員的集合）。"""

    target: Id = Field(description="目標集合的欄位 id")
    behavior: Literal["assign", "add", "remove"] = Field(
        default="assign", description="以選取的值取代集合的成員、加入集合或從集合移除"
    )
    clearing: Literal["keep", "add_all", "remove_all"] = Field(
        default="keep", description="清除選取時：保留集合的成員、加入所有值、移除所有值"
    )


class Action(IRModel):
    """Dashboard 的動作：選取（或滑過）來源工作表的標記時執行。

    - filter：以標記的欄位值篩選目標工作表；目標工作表上對應的篩選是 action 指向這個動作的
      TupleFilter，清除選取時依 clearing 處理
    - url：以標記的欄位值代入網址後開啟
    - parameter：把標記的欄位值設給參數
    - navigate：切換到 target_dashboard 或 target_worksheet
    - set：以標記的欄位值改變集合的成員（用到這個集合的工作表隨之改變）
    """

    id: Id
    name: str = Field(min_length=1, description="Tableau 內部名稱，例如 [Action1_…]")
    caption: str
    kind: Literal["filter", "url", "parameter", "navigate", "set"]
    url: UrlAction | None = None
    parameter: ParameterAction | None = None
    set_action: SetAction | None = None
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
        if (self.kind == "set") != (self.set_action is not None):
            raise ValueError("集合動作必須、也只有集合動作能指定 set_action")
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
    stories: list[Story] = Field(default_factory=list)
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
