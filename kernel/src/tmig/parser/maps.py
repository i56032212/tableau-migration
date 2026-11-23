"""工作表的地圖設定：背景地圖的樣式與圖層（<style-rule element='map'／'map-layer'>），與平移、縮放後
存檔的顯示範圍（座標軸樣式中 Latitude／Longitude (generated) 的固定範圍，Web Mercator 的公尺）。"""

from __future__ import annotations

import contextlib
import math
from typing import get_args

from lxml import etree

from tmig.ir.models import DensityStyle, MapLayerName, MapOptions, MapStyle, MapViewport
from tmig.parser.twbx import child, children

EARTH_RADIUS = 6378137.0
"""Web Mercator（EPSG:3857）的地球半徑（公尺）。"""

STYLES = frozenset(get_args(MapStyle))

LAYER_PREFIXES: tuple[tuple[str, MapLayerName], ...] = (
    ("background", "base"),
    ("waterway", "water"),
    ("water", "water"),
    ("admin-0-boundaries", "country_borders"),
    ("9-dash-line", "country_borders"),
    ("admin-0-label", "country_names"),
    ("admin-1-boundaries", "state_borders"),
    ("admin1-water-lines", "state_borders"),
    ("admin-1-label", "state_names"),
    ("us-admin-1-label", "state_names"),
    ("admin-2-boundaries", "county_borders"),
    ("us-admin-2-boundaries", "county_borders"),
    ("place-", "cities"),
)
"""Tableau 底圖樣式中的圖層 id（例如 admin-1-label-3rd-tier）→ 平台的底圖圖層；其他圖層（地表覆蓋、
道路、美國人口普查的資料圖層等）平台的底圖沒有，略過。"""

GENERATED_AXES = {"[Latitude (generated)]": "lat", "[Longitude (generated)]": "lon"}


def layer_of(layer_id: str) -> MapLayerName | None:
    for prefix, name in LAYER_PREFIXES:
        if layer_id == prefix or layer_id.startswith(prefix):
            return name
    return None


def map_options(
    style: etree._Element | None, hide_unknown: bool = False, basemap: bool = True
) -> MapOptions | None:
    """工作表 <style> 中的地圖設定；都是預設值時為 None。basemap 為 False：背景地圖選「無」，座標軸的
    固定範圍沒有 projection 屬性時是經緯度（不是 Web Mercator 的公尺；寫法取自公開報表）。"""
    options = MapOptions(hide_unknown=hide_unknown, basemap=basemap)
    degrees: dict[str, tuple[float, float]] = {}
    states: dict[MapLayerName, list[bool]] = {}
    ranges: dict[str, tuple[float, float]] = {}
    for rule in children(style, "style-rule"):
        element = rule.get("element")
        if element == "map":
            for fmt in children(rule, "format"):
                name, value = fmt.get("attr"), fmt.get("value", "")
                if name == "map-style" and value in STYLES:
                    options.style = value  # type: ignore[assignment]
                elif name == "washout":
                    with contextlib.suppress(ValueError):
                        options.washout = min(max(float(value), 0.0), 1.0)
        elif element == "map-layer":
            for fmt in children(rule, "format"):
                layer = layer_of(fmt.get("id", ""))
                if fmt.get("attr") == "enabled" and layer is not None:
                    states.setdefault(layer, []).append(fmt.get("value") == "true")
        elif element == "axis":
            for enc in children(rule, "encoding"):
                axis = GENERATED_AXES.get(_short(enc.get("field", "")))
                if (
                    axis is None
                    or enc.get("attr") != "space"
                    or enc.get("range-type") != "fixed"
                    or enc.get("projection", "EPSG:3857") != "EPSG:3857"
                ):
                    continue
                try:
                    low, high = float(enc.get("min", "")), float(enc.get("max", ""))
                except ValueError:
                    continue
                if not basemap and enc.get("projection") is None:
                    degrees[axis] = (min(low, high), max(low, high))
                else:
                    ranges[axis] = (low, high)
    # 同一個圖層的各部分（例如州名的各層級）一起開關：有任何一部分開啟就是開啟
    options.layers = {name: any(values) for name, values in states.items()}
    if "lat" in ranges and "lon" in ranges:
        options.viewport = _viewport(ranges["lon"], ranges["lat"])
    elif "lat" in degrees and "lon" in degrees:
        (west, east), (south, north) = degrees["lon"], degrees["lat"]
        if east > west and north > south:
            options.viewport = MapViewport(
                west=round(west, 6),
                south=round(south, 6),
                east=round(east, 6),
                north=round(north, 6),
            )
    if options == MapOptions():
        return None
    return options


def _short(field: str) -> str:
    """[federated.xxx].[Latitude (generated)] → [Latitude (generated)]。"""
    index = field.rfind("].[")
    return field[index + 2 :] if index >= 0 else field


def _viewport(x: tuple[float, float], y: tuple[float, float]) -> MapViewport | None:
    west, east = (math.degrees(v / EARTH_RADIUS) for v in x)
    south, north = (
        math.degrees(2 * math.atan(math.exp(v / EARTH_RADIUS)) - math.pi / 2) for v in y
    )
    south, north = max(south, -85.06), min(north, 85.06)
    if east <= west or north <= south:
        return None
    return MapViewport(
        west=round(west, 6), south=round(south, 6), east=round(east, 6), north=round(north, 6)
    )


def density_style(pane: etree._Element) -> DensityStyle | None:
    """密度標記的配色（<encoding attr='density-color' palette=…>）與強度（density-intensity）。"""
    style = DensityStyle()
    for rule in children(child(pane, "style"), "style-rule"):
        if rule.get("element") != "mark":
            continue
        for enc in children(rule, "encoding"):
            if enc.get("attr") == "density-color" and enc.get("palette"):
                style.palette = enc.get("palette")
        for fmt in children(rule, "format"):
            if fmt.get("attr") == "density-intensity":
                with contextlib.suppress(ValueError):
                    style.intensity = min(max(float(fmt.get("value", "")), 0.0), 1.0)
    return None if style == DensityStyle() else style
