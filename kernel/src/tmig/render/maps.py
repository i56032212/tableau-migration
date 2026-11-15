"""地圖：Latitude／Longitude (generated) 在欄列架上的工作表（Tableau 的符號地圖、填色地圖、密度圖與
標記圖層），以及緯度、經度角色的欄位在欄列架上的地圖。

- 位置在查詢之後依檢視中的地理欄位找出（tmig.geo.geocode，與 Tableau 相同以最細的地理角色為準）
- 填色地圖的形狀與底圖（陸地、國界、州界）以 TopoJSON 從 /api/geo/<版本>/ 取得（瀏覽器快取），
  依每一列的形狀 id 查出；底圖的地名、地圖上的標籤與密度圖在伺服器端算好
- 以麥卡托投影畫出（與 Tableau 的 Web Mercator 相同）：自動縮放到所有標記的範圍，存檔時固定了範圍
  （平移、縮放過）的地圖畫出存檔的範圍；跨越 180 度經線的範圍（例如俄羅斯）以該處為中心
- 標記圖層與雙軸地圖：每張標記卡是一個圖層，依序由下往上畫
- 空間資料（Shapefile 等空間檔案的 Geometry、MAKEPOINT 等空間函式）：每個標記的形狀是查詢結果中
  COLLECT 的 WKT，多邊形、線與點分別畫出（多邊形填色、線依顏色畫線、點畫成圓點）；形狀依畫面的
  像素簡化，WKT 不放進規格
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tmig.geo import SHAPES, spatial, version
from tmig.geo.extent import MAX_LATITUDE, Box, Projector, mercator_y, normalize_lon
from tmig.geo.gazetteer import ROLES, Place, gazetteer
from tmig.geo.geocode import GeoSettings, finest, geocode
from tmig.ir.models import (
    GEOMETRY_GENERATED,
    IR,
    LATITUDE_GENERATED,
    LONGITUDE_GENERATED,
    ColorPalette,
    DataField,
    FieldUse,
    MapLayerName,
    MapOptions,
    Marks,
    Scalar,
    Worksheet,
)
from tmig.planner.state import use_key
from tmig.render.colors import member_order
from tmig.render.density import KERNEL_RADIUS, MAX_GRID, density_image, palette_of
from tmig.render.palettes import DEFAULT_REGULAR, PALETTES

if TYPE_CHECKING:
    from tmig.render.vegalite import Channel, _Builder

LON, LAT, SHAPE, FEATURE, LABEL = "__lon", "__lat", "__shape", "__feature", "__map_label"
"""每個圖層在資料列中的位置欄（後面接圖層的編號）：經度、緯度、形狀檔、形狀 id、標籤。"""
GEO = "__geo"
"""查出的形狀（GeoJSON）。"""
GPOLY, GLINE, GPTS, GPT, GLON, GLAT = "__gpoly", "__gline", "__gpts", "__gpt", "__glon", "__glat"
"""空間資料的圖層在資料列中的欄（後面接圖層的編號）：多邊形與線（GeoJSON）、點（[經度, 緯度] 的清單，
畫圖時展開成一點一列）。"""
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
FACET_HEADER = 24
"""分格的地圖每一格標題的高度（像素，估計每一格的大小用）。"""
FACET_GAP = 16
"""分格的地圖格與格之間的距離（像素）。"""


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
    geoms: dict[int, spatial.Geometry] = field(default_factory=dict)
    """空間資料的圖層：各資料列（以 id() 為鍵）的形狀。"""
    areas: dict[int, float] = field(default_factory=dict)
    """空間資料的圖層：各資料列的形狀範圍的大小（標籤的優先順序）。"""
    centered: bool = False
    """空間資料的圖層中有多邊形：標籤寫在形狀的中央（與填色地圖相同）。"""

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
    shapes: dict[str, spatial.Geometry | None] = {}
    wkt_columns: set[str] = set()
    for n, (pane, marks) in enumerate(cards):
        own = pane is not None and pane in pane_values
        records = pane_values[pane] if own and pane is not None else values
        layer = Layer(n, marks, pane, records, own)
        plan.layers.append(layer)
        geometry = marks.geometry
        if geometry is not None and geometry.field != GEOMETRY_GENERATED:
            # 空間資料：位置與形狀就是查詢結果中的 WKT
            ch = channels.get(use_key(geometry))
            if ch is not None:
                _place_geometries(layer, ch.name, shapes, plan)
                wkt_columns.update((ch.name, ch.label))
            continue
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
    # WKT 不放進規格（畫圖用的形狀依畫面簡化後另外放）
    for records in [values, *pane_values.values()]:
        for record in records:
            for column in wkt_columns:
                if column in record:
                    record[column] = None
    return plan, values, pane_values


def _place_geometries(
    layer: Layer, column: str, shapes: dict[str, spatial.Geometry | None], plan: MapPlan
) -> None:
    """空間資料的圖層：各資料列的形狀（WKT）、範圍與標籤的位置；沒有形狀（Null）的標記不畫。"""
    layer.kind = "geometry"
    failed = 0
    for record in layer.records:
        text = record.get(column)
        geom: spatial.Geometry | None = None
        if isinstance(text, str):
            if text not in shapes:
                try:
                    shapes[text] = spatial.parse_wkt(text)
                except spatial.SpatialError:
                    shapes[text] = None
                    failed += 1
            geom = shapes[text]
        found = spatial.bounds(geom)
        if geom is None or found is None:
            record[layer.name(LON)] = record[layer.name(LAT)] = None
            continue
        west, south, east, north = found
        point = spatial.label_point(geom) or [(west + east) / 2, (south + north) / 2]
        record[layer.name(LON)], record[layer.name(LAT)] = point[0], point[1]
        layer.geoms[id(record)] = geom
        layer.boxes.append(found)
        layer.areas[id(record)] = (east - west) * (mercator_y(north) - mercator_y(south))
        if not layer.centered and next(spatial.iter_polygons(geom), None) is not None:
            layer.centered = True
    if failed:
        plan.notes.append(f"{failed} 個形狀的 WKT 無法解析，沒有畫出")


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


def body(
    builder: _Builder,
    ir: IR,
    ws: Worksheet,
    plan: MapPlan,
    rows_dims: list[FieldUse] | None = None,
    cols_dims: list[FieldUse] | None = None,
    params: dict[str, Scalar] | None = None,
) -> dict[str, Any]:
    """地圖的規格片段（projection 與各圖層）。欄列架上有離散維度時與 Tableau 相同分格（每格一張地圖，
    範圍相同），以 Vega-Lite 的 facet 畫出；標籤的重疊依每一格的大小各自判斷。"""
    options = ws.map or MapOptions()
    layers_on: dict[MapLayerName, bool] = {**DEFAULT_LAYERS, **options.layers}
    facet: dict[str, Any] = {}
    if rows_dims:
        facet["row"] = builder._facet(rows_dims, "rows_dim")
    if cols_dims:
        facet["column"] = builder._facet(cols_dims, "cols_dim")
    sheet_extent = builder.extent
    if facet:
        # 每一格的大小：依格數平分（減去標題的高度），投影與標籤的位置依它估計
        def count(channel: str) -> int:
            spec = facet.get(channel)
            if spec is None:
                return 1
            return len({r.get(spec["field"]) for r in builder.values}) or 1

        builder.extent = (
            max(sheet_extent[0] / count("column") - FACET_GAP, 80.0),
            max(sheet_extent[1] / count("row") - FACET_HEADER - FACET_GAP, 60.0),
        )
        if any(layer.own_data for layer in plan.layers):
            builder.notes.append("分格的地圖中，詳細程度較粗的圖層在每一格都畫出全部的標記")
        if any(layer.kind == "density" for layer in plan.layers):
            builder.notes.append("分格的密度圖在每一格都畫出全部標記的密度")
    try:
        return _body(builder, ir, ws, plan, options, layers_on, facet, params or {})
    finally:
        builder.extent = sheet_extent


def _body(
    builder: _Builder,
    ir: IR,
    ws: Worksheet,
    plan: MapPlan,
    options: MapOptions,
    layers_on: dict[MapLayerName, bool],
    facet: dict[str, Any],
    params: dict[str, Scalar],
) -> dict[str, Any]:
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
    keys = [spec["field"] for spec in facet.values()]
    for layer in plan.layers:
        drawn, labels = _data_layer(builder, layer, project, box, placed, keys)
        marks_layers += drawn
        label_layers += labels
    base = _base_layers(box, zoom, layers_on, style)
    names = _place_labels(box, zoom, project, width, height, layers_on, style, placed)
    references = _reference_layers(builder, ir, ws, plan, box, params)
    references += _trend_layers(builder, plan)
    layers = [*base, *marks_layers, *references, *names, *label_layers]
    if plan.unknown:
        layers.append(_unknown_indicator(plan.unknown, builder.text_color(), bool(facet)))
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
    # 畫面之外的形狀裁掉（SVG 小很多）；多留 2 像素，邊緣不會出現裁切的框線（分格時依每一格的大小）
    w, h = ("child_width", "child_height") if facet else ("width", "height")
    projection["clipExtent"] = {"expr": f"[[-2, -2], [{w} + 2, {h} + 2]]"}
    result: dict[str, Any] = {"projection": projection, "layer": layers}
    if layers_on["water"] and layers_on["base"]:
        result["view"] = {"fill": style.water, "stroke": None}
    if facet:
        return {"facet": facet, "spec": result}
    return result


def _reference_layers(
    builder: _Builder, ir: IR, ws: Worksheet, plan: MapPlan, box: Box, params: dict[str, Scalar]
) -> list[dict[str, Any]]:
    """地圖上的參考線：緯度（列架）、經度（欄架）座標軸上的參考線，與 Tableau 相同畫成橫跨地圖的緯線、
    經線，以第一個圖層的標記位置計算（平均、中位數、最小、最大、常數或參數等）；標籤寫在線的左端（經線在
    上端）。參考區間（兩條參考線配成一對）在兩條線之間塗上底色。分布與盒鬚圖尚未畫出。"""
    if not ws.reference_lines or not plan.layers:
        return []
    from tmig.render.vegalite import (
        REF,
        Channel,
        _Constant,
        _line_style,
        _reference_labels,
        _reference_source,
        _reference_values,
    )

    fields = {f.id: f for d in ir.datasources for f in d.fields}
    main = plan.layers[0]
    layers: list[dict[str, Any]] = []
    drawn: dict[str, tuple[str, float, str | None]] = {}
    """畫好的參考線：id →（緯度或經度、值、底色），給參考區間配對用。"""
    for n, line in enumerate(ws.reference_lines):
        role = _coordinate_role(line.axis, fields)
        if role is None:
            builder.notes.append(f"地圖上的參考線 {line.id} 不在經緯度的座標軸上，沒有畫出")
            continue
        if line.kind != "line":
            builder.notes.append(f"地圖上的分布與盒鬚圖尚未畫出（參考線 {line.id}）")
            continue
        position = main.name(LAT if role == "latitude" else LON)
        source: str | _Constant | None = _reference_source(line, builder.channels, params, False)
        if source is None and isinstance(line.value, FieldUse):
            # 標記的位置（Latitude／Longitude (generated)）：以這個圖層找到的經緯度計算
            source = position if _coordinate_role(line.value, fields) == role else None
        if source is None:
            builder.notes.append(f"地圖上的參考線 {line.id} 的值不在查詢結果中，沒有畫出")
            continue
        numbers = [
            float(r[source])
            for r in main.records
            if isinstance(source, str) and isinstance(r.get(source), int | float)
        ]
        values = [v for v in _reference_values(line, numbers, source) if v is not None]
        style = _line_style(line)
        east = box.east if box.east >= box.west else box.east + 360
        name = f"{REF}{n}_map"
        if values:
            drawn[line.id] = (role, values[0], line.fill_color)
        for value in values:
            if role == "latitude":
                points = [{"lon": box.west, "lat": value}, {"lon": east, "lat": value}]
            else:
                points = [{"lon": value, "lat": box.south}, {"lon": value, "lat": box.north}]
            if style is not None:
                layers.append(
                    {
                        "name": name,
                        "data": {"values": points},
                        "mark": {"type": "line", "clip": True, **style},
                        "encoding": {
                            "longitude": {"field": "lon", "type": "quantitative"},
                            "latitude": {"field": "lat", "type": "quantitative"},
                        },
                    }
                )
        axis = Channel(position, position, role.capitalize(), "measure", "real", line.axis)
        for i, text in _reference_labels(line, axis, list(values), False):
            point = _label_point(role, values[i], box, east)
            mark: dict[str, Any] = {
                "type": "text",
                "align": "left",
                "baseline": "bottom" if role == "latitude" else "top",
                "dx": 3,
                "dy": -2 if role == "latitude" else 2,
                "fontSize": 11,
                "color": "#333333",
                "clip": True,
            }
            layers.append(
                {
                    "name": f"{name}_label",
                    "data": {"values": [{**point, "text": text}]},
                    "mark": mark,
                    "encoding": {
                        "longitude": {"field": "lon", "type": "quantitative"},
                        "latitude": {"field": "lat", "type": "quantitative"},
                        "text": {"field": "text"},
                    },
                }
            )
    # 參考區間：配成一對的兩條參考線之間塗上底色（畫在線的下面）
    east = box.east if box.east >= box.west else box.east + 360
    bands: list[dict[str, Any]] = []
    done: set[frozenset[str]] = set()
    for n, line in enumerate(ws.reference_lines):
        pair = frozenset({line.id, line.paired or ""})
        first, second = drawn.get(line.id), drawn.get(line.paired or "")
        if first is None or second is None or first[0] != second[0] or pair in done:
            continue
        done.add(pair)
        low, high = sorted([first[1], second[1]])
        corners = (
            {"lon": box.west, "lat": low, "lon2": east, "lat2": high}
            if first[0] == "latitude"
            else {"lon": low, "lat": box.south, "lon2": high, "lat2": box.north}
        )
        bands.append(
            {
                "name": f"{REF}{n}_map_band",
                "data": {"values": [corners]},
                "mark": {
                    "type": "rect",
                    "clip": True,
                    "color": _band_color(first[2] or second[2]),
                    "opacity": BAND_OPACITY,
                    "tooltip": False,
                },
                "encoding": {
                    "longitude": {"field": "lon", "type": "quantitative"},
                    "latitude": {"field": "lat", "type": "quantitative"},
                    "longitude2": {"field": "lon2"},
                    "latitude2": {"field": "lat2"},
                },
            }
        )
    return [*bands, *layers]


BAND_OPACITY = 0.25
"""地圖上參考區間的底色的不透明度。"""
TREND_POINTS = 60
"""地圖上的趨勢線取的點數。"""


def _band_color(color: str | None) -> str:
    """參考區間的底色：#rrggbb（帶透明度的 #rrggbbaa 只取顏色）；沒有設定時是淺灰色。"""
    if color is None:
        return "#bab0ac"
    return color[:7] if color.startswith("#") and len(color) == 9 else color


