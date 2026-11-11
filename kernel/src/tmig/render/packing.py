"""欄列架上都沒有欄位時多個標記的排列（與 Tableau 相同）：方塊為樹狀圖、圓形為泡泡圖、文字為文字雲。

- 樹狀圖：squarified 演算法（Bruls、Huizing 與 van Wijk），由大到小、大的在左上；有分組（色彩上的維度，
  每個顏色有多個標記）時先依組排出區塊（大小為各組的合計），組內再排
- 泡泡圖：前緣鏈（front-chain）演算法（Wang 等人，與 d3.packSiblings 相同）依序把圓靠著已放好的圓排，
  大的先排、在中間；有分組時同組的泡泡排在一起，各組的外接圓再同樣排列
- 文字雲：大的字先放（在中間），之後的字緊靠已放好的字、找不重疊且讓整體最緊密的位置

座標都是相對的：樹狀圖在 0–1 的正方形中（y 向下，寬度依呼叫端的長寬比換算），泡泡圖與文字雲以原點為
中心，由呼叫端依畫面的大小縮放。
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

Rect = tuple[float, float, float, float]
"""長方形：左、上、寬、高。"""


# ---------------------------------------------------------------- 樹狀圖


def squarify(areas: Sequence[float], x: float, y: float, w: float, h: float) -> list[Rect]:
    """依序（呼叫端已由大到小排好）排出面積與 areas 成比例、填滿 (x, y, w, h) 的長方形。"""
    total = sum(areas)
    if not areas or total <= 0 or w <= 0 or h <= 0:
        return [(x, y, 0.0, 0.0) for _ in areas]
    scaled = [a * w * h / total for a in areas]
    rects: list[Rect] = []
    i = 0
    while i < len(scaled):
        side = min(w, h)
        row = [scaled[i]]
        j = i + 1
        while j < len(scaled) and _worst([*row, scaled[j]], side) <= _worst(row, side):
            row.append(scaled[j])
            j += 1
        size = sum(row)
        last = j == len(scaled)
        if w >= h:
            # 沿短邊（高）排成一欄
            width = w if last else min(size / h, w)
            top = y
            for n, area in enumerate(row):
                height = (y + h - top) if n == len(row) - 1 else area / width
                rects.append((x, top, width, height))
                top += height
            x += width
            w -= width
        else:
            height = h if last else min(size / w, h)
            left = x
            for n, area in enumerate(row):
                width = (x + w - left) if n == len(row) - 1 else area / height
                rects.append((left, y, width, height))
                left += width
            y += height
            h -= height
        i = j
    return rects


def _worst(row: Sequence[float], side: float) -> float:
    """一列（欄）中最狹長的長方形的長寬比。"""
    size = sum(row)
    if size <= 0 or side <= 0:
        return math.inf
    square = side * side
    return max(
        (max(square * a / (size * size), size * size / (square * a)) for a in row if a > 0),
        default=math.inf,
    )


def treemap(
    values: Sequence[float | None], groups: Sequence[Hashable] | None = None, aspect: float = 1.5
) -> list[Rect | None]:
    """各標記的長方形（0–1 的座標，y 向下）。values 不是正數的標記不畫（None）；aspect 為寬與高的比例。"""
    out: list[Rect | None] = [None] * len(values)
    shown = [i for i, v in enumerate(values) if v is not None and v > 0]
    if not shown:
        return out
    size = {i: float(values[i]) for i in shown}  # type: ignore[arg-type]
    width = max(aspect, 1e-6)

    def place(members: list[int], box: Rect) -> None:
        members = sorted(members, key=lambda i: -size[i])  # 同樣大小時依資料的順序
        for i, rect in zip(members, squarify([size[i] for i in members], *box), strict=True):
            out[i] = rect

    blocks = _groups(shown, groups, size)
    if blocks is None:
        place(shown, (0.0, 0.0, width, 1.0))
    else:
        totals = [sum(size[i] for i in members) for members in blocks]
        for members, box in zip(blocks, squarify(totals, 0.0, 0.0, width, 1.0), strict=True):
            place(members, box)
    return [None if r is None else (r[0] / width, r[1], r[2] / width, r[3]) for r in out]


def _groups(
    shown: list[int], groups: Sequence[Hashable] | None, size: dict[int, float]
) -> list[list[int]] | None:
    """分組（由大到小）；沒有分組、只有一組或每組只有一個標記時為 None。"""
    if groups is None:
        return None
    found: dict[Hashable, list[int]] = {}
    for i in shown:
        found.setdefault(groups[i], []).append(i)
    if len(found) < 2 or len(found) == len(shown):
        return None
    return sorted(found.values(), key=lambda members: -sum(size[i] for i in members))


# ---------------------------------------------------------------- 泡泡圖


@dataclass
class _Circle:
    x: float
    y: float
    r: float


class _Node:
    __slots__ = ("circle", "next", "prev")

    def __init__(self, circle: _Circle) -> None:
        self.circle = circle
        self.next: _Node = self
        self.prev: _Node = self


def pack_siblings(radii: Sequence[float]) -> list[tuple[float, float]]:
    """依序把半徑為 radii 的圓靠著排（互不重疊），回傳各圓的中心（第一個在原點附近）。"""
    circles = [_Circle(0.0, 0.0, r) for r in radii]
    n = len(circles)
    if n == 0:
        return []
    a = circles[0]
    if n == 1:
        return [(0.0, 0.0)]
    b = circles[1]
    a.x, b.x = -b.r, a.r
    if n > 2:
        _place(b, a, circles[2])
        na, nb, nc = _Node(a), _Node(b), _Node(circles[2])
        na.next = nc.prev = nb
        nb.next = na.prev = nc
        nc.next = nb.prev = na
        i = 3
        while i < n:
            c = circles[i]
            _place(na.circle, nb.circle, c)
            # 前緣鏈上最近的相交的圓（以沿著鏈的距離判斷前後）
            j, k = nb.next, na.prev
            sj, sk = nb.circle.r, na.circle.r
            retry = False
            while True:
                if sj <= sk:
                    if _intersects(j.circle, c):
                        nb = j
                        na.next, nb.prev = nb, na
                        retry = True
                        break
                    sj += j.circle.r
                    j = j.next
                else:
                    if _intersects(k.circle, c):
                        na = k
                        na.next, nb.prev = nb, na
                        retry = True
                        break
                    sk += k.circle.r
                    k = k.prev
                if j is k.next:
                    break
            if retry:
                continue
            # 放在 a 與 b 之間，再找最靠近中心的相鄰兩圓
            node = _Node(c)
            node.prev, node.next = na, nb
            na.next = nb.prev = node
            nb = node
            best = _score(na)
            current = node.next
            while current is not nb:
                score = _score(current)
                if score < best:
                    na, best = current, score
                current = current.next
            nb = na.next
            i += 1
    return [(c.x, c.y) for c in circles]


def _place(b: _Circle, a: _Circle, c: _Circle) -> None:
    """把 c 放在與 a、b 都相切的位置。"""
    dx, dy = b.x - a.x, b.y - a.y
    d2 = dx * dx + dy * dy
    if d2:
        a2 = (a.r + c.r) ** 2
        b2 = (b.r + c.r) ** 2
        if a2 > b2:
            x = (d2 + b2 - a2) / (2 * d2)
            y = math.sqrt(max(0.0, b2 / d2 - x * x))
            c.x = b.x - x * dx - y * dy
            c.y = b.y - x * dy + y * dx
        else:
            x = (d2 + a2 - b2) / (2 * d2)
            y = math.sqrt(max(0.0, a2 / d2 - x * x))
            c.x = a.x + x * dx - y * dy
            c.y = a.y + x * dy + y * dx
    else:
        c.x = a.x + c.r
        c.y = a.y


def _intersects(a: _Circle, b: _Circle) -> bool:
    dr = a.r + b.r - 1e-6
    dx, dy = b.x - a.x, b.y - a.y
    return dr > 0 and dr * dr > dx * dx + dy * dy


def _score(node: _Node) -> float:
    a, b = node.circle, node.next.circle
    ab = a.r + b.r
    dx = (a.x * b.r + b.x * a.r) / ab
    dy = (a.y * b.r + b.y * a.r) / ab
    return dx * dx + dy * dy


def enclosing(centers: Sequence[tuple[float, float]], radii: Sequence[float]) -> _Circle:
    """包住所有圓的圓（近似最小）：從面積加權的重心開始，逐步往最遠的圓移動。"""
    weights = [r * r for r in radii]
    total = sum(weights) or 1.0
    x = sum(c[0] * w for c, w in zip(centers, weights, strict=True)) / total
    y = sum(c[1] * w for c, w in zip(centers, weights, strict=True)) / total
    for step in range(1, 200):
        far = max(range(len(radii)), key=lambda i: math.dist((x, y), centers[i]) + radii[i])
        fx, fy = centers[far]
        distance = math.dist((x, y), (fx, fy))
        if distance == 0:
            break
        # 往最遠的圓的外緣移動，步伐逐漸縮小（Bădoiu–Clarkson）
        reach = (distance + radii[far]) / distance
        tx, ty = x + (fx - x) * reach, y + (fy - y) * reach
        x += (tx - x) / (step + 1) / 2
        y += (ty - y) / (step + 1) / 2
    radius = max(math.dist((x, y), c) + r for c, r in zip(centers, radii, strict=True))
    return _Circle(x, y, radius)


def pack(
    radii: Sequence[float], groups: Sequence[Hashable] | None = None, padding: float = 0.0
) -> list[tuple[float, float] | None]:
    """泡泡的中心：大的先排；半徑不是正數的不畫（None）。padding 為泡泡之間的空隙。"""
    out: list[tuple[float, float] | None] = [None] * len(radii)
    shown = [i for i, r in enumerate(radii) if r > 0]
    if not shown:
        return out
    size = {i: radii[i] for i in shown}

    def siblings(members: list[int]) -> list[tuple[float, float]]:
        return pack_siblings([size[i] + padding / 2 for i in members])

    blocks = _groups(shown, groups, {i: r * r for i, r in size.items()})
    if blocks is None:
        members = sorted(shown, key=lambda i: -size[i])
        for i, center in zip(members, siblings(members), strict=True):
            out[i] = center
        return out
    placed: list[tuple[list[int], list[tuple[float, float]], _Circle]] = []
    for block in blocks:
        members = sorted(block, key=lambda i: -size[i])
        centers = siblings(members)
        placed.append((members, centers, enclosing(centers, [size[i] for i in members])))
    outer = pack_siblings([circle.r + padding for _, _, circle in placed])
    for (members, centers, circle), (gx, gy) in zip(placed, outer, strict=True):
        for i, (x, y) in zip(members, centers, strict=True):
            out[i] = (gx + x - circle.x, gy + y - circle.y)
    return out


# ---------------------------------------------------------------- 文字雲


def word_cloud(
    boxes: Sequence[tuple[float, float]], ratio: float = 1.6, gap: float = 2.0
) -> list[tuple[float, float]]:
    """依序放置寬高為 boxes 的文字，回傳各字的中心（第一個在原點）。每個字放在緊靠已放好的字
    （上下左右，對齊兩端或中間）、不與其他字重疊的位置中，讓整體的外框最小（寬度以 ratio 折算，整體排成
    寬高約 ratio 的長方形）、再最靠近中心的一個。"""
    if not boxes:
        return []
    cell = max(16.0, 2 * sum((w + h) / 2 for w, h in boxes) / len(boxes))
    grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
    placed: list[tuple[float, float, float, float]] = []
    centers: list[tuple[float, float]] = []
    outer = (0.0, 0.0, 0.0, 0.0)
    for w, h in boxes:
        center = (0.0, 0.0)
        if placed:
            candidates: list[tuple[float, float]] = []
            for x0, y0, x1, y1 in placed:
                for cy in (y0 + h / 2, (y0 + y1) / 2, y1 - h / 2):
                    candidates.append((x1 + gap + w / 2, cy))
                    candidates.append((x0 - gap - w / 2, cy))
                for cx in (x0 + w / 2, (x0 + x1) / 2, x1 - w / 2):
                    candidates.append((cx, y0 - gap - h / 2))
                    candidates.append((cx, y1 + gap + h / 2))

            def score(
                c: tuple[float, float],
                w: float = w,
                h: float = h,
                outer: tuple[float, float, float, float] = outer,
            ) -> tuple[float, float]:
                left, top = min(outer[0], c[0] - w / 2), min(outer[1], c[1] - h / 2)
                right, bottom = max(outer[2], c[0] + w / 2), max(outer[3], c[1] + h / 2)
                size = max((right - left) / ratio, bottom - top)
                return round(size, 6), (c[0] / ratio) ** 2 + c[1] ** 2

            candidates.sort(key=score)
            center = next(
                (
                    (cx, cy)
                    for cx, cy in candidates
                    if not _collides(grid, cell, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
                ),
                (outer[2] + gap + w / 2, 0.0),
            )
        box = (center[0] - w / 2, center[1] - h / 2, center[0] + w / 2, center[1] + h / 2)
        outer = (
            box
            if not placed
            else (
                min(outer[0], box[0]),
                min(outer[1], box[1]),
                max(outer[2], box[2]),
                max(outer[3], box[3]),
            )
        )
        _insert(grid, cell, box)
        placed.append(box)
        centers.append(center)
    return centers


def _cells(cell: float, box: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = box
    return [
        (i, j)
        for i in range(math.floor(x0 / cell), math.floor(x1 / cell) + 1)
        for j in range(math.floor(y0 / cell), math.floor(y1 / cell) + 1)
    ]


def _collides(
    grid: dict[tuple[int, int], list[tuple[float, float, float, float]]],
    cell: float,
    box: tuple[float, float, float, float],
) -> bool:
    x0, y0, x1, y1 = box
    for key in _cells(cell, box):
        for a0, b0, a1, b1 in grid.get(key, ()):
            if x0 < a1 - 1e-9 and a0 < x1 - 1e-9 and y0 < b1 - 1e-9 and b0 < y1 - 1e-9:
                return True
    return False


def _insert(
    grid: dict[tuple[int, int], list[tuple[float, float, float, float]]],
    cell: float,
    box: tuple[float, float, float, float],
) -> None:
    for key in _cells(cell, box):
        grid.setdefault(key, []).append(box)
