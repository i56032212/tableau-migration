"""頁面的顯示歷程（頁面控制項的「顯示歷程」）：工作表 <table> 之下的 <page-trail-options> → Worksheet.page_history。

寫法取自 GitHub 上公開的報表（約 200 份有 <page-trail-options> 的 .twb，見 README「已知限制與待驗證」）：

    <page-trail-options enabled='true' lines='true' mark-type='all' marks='false' size='3' />

- enabled='true'：勾選「顯示歷程」；沒有寫時是關（只留下設定）
- mark-type：顯示歷程的標記（Tableau 2021.2 說明文件 buildmanual_shelves.htm：選取的、醒目提示的、手動指定的或
  全部的標記）。平台只有「全部」（all）；其他的（沒有寫時推定是預設的「選取的標記」）不顯示歷程，記為略過
- lines='true' 畫軌跡、marks='false' 不畫之前各頁的標記：顯示標記、軌跡或兩者
- size：「長度」（顯示之前幾頁；沒有寫時是全部）——屬性與選項的對應是推定的
- 標記與軌跡的格式（mark-color、line-color、line-pattern、line-stroke-width、trail-effect、start／end 等）
  平台沒有，記為略過（以預設的淡化呈現）
"""

from __future__ import annotations

from lxml import etree

from tmig.ir.models import PageHistory
from tmig.parser.context import ParseContext

KNOWN = frozenset({"enabled", "mark-type", "lines", "marks", "size"})
FORMATS = {
    "mark-color": "標記的色彩",
    "mark-color-auto": "標記的色彩",
    "line-color": "軌跡的色彩",
    "line-color-auto": "軌跡的色彩",
    "line-pattern": "軌跡的線條樣式",
    "line-stroke-width": "軌跡的線寬",
    "trail-effect": "軌跡的效果",
    "start": "淡化",
    "end": "淡化",
}
"""平台沒有的格式（屬性 → 顯示歷程對話框中的名稱；start、end 的意義推定是淡化的範圍）。"""


def page_history(
    ctx: ParseContext, element: etree._Element | None, sheet: str, paged: bool
) -> PageHistory | None:
    """顯示歷程的設定；沒有勾選、沒有頁面架或平台不能照做時為 None。"""
    if element is None or element.get("enabled") != "true":
        return None
    if not paged:
        ctx.ignore(element, f"工作表「{sheet}」沒有頁面架，顯示歷程的設定不影響檢視：略過")
        return None
    if element.get("mark-type") != "all":
        which = {"highlighted": "醒目提示的", "manual": "手動指定的"}.get(
            element.get("mark-type") or "", "選取的"
        )
        ctx.ignore(
            element,
            f"工作表「{sheet}」只對{which}標記顯示歷程：新平台只能對全部的標記顯示，尚不支援（不顯示歷程）",
        )
        return None
    lines = element.get("lines") == "true"
    marks = element.get("marks") != "false"
    show = "both" if lines and marks else "trails" if lines else "marks"
    size = element.get("size")
    length = int(size) if size is not None and size.isdigit() and int(size) >= 1 else None
    others = sorted(
        {label for name, label in FORMATS.items() if element.get(name) is not None},
        key=list(dict.fromkeys(FORMATS.values())).index,
    )
    unknown = [n for n in element.attrib if str(n) not in KNOWN and str(n) not in FORMATS]
    if others or unknown:
        listed = "、".join([*others, *(str(n) for n in unknown)])
        ctx.ignore(element, f"工作表「{sheet}」顯示歷程的格式（{listed}）尚不支援：以預設呈現")
    return PageHistory(show=show, length=length)  # type: ignore[arg-type]
