"""欄列架上都沒有欄位時多個標記的排列：樹狀圖、泡泡圖、文字雲（tmig.render.packing）與它們的規格。"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

from tmig.data.executor import execute_query
from tmig.parser.workbook import parse_workbook
from tmig.render.packing import pack, pack_siblings, squarify, treemap, word_cloud
from tmig.render.vegalite import BUBBLE, CLOUD, TREEMAP, WRAPPED, RenderResult, _wrap, build_spec
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import ref, worksheet
from ..sqlgen.test_query import CATEGORY, REGION, SALES, db
from .test_vegalite import validator

__all__ = ["db"]  # pytest fixture


def overlapping(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """兩個長方形（左、上、寬、高）重疊（只有邊相接不算）。"""
    eps = 1e-9
    return (
        a[0] < b[0] + b[2] - eps
        and b[0] < a[0] + a[2] - eps
        and a[1] < b[1] + b[3] - eps
        and b[1] < a[1] + a[3] - eps
    )


# ---------------------------------------------------------------- 排列


def test_squarify_fills_the_box_in_proportion() -> None:
    values = [6.0, 6, 4, 3, 2, 2, 1]
    rects = squarify(values, 0, 0, 6, 4)
    assert [round(w * h, 9) for _, _, w, h in rects] == [round(v / 24 * 24, 9) for v in values]
    assert sum(w * h for _, _, w, h in rects) == 24
    assert not any(overlapping(a, b) for i, a in enumerate(rects) for b in rects[:i])
    assert all(
        x >= 0 and x + w <= 6 + 1e-9 and y >= 0 and y + h <= 4 + 1e-9 for x, y, w, h in rects
    )
    # 最大的在左上（Bruls 等人的論文中的例子：第一欄是兩個 6）
    assert rects[0][:2] == (0, 0) and rects[1][0] == 0


def test_treemap_groups_are_contiguous_blocks() -> None:
    random.seed(7)
    values: list[float | None] = [random.uniform(1, 10) for _ in range(30)]
    values[3] = None  # Null 與非正數的標記不畫
    values[4] = -2
    groups = [i % 3 for i in range(30)]
    rects = treemap(values, groups, aspect=1.6)
    assert rects[3] is None and rects[4] is None
    shown = [r for r in rects if r is not None]
    assert abs(sum(w * h for _, _, w, h in shown) - 1) < 1e-9
    assert not any(overlapping(a, b) for i, a in enumerate(shown) for b in shown[:i])
    # 同一組的長方形合起來是一個長方形（外框的面積等於各長方形的面積和）
    for g in range(3):
        members = [r for r, k in zip(rects, groups, strict=True) if r is not None and k == g]
        left, top = min(r[0] for r in members), min(r[1] for r in members)
        right = max(r[0] + r[2] for r in members)
        bottom = max(r[1] + r[3] for r in members)
        assert abs((right - left) * (bottom - top) - sum(r[2] * r[3] for r in members)) < 1e-9


def test_pack_siblings_touch_without_overlapping() -> None:
    random.seed(3)
    radii = sorted((random.uniform(1, 10) for _ in range(80)), reverse=True)
    centers = pack_siblings(radii)
    for i in range(len(radii)):
        for j in range(i):
            assert math.dist(centers[i], centers[j]) >= radii[i] + radii[j] - 1e-6
    # 每個圓都靠著另一個圓（相切）
    for i in range(1, len(radii)):
        gap = min(
            math.dist(centers[i], centers[j]) - radii[i] - radii[j]
            for j in range(len(radii))
            if j != i
        )
        assert gap < 1e-6
    # 大的先排，在中間附近
    assert math.dist(centers[0], (0, 0)) <= radii[0] + radii[1]


def test_pack_keeps_groups_together() -> None:
    radii = [3.0, 1, 2, 1, 2.5, 0, 1.5, 1]
    groups = ["a", "b", "a", "b", "c", "c", "a", "c"]
    centers = pack(radii, groups, padding=0.1)
    assert centers[5] is None  # 半徑是 0：不畫
    shown = [i for i, c in enumerate(centers) if c is not None]
    for i in shown:
        for j in shown:
            if i < j:
                ci, cj = centers[i], centers[j]
                assert ci is not None and cj is not None
                assert math.dist(ci, cj) >= radii[i] + radii[j] + 0.1 - 1e-6
    # 同組的泡泡互相靠近：每個泡泡到同組最近的泡泡的距離比到別組的近
    for i in shown:
        own = [j for j in shown if j != i and groups[j] == groups[i]]
        other = [j for j in shown if groups[j] != groups[i]]
        if own:
            near = min(math.dist(centers[i], centers[j]) for j in own)  # type: ignore[arg-type]
            far = min(math.dist(centers[i], centers[j]) for j in other)  # type: ignore[arg-type]
            assert near <= far + max(radii) * 2


def test_word_cloud_places_words_without_overlap() -> None:
    random.seed(5)
    boxes = sorted(
        ((random.uniform(20, 120), random.uniform(10, 36)) for _ in range(120)), key=lambda b: -b[1]
    )
    centers = word_cloud(boxes)
    assert centers[0] == (0.0, 0.0)  # 最大的字在中間
    rects = [(x - w / 2, y - h / 2, w, h) for (x, y), (w, h) in zip(centers, boxes, strict=True)]
    assert not any(overlapping(a, b) for i, a in enumerate(rects) for b in rects[:i])
    width = max(r[0] + r[2] for r in rects) - min(r[0] for r in rects)
    height = max(r[1] + r[3] for r in rects) - min(r[1] for r in rects)
    assert 1.2 < width / height < 2.2  # 寬高約 1.6 的一團
    assert sum(w * h for w, h in boxes) / (width * height) > 0.6


def test_wrap_breaks_at_spaces_punctuation_and_wide_characters() -> None:
    assert _wrap("Technology and Internet", 80, 12) == "Technology\nand Internet"
    assert _wrap("Banking,Insurance,Outsourcing", 90, 12) == "Banking,\nInsurance,\nOutsourcing"
    assert _wrap("資訊與通訊服務業", 50, 12) == "資訊與通\n訊服務業"
    assert _wrap("Supercalifragilistic", 30, 12) == "Supercalifragilistic"  # 不拆開單字
    assert _wrap("A\nB C", 1000, 12) == "A\nB C"


# ---------------------------------------------------------------- 規格


def render(tmp_path: Path, db: Path, sheet: str) -> RenderResult:
    xml = b.workbook(worksheets=sheet)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    rendered = build_spec(ir, ws.id, query.columns, execute_query(db, query))
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered


def values(rendered: RenderResult) -> list[dict[str, Any]]:
    assert rendered.spec is not None
    return rendered.spec["data"]["values"]


def test_treemap_from_size_without_shelves(tmp_path: Path, db: Path) -> None:
    """「自動」標記、大小上有量值、欄列架上沒有欄位：樹狀圖（與 Tableau 相同）。各區的面積與銷售成比例，
    依大小由左上排到右下；標籤（兩個欄位各一行）放得下時顯示在左上角。"""
    sheet = worksheet(
        "Treemap",
        [REGION, SALES],
        mark="Automatic",
        encodings=f"<size column='{ref('sum:Sales:qk')}' />"
        f"<text column='{ref('none:Region:nk')}' /><text column='{ref('sum:Sales:qk')}' />",
    )
    rendered = render(tmp_path, db, sheet)
    assert rendered.mark == "square"
    rows = values(rendered)
    total = sum(r["c1"] for r in rows)
    for r in rows:
        area = (r[TREEMAP[1]] - r[TREEMAP[0]]) * (r[TREEMAP[3]] - r[TREEMAP[2]])
        assert abs(area - r["c1"] / total) < 1e-5
        assert r[WRAPPED].split("\n")[0] == r["c0_label"]
    biggest = max(rows, key=lambda r: r["c1"])
    assert (biggest[TREEMAP[0]], biggest[TREEMAP[2]]) == (0, 0)
    spec = rendered.spec
    assert spec is not None
    rect, text = spec["layer"]
    assert rect["mark"]["type"] == "rect" and rect["encoding"]["x2"]["field"] == TREEMAP[1]
    assert rect["encoding"]["y"]["scale"]["reverse"] is True
    assert text["encoding"]["text"]["condition"]["field"] == WRAPPED
    assert "size" not in spec["encoding"]  # 大小已經是面積


def test_single_square_stays_a_cell(tmp_path: Path, db: Path) -> None:
    """只有一個標記（KPI）：照常畫成一格。"""
    sheet = worksheet(
        "KPI",
        [SALES],
        mark="Square",
        encodings=f"<color column='{ref('sum:Sales:qk')}' /><text column='{ref('sum:Sales:qk')}' />",
    )
    rendered = render(tmp_path, db, sheet)
    assert all(TREEMAP[0] not in r for r in values(rendered))


def test_packed_bubbles(tmp_path: Path, db: Path) -> None:
    """圓形、欄列架上沒有欄位：泡泡圖。面積與銷售成比例、互不重疊，依圖的寬高等比例縮放。"""
    sheet = worksheet(
        "Bubbles",
        [REGION, CATEGORY, SALES],
        mark="Circle",
        encodings=f"<size column='{ref('sum:Sales:qk')}' /><color column='{ref('none:Category:nk')}' />"
        f"<text column='{ref('none:Region:nk')}' />",
    )
    rendered = render(tmp_path, db, sheet)
    rows = [r for r in values(rendered) if r.get(BUBBLE[2])]
    assert len(rows) >= 4
    for r in rows:
        assert abs(r[BUBBLE[2]] ** 2 - r["c2"]) < 1e-3 * r["c2"]
    for i, a in enumerate(rows):
        for c in rows[:i]:
            distance = math.dist((a[BUBBLE[0]], a[BUBBLE[1]]), (c[BUBBLE[0]], c[BUBBLE[1]]))
            assert distance >= a[BUBBLE[2]] + c[BUBBLE[2]] - 1e-3
    spec = rendered.spec
    assert spec is not None
    assert spec["transform"][0]["as"] == BUBBLE[3]
    circle = spec["layer"][0]
    assert circle["mark"]["type"] == "circle" and circle["encoding"]["size"]["scale"] is None
    assert "expr" in circle["encoding"]["x"]["scale"]["domainMin"]


def test_word_cloud(tmp_path: Path, db: Path) -> None:
    """文字、欄列架上沒有欄位、多個標記：文字雲，字級依大小（銷售最多的區最大、在中間）。"""
    sheet = worksheet(
        "Cloud",
        [REGION, SALES],
        mark="Text",
        encodings=f"<text column='{ref('none:Region:nk')}' /><size column='{ref('sum:Sales:qk')}' />",
    )
    rendered = render(tmp_path, db, sheet)
    rows = values(rendered)
    biggest = max(rows, key=lambda r: r["c1"])
    assert (biggest[CLOUD[0]], biggest[CLOUD[1]]) == (0, 0)
    assert biggest[CLOUD[2]] == max(r[CLOUD[2]] for r in rows)
    spec = rendered.spec
    assert spec is not None
    assert spec["mark"]["type"] == "text" and spec["encoding"]["size"]["field"] == CLOUD[3]
    assert spec["encoding"]["text"]["field"] == "c0_label"


def test_labels_without_custom_text_show_every_field(tmp_path: Path, db: Path) -> None:
    """標籤上有多個欄位、沒有自訂的標籤文字：各占一行（與 Tableau 的預設相同）。"""
    sheet = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{ref('none:Region:nk')}' /><text column='{ref('sum:Sales:qk')}' />",
    )
    rendered = render(tmp_path, db, sheet)
    for r in values(rendered):
        assert r["__text0"] == f"{r['c0_label']}\n{r['c1_label']}"
