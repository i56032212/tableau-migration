"""預測選「自動」時以較細的時間估計（Tableau 2021.2 說明文件「How Forecasting Works in Tableau」的 Getting More
Data，https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_how_it_works.htm ，2026-11-23 查閱）。

- 檢視中少於 9 年的資料時以季估計、彙總回年；少於 9 季時以月；少於 9 週時以日（還有日→時、時→分、分→秒）
- 只在量值是 SUM 或 COUNT 時可以；其他的彙總照樣以檢視的細度估計（test_years_are_estimated_by_quarters 的最後）
- 忽略的最後幾期是估計的細度（forecast_options.htm）：檢視的最後一期成為實際值與預測值的彙總
- 文件沒有寫明的（怎麼數期數、不完整的最後一期、以季估計仍少於 9 季時是否改以月、日→時等）列為不支援

資料：East、West 兩區，2019-01 到 2022-12 每月一筆（每區 48 個月、16 季、4 年），銷售有趨勢與季節。
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute_query
from tmig.data.live import execute_live
from tmig.data.sources import DuckDBExecutor
from tmig.ir import IR, LiveConnection, TableRelation
from tmig.parser.workbook import parse_workbook
from tmig.planner.analytics import ACTUAL, ESTIMATE, HIGH, LOW, Analytics, ForecastPlan, apply
from tmig.planner.forecast import SEASONS, best_fit, forecast
from tmig.sqlgen.query import QueryError, worksheet_sql

from ..engines import as_live
from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet


def month_start(year: int, month: int) -> dt.date:
    return dt.date(year, month, 1)


def sales(region: str, t: int) -> float:
    """第 t 個月（0 起算）的銷售：趨勢加上 12 個月的季節。"""
    base = 100.0 if region == "East" else 60.0
    return round(base + 3 * t + 15 * math.sin(2 * math.pi * t / 12), 2)


def rows_until(last: dt.date, first_year: int = 2019) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = []
    t = 0
    day = dt.date(first_year, 1, 15)
    while day <= last:
        for region in ("East", "West"):
            found.append((region, "Furniture", f"O{t}{region}", day, sales(region, t), 1.0, 1))
        t += 1
        day = dt.date(day.year + day.month // 12, day.month % 12 + 1, 15)
    return found


def database(path: Path, rows: list[tuple[Any, ...]]) -> Path:
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Category" VARCHAR, '
            '"Order ID" VARCHAR, "Order Date" DATE, "Sales" DOUBLE, "Profit" DOUBLE, '
            '"Quantity" BIGINT)'
        )
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
    return path


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return database(
        tmp_path_factory.mktemp("fc") / "orders.duckdb", rows_until(dt.date(2022, 12, 31))
    )


SPEC = (
    "<forecast-specification auto-forecast-agg='true' band-confidence-level='95.000000' "
    "enabled='true' fill-type='fill-missing' ignore-last='{ignore}' model-type='auto-season' "
    "range-size='2' range-periods='{periods}' range-type='next' show-prediction-bands='true' />"
)
INDICATOR = (
    "<column-instance column='[Forecast Indicator]' derivation='None' "
    "forecast-column-base='[Forecast Indicator]' forecast-column-type='forecast-indicator' "
    "name='[none:Forecast Indicator:nk]' pivot='key' type='nominal' />"
)
DERIVATIONS = {
    "year": ("Year-Trunc", "tyr"),
    "quarter": ("Quarter-Trunc", "tqr"),
    "week": ("Week-Trunc", "twk"),
    "day": ("Day-Trunc", "tdy"),
}


def sheet(
    unit: str,
    agg: str = "Sum",
    ignore: int = 1,
    regions: bool = True,
    view_extra: str = "",
) -> str:
    derivation, prefix = DERIVATIONS[unit]
    short = {"Sum": "sum", "Avg": "avg", "Count": "cnt"}[agg]
    time = f"{prefix}:Order Date:qk"
    measure = f"{short}:Sales:qk"
    deps = [
        instance("[Order Date]", derivation, f"[{time}]", "quantitative"),
        instance("[Sales]", agg, f"[{measure}]", "quantitative"),
        instance("[Region]", "None", "[none:Region:nk]"),
        f"<column-instance column='[Sales]' derivation='{agg}' forecast-column-base='[{measure}]' "
        f"forecast-column-type='forecast-value' name='[fVal:{measure}]' pivot='key' "
        "type='quantitative' />",
        INDICATOR,
    ]
    colors = f"<color column='{ref('none:Forecast Indicator:nk')}' />"
    detail = f"<lod column='{ref('none:Region:nk')}' />" if regions else ""
    periods = {"year": "Year-Trunc", "quarter": "Quarter-Trunc"}.get(unit, "Month-Trunc")
    return worksheet(
        "Sales Forecast",
        deps,
        rows=ref(f"fVal:{measure}"),
        cols=ref(time),
        mark="Line",
        encodings=colors + detail,
        view_extra=view_extra,
        table_extra=SPEC.format(ignore=ignore, periods=periods),
    )


def parse(tmp_path: Path, xml_sheet: str) -> IR:
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=xml_sheet)))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


def run(ir: IR, db: Path) -> tuple[list[dict[str, Any]], Any]:
    query = worksheet_sql(ir, ir.worksheets[0].id)
    result = execute_query(db, query)
    return result.records(), query.analytics


def quarterly(region: str, last: dt.date) -> dict[dt.date, float]:
    """各季的銷售合計（直接由資料計算）。"""
    found: dict[dt.date, float] = {}
    for r in rows_until(last):
        if r[0] != region:
            continue
        day: dt.date = r[3]
        key = month_start(day.year, (day.month - 1) // 3 * 3 + 1)
        found[key] = found.get(key, 0.0) + r[4]
    return found


# ---------------------------------------------------------------- 年→季


def test_years_are_estimated_by_quarters(tmp_path: Path, db: Path) -> None:
    """4 年的資料（少於 9 年）、SUM(Sales)：以季估計再加總回年。忽略最後 1 期是 2022 年第 4 季：2022 年成為
    估計的期，是第 1–3 季的實際值加上第 4 季的估計值；之後 2 年各是 4 季估計值的合計。"""
    ir = parse(tmp_path, sheet("year"))
    records, analytics = run(ir, db)
    east = [r for r in records if r["f_region"] == "East"]
    by_year = {r["trunc_year(f_order_date)"].year: r for r in east}
    assert sorted(by_year) == [2019, 2020, 2021, 2022, 2023, 2024]
    assert [by_year[y]["f_forecast_indicator"] for y in (2019, 2020, 2021)] == [ACTUAL] * 3
    assert [by_year[y]["f_forecast_indicator"] for y in (2022, 2023, 2024)] == [ESTIMATE] * 3
    # 直接以季的資料計算：前 15 季配適，估計第 16 季與之後的 8 季
    quarters = quarterly("East", dt.date(2022, 12, 31))
    values = [quarters[k] for k in sorted(quarters)]
    found = best_fit(values[:15], SEASONS["quarter"], True)
    predicted = forecast(found, 9).values
    expected = {
        2022: sum(values[12:15]) + predicted[0],
        2023: sum(predicted[1:5]),
        2024: sum(predicted[5:9]),
    }
    for year, value in expected.items():
        assert by_year[year]["sum(f_sales)"] == pytest.approx(value)
        low, high = by_year[year][LOW + "sum(f_sales)"], by_year[year][HIGH + "sum(f_sales)"]
        assert low < value < high
    # 實際的年不變
    assert by_year[2019]["sum(f_sales)"] == pytest.approx(sum(values[:4]))
    # 描述預測：實際使用的細度是季
    described = analytics.details["forecast"]
    assert (described["unit"], described["view_unit"]) == ("quarter", "year")
    assert {m["period"] for m in described["models"]} == {4}
    # AVG(Sales)：Tableau 只能為 SUM、COUNT 取得較細的資料，照樣以年估計
    records, analytics = run(parse(tmp_path, sheet("year", agg="Avg")), db)
    assert analytics.details["forecast"]["unit"] == "year"
    assert any(r["f_forecast_indicator"] == ESTIMATE for r in records)


def test_quarters_are_estimated_by_months(tmp_path: Path) -> None:
    """6 季的資料（少於 9 季）：以月估計再加總回季；忽略最後 1 期是最後一個月。"""
    db = database(tmp_path / "six.duckdb", rows_until(dt.date(2020, 6, 30)))
    ir = parse(tmp_path, sheet("quarter", regions=False))
    records, analytics = run(ir, db)
    by_quarter = {r["trunc_quarter(f_order_date)"]: r for r in records}
    months: list[float] = [
        sum(r[4] for r in rows_until(dt.date(2020, 6, 30)) if r[3].month == m and r[3].year == y)
        for y in (2019, 2020)
        for m in range(1, 13)
        if (y, m) <= (2020, 6)
    ]
    found = best_fit(months[:17], SEASONS["month"], True)
    predicted = forecast(found, 7).values
    last = by_quarter[dt.date(2020, 4, 1)]
    assert last["f_forecast_indicator"] == ESTIMATE
    assert last["sum(f_sales)"] == pytest.approx(sum(months[15:17]) + predicted[0])
    assert by_quarter[dt.date(2020, 7, 1)]["sum(f_sales)"] == pytest.approx(sum(predicted[1:4]))
    assert by_quarter[dt.date(2020, 10, 1)]["sum(f_sales)"] == pytest.approx(sum(predicted[4:7]))
    assert analytics.details["forecast"]["unit"] == "month"


def test_live_connection_runs_the_finer_query(tmp_path: Path, db: Path) -> None:
    """Live 連線（兩段式執行）：較細的查詢同樣在來源資料庫執行，結果與萃取相同。"""
    ir = parse(tmp_path, sheet("year"))
    expected, _ = run(ir, db)
    live = as_live(ir, "duckdb")
    ds = live.datasources[0]
    connection = ds.connection
    assert isinstance(connection, LiveConnection) and isinstance(connection.relation, TableRelation)
    relation = connection.relation.model_copy(update={"table": ["Extract", "Extract"]})
    live = live.model_copy(
        update={
            "datasources": [
                ds.model_copy(
                    update={"connection": connection.model_copy(update={"relation": relation})}
                )
            ]
        }
    )
    executor = DuckDBExecutor(db)
    query = worksheet_sql(live, live.worksheets[0].id)
    try:
        found = execute_live(query, executor).records()
    finally:
        executor.close()
    key = "sum(f_sales)"
    assert [r[key] for r in found] == pytest.approx([r[key] for r in expected])
    assert query.analytics is not None
    assert query.analytics.details["forecast"]["unit"] == "quarter"


# ---------------------------------------------------------------- 文件沒有寫明：不支援


def unsupported(ir: IR, db: Path) -> str:
    with pytest.raises(QueryError) as info:
        run(ir, db)
    assert info.value.unsupported
    return str(info.value)


def test_period_count_that_depends_on_the_reading_is_unsupported(tmp_path: Path) -> None:
    """9 年的資料、忽略最後 1 期：算不算忽略的期會決定是否少於 9 年，說明文件沒有寫，列為不支援。"""
    rows = rows_until(dt.date(2022, 12, 31), first_year=2014)
    db = database(tmp_path / "nine.duckdb", rows)
    message = unsupported(parse(tmp_path, sheet("year")), db)
    assert "工作表「Sales Forecast」的預測：檢視中的時間序列有 8 到 9 年的資料" in message
    assert "f_" not in message and "sum(" not in message
    # 不忽略最後一期時一定不少於 9 年：照樣以年估計
    _, analytics = run(parse(tmp_path, sheet("year", ignore=0)), db)
    assert analytics.details["forecast"]["unit"] == "year"


def test_incomplete_last_year_is_unsupported(tmp_path: Path) -> None:
    db = database(tmp_path / "partial.duckdb", rows_until(dt.date(2022, 6, 30)))
    message = unsupported(parse(tmp_path, sheet("year")), db)
    assert "最後一年（2022-01-01 起）的資料只到 2022-04-01" in message


def test_too_few_quarters_is_unsupported(tmp_path: Path) -> None:
    """2 年（8 季）：以季估計仍少於 9 季，Tableau 說資料仍不夠時改以月，沒有寫明怎樣算不夠。"""
    db = database(tmp_path / "two.duckdb", rows_until(dt.date(2020, 12, 31)))
    message = unsupported(parse(tmp_path, sheet("year")), db)
    assert "以季估計也只有 7 季" in message


def test_days_by_hours_and_blockers_are_unsupported(tmp_path: Path) -> None:
    days = [
        ("East", "Furniture", f"D{i}", dt.date(2019, 1, 1 + i), 10.0 + i, 1.0, 1) for i in range(5)
    ]
    db = database(tmp_path / "days.duckdb", days)
    # 日（5 天）：Tableau 以小時估計，尚不支援
    message = unsupported(parse(tmp_path, sheet("day", regions=False)), db)
    assert "以小時估計尚不支援" in message
    # 量值篩選：在較細的層級篩選的結果可能不同
    at_least = (
        f"<filter class='quantitative' column='{ref('sum:Sales:qk')}' included-values='in-range'>"
        "<min>0</min></filter>"
    )
    big = database(tmp_path / "big.duckdb", rows_until(dt.date(2022, 12, 31)))
    message = unsupported(parse(tmp_path, sheet("year", view_extra=at_least)), big)
    assert "有量值或 Table Calculation 的篩選" in message


def test_weeks_by_days_and_no_fetch() -> None:
    """週→日（以 apply 直接計算）：檢視的週依資料源的週起始日（這裡是週一），較細的日歸到它所在的週；
    沒有辦法另外查詢時列為不支援。"""
    from tmig.ir.models import FieldUse, Forecast
    from tmig.planner.analytics import Finer, UnsupportedForecast

    start = dt.date(2024, 1, 1)  # 週一
    days = [start + dt.timedelta(days=i) for i in range(28)]
    daily = [(d, 10.0 + (i % 7) + i * 0.5) for i, d in enumerate(days)]
    weekly: dict[dt.date, float] = {}
    for d, v in daily:
        week = d - dt.timedelta(days=d.weekday())
        weekly[week] = weekly.get(week, 0.0) + v
    rows = [(w, v, ACTUAL) for w, v in sorted(weekly.items())]
    settings = Forecast(
        measures=[FieldUse(field="f_sales", agg="sum")],
        ignore_last=0,
        length="next",
        size=2,
        unit="week",
    )

    def plan() -> Analytics:
        return Analytics(
            forecast=ForecastPlan(
                time="week",
                unit="week",
                measures=["sum(f_sales)"],
                indicator="indicator",
                settings=settings,
                additive=["sum(f_sales)"],
                worksheet="Weekly",
                finer=Finer("day", "day", None),
            )
        )

    columns = ["week", "sum(f_sales)", "indicator"]
    with pytest.raises(UnsupportedForecast, match="這裡不能另外查詢較細的資料"):
        apply(plan(), columns, list(rows))
    analytics = plan()
    out_columns, out = apply(
        analytics, columns, list(rows), lambda _q: (["day", "sum(f_sales)"], list(daily))
    )
    found = best_fit([v for _, v in daily], SEASONS["day"], True)
    predicted = forecast(found, 14).values
    estimates = [r for r in out if r[2] == ESTIMATE]
    assert [r[0] for r in estimates] == [dt.date(2024, 1, 29), dt.date(2024, 2, 5)]
    assert estimates[0][1] == pytest.approx(sum(predicted[:7]))
    assert estimates[1][1] == pytest.approx(sum(predicted[7:]))
    assert out_columns[-2:] == [LOW + "sum(f_sales)", HIGH + "sum(f_sales)"]
