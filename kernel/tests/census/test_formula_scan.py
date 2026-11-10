import pytest

from tmig.census.formula_scan import scan_formula
from tmig.formula.lexer import tokenize


def test_counts_function_calls() -> None:
    scan = scan_formula("SUM([Profit]) / SUM([Sales]) + zn(AVG([Discount]))")
    assert scan.functions == {"SUM": 2, "ZN": 1, "AVG": 1}
    assert scan.outside_v0 == []


def test_keywords_are_not_functions() -> None:
    scan = scan_formula("IF ([a] > 1) THEN 1 ELSEIF NOT([b]) THEN 2 ELSE 3 END")
    assert scan.functions == {}
    assert {"IF", "THEN", "ELSEIF", "NOT", "ELSE", "END"} <= scan.keywords


def test_ignores_comments_strings_and_field_names() -> None:
    formula = (
        "// SUM( 在註解裡\r\n"
        "/* MAX( 區塊註解 */\n"
        'IF [COUNT(Orders)] = \'LEFT(\' THEN "it\'s [x]" ELSE "say ""MIN(""" END'
    )
    scan = scan_formula(formula)
    assert scan.functions == {}
    assert scan.errors == ()


def test_escaped_brackets_in_field_names() -> None:
    scan = scan_formula("SUM([Sales [USD]]]) + MAX([a]]b])")
    assert scan.functions == {"SUM": 1, "MAX": 1}
    assert scan.references == ("[Sales [USD]]]", "[a]]b]")


def test_qualified_references() -> None:
    scan = scan_formula("IIF(RANK(SUM([Sales])) <= [Parameters].[Top N], 1, 0)")
    assert scan.references == ("[Sales]", "[Parameters].[Top N]")


@pytest.mark.parametrize(
    ("formula", "lod"),
    [
        ("{FIXED [Region] : SUM([Sales])}", ("FIXED",)),
        ("{ include [Region] : AVG([Sales]) }", ("INCLUDE",)),
        ("{EXCLUDE [Month] : SUM([Sales])}", ("EXCLUDE",)),
        ("{SUM([Sales])}", ("TABLE",)),
        ("{FIXED : MAX({INCLUDE [a] : MIN([b])})}", ("FIXED", "INCLUDE")),
    ],
)
def test_lod(formula: str, lod: tuple[str, ...]) -> None:
    assert scan_formula(formula).lod == lod


def test_table_calc_and_user_functions() -> None:
    scan = scan_formula(
        "IF ISMEMBEROF('Sales') OR USERNAME() = [Owner] THEN WINDOW_AVG(SUM([x])) END"
        " + RUNNING_SUM(SUM([y])) + LOOKUP(SUM([y]), -1)"
    )
    assert scan.user_functions == ["ISMEMBEROF", "USERNAME"]
    assert scan.table_calc_functions == ["LOOKUP", "RUNNING_SUM", "WINDOW_AVG"]


def test_outside_v0() -> None:
    scan = scan_formula(
        "IF MODEL_QUANTILE(0.5, [a], [b]) > 0 THEN PROPER([d]) END + ATTR([x]) IN (1) + DATENAME('month', [d])"
    )
    assert scan.outside_v0 == ["MODEL_QUANTILE", "PROPER"]


def test_numbers_and_date_literals() -> None:
    tokens = tokenize("1.5e3 + .25 - #2025-01-01 08:00:00#")
    assert [t.kind for t in tokens] == ["number", "op", "number", "op", "date", "eof"]
    assert tokens[-2].value == "2025-01-01 08:00:00"


@pytest.mark.parametrize(
    ("formula", "message"),
    [
        ("'abc", "字串沒有結束"),
        ("SUM([Sales)", "方括號沒有結束"),
        ("#2025-01-01", "日期常值沒有結束"),
        ("/* never ends", "區塊註解沒有結束"),
    ],
)
def test_reports_unterminated_tokens(formula: str, message: str) -> None:
    errors = scan_formula(formula).errors
    assert len(errors) == 1
    assert message in errors[0]
