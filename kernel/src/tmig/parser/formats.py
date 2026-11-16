"""工作表與 dashboard 區塊的格式：座標軸（顯示、標題、範圍、反轉）、格線與座標軸線、背景、
區塊的框線與內外距。

.twb 中的寫法（依公開報表歸納）：

- 工作表的 <table><style>：
  - <style-rule element='axis'>：format attr='display'（value='false' 隱藏座標軸或標題）、attr='title'
    （自訂標題，value 為文字），以 field 與 scope（rows／cols）指定欄位；
    <encoding attr='space'>：range-type='fixed'（min、max）、'fixedmin'、'fixedmax'，reverse='true'，
    domain-expand='false'（自動範圍不必包含 0），synchronized='true'（雙軸同步）；日期寫成 #2024-12-30#
  - 沒有 field 的 axis 格式：line-visibility='off'、stroke-size='0'（不畫座標軸線）、tick-color 透明
    （不畫刻度），scope 為 rows、cols 或省略（兩者）；stroke-color、stroke-size、line-pattern-only
    （solid、dashed、dotted）為座標軸線的顏色、線寬與樣式
  - <style-rule element='gridline'>：line-visibility='off'、stroke-size='0'（不畫格線）；stroke-color、
    stroke-size、line-pattern-only 為格線的顏色、線寬與樣式（屬性名稱與零線、趨勢線、參考線相同；
    格線與分隔線的寫法沒有樣本，是推定的）
  - <style-rule element='table'>：format attr='background-color'（工作表的背景，可以透明）
  - <style-rule element='label'> 加上 field：format attr='display'（value='false' 不顯示離散欄位的標題）、
    attr='color'（標題成員或座標軸刻度文字的顏色）、attr='text-align'（標題成員的水平對齊：left、center、
    right，auto 為預設）
  - <style-rule element='worksheet'>：format attr='display-field-labels'（value='false' 不顯示 scope 那一
    方向的欄位標籤）、attr='color'（工作表文字的預設顏色）
  - <style-rule element='cell'>：format attr='color'（文字表儲存格的文字）、attr='text-align'（儲存格文字的
    水平對齊，auto 為預設；Format > 窗格的對齊通常寫在標記卡 <pane> 的 <style> 中）
  - <style-rule element='field-labels'>：format attr='background-color'（欄位標籤的背景）
  - 工作表沒有寫文字顏色時依活頁簿的 <style>（element='worksheet'，其次 'all'）
  - 大小（像素，拖曳邊界或 Format > Cell Size 調整後才寫）：element='cell' 的 height／width（沒有 field 為
    整張表每一列的高度、每一欄的寬度；有 field 為該離散欄位成員的大小），element='header'／'axis' 加上
    field 的 width（列的標題、座標軸寬度）與 height（欄的標題、座標軸高度）
  - 色帶：element='table' 的 band-size（每段的列數，0 為不上色），element='pane'／'header' 的
    band-color，以 scope（rows／cols）區分列與欄
  - 標記卡（<pane>）的 <style-rule element='mark'> 的 size：「大小」滑桿的值（沒有寫時為預設）；
    mark-labels-*：顯示標記標籤（show）、加上標籤的標記（mode：all、range 為最小值／最大值、line-ends 為
    線條兩端、selection、highlight）、range-scope（pane、cell、table、multi-mark 為每條線）與 range-field、
    range-min／max、line-first／last，cull='false' 為允許標籤重疊
  - 字型（Format > Font）：font-family、font-size（pt）、font-weight、font-style，元素為 worksheet（整張
    工作表）、label（標題成員與刻度；有 field 時為這個欄位）、cell（文字表）、field-labels、title、legend、
    legend-title、tooltip；活頁簿 <style> 的設定（all 為預設字型）在前；標記標籤為標記卡的 datalabel
  - 分隔線：element='table-div'（窗格）與 'header-div'（標題）的 line-visibility、stroke-size、
    stroke-color、line-pattern-only（樣式，各自設定），table-div 的 div-level（第幾層標題之間，
    0 為不畫），以 scope 區分列與欄；沒有寫時
    Tableau 預設畫（使用者常關掉：公開報表中最常見的寫法是 line-visibility='off'）
- dashboard 的 <style>：<style-rule element='table'> 的 background-color（dashboard 的背景）
- dashboard 區塊的 <zone-style>：background-color、border-style／border-width／border-color、
  margin（外距，可依 margin-top 等各邊覆寫）、padding（內距）；沒有寫的為 0
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from lxml import etree

from tmig.ir.models import (
    Axis,
    Banding,
    Divider,
    FieldUse,
    FontElement,
    FontStyle,
    LineStyle,
    Scalar,
    ShelfStyle,
    TextStyle,
    TrendLine,
    ZeroLine,
    ZoneStyle,
)
from tmig.parser.colors import hex_color
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import child, children
from tmig.planner.state import use_key

DATE = re.compile(r"^#(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}:\d{2}))?#$")
SHELVES = {"rows": "rows", "cols": "columns"}
SIZES = ("width", "height")
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
    cell_align: Literal["left", "center", "right"] | None = None
    field_label_background: str | None = None
    title_background: str | None = None
    label_valign: Literal["top", "center", "bottom"] | None = None
    fonts: dict[FontElement, FontStyle] = field(default_factory=dict)
    field_fonts: dict[str, FontStyle] = field(default_factory=dict)
    field_colors: dict[str, str] = field(default_factory=dict)


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

    def field_key(ref: str) -> str | None:
        """依欄位設定的格式的欄位鍵（欄位已不在工作表上時為 None）。"""
        parts = qualified_parts(ref)
        if not parts or parts[0] == "Parameters":
            return None
        use = resolve(ref)
        return None if isinstance(use, str) else use_key(use)

    def turn(scope: str | None, name: str, on: bool | None) -> None:
        if on is None:
            return
        for shelf in [SHELVES[scope]] if scope in SHELVES else list(shelves):
            shelves[shelf] = shelves[shelf].model_copy(update={name: on})

    zeros: dict[str, dict[str, str]] = {"rows": {}, "columns": {}}
    grids: dict[str, dict[str, str]] = {"rows": {}, "columns": {}}
    rulers: dict[str, dict[str, str]] = {"rows": {}, "columns": {}}

    def lines(element: str | None, item: etree._Element) -> None:
        """格線、座標軸線、刻度與零線（沒有 field 的格式）；工作表的設定覆寫活頁簿的。"""
        name, value, scope = item.get("attr"), item.get("value"), item.get("scope")
        if element == "zeroline" and name:
            for shelf in [SHELVES[scope]] if scope in SHELVES else list(zeros):
                zeros[shelf][name] = value or ""
        elif element == "gridline":
            turn(scope, "grid", _line(name, value))
            if name in LINE_ATTRS:
                for shelf in [SHELVES[scope]] if scope in SHELVES else list(grids):
                    grids[shelf][name] = value or ""
        elif element == "axis" and name == "tick-color":
            turn(scope, "ticks", not _transparent(value) if hex_color(value) else None)
        elif element == "axis":
            turn(scope, "axis", _line(name, value))
            if name in LINE_ATTRS:
                for shelf in [SHELVES[scope]] if scope in SHELVES else list(rulers):
                    rulers[shelf][name] = value or ""

    def sizes(element: str, name: str, value: str | None, ref: str | None) -> None:
        size = _int(value)
        if size is None or size <= 0:
            return
        if ref is None:
            if element == "cell":
                shelf = "rows" if name == "height" else "columns"
                shelves[shelf] = shelves[shelf].model_copy(update={"cell": size})
            return
        for target in targets(ref, None):
            # 沿著這個方向的大小（列的高度、欄的寬度）是成員的大小，另一個方向是標題或座標軸的大小
            along = (target.shelf == "rows") == (name == "height")
            if element == "cell" and along:
                target.cell = size
            elif element in ("header", "axis") and not along:
                target.size = size

    bands: dict[str, dict[str, str]] = {"rows": {}, "columns": {}}
    dividers: dict[str, dict[str, str]] = {"rows": {}, "columns": {}}

    def fonts(element: str | None, item: etree._Element) -> bool:
        """沒有 field 的字型格式：活頁簿的在前、工作表的覆寫（all 是活頁簿的預設字型）。"""
        name = item.get("attr") or ""
        target = FONT_ELEMENTS.get(element or "")
        if target is None or name not in FONT_ATTRS or item.get("field") is not None:
            return False
        result.fonts[target] = font_attr(result.fonts.get(target), name, item.get("value"))
        return True

    # 活頁簿的格式（Format > Workbook）是每張工作表的預設
    for rule in children(workbook_style, "style-rule"):
        for item in children(rule, "format"):
            if item.get("field") is None:
                lines(rule.get("element"), item)
                fonts(rule.get("element"), item)

    for rule in children(style, "style-rule"):
        element = rule.get("element")
        for item in children(rule):
            tag = item.tag if isinstance(item.tag, str) else ""
            name, value, scope = item.get("attr"), item.get("value"), item.get("scope")
            ref = item.get("field")
            if tag == "format" and name in FONT_ATTRS and item.get("data-class") is None:
                if fonts(element, item):
                    continue
                if element == "label" and ref is not None:
                    for target in targets(ref, scope):
                        target.font = font_attr(target.font, name or "", value)
                elif element == "cell" and ref is not None and (key := field_key(ref)):
                    # 欄位在標記上（文字表、標籤）的字型
                    result.field_fonts[key] = font_attr(
                        result.field_fonts.get(key), name or "", value
                    )
            elif tag == "format" and name == "background-color" and element == "table":
                result.background = hex_color(value) or result.background
            elif tag == "format" and name == "background-color" and element == "field-labels":
                result.field_label_background = _opaque(value)
            elif tag == "format" and name == "background-color" and element == "title":
                result.title_background = _opaque(value)
            elif tag == "format" and element == "cell" and name == "text-align" and ref is None:
                if item.get("data-class") is None and scope is None:
                    result.cell_align = _align(value)
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
                elif element == "cell" and ref is not None and (key := field_key(ref)):
                    result.field_colors[key] = color
            elif tag == "format" and element == "label" and name == "display" and ref is not None:
                # 離散欄位的「顯示標題」
                for target in targets(ref, scope):
                    target.show = value != "false"
            elif (
                tag == "format" and element == "label" and name == "text-align" and ref is not None
            ):
                for target in targets(ref, scope):
                    target.align = _align(value)
            elif tag == "format" and element == "label" and name == "vertical-align":
                valign = _valign(value)
                if ref is None:
                    result.label_valign = valign
                for target in targets(ref, scope):
                    target.valign = valign
            elif tag == "format" and element == "label" and name == "text-orientation":
                try:
                    angle: int | None = int(value or "")
                except ValueError:
                    angle = None
                for target in targets(ref, scope):
                    target.angle = angle if angle not in (None, 0) else None
            elif tag == "format" and element == "worksheet" and name == "display-field-labels":
                turn(scope, "field_labels", {"false": False, "true": True}.get(value or ""))
            elif tag == "format" and element in ("gridline", "axis", "zeroline") and ref is None:
                lines(element, item)
            elif tag == "format" and element in ("cell", "header", "axis") and name in SIZES:
                sizes(element, name or "", value, ref)
            elif tag == "format" and scope in SHELVES and _banding_format(element, name, item):
                bands[SHELVES[scope]][f"{element}:{name}"] = value or ""
            elif tag == "format" and scope in SHELVES and element in ("table-div", "header-div"):
                if item.get("data-class") is None:
                    dividers[SHELVES[scope]][f"{element}:{name}"] = value or ""
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
    for shelf in shelves:
        pane, header = _dividers(dividers[shelf])
        shelves[shelf] = shelves[shelf].model_copy(
            update={
                "banding": _banding(bands[shelf]),
                "divider": pane,
                "header_divider": header,
                "zero_line": _zero_line(zeros[shelf]),
                "grid_line": _line_style(grids[shelf]),
                "axis_line": _line_style(rulers[shelf]),
            }
        )
    result.row_style, result.column_style = shelves["rows"], shelves["columns"]
    return result


def _banding_format(element: str | None, name: str | None, item: etree._Element) -> bool:
    """色帶的格式：表的 band-size，窗格與標題的 band-color（總計等 data-class 的不算）。"""
    if item.get("data-class") is not None:
        return False
    return (element == "table" and name in ("band-size", "band-level")) or (
        element in ("pane", "header") and name == "band-color"
    )


def _banding(raw: dict[str, str]) -> Banding | None:
    """色帶：至少窗格或標題有（不透明的）顏色，且每段的列數不是 0。"""
    size = _int(raw.get("table:band-size"))
    pane = _opaque(raw.get("pane:band-color"))
    header = _opaque(raw.get("header:band-color"))
    if size == 0 or (pane is None and header is None):
        return None
    level = _int(raw.get("table:band-level"))
    return Banding(pane=pane, header=header, size=size or 1, level=level if level else None)


def _zero_line(raw: dict[str, str]) -> ZeroLine | None:
    """零線：沒有寫時與 Tableau 相同都畫；line-visibility='off'、stroke-size='0' 或透明時不畫。"""
    width = _int(raw.get("stroke-size"))
    color = hex_color(raw.get("stroke-color"))
    if raw.get("line-visibility") == "off" or (width is not None and width <= 0):
        return None
    if color is not None and _transparent(color):
        return None
    pattern = raw.get("line-pattern-only")
    return ZeroLine(
        color=color or ZeroLine().color,
        width=width or 1,
        pattern="solid" if pattern == "solid" else "dashed" if pattern == "dashed" else "dotted",
    )


LINE_ATTRS = ("stroke-color", "stroke-size", "line-pattern-only")
"""線條格式中決定樣子的屬性：顏色、線寬（像素）與樣式（solid、dashed、dotted）。"""


def _pattern(value: str | None) -> Literal["solid", "dashed", "dotted"]:
    """線的樣式（line-pattern-only）：dashed、dotted，其餘（含沒有寫）為實線。"""
    return "dashed" if value == "dashed" else "dotted" if value == "dotted" else "solid"


def _line_style(raw: dict[str, str]) -> LineStyle | None:
    """格線或座標軸線的樣式：顏色、線寬與樣式都沒有寫（或與預設相同）時為 None；透明的顏色與寬度 0
    由是否畫出（grid、axis）處理，這裡不算樣式。"""
    color = hex_color(raw.get("stroke-color"))
    if color is not None and _transparent(color):
        color = None
    width = _int(raw.get("stroke-size"))
    style = LineStyle(
        color=color,
        width=width if width is not None and width >= 1 else 1,
        pattern=_pattern(raw.get("line-pattern-only")),
    )
    return None if style == LineStyle() else style


def _valign(value: str | None) -> Literal["top", "center", "bottom"] | None:
    if value == "top" or value == "center" or value == "bottom":
        return value
    return None


def _dividers(raw: dict[str, str]) -> tuple[Divider | None, Divider | None]:
    """窗格（table-div）與標題（header-div）的分隔線。沒有寫時與 Tableau 相同都畫（淺灰、1 像素、
    最內層以外的每一層之間）；line-visibility='off'、stroke-size='0' 或透明時不畫，div-level='0' 時
    兩者都不畫。標題的線沒有寫顏色時與窗格相同，層數依窗格的 div-level（兩者共用）。"""
    level = _int(raw.get("table-div:div-level"))
    if level is not None and level <= 0:
        return None, None

    def line(element: str, inherit: str | None) -> Divider | None:
        width = _int(raw.get(f"{element}:stroke-size"))
        color = hex_color(raw.get(f"{element}:stroke-color")) or inherit
        if raw.get(f"{element}:line-visibility") == "off" or (width is not None and width <= 0):
            return None
        if color is not None and _transparent(color):
            return None
        return Divider(
            color=color or Divider().color,
            width=width or 1,
            level=level,
            pattern=_pattern(raw.get(f"{element}:line-pattern-only")),
        )

    pane = line("table-div", None)
    return pane, line("header-div", hex_color(raw.get("table-div:stroke-color")))


FONT_ELEMENTS: dict[str, FontElement] = {
    "all": "worksheet",
    "worksheet": "worksheet",
    "label": "label",
    "cell": "cell",
    "field-labels": "field-labels",
    "title": "title",
    "legend": "legend",
    "legend-title": "legend-title",
    "tooltip": "tooltip",
}
"""<style-rule> 的 element → 字型的元素（活頁簿的 all 是整份報表的預設字型）。"""
FONT_ATTRS = ("font-family", "font-size", "font-weight", "font-style")


def font_attr(font: FontStyle | None, name: str, value: str | None) -> FontStyle:
    """在字型設定上套用一個格式（font-family、font-size、font-weight、font-style）。"""
    current = font or FontStyle()
    if name == "font-family" and value:
        return current.model_copy(update={"family": value})
    if name == "font-size":
        try:
            size = float(value or "")
        except ValueError:
            return current
        return current.model_copy(update={"size": size}) if 0 < size < 1000 else current
    if name == "font-weight" and value in ("bold", "normal"):
        return current.model_copy(update={"bold": value == "bold"})
    if name == "font-style" and value in ("italic", "normal"):
        return current.model_copy(update={"italic": value == "italic"})
    return current


TREND_FITS: dict[str, Literal["linear", "log", "exp", "power", "polynomial"]] = {
    "linear": "linear",
    "log": "log",
    "logarithmic": "log",
    "exp": "exp",
    "exponential": "exp",
    "power": "power",
    "polynomial": "polynomial",
}


def pane_trend_line(pane: etree._Element) -> tuple[TrendLine | None, str | None]:
    """標記卡（<pane>）的趨勢線：<trendline enabled fit degree exclude-color exclude-intercept
    enable-confidence-bands enable-tooltips> 與 <style-rule element='trendline'> 的線條格式。回傳趨勢線與
    不支援的原因（沒有時為 None）；不分組的因子（<excluded-factors>）由呼叫端解析。"""
    element = child(pane, "trendline")
    if element is None or element.get("enabled") == "false":
        return None, None
    fit = TREND_FITS.get(element.get("fit") or "linear")
    if fit is None:
        return None, f"趨勢線的模型 {element.get('fit')} 尚不支援"
    raw: dict[str, str] = {}
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") == "trendline":
            for item in children(rule, "format"):
                raw[item.get("attr") or ""] = item.get("value") or ""
    if raw.get("line-visibility") == "off":
        return None, None
    color = hex_color(raw.get("stroke-color"))
    width = _int(raw.get("stroke-size"))
    pattern = raw.get("line-pattern-only")
    degree = _int(element.get("degree"))
    trend = TrendLine(
        fit=fit,
        degree=min(max(degree, 2), 8) if degree else 3,
        by_color=element.get("exclude-color") != "true",
        intercept=element.get("exclude-intercept") != "true",
        confidence_bands=element.get("enable-confidence-bands") == "true",
        tooltips=element.get("enable-tooltips") != "false",
        color=color,
        width=width or 1,
        pattern="dashed" if pattern == "dashed" else "dotted" if pattern == "dotted" else "solid",
    )
    return trend, None


def reference_styles(styles: list[etree._Element | None]) -> dict[str, dict[str, str]]:
    """參考線的格式（<style-rule element='refline'|'refband'>，在工作表或標記卡的 <style>）：參考線 id →
    {「refline:屬性」或「refband:屬性」: 值}；沒有 id 的格式套用到所有參考線（id 為 *）。"""
    found: dict[str, dict[str, str]] = {}
    for style in styles:
        for rule in children(style, "style-rule"):
            element = rule.get("element")
            if element not in ("refline", "refband"):
                continue
            for item in children(rule, "format"):
                if item.get("scope"):
                    continue  # 依架設定的格式（例如 rows 的線條）只影響座標軸的線
                key = item.get("id") or "*"
                found.setdefault(key, {})[f"{element}:{item.get('attr')}"] = item.get("value") or ""
    return found


def pane_label_match(pane: etree._Element) -> bool:
    """標記標籤的顏色是否與標記相符（標記卡的 datalabel 的 color-mode='match'）。"""
    return any(
        item.get("attr") == "color-mode" and item.get("value") == "match"
        for rule in children(child(pane, "style"), "style-rule")
        if rule.get("element") == "datalabel"
        for item in children(rule, "format")
    )


def pane_label_font(pane: etree._Element) -> FontStyle | None:
    """標記卡（<pane>）的標記標籤字型：<style-rule element='datalabel'> 的字型格式。"""
    font: FontStyle | None = None
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "datalabel":
            continue
        for item in children(rule, "format"):
            name = item.get("attr") or ""
            if name in FONT_ATTRS and item.get("field") is None:
                font = font_attr(font, name, item.get("value"))
    return font


def pane_mark_size(pane: etree._Element) -> float | None:
    """標記卡（<pane>）的「大小」滑桿：<style-rule element='mark'> 的 size；沒有設定時為 None。"""
    found: float | None = None
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "mark":
            continue
        for item in children(rule, "format"):
            if item.get("attr") == "size" and item.get("field") is None:
                try:
                    value = float(item.get("value") or "")
                except ValueError:
                    continue
                found = value if value > 0 and math.isfinite(value) else found
    return found


def pane_line_type(pane: etree._Element) -> Literal["linear", "step", "jump"]:
    """標記卡（<pane>）的線條類型：<style-rule element='mark'> 的 line-interpolation（step、jump；
    其餘為直線）。"""
    found: Literal["linear", "step", "jump"] = "linear"
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "mark":
            continue
        for item in children(rule, "format"):
            if item.get("attr") == "line-interpolation" and item.get("field") is None:
                value = (item.get("value") or "").lower()
                found = "step" if value == "step" else "jump" if value == "jump" else "linear"
    return found


RUN_ALIGN: dict[str, Literal["left", "center", "right"]] = {
    "0": "left",
    "1": "center",
    "2": "right",
}
"""格式化文字中 run 的 fontalignment → 段落的對齊。"""


def run_style(run: etree._Element) -> TextStyle | None:
    """格式化文字中一段（<run>）的格式：bold、italic、underline、fontsize（pt）、fontcolor、fontname、
    fontalignment；都沒有寫時為 None。"""
    size = run.get("fontsize")
    style = TextStyle(
        bold=True if run.get("bold") == "true" else None,
        italic=True if run.get("italic") == "true" else None,
        underline=True if run.get("underline") == "true" else None,
        size=float(size)
        if size and re.fullmatch(r"\d+(\.\d+)?", size) and float(size) > 0
        else None,
        color=run.get("fontcolor")
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", run.get("fontcolor") or "")
        else None,
        font=run.get("fontname") or None,
        align=RUN_ALIGN.get(run.get("fontalignment") or ""),
    )
    return style if style.model_dump(exclude_none=True) else None


LABEL_MODES: dict[str, str] = {
    "all": "all",
    "range": "min_max",
    "line-ends": "line_ends",
    "selection": "selected",
    "highlight": "highlighted",
}
"""mark-labels-mode → 加上標籤的標記（MarkLabels.marks）。"""
LABEL_SCOPES: dict[str, str] = {
    "pane": "pane",
    "cell": "cell",
    "table": "table",
    "multi-mark": "line",
}
"""mark-labels-range-scope → 最小值／最大值的範圍（multi-mark 是每條線）。"""


@dataclass
class PaneLabels:
    """標記卡（<pane>）的標籤設定：MarkLabels 的欄位（field 另以 field_ref 解析），與認不得的設定。"""

    settings: dict[str, object]
    field_ref: str | None
    unknown: list[str]


def pane_labels(pane: etree._Element) -> PaneLabels | None:
    """標記卡（<pane>）的 <style-rule element='mark'> 中的 mark-labels-*：show（顯示標記標籤）、mode（all、
    range 為最小值／最大值、line-ends 為線條兩端、selection、highlight）、range-scope、range-field、
    range-min／max、line-first／last、cull（false 為允許標籤重疊）。都沒有寫時為 None。"""
    raw: dict[str, str] = {}
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "mark":
            continue
        for item in children(rule, "format"):
            name = item.get("attr") or ""
            if name.startswith("mark-labels-") and item.get("field") is None:
                raw[name.removeprefix("mark-labels-")] = item.get("value") or ""
    if not raw:
        return None
    settings: dict[str, object] = {}
    unknown: list[str] = []
    if "show" in raw:
        settings["show"] = raw["show"] != "false"
    mode = raw.get("mode")
    if mode is not None:
        if mode in LABEL_MODES:
            settings["marks"] = LABEL_MODES[mode]
        else:
            unknown.append(f"mark-labels-mode={mode}")
    scope = raw.get("range-scope")
    if scope is not None and settings.get("marks") == "min_max":
        if scope in LABEL_SCOPES:
            settings["scope"] = LABEL_SCOPES[scope]
        else:
            unknown.append(f"mark-labels-range-scope={scope}")
    for attr, key in (
        ("range-min", "minimum"),
        ("range-max", "maximum"),
        ("line-first", "first"),
        ("line-last", "last"),
    ):
        if attr in raw:
            settings[key] = raw[attr] != "false"
    if raw.get("cull") == "false":
        settings["overlap"] = True
    ref = raw.get("range-field") or None
    return PaneLabels(settings, ref if settings.get("marks") == "min_max" else None, unknown)


def pane_cell_align(pane: etree._Element) -> Literal["left", "center", "right"] | None:
    """標記卡（<pane>）的 <style-rule element='cell'> 的 text-align：Format > 窗格的對齊。"""
    found: Literal["left", "center", "right"] | None = None
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "cell":
            continue
        for item in children(rule, "format"):
            if item.get("attr") == "text-align" and item.get("field") is None:
                found = _align(item.get("value"))
    return found


def pane_cell_valign(pane: etree._Element) -> Literal["top", "center", "bottom"] | None:
    """標記卡（<pane>）的 <style-rule element='cell'> 的 vertical-align：標籤的垂直對齊。"""
    found: Literal["top", "center", "bottom"] | None = None
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "cell":
            continue
        for item in children(rule, "format"):
            if item.get("attr") == "vertical-align" and item.get("field") is None:
                value = item.get("value")
                found = (
                    "top"
                    if value == "top"
                    else "bottom"
                    if value == "bottom"
                    else "center"
                    if value in ("center", "middle")
                    else None
                )
    return found


def _align(value: str | None) -> Literal["left", "center", "right"] | None:
    """水平對齊：left、center、right；auto（預設）與其他值為 None。"""
    if value == "left" or value == "center" or value == "right":
        return value
    return None


def _opaque(value: str | None) -> str | None:
    color = hex_color(value)
    return None if color is None or _transparent(color) else color


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
    scale = item.get("scale")
    if scale == "log" or scale == "symlog":
        target.scale = scale
    # 主要刻度：不顯示，或固定的間距與起點（數值；日期的間距帶單位，依自動）
    if item.get("major-show") == "false":
        target.ticks = False
    spacing = _number(item.get("major-spacing"))
    if spacing is not None and spacing > 0 and item.get("major-units") is None:
        target.tick_spacing = spacing
        target.tick_origin = _number(item.get("major-origin"))


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


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
