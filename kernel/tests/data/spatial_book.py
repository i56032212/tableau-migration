"""測試用的空間資料報表：依真實 Tableau 檔案的結構手寫（空間檔案的連線、spatial 型別的欄位、
COLLECT 的欄位實例、標記卡上的 geometry 編碼）。

- Counties：萃取（.hyper 的 GEOGRAPHY 欄位），臺灣各縣市的形狀（平台的 Natural Earth 資料）與人口
- Stores：沒有萃取的 Shapefile（壓縮成 .zip），點的座標是 TWD97 二度分帶（EPSG:3826，.prj），屬性的文字是 UTF-8
- Cities：沒有萃取的 CSV，以 MAKEPOINT、DISTANCE、BUFFER、MAKELINE、AREA 的計算欄位畫成地圖
- Place Counties：CSV 與 GeoJSON 的空間 join（Intersects，left join）：地點以 MAKEPOINT 對到所在的縣市
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

from tmig.data.spatial_files import read_topojson
from tmig.geo import SHAPES
from tmig.geo.projection import crs_from_epsg
from tmig.geo.spatial import parse_wkt

POPULATION = {
    "Taipei City": 2_490_000,
    "New Taipei City": 4_000_000,
    "Taoyuan": 2_290_000,
    "Taichung City": 2_820_000,
    "Tainan City": 1_860_000,
    "Kaohsiung City": 2_740_000,
}
STORES = [
    ("台北旗艦店", 25.0339, 121.5645, 980.5, "20190105"),
    ("台中店", 24.1477, 120.6736, 420.0, "20200301"),
    ("高雄店", 22.6273, 120.3014, 515.25, "20210720"),
    ("花蓮店", 23.9769, 121.6044, 130.0, ""),
]
CITIES = [
    ("Taipei", 25.0339, 121.5645),
    ("Hsinchu", 24.8138, 120.9675),
    ("Taichung", 24.1477, 120.6736),
    ("Tainan", 22.9997, 120.2270),
    ("Kaohsiung", 22.6273, 120.3014),
]
PLACES = [
    ("Taipei 101", 25.0339, 121.5645, "Taipei City"),
    ("Sun Moon Lake", 23.8650, 120.9150, "Nantou"),
    ("Magong", 23.5654, 119.5793, "Penghu"),
    ("Hualien Station", 23.9929, 121.6011, "Hualien"),
    ("Keelung Port", 25.1276, 121.7392, "Keelung City"),
    # 臺灣海峽：沒有對到任何縣市（left join 的縣市為 Null）
    ("Taiwan Strait", 24.5, 119.5, None),
]
TWD97_PRJ = (
    'PROJCS["TWD_1997_TM_Taiwan",GEOGCS["GCS_TWD_1997",DATUM["D_TWD_1997",'
    'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",250000.0],PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",121.0],PARAMETER["Scale_Factor",0.9999],'
    'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
)


def county_rows() -> list[tuple[str, int | None, str | None]]:
    """縣市名稱、人口與形狀的 WKT。"""
    topology = json.loads((SHAPES / "admin1" / "TWN.json").read_text(encoding="utf-8"))
    table = read_topojson(topology, "admin1", "TWN.json")
    # 六都以外的人口以名稱長度推算（只用來著色，數字不重要）
    return [
        (
            str(row["name"]),
            POPULATION.get(str(row["name"]), 90_000 * len(str(row["name"]))),
            row["Geometry"],
        )
        for row in table.rows
    ]


def counties_geojson() -> bytes:
    """縣市的 GeoJSON（空間 join 的右側）。"""
    features = [
        {"type": "Feature", "properties": {"Name": name}, "geometry": parse_wkt(wkt)}
        for name, _, wkt in county_rows()
    ]
    return json.dumps({"type": "FeatureCollection", "features": features}).encode("utf-8")


# ---------------------------------------------------------------- Shapefile


def dbf(fields: list[tuple[str, str, int, int]], rows: list[list[object]], encoding: str) -> bytes:
    header_length = 32 + 32 * len(fields) + 1
    record_length = 1 + sum(f[2] for f in fields)
    out = io.BytesIO()
    out.write(struct.pack("<BBBBIHH20x", 3, 124, 1, 1, len(rows), header_length, record_length))
    for name, kind, length, decimals in fields:
        raw = name.encode(encoding)[:10].ljust(11, b"\x00")
        out.write(raw + kind.encode() + b"\x00" * 4 + bytes([length, decimals]) + b"\x00" * 14)
    out.write(b"\x0d")
    for row in rows:
        out.write(b" ")
        for (_, kind, length, decimals), value in zip(fields, row, strict=True):
            if kind == "C":
                raw = str(value).encode(encoding)[:length].ljust(length)
            elif kind == "N":
                text = "" if value is None else f"{float(value):.{decimals}f}"  # type: ignore[arg-type]
                raw = text.rjust(length).encode("ascii")
            else:
                raw = str(value or "").ljust(length).encode("ascii")
            out.write(raw)
    out.write(b"\x1a")
    return out.getvalue()


def shp_points(points: list[tuple[float, float]]) -> tuple[bytes, bytes]:
    """點的 .shp 與 .shx。"""
    records = b""
    index = b""
    offset = 50
    for i, (x, y) in enumerate(points):
        content = struct.pack("<idd", 1, x, y)
        records += struct.pack(">ii", i + 1, len(content) // 2) + content
        index += struct.pack(">ii", offset, len(content) // 2)
        offset += 4 + len(content) // 2
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    bbox = (min(xs), min(ys), max(xs), max(ys))

    def header(length_words: int) -> bytes:
        return (
            struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, length_words)
            + struct.pack("<ii", 1000, 1)
            + struct.pack("<4d", *bbox)
            + struct.pack("<4d", 0, 0, 0, 0)
        )

    shp = header(50 + len(records) // 2) + records
    shx = header(50 + len(index) // 2) + index
    return shp, shx


def shp_polygons(shapes: list[list[list[tuple[float, float]]]], kind: int = 5) -> bytes:
    """多邊形（kind 5）或線（kind 3）的 .shp：每個形狀是幾個部分（環或線）。"""
    records = b""
    xs: list[float] = []
    ys: list[float] = []
    for i, parts in enumerate(shapes):
        points = [p for part in parts for p in part]
        xs += [p[0] for p in points]
        ys += [p[1] for p in points]
        starts: list[int] = []
        at = 0
        for part in parts:
            starts.append(at)
            at += len(part)
        box = (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        )
        content = (
            struct.pack("<i4d2i", kind, *box, len(parts), len(points))
            + struct.pack(f"<{len(starts)}i", *starts)
            + b"".join(struct.pack("<2d", *p) for p in points)
        )
        records += struct.pack(">ii", i + 1, len(content) // 2) + content
    header = (
        struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, 50 + len(records) // 2)
        + struct.pack("<ii", 1000, kind)
        + struct.pack("<4d", min(xs), min(ys), max(xs), max(ys))
        + struct.pack("<4d", 0, 0, 0, 0)
    )
    return header + records


def stores_zip() -> bytes:
    crs = crs_from_epsg(3826)
    xs, ys = crs.forward([s[2] for s in STORES], [s[1] for s in STORES])
    shp, shx = shp_points(list(zip(xs.tolist(), ys.tolist(), strict=True)))
    table = dbf(
        [("Store", "C", 40, 0), ("Sales", "N", 12, 2), ("Opened", "D", 8, 0)],
        [[s[0], s[3], s[4]] for s in STORES],
        "utf-8",
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("stores/stores.shp", shp)
        archive.writestr("stores/stores.shx", shx)
        archive.writestr("stores/stores.dbf", table)
        archive.writestr("stores/stores.prj", TWD97_PRJ)
        archive.writestr("stores/stores.cpg", "UTF-8")
    return out.getvalue()


# ---------------------------------------------------------------- 萃取


def counties_hyper(path: Path, log_dir: Path) -> None:
    from tableauhyperapi import (
        Connection,
        CreateMode,
        HyperProcess,
        Inserter,
        SqlType,
        TableDefinition,
        TableName,
        Telemetry,
    )

    table = TableDefinition(
        TableName("Extract", "Extract"),
        [
            TableDefinition.Column("Name", SqlType.text()),
            TableDefinition.Column("Population", SqlType.big_int()),
            TableDefinition.Column("Geometry", SqlType.geography()),
        ],
    )
    staged = [
        TableDefinition.Column("Name", SqlType.text()),
        TableDefinition.Column("Population", SqlType.big_int()),
        TableDefinition.Column("wkt", SqlType.text()),
    ]
    mapping = [
        Inserter.ColumnMapping("Name"),
        Inserter.ColumnMapping("Population"),
        Inserter.ColumnMapping("Geometry", 'CAST("wkt" AS GEOGRAPHY)'),
    ]
    log_dir.mkdir(parents=True, exist_ok=True)
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, str(path), CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        conn.catalog.create_table(table)
        with Inserter(conn, table, mapping, inserter_definition=staged) as inserter:
            inserter.add_rows([list(row) for row in county_rows()])
            inserter.execute()


# ---------------------------------------------------------------- 報表


def _record(name: str, datatype: str, remote: int, parent: str, ordinal: int, agg: str) -> str:
    return (
        f"<metadata-record class='column'><remote-name>{name}</remote-name>"
        f"<remote-type>{remote}</remote-type><local-name>[{name}]</local-name>"
        f"<parent-name>[{parent}]</parent-name><remote-alias>{name}</remote-alias>"
        f"<ordinal>{ordinal}</ordinal><local-type>{datatype}</local-type>"
        f"<aggregation>{agg}</aggregation><contains-null>true</contains-null></metadata-record>"
    )


SPATIAL_COLUMN = "<column datatype='spatial' name='[Geometry]' role='measure' type='nominal' />"


def _calc(name: str, caption: str, formula: str, datatype: str) -> str:
    role = "measure"
    kind = "nominal" if datatype == "spatial" else "quantitative"
    return (
        f"<column caption={quoteattr(caption)} datatype='{datatype}' name='[{name}]' role='{role}' "
        f"type='{kind}'><calculation class='tableau' formula={quoteattr(formula)} /></column>"
    )


CALCS = [
    ("Calculation_point", "Point", "MAKEPOINT([Lat], [Lon])", "spatial"),
    (
        "Calculation_distance",
        "Distance to Taipei",
        "DISTANCE([Point], MAKEPOINT(25.0339, 121.5645), 'km')",
        "real",
    ),
    ("Calculation_buffer", "Buffer", "BUFFER([Point], 15, 'km')", "spatial"),
    ("Calculation_route", "Route", "MAKELINE(MAKEPOINT(25.0339, 121.5645), [Point])", "spatial"),
    ("Calculation_area", "Buffer Area", "AREA([Buffer], 'km')", "real"),
]


PLACE_POINT = ("Calculation_place_point", "Place Point", "MAKEPOINT([Lat], [Lon])", "spatial")


def datasources_xml() -> str:
    counties = f"""
    <datasource caption='Counties' inline='true' name='federated.counties' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='counties' name='ogrdirect.counties'>
            <connection class='ogrdirect' directory='C:/gis' filename='counties.shp' password='' server='' tablename='' />
          </named-connection>
        </named-connections>
        <relation connection='ogrdirect.counties' name='counties' table='[counties]' type='table' />
      </connection>
      <extract count='-1' enabled='true' units='records'>
        <connection class='hyper' dbname='Data/Extracts/counties.hyper' schema='Extract' tablename='Extract'>
          <relation name='Extract' table='[Extract].[Extract]' type='table' />
          <metadata-records>
            {_record("Name", "string", 129, "Extract", 0, "Count")}
            {_record("Population", "integer", 20, "Extract", 1, "Sum")}
            {_record("Geometry", "spatial", 128, "Extract", 2, "Collect")}
          </metadata-records>
        </connection>
      </extract>
      <aliases enabled='yes' />
      {SPATIAL_COLUMN}
    </datasource>"""
    stores = f"""
    <datasource caption='Stores' inline='true' name='federated.stores' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='stores' name='ogrdirect.stores'>
            <connection class='ogrdirect' directory='C:/gis' filename='stores.zip' password='' server='' tablename='' />
          </named-connection>
        </named-connections>
        <relation connection='ogrdirect.stores' name='stores' table='[stores]' type='table'>
          <columns>
            <column datatype='string' name='Store' ordinal='0' />
            <column datatype='real' name='Sales' ordinal='1' />
            <column datatype='date' name='Opened' ordinal='2' />
            <column datatype='spatial' name='Geometry' ordinal='3' />
          </columns>
        </relation>
        <metadata-records>
          {_record("Store", "string", 129, "stores", 0, "Count")}
          {_record("Sales", "real", 5, "stores", 1, "Sum")}
          {_record("Opened", "date", 7, "stores", 2, "Year")}
          {_record("Geometry", "spatial", 8, "stores", 3, "Collect")}
        </metadata-records>
      </connection>
      <aliases enabled='yes' />
      {SPATIAL_COLUMN}
    </datasource>"""
    calcs = "".join(_calc(*c) for c in CALCS)
    cities = f"""
    <datasource caption='Cities' inline='true' name='federated.cities' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='cities' name='textscan.cities'>
            <connection class='textscan' directory='C:/data' filename='cities.csv' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.cities' name='cities.csv' table='[cities#csv]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
            <column datatype='string' name='City' ordinal='0' />
            <column datatype='real' name='Lat' ordinal='1' />
            <column datatype='real' name='Lon' ordinal='2' />
          </columns>
        </relation>
        <metadata-records>
          {_record("City", "string", 129, "cities.csv", 0, "Count")}
          {_record("Lat", "real", 5, "cities.csv", 1, "Sum")}
          {_record("Lon", "real", 5, "cities.csv", 2, "Sum")}
        </metadata-records>
      </connection>
      <aliases enabled='yes' />
      {calcs}
    </datasource>"""
    places = f"""
    <datasource caption='Place Counties' inline='true' name='federated.places' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='places' name='textscan.places'>
            <connection class='textscan' directory='C:/data' filename='places.csv' password='' server='' />
          </named-connection>
          <named-connection caption='counties' name='ogrdirect.geojson'>
            <connection class='ogrdirect' directory='C:/gis' filename='counties.geojson' ogr-grid-shift-folder='PROJ4' password='' server='' tablename='' />
          </named-connection>
        </named-connections>
        <relation join='left' type='join'>
          <clause type='join'>
            <expression op='Intersects'>
              <expression op='MAKEPOINT([places.csv].[Lat],[places.csv].[Lon])' />
              <expression op='[counties.geojson].[Geometry]' />
            </expression>
          </clause>
          <relation connection='textscan.places' name='places.csv' table='[places#csv]' type='table'>
            <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
              <column datatype='string' name='Place' ordinal='0' />
              <column datatype='real' name='Lat' ordinal='1' />
              <column datatype='real' name='Lon' ordinal='2' />
            </columns>
          </relation>
          <relation connection='ogrdirect.geojson' name='counties.geojson' table='[counties.geojson]' type='table'>
            <columns>
              <column datatype='string' name='Name' ordinal='0' />
              <column datatype='spatial' name='Geometry' ordinal='1' />
            </columns>
          </relation>
        </relation>
        <metadata-records>
          {_record("Place", "string", 129, "places.csv", 0, "Count")}
          {_record("Lat", "real", 5, "places.csv", 1, "Sum")}
          {_record("Lon", "real", 5, "places.csv", 2, "Sum")}
          {_record("Name", "string", 129, "counties.geojson", 3, "Count")}
          {_record("Geometry", "spatial", 8, "counties.geojson", 4, "Collect")}
        </metadata-records>
      </connection>
      <aliases enabled='yes' />
      {SPATIAL_COLUMN}
      {_calc(*PLACE_POINT)}
    </datasource>"""
    return counties + stores + cities + places


def _instance(column: str, derivation: str, name: str, kind: str = "nominal") -> str:
    return (
        f"<column-instance column='[{column}]' derivation='{derivation}' name='[{name}]' "
        f"pivot='key' type='{kind}' />"
    )


def map_sheet(name: str, ds: str, deps: str, encodings: str) -> str:
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources><datasource name='{ds}' /></datasources>
          <mapsources><mapsource name='Tableau' /></mapsources>
          <datasource-dependencies datasource='{ds}'>{deps}</datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes>
          <pane selection-relaxation-option='selection-relaxation-allow'>
            <view><breakdown value='auto' /></view>
            <mark class='Automatic' />
            <encodings>{encodings}</encodings>
          </pane>
        </panes>
        <rows>[{ds}].[Latitude (generated)]</rows>
        <cols>[{ds}].[Longitude (generated)]</cols>
      </table>
    </worksheet>"""


def worksheets_xml() -> str:
    c = "federated.counties"
    county = map_sheet(
        "County Map",
        c,
        SPATIAL_COLUMN
        + _instance("Geometry", "Collect", "clct:Geometry:nk")
        + _instance("Name", "None", "none:Name:nk")
        + _instance("Population", "Sum", "sum:Population:qk", "quantitative"),
        f"<color column='[{c}].[sum:Population:qk]' />"
        f"<lod column='[{c}].[clct:Geometry:nk]' />"
        f"<lod column='[{c}].[none:Name:nk]' />"
        f"<text column='[{c}].[none:Name:nk]' />"
        f"<geometry column='[{c}].[clct:Geometry:nk]' />",
    )
    s = "federated.stores"
    stores = map_sheet(
        "Store Map",
        s,
        SPATIAL_COLUMN
        + _instance("Geometry", "Collect", "clct:Geometry:nk")
        + _instance("Store", "None", "none:Store:nk")
        + _instance("Sales", "Sum", "sum:Sales:qk", "quantitative"),
        f"<size column='[{s}].[sum:Sales:qk]' />"
        f"<lod column='[{s}].[clct:Geometry:nk]' />"
        f"<lod column='[{s}].[none:Store:nk]' />"
        f"<geometry column='[{s}].[clct:Geometry:nk]' />",
    )
    t = "federated.cities"
    deps = "".join(_calc(*calc) for calc in CALCS) + _instance("City", "None", "none:City:nk")
    buffers = map_sheet(
        "Buffers",
        t,
        deps + _instance("Calculation_buffer", "Collect", "clct:Calculation_buffer:nk"),
        f"<lod column='[{t}].[clct:Calculation_buffer:nk]' />"
        f"<lod column='[{t}].[none:City:nk]' />"
        f"<text column='[{t}].[none:City:nk]' />"
        f"<geometry column='[{t}].[clct:Calculation_buffer:nk]' />",
    )
    routes = map_sheet(
        "Routes",
        t,
        deps + _instance("Calculation_route", "Collect", "clct:Calculation_route:nk"),
        f"<color column='[{t}].[none:City:nk]' />"
        f"<lod column='[{t}].[clct:Calculation_route:nk]' />"
        f"<geometry column='[{t}].[clct:Calculation_route:nk]' />",
    )
    distances = f"""
    <worksheet name='Distances'>
      <table>
        <view>
          <datasources><datasource name='{t}' /></datasources>
          <datasource-dependencies datasource='{t}'>
            {deps}
            {_instance("Calculation_distance", "Sum", "sum:Calculation_distance:qk", "quantitative")}
            {_instance("Calculation_area", "Sum", "sum:Calculation_area:qk", "quantitative")}
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Bar' />
          <encodings><text column='[{t}].[sum:Calculation_area:qk]' /></encodings>
        </pane></panes>
        <rows>[{t}].[none:City:nk]</rows>
        <cols>[{t}].[sum:Calculation_distance:qk]</cols>
      </table>
    </worksheet>"""
    p = "federated.places"
    places = f"""
    <worksheet name='Places per County'>
      <table>
        <view>
          <datasources><datasource name='{p}' /></datasources>
          <datasource-dependencies datasource='{p}'>
            {_instance("Name", "None", "none:Name:nk")}
            {_instance("Place", "Count", "cnt:Place:qk", "quantitative")}
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Automatic' />
          <encodings><text column='[{p}].[cnt:Place:qk]' /></encodings>
        </pane></panes>
        <rows>[{p}].[none:Name:nk]</rows>
        <cols />
      </table>
    </worksheet>"""
    # 2021.2 的地圖圖層：圖層的元素與屬性帶功能旗標的前綴，第二個圖層的卡上有兩個空間欄位
    # （沒有 geometry 編碼，依圖層的名稱決定形狀）
    clct = _instance("Geometry", "Collect", "clct:Geometry:nk")
    point = _instance("Calculation_place_point", "Collect", "clct:Calculation_place_point:nk")
    layers = f"""
    <worksheet name='Place Layers'>
      <table>
        <view>
          <datasources><datasource name='{p}' /></datasources>
          <mapsources><mapsource name='Tableau' /></mapsources>
          <datasource-dependencies datasource='{p}'>
            {SPATIAL_COLUMN}{_calc(*PLACE_POINT)}{clct}{point}
            {_instance("Place", "Count", "cnt:Place:qk", "quantitative")}
            {_instance("Place", "None", "none:Place:nk")}
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes _.fcp.Layers.true...customization-axis='layer'>
          <_.fcp.Layers.true...pane id='1'><view><breakdown value='auto' /></view><mark class='Automatic' /></_.fcp.Layers.true...pane>
          <pane _.fcp.Layers.true...generated-title='Geometry' id='0'>
            <view><breakdown value='auto' /></view><mark class='Automatic' />
            <encodings>
              <color column='[{p}].[cnt:Place:qk]' />
              <lod column='[{p}].[clct:Geometry:nk]' />
              <geometry column='[{p}].[clct:Geometry:nk]' />
            </encodings>
          </pane>
          <_.fcp.Layers.true...pane generated-title='Place Point' id='2'>
            <view><breakdown value='auto' /></view><mark class='Automatic' />
            <encodings>
              <lod column='[{p}].[none:Place:nk]' />
              <lod column='[{p}].[clct:Geometry:nk]' />
              <lod column='[{p}].[clct:Calculation_place_point:nk]' />
            </encodings>
          </_.fcp.Layers.true...pane>
        </panes>
        <rows>[{p}].[Latitude (generated)]</rows>
        <cols>[{p}].[Longitude (generated)]</cols>
      </table>
    </worksheet>"""
    return county + stores + buffers + routes + distances + places + layers


def workbook_xml() -> str:
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2021.2.7 (20212.22.0617.1123)' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences />
  <datasources>{datasources_xml()}</datasources>
  <worksheets>{worksheets_xml()}</worksheets>
  <windows />
</workbook>
"""


def write_spatial_twbx(folder: Path) -> Path:
    """寫出 spatial.twbx（含萃取、Shapefile 壓縮檔與 CSV）。"""
    folder.mkdir(parents=True, exist_ok=True)
    hyper = folder / "counties.hyper"
    counties_hyper(hyper, folder / "hyper-log")
    csv = "City,Lat,Lon\r\n" + "".join(f"{c},{lat},{lon}\r\n" for c, lat, lon in CITIES)
    places = "Place,Lat,Lon\r\n" + "".join(f"{n},{lat},{lon}\r\n" for n, lat, lon, _ in PLACES)
    path = folder / "spatial.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("spatial.twb", workbook_xml().encode("utf-8"))
        archive.write(hyper, "Data/Extracts/counties.hyper")
        archive.writestr("Data/gis/stores.zip", stores_zip())
        archive.writestr("Data/data/cities.csv", csv.encode("utf-8"))
        archive.writestr("Data/data/places.csv", places.encode("utf-8"))
        archive.writestr("Data/gis/counties.geojson", counties_geojson())
    return path
