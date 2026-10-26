from tmig.census.collect import INSTANCE_PREFIXES
from tmig.parser.refs import parse_instance, qualified_parts, shelf_operators, split_brackets


def test_split_brackets_with_escapes() -> None:
    names, rest = split_brackets("[Orders].[Sales [USD]]]")
    assert names == ["Orders", "Sales [USD]"]
    assert rest == "[].[]"


def test_qualified_parts() -> None:
    assert qualified_parts("[World Indicators].[sum:F: GDP (curr $):qk]") == [
        "World Indicators",
        "sum:F: GDP (curr $):qk",
    ]


def test_shelf_operators_ignore_names() -> None:
    assert shelf_operators("[ds].[none:Country / Region:nk]") == set()
    assert shelf_operators("[ds].[none:Region:nk] / [ds].[none:Category:nk]") == {"/"}
    assert shelf_operators("([ds].[sum:A:qk] + [ds].[sum:B:qk])") == {"(", "+", ")"}


def test_parse_instance_keeps_colons_in_field_names() -> None:
    parsed = parse_instance("rank:sum:F: GDP (curr $):qk", INSTANCE_PREFIXES)
    assert parsed.prefixes == ("rank", "sum")
    assert parsed.field == "F: GDP (curr $)"
    assert parsed.kind == "qk"


def test_parse_instance_without_prefix() -> None:
    parsed = parse_instance("Multiple Values", INSTANCE_PREFIXES)
    assert parsed.prefixes == ()
    assert parsed.field == "Multiple Values"
