"""查詢之後的分析：叢集（tmig.planner.cluster）、預測模型函式（tmig.planner.predictive）與預測
（tmig.planner.forecast）。

工作表的 SQL 照常查詢（叢集欄位是常數 0、MODEL_* 是 Null、Forecast Indicator 是常數 "Actual"），執行後依
WorksheetSql.analytics 的計畫補上叢集編號、預測模型函式的結果、預測的估計值與預測區間。每條執行路徑（萃取、Live）都在
執行器的最後呼叫 apply()。

預測選「自動」時 Tableau 會改以較細的時間估計（Tableau 2021.2 說明文件「How Forecasting Works in Tableau」的
Getting More Data，https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_how_it_works.htm ，2026-11-23
查閱）：檢視中少於 9 年的資料時以季估計、再彙總回年（還不夠時以月），少於 9 季時以月、少於 9 週時以日、
少於 9 日時以時、少於 9 小時以分、少於 9 分以秒；只在量值是 SUM 或 COUNT 時可以。估計的細度上「忽略最後幾期」
（說明文件「Set Options for Forecast」，forecast_options.htm）：檢視的最後一期可能是實際值與預測值的彙總。
文件寫明的部分照做（見 _refinement）：年→季、季→月、週→日，各條時間序列都完整到檢視的最後一期，且不論怎麼
數期數（算不算沒有資料的期、忽略的最後幾期）都少於 9 期；估計值依 SUM、COUNT 加總回檢視的期，預測區間以
模擬計算（說明文件：彙總的預測以模擬計算區間；模擬的方法沒有公開，推定）。文件沒有寫明的情況（期數的算法
影響結果、最後一期不完整、年以季估計仍少於 9 季時是否改以月、日→時等）列為不支援，不給出可能不同的數字。
"""

from __future__ import annotations

import bisect
import calendar
import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tmig.ir.models import Forecast
from tmig.planner.predictive import ModelPlan

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
    categories: list[str] = field(default_factory=list)
    """分析的維度（類別變數）的輸出欄名：檢視中的維度，或另外輸出的 ATTR（Null 為 * 或沒有值）。"""
    captions: dict[str, str] = field(default_factory=dict)
    """類別變數的輸出欄名 → 膠囊上的寫法：附註裡這樣稱呼，不寫輸出欄名。"""


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
    captions: dict[str, str] = field(default_factory=dict)
    """量值的輸出欄名 → 膠囊上的寫法（SUM(Profit)）：附註裡這樣稱呼量值，不寫輸出欄名。"""
    results: dict[str, str] = field(default_factory=dict)
    """預測結果不是實際值與預測值的量值：輸出欄名 → 結果類型（trend、precision、precision_percent、quality、
    upper、lower；見 tmig.planner.forecast.result_values）。這些欄只有估計的期有值（實際值的期為 Null，
    推定），也不畫預測區間。"""
    bases: dict[str, str] = field(default_factory=dict)
    """量值的輸出欄名 → 它預測的量值（不含結果類型）的欄位鍵：同一個量值以幾種結果類型出現時只估計一次，
    描述預測也只列一次。"""
    additive: list[str] = field(default_factory=list)
    """彙總是 SUM 或 COUNT 的量值（輸出欄名）：Tableau 只為它們取得較細的資料（見模組說明）。"""
    worksheet: str = ""
    """工作表的名稱（不支援時的說明用）。"""
    finer: Finer | None = None
    """以較細的時間估計時另外的查詢（年→季、季→月、週→日；其他細度為 None）。"""
    blocker: str | None = None
    """需要以較細的時間估計時不能照做的原因（例如用到預測結果、量值篩選；見 tmig.sqlgen.query）。"""


@dataclass
class Finer:
    """以較細的時間估計：查詢（tmig.sqlgen.query.WorksheetSql，維度、篩選與工作表相同，時間換成較細的
    細度）與結果中時間的輸出欄名。"""

    unit: str
    time: str
    query: Any


class UnsupportedForecast(ValueError):
    """預測需要以 Tableau 文件沒有寫明的方式計算：列為不支援（執行器轉成 QueryError）。"""


