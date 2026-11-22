"""預測（Tableau 的「分析 → 預測」）：指數平滑。

與 Tableau 相同的做法（「How Forecasting Works in Tableau」）：
- 指數平滑的模型：趨勢與季節各為無、加法或乘法（乘法趨勢不搭配加法季節），自動時最多八個模型
  （三個沒有季節、五個有季節），平滑參數以最小平方誤差最佳化，初始值依慣用的做法估計（不最佳化），
  以 AIC 選出最好的模型
- 季節長度依時間的細度：月 12、季 4、週 13、日 7、時 24；年與其他細度不估計季節
- 有趨勢的模型至少要 5 個值，有季節的模型至少要兩個完整的季節（或一個季節加 5 個值）
- 乘法的模型：值有 0 或負數時不能使用
- 忽略最後幾期（預設 1）：不用來估計模型，改以預測值呈現
- 預測區間：加法的模型以公式計算，乘法的模型以模擬計算（固定的亂數種子）

Tableau 的初始值與最佳化細節沒有公開，估計值可能與 Tableau 有些微差異。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal

import numpy as np

Component = Literal["none", "additive", "multiplicative"]
SEASONS = {"month": 12, "quarter": 4, "week": 13, "day": 7, "hour": 24}
"""時間的細度 → 季節長度。"""
AUTO_LENGTH = {"year": 5, "quarter": 4, "month": 12, "week": 13, "day": 7, "hour": 24}
"""自動的預測長度（Tableau 依資料決定，沒有公開；這裡取一個季節，年為 5 年）。"""
COMPONENT_NAMES: dict[str, str] = {"none": "無", "additive": "加法", "multiplicative": "乘法"}
"""趨勢與季節的種類在預測選項（自訂模型）中的名稱。"""


@dataclass(frozen=True)
class Model:
    trend: Component
    season: Component

    @property
    def code(self) -> str:
        names = {"none": "N", "additive": "A", "multiplicative": "M"}
        return f"{names[self.trend]}{names[self.season]}"

    @property
    def name(self) -> str:
        """給使用者看的寫法：趨勢與季節在預測選項中的名稱（附註用，不寫模型的代號）。"""
        return f"趨勢：{COMPONENT_NAMES[self.trend]}、季節：{COMPONENT_NAMES[self.season]}"


NON_SEASONAL = [Model("none", "none"), Model("additive", "none"), Model("multiplicative", "none")]
SEASONAL = [
    Model("none", "additive"),
    Model("additive", "additive"),
    Model("none", "multiplicative"),
    Model("additive", "multiplicative"),
    Model("multiplicative", "multiplicative"),
]


@dataclass
class Fit:
    model: Model
    params: tuple[float, float, float]
    """alpha（水準）、beta（趨勢）、gamma（季節）。"""
    level: float
    trend: float
    seasons: list[float]
    sse: float
    n: int
    aic: float
    period: int
    mae: float | None = None
    """平均絕對誤差（各期的一步預測誤差）。"""
    mase: float | None = None
    """平均絕對誤差 ÷ 單純預測（下一期等於這一期）的平均絕對誤差；資料沒有變動時為 None。"""
    mape: float | None = None
    """平均絕對百分比誤差（百分比）；資料中有 0 時沒有定義，為 None。"""
    seasonal_parts: list[float] | None = None
    """各期的一步預測中季節的成分（與數值同單位：加法季節是季節指數，乘法季節是去掉季節的值 ×（指數 − 1））；
    沒有季節的模型為 None。描述預測的「季節影響」用。"""

    @property
    def quality(self) -> Literal["good", "ok", "poor"] | None:
        """預測的品質（Tableau「描述預測」的 Quality）：與單純預測相比，誤差不到一半為 good、較小為 ok、
        較大為 poor。"""
        if self.mase is None:
            return None
        return "good" if self.mase < 0.5 else "ok" if self.mase < 1 else "poor"

    @property
    def quality_score(self) -> float | None:
        """預測結果「品質」的值：100 × MAX(1 − MASE, 0)（Tableau 的「Forecast Field Results」），0 最差、
        100 最好；資料沒有變動（MASE 沒有定義）時為 None。"""
        if self.mase is None:
            return None
        return 100 * max(1 - self.mase, 0.0)


@dataclass
class ForecastResult:
    values: list[float]
    low: list[float]
    high: list[float]
    model: Model


class ForecastError(ValueError):
    """資料不足或不適合預測。"""


def _combine(level: float, trend: float, model: Model, steps: int = 1) -> float:
    if model.trend == "additive":
        return level + steps * trend
    if model.trend == "multiplicative":
        return level * trend**steps
    return level


def _initial(y: np.ndarray, model: Model, m: int) -> tuple[float, float, list[float]]:
    """慣用的初始值（不最佳化）：前幾期的直線趨勢（有季節時取前幾個完整的季節）；季節指數為去掉趨勢後
    各期的平均。水準與趨勢是第一個值之前一期的狀態。"""
    n = len(y)
    if model.season == "none":
        if model.trend == "none":
            return float(y[0]), 0.0, []
        k = min(n, 10)
        slope, intercept = (float(v) for v in np.polyfit(np.arange(k), y[:k], 1))
        if model.trend == "additive":
            return intercept - slope, slope, []
        ratio = 1 + slope / intercept if intercept > 0 and intercept + slope > 0 else 1.0
        return intercept / ratio, ratio, []
    k = max(min(n // m, 3), 1) * m
    t = np.arange(k)
    slope, intercept = (float(v) for v in np.polyfit(t, y[:k], 1))
    line = intercept + slope * t
    if model.season == "additive":
        detrended = y[:k] - line
        seasons = np.array([np.mean(detrended[j::m]) for j in range(m)])
        seasons -= seasons.mean()
    else:
        if (line <= 0).any():
            raise ForecastError("乘法的季節需要正的趨勢")
        detrended = y[:k] / line
        seasons = np.array([np.mean(detrended[j::m]) for j in range(m)])
        seasons /= seasons.mean()
    if model.trend == "additive":
        return intercept - slope, slope, [float(v) for v in seasons]
    if model.trend == "multiplicative":
        ratio = 1 + slope / intercept if intercept > 0 and intercept + slope > 0 else 1.0
        return intercept / ratio, ratio, [float(v) for v in seasons]
    return float(np.mean(line)), 0.0, [float(v) for v in seasons]


def _run(
    y: np.ndarray,
    model: Model,
    m: int,
    params: tuple[float, float, float],
    errors: list[float] | None = None,
    parts: list[float] | None = None,
) -> tuple[float, float, float, list[float]]:
    """平滑：回傳最小平方誤差與最後的水準、趨勢、季節；errors 有給時另外記下各期的一步預測誤差，
    parts 有給時記下各期一步預測中季節的成分（一步預測 − 去掉季節的部分）。"""
    alpha, beta, gamma = params
    level, trend, seasons = _initial(y, model, m)
    seasons = list(seasons)
    sse = 0.0
    for t, value in enumerate(y):
        base = _combine(level, trend, model)
        if model.season == "additive":
            season = seasons[t % m]
            fitted = base + season
        elif model.season == "multiplicative":
            season = seasons[t % m]
            fitted = base * season
        else:
            season = 0.0
            fitted = base
        error = float(value) - fitted
        sse += error * error
        if errors is not None:
            errors.append(error)
        if parts is not None:
            parts.append(float(fitted - base))
        if model.season == "additive":
            new_level = alpha * (value - season) + (1 - alpha) * base
        elif model.season == "multiplicative":
            new_level = alpha * (value / season) + (1 - alpha) * base if season else base
        else:
            new_level = alpha * value + (1 - alpha) * base
        if model.trend == "additive":
            trend = beta * (new_level - level) + (1 - beta) * trend
        elif model.trend == "multiplicative":
            ratio = new_level / level if level else 1.0
            trend = beta * ratio + (1 - beta) * trend
        if model.season == "additive":
            seasons[t % m] = gamma * (value - base) + (1 - gamma) * season
        elif model.season == "multiplicative":
            seasons[t % m] = gamma * (value / base) + (1 - gamma) * season if base else season
        level = float(new_level)
        if not (math.isfinite(level) and math.isfinite(trend)):
            return math.inf, level, trend, seasons
    return sse, level, trend, seasons


def _nelder_mead(function, start: list[float], steps: int = 400) -> list[float]:
    """Nelder–Mead（沒有外部的最佳化套件）。"""
    dim = len(start)
    simplex = [list(start)]
    for i in range(dim):
        point = list(start)
        point[i] += 0.5
        simplex.append(point)
    values = [function(p) for p in simplex]
    for _ in range(steps):
        order = sorted(range(dim + 1), key=lambda i: values[i])
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        if abs(values[-1] - values[0]) < 1e-10 * (abs(values[0]) + 1e-10):
            break
        centroid = [sum(p[i] for p in simplex[:-1]) / dim for i in range(dim)]
        worst = simplex[-1]
        reflected = [c + (c - w) for c, w in zip(centroid, worst, strict=True)]
        value = function(reflected)
        if value < values[0]:
            expanded = [c + 2 * (c - w) for c, w in zip(centroid, worst, strict=True)]
            expanded_value = function(expanded)
            if expanded_value < value:
                simplex[-1], values[-1] = expanded, expanded_value
            else:
                simplex[-1], values[-1] = reflected, value
        elif value < values[-2]:
            simplex[-1], values[-1] = reflected, value
        else:
            contracted = [c + 0.5 * (w - c) for c, w in zip(centroid, worst, strict=True)]
            contracted_value = function(contracted)
            if contracted_value < values[-1]:
                simplex[-1], values[-1] = contracted, contracted_value
            else:
                best = simplex[0]
                simplex = [best] + [
                    [b + 0.5 * (p - b) for b, p in zip(best, point, strict=True)]
                    for point in simplex[1:]
                ]
                values = [values[0]] + [function(p) for p in simplex[1:]]
    return simplex[min(range(dim + 1), key=lambda i: values[i])]


def _squash(value: float) -> float:
    return 1 / (1 + math.exp(-max(min(value, 30.0), -30.0)))


def fit(y: list[float], model: Model, m: int) -> Fit | None:
    data = np.asarray(y, dtype=float)
    n = len(data)
    if model.trend != "none" and n < 5:
        return None
    if model.season != "none" and (m < 2 or n < min(2 * m, m + 5)):
        return None
    multiplicative = "multiplicative" in (model.trend, model.season)
    if multiplicative and (data <= 0).any():
        return None
    count = 1 + (model.trend != "none") + (model.season != "none")

    def params_of(raw: list[float]) -> tuple[float, float, float]:
        alpha = _squash(raw[0])
        beta = _squash(raw[1]) if model.trend != "none" else 0.0
        gamma = _squash(raw[2]) * (1 - alpha) if model.season != "none" else 0.0
        return alpha, beta, gamma

    def objective(raw: list[float]) -> float:
        sse = _run(data, model, m, params_of(raw))[0]
        return sse if math.isfinite(sse) else 1e300

    try:
        _initial(data, model, m)
    except ForecastError:
        return None
    raw = _nelder_mead(objective, [0.0, -1.0, -1.0])
    params = params_of(raw)
    errors: list[float] = []
    parts: list[float] = []
    sse, level, trend, seasons = _run(data, model, m, params, errors, parts)
    if not math.isfinite(sse):
        return None
    variance = max(sse / n, 1e-300)
    aic = n * math.log(variance) + 2 * (count + 1)
    mae, mase, mape = _quality(data, errors)
    seasonal = parts if model.season != "none" else None
    return Fit(model, params, level, trend, seasons, sse, n, aic, m, mae, mase, mape, seasonal)


def _quality(y: np.ndarray, errors: list[float]) -> tuple[float | None, float | None, float | None]:
    """品質指標（Tableau「描述預測」的 MAE、MASE、MAPE）：誤差是各期的一步預測誤差。
    MASE 的分母是單純預測（下一期的值等於這一期）在同一段資料上的平均絕對誤差；MAPE 是誤差占實際值的
    百分比的平均，資料中有 0 時沒有定義。"""
    if len(errors) != len(y) or len(y) == 0:
        return None, None, None
    absolute = np.abs(np.asarray(errors, dtype=float))
    mae = float(absolute.mean())
    naive = float(np.abs(np.diff(y)).mean()) if len(y) > 1 else 0.0
    mase = mae / naive if naive > 0 else None
    mape = float((absolute / np.abs(y)).mean() * 100) if (y != 0).all() else None
    return mae, mase, mape


def candidates(y: list[float], period: int, seasonal: bool) -> list[Fit]:
    models = list(NON_SEASONAL)
    if seasonal and period >= 2:
        models += SEASONAL
    fits = [found for model in models if (found := fit(y, model, period)) is not None]
    return fits


def best_fit(y: list[float], period: int, seasonal: bool = True, model: Model | None = None) -> Fit:
    """以 AIC 選出最好的模型；指定模型時只估計那個模型。"""
    if model is not None:
        found = fit(y, model, period)
        if found is None:
            raise ForecastError(f"資料不足以估計自訂的模型（{model.name}）")
        return found
    fits = candidates(y, period, seasonal)
    if not fits:
        raise ForecastError("資料不足以預測")
    return min(fits, key=lambda f: f.aic)


def _point(found: Fit, steps: int) -> float:
    base = _combine(found.level, found.trend, found.model, steps)
    if found.model.season == "none":
        return base
    season = found.seasons[(found.n + steps - 1) % found.period]
    return base + season if found.model.season == "additive" else base * season


def forecast(found: Fit, horizon: int, confidence: float = 0.95) -> ForecastResult:
    values = [float(_point(found, h)) for h in range(1, horizon + 1)]
    sigma = math.sqrt(found.sse / max(found.n, 1))
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    alpha, beta, gamma = found.params
    multiplicative = "multiplicative" in (found.model.trend, found.model.season)
    low: list[float] = []
    high: list[float] = []
    if not multiplicative:
        for h in range(1, horizon + 1):
            total = 1.0
            for j in range(1, h):
                c = alpha
                if found.model.trend == "additive":
                    c += alpha * beta * j
                if found.model.season != "none" and j % found.period == 0:
                    c += gamma
                total += c * c
            width = z * sigma * math.sqrt(total)
            low.append(float(values[h - 1] - width))
            high.append(float(values[h - 1] + width))
        return ForecastResult(values, low, high, found.model)
    # 乘法的模型：模擬（固定的亂數種子，結果每次相同）
    rng = np.random.default_rng(20211)
    paths = np.zeros((2000, horizon))
    for p in range(paths.shape[0]):
        level, trend, seasons = found.level, found.trend, list(found.seasons)
        for h in range(horizon):
            base = _combine(level, trend, found.model)
            t = found.n + h
            season = seasons[t % found.period] if found.model.season != "none" else 0.0
            if found.model.season == "multiplicative":
                mean = base * season
            elif found.model.season == "additive":
                mean = base + season
            else:
                mean = base
            value = mean + rng.normal(0.0, sigma)
            paths[p, h] = value
            if found.model.season == "multiplicative":
                new_level = alpha * (value / season) + (1 - alpha) * base if season else base
            elif found.model.season == "additive":
                new_level = alpha * (value - season) + (1 - alpha) * base
            else:
                new_level = alpha * value + (1 - alpha) * base
            if found.model.trend == "additive":
                trend = beta * (new_level - level) + (1 - beta) * trend
            elif found.model.trend == "multiplicative":
                trend = beta * (new_level / level if level else 1.0) + (1 - beta) * trend
            if found.model.season == "multiplicative" and base:
                seasons[t % found.period] = gamma * (value / base) + (1 - gamma) * season
            elif found.model.season == "additive":
                seasons[t % found.period] = gamma * (value - base) + (1 - gamma) * season
            level = new_level
    tail = (1 - confidence) / 2
    low = [float(v) for v in np.quantile(paths, tail, axis=0)]
    high = [float(v) for v in np.quantile(paths, 1 - tail, axis=0)]
    return ForecastResult(values, low, high, found.model)


# ---------------------------------------------------------------- 預測結果與描述預測的摘要


def trend_values(found: Fit, horizon: int) -> list[float]:
    """預測結果「趨勢」：去掉季節成分的估計值，即水準與趨勢的組合（沒有季節的模型與估計值相同）。"""
    return [
        float(_combine(found.level, found.trend, found.model, h)) for h in range(1, horizon + 1)
    ]


def precisions(predicted: ForecastResult) -> list[float]:
    """預測結果「精度」：預測區間與估計值的距離。加法的模型上下對稱，即區間的一半；乘法的模型以模擬計算，
    上下不一定對稱，也取區間寬度的一半（推定：Tableau 沒有說明不對稱時的算法）。"""
    return [(hi - lo) / 2 for lo, hi in zip(predicted.low, predicted.high, strict=True)]


ResultKind = Literal["trend", "precision", "precision_percent", "quality", "upper", "lower"]


def result_values(
    kind: ResultKind | None, found: Fit, predicted: ForecastResult, horizon: int
) -> list[float | None]:
    """預測的量值在估計的各期顯示的值（Tableau 的預測結果，見「Forecast Field Results」）：
    None 為實際值與預測值（估計值）；trend 去掉季節成分；precision 預測區間與估計值的距離；
    precision_percent 精度 ÷ 估計值（以百分比顯示；估計值為 0 時沒有定義）；quality 為
    100 × MAX(1 − MASE, 0)，每一期相同；upper、lower 為預測區間的上下界。"""
    if kind is None:
        return list(predicted.values)
    if kind == "trend":
        return list(trend_values(found, horizon))
    if kind == "upper":
        return list(predicted.high)
    if kind == "lower":
        return list(predicted.low)
    if kind == "quality":
        return [found.quality_score] * horizon
    spread = precisions(predicted)
    if kind == "precision":
        return list(spread)
    return [p / abs(v) if v else None for p, v in zip(spread, predicted.values, strict=True)]


def summary(found: Fit, predicted: ForecastResult, horizon: int) -> dict[str, float | None]:
    """描述預測的摘要（Tableau「Forecast Descriptions」的 Summary）：

    - 初始：第一個估計的期的估計值與精度（「值 ± 精度」）
    - 相對於初始值的變化：最後一個與第一個估計值的差
    - 季節影響的高值、低值（有季節的模型）：實際值與估計值合起來的序列中最後一個完整季節（最後 m 期，
      推定）的季節成分的最大、最小值；季節成分是與趨勢的差（與數值同單位，加總約為 0）。實際值的期用一步
      預測的季節成分
    - 貢獻：趨勢與季節占估計值變化的比例（百分比，加起來是 100%）。Tableau 沒有公開算法，這裡取估計的各期中
      去掉季節的部分與季節成分各自的變異數占兩者合計的比例（推定）；沒有季節時趨勢 100%，兩者都沒有變化時
      也是趨勢 100%
    """
    values = predicted.values[:horizon]
    spread = precisions(predicted)
    trend = trend_values(found, horizon)
    result: dict[str, float | None] = {
        "initial": values[0] if values else None,
        "initial_precision": spread[0] if spread else None,
        "change": values[-1] - values[0] if values else None,
        "seasonal_high": None,
        "seasonal_low": None,
        "contribution_trend": 100.0,
        "contribution_season": 0.0,
    }
    if found.model.season == "none" or not values:
        return result
    future = [v - t for v, t in zip(values, trend, strict=True)]
    combined = [*(found.seasonal_parts or []), *future]
    last = combined[-found.period :]
    if last:
        result["seasonal_high"] = max(last)
        result["seasonal_low"] = min(last)
    trend_var = float(np.var(trend)) if len(trend) > 1 else 0.0
    season_var = float(np.var(future)) if len(future) > 1 else 0.0
    if trend_var + season_var > 0:
        result["contribution_trend"] = 100 * trend_var / (trend_var + season_var)
        result["contribution_season"] = 100 * season_var / (trend_var + season_var)
    return result
