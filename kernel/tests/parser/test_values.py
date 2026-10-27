import pytest

from tmig.parser.ids import IdAllocator, slugify
from tmig.parser.values import LiteralError, parse_literal


@pytest.mark.parametrize(
    ("text", "datatype", "expected"),
    [
        ("%null%", "string", None),
        ('"East"', "string", "East"),
        ('"say ""hi"""', "string", 'say "hi"'),
        ("#2024-01-05#", "date", "2024-01-05"),
        ("#2024-01-05 00:00:00#", "date", "2024-01-05"),
        ("#2024-01-05 08:30:00#", "datetime", "2024-01-05T08:30:00"),
        ("#2024-01-05#", "datetime", "2024-01-05T00:00:00"),
        ("2024-01-05", "date", "2024-01-05"),
        ("true", "boolean", True),
        ("False", "boolean", False),
        ("42", "integer", 42),
        ("42", "real", 42.0),
        ("-1.5", "real", -1.5),
        ("1e3", "real", 1000.0),
        ("3.0", "integer", 3),
        ("plain", "string", "plain"),
        (None, "string", None),
    ],
)
def test_parse_literal(text: str | None, datatype: str, expected: object) -> None:
    value = parse_literal(text, datatype)
    assert value == expected
    assert type(value) is type(expected)


def test_parse_literal_errors() -> None:
    with pytest.raises(LiteralError):
        parse_literal("#not a date#", "date")
    with pytest.raises(LiteralError):
        parse_literal("abc", "integer")


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Sales by Region", "sales_by_region"),
        ("Sub-Category", "sub_category"),
        ("Country/Region", "country_region"),
        ("Calculation_123", "calc_123"),
        ("Café", "cafe"),
        ("利潤率", ""),
        ("2024 利潤", ""),
        ("   ", ""),
    ],
)
def test_slugify(text: str, slug: str) -> None:
    assert slugify(text) == slug


def test_id_allocator() -> None:
    ids = IdAllocator()
    assert ids.make("f", "利潤率", "[Calculation_1]") == "f_calc_1"
    assert ids.make("ws", "Sales") == "ws_sales"
    assert ids.make("ws", "Sales!") == "ws_sales_2"
    assert (
        ids.make("db", "銷售總覽")
        == "db_" + __import__("hashlib").sha1("銷售總覽".encode()).hexdigest()[:8]
    )
    long_id = ids.make("f", "x" * 100)
    assert len(long_id) == 64
    assert len(ids.make("f", "x" * 100)) == 64
