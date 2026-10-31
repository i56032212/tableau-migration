from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tmig.render.format import (
    axis_format,
    d3_format,
    format_number,
    format_value,
    parse_format,
)


@pytest.mark.parametrize(
    ("fmt", "value", "text", "d3"),
    [
        ("n#,##0", 1234.567, "1,235", ",.0f"),
        ("#,##0.00;-#,##0.00", -1234.567, "-1,234.57", ",.2f"),
        ("p0.0%", 0.1234, "12.3%", ".1%"),
        ('c"$"#,##0', -1234.5, "-$1,235", "$,.0f"),
        ('c"NT$"#,##0.00', 1234.5, "NT$1,234.50", ",.2f"),
        ('#,##0" 件"', 12, "12 件", ",.0f"),
    ],
)
def test_formats(fmt: str, value: float, text: str, d3: str) -> None:
    parsed = parse_format(fmt)
    assert format_number(value, parsed) == text
    assert d3_format(parsed) == d3


@pytest.mark.parametrize(
    ("fmt", "value", "text"),
    [
        # 單位換算（公開報表 Car Sales 的格式）：結尾的逗號各除以 1000
        ('c" $"#,##0,,.00M;" $"-#,##0,,.00M', 361730845, " $361.73M"),
        ('c" $"#,##0,,.00M;" $"-#,##0,,.00M', -9164492, " $-9.16M"),
        ('n"#"#,##0,.00K;-"#"#,##0,.00K', 369, "#0.37K"),
        ('n"#"#,##0,.00K;-"#"#,##0,.00K', 12930, "#12.93K"),
        ("0.0,,", 2345678, "2.3"),
        ("#,##0,K", 1234567, "1,235K"),
        # 第二段（負數）以絕對值套用；第三段（零）
        ('c"$"#,##0.00;("$"#,##0.00)', -1234.5, "($1,234.50)"),
        ('#,##0;-#,##0;"—"', 0, "—"),
        ("p0.00%", -0.0175, "-1.75%"),
        # 地區設定的標準貨幣（美國英文）
        ("C1033%", -13.38, "$-13.38"),
        ("C1033", 1102.1, "$1,102.10"),
    ],
)
def test_scaling_and_sections(fmt: str, value: float, text: str) -> None:
    assert format_number(value, parse_format(fmt)) == text


def test_axis_format() -> None:
    assert axis_format(parse_format("n#,##0")) == {"format": ",.0f"}
    assert axis_format(parse_format('c"$"#,##0')) == {"format": "$,.0f"}
    scaled = axis_format(parse_format('c" $"#,##0,,.00M;" $"-#,##0,,.00M'))
    assert scaled == {
        "labelExpr": "datum.value < 0 ? ' $-' + format(abs(datum.value) / 1000000, ',.2f') + 'M'"
        " : ' $' + format(abs(datum.value) / 1000000, ',.2f') + 'M'"
    }
    suffix = axis_format(parse_format('#,##0" 件"'))
    assert suffix is not None and "' 件'" in suffix["labelExpr"]
    assert axis_format(None) is None


def test_automatic_format() -> None:
    assert parse_format(None) is None
    assert parse_format("0.00e+00") is None
    assert format_number(1234567.891, None) == "1,234,568"
    assert format_number(12.5, None) == "12.5"
    assert format_number(0.12345, None) == "0.1235"
    assert format_number(7, None, integer=True) == "7"
    assert format_number(Decimal("3.00"), None) == "3"


@pytest.mark.parametrize(
    ("value", "fmt", "text"),
    [
        (dt.date(2020, 4, 1), "*dddd, mmmm d, yyyy", "Wednesday, April 1, 2020"),
        (dt.date(2020, 1, 2), "m/d/yyyy", "1/2/2020"),
        (dt.date(2020, 1, 2), "mmm yy", "Jan 20"),
        (dt.datetime(2020, 1, 2, 15, 4, 5), "yyyy-mm-dd hh:nn:ss", "2020-01-02 15:04:05"),
        (dt.datetime(2020, 1, 2, 15, 4, 5), "h:nn AM/PM", "3:04 PM"),
        (dt.date(2020, 5, 2), '"Q"q yyyy', "Q2 2020"),
    ],
)
def test_date_formats(value: dt.date, fmt: str, text: str) -> None:
    assert format_value(value, "date", fmt) == text


def test_number_format_on_date_is_ignored() -> None:
    assert format_value(dt.date(2020, 1, 2), "date", "n#,##0") == "2020-01-02"


def test_format_value() -> None:
    assert format_value(None, "real") == "Null"
    assert format_value(True, "boolean") == "True"
    assert format_value(dt.date(2024, 3, 1), "date", trunc="month") == "2024-03"
    assert format_value(dt.date(2024, 4, 1), "date", trunc="quarter") == "2024 Q2"
    assert format_value(dt.date(2024, 1, 1), "date", trunc="year") == "2024"
    assert format_value(dt.date(2024, 1, 5), "date") == "2024-01-05"
    assert format_value("East", "string") == "East"
