"""畫圖的附註裡稱呼東西的方式：與使用者在畫面上看到的相同，不寫內部的代號。

附註（RenderResult.notes、/render 回應的 notes）會顯示給作者與看報表的人：寫「參考線 refline0」
「預測（sum(f_profit)）」沒有人看得懂。這裡的名稱與編輯器畫面上的寫法相同：

- 欄位：膠囊上的寫法——SUM(Profit)、YEAR(Order Date)，快速 Table Calculation 的後面有「△」，預測結果
  不是實際值與預測值時寫在後面（SUM(Profit)（趨勢））（web/src/editor/model.ts 的 pillLabel）；
  Measure Names、Measure Values 與產生的經緯度是 Tableau 的名稱
- 標記類型：標記卡的選單（web/src/editor/model.ts 的 MARK_TYPES）
- 背景地圖的樣式：地圖選項（web/src/editor/Editor.tsx 的 MAP_STYLES）
- 參考線：「分析」分頁清單上的寫法——「平均：SUM(Sales)」「分布（百分位數）：SUM(Sales)」「盒鬚圖：SUM(Sales)」
  （web/src/editor/Editor.tsx 的 AnalyticsPane）
- 註解：註解對話框清單上的文字（欄位與參數寫成 <名稱>，web/src/editor/RichText.tsx 的 plainText）
- 預測模型的趨勢與季節：預測選項（web/src/editor/Dialogs.tsx 的 COMPONENTS；見 tmig.planner.forecast）

只依賴 IR 的模型，查詢（tmig.sqlgen）與畫圖（tmig.render）都可以用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tmig.ir.models import (
    GEOMETRY_GENERATED,
    LATITUDE_GENERATED,
    LONGITUDE_GENERATED,
    MEASURE_NAMES,
    MEASURE_VALUES,
    Annotation,
    DataField,
    FieldUse,
    Parameter,
    ReferenceLine,
    TableCalc,
)
from tmig.render.datasource import use_caption

SPECIAL_NAMES = {
    MEASURE_NAMES: "Measure Names",
    MEASURE_VALUES: "Measure Values",
    LATITUDE_GENERATED: "Latitude (generated)",
    LONGITUDE_GENERATED: "Longitude (generated)",
    GEOMETRY_GENERATED: "Geometry (generated)",
}
"""不在資料源裡的欄位（Tableau 產生的）在資料窗格與膠囊上的名稱。"""
MARK_NAMES = {
    "automatic": "自動",
    "bar": "長條",
    "line": "線",
    "area": "區域",
    "square": "方形",
    "circle": "圓形",
    "shape": "形狀",
    "text": "文字",
    "pie": "圓餅",
    "gantt": "甘特條",
    "polygon": "多邊形",
    "map": "地圖",
    "density": "密度",
}
"""標記類型在標記卡選單上的名稱。"""
MAP_STYLE_NAMES = {
    "light": "淺色",
    "normal": "一般",
    "dark": "深色",
    "streets": "街道",
    "outdoors": "戶外",
    "satellite": "衛星",
}
"""背景地圖的樣式在地圖選項中的名稱。"""
REFERENCE_NAMES = {
    "average": "平均",
    "median": "中位數",
    "sum": "總和",
    "total": "總計",
    "min": "最小值",
    "max": "最大值",
    "constant": "常數",
    "percentile": "百分位數",
    "stdev": "標準差",
    "quantiles": "分位數",
    "confidence": "信賴區間",
    "median_confidence": "中位數信賴區間",
}
"""參考線的計算在「分析」分頁清單上的名稱。"""
NAME_LIMIT = 30
"""附註裡引用的文字（註解的內容）最多幾個字，超過時截短加上「…」。"""
FORECAST_RESULT_NAMES = {
    "trend": "趨勢",
    "precision": "精度",
    "precision_percent": "精度 %",
    "quality": "品質",
    "upper": "上層預測區間",
    "lower": "較低預測區間",
    "none": "無",
}
"""預測結果在膠囊選單「預測結果」中的名稱（Tableau 繁體中文版的寫法）；膠囊上寫在量值的後面，例如
SUM(Sales)（趨勢）。"""


def pill_name(
    use: FieldUse, fields: Mapping[str, DataField], calcs: Sequence[TableCalc] = ()
) -> str:
    """欄位在膠囊上的寫法：SUM(Profit)、YEAR(Order Date)、Category；快速 Table Calculation（calcs 是工作表的
    Table Calculation）的後面有「△」。"""
    if use.field in SPECIAL_NAMES:
        return SPECIAL_NAMES[use.field]
    name = use_caption(use, fields)
    if use.table_calc is not None:
        calc = next((c for c in calcs if c.id == use.table_calc), None)
        if calc is not None and calc.type != "formula":
            name += " △"
    if use.forecast is not None and use.forecast != "none":
        name += f"（{FORECAST_RESULT_NAMES[use.forecast]}）"
    return name


def mark_name(mark: str) -> str:
    """標記類型在選單上的名稱（不認得的照原樣）。"""
    return MARK_NAMES.get(mark, mark)


def reference_name(
    line: ReferenceLine, fields: Mapping[str, DataField], calcs: Sequence[TableCalc] = ()
) -> str:
    """參考線、參考區間、分布與盒鬚圖在「分析」分頁清單上的寫法：計算與它所在座標軸的量值。"""
    if line.kind == "boxplot":
        what = "盒鬚圖"
    elif line.kind == "distribution":
        what = f"分布（{REFERENCE_NAMES[line.formula]}）"
    else:
        what = REFERENCE_NAMES[line.formula] + ("（區間）" if line.paired else "")
    return f"{what}：{pill_name(line.axis, fields, calcs)}"


def annotation_name(
    annotation: Annotation,
    index: int,
    fields: Mapping[str, DataField],
    parameters: Sequence[Parameter] = (),
    calcs: Sequence[TableCalc] = (),
) -> str:
    """註解在附註裡的稱呼：「註解「看這裡」」——它的文字（欄位與參數寫成 <名稱>，與註解對話框的清單相同；
    換行寫成空白，太長時截短）；沒有文字時是「第 n 個註解」（index 從 0 起算，清單上的順序）。"""
    captions = {p.id: p.caption for p in parameters}
    parts: list[str] = []
    for run in annotation.text:
        if run.text is not None:
            parts.append(run.text)
        elif run.use is not None:
            parts.append(f"<{pill_name(run.use, fields, calcs)}>")
        elif run.parameter is not None:
            parts.append(f"<{captions.get(run.parameter, run.parameter)}>")
    text = " ".join("".join(parts).split())
    if not text:
        return f"第 {index + 1} 個註解"
    if len(text) > NAME_LIMIT:
        text = text[:NAME_LIMIT].rstrip() + "…"
    return f"註解「{text}」"


__all__ = [
    "FORECAST_RESULT_NAMES",
    "MAP_STYLE_NAMES",
    "MARK_NAMES",
    "REFERENCE_NAMES",
    "SPECIAL_NAMES",
    "annotation_name",
    "mark_name",
    "pill_name",
    "reference_name",
]
