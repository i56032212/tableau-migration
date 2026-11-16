"""查詢之後的分析：叢集（tmig.planner.cluster）與預測（tmig.planner.forecast）。

工作表的 SQL 照常查詢（叢集欄位是常數 0、Forecast Indicator 是常數 "Actual"），執行後依
WorksheetSql.analytics 的計畫補上叢集編號、預測的估計值與預測區間。每條執行路徑（萃取、Live）都在
執行器的最後呼叫 apply()。
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from tmig.ir.models import Forecast

ESTIMATE = "Estimate"
ACTUAL = "Actual"
LOW = "__fc_low:"
HIGH = "__fc_high:"
"""預測區間的欄名前綴（後接量值的輸出欄名）。"""


@dataclass
class ClusterPlan:
    column: str
    """叢集欄位的輸出欄名。"""
    measures: list[str]
    """分析的量值的輸出欄名。"""
    count: int | None = None


@dataclass
class ForecastPlan:
    time: str
    """時間維度的輸出欄名。"""
    unit: str
    """時間的細度：year、quarter、month、week、day、hour（截斷），或 year_number（年份的整數）。"""
    measures: list[str]
    series: list[str] = field(default_factory=list)
    """其他維度的輸出欄名：各自一條時間序列。"""
    indicator: str | None = None
    settings: Forecast | None = None


@dataclass
class Analytics:
    cluster: ClusterPlan | None = None
    forecast: ForecastPlan | None = None
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    """計算之後的模型摘要（「描述叢集」「描述預測」）：cluster、forecast。"""


def apply(
    analytics: Analytics | None, columns: list[str], rows: list[tuple[Any, ...]]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """依計畫補上叢集與預測；沒有計畫時原樣回傳。"""
    if analytics is None:
        return columns, rows
    if analytics.cluster is not None:
        rows = _clusters(analytics.cluster, columns, rows, analytics.details)
    if analytics.forecast is not None:
        columns, rows = _forecast(
            analytics.forecast, columns, rows, analytics.notes, analytics.details
        )
    return columns, rows


# ---------------------------------------------------------------- 叢集


def _clusters(
    plan: ClusterPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    details: dict[str, Any] | None = None,
) -> list[tuple[Any, ...]]:
    from tmig.planner.cluster import cluster, describe

    at = columns.index(plan.column)
    measures = [columns.index(m) for m in plan.measures]
    valid = [i for i, row in enumerate(rows) if all(_number(row[j]) is not None for j in measures)]
    values = [[float(rows[i][j]) for j in measures] for i in valid]
    labels = cluster(values, plan.count)
    if details is not None:
        # 描述叢集：變數、沒有分到叢集的標記數（量值是 Null）與各叢集的摘要
        details["cluster"] = {
            **describe(values, labels),
            "measures": plan.measures,
            "column": plan.column,
            "unclustered": len(rows) - len(valid),
            "automatic": plan.count is None,
        }
    assigned = dict(zip(valid, labels, strict=True))
    return [_replace(row, at, assigned.get(i, 0)) for i, row in enumerate(rows)]


# ---------------------------------------------------------------- 預測


def _forecast(
    plan: ForecastPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    notes: list[str],
    details: dict[str, Any] | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    from tmig.planner.forecast import AUTO_LENGTH, SEASONS, ForecastError, Model, best_fit, forecast

    settings = plan.settings or Forecast(measures=[])
    time_at = columns.index(plan.time)
    measures = [columns.index(m) for m in plan.measures]
    series_at = [columns.index(s) for s in plan.series]
    indicator_at = columns.index(plan.indicator) if plan.indicator in columns else None
    extra = [p + m for m in plan.measures for p in (LOW, HIGH)]
    width = len(columns)
    out_columns = [*columns, *extra]
    groups: dict[tuple[Any, ...], list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(tuple(row[j] for j in series_at), []).append(i)
    unit = plan.unit
    period = SEASONS.get(unit, 1)
    seasonal = settings.model != "auto_no_season" and period > 1
    custom = (
        Model(settings.trend or "none", settings.season or "none")
        if settings.model == "custom"
        else None
    )
    result: list[tuple[Any, ...]] = [(*row, *([None] * len(extra))) for row in rows]
    appended: dict[int, list[tuple[Any, ...]]] = {}
    models: list[dict[str, Any]] = []
    for key, members in groups.items():
        dated = sorted(
            (i for i in members if rows[i][time_at] is not None), key=lambda i: rows[i][time_at]
        )
        if len(dated) < 2:
            continue
        timeline = _timeline(rows[dated[0]][time_at], rows[dated[-1]][time_at], unit)
        index = {rows[i][time_at]: i for i in dated}
        trim = min(max(settings.ignore_last, 0), len(timeline) - 1)
        fitted_times = timeline[: len(timeline) - trim]
        horizon = trim + _horizon(settings, unit, timeline[-1], AUTO_LENGTH)
        future = [_step(timeline[-1], unit, k) for k in range(1, horizon - trim + 1)]
        estimates: list[list[float] | None] = []
        bands: list[tuple[list[float], list[float]] | None] = []
        for j in measures:
            values = []
            for t in fitted_times:
                value = _number(rows[index[t]][j]) if t in index else None
                values.append(value)
            series = _fill(values, settings.fill_zeros)
            if series is None:
                estimates.append(None)
                bands.append(None)
                continue
            try:
                found = best_fit(series, period, seasonal, custom)
            except ForecastError as exc:
                notes.append(f"預測（{columns[j]}）：{exc}")
                estimates.append(None)
                bands.append(None)
                continue
            predicted = forecast(found, horizon, settings.confidence)
            estimates.append(predicted.values)
            bands.append((predicted.low, predicted.high))
            models.append(
                {
                    "series": list(key),
                    "measure": columns[j],
                    "model": found.model.code,
                    "trend": found.model.trend,
                    "season": found.model.season,
                    "alpha": found.params[0],
                    "beta": found.params[1],
                    "gamma": found.params[2],
                    "aic": found.aic,
                    "rmse": math.sqrt(found.sse / found.n) if found.n else None,
                    "mae": found.mae,
                    "mase": found.mase,
                    "mape": found.mape,
                    "quality": found.quality,
                    "observations": found.n,
                    "period": found.period,
                    "horizon": horizon,
                    "first": fitted_times[0],
                    "last": fitted_times[-1],
                    "until": future[-1] if future else timeline[-1],
                }
            )
        if all(e is None for e in estimates):
            continue
        # 忽略的最後幾期：以估計值取代實際值
        trimmed = timeline[len(timeline) - trim :]
        for h, t in enumerate(trimmed):
            i = index.get(t)
            if i is None:
                continue
            result[i] = _estimate_row(
                result[i], measures, estimates, bands, h, indicator_at, width, len(plan.measures)
            )
        last = max(members)
        new_rows = appended.setdefault(last, [])
        for h, t in enumerate(future, start=trim):
            base: list[Any] = [None] * (width + len(extra))
            for s, j in zip(key, series_at, strict=True):
                base[j] = s
            base[time_at] = t
            new_rows.append(
                _estimate_row(
                    tuple(base),
                    measures,
                    estimates,
                    bands,
                    h,
                    indicator_at,
                    width,
                    len(plan.measures),
                )
            )
    ordered: list[tuple[Any, ...]] = []
    for i, row in enumerate(result):
        ordered.append(row)
        ordered.extend(appended.get(i, []))
    if details is not None:
        # 描述預測：選項與各條時間序列的模型（水準、趨勢、季節的平滑係數與品質）
        details["forecast"] = {
            "time": plan.time,
            "unit": unit,
            "series": plan.series,
            "ignore_last": max(settings.ignore_last, 0),
            "confidence": settings.confidence,
            "seasonal": seasonal,
            "models": models,
        }
    return out_columns, ordered


def _estimate_row(
    row: tuple[Any, ...],
    measures: list[int],
    estimates: list[list[float] | None],
    bands: list[tuple[list[float], list[float]] | None],
    h: int,
    indicator_at: int | None,
    width: int,
    count: int,
) -> tuple[Any, ...]:
    values = list(row)
    for n, (j, predicted, band) in enumerate(zip(measures, estimates, bands, strict=True)):
        if predicted is None or band is None:
            continue
        values[j] = predicted[h]
        values[width + 2 * n] = band[0][h]
        values[width + 2 * n + 1] = band[1][h]
    if indicator_at is not None:
        values[indicator_at] = ESTIMATE
    assert len(values) == width + 2 * count
    return tuple(values)


def _horizon(settings: Forecast, unit: str, last: Any, auto: dict[str, int]) -> int:
    """預測的期數（不含忽略的最後幾期）。"""
    if settings.length == "auto" or settings.size is None:
        return auto.get("year" if unit == "year_number" else unit, 5)
    size = max(settings.size, 0)
    target = settings.unit or ("year" if unit == "year_number" else unit)
    per = _periods_per(target, "year" if unit == "year_number" else unit)
    if settings.length == "next":
        return max(size * per, 1)
    # until：到 size 個 target 之後的期末
    count = 0
    current = last
    end = _end_of(last, target, size, unit)
    while count < 10000:
        nxt = _step(current, unit, 1)
        if _after(nxt, end):
            break
        current = nxt
        count += 1
    return max(count, 1)


PER_YEAR = {"year": 1, "quarter": 4, "month": 12, "week": 52, "day": 365, "hour": 8760}


def _periods_per(target: str, unit: str) -> int:
    if target == unit:
        return 1
    if target in PER_YEAR and unit in PER_YEAR:
        return max(PER_YEAR[unit] // PER_YEAR[target], 1)
    return 1


def _end_of(last: Any, target: str, size: int, unit: str) -> Any:
    if unit == "year_number":
        return int(last) + size
    year = last.year + (size if target == "year" else 0)
    if target == "year":
        return dt.date(year, 12, 31)
    if target == "quarter":
        months = (last.month - 1) // 3 * 3 + 3 * size + 2
        y, m = last.year + months // 12, months % 12 + 1
        return dt.date(y, m, calendar.monthrange(y, m)[1])
    if target == "month":
        months = last.month - 1 + size
        y, m = last.year + months // 12, months % 12 + 1
        return dt.date(y, m, calendar.monthrange(y, m)[1])
    return _step(last, unit, size)


def _after(value: Any, end: Any) -> bool:
    if isinstance(value, dt.datetime) and not isinstance(end, dt.datetime):
        return value.date() > end
    return value > end


def _timeline(first: Any, last: Any, unit: str) -> list[Any]:
    found = [first]
    while found[-1] < last and len(found) < 100000:
        found.append(_step(found[-1], unit, 1))
    return found


def _step(value: Any, unit: str, k: int) -> Any:
    if unit == "year_number":
        return int(value) + k
    if unit in ("year", "quarter", "month"):
        months = {"year": 12, "quarter": 3, "month": 1}[unit] * k
        total = value.month - 1 + months
        year, month = value.year + total // 12, total % 12 + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)
    if unit == "week":
        return value + dt.timedelta(weeks=k)
    if unit == "day":
        return value + dt.timedelta(days=k)
    if unit == "hour":
        return value + dt.timedelta(hours=k)
    raise ValueError(f"預測的時間細度 {unit} 不支援")


def _fill(values: list[float | None], zeros: bool) -> list[float] | None:
    """缺少的期：補 0（「以 0 填補缺少的值」），否則以前後的值線性內插（頭尾缺少時以最近的值）。"""
    if zeros:
        return [0.0 if v is None else v for v in values]
    known = [i for i, v in enumerate(values) if v is not None]
    if len(known) < 2:
        return None
    filled = list(values)
    for i, v in enumerate(values):
        if v is not None:
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        if before is None:
            filled[i] = values[after]  # type: ignore[index]
        elif after is None:
            filled[i] = values[before]
        else:
            a, b = values[before], values[after]
            assert a is not None and b is not None
            filled[i] = a + (b - a) * (i - before) / (after - before)
    return [float(v) for v in filled]  # type: ignore[arg-type]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _replace(row: tuple[Any, ...], at: int, value: Any) -> tuple[Any, ...]:
    return (*row[:at], value, *row[at + 1 :])
