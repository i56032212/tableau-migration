"""Vega-Lite 規格產生（F1、J1–J3）：IR 工作表＋查詢結果 → Vega-Lite v6 規格（資料內嵌）。

目標是資訊等價：同樣的標記類型、欄位對應、排序、顏色與標籤，字型等細部樣式不強求一致。

- 長條、折線、面積、圓點、方塊、形狀：量值在欄架為橫向、在列架為直向；兩邊都有量值時畫成散佈圖
- 文字表（交叉表）：列架維度為 y、欄架維度為 x，標籤量值為文字
- 同一架上有多個維度時，合併成「外層｜內層」的單一座標，不做分面；多層的列標題與 Tableau 相同每一層
  一欄（外層的成員只寫在各組的第一列，欄位標籤寫在各欄上方），畫在座標軸的區域中
- 同一架上有多個量值：每個量值一張圖上下（列架）或左右（欄架）排列；雙軸的第二個量值與前一個
  重疊，兩個軸各自的刻度；每個量值使用自己的標記卡
- Measure Names／Values：查詢結果是寬表（每個量值一欄），這裡轉成長表（每個量值一列），
  Measure Names 依 Measure Values 卡的順序，Measure Values 的文字依各量值自己的格式
- 圓餅：角度為 wedge-size 的量值；欄列架上的量值只是撐出版面（例如 MIN(0)），不畫成軸
- 離散維度依查詢結果的順序排列（查詢已套用工作表的排序）
- 標籤與工具提示的文字依 IR 的 Tableau 格式字串在後端先格式化
- 多邊形：依路徑（沒有時依資料的順序）連接各點並填滿，每個多邊形由詳細資料的維度區分
- 座標軸：隱藏（保留格線）、自訂標題、固定範圍（超出的部分裁切）、反轉、不包含 0；雙軸同步時
  共用刻度。格線、座標軸線與刻度依列（縱軸）與欄（橫軸）的設定
- 工作表的背景（可以透明）與文字顏色：座標軸、圖例、標籤與文字表的文字
- 顯示大小（standard、fit_width、fit_height、entire_view）：離散的列每一列固定高度（預設 20 像素）、
  離散的欄依內容的寬度，連續的座標軸填滿；大小寫在 usermeta.sizing，檢視器依區塊的設定再套用一次
  （見 apply_fit）。標題與座標軸區域設定的寬度、列與欄的色帶與分隔線也照樣畫出
- 甘特、地圖等尚未支援的標記回傳 None，由檢視器顯示「未支援」
- 參考線（J4）：值在後端依範圍（整張表、窗格、儲存格）以標記的值計算；離散軸上以每個標記位置的
  短線（tick）連成參考線，連續軸或沒有另一軸時畫成橫跨整張圖的線；只有一條時加上標籤
"""

from __future__ import annotations

import copy
import itertools
import json
import math
import re
import statistics
import unicodedata
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, TypeGuard

from tmig.data.executor import QueryResult
from tmig.ir.models import (
    IR,
    MEASURE_NAMES,
    MEASURE_VALUES,
    SPECIAL_FIELDS,
    AnnotationPoint,
    Axis,
    Banding,
    ColorPalette,
    DataField,
    Divider,
    FieldUse,
    FontElement,
    FontStyle,
    LabelScope,
    MarkLabels,
    Marks,
    Pane,
    ParamRef,
    ReferenceLine,
    Scalar,
    ShelfStyle,
    TextRun,
    TextStyle,
    TrendLine,
    TupleFilter,
    Worksheet,
    ZeroLine,
    Zone,
)
from tmig.planner.state import use_key
from tmig.render import maps, packing
from tmig.render.aliases import alias_key, member_aliases, parameter_label
from tmig.render.colors import continuous_scale, discrete_scale, member_order, shape_scale
from tmig.render.fonts import (
    DEFAULT_SIZE,
    LABEL_COLOR,
    SEMIBOLD,
    TITLE_COLOR,
    TITLE_SIZE,
    Font,
    resolve,
)
from tmig.render.format import (
    PERCENT_DEFAULT,
    PERCENT_TABLE_CALCS,
    axis_format,
    display_format,
    fiscal_label,
    format_value,
    parse_format,
)
from tmig.render.missing import pad_result
from tmig.render.palettes import DEFAULT_REGULAR, PALETTES
from tmig.render.trends import fit_model, format_p, format_r2, t_quantile
from tmig.sqlgen.query import (
    COLUMN_ORDER,
    GRAND_TOTAL,
    SUBTOTAL,
    OutputColumn,
    TitleQueries,
    TotalQuery,
)

SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
SEPARATOR = "｜"
UNSUPPORTED_MARKS = {"map"}
GANTT_DAY_MS = 86400000
"""甘特條在日期軸上的長度：「大小」的量值以天為單位（與 Tableau 相同）。"""
TIME_UNITS = {
    "year": "utcyear",
    "quarter": "utcyearquarter",
    "month": "utcyearmonth",
    "week": "utcyearmonthdate",
    "day": "utcyearmonthdate",
    "hour": "utcyearmonthdatehours",
    "minute": "utcyearmonthdatehoursminutes",
    "second": "utcyearmonthdatehoursminutesseconds",
}
AGG_LABELS = {
    "sum": "",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count": "CNT",
    "countd": "CNTD",
    "median": "MEDIAN",
    "attr": "ATTR",
    "stdev": "STDEV",
    "stdevp": "STDEVP",
    "var": "VAR",
    "varp": "VARP",
    "pct5": "PCT5",
    "pct10": "PCT10",
    "pct25": "PCT25",
    "pct50": "PCT50",
    "pct75": "PCT75",
    "pct90": "PCT90",
    "pct95": "PCT95",
    "collect": "COLLECT",
}
TC_LABELS = {
    "running_total": "累計",
    "percent_of_total": "合計百分比",
    "rank": "排名",
    "difference": "差異",
    "percent_difference": "差異百分比",
    "moving": "移動計算",
}
MOVING_LABELS = {"avg": "移動平均", "sum": "移動總和", "min": "移動最小值", "max": "移動最大值"}
VL_MARKS = {
    "bar": "bar",
    "line": "line",
    "area": "area",
    "circle": "circle",
    "square": "square",
    "shape": "point",
    "polygon": "line",
}
ROW = "__row"
NEXT = "__next"
DROP_LINE_COLOR = "#666666"
"""下拉線的顏色。"""
"""跳躍的線：同一條線上下一個點的位置（_Builder.jump_layers）。"""
TOTAL = "__total"
HEADER_LINE_HEIGHT = 15
"""多層欄標題分行顯示時的行高（像素；9pt 的字）。"""
ORDER = "__order"
"""欄標題的名次（查詢的 __order(columns)），只在產生規格時使用、不留在資料中。"""
"""總計列的標記（資料中為 True）。"""
TEXT_COLOR = "#1f2328"
"""標籤與文字沒有設定顏色時的顏色。"""
ROW_HEIGHT = 20
"""Tableau 標準顯示大小的列高（像素）：離散的列沒有設定大小時每一列的高度，長條與文字表相同
（以 Tableau 的畫面量測：Logshark 報表的長條圖與文字表）。列數多時在區塊內捲動。"""
COLUMN_WIDTH = (40, 320)
"""離散的欄依內容估計寬度時的最小與最大值（像素）。"""
BAR_BAND = 0.75
"""長條占列高（欄寬）的比例：Tableau 沒有設定「大小」時的預設（以 Logshark 報表的 Tableau 畫面量測：
20 像素的列中長條 15 像素，放大的區塊中也是 75%）。設定了大小 s 時為 s / 2（s = 1 為一半、s = 2 填滿）。"""
POINT_DIAMETER = 10
"""圓點、形狀與方塊的直徑（像素）：Tableau 大小 1（預設）時的直徑，大小 s 時為 s 倍（同樣以 Tableau 的
畫面量測：大小 1.637 的圓點直徑 16 像素）。"""
LINE_WIDTH = 2
"""折線的寬度（像素）：Tableau 大小 1（預設）時的線寬，大小 s 時為 s 倍（以 Tableau 的畫面量測：預設的
線約 2 像素，大小 14.55 的線約 30 像素，見 HR Dashboards 的啞鈴圖）。"""
TREEMAP = ("__tm_x", "__tm_x2", "__tm_y", "__tm_y2")
"""樹狀圖的長方形（0–1 的座標，y 向下）。"""
BUBBLE = ("__pb_x", "__pb_y", "__pb_r", "__pb_size")
"""泡泡圖的中心與半徑（排列的單位），與畫出的大小（像素，外框的面積）。"""
CLOUD = ("__wc_x", "__wc_y", "__wc_fs", "__wc_size")
"""文字雲的中心與字級（像素），與畫出的字級（放不下時縮小）。"""
LABEL_W, LABEL_H = "__label_w", "__label_h"
"""標籤的寬高估計（像素）：放不下的標籤不顯示。"""
MAX_CLOUD = 400
"""文字雲最多的字數：更多時照常畫成一格。"""
DARK_LUMINANCE = 0.3
"""標記的顏色比這個亮度（WCAG 的相對亮度）暗時，上面的文字為白色。"""
DEFAULT_EXTENT = (960.0, 600.0)
"""不在 dashboard 上的工作表假設的大小（像素）。"""
WRAPPED = "__label_wrapped"
"""依標記的大小換行的標籤（樹狀圖、泡泡圖）。"""
BAND = "__band_"
"""色帶的資料欄（__band_rows、__band_columns）。"""
DECO = "__deco"
LABEL_HIDDEN = "__label_hidden"
"""參數：重疊而隱藏的標記標籤（標籤 id 的清單），由檢視器依畫好的位置算出（見 _Builder.named_label）。"""
LABEL_ID = "__lid"
HOLE = "__hole"
PIE_ONE = "__pie_one"
"""角度上沒有欄位的圓餅：每一塊的大小（都是 1）。"""
"""分面的甜甜圈中間的文字（每一格的第一列資料）。"""
"""標記標籤的 id（資料中每一列，含較粗的標記卡的資料）：重疊時隱藏的標籤依它找出。"""
"""色帶、多層列標題等裝飾圖層的名稱前綴：檢視器的選取與醒目提示略過這些圖層（與參考線的 __ref 相同）。"""
MEASURE_NAMES_TITLE = "Measure Names"
MEASURE_VALUES_TITLE = "Measure Values"
MEASURE_NAME_FIELD = "__mn_name"
"""雙軸等以 Measure Names 上色時，每個量值圖層的常數欄位（量值的名稱）。"""


@dataclass
class Channel:
    """資料中的一個欄位：原始值欄與顯示文字欄。"""

    name: str
    label: str
    title: str
    role: Literal["dimension", "measure"]
    datatype: str
    use: FieldUse
    format: str | None = None
    aliases: dict[Any, str] = field(default_factory=dict)
    """成員別名（原始值的比對鍵 → 顯示名稱）；只有離散維度的原始成員有。"""
    is_set: bool = False
    """集合（成員依「內」、「外」的順序）。"""
    fiscal: int = 1
    """日期欄位的會計年度起始月（年、季的標籤加上 FY）。"""


@dataclass
class RenderResult:
    spec: dict[str, Any] | None
    rows: int
    mark: str
    unsupported: str | None = None
    notes: list[str] = field(default_factory=list)


def fiscal_starts(ir: IR) -> dict[str, int]:
    """日期欄位 id → 會計年度起始月（欄位自訂的或資料源的；只列出不是一月的）。"""
    result: dict[str, int] = {}
    for ds in ir.datasources:
        for f in ds.fields:
            if f.datatype in ("date", "datetime"):
                start = f.fiscal_year_start or ds.settings.fiscal_year_start
                if start != 1:
                    result[f.id] = start
    return result


def caption(ir_field: DataField, use: FieldUse, ws: Worksheet) -> str:
    """類似 Tableau 欄位膠囊上的名稱。"""
    name = ir_field.caption
    if ir_field.set is not None and use.agg is None:
        name = f"IN/OUT({name})"
    elif use.date is not None:
        name = f"{use.date.unit.upper()}({name})"
    elif use.agg is not None and AGG_LABELS[use.agg]:
        name = f"{AGG_LABELS[use.agg]}({name})"
    if use.table_calc is not None:
        tc = next((t for t in ws.table_calcs if t.id == use.table_calc), None)
        if tc is not None and tc.type in TC_LABELS:
            label = TC_LABELS[tc.type]
            if tc.type == "moving":
                label = MOVING_LABELS.get(str(tc.options.get("aggregation")), label)
            if tc.secondary is not None:
                label = f"{label}的{TC_LABELS.get(tc.secondary.type, tc.secondary.type)}"
            name = f"{name}（{label}）"
    return name


