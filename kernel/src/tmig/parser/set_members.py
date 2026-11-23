"""顯示集合中的成員（Tableau 的「Show Members in Set」）：集合直接放在欄列架或標記卡上（沒有 IN/OUT 的
欄位實例）時列出集合所在維度的成員，而不是 In／Out。

Tableau 2021.2 說明文件「Create Sets」
（https://help.tableau.com/v2021.2/pro/desktop/en-us/sortgroup_sets_create.htm ，2026-11-23 查閱）：「Show Members
in Set」列出集合的各個成員，並「automatically adds a filter to the view that includes only the members of the
set」。這個篩選寫在 .twb 的篩選架上（``<filter class='categorical' column='[資料源].[集合]' />``，沒有 groupfilter；
寫法取自 GitHub 上公開的 .twb，見 README「已知限制與待驗證」），照常解析成「在集合中」的篩選
（tmig.parser.fields）；使用者移除了這個篩選時列出維度的所有成員。

架上的集合換成一個隱藏的計算欄位：公式是集合所在的維度、名稱是集合的名稱（標題、圖例與工具提示上
顯示集合的名稱，與 Tableau 的膠囊相同），維度的別名、格式與地理角色照抄。多個維度的集合（成員是各維度的
值組合）以成員顯示尚不支援：Tableau 把組合寫成一個成員時的分隔與排序沒有寫明。
"""

from __future__ import annotations

from tmig.formula.ast import FieldRef, to_json
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.ir.models import Calc, DataField, FieldUse
from tmig.parser.context import DsState, ParseContext
from tmig.parser.ids import strip_brackets

SUFFIX = " (members)"
"""隱藏的計算欄位的內部名稱：集合的名稱加上這個字尾（[Top 5 Sub Product (members)]）。"""


def members_use(ctx: ParseContext, ds: DsState, found: DataField) -> FieldUse | str:
    """集合 → 列出成員的欄位使用（隱藏的計算欄位，同一個集合只建立一次）；不支援時回傳原因。"""
    assert found.set is not None
    levels = found.set.levels
    if len(levels) != 1:
        return (
            f"集合「{found.caption}」以成員顯示（顯示集合中的成員）：多個維度的集合尚不支援"
            "（各維度的值組合成一個成員的寫法 Tableau 沒有寫明）"
        )
    level = levels[0]
    name = f"[{strip_brackets(found.name)}{SUFFIX}]"
    existing = ds.by_name.get(name)
    if existing is None:
        base = ds.field_by_id(level.field)
        if ds.analyzer is None:
            return f"集合「{found.caption}」以成員顯示：資料源的公式無法分析"
        analyzer = ds.analyzer
        node = parse_formula(base.name)
        try:
            analyzer.analyze(node)
        except FormulaSemanticError as exc:
            return f"集合「{found.caption}」以成員顯示：{exc.message}"

        def resolve(ref: FieldRef) -> dict[str, str] | None:
            target = analyzer.resolve(ref)
            return {"target": target.kind, "id": target.id}

        existing = DataField(
            id=ctx.ids.make("f", f"{found.caption} members"),
            name=name,
            caption=found.caption,
            role="dimension",
            datatype=base.datatype,
            format=base.format,
            hidden=True,
            calc=Calc(formula=base.name, ast=to_json(node, resolve), level="row"),
            aliases=list(base.aliases),
            geo_role=base.geo_role,
            geo_aliases=list(base.geo_aliases),
            default_sort=base.default_sort,
            fiscal_year_start=base.fiscal_year_start,
            comment=f"集合「{found.caption}」以成員顯示（顯示集合中的成員）",
            source_ref=found.source_ref,
        )
        ds.add_field(existing)
        ds.levels[existing.id] = "row"
    return FieldUse(field=existing.id, date=level.date)


__all__ = ["SUFFIX", "members_use"]
