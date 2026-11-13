"""MySQL 的空間欄位：以臨時的 MySQL 8 實跑（沒有時略過；TMIG_REQUIRE_ENGINES=1 時改為失敗）。

MySQL 8 的地理座標系統（SRID 4326）依 EPSG 的軸序把 WKT 寫成「緯度 經度」：轉成 WKT 時指定經度在前；
兩邊都是空間欄位的空間 join 在 MySQL 以 ST_Intersects 計算。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from tmig.authoring import add_live_table, new_live_workbook
from tmig.data.catalog import table_columns
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig
from tmig.edit import replace_worksheet
from tmig.geo import spatial
from tmig.ir import IR, FileColumn, LiveConnection
from tmig.sqlgen.query import worksheet_sql

from ..engines import mysql_connect, mysql_server

MYSQL = LiveConnection(
    kind="live", connection_id="my_geo", dbclass="mysql", dialect="mysql", database="tmig_geo"
)


@pytest.fixture(scope="module")
def runtime() -> Iterator[DataRuntime]:
    with mysql_server() as params:
        con = mysql_connect(params, "tmig_geo")
        with con.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE districts (name VARCHAR(20), boundary POLYGON SRID 4326 NOT NULL)"
            )
            # 地理座標系統的 WKT 依 EPSG 的軸序（緯度在前）
            cursor.execute(
                "INSERT INTO districts VALUES ('North', ST_GeomFromText("
                "'POLYGON((25 121.5, 25 121.6, 25.1 121.6, 25.1 121.5, 25 121.5))', 4326))"
            )
            cursor.execute(
                "CREATE TABLE sites (site VARCHAR(20), visitors DOUBLE, "
                "location POINT SRID 4326 NOT NULL)"
            )
            cursor.execute(
                "INSERT INTO sites VALUES "
                "('S1', 10, ST_SRID(POINT(121.5654, 25.0330), 4326)), "
                "('S2', 20, ST_SRID(POINT(121.5170, 25.0478), 4326)), "
                "('S3', 5, ST_SRID(POINT(120.3014, 22.6273), 4326))"
            )
        con.close()
        configs = {
            "my_geo": ConnectionConfig("my_geo", "mysql", {**params, "database": "tmig_geo"})
        }
        with DataRuntime(configs) as found:
            yield found


class Fetcher:
    def __init__(self, runtime: DataRuntime) -> None:
        self.runtime = runtime

    def fetch(self, sql: str) -> pa.Table:
        return self.runtime.fetch(MYSQL, sql)


def columns_of(runtime: DataRuntime, table: str) -> list[FileColumn]:
    return table_columns(Fetcher(runtime), "mysql", ["tmig_geo", table])


def run(runtime: DataRuntime, ir: IR, rows: list[str], column: dict[str, Any]) -> dict[Any, Any]:
    f = {x.caption: x.id for x in ir.datasources[0].fields}
    data = ir.worksheets[0].model_dump(mode="json")
    data.update(
        rows=[{"field": f[r]} for r in rows], columns=[{**column, "field": f[column["field"]]}]
    )
    ir, ws = replace_worksheet(ir, data)
    result = runtime.run(worksheet_sql(ir, ws.id), ir.datasources[0], lambda: None)  # type: ignore[arg-type]
    return {row[0]: row[1] for row in result.rows}


def test_mysql_spatial_columns_and_join(runtime: DataRuntime) -> None:
    sites = columns_of(runtime, "sites")
    assert [(c.name, c.datatype) for c in sites] == [
        ("site", "string"),
        ("visitors", "real"),
        ("location", "spatial"),
    ]
    ir = new_live_workbook("Sites", MYSQL, ["tmig_geo", "sites"], None, sites)
    # 經度在前的 WKT（與其他資料庫、萃取相同）
    points = run(runtime, ir, ["site"], {"field": "location", "agg": "collect"})
    assert spatial.parse_wkt(points["S1"]) == {"type": "Point", "coordinates": [121.5654, 25.033]}
    ir = add_live_table(
        ir,
        ir.datasources[0].id,
        ["tmig_geo", "districts"],
        None,
        columns_of(runtime, "districts"),
        how="join",
        join="left",
        on=[
            (
                next(f.id for f in ir.datasources[0].fields if f.caption == "location"),
                "intersects",
                "boundary",
            )
        ],
    )
    assert run(runtime, ir, ["name"], {"field": "visitors", "agg": "sum"}) == {
        "North": 30.0,
        None: 5.0,
    }
    # 多邊形也是經度在前（join 後 North 有兩列，COLLECT 為兩個相同的多邊形）
    shapes = run(runtime, ir, ["name"], {"field": "boundary", "agg": "collect"})
    north = spatial.parse_wkt(shapes["North"])
    assert north is not None and north["type"] == "MultiPolygon"
    assert north["coordinates"][0][0][:2] == [[121.5, 25.0], [121.6, 25.0]]
