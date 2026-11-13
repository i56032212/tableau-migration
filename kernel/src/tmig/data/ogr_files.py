"""Esri File Geodatabase（.gdb 資料夾，或壓縮成 .gdb.zip）與 MapInfo（.tab 與 .dat、.map、.id，或 .mif 與 .mid）：
以 GDAL（pyogrio）讀出圖層的屬性與形狀（WKB）。座標系統依檔案記下的（EPSG 代碼或 WKT），由呼叫端換成 WGS84 的
經緯度（tmig.geo.projection，含基準面之間的轉換），與 Tableau 相同「不論座標系統，一律轉換成 WGS84」。

- 資料表是圖層（File Geodatabase 的 feature class；MapInfo 一個檔案一張）；欄位依檔案中的順序
- 形狀的 Z、M 值略過；只有一個部分的 MULTI*（File Geodatabase 的多邊形一律是 MultiPolygon）寫成單一的形狀，
  與 Shapefile 相同；曲線（圓弧等）尚未支援
"""

from __future__ import annotations

import datetime as dt
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from tmig.geo.spatial import Geometry

GDB_SUFFIX = ".gdb"
MAPINFO_TAB = (".tab", ".dat", ".map", ".id", ".ind")
"""MapInfo 表格（.tab）與它的同名檔案。"""
MAPINFO_MIF = (".mif", ".mid")
"""MapInfo 交換格式（.mif）與它的資料檔（.mid）。"""
WKB_TYPES = {
    1: "Point",
    2: "LineString",
    3: "Polygon",
    4: "MultiPoint",
    5: "MultiLineString",
    6: "MultiPolygon",
    7: "GeometryCollection",
}


class OgrError(ValueError):
    pass


@dataclass
class OgrLayer:
    """讀出的圖層：欄名（檔案中的順序）、各列的屬性、座標系統（EPSG:代碼或 WKT；沒有時為 None）與形狀。"""

    name: str
    fields: list[str]
    rows: list[dict[str, Any]]
    crs: str | None
    shapes: list[Geometry | None]


def is_ogr_path(path: str) -> bool:
    """以 GDAL 讀的空間檔案：.gdb 資料夾、.tab、.mif。"""
    lowered = path.lower().rstrip("/")
    return lowered.endswith((GDB_SUFFIX, ".tab", ".mif"))


def companion_paths(names: list[str], path: str) -> list[str]:
    """.twbx 內與空間檔案一起讀的檔案：.gdb 資料夾中的檔案，MapInfo 的同名檔案（不分大小寫）。"""
    lowered = path.lower().rstrip("/")
    if lowered.endswith(GDB_SUFFIX):
        prefix = lowered + "/"
        return [n for n in names if n.lower().startswith(prefix)]
    suffix = PurePosixPath(lowered).suffix
    group = MAPINFO_TAB if suffix == ".tab" else MAPINFO_MIF if suffix == ".mif" else None
    if group is None:
        return [path]
    stem = lowered[: -len(suffix)]
    found = [
        n
        for n in names
        if PurePosixPath(n).suffix.lower() in group
        and n.lower()[: -len(PurePosixPath(n).suffix)] == stem
    ]
    return found or [path]


def gdb_in_zip(names: list[str]) -> list[str]:
    """壓縮檔中的 File Geodatabase（.gdb 資料夾的路徑）。"""
    found: list[str] = []
    for name in names:
        parts = name.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower().endswith(GDB_SUFFIX):
                folder = "/".join(parts[: i + 1])
                if folder not in found:
                    found.append(folder)
                break
    return found


# ---------------------------------------------------------------- WKB


class _Wkb:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _uint(self, order: str) -> int:
        (value,) = struct.unpack_from(order + "I", self.data, self.pos)
        self.pos += 4
        return int(value)

    def geometry(self) -> Geometry:
        order = "<" if self.data[self.pos] == 1 else ">"
        self.pos += 1
        code = self._uint(order)
        dims = 2 + (1 if code & 0x80000000 else 0) + (1 if code & 0x40000000 else 0)
        if code & 0x20000000:
            self.pos += 4  # EWKB 的 SRID
        code &= 0x0FFFFFFF
        if code >= 3000:
            dims, code = 4, code - 3000
        elif code >= 1000:
            dims, code = 3, code % 1000
        kind = WKB_TYPES.get(code)
        if kind is None:
            raise OgrError(f"形狀的類型（WKB {code}，例如圓弧等曲線）尚未支援")
        if kind == "Point":
            values = struct.unpack_from(order + "d" * dims, self.data, self.pos)
            self.pos += 8 * dims
            if values[0] != values[0] or values[1] != values[1]:
                return {"type": "Point", "coordinates": []}  # 空的點（POINT EMPTY）
            return {"type": "Point", "coordinates": [values[0], values[1]]}
        if kind == "LineString":
            return {"type": kind, "coordinates": self._points(order, dims)}
        if kind == "Polygon":
            count = self._uint(order)
            return {"type": kind, "coordinates": [self._points(order, dims) for _ in range(count)]}
        count = self._uint(order)
        parts = [self.geometry() for _ in range(count)]
        if kind == "GeometryCollection":
            return {"type": kind, "geometries": parts}
        return {"type": kind, "coordinates": [p["coordinates"] for p in parts if p["coordinates"]]}

    def _points(self, order: str, dims: int) -> list[list[float]]:
        count = self._uint(order)
        values = np.frombuffer(self.data, dtype=order + "f8", count=count * dims, offset=self.pos)
        self.pos += 8 * count * dims
        return values.reshape(count, dims)[:, :2].tolist()


