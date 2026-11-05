"""集合（Set）：<group user:ui-builder='filter-group'> → 布林欄位，Tableau 顯示為 In／Out。

三種集合（寫法取自公開報表與 Tableau 的集合對話框）：

- 固定成員：列出的成員在集合中，例如
  ``<groupfilter function='member' level='[none:Burst Out Set list:nk]' member='"6112"' />``；
  多個成員為 union，「排除」為 except(level-members, …)，空集合為 empty-level（層級寫在 member 屬性）。
  在標記上建立的集合可以有多個層級，每個成員是各層級的值組合（crossjoin）。轉成等價的公式
- Top N（集合對話框的「頂部」頁）：``end(order(level-members))``，數量可以是參數，例如
  ``count='[Parameters].[Parameter 2]'``（公開報表 Prep 的 Error Code Set）
- 條件（「條件」頁）：``filter(expression='SUM([Profit]) > 0', …)``

Top N 與條件集合與 Top N、條件篩選相同，以內容篩選後、維度篩選前的資料決定成員（查詢時計算）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from lxml import etree

from tmig.ir.models import (
    FieldSet,
    FieldUse,
    MemberAlias,
    Parameter,
    ParamRef,
    Scalar,
    SetCondition,
    SetMembers,
    SetTop,
)
from tmig.ir.setformula import members_formula
from tmig.parser.groups import GroupError
from tmig.parser.twbx import child, children
from tmig.parser.values import parse_literal

IN_OUT = [MemberAlias(value=True, label="In"), MemberAlias(value=False, label="Out")]
"""集合的值在 Tableau 上顯示為 In／Out。"""


@dataclass(frozen=True)
class Level:
    """集合的一個層級：欄位使用、在公式中的寫法與值的型別。"""

    use: FieldUse
    expr: str
    datatype: str


LevelResolver = Callable[[str], Level]
"""層級（[State]、[none:State:nk]、[yr:Order Date:ok]）→ Level；無法解析時丟出 GroupError。"""
ParamLookup = Callable[[str], Parameter | None]


def parse_set(
    group: etree._Element, resolve: LevelResolver, parameter: ParamLookup
) -> tuple[FieldSet, str | None]:
    """集合 → (定義, 固定成員的公式)；Top N 與條件集合的公式為 None（查詢時計算）。"""
    roots = children(group, "groupfilter")
    if len(roots) != 1:
        raise GroupError("結構不明")
    root = roots[0]
    function = root.get("function")
    if function == "end":
        return _top_n(root, resolve, parameter), None
    if function == "filter":
        return _condition(root, resolve), None
    return _members(root, resolve)


# ------------------------------------------------------------ Top N 與條件


def _selection(node: etree._Element | None, level: Level) -> tuple[str, list[Scalar]]:
    """排名或條件前先選取的成員：全部、列出的成員（include）或全部扣掉列出的（exclude）。"""
    if node is None or node.get("function") == "level-members":
        return "all", []
    function = node.get("function")
    if function == "member":
        return "include", [parse_literal(node.get("member"), level.datatype)]
    if function == "union" and all(
        p.get("function") == "member" for p in children(node, "groupfilter")
    ):
        return "include", [
            parse_literal(p.get("member"), level.datatype) for p in children(node, "groupfilter")
        ]
    if function == "except":
        parts = children(node, "groupfilter")
        if parts and parts[0].get("function") == "level-members":
            removed: list[Scalar] = []
            for part in parts[1:]:
                mode, values = _selection(part, level)
                if mode != "include":
                    raise GroupError("排除的成員結構不明")
                removed += values
            return "exclude", removed
    raise GroupError(f"先選取的成員（function={function}）不支援")


def _single_level(node: etree._Element, resolve: LevelResolver) -> Level:
    levels = {g.get("level") for g in node.iter("groupfilter") if g.get("level")}
    if len(levels) != 1:
        raise GroupError("Top N 與條件集合必須剛好以一個欄位為層級")
    level = resolve(levels.pop() or "")
    if level.use.date is not None:
        raise GroupError("以日期部分為層級的 Top N、條件集合尚不支援")
    return level


def _top_n(root: etree._Element, resolve: LevelResolver, parameter: ParamLookup) -> FieldSet:
    order = child(root, "groupfilter")
    if order is None or order.get("function") != "order" or not order.get("expression"):
        raise GroupError("Top N 集合缺少排名依據")
    level = _single_level(root, resolve)
    mode, values = _selection(child(order, "groupfilter"), level)
    end = root.get("end", "top")
    if end not in ("top", "bottom"):
        raise GroupError(f"Top N 集合的方向 {end} 不支援")
    units = root.get("units", "records")
    if units != "records":
        raise GroupError(f"Top N 集合的單位 {units} 不支援")
    text = root.get("count", "")
    count: int | ParamRef
    if text.startswith("["):
        found = parameter(text)
        if found is None:
            raise GroupError(f"Top N 集合的參數 {text} 不存在")
        count = ParamRef(parameter=found.id)
    else:
        try:
            count = int(text)
        except ValueError as exc:
            raise GroupError(f"Top N 集合的數量 {text} 無法解析") from exc
        if count <= 0:
            raise GroupError(f"Top N 集合的數量 {count} 必須大於 0")
    # 與 Top N 篩選相同：top＋DESC 與 bottom＋ASC 都是取最大的 N 個
    largest = (end == "top") == (order.get("direction", "DESC").upper() == "DESC")
    rule = SetTop(
        kind="top_n",
        direction="top" if largest else "bottom",
        count=count,
        by=order.get("expression", ""),
        mode=mode,  # type: ignore[arg-type]
        values=values,
    )
    return FieldSet(levels=[level.use], rule=rule)


def _condition(root: etree._Element, resolve: LevelResolver) -> FieldSet:
    expression = root.get("expression")
    if not expression:
        raise GroupError("條件集合缺少條件公式")
    level = _single_level(root, resolve)
    mode, values = _selection(child(root, "groupfilter"), level)
    rule = SetCondition(
        kind="condition",
        condition=expression,
        mode=mode,  # type: ignore[arg-type]
        values=values,
    )
    return FieldSet(levels=[level.use], rule=rule)


# ------------------------------------------------------------ 固定成員


def _member_tuples(
    node: etree._Element, resolve: LevelResolver, levels: dict[str, Level]
) -> list[dict[str, Scalar]]:
    """member、crossjoin(member…)、union(…) → 成員（層級 → 值）。levels 依出現順序記下層級
    （同一個欄位可能寫成 [Region] 或 [none:Region:nk]，以公式中的寫法為鍵）。"""
    function = node.get("function")
    if function == "member":
        level = _level(node.get("level", ""), resolve, levels)
        return [{level.expr: parse_literal(node.get("member"), level.datatype)}]
    if function == "crossjoin":
        combined: dict[str, Scalar] = {}
        for part in children(node, "groupfilter"):
            items = _member_tuples(part, resolve, levels)
            if len(items) != 1:
                raise GroupError("值組合的結構不明")
            combined.update(items[0])
        return [combined]
    if function == "union":
        result: list[dict[str, Scalar]] = []
        for part in children(node, "groupfilter"):
            result += _member_tuples(part, resolve, levels)
        return result
    raise GroupError(f"集合的成員定義（function={function}）不支援")


def _level(name: str, resolve: LevelResolver, levels: dict[str, Level]) -> Level:
    level = resolve(name)
    return levels.setdefault(level.expr, level)


def _members(root: etree._Element, resolve: LevelResolver) -> tuple[FieldSet, str]:
    function = root.get("function")
    mode: Literal["include", "exclude"] = "include"
    found: dict[str, Level] = {}
    members: list[dict[str, Scalar]] = []
    if function == "empty-level":
        # 空集合的層級寫在 member 屬性
        _level(root.get("member") or root.get("level") or "", resolve, found)
    elif function == "level-members":
        _level(root.get("level", ""), resolve, found)
        mode = "exclude"  # 全部成員：排除空清單
    elif function == "except":
        parts = children(root, "groupfilter")
        if not parts or parts[0].get("function") != "level-members":
            raise GroupError("排除的成員結構不明")
        _level(parts[0].get("level", ""), resolve, found)
        for part in parts[1:]:
            members += _member_tuples(part, resolve, found)
        mode = "exclude"
    else:
        members = _member_tuples(root, resolve, found)
    order = list(found)
    if not order or any(set(m) != set(order) for m in members):
        raise GroupError("成員的層級不一致")
    levels = [found[key] for key in order]
    rows = [[m[key] for key in order] for m in members]
    formula = members_formula([(level.expr, level.datatype) for level in levels], rows, mode)
    rule = SetMembers(kind="members", mode=mode, members=rows)
    return FieldSet(levels=[level.use for level in levels], rule=rule), formula


__all__ = ["IN_OUT", "Level", "parse_set"]
