"""地名的比對（tmig.geo）：與 Tableau 相同，以最細的地理角色為準、較粗的角色與資料源的預設國家為範圍。"""

from __future__ import annotations

import gzip
import json

import pytest

from tmig.geo import SHAPES, version
from tmig.geo.extent import Box, covering_arc
from tmig.geo.gazetteer import gazetteer
from tmig.geo.geocode import GeoSettings, finest, geocode
from tmig.geo.names import normalize, variants
from tmig.geo.topojson import load


def names(results: list) -> list[str | None]:
    return [r.place.name if r.place is not None else None for r in results]


def test_normalize_and_variants() -> None:
    assert normalize("  São   Paulo ") == "sao paulo"
    assert normalize("臺北市") == normalize("台北市")
    assert normalize("St. Louis") == "st louis"
    assert normalize("Ａｒｉｚｏｎａ") == "arizona"  # 全形字
    assert "saint louis" in variants("St. Louis")
    assert "st paul" in variants("Saint Paul")


def test_countries_in_many_spellings() -> None:
    values = [
        "United States",
        "USA",
        "US",
        "台灣",
        "Taiwan",
        "中國",
        "Deutschland",
        "FR",
        "Atlantis",
    ]
    found = geocode([{"c": v} for v in values], {"country": "c"})
    assert [r.place.country if r.place else None for r in found] == [
        "USA",
        "USA",
        "USA",
        "TWN",
        "TWN",
        "CHN",
        "DEU",
        "FRA",
        None,
    ]
    assert found[-1].status == "unknown"


def test_states_with_default_country_and_codes() -> None:
    settings = GeoSettings(default_country="United States")
    found = geocode(
        [{"s": v} for v in ("Texas", "CA", "New York", "Florda")], {"state": "s"}, settings
    )
    assert names(found) == ["Texas", "California", "New York", None]
    # 「編輯位置」中指定的對應
    fixed = GeoSettings(default_country="United States", aliases={"state": {"Florda": "Florida"}})
    assert names(geocode([{"s": "Florda"}], {"state": "s"}, fixed)) == ["Florida"]
    # 台灣的縣市（中文、別名）
    taiwan = geocode(
        [{"s": "臺北市"}, {"s": "台北"}, {"s": "高雄市"}],
        {"state": "s"},
        GeoSettings(default_country="台灣"),
    )
    assert [r.place.country for r in taiwan if r.place] == ["TWN", "TWN", "TWN"]


def test_cities_use_the_state_in_the_view() -> None:
    records = [
        {"city": "Springfield", "state": "Illinois"},
        {"city": "Springfield", "state": "Massachusetts"},
        {"city": "Portland", "state": "Oregon"},
        {"city": "Portland", "state": "Maine"},
    ]
    found = geocode(records, {"city": "city", "state": "state"}, GeoSettings("United States"))
    assert [r.status for r in found] == ["ok"] * 4
    assert found[0].place.lon < -89 and found[1].place.lon > -73  # type: ignore[union-attr]
    assert found[2].place.lon < -120 and found[3].place.lon > -71  # type: ignore[union-attr]


def test_counties_and_zip_codes() -> None:
    found = geocode(
        [{"county": "Los Angeles County", "state": "CA"}, {"county": "Cook", "state": "IL"}],
        {"county": "county", "state": "state"},
        GeoSettings("United States"),
    )
    assert [r.place.feature for r in found if r.place] == ["06037", "17031"]
    zips = geocode([{"z": 98101}, {"z": 623.0}, {"z": "98052-1234"}, {"z": None}], {"zip": "z"})
    assert [r.place.name if r.place else None for r in zips] == ["98101", "00623", "98052", None]


def test_finest_role() -> None:
    assert finest(["country", "state"]) == "state"
    assert finest(["zip", "city", "state"]) == "zip"
    assert finest([]) is None


def test_covering_arc_and_boxes() -> None:
    assert covering_arc([(19.6, 180.0), (-180.0, -169.0)]) == (19.6, -169.0)
    assert covering_arc([(-124.0, -66.0), (-168.0, -141.0)]) == (-168.0, -66.0)
    # 跨越 180 度經線的範圍：中心在太平洋
    box = Box.of([(170.0, -20.0, -170.0, -10.0)])
    assert box is not None
    assert abs(box.center) == pytest.approx(180.0, abs=1e-6)
    assert box.half == pytest.approx(10.0)
    # 存檔的範圍超過一整圈（環繞世界）：顯示整個世界
    wide = Box.viewport(106.78, -53.46, 596.16, 85.33)
    assert wide.half == pytest.approx(179.99) and wide.north == 85.0
    projector = Box.of([(-124.0, 24.0, -66.0, 49.0)]).project(800, 500)  # type: ignore[union-attr]
    x, y = projector(-95.0, 37.0)
    lon, lat = projector.invert(x, y)
    assert (lon, lat) == (pytest.approx(-95.0), pytest.approx(37.0))


def test_fit_bounds_skip_remote_islands() -> None:
    """自動縮放的範圍只算主要的陸塊：法國不含海外省、美國含阿拉斯加、俄羅斯跨越 180 度經線。"""
    found = geocode([{"c": c} for c in ("France", "United States", "Russia")], {"country": "c"})
    france, usa, russia = (r.place for r in found)
    assert france is not None and france.west > -10 and france.east < 10
    assert usa is not None and usa.west < -160 and usa.north > 70 and usa.south > 20
    assert russia is not None and russia.west > russia.east  # 跨越 180 度經線


def test_shapes_and_labels_data() -> None:
    assert version() != "dev"
    countries = load(SHAPES / "countries-110m.json", "countries")
    assert "USA" in countries and countries["USA"]["type"] == "MultiPolygon"
    topology = json.loads((SHAPES / "admin1" / "USA.json").read_text(encoding="utf-8"))
    assert set(topology["objects"]) == {"admin1", "borders"}
    assert len(gzip.compress((SHAPES / "countries-50m.json").read_bytes())) < 400_000
    labels = gazetteer().within("state", -125, 24, -66, 50, 4.0, 100, ["USA"])
    assert {"Texas", "California"} <= {p.name for p in labels}
    in_view = {p.country for p in gazetteer().overlapping("country", [(-125.0, 24.0, -66.0, 50.0)])}
    assert {"USA", "CAN", "MEX"} <= in_view
