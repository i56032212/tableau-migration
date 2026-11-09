"""Tableau 的內建色盤（以 .twb 中的內部名稱為鍵）與色彩的取樣、內插。

色盤的顏色由小到大排列：連續色盤的第一個顏色對應最小值；發散色盤的中間顏色對應中心。離散（regular）
色盤依序指定給沒有自訂顏色的值。

顏色值的來源（Tableau 沒有公開色盤定義檔，以下為公開整理的資料，彼此比對過）：

- tableau-10、tableau-20（Tableau 10 以前的預設色盤）與 tableau10_10_0、tableau20_10_0（之後的預設）：
  與 D3、Vega 的 category10、category20、tableau10、tableau20 相同
- 其他 *_10_0 色盤：color-buddy 的 tableau-colors.json（與 ggthemes 的 tableau.yml 比對，兩者的起訖顏色
  相同；不一致的離散色盤 Miller Stone、Summer、Winter 不收錄）。依 BSD 授權保留其著作權聲明：

      Copyright (c) 2024-2025, Andrew McNutt. All rights reserved.
      Redistribution and use in source and binary forms, with or without modification, are
      permitted provided that the following conditions are met: (1) Redistributions of source
      code must retain the above copyright notice, this list of conditions and the following
      disclaimer. (2) Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation and/or other
      materials provided with the distribution. (3) Neither the name of the copyright holder nor
      the names of its contributors may be used to endorse or promote products derived from this
      software without specific prior written permission.
      THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
      OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
      MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
      COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
      EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
      GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
      AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
      NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
      OF THE POSSIBILITY OF SUCH DAMAGE.

- Tableau 10 以前的連續色盤（tableau-red、tableau-red-blue、area-red-green、tableau-map-temperatur）：
  ggthemes 整理的 Classic 色盤（原資料由大到小排列，這裡反轉成由小到大）

中間的顏色是否與 Tableau 完全相同尚未以 Tableau 實機比對；起訖與中心的顏色兩個來源一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PaletteKind = Literal["regular", "sequential", "diverging"]


@dataclass(frozen=True)
class Palette:
    kind: PaletteKind
    colors: tuple[str, ...]


DEFAULT_REGULAR = "tableau10_10_0"
"""離散欄位的預設色盤（Tableau 10 起的「自動」）。"""
AUTOMATIC_SEQUENTIAL = "blue_10_0"
"""連續量值的預設色盤：值都是正數（或都是負數）時。"""
AUTOMATIC_DIVERGING = "orange_blue_diverging_10_0"
"""連續量值的預設色盤：值有正有負時（以 0 為中心）。"""


def _p(kind: PaletteKind, colors: str) -> Palette:
    return Palette(kind, tuple(colors.split()))


PALETTES: dict[str, Palette] = {
    "tableau-10": _p(
        "regular", "#1f77b4 #ff7f0e #2ca02c #d62728 #9467bd #8c564b #e377c2 #7f7f7f #bcbd22 #17becf"
    ),
    "tableau-20": _p(
        "regular",
        "#1f77b4 #aec7e8 #ff7f0e #ffbb78 #2ca02c #98df8a #d62728 #ff9896 #9467bd "
        "#c5b0d5 #8c564b #c49c94 #e377c2 #f7b6d2 #7f7f7f #c7c7c7 #bcbd22 #dbdb8d "
        "#17becf #9edae5",
    ),
    "tableau10_10_0": _p(
        "regular", "#4e79a7 #f28e2b #e15759 #76b7b2 #59a14f #edc948 #b07aa1 #ff9da7 #9c755f #bab0ac"
    ),
    "tableau20_10_0": _p(
        "regular",
        "#4e79a7 #a0cbe8 #f28e2b #ffbe7d #59a14f #8cd17d #b6992d #f1ce63 #499894 "
        "#86bcb6 #e15759 #ff9d9a #79706e #bab0ac #d37295 #fabfd2 #b07aa1 #d4a6c8 "
        "#9d7660 #d7b5a6",
    ),
    "traffic_light_10_0": _p(
        "regular", "#b60a1c #e39802 #309143 #e03531 #f0bd27 #51b364 #ff684c #ffda66 #8ace7e"
    ),
    "color_blind_10_0": _p(
        "regular", "#1170aa #fc7d0b #a3acb9 #57606c #5fa2ce #c85200 #7b848f #a3cce9 #ffbc79 #c8d0d9"
    ),
    "seattle_grays_10_0": _p("regular", "#767f8b #b3b7b8 #5c6068 #d3d3d3 #989ca3"),
    "superfishel_stone_10_0": _p(
        "regular", "#6388b4 #ffae34 #ef6f6a #8cc2ca #55ad89 #c3bc3f #bb7693 #baa094 #a9b5ae #767676"
    ),
    "jewel_bright_10_0": _p(
        "regular", "#eb1e2c #fd6f30 #f9a729 #f9d23c #5fbb68 #64cdcc #91dcea #a4a4d5 #bbc9e5"
    ),
    "nuriel_stone_10_0": _p(
        "regular", "#8175aa #6fb899 #31a1b3 #ccb22b #a39fc9 #94d0c0 #959c9e #027b8e #9f8f12"
    ),
    "blue_red_brown_10_0": _p(
        "regular",
        "#466f9d #91b3d7 #ed444a #feb5a2 #9d7660 #d7b5a6 #3896c4 #a0d4ee #ba7e45 "
        "#e9b87f #c8133b #ea8783",
    ),
    "green_orange_cyan_yellow_10_0": _p(
        "regular",
        "#4e9f50 #87d180 #ef8a0c #fcc66d #3ca8bc #98d9e4 #94a323 #c3ce3d #a08400 "
        "#f7d42a #26897e #8dbfa8",
    ),
    "purple_pink_gray_10_0": _p(
        "regular",
        "#8074a8 #c6c1f0 #c46487 #ffbed1 #9c9290 #c5bfbe #9b93c9 #ddb5d5 #7c7270 "
        "#f498b6 #b173a0 #c799bc",
    ),
    "blue_10_0": _p(
        "sequential",
        "#b9ddf1 #a6d0ea #94c2e1 #83b3d7 #75a6cc #6798c1 #598ab6 #4c7dab #42709e #376491 #2a5783",
    ),
    "orange_10_0": _p(
        "sequential",
        "#ffc685 #fab767 #f5a847 #f59536 #f2822a #ed6f20 #e16220 #d05822 #c04f23 #af4623 #9e3d22",
    ),
    "green_10_0": _p(
        "sequential",
        "#b3e0a6 #99d788 #87cc79 #78bf6c #6ab25f #5ea654 #4e9a51 #408e4d #2f8243 #27763d #24693d",
    ),
    "red_10_0": _p(
        "sequential",
        "#ffbeb2 #feac9d #fc9b87 #fa8971 #f67561 #f26250 #eb4c48 #e03841 #d2283d #c11a3b #ae123a",
    ),
    "purple_10_0": _p(
        "sequential",
        "#eec9e5 #e7bada #dcadcd #d1a0c0 #c792b5 #bc86a9 #b0799f #a06f99 #916491 #865986 #7c4d79",
    ),
    "brown_10_0": _p(
        "sequential",
        "#eedbbd #ecca96 #e9b97a #e4a865 #dc9856 #d18954 #c7784c #c0673f #b85536 #ad4433 #9f3632",
    ),
    "gray_10_0": _p(
        "sequential",
        "#d5d5d5 #c6c8c7 #b6b9b9 #a7acad #979fa1 #889296 #7a858b #6c7880 #606b76 #555f6a #49525e",
    ),
    "gray_warm_10_0": _p(
        "sequential",
        "#dcd4d0 #cec5c1 #c0b8b4 #b3aaa7 #a59c99 #98908c #8b827f #7e7673 #726866 #665c5a #59504e",
    ),
    "teal_10_0": _p(
        "sequential",
        "#bbdfdf #a2d4d5 #8ac9c9 #75bcbb #61b0af #4da5a4 #379998 #2b8b8c #1e7f7f #127273 #006667",
    ),
    "blue_teal_10_0": _p(
        "sequential", "#bce4d8 #9dd3d1 #81c3cb #65b3c2 #45a2b9 #368fae #347da0 #306a93 #2c5985"
    ),
    "orange_gold_10_0": _p(
        "sequential",
        "#f4d166 #f8be5c #f8aa4c #f5983b #f3852a #ef701b #e2621f #d65322 #c54923 #b14223 #9e3a26",
    ),
    "green_gold_10_0": _p(
        "sequential",
        "#f4d166 #d5ca60 #b6c35c #98bb59 #7cb257 #60a656 #4b9c53 #3f8f4f #33834a #257740 #146c36",
    ),
    "red_gold_10_0": _p(
        "sequential",
        "#f4d166 #f6be59 #f9aa51 #fc964e #f6834b #ee734a #e56249 #db5247 #cf4244 #c43141 #b71d3e",
    ),
    "orange_blue_diverging_10_0": _p(
        "diverging",
        "#9e3d22 #ad4523 #bc4d23 #cb5522 #d95e21 #e76820 #ee7725 #f58a30 #fa9e3f "
        "#fdb053 #ffc171 #d9d5c9 #9dcbe4 #8ec2df #80b6d9 #73a8d0 #669bc7 #5c91bf "
        "#5385b2 #487aa9 #3b6ea0 #306495 #2b5c8a",
    ),
    "red_green_diverging_10_0": _p(
        "diverging",
        "#ae123a #bf193b #ce253d #dc3440 #e74545 #ef574c #f46956 #f77c66 #fa8d75 "
        "#fc9c89 #feac9d #ced7c3 #99d788 #88cd7a #7bc26f #6fb663 #64ab59 #57a153 "
        "#499550 #3b8a4a #2d7f42 #27753d #24693d",
    ),
    "green_blue_diverging_10_0": _p(
        "diverging",
        "#24693d #27753d #308143 #3d8c4b #4a974f #58a153 #66ad5a #72b966 #7dc471 "
        "#8cd07d #9cda8c #c9dad2 #a0c9e0 #90bddc #80b1d5 #73a6cc #689ac3 #5e8fb9 "
        "#5283af #4878a5 #406c99 #35628e #2a5783",
    ),
    "red_blue_diverging_10_0": _p(
        "diverging",
        "#a90c38 #bb163a #ca223c #d9303f #e44144 #ee534b #f36755 #f77a64 #fa8b75 "
        "#fd9b87 #ffac9b #dfd4d1 #a9cee6 #96c2e1 #88b6d9 #7aabd0 #6d9fc6 #6192bc "
        "#5787b3 #4c7ca9 #42709e #386592 #2e5a87",
    ),
    "red_black_10_0": _p(
        "diverging",
        "#ae123a #bf193b #cf253d #dc3340 #e74445 #ef574c #f46a57 #f77b66 #fb8d75 "
        "#fc9d89 #feac9d #d9d9d9 #c6c8c7 #b8bbba #aaafaf #9ca3a5 #8e979a #818b90 "
        "#748087 #68747d #5e6974 #545e69 #49525e",
    ),
    "gold_purple_diverging_10_0": _p(
        "diverging",
        "#ad9024 #b49629 #bb9d32 #c1a33b #c8ab43 #cfb250 #d4b95e #dbc06c #dfc689 "
        "#e3d8cf #deb7cb #d9aec8 #d4a3c3 #ce9bbe #c892b8 #c189b0 #ba82a9 #b37aa1 "
        "#ac7299",
    ),
    "red_green_gold_diverging_10_0": _p(
        "diverging",
        "#be2a3e #c7383f #d24743 #de5748 #e56349 #ec714a #f37e4b #f88a4d #fa994e "
        "#fba952 #fcbb58 #f4d166 #cec85e #b6c35a #9fbe5f #89b761 #72b065 #5fa866 "
        "#4f9f63 #459559 #398a50 #2e7f47 #22763f",
    ),
    "sunrise_sunset_diverging_10_0": _p(
        "diverging",
        "#33608c #526598 #7567a2 #9768a5 #b669a1 #d16c98 #e7718a #f67c7c #ff8974 "
        "#f6ba57 #ee9b47 #ee8a43 #ed7846 #e96749 #df5a47 #d54c45 #cb3f42 #c12e3f "
        "#b81840",
    ),
    "orange_blue_white_diverging_10_0": _p(
        "diverging",
        "#9e3d22 #b24723 #c55123 #d85d21 #e96b21 #f17e29 #f79236 #fba749 #febb65 "
        "#ffcd93 #ffdfc0 #ffffff #d2ebfa #bbddf1 #a5cfe7 #8dc1df #7cb2d6 #6da3cc "
        "#5f94c1 #5386b3 #4577a7 #366a9b #2b5c8a",
    ),
    "red_green_white_diverging_10_0": _p(
        "diverging",
        "#ae123a #c41e3c #d8303f #e84646 #f16051 #f77963 #fa9079 #fda695 #ffbab0 "
        "#fed1cc #fbe6e5 #ffffff #daefd9 #bce5b7 #a1da93 #89cd7b #79bf6c #6ab15e "
        "#59a354 #489450 #378747 #29783e #24693d",
    ),
    "green_blue_white_diverging_10_0": _p(
        "diverging",
        "#24693d #29783e #368646 #46934e #59a153 #69b05d #77be6b #88cd79 #9dda8e "
        "#b8e5b1 #d5efd4 #ffffff #cee8f3 #b8daea #a3cbe1 #8ebcdb #7cadd2 #6c9ec6 "
        "#5e8fb9 #5081ad #4472a0 #386591 #2a5783",
    ),
    "red_blue_white_diverging_10_0": _p(
        "diverging",
        "#a90c38 #c01a3b #d52c3e #e54245 #f05c4f #f67660 #fb8d78 #fea391 #ffbaae "
        "#ffd0cb #fbe6e5 #ffffff #d2ebfa #bdddf0 #a8cde6 #93c0df #81b1d5 #70a2c8 "
        "#6192bc #5484b1 #4776a3 #3a6795 #2e5a87",
    ),
    "red_black_white_diverging_10_0": _p(
        "diverging",
        "#ae123a #c51d3c #d9303f #e84646 #f16051 #f77964 #fb8f78 #fda694 #ffbab0 "
        "#fed1cc #fbe6e5 #ffffff #ebebeb #dadada #c9caca #b7bbba #a6acac #949c9e "
        "#838d91 #727e86 #646f79 #56606b #49525e",
    ),
    "tableau-red": _p(
        "sequential", "#eac0bd #f89a90 #f57667 #e35745 #d8392c #cf1719 #c21417 #b10c1d #9c0824"
    ),
    "tableau-red-blue": _p(
        "diverging",
        "#9c0824 #bd1316 #d11719 #df513f #fc8375 #cacaca #67add4 #3a87b7 #1c73b1 #1c5998 #26456e",
    ),
    "area-red-green": _p(
        "diverging",
        "#bd1100 #c82912 #d23a21 #dc4930 #e6583e #ef654d #f7705b #fd7e6b #fe8e7e "
        "#fca294 #e9dabe #c7e298 #b1de7f #a0d571 #90cb68 #82c162 #75b65d #69aa56 "
        "#5ea049 #559633 #4a8c1c",
    ),
    "tableau-map-temperatur": _p(
        "diverging", "#529985 #6c9e6e #99b059 #dbcf47 #ebc24b #e3a14f #c26b51"
    ),
}


def palette(name: str | None) -> Palette | None:
    return PALETTES.get(name) if name else None


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")[:6]
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def interpolate(colors: tuple[str, ...] | list[str], t: float) -> str:
    """色盤上位置 t（0 到 1）的顏色：相鄰兩個顏色之間依 RGB 線性內插。"""
    if len(colors) == 1:
        return colors[0]
    t = max(0.0, min(1.0, t))
    position = t * (len(colors) - 1)
    index = min(int(position), len(colors) - 2)
    fraction = position - index
    low, high = _rgb(colors[index]), _rgb(colors[index + 1])
    return _hex(tuple(a + (b - a) * fraction for a, b in zip(low, high, strict=True)))  # type: ignore[arg-type]


def sample(colors: tuple[str, ...] | list[str], count: int) -> list[str]:
    """從連續色盤平均取 count 個顏色（含兩端）。"""
    if count <= 1:
        return [interpolate(colors, 0.5 if count == 1 else 0.0)][:count]
    return [interpolate(colors, i / (count - 1)) for i in range(count)]


__all__ = [
    "AUTOMATIC_DIVERGING",
    "AUTOMATIC_SEQUENTIAL",
    "DEFAULT_REGULAR",
    "PALETTES",
    "Palette",
    "PaletteKind",
    "interpolate",
    "palette",
    "sample",
]
