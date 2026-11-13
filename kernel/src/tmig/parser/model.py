"""Relationship 模型（N6）：object-graph 的邏輯資料表與關聯。

Tableau 2020.2 起，多表的資料源預設是 Relationship 模型：object-graph 的每個 object 是一張邏輯資料表
（Live 連線時 properties context='' 是它的實體資料表；萃取時 context='extract' 是 .hyper 中對應的資料表），
relationships 以欄位（local name）的比較連接兩張邏輯資料表，連成一棵樹。

- 欄位所屬的邏輯資料表：metadata-record 的 object-id（Live），或萃取的 parent-name（即 object id）
- 邏輯資料表內含 join（實體層的 join）：Live 連線與檔案時欄位另記所屬的實體資料表（metadata-record 的
  parent-name，見 DataField.physical_table），查詢時邏輯資料表是 join 的子查詢；萃取已存成單一資料表
- 關聯條件是欄位之間的比較（以 AND 連接）；一邊是公式時（關聯計算，例如
  <expression op='DATETRUNC(&apos;month&apos;,[ORDER_TS])' />）成為所屬邏輯資料表上隱藏的計算欄位
  （DataField.relationship_calc），公式只能用到同一張邏輯資料表的欄位
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from tmig.ir.models import (
    Calc,
    DataField,
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
from tmig.parser.twbx import child, children, text_of, xpath


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
        conditions = _conditions(expression, state, left, right, ctx)
        if not conditions:
            ctx.flag(
                rel,
                f"資料源「{caption}」的關聯條件不是欄位的比較（或只用到一邊資料表欄位的關聯計算），"
                "尚不支援",
            )
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
    expression: etree._Element, state: DsState, left: str, right: str, ctx: ParseContext
) -> list[RelationshipCondition] | None:
    op = expression.get("op", "")
    operands = children(expression, "expression")
    if op.upper() == "AND":
        result: list[RelationshipCondition] = []
        for item in operands:
            found = _conditions(item, state, left, right, ctx)
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
        if data_field is None:
            data_field = _relationship_calc(operand, state, ctx, {left, right})
        if data_field is None or (data_field.column is None and not data_field.relationship_calc):
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


def _relationship_calc(
    operand: etree._Element, state: DsState, ctx: ParseContext, tables: set[str]
) -> DataField | None:
    """關聯條件的一邊是公式（關聯計算）：公式中的欄位都屬於關聯兩邊的同一張邏輯資料表時，成為那張資料表
    上隱藏的計算欄位（同一張資料表的同一個公式共用一個）。"""
    from tmig.formula.analyze import Analyzer
    from tmig.formula.ast import FieldRef, walk
    from tmig.formula.lexer import FormulaSyntaxError
    from tmig.formula.parser import parse_formula
    from tmig.formula.types import FormulaSemanticError
    from tmig.ir.scope import DatasourceScope

    formula = operand.get("op", "").strip()
    if not formula:
        return None
    try:
        node = parse_formula(formula)
    except FormulaSyntaxError:
        return None
    owners: set[str | None] = set()
    for ref in walk(node):
        if not isinstance(ref, FieldRef):
            continue
        if len(ref.parts) != 1:
            return None
        found = state.by_name.get("[" + ref.parts[0].replace("]", "]]") + "]")
        if found is None or found.column is None:
            return None
        owners.add(found.table)
    if len(owners) != 1:
        return None
    (table,) = owners
    if table is None or table not in tables:
        return None
    for existing in state.fields:
        if (
            existing.relationship_calc
            and existing.table == table
            and existing.calc is not None
            and existing.calc.formula == formula
        ):
            return existing
    try:
        info = Analyzer(DatasourceScope(state.name, state.fields, [])).analyze(node)
    except FormulaSemanticError:
        return None
    if (
        info.level != "row"
        or info.has_lod
        or info.datatype
        not in (
            "string",
            "integer",
            "real",
            "boolean",
            "date",
            "datetime",
        )
    ):
        return None
    count = sum(1 for f in state.fields if f.relationship_calc) + 1
    name = f"[Relationship Calculation {count}]"
    data_field = DataField(
        id=ctx.ids.make("f", f"relationship calc {count}", name),
        name=name,
        caption=formula,
        role="dimension",
        datatype=info.datatype,  # type: ignore[arg-type]
        hidden=True,
        calc=Calc(formula=formula),
        table=table,
        relationship_calc=True,
        source_ref=xpath(operand),
    )
    state.add_field(data_field)
    return data_field


__all__ = [
    "GraphObject",
    "extract_tables",
    "graph_objects",
    "live_tables",
    "object_id_of",
    "relationships",
]
