"""字型：Tableau 的字型名稱 → CSS 的字型與粗細、pt → 像素，以及 Tableau 的預設字型。

預設值依 Tableau 的畫面量測（Logshark 報表文件中 1 倍大小的截圖）：標題成員、刻度、座標軸標題與欄位標籤
都是 9pt（12 像素：含上下伸部的字高 11 像素），工作表標題 15pt（20 像素）；標題成員與刻度為灰色
（#555555），座標軸標題、欄位標籤與工作表標題較深（#333333），欄位標籤為半粗體。

Tableau 的字型（Tableau Book、Tableau Semibold 等）是 Tableau 隨附的字型，裝了 Tableau Desktop 的電腦上有；
沒有時依序改用相近的字型（Benton Sans、Segoe UI、Helvetica Neue、Arial）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tmig.ir.models import FontStyle

DEFAULT_FAMILY = "Tableau Book"
DEFAULT_SIZE = 9.0
"""工作表文字的預設字級（pt）。"""
TITLE_SIZE = 15.0
"""工作表標題的預設字級（pt）。"""
LABEL_COLOR = "#555555"
"""標題成員與座標軸刻度的預設顏色。"""
TITLE_COLOR = "#333333"
"""座標軸標題、欄位標籤與工作表標題的預設顏色。"""
SEMIBOLD = 600
BOLD = 700
TABLEAU_WEIGHTS = {
    "Tableau Light": 300,
    "Tableau Book": 400,
    "Tableau Regular": 400,
    "Tableau Medium": 500,
    "Tableau Semibold": SEMIBOLD,
    "Tableau Bold": BOLD,
}
"""Tableau 隨附的字型：每個粗細是一個字型名稱。"""
SUFFIX_WEIGHTS = {
    "light": 300,
    "medium": 500,
    "semibold": SEMIBOLD,
    "demibold": SEMIBOLD,
    "bold": BOLD,
    "black": 900,
}
"""其他字型名稱結尾的粗細（例如 Segoe UI Semibold）。"""
FALLBACK = '"Benton Sans", "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif'


@dataclass(frozen=True)
class Font:
    """解析後的字型：CSS 的 font-family、字級（像素）、粗細與是否斜體。"""

    family: str
    size: float
    weight: int
    italic: bool = False

    def mark(self) -> dict[str, Any]:
        """Vega-Lite 文字標記的屬性。"""
        props: dict[str, Any] = {"font": self.family, "fontSize": self.size}
        if self.weight != 400:
            props["fontWeight"] = self.weight
        if self.italic:
            props["fontStyle"] = "italic"
        return props

    def axis(self, prefix: str) -> dict[str, Any]:
        """座標軸、圖例的屬性（prefix 為 label 或 title）。"""
        props: dict[str, Any] = {
            f"{prefix}Font": self.family,
            f"{prefix}FontSize": self.size,
            f"{prefix}FontWeight": self.weight,
        }
        if self.italic:
            props[f"{prefix}FontStyle"] = "italic"
        return props

    def css(self) -> dict[str, Any]:
        """檢視器（CSS）用的字型。"""
        return {
            "family": self.family,
            "size": self.size,
            "weight": self.weight,
            "italic": self.italic,
        }


def pixels(points: float) -> float:
    """pt → 像素（96 DPI）。"""
    return round(points * 4 / 3, 2)


def css_family(name: str) -> tuple[str, int]:
    """字型名稱 → CSS 的 font-family 與這個字型的粗細。"""
    quoted = '"' + name.replace('"', "") + '"'
    if name in TABLEAU_WEIGHTS:
        return f"{quoted}, {FALLBACK}", TABLEAU_WEIGHTS[name]
    words = name.split()
    weight = SUFFIX_WEIGHTS.get(words[-1].lower(), 400) if len(words) > 1 else 400
    if weight != 400:
        base = '"' + " ".join(words[:-1]).replace('"', "") + '"'
        return f"{quoted}, {base}, sans-serif", weight
    return f"{quoted}, sans-serif", 400


def resolve(
    *layers: FontStyle | None, size: float = DEFAULT_SIZE, weight: int | None = None
) -> Font:
    """依序套用各層的字型設定（後面的覆寫前面的）；size、weight 為這個元素的預設。"""
    family = DEFAULT_FAMILY
    points = size
    bold: bool | None = None
    italic = False
    for layer in layers:
        if layer is None:
            continue
        if layer.family:
            family = layer.family
        if layer.size:
            points = layer.size
        if layer.bold is not None:
            bold = layer.bold
        if layer.italic is not None:
            italic = layer.italic
    stack, own = css_family(family)
    if bold is True:
        resolved = max(own, BOLD)
    elif bold is False or weight is None:
        resolved = own
    else:
        resolved = max(own, weight)
    return Font(stack, pixels(points), resolved, italic)


__all__ = [
    "DEFAULT_SIZE",
    "LABEL_COLOR",
    "SEMIBOLD",
    "TITLE_COLOR",
    "TITLE_SIZE",
    "Font",
    "css_family",
    "pixels",
    "resolve",
]
