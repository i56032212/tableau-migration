"""密度標記（Tableau 的密度圖，2018.3 起）：每個標記以高斯核疊加，依密度上色（低密度處透明），畫成一張
PNG 圖片，以經緯度的四角疊在地圖上（圖片的格點在麥卡托投影中等距，與地圖的投影一致）。"""

from __future__ import annotations

import base64
import struct
import zlib
from collections.abc import Sequence

import numpy as np

PALETTES: dict[str, tuple[str, ...]] = {
    "blue": ("#d4e6f4", "#9ecae1", "#4f9bd0", "#1f6fb2", "#0b4083"),
    "multicolor": ("#3b4cc0", "#5fa2e4", "#9ed8a8", "#f7e267", "#f39b4c", "#d7301f"),
    "gold": ("#fbe9b7", "#f6d06f", "#e9a93a", "#c97d17", "#8f5207"),
    "green": ("#d6efc4", "#a1d99b", "#5bb46f", "#238b45", "#00592b"),
    "red": ("#fcd5c8", "#f99d83", "#ef5d4b", "#c9202b", "#83000f"),
}
"""密度的配色（Tableau 有十種，依名稱的色系對應；預設與 Tableau 相同為藍色）。"""

KERNEL_RADIUS = 16.0
"""高斯核的半徑（像素，標準差的 2 倍），標記卡的大小為 1 時（依 Tableau 的畫面估計）。"""
MAX_GRID = 640
"""圖片的最大邊長（像素）：地圖較大時瀏覽器平滑放大。"""
DEFAULT_INTENSITY = 0.5


def palette_of(name: str | None) -> tuple[str, ...]:
    """Tableau 的密度配色名稱（例如 tableau-map-temperatur、Density Multi-color Dark）→ 顏色。"""
    lowered = (name or "").casefold().replace("-", "").replace(" ", "")
    if "multi" in lowered or "temperatur" in lowered:
        return PALETTES["multicolor"]
    for key in ("gold", "orange", "green", "red"):
        if key in lowered:
            return PALETTES["gold" if key == "orange" else key]
    return PALETTES["blue"]


def density_image(
    points: Sequence[tuple[float, float]],
    width: int,
    height: int,
    radius: float,
    colors: Sequence[str],
    intensity: float | None = None,
) -> str:
    """像素座標（0 ≤ x < width、0 ≤ y < height）的各點疊加成密度圖，回傳 PNG 的 data URL。"""
    width, height = max(int(width), 1), max(int(height), 1)
    grid = np.zeros((height, width), dtype=np.float64)
    xs = np.array([p[0] for p in points], dtype=np.float64)
    ys = np.array([p[1] for p in points], dtype=np.float64)
    inside = (xs >= 0) & (xs < width - 1) & (ys >= 0) & (ys < height - 1)
    xs, ys = xs[inside], ys[inside]
    if len(xs):
        # 雙線性分配到四個相鄰的格點，位置不會因取整數而偏移
        x0, y0 = np.floor(xs).astype(int), np.floor(ys).astype(int)
        fx, fy = xs - x0, ys - y0
        np.add.at(grid, (y0, x0), (1 - fx) * (1 - fy))
        np.add.at(grid, (y0, x0 + 1), fx * (1 - fy))
        np.add.at(grid, (y0 + 1, x0), (1 - fx) * fy)
        np.add.at(grid, (y0 + 1, x0 + 1), fx * fy)
    sigma = max(radius / 2, 0.5)
    reach = int(np.ceil(3 * sigma))
    offsets = np.arange(-reach, reach + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (offsets / sigma) ** 2)
    grid = np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="same"), 1, grid)
    grid = np.apply_along_axis(lambda col: np.convolve(col, kernel, mode="same"), 0, grid)
    peak = float(grid.max())
    level = DEFAULT_INTENSITY if intensity is None else min(max(intensity, 0.0), 1.0)
    # 強度越高，最高的密度越低（更多地方達到最深的顏色，與 Tableau 的「強度」相同）
    t = np.clip(grid / (peak * (1 - 0.8 * level)), 0, 1) if peak > 0 else grid
    stops = np.array([_rgb(c) for c in colors], dtype=np.float64)
    position = t * (len(stops) - 1)
    low = np.clip(np.floor(position).astype(int), 0, len(stops) - 2)
    frac = (position - low)[..., None]
    rgb = stops[low] * (1 - frac) + stops[low + 1] * frac
    alpha = np.clip(t * 2.5, 0, 1) * 0.9
    rgba = np.concatenate([rgb, (alpha * 255)[..., None]], axis=2)
    return "data:image/png;base64," + base64.b64encode(_png(rgba.round().astype(np.uint8))).decode()


def _rgb(color: str) -> tuple[int, int, int]:
    digits = color.lstrip("#")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _png(rgba: np.ndarray) -> bytes:
    """RGBA 的 PNG（不需要影像套件）。"""
    height, width, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
