"""背景圖片（地圖＞背景圖片，IR 的 BackgroundImage）畫在標記之下：圖片的左、右邊對應 X 座標軸上的值，
上、下邊對應 Y 座標軸上的值（Tableau：「Select the field to map to the x-axis of the image and specify the
left and right values」，https://help.tableau.com/v2021.2/pro/desktop/en-us/bkimages.htm ，2026-11-23 查閱）。

- 工作表的欄架上有 X 欄位、列架上有 Y 欄位（連續，彙總或取消彙總都可以）時顯示；工作表停用的不畫
- 淡化（washout）：圖片的不透明度為 1 − 淡化（推定：在白色的窗格上與淡化成白色相同）
- 鎖定外觀比例：圖片保持寬高比、在對應的範圍內置中（推定）；沒有鎖定時拉伸到整個範圍
- 一律顯示整張圖片：座標軸的自動範圍包含整張圖片（固定的座標軸範圍不變）
- 條件：檢視中條件的欄位有這個值時才顯示（都要成立；檢視中沒有這個欄位時不顯示，推定）
- 對應的值與座標軸的方向相反（例如上方的值比下方小、座標軸又沒有反轉）時圖片上下（左右）顛倒（推定），
  由檢視器載入圖片時翻轉（usermeta.background_images 的 flip_x、flip_y）
- 分格（欄列架上的離散維度把圖分成窗格）：每個窗格各自畫，窗格共用座標軸；條件的欄位是分格的維度時
  只畫在這個值的窗格（推定：說明文件沒有寫分格的情況）
- 同一架上有多個座標軸（多個量值、雙軸）：畫在 X、Y 欄位的座標軸所在的窗格，依那個座標軸對齊（推定）
- 地圖（Latitude／Longitude (generated)，或緯度、經度角色的欄位）：說明文件寫要先把背景地圖關掉
  （「you'll need to disable the built in maps before your background image will display」，地圖＞背景地圖＞
  無）才顯示，見 tmig.render.maps 的 map_layers

圖片的網址由檢視器載入後填進圖層的資料（與自訂的圖片形狀相同，見 web/src/shapeImages.ts）。圖片的位置以
Vega 的 scale('比例尺', 值) 計算（不影響座標軸的自動範圍）；比例尺的名稱依圖在規格中的位置（分格、多個座標軸
時是 Vega-Lite 為各窗格產生的名稱），在規格組好之後由 resolve_scales 填上。
"""

from __future__ import annotations

from typing import Any

from tmig.ir.models import BackgroundImage, Worksheet
from tmig.planner.state import use_key
from tmig.render.colors import value_key

LAYER = "__deco__bg"
"""背景圖片圖層的名稱前綴（裝飾圖層：檢視器的選取與醒目提示略過）。"""
IMAGE = "__bg_image"
URL = "__bg_url"
SCALE = "__bg_scale"
"""比例尺名稱的佔位：__bg_scale:x:欄名（見 resolve_scales）。"""


def shown(image: BackgroundImage, channels: dict[str, Any], values: list[dict[str, Any]]) -> bool:
    """條件都成立：檢視中條件的欄位有這個值。"""
    for condition in image.conditions:
        ch = channels.get(use_key(condition.field))
        if ch is None:
            return False
        wanted = value_key(condition.value)
        if not any(value_key(record.get(ch.name)) == wanted for record in values):
            return False
    return True


def _reversed(ws: Worksheet, field: str, shelf: str) -> bool:
    return any(a.reverse for a in ws.axes if a.use.field == field and a.shelf == shelf)


def wanted_images(ws: Worksheet, images: list[BackgroundImage]) -> list[BackgroundImage]:
    """這張工作表要畫的背景圖片：欄架上有 X 欄位、列架上有 Y 欄位（連續），工作表沒有停用。"""
    xs = [u for u in ws.columns if u.continuous]
    ys = [u for u in ws.rows if u.continuous]
    return [
        image
        for image in images
        if image.id not in ws.background_images_off
        and any(u.field == image.x for u in xs)
        and any(u.field == image.y for u in ys)
    ]


def image_meta(
    n: int, image: BackgroundImage, ws: Worksheet, map_axes: bool = False
) -> dict[str, Any]:
    """檢視器載入圖片用的欄與翻轉：對應的值與座標軸的方向相反時翻轉（地圖的座標軸不反轉）。"""
    flip_x = (image.left > image.right) != (not map_axes and _reversed(ws, image.x, "columns"))
    flip_y = (image.top < image.bottom) != (not map_axes and _reversed(ws, image.y, "rows"))
    return {"image": f"{IMAGE}{n}", "url": f"{URL}{n}", "flip_x": flip_x, "flip_y": flip_y}