def _trend_layers(builder: _Builder, plan: MapPlan) -> list[dict[str, Any]]:
    """地圖上的趨勢線：各圖層標記的緯度對經度配適模型（與一般圖表相同的模型），畫在標記的經度範圍內；
    模型記在描述趨勢模型的清單中。"""
    from tmig.render.trends import fit_model
    from tmig.render.vegalite import DASHES, TREND, TREND_COLOR, _css_color

    layers: list[dict[str, Any]] = []
    for layer in plan.layers:
        trend = layer.marks.trend
        if trend is None:
            continue
        points = [
            (float(r[layer.name(LON)]), float(r[layer.name(LAT)]))
            for r in layer.records
            if isinstance(r.get(layer.name(LON)), int | float)
            and isinstance(r.get(layer.name(LAT)), int | float)
        ]
        model = fit_model(points, trend.fit, trend.degree, trend.intercept)
        if model is None:
            builder.notes.append("地圖上趨勢線的資料不足，未畫出")
            continue
        step = (model.high - model.low) / (TREND_POINTS - 1)
        path = []
        for i in range(TREND_POINTS):
            lon = model.low + step * i
            lat = model.predict(lon)
            if lat is not None and -MAX_LATITUDE <= lat <= MAX_LATITUDE:
                path.append({"lon": lon, "lat": lat, "order": i})
        if len(path) < 2:
            continue
        size = (trend.degree if trend.fit == "polynomial" else 1) + (1 if trend.intercept else 0)
        builder.trend_models.append(
            {
                "group": [],
                "fit": trend.fit,
                "equation": model.equation("Latitude", "Longitude"),
                "observations": model.n,
                "filtered": len(points) - model.n,
                "model_df": size,
                "residual_df": model.n - size,
                "r2": model.r2,
                "p": model.p_value,
                "standard_error": model.sigma,
                "terms": list(model.terms),
            }
        )
        mark: dict[str, Any] = {
            "type": "line",
            "clip": True,
            "strokeWidth": trend.width,
            "color": _css_color(trend.color) if trend.color is not None else TREND_COLOR,
            "tooltip": False,
        }
        if trend.pattern != "solid":
            mark["strokeDash"] = DASHES[trend.pattern]
        layers.append(
            {
                "name": f"{TREND}_map{layer.index}",
                "data": {"values": path},
                "mark": mark,
                "encoding": {
                    "longitude": {"field": "lon", "type": "quantitative"},
                    "latitude": {"field": "lat", "type": "quantitative"},
                    "order": {"field": "order", "type": "quantitative"},
                },
            }
        )
    return layers


