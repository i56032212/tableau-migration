"""Tableau 欄位參照的拆解。

.twb 中的參照以方括號包住名稱，名稱內的 ``]`` 寫成 ``]]``，例如
``[Orders].[sum:Sales:qk]``。欄列架的文字由參照與運算子組成：
``/`` 為巢狀、``*`` 為交叉、``+`` 搭配括號為合併軸或雙軸。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instance:
    """欄位實例名稱，例如 ``sum:Sales:qk`` 拆成前綴 ``("sum",)``、欄位 ``Sales``、型態 ``qk``。"""

    prefixes: tuple[str, ...]
    field: str
    kind: str


def split_brackets(text: str) -> tuple[list[str], str]:
    """拆出所有方括號內的名稱，並回傳去掉方括號內容後剩下的文字。"""
    names: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "[":
            rest.append(char)
            i += 1
            continue
        i += 1
        name: list[str] = []
        while i < len(text):
            if text[i] == "]":
                if i + 1 < len(text) and text[i + 1] == "]":
                    name.append("]")
                    i += 2
                    continue
                i += 1
                break
            name.append(text[i])
            i += 1
        names.append("".join(name))
        rest.append("[]")
    return names, "".join(rest)


def qualified_parts(ref: str) -> list[str]:
    """``[Orders].[sum:Sales:qk]`` → ``["Orders", "sum:Sales:qk"]``。"""
    return split_brackets(ref)[0]


def shelf_operators(text: str) -> set[str]:
    """欄列架文字中，方括號以外出現的運算子。"""
    _, rest = split_brackets(text)
    return {char for char in rest if char in "+/*()"}


def parse_instance(name: str, known_prefixes: frozenset[str]) -> Instance:
    """拆解欄位實例名稱。欄位名稱本身可能含冒號，所以前綴只取已知的詞。"""
    parts = name.split(":")
    if len(parts) < 3:
        return Instance((), name, "")
    kind = parts[-1]
    body = parts[:-1]
    prefixes: list[str] = []
    while len(body) > 1 and body[0] in known_prefixes:
        prefixes.append(body.pop(0))
    return Instance(tuple(prefixes), ":".join(body), kind)
