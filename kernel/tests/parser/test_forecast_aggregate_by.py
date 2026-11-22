"""預測選項指定的「彙總依據」（auto-forecast-agg='false'）：仍列為不支援，訊息說明原因。

Tableau 2021.2 說明文件（2026-11-23 查閱）：
- https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_options.htm ：「彙總依據」是時間序列的時間細度，
  可以比檢視細；忽略的最後幾期是估計的細度，檢視的最後一期可能是實際值與預測值的彙總
- https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_how_it_works.htm ：以較細的細度估計後彙總回檢視，
  只在 SUM 或 COUNT 時可以；這種預測的區間以模擬計算

估計與彙總回去的做法與「自動」改用較細的細度時相同（見 tests/sqlgen/test_forecast_granularity.py），但 .twb
中指定的細度的寫法沒有樣本。
"""

from __future__ import annotations

from pathlib import Path

from tmig.parser.workbook import parse_workbook

from . import builder as b
from .builder import instance, ref, worksheet

SPEC = (
    "<forecast-specification auto-forecast-agg='false' band-confidence-level='95.000000' "
    "enabled='true' fill-type='fill-missing' ignore-last='1' model-type='auto-season' "
    "range-size='3' range-periods='Month-Trunc' range-type='next' show-prediction-bands='true' />"
)


def test_aggregate_by_is_flagged_with_the_reason(tmp_path: Path) -> None:
    month = instance("[Order Date]", "Quarter-Trunc", "[tqr:Order Date:qk]", "quantitative")
    sales = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
    value = (
        "<column-instance column='[Sales]' derivation='Sum' forecast-column-base='[sum:Sales:qk]' "
        "forecast-column-type='forecast-value' name='[fVal:sum:Sales:qk]' pivot='key' "
        "type='quantitative' />"
    )
    sheet = worksheet(
        "Quarterly Forecast",
        [month, sales, value],
        rows=ref("fVal:sum:Sales:qk"),
        cols=ref("tqr:Order Date:qk"),
        mark="Line",
        table_extra=SPEC,
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    reasons = [u.reason for u in ir.unsupported]
    assert any(
        "工作表「Quarterly Forecast」的預測選項指定了「彙總依據」（不是「自動」）" in r
        and "寫法沒有樣本" in r
        for r in reasons
    ), reasons
    assert ir.worksheets == [] or all(w.forecast is None for w in ir.worksheets)
