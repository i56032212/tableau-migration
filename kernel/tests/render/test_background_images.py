"""背景圖片（地圖＞背景圖片）：解析 <mapped-images> 與工作表停用的圖片，畫在標記之下、對齊座標軸的範圍，
存報表套件時一併帶著圖片。

寫法取自 GitHub 上公開的 .twb／.twbx（ZoneMaps／TeamByMatch 的 field_black_background、Meeting Room
Utilisation Analysis 的樓層平面圖與條件、Hong Kong MTR 的 show-entire-image、smartphone 的淡化、Punto 2 的
lock-aspect-ratio、radar_chart 的工作表停用）。

測試資料見 tests/sqlgen/test_query.py：各區的 SUM(Sales) East 350、North 60、South 100、West 800；
SUM(Profit) East 45、West 20、South 10、North 6。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tmig.ir import IR, ImageCondition
from tmig.ir.integrity import check_references
from tmig.package import image_paths
from tmig.parser.workbook import parse_workbook

from ..parser import builder as b
from ..parser.builder import DS, ref, worksheet
from .test_text import PROFIT, REGION, SALES, db, render

__all__ = ["db"]  # pytest fixture

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


def mapped(
    name: str = "MappedImage1",
    caption: str = "Floor",
    expression: str = "file:Image/floor.png",
    mapping: str = "bottom='0.0' left='0.0' right='1000.0' top='50.0' x='[Sales]' y='[Profit]'",
    options: str = "",
    condition: str = "",
) -> str:
    return (
        f"<mapped-image caption='{caption}' expression='{expression}' name='[{name}]'>"
        f"<mapping {mapping} />{options}{condition}</mapped-image>"
    )


def scatter(name: str = "Scatter", style: str = "", rows: str | None = None) -> str:
    """各區的 SUM(Sales)（欄）與 SUM(Profit)（列）的散布圖。"""
    sheet = worksheet(
        name,
        [REGION, SALES, PROFIT],
        rows=rows if rows is not None else ref("sum:Profit:qk"),
        cols=ref("sum:Sales:qk"),
        mark="Circle",
        encodings=f"<lod column='{ref('none:Region:nk')}' />",
    )
    return sheet.replace("<style />", f"<style>{style}</style>") if style else sheet


def disabled(name: str = "MappedImage1") -> str:
    return (
        "<style-rule element='mapped-image'>"
        f"<format attr='enabled' id='[{DS}].[{name}]' value='false' /></style-rule>"
    )


def parse(tmp_path: Path, images: str, sheets: str, files: dict[str, bytes] | None = None) -> IR:
    xml = b.workbook(
        b.extract_datasource(extra=f"<mapped-images>{images}</mapped-images>"), worksheets=sheets
    )
    path = b.write_twbx(tmp_path, xml, {"Image/floor.png": PNG} if files is None else files)
    return parse_workbook(path, extracts_omitted=True)


def layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        layer
        for layer in spec.get("layer", [])
        if str(layer.get("name", "")).startswith("__deco__bg")
    ]


# ---------------------------------------------------------------- 解析


def test_background_images_are_parsed(tmp_path: Path) -> None:
    images = mapped() + mapped(
        "MappedImage2",
        "Upper",
        "file:///C:/Users/someone/Pictures/floor.png",
        options=(
            "<image-options desaturate-image='true' desaturate-pct='0.73' "
            "lock-aspect-ratio='false' show-entire-image='true' />"
        ),
        condition=(
            "<groupfilter function='crossjoin'>"
            "<groupfilter function='member' level='[Region]' member='&quot;East&quot;' />"
            "</groupfilter>"
        ),
    )
    ir = parse(tmp_path, images, scatter() + scatter("Plain", disabled()))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert check_references(ir) == []
    first, second = ir.datasources[0].background_images
    assert (first.name, first.image, first.x, first.y) == (
        "Floor",
        "Image/floor.png",
        "f_sales",
        "f_profit",
    )
    assert (first.left, first.right, first.top, first.bottom) == (0.0, 1000.0, 50.0, 0.0)
    assert (first.washout, first.lock_aspect, first.show_entire, first.conditions) == (
        0,
        True,
        False,
        [],
    )
    # 電腦上的路徑：.twbx 的 Image/ 中有同名的檔案時用它
    assert second.image == "Image/floor.png"
    assert (second.washout, second.lock_aspect, second.show_entire) == (0.73, False, True)
    assert [(c.field.field, c.value) for c in second.conditions] == [("f_region", "East")]
    sheets = {w.name: w for w in ir.worksheets}
    assert sheets["Scatter"].background_images_off == []
    assert sheets["Plain"].background_images_off == [first.id]
    # 存報表套件時帶著圖片
    assert "Image/floor.png" in image_paths(ir)


def test_missing_and_unknown_images(tmp_path: Path) -> None:
    """報表中沒有的圖片檔略過（畫不出來，不影響數字）；網址略過；認不得的寫法不支援。"""
    images = (
        mapped("MappedImage1", "Gone", "file:///C:/elsewhere/nothing.png")
        + mapped("MappedImage2", "Web", "https://example.com/floor.png")
        + mapped("MappedImage3", "Odd", options="<image-options tile='true' />")
        + mapped(
            "MappedImage4",
            "Odd Condition",
            condition="<groupfilter function='union'><groupfilter function='member' level='[Region]' member='&quot;East&quot;' /></groupfilter>",
        )
    )
    ir = parse(tmp_path, images, scatter())
    assert [u.reason for u in ir.unsupported] == [
        "背景圖片「Odd」：選項 tile 不支援",
        "背景圖片「Odd Condition」：顯示的條件（function=union）不支援",
    ]
    ignored = [u.reason for u in ir.ignored if u.reason.startswith("背景圖片")]
    assert len(ignored) == 2 and all("不在報表中" in r for r in ignored)
    assert ir.datasources[0].background_images == []


# ---------------------------------------------------------------- 呈現


def test_image_is_drawn_under_the_marks(tmp_path: Path, db: Path) -> None:
    ir = parse(tmp_path, mapped(), scatter())
    spec = render(ir, db).spec
    (layer,) = layers(spec)
    assert spec["layer"][0] is layer  # 最下層
    assert layer["data"]["values"] == [{"__bg_image0": "Image/floor.png", "__bg_url0": None}]
    mark = layer["mark"]
    assert (mark["type"], mark["aspect"], mark["clip"]) == ("image", True, True)
    assert mark["x"] == {"expr": "(scale('x', 0.0) + scale('x', 1000.0)) / 2"}
    assert mark["height"] == {"expr": "abs(scale('y', 0.0) - scale('y', 50.0))"}
    assert "opacity" not in mark
    assert spec["usermeta"]["background_images"] == [
        {"image": "__bg_image0", "url": "__bg_url0", "flip_x": False, "flip_y": False}
    ]
    # 自動的座標軸範圍不受圖片影響
    main = spec["layer"][1]
    assert "domain" not in (main.get("encoding", {}).get("x", {}).get("scale") or {})


def test_washout_aspect_entire_image_and_flip(tmp_path: Path, db: Path) -> None:
    options = (
        "<image-options desaturate-image='true' desaturate-pct='0.25' "
        "lock-aspect-ratio='false' show-entire-image='true' />"
    )
    # 上方的值（0）比下方（50）小、座標軸沒有反轉：圖片上下顛倒
    flipped = "bottom='50.0' left='0.0' right='2000.0' top='0.0' x='[Sales]' y='[Profit]'"
    ir = parse(tmp_path, mapped(mapping=flipped, options=options), scatter())
    spec = render(ir, db).spec
    (layer,) = layers(spec)
    assert (layer["mark"]["opacity"], layer["mark"]["aspect"]) == (0.75, False)
    assert spec["usermeta"]["background_images"][0]["flip_y"] is True
    main = spec["layer"][1]["encoding"]
    assert main["x"]["scale"]["domain"] == {"unionWith": [0.0, 2000.0]}
    assert main["y"]["scale"]["domain"] == {"unionWith": [0.0, 50.0]}


def test_conditions_and_disabled_images(tmp_path: Path, db: Path) -> None:
    """條件：檢視中有這個值時才顯示（欄位不在檢視中時不顯示）；工作表停用的圖片不畫。"""

    def condition(region: str, field: str = "[Region]") -> str:
        return (
            "<groupfilter function='crossjoin'>"
            f"<groupfilter function='member' level='{field}' member='&quot;{region}&quot;' />"
            "</groupfilter>"
        )

    images = (
        mapped("MappedImage1", "East", condition=condition("East"))
        + mapped("MappedImage2", "Nowhere", condition=condition("Atlantis"))
        + mapped("MappedImage3", "Category", condition=condition("Furniture", "[Category]"))
    )
    ir = parse(tmp_path, images, scatter())
    spec = render(ir, db).spec
    assert [layer["data"]["values"][0] for layer in layers(spec)] == [
        {"__bg_image0": "Image/floor.png", "__bg_url0": None}
    ]
    assert ir.datasources[0].background_images[2].conditions == [
        ImageCondition.model_validate({"field": {"field": "f_category"}, "value": "Furniture"})
    ]
    off = parse(tmp_path, mapped(), scatter(style=disabled()))
    assert layers(render(off, db).spec) == []


def test_views_not_drawn_yet_are_noted(tmp_path: Path, db: Path) -> None:
    """同一架上有多個座標軸（或分格）的檢視尚未畫出背景圖片：寫進附註；沒有 X、Y 欄位的檢視不畫。"""
    two = f"({ref('sum:Profit:qk')} + {ref('sum:Profit:qk')})"
    ir = parse(tmp_path, mapped(), scatter(rows=two))
    rendered = render(ir, db)
    assert layers(rendered.spec) == []
    assert rendered.notes == ["背景圖片「Floor」：分格或同一架上有多個座標軸的檢視尚未畫出"]
    bars = worksheet("Bars", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    other = render(parse(tmp_path, mapped(), bars), db)
    assert layers(other.spec) == [] and other.notes == []


def test_background_image_references_are_checked(tmp_path: Path) -> None:
    ir = parse(tmp_path, mapped(), scatter())
    broken = ir.model_copy(deep=True)
    broken.datasources[0].background_images[0].x = "f_missing"
    broken.worksheets[0].background_images_off = ["bg_missing"]
    assert check_references(broken) == [
        "datasources[ds_orders].background_images[bg_floor].x：欄位「f_missing」不在資料源「ds_orders」中",
        "worksheets[ws_scatter].background_images_off：背景圖片「bg_missing」不在資料源中",
    ]
