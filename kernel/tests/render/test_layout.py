"""dashboard 版面（I1）：固定尺寸用 IR 座標；自動與範圍尺寸依顯示尺寸重排。"""

from __future__ import annotations

import pytest

from tmig.ir.models import Dashboard, Size, Zone
from tmig.render.layout import Box, display_size, layout_dashboard


def zone(zid: str, x: int, y: int, w: int, h: int, **kwargs: object) -> Zone:
    kind = "container" if "children" in kwargs or "layout" in kwargs else "blank"
    return Zone(id=zid, kind=kind, x=x, y=y, w=w, h=h, source_ref=zid, **kwargs)  # type: ignore[arg-type]


def board(sizing: str = "automatic", **kwargs: object) -> Dashboard:
    # 1000×500：左邊兩個區塊上下排列，右邊固定 200 像素寬的欄
    left = zone(
        "z2",
        0,
        0,
        790,
        500,
        layout="vertical",
        children=[zone("z3", 0, 0, 790, 245), zone("z4", 0, 255, 790, 245)],
    )
    right = zone(
        "z5",
        800,
        0,
        200,
        500,
        layout="vertical",
        fixed=200,
        children=[zone("z6", 800, 0, 200, 100, fixed=100), zone("z7", 800, 100, 200, 400)],
    )
    root = zone("z1", 0, 0, 1000, 500, layout="horizontal", children=[left, right])
    return Dashboard(
        id="db",
        name="db",
        size=Size(width=1000, height=500),
        sizing=sizing,  # type: ignore[arg-type]
        zones=[root],
        source_ref="db",
        **kwargs,  # type: ignore[arg-type]
    )


def approx_box(box: Box) -> tuple[object, ...]:
    return tuple(pytest.approx(v) for v in (box.x, box.y, box.w, box.h))


def test_reference_size_keeps_ir_coordinates() -> None:
    dashboard = board()
    boxes = layout_dashboard(dashboard, 1000, 500)
    for z in ("z1", "z2", "z3", "z4", "z5", "z6", "z7"):
        found = boxes[z]
        assert found == Box(found.x, found.y, found.w, found.h)
    assert boxes["z4"] == Box(0, 255, 790, 245)
    assert boxes["z6"] == Box(800, 0, 200, 100)


def test_fixed_zones_keep_pixels_when_resized() -> None:
    boxes = layout_dashboard(board(), 1400, 700)
    # 右欄固定 200 寬；間距 10 不變；左欄拿到其餘的 1190
    assert approx_box(boxes["z5"]) == approx_box(Box(1200, 0, 200, 700))
    assert approx_box(boxes["z2"]) == approx_box(Box(0, 0, 1190, 700))
    # 左欄內上下排列：間距 10 不變，兩個區塊依比例分配 690
    assert approx_box(boxes["z3"]) == approx_box(Box(0, 0, 1190, 345))
    assert approx_box(boxes["z4"]) == approx_box(Box(0, 355, 1190, 345))
    # 右欄內固定 100 高的區塊不變，其餘給下一個區塊
    assert approx_box(boxes["z6"]) == approx_box(Box(1200, 0, 200, 100))
    assert approx_box(boxes["z7"]) == approx_box(Box(1200, 100, 200, 600))


def test_hidden_zone_takes_no_space() -> None:
    dashboard = board()
    right = dashboard.zones[0].children[1]
    right.children[0].hidden = True
    boxes = layout_dashboard(dashboard, 1000, 500)
    assert boxes["z6"].w == 0 and boxes["z6"].h == 0
    # 原本在固定區塊下方的區塊保留原來的間距位置，高度依比例占滿剩餘空間
    assert approx_box(boxes["z7"]) == approx_box(Box(800, 100, 200, 400))


def test_fixed_dashboard_ignores_display_size() -> None:
    dashboard = board("fixed")
    assert display_size(dashboard, 1600, 900) == (1000, 500)
    assert layout_dashboard(dashboard, 1600, 900)["z4"] == Box(0, 255, 790, 245)


def test_range_dashboard_is_clamped() -> None:
    dashboard = board(
        "range", min_size=Size(width=800, height=400), max_size=Size(width=1200, height=600)
    )
    assert display_size(dashboard, 1600, 900) == (1200, 600)
    assert display_size(dashboard, 500, 300) == (800, 400)
    assert display_size(dashboard, 1000, 450) == (1000, 450)


def test_overlapping_flow_children_fall_back_to_scaling() -> None:
    children = [zone("z2", 0, 0, 600, 500), zone("z3", 400, 0, 600, 500)]
    root = zone("z1", 0, 0, 1000, 500, layout="horizontal", children=children)
    dashboard = Dashboard(
        id="db",
        name="db",
        size=Size(width=1000, height=500),
        sizing="automatic",
        zones=[root],
        source_ref="db",
    )
    boxes = layout_dashboard(dashboard, 2000, 1000)
    assert approx_box(boxes["z3"]) == approx_box(Box(800, 0, 1200, 1000))
