import pytest

from tmig.formula.ast import Lod
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError, Info

from .helpers import analyzer


def info(formula: str) -> Info:
    return analyzer().analyze(parse_formula(formula))


def error(formula: str) -> FormulaSemanticError:
    with pytest.raises(FormulaSemanticError) as exc:
        info(formula)
    return exc.value


@pytest.mark.parametrize(
    ("formula", "datatype", "level"),
    [
        ("1", "integer", "const"),
        ("1 + 2.5", "real", "const"),
        ("[Parameters].[Top N] * 2", "integer", "const"),
        ("[Parameters].[Parameter 1]", "integer", "const"),
        ("[Sales]", "real", "row"),
        ("[Quantity] + 1", "integer", "row"),
        ("[Quantity] / 2", "real", "row"),
        ("[Quantity] % 2", "integer", "row"),
        ("[Region] + ' / ' + [Category]", "string", "row"),
        ("[Order Date] + 1", "date", "row"),
        ("[Sales] > 100 AND [Region] = 'West'", "boolean", "row"),
        ("[Region] IN ('East', 'West')", "boolean", "row"),
        ("IF [Sales] > 0 THEN 1 ELSE 0.5 END", "real", "row"),
        ("IF [Sales] > 0 THEN 'yes' END", "string", "row"),
        ("CASE [Region] WHEN 'East' THEN 1 ELSE 2 END", "integer", "row"),
        ("IIF([Returned], 1, NULL)", "integer", "row"),
        ("ZN([Quantity])", "integer", "row"),
        ("IFNULL([Profit], 0)", "real", "row"),
        ("ISNULL([Profit])", "boolean", "row"),
        ("ROUND([Sales], 2)", "real", "row"),
        ("CEILING([Sales])", "integer", "row"),
        ("MIN([Sales], [Profit])", "real", "row"),
        ("LEFT([Region], 2)", "string", "row"),
        ("CONTAINS([Region], 'st')", "boolean", "row"),
        ("YEAR([Order Date])", "integer", "row"),
        ("DATEPART('week', [Order Date], 'monday')", "integer", "row"),
        ("DATETRUNC('month', [Order Date])", "date", "row"),
        ("DATETRUNC('hour', [Ship Time])", "datetime", "row"),
        ("DATEADD('month', 1, [Order Date])", "datetime", "row"),
        ("DATEDIFF('day', [Order Date], [Ship Time])", "integer", "row"),
        ("STR([Quantity])", "string", "row"),
        ("INT([Sales])", "integer", "row"),
        ("SUM([Sales])", "real", "agg"),
        ("SUM([Quantity])", "integer", "agg"),
        ("AVG([Quantity])", "real", "agg"),
        ("COUNTD([Order ID])", "integer", "agg"),
        ("MIN([Order Date])", "date", "agg"),
        ("ATTR([Region])", "string", "agg"),
        ("SUM([Sales]) / SUM([Quantity])", "real", "agg"),
        ("[Profit Ratio] * 100", "real", "agg"),
        ("SUM([Sales]) > [Parameters].[Top N]", "boolean", "agg"),
        ("PERCENTILE([Sales], 0.9)", "real", "agg"),
        ("RANK(SUM([Sales]))", "integer", "tc"),
        ("RUNNING_SUM(SUM([Quantity]))", "integer", "tc"),
        ("SUM([Sales]) / TOTAL(SUM([Sales]))", "real", "tc"),
        ("WINDOW_AVG(SUM([Sales]), -2, 0)", "real", "tc"),
        ("WINDOW_SUM(SUM([Sales]), FIRST(), LAST())", "real", "tc"),
        ("INDEX() <= 3", "boolean", "tc"),
        ("LOOKUP(SUM([Sales]), -1)", "real", "tc"),
        ("[Running Sales] - SUM([Sales])", "real", "tc"),
    ],
)
def test_types_and_levels(formula: str, datatype: str, level: str) -> None:
    result = info(formula)
    assert (result.datatype, result.level) == (datatype, level)


