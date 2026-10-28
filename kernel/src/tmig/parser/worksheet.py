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

import re
from typing import Literal

from lxml import etree

from tmig.ir.models import (
    MEASURE_NAMES,
    MEASURE_VALUES,
    SPECIAL_FIELDS,
    FieldUse,
    Filter,
    Marks,
    MarkType,
    Pane,
    ParamRef,
    ReferenceLine,
    Scalar,
    Sort,
    TableCalc,
    TableCalcPreset,
    TupleFilter,
    Worksheet,
)
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
from tmig.parser.twbx import attr, child, children, local_name, text_of, xpath
from tmig.parser.values import LiteralError, parse_literal
from tmig.planner.state import use_key

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
    "Field": "specific",
}
QUICK_TYPES = {
    "CumTotal": "running_total",
    "PctTotal": "percent_of_total",
    "Rank": "rank",
    "Difference": "difference",
    "PctDiff": "percent_difference",
    "WindowTotal": "moving",
}
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
    {"constant", "average", "sum", "total", "min", "max", "median", "percentile", "stdev"}
)
REFERENCE_SCOPES = {"per-table": "table", "per-pane": "pane", "per-cell": "cell"}
REFERENCE_LABELS = frozenset({"none", "automatic", "value", "computation", "custom"})
KNOWN_UNSUPPORTED = {
    "trend-line": "趨勢線",
    "forecast-specification": "預測",
    "subtotals": "小計與總計",
    "mapsources": "地圖",
    "mapsource": "地圖",
    "add-in": "工作表擴充功能",
}


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
        if len(main) > 1:
            ctx.flag(
                view, f"工作表「{self.name}」使用 Data Blending（{'、'.join(main)}），v0 不支援"
            )
            return None
        ds = ctx.datasources.get(main[0])
        if ds is None:
            ctx.flag(el, f"工作表「{self.name}」的資料源 {main[0]} 無法移轉")
            return None
        self.ds = ds
        self._dependencies(view, main[0])
        self._check_elements(el)

        rows = self._shelf(child(table, "rows"))
        cols = self._shelf(child(table, "cols"))
        pages = child(table, "pages")
        if pages is not None and (text_of(pages) or len(children(pages))):
            ctx.flag(pages, "頁面架（Pages）v0 不支援")
        if view is not None:
            aggregation = child(view, "aggregation")
            if aggregation is not None and aggregation.get("value") == "false":
                ctx.flag(aggregation, "取消彙總量值（Aggregate Measures 關閉）v0 不支援")
        marks, panes, mark_dims = self._marks(child(table, "panes"), rows, cols)
        measure_values = self._measure_values(view, [*rows, *cols, *marks.uses()], panes)
        card_uses = [*rows, *cols, *marks.uses(), *(u for p in panes for u in p.marks.uses())]
        if not measure_values and any(u.field == MEASURE_VALUES for u in card_uses):
            return None  # 無法得知 Measure Values 的量值（已列為不支援）
        sheet_id = ctx.ids.make("ws", self.name)
        table_calcs = self._table_calcs(rows, cols, mark_dims)
        filters, kinds = self._filters(view)
        self._shared_filters(view, main[0], filters, kinds)
        sorts = self._sorts(view)
        reference_lines = self._reference_lines(child(table, "panes"))
        worksheet = Worksheet(
            id=sheet_id,
            name=self.name,
            datasource=ds.id,
            rows=rows,
            columns=cols,
            marks=marks,
            panes=panes,
            measure_values=measure_values,
            filters=filters,
            sorts=sorts,
            table_calcs=table_calcs,
            reference_lines=reference_lines,
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

    def _dependencies(self, view: etree._Element, ds_name: str) -> None:
        assert self.ds is not None
        added = False
        for dep in children(view, "datasource-dependencies"):
            if dep.get("datasource") != ds_name:
                continue
            for column in children(dep, "column"):
                name = column.get("name", "")
                if name in self.ds.by_name or child(column, "calculation") is None:
                    continue
                # 直接在欄列架上輸入的臨時計算，只存在工作表裡
                if add_calc_field(self.ctx, self.ds, column) is not None:
                    added = True
            for inst in children(dep, "column-instance"):
                # 物件模型的欄位實例名稱含資料表前綴，例如 [__tableau_internal_object_id__].[cnt:…:qk]
                self.instances[instance_key(inst.get("name", ""))] = inst
        if added:
            analyze_calcs(self.ctx, self.ds)

    def resolve(self, ref: str) -> FieldUse | str:
        assert self.ds is not None
        from tmig.parser.refs import qualified_parts

        parts = qualified_parts(ref)
        if len(parts) == 2 and parts[0] not in (self.ds.name, "Parameters"):
            return f"參照了其他資料源 {parts[0]}"
        if parts and parts[0] == "Parameters":
            return f"欄列架上的參數 {ref} v0 不支援"
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

    def _reference_lines(self, panes_el: etree._Element | None) -> list[ReferenceLine]:
        lines: list[ReferenceLine] = []
        for pane in children(panes_el, "pane"):
            for element in children(pane, "reference-line"):
                line = self._reference_line(element, len(lines))
                if line is not None:
                    lines.append(line)
        return lines

    def _reference_line(self, element: etree._Element, index: int) -> ReferenceLine | None:
        """參考線：常數、參數或標記值的彙總；新平台畫不出來的種類記為不影響數字。"""
        ctx = self.ctx
        formula = element.get("formula", "")
        scope = REFERENCE_SCOPES.get(element.get("scope", ""))
        if formula not in REFERENCE_FORMULAS or scope is None:
            ctx.ignore(
                element,
                f"參考線、參考區間或分布（{formula}，{element.get('scope')}）新平台不畫（不影響數字）",
            )
            return None
        if element.get("paired-id") or element.get("paired-distribution-id"):
            ctx.ignore(element, "參考區間的底色新平台不畫，只畫兩端的參考線（不影響數字）")
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
        if any(d.get("percentage") for d in details):
            ctx.ignore(element, "百分比分布（彙總值的幾成）新平台不畫（不影響數字）")
            return None
        percentiles: list[float] = []
        factors: list[float] = []
        try:
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
            ctx.flag(element, "參考線的百分位數或標準差倍數無法解析")
            return None
        label = element.get("label-type", "automatic")
        return ReferenceLine(
            id=element.get("id") or f"refline{index}",
            axis=axis,
            value=value,
            formula=formula,  # type: ignore[arg-type]
            constant=constant,
            percentiles=percentiles,
            factors=factors,
            sample=element.get("type") != "population",
            scope=scope,  # type: ignore[arg-type]
            label=label if label in REFERENCE_LABELS else "automatic",  # type: ignore[arg-type]
            label_text=element.get("label") if label == "custom" else None,
            source_ref=xpath(element),
        )

    def _marks(
        self, panes_el: etree._Element | None, rows: list[FieldUse], cols: list[FieldUse]
    ) -> tuple[Marks, list[Pane], list[FieldUse]]:
        """標記卡：沒有 id 的 <pane> 是共用的標記卡，帶 y-axis-name／x-axis-name 的是個別量值的標記卡。"""
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
            if measure not in (rows if axis == "rows" else cols):
                self.ctx.flag(element, f"標記卡的量值 {axis_ref} 不在欄列架上")
                continue
            card, card_dims = self._card(element)
            dual = (element.get("y-index") or element.get("x-index")) == "1"
            result.append(Pane(measure=measure, axis=axis, dual=dual, marks=card))
            pane_dims.append(card_dims)
        if result:
            signatures = {frozenset(use_key(d) for d in found) for found in pane_dims}
            if len(signatures) > 1:
                self.ctx.flag(
                    panes_el if panes_el is not None else self.el,
                    "各量值的標記卡維度不同（各量值的詳細程度不同），尚不支援",
                )
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
            elif tag == "path":
                marks.path = use
            elif tag == "text":
                marks.label.append(use)
            elif tag == "lod":
                marks.detail.append(use)
            else:
                marks.tooltip.append(use)
            assert self.ds is not None
            if use.field not in SPECIAL_FIELDS and not is_aggregate(self.ds, use):
                dims.append(use)
        return marks, dims

    # ------------------------------------------------------------ Measure Names／Values

    def _measure_values(
        self, view: etree._Element, uses: list[FieldUse], panes: list[Pane]
    ) -> list[FieldUse]:
        """Measure Values 卡上的量值：Measure Names 篩選選取的量值，依手動排序的順序。"""
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
        result: list[FieldUse] = []
        for ref in refs:
            use = self._use(ref, element, "Measure Values 的量值")
            if use is None:
                continue
            assert self.ds is not None
            if not is_aggregate(self.ds, use):
                self.ctx.flag(element, f"Measure Values 中的 {ref} 不是量值")
                continue
            result.append(use)
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
        row_dims = [u for u in rows if u.field not in SPECIAL_FIELDS and not is_aggregate(ds, u)]
        col_dims = [u for u in cols if u.field not in SPECIAL_FIELDS and not is_aggregate(ds, u)]
        result: list[TableCalc] = []
        # 依工作表相依清單的順序處理；只出現在篩選上的 Table Calculation 也要先建立
        for key, inst in self.instances.items():
            calc_el = child(inst, "table-calc")
            data_field = ds.by_name.get(inst.get("column", ""))
            is_formula = data_field is not None and ds.levels.get(data_field.id) == "table_calc"
            if calc_el is None and not is_formula:
                continue
            use = self.resolve(key)
            if isinstance(use, str):
                self.ctx.flag(inst, f"Table Calculation 的欄位無法解析：{use}")
                continue
            if calc_el is None:
                result.append(self._default_formula_calc(use, row_dims, col_dims, mark_dims, inst))
                continue
            created = self._table_calc(calc_el, use, row_dims, col_dims, mark_dims)
            if created is not None:
                result.append(created)
        return result

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
        if type_attr is None:
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
        if calc_type in ("difference", "percent_difference"):
            # 與前一個（address -1）或其他相對位置的值比較；與第一個、最後一個比較尚不支援
            mode = calc_el.get("diff-options", "Relative")
            values = [text_of(v) for v in children(child(calc_el, "address"), "value")]
            if mode != "Relative" or len(values) != 1 or not re.fullmatch(r"-?\d+", values[0]):
                self.ctx.flag(
                    calc_el,
                    f"差異計算的比較對象（{mode}：{'、'.join(values) or '未指定'}）v0 不支援",
                )
                return None
            options["offset"] = int(values[0])
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
        if preset == "table_down":
            addressing = _field_ids(row_dims + mark_dims)
        elif preset == "table_across":
            addressing = _field_ids(col_dims + mark_dims)
        else:
            refs = [r for r in [calc_el.get("ordering-field")] if r]
            refs += [o.get("field", "") for o in children(calc_el, "order")]
            addressing = []
            for ref in refs:
                found = self._field_ref(ref)
                if found is None:
                    self.ctx.flag(calc_el, f"Table Calculation 的定址欄位 {ref} 找不到")
                    return None
                if found not in addressing:
                    addressing.append(found)
            if not addressing:
                self.ctx.flag(calc_el, "指定維度的 Table Calculation 沒有列出定址欄位")
                return None
        caption = ds.field_by_id(use.field).caption
        tc_id = self.ctx.ids.make("tc", f"{calc_type} {caption}", f"{calc_type} {use.field}")
        use.table_calc = tc_id
        return TableCalc(
            id=tc_id,
            type=calc_type,  # type: ignore[arg-type]
            preset=preset,
            addressing=addressing,
            inferred=inferred,
            options=options,
            source_ref=xpath(calc_el),
        )

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
        if kind == "sheet_link":
            name = attr(root, "ui-action-filter") if root is not None else None
            action = self.ctx.action_ids.get(name or "")
        elif kind == "vit" and "vit" not in self.ctx.notes:
            self.ctx.notes.add("vit")
            self.ctx.ignore(
                element,
                "工具提示中的工作表（Viz in Tooltip）：以一般工作表呈現，工具提示不顯示圖表",
            )
        caption = element.get("column", "")
        return TupleFilter(
            kind="tuple",
            id=self.ctx.ids.make("flt", instance_key(caption).strip("[]"), caption),
            fields=uses,
            stage="context" if element.get("context") == "true" else "dimension",
            mode=mode,  # type: ignore[arg-type]
            tuples=tuples,
            action=action,
            source_ref=xpath(element),
        )

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


def _field_ids(uses: list[FieldUse]) -> list[str]:
    result: list[str] = []
    for use in uses:
        if use.field not in result:
            result.append(use.field)
    return result


def _order(direction: str | None) -> Literal["asc", "desc"]:
    return "desc" if (direction or "").upper() == "DESC" else "asc"