def _layer(n: int, image: BackgroundImage, x: str, y: str, row: dict[str, Any]) -> dict[str, Any]:
    """一張圖片的圖層：位置以 X、Y 欄位的比例尺換算（比例尺的名稱先寫佔位，見 resolve_scales）。"""
    sx, sy = f"{SCALE}:x:{x}", f"{SCALE}:y:{y}"
    left, right = f"scale('{sx}', {image.left})", f"scale('{sx}', {image.right})"
    top, bottom = f"scale('{sy}', {image.top})", f"scale('{sy}', {image.bottom})"
    mark: dict[str, Any] = {
        "type": "image",
        "aspect": image.lock_aspect,
        "align": "center",
        "baseline": "middle",
        "clip": True,
        "tooltip": False,
        "x": {"expr": f"({left} + {right}) / 2"},
        "y": {"expr": f"({top} + {bottom}) / 2"},
        "width": {"expr": f"abs({right} - {left})"},
        "height": {"expr": f"abs({bottom} - {top})"},
    }
    if image.washout > 0:
        mark["opacity"] = round(1 - image.washout, 4)
    return {
        "name": f"{LAYER}{n}",
        "data": {"values": [{f"{IMAGE}{n}": image.image, f"{URL}{n}": None, **row}]},
        "transform": [{"filter": f"isValid(datum['{URL}{n}'])"}],
        "mark": mark,
        "encoding": {"url": {"field": f"{URL}{n}", "type": "nominal"}},
    }


def _units(node: dict[str, Any]) -> list[dict[str, Any]]:
    """圖中的各個單一圖（有 mark 與 encoding；裝飾與參考線的圖層除外）。"""
    if str(node.get("name", "")).startswith(("__deco", "__ref")):
        return []
    found: list[dict[str, Any]] = []
    if "mark" in node and isinstance(node.get("encoding"), dict):
        found.append(node)
    for child in node.get("layer", []):
        found += _units(child)
    return found


def _quantitative(encoding: dict[str, Any], channel: str, names: set[str]) -> str | None:
    found = encoding.get(channel)
    if (
        isinstance(found, dict)
        and found.get("type") == "quantitative"
        and found.get("field") in names
    ):
        return str(found["field"])
    return None


def _charts(body: dict[str, Any]) -> list[dict[str, Any]]:
    """可以疊上圖片的各張圖：多個座標軸時是 vconcat／hconcat 中的每一張（雙軸是一張），否則是整個圖。"""
    for key in ("vconcat", "hconcat"):
        if key in body:
            return [c for child in body[key] for c in _charts(child)]
    return [body] if ("mark" in body or "layer" in body) else []


