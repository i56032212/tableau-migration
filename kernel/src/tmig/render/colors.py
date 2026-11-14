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
        labels.sort(key=lambda label: _member_order(raw[label], members_first))
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


def shape_scale(
    assignments: Sequence[ShapeAssignment], pairs: Iterable[tuple[Any, Any]]
) -> dict[str, Any] | None:
    """形狀的比例尺：domain 為資料中的顯示值（成員依原值排序，與色彩相同），range 為各值的形狀。
    指定的形狀優先，其餘的成員依預設形狀調色盤的順序。pairs：（顯示值、原值）。"""
    assigned: dict[tuple[str, Any], ShapeName] = {
        value_key(item.value): item.shape for item in assignments
    }
    labels: list[Any] = []
    raw: dict[Any, Any] = {}
    for label, value in pairs:
        if label not in raw:
            raw[label] = value
            labels.append(label)
    if not labels:
        return None
    labels.sort(key=lambda label: _member_order(raw[label]))
    shapes: list[ShapeName] = [
        assigned.get(value_key(raw[label]), SHAPE_ORDER[i % len(SHAPE_ORDER)])
        for i, label in enumerate(labels)
    ]
    return {"domain": labels, "range": [VEGA_SHAPES[s] for s in shapes]}


def _member_order(value: Any, members_first: bool = False) -> tuple[int, Any, str]:
    """成員的排序（與 Tableau 的預設相同）：數字（含布林）由小到大、文字不分大小寫，Null 在最後；
    集合（members_first）的 True（內）在前。"""
    if value is None:
        return (3, 0, "")
    if members_first and isinstance(value, bool):
        return (0, 0 if value else 1, "")
    if isinstance(value, bool | int | float):
        return (0, float(value), "")
    if isinstance(value, str):
        return (1, value.casefold(), value)
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


__all__ = ["continuous_scale", "discrete_scale", "value_key"]
