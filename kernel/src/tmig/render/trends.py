"""趨勢線的模型：與 Tableau 相同，以（轉換後的）最小平方法配適，並算出 R 平方、P 值與 95% 信賴區間。

Tableau 的五種模型（依 Tableau 文件「趨勢線模型類型」）：

- 線性：Y = b1·X + b0
- 對數：Y = b1·ln(X) + b0，X ≤ 0 的標記不納入
- 指數：Y = b0·e^(b1·X)，以 ln(Y) 配適；Y ≤ 0 的標記不納入（全部為負時以 −Y 配適，依公開資料推定）
- 乘冪：Y = b0·X^b1，以 ln(Y) 對 ln(X) 配適；X ≤ 0、Y ≤ 0 的標記不納入
- 多項式：Y = b0 + b1·X + … + bn·X^n（n 為 2–8）

「通過原點」（exclude-intercept）時沒有 b0（指數與乘冪為 b0 = 1）。R 平方與 P 值在轉換後的空間計算：
P 值為模型與只有常數的模型比較的 F 檢定（通過原點時與 0 比較，R 平方也以未減平均的平方和計算）。
信賴區間為平均值的 95% 信賴區間（t 分配）；指數模型與 Tableau 相同沒有信賴區間。

不依賴 numpy：最小平方以 Householder QR 求解（自變數先平移、縮放到 [−1, 1]，日期與高次多項式也穩定），
F 與 t 分配以正規化不完全 Beta 函數計算。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Fit = Literal["linear", "log", "exp", "power", "polynomial"]

LOG_X = ("log", "power")
"""自變數取自然對數的模型。"""
LOG_Y = ("exp", "power")
"""應變數取自然對數的模型。"""


@dataclass(frozen=True)
class Model:
    """配適好的模型。terms 為公式中的係數（索引為 X 的次方；指數與乘冪為 ln(b0)、b1）。"""

    fit: Fit
    intercept: bool
    n: int
    low: float
    """納入計算的標記中 X 的最小值（線畫在 low 到 high 之間）。"""
    high: float
    r2: float | None
    p_value: float | None
    terms: tuple[float, ...]
    _beta: tuple[float, ...]
    _center: float
    _scale: float
    _r: tuple[tuple[float, ...], ...]
    _sigma: float | None
    _t: float | None
    _sign: float

    @property
    def sigma(self) -> float | None:
        """殘差的標準誤（轉換後的空間）；殘差自由度為 0 時為 None。"""
        return self._sigma

    def predict(self, x: float) -> float | None:
        """X 對應的趨勢值；X 不在模型的定義域（對數、乘冪的 X ≤ 0）時為 None。"""
        v = self._linear(x)
        return None if v is None else self._back(v)

    def bounds(self, x: float) -> tuple[float, float] | None:
        """X 對應的 95% 信賴區間（下界、上界）；指數模型、自由度為 0 時為 None。"""
        if self.fit == "exp" or self._t is None or self._sigma is None:
            return None
        u = _transform_x(x, self.fit)
        if u is None:
            return None
        basis = self._basis(u)
        v = math.fsum(b * c for b, c in zip(basis, self._beta, strict=True))
        # 平均值的標準誤：s·√(bᵀ(XᵀX)⁻¹b) = s·‖R⁻ᵀb‖
        w: list[float] = []
        for i, value in enumerate(basis):
            w.append((value - math.fsum(self._r[j][i] * w[j] for j in range(i))) / self._r[i][i])
        margin = self._t * self._sigma * math.sqrt(math.fsum(x * x for x in w))
        low, high = self._back(v - margin), self._back(v + margin)
        return (low, high) if low <= high else (high, low)

    def equation(self, y: str, x: str) -> str:
        """停在線上時顯示的公式（與 Tableau 的工具提示相同的寫法，係數取 6 位有效數字）。"""
        t = self.terms
        if self.fit == "polynomial":
            parts = [f"{_number(t[k])}*{x}^{k}" for k in range(len(t) - 1, 1, -1)]
            parts.append(f"{_number(t[1])}*{x}")
            if self.intercept:
                parts.append(_number(t[0]))
            return f"{y} = " + " + ".join(parts)
        b0, b1 = t[0], t[1]
        constant = f" + {_number(b0)}" if self.intercept else ""
        if self.fit == "linear":
            return f"{y} = {_number(b1)}*{x}{constant}"
        if self.fit == "log":
            return f"{y} = {_number(b1)}*ln({x}){constant}"
        factor = self._sign * (math.exp(b0) if self.intercept else 1.0)
        if self.fit == "exp":
            return f"{y} = {_number(factor)}*exp({_number(b1)}*{x})"
        return f"{y} = {_number(factor)}*{x}^{_number(b1)}"

    def _basis(self, u: float) -> list[float]:
        z = (u - self._center) / self._scale
        order = len(self._beta) - (1 if self.intercept else 0)
        powers = [z**k for k in range(1, order + 1)]
        return [1.0, *powers] if self.intercept else powers

    def _linear(self, x: float) -> float | None:
        u = _transform_x(x, self.fit)
        if u is None:
            return None
        return math.fsum(b * c for b, c in zip(self._basis(u), self._beta, strict=True))

    def _back(self, v: float) -> float:
        if self.fit in LOG_Y:
            try:
                return self._sign * math.exp(v)
            except OverflowError:
                return self._sign * math.inf
        return v


def fit_model(
    points: Sequence[tuple[float, float]],
    fit: Fit,
    degree: int = 3,
    intercept: bool = True,
) -> Model | None:
    """以標記的 (X, Y) 配適模型；資料不足（點數少於參數個數、X 全部相同）時為 None。"""
    pairs = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    if fit in LOG_X:
        pairs = [(x, y) for x, y in pairs if x > 0]
    sign = 1.0
    if fit in LOG_Y:
        if pairs and all(y < 0 for _, y in pairs):
            sign = -1.0
            pairs = [(x, -y) for x, y in pairs]
        else:
            pairs = [(x, y) for x, y in pairs if y > 0]
    us = [math.log(x) if fit in LOG_X else x for x, _ in pairs]
    vs = [math.log(y) if fit in LOG_Y else y for _, y in pairs]
    order = degree if fit == "polynomial" else 1
    size = order + (1 if intercept else 0)
    n = len(us)
    if n == 0 or n < size:
        return None
    # 通過原點時不能平移（平移等於加上截距），只縮放
    center = statistics.fmean(us) if intercept else 0.0
    scale = max(abs(u - center) for u in us)
    if scale == 0:
        return None
    rows = [_powers((u - center) / scale, order, intercept) for u in us]
    solved = _least_squares(rows, vs)
    if solved is None:
        return None
    beta, r, sse = solved
    df = n - size
    if intercept:
        mean = statistics.fmean(vs)
        sst = math.fsum((v - mean) ** 2 for v in vs)
        df_model = size - 1
    else:
        sst = math.fsum(v * v for v in vs)
        df_model = size
    r2 = 1 - sse / sst if sst > 0 else None
    p_value: float | None = None
    if df > 0 and df_model > 0 and sst > 0:
        if sse <= sst * 1e-15:
            p_value = 0.0
        else:
            f = ((sst - sse) / df_model) / (sse / df)
            p_value = f_sf(f, df_model, df) if f > 0 else 1.0
    return Model(
        fit=fit,
        intercept=intercept,
        n=n,
        low=min(x for x, _ in pairs),
        high=max(x for x, _ in pairs),
        r2=r2,
        p_value=p_value,
        terms=_terms(beta, center, scale, order, intercept),
        _beta=tuple(beta),
        _center=center,
        _scale=scale,
        _r=tuple(tuple(row) for row in r),
        _sigma=math.sqrt(sse / df) if df > 0 else None,
        _t=t_quantile(0.975, df) if df > 0 else None,
        _sign=sign,
    )


def format_r2(value: float | None) -> str:
    return "" if value is None else _number(value)


def format_p(value: float | None) -> str:
    """P 值：小於 0.0001 時寫成「< 0.0001」（與 Tableau 相同）。"""
    if value is None:
        return ""
    return "< 0.0001" if value < 0.0001 else _number(value)


# ---------------------------------------------------------------- 最小平方


def _powers(z: float, order: int, intercept: bool) -> list[float]:
    powers = [z**k for k in range(1, order + 1)]
    return [1.0, *powers] if intercept else powers


def _least_squares(
    rows: list[list[float]], v: list[float]
) -> tuple[list[float], list[list[float]], float] | None:
    """X·β ≈ v 的最小平方解（Householder QR）：回傳 β、R（上三角）與殘差平方和；X 的秩不足時為 None。"""
    n, p = len(rows), len(rows[0])
    a = [list(row) for row in rows]
    b = list(v)
    for k in range(p):
        u = [a[i][k] for i in range(k, n)]
        norm = math.sqrt(math.fsum(c * c for c in u))
        if norm == 0.0:
            return None
        u[0] -= -norm if u[0] > 0 else norm
        length = math.fsum(c * c for c in u)
        if length == 0.0:
            continue
        for j in range(k, p):
            f = 2 * math.fsum(u[i] * a[k + i][j] for i in range(len(u))) / length
            if f:
                for i in range(len(u)):
                    a[k + i][j] -= f * u[i]
        f = 2 * math.fsum(u[i] * b[k + i] for i in range(len(u))) / length
        for i in range(len(u)):
            b[k + i] -= f * u[i]
    r = [[a[i][j] if j >= i else 0.0 for j in range(p)] for i in range(p)]
    biggest = max(abs(r[i][i]) for i in range(p))
    if biggest == 0.0 or any(abs(r[i][i]) <= 1e-10 * biggest for i in range(p)):
        return None
    beta = [0.0] * p
    for i in range(p - 1, -1, -1):
        rest = math.fsum(r[i][j] * beta[j] for j in range(i + 1, p))
        beta[i] = (b[i] - rest) / r[i][i]
    return beta, r, math.fsum(x * x for x in b[p:])


def _terms(
    beta: list[float], center: float, scale: float, order: int, intercept: bool
) -> tuple[float, ...]:
    """縮放後的係數 → 公式中 X（或 ln X）各次方的係數：z = (u − center) / scale 以二項式展開。"""
    coefficients = beta if intercept else [0.0, *beta]
    terms: list[float] = []
    for j in range(order + 1):
        terms.append(
            math.fsum(
                coefficients[k] * math.comb(k, j) * (-center) ** (k - j) / scale**k
                for k in range(j, order + 1)
            )
        )
    return tuple(terms)


def _transform_x(x: float, fit: Fit) -> float | None:
    if fit in LOG_X:
        return math.log(x) if x > 0 else None
    return x


def _number(value: float) -> str:
    """6 位有效數字（C 的 %g）。"""
    text = f"{value:.6g}"
    return "0" if text == "-0" else text


# ---------------------------------------------------------------- 分配


def betainc(a: float, b: float, x: float) -> float:
    """正規化不完全 Beta 函數 I_x(a, b)（連分數，Numerical Recipes 的 betacf）。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(a, b, x) / a
    return 1.0 - front * _beta_fraction(b, a, 1.0 - x) / b


