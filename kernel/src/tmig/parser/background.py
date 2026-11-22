"""背景圖片（地圖＞背景圖片）：資料源的 <mapped-images>，與工作表停用的圖片。

寫法取自 GitHub 上公開的 .twb／.twbx（Hong Kong MTR、ZoneMaps、Meeting Room Utilisation Analysis、
Hexbin Trellis Chart 等，見 README「已知限制與待驗證」）：

    <mapped-images>
      <mapped-image caption='Ground Floor' expression='file:Image/floor.png' name='[MappedImage1]'>
        <mapping bottom='0.0' left='0.0' right='2481.0' top='1754.0' x='[x]' y='[y]' />
        <image-options desaturate-image='true' desaturate-pct='0.73' lock-aspect-ratio='false'
                       show-entire-image='true' />
        <groupfilter function='crossjoin'>
          <groupfilter function='member' level='[Floor]' member='"Ground"' />
        </groupfilter>
      </mapped-image>
    </mapped-images>

.twbx 中 expression 是 ``file:Image/<檔名>``（圖片放在 .twbx 的 Image/）；.twb 是電腦上的路徑
（``file:///C:/…``），報表中沒有這個檔案時略過（圖片畫不出來，不影響數字）。image-options 沒有寫的是
Tableau 的預設：鎖定外觀比例、不一律顯示整張圖片、不淡化。工作表停用的圖片寫在工作表的
``<style-rule element='mapped-image'><format attr='enabled' id='[資料源].[MappedImage1]' value='false' />``；
沒有寫的工作表都啟用。認不得的寫法列為不支援。
"""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote

from lxml import etree

from tmig.ir.models import (
    LATITUDE_GENERATED,
    LONGITUDE_GENERATED,
    BackgroundImage,
    FieldUse,
    ImageCondition,
)
from tmig.parser.context import DsState, ParseContext
from tmig.parser.fields import use_from_name, value_type
from tmig.parser.ids import strip_brackets
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import child, children, local_name, xpath
from tmig.parser.values import LiteralError, parse_literal

IMAGE_FOLDER = "Image"
"""Tableau 把背景圖片放在 .twbx 的 Image/（與 dashboard 的圖片物件相同）。"""
IMAGE_ATTRS = frozenset({"caption", "expression", "name"})
MAPPING_ATTRS = frozenset({"bottom", "left", "right", "top", "x", "y"})
OPTION_ATTRS = frozenset(
    {"desaturate-image", "desaturate-pct", "lock-aspect-ratio", "show-entire-image"}
)


class ImageError(ValueError):
    pass


def background_images(
    ctx: ParseContext, state: DsState, element: etree._Element | None
) -> list[BackgroundImage]:
    """<mapped-images> → 背景圖片；認不得的圖片列為不支援，檔案不在報表中的略過。"""
    images: list[BackgroundImage] = []
    for item in children(element):
        if local_name(item) != "mapped-image":
            ctx.flag(item, f"背景圖片中的未知元素 <{local_name(item)}>")
            continue
        caption = item.get("caption") or strip_brackets(item.get("name", ""))
        try:
            image = _image(ctx, state, item, caption)
        except (ImageError, LiteralError) as exc:
            ctx.flag(item, f"背景圖片「{caption}」：{exc}")
            continue
        if image is not None:
            images.append(image)
    return images


