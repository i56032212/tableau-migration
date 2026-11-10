"""盤點報告的資料模型（輸出為 JSON）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CENSUS_VERSION = "1"


class PackagedFileInfo(BaseModel):
    name: str
    size: int


class FormulaInfo(BaseModel):
    datasource: str
    field: str = Field(description="顯示名稱")
    name: str = Field(description="Tableau 內部名稱")
    formula: str
    functions: list[str]
    lod: list[str]
    table_calc_functions: list[str]
    user_functions: list[str]
    outside_v0: list[str]
    scan_errors: list[str]


class DatasourceInfo(BaseModel):
    name: str
    caption: str
    published: bool = Field(description="Published Data Source（sqlproxy 連線）")
    connection_classes: list[str] = Field(description="具名連線的類型，例如 oracle、hyper")
    data_in_hyper: bool = Field(
        description="資料位於 .twbx 內的萃取檔（Extract 或 Hyper 檔資料源）"
    )
    extract_enabled: bool | None = Field(description="是否有啟用的 Extract；沒有 Extract 時為 None")
    extract_files: list[str] = Field(
        description="萃取或資料檔在 .twbx 內的路徑（.hyper 或舊版 .tde）"
    )
    extract_tables: list[str] = Field(description="萃取或資料檔中的資料表")
    data_files: list[str] = Field(
        default_factory=list,
        description="沒有萃取的檔案型連線（Excel、文字檔）在 .twbx 內的檔案",
    )
    source_relation_types: list[str] = Field(description="來源連線的關聯類型：table、join、text…")
    logical_tables: int = Field(description="Relationship 模型中的邏輯資料表數")
    custom_sql: bool
    field_count: int
    calc_count: int
    groups_or_sets: int = Field(
        description="尚未支援的群組數量（合併欄位等；不含集合、使用者篩選與 Tableau 自動產生的群組）"
    )
    user_filters: list[str] = Field(
        default_factory=list, description="Tableau 的使用者篩選（伺服器 → 建立使用者篩選）"
    )
    datasource_filters: int
    extract_filters: int
    filter_user_functions: list[str] = Field(description="資料源篩選中出現的使用者函式")
    filter_parameters: list[str] = Field(
        default_factory=list, description="用到使用者函式的資料源篩選中出現的參數"
    )
    authentication: list[str] = Field(
        default_factory=list,
        description="來源連線的驗證方式（.twb 的 authentication，例如 sspi、oauth）",
    )
    server_auth_modes: list[str] = Field(
        default_factory=list,
        description="Tableau Server 的驗證設定（.twb 的 workgroup-auth-mode，例如 prompt；as-is 不列）",
    )
    initial_sql: bool = Field(default=False, description="來源連線設定了 Initial SQL")
    initial_sql_user: list[str] = Field(
        default_factory=list,
        description="Initial SQL 中用到的使用者參數，例如 [TableauServerUser]",
    )


class ServerConnection(BaseModel):
    """Tableau Server 上報表或 Published Data Source 的連線設定（REST API；不含帳號與密碼）。"""

    model_config = ConfigDict(extra="ignore")

    type: str = Field(description="連線類型，例如 oracle、databricks")
    datasource: str | None = Field(default=None, description="資料源名稱")
    published_datasource: str | None = Field(
        default=None,
        description="屬於報表連到的 Published Data Source 時，報表中這個資料源的名稱",
    )
    embed_password: bool | None = Field(
        default=None, description="是否嵌入密碼；False 表示檢視者要用自己的帳號連線"
    )
    authentication: str | None = Field(
        default=None, description="驗證方式（REST API 的 authenticationType，新版伺服器才有）"
    )


class ParameterInfo(BaseModel):
    name: str
    caption: str
    datatype: str
    domain: str = Field(description="list、range、all 或 Tableau 原始值")
    value: str | None


class FilterInfo(BaseModel):
    column: str
    kind: str = Field(
        description="categorical、range、measure_range、calc_range、top_n、condition、relative_date"
    )
    context: bool
    detail: str


class TableCalcInfo(BaseModel):
    column: str
    type: str | None = Field(description="Tableau 的 table-calc type；計算欄位內的函式為 None")
    ordering_type: str | None


class WorksheetInfo(BaseModel):
    name: str
    datasources: list[str]
    blending: bool
    mark_types: list[str]
    pane_count: int
    encodings: list[str]
    rows: str
    cols: str
    shelf_operators: list[str]
    measure_names_values: bool
    filters: list[FilterInfo]
    table_calcs: list[TableCalcInfo]
    sorts: list[str]
    reference_lines: int
    trend_lines: int
    maps: bool


class DashboardInfo(BaseModel):
    name: str
    story: bool
    sizing: str = Field(description="fixed、automatic 或其他 Tableau 尺寸模式")
    size: dict[str, str]
    zone_types: dict[str, int]
    worksheets: list[str]
    device_layouts: bool


class ActionInfo(BaseModel):
    caption: str
    kind: str = Field(description="filter、highlight 或 Tableau 原始指令")
    activation: str | None
    source: str


class Check(BaseModel):
    key: str
    title: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class CensusReport(BaseModel):
    census_version: Literal["1"] = "1"
    file: str
    twb: str
    packaged: bool = Field(description="輸入是否為 .twbx（含資料）")
    tableau_version: str | None
    files: list[PackagedFileInfo]
    datasources: list[DatasourceInfo]
    parameters: list[ParameterInfo]
    worksheets: list[WorksheetInfo]
    dashboards: list[DashboardInfo]
    actions: list[ActionInfo]
    formulas: list[FormulaInfo]
    functions: dict[str, int] = Field(description="函式 → 用到它的計算欄位數")
    functions_outside_v0: list[str]
    lod: dict[str, int] = Field(description="LOD 種類 → 出現次數")
    calc_classes: dict[str, int]
    checks: list[Check]
    grade: Literal["A", "B", "C"]
    grade_reasons: list[str]
    element_inventory: dict[str, int] = Field(description="工作表與 dashboard 內各 XML 標籤的數量")
