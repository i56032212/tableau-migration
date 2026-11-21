"""趨勢線：解析 <trendline> 與線條格式；依窗格與顏色分組配適模型（與 Tableau 相同的轉換與最小平方法），
畫在各組資料的範圍內，信賴區間為上下兩條線，工具提示為公式、R 平方與 P 值。

模型的數字以 numpy／scipy 的結果核對（見 test_models）。測試資料見 tests/sqlgen/test_query.py。
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tmig.ir import FieldUse, TrendLine
from tmig.render.trends import betainc, f_sf, fit_model, format_p, t_quantile, t_two_sided
from tmig.render.vegalite import main_encoding

from ..parser.builder import instance, ref, worksheet
from .test_formats import MONTH
from .test_text import PROFIT, REGION, SALES, db, parse, render
from .test_vegalite import validator

__all__ = ["db"]  # pytest fixture

TREND = (
    "<trendline degree='{degree}' enable-confidence-bands='{bands}' enable-instant-analytics='true' "
    "enabled='true' exclude-color='{exclude_color}' exclude-intercept='{exclude_intercept}' "
    "fit='{fit}'>{children}</trendline>"
)

# Anscombe 的第一組資料；預期值以 numpy.linalg.lstsq 與 scipy.stats 算出
X = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5]
Y = [8.04, 6.95, 7.58, 8.81, 8.33, 9.96, 7.24, 4.26, 10.84, 4.82, 5.68]


def trendline(
    fit: str = "linear",
    degree: int = 3,
    bands: bool = False,
    by_color: bool = True,
    intercept: bool = True,
    children: str = "",
) -> str:
    return TREND.format(
        fit=fit,
        degree=degree,
        bands=str(bands).lower(),
        exclude_color=str(not by_color).lower(),
        exclude_intercept=str(not intercept).lower(),
        children=children,
    )


ORDER = instance("[Order ID]", "None", "[none:Order ID:nk]")


def scatter(trend: str, style: str = "", color: bool = True) -> str:
    """每張訂單一個點（7 個）；依地區著色時東、西、南區各兩個點，北區一個點（畫不出線）。"""
    encodings = f"<lod column='{ref('none:Order ID:nk')}' />"
    if color:
        encodings += f"<color column='{ref('none:Region:nk')}' />"
    return worksheet(
        "Scatter",
        [REGION, ORDER, SALES, PROFIT],
        rows=ref("sum:Profit:qk"),
        cols=ref("sum:Sales:qk"),
        mark="Circle",
        encodings=encodings,
        pane_extra=trend + style,
    )


def layer(spec: dict[str, Any], name: str = "__ref_trend") -> dict[str, Any]:
    return next(item for item in spec["layer"] if item.get("name") == name)


def days(text: str) -> float:
    """日期（ISO 8601）→ 1899-12-30 起的天數（趨勢線在時間軸上配適時的橫軸值）。"""
    return (datetime.fromisoformat(text) - datetime(1899, 12, 30)).total_seconds() / 86400


def test_models() -> None:
    """五種模型、通過原點與信賴區間：與 numpy、scipy 的結果相同。"""
    points = list(zip(X, Y, strict=True))
    linear = fit_model(points, "linear")
    assert linear is not None
    assert linear.terms == pytest.approx((3.00009091, 0.50009091))
    assert (linear.r2, linear.p_value) == pytest.approx((0.666542459508775, 0.002169628873078794))
    assert linear.bounds(9) == pytest.approx((6.657463949878938, 8.34435423193924))
    assert linear.equation("Y", "X") == "Y = 0.500091*X + 3.00009"
    poly = fit_model(points, "polynomial", 2)
    assert poly is not None and poly.terms == pytest.approx((0.7550676, 1.06925175, -0.03162005))
    assert (poly.r2, poly.p_value) == pytest.approx((0.6873274348982257, 0.009557825694852478))
    assert poly.equation("Y", "X") == "Y = -0.03162*X^2 + 1.06925*X + 0.755068"
    origin = fit_model(points, "linear", intercept=False)
    assert origin is not None and origin.terms == pytest.approx((0.0, 0.7968032))
    assert (origin.r2, origin.p_value) == pytest.approx((0.9626726912067551, 1.811759884958491e-08))
    assert origin.equation("Y", "X") == "Y = 0.796803*X"
    log = fit_model(points, "log")
    assert log is not None and log.terms == pytest.approx((-1.29257387, 4.13378366))
    assert log.equation("Y", "X") == "Y = 4.13378*ln(X) + -1.29257"
    exp = fit_model(points, "exp")
    assert exp is not None and exp.terms == pytest.approx((1.33622755, 0.07137745))
    assert (exp.r2, exp.p_value) == pytest.approx((0.6645816878182201, 0.0022301540097202453))
    assert exp.bounds(9) is None  # 指數模型沒有信賴區間
    assert exp.equation("Y", "X") == "Y = 3.80466*exp(0.0713774*X)"
    power = fit_model(points, "power")
    assert power is not None and power.terms == pytest.approx((0.70358786, 0.59939003))
    assert power.bounds(9) == pytest.approx((6.722033238608663, 8.463595141177663))
    assert power.equation("Y", "X") == "Y = 2.02099*X^0.59939"
    # 對數、乘冪不納入 X ≤ 0 的標記；X 全部相同、點數不足時沒有模型
    shifted = fit_model([(0, 1.0), (-1, 2.0), *points], "log")
    assert shifted is not None and shifted.terms == pytest.approx(log.terms)
    assert fit_model([(1, 1.0), (1, 2.0)], "linear") is None
    assert fit_model([(1, 1.0), (2, 2.0)], "polynomial", 2) is None
    # 兩點：線通過兩點，沒有 P 值與信賴區間
    two = fit_model([(1, 1.0), (3, 5.0)], "linear")
    assert two is not None and two.terms == pytest.approx((-1.0, 2.0)) and two.p_value is None
    assert two.bounds(2) is None
    # 指數模型的值全部為負時以 −Y 配適
    negative = fit_model([(x, -y) for x, y in points], "exp")
    assert negative is not None and negative.predict(9) == pytest.approx(-(exp.predict(9) or 0))
    # 日期的數值（約 45000 天）與 8 次多項式：先縮放再解，係數仍然正確
    dates = [(45000 + 30 * i, 100 + 3 * i + 0.5 * (i % 3)) for i in range(40)]
    high = fit_model(dates, "polynomial", 8)
    assert high is not None and high.r2 is not None and high.r2 > 0.99
    assert high.predict(45000 + 30 * 20) == pytest.approx(160 + 0.5 * (20 % 3), abs=0.6)


def test_coefficients() -> None:
    """描述趨勢模型的各係數：估計值、標準誤、t 值與 P 值。標準誤以 numpy 在沒有縮放的設計矩陣上獨立算出
    （s²·(XᵀX)⁻¹ 的對角線）；Anscombe 第一組資料的數字是廣為引用的結果。"""
    import numpy as np

    points = list(zip(X, Y, strict=True))
    linear = fit_model(points, "linear")
    assert linear is not None
    slope, constant = linear.coefficients()
    assert (slope.power, constant.power) == (1, 0)
    assert (slope.value, constant.value) == pytest.approx((0.50009091, 3.00009091))
    assert (slope.standard_error, constant.standard_error) == pytest.approx(
        (0.117905500, 1.124746791), rel=1e-6
    )
    assert (slope.t, constant.t) == pytest.approx((4.241455, 2.667348), rel=1e-5)
    assert constant.p == pytest.approx(0.025734, rel=1e-4)
    # 只有一個自變數時，斜率的 t 檢定與整個模型的 F 檢定是同一件事
    assert slope.p == pytest.approx(linear.p_value)

    def expected(us: list[float], vs: list[float], powers: list[int]) -> list[tuple[float, float]]:
        design = np.array([[u**k for k in powers] for u in us])
        beta, *_ = np.linalg.lstsq(design, np.array(vs), rcond=None)
        residual = np.array(vs) - design @ beta
        variance = float(residual @ residual) / (len(us) - len(powers))
        errors = np.sqrt(variance * np.diag(np.linalg.inv(design.T @ design)))
        return [(float(b), float(e)) for b, e in zip(beta, errors, strict=True)]

    xs, ys = [float(x) for x in X], list(Y)
    cases: list[tuple[Any, list[float], list[float], list[int]]] = [
        (fit_model(points, "polynomial", 3), xs, ys, [3, 2, 1, 0]),
        (fit_model(points, "polynomial", 2, intercept=False), xs, ys, [2, 1]),
        (fit_model(points, "linear", intercept=False), xs, ys, [1]),
        (fit_model(points, "log"), [math.log(x) for x in X], ys, [1, 0]),
        # 指數與乘冪：取對數之後的空間（截距是 ln(b0)）
        (fit_model(points, "exp"), xs, [math.log(y) for y in Y], [1, 0]),
        (
            fit_model(points, "power"),
            [math.log(x) for x in X],
            [math.log(y) for y in Y],
            [1, 0],
        ),
    ]
    for model, us, vs, powers in cases:
        assert model is not None
        found = model.coefficients()
        assert [c.power for c in found] == powers
        df = len(us) - len(powers)
        for c, (value, error) in zip(found, expected(us, vs, powers), strict=True):
            assert c.value == pytest.approx(value, rel=1e-7, abs=1e-9)
            assert c.standard_error == pytest.approx(error, rel=1e-6)
            assert c.t == pytest.approx(value / error, rel=1e-6)
            assert c.p == pytest.approx(t_two_sided(abs(value / error), df))
    # 日期的數值（約 45000 天）：自變數縮放之後，標準誤仍與沒有縮放的算法相同
    dates = [(45000.0 + 30 * i, 100 + 3 * i + 0.5 * (i % 3)) for i in range(40)]
    dated = fit_model(dates, "linear")
    assert dated is not None
    centered = [x - 45000.0 for x, _ in dates]
    (slope_value, slope_error), _ = expected(centered, [y for _, y in dates], [1, 0])
    found_slope = dated.coefficients()[0]
    assert found_slope.value == pytest.approx(slope_value, rel=1e-7)
    assert found_slope.standard_error == pytest.approx(slope_error, rel=1e-6)
    # 兩點：殘差自由度是 0，沒有標準誤、t 值與 P 值
    two = fit_model([(1, 1.0), (3, 5.0)], "linear")
    assert two is not None
    assert [(c.value, c.standard_error, c.t, c.p) for c in two.coefficients()] == [
        (pytest.approx(2.0), None, None, None),
        (pytest.approx(-1.0), None, None, None),
    ]


def test_distributions() -> None:
    """t 與 F 分配（以 scipy.stats 的數值核對）。"""
    assert [round(t_quantile(0.975, df), 6) for df in (1, 2, 10, 30, 1000)] == [
        12.706205,
        4.302653,
        2.228139,
        2.042272,
        1.962339,
    ]
    assert f_sf(4.964603, 1, 10) == pytest.approx(0.05, abs=1e-6)
    assert f_sf(3.492828, 2, 20) == pytest.approx(0.05, abs=1e-6)
    assert betainc(2, 3, 0.4) == pytest.approx(0.5248)
    assert (format_p(0.00001), format_p(0.0123456789), format_p(None)) == (
        "< 0.0001",
        "0.0123457",
        "",
    )


def test_trend_lines_are_parsed(tmp_path: Path) -> None:
    ir = parse(tmp_path, scatter(trendline()))
    assert ir.unsupported == []
    assert ir.worksheets[0].marks.trend == TrendLine()
    style = (
        "<style><style-rule element='trendline'><format attr='stroke-color' value='#F9A655' />"
        "<format attr='stroke-size' value='2' /><format attr='line-pattern-only' value='dashed' />"
        "</style-rule></style>"
    )
    sheet = scatter(trendline("polynomial", 2, bands=True, intercept=False), style)
    trend = parse(tmp_path, sheet).worksheets[0].marks.trend
    assert trend == TrendLine(
        fit="polynomial",
        degree=2,
        intercept=False,
        confidence_bands=True,
        color="#f9a655",
        width=2,
        pattern="dashed",
    )
    # 不分組的因子
    factors = f"<excluded-factors><column>{ref('none:Region:nk')}</column></excluded-factors>"
    ir = parse(tmp_path, scatter(trendline(children=factors)))
    assert ir.unsupported == []
    trend = ir.worksheets[0].marks.trend
    assert trend is not None and trend.excluded == [FieldUse(field="f_region")]
    # 關閉、線條隱藏時沒有趨勢線；不認得的模型列為不支援
    hidden = "<style><style-rule element='trendline'><format attr='line-visibility' value='off' /></style-rule></style>"
    assert parse(tmp_path, scatter(trendline(), hidden)).worksheets[0].marks.trend is None
    off = scatter(trendline().replace("enabled='true'", "enabled='false'"))
    assert parse(tmp_path, off).worksheets[0].marks.trend is None
    ir = parse(tmp_path, scatter(trendline("moving")))
    assert [u.reason for u in ir.unsupported] == ["趨勢線的模型 moving 尚不支援"]


def test_trend_lines_by_color(tmp_path: Path, db: Path) -> None:
    """依顏色分組：每個地區一條線，顏色與標記相同（共用色彩刻度），數字與獨立計算的迴歸相同。"""
    spec = render(parse(tmp_path, scatter(trendline())), db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    main = main_encoding(spec)
    assert main is not None
    x, y, color = main["x"]["field"], main["y"]["field"], main["color"]["field"]
    trend = layer(spec)
    assert trend["encoding"]["color"]["field"] == color
    assert trend["encoding"]["color"] == main["color"]  # 共用色彩刻度與圖例
    assert trend["encoding"]["x"]["title"] == main["x"]["title"]
    assert [t["title"] for t in trend["encoding"]["tooltip"]] == [
        main["color"]["title"],
        "Trend Line",
        "R-Squared",
        "P-value",
    ]
    rows = trend["data"]["values"]
    marks = [v for v in spec["data"]["values"] if not v.get("__total")]
    assert {r[color] for r in rows} == {"East", "West", "South"}
    for region in {v[color] for v in marks}:
        points = [(v[x], v[y]) for v in marks if v[color] == region]
        line = [r for r in rows if r[color] == region]
        if len(points) < 2:
            assert line == []
            continue
        slope, intercept = statistics.linear_regression(*zip(*points, strict=True))
        # 線性、沒有信賴區間：畫在資料範圍的兩端
        assert [r["__ref_trend_x"] for r in line] == [
            min(p[0] for p in points),
            max(p[0] for p in points),
        ]
        for r in line:
            assert r["__ref_trend_y"] == pytest.approx(slope * r["__ref_trend_x"] + intercept)
        assert line[0]["__ref_trend_model"].startswith(f"{main['y']['title']} = ")
    # 描述趨勢模型：每條線的公式、觀測數、自由度、R 平方、P 值與標準誤
    described = {m["group"][-1]: m for m in spec["usermeta"]["trends"]}
    assert set(described) == {r[color] for r in rows}
    for region, model in described.items():
        points = [(v[x], v[y]) for v in marks if v[color] == region]
        slope, intercept = statistics.linear_regression(*zip(*points, strict=True))
        assert model["fit"] == "linear" and model["observations"] == len(points)
        assert (model["model_df"], model["residual_df"]) == (2, len(points) - 2)
        assert model["terms"] == pytest.approx([intercept, slope])
        # 各係數（斜率在前、截距在後）：兩個點的線沒有標準誤，三個點以上有
        assert [c["power"] for c in model["coefficients"]] == [1, 0]
        assert [c["value"] for c in model["coefficients"]] == pytest.approx([slope, intercept])
        assert all(
            (c["standard_error"] is None) == (len(points) == 2) for c in model["coefficients"]
        )
        assert model["equation"].startswith(f"{main['y']['title']} = ")
        if len(points) > 2:
            residuals = [py - (slope * px + intercept) for px, py in points]
            sse = sum(r * r for r in residuals)
            assert model["standard_error"] == pytest.approx((sse / (len(points) - 2)) ** 0.5)
            assert 0 <= model["r2"] <= 1 and 0 <= model["p"] <= 1


def test_trend_line_options(tmp_path: Path, db: Path) -> None:
    """不依顏色分組（一條線、預設的深灰色）、指數模型、信賴區間與不分組的因子。"""
    spec = render(parse(tmp_path, scatter(trendline("exp", by_color=False))), db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    trend = layer(spec)
    assert trend["mark"]["color"] == "#4f4f4f" and "color" not in trend["encoding"]
    assert {r["__ref_trend_group"] for r in trend["data"]["values"]} == {0}
    assert len(trend["data"]["values"]) == 60  # 曲線取 60 個點
    assert not any(item.get("name") == "__ref_trend_low" for item in spec["layer"])

    spec = render(parse(tmp_path, scatter(trendline(bands=True), color=False)), db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    rows = layer(spec)["data"]["values"]
    low, high = layer(spec, "__ref_trend_low"), layer(spec, "__ref_trend_high")
    assert (
        low["mark"]["strokeDash"] == [4, 2] and high["encoding"]["y"]["field"] == "__ref_trend_high"
    )
    assert all(r["__ref_trend_low"] <= r["__ref_trend_y"] <= r["__ref_trend_high"] for r in rows)
    # 範圍中間的信賴區間最窄
    widths = [r["__ref_trend_high"] - r["__ref_trend_low"] for r in rows]
    assert min(widths) < widths[0] and min(widths) < widths[-1]

    # 地區不分組：所有地區共用一個模型（只有一條線）
    factors = f"<excluded-factors><column>{ref('none:Region:nk')}</column></excluded-factors>"
    spec = render(parse(tmp_path, scatter(trendline(children=factors))), db).spec
    trend = layer(spec)
    assert {r["__ref_trend_group"] for r in trend["data"]["values"]} == {0}
    assert "color" in trend["mark"]


def test_trend_lines_on_time_axes(tmp_path: Path, db: Path) -> None:
    """時間軸：以 1899-12-30 起的天數配適，點取各標記的時間（字串照原樣）；與獨立計算的迴歸相同。"""
    line = worksheet(
        "Line",
        [MONTH, SALES],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        pane_extra=trendline(),
    )
    spec = render(parse(tmp_path, line), db).spec
    assert [e.message for e in validator().iter_errors(spec)] == []
    main = main_encoding(spec)
    assert main is not None
    trend = layer(spec)
    assert trend["encoding"]["x"]["type"] == "temporal"
    assert trend["encoding"]["x"]["timeUnit"] == main["x"]["timeUnit"] == "utcyearmonth"
    marks = spec["data"]["values"]
    x, y = main["x"]["field"], main["y"]["field"]
    rows = trend["data"]["values"]
    assert [r["__ref_trend_x"] for r in rows] == sorted({v[x] for v in marks})

    slope, intercept = statistics.linear_regression(
        [days(v[x]) for v in marks], [v[y] for v in marks]
    )
    for r in rows:
        assert r["__ref_trend_y"] == pytest.approx(slope * days(r["__ref_trend_x"]) + intercept)
    assert math.isclose(
        float(rows[0]["__ref_trend_r2"]),
        statistics.correlation([days(v[x]) for v in marks], [v[y] for v in marks]) ** 2,
        rel_tol=1e-5,
    )


def test_trend_lines_need_continuous_axes(tmp_path: Path, db: Path) -> None:
    bars = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("sum:Sales:qk"),
        cols=ref("none:Region:nk"),
        pane_extra=trendline(),
    )
    rendered = render(parse(tmp_path, bars), db)
    assert rendered.notes == ["趨勢線需要橫軸與縱軸都是連續的座標軸，未畫出"]


def test_trend_lines_on_dual_axes(tmp_path: Path, db: Path) -> None:
    """雙軸的兩個量值各有一條趨勢線（2026-11-21 稽核：兩個圖層都叫 __ref_trend，疊在同一張圖上時 Vega 回報
    Duplicate data set name，整張圖畫不出來）：圖層的名稱各不相同；以 Measure Names 上色時，趨勢線與自己的
    量值同色（量值的名稱是圖層上算出的常數，不在資料列中）。"""
    color = f"<color column='{ref(':Measure Names')}' />"

    def pane(pane_id: int, measure: str, index: str = "") -> str:
        return (
            f"<pane id='{pane_id}' y-axis-name='{ref(measure)}'{index}><view><breakdown value='auto' />"
            f"</view><mark class='Line' /><encodings>{color}</encodings>{trendline()}</pane>"
        )

    sheet = worksheet(
        "Dual",
        [MONTH, SALES, PROFIT],
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=color,
        pane_extra=trendline(),
        panes_extra=pane(2, "sum:Sales:qk") + pane(3, "sum:Profit:qk", " y-index='1'"),
    )
    ir = parse(tmp_path, sheet)
    assert [p.marks.trend is not None for p in ir.worksheets[0].panes] == [True, True]
    rendered = render(ir, db)
    spec = rendered.spec
    assert spec["resolve"]["scale"]["y"] == "independent"

    def named(node: dict[str, Any]) -> list[dict[str, Any]]:
        found = [node] if "name" in node and "mark" in node else []
        return found + [item for child in node.get("layer", []) for item in named(child)]

    trends = [item for item in named(spec) if item["name"].startswith("__ref_trend")]
    assert [item["name"] for item in trends] == ["__ref_trend", "__ref_trend2"]
    marks = spec["data"]["values"]
    x = main_encoding(spec)["x"]["field"]  # type: ignore[index]
    for trend, title, own in zip(trends, ("Sales", "Profit"), spec["layer"], strict=True):
        assert trend in own["layer"]  # 各自疊在自己的量值上，用那個軸的刻度
        y = main_encoding(own)["y"]  # type: ignore[index]
        assert (y["title"], trend["encoding"]["y"]["title"]) == (title, title)
        rows = trend["data"]["values"]
        slope, intercept = statistics.linear_regression(
            [days(v[x]) for v in marks], [v[y["field"]] for v in marks]
        )
        for r in rows:
            assert r["__ref_trend_y"] == pytest.approx(slope * days(r["__ref_trend_x"]) + intercept)
        # 與這個量值的標記同色：色彩是同一個欄位與比例尺，值是量值的名稱
        assert trend["encoding"]["color"] == main_encoding(own)["color"]  # type: ignore[index]
        assert {r["__mn_name"] for r in rows} == {title}
    assert [m["group"] for m in spec["usermeta"]["trends"]] == [["Sales"], ["Profit"]]


def test_trend_line_of_a_measure_beside_measure_values(tmp_path: Path, db: Path) -> None:
    """Measure Values 與另一個量值疊成雙軸、都以 Measure Names 上色、各有趨勢線：另一個量值的趨勢線只以每個
    標記一份資料配適（長表中每列在每個 Measure Values 的量值重複一次，照算的話觀測數與 P 值都不對），顏色與
    它的標記相同（色彩是固定的 datum，不是欄位）；三條趨勢線的圖層名稱各不相同。"""
    values = ref("Multiple Values")
    names = f"<color column='{ref(':Measure Names')}' />"
    average = instance("[Sales]", "Avg", "[avg:Sales:qk]", "quantitative")

    def pane(pane_id: int, axis: str, index: str = "") -> str:
        return (
            f"<pane id='{pane_id}' y-axis-name='{axis}'{index}><view><breakdown value='auto' /></view>"
            f"<mark class='Line' /><encodings>{names}</encodings>{trendline()}</pane>"
        )

    sheet = worksheet(
        "Dual",
        [MONTH, SALES, average, PROFIT],
        rows=f"({values} + {ref('sum:Profit:qk')})",
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=names,
        pane_extra=trendline(),
        panes_extra=pane(2, values) + pane(3, ref("sum:Profit:qk"), " y-index='1'"),
        view_extra=(
            f"<filter class='categorical' column='{ref(':Measure Names')}'>"
            "<groupfilter function='union' user:op='manual'>"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('sum:Sales:qk')}&quot;' />"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('avg:Sales:qk')}&quot;' />"
            "</groupfilter></filter>"
        ),
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    spec = render(ir, db).spec
    marks = spec["data"]["values"]
    months = {v[main_encoding(spec)["x"]["field"]] for v in marks}  # type: ignore[index]
    assert len(marks) == 2 * len(months)  # 長表：每個月在 Measure Values 的兩個量值各一列
    models = {tuple(m["group"]): m for m in spec["usermeta"]["trends"]}
    assert list(models) == [("Sales",), ("AVG(Sales)",), ("Profit",)]
    assert {m["observations"] for m in models.values()} == {len(months)}  # Profit 不是兩倍

    def named(node: dict[str, Any]) -> list[dict[str, Any]]:
        found = [node] if "name" in node and "mark" in node else []
        return found + [item for child in node.get("layer", []) for item in named(child)]

    trends = [item for item in named(spec) if item["name"].startswith("__ref_trend")]
    assert [item["name"] for item in trends] == ["__ref_trend", "__ref_trend2"]
    folded, profit = trends
    assert folded["encoding"]["color"]["field"] == "__mn"
    assert {r["__mn"] for r in folded["data"]["values"]} == {"Sales", "AVG(Sales)"}
    assert profit["encoding"]["color"] == {
        "datum": "Profit",
        "type": "nominal",
        "title": "Measure Names",
    }
    assert "color" not in profit["mark"]
