"""解析過程共用的狀態：id 配發、unsupported／ignored 紀錄、資料源與工作表的對照表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lxml import etree

from tmig.formula.analyze import Analyzer
from tmig.ir.models import (
    CalcLevel,
    Connection,
    DataField,
    FieldUse,
    Filter,
    FitMode,
    GeoSettings,
    Hierarchy,
    Marks,
    Parameter,
    PublishedSource,
    Unsupported,
    WeekDay,
    Zone,
)
from tmig.parser.ids import IdAllocator
from tmig.parser.twbx import WorkbookPackage, local_name, xpath

SNIPPET_LIMIT = 600

FilterKind = Literal["categorical", "range", "top_n", "condition", "relative_date"]
"""單一欄位篩選的種類（dashboard 依此決定篩選控制項）。"""

DATATYPES = frozenset({"string", "integer", "real", "boolean", "date", "datetime"})


def snippet(element: etree._Element, limit: int = SNIPPET_LIMIT) -> str:
    text = etree.tostring(element, encoding="unicode", with_tail=False)
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class DsState:
    """一個資料源的解析狀態。"""

    element: etree._Element
    name: str
    """Tableau 內部名稱，例如 federated.0abc。"""
    id: str
    caption: str
    connection: Connection
    week_start: WeekDay
    fields: list[DataField] = field(default_factory=list)
    by_name: dict[str, DataField] = field(default_factory=dict)
    """以內部名稱（含方括號，例如 [Sales]）找欄位。"""
    instances: dict[str, etree._Element] = field(default_factory=dict)
    """資料源層級的 column-instance（資料源篩選用），以名稱（含方括號）為鍵。"""
    filters: list[Filter] = field(default_factory=list)
    auto_groups: dict[str, str] = field(default_factory=dict)
    """Tableau 自動產生的群組（篩選動作、工具提示、排除、只保留），名稱 → 種類（sheet_link、vit、include、exclude）。"""
    analyzer: Analyzer | None = None
    levels: dict[str, CalcLevel | None] = field(default_factory=dict)
    """計算欄位 id → 計算層級；公式無法分析時為 None。"""
    published: PublishedSource | None = None
    """已併入的 Published Data Source（N8）。"""
    identity_sets: set[str] = field(default_factory=set)
    """Tableau 的使用者篩選轉成的布林計算欄位 id；工作表以「在集合中」篩選（P1）。"""
    set_fields: set[str] = field(default_factory=set)
    analytic_models: dict[str, etree._Element] = field(default_factory=dict)
    """分析模型（叢集）：名稱（例如 [AnalyticModel:{…}]）→ <analytic-model>。"""
    """集合（含使用者篩選）的欄位 id：篩選架上沒有條件的篩選表示「在集合中」。"""
    hierarchies: list[Hierarchy] = field(default_factory=list)
    """階層（<drill-paths>）。"""
    geo: GeoSettings | None = None
    """地理設定（預設的國家、州）。"""

    def add_field(self, data_field: DataField) -> None:
        self.fields.append(data_field)
        self.by_name[data_field.name] = data_field

    def field_by_id(self, field_id: str) -> DataField:
        for item in self.fields:
            if item.id == field_id:
                return item
        raise KeyError(field_id)


@dataclass
class SheetState:
    """一張工作表解析後留給 dashboard 使用的資訊。"""

    id: str
    name: str
    ds: DsState
    instances: dict[str, etree._Element]
    uses: dict[str, FieldUse]
    """已解析的欄位實例，以名稱（含方括號）為鍵。"""
    filter_kinds: dict[str, FilterKind]
    """工作表上的篩選種類，以欄位實例名稱為鍵；dashboard 依此決定控制項。"""
    cross: dict[str, str] = field(default_factory=dict)
    """跨資料源篩選：其他資料源的欄位（[資料源].[欄位實例]）→ 篩選的主要資料源欄位實例名稱。"""


@dataclass
class SharedView:
    """全域篩選（<shared-views>）：套用到所有使用該資料源、且篩選架上有該欄位的工作表。"""

    element: etree._Element
    instances: dict[str, etree._Element]
    """欄位實例，以名稱（含方括號）為鍵。"""
    filters: dict[str, etree._Element]
    """以欄位實例名稱（例如 [none:Region:nk]）為鍵的 <filter>。"""
    related: dict[str, etree._Element] = field(default_factory=dict)
    """其他資料源的欄位「套用到所有使用相關資料源的工作表」的篩選，以 [資料源].[欄位實例] 為鍵；
    <target field> 是這個資料源中對應的欄位。"""
    foreign_instances: dict[str, dict[str, etree._Element]] = field(default_factory=dict)
    """其他資料源的欄位實例：資料源名稱 → 欄位實例名稱 → <column-instance>。"""
    failed: set[str] = field(default_factory=set)
    """解析失敗、已列為不支援的篩選，避免每張工作表重複列出。"""


@dataclass
class TooltipRef:
    """工作表的工具提示文字中的 <Sheet name="…" maxwidth="…" maxheight="…" filter="…">（Viz in Tooltip）：
    所有工作表解析完後才對應到目標工作表（見 tmig.parser.workbook）。"""

    sheet: str
    """來源工作表的名稱。"""
    marks: Marks
    """工具提示所在的標記卡。"""
    element: etree._Element
    name: str
    """目標工作表的名稱。"""
    width: int
    height: int
    filter: str | None
    """filter 屬性的原文，例如 <All Fields>、<Region>,<Category>；沒寫時為 None。"""


@dataclass
class ParseContext:
    package: WorkbookPackage
    lenient: bool = False
    """寬鬆模式：資料源不是嵌入式萃取時仍解析欄位與工作表（以佔位連線表示），供盤點與測試使用。"""
    extracts_omitted: bool = False
    """.twbx 下載時刻意不含萃取檔（只盤點結構）：萃取檔不在 .twbx 內時不列為不支援。"""
    ids: IdAllocator = field(default_factory=IdAllocator)
    unsupported: list[Unsupported] = field(default_factory=list)
    ignored: list[Unsupported] = field(default_factory=list)
    datasources: dict[str, DsState] = field(default_factory=dict)
    """以 Tableau 內部名稱為鍵。"""
    parameters: list[Parameter] = field(default_factory=list)
    params_by_name: dict[str, Parameter] = field(default_factory=dict)
    param_columns: dict[str, etree._Element] = field(default_factory=dict)
    """參數 id → Parameters 資料源中的 <column>（欄列架上的參數當成計算欄位時，沿用它的別名）。"""
    """以內部名稱（含方括號，例如 [Parameter 1]）為鍵。"""
    sheets: dict[str, SheetState] = field(default_factory=dict)
    """以工作表名稱為鍵。"""
    shared_views: dict[str, SharedView] = field(default_factory=dict)
    """全域篩選，以資料源的 Tableau 內部名稱為鍵。"""
    blank_sheets: set[str] = field(default_factory=set)
    """空白工作表的名稱：dashboard 上以空白區塊呈現。"""
    action_ids: dict[str, str] = field(default_factory=dict)
    """篩選動作的內部名稱（例如 [Action1_…]）→ IR id；解析工作表前先配發。"""
    stale_action_filters: list[tuple[str, str]] = field(default_factory=list)
    """參照已不存在的篩選動作的篩選（工作表名稱、篩選 id）：解析動作時連到會寫入它的動作。"""
    notes: set[str] = field(default_factory=set)
    """已記在 ignored 的整份報表說明，避免重複。"""
    windows: dict[str, tuple[str, str]] = field(default_factory=dict)
    """<windows> 中各視窗的 simple-id（大寫、不含大括號）→（類型、名稱），導覽按鈕以它找目標。"""
    sheet_fits: dict[str, FitMode] = field(default_factory=dict)
    sheet_pages: dict[str, str] = field(default_factory=dict)
    paged_sheets: set[str] = field(default_factory=set)
    """有頁面架的工作表名稱（dashboard 的頁面控制項只能對應這些工作表）。"""
    """工作表名稱 → 目前的頁（視窗 <viewpoint> 的 <current-page> 中的值原文，例如 #2016-10-01 00:00:00#）。"""
    """工作表名稱 → 工作表視窗的顯示大小（IR 的 FitMode）；沒有視窗的為 standard。"""
    dashboard_fits: dict[str, dict[str, FitMode]] = field(default_factory=dict)
    """dashboard 名稱 → 工作表名稱 → 這個 dashboard 中的顯示大小（<viewpoint> 的 <zoom>）。"""
    dashboard_ids: dict[str, str] = field(default_factory=dict)
    """dashboard 名稱 → IR id；導覽按鈕可能先參照之後才解析的 dashboard，所以第一次用到時配發。"""
    navigations: list[tuple[etree._Element, Zone]] = field(default_factory=list)
    story_ids: dict[str, str] = field(default_factory=dict)
    dynamic_parameters: list[tuple[Parameter, etree._Element]] = field(default_factory=list)
    """動態參數（<column default-value-field=… source-field=…>）：資料源解析完後才對應到欄位。"""
    tooltip_refs: list[TooltipRef] = field(default_factory=list)
    """工具提示中的工作表（Viz in Tooltip），依工作表解析的順序。"""
    broken_calcs: list[tuple[str, Unsupported]] = field(default_factory=list)
    """公式有錯誤的計算欄位（欄位 id、不支援的紀錄）：沒有用到的最後改列為略過。"""
    story_names: set[str] = field(default_factory=set)
    """workbook 中 Story 的名稱（導覽按鈕與切換動作的目標可能是 Story）。"""
    """導覽到 dashboard 的按鈕：所有 dashboard 解析完後確認目標存在。"""

    def dashboard_id(self, name: str) -> str:
        if name not in self.dashboard_ids:
            self.dashboard_ids[name] = self.ids.make("db", name)
        return self.dashboard_ids[name]

    def story_id(self, name: str) -> str:
        """Story 的 id（動作與按鈕在 Story 解析前就可能參照它）。"""
        if name not in self.story_ids:
            self.story_ids[name] = self.ids.make("st", name)
        return self.story_ids[name]

    def flag(self, element: etree._Element, reason: str) -> None:
        """記錄 v0 不支援的元素：報表因此進人工佇列。"""
        self.unsupported.append(self._entry(element, reason))

    def ignore(self, element: etree._Element, reason: str) -> None:
        """記錄刻意略過、不影響數字的元素。"""
        self.ignored.append(self._entry(element, reason))

    def _entry(self, element: etree._Element, reason: str) -> Unsupported:
        return Unsupported(
            source_ref=xpath(element),
            reason=reason,
            element=local_name(element) or None,
            xml=snippet(element),
        )

    def parameter(self, ref: str) -> Parameter | None:
        """[Parameters].[Parameter 1] 或 [Parameter 1] → 參數。"""
        from tmig.parser.refs import qualified_parts

        parts = qualified_parts(ref)
        if not parts:
            return None
        if len(parts) == 2 and parts[0] != "Parameters":
            return None
        return self.params_by_name.get("[" + parts[-1].replace("]", "]]") + "]")
