"""趨勢線：解析 <trendline> 與線條格式；依窗格與顏色分組配適模型（與 Tableau 相同的轉換與最小平方法），
畫在各組資料的範圍內，信賴區間為上下兩條線，工具提示為公式、R 平方與 P 值。

模型的數字以 numpy／scipy 的結果核對（見 test_models）。測試資料見 tests/sqlgen/test_query.py。
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any

import pytest

from tmig.ir import FieldUse, TrendLine
from tmig.render.trends import betainc, f_sf, fit_model, format_p, t_quantile
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

    def days(text: str) -> float:
        from datetime import datetime

        return (datetime.fromisoformat(text) - datetime(1899, 12, 30)).total_seconds() / 86400

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
