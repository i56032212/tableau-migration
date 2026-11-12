"""工作表：欄列架、標記、篩選、排序與 Table Calculation（A4）。

Table Calculation 的計算方向（Tableau 的 Compute Using）依真實 .twb 歸納：

- ``ordering-type='Columns'`` 為 Table (down)：沿著欄往下算，定址為列架上的維度
- ``ordering-type='Rows'`` 為 Table (across)：沿著列往右算，定址為欄架上的維度
- 標記卡上的維度為分割；計算方向的架上沒有維度時改為定址標記卡上的維度（圓餅的百分比、100% 堆疊長條的
  Table (down)），與 Tableau 的畫面相同（HR Dashboards 的 Departments：列架上的 INDEX() 依部門編號，
  色彩上的狀態各自從 1 開始，同一個部門的兩種狀態在同一列；TabMon 的 Logins Per Hour：每個 instance
  各自與前一小時的差異）
- 圓餅（甜甜圈）的標記卡上的 Table Calculation 例外：標記卡上的維度與架上的維度一起定址（HR Analytics 的
  甜甜圈：各年齡層、性別的百分比合計 100%；HR Dashboards 的 Gender：甜甜圈各塊的比例）
- ``ordering-type='Field'`` 為指定維度：``ordering-field`` 或 ``<order field>`` 列出定址欄位
- 未寫 ``ordering-type`` 時依 Tableau 預設 Table (across)，並標記為推定（``inferred``）

其餘未納入的分割方式一律列為不支援，等真實報表出現再補。

同一架上有多個量值時，Tableau 以 ``+`` 連接，每個量值各有一張標記卡（帶 ``y-axis-name`` 或
``x-axis-name`` 的 <pane>；``y-index='1'`` 表示雙軸的第二個軸）。各標記卡的維度必須相同，
否則各量值的詳細程度不同，列為不支援。Measure Names／Values 以特殊欄位 ``measure_names``、
``measure_values`` 表示，Measure Values 的量值與順序取自 Measure Names 的篩選與手動排序。
"""

from __future__ import annotations

import copy
import itertools
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from lxml import etree

from tmig.ir.models import (
    GENERATED_FIELDS,
    GEOMETRY_GENERATED,
    LATITUDE_GENERATED,
    LONGITUDE_GENERATED,
    MEASURE_NAMES,
    MEASURE_VALUES,
    SPECIAL_FIELDS,
    Annotation,
    AnnotationPoint,
    Blend,
    BlendLink,
    CategoricalFilter,
    ClusterSpec,
    ColorPalette,
    DataField,
    FieldUse,
    Filter,
    Forecast,
    MarkLabels,
    Marks,
    MarkType,
    MeasureAlias,
    NestedTableCalc,
    Pane,
    ParamRef,
    RangeFilter,
    ReferenceLine,
    Scalar,
    Sort,
    TableCalc,
    TableCalcPreset,
    TextRun,
    TextStyle,
    TupleFilter,
    VisualTotal,
    Worksheet,
)
from tmig.parser.colors import color_encodings, hex_color, label_color, mark_style, parse_color
from tmig.parser.context import (
    DsState,
    FilterKind,
    ParseContext,
    SharedView,
    SheetState,
    TooltipRef,
)
from tmig.parser.datasource import (
    TABLE_ID_PREFIX,
    add_calc_field,
    add_cluster_field,
    add_forecast_indicator,
    add_table_count,
    add_uncached_column,
    analyze_calcs,
)
from tmig.parser.fields import (
    FilterParser,
    bucket_values,
    instance_key,
    is_aggregate,
    use_from_instance,
    use_from_name,
    value_type,
)
from tmig.parser.formats import (
    pane_cell_align,
    pane_label_font,
    pane_label_match,
    pane_labels,
    pane_mark_size,
    pane_trend_line,
    reference_styles,
    run_style,
    worksheet_formats,
)
from tmig.parser.maps import density_style, map_options
from tmig.parser.model import graph_objects
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import attr, child, children, descendants, local_name, text_of, xpath
from tmig.parser.values import LiteralError, parse_literal
from tmig.planner.state import use_key

TEXT_TOKEN = re.compile(r"<([^<>]+)>")
SHEET_TAG = re.compile(r'<Sheet((?:\s+[\w-]+="[^"]*")*)\s*/?>')
"""工具提示文字中的工作表（Viz in Tooltip）：<Sheet name="…" maxwidth="300" maxheight="300" filter="<All Fields>">"""
SHEET_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
"""自訂文字中的參照：<[資料源].[欄位實例]>、<[Parameters].[參數]>、<Sheet Name>。"""
LINE_BREAK = "\u00c6\n"
"""Tableau 格式化文字的換行（Æ 加換行）。"""

MARKS: dict[str, MarkType] = {
    "Automatic": "automatic",
    "Bar": "bar",
    "Line": "line",
    "Area": "area",
    "Circle": "circle",
    "Square": "square",
    "Shape": "shape",
    "Text": "text",
    "Pie": "pie",
    "GanttBar": "gantt",
    "Polygon": "polygon",
    "Multipolygon": "map",
    "Heatmap": "density",
}
SPECIAL_KEYS = {
    "[:Measure Names]": MEASURE_NAMES,
    "[Multiple Values]": MEASURE_VALUES,
    "[Latitude (generated)]": LATITUDE_GENERATED,
    "[Longitude (generated)]": LONGITUDE_GENERATED,
    "[Geometry (generated)]": GEOMETRY_GENERATED,
}
TUPLE_PREFIXES = {
    "[Action (": "sheet_link",
    "[Tooltip (": "vit",
    "[Exclusions (": "exclude",
    "[Inclusions (": "include",
}
"""Tableau 自動產生的多欄位篩選欄位（資料源 <group> 沒寫 auto-column 時依名稱判斷）。"""
ENCODINGS = ("color", "size", "text", "lod", "tooltip", "shape", "wedge-size", "path", "geometry")
ORDERING: dict[str, TableCalcPreset] = {
    "Columns": "table_down",
    "Rows": "table_across",
    "Table": "table_across_then_down",
    "TableCol": "table_down_then_across",
    "ColumnInPane": "pane_down",
    "RowInPane": "pane_across",
    "Pane": "pane_across_then_down",
    "PaneCol": "pane_down_then_across",
    "CellInPane": "cell",
    "Field": "specific",
}
"""ordering-type → 計算方向（Tableau 的 Rows 是「表格（橫向）」：沿著一列計算）。"""
QUICK_TYPES = {
    "CumTotal": "running_total",
    "PctTotal": "percent_of_total",
    "Rank": "rank",
    "PctRank": "percentile",
    "Difference": "difference",
    "PctDiff": "percent_difference",
    "PctValue": "percent_from",
    "WindowTotal": "moving",
}
DIFFERENCE_TYPES = ("difference", "percent_difference", "percent_from")
FIXED_ENDS = {"%first%": "first", "first": "first", "%last%": "last", "last": "last"}
"""「相對於」第一個、最後一個的寫法（依公開資料推定）；其他值為定址欄位的成員。"""
MOVING_AGGS = frozenset(
    {"sum", "avg", "min", "max", "median", "count", "stdev", "stdevp", "var", "varp"}
)
RANK_TYPES = {
    "Competition": "competition",
    "Dense": "dense",
    "Modified": "modified",
    "Unique": "unique",
}

# 工作表內可以出現的元素；OPAQUE 只影響格式，不往下檢查
KNOWN = frozenset(
    {
        "worksheet",
        "table",
        "view",
        "datasources",
        "datasource",
        "datasource-dependencies",
        "column",
        "column-instance",
        "table-calc",
        "order",
        "calculation",
        "range",
        "members",
        "member",
        "filter",
        "groupfilter",
        "min",
        "max",
        "sort",
        "computed-sort",
        "manual-sort",
        "natural-sort",
        "alphabetic-sort",
        "dictionary",
        "bucket",
        "shelf-sorts",
        "shelf-sort-v2",
        # 較舊版本的依量值排序：<view><rows><shelf-sort><dimensions-to-sort><dimension>
        "shelf-sort",
        "dimensions-to-sort",
        "dimension",
        "slices",
        "aggregation",
        "panes",
        "pane",
        "mark",
        "encodings",
        "color",
        "size",
        "text",
        "lod",
        "tooltip",
        "shape",
        "wedge-size",
        "path",
        "geometry",
        # 地圖：背景地圖的來源（活頁簿的 <mapsources> 決定，見 tmig.parser.workbook）
        "mapsources",
        "mapsource",
        "rows",
        "cols",
        "pages",
        "simple-id",
        "repository-location",
        "breakdown",
        # 群組定義（categorical-bin 的 <bin><value>）；差異計算的相對位置（<address><value>）
        "bin",
        "value",
        "address",
        # 參考線（J4）
        "reference-line",
        "reference-line-value",
        # 小計
        "subtotals",
        # Data Blending 手動設為作用中、取消作用的連結欄位（_blends）
        "join-lod-include-overrides",
        "join-lod-exclude-overrides",
        # 趨勢線（不分組的因子 <excluded-factors><column>）
        "trendline",
        "excluded-factors",
    }
)
OPAQUE = frozenset(
    {
        # 註解另外解析（_annotations）
        "annotations",
        "style",
        "layout-options",
        "customized-tooltip",
        "customized-label",
        "mark-sizing",
        "desc",
        "aliases",
        "formatted-text",
        "tooltip-style",
        "title",
        "caption",
        "layout-cache",
        "page-trail-options",
        "target",
        "semantic-values",
    }
)
IGNORED_ELEMENTS = {
    "mark-labels": "特定標記的標籤（標記標籤＞一律顯示）：新平台在工具提示與資料表中呈現這些值",
    "sort-filter-info": "依量值排序時只看特定成員的值（sort-filter-info）：新平台以全部成員的值排序",
    "show-full-range": "顯示缺少的值（沒有資料的分組或日期）：新平台只顯示有資料的標記",
}
REFERENCE_FORMULAS = frozenset(
    {
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
    }
)
VISUAL_TOTALS: dict[str, Literal["sum", "avg", "min", "max"]] = {
    "sum": "sum",
    "avg": "avg",
    "average": "avg",
    "min": "min",
    "minimum": "min",
    "max": "max",
    "maximum": "max",
}
"""總計的計算方式（Total Using）：visual-totals 的值 → 畫面上各標記的值再彙總的方式。"""
DISTRIBUTIONS = frozenset({"percentile", "stdev", "quantiles", "confidence", "median_confidence"})
"""分布（多個值，之間塗上底色）的計算方式；百分比（reference-line-value 的 percentage）也是分布。"""
LINE_PATTERNS = {"solid": "solid", "dashed": "dashed", "dotted": "dotted", "dash-dot": "dashed"}
REFERENCE_SCOPES = {"per-table": "table", "per-pane": "pane", "per-cell": "cell"}
REFERENCE_LABELS = frozenset({"none", "automatic", "value", "computation", "custom"})
KNOWN_UNSUPPORTED = {
    # 類別篩選以外的「開啟活頁簿時篩選到最新的值」（類別篩選的見 CategoricalFilter.latest）
    "preset": "篩選的「開啟報表時篩選到最新的值」（<preset type='last-values'>）",
    "add-in": "工作表擴充功能",
}


def _folded_axes(style: etree._Element | None) -> set[str]:
    """座標軸樣式中疊到前一個軸（雙軸）的量值：<encoding attr='space' field=… fold='true'>。"""
    folded: set[str] = set()
    for rule in children(style, "style-rule"):
        if rule.get("element") != "axis":
            continue
        for enc in children(rule, "encoding"):
            if enc.get("attr") == "space" and enc.get("fold") == "true" and enc.get("field"):
                folded.add(instance_key(enc.get("field", "")))
    return folded


def _relation_use(ds: DsState, ref: str, instances: dict[str, etree._Element]) -> FieldUse | None:
    """自訂關聯中的欄位（<datasource-relationships> 的相依清單中的欄位實例，或欄位本身）。"""
    key = instance_key(ref)
    inst = instances.get(key)
    if inst is not None:
        use = use_from_instance(ds, inst)
        return None if isinstance(use, str) else use.model_copy(update={"continuous": False})
    data_field = ds.by_name.get(key)
    return FieldUse(field=data_field.id) if data_field is not None else None


def shelf_refs(text: str) -> tuple[list[str], set[str]]:
    """拆解欄列架文字：回傳完整參照（例如 [ds].[none:Region:nk]）與出現的運算子。"""
    refs: list[str] = []
    operators: set[str] = set()
    current: list[str] = []

    def flush() -> None:
        if current:
            refs.append(".".join("[" + part.replace("]", "]]") + "]" for part in current))
            current.clear()

    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "[":
            j = i + 1
            name: list[str] = []
            while j < len(text):
                if text[j] == "]":
                    if j + 1 < len(text) and text[j + 1] == "]":
                        name.append("]")
                        j += 2
                        continue
                    break
                name.append(text[j])
                j += 1
            current.append("".join(name))
            i = j + 1
            continue
        if ch == ".":
            i += 1
            continue
        if ch in "/*+()":
            flush()
            operators.add(ch)
        elif ch.isspace():
            flush()
        i += 1
    flush()
    return refs, operators


