"""叢集（分析 → 叢集）與預測（分析 → 預測）：解析、查詢之後的計算與圖表。測試資料見 test_query。"""

from __future__ import annotations

import datetime as dt
import itertools
import math
from pathlib import Path

import pytest

from tmig.data.executor import execute_query
from tmig.parser.workbook import parse_workbook
from tmig.planner.analytics import ACTUAL, ESTIMATE, HIGH, LOW
from tmig.planner.cluster import calinski_harabasz, cluster, kmeans, scale
from tmig.planner.forecast import Model, best_fit, forecast
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from ..render.test_vegalite import validator
from .test_query import REGION, SALES, db

__all__ = ["db"]  # pytest fixture

# ---------------------------------------------------------------- 叢集


def test_kmeans_splits_until_k_clusters() -> None:
    groups = [[1, 1], [1.1, 0.9], [5, 5], [5.2, 5.1], [9, 1], [9.1, 1.2], [1, 9], [1.2, 9.1]]
    labels = cluster(groups)
    # 四群（自動以 Calinski-Harabasz 的第一個局部最大值決定），同一群的點編號相同
    assert len(set(labels)) == 4
    assert all(labels[i] == labels[i + 1] for i in range(0, 8, 2))
    two = cluster(groups, 2)
    assert len(set(two)) == 2 and two[0] == two[1]
    import numpy as np

    points = scale(np.asarray(groups, dtype=float))
    assert points.min() == 0 and points.max() == 1
    four, _ = kmeans(points, 4)
    three, _ = kmeans(points, 3)
    assert calinski_harabasz(points, four) > calinski_harabasz(points, three)


ANALYTIC_MODEL = f"""
  <analytic-model>
    <name><field name='[AnalyticModel:{{test}}]' /></name>
    <analytic-model-specifications class='cluster-specifications'>
      <analysis-fields><field name='[{b.DS}].[sum:Sales:qk]' /></analysis-fields>
      <filter-fields /><lod-fields />
      <number-of-desired-clusters>{{count}}</number-of-desired-clusters>
      <is-dissagregate>false</is-dissagregate>
    </analytic-model-specifications>
    <result-fields />
  </analytic-model>"""
CLUSTER_COLUMN = (
    "<column caption='Clusters' datatype='integer' hidden='true' is-adhoc-cluster='true' "
    "name='[AdhocCluster:1]' parent-model='[AnalyticModel:{test}]' role='dimension' type='ordinal'>"
    "<aliases><alias key='0' value='Not Clustered' /><alias key='1' value='Cluster 1' />"
    "<alias key='2' value='Cluster 2' /></aliases></column>"
)


def cluster_sheet(tmp_path: Path, count: int = 2) -> object:
    sheet = worksheet(
        "Regions",
        [
            REGION,
            SALES,
            instance("[AdhocCluster:1]", "None", "[none:AdhocCluster:1:ok]", "ordinal"),
        ],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<color column='{ref('none:AdhocCluster:1:ok')}' />",
        deps_extra=CLUSTER_COLUMN,
    )
    model = ANALYTIC_MODEL.replace("{count}", str(count))
    xml = b.workbook(b.extract_datasource(extra=model), worksheets=sheet)
    return parse_workbook(b.write_twb(tmp_path, xml))


def test_cluster_by_view_measures(tmp_path: Path, db: Path) -> None:
    """各區的銷售分成兩群（East 350、West 800、South 100、North 60）：分割法從平均值切成
    {South, North} 與 {East, West}，Lloyd 演算法從這裡開始時 East 留在 West 那一群（局部最佳解，
    與 Tableau 的做法相同）。"""
    ir = cluster_sheet(tmp_path)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]  # type: ignore[attr-defined]
    ws = ir.worksheets[0]  # type: ignore[attr-defined]
    assert ws.cluster is not None and ws.cluster.count == 2
    query = worksheet_sql(ir, ws.id)  # type: ignore[arg-type]
    result = execute_query(db, query)
    by_region = {r["f_region"]: r["f_clusters"] for r in result.records()}
    assert by_region["East"] == by_region["West"] != by_region["South"] == by_region["North"]
    rendered = build_spec(ir, ws.id, query.columns, result)  # type: ignore[arg-type]
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    # 叢集編號以別名顯示（Cluster 1、Cluster 2）
    shown = {v for record in rendered.spec["data"]["values"] for v in record.values()}
    assert {"Cluster 1", "Cluster 2"} <= shown
    assert any("k-means" in n for n in rendered.notes)


