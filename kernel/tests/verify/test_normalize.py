from __future__ import annotations

import datetime as dt

import pytest

from tmig.verify.normalize import (
    NormalizeError,
    is_null,
    normalize_actual,
    parse_bool,
    parse_date,
    parse_number,
)


@pytest.mark.parametrize(
    ("text", "value", "decimals", "formatted"),
    [
        ("42", 42.0, 0, False),
        ("-0.5", -0.5, 1, False),
        ("+7", 7.0, 0, False),
        ("1,234.5", 1234.5, 1, True),
        ("$1,234", 1234.0, 0, True),
        ("-$5.00", -5.0, 2, True),
        ("(1,234)", -1234.0, 0, True),
        ("12.3%", 0.123, 3, True),
        ("1.2K", 1200.0, -2, True),
        ("3.5M", 3500000.0, -5, True),
        ("NT$ 3,000", 3000.0, 0, True),
        ("1e-5", 1e-5, None, False),
        ("252612.74350000013", 252612.74350000013, 11, False),
    ],
)
def test_parse_number(text: str, value: float, decimals: int | None, formatted: bool) -> None:
    parsed = parse_number(text)
    assert parsed.value == pytest.approx(value)
    assert (parsed.decimals, parsed.formatted) == (decimals, formatted)


def test_parse_number_errors() -> None:
    for text in ("abc", "1.2.3", "", "$"):
        with pytest.raises(NormalizeError):
            parse_number(text)


@pytest.mark.parametrize(
    ("text", "iso"),
    [
        ("2024-01-02", "2024-01-02"),
        ("2024-01-02 00:00:00", "2024-01-02"),
        ("2024-01-02T08:00:00", "2024-01-02T08:00:00"),
        ("1/2/2024", "2024-01-02"),
        ("1/2/2024 3:04:05 PM", "2024-01-02T15:04:05"),
        ("12/31/2024 12:00:00 AM", "2024-12-31"),
        ("2024/1/2", "2024-01-02"),
        ("January 2024", "2024-01-01"),
        ("Sept 2024", "2024-09-01"),
        ("March 5, 2024", "2024-03-05"),
        ("2024年1月", "2024-01-01"),
        ("2024年1月2日", "2024-01-02"),
        ("Q2 2024", "2024-04-01"),
        ("2024 Q3", "2024-07-01"),
        ("2024年第4季", "2024-10-01"),
        ("2024", "2024-01-01"),
    ],
)
def test_parse_date(text: str, iso: str) -> None:
    assert parse_date(text) == iso


def test_parse_date_errors() -> None:
    for text in ("2024-02-30", "yesterday", "13/45/2024"):
        with pytest.raises(NormalizeError):
            parse_date(text)


def test_nulls_and_booleans() -> None:
    assert all(is_null(t) for t in ("", " ", "Null", "%null%", None))
    assert not is_null("0")
    assert parse_bool("True") is True and parse_bool("假") is False
    with pytest.raises(NormalizeError):
        parse_bool("maybe")


def test_normalize_actual() -> None:
    assert normalize_actual(dt.date(2024, 1, 2), "date") == "2024-01-02"
    assert normalize_actual(dt.datetime(2024, 1, 2), "datetime") == "2024-01-02"
    assert normalize_actual(dt.datetime(2024, 1, 2, 3, 4), "datetime") == "2024-01-02T03:04:00"
    assert normalize_actual(3, "integer") == 3.0
    assert normalize_actual(None, "string") is None