class SheetParser:
    def __init__(self, ctx: ParseContext, element: etree._Element) -> None:
        self.ctx = ctx
        self.el = element
        self.name = element.get("name", "")
        self.instances: dict[str, etree._Element] = {}
        self._pie = False
        """目前處理的 Table Calculation 用在圓餅的標記卡上（定址標記卡上的維度，見 _direction）。"""
        self.uses: dict[str, FieldUse] = {}
        self.ds: DsState | None = None
        self.secondaries: dict[str, DsState] = {}
        """Data Blending 的次要資料源：名稱 → 狀態，依 <datasources> 的順序（第一個是主要資料源）。"""
        self.secondary_instances: dict[str, dict[str, etree._Element]] = {}
        self._blending = False
        """目前解析的位置可以用次要資料源的欄位（欄列架、標記卡與其中的文字、數字格式）。"""
        self._colors: dict[str, etree._Element] | None = None
        """色彩編碼（工作表與資料源的 <style>）：欄位實例鍵 → <encoding>。"""
        self.cross: dict[str, str] = {}
        """跨資料源篩選：其他資料源的欄位（[資料源].[欄位實例]）→ 主要資料源中對應的欄位實例名稱。"""
        self.forecast_measures: list[FieldUse] = []
        """預測的量值（fVal: 的欄位實例對應的量值）。"""
        self.forecast_indicator: str | None = None
        self.cluster: tuple[str, str] | None = None
        """叢集：（叢集欄位 id、分析模型的名稱）。"""
        self._keyed: dict[str, Filter] = {}
        """工作表上的單一欄位篩選，以篩選的欄位實例名稱為鍵（跨資料源篩選與同一欄位的篩選合併用）。"""
        self.hide_unknown = False
        """地圖篩選掉未知的位置（Latitude／Longitude (generated) 的 non-null 篩選）。"""

    # ------------------------------------------------------------ 入口

    def parse(self) -> Worksheet | None:
        ctx, el = self.ctx, self.el
        table = child(el, "table")
        view = child(table, "view")
        if table is None or view is None:
            ctx.flag(el, f"工作表「{self.name}」沒有 <table>／<view>")
            return None
        sources = [d.get("name", "") for d in children(child(view, "datasources"), "datasource")]
        main = [s for s in sources if s != "Parameters"]
        if not main:
            if _is_blank(table):
                ctx.ignore(el, f"空白工作表「{self.name}」（沒有資料源）：以空白區塊呈現")
                ctx.blank_sheets.add(self.name)
            else:
                ctx.flag(el, f"工作表「{self.name}」沒有資料源")
            return None
        ds = ctx.datasources.get(main[0])
        if ds is None:
            ctx.flag(el, f"工作表「{self.name}」的資料源 {main[0]} 無法移轉")
            return None
        self.ds = ds
        for name in main[1:]:
            secondary = ctx.datasources.get(name)
            if secondary is None:
                ctx.flag(view, f"工作表「{self.name}」的次要資料源 {name}（Data Blending）無法移轉")
                return None
            self.secondaries[name] = secondary
        self._dependencies(view, main[0])
        for name, secondary in self.secondaries.items():
            self.secondary_instances[name] = self._dependencies(view, name, secondary)
        self._check_elements(el)

        with self._blend_scope():
            rows = self._shelf(child(table, "rows"))
            cols = self._shelf(child(table, "cols"))
        subtotals = self._subtotals(table, rows, cols)
        totals = self._totals(table, rows, cols)
        visual_totals = self._visual_totals() if totals or subtotals else []
        totals_colored = bool(totals or subtotals) and self._totals_colored(child(table, "style"))
        page_use, page = self._pages(child(table, "pages"))
        aggregation = child(view, "aggregation")
        # 取消彙總量值：量值的欄位實例沒有彙總（[none:Sales:qk]），查詢時每一列資料是一個標記
        disaggregated = aggregation is not None and aggregation.get("value") == "false"
        with self._blend_scope():
            marks, panes, mark_dims = self._marks(
                child(table, "panes"), rows, cols, _folded_axes(child(table, "style"))
            )
            measure_values, measure_aliases = self._measure_values(
                view, [*rows, *cols, *marks.uses()], panes
            )
        card_uses = [*rows, *cols, *marks.uses(), *(u for p in panes for u in p.marks.uses())]
        if not measure_values and any(u.field == MEASURE_VALUES for u in card_uses):
            return None  # 無法得知 Measure Values 的量值（已列為不支援）
        blends = self._blends(
            view,
            [*card_uses, *measure_values],
            bool(totals or subtotals),
            child(table, "join-lod-include-overrides"),
            child(table, "join-lod-exclude-overrides"),
        )
        if blends is None:
            return None
        sheet_id = ctx.ids.make("ws", self.name)
        table_calcs = self._table_calcs(rows, cols, mark_dims, _pie_keys(child(table, "panes")))
        filters, kinds = self._filters(view)
        self._shared_filters(view, main[0], filters, kinds)
        sorts = self._sorts(view)
        reference_lines = self._reference_lines(child(table, "panes"), child(table, "style"))
        annotations = self._annotations(child(table, "annotations"))
        forecast = self._forecast(child(table, "forecast-specification"))
        cluster = self._cluster_spec(card_uses)
        with self._blend_scope():
            formats = self._formats(child(table, "style"), [*rows, *cols])
        sheet_formats = worksheet_formats(
            child(table, "style"), self.resolve, rows, cols, child(ctx.package.root, "style")
        )
        title = self._title(child(el, "layout-options"))
        self._check_label_text(card_uses, [marks, *(p.marks for p in panes)])
        on_map = any(u.field in GENERATED_FIELDS for u in [*rows, *cols])
        options = map_options(child(table, "style"), self.hide_unknown) if on_map else None
        worksheet = Worksheet(
            id=sheet_id,
            name=self.name,
            datasource=ds.id,
            rows=rows,
            columns=cols,
            marks=marks,
            panes=panes,
            measure_values=measure_values,
            measure_aliases=measure_aliases,
            filters=filters,
            sorts=sorts,
            table_calcs=table_calcs,
            reference_lines=reference_lines,
            annotations=annotations,
            cluster=cluster,
            forecast=forecast,
            formats=formats,
            title=title,
            totals=totals,
            totals_colored=totals_colored,
            subtotals=subtotals,
            visual_totals=visual_totals,
            axes=sheet_formats.axes,
            row_style=sheet_formats.row_style,
            column_style=sheet_formats.column_style,
            background=sheet_formats.background,
            text_color=sheet_formats.text_color,
            cell_color=sheet_formats.cell_color,
            field_fonts=sheet_formats.field_fonts,
            field_colors=sheet_formats.field_colors,
            cell_align=sheet_formats.cell_align,
            field_label_background=sheet_formats.field_label_background,
            fonts=sheet_formats.fonts,
            label_valign=sheet_formats.label_valign,
            fit=ctx.sheet_fits.get(self.name, "standard"),
            blends=blends,
            disaggregated=disaggregated,
            pages=page_use,
            page=page,
            map=options,
            source_ref=xpath(el),
        )
        ctx.sheets[self.name] = SheetState(
            id=sheet_id,
            name=self.name,
            ds=ds,
            instances=self.instances,
            uses=self.uses,
            filter_kinds=kinds,
            cross=self.cross,
        )
        return worksheet

    # ------------------------------------------------------------ 欄位實例

    def _dependencies(
        self, view: etree._Element, ds_name: str, ds: DsState | None = None
    ) -> dict[str, etree._Element]:
        """工作表相依清單中這個資料源的欄位實例（主要資料源的存到 self.instances）。"""
        state = ds or self.ds
        assert state is not None
        instances = self.instances if ds is None else {}
        added = False
        for dep in children(view, "datasource-dependencies"):
            if dep.get("datasource") != ds_name:
                continue
            for column in children(dep, "column"):
                name = column.get("name", "")
                if column.get("is-adhoc-cluster") == "true":
                    # 叢集：查詢之後才填入叢集編號
                    existed = name in state.by_name
                    found = add_cluster_field(self.ctx, state, column)
                    if found is not None and ds is None:
                        self.cluster = (found.id, column.get("parent-model", ""))
                    added = added or (found is not None and not existed)
                    continue
                if name in state.by_name:
                    continue
                if name.startswith(TABLE_ID_PREFIX) and len(graph_objects(state.element)) <= 1:
                    # 單表資料源的「資料表 (計數)」只寫在工作表的相依清單中
                    add_table_count(self.ctx, state, name, column.get("caption"), column)
                    added = True
                    continue
                if child(column, "calculation") is None:
                    # Live 連線：不在存檔時快取的欄位清單中、只寫在工作表相依清單中的欄位
                    if add_uncached_column(self.ctx, state, column):
                        added = True
                    continue
                # 直接在欄列架上輸入的臨時計算，只存在工作表裡
                if add_calc_field(self.ctx, state, column) is not None:
                    added = True
            for inst in children(dep, "column-instance"):
                # 物件模型的欄位實例名稱含資料表前綴，例如 [__tableau_internal_object_id__].[cnt:…:qk]
                instances[instance_key(inst.get("name", ""))] = inst
        if added:
            analyze_calcs(self.ctx, state)
        return instances

    def _parameter_field(self, ref: str) -> FieldUse | str:
        """欄列架或標記卡上的參數（例如放在文字上顯示目前的選擇）：當成公式只有這個參數的計算欄位，
        沿用參數的別名（顯示的值與 Tableau 相同）。"""
        assert self.ds is not None
        param = self.ctx.parameter(ref)
        if param is None:
            return f"找不到參數 {ref}"
        key = f"[Parameters].{param.name}"
        if key in self.uses:
            return self.uses[key]
        name = f"[{param.name[1:-1]} (Parameter)]"
        existing = self.ds.by_name.get(name)
        if existing is None:
            source = self.ctx.param_columns.get(param.id)
            column = etree.Element("column")
            column.set("name", name)
            column.set("caption", param.caption)
            column.set("datatype", param.datatype)
            column.set("role", "dimension")
            column.set("type", "nominal")
            aliases = child(source, "aliases") if source is not None else None
            if aliases is not None:
                column.append(copy.deepcopy(aliases))
            calculation = etree.SubElement(column, "calculation")
            calculation.set("class", "tableau")
            calculation.set("formula", f"[Parameters].{param.name}")
            created = add_calc_field(self.ctx, self.ds, column)
            if created is None:
                return f"欄列架上的參數 {ref} 無法轉成計算欄位"
            analyze_calcs(self.ctx, self.ds)
            existing = created
        use = FieldUse(field=existing.id)
        self.uses[key] = use
        return use

    def resolve(self, ref: str) -> FieldUse | str:
        assert self.ds is not None
        parts = qualified_parts(ref)
        if len(parts) == 2 and parts[0] in self.secondaries:
            return self._secondary_use(parts[0], ref)
        if len(parts) == 2 and parts[0] not in (self.ds.name, "Parameters"):
            return f"參照了其他資料源 {parts[0]}"
        if parts and parts[0] == "Parameters":
            return self._parameter_field(ref)
        key = instance_key(ref)
        if key in self.uses:
            return self.uses[key]
        if key in SPECIAL_KEYS:
            # Latitude／Longitude (generated) 是連續的座標軸（綠色膠囊）
            special = SPECIAL_KEYS[key]
            use = FieldUse(
                field=special, continuous=special in (LATITUDE_GENERATED, LONGITUDE_GENERATED)
            )
            self.uses[key] = use
            return use
        inst = self.instances.get(key)
        if inst is not None and inst.get("forecast-column-type"):
            return self._forecast_use(key, inst)
        if inst is None:
            data_field = self.ds.by_name.get(key)
            if data_field is None:
                if "\\%" in ref:
                    # Measure Names 的成員與色彩對照中，欄位名稱的 % 寫成 \%
                    return self.resolve(ref.replace("\\%", "%"))
                if key.startswith("[:"):
                    return f"特殊欄位 {key} v0 不支援"
                return f"找不到欄位實例 {ref}"
            use = FieldUse(field=data_field.id)
        else:
            resolved = use_from_instance(self.ds, inst)
            if isinstance(resolved, str):
                return resolved
            use = resolved
        self.uses[key] = use
        return use

    def _forecast_use(self, key: str, inst: etree._Element) -> FieldUse | str:
        """預測的結果：實際值與估計值（fVal:）是預測的量值本身；Forecast Indicator 是查詢之後才補上
        Estimate 的字串欄位。其他結果類型（趨勢、精確度等）尚不支援。"""
        assert self.ds is not None
        kind = inst.get("forecast-column-type")
        if kind == "forecast-value":
            base = self.resolve(inst.get("forecast-column-base", ""))
            if isinstance(base, str):
                return base
            if base not in self.forecast_measures:
                self.forecast_measures.append(base)
            self.uses[key] = base
            return base
        if kind == "forecast-indicator":
            found = add_forecast_indicator(self.ctx, self.ds, inst)
            if found is None:
                return "Forecast Indicator 無法建立"
            analyze_calcs(self.ctx, self.ds)
            use = FieldUse(field=found.id)
            self.forecast_indicator = found.id
            self.uses[key] = use
            return use
        return f"預測的結果類型 {kind} v0 不支援"

    def _forecast(self, element: etree._Element | None) -> Forecast | None:
        """預測的設定（<forecast-specification>）：開啟時才有預測。"""
        if element is None or element.get("enabled") != "true":
            return None
        if not self.forecast_measures:
            self.ctx.ignore(
                element, f"工作表「{self.name}」開啟了預測，但檢視中沒有預測的量值：略過"
            )
            return None
        if element.get("auto-forecast-agg") == "false":
            self.ctx.flag(element, "預測的「彙總依據」不是檢視的時間細度，v0 不支援")
            return None
        components = {
            "ets-none": "none",
            "ets-additive": "additive",
            "ets-multiplicative": "multiplicative",
        }
        model = {"auto-season": "auto", "auto": "auto_no_season", "custom": "custom"}.get(
            element.get("model-type", ""), "auto"
        )
        length = {"auto": "auto", "next": "next", "end-of": "until"}.get(
            element.get("range-type", ""), "auto"
        )
        size = element.get("range-size")
        periods = (element.get("range-periods") or "").split("-")[0].lower()
        try:
            confidence = float(element.get("band-confidence-level") or "95") / 100
            ignore_last = int(element.get("ignore-last") or "1")
        except ValueError:
            confidence, ignore_last = 0.95, 1
        return Forecast(
            measures=list(self.forecast_measures),
            indicator=self.forecast_indicator,
            model=model,  # type: ignore[arg-type]
            trend=components.get(element.get("trend-type", "")),  # type: ignore[arg-type]
            season=components.get(element.get("season-type", "")),  # type: ignore[arg-type]
            length=length,  # type: ignore[arg-type]
            size=int(size) if size and size.isdigit() else None,
            unit=periods
            if periods in ("year", "quarter", "month", "week", "day", "hour")
            else None,  # type: ignore[arg-type]
            ignore_last=max(ignore_last, 0),
            fill_zeros=element.get("fill-type") == "fill-zero",
            confidence=min(max(confidence, 0.01), 0.999),
            bands=element.get("show-prediction-bands") != "false",
        )

    def _cluster_spec(self, card_uses: list[FieldUse]) -> ClusterSpec | None:
        """叢集的設定（資料源中的 <analytic-model>）：分析的量值都要在檢視中。"""
        if self.cluster is None:
            return None
        assert self.ds is not None
        field_id, name = self.cluster
        model = self.ds.analytic_models.get(name)
        if model is None:
            self.ctx.flag(self.el, f"工作表「{self.name}」的叢集找不到分析模型 {name}")
            return None
        spec = child(model, "analytic-model-specifications")
        if spec is None or spec.get("class") != "cluster-specifications":
            self.ctx.flag(model, f"分析模型 {name} 不是叢集，v0 不支援")
            return None
        if text_of(child(spec, "is-dissagregate")) == "true":
            self.ctx.flag(spec, "取消彙總的叢集（每一列資料分叢集）v0 不支援")
            return None
        if children(child(spec, "lod-fields"), "field") or children(
            child(spec, "filter-fields"), "field"
        ):
            self.ctx.flag(spec, "叢集的詳細程度或篩選欄位 v0 不支援")
            return None
        measures: list[FieldUse] = []
        for item in children(child(spec, "analysis-fields"), "field"):
            use = self._use(item.get("name"), spec, "叢集的分析欄位")
            if use is None:
                return None
            if not self._is_measure(use):
                self.ctx.flag(spec, "叢集的分析欄位是維度（以對應分析分群）v0 不支援")
                return None
            if use not in card_uses:
                self.ctx.flag(spec, f"叢集的分析量值 {item.get('name')} 不在檢視中，v0 不支援")
                return None
            measures.append(use)
        if not measures:
            self.ctx.flag(spec, "叢集沒有分析的量值")
            return None
        text = text_of(child(spec, "number-of-desired-clusters"))
        count = int(text) if text.isdigit() and int(text) > 0 else None
        return ClusterSpec(field=field_id, measures=measures, count=count)

    def _secondary_use(self, name: str, ref: str) -> FieldUse | str:
        """次要資料源（Data Blending）的欄位：只能是欄列架與標記卡上彙總的量值。"""
        secondary = self.secondaries[name]
        if not self._blending:
            return f"次要資料源「{secondary.caption}」（Data Blending）的欄位只支援放在欄列架與標記卡上"
        key = instance_key(ref)
        cache = f"[{name}].{key}"
        if cache in self.uses:
            return self.uses[cache]
        inst = self.secondary_instances.get(name, {}).get(key)
        if inst is None:
            data_field = secondary.by_name.get(key)
            if data_field is None:
                return f"找不到次要資料源「{secondary.caption}」的欄位實例 {ref}"
            use: FieldUse | str = FieldUse(field=data_field.id)
        else:
            if children(inst, "table-calc"):
                return f"次要資料源「{secondary.caption}」的欄位套用 Table Calculation（Data Blending）v0 不支援"
            use = use_from_instance(secondary, inst)
        if isinstance(use, str):
            return use
        if not is_aggregate(secondary, use) or secondary.levels.get(use.field) == "table_calc":
            caption = secondary.field_by_id(use.field).caption
            return (
                f"次要資料源「{secondary.caption}」的「{caption}」不是彙總的量值："
                "Data Blending v0 只支援次要資料源的量值"
            )
        self.uses[cache] = use
        return use

    @contextmanager
    def _blend_scope(self) -> Iterator[None]:
        saved, self._blending = self._blending, True
        try:
            yield
        finally:
            self._blending = saved

    def _field(self, field_id: str) -> DataField:
        """主要或次要資料源的欄位。"""
        assert self.ds is not None
        for ds in (self.ds, *self.secondaries.values()):
            for item in ds.fields:
                if item.id == field_id:
                    return item
        raise KeyError(field_id)

    def _is_measure(self, use: FieldUse) -> bool:
        """在檢視中是否為量值（次要資料源的欄位依它自己的資料源判斷）。"""
        assert self.ds is not None
        for secondary in self.secondaries.values():
            if any(f.id == use.field for f in secondary.fields):
                return is_aggregate(secondary, use)
        return is_aggregate(self.ds, use)

    # ------------------------------------------------------------ 頁面架

    def _pages(self, element: etree._Element | None) -> tuple[FieldUse | None, Scalar]:
        """頁面架：一個維度（<pages><column>…</column></pages>）；目前的頁取自工作表視窗的
        <current-page>。頁面的歷程記錄（page-trail-options）不支援，只顯示目前的頁。"""
        columns = children(element, "column")
        if element is None or not columns:
            return None, None
        if len(columns) > 1:
            self.ctx.flag(element, f"工作表「{self.name}」的頁面架上有多個欄位，v0 不支援")
            return None, None
        use = self._use(text_of(columns[0]), columns[0], "頁面架")
        if use is None:
            return None, None
        if use.field in SPECIAL_FIELDS or self._is_measure(use):
            self.ctx.flag(element, f"工作表「{self.name}」的頁面架上是量值，v0 不支援")
            return None, None
        self.ctx.paged_sheets.add(self.name)
        raw = self.ctx.sheet_pages.get(self.name)
        if raw is None:
            return use, None
        try:
            return use, parse_literal(raw, value_type(self._field(use.field), use))
        except LiteralError:
            self.ctx.ignore(element, f"工作表「{self.name}」目前的頁 {raw} 無法辨識：顯示第一頁")
            return use, None

    # ------------------------------------------------------------ Data Blending

    def _blends(
        self,
        view: etree._Element,
        uses: list[FieldUse],
        totals: bool,
        include: etree._Element | None = None,
        exclude: etree._Element | None = None,
    ) -> list[Blend] | None:
        """次要資料源與連結欄位。自訂關聯（<datasource-relationships>）優先，沒有時以同名的維度自動連結；
        檢視中要有連結欄位（作用中的連結），否則無法混合。

        連結欄位在檢視中時自動作用；手動取消作用的連結（<join-lod-exclude-overrides>）不用。"""
        assert self.ds is not None
        primary_ids = {f.id for f in self.ds.fields}
        dims = [
            u
            for u in uses
            if u.field in primary_ids and u.field not in SPECIAL_FIELDS and not self._is_measure(u)
        ]
        blends: list[Blend] = []
        for name, secondary in self.secondaries.items():
            ids = {f.id for f in secondary.fields}
            if not any(u.field in ids for u in uses):
                continue
            label = f"工作表「{self.name}」混合次要資料源「{secondary.caption}」（Data Blending）"
            if totals:
                self.ctx.flag(view, f"{label}並加上總計或小計，v0 不支援")
                return None
            links = self._links(name, secondary)
            if not links:
                self.ctx.flag(view, f"{label}：找不到連結欄位")
                return None
            excluded = self._override_fields(exclude, name, secondary, label)
            if excluded is None:
                return None
            links = [link for link in links if link.secondary.field not in excluded]
            if not any(link.secondary_for(d) is not None for link in links for d in dims):
                self.ctx.flag(view, f"{label}：檢視中沒有連結欄位（沒有作用中的連結）")
                return None
            # 手動設為作用中的連結（<join-lod-include-overrides>）：連結欄位在檢視中時與自動相同；
            # 不在檢視中時要在更細的層級混合後再彙總，尚不支援
            included = self._override_fields(include, name, secondary, label)
            if included is None:
                return None
            active = {
                link.secondary.field
                for link in links
                for d in dims
                if link.secondary_for(d) is not None
            }
            if included - active:
                self.ctx.flag(
                    include if include is not None else view,
                    f"{label}：連結欄位不在檢視中卻設為作用中，v0 不支援",
                )
                return None
            blends.append(Blend(datasource=secondary.id, links=links))
        return blends

    def _override_fields(
        self, element: etree._Element | None, name: str, secondary: DsState, label: str
    ) -> set[str] | None:
        """<join-lod-include-overrides>、<join-lod-exclude-overrides> 中這個次要資料源的欄位 id。"""
        found: set[str] = set()
        for column in children(element, "column"):
            parts = qualified_parts(text_of(column))
            if len(parts) != 2 or parts[0] != name:
                continue
            field = secondary.by_name.get("[" + parts[1].replace("]", "]]") + "]")
            if field is None:
                self.ctx.flag(column, f"{label}：連結欄位 {text_of(column)} 無法解析")
                return None
            found.add(field.id)
        return found

    def _links(self, name: str, secondary: DsState) -> list[BlendLink]:
        assert self.ds is not None
        primary = self.ds
        relations = child(self.ctx.package.root, "datasource-relationships")
        instances: dict[str, dict[str, etree._Element]] = {}
        for dep in children(relations, "datasource-dependencies"):
            found = instances.setdefault(dep.get("datasource", ""), {})
            for inst in children(dep, "column-instance"):
                found[instance_key(inst.get("name", ""))] = inst
        custom: list[BlendLink] = []
        defined = False
        for relation in children(relations, "datasource-relationship"):
            pair = (relation.get("source"), relation.get("target"))
            if pair not in ((primary.name, name), (name, primary.name)):
                continue
            defined = True
            for item in descendants(child(relation, "column-mapping"), "map"):
                refs = [item.get("key", ""), item.get("value", "")]
                if qualified_parts(refs[0])[:1] == [name]:
                    refs.reverse()
                left = _relation_use(primary, refs[0], instances.get(primary.name, {}))
                right = _relation_use(secondary, refs[1], instances.get(name, {}))
                if left is None or right is None:
                    self.ctx.flag(
                        relation, f"Data Blending 的自訂關聯 {refs[0]} ＝ {refs[1]} 無法解析"
                    )
                    continue
                custom.append(BlendLink(primary=left, secondary=right))
        if defined:
            return custom
        # 自動連結：名稱相同的維度
        by_caption = {
            f.caption: f
            for f in secondary.fields
            if f.role == "dimension"
            and secondary.levels.get(f.id) not in ("aggregate", "table_calc")
        }
        links: list[BlendLink] = []
        for data_field in primary.fields:
            if data_field.role != "dimension" or data_field.caption not in by_caption:
                continue
            if primary.levels.get(data_field.id) in ("aggregate", "table_calc"):
                continue
            other = by_caption[data_field.caption]
            links.append(
                BlendLink(primary=FieldUse(field=data_field.id), secondary=FieldUse(field=other.id))
            )
        return links

    def _use(self, ref: str | None, anchor: etree._Element, what: str) -> FieldUse | None:
        if not ref:
            self.ctx.flag(anchor, f"{what}缺少欄位")
            return None
        resolved = self.resolve(ref)
        if isinstance(resolved, str):
            self.ctx.flag(anchor, f"{what}：{resolved}")
            return None
        return resolved

    # ------------------------------------------------------------ 欄列架與標記

    def _shelf(self, element: etree._Element | None) -> list[FieldUse]:
        if element is None:
            return []
        text = text_of(element)
        if not text:
            return []
        refs, _operators = shelf_refs(text)
        # 「+」連接同一架上的多個量值（各自一個軸，或以 y-index 組成雙軸）
        uses: list[FieldUse] = []
        for ref in refs:
            use = self._use(ref, element, f"{local_name(element)} 架")
            if use is not None:
                uses.append(use)
        return uses

    def _reference_lines(
        self, panes_el: etree._Element | None, table_style: etree._Element | None
    ) -> list[ReferenceLine]:
        panes = children(panes_el, "pane")
        styles = reference_styles([table_style, *(child(p, "style") for p in panes)])
        lines: list[ReferenceLine] = []
        for pane in panes:
            for element in children(pane, "reference-line"):
                line = self._reference_line(element, len(lines), styles)
                if line is not None:
                    lines.append(line)
        ids = {line.id for line in lines}
        # 參考區間的另一條線沒有解析出來時，只畫這一條
        return [
            line
            if line.paired is None or line.paired in ids
            else line.model_copy(update={"paired": None})
            for line in lines
        ]

    def _reference_line(
        self, element: etree._Element, index: int, styles: dict[str, dict[str, str]]
    ) -> ReferenceLine | None:
        """參考線、參考區間、分布與盒鬚圖：常數、參數或標記值的彙總；新平台畫不出來的種類記為不影響數字。"""
        ctx = self.ctx
        formula = element.get("formula", "")
        formula = "median_confidence" if formula == "medianconfidence" else formula
        scope = REFERENCE_SCOPES.get(element.get("scope", ""))
        boxplot = element.get("boxplot-whisker-type") is not None
        if (formula not in REFERENCE_FORMULAS and not boxplot) or scope is None:
            ctx.ignore(
                element,
                f"參考線、參考區間或分布（{formula}，{element.get('scope')}）新平台不畫（不影響數字）",
            )
            return None
        axis = self.resolve(instance_key(element.get("axis-column", "")))
        if isinstance(axis, str) or axis.field in SPECIAL_FIELDS:
            ctx.ignore(element, "Measure Values 軸上的參考線新平台不畫（不影響數字）")
            return None
        value_ref = element.get("value-column", "")
        value: FieldUse | ParamRef | None = None
        constant: Scalar = None
        if value_ref.startswith("[Parameters]"):
            param = ctx.parameter(value_ref)
            if param is None:
                ctx.flag(element, f"參考線的參數 {value_ref} 找不到")
                return None
            value = ParamRef(parameter=param.id)
        elif formula == "constant":
            try:
                constant = parse_literal(element.get("value"), "real")
            except LiteralError as exc:
                ctx.flag(element, f"參考線的常數無法解析：{exc}")
                return None
        else:
            found = self.resolve(instance_key(value_ref)) if value_ref else axis
            if isinstance(found, str) or found.field in SPECIAL_FIELDS:
                ctx.ignore(element, f"參考線的量值 {value_ref} 新平台不畫（不影響數字）")
                return None
            value = found
        details = children(element, "reference-line-value")
        percentiles: list[float] = []
        factors: list[float] = []
        percentages: list[float] = []
        tiles: int | None = None
        probability = 95.0
        try:
            percentages = [float(d.get("percentage", "")) for d in details if d.get("percentage")]
            if formula == "quantiles":
                tiles = int(element.get("tile-count") or 4)
                if tiles < 2:
                    raise ValueError(tiles)
            if formula in ("confidence", "median_confidence"):
                probability = float(element.get("probability") or 95)
                if not 0 < probability < 100:
                    raise ValueError(probability)
            if formula == "percentile":
                percentiles = [
                    float(d.get("percentile", "")) for d in details if d.get("percentile")
                ]
                percentiles = percentiles or [50.0]
                if any(not 0 <= v <= 100 for v in percentiles):
                    raise ValueError(percentiles)
            if formula == "stdev":
                factors = [float(d.get("factor", "")) for d in details if d.get("factor")]
                factors = factors or [-1.0, 1.0]
                if element.get("symmetric") == "true":
                    factors = sorted({abs(f) for f in factors} | {-abs(f) for f in factors})
        except ValueError:
            ctx.flag(element, "參考線的百分位數、標準差倍數、百分比、分位數或信賴水準無法解析")
            return None
        label = element.get("label-type", "automatic")
        line_id = element.get("id") or f"refline{index}"
        style = {**styles.get("*", {}), **styles.get(line_id, {})}
        kind = (
            "boxplot"
            if boxplot
            else "distribution"
            if formula in DISTRIBUTIONS or percentages
            else "line"
        )
        visible = style.get("refline:line-visibility") or style.get("refband:line-visibility")
        width = style.get("refline:stroke-size") or style.get("refband:stroke-size")
        pattern = style.get("refline:line-pattern-only") or style.get("refline:line-pattern")
        pattern = pattern or style.get("refband:line-pattern-only")
        return ReferenceLine(
            id=line_id,
            axis=axis,
            value=value,
            formula=formula if formula in REFERENCE_FORMULAS else "median",  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            constant=constant,
            percentiles=percentiles,
            factors=factors,
            percentages=percentages,
            tiles=tiles,
            probability=probability,
            sample=element.get("type") != "population",
            scope=scope,  # type: ignore[arg-type]
            label=label if label in REFERENCE_LABELS else "automatic",  # type: ignore[arg-type]
            label_text=element.get("label") if label == "custom" else None,
            label_format=style.get("refline:text-format") or None,
            paired=element.get("paired-id") or None,
            fill_above=element.get("fill-above") == "true",
            fill_below=element.get("fill-below") == "true",
            line_visible=None if not visible else visible != "off" and width != "0",
            line_color=hex_color(style.get("refline:stroke-color")),
            line_width=int(width) if width and width.isdigit() and int(width) > 0 else None,
            line_pattern=LINE_PATTERNS.get(pattern or ""),  # type: ignore[arg-type]
            fill_color=hex_color(style.get("refband:fill-color")),
            above_color=hex_color(style.get("refline:fill-above")),
            below_color=hex_color(style.get("refline:fill-below")),
            whiskers="minmax" if element.get("boxplot-whisker-type") == "minmax" else "standard",
            hide_marks=element.get("boxplot-mark-exclusion") == "true",
            source_ref=xpath(element),
        )

    def _marks(
        self,
        panes_el: etree._Element | None,
        rows: list[FieldUse],
        cols: list[FieldUse],
        folded: set[str],
    ) -> tuple[Marks, list[Pane], list[FieldUse]]:
        """標記卡：沒有 id 的 <pane> 是共用的標記卡，帶 y-axis-name／x-axis-name 的是個別量值的標記卡。

        雙軸：個別標記卡的 y-index／x-index 大於 0，或座標軸樣式把該量值的軸疊到前一個（fold）。
        各標記卡的詳細程度（欄列架與標記卡上的維度）可以不同，查詢時各自計算（J3）；
        但圓餅的內圈有外圈沒有的維度（多層圓餅）尚不支援。
        """
        panes = children(panes_el, "pane")
        if not panes:
            return Marks(type="automatic"), [], []
        latitude = next((u for u in rows if u.field == LATITUDE_GENERATED), None)
        if panes_el is not None and panes_el.get("customization-axis") == "layer":
            if latitude is not None:
                return self._layers(panes, latitude)
            self.ctx.flag(panes_el, "標記圖層（不在地圖上）v0 不支援")
        main = next((p for p in panes if p.get("id") is None), panes[0])
        marks, dims = self._card(main)
        result: list[Pane] = []
        pane_dims: list[list[FieldUse]] = []
        for element in panes:
            if element is main:
                continue
            axis_ref = element.get("y-axis-name") or element.get("x-axis-name")
            axis = "rows" if element.get("y-axis-name") else "columns"
            if not axis_ref:
                if _pane_signature(element) != _pane_signature(main):
                    self.ctx.flag(element, "多個標記卡（沒有對應的量值，設定也不同）v0 不支援")
                continue
            measure = self._use(axis_ref, element, "標記卡的量值")
            if measure is None:
                continue
            if measure.field in (MEASURE_NAMES, MEASURE_VALUES):
                # Measure Values 的軸的標記卡：與共用的標記卡相同、不是雙軸時（例如 Measure Values 與
                # 另一個量值各自一個軸）等同共用的標記卡；設定不同或疊成雙軸時是個別的標記卡（例如
                # 以 Measure Names 上色的折線，疊上標出最大、最小值的圓點）
                index = element.get("y-index") or element.get("x-index") or "0"
                dual = (index.isdigit() and int(index) > 0) or instance_key(axis_ref) in folded
                if not dual and _pane_signature(element) == _pane_signature(main):
                    continue
                if measure.field != MEASURE_VALUES:
                    self.ctx.flag(element, f"標記卡的軸 {axis_ref} 不是量值")
                    continue
            if measure not in (rows if axis == "rows" else cols):
                self.ctx.flag(element, f"標記卡的量值 {axis_ref} 不在欄列架上")
                continue
            card, card_dims = self._card(element)
            index = element.get("y-index") or element.get("x-index") or "0"
            dual = (index.isdigit() and int(index) > 0) or instance_key(axis_ref) in folded
            result.append(Pane(measure=measure, axis=axis, dual=dual, marks=card))
            pane_dims.append(card_dims)
        if result:
            dims = pane_dims[0]
        return marks, result, dims

    def _layers(
        self, panes: list[etree._Element], latitude: FieldUse
    ) -> tuple[Marks, list[Pane], list[FieldUse]]:
        """地圖的標記圖層（Tableau 2020.4 起的「新增標記圖層」）：每個圖層是一張個別的標記卡（ws.panes，
        依 <pane> 的順序由下往上畫；與雙軸相同，詳細程度較粗的圖層各自查詢），共用的標記卡沒有欄位。
        沒有圖層名稱（generated-title）的 <pane> 是共用的設定、不是圖層；隱藏的圖層不畫。"""
        layers = [p for p in panes if p.get("generated-title") or p.get("title")] or panes[:1]
        shown = []
        for element in layers:
            if element.get("hidden") == "true":
                title = element.get("title") or element.get("generated-title")
                self.ctx.ignore(element, f"工作表「{self.name}」隱藏的地圖圖層「{title}」：不畫出")
                continue
            shown.append(element)
        if not shown:
            shown = layers[:1]
        cards = [self._card(element) for element in shown]
        result = [
            Pane(measure=latitude, axis="rows", dual=i > 0, marks=card)
            for i, (card, _) in enumerate(cards)
        ]
        return Marks(type=cards[0][0].type), result, cards[0][1]

    def _card(self, pane: etree._Element) -> tuple[Marks, list[FieldUse]]:
        """一張標記卡：標記類型與編碼；回傳標記卡與其中的維度。"""
        mark_el = child(pane, "mark")
        mark_class = mark_el.get("class", "Automatic") if mark_el is not None else "Automatic"
        mark_type = MARKS.get(mark_class)
        if mark_type is None:
            self.ctx.flag(
                mark_el if mark_el is not None else pane, f"標記類型 {mark_class} v0 不支援"
            )
            mark_type = "automatic"
        marks = Marks(type=mark_type)
        dims: list[FieldUse] = []
        singles = {"color": "color", "size": "size", "shape": "shape", "wedge-size": "angle"}
        color_ref: str | None = None
        for enc in children(child(pane, "encodings")):
            tag = local_name(enc)
            if tag not in ENCODINGS:
                self.ctx.flag(enc, f"標記編碼 <{tag}> v0 不支援")
                continue
            use = self._use(enc.get("column"), enc, f"標記編碼 {tag}")
            if use is None:
                continue
            if tag in singles:
                attr_name = singles[tag]
                if tag == "color" and marks.color is not None and not self._is_measure(use):
                    # 色彩上有多個欄位（Tableau 以組合著色）：以第一個欄位著色，其他的維度當作詳細資料
                    # （詳細程度相同，數字不變）；Measure Names 不改變詳細程度，略過
                    self.ctx.ignore(
                        enc,
                        f"工作表「{self.name}」的色彩上有多個欄位：以第一個欄位著色，"
                        "其他的欄位當作詳細資料（Tableau 以欄位值的組合著色）",
                    )
                    if use.field != MEASURE_NAMES and use not in marks.detail:
                        marks.detail.append(use)
                        dims.append(use)
                    continue
                if getattr(marks, attr_name) is not None:
                    self.ctx.flag(enc, f"標記編碼 {tag} 重複設定")
                    continue
                setattr(marks, attr_name, use)
                if tag == "color":
                    color_ref = enc.get("column")
            elif tag == "path":
                marks.path = use
            elif tag == "geometry":
                if use.field != GEOMETRY_GENERATED:
                    self.ctx.flag(enc, "空間檔案（Shapefile 等）的幾何欄位 v0 不支援")
                    continue
                marks.geometry = use
            elif tag == "text":
                marks.label.append(use)
            elif tag == "lod":
                marks.detail.append(use)
            else:
                marks.tooltip.append(use)
            if use.field not in SPECIAL_FIELDS and not self._is_measure(use):
                dims.append(use)
        label = child(child(pane, "customized-label"), "formatted-text")
        if label is not None:
            marks.label_text = self._text_runs(label, f"工作表「{self.name}」的自訂標籤")
        tooltip = child(child(pane, "customized-tooltip"), "formatted-text")
        if tooltip is not None:
            self._tooltip_sheets(tooltip, marks)
        if marks.color is not None and color_ref is not None:
            marks.color_palette = self._color_palette(color_ref, marks.color)
        marks.mark_color, marks.opacity, marks.stroke = mark_style(pane)
        if mark_type == "density":
            marks.density = density_style(pane)
        marks.label_color = label_color(pane)
        marks.cell_align = pane_cell_align(pane)
        marks.label_font = pane_label_font(pane)
        marks.label_match = pane_label_match(pane)
        marks.trend, reason = pane_trend_line(pane)
        trend_el = child(pane, "trendline")
        if reason is not None and trend_el is not None:
            self.ctx.flag(trend_el, reason)
        if marks.trend is not None:
            for column in children(child(trend_el, "excluded-factors"), "column"):
                use = self._use(text_of(column), column, "趨勢線的因子")
                if use is not None:
                    marks.trend.excluded.append(use)
        marks.mark_size = pane_mark_size(pane)
        marks.labels = self._labels(pane)
        return marks, dims

    def _labels(self, pane: etree._Element) -> MarkLabels | None:
        """標記卡「標籤」的設定（顯示與否、最小值／最大值、線條兩端、重疊）；都是預設時為 None。"""
        found = pane_labels(pane)
        if found is None:
            return None
        for item in found.unknown:
            self.ctx.ignore(pane, f"標記標籤的設定 {item} 尚不支援，依預設呈現（不影響數字）")
        settings = dict(found.settings)
        if found.field_ref is not None:
            use = self._use(found.field_ref, pane, "標記標籤的最小值／最大值欄位")
            if use is not None and self._is_measure(use):
                settings["field"] = use
            elif use is not None:
                self.ctx.ignore(pane, "標記標籤的最小值／最大值欄位不是量值，改依座標軸上的量值")
        labels = MarkLabels.model_validate(settings)
        return None if labels == MarkLabels() else labels

    def _color_palette(self, ref: str, use: FieldUse) -> ColorPalette | None:
        """顏色欄位的色彩設定：工作表的 <style> 優先，其次是資料源的 <style>（同一個欄位實例）。"""
        assert self.ds is not None
        if self._colors is None:
            source = color_encodings(child(self.ds.element, "style"))
            sheet = color_encodings(child(child(self.el, "table"), "style"))
            self._colors = {**source, **sheet}
        enc = self._colors.get(instance_key(ref))
        if enc is None:
            return None
        return parse_color(enc, self._measure_key if use.field == MEASURE_NAMES else None)

    def _measure_key(self, ref: str) -> str | None:
        """Measure Names 的值（量值的欄位實例）→ 量值的欄位鍵；不在這張工作表的量值回傳 None。"""
        parts = qualified_parts(ref)
        if not parts or parts[0] == "Parameters":
            return None
        resolved = self.resolve(ref)
        return None if isinstance(resolved, str) else use_key(resolved)

    # ------------------------------------------------------------ 自訂文字、數字格式與標題

    def _text_runs(self, formatted: etree._Element, what: str) -> list[TextRun]:
        """格式化文字（<formatted-text> 的各段 <run>）→ 文字與參照依序排列，各段帶著它的格式（字級、粗體、
        顏色等，例如 KPI 的大數字）。

        參照可能被拆在相鄰的幾段中，所以先串成一段再找，格式取參照開頭那一段的；認不得的 <…> 照原文保留。
        """
        text = ""
        styles: list[TextStyle | None] = []
        for run in children(formatted, "run"):
            # 單獨的 Æ 是格式化文字的分段記號，畫面上不顯示
            raw = "".join(str(part) for part in run.itertext())
            raw = raw.replace(LINE_BREAK, "\n").replace("\u00c6", "")
            text += raw
            styles += [run_style(run)] * len(raw)
        text = text.rstrip()
        runs: list[TextRun] = []

        def style_at(index: int) -> TextStyle | None:
            return styles[index] if index < len(styles) else None

        def add_text(start: int, end: int) -> None:
            # 依各字的格式分段；換行沒有格式（不影響下一行）
            for index in range(start, end):
                value = text[index]
                style = None if value == "\n" else style_at(index)
                last = runs[-1] if runs else None
                if last is not None and last.text is not None and last.style == style:
                    runs[-1] = last.model_copy(update={"text": last.text + value})
                else:
                    runs.append(TextRun(text=value, style=style))

        def add_name(value: str, index: int) -> None:
            last = runs[-1] if runs else None
            style = style_at(index)
            if last is not None and last.text is not None and last.style == style:
                runs[-1] = last.model_copy(update={"text": last.text + value})
            else:
                runs.append(TextRun(text=value, style=style))

        position = 0
        for match in TEXT_TOKEN.finditer(text):
            add_text(position, match.start())
            position = match.end()
            token = match.group(1)
            if token == "Sheet Name":
                add_name(self.name, match.start())
                continue
            parts = qualified_parts(token)
            if len(parts) == 2 and parts[0] == "Parameters":
                param = self.ctx.parameter(token)
                if param is None:
                    self.ctx.flag(formatted, f"{what}：找不到參數 {token}")
                    continue
                runs.append(TextRun(parameter=param.id, style=style_at(match.start())))
                continue
            if (
                len(parts) == 2
                and self.ds is not None
                and parts[0] in (self.ds.name, *self.secondaries)
            ):
                resolved = self.resolve(token)
                if isinstance(resolved, str):
                    self.ctx.flag(formatted, f"{what}：{resolved}")
                    continue
                runs.append(TextRun(use=resolved, style=style_at(match.start())))
                continue
            add_text(match.start(), match.end())
        add_text(position, len(text))
        return runs

    def _annotations(self, element: etree._Element | None) -> list[Annotation]:
        """註解：<annotation class='mark'|'point'|'area'>，文字（<formatted-text>）與位置
        （<point>、<top-left>、<bottom-right> 的 <visual-coordinate>）；文字相對於位置的偏移在 <body x y>。
        認不得的位置略過這個註解（只影響圖表）。"""
        found: list[Annotation] = []
        for item in children(element, "annotation"):
            kind = item.get("class")
            if kind not in ("mark", "point", "area"):
                self.ctx.ignore(item, f"工作表「{self.name}」的註解類型 {kind} 尚不支援，未畫出")
                continue
            formatted = child(item, "formatted-text")
            text = (
                self._text_runs(formatted, f"工作表「{self.name}」的註解")
                if formatted is not None
                else []
            )
            body = child(item, "body")
            offset = (
                _number(body.get("x") if body is not None else None),
                _number(body.get("y") if body is not None else None),
            )
            positions: dict[str, AnnotationPoint | None] = {}
            for tag in ("point",) if kind != "area" else ("top-left", "bottom-right"):
                coordinate = child(child(item, tag), "visual-coordinate")
                positions[tag] = (
                    self._annotation_point(coordinate) if coordinate is not None else None
                )
            if any(p is None for p in positions.values()):
                self.ctx.ignore(item, f"工作表「{self.name}」的註解位置無法解析，未畫出")
                continue
            found.append(
                Annotation(
                    kind=kind,  # type: ignore[arg-type]
                    text=text,
                    point=positions.get("point"),
                    top_left=positions.get("top-left"),
                    bottom_right=positions.get("bottom-right"),
                    offset=offset,
                )
            )
        return found

    def _annotation_point(self, coordinate: etree._Element) -> AnnotationPoint | None:
        """<visual-coordinate class='axis'>：x-coord、y-coord 的 axis-value 與座標軸的量值；
        class='mark'：<tuple-reference> 的欄位（<columns>）與值（<tuple>）。"""
        if coordinate.get("class") == "mark":
            reference = child(coordinate, "tuple-reference")
            columns = children(child(child(reference, "tuple-descriptor"), "columns"), "field")
            values = children(child(reference, "tuple"), "value")
            if not columns or len(columns) != len(values):
                return None
            mark: dict[str, Scalar] = {}
            for column, value in zip(columns, values, strict=True):
                use = self.resolve(text_of(column))
                if isinstance(use, str) or use.agg is not None or use.table_calc is not None:
                    continue  # 量值不用來找標記
                if use.field in SPECIAL_FIELDS:
                    continue
                field = self._field(use.field)
                try:
                    mark[use_key(use)] = parse_literal(text_of(value), value_type(field, use))
                except LiteralError:
                    return None
            return AnnotationPoint(mark=mark) if mark else None
        point: dict[str, object] = {}
        for axis in ("x", "y"):
            coord = child(coordinate, f"{axis}-coord")
            if coord is None:
                continue
            raw = coord.get("axis-value")
            ref = text_of(child(child(coord, "node-reference"), "axis"))
            if raw is None or not ref:
                continue
            use = self.resolve(ref)
            if isinstance(use, str):
                return None
            try:
                value = (
                    parse_literal(raw, "real")
                    if not raw.startswith("#")
                    else parse_literal(raw, "datetime")
                )
            except LiteralError:
                return None
            point[axis], point[f"{axis}_axis"] = value, use
        if not point:
            return None
        return AnnotationPoint.model_validate(point)

    def _tooltip_sheets(self, formatted: etree._Element, marks: Marks) -> None:
        """工具提示文字中的工作表（Viz in Tooltip）：記下名稱、大小與篩選的欄位，所有工作表解析完後
        才對應到目標工作表。工具提示的其他文字不影響數字，檢視器顯示標記的欄位與值。"""
        text = "".join(str(part) for run in children(formatted, "run") for part in run.itertext())
        for match in SHEET_TAG.finditer(text):
            attrs = dict(SHEET_ATTR.findall(match.group(1)))
            self.ctx.tooltip_refs.append(
                TooltipRef(
                    sheet=self.name,
                    marks=marks,
                    element=formatted,
                    name=attrs.get("name", ""),
                    width=_pixels(attrs.get("maxwidth")),
                    height=_pixels(attrs.get("maxheight")),
                    filter=attrs.get("filter"),
                )
            )

    def _check_label_text(self, uses: list[FieldUse], cards: list[Marks]) -> None:
        """自訂標籤中的欄位必須在標記卡或欄列架上（查詢結果才有它的值）。"""
        for marks in cards:
            missing = [run.use for run in marks.label_text if run.use and run.use not in uses]
            if missing:
                names = "、".join(self._field(u.field).caption for u in missing)
                self.ctx.flag(
                    self.el, f"工作表「{self.name}」的自訂標籤用到不在標記卡上的欄位 {names}"
                )
                marks.label_text = []

    def _formats(self, style: etree._Element | None, shelves: list[FieldUse]) -> dict[str, str]:
        """工作表上設定的數字格式：<style-rule element='cell'>（標記與文字表的值）與
        element='label'（欄列標題）中依欄位設定的 text-format。同一欄位兩者都有時，
        欄列架上的離散欄位（顯示成標題）取 label，其餘取 cell。"""
        found: dict[str, dict[str, str]] = {"cell": {}, "label": {}}
        for rule in children(style, "style-rule"):
            target = found.get(rule.get("element", ""))
            if target is None:
                continue
            for item in children(rule, "format"):
                ref, value = item.get("field"), item.get("value")
                if item.get("attr") != "text-format" or not ref or not value:
                    continue
                resolved = self.resolve(ref)
                if isinstance(resolved, str):
                    continue  # 格式設定的欄位已不在工作表上
                target.setdefault(use_key(resolved), value)
        headers = {use_key(u) for u in shelves if not u.continuous}
        cell, label = found["cell"], found["label"]
        formats: dict[str, str] = {}
        for key in sorted(cell.keys() | label.keys()):
            first, second = (label, cell) if key in headers else (cell, label)
            formats[key] = first.get(key) or second[key]
        return formats

    def _headers(self, uses: list[FieldUse]) -> list[FieldUse]:
        """架上的離散維度（總計與小計以它們為詳細程度）。"""
        assert self.ds is not None
        return [
            u
            for u in uses
            if u.field not in SPECIAL_FIELDS and not u.continuous and not self._is_measure(u)
        ]

    def _subtotals(
        self, table: etree._Element, rows: list[FieldUse], cols: list[FieldUse]
    ) -> list[FieldUse]:
        """小計：<subtotals> 列出的維度。同一架上之後還有離散維度時才有小計（最內層的維度沒有小計）；
        不在架上的維度（之前放過、已移走）略過。之後只有 Measure Names 或離散的量值時，Tableau 的
        小計怎麼畫未確認，列為不支援。"""
        assert self.ds is not None
        element = child(table, "subtotals")
        found: list[FieldUse] = []
        for column in children(element, "column"):
            use = self.resolve(instance_key(text_of(column)))
            if isinstance(use, str):
                continue
            for shelf in (rows, cols):
                headers = self._headers(shelf)
                if use not in headers:
                    continue
                inner = [u for u in shelf[shelf.index(use) + 1 :] if not u.continuous]
                if any(u in headers for u in inner):
                    if use not in found:
                        found.append(use)
                elif inner:
                    caption = self.ds.field_by_id(use.field).caption
                    self.ctx.flag(
                        column,
                        f"工作表「{self.name}」的小計：「{caption}」之後只有 Measure Names "
                        "或離散的量值，v0 不支援",
                    )
        return found

    def _visual_totals(self) -> list[VisualTotal]:
        """總計的計算方式（Total Using）：量值欄位實例的 visual-totals（Sum、Avg、Min、Max）；自動（Default）
        與沒有寫的不列出。"""
        found: list[VisualTotal] = []
        for key, inst in self.instances.items():
            raw = (inst.get("visual-totals") or "").lower()
            if not raw or raw in ("default", "automatic", "auto"):
                continue
            agg = VISUAL_TOTALS.get(raw)
            use = self.resolve(key)
            if agg is None or isinstance(use, str):
                self.ctx.flag(
                    inst,
                    f"工作表「{self.name}」的總計計算方式（Total Using：{inst.get('visual-totals')}）"
                    "無法辨識",
                )
                continue
            found.append(VisualTotal(use=use, agg=agg))
        return found

    def _totals(
        self,
        table: etree._Element,
        rows: list[FieldUse],
        cols: list[FieldUse],
    ) -> list[Literal["rows", "columns"]]:
        """總計（Grand Total）：欄列架的 total='true'。列架的總計是最下方的一列（每一欄的合計），
        欄架的總計是最右邊的一欄（每一列的合計），都在去掉該架上離散維度的詳細程度重新彙總
        （Tableau 的「自動」）。架上沒有離散維度（只有量值或 Measure Names）時沒有總計可以加。"""
        assert self.ds is not None
        totals: list[Literal["rows", "columns"]] = []
        for tag, shelf, uses in (("rows", "rows", rows), ("cols", "columns", cols)):
            element = child(table, tag)
            if element is None or element.get("total") != "true":
                continue
            if not self._headers(uses):
                self.ctx.ignore(
                    element, f"工作表「{self.name}」的總計：架上沒有離散的維度，沒有總計可加"
                )
                continue
            totals.append(shelf)  # type: ignore[arg-type]
        return totals

    @staticmethod
    def _totals_colored(style: etree._Element | None) -> bool:
        """色彩的「包含總計」：標記樣式中色彩編碼的 include-totals='true'。"""
        return any(
            enc.get("attr") == "color" and enc.get("include-totals") == "true"
            for rule in children(style, "style-rule")
            if rule.get("element") == "mark"
            for enc in children(rule, "encoding")
        )

    def _title(self, layout: etree._Element | None) -> list[TextRun]:
        """自訂的工作表標題；只有工作表名稱（<Sheet Name>）時與預設相同，回傳空的。

        標題中的欄位值與 Tableau 相同，以整張工作表計算（量值是全部資料的彙總，維度只有一個值時顯示它、
        多個值時顯示 *）；Table Calculation 與 Measure Names／Values 的值尚不支援。"""
        formatted = child(child(layout, "title"), "formatted-text")
        if formatted is None:
            return []
        what = f"工作表「{self.name}」的標題"
        runs: list[TextRun] = []
        for run in self._text_runs(formatted, what):
            use = run.use
            if use is not None and (use.table_calc is not None or use.field in SPECIAL_FIELDS):
                assert self.ds is not None
                caption = (
                    use.field
                    if use.field in SPECIAL_FIELDS
                    else self.ds.field_by_id(use.field).caption
                )
                self.ctx.flag(
                    formatted, f"{what}用到「{caption}」的 Table Calculation 值，尚不支援"
                )
                continue
            runs.append(run)
        return [] if runs == [TextRun(text=self.name)] else runs

    # ------------------------------------------------------------ Measure Names／Values

    def _measure_values(
        self, view: etree._Element, uses: list[FieldUse], panes: list[Pane]
    ) -> tuple[list[FieldUse], list[MeasureAlias]]:
        """Measure Values 卡上的量值（Measure Names 篩選選取的量值，依手動排序的順序）與其別名。"""
        values = self._measure_value_uses(view, uses, panes)
        assert self.ds is not None
        labels: dict[str, str] = {}
        for column in children(self.ds.element, "column"):
            if column.get("name") != "[:Measure Names]":
                continue
            for alias in descendants(child(column, "aliases"), "alias"):
                key, label = alias.get("key"), alias.get("value")
                if key is not None and label is not None:
                    labels[instance_key(str(parse_literal(key, "string")))] = label
        aliases = [MeasureAlias(use=use, label=labels[key]) for key, use in values if key in labels]
        return [use for _, use in values], aliases

    def _measure_value_uses(
        self, view: etree._Element, uses: list[FieldUse], panes: list[Pane]
    ) -> list[tuple[str, FieldUse]]:
        """Measure Values 卡上的量值與其欄位實例（例如 [avg:Age:qk]），依顯示順序。"""
        all_uses = uses + [u for p in panes for u in p.marks.uses()]
        used = any(u.field == MEASURE_VALUES for u in all_uses)
        names_used = any(u.field == MEASURE_NAMES for u in all_uses)
        element = next(
            (
                f
                for f in children(view, "filter")
                if instance_key(f.get("column", "")) == "[:Measure Names]"
            ),
            None,
        )
        if element is None:
            if used:
                self.ctx.flag(view, "用到 Measure Values，卻沒有 Measure Names 篩選列出量值")
            return []
        if not used and not names_used:
            return []
        refs = self._measure_name_refs(child(element, "groupfilter"))
        if refs is None:
            self.ctx.flag(element, "Measure Names 篩選的選取方式尚不支援")
            return []
        order = next(
            (
                e
                for e in children(view, "manual-sort")
                if instance_key(e.get("column", "")) == "[:Measure Names]"
            ),
            None,
        )
        if order is not None:
            ranked = [
                str(parse_literal(text_of(b), "string"))
                for b in children(child(order, "dictionary"), "bucket")
                if text_of(b) != "%all%"
            ]
            refs.sort(key=lambda r: ranked.index(r) if r in ranked else len(ranked))
        result: list[tuple[str, FieldUse]] = []
        for ref in refs:
            use = self._use(ref, element, "Measure Values 的量值")
            if use is None:
                continue
            if not self._is_measure(use):
                self.ctx.flag(element, f"Measure Values 中的 {ref} 不是量值")
                continue
            result.append((instance_key(ref), use))
        return result

    def _measure_name_refs(self, group: etree._Element | None) -> list[str] | None:
        """Measure Names 篩選選取的量值：逐一列出；「全部」（level-members）；或「全部，排除…」（except）。"""
        if group is None:
            return None
        function = group.get("function")
        if function == "level-members":
            return self._all_measure_refs()
        if function == "except":
            parts = children(group, "groupfilter")
            if len(parts) != 2 or parts[0].get("function") != "level-members":
                return None
            excluded = self._member_refs(parts[1])
            if excluded is None:
                return None
            dropped = {instance_key(r) for r in excluded}
            return [r for r in self._all_measure_refs() if instance_key(r) not in dropped]
        return self._member_refs(group)

    def _all_measure_refs(self) -> list[str]:
        """Measure Names 篩選選取全部：Measure Values 是資料源中所有的量值（各以預設的彙總），工作表的
        相依清單列出了這些量值的欄位實例。依資料窗格的順序：依顯示名稱排序，資料表的計數（例如
        「Orders (計數)」）在最後。同一個量值有多個實例時取預設的彙總（其他的是放在別處的欄位）。"""
        assert self.ds is not None
        chosen: dict[str, tuple[str, FieldUse]] = {}
        for key, inst in self.instances.items():
            if inst.get("derivation") in (None, "None") or child(inst, "table-calc") is not None:
                continue
            use = self.resolve(key)
            if isinstance(use, str) or use.field in SPECIAL_FIELDS or not self._is_measure(use):
                continue
            data_field = next((f for f in self.ds.fields if f.id == use.field), None)
            if data_field is None or data_field.role != "measure":
                continue
            default = data_field.default_agg or "sum"
            current = chosen.get(use.field)
            if current is None or (use.agg == default and current[1].agg != default):
                chosen[use.field] = (key, use)
        fields = {f.id: f for f in self.ds.fields}

        def rank(item: tuple[str, FieldUse]) -> tuple[bool, str]:
            found = fields[item[1].field]
            return (found.name.startswith(TABLE_ID_PREFIX), found.caption.casefold())

        return [f"[{self.ds.name}].{key}" for key, _ in sorted(chosen.values(), key=rank)]

    def _member_refs(self, group: etree._Element | None) -> list[str] | None:
        if group is None:
            return None
        function = group.get("function")
        if function == "member":
            return [str(parse_literal(group.get("member"), "string"))]
        if function != "union":
            return None
        refs: list[str] = []
        for part in children(group, "groupfilter"):
            found = self._member_refs(part)
            if found is None:
                return None
            refs.extend(found)
        return refs

    # ------------------------------------------------------------ Table Calculation

    def _table_calcs(
        self,
        rows: list[FieldUse],
        cols: list[FieldUse],
        mark_dims: list[FieldUse],
        pie_keys: frozenset[str] = frozenset(),
    ) -> list[TableCalc]:
        """pie_keys：圓餅的標記卡上的欄位實例（這些 Table Calculation 一併定址標記卡上的維度，見 _direction）。"""
        assert self.ds is not None
        ds = self.ds
        row_dims = [u for u in rows if u.field not in SPECIAL_FIELDS and not self._is_measure(u)]
        col_dims = [u for u in cols if u.field not in SPECIAL_FIELDS and not self._is_measure(u)]
        # 窗格內變化的維度（窗格的計算方向只定址這些）
        self._pane_dims = (self._inner(rows), self._inner(cols))
        result: list[TableCalc] = []
        # 依工作表相依清單的順序處理；只出現在篩選上的 Table Calculation 也要先建立
        for key, inst in self.instances.items():
            # 第一個沒有 field 的 <table-calc> 是這個欄位的設定；有 field 的是巢狀計算各自的計算方向
            elements = children(inst, "table-calc")
            calc_el = next((e for e in elements if e.get("field") is None), None)
            data_field = ds.by_name.get(inst.get("column", ""))
            is_formula = data_field is not None and ds.levels.get(data_field.id) == "table_calc"
            if calc_el is None and not is_formula:
                continue
            use = self.resolve(key)
            if isinstance(use, str):
                self.ctx.flag(inst, f"Table Calculation 的欄位無法解析：{use}")
                continue
            self._pie = key in pie_keys
            if calc_el is None:
                created = self._default_formula_calc(use, row_dims, col_dims, mark_dims, inst)
            else:
                created = self._table_calc(calc_el, use, row_dims, col_dims, mark_dims)
            if created is None:
                continue
            nested = [
                found
                for e in elements
                if e.get("field") is not None
                and (found := self._nested_calc(e, row_dims, col_dims, mark_dims)) is not None
            ]
            result.append(created.model_copy(update={"nested": nested}) if nested else created)
        return result

    def _nested_calc(
        self,
        calc_el: etree._Element,
        row_dims: list[FieldUse],
        col_dims: list[FieldUse],
        mark_dims: list[FieldUse],
    ) -> NestedTableCalc | None:
        """巢狀計算的計算方向：<table-calc field='[ds].[Calculation1]' ordering-type='Rows' />。"""
        assert self.ds is not None
        ref = calc_el.get("field", "")
        field_id = self._field_ref(ref)
        if field_id is None:
            self.ctx.flag(calc_el, f"巢狀計算的欄位 {ref} 找不到")
            return None
        if self.ds.levels.get(field_id) != "table_calc":
            self.ctx.ignore(calc_el, "巢狀計算的設定指向非 Table Calculation 的欄位，不影響數字")
            return None
        direction = self._direction(calc_el, row_dims, col_dims, mark_dims)
        if direction is None:
            return None
        return NestedTableCalc(
            field=field_id,
            preset=direction.preset,
            addressing=direction.addressing,
            instances=direction.instances,
            inferred=direction.inferred,
        )

    def _default_formula_calc(
        self,
        use: FieldUse,
        row_dims: list[FieldUse],
        col_dims: list[FieldUse],
        mark_dims: list[FieldUse],
        inst: etree._Element | None,
    ) -> TableCalc:
        assert self.ds is not None
        caption = self.ds.field_by_id(use.field).caption
        tc_id = self.ctx.ids.make("tc", f"formula {caption}", f"formula {use.field}")
        use.table_calc = tc_id
        return TableCalc(
            id=tc_id,
            type="formula",
            preset="table_across",
            addressing=_field_ids(col_dims + mark_dims if self._pie or not col_dims else col_dims),
            inferred=True,
            source_ref=xpath(inst) if inst is not None else xpath(self.el),
        )

    def _table_calc(
        self,
        calc_el: etree._Element,
        use: FieldUse,
        row_dims: list[FieldUse],
        col_dims: list[FieldUse],
        mark_dims: list[FieldUse],
    ) -> TableCalc | None:
        assert self.ds is not None
        ds = self.ds
        type_attr = calc_el.get("type")
        if type_attr in (None, "None", "Custom"):
            # 沒有寫類型或 Custom：計算欄位本身的 Table Calculation 公式的計算方向
            if ds.levels.get(use.field) != "table_calc":
                self.ctx.ignore(calc_el, "非 Table Calculation 欄位上的計算方向設定，不影響數字")
                return None
            calc_type = "formula"
        elif type_attr in QUICK_TYPES:
            calc_type = QUICK_TYPES[type_attr]
        else:
            self.ctx.flag(calc_el, f"快速 Table Calculation 類型 {type_attr} v0 不支援")
            return None
        options: dict[str, Scalar] = {}
        if calc_type == "running_total":
            options["aggregation"] = calc_el.get("aggregation", "Sum").lower()
            if options["aggregation"] not in ("sum", "avg", "min", "max"):
                self.ctx.flag(calc_el, f"累計的彙總方式 {options['aggregation']} v0 不支援")
                return None
        if calc_type == "moving":
            # 移動計算：from、to 為相對於目前標記的位置（含兩端），例如 -2、0 為前兩個與目前的值
            agg = calc_el.get("aggregation", "Avg").lower()
            window = [w for w in (calc_el.get("window-options") or "").split(",") if w]
            try:
                start, end = int(calc_el.get("from", "-2")), int(calc_el.get("to", "0"))
            except ValueError:
                start, end = 1, 0
            if agg not in MOVING_AGGS or start > end:
                self.ctx.flag(
                    calc_el,
                    f"移動計算的設定（{agg}，{calc_el.get('from')} 到 {calc_el.get('to')}）v0 不支援",
                )
                return None
            options.update(
                aggregation=agg,
                start=start,
                end=end,
                null_if_incomplete="NullIfIncomplete" in window,
            )
        if calc_type == "rank":
            parts = [
                p.strip() for p in calc_el.get("rank-options", "Competition,Descending").split(",")
            ]
            rank_type = RANK_TYPES.get(parts[0]) if parts else None
            if rank_type is None:
                self.ctx.flag(calc_el, f"排名方式 {calc_el.get('rank-options')} v0 不支援")
                return None
            options["rank_type"] = rank_type
            options["direction"] = "asc" if len(parts) > 1 and parts[1] == "Ascending" else "desc"
        if calc_type == "percentile":
            # 百分位數（RANK_PERCENTILE）：rank-options 的方向，預設遞增
            parts = [p.strip() for p in calc_el.get("rank-options", "").split(",")]
            options["direction"] = "desc" if "Descending" in parts else "asc"
        direction = self._direction(calc_el, row_dims, col_dims, mark_dims)
        if direction is None:
            return None
        if calc_type in DIFFERENCE_TYPES and not self._difference(calc_el, direction, options):
            return None
        level = self._level_ref(calc_el, "level-address")
        restart = self._level_ref(calc_el, "level-break")
        if level is False or restart is False:
            return None
        caption = ds.field_by_id(use.field).caption
        tc_id = self.ctx.ids.make("tc", f"{calc_type} {caption}", f"{calc_type} {use.field}")
        use.table_calc = tc_id
        return TableCalc(
            id=tc_id,
            type=calc_type,  # type: ignore[arg-type]
            preset=direction.preset,
            addressing=direction.addressing,
            instances=direction.instances,
            level=level,
            restart=restart,
            inferred=direction.inferred,
            options=options,
            source_ref=xpath(calc_el),
        )

    def _difference(
        self, calc_el: etree._Element, direction: Direction, options: dict[str, Scalar]
    ) -> bool:
        """差異、差異百分比與百分比（Percent From）的比較對象：diff-options 為 Relative 時 address 是相對位置
        （-1 為前一個）；Fixed 時是第一個、最後一個或定址欄位的某個成員。Compounded 為複合成長率。"""
        assert self.ds is not None
        flags = [f.strip() for f in calc_el.get("diff-options", "Relative").split(",") if f.strip()]
        mode = flags[0] if flags else "Relative"
        values = [text_of(v) for v in children(child(calc_el, "address"), "value")]
        if "Compounded" in flags:
            options["compounded"] = True
        if "Parameter" in flags:
            # 比較對象由參數決定：Relative 時參數是相對位置，Fixed 時是定址欄位的成員
            ref = values[0].strip() if len(values) == 1 else ""
            parts = qualified_parts(ref) if ref else []
            if len(parts) != 2 or parts[0] != "Parameters" or mode not in ("Relative", "Fixed"):
                self.ctx.flag(calc_el, f"差異計算的比較對象參數（{'、'.join(values)}）無法辨識")
                return False
            if mode == "Fixed" and len(direction.addressed) != 1:
                self.ctx.flag(calc_el, "差異計算與參數指定的成員比較時，定址的欄位必須只有一個")
                return False
            options["parameter"] = ref
            if mode == "Fixed":
                options["relative_to"] = "member"
                options["member_field"] = use_key(direction.addressed[0])
            return True
        if mode == "Relative":
            if len(values) != 1 or not re.fullmatch(r"-?\d+", values[0]):
                self.ctx.flag(
                    calc_el,
                    f"差異計算的比較對象（相對位置：{'、'.join(values) or '未指定'}）無法辨識",
                )
                return False
            options["offset"] = int(values[0])
            return True
        if mode != "Fixed" or len(values) > 1:
            self.ctx.flag(
                calc_el, f"差異計算的比較對象（{mode}：{'、'.join(values) or '未指定'}）尚不支援"
            )
            return False
        end = FIXED_ENDS.get(values[0].strip().lower()) if values else "first"
        if end is not None:
            options["relative_to"] = end
            return True
        # 定址欄位的某個成員（例如與 2019 年比較）：只有一個定址的欄位時
        targets = direction.addressed
        if len(targets) != 1:
            self.ctx.flag(calc_el, f"差異計算與成員 {values[0]} 比較時，定址的欄位必須只有一個")
            return False
        target = targets[0]
        try:
            member = parse_literal(values[0], value_type(self.ds.field_by_id(target.field), target))
        except LiteralError:
            self.ctx.flag(calc_el, f"差異計算的比較成員「{values[0]}」無法辨識")
            return False
        options["relative_to"] = "member"
        options["member"] = member
        options["member_field"] = use_key(target)
        return True

    def _level_ref(
        self, calc_el: etree._Element, attr_name: str
    ) -> FieldUse | Literal[False] | None:
        """「計算層級」（level-address）與「重新開始間隔」（level-break）的欄位：欄位名稱（所有實例）或
        欄位實例；找不到時列為不支援並回傳 False。"""
        ref = calc_el.get(attr_name)
        if not ref:
            return None
        assert self.ds is not None
        data_field = self.ds.by_name.get(instance_key(ref))
        if data_field is not None:
            return FieldUse(field=data_field.id)
        resolved = self.resolve(ref)
        if isinstance(resolved, str):
            self.ctx.flag(calc_el, f"Table Calculation 的 {attr_name} 欄位 {ref}：{resolved}")
            return False
        return FieldUse(field=resolved.field, date=resolved.date, continuous=resolved.continuous)

    def _inner(self, shelf: list[FieldUse]) -> list[FieldUse]:
        """這一架在窗格內變化的維度：最後是離散維度時為它（它切出儲存格，外層的維度切出窗格）；最後是
        連續的維度（座標軸）時為結尾連續的維度；最後是量值或 Measure Names 時沒有。"""

        def is_dim(u: FieldUse) -> bool:
            return u.field not in SPECIAL_FIELDS and not self._is_measure(u)

        if not shelf or not is_dim(shelf[-1]):
            return []
        if not shelf[-1].continuous:
            return [shelf[-1]]
        inner: list[FieldUse] = []
        for u in reversed(shelf):
            if not is_dim(u) or not u.continuous:
                break
            inner.insert(0, u)
        return inner

    def _direction(
        self,
        calc_el: etree._Element,
        row_dims: list[FieldUse],
        col_dims: list[FieldUse],
        mark_dims: list[FieldUse],
    ) -> Direction | None:
        """計算方向；設定認不得時列為不支援並回傳 None。

        表格的方向定址欄（列）架上所有的維度；窗格的方向只定址窗格內變化的維度（見 _inner）；儲存格只定址
        標記卡上的維度。標記卡上的維度是分割，只有計算方向上沒有維度時才定址它們；圓餅的標記卡上的
        Table Calculation 例外，一併定址標記卡上的維度（排在最後）。都與 Tableau 的畫面相同（見模組說明）。"""
        ordering = calc_el.get("ordering-type")
        inferred = ordering is None
        preset: TableCalcPreset
        if ordering is None:
            preset = "table_across"
        elif ordering in ORDERING:
            preset = ORDERING[ordering]
        else:
            self.ctx.flag(
                calc_el, f"Table Calculation 的計算方向 ordering-type={ordering} v0 不支援"
            )
            return None
        inner_rows, inner_cols = getattr(self, "_pane_dims", ([], []))
        shelves: dict[TableCalcPreset, list[FieldUse]] = {
            "table_across": col_dims,
            "table_down": row_dims,
            "table_across_then_down": col_dims + row_dims,
            "table_down_then_across": row_dims + col_dims,
            "pane_across": inner_cols,
            "pane_down": inner_rows,
            "pane_across_then_down": inner_cols + inner_rows,
            "pane_down_then_across": inner_rows + inner_cols,
            "cell": [],
        }
        if preset in shelves:
            shelf = shelves[preset]
            addressed = _unique_uses(shelf + mark_dims if self._pie or not shelf else shelf)
            # 表格的方向定址各欄位所有的實例；窗格與儲存格只定址其中一部分，記下實例
            exact = preset.startswith("pane") or preset == "cell"
            return Direction(
                preset, _field_ids(addressed), addressed if exact else [], inferred, addressed
            )
        refs = [r for r in [calc_el.get("ordering-field")] if r]
        refs += [o.get("field", "") for o in children(calc_el, "order")]
        view = _unique_uses(row_dims + col_dims + mark_dims)
        addressing: list[str] = []
        addressed: list[FieldUse] = []
        partial = False
        for ref in refs:
            found = self._field_ref(ref)
            if found is None:
                self.ctx.flag(calc_el, f"Table Calculation 的定址欄位 {ref} 找不到")
                return None
            if found not in addressing:
                addressing.append(found)
            assert self.ds is not None
            if self.ds.by_name.get(instance_key(ref)) is not None:
                # 欄位名稱：這個欄位在檢視中的所有實例（例如年、月）
                matches = [u for u in view if u.field == found]
            else:
                resolved = self.resolve(ref)
                matches = (
                    [u for u in view if use_key(u) == use_key(resolved)]
                    if not isinstance(resolved, str)
                    else []
                )
                partial = partial or any(u.field == found and u not in matches for u in view)
            addressed += [u for u in matches if u not in addressed]
        if not addressing:
            self.ctx.flag(calc_el, "指定維度的 Table Calculation 沒有列出定址欄位")
            return None
        return Direction(preset, addressing, addressed if partial else [], inferred, addressed)

    def _field_ref(self, ref: str) -> str | None:
        """[ds].[Region]（欄位名稱）或 [ds].[none:Region:nk]（欄位實例）→ 欄位 id。"""
        assert self.ds is not None
        key = instance_key(ref)
        data_field = self.ds.by_name.get(key)
        if data_field is not None:
            return data_field.id
        resolved = self.resolve(ref)
        return None if isinstance(resolved, str) else resolved.field

    # ------------------------------------------------------------ 篩選與排序

    def _filters(self, view: etree._Element) -> tuple[list[Filter], dict[str, FilterKind]]:
        assert self.ds is not None
        parser = FilterParser(self.ctx, self.ds, self.resolve, "worksheet")
        filters: list[Filter] = []
        kinds: dict[str, FilterKind] = {}
        cross: list[etree._Element] = []
        for element in children(view, "filter"):
            column = element.get("column", "")
            key = instance_key(column)
            if key == "[:Measure Names]":
                continue  # 決定 Measure Values 的量值，不是資料篩選
            if SPECIAL_KEYS.get(key) in GENERATED_FIELDS:
                # 地圖的「篩選資料」（未知的位置）：查詢之後找出位置時去掉；其他範圍尚不支援
                if element.get("included-values") == "non-null":
                    self.hide_unknown = True
                else:
                    self.ctx.flag(
                        element, "依地圖位置（Latitude／Longitude (generated)）的範圍篩選 v0 不支援"
                    )
                continue
            parts = qualified_parts(column)
            if len(parts) == 2 and parts[0] not in (self.ds.name, "Parameters"):
                cross.append(element)  # 其他資料源的欄位：主要資料源自己的篩選都解析後再處理
                continue
            tuple_kind = self.ds.auto_groups.get(key) or next(
                (kind for prefix, kind in TUPLE_PREFIXES.items() if key.startswith(prefix)), None
            )
            if tuple_kind is not None:
                found = self._tuple_filter(element, tuple_kind)
                if found is not None:
                    filters.append(found)
                continue
            parsed = parser.parse(element)
            if parsed is None or isinstance(parsed, TupleFilter):
                continue
            filters.append(parsed)
            kinds[key] = parsed.kind
            self._keyed[key] = parsed
        for element in cross:
            self._cross_filter(element, parser, filters, kinds)
        return filters, kinds

    def _cross_filter(
        self,
        element: etree._Element,
        parser: FilterParser,
        filters: list[Filter],
        kinds: dict[str, FilterKind],
        group: str | None = None,
    ) -> bool:
        """其他資料源欄位的篩選（Tableau 10 的跨資料源篩選，套用到相關資料源）：<target field> 是主要資料源
        中對應的欄位（依資料源的關聯），以相同的值篩選該欄位。沒有對應欄位的（次要資料源自己的篩選）
        尚不支援。

        同一欄位已有不設限、不屬於篩選群組的篩選時（Tableau 常一併放上）以跨資料源的篩選取代；否則一併
        套用（AND），篩選控制項控制跨資料源的篩選。回傳是否加入。"""
        assert self.ds is not None
        column = element.get("column", "")
        target_el = child(element, "target")
        target = target_el.get("field", "") if target_el is not None else ""
        if qualified_parts(target)[:1] != [self.ds.name]:
            self.ctx.flag(
                element,
                f"工作表「{self.name}」以其他資料源的欄位 {column} 篩選，沒有對應到主要資料源的欄位"
                "（次要資料源的篩選），v0 不支援",
            )
            return False
        key = instance_key(target)
        if key not in self.instances and key not in self.uses:
            # 對應的欄位沒有出現在工作表的欄位實例中：依欄位實例名稱推得
            resolved = use_from_name(self.ds, key)
            if isinstance(resolved, FieldUse):
                self.uses[key] = resolved
        parsed = parser.parse(element, group=group, column=target)
        if parsed is None or isinstance(parsed, TupleFilter):
            return False
        existing = self._keyed.get(key)
        index = next((i for i, f in enumerate(filters) if f is existing), None)
        if index is None:
            filters.append(parsed)
        elif existing is not None and existing.group is None and _unrestricted(existing):
            filters[index] = parsed
        else:
            # 放在前面：工作表的篩選狀態以欄位為鍵，套用到同一欄位的第一個篩選（跨資料源篩選的控制項）
            filters.insert(index, parsed)
        kinds[key] = parsed.kind
        self._keyed[key] = parsed
        parts = qualified_parts(column)
        self.cross[f"[{parts[0]}].{instance_key(column)}"] = key
        return True

    def _tuple_filter(self, element: etree._Element, kind: str) -> TupleFilter | None:
        """篩選動作、工具提示、排除與只保留的篩選：依多個欄位的值組合篩選。"""
        assert self.ds is not None
        root = child(element, "groupfilter")
        tree = _tuple_tree(root) if root is not None else None
        if tree is None:
            self.ctx.flag(element, "篩選動作或排除的篩選結構 v0 不支援")
            return None
        mode, levels, raw = tree
        uses: list[FieldUse] = []
        for level in levels:
            key = instance_key(level)
            if key in self.instances or key in self.uses:
                resolved = self.resolve(key)
            elif key in self.ds.by_name:
                resolved = FieldUse(field=self.ds.by_name[key].id)
            else:
                resolved = use_from_name(self.ds, key)
            if isinstance(resolved, str):
                self.ctx.flag(element, f"多欄位篩選的欄位無法解析：{resolved}")
                return None
            uses.append(resolved)
        types = [value_type(self.ds.field_by_id(u.field), u) for u in uses]
        try:
            tuples = [
                [parse_literal(value, t) for value, t in zip(row, types, strict=True)]
                for row in raw
            ]
        except LiteralError as exc:
            self.ctx.flag(element, f"多欄位篩選的值無法解析：{exc}")
            return None
        action: str | None = None
        stale = False
        if kind == "sheet_link":
            name = attr(root, "ui-action-filter") if root is not None else None
            action = self.ctx.action_ids.get(name or "")
            stale = action is None
        caption = element.get("column", "")
        found = TupleFilter(
            kind="tuple",
            id=self.ctx.ids.make("flt", instance_key(caption).strip("[]"), caption),
            fields=uses,
            stage="context" if element.get("context") == "true" else "dimension",
            mode=mode,  # type: ignore[arg-type]
            tuples=tuples,
            action=action,
            # 工具提示中的工作表的篩選：在來源工作表的工具提示中改以滑過的標記篩選
            tooltip=kind == "vit",
            source_ref=xpath(element),
        )
        if stale:
            self.ctx.stale_action_filters.append((self.name, found.id))
        return found

    def _shared_filters(
        self,
        view: etree._Element,
        ds_name: str,
        filters: list[Filter],
        kinds: dict[str, FilterKind],
    ) -> None:
        """篩選架（<slices>）上沒有本地 <filter> 的欄位，套用該資料源的全域篩選。"""
        shared = self.ctx.shared_views.get(ds_name)
        if shared is None:
            return
        assert self.ds is not None
        parser = FilterParser(self.ctx, self.ds, self.resolve, "worksheet")
        related: list[tuple[etree._Element, str, str]] = []
        for column in children(child(view, "slices"), "column"):
            parts = qualified_parts(text_of(column))
            key = instance_key(text_of(column))
            if len(parts) == 2 and parts[0] != ds_name:
                related.append((column, parts[0], key))  # 主要資料源自己的全域篩選之後再處理
                continue
            element = shared.filters.get(key)
            if element is None or key in kinds or key in shared.failed:
                continue
            if key not in self.instances and key in shared.instances:
                self.instances[key] = shared.instances[key]
            parsed = parser.parse(element)
            if parsed is None or isinstance(parsed, TupleFilter):
                shared.failed.add(key)
                continue
            parsed = parsed.model_copy(
                update={"group": f"shared:{self.ds.id}:{use_key(parsed.field)}"}
            )
            filters.append(parsed)
            kinds[key] = parsed.kind
            self._keyed[key] = parsed
        for column, name, key in related:
            self._related_filter(column, name, key, shared, parser, filters, kinds)

    def _related_filter(
        self,
        column: etree._Element,
        name: str,
        key: str,
        shared: SharedView,
        parser: FilterParser,
        filters: list[Filter],
        kinds: dict[str, FilterKind],
    ) -> None:
        """篩選架上其他資料源的欄位：主要資料源的全域篩選中「套用到所有使用相關資料源的工作表」的篩選
        （跨資料源篩選），與該資料源自己的工作表屬於同一篩選群組，篩選控制項一起更新。"""
        foreign = f"[{name}].{key}"
        element = shared.related.get(foreign)
        if element is None:
            other = self.ctx.shared_views.get(name)
            if other is not None and key in other.filters:
                self.ctx.flag(
                    column,
                    f"工作表「{self.name}」套用次要資料源的全域篩選（Data Blending），v0 不支援",
                )
            return
        if foreign in self.cross or foreign in shared.failed:
            return  # 工作表上有本地的跨資料源篩選
        target_el = child(element, "target")
        target_key = instance_key(target_el.get("field", "")) if target_el is not None else ""
        if target_key and target_key not in self.instances and target_key in shared.instances:
            self.instances[target_key] = shared.instances[target_key]
        if not self._cross_filter(
            element, parser, filters, kinds, self._related_group(name, key, shared)
        ):
            shared.failed.add(foreign)

    def _related_group(self, name: str, key: str, shared: SharedView) -> str:
        """跨資料源篩選的群組：與篩選欄位所屬資料源的全域篩選相同（見 _shared_filters）。"""
        state = self.ctx.datasources.get(name)
        if state is None:
            return f"shared:{name}:{key}"
        inst = shared.foreign_instances.get(name, {}).get(key)
        if inst is None:
            own = self.ctx.shared_views.get(name)
            inst = own.instances.get(key) if own is not None else None
        use = use_from_instance(state, inst) if inst is not None else use_from_name(state, key)
        if isinstance(use, str):
            return f"shared:{state.id}:{key}"
        return f"shared:{state.id}:{use_key(use)}"

    def _sorts(self, view: etree._Element) -> list[Sort]:
        sorts: list[Sort] = []
        for element in children(view):
            tag = local_name(element)
            if instance_key(element.get("column", "")) == "[:Measure Names]":
                continue  # Measure Names 的順序已反映在 measure_values
            try:
                if tag in ("sort", "computed-sort"):
                    parsed = self._sort(element, tag)
                elif tag == "manual-sort":
                    parsed = self._manual(element)
                elif tag in ("natural-sort", "alphabetic-sort"):
                    # 依字母排序（alphabetic-sort 是較舊版本的寫法）
                    parsed = self._simple(element, element.get("column"), "alphabetic")
                elif tag == "shelf-sorts":
                    for item in children(element, "shelf-sort-v2"):
                        found = self._by_field(
                            item,
                            item.get("dimension-to-sort"),
                            item.get("measure-to-sort-by"),
                            item.get("direction"),
                        )
                        if found is not None:
                            sorts.append(found)
                    continue
                elif tag in ("rows", "cols"):
                    # 較舊版本的依量值排序（工具列的排序按鈕）：<rows><shelf-sort measure-to-sort-by>
                    for item in children(element, "shelf-sort"):
                        for dim in children(child(item, "dimensions-to-sort"), "dimension"):
                            found = self._by_field(
                                item,
                                text_of(dim),
                                item.get("measure-to-sort-by"),
                                item.get("direction"),
                            )
                            if found is not None:
                                sorts.append(found)
                    continue
                else:
                    continue
            except LiteralError as exc:
                self.ctx.flag(element, f"排序值無法解析：{exc}")
                continue
            if parsed is not None:
                sorts.append(parsed)
        return sorts

    def _sort(self, element: etree._Element, tag: str) -> Sort | None:
        sort_class = "computed" if tag == "computed-sort" else element.get("class", "")
        column = element.get("column")
        if sort_class == "computed":
            return self._by_field(element, column, element.get("using"), element.get("direction"))
        if sort_class == "alphabetic":
            return self._simple(element, column, "alphabetic")
        if sort_class == "datasource":
            return self._simple(element, column, "data_source")
        if sort_class == "natural":
            return self._simple(element, column, "alphabetic")
        if sort_class == "manual":
            return self._manual(element)
        self.ctx.flag(element, f"排序方式 {sort_class} v0 不支援")
        return None

    def _by_field(
        self,
        element: etree._Element,
        column: str | None,
        using: str | None,
        direction: str | None,
    ) -> Sort | None:
        dim = self._use(column, element, "排序的維度")
        by = self._use(using, element, "排序依據")
        if dim is None or by is None:
            return None
        return Sort(field=dim.field, kind="by_field", order=_order(direction), by=by)

    def _simple(
        self,
        element: etree._Element,
        column: str | None,
        kind: Literal["alphabetic", "data_source"],
    ) -> Sort | None:
        dim = self._use(column, element, "排序的維度")
        if dim is None:
            return None
        return Sort(field=dim.field, kind=kind, order=_order(element.get("direction")))

    def _manual(self, element: etree._Element) -> Sort | None:
        dim = self._use(element.get("column"), element, "手動排序的維度")
        if dim is None:
            return None
        assert self.ds is not None
        datatype = value_type(self.ds.field_by_id(dim.field), dim)
        values = bucket_values(child(element, "dictionary"), datatype)
        if not values:
            return None
        return Sort(
            field=dim.field,
            kind="manual",
            order=_order(element.get("direction")),
            manual_values=values,
        )

    # ------------------------------------------------------------ 未知元素

    def _check_elements(self, element: etree._Element) -> None:
        for item in children(element):
            tag = local_name(item)
            if tag in OPAQUE:
                continue
            if tag in IGNORED_ELEMENTS:
                self.ctx.ignore(item, IGNORED_ELEMENTS[tag])
                continue
            if tag == "dropline":
                # Drop Lines：預設只在選取標記時顯示
                if item.get("drop-when") == "always":
                    self.ctx.flag(item, "一律顯示的 Drop Lines v0 不支援")
                else:
                    self.ctx.ignore(item, "Drop Lines（選取標記時才顯示）：新平台不顯示")
                continue
            if tag == "preset" and _latest_preset(item):
                continue  # 類別篩選的「開啟報表時篩選到最新的值」：CategoricalFilter.latest
            if tag == "forecast-specification":
                self._check_forecast(item)
                continue
            if tag in KNOWN_UNSUPPORTED:
                self.ctx.flag(item, f"{KNOWN_UNSUPPORTED[tag]} v0 不支援")
                continue
            if tag not in KNOWN:
                self.ctx.flag(item, f"工作表中的未知元素 <{tag}>")
                continue
            self._check_elements(item)

    def _check_forecast(self, item: etree._Element) -> None:
        """<forecast-specification>：工作表 <table> 之下開啟的預測由 _forecast 解析（Worksheet.forecast）。"""
        parent = item.getparent()
        if parent is None or local_name(parent) != "table":
            self.ctx.flag(
                item, "預測 v0 不支援：<forecast-specification> 不在工作表的 <table> 之下"
            )
        elif item.get("enabled") == "false":
            # 曾經開過又關掉的預測：只留下設定，畫面上沒有預測
            self.ctx.ignore(item, "預測已關閉（只留下設定）：略過")
        elif item.get("enabled") != "true":
            self.ctx.flag(item, "預測 v0 不支援：<forecast-specification> 沒有標明是否開啟")


