"""產生地圖用的地名資料與形狀（kernel/src/tmig/geo/data）。

來源（先以 tools/geodata/fetch.sh 下載到 --sources）：
- Natural Earth 5.x（公有領域）：國家（110m、50m、10m）、州／省（10m）、人口聚居地（10m）
- GeoNames（CC BY 4.0，npm 套件 cities.json）：人口 1000 以上的城市
- 美國人口普查局（公有領域，npm 套件 us-atlas）：郡的形狀
- npm 套件 zipcodes（BSD）：美國郵遞區號的位置
- npm 套件 i18n-iso-countries（MIT）：各語言的國名

輸出：
- places.parquet：每個地點一列（id、角色、國家、州、名稱、位置、範圍、形狀檔、底圖標籤的層級）
- names.parquet：比對鍵（正規化的名稱、縮寫與代碼）→ 地點
- shapes/：TopoJSON（countries-110m／50m／10m.json、admin1/<國家>.json、us-counties.json），檢視器取用

用法：uv run python tools/geodata/build.py --sources <下載的目錄> --mapshaper <mapshaper 的路徑>
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kernel" / "src"))

from tmig.geo.extent import covering_arc  # noqa: E402
from tmig.geo.names import variants  # noqa: E402

OUT = ROOT / "kernel" / "src" / "tmig" / "geo" / "data"

# 中文資料常見的國名（Natural Earth 的繁體中文名稱是正式國名）
COUNTRY_ALIASES = {
    "TWN": ["台灣", "臺灣", "中華民國", "Taiwan, Province of China", "Republic of China"],
    "CHN": ["中國", "中国", "中國大陸", "中国大陆", "大陸", "大陆", "Mainland China", "PRC"],
    "KOR": ["韓國", "韩国", "南韓", "南韩", "Korea", "Korea, South", "Republic of Korea"],
    "PRK": ["北韓", "北韩", "朝鮮", "Korea, North"],
    "HKG": ["香港", "Hong Kong SAR"],
    "MAC": ["澳門", "澳门", "Macao", "Macau SAR"],
    "USA": ["美國", "美国", "United States", "USA", "US", "U.S.", "U.S.A.", "America"],
    "GBR": ["英國", "英国", "UK", "U.K.", "Great Britain", "Britain", "England"],
    "RUS": ["俄羅斯", "俄罗斯", "Russia"],
    "VNM": ["越南", "Vietnam", "Viet Nam"],
    "CZE": ["Czech Republic"],
    "MKD": ["Macedonia"],
    "SWZ": ["Swaziland"],
}
# 州：美國、加拿大與澳洲的郵政縮寫在 Natural Earth 的 postal 欄位；中文資料常見的寫法
STATE_ALIASES = {
    "TWN": {"臺北市": ["台北", "臺北"], "新北市": ["新北"], "桃園市": ["桃園"]},
}
# Natural Earth 沒有的州級行政區（太小）：只有位置，填色地圖上以圓點呈現
STATE_EXTRAS = [
    {
        "country": "TWN",
        "code": "TWN-LJ",
        "name": "Lienchiang",
        "names": [
            "連江縣",
            "连江县",
            "連江",
            "Lienchiang",
            "Lienchiang County",
            "Lianjiang",
            "馬祖",
            "Matsu",
        ],
        "lat": 26.1608,
        "lon": 119.9516,
        "bounds": (119.85, 25.93, 120.52, 26.40),
        "rank": 8.0,
    },
]
COUNTY_SUFFIXES = (
    " county",
    " parish",
    " borough",
    " census area",
    " city and borough",
    " municipality",
)


def features(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def coordinates(geometry: dict[str, Any] | None) -> Iterator[tuple[float, float]]:
    if not geometry:
        return

    def walk(value: Any) -> Iterator[tuple[float, float]]:
        if value and isinstance(value[0], (int, float)):
            yield float(value[0]), float(value[1])
        else:
            for item in value:
                yield from walk(item)

    yield from walk(geometry.get("coordinates", []))


KEEP_AREA = 0.15
"""自動縮放的範圍：面積至少是最大一塊的這個比例（且距離在 FAR_KM 之內）的部分才算入。"""
FAR_KM = 1500.0
NEAR_KM = 400.0
"""距離最大一塊在這之內的部分（例如近海的島嶼）不論大小都算入。"""


def outer_rings(geometry: dict[str, Any] | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"][0]]
    if geometry["type"] == "MultiPolygon":
        return [polygon[0] for polygon in geometry["coordinates"]]
    return []


def ring_area(ring: list[list[float]]) -> float:
    """面積（平方公里，以平均緯度近似）。"""
    total = sum(x0 * y1 - x1 * y0 for (x0, y0, *_), (x1, y1, *_) in itertools.pairwise(ring))
    lat = sum(p[1] for p in ring) / len(ring)
    return abs(total) / 2 * 111.32 * 110.57 * math.cos(math.radians(lat))


def box_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """兩個範圍之間的距離（公里，經度考慮跨越 180 度經線）。"""
    dx = min(max(0.0, a[0] + shift - b[2], b[0] - (a[2] + shift)) for shift in (-360.0, 0.0, 360.0))
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    lat = math.radians((a[1] + a[3] + b[1] + b[3]) / 4)
    return math.hypot(dx * 111.32 * math.cos(lat), dy * 110.57)


def bounds(
    geometry: dict[str, Any] | None, lon: float, lat: float
) -> tuple[float, float, float, float]:
    """自動縮放的範圍（西、南、東、北）：與主要的陸塊相近或夠大的部分（遠方的小島，例如法國的海外省、
    夏威夷的西北群島不算入）；跨越 180 度經線的範圍（例如俄羅斯、斐濟）west > east。"""
    rings = [r for r in outer_rings(geometry) if len(r) >= 3]
    if not rings:
        points = list(coordinates(geometry))
        if not points:
            return lon, lat, lon, lat
        rings = [[[x, y] for x, y in points]]
    parts = []
    for ring in rings:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        parts.append((ring_area(ring), (min(xs), min(ys), max(xs), max(ys))))
    largest_area, largest = max(parts, key=lambda part: part[0])
    kept = [
        box
        for area, box in parts
        if (area >= KEEP_AREA * largest_area and box_distance(box, largest) <= FAR_KM)
        or box_distance(box, largest) <= NEAR_KM
    ]
    west, east = covering_arc([(box[0], box[2]) for box in kept])
    return west, min(box[1] for box in kept), east, max(box[3] for box in kept)


def split_names(*values: Any) -> list[str]:
    found: list[str] = []
    for value in values:
        if value is None or value == "" or value == -99 or value == "-99":
            continue
        for part in str(value).split("|"):
            part = part.strip()
            if part and part not in found:
                found.append(part)
    return found


class Builder:
    def __init__(self, sources: Path, mapshaper: str) -> None:
        self.sources = sources
        self.mapshaper = mapshaper
        self.places: list[dict[str, Any]] = []
        self.names: dict[tuple[str, str], int] = {}
        """（比對鍵、地點）→ 優先順序：0 名稱、1 代碼與縮寫、2 別名與其他語言的名稱。"""
        self.country_by_a2: dict[str, str] = {}
        self.country_by_a3: dict[str, str] = {}
        self.state_by_gn: dict[str, str] = {}
        self.state_by_postal: dict[tuple[str, str], str] = {}
        self.state_by_name: dict[tuple[str, str], str] = {}

    def add(
        self,
        place: dict[str, Any],
        names: Iterable[Any],
        codes: Iterable[Any] = (),
        alternates: Iterable[Any] = (),
    ) -> None:
        self.places.append(place)
        for priority, group in enumerate((names, codes, alternates)):
            for name in split_names(*group):
                for key in variants(name):
                    item = (key, place["id"])
                    self.names[item] = min(self.names.get(item, priority), priority)

    # ------------------------------------------------------------ 國家

    def countries(self) -> None:
        langs = self.sources / "npm" / "i18n-iso-countries" / "langs"
        translated: dict[str, set[str]] = {}
        for file in sorted(langs.glob("*.json")):
            data = json.loads(file.read_text(encoding="utf-8"))
            for a2, value in data["countries"].items():
                for name in value if isinstance(value, list) else [value]:
                    translated.setdefault(a2, set()).add(name)
        countries = features(self.sources / "ne" / "ne_10m_admin_0_countries.geojson")
        # 屬地（例如克利珀頓島、澳洲的珊瑚海群島）與本國共用 ISO 代碼：代碼只給本國（ADM0_A3 與 ISO 代碼
        # 相同的那一個），否則兩者都比對到而無法決定；沒有本國的代碼（例如巴勒斯坦 PSX／PSE）照常
        owners = {
            f["properties"]["ISO_A3_EH"]: f["properties"]["ADM0_A3"]
            for f in countries
            if f["properties"]["ISO_A3_EH"] == f["properties"]["ADM0_A3"]
        }
        for feature in countries:
            p = feature["properties"]
            a3 = p["ADM0_A3"]
            a2 = p["ISO_A2_EH"] if p["ISO_A2_EH"] not in ("-99", None) else None
            iso3 = p["ISO_A3_EH"] if p["ISO_A3_EH"] not in ("-99", None) else None
            lon, lat = float(p["LABEL_X"]), float(p["LABEL_Y"])
            west, south, east, north = bounds(feature["geometry"], lon, lat)
            place_id = f"country:{a3}"
            names = [p["NAME"], p["NAME_LONG"], p["ADMIN"], p["NAME_EN"], p["NAME_SORT"]]
            if iso3 is not None and owners.get(iso3, a3) != a3:
                a2 = iso3 = None
            codes = [a2, iso3, a3, p["FIPS_10"]]
            alternates = [
                p["NAME_ALT"],
                p["FORMAL_EN"],
                p["BRK_NAME"],
                p["NAME_CIAWF"],
                p["GEOUNIT"],
                p["SUBUNIT"],
                *[p[k] for k in p if k.startswith("NAME_") and len(k) <= 8 and k != "NAME_LEN"],
                *sorted(translated.get(a2 or "", set())),
                *COUNTRY_ALIASES.get(a3, []),
            ]
            self.add(
                {
                    "id": place_id,
                    "role": "country",
                    "country": a3,
                    "admin1": None,
                    "name": p["NAME"],
                    "lat": lat,
                    "lon": lon,
                    "west": west,
                    "south": south,
                    "east": east,
                    "north": north,
                    "shape": "countries",
                    "feature": a3,
                    "rank": float(p["MIN_LABEL"]),
                },
                names,
                codes,
                alternates,
            )
            self.country_by_a3[a3] = a3
            if a2:
                self.country_by_a2.setdefault(a2, a3)
            if iso3:
                self.country_by_a3.setdefault(iso3, a3)

    # ------------------------------------------------------------ 州、省

    def states(self) -> None:
        for feature in features(self.sources / "ne" / "ne_10m_admin_1_states_provinces.geojson"):
            p = feature["properties"]
            country = str(self.country_by_a3.get(p["adm0_a3"]) or p["adm0_a3"])
            code = p["adm1_code"]
            lon, lat = float(p["longitude"]), float(p["latitude"])
            west, south, east, north = bounds(feature["geometry"], lon, lat)
            place_id = f"state:{code}"
            iso = p.get("iso_3166_2") or ""
            short = iso.split("-", 1)[1] if "-" in iso else None
            postal = p.get("postal")
            names = [p["name"]]
            codes = [iso, p.get("code_hasc"), p["name_en"]]
            if postal and country in ("USA", "CAN", "AUS", "MEX", "BRA", "IND"):
                codes.append(postal)
            if short and country in ("USA", "CAN", "AUS"):
                codes.append(short)
            alternates = [
                p["name_alt"],
                p["name_local"],
                p["gn_name"],
                p["woe_name"],
                p["gns_name"],
                *[p[k] for k in p if k.startswith("name_") and len(k) <= 8 and k != "name_len"],
            ]
            for official, extra in STATE_ALIASES.get(country, {}).items():
                if official in (p.get("name_zht"), p.get("name_zh")):
                    alternates += extra
            self.add(
                {
                    "id": place_id,
                    "role": "state",
                    "country": country,
                    "admin1": place_id,
                    "name": p["name"],
                    "lat": lat,
                    "lon": lon,
                    "west": west,
                    "south": south,
                    "east": east,
                    "north": north,
                    "shape": f"admin1/{country}",
                    "feature": code,
                    "rank": float(p["min_label"] or 6),
                },
                names,
                codes,
                alternates,
            )
            if p.get("gn_a1_code"):
                self.state_by_gn.setdefault(p["gn_a1_code"], place_id)
            if postal:
                self.state_by_postal.setdefault((country, postal.upper()), place_id)
            for name in split_names(p["name"], p["name_en"], p["gn_name"], p["name_alt"]):
                for key in variants(name):
                    self.state_by_name.setdefault((country, key), place_id)

        for extra in STATE_EXTRAS:
            place_id = f"state:{extra['code']}"
            west, south, east, north = extra["bounds"]
            self.add(
                {
                    "id": place_id,
                    "role": "state",
                    "country": extra["country"],
                    "admin1": place_id,
                    "name": extra["name"],
                    "lat": extra["lat"],
                    "lon": extra["lon"],
                    "west": west,
                    "south": south,
                    "east": east,
                    "north": north,
                    "shape": None,
                    "feature": None,
                    "rank": extra["rank"],
                },
                [extra["name"]],
                [extra["code"]],
                extra["names"],
            )
            for name in extra["names"]:
                for key in variants(name):
                    self.state_by_name.setdefault((extra["country"], key), place_id)

    # ------------------------------------------------------------ 城市

    def cities(self) -> None:
        base = self.sources / "npm" / "cities.json"
        admin1 = {
            a["code"]: a["name"]
            for a in json.loads((base / "admin1.json").read_text(encoding="utf-8"))
        }
        for n, city in enumerate(json.loads((base / "cities.json").read_text(encoding="utf-8"))):
            country = self.country_by_a2.get(city["country"])
            if country is None:
                continue
            code = f"{city['country']}.{city['admin1']}"
            state = self.state_by_gn.get(code)
            if state is None and code in admin1:
                for key in variants(admin1[code]):
                    state = self.state_by_name.get((country, key))
                    if state:
                        break
            lat, lon = float(city["lat"]), float(city["lng"])
            self.add(
                {
                    "id": f"city:gn{n}",
                    "role": "city",
                    "country": country,
                    "admin1": state,
                    "name": city["name"],
                    "lat": lat,
                    "lon": lon,
                    "west": lon,
                    "south": lat,
                    "east": lon,
                    "north": lat,
                    "shape": None,
                    "feature": None,
                    "rank": None,
                },
                [city["name"]],
            )
        # Natural Earth 的人口聚居地：多語言的名稱（含中文）與人口，也是底圖的城市標籤
        for feature in features(self.sources / "ne" / "ne_10m_populated_places.geojson"):
            p = feature["properties"]
            country = self.country_by_a3.get(p["ADM0_A3"])
            if country is None:
                continue
            state = None
            for key in variants(p.get("ADM1NAME")):
                state = self.state_by_name.get((country, key))
                if state:
                    break
            lat, lon = float(p["LATITUDE"]), float(p["LONGITUDE"])
            self.add(
                {
                    "id": f"city:ne{p['NE_ID']}",
                    "role": "city",
                    "country": country,
                    "admin1": state,
                    "name": p["NAME"],
                    "lat": lat,
                    "lon": lon,
                    "west": lon,
                    "south": lat,
                    "east": lon,
                    "north": lat,
                    "shape": None,
                    "feature": None,
                    "rank": float(p["MIN_ZOOM"] or 10),
                    "population": int(p["POP_MAX"] or 0),
                },
                [p["NAME"], p["NAMEASCII"], p["NAME_EN"]],
                (),
                [
                    p["NAMEALT"],
                    p["NAMEPAR"],
                    *[p[k] for k in p if k.startswith("NAME_") and len(k) <= 8],
                ],
            )

    # ------------------------------------------------------------ 美國的郡與郵遞區號

    def counties(self) -> None:
        states_by_fips = {
            fips: postal
            for postal, fips in (
                ("AL", "01"), ("AK", "02"), ("AZ", "04"), ("AR", "05"), ("CA", "06"), ("CO", "08"),
                ("CT", "09"), ("DE", "10"), ("DC", "11"), ("FL", "12"), ("GA", "13"), ("HI", "15"),
                ("ID", "16"), ("IL", "17"), ("IN", "18"), ("IA", "19"), ("KS", "20"), ("KY", "21"),
                ("LA", "22"), ("ME", "23"), ("MD", "24"), ("MA", "25"), ("MI", "26"), ("MN", "27"),
                ("MS", "28"), ("MO", "29"), ("MT", "30"), ("NE", "31"), ("NV", "32"), ("NH", "33"),
                ("NJ", "34"), ("NM", "35"), ("NY", "36"), ("NC", "37"), ("ND", "38"), ("OH", "39"),
                ("OK", "40"), ("OR", "41"), ("PA", "42"), ("RI", "44"), ("SC", "45"), ("SD", "46"),
                ("TN", "47"), ("TX", "48"), ("UT", "49"), ("VT", "50"), ("VA", "51"), ("WA", "53"),
                ("WV", "54"), ("WI", "55"), ("WY", "56"), ("PR", "72"),
            )
        }  # fmt: skip
        source = self.sources / "npm" / "us-atlas" / "counties-10m.json"
        with tempfile.TemporaryDirectory() as tmp:
            geojson = Path(tmp) / "counties.json"
            self.mapshaper_run(
                "-i", str(source), "-target", "counties", "-o", str(geojson), "format=geojson"
            )
            for feature in features(geojson):
                fips = str(feature.get("id") or feature["properties"].get("id"))
                state = self.state_by_postal.get(("USA", states_by_fips.get(fips[:2], "")))
                points = list(coordinates(feature["geometry"]))
                if not points:
                    continue
                west, south, east, north = bounds(feature["geometry"], points[0][0], points[0][1])
                lon, lat = centroid(feature["geometry"]) or ((west + east) / 2, (south + north) / 2)
                name = feature["properties"]["name"]
                names = [name]
                lowered = name.lower()
                if not any(lowered.endswith(s) for s in COUNTY_SUFFIXES):
                    names += [f"{name} County", f"{name} Parish", f"{name} Borough"]
                self.add(
                    {
                        "id": f"county:{fips}",
                        "role": "county",
                        "country": "USA",
                        "admin1": state,
                        "name": name,
                        "lat": lat,
                        "lon": lon,
                        "west": west,
                        "south": south,
                        "east": east,
                        "north": north,
                        "shape": "us-counties",
                        "feature": fips,
                        "rank": None,
                    },
                    names,
                    [fips],
                )

    def zips(self) -> None:
        text = (self.sources / "npm" / "zipcodes" / "lib" / "codes.js").read_text(encoding="utf-8")
        start = text.index("exports.codes")
        data, _ = json.JSONDecoder().raw_decode(text, text.index("{", start))
        for code, item in data.items():
            if item.get("country") != "US" or item.get("latitude") in (None, ""):
                continue
            lat, lon = float(item["latitude"]), float(item["longitude"])
            self.add(
                {
                    "id": f"zip:US:{code}",
                    "role": "zip",
                    "country": "USA",
                    "admin1": self.state_by_postal.get(("USA", str(item.get("state", "")).upper())),
                    "name": code,
                    "lat": lat,
                    "lon": lon,
                    "west": lon,
                    "south": lat,
                    "east": lon,
                    "north": lat,
                    "shape": None,
                    "feature": None,
                    "rank": None,
                },
                [code],
            )

    # ------------------------------------------------------------ 形狀

    def mapshaper_run(self, *args: str) -> None:
        subprocess.run([self.mapshaper, *args], check=True, capture_output=True, text=True)

    def shapes(self, out: Path) -> None:
        shapes = out / "shapes"
        if shapes.exists():
            shutil.rmtree(shapes)
        (shapes / "admin1").mkdir(parents=True)
        ne = self.sources / "ne"
        for scale, simplify in (("110m", None), ("50m", "40%"), ("10m", "6%")):
            command = [
                self.mapshaper,
                "-i",
                str(ne / f"ne_{scale}_admin_0_countries_lakes.geojson"),
                "name=countries",
                "-filter-fields",
                "ADM0_A3,NAME",
                "-rename-fields",
                "name=NAME",
            ]
            if simplify:
                command += ["-simplify", simplify, "keep-shapes"]
            # 國界：相鄰國家共用的邊界線（不含海岸線）
            command += ["-innerlines", "+", "name=borders"]
            command += [
                "-o",
                str(shapes / f"countries-{scale}.json"),
                "format=topojson",
                "id-field=ADM0_A3",
                "quantization=100000",
                "target=countries,borders",
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
        with tempfile.TemporaryDirectory() as tmp:
            # 州：全球一起簡化（共用的邊界一致），再依國家分檔
            simplified = Path(tmp) / "admin1.json"
            subprocess.run(
                [
                    self.mapshaper,
                    "-i",
                    str(ne / "ne_10m_admin_1_states_provinces_lakes.geojson"),
                    "name=admin1",
                    "-filter-fields",
                    "adm1_code,adm0_a3,name",
                    "-simplify",
                    "8%",
                    "keep-shapes",
                    "-o",
                    str(simplified),
                    "format=geojson",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            by_country: dict[str, list[dict[str, Any]]] = {}
            for feature in features(simplified):
                a3 = str(feature["properties"]["adm0_a3"])
                by_country.setdefault(self.country_by_a3.get(a3) or a3, []).append(feature)
            for country, members in by_country.items():
                part = Path(tmp) / f"{country}.geojson"
                part.write_text(
                    json.dumps({"type": "FeatureCollection", "features": members}), encoding="utf-8"
                )
                subprocess.run(
                    [
                        self.mapshaper,
                        "-i",
                        str(part),
                        "name=admin1",
                        "-filter-fields",
                        "adm1_code,name",
                        "-innerlines",
                        "+",
                        "name=borders",
                        "-o",
                        str(shapes / "admin1" / f"{country}.json"),
                        "format=topojson",
                        "id-field=adm1_code",
                        "quantization=100000",
                        "target=admin1,borders",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
        shutil.copy(
            self.sources / "npm" / "us-atlas" / "counties-10m.json", shapes / "us-counties.json"
        )

    # ------------------------------------------------------------ 輸出

    def write(self, out: Path) -> None:
        import pyarrow as pa

        out.mkdir(parents=True, exist_ok=True)
        columns = ["id", "role", "country", "admin1", "name", "lat", "lon", "west", "south"]
        columns += ["east", "north", "shape", "feature", "rank", "population"]
        rounded = {"lat", "lon", "west", "south", "east", "north"}
        places = pa.table(
            {
                c: [round(p[c], 5) if c in rounded else p.get(c) for p in self.places]
                for c in columns
            },
            schema=pa.schema(
                [
                    (c, pa.float64() if c in rounded or c == "rank" else pa.int64())
                    if c in rounded or c in ("rank", "population")
                    else (c, pa.string())
                    for c in columns
                ]
            ),
        )
        items = sorted(self.names.items())
        names = pa.table(
            {
                "norm": [key for (key, _), _ in items],
                "id": [place for (_, place), _ in items],
                "priority": pa.array([priority for _, priority in items], pa.int8()),
            }
        )
        con = duckdb.connect()
        con.register("places_source", places)
        con.register("names_source", names)
        con.execute(
            f"COPY (SELECT * FROM places_source ORDER BY role, country, id) "
            f"TO '{out / 'places.parquet'}' (FORMAT parquet, COMPRESSION zstd)"
        )
        con.execute(
            f"COPY (SELECT * FROM names_source ORDER BY norm, id) "
            f"TO '{out / 'names.parquet'}' (FORMAT parquet, COMPRESSION zstd)"
        )


def centroid(geometry: dict[str, Any]) -> tuple[float, float] | None:
    """多邊形（外圈）的面積加權中心。"""
    polygons = (
        [geometry["coordinates"]]
        if geometry["type"] == "Polygon"
        else geometry["coordinates"]
        if geometry["type"] == "MultiPolygon"
        else []
    )
    total = cx = cy = 0.0
    for polygon in polygons:
        ring = polygon[0]
        for (x0, y0), (x1, y1) in itertools.pairwise(ring):
            cross = x0 * y1 - x1 * y0
            total += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
    if math.isclose(total, 0.0):
        return None
    return cx / (3 * total), cy / (3 * total)


def main() -> int:
    parser = argparse.ArgumentParser(description="產生地圖用的地名資料與形狀")
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--mapshaper", default="mapshaper")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--skip-shapes", action="store_true", help="只重新產生地名資料")
    args = parser.parse_args()
    builder = Builder(args.sources, args.mapshaper)
    builder.countries()
    builder.states()
    builder.cities()
    builder.counties()
    builder.zips()
    builder.write(args.out)
    if not args.skip_shapes:
        builder.shapes(args.out)
    roles: dict[str, int] = {}
    for place in builder.places:
        roles[place["role"]] = roles.get(place["role"], 0) + 1
    # 形狀檔的版本：檢視器以它組成網址，形狀更新後瀏覽器不會沿用快取
    digest = hashlib.sha256()
    for file in sorted((args.out / "shapes").rglob("*.json")):
        digest.update(file.relative_to(args.out).as_posix().encode())
        digest.update(file.read_bytes())
    manifest = {"version": digest.hexdigest()[:12], "places": roles, "keys": len(builder.names)}
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    size = sum(f.stat().st_size for f in args.out.rglob("*") if f.is_file())
    print(f"地點 {roles}，比對鍵 {len(builder.names):,}，共 {size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
