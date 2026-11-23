"""預測選項指定的「彙總依據」（Forecast.aggregate_by，.twb 的 auto-forecast-agg='false' 與 forecast-agg）。

Tableau 2021.2 說明文件（https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_options.htm ，2026-11-23 查閱）：
「彙總依據」是時間序列的時間細度，通常與檢視相同，也可以比檢視細；忽略的最後幾期是估計的細度，檢視的最後一期
可能成為預測的期。與「自動」不同，不依期數決定：

- 比檢視細（年→季、年→月、季→月、週→日）：一律以指定的細度估計再加總回檢視的期（SUM、COUNT，與「自動」改用
  較細的細度時相同，見 test_forecast_granularity.py）
- 與檢視相同：以檢視的期估計，資料再少也不改用較細的細度
- 其他（比檢視粗、月→週等、SUM／COUNT 以外的彙總、不完整的最後一期）列為不支援

資料與工作表沿用 test_forecast_granularity.py：East、West 兩區每月一筆，銷售有趨勢與 12 個月的季節。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from tmig.planner.analytics import ACTUAL, ESTIMATE, HIGH, LOW
from tmig.planner.forecast import SEASONS, best_fit, forecast

from .test_forecast_granularity import database, parse, rows_until, run, sheet, unsupported


def chosen(xml: str, agg: str) -> str:
    """把工作表的預測選項改成指定的彙總依據（auto-forecast-agg='false' forecast-agg='…'）。"""
    assert "auto-forecast-agg='true'" in xml
    return xml.replace(
        "auto-forecast-agg='true'", f"auto-forecast-agg='false' forecast-agg='{agg}'"
    )


def monthly(rows: list[tuple[Any, ...]], region: str) -> list[float]:
    """一區每個月的銷售（資料每月一筆，依時間排序）。"""
    return [r[4] for r in sorted((r for r in rows if r[0] == region), key=lambda r: r[3])]


def test_years_estimated_by_chosen_quarters(tmp_path: Path) -> None:
    """9 年的資料：「自動」時算不算忽略的期會決定是否改用季（列為不支援）；指定以季估計時一律以季估計再
    加總回年。忽略最後 1 期是 2022 年第 4 季：2022 年是前 3 季的實際值加上第 4 季的估計值。"""
    rows = rows_until(dt.date(2022, 12, 31), first_year=2014)
    db = database(tmp_path / "nine.duckdb", rows)
    records, analytics = run(parse(tmp_path, chosen(sheet("year"), "Quarter-Trunc")), db)
    months = monthly(rows, "East")
    quarters = [sum(months[i : i + 3]) for i in range(0, len(months), 3)]
    found = best_fit(quarters[:35], SEASONS["quarter"], True)
    predicted = forecast(found, 9).values
    east = {r["trunc_year(f_order_date)"].year: r for r in records if r["f_region"] == "East"}
    assert sorted(east) == list(range(2014, 2025))  # 預測 2 年
    assert east[2021]["f_forecast_indicator"] == ACTUAL
    assert east[2021]["sum(f_sales)"] == pytest.approx(sum(quarters[28:32]))
    assert east[2022]["f_forecast_indicator"] == ESTIMATE
    assert east[2022]["sum(f_sales)"] == pytest.approx(sum(quarters[32:35]) + predicted[0])
    assert east[2023]["sum(f_sales)"] == pytest.approx(sum(predicted[1:5]))
    assert east[2024]["sum(f_sales)"] == pytest.approx(sum(predicted[5:9]))
    low, high = east[2023][LOW + "sum(f_sales)"], east[2023][HIGH + "sum(f_sales)"]
    assert low < east[2023]["sum(f_sales)"] < high
    described = analytics.details["forecast"]
    assert (described["unit"], described["view_unit"]) == ("quarter", "year")


def test_years_estimated_by_chosen_months(tmp_path: Path) -> None:
    """年→月（「自動」沒有這個組合）：以月配適，之後每年是 12 個月估計值的合計。"""
    rows = rows_until(dt.date(2022, 12, 31))
    db = database(tmp_path / "four.duckdb", rows)
    records, analytics = run(parse(tmp_path, chosen(sheet("year"), "Month-Trunc")), db)
    months = monthly(rows, "West")
    found = best_fit(months[:47], SEASONS["month"], True)
    predicted = forecast(found, 25).values
    west = {r["trunc_year(f_order_date)"].year: r for r in records if r["f_region"] == "West"}
    assert sorted(west) == list(range(2019, 2025))
    assert west[2022]["sum(f_sales)"] == pytest.approx(sum(months[36:47]) + predicted[0])
    assert west[2023]["sum(f_sales)"] == pytest.approx(sum(predicted[1:13]))
    assert west[2024]["sum(f_sales)"] == pytest.approx(sum(predicted[13:25]))
    assert analytics.details["forecast"]["unit"] == "month"
    assert {m["period"] for m in analytics.details["forecast"]["models"]} == {12}


def test_same_granularity_as_the_view_is_not_refined(tmp_path: Path) -> None:
    """6 季的資料：「自動」時以月估計（少於 9 季），指定以季估計時照檢視的季估計。"""
    db = database(tmp_path / "six.duckdb", rows_until(dt.date(2020, 6, 30)))
    _, analytics = run(parse(tmp_path, sheet("quarter", regions=False)), db)
    assert analytics.details["forecast"]["unit"] == "month"
    records, analytics = run(
        parse(tmp_path, chosen(sheet("quarter", regions=False), "Quarter-Trunc")), db
    )
    assert analytics.details["forecast"]["unit"] == "quarter"
    assert any(r["f_forecast_indicator"] == ESTIMATE for r in records)


def test_undocumented_chosen_cases_are_unsupported(tmp_path: Path) -> None:
    """AVG、月→週、不完整的最後一期：說明文件沒有寫，列為不支援（訊息不含內部的欄名）。"""
    db = database(tmp_path / "four.duckdb", rows_until(dt.date(2022, 12, 31)))
    message = unsupported(parse(tmp_path, chosen(sheet("year", agg="Avg"), "Quarter-Trunc")), db)
    assert "工作表「Sales Forecast」的預測：預測選項指定以季估計（檢視的時間是年）" in message
    assert "量值不是 SUM 或 COUNT" in message
    assert "f_" not in message and "avg(" not in message
    by_month = sheet("quarter").replace("Quarter-Trunc", "Month-Trunc").replace("tqr:", "tmn:")
    message = unsupported(parse(tmp_path, chosen(by_month, "Week-Trunc")), db)
    assert "預測選項指定以週估計（檢視的時間是月）" in message and "這個組合尚不支援" in message
    partial = database(tmp_path / "partial.duckdb", rows_until(dt.date(2022, 6, 30)))
    message = unsupported(parse(tmp_path, chosen(sheet("year"), "Quarter-Trunc")), partial)
    assert "最後一年（2022-01-01 起）的資料只到 2022-04-01" in message