def test_cluster_needs_measures_in_view(tmp_path: Path) -> None:
    sheet = worksheet(
        "Regions",
        [REGION, instance("[AdhocCluster:1]", "None", "[none:AdhocCluster:1:ok]", "ordinal")],
        rows=ref("none:Region:nk"),
        encodings=f"<color column='{ref('none:AdhocCluster:1:ok')}' />",
        deps_extra=CLUSTER_COLUMN,
    )
    model = ANALYTIC_MODEL.replace("{count}", "0")
    xml = b.workbook(b.extract_datasource(extra=model), worksheets=sheet)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert any("叢集的分析" in u.reason for u in ir.unsupported)


# ---------------------------------------------------------------- 預測


def test_trend_is_extended() -> None:
    values = [10.0 + 2 * i for i in range(12)]
    found = best_fit(values, 1, seasonal=False)
    assert found.model == Model("additive", "none")
    predicted = forecast(found, 3)
    assert predicted.values == pytest.approx([34.0, 36.0, 38.0])


def test_seasonal_model_for_months() -> None:
    values = [100 + 10 * math.sin(2 * math.pi * i / 12) + i for i in range(48)]
    found = best_fit(values, 12)
    assert found.model.season != "none"
    predicted = forecast(found, 12)
    truth = [100 + 10 * math.sin(2 * math.pi * i / 12) + i for i in range(48, 60)]
    assert max(abs(a - b) for a, b in zip(predicted.values, truth, strict=True)) < 5
    assert all(
        lo <= v <= hi
        for lo, v, hi in zip(predicted.low, predicted.values, predicted.high, strict=True)
    )


def test_multiplicative_models_need_positive_values() -> None:
    values = [0.0, 5, 3, 8, 6, 9, 7, 12]
    assert best_fit(values, 1, seasonal=False).model.trend != "multiplicative"


def test_forecast_quality_metrics() -> None:
    """描述預測的品質指標：MAE、MASE（與「下一期等於這一期」的單純預測相比）、MAPE 與品質。
    簡單指數平滑的一步預測誤差在這裡依定義重算一次核對。"""
    values = [12.0, 15, 11, 14, 18, 13, 16, 20, 15, 17, 22, 16]
    found = best_fit(values, 1, model=Model("none", "none"))
    alpha = found.params[0]
    level = values[0]  # 初始的水準是第一個值
    errors: list[float] = []
    for value in values:
        errors.append(value - level)
        level = alpha * value + (1 - alpha) * level
    mae = sum(abs(e) for e in errors) / len(values)
    naive = sum(abs(b - a) for a, b in itertools.pairwise(values)) / (len(values) - 1)
    assert math.sqrt(found.sse / found.n) == pytest.approx(
        math.sqrt(sum(e * e for e in errors) / len(values))
    )
    assert found.mae == pytest.approx(mae)
    assert found.mase == pytest.approx(mae / naive)
    assert found.mape == pytest.approx(
        100 * sum(abs(e) / v for e, v in zip(errors, values, strict=True)) / len(values)
    )
    # 上下跳動的資料：平滑的預測比單純預測好（MASE 小於 1），但不到一半
    assert found.mase is not None and 0.5 <= found.mase < 1 and found.quality == "ok"
    # 直線：有趨勢的模型幾乎沒有誤差，單純預測每期差 2
    line = best_fit([10.0 + 2 * i for i in range(12)], 1, seasonal=False)
    assert line.mase is not None and line.mase < 0.01 and line.quality == "good"
    assert line.mae == pytest.approx(0, abs=1e-6) and line.mape == pytest.approx(0, abs=1e-6)
    # 單純預測比較好時是 poor：隨機漫步配上錯誤的模型（指定沒有趨勢、平滑係數被迫偏離 1 的乘法季節）
    walk = [100.0, 101, 103, 102, 105, 107, 106, 109, 111, 110, 113, 115, 114, 117, 119, 118]
    forced = best_fit(walk, 4, model=Model("none", "multiplicative"))
    assert forced.mase is not None and forced.mase > 1 and forced.quality == "poor"
    # 資料中有 0：MAPE 沒有定義；資料完全不變：單純預測沒有誤差，MASE 與品質沒有定義
    zeros = best_fit([0.0, 5, 3, 8, 6, 9, 7, 12], 1, model=Model("none", "none"))
    assert zeros.mape is None and zeros.mae is not None and zeros.mase is not None
    flat = best_fit([7.0] * 8, 1, model=Model("none", "none"))
    assert flat.mae == pytest.approx(0) and flat.mase is None and flat.quality is None


FORECAST = (
    "<forecast-specification auto-forecast-agg='true' band-confidence-level='95.000000' "
    "enabled='true' fill-type='fill-missing' ignore-last='1' model-type='auto-season' "
    "range-size='3' range-periods='Month-Trunc' range-type='next' show-prediction-bands='true' />"
)


