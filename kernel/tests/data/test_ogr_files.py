"""Esri File Geodatabase、MapInfo 與座標基準面之間的轉換：以 GDAL（pyogrio）寫出測試用的檔案，讀成資料列、
從 .twbx 解析與匯入、桌面版連線，座標與 Tableau 相同一律換成 WGS84（含基準面之間的轉換，PROJ）。

基準面的驗算：OSGB 1936 的 British National Grid（英國地形測量局 OS 的範例點，WGS84 約 52.65798°N、1.71605°E）、
TWD67 二度分帶（與 TWD97 在台北約相差 X + 828、Y - 207 公尺）。"""

from __future__ import annotations

import datetime as dt
import json
import struct
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tmig.authoring import inspect_file, new_workbook
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_sources, import_workbook
from tmig.data.ogr_files import companion_paths, gdb_in_zip, parse_wkb
from tmig.data.spatial_files import SpatialFileError, read_spatial
from tmig.edit import replace_worksheet
from tmig.geo.projection import crs_from_epsg, crs_from_prj, make_point_srid
from tmig.geo.spatial import parse_wkt
from tmig.ir import FileConnection
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from .test_file_sources import sheet

pyogrio = pytest.importorskip("pyogrio")
# 寫出測試用的 File Geodatabase 時，GDAL 提醒日期欄位存成日期時間（ArcGIS Pro 3.2 之前的格式）
pytestmark = pytest.mark.filterwarnings("ignore:Field .* will be written as a DateTime")

BNG = (651409.903, 313177.270)
"""OS 的範例點（British National Grid）。"""
TWD67_POINT = (305000.0, 2770000.0)


def point(x: float, y: float) -> bytes:
    return struct.pack("<BIdd", 1, 1, x, y)


def polygon(rings: list[list[tuple[float, float]]]) -> bytes:
    out = struct.pack("<BII", 1, 3, len(rings))
    for ring in rings:
        out += struct.pack("<I", len(ring)) + b"".join(struct.pack("<dd", *p) for p in ring)
    return out


