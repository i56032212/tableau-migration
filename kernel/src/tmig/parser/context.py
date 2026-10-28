"""解析過程共用的狀態：id 配發、unsupported／ignored 紀錄、資料源與工作表的對照表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lxml import etree

from tmig.formula.analyze import Analyzer
from tmig.ir.models import (
    CalcLevel,
    DataField,
    ExtractConnection,
    FieldUse,
    Filter,
    Parameter,
    Unsupported,
    WeekDay,
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
    connection: ExtractConnection
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


@dataclass
class SharedView:
    """全域篩選（<shared-views>）：套用到所有使用該資料源、且篩選架上有該欄位的工作表。"""

    element: etree._Element
    instances: dict[str, etree._Element]
    """欄位實例，以名稱（含方括號）為鍵。"""
    filters: dict[str, etree._Element]
    """以欄位實例名稱（例如 [none:Region:nk]）為鍵的 <filter>。"""
    failed: set[str] = field(default_factory=set)
    """解析失敗、已列為不支援的篩選，避免每張工作表重複列出。"""


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
    """以內部名稱（含方括號，例如 [Parameter 1]）為鍵。"""
    sheets: dict[str, SheetState] = field(default_factory=dict)
    """以工作表名稱為鍵。"""
    shared_views: dict[str, SharedView] = field(default_factory=dict)
    """全域篩選，以資料源的 Tableau 內部名稱為鍵。"""
    blank_sheets: set[str] = field(default_factory=set)
    """空白工作表的名稱：dashboard 上以空白區塊呈現。"""
    action_ids: dict[str, str] = field(default_factory=dict)
    """篩選動作的內部名稱（例如 [Action1_…]）→ IR id；解析工作表前先配發。"""
    notes: set[str] = field(default_factory=set)
    """已記在 ignored 的整份報表說明，避免重複。"""

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