def _latest_preset(preset: etree._Element) -> bool:
    """類別篩選的「開啟報表時篩選到最新的值」（解析成 CategoricalFilter.latest）。"""
    parent = preset.getparent()
    return (
        preset.get("type") == "last-values"
        and parent is not None
        and local_name(parent) == "filter"
        and parent.get("class") == "categorical"
    )


TupleTree = tuple[str, list[str], list[list[str | None]]]


def _tuple_tree(node: etree._Element) -> TupleTree | None:
    """groupfilter 樹 → (mode, 欄位層級, 值組合)。成員值保留 Tableau 的常值寫法。"""
    function = node.get("function")
    parts = children(node, "groupfilter")
    if function == "level-members":
        return "all", [node.get("level", "")], []
    if function == "empty-level":
        return "include", [node.get("member") or node.get("level", "")], []
    if function == "member":
        return "include", [node.get("level", "")], [[node.get("member")]]
    if function == "crossjoin" and parts:
        kinds = {p.get("function") for p in parts}
        levels = [p.get("level", "") for p in parts]
        if kinds == {"level-members"}:
            return "all", levels, []
        if kinds == {"empty-level"}:
            # 清除選取時排除所有值的篩選動作：層級寫在 member 屬性
            return "include", [p.get("member") or p.get("level", "") for p in parts], []
        # 各層級的成員（單一成員或多個成員的 union）：值組合為各層級成員的所有組合
        columns: list[list[str | None]] = []
        levels = []
        for part in parts:
            sub = _tuple_tree(part)
            if sub is None or sub[0] != "include" or len(sub[1]) != 1:
                return None
            levels.append(sub[1][0])
            columns.append([row[0] for row in sub[2]])
        return "include", levels, [list(combo) for combo in itertools.product(*columns)]
    if function == "union" and parts:
        found_levels: list[str] | None = None
        rows: list[list[str | None]] = []
        for part in parts:
            sub = _tuple_tree(part)
            if sub is None or sub[0] != "include":
                return None
            if found_levels is None:
                found_levels = sub[1]
            elif sub[1] != found_levels:
                return None
            rows += sub[2]
        return "include", found_levels or [], rows
    if function == "except" and parts:
        first = _tuple_tree(parts[0])
        if first is None or first[0] != "all":
            return None
        excluded: list[list[str | None]] = []
        for part in parts[1:]:
            sub = _tuple_tree(part)
            if sub is None or sub[0] != "include" or sub[1] != first[1]:
                return None
            excluded += sub[2]
        return "exclude", first[1], excluded
    return None