def _label_point(role: str, value: float, box: Box, east: float) -> dict[str, float]:
    """參考線標籤的位置：緯線在左端、經線在上端（稍微往內，避開地圖的邊緣）。"""
    if role == "latitude":
        return {"lon": box.west + (east - box.west) * 0.01, "lat": value}
    return {"lon": value, "lat": box.north - (box.north - box.south) * 0.01}


def _coordinate_role(use: FieldUse, fields: dict[str, DataField]) -> str | None:
    """欄位是緯度或經度（Latitude／Longitude (generated)，或地理角色為緯度、經度的欄位）。"""
    if use.field == LATITUDE_GENERATED:
        return "latitude"
    if use.field == LONGITUDE_GENERATED:
        return "longitude"
    role = fields[use.field].geo_role if use.field in fields else None
    return role if role in ("latitude", "longitude") else None


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


def _unknown_indicator(count: int, color: str, faceted: bool = False) -> dict[str, Any]:
    """右下角的「N 個未知」（與 Tableau 相同，找不到位置的標記不畫）；分格時寫在每一格的右下角。"""
    w, h = ("child_width", "child_height") if faceted else ("width", "height")
    return {
        "name": f"{DECO}_unknown",
        "data": {"values": [{"text": f"{count:,} 個未知"}]},
        "mark": {
            "type": "text",
            "align": "right",
            "baseline": "bottom",
            "x": {"expr": f"{w} - 6"},
            "y": {"expr": f"{h} - 4"},
            "fontSize": 10,
            "color": color,
            "opacity": 0.8,
        },
        "encoding": {"text": {"field": "text"}},
    }


