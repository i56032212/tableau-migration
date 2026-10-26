"""語法樹的 S 運算式表示法，用於測試語料與除錯輸出。

格式（未來 TypeScript 版解析器以同一份語料驗證）：

- 常值：``1``、``1.5``、``"abc"``、``TRUE``、``NULL``、``#2024-01-01#``、``#2024-01-01T10:00:00#``
- 欄位：``[Sales]``、``[Parameters].[Top N]``
- 運算：``(+ a b)``、``(- a)``、``(NOT a)``、``(IN a (b c))``
- 函式：``(SUM [Sales])``
- IF：``(IF c1 t1 c2 t2 ELSE e)``；CASE：``(CASE s w1 t1 ELSE e)``
- LOD：``(FIXED ([Region]) (SUM [Sales]))``
"""

from __future__ import annotations

import json

from tmig.formula.ast import Binary, Call, Case, FieldRef, If, InList, LiteralNode, Lod, Node, Unary


def to_sexpr(node: Node) -> str:
    match node:
        case LiteralNode(kind=kind, value=value):
            if kind == "null":
                return "NULL"
            if kind == "boolean":
                return "TRUE" if value else "FALSE"
            if kind == "string":
                return json.dumps(value, ensure_ascii=False)
            if kind in ("date", "datetime"):
                return f"#{value}#"
            return repr(value)
        case FieldRef():
            return node.text
        case Unary(op=op, operand=operand):
            return f"({op} {to_sexpr(operand)})"
        case Binary(op=op, left=left, right=right):
            return f"({op} {to_sexpr(left)} {to_sexpr(right)})"
        case InList(operand=operand, values=values):
            return f"(IN {to_sexpr(operand)} ({' '.join(to_sexpr(v) for v in values)}))"
        case Call(name=name, args=args):
            return f"({' '.join([name, *(to_sexpr(a) for a in args)])})"
        case If(branches=branches, else_=else_):
            parts = ["IF"]
            for cond, then in branches:
                parts += [to_sexpr(cond), to_sexpr(then)]
            if else_ is not None:
                parts += ["ELSE", to_sexpr(else_)]
            return f"({' '.join(parts)})"
        case Case(subject=subject, whens=whens, else_=else_):
            parts = ["CASE", to_sexpr(subject)]
            for when, then in whens:
                parts += [to_sexpr(when), to_sexpr(then)]
            if else_ is not None:
                parts += ["ELSE", to_sexpr(else_)]
            return f"({' '.join(parts)})"
        case Lod(kind=kind, dims=dims, expr=expr):
            dim_text = " ".join(d.text for d in dims)
            return f"({kind.upper()} ({dim_text}) {to_sexpr(expr)})"