class _Builder:
    """一張工作表的規格；channels 以欄位鍵（Measure Names／Values 以其特殊 id）為鍵。"""

    def __init__(
        self,
        ws: Worksheet,
        channels: dict[str, Channel],
        pane_values: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.ws = ws
        self.channels = channels
        self.notes: list[str] = []
        self.pane_values = pane_values or {}
        """詳細程度較粗的標記卡（ws.panes 的索引）各自的資料列（J3）。"""
        self.rows = list(ws.rows)
        self.columns = list(ws.columns)
        """畫出的欄列架（見 build_spec：沒有對應量值的 Measure Names 不畫）。"""
        self.label_fields: dict[int, str] = {}
        """有自訂標籤文字的標記卡（id(Marks)）→ 資料中組好的標籤欄。"""
        self.label_count = 0
        self.wrap_count = 0
        self.combo_count = 0
        """換行的標籤欄、多個欄位組合的色彩欄的編號。"""
        self.drop_count = 0
        """下拉線圖層的編號。"""
        """標記標籤圖層的編號（圖層名稱與旗標欄各自不同）。"""
        self.text_styles: dict[int, TextStyle] = {}
        """自訂標籤文字的格式（標記卡的 id → 各行相同的格式，或第一行的格式）。"""
        self.text_lines: dict[int, list[tuple[str, TextStyle | None]]] = {}
        """各行格式不同的自訂標籤文字（例如 KPI 的小標題與大數字）：每一行的資料欄與格式。"""
        self.culled = False
        """有重疊時隱藏的標記標籤（規格加上 __label_hidden 參數與標籤 id）。"""
        self.pie_extent = "min(width, height)"
        """圓餅的大小所依據的區塊邊長（分面時為每一格的）。"""
        self.values: list[dict[str, Any]] = []
        """工作表的資料列（色彩的範圍與各值的顏色依它決定）。"""
        self.measure_names: dict[Scalar, Any] = {}
        """Measure Names：量值的欄位鍵 → 顯示名稱（指定給量值的顏色依它對應）。"""
        self.extent = DEFAULT_EXTENT
        """圖的大小（像素，工作表在 dashboard 上的區塊）：樹狀圖依它的寬高比排列，標籤依它換行。"""

    def text_field(self, marks: Marks) -> str | None:
        """標籤（文字）上的欄：自訂的標籤文字，或標籤上第一個欄位的顯示文字。文字表的文字用它（不受
        「顯示標記標籤」影響）。"""
        if id(marks) in self.label_fields:
            return self.label_fields[id(marks)]
        return self.ch(marks.label[0]).label if marks.label else None

    def label_field(self, marks: Marks, default: str | None = None) -> str | None:
        """標記標籤的欄：關閉了標記標籤時為 None；否則為標籤上的文字（text_field），標籤上沒有欄位但開啟了
        標記標籤時為 default（與 Tableau 相同標示座標軸上的量值等）。"""
        setting = marks.labels
        if setting is not None and setting.show is False:
            return None
        found = self.text_field(marks)
        if found is None and setting is not None and setting.show:
            return default
        return found

    def text_font(self, marks: Marks, style: TextStyle | None = None) -> Font:
        """標記上文字（文字表、標記標籤）的字型：工作表與標記卡（datalabel）的設定，再套用自訂標籤文字的
        格式（style；省略時為各行相同的格式或第一行的）。"""
        style = style if style is not None else self.text_styles.get(id(marks))
        extra = (
            FontStyle(family=style.font, size=style.size, bold=style.bold, italic=style.italic)
            if style is not None
            else None
        )
        return self.font("cell", marks.label_font, extra)

    def text_fill(self, marks: Marks, default: str, style: TextStyle | None = None) -> str:
        """自訂標籤文字的顏色（格式中的 fontcolor），沒有時為 default。"""
        style = style if style is not None else self.text_styles.get(id(marks))
        return _css_color(style.color) if style is not None and style.color else default

    def shown(self, use: FieldUse) -> bool:
        return use.field not in SPECIAL_FIELDS or use.field in self.channels

    def with_pane_data(self, body: dict[str, Any], pane: Pane | None) -> dict[str, Any]:
        """標記卡有自己的資料時，這張圖改用它；工具提示只列出資料中有的欄位。"""
        index = next((i for i, p in enumerate(self.ws.panes) if p is pane), None)
        if index is None or index not in self.pane_values:
            return body
        records = self.pane_values[index]
        present = set(records[0]) if records else set()
        encoding = body.get("encoding")
        if encoding is not None and "tooltip" in encoding:
            encoding["tooltip"] = [t for t in encoding["tooltip"] if t["field"] in present]
        return {**body, "data": {"values": records}}

    def ch(self, use: FieldUse) -> Channel:
        return self.channels[use.field if use.field in SPECIAL_FIELDS else use_key(use)]

    def records_of(self, marks: Marks) -> list[dict[str, Any]]:
        """標記卡的資料列：詳細程度較粗的標記卡有自己的資料。"""
        index = next((i for i, p in enumerate(self.ws.panes) if p.marks is marks), None)
        if index is not None and index in self.pane_values:
            return self.pane_values[index]
        return self.values

    def is_measure(self, use: FieldUse) -> bool:
        return self.ch(use).role == "measure"

    def is_continuous_date(self, use: FieldUse) -> bool:
        """連續的完整日期（不取日期部分或截斷）。"""
        ch = self.channels.get(use_key(use))
        return use.continuous and ch is not None and ch.datatype in ("date", "datetime")

    def is_axis(self, use: FieldUse) -> bool:
        """欄列架上畫成座標軸的量值：連續的量值與 Measure Values；取消彙總量值時，不彙總的連續數值
        （每一列資料的值）也是座標軸。"""
        if self.is_measure(use):
            return use.continuous or use.field in SPECIAL_FIELDS
        return (
            self.ws.disaggregated
            and use.continuous
            and use.date is None
            and self.ch(use).datatype in ("integer", "real")
        )

    # ------------------------------------------------------------ 座標

    def axis_dim(
        self, uses: list[FieldUse], composite: str, multiline: bool = False
    ) -> dict[str, Any] | None:
        """維度的座標：連續的為座標軸，離散的為標題（多個維度合併成「外層｜內層」，multiline 時
        分行顯示）。"""
        if not uses:
            return None
        shelf: Literal["rows", "columns"] = "rows" if composite == "rows_dim" else "columns"
        if len(uses) > 1:
            encoding = {"field": composite, "type": "ordinal", "sort": None}
        else:
            encoding = _dimension(self.ch(uses[0]))
            if encoding["type"] in ("quantitative", "temporal"):
                return self.axis_settings(encoding, uses[0], shelf)
        shown, title, color = self.header(uses, shelf)
        encoding["title"] = SEPARATOR.join(self.ch(u).title for u in uses)
        axis: dict[str, Any] = {"ticks": False}  # Tableau 的標題沒有刻度
        if not shown:
            # 全部不顯示：不保留標題的空間
            axis.update(labels=False, domain=False, labelPadding=0, minExtent=0, maxExtent=0)
        expr = _header_expr(shown, len(uses), multiline and len(uses) > 1)
        if shown and expr is not None:
            axis["labelExpr"] = expr
        if title != encoding["title"]:
            axis["title"] = title
        if color is not None:
            axis["labelColor"] = color
        fonted = [i for i in shown if (s := self.axis_of(uses[i], shelf)) is not None and s.font]
        if fonted:
            axis.update(self.label_font(uses[fonted[-1]], shelf).axis("label"))
        extent = self.header_extent(uses, shelf)
        if extent is not None:
            # 拖曳調整過的標題寬度（列）或高度（欄）：標題區域固定這個大小，較長的成員截斷
            axis.update(minExtent=extent, maxExtent=extent)
            if shelf == "rows":
                axis["labelLimit"] = max(extent - 8, 8)
        if axis:
            encoding["axis"] = axis
        return encoding

    def header_extent(self, uses: list[FieldUse], shelf: Literal["rows", "columns"]) -> int | None:
        """離散標題（顯示的各層）設定的總寬度（列）或總高度（欄）；有一層沒有設定時為 None。"""
        shown = self.header(uses, shelf)[0]
        settings = [self.axis_of(uses[i], shelf) for i in shown]
        sizes = [s.size for s in settings if s is not None and s.size is not None]
        return sum(sizes) if shown and len(sizes) == len(shown) else None

    def shelf_size(
        self,
        encoding: dict[str, Any] | None,
        dims: list[FieldUse],
        shelf: Literal["rows", "columns"],
        values: list[dict[str, Any]],
        texts: list[str],
    ) -> dict[str, Any]:
        """一個方向的大小（usermeta.sizing）：離散的為每一列（欄）的大小 step，欄另有成員數 count
        與是否固定寬度 fixed（設定了大小；文字表在 build_spec 中也設為固定）；連續的座標軸設定了
        大小時為 size，否則為空（填滿）。"""
        style = self.ws.row_style if shelf == "rows" else self.ws.column_style
        if encoding is None:
            return {}
        if encoding.get("type") in ("ordinal", "nominal"):
            innermost = self.axis_of(dims[-1], shelf) if dims else None
            explicit = (innermost.cell if innermost is not None else None) or style.cell
            if shelf == "rows":
                return {"step": explicit or ROW_HEIGHT}
            labels = list(dict.fromkeys(v.get(encoding.get("field", "")) for v in values))
            natural = explicit or _column_width([*labels, *texts])
            return {"step": natural, "count": len(labels), "fixed": explicit is not None}
        return {"size": style.cell} if style.cell else {}

    def header(
        self, uses: list[FieldUse], shelf: Literal["rows", "columns"]
    ) -> tuple[list[int], str | None, str | None]:
        """離散欄位的標題：顯示的層級（Show Header）、欄位標籤（不顯示時為 None）與文字顏色。"""
        style = self.ws.row_style if shelf == "rows" else self.ws.column_style
        settings = [self.axis_of(u, shelf) for u in uses]
        shown = [i for i, s in enumerate(settings) if s is None or s.show]
        title = (
            SEPARATOR.join(self.ch(uses[i]).title for i in shown)
            if style.field_labels and shown
            else None
        )
        colors = [s.label_color for s in settings if s is not None and s.label_color is not None]
        return shown, title, _css_color(colors[-1]) if colors else None

    def axis_measure(self, use: FieldUse, shelf: Literal["rows", "columns"]) -> dict[str, Any]:
        ch = self.ch(use)
        encoding: dict[str, Any] = {"field": ch.name, "type": "quantitative", "title": ch.title}
        # Table Calculation 的軸只在百分比類時套用格式（累計、排名等沿用自動刻度）
        formatted = not use.table_calc or _table_calc_type(self.ws, use) in PERCENT_TABLE_CALCS
        axis = axis_format(parse_format(ch.format)) if formatted else None
        if axis:
            encoding["axis"] = axis
        return self.axis_settings(encoding, use, shelf)

    def axis_of(self, use: FieldUse, shelf: str) -> Axis | None:
        key = use_key(use)
        return next((a for a in self.ws.axes if a.shelf == shelf and use_key(a.use) == key), None)

    def axis_settings(
        self, encoding: dict[str, Any], use: FieldUse, shelf: Literal["rows", "columns"]
    ) -> dict[str, Any]:
        """連續座標軸的設定（隱藏、標題、固定範圍、反轉、包含 0、文字顏色）與這個方向的格線、
        座標軸線與刻度。隱藏的座標軸與 Tableau 相同保留格線。"""
        axis: dict[str, Any] = dict(encoding.get("axis") or {})
        lines = self.ws.row_style if shelf == "rows" else self.ws.column_style
        if not lines.grid:
            axis["grid"] = False
        if not lines.axis:
            axis["domain"] = False
        if not lines.ticks:
            axis["ticks"] = False
        if lines.zero_line is not None and encoding.get("type") == "quantitative":
            _zero_line(axis, lines.zero_line, lines.grid)
        setting = self.axis_of(use, shelf)
        if setting is not None:
            temporal = encoding.get("type") == "temporal"
            if setting.label_color is not None:
                axis["labelColor"] = _css_color(setting.label_color)
            if setting.font is not None:
                axis.update(self.label_font(use, shelf).axis("label"))
            if setting.title is not None:
                axis["title"] = setting.title or None
            if not setting.show:
                axis.update(labels=False, ticks=False, domain=False, title=None)
            if setting.size is not None:
                axis.update(minExtent=setting.size, maxExtent=setting.size)
            scale: dict[str, Any] = dict(encoding.get("scale") or {})
            start = _axis_value(setting.start, temporal)
            end = _axis_value(setting.end, temporal)
            if start is not None and end is not None:
                scale["domain"] = [start, end]
            elif start is not None:
                scale["domainMin"] = start
            elif end is not None:
                scale["domainMax"] = end
            if start is not None or end is not None:
                scale["nice"] = False  # 固定的範圍不再延伸到整數刻度
            if setting.reverse:
                scale["reverse"] = True
            if not temporal and (not setting.include_zero or start is not None):
                scale["zero"] = False
            if not temporal and setting.scale != "linear":
                # 對數刻度不含 0（對稱對數可以）
                scale["type"] = setting.scale
                if setting.scale == "log":
                    scale["zero"] = False
            if not setting.ticks and setting.show:
                axis.update(labels=False, ticks=False)
            if not temporal and setting.tick_spacing is not None:
                ticks = self.fixed_ticks(use, setting, start, end)
                if ticks is not None:
                    axis["values"] = ticks
                    self.minor_ticks(axis, use, setting, start, end)
            if scale:
                encoding["scale"] = scale
        if axis:
            encoding["axis"] = axis
        return encoding

    def minor_ticks(
        self, axis: dict[str, Any], use: FieldUse, setting: Axis, start: Any, end: Any
    ) -> None:
        """次要刻度（主要刻度是固定間距時）：每隔 minor_spacing 一個較短、沒有文字的刻度；格線只畫在
        主要刻度上。Vega 的座標軸沒有次要刻度：把主要、次要刻度都列為刻度，再依值是不是主要刻度決定
        文字、刻度的長度與格線。"""
        major, minor = setting.tick_spacing, setting.minor_spacing
        if major is None or minor is None or minor >= major:
            return
        found = self.fixed_ticks(
            use, setting.model_copy(update={"tick_spacing": minor}), start, end
        )
        if found is None:
            return
        origin = setting.tick_origin or 0.0
        merged = {round(v, 9) for v in found} | {round(v, 9) for v in axis["values"]}
        axis["values"] = sorted(merged)
        position = f"(datum.value - {origin!r}) / {major!r}"
        test = f"abs({position} - round({position})) < 1e-6"
        label = axis.get("labelExpr", "datum.label")
        axis["labelExpr"] = f"{test} ? ({label}) : ''"
        axis["tickSize"] = {"condition": {"test": test, "value": 5}, "value": 3}
        if axis.get("grid", True):
            axis["gridOpacity"] = {"condition": {"test": test, "value": 1}, "value": 0}

    def fixed_ticks(self, use: FieldUse, setting: Axis, start: Any, end: Any) -> list[float] | None:
        """固定間距的主要刻度（編輯座標軸的「主要刻度：固定」）：從起點（預設 0）起每隔一個間距一個，涵蓋
        座標軸可能的範圍——固定的範圍，或資料的範圍（堆疊時是各標記的值相加，所以取正值、負值各自的總和為界；
        超出座標軸的刻度不會畫出）。刻度太多（超過 1000 個）時依自動。"""
        ch = self.channels.get(use_key(use))
        step = setting.tick_spacing
        if ch is None or step is None:
            return None
        numbers = [
            float(v)
            for v in (record.get(ch.name) for record in self.values)
            if isinstance(v, int | float) and not isinstance(v, bool)
        ]
        # 堆疊時座標軸到各標記的值相加之處：正值、負值各自的總和為界
        low = min(sum(v for v in numbers if v < 0), min(numbers, default=0.0))
        high = max(sum(v for v in numbers if v > 0), max(numbers, default=0.0))
        if setting.include_zero:
            low, high = min(low, 0.0), max(high, 0.0)
        if isinstance(start, int | float):
            low = float(start)
        if isinstance(end, int | float):
            high = float(end)
        origin = setting.tick_origin or 0.0
        first = math.ceil((low - origin) / step - 1e-9)
        last = math.floor((high - origin) / step + 1e-9)
        if last < first or last - first > 1000:
            return None
        return [origin + n * step for n in range(first, last + 1)]

    def align_cells(
        self,
        vl_mark: dict[str, Any],
        encoding: dict[str, Any],
        marks: Marks,
        text_field: str | None,
    ) -> None:
        """文字表儲存格中文字的水平對齊：標記卡或工作表設定的對齊（Format > 窗格），沒有設定時與
        Tableau 相同數字靠右、文字靠左（只有一格、沒有標題時置中）。有離散的欄時對齊各欄的邊緣
        （band 的起點或終點），否則對齊整張圖的邊緣。"""
        explicit = marks.cell_align or self.ws.cell_align
        if explicit is None and "x" not in encoding and "y" not in encoding:
            return
        if any(
            isinstance(encoding.get(c), dict)
            and encoding[c].get("type") not in ("ordinal", "nominal")
            for c in ("x", "y")
        ):
            return  # 連續座標軸上的文字（例如依日期擺放的註記）置中在標記的位置
        numeric = text_field is not None and (
            any(self.is_measure(u) for u in marks.label)
            if marks.label
            else any(c.label == text_field and c.role == "measure" for c in self.channels.values())
        )
        align = explicit or ("right" if numeric else "left")
        if align == "center":
            return
        vl_mark["align"] = align
        vl_mark["dx"] = -CELL_PADDING if align == "right" else CELL_PADDING
        x = encoding.get("x")
        if isinstance(x, dict) and x.get("type") in ("ordinal", "nominal"):
            scale = dict(x.get("scale") or {})
            scale.setdefault("type", "band")
            scale.setdefault("paddingInner", 0)
            scale.setdefault("paddingOuter", 0)
            x["scale"] = scale
            x["bandPosition"] = 1 if align == "right" else 0
        elif x is None:
            vl_mark["x"] = {"expr": "width"} if align == "right" else 0

    def label_color(self, marks: Marks, vl_mark: dict[str, Any]) -> str:
        """標記標籤的顏色：與標記相符時為標記的顏色（依欄位著色時另由 _label_layer 沿用色彩的 encoding），
        否則為標籤的文字顏色。"""
        if marks.label_match and marks.color is None:
            return str(vl_mark.get("color") or DEFAULT_MARK_COLOR)
        return self.text_color(marks)

    def font(
        self,
        element: FontElement,
        *layers: FontStyle | None,
        size: float = DEFAULT_SIZE,
        weight: int | None = None,
    ) -> Font:
        """元素的字型：工作表的預設字型、這個元素的設定，再依序套用 layers（欄位、標記卡的設定）。"""
        fonts = self.ws.fonts
        return resolve(
            fonts.get("worksheet"), fonts.get(element), *layers, size=size, weight=weight
        )

    def label_font(self, use: FieldUse, shelf: str) -> Font:
        """標題成員或座標軸刻度的字型：工作表的 label，再套用這個欄位的設定。"""
        setting = self.axis_of(use, shelf)
        return self.font("label", setting.font if setting is not None else None)

    def text_color(self, marks: Marks | None = None, cell: bool = False) -> str:
        """標記上的文字顏色：標籤的顏色、文字表儲存格的顏色（cell）、工作表的文字顏色，都沒有時為
        預設的深灰色。"""
        for color in (
            marks.label_color if marks is not None else None,
            self.ws.cell_color if cell else None,
            self.ws.text_color,
        ):
            if color is not None:
                return _css_color(color)
        return TEXT_COLOR

    # ------------------------------------------------------------ 單一圖

    def unit(
        self,
        marks: Marks,
        mark: str,
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> tuple[dict[str, Any], str]:
        """一張圖的 mark、encoding（與標籤圖層）。回傳規格片段與實際的標記類型。"""
        if mark == "gantt":
            found = self.gantt(marks, rows_dims, cols_dims, rows_meas, cols_meas)
            if found is not None:
                return found
            self.notes.append("甘特條有多個連續座標軸或在窗格中，尚未支援，以長條呈現")
            mark = "bar"
        if not (rows_dims or cols_dims or rows_meas or cols_meas):
            packed = self.packed(marks, mark)
            if packed is not None:
                return packed
        encoding: dict[str, Any] = {}
        vl_mark: dict[str, Any]
        label_field: str | None = None
        label_type = ""
        highlight_table = False
        if mark == "text" or not (rows_meas or cols_meas):
            # 只有離散欄位的圓點與形狀（大小上有欄位的方塊也是）：每格一個標記（與 Tableau 相同，不是文字）
            sized_squares = mark == "square" and marks.size is not None
            dots = (mark in ("circle", "shape") or sized_squares) and bool(rows_dims or cols_dims)
            # 方塊標記、只有維度：醒目提示表（填滿每一格的色塊加上文字）
            highlight = mark == "square" and not sized_squares
            if not dots:
                mark = "square" if highlight else "text"
                highlight_table = highlight
            y = self.axis_dim(rows_dims, "rows_dim")
            x = self.axis_dim(cols_dims, "cols_dim", multiline=True)
            if y is not None:
                encoding["y"] = {
                    **y,
                    "axis": {"orient": "left", "title": y.get("title"), **y.get("axis", {})},
                }
            if x is not None:
                axis: dict[str, Any] = {
                    "orient": "top",
                    "labelAngle": 0,
                    "title": x.get("title"),
                    **x.get("axis", {}),
                }
                if x["field"] == "cols_dim":
                    # 多層的欄標題分行顯示（外層在上），欄窄時不會疊在一起；多出的行往上排
                    levels = max(len(self.header(cols_dims, "columns")[0]), 1)
                    axis["labelLineHeight"] = HEADER_LINE_HEIGHT
                    axis["labelPadding"] = 2 + HEADER_LINE_HEIGHT * (levels - 1)
                encoding["x"] = {**x, "axis": axis}
            text_field = self.label_field(marks) if dots else self.text_field(marks)
            if text_field is None and not dots:
                measures = [
                    c
                    for c in self.channels.values()
                    if c.role == "measure" and c.datatype != "spatial"
                ]
                text_field = measures[0].label if measures else None
            if dots:
                vl_mark = {"type": VL_MARKS[mark], "tooltip": True}
                if mark == "shape":
                    vl_mark["filled"] = True
                if text_field is not None:
                    label_field, label_type = text_field, "cell"
                text_field = None
            else:
                vl_mark = {"type": "text", "tooltip": True, "lineBreak": "\n"}
            if not dots:
                vl_mark.update(self.text_font(marks).mark())
                if marks.color is None and marks.mark_color is None:
                    vl_mark["color"] = self.text_fill(marks, self.text_color(marks, cell=True))
                if text_field is not None:
                    encoding["text"] = {"field": text_field, "type": "nominal"}
                else:
                    encoding["text"] = {"value": "Abc"}
            if mark == "text":
                self.align_cells(vl_mark, encoding, marks, text_field)
        else:
            vl_type = VL_MARKS.get(mark, "point")
            if rows_meas and cols_meas:
                encoding["x"] = self.axis_measure(cols_meas[0], "columns")
                encoding["y"] = self.axis_measure(rows_meas[0], "rows")
                if mark != "polygon":
                    vl_type = "point" if vl_type in ("bar", "line", "area") else vl_type
                for dims in (rows_dims, cols_dims):
                    for use in dims:
                        encoding.setdefault("detail", []).append(
                            {"field": self.ch(use).name, "type": "nominal"}
                        )
            elif cols_meas:
                encoding["x"] = self.axis_measure(cols_meas[0], "columns")
                y = self.axis_dim(rows_dims, "rows_dim")
                if y is not None:
                    encoding["y"] = y
                for use in cols_dims + self.line_breaks(vl_type, marks, rows_dims):
                    encoding.setdefault("detail", []).append(
                        {"field": self.ch(use).name, "type": "nominal"}
                    )
            else:
                encoding["y"] = self.axis_measure(rows_meas[0], "rows")
                x = self.axis_dim(cols_dims, "cols_dim")
                if x is not None:
                    encoding["x"] = x
                for use in rows_dims + self.line_breaks(vl_type, marks, cols_dims):
                    encoding.setdefault("detail", []).append(
                        {"field": self.ch(use).name, "type": "nominal"}
                    )
            vl_mark = {"type": vl_type, "tooltip": True}
            if mark == "polygon":
                # 多邊形：以填滿的折線依序連接各點
                vl_mark["filled"] = True
            elif vl_type == "line" and marks.markers == "all":
                # 色彩的「效果＞標記：全部」：每個資料點畫出與線同色的圓點
                vl_mark["point"] = {"filled": True, "size": 36}
            elif vl_type in ("line", "area"):
                # 與 Tableau 的預設相同不畫出點；透明的點讓每個標記都能停留顯示工具提示、點選
                vl_mark["point"] = "transparent"
            # 堆疊標記（分析＞堆疊標記）：只有一個量值的座標軸時才有堆疊
            value_channel = None if rows_meas and cols_meas else "x" if cols_meas else "y"
            if (
                self.ws.stack is not None
                and value_channel is not None
                and mark != "polygon"
                and encoding[value_channel].get("type") == "quantitative"
            ):
                # 關閉：長條與區域從 0 畫起、互相重疊；開啟：折線、圓點等也依標記卡上的維度累加
                encoding[value_channel]["stack"] = "zero" if self.ws.stack == "on" else None
            if mark == "shape":
                vl_mark["filled"] = True
            if any(_fixed_range(encoding.get(c)) for c in ("x", "y")):
                vl_mark["clip"] = True  # 與 Tableau 相同，固定範圍之外的標記不畫
            if self.ws.null_marks is not None:
                # 座標軸上的 Null（特殊值＞標記）：預設隱藏、折線斷開；connect 當作沒有這一列（折線連接
                # 前後的點），zero 畫在預設值（0）
                vl_mark["invalid"] = "filter" if self.ws.null_marks == "connect" else "show"
            # 開啟標記標籤、標籤上沒有欄位時標示數值座標軸上的量值（與 Tableau 相同）
            value_axis = rows_meas if not cols_meas else cols_meas if not rows_meas else []
            default = self.ch(value_axis[0]).label if value_axis else None
            found = self.label_field(marks, default)
            if mark == "polygon" and found is not None:
                self.notes.append("多邊形的標籤尚未畫出")
            elif found is not None:
                label_field = found
                label_type = vl_type

        self.common(encoding, marks)
        if highlight_table:
            vl_mark.pop("color", None)
            text = {
                "mark": vl_mark,
                "encoding": {
                    "text": encoding.pop("text"),
                    "color": {"value": self.text_color(marks, cell=True)},
                },
            }
            rect = _mark_style({"type": "rect", "tooltip": True}, marks)
            return {"encoding": encoding, "layer": [{"mark": rect}, text]}, mark
        color = encoding.get("color")
        if (
            vl_mark["type"] in ("line", "area")
            and not vl_mark.get("filled")
            and isinstance(color, dict)
            and color.get("type") == "quantitative"
        ):
            # Vega-Lite 依顏色的欄位分開各條線（依量值著色時每個值一條）：以色階中間的顏色畫成一條線
            del encoding["color"]
            vl_mark["color"] = _middle_color(color.get("scale"))
            self.notes.append("依量值著色的折線以色階中間的單一顏色畫出（Tableau 依各段的值著色）")
        _mark_style(vl_mark, marks)
        self.size_mark(vl_mark, encoding, marks)
        if marks.path is not None and vl_mark["type"] in ("line", "area"):
            encoding["order"] = {"field": self.ch(marks.path).name}
        elif mark == "polygon":
            vl_mark["order"] = None  # 沒有路徑時依資料的順序連接（而不是依 x 排序）
        # 線條類型（階梯、跳躍）：量值在橫軸時線沿縱軸前進
        jump: list[dict[str, Any]] | None = None
        if (
            marks.line_type != "linear"
            and mark == "line"
            and vl_mark["type"] == "line"
            and bool(rows_meas) != bool(cols_meas)
        ):
            if marks.line_type == "step":
                vl_mark["interpolate"] = "step-before" if cols_meas else "step-after"
            else:
                jump = self.jump_layers(marks, vl_mark, encoding, vertical=bool(cols_meas))
        drops = self.drop_layers(encoding) if mark not in ("text", "pie", "polygon") else []
        if label_field is not None:
            labels = self.label_layers(
                marks, encoding, vl_mark, label_type, label_field, rows_meas, cols_meas
            )
            if labels:
                return {
                    "encoding": encoding,
                    "layer": [*(jump or [{"mark": vl_mark}]), *drops, *labels],
                }, mark
        if mark == "text":
            lines = self.line_layers(marks, vl_mark, encoding)
            if lines is not None:
                return lines, mark
        if jump is not None:
            return {"encoding": encoding, "layer": [*jump, *drops]}, mark
        if drops:
            return {"encoding": encoding, "layer": [{"mark": vl_mark}, *drops]}, mark
        return {"mark": vl_mark, "encoding": encoding}, mark

    def drop_layers(self, encoding: dict[str, Any]) -> list[dict[str, Any]]:
        """下拉線：從標記到連續座標軸的虛線（到橫軸是往下的垂直線，到縱軸是往左的水平線）。

        一律顯示的是裝飾圖層（__deco_drop）；選取標記時才顯示的（__drop_s）預設不畫，檢視器選取標記後
        改成只畫選取的標記（與只標示選取標記的標籤相同）。"""
        settings = self.ws.drop_lines
        if settings is None:
            return []

        def continuous(channel: str) -> bool:
            enc = encoding.get(channel)
            return isinstance(enc, dict) and enc.get("type") in ("quantitative", "temporal")

        if not ("x" in encoding and "y" in encoding):
            return []
        prefix = "__deco_drop" if settings.when == "always" else "__drop_s"
        hidden: list[dict[str, Any]] = [] if settings.when == "always" else [{"filter": "false"}]
        rule = {"type": "rule", "strokeDash": [3, 3], "strokeWidth": 1, "tooltip": None}
        plain = {"color": {"value": DROP_LINE_COLOR}, "size": {"value": 1}}
        layers: list[dict[str, Any]] = []
        ends: list[tuple[str, dict[str, Any]]] = []
        if settings.x and continuous("x"):
            ends.append(("x", {"y2": {"value": "height"}}))
        if settings.y and continuous("y"):
            ends.append(("y", {"x2": {"value": 0}}))
        for axis, end in ends:
            self.drop_count += 1
            layers.append(
                {
                    "name": f"{prefix}{axis}{self.drop_count}",
                    "transform": list(hidden),
                    "mark": dict(rule),
                    "encoding": {**end, **plain},
                }
            )
        if layers and settings.labels:
            self.notes.append("下拉線在座標軸上的標籤尚未畫出")
        return layers

    def jump_layers(
        self, marks: Marks, vl_mark: dict[str, Any], encoding: dict[str, Any], vertical: bool
    ) -> list[dict[str, Any]] | None:
        """跳躍的線（Tableau 線條類型的 Jump）：每個點畫一段到下一個點的位置的水平線，不畫垂直的連接。
        以 window 找出同一條線上下一個點的位置，畫成線段（rule）；另有透明的點供停留與選取。"""
        axis, end = ("y", "y2") if vertical else ("x", "x2")
        position = encoding.get(axis)
        if not isinstance(position, dict) or "field" not in position:
            return None
        series = [d["field"] for d in encoding.get("detail", []) if "field" in d]
        for name in ("color", "size", "strokeDash", "row", "column"):
            enc = encoding.get(name)
            if (
                isinstance(enc, dict)
                and "field" in enc
                and enc.get("type") in ("nominal", "ordinal")
            ):
                series.append(enc["field"])
        continuous = position.get("type") in ("quantitative", "temporal")
        order = (
            self.ch(marks.path).name
            if marks.path is not None
            else position["field"]
            if continuous
            else ROW
        )
        window = {
            "window": [{"op": "lead", "field": position["field"], "as": NEXT}],
            "sort": [{"field": order}],
            "groupby": list(dict.fromkeys(f for f in series if f != position["field"])),
        }
        rule = {
            k: v for k, v in vl_mark.items() if k in ("color", "opacity", "strokeWidth", "clip")
        }
        return [
            {
                "transform": [window, {"filter": f"isValid(datum['{NEXT}'])"}],
                "mark": {**rule, "type": "rule", "tooltip": True},
                "encoding": {end: {"field": NEXT}},
            },
            {"mark": {"type": "point", "opacity": 0, "tooltip": True}},
        ]

    def line_layers(
        self, marks: Marks, vl_mark: dict[str, Any], encoding: dict[str, Any]
    ) -> dict[str, Any] | None:
        """各行格式不同的自訂文字（例如 KPI 的小標題與大數字）：每一行一個文字圖層，依各行的字級由上而下
        排列、整段垂直置中；不是時為 None。"""
        lines = self.text_lines.get(id(marks))
        text = encoding.get("text")
        if (
            not lines
            or not isinstance(text, dict)
            or text.get("field") != self.label_fields.get(id(marks))
        ):
            return None
        fonts = [self.text_font(marks, style) for _, style in lines]
        heights = [font.size * 1.2 for font in fonts]
        offset = -sum(heights) / 2
        layers: list[dict[str, Any]] = []
        for (field_name, style), font, height in zip(lines, fonts, heights, strict=True):
            mark = {
                **vl_mark,
                **font.mark(),
                "baseline": "middle",
                "dy": round(offset + height / 2, 2),
            }
            mark.pop("lineBreak", None)
            if "color" in vl_mark:
                mark["color"] = self.text_fill(marks, vl_mark["color"], style)
            layers.append(
                {"mark": mark, "encoding": {"text": {"field": field_name, "type": "nominal"}}}
            )
            offset += height
        return {"encoding": {k: v for k, v in encoding.items() if k != "text"}, "layer": layers}

    def line_breaks(self, vl_type: str, marks: Marks, dims: list[FieldUse]) -> list[FieldUse]:
        """折線與區域依沿著線的那一架上的離散維度分開各條線（與 Tableau 相同，線不跨窗格）：沒有路徑時
        沿著最內層的維度連接、外層的維度分開；有路徑時依路徑連接，架上的維度都分開（例如啞鈴圖）。"""
        if vl_type not in ("line", "area"):
            return []
        discrete = [u for u in dims if not u.continuous]
        if marks.path is not None:
            return discrete
        return [u for u in discrete if u is not dims[-1]]

    # ------------------------------------------------------------ 標記標籤

    def label_layers(
        self,
        marks: Marks,
        encoding: dict[str, Any],
        vl_mark: dict[str, Any],
        vl_type: str,
        field_name: str,
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> list[dict[str, Any]]:
        """標記標籤的圖層：只標示部分標記時依資料中算好的旗標篩選（label_flags），折線與區域的最小值寫在
        點的下方；與 Tableau 相同，互相重疊的標籤由檢視器隱藏（允許重疊時不隱藏）。"""
        setting = marks.labels or MarkLabels()
        stacked = (
            vl_type == "bar"
            and self.ws.stack != "off"
            and (
                encoding.get("color", {}).get("type") == "nominal"
                or any(self.ch(u).role == "dimension" for u in marks.detail)
            )
        )
        size = marks.mark_size if marks.mark_size is not None else 1
        if setting.wrap:
            field_name = self.wrapped(marks, field_name)

        def layer(extreme: Literal["max", "min"] | None = None) -> dict[str, Any]:
            found = _label_layer(
                encoding,
                vl_type,
                field_name,
                self.text_fill(marks, self.label_color(marks, vl_mark)),
                self.text_font(marks),
                marks.label_match,
                marks.cell_align,
                POINT_DIAMETER * size / 2,
                extreme,
                stacked,
                marks.label_valign,
            )
            if setting.direction is not None:
                _rotate_label(found["mark"], setting.direction)
            return found

        layers: list[dict[str, Any]]
        if setting.marks in ("selected", "highlighted"):
            # 只標示選取（醒目提示）的標記：檢視器依選取的列改寫篩選
            found = layer()
            found["transform"] = [{"filter": "false"}]
            return [self.named_label(found, "s")]
        flag = self.label_flags(marks, setting, rows_meas, cols_meas)
        if flag is None:
            layers = [layer()]
        else:
            test = f"datum[{json.dumps(flag)}]"
            if setting.marks == "min_max" and vl_type in ("line", "area"):
                layers = []
                for kind, wanted in (("max", setting.maximum), ("min", setting.minimum)):
                    if wanted:
                        found = layer("max" if kind == "max" else "min")
                        found["transform"] = [{"filter": f"{test} === '{kind}'"}]
                        layers.append(found)
            else:
                layers = [layer()]
                layers[0]["transform"] = [{"filter": test}]
        # 堆疊長條各段內的標籤放不下時與 Tableau 相同不顯示（檢視器依畫好的長條判斷：橫的長條看寬度，
        # 直的看高度）
        horizontal = encoding.get("x", {}).get("type") == "quantitative"
        kind = "k" if setting.overlap else ("ix" if horizontal else "iy") if stacked else "c"
        return [self.named_label(found, kind) for found in layers]

    def wrapped(self, marks: Marks, field_name: str) -> str:
        """換行的標籤文字（標籤對齊的「換行：開」）：資料中加上換好行的欄，回傳它的名稱。"""
        self.wrap_count += 1
        name = f"__wrap{self.wrap_count}"
        size = self.text_font(marks).size
        for record in self.records_of(marks):
            value = record.get(field_name)
            record[name] = None if value is None else _wrap(value, LABEL_WRAP_WIDTH, size)
        return name

    def named_label(self, layer: dict[str, Any], kind: str) -> dict[str, Any]:
        """標籤圖層的名稱：__label_c（重疊時隱藏）、__label_ix／__label_iy（堆疊長條各段內的標籤：重疊或
        寬度／高度放不下時隱藏）、__label_k（允許重疊）、__label_s（只標示選取的）。隱藏的標籤依參數
        __label_hidden（檢視器算出的標籤 id）清空文字。"""
        self.label_count += 1
        if kind in ("c", "ix", "iy"):
            text = layer["encoding"]["text"]
            layer["encoding"]["text"] = {
                "condition": {
                    "test": f"indexof({LABEL_HIDDEN}, datum.{LABEL_ID}) >= 0",
                    "value": "",
                },
                **text,
            }
            if not self.culled:
                # 每一列的標籤 id（在分面等複製資料之前加上）
                records = itertools.chain(self.values, *self.pane_values.values())
                for n, record in enumerate(records):
                    record[LABEL_ID] = n
                self.culled = True
        return {"name": f"__label_{kind}{self.label_count}", **layer}

    def label_flags(
        self,
        marks: Marks,
        setting: MarkLabels,
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> str | None:
        """最小值／最大值、線條兩端：在資料中標記要加上標籤的列（'min'、'max'、'first'、'last'）；回傳
        欄名（每張標記卡一欄）。全部的標記都加上標籤時為 None。"""
        if setting.marks not in ("min_max", "line_ends"):
            return None
        records = [r for r in self.records_of(marks) if not r.get(TOTAL)]
        name = f"__lab{self.label_count + 1}"
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        scope: LabelScope = setting.scope if setting.marks == "min_max" else "line"
        parts = self.label_parts(marks, scope)
        for record in records:
            key = tuple(_hashable(record.get(f)) for f in parts)
            groups.setdefault(key, []).append(record)
        if setting.marks == "min_max":
            value = self.label_value(marks, setting, rows_meas, cols_meas)
            if value is None:
                self.notes.append("找不到標示最小值／最大值的量值，標籤未畫出")
                return name
            for members in groups.values():
                numbers = [r for r in members if _is_number(r.get(value))]
                if not numbers:
                    continue
                if setting.minimum:
                    min(numbers, key=lambda r: float(r[value]))[name] = "min"
                if setting.maximum:
                    # 同樣大時標示第一個
                    best = max(float(r[value]) for r in numbers)
                    next(r for r in numbers if float(r[value]) == best)[name] = "max"
            return name
        along = self.label_along(marks, rows_meas, cols_meas)
        for members in groups.values():
            ordered = (
                sorted(members, key=lambda r: _sort_key(r.get(along)))
                if along is not None
                else members
            )
            if setting.first and ordered:
                ordered[0][name] = "first"
            if setting.last and ordered:
                ordered[-1][name] = "last"
        return name

    def label_parts(self, marks: Marks, scope: LabelScope) -> list[str]:
        """最小值／最大值的範圍（線條兩端的每條線）的分組欄：窗格為欄列架上外層的離散欄位（架上最後是離散
        欄位時它切出儲存格，不分組），儲存格為所有離散欄位，每條線另加標記卡上的維度（路徑除外）。"""
        if scope == "table":
            return []
        uses: list[FieldUse] = []
        for shelf in (self.rows, self.columns):
            discrete = [u for u in shelf if not self.is_axis(u) and not u.continuous]
            ends = bool(shelf) and not self.is_axis(shelf[-1]) and not shelf[-1].continuous
            uses += discrete[:-1] if scope != "cell" and ends else discrete
        if scope == "line":
            on_card = [marks.color, marks.size, marks.shape, *marks.detail, *marks.label]
            uses += [u for u in on_card + marks.tooltip if u is not None and not self.is_measure(u)]
        names: list[str] = []
        for use in uses:
            name = self.ch(use).name
            if name not in names:
                names.append(name)
        return names

    def label_value(
        self,
        marks: Marks,
        setting: MarkLabels,
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> str | None:
        """最小值／最大值依據的量值（原始值欄）：指定的欄位，其次是數值座標軸上的量值、標籤上的量值、大小上
        的量值。"""
        if setting.field is not None and use_key(setting.field) in self.channels:
            return self.ch(setting.field).name
        axis = rows_meas or cols_meas  # 兩個座標軸都是量值時（散佈圖）為列上的量值
        candidates = [*axis, *marks.label, *([marks.size] if marks.size is not None else [])]
        found = next((u for u in candidates if self.is_measure(u)), None)
        return self.ch(found).name if found is not None else None

    def label_along(
        self, marks: Marks, rows_meas: list[FieldUse], cols_meas: list[FieldUse]
    ) -> str | None:
        """線條兩端的順序：路徑，其次是沿著線的連續維度（例如日期的座標軸）；都沒有時依資料的順序（離散
        的座標依資料的順序排列）。"""
        if marks.path is not None:
            return self.ch(marks.path).name
        shelf = self.rows if cols_meas and not rows_meas else self.columns
        along = [u for u in shelf if u.continuous and not self.is_axis(u)]
        return self.ch(along[-1]).name if along else None

    # ------------------------------------------------------------ 沒有座標軸的排列

    def packed(self, marks: Marks, mark: str) -> tuple[dict[str, Any], str] | None:
        """欄列架上都沒有欄位時的多個標記（與 Tableau 相同）：方塊為樹狀圖、圓形為泡泡圖、文字為文字雲
        （排列見 tmig.render.packing）。只有一個方塊或文字時照常畫成一格，標記太多的文字雲也是。"""
        records = [r for r in self.records_of(marks) if not r.get(TOTAL)]
        minimum = 1 if mark == "circle" else 2
        if mark not in ("square", "circle", "text") or len(records) < minimum:
            return None
        if mark == "text" and len(records) > MAX_CLOUD:
            return None
        encoding: dict[str, Any] = {}
        self.common(encoding, marks)
        encoding.pop("size", None)  # 大小決定排列：以算好的位置與大小畫出
        sizes = self.size_values(marks, records)
        color = encoding.get("color")
        groups = (
            [r.get(color["field"]) for r in records]
            if isinstance(color, dict) and color.get("type") == "nominal"
            else None
        )
        font = self.text_font(marks)
        # 文字雲的文字不受「顯示標記標籤」影響；樹狀圖、泡泡圖的是標記標籤
        label = self.text_field(marks) if mark == "text" else self.label_field(marks)
        if mark == "text":
            if label is None:
                measures = [
                    c
                    for c in self.channels.values()
                    if c.role == "measure" and c.datatype != "spatial"
                ]
                label = measures[0].label if measures else None
            if label is None:
                return None
            return self.word_cloud(marks, encoding, records, sizes, label, font), mark
        if mark == "square":
            return self.treemap(marks, encoding, records, sizes, groups, label, font), mark
        return self.bubbles(marks, encoding, records, sizes, groups, label, font), mark

    def size_values(self, marks: Marks, records: list[dict[str, Any]]) -> list[float | None]:
        """各標記的大小：大小上的量值（不是數字的為 None），離散的欄位依值出現的順序，沒有欄位時都相同。"""
        if marks.size is None:
            return [1.0] * len(records)
        ch = self.ch(marks.size)
        if ch.role == "measure" and ch.datatype not in ("string", "boolean"):
            return [float(r[ch.name]) if _is_number(r.get(ch.name)) else None for r in records]
        order = {v: n + 1 for n, v in enumerate(dict.fromkeys(r.get(ch.name) for r in records))}
        return [float(order[r.get(ch.name)]) for r in records]

    def contrast(self, marks: Marks, encoding: dict[str, Any]) -> dict[str, Any]:
        """標記上的文字顏色：設定了標籤顏色時用它，否則依標記的顏色自動取白色或深色（與 Tableau 相同）。"""
        if marks.label_color is not None:
            return {"value": _css_color(marks.label_color)}
        color = encoding.get("color")
        if isinstance(color, dict) and isinstance(color.get("field"), str):
            field = json.dumps(color["field"])
            test = f"luminance(scale('color', datum[{field}])) < {DARK_LUMINANCE}"
            return {"condition": {"test": test, "value": "#ffffff"}, "value": TEXT_COLOR}
        fill = marks.mark_color or DEFAULT_MARK_COLOR
        return {"value": "#ffffff" if _luminance(fill) < DARK_LUMINANCE else TEXT_COLOR}

    def treemap(
        self,
        marks: Marks,
        encoding: dict[str, Any],
        records: list[dict[str, Any]],
        sizes: list[float | None],
        groups: list[Any] | None,
        label: str | None,
        font: Font,
    ) -> dict[str, Any]:
        """樹狀圖：0–1 的座標（y 向下）填滿整張圖，長方形依工作表在 dashboard 上的長寬比排列。"""
        width, height = self.extent
        rects = packing.treemap(sizes, groups, width / height)
        for record, rect in zip(records, rects, strict=True):
            if rect is None:
                continue
            x, y, w, h = rect
            record[TREEMAP[0]], record[TREEMAP[1]] = round(x, 6), round(x + w, 6)
            record[TREEMAP[2]], record[TREEMAP[3]] = round(y, 6), round(y + h, 6)
            if label is not None:
                # 依預期的大小換行（與 Tableau 相同，放不下的標籤不顯示）
                record[WRAPPED] = _wrap(record.get(label), 0.9 * w * width - 8, font.size)
                record[LABEL_W], record[LABEL_H] = _text_extent(record[WRAPPED], font.size)
        unit = {"domain": [0, 1], "nice": False, "zero": False}
        x = {"field": TREEMAP[0], "type": "quantitative", "axis": None, "scale": unit}
        y = {
            "field": TREEMAP[2],
            "type": "quantitative",
            "axis": None,
            "scale": {**unit, "reverse": True},
        }
        rect = _mark_style({"type": "rect", "tooltip": True}, marks)
        if marks.stroke is None:
            rect.update(stroke="#ffffff", strokeWidth=1)  # 方塊之間的白色分隔線
        layers: list[dict[str, Any]] = [
            {
                "mark": rect,
                "encoding": {
                    "x": x,
                    "x2": {"field": TREEMAP[1]},
                    "y": y,
                    "y2": {"field": TREEMAP[3]},
                },
            }
        ]
        if label is not None:
            x0, x1, y0, y1 = (f"datum[{json.dumps(n)}]" for n in TREEMAP)
            fits = (
                f"({x1} - {x0}) * width >= datum[{json.dumps(LABEL_W)}] + 8 && "
                f"({y1} - {y0}) * height >= datum[{json.dumps(LABEL_H)}] + 6"
            )
            text = {
                "type": "text",
                "align": "left",
                "baseline": "top",
                "dx": 4,
                "dy": 3,
                "lineBreak": "\n",
                **font.mark(),
            }
            layers.append(
                {
                    "mark": text,
                    "encoding": {
                        "x": x,
                        "y": y,
                        # 放不下的標籤不顯示（與 Tableau 相同）
                        "text": {"condition": {"test": fits, "field": WRAPPED}, "value": ""},
                        "color": self.contrast(marks, encoding),
                    },
                }
            )
        return {"encoding": encoding, "layer": layers}

    def bubbles(
        self,
        marks: Marks,
        encoding: dict[str, Any],
        records: list[dict[str, Any]],
        sizes: list[float | None],
        groups: list[Any] | None,
        label: str | None,
        font: Font,
    ) -> dict[str, Any]:
        """泡泡圖：面積與大小成比例；排好的泡泡依圖的寬高等比例縮放到填滿（圓維持圓形）。"""
        radii = [math.sqrt(v) if v is not None and v > 0 else 0.0 for v in sizes]
        shown = [r for r in radii if r > 0]
        padding = (sum(shown) / len(shown)) * 0.08 if shown else 0.0
        centers = packing.pack(radii, groups, padding)
        box: list[float] = []
        for record, radius, center in zip(records, radii, centers, strict=True):
            if center is None:
                continue
            record[BUBBLE[0]], record[BUBBLE[1]] = round(center[0], 6), round(center[1], 6)
            record[BUBBLE[2]] = round(radius, 6)
            box = _extend(
                box, center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius
            )
        if not box:
            box = [-1.0, -1.0, 1.0, 1.0]
        span = (max(box[2] - box[0], 1e-9), max(box[3] - box[1], 1e-9))
        if label is not None:
            # 依預期的大小換行：泡泡直徑的四分之三
            pixels = min(self.extent[0] / span[0], self.extent[1] / span[1])
            for record, radius in zip(records, radii, strict=True):
                if radius > 0:
                    record[WRAPPED] = _wrap(record.get(label), 1.5 * radius * pixels, font.size)
                    record[LABEL_W], record[LABEL_H] = _text_extent(record[WRAPPED], font.size)
        k = f"min(width / {_num(span[0])}, height / {_num(span[1])})"
        transform = [
            {"calculate": f"pow(2 * datum[{json.dumps(BUBBLE[2])}] * {k}, 2)", "as": BUBBLE[3]}
        ]
        x, y = _fitted_axes(BUBBLE[0], BUBBLE[1], box, k)
        circle = _mark_style({"type": "circle", "tooltip": True, "opacity": 1}, marks)
        layers: list[dict[str, Any]] = [
            {
                "mark": circle,
                "encoding": {
                    "x": x,
                    "y": y,
                    "size": {"field": BUBBLE[3], "type": "quantitative", "scale": None},
                },
            }
        ]
        if label is not None:
            diameter = f"2 * datum[{json.dumps(BUBBLE[2])}] * {k}"
            # 標籤的外框在圓內（對角線不超過直徑）
            fits = (
                f"pow(datum[{json.dumps(LABEL_W)}] + 4, 2) + pow(datum[{json.dumps(LABEL_H)}], 2)"
                f" <= pow({diameter}, 2)"
            )
            text = {
                "type": "text",
                "align": "center",
                "baseline": "middle",
                "lineBreak": "\n",
                **font.mark(),
            }
            layers.append(
                {
                    "mark": text,
                    "encoding": {
                        "x": x,
                        "y": y,
                        "text": {"condition": {"test": fits, "field": WRAPPED}, "value": ""},
                        "color": self.contrast(marks, encoding),
                    },
                }
            )
        return {"transform": transform, "encoding": encoding, "layer": layers}

    def word_cloud(
        self,
        marks: Marks,
        encoding: dict[str, Any],
        records: list[dict[str, Any]],
        sizes: list[float | None],
        label: str,
        font: Font,
    ) -> dict[str, Any]:
        """文字雲：字級依大小（最小到最大約 1 到 3 倍）；放不下時整體等比例縮小。"""
        scale = marks.mark_size if marks.mark_size else 1.0
        valid = [v for v in sizes if v is not None]
        low, high = (min(valid), max(valid)) if valid else (0.0, 0.0)
        order: list[tuple[int, float]] = []
        for n, value in enumerate(sizes):
            if value is None or records[n].get(label) in (None, ""):
                continue
            share = (
                (value - low) / (high - low) if high > low else (0.0 if marks.size is None else 0.5)
            )
            order.append((n, font.size * scale * (1 + 2 * share)))
        order.sort(key=lambda item: -item[1])  # 大的字先放（同樣大時依資料的順序）
        boxes = []
        for n, size in order:
            width, height = _text_extent(records[n].get(label), size)
            boxes.append((width + 4, height))
        box: list[float] = []
        for (n, size), center, (width, height) in zip(
            order, packing.word_cloud(boxes), boxes, strict=True
        ):
            record = records[n]
            record[CLOUD[0]], record[CLOUD[1]], record[CLOUD[2]] = (
                round(center[0], 3),
                round(center[1], 3),
                round(size, 3),
            )
            box = _extend(
                box,
                center[0] - width / 2,
                center[1] - height / 2,
                center[0] + width / 2,
                center[1] + height / 2,
            )
        if not box:
            box = [-1.0, -1.0, 1.0, 1.0]
        k = f"min(1, width / {_num(box[2] - box[0])}, height / {_num(box[3] - box[1])})"
        transform = [{"calculate": f"datum[{json.dumps(CLOUD[2])}] * {k}", "as": CLOUD[3]}]
        x, y = _fitted_axes(CLOUD[0], CLOUD[1], box, k)
        text: dict[str, Any] = {
            "type": "text",
            "tooltip": True,
            "align": "center",
            "baseline": "middle",
            "lineBreak": "\n",
            **font.mark(),
        }
        text.pop("fontSize", None)
        if marks.color is None:
            text["color"] = (
                _css_color(marks.mark_color)
                if marks.mark_color is not None
                else self.text_color(marks, cell=True)
            )
        if marks.opacity is not None:
            text["opacity"] = marks.opacity
        encoding.update(
            x=x,
            y=y,
            text={"field": label, "type": "nominal"},
            size={"field": CLOUD[3], "type": "quantitative", "scale": None},
        )
        return {"transform": transform, "mark": text, "encoding": encoding}

    def gantt(
        self,
        marks: Marks,
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> tuple[dict[str, Any], str] | None:
        """甘特條：從連續座標軸上的位置開始，長度為「大小」的量值（日期軸以天為單位，可以是負數）；
        沒有大小時畫成細線。連續座標軸只能有一個（欄或列，同一架上沒有其他維度），另一架上的離散維度
        分列；不符合時回傳 None。"""

        def continuous(dims: list[FieldUse]) -> list[FieldUse]:
            return [u for u in dims if _dimension(self.ch(u))["type"] != "ordinal"]

        layout: (
            tuple[Literal["rows", "columns"], FieldUse, bool, list[FieldUse], list[FieldUse]] | None
        ) = None
        shelves: list[
            tuple[Literal["rows", "columns"], list[FieldUse], list[FieldUse], list[FieldUse]]
        ] = [
            ("columns", cols_meas, cols_dims, rows_dims),
            ("rows", rows_meas, rows_dims, cols_dims),
        ]
        for shelf, meas, dims, other in shelves:
            if meas and len(meas) == 1 and not continuous(dims):
                layout = (shelf, meas[0], True, other, dims)
            elif not meas and len(dims) == 1 and continuous(dims):
                layout = (shelf, dims[0], False, other, [])
            else:
                continue
            break
        if layout is None:
            return None
        shelf, axis_use, is_measure, other, same = layout
        if same or continuous(other) or (rows_meas and cols_meas):
            return None
        position, band = ("x", "y") if shelf == "columns" else ("y", "x")
        if is_measure:
            pos = self.axis_measure(axis_use, shelf)
        else:
            found = self.axis_dim([axis_use], "cols_dim" if shelf == "columns" else "rows_dim")
            assert found is not None
            pos = found
        temporal = pos.get("type") == "temporal"
        if temporal:
            # 位置是資料中的日期本身（已截斷），以 UTC 呈現；結束的時間以毫秒計算
            pos.pop("timeUnit", None)
            pos["scale"] = {**pos.get("scale", {}), "type": "utc"}
        encoding: dict[str, Any] = {position: pos}
        band_enc = self.axis_dim(other, "rows_dim" if shelf == "columns" else "cols_dim")
        if band_enc is not None:
            encoding[band] = band_enc
        transform: list[dict[str, Any]] = []
        vl_mark: dict[str, Any]
        if marks.size is not None:
            start = f"datum[{json.dumps(pos['field'])}]"
            length = f"datum[{json.dumps(self.ch(marks.size).name)}]"
            end = (
                f"time(toDate({start})) + {length} * {GANTT_DAY_MS}"
                if temporal
                else f"{start} + {length}"
            )
            transform.append({"calculate": end, "as": "__gantt_end"})
            encoding[f"{position}2"] = {"field": "__gantt_end"}
            vl_mark = {"type": "bar", "tooltip": True}
        else:
            vl_mark = {
                "type": "tick",
                "tooltip": True,
                "orient": "vertical" if position == "x" else "horizontal",
                "thickness": 2,
            }
        self.common(encoding, marks)
        encoding.pop("size", None)  # 「大小」是甘特條的長度
        _mark_style(vl_mark, marks)
        self.size_mark(vl_mark, encoding, marks)
        if self.label_field(marks) is not None:
            self.notes.append("甘特條的標籤尚未畫出")
        body: dict[str, Any] = {"mark": vl_mark, "encoding": encoding}
        if transform:
            body["transform"] = transform
        return body, "gantt"

    def size_mark(self, vl_mark: dict[str, Any], encoding: dict[str, Any], marks: Marks) -> None:
        """標記卡的「大小」：離散的列（欄）上長條占列高（欄寬）的比例，圓點與形狀的直徑，折線的寬度
        （依 Tableau 的畫面量測，見 BAR_BAND、POINT_DIAMETER、LINE_WIDTH）。依欄位決定大小時不變。"""
        size = marks.mark_size
        kind = vl_mark["type"]
        if kind == "bar":
            band = next(
                (
                    c
                    for c in ("y", "x")
                    if isinstance(encoding.get(c), dict)
                    and encoding[c].get("type") in ("ordinal", "nominal")
                ),
                None,
            )
            if band is None or "size" in encoding:
                return
            fraction = BAR_BAND if size is None else min(size / 2, 1)
            vl_mark["height" if band == "y" else "width"] = {"band": round(fraction, 4)}
            # 比例以整列（欄）計算：列之間沒有 Vega-Lite 預設的空隙
            scale = dict(encoding[band].get("scale") or {})
            scale.update(type="band", paddingInner=0, paddingOuter=0)
            encoding[band]["scale"] = scale
        elif kind in ("circle", "square", "point") and "size" not in encoding:
            diameter = POINT_DIAMETER * (size if size is not None else 1)
            vl_mark["size"] = round(diameter**2, 2)  # Vega 的 size 是外框的面積
        elif kind == "line" and not vl_mark.get("filled"):
            vl_mark["strokeWidth"] = round(LINE_WIDTH * (size if size is not None else 1), 2)
            # 與 Tableau 相同，線的兩端與轉折是圓的（粗的線最明顯，例如啞鈴圖）
            vl_mark.update(strokeCap="round", strokeJoin="round")

    def common(self, encoding: dict[str, Any], marks: Marks) -> None:
        """顏色、大小、形狀、詳細資料與工具提示。"""
        names_only = (
            marks.color is not None
            and marks.color.field == MEASURE_NAMES
            and MEASURE_NAMES not in self.channels
        )
        # 沒有 Measure Values 時色彩上的 Measure Names（雙軸）：各量值一個顏色，由 multi 加上
        if marks.color is not None and not names_only:
            ch = self.ch(marks.color)
            records = self.records_of(marks)
            # 量值以連續色彩呈現；彙總後是布林或文字（例如 ATTR([是否本週])）時與 Tableau 相同為離散色彩
            if ch.role == "measure" and ch.datatype not in ("boolean", "string"):
                # 預設與 Tableau 相同，總計不影響色階的範圍（見 _exclude_totals_from_color）
                numbers = [
                    float(r[ch.name])
                    for r in records
                    if _is_number(r.get(ch.name)) and (self.ws.totals_colored or not r.get(TOTAL))
                ]
                encoding["color"] = {
                    "field": ch.name,
                    "type": "quantitative",
                    "title": ch.title,
                    "scale": continuous_scale(marks.color_palette, numbers),
                }
            else:
                encoding["color"] = {
                    "field": ch.label,
                    "type": "nominal",
                    "title": ch.title,
                    "sort": None,
                }
                setting = marks.color_palette
                if setting is None or setting.kind != "discrete":
                    setting = AUTOMATIC_DISCRETE
                more = [
                    self.ch(u)
                    for u in marks.color_more
                    if u in marks.detail and u != marks.color and self.ch(u).role == "dimension"
                ]
                if more and ch.use.field != MEASURE_NAMES:
                    scale = self.combined_color(encoding, setting, [ch, *more], records)
                else:
                    scale = discrete_scale(
                        setting,
                        ((r.get(ch.label), r.get(ch.name)) for r in records),
                        self.measure_names if ch.use.field == MEASURE_NAMES else None,
                        members_first=ch.is_set,
                    )
                if scale is not None:
                    encoding["color"]["scale"] = scale
        if marks.size is not None:
            ch = self.ch(marks.size)
            encoding["size"] = {"field": ch.name, "type": "quantitative", "title": ch.title}
        if marks.shape is not None:
            ch = self.ch(marks.shape)
            shape: dict[str, Any] = {"field": ch.label, "type": "nominal", "title": ch.title}
            encoding["shape"] = shape
            # 指定的形狀與 Tableau 預設形狀調色盤的順序（成員依原值排序，不隨工作表的排序改變）
            shapes = shape_scale(
                marks.shape_assignments,
                (
                    (r.get(ch.label), r.get(ch.name))
                    for r in self.records_of(marks)
                    if not r.get(TOTAL)
                ),
            )
            if shapes is not None:
                shape["scale"] = shapes
        for use in marks.detail:
            ch = self.ch(use)
            if ch.role == "dimension":
                encoding.setdefault("detail", []).append({"field": ch.name, "type": "nominal"})
        # 空間資料（形狀）不在工具提示中（與 Tableau 相同）；膠囊選單取消「包含在工具提示中」的欄位也不列
        excluded = {use_key(u) for u in self.ws.tooltip_excluded}
        encoding["tooltip"] = [
            {"field": ch.label, "title": ch.title}
            for key, ch in self.channels.items()
            if ch.datatype != "spatial" and key not in excluded
        ]

    def combined_color(
        self,
        encoding: dict[str, Any],
        setting: ColorPalette,
        parts: list[Channel],
        records: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """色彩上有多個維度（Tableau 以欄位值的組合著色）：資料中加上各欄位顯示值以「, 」連接的欄，
        依它著色；圖例的成員依各欄位的原值排序（第一個欄位在前），指定的顏色以組合的文字對應。"""
        self.combo_count += 1
        name = f"__color{self.combo_count}"
        order: dict[str, tuple[Any, ...]] = {}
        for record in records:
            label = ", ".join(
                "Null" if record.get(c.label) is None else str(record.get(c.label)) for c in parts
            )
            record[name] = label
            order.setdefault(label, tuple(member_order(record.get(c.name)) for c in parts))
        encoding["color"] = {
            "field": name,
            "type": "nominal",
            "title": ", ".join(c.title for c in parts),
            "sort": None,
        }
        labels = sorted(order, key=lambda label: order[label])
        # keys 不是 None 時依給的順序（已經排好），指定的顏色以組合的文字對應
        return discrete_scale(setting, ((label, label) for label in labels), {})

    # ------------------------------------------------------------ 多個量值

    def names_scale(self, measures: list[FieldUse]) -> dict[str, Any] | None:
        """色彩上的 Measure Names（沒有 Measure Values，例如雙軸）：每個量值一個顏色的比例尺，
        依量值的名稱；色彩的設定（指定的顏色）以量值的欄位鍵對應。"""
        if MEASURE_NAMES in self.channels:
            return None
        cards = [self.ws.marks, *(p.marks for p in self.ws.panes)]
        named = [m for m in cards if m.color is not None and m.color.field == MEASURE_NAMES]
        if not named:
            return None
        setting = next((m.color_palette for m in named if m.color_palette is not None), None)
        if setting is None or setting.kind != "discrete":
            setting = AUTOMATIC_DISCRETE
        titles = [self.ch(u).title for u in measures]
        keys: dict[Scalar, Any] = {use_key(u): self.ch(u).title for u in measures}
        return discrete_scale(setting, ((t, t) for t in titles), keys)

    def folded_names_scale(self, measures: list[FieldUse]) -> dict[str, Any] | None:
        """Measure Values 與其他量值在同一架上（各自一個軸或雙軸）、色彩是 Measure Names：Measure Values 的
        各量值與其他量值共用一個比例尺（其他量值以自己的名稱上色，與 Tableau 相同）。"""
        if MEASURE_VALUES not in self.channels:
            return None
        others = [u for u in measures if u.field != MEASURE_VALUES]
        cards = [self.ws.marks, *(p.marks for p in self.ws.panes)]
        named = [m for m in cards if m.color is not None and m.color.field == MEASURE_NAMES]
        if not others or not named:
            return None
        setting = next((m.color_palette for m in named if m.color_palette is not None), None)
        if setting is None or setting.kind != "discrete":
            setting = AUTOMATIC_DISCRETE
        titles = [*self.measure_names.values(), *(self.ch(u).title for u in others)]
        keys: dict[Scalar, Any] = {
            **self.measure_names,
            **{use_key(u): self.ch(u).title for u in others},
        }
        return discrete_scale(setting, ((t, t) for t in titles), keys)

    def multi(
        self,
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> tuple[dict[str, Any], str]:
        """同一架上有多個量值：每個量值一張圖；雙軸的量值與前一張重疊。"""
        axis: Literal["rows", "columns"] = "rows" if len(rows_meas) > 1 else "columns"
        if rows_meas and cols_meas and len(rows_meas) > 1 and len(cols_meas) > 1:
            self.notes.append("欄架與列架都有多個量值，只畫欄架的第一個量值")
            cols_meas = cols_meas[:1]
        measures = rows_meas if axis == "rows" else cols_meas
        remaining = list(self.ws.panes)
        charts: list[dict[str, Any]] = []
        kinds: list[str] = []
        names_scale = self.names_scale(measures)
        folded_scale = self.folded_names_scale(measures)
        folded = MEASURE_VALUES in self.channels
        for use in measures:
            pane = _take_pane(remaining, use, axis)
            marks = pane.marks if pane is not None else self.ws.marks
            mark = marks.type
            if mark == "automatic":
                mark = _infer_mark(
                    rows_dims,
                    cols_dims,
                    [use] if axis == "rows" else rows_meas,
                    [use] if axis == "columns" else cols_meas,
                    self.is_continuous_date,
                )
            if mark in UNSUPPORTED_MARKS or mark == "pie":
                self.notes.append(f"{self.ch(use).title} 的標記類型 {mark} 尚未支援，以長條呈現")
                mark = "bar"
            if axis == "rows":
                body, kind = self.unit(marks, mark, rows_dims, cols_dims, [use], cols_meas)
            else:
                body, kind = self.unit(marks, mark, rows_dims, cols_dims, rows_meas, [use])
            body = self.with_pane_data(body, pane)
            if (
                names_scale is not None
                and marks.color is not None
                and marks.color.field == MEASURE_NAMES
            ):
                _color_datum(body, self.ch(use).title, names_scale)
            if folded and use.field != MEASURE_VALUES:
                # Measure Values 以外的量值：長表中每列在每個 Measure Values 的量值重複一次，只取一份；
                # 色彩上的 Measure Names 是這個量值自己的名稱
                _first_fold_only(body)
                if (
                    folded_scale is not None
                    and marks.color is not None
                    and marks.color.field == MEASURE_NAMES
                ):
                    # 比例尺在 Measure Values 的圖上（含這個量值的名稱）
                    _color_datum(body, self.ch(use).title, None, replace=True)
            elif folded_scale is not None:
                _names_scale(body, folded_scale)
            if marks.trend is not None:
                _trend_lines(self, body, marks.trend, self.records_of(marks))
            kinds.append(kind)
            if pane is not None and pane.dual and charts:
                channel = "y" if axis == "rows" else "x"
                previous = charts.pop()
                layers = (
                    previous["layer"]
                    if "layer" in previous and "resolve" in previous
                    else [previous]
                )
                setting = self.axis_of(use, axis)
                synchronized = setting is not None and setting.synchronized
                # 同步的雙軸共用刻度，否則兩個軸各自的刻度
                scales = {channel: "shared" if synchronized else "independent"}
                for name in ("color", "size"):
                    # 兩個軸的標記卡以不同的欄位著色（或決定大小）：各自的比例尺與圖例（與 Tableau 相同）
                    fields = {_channel_field(layer, name) for layer in [*layers, body]}
                    if len(fields - {None}) > 1:
                        scales[name] = "independent"
                charts.append({"layer": [*layers, body], "resolve": {"scale": scales}})
            else:
                charts.append(body)
        if len(charts) == 1:
            return charts[0], "+".join(kinds)
        return ({"vconcat": charts} if axis == "rows" else {"hconcat": charts}), "+".join(kinds)

    # ------------------------------------------------------------ 圓餅

    def pie(
        self, marks: Marks, rows_dims: list[FieldUse], cols_dims: list[FieldUse]
    ) -> dict[str, Any]:
        """圓餅：角度為 wedge-size，顏色為顏色欄位；欄列架上的維度以分面呈現（Vega-Lite 的 facet，每一格
        一個圓餅與它的標籤）。"""
        encoding: dict[str, Any] = {}
        if marks.angle is not None:
            encoding["theta"] = {
                "field": self.ch(marks.angle).name,
                "type": "quantitative",
                "stack": True,
            }
        else:
            # 角度上沒有欄位：各塊一樣大（與 Tableau 相同），以每列都是 1 的欄堆疊
            for record in self.records_of(marks):
                record[PIE_ONE] = 1
            encoding["theta"] = {"field": PIE_ONE, "type": "quantitative", "stack": True}
        self.common(encoding, marks)
        if marks.color is not None and encoding["color"]["type"] == "nominal":
            encoding["order"] = {"field": ROW}
        facet: dict[str, Any] = {}
        if rows_dims:
            facet["row"] = self._facet(rows_dims, "rows_dim")
        if cols_dims:
            facet["column"] = self._facet(cols_dims, "cols_dim")
        mark = _mark_style({"type": "arc", "tooltip": True}, marks)
        # 分面時每一格的大小是 child_width、child_height（width、height 是整張圖的）
        self.pie_extent = "min(child_width, child_height)" if facet else "min(width, height)"
        mark["outerRadius"] = {"expr": f"{self.pie_extent} * {_pie_fraction(marks) / 2}"}
        layers: list[dict[str, Any]] = [{"mark": mark, "encoding": encoding}]
        # 開啟標記標籤、標籤上沒有欄位時標示角度的量值
        found = self.label_field(
            marks, self.ch(marks.angle).label if marks.angle is not None else None
        )
        if found is not None:
            if id(marks) in self.label_fields or not marks.label:
                text = "{" + found + "}"
            else:
                text = "\n".join("{" + self.ch(u).label + "}" for u in marks.label)
            label = {
                "mark": {
                    "type": "text",
                    **self.text_font(marks).mark(),
                    "lineBreak": "\n",
                    # 與 Tableau 相同寫在圓餅外側
                    "radius": {"expr": f"{self.pie_extent} * {_pie_fraction(marks) / 2} + 24"},
                },
                "encoding": {
                    "text": {"field": "__pie_label"},
                    "color": {"value": self.text_fill(marks, self.text_color(marks))},
                    "theta": encoding["theta"],
                    **({"order": encoding["order"]} if "order" in encoding else {}),
                },
                "transform": [{"calculate": _template(text), "as": "__pie_label"}],
            }
            setting = marks.labels or MarkLabels()
            layers.append(self.named_label(label, "k" if setting.overlap else "c"))
        if facet:
            # 分面中的各圖層各自的 encoding（中間的文字等圖層不能沿用角度）
            return {"facet": facet, "spec": {"layer": layers}}
        if len(layers) == 1:
            return layers[0]
        return {"encoding": encoding, "layer": [{"mark": mark}, *layers[1:]]}

    def donut(
        self,
        body: dict[str, Any],
        values: list[dict[str, Any]],
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
    ) -> dict[str, Any]:
        """甜甜圈（J3）與多層圓餅：圓餅與疊在上面（雙軸）的標記卡。有角度與顏色的標記卡畫成另一層圓餅
        （依自己的詳細程度與大小，例如內圈依類別、外圈依子類別），其他的畫成中間的洞，它的標籤（通常是
        合計）寫在中間；分面時每一格各自的洞與標籤。"""
        dual = [(i, p) for i, p in enumerate(self.ws.panes) if i > 0 and p.dual]
        rings = [(i, p) for i, p in dual if _is_ring(p.marks)]
        holes = [(i, p) for i, p in dual if not _is_ring(p.marks)]
        if 0 in self.pane_values:
            # 第一層的詳細程度較粗（多層圓餅的內圈）：以它自己的查詢結果畫
            body = self.with_pane_data(body, self.ws.panes[0])
        if rings and (rows_dims or cols_dims):
            self.notes.append("分面的多層圓餅只畫出第一層（不影響數字）")
            rings = []
        if rings:
            layers = [body]
            for _, pane in rings:
                layers.append(self.with_pane_data(self.pie(pane.marks, rows_dims, cols_dims), pane))
            body = {"layer": layers}
        if not holes:
            return body
        # 洞的大小依洞的標記卡的大小（沒有設定時為外圈的六成）
        hole = holes[0][1].marks
        outer = max(
            (_pie_fraction(p.marks) for p in self.ws.panes if not p.dual or _is_ring(p.marks)),
            default=0.5,
        )
        inner = min(_pie_fraction(hole), outer) if hole.mark_size is not None else outer * 0.6
        for mark in _arc_marks(body):
            mark["innerRadius"] = {"expr": f"{self.pie_extent} * {round(inner / 2, 4)}"}
        facet = body.get("facet")
        keys = [f["field"] for f in facet.values()] if isinstance(facet, dict) else []
        centers: dict[tuple[Any, ...], list[str]] = {}
        for index, pane in holes:
            if pane.marks.labels is not None and pane.marks.labels.show is False:
                continue  # 關閉了標記標籤
            records = self.pane_values.get(index, values)
            fields = (
                [self.label_fields[id(pane.marks)]]
                if id(pane.marks) in self.label_fields
                else [self.ch(use).label for use in pane.marks.label]
            )
            for name in fields:
                for record in records:
                    text = record.get(name)
                    lines = centers.setdefault(tuple(record.get(k) for k in keys), [])
                    if text not in (None, "") and str(text) not in lines:
                        lines.append(str(text))
        if not any(centers.values()):
            return body
        font = self.text_font(hole).mark()
        color = self.text_fill(hole, self.text_color(hole))
        if isinstance(facet, dict):
            # 分面：每一格的文字放在這一格的第一列資料上（分面的各圖層共用資料）
            seen: set[tuple[Any, ...]] = set()
            for record in values:
                key = tuple(record.get(k) for k in keys)
                record[HOLE] = "\n".join(centers.get(key, [])) if key not in seen else ""
                seen.add(key)
            body["spec"]["layer"].append(
                {
                    "transform": [{"filter": f"datum.{HOLE}"}],
                    "mark": {
                        "type": "text",
                        "x": {"expr": "child_width / 2"},
                        "y": {"expr": "child_height / 2"},
                        "lineBreak": "\n",
                        **font,
                        "color": color,
                    },
                    "encoding": {"text": {"field": HOLE}},
                }
            )
            return body
        center = {
            "data": {"values": [{}]},
            "mark": {
                "type": "text",
                "x": {"expr": "width / 2"},
                "y": {"expr": "height / 2"},
                "lineBreak": "\n",
                **font,
                "color": color,
            },
            "encoding": {"text": {"value": "\n".join(centers.get((), []))}},
        }
        return {"layer": [body, center]}

    def _facet(self, uses: list[FieldUse], composite: str) -> dict[str, Any]:
        """分面（圓餅的欄列架維度）：標題的顯示、欄位標籤與文字顏色與座標軸相同。"""
        field_name = composite if len(uses) > 1 else self.ch(uses[0]).label
        facet: dict[str, Any] = {"field": field_name, "type": "ordinal", "sort": None}
        shown, title, color = self.header(uses, "rows" if composite == "rows_dim" else "columns")
        facet["title"] = title
        header: dict[str, Any] = {}
        if not shown:
            header["labels"] = False
        expr = _header_expr(shown, len(uses), False)
        if shown and expr is not None:
            header["labelExpr"] = expr
        if color is not None:
            header["labelColor"] = color
        if header:
            facet["header"] = header
        return facet


def build_spec(
    ir: IR,
    worksheet: str,
    columns: list[OutputColumn],
    result: QueryResult,
    parameters: dict[str, Scalar] | None = None,
    panes: dict[int, tuple[list[OutputColumn], QueryResult]] | None = None,
    totals: list[tuple[TotalQuery, QueryResult]] | None = None,
) -> RenderResult:
    """parameters：查詢時的參數值（參數的參考線用）；省略時為 IR 的預設值。
    panes：詳細程度較粗的標記卡各自的查詢結果（tmig.sqlgen.query.pane_sql），以 ws.panes 的索引為鍵。
    totals：總計的查詢結果（tmig.sqlgen.query.totals_sql），附加在資料最後；去掉的維度顯示為 Grand Total。"""
    ws = next(w for w in ir.worksheets if w.id == worksheet)
    # Data Blending：次要資料源的量值也在查詢結果中
    sources = {ws.datasource, *(b.datasource for b in ws.blends)}
    fields = {f.id: f for d in ir.datasources if d.id in sources for f in d.fields}
    fiscal = fiscal_starts(ir)

    channels: dict[str, Channel] = {}
    for n, column in enumerate(c for c in columns if c.role != "sort" and c.use is not None):
        assert column.use is not None
        data_field = fields[column.use.field]
        # 百分比（分析＞百分比）：量值是占範圍合計的比例，以百分比顯示
        raw_measure = (
            ws.disaggregated
            and column.use.continuous
            and column.use.agg is None
            and data_field.role == "measure"
        )
        percent = (
            ws.percentages is not None
            and (column.role == "measure" or raw_measure)
            and column.datatype in ("integer", "real")
        )
        channels[column.key] = Channel(
            name=f"c{n}",
            label=f"c{n}_label",
            title=caption(data_field, column.use, ws) + ("（合計百分比）" if percent else ""),
            role="measure" if column.role == "measure" else "dimension",
            datatype="real" if percent else column.datatype,
            use=column.use,
            format=PERCENT_DEFAULT if percent else value_format(ws, data_field, column.use),
            aliases=member_aliases(data_field, column.use) if column.role == "dimension" else {},
            is_set=data_field.set is not None,
            fiscal=fiscal.get(data_field.id, 1),
        )
    uses = [*ws.rows, *ws.columns, *ws.marks.uses()]
    uses += [u for pane in ws.panes for u in pane.marks.uses()]
    long = bool(ws.measure_values) and any(u.field in (MEASURE_NAMES, MEASURE_VALUES) for u in uses)
    builder = _Builder(ws, channels)
    builder.extent = _sheet_extent(ir, ws)

    # ---------------------------------------------------------- 資料
    # 顯示遺漏的值：日期與數值分組在最小值到最大值之間補上沒有資料的成員
    result = pad_result(ir, ws, columns, result, parameters, builder.notes)
    values = _records(channels, result, ws.null_text)
    if COLUMN_ORDER in result.columns:
        at = result.columns.index(COLUMN_ORDER)
        for record, row in zip(values, result.rows, strict=True):
            record[ORDER] = row[at]
    for total, total_result in totals or []:
        records = _records(channels, total_result, ws.null_text)
        for record in records:
            for key in total.dropped:
                if key in channels:
                    record[channels[key].name] = None
                    record[channels[key].label] = total.label(key)
            record[TOTAL] = True
        # 欄總計在上方：列架的總計與小計放在各組之前（表頭依資料的順序）；欄架的見 _column_order
        # （列與欄都有離散維度時；只有欄維度時表頭也依資料的順序，列總計在左側即放在前面）
        first = (ws.totals_top and "rows" in total.shelves) or (
            ws.totals_left and total.shelves == ("columns",) and COLUMN_ORDER not in result.columns
        )
        if total.group is None:
            values = records + values if first else values + records
        else:
            keys = [channels[k].label for k in total.group if k in channels]
            place = before_groups if first else after_groups
            values = place(values, records, lambda r, keys=keys: tuple(r.get(k) for k in keys))
    pane_values = {
        i: _records(channels, pane_result, ws.null_text)
        for i, (_, pane_result) in (panes or {}).items()
    }
    if long and pane_values:
        builder.notes.append("用到 Measure Values 時，各標記卡一律以工作表的詳細程度呈現")
        pane_values = {}
    if totals and pane_values:
        # 總計只依工作表的詳細程度查詢：較粗的標記卡另外查詢的資料中沒有總計
        builder.notes.append("有總計時，各標記卡一律以工作表的詳細程度呈現")
        pane_values = {}
    builder.pane_values = pane_values
    if long:
        names = {use_key(a.use): a.label for a in ws.measure_aliases}
        builder.measure_names = {
            use_key(u): names.get(use_key(u), builder.ch(u).title) for u in ws.measure_values
        }
        values = _fold(values, [builder.ch(u) for u in ws.measure_values], names)
        channels[MEASURE_NAMES] = Channel(
            "__mn",
            "__mn",
            MEASURE_NAMES_TITLE,
            "dimension",
            "string",
            FieldUse(field=MEASURE_NAMES),
        )
        channels[MEASURE_VALUES] = Channel(
            "__mv",
            "__mv_label",
            MEASURE_VALUES_TITLE,
            "measure",
            "real",
            FieldUse(field=MEASURE_VALUES),
        )

    params = {p.id: p.value for p in ir.parameters} | (parameters or {})
    cards = [(None, ws.marks), *enumerate(p.marks for p in ws.panes)]

    def field_style(use: FieldUse) -> TextStyle | None:
        """欄位在標記上的字型與顏色（Format > 欄位的窗格字型，例如 KPI 的大數字）。"""
        font, color = ws.field_fonts.get(use_key(use)), ws.field_colors.get(use_key(use))
        if font is None and color is None:
            return None
        return TextStyle(
            size=font.size if font else None,
            bold=font.bold if font else None,
            italic=font.italic if font else None,
            font=font.family if font else None,
            color=color,
        )

    for n, (index, marks) in enumerate(cards):
        labelled = [channels[use_key(u)] for u in marks.label if use_key(u) in channels]
        if not marks.label_text and len(marks.label) == 1:
            style = field_style(marks.label[0])
            if style is not None:
                builder.text_styles[id(marks)] = style
        if not marks.label_text and len(labelled) < 2:
            continue
        name = f"__text{n}"
        builder.label_fields[id(marks)] = name
        lines = _text_lines(marks.label_text, field_style)
        styles = [style for _, style in lines]
        first = next((style for style in styles if style is not None), None)
        if first is not None:
            builder.text_styles[id(marks)] = first
        if len(lines) > 1 and any(style != styles[0] for style in styles):
            # 各行的格式不同（例如 KPI 的小標題與大數字）：每一行另外組成一欄，分行畫出
            builder.text_lines[id(marks)] = [
                (f"{name}_{i}", style) for i, (_, style) in enumerate(lines)
            ]
        targets = [values]
        if index is not None and index in pane_values:
            targets = [pane_values[index]]
        for records in targets:
            for record in records:
                if marks.label_text:
                    record[name] = compose_text(marks.label_text, ir, params, channels, record)
                    if id(marks) in builder.text_lines:
                        for i, (runs, _) in enumerate(lines):
                            record[f"{name}_{i}"] = compose_text(runs, ir, params, channels, record)
                else:
                    # 沒有自訂的標籤文字：標籤上的欄位各占一行（與 Tableau 的預設相同）
                    record[name] = "\n".join(
                        "" if record.get(ch.label) is None else str(record.get(ch.label))
                        for ch in labelled
                    )

    # Measure Names 沒有對應的量值（Measure Values 卡是空的）時不畫：Tableau 只顯示空白的欄標題
    builder.rows = [u for u in ws.rows if builder.shown(u)]
    builder.columns = [u for u in ws.columns if builder.shown(u)]
    if any(not builder.shown(u) and u.field == MEASURE_NAMES for u in [*ws.rows, *ws.columns]):
        builder.notes.append("Measure Names 沒有對應的量值（Measure Values 卡是空的），不顯示")
    # 欄列架上離散（藍色）的量值是標題、不是座標軸（例如以量值的值排成表格、INDEX() 的排名）
    rows_dims = [u for u in builder.rows if not builder.is_axis(u)]
    rows_meas = [u for u in builder.rows if builder.is_axis(u)]
    cols_dims = [u for u in builder.columns if not builder.is_axis(u)]
    cols_meas = [u for u in builder.columns if builder.is_axis(u)]
    composites: dict[str, list[FieldUse]] = {}
    if len(rows_dims) > 1:
        composites["rows_dim"] = rows_dims
    if len(cols_dims) > 1:
        composites["cols_dim"] = cols_dims
    pie_marks = _pie_marks(ws)
    map_plan: maps.MapPlan | None = None
    if maps.is_map(ws, fields):
        # 地圖：查詢之後找出各標記的位置（篩選掉未知的位置時在列號之前）
        map_plan, values, pane_values = maps.locate(ir, ws, channels, values, pane_values)
        builder.pane_values = pane_values
    for i, record in enumerate(values):
        # 列號：檢視器選取標記時，以它找回未經 Vega 解析的原始值；圓餅各塊也依它排列；總計不參與選取
        record[ROW] = -1 if record.get(TOTAL) else i
        for name, members in composites.items():
            labels = [record[builder.ch(u).label] for u in members]
            # 去掉的維度在架的後段，標題只寫一次：「East｜Total」、整列的「Grand Total」
            dropped = [_dropped(record, builder.ch(u)) for u in members]
            while len(labels) > 1 and dropped[-1] and dropped[-2] and labels[-1] == labels[-2]:
                labels.pop()
                dropped.pop()
            record[name] = SEPARATOR.join(labels)
    for records in pane_values.values():
        for record in records:
            record[ROW] = -1  # 較粗的標記卡不參與選取
            for name, members in composites.items():
                record[name] = SEPARATOR.join(
                    str(record.get(builder.ch(u).label, "")) for u in members
                )

    builder.values = values
    mark = ws.marks.type
    multiple = len(rows_meas) > 1 or len(cols_meas) > 1
    if map_plan is not None:
        body = maps.body(builder, ir, ws, map_plan, rows_dims, cols_dims, params)
        mark = "map"
        for name, present in (
            ("趨勢線", any(m.trend for m in [ws.marks, *(p.marks for p in ws.panes)])),
            ("預測", ws.forecast),
            ("註解", ws.annotations),
        ):
            if present:
                builder.notes.append(f"地圖上的{name}尚未支援，未畫出（不影響數字）")
    elif pie_marks is not None:
        body = builder.pie(pie_marks, rows_dims, cols_dims)
        if len(ws.panes) > 1:
            body = builder.donut(body, values, rows_dims, cols_dims)
        mark = "pie"
    elif multiple:
        body, mark = builder.multi(rows_dims, cols_dims, rows_meas, cols_meas)
    else:
        if mark == "automatic":
            mark = _infer_mark(
                rows_dims,
                cols_dims,
                rows_meas,
                cols_meas,
                builder.is_continuous_date,
                sized=ws.marks.size is not None,
            )
        if mark in UNSUPPORTED_MARKS:
            return RenderResult(None, len(result.rows), mark, f"標記類型 {mark} 尚未支援")
        body, mark = builder.unit(ws.marks, mark, rows_dims, cols_dims, rows_meas, cols_meas)

    # 欄標題依查詢算出的名次排列（列與欄都有離散維度時）：資料依列優先排序，只看資料的順序時
    # 第一列沒有的欄會排到後面
    column_field: str | None = None
    column_order: list[Any] = []
    if cols_dims and any(ORDER in v for v in values):
        column_field = "cols_dim" if len(cols_dims) > 1 else builder.ch(cols_dims[0]).label
        column_order = _column_order(
            values, column_field, [builder.ch(u) for u in cols_dims], ws.totals_left
        )
    for channel in ("row", "column"):
        # 分面依資料的順序（查詢已依排序設定排好）；Vega-Lite 的分面不指定時會依字母排序
        facet = (body.get("facet") or body.get("encoding", {})).get(channel)
        if isinstance(facet, dict) and facet.get("sort") is None:
            if channel == "column" and facet.get("field") == column_field:
                facet["sort"] = column_order
            else:
                facet["sort"] = list(dict.fromkeys(v.get(facet["field"]) for v in values))

    if ws.reference_lines and map_plan is None:
        params = {p.id: p.value for p in ir.parameters} | (parameters or {})
        body = _with_reference_lines(builder, body, values, params)
    trend = next((m.trend for m in [ws.marks, *(p.marks for p in ws.panes)] if m.trend), None)
    if map_plan is not None:
        pass
    elif trend is not None and mark == "pie":
        builder.notes.append("圓餅沒有座標軸，趨勢線未畫出")
    elif trend is not None and not multiple:
        # 多個量值時每張圖各自畫（見 _Builder.multi）
        _trend_lines(builder, body, trend, builder.records_of(ws.marks))

    if ws.cluster is not None:
        builder.notes.append(
            "叢集以平台的 k-means 計算（與 Tableau 相同的做法），分群的結果與編號可能與 Tableau 不同"
        )
    if ws.forecast is not None:
        builder.notes.append(
            "預測的估計值以平台的指數平滑計算（與 Tableau 相同的模型），可能與 Tableau 有些微差異"
        )
        if ws.forecast.bands and map_plan is None:
            if pie_marks is None and not multiple and ("mark" in body or "layer" in body):
                body = _with_forecast_bands(builder, body, values, result)
            else:
                builder.notes.append("多個量值、圓餅等圖表上的預測區間尚未畫出（不影響數字）")

    if ws.annotations and map_plan is None:
        if pie_marks is None and not multiple and ("mark" in body or "layer" in body):
            params = {p.id: p.value for p in ir.parameters} | (parameters or {})
            body = _with_annotations(builder, body, values, ir, params)
        else:
            builder.notes.append("多個量值、圓餅等圖表上的註解尚未畫出（不影響數字）")

    if (
        map_plan is None
        and pie_marks is None
        and not multiple
        and ("mark" in body or "layer" in body)
    ):
        body = _small_multiples(builder, body, values, rows_dims, cols_dims, rows_meas, cols_meas)

    tooltip = _tooltip_template(
        ws, ir, channels, {p.id: p.value for p in ir.parameters} | (parameters or {})
    )
    # 列數不含總計（與工作表的查詢、計算比對的筆數相同）
    marks_count = sum(1 for v in values if not v.get(TOTAL))
    spec: dict[str, Any] = {
        "$schema": SCHEMA,
        "usermeta": {
            "worksheet": ws.id,
            "name": ws.name,
            "rows": marks_count,
            "mark": mark,
            # 篩選與網址動作：標記的維度欄與量值欄（資料欄名 → 欄位鍵）
            "dimensions": {
                ch.name: key
                for key, ch in channels.items()
                if ch.role == "dimension" and ch.use.field not in SPECIAL_FIELDS
            },
            "measures": {
                ch.name: key
                for key, ch in channels.items()
                if ch.role == "measure" and ch.use.field not in SPECIAL_FIELDS
            },
        },
        "data": {"values": values},
        "width": "container",
        "height": "container",
        "autosize": {"type": "fit", "contains": "padding"},
        "config": _config(ws),
    }
    if ws.background is not None:
        spec["background"] = _css_color(ws.background)
        spec["usermeta"]["background"] = spec["background"]
    if ws.text_color is not None:
        spec["usermeta"]["text_color"] = _css_color(ws.text_color)
    if tooltip is not None:
        spec["usermeta"]["tooltip"] = tooltip
    title_color = next((r.style.color for r in ws.title if r.style and r.style.color), None)
    spec["usermeta"]["title_color"] = title_color or _title_color(ws)
    spec["usermeta"]["title_font"] = _title_font(ws)
    if ws.title_background is not None:
        spec["usermeta"]["title_background"] = _css_color(ws.title_background)
    if "vconcat" in body or "hconcat" in body or "facet" in body:
        # 多張圖（分面）無法自動填滿容器，由檢視器依區塊大小設定每張圖的寬高
        del spec["width"], spec["height"]
        spec["autosize"] = {"type": "pad"}
    spec.update(body)
    if mark == "text" and not (rows_dims or cols_dims or rows_meas or cols_meas):
        # 單格的文字（KPI）：與 Tableau 相同不留內距，小區塊中的大數字不會被裁掉
        spec["padding"] = 0
    if builder.culled:
        spec["params"] = [*spec.get("params", []), {"name": LABEL_HIDDEN, "value": []}]
    if column_field is not None:
        _sort_headers(spec, column_field, column_order)
    for record in values:
        record.pop(ORDER, None)
    if totals and not ws.totals_colored:
        _exclude_totals_from_color(spec, mark, builder.text_color(ws.marks, cell=True))
    encoding = main_encoding(spec)
    sized = (
        map_plan is None
        and encoding is not None
        and not any(k in spec for k in ("vconcat", "hconcat", "facet"))
        and not any(k in encoding for k in ("row", "column"))
    )
    if sized and encoding is not None:
        text = encoding.get("text")
        texts = (
            [str(v.get(text["field"], "")) for v in values]
            if isinstance(text, dict) and "field" in text
            else []
        )
        spec["usermeta"]["sizing"] = {
            "rows": builder.shelf_size(encoding.get("y"), rows_dims, "rows", values, []),
            "columns": builder.shelf_size(encoding.get("x"), cols_dims, "columns", values, texts),
        }
        y_type = encoding.get("y", {}).get("type") if isinstance(encoding.get("y"), dict) else None
        sizing: dict[str, dict[str, Any]] = spec["usermeta"]["sizing"]
        # 只有離散欄位的圓點與形狀（每格一個標記）與文字表相同排成表格
        dots = mark in ("circle", "shape") and not rows_meas and not cols_meas
        if dots:
            label = builder.label_field(ws.marks)
            labels = [str(v.get(label, "")) for v in values] if label is not None else []
            width = POINT_DIAMETER + 16 + (_column_width(labels) if labels else 0)
        else:
            width = _column_width(texts)
        if (mark == "text" or dots) and "x" not in encoding and y_type in ("ordinal", "nominal"):
            # 只有列標題的文字表是一欄：寬度依內容（與離散的欄相同），不填滿
            sizing["columns"] = {"size": ws.column_style.cell or width}
        elif (mark == "text" or dots) and "step" in sizing["columns"]:
            # 文字表的欄與 Tableau 相同依內容的寬度，標準大小時不拉寬填滿區塊
            sizing["columns"]["fixed"] = True
        shelves: list[tuple[Literal["rows", "columns"], str, list[FieldUse], ShelfStyle]] = [
            ("rows", "y", rows_dims, ws.row_style),
            ("columns", "x", cols_dims, ws.column_style),
        ]
        x = encoding.get("x")
        if (
            isinstance(x, dict)
            and x.get("field") == "cols_dim"
            and len(cols_dims) > 1
            and x.get("type") in ("ordinal", "nominal")
        ):
            _column_headers(builder, spec, x, cols_dims, values)
        headers: RowHeaders | None = None
        y = encoding.get("y")
        if isinstance(y, dict) and y.get("type") in ("ordinal", "nominal"):
            if y.get("field") == "rows_dim" and len(rows_dims) > 1:
                headers = _row_headers(builder, spec, y, rows_dims, values)
            elif len(rows_dims) == 1 and y.get("field") == builder.ch(rows_dims[0]).label:
                headers = _row_header_axis(builder, y, rows_dims[0], values)
        if headers is not None and ws.row_style.field_labels and ws.field_label_background:
            _field_label_background(spec, headers, ws.field_label_background)
        for shelf, channel, dims, style in shelves:
            enc = encoding.get(channel)
            if not isinstance(enc, dict) or enc.get("type") not in ("ordinal", "nominal"):
                continue
            if style.divider is not None:
                _divider(spec, enc, channel, values, len(dims), style.divider)
            if shelf == "rows" and headers is not None and style.header_divider is not None:
                _header_divider(spec, enc, values, len(dims), style.header_divider, headers)
            shown = len(builder.header(dims, "columns")[0]) if shelf == "columns" and dims else 0
            if shelf == "columns" and style.header_divider is not None and shown:
                _column_header_divider(
                    spec,
                    enc,
                    values,
                    len(dims),
                    style.header_divider,
                    shown * HEADER_LINE_HEIGHT + 4,
                )
            if style.banding is not None and mark not in ("square", "pie"):
                extent = (
                    headers.total
                    if shelf == "rows" and headers is not None
                    else (builder.header_extent(dims, shelf) if dims else None)
                )
                _banding(spec, enc, channel, values, style.banding, extent, len(dims))
    spec["usermeta"]["fit"] = ws.fit
    apply_fit(spec, ws.fit)
    return RenderResult(spec, marks_count, mark, None, builder.notes)


def apply_fit(spec: dict[str, Any], fit: str) -> None:
    """依顯示大小設定圖的寬高（usermeta.sizing 的各方向大小）：fit_width／entire_view 時寬度填滿，
    fit_height／entire_view 時高度填滿；否則離散的列固定每一列的高度、設定了大小的欄固定每一欄的
    寬度、設定了大小的連續座標軸固定大小。檢視器以同樣的規則依區塊的設定重算（離散的欄另外在
    區塊放不下時固定寬度、可以捲動）。"""
    sizing = spec.get("usermeta", {}).get("sizing")
    if sizing is None:
        return
    rows, columns = sizing.get("rows", {}), sizing.get("columns", {})
    width: Any = "container"
    height: Any = "container"
    if fit not in ("fit_height", "entire_view"):
        if "step" in rows:
            height = {"step": rows["step"]}
        elif "size" in rows:
            height = rows["size"]
    if fit not in ("fit_width", "entire_view"):
        if "size" in columns:
            width = columns["size"]
        elif "step" in columns and columns.get("fixed"):
            width = {"step": columns["step"]}
    spec["width"], spec["height"] = width, height
    fill_x, fill_y = width == "container", height == "container"
    kind = "fit" if fill_x and fill_y else "fit-x" if fill_x else "fit-y" if fill_y else "pad"
    spec["autosize"] = {"type": kind, "contains": "padding"}


HEADER_WIDTH = (32, 200)
"""列標題依內容估計寬度時的最小與最大值（像素）；Tableau 也會截斷過長的標題。"""
HEADER_PADDING = 4
"""列標題文字與欄左緣的距離（像素）。"""
CELL_PADDING = 4
"""文字表中靠左或靠右的文字與儲存格邊緣的距離（像素）。"""
HEADER = "__header"
"""多層列標題的資料欄（__header0、__header1…）與圖層名稱前綴。"""
DIVIDER = "__divider_"
"""標題區域的分隔線的資料欄（線的左端，像素）。"""


@dataclass
class RowHeaders:
    """多層列標題的位置：總寬度與顯示的各層所在欄的左緣（相對於窗格的左緣，為負數）。"""

    total: int
    starts: dict[int, int]


def _header_width(
    builder: _Builder, use: FieldUse, texts: list[str], angle: int | None = None
) -> int:
    """一層列標題的欄寬：拖曳調整過的依設定，否則依成員（與欄位標籤）的內容估計，限制在
    HEADER_WIDTH 之間（較長的成員截斷）；直寫時為一行字的寬度。"""
    setting = builder.axis_of(use, "rows")
    if setting is not None and setting.size is not None:
        return setting.size
    if angle is not None:
        return round(builder.label_font(use, "rows").size * 1.6) + 2 * HEADER_PADDING
    if builder.ws.row_style.field_labels:
        texts = [*texts, builder.ch(use).title]
    low, high = HEADER_WIDTH
    size = builder.label_font(use, "rows").size
    return min(max(_column_width(texts, size), low), high)


def _header_valign(builder: _Builder, use: FieldUse) -> Literal["top", "center", "bottom"]:
    """外層的標題成員在各組中的垂直位置：這個欄位的設定、工作表的設定，沒有時為上（與 Tableau 相同）。"""
    setting = builder.axis_of(use, "rows")
    return (setting.valign if setting is not None else None) or builder.ws.label_valign or "top"


def _header_angle(builder: _Builder, use: FieldUse) -> int | None:
    """標題成員的文字方向（度）；橫寫時為 None。"""
    setting = builder.axis_of(use, "rows")
    angle = setting.angle if setting is not None else None
    return angle if angle is not None and angle % 180 != 0 else None


def _groups(order: list[Any], parts: dict[Any, list[str]], level: int) -> list[list[Any]]:
    """依前 level + 1 層的成員把相鄰的列分組（多層列標題中這一層的各組）。"""
    groups: list[list[Any]] = []
    previous: list[str] | None = None
    for value in order:
        here = parts[value][: level + 1]
        if previous is None or here != previous:
            groups.append([])
        groups[-1].append(value)
        previous = here
    return groups


def _header_align(builder: _Builder, use: FieldUse) -> Literal["left", "center", "right"]:
    setting = builder.axis_of(use, "rows")
    return (setting.align if setting is not None else None) or "left"


def _row_header_axis(
    builder: _Builder, encoding: dict[str, Any], use: FieldUse, values: list[dict[str, Any]]
) -> RowHeaders | None:
    """單層的列標題：與 Tableau 相同畫成一欄（寬度依設定或內容），成員靠左（或依設定的對齊）、不畫
    刻度，欄位標籤橫寫在欄的上方。以縱軸本身的標籤與標題畫出。不顯示標題時為 None。"""
    shown, title, _ = builder.header([use], "rows")
    if not shown:
        return None
    texts = [str(v.get(encoding["field"]) or "") for v in values]
    angle = _header_angle(builder, use)
    width = _header_width(builder, use, texts, angle)
    align = "center" if angle is not None else _header_align(builder, use)
    label_font = builder.label_font(use, "rows")
    padding = {"left": width - HEADER_PADDING, "center": width / 2, "right": HEADER_PADDING}
    axis = dict(encoding.get("axis") or {})
    axis.update(
        zindex=1,  # 畫在色帶與欄位標籤的背景之上
        **label_font.axis("label"),
        ticks=False,
        labelAlign=align,
        labelPadding=padding[align],
        labelLimit=max(width - 2 * HEADER_PADDING, 8),
        minExtent=width,
        maxExtent=width,
    )
    if angle is not None:
        axis.update(labelAngle=angle, labelBaseline="middle", labelLimit=0)
    if title is None:
        axis["title"] = None
    else:
        axis.update(
            title=title,
            titleAngle=0,
            titleAlign="left",
            titleBaseline="bottom",
            titleX=-width + HEADER_PADDING,
            titleY=-4,
            **builder.font("field-labels", weight=SEMIBOLD).axis("title"),
            titleLimit=max(width - 2 * HEADER_PADDING, 8),
        )
    encoding["axis"] = axis
    return RowHeaders(width, {0: -width})


def _row_headers(
    builder: _Builder,
    spec: dict[str, Any],
    encoding: dict[str, Any],
    dims: list[FieldUse],
    values: list[dict[str, Any]],
) -> RowHeaders | None:
    """多層的列標題：每一層一欄（設定了寬度時依設定，否則依內容估計），外層的成員只寫在各組的
    第一列；有欄位標籤時寫在各欄上方。標題畫在縱軸的區域中（縱軸只留下座標軸線）。所有層都不
    顯示時為 None。"""
    shown, _, _ = builder.header(dims, "rows")
    if not shown:
        return None
    style = builder.ws.row_style
    order = list(dict.fromkeys(v.get("rows_dim") for v in values))
    parts = {value: str(value if value is not None else "").split(SEPARATOR) for value in order}

    def part(value: Any, level: int) -> str:
        found = parts[value]
        return found[level] if level < len(found) else ""

    # 外層的成員寫在各組的哪一列：上（第一列，預設）、中（奇數列的組在中間那一列；偶數列的組在中間兩列
    # 之間，另一個圖層畫在那一列的尾端）、下（最後一列）
    placed: dict[tuple[Any, int], str] = {}
    between: dict[tuple[Any, int], str] = {}
    innermost = len(dims) - 1
    for level in shown:
        valign = _header_valign(builder, dims[level]) if level < innermost else "top"
        for group in _groups(order, parts, level):
            text = part(group[0], level)
            if valign == "bottom":
                placed[(group[-1], level)] = text
            elif valign == "center" and len(group) % 2 == 0:
                between[(group[len(group) // 2 - 1], level)] = text
            elif valign == "center":
                placed[(group[len(group) // 2], level)] = text
            else:
                placed[(group[0], level)] = text
    angles = {level: _header_angle(builder, dims[level]) for level in shown}
    widths = [
        _header_width(builder, dims[level], [part(v, level) for v in order], angles[level])
        for level in shown
    ]
    total = sum(widths)
    # 交叉表中每一列有多筆資料（每一欄一筆）：標題只寫在其中一筆，避免同一個字重疊畫好幾次
    seen: set[tuple[Any, int]] = set()
    for record in values:
        value = record.get("rows_dim")
        for level in shown:
            key = (value, level)
            first = key not in seen
            seen.add(key)
            record[f"{HEADER}{level}"] = placed.get(key, "") if first else ""
            if between:
                record[f"{HEADER}{level}_mid"] = between.get(key, "") if first else ""
    if between:
        # 中間兩列之間：列的高度以整列計算（band），文字畫在上面那一列的尾端
        scale = dict(encoding.get("scale") or {})
        scale.update(type="band", paddingInner=0, paddingOuter=0)
        encoding["scale"] = scale

    position = {k: encoding[k] for k in ("field", "type", "sort", "scale") if k in encoding}
    layers: list[dict[str, Any]] = []
    starts: dict[int, int] = {}
    offset = -total
    for level, width in zip(shown, widths, strict=True):
        starts[level] = offset
        setting = builder.axis_of(dims[level], "rows")
        color = (
            _css_color(setting.label_color)
            if setting is not None and setting.label_color is not None
            else _label_color(builder.ws)
        )
        align = _header_align(builder, dims[level])
        angle = angles[level]
        mark: dict[str, Any] = {
            "type": "text",
            "align": align,
            "baseline": "middle",
            **builder.label_font(dims[level], "rows").mark(),
            "limit": max(width - 2 * HEADER_PADDING, 8),
            "color": color,
        }
        if angle is not None:
            # 直寫：以欄的中間為軸旋轉（Tableau 的 -90 為由下往上）
            mark.update(align="center", angle=angle % 360, limit=0)
            align = "center"
        x = {
            "left": offset + HEADER_PADDING,
            "center": offset + width / 2,
            "right": offset + width - HEADER_PADDING,
        }[align]
        layers.append(
            {
                "name": f"{DECO}{HEADER}{level}",
                "transform": [{"calculate": "-1", "as": ROW}],
                "mark": mark,
                "encoding": {
                    "y": position,
                    "x": {"value": x},
                    "text": {"field": f"{HEADER}{level}"},
                },
            }
        )
        if any(key[1] == level for key in between):
            layers.append(
                {
                    "name": f"{DECO}{HEADER}{level}_mid",
                    "transform": [{"calculate": "-1", "as": ROW}],
                    "mark": mark,
                    "encoding": {
                        "y": {**position, "scale": encoding["scale"], "bandPosition": 1},
                        "x": {"value": x},
                        "text": {"field": f"{HEADER}{level}_mid"},
                    },
                }
            )
        if style.field_labels:
            layers.append(
                {
                    "name": f"{DECO}{HEADER}{level}_title",
                    "data": {"values": [{}]},
                    "mark": {
                        "type": "text",
                        "align": "left",
                        "baseline": "bottom",
                        **builder.font("field-labels", weight=SEMIBOLD).mark(),
                        "limit": max(width - 2 * HEADER_PADDING, 8),
                        "color": _title_color(builder.ws),
                        "x": offset + HEADER_PADDING,
                        "y": -4,
                    },
                    "encoding": {"text": {"value": builder.ch(dims[level]).title}},
                }
            )
        offset += width
    axis = dict(encoding.get("axis") or {})
    axis.update(labels=False, ticks=False, title=None, minExtent=total, maxExtent=total)
    axis.pop("labelExpr", None)
    axis.pop("labelLimit", None)
    encoding["axis"] = axis
    _add_layers(spec, layers)
    return RowHeaders(total, starts)


COLUMN_HEADER = "__colheader"
"""多層欄標題的資料欄（__colheader0、__colheader1…）與圖層名稱前綴。"""


def _column_headers(
    builder: _Builder,
    spec: dict[str, Any],
    encoding: dict[str, Any],
    dims: list[FieldUse],
    values: list[dict[str, Any]],
) -> None:
    """多層的欄標題：每一層一行（文字表在上方、其他圖在下方，外層離圖最遠），外層的成員與 Tableau 相同
    合併成一格、寫在各組的中間（偶數欄的組寫在中間兩欄之間）。座標軸只留下欄位標籤。"""
    shown, _, _ = builder.header(dims, "columns")
    if not shown:
        return
    order = list(dict.fromkeys(v.get("cols_dim") for v in values))
    parts = {value: _levels(value, len(dims)) for value in order}
    placed: dict[tuple[Any, int], str] = {}
    between: dict[tuple[Any, int], str] = {}
    for level in shown:
        for group in _groups(order, parts, level):
            text = parts[group[0]][level] if level < len(parts[group[0]]) else ""
            if len(group) % 2 == 0:
                between[(group[len(group) // 2 - 1], level)] = text
            else:
                placed[(group[len(group) // 2], level)] = text
    seen: set[tuple[Any, int]] = set()
    for record in values:
        value = record.get("cols_dim")
        for level in shown:
            key = (value, level)
            first = key not in seen
            seen.add(key)
            record[f"{COLUMN_HEADER}{level}"] = placed.get(key, "") if first else ""
            record[f"{COLUMN_HEADER}{level}_mid"] = between.get(key, "") if first else ""
    scale = dict(encoding.get("scale") or {})
    scale.update(type="band", paddingInner=0, paddingOuter=0)
    encoding["scale"] = scale
    axis = dict(encoding.get("axis") or {})
    top = axis.get("orient") == "top"
    position = {k: encoding[k] for k in ("field", "type", "sort", "scale") if k in encoding}
    color = _label_color(builder.ws)
    layers: list[dict[str, Any]] = []
    count = len(shown)
    for index, level in enumerate(shown):
        # 離圖最近的是最內層：上方時往上排，下方時往下排
        distance = 4 + (count - 1 - index) * HEADER_LINE_HEIGHT
        setting = builder.axis_of(dims[level], "columns")
        mark = {
            "type": "text",
            "y": -distance if top else {"expr": f"height + {distance}"},
            "align": "center",
            "baseline": "bottom" if top else "top",
            **builder.label_font(dims[level], "columns").mark(),
            "color": _css_color(setting.label_color)
            if setting is not None and setting.label_color is not None
            else color,
        }
        for suffix, band in (("", 0.5), ("_mid", 1)):
            layers.append(
                {
                    "name": f"{DECO}{COLUMN_HEADER}{level}{suffix}",
                    "transform": [{"calculate": "-1", "as": ROW}],
                    "mark": mark,
                    "encoding": {
                        "x": {**position, "bandPosition": band},
                        "text": {"field": f"{COLUMN_HEADER}{level}{suffix}"},
                    },
                }
            )
    extent = count * HEADER_LINE_HEIGHT + 4
    axis.update(labels=False, minExtent=extent, maxExtent=extent)
    axis.pop("labelExpr", None)
    axis.pop("labelPadding", None)
    axis.pop("labelLineHeight", None)
    if not axis.get("tickOpacity"):
        axis["ticks"] = False
    encoding["axis"] = axis
    _add_layers(spec, layers)


FIELD_LABEL_HEIGHT = 20
"""欄位標籤那一列的高度（像素，與標準的列高相同）。"""


def _field_label_background(spec: dict[str, Any], headers: RowHeaders, color: str) -> None:
    """欄位標籤的背景：列標題上方一列、橫跨各層標題的欄，畫在最下層。"""
    layer = {
        "name": f"{DECO}_field_labels",
        "data": {"values": [{}]},
        "mark": {
            "type": "rect",
            "color": _css_color(color),
            "x": -headers.total,
            "x2": 0,
            "y": -FIELD_LABEL_HEIGHT,
            "y2": 0,
        },
    }
    body = {
        k: spec.pop(k) for k in ("mark", "encoding", "layer", "transform", "resolve") if k in spec
    }
    spec["layer"] = [layer, body]


def _add_layers(spec: dict[str, Any], layers: list[dict[str, Any]]) -> None:
    """在圖上加上裝飾的圖層（畫在主圖之上）。"""
    if "layer" in spec and "encoding" not in spec and "resolve" not in spec:
        spec["layer"] = [*spec["layer"], *layers]  # 例如參考線已把主圖包成圖層
    else:
        # 共用的 encoding（顏色、大小等）不套用到裝飾的圖層
        body = {
            k: spec.pop(k)
            for k in ("mark", "encoding", "layer", "transform", "resolve")
            if k in spec
        }
        spec["layer"] = [body, *layers]


def main_encoding(node: dict[str, Any]) -> dict[str, Any] | None:
    """圖的主要 encoding：本身或第一個有 x、y 的圖層（甜甜圈中間的文字等圖層在後；參考線、色帶與
    多層列標題的圖層除外）。"""
    encoding = node.get("encoding")
    if isinstance(encoding, dict) and ("x" in encoding or "y" in encoding):
        return encoding
    for child in node.get("layer", []):
        if str(child.get("name", "")).startswith((REF, DECO)):
            continue
        found = main_encoding(child)
        if found is not None:
            return found
    return encoding if isinstance(encoding, dict) else None


def _channel_field(node: dict[str, Any], channel: str) -> str | None:
    """圖的主要 encoding 上某個通道的欄位（沒有或是固定值時為 None）。"""
    encoding = main_encoding(node)
    found = encoding.get(channel) if isinstance(encoding, dict) else None
    if not isinstance(found, dict) and isinstance(node.get("encoding"), dict):
        found = node["encoding"].get(channel)
    return found.get("field") if isinstance(found, dict) else None


def _column_width(texts: Sequence[Any], size: float = 12) -> int:
    """離散的欄依內容估計的寬度：最長的標題或儲存格文字（全形字約一個字高、其餘約 0.55 個字高，size 為
    字級的像素）加上左右的空白，限制在 COLUMN_WIDTH 之間。"""
    widest = 0.0
    wide, narrow = size, size * 0.55
    for text in texts:
        for line in re.split(f"{SEPARATOR}|\n", str(text if text is not None else "")):
            width = sum(
                wide if unicodedata.east_asian_width(c) in ("W", "F") else narrow for c in line
            )
            widest = max(widest, width)
    low, high = COLUMN_WIDTH
    return int(min(max(round(widest + 16), low), high))


def _band_edges(values: list[dict[str, Any]], field: str, levels: int, level: int) -> list[Any]:
    """分隔線：各列（欄）的值中，下一列（欄）的前 level 層與它不同者（線畫在它的尾端）。"""
    order = list(dict.fromkeys(v.get(field) for v in values))
    depth = max(levels, 1)

    def group(value: Any) -> tuple[str, ...]:
        parts = str(value).split(SEPARATOR) if depth > 1 else [str(value)]
        return tuple(parts[: min(level, depth)])

    return [a for a, b in itertools.pairwise(order) if group(a) != group(b)]


def _divider_level(divider: Divider, levels: int) -> int:
    """分隔線分隔的層數：設定的層數，沒有設定時為最內層以外的每一層（Tableau 的預設）。"""
    return divider.level if divider.level is not None else levels - 1


def _divider(
    spec: dict[str, Any],
    encoding: dict[str, Any],
    channel: str,
    values: list[dict[str, Any]],
    levels: int,
    divider: Divider,
) -> None:
    """窗格的分隔線：以座標軸的格線畫在各列（欄）的邊界（tickBand extent），只顯示前 level 層的成員
    改變處。"""
    level = _divider_level(divider, levels)
    edges = _band_edges(values, encoding["field"], levels, level) if level >= 1 else []
    if not edges:
        return
    axis = dict(encoding.get("axis") or {})
    test = f"indexof({json.dumps(edges, ensure_ascii=False)}, datum.value) >= 0"
    axis.update(
        grid=True,
        tickBand="extent",
        gridColor=_css_color(divider.color),
        gridWidth=divider.width,
        gridOpacity={"condition": {"test": test, "value": 1}, "value": 0},
    )
    encoding["axis"] = axis
    _flush_bands(spec, encoding, channel)


def _header_divider(
    spec: dict[str, Any],
    encoding: dict[str, Any],
    values: list[dict[str, Any]],
    levels: int,
    divider: Divider,
    headers: RowHeaders,
) -> None:
    """標題區域的分隔線（多層的列標題）：畫在各組的邊界，從改變的那一層所在的欄畫到窗格（外層沒有
    改變的欄不切開，與 Tableau 相同）。"""
    level = _divider_level(divider, levels)
    field_name = encoding["field"]
    edges = set(_band_edges(values, field_name, levels, level)) if level >= 1 else set()
    order = list(dict.fromkeys(v.get(field_name) for v in values))
    starts: dict[Any, int] = {}
    for a, b in itertools.pairwise(order):
        if a not in edges:
            continue
        here, after = _levels(a, levels), _levels(b, levels)
        changed = next((i for i, (x, y) in enumerate(zip(here, after, strict=False)) if x != y), 0)
        start = next((x for lv, x in sorted(headers.starts.items()) if lv >= changed), None)
        if start is not None:
            starts[a] = start
    if not starts:
        return
    flag = f"{DIVIDER}rows"
    for record in values:
        record[flag] = starts.get(record.get(field_name), 0)
    _flush_bands(spec, encoding, "y")
    position = {k: encoding[k] for k in ("field", "type", "sort", "scale") if k in encoding}
    layer = {
        "name": f"{DECO}{flag}_header",
        "transform": [{"calculate": "-1", "as": ROW}],
        "mark": {"type": "rule", "color": _css_color(divider.color), "strokeWidth": divider.width},
        "encoding": {
            # 線在這一列的尾端（band 的結束處）；不是邊界的列為長度 0 的線
            "y": {**position, "bandPosition": 1},
            "x": {"field": flag, "type": "quantitative", "scale": None},
            "x2": {"value": 0},
            "strokeOpacity": {
                "condition": {"test": f"datum['{flag}'] < 0", "value": 1},
                "value": 0,
            },
        },
    }
    _add_layers(spec, [layer])


def _levels(value: Any, levels: int) -> list[str]:
    """列（欄）的值中各層的成員（多層的合併成「外層｜內層」）。"""
    text = str(value if value is not None else "")
    return text.split(SEPARATOR) if levels > 1 else [text]


TREND_COLOR = "#4f4f4f"
"""趨勢線沒有設定顏色（也沒有依顏色分組）時的顏色。"""
TREND = "__ref_trend"
"""趨勢線的圖層名稱與資料欄前綴（__ref 開頭，檢視器選取標記時略過）。"""
TREND_SAMPLES = 60
"""曲線（與信賴區間）在連續的橫軸上取的點數。"""
DAY_ZERO = datetime(1899, 12, 30)
"""日期轉成數值時的第 0 天（與 FLOAT(日期) 相同）。"""


def _trend_lines(
    builder: _Builder, body: dict[str, Any], trend: TrendLine, records: list[dict[str, Any]]
) -> None:
    """趨勢線圖層：縱軸的值對橫軸的值配適模型（tmig.render.trends），每個窗格、依顏色分組時每個顏色
    一條，畫在各組資料的範圍內；信賴區間為上下兩條虛線，停在線上時顯示公式、R 平方與 P 值。
    資料附在圖層上（時間軸以 1899-12-30 起的天數計算）。"""
    encoding = main_encoding(body)
    x = encoding.get("x") if encoding is not None else None
    y = encoding.get("y") if encoding is not None else None
    if (
        encoding is None
        or not isinstance(x, dict)
        or not isinstance(y, dict)
        or y.get("type") != "quantitative"
        or x.get("type") not in ("quantitative", "temporal")
        or "field" not in x
        or "field" not in y
    ):
        builder.notes.append("趨勢線需要橫軸與縱軸都是連續的座標軸，未畫出")
        return
    temporal = x["type"] == "temporal"
    excluded = {
        builder.channels[use_key(u)].name for u in trend.excluded if use_key(u) in builder.channels
    }
    keys = [k for k in _scope_keys(builder)["pane"] if k not in excluded]
    color = encoding.get("color")
    color_field: str | None = None
    if (
        trend.by_color
        and isinstance(color, dict)
        and color.get("type") in ("nominal", "ordinal")
        and "field" in color
    ):
        color_ch = next(
            (c for c in builder.channels.values() if color["field"] in (c.name, c.label)), None
        )
        if color_ch is None or color_ch.name not in excluded:
            color_field = color["field"]
    groups: dict[tuple[Any, ...], list[tuple[float, float, Any]]] = {}
    for record in records:
        if record.get(TOTAL):
            continue
        raw, value = record.get(x["field"]), record.get(y["field"])
        number = _date_number(raw) if temporal else (float(raw) if _is_number(raw) else None)
        if number is None or not _is_number(value):
            continue
        key = tuple(record.get(k) for k in keys)
        if color_field is not None:
            key += (record.get(color_field),)
        groups.setdefault(key, []).append((number, float(value), raw))
    x_title, y_title = str(x.get("title") or ""), str(y.get("title") or "")
    rows: list[dict[str, Any]] = []
    bands = False
    for group, (key, points) in enumerate(groups.items()):
        model = fit_model(
            [(px, py) for px, py, _ in points], trend.fit, trend.degree, trend.intercept
        )
        if model is None:
            continue
        if temporal:
            # 時間軸：取各標記的時間（字串照原樣，與標記的時間以同樣的方式解析）
            positions = dict(sorted((px, raw) for px, _, raw in points))
            samples = [(px, raw) for px, raw in positions.items() if model.low <= px <= model.high]
        elif trend.fit == "linear" and not trend.confidence_bands:
            samples = [(model.low, model.low), (model.high, model.high)]
        else:
            step = (model.high - model.low) / (TREND_SAMPLES - 1)
            samples = [(v, v) for v in (model.low + step * i for i in range(TREND_SAMPLES))]
        common: dict[str, Any] = {f"{TREND}_group": group}
        # 窗格的維度值：分面（小倍數）時每個窗格只畫自己的趨勢線
        common.update(zip(keys, key[: len(keys)], strict=True))
        if color_field is not None:
            common[color_field] = key[-1]
        if trend.tooltips:
            common[f"{TREND}_model"] = model.equation(y_title, x_title)
            common[f"{TREND}_r2"] = format_r2(model.r2)
            common[f"{TREND}_p"] = format_p(model.p_value)
        for number, at in samples:
            predicted = model.predict(number)
            if predicted is None or not math.isfinite(predicted):
                continue
            row = {**common, f"{TREND}_x": at, f"{TREND}_y": predicted}
            bounds = model.bounds(number) if trend.confidence_bands else None
            if bounds is not None and all(math.isfinite(b) for b in bounds):
                row[f"{TREND}_low"], row[f"{TREND}_high"] = bounds
                bands = True
            rows.append(row)
    if not rows:
        builder.notes.append("趨勢線的資料不足（每組至少要有與參數個數相同的標記），未畫出")
        return

    def position(field: str, source: dict[str, Any], kind: str) -> dict[str, Any]:
        # 與主圖共用刻度與座標軸：同樣的標題與刻度設定
        out: dict[str, Any] = {"field": field, "type": kind}
        for prop in ("title", "scale", "timeUnit"):
            if prop in source:
                out[prop] = copy.deepcopy(source[prop])
        return out

    line: dict[str, Any] = {
        "x": position(f"{TREND}_x", x, x["type"]),
        "y": position(f"{TREND}_y", y, "quantitative"),
        "detail": {"field": f"{TREND}_group", "type": "nominal"},
    }
    mark: dict[str, Any] = {"type": "line", "strokeWidth": trend.width}
    if trend.pattern != "solid":
        mark["strokeDash"] = DASHES[trend.pattern]
    if trend.color is not None:
        mark["color"] = _css_color(trend.color)
    elif color_field is not None and isinstance(color, dict):
        # 與標記共用色彩刻度與圖例（同樣的欄位與設定，圖例合併成一個）
        line["color"] = copy.deepcopy(color)
    else:
        mark["color"] = TREND_COLOR
    if trend.tooltips:
        tips: list[dict[str, Any]] = []
        if color_field is not None and isinstance(color, dict):
            tips.append({"field": color_field, "title": color.get("title", "")})
        tips += [
            {"field": f"{TREND}_model", "title": "Trend Line"},
            {"field": f"{TREND}_r2", "title": "R-Squared"},
            {"field": f"{TREND}_p", "title": "P-value"},
        ]
        line["tooltip"] = tips
    else:
        mark["tooltip"] = False
    layers = [{"name": TREND, "data": {"values": rows}, "mark": mark, "encoding": line}]
    if bands:
        for bound in ("low", "high"):
            layers.append(
                {
                    "name": f"{TREND}_{bound}",
                    "data": {"values": rows},
                    "mark": {**mark, "strokeDash": DASHES["dashed"]},
                    "encoding": {**line, "y": position(f"{TREND}_{bound}", y, "quantitative")},
                }
            )
    _add_layers(body, layers)


def _date_number(value: Any) -> float | None:
    """日期、日期時間（ISO 8601 字串）→ 1899-12-30 起的天數（含一天中的時間）。"""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return (moment - DAY_ZERO).total_seconds() / 86400


def _column_header_divider(
    spec: dict[str, Any],
    encoding: dict[str, Any],
    values: list[dict[str, Any]],
    levels: int,
    divider: Divider,
    extent: int,
) -> None:
    """欄標題區域的分隔線：以座標軸的刻度畫在各組的邊界（tickBand extent），長度為標題的高度。"""
    level = _divider_level(divider, levels)
    edges = _band_edges(values, encoding["field"], levels, level) if level >= 1 else []
    if not edges:
        return
    axis = dict(encoding.get("axis") or {})
    test = f"indexof({json.dumps(edges, ensure_ascii=False)}, datum.value) >= 0"
    axis.update(
        ticks=True,
        tickBand="extent",
        tickSize=extent,
        tickColor=_css_color(divider.color),
        tickWidth=divider.width,
        tickOpacity={"condition": {"test": test, "value": 1}, "value": 0},
    )
    encoding["axis"] = axis
    _flush_bands(spec, encoding, "x")


def _flush_bands(spec: dict[str, Any], encoding: dict[str, Any], channel: str) -> None:
    """分隔線與色帶畫在列（欄）之間：band scale 的列（欄）之間沒有空隙，長條維持原本的粗細。"""
    scale = dict(encoding.get("scale") or {})
    scale.update(type="band", paddingInner=0, paddingOuter=0)
    encoding["scale"] = scale
    size = "height" if channel == "y" else "width"

    def keep_bar_size(mark: dict[str, Any]) -> None:
        if mark.get("type") == "bar":
            mark.setdefault(size, {"band": BAR_BAND})

    _visit_marks(spec, keep_bar_size)


def _banding(
    spec: dict[str, Any],
    encoding: dict[str, Any],
    channel: str,
    values: list[dict[str, Any]],
    banding: Banding,
    extent: int | None,
    levels: int = 1,
) -> None:
    """色帶：每 size 列（欄）為一段，隔段畫上窗格的顏色（列標題的寬度有設定時，標題也畫上標題的
    顏色）；設定了層級（band-level）時以前幾層標題的組為單位。色帶畫在最下層，不參與選取。"""
    shelf = "rows" if channel == "y" else "columns"
    flag = f"{BAND}{shelf}"
    field_name = encoding["field"]
    keys = list(dict.fromkeys(v.get(field_name) for v in values))
    order: dict[Any, int] = {}
    previous: list[str] | None = None
    index = -1
    for value in keys:
        if banding.level is None or banding.level >= levels:
            order[value] = len(order)  # 每一列（欄）
            continue
        group = _levels(value, levels)[: banding.level]
        if group != previous:
            index += 1
        order[value] = index
        previous = group
    for record in values:
        record[flag] = (order[record.get(field_name)] // banding.size) % 2 == 1
    _flush_bands(spec, encoding, channel)  # 色帶連成一片
    position = {k: encoding[k] for k in ("field", "type", "sort", "scale") if k in encoding}
    test = f"datum['{flag}'] === true"

    def band(color: str, span: int | None, part: str) -> dict[str, Any]:
        layer_encoding: dict[str, Any] = {
            channel: position,
            "color": {
                "condition": {"test": test, "value": _css_color(color)},
                "value": "transparent",
            },
        }
        if span is not None:
            layer_encoding.update(x={"value": -span}, x2={"value": 0})
        return {
            "name": f"{DECO}{flag}_{part}",
            "transform": [{"calculate": "-1", "as": ROW}],
            "mark": {"type": "rect"},
            "encoding": layer_encoding,
        }

    layers: list[dict[str, Any]] = []
    if banding.pane is not None:
        layers.append(band(banding.pane, None, "pane"))
    if banding.header is not None and extent is not None and channel == "y":
        # 欄的標題位置依圖而定（上方或下方），只畫列標題的色帶
        layers.append(band(banding.header, extent, "header"))
    if not layers:
        return
    body = {
        k: spec.pop(k) for k in ("mark", "encoding", "layer", "transform", "resolve") if k in spec
    }
    spec["layer"] = [*layers, body]


def _visit_marks(node: Any, visit: Callable[[dict[str, Any]], Any]) -> None:
    if isinstance(node, list):
        for item in node:
            _visit_marks(item, visit)
        return
    if not isinstance(node, dict):
        return
    mark = node.get("mark")
    if isinstance(mark, dict):
        visit(mark)
    for key in ("layer", "vconcat", "hconcat", "concat", "spec"):
        if key in node:
            _visit_marks(node[key], visit)


DISCRETE_INTEGER_FORMAT = "0"
"""離散維度的整數沒有設定格式時不加千分位（與 Tableau 相同，例如年份顯示 2026 而不是 2,026）。"""


def value_format(ws: Worksheet, data_field: DataField, use: FieldUse) -> str | None:
    """欄位值的顯示格式：工作表上設定的格式優先（Measure Values 的格式套用到其中每個量值），
    其次是欄位的預設格式（百分比類的快速 Table Calculation 預設以百分比顯示）；都沒有時，
    離散維度的整數不加千分位。"""
    key = use_key(use)
    sheet_format = ws.formats.get(key)
    if sheet_format is None and any(use_key(u) == key for u in ws.measure_values):
        sheet_format = ws.formats.get(MEASURE_VALUES)
    found = sheet_format or display_format(data_field.format, _table_calc_type(ws, use))
    if (
        found is None
        and data_field.datatype == "integer"
        and use.agg is None
        and use.date is None
        and use.table_calc is None
        and not use.continuous
    ):
        return DISCRETE_INTEGER_FORMAT
    return found


def _text_lines(
    runs: list[TextRun], field_style: Callable[[FieldUse], TextStyle | None] | None = None
) -> list[tuple[list[TextRun], TextStyle | None]]:
    """自訂文字依換行分成各行：每一行的各段，與這一行的格式（第一段有內容的格式，字級取這一行最大的）。
    欄位的值以欄位在標記上的字型（field_style）為底，再套用這一段的格式。"""
    lines: list[list[TextRun]] = [[]]
    for run in runs:
        base = field_style(run.use) if field_style is not None and run.use is not None else None
        if base is not None:
            own = run.style.model_dump(exclude_none=True) if run.style is not None else {}
            run = run.model_copy(update={"style": base.model_copy(update=own)})
        if run.text is not None and "\n" in run.text:
            for k, part in enumerate(run.text.split("\n")):
                if k:
                    lines.append([])
                if part:
                    lines[-1].append(run.model_copy(update={"text": part}))
        else:
            lines[-1].append(run)
    result: list[tuple[list[TextRun], TextStyle | None]] = []
    for line in lines:
        visible = [r for r in line if r.text is None or r.text.strip()]
        style = next((r.style for r in visible if r.style is not None), None)
        sizes = [r.style.size for r in visible if r.style is not None and r.style.size]
        if style is not None and sizes:
            style = style.model_copy(update={"size": max(sizes)})
        result.append((line, style))
    return result


def _tooltip_template(
    ws: Worksheet, ir: IR, channels: dict[str, Channel], params: dict[str, Scalar]
) -> dict[str, Any] | None:
    """自訂的工具提示（usermeta.tooltip）；都是預設（顯示、列出各欄位的值）時為 None。

    show：是否顯示工具提示。runs：自訂文字依序的各段——文字（text，參數已代入目前的值），或欄位的值
    （column 為顯示文字的資料欄，value 為原始值的資料欄：Null 時不顯示，除非 Null 設有別名），各段帶著格式
    （style）。檢視器以滑過的標記的資料組成工具提示。各標記卡的自訂文字不同時取第一個。"""
    cards = [ws.marks, *(pane.marks for pane in ws.panes)]
    text = next((marks.tooltip_text for marks in cards if marks.tooltip_text), [])
    if ws.tooltips and not text:
        return None
    runs: list[dict[str, Any]] = []
    for run in text:
        item: dict[str, Any]
        if run.text is not None:
            item = {"text": run.text}
        elif run.parameter is not None:
            param = next((p for p in ir.parameters if p.id == run.parameter), None)
            value = params.get(run.parameter)
            if param is None or value is None:
                continue
            label = parameter_label(param, value)
            item = {"text": label if label is not None else format_value(value, param.datatype)}
        else:
            assert run.use is not None
            key = run.use.field if run.use.field in SPECIAL_FIELDS else use_key(run.use)
            ch = channels.get(key)
            if ch is None:
                continue  # 不在檢視中的欄位沒有值
            item = {"column": ch.label, "value": ch.name, "null": None in ch.aliases}
        if run.style is not None:
            item["style"] = run.style.model_dump(mode="json", exclude_none=True)
        runs.append(item)
    return {"show": ws.tooltips, "runs": runs}


def compose_text(
    runs: list[TextRun],
    ir: IR,
    params: dict[str, Scalar],
    channels: dict[str, Channel] | None = None,
    record: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """自訂文字（標籤、標題）：文字、參數的值與欄位值依序串接；Null 的欄位不顯示（與 Tableau 相同）。

    欄位值取自這個標記（channels、record），或已算好的顯示文字（fields：欄位鍵 → 文字，標題用）。"""
    parts: list[str] = []
    for run in runs:
        if run.text is not None:
            parts.append(run.text)
        elif run.parameter is not None:
            param = next((p for p in ir.parameters if p.id == run.parameter), None)
            value = params.get(run.parameter)
            if param is not None and value is not None:
                label = parameter_label(param, value)
                parts.append(label if label is not None else format_value(value, param.datatype))
        elif run.use is not None and fields is not None:
            parts.append(fields.get(use_key(run.use), ""))
        elif run.use is not None and channels is not None and record is not None:
            key = run.use.field if run.use.field in SPECIAL_FIELDS else use_key(run.use)
            ch = channels.get(key)
            # Null 的欄位不顯示，除非 Null 設有別名
            if ch is not None and (record.get(ch.name) is not None or None in ch.aliases):
                parts.append(str(record.get(ch.label, "")))
    return "".join(parts)


def worksheet_title(
    ir: IR,
    ws: Worksheet,
    parameters: dict[str, Scalar] | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """工作表的標題：自訂標題（代入目前的參數值與標題中的欄位值，見 title_values），沒有時為工作表名稱。"""
    if not ws.title:
        return ws.name
    params = {p.id: p.value for p in ir.parameters} | (parameters or {})
    return compose_text(ws.title, ir, params, fields=fields or {}) or ws.name


def worksheet_caption(
    ir: IR, ws: Worksheet, parameters: dict[str, Scalar] | None = None, force: bool = False
) -> str | None:
    """工作表的說明（Caption）：不顯示時為 None；自訂的文字（代入目前的參數值），沒有時為自動產生的說明。
    force：不論工作表本身是否顯示（dashboard 的區塊各自決定是否顯示說明）。"""
    if not ws.caption_shown and not force:
        return None
    if ws.caption:
        params = {p.id: p.value for p in ir.parameters} | (parameters or {})
        text = compose_text(ws.caption, ir, params, fields={})
        if text:
            return text
    return _auto_caption(ir, ws)


def _auto_caption(ir: IR, ws: Worksheet) -> str:
    """自動產生的說明：欄列架上的維度與量值、標記卡上的欄位與篩選的欄位（Tableau 的自動說明是英文的句子，
    這裡是相同內容的簡短敘述）。"""
    fields = {f.id: f for d in ir.datasources for f in d.fields}

    def name(use: FieldUse) -> str:
        if use.field == MEASURE_NAMES:
            return MEASURE_NAMES_TITLE
        if use.field == MEASURE_VALUES:
            return "Measure Values"
        data_field = fields.get(use.field)
        return caption(data_field, use, ws) if data_field is not None else use.field

    def measure(use: FieldUse) -> bool:
        data_field = fields.get(use.field)
        return (
            use.agg is not None
            or use.table_calc is not None
            or use.field == MEASURE_VALUES
            or (data_field is not None and data_field.role == "measure")
        )

    shelves = [*ws.columns, *ws.rows]
    measures = list(dict.fromkeys(name(u) for u in shelves if measure(u)))
    dims = list(dict.fromkeys(name(u) for u in shelves if not measure(u)))
    parts: list[str] = []
    if measures and dims:
        parts.append(f"{'、'.join(dims)}的{'與'.join(measures)}。")
    elif measures or dims:
        parts.append(f"{'、'.join(measures or dims)}。")
    marks = ws.marks
    for label, use in (("色彩", marks.color), ("大小", marks.size), ("形狀", marks.shape)):
        if use is not None:
            parts.append(f"{label}顯示{name(use)}。")
    if marks.label:
        parts.append(f"標記的標籤為{'、'.join(name(u) for u in marks.label)}。")
    if marks.detail:
        parts.append(f"詳細資料顯示{'、'.join(name(u) for u in marks.detail)}。")
    filtered = list(
        dict.fromkeys(name(f.field) for f in ws.filters if not isinstance(f, TupleFilter))
    )
    if filtered:
        parts.append(f"資料依{'、'.join(filtered)}篩選。")
    return "".join(parts)


MULTIPLE_VALUES = "*"
"""標題中的維度有多個值時顯示的文字（與 Tableau 的 ATTR 相同）。"""


def title_values(
    ir: IR,
    ws: Worksheet,
    queries: TitleQueries,
    measures: QueryResult | None,
    dimensions: dict[str, QueryResult],
    main: tuple[list[OutputColumn], QueryResult] | None = None,
) -> dict[str, str]:
    """標題中欄位值的顯示文字（欄位鍵 → 文字）：量值依格式顯示；維度只有一個值時顯示它（成員別名、
    日期部分照樣套用），多個值時為 *；Null 與沒有資料時不顯示。
    main：工作表查詢的欄位與結果（標題中在檢視中的維度直接取它的值）。"""
    fields = {f.id: f for d in ir.datasources for f in d.fields}
    starts = fiscal_starts(ir)
    texts: dict[str, str] = {}
    if queries.measures is not None and measures is not None and measures.rows:
        index = {key: i for i, key in enumerate(measures.columns)}
        row = measures.rows[0]
        for column in queries.measures.columns:
            use = column.use
            if use is None or column.key not in index or row[index[column.key]] is None:
                continue
            fmt = value_format(ws, fields[use.field], use)
            texts[column.key] = format_value(row[index[column.key]], column.datatype, fmt)
    sources: dict[str, tuple[OutputColumn, QueryResult]] = {}
    for key, query in queries.dimensions.items():
        column = next((c for c in query.columns if c.key == key), None)
        if column is not None and key in dimensions:
            sources[key] = (column, dimensions[key])
    if main is not None:
        main_columns, main_result = main
        for column in main_columns:
            if column.key in queries.from_main:
                sources[column.key] = (column, main_result)
    for key, (column, result) in sources.items():
        if column.use is None or key not in result.columns:
            continue
        position = result.columns.index(key)
        values = list(dict.fromkeys(row[position] for row in result.rows))
        if len(values) > 1:
            texts[key] = MULTIPLE_VALUES
            continue
        if not values or values[0] is None:
            continue
        value, use = values[0], column.use
        alias = member_aliases(fields[use.field], use).get(alias_key(value))
        fiscal = (
            fiscal_label(value, use.date.kind, use.date.unit, starts.get(use.field, 1))
            if use.date is not None
            else None
        )
        if alias is not None:
            texts[key] = alias
        elif fiscal is not None:
            texts[key] = fiscal
        elif use.date is not None and use.date.kind == "part":
            texts[key] = str(value)
        else:
            trunc = use.date.unit if use.date is not None else None
            fmt = value_format(ws, fields[use.field], use)
            texts[key] = format_value(value, column.datatype, fmt, trunc)
    return texts


def _dropped(record: dict[str, Any], ch: Channel) -> bool:
    """總計或小計的資料列中，這個維度是去掉的（原始值為空、顯示 Total 或 Grand Total）。"""
    return (
        bool(record.get(TOTAL))
        and record.get(ch.name) is None
        and record.get(ch.label) in (GRAND_TOTAL, SUBTOTAL)
    )


def _column_order(
    values: list[dict[str, Any]], field: str, members: list[Channel], left: bool = False
) -> list[Any]:
    """欄標題的順序：工作表的標記依名次（只看欄維度自己的排序，與列無關）；小計緊接在它的組
    （前段的標題相同的最後一欄）之後，總計在最後。left（列總計在左側）：小計在它的組之前，總計在最前。"""
    order: list[Any] = []
    headers: dict[Any, tuple[Any, ...]] = {}
    marks = [v for v in values if not v.get(TOTAL) and v.get(ORDER) is not None]
    for record in sorted(marks, key=lambda v: v[ORDER]):
        value = record.get(field)
        if value not in headers:
            headers[value] = tuple(record.get(ch.label) for ch in members)
            order.append(value)
    for record in values:
        value = record.get(field)
        if not record.get(TOTAL) or value in headers:
            continue
        headers[value] = tuple(record.get(ch.label) for ch in members)
        kept: list[Any] = []
        for ch in members:
            if _dropped(record, ch):
                break
            kept.append(record.get(ch.label))
        group = tuple(kept)
        same = [i for i, v in enumerate(order) if kept and headers[v][: len(group)] == group]
        if left:
            order.insert(same[0] if same else 0, value)
        else:
            order.insert(same[-1] + 1 if same else len(order), value)
    return order


def _sort_headers(node: Any, field: str, order: list[Any]) -> None:
    """規格中（含圖層與並排的圖）以欄標題為欄位、依資料順序的 x 與 column 改為指定的順序。"""
    if isinstance(node, list):
        for item in node:
            _sort_headers(item, field, order)
        return
    if not isinstance(node, dict):
        return
    encoding = node.get("encoding")
    if isinstance(encoding, dict):
        for channel in ("x", "column"):
            enc = encoding.get(channel)
            if isinstance(enc, dict) and enc.get("field") == field and enc.get("sort", 0) is None:
                enc["sort"] = order
    for key in ("layer", "vconcat", "hconcat", "concat", "spec"):
        if key in node:
            _sort_headers(node[key], field, order)


def after_groups[RowT](
    rows: Sequence[RowT], inserted: Sequence[RowT], group: Callable[[RowT], Hashable]
) -> list[RowT]:
    """小計放在各組之後：組相同（group 的值相同，含先放入的較內層小計）的最後一筆之後；
    找不到所屬的組時放在最後。圖表的表頭依資料的順序排列，小計因此緊接在它的組之後。"""
    last: dict[Hashable, int] = {}
    for i, row in enumerate(rows):
        last[group(row)] = i
    after: dict[int, list[RowT]] = {}
    tail: list[RowT] = []
    for row in inserted:
        i = last.get(group(row))
        if i is None:
            tail.append(row)
        else:
            after.setdefault(i, []).append(row)
    result: list[RowT] = []
    for i, row in enumerate(rows):
        result.append(row)
        result += after.get(i, [])
    return result + tail


def before_groups[RowT](
    rows: Sequence[RowT], inserted: Sequence[RowT], group: Callable[[RowT], Hashable]
) -> list[RowT]:
    """小計放在各組之前（欄總計在上方）：組相同的第一筆之前；找不到所屬的組時放在最前。"""
    first: dict[Hashable, int] = {}
    for i, row in enumerate(rows):
        first.setdefault(group(row), i)
    before: dict[int, list[RowT]] = {}
    head: list[RowT] = []
    for row in inserted:
        i = first.get(group(row))
        if i is None:
            head.append(row)
        else:
            before.setdefault(i, []).append(row)
    result: list[RowT] = list(head)
    for i, row in enumerate(rows):
        result += before.get(i, [])
        result.append(row)
    return result


def _exclude_totals_from_color(spec: dict[str, Any], mark: str, text: str) -> None:
    """與 Tableau 的預設相同，依量值著色時總計不上色（醒目提示表最明顯）；總計也不影響色階的範圍，
    見 _Builder.common。"""
    color = spec.get("encoding", {}).get("color")
    if not isinstance(color, dict) or color.get("type") != "quantitative":
        return
    color["condition"] = {
        "test": f"datum['{TOTAL}'] === true",
        "value": "transparent" if mark == "square" else text,
    }


def _records(
    channels: dict[str, Channel], result: QueryResult, null_text: str | None = None
) -> list[dict[str, Any]]:
    """查詢結果 → 資料列：原始值欄與顯示文字欄；只轉換結果中有的欄位。null_text：量值是 Null 時的
    顯示文字（特殊值的文字）。"""
    index = {key: i for i, key in enumerate(result.columns)}
    present = [(key, ch) for key, ch in channels.items() if key in index]
    records: list[dict[str, Any]] = []
    for row in result.rows:
        record: dict[str, Any] = {}
        for key, ch in present:
            raw = row[index[key]]
            use = ch.use
            trunc = use.date.unit if use.date is not None and use.date.kind == "trunc" else None
            record[ch.name] = _json_value(raw)
            alias = ch.aliases.get(alias_key(raw)) if ch.aliases else None
            fiscal = (
                fiscal_label(raw, use.date.kind, use.date.unit, ch.fiscal)
                if use.date is not None and ch.fiscal != 1
                else None
            )
            if alias is not None:
                record[ch.label] = alias
            elif fiscal is not None:
                record[ch.label] = fiscal
            elif use.date is not None and use.date.kind == "part" and raw is not None:
                record[ch.label] = str(raw)
            elif raw is None and null_text is not None and ch.role == "measure":
                record[ch.label] = null_text
            else:
                record[ch.label] = format_value(raw, ch.datatype, ch.format, trunc)
        records.append(record)
    return records


COMPUTATION_NAMES = {
    "constant": "Constant",
    "average": "Average",
    "sum": "Sum",
    "total": "Total",
    "min": "Minimum",
    "max": "Maximum",
    "median": "Median",
    "percentile": "Percentile",
    "stdev": "Std. Dev.",
    "quantiles": "Quantiles",
    "confidence": "Confidence Interval",
    "median_confidence": "Median Confidence Interval",
}
SCOPE_NAMES = {"table": "Table", "pane": "Pane", "cell": "Cell"}
REF_COLOR = "#b4b4b4"
"""參考線的預設顏色與粗細（依 Logshark 報表文件中 Tableau 的截圖量測：2 像素的灰線）。"""
REF_WIDTH = 2
REF_LABEL_COLOR = "#555555"
BAND_COLOR = "#e0e0e0"
"""參考區間（兩條參考線之間）預設的底色。"""
SHADES = ("#f6f6f6", "#dddddd")
"""分布的底色：最內側到最外側（依截圖量測：標準差 2 到 3 之間 #f6f6f6、3 以上 #dddddd）。"""
BOX_COLORS = ("#e6e6e6", "#cfcfcf")
"""盒鬚圖的盒子：中位數以下、以上兩種灰色（依公開資料推定）。"""
WHISKER_COLOR = "#999999"
REF = "__ref"
"""參考線的資料欄與圖層名稱前綴；檢視器選取標記時略過這些圖層。"""


@dataclass(frozen=True)
class _Constant:
    """參考線的值是常數或參數：數值，或時間軸上的時間（UTC 毫秒）。"""

    value: float


def _with_reference_lines(
    builder: _Builder,
    body: dict[str, Any],
    values: list[dict[str, Any]],
    params: dict[str, Scalar],
) -> dict[str, Any]:
    """參考線、參考區間、分布與盒鬚圖：每一列資料寫入所屬範圍的值（__ref…），再畫出。

    範圍：整張表；窗格＝欄列架上的離散維度，但各架最內層若是離散維度則不算（它切出的是儲存格）；
    儲存格＝欄列架上所有的離散維度。線畫在標記之上；底色（參考區間、分布、盒鬚圖）畫在標記之下。
    同一架上有多個量值（每個量值一張圖）與雙軸時，每張圖（雙軸的每一層）畫自己的座標軸上的參考線，
    與那個量值共用刻度。
    """
    ws, notes = builder.ws, builder.notes
    handled: set[int] = set()

    def walk(node: dict[str, Any]) -> dict[str, Any]:
        for key in ("vconcat", "hconcat"):
            if key in node:
                return {**node, key: [walk(child) for child in node[key]]}
        if "layer" in node and "resolve" in node and not isinstance(node.get("encoding"), dict):
            # 雙軸：每一層是一個量值的圖，各自的刻度
            return {**node, "layer": [walk(child) for child in node["layer"]]}
        if "mark" not in node and "layer" not in node:
            return node
        data = node.get("data")
        own = isinstance(data, dict) and "values" in data
        records = data["values"] if isinstance(data, dict) and "values" in data else values
        # 同一個量值在雙軸的兩層（例如區域加折線）時，參考線只畫在第一層
        found, done = _chart_reference_lines(builder, node, records, params, handled)
        handled.update(done)
        if own and found is not node:
            # 這張圖有自己的資料（詳細程度較粗的標記卡）：參考線的圖層也用它
            inner = [
                {k: v for k, v in layer.items() if k != "data"} if layer is node else layer
                for layer in found["layer"]
            ]
            return {"data": data, "layer": inner}
        return found

    if not any(k in body for k in ("vconcat", "hconcat", "mark", "layer")):
        notes.append("圓餅等沒有座標軸的圖表上的參考線尚未畫出（不影響數字）")
        return body
    body = walk(body)
    for n, line in enumerate(ws.reference_lines):
        if n not in handled:
            notes.append(f"參考線 {line.id} 的量值不在座標軸上，沒有畫出")
    return body


def _chart_reference_lines(
    builder: _Builder,
    body: dict[str, Any],
    values: list[dict[str, Any]],
    params: dict[str, Scalar],
    skip: set[int] | None = None,
) -> tuple[dict[str, Any], set[int]]:
    """一張圖（有 x、y 的 encoding）上的參考線：只畫量值在這張圖的座標軸上、還沒畫過（skip 以外）的；
    回傳圖與處理過的參考線。"""
    ws, notes = builder.ws, builder.notes
    encoding = main_encoding(body)
    if not isinstance(encoding, dict):
        return body, set()
    scopes = _scope_keys(builder)
    behind: list[dict[str, Any]] = []
    front: list[dict[str, Any]] = []
    drawn: dict[str, tuple[str, str]] = {}
    handled: set[int] = set()
    for n, line in enumerate(ws.reference_lines):
        axis = builder.channels.get(use_key(line.axis))
        position = next(
            (
                c
                for c in ("x", "y")
                if axis is not None
                and isinstance(encoding.get(c), dict)
                and encoding[c].get("field") == axis.name
            ),
            None,
        )
        if axis is None or position is None or (skip is not None and n in skip):
            continue
        handled.add(n)
        group_keys = scopes[line.scope]
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for record in values:
            if record.get(TOTAL):
                continue  # 總計的列與欄不畫參考線，也不納入計算
            groups.setdefault(tuple(record.get(k) for k in group_keys), []).append(record)
        place = _Placement(encoding, position, group_keys, len(groups))
        if line.kind == "boxplot":
            _box_plot(n, line, axis.name, groups, place, behind, body)
            continue
        temporal = encoding[position].get("type") == "temporal"
        source = _reference_source(line, builder.channels, params, temporal)
        if source is None:
            notes.append(f"參考線 {line.id} 的值不在查詢結果中，沒有畫出")
            continue
        if line.formula == "total" and isinstance(source, str):
            agg = line.value.agg if isinstance(line.value, FieldUse) else None
            if agg not in ("sum", "count", "min", "max"):
                notes.append(f"參考線 {line.id} 的合計需要重新彙總整體資料，沒有畫出")
                continue
        count = len(_reference_values(line, [1.0, 2.0, 3.0], "x"))
        names = [f"{REF}{n}_{i}" for i in range(count)]
        first: list[float | None] = []
        for members in groups.values():
            numbers = (
                [float(v) for m in members if _is_number(v := m.get(source))]
                if isinstance(source, str)
                else []
            )
            computed = _reference_values(line, numbers, source)
            if not first:
                first = list(computed)
            for name, value in zip(names, computed, strict=True):
                for m in members:
                    m[name] = value
        drawn[line.id] = (names[0], position)
        style = _line_style(line)
        if style is not None:
            for name in names:
                front.append(place.line(name, style))
        if line.kind == "distribution":
            for i in range(len(names) - 1):
                shade = _shade(i + 1, len(names) - 1 + line.fill_above + line.fill_below)
                behind.append(place.fill(f"{REF}{n}_fill{i}", names[i], names[i + 1], shade))
        top = len(names) - 1 + line.fill_above + line.fill_below
        if line.fill_above and (line.above_color is None or not line.above_color.endswith("00")):
            color = _css_color(line.above_color) if line.above_color else _shade(top, top)
            behind.append(place.fill(f"{REF}{n}_above", names[-1], "end", color))
        if line.fill_below and (line.below_color is None or not line.below_color.endswith("00")):
            color = _css_color(line.below_color) if line.below_color else _shade(top, top)
            behind.append(place.fill(f"{REF}{n}_below", names[0], "start", color))
        partner = drawn.get(line.paired) if line.paired else None
        if partner is not None and partner[1] == position:
            # 參考區間：兩條參考線之間塗上底色（後一條畫的時候兩條都算好了）
            color = _css_color(line.fill_color) if line.fill_color else BAND_COLOR
            behind.append(place.fill(f"{REF}{n}_band", partner[0], names[0], color))
        if len(groups) == 1:
            for i, text in _reference_labels(line, axis, first, temporal):
                front.append(place.label(f"{names[i]}_label", names[i], text))
    if not behind and not front:
        return body, handled
    return {"layer": [*behind, body, *front]}, handled


ANNOTATION = f"{DECO}_annotation"
"""註解的圖層名稱與資料欄前綴（裝飾圖層，檢視器選取標記時略過）。"""
ANNOTATION_COLOR = "#333333"
ANNOTATION_LINE = "#8c8c8c"


def _with_annotations(
    builder: _Builder,
    body: dict[str, Any],
    values: list[dict[str, Any]],
    ir: IR,
    params: dict[str, Scalar],
) -> dict[str, Any]:
    """註解：標記的註解畫在那個標記的位置，點與區域的註解畫在座標軸的值上；文字相對於位置偏移時
    畫一條線連到文字（與 Tableau 相同）。只畫在第一列資料所在的窗格。"""
    ws, notes = builder.ws, builder.notes
    encoding = main_encoding(body)
    rows = [v for v in values if not v.get(TOTAL)]
    if not isinstance(encoding, dict) or not rows:
        notes.append("這張圖表上的註解尚未畫出（不影響數字）")
        return body
    layers: list[dict[str, Any]] = []
    for n, annotation in enumerate(ws.annotations):
        name = f"{ANNOTATION}{n}"
        dx, dy = annotation.offset
        if annotation.kind == "mark":
            assert annotation.point is not None
            record = _annotation_record(builder, rows, annotation.point.mark or {})
            if record is None:
                notes.append(f"註解 {n + 1} 的標記不在目前的資料中，沒有畫出")
                continue
            record[name] = compose_text(annotation.text, ir, params, builder.channels, record)
            position = {
                c: _band_center(encoding[c])
                for c in ("x", "y")
                if isinstance(encoding.get(c), dict)
            }
            layers += _annotation_text(name, position, dx, dy)
            continue
        record = rows[0]
        if annotation.kind == "point":
            assert annotation.point is not None
            position = _annotation_position(builder, encoding, annotation.point, record, name)
            if position is None:
                notes.append(f"註解 {n + 1} 的位置不在這張圖的座標軸上，沒有畫出")
                continue
            record[name] = compose_text(annotation.text, ir, params, builder.channels, record)
            layers += _annotation_text(name, position, dx, dy)
            continue
        assert annotation.top_left is not None and annotation.bottom_right is not None
        start = _annotation_position(builder, encoding, annotation.top_left, record, f"{name}_a")
        end = _annotation_position(builder, encoding, annotation.bottom_right, record, f"{name}_b")
        if start is None or end is None or set(start) != set(end):
            notes.append(f"註解 {n + 1} 的區域不在這張圖的座標軸上，沒有畫出")
            continue
        record[name] = compose_text(annotation.text, ir, params, builder.channels, record)
        box: dict[str, Any] = dict(start)
        for channel, enc in end.items():
            box[f"{channel}2"] = {"field": enc["field"]}
        center: dict[str, Any] = {}
        for channel in start:
            first = record.get(start[channel]["field"])
            second = record.get(end[channel]["field"])
            mid = f"{name}_{channel}mid"
            record[mid] = (
                (first + second) / 2 if _is_number(first) and _is_number(second) else first
            )
            center[channel] = {**start[channel], "field": mid}
        layers.append(
            {
                "name": f"{name}_box",
                "transform": [{"filter": f"isValid(datum[{name!r}])"}],
                "mark": {
                    "type": "rect",
                    "fill": "#f2f2f2",
                    "fillOpacity": 0.6,
                    "stroke": "#9a9a9a",
                    "strokeWidth": 1,
                    "tooltip": False,
                },
                "encoding": box,
            }
        )
        layers.append(
            {
                "name": name,
                "transform": [{"filter": f"isValid(datum[{name!r}])"}],
                "mark": {
                    "type": "text",
                    "align": "center",
                    "baseline": "middle",
                    "color": ANNOTATION_COLOR,
                    "fontSize": 11,
                    "lineBreak": "\n",
                    "tooltip": False,
                },
                "encoding": {**center, "text": {"field": name}},
            }
        )
    if not layers:
        return body
    return {"layer": [body, *layers]}


FORECAST_BAND = f"{DECO}_forecast"


def _with_forecast_bands(
    builder: _Builder, body: dict[str, Any], values: list[dict[str, Any]], result: QueryResult
) -> dict[str, Any]:
    """預測區間：估計的期以淺色的區域畫出上下界（畫在標記的後面）。"""
    from tmig.planner.analytics import HIGH, LOW

    ws = builder.ws
    assert ws.forecast is not None
    encoding = main_encoding(body)
    if not isinstance(encoding, dict):
        return body
    index = {key: i for i, key in enumerate(result.columns)}
    layers: list[dict[str, Any]] = []
    for n, use in enumerate(ws.forecast.measures):
        channel = builder.channels.get(use_key(use))
        key = use_key(use)
        if channel is None or LOW + key not in index:
            continue
        axis = next(
            (
                c
                for c in ("y", "x")
                if isinstance(encoding.get(c), dict) and encoding[c].get("field") == channel.name
            ),
            None,
        )
        other = "x" if axis == "y" else "y"
        if axis is None or not isinstance(encoding.get(other), dict):
            continue
        low, high = f"{FORECAST_BAND}{n}_low", f"{FORECAST_BAND}{n}_high"
        for record, row in zip(values, result.rows, strict=False):
            record[low] = _json_value(row[index[LOW + key]])
            record[high] = _json_value(row[index[HIGH + key]])
        position = dict(encoding[other])
        if position.get("type") in ("nominal", "ordinal"):
            # 離散的時間軸：區域的頂點在格子的中間（與標記相同）
            position["bandPosition"] = 0.5
        band: dict[str, Any] = {
            other: position,
            axis: {**encoding[axis], "field": low},
            f"{axis}2": {"field": high},
        }
        series = _series_detail(encoding)
        if series:
            band["detail"] = series  # 每條序列（色彩、詳細資料）各自的區間
        layers.append(
            {
                # 實際值的列沒有區間（Null），區域在這些列斷開。不先篩掉這些列：離散軸的順序由
                # 各層資料依序合併，篩掉會讓估計的期排到最前面
                "name": f"{FORECAST_BAND}{n}",
                "mark": {"type": "area", "color": "#bab0ac", "opacity": 0.3, "tooltip": False},
                "encoding": band,
            }
        )
    if not layers:
        return body
    return {"layer": [*layers, body]}


def _series_detail(encoding: dict[str, Any]) -> list[dict[str, Any]]:
    """各自成一條序列的離散欄位（色彩、詳細資料、形狀）。"""
    found: list[dict[str, Any]] = []
    for channel in ("color", "detail", "shape"):
        defs = encoding.get(channel)
        for item in defs if isinstance(defs, list) else [defs]:
            if (
                isinstance(item, dict)
                and isinstance(item.get("field"), str)
                and item.get("type") in ("nominal", "ordinal")
                and all(f["field"] != item["field"] for f in found)
            ):
                found.append({"field": item["field"], "type": item["type"]})
    return found


def _annotation_record(
    builder: _Builder, rows: list[dict[str, Any]], mark: dict[str, Scalar]
) -> dict[str, Any] | None:
    """標記的註解所在的那一列：標記的維度值都相同（工作表上沒有的欄位不比對）。"""
    pairs = [(builder.channels[k].name, v) for k, v in mark.items() if k in builder.channels]
    if not pairs:
        return None
    return next((r for r in rows if all(r.get(name) == value for name, value in pairs)), None)


def _band_center(enc: dict[str, Any]) -> dict[str, Any]:
    """離散軸上的位置取格子的中間（與標記相同）。"""
    out = {k: v for k, v in enc.items() if k in ("field", "type", "title", "scale", "timeUnit")}
    if out.get("type") in ("nominal", "ordinal"):
        out["bandPosition"] = 0.5
    return out


def _annotation_position(
    builder: _Builder,
    encoding: dict[str, Any],
    point: AnnotationPoint,
    record: dict[str, Any],
    name: str,
) -> dict[str, Any] | None:
    """點或區域角落的位置：座標軸的值寫入第一列資料（name_x、name_y），沿用座標軸的比例尺。"""
    position: dict[str, Any] = {}
    for value, axis in ((point.x, point.x_axis), (point.y, point.y_axis)):
        if axis is None or value is None:
            continue
        channel = builder.channels.get(use_key(axis))
        found = next(
            (
                c
                for c in ("x", "y")
                if channel is not None
                and isinstance(encoding.get(c), dict)
                and encoding[c].get("field") == channel.name
            ),
            None,
        )
        if found is None:
            return None
        field = f"{name}_{found}"
        record[field] = value
        position[found] = {**_band_center(encoding[found]), "field": field}
    return position or None


def _annotation_text(name: str, position: dict[str, Any], dx: int, dy: int) -> list[dict[str, Any]]:
    """標記與點的註解：文字（相對於位置偏移 dx、dy 像素），偏移時畫一條線連到文字。"""
    flt = [{"filter": f"isValid(datum[{name!r}])"}]
    layers: list[dict[str, Any]] = []
    if (dx or dy) and len(position) == 2:
        line = {
            **position,
            "x2": {"field": position["x"]["field"]},
            "y2": {"field": position["y"]["field"]},
        }
        for channel in ("x2", "y2"):
            if "bandPosition" in position[channel[0]]:
                line[channel]["bandPosition"] = 0.5
        layers.append(
            {
                "name": f"{name}_line",
                "transform": flt,
                "mark": {
                    "type": "rule",
                    "color": ANNOTATION_LINE,
                    "x2Offset": dx,
                    "y2Offset": dy,
                    "tooltip": False,
                },
                "encoding": line,
            }
        )
    layers.append(
        {
            "name": name,
            "transform": flt,
            "mark": {
                "type": "text",
                "align": "left" if dx >= 0 else "right",
                "baseline": "middle",
                "dx": dx + (4 if dx >= 0 else -4),
                "dy": dy,
                "color": ANNOTATION_COLOR,
                "fontSize": 11,
                "lineBreak": "\n",
                "tooltip": False,
            },
            "encoding": {**position, "text": {"field": name}},
        }
    )
    return layers


class _Placement:
    """參考線圖層在另一軸上的位置：離散軸且有多個範圍時每個標記位置各畫一段（同一範圍連成一條），
    否則橫跨整個窗格（多個範圍時依範圍分組）。"""

    def __init__(
        self, encoding: dict[str, Any], position: str, group_keys: list[str], groups: int
    ) -> None:
        self.position = position
        self.other = "y" if position == "x" else "x"
        self.other_enc = encoding.get(self.other)
        self.title = encoding[position].get("title")
        self.kind = encoding[position].get("type", "quantitative")
        self.scale = encoding[position].get("scale")
        self.group_keys = group_keys
        self.discrete = isinstance(self.other_enc, dict) and self.other_enc.get("type") in (
            "nominal",
            "ordinal",
        )
        self.per_mark = groups > 1 and self.discrete

    def field(self, name: str) -> dict[str, Any]:
        out: dict[str, Any] = {"field": name, "type": self.kind, "title": self.title}
        if self.scale is not None:
            out["scale"] = self.scale
        return out

    def summary(self, fields: list[str], by_position: bool) -> list[dict[str, Any]]:
        """每個範圍一列（by_position 時離散軸的每個位置一列）：各值取最大（同一範圍的值都相同）。"""
        keys = list(self.group_keys)
        if by_position and isinstance(self.other_enc, dict) and self.other_enc["field"] not in keys:
            keys.append(self.other_enc["field"])
        return [
            {
                "aggregate": [{"op": "max", "field": f, "as": f} for f in fields],
                "groupby": keys,
            }
        ]

    def line(self, name: str, style: dict[str, Any]) -> dict[str, Any]:
        if self.per_mark:
            # 離散軸：每個標記位置畫一段短線，同一範圍的短線連成該範圍的參考線
            return {
                "name": name,
                "transform": [{"calculate": "-1", "as": ROW}],
                "mark": {
                    "type": "tick",
                    "thickness": style.get("strokeWidth", REF_WIDTH),
                    "color": style["color"],
                },
                "encoding": {self.position: self.field(name), self.other: self.other_enc},
            }
        enc: dict[str, Any] = {self.position: {**self.field(name), "aggregate": "max"}}
        if self.group_keys:
            enc["detail"] = [{"field": k, "type": "nominal"} for k in self.group_keys]
        return {"name": name, "mark": {"type": "rule", **style}, "encoding": enc}

    def fill(self, name: str, low: str, high: str, color: str) -> dict[str, Any]:
        """low 到 high 之間的底色；high 為 start、end 時延伸到窗格的下緣（左緣）、上緣（右緣）。"""
        fields = [f for f in (low, high) if f not in ("start", "end")]
        edge: dict[str, dict[str, Any]] = {
            "start": {"value": "height" if self.position == "y" else 0},
            "end": {"value": 0 if self.position == "y" else "width"},
        }
        enc: dict[str, Any] = {
            self.position: self.field(low),
            f"{self.position}2": edge.get(high, {"field": high}),
        }
        if self.per_mark and isinstance(self.other_enc, dict):
            enc[self.other] = self.other_enc
        return {
            "name": name,
            "transform": self.summary(fields, self.per_mark),
            "mark": {"type": "rect", "color": color, "tooltip": False},
            "encoding": enc,
        }

    def label(self, name: str, field_name: str, text: str) -> dict[str, Any]:
        """標籤：與 Tableau 相同寫在窗格的左側、線的上方（縱向的參考線寫在上方、線的右側）。"""
        place: dict[str, Any] = (
            {"y": {"value": 0}, "align": "left", "dx": 3, "dy": 0}
            if self.position == "x"
            else {"x": {"value": 0}, "align": "left", "dx": 3, "dy": -3}
        )
        return {
            "name": name,
            "mark": {
                "type": "text",
                "color": REF_LABEL_COLOR,
                "align": place.pop("align"),
                "baseline": "bottom" if self.position == "y" else "top",
                "dx": place.pop("dx"),
                "dy": place.pop("dy"),
            },
            "encoding": {
                self.position: {**self.field(field_name), "aggregate": "max"},
                **place,
                "text": {"value": text},
            },
        }

    def box(self, name: str, low: str, high: str, color: str) -> dict[str, Any]:
        """盒鬚圖的盒子：離散軸時占每個位置的一半寬，否則畫在窗格中間。"""
        enc: dict[str, Any] = {self.position: self.field(low), f"{self.position}2": {"field": high}}
        mark: dict[str, Any] = {"type": "bar", "color": color, "tooltip": False}
        if self.discrete and isinstance(self.other_enc, dict):
            enc[self.other] = self.other_enc
            mark["width" if self.other == "x" else "height"] = {"band": 0.5}
        else:
            size = "width" if self.other == "x" else "height"
            enc[self.other] = {"value": {"expr": f"{size} * 0.4"}}
            enc[f"{self.other}2"] = {"value": {"expr": f"{size} * 0.6"}}
            mark["type"] = "rect"
        return {
            "name": name,
            "transform": self.summary([low, high], self.discrete),
            "mark": mark,
            "encoding": enc,
        }

    def whisker(self, name: str, low: str, high: str) -> dict[str, Any]:
        enc: dict[str, Any] = {self.position: self.field(low), f"{self.position}2": {"field": high}}
        if self.discrete and isinstance(self.other_enc, dict):
            enc[self.other] = {**self.other_enc, "bandPosition": 0.5}
        else:
            size = "width" if self.other == "x" else "height"
            enc[self.other] = {"value": {"expr": f"{size} / 2"}}
        return {
            "name": name,
            "transform": self.summary([low, high], self.discrete),
            "mark": {"type": "rule", "color": WHISKER_COLOR, "tooltip": False},
            "encoding": enc,
        }

    def cap(self, name: str, field_name: str) -> dict[str, Any]:
        """鬚的兩端：短的橫線（離散軸時占位置寬的四分之一）。"""
        enc: dict[str, Any] = {self.position: self.field(field_name)}
        if self.discrete and isinstance(self.other_enc, dict):
            enc[self.other] = {**self.other_enc, "bandPosition": 0.375}
            enc[f"{self.other}2"] = {"field": self.other_enc["field"], "bandPosition": 0.625}
        else:
            size = "width" if self.other == "x" else "height"
            enc[self.other] = {"value": {"expr": f"{size} * 0.45"}}
            enc[f"{self.other}2"] = {"value": {"expr": f"{size} * 0.55"}}
        return {
            "name": name,
            "transform": self.summary([field_name], self.discrete),
            "mark": {"type": "rule", "color": WHISKER_COLOR, "tooltip": False},
            "encoding": enc,
        }


def _box_plot(
    n: int,
    line: ReferenceLine,
    field_name: str,
    groups: dict[tuple[Any, ...], list[dict[str, Any]]],
    place: _Placement,
    behind: list[dict[str, Any]],
    body: dict[str, Any],
) -> None:
    """盒鬚圖：每個範圍的四分位數（線性內插）畫成兩種灰色的盒子（中位數上下），鬚延伸到 1.5 倍四分位距內
    最遠的值（或最小、最大值），畫在標記之下；設定隱藏底下的標記時只顯示鬚以外的標記（離群值）。"""
    names = {k: f"{REF}{n}_{k}" for k in ("q1", "median", "q3", "low", "high")}
    outlier = f"{REF}{n}_out"
    for members in groups.values():
        numbers = sorted(float(v) for m in members if _is_number(v := m.get(field_name)))
        if not numbers:
            continue
        q1, median, q3 = (_percentile(numbers, p) for p in (25, 50, 75))
        if line.whiskers == "minmax":
            low, high = numbers[0], numbers[-1]
        else:
            reach = 1.5 * (q3 - q1)
            low = min(v for v in numbers if v >= q1 - reach)
            high = max(v for v in numbers if v <= q3 + reach)
        stats = {"q1": q1, "median": median, "q3": q3, "low": low, "high": high}
        for m in members:
            for key, name in names.items():
                m[name] = stats[key]
            value = m.get(field_name)
            m[outlier] = _is_number(value) and (value < low or value > high)
    behind += [
        place.whisker(f"{REF}{n}_whisker_low", names["low"], names["q1"]),
        place.whisker(f"{REF}{n}_whisker_high", names["q3"], names["high"]),
        place.box(f"{REF}{n}_box_low", names["q1"], names["median"], BOX_COLORS[0]),
        place.box(f"{REF}{n}_box_high", names["median"], names["q3"], BOX_COLORS[1]),
        place.cap(f"{REF}{n}_cap_low", names["low"]),
        place.cap(f"{REF}{n}_cap_high", names["high"]),
    ]
    encoding = body.get("encoding")
    if line.hide_marks and isinstance(encoding, dict):
        encoding["opacity"] = {
            "condition": {"test": f"datum[{json.dumps(outlier)}]", "value": 1},
            "value": 0,
        }


def _line_style(line: ReferenceLine) -> dict[str, Any] | None:
    """線的格式；不畫線時為 None。預設：參考線畫（2 像素的灰色實線），分布與參考區間不畫。"""
    visible = line.line_visible
    if visible is None:
        visible = line.kind == "line" and line.paired is None
    if not visible:
        return None
    style: dict[str, Any] = {
        "color": _css_color(line.line_color) if line.line_color else REF_COLOR,
        "strokeWidth": line.line_width or REF_WIDTH,
    }
    if line.line_pattern == "dashed":
        style["strokeDash"] = [6, 3]
    elif line.line_pattern == "dotted":
        width = style["strokeWidth"]
        style["strokeDash"] = [width, width]
    return style


def _shade(level: int, levels: int) -> str:
    """分布的第 level 層底色（1 為最內側）：由淺到深內插 SHADES。"""
    if levels <= 1:
        return SHADES[0]
    t = (level - 1) / (levels - 1)
    low, high = (int(c[1:], 16) for c in SHADES)
    channels = []
    for shift in (16, 8, 0):
        a, b = (low >> shift) & 255, (high >> shift) & 255
        channels.append(round(a + (b - a) * t))
    return "#" + "".join(f"{c:02x}" for c in channels)


def _percentile(ordered: list[float], percent: float) -> float:
    """線性內插的百分位數（與 Tableau 的 PERCENTILE 相同）；ordered 已排序且非空。"""
    position = (len(ordered) - 1) * percent / 100
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _scope_keys(builder: _Builder) -> dict[str, list[str]]:
    """參考線範圍 → 分組用的資料欄名。"""
    pane: list[FieldUse] = []
    cell: list[FieldUse] = []
    for shelf in (builder.rows, builder.columns):
        discrete = [u for u in shelf if not builder.is_measure(u) and not u.continuous]
        cell += discrete
        innermost_discrete = bool(shelf) and bool(discrete) and shelf[-1] is discrete[-1]
        pane += discrete[:-1] if innermost_discrete else discrete

    def names(uses: list[FieldUse]) -> list[str]:
        return [builder.ch(u).name for u in uses if u.field not in SPECIAL_FIELDS]

    return {"table": [], "pane": names(pane), "cell": names(cell)}


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _middle_color(scale: Any) -> str:
    """連續色彩的比例尺中間的顏色（沒有時為 Tableau 預設的標記顏色）。"""
    colors = scale.get("range") if isinstance(scale, dict) else None
    if isinstance(colors, list) and colors and all(isinstance(c, str) for c in colors):
        return colors[len(colors) // 2]
    return DEFAULT_MARK_COLOR


def _hashable(value: Any) -> Any:
    """分組的鍵：清單等不能雜湊的值轉成文字。"""
    try:
        hash(value)
    except TypeError:
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _sort_key(value: Any) -> tuple[int, Any]:
    """依值排序（數字、日期字串）：Null 在最後，不同型別的值依文字比較。"""
    if value is None:
        return (2, "")
    if _is_number(value):
        return (0, float(value))
    return (1, str(value))


def _reference_source(
    line: ReferenceLine, channels: dict[str, Channel], params: dict[str, Scalar], temporal: bool
) -> str | _Constant | None:
    """參考線的值來源：資料欄名（以標記的值計算），或常數、參數的值（時間軸上為 UTC 毫秒）。"""
    if isinstance(line.value, ParamRef):
        value = params.get(line.value.parameter)
    elif line.formula == "constant" or line.value is None:
        value = line.constant
    else:
        ch = channels.get(use_key(line.value))
        return ch.name if ch is not None else None
    if _is_number(value):
        return _Constant(float(value))
    number = _axis_value(value, temporal) if temporal else None
    return _Constant(number) if number is not None else None


def _reference_values(
    line: ReferenceLine, numbers: list[float], source: str | _Constant
) -> list[float | None]:
    """一個範圍的參考值（分布為多個值，由小到大的順序與設定相同）。"""
    if isinstance(source, _Constant):
        return [source.value]
    formula = line.formula
    if line.percentages:
        base = _single_value(line, numbers)
        return [None if base is None else base * p / 100 for p in line.percentages]
    if formula == "percentile":
        return [_percentile(sorted(numbers), p) if numbers else None for p in line.percentiles]
    if formula == "stdev":
        enough = len(numbers) >= (2 if line.sample else 1)
        if not enough:
            return [None for _ in line.factors]
        sd = statistics.stdev(numbers) if line.sample else statistics.pstdev(numbers)
        mean = statistics.fmean(numbers)
        return [mean + f * sd for f in line.factors]
    if formula == "quantiles":
        tiles = line.tiles or 4
        ordered = sorted(numbers)
        return [_percentile(ordered, 100 * i / tiles) if ordered else None for i in range(1, tiles)]
    if formula == "confidence":
        # 平均值的信賴區間：平均 ± t × 標準誤（樣本標準差）
        if len(numbers) < 2:
            return [None, None]
        mean = statistics.fmean(numbers)
        margin = (
            t_quantile(0.5 + line.probability / 200, len(numbers) - 1)
            * statistics.stdev(numbers)
            / math.sqrt(len(numbers))
        )
        return [mean - margin, mean + margin]
    if formula == "median_confidence":
        return list(_median_interval(sorted(numbers), line.probability / 100))
    return [_single_value(line, numbers)]


def _single_value(line: ReferenceLine, numbers: list[float]) -> float | None:
    if not numbers:
        return None
    formula = line.formula
    agg = line.value.agg if isinstance(line.value, FieldUse) else None
    if formula == "average":
        return statistics.fmean(numbers)
    if formula == "sum" or (formula == "total" and agg in ("sum", "count")):
        return math.fsum(numbers)
    if formula == "min" or (formula == "total" and agg == "min"):
        return min(numbers)
    if formula == "max" or (formula == "total" and agg == "max"):
        return max(numbers)
    if formula == "median":
        return statistics.median(numbers)
    return None


def _median_interval(ordered: list[float], level: float) -> tuple[float | None, float | None]:
    """中位數的信賴區間（不假設分配：以二項分配找對稱的順序統計量，涵蓋機率至少為 level）。"""
    n = len(ordered)
    if n < 2:
        return None, None
    best: tuple[int, int] | None = None
    for j in range(n // 2, -1, -1):
        k = n - 1 - j
        coverage = math.fsum(math.comb(n, i) for i in range(j + 1, k + 1)) / 2**n
        if coverage >= level:
            best = (j, k)
            break
    if best is None:
        return ordered[0], ordered[-1]
    return ordered[best[0]], ordered[best[1]]


def _reference_labels(
    line: ReferenceLine, axis: Channel, values: list[float | None], temporal: bool
) -> list[tuple[int, str]]:
    """標籤（只有一個範圍時）：參考線一個、分布每個值一個；回傳 (第幾個值, 文字)。自動的標籤：
    參考線為值，分布為各值的名稱（例如 2 Standard Deviations，與 Tableau 的截圖相同）。"""
    if line.label == "none" or not values:
        return []
    computation = COMPUTATION_NAMES[line.formula]
    labels: list[tuple[int, str]] = []
    for i, value in enumerate(values):
        if value is None:
            continue
        if temporal:
            text = datetime.fromtimestamp(value / 1000, UTC).date().isoformat()
        else:
            text = format_value(value, "real", line.label_format or axis.format, None)
        part = _distribution_name(line, i) if line.kind == "distribution" else computation
        if line.label == "computation" or (
            line.label == "automatic" and line.kind == "distribution"
        ):
            labels.append((i, part))
        elif line.label == "custom":
            custom = (line.label_text or "").replace("<Value>", text).replace("<Computation>", part)
            labels.append((i, custom.replace("<Scope>", SCOPE_NAMES[line.scope])))
        else:
            labels.append((i, text))
    return labels


def _distribution_name(line: ReferenceLine, i: int) -> str:
    """分布中第 i 個值的名稱（自動標籤）：例如 2 Standard Deviations、25th Percentile。"""
    if line.percentages:
        return f"{line.percentages[i]:g}% of {COMPUTATION_NAMES[line.formula]}"
    if line.formula == "stdev":
        return f"{line.factors[i]:g} Standard Deviations"
    if line.formula == "percentile":
        p = line.percentiles[i]
        suffix = "th" if 10 <= p % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(int(p) % 10, "th")
        return f"{p:g}{suffix} Percentile"
    if line.formula == "quantiles":
        return f"Quantile {i + 1}"
    return ("Lower " if i == 0 else "Upper ") + COMPUTATION_NAMES[line.formula]


def _table_calc_type(ws: Worksheet, use: FieldUse) -> str | None:
    if use.table_calc is None:
        return None
    tc = next((t for t in ws.table_calcs if t.id == use.table_calc), None)
    if tc is not None and tc.secondary is not None:
        return tc.secondary.type  # 次要計算決定顯示的格式（例如累計之後的合計百分比以百分比顯示）
    return tc.type if tc is not None else None


def _fold(
    values: list[dict[str, Any]], measures: list[Channel], names: dict[str, str]
) -> list[dict[str, Any]]:
    """寬表 → 長表：每個量值一列，加上 Measure Names（名稱、順序）與 Measure Values（值、文字）。
    names：Measure Names 的別名（量值的欄位鍵 → 顯示名稱）。"""
    long: list[dict[str, Any]] = []
    for record in values:
        for i, ch in enumerate(measures):
            item = dict(record)
            item["__mn"] = names.get(use_key(ch.use), ch.title)
            item["__mn_i"] = i
            item["__mv"] = record[ch.name]
            item["__mv_label"] = record[ch.label]
            long.append(item)
    return long


DEFAULT_MARK_COLOR = PALETTES[DEFAULT_REGULAR].colors[0]
"""沒有顏色欄位時標記的預設顏色（Tableau 10 的第一個顏色）。"""
AUTOMATIC_DISCRETE = ColorPalette(kind="discrete")
"""離散欄位沒有設定顏色時：依 Tableau 10 的順序。"""
BORDERED = frozenset({"bar", "rect", "arc", "circle", "square", "point", "tick"})
HALOED = frozenset({"circle", "square", "point"})
"""光暈作用的標記（Vega-Lite 的類型）。"""
LABEL_WRAP_WIDTH = 120
"""標籤換行（換行：開）時每一行的寬度上限（像素）；Tableau 依標記與儲存格的大小決定，這裡是固定值。"""
"""可以畫框線的標記（Vega-Lite 的類型）；面積的框線另以折線疊在上緣。"""


def _mark_style(mark: dict[str, Any], marks: Marks) -> dict[str, Any]:
    """標記卡上設定的顏色（沒有顏色欄位時；沒有設定時為 Tableau 的預設色，文字除外）、不透明度與框線。"""
    if marks.color is None and marks.mark_color is not None:
        mark["color"] = marks.mark_color
    elif marks.color is None and mark["type"] != "text":
        mark["color"] = DEFAULT_MARK_COLOR
    if marks.opacity is not None:
        mark["opacity"] = marks.opacity
    if marks.stroke is not None:
        if mark["type"] == "area":
            mark["line"] = {"color": marks.stroke}
        elif mark["type"] in BORDERED or (mark["type"] == "line" and mark.get("filled")):
            mark["stroke"] = marks.stroke
            mark["strokeWidth"] = 1
    elif marks.halo and mark["type"] in HALOED:
        # 光暈：圓點、形狀與方塊外圍的白色細邊
        mark["stroke"] = "#ffffff"
        mark["strokeWidth"] = 1
    return mark


def _header_expr(shown: list[int], levels: int, multiline: bool) -> str | None:
    """離散標題的文字：只顯示 shown 的層級（多層時資料中的值為「外層｜內層」）；multiline 時分行。
    全部顯示且不分行時為 None（直接顯示值）。"""
    if len(shown) == levels and not multiline:
        return None
    if len(shown) == levels:
        return f"split(datum.label, '{SEPARATOR}')"
    parts = [f"(split(datum.label, '{SEPARATOR}')[{i}] || '')" for i in shown]
    if multiline:
        return "[" + ", ".join(parts) + "]"
    return f" + '{SEPARATOR}' + ".join(parts)


def _config(ws: Worksheet) -> dict[str, Any]:
    """Vega-Lite 的 config：工作表的字型（Tableau 的預設為 9pt）與文字顏色（沒有設定時與 Tableau 相同，
    刻度與標題成員為灰色、標題較深）套用到座標軸、圖例、分面的標題與文字標記。"""
    fonts = ws.fonts
    base = resolve(fonts.get("worksheet"))
    label = resolve(fonts.get("worksheet"), fonts.get("label"))
    legend = resolve(fonts.get("worksheet"), fonts.get("legend"))
    legend_title = resolve(fonts.get("worksheet"), fonts.get("legend-title"), weight=SEMIBOLD)
    cell = resolve(fonts.get("worksheet"), fonts.get("cell"))
    label_color = _css_color(ws.text_color) if ws.text_color is not None else LABEL_COLOR
    title_color = _css_color(ws.text_color) if ws.text_color is not None else TITLE_COLOR
    colors = {"labelColor": label_color, "titleColor": title_color}
    return {
        "font": base.family,
        "view": {"stroke": None},
        "axis": {
            "labelLimit": 160,
            **label.axis("label"),
            **base.axis("title"),
            **colors,
            "gridColor": GRID_COLOR,
            "domainColor": RULER_COLOR,
            "tickColor": RULER_COLOR,
        },
        "legend": {**legend.axis("label"), **legend_title.axis("title"), **colors},
        "header": {**label.axis("label"), **base.axis("title"), **colors},
        "text": {"font": cell.family, "fontSize": cell.size},
    }


GRID_COLOR = "#f0f0f0"
"""格線的顏色（Tableau 的預設，依畫面量測）。"""
RULER_COLOR = "#f4f4f4"
"""座標軸線與刻度的顏色（Tableau 的預設，依畫面量測，幾乎看不出來）。"""
DASHES = {"solid": [], "dashed": [4, 2], "dotted": [2, 1]}


def _zero_line(axis: dict[str, Any], zero: ZeroLine, grid: bool) -> None:
    """零線：以座標軸在 0 的格線畫出（顏色、線寬與樣式依設定；格線關閉時只畫這一條）。範圍不含 0 時
    沒有 0 的刻度，與 Tableau 相同不畫。"""
    test = "datum.value === 0"
    axis["grid"] = True
    axis["gridColor"] = {
        "condition": {"test": test, "value": _css_color(zero.color)},
        "value": GRID_COLOR,
    }
    axis["gridDash"] = {"condition": {"test": test, "value": DASHES[zero.pattern]}, "value": []}
    axis["gridWidth"] = {"condition": {"test": test, "value": zero.width}, "value": 1}
    if not grid:
        axis["gridOpacity"] = {"condition": {"test": test, "value": 1}, "value": 0}


def _title_font(ws: Worksheet) -> dict[str, Any]:
    """工作表標題的字型（檢視器）：標題第一段文字的格式、工作表的 title 設定，沒有時與 Tableau 相同為 15pt；
    字型名稱沿用工作表的預設字型。另附段落的對齊。"""
    style = next((r.style for r in ws.title if r.style is not None), None)
    sheet = ws.fonts.get("worksheet")
    layers: list[FontStyle | None] = [
        FontStyle(family=sheet.family) if sheet is not None and sheet.family else None,
        ws.fonts.get("title"),
    ]
    if style is not None:
        layers.append(
            FontStyle(family=style.font, size=style.size, bold=style.bold, italic=style.italic)
        )
    font = resolve(*layers, size=TITLE_SIZE).css()
    if style is not None and style.align is not None:
        font["align"] = style.align
    return font


def _label_color(ws: Worksheet) -> str:
    """標題成員的顏色：工作表的文字顏色，沒有設定時與 Tableau 相同為灰色。"""
    return _css_color(ws.text_color) if ws.text_color is not None else LABEL_COLOR


def _title_color(ws: Worksheet) -> str:
    """欄位標籤與標題的顏色：工作表的文字顏色，沒有設定時與 Tableau 相同為深灰色。"""
    return _css_color(ws.text_color) if ws.text_color is not None else TITLE_COLOR


def _css_color(color: str) -> str:
    """IR 的顏色（#rrggbb 或 #rrggbbaa）→ CSS；完全透明時為 transparent。"""
    return "transparent" if len(color) == 9 and color.endswith("00") else color


def _fixed_range(encoding: Any) -> bool:
    scale = encoding.get("scale") if isinstance(encoding, dict) else None
    return isinstance(scale, dict) and any(k in scale for k in ("domain", "domainMin", "domainMax"))


def _axis_value(value: Scalar, temporal: bool) -> float | None:
    """固定範圍的值：數值；時間軸為 UTC 的毫秒數（日期的 ISO 8601 字串）。"""
    if temporal and isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp() * 1000
    if not temporal and _is_number(value):
        return float(value)
    return None


def _take_pane(remaining: list[Pane], use: FieldUse, axis: str) -> Pane | None:
    for pane in remaining:
        if pane.axis == axis and pane.measure == use:
            remaining.remove(pane)
            return pane
    return None


def _is_ring(marks: Marks) -> bool:
    """疊在圓餅上的標記卡畫成另一層圓餅（有角度與顏色），否則畫成中間的洞。"""
    return marks.type == "pie" and marks.angle is not None and marks.color is not None


def _arc_marks(node: dict[str, Any]) -> list[dict[str, Any]]:
    """規格中所有圓餅（arc）的 mark（含分面中的）。"""
    found: list[dict[str, Any]] = []
    mark = node.get("mark")
    if isinstance(mark, dict) and mark.get("type") == "arc":
        found.append(mark)
    for child_node in node.get("layer", []):
        found += _arc_marks(child_node)
    if isinstance(node.get("spec"), dict):
        found += _arc_marks(node["spec"])
    return found


def _pie_marks(ws: Worksheet) -> Marks | None:
    """圓餅的標記卡：共用標記卡或第一張個別標記卡是圓餅時。"""
    if ws.panes:
        first = ws.panes[0].marks
        if first.type == "pie":
            return first
        return None
    return ws.marks if ws.marks.type == "pie" else None


def _template(text: str) -> str:
    """{c0_label}｜{c1_label} → Vega 運算式。"""
    parts: list[str] = []
    rest = text
    while rest:
        start = rest.find("{")
        if start < 0:
            parts.append(repr(rest))
            break
        if start:
            parts.append(repr(rest[:start]))
        end = rest.index("}", start)
        parts.append(f"datum['{rest[start + 1 : end]}']")
        rest = rest[end + 1 :]
    return " + ".join(parts) if parts else "''"


PIE_SIZE = 1.0
"""圓餅沒有設定大小時的大小（直徑為區塊較短邊的一半；依公開報表推定，使用者多半調大）。"""


MAX_PANES = 200
"""分面（小倍數）最多的窗格數；超過時以單一窗格呈現。"""


def _small_multiples(
    builder: _Builder,
    body: dict[str, Any],
    values: list[dict[str, Any]],
    rows_dims: list[FieldUse],
    cols_dims: list[FieldUse],
    rows_meas: list[FieldUse],
    cols_meas: list[FieldUse],
) -> dict[str, Any]:
    """欄列架上與量值的座標軸同一架的離散維度把圖分成窗格（Tableau 的窗格；例如列為 Region、SUM(Sales)，
    欄為連續的月份時，每個地區一張折線圖由上往下排）：每個窗格篩選自己的資料，共用刻度；列的窗格只有
    最下面一張畫橫軸的刻度，欄的窗格只有最左邊一張畫縱軸的刻度。窗格的標題寫在左側（列）與上方（欄）。"""
    row_uses = (
        rows_dims if rows_meas and rows_dims and all(not u.continuous for u in rows_dims) else []
    )
    col_uses = (
        cols_dims if cols_meas and cols_dims and all(not u.continuous for u in cols_dims) else []
    )
    if not (rows_meas and cols_meas):
        # 只有一個量值的座標軸：另一架的維度是座標（標題），不分窗格
        row_uses = row_uses if rows_meas else []
        col_uses = col_uses if cols_meas else []
    row_chs = [builder.ch(u) for u in row_uses if u.field not in SPECIAL_FIELDS]
    col_chs = [builder.ch(u) for u in col_uses if u.field not in SPECIAL_FIELDS]
    if len(row_chs) != len(row_uses) or len(col_chs) != len(col_uses) or not (row_chs or col_chs):
        return body

    def panes(
        chs: list[Channel], ranked: bool = False
    ) -> tuple[list[tuple[Any, ...]], dict[tuple[Any, ...], str]]:
        """窗格依資料的順序；欄的窗格依欄維度自己的排序（查詢算出的名次，見 _column_order）。"""
        keys: list[tuple[Any, ...]] = []
        labels: dict[tuple[Any, ...], str] = {}
        records = values
        if ranked and any(v.get(ORDER) is not None for v in values):
            records = sorted(values, key=lambda v: (v.get(ORDER) is None, v.get(ORDER) or 0))
        for record in records:
            key = tuple(record.get(c.name) for c in chs)
            if key not in labels:
                keys.append(key)
                labels[key] = " / ".join(str(record.get(c.label, "")) for c in chs)
        return keys, labels

    row_keys, row_labels = panes(row_chs) if row_chs else ([()], {(): ""})
    col_keys, col_labels = panes(col_chs, ranked=True) if col_chs else ([()], {(): ""})
    if len(row_keys) * len(col_keys) > MAX_PANES:
        builder.notes.append(f"窗格超過 {MAX_PANES} 個，以單一窗格呈現")
        return body
    row_shown, _, row_color = builder.header(row_uses, "rows") if row_uses else ([], None, None)
    col_shown, _, col_color = builder.header(col_uses, "columns") if col_uses else ([], None, None)
    font = builder.font("label")

    def free(measures: list[FieldUse], shelf: Literal["rows", "columns"]) -> bool:
        setting = builder.axis_of(measures[0], shelf) if measures else None
        return setting is not None and setting.independent

    y_free = bool(row_chs) and free(rows_meas, "rows")
    x_free = bool(col_chs) and free(cols_meas, "columns")

    def pane(
        row: tuple[Any, ...], col: tuple[Any, ...], first_col: bool, first_row: bool, last_row: bool
    ) -> dict[str, Any]:
        chart = copy.deepcopy(body)
        pairs = [(c.name, v) for c, v in zip(row_chs, row, strict=True)]
        pairs += [(c.name, v) for c, v in zip(col_chs, col, strict=True)]
        chart["transform"] = [{"filter": _all_equal(pairs)}, *chart.get("transform", [])]
        for layer in _own_data_layers(chart):
            present = [(name, v) for name, v in pairs if name in layer["data"]["values"][0]]
            if present:
                layer["transform"] = [{"filter": _all_equal(present)}, *layer.get("transform", [])]
        grid = bool(row_chs) and bool(col_chs)
        # 欄與列都分窗格、橫軸各窗格獨立時，每一格的範圍不同：每一列都畫橫軸
        if not last_row and not (x_free and grid):
            _hide_axis(chart, "x")
        if not first_col:
            _hide_axis(chart, "y")
        if row_chs and first_col and row_shown:
            if grid:
                # 欄與列都分窗格：列的標題寫在縱軸標題的上一行（圖的標題留給欄）
                _prefix_axis_title(chart, "y", row_labels[row])
            else:
                chart["title"] = {
                    "text": row_labels[row],
                    "orient": "left",
                    "angle": 0,
                    "anchor": "middle",
                    "align": "right",
                    "baseline": "middle",
                    "fontWeight": font.weight,
                    "fontSize": font.size,
                    "font": font.family,
                    "color": row_color or builder.text_color(),
                    "offset": 6,
                }
        if col_chs and first_row and col_shown:
            chart["title"] = {
                "text": col_labels[col],
                "orient": "top",
                "anchor": "middle",
                "fontWeight": font.weight,
                "fontSize": font.size,
                "font": font.family,
                "color": col_color or builder.text_color(),
            }
        return chart

    shared = {"x": "shared", "y": "shared", "color": "shared", "size": "shared"}
    # 各窗格獨立的座標軸範圍：列架上的量值在每一列的窗格各自決定範圍（同一列的各欄仍共用），
    # 欄架上的量值在每一欄
    across_rows = {"scale": {**shared, "y": "independent" if y_free else "shared"}}
    across_cols = {"scale": {**shared, "x": "independent" if x_free else "shared"}}
    grid = [
        [pane(row, col, j == 0, i == 0, i == len(row_keys) - 1) for j, col in enumerate(col_keys)]
        for i, row in enumerate(row_keys)
    ]
    if len(col_keys) == 1:
        return {"vconcat": [line[0] for line in grid], "resolve": across_rows, "spacing": 4}
    if len(row_keys) == 1:
        return {"hconcat": grid[0], "resolve": across_cols, "spacing": 4}
    return {
        "vconcat": [{"hconcat": line, "resolve": across_cols, "spacing": 4} for line in grid],
        "resolve": across_rows,
        "spacing": 4,
    }


def _all_equal(pairs: list[tuple[str, Any]]) -> str:
    """Vega 運算式：資料欄依序等於各值。"""
    return " && ".join(
        f"datum[{json.dumps(name)}] === {_js_literal(value)}" for name, value in pairs
    )


def _js_literal(value: Any) -> str:
    """資料列中的值在規格（JSON）中的寫法：日期為 ISO 8601 字串。"""
    if isinstance(value, datetime | date):
        return json.dumps(value.isoformat())
    if isinstance(value, Decimal):
        return json.dumps(float(value))
    return json.dumps(value, ensure_ascii=False, default=str)


def _own_data_layers(node: dict[str, Any]) -> list[dict[str, Any]]:
    """有自己資料的圖層（趨勢線等），且資料不是空的。"""
    found: list[dict[str, Any]] = []
    for child in node.get("layer", []):
        values = (
            child.get("data", {}).get("values") if isinstance(child.get("data"), dict) else None
        )
        if isinstance(values, list) and values and isinstance(values[0], dict):
            found.append(child)
        found.extend(_own_data_layers(child))
    return found


def _prefix_axis_title(node: dict[str, Any], channel: str, text: str) -> None:
    """座標軸標題的上一行加上文字（窗格的標題）。"""
    encoding = node.get("encoding")
    if isinstance(encoding, dict) and isinstance(encoding.get(channel), dict):
        target = encoding[channel]
        title = target.get("title")
        target["title"] = [text, *([str(title)] if title else [])]
        return
    for child in node.get("layer", []):
        if not str(child.get("name", "")).startswith((REF, DECO)):
            _prefix_axis_title(child, channel, text)
            return


def _hide_axis(node: dict[str, Any], channel: str) -> None:
    """不畫這個方向座標軸的刻度文字與標題（保留格線）：窗格之間共用的座標軸只畫一次。"""
    encoding = node.get("encoding")
    if isinstance(encoding, dict) and isinstance(encoding.get(channel), dict):
        target = encoding[channel]
        if target.get("axis") is not None or target.get("type") in (
            "quantitative",
            "temporal",
            "ordinal",
            "nominal",
        ):
            axis = dict(target.get("axis") or {})
            axis.update(labels=False, title=None, ticks=False)
            target["axis"] = axis
    for child in node.get("layer", []):
        _hide_axis(child, channel)


def _first_fold_only(body: dict[str, Any]) -> None:
    """Measure Values 的長表中只取每列的第一份（各標記卡另外查詢的資料沒有 __mn_i，全部保留）；
    工具提示不列 Measure Names／Values。"""
    body.setdefault("transform", []).insert(0, {"filter": "!(datum.__mn_i > 0)"})

    def visit(node: dict[str, Any]) -> None:
        tooltip = node.get("encoding", {}).get("tooltip")
        if isinstance(tooltip, list):
            node["encoding"]["tooltip"] = [
                t for t in tooltip if t.get("field") not in ("__mn", "__mv_label")
            ]
        for child in node.get("layer", []):
            visit(child)

    visit(body)


def _names_scale(body: dict[str, Any], scale: dict[str, Any]) -> None:
    """以 Measure Names 上色的圖層改用共用的比例尺（見 folded_names_scale）。"""
    color = body.get("encoding", {}).get("color")
    if isinstance(color, dict) and color.get("field") == "__mn":
        color["scale"] = scale
    for child in body.get("layer", []):
        _names_scale(child, scale)


def _color_datum(
    body: dict[str, Any], title: str, scale: dict[str, Any] | None, replace: bool = False
) -> None:
    """量值的圖層以固定的顏色（Measure Names 中這個量值的顏色）呈現，與其他量值共用圖例。
    replace：取代既有的顏色（Measure Values 的長表中的 Measure Names）。scale 為 None 時沿用其他圖層
    以欄位上色的比例尺（圖層與並排的圖共用色彩的比例尺）。"""

    def visit(node: dict[str, Any]) -> None:
        mark = node.get("mark")
        kind = mark.get("type") if isinstance(mark, dict) else mark
        name = node.get("name")
        decoration = isinstance(name, str) and name.startswith(("__ref", "__deco"))
        if mark is not None and kind != "text" and not decoration:
            encoding = node.setdefault("encoding", {})
            if replace and isinstance(encoding.get("color"), dict) and "field" in encoding["color"]:
                del encoding["color"]
            if "color" not in encoding:
                if scale is None:
                    encoding["color"] = {
                        "datum": title,
                        "type": "nominal",
                        "title": MEASURE_NAMES_TITLE,
                    }
                else:
                    # 常數的 datum 不能帶比例尺：以常數欄位上色
                    node.setdefault("transform", []).append(
                        {
                            "calculate": json.dumps(title, ensure_ascii=False),
                            "as": MEASURE_NAME_FIELD,
                        }
                    )
                    encoding["color"] = {
                        "field": MEASURE_NAME_FIELD,
                        "type": "nominal",
                        "title": MEASURE_NAMES_TITLE,
                        "scale": scale,
                    }
        for child in node.get("layer", []):
            visit(child)

    visit(body)


def _pie_fraction(marks: Marks) -> float:
    """圓餅的直徑占區塊（分面的每一格）較短邊的比例：大小 s 時為 s / 2，最多填滿（依 Tableau 的畫面量測：
    大小 1.505 的圓餅直徑約為區塊高度的 76%，甜甜圈內外圈的直徑比與大小的比相同）。"""
    size = marks.mark_size if marks.mark_size is not None else PIE_SIZE
    return round(min(size / 2, 1), 4)


def _label_layer(
    encoding: dict[str, Any],
    vl_type: str,
    field_name: str,
    color: str = TEXT_COLOR,
    font: Font | None = None,
    match: bool = False,
    align: Literal["left", "center", "right"] | None = None,
    radius: float = POINT_DIAMETER / 2,
    extreme: Literal["max", "min"] | None = None,
    stacked: bool = False,
    valign: Literal["top", "center", "bottom"] | None = None,
) -> dict[str, Any]:
    """標籤：長條在尾端（stacked：依標記卡上的維度堆疊時在各段內側），圓點、形狀與方塊在右邊（對齊設為
    置中時在標記中央、靠左時在左邊；與 Tableau 相同），折線與區域在點的上方；折線的最大值（extreme）
    寫在點的右上、最小值在右下（與 Tableau 的畫面相同）。match 時文字的顏色與標記相同（依欄位著色時沿用
    同一個色彩的 encoding）。"""
    stacked = stacked and vl_type == "bar"
    horizontal = encoding.get("x", {}).get("type") == "quantitative"
    font = font or resolve()
    transforms: list[dict[str, Any]] = []
    mark: dict[str, Any] = {"type": "text", **font.mark(), "lineBreak": "\n"}
    layer: dict[str, Any] = {"text": {"field": field_name}, "color": {"value": color}}
    if "size" in encoding:
        layer["size"] = {"value": font.size}  # 大小上的欄位決定標記的大小，不是標籤的字級
    if vl_type in ("cell", "circle", "square", "point"):
        if valign in ("top", "bottom"):
            # 垂直對齊：標籤在標記的上方或下方；水平沒有指定時置中
            gap = radius + 4
            mark.update(
                baseline="bottom" if valign == "top" else "top",
                dy=-gap if valign == "top" else gap,
            )
            if align == "left":
                mark.update(align="right", dx=-gap)
            elif align == "right":
                mark.update(align="left", dx=gap)
            else:
                mark.update(align="center")
        elif align == "center":
            mark.update(align="center", baseline="middle")
        elif align == "left":
            mark.update(align="right", baseline="middle", dx=-(radius + 4))
        else:
            mark.update(align="left", baseline="middle", dx=radius + 4)
    elif vl_type == "bar" and horizontal:
        mark.update(align="right", dx=-3) if stacked else mark.update(align="left", dx=3)
        if stacked:
            layer["x"] = {**encoding["x"], "stack": "zero"}
        elif align == "left":
            # 水平對齊靠左：標籤在長條的起點（0）內側；置中：在長條的中間
            layer["x"] = {"datum": 0, "type": "quantitative"}
        elif align == "center":
            mark.update(align="center", dx=0)
            layer["x"] = _bar_middle(encoding["x"], transforms)
    elif vl_type == "bar":
        mark.update(baseline="top", dy=3) if stacked else mark.update(baseline="bottom", dy=-3)
        if stacked:
            layer["y"] = {**encoding["y"], "stack": "zero"}
        elif valign == "bottom":
            layer["y"] = {"datum": 0, "type": "quantitative"}
        elif valign == "center":
            mark.update(baseline="middle", dy=0)
            layer["y"] = _bar_middle(encoding["y"], transforms)
    elif extreme == "max":
        mark.update(align="left", baseline="bottom", dx=3, dy=-2)
    elif extreme == "min":
        mark.update(align="left", baseline="top", dx=3, dy=2)
    else:
        # 折線與區域：預設在點的上方；垂直對齊可以改到中間或下方，水平對齊改到左邊或右邊
        if valign == "bottom":
            mark.update(baseline="top", dy=4)
        elif valign == "center":
            mark.update(baseline="middle")
        else:
            mark.update(baseline="bottom", dy=-4)
        if align == "left":
            mark.update(align="right", dx=-4)
        elif align == "right":
            mark.update(align="left", dx=4)
    if match and "field" in encoding.get("color", {}):
        layer["color"] = encoding["color"]
    elif stacked:
        # 文字改成白色，但仍以顏色（詳細資料）的欄位分組，才能與長條同樣堆疊
        layer["color"] = {"value": "#ffffff"}
        if "field" in encoding.get("color", {}):
            layer["detail"] = {
                "field": encoding["color"]["field"],
                "type": encoding["color"]["type"],
            }
    if transforms:
        return {"transform": transforms, "mark": mark, "encoding": layer}
    return {"mark": mark, "encoding": layer}


BAR_MIDDLE = "__bar_mid"


def _bar_middle(channel: dict[str, Any], transforms: list[dict[str, Any]]) -> dict[str, Any]:
    """長條中間的位置（標籤置中）：量值的一半；沿用座標軸的刻度與標題。"""
    transforms.append({"calculate": f"datum[{json.dumps(channel['field'])}] / 2", "as": BAR_MIDDLE})
    return {**channel, "field": BAR_MIDDLE, "stack": None}


def _rotate_label(mark: dict[str, Any], direction: Literal["up", "down"]) -> None:
    """直寫的標籤（文字方向：由下往上、由上往下）：文字轉 90 度，仍在原來的位置（標記的上、下、左、右）。

    Vega 的 align、baseline 與 dx、dy 都相對於文字自己的方向，所以依旋轉換算：由下往上時文字的起點在下方，
    「在錨點上方」成為 align=left，「在錨點右邊」成為 baseline=top；由上往下相反。"""
    sx, sy = mark.pop("dx", 0), mark.pop("dy", 0)
    horizontal = mark.get("align", "center")  # left：文字在錨點右邊；right：在左邊
    vertical = mark.get("baseline", "middle")  # bottom：文字在錨點上方；top：在下方
    if direction == "up":
        mark["align"] = {"bottom": "left", "top": "right"}.get(vertical, "center")
        mark["baseline"] = {"left": "top", "right": "bottom"}.get(horizontal, "middle")
        dx, dy = -sy, sx
        mark["angle"] = 270
    else:
        mark["align"] = {"bottom": "right", "top": "left"}.get(vertical, "center")
        mark["baseline"] = {"left": "bottom", "right": "top"}.get(horizontal, "middle")
        dx, dy = sy, -sx
        mark["angle"] = 90
    if dx:
        mark["dx"] = dx
    if dy:
        mark["dy"] = dy


def _dimension(ch: Channel) -> dict[str, Any]:
    use = ch.use
    if use.date is not None and use.date.kind == "trunc" and use.continuous:
        return {
            "field": ch.name,
            "type": "temporal",
            "timeUnit": TIME_UNITS[use.date.unit],
            "title": ch.title,
        }
    if use.continuous and ch.datatype in ("integer", "real"):
        return {"field": ch.name, "type": "quantitative", "title": ch.title}
    if use.continuous and ch.datatype == "date":
        # 連續的完整日期（不取日期部分或截斷）：時間軸；日期字串以 UTC 解讀，避免時區差一天
        return {
            "field": ch.name,
            "type": "temporal",
            "timeUnit": "utcyearmonthdate",
            "title": ch.title,
        }
    if use.continuous and ch.datatype == "datetime":
        return {"field": ch.name, "type": "temporal", "scale": {"type": "utc"}, "title": ch.title}
    return {"field": ch.label, "type": "ordinal", "sort": None, "title": ch.title}


def _infer_mark(
    rows_dims: list[FieldUse],
    cols_dims: list[FieldUse],
    rows_meas: list[FieldUse],
    cols_meas: list[FieldUse],
    is_continuous_date: Callable[[FieldUse], bool] = lambda use: False,
    sized: bool = False,
) -> str:
    """Tableau 的「自動」標記：欄列架上都沒有欄位、大小上有欄位時為方塊（樹狀圖）；沒有量值在欄列架上
    為文字表；連續日期為折線；其餘為長條。"""
    if not rows_meas and not cols_meas:
        return "square" if sized and not rows_dims and not cols_dims else "text"
    if rows_meas and cols_meas:
        return "circle"
    for use in rows_dims + cols_dims:
        if (use.date is not None and use.continuous) or is_continuous_date(use):
            return "line"
    return "bar"


def _sheet_extent(ir: IR, ws: Worksheet) -> tuple[float, float]:
    """工作表在 dashboard 上的區塊（第一個顯示它的區塊）的大小；不在 dashboard 上時為 DEFAULT_EXTENT。"""

    def walk(zones: list[Zone]) -> tuple[float, float] | None:
        for zone in zones:
            if zone.kind == "worksheet" and zone.worksheet == ws.id and zone.w > 0 and zone.h > 0:
                return float(zone.w), float(zone.h)
            found = walk(zone.children)
            if found is not None:
                return found
        return None

    for dashboard in ir.dashboards:
        found = walk(dashboard.zones)
        if found is not None:
            return found
    return DEFAULT_EXTENT


BREAK_AFTER = frozenset(",;:/-)")
"""換行時可以斷開的標點（斷在標點之後）。"""


def _wrap(text: Any, width: float, size: float) -> str:
    """每一行在 width 像素內換行（與 Tableau 的標籤相同）：英文在空白與標點處，全形字之間都可以；
    放不下的單字不拆開。"""
    out: list[str] = []
    for line in str(text if text is not None else "").split("\n"):
        words: list[str] = []
        current = ""
        for c in line:
            if c == " " or unicodedata.east_asian_width(c) in ("W", "F"):
                if current:
                    words.append(current)
                    current = ""
                words.append(c)
            elif c in BREAK_AFTER:
                words.append(current + c)  # 標點之後也可以換行
                current = ""
            else:
                current += c
        if current:
            words.append(current)
        buffer = ""
        lines: list[str] = []
        for word in words:
            if buffer.strip() and _text_extent((buffer + word).rstrip(), size)[0] > width:
                lines.append(buffer.rstrip())
                buffer = "" if word == " " else word
            else:
                buffer += word
        lines.append(buffer.rstrip())
        out.extend(lines)
    return "\n".join(out)


def _text_extent(text: Any, size: float) -> tuple[float, float]:
    """文字的寬高估計（像素）：全形字約一個字高、其餘約 0.55 個字高，行高為字高的 1.2 倍。"""
    lines = str(text if text is not None else "").split("\n")
    width = max(
        sum(size if unicodedata.east_asian_width(c) in ("W", "F") else size * 0.55 for c in line)
        for line in lines
    )
    return round(width, 2), round(len(lines) * size * 1.2, 2)


def _luminance(color: str) -> float:
    """顏色的相對亮度（WCAG，與 Vega 的 luminance() 相同）。"""
    digits = _css_color(color).lstrip("#")[:6]
    if len(digits) != 6:
        return 1.0
    linear = []
    for i in (0, 2, 4):
        c = int(digits[i : i + 2], 16) / 255
        linear.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _extend(box: list[float], x0: float, y0: float, x1: float, y1: float) -> list[float]:
    """外框（左、上、右、下）加上一個範圍。"""
    if not box:
        return [x0, y0, x1, y1]
    return [min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1)]


def _num(value: float) -> str:
    return f"{value:.6g}"


def _fitted_axes(
    x_field: str, y_field: str, box: list[float], k: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """排好的標記的位置：box（排列的範圍）的中心在圖的中間，k（運算式）為每單位的像素，兩軸相同
    （圖形不變形）；沒有座標軸。"""

    def axis(field: str, center: float, extent: str, reverse: bool) -> dict[str, Any]:
        scale: dict[str, Any] = {
            "domainMin": {"expr": f"{_num(center)} - {extent} / 2 / ({k})"},
            "domainMax": {"expr": f"{_num(center)} + {extent} / 2 / ({k})"},
            "nice": False,
            "zero": False,
        }
        if reverse:
            scale["reverse"] = True
        return {"field": field, "type": "quantitative", "axis": None, "scale": scale}

    return (
        axis(x_field, (box[0] + box[2]) / 2, "width", False),
        axis(y_field, (box[1] + box[3]) / 2, "height", True),
    )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return float(value)
