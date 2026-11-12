"""空間檔案的讀取（tmig.data.spatial_files）：Shapefile（.prj 的投影、內環、編碼、壓縮檔）、GeoJSON、KML、
TopoJSON。"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import zipfile

import pytest

from tmig.data.spatial_files import SpatialFileError, read_spatial
from tmig.geo.projection import crs_from_epsg
from tmig.geo.spatial import parse_wkt

from .spatial_book import STORES, TWD97_PRJ, dbf, shp_points, shp_polygons, stores_zip


def coords(text: str | None) -> list[float]:
    assert text is not None
    return [float(v) for v in re.findall(r"-?\d+\.\d+", text)]


def test_zipped_shapefile_with_projection() -> None:
    """TWD97 二度分帶的點換回經緯度；UTF-8 的中文、數字與日期的屬性。"""
    table = read_spatial({"Data/stores.zip": stores_zip()}, "Data/stores.zip", "stores")
    assert table.columns == ["Store", "Sales", "Opened", "Geometry"]
    by_name = {row["Store"]: row for row in table.rows}
    assert set(by_name) == {s[0] for s in STORES}
    for name, lat, lon, sales, opened in STORES:
        row = by_name[name]
        assert row["Sales"] == sales
        assert row["Opened"] == (
            dt.date(int(opened[:4]), int(opened[4:6]), int(opened[6:])) if opened else None
        )
        assert coords(row["Geometry"]) == pytest.approx([lon, lat], abs=1e-7)


def test_loose_shapefile_polygons_with_holes_and_big5() -> None:
    """各環依包含關係分成外環與內環（與環的方向無關）；.cpg 指定 Big5（950）。"""
    outer = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]  # 順時針（ESRI 的外環）
    hole = [(0.2, 0.2), (0.4, 0.2), (0.4, 0.4), (0.2, 0.4), (0.2, 0.2)]  # 逆時針（ESRI 的內環）
    island = [(0.25, 0.25), (0.25, 0.3), (0.3, 0.3), (0.3, 0.25), (0.25, 0.25)]  # 內環中的小島
    second = [(2.0, 2.0), (3.0, 2.0), (3.0, 3.0), (2.0, 3.0), (2.0, 2.0)]  # 方向不合 ESRI 的外環
    shp = shp_polygons([[outer, hole, island, second], [second]])
    table_bytes = dbf(
        [("名稱", "C", 20, 0), ("面積", "N", 10, 3)], [["臺北", 1.5], ["新北", None]], "cp950"
    )
    files = {"shapes/area.shp": shp, "shapes/area.dbf": table_bytes, "shapes/area.cpg": b"950"}
    table = read_spatial(files, "shapes/area.shp")
    assert [row["名稱"] for row in table.rows] == ["臺北", "新北"]
    assert [row["面積"] for row in table.rows] == [1.5, None]
    geom = parse_wkt(table.rows[0]["Geometry"])
    assert geom is not None and geom["type"] == "MultiPolygon"
    polygons = geom["coordinates"]
    assert [len(p) for p in polygons] == [2, 1, 1]  # 外環＋內環、第二個外環、內環中的小島
    assert parse_wkt(table.rows[1]["Geometry"]) == {
        "type": "Polygon",
        "coordinates": parse_wkt(table.rows[1]["Geometry"])["coordinates"],  # type: ignore[index]
    }


def test_polylines_and_missing_attributes() -> None:
    shp = shp_polygons(
        [
            [[(121.0, 25.0), (121.05, 25.05)]],
            [[(120.0, 23.0), (120.01, 23.0)], [(120.5, 23.5), (120.6, 23.6)]],
        ],
        kind=3,
    )
    table = read_spatial({"lines.shp": shp}, "lines.shp")
    assert table.columns == ["Geometry"]
    assert table.rows[0]["Geometry"].startswith("LINESTRING(121.0000000 25.0000000")
    assert table.rows[1]["Geometry"].startswith("MULTILINESTRING((120.0000000 23.0000000")


def test_points_without_prj_are_lon_lat() -> None:
    shp, shx = shp_points([(121.5, 25.0)])
    table = read_spatial({"p.shp": shp, "p.shx": shx}, "p.shp")
    assert table.rows[0]["Geometry"] == "POINT(121.5000000 25.0000000)"


def test_geojson_with_crs() -> None:
    crs = crs_from_epsg(3826)
    x, y = crs.forward([121.5645], [25.0339])
    document = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3826"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "101", "height": 508, "tags": ["tower"]},
                "geometry": {"type": "Point", "coordinates": [float(x[0]), float(y[0])]},
            },
            {"type": "Feature", "properties": {"name": "none"}, "geometry": None},
        ],
    }
    table = read_spatial({"a.geojson": json.dumps(document).encode()}, "a.geojson")
    assert table.columns == ["name", "height", "tags", "Geometry"]
    first, second = table.rows
    assert first["Geometry"] == "POINT(121.5645000 25.0339000)"
    assert first["tags"] == '["tower"]'
    assert second["Geometry"] is None


KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>
  <Placemark><name>公園</name><description>綠地</description>
    <ExtendedData><Data name="area"><value>12.5</value></Data></ExtendedData>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>
      121.50,25.00,0 121.51,25.00,0 121.51,25.01,0 121.50,25.01,0 121.50,25.00,0
    </coordinates></LinearRing></outerBoundaryIs>
    <innerBoundaryIs><LinearRing><coordinates>
      121.502,25.002 121.503,25.002 121.503,25.003 121.502,25.002
    </coordinates></LinearRing></innerBoundaryIs></Polygon>
  </Placemark>
  <Placemark><name>站牌</name>
    <ExtendedData><SchemaData schemaUrl="#s"><SimpleData name="route">307</SimpleData></SchemaData></ExtendedData>
    <MultiGeometry><Point><coordinates>121.5,25.0</coordinates></Point><Point><coordinates>121.6,25.1</coordinates></Point></MultiGeometry>
  </Placemark>
</Folder></Document></kml>"""