# ---------------------------------------------------------------- 資料的圖層


def _data_layer(
    builder: _Builder,
    layer: Layer,
    project: Projector,
    box: Box,
    placed: list[Rect],
    keys: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """一張標記卡的圖層與標籤圖層（keys：分格的欄，標籤的重疊依每一格判斷）。"""
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
        return _density_layers(builder, layer, project, encoding, position), []
    if layer.kind == "geometry":
        drawn = _geometry_layers(builder, layer, project, encoding, data)
    elif layer.kind == "polygon":
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
            measures = [
                c
                for c in builder.channels.values()
                if c.role == "measure" and c.datatype != "spatial"
            ]
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
        # 形狀：標記卡上固定的形狀與自訂的圖片形狀（圖片另外一層，畫在同一個位置）
        main, pictures = builder.shape_layers(marks, mark, encoding)
        drawn.append(
            {
                **data,
                "transform": [valid],
                "mark": mark,
                "encoding": {**encoding, **position, **main.get("encoding", {})},
            }
        )
        for picture in pictures:
            drawn.append(
                {
                    **data,
                    **picture,
                    "transform": [valid, *picture["transform"]],
                    "encoding": {**position, **picture["encoding"]},
                }
            )
        if marks.type in ("pie", "bar", "area", "gantt", "polygon"):
            builder.notes.append(f"地圖上的{marks.type}標記以圓點呈現")
    labels = _label_layer(builder, layer, project, encoding, data, position, placed, keys or [])
    return drawn, labels


def _geometry_layers(
    builder: _Builder,
    layer: Layer,
    project: Projector,
    encoding: dict[str, Any],
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    """空間資料的圖層：多邊形（填色，與填色地圖相同的框線）、線（依顏色畫線，粗細依標記的大小）與點
    （圓點）。形狀依畫面的像素簡化（半個像素以內的頂點合併）。"""
    from tmig.render.vegalite import LINE_WIDTH, _mark_style

    marks = layer.marks
    poly, line, pts = layer.name(GPOLY), layer.name(GLINE), layer.name(GPTS)
    has: dict[str, bool] = {poly: False, line: False, pts: False}
    for record in layer.records:
        geom = layer.geoms.get(id(record))
        polygons, lines, points = (
            spatial.render_parts(geom, project.arrays) if geom is not None else (None, None, [])
        )
        record[poly], record[line] = polygons, lines
        record[pts] = points or None
        has[poly] = has[poly] or polygons is not None
        has[line] = has[line] or lines is not None
        has[pts] = has[pts] or bool(points)
    drawn: list[dict[str, Any]] = []
    shaped = {k: v for k, v in encoding.items() if k not in ("size", "shape")}
    if has[poly]:
        fill_encoding = dict(shaped)
        _map_palette(builder, layer, fill_encoding)
        mark = _mark_style({"type": "geoshape", "tooltip": True, "clip": True}, marks)
        if marks.stroke is None:
            mark.update(stroke="#5f5f5f", strokeWidth=0.6, strokeOpacity=0.55)
        drawn.append(
            {
                **data,
                "transform": [{"filter": f"isValid(datum[{json.dumps(poly)}])"}],
                "mark": mark,
                "encoding": {**fill_encoding, "shape": {"field": poly, "type": "geojson"}},
            }
        )
    if has[line]:
        width = LINE_WIDTH * (marks.mark_size if marks.mark_size is not None else 1)
        mark = _mark_style(
            {
                "type": "geoshape",
                "filled": False,
                "tooltip": True,
                "clip": True,
                "strokeWidth": round(width, 2),
                "strokeCap": "round",
                "strokeJoin": "round",
            },
            marks,
        )
        line_encoding = dict(shaped)
        drawn.append(
            {
                **data,
                "transform": [{"filter": f"isValid(datum[{json.dumps(line)}])"}],
                "mark": mark,
                "encoding": {**line_encoding, "shape": {"field": line, "type": "geojson"}},
            }
        )
    if has[pts]:
        point = _mark_style({"type": "circle", "tooltip": True, "clip": True}, marks)
        point_encoding = dict(encoding)
        builder.size_mark(point, point_encoding, marks)
        item, lon, lat = layer.name(GPT), layer.name(GLON), layer.name(GLAT)
        drawn.append(
            {
                **data,
                "transform": [
                    {"filter": f"isValid(datum[{json.dumps(pts)}])"},
                    {"flatten": [pts], "as": [item]},
                    {"calculate": f"datum[{json.dumps(item)}][0]", "as": lon},
                    {"calculate": f"datum[{json.dumps(item)}][1]", "as": lat},
                ],
                "mark": point,
                "encoding": {
                    **point_encoding,
                    "longitude": {"field": lon, "type": "quantitative"},
                    "latitude": {"field": lat, "type": "quantitative"},
                },
            }
        )
    return drawn


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
    keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """標記的標籤：填色的區域在標籤點的中央，點在右側；互相重疊的標籤與 Tableau 相同不顯示
    （依工作表在 dashboard 上的大小估計，較大的區域、標記優先）。"""
    marks = layer.marks
    source = builder.label_field(marks, layer.place_label)
    if source is None:
        return []
    font = builder.font("cell", marks.label_font)
    polygon = layer.kind == "polygon" or layer.centered
    name = layer.name(LABEL)
    sizes = builder.size_values(marks, layer.records) if marks.size is not None else None
    order = sorted(
        range(len(layer.records)),
        key=lambda i: _priority(layer, i, sizes),
        reverse=True,
    )
    width, height = builder.extent
    # 分格時每一格各自判斷重疊（其他格的標籤不影響）；底圖的地名避開所有格的標籤
    cells: dict[tuple[Any, ...], list[Rect]] = {}
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
        cell = cells.setdefault(tuple(record.get(k) for k in keys or []), [])
        if any(_overlap(rect, other) for other in (cell if keys else placed)):
            continue
        cell.append(rect)
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
    if layer.kind == "geometry":
        return layer.areas.get(id(layer.records[index]), 0.0)
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


def _density_layers(
    builder: _Builder,
    layer: Layer,
    project: Projector,
    encoding: dict[str, Any],
    position: dict[str, Any],
) -> list[dict[str, Any]]:
    """密度圖：伺服器端算好的密度圖片，以四角的經緯度疊在地圖上。色彩上有離散的維度時，每個成員各自
    一張密度圖（由淺到這個成員的顏色），另有一層看不見的點帶出色彩的圖例。"""
    width, height = builder.extent
    radius = KERNEL_RADIUS * (layer.marks.mark_size or 1.0)
    located = [r for r in layer.records if r.get(layer.name(LON)) is not None]
    if not located:
        return []
    points = [project(r[layer.name(LON)], r[layer.name(LAT)]) for r in located]
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
    west, north = project.invert(left, top)
    east, south = project.invert(left + grid_w / scale, top + grid_h / scale)
    south, north = max(south, -MAX_LATITUDE), min(north, MAX_LATITUDE)

    def image(
        name: str, chosen: list[tuple[float, float]], colors: Sequence[str]
    ) -> dict[str, Any]:
        url = density_image(
            chosen, grid_w, grid_h, radius * scale, colors, style.intensity if style else None
        )
        return {
            "name": name,
            "data": {
                "values": [{"url": url, "lon": west, "lat": south, "lon2": east, "lat2": north}]
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

    color = encoding.get("color")
    grouped = (
        isinstance(color, dict)
        and color.get("type") in ("nominal", "ordinal")
        and "field" in color
        and layer.marks.color is not None
    )
    if not grouped:
        colors = palette_of(style.palette if style else None)
        return [image(f"{DECO}_density{layer.index}", shifted, colors)]
    assert isinstance(color, dict) and layer.marks.color is not None
    field = color["field"]
    members: dict[Any, list[tuple[float, float]]] = {}
    for record, point in zip(located, shifted, strict=True):
        members.setdefault(record.get(field), []).append(point)
    scale_def = color.get("scale") if isinstance(color.get("scale"), dict) else None
    if scale_def is not None and "domain" in scale_def and "range" in scale_def:
        domain, shades = list(scale_def["domain"]), list(scale_def["range"])
    else:
        # 沒有指定色盤：與離散色彩相同，成員依原值排序後依序取 Tableau 10 的顏色
        channel = builder.ch(layer.marks.color)
        raw = {r.get(field): r.get(channel.name) for r in located}
        domain = sorted(members, key=lambda label: member_order(raw.get(label)))
        base = PALETTES[DEFAULT_REGULAR].colors
        shades = [base[i % len(base)] for i in range(len(domain))]
    tints = dict(zip(domain, shades, strict=False))
    layers = [
        image(
            f"{DECO}_density{layer.index}_{i}",
            members[label],
            (_tint(tints.get(label, "#4e79a7")), tints.get(label, "#4e79a7")),
        )
        for i, label in enumerate(d for d in domain if d in members)
    ]
    # 色彩的圖例：看不見的點（密度圖是圖片，本身沒有圖例）
    legend = copy.deepcopy(color)
    legend["scale"] = {"domain": domain, "range": shades}
    legend["legend"] = {**(legend.get("legend") or {}), "symbolOpacity": 1, "symbolType": "square"}
    layers.append(
        {
            "name": f"{DECO}_density{layer.index}_legend",
            "data": {"values": located},
            "mark": {"type": "point", "opacity": 0, "filled": True, "tooltip": False},
            "encoding": {**position, "color": legend},
        }
    )
    return layers


def _tint(color: str, amount: float = 0.8) -> str:
    """顏色與白色混合（amount 為白色的比例）：密度由低到高是淺色到這個顏色。"""
    digits = color.lstrip("#")
    if len(digits) != 6:
        return "#ffffff"
    parts = [int(digits[i : i + 2], 16) for i in (0, 2, 4)]
    mixed = [round(c + (255 - c) * amount) for c in parts]
    return "#" + "".join(f"{c:02x}" for c in mixed)
