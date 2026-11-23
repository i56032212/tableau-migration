"""預測結果（量值膠囊的「預測結果」：趨勢、精度、精度 %、品質、上層／較低預測區間、無）與描述預測的摘要
（初始、相對於初始值的變化、季節影響、貢獻）。測試資料見 test_query；預測的設定見 test_analytics。"""

from __future__ import annotations

import datetime as dt
import itertools
import math
from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import execute_query
from tmig.ir.models import FieldUse, Forecast
from tmig.parser.workbook import parse_workbook
from tmig.planner.analytics import ACTUAL, ESTIMATE, HIGH, LOW, Analytics, ForecastPlan, apply
from tmig.planner.forecast import (
    Model,
    best_fit,
    forecast,
    precisions,
    result_values,
    summary,
    trend_values,
)
from tmig.render.names import pill_name
from tmig.render.vegalite import build_spec, value_format
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from ..render.test_vegalite import validator
from .test_analytics import FORECAST
from .test_query import SALES, db

__all__ = ["db"]  # pytest fixture

MONTH = "trunc_month(f_order_date)"
VALUE = "sum(f_sales)"


def result_instance(kind: str, name: str) -> str:
    """預測結果的欄位實例：與實際值與預測值（fVal:）相同的寫法，forecast-column-type 換成結果類型。"""
    return (
        "<column-instance column='[Sales]' derivation='Sum' forecast-column-base='[sum:Sales:qk]' "
        f"forecast-column-type='{kind}' name='[{name}:sum:Sales:qk]' pivot='key' "
        "type='quantitative' />"
    )


INDICATOR = (
    "<column-instance column='[Forecast Indicator]' derivation='None' "
    "forecast-column-base='[Forecast Indicator]' forecast-column-type='forecast-indicator' "
    "name='[none:Forecast Indicator:nk]' pivot='key' type='nominal' />"
)


def results_ir(
    tmp_path: Path,
    extra: dict[str, str] | None = None,
    on_rows: tuple[str, ...] = ("fVal", "fPIU", "fPIL"),
) -> Any:
    """月份的銷售：列上是實際值與預測值、上層預測區間、較低預測區間（on_rows），工具提示上是其餘的結果類型
    （趨勢、精度、精度 % 與品質）。"""
    kinds = {
        "fTrd": "forecast-trend",
        "fPrc": "forecast-precision",
        "fPct": "forecast-precision-percent",
        "fQly": "forecast-quality",
        "fPIU": "forecast-prediction-interval-upper",
        "fPIL": "forecast-prediction-interval-lower",
        **(extra or {}),
    }
    deps = [
        instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative"),
        SALES,
        result_instance("forecast-value", "fVal"),
        *(result_instance(kind, name) for name, kind in kinds.items()),
        INDICATOR,
    ]
    rows = " + ".join(ref(f"{n}:sum:Sales:qk") for n in on_rows)
    tips = "".join(
        f"<tooltip column='{ref(f'{n}:sum:Sales:qk')}' />" for n in kinds if n not in on_rows
    )
    sheet = worksheet(
        "Trend",
        deps,
        rows=f"({rows})" if len(on_rows) > 1 else rows,
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=f"<color column='{ref('none:Forecast Indicator:nk')}' />{tips}",
        table_extra=FORECAST,
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    return parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))


def test_forecast_result_types_are_parsed(tmp_path: Path) -> None:
    """預測結果的各種類型是同一個量值加上結果類型（FieldUse.forecast），各自一欄並都列為預測的量值；
    實際值與預測值（fVal:）是量值本身。上層、較低預測區間的 forecast-column-type（fPIU:、fPIL:）取自公開
    報表（tests/parser/test_forecast_result_names.py），其餘的名稱沒有樣本（推定的）。"""
    ir = results_ir(tmp_path)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    assert ws.forecast is not None
    sales = FieldUse(field="f_sales", agg="sum", continuous=True)
    assert ws.rows == [
        sales,
        sales.model_copy(update={"forecast": "upper"}),
        sales.model_copy(update={"forecast": "lower"}),
    ]
    assert {u.forecast for u in ws.forecast.measures} == {
        None,
        "trend",
        "precision",
        "precision_percent",
        "quality",
        "upper",
        "lower",
    }
    fields = {f.id: f for f in ir.datasources[0].fields}
    assert pill_name(ws.rows[1], fields) == "SUM(Sales)（上層預測區間）"
    # 「無」：量值照常查詢，但不預測
    none = results_ir(tmp_path / "none", {"fNon": "forecast-none"})
    assert none.unsupported == []
    assert all(u.forecast != "none" for u in none.worksheets[0].forecast.measures)