def test_kml() -> None:
    table = read_spatial({"a.kml": KML.encode()}, "a.kml")
    assert table.columns == ["Name", "Description", "area", "route", "Geometry"]
    park, stop = table.rows
    assert (park["Name"], park["Description"], park["area"]) == ("公園", "綠地", "12.5")
    geom = parse_wkt(park["Geometry"])
    assert geom is not None and geom["type"] == "Polygon" and len(geom["coordinates"]) == 2
    assert stop["route"] == "307"
    assert stop["Geometry"] == "MULTIPOINT((121.5000000 25.0000000), (121.6000000 25.1000000))"


def test_topojson_layer() -> None:
    document = {
        "type": "Topology",
        "transform": {"scale": [0.001, 0.001], "translate": [120, 22]},
        "arcs": [[[0, 0], [1000, 0], [0, 1000], [-1000, 0], [0, -1000]]],
        "objects": {
            "first": {"type": "GeometryCollection", "geometries": []},
            "towns": {
                "type": "GeometryCollection",
                "geometries": [
                    {"type": "Polygon", "arcs": [[0]], "properties": {"town": "A"}},
                    {"type": "Point", "coordinates": [500, 500], "properties": {"town": "B"}},
                ],
            },
        },
    }
    table = read_spatial({"t.topojson": json.dumps(document).encode()}, "t.topojson", "towns")
    assert [row["town"] for row in table.rows] == ["A", "B"]
    assert table.rows[1]["Geometry"] == "POINT(120.5000000 22.5000000)"
    polygon = parse_wkt(table.rows[0]["Geometry"])
    assert polygon is not None and polygon["type"] == "Polygon"


def test_unsupported_formats() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("parcels.gdb/a00000001.gdbtable", b"\x00")
    with pytest.raises(SpatialFileError, match="File Geodatabase"):
        read_spatial({"p.zip": out.getvalue()}, "p.zip")
    with pytest.raises(SpatialFileError, match="MapInfo"):
        read_spatial({"p.tab": b""}, "p.tab")
    with pytest.raises(SpatialFileError, match="Shapefile"):
        read_spatial({"bad.shp": b"not a shapefile"}, "bad.shp")
    prj = TWD97_PRJ.replace("Transverse_Mercator", "Polar_Stereographic")
    shp, _ = shp_points([(1.0, 2.0)])
    with pytest.raises(SpatialFileError, match="座標系統"):
        read_spatial({"p.shp": shp, "p.prj": prj.encode()}, "p.shp")