def square(x: float, y: float, size: float) -> bytes:
    return polygon([[(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]])


def write_gdb(folder: Path) -> Path:
    """Sample.gdb：districts（TWD97 二度分帶的多邊形）與 stores（經緯度的點，含日期與整數）。"""
    from pyogrio import raw

    gdb = folder / "Sample.gdb"
    raw.write(
        str(gdb),
        np.array([square(300000, 2770000, 10000), square(250000, 2650000, 5000)], dtype=object),
        [np.array(["北區", "南區"], dtype=object), np.array([1.5, 2.25])],
        fields=["name", "score"],
        layer="districts",
        driver="OpenFileGDB",
        crs="EPSG:3826",
        geometry_type="Polygon",
        # 與 ArcGIS 建立的 feature class 相同有 Shape_Length、Shape_Area
        layer_options={"CREATE_SHAPE_AREA_AND_LENGTH_FIELDS": "YES"},
    )
    raw.write(
        str(gdb),
        np.array([point(121.5, 25.0), point(120.3, 22.6)], dtype=object),
        [
            np.array(["A", "B"], dtype=object),
            np.array([3, 4], dtype=np.int32),
            np.array(["2024-01-15", "2024-02-01"], dtype="datetime64[D]"),
        ],
        fields=["label", "staff", "opened"],
        layer="stores",
        driver="OpenFileGDB",
        crs="EPSG:4326",
        geometry_type="Point",
    )
    return gdb


def zipped(gdb: Path) -> bytes:
    out = gdb.with_name(gdb.name + ".zip")
    with zipfile.ZipFile(out, "w") as archive:
        for f in sorted(gdb.rglob("*")):
            archive.write(f, f"{gdb.name}/{f.relative_to(gdb).as_posix()}")
    return out.read_bytes()


def write_mapinfo(folder: Path) -> dict[str, Path]:
    """towns.tab（British National Grid）與 towns67.mif（TWD67 二度分帶）。"""
    from pyogrio import raw

    tab = folder / "towns.tab"
    raw.write(
        str(tab),
        np.array([point(*BNG)], dtype=object),
        [np.array(["Caister"], dtype=object), np.array([12.5])],
        fields=["town", "area"],
        driver="MapInfo File",
        crs="EPSG:27700",
        geometry_type="Point",
    )
    # MapInfo 的 TWD67 二度分帶（臺灣常用的 MAPINFOW.PRJ 寫法：自訂基準面 999、橢球 2 與三參數）
    mif = folder / "towns67.mif"
    mif.write_text(
        'Version 300\nCharset "Neutral"\nDelimiter ","\n'
        'CoordSys Earth Projection 8, 999, 2, -752, -358, -179, "m", 121, 0, 0.9999, 250000, 0\n'
        f"Columns 1\n  town Char(10)\nData\n\nPoint {TWD67_POINT[0]} {TWD67_POINT[1]}\n",
        encoding="utf-8",
    )
    mif.with_suffix(".mid").write_text('"Taipei"\n', encoding="utf-8")
    return {"tab": tab, "mif": mif}


def files_of(folder: Path, prefix: str = "") -> dict[str, bytes]:
    return {
        prefix + f.relative_to(folder).as_posix(): f.read_bytes()
        for f in sorted(folder.rglob("*"))
        if f.is_file()
    }


def coords(text: str | None) -> list[float]:
    geom = parse_wkt(text)
    assert geom is not None
    return [float(v) for v in str(geom["coordinates"]).replace("[", "").replace("]", "").split(",")]


def test_parse_wkb() -> None:
    assert parse_wkb(point(1.5, 2.5)) == {"type": "Point", "coordinates": [1.5, 2.5]}
    # Z 值略過；只有一個部分的 MULTIPOLYGON 寫成 POLYGON
    z = struct.pack("<BIddd", 1, 1001, 1.0, 2.0, 99.0)
    assert parse_wkb(z) == {"type": "Point", "coordinates": [1.0, 2.0]}
    multi = struct.pack("<BII", 1, 6, 1) + square(0, 0, 1)
    found = parse_wkb(multi)
    assert found is not None and found["type"] == "Polygon" and len(found["coordinates"][0]) == 5
    # EWKB（含 SRID）與大端序
    ewkb = struct.pack("<BIIdd", 1, 0x20000001, 4326, 3.0, 4.0)
    assert parse_wkb(ewkb) == {"type": "Point", "coordinates": [3.0, 4.0]}
    big = struct.pack(">BIdd", 0, 1, 5.0, 6.0)
    assert parse_wkb(big) == {"type": "Point", "coordinates": [5.0, 6.0]}
    assert parse_wkb(None) is None


def test_datum_shifts() -> None:
    """與 Tableau 相同一律換成 WGS84：其他橢球的基準面以 PROJ 轉換。"""
    found = coords(make_point_srid(*BNG, 27700))
    assert found == pytest.approx([1.71605, 52.65798], abs=2e-5)
    # TWD67 → WGS84，再換成 TWD97：與 TWD97 的座標約相差 X + 828、Y - 207 公尺
    lon, lat = coords(make_point_srid(*TWD67_POINT, 3828))
    x, y = crs_from_epsg(3826).forward([lon], [lat])
    assert float(x[0]) - TWD67_POINT[0] == pytest.approx(828, abs=3)
    assert float(y[0]) - TWD67_POINT[1] == pytest.approx(-207, abs=3)
    # WGS84、GRS80 的基準面（TWD97）不轉換基準面
    assert not crs_from_epsg(3826).shifted
    assert crs_from_epsg(27700).shifted
    # 公式不支援的投影（荷蘭的斜軸立體投影、瑞士的斜軸麥卡托）以 PROJ 計算
    assert coords(make_point_srid(155000, 463000, 28992)) == pytest.approx(
        [5.38720, 52.15517], abs=1e-4
    )
    assert coords(make_point_srid(2600000, 1200000, 2056)) == pytest.approx(
        [7.43863, 46.95108], abs=1e-4
    )
    # .prj（ESRI 的寫法）的 TWD67 也轉換
    prj = (
        'PROJCS["TWD_1967_TM_Taiwan",GEOGCS["GCS_TWD_1967",DATUM["D_TWD_1967",'
        'SPHEROID["GRS_1967_Truncated",6378160.0,298.25]],PRIMEM["Greenwich",0.0],'
        'UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["False_Easting",250000.0],PARAMETER["False_Northing",0.0],'
        'PARAMETER["Central_Meridian",121.0],PARAMETER["Scale_Factor",0.9999],'
        'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
    )
    shifted_lon, shifted_lat = crs_from_prj(prj).to_wgs84([TWD67_POINT[0]], [TWD67_POINT[1]])
    assert [float(shifted_lon[0]), float(shifted_lat[0])] == pytest.approx([lon, lat], abs=1e-6)


def test_read_geodatabase(tmp_path: Path) -> None:
    gdb = write_gdb(tmp_path)
    folder = files_of(tmp_path, "")
    districts = read_spatial(folder, "Sample.gdb", "districts")
    # 欄位依檔案中的順序（GDAL 建立的 feature class 中 Shape_Area、Shape_Length 在前），最後是 Geometry
    assert set(districts.columns) == {"name", "score", "Shape_Length", "Shape_Area", "Geometry"}
    assert districts.columns[-1] == "Geometry"
    north = districts.rows[0]
    assert north["name"] == "北區" and north["score"] == 1.5
    # TWD97 二度分帶（GRS80）：與公式的結果相同
    lon, lat = crs_from_epsg(3826).inverse([300000.0], [2770000.0])
    first = parse_wkt(north["Geometry"])
    assert first is not None and first["type"] == "Polygon"
    corner = min(first["coordinates"][0], key=lambda c: c[0] + c[1])
    assert corner == pytest.approx([float(lon[0]), float(lat[0])], abs=1e-6)
    # 壓縮成 .gdb.zip
    data = zipped(gdb)
    stores = read_spatial({"Sample.gdb.zip": data}, "Sample.gdb.zip", "stores")
    assert [r["label"] for r in stores.rows] == ["A", "B"]
    # File Geodatabase 的日期欄位是日期時間（ArcGIS Pro 3.2 之前沒有只有日期的型別）
    assert stores.rows[0]["staff"] == 3
    assert stores.rows[0]["opened"] == dt.datetime(2024, 1, 15)
    assert stores.rows[0]["Geometry"] == "POINT(121.5000000 25.0000000)"
    with pytest.raises(SpatialFileError, match="沒有資料表「parcels」"):
        read_spatial({"Sample.gdb.zip": data}, "Sample.gdb.zip", "parcels")
    names = list(folder)
    assert len(companion_paths(names, "Sample.gdb")) == len(names)
    assert gdb_in_zip(["x/Sample.gdb/a.gdbtable", "readme.txt"]) == ["x/Sample.gdb"]


def test_empty_shapes_with_datum_shift(tmp_path: Path) -> None:
    """空的形狀與沒有形狀的列：要轉換基準面的座標系統也照常讀（File Geodatabase 的 POINT EMPTY 讀成 Null，
    GeoJSON 的空形狀與 Hyper 相同寫成 MULTI* EMPTY）。"""
    from pyogrio import raw

    expected = "POINT(121.5532405 25.0352298)"
    empty = struct.pack("<BIdd", 1, 1, float("nan"), float("nan"))
    raw.write(
        str(tmp_path / "Empty.gdb"),
        np.array([point(*TWD67_POINT), empty, None], dtype=object),
        [np.array(["A", "B", "C"], dtype=object)],
        fields=["label"],
        layer="towns",
        driver="OpenFileGDB",
        crs="EPSG:3828",
        geometry_type="Point",
    )
    rows = read_spatial(files_of(tmp_path), "Empty.gdb", "towns").rows
    assert [r["Geometry"] for r in rows] == [expected, None, None]
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3828"}},
        "features": [
            {"type": "Feature", "properties": {}, "geometry": geometry}
            for geometry in (
                {"type": "Point", "coordinates": list(TWD67_POINT)},
                {"type": "Point", "coordinates": []},
                {"type": "LineString", "coordinates": []},
                None,
            )
        ],
    }
    data = json.dumps(collection).encode()
    rows = read_spatial({"towns.geojson": data}, "towns.geojson").rows
    assert [r["Geometry"] for r in rows] == [
        expected,
        "MULTIPOINT EMPTY",
        "MULTILINESTRING EMPTY",
        None,
    ]


