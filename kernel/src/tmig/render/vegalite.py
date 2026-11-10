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
from datetime import UTC, datetime
from typing import Any, Literal, TypeGuard

from tmig.data.executor import QueryResult
from tmig.ir.models import (
    IR,
    MEASURE_NAMES,
    MEASURE_VALUES,
    SPECIAL_FIELDS,
    Axis,
    Banding,
    ColorPalette,
    DataField,
    Divider,
    FieldUse,
    FontElement,
    FontStyle,
    Marks,
    Pane,
    ParamRef,
    ReferenceLine,
    Scalar,
    ShelfStyle,
    TextRun,
    TrendLine,
    Worksheet,
    ZeroLine,
)
from tmig.planner.state import use_key
from tmig.render.aliases import alias_key, member_aliases, parameter_label
from tmig.render.colors import continuous_scale, discrete_scale
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
    PERCENT_TABLE_CALCS,
    axis_format,
    display_format,
    format_value,
    parse_format,
)
from tmig.render.palettes import DEFAULT_REGULAR, PALETTES
from tmig.render.trends import fit_model, format_p, format_r2
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
UNSUPPORTED_MARKS = {"gantt", "map"}
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
LINE_WIDTH = 3
"""折線的寬度（像素）：Tableau 大小 1（預設）時的線寬，大小 s 時為 s 倍（線寬的倍數依圓點推定）。"""
BAND = "__band_"
"""色帶的資料欄（__band_rows、__band_columns）。"""
DECO = "__deco"
"""色帶、多層列標題等裝飾圖層的名稱前綴：檢視器的選取與醒目提示略過這些圖層（與參考線的 __ref 相同）。"""
MEASURE_NAMES_TITLE = "Measure Names"
MEASURE_VALUES_TITLE = "Measure Values"


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


