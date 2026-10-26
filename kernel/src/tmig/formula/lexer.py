"""Tableau 公式的詞法分析。

規則：

- 註解：``//`` 到行尾，``/* … */`` 區塊
- 字串：單引號或雙引號，引號本身以連續兩個表示，例如 ``'it''s'``
- 欄位：方括號，名稱內的 ``]`` 以 ``]]`` 表示；``[Parameters].[Top N]`` 由解析器組合
- 日期：``#2025-01-31#``、``#2025-01-31 08:00:00#``
- 數字：``1``、``1.5``、``.5``、``1e3``
- 關鍵字與函式名稱不分大小寫
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_TWO_CHAR_OPS = ("<=", ">=", "!=", "==", "<>")
_ONE_CHAR_OPS = "+-*/%^=<>"
_PUNCT = "{}(),:."


class FormulaSyntaxError(ValueError):
    """公式無法解析。``pos`` 為錯誤在公式中的字元位置（從 0 起算）。"""

    def __init__(self, message: str, pos: int) -> None:
        super().__init__(f"{message}（位置 {pos}）")
        self.message = message
        self.pos = pos


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    """number、string、date、field、ident、op、punct、eof。"""
    text: str
    """原始文字。"""
    value: str
    """解碼後的值：字串去掉引號、欄位去掉方括號、日期去掉 #。"""
    pos: int


def _read_quoted(text: str, start: int, close: str) -> tuple[str, int] | None:
    """讀取以 close 結束、以連續兩個 close 跳脫的內容；回傳（內容，結束後的位置）。"""
    i = start + 1
    out: list[str] = []
    n = len(text)
    while i < n:
        char = text[i]
        if char == close:
            if i + 1 < n and text[i + 1] == close:
                out.append(close)
                i += 2
                continue
            return "".join(out), i + 1
        out.append(char)
        i += 1
    return None


def tokenize(formula: str) -> list[Token]:
    """把公式切成 token，最後一個一定是 eof；遇到無法辨識的內容丟出 FormulaSyntaxError。"""
    tokens, error = tokenize_partial(formula)
    if error is not None:
        raise error
    return tokens


def tokenize_partial(formula: str) -> tuple[list[Token], FormulaSyntaxError | None]:
    """同 tokenize，但遇到錯誤時回傳錯誤前的 token（不含 eof）與錯誤，供盤點使用。"""
    try:
        return _tokenize(formula), None
    except _Partial as partial:
        return partial.tokens, partial.error


class _Partial(Exception):
    def __init__(self, tokens: list[Token], error: FormulaSyntaxError) -> None:
        self.tokens = tokens
        self.error = error


def _tokenize(formula: str) -> list[Token]:
    tokens: list[Token] = []
    try:
        return _scan(formula, tokens)
    except FormulaSyntaxError as error:
        raise _Partial(tokens, error) from None


def _scan(formula: str, tokens: list[Token]) -> list[Token]:
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
                raise FormulaSyntaxError("區塊註解沒有結束", i)
            i = end + 2
        elif char in "'\"":
            read = _read_quoted(text, i, char)
            if read is None:
                raise FormulaSyntaxError("字串沒有結束", i)
            value, end = read
            tokens.append(Token("string", text[i:end], value, i))
            i = end
        elif char == "[":
            read = _read_quoted(text, i, "]")
            if read is None:
                raise FormulaSyntaxError("欄位名稱的方括號沒有結束", i)
            value, end = read
            tokens.append(Token("field", text[i:end], value, i))
            i = end
        elif char == "#":
            end = text.find("#", i + 1)
            if end < 0:
                raise FormulaSyntaxError("日期常值沒有結束", i)
            tokens.append(Token("date", text[i : end + 1], text[i + 1 : end].strip(), i))
            i = end + 1
        elif match := _IDENT.match(text, i):
            tokens.append(Token("ident", match.group(), match.group(), i))
            i = match.end()
        elif match := _NUMBER.match(text, i):
            tokens.append(Token("number", match.group(), match.group(), i))
            i = match.end()
        elif text.startswith(_TWO_CHAR_OPS, i):
            tokens.append(Token("op", text[i : i + 2], text[i : i + 2], i))
            i += 2
        elif char in _ONE_CHAR_OPS:
            tokens.append(Token("op", char, char, i))
            i += 1
        elif char in _PUNCT:
            tokens.append(Token("punct", char, char, i))
            i += 1
        else:
            raise FormulaSyntaxError(f"無法辨識的字元「{char}」", i)
    tokens.append(Token("eof", "", "", n))
    return tokens
