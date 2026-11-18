"""自訂的圖片形狀（Tableau 的自訂形狀調色盤）與標記卡上固定的形狀。

寫法取自公開報表（LogShark 的 Config、TabadminController，Asia Cup Analysis、Stock Market Analysis）：
圖片存在活頁簿的 <external><shapes>，固定的形狀在窗格樣式的 format attr='shape'，各成員的形狀在形狀編碼的
<map to='調色盤/檔名'>。測試資料見 tests/sqlgen/test_query.py。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tmig.ir import IR, MarkShape, ShapeAssignment
from tmig.package import image_paths, read_package, read_report_file, shape_images, write_package
from tmig.parser.workbook import parse_workbook
from tmig.shapes import embedded_shapes, palettes, shape_path

from ..parser import builder as b
from ..parser.builder import ref, worksheet
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, render

__all__ = ["db"]  # pytest fixture

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
"""1×1 的 PNG。"""


def embedded(*names: str) -> str:
    """活頁簿內嵌的圖片形狀（base64 分行、前後有空白，與 Tableau 寫出的相同）。"""
    text = base64.b64encode(PNG).decode()
    body = "".join(
        f"<shape name='{name}'>\n        {text[:40]}\n        {text[40:]}\n      </shape>"
        for name in names
    )
    return f"<external><shapes>{body}</shapes></external>"


def mark_rule(*formats: str, encoding: str = "") -> str:
    return f"<style><style-rule element='mark'>{''.join(formats)}{encoding}</style-rule></style>"


def scatter(
    pane_extra: str = "", encodings: str = "", mark: str = "Shape", sheet_style: str = ""
) -> str:
    sheet = worksheet(
        "Shapes",
        [REGION, CATEGORY, SALES, PROFIT],
        rows=ref("sum:Sales:qk"),
        cols=ref("sum:Profit:qk"),
        mark=mark,
        encodings=f"<lod column='{ref('none:Region:nk')}' />{encodings}",
        pane_extra=pane_extra,
    )
    return sheet.replace("<style />", sheet_style, 1) if sheet_style else sheet


def parse(tmp_path: Path, sheet: str, shapes: str = "") -> IR:
    xml = b.workbook(b.extract_datasource(), worksheets=sheet, extra=shapes)
    return parse_workbook(b.write_twb(tmp_path, xml))


def layers(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    point = next(layer for layer in spec["layer"] if layer["mark"]["type"] == "point")
    image = next(layer for layer in spec["layer"] if layer["mark"]["type"] == "image")
    return point, image


def test_shape_names_and_image_paths() -> None:
    assert shape_path("My Shapes/check_box.png") == "Shapes/My Shapes/check_box.png"
    assert shape_path(":custom/Batting.JPG") == "Shapes/custom/Batting.JPG"
    assert shape_path("logo.png") == "Shapes/logo.png"
    # 內建的形狀、不是圖片、會跳出資料夾的名稱
    for name in ("circle", ":filled/circle", "Arrows/up", "../secret.png", "a/../b.png", ""):
        assert shape_path(name) is None
    assert MarkShape(image="Shapes/KPI/up.png").shape == "circle"
    for bad in ("KPI/up.png", "Shapes/../up.png", "Shapes/KPI/up.exe", "Shapes\\KPI\\up.png"):
        with pytest.raises(ValidationError):
            MarkShape(image=bad)


def test_fixed_image_shape(tmp_path: Path, db: Path) -> None:
    """標記卡上固定的圖片形狀：點看不見（仍可以停留、點選），圖片畫在同一個位置，邊長是 30 像素乘上標記卡的大小（點的外框與圖片一樣大，整張圖都點得到）。"""
    style = mark_rule(
        "<format attr='shape' value='My Shapes/check_box.png' />",
        "<format attr='size' value='2' />",
    )
    ir = parse(tmp_path, scatter(style), embedded("My Shapes/check_box.png"))
    assert ir.unsupported == []
    marks = ir.worksheets[0].marks
    assert marks.mark_shape == MarkShape(image="Shapes/My Shapes/check_box.png")
    assert image_paths(ir) == shape_images(ir) == ["Shapes/My Shapes/check_box.png"]
    spec = render(ir, db).spec
    point, image = layers(spec)
    assert spec["usermeta"]["shape_images"] == [
        {"image": "__shape_img0", "tint": "__shape_tint0", "url": "__shape_url0"}
    ]
    hidden = {"condition": {"test": "isValid(datum['__shape_img0'])", "value": 0}, "value": 1}
    assert point["encoding"] == {"fillOpacity": hidden, "strokeOpacity": hidden}
    assert image["name"] == "__shape_images0"
    assert image["transform"] == [{"filter": "isValid(datum['__shape_url0'])"}]
    assert image["encoding"] == {"url": {"field": "__shape_url0", "type": "nominal"}}
    side = point["mark"]["size"] ** 0.5
    assert (image["mark"]["width"], image["mark"]["height"]) == (side, side) and side == 60
    assert image["mark"]["aspect"] is True
    values = spec["data"]["values"]
    assert {v["__shape_img0"] for v in values} == {"Shapes/My Shapes/check_box.png"}
    assert {v["__shape_tint0"] for v in values} == {None}  # 沒有設定顏色：圖片原本的顏色
    assert all("__shape_url0" not in v for v in values)  # 由檢視器載入圖片後填入
    assert "shape_legend" not in spec["usermeta"]


def test_fixed_builtin_shape_and_unknown_image(tmp_path: Path, db: Path) -> None:
    """固定的內建形狀（times、:filled/diamond）；圖片沒有存在活頁簿裡的自訂形狀依預設的形狀呈現。"""
    ir = parse(tmp_path, scatter(mark_rule("<format attr='shape' value='times' />")))
    assert ir.worksheets[0].marks.mark_shape == MarkShape(shape="times")
    spec = render(ir, db).spec
    assert spec["mark"]["type"] == "point" and spec["mark"]["shape"].startswith("M-1,-0.7")
    assert "shape_images" not in spec["usermeta"]

    (tmp_path / "filled").mkdir()
    style = mark_rule("<format attr='shape' value=':filled/diamond' />")
    ir = parse(tmp_path / "filled", scatter(style))
    assert ir.worksheets[0].marks.mark_shape == MarkShape(shape="diamond")
    assert render(ir, db).spec["mark"]["shape"] == "diamond"

    (tmp_path / "missing").mkdir()
    style = mark_rule("<format attr='shape' value='Arrows/up.png' />")
    ir = parse(tmp_path / "missing", scatter(style), embedded("Arrows/down.png"))
    assert ir.worksheets[0].marks.mark_shape is None
    assert any("Arrows/up.png" in item.reason for item in ir.ignored)
    assert "shape" not in render(ir, db).spec["mark"]


def test_automatic_mark_with_a_shape_is_a_shape(tmp_path: Path, db: Path) -> None:
    """兩個量值的散佈圖上「自動」是形狀（與 Tableau 相同）：設定的形狀照樣套用（Asia Cup Analysis）。"""
    style = mark_rule("<format attr='shape' value=':custom/batting.png' />")
    ir = parse(tmp_path, scatter(style, mark="Automatic"), embedded(":custom/batting.png"))
    marks = ir.worksheets[0].marks
    assert marks.type == "automatic" and marks.mark_shape == MarkShape(
        image="Shapes/custom/batting.png"
    )
    rendered = render(ir, db)
    assert rendered.mark == "shape"
    point, image = layers(rendered.spec)
    assert image["mark"]["width"] == point["mark"]["size"] ** 0.5 == 30


def test_image_shapes_by_member_with_color_and_size(tmp_path: Path, db: Path) -> None:
    """形狀上的欄位：指定圖片的成員畫圖片、其餘照預設的形狀；色彩上有欄位時圖片套用各標記的顏色；
    大小上有欄位時圖片跟著大小的比例尺；圖例由檢視器畫（Vega 的圖例不能放圖片）。"""
    region = ref("none:Region:nk")
    shape_map = (
        f"<encoding attr='shape' field='{region}' type='shape'>"
        "<map to='KPI/up.png'><bucket>&quot;East&quot;</bucket></map>"
        "<map to='KPI/down.png'><bucket>&quot;West&quot;</bucket></map>"
        "<map to='plus'><bucket>&quot;South&quot;</bucket></map>"
        "<map to='KPI/gone.png'><bucket>&quot;North&quot;</bucket></map>"
        "</encoding>"
    )
    sheet = scatter(
        encodings=(
            f"<shape column='{region}' /><color column='{region}' />"
            f"<size column='{ref('sum:Sales:qk')}' />"
        ),
        sheet_style=mark_rule(encoding=shape_map),
    )
    ir = parse(tmp_path, sheet, embedded("KPI/up.png", "KPI/down.png"))
    assert ir.unsupported == []
    marks = ir.worksheets[0].marks
    assert [(a.value, a.shape, a.image) for a in marks.shape_assignments] == [
        ("East", "circle", "Shapes/KPI/up.png"),
        ("West", "circle", "Shapes/KPI/down.png"),
        ("South", "plus", None),
    ]
    assert any("KPI/gone.png" in item.reason for item in ir.ignored)  # 圖片沒有存在活頁簿裡
    spec = render(ir, db).spec
    point, image = layers(spec)
    encoding = spec["encoding"]
    assert encoding["shape"]["legend"] is None
    assert spec["usermeta"]["shape_legend"]["title"] == "Region"
    entries = spec["usermeta"]["shape_legend"]["entries"]
    assert [e["label"] for e in entries] == ["East", "North", "South", "West"]
    assert (entries[0]["label"], entries[0]["image"]) == ("East", "Shapes/KPI/up.png")
    assert entries[1]["path"].startswith("M-0.9")  # 沒有指定：預設調色盤的第二個（方形）
    assert entries[2]["path"].startswith("M-0.3,-1")  # 加號
    assert (entries[3]["label"], entries[3]["image"]) == ("West", "Shapes/KPI/down.png")
    name = encoding["shape"]["field"]
    scale = encoding["color"]["scale"]
    colors = dict(zip(scale["domain"], scale["range"], strict=True))
    # 色彩與形狀是同一個欄位：合併的圖例，各成員的形狀帶著它的顏色
    assert [e["color"] for e in entries] == [colors[e["label"]] for e in entries]
    for value in spec["data"]["values"]:
        member = value[name]
        expected = {"East": "Shapes/KPI/up.png", "West": "Shapes/KPI/down.png"}.get(member)
        assert value["__shape_img0"] == expected
        assert value["__shape_tint0"] == (colors[member] if expected else None)
    size_field = encoding["size"]["field"]
    assert image["mark"]["width"] == {"expr": f"sqrt(scale('size', datum[\"{size_field}\"]))"}
    assert "size" not in point["mark"]


def test_mark_color_tints_the_image(tmp_path: Path, db: Path) -> None:
    """標記卡設定了顏色（色彩上沒有欄位）：圖片套用這個顏色。"""
    style = mark_rule(
        "<format attr='shape' value='KPI/up.png' />",
        "<format attr='mark-color' value='#e15759' />",
    )
    ir = parse(tmp_path, scatter(style), embedded("KPI/up.png"))
    spec = render(ir, db).spec
    assert {v["__shape_tint0"] for v in spec["data"]["values"]} == {"#e15759"}


def test_shape_files_of_a_report(tmp_path: Path) -> None:
    """圖片的來源：報表目錄中 .twb／.twbx 內嵌的圖片與 files/Shapes/；存成報表套件時一併帶著。"""
    style = mark_rule("<format attr='shape' value='KPI/up.png' />")
    report = tmp_path / "report"
    report.mkdir()
    ir = parse(report, scatter(style), embedded("KPI/up.png", ":custom/x.png", "bad name.txt"))
    assert sorted(embedded_shapes(report)) == ["Shapes/KPI/up.png", "Shapes/custom/x.png"]
    assert read_report_file(report, "Shapes/KPI/up.png") == PNG
    assert read_report_file(report, "Shapes/KPI/none.png") is None
    assert read_report_file(report, "Shapes/../report.twb") is None
    # 編輯器加入的圖片在 files/Shapes/ 下：優先於內嵌的圖片
    added = report / "files" / "Shapes" / "Mine"
    added.mkdir(parents=True)
    (added / "star.png").write_bytes(PNG + b"1")
    (report / "files" / "Shapes" / "KPI").mkdir()
    (report / "files" / "Shapes" / "KPI" / "up.png").write_bytes(PNG + b"2")
    (added / "notes.txt").write_text("x", encoding="utf-8")
    assert palettes(report) == {
        "custom": ["Shapes/custom/x.png"],
        "KPI": ["Shapes/KPI/up.png"],
        "Mine": ["Shapes/Mine/star.png"],
    }
    assert read_report_file(report, "Shapes/KPI/up.png") == PNG + b"2"
    # 存成報表套件再開啟：用到的圖片形狀（固定的形狀、各成員的形狀）在 files/ 下
    sheet = ir.worksheets[0]
    assignment = ShapeAssignment(value="East", image="Shapes/Mine/star.png")
    marks = sheet.marks.model_copy(update={"shape_assignments": [assignment]})
    ir = ir.model_copy(update={"worksheets": [sheet.model_copy(update={"marks": marks})]})
    assert image_paths(ir) == ["Shapes/KPI/up.png", "Shapes/Mine/star.png"]
    (report / "data").mkdir()
    for ds in ir.datasources:
        (report / "data" / f"{ds.id}.duckdb").write_bytes(b"")
    info = write_package(tmp_path / "report.tmigx", ir, report)
    assert info.files == ["Shapes/KPI/up.png", "Shapes/Mine/star.png"] and not info.missing_files
    opened = tmp_path / "opened"
    opened.mkdir()
    read_package(tmp_path / "report.tmigx", opened)
    assert read_report_file(opened, "Shapes/KPI/up.png") == PNG + b"2"
    assert read_report_file(opened, "Shapes/Mine/star.png") == PNG + b"1"
    assert palettes(opened) == {"KPI": ["Shapes/KPI/up.png"], "Mine": ["Shapes/Mine/star.png"]}


def button(pane_extra: str, encodings: str = "") -> str:
    """欄列架上都沒有欄位的形狀標記（蓋在 dashboard 的圖片上當按鈕用的工作表）。"""
    return worksheet(
        "Button",
        [REGION, SALES],
        mark="Shape",
        encodings=f"<lod column='{ref('none:Region:nk')}' />{encodings}",
        pane_extra=pane_extra,
    )


def test_image_shape_without_shelves_is_drawn_in_the_middle(tmp_path: Path, db: Path) -> None:
    """欄列架上都沒有欄位、也沒有標籤的形狀標記：畫出圖片（在檢視的中間），不是文字的「Abc」；圖片的
    大小相對於儲存格（這裡是整個檢視）較短的一邊——大小 s 時為 s / 2、最多填滿，所以大小 4 的透明圖片
    填滿檢視；看不見的點與圖片一樣大（點選的範圍）。有標籤時照舊以文字呈現標籤。"""
    style = mark_rule(
        "<format attr='shape' value='Clear/blank.png' />", "<format attr='size' value='4' />"
    )
    ir = parse(tmp_path, button(style), embedded("Clear/blank.png"))
    assert ir.unsupported == []
    spec = render(ir, db).spec
    point, image = layers(spec)
    assert "text" not in json.dumps(spec["layer"][0].get("mark"))
    side = "min(width, height) * 1.0"
    assert image["mark"]["width"] == image["mark"]["height"] == {"expr": side}
    assert point["mark"]["size"] == {"expr": f"pow({side}, 2)"}
    # 沒有欄、列：圖片與點一樣在檢視的中間
    assert image["encoding"]["x"] == {"value": {"expr": "width / 2"}}
    assert image["encoding"]["y"] == {"value": {"expr": "height / 2"}}
    assert "x" not in point.get("encoding", {}) and "x" not in spec.get("encoding", {})

    # 有標籤：以文字呈現（各段文字的格式在文字標記上才完整），不畫成點
    labeled = button(style, encodings=f"<text column='{ref('sum:Sales:qk')}' />")
    spec = render(parse(tmp_path, labeled, embedded("Clear/blank.png")), db).spec
    marks = json.dumps(spec)
    assert '"type": "text"' in marks and '"type": "image"' not in marks


def test_multiline_label_above_an_image_shape(tmp_path: Path, db: Path) -> None:
    """圖片形狀上方的多行標籤（自訂的標籤文字中有換行）：整段往上移，最後一行在圖片的上緣之上；
    偏移依圖片的大小（相對於儲存格：欄上有維度時是每一格的寬與檢視的高較短的一邊）。"""
    label = (
        "<customized-label><formatted-text><run fontsize='5'> </run><run>\n</run>"
        f"<run fontsize='15'>&lt;{ref('sum:Sales:qk')}&gt;</run></formatted-text></customized-label>"
    )
    style = (
        "<style><style-rule element='cell'><format attr='text-align' value='center' />"
        "<format attr='vertical-align' value='top' /></style-rule>"
        "<style-rule element='mark'><format attr='mark-labels-show' value='true' />"
        "<format attr='shape' value='Clear/blank.png' /><format attr='size' value='2' />"
        "</style-rule></style>"
    )
    sheet = worksheet(
        "Icons",
        [REGION, SALES],
        cols=ref("none:Region:nk"),
        mark="Shape",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=label + style,
    )
    ir = parse(tmp_path, sheet, embedded("Clear/blank.png"))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    marks = ir.worksheets[0].marks
    assert (marks.cell_align, marks.label_valign) == ("center", "top")
    spec = render(ir, db).spec
    text = next(
        layer for layer in spec["layer"] if str(layer.get("name", "")).startswith("__label_")
    )
    font = text["mark"]["fontSize"]
    # 圖片的半徑 + 間隔 4，再往上一行（Vega 的行高是字級 + 2）；大小 2 的圖片填滿儲存格較短的一邊
    side = "min(bandwidth('x'), height) * 1.0"
    assert text["mark"]["baseline"] == "bottom" and text["mark"]["align"] == "center"
    assert text["mark"]["dy"] == {"expr": f"-1 * ((({side}) / 2) + {4 + font + 2})"}
    image = next(layer for layer in spec["layer"] if layer["mark"]["type"] == "image")
    assert image["mark"]["width"] == {"expr": side}
    # 只有欄的標題：圖片在這一格的中間（沒有列）
    assert image["encoding"]["y"] == {"value": {"expr": "height / 2"}}
    assert "x" not in image["encoding"]