@dataclass
class RenderResult:
    spec: dict[str, Any] | None
    rows: int
    mark: str
    unsupported: str | None = None
    notes: list[str] = field(default_factory=list)


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
        self.pie_extent = "min(width, height)"
        """圓餅的大小所依據的區塊邊長（分面時為每一格的）。"""
        self.values: list[dict[str, Any]] = []
        """工作表的資料列（色彩的範圍與各值的顏色依它決定）。"""
        self.measure_names: dict[Scalar, Any] = {}
        """Measure Names：量值的欄位鍵 → 顯示名稱（指定給量值的顏色依它對應）。"""

    def label_field(self, marks: Marks) -> str | None:
        """標記的標籤欄：自訂的標籤文字，或標籤上第一個欄位的顯示文字。"""
        if id(marks) in self.label_fields:
            return self.label_fields[id(marks)]
        return self.ch(marks.label[0]).label if marks.label else None

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
        """欄列架上畫成座標軸的量值：連續的量值與 Measure Values。"""
        return self.is_measure(use) and (use.continuous or use.field in SPECIAL_FIELDS)

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
            if scale:
                encoding["scale"] = scale
        if axis:
            encoding["axis"] = axis
        return encoding

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
        encoding: dict[str, Any] = {}
        vl_mark: dict[str, Any]
        label_field: str | None = None
        label_type = ""
        if mark == "text" or not (rows_meas or cols_meas):
            # 方塊標記、只有維度並以顏色區分：醒目提示表（色塊加上文字）
            highlight = mark == "square" and marks.color is not None
            mark = "square" if highlight else "text"
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
            text_field = self.label_field(marks)
            if text_field is None:
                measures = [c for c in self.channels.values() if c.role == "measure"]
                text_field = measures[0].label if measures else None
            vl_mark = {"type": "text", "tooltip": True, "lineBreak": "\n"}
            vl_mark.update(self.font("cell", marks.label_font).mark())
            if marks.color is None and marks.mark_color is None:
                vl_mark["color"] = self.text_color(marks, cell=True)
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
                for use in cols_dims:
                    encoding.setdefault("detail", []).append(
                        {"field": self.ch(use).name, "type": "nominal"}
                    )
            else:
                encoding["y"] = self.axis_measure(rows_meas[0], "rows")
                x = self.axis_dim(cols_dims, "cols_dim")
                if x is not None:
                    encoding["x"] = x
                for use in rows_dims:
                    encoding.setdefault("detail", []).append(
                        {"field": self.ch(use).name, "type": "nominal"}
                    )
            vl_mark = {"type": vl_type, "tooltip": True}
            if mark == "polygon":
                # 多邊形：以填滿的折線依序連接各點
                vl_mark["filled"] = True
            elif vl_type in ("line", "area"):
                # 與 Tableau 的預設相同不畫出點；透明的點讓每個標記都能停留顯示工具提示、點選
                vl_mark["point"] = "transparent"
            if mark == "shape":
                vl_mark["filled"] = True
            if any(_fixed_range(encoding.get(c)) for c in ("x", "y")):
                vl_mark["clip"] = True  # 與 Tableau 相同，固定範圍之外的標記不畫
            if mark == "polygon" and (marks.label or id(marks) in self.label_fields):
                self.notes.append("多邊形的標籤尚未畫出")
            elif marks.label or id(marks) in self.label_fields:
                label_field = self.label_field(marks)
                label_type = vl_type

        self.common(encoding, marks)
        if mark == "square":
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
        _mark_style(vl_mark, marks)
        self.size_mark(vl_mark, encoding, marks)
        if marks.path is not None and vl_mark["type"] in ("line", "area"):
            encoding["order"] = {"field": self.ch(marks.path).name}
        elif mark == "polygon":
            vl_mark["order"] = None  # 沒有路徑時依資料的順序連接（而不是依 x 排序）
        if label_field is not None:
            return {
                "encoding": encoding,
                "layer": [
                    {"mark": vl_mark},
                    _label_layer(
                        encoding,
                        label_type,
                        label_field,
                        marks.color is not None,
                        self.label_color(marks, vl_mark),
                        self.font("cell", marks.label_font),
                        marks.label_match,
                    ),
                ],
            }, mark
        return {"mark": vl_mark, "encoding": encoding}, mark

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

    def common(self, encoding: dict[str, Any], marks: Marks) -> None:
        """顏色、大小、形狀、詳細資料與工具提示。"""
        if marks.color is not None:
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
                scale = discrete_scale(
                    setting,
                    ((r.get(ch.label), r.get(ch.name)) for r in records),
                    self.measure_names if ch.use.field == MEASURE_NAMES else None,
                )
                if scale is not None:
                    encoding["color"]["scale"] = scale
        if marks.size is not None:
            ch = self.ch(marks.size)
            encoding["size"] = {"field": ch.name, "type": "quantitative", "title": ch.title}
        if marks.shape is not None:
            ch = self.ch(marks.shape)
            encoding["shape"] = {"field": ch.label, "type": "nominal", "title": ch.title}
        for use in marks.detail:
            ch = self.ch(use)
            if ch.role == "dimension":
                encoding.setdefault("detail", []).append({"field": ch.name, "type": "nominal"})
        encoding["tooltip"] = [
            {"field": ch.label, "title": ch.title} for ch in self.channels.values()
        ]

    # ------------------------------------------------------------ 多個量值

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
                charts.append(
                    {
                        "layer": [*layers, body],
                        # 同步的雙軸共用刻度，否則兩個軸各自的刻度
                        "resolve": {
                            "scale": {channel: "shared" if synchronized else "independent"}
                        },
                    }
                )
            else:
                charts.append(body)
        if len(charts) == 1:
            return charts[0], "+".join(kinds)
        return ({"vconcat": charts} if axis == "rows" else {"hconcat": charts}), "+".join(kinds)

    # ------------------------------------------------------------ 圓餅

    def pie(
        self, marks: Marks, rows_dims: list[FieldUse], cols_dims: list[FieldUse]
    ) -> dict[str, Any]:
        """圓餅：角度為 wedge-size，顏色為顏色欄位；欄列架上的維度以分面呈現。"""
        encoding: dict[str, Any] = {}
        if marks.angle is not None:
            encoding["theta"] = {
                "field": self.ch(marks.angle).name,
                "type": "quantitative",
                "stack": True,
            }
        else:
            encoding["theta"] = {"value": 1}
        self.common(encoding, marks)
        if marks.color is not None and encoding["color"]["type"] == "nominal":
            encoding["order"] = {"field": ROW}
        if rows_dims:
            encoding["row"] = self._facet(rows_dims, "rows_dim")
        if cols_dims:
            encoding["column"] = self._facet(cols_dims, "cols_dim")
        mark = _mark_style({"type": "arc", "tooltip": True}, marks)
        # 分面時每一格的大小是 child_width、child_height（width、height 是整張圖的）
        self.pie_extent = (
            "min(child_width, child_height)" if rows_dims or cols_dims else "min(width, height)"
        )
        mark["outerRadius"] = {"expr": f"{self.pie_extent} * {_pie_fraction(marks) / 2}"}
        has_label = bool(marks.label) or id(marks) in self.label_fields
        if not has_label or rows_dims or cols_dims:
            return {"mark": mark, "encoding": encoding}
        if id(marks) in self.label_fields:
            text = "{" + self.label_fields[id(marks)] + "}"
        else:
            text = "\n".join("{" + self.ch(u).label + "}" for u in marks.label)
        label = {
            "mark": {
                "type": "text",
                **self.font("cell", marks.label_font).mark(),
                "lineBreak": "\n",
                # 與 Tableau 相同寫在圓餅外側
                "radius": {"expr": f"{self.pie_extent} * {_pie_fraction(marks) / 2} + 24"},
            },
            "encoding": {
                "text": {"field": "__pie_label"},
                "color": {"value": self.text_color(marks)},
                "theta": encoding["theta"],
                **({"order": encoding["order"]} if "order" in encoding else {}),
            },
            "transform": [{"calculate": _template(text), "as": "__pie_label"}],
        }
        return {"encoding": encoding, "layer": [{"mark": mark}, label]}

    def donut(
        self,
        body: dict[str, Any],
        values: list[dict[str, Any]],
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
    ) -> dict[str, Any]:
        """甜甜圈（J3）：圓餅與疊在上面（雙軸）、詳細程度較粗的標記卡；
        後者畫成中間的洞，它的標籤（通常是合計）寫在中間。"""
        holes = [(i, p) for i, p in enumerate(self.ws.panes) if i > 0 and p.dual]
        if not holes:
            return body
        # 洞的大小依洞的標記卡的大小（沒有設定時為外圈的六成）
        hole = holes[0][1].marks
        outer = max((_pie_fraction(p.marks) for p in self.ws.panes if not p.dual), default=0.5)
        inner = min(_pie_fraction(hole), outer) if hole.mark_size is not None else outer * 0.6
        for layer in [body, *body.get("layer", [])]:
            mark = layer.get("mark")
            if isinstance(mark, dict) and mark.get("type") == "arc":
                mark["innerRadius"] = {"expr": f"{self.pie_extent} * {round(inner / 2, 4)}"}
        if rows_dims or cols_dims:
            self.notes.append("分面的甜甜圈圖中間的標籤尚未畫出（不影響數字）")
            return body
        lines: list[str] = []
        for index, pane in holes:
            records = self.pane_values.get(index, values)
            fields = (
                [self.label_fields[id(pane.marks)]]
                if id(pane.marks) in self.label_fields
                else [self.ch(use).label for use in pane.marks.label]
            )
            for name in fields:
                for record in records:
                    text = record.get(name)
                    if text not in (None, "") and str(text) not in lines:
                        lines.append(str(text))
        if not lines:
            return body
        center = {
            "data": {"values": [{}]},
            "mark": {
                "type": "text",
                "x": {"expr": "width / 2"},
                "y": {"expr": "height / 2"},
                "lineBreak": "\n",
                **self.font("cell", holes[0][1].marks.label_font).mark(),
                "color": self.text_color(holes[0][1].marks),
            },
            "encoding": {"text": {"value": "\n".join(lines)}},
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
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    fields = {f.id: f for f in ds.fields}

    channels: dict[str, Channel] = {}
    for n, column in enumerate(c for c in columns if c.role != "sort" and c.use is not None):
        assert column.use is not None
        data_field = fields[column.use.field]
        channels[column.key] = Channel(
            name=f"c{n}",
            label=f"c{n}_label",
            title=caption(data_field, column.use, ws),
            role="measure" if column.role == "measure" else "dimension",
            datatype=column.datatype,
            use=column.use,
            format=value_format(ws, data_field, column.use),
            aliases=member_aliases(data_field, column.use) if column.role == "dimension" else {},
        )
    uses = [*ws.rows, *ws.columns, *ws.marks.uses()]
    uses += [u for pane in ws.panes for u in pane.marks.uses()]
    long = bool(ws.measure_values) and any(u.field in SPECIAL_FIELDS for u in uses)
    builder = _Builder(ws, channels)

    # ---------------------------------------------------------- 資料
    values = _records(channels, result)
    if COLUMN_ORDER in result.columns:
        at = result.columns.index(COLUMN_ORDER)
        for record, row in zip(values, result.rows, strict=True):
            record[ORDER] = row[at]
    for total, total_result in totals or []:
        records = _records(channels, total_result)
        for record in records:
            for key in total.dropped:
                if key in channels:
                    record[channels[key].name] = None
                    record[channels[key].label] = total.label(key)
            record[TOTAL] = True
        if total.group is None:
            values += records
        else:
            keys = [channels[k].label for k in total.group if k in channels]
            values = after_groups(
                values, records, lambda r, keys=keys: tuple(r.get(k) for k in keys)
            )
    pane_values = {
        i: _records(channels, pane_result) for i, (_, pane_result) in (panes or {}).items()
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
    for n, (index, marks) in enumerate(cards):
        if not marks.label_text:
            continue
        name = f"__text{n}"
        builder.label_fields[id(marks)] = name
        targets = [values]
        if index is not None and index in pane_values:
            targets = [pane_values[index]]
        for records in targets:
            for record in records:
                record[name] = compose_text(marks.label_text, ir, params, channels, record)

    # Measure Names 沒有對應的量值（Measure Values 卡是空的）時不畫：Tableau 只顯示空白的欄標題
    builder.rows = [u for u in ws.rows if builder.shown(u)]
    builder.columns = [u for u in ws.columns if builder.shown(u)]
    if len(builder.rows) + len(builder.columns) < len(ws.rows) + len(ws.columns):
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
    if pie_marks is not None:
        body = builder.pie(pie_marks, rows_dims, cols_dims)
        if len(ws.panes) > 1:
            body = builder.donut(body, values, rows_dims, cols_dims)
        mark = "pie"
    elif multiple:
        body, mark = builder.multi(rows_dims, cols_dims, rows_meas, cols_meas)
    else:
        if mark == "automatic":
            mark = _infer_mark(
                rows_dims, cols_dims, rows_meas, cols_meas, builder.is_continuous_date
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
        column_order = _column_order(values, column_field, [builder.ch(u) for u in cols_dims])
    for channel in ("row", "column"):
        # 分面依資料的順序（查詢已依排序設定排好）；Vega-Lite 的分面不指定時會依字母排序
        facet = body.get("encoding", {}).get(channel)
        if isinstance(facet, dict) and facet.get("sort") is None:
            if channel == "column" and facet.get("field") == column_field:
                facet["sort"] = column_order
            else:
                facet["sort"] = list(dict.fromkeys(v.get(facet["field"]) for v in values))

    if ws.reference_lines:
        params = {p.id: p.value for p in ir.parameters} | (parameters or {})
        body = _with_reference_lines(builder, body, values, params)
    trend = next((m.trend for m in [ws.marks, *(p.marks for p in ws.panes)] if m.trend), None)
    if trend is not None and mark == "pie":
        builder.notes.append("圓餅沒有座標軸，趨勢線未畫出")
    elif trend is not None and not multiple:
        # 多個量值時每張圖各自畫（見 _Builder.multi）
        _trend_lines(builder, body, trend, builder.records_of(ws.marks))

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
    title_color = next((r.style.color for r in ws.title if r.style and r.style.color), None)
    spec["usermeta"]["title_color"] = title_color or _title_color(ws)
    spec["usermeta"]["title_font"] = _title_font(ws)
    if "vconcat" in body or "hconcat" in body:
        # 多張圖無法自動填滿容器，由檢視器依區塊大小設定每張圖的寬高
        del spec["width"], spec["height"]
        spec["autosize"] = {"type": "pad"}
    spec.update(body)
    if column_field is not None:
        _sort_headers(spec, column_field, column_order)
    for record in values:
        record.pop(ORDER, None)
    if totals and not ws.totals_colored:
        _exclude_totals_from_color(spec, mark, builder.text_color(ws.marks, cell=True))
    encoding = main_encoding(spec)
    sized = (
        encoding is not None
        and not any(k in spec for k in ("vconcat", "hconcat"))
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
        if mark == "text" and "x" not in encoding and y_type in ("ordinal", "nominal"):
            # 只有列標題的文字表是一欄：寬度依內容（與離散的欄相同），不填滿
            sizing["columns"] = {"size": ws.column_style.cell or _column_width(texts)}
        elif mark == "text" and "step" in sizing["columns"]:
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
        if alias is not None:
            texts[key] = alias
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


def _column_order(values: list[dict[str, Any]], field: str, members: list[Channel]) -> list[Any]:
    """欄標題的順序：工作表的標記依名次（只看欄維度自己的排序，與列無關）；小計緊接在它的組
    （前段的標題相同的最後一欄）之後，總計在最後。"""
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


def _records(channels: dict[str, Channel], result: QueryResult) -> list[dict[str, Any]]:
    """查詢結果 → 資料列：原始值欄與顯示文字欄；只轉換結果中有的欄位。"""
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
            if alias is not None:
                record[ch.label] = alias
            elif use.date is not None and use.date.kind == "part" and raw is not None:
                record[ch.label] = str(raw)
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
}
SCOPE_NAMES = {"table": "Table", "pane": "Pane", "cell": "Cell"}
REF_COLOR = "#444"
REF = "__ref"
"""參考線的資料欄與圖層名稱前綴；檢視器選取標記時略過這些圖層。"""


def _with_reference_lines(
    builder: _Builder,
    body: dict[str, Any],
    values: list[dict[str, Any]],
    params: dict[str, Scalar],
) -> dict[str, Any]:
    """參考線圖層：每一列資料寫入所屬範圍的參考值（__ref…），再以 rule 或 tick 畫出。

    範圍：整張表；窗格＝欄列架上的離散維度，但各架最內層若是離散維度則不算（它切出的是儲存格）；
    儲存格＝欄列架上所有的離散維度。
    """
    ws, notes = builder.ws, builder.notes
    encoding = body.get("encoding") if "mark" in body or "layer" in body else None
    if encoding is None:
        notes.append("多個量值、圓餅等圖表上的參考線尚未畫出（不影響數字）")
        return body
    scopes = _scope_keys(builder)
    layers: list[dict[str, Any]] = []
    for n, line in enumerate(ws.reference_lines):
        axis = builder.channels.get(use_key(line.axis))
        position = next(
            (
                c
                for c in ("x", "y")
                if axis is not None and encoding.get(c, {}).get("field") == axis.name
            ),
            None,
        )
        if axis is None or position is None:
            notes.append(f"參考線 {line.id} 的量值不在座標軸上，沒有畫出")
            continue
        source = _reference_source(line, builder.channels, params)
        if source is None:
            notes.append(f"參考線 {line.id} 的值不在查詢結果中，沒有畫出")
            continue
        if line.formula == "total" and isinstance(source, str):
            agg = line.value.agg if isinstance(line.value, FieldUse) else None
            if agg not in ("sum", "count", "min", "max"):
                notes.append(f"參考線 {line.id} 的合計需要重新彙總整體資料，沒有畫出")
                continue
        group_keys = scopes[line.scope]
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for record in values:
            if record.get(TOTAL):
                continue  # 總計的列與欄不畫參考線，也不納入計算
            groups.setdefault(tuple(record.get(k) for k in group_keys), []).append(record)
        params_of_line: list[float | None] = (
            list(line.factors)
            if line.formula == "stdev"
            else list(line.percentiles)
            if line.formula == "percentile"
            else [None]
        )
        names = [f"{REF}{n}_{i}" for i in range(len(params_of_line))]
        first: list[float] = []
        for members in groups.values():
            numbers = (
                [float(v) for m in members if _is_number(v := m.get(source))]
                if isinstance(source, str)
                else []
            )
            for name, extra in zip(names, params_of_line, strict=True):
                value = _reference_value(line, numbers, source, extra)
                if value is not None and name == names[0]:
                    first.append(value)
                for m in members:
                    m[name] = value
        other = "y" if position == "x" else "x"
        other_enc = encoding.get(other)
        title = encoding[position].get("title")
        per_mark = (
            len(groups) > 1
            and other_enc is not None
            and other_enc.get("type") in ("nominal", "ordinal")
        )
        for name in names:
            measure = {"field": name, "type": "quantitative", "title": title}
            if per_mark:
                # 離散軸：每個標記位置畫一段短線，同一範圍的短線連成該範圍的參考線
                layers.append(
                    {
                        "name": name,
                        "transform": [{"calculate": "-1", "as": ROW}],
                        "mark": {"type": "tick", "color": REF_COLOR, "thickness": 1.5},
                        "encoding": {position: measure, other: other_enc},
                    }
                )
                continue
            rule: dict[str, Any] = {position: {**measure, "aggregate": "max"}}
            if len(groups) > 1:
                rule["detail"] = [{"field": k, "type": "nominal"} for k in group_keys]
            layers.append(
                {
                    "name": name,
                    "mark": {"type": "rule", "color": REF_COLOR, "strokeDash": [5, 3]},
                    "encoding": rule,
                }
            )
        label = _reference_label(line, axis, first)
        if label and len(groups) == 1 and len(names) == 1:
            place: dict[str, Any] = (
                {"y": {"value": 0}, "align": "left", "dx": 3, "dy": 0}
                if position == "x"
                else {"x": {"value": "width"}, "align": "right", "dx": 0, "dy": -3}
            )
            layers.append(
                {
                    "name": f"{REF}{n}_label",
                    "mark": {
                        "type": "text",
                        "color": REF_COLOR,
                        "align": place.pop("align"),
                        "baseline": "bottom" if position == "y" else "top",
                        "dx": place.pop("dx"),
                        "dy": place.pop("dy"),
                    },
                    "encoding": {
                        position: {
                            "field": names[0],
                            "type": "quantitative",
                            "aggregate": "max",
                            "title": title,
                        },
                        **place,
                        "text": {"value": label},
                    },
                }
            )
    if not layers:
        return body
    return {"layer": [body, *layers]}


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


def _reference_source(
    line: ReferenceLine, channels: dict[str, Channel], params: dict[str, Scalar]
) -> str | float | None:
    """參考線的值來源：資料欄名（以標記的值計算），或常數、參數的值。"""
    if isinstance(line.value, ParamRef):
        value = params.get(line.value.parameter)
        return float(value) if _is_number(value) else None
    if line.formula == "constant" or line.value is None:
        return float(line.constant) if _is_number(line.constant) else None
    ch = channels.get(use_key(line.value))
    return ch.name if ch is not None else None


def _reference_value(
    line: ReferenceLine, numbers: list[float], source: str | float, extra: float | None
) -> float | None:
    """一個範圍的參考值；extra 為百分位數或標準差倍數。"""
    if not isinstance(source, str):
        return source
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
    if formula == "percentile":
        ordered = sorted(numbers)
        position = (len(ordered) - 1) * (50.0 if extra is None else extra) / 100
        low = math.floor(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    if formula == "stdev":
        if len(numbers) < (2 if line.sample else 1):
            return None
        sd = statistics.stdev(numbers) if line.sample else statistics.pstdev(numbers)
        return statistics.fmean(numbers) + (extra or 0.0) * sd
    return None


def _reference_label(line: ReferenceLine, axis: Channel, values: list[float]) -> str:
    """只有一條線時的標籤；自動與值都顯示依量值格式化的值。"""
    if line.label == "none" or not values:
        return ""
    text = format_value(values[0], "real", axis.format, None)
    computation = COMPUTATION_NAMES[line.formula]
    if line.label == "computation":
        return computation
    if line.label == "custom":
        return (
            (line.label_text or "")
            .replace("<Value>", text)
            .replace("<Computation>", computation)
            .replace("<Scope>", SCOPE_NAMES[line.scope])
        )
    return text


def _table_calc_type(ws: Worksheet, use: FieldUse) -> str | None:
    if use.table_calc is None:
        return None
    tc = next((t for t in ws.table_calcs if t.id == use.table_calc), None)
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


def _pie_fraction(marks: Marks) -> float:
    """圓餅的直徑占區塊（分面的每一格）較短邊的比例：大小 s 時為 s / 2，最多填滿（依 Tableau 的畫面量測：
    大小 1.505 的圓餅直徑約為區塊高度的 76%，甜甜圈內外圈的直徑比與大小的比相同）。"""
    size = marks.mark_size if marks.mark_size is not None else PIE_SIZE
    return round(min(size / 2, 1), 4)


def _label_layer(
    encoding: dict[str, Any],
    vl_type: str,
    field_name: str,
    colored: bool,
    color: str = TEXT_COLOR,
    font: Font | None = None,
    match: bool = False,
) -> dict[str, Any]:
    """標籤：長條在尾端（堆疊時在各段內側），其他標記在點的上方。match 時文字的顏色與標記相同
    （依欄位著色時沿用同一個色彩的 encoding）。"""
    stacked = vl_type == "bar" and colored
    horizontal = encoding.get("x", {}).get("type") == "quantitative"
    mark: dict[str, Any] = {"type": "text", **(font or resolve()).mark(), "lineBreak": "\n"}
    layer: dict[str, Any] = {"text": {"field": field_name}, "color": {"value": color}}
    if vl_type == "bar" and horizontal:
        mark.update(align="right", dx=-3) if stacked else mark.update(align="left", dx=3)
        if stacked:
            layer["x"] = {**encoding["x"], "stack": "zero"}
    elif vl_type == "bar":
        mark.update(baseline="top", dy=3) if stacked else mark.update(baseline="bottom", dy=-3)
        if stacked:
            layer["y"] = {**encoding["y"], "stack": "zero"}
    else:
        mark.update(baseline="bottom", dy=-4)
    if match and "field" in encoding.get("color", {}):
        layer["color"] = encoding["color"]
    elif stacked and "color" in encoding:
        # 文字改成白色，但仍以顏色欄位分組，才能與長條同樣堆疊
        layer["color"] = {"value": "#ffffff"}
        layer["detail"] = {"field": encoding["color"]["field"], "type": encoding["color"]["type"]}
    return {"mark": mark, "encoding": layer}


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
) -> str:
    """Tableau 的「自動」標記：沒有量值在欄列架上為文字表；連續日期為折線；其餘為長條。"""
    if not rows_meas and not cols_meas:
        return "text"
    if rows_meas and cols_meas:
        return "circle"
    for use in rows_dims + cols_dims:
        if (use.date is not None and use.continuous) or is_continuous_date(use):
            return "line"
    return "bar"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return float(value)
