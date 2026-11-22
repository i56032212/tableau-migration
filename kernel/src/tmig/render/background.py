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

圖片的網址由檢視器載入後填進圖層的資料（與自訂的圖片形狀相同，見 web/src/shapeImages.ts）。地圖（緯度、
經度）、分格（欄列架上有維度）與同一架上有多個座標軸的檢視尚未畫出，寫進附註。
"""

from __future__ import annotations

from typing import Any

from tmig.ir.models import GENERATED_FIELDS, BackgroundImage, Worksheet
from tmig.planner.state import use_key
from tmig.render.colors import value_key

LAYER = "__deco__bg"
"""背景圖片圖層的名稱前綴（裝飾圖層：檢視器的選取與醒目提示略過）。"""
IMAGE = "__bg_image"
URL = "__bg_url"


def _shown(image: BackgroundImage, channels: dict[str, Any], values: list[dict[str, Any]]) -> bool:
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


def background_layers(
    spec: dict[str, Any],
    ws: Worksheet,
    images: list[BackgroundImage],
    channels: dict[str, Any],
    values: list[dict[str, Any]],
    notes: list[str],
) -> None:
    """工作表的背景圖片畫在最下層（spec 原地修改）。"""
    from tmig.render.vegalite import main_encoding

    xs = [u for u in ws.columns if u.continuous]
    ys = [u for u in ws.rows if u.continuous]
    wanted = [
        image
        for image in images
        if image.id not in ws.background_images_off
        and any(u.field == image.x for u in xs)
        and any(u.field == image.y for u in ys)
    ]
    if not wanted:
        return
    names = "、".join(f"「{image.name}」" for image in wanted)
    if any(image.x in GENERATED_FIELDS or image.y in GENERATED_FIELDS for image in wanted):
        notes.append(f"背景圖片{names}在地圖（緯度、經度）上尚未畫出")
        return
    discrete = [u for u in [*ws.rows, *ws.columns] if not u.continuous]
    encoding = main_encoding(spec)
    single = (
        len(xs) == 1
        and len(ys) == 1
        and not discrete
        and encoding is not None
        and not any(k in spec for k in ("facet", "vconcat", "hconcat", "concat"))
        and not any(k in encoding for k in ("row", "column"))
        and all(
            isinstance(encoding.get(c), dict) and encoding[c].get("type") == "quantitative"
            for c in ("x", "y")
        )
    )
    if not single or encoding is None:
        notes.append(f"背景圖片{names}：分格或同一架上有多個座標軸的檢視尚未畫出")
        return
    layers: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    for n, image in enumerate(wanted):
        if not _shown(image, channels, values):
            continue
        flip_x = (image.left > image.right) != _reversed(ws, image.x, "columns")
        flip_y = (image.top < image.bottom) != _reversed(ws, image.y, "rows")
        left, right = f"scale('x', {image.left})", f"scale('x', {image.right})"
        top, bottom = f"scale('y', {image.top})", f"scale('y', {image.bottom})"
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
        layers.append(
            {
                "name": f"{LAYER}{n}",
                "data": {"values": [{f"{IMAGE}{n}": image.image, f"{URL}{n}": None}]},
                "transform": [{"filter": f"isValid(datum['{URL}{n}'])"}],
                "mark": mark,
                "encoding": {"url": {"field": f"{URL}{n}", "type": "nominal"}},
            }
        )
        meta.append(
            {"image": f"{IMAGE}{n}", "url": f"{URL}{n}", "flip_x": flip_x, "flip_y": flip_y}
        )
        if image.show_entire:
            for channel, low, high in (
                ("x", image.left, image.right),
                ("y", image.top, image.bottom),
            ):
                scale = encoding[channel].setdefault("scale", {})
                domain = scale.get("domain")
                if domain is None:
                    scale["domain"] = {"unionWith": [min(low, high), max(low, high)]}
                elif isinstance(domain, dict) and isinstance(domain.get("unionWith"), list):
                    union = [*domain["unionWith"], low, high]
                    domain["unionWith"] = [min(union), max(union)]
    if not layers:
        return
    body = {
        k: spec.pop(k) for k in ("mark", "encoding", "layer", "transform", "resolve") if k in spec
    }
    spec["layer"] = [*layers, body]
    spec["usermeta"]["background_images"] = meta


__all__ = ["background_layers"]
