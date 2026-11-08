"""Tableau 公式解析器（Pratt parser）。

運算子優先順序依 Tableau 文件，由高到低：

1. 負號 ``-``
2. ``^``
3. ``*`` ``/`` ``%``
4. ``+`` ``-``
5. 比較 ``=`` ``==`` ``!=`` ``<>`` ``<`` ``<=`` ``>`` ``>=`` 與 ``IN``
6. ``NOT``
7. ``AND``
8. ``OR``

手寫而非 ANTLR 產生：開發環境無法下載 ANTLR 工具，且手寫版不需要 Java 建置步驟。
未來的 TypeScript 版以 ``kernel/tests/formula/corpus.json`` 這份共用語料確保行為一致。
"""

from __future__ import annotations

from tmig.formula.ast import (
    Binary,
    Call,
    Case,
    FieldRef,
    If,
    InList,
    LiteralNode,
    Lod,
    LodKind,
    Node,
    Unary,
)
from tmig.formula.lexer import FormulaSyntaxError, Token, tokenize

_COMPARISON = {
    "=": "=",
    "==": "=",
    "!=": "!=",
    "<>": "!=",
    "<": "<",
    "<=": "<=",
    ">": ">",
    ">=": ">=",
}
_LOD_KINDS: dict[str, LodKind] = {"FIXED": "fixed", "INCLUDE": "include", "EXCLUDE": "exclude"}

# 左結合力
_BP_OR = 10
_BP_AND = 20
_BP_NOT = 30
_BP_COMPARE = 40
_BP_ADD = 50
_BP_MUL = 60
_BP_POW = 70
_BP_NEGATE = 80

_RESERVED = frozenset(
    {"IF", "THEN", "ELSEIF", "ELSE", "END", "CASE", "WHEN", "AND", "OR", "NOT", "IN"}
    | {"FIXED", "INCLUDE", "EXCLUDE"}
)


