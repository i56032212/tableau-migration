"""預測模型函式（MODEL_QUANTILE、MODEL_PERCENTILE）：查詢之後在檢視的標記上計算（與叢集、預測相同，見
tmig.planner.analytics）。

Tableau 2021.2 說明文件寫明、這裡照做的（2026-11-23 查閱）：

- 「How Predictive Modeling Functions Work in Tableau」
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/predictions_overview.htm ）：模型的資料是每個標記一列、
  每個標記的權重相同（不依標記包含的資料列數加權）；維度要以 ATTR 當作預測變數，標記跨多個值時是「*」，
  「*」是一個獨立的類別，所有標記都是「*」時忽略這個預測變數；每個分割至少要有 3 個標記，否則沒有結果
  （predictions_resolve_errors.htm：Null）；預測變數與標記一對一（例如以 ATTR([State]) 預測、檢視只有 State）
  時是錯誤
- 「Compute Using and Data Partitioning in Predictive Modeling」（predictions_compute_using.htm）：沒有計算的方向，
  「計算依據」只用來分割；每個分割各自建立模型
- 「Choosing a Predictive Model」（predictions_choosing_model.htm）：預設的線性迴歸是最小平方法（OLS）
- 函式說明（functions_functions_predictivemodeling.htm）：MODEL_QUANTILE 是後驗預測分配的分位數、
  MODEL_PERCENTILE 是觀察值在後驗預測分配中的累積機率（CDF）
- 重複的預測變數（彼此相同、成比例或互補，例如 IsRain 與 IsClear）是錯誤（predictions_resolve_errors.htm）

文件沒有寫、這裡推定的（README「已知限制與待驗證」）：後驗預測分配以無資訊的先驗（p(β, σ²) ∝ 1/σ²）
計算——自由度 n − p 的 Student t 分配，中心是 OLS 的預測值、尺度是 s·√(1 + x(XᵀX)⁻¹xᵀ)（與傳統的預測區間
相同；分位數 0.5 一律是 OLS 的預測值，與先驗無關）；目標是 Null 的標記不進模型、仍然預測（「Predicting the
Future」的例子以模型預測沒有資料的未來月份）；量值的預測變數是 Null 的標記不進模型、結果是 Null；維度的 Null
與「*」是同一個類別；只在沒有目標的標記上出現的類別預測不了（Null）；所有標記的殘差都是 0 時分配退化成
預測值一點。重複的預測變數以矩陣的秩判斷（常數的量值也算），自由度不到 1 也是錯誤。
"""

from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

MIN_MARKS = 3
"""每個分割至少要有的標記數（Tableau 說明文件）。"""


class ModelError(ValueError):
    """Tableau 會顯示錯誤的情況（重複的預測變數、預測變數與標記一對一）：整張工作表列為不支援。"""


@dataclass
class ModelPredictor:
    column: str
    """查詢結果中預測變數的欄名（另外輸出的隱藏欄）。"""
    kind: Literal["measure", "category"]
    square: bool = False
    """augmentation=on 的量值：加上 2 次項（2 次的 Hermite 多項式與 1、x、x² 張成相同的空間，OLS 的結果
    與多項式的尺度無關）。"""


@dataclass
class ModelPlan:
    column: str
    """MODEL_* 量值的輸出欄名（查詢時是 Null，在這裡填上）。"""
    function: Literal["MODEL_QUANTILE", "MODEL_PERCENTILE"]
    target: str
    """目標的輸出欄名（隱藏欄）。"""
    predictors: list[ModelPredictor]
    partition: list[str] = field(default_factory=list)
    """分割的維度（不在計算依據中的維度）的輸出欄名。"""
    quantile: float | None = None
    caption: str = ""
    """膠囊上的寫法（錯誤訊息用）。"""


# ---------------------------------------------------------------- Student t 分配


def _beta_fraction(a: float, b: float, x: float) -> float:
    """不完全 Beta 函數的連分數（Lentz 法）。"""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return h


