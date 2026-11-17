"""dashboard 版面：依顯示尺寸計算每個區塊的位置（I1）。

- 固定尺寸的 dashboard 一律使用 IR 的像素座標
- 自動與範圍尺寸的 dashboard 依顯示尺寸重排：
  - 最外層與 tiled 容器（layout-basic）的子區塊依比例縮放
  - horizontal／vertical 容器（layout-flow）沿排列方向分配：固定大小的區塊維持像素大小，
    區塊間的間距維持不變，其餘區塊依參考尺寸下的大小比例分配剩下的空間；另一個方向依比例縮放。
    Tableau 的固定大小（fixed-size）不含區塊的外距：區塊占的長度是固定大小加上排列方向兩側的外距
  - 隱藏的區塊不占空間；子區塊重疊時（不是正常的並排版面）退回依比例縮放
- 範圍尺寸的顯示尺寸限制在 min_size 與 max_size 之間

在參考尺寸（IR 換算座標用的尺寸）下，結果與 IR 座標相同。
檢視器的 web/src/layout.ts 是同一套演算法，端到端測試確認兩者一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from tmig.ir.models import Dashboard, Zone


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float


def display_size(dashboard: Dashboard, width: float, height: float) -> tuple[float, float]:
    """dashboard 在可用空間 width×height 中的顯示尺寸。"""
    if dashboard.sizing == "fixed":
        return float(dashboard.size.width), float(dashboard.size.height)
    w, h = width, height
    if dashboard.sizing == "range":
        if dashboard.min_size is not None:
            w, h = max(w, dashboard.min_size.width), max(h, dashboard.min_size.height)
        if dashboard.max_size is not None:
            w, h = min(w, dashboard.max_size.width), min(h, dashboard.max_size.height)
    return w, h


def layout_dashboard(dashboard: Dashboard, width: float, height: float) -> dict[str, Box]:
    """顯示尺寸 width×height（已套用範圍限制）下每個區塊的位置。"""
    if dashboard.sizing == "fixed":
        width, height = dashboard.size.width, dashboard.size.height
    sx = width / dashboard.size.width
    sy = height / dashboard.size.height
    result: dict[str, Box] = {}
    for zone in dashboard.zones:
        _place(zone, Box(zone.x * sx, zone.y * sy, zone.w * sx, zone.h * sy), result)
    return result


def _place(zone: Zone, box: Box, result: dict[str, Box]) -> None:
    result[zone.id] = box
    if zone.kind != "container" or not zone.children:
        return
    if zone.layout in ("horizontal", "vertical"):
        _flow(zone, box, result)
        return
    for child in zone.children:
        _place(child, _scaled(child, zone, box), result)


def _ratio(new: float, old: int) -> float:
    return new / old if old else 0.0


def _scaled(child: Zone, parent: Zone, box: Box) -> Box:
    rx, ry = _ratio(box.w, parent.w), _ratio(box.h, parent.h)
    return Box(
        box.x + (child.x - parent.x) * rx,
        box.y + (child.y - parent.y) * ry,
        child.w * rx,
        child.h * ry,
    )


def _flow(zone: Zone, box: Box, result: dict[str, Box]) -> None:
    horizontal = zone.layout == "horizontal"
    visible = [c for c in zone.children if not c.hidden]
    for child in zone.children:
        if child.hidden:
            _place(child, Box(box.x, box.y, 0, 0), result)
    if not visible:
        return

    def start(z: Zone) -> int:
        return z.x if horizontal else z.y

    def span(z: Zone) -> int:
        return z.w if horizontal else z.h

    visible.sort(key=start)
    parent_start, parent_span = start(zone), span(zone)
    new_start, new_span = (box.x, box.w) if horizontal else (box.y, box.h)
    gaps: list[int] = []
    previous_end = parent_start
    for child in visible:
        gaps.append(start(child) - previous_end)
        previous_end = start(child) + span(child)
    trailing = parent_start + parent_span - previous_end
    if min(gaps) < 0 or trailing < 0:
        # 子區塊重疊或超出容器（不是 Tableau 正常的並排版面）：退回依比例縮放
        for child in visible:
            _place(child, _scaled(child, zone, box), result)
        return

    def fixed_span(z: Zone) -> float | None:
        """固定大小的區塊在排列方向占的長度：固定大小加上兩側的外距（Tableau 的 fixed-size 不含外距）。"""
        if z.fixed is None:
            return None
        top, right, bottom, left = z.style.margin if z.style is not None else (0, 0, 0, 0)
        return float(z.fixed + (left + right if horizontal else top + bottom))

    fixed_total = sum(f for c in visible if (f := fixed_span(c)) is not None)
    free_ref = sum(span(c) for c in visible if c.fixed is None)
    free_new = new_span - fixed_total - sum(gaps) - trailing
    scale = max(free_new, 0.0) / free_ref if free_ref else 0.0
    cross = _ratio(box.h, zone.h) if horizontal else _ratio(box.w, zone.w)
    cursor = new_start
    for child, gap in zip(visible, gaps, strict=True):
        cursor += gap
        fixed = fixed_span(child)
        length = fixed if fixed is not None else span(child) * scale
        if horizontal:
            child_box = Box(cursor, box.y + (child.y - zone.y) * cross, length, child.h * cross)
        else:
            child_box = Box(box.x + (child.x - zone.x) * cross, cursor, child.w * cross, length)
        _place(child, child_box, result)
        cursor += length