def forecast_ir(tmp_path: Path, discrete: bool = False, mark: str = "Line") -> object:
    name = "tmn:Order Date:ok" if discrete else "tmn:Order Date:qk"
    kind = "ordinal" if discrete else "quantitative"
    month = instance("[Order Date]", "Month-Trunc", f"[{name}]", kind)
    deps = [
        month,
        SALES,
        "<column-instance column='[Sales]' derivation='Sum' forecast-column-base='[sum:Sales:qk]' "
        "forecast-column-type='forecast-value' name='[fVal:sum:Sales:qk]' pivot='key' "
        "type='quantitative' />",
        "<column-instance column='[Forecast Indicator]' derivation='None' "
        "forecast-column-base='[Forecast Indicator]' forecast-column-type='forecast-indicator' "
        "name='[none:Forecast Indicator:nk]' pivot='key' type='nominal' />",
    ]
    sheet = worksheet(
        "Trend",
        deps,
        rows=ref("fVal:sum:Sales:qk"),
        cols=ref(name),
        mark=mark,
        encodings=f"<color column='{ref('none:Forecast Indicator:nk')}' />",
        table_extra=FORECAST,
    )
    return parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))


def test_forecast_months(tmp_path: Path, db: Path) -> None:
    """月份的銷售（2023-01 到 2024-03，中間沒有資料的月以前後的值內插）：最後一個月改以估計值呈現，
    另外估計之後的 3 個月；估計的列 Forecast Indicator 為 Estimate，並有預測區間。"""
    ir = forecast_ir(tmp_path)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]  # type: ignore[attr-defined]
    ws = ir.worksheets[0]  # type: ignore[attr-defined]
    assert ws.forecast is not None and ws.forecast.length == "next" and ws.forecast.size == 3
    query = worksheet_sql(ir, ws.id)  # type: ignore[arg-type]
    result = execute_query(db, query)
    rows = result.records()
    month = "trunc_month(f_order_date)"
    estimates = [r for r in rows if r["f_forecast_indicator"] == ESTIMATE]
    actuals = [r for r in rows if r["f_forecast_indicator"] == ACTUAL]
    assert [r[month] for r in estimates] == [
        dt.date(2024, 3, 1),
        dt.date(2024, 4, 1),
        dt.date(2024, 5, 1),
        dt.date(2024, 6, 1),
    ]
    assert len(actuals) == 5 and all(r[LOW + "sum(f_sales)"] is None for r in actuals)
    for r in estimates:
        assert r[LOW + "sum(f_sales)"] <= r["sum(f_sales)"] <= r[HIGH + "sum(f_sales)"]
    rendered = build_spec(ir, ws.id, query.columns, result)  # type: ignore[arg-type]
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    band = rendered.spec["layer"][0]
    assert band["name"].startswith("__deco_forecast") and band["mark"]["type"] == "area"
    assert any("指數平滑" in n for n in rendered.notes)


def test_forecast_band_on_discrete_axis(tmp_path: Path, db: Path) -> None:
    """離散的月份（長條圖）：預測區間的區域不先篩掉實際值的列（否則合併的離散軸會把估計的期排到
    最前面），頂點在格子中間，並依 Forecast Indicator 分開。"""
    ir = forecast_ir(tmp_path, discrete=True, mark="Bar")
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]  # type: ignore[attr-defined]
    ws = ir.worksheets[0]  # type: ignore[attr-defined]
    query = worksheet_sql(ir, ws.id)  # type: ignore[arg-type]
    result = execute_query(db, query)
    rendered = build_spec(ir, ws.id, query.columns, result)  # type: ignore[arg-type]
    assert rendered.spec is not None
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    band, body = rendered.spec["layer"]
    assert band["name"].startswith("__deco_forecast") and "transform" not in band
    x = band["encoding"]["x"]
    assert x["field"] == body["encoding"]["x"]["field"] and x["bandPosition"] == 0.5
    color = body["encoding"]["color"]["field"]
    assert {"field": color, "type": "nominal"} in band["encoding"]["detail"]
    months = [record[x["field"]] for record in rendered.spec["data"]["values"]]
    assert months == sorted(months) and len(months) == len(set(months))
    estimates = [
        r for r in rendered.spec["data"]["values"] if r[band["encoding"]["y"]["field"]] is not None
    ]
    assert len(estimates) == 4 and estimates[-1][x["field"]] == months[-1]


def test_disabled_forecast_is_ignored(tmp_path: Path) -> None:
    sheet = worksheet(
        "Trend",
        [SALES, instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative")],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        table_extra=FORECAST.replace("enabled='true'", "enabled='false'"),
    )
    ir = parse_workbook(b.write_twb(tmp_path, b.workbook(worksheets=sheet)))
    assert ir.unsupported == [] and ir.worksheets[0].forecast is None
    assert any("預測已關閉" in u.reason for u in ir.ignored)
