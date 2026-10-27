from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tmig.render.format import d3_format, format_number, format_value, parse_format


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


def test_automatic_format() -> None:
    assert parse_format(None) is None
    assert parse_format("0.00e+00") is None
    assert format_number(1234567.891, None) == "1,234,568"
    assert format_number(12.5, None) == "12.5"
    assert format_number(0.12345, None) == "0.1235"
    assert format_number(7, None, integer=True) == "7"
    assert format_number(Decimal("3.00"), None) == "3"


def test_format_value() -> None:
    assert format_value(None, "real") == "Null"
    assert format_value(True, "boolean") == "True"
    assert format_value(dt.date(2024, 3, 1), "date", trunc="month") == "2024-03"
    assert format_value(dt.date(2024, 4, 1), "date", trunc="quarter") == "2024 Q2"
    assert format_value(dt.date(2024, 1, 1), "date", trunc="year") == "2024"
    assert format_value(dt.date(2024, 1, 5), "date") == "2024-01-05"
    assert format_value("East", "string") == "East"
