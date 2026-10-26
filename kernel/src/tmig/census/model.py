"""盤點報告的資料模型（輸出為 JSON）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
    source_relation_types: list[str] = Field(description="來源連線的關聯類型：table、join、text…")
    logical_tables: int = Field(description="Relationship 模型中的邏輯資料表數")
    custom_sql: bool
    field_count: int
    calc_count: int
    groups_or_sets: int = Field(description="群組與集合的數量")
    datasource_filters: int
    extract_filters: int
    filter_user_functions: list[str] = Field(description="資料源篩選中出現的使用者函式")


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