def beta_inc(a: float, b: float, x: float) -> float:
    """正規化的不完全 Beta 函數 I_x(a, b)。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_fraction(a, b, x) / a
    return 1.0 - front * _beta_fraction(b, a, 1.0 - x) / b


def t_cdf(t: float, dof: float) -> float:
    """自由度 dof 的 Student t 分配的累積機率。"""
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    tail = 0.5 * beta_inc(dof / 2.0, 0.5, dof / (dof + t * t))
    return 1.0 - tail if t > 0 else tail


def _t_pdf(t: float, dof: float) -> float:
    log = (
        math.lgamma((dof + 1) / 2)
        - math.lgamma(dof / 2)
        - 0.5 * math.log(dof * math.pi)
        - (dof + 1) / 2 * math.log1p(t * t / dof)
    )
    return math.exp(log)


def t_ppf(q: float, dof: float) -> float:
    """Student t 分配的分位數（0 < q < 1）：二分法夾出、牛頓法收斂。"""
    if not 0 < q < 1:
        raise ValueError("分位數必須在 0 與 1 之間")
    if q == 0.5:
        return 0.0
    if q < 0.5:
        return -t_ppf(1.0 - q, dof)
    low, high = 0.0, 1.0
    while t_cdf(high, dof) < q:
        low, high = high, high * 2.0
        if high > 1e300:
            return math.inf
    for _ in range(200):
        middle = (low + high) / 2.0
        if t_cdf(middle, dof) < q:
            low = middle
        else:
            high = middle
        if high - low <= 1e-15 * max(1.0, high):
            break
    t = (low + high) / 2.0
    for _ in range(3):
        density = _t_pdf(t, dof)
        if density <= 0:
            break
        step = (t_cdf(t, dof) - q) / density
        t -= step
        if abs(step) <= 1e-16 * max(1.0, abs(t)):
            break
    return t


# ---------------------------------------------------------------- 線性迴歸


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _category(value: Any) -> Hashable:
    """類別的鍵：Null（與「*」）是同一個類別。"""
    return ("value", value) if value is not None else ("null",)


def _sort_key(key: Hashable) -> tuple[int, str, str]:
    return (0, type(key).__name__, repr(key))


def predict(plan: ModelPlan, columns: list[str], rows: list[tuple[Any, ...]]) -> list[float | None]:
    """一個分割的標記（rows）各自的結果；標記少於 3 個時都是 None。重複的預測變數丟出 ModelError。"""
    y_at = columns.index(plan.target)
    at = [columns.index(p.column) for p in plan.predictors]
    targets = [_number(r[y_at]) for r in rows]

    def measures_ok(row: tuple[Any, ...]) -> bool:
        return all(
            _number(row[j]) is not None
            for j, p in zip(at, plan.predictors, strict=True)
            if p.kind == "measure"
        )

    train = [i for i, row in enumerate(rows) if targets[i] is not None and measures_ok(row)]
    if len(train) < MIN_MARKS:
        return [None] * len(rows)
    levels: list[list[Hashable]] = []
    for j, p in zip(at, plan.predictors, strict=True):
        if p.kind == "category":
            seen = {_category(rows[i][j]) for i in train}
            levels.append(sorted(seen, key=_sort_key))
        else:
            levels.append([])

    def design(row: tuple[Any, ...]) -> list[float] | None:
        x = [1.0]
        for j, p, known in zip(at, plan.predictors, levels, strict=True):
            if p.kind == "measure":
                value = _number(row[j])
                if value is None:
                    return None
                x.append(value)
                if p.square:
                    x.append(value * value)
                continue
            key = _category(row[j])
            if key not in known:
                return None
            # 第一個類別是基準（OLS 的預測與基準的選擇無關）
            x.extend(1.0 if key == other else 0.0 for other in known[1:])
        return x

    matrix = np.array([design(rows[i]) for i in train], dtype=float)
    y = np.array([targets[i] for i in train], dtype=float)
    n, p = matrix.shape
    norms = np.linalg.norm(matrix, axis=0)
    if (norms == 0).any() or np.linalg.matrix_rank(matrix / norms) < p:
        raise ModelError(
            f"{plan.caption}：預測變數彼此重複（相同、成比例、互補或是常數），Tableau 會顯示錯誤"
        )
    dof = n - p
    if dof < 1:
        raise ModelError(
            f"{plan.caption}：預測變數與標記一對一（標記數不多於模型的參數），Tableau 會顯示錯誤"
        )
    q, r = np.linalg.qr(matrix / norms)
    beta = np.linalg.solve(r, q.T @ y) / norms
    residuals = y - matrix @ beta
    variance = float(residuals @ residuals) / dof
    spread = 0.0 if plan.quantile is None else t_ppf(plan.quantile, dof)
    r_inv = np.linalg.inv(r)
    results: list[float | None] = []
    for i, row in enumerate(rows):
        x = design(row)
        if x is None:
            results.append(None)
            continue
        vector = np.array(x, dtype=float)
        center = float(vector @ beta)
        leverage = float(np.sum(((vector / norms) @ r_inv) ** 2))
        scale = math.sqrt(max(variance * (1.0 + leverage), 0.0))
        if plan.function == "MODEL_QUANTILE":
            results.append(center + spread * scale if scale > 0 else center)
            continue
        observed = targets[i]
        if observed is None:
            results.append(None)
        elif scale > 0:
            results.append(t_cdf((observed - center) / scale, dof))
        else:
            results.append(1.0 if observed >= center else 0.0)
    return results


def apply_models(
    plans: list[ModelPlan], columns: list[str], rows: list[tuple[Any, ...]]
) -> list[tuple[Any, ...]]:
    """依分割（不在計算依據中的維度）各自建立模型，結果填進 MODEL_* 的欄。"""
    for plan in plans:
        at = columns.index(plan.column)
        keys = [columns.index(c) for c in plan.partition]
        groups: dict[tuple[Hashable, ...], list[int]] = {}
        for i, row in enumerate(rows):
            groups.setdefault(tuple(_category(row[k]) for k in keys), []).append(i)
        values: list[float | None] = [None] * len(rows)
        for members in groups.values():
            found = predict(plan, columns, [rows[i] for i in members])
            for i, value in zip(members, found, strict=True):
                values[i] = value
        rows = [(*row[:at], values[i], *row[at + 1 :]) for i, row in enumerate(rows)]
    return rows


__all__ = [
    "MIN_MARKS",
    "ModelError",
    "ModelPlan",
    "ModelPredictor",
    "apply_models",
    "beta_inc",
    "predict",
    "t_cdf",
    "t_ppf",
]