def test_read_mapinfo(tmp_path: Path) -> None:
    paths = write_mapinfo(tmp_path)
    files = files_of(tmp_path)
    assert sorted(companion_paths(list(files), "towns.tab")) == [
        "towns.dat",
        "towns.id",
        "towns.map",
        "towns.tab",
    ]
    towns = read_spatial(files, "towns.tab")
    (row,) = towns.rows
    assert row["town"] == "Caister" and row["area"] == 12.5
    assert coords(row["Geometry"]) == pytest.approx([1.71605, 52.65798], abs=2e-5)
    old = read_spatial(files, "towns67.mif")
    assert old.rows[0]["town"] == "Taipei"
    # 三參數與七參數的差距在 1 公尺之內
    assert coords(old.rows[0]["Geometry"]) == pytest.approx(
        coords(make_point_srid(*TWD67_POINT, 3828)), abs=1e-5
    )
    assert paths["mif"].with_suffix(".mid").is_file()


def record(name: str, datatype: str, parent: str, ordinal: int) -> str:
    agg = {"real": "Sum", "integer": "Sum", "spatial": "Collect"}.get(datatype, "Count")
    return (
        f"<metadata-record class='column'><remote-name>{name}</remote-name>"
        f"<remote-type>129</remote-type><local-name>[{name}]</local-name>"
        f"<parent-name>[{parent}]</parent-name><remote-alias>{name}</remote-alias>"
        f"<ordinal>{ordinal}</ordinal><local-type>{datatype}</local-type>"
        f"<aggregation>{agg}</aggregation></metadata-record>"
    )


