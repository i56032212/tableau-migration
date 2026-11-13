"""投影座標 → WGS84 的經緯度：空間檔案的 .prj（ESRI／OGC 的 WKT）與 MAKEPOINT(X, Y, SRID) 的 EPSG 代碼。

支援的投影（公式依 EPSG Guidance Note 7-2）：橫麥卡托（JHS 的 Krüger 級數，UTM、TWD97 二度分帶等）、
麥卡托（1SP、2SP 與 Web Mercator）、Lambert 正形圓錐（1SP、2SP）、Albers 等積圓錐與 Lambert 方位等積；
地理座標系統直接是經緯度（本初子午線不是格林威治時加上差值）。

基準面之間的轉換（Crs.to_wgs84）：與 Tableau 相同「不論座標系統，一律轉換成 WGS84（EPSG:4326）」。
WGS84 與 GRS80 橢球的基準面（TWD97、NAD83、ETRS89、GDA94、JGD2000 等，與 WGS84 的差距在 1 公尺左右）直接以上面的
公式換成經緯度；其他橢球的基準面（例如 TWD67 的 GRS67、OSGB 1936 的 Airy、Tokyo 的 Bessel、NAD27 的 Clarke 1866、
ED50 的 International 1924）以 PROJ（pyproj）依 EPSG 資料庫的轉換參數計算（沒有網路下載的格網檔，以七參數等
轉換，與 GDAL 預設相同）；上面的公式不支援的投影（例如斜軸麥卡托、斜軸立體投影）與 EPSG 代碼也以 PROJ 計算。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

import numpy as np


class ProjectionError(ValueError):
    pass


PROJ_KIND = "proj"
"""上面的公式不支援、以 PROJ 計算的座標系統。"""


SUPPLEMENTAL_TOWGS84 = {
    # TWD67（虎子山基準）：EPSG 資料庫沒有到 WGS84 的轉換（PROJ 只能以 ballpark 不轉換，偏差約 800 公尺）。
    # 臺灣常用的七參數（台北的 TWD67 → TWD97 約為 X + 828、Y - 207 公尺，與常用的換算相符）
    "twd1967": "-752,-358,-179,-0.0000011698,0.0000018398,0.0000009822,0.00002329",
    "twd67": "-752,-358,-179,-0.0000011698,0.0000018398,0.0000009822,0.00002329",
    "taiwandatum1967": "-752,-358,-179,-0.0000011698,0.0000018398,0.0000009822,0.00002329",
}
"""EPSG 資料庫沒有轉換參數的基準面（依基準面的名稱，只留英數字、小寫）→ PROJ 的 towgs84。"""


@lru_cache(maxsize=128)
def _transformer(source: str) -> Any:
    """座標系統 → WGS84 的 PROJ 轉換（經度在前）；沒有安裝 pyproj 或認不得時為 None。"""
    import warnings

    try:
        from pyproj import CRS, Transformer
        from pyproj.exceptions import CRSError, ProjError
    except ImportError:  # pragma: no cover - 依安裝的套件
        return None
    with warnings.catch_warnings():
        # 沒有下載格網檔時 PROJ 改用次佳的轉換（七參數等）並提出警告
        warnings.simplefilter("ignore")
        try:
            crs = CRS.from_user_input(source)
            datum = re.sub(r"[^a-z0-9]", "", (crs.datum.name if crs.datum else "").lower())
            towgs84 = next((v for k, v in SUPPLEMENTAL_TOWGS84.items() if k in datum), None)
            if towgs84 is not None:
                crs = CRS.from_proj4(f"{crs.to_proj4()} +towgs84={towgs84}")
            return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except (CRSError, ProjError):
            return None


def _proj_crs(source: str) -> Crs:
    """以 PROJ 計算的座標系統；PROJ 也認不得時丟出 ProjectionError。"""
    try:
        from pyproj import CRS
        from pyproj.exceptions import CRSError
    except ImportError as exc:  # pragma: no cover - 依安裝的套件
        raise ProjectionError("這個座標系統需要 PROJ（pyproj）") from exc
    try:
        crs = CRS.from_user_input(source)
    except CRSError as exc:
        raise ProjectionError(f"座標系統無法解讀：{exc}") from exc
    if _transformer(source) is None:
        raise ProjectionError(f"座標系統「{crs.name}」無法換成 WGS84")
    return Crs(PROJ_KIND, name=crs.name, source=source)


@dataclass(frozen=True)
class Ellipsoid:
    a: float
    inv_f: float
    """扁率的倒數；0 表示圓球。"""

    @property
    def f(self) -> float:
        return 1 / self.inv_f if self.inv_f else 0.0

    @property
    def e2(self) -> float:
        return self.f * (2 - self.f)

    @property
    def e(self) -> float:
        return math.sqrt(self.e2)


WGS84 = Ellipsoid(6378137.0, 298.257223563)
GRS80 = Ellipsoid(6378137.0, 298.257222101)
GRS67 = Ellipsoid(6378160.0, 298.247167427)
AIRY = Ellipsoid(6377563.396, 299.3249646)
BESSEL = Ellipsoid(6377397.155, 299.1528128)
INTL = Ellipsoid(6378388.0, 297.0)
CLARKE66 = Ellipsoid(6378206.4, 294.9786982)
KRASSOWSKY = Ellipsoid(6378245.0, 298.3)


@dataclass(frozen=True)
class Crs:
    """座標系統：kind 為 geographic、tm、mercator、web_mercator、lcc、albers 或 laea；角度為度。"""

    kind: str
    ellipsoid: Ellipsoid = WGS84
    lat0: float = 0.0
    lon0: float = 0.0
    k0: float = 1.0
    fe: float = 0.0
    """假東距（公尺）。"""
    fn: float = 0.0
    """假北距（公尺）。"""
    sp1: float | None = None
    sp2: float | None = None
    unit: float = 1.0
    """座標的單位是幾公尺（美國測量英尺 0.3048006096…）；地理座標系統為角度單位是幾度。"""
    prime_meridian: float = 0.0
    name: str = ""
    extra: dict[str, float] = field(default_factory=dict, compare=False, hash=False)
    source: str = field(default="", compare=False, hash=False)
    """原本的定義（.prj 的 WKT 或 EPSG:代碼）：基準面要轉換時以 PROJ 計算。"""

    @property
    def shifted(self) -> bool:
        """基準面不是 WGS84（或幾乎相同的 GRS80 系）：換成 WGS84 時要轉換基準面。"""
        if self.kind == PROJ_KIND:
            return True
        return not any(
            abs(self.ellipsoid.a - e.a) < 1e-3 and abs(self.ellipsoid.inv_f - e.inv_f) < 1e-6
            for e in (WGS84, GRS80)
        )

    def to_wgs84(self, x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        """座標（陣列）→ WGS84 的 (經度, 緯度)（度），含基準面之間的轉換。"""
        if self.shifted and self.source:
            transformer = _transformer(self.source)
            if transformer is not None:
                lon, lat = transformer.transform(
                    np.asarray(x, dtype=float), np.asarray(y, dtype=float)
                )
                lon = (np.asarray(lon, dtype=float) + 180.0) % 360.0 - 180.0
                return lon, np.asarray(lat, dtype=float)
        if self.kind == PROJ_KIND:
            raise ProjectionError(f"座標系統「{self.name or self.source}」需要 PROJ（pyproj）")
        return self.inverse(x, y)

    def inverse(self, x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
        """投影座標（陣列）→ (經度, 緯度)（度）。"""
        xs = np.asarray(x, dtype=float) * self.unit
        ys = np.asarray(y, dtype=float) * self.unit
        if self.kind == "geographic":
            return xs + self.prime_meridian, ys.copy()
        handler = _INVERSE.get(self.kind)
        if handler is None:
            raise ProjectionError(f"投影 {self.kind} 尚未支援")
        lon, lat = handler(self, xs - self.fe, ys - self.fn)
        lon = np.degrees(lon) + self.lon0 + self.prime_meridian
        lon = (lon + 180.0) % 360.0 - 180.0
        return lon, np.degrees(lat)

    def forward(self, lon: Any, lat: Any) -> tuple[np.ndarray, np.ndarray]:
        """(經度, 緯度)（度）→ 投影座標（測試與驗算用）。"""
        lons = np.asarray(lon, dtype=float) - self.prime_meridian
        lats = np.asarray(lat, dtype=float)
        if self.kind == "geographic":
            return lons / self.unit, lats / self.unit
        handler = _FORWARD.get(self.kind)
        if handler is None:
            raise ProjectionError(f"投影 {self.kind} 尚未支援")
        dlon = np.radians((lons - self.lon0 + 180.0) % 360.0 - 180.0)
        x, y = handler(self, dlon, np.radians(lats))
        return (x + self.fe) / self.unit, (y + self.fn) / self.unit


# ---------------------------------------------------------------- 共用


def _t(phi: Any, e: float) -> Any:
    s = np.sin(phi)
    return np.tan(math.pi / 4 - phi / 2) / ((1 - e * s) / (1 + e * s)) ** (e / 2)


def _m(phi: float, e2: float) -> float:
    return math.cos(phi) / math.sqrt(1 - e2 * math.sin(phi) ** 2)


def _phi_from_t(t: Any, e: float) -> Any:
    phi = math.pi / 2 - 2 * np.arctan(t)
    for _ in range(15):
        s = np.sin(phi)
        phi = math.pi / 2 - 2 * np.arctan(t * ((1 - e * s) / (1 + e * s)) ** (e / 2))
    return phi


def _q(phi: Any, e: float, e2: float) -> Any:
    s = np.sin(phi)
    if e == 0:
        return 2 * s
    return (1 - e2) * (s / (1 - e2 * s * s) - (1 / (2 * e)) * np.log((1 - e * s) / (1 + e * s)))


def _from_authalic(beta: Any, e2: float) -> Any:
    """等積緯度 → 緯度：級數的近似值再以牛頓法修正（Snyder 3-16），誤差在 1e-12 度以內。"""
    e4, e6 = e2 * e2, e2 * e2 * e2
    phi = (
        beta
        + (e2 / 3 + 31 * e4 / 180 + 517 * e6 / 5040) * np.sin(2 * beta)
        + (23 * e4 / 360 + 251 * e6 / 3780) * np.sin(4 * beta)
        + (761 * e6 / 45360) * np.sin(6 * beta)
    )
    e = math.sqrt(e2)
    if e == 0:
        return phi
    qp = float(_q(math.pi / 2, e, e2))
    q = np.sin(beta) * qp
    for _ in range(3):
        s = np.sin(phi)
        c = np.cos(phi)
        with np.errstate(divide="ignore", invalid="ignore"):
            step = (
                (1 - e2 * s * s) ** 2
                / (2 * c)
                * (
                    q / (1 - e2)
                    - s / (1 - e2 * s * s)
                    + (1 / (2 * e)) * np.log((1 - e * s) / (1 + e * s))
                )
            )
        phi = np.where(np.abs(c) > 1e-12, phi + np.nan_to_num(step), phi)
    return phi


# ---------------------------------------------------------------- 橫麥卡托（JHS）


def _tm_constants(crs: Crs) -> tuple[float, list[float], list[float], float]:
    f = crs.ellipsoid.f
    e = crs.ellipsoid.e
    n = f / (2 - f)
    big_b = crs.ellipsoid.a / (1 + n) * (1 + n**2 / 4 + n**4 / 64)
    h = [
        n / 2 - 2 / 3 * n**2 + 5 / 16 * n**3 + 41 / 180 * n**4,
        13 / 48 * n**2 - 3 / 5 * n**3 + 557 / 1440 * n**4,
        61 / 240 * n**3 - 103 / 140 * n**4,
        49561 / 161280 * n**4,
    ]
    hi = [
        n / 2 - 2 / 3 * n**2 + 37 / 96 * n**3 - 1 / 360 * n**4,
        1 / 48 * n**2 + 1 / 15 * n**3 - 437 / 1440 * n**4,
        17 / 480 * n**3 - 37 / 840 * n**4,
        4397 / 161280 * n**4,
    ]
    phi0 = math.radians(crs.lat0)
    if phi0 == 0:
        m0 = 0.0
    else:
        q0 = math.asinh(math.tan(phi0)) - e * math.atanh(e * math.sin(phi0))
        beta0 = math.atan(math.sinh(q0))
        xi00 = math.asin(math.sin(beta0))
        xi0 = xi00 + sum(h[k] * math.sin(2 * (k + 1) * xi00) for k in range(4))
        m0 = big_b * xi0
    return big_b, h, hi, m0


def _tm_forward(crs: Crs, dlon: np.ndarray, phi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    e = crs.ellipsoid.e
    big_b, h, _, m0 = _tm_constants(crs)
    q = np.arcsinh(np.tan(phi)) - e * np.arctanh(e * np.sin(phi))
    beta = np.arctan(np.sinh(q))
    eta0 = np.arctanh(np.cos(beta) * np.sin(dlon))
    xi0 = np.arcsin(np.sin(beta) * np.cosh(eta0))
    xi = xi0 + sum(h[k] * np.sin(2 * (k + 1) * xi0) * np.cosh(2 * (k + 1) * eta0) for k in range(4))
    eta = eta0 + sum(
        h[k] * np.cos(2 * (k + 1) * xi0) * np.sinh(2 * (k + 1) * eta0) for k in range(4)
    )
    return crs.k0 * big_b * eta, crs.k0 * (big_b * xi - m0)


def _tm_inverse(crs: Crs, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    e = crs.ellipsoid.e
    big_b, _, hi, m0 = _tm_constants(crs)
    eta_ = x / (big_b * crs.k0)
    xi_ = (y + crs.k0 * m0) / (big_b * crs.k0)
    xi0 = xi_ - sum(
        hi[k] * np.sin(2 * (k + 1) * xi_) * np.cosh(2 * (k + 1) * eta_) for k in range(4)
    )
    eta0 = eta_ - sum(
        hi[k] * np.cos(2 * (k + 1) * xi_) * np.sinh(2 * (k + 1) * eta_) for k in range(4)
    )
    beta = np.arcsin(np.sin(xi0) / np.cosh(eta0))
    q = np.arcsinh(np.tan(beta))
    qq = q.copy()
    for _ in range(15):
        qq = q + e * np.arctanh(e * np.tanh(qq))
    phi = np.arctan(np.sinh(qq))
    lam = np.arcsin(np.tanh(eta0) / np.cos(beta))
    return lam, phi


# ---------------------------------------------------------------- 麥卡托


def _mercator_k0(crs: Crs) -> float:
    if crs.sp1 is not None:
        phi1 = math.radians(crs.sp1)
        return _m(phi1, crs.ellipsoid.e2)
    return crs.k0


def _mercator_forward(crs: Crs, dlon: np.ndarray, phi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, e = crs.ellipsoid.a, crs.ellipsoid.e
    k0 = _mercator_k0(crs)
    return a * k0 * dlon, -a * k0 * np.log(_t(phi, e))


def _mercator_inverse(crs: Crs, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, e = crs.ellipsoid.a, crs.ellipsoid.e
    k0 = _mercator_k0(crs)
    t = np.exp(-y / (a * k0))
    return x / (a * k0), _phi_from_t(t, e)


def _web_forward(crs: Crs, dlon: np.ndarray, phi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = crs.ellipsoid.a
    return a * dlon, a * np.log(np.tan(math.pi / 4 + phi / 2))


def _web_inverse(crs: Crs, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = crs.ellipsoid.a
    return x / a, math.pi / 2 - 2 * np.arctan(np.exp(-y / a))


# ---------------------------------------------------------------- Lambert 正形圓錐


def _lcc_constants(crs: Crs) -> tuple[float, float, float]:
    """n、F 與原點的 r（rF）；1SP 時比例係數併入 F。"""
    a, e, e2 = crs.ellipsoid.a, crs.ellipsoid.e, crs.ellipsoid.e2
    phi0 = math.radians(crs.lat0)
    if crs.sp1 is not None and crs.sp2 is not None and abs(crs.sp1 - crs.sp2) > 1e-12:
        phi1, phi2 = math.radians(crs.sp1), math.radians(crs.sp2)
        m1, m2 = _m(phi1, e2), _m(phi2, e2)
        t1, t2 = float(_t(phi1, e)), float(_t(phi2, e))
        n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
        big_f = m1 / (n * t1**n)
        r_f = a * big_f * float(_t(phi0, e)) ** n
        return n, big_f, r_f
    origin = math.radians(crs.sp1) if crs.sp1 is not None else phi0
    m0, t0 = _m(origin, e2), float(_t(origin, e))
    n = math.sin(origin)
    big_f = m0 / (n * t0**n) * crs.k0
    r_f = a * big_f * float(_t(phi0, e)) ** n
    return n, big_f, r_f


def _lcc_forward(crs: Crs, dlon: np.ndarray, phi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, e = crs.ellipsoid.a, crs.ellipsoid.e
    n, big_f, r_f = _lcc_constants(crs)
    r = a * big_f * _t(phi, e) ** n
    theta = n * dlon
    return r * np.sin(theta), r_f - r * np.cos(theta)


def _lcc_inverse(crs: Crs, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, e = crs.ellipsoid.a, crs.ellipsoid.e
    n, big_f, r_f = _lcc_constants(crs)
    dy = r_f - y
    r = np.sign(n) * np.sqrt(x * x + dy * dy)
    theta = np.arctan2(np.sign(n) * x, np.sign(n) * dy)
    t = (r / (a * big_f)) ** (1 / n)
    return theta / n, _phi_from_t(t, e)


# ---------------------------------------------------------------- Albers 等積圓錐


def _albers_constants(crs: Crs) -> tuple[float, float, float]:
    a, e, e2 = crs.ellipsoid.a, crs.ellipsoid.e, crs.ellipsoid.e2
    phi0 = math.radians(crs.lat0)
    phi1 = math.radians(crs.sp1 if crs.sp1 is not None else crs.lat0)
    phi2 = math.radians(crs.sp2 if crs.sp2 is not None else crs.lat0)
    m1, m2 = _m(phi1, e2), _m(phi2, e2)
    q0, q1, q2 = (float(_q(p, e, e2)) for p in (phi0, phi1, phi2))
    n = (m1 * m1 - m2 * m2) / (q2 - q1) if abs(phi1 - phi2) > 1e-12 else math.sin(phi1)
    c = m1 * m1 + n * q1
    rho0 = a * math.sqrt(c - n * q0) / n
    return n, c, rho0


def _albers_forward(crs: Crs, dlon: np.ndarray, phi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, e, e2 = crs.ellipsoid.a, crs.ellipsoid.e, crs.ellipsoid.e2
    n, c, rho0 = _albers_constants(crs)
    rho = a * np.sqrt(c - n * _q(phi, e, e2)) / n
    theta = n * dlon
    return rho * np.sin(theta), rho0 - rho * np.cos(theta)


def _albers_inverse(crs: Crs, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, e, e2 = crs.ellipsoid.a, crs.ellipsoid.e, crs.ellipsoid.e2
    n, c, rho0 = _albers_constants(crs)
    dy = rho0 - y
    rho = np.sqrt(x * x + dy * dy)
    theta = np.arctan2(np.sign(n) * x, np.sign(n) * dy)
    q = (c - rho * rho * n * n / (a * a)) / n
    qp = float(_q(math.pi / 2, e, e2))
    beta = np.arcsin(np.clip(q / qp, -1.0, 1.0))
    return theta / n, _from_authalic(beta, e2)


# ---------------------------------------------------------------- Lambert 方位等積


def _laea_constants(crs: Crs) -> tuple[float, float, float, float]:
    a, e, e2 = crs.ellipsoid.a, crs.ellipsoid.e, crs.ellipsoid.e2
    phi0 = math.radians(crs.lat0)
    qp = float(_q(math.pi / 2, e, e2))
    beta0 = math.asin(float(_q(phi0, e, e2)) / qp)
    rq = a * math.sqrt(qp / 2)
    d = a * _m(phi0, e2) / (rq * math.cos(beta0))
    return qp, beta0, rq, d


def _laea_forward(crs: Crs, dlon: np.ndarray, phi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    e, e2 = crs.ellipsoid.e, crs.ellipsoid.e2
    qp, beta0, rq, d = _laea_constants(crs)
    beta = np.arcsin(_q(phi, e, e2) / qp)
    big_b = rq * np.sqrt(
        2 / (1 + math.sin(beta0) * np.sin(beta) + math.cos(beta0) * np.cos(beta) * np.cos(dlon))
    )
    x = big_b * d * np.cos(beta) * np.sin(dlon)
    y = (big_b / d) * (
        math.cos(beta0) * np.sin(beta) - math.sin(beta0) * np.cos(beta) * np.cos(dlon)
    )
    return x, y


def _laea_inverse(crs: Crs, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    e2 = crs.ellipsoid.e2
    _, beta0, rq, d = _laea_constants(crs)
    rho = np.sqrt((x / d) ** 2 + (d * y) ** 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        c = 2 * np.arcsin(np.clip(rho / (2 * rq), -1.0, 1.0))
        beta = np.where(
            rho > 0,
            np.arcsin(
                np.clip(
                    np.cos(c) * math.sin(beta0) + d * y * np.sin(c) * math.cos(beta0) / rho,
                    -1.0,
                    1.0,
                )
            ),
            beta0,
        )
        lam = np.arctan2(
            x * np.sin(c),
            d * rho * math.cos(beta0) * np.cos(c) - d * d * y * math.sin(beta0) * np.sin(c),
        )
    return lam, _from_authalic(beta, e2)


_FORWARD = {
    "tm": _tm_forward,
    "mercator": _mercator_forward,
    "web_mercator": _web_forward,
    "lcc": _lcc_forward,
    "albers": _albers_forward,
    "laea": _laea_forward,
}
_INVERSE = {
    "tm": _tm_inverse,
    "mercator": _mercator_inverse,
    "web_mercator": _web_inverse,
    "lcc": _lcc_inverse,
    "albers": _albers_inverse,
    "laea": _laea_inverse,
}


# ---------------------------------------------------------------- .prj（WKT）


@dataclass
class _Node:
    name: str
    args: list[Any]

    def children(self, name: str) -> list[_Node]:
        return [a for a in self.args if isinstance(a, _Node) and a.name.upper() == name]

    def first(self, name: str) -> _Node | None:
        found = self.children(name)
        return found[0] if found else None


_TOKENS = re.compile(
    r'\s*(?:"((?:[^"]|"")*)"|([A-Za-z_][A-Za-z0-9_]*)|([-+]?[0-9.]+(?:[eE][-+]?\d+)?)|([\[\]\(\),]))'
)


def _parse_wkt(text: str) -> _Node:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        found = _TOKENS.match(text, pos)
        if found is None:
            if text[pos:].strip():
                raise ProjectionError(f".prj 的第 {pos + 1} 個字元無法解析")
            break
        quoted, word, number, punct = found.groups()
        if quoted is not None:
            tokens.append(("str", quoted.replace('""', '"')))
        elif word is not None:
            tokens.append(("word", word))
        elif number is not None:
            tokens.append(("num", number))
        elif punct is not None:
            tokens.append(("punct", punct))
        pos = found.end()
    index = 0

    def node() -> _Node:
        nonlocal index
        kind, name = tokens[index]
        if kind != "word":
            raise ProjectionError(".prj 的結構不正確")
        index += 1
        args: list[Any] = []
        if index < len(tokens) and tokens[index] in (("punct", "["), ("punct", "(")):
            index += 1
            while index < len(tokens) and tokens[index] not in (("punct", "]"), ("punct", ")")):
                kind, value = tokens[index]
                if kind == "punct" and value == ",":
                    index += 1
                    continue
                if kind == "word" and index + 1 < len(tokens) and tokens[index + 1][1] in "[(":
                    args.append(node())
                    continue
                args.append(float(value) if kind == "num" else value)
                index += 1
            index += 1
        return _Node(name, args)

    if not tokens:
        raise ProjectionError(".prj 是空的")
    return node()


ELLIPSOIDS = {
    "wgs_1984": WGS84,
    "wgs 84": WGS84,
    "grs_1980": GRS80,
    "grs 1980": GRS80,
    "grs_1967": GRS67,
    "airy_1830": AIRY,
    "bessel_1841": BESSEL,
    "international_1924": INTL,
    "clarke_1866": CLARKE66,
    "krasovsky_1940": KRASSOWSKY,
}

PROJECTIONS = {
    "transverse_mercator": "tm",
    "transverse mercator": "tm",
    "gauss_kruger": "tm",
    "gauss-kruger": "tm",
    "mercator": "mercator",
    "mercator_1sp": "mercator",
    "mercator_2sp": "mercator",
    "mercator (variant a)": "mercator",
    "mercator (variant b)": "mercator",
    "mercator_auxiliary_sphere": "web_mercator",
    "popular_visualisation_pseudo_mercator": "web_mercator",
    "popular visualisation pseudo mercator": "web_mercator",
    "lambert_conformal_conic": "lcc",
    "lambert_conformal_conic_1sp": "lcc",
    "lambert_conformal_conic_2sp": "lcc",
    "lambert conic conformal (1sp)": "lcc",
    "lambert conic conformal (2sp)": "lcc",
    "albers": "albers",
    "albers_conic_equal_area": "albers",
    "albers equal area": "albers",
    "lambert_azimuthal_equal_area": "laea",
    "lambert azimuthal equal area": "laea",
}


def _number(node: _Node | None, index: int = 0) -> float | None:
    if node is None:
        return None
    numbers = [a for a in node.args if isinstance(a, float)]
    return numbers[index] if len(numbers) > index else None


def crs_from_prj(text: str) -> Crs:
    """.prj 的 WKT（ESRI 或 OGC 的寫法）→ 座標系統；上面的公式不支援的投影以 PROJ 計算，PROJ 也認不得時丟出
    ProjectionError。"""
    cleaned = text.strip().lstrip("\ufeff")
    try:
        return replace(_crs_from_prj(cleaned), source=cleaned)
    except ProjectionError:
        try:
            return _proj_crs(cleaned)
        except ProjectionError:
            pass
        raise


def crs_from_user(text: str) -> Crs:
    """座標系統的文字（EPSG:代碼、.prj 或 GDAL 的 WKT）→ 座標系統。"""
    stripped = text.strip()
    match = re.fullmatch(r"(?i)epsg:(\d+)", stripped)
    if match:
        return crs_from_epsg(int(match.group(1)))
    return crs_from_prj(stripped)


def _crs_from_prj(text: str) -> Crs:
    root = _parse_wkt(text)
    kind = root.name.upper()
    if kind in ("GEOGCS", "GEOGCRS", "GEODCRS"):
        return _geographic(root)
    if kind not in ("PROJCS", "PROJCRS"):
        raise ProjectionError(f".prj 的座標系統 {root.name} 尚未支援")
    geog = root.first("GEOGCS") or root.first("BASEGEOGCRS") or root.first("BASEGEODCRS")
    base = _geographic(geog) if geog is not None else Crs("geographic")
    projection = root.first("PROJECTION") or root.first("METHOD")
    method = str(projection.args[0]).lower() if projection is not None and projection.args else ""
    found = PROJECTIONS.get(method)
    params: dict[str, float] = {}
    for p in root.children("PARAMETER"):
        if p.args and isinstance(p.args[0], str):
            value = _number(p)
            if value is not None:
                params[p.args[0].lower().replace(" ", "_")] = value
    if found is None:
        raise ProjectionError(f"投影「{method or '未知'}」尚未支援")
    if found == "mercator" and params.get("auxiliary_sphere_type") is not None:
        found = "web_mercator"
    units = root.children("UNIT")
    unit = _number(units[-1], 0) if units else None

    def param(*names: str) -> float | None:
        return next((params[n] for n in names if n in params), None)

    sp1 = param("standard_parallel_1", "latitude_of_1st_standard_parallel")
    sp2 = param("standard_parallel_2", "latitude_of_2nd_standard_parallel")
    lat0 = param(
        "latitude_of_origin",
        "latitude_of_center",
        "latitude_of_false_origin",
        "latitude_of_natural_origin",
    )
    scale = unit or 1.0
    return Crs(
        kind=found,
        ellipsoid=WGS84 if found == "web_mercator" else base.ellipsoid,
        lat0=lat0 or 0.0,
        lon0=param(
            "central_meridian",
            "longitude_of_center",
            "longitude_of_origin",
            "longitude_of_false_origin",
            "longitude_of_natural_origin",
        )
        or 0.0,
        k0=param("scale_factor", "scale_factor_at_natural_origin") or 1.0,
        # 假東距、假北距寫在投影的單位（例如美國測量英尺），換成公尺
        fe=(param("false_easting", "easting_at_false_origin") or 0.0) * scale,
        fn=(param("false_northing", "northing_at_false_origin") or 0.0) * scale,
        sp1=sp1,
        sp2=sp2,
        unit=scale,
        prime_meridian=base.prime_meridian,
        name=str(root.args[0]) if root.args else "",
    )


def _geographic(node: _Node) -> Crs:
    datum = node.first("DATUM")
    spheroid = (datum.first("SPHEROID") or datum.first("ELLIPSOID")) if datum is not None else None
    ellipsoid = WGS84
    if spheroid is not None:
        a, inv_f = _number(spheroid, 0), _number(spheroid, 1)
        if a is not None and inv_f is not None:
            ellipsoid = Ellipsoid(a, inv_f)
        elif spheroid.args:
            ellipsoid = ELLIPSOIDS.get(str(spheroid.args[0]).lower(), WGS84)
    primem = _number(node.first("PRIMEM")) or 0.0
    unit_node = node.first("UNIT") or node.first("ANGLEUNIT")
    radians = _number(unit_node)
    unit = math.degrees(radians) if radians else 1.0
    return Crs(
        "geographic",
        ellipsoid=ellipsoid,
        unit=unit,
        prime_meridian=primem,
        name=str(node.args[0]) if node.args else "",
    )


# ---------------------------------------------------------------- EPSG 代碼（MAKEPOINT 的 SRID）

_GEOGRAPHIC = (4326, 4269, 4258, 4283, 4167, 3824, 4612, 6668, 4490, 4617, 4171, 4674, 7844, 4019)


def _utm(zone: int, south: bool, ellipsoid: Ellipsoid) -> Crs:
    return Crs(
        "tm",
        ellipsoid=ellipsoid,
        lon0=zone * 6 - 183,
        k0=0.9996,
        fe=500000.0,
        fn=10000000.0 if south else 0.0,
    )


_JAPAN_ORIGINS = [
    (33.0, 129.5),
    (33.0, 131.0),
    (36.0, 132.0 + 1 / 6),
    (33.0, 133.5),
    (36.0, 134.0 + 1 / 3),
    (36.0, 136.0),
    (36.0, 137.0 + 1 / 6),
    (36.0, 138.5),
    (36.0, 139.0 + 5 / 6),
    (40.0, 140.0 + 5 / 6),
    (44.0, 140.25),
    (44.0, 142.25),
    (44.0, 144.25),
    (26.0, 142.0),
    (26.0, 127.5),
    (26.0, 124.0),
    (26.0, 131.0),
    (20.0, 136.0),
    (26.0, 154.0),
]
"""日本平面直角座標系 I–XIX 的原點（緯度, 經度）。"""


@lru_cache(maxsize=256)
def crs_from_epsg(code: int) -> Crs:
    """EPSG 代碼 → 座標系統：常用的地理座標系統、Web Mercator、UTM、TWD97 二度分帶等以上面的公式計算，
    其他代碼以 PROJ 計算（PROJ 也認不得時丟出 ProjectionError）。"""
    try:
        found = _crs_from_epsg(code)
    except ProjectionError:
        return _proj_crs(f"EPSG:{code}")
    return replace(found, source=f"EPSG:{code}")


def _crs_from_epsg(code: int) -> Crs:
    if code in _GEOGRAPHIC:
        return Crs("geographic", ellipsoid=GRS80 if code != 4326 else WGS84)
    if code in (3857, 900913, 102100, 102113, 3785):
        return Crs("web_mercator")
    if 32601 <= code <= 32660:
        return _utm(code - 32600, False, WGS84)
    if 32701 <= code <= 32760:
        return _utm(code - 32700, True, WGS84)
    if 26901 <= code <= 26923:
        return _utm(code - 26900, False, GRS80)
    if 25828 <= code <= 25838:
        return _utm(code - 25800, False, GRS80)
    if 6669 <= code <= 6687 or 2443 <= code <= 2461:
        lat0, lon0 = _JAPAN_ORIGINS[code - (6669 if code >= 6669 else 2443)]
        return Crs("tm", ellipsoid=GRS80, lat0=lat0, lon0=lon0, k0=0.9999)
    table: dict[int, Crs] = {
        # 臺灣：TWD97 / TM2（121、119 度分帶）；TWD67 為 GRS67 橢球（基準面以 PROJ 轉換）
        3826: Crs("tm", ellipsoid=GRS80, lon0=121.0, k0=0.9999, fe=250000.0),
        3825: Crs("tm", ellipsoid=GRS80, lon0=119.0, k0=0.9999, fe=250000.0),
        3828: Crs("tm", ellipsoid=GRS67, lon0=121.0, k0=0.9999, fe=250000.0),
        3827: Crs("tm", ellipsoid=GRS67, lon0=119.0, k0=0.9999, fe=250000.0),
        27700: Crs(
            "tm",
            ellipsoid=AIRY,
            lat0=49.0,
            lon0=-2.0,
            k0=0.9996012717,
            fe=400000.0,
            fn=-100000.0,
        ),
        2154: Crs(
            "lcc",
            ellipsoid=GRS80,
            lat0=46.5,
            lon0=3.0,
            sp1=49.0,
            sp2=44.0,
            fe=700000.0,
            fn=6600000.0,
        ),
        3035: Crs("laea", ellipsoid=GRS80, lat0=52.0, lon0=10.0, fe=4321000.0, fn=3210000.0),
        5070: Crs("albers", ellipsoid=GRS80, lat0=23.0, lon0=-96.0, sp1=29.5, sp2=45.5),
        3310: Crs("albers", ellipsoid=GRS80, lon0=-120.0, sp1=34.0, sp2=40.5, fn=-4000000.0),
        3493: Crs("albers", ellipsoid=GRS80, lon0=-120.0, sp1=34.0, sp2=40.5, fn=-4000000.0),
        3577: Crs("albers", ellipsoid=GRS80, lon0=132.0, sp1=-18.0, sp2=-36.0),
        3112: Crs("lcc", ellipsoid=GRS80, lon0=134.0, sp1=-18.0, sp2=-36.0),
        2193: Crs("tm", ellipsoid=GRS80, lon0=173.0, k0=0.9996, fe=1600000.0, fn=10000000.0),
        3067: Crs("tm", ellipsoid=GRS80, lon0=27.0, k0=0.9996, fe=500000.0),
        31467: Crs("tm", ellipsoid=BESSEL, lon0=9.0, fe=3500000.0),
        31468: Crs("tm", ellipsoid=BESSEL, lon0=12.0, fe=4500000.0),
        3414: Crs(
            "tm",
            ellipsoid=WGS84,
            lat0=1 + 22 / 60,
            lon0=103 + 50 / 60,
            fe=28001.642,
            fn=38744.572,
        ),
        5179: Crs(
            "tm", ellipsoid=GRS80, lat0=38.0, lon0=127.5, k0=0.9996, fe=1000000.0, fn=2000000.0
        ),
        5185: Crs("tm", ellipsoid=GRS80, lat0=38.0, lon0=125.0, fe=200000.0, fn=600000.0),
        5186: Crs("tm", ellipsoid=GRS80, lat0=38.0, lon0=127.0, fe=200000.0, fn=600000.0),
        5187: Crs("tm", ellipsoid=GRS80, lat0=38.0, lon0=129.0, fe=200000.0, fn=600000.0),
        5188: Crs("tm", ellipsoid=GRS80, lat0=38.0, lon0=131.0, fe=200000.0, fn=600000.0),
    }
    found = table.get(code)
    if found is None:
        raise ProjectionError(f"SRID {code} 尚未支援")
    return found


def make_point_srid(x: float | None, y: float | None, srid: int | None) -> str | None:
    """MAKEPOINT(X, Y, SRID)：投影座標換成 WGS84 的點（WKT）；不支援的 SRID 為 Null。"""
    from tmig.geo.spatial import make_point

    if x is None or y is None or srid is None:
        return None
    try:
        crs = crs_from_epsg(int(srid))
        lon, lat = crs.to_wgs84(np.array([x]), np.array([y]))
    except (ProjectionError, OverflowError):
        return None
    if not (math.isfinite(float(lon[0])) and math.isfinite(float(lat[0]))):
        return None
    return make_point(float(lat[0]), float(lon[0]))


__all__ = [
    "Crs",
    "ProjectionError",
    "crs_from_epsg",
    "crs_from_prj",
    "crs_from_user",
    "make_point_srid",
]
