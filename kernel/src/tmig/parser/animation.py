"""標記的動畫（格式＞動畫）：活頁簿預設（Workbook.animation）與各工作表的設定（Worksheet.animation）。

.twb 中的寫法取自 GitHub 上公開的報表（2021.2、2022.2、2022.4、2024.2、2025.1、2025.2 存的檔案，見 README
「已知限制與待驗證」的「標記的動畫」）：

    <style>                                    （活頁簿預設在 /workbook/style，工作表在 <table><style>）
      <_.fcp.MarkAnimation.true...style-rule element='animation'>
        <format attr='animation-on' value='ao-off' />            ao-on＝開、ao-off＝關
        <format attr='animation-duration' value='0.5' />         秒
        <format attr='animation-style' value='as-phased' />      as-synced＝同時、as-phased＝依序
        <_.fcp.AnimationOnByDefault.false...format attr='animation-on' value='ao-on' />
      </_.fcp.MarkAnimation.true...style-rule>
    </style>

- 工作表上沒有寫的項目沿用活頁簿的預設（IR 中是 None）
- 活頁簿預設的開關：有生效的 animation-on 就照它；沒有時，document-format-change-manifest 有
  ``AnimationOnByDefault``（不帶前置，或 ``_.fcp.AnimationOnByDefault.true...``）的檔案是開（2025.2 的公開報表只寫了
  這個旗標、沒有 style-rule），沒有這個旗標的舊檔案是關
- ``_.fcp.AnimationOnByDefault.false...`` 的元素是給舊版讀的替代內容（與開的結果相同），照解析器的慣例略過
  （tmig.parser.twbx 的 children 不列出功能旗標為 false 的元素）
- 不認得的值記為略過（動畫不影響數字）
"""

from __future__ import annotations

import math
from typing import Literal

from lxml import etree

from tmig.ir.models import Animation
from tmig.parser.context import ParseContext
from tmig.parser.twbx import child, children

SWITCHES = {"ao-on": True, "ao-off": False}
STYLES: dict[str, Literal["simultaneous", "sequential"]] = {
    "as-synced": "simultaneous",
    "as-phased": "sequential",
}
MAX_SECONDS = 10.0
"""Tableau 的自訂持續時間最多 10 秒（formatting_animations.htm）。"""


def animation_rule(ctx: ParseContext, style: etree._Element | None) -> Animation | None:
    """<style> 中 element='animation' 的設定；沒有任何設定時為 None。"""
    on: bool | None = None
    duration: float | None = None
    kind: Literal["simultaneous", "sequential"] | None = None
    found = False
    for rule in children(style, "style-rule"):
        if rule.get("element") != "animation":
            continue
        for item in children(rule, "format"):
            name, value = item.get("attr") or "", item.get("value") or ""
            if name == "animation-on" and value in SWITCHES:
                on, found = SWITCHES[value], True
            elif name == "animation-duration" and (seconds := _seconds(value)) is not None:
                duration, found = seconds, True
            elif name == "animation-style" and value in STYLES:
                kind, found = STYLES[value], True
            else:
                ctx.ignore(item, f"動畫（格式＞動畫）的設定 {name}＝{value} 不認得：略過")
    if not found:
        return None
    return Animation(on=on, duration=duration, style=kind)


def workbook_animation(ctx: ParseContext, root: etree._Element) -> Animation | None:
    """活頁簿預設的動畫：style-rule 的設定，開關沒有寫時依 manifest 的 AnimationOnByDefault。只寫了「關」的
    設定與沒有設定相同（Workbook.animation 的 None 為關），記為 None。"""
    rule = animation_rule(ctx, child(root, "style"))
    if rule == Animation(on=False):
        return None
    if rule is not None and rule.on is not None:
        return rule
    manifest = child(root, "document-format-change-manifest")
    on_by_default = child(manifest, "AnimationOnByDefault") is not None
    if not on_by_default:
        return rule
    if rule is None:
        return Animation(on=True)
    return rule.model_copy(update={"on": True})


def _seconds(value: str) -> float | None:
    """持續時間（秒）：Tableau 以單精度存（0.30000001192092896），取到千分之一秒；超過 10 秒時為 10 秒。"""
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return round(min(seconds, MAX_SECONDS), 3)
