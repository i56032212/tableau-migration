"""空間檔案（Tableau 的 ogrdirect 連線）：讀成資料列（屬性欄位與 Geometry 的 WKT）。

- Esri Shapefile：.shp 與同名的 .dbf、.prj、.cpg（可壓縮成 .zip，Tableau 連線到 .zip 時讀其中的
  Shapefile）。座標依 .prj 換成 WGS84 的經緯度（tmig.geo.projection），沒有 .prj 時視為經緯度；
  多邊形的各環依包含關係分成外環與內環（與環的方向無關）；屬性的文字依 .cpg（沒有時依 .dbf 的
  語言代碼，再沒有時先試 UTF-8）解碼；Z、M 值略過
- GeoJSON（FeatureCollection、Feature 或單一 geometry；舊版的 crs 名稱可指定 EPSG 代碼）
- KML（各層 Folder 中的 Placemark：Name、Description 與 ExtendedData 的欄位）
- TopoJSON（資料表是 objects 中的物件）

形狀都依 Hyper 讀入 WKT 時的處理切分直線邊、調整環的方向（tmig.geo.spatial.normalize），與 Tableau 的萃取相同。
Esri File Geodatabase、MapInfo 尚未支援。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import struct
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import numpy as np

from tmig.geo.projection import Crs, ProjectionError, crs_from_epsg, crs_from_prj
from tmig.geo.spatial import Geometry, format_wkt, normalize

GEOMETRY_COLUMN = "Geometry"
"""Tableau 給空間檔案的形狀欄位的名稱。"""
SHAPEFILE_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")

DBF_CODEPAGES = {
    0x01: "cp437",
    0x02: "cp850",
    0x03: "cp1252",
    0x57: "cp1252",
    0x13: "cp932",
    0x4D: "cp936",
    0x4E: "cp949",
    0x4F: "cp950",
    0x78: "cp950",
    0x79: "cp949",
    0x7A: "cp936",
    0x7B: "cp932",
    0x7C: "cp874",
    0x7D: "cp1255",
    0x7E: "cp1256",
    0xC8: "cp1250",
    0xC9: "cp1251",
    0xCA: "cp1254",
    0xCB: "cp1253",
}
"""dBase 的語言代碼（.dbf 第 29 個位元組）→ 字碼頁。"""
CPG_ALIASES = {"ansi 1252": "cp1252", "big5": "cp950", "gbk": "cp936", "gb2312": "cp936"}


class SpatialFileError(ValueError):
    pass


@dataclass
class SpatialTable:
    """讀出的資料：欄名（檔案中的順序，最後一欄是 Geometry）與各列（欄名 → 值）。"""

    columns: list[str]
    rows: list[dict[str, Any]]


# ---------------------------------------------------------------- 入口


def read_spatial(files: dict[str, bytes], path: str, layer: str | None = None) -> SpatialTable:
    """files：.twbx 內的檔案（路徑 → 內容，至少含 path 與 Shapefile 的同名檔案）；layer：資料表名稱
    （壓縮檔中的 Shapefile、TopoJSON 的物件）。"""
    suffix = PurePosixPath(path).suffix.lower()
    data = files.get(path)
    if data is None:
        raise SpatialFileError(f"找不到空間檔案 {path}")
    if suffix == ".zip":
        return _read_zip(data, layer, path)
    if suffix == ".shp":
        stem = path[: -len(suffix)]
        parts = {
            ext: files.get(stem + ext) or files.get(stem + ext.upper()) for ext in SHAPEFILE_PARTS
        }
        return read_shapefile(parts, path)
    if suffix in (".geojson", ".json"):
        document = _json(data, path)
        if isinstance(document, dict) and document.get("type") == "Topology":
            return read_topojson(document, layer, path)
        return read_geojson(document, path)
    if suffix == ".topojson":
        return read_topojson(_json(data, path), layer, path)
    if suffix == ".kml":
        return read_kml(data, path)
    if suffix == ".kmz":
        with zipfile.ZipFile(io.BytesIO(data)) as kmz:
            inner = next((n for n in kmz.namelist() if n.lower().endswith(".kml")), None)
            if inner is None:
                raise SpatialFileError(f"{path} 內沒有 .kml")
            return read_kml(kmz.read(inner), path)
    if suffix in (".tab", ".mif", ".gdb") or path.lower().endswith(".gdb.zip"):
        raise SpatialFileError(
            f"{PurePosixPath(path).name}：MapInfo、Esri File Geodatabase 尚未支援"
        )
    raise SpatialFileError(f"{PurePosixPath(path).name}：不認得的空間檔案格式")


def sidecar_paths(names: list[str], path: str) -> list[str]:
    """.twbx 內與 Shapefile 同名的檔案（.shx、.dbf、.prj、.cpg）。"""
    if not path.lower().endswith(".shp"):
        return [path]
    stem = path[:-4].lower()
    return [n for n in names if n.lower()[:-4] == stem and n.lower()[-4:] in SHAPEFILE_PARTS]


def _read_zip(data: bytes, layer: str | None, path: str) -> SpatialTable:
    with zipfile.ZipFile(io.BytesIO(data)) as inner:
        names = [n for n in inner.namelist() if not n.endswith("/")]
        if any(".gdb/" in n.lower() for n in names):
            raise SpatialFileError(f"{PurePosixPath(path).name}：Esri File Geodatabase 尚未支援")
        shapes = [n for n in names if n.lower().endswith(".shp")]
        if shapes:
            wanted = (layer or "").lower()
            chosen = next((n for n in shapes if PurePosixPath(n).stem.lower() == wanted), shapes[0])
            stem = chosen[:-4]
            parts: dict[str, bytes | None] = {}
            lowered = {n.lower(): n for n in names}
            for ext in SHAPEFILE_PARTS:
                found = lowered.get((stem + ext).lower())
                parts[ext] = inner.read(found) if found else None
            return read_shapefile(parts, chosen)
        others = [
            n
            for n in names
            if PurePosixPath(n).suffix.lower() in (".geojson", ".json", ".kml", ".topojson")
        ]
        if others:
            return read_spatial({others[0]: inner.read(others[0])}, others[0], layer)
    raise SpatialFileError(f"{PurePosixPath(path).name} 內沒有 Shapefile 或其他空間檔案")


def _json(data: bytes, path: str) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SpatialFileError(f"{PurePosixPath(path).name} 不是可讀取的 JSON：{exc}") from exc


def _wkt(geom: Geometry | None) -> str | None:
    normalized = normalize(geom)
    if normalized is None:
        return None
    return format_wkt(normalized)


# ---------------------------------------------------------------- Shapefile


def read_shapefile(parts: dict[str, bytes | None], path: str) -> SpatialTable:
    shp = parts.get(".shp")
    if shp is None:
        raise SpatialFileError(f"找不到 {path}")
    crs: Crs | None = None
    prj = parts.get(".prj")
    if prj:
        try:
            crs = crs_from_prj(prj.decode("latin-1"))
        except ProjectionError as exc:
            raise SpatialFileError(f"{PurePosixPath(path).name} 的座標系統：{exc}") from exc
    shapes = _shp_records(shp, path)
    dbf = parts.get(".dbf")
    names: list[str] = []
    records: list[dict[str, Any]] = []
    if dbf:
        cpg = parts.get(".cpg")
        encoding = cpg.decode("latin-1").strip() if cpg else None
        names, records = _dbf(dbf, encoding, path)
    rows: list[dict[str, Any]] = []
    for i, shape in enumerate(shapes):
        row = dict(records[i]) if i < len(records) else dict.fromkeys(names)
        geom = _project(shape, crs) if shape is not None and crs is not None else shape
        row[GEOMETRY_COLUMN] = _wkt(geom)
        rows.append(row)
    return SpatialTable([*names, GEOMETRY_COLUMN], rows)


def _shp_records(data: bytes, path: str) -> list[Geometry | None]:
    if len(data) < 100 or struct.unpack(">i", data[:4])[0] != 9994:
        raise SpatialFileError(f"{PurePosixPath(path).name} 不是 Shapefile")
    found: list[Geometry | None] = []
    offset = 100
    size = len(data)
    while offset + 8 <= size:
        _, length = struct.unpack(">ii", data[offset : offset + 8])
        content = data[offset + 8 : offset + 8 + length * 2]
        offset += 8 + length * 2
        if len(content) < 4:
            break
        found.append(_shape(content, path))
    return found


def _shape(content: bytes, path: str) -> Geometry | None:
    kind = struct.unpack("<i", content[:4])[0]
    if kind == 0:
        return None
    if kind in (1, 11, 21):
        x, y = struct.unpack("<2d", content[4:20])
        return {"type": "Point", "coordinates": [x, y]}
    if kind in (8, 18, 28):
        (count,) = struct.unpack("<i", content[36:40])
        points = np.frombuffer(content, dtype="<f8", count=2 * count, offset=40).reshape(-1, 2)
        return {"type": "MultiPoint", "coordinates": points.tolist()}
    if kind in (3, 13, 23, 5, 15, 25):
        num_parts, num_points = struct.unpack("<2i", content[36:44])
        starts = list(struct.unpack(f"<{num_parts}i", content[44 : 44 + 4 * num_parts]))
        at = 44 + 4 * num_parts
        points = np.frombuffer(content, dtype="<f8", count=2 * num_points, offset=at).reshape(-1, 2)
        bounds = [*starts, num_points]
        pieces = [points[bounds[k] : bounds[k + 1]].tolist() for k in range(num_parts)]
        pieces = [p for p in pieces if p]
        if kind in (3, 13, 23):
            if len(pieces) == 1:
                return {"type": "LineString", "coordinates": pieces[0]}
            return {"type": "MultiLineString", "coordinates": pieces}
        return _assemble(pieces)
    raise SpatialFileError(f"{PurePosixPath(path).name} 的形狀類型 {kind}（MultiPatch 等）尚未支援")


def _ring_area(ring: list[list[float]]) -> float:
    arr = np.asarray(ring, dtype=float)
    x, y = arr[:, 0], arr[:, 1]
    return float(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1])) / 2


def _contains(ring: list[list[float]], points: list[list[float]]) -> bool:
    """ring 是否包含 points（另一個環上的幾個頂點，多數決：頂點可能剛好在 ring 的邊上）。"""
    arr = np.asarray(ring, dtype=float)
    x1, y1, x2, y2 = arr[:-1, 0], arr[:-1, 1], arr[1:, 0], arr[1:, 1]
    votes = 0
    for px, py in points:
        crossing = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xs = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        votes += 1 if int(np.count_nonzero(crossing & (px < xs))) % 2 == 1 else -1
    return votes > 0


def _probes(ring: list[list[float]]) -> list[list[float]]:
    n = len(ring) - 1
    return [ring[0], ring[max(n // 3, 0)], ring[max(2 * n // 3, 0)]]


def _assemble(rings: list[list[list[float]]]) -> Geometry | None:
    """多邊形的各環依包含關係分成外環與內環（與環的方向無關）：由大到小，被偶數層包住的是外環，
    奇數層的是包住它的外環的內環。"""
    closed = [r if r[0] == r[-1] else [*r, r[0]] for r in rings if len(r) >= 3]
    if not closed:
        return None
    order = sorted(range(len(closed)), key=lambda i: abs(_ring_area(closed[i])), reverse=True)
    depth: dict[int, int] = {}
    owner_of: dict[int, int] = {}
    placed: list[int] = []
    for i in order:
        probes = _probes(closed[i])
        owner = next((j for j in reversed(placed) if _contains(closed[j], probes)), None)
        depth[i] = 0 if owner is None else depth[owner] + 1
        if owner is not None:
            owner_of[i] = owner
        placed.append(i)
    polygons: dict[int, list[list[list[float]]]] = {
        i: [closed[i]] for i in order if depth[i] % 2 == 0
    }
    for i in order:
        if depth[i] % 2 == 1:
            polygons[owner_of[i]].append(closed[i])
    found = [polygons[i] for i in order if i in polygons]
    if len(found) == 1:
        return {"type": "Polygon", "coordinates": found[0]}
    return {"type": "MultiPolygon", "coordinates": found}


def _project(geom: Geometry, crs: Crs) -> Geometry:
    """幾何的座標依座標系統換成經緯度。"""
    if crs.kind == "geographic" and crs.unit == 1.0 and crs.prime_meridian == 0.0:
        return geom

    def convert(coords: Any) -> Any:
        arr = np.asarray(coords, dtype=float)
        if arr.ndim == 1:
            lon, lat = crs.inverse(arr[:1], arr[1:2])
            return [float(lon[0]), float(lat[0])]
        if arr.ndim == 2:
            lon, lat = crs.inverse(arr[:, 0], arr[:, 1])
            return np.column_stack([lon, lat]).tolist()
        return [convert(c) for c in coords]

    if geom["type"] == "GeometryCollection":
        return {
            "type": "GeometryCollection",
            "geometries": [_project(g, crs) for g in geom["geometries"]],
        }
    coords = geom["coordinates"]
    if geom["type"] in ("Polygon", "MultiLineString"):
        return {"type": geom["type"], "coordinates": [convert(r) for r in coords]}
    if geom["type"] == "MultiPolygon":
        return {"type": geom["type"], "coordinates": [[convert(r) for r in p] for p in coords]}
    return {"type": geom["type"], "coordinates": convert(coords)}


def _dbf(data: bytes, encoding: str | None, path: str) -> tuple[list[str], list[dict[str, Any]]]:
    if len(data) < 32:
        raise SpatialFileError(f"{PurePosixPath(path).name} 的 .dbf 不完整")
    count = struct.unpack("<I", data[4:8])[0]
    header_length, record_length = struct.unpack("<HH", data[8:12])
    language = data[29]
    fields: list[tuple[str, str, int, int]] = []
    at = 32
    while at + 32 <= header_length and data[at] != 0x0D:
        raw_name = data[at : at + 11].split(b"\x00")[0]
        kind = chr(data[at + 11])
        length, decimals = data[at + 16], data[at + 17]
        fields.append((raw_name.decode("latin-1").strip(), kind, length, decimals))
        at += 32
    codec = _codec(encoding, language)
    names = [_decode(f[0].encode("latin-1"), codec) for f in fields]
    rows: list[dict[str, Any]] = []
    start = header_length
    for r in range(count):
        record = data[start + r * record_length : start + (r + 1) * record_length]
        if len(record) < record_length:
            break
        values: dict[str, Any] = {}
        pos = 1  # 第一個位元組是刪除標記
        for name, (_, kind, length, decimals) in zip(names, fields, strict=True):
            raw = record[pos : pos + length]
            pos += length
            values[name] = _dbf_value(raw, kind, decimals, codec)
        rows.append(values)
    return names, rows


def _codec(cpg: str | None, language: int) -> str | None:
    if cpg:
        name = cpg.strip().lower()
        name = CPG_ALIASES.get(name, name)
        if name.isdigit():
            name = f"cp{name}"
        try:
            "".encode(name)
            return name
        except LookupError:
            pass
    return DBF_CODEPAGES.get(language)


def _decode(raw: bytes, codec: str | None) -> str:
    if codec is not None:
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _dbf_value(raw: bytes, kind: str, decimals: int, codec: str | None) -> Any:
    if kind in ("C", "V"):
        text = _decode(raw, codec).rstrip(" \x00")
        return text if text != "" else None
    text = raw.decode("latin-1").strip(" \x00")
    if text == "" or set(text) <= {"*", "?"}:
        return None
    if kind in ("N", "F", "O"):
        try:
            number = float(text)
        except ValueError:
            return None
        if kind == "N" and decimals == 0 and number.is_integer() and abs(number) < 2**63:
            return int(number)
        return number
    if kind == "I":
        return int(text)
    if kind == "D":
        try:
            return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    if kind == "L":
        if text[:1] in "TtYy":
            return True
        if text[:1] in "FfNn":
            return False
        return None
    return None


# ---------------------------------------------------------------- GeoJSON


def _geojson_crs(document: dict[str, Any]) -> Crs | None:
    crs = document.get("crs")
    if not isinstance(crs, dict):
        return None
    name = str((crs.get("properties") or {}).get("name") or "")
    digits = "".join(ch for ch in name.rsplit(":", 1)[-1] if ch.isdigit())
    if not digits or name.upper().endswith("CRS84") or digits == "4326":
        return None
    try:
        return crs_from_epsg(int(digits))
    except ProjectionError as exc:
        raise SpatialFileError(f"GeoJSON 的座標系統：{exc}") from exc


def _plain(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def read_geojson(document: Any, path: str) -> SpatialTable:
    if not isinstance(document, dict):
        raise SpatialFileError(f"{PurePosixPath(path).name} 不是 GeoJSON")
    crs = _geojson_crs(document)
    kind = document.get("type")
    if kind == "FeatureCollection":
        features = document.get("features") or []
    elif kind == "Feature":
        features = [document]
    elif kind in (
        "Point",
        "LineString",
        "Polygon",
        "MultiPoint",
        "MultiLineString",
        "MultiPolygon",
        "GeometryCollection",
    ):
        features = [{"type": "Feature", "properties": {}, "geometry": document}]
    else:
        raise SpatialFileError(f"{PurePosixPath(path).name} 不是 GeoJSON")
    names: list[str] = []
    rows: list[dict[str, Any]] = []
    for feature in features:
        properties = feature.get("properties") or {}
        row = {k: _plain(v) for k, v in properties.items()}
        for k in row:
            if k not in names:
                names.append(k)
        geom = feature.get("geometry")
        if geom is not None and crs is not None:
            geom = _project(geom, crs)
        row[GEOMETRY_COLUMN] = _wkt(geom) if geom else None
        rows.append(row)
    return SpatialTable([*names, GEOMETRY_COLUMN], rows)


# ---------------------------------------------------------------- TopoJSON


def read_topojson(document: Any, layer: str | None, path: str) -> SpatialTable:
    if not isinstance(document, dict) or document.get("type") != "Topology":
        raise SpatialFileError(f"{PurePosixPath(path).name} 不是 TopoJSON")
    objects = document.get("objects") or {}
    if not objects:
        raise SpatialFileError(f"{PurePosixPath(path).name} 沒有物件")
    name = next((k for k in objects if k.lower() == (layer or "").lower()), next(iter(objects)))
    transform = document.get("transform")
    arcs: list[list[list[float]]] = []
    for arc in document.get("arcs") or []:
        points: list[list[float]] = []
        x = y = 0.0
        for step in arc:
            if transform:
                x += step[0]
                y += step[1]
                points.append(
                    [
                        x * transform["scale"][0] + transform["translate"][0],
                        y * transform["scale"][1] + transform["translate"][1],
                    ]
                )
            else:
                points.append([float(step[0]), float(step[1])])
        arcs.append(points)

    def line(indices: list[int]) -> list[list[float]]:
        points: list[list[float]] = []
        for index in indices:
            arc = arcs[index] if index >= 0 else arcs[~index][::-1]
            points.extend(arc if not points else arc[1:])
        return points

    def position(p: list[float]) -> list[float]:
        if not transform:
            return [float(p[0]), float(p[1])]
        return [
            p[0] * transform["scale"][0] + transform["translate"][0],
            p[1] * transform["scale"][1] + transform["translate"][1],
        ]

    def geometry(obj: dict[str, Any]) -> Geometry | None:
        kind = obj.get("type")
        if kind == "Point":
            return {"type": kind, "coordinates": position(obj["coordinates"])}
        if kind == "MultiPoint":
            return {"type": kind, "coordinates": [position(p) for p in obj["coordinates"]]}
        if kind == "LineString":
            return {"type": kind, "coordinates": line(obj["arcs"])}
        if kind == "MultiLineString":
            return {"type": kind, "coordinates": [line(a) for a in obj["arcs"]]}
        if kind == "Polygon":
            return {"type": kind, "coordinates": [line(r) for r in obj["arcs"]]}
        if kind == "MultiPolygon":
            return {"type": kind, "coordinates": [[line(r) for r in p] for p in obj["arcs"]]}
        return None

    root = objects[name]
    members = root.get("geometries") if root.get("type") == "GeometryCollection" else [root]
    names: list[str] = []
    rows: list[dict[str, Any]] = []
    for obj in members or []:
        row = {k: _plain(v) for k, v in (obj.get("properties") or {}).items()}
        for k in row:
            if k not in names:
                names.append(k)
        row[GEOMETRY_COLUMN] = _wkt(geometry(obj))
        rows.append(row)
    return SpatialTable([*names, GEOMETRY_COLUMN], rows)


# ---------------------------------------------------------------- KML


def _local(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _kml_coords(text: str | None) -> list[list[float]]:
    found: list[list[float]] = []
    for item in (text or "").split():
        parts = item.split(",")
        if len(parts) >= 2:
            try:
                found.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return found


def _kml_geometry(element: Any) -> Geometry | None:
    kind = _local(element.tag)

    def coords_of(node: Any) -> list[list[float]]:
        for child in node.iter():
            if isinstance(child.tag, str) and _local(child.tag) == "coordinates":
                return _kml_coords(child.text)
        return []

    if kind == "Point":
        c = coords_of(element)
        return {"type": "Point", "coordinates": c[0]} if c else None
    if kind in ("LineString", "LinearRing"):
        c = coords_of(element)
        return {"type": "LineString", "coordinates": c} if len(c) >= 2 else None
    if kind == "Polygon":
        rings: list[list[list[float]]] = []
        for boundary in element:
            if not isinstance(boundary.tag, str):
                continue
            name = _local(boundary.tag)
            if name in ("outerBoundaryIs", "innerBoundaryIs"):
                c = coords_of(boundary)
                if len(c) >= 3:
                    if name == "outerBoundaryIs":
                        rings.insert(0, c)
                    else:
                        rings.append(c)
        return {"type": "Polygon", "coordinates": rings} if rings else None
    if kind == "MultiGeometry":
        parts = [
            g
            for g in (_kml_geometry(child) for child in element if isinstance(child.tag, str))
            if g is not None
        ]
        kinds = {p["type"] for p in parts}
        if len(kinds) == 1 and parts:
            single = next(iter(kinds))
            if single in ("Point", "LineString", "Polygon"):
                return {"type": f"Multi{single}", "coordinates": [p["coordinates"] for p in parts]}
        return {"type": "GeometryCollection", "geometries": parts} if parts else None
    return None


GEOMETRY_TAGS = frozenset({"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"})


def read_kml(data: bytes, path: str) -> SpatialTable:
    from lxml import etree

    try:
        root = etree.fromstring(
            data, parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
    except etree.XMLSyntaxError as exc:
        raise SpatialFileError(f"{PurePosixPath(path).name} 不是可讀取的 KML：{exc}") from exc
    names = ["Name", "Description"]
    rows: list[dict[str, Any]] = []
    for placemark in root.iter():
        if not isinstance(placemark.tag, str) or _local(placemark.tag) != "Placemark":
            continue
        row: dict[str, Any] = {"Name": None, "Description": None}
        geom: Geometry | None = None
        for child in placemark:
            if not isinstance(child.tag, str):
                continue
            tag = _local(child.tag)
            if tag == "name":
                row["Name"] = (child.text or "").strip() or None
            elif tag == "description":
                row["Description"] = (child.text or "").strip() or None
            elif tag == "ExtendedData":
                for item in child.iter():
                    if not isinstance(item.tag, str):
                        continue
                    local = _local(item.tag)
                    key = item.get("name")
                    if not key:
                        continue
                    if local == "Data":
                        value = next(
                            (
                                v.text
                                for v in item
                                if isinstance(v.tag, str) and _local(v.tag) == "value"
                            ),
                            None,
                        )
                    elif local == "SimpleData":
                        value = item.text
                    else:
                        continue
                    row[key] = value.strip() if isinstance(value, str) else value
                    if key not in names:
                        names.append(key)
            elif tag in GEOMETRY_TAGS and geom is None:
                geom = _kml_geometry(child)
        row[GEOMETRY_COLUMN] = _wkt(geom)
        rows.append(row)
    return SpatialTable([*names, GEOMETRY_COLUMN], rows)


__all__ = [
    "GEOMETRY_COLUMN",
    "SpatialFileError",
    "SpatialTable",
    "read_geojson",
    "read_kml",
    "read_shapefile",
    "read_spatial",
    "read_topojson",
    "sidecar_paths",
]
