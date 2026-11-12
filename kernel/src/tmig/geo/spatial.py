"""空間資料（Tableau 的 spatial 型別）：在 DuckDB 中以 WKT 文字存放（與 Hyper 的 CAST(geography AS TEXT)
相同，座標到小數第 7 位、經度在前），在 Python 中以 GeoJSON 的 geometry 表示。

- WKT 的解析與輸出；Hyper 讀入 WKT（空間檔案、萃取）時的處理：經緯度上的直線邊依長度對半切分
  （緯度差超過 0.0016 弧度，或經度差超過 0.0016 弧度除以起點所在 9 度緯度帶的 cos），以大地線畫出時
  與原本的直線相同；外環改成逆時針
- COLLECT：同一種形狀合成 MULTI*，不同種合成 GEOMETRYCOLLECTION（與 Hyper 的 geo_collect 相同，重複的保留）
- 大地測量（WGS84 橢球）：DISTANCE 以 GeographicLib 計算大地線長度；BUFFER 與 Hyper 相同為 120 邊形
  （頂點在半徑 r 的大地線圓上，由正北開始逆時針）；AREA 為大地線多邊形的面積（以等積緯度換到等積球面
  計算，與 GeographicLib 的差距約 1e-10）。Hyper 內部的座標精度約 6e-8 度，結果的差距在公釐以內
- 畫圖用：範圍、標籤的位置、依畫面像素簡化，與 d3 的環方向（外環順時針）
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
from geographiclib.geodesic import Geodesic

Coord = list[float]
"""[經度, 緯度]。"""
Ring = list[Coord]
Geometry = dict[str, Any]
"""GeoJSON 的 geometry：{"type": "Polygon", "coordinates": [...]} 等。"""

DECIMALS = 7
"""WKT 文字的小數位數（與 Hyper 的 CAST(geography AS TEXT) 相同）。"""
DENSIFY_LIMIT = math.degrees(0.0016)
"""Hyper 切分直線邊的長度門檻（度）。"""
BUFFER_SIDES = 120
"""BUFFER 的多邊形邊數（與 Hyper 的 geo_buffer 相同）。"""

LENGTH_UNITS: dict[str, float] = {
    **dict.fromkeys(("meters", "metres", "meter", "metre", "m"), 1.0),
    **dict.fromkeys(("kilometers", "kilometres", "kilometer", "kilometre", "km"), 1000.0),
    **dict.fromkeys(("miles", "mile", "mi"), 1609.344),
    **dict.fromkeys(("feet", "foot", "ft"), 0.3048),
}
"""DISTANCE、BUFFER、AREA 的單位（每單位的公尺數；AREA 為平方）。"""

WGS84 = Geodesic.WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_B = _A * (1 - _F)
_E2 = _F * (2 - _F)
_E = math.sqrt(_E2)

TYPES = {
    "POINT": "Point",
    "LINESTRING": "LineString",
    "POLYGON": "Polygon",
    "MULTIPOINT": "MultiPoint",
    "MULTILINESTRING": "MultiLineString",
    "MULTIPOLYGON": "MultiPolygon",
    "GEOMETRYCOLLECTION": "GeometryCollection",
}
WKT_NAMES = {v: k for k, v in TYPES.items()}
KIND = {
    "POINT": "point",
    "MULTIPOINT": "point",
    "LINESTRING": "line",
    "MULTILINESTRING": "line",
    "POLYGON": "polygon",
    "MULTIPOLYGON": "polygon",
}
MULTI = {"point": "MULTIPOINT", "line": "MULTILINESTRING", "polygon": "MULTIPOLYGON"}


class SpatialError(ValueError):
    pass


def unit_factor(unit: str | None) -> float | None:
    """單位名稱 → 每單位的公尺數；不認得的單位為 None。"""
    if unit is None:
        return None
    return LENGTH_UNITS.get(unit.strip().lower())


# ---------------------------------------------------------------- WKT


class _Reader:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def skip(self) -> None:
        s, i = self.s, self.i
        while i < len(s) and s[i] in " \t\r\n":
            i += 1
        self.i = i

    def peek(self) -> str:
        self.skip()
        return self.s[self.i] if self.i < len(self.s) else ""

    def word(self) -> str:
        self.skip()
        start = self.i
        while self.i < len(self.s) and self.s[self.i].isalpha():
            self.i += 1
        return self.s[start : self.i].upper()

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise SpatialError(f"WKT 的第 {self.i + 1} 個字元應為「{char}」")
        self.i += 1

    def empty(self) -> bool:
        """EMPTY（例如 POLYGON EMPTY）。"""
        self.skip()
        if self.s[self.i : self.i + 5].upper() == "EMPTY":
            self.i += 5
            return True
        return False

    def coords(self) -> list[Coord]:
        """(x y, x y, …)：座標中沒有括號，到下一個右括號為止。"""
        self.expect("(")
        end = self.s.find(")", self.i)
        if end < 0:
            raise SpatialError("WKT 的括號不成對")
        found: list[Coord] = []
        for part in self.s[self.i : end].split(","):
            numbers = part.split()
            if len(numbers) < 2:
                raise SpatialError(f"WKT 的座標「{part.strip()}」不完整")
            found.append([float(numbers[0]), float(numbers[1])])
        self.i = end + 1
        return found

    def listed(self, item: Callable[[], Any]) -> list[Any]:
        """(item, item, …)。"""
        self.expect("(")
        items = [item()]
        while self.peek() == ",":
            self.i += 1
            items.append(item())
        self.expect(")")
        return items

    def point_item(self) -> Coord:
        """MULTIPOINT 的成員：(x y) 或 x y。"""
        if self.peek() == "(":
            found = self.coords()
            return found[0]
        self.skip()
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ",)":
            self.i += 1
        numbers = self.s[start : self.i].split()
        if len(numbers) < 2:
            raise SpatialError("WKT 的 MULTIPOINT 座標不完整")
        return [float(numbers[0]), float(numbers[1])]

    def geometry(self) -> Geometry:
        name = self.word()
        if name not in TYPES:
            raise SpatialError(f"不認得的 WKT 形狀「{name or self.s[:20]}」")
        kind = TYPES[name]
        # 維度標記（Z、M、ZM）：只用前兩個座標
        saved = self.i
        if self.word() not in ("Z", "M", "ZM"):
            self.i = saved
        if kind == "GeometryCollection":
            if self.empty():
                return {"type": kind, "geometries": []}
            return {"type": kind, "geometries": self.listed(self.geometry)}
        if self.empty():
            return {"type": kind, "coordinates": []}
        if kind == "Point":
            return {"type": kind, "coordinates": self.coords()[0]}
        if kind == "LineString":
            return {"type": kind, "coordinates": self.coords()}
        if kind == "Polygon":
            return {"type": kind, "coordinates": self.listed(self.coords)}
        if kind == "MultiPoint":
            return {"type": kind, "coordinates": self.listed(self.point_item)}
        if kind == "MultiLineString":
            return {"type": kind, "coordinates": self.listed(self.coords)}
        return {"type": kind, "coordinates": self.listed(lambda: self.listed(self.coords))}


def parse_wkt(text: str | None) -> Geometry | None:
    """WKT → GeoJSON 的 geometry；None 或空字串為 None，寫法錯誤時丟出 SpatialError。"""
    if text is None or not text.strip():
        return None
    reader = _Reader(text)
    try:
        found = reader.geometry()
    except (ValueError, IndexError) as exc:
        raise SpatialError(f"WKT 無法解析：{exc}") from exc
    if reader.peek() not in ("", ";"):
        raise SpatialError("WKT 的結尾有多餘的文字")
    return found


def _num(value: float) -> str:
    text = f"{value:.{DECIMALS}f}"
    return text[1:] if text.startswith("-") and not text.strip("-0.") else text


def _seq(coords: list[Coord]) -> str:
    return ", ".join(f"{_num(c[0])} {_num(c[1])}" for c in coords)


def format_wkt(geom: Geometry) -> str:
    """GeoJSON 的 geometry → WKT（座標到小數第 7 位，與 Hyper 的文字相同）。"""
    kind = geom["type"]
    name = WKT_NAMES[kind]
    if kind == "GeometryCollection":
        items = geom.get("geometries") or []
        return f"{name}({', '.join(format_wkt(g) for g in items)})" if items else f"{name} EMPTY"
    c = geom.get("coordinates") or []
    if not c:
        # 空的形狀：Hyper 一律寫成 MULTI*（POLYGON EMPTY → MULTIPOLYGON EMPTY）
        return f"{name if name.startswith('MULTI') else 'MULTI' + name} EMPTY"
    if kind == "Point":
        return f"POINT({_num(c[0])} {_num(c[1])})"
    if kind == "LineString":
        return f"LINESTRING({_seq(c)})"
    if kind == "Polygon":
        return "POLYGON(" + ", ".join(f"({_seq(r)})" for r in c) + ")"
    if kind == "MultiPoint":
        return "MULTIPOINT(" + ", ".join(f"({_num(p[0])} {_num(p[1])})" for p in c) + ")"
    if kind == "MultiLineString":
        return "MULTILINESTRING(" + ", ".join(f"({_seq(line)})" for line in c) + ")"
    return (
        "MULTIPOLYGON("
        + ", ".join("(" + ", ".join(f"({_seq(r)})" for r in poly) + ")" for poly in c)
        + ")"
    )


# ---------------------------------------------------------------- 結構


def iter_points(geom: Geometry | None) -> Iterator[Coord]:
    if geom is None:
        return
    kind = geom["type"]
    if kind == "GeometryCollection":
        for g in geom.get("geometries") or []:
            yield from iter_points(g)
    elif kind == "Point" and geom.get("coordinates"):
        yield geom["coordinates"]
    elif kind == "MultiPoint":
        yield from geom.get("coordinates") or []


def iter_lines(geom: Geometry | None) -> Iterator[list[Coord]]:
    if geom is None:
        return
    kind = geom["type"]
    if kind == "GeometryCollection":
        for g in geom.get("geometries") or []:
            yield from iter_lines(g)
    elif kind == "LineString" and geom.get("coordinates"):
        yield geom["coordinates"]
    elif kind == "MultiLineString":
        yield from geom.get("coordinates") or []


def iter_polygons(geom: Geometry | None) -> Iterator[list[Ring]]:
    if geom is None:
        return
    kind = geom["type"]
    if kind == "GeometryCollection":
        for g in geom.get("geometries") or []:
            yield from iter_polygons(g)
    elif kind == "Polygon" and geom.get("coordinates"):
        yield geom["coordinates"]
    elif kind == "MultiPolygon":
        yield from geom.get("coordinates") or []


def bounds(geom: Geometry | None) -> tuple[float, float, float, float] | None:
    """(west, south, east, north)；沒有座標時為 None。"""
    xs: list[float] = []
    ys: list[float] = []
    for p in iter_points(geom):
        xs.append(p[0])
        ys.append(p[1])
    for line in iter_lines(geom):
        if line:
            arr = np.asarray(line, dtype=float)
            xs += [float(arr[:, 0].min()), float(arr[:, 0].max())]
            ys += [float(arr[:, 1].min()), float(arr[:, 1].max())]
    for poly in iter_polygons(geom):
        if poly and poly[0]:
            arr = np.asarray(poly[0], dtype=float)
            xs += [float(arr[:, 0].min()), float(arr[:, 0].max())]
            ys += [float(arr[:, 1].min()), float(arr[:, 1].max())]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def shape_type(geom: Geometry | None) -> str:
    """形狀的種類（Tableau 的 SHAPETYPE 的寫法）：Point、MultiPolygon、Mixed 等；沒有座標為 Empty。"""
    if geom is None:
        return "Empty"
    kind = geom["type"]
    if kind == "GeometryCollection":
        items = [shape_type(g) for g in geom.get("geometries") or []]
        items = [i for i in items if i != "Empty"]
        if not items:
            return "Empty"
        return items[0] if len(set(items)) == 1 else "Mixed"
    return kind if geom.get("coordinates") else "Empty"


# ---------------------------------------------------------------- Hyper 讀入 WKT 時的處理


def _signed_area(ring: Ring) -> float:
    """經緯度平面上的有號面積（逆時針為正）。"""
    if len(ring) < 3:
        return 0.0
    arr = np.asarray(ring, dtype=float)
    x, y = arr[:, 0], arr[:, 1]
    return float(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1])) / 2


def _split_edge(p: Coord, q: Coord, out: list[Coord]) -> None:
    stack = [(p, q)]
    while stack:
        a, b = stack.pop()
        band = math.floor(min(abs(a[1]), 90.0) / 9.0) * 9.0
        cos = math.cos(math.radians(band))
        lon_limit = DENSIFY_LIMIT / cos if cos > 1e-12 else math.inf
        if abs(b[1] - a[1]) > DENSIFY_LIMIT or abs(b[0] - a[0]) > lon_limit:
            m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
            # 先處理前半段（堆疊後進先出）
            stack.append((m, b))
            stack.append((a, m))
        else:
            out.append(b)


def densify_line(coords: list[Coord]) -> list[Coord]:
    """經緯度上的直線邊切分到 Hyper 的長度門檻以下（以大地線畫出時與直線相同）。"""
    if len(coords) < 2:
        return [list(c) for c in coords]
    out: list[Coord] = [list(coords[0])]
    for p, q in itertools.pairwise(coords):
        _split_edge(p, q, out)
    return out


def _orient(ring: Ring, counterclockwise: bool) -> Ring:
    area = _signed_area(ring)
    if (area > 0) != counterclockwise and area != 0:
        return ring[::-1]
    return ring


def _closed(ring: Ring) -> Ring:
    if ring and ring[0] != ring[-1]:
        return [*ring, list(ring[0])]
    return ring


def normalize(geom: Geometry | None) -> Geometry | None:
    """與 Hyper 讀入 WKT 時相同：直線邊切分、環封閉，外環逆時針、內環順時針。"""
    if geom is None:
        return None
    kind = geom["type"]
    if kind == "GeometryCollection":
        items = [normalize(g) for g in geom.get("geometries") or []]
        return {"type": kind, "geometries": [g for g in items if g is not None]}
    c = geom.get("coordinates") or []
    if kind in ("Point", "MultiPoint") or not c:
        return {"type": kind, "coordinates": c}
    if kind == "LineString":
        return {"type": kind, "coordinates": densify_line(c)}
    if kind == "MultiLineString":
        return {"type": kind, "coordinates": [densify_line(line) for line in c]}

    def polygon(rings: list[Ring]) -> list[Ring]:
        return [
            _orient(densify_line(_closed(r)), counterclockwise=(k == 0))
            for k, r in enumerate(rings)
            if r
        ]

    if kind == "Polygon":
        return {"type": kind, "coordinates": polygon(c)}
    return {"type": kind, "coordinates": [polygon(p) for p in c]}


# ---------------------------------------------------------------- COLLECT


def _keyword(text: str) -> tuple[str, str]:
    """WKT 的形狀名稱與括號（含）之後的內容。"""
    stripped = text.strip()
    at = stripped.find("(")
    if at < 0:
        return stripped.split()[0].upper() if stripped else "", ""
    head = stripped[:at].split()
    return (head[0].upper() if head else ""), stripped[at:]


def collect(items: list[str | None] | None) -> str | None:
    """多個形狀合成一個（彙總 COLLECT）：同一種形狀合成 MULTI*，不同種合成 GEOMETRYCOLLECTION，
    只有一個時就是它本身；重複的形狀保留，空的形狀略過（與 Hyper 的 geo_collect 相同）。"""
    texts = [t for t in (items or []) if t and "EMPTY" not in t[:40].upper()]
    if not texts:
        return None
    if len(texts) == 1:
        return texts[0]
    parsed = [_keyword(t) for t in texts]
    kinds = {KIND.get(name) for name, _ in parsed}
    if len(kinds) == 1 and None not in kinds:
        kind = next(iter(kinds))
        assert kind is not None
        members: list[str] = []
        for name, body in parsed:
            if name.startswith("MULTI"):
                inner = body[1:-1].strip()
                if inner:
                    members.append(inner)
            else:
                members.append(body)
        return f"{MULTI[kind]}({', '.join(members)})"
    flat: list[str] = []
    for text, (name, body) in zip(texts, parsed, strict=True):
        if name == "GEOMETRYCOLLECTION":
            inner = body[1:-1].strip()
            if inner:
                flat.append(inner)
        else:
            flat.append(text.strip())
    return f"GEOMETRYCOLLECTION({', '.join(flat)})"


# ---------------------------------------------------------------- 空間函式


def make_point(lat: float | None, lon: float | None) -> str | None:
    """MAKEPOINT(緯度, 經度)：WGS84 的點；緯度超出 ±90 時為 Null，經度超出 ±180 時換到 ±180 之內
    （與 Hyper 相同）。"""
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if abs(lat) > 90:
        return None
    if not -180 <= lon <= 180:
        lon = (lon + 180.0) % 360.0 - 180.0
    return f"POINT({_num(lon)} {_num(lat)})"


def _single_point(text: str | None) -> Coord | None:
    geom = parse_wkt(text) if text else None
    if geom is None:
        return None
    if geom["type"] == "Point" and geom.get("coordinates"):
        return [float(geom["coordinates"][0]), float(geom["coordinates"][1])]
    if geom["type"] == "MultiPoint" and len(geom.get("coordinates") or []) == 1:
        p = geom["coordinates"][0]
        return [float(p[0]), float(p[1])]
    return None


def make_line(a: str | None, b: str | None) -> str | None:
    """MAKELINE(點, 點)：兩點之間的線（畫成大地線）；任一不是點時為 Null。"""
    p, q = _single_point(a), _single_point(b)
    if p is None or q is None:
        return None
    return f"LINESTRING({_seq([p, q])})"


def distance(a: str | None, b: str | None, unit: str | None) -> float | None:
    """DISTANCE(點, 點, 單位)：WGS84 橢球上的大地線長度。"""
    factor = unit_factor(unit)
    p, q = _single_point(a), _single_point(b)
    if p is None or q is None or factor is None:
        return None
    meters = float(WGS84.Inverse(p[1], p[0], q[1], q[0], Geodesic.DISTANCE)["s12"])
    return meters / factor


def destinations(lat: float, lon: float, azimuths: np.ndarray, meters: float) -> np.ndarray:
    """從 (lat, lon) 沿各方位角走 meters 公尺的點（Vincenty 的正算，WGS84；誤差在 0.1 公釐以內）。
    回傳 [[經度, 緯度], …]。"""
    phi1 = math.radians(lat)
    alpha1 = np.radians(azimuths)
    tan_u1 = (1 - _F) * math.tan(phi1)
    cos_u1 = 1 / math.sqrt(1 + tan_u1 * tan_u1)
    sin_u1 = tan_u1 * cos_u1
    sin_a1, cos_a1 = np.sin(alpha1), np.cos(alpha1)
    sigma1 = np.arctan2(tan_u1, cos_a1)
    sin_alpha = cos_u1 * sin_a1
    cos2_alpha = 1 - sin_alpha * sin_alpha
    u2 = cos2_alpha * (_A * _A - _B * _B) / (_B * _B)
    big_a = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    big_b = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))
    sigma = meters / (_B * big_a)
    cos2sm = np.cos(2 * sigma1 + sigma)
    for _ in range(20):
        cos2sm = np.cos(2 * sigma1 + sigma)
        sin_s, cos_s = np.sin(sigma), np.cos(sigma)
        delta = (
            big_b
            * sin_s
            * (
                cos2sm
                + big_b
                / 4
                * (
                    cos_s * (-1 + 2 * cos2sm * cos2sm)
                    - big_b / 6 * cos2sm * (-3 + 4 * sin_s * sin_s) * (-3 + 4 * cos2sm * cos2sm)
                )
            )
        )
        new = meters / (_B * big_a) + delta
        done = bool(np.all(np.abs(new - sigma) < 1e-13))
        sigma = new
        if done:
            break
    sin_s, cos_s = np.sin(sigma), np.cos(sigma)
    cos2sm = np.cos(2 * sigma1 + sigma)
    tmp = sin_u1 * sin_s - cos_u1 * cos_s * cos_a1
    phi2 = np.arctan2(
        sin_u1 * cos_s + cos_u1 * sin_s * cos_a1,
        (1 - _F) * np.sqrt(sin_alpha * sin_alpha + tmp * tmp),
    )
    lam = np.arctan2(sin_s * sin_a1, cos_u1 * cos_s - sin_u1 * sin_s * cos_a1)
    c = _F / 16 * cos2_alpha * (4 + _F * (4 - 3 * cos2_alpha))
    big_l = lam - (1 - c) * _F * sin_alpha * (
        sigma + c * sin_s * (cos2sm + c * cos_s * (-1 + 2 * cos2sm * cos2sm))
    )
    lon2 = (lon + np.degrees(big_l) + 180.0) % 360.0 - 180.0
    return np.column_stack([lon2, np.degrees(phi2)])


def buffer(geom_text: str | None, radius: float | None, unit: str | None) -> str | None:
    """BUFFER(點, 半徑, 單位)：與 Hyper 相同的 120 邊形（頂點在半徑的大地線圓上，由正北開始逆時針）；
    半徑為 0 時是點本身；不是點、半徑為負時為 Null。"""
    factor = unit_factor(unit)
    p = _single_point(geom_text)
    if p is None or radius is None or factor is None or not math.isfinite(radius) or radius < 0:
        return None
    if radius == 0:
        return f"POINT({_num(p[0])} {_num(p[1])})"
    azimuths = -np.arange(BUFFER_SIDES, dtype=float) * (360.0 / BUFFER_SIDES)
    ring = destinations(p[1], p[0], azimuths, radius * factor).tolist()
    ring.append(ring[0])
    return f"POLYGON(({_seq(ring)}))"


def _authalic_q(phi: np.ndarray) -> np.ndarray:
    s = np.sin(phi)
    return (1 - _E2) * (
        s / (1 - _E2 * s * s) - (1 / (2 * _E)) * np.log((1 - _E * s) / (1 + _E * s))
    )


_QP = float(_authalic_q(np.array(math.pi / 2)))
_RQ2 = _A * _A * _QP / 2
"""等積球面的半徑平方。"""


def ring_area(ring: Ring) -> float:
    """大地線多邊形環的面積（平方公尺，不分方向）：緯度換成等積緯度後，以球面上各邊與赤道之間的面積加總。"""
    if len(ring) < 3:
        return 0.0
    arr = np.asarray(ring, dtype=float)
    if np.array_equal(arr[0], arr[-1]):
        arr = arr[:-1]
    if len(arr) < 3:
        return 0.0
    lon = np.radians(arr[:, 0])
    beta = np.arcsin(np.clip(_authalic_q(np.radians(arr[:, 1])) / _QP, -1.0, 1.0))
    lon2, beta2 = np.roll(lon, -1), np.roll(beta, -1)
    dlon = (lon2 - lon + math.pi) % (2 * math.pi) - math.pi
    t1, t2 = np.tan(beta / 2), np.tan(beta2 / 2)
    excess = 2 * np.arctan(np.tan(dlon / 2) * (t1 + t2) / (1 + t1 * t2))
    return abs(float(excess.sum())) * _RQ2


def area(geom_text: str | None, unit: str | None) -> float | None:
    """AREA(形狀, 單位)：多邊形的面積（單位的平方），內環扣除；點與線為 0。"""
    factor = unit_factor(unit)
    if factor is None or geom_text is None:
        return None
    geom = parse_wkt(geom_text)
    if geom is None:
        return None
    total = 0.0
    for poly in iter_polygons(geom):
        for k, ring in enumerate(poly):
            value = ring_area(ring)
            total += value if k == 0 else -value
    return total / (factor * factor)


# ---------------------------------------------------------------- 畫圖


def label_point(geom: Geometry | None) -> Coord | None:
    """標籤的位置：最大的多邊形的重心（在形狀外時改用通過重心的水平線上最長的一段的中點）；
    線為最長的線的中間的頂點；點為第一個點。"""
    polygons = list(iter_polygons(geom))
    if polygons:
        best = max(polygons, key=lambda p: abs(_signed_area(p[0])) if p and p[0] else 0.0)
        if best and len(best[0]) >= 3:
            return _polygon_label(best)
    lines = [line for line in iter_lines(geom) if line]
    if lines:
        longest = max(lines, key=len)
        return list(longest[len(longest) // 2])
    for p in iter_points(geom):
        return list(p)
    return None


def _polygon_label(poly: list[Ring]) -> Coord:
    ring = np.asarray(poly[0], dtype=float)
    x, y = ring[:, 0], ring[:, 1]
    cross = x[:-1] * y[1:] - x[1:] * y[:-1]
    a = cross.sum() / 2
    if abs(a) < 1e-18:
        return [float(x.mean()), float(y.mean())]
    cx = float(((x[:-1] + x[1:]) * cross).sum() / (6 * a))
    cy = float(((y[:-1] + y[1:]) * cross).sum() / (6 * a))
    if _inside(cx, cy, poly):
        return [cx, cy]
    # 通過重心的水平線與各環的交點：取最長的內部線段的中點
    hits: list[float] = []
    for r in poly:
        arr = np.asarray(r, dtype=float)
        x1, y1, x2, y2 = arr[:-1, 0], arr[:-1, 1], arr[1:, 0], arr[1:, 1]
        crossing = (y1 > cy) != (y2 > cy)
        t = (cy - y1[crossing]) / (y2[crossing] - y1[crossing])
        hits += (x1[crossing] + t * (x2[crossing] - x1[crossing])).tolist()
    hits.sort()
    segments = [(hits[i], hits[i + 1]) for i in range(0, len(hits) - 1, 2)]
    if not segments:
        return [cx, cy]
    left, right = max(segments, key=lambda s: s[1] - s[0])
    return [(left + right) / 2, cy]


def _inside(px: float, py: float, poly: list[Ring]) -> bool:
    inside = False
    for r in poly:
        arr = np.asarray(r, dtype=float)
        x1, y1, x2, y2 = arr[:-1, 0], arr[:-1, 1], arr[1:, 0], arr[1:, 1]
        crossing = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xs = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        count = int(np.count_nonzero(crossing & (px < xs)))
        if count % 2:
            inside = not inside
    return inside


Project = Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]
"""經緯度（陣列）→ 畫面像素（陣列）。"""


def _simplify(coords: list[Coord], project: Project, grid: float, ring: bool) -> list[Coord]:
    """依畫面像素簡化：投影到像素格（grid 像素一格），連續落在同一格的頂點只留第一個；座標四捨五入到
    小數第 6 位（約 0.1 公尺）。環至少保留 4 個頂點。"""
    arr = np.asarray(coords, dtype=float)
    if len(arr) <= (4 if ring else 2):
        return np.round(arr, 6).tolist()
    xs, ys = project(arr[:, 0], arr[:, 1])
    cells = np.column_stack([np.floor(xs / grid), np.floor(ys / grid)])
    keep = np.ones(len(arr), dtype=bool)
    keep[1:] = np.any(cells[1:] != cells[:-1], axis=1)
    keep[-1] = True
    kept = arr[keep]
    if ring and len(kept) < 4:
        step = max(len(arr) // 3, 1)
        kept = np.vstack([arr[0], arr[step], arr[min(2 * step, len(arr) - 2)], arr[0]])
    return np.round(kept, 6).tolist()


def render_parts(
    geom: Geometry | None, project: Project, grid: float = 0.5
) -> tuple[Geometry | None, Geometry | None, list[Coord]]:
    """畫圖用的形狀：多邊形（MultiPolygon，d3 的方向：外環順時針、內環逆時針）、線（MultiLineString）
    與點，依畫面像素簡化。"""
    polygons: list[list[Ring]] = []
    for poly in iter_polygons(geom):
        rings: list[Ring] = []
        for k, r in enumerate(poly):
            if len(r) < 4:
                continue
            simple = _simplify(r, project, grid, ring=True)
            rings.append(_orient(simple, counterclockwise=(k != 0)))
        if rings:
            polygons.append(rings)
    lines = [
        _simplify(line, project, grid, ring=False) for line in iter_lines(geom) if len(line) >= 2
    ]
    points = [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in iter_points(geom)]
    polygon_geom = {"type": "MultiPolygon", "coordinates": polygons} if polygons else None
    line_geom = {"type": "MultiLineString", "coordinates": lines} if lines else None
    return polygon_geom, line_geom, points


__all__ = [
    "BUFFER_SIDES",
    "LENGTH_UNITS",
    "Geometry",
    "SpatialError",
    "area",
    "bounds",
    "buffer",
    "collect",
    "distance",
    "format_wkt",
    "label_point",
    "make_line",
    "make_point",
    "normalize",
    "parse_wkt",
    "render_parts",
    "shape_type",
    "unit_factor",
]
