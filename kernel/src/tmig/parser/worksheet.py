"""工作表：欄列架、標記、篩選、排序與 Table Calculation（A4）。

Table Calculation 的計算方向（Tableau 的 Compute Using）依真實 .twb 歸納：

- ``ordering-type='Columns'`` 為 Table (down)：沿著欄往下算，定址為列架上的維度與標記卡上的維度
- ``ordering-type='Rows'`` 為 Table (across)：沿著列往右算，定址為欄架上的維度與標記卡上的維度
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
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from lxml import etree

from tmig.ir.models import (
    MEASURE_NAMES,
    MEASURE_VALUES,
    SPECIAL_FIELDS,
    Blend,
    BlendLink,
    ColorPalette,
    DataField,
    FieldUse,
    Filter,
    Marks,
    MarkType,
    MeasureAlias,
    NestedTableCalc,
    Pane,
    ParamRef,
    ReferenceLine,
    Scalar,
    Sort,
    TableCalc,
    TableCalcPreset,
    TextRun,
    TupleFilter,
    VisualTotal,
    Worksheet,
)
from tmig.parser.colors import color_encodings, hex_color, label_color, mark_style, parse_color
from tmig.parser.context import DsState, FilterKind, ParseContext, SheetState
from tmig.parser.datasource import add_calc_field, analyze_calcs
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
    pane_mark_size,
    pane_trend_line,
    reference_styles,
    worksheet_formats,
)
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import attr, child, children, descendants, local_name, text_of, xpath
from tmig.parser.values import LiteralError, parse_literal
from tmig.planner.state import use_key

TEXT_TOKEN = re.compile(r"<([^<>]+)>")
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
}
SPECIAL_KEYS = {"[:Measure Names]": MEASURE_NAMES, "[Multiple Values]": MEASURE_VALUES}
TUPLE_PREFIXES = {
    "[Action (": "sheet_link",
    "[Tooltip (": "vit",
    "[Exclusions (": "exclude",
    "[Inclusions (": "include",
}
"""Tableau 自動產生的多欄位篩選欄位（資料源 <group> 沒寫 auto-column 時依名稱判斷）。"""
ENCODINGS = ("color", "size", "text", "lod", "tooltip", "shape", "wedge-size", "path")
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
        "dictionary",
        "bucket",
        "shelf-sorts",
        "shelf-sort-v2",
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
        # 趨勢線（不分組的因子 <excluded-factors><column>）
        "trendline",
        "excluded-factors",
    }
)
OPAQUE = frozenset(
    {
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
    "forecast-specification": "預測",
    "mapsources": "地圖",
    "mapsource": "地圖",
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
        self.uses: dict[str, FieldUse] = {}
        self.ds: DsState | None = None
        self.secondaries: dict[str, DsState] = {}
        """Data Blending 的次要資料源：名稱 → 狀態，依 <datasources> 的順序（第一個是主要資料源）。"""
        self.secondary_instances: dict[str, dict[str, etree._Element]] = {}
        self._blending = False
        """目前解析的位置可以用次要資料源的欄位（欄列架、標記卡與其中的文字、數字格式）。"""
        self._colors: dict[str, etree._Element] | None = None
        """色彩編碼（工作表與資料源的 <style>）：欄位實例鍵 → <encoding>。"""

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
        pages = child(table, "pages")
        if pages is not None and (text_of(pages) or len(children(pages))):
            ctx.flag(pages, "頁面架（Pages）v0 不支援")
        if view is not None:
            aggregation = child(view, "aggregation")
            if aggregation is not None and aggregation.get("value") == "false":
                ctx.flag(aggregation, "取消彙總量值（Aggregate Measures 關閉）v0 不支援")
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
        blends = self._blends(view, [*card_uses, *measure_values], bool(totals or subtotals))
        if blends is None:
            return None
        sheet_id = ctx.ids.make("ws", self.name)
        table_calcs = self._table_calcs(rows, cols, mark_dims)
        filters, kinds = self._filters(view)
        self._shared_filters(view, main[0], filters, kinds)
        sorts = self._sorts(view)
        reference_lines = self._reference_lines(child(table, "panes"), child(table, "style"))
        with self._blend_scope():
            formats = self._formats(child(table, "style"), [*rows, *cols])
        sheet_formats = worksheet_formats(
            child(table, "style"), self.resolve, rows, cols, child(ctx.package.root, "style")
        )
        title = self._title(child(el, "layout-options"))
        self._check_label_text(card_uses, [marks, *(p.marks for p in panes)])
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
            cell_align=sheet_formats.cell_align,
            field_label_background=sheet_formats.field_label_background,
            fonts=sheet_formats.fonts,
            label_valign=sheet_formats.label_valign,
            fit=ctx.sheet_fits.get(self.name, "standard"),
            blends=blends,
            source_ref=xpath(el),
        )
        ctx.sheets[self.name] = SheetState(
            id=sheet_id,
            name=self.name,
            ds=ds,
            instances=self.instances,
            uses=self.uses,
            filter_kinds=kinds,
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
                if name in state.by_name or child(column, "calculation") is None:
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
            use = FieldUse(field=SPECIAL_KEYS[key])
            self.uses[key] = use
            return use
        inst = self.instances.get(key)
        if inst is None:
            data_field = self.ds.by_name.get(key)
            if data_field is None:
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

    # ------------------------------------------------------------ Data Blending

    def _blends(
        self, view: etree._Element, uses: list[FieldUse], totals: bool
    ) -> list[Blend] | None:
        """次要資料源與連結欄位。自訂關聯（<datasource-relationships>）優先，沒有時以同名的維度自動連結；
        檢視中要有連結欄位（作用中的連結），否則無法混合。"""
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
            if not any(link.secondary_for(d) is not None for link in links for d in dims):
                self.ctx.flag(view, f"{label}：檢視中沒有連結欄位（沒有作用中的連結）")
                return None
            blends.append(Blend(datasource=secondary.id, links=links))
        return blends

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
            if measure.field in SPECIAL_FIELDS:
                # 例如 Measure Values 與另一個量值疊成雙軸：Measure Values 的軸有自己的標記卡
                self.ctx.flag(
                    element, "Measure Values 的軸有個別的標記卡（例如與其他量值疊成雙軸），尚不支援"
                )
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
                if getattr(marks, attr_name) is not None:
                    self.ctx.flag(enc, f"標記編碼 {tag} 重複設定")
                    continue
                setattr(marks, attr_name, use)
                if tag == "color":
                    color_ref = enc.get("column")
            elif tag == "path":
                marks.path = use
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
        if marks.color is not None and color_ref is not None:
            marks.color_palette = self._color_palette(color_ref, marks.color)
        marks.mark_color, marks.opacity, marks.stroke = mark_style(pane)
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
        return marks, dims

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
        """格式化文字（<formatted-text> 的各段 <run>）→ 文字與參照依序排列；樣式（字型、顏色）不保留。

        參照可能被拆在相鄰的幾段中，所以先串成一段再找；認不得的 <…> 照原文保留。
        """
        text = "".join(str(part) for run in children(formatted, "run") for part in run.itertext())
        # 單獨的 Æ 是格式化文字的分段記號，畫面上不顯示
        text = text.replace(LINE_BREAK, "\n").replace("\u00c6", "").rstrip()
        runs: list[TextRun] = []

        def add_text(value: str) -> None:
            if not value:
                return
            if runs and runs[-1].text is not None:
                runs[-1] = TextRun(text=runs[-1].text + value)
            else:
                runs.append(TextRun(text=value))

        position = 0
        for match in TEXT_TOKEN.finditer(text):
            add_text(text[position : match.start()])
            position = match.end()
            token = match.group(1)
            if token == "Sheet Name":
                add_text(self.name)
                continue
            parts = qualified_parts(token)
            if len(parts) == 2 and parts[0] == "Parameters":
                param = self.ctx.parameter(token)
                if param is None:
                    self.ctx.flag(formatted, f"{what}：找不到參數 {token}")
                    continue
                runs.append(TextRun(parameter=param.id))
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
                runs.append(TextRun(use=resolved))
                continue
            add_text(match.group(0))
        add_text(text[position:])
        return runs

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
        refs = self._member_refs(child(element, "groupfilter"))
        if refs is None:
            self.ctx.flag(element, "Measure Names 篩選不是逐一列出量值（例如「全部」），尚不支援")
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
        self, rows: list[FieldUse], cols: list[FieldUse], mark_dims: list[FieldUse]
    ) -> list[TableCalc]:
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
            addressing=_field_ids(col_dims + mark_dims),
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
        標記卡上的維度。標記卡上的維度一律在定址的最後（與 Tableau 相同）。"""
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
            addressed = _unique_uses(shelves[preset] + mark_dims)
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
        for element in children(view, "filter"):
            key = instance_key(element.get("column", ""))
            if key == "[:Measure Names]":
                continue  # 決定 Measure Values 的量值，不是資料篩選
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
            kinds[instance_key(element.get("column", ""))] = parsed.kind
        return filters, kinds

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
        elif kind == "vit" and "vit" not in self.ctx.notes:
            self.ctx.notes.add("vit")
            self.ctx.ignore(
                element,
                "工具提示中的工作表（Viz in Tooltip）：以一般工作表呈現，工具提示不顯示圖表",
            )
        caption = element.get("column", "")
        found = TupleFilter(
            kind="tuple",
            id=self.ctx.ids.make("flt", instance_key(caption).strip("[]"), caption),
            fields=uses,
            stage="context" if element.get("context") == "true" else "dimension",
            mode=mode,  # type: ignore[arg-type]
            tuples=tuples,
            action=action,
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
        for column in children(child(view, "slices"), "column"):
            key = instance_key(text_of(column))
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
                elif tag == "natural-sort":
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
            if tag in KNOWN_UNSUPPORTED:
                self.ctx.flag(item, f"{KNOWN_UNSUPPORTED[tag]} v0 不支援")
                continue
            if tag not in KNOWN:
                self.ctx.flag(item, f"工作表中的未知元素 <{tag}>")
                continue
            self._check_elements(item)


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
        if kinds == {"member"}:
            return "include", levels, [[p.get("member") for p in parts]]
        if kinds == {"empty-level"}:
            # 清除選取時排除所有值的篩選動作：層級寫在 member 屬性
            return "include", [p.get("member") or p.get("level", "") for p in parts], []
        return None
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