def _unrestricted(flt: Filter) -> bool:
    """不設限的篩選（全部的值，含 Null）。"""
    if isinstance(flt, CategoricalFilter):
        return flt.mode == "all"
    if isinstance(flt, RangeFilter):
        return flt.min is None and flt.max is None and flt.include_null
    return False


def _is_blank(table: etree._Element | None) -> bool:
    """欄列架與標記卡都是空的工作表（常用來當作標題或留白）。"""
    if table is None:
        return True
    if text_of(child(table, "rows")) or text_of(child(table, "cols")):
        return False
    return not any(
        len(children(child(p, "encodings"))) for p in children(child(table, "panes"), "pane")
    )


def _pane_signature(pane: etree._Element) -> tuple[str, tuple[tuple[str, str], ...]]:
    mark_el = child(pane, "mark")
    mark = mark_el.get("class", "") if mark_el is not None else ""
    encodings = tuple(
        (local_name(e), e.get("column", "")) for e in children(child(pane, "encodings"))
    )
    return mark, encodings


@dataclass(frozen=True)
class Direction:
    """解析後的計算方向：instances 只在需要區分同一欄位的實例時有值；addressed 為定址的實例（檢視中）。"""

    preset: TableCalcPreset
    addressing: list[str]
    instances: list[FieldUse]
    inferred: bool
    addressed: list[FieldUse]