def _image(
    ctx: ParseContext, state: DsState, item: etree._Element, caption: str
) -> BackgroundImage | None:
    unknown = sorted({str(k) for k in item.attrib} - IMAGE_ATTRS)
    if unknown:
        raise ImageError(f"屬性 {'、'.join(unknown)} 不支援")
    path = _packaged(ctx, item.get("expression", ""))
    if path is None:
        ctx.ignore(
            item,
            f"背景圖片「{caption}」的檔案（{item.get('expression')}）不在報表中：未畫出"
            "（Tableau 另存成 .twbx 時把圖片放進報表的 Image/）",
        )
        return None
    mapping = child(item, "mapping")
    if mapping is None or set(mapping.attrib) != MAPPING_ATTRS:
        raise ImageError("座標的對應（mapping）結構不明")
    options = child(item, "image-options")
    conditions: list[ImageCondition] = []
    for part in children(item):
        tag = local_name(part)
        if tag == "groupfilter":
            conditions += _conditions(state, part)
        elif tag not in ("mapping", "image-options"):
            raise ImageError(f"未知的元素 <{tag}>")
    attrs = dict(options.attrib) if options is not None else {}
    unknown = sorted({str(k) for k in attrs} - OPTION_ATTRS)
    if unknown:
        raise ImageError(f"選項 {'、'.join(unknown)} 不支援")
    washout = 0.0
    if attrs.get("desaturate-image") == "true":
        try:
            washout = min(max(float(attrs.get("desaturate-pct", "0")), 0.0), 1.0)
        except ValueError as exc:
            raise ImageError(f"淡化的比例 {attrs.get('desaturate-pct')} 無法辨識") from exc
    x, y = _axis_field(state, mapping.get("x", "")), _axis_field(state, mapping.get("y", ""))
    try:
        edges = {k: float(mapping.get(k, "")) for k in ("left", "right", "top", "bottom")}
    except ValueError as exc:
        raise ImageError("座標的範圍不是數字") from exc
    name = item.get("name", "")
    image_id = ctx.ids.make("bg", caption, f"{state.name}{name}")
    state.image_names[name] = image_id
    return BackgroundImage(
        id=image_id,
        name=caption,
        image=path,
        x=x,
        y=y,
        washout=washout,
        lock_aspect=attrs.get("lock-aspect-ratio") != "false",
        show_entire=attrs.get("show-entire-image") == "true",
        conditions=conditions,
        source_ref=xpath(item),
        **edges,
    )


def _packaged(ctx: ParseContext, expression: str) -> str | None:
    """圖片在報表中的路徑：file:Image/x.png（.twbx 內）；電腦上的路徑時，.twbx 的 Image/ 中有同名的檔案
    才用它。網址與報表中沒有的檔案為 None。"""
    names = {f.name.replace("\\", "/") for f in ctx.package.files}
    if not expression.startswith("file:"):
        return None
    raw = unquote(expression[len("file:") :])
    if not raw.startswith("/"):
        relative = raw.replace("\\", "/")
        return relative if relative in names else None
    candidate = f"{IMAGE_FOLDER}/{PurePosixPath(raw.replace(chr(92), '/')).name}"
    return candidate if candidate in names else None


MAP_AXES = {
    "[Latitude (generated)]": LATITUDE_GENERATED,
    "[Longitude (generated)]": LONGITUDE_GENERATED,
}


def _axis_field(state: DsState, name: str) -> str:
    """mapping 的 x、y：資料源中的欄位（名稱，例如 [X]；地圖的 [Longitude (generated)] 也算）。"""
    if name in MAP_AXES:
        return MAP_AXES[name]
    found = state.by_name.get(name)
    if found is not None:
        return found.id
    resolved = use_from_name(state, name)
    if isinstance(resolved, str):
        raise ImageError(f"座標的欄位 {name}：{resolved}")
    return resolved.field


def _conditions(state: DsState, group: etree._Element) -> list[ImageCondition]:
    """「只在下列條件成立時顯示圖片」：crossjoin 之下各欄位的 member（都要成立）。"""
    parts = [group] if group.get("function") == "member" else children(group, "groupfilter")
    if group.get("function") not in ("member", "crossjoin") or not parts:
        raise ImageError(f"顯示的條件（function={group.get('function')}）不支援")
    found: list[ImageCondition] = []
    for part in parts:
        if part.get("function") != "member":
            raise ImageError(f"顯示的條件（function={part.get('function')}）不支援")
        level = part.get("level", "")
        data_field = state.by_name.get(level)
        use: FieldUse | str = (
            FieldUse(field=data_field.id) if data_field is not None else use_from_name(state, level)
        )
        if isinstance(use, str):
            raise ImageError(f"條件的欄位 {level}：{use}")
        field = state.field_by_id(use.field)
        found.append(
            ImageCondition(
                field=use, value=parse_literal(part.get("member"), value_type(field, use))
            )
        )
    return found


def disabled_images(style: etree._Element | None, ds: DsState) -> list[str]:
    """工作表停用的背景圖片（BackgroundImage.id）：<style-rule element='mapped-image'> 中
    <format attr='enabled' id='[資料源].[MappedImage1]' value='false'>。"""
    by_name = ds.image_names
    off: list[str] = []
    for rule in children(style, "style-rule"):
        if rule.get("element") != "mapped-image":
            continue
        for fmt in children(rule, "format"):
            parts = qualified_parts(fmt.get("id", ""))
            if fmt.get("attr") != "enabled" or len(parts) != 2 or parts[0] != ds.name:
                continue
            found = by_name.get(f"[{parts[1]}]")
            if found is not None and fmt.get("value") == "false" and found not in off:
                off.append(found)
    return off


__all__ = ["background_images", "disabled_images"]
