"""預測結果的 forecast-column-type：上層、較低預測區間的名稱取自 GitHub 上公開的報表（只留下相關的元素）。

- https://github.com/Payalchak/US-Sales-Profit-Analysis-Tableau/blob/845d948c85834a316eb37ebe07d1cf5a954f541c/US-Sales%20%26%20Profit%20Analysis%20Dashboard.twb
  （2026.2.2，月的 SUM(Sales)）
- https://github.com/Krutika-Deshpande/Visualization/blob/6ebdf8f91e9840f7abf322a066185eb2488a1cdd/Greenhouse%20Gas%20Emissions%20-%20Boston.twb
  （10.5.2）、https://github.com/univizor/explore/blob/066d4667b4b15e1d72258005c56472d35784627e/workbooks/002-when_disk.twb
  （10.1.1，MAX）

寫法是 forecast-column-type='forecast-prediction-interval-upper'／'-lower'，欄位實例的前綴是 fPIU:、fPIL:。
約 1000 份有預測的公開報表中沒有看過趨勢、精度、精度 %、品質的寫法（那些名稱仍是推定的）。
"""

from __future__ import annotations

from pathlib import Path

from tmig.ir.models import FieldUse
from tmig.parser.workbook import parse_workbook

from . import builder as b
from .builder import instance, ref, worksheet

SPEC = (
    "<forecast-specification auto-forecast-agg='true' band-confidence-level='95.000000' "
    "enabled='true' fill-type='fill-missing' ignore-last='1' model-type='auto-season' "
    "range-type='auto' show-prediction-bands='true' />"
)


def _result(kind: str, prefix: str) -> str:
    return (
        "<column-instance column='[Sales]' derivation='Sum' forecast-column-base='[sum:Sales:qk]' "
        f"forecast-column-type='{kind}' name='[{prefix}:sum:Sales:qk]' pivot='key' "
        "type='quantitative' />"
    )


def test_prediction_interval_results_are_parsed(tmp_path: Path) -> None:
    """fPIU:、fPIL: 是同一個量值的上層、較低預測區間（FieldUse.forecast 的 upper、lower）。"""
    deps = [
        instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative"),
        instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
        _result("forecast-prediction-interval-lower", "fPIL"),
        _result("forecast-prediction-interval-upper", "fPIU"),
        _result("forecast-value", "fVal"),
    ]
    tips = "".join(f"<tooltip column='{ref(f'{p}:sum:Sales:qk')}' />" for p in ("fPIU", "fPIL"))
    sheet = worksheet(
        "Monthly Sales trend and Projection",
        deps,
        rows=ref("fVal:sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=tips,
        table_extra=SPEC,
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    sales = FieldUse(field="f_sales", agg="sum", continuous=True)
    assert ws.rows == [sales]
    assert [u.forecast for u in ws.marks.tooltip] == ["upper", "lower"]
    assert ws.forecast is not None
    assert {u.forecast for u in ws.forecast.measures} == {None, "upper", "lower"}
