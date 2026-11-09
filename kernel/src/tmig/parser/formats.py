"""工作表與 dashboard 區塊的格式：座標軸（顯示、標題、範圍、反轉）、格線與座標軸線、背景、
區塊的框線與內外距。

.twb 中的寫法（依公開報表歸納）：

- 工作表的 <table><style>：
  - <style-rule element='axis'>：format attr='display'（value='false' 隱藏座標軸或標題）、attr='title'
    （自訂標題，value 為文字），以 field 與 scope（rows／cols）指定欄位；
    <encoding attr='space'>：range-type='fixed'（min、max）、'fixedmin'、'fixedmax'，reverse='true'，
    domain-expand='false'（自動範圍不必包含 0），synchronized='true'（雙軸同步）；日期寫成 #2024-12-30#
  - 沒有 field 的 axis 格式：line-visibility='off'、stroke-size='0'（不畫座標軸線）、tick-color 透明
    （不畫刻度），scope 為 rows、cols 或省略（兩者）
  - <style-rule element='gridline'>：line-visibility='off'、stroke-size='0'（不畫格線）
  - <style-rule element='table'>：format attr='background-color'（工作表的背景，可以透明）
  - <style-rule element='label'> 加上 field：format attr='display'（value='false' 不顯示離散欄位的標題）、
    attr='color'（標題成員或座標軸刻度文字的顏色）
  - <style-rule element='worksheet'>：format attr='display-field-labels'（value='false' 不顯示 scope 那一
    方向的欄位標籤）、attr='color'（工作表文字的預設顏色）
  - <style-rule element='cell'>：format attr='color'（文字表儲存格的文字）
  - 工作表沒有寫文字顏色時依活頁簿的 <style>（element='worksheet'，其次 'all'）
- dashboard 的 <style>：<style-rule element='table'> 的 background-color（dashboard 的背景）
- dashboard 區塊的 <zone-style>：background-color、border-style／border-width／border-color、
  margin（外距，可依 margin-top 等各邊覆寫）、padding（內距）；沒有寫的為 0
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from lxml import etree

from tmig.ir.models import Axis, FieldUse, Scalar, ShelfStyle, ZoneStyle
from tmig.parser.colors import hex_color
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import child, children
from tmig.planner.state import use_key

DATE = re.compile(r"^#(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}:\d{2}))?#$")
SHELVES = {"rows": "rows", "cols": "columns"}
BORDERS = ("none", "solid", "dashed", "dotted")
SIDES = ("top", "right", "bottom", "left")


@dataclass
class SheetFormats:
    """工作表的座標軸、線條、背景與文字顏色（Worksheet 中同名的欄位）。"""

    axes: list[Axis] = field(default_factory=list)
    row_style: ShelfStyle = field(default_factory=ShelfStyle)
    column_style: ShelfStyle = field(default_factory=ShelfStyle)
    background: str | None = None
    text_color: str | None = None
    cell_color: str | None = None


def worksheet_formats(
    style: etree._Element | None,
    resolve: Callable[[str], FieldUse | str],
    rows: list[FieldUse],
    cols: list[FieldUse],
    workbook_style: etree._Element | None = None,
) -> SheetFormats:
    """工作表的格式。座標軸只保留欄列架上的欄位（設定可能是移除欄位前留下的）。"""
    axes: dict[tuple[str, str], Axis] = {}
    shelves = {"rows": ShelfStyle(), "columns": ShelfStyle()}
    result = SheetFormats(text_color=default_text_color(workbook_style))

    def targets(ref: str | None, scope: str | None) -> list[Axis]:
        """欄位在 scope（rows／cols；省略時為欄位所在的架）上的座標軸設定。"""
        parts = qualified_parts(ref or "")
        if ref is None or not parts or parts[0] == "Parameters":
            return []
        if scope is not None and scope not in SHELVES:
            return []
        use = resolve(ref)
        if isinstance(use, str):
            return []
        found: list[Axis] = []
        for shelf, uses in (("rows", rows), ("columns", cols)):
            if use not in uses or (scope is not None and SHELVES[scope] != shelf):
                continue
            key = (use_key(use), shelf)
            if key not in axes:
                axes[key] = Axis(use=use, shelf=shelf)  # type: ignore[arg-type]
            found.append(axes[key])
        return found

    def turn(scope: str | None, name: str, on: bool | None) -> None:
        if on is None:
            return
        for shelf in [SHELVES[scope]] if scope in SHELVES else list(shelves):
            shelves[shelf] = shelves[shelf].model_copy(update={name: on})

    def lines(element: str | None, item: etree._Element) -> None:
        """格線、座標軸線與刻度（沒有 field 的格式）；工作表的設定覆寫活頁簿的。"""
        name, value, scope = item.get("attr"), item.get("value"), item.get("scope")
        if element == "gridline":
            turn(scope, "grid", _line(name, value))
        elif element == "axis" and name == "tick-color":
            turn(scope, "ticks", not _transparent(value) if hex_color(value) else None)
        elif element == "axis":
            turn(scope, "axis", _line(name, value))

    # 活頁簿的格式（Format > Workbook）是每張工作表的預設
    for rule in children(workbook_style, "style-rule"):
        for item in children(rule, "format"):
            if item.get("field") is None:
                lines(rule.get("element"), item)

    for rule in children(style, "style-rule"):
        element = rule.get("element")
        for item in children(rule):
            tag = item.tag if isinstance(item.tag, str) else ""
            name, value, scope = item.get("attr"), item.get("value"), item.get("scope")
            ref = item.get("field")
            if tag == "format" and name == "background-color" and element == "table":
                result.background = hex_color(value) or result.background
            elif tag == "format" and name == "color" and item.get("data-class") is None:
                color = hex_color(value)
                if color is None:
                    continue
                if element == "worksheet" and ref is None:
                    result.text_color = color
                elif element == "cell" and ref is None and scope is None:
                    result.cell_color = color
                elif element == "label" and ref is not None:
                    for target in targets(ref, scope):
                        target.label_color = color
            elif tag == "format" and element == "label" and name == "display" and ref is not None:
                # 離散欄位的「顯示標題」
                for target in targets(ref, scope):
                    target.show = value != "false"
            elif tag == "format" and element == "worksheet" and name == "display-field-labels":
                turn(scope, "field_labels", {"false": False, "true": True}.get(value or ""))
            elif tag == "format" and element in ("gridline", "axis") and ref is None:
                lines(element, item)
            elif element == "axis" and tag == "format" and name in ("display", "title"):
                for target in targets(ref, scope or ""):
                    if name == "display":
                        target.show = value != "false"
                    else:
                        target.title = value or ""
            elif element == "axis" and tag == "encoding" and name == "space":
                for target in targets(ref, scope or ""):
                    _range(target, item)
    # 只留下有設定的座標軸（例如只寫了雙軸的 fold）
    result.axes = [a for a in axes.values() if a != Axis(use=a.use, shelf=a.shelf)]
    result.row_style, result.column_style = shelves["rows"], shelves["columns"]
    return result


def default_text_color(workbook_style: etree._Element | None) -> str | None:
    """活頁簿的預設文字顏色：<style> 中 element='worksheet' 的 color，其次 'all'。"""
    found: dict[str, str] = {}
    for rule in children(workbook_style, "style-rule"):
        element = rule.get("element") or ""
        for item in children(rule, "format"):
            color = hex_color(item.get("value")) if item.get("attr") == "color" else None
            if color is not None and element in ("worksheet", "all") and item.get("field") is None:
                found[element] = color
    return found.get("worksheet") or found.get("all")


def dashboard_background(dashboard: etree._Element) -> str | None:
    """dashboard 的背景顏色（<style> 中 element='table' 的 background-color）。"""
    color: str | None = None
    for rule in children(child(dashboard, "style"), "style-rule"):
        if rule.get("element") != "table":
            continue
        for item in children(rule, "format"):
            if item.get("attr") == "background-color":
                color = hex_color(item.get("value")) or color
    return color


def zone_style(element: etree._Element | None) -> ZoneStyle | None:
    """<zone-style> → ZoneStyle；沒有寫或都是預設（沒有外距、內距、框線與背景）時回傳 None。"""
    formats = {f.get("attr"): f.get("value") for f in children(element, "format")}
    if not formats:
        return None
    border = formats.get("border-style") or "none"
    width = _int(formats.get("border-width")) or 0
    style = ZoneStyle(
        background=hex_color(formats.get("background-color")),
        border=border if border in BORDERS else "solid",  # type: ignore[arg-type]
        border_width=width if border != "none" else 0,
        border_color=hex_color(formats.get("border-color")) if border != "none" else None,
        margin=_sides(formats, "margin"),
        padding=_sides(formats, "padding"),
    )
    if style.border != "none" and style.border_width == 0:
        style = style.model_copy(update={"border": "none", "border_color": None})
    return None if style == ZoneStyle() else style


def _range(target: Axis, item: etree._Element) -> None:
    kind = item.get("range-type")
    if kind in ("fixed", "fixedmin"):
        target.start = _value(item.get("min"))
    if kind in ("fixed", "fixedmax"):
        target.end = _value(item.get("max"))
    if item.get("reverse") == "true":
        target.reverse = True
    if item.get("domain-expand") == "false":
        target.include_zero = False
    if item.get("synchronized") == "true":
        target.synchronized = True


def _value(text: str | None) -> Scalar:
    """範圍的值：數值，或日期（#2024-12-30#）的 ISO 8601 字串。"""
    if text is None:
        return None
    date = DATE.match(text)
    if date is not None:
        return date.group(1) + (f"T{date.group(2)}" if date.group(2) else "")
    try:
        return float(text)
    except ValueError:
        return None


def _line(name: str | None, value: str | None) -> bool | None:
    """線條的格式：不畫（False）、畫（True），或與是否畫出無關（None）。"""
    if name == "line-visibility" and value in ("on", "off"):
        return value == "on"
    if name == "stroke-size" and value is not None:
        size = _int(value)
        return None if size is None else size > 0
    return None


def _transparent(value: str | None) -> bool:
    color = hex_color(value)
    return color is not None and len(color) == 9 and color.endswith("00")


def _sides(formats: dict[str | None, str | None], name: str) -> tuple[int, int, int, int]:
    base = _int(formats.get(name))
    every = base if base is not None else 0
    values = []
    for side in SIDES:
        own = _int(formats.get(f"{name}-{side}"))
        values.append(own if own is not None else every)
    return (values[0], values[1], values[2], values[3])


def _int(value: str | None) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except ValueError:
        return None


__all__ = [
    "SheetFormats",
    "dashboard_background",
    "default_text_color",
    "worksheet_formats",
    "zone_style",
]