def test_lod_is_row_level() -> None:
    result = info("{FIXED [Region] : SUM([Sales])}")
    assert (result.datatype, result.level, result.has_lod) == ("real", "row", True)
    assert info("[Share of Region]").has_lod
    assert info("SUM([Share of Region])").level == "agg"
    assert info("AVG({INCLUDE [Customer] : SUM([Profit])})").has_lod


@pytest.mark.parametrize(
    ("formula", "message"),
    [
        ("SUM([Sales]) + [Profit]", "不能混用彙總與非彙總"),
        ("IF [Sales] > 0 THEN SUM([Profit]) END", "不能混用彙總與非彙總"),
        ("SUM(SUM([Sales]))", "已經是彙總"),
        ("SUM([Profit Ratio])", "已經是彙總"),
        ("RANK([Sales])", "必須是彙總"),
        ("{FIXED [Region] : [Sales]}", "LOD 運算式內部必須是彙總"),
        ("{FIXED [Profit Ratio] : SUM([Sales])}", "必須是列層級的欄位"),
        ("[Nope]", "找不到欄位 [Nope]"),
        ("[Parameters].[Nope]", "找不到欄位"),
        ("SUM([Region])", "必須是數字"),
        ("[Region] + 1", "無法相加"),
        ("[Region] > 1", "無法比較"),
        ("IF [Sales] THEN 1 END", "條件必須是布林值"),
        ("IF [Sales] > 0 THEN 1 ELSE 'x' END", "型別不一致"),
        ("NOT [Sales]", "NOT 只能用在布林值"),
        ("LEFT([Region])", "需要 2 個參數"),
        ("DATEPART([Region], [Order Date])", "必須是字串常值"),
        ("DATEPART('fortnight', [Order Date])", "不支援「fortnight」"),
        ("WINDOW_SUM(SUM([Sales]), -1)", "必須同時指定起點與終點"),
        ("WINDOW_SUM(SUM([Sales]), [Parameters].[Top N], 0)", "視窗位移只支援"),
        ("[Loop A]", "循環參照"),
        ("{FIXED [Region] : SUM({FIXED [Category] : SUM([Sales])})}", "尚不支援巢狀 LOD"),
        ("[Region] - [Order Date]", "無法相減"),
        ("[Order Date] - [Region]", "無法相減"),
        ("REGEXP_MATCH([Region], '(?=a)')", "前向查看"),
        ("REGEXP_MATCH([Region], '(a)\\1')", "反向參照"),
        ("REGEXP_EXTRACT_NTH([Region], '(a)', [Quantity])", "整數常值"),
        ("REGEXP_REPLACE([Region], 'a', 1)", "必須是字串"),
        ("DATEPARSE('yyyy-ww', [Region])", "「ww」尚不支援"),
        ("DATEPARSE([Region], [Region])", "格式必須是字串常值"),
        ("DATENAME('fortnight', [Order Date])", "不支援「fortnight」"),
    ],
)
def test_errors(formula: str, message: str) -> None:
    assert message in error(formula).message


def test_unsupported_function_is_flagged() -> None:
    exc = error("HEXBINX([Sales], [Profit])")
    assert exc.unsupported
    assert "HEXBINX" in exc.message


def test_expand_walks_into_calcs() -> None:
    a = analyzer()
    node = parse_formula("SUM([Share of Region])")
    lods = [n for n in a.expand(node) if isinstance(n, Lod)]
    assert len(lods) == 1
    assert lods[0].dims[0].parts == ("Region",)


def test_calc_results_are_cached() -> None:
    a = analyzer()
    first = a.analyze(parse_formula("[Profit Ratio]"))
    second = a.analyze(parse_formula("[Profit Ratio] + 1"))
    assert first.level == second.level == "agg"
