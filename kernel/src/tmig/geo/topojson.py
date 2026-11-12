"""TopoJSON 的解碼：填色地圖的形狀在伺服器端轉成 GeoJSON（只放進用到的形狀）。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

Coordinates = list[list[float]]


def decode(topology: dict[str, Any], name: str, digits: int = 4) -> dict[str, dict[str, Any]]:
    """物件 name 中各形狀的 GeoJSON geometry，以 id 為鍵；座標四捨五入到 digits 位小數（約 10 公尺）。"""
    transform = topology.get("transform")
    scale = transform["scale"] if transform else None
    translate = transform["translate"] if transform else None
    arcs: list[Coordinates] = []
    for arc in topology["arcs"]:
        points: Coordinates = []
        x = y = 0.0
        for position in arc:
            if scale is not None and translate is not None:
                x += position[0]
                y += position[1]
                point = [x * scale[0] + translate[0], y * scale[1] + translate[1]]
            else:
                point = [float(position[0]), float(position[1])]
            points.append([round(point[0], digits), round(point[1], digits)])
        arcs.append(points)

    def line(indices: list[int]) -> Coordinates:
        points: Coordinates = []
        for index in indices:
            arc = arcs[index] if index >= 0 else arcs[~index][::-1]
            points.extend(arc if not points else arc[1:])
        return points

    def ring(indices: list[int]) -> Coordinates:
        points = line(indices)
        if points and points[0] != points[-1]:
            points.append(points[0])
        return points

    def geometry(obj: dict[str, Any]) -> dict[str, Any] | None:
        kind = obj.get("type")
        if kind == "Polygon":
            return {"type": "Polygon", "coordinates": [ring(r) for r in obj["arcs"]]}
        if kind == "MultiPolygon":
            return {
                "type": "MultiPolygon",
                "coordinates": [[ring(r) for r in polygon] for polygon in obj["arcs"]],
            }
        if kind == "LineString":
            return {"type": "LineString", "coordinates": line(obj["arcs"])}
        if kind == "MultiLineString":
            return {"type": "MultiLineString", "coordinates": [line(a) for a in obj["arcs"]]}
        return None

    found: dict[str, dict[str, Any]] = {}
    collection = topology["objects"][name]
    members = collection.get("geometries", [collection])
    for obj in members:
        if "id" not in obj:
            continue
        shape = geometry(obj)
        if shape is not None:
            found[str(obj["id"])] = shape
    return found


@lru_cache(maxsize=64)
def load(path: Path, name: str) -> dict[str, dict[str, Any]]:
    """形狀檔（TopoJSON）中的物件 name，解碼後快取。"""
    return decode(json.loads(path.read_text(encoding="utf-8")), name)
