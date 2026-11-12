"""Relationship 模型（N6）：object-graph 的邏輯資料表與關聯。

Tableau 2020.2 起，多表的資料源預設是 Relationship 模型：object-graph 的每個 object 是一張邏輯資料表
（Live 連線時 properties context='' 是它的實體資料表；萃取時 context='extract' 是 .hyper 中對應的資料表），
relationships 以欄位（local name）的比較連接兩張邏輯資料表，連成一棵樹。

- 欄位所屬的邏輯資料表：metadata-record 的 object-id（Live），或萃取的 parent-name（即 object id）
- 邏輯資料表內含 join（實體層的 join）時，Live 連線尚不支援（萃取已存成單一資料表，可以使用）
- 關聯條件只支援欄位之間的比較（以 AND 連接）；含計算的關聯尚不支援
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from tmig.ir.models import (
    LogicalTable,
    Relation,
    Relationship,
    RelationshipCondition,
    TableRelation,
)
from tmig.parser.connections import COMPARISONS, OPERATORS, parse_relation
from tmig.parser.context import DsState, ParseContext
from tmig.parser.ids import strip_brackets
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import child, children, text_of


@dataclass
class GraphObject:
    element: etree._Element
    id: str
    caption: str
    live: etree._Element | None
    """properties context='' 的 relation（來源資料庫的實體資料表）。"""
    extract: etree._Element | None
    """properties context='extract' 的 relation（.hyper 中的資料表）。"""


def graph_objects(ds_el: etree._Element) -> list[GraphObject]:
    graph = child(ds_el, "object-graph")
    result: list[GraphObject] = []
    for obj in children(child(graph, "objects"), "object"):
        object_id = obj.get("id") or ""
        live = extract = None
        for props in children(obj, "properties"):
            relation = child(props, "relation")
            if props.get("context", "") == "extract":
                extract = relation
            elif props.get("context", "") == "":
                live = relation
        result.append(
            GraphObject(
                obj, object_id, obj.get("caption") or strip_brackets(object_id), live, extract
            )
        )
    return result


def live_tables(
    ctx: ParseContext, objects: list[GraphObject], caption: str
) -> list[LogicalTable] | None:
    """Live 連線的邏輯資料表；有不支援的寫法時列入 unsupported 並回傳 None。"""
    tables: list[LogicalTable] = []
    for obj in objects:
        if obj.live is None:
            ctx.flag(obj.element, f"資料源「{caption}」的邏輯資料表「{obj.caption}」沒有實體資料表")
            return None
        relation = parse_relation(ctx, obj.live, caption)
        if relation is None:
            return None
        if relation.kind == "join":
            ctx.flag(
                obj.element,
                f"資料源「{caption}」的邏輯資料表「{obj.caption}」內含 join（實體層），Live 連線尚不支援",
            )
            return None
        tables.append(LogicalTable(id=obj.id, caption=obj.caption, relation=relation))
    return tables


def extract_tables(
    ctx: ParseContext, objects: list[GraphObject], caption: str
) -> list[LogicalTable] | None:
    """萃取的邏輯資料表：每張是 .hyper 中的一張資料表（實體層的 join 已存成單表）。"""
    tables: list[LogicalTable] = []
    for obj in objects:
        parts = qualified_parts(obj.extract.get("table", "")) if obj.extract is not None else []
        if len(parts) != 2:
            ctx.flag(
                obj.element, f"資料源「{caption}」的邏輯資料表「{obj.caption}」找不到萃取的資料表"
            )
            return None
        relation: Relation = TableRelation(kind="table", name=obj.id, table=parts)
        tables.append(LogicalTable(id=obj.id, caption=obj.caption, relation=relation))
    return tables


def object_id_of(record: etree._Element) -> str | None:
    """metadata-record 所屬的邏輯資料表：object-id（Live），或 parent-name（萃取時即 object id）。"""
    found = child(record, "object-id")
    text = text_of(found) if found is not None else ""
    return strip_brackets(text) or strip_brackets(text_of(child(record, "parent-name"))) or None


def relationships(
    ctx: ParseContext, ds_el: etree._Element, state: DsState, table_ids: set[str], caption: str
) -> list[Relationship] | None:
    graph = child(ds_el, "object-graph")
    result: list[Relationship] = []
    for rel in children(child(graph, "relationships"), "relationship"):
        first = child(rel, "first-end-point")
        second = child(rel, "second-end-point")
        left = first.get("object-id") if first is not None else None
        right = second.get("object-id") if second is not None else None
        expression = child(rel, "expression")
        if left not in table_ids or right not in table_ids or expression is None:
            ctx.flag(rel, f"資料源「{caption}」的關聯無法解析")
            return None
        assert left is not None and right is not None
        conditions = _conditions(expression, state, left, right)
        if not conditions:
            ctx.flag(rel, f"資料源「{caption}」的關聯條件不是欄位比較（例如含計算），尚不支援")
            return None
        result.append(Relationship(left=left, right=right, on=conditions))
    if not result:
        ctx.flag(
            graph if graph is not None else ds_el,
            f"資料源「{caption}」的 Relationship 模型沒有關聯",
        )
        return None
    return result


def _conditions(
    expression: etree._Element, state: DsState, left: str, right: str
) -> list[RelationshipCondition] | None:
    op = expression.get("op", "")
    operands = children(expression, "expression")
    if op.upper() == "AND":
        result: list[RelationshipCondition] = []
        for item in operands:
            found = _conditions(item, state, left, right)
            if found is None:
                return None
            result.extend(found)
        return result
    op = OPERATORS.get(op, op)
    if op not in COMPARISONS or len(operands) != 2:
        return None
    fields = []
    for operand in operands:
        if children(operand):
            return None
        data_field = state.by_name.get(operand.get("op", ""))
        if data_field is None or data_field.column is None:
            return None
        fields.append(data_field)
    a, b = fields
    # 條件兩邊的順序依欄位所屬的資料表對齊 left、right（不等式隨之反向）
    if a.table == right and b.table == left:
        a, b = b, a
        op = FLIPPED.get(op, op)
    if a.table != left or b.table != right:
        return None
    return [RelationshipCondition(left=a.id, op=op, right=b.id)]  # type: ignore[arg-type]


FLIPPED = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}


__all__ = [
    "GraphObject",
    "extract_tables",
    "graph_objects",
    "live_tables",
    "object_id_of",
    "relationships",
]