def background_body(
    body: dict[str, Any],
    ws: Worksheet,
    images: list[BackgroundImage],
    channels: dict[str, Any],
    values: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """背景圖片畫在 X、Y 欄位的座標軸所在的每張圖的最下層（分格之前；分格時每個窗格各自一份）。
    回傳新的圖與 usermeta.background_images。"""
    wanted = wanted_images(ws, images)
    if not wanted:
        return body, []
    discrete = {
        use_key(u) for u in [*ws.rows, *ws.columns] if not u.continuous and use_key(u) in channels
    }
    meta: list[dict[str, Any]] = []
    plans: list[tuple[int, BackgroundImage, set[str], set[str], dict[str, Any]]] = []
    for n, image in enumerate(wanted):
        if not shown(image, channels, values):
            continue
        xs = {
            channels[use_key(u)].name
            for u in ws.columns
            if u.continuous and u.field == image.x and use_key(u) in channels
        }
        ys = {
            channels[use_key(u)].name
            for u in ws.rows
            if u.continuous and u.field == image.y and use_key(u) in channels
        }
        # 條件的欄位是分格的維度：圖片的資料列帶著這個值，分格時只留在這個值的窗格（見 _small_multiples）
        row = {
            channels[use_key(c.field)].name: c.value
            for c in image.conditions
            if use_key(c.field) in discrete
        }
        plans.append((n, image, xs, ys, row))
    if not plans:
        return body, []
    used: set[int] = set()
    for chart in _charts(body):
        layers: list[dict[str, Any]] = []
        for n, image, xs, ys, row in plans:
            units = _units(chart)
            x = next((f for u in units if (f := _quantitative(u["encoding"], "x", xs))), None)
            y = next((f for u in units if (f := _quantitative(u["encoding"], "y", ys))), None)
            if x is None or y is None:
                continue
            layers.append(_layer(n, image, x, y, row))
            used.add(n)
            if image.show_entire:
                for unit in units:
                    for channel, field, low, high in (
                        ("x", x, image.left, image.right),
                        ("y", y, image.top, image.bottom),
                    ):
                        if _quantitative(unit["encoding"], channel, {field}) is not None:
                            _include(unit["encoding"][channel], low, high)
        if not layers:
            continue
        inner = {
            k: chart.pop(k)
            for k in ("mark", "encoding", "layer", "transform", "resolve")
            if k in chart
        }
        chart["layer"] = [*layers, inner]
    meta = [image_meta(n, image, ws) for n, image, *_ in plans if n in used]
    return body, meta


def _include(encoding: dict[str, Any], low: float, high: float) -> None:
    """一律顯示整張圖片：座標軸的自動範圍包含整張圖片（固定的範圍不變）。"""
    scale = encoding.setdefault("scale", {})
    domain = scale.get("domain")
    if domain is None:
        scale["domain"] = {"unionWith": [min(low, high), max(low, high)]}
    elif isinstance(domain, dict) and isinstance(domain.get("unionWith"), list):
        union = [*domain["unionWith"], low, high]
        domain["unionWith"] = [min(union), max(union)]


# ---------------------------------------------------------------- 比例尺的名稱


def _child_name(child: dict[str, Any], parent: str, kind: str, index: int) -> str:
    """Vega-Lite 給子圖的名稱（沒有自己的 name 時）：最上層的子圖是 concat_0、layer_1，以下接在父圖的
    名稱之後（concat_0_concat_1）。"""
    if isinstance(child.get("name"), str):
        return child["name"]
    return f"{parent}_{kind}_{index}" if parent else f"{kind}_{index}"


def _children(node: dict[str, Any], name: str) -> list[tuple[dict[str, Any], str, str]]:
    found: list[tuple[dict[str, Any], str, str]] = []
    for key, kind in (("layer", "layer"), ("vconcat", "concat"), ("hconcat", "concat")):
        for i, child in enumerate(node.get(key, [])):
            if isinstance(child, dict):
                found.append((child, _child_name(child, name, kind, i), key))
    return found


def _has_field(node: dict[str, Any], channel: str, field: str) -> bool:
    return any(_quantitative(u["encoding"], channel, {field}) for u in _units(node))


def _owner(node: dict[str, Any], name: str, channel: str, field: str) -> str:
    """圖（layer）之內這個欄位的比例尺所在的圖：圖層預設共用比例尺，獨立（雙軸）時在那一層。"""
    if "layer" not in node:
        return name
    independent = node.get("resolve", {}).get("scale", {}).get(channel) == "independent"
    for child, child_name, _ in _children(node, name):
        if str(child.get("name", "")).startswith(LAYER) or not _has_field(child, channel, field):
            continue
        found = _owner(child, child_name, channel, field)
        if independent or found != child_name:
            return found
        return name
    return name


def _shares(node: dict[str, Any], channel: str) -> bool:
    """這一層把子圖的比例尺合併成一個：圖層預設共用；vconcat／hconcat 預設各自獨立。"""
    resolve = node.get("resolve", {}).get("scale", {}).get(channel)
    if "layer" in node:
        return resolve != "independent"
    return resolve == "shared"


def resolve_scales(spec: dict[str, Any]) -> None:
    """規格組好之後填上背景圖片的比例尺名稱（spec 原地修改）：圖片所在的圖中 X、Y 欄位的比例尺，往上經過
    共用比例尺的各層，名稱是擁有它的那一層的名稱加上 _x、_y（最上層是 x、y）。"""

    def visit(node: dict[str, Any], name: str, path: list[tuple[dict[str, Any], str]]) -> None:
        children = _children(node, name)
        for child, _name, _ in children:
            if str(child.get("name", "")).startswith(LAYER):
                _fill(child, node, name, path)
        for child, child_name, _ in children:
            visit(child, child_name, [*path, (node, name)])

    visit(spec, "", [])


def _fill(
    image: dict[str, Any],
    parent: dict[str, Any],
    name: str,
    path: list[tuple[dict[str, Any], str]],
) -> None:
    mark = image.get("mark")
    if not isinstance(mark, dict):
        return
    text = str(mark)
    for channel in ("x", "y"):
        marker = f"{SCALE}:{channel}:"
        start = text.find(marker)
        if start < 0:
            continue
        field = text[start + len(marker) :].split("'", 1)[0]
        owner = _owner(parent, name, channel, field)
        current = name
        for ancestor, ancestor_name in reversed(path):
            if owner != current or not _shares(ancestor, channel):
                break
            owner = current = ancestor_name
        scale = f"{owner}_{channel}" if owner else channel
        for key in ("x", "y", "width", "height"):
            value = mark.get(key)
            if isinstance(value, dict) and isinstance(value.get("expr"), str):
                value["expr"] = value["expr"].replace(f"{marker}{field}", scale)


__all__ = [
    "IMAGE",
    "LAYER",
    "URL",
    "background_body",
    "image_meta",
    "resolve_scales",
    "shown",
    "wanted_images",
]
