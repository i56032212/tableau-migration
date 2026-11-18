"""色彩的比例尺：與 Tableau 相同的色盤、指定給各值的顏色與連續色階（Vega-Lite 的 scale）。

離散：工作表或資料源中指定給各值的顏色；沒有指定的值依色盤的順序（預設 Tableau 10），
色盤是連續型時（例如以 Blue 指定給離散欄位）平均取色。

連續（與 Tableau 的預設相同）：
- 沒有設定時，值有正有負用 Orange-Blue Diverging（以 0 為中心），否則用 Blue，範圍為資料的最小值到最大值
- 發散色盤的中心預設為 0；兩邊對稱，離中心較遠的一端用到最深的顏色，另一端依比例
  （勾選 Use Full Color Range 時兩端都用到最深的顏色；也指定了範圍的起訖時同樣以起訖為兩端）
- 分段：在範圍內等分成 N 段，每段的顏色從色盤平均取出
- 範圍之外的值用兩端的顏色
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
from collections.abc import Iterable, Sequence
from typing import Any

from tmig.ir.models import ColorPalette, Scalar, ShapeAssignment, ShapeName
from tmig.natural import natural_key
from tmig.render.palettes import (
    AUTOMATIC_DIVERGING,
    AUTOMATIC_SEQUENTIAL,
    DEFAULT_REGULAR,
    PALETTES,
    palette,
    sample,
)


def value_key(value: Any) -> tuple[str, Any]:
    """比對值用的鍵：數值不分整數與小數，日期以 ISO 8601 字串比對。"""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float):
        return ("number", float(value))
    if isinstance(value, dt.datetime):
        return ("text", value.isoformat())
    if isinstance(value, dt.date):
        return ("text", value.isoformat())
    return ("text", str(value))


def discrete_scale(
    setting: ColorPalette | None,
    pairs: Iterable[tuple[Any, Any]],
    keys: dict[Scalar, Any] | None = None,
    members_first: bool = False,
) -> dict[str, Any] | None:
    """離散色彩的比例尺：domain 為資料中的顯示值，range 為各值的顏色。

    與 Tableau 相同，顏色屬於欄位的成員、不隨工作表的排序改變：成員依原值排序（數字由小到大、文字不分大小寫、
    Null 在最後），圖例也依這個順序；沒有指定顏色的成員依序取色盤的顏色。Measure Names（keys）依量值在檢視中的
    順序（出現的順序）。集合（members_first）的「內」（True）在「外」之前。

    pairs：（顯示值、原值）；keys：指定的值換成原值（Measure Names 時量值的欄位鍵 → 顯示名稱）。
    沒有設定時回傳 None（Vega-Lite 的預設即 Tableau 10）。"""
    if setting is None or setting.kind != "discrete":
        return None
    assigned: dict[tuple[str, Any], str] = {}
    for item in setting.assignments:
        value = keys.get(item.value, item.value) if keys is not None else item.value
        assigned.setdefault(value_key(value), item.color)
    labels: list[Any] = []
    raw: dict[Any, Any] = {}
    for label, value in pairs:
        if label not in raw:
            raw[label] = value
            labels.append(label)
    if not labels:
        return None
    if keys is None:
        labels.sort(key=lambda label: member_order(raw[label], members_first))
    chosen = palette(setting.palette)
    if chosen is None or not chosen.colors:
        chosen = PALETTES[DEFAULT_REGULAR]
    fallback = (
        [chosen.colors[i % len(chosen.colors)] for i in range(len(labels))]
        if chosen.kind == "regular"
        else sample(chosen.colors, len(labels))
    )
    colors = [assigned.get(value_key(raw[label]), fallback[i]) for i, label in enumerate(labels)]
    return {"domain": labels, "range": colors}


SHAPE_ORDER: tuple[ShapeName, ...] = (
    "circle",
    "square",
    "plus",
    "times",
    "asterisk",
    "diamond",
    "triangle",
    "down_triangle",
    "left_triangle",
    "right_triangle",
)
"""Tableau 預設形狀調色盤的順序。"""
VEGA_SHAPES: dict[ShapeName, str] = {
    "circle": "circle",
    "square": "square",
    "plus": "cross",
    # 叉號與星號：Vega 沒有內建的形狀，以路徑畫出（單位大小的範圍為 -1 到 1）
    "times": "M-1,-0.7L-0.7,-1L0,-0.3L0.7,-1L1,-0.7L0.3,0L1,0.7L0.7,1L0,0.3L-0.7,1L-1,0.7L-0.3,0Z",
    "asterisk": "M0,-1L0.26,-0.36L0.95,-0.31L0.42,0.14L0.59,0.81L0,0.44L-0.59,0.81L-0.42,0.14L-0.95,-0.31L-0.26,-0.36Z",
    "diamond": "diamond",
    "triangle": "triangle-up",
    "down_triangle": "triangle-down",
    "left_triangle": "triangle-left",
    "right_triangle": "triangle-right",
}


SHAPE_PATHS: dict[ShapeName, str] = {
    "circle": "M1,0A1,1,0,1,1,-1,0A1,1,0,1,1,1,0Z",
    "square": "M-0.9,-0.9H0.9V0.9H-0.9Z",
    "plus": "M-0.3,-1H0.3V-0.3H1V0.3H0.3V1H-0.3V0.3H-1V-0.3H-0.3Z",
    "times": VEGA_SHAPES["times"],
    "asterisk": VEGA_SHAPES["asterisk"],
    "diamond": "M0,-1L0.7,0L0,1L-0.7,0Z",
    "triangle": "M0,-1L1,0.85H-1Z",
    "down_triangle": "M0,1L1,-0.85H-1Z",
    "left_triangle": "M-1,0L0.85,-1V1Z",
    "right_triangle": "M1,0L-0.85,-1V1Z",
}
"""各形狀的 SVG 路徑（範圍 -1 到 1）：檢視器自己畫的形狀圖例用。"""


def shape_members(
    assignments: Sequence[ShapeAssignment], pairs: Iterable[tuple[Any, Any]]
) -> list[tuple[Any, ShapeName, str | None]]:
    """形狀欄位在資料中的各成員：（顯示值、形狀、自訂的圖片）。成員依原值排序（與色彩相同）；指定的形狀
    優先，其餘的成員依預設形狀調色盤的順序。pairs：（顯示值、原值）。"""
    assigned = {value_key(item.value): item for item in assignments}
    labels: list[Any] = []
    raw: dict[Any, Any] = {}
    for label, value in pairs:
        if label not in raw:
            raw[label] = value
            labels.append(label)
    labels.sort(key=lambda label: member_order(raw[label]))
    members: list[tuple[Any, ShapeName, str | None]] = []
    for i, label in enumerate(labels):
        item = assigned.get(value_key(raw[label]))
        if item is None:
            members.append((label, SHAPE_ORDER[i % len(SHAPE_ORDER)], None))
        else:
            members.append((label, item.shape, item.image))
    return members


def shape_scale(
    assignments: Sequence[ShapeAssignment], pairs: Iterable[tuple[Any, Any]]
) -> dict[str, Any] | None:
    """形狀的比例尺：domain 為資料中的顯示值（成員依原值排序，與色彩相同），range 為各值的形狀。
    指定的形狀優先，其餘的成員依預設形狀調色盤的順序。pairs：（顯示值、原值）。"""
    members = shape_members(assignments, pairs)
    if not members:
        return None
    return {
        "domain": [label for label, _, _ in members],
        "range": [VEGA_SHAPES[shape] for _, shape, _ in members],
    }


def _rgb(color: str) -> tuple[int, int, int] | None:
    digits = color.lstrip("#")
    if len(digits) not in (6, 8):
        return None
    try:
        return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
    except ValueError:
        return None


def scale_color(scale: Any, value: Any) -> str | None:
    """比例尺（discrete_scale、continuous_scale 的結果）中一個值的顏色；算不出來時為 None。

    離散：domain 中的顯示值對應 range 的顏色。連續：分段（quantize）取所在的那一段；其餘在 domain 的各點
    之間以 RGB 線性內插（與規格的 interpolate: rgb 相同），範圍之外取兩端的顏色。"""
    if not isinstance(scale, dict):
        return None
    domain, colors = scale.get("domain"), scale.get("range")
    if not isinstance(domain, list) or not isinstance(colors, list) or not colors:
        return None
    if scale.get("type") == "quantize":
        if not isinstance(value, int | float) or isinstance(value, bool) or len(domain) != 2:
            return None
        start, end = float(domain[0]), float(domain[1])
        if end <= start:
            return str(colors[0])
        step = int((float(value) - start) / (end - start) * len(colors))
        return str(colors[min(max(step, 0), len(colors) - 1)])
    if "interpolate" not in scale:
        try:
            return str(colors[domain.index(value) % len(colors)])
        except ValueError:
            return None
    if not isinstance(value, int | float) or isinstance(value, bool) or len(domain) != len(colors):
        return None
    number = float(value)
    if number <= domain[0]:
        return str(colors[0])
    if number >= domain[-1]:
        return str(colors[-1])
    for i in range(len(domain) - 1):
        low, high = float(domain[i]), float(domain[i + 1])
        if low <= number <= high:
            a, b = _rgb(str(colors[i])), _rgb(str(colors[i + 1]))
            if a is None or b is None:
                return None
            t = (number - low) / (high - low) if high > low else 0.0
            mixed = [round(a[k] + (b[k] - a[k]) * t) for k in range(3)]
            return "#" + "".join(f"{c:02x}" for c in mixed)
    return None


def member_order(value: Any, members_first: bool = False) -> tuple[int, Any, str]:
    """成員的排序（與 Tableau 的預設相同）：數字（含布林）由小到大、文字不分大小寫的自然排序（字串中的
    數字當成數值，見 tmig.natural），Null 在最後；集合（members_first）的 True（內）在前。"""
    if value is None:
        return (3, 0, "")
    if members_first and isinstance(value, bool):
        return (0, 0 if value else 1, "")
    if isinstance(value, bool | int | float):
        return (0, float(value), "")
    if isinstance(value, str):
        return (1, natural_key(value.casefold()), value)
    return (2, str(value), "")


def continuous_scale(setting: ColorPalette | None, numbers: list[float]) -> dict[str, Any]:
    """連續色彩的比例尺（Vega-Lite 的 scale）。numbers：資料中的值（決定預設的範圍與自動色盤）。"""
    low = min(numbers) if numbers else None
    high = max(numbers) if numbers else None
    colors, diverging = _palette_colors(setting, low, high)
    if setting is not None and setting.reverse:
        colors = colors[::-1]
    start = setting.start if setting is not None and setting.start is not None else low
    end = setting.end if setting is not None and setting.end is not None else high
    if start is None or end is None:
        return {"range": [colors[0], colors[-1]], "interpolate": "rgb"}
    if start > end:
        start, end = end, start
    steps = setting.steps if setting is not None else None
    if diverging:
        center = setting.center if setting is not None and setting.center is not None else 0.0
        explicit = setting is not None and setting.start is not None and setting.end is not None
        full = setting is not None and (setting.full_range or explicit)
        if not (full and start < center < end):
            half = max(abs(start - center), abs(end - center)) or 1.0
            start, end = center - half, center + half
        else:
            if steps is None:
                return _piecewise(colors, start, end, center)
    if end == start:
        end = start + 1.0
    if steps is not None:
        return {"type": "quantize", "domain": [start, end], "range": sample(colors, steps)}
    return _piecewise(colors, start, end, None)


def _palette_colors(
    setting: ColorPalette | None, low: float | None, high: float | None
) -> tuple[list[str], bool]:
    """色盤的顏色（由小到大）與是否為發散型；沒有設定或認不得的色盤依 Tableau 的自動色盤。"""
    if setting is not None and setting.kind == "continuous":
        if setting.colors:
            return list(setting.colors), setting.diverging
        chosen = palette(setting.palette)
        if chosen is not None and chosen.kind != "regular":
            return list(chosen.colors), chosen.kind == "diverging"
    mixed = low is not None and high is not None and low < 0 < high
    name = AUTOMATIC_DIVERGING if mixed else AUTOMATIC_SEQUENTIAL
    return list(PALETTES[name].colors), mixed


def _piecewise(colors: list[str], start: float, end: float, center: float | None) -> dict[str, Any]:
    """色盤的每個顏色對應範圍內等距的值；有中心時，前半段分布在起點到中心、後半段在中心到終點。"""
    count = len(colors)
    positions = [i / (count - 1) for i in range(count)]
    if center is None:
        domain = [start + (end - start) * t for t in positions]
    else:
        domain = [
            start + (center - start) * (t / 0.5)
            if t <= 0.5
            else center + (end - center) * ((t - 0.5) / 0.5)
            for t in positions
        ]
    domain = [_round(v) for v in domain]
    if any(b <= a for a, b in itertools.pairwise(domain)):
        # 範圍太小（四捨五入後重複）：只用兩端
        return {
            "domain": [start, end],
            "range": [colors[0], colors[-1]],
            "interpolate": "rgb",
            "clamp": True,
        }
    return {"domain": domain, "range": colors, "interpolate": "rgb", "clamp": True}


def _round(value: float) -> float:
    """去掉浮點誤差（例如 0.30000000000000004），保留 12 位有效數字。"""
    if value == 0 or not math.isfinite(value):
        return value
    digits = max(0, 12 - math.floor(math.log10(abs(value))))
    return round(value, digits)


__all__ = [
    "continuous_scale",
    "discrete_scale",
    "scale_color",
    "shape_members",
    "shape_scale",
    "value_key",
]
