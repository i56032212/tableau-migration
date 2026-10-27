"""dashboard：固定尺寸、區塊樹與座標換算（A5）。

.twb 以 dashboard 尺寸的 100000 分之一記錄區塊座標；換算為像素時 x、w 依寬度，y、h 依高度。
不支援的區塊以 blank 佔位，並記錄在 unsupported（檢視器依 source_ref 顯示「未支援」）。
"""

from __future__ import annotations

from lxml import etree

from tmig.ir.models import ControlType, Dashboard, Size, Zone
from tmig.parser.context import ParseContext, SheetState
from tmig.parser.fields import instance_key
from tmig.parser.twbx import attr, child, children, descendants, local_name, xpath

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
}
PARAM_MODES: dict[str, ControlType] = {
    "slider": "single_slider",
    "compact": "single_dropdown",
    "radiolist": "single_list",
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
ZONE_OPAQUE = frozenset({"zone-style", "formatted-text", "layout-cache", "format"})


class DashboardParser:
    def __init__(self, ctx: ParseContext, element: etree._Element) -> None:
        self.ctx = ctx
        self.el = element
        self.name = element.get("name", "")
        self.width = 0
        self.height = 0

    def parse(self) -> Dashboard | None:
        ctx, el = self.ctx, self.el
        if el.get("type") == "storyboard":
            ctx.flag(el, f"Story「{self.name}」v0 不支援")
            return None
        size_el = child(el, "size")
        width, height, fixed = _size(size_el)
        if not fixed:
            ctx.flag(
                size_el if size_el is not None else el,
                f"dashboard「{self.name}」不是固定尺寸，v0 不支援；暫以 {width}×{height} 換算座標",
            )
        self.width, self.height = width, height
        for item in children(el):
            tag = local_name(item)
            if tag == "devicelayouts":
                ctx.ignore(item, "裝置專屬版面（本階段只轉桌面版面）")
            elif tag not in DASHBOARD_CHILDREN:
                ctx.flag(item, f"dashboard 中的未知元素 <{tag}>")
        zones = [self._zone(z) for z in children(child(el, "zones"), "zone")]
        return Dashboard(
            id=ctx.ids.make("db", self.name),
            name=self.name,
            size=Size(width=width, height=height),
            zones=zones,
            source_ref=xpath(el),
        )

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
        base = {
            "id": zone_id if zone_id[1:].isdigit() else "z0",
            "x": self._px(element, "x", self.width),
            "y": self._px(element, "y", self.height),
            "w": self._px(element, "w", self.width),
            "h": self._px(element, "h", self.height),
            "source_ref": xpath(element),
        }
        child_zones = children(element, "zone")
        self._check(element)

        if kind_attr in ("layout-basic", "layout-flow", "layout-free"):
            return Zone(kind="container", children=[self._zone(z) for z in child_zones], **base)
        if child_zones:
            self.ctx.flag(element, "非容器的區塊含有子區塊")
        if kind_attr is None and name:
            sheet = self._sheet(element, name)
            if sheet is None:
                return Zone(kind="blank", **base)
            return Zone(kind="worksheet", worksheet=sheet.id, **base)
        if kind_attr == "title":
            return Zone(kind="title", text=_runs(element) or self.name, **base)
        if kind_attr == "text":
            return Zone(kind="text", text=_runs(element), **base)
        if kind_attr == "bitmap":
            image = element.get("param")
            if not image:
                self.ctx.flag(element, "圖片區塊沒有指定圖片")
                return Zone(kind="blank", **base)
            return Zone(kind="image", image=image.replace("\\", "/"), **base)
        if kind_attr == "empty":
            return Zone(kind="blank", **base)
        if kind_attr == "filter":
            return self._filter_zone(element, name, base)
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
        use = sheet.uses.get(key)
        if use is None:
            self.ctx.flag(element, f"篩選控制項的欄位 {param} 不在工作表「{name}」上")
            return Zone(kind="blank", **base)
        filter_kind = sheet.filter_kinds.get(key)
        if filter_kind is None:
            self.ctx.flag(element, f"篩選控制項的欄位 {param} 在工作表「{name}」上沒有篩選")
            return Zone(kind="blank", **base)
        if filter_kind == "top_n":
            self.ctx.flag(element, "Top N 篩選的控制項 v0 不支援")
            return Zone(kind="blank", **base)
        mode = element.get("mode")
        control: ControlType
        if filter_kind == "range":
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


def _size(size: etree._Element | None) -> tuple[int, int, bool]:
    if size is None:
        return REFERENCE_SIZE[0], REFERENCE_SIZE[1], False

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
    width = max_w or min_w or REFERENCE_SIZE[0]
    height = max_h or min_h or REFERENCE_SIZE[1]
    return width, height, fixed


def _runs(element: etree._Element) -> str | None:
    texts = [run.text or "" for run in descendants(child(element, "formatted-text"), "run")]
    text = "".join(texts).replace("Æ", "").strip()
    return text or None
