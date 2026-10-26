"""盤點用的公式掃描：只做詞法分析，找出函式、LOD、使用者函式與欄位參照。

這不是公式引擎。正式的語法分析由 B1 的 ANTLR 文法負責；這裡只要能可靠地
略過註解、字串與欄位名稱，數出真正被呼叫的函式即可。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from tmig.formula.catalog import (
    KEYWORDS,
    LOD_KEYWORDS,
    V0_KEYWORDS,
    in_v0,
    is_table_calc_function,
    is_user_function,
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_TWO_CHAR_OPS = ("<=", ">=", "!=", "==", "<>")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int


@dataclass(frozen=True)
class FormulaScan:
    functions: Counter[str] = field(default_factory=Counter)
    """被呼叫的函式（大寫）與次數。"""
    lod: tuple[str, ...] = ()
    """依出現順序的 LOD 種類：FIXED、INCLUDE、EXCLUDE；沒有關鍵字的 {...} 記為 TABLE。"""
    keywords: frozenset[str] = frozenset()
    references: tuple[str, ...] = ()
    """欄位參照，含限定名稱，例如 [Parameters].[Top N]。"""
    errors: tuple[str, ...] = ()

    @property
    def table_calc_functions(self) -> list[str]:
        return sorted(name for name in self.functions if is_table_calc_function(name))

    @property
    def user_functions(self) -> list[str]:
        return sorted(name for name in self.functions if is_user_function(name))

    @property
    def outside_v0(self) -> list[str]:
        names = {name for name in self.functions if not in_v0(name)}
        names |= {f"{word}（運算子）" for word in self.keywords - V0_KEYWORDS}
        return sorted(names)


def tokenize(formula: str) -> tuple[list[Token], list[str]]:
    tokens: list[Token] = []
    errors: list[str] = []
    text = formula
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                errors.append(f"位置 {i}：區塊註解沒有結束")
                break
            i = end + 2
        elif char in "'\"":
            start = i
            i += 1
            closed = False
            while i < n:
                if text[i] == char:
                    if i + 1 < n and text[i + 1] == char:
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                errors.append(f"位置 {start}：字串沒有結束")
                break
            tokens.append(Token("string", text[start:i], start))
        elif char == "[":
            start = i
            i += 1
            closed = False
            while i < n:
                if text[i] == "]":
                    if i + 1 < n and text[i + 1] == "]":
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                errors.append(f"位置 {start}：欄位名稱的方括號沒有結束")
                break
            tokens.append(Token("field", text[start:i], start))
        elif char == "#":
            end = text.find("#", i + 1)
            if end < 0:
                errors.append(f"位置 {i}：日期常值沒有結束")
                break
            tokens.append(Token("date", text[i : end + 1], i))
            i = end + 1
        elif match := _IDENT.match(text, i):
            tokens.append(Token("ident", match.group(), i))
            i = match.end()
        elif match := _NUMBER.match(text, i):
            tokens.append(Token("number", match.group(), i))
            i = match.end()
        elif char in "{}(),:.":
            tokens.append(Token(char, char, i))
            i += 1
        elif text.startswith(_TWO_CHAR_OPS, i):
            tokens.append(Token("op", text[i : i + 2], i))
            i += 2
        else:
            tokens.append(Token("op", char, i))
            i += 1
    return tokens, errors


def scan_formula(formula: str) -> FormulaScan:
    tokens, errors = tokenize(formula)
    functions: Counter[str] = Counter()
    lod: list[str] = []
    keywords: set[str] = set()
    references: list[str] = []
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.kind == "ident":
            upper = token.text.upper()
            if upper in KEYWORDS:
                keywords.add(upper)
            elif following is not None and following.kind == "(":
                functions[upper] += 1
        elif token.kind == "{":
            if following is not None and following.kind == "ident":
                word = following.text.upper()
                lod.append(word if word in LOD_KEYWORDS else "TABLE")
            else:
                lod.append("TABLE")
        elif token.kind == "field":
            previous = tokens[index - 1] if index > 0 else None
            if previous is not None and previous.kind == "." and references:
                references[-1] = f"{references[-1]}.{token.text}"
            else:
                references.append(token.text)
    return FormulaScan(
        functions=functions,
        lod=tuple(lod),
        keywords=frozenset(keywords),
        references=tuple(references),
        errors=tuple(errors),
    )
