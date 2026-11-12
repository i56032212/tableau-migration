"""空間資料的報表（tests/data/spatial_book.py）：解析、匯入、查詢與地圖的規格。

- 萃取中的 GEOGRAPHY 欄位以 WKT 文字匯入；沒有萃取的 Shapefile 壓縮檔、CSV 匯入時讀進資料庫
- 工作表上的 COLLECT(Geometry) 是標記的形狀：查詢每個標記一個形狀，地圖畫成多邊形、點或線
- 計算欄位的 DISTANCE、AREA(BUFFER(…)) 與 Hyper（Tableau 的引擎）計算的結果相同
- 空間 join（Intersects）：CSV 的地點以 MAKEPOINT 對到 GeoJSON 中所在的縣市（left join，沒有對到的為 Null）
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tmig.census import run_census
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_workbook
from tmig.ir import IR, ExtractConnection, FileConnection
from tmig.ir.integrity import check_references
from tmig.parser.workbook import parse_workbook
from tmig.render.datasource import datasource_summary
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from .spatial_book import CITIES, PLACES, STORES, write_spatial_twbx

pytest.importorskip("tableauhyperapi")


@pytest.fixture(scope="module")
def twbx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_spatial_twbx(tmp_path_factory.mktemp("spatial-book"))


@pytest.fixture(scope="module")
def ir(twbx: Path) -> IR:
    return parse_workbook(twbx)


@pytest.fixture(scope="module")
def data(twbx: Path, ir: IR, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("spatial-data")
    import_workbook(twbx, out, ir)
    return out


def sheet(ir: IR, name: str) -> Any:
    return next(w for w in ir.worksheets if w.name == name)


def run(ir: IR, data: Path, name: str) -> tuple[Any, Any]:
    ws = sheet(ir, name)
    query = worksheet_sql(ir, ws.id)
    return query, execute_query(data / f"{ws.datasource}.duckdb", query)


def test_parse(ir: IR) -> None:
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert check_references(ir) == []
    counties, stores, cities, _ = ir.datasources
    assert isinstance(counties.connection, ExtractConnection)
    geometry = next(f for f in counties.fields if f.caption == "Geometry")
    assert (geometry.datatype, geometry.role, geometry.default_agg) == (
        "spatial",
        "measure",
        "collect",
    )
    assert isinstance(stores.connection, FileConnection)
    (table,) = stores.connection.tables
    assert (table.format, table.path, table.layer) == ("spatial", "Data/gis/stores.zip", "stores")
    assert [(c.name, c.datatype) for c in table.columns][-1] == ("Geometry", "spatial")
    point = next(f for f in cities.fields if f.caption == "Point")
    assert (point.datatype, point.default_agg) == ("spatial", "collect")
    county = sheet(ir, "County Map")
    assert county.marks.geometry is not None
    assert (county.marks.geometry.field, county.marks.geometry.agg) == (geometry.id, "collect")
    # 詳細資料上的 COLLECT 就是形狀，不另外放在詳細資料
    assert [u.field for u in county.marks.detail] == [
        f.id for f in counties.fields if f.caption == "Name"
    ]


def test_import(ir: IR, data: Path) -> None:
    for ds in ir.datasources:
        report = json.loads((data / f"{ds.id}.import.json").read_text(encoding="utf-8"))
        assert report["ok"] is True, report
        assert report["missing_columns"] == []
        geometry = next((c for c in report["table"]["columns"] if c["name"] == "Geometry"), None)
        if geometry is not None:
            assert geometry["duckdb_type"] == "VARCHAR"
    summary = datasource_summary(ir.datasources[1])
    assert summary["connection"]["type"] == "空間檔案"
    assert summary["tables"][0]["physical"][0]["source"] == "stores.zip：stores"


def test_queries(ir: IR, data: Path) -> None:
    _, counties = run(ir, data, "County Map")
    assert len(counties.rows) == 21
    shapes = [row[-1] for row in counties.rows]
    assert all(s.startswith(("POLYGON((", "MULTIPOLYGON(((")) for s in shapes)
    _, stores = run(ir, data, "Store Map")
    found = {row[0]: row[-1] for row in stores.rows}
    for name, lat, lon, _, _ in STORES:
        values = [float(v) for v in re.findall(r"-?\d+\.\d+", found[name])]
        assert values == pytest.approx([lon, lat], abs=1e-7)
    _, routes = run(ir, data, "Routes")
    assert {row[0]: row[-1] for row in routes.rows}["Kaohsiung"] == (
        "LINESTRING(121.5645000 25.0339000, 120.3014000 22.6273000)"
    )


def test_spatial_join(ir: IR, data: Path) -> None:
    """CSV 與 GeoJSON 的空間 join：條件是 MAKEPOINT(緯度, 經度) 與縣市的形狀相交。"""
    places = ir.datasources[3]
    assert isinstance(places.connection, FileConnection)
    assert [t.format for t in places.connection.tables] == ["text", "spatial"]
    relation = places.connection.relation
    assert relation is not None and relation.kind == "join"
    (condition,) = relation.on
    assert condition.op == "intersects"
    assert condition.left.formula == "MAKEPOINT([places.csv].[Lat],[places.csv].[Lon])"
    query, result = run(ir, data, "Places per County")
    counts = {row[0]: row[1] for row in result.rows}
    expected: dict[str | None, int] = {}
    for *_, county in PLACES:
        expected[county] = expected.get(county, 0) + 1
    assert counts == expected
    # 形狀與範圍在資料表的子查詢中先算好，join 條件只比較這些欄
    assert query.sql.count("tmig_geo_point(") == 1


def test_map_layers_with_spatial_fields(ir: IR, data: Path) -> None:
    """2021.2 的地圖圖層（元素與屬性帶功能旗標的前綴）：每個圖層一種形狀；卡上有兩個空間欄位、
    沒有 geometry 編碼時，以圖層的名稱（空間欄位的名稱）決定形狀，另一個記為略過。"""
    ws = sheet(ir, "Place Layers")
    first, second = ws.panes
    assert first.marks.geometry is not None and second.marks.geometry is not None
    fields = {f.id: f.caption for f in ir.datasources[3].fields}
    assert [fields[p.marks.geometry.field] for p in ws.panes] == ["Geometry", "Place Point"]
    assert (second.dual, [u.field for u in second.marks.detail]) == (True, ["f_place"])
    assert any("形狀是「Place Point」，「Geometry」不畫出" in i.reason for i in ir.ignored)
    query = worksheet_sql(ir, ws.id)
    from tmig.sqlgen.query import pane_sql

    db = data / f"{ws.datasource}.duckdb"
    panes = {i: (q.columns, execute_query(db, q)) for i, q in pane_sql(ir, ws.id, query).items()}
    rendered = build_spec(ir, ws.id, query.columns, execute_query(db, query), None, panes or None)
    assert rendered.spec is not None
    drawn = [
        layer["mark"]["type"]
        for layer in rendered.spec["layer"]
        if not str(layer.get("name", "")).startswith("__deco")
    ]
    assert drawn == ["geoshape", "circle"]


def test_distances_match_hyper(ir: IR, data: Path, tmp_path: Path) -> None:
    """DISTANCE 與 AREA(BUFFER(…)) 的數字與 Hyper 的 tableau.geo_* 函式相同。"""
    import tableauhyperapi as h

    _, result = run(ir, data, "Distances")
    found = {row[0]: (row[1], row[2]) for row in result.rows}
    taipei = "tableau.geo_make_point(25.0339::double precision, 121.5645::double precision)"
    with (
        h.HyperProcess(
            h.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(tmp_path)}
        ) as process,
        h.Connection(process.endpoint) as conn,
    ):
        for city, lat, lon in CITIES:
            point = f"tableau.geo_make_point({lat}::double precision, {lon}::double precision)"
            distance = conn.execute_list_query(
                f"SELECT tableau.geo_distance({point}, {taipei}, 'km')"
            )[0][0]
            area = conn.execute_list_query(
                f"SELECT tableau.geo_area(tableau.geo_buffer({point}, 15::double precision, 'km'), 'km')"
            )[0][0]
            assert found[city][0] == pytest.approx(distance, rel=1e-6, abs=1e-9)
            assert found[city][1] == pytest.approx(area, rel=1e-6)


def specs(ir: IR, data: Path, name: str) -> tuple[dict[str, Any], list[str]]:
    query, result = run(ir, data, name)
    rendered = build_spec(ir, sheet(ir, name).id, query.columns, result)
    assert rendered.unsupported is None and rendered.spec is not None
    return rendered.spec, rendered.notes


def layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [layer for layer in spec["layer"] if not str(layer.get("name", "")).startswith("__deco")]


def test_polygon_map_spec(ir: IR, data: Path) -> None:
    spec, notes = specs(ir, data, "County Map")
    assert notes == []
    text = json.dumps(spec)
    # WKT 不放進規格（畫圖用的形狀已依畫面簡化）
    assert "POLYGON((" not in text and "__gpoly0" in text
    shapes = [
        layer for layer in layers(spec) if layer.get("encoding", {}).get("shape", {}).get("field")
    ]
    (fill,) = shapes
    assert fill["mark"]["type"] == "geoshape"
    assert fill["encoding"]["shape"] == {"field": "__gpoly0", "type": "geojson"}
    assert fill["encoding"]["color"]["type"] == "quantitative"
    tooltips = [t["title"] for t in fill["encoding"]["tooltip"]]
    assert "Geometry" not in " ".join(tooltips)
    values = spec["data"]["values"]
    polygon = next(v["__gpoly0"] for v in values if v.get("__gpoly0"))
    ring = polygon["coordinates"][0][0]
    # d3 的方向：外環順時針（經緯度平面上的有號面積為負）
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in itertools.pairwise(ring))
    assert area < 0
    labels = [layer for layer in layers(spec) if layer.get("mark", {}).get("type") == "text"]
    assert labels and labels[0]["mark"]["align"] == "center"
    assert sum(1 for v in values if v.get("__map_label0")) >= 15


def test_point_and_line_specs(ir: IR, data: Path) -> None:
    spec, _ = specs(ir, data, "Store Map")
    (points,) = [layer for layer in layers(spec) if layer["mark"]["type"] == "circle"]
    assert {"flatten": ["__gpts0"], "as": ["__gpt0"]} in points["transform"]
    assert points["encoding"]["size"]["type"] == "quantitative"
    assert all(v["__gpts0"] for v in spec["data"]["values"])
    spec, _ = specs(ir, data, "Routes")
    (lines,) = [layer for layer in layers(spec) if layer["mark"].get("filled") is False]
    assert lines["encoding"]["shape"] == {"field": "__gline0", "type": "geojson"}
    assert lines["encoding"]["color"]["field"]
    spec, _ = specs(ir, data, "Buffers")
    assert any(layer["mark"]["type"] == "geoshape" for layer in layers(spec))


def test_census_reads_spatial_files(twbx: Path) -> None:
    report = run_census(twbx)
    access = next(c for c in report.checks if c.key == "data_access")
    assert access.status == "pass", access.detail
