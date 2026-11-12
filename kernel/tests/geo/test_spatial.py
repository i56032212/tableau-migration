"""空間資料（tmig.geo.spatial）：WKT、Hyper 讀入 WKT 時的處理、COLLECT 與大地測量。

與 Tableau 的引擎比對：同一份 WKT、同一組座標以 Hyper（tableauhyperapi）的 GEOGRAPHY 與 tableau.geo_* 函式
計算，結果與本平台相同（Hyper 內部的座標精度約 6e-8 度，數字的差距在 1e-6 以內）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from tmig.geo import spatial
from tmig.geo.extent import Box
from tmig.geo.spatial import SpatialError, format_wkt, normalize, parse_wkt

SAMPLES = [
    "POLYGON((120 22, 122 22, 122 25, 120 25, 120 22))",
    # 順時針的外環改成逆時針
    "POLYGON((0 0, 0 0.01, 0.01 0.01, 0 0))",
    "LINESTRING(10 0, 10.3 0, 10.3 0.3)",
    # 內環改成順時針
    "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 3 2, 3 3, 2 3, 2 2))",
    "MULTIPOLYGON(((0 0, 1 0, 1 1, 0 0)), ((2 2, 3 2, 3 3, 2 2)))",
    # 經度的門檻依起點所在的 9 度緯度帶放寬
    "LINESTRING(0 60, 0.2 60)",
    "LINESTRING(0 26.99, 0.1 27.01)",
    "LINESTRING(0 27.01, 0.1 26.99)",
    "POLYGON((-100 70, -60 70, -60 80, -100 80, -100 70))",
    "MULTIPOINT(1 2, 3 4)",
    "GEOMETRYCOLLECTION(POINT(1 2), LINESTRING(0 0, 0.01 0.01))",
    "LINESTRING Z (1 2 3, 4 5 6)",
    "POLYGON EMPTY",
]


@pytest.fixture(scope="module")
def hyper(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    tableauhyperapi = pytest.importorskip("tableauhyperapi")
    log = tmp_path_factory.mktemp("hyper-log")
    with (
        tableauhyperapi.HyperProcess(
            tableauhyperapi.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
            parameters={"log_dir": str(log)},
        ) as process,
        tableauhyperapi.Connection(process.endpoint) as conn,
    ):
        yield conn


def scalar(conn: Any, sql: str) -> Any:
    return conn.execute_list_query(f"SELECT {sql}")[0][0]


def point(lat: float, lon: float) -> str:
    return f"tableau.geo_make_point({lat}::double precision, {lon}::double precision)"


def numbers(text: str) -> list[float]:
    return [float(v) for v in re.findall(r"-?\d+\.\d+", text)]


def test_wkt_round_trip() -> None:
    geom = parse_wkt("MULTIPOLYGON (((1 2, 3 4, 5 6, 1 2)), ((7 8, 9 10, 11 12, 7 8)))")
    assert geom == {
        "type": "MultiPolygon",
        "coordinates": [
            [[[1, 2], [3, 4], [5, 6], [1, 2]]],
            [[[7, 8], [9, 10], [11, 12], [7, 8]]],
        ],
    }
    assert geom is not None
    assert format_wkt(geom).startswith("MULTIPOLYGON(((1.0000000 2.0000000, 3.0000000 4.0000000")
    assert parse_wkt("POINT Z (1 2 3)") == {"type": "Point", "coordinates": [1.0, 2.0]}
    assert parse_wkt("MULTIPOINT((1 2), (3 4))") == parse_wkt("MULTIPOINT(1 2, 3 4)")
    assert format_wkt(parse_wkt("POINT(-0.00000001 0)") or {}) == "POINT(0.0000000 0.0000000)"
    assert parse_wkt(None) is None and parse_wkt("  ") is None
    for broken in ("POLYGON((1 2, 3", "CIRCLE(1 2)", "POINT(1)", "POINT(1 2) extra"):
        with pytest.raises(SpatialError):
            parse_wkt(broken)


@pytest.mark.parametrize("wkt", SAMPLES)
def test_normalize_matches_hyper(hyper: Any, wkt: str) -> None:
    """與 Hyper 讀入 WKT（CAST AS GEOGRAPHY）後的文字相同：直線邊的切分、環的方向、空的形狀。"""
    expected = scalar(hyper, f"CAST(CAST('{wkt}' AS GEOGRAPHY) AS TEXT)")
    geom = parse_wkt(wkt)
    assert geom is not None
    found = normalize(geom)
    assert found is not None
    assert format_wkt(found) == expected


def test_collect_matches_hyper(hyper: Any) -> None:
    groups = [
        ["POINT(1 2)", "POINT(3 4)", "POLYGON((0 0, 0.01 0, 0.01 0.01, 0 0))"],
        ["POINT(1 2)", "POINT(1 2)"],
        ["POLYGON((0 0, 0.01 0, 0.01 0.01, 0 0))", "POLYGON((1 1, 1.01 1, 1.01 1.01, 1 1))"],
        ["POLYGON((0 0, 0.01 0, 0.01 0.01, 0 0))"],
        ["LINESTRING(0 0, 0.01 0.01)", "MULTILINESTRING((1 1, 1.01 1.01), (2 2, 2.01 2.01))"],
    ]
    for items in groups:
        texts = [scalar(hyper, f"CAST(CAST('{w}' AS GEOGRAPHY) AS TEXT)") for w in items]
        values = ", ".join(f"(CAST('{w}' AS GEOGRAPHY))" for w in items)
        expected = scalar(
            hyper, f"CAST(tableau.geo_collect(g) AS TEXT) FROM (VALUES {values}) t(g)"
        )
        assert spatial.collect(texts) == expected
    assert spatial.collect([None, None]) is None
    assert spatial.collect([]) is None


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ((25.03, 121.5), (22.6, 120.3)),
        ((0, 0), (0, 1)),
        ((0, 0), (1, 0)),
        ((60, 0), (61, 1)),
        ((-33.9, 18.4), (40.7, -74.0)),
        ((0, 0), (0.5, 179.7)),
    ],
)
@pytest.mark.parametrize("unit", ["m", "km", "miles", "FT"])
def test_distance_matches_hyper(
    hyper: Any, a: tuple[float, float], b: tuple[float, float], unit: str
) -> None:
    expected = scalar(hyper, f"tableau.geo_distance({point(*a)}, {point(*b)}, '{unit}')")
    found = spatial.distance(spatial.make_point(*a), spatial.make_point(*b), unit)
    assert found == pytest.approx(expected, rel=1e-7)


def test_make_point_and_line_match_hyper(hyper: Any) -> None:
    for lat, lon in [(25.03, 121.5), (-33.9, 18.4), (0, 200), (0, -180), (91, 0)]:
        expected = scalar(hyper, f"CAST({point(lat, lon)} AS TEXT)")
        assert spatial.make_point(lat, lon) == expected
    a, b = point(25.03, 121.5), point(22.6, 120.3)
    expected = scalar(hyper, f"CAST(tableau.geo_make_line({a}, {b}) AS TEXT)")
    assert spatial.make_line("POINT(121.5 25.03)", "POINT(120.3 22.6)") == expected
    # 不是點時為 Null
    assert spatial.make_line("POINT(1 2)", "POLYGON((0 0, 1 0, 1 1, 0 0))") is None
    assert spatial.distance("POINT(1 2)", "LINESTRING(0 0, 1 1)", "m") is None


@pytest.mark.parametrize(
    ("lat", "lon", "radius", "unit"),
    [(25.03, 121.5, 1, "km"), (60, 10, 50, "km"), (-33.9, 18.4, 300, "mi"), (0, 0, 800, "m")],
)
def test_buffer_matches_hyper(hyper: Any, lat: float, lon: float, radius: float, unit: str) -> None:
    """120 邊形、頂點在大地線圓上（由正北開始逆時針）：頂點的座標差距在 Hyper 的精度以內，面積相同。"""
    sql = f"tableau.geo_buffer({point(lat, lon)}, {radius}::double precision, '{unit}')"
    expected = scalar(hyper, f"CAST({sql} AS TEXT)")
    found = spatial.buffer(spatial.make_point(lat, lon), radius, unit)
    assert found is not None and found.startswith("POLYGON((")
    a, b = numbers(found), numbers(expected)
    assert len(a) == len(b) == 2 * (spatial.BUFFER_SIDES + 1)
    assert max(abs(x - y) for x, y in zip(a, b, strict=True)) <= 3e-7
    # Hyper 的頂點有約 7 公釐的座標精度：小的緩衝區面積的相對差距較大（800 公尺時約 2e-6）
    area = scalar(hyper, f"tableau.geo_area({sql}, 'm')")
    assert spatial.area(found, "m") == pytest.approx(area, rel=5e-6)
    assert spatial.buffer("POINT(1 2)", 0, "km") == "POINT(1.0000000 2.0000000)"
    assert spatial.buffer("POINT(1 2)", -1, "km") is None
    assert spatial.buffer("POINT(1 2)", 1, "yards") is None


@pytest.mark.parametrize(
    "wkt", [*SAMPLES[:5], "POLYGON((-100 70, -60 70, -60 80, -100 80, -100 70))"]
)
@pytest.mark.parametrize("unit", ["m", "km", "mi"])
def test_area_matches_hyper(hyper: Any, wkt: str, unit: str) -> None:
    stored = scalar(hyper, f"CAST(CAST('{wkt}' AS GEOGRAPHY) AS TEXT)")
    expected = scalar(hyper, f"tableau.geo_area(CAST('{wkt}' AS GEOGRAPHY), '{unit}')")
    found = spatial.area(stored, unit)
    assert found == pytest.approx(expected, rel=1e-6)
    assert spatial.area("POINT(1 2)", "m") == 0.0


def test_units() -> None:
    assert spatial.unit_factor("Kilometres") == 1000
    assert spatial.unit_factor(" MI ") == 1609.344
    assert spatial.unit_factor("ft") == 0.3048
    assert spatial.unit_factor("yards") is None


def test_shape_helpers() -> None:
    geom = parse_wkt("POLYGON((0 0, 10 0, 10 2, 2 2, 2 8, 10 8, 10 10, 0 10, 0 0))")
    assert spatial.bounds(geom) == (0, 0, 10, 10)
    # C 形的多邊形：重心在缺口中，標籤改放在通過重心的最長內部線段的中點
    label = spatial.label_point(geom)
    assert label is not None and 0 < label[0] < 2 and 0 < label[1] < 10
    assert spatial.label_point(parse_wkt("LINESTRING(0 0, 1 1, 2 2)")) == [1, 1]
    assert spatial.shape_type(parse_wkt("GEOMETRYCOLLECTION(POINT(1 2), POINT(3 4))")) == "Point"
    assert (
        spatial.shape_type(parse_wkt("GEOMETRYCOLLECTION(POINT(1 2), LINESTRING(0 0, 1 1))"))
        == "Mixed"
    )
    assert spatial.shape_type(parse_wkt("POLYGON EMPTY")) == "Empty"


def test_render_parts_are_simplified_with_d3_winding() -> None:
    """畫圖用的形狀：依畫面像素簡化，外環順時針、內環逆時針（d3 的方向）。"""
    ring = [[math.cos(t) * 0.01, math.sin(t) * 0.01] for t in np.linspace(0, 2 * math.pi, 2001)]
    ring[-1] = ring[0]
    hole = [[p[0] * 0.5, p[1] * 0.5] for p in ring]
    geom = {"type": "Polygon", "coordinates": [ring, hole[::-1]]}
    box = Box(0.0, 1.0, -1.0, 1.0)
    project = box.project(400, 400)
    polygons, lines, points = spatial.render_parts(geom, project.arrays)
    assert lines is None and points == []
    assert polygons is not None
    outer, inner = polygons["coordinates"][0]
    assert 4 <= len(outer) < 200 and outer[0] == outer[-1]
    assert spatial._signed_area(outer) < 0 < spatial._signed_area(inner)


def test_normalize_closes_and_orients_rings() -> None:
    """空間檔案讀進來的形狀也經過同樣的處理：環封閉、外環逆時針、1 度的邊切成 16 段。"""
    geom = normalize({"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1]]]})
    assert geom is not None
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1] and spatial._signed_area(ring) > 0
    assert len(ring) == 3 * 16 + 1


SQUARE_WITH_HOLE = "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0), (4 4, 6 4, 6 6, 4 6, 4 4))"
INTERSECTS_CASES = [
    # 點與多邊形：內部、邊上、頂點上、外面、內環（洞）中、內環的邊上
    ("POINT(5 2)", SQUARE_WITH_HOLE, True),
    ("POINT(10 5)", SQUARE_WITH_HOLE, True),
    ("POINT(0 0)", SQUARE_WITH_HOLE, True),
    ("POINT(10.0001 5)", SQUARE_WITH_HOLE, False),
    ("POINT(5 5)", SQUARE_WITH_HOLE, False),
    ("POINT(4 5)", SQUARE_WITH_HOLE, True),
    # 多點：任一點相交即相交
    ("MULTIPOINT(20 20, 5 5)", SQUARE_WITH_HOLE, False),
    ("MULTIPOINT(20 20, 1 1)", SQUARE_WITH_HOLE, True),
    # 線與多邊形：穿過（沒有頂點在多邊形中）、完全在洞中、在外面但範圍重疊
    ("LINESTRING(-1 5, 11 5)", SQUARE_WITH_HOLE, True),
    ("LINESTRING(4.5 4.5, 5.5 5.5)", SQUARE_WITH_HOLE, False),
    ("LINESTRING(-1 11, 11 11)", "POLYGON((0 0, 10 0, 0 10, 0 0))", False),
    # 多邊形與多邊形：包含、十字交叉（沒有頂點在對方中）、在洞中、相接於一個頂點、分開
    ("POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))", SQUARE_WITH_HOLE, True),
    ("POLYGON((-1 4, 11 4, 11 6, -1 6, -1 4))", "POLYGON((4 -1, 6 -1, 6 11, 4 11, 4 -1))", True),
    ("POLYGON((4.5 4.5, 5.5 4.5, 5.5 5.5, 4.5 5.5, 4.5 4.5))", SQUARE_WITH_HOLE, False),
    ("POLYGON((10 10, 12 10, 12 12, 10 10))", SQUARE_WITH_HOLE, True),
    ("POLYGON((0 0, 1 0, 0 1, 0 0))", "POLYGON((0.6 0.6, 2 0.6, 2 2, 0.6 0.6))", False),
    # 線與線：交叉、端點相接、平行、共線重疊
    ("LINESTRING(0 0, 2 2)", "LINESTRING(0 2, 2 0)", True),
    ("LINESTRING(0 0, 1 1)", "LINESTRING(1 1, 2 0)", True),
    ("LINESTRING(0 0, 2 0)", "LINESTRING(0 1, 2 1)", False),
    ("LINESTRING(0 0, 2 0)", "LINESTRING(1 0, 3 0)", True),
    # 點與點、點與線
    ("POINT(1 2)", "POINT(1 2)", True),
    ("POINT(1 2)", "POINT(1 2.0001)", False),
    ("POINT(1 1)", "LINESTRING(0 0, 2 2)", True),
    ("POINT(1 1.1)", "LINESTRING(0 0, 2 2)", False),
    # 形狀集合
    ("GEOMETRYCOLLECTION(POINT(50 50), LINESTRING(-1 5, 1 5))", SQUARE_WITH_HOLE, True),
]


@pytest.mark.parametrize(("a", "b", "expected"), INTERSECTS_CASES)
def test_intersects(a: str, b: str, expected: bool) -> None:
    """空間 join 的 Intersects：邊界也算相交（點在邊上、形狀相接）；內環（洞）中不算。"""
    assert spatial.intersects(a, b) is expected
    assert spatial.intersects(b, a) is expected


def test_intersects_nulls_and_invalid() -> None:
    assert spatial.intersects(None, SQUARE_WITH_HOLE) is None
    assert spatial.intersects("POINT(1 1)", "not a shape") is None
    assert spatial.intersects("POINT EMPTY", SQUARE_WITH_HOLE) is None
    assert spatial.bounds_list("POINT(121.5 25)") == [121.5, 25, 121.5, 25]
    assert spatial.bounds_list(SQUARE_WITH_HOLE) == [0, 0, 10, 10]
    assert spatial.bounds_list(None) is None


def test_intersects_pairs_matches_one_by_one() -> None:
    """一批配對（DuckDB 的 Arrow 函式）：點依另一邊的形狀分組計算，結果與逐一計算相同。"""
    rng = np.random.default_rng(7)
    shapes = [case[1] for case in INTERSECTS_CASES] + [case[0] for case in INTERSECTS_CASES]
    points = [f"POINT({x:.7f} {y:.7f})" for x, y in rng.uniform(-2, 12, size=(300, 2))]
    points += ["POINT(10 5)", "POINT(4 5)", "POINT(0 0)"]
    left = sorted(set(points + shapes[:6]))
    right = [*sorted(set(shapes)), "broken"]
    left_index = rng.integers(-1, len(left), size=4000)
    right_index = rng.integers(-1, len(right), size=4000)
    result, null = spatial.intersects_pairs(left, left_index, right, right_index)
    for row in range(len(left_index)):
        i, j = int(left_index[row]), int(right_index[row])
        expected = spatial.intersects(left[i] if i >= 0 else None, right[j] if j >= 0 else None)
        if expected is None:
            assert null[row]
        else:
            assert not null[row] and bool(result[row]) is expected, (left[i], right[j])


def test_intersects_in_duckdb() -> None:
    """DuckDB 中的空間 join：範圍的不等式縮小配對、形狀先在資料表的子查詢中算好。"""
    import duckdb

    from tmig.data import udf
    from tmig.ir.models import JoinCondition, JoinRelation, JoinTerm, TableRelation
    from tmig.sqlgen.source import _relation_sql

    con = udf.register(duckdb.connect())
    con.execute("CREATE TABLE zones(zone VARCHAR, shape VARCHAR)")
    con.execute(
        "INSERT INTO zones VALUES ('square', ?), ('far', 'POLYGON((50 50, 51 50, 51 51, 50 50))')",
        [SQUARE_WITH_HOLE],
    )
    con.execute("CREATE TABLE sites(site VARCHAR, lat DOUBLE, lon DOUBLE)")
    con.execute(
        "INSERT INTO sites VALUES ('in', 2, 5), ('hole', 5, 5), ('edge', 5, 10), "
        "('away', 30, 30), ('missing', NULL, 1)"
    )
    relation = JoinRelation(
        kind="join",
        join="left",
        left=TableRelation(kind="table", name="sites", table=["sites"]),
        right=TableRelation(kind="table", name="zones", table=["zones"]),
        on=[
            JoinCondition(
                left=JoinTerm(formula="MAKEPOINT([sites].[lat], [sites].[lon])"),
                op="intersects",
                right=JoinTerm(table="zones", column="shape"),
            )
        ],
    )
    columns = {("sites", "lat"): "real", ("sites", "lon"): "real", ("zones", "shape"): "spatial"}
    from_sql = _relation_sql(relation, [], columns)
    # 形狀與範圍在 join 之前算好（每列一次）
    assert from_sql.count(udf.GEO_POINT) == 1
    rows = con.execute(
        f'SELECT "sites"."site", "zones"."zone" FROM {from_sql} ORDER BY 1'
    ).fetchall()
    assert rows == [
        ("away", None),
        ("edge", "square"),
        ("hole", None),
        ("in", "square"),
        ("missing", None),
    ]
    con.close()
