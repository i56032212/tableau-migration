"""Live 連線上的空間函式與空間欄位：用到空間函式的部分改在 DuckDB 計算（與萃取相同的球面計算），之前的
資料列下推到來源資料庫、只取用到的欄；資料庫的空間欄位在來源資料庫轉成 WKT 文字。

以臨時的 PostgreSQL 實跑（沒有時略過；TMIG_REQUIRE_ENGINES=1 時改為失敗），數字與直接以空間函式計算的
結果比對。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tmig.authoring import Calculated, add_live_table, new_live_workbook
from tmig.data.live import execute_live, execute_rows, source_tables
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig
from tmig.edit import replace_worksheet, upsert_calc
from tmig.geo import spatial
from tmig.ir import IR, FieldUse, FileColumn, LiveConnection, RangeFilter
from tmig.sqlgen.dialect import DialectError, localize_spatial, split, to_dialect
from tmig.sqlgen.query import worksheet_sql
from tmig.sqlgen.source import datasource_source

from ..engines import pg_connect, postgres_cluster

PG = LiveConnection(
    kind="live", connection_id="pg_stores", dbclass="postgres", dialect="postgres", database="shop"
)
COLUMNS = [
    FileColumn(name="store", datatype="string"),
    FileColumn(name="region", datatype="string"),
    FileColumn(name="lat", datatype="real"),
    FileColumn(name="lon", datatype="real"),
    FileColumn(name="sales", datatype="real"),
    FileColumn(name="note", datatype="string"),
]
STORES = [
    ("S1", "North", 25.0330, 121.5654, 10.0, "a"),
    ("S2", "North", 25.0478, 121.5170, 20.0, "b"),
    ("S3", "South", 22.6273, 120.3014, 5.0, None),
]
HOME = (25.0330, 121.5654)


@pytest.fixture(scope="module")
def runtime() -> Iterator[DataRuntime]:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    with postgres_cluster() as dsn:
        admin = pg_connect(dsn)
        admin.execute("DROP DATABASE IF EXISTS shop")
        admin.execute("CREATE DATABASE shop")
        admin.close()
        con = pg_connect(make_conninfo(dsn, dbname="shop"))
        con.execute(
            "CREATE TABLE stores (store TEXT, region TEXT, lat DOUBLE PRECISION, "
            "lon DOUBLE PRECISION, sales DOUBLE PRECISION, note TEXT)"
        )
        with con.cursor() as cursor:
            cursor.executemany("INSERT INTO stores VALUES (%s, %s, %s, %s, %s, %s)", STORES)
        con.execute("CREATE TABLE bands (band BIGINT, label TEXT)")
        con.execute("INSERT INTO bands VALUES (0, 'near'), (2, 'far'), (9, 'none')")
        con.close()
        info = conninfo_to_dict(dsn)
        params = {k: v for k, v in info.items() if k in ("host", "port", "user", "password")}
        configs = {
            "pg_stores": ConnectionConfig("pg_stores", "postgres", {**params, "database": "shop"})
        }
        with DataRuntime(configs) as found:
            yield found


def workbook() -> IR:
    ir = new_live_workbook("Stores", PG, ["public", "stores"], None, COLUMNS)
    ds = ir.datasources[0].id
    home = f"MAKEPOINT({HOME[0]}, {HOME[1]})"
    ir, _ = upsert_calc(ir, ds, "Distance", f"DISTANCE(MAKEPOINT([lat], [lon]), {home}, 'km')")
    ir, _ = upsert_calc(
        ir, ds, "Buffer Area", "AREA(BUFFER(MAKEPOINT([lat], [lon]), 5, 'km'), 'km')"
    )
    ir, _ = upsert_calc(ir, ds, "Point", "MAKEPOINT([lat], [lon])")
    ir, _ = upsert_calc(ir, ds, "Route", f"MAKELINE(MAKEPOINT([lat], [lon]), {home})")
    return ir


def run(
    runtime: DataRuntime, ir: IR, rows: list[str], measure: str | None
) -> list[tuple[Any, ...]]:
    f = {x.caption: x.id for x in ir.datasources[0].fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(
        rows=[{"field": f[r]} for r in rows],
        columns=[{"field": f[measure], "agg": "sum"}] if measure else [],
    )
    ir, ws = replace_worksheet(ir, sheet)
    query = worksheet_sql(ir, ws.id)
    return runtime.run(query, ir.datasources[0], lambda: None).rows  # type: ignore[arg-type,return-value]


def expected_distance(lat: float, lon: float) -> float:
    found = spatial.distance(spatial.make_point(lat, lon), spatial.make_point(*HOME), "km")
    assert found is not None
    return found


def test_spatial_functions_on_postgres(runtime: DataRuntime) -> None:
    ir = workbook()
    by_region: dict[str, float] = {}
    for _, region, lat, lon, *_ in STORES:
        by_region[region] = by_region.get(region, 0.0) + expected_distance(lat, lon)
    found = dict(run(runtime, ir, ["region"], "Distance"))
    assert found.keys() == by_region.keys()
    for region, value in by_region.items():
        assert found[region] == pytest.approx(value, rel=1e-12)
    # BUFFER 的面積：120 邊形（與 Hyper 相同）
    area = spatial.area(spatial.buffer(spatial.make_point(25.0, 121.0), 5, "km"), "km")
    assert area is not None
    for _, value in run(runtime, ir, ["store"], "Buffer Area"):
        assert value == pytest.approx(area, rel=1e-3)
    # 形狀當作維度：點與線（WKT）
    points = dict(run(runtime, ir, ["store", "Point"], None))
    assert points["S3"] == spatial.make_point(22.6273, 120.3014)
    routes = dict(run(runtime, ir, ["store", "Route"], None))
    assert routes["S1"] == spatial.make_line(
        spatial.make_point(25.0330, 121.5654), spatial.make_point(*HOME)
    )


def test_datasource_filter_on_spatial_calc(runtime: DataRuntime) -> None:
    """資料源篩選用到空間函式（讀資料表的那一段）：取回用到的資料表，整段查詢在 DuckDB 執行。"""
    ir = workbook()
    ds = ir.datasources[0]
    distance = next(f for f in ds.fields if f.caption == "Distance")
    near = RangeFilter(
        kind="range",
        id="flt_near",
        field=FieldUse(field=distance.id),
        stage="datasource",
        max=10.0,
        source_ref="/test",
    )
    ir = ir.model_copy(update={"datasources": [ds.model_copy(update={"filters": [near]})]})
    f = {x.caption: x.id for x in ds.fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[{"field": f["region"]}], columns=[{"field": f["sales"], "agg": "sum"}])
    _, ws = replace_worksheet(ir, sheet)
    # 資料源篩選在讀出資料列之後（src），之前的 base 仍下推
    assert localize_spatial(worksheet_sql(_, ws.id))[1]
    found = dict(run(runtime, ir, ["region"], "sales"))
    # 只有台北的兩家店在 10 公里內
    assert found == {"North": 30.0}


def test_join_calculation_with_spatial_function(runtime: DataRuntime) -> None:
    """join 計算用到空間函式（讀資料表的那一段）：取回兩張資料表用到的欄，整段查詢在 DuckDB 執行。"""
    ir = workbook()
    ds = ir.datasources[0].id
    bands = [
        FileColumn(name="band", datatype="integer"),
        FileColumn(name="label", datatype="string"),
    ]
    home = f"MAKEPOINT({HOME[0]}, {HOME[1]})"
    formula = f"INT(DISTANCE(MAKEPOINT([lat], [lon]), {home}, 'km') / 100)"
    ir = add_live_table(
        ir,
        ds,
        ["public", "bands"],
        None,
        bands,
        how="join",
        on=[(Calculated(formula), "=", "band")],
    )
    f = {x.caption: x.id for x in ir.datasources[0].fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[{"field": f["label"]}], columns=[{"field": f["sales"], "agg": "sum"}])
    ir, ws = replace_worksheet(ir, sheet)
    query = worksheet_sql(ir, ws.id)
    assert not localize_spatial(query)[1]
    with pytest.raises(DialectError):
        [to_dialect(fetch.sql, "postgres") for fetch in split(query).fetches]
    expected: dict[str, float] = {}
    for _, _, lat, lon, sales, _ in STORES:
        # 高雄的店約 296 公里（第 2 段）
        label = {0: "near", 2: "far"}.get(int(expected_distance(lat, lon) / 100))
        if label is not None:
            expected[label] = expected.get(label, 0.0) + sales
    connection = ir.datasources[0].connection
    assert isinstance(connection, LiveConnection)
    result = runtime.run(query, ir.datasources[0], lambda: None)  # type: ignore[arg-type]
    assert dict(result.rows) == expected == {"near": 30.0, "far": 5.0}


def test_rows_fallback_matches_split(runtime: DataRuntime) -> None:
    """execute_rows（整段在 DuckDB）與兩段式執行的結果相同；只取用到的欄。"""
    ir = workbook()
    f = {x.caption: x.id for x in ir.datasources[0].fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[{"field": f["store"]}], columns=[{"field": f["Distance"], "agg": "sum"}])
    ir, ws = replace_worksheet(ir, sheet)
    query = worksheet_sql(ir, ws.id)
    assert source_tables(query.sql) == {("public", "stores"): {"store", "lat", "lon"}}
    connection = ir.datasources[0].connection
    assert isinstance(connection, LiveConnection)
    with runtime._executor(connection) as executor:  # pyright: ignore[reportPrivateUsage]
        rows = execute_rows(query, executor).rows
        assert rows == execute_live(query, executor).rows
    assert dict(rows)["S1"] == pytest.approx(0.0, abs=1e-9)


def test_split_keeps_rows_in_the_database() -> None:
    ir = workbook()
    f = {x.caption: x.id for x in ir.datasources[0].fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[{"field": f["region"]}], columns=[{"field": f["Distance"], "agg": "sum"}])
    ir, ws = replace_worksheet(ir, sheet)
    query = worksheet_sql(ir, ws.id)
    ctes, localized = localize_spatial(query)
    assert localized
    remote = [c.name for c in ctes if c.pushdown]
    assert remote == ["base", "src"]
    plan = split(query)
    (fetch,) = plan.fetches
    sql = to_dialect(fetch.sql, "postgres")
    # 只取用到的欄（地區與座標），空間函式不在來源資料庫的 SQL 中
    assert sql.endswith('SELECT "f_region", "f_lat", "f_lon" FROM "src"')
    assert "tmig_geo" not in sql.lower()
    assert "tmig_geo_distance" in plan.local_sql


def test_spatial_columns_become_wkt() -> None:
    """資料庫的空間欄位：在來源資料庫轉成 WKT 文字（Tableau 2021.2 支援的資料庫）。"""
    sql = 'SELECT tmig_geo_wkt("t"."geo") AS "f_geo" FROM "s"."t" AS "t"'
    expected = {
        "postgres": 'SELECT ST_ASTEXT("t"."geo")',
        "redshift": 'SELECT ST_ASTEXT("t"."geo")',
        "tsql": "SELECT [t].[geo].STAsText()",
        "oracle": 'SELECT SDO_UTIL.TO_WKTGEOMETRY("t"."geo")',
        "snowflake": 'SELECT ST_ASWKT("t"."geo")',
        "bigquery": "SELECT ST_ASTEXT(`t`.`geo`)",
        "hana": 'SELECT "t"."geo".ST_AsWKT()',
    }
    for dialect, start in expected.items():
        assert to_dialect(sql, dialect).startswith(start), dialect
    with pytest.raises(DialectError, match="空間欄位尚未支援"):
        to_dialect(sql, "databricks")
    # Live 連線的空間欄位在 base 中轉換
    columns = [
        FileColumn(name="name", datatype="string"),
        FileColumn(name="geo", datatype="spatial"),
    ]
    ir = new_live_workbook("Geo", PG, ["public", "areas"], None, columns)
    source = datasource_source(ir.datasources[0])
    assert 'tmig_geo_wkt("areas"."geo") AS "f_geo"' in source.from_sql


def test_rows_fallback_converts_spatial_columns(tmp_path: Any) -> None:
    """execute_rows：空間欄位在來源資料庫轉成 WKT（以 DuckDB 當作來源資料庫，tmig_geo_wkt 原樣傳回）。"""
    import duckdb

    from tmig.data.live import spatial_columns
    from tmig.data.sources import DuckDBExecutor

    path = tmp_path / "source.duckdb"
    con = duckdb.connect(str(path))
    con.execute('CREATE SCHEMA "geo"')
    con.execute('CREATE TABLE "geo"."areas" (name VARCHAR, shape VARCHAR, note VARCHAR)')
    con.execute(
        "INSERT INTO geo.areas VALUES ('A', 'POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))', 'x'), "
        "('B', 'POINT(5 5)', 'y')"
    )
    con.close()
    duck = LiveConnection(
        kind="live", connection_id="duck_geo", dbclass="duckdb", dialect="duckdb", database="geo"
    )
    columns = [
        FileColumn(name="name", datatype="string"),
        FileColumn(name="shape", datatype="spatial"),
        FileColumn(name="note", datatype="string"),
    ]
    ir = new_live_workbook("Geo", duck, ["geo", "areas"], None, columns)
    ds = ir.datasources[0].id
    ir, _ = upsert_calc(ir, ds, "Area", "AREA([shape], 'km')")
    f = {x.caption: x.id for x in ir.datasources[0].fields}
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[{"field": f["name"]}], columns=[{"field": f["Area"], "agg": "sum"}])
    ir, ws = replace_worksheet(ir, sheet)
    query = worksheet_sql(ir, ws.id)
    assert spatial_columns(query.sql) == {("geo", "areas"): {"shape"}}
    executor = DuckDBExecutor(path)
    try:
        rows = dict(execute_rows(query, executor).rows)
    finally:
        executor.close()
    expected = spatial.area("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))", "km")
    assert rows["A"] == pytest.approx(expected) and rows["B"] == 0