def _beta_fraction(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        step = m * (b - m) * x / ((a - 1 + m2) * (a + m2))
        d = 1.0 + step * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + step / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        step = -(a + m) * (a + b + m) * x / ((a + m2) * (a + 1 + m2))
        d = 1.0 + step * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + step / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def f_sf(f: float, d1: float, d2: float) -> float:
    """F 分配的右尾機率 P(F > f)。"""
    if f <= 0:
        return 1.0
    return betainc(d2 / 2, d1 / 2, d2 / (d2 + d1 * f))


def t_two_sided(t: float, df: float) -> float:
    """t 分配的雙尾機率 P(|T| > t)。"""
    return betainc(df / 2, 0.5, df / (df + t * t))


def t_quantile(p: float, df: float) -> float:
    """t 分配的分位數（p > 0.5，例如 0.975）：以二分法解 P(|T| > t) = 2(1 − p)。"""
    target = 2 * (1 - p)
    low, high = 0.0, 1.0
    while t_two_sided(high, df) > target:
        high *= 2
    for _ in range(200):
        middle = (low + high) / 2
        if t_two_sided(middle, df) > target:
            low = middle
        else:
            high = middle
    return (low + high) / 2


__all__ = ["Fit", "Model", "betainc", "f_sf", "fit_model", "format_p", "format_r2", "t_quantile"]