def test_unknown_forecast_result_type_stays_unsupported(tmp_path: Path) -> None:
    """不認得的結果類型照舊列為不支援（報表進人工佇列），不當成實際值與預測值。"""
    ir = results_ir(tmp_path, {"fOdd": "forecast-something-else"})
    reasons = [u.reason for u in ir.unsupported]
    assert any("forecast-something-else" in r for r in reasons), reasons


def test_forecast_results_are_computed(tmp_path: Path, db: Path) -> None:
    """估計的各期：上層／較低預測區間是預測區間的上下界，精度是區間的一半，精度 % 是精度 ÷ 估計值，品質是
    100 × MAX(1 − MASE, 0)，趨勢是去掉季節成分的估計值（15 個月的資料估計不了 12 個月的季節，與估計值相同）。
    實際值的期，結果類型的欄是 Null（推定）；實際值與預測值的欄照舊。"""
    ir = results_ir(tmp_path)
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    result = execute_query(db, query)
    rows = result.records()
    estimates = [r for r in rows if r["f_forecast_indicator"] == ESTIMATE]
    actuals = [r for r in rows if r["f_forecast_indicator"] == ACTUAL]
    assert len(estimates) == 4 and len(actuals) == 5
    kinds = ("trend", "precision", "precision_percent", "quality", "upper", "lower")
    for r in actuals:
        assert r[VALUE] is not None
        assert all(r[f"{VALUE}~{k}"] is None for k in kinds)
    for r in estimates:
        value, low, high = r[VALUE], r[LOW + VALUE], r[HIGH + VALUE]
        assert low < value < high
        assert r[f"{VALUE}~upper"] == pytest.approx(high)
        assert r[f"{VALUE}~lower"] == pytest.approx(low)
        assert r[f"{VALUE}~precision"] == pytest.approx((high - low) / 2)
        assert r[f"{VALUE}~precision_percent"] == pytest.approx((high - low) / 2 / abs(value))
        assert r[f"{VALUE}~trend"] == pytest.approx(value)
        # 結果類型的欄沒有自己的預測區間
        assert r[LOW + f"{VALUE}~upper"] is None
    quality = {r[f"{VALUE}~quality"] for r in estimates}
    assert len(quality) == 1 and 0 <= quality.pop() <= 100
    # 描述預測：同一個量值只估計一次，模型只列一次
    assert query.analytics is not None
    assert [m["measure"] for m in query.analytics.details["forecast"]["models"]] == [VALUE]


def test_forecast_results_render(tmp_path: Path, db: Path) -> None:
    """圖：預測區間只畫在實際值與預測值上（工具提示上的上層預測區間等沒有自己的區間）；精度 % 以百分比、
    品質以整數顯示（推定），工具提示的標題寫出結果類型。三個量值各自一個座標軸時照常畫出。"""
    ir = results_ir(tmp_path, on_rows=("fVal",))
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    result = execute_query(db, query)
    rendered = build_spec(ir, ws.id, query.columns, result)
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    bands = [layer["name"] for layer in rendered.spec["layer"][:-1]]
    assert bands == ["__deco_forecast0"]
    text = str(rendered.spec)
    for name in ("趨勢", "精度", "精度 %", "品質", "上層預測區間", "較低預測區間"):
        assert f"Sales（{name}）" in text, name
    fields = {f.id: f for f in ir.datasources[0].fields}
    percent = next(u for u in ws.forecast.measures if u.forecast == "precision_percent")
    quality = next(u for u in ws.forecast.measures if u.forecast == "quality")
    upper = next(u for u in ws.forecast.measures if u.forecast == "upper")
    assert value_format(ws, fields["f_sales"], percent) == "p0.00%"
    assert value_format(ws, fields["f_sales"], quality) == "n#,##0"
    assert value_format(ws, fields["f_sales"], upper) == fields["f_sales"].format

    ir = results_ir(tmp_path / "axes")
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    rendered = build_spec(ir, ws.id, query.columns, execute_query(db, query))
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []


