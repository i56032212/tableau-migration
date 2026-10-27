"""IR 穩定 id 的產生規則。

- 格式：``<前綴>_<代稱>``，小寫英數字與底線，最長 64 字元
- 代稱依序取候選名稱中第一個能轉成英數字的（例如顯示名稱、Tableau 內部名稱）；
  含中文等非英文字母的名稱不採用，以免只剩數字而失去意義
- 全部候選都不能用時，以第一個候選的 SHA-1 前 8 碼代替
- 同前綴重複時依序加上 _2、_3
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """轉成小寫英數字與底線；含無法轉成英文字母的字（如中文）時回傳空字串。"""
    for ch in text:
        if ch.isalpha() and not ch.isascii():
            base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore")
            if not base:
                return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM.sub("_", ascii_text.lower()).strip("_")
    if slug.startswith("calculation_"):
        slug = "calc_" + slug.removeprefix("calculation_")
    return slug


def strip_brackets(name: str) -> str:
    if len(name) >= 2 and name[0] == "[" and name[-1] == "]":
        return name[1:-1].replace("]]", "]")
    return name


class IdAllocator:
    """在一份 IR 內配發不重複的 id。"""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def reserve(self, value: str) -> None:
        self._used.add(value)

    def make(self, prefix: str, *candidates: str | None) -> str:
        texts = [strip_brackets(c) for c in candidates if c]
        slug = next((s for s in (slugify(t) for t in texts) if s), "")
        if not slug:
            seed = texts[0] if texts else prefix
            slug = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
        base = f"{prefix}_{slug}"[:64].rstrip("_")
        value = base
        n = 2
        while value in self._used:
            suffix = f"_{n}"
            value = base[: 64 - len(suffix)].rstrip("_") + suffix
            n += 1
        self._used.add(value)
        return value
