"""背景圖片在分格、多個座標軸與地圖上（第三批）。

Tableau 2021.2 說明文件「Use Background Images in Your Views」
（https://help.tableau.com/v2021.2/pro/desktop/en-us/bkimages.htm ，2026-11-23 查閱）：X、Y 欄位在欄、列架上時
顯示；地圖（產生的緯度、經度）要先把背景地圖關掉（地圖＞背景地圖＞無）才顯示；背景地圖「無」是「a viz type
other than a map」（maps_mapsources.htm）。分格、多個座標軸的情況文件沒有寫（推定：每個窗格各自畫、依 X、Y
欄位的座標軸對齊；條件的欄位是分格的維度時只畫在這個值的窗格）。.twb 中背景地圖「無」寫成
``<mapsources><mapsource name='' /></mapsources>``，固定的座標軸範圍是經緯度、沒有 projection 屬性（寫法取自
GitHub 上公開的 .twb：05_Background_Map、Comuni italiani e popolazione、2022_density_map）。

圖片的位置以 Vega 的 scale('比例尺', 值) 換算；分格與多個座標軸時比例尺的名稱依 Vega-Lite 為各窗格產生的
名稱（concat_0_x、layer_1_layer_1_y；以 Vega-Lite 6.4.1 編譯驗證過，見 resolve_scales）。

測試資料見 tests/sqlgen/test_query.py：各區的 SUM(Sales) East 350、North 60、South 100、West 800。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tmig.data.executor import execute_query
from tmig.ir import IR
from tmig.ir.models import LATITUDE_GENERATED, LONGITUDE_GENERATED
from tmig.parser.workbook import parse_workbook
from tmig.render.background import resolve_scales
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from . import test_maps as geo
from .test_background_images import PNG, mapped, parse
from .test_text import PROFIT, REGION, SALES, db, render
from .test_vegalite import validator

__all__ = ["db"]  # pytest fixture

QUANTITY = instance("[Quantity]", "Sum", "[sum:Quantity:qk]", "quantitative")


def images_in(node: Any) -> list[dict[str, Any]]:
    """規格中所有的背景圖片圖層（依出現的順序）。"""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if str(node.get("name", "")).startswith("__deco__bg"):
            found.append(node)
        for value in node.values():
            found += images_in(value)
    elif isinstance(node, list):
        for item in node:
            found += images_in(item)
    return found


def region_condition(region: str) -> str:
    return (
        "<groupfilter function='crossjoin'>"
        f"<groupfilter function='member' level='[Region]' member='&quot;{region}&quot;' />"
        "</groupfilter>"
    )


def test_faceted_view_draws_the_image_in_every_pane(tmp_path: Path, db: Path) -> None:
    """列架上的 Region 把散布圖分成四個窗格：每個窗格各自畫圖片（共用的比例尺 x、y）；條件是 Region 為
    East 的圖片只在 East 的窗格。"""
    sheet = worksheet(
        "Panes",
        [REGION, SALES, PROFIT],
        rows=f"({ref('none:Region:nk')} / {ref('sum:Profit:qk')})",
        cols=ref("sum:Sales:qk"),
        mark="Circle",
    )
    images = mapped() + mapped("MappedImage2", "East only", condition=region_condition("East"))
    spec = render(parse(tmp_path, images, sheet), db).spec
    panes = spec["vconcat"]
    assert len(panes) == 4
    region = next(c for c, key in spec["usermeta"]["dimensions"].items() if key == "f_region")
    labels = []
    for pane in panes:
        floor, east = pane["layer"][0], pane["layer"][1]
        assert (floor["name"], east["name"]) == ("__deco__bg0", "__deco__bg1")
        assert "scale('x', 1000.0)" in floor["mark"]["width"]["expr"]
        assert "scale('y', 50.0)" in floor["mark"]["height"]["expr"]
        # 沒有條件的圖片每個窗格都畫；條件的欄位是分格的維度：圖片的資料帶著 East，窗格的篩選（這個窗格的
        # Region）只留 East 的窗格
        assert all(region not in t["filter"] for t in floor["transform"])
        assert east["data"]["values"][0][region] == "East"
        label = pane["title"]["text"]
        (pane_filter,) = [t["filter"] for t in east["transform"] if region in t["filter"]]
        assert pane_filter == f'datum["{region}"] === "{label}"'
        labels.append(label)
    assert labels == ["East", "North", "South", "West"]
    assert spec["usermeta"]["background_images"] == [
        {"image": "__bg_image0", "url": "__bg_url0", "flip_x": False, "flip_y": False},
        {"image": "__bg_image1", "url": "__bg_url1", "flip_x": False, "flip_y": False},
    ]


def test_several_axes_on_a_shelf(tmp_path: Path, db: Path) -> None:
    """列架上兩個量值（兩個座標軸）：圖片只畫在 Y 欄位（Profit）的座標軸那一張，比例尺是那一張自己的
    （concat_0_x、concat_0_y）；雙軸時依 Y 欄位所在的座標軸（獨立的比例尺 layer_1_layer_1_y）。"""
    two = f"({ref('sum:Profit:qk')} + {ref('sum:Quantity:qk')})"
    sheet = worksheet(
        "Two", [REGION, SALES, PROFIT, QUANTITY], rows=two, cols=ref("sum:Sales:qk"), mark="Circle"
    )
    spec = render(parse(tmp_path, mapped(), sheet), db).spec
    profit, quantity = spec["vconcat"]
    (image,) = images_in(profit)
    assert images_in(quantity) == []
    assert "scale('concat_0_x', 0.0)" in image["mark"]["x"]["expr"]
    assert "scale('concat_0_y', 50.0)" in image["mark"]["y"]["expr"]

    # 雙軸：Profit 是第一個軸、Quantity 是第二個軸；圖片的 Y 是 Quantity
    dual = worksheet(
        "Dual",
        [REGION, SALES, PROFIT, QUANTITY],
        rows=f"({ref('sum:Profit:qk')} + {ref('sum:Quantity:qk')})",
        cols=ref("sum:Sales:qk"),
        mark="Circle",
        panes_extra="".join(
            f"<pane id='{n}' y-axis-name='{ref(axis)}'{index}><view><breakdown value='auto' /></view>"
            "<mark class='Circle' /><encodings /></pane>"
            for n, axis, index in ((2, "sum:Profit:qk", ""), (3, "sum:Quantity:qk", " y-index='1'"))
        ),
    )
    on_quantity = mapped(
        mapping="bottom='0.0' left='0.0' right='1000.0' top='10.0' x='[Sales]' y='[Quantity]'"
    )
    spec = render(parse(tmp_path, on_quantity, dual), db).spec
    (image,) = images_in(spec)
    assert spec["layer"][0] is image
    inner = spec["layer"][1]
    assert inner["resolve"]["scale"]["y"] == "independent"
    assert "scale('x', 1000.0)" in image["mark"]["width"]["expr"]
    assert "scale('layer_1_layer_1_y', 10.0)" in image["mark"]["height"]["expr"]


def test_scale_names_follow_vega_lite() -> None:
    """比例尺的名稱：共用的層往上合併（最上層是 x、y），各自獨立時是那一張圖的名稱（Vega-Lite 6.4.1 編譯的
    結果：vconcat 預設各自獨立；巢狀時 concat_0_concat_1_x；圖層預設共用）。"""

    def image(x: str, y: str) -> dict[str, Any]:
        return {
            "name": "__deco__bg0",
            "data": {"values": [{}]},
            "mark": {
                "type": "image",
                "x": {"expr": f"scale('__bg_scale:x:{x}', 1)"},
                "y": {"expr": f"scale('__bg_scale:y:{y}', 1)"},
            },
        }

    def unit(x: str, y: str) -> dict[str, Any]:
        return {
            "mark": "point",
            "encoding": {
                "x": {"field": x, "type": "quantitative"},
                "y": {"field": y, "type": "quantitative"},
            },
        }

    spec: dict[str, Any] = {
        "vconcat": [
            {
                "hconcat": [
                    {"layer": [image("a", "b"), unit("a", "b")]},
                    {"layer": [image("a", "b"), unit("a", "b")]},
                ],
                "resolve": {"scale": {"x": "independent", "y": "shared"}},
            }
        ],
        "resolve": {"scale": {"x": "shared", "y": "independent"}},
    }
    resolve_scales(spec)
    first, second = spec["vconcat"][0]["hconcat"]
    assert first["layer"][0]["mark"]["x"]["expr"] == "scale('concat_0_concat_0_x', 1)"
    assert first["layer"][0]["mark"]["y"]["expr"] == "scale('concat_0_y', 1)"
    assert second["layer"][0]["mark"]["x"]["expr"] == "scale('concat_0_concat_1_x', 1)"


# ---------------------------------------------------------------- 地圖

MAP_IMAGE = (
    "<mapped-images><mapped-image caption='Texas' expression='file:Image/floor.png' name='[MappedImage1]'>"
    "<mapping bottom='25.0' left='-107.0' right='-93.0' top='37.0' "
    "x='[Longitude (generated)]' y='[Latitude (generated)]' />"
    "<image-options show-entire-image='true' /></mapped-image></mapped-images>"
)
NO_BASEMAP = "<mapsources><mapsource name='' /></mapsources>"


def map_ir(tmp_path: Path, view_extra: str = "", style: str = "") -> IR:
    sheet = geo.map_sheet(
        "Cities",
        [geo.STATE, geo.CITY, geo.SALES],
        "Circle",
        f"<lod column='{b.ref('none:City:nk')}' /><lod column='{b.ref('none:State:nk')}' />",
        view_extra=view_extra,
    )
    if style:
        sheet = sheet.replace("<style />", f"<style>{style}</style>")
    datasource = b.extract_datasource(
        columns=geo.GEO_COLUMNS + MAP_IMAGE, extract_inner=geo.metadata()
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = b.write_twbx(
        tmp_path, b.workbook(datasource, worksheets=sheet), {"Image/floor.png": PNG}
    )
    ir = parse_workbook(path, extracts_omitted=True)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


def map_spec(ir: IR, db: Path) -> dict[str, Any]:
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    from tmig.render.vegalite import build_spec

    rendered = build_spec(ir, ws.id, query.columns, execute_query(db, query))
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered.spec


def test_map_shows_the_image_only_without_a_basemap(tmp_path: Path) -> None:
    """產生的緯度、經度：有背景地圖時不顯示（與 Tableau 相同，要先關掉背景地圖）；背景地圖選「無」時不畫
    底圖、以等距的經緯度畫出，圖片的四角是對應的經緯度、畫在最下層。"""
    db = geo_db(tmp_path)
    with_map = map_spec(map_ir(tmp_path / "a"), db)
    assert images_in(with_map) == [] and "background_images" not in with_map["usermeta"]
    assert with_map["projection"]["type"] == "mercator"

    ir = map_ir(tmp_path / "b", NO_BASEMAP)
    image = ir.datasources[0].background_images[0]
    assert (image.x, image.y) == (LONGITUDE_GENERATED, LATITUDE_GENERATED)
    ws = ir.worksheets[0]
    assert ws.map is not None and ws.map.basemap is False
    spec = map_spec(ir, db)
    assert spec["projection"]["type"] == "equirectangular"
    (layer,) = images_in(spec)
    assert spec["layer"][0] is layer
    assert layer["data"]["values"][0] | {"__bg_url0": None} == {
        "__bg_image0": "Image/floor.png",
        "__bg_url0": None,
        "lon": -107.0,
        "lat": 25.0,
        "lon2": -93.0,
        "lat2": 37.0,
    }
    assert set(layer["encoding"]) == {"longitude", "latitude", "longitude2", "latitude2", "url"}
    # 沒有底圖（陸地、國界、地名）；「N 個未知」的指示器照常
    deco = [
        str(x.get("name")) for x in spec["layer"] if str(x.get("name", "")).startswith("__deco_map")
    ]
    assert deco == ["__deco_map_unknown"]
    assert "view" not in spec or spec["view"].get("fill") is None
    assert spec["usermeta"]["background_images"] == [
        {"image": "__bg_image0", "url": "__bg_url0", "flip_x": False, "flip_y": False}
    ]
    # 一律顯示整張圖片：自動的範圍包含圖片的四角
    fit = spec["projection"]["fit"]["geometry"]["coordinates"]
    assert fit[0][0] <= -107.0 and fit[1][0] >= -93.0 and fit[0][1] <= 25.0 and fit[1][1] >= 37.0


def test_no_basemap_degree_viewport(tmp_path: Path) -> None:
    """背景地圖「無」時存檔的固定範圍是經緯度（沒有 projection 屬性）：照樣是顯示的範圍。"""
    style = (
        "<style-rule element='axis'>"
        f"<encoding attr='space' class='0' field='{geo.GEN_LON}' field-type='quantitative' max='-90.0' "
        "min='-110.0' range-type='fixed' scope='cols' type='space' />"
        f"<encoding attr='space' class='0' field='{geo.GEN_LAT}' field-type='quantitative' max='40.0' "
        "min='20.0' range-type='fixed' scope='rows' type='space' />"
        "</style-rule>"
    )
    ws = map_ir(tmp_path, NO_BASEMAP, style).worksheets[0]
    assert ws.map is not None and ws.map.viewport is not None
    v = ws.map.viewport
    assert (v.west, v.south, v.east, v.north) == (-110.0, 20.0, -90.0, 40.0)


def geo_db(tmp_path: Path) -> Path:
    import duckdb

    path = tmp_path / "geo.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Country" VARCHAR, "State" VARCHAR, "City" VARCHAR, '
            '"Zip" BIGINT, "Sales" DOUBLE, "Lat" DOUBLE, "Lon" DOUBLE)'
        )
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', geo.ROWS)
    return path
