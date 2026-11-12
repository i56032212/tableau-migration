"""投影座標 → 經緯度（tmig.geo.projection）：EPSG Guidance Note 7-2 與 Snyder 的範例、.prj 的解析與 EPSG 代碼。"""

from __future__ import annotations

import pytest

from tmig.geo.projection import (
    BESSEL,
    CLARKE66,
    Crs,
    ProjectionError,
    crs_from_epsg,
    crs_from_prj,
    make_point_srid,
)

US_FOOT = 0.3048006096012192


def dms(d: float, m: float = 0, s: float = 0) -> float:
    return d + m / 60 + s / 3600


EXAMPLES = [
    # OSGB 1936 / British National Grid（橫麥卡托）
    (crs_from_epsg(27700), (0.5, 50.5), (577274.99, 69740.50)),
    # NAD27 / Texas South Central（Lambert 正形圓錐 2SP，美國測量英尺）
    (
        Crs(
            "lcc",
            ellipsoid=CLARKE66,
            lat0=dms(27, 50),
            lon0=-99,
            sp1=dms(28, 23),
            sp2=dms(30, 17),
            fe=2000000 * US_FOOT,
            unit=US_FOOT,
        ),
        (-96, 28.5),
        (2963503.91, 254759.80),
    ),
    # ETRS89 / LAEA Europe（Lambert 方位等積）
    (crs_from_epsg(3035), (5, 50), (3962799.45, 2999718.85)),
    # Makassar / NEIEZ（麥卡托 1SP）
    (
        Crs("mercator", ellipsoid=BESSEL, lon0=110, k0=0.997, fe=3900000, fn=900000),
        (120, -3),
        (5009726.58, 569150.82),
    ),
    # WGS 84 / Pseudo-Mercator
    (crs_from_epsg(3857), (-dms(100, 20), dms(24, 22, 54.433)), (-11169055.58, 2800000.00)),
]


@pytest.mark.parametrize(("crs", "lonlat", "xy"), EXAMPLES)
def test_epsg_examples(crs: Crs, lonlat: tuple[float, float], xy: tuple[float, float]) -> None:
    x, y = crs.forward([lonlat[0]], [lonlat[1]])
    assert (float(x[0]), float(y[0])) == pytest.approx(xy, abs=0.01)
    lon, lat = crs.inverse([xy[0]], [xy[1]])
    assert (float(lon[0]), float(lat[0])) == pytest.approx(lonlat, abs=1e-7)


def test_albers_snyder_example() -> None:
    crs = Crs("albers", ellipsoid=CLARKE66, lat0=23, lon0=-96, sp1=29.5, sp2=45.5)
    x, y = crs.forward([-75], [35])
    assert (float(x[0]), float(y[0])) == pytest.approx((1885472.7, 1535925.0), abs=0.05)
    lon, lat = crs.inverse([1885472.7], [1535925.0])
    assert (float(lon[0]), float(lat[0])) == pytest.approx((-75, 35), abs=1e-6)


@pytest.mark.parametrize(
    ("code", "lon", "lat"),
    [
        (3826, 121.5645, 25.0339),
        (3825, 118.3, 24.45),
        (32651, 121.5645, 25.0339),
        (32733, 18.4, -33.9),
        (26918, -74.0, 40.7),
        (2154, 2.35, 48.85),
        (5070, -75, 35),
        (3310, -118.25, 34.05),
        (6677, 139.76, 35.68),
        (5179, 127.0, 37.5),
        (2193, 174.78, -41.29),
        (3414, 103.85, 1.29),
    ],
)
def test_round_trip(code: int, lon: float, lat: float) -> None:
    crs = crs_from_epsg(code)
    x, y = crs.forward([lon], [lat])
    back_lon, back_lat = crs.inverse(x, y)
    assert (float(back_lon[0]), float(back_lat[0])) == pytest.approx((lon, lat), abs=1e-9)