def seasonal_plan(kind: str | None = None) -> tuple[Analytics, list[str], list[tuple[Any, ...]]]:
    """四年的月資料（加法的季節加上直線趨勢），忽略最後一期、自動的長度（12 個月）。"""
    columns = ["month", "sales"] + (["sales~" + kind] if kind else [])
    rows = []
    for i in range(48):
        day = dt.date(2021 + i // 12, i % 12 + 1, 1)
        value = 200 + 3 * i + 25 * math.sin(2 * math.pi * i / 12)
        rows.append((day, value, *([value] if kind else [])))
    measures = [FieldUse(field="f_sales", agg="sum")]
    plan = ForecastPlan(
        time="month",
        unit="month",
        measures=columns[1:],
        settings=Forecast(measures=measures),
        results={"sales~" + kind: kind} if kind else {},
        bases={"sales~" + kind: "sales"} if kind else {},
    )
    return Analytics(forecast=plan), columns, rows


def test_trend_removes_the_seasonal_component() -> None:
    """有季節的模型：趨勢是估計值減去（加法）或除以（乘法）季節成分，即水準與趨勢的組合。"""
    analytics, columns, rows = seasonal_plan("trend")
    out_columns, out = apply(analytics, columns, rows)
    model = analytics.details["forecast"]["models"][0]
    assert model["season"] != "none"
    estimated = [r for r in out if r[2] is not None]
    assert len(estimated) == 13  # 忽略的最後一期與之後的 12 個月
    trend = [r[2] for r in estimated]
    values = [r[1] for r in estimated]
    # 去掉季節之後沒有起伏：相鄰兩期的差幾乎相同（直線的趨勢），估計值則隨季節上下
    steps = [b - a for a, b in itertools.pairwise(trend)]
    assert max(steps) - min(steps) < 0.5
    assert max(abs(v - t) for v, t in zip(values, trend, strict=True)) > 10
    # 實際值的期沒有趨勢的值（推定）
    assert all(r[2] is None for r in out[:47])
    assert out_columns[:3] == columns


def test_result_values_match_definitions() -> None:
    """result_values 與 Tableau「Forecast Field Results」的定義逐一核對（乘法的模型以模擬算預測區間，
    上下不對稱時精度取區間的一半）。"""
    values = [100 + 10 * math.sin(2 * math.pi * i / 4) + 2 * i for i in range(24)]
    for model in (Model("additive", "additive"), Model("additive", "multiplicative")):
        found = best_fit(values, 4, model=model)
        predicted = forecast(found, 6)
        assert result_values(None, found, predicted, 6) == predicted.values
        assert result_values("upper", found, predicted, 6) == predicted.high
        assert result_values("lower", found, predicted, 6) == predicted.low
        spread = precisions(predicted)
        assert result_values("precision", found, predicted, 6) == spread
        assert result_values("precision_percent", found, predicted, 6) == pytest.approx(
            [p / abs(v) for p, v in zip(spread, predicted.values, strict=True)]
        )
        trend = trend_values(found, 6)
        seasons = [found.seasons[(found.n + h - 1) % 4] for h in range(1, 7)]
        if model.season == "additive":
            assert trend == pytest.approx(
                [v - s for v, s in zip(predicted.values, seasons, strict=True)]
            )
        else:
            assert trend == pytest.approx(
                [v / s for v, s in zip(predicted.values, seasons, strict=True)]
            )
        assert found.mase is not None
        assert result_values("quality", found, predicted, 6) == [100 * max(1 - found.mase, 0)] * 6
    # 單純預測比較好時品質是 0；資料沒有變動時品質沒有定義
    walk = [100.0, 101, 103, 102, 105, 107, 106, 109, 111, 110, 113, 115, 114, 117, 119, 118]
    poor = best_fit(walk, 4, model=Model("none", "multiplicative"))
    assert poor.quality_score == 0
    flat = best_fit([7.0] * 8, 1, model=Model("none", "none"))
    assert flat.quality_score is None


def test_describe_forecast_summary() -> None:
    """描述預測的摘要：初始（第一個估計的期的估計值與精度）、相對於初始值的變化（最後一個減第一個估計值）、
    季節影響（最後一個完整季節的季節成分的高值與低值，推定為最後 m 期）、貢獻（趨勢與季節，加起來 100%，
    算法是推定的）。"""
    analytics, columns, rows = seasonal_plan()
    _, out = apply(analytics, columns, rows)
    model = analytics.details["forecast"]["models"][0]
    estimates = [r for r in out[47:]]
    assert model["initial_time"] == dt.date(2024, 12, 1)
    assert model["until"] == dt.date(2025, 12, 1)
    assert model["initial"] == pytest.approx(estimates[0][1])
    band = (estimates[0][3] - estimates[0][2]) / 2
    assert model["initial_precision"] == pytest.approx(band)
    assert model["change"] == pytest.approx(estimates[-1][1] - estimates[0][1])
    # 季節成分約 ±25（正弦的振幅），加總約為 0
    assert 15 < model["seasonal_high"] < 35 and -35 < model["seasonal_low"] < -15
    assert model["contribution_trend"] + model["contribution_season"] == pytest.approx(100)
    assert 0 < model["contribution_season"] < 100

    # 沒有季節的模型：沒有季節影響，趨勢的貢獻是 100%
    plain = summary(*_fit_and_forecast([10.0 + 2 * i for i in range(12)], 1), 3)
    assert (plain["seasonal_high"], plain["seasonal_low"]) == (None, None)
    assert (plain["contribution_trend"], plain["contribution_season"]) == (100.0, 0.0)
    assert plain["change"] == pytest.approx(4.0)


def _fit_and_forecast(values: list[float], period: int) -> tuple[Any, Any]:
    found = best_fit(values, period, seasonal=False)
    return found, forecast(found, 3)
