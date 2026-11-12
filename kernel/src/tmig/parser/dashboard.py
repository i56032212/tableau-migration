"""dashboard：尺寸、區塊樹與座標換算（A5、I1）。

.twb 以 dashboard 尺寸的 100000 分之一記錄區塊座標；換算為像素時 x、w 依寬度，y、h 依高度。
自動與範圍尺寸的 dashboard 以參考尺寸換算，並記下容器的排列方向與固定大小的區塊，
檢視時依視窗大小重排（見 tmig.render.layout）。
按鈕（I2）：導覽到 dashboard 或工作表、顯示或隱藏區塊、匯出；網頁區塊記下網址。
不支援的區塊以 blank 佔位，並記錄在 unsupported（檢視器依 source_ref 顯示「未支援」）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from lxml import etree

from tmig.ir.integrity import iter_zones
from tmig.ir.models import (
    DEVICE_SIZES,
    Button,
    ControlType,
    Dashboard,
    DeviceLayout,
    Size,
    TextRun,
    TextStyle,
    Unsupported,
    Zone,
)
from tmig.parser.context import ParseContext, SheetState
from tmig.parser.fields import instance_key
from tmig.parser.formats import dashboard_background, run_style, zone_style
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import attr, child, children, descendants, local_name, text_of, xpath

UNITS = 100000
REFERENCE_SIZE = (1366, 768)
"""自動或範圍尺寸的 dashboard 先以這個參考尺寸換算座標。"""

FILTER_MODES: dict[str, ControlType] = {
    "checkdropdown": "multi_dropdown",
    "dropdown": "single_dropdown",
    "checklist": "multi_list",
    "radiolist": "single_list",
    "typein": "type_in",
    "type_in": "type_in",
    "pattern": "type_in",
    "typeinlist": "type_in",
}
PARAM_MODES: dict[str, ControlType] = {
    "slider": "single_slider",
    "compact": "single_dropdown",
    "radiolist": "single_list",
    "list": "single_list",
    "typein": "type_in",
    "type_in": "type_in",
}
LEGENDS = {"color": "color", "size": "size", "shape": "shape"}
DASHBOARD_CHILDREN = frozenset(
    {
        "style",
        "size",
        "zones",
        "simple-id",
        "datasources",
        "datasource-dependencies",
        "layout-options",
        "repository-location",
        "devicelayouts",
    }
)
ZONE_OPAQUE = frozenset({"zone-style", "formatted-text", "layout-cache", "format", "button"})
WINDOW_ID = re.compile(r'window-id="\{?([0-9A-Fa-f-]+)\}?"')
TOGGLE_ZONES = re.compile(r"zone-ids=\[([\d,\s]*)\]")
EXPORT_TYPE = re.compile(r'dashboard-button-export-type="(\w+)"')


def _image_path(state: etree._Element | None) -> str | None:
    path = text_of(child(state, "image-path"))
    return path.replace("\\", "/") if path else None


class DashboardParser:
    def __init__(self, ctx: ParseContext, element: etree._Element) -> None:
        self.ctx = ctx
        self.el = element
        self.name = element.get("name", "")
        self.width = 0
        self.height = 0

    def parse(self) -> Dashboard | None:
        ctx, el = self.ctx, self.el
        size_el = child(el, "size")
        sizing = _size(size_el)
        width, height = sizing.width, sizing.height
        if sizing.mode != "fixed":
            label = "自動尺寸" if sizing.mode == "automatic" else "範圍尺寸"
            ctx.ignore(
                size_el if size_el is not None else el,
                f"dashboard「{self.name}」為{label}：區塊座標以 {width}×{height} 換算，檢視時依視窗大小重排"
                "（固定大小的區塊維持像素大小，其餘依比例分配），與 Tableau 的版面重排可能略有差異",
            )
        self.width, self.height = width, height
        for item in children(el):
            tag = local_name(item)
            if tag not in DASHBOARD_CHILDREN:
                ctx.flag(item, f"dashboard 中的未知元素 <{tag}>")
        zones = [self._zone(z) for z in children(child(el, "zones"), "zone")]
        self._check_toggles(zones)
        layouts = self._layouts(child(el, "devicelayouts"), sizing)
        self.width, self.height = width, height
        return Dashboard(
            id=ctx.dashboard_id(self.name),
            name=self.name,
            size=Size(width=width, height=height),
            sizing=sizing.mode,
            min_size=sizing.min_size,
            max_size=sizing.max_size,
            background=dashboard_background(el),
            zones=zones,
            layouts=layouts,
            source_ref=xpath(el),
        )

    def _layouts(self, element: etree._Element | None, default: Sizing) -> list[DeviceLayout]:
        """裝置專屬版面：<devicelayout name='Phone'|'Tablet'|'Desktop'>，各有尺寸（sizing-mode 為
        vscroll 時是符合寬度）與區塊。區塊座標依版面的尺寸換算；沒有寫尺寸時與預設版面相同。"""
        layouts: list[DeviceLayout] = []
        for item in children(element, "devicelayout"):
            name = (item.get("name") or "").lower()
            if name not in DEVICE_SIZES:
                self.ctx.flag(
                    item, f"dashboard「{self.name}」的裝置版面「{item.get('name')}」不支援"
                )
                continue
            if any(layout.device == name for layout in layouts):
                self.ctx.ignore(item, f"dashboard「{self.name}」重複的{name}版面：只用第一個")
                continue
            size_el = child(item, "size")
            mode = size_el.get("sizing-mode") if size_el is not None else None
            device_w, device_h = DEVICE_SIZES[name]
            if size_el is None:
                kind, width, height = "default", default.width, default.height
            elif mode == "vscroll":
                height = (
                    _int(size_el.get("maxheight")) or _int(size_el.get("minheight")) or device_h
                )
                kind, width = "fit_width", device_w
            elif mode == "automatic":
                kind, width, height = "automatic", device_w, device_h
            else:
                sized = _size(size_el)
                if sized.mode == "fixed":
                    kind, width, height = "fixed", sized.width, sized.height
                else:
                    kind, width, height = "automatic", sized.width, sized.height
            self.width, self.height = width, height
            zones = [self._zone(z) for z in children(child(item, "zones"), "zone")]
            self._check_toggles(zones)
            layouts.append(
                DeviceLayout(
                    device=name,  # type: ignore[arg-type]
                    sizing=kind,  # type: ignore[arg-type]
                    size=Size(width=width, height=height),
                    auto_generated=item.get("auto-generated") == "true",
                    zones=zones,
                )
            )
        return layouts

    # ------------------------------------------------------------ 區塊

    def _px(self, element: etree._Element, name: str, total: int) -> int:
        raw = element.get(name)
        try:
            value = int(raw) if raw is not None else 0
        except ValueError:
            value = 0
        return max(0, round(value * total / UNITS))

    def _zone(self, element: etree._Element) -> Zone:
        kind_attr = attr(element, "type-v2") or attr(element, "type")
        name = element.get("name")
        zone_id = f"z{element.get('id', '0')}"
        base: dict = {
            "id": zone_id if zone_id[1:].isdigit() else "z0",
            "x": self._px(element, "x", self.width),
            "y": self._px(element, "y", self.height),
            "w": self._px(element, "w", self.width),
            "h": self._px(element, "h", self.height),
            "source_ref": xpath(element),
        }
        if element.get("is-fixed") == "true" and (element.get("fixed-size") or "").isdigit():
            base["fixed"] = int(element.get("fixed-size", "0"))
        if element.get("hidden-by-user") == "true":
            base["hidden"] = True
        style = zone_style(child(element, "zone-style"))
        if style is not None:
            base["style"] = style
        child_zones = children(element, "zone")
        self._check(element)

        if kind_attr in ("layout-basic", "layout-flow", "layout-free"):
            layout = "tiled"
            if kind_attr == "layout-flow":
                layout = "vertical" if element.get("param") == "vert" else "horizontal"
            return Zone(
                kind="container",
                layout=layout,
                children=[self._zone(z) for z in child_zones],
                **base,
            )
        if child_zones:
            self.ctx.flag(element, "非容器的區塊含有子區塊")
        if kind_attr is None and name:
            if name in self.ctx.blank_sheets:
                return Zone(kind="blank", **base)
            sheet = self._sheet(element, name)
            if sheet is None:
                return Zone(kind="blank", **base)
            show_title = element.get("show-title") != "false"
            # 這個 dashboard 中的顯示大小；沒有 <viewpoint> 時依工作表本身
            fit = self.ctx.dashboard_fits.get(self.name, {}).get(name)
            return Zone(
                kind="worksheet", worksheet=sheet.id, show_title=show_title, fit=fit, **base
            )
        if kind_attr == "title":
            text, content = self._text(element)
            return Zone(kind="title", text=text or self.name, content=content, **base)
        if kind_attr == "text":
            text, content = self._text(element)
            if text is None and not content:
                return Zone(kind="blank", **base)  # 空白的文字物件：Tableau 顯示空的區塊
            return Zone(kind="text", text=text, content=content, **base)
        if kind_attr == "bitmap":
            image = element.get("param")
            if not image:
                # 還沒選圖片的圖片物件：Tableau 顯示空白的區塊
                self.ctx.ignore(element, "圖片區塊沒有指定圖片：以空白區塊呈現")
                return Zone(kind="blank", **base)
            return Zone(kind="image", image=image.replace("\\", "/"), **base)
        if kind_attr == "empty":
            return Zone(kind="blank", **base)
        if kind_attr == "dashboard-object":
            button = child(element, "button")
            if button is None:
                self.ctx.flag(element, "dashboard 物件（不是按鈕）v0 不支援")
                return Zone(kind="blank", **base)
            return self._button_zone(element, button, base)
        if kind_attr == "web":
            url = (element.get("param") or "").strip()
            if not url:
                self.ctx.ignore(element, "沒有指定網址的網頁區塊：以空白區塊呈現")
                return Zone(kind="blank", **base)
            return Zone(kind="web", url=url, **base)
        if kind_attr == "highlighter":
            # 醒目提示控制項：選擇欄位的值，工作表中這個值的標記醒目、其餘淡化（不影響數字）
            sheet = self._sheet(element, name)
            if sheet is None:
                return Zone(kind="blank", **base)
            use = sheet.uses.get(instance_key(element.get("param", "")))
            if use is None:
                self.ctx.flag(
                    element, f"醒目提示控制項的欄位 {element.get('param')} 不在工作表「{name}」上"
                )
                return Zone(kind="blank", **base)
            return Zone(kind="highlighter", worksheet=sheet.id, field=use, **base)
        if kind_attr == "filter":
            return self._filter_zone(element, name, base)
        if kind_attr == "currpage":
            # 頁面控制項：切換工作表頁面架上的頁
            sheet = self._sheet(element, name)
            if sheet is None or name not in self.ctx.paged_sheets:
                self.ctx.flag(element, f"頁面控制項的工作表「{name}」沒有頁面架")
                return Zone(kind="blank", **base)
            return Zone(kind="page", worksheet=sheet.id, **base)
        if kind_attr == "paramctrl":
            return self._param_zone(element, base)
        if kind_attr in LEGENDS:
            sheet = self._sheet(element, name)
            if sheet is None:
                return Zone(kind="blank", **base)
            return Zone(
                kind="legend",
                worksheet=sheet.id,
                legend=LEGENDS[kind_attr],  # type: ignore[arg-type]
                **base,
            )
        self.ctx.flag(element, f"dashboard 區塊類型 {kind_attr or '（未標示）'} v0 不支援")
        return Zone(kind="blank", **base)

    def _button_zone(self, element: etree._Element, button: etree._Element, base: dict) -> Zone:
        """按鈕：導覽（goto-sheet）、顯示或隱藏區塊（toggle-action）、匯出（export-button-action）。"""
        ctx = self.ctx
        states = children(button, "button-visual-state")
        first = states[0] if states else None
        font = child(first, "button-caption-font-style")
        style: dict[str, Any] = {
            "caption": text_of(child(first, "caption")) or None,
            "tooltip": text_of(child(first, "tooltip-text")) or None,
            "image": _image_path(first),
            "color": font.get("fontcolor") if font is not None else None,
            "background": next(
                (
                    f.get("value")
                    for f in children(first, "format")
                    if f.get("attr") == "background-color"
                ),
                None,
            ),
        }
        action = button.get("action") or ""
        toggle = child(button, "toggle-action")
        export = child(button, "export-button-action")
        if action.startswith("tabdoc:goto-sheet"):
            found = WINDOW_ID.search(action)
            window = ctx.windows.get(found.group(1).upper()) if found else None
            if window is None:
                ctx.flag(element, "導覽按鈕的目標視窗找不到")
                return Zone(kind="blank", **base)
            kind, name = window
            if kind == "worksheet" and name in ctx.sheets:
                target = Button(action="navigate", target_worksheet=ctx.sheets[name].id, **style)
                return Zone(kind="button", button=target, **base)
            if kind in ("dashboard", "storyboard"):
                # Story 的視窗類別也是 dashboard：目標是 Story 時在 Story 解析後改成 target_story
                target = Button(action="navigate", target_dashboard=ctx.dashboard_id(name), **style)
                zone = Zone(kind="button", button=target, **base)
                ctx.navigations.append((element, zone))
                return zone
            ctx.flag(element, f"導覽按鈕的目標「{name}」（{kind}）無法移轉")
            return Zone(kind="blank", **base)
        if toggle is not None:
            found = TOGGLE_ZONES.search(text_of(toggle))
            zones = [f"z{n}" for n in re.findall(r"\d+", found.group(1))] if found else []
            if not zones:
                ctx.flag(element, "顯示／隱藏按鈕沒有指定區塊")
                return Zone(kind="blank", **base)
            hidden = _image_path(states[1]) if len(states) > 1 else None
            target = Button(action="toggle", zones=zones, image_hidden=hidden, **style)
            return Zone(kind="button", button=target, **base)
        if export is not None:
            found = EXPORT_TYPE.search(text_of(export))
            kind = found.group(1).lower() if found else ""
            if kind not in ("pdf", "image", "powerpoint"):
                ctx.flag(element, f"匯出按鈕的格式 {kind or '（未指定）'} v0 不支援")
                return Zone(kind="blank", **base)
            target = Button(action="export", export=kind, **style)
            return Zone(kind="button", button=target, **base)
        ctx.flag(element, f"按鈕的動作 {action or '（未指定）'} v0 不支援")
        return Zone(kind="blank", **base)

    def _check_toggles(self, zones: list[Zone]) -> None:
        """顯示／隱藏按鈕的區塊必須在同一個 dashboard 上。找不到的區塊（例如已刪除）在 Tableau 上
        按了也沒有作用：只切換找得到的區塊；一個都找不到時改為空白區塊（記在略過的元素）。"""
        ids = {z.id for z in iter_zones(zones)}
        for zone in iter_zones(zones):
            if zone.button is None or zone.button.action != "toggle":
                continue
            missing = [z for z in zone.button.zones if z not in ids]
            if not missing:
                continue
            self.ctx.ignored.append(
                Unsupported(
                    source_ref=zone.source_ref,
                    reason=f"顯示／隱藏按鈕的區塊 {'、'.join(missing)} 不在此 dashboard 上"
                    "（Tableau 上按了也沒有作用）",
                    element="zone",
                )
            )
            remaining = [z for z in zone.button.zones if z in ids]
            if remaining:
                zone.button = zone.button.model_copy(update={"zones": remaining})
            else:
                zone.kind, zone.button = "blank", None

    def _text(self, element: etree._Element) -> tuple[str | None, list[TextRun]]:
        return formatted_content(self.ctx, element, self.name)

    def _sheet(self, element: etree._Element, name: str | None) -> SheetState | None:
        sheet = self.ctx.sheets.get(name or "")
        if sheet is None:
            self.ctx.flag(element, f"區塊參照的工作表「{name}」無法移轉")
        return sheet

    def _filter_zone(self, element: etree._Element, name: str | None, base: dict) -> Zone:
        sheet = self._sheet(element, name)
        if sheet is None:
            return Zone(kind="blank", **base)
        param = element.get("param", "")
        key = instance_key(param)
        parts = qualified_parts(param)
        if len(parts) == 2 and parts[0] != sheet.ds.name:
            # 其他資料源的欄位：工作表上的跨資料源篩選（篩選主要資料源中對應的欄位）
            mapped = sheet.cross.get(f"[{parts[0]}].{key}")
            if mapped is None:
                self.ctx.flag(
                    element,
                    f"篩選控制項的欄位 {param} 是其他資料源的欄位，工作表「{name}」上沒有對應的"
                    "跨資料源篩選",
                )
                return Zone(kind="blank", **base)
            key = mapped
        if key.startswith("[attr:") and key not in sheet.filter_kinds:
            # ATTR(維度) 的篩選控制項：工作表上沒有這個篩選時，是同一個維度的類別篩選
            dimension = "[none:" + key.removeprefix("[attr:")
            if dimension in sheet.filter_kinds:
                key = dimension
        use = sheet.uses.get(key)
        if use is None:
            self.ctx.flag(element, f"篩選控制項的欄位 {param} 不在工作表「{name}」上")
            return Zone(kind="blank", **base)
        filter_kind = sheet.filter_kinds.get(key)
        if filter_kind is None:
            self.ctx.flag(element, f"篩選控制項的欄位 {param} 在工作表「{name}」上沒有篩選")
            return Zone(kind="blank", **base)
        # Top N 篩選的控制項：選取的成員是 Top N 篩選「一般」頁的選取（排名前先選取），樣式與一般的類別篩選相同
        mode = element.get("mode")
        control: ControlType
        if filter_kind in ("relative_date", "condition"):
            control = filter_kind
        elif filter_kind == "range":
            datatype = sheet.ds.field_by_id(use.field).datatype
            is_date = datatype in ("date", "datetime") and use.date is None
            control = "date_range" if is_date else "range_slider"
        elif mode is None:
            control = "multi_list"
        elif mode == "slider":
            control = "single_slider"
        elif mode in FILTER_MODES:
            control = FILTER_MODES[mode]
        else:
            self.ctx.flag(element, f"篩選控制項的樣式 {mode} v0 不支援")
            return Zone(kind="blank", **base)
        return Zone(kind="filter", worksheet=sheet.id, field=use, control=control, **base)

    def _param_zone(self, element: etree._Element, base: dict) -> Zone:
        ref = element.get("param", "")
        param = self.ctx.parameter(ref)
        if param is None:
            self.ctx.flag(element, f"參數控制項的參數 {ref} 不存在")
            return Zone(kind="blank", **base)
        mode = element.get("mode")
        control: ControlType
        if mode in PARAM_MODES:
            control = PARAM_MODES[mode]
        elif mode is None:
            control = {"list": "single_dropdown", "range": "single_slider"}.get(
                param.allowed.kind, "type_in"
            )  # type: ignore[assignment]
        else:
            self.ctx.flag(element, f"參數控制項的樣式 {mode} v0 不支援")
            return Zone(kind="blank", **base)
        return Zone(kind="parameter", parameter=param.id, control=control, **base)

    def _check(self, element: etree._Element) -> None:
        for item in children(element):
            tag = local_name(item)
            if tag == "zone" or tag in ZONE_OPAQUE:
                continue
            self.ctx.flag(item, f"dashboard 區塊中的未知元素 <{tag}>")


@dataclass(frozen=True)
class Sizing:
    mode: Literal["fixed", "automatic", "range"]
    width: int
    height: int
    """區塊座標換算時的尺寸。"""
    min_size: Size | None = None
    max_size: Size | None = None


def _int(value: str | None) -> int | None:
    try:
        number = int(value) if value is not None else None
    except ValueError:
        return None
    return number if number and number > 0 else None


def _size(size: etree._Element | None) -> Sizing:
    """dashboard 尺寸：固定、自動（隨視窗）、範圍（隨視窗但有上下限）。"""
    if size is None:
        return Sizing("automatic", *REFERENCE_SIZE)

    def num(name: str) -> int | None:
        raw = size.get(name)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    max_w, max_h, min_w, min_h = (
        num("maxwidth"),
        num("maxheight"),
        num("minwidth"),
        num("minheight"),
    )
    mode = size.get("sizing-mode")
    fixed = mode == "fixed" or (
        mode is None
        and max_w is not None
        and max_w == min_w
        and max_h is not None
        and max_h == min_h
    )
    if fixed:
        return Sizing(
            "fixed", max_w or min_w or REFERENCE_SIZE[0], max_h or min_h or REFERENCE_SIZE[1]
        )
    if mode == "automatic" or not any((max_w, max_h, min_w, min_h)):
        return Sizing("automatic", *REFERENCE_SIZE)
    low = Size(width=min_w, height=min_h) if min_w and min_h else None
    high = Size(width=max_w, height=max_h) if max_w and max_h else None
    reference = high or low
    width, height = (reference.width, reference.height) if reference else REFERENCE_SIZE
    return Sizing("range", width, height, low, high)


TEXT_TOKEN = re.compile(r"<([^<>]+)>")
"""文字物件中的參照：<[Parameters].[參數]>、<Sheet Name>。"""


def _runs(element: etree._Element) -> str | None:
    """格式化文字的純文字（Story 的標題等）：去掉 Æ 與前後的空白。"""
    texts = [run.text or "" for run in descendants(child(element, "formatted-text"), "run")]
    text = "".join(texts).replace("Æ", "").strip()
    return text or None


def formatted_content(
    ctx: ParseContext, element: etree._Element, sheet_name: str
) -> tuple[str | None, list[TextRun]]:
    """文字物件與標題：純文字（舊的寫法，參數保留原樣），與依序的各段（參數換成參數的值、附格式）。

    Tableau 以 Æ 加換行表示段落；<Sheet Name> 為 dashboard 的名稱。只有一段、沒有參數也沒有格式時
    content 為空（只用 text）。
    """
    content: list[TextRun] = []
    plain: list[str] = []
    parameters = False

    def add(text: str, style: TextStyle | None) -> None:
        if not text:
            return
        plain.append(text)
        last = content[-1] if content else None
        if last is not None and last.text is not None and last.style == style:
            content[-1] = last.model_copy(update={"text": last.text + text})
        else:
            content.append(TextRun(text=text, style=style))

    for run in descendants(child(element, "formatted-text"), "run"):
        raw = (run.text or "").replace("Æ\n", "\n").replace("Æ", "")
        style = run_style(run)
        position = 0
        for match in TEXT_TOKEN.finditer(raw):
            add(raw[position : match.start()], style)
            position = match.end()
            token = match.group(1)
            if token == "Sheet Name":
                add(sheet_name, style)
                continue
            parts = qualified_parts(token)
            param = ctx.parameter(token) if len(parts) == 2 and parts[0] == "Parameters" else None
            if param is None:
                add(match.group(0), style)
                continue
            parameters = True
            plain.append(match.group(0))
            content.append(TextRun(parameter=param.id, style=style))
        add(raw[position:], style)
    # 去掉開頭與結尾的空白（與 text 相同）
    while content and content[0].text is not None and not content[0].text.strip():
        content.pop(0)
    while content and content[-1].text is not None and not content[-1].text.strip():
        content.pop()
    if content and content[0].text is not None:
        content[0] = content[0].model_copy(update={"text": content[0].text.lstrip()})
    if content and content[-1].text is not None:
        content[-1] = content[-1].model_copy(update={"text": content[-1].text.rstrip()})
    styled = any(r.style is not None for r in content)
    if not parameters and not styled and len(content) <= 1:
        content = []
    return "".join(plain).strip() or None, content