def parse_wkb(data: bytes | None) -> Geometry | None:
    """WKB（ISO 與 EWKB，Z、M 值略過）→ GeoJSON 的 geometry；只有一個部分的 MULTI* 寫成單一的形狀。"""
    if data is None or len(data) == 0:
        return None
    try:
        geom = _Wkb(bytes(data)).geometry()
    except OgrError:
        raise
    except (struct.error, IndexError, ValueError) as exc:
        raise OgrError(f"形狀無法解析：{exc}") from exc
    return _single(geom)


def _single(geom: Geometry) -> Geometry:
    single = {"MultiPoint": "Point", "MultiLineString": "LineString", "MultiPolygon": "Polygon"}
    kind = geom["type"]
    coords = geom.get("coordinates") or []
    if kind in single and len(coords) == 1:
        return {"type": single[kind], "coordinates": coords[0]}
    return geom


# ---------------------------------------------------------------- 讀取


def _materialize(files: dict[str, bytes], root: Path) -> None:
    for name, data in files.items():
        target = root.joinpath(*PurePosixPath(name).parts)
        if not target.resolve().is_relative_to(root.resolve()):
            raise OgrError(f"不正確的檔案路徑：{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _location(root: Path, path: str) -> str:
    """GDAL 讀取的位置：.zip 中的 .gdb 以 /vsizip/ 讀。"""
    source = root.joinpath(*PurePosixPath(path).parts)
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(source) as archive:
            inner = gdb_in_zip(archive.namelist())
        if not inner:
            raise OgrError(f"{PurePosixPath(path).name} 內沒有 File Geodatabase（.gdb 資料夾）")
        return f"/vsizip/{source.as_posix()}/{inner[0]}"
    return str(source)


def ogr_layers(files: dict[str, bytes], path: str) -> list[str]:
    """檔案中的資料表（圖層）名稱。"""
    import pyogrio

    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        _materialize(files, root)
        try:
            return [str(row[0]) for row in pyogrio.list_layers(_location(root, path))]
        except OgrError:
            raise
        except Exception as exc:
            raise OgrError(f"{PurePosixPath(path).name} 無法讀取：{exc}") from exc


def read_ogr(files: dict[str, bytes], path: str, layer: str | None = None) -> OgrLayer:
    """files：.twbx 內的檔案（路徑 → 內容，含 .gdb 資料夾中的檔案、MapInfo 的同名檔案）；path：.gdb 資料夾、
    含 .gdb 的 .zip、.tab 或 .mif；layer：圖層（省略時為第一個；只有一個圖層時名稱不同也用它）。"""
    try:
        import pyogrio
        from pyogrio import raw
    except ImportError as exc:  # pragma: no cover - 依安裝的套件
        raise OgrError("讀取 File Geodatabase、MapInfo 需要 pyogrio（uv sync）") from exc
    label = PurePosixPath(path.rstrip("/")).name
    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        _materialize(files, root)
        location = _location(root, path)
        try:
            layers = [str(row[0]) for row in pyogrio.list_layers(location)]
        except Exception as exc:
            raise OgrError(f"{label} 無法讀取：{exc}") from exc
        if not layers:
            raise OgrError(f"{label} 中沒有資料表")
        chosen = layers[0]
        if layer:
            wanted = (
                PurePosixPath(layer).stem.lower()
                if layer.lower().endswith((".tab", ".mif"))
                else layer.lower()
            )
            match = next((n for n in layers if n.lower() in (layer.lower(), wanted)), None)
            if match is None and len(layers) > 1:
                raise OgrError(f"{label} 中沒有資料表「{layer}」（有：{'、'.join(layers)}）")
            chosen = match or layers[0]
        try:
            meta, _, geometry, data = raw.read(location, layer=chosen)
        except Exception as exc:
            raise OgrError(f"{label}（{chosen}）無法讀取：{exc}") from exc
    fields = [str(f) for f in meta["fields"]]
    dtypes = [str(t) for t in meta.get("dtypes", [])]
    count = len(geometry) if geometry is not None else (len(data[0]) if len(data) else 0)
    rows = [
        {
            name: _value(data[j][i], dtypes[j] if j < len(dtypes) else "")
            for j, name in enumerate(fields)
        }
        for i in range(count)
    ]
    shapes: list[Geometry | None] = (
        [parse_wkb(g) for g in geometry] if geometry is not None else [None] * count
    )
    crs = meta.get("crs")
    return OgrLayer(chosen, fields, rows, str(crs) if crs else None, shapes)


def _value(value: Any, dtype: str) -> Any:
    """GDAL 的值 → Python 的值：日期為 date、日期時間為 datetime、NaN 為 None。"""
    if value is None:
        return None
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            return None
        moment = value.astype("datetime64[us]").item()
        if isinstance(moment, dt.datetime) and dtype.startswith("datetime64[D"):
            return moment.date()
        return moment
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, bytes):
        return value.hex()
    return value


__all__ = [
    "GDB_SUFFIX",
    "MAPINFO_MIF",
    "MAPINFO_TAB",
    "OgrError",
    "OgrLayer",
    "companion_paths",
    "gdb_in_zip",
    "is_ogr_path",
    "ogr_layers",
    "parse_wkb",
    "read_ogr",
]
