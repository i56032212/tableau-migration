"""地名的正規化：建資料（tools/geodata/build.py）與比對時用同一個函式。

不分大小寫、去掉重音符號與標點（St. Louis、St Louis 相同）、全形轉半形，「臺」與「台」相同，& 與 and 相同。
"""

from __future__ import annotations

import re
import unicodedata

_PUNCTUATION = re.compile(r"[.,'’‘`´\"“”\-‐‑–—_/\\()\[\]{}:;!?·・、，。]+")
_SPACES = re.compile(r"\s+")


def normalize(text: object) -> str:
    """比對用的名稱；空字串表示沒有可比對的內容。"""
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).casefold()
    value = value.replace("臺", "台").replace("&", " and ")
    value = "".join(c for c in unicodedata.normalize("NFD", value) if not unicodedata.combining(c))
    value = _PUNCTUATION.sub(" ", value)
    return _SPACES.sub(" ", value).strip()


def variants(text: object) -> set[str]:
    """一個名稱的比對鍵：本身，加上 Saint／St、Mount／Mt、Fort／Ft 的互換。"""
    base = normalize(text)
    if not base:
        return set()
    found = {base}
    swaps = (("saint ", "st "), ("sainte ", "ste "), ("mount ", "mt "), ("fort ", "ft "))
    for long, short in swaps:
        for value in list(found):
            if value.startswith(long):
                found.add(short + value[len(long) :])
            elif value.startswith(short):
                found.add(long + value[len(short) :])
            if f" {long}" in value:
                found.add(value.replace(f" {long}", f" {short}"))
    return found