def spatial_datasource(name: str, filename: str, table: str, columns: list[tuple[str, str]]) -> str:
    records = "".join(record(c, t, table, i) for i, (c, t) in enumerate(columns))
    return f"""
    <datasource caption='{table}' inline='true' name='federated.{name}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='{table}' name='ogrdirect.{name}'>
            <connection class='ogrdirect' directory='C:/gis' filename='{filename}'
              ogr-grid-shift-folder='PROJ4' password='' server='' tablename='' />
          </named-connection>
        </named-connections>
        <relation connection='ogrdirect.{name}' name='{table}' table='[{table}]' type='table' />
        <metadata-records>{records}</metadata-records>
      </connection>
      <aliases enabled='yes' />
    </datasource>"""


def run(ir: Any, folder: Path, ds_index: int, rows: str, measure: str) -> dict[Any, Any]:
    ds = ir.datasources[ds_index]
    f = {x.caption: x.id for x in ds.fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        datasource=ds.id, rows=[{"field": f[rows]}], columns=[{"field": f[measure], "agg": "sum"}]
    )
    ir, ws = replace_worksheet(ir, sheet)
    found = execute_query(folder / f"{ds.id}.duckdb", worksheet_sql(ir, ws.id))
    return {row[0]: row[1] for row in found.rows}


