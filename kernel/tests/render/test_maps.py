"""地圖（Latitude／Longitude (generated)）：解析（地理角色、編輯位置、背景地圖、標記圖層、存檔的範圍）與
規格（填色地圖、符號地圖、密度圖、標記圖層、未知的位置）。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute_query
from tmig.ir.models import IR, GeoAlias, GeoSettings, MapOptions, MapViewport
from tmig.parser.workbook import parse_workbook
from tmig.render.maps import DECO, MAP_SEQUENTIAL
from tmig.render.palettes import PALETTES
from tmig.render.vegalite import RenderResult, build_spec
from tmig.sqlgen.query import pane_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, worksheet
from .test_vegalite import validator

COLUMNS = [
    ("Country", "string", "Count"),
    ("State", "string", "Count"),
    ("City", "string", "Count"),
    ("Zip", "integer", "Sum"),
    ("Sales", "real", "Sum"),
    ("Lat", "real", "Avg"),
    ("Lon", "real", "Avg"),
]
ROWS = [
    ("United States", "Texas", "Houston", 77002, 100.0, 29.76, -95.37),
    ("United States", "Texas", "Dallas", 75201, 50.0, 32.78, -96.80),
    ("United States", "California", "Los Angeles", 90012, 300.0, 34.05, -118.24),
    ("United States", "Florda", "Miami", 33130, 80.0, 25.76, -80.19),
    ("United States", "Atlantis", "Nowhere", 99999, 10.0, None, None),
]
GEO_COLUMNS = """
  <column datatype='string' name='[Country]' role='dimension' semantic-role='[Country].[ISO3166_2]' type='nominal' />
  <column datatype='string' name='[State]' role='dimension' semantic-role='[State].[Name]' type='nominal'>
    <semantic-values semantic-role='[State].[Name]'>
      <semantic-value key='&quot;Florda&quot;' value='&quot;Florida&quot;' />
    </semantic-values>
  </column>
  <column datatype='string' name='[City]' role='dimension' semantic-role='[City].[Name]' type='nominal' />
  <column datatype='integer' name='[Zip]' role='dimension' semantic-role='[ZipCode].[Name]' type='ordinal' />
  <column datatype='real' name='[Lat]' role='measure' semantic-role='[Geographical].[Latitude]' type='quantitative' />
  <column datatype='real' name='[Lon]' role='measure' semantic-role='[Geographical].[Longitude]' type='quantitative' />
  <semantic-values>
    <semantic-value key='[Country].[Name]' value='&quot;United States&quot;' />
    <semantic-value key='[State].[Name]' value='%null%' />
  </semantic-values>
