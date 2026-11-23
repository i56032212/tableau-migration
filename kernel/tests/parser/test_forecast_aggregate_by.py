"""預測選項指定的「彙總依據」（auto-forecast-agg='false'）：forecast-agg 是估計的時間細度（Forecast.aggregate_by）。

寫法取自 GitHub 上公開的報表（只留下相關的屬性）：
- 檢視是年（tyr:）、指定以月估計：
  https://github.com/Bernardino652/tfm/blob/aa4ff1d6659f37ac1d903283f10859022f272732/GraficosInteractivos/tendenciasypredicciones.twb
  （2024.1.1）
- 檢視是月（tmn:）、指定以月估計（與檢視相同）：
  https://github.com/GyaneshSamanta/DAVM/blob/4e6943c6e6e359d512fc27b1aeeac340088a5bb0/Workbooks/Session%202.twb （2025.2.5）
- 其他看過的值：Year-Trunc、Week-Trunc、Day-Trunc、Hour-Trunc；forecast-agg 只在 auto-forecast-agg='false' 時出現

Tableau 2021.2 說明文件（https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_options.htm ，2026-11-23 查閱）：
「彙總依據」是時間序列的時間細度，通常與檢視相同，也可以比檢視細；整數維度時一律是 Periods。估計的做法見
tests/sqlgen/test_forecast_chosen_granularity.py。
"""

from __future__ import annotations

from pathlib import Path

from tmig.parser.workbook import parse_workbook

from . import builder as b
from .builder import instance, ref, worksheet

SPEC = (
    "<forecast-specification auto-forecast-agg='false' band-confidence-level='95.000000' "
    "enabled='true' fill-type='fill-missing' {agg}ignore-last='1' model-type='auto-season' "
    "range-type='auto' show-prediction-bands='true' />"
)


def _sheet(agg: str) -> str:
    year = instance("[Order Date]", "Year-Trunc", "[tyr:Order Date:qk]", "quantitative")
    sales = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
    value = (
        "<column-instance column='[Sales]' derivation='Sum' forecast-column-base='[sum:Sales:qk]' "
        "forecast-column-type='forecast-value' name='[fVal:sum:Sales:qk]' pivot='key' "
        "type='quantitative' />"
    )
    return worksheet(
        "Yearly Forecast",
        [year, sales, value],
        rows=ref("fVal:sum:Sales:qk"),
        cols=ref("tyr:Order Date:qk"),
        mark="Line",
        table_extra=SPEC.format(agg=agg),
    )


def test_aggregate_by_is_parsed(tmp_path: Path) -> None:
    """forecast-agg='Month-Trunc' → aggregate_by='month'；其餘的設定照常。"""
    ir = parse_workbook(
        b.write_twb(tmp_path, b.workbook(worksheets=_sheet("forecast-agg='Month-Trunc' ")))
    )
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    forecast = ir.worksheets[0].forecast
    assert forecast is not None
    assert (forecast.aggregate_by, forecast.ignore_last, forecast.length) == ("month", 1, "auto")
    for written, unit in (("Year-Trunc", "year"), ("Week-Trunc", "week"), ("Hour-Trunc", "hour")):
        xml = b.workbook(worksheets=_sheet(f"forecast-agg='{written}' "))
        found = parse_workbook(b.write_twb(tmp_path, xml)).worksheets[0].forecast
        assert found is not None and found.aggregate_by == unit


def test_unknown_aggregate_by_is_flagged(tmp_path: Path) -> None:
    """整數維度的 Periods、沒有寫細度：不支援，訊息說明原因。"""
    for agg, shown in (("forecast-agg='Periods' ", "Periods"), ("", "沒有寫")):
        ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=_sheet(agg))))
        reasons = [u.reason for u in ir.unsupported]
        assert any(
            f"工作表「Yearly Forecast」的預測選項指定的「彙總依據」（{shown}）不是年、季、月、週、日或時"
            in r
            for r in reasons
        ), reasons
        assert all(w.forecast is None for w in ir.worksheets)
