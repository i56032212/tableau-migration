"""色彩：顏色欄位的色彩設定（Tableau 的「編輯色彩」）與標記的顏色、不透明度。

.twb 中的寫法（依公開報表歸納）：

- 離散欄位的顏色多半存在資料源的 <style>（所有工作表共用）::

      <style-rule element='mark'>
        <encoding attr='color' field='[none:Category:nk]' type='palette' palette='tableau10_10_0'>
          <map to='#4e79a7'><bucket>"Furniture"</bucket></map>
          <map to='#e15759'><bucket>%null%</bucket></map>
        </encoding>

  值為字串（"…"）、數值、布林、%null%；Measure Names 的值是量值的欄位實例（"[資料源].[sum:Sales:qk]"）。
- 連續量值的色盤存在工作表的 <style>（field 帶資料源前綴）：type='interpolated' 為內建色盤，
  'custom-interpolated' 為自訂（<color-palette> 列出顏色）；reverse、num-steps（分段）、min／max
  （範圍的起訖）、center（發散的中心）、symmetric='false'（Use Full Color Range）
- 工作表的設定優先於資料源的設定（同一個欄位實例，例如 [sum:Sales:qk]；Table Calculation 的不同實例
  如 [usr:X:nk:1]、[usr:X:nk:2] 各自設定）
- 標記卡的 <pane><style><style-rule element='mark'>：format attr='mark-color'（沒有顏色欄位時的顏色）、
  'mark-transparency'（0–255 的不透明度）、'has-stroke'＋'stroke-color'（框線）
"""

from __future__ import annotations

import re
from collections.abc import Callable

from lxml import etree

from tmig.ir.models import ColorAssignment, ColorPalette, Scalar
from tmig.parser.fields import instance_key
from tmig.parser.twbx import child, children

HEX = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
NUMBER = re.compile(r"^-?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
DATE = re.compile(r"^#(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}:\d{2}))?#$")


def hex_color(value: str | None) -> str | None:
    """#RRGGBB 或 #RRGGBBAA（統一為小寫）；其他寫法回傳 None。"""
    if value is None or not HEX.match(value):
        return None
    return value.lower()


def color_encodings(style: etree._Element | None) -> dict[str, etree._Element]:
    """<style> 中標記的色彩編碼：欄位實例鍵（例如 [none:Category:nk]）→ <encoding>。"""
    found: dict[str, etree._Element] = {}
    for rule in children(style, "style-rule"):
        if rule.get("element") != "mark":
            continue
        for enc in children(rule, "encoding"):
            field = enc.get("field")
            if enc.get("attr") == "color" and field and "\n" not in field:
                found.setdefault(instance_key(field), enc)
    return found


def parse_color(
    enc: etree._Element, measure: Callable[[str], str | None] | None = None
) -> ColorPalette | None:
    """一個色彩編碼 → ColorPalette。measure：顏色欄位是 Measure Names 時，把值（量值的欄位實例）
    轉成量值的欄位鍵；找不到的量值略過。認不得的寫法回傳 None（依 Tableau 的自動色彩）。"""
    kind = enc.get("type")
    if kind == "palette":
        assignments: list[ColorAssignment] = []
        seen: set[tuple[str, Scalar]] = set()
        for item in children(enc, "map"):
            color = hex_color(item.get("to"))
            if color is None:
                continue
            for bucket in children(item, "bucket"):
                parsed = _bucket(bucket.text or "", measure)
                if parsed is None:
                    continue
                (value,) = parsed
                key = (type(value).__name__, value)
                if key in seen:
                    continue
                seen.add(key)
                assignments.append(ColorAssignment(value=value, color=color))
        if not assignments and not enc.get("palette"):
            return None
        return ColorPalette(kind="discrete", palette=enc.get("palette"), assignments=assignments)
    if kind not in ("interpolated", "custom-interpolated"):
        return None
    colors: list[str] = []
    diverging = False
    if kind == "custom-interpolated":
        palette_el = child(enc, "color-palette")
        colors = [c for c in (hex_color(e.text) for e in children(palette_el, "color")) if c]
        if len(colors) < 2:
            return None
        diverging = palette_el is not None and palette_el.get("type") == "ordered-diverging"
    elif not enc.get("palette"):
        return None
    steps = _int(enc.get("num-steps"))
    return ColorPalette(
        kind="continuous",
        palette=enc.get("palette") if kind == "interpolated" else None,
        colors=colors,
        diverging=diverging,
        reverse=enc.get("reverse") == "true",
        steps=steps if steps is not None and steps >= 2 else None,
        start=_float(enc.get("min")),
        end=_float(enc.get("max")),
        center=_float(enc.get("center")),
        full_range=enc.get("symmetric") == "false",
    )


def mark_style(pane: etree._Element) -> tuple[str | None, float | None, str | None]:
    """標記卡的顏色（沒有顏色欄位時）、不透明度（不透明時為 None）與框線顏色（沒有框線時為 None）。"""
    color: str | None = None
    opacity: float | None = None
    stroke: str | None = None
    has_stroke = False
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "mark":
            continue
        for item in children(rule, "format"):
            name, value = item.get("attr"), item.get("value")
            if name == "mark-color":
                color = hex_color(value) or color
            elif name == "mark-transparency":
                alpha = _int(value)
                if alpha is not None and 0 <= alpha < 255:
                    opacity = round(alpha / 255, 4)
            elif name == "has-stroke":
                has_stroke = value == "true"
            elif name == "stroke-color":
                stroke = hex_color(value) or stroke
    return color, opacity, stroke if has_stroke else None


def _bucket(text: str, measure: Callable[[str], str | None] | None) -> tuple[Scalar] | None:
    """色彩對應中的一個值；Tableau 的特殊值（%all%、%many-values%）與找不到的量值回傳 None。"""
    if text == "%null%":
        return (None,)
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        value = text[1:-1].replace('""', '"')
        if measure is not None:
            key = measure(value)
            return (key,) if key is not None else None
        return (value,)
    if measure is not None:
        return None
    if text in ("true", "false"):
        return (text == "true",)
    date = DATE.match(text)
    if date is not None:
        return (date.group(1) + (f"T{date.group(2)}" if date.group(2) else ""),)
    if NUMBER.match(text):
        number = float(text)
        return (int(number) if number.is_integer() and "." not in text else number,)
    return None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


__all__ = ["color_encodings", "hex_color", "mark_style", "parse_color"]
