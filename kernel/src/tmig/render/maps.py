"""地圖：Latitude／Longitude (generated) 在欄列架上的工作表（Tableau 的符號地圖、填色地圖、密度圖與
標記圖層），以及緯度、經度角色的欄位在欄列架上的地圖。

- 位置在查詢之後依檢視中的地理欄位找出（tmig.geo.geocode，與 Tableau 相同以最細的地理角色為準）
- 填色地圖的形狀與底圖（陸地、國界、州界）以 TopoJSON 從 /api/geo/<版本>/ 取得（瀏覽器快取），
  依每一列的形狀 id 查出；底圖的地名、地圖上的標籤與密度圖在伺服器端算好
- 以麥卡托投影畫出（與 Tableau 的 Web Mercator 相同）：自動縮放到所有標記的範圍，存檔時固定了範圍
  （平移、縮放過）的地圖畫出存檔的範圍；跨越 180 度經線的範圍（例如俄羅斯）以該處為中心
- 標記圖層與雙軸地圖：每張標記卡是一個圖層，依序由下往上畫
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tmig.geo import SHAPES, version
from tmig.geo.extent import MAX_LATITUDE, Box, Projector, mercator_y, normalize_lon
from tmig.geo.gazetteer import ROLES, Place, gazetteer
from tmig.geo.geocode import GeoSettings, finest, geocode
from tmig.ir.models import (
    IR,
    ColorPalette,
    DataField,
    MapLayerName,
    MapOptions,
    Marks,
    Worksheet,
)
from tmig.planner.state import use_key
from tmig.render.density import KERNEL_RADIUS, MAX_GRID, density_image, palette_of

if TYPE_CHECKING:
    from tmig.render.vegalite import Channel, _Builder

LON, LAT, SHAPE, FEATURE, LABEL = "__lon", "__lat", "__shape", "__feature", "__map_label"
"""每個圖層在資料列中的位置欄（後面接圖層的編號）：經度、緯度、形狀檔、形狀 id、標籤。"""
GEO = "__geo"
"""查出的形狀（GeoJSON）。"""
DECO = "__deco_map"
"""底圖的圖層名稱前綴（檢視器不對它們醒目提示、選取）。"""

SHAPED = frozenset({"country", "state", "county"})
"""有區域形狀的地理角色（郵遞區號只有中心點）。"""
POINT_MARKS = {"circle": "circle", "square": "square", "shape": "point"}
LABEL_GAP = 3
"""點的標籤與點之間的距離（像素）。"""
PADDING = 0.06
"""自動縮放時四周的留白（範圍的比例）。"""
MIN_SPAN = (1.0, 0.6)
"""自動縮放的最小範圍（經度、緯度的度數）：只有一個點時不會放大到街道。"""
WORLD = Box(0.0, 179.99, -58.0, 78.0)
"""沒有任何位置時顯示整個世界。"""

DEFAULT_LAYERS: dict[MapLayerName, bool] = {
    "base": True,
    "water": True,
    "country_borders": True,
    "country_names": True,
    "state_borders": True,
    "state_names": True,
    "county_borders": False,
    "cities": False,
}
"""底圖圖層的預設（Tableau 的地圖圖層：郡界、城市名稱預設不顯示）。"""


@dataclass(frozen=True)
class BaseStyle:
    water: str
    land: str
    border: str
    state_border: str
    label: str
    country_label: str


STYLES = {
    "light": BaseStyle("#ffffff", "#e6e6e6", "#c9c9c9", "#d8d8d8", "#a6a6a6", "#8c8c8c"),
    "normal": BaseStyle("#aacbe5", "#f5f3ee", "#c3bcaf", "#d5cfc4", "#8d8d8d", "#6e6e6e"),
    "dark": BaseStyle("#262626", "#404040", "#5c5c5c", "#4e4e4e", "#9c9c9c", "#b4b4b4"),
    "streets": BaseStyle("#a6d1ef", "#f2efe9", "#c6beb1", "#d7d0c4", "#8a8a8a", "#6a6a6a"),
    "outdoors": BaseStyle("#a5cdee", "#e9ecdb", "#bfc2a8", "#d0d3bc", "#7e8273", "#646857"),
    "satellite": BaseStyle("#1d3c58", "#56603f", "#86906a", "#717b56", "#e3e3e3", "#f2f2f2"),
}
"""背景地圖的樣式（依 Tableau 的畫面估計；衛星影像、街道、戶外地圖以簡化的配色呈現）。"""

LOD = ((2.6, "110m"), (5.0, "50m"))
"""國家形狀的精細度：縮放層級低於門檻時用較粗的形狀（其餘用 10m）。"""
STATE_BORDER_ZOOM = 2.6
"""州界、州名出現的縮放層級。"""
COUNTY_BORDER_ZOOM = 5.5
LABEL_FONT = {"country": 11.0, "state": 9.0, "city": 9.0}
MAP_SEQUENTIAL = "blue_teal_10_0"
"""填色地圖的自動連續色盤（Tableau 的 Blue-Teal）。"""
MAX_LABELS = 160
"""每種底圖地名最多的數量（重要的在前）。"""


@dataclass
class Layer:
    """一個圖層（一張標記卡）的資料與位置。"""

    index: int
    marks: Marks
    pane: int | None
    """ws.panes 的索引；None 為共用的標記卡（沒有個別的標記卡時）。"""
    records: list[dict[str, Any]]
    own_data: bool
    """資料列是這個標記卡自己的查詢結果（詳細程度較粗的圖層）。"""
    role: str | None = None
    kind: str = "point"
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    places: dict[int, Place] = field(default_factory=dict)
    """各資料列（以 id() 為鍵）找到的地點。"""
    unknown: int = 0
    place_label: str | None = None
    """最細的地理角色欄位的顯示文字欄：開啟標記標籤、標籤上沒有欄位時標示地名（與 Tableau 相同）。"""

    def name(self, base: str) -> str:
        return f"{base}{self.index}"


@dataclass
class MapPlan:
    layers: list[Layer]
    notes: list[str] = field(default_factory=list)

    @property
    def unknown(self) -> int:
        return sum(layer.unknown for layer in self.layers)


def is_map(ws: Worksheet, fields: dict[str, DataField]) -> bool:
    """Latitude／Longitude (generated) 在欄列架上，或緯度、經度角色的欄位分別在列與欄架上。"""
    shelves = [*ws.rows, *ws.columns]
    if any(u.field in ("latitude_generated", "longitude_generated") for u in shelves):
        return True
    return _coordinate_fields(ws, fields) is not None


def _coordinate_fields(ws: Worksheet, fields: dict[str, DataField]) -> tuple[str, str] | None:
    """列架上緯度角色、欄架上經度角色的欄位（欄位鍵）。"""
    lat = next(
        (u for u in ws.rows if u.field in fields and fields[u.field].geo_role == "latitude"), None
    )
    lon = next(
        (u for u in ws.columns if u.field in fields and fields[u.field].geo_role == "longitude"),
        None,
    )
    return (use_key(lat), use_key(lon)) if lat is not None and lon is not None else None


def locate(
    ir: IR,
    ws: Worksheet,
    channels: dict[str, Channel],
    values: list[dict[str, Any]],
    pane_values: dict[int, list[dict[str, Any]]],
) -> tuple[MapPlan, list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """找出每個圖層的標記位置（加在資料列上），回傳圖層與（篩選掉未知位置之後的）資料列。"""
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    fields = {f.id: f for d in ir.datasources for f in d.fields}
    options = ws.map or MapOptions()
    cards: list[tuple[int | None, Marks]] = (
        [(i, p.marks) for i, p in enumerate(ws.panes)] if ws.panes else [(None, ws.marks)]
    )
    coordinates = _coordinate_fields(ws, fields)
    plan = MapPlan([])
    for n, (pane, marks) in enumerate(cards):
        own = pane is not None and pane in pane_values
        records = pane_values[pane] if own and pane is not None else values
        layer = Layer(n, marks, pane, records, own)
        plan.layers.append(layer)
        if coordinates is not None:
            _place_coordinates(layer, channels[coordinates[0]].name, channels[coordinates[1]].name)
            continue
        keys: dict[str, str] = {}
        labels: dict[str, str] = {}
        aliases: dict[str, dict[str, str]] = {}
        for use in [*ws.rows, *ws.columns, *marks.uses()]:
            ch = channels.get(use_key(use))
            data_field = fields.get(use.field)
            if ch is None or ch.role != "dimension" or data_field is None:
                continue
            role = data_field.geo_role
            if role in ROLES and role not in keys and use.date is None:
                keys[role] = ch.name
                labels[role] = ch.label
                aliases[role] = {a.value: a.location for a in data_field.geo_aliases}
        layer.role = finest(list(keys))
        layer.place_label = labels.get(layer.role) if layer.role is not None else None
        layer.kind = _kind(marks, layer.role)
        filled = marks.type == "map" or (marks.type == "automatic" and marks.geometry is not None)
        if filled and layer.role is not None and layer.kind != "polygon":
            plan.notes.append(
                "郵遞區號、城市沒有區域形狀（平台的地理資料只有中心點），填色地圖以中心點的圓點呈現"
            )
        if layer.role is None:
            layer.unknown = len(records)
            if records:
                plan.notes.append("地圖的圖層中沒有地理角色的欄位，標記的位置未知")
            continue
        settings = GeoSettings(
            default_country=ds.geo.country if ds.geo else None,
            default_state=ds.geo.state if ds.geo else None,
            aliases=aliases,
        )
        located = geocode(records, keys, settings)
        polygons = layer.kind == "polygon"
        for record, found in zip(records, located, strict=True):
            place = found.place
            if place is None:
                record[layer.name(LON)] = record[layer.name(LAT)] = None
                layer.unknown += 1
                continue
            record[layer.name(LON)], record[layer.name(LAT)] = place.lon, place.lat
            layer.places[id(record)] = place
            if polygons and place.shape is not None:
                record[layer.name(SHAPE)] = place.shape
                record[layer.name(FEATURE)] = place.feature
                layer.boxes.append((place.west, place.south, place.east, place.north))
            else:
                layer.boxes.append((place.lon, place.lat, place.lon, place.lat))
        if polygons and any(
            r.get(layer.name(LON)) is not None and r.get(layer.name(SHAPE)) is None for r in records
        ):
            plan.notes.append(
                "部分位置沒有區域形狀（例如連江縣，平台的地理資料只有位置），填色地圖上以圓點呈現"
            )
    if options.hide_unknown:
        # 「篩選資料」：去掉未知的位置（在列號之前，列號仍連續）
        def known(record: dict[str, Any], layers: list[Layer]) -> bool:
            return all(record.get(layer.name(LON)) is not None for layer in layers)

        main_layers = [layer for layer in plan.layers if not layer.own_data]
        values = [r for r in values if known(r, main_layers)]
        pane_values = {
            i: [r for r in records if known(r, [la for la in plan.layers if la.pane == i])]
            for i, records in pane_values.items()
        }
        for layer in plan.layers:
            layer.records = (
                pane_values[layer.pane] if layer.own_data and layer.pane is not None else values
            )
            layer.unknown = 0
    return plan, values, pane_values


def _place_coordinates(layer: Layer, lat_column: str, lon_column: str) -> None:
    """緯度、經度角色的欄位：位置就是欄位的值。"""
    layer.kind = "density" if layer.marks.type == "density" else _kind(layer.marks, None)
    for record in layer.records:
        lat, lon = record.get(lat_column), record.get(lon_column)
        if not isinstance(lat, int | float) or not isinstance(lon, int | float):
            record[layer.name(LON)] = record[layer.name(LAT)] = None
            layer.unknown += 1
            continue
        record[layer.name(LON)], record[layer.name(LAT)] = float(lon), float(lat)
        layer.boxes.append((float(lon), float(lat), float(lon), float(lat)))


def _kind(marks: Marks, role: str | None) -> str:
    """圖層的畫法：填色的區域（polygon）、點、文字、線或密度。"""
    mark = marks.type
    if mark == "density":
        return "density"
    if mark == "map" or (mark == "automatic" and marks.geometry is not None):
        return "polygon" if role in SHAPED else "circle"
    if mark in ("text", "line"):
        return mark
    if mark in POINT_MARKS:
        return mark
    return "circle"


# ---------------------------------------------------------------- 規格


def body(builder: _Builder, ir: IR, ws: Worksheet, plan: MapPlan) -> dict[str, Any]:
    """地圖的規格片段（projection 與各圖層）。"""
    options = ws.map or MapOptions()
    layers_on: dict[MapLayerName, bool] = {**DEFAULT_LAYERS, **options.layers}
    width, height = builder.extent
    box = _view_box(ir, ws, plan, options)
    zoom = box.zoom(width, height)
    project = box.project(width, height)
    style = _washed(STYLES[options.style], options.washout)
    builder.notes.extend(dict.fromkeys(plan.notes))
    if options.style in ("satellite", "streets", "outdoors"):
        builder.notes.append(
            f"背景地圖的「{options.style}」樣式以簡化的配色呈現（平台的底圖沒有街道、地形與衛星影像）"
        )
    marks_layers: list[dict[str, Any]] = []
    label_layers: list[dict[str, Any]] = []
    placed: list[Rect] = []
    """已放上的標籤（像素範圍，依工作表在 dashboard 上的大小估計）：互相重疊的不顯示。"""
    for layer in plan.layers:
        drawn, labels = _data_layer(builder, layer, project, box, placed)
        marks_layers += drawn
        label_layers += labels
    base = _base_layers(box, zoom, layers_on, style)
    names = _place_labels(box, zoom, project, width, height, layers_on, style, placed)
    layers = [*base, *marks_layers, *names, *label_layers]
    if plan.unknown:
        layers.append(_unknown_indicator(plan.unknown, builder.text_color()))
    projection: dict[str, Any] = {
        "type": "mercator",
        "fit": {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "MultiPoint",
                "coordinates": [
                    [round(box.west, 6), round(box.south, 6)],
                    [round(box.east, 6), round(box.north, 6)],
                ],
            },
        },
    }
    if abs(box.center) > 1e-9:
        projection["rotate"] = [round(-box.center, 6), 0, 0]
    # 畫面之外的形狀裁掉（SVG 小很多）；多留 2 像素，邊緣不會出現裁切的框線
    projection["clipExtent"] = {"expr": "[[-2, -2], [width + 2, height + 2]]"}
    result: dict[str, Any] = {"projection": projection, "layer": layers}
    if layers_on["water"] and layers_on["base"]:
        result["view"] = {"fill": style.water, "stroke": None}
    return result


def _view_box(ir: IR, ws: Worksheet, plan: MapPlan, options: MapOptions) -> Box:
    if options.viewport is not None:
        v = options.viewport
        return Box.viewport(v.west, v.south, v.east, v.north)
    found = Box.of(box for layer in plan.layers for box in layer.boxes)
    if found is not None:
        return found.padded(PADDING, *MIN_SPAN)
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    if ds.geo is not None and ds.geo.country:
        country = geocode([{"c": ds.geo.country}], {"country": "c"})[0].place
        if country is not None:
            fallback = Box.of([(country.west, country.south, country.east, country.north)])
            if fallback is not None:
                return fallback.padded(PADDING, *MIN_SPAN)
    return WORLD


def _washed(style: BaseStyle, washout: float) -> BaseStyle:
    """淡化：各顏色往白色混合。"""
    if washout <= 0:
        return style

    def mix(color: str) -> str:
        digits = color.lstrip("#")
        channels = [int(digits[i : i + 2], 16) for i in (0, 2, 4)]
        return "#" + "".join(f"{round(c + (255 - c) * washout):02x}" for c in channels)

    return BaseStyle(*(mix(getattr(style, f)) for f in BaseStyle.__dataclass_fields__))


def _shape_url(name: str) -> str:
    return f"/api/geo/{version()}/{name}.json"


def _shape_source(shape: str, zoom: float) -> tuple[str, str]:
    """形狀檔（地點的 shape）→ 網址與 TopoJSON 中的物件名稱。"""
    if shape == "countries":
        scale = next((s for limit, s in LOD if zoom < limit), "10m")
        return _shape_url(f"countries-{scale}"), "countries"
    if shape == "us-counties":
        return _shape_url("us-counties"), "counties"
    return _shape_url(shape), "admin1"


def _base_layers(
    box: Box, zoom: float, on: dict[MapLayerName, bool], style: BaseStyle
) -> list[dict[str, Any]]:
    """底圖：陸地、國界、畫面中各國的州界（縮放到一定程度時）、美國的郡界。"""
    layers: list[dict[str, Any]] = []
    countries, _ = _shape_source("countries", zoom)
    if on["base"]:
        layers.append(
            {
                "name": f"{DECO}_land",
                "data": {"url": countries, "format": {"type": "topojson", "feature": "countries"}},
                "mark": {"type": "geoshape", "fill": style.land, "stroke": None, "clip": True},
            }
        )
    in_view = gazetteer().overlapping("country", _intervals(box))
    if on["state_borders"] and zoom >= STATE_BORDER_ZOOM:
        for place in in_view:
            if place.country is None or not (SHAPES / "admin1" / f"{place.country}.json").exists():
                continue
            layers.append(
                {
                    "name": f"{DECO}_states_{place.country}",
                    "data": {
                        "url": _shape_url(f"admin1/{place.country}"),
                        "format": {"type": "topojson", "feature": "borders"},
                    },
                    "mark": _line_mark(style.state_border, 0.5),
                }
            )
    us = any(p.country == "USA" for p in in_view)
    if on["county_borders"] and zoom >= COUNTY_BORDER_ZOOM and us:
        layers.append(
            {
                "name": f"{DECO}_counties",
                "data": {
                    "url": _shape_url("us-counties"),
                    "format": {"type": "topojson", "feature": "counties"},
                },
                "mark": _line_mark(style.state_border, 0.3),
            }
        )
    if on["country_borders"]:
        layers.append(
            {
                "name": f"{DECO}_borders",
                "data": {"url": countries, "format": {"type": "topojson", "feature": "borders"}},
                "mark": _line_mark(style.border, 0.8),
            }
        )
    return layers


def _line_mark(color: str, width: float) -> dict[str, Any]:
    return {
        "type": "geoshape",
        "filled": False,
        "stroke": color,
        "strokeWidth": width,
        "clip": True,
    }


def _intervals(box: Box) -> list[tuple[float, float, float, float]]:
    """box 的經度範圍分成 ±180 之內的區段（west, south, east, north）。"""
    west, east = box.west, box.east
    if east - west >= 359.0:
        return [(-180.0, box.south, 180.0, box.north)]
    west, east = normalize_lon(west), normalize_lon(east)
    if west <= east:
        return [(west, box.south, east, box.north)]
    return [(west, box.south, 180.0, box.north), (-180.0, box.south, east, box.north)]


Rect = tuple[float, float, float, float]


def _place_labels(
    box: Box,
    zoom: float,
    project: Projector,
    width: float,
    height: float,
    on: dict[MapLayerName, bool],
    style: BaseStyle,
    placed: list[Rect],
) -> list[dict[str, Any]]:
    """底圖的地名：國名、州名、城市名稱，依 Natural Earth 的出現層級（縮放到該層級才顯示）；與已放上的
    標籤（地圖上的標記標籤、較重要的地名）重疊的不顯示（依工作表在 dashboard 上的大小估計）。"""
    roles = [
        ("country", on["country_names"], style.country_label),
        ("state", on["state_names"] and zoom >= STATE_BORDER_ZOOM, style.label),
        ("city", on["cities"], style.label),
    ]
    layers: list[dict[str, Any]] = []
    for role, shown, color in roles:
        if not shown:
            continue
        places: list[Place] = []
        for west, south, east, north in _intervals(box):
            places += gazetteer().within(role, west, south, east, north, zoom, MAX_LABELS * 2)
        size = LABEL_FONT[role]
        values: list[dict[str, Any]] = []
        for place in sorted(places, key=lambda p: (p.rank or 99, -(p.population or 0), p.name)):
            x, y = project(place.lon, place.lat)
            w, h = _extent(place.name, size)
            if role == "city":
                rect = (x - 2, y - h / 2, x + 4 + w, y + h / 2)
            else:
                rect = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
            if rect[0] < 0 or rect[1] < 0 or rect[2] > width or rect[3] > height:
                continue
            if any(_overlap(rect, other) for other in placed):
                continue
            placed.append(rect)
            values.append({"name": place.name, "lon": place.lon, "lat": place.lat})
            if len(values) >= MAX_LABELS:
                break
        if not values:
            continue
        mark: dict[str, Any] = {"type": "text", "fontSize": size, "color": color, "clip": True}
        if role == "country":
            mark["fontWeight"] = 600
        if role == "city":
            mark.update(align="left", baseline="middle", dx=4)
        position = {
            "longitude": {"field": "lon", "type": "quantitative"},
            "latitude": {"field": "lat", "type": "quantitative"},
        }
        if role == "city":
            layers.append(
                {
                    "name": f"{DECO}_city_dots",
                    "data": {"values": values},
                    "mark": {"type": "circle", "size": 9, "color": color, "clip": True},
                    "encoding": position,
                }
            )
        layers.append(
            {
                "name": f"{DECO}_{role}_names",
                "data": {"values": values},
                "mark": mark,
                "encoding": {**position, "text": {"field": "name"}},
            }
        )
    return layers


def _unknown_indicator(count: int, color: str) -> dict[str, Any]:
    """右下角的「N 個未知」（與 Tableau 相同，找不到位置的標記不畫）。"""
    return {
        "name": f"{DECO}_unknown",
        "data": {"values": [{"text": f"{count:,} 個未知"}]},
        "mark": {
            "type": "text",
            "align": "right",
            "baseline": "bottom",
            "x": {"expr": "width - 6"},
            "y": {"expr": "height - 4"},
            "fontSize": 10,
            "color": color,
            "opacity": 0.8,
        },
        "encoding": {"text": {"field": "text"}},
    }


# ---------------------------------------------------------------- 資料的圖層


def _data_layer(
    builder: _Builder, layer: Layer, project: Projector, box: Box, placed: list[Rect]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """一張標記卡的圖層與標籤圖層。"""
    from tmig.render.vegalite import _mark_style

    marks = layer.marks
    lon, lat = layer.name(LON), layer.name(LAT)
    position = {
        "longitude": {"field": lon, "type": "quantitative"},
        "latitude": {"field": lat, "type": "quantitative"},
    }
    valid = {"filter": f"isValid(datum[{json.dumps(lon)}])"}
    data = {"data": {"values": layer.records}} if layer.own_data else {}
    encoding: dict[str, Any] = {}
    builder.common(encoding, marks)
    drawn: list[dict[str, Any]] = []
    if layer.kind == "density":
        image = _density_layer(builder, layer, project)
        return ([image] if image is not None else []), []
    if layer.kind == "polygon":
        encoding.pop("size", None)
        encoding.pop("shape", None)
        _map_palette(builder, layer, encoding)
        shapes = sorted(
            {str(r[layer.name(SHAPE)]) for r in layer.records if r.get(layer.name(SHAPE))}
        )
        mark = _mark_style({"type": "geoshape", "tooltip": True, "clip": True}, marks)
        if marks.stroke is None:
            mark.update(stroke="#5f5f5f", strokeWidth=0.6, strokeOpacity=0.55)
        zoom = box.zoom(*builder.extent)
        for shape in shapes:
            url, obj = _shape_source(shape, zoom)
            drawn.append(
                {
                    **data,
                    "transform": [
                        {
                            "filter": f"datum[{json.dumps(layer.name(SHAPE))}] === {json.dumps(shape)}"
                        },
                        {
                            "lookup": layer.name(FEATURE),
                            "from": {
                                "data": {
                                    "url": url,
                                    "format": {"type": "topojson", "feature": obj},
                                },
                                "key": "id",
                            },
                            "as": GEO,
                        },
                        {"filter": f"isValid(datum.{GEO})"},
                    ],
                    "mark": mark,
                    "encoding": {**encoding, "shape": {"field": GEO, "type": "geojson"}},
                }
            )
        # 沒有形狀的位置（例如郵遞區號）以圓點畫出
        if any(r.get(lon) is not None and not r.get(layer.name(SHAPE)) for r in layer.records):
            point = _mark_style({"type": "circle", "tooltip": True, "clip": True}, marks)
            builder.size_mark(point, encoding, marks)
            drawn.append(
                {
                    **data,
                    "transform": [
                        valid,
                        {"filter": f"!datum[{json.dumps(layer.name(SHAPE))}]"},
                    ],
                    "mark": point,
                    "encoding": {**encoding, **position},
                }
            )
    elif layer.kind == "text":
        text_field = builder.text_field(marks)
        if text_field is None:
            measures = [c for c in builder.channels.values() if c.role == "measure"]
            text_field = measures[0].label if measures else None
        mark = {
            "type": "text",
            "tooltip": True,
            "lineBreak": "\n",
            "clip": True,
            **builder.font("cell", marks.label_font).mark(),
        }
        if marks.color is None:
            mark["color"] = builder.label_color(marks, mark)
        encoding["text"] = (
            {"field": text_field, "type": "nominal"} if text_field else {"value": "Abc"}
        )
        drawn.append(
            {**data, "transform": [valid], "mark": mark, "encoding": {**encoding, **position}}
        )
        return drawn, []
    elif layer.kind == "line":
        mark = _mark_style({"type": "line", "tooltip": True, "clip": True}, marks)
        if marks.path is not None:
            encoding["order"] = {"field": builder.ch(marks.path).name}
        else:
            mark["order"] = None
        drawn.append(
            {**data, "transform": [valid], "mark": mark, "encoding": {**encoding, **position}}
        )
    else:
        mark = _mark_style(
            {"type": POINT_MARKS.get(layer.kind, "circle"), "tooltip": True, "clip": True}, marks
        )
        if layer.kind == "shape":
            mark["filled"] = True
        builder.size_mark(mark, encoding, marks)
        drawn.append(
            {**data, "transform": [valid], "mark": mark, "encoding": {**encoding, **position}}
        )
        if marks.type in ("pie", "bar", "area", "gantt", "polygon"):
            builder.notes.append(f"地圖上的{marks.type}標記以圓點呈現")
    labels = _label_layer(builder, layer, project, encoding, data, position, placed)
    return drawn, labels


def _map_palette(builder: _Builder, layer: Layer, encoding: dict[str, Any]) -> None:
    """填色地圖依量值著色、沒有設定色盤時，Tableau 的自動色盤是 Blue-Teal（其他標記是 Blue）；
    有正有負時仍是自動的發散色盤。"""
    color = encoding.get("color")
    marks = layer.marks
    if (
        not isinstance(color, dict)
        or color.get("type") != "quantitative"
        or marks.color is None
        or marks.color_palette is not None
    ):
        return
    from tmig.render.colors import continuous_scale

    numbers = [
        float(r[color["field"]])
        for r in layer.records
        if isinstance(r.get(color["field"]), int | float)
        and not isinstance(r.get(color["field"]), bool)
    ]
    if numbers and min(numbers) < 0 < max(numbers):
        return
    color["scale"] = continuous_scale(
        ColorPalette(kind="continuous", palette=MAP_SEQUENTIAL), numbers
    )


def _label_layer(
    builder: _Builder,
    layer: Layer,
    project: Projector,
    encoding: dict[str, Any],
    data: dict[str, Any],
    position: dict[str, Any],
    placed: list[Rect],
) -> list[dict[str, Any]]:
    """標記的標籤：填色的區域在標籤點的中央，點在右側；互相重疊的標籤與 Tableau 相同不顯示
    （依工作表在 dashboard 上的大小估計，較大的區域、標記優先）。"""
    marks = layer.marks
    source = builder.label_field(marks, layer.place_label)
    if source is None:
        return []
    font = builder.font("cell", marks.label_font)
    polygon = layer.kind == "polygon"
    name = layer.name(LABEL)
    sizes = builder.size_values(marks, layer.records) if marks.size is not None else None
    order = sorted(
        range(len(layer.records)),
        key=lambda i: _priority(layer, i, sizes),
        reverse=True,
    )
    width, height = builder.extent
    for i in order:
        record = layer.records[i]
        record[name] = ""
        lon, lat = record.get(layer.name(LON)), record.get(layer.name(LAT))
        text = record.get(source)
        if lon is None or lat is None or text is None or str(text) == "":
            continue
        x, y = project(lon, lat)
        w, h = _extent(str(text), font.size)
        if polygon:
            rect = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
        else:
            rect = (x + LABEL_GAP + 4, y - h / 2, x + LABEL_GAP + 4 + w, y + h / 2)
        if not (-w < rect[0] < width and -h < rect[1] < height):
            continue
        if any(_overlap(rect, other) for other in placed):
            continue
        placed.append(rect)
        record[name] = str(text)
    mark: dict[str, Any] = {"type": "text", "lineBreak": "\n", "clip": True, **font.mark()}
    if polygon:
        mark.update(align="center", baseline="middle")
        color = builder.contrast(marks, encoding)
    else:
        mark.update(align="left", baseline="middle", dx=LABEL_GAP + 4)
        color = {"value": builder.label_color(marks, mark)}
    return [
        {
            **data,
            "transform": [{"filter": f"datum[{json.dumps(name)}] !== ''"}],
            "mark": mark,
            "encoding": {**position, "text": {"field": name}, "color": color},
        }
    ]


def _priority(layer: Layer, index: int, sizes: list[float | None] | None) -> float:
    """標籤的優先順序：點的大小，區域的面積（範圍的大小，以麥卡托投影的比例近似）。"""
    if sizes is not None:
        return sizes[index] or 0.0
    place = layer.places.get(id(layer.records[index]))
    if place is None or layer.kind != "polygon":
        return 0.0
    width = place.east - place.west if place.west <= place.east else place.east - place.west + 360
    return width * (mercator_y(place.north) - mercator_y(place.south))


def _extent(text: str, size: float) -> tuple[float, float]:
    from tmig.render.vegalite import _text_extent

    return _text_extent(text, size)


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _density_layer(builder: _Builder, layer: Layer, project: Projector) -> dict[str, Any] | None:
    """密度圖：伺服器端算好的密度圖片，以四角的經緯度疊在地圖上。"""
    width, height = builder.extent
    radius = KERNEL_RADIUS * (layer.marks.mark_size or 1.0)
    points = [
        project(r[layer.name(LON)], r[layer.name(LAT)])
        for r in layer.records
        if r.get(layer.name(LON)) is not None
    ]
    if not points:
        return None
    margin = 3 * radius
    left = max(min(x for x, _ in points) - margin, -width)
    right = min(max(x for x, _ in points) + margin, 2 * width)
    top = max(min(y for _, y in points) - margin, -height)
    bottom = min(max(y for _, y in points) + margin, 2 * height)
    scale = min(1.0, MAX_GRID / max(right - left, bottom - top, 1.0))
    grid_w = max(int((right - left) * scale), 1)
    grid_h = max(int((bottom - top) * scale), 1)
    shifted = [((x - left) * scale, (y - top) * scale) for x, y in points]
    style = layer.marks.density
    image = density_image(
        shifted,
        grid_w,
        grid_h,
        radius * scale,
        palette_of(style.palette if style else None),
        style.intensity if style else None,
    )
    west, north = project.invert(left, top)
    east, south = project.invert(left + grid_w / scale, top + grid_h / scale)
    south, north = max(south, -MAX_LATITUDE), min(north, MAX_LATITUDE)
    if layer.marks.color is not None:
        builder.notes.append("密度圖依色彩欄位分組的密度尚未支援，以全部標記的密度呈現")
    return {
        "name": f"{DECO}_density{layer.index}",
        "data": {
            "values": [{"url": image, "lon": west, "lat": south, "lon2": east, "lat2": north}]
        },
        "mark": {"type": "image", "aspect": False, "clip": True},
        "encoding": {
            "longitude": {"field": "lon", "type": "quantitative"},
            "latitude": {"field": "lat", "type": "quantitative"},
            "longitude2": {"field": "lon2"},
            "latitude2": {"field": "lat2"},
            "url": {"field": "url", "type": "nominal"},
        },
    }
