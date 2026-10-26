"""盤點用的公式掃描：找出函式、LOD、使用者函式與欄位參照。

只做詞法層級的掃描，不需要公式完整合法；正式的語法分析在 ``tmig.formula.parser``。
"""

from __future__ import annotations

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
from tmig.formula.lexer import tokenize_partial


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


def scan_formula(formula: str) -> FormulaScan:
    tokens, error = tokenize_partial(formula)
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
            elif following is not None and following.text == "(":
                functions[upper] += 1
        elif token.text == "{" and token.kind == "punct":
            if following is not None and following.kind == "ident":
                word = following.text.upper()
                lod.append(word if word in LOD_KEYWORDS else "TABLE")
            else:
                lod.append("TABLE")
        elif token.kind == "field":
            previous = tokens[index - 1] if index > 0 else None
            if previous is not None and previous.text == "." and references:
                references[-1] = f"{references[-1]}.{token.text}"
            else:
                references.append(token.text)
    return FormulaScan(
        functions=functions,
        lod=tuple(lod),
        keywords=frozenset(keywords),
        references=tuple(references),
        errors=(str(error),) if error is not None else (),
    )
