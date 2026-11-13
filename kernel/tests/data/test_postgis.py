"""資料庫的空間欄位（PostGIS）：以臨時的 PostgreSQL + PostGIS 實跑（沒有 PostgreSQL 或 PostGIS 時略過；
TMIG_REQUIRE_ENGINES=1 時改為失敗），數字與直接以空間函式計算的結果比對。

- 桌面版連線到資料庫：列出的欄位含空間欄位（geometry、geography）；自訂 SQL 選出的空間欄位
- Live 連線：空間欄位在 PostGIS 轉成 WKT（ST_AsText）；兩邊都是空間欄位的空間 join 與 Tableau 的 Live 連線
  相同在 PostGIS 以 ST_Intersects 計算；一邊是 join 計算（MAKEPOINT）時取回資料表在本機計算；空間函式
  （DISTANCE、AREA、COLLECT）在本機計算；Relationship 模型中的空間欄位；地圖（geoshape）
- 跨資料庫的 join：PostGIS 的空間欄位與匯入的 GeoJSON 在本機做空間 join
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tmig.authoring import (
    Calculated,
    add_live_table,
    inspect_file,
    new_live_workbook,
    new_workbook,
)
from tmig.data.catalog import sql_columns, table_columns
from tmig.data.hyper_import import import_sources
from tmig.data.live import execute_live
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig, Executor
from tmig.edit import replace_worksheet, upsert_calc
from tmig.geo import spatial
from tmig.ir import IR, FileColumn, FileConnection, LiveConnection, is_federated
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from ..engines import pg_connect, postgres_cluster, unavailable

PG = LiveConnection(
    kind="live", connection_id="pg_geo", dbclass="postgres", dialect="postgres", database="geo"
)
NORTH = "POLYGON((121.5 25,121.6 25,121.6 25.1,121.5 25.1,121.5 25))"
SOUTH = "POLYGON((120.25 22.6,120.35 22.6,120.35 22.7,120.25 22.7,120.25 22.6))"
DISTRICTS = [("North", NORTH), ("South", SOUTH)]
SITES = [
    ("S1", "North", 25.0330, 121.5654, 10.0),
    ("S2", "North", 25.0478, 121.5170, 20.0),
    ("S3", "South", 22.6273, 120.3014, 5.0),
    ("S4", None, 24.0, 121.0, 7.0),  # 不在任何行政區
]
HOME = (25.0330, 121.5654)
BY_DISTRICT = {"North": 30.0, "South": 5.0}
"""各行政區內的參觀人數（S4 不在任何行政區）。"""


@pytest.fixture(scope="module")
def runtime() -> Iterator[DataRuntime]:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    with postgres_cluster() as dsn:
        admin = pg_connect(dsn)
        admin.execute("DROP DATABASE IF EXISTS geo")
        admin.execute("CREATE DATABASE geo")
        admin.close()
        con = pg_connect(make_conninfo(dsn, dbname="geo"))
        try:
            con.execute("CREATE EXTENSION postgis")
        except Exception as exc:
            con.close()
            unavailable(f"PostgreSQL 沒有安裝 PostGIS（{str(exc).splitlines()[0]}）")
            return
        con.execute(
            "CREATE TABLE districts (name TEXT, boundary geometry(Polygon, 4326), "
            '"Area Geog" geography(Polygon, 4326))'
        )
        for name, wkt in DISTRICTS:
            con.execute(
                "INSERT INTO districts VALUES (%s, ST_GeomFromText(%s, 4326), ST_GeogFromText(%s))",
                (name, wkt, f"SRID=4326;{wkt}"),
            )
        con.execute(
            "CREATE TABLE sites (site TEXT, district TEXT, lat DOUBLE PRECISION, "
            'lon DOUBLE PRECISION, visitors DOUBLE PRECISION, "Location" geometry(Point, 4326), '
            "position geography(Point, 4326))"
        )
        for site, district, lat, lon, visitors in SITES:
            con.execute(
                "INSERT INTO sites VALUES (%s, %s, %s, %s, %s, "
                "ST_SetSRID(ST_MakePoint(%s, %s), 4326), ST_MakePoint(%s, %s)::geography)",
                (site, district, lat, lon, visitors, lon, lat, lon, lat),
            )
        con.close()
        info = conninfo_to_dict(dsn)
        params = {k: v for k, v in info.items() if k in ("host", "port", "user", "password")}
        configs = {"pg_geo": ConnectionConfig("pg_geo", "postgres", {**params, "database": "geo"})}
        with DataRuntime(configs) as found:
            yield found


class Recording:
    """記下送到 PostGIS 的 SQL。"""

    dialect = "postgres"

    def __init__(self, inner: Executor) -> None:
        self.inner = inner
        self.sqls: list[str] = []

    def fetch(self, sql: str) -> pa.Table:
        self.sqls.append(sql)
        return self.inner.fetch(sql)

    def close(self) -> None:
        pass


class Fetcher:
    def __init__(self, runtime: DataRuntime) -> None:
        self.runtime = runtime

    def fetch(self, sql: str) -> pa.Table:
        return self.runtime.fetch(PG, sql)


def columns_of(runtime: DataRuntime, table: str) -> list[FileColumn]:
    return table_columns(Fetcher(runtime), "postgres", ["public", table])


def ids(ir: IR, ds: int = 0) -> dict[str, str]:
    return {f.caption: f.id for f in ir.datasources[ds].fields}


def sheet(ir: IR, rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> tuple[IR, str]:
    data = ir.worksheets[0].model_dump(mode="json")
    data.update(rows=rows, columns=columns)
    ir, ws = replace_worksheet(ir, data)
    return ir, ws.id


def run(
    runtime: DataRuntime, ir: IR, worksheet: str, database: Any = None
) -> list[tuple[Any, ...]]:
    ws = next(w for w in ir.worksheets if w.id == worksheet)
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    query = worksheet_sql(ir, worksheet)
    return runtime.run(query, ds, database or (lambda: None)).rows  # type: ignore[return-value]


def recorded(
    runtime: DataRuntime, ir: IR, worksheet: str
) -> tuple[list[tuple[Any, ...]], list[str]]:
    """兩段式執行，記下送到 PostGIS 的 SQL。"""
    with runtime._executor(PG) as executor:  # pyright: ignore[reportPrivateUsage]
        recording = Recording(executor)
        rows = execute_live(worksheet_sql(ir, worksheet), recording).rows
    return rows, recording.sqls  # type: ignore[return-value]


def coordinates(text: object) -> list[list[float]]:
    geom = spatial.parse_wkt(str(text))
    assert geom is not None and geom["type"] == "Polygon"
    return [[round(x, 7), round(y, 7)] for x, y in geom["coordinates"][0]]


def test_catalog_lists_spatial_columns(runtime: DataRuntime) -> None:
    """桌面版連線到資料庫：PostGIS 的 geometry、geography 是空間欄位（Tableau 的 spatial）。"""
    sites = columns_of(runtime, "sites")
    assert [(c.name, c.datatype) for c in sites] == [
        ("site", "string"),
        ("district", "string"),
        ("lat", "real"),
        ("lon", "real"),
        ("visitors", "real"),
        ("Location", "spatial"),
        ("position", "spatial"),
    ]
    assert [c.datatype for c in columns_of(runtime, "districts")] == [
        "string",
        "spatial",
        "spatial",
    ]


def test_spatial_join_runs_in_postgis(runtime: DataRuntime) -> None:
    """兩邊都是空間欄位的空間 join：與 Tableau 的 Live 連線相同在 PostGIS 以 ST_Intersects 計算，
    整段查詢下推（沒有取回資料表）。"""
    ir = new_live_workbook("Sites", PG, ["public", "sites"], None, columns_of(runtime, "sites"))
    ds = ir.datasources[0].id
    location = ids(ir)["Location"]
    ir = add_live_table(
        ir,
        ds,
        ["public", "districts"],
        None,
        columns_of(runtime, "districts"),
        how="join",
        on=[(location, "intersects", "boundary")],
    )
    f = ids(ir)
    ir, ws = sheet(ir, [{"field": f["name"]}], [{"field": f["visitors"], "agg": "sum"}])
    rows, sqls = recorded(runtime, ir, ws)
    assert dict(rows) == BY_DISTRICT  # type: ignore[arg-type]
    (sql,) = sqls
    assert 'ST_INTERSECTS("sites"."Location", "districts"."boundary")' in sql
    assert "tmig_geo" not in sql.lower()
    # 服務的執行路徑（DataRuntime）結果相同；geography 的欄位也在 PostGIS 計算
    assert dict(run(runtime, ir, ws)) == BY_DISTRICT  # type: ignore[arg-type]
    ir2 = new_live_workbook("Geog", PG, ["public", "sites"], None, columns_of(runtime, "sites"))
    ir2 = add_live_table(
        ir2,
        ir2.datasources[0].id,
        ["public", "districts"],
        None,
        columns_of(runtime, "districts"),
        how="join",
        join="left",
        on=[(ids(ir2)["position"], "intersects", "Area Geog")],
    )
    g = ids(ir2)
    ir2, ws2 = sheet(ir2, [{"field": g["name"]}], [{"field": g["visitors"], "agg": "sum"}])
    assert dict(run(runtime, ir2, ws2)) == {**BY_DISTRICT, None: 7.0}  # type: ignore[arg-type]


def test_join_calculation_point_in_polygon(runtime: DataRuntime) -> None:
    """一邊是 join 計算（MAKEPOINT）的空間 join：取回兩張資料表（PostGIS 的空間欄位轉成 WKT），在本機計算；
    結果與在 PostGIS 計算的相同。"""
    ir = new_live_workbook("Points", PG, ["public", "sites"], None, columns_of(runtime, "sites"))
    ir = add_live_table(
        ir,
        ir.datasources[0].id,
        ["public", "districts"],
        None,
        columns_of(runtime, "districts"),
        how="join",
        on=[(Calculated("MAKEPOINT([lat], [lon])"), "intersects", "boundary")],
    )
    f = ids(ir)
    ir, ws = sheet(ir, [{"field": f["name"]}], [{"field": f["visitors"], "agg": "sum"}])
    rows, sqls = recorded(runtime, ir, ws)
    assert dict(rows) == BY_DISTRICT  # type: ignore[arg-type]
    # 取回資料表：空間欄位都在 PostGIS 轉成 WKT（先算形狀的子查詢 SELECT * 時先取 0 列得到欄名）
    fetched = [s for s in sqls if "WHERE 1 = 0" not in s]
    assert any('ST_ASTEXT("boundary") AS "boundary"' in s for s in fetched), sqls
    assert any('ST_ASTEXT("Location") AS "Location"' in s for s in fetched), sqls
    assert all("tmig_geo" not in s.lower() for s in sqls)
    # 點的位置（PostGIS 的空間欄位）一起顯示：WKT
    ir, ws = sheet(
        ir,
        [{"field": f["site"]}, {"field": f["name"]}],
        [{"field": f["Location"], "agg": "collect"}],
    )
    shapes = {row[0]: spatial.parse_wkt(str(row[2])) for row in run(runtime, ir, ws)}
    assert shapes == {
        site: {"type": "Point", "coordinates": [lon, lat]}
        for site, district, lat, lon, _ in SITES
        if district
    }


def test_spatial_functions_and_shapes(runtime: DataRuntime) -> None:
    """空間欄位的形狀（COLLECT）、距離與面積：在 PostGIS 轉成 WKT 後在本機計算，與直接計算的結果相同。"""
    ir = new_live_workbook(
        "Shapes", PG, ["public", "districts"], None, columns_of(runtime, "districts")
    )
    ds = ir.datasources[0].id
    ir, _ = upsert_calc(ir, ds, "Area km", "AREA([boundary], 'km')")
    ir, _ = upsert_calc(ir, ds, "Geog Area km", "AREA([Area Geog], 'km')")
    f = ids(ir)
    ir, ws = sheet(ir, [{"field": f["name"]}], [{"field": f["boundary"], "agg": "collect"}])
    shapes = dict(run(runtime, ir, ws))
    assert coordinates(shapes["North"]) == coordinates(NORTH)
    assert coordinates(shapes["South"]) == coordinates(SOUTH)
    ir, ws = sheet(ir, [{"field": f["name"]}], [{"field": f["Area km"], "agg": "sum"}])
    areas = dict(run(runtime, ir, ws))
    for name, wkt in DISTRICTS:
        expected = spatial.area(wkt, "km")
        assert expected is not None and areas[name] == pytest.approx(expected, rel=1e-9)
    ir, ws = sheet(ir, [{"field": f["name"]}], [{"field": f["Geog Area km"], "agg": "sum"}])
    assert dict(run(runtime, ir, ws)) == pytest.approx(areas, rel=1e-9)
    # 點：geometry 與 geography 的距離相同
    sites = new_live_workbook("Near", PG, ["public", "sites"], None, columns_of(runtime, "sites"))
    sds = sites.datasources[0].id
    home = f"MAKEPOINT({HOME[0]}, {HOME[1]})"
    sites, _ = upsert_calc(sites, sds, "Km", f"DISTANCE([Location], {home}, 'km')")
    sites, _ = upsert_calc(sites, sds, "Geog Km", f"DISTANCE([position], {home}, 'km')")
    g = ids(sites)
    expected = {
        site: spatial.distance(spatial.make_point(lat, lon), spatial.make_point(*HOME), "km")
        for site, _, lat, lon, _ in SITES
    }
    for measure in ("Km", "Geog Km"):
        sites, ws = sheet(sites, [{"field": g["site"]}], [{"field": g[measure], "agg": "sum"}])
        found = dict(run(runtime, sites, ws))
        assert found.keys() == expected.keys()
        for site, value in expected.items():
            assert found[site] == pytest.approx(value, rel=1e-9, abs=1e-9), (measure, site)


def test_relationship_model_with_spatial_columns(runtime: DataRuntime) -> None:
    """Relationship 模型：邏輯資料表的空間欄位在 PostGIS 轉成 WKT。"""
    ir = new_live_workbook("Model", PG, ["public", "sites"], None, columns_of(runtime, "sites"))
    ds = ir.datasources[0].id
    ir = add_live_table(
        ir,
        ds,
        ["public", "districts"],
        None,
        columns_of(runtime, "districts"),
        on=[(ids(ir)["district"], "=", "name")],
    )
    f = ids(ir)
    ir, ws = sheet(
        ir,
        [{"field": f["name"]}],
        [{"field": f["boundary"], "agg": "collect"}, {"field": f["visitors"], "agg": "sum"}],
    )
    found = {row[0]: row[1:] for row in run(runtime, ir, ws)}
    assert coordinates(found["North"][0]) == coordinates(NORTH)
    assert found["North"][1] == 30.0 and found["South"][1] == 5.0


def test_federated_spatial_join_with_geojson(runtime: DataRuntime, tmp_path: Path) -> None:
    """跨資料庫的 join：匯入的 GeoJSON（行政區）與 PostGIS 的點（空間欄位在 PostGIS 轉成 WKT 後取回），
    在本機做空間 join。"""
    features = [
        {
            "type": "Feature",
            "properties": {"zone": name},
            "geometry": spatial.parse_wkt(wkt),
        }
        for name, wkt in DISTRICTS
    ]
    source = tmp_path / "zones.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    sources = tmp_path / "report" / "sources"
    (sources / "Data").mkdir(parents=True)
    (sources / "Data" / "zones.geojson").write_bytes(source.read_bytes())
    ir = new_workbook("Zones", inspect_file(source), None, "Data/zones.geojson")
    ds = ir.datasources[0].id
    geometry = ids(ir)["Geometry"]
    ir = add_live_table(
        ir,
        ds,
        ["public", "sites"],
        None,
        columns_of(runtime, "sites"),
        how="join",
        on=[(geometry, "intersects", "Location")],
        connection=PG,
    )
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection) and is_federated(connection)
    data = tmp_path / "report" / "data"
    (result,) = import_sources(sources, data, ir)
    assert result.ok, result.to_json()
    f = ids(ir)
    ir, ws = sheet(ir, [{"field": f["zone"]}], [{"field": f["visitors"], "agg": "sum"}])
    assert dict(run(runtime, ir, ws, data / f"{ds}.duckdb")) == BY_DISTRICT  # type: ignore[arg-type]


def test_custom_sql_spatial_columns(runtime: DataRuntime) -> None:
    """自訂 SQL 選出的 geometry：取回的值是 EWKB 的十六進位文字，判斷為空間欄位；查詢時在 PostGIS 轉成 WKT。"""
    sql = "SELECT name, ST_Centroid(boundary) AS center, boundary FROM districts"
    columns = sql_columns(Fetcher(runtime), "postgres", sql)
    assert [(c.name, c.datatype) for c in columns] == [
        ("name", "string"),
        ("center", "spatial"),
        ("boundary", "spatial"),
    ]
    ir = new_live_workbook("Custom", PG, None, sql, columns)
    f = ids(ir)
    ir, ws = sheet(ir, [{"field": f["name"]}], [{"field": f["center"], "agg": "collect"}])
    centers = {name: spatial.parse_wkt(str(wkt)) for name, wkt in run(runtime, ir, ws)}
    north = centers["North"]
    assert north is not None and north["type"] == "Point"
    assert north["coordinates"] == pytest.approx([121.55, 25.05], abs=1e-9)


def test_map_of_postgis_shapes(runtime: DataRuntime) -> None:
    """地圖：標記卡上的 COLLECT 是 PostGIS 的多邊形（在 PostGIS 轉成 WKT），畫成 geoshape。"""
    ir = new_live_workbook(
        "Map", PG, ["public", "districts"], None, columns_of(runtime, "districts")
    )
    f = ids(ir)
    data = ir.worksheets[0].model_dump(mode="json")
    data.update(
        rows=[{"field": "latitude_generated", "continuous": True}],
        columns=[{"field": "longitude_generated", "continuous": True}],
        marks={
            "type": "automatic",
            "geometry": {"field": f["boundary"], "agg": "collect"},
            "detail": [{"field": f["name"]}],
        },
    )
    ir, ws = replace_worksheet(ir, data)
    query = worksheet_sql(ir, ws.id)
    result = runtime.run(query, ir.datasources[0], lambda: None)  # type: ignore[arg-type]
    rendered = build_spec(ir, ws.id, query.columns, result)
    assert rendered.notes == []
    spec = rendered.spec
    assert spec is not None
    text = json.dumps(spec)
    assert '"geoshape"' in text and "POLYGON((" not in text
    values = spec["data"]["values"]
    polygons = [v["__gpoly0"] for v in values if v.get("__gpoly0")]
    assert len(polygons) == 2
    xs = sorted({round(x, 6) for p in polygons for x, _ in p["coordinates"][0][0]})
    assert xs[0] == 120.25 and xs[-1] == 121.6
