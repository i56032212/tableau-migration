"""欄位實例（column-instance）與篩選的解析，資料源與工作表共用。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from lxml import etree

from tmig.ir.models import (
    Aggregation,
    CategoricalFilter,
    DataField,
    DateDerivation,
    FieldUse,
    Filter,
    FilterStage,
    ParamRef,
    RangeFilter,
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
        return f"欄位 {column} 不在資料源中（可能是群組、集合或分組）"
    derivation = instance.get("derivation", "None")
    continuous = instance.get("type") == "quantitative"
    if derivation in ("None", "User"):
        return FieldUse(field=data_field.id, continuous=continuous)
    if derivation in AGG_DERIVATIONS:
        return FieldUse(field=data_field.id, agg=AGG_DERIVATIONS[derivation], continuous=continuous)
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
    if use.agg in ("avg", "median", "stdev", "stdevp", "var", "varp"):
        return "real"
    return field.datatype


def _literal(text: str | None, datatype: str) -> Scalar:
    return parse_literal(text, datatype)


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

    def parse(self, element: etree._Element) -> Filter | None:
        column = element.get("column", "")
        resolved = self.resolve(column)
        if isinstance(resolved, str):
            self.ctx.flag(element, f"篩選的欄位無法解析：{resolved}")
            return None
        use = resolved
        data_field = self.ds.field_by_id(use.field)
        kind_class = element.get("class")
        context = element.get("context") == "true"
        base_id = self.ctx.ids.make("flt", data_field.caption, data_field.name)
        try:
            if kind_class == "categorical":
                return self._categorical(element, use, data_field, context, base_id)
            if kind_class == "quantitative":
                return self._range(element, use, data_field, context, base_id)
        except LiteralError as exc:
            self.ctx.flag(element, f"篩選值無法解析：{exc}")
            return None
        if kind_class == "relative-date":
            self.ctx.flag(element, "相對日期篩選 v0 不支援")
        else:
            self.ctx.flag(element, f"篩選類型 {kind_class} v0 不支援")
        return None

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
        if len(groups) != 1:
            self.ctx.flag(element, "沒有選取任何值或結構不明的類別篩選 v0 不支援")
            return None
        root = groups[0]
        if is_aggregate(self.ds, use):
            self.ctx.flag(element, "量值上的類別篩選 v0 不支援")
            return None
        function = root.get("function")
        if function == "end":
            return self._top_n(element, root, use, context, filter_id)
        datatype = value_type(data_field, use)
        selection = self._selection(root, datatype)
        if selection is None:
            self.ctx.flag(element, f"類別篩選的條件（function={function}）v0 不支援")
            return None
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
        if inner is None or inner.get("function") != "level-members":
            self.ctx.flag(element, "Top N 篩選同時設定了一般選取或其他條件，v0 不支援")
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


def has_aliases(column: etree._Element | None) -> bool:
    aliases = child(column, "aliases")
    return aliases is not None and any(True for _ in descendants(aliases, "alias"))


def field_label(name: str) -> str:
    return strip_brackets(name)
