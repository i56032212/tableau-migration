"""依檢視中的地理欄位找出每個標記的位置（Tableau 的 Latitude／Longitude (generated)、Geometry (generated)）。

與 Tableau 相同：
- 以檢視中最細的地理角色為準（郵遞區號 > 城市 > 郡 > 州 > 國家），較粗的角色是比對的範圍
  （例如同名的城市以州區分）
- 檢視中沒有國家（或州）時，以資料源的預設國家（州）為範圍（「編輯位置」的設定，.twb 的 semantic-values）
- 「編輯位置」中指定的對應（例如 Florda → Florida）先套用
- 找不到或有多個可能（同名、範圍不足以區分）的值不畫，列為未知
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from tmig.geo.gazetteer import ROLES, Gazetteer, Place, gazetteer
from tmig.geo.names import normalize

NEARBY_KM = 50.0
"""同一個城市在兩份資料中的位置可能略有不同：距離在這之內的候選視為同一個地點。"""


@dataclass(frozen=True)
class GeoSettings:
    """資料源的地理設定（Tableau 的「編輯位置」）。"""

    default_country: str | None = None
    default_state: str | None = None
    aliases: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """各地理角色的欄位中指定的對應：角色 → （原始值 → 地名）。"""


@dataclass(frozen=True)
class Located:
    place: Place | None
    status: Literal["ok", "unknown", "ambiguous"]
    value: Any = None
    """最細的地理角色在這一列的值。"""


def finest(roles: Sequence[str]) -> str | None:
    """檢視中最細的地理角色。"""
    present = [r for r in ROLES if r in roles]
    return present[-1] if present else None


def geocode(
    records: Sequence[Mapping[str, Any]],
    keys: Mapping[str, str],
    settings: GeoSettings | None = None,
    source: Gazetteer | None = None,
) -> list[Located]:
    """每一列的位置；keys 為地理角色 → 記錄中原始值的欄位。"""
    settings = settings or GeoSettings()
    source = source or gazetteer()
    role = finest(list(keys))
    if role is None:
        return [Located(None, "unknown") for _ in records]
    aliases = {
        r: {normalize(k): v for k, v in mapping.items()} for r, mapping in settings.aliases.items()
    }

    def key_of(value: Any, r: str = "") -> str:
        text = "" if value is None else _text(value)
        text = aliases.get(r, {}).get(normalize(text), text)
        if r == "zip":
            text = _zip(text)
        return normalize(text)

    wanted: dict[str, set[str]] = {r: set() for r in keys}
    for record in records:
        for r, column in keys.items():
            wanted[r].add(key_of(record.get(column), r))
    if settings.default_country:
        wanted.setdefault("country", set()).add(key_of(settings.default_country))
    if settings.default_state:
        wanted.setdefault("state", set()).add(key_of(settings.default_state))
    found = {r: source.candidates(r, values) for r, values in wanted.items()}

    default_country = _pick(found.get("country", {}).get(key_of(settings.default_country)))
    default_state = _pick(
        found.get("state", {}).get(key_of(settings.default_state)), country=default_country
    )

    results: list[Located] = []
    for record in records:
        context: dict[str, Place | None] = {}
        status: Literal["ok", "unknown", "ambiguous"] = "ok"
        for r in ROLES:
            if r not in keys:
                continue
            value = record.get(keys[r])
            if value is None or str(value).strip() == "":
                status = "unknown"
                break
            country = context.get("country") or (None if "country" in keys else default_country)
            state = context.get("state") or (None if "state" in keys else default_state)
            if r == "country":
                country = state = None
            elif r == "state":
                state = None
            matches = found[r].get(key_of(value, r)) or []
            chosen = _pick(
                matches,
                country=country,
                state=state if r in ("county", "city", "zip") else None,
                prefer=None if "country" in keys else default_country,
            )
            if chosen is None:
                status = (
                    "ambiguous" if len(matches) > 1 and _narrowed(matches, country) else "unknown"
                )
                break
            context[r] = chosen
        place = context.get(role) if status == "ok" else None
        results.append(Located(place, status, record.get(keys[role])))
    return results


def _text(value: Any) -> str:
    """比對用的文字：整數型的數字（例如以數字儲存的郵遞區號）不帶小數點。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _zip(text: str) -> str:
    """美國郵遞區號：ZIP+4 取前五碼，以數字儲存而少了開頭的 0（例如 623）補成五碼。"""
    text = text.strip()
    if len(text) == 10 and text[5] == "-" and text[:5].isdigit():
        return text[:5]
    if text.isdigit() and len(text) < 5:
        return text.zfill(5)
    return text


def _narrowed(matches: list[tuple[int, Place]], country: Place | None) -> bool:
    """範圍內還有多個候選（同名）：列為不明確，而不是找不到。"""
    if country is None:
        return True
    return sum(1 for _, p in matches if p.country == country.country) > 1


def _pick(
    matches: list[tuple[int, Place]] | None,
    country: Place | None = None,
    state: Place | None = None,
    prefer: Place | None = None,
) -> Place | None:
    """在範圍內的候選中選出一個：同名而不同地點時不選（不明確）。"""
    if not matches:
        return None
    remaining = matches
    if country is not None:
        remaining = [(n, p) for n, p in remaining if p.country == country.country]
    if state is not None:
        within = [(n, p) for n, p in remaining if p.admin1 == state.id]
        remaining = within
    if not remaining:
        return None
    if prefer is not None and len({p.country for _, p in remaining}) > 1:
        preferred = [(n, p) for n, p in remaining if p.country == prefer.country]
        remaining = preferred or remaining
    best = min(n for n, _ in remaining)
    remaining = [(n, p) for n, p in remaining if n == best]
    first = remaining[0][1]
    if all(_distance_km(first, p) <= NEARBY_KM for _, p in remaining[1:]):
        # 同一個地點（兩份資料各有一筆）：取人口資料較完整、排在前面的
        return first
    return None


def _distance_km(a: Place, b: Place) -> float:
    lat = math.radians((a.lat + b.lat) / 2)
    dx = (a.lon - b.lon) * math.cos(lat) * 111.32
    dy = (a.lat - b.lat) * 110.57
    return math.hypot(dx, dy)