def _pie_keys(panes: etree._Element | None) -> frozenset[str]:
    """圓餅標記卡（mark class='Pie'）上的欄位實例（[usr:Calculation1:qk:8] 等）。"""
    keys: set[str] = set()
    for pane in children(panes, "pane"):
        mark_el = child(pane, "mark")
        if mark_el is None or mark_el.get("class") != "Pie":
            continue
        for enc in children(child(pane, "encodings")):
            column = enc.get("column")
            if column:
                keys.add(instance_key(column))
    return frozenset(keys)


def _unique_uses(uses: list[FieldUse]) -> list[FieldUse]:
    result: list[FieldUse] = []
    keys: set[str] = set()
    for use in uses:
        key = use_key(use)
        if key not in keys:
            keys.add(key)
            result.append(use)
    return result


def _field_ids(uses: list[FieldUse]) -> list[str]:
    result: list[str] = []
    for use in uses:
        if use.field not in result:
            result.append(use.field)
    return result


def _order(direction: str | None) -> Literal["asc", "desc"]:
    return "desc" if (direction or "").upper() == "DESC" else "asc"


def _pixels(value: str | None, default: int = 300) -> int:
    """工具提示中工作表的最大寬、高（像素）；沒寫或不是正數時為 Tableau 的預設 300。"""
    try:
        number = int(float(value)) if value else default
    except ValueError:
        return default
    return number if number > 0 else default


def _number(value: str | None) -> int:
    """註解文字的偏移（像素）；沒寫或不是數字時為 0。"""
    try:
        return round(float(value)) if value else 0
    except ValueError:
        return 0