"""
STATE = instance("[State]", "None", "[none:State:nk]")
CITY = instance("[City]", "None", "[none:City:nk]")
ZIP = instance("[Zip]", "None", "[none:Zip:ok]", "ordinal")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
LAT = instance("[Lat]", "Avg", "[avg:Lat:qk]", "quantitative")
LON = instance("[Lon]", "Avg", "[avg:Lon:qk]", "quantitative")
GEN_LAT = b.ref("Latitude (generated)")
GEN_LON = b.ref("Longitude (generated)")
GEOMETRY = f"<geometry column='{b.ref('Geometry (generated)')}' />"


def metadata() -> str:
    records = [
        f"""<metadata-record class='column'><remote-name>{name}</remote-name><remote-type>129</remote-type>
        <local-name>[{name}]</local-name><parent-name>[Extract]</parent-name><ordinal>{i}</ordinal>
        <local-type>{datatype}</local-type><aggregation>{agg}</aggregation></metadata-record>"""
        for i, (name, datatype, agg) in enumerate(COLUMNS)
    ]
    return "<metadata-records>" + "".join(records) + "</metadata-records>"


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("geo") / "geo.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Country" VARCHAR, "State" VARCHAR, "City" VARCHAR, '
            '"Zip" BIGINT, "Sales" DOUBLE, "Lat" DOUBLE, "Lon" DOUBLE)'
        )
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', ROWS)
    return path


def parse(tmp_path: Path, sheets: str) -> IR:
    datasource = b.extract_datasource(columns=GEO_COLUMNS, extract_inner=metadata())
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(datasource, worksheets=sheets)))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


def render(ir: IR, db: Path, name: str | None = None) -> RenderResult:
    ws = next(w for w in ir.worksheets if name is None or w.name == name)
    query = worksheet_sql(ir, ws.id)
    panes = {i: (q.columns, execute_query(db, q)) for i, q in pane_sql(ir, ws.id, query).items()}
    rendered = build_spec(ir, ws.id, query.columns, execute_query(db, query), None, panes or None)
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered


def data_layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [layer for layer in spec["layer"] if not str(layer.get("name", "")).startswith(DECO)]


def map_sheet(name: str, instances: list[str], mark: str, encodings: str, **kwargs: str) -> str:
    return worksheet(
        name, instances, rows=GEN_LAT, cols=GEN_LON, mark=mark, encodings=encodings, **kwargs
    )


def test_geographic_roles_and_settings(tmp_path: Path) -> None:
    sheet = map_sheet(
        "Filled",
        [STATE, SALES],
        "Multipolygon",
        f"<lod column='{b.ref('none:State:nk')}' />{GEOMETRY}",
    )
    ir = parse(tmp_path, sheet)
    ds = ir.datasources[0]
    roles = {f.caption: f.geo_role for f in ds.fields if f.geo_role}
    assert roles == {
        "Country": "country",
        "State": "state",
        "City": "city",
        "Zip": "zip",
        "Lat": "latitude",
        "Lon": "longitude",
    }
    state = next(f for f in ds.fields if f.caption == "State")
    assert state.geo_aliases == [GeoAlias(value="Florda", location="Florida")]
    assert ds.geo == GeoSettings(country="United States")
    ws = ir.worksheets[0]
    assert [u.field for u in ws.rows] == ["latitude_generated"]
    assert [u.field for u in ws.columns] == ["longitude_generated"]
    assert ws.marks.type == "map" and ws.marks.geometry is not None
    assert ws.map is None


def test_filled_map(tmp_path: Path, db: Path) -> None:
    """填色地圖：州的形狀以 TopoJSON 查出（瀏覽器取得），Blue-Teal 的自動色盤，標籤在州的中央；
    編輯位置的 Florda 對應到 Florida，找不到的 Atlantis 列為未知。"""
    sheet = map_sheet(
        "Filled",
        [STATE, SALES],
        "Multipolygon",
        f"<color column='{b.ref('sum:Sales:qk')}' /><text column='{b.ref('none:State:nk')}' />"
        f"<lod column='{b.ref('none:State:nk')}' />{GEOMETRY}",
    )
    rendered = render(parse(tmp_path, sheet), db)
    spec = rendered.spec
    assert spec is not None and rendered.mark == "map" and rendered.rows == 4
    projection = spec["projection"]
    assert projection["type"] == "mercator"
    (west, south), (east, north) = projection["fit"]["geometry"]["coordinates"]
    assert -128 < west < -120 and -82 < east < -75 and 22 < south < 27 and 40 < north < 45
    polygons = [layer for layer in data_layers(spec) if layer["mark"]["type"] == "geoshape"]
    assert len(polygons) == 1
    lookup = polygons[0]["transform"][1]["lookup"]
    assert lookup == "__feature0"
    source = polygons[0]["transform"][1]["from"]["data"]
    assert source["url"].endswith("/admin1/USA.json") and source["format"]["feature"] == "admin1"
    assert polygons[0]["encoding"]["shape"] == {"field": "__geo", "type": "geojson"}
    scale = polygons[0]["encoding"]["color"]["scale"]
    assert scale["range"][0] == PALETTES[MAP_SEQUENTIAL].colors[0]
    values = {r["c0"]: r for r in spec["data"]["values"]}
    assert values["Florda"]["__shape0"] == "admin1/USA" and values["Texas"]["__lon0"] < -97
    assert values["Atlantis"]["__lon0"] is None
    labels = [layer for layer in data_layers(spec) if layer["mark"]["type"] == "text"]
    assert labels and labels[0]["mark"]["align"] == "center"
    assert {r["__map_label0"] for r in spec["data"]["values"]} >= {"Texas", "California"}
    unknown = next(layer for layer in spec["layer"] if layer.get("name") == f"{DECO}_unknown")
    assert unknown["data"]["values"] == [{"text": "1 個未知"}]
    names = {layer.get("name") for layer in spec["layer"]}
    assert {f"{DECO}_land", f"{DECO}_borders", f"{DECO}_states_USA"} <= names
    assert spec["view"] == {"fill": "#ffffff", "stroke": None}


def test_symbol_map_and_hidden_unknown(tmp_path: Path, db: Path) -> None:
    """自動標記、沒有 Geometry：圓點（符號地圖）以大小表示銷售；「篩選資料」去掉未知的位置。"""
    sheet = map_sheet(
        "Symbols",
        [STATE, CITY, SALES],
        "Automatic",
        f"<size column='{b.ref('sum:Sales:qk')}' /><lod column='{b.ref('none:City:nk')}' />"
        f"<lod column='{b.ref('none:State:nk')}' />",
        view_extra=(
            f"<filter class='quantitative' column='{GEN_LAT}' included-values='non-null' />"
            f"<filter class='quantitative' column='{GEN_LON}' included-values='non-null' />"
        ),
    )
    ir = parse(tmp_path, sheet)
    assert ir.worksheets[0].map == MapOptions(hide_unknown=True)
    rendered = render(ir, db)
    spec = rendered.spec
    assert spec is not None and rendered.rows == 4  # Atlantis／Nowhere 去掉
    (layer,) = data_layers(spec)
    assert layer["mark"]["type"] == "circle"
    assert layer["encoding"]["longitude"] == {"field": "__lon0", "type": "quantitative"}
    assert "size" in layer["encoding"]
    assert not any(layer.get("name") == f"{DECO}_unknown" for layer in spec["layer"])
    assert [r["__row"] for r in spec["data"]["values"]] == [0, 1, 2, 3]


def test_map_layers_and_style(tmp_path: Path, db: Path) -> None:
    """標記圖層：州（填色）在下、城市（圓點）在上，詳細程度較粗的州圖層另外查詢；深色、淡化、關閉的
    底圖圖層與存檔時固定的範圍。"""
    state_lod = f"<lod column='{b.ref('none:State:nk')}' />"
    layers = f"""
      <pane id='5'><view><breakdown value='auto' /></view><mark class='Automatic' />
        <encodings>{state_lod}</encodings></pane>
      <pane generated-title='State' id='4'><view><breakdown value='auto' /></view><mark class='Multipolygon' />
        <encodings><color column='{b.ref("sum:Sales:qk")}' />{state_lod}{GEOMETRY}</encodings></pane>
      <pane generated-title='City' id='6'><view><breakdown value='auto' /></view><mark class='Circle' />
        <encodings><lod column='{b.ref("none:City:nk")}' />{state_lod}</encodings></pane>"""
    style = f"""
      <style>
        <style-rule element='axis'>
          <encoding attr='space' class='0' field='{GEN_LON}' field-type='quantitative' max='-7000000' min='-14000000' projection='EPSG:3857' range-type='fixed' scope='cols' type='space' />
          <encoding attr='space' class='0' field='{GEN_LAT}' field-type='quantitative' max='6500000' min='2800000' projection='EPSG:3857' range-type='fixed' scope='rows' type='space' />
        </style-rule>
        <style-rule element='map-layer'>
          <format attr='enabled' id='admin-1-label-1st-tier' value='false' />
          <format attr='enabled' id='admin-1-label-2nd-tier' value='false' />
          <format attr='enabled' id='place-city-lg-n' value='true' />
          <format attr='enabled' id='dp05_0001e' value='false' />
        </style-rule>
        <style-rule element='map'>
          <format attr='washout' value='0.5' />
          <format attr='map-style' value='dark' />
        </style-rule>
      </style>"""
    sheet = f"""
    <worksheet name='Layers'>
      <table>
        <view>
          <datasources><datasource caption='Orders' name='{b.DS}' /></datasources>
          <mapsources><mapsource name='Tableau' /></mapsources>
          <datasource-dependencies datasource='{b.DS}'>{STATE}{CITY}{SALES}</datasource-dependencies>
          <aggregation value='true' />
        </view>
        {style}
        <panes customization-axis='layer'>{layers}</panes>
        <rows>{GEN_LAT}</rows>
        <cols>{GEN_LON}</cols>
      </table>
    </worksheet>"""
    ir = parse(tmp_path, sheet)
    ws = ir.worksheets[0]
    assert [p.marks.type for p in ws.panes] == ["map", "circle"]
    assert ws.marks.uses() == []
    assert ws.map is not None
    assert ws.map.style == "dark" and ws.map.washout == 0.5
    assert ws.map.layers == {"state_names": False, "cities": True}
    viewport = ws.map.viewport
    assert isinstance(viewport, MapViewport)
    assert viewport.west == pytest.approx(-125.77, abs=0.01)
    assert viewport.east == pytest.approx(-62.88, abs=0.01)
    rendered = render(ir, db)
    spec = rendered.spec
    assert spec is not None
    drawn = data_layers(spec)
    # 州的圖層有自己的資料（每州一列），城市的圖層用工作表的資料
    assert drawn[0]["mark"]["type"] == "geoshape" and len(drawn[0]["data"]["values"]) == 4
    assert drawn[1]["mark"]["type"] == "circle" and "data" not in drawn[1]
    (west, _), (east, _) = spec["projection"]["fit"]["geometry"]["coordinates"]
    assert (west, east) == (pytest.approx(-125.77, abs=0.01), pytest.approx(-62.88, abs=0.01))
    names = {layer.get("name") for layer in spec["layer"]}
    assert f"{DECO}_state_names" not in names  # 州名關閉
    land = next(layer for layer in spec["layer"] if layer.get("name") == f"{DECO}_land")
    assert land["mark"]["fill"] == "#a0a0a0"  # 深色的陸地（#404040）淡化 50%
    assert spec["view"]["fill"] == "#929292"  # 深色的水域（#262626）淡化 50%


def test_density_map(tmp_path: Path, db: Path) -> None:
    sheet = map_sheet(
        "Density",
        [STATE, CITY],
        "Heatmap",
        f"<lod column='{b.ref('none:City:nk')}' /><lod column='{b.ref('none:State:nk')}' />",
        pane_extra="""<style><style-rule element='mark'>
          <encoding attr='density-color' palette='tableau-map-temperatur' type='interpolated' />
          <format attr='density-intensity' value='0.75' /></style-rule></style>""",
    )
    ir = parse(tmp_path, sheet)
    density = ir.worksheets[0].marks.density
    assert density is not None and density.palette == "tableau-map-temperatur"
    assert density.intensity == 0.75
    rendered = render(ir, db)
    spec = rendered.spec
    assert spec is not None
    image = next(layer for layer in spec["layer"] if layer.get("name") == f"{DECO}_density0")
    assert image["mark"]["type"] == "image"
    (value,) = image["data"]["values"]
    assert value["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(value["url"].split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
    assert value["lon"] < -118 and value["lon2"] > -80 and value["lat"] < value["lat2"]


def test_latitude_longitude_fields(tmp_path: Path, db: Path) -> None:
    """緯度、經度角色的欄位分別在列與欄架上：位置就是欄位的值。"""
    sheet = worksheet(
        "Points",
        [CITY, LAT, LON],
        rows=b.ref("avg:Lat:qk"),
        cols=b.ref("avg:Lon:qk"),
        mark="Automatic",
        encodings=f"<lod column='{b.ref('none:City:nk')}' />",
    )
    rendered = render(parse(tmp_path, sheet), db)
    spec = rendered.spec
    assert spec is not None and rendered.mark == "map"
    values = {r["c0"]: r for r in spec["data"]["values"]}
    assert values["Houston"]["__lat0"] == pytest.approx(29.76)
    assert values["Nowhere"]["__lat0"] is None
    (layer,) = data_layers(spec)
    assert layer["mark"]["type"] == "circle"


def test_zip_codes_draw_points(tmp_path: Path, db: Path) -> None:
    """郵遞區號沒有區域形狀：填色地圖以中心點的圓點畫出，並說明。"""
    sheet = map_sheet(
        "Zips",
        [ZIP, SALES],
        "Automatic",
        f"<color column='{b.ref('sum:Sales:qk')}' /><lod column='{b.ref('none:Zip:ok')}' />{GEOMETRY}",
    )
    rendered = render(parse(tmp_path, sheet), db)
    spec = rendered.spec
    assert spec is not None
    (layer,) = data_layers(spec)
    assert layer["mark"]["type"] == "circle"
    assert any("郵遞區號" in note for note in rendered.notes)
    found = [r for r in spec["data"]["values"] if r["__lon0"] is not None]
    assert len(found) == 4  # 99999 不存在


def test_spec_is_json_serializable(tmp_path: Path, db: Path) -> None:
    sheet = map_sheet(
        "Filled",
        [STATE, SALES],
        "Multipolygon",
        f"<lod column='{b.ref('none:State:nk')}' />{GEOMETRY}",
    )
    rendered = render(parse(tmp_path, sheet), db)
    json.dumps(rendered.spec)


def test_faceted_map_and_reference_lines(tmp_path: Path, db: Path) -> None:
    """欄列架上有離散維度的地圖：與 Tableau 相同分格（每一州一格、範圍相同）；緯度座標軸上的參考線
    （平均）畫成橫跨地圖的緯線，標籤寫在左端。"""
    from ..parser.test_extensions import refline

    lines = refline(0, "average", label="value", axis="Latitude (generated)").replace(
        b.ref("Latitude (generated)"), GEN_LAT
    )
    sheet = worksheet(
        "Faceted",
        [STATE, CITY, SALES],
        rows=f"({b.ref('none:State:nk')} / {GEN_LAT})",
        cols=GEN_LON,
        mark="Circle",
        encodings=f"<lod column='{b.ref('none:City:nk')}' />",
        pane_extra=lines,
    )
    ir = parse(tmp_path, sheet)
    rendered = render(ir, db)
    spec = rendered.spec
    assert spec is not None and rendered.mark == "map"
    assert "row" in spec["facet"] and "width" not in spec
    inner = spec["spec"]
    assert inner["projection"]["clipExtent"]["expr"].startswith("[[-2, -2], [child_width")
    references = [
        layer for layer in inner["layer"] if str(layer.get("name", "")).startswith("__ref")
    ]
    line, label = references
    lats = [r["__lat0"] for r in spec["data"]["values"] if r.get("__lat0") is not None]
    average = sum(lats) / len(lats)
    assert [p["lat"] for p in line["data"]["values"]] == [pytest.approx(average)] * 2
    assert label["data"]["values"][0]["text"]
    assert not [n for n in rendered.notes if "參考線" in n or "分格" in n]
