"""公式語法樹。

節點不可變，``pos`` 是節點在公式中的起始位置。語法樹可轉成 JSON 存進 IR 的
``calc.ast``；欄位參照在分析後會附上解析出的 IR id。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

LiteralKind = Literal["integer", "real", "string", "boolean", "date", "datetime", "null"]
LodKind = Literal["fixed", "include", "exclude"]


@dataclass(frozen=True, slots=True)
class LiteralNode:
    pos: int
    kind: LiteralKind
    value: Any
    """integer → int、real → float、string → str、boolean → bool、date／datetime → ISO 字串、null → None。"""


@dataclass(frozen=True, slots=True)
class FieldRef:
    pos: int
    parts: tuple[str, ...]
    """方括號內的名稱，例如 ("Sales",) 或 ("Parameters", "Top N")。"""

    @property
    def text(self) -> str:
        return ".".join("[" + part.replace("]", "]]") + "]" for part in self.parts)


@dataclass(frozen=True, slots=True)
class Unary:
    pos: int
    op: Literal["-", "NOT"]
    operand: Node


@dataclass(frozen=True, slots=True)
class Binary:
    pos: int
    op: str
    """+ - * / % ^ = != < <= > >= AND OR；== 正規化為 =，<> 正規化為 !=。"""
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class InList:
    pos: int
    operand: Node
    values: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Call:
    pos: int
    name: str
    """大寫函式名稱。"""
    args: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class If:
    pos: int
    branches: tuple[tuple[Node, Node], ...]
    """(條件, 結果)，依序為 IF 與各 ELSEIF。"""
    else_: Node | None


@dataclass(frozen=True, slots=True)
class Case:
    pos: int
    subject: Node
    whens: tuple[tuple[Node, Node], ...]
    else_: Node | None


@dataclass(frozen=True, slots=True)
class Lod:
    pos: int
    kind: LodKind
    dims: tuple[FieldRef, ...]
    """{ SUM([Sales]) } 這種沒有關鍵字的寫法視為不帶維度的 FIXED。"""
    expr: Node


Node = LiteralNode | FieldRef | Unary | Binary | InList | Call | If | Case | Lod


def children(node: Node) -> Iterator[Node]:
    """直接子節點，依公式中的順序。"""
    match node:
        case LiteralNode() | FieldRef():
            return
        case Unary(operand=operand):
            yield operand
        case Binary(left=left, right=right):
            yield left
            yield right
        case InList(operand=operand, values=values):
            yield operand
            yield from values
        case Call(args=args):
            yield from args
        case If(branches=branches, else_=else_):
            for cond, then in branches:
                yield cond
                yield then
            if else_ is not None:
                yield else_
        case Case(subject=subject, whens=whens, else_=else_):
            yield subject
            for when, then in whens:
                yield when
                yield then
            if else_ is not None:
                yield else_
        case Lod(dims=dims, expr=expr):
            yield from dims
            yield expr


def walk(node: Node) -> Iterator[Node]:
    """前序走訪所有節點。"""
    yield node
    for child in children(node):
        yield from walk(child)


# ---------------------------------------------------------------- JSON

Resolver = Callable[[FieldRef], dict[str, str] | None]
"""把欄位參照轉成要附加的資訊，例如 {"target": "field", "id": "f_sales"}。"""


def to_json(node: Node, resolve: Resolver | None = None) -> dict[str, Any]:
    def conv(n: Node) -> dict[str, Any]:
        match n:
            case LiteralNode(kind=kind, value=value):
                return {"node": "literal", "kind": kind, "value": value}
            case FieldRef(parts=parts):
                data: dict[str, Any] = {"node": "field", "ref": n.text, "parts": list(parts)}
                extra = resolve(n) if resolve is not None else None
                if extra:
                    data.update(extra)
                return data
            case Unary(op=op, operand=operand):
                return {"node": "unary", "op": op, "operand": conv(operand)}
            case Binary(op=op, left=left, right=right):
                return {"node": "binary", "op": op, "left": conv(left), "right": conv(right)}
            case InList(operand=operand, values=values):
                return {"node": "in", "operand": conv(operand), "values": [conv(v) for v in values]}
            case Call(name=name, args=args):
                return {"node": "call", "name": name, "args": [conv(a) for a in args]}
            case If(branches=branches, else_=else_):
                return {
                    "node": "if",
                    "branches": [{"cond": conv(c), "then": conv(t)} for c, t in branches],
                    "else": conv(else_) if else_ is not None else None,
                }
            case Case(subject=subject, whens=whens, else_=else_):
                return {
                    "node": "case",
                    "subject": conv(subject),
                    "whens": [{"when": conv(w), "then": conv(t)} for w, t in whens],
                    "else": conv(else_) if else_ is not None else None,
                }
            case Lod(kind=kind, dims=dims, expr=expr):
                return {
                    "node": "lod",
                    "kind": kind,
                    "dims": [conv(d) for d in dims],
                    "expr": conv(expr),
                }

    return conv(node)


def from_json(data: dict[str, Any]) -> Node:
    """由 to_json 的輸出重建語法樹（位置資訊設為 0，解析出的 id 不保留）。"""
    kind = data["node"]
    if kind == "literal":
        return LiteralNode(0, data["kind"], data["value"])
    if kind == "field":
        return FieldRef(0, tuple(data["parts"]))
    if kind == "unary":
        return Unary(0, data["op"], from_json(data["operand"]))
    if kind == "binary":
        return Binary(0, data["op"], from_json(data["left"]), from_json(data["right"]))
    if kind == "in":
        return InList(0, from_json(data["operand"]), tuple(from_json(v) for v in data["values"]))
    if kind == "call":
        return Call(0, data["name"], tuple(from_json(a) for a in data["args"]))
    if kind == "if":
        branches = tuple((from_json(b["cond"]), from_json(b["then"])) for b in data["branches"])
        else_ = from_json(data["else"]) if data["else"] is not None else None
        return If(0, branches, else_)
    if kind == "case":
        whens = tuple((from_json(w["when"]), from_json(w["then"])) for w in data["whens"])
        else_ = from_json(data["else"]) if data["else"] is not None else None
        return Case(0, from_json(data["subject"]), whens, else_)
    if kind == "lod":
        dims = tuple(from_json(d) for d in data["dims"])
        if not all(isinstance(d, FieldRef) for d in dims):
            raise ValueError("LOD 的維度必須是欄位")
        return Lod(0, data["kind"], dims, from_json(data["expr"]))  # type: ignore[arg-type]
    raise ValueError(f"未知的節點類型：{kind}")


def strip_positions(node: Node) -> Node:
    """位置歸零，用來比較兩棵樹的結構是否相同。"""
    return from_json(to_json(node))