def test_twd97_known_point() -> None:
    """TWD97 二度分帶的原點：中央經線 121 度、假東距 250000 公尺，赤道上北距為 0。"""
    crs = crs_from_epsg(3826)
    lon, lat = crs.inverse([250000.0], [0.0])
    assert (float(lon[0]), float(lat[0])) == pytest.approx((121.0, 0.0), abs=1e-12)
    x, _ = crs.forward([121.0], [23.5])
    assert float(x[0]) == pytest.approx(250000.0, abs=1e-6)


def test_prj_files() -> None:
    twd97 = crs_from_prj(
        'PROJCS["TWD_1997_TM_Taiwan",GEOGCS["GCS_TWD_1997",DATUM["D_TWD_1997",'
        'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
        'UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["False_Easting",250000.0],PARAMETER["False_Northing",0.0],'
        'PARAMETER["Central_Meridian",121.0],PARAMETER["Scale_Factor",0.9999],'
        'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
    )
    assert twd97 == crs_from_epsg(3826).__class__(
        "tm",
        ellipsoid=twd97.ellipsoid,
        lon0=121.0,
        k0=0.9999,
        fe=250000.0,
        name="TWD_1997_TM_Taiwan",
    )
    # 美國州平面座標（英尺）：假東距寫在英尺，換成公尺
    long_island = crs_from_prj(
        'PROJCS["NAD_1983_StatePlane_New_York_Long_Island_FIPS_3104_Feet",'
        'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
        'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
        'UNIT["Degree",0.0174532925199433]],PROJECTION["Lambert_Conformal_Conic"],'
        'PARAMETER["False_Easting",984250.0],PARAMETER["False_Northing",0.0],'
        'PARAMETER["Central_Meridian",-74.0],PARAMETER["Standard_Parallel_1",40.66666666666666],'
        'PARAMETER["Standard_Parallel_2",41.03333333333333],'
        'PARAMETER["Latitude_Of_Origin",40.16666666666666],UNIT["Foot_US",0.3048006096012192]]'
    )
    assert long_island.kind == "lcc"
    assert long_island.fe == pytest.approx(300000.0)
    x, y = long_island.forward([-73.9857], [40.7484])  # 帝國大廈
    assert 980000 < float(x[0]) < 1000000 and 200000 < float(y[0]) < 230000
    web = crs_from_prj(
        'PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",GEOGCS["GCS_WGS_1984",'
        'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Mercator_Auxiliary_Sphere"],PARAMETER["False_Easting",0.0],'
        'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",0.0],'
        'PARAMETER["Standard_Parallel_1",0.0],PARAMETER["Auxiliary_Sphere_Type",0.0],'
        'UNIT["Meter",1.0]]'
    )
    assert web.kind == "web_mercator"
    geographic = crs_from_prj(
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
    )
    lon, lat = geographic.inverse([121.5], [25.0])
    assert (float(lon[0]), float(lat[0])) == pytest.approx((121.5, 25.0), abs=1e-12)
    with pytest.raises(ProjectionError, match="polar_stereographic"):
        crs_from_prj(
            'PROJCS["x",GEOGCS["g",DATUM["d",SPHEROID["s",6378137,298.257223563]],PRIMEM["G",0],'
            'UNIT["Degree",0.0174532925199433]],PROJECTION["Polar_Stereographic"],UNIT["Meter",1]]'
        )


def test_make_point_with_srid() -> None:
    crs = crs_from_epsg(3826)
    x, y = crs.forward([121.5645], [25.0339])
    assert make_point_srid(float(x[0]), float(y[0]), 3826) == "POINT(121.5645000 25.0339000)"
    assert make_point_srid(121.5, 25.0, 4326) == "POINT(121.5000000 25.0000000)"
    assert make_point_srid(1.0, 2.0, 999999) is None
    with pytest.raises(ProjectionError):
        crs_from_epsg(999999)