def parse_formula(formula: str) -> Node:
    """解析公式；語法錯誤時丟出 FormulaSyntaxError。"""
    parser = _Parser(tokenize(formula))
    if parser.peek().kind == "eof":
        raise FormulaSyntaxError("公式是空的", 0)
    node = parser.expression(0)
    token = parser.peek()
    if token.kind != "eof":
        raise FormulaSyntaxError(f"多出無法解析的內容「{token.text}」", token.pos)
    return node


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    # ------------------------------------------------------------ token 工具

    def peek(self, offset: int = 0) -> Token:
        index = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def advance(self) -> Token:
        token = self.peek()
        if token.kind != "eof":
            self.index += 1
        return token

    def is_word(self, token: Token, word: str) -> bool:
        return token.kind == "ident" and token.text.upper() == word

    def expect_word(self, word: str) -> Token:
        token = self.advance()
        if not self.is_word(token, word):
            raise FormulaSyntaxError(
                f"這裡應該是 {word}，卻是「{token.text or '公式結尾'}」", token.pos
            )
        return token

    def expect_punct(self, char: str) -> Token:
        token = self.advance()
        if token.kind != "punct" or token.text != char:
            raise FormulaSyntaxError(
                f"這裡應該是「{char}」，卻是「{token.text or '公式結尾'}」", token.pos
            )
        return token

    # ------------------------------------------------------------ 運算式

    def infix_power(self, token: Token) -> int | None:
        if token.kind == "op":
            if token.text in _COMPARISON:
                return _BP_COMPARE
            if token.text in ("+", "-"):
                return _BP_ADD
            if token.text in ("*", "/", "%"):
                return _BP_MUL
            if token.text == "^":
                return _BP_POW
        if token.kind == "ident":
            word = token.text.upper()
            if word == "OR":
                return _BP_OR
            if word == "AND":
                return _BP_AND
            if word == "IN":
                return _BP_COMPARE
        return None

    def expression(self, min_power: int) -> Node:
        left = self.prefix()
        while True:
            token = self.peek()
            power = self.infix_power(token)
            if power is None or power < min_power:
                return left
            self.advance()
            word = token.text.upper() if token.kind == "ident" else token.text
            if word == "IN":
                left = self.in_list(left, token)
            elif word in ("AND", "OR"):
                left = Binary(token.pos, word, left, self.expression(power + 1))
            elif token.text in _COMPARISON:
                left = Binary(token.pos, _COMPARISON[token.text], left, self.expression(power + 1))
            else:
                left = Binary(token.pos, token.text, left, self.expression(power + 1))

    def in_list(self, operand: Node, token: Token) -> Node:
        self.expect_punct("(")
        values = [self.expression(0)]
        while self.peek().text == "," and self.peek().kind == "punct":
            self.advance()
            values.append(self.expression(0))
        self.expect_punct(")")
        return InList(token.pos, operand, tuple(values))

    def prefix(self) -> Node:
        token = self.advance()
        kind = token.kind
        if kind == "number":
            text = token.text
            if any(c in text for c in ".eE"):
                return LiteralNode(token.pos, "real", float(text))
            return LiteralNode(token.pos, "integer", int(text))
        if kind == "string":
            return LiteralNode(token.pos, "string", token.value)
        if kind == "date":
            return self.date_literal(token)
        if kind == "field":
            parts = [token.value]
            while self.peek().text == "." and self.peek(1).kind == "field":
                self.advance()
                parts.append(self.advance().value)
            return FieldRef(token.pos, tuple(parts))
        if kind == "op" and token.text in ("-", "+"):
            operand = self.expression(_BP_NEGATE)
            if token.text == "+":
                return operand
            if isinstance(operand, LiteralNode) and operand.kind in ("integer", "real"):
                return LiteralNode(token.pos, operand.kind, -operand.value)
            return Unary(token.pos, "-", operand)
        if kind == "punct" and token.text == "(":
            node = self.expression(0)
            self.expect_punct(")")
            return node
        if kind == "punct" and token.text == "{":
            return self.lod(token)
        if kind == "ident":
            return self.word(token)
        if kind == "eof":
            raise FormulaSyntaxError("公式不完整", token.pos)
        raise FormulaSyntaxError(f"這裡不能出現「{token.text}」", token.pos)

    def word(self, token: Token) -> Node:
        word = token.text.upper()
        if word == "TRUE":
            return LiteralNode(token.pos, "boolean", True)
        if word == "FALSE":
            return LiteralNode(token.pos, "boolean", False)
        if word == "NULL":
            return LiteralNode(token.pos, "null", None)
        if word == "NOT":
            return Unary(token.pos, "NOT", self.expression(_BP_NOT))
        if word == "IF":
            return self.if_expression(token)
        if word == "CASE":
            return self.case_expression(token)
        if word in _RESERVED:
            raise FormulaSyntaxError(f"這裡不能出現 {word}", token.pos)
        if self.peek().text == "(" and self.peek().kind == "punct":
            self.advance()
            args: list[Node] = []
            if not (self.peek().kind == "punct" and self.peek().text == ")"):
                args.append(self.expression(0))
                while self.peek().kind == "punct" and self.peek().text == ",":
                    self.advance()
                    args.append(self.expression(0))
            self.expect_punct(")")
            return Call(token.pos, word, tuple(args))
        raise FormulaSyntaxError(f"「{token.text}」不是函式呼叫，也不是關鍵字", token.pos)

    def date_literal(self, token: Token) -> Node:
        text = token.value
        date_part, _, time_part = text.partition(" ")
        pieces = date_part.split("-")
        if len(pieces) != 3 or not all(p.isdigit() for p in pieces):
            raise FormulaSyntaxError(
                f"無法辨識的日期常值「{token.text}」，只支援 #yyyy-mm-dd#", token.pos
            )
        year, month, day = (int(p) for p in pieces)
        iso = f"{year:04d}-{month:02d}-{day:02d}"
        if not time_part.strip():
            return LiteralNode(token.pos, "date", iso)
        clock = time_part.strip().split(":")
        if not (2 <= len(clock) <= 3) or not all(c.replace(".", "", 1).isdigit() for c in clock):
            raise FormulaSyntaxError(f"無法辨識的時間「{time_part.strip()}」", token.pos)
        hh, mm = int(clock[0]), int(clock[1])
        ss = clock[2] if len(clock) == 3 else "00"
        return LiteralNode(token.pos, "datetime", f"{iso}T{hh:02d}:{mm:02d}:{ss}")

    def if_expression(self, token: Token) -> Node:
        branches: list[tuple[Node, Node]] = []
        condition = self.expression(0)
        self.expect_word("THEN")
        branches.append((condition, self.expression(0)))
        else_: Node | None = None
        while True:
            nxt = self.peek()
            if self.is_word(nxt, "ELSEIF"):
                self.advance()
                condition = self.expression(0)
                self.expect_word("THEN")
                branches.append((condition, self.expression(0)))
            elif self.is_word(nxt, "ELSE"):
                self.advance()
                else_ = self.expression(0)
                self.expect_word("END")
                break
            elif self.is_word(nxt, "END"):
                self.advance()
                break
            else:
                raise FormulaSyntaxError(
                    f"IF 之後應該接 ELSEIF、ELSE 或 END，卻是「{nxt.text or '公式結尾'}」", nxt.pos
                )
        return If(token.pos, tuple(branches), else_)

    def case_expression(self, token: Token) -> Node:
        subject = self.expression(0)
        whens: list[tuple[Node, Node]] = []
        else_: Node | None = None
        while self.is_word(self.peek(), "WHEN"):
            self.advance()
            when = self.expression(0)
            self.expect_word("THEN")
            whens.append((when, self.expression(0)))
        if not whens:
            nxt = self.peek()
            raise FormulaSyntaxError("CASE 至少要有一個 WHEN", nxt.pos)
        if self.is_word(self.peek(), "ELSE"):
            self.advance()
            else_ = self.expression(0)
        self.expect_word("END")
        return Case(token.pos, subject, tuple(whens), else_)

    def lod(self, token: Token) -> Node:
        first = self.peek()
        kind = _LOD_KINDS.get(first.text.upper()) if first.kind == "ident" else None
        if kind is None:
            expr = self.expression(0)
            self.expect_punct("}")
            return Lod(token.pos, "fixed", (), expr)
        self.advance()
        dims: list[Node] = []
        if not (self.peek().kind == "punct" and self.peek().text == ":"):
            while True:
                # 維度可以是欄位或列層級的運算式（分析時檢查層級）
                dims.append(self.expression(0))
                if self.peek().kind == "punct" and self.peek().text == ",":
                    self.advance()
                    continue
                break
        self.expect_punct(":")
        expr = self.expression(0)
        self.expect_punct("}")
        return Lod(token.pos, kind, tuple(dims), expr)


def _pos(node: Node) -> int:
    return node.pos
