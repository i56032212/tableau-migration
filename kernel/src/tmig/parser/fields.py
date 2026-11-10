"""欄位實例（column-instance）與篩選的解析，資料源與工作表共用。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from lxml import etree

from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.ir.models import (
    PERCENTILES,
    Aggregation,
    CategoricalFilter,
    ConditionFilter,
    DataField,
    DateDerivation,
    FieldUse,
    Filter,
    FilterStage,
    MemberAlias,
    MemberRange,
    ParamRef,
    RangeFilter,
    RelativeDateFilter,
    Scalar,
    TopNFilter,
)
from tmig.parser.context import DsState, ParseContext
from tmig.parser.ids import strip_brackets
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import attr, child, children, descendants, text_of
from tmig.parser.values import LiteralError, parse_literal

AGG_DERIVATIONS: dict[str, Aggregation] = {
    "Sum": "sum",
    "Avg": "avg",
    "Min": "min",
    "Max": "max",
    "Count": "count",
    "CountD": "countd",
    "Median": "median",
    "Attribute": "attr",
    "Stdev": "stdev",
    "StdevP": "stdevp",
    "Var": "var",
    "VarP": "varp",
}
DATE_DERIVATIONS: dict[str, tuple[Literal["part", "trunc"], str]] = {
    "Year": ("part", "year"),
    "Quarter": ("part", "quarter"),
    "Month": ("part", "month"),
    "Week": ("part", "week"),
    "Weekday": ("part", "weekday"),
    "Day": ("part", "day"),
    "Hour": ("part", "hour"),
    "Minute": ("part", "minute"),
    "Second": ("part", "second"),
    "Year-Trunc": ("trunc", "year"),
    "Quarter-Trunc": ("trunc", "quarter"),
    "Month-Trunc": ("trunc", "month"),
    "Week-Trunc": ("trunc", "week"),
    "Day-Trunc": ("trunc", "day"),
    "Hour-Trunc": ("trunc", "hour"),
    "Minute-Trunc": ("trunc", "minute"),
    "Second-Trunc": ("trunc", "second"),
    "MY": ("trunc", "month"),
    "MDY": ("trunc", "day"),
}

NAME_PREFIXES = {
    "none": "None",
    "usr": "User",
    "yr": "Year",
    "qr": "Quarter",
    "mn": "Month",
    "wk": "Week",
    "wd": "Weekday",
    "dy": "Day",
    "hr": "Hour",
    "mi": "Minute",
    "sc": "Second",
    "tyr": "Year-Trunc",
    "tqr": "Quarter-Trunc",
    "tmn": "Month-Trunc",
    "twk": "Week-Trunc",
    "tdy": "Day-Trunc",
    "thr": "Hour-Trunc",
    "tmi": "Minute-Trunc",
    "tsc": "Second-Trunc",
    "my": "MY",
    "mdy": "MDY",
    "io": "InOut",
}
"""欄位實例名稱的前綴（例如 [yr:Order Date:ok] 的 yr）→ 衍生方式；用於沒有 column-instance 的參照。"""

Resolver = Callable[[str], "FieldUse | str"]
"""把篩選或排序參照的欄位實例（例如 [ds].[none:Region:nk]）轉成 FieldUse；失敗時回傳原因。"""


def instance_key(ref: str) -> str:
    """[ds].[sum:Sales:qk] 或 [sum:Sales:qk] → [sum:Sales:qk]。"""
    parts = qualified_parts(ref)
    if not parts:
        return ref
    return "[" + parts[-1].replace("]", "]]") + "]"


def use_from_instance(ds: DsState, instance: etree._Element) -> FieldUse | str:
    """column-instance → FieldUse（尚未套用 Table Calculation）；不支援時回傳原因。"""
    column = instance.get("column", "")
    data_field = ds.by_name.get(column)
    if data_field is None:
        if column.startswith("[:"):
            return f"Measure Names／Values 等特殊欄位 {column} v0 不支援"
        return f"欄位 {column} 不在資料源中（可能是集合或無法移轉的計算欄位）"
    derivation = instance.get("derivation", "None")
    continuous = instance.get("type") == "quantitative"
    if derivation in ("None", "User"):
        return FieldUse(field=data_field.id, continuous=continuous)
    if derivation == "InOut":
        # 集合放在架上（IN/OUT）：成員是否在集合中，顯示為 In／Out；欄位實例上可以另設別名
        if data_field.id not in ds.set_fields:
            return f"{column} 不是集合，卻使用 IN/OUT"
        labels = _in_out_aliases(instance)
        if labels:
            data_field.aliases = labels
        return FieldUse(field=data_field.id)
    if derivation in AGG_DERIVATIONS:
        return FieldUse(field=data_field.id, agg=AGG_DERIVATIONS[derivation], continuous=continuous)
    if derivation == "Percentile":
        # 百分位數：百分位在 aggregation-param（彙總選單只有 5、10、25、50、75、90、95）
        param = instance.get("aggregation-param", "")
        agg = f"pct{param}"
        if agg not in PERCENTILES:
            return f"百分位數 {param}（{instance.get('name')}）v0 不支援"
        return FieldUse(field=data_field.id, agg=agg, continuous=continuous)  # type: ignore[arg-type]
    if derivation in DATE_DERIVATIONS:
        if data_field.datatype not in ("date", "datetime"):
            return f"{column} 不是日期欄位，卻使用日期衍生 {derivation}"
        kind, unit = DATE_DERIVATIONS[derivation]
        return FieldUse(
            field=data_field.id,
            date=DateDerivation(kind=kind, unit=unit),  # type: ignore[arg-type]
            continuous=continuous,
        )
    return f"欄位衍生方式 {derivation}（{instance.get('name')}）v0 不支援"


def _in_out_aliases(instance: etree._Element) -> list[MemberAlias]:
    """IN/OUT 欄位實例上的別名（例如 In top 5／Not in Top 5）；沒寫的值維持 In／Out。"""
    labels = {True: "In", False: "Out"}
    found = False
    for alias in descendants(child(instance, "aliases"), "alias"):
        key = (alias.get("key") or "").strip().lower()
        if key in ("true", "false") and alias.get("value") is not None:
            labels[key == "true"] = alias.get("value", "")
            found = True
    if not found:
        return []
    return [
        MemberAlias(value=True, label=labels[True]),
        MemberAlias(value=False, label=labels[False]),
    ]


def use_from_name(ds: DsState, name: str) -> FieldUse | str:
    """[yr:Order Date:ok]、[none:Region:nk] 等欄位實例名稱 → FieldUse（沒有 column-instance 時使用）。"""
    bare = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    parts = bare.split(":")
    if len(parts) < 3 or parts[0] not in NAME_PREFIXES:
        return f"無法辨識的欄位實例名稱 {name}"
    column = "[" + ":".join(parts[1:-1]).replace("]", "]]") + "]"
    element = etree.Element(
        "column-instance",
        column=column,
        derivation=NAME_PREFIXES[parts[0]],
        name=name,
        type="quantitative" if parts[-1] == "qk" else "nominal",
    )
    return use_from_instance(ds, element)


def is_table_calc(ds: DsState, use: FieldUse) -> bool:
    return use.table_calc is not None or ds.levels.get(use.field) == "table_calc"


def is_aggregate(ds: DsState, use: FieldUse) -> bool:
    """在檢視中是否為量值：有彙總、或是彙總／Table Calculation 層級的計算欄位。"""
    if use.agg is not None or use.table_calc is not None:
        return True
    return ds.levels.get(use.field) in ("aggregate", "table_calc")


def value_type(field: DataField, use: FieldUse) -> str:
    """篩選值、排序值的型別：日期部分為整數，彙總依彙總方式。"""
    if use.date is not None:
        return "integer" if use.date.kind == "part" else field.datatype
    if use.agg in ("count", "countd"):
        return "integer"
    if use.agg in ("avg", "median", "stdev", "stdevp", "var", "varp", *PERCENTILES):
        return "real"
    return field.datatype


def _literal(text: str | None, datatype: str) -> Scalar:
    return parse_literal(text, datatype)


RELATIVE_PERIODS = ("year", "quarter", "month", "week", "day")

Selection = tuple[Literal["include", "exclude", "all"], list[Scalar]]


def _intersect(a: Selection, b: Selection) -> Selection:
    """兩個成員選取的交集。"""
    if a[0] == "all":
        return b
    if b[0] == "all":
        return a
    if a[0] == "include" and b[0] == "include":
        return "include", [v for v in a[1] if v in b[1]]
    if a[0] == "include":
        return "include", [v for v in a[1] if v not in b[1]]
    if b[0] == "include":
        return "include", [v for v in b[1] if v not in a[1]]
    return "exclude", a[1] + [v for v in b[1] if v not in a[1]]


# ---------------------------------------------------------------- 篩選


class FilterParser:
    """解析 <filter>。scope 決定可用的運算階段。"""

    def __init__(
        self,
        ctx: ParseContext,
        ds: DsState,
        resolve: Resolver,
        scope: Literal["extract", "datasource", "worksheet"],
    ) -> None:
        self.ctx = ctx
        self.ds = ds
        self.resolve = resolve
        self.scope = scope

    def parse(
        self, element: etree._Element, group: str | None = None, column: str | None = None
    ) -> Filter | None:
        """group：篩選群組；省略時依 filter-group 屬性（「套用到選取的工作表」）。
        column：篩選的欄位；省略時為 <filter column>（跨資料源篩選傳入相關資料源中對應的欄位）。"""
        column = column or element.get("column", "")
        resolved = self.resolve(column)
        if isinstance(resolved, str):
            self.ctx.flag(element, f"篩選的欄位無法解析：{resolved}")
            return None
        use = resolved
        data_field = self.ds.field_by_id(use.field)
        kind_class = element.get("class")
        context = element.get("context") == "true"
        base_id = self.ctx.ids.make("flt", data_field.caption, data_field.name)
        if group is None and element.get("filter-group"):
            group = f"group:{element.get('filter-group')}"
        if kind_class in ("categorical", "quantitative"):
            try:
                if kind_class == "categorical":
                    parsed = self._categorical(element, use, data_field, context, base_id)
                else:
                    parsed = self._range(element, use, data_field, context, base_id)
            except LiteralError as exc:
                self.ctx.flag(element, f"篩選值無法解析：{exc}")
                return None
            if parsed is not None and group is not None and self.scope == "worksheet":
                parsed = parsed.model_copy(update={"group": group})
            return parsed
        if kind_class == "relative-date":
            relative = self._relative_date(element, use, data_field, context, base_id)
            if relative is not None and group is not None and self.scope == "worksheet":
                relative = relative.model_copy(update={"group": group})
            return relative
        self.ctx.flag(element, f"篩選類型 {kind_class} v0 不支援")
        return None

    def _relative_date(
        self,
        element: etree._Element,
        use: FieldUse,
        data_field: DataField,
        context: bool,
        filter_id: str,
    ) -> RelativeDateFilter | None:
        """相對日期：例如最近 3 個月（first-period=-2、last-period=0、period-type=month）。"""
        if data_field.datatype not in ("date", "datetime"):
            self.ctx.flag(element, f"相對日期篩選的欄位「{data_field.caption}」不是日期")
            return None
        period = element.get("period-type-v2") or element.get("period-type") or ""
        if period not in RELATIVE_PERIODS:
            self.ctx.flag(element, f"相對日期篩選的期間 {period} v0 不支援")
            return None
        try:
            first = int(element.get("first-period", "0"))
            last = int(element.get("last-period", "0"))
            anchor_text = element.get("anchor")
            anchor = _literal(anchor_text, "date") if anchor_text else None
        except (ValueError, LiteralError) as exc:
            self.ctx.flag(element, f"相對日期篩選的設定無法解析：{exc}")
            return None
        if first > last:
            first, last = last, first
        return RelativeDateFilter(
            kind="relative_date",
            id=filter_id,
            field=FieldUse(field=use.field),  # 一律以日期本身計算（與欄位實例的日期部分無關）
            stage=self._stage_for_dimension(element, context),
            period=period,  # type: ignore[arg-type]
            first=first,
            last=last,
            include_future=element.get("include-future", "true") == "true",
            include_null=element.get("include-null") == "true",
            anchor=str(anchor) if anchor is not None else None,
            source_ref=_xpath(element),
        )

    def _stage_for_dimension(self, element: etree._Element, context: bool) -> FilterStage:
        if self.scope == "extract":
            return "extract"
        if self.scope == "datasource":
            return "datasource"
        return "context" if context else "dimension"

    def _categorical(
        self,
        element: etree._Element,
        use: FieldUse,
        data_field: DataField,
        context: bool,
        filter_id: str,
    ) -> Filter | None:
        groups = children(element, "groupfilter")
        if not groups and use.field in self.ds.set_fields:
            # 集合（含使用者篩選）放在篩選架上：只顯示集合中的成員，也就是布林欄位為 TRUE 的列
            return CategoricalFilter(
                kind="categorical",
                id=filter_id,
                field=FieldUse(field=use.field),
                stage=self._stage_for_dimension(element, context),
                mode="include",
                values=[True],
                source_ref=_xpath(element),
            )
        if len(groups) != 1:
            self.ctx.flag(element, "沒有選取任何值或結構不明的類別篩選 v0 不支援")
            return None
        root = groups[0]
        function = root.get("function")
        datatype = value_type(data_field, use)
        if is_aggregate(self.ds, use):
            return self._measure_selection(element, root, use, datatype, filter_id)
        if function == "end":
            return self._top_n(element, root, use, datatype, context, filter_id)
        if function == "filter":
            return self._condition(element, root, use, datatype, context, filter_id)
        ranges: list[MemberRange] = []
        selection = self._selection(root, datatype)
        if selection is None:
            ranged = self._ranged_selection(root, datatype)
            if ranged is None:
                self.ctx.flag(element, f"類別篩選的條件（function={function}）v0 不支援")
                return None
            selection, ranges = ranged
        mode, values = selection
        stage = self._stage_for_dimension(element, context)
        assert stage is not None
        return CategoricalFilter(
            kind="categorical",
            id=filter_id,
            field=use,
            stage=stage,
            mode=mode,
            values=values,
            ranges=ranges,
            source_ref=_xpath(element),
        )

    def _ranged_selection(
        self, group: etree._Element, datatype: str
    ) -> tuple[tuple[Literal["include", "exclude", "all"], list[Scalar]], list[MemberRange]] | None:
        """含連續成員（range）的選取：在篩選清單中連續勾選時，Tableau 記成 from–to（含頭尾）。

        include：range、union(member, range, …)；exclude：except(level-members, member／range／union…)。
        排除「全部扣掉一些」等於只保留那些：except(level-members, except(level-members, …))。"""
        function = group.get("function")
        parts = children(group, "groupfilter")
        if function in ("range", "union"):
            found = self._range_members([group] if function == "range" else parts, datatype)
            return None if found is None else (("include", found[0]), found[1])
        if function != "except" or not parts or parts[0].get("function") != "level-members":
            return None
        rest = parts[1:]
        if len(rest) == 1 and rest[0].get("function") == "except":
            inner = children(rest[0], "groupfilter")
            if inner and inner[0].get("function") == "level-members":
                found = self._range_members(inner[1:], datatype)
                return None if found is None else (("include", found[0]), found[1])
            return None
        found = self._range_members(rest, datatype)
        return None if found is None else (("exclude", found[0]), found[1])

    def _range_members(
        self, parts: list[etree._Element], datatype: str
    ) -> tuple[list[Scalar], list[MemberRange]] | None:
        values: list[Scalar] = []
        ranges: list[MemberRange] = []
        for part in parts:
            function = part.get("function")
            if function == "member":
                values.append(_literal(part.get("member"), datatype))
            elif function == "range":
                start = _literal(part.get("from"), datatype)
                end = _literal(part.get("to"), datatype)
                if start is None or end is None:
                    return None
                ranges.append(MemberRange(start=start, end=end))
            elif function == "union":
                nested = self._range_members(children(part, "groupfilter"), datatype)
                if nested is None:
                    return None
                values += nested[0]
                ranges += nested[1]
            else:
                return None
        return values, ranges

    def _measure_selection(
        self,
        element: etree._Element,
        root: etree._Element,
        use: FieldUse,
        datatype: str,
        filter_id: str,
    ) -> Filter | None:
        """離散的彙總計算欄位上的類別篩選：在每個標記的彙總值上判斷（量值篩選階段）。"""
        if self.scope != "worksheet":
            self.ctx.flag(element, "資料源上的量值類別篩選 v0 不支援")
            return None
        selection = self._selection(root, datatype)
        if selection is None:
            self.ctx.flag(
                element, f"量值上的類別篩選條件（function={root.get('function')}）v0 不支援"
            )
            return None
        mode, values = selection
        table_calc = use.table_calc is not None or self.ds.levels.get(use.field) == "table_calc"
        return CategoricalFilter(
            kind="categorical",
            id=filter_id,
            field=use,
            stage="table_calc" if table_calc else "measure",
            mode=mode,
            values=values,
            source_ref=_xpath(element),
        )

    def _condition(
        self,
        element: etree._Element,
        root: etree._Element,
        use: FieldUse,
        datatype: str,
        context: bool,
        filter_id: str,
    ) -> Filter | None:
        """萬用字元與條件：groupfilter function='filter' 的 expression，內層為先選取的成員。"""
        expression = root.get("expression")
        if not expression:
            self.ctx.flag(element, "條件篩選缺少條件公式")
            return None
        inner = child(root, "groupfilter")
        selection = ("all", []) if inner is None else self._selection(inner, datatype)
        if selection is None:
            self.ctx.flag(element, "條件篩選同時設定了無法解析的一般選取，v0 不支援")
            return None
        analyzer = self.ds.analyzer
        if analyzer is not None:
            try:
                info = analyzer.analyze(parse_formula(expression))
            except (FormulaSyntaxError, FormulaSemanticError) as exc:
                self.ctx.flag(element, f"條件篩選的公式「{expression}」：{exc}")
                return None
            if info.datatype not in ("boolean", "null"):
                self.ctx.flag(element, f"條件篩選的公式「{expression}」不是布林值")
                return None
            if info.level == "tc":
                self.ctx.flag(element, "條件篩選的公式含 Table Calculation，v0 不支援")
                return None
            if info.level == "agg" and self.scope != "worksheet":
                self.ctx.flag(element, "資料源上的彙總條件篩選 v0 不支援")
                return None
        mode, values = selection
        return ConditionFilter(
            kind="condition",
            id=filter_id,
            field=use,
            stage=self._stage_for_dimension(element, context),
            condition=expression,
            mode=mode,
            values=values,
            source_ref=_xpath(element),
        )

    def _selection(
        self, group: etree._Element, datatype: str
    ) -> tuple[Literal["include", "exclude", "all"], list[Scalar]] | None:
        function = group.get("function")
        if function == "member":
            return "include", [_literal(group.get("member"), datatype)]
        if function == "union":
            members = self._members(group, datatype)
            return None if members is None else ("include", members)
        if function == "level-members":
            return "all", []
        if function == "empty-level":
            return "include", []
        if function == "intersection":
            result: tuple[Literal["include", "exclude", "all"], list[Scalar]] = ("all", [])
            for part in children(group, "groupfilter"):
                sub = self._selection(part, datatype)
                if sub is None:
                    return None
                result = _intersect(result, sub)
            return result
        if function == "except":
            parts = children(group, "groupfilter")
            if not parts or parts[0].get("function") != "level-members":
                return None
            excluded: list[Scalar] = []
            for part in parts[1:]:
                if part.get("function") == "member":
                    excluded.append(_literal(part.get("member"), datatype))
                elif part.get("function") == "union":
                    members = self._members(part, datatype)
                    if members is None:
                        return None
                    excluded.extend(members)
                else:
                    return None
            return "exclude", excluded
        return None

    def _members(self, group: etree._Element, datatype: str) -> list[Scalar] | None:
        values: list[Scalar] = []
        for part in children(group, "groupfilter"):
            if part.get("function") == "member":
                values.append(_literal(part.get("member"), datatype))
            elif part.get("function") == "union":
                nested = self._members(part, datatype)
                if nested is None:
                    return None
                values.extend(nested)
            else:
                return None
        return values

    def _top_n(
        self,
        element: etree._Element,
        group: etree._Element,
        use: FieldUse,
        datatype: str,
        context: bool,
        filter_id: str,
    ) -> Filter | None:
        if self.scope != "worksheet":
            self.ctx.flag(element, "資料源上的 Top N 篩選 v0 不支援")
            return None
        order = child(group, "groupfilter")
        if order is None or order.get("function") != "order" or not order.get("expression"):
            self.ctx.flag(element, "Top N 篩選缺少排名依據")
            return None
        inner = child(order, "groupfilter")
        selection = ("all", []) if inner is None else self._selection(inner, datatype)
        if selection is None:
            self.ctx.flag(element, "Top N 篩選同時設定了條件或無法解析的選取，v0 不支援")
            return None
        end = group.get("end", "top")
        if end not in ("top", "bottom"):
            self.ctx.flag(element, f"Top N 篩選的方向 {end} 不支援")
            return None
        count_text = group.get("count", "")
        count: int | ParamRef
        if count_text.startswith("["):
            param = self.ctx.parameter(count_text)
            if param is None:
                self.ctx.flag(element, f"Top N 篩選的參數 {count_text} 不存在")
                return None
            count = ParamRef(parameter=param.id)
        else:
            try:
                count = int(count_text)
            except ValueError:
                self.ctx.flag(element, f"Top N 篩選的數量 {count_text} 無法解析")
                return None
            if count <= 0:
                self.ctx.flag(element, f"Top N 篩選的數量 {count} 必須大於 0")
                return None
        # function='end' 從排序後清單的頂端（top）或底端（bottom）取 count 個成員；
        # 清單依 order 的 direction 排序，所以 top＋DESC 與 bottom＋ASC 都是取最大的 N 個
        direction = order.get("direction", "DESC").upper()
        largest = (end == "top") == (direction == "DESC")
        return TopNFilter(
            kind="top_n",
            id=filter_id,
            field=use,
            stage="context" if context else "top_n",
            direction="top" if largest else "bottom",
            count=count,
            by=order.get("expression", ""),
            mode=selection[0],
            values=selection[1],
            source_ref=_xpath(element),
        )

    def _range(
        self,
        element: etree._Element,
        use: FieldUse,
        data_field: DataField,
        context: bool,
        filter_id: str,
    ) -> Filter | None:
        included = element.get("included-values", "in-range")
        datatype = value_type(data_field, use)
        low = child(element, "min")
        high = child(element, "max")
        minimum = _literal(text_of(low), datatype) if low is not None else None
        maximum = _literal(text_of(high), datatype) if high is not None else None
        include_null = False
        if included == "in-range-or-null":
            include_null = True
        elif included == "all":
            minimum = maximum = None
            include_null = True
        elif included == "non-null":
            minimum = maximum = None
        elif included != "in-range":
            self.ctx.flag(element, f"範圍篩選的 included-values={included} v0 不支援")
            return None
        stage: FilterStage | None
        if is_table_calc(self.ds, use):
            stage = "table_calc"
        elif is_aggregate(self.ds, use):
            stage = "measure"
        else:
            stage = self._stage_for_dimension(element, context)
        if stage in ("measure", "table_calc") and self.scope != "worksheet":
            self.ctx.flag(element, "資料源上的量值篩選 v0 不支援")
            return None
        if stage in ("measure", "table_calc") and context:
            self.ctx.flag(element, "量值的內容篩選 v0 不支援")
            return None
        assert stage is not None
        return RangeFilter(
            kind="range",
            id=filter_id,
            field=use,
            stage=stage,
            min=minimum,
            max=maximum,
            include_null=include_null,
            source_ref=_xpath(element),
        )


def _xpath(element: etree._Element) -> str:
    return element.getroottree().getpath(element)


def bucket_values(dictionary: etree._Element | None, datatype: str) -> list[Scalar]:
    """手動排序的 <bucket>；%all% 代表「其餘值」，略過。"""
    values: list[Scalar] = []
    for bucket in children(dictionary, "bucket"):
        text = text_of(bucket)
        if text == "%all%":
            continue
        values.append(parse_literal(text, datatype))
    return values


def ui_attr(element: etree._Element, name: str) -> str | None:
    return attr(element, name)


def member_aliases(
    ctx: ParseContext, column: etree._Element | None, datatype: str, what: str
) -> list[MemberAlias]:
    """成員別名：<aliases><alias key='"High School"' value='H-Sch' />；key 是 Tableau 常值，
    %null% 為 Null 的別名。無法解析的 key 列為不支援（別名會改變顯示值，不能默默略過）。"""
    result: list[MemberAlias] = []
    for alias in descendants(child(column, "aliases"), "alias"):
        key, label = alias.get("key"), alias.get("value")
        if key is None or label is None:
            continue
        try:
            value = parse_literal(key, datatype)
        except LiteralError as exc:
            ctx.flag(alias, f"{what}的別名「{key}」無法解析：{exc}")
            continue
        result.append(MemberAlias(value=value, label=label))
    return result


def field_label(name: str) -> str:
    return strip_brackets(name)