def test_twbx_with_geodatabase_and_mapinfo(tmp_path: Path) -> None:
    """.twbx 中的 File Geodatabase（資料夾與 .gdb.zip）與 MapInfo：解析、匯入後查詢。"""
    source = tmp_path / "source"
    source.mkdir()
    gdb = write_gdb(source)
    write_mapinfo(source)
    datasources = (
        spatial_datasource(
            "folder",
            "Sample.gdb",
            "districts",
            [
                ("name", "string"),
                ("score", "real"),
                ("Shape_Length", "real"),
                ("Shape_Area", "real"),
                ("Geometry", "spatial"),
            ],
        )
        + spatial_datasource(
            "zipped",
            "Sample.gdb.zip",
            "stores",
            [
                ("label", "string"),
                ("staff", "integer"),
                ("opened", "datetime"),
                ("Geometry", "spatial"),
            ],
        )
        + spatial_datasource(
            "tab",
            "towns.tab",
            "towns",
            [("town", "string"), ("area", "real"), ("Geometry", "spatial")],
        )
    )
    xml = b.workbook(
        datasource=datasources, worksheets=sheet("Sheet", "federated.folder", "name", "score")
    )
    package = {
        **files_of(gdb, "Data/Sample.gdb/"),
        "Data/Sample.gdb.zip": zipped(gdb),
        **{f"Data/{k}": v for k, v in files_of(source).items() if k.startswith("towns.")},
    }
    twbx = b.write_twbx(tmp_path, xml, package)
    ir = parse_workbook(twbx, lenient=True)
    assert [u.reason for u in ir.unsupported] == []
    folder_ds, zip_ds, tab_ds = ir.datasources
    assert isinstance(tab_ds.connection, FileConnection)
    assert tab_ds.connection.tables[0].path == "Data/towns.tab"
    assert isinstance(folder_ds.connection, FileConnection)
    assert folder_ds.connection.tables[0].path == "Data/Sample.gdb"
    assert isinstance(zip_ds.connection, FileConnection)
    assert zip_ds.connection.tables[0].path == "Data/Sample.gdb.zip"
    data = tmp_path / "data"
    results = import_workbook(twbx, data, ir)
    assert all(r.ok for r in results), [r.to_json() for r in results]
    assert run(ir, data, 0, "name", "score") == {"北區": 1.5, "南區": 2.25}
    assert run(ir, data, 1, "label", "staff") == {"A": 3, "B": 4}
    assert run(ir, data, 2, "town", "area") == {"Caister": 12.5}


def test_desktop_connects_to_geodatabase_and_mapinfo(tmp_path: Path) -> None:
    source = tmp_path / "author"
    source.mkdir()
    gdb = write_gdb(source)
    paths = write_mapinfo(source)
    inspected = inspect_file(gdb)
    assert [t.name for t in inspected.tables] == ["districts", "stores"]
    assert [c.datatype for c in inspected.table("stores").columns][-1] == "spatial"
    (source / "Sample.gdb.zip").write_bytes(zipped(gdb))
    assert [t.name for t in inspect_file(source / "Sample.gdb.zip").tables] == [
        "districts",
        "stores",
    ]
    tab = inspect_file(paths["tab"])
    assert {p.suffix for p in tab.sidecars} == {".dat", ".map", ".id"}
    # 以 .gdb.zip 的 stores 建立報表，匯入後查詢
    sources = tmp_path / "report" / "sources"
    (sources / "Data").mkdir(parents=True)
    (sources / "Data" / "Sample.gdb.zip").write_bytes((source / "Sample.gdb.zip").read_bytes())
    ir = new_workbook(
        "GDB", inspect_file(source / "Sample.gdb.zip"), "stores", "Data/Sample.gdb.zip"
    )
    (result,) = import_sources(sources, tmp_path / "report" / "data", ir)
    assert result.ok, result.to_json()
    found = run(ir, tmp_path / "report" / "data", 0, "label", "staff")
    assert found == {"A": 3, "B": 4}