Fetch = Callable[[Any], tuple[list[str], list[tuple[Any, ...]]]]
"""執行另一個查詢（WorksheetSql）：回傳欄名與資料列。"""


@dataclass
class Analytics:
    cluster: ClusterPlan | None = None
    forecast: ForecastPlan | None = None
    models: list[ModelPlan] = field(default_factory=list)
    """預測模型函式（MODEL_QUANTILE、MODEL_PERCENTILE，見 tmig.planner.predictive）。"""
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    """計算之後的模型摘要（「描述叢集」「描述預測」）：cluster、forecast。"""


def apply(
    analytics: Analytics | None,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    fetch: Fetch | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """依計畫補上叢集與預測；沒有計畫時原樣回傳。fetch：執行以較細的時間估計的查詢（ForecastPlan.finer）；
    None 時需要它的預測列為不支援。"""
    if analytics is None:
        return columns, rows
    if analytics.cluster is not None:
        rows = _clusters(analytics.cluster, columns, rows, analytics.details)
    if analytics.models:
        from tmig.planner.predictive import apply_models

        rows = apply_models(analytics.models, columns, rows)
    if analytics.forecast is not None:
        plan = analytics.forecast
        finer = _refinement(plan, columns, rows, fetch)
        if finer is not None:
            columns, rows = _refined_forecast(
                plan, columns, rows, finer, analytics.notes, analytics.details
            )
        else:
            columns, rows = _forecast(plan, columns, rows, analytics.notes, analytics.details)
    return columns, rows


# ---------------------------------------------------------------- 叢集


def _clusters(
    plan: ClusterPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    details: dict[str, Any] | None = None,
) -> list[tuple[Any, ...]]:
    """叢集編號。量值是 Null 的標記不分叢集（Tableau 說明文件）；類別變數超過 25 個值時不用它（同上，
    描述叢集中列在 ignored），ATTR 是 *（這裡是 Null）的標記不分叢集（同上；真的沒有值的 Null 也一樣，
    推定）。"""
    from tmig.planner.cluster import MAX_CATEGORIES, cluster, describe

    at = columns.index(plan.column)
    measures = [columns.index(m) for m in plan.measures]
    categories: list[int] = []
    ignored: list[str] = []
    for name in plan.categories:
        j = columns.index(name)
        if len({row[j] for row in rows if row[j] is not None}) > MAX_CATEGORIES:
            ignored.append(plan.captions.get(name, name))
            continue
        categories.append(j)
    valid = [
        i
        for i, row in enumerate(rows)
        if all(_number(row[j]) is not None for j in measures)
        and all(row[j] is not None for j in categories)
    ]
    values = [[float(rows[i][j]) for j in measures] for i in valid]
    groups = [[rows[i][j] for j in categories] for i in valid] if categories else None
    labels = cluster(values, plan.count, groups)
    if details is not None:
        # 描述叢集：變數、沒有分到叢集的標記數（量值是 Null、類別是 *）與各叢集的摘要（量值的部分）
        details["cluster"] = {
            **describe(values, labels),
            "measures": plan.measures,
            "categories": [plan.captions.get(columns[j], columns[j]) for j in categories],
            "ignored": ignored,
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
    from tmig.planner.forecast import (
        AUTO_LENGTH,
        SEASONS,
        ForecastError,
        Model,
        best_fit,
        forecast,
        result_values,
    )

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
    # 結果類型的欄（趨勢、精度等）只有估計的期有值：實際值的期先清成 Null
    blank = {columns.index(c) for c in plan.results if c in columns}
    result: list[tuple[Any, ...]] = [
        (*(None if k in blank else v for k, v in enumerate(row)), *([None] * len(extra)))
        for row in rows
    ]
    appended: dict[int, list[tuple[Any, ...]]] = {}
    models: list[dict[str, Any]] = []
    # 可以用來估計的資料不到兩期、估計不了的時間序列數：本來就不到兩期的，與扣掉忽略的最後幾期之後不到兩期的
    too_short = 0
    short_after_ignoring = 0
    for key, members in groups.items():
        dated = sorted(
            (i for i in members if rows[i][time_at] is not None), key=lambda i: rows[i][time_at]
        )
        if len(dated) < 2:
            too_short += 1
            continue
        short = False
        timeline = _timeline(rows[dated[0]][time_at], rows[dated[-1]][time_at], unit)
        index = {rows[i][time_at]: i for i in dated}
        trim = min(max(settings.ignore_last, 0), len(timeline) - 1)
        fitted_times = timeline[: len(timeline) - trim]
        horizon = trim + _horizon(settings, unit, timeline[-1], AUTO_LENGTH)
        future = [_step(timeline[-1], unit, k) for k in range(1, horizon - trim + 1)]
        estimates: list[list[float | None] | None] = []
        bands: list[tuple[list[float], list[float]] | None] = []
        # 同一個量值以幾種結果類型出現時（同一個基礎量值的欄）只估計一次
        fitted: dict[str, Any] = {}
        estimated = [*timeline[len(timeline) - trim :], *future]
        for j in measures:
            column = columns[j]
            base = plan.bases.get(column, column)
            kind = plan.results.get(column)
            if base not in fitted:
                values = []
                for t in fitted_times:
                    value = _number(rows[index[t]][j]) if t in index else None
                    values.append(value)
                series = _fill(values, settings.fill_zeros)
                if series is None:
                    fitted[base] = None
                    short = True
                else:
                    try:
                        found = best_fit(series, period, seasonal, custom)
                    except ForecastError as exc:
                        # 每條時間序列各估計一次：同一個原因只說一次
                        _note(notes, f"預測（{plan.captions.get(column, column)}）：{exc}")
                        fitted[base] = None
                    else:
                        predicted = forecast(found, horizon, settings.confidence)
                        fitted[base] = (found, predicted)
                        models.append(
                            _model_details(
                                key, column, found, predicted, horizon, fitted_times, estimated
                            )
                        )
            if fitted[base] is None:
                estimates.append(None)
                bands.append(None)
                continue
            found, predicted = fitted[base]
            estimates.append(result_values(kind, found, predicted, horizon))  # type: ignore[arg-type]
            # 預測區間只畫在實際值與預測值上
            bands.append((predicted.low, predicted.high) if kind is None else None)
        if all(e is None for e in estimates):
            short_after_ignoring += short
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
            added: list[Any] = [None] * (width + len(extra))
            for s, j in zip(key, series_at, strict=True):
                added[j] = s
            added[time_at] = t
            new_rows.append(
                _estimate_row(
                    tuple(added),
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
    if too_short or short_after_ignoring:
        # 沒有估計的值、也沒有別的說明時，使用者看不出預測為什麼沒有出現：說明原因
        count = too_short + short_after_ignoring
        ignored = max(settings.ignore_last, 0)
        after = f"扣掉忽略的最後 {ignored} 期之後，" if short_after_ignoring and ignored else ""
        which = "" if count == len(groups) else f"有 {count} 條時間序列"
        _note(notes, f"預測：{which}{after}資料不到兩期，估計不出來")
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


# ---------------------------------------------------------------- 以較細的時間估計（「自動」）

MIN_PERIODS = 9
"""檢視中少於這麼多期時 Tableau 改以較細的時間估計（說明文件的 Getting More Data）。"""
FINER_NAMES = {
    "year": "季（還不夠時以月）",
    "year_number": "季（還不夠時以月）",
    "quarter": "月",
    "week": "日",
    "day": "小時",
    "hour": "分鐘",
}
"""檢視的細度 → Tableau 改用的細度（說明文件）。月沒有較細的估計。"""
SUB_PERIODS = {"year": 4, "quarter": 3, "week": 7}
"""平台照做的細度（年→季、季→月、週→日）：檢視的一期有幾個較細的期。"""
SUB_COUNTS = {
    ("year", "quarter"): 4,
    ("year", "month"): 12,
    ("quarter", "month"): 3,
    ("week", "day"): 7,
}
"""（檢視的細度, 估計的細度）→ 檢視的一期有幾個較細的期：「自動」的三種，加上預測選項指定「彙總依據」時的
年→月（與 tmig.sqlgen.query 的 CHOSEN_FINER 相同）。"""
UNIT_NAMES = {
    "year": "年",
    "year_number": "年",
    "quarter": "季",
    "month": "月",
    "week": "週",
    "day": "日",
    "hour": "時",
    "minute": "分",
    "second": "秒",
}
COUNT_WORDS = {
    "year": "年",
    "year_number": "年",
    "quarter": "季",
    "month": "個月",
    "week": "週",
    "day": "天",
    "hour": "小時",
}


def _series_times(
    rows: list[tuple[Any, ...]], time_at: int, series_at: list[int]
) -> dict[tuple[Any, ...], set[Any]]:
    """各條時間序列（其他維度的值）有資料的期。"""
    groups: dict[tuple[Any, ...], set[Any]] = {}
    for row in rows:
        if row[time_at] is not None:
            groups.setdefault(tuple(row[j] for j in series_at), set()).add(row[time_at])
    return groups


def _view_periods(first: Any, unit: str) -> Callable[[Any], Any]:
    """較細的期 → 它所在的檢視的期（檢視的期從 first 起依 unit 排列；週依資料源的週起始日，所以從檢視的
    第一期往後數，不另外截斷）。"""
    starts = [first]

    def of(value: Any) -> Any:
        if value < starts[0]:
            return None
        while starts[-1] <= value:
            starts.append(_step(starts[-1], unit, 1))
        return starts[bisect.bisect_right(starts, value) - 1]

    return of


def _refinement(
    plan: ForecastPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    fetch: Fetch | None,
) -> tuple[list[str], list[tuple[Any, ...]]] | None:
    """Tableau 是否改以較細的時間估計（見模組說明）：不會時 None；會、且文件寫明了做法時回傳較細的查詢結果；
    做法沒有寫明時 UnsupportedForecast（說明工作表、細度與期數）。

    期數的算法沒有寫（算不算沒有資料的期、忽略的最後幾期、各條時間序列還是整個檢視）：最少的算法也不少於
    9 期時不會改；最多的算法也少於 9 期時一定會改；之間的列為不支援。

    預測選項指定了「彙總依據」時不依期數判斷，見 _chosen_refinement。"""
    settings = plan.settings or Forecast(measures=[])
    if settings.aggregate_by is not None:
        return _chosen_refinement(plan, columns, rows, fetch)
    if not plan.additive or plan.unit not in FINER_NAMES:
        return None
    time_at = columns.index(plan.time)
    series_at = [columns.index(s) for s in plan.series]
    groups = _series_times(rows, time_at, series_at)
    if not groups:
        return None
    ignore = max(settings.ignore_last, 0)
    fewest = max(min(len(times) for times in groups.values()) - ignore, 0)
    everything = [t for times in groups.values() for t in times]
    spans = [len(_timeline(min(times), max(times), plan.unit)) for times in groups.values()]
    most = max([*spans, len(_timeline(min(everything), max(everything), plan.unit))])
    if fewest >= MIN_PERIODS:
        return None
    word = COUNT_WORDS[plan.unit]
    where = f"工作表「{plan.worksheet}」的預測" if plan.worksheet else "預測"
    rule = (
        f"Tableau 在檢視中少於 {MIN_PERIODS} {word}的資料、量值是 SUM 或 COUNT 時改以"
        f"{FINER_NAMES[plan.unit]}估計再彙總回來"
    )
    if most >= MIN_PERIODS:
        raise UnsupportedForecast(
            f"{where}：檢視中的時間序列有 {fewest} 到 {most} {word}的資料（依是否算入沒有資料的期、"
            f"忽略的最後 {ignore} 期而不同）；{rule}，說明文件沒有寫怎麼數期數，尚不支援"
        )
    head = f"{where}：檢視中只有 {most} {word}的資料，{rule}"
    if plan.unit not in SUB_PERIODS:
        raise UnsupportedForecast(f"{head}；以{FINER_NAMES[plan.unit]}估計尚不支援")
    if plan.blocker is not None:
        raise UnsupportedForecast(f"{head}；{plan.blocker}，尚不支援")
    if plan.finer is None or fetch is None:
        raise UnsupportedForecast(f"{head}；這裡不能另外查詢較細的資料，尚不支援")
    fine_columns, fine_rows = fetch(plan.finer.query)
    fine_at = fine_columns.index(plan.finer.time)
    fine_series = [fine_columns.index(s) for s in plan.series]
    fine_groups = _series_times(fine_rows, fine_at, fine_series)
    if plan.unit == "year" and fine_groups:
        quarters = max(min(len(times) for times in fine_groups.values()) - ignore, 0)
        if quarters < MIN_PERIODS:
            raise UnsupportedForecast(
                f"{head}；以季估計也只有 {quarters} 季：說明文件說資料仍不夠時改以月估計，沒有寫明"
                "怎樣算不夠，尚不支援"
            )
    _check_last_period(plan, min(everything), fine_groups, head)
    return fine_columns, fine_rows


def _check_last_period(
    plan: ForecastPlan, first: Any, fine_groups: dict[tuple[Any, ...], set[Any]], head: str
) -> None:
    """各條時間序列較細的資料要到檢視最後一期的期末：不完整的最後一期怎麼處理說明文件沒有寫，列為不支援。"""
    assert plan.finer is not None
    word = COUNT_WORDS[plan.unit]
    view_of = _view_periods(first, plan.unit)
    for times in fine_groups.values():
        last = max(times)
        if view_of(_step(last, plan.finer.unit, 1)) == view_of(last):
            raise UnsupportedForecast(
                f"{head}；最後一{word[-1]}（{_iso(view_of(last))} 起）的資料只到 {_iso(last)}，"
                "說明文件沒有寫以較細的時間估計時怎麼處理不完整的最後一期，尚不支援"
            )


def _chosen_refinement(
    plan: ForecastPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    fetch: Fetch | None,
) -> tuple[list[str], list[tuple[Any, ...]]] | None:
    """預測選項指定的「彙總依據」（Forecast.aggregate_by）：與檢視的細度相同時以檢視的期估計（不另外取得
    較細的資料，也不依期數改用較細的細度）；比檢視細時一律以它估計再彙總回檢視的期（說明文件
    forecast_options.htm：通常與檢視相同、也可以比檢視細，忽略的最後幾期是估計的細度）。彙總回去的做法與
    「自動」相同（SUM、COUNT 加總；其他彙總怎麼回去文件沒有寫）。平台照做的組合是 SUB_COUNTS，其餘、比檢視粗
    與文件沒有寫明的情況列為不支援。"""
    settings = plan.settings or Forecast(measures=[])
    chosen = settings.aggregate_by
    view = "year" if plan.unit == "year_number" else plan.unit
    if chosen is None or chosen == view:
        return None
    where = f"工作表「{plan.worksheet}」的預測" if plan.worksheet else "預測"
    head = (
        f"{where}：預測選項指定以{UNIT_NAMES.get(chosen, chosen)}估計（檢視的時間是"
        f"{UNIT_NAMES.get(plan.unit, plan.unit)}{'份' if plan.unit == 'year_number' else ''}），"
        "Tableau 估計之後彙總回檢視的期"
    )
    if (plan.unit, chosen) not in SUB_COUNTS:
        raise UnsupportedForecast(f"{head}；這個組合尚不支援")
    if len(plan.additive) != len(plan.measures):
        raise UnsupportedForecast(
            f"{head}；量值不是 SUM 或 COUNT 時怎麼彙總回檢視的期說明文件沒有寫，尚不支援"
        )
    if plan.blocker is not None:
        raise UnsupportedForecast(f"{head}；{plan.blocker}，尚不支援")
    if plan.finer is None or fetch is None:
        raise UnsupportedForecast(f"{head}；這裡不能另外查詢較細的資料，尚不支援")
    time_at = columns.index(plan.time)
    dated = [row[time_at] for row in rows if row[time_at] is not None]
    fine_columns, fine_rows = fetch(plan.finer.query)
    if not dated:
        return fine_columns, fine_rows
    fine_at = fine_columns.index(plan.finer.time)
    fine_series = [fine_columns.index(s) for s in plan.series]
    _check_last_period(plan, min(dated), _series_times(fine_rows, fine_at, fine_series), head)
    return fine_columns, fine_rows


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _refined_forecast(
    plan: ForecastPlan,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    fine: tuple[list[str], list[tuple[Any, ...]]],
    notes: list[str],
    details: dict[str, Any] | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """以較細的時間估計再加總回檢視的期（SUM、COUNT）：每條時間序列以較細的期配適模型（忽略的最後幾期是較細
    的期），之後的期數是檢視的期數 × 每期的較細期數；檢視的一期是其中實際的較細期的值加上估計的較細期的
    估計值（含忽略的最後幾期的檢視的期，成為估計的期）。預測區間以模擬的路徑同樣加總後取分位數（推定）。"""
    import numpy as np

    from tmig.planner.forecast import (
        AUTO_LENGTH,
        SEASONS,
        ForecastError,
        Model,
        best_fit,
        forecast,
        simulate,
    )

    assert plan.finer is not None
    settings = plan.settings or Forecast(measures=[])
    unit, fine_unit = plan.unit, plan.finer.unit
    fine_columns, fine_rows = fine
    time_at = columns.index(plan.time)
    measures = [columns.index(m) for m in plan.measures]
    series_at = [columns.index(s) for s in plan.series]
    indicator_at = columns.index(plan.indicator) if plan.indicator in columns else None
    fine_at = fine_columns.index(plan.finer.time)
    fine_measures = [fine_columns.index(m) for m in plan.measures]
    fine_series = [fine_columns.index(s) for s in plan.series]
    extra = [p + m for m in plan.measures for p in (LOW, HIGH)]
    width = len(columns)
    result: list[tuple[Any, ...]] = [(*row, *([None] * len(extra))) for row in rows]
    groups: dict[tuple[Any, ...], list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(tuple(row[j] for j in series_at), []).append(i)
    fine_groups: dict[tuple[Any, ...], dict[Any, tuple[Any, ...]]] = {}
    for row in fine_rows:
        if row[fine_at] is not None:
            fine_groups.setdefault(tuple(row[j] for j in fine_series), {})[row[fine_at]] = row
    dated_all = [row[time_at] for row in rows if row[time_at] is not None]
    view_of = _view_periods(min(dated_all), unit)
    period = SEASONS.get(fine_unit, 1)
    seasonal = settings.model != "auto_no_season" and period > 1
    custom = (
        Model(settings.trend or "none", settings.season or "none")
        if settings.model == "custom"
        else None
    )
    tail = (1 - settings.confidence) / 2
    appended: dict[int, list[tuple[Any, ...]]] = {}
    models: list[dict[str, Any]] = []
    too_short = 0
    for key, members in groups.items():
        dated = sorted(
            (i for i in members if rows[i][time_at] is not None), key=lambda i: rows[i][time_at]
        )
        found_rows = fine_groups.get(key, {})
        if not dated or len(found_rows) < 2:
            too_short += 1
            continue
        times = sorted(found_rows)
        timeline = _timeline(times[0], times[-1], fine_unit)
        trim = min(max(settings.ignore_last, 0), len(timeline) - 1)
        fitted_times = timeline[: len(timeline) - trim]
        ahead = (
            _horizon(settings, unit, rows[dated[-1]][time_at], AUTO_LENGTH)
            * SUB_COUNTS[(unit, fine_unit)]
        )
        future = [_step(timeline[-1], fine_unit, k) for k in range(1, ahead + 1)]
        estimated = [*timeline[len(timeline) - trim :], *future]
        horizon = len(estimated)
        owner = [view_of(t) for t in estimated]
        periods = list(dict.fromkeys(owner))
        positions = {v: [h for h, o in enumerate(owner) if o == v] for v in periods}
        skipped = set(estimated)
        estimates: list[list[float | None] | None] = []
        bands: list[tuple[list[float], list[float]] | None] = []
        for column, j in zip(plan.measures, fine_measures, strict=True):
            values = [_number(found_rows[t][j]) if t in found_rows else None for t in fitted_times]
            series = _fill(values, settings.fill_zeros)
            if series is None:
                estimates.append(None)
                bands.append(None)
                continue
            try:
                fitted = best_fit(series, period, seasonal, custom)
            except ForecastError as exc:
                _note(notes, f"預測（{plan.captions.get(column, column)}）：{exc}")
                estimates.append(None)
                bands.append(None)
                continue
            predicted = forecast(fitted, horizon, settings.confidence)
            paths = simulate(fitted, horizon)
            # 檢視的一期：其中實際的較細期（不是估計的期）的值，加上估計的較細期的估計值
            actual = dict.fromkeys(periods, 0.0)
            for t, row in found_rows.items():
                v = view_of(t)
                if t not in skipped and v in actual:
                    actual[v] += _number(row[j]) or 0.0
            totals = [actual[v] + sum(predicted.values[h] for h in positions[v]) for v in periods]
            simulated = np.stack(
                [actual[v] + paths[:, positions[v]].sum(axis=1) for v in periods], axis=1
            )
            low = [float(x) for x in np.quantile(simulated, tail, axis=0)]
            high = [float(x) for x in np.quantile(simulated, 1 - tail, axis=0)]
            estimates.append([float(v) for v in totals])
            bands.append((low, high))
            models.append(
                _model_details(key, column, fitted, predicted, horizon, fitted_times, estimated)
            )
        if all(e is None for e in estimates):
            too_short += 1
            continue
        index = {rows[i][time_at]: i for i in dated}
        new_rows = appended.setdefault(max(members), [])
        for h, v in enumerate(periods):
            i = index.get(v)
            if i is not None:
                result[i] = _estimate_row(
                    result[i], measures, estimates, bands, h, indicator_at, width, len(measures)
                )
                continue
            added: list[Any] = [None] * (width + len(extra))
            for s, j in zip(key, series_at, strict=True):
                added[j] = s
            added[time_at] = v
            new_rows.append(
                _estimate_row(
                    tuple(added), measures, estimates, bands, h, indicator_at, width, len(measures)
                )
            )
    ordered: list[tuple[Any, ...]] = []
    for i, row in enumerate(result):
        ordered.append(row)
        ordered.extend(appended.get(i, []))
    if too_short:
        which = "" if too_short == len(groups) else f"有 {too_short} 條時間序列"
        _note(notes, f"預測：{which}資料不到兩期，估計不出來")
    if details is not None:
        details["forecast"] = {
            "time": plan.time,
            "unit": fine_unit,
            "view_unit": unit,
            "series": plan.series,
            "ignore_last": max(settings.ignore_last, 0),
            "confidence": settings.confidence,
            "seasonal": seasonal,
            "models": models,
        }
    return [*columns, *extra], ordered


def _note(notes: list[str], text: str) -> None:
    """加上一則附註（已經有同樣的就不重複）。"""
    if text not in notes:
        notes.append(text)


def _model_details(
    key: tuple[Any, ...],
    column: str,
    found: Any,
    predicted: Any,
    horizon: int,
    fitted_times: list[Any],
    estimated: list[Any],
) -> dict[str, Any]:
    """描述預測中一條時間序列、一個量值的模型：選項、平滑係數、品質指標，與摘要（初始、相對於初始值的變化、
    季節影響、貢獻；見 tmig.planner.forecast.summary）。estimated 是估計的各期（忽略的最後幾期與之後的期）。"""
    from tmig.planner.forecast import summary

    return {
        "series": list(key),
        "measure": column,
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
        "until": estimated[-1] if estimated else fitted_times[-1],
        "initial_time": estimated[0] if estimated else None,
        **summary(found, predicted, horizon),
    }


def _estimate_row(
    row: tuple[Any, ...],
    measures: list[int],
    estimates: list[list[float | None] | None],
    bands: list[tuple[list[float], list[float]] | None],
    h: int,
    indicator_at: int | None,
    width: int,
    count: int,
) -> tuple[Any, ...]:
    values = list(row)
    for n, (j, predicted, band) in enumerate(zip(measures, estimates, bands, strict=True)):
        if predicted is None:
            continue
        values[j] = predicted[h]
        if band is not None:
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
