"""經緯度範圍的計算：地圖自動縮放的範圍（可能跨越 180 度經線）與麥卡托投影。"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

MAX_LATITUDE = 85.0511
"""Web Mercator 的緯度上限。"""
FIT_LATITUDE = 85.0
"""自動縮放的緯度上限：超出 Web Mercator 範圍的點在投影時被裁掉，無法用來縮放。"""


def covering_arc(intervals: Iterable[tuple[float, float]]) -> tuple[float, float]:
    """涵蓋所有經度區間（west ≤ east，在 ±180 之內）的最短弧；跨越 180 度經線時 west > east。"""
    merged: list[list[float]] = []
    for west, east in sorted(intervals):
        if merged and west <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], east)
        else:
            merged.append([west, east])
    if not merged:
        raise ValueError("沒有經度區間")
    gaps = [
        ((merged[(i + 1) % len(merged)][0] - merged[i][1]) % 360, i) for i in range(len(merged))
    ]
    _, at = max(gaps)
    if at == len(merged) - 1:
        return merged[0][0], merged[-1][1]
    return merged[at + 1][0], merged[at][1]


def mercator_y(lat: float) -> float:
    """緯度 → 麥卡托投影的 y（弧度）。"""
    phi = math.radians(max(-MAX_LATITUDE, min(MAX_LATITUDE, lat)))
    return math.log(math.tan(math.pi / 4 + phi / 2))


def normalize_lon(lon: float) -> float:
    return (lon + 180.0) % 360.0 - 180.0


@dataclass(frozen=True)
class Box:
    """經緯度的範圍：中心經度、東西方向的半寬（度），與南北的緯度。"""

    center: float
    half: float
    south: float
    north: float

    @classmethod
    def of(cls, boxes: Iterable[tuple[float, float, float, float]]) -> Box | None:
        """多個範圍（west, south, east, north；west > east 表示跨越 180 度經線）的聯集。"""
        intervals: list[tuple[float, float]] = []
        south, north = 90.0, -90.0
        for west, s, east, n in boxes:
            if west <= east:
                intervals.append((west, east))
            else:
                intervals += [(west, 180.0), (-180.0, east)]
            south, north = min(south, s), max(north, n)
        if not intervals:
            return None
        west, east = covering_arc(intervals)
        width = east - west if west <= east else east - west + 360.0
        south, north = max(south, -FIT_LATITUDE), min(north, FIT_LATITUDE)
        return cls(normalize_lon(west + width / 2), width / 2, south, north)

    @classmethod
    def viewport(cls, west: float, south: float, east: float, north: float) -> Box:
        """存檔時的顯示範圍：經度可以超出 ±180；超過一整圈時顯示整個世界。"""
        width = east - west
        south, north = max(south, -FIT_LATITUDE), min(north, FIT_LATITUDE)
        return cls(normalize_lon(west + width / 2), min(width / 2, 179.99), south, north)

    def padded(self, fraction: float, min_lon: float, min_lat: float) -> Box:
        """四周留白（以範圍的比例）；範圍太小（例如只有一個點）時至少有 min_lon、min_lat 度。"""
        half = max(self.half * (1 + fraction), min_lon / 2)
        middle = (self.south + self.north) / 2
        half_lat = max((self.north - self.south) / 2 * (1 + fraction), min_lat / 2)
        south = max(middle - half_lat, -FIT_LATITUDE)
        north = min(middle + half_lat, FIT_LATITUDE)
        return Box(self.center, min(half, 179.99), south, north)

    @property
    def west(self) -> float:
        return self.center - self.half

    @property
    def east(self) -> float:
        return self.center + self.half

    def scale(self, width: float, height: float) -> float:
        """投影填滿 width × height 時的比例（像素／弧度）。"""
        dx = math.radians(2 * self.half) or 1e-9
        dy = (mercator_y(self.north) - mercator_y(self.south)) or 1e-9
        return min(width / dx, height / dy)

    def zoom(self, width: float, height: float) -> float:
        """Web 地圖的縮放層級（世界寬 256 × 2^z 像素）。"""
        return math.log2(2 * math.pi * self.scale(width, height) / 256)

    def project(self, width: float, height: float) -> Projector:
        return Projector(self, width, height)


class Projector:
    """填滿 width × height、以 box 置中的麥卡托投影（與 Vega 的 fit 相同）：經緯度 → 像素。"""

    def __init__(self, box: Box, width: float, height: float) -> None:
        self.box = box
        self.k = box.scale(width, height)
        y0, y1 = mercator_y(box.south), mercator_y(box.north)
        self.x_offset = width / 2
        self.y_offset = height / 2 + self.k * (y0 + y1) / 2

    def __call__(self, lon: float, lat: float) -> tuple[float, float]:
        dx = normalize_lon(lon - self.box.center)
        return (
            self.x_offset + self.k * math.radians(dx),
            self.y_offset - self.k * mercator_y(lat),
        )

    def invert(self, x: float, y: float) -> tuple[float, float]:
        """像素 → 經緯度（經度在 box 的中心 ±180 之內，可能超出 ±180）。"""
        lon = self.box.center + math.degrees((x - self.x_offset) / self.k)
        lat = math.degrees(2 * math.atan(math.exp((self.y_offset - y) / self.k)) - math.pi / 2)
        return lon, lat
