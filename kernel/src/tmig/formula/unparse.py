"""語法樹 → 公式文字（parse_formula 可以再解析成相同的語法樹；運算一律加括號）。

用在把公式的一部分拆出來另外計算，例如 Data Blending 中引用次要資料源欄位的計算欄位
（tmig.sqlgen.blend）。
"""

from __future__ import annotations

from tmig.formula.ast import Binary, Call, Case, FieldRef, If, InList, LiteralNode, Lod, Node, Unary


def to_formula(node: Node) -> str:
    match node:
        case LiteralNode(kind=kind, value=value):
            if kind == "null":
                return "NULL"
            if kind == "boolean":
                return "TRUE" if value else "FALSE"
            if kind == "string":
                return "'" + str(value).replace("'", "''") + "'"
            if kind in ("date", "datetime"):
                return f"#{str(value).replace('T', ' ')}#"
            return repr(value)
        case FieldRef():
            return node.text
        case Unary(op=op, operand=operand):
            return f"({op} {to_formula(operand)})"
        case Binary(op=op, left=left, right=right):
            return f"({to_formula(left)} {op} {to_formula(right)})"
        case InList(operand=operand, values=values):
            return f"({to_formula(operand)} IN ({', '.join(to_formula(v) for v in values)}))"
        case Call(name=name, args=args):
            return f"{name}({', '.join(to_formula(a) for a in args)})"
        case If(branches=branches, else_=else_):
            parts: list[str] = []
            for index, (cond, then) in enumerate(branches):
                parts.append(f"{'IF' if index == 0 else 'ELSEIF'} {to_formula(cond)}")
                parts.append(f"THEN {to_formula(then)}")
            if else_ is not None:
                parts.append(f"ELSE {to_formula(else_)}")
            return f"({' '.join(parts)} END)"
        case Case(subject=subject, whens=whens, else_=else_):
            parts = [f"CASE {to_formula(subject)}"]
            for when, then in whens:
                parts.append(f"WHEN {to_formula(when)} THEN {to_formula(then)}")
            if else_ is not None:
                parts.append(f"ELSE {to_formula(else_)}")
            return f"({' '.join(parts)} END)"
        case Lod(kind=kind, dims=dims, expr=expr):
            listed = ", ".join(to_formula(d) for d in dims)
            head = f"{kind.upper()} {listed} : " if dims or kind != "fixed" else ""
            return "{ " + head + to_formula(expr) + " }"


__all__ = ["to_formula"]
