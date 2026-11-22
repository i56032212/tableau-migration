"""Data Blending：次要資料源欄位的條件與 Top N 篩選、次要資料源計算欄位中的 LOD、全域篩選，以手算的結果驗證。

主要資料源為 test_query 的訂單（East 350、West 800、South 100、North 60）；次要資料源為各地區、各年的目標
（test_blend 的 TARGETS：East 2023 300、East 2024 100、West 2023 900、South 2024 50、Central 2024 999；
North 沒有目標）。各年的目標合計：2023 為 1200、2024 為 1149；全部 2349。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.data.executor import execute_query
from tmig.ir import IR, CategoricalFilter, ConditionFilter, TopNFilter
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import QueryError, worksheet_sql

from ..parser import builder as b
from ..parser.builder import DS, instance, ref, worksheet
from .test_blend import (
    SOURCES,
    TARGET,
    TARGETS,
    YEAR_TEXT,
    fields,
    filtered_sheet,
    parse,
    target_ref,
    targets_datasource,
    year_filter,
)
from .test_query import REGION, SALES

YEAR = "[none:Calculation_year:nk]"
LOD_CALCS = (
    YEAR_TEXT
    + b.calc("Calculation_total", "Total Target", "{FIXED : SUM([Target])}")
    + b.calc("Calculation_share", "Target Share", "SUM([Target]) / MIN([Total Target])")
    + b.calc("Calculation_peryear", "Per Year", "{INCLUDE [Order Date] : SUM([Target])}")
    + b.calc(
        "Calculation_wshare",
        "Window Share",
        "WINDOW_SUM(SUM([Target])) / MIN({FIXED : SUM([Target])})",
    )
)
SHARE = instance("[Calculation_share]", "User", "[usr:Calculation_share:qk]", "quantitative")
PER_YEAR = instance("[Calculation_peryear]", "Avg", "[avg:Calculation_peryear:qk]", "quantitative")
WINDOW_SHARE = instance(
    "[Calculation_wshare]", "User", "[usr:Calculation_wshare:qk]", "quantitative"
)


@pytest.fixture(scope="module")
def folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("blend_secondary")


def top_years(count: int, end: str = "top", context: bool = False) -> str:
    """次要資料源 Year Text 依 SUM([Target]) 的 Top N 篩選（運算式是次要資料源的欄位）。"""
    ctx = " context='true'" if context else ""
    return (
        f"<filter class='categorical' column='{target_ref(YEAR[1:-1])}'{ctx}>"
        f"<groupfilter count='{count}' end='{end}' function='end' units='records'>"
        "<groupfilter direction='DESC' expression='SUM([Target])' function='order'>"
        f"<groupfilter function='level-members' level='{YEAR}' /></groupfilter></groupfilter>"
        "</filter>"
    )


def year_condition(expression: str) -> str:
    return (
        f"<filter class='categorical' column='{target_ref(YEAR[1:-1])}'>"
        f"<groupfilter expression='{expression}' function='filter'>"
        f"<groupfilter function='level-members' level='{YEAR}' /></groupfilter></filter>"
    )


def by_region(ir: IR, folder: Path) -> dict[object, tuple[object, ...]]:
    ids = fields(ir)
    ws = ir.worksheets[0]
    found = execute_query(folder / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id)).records()
    target, sales = f"sum({ids['Targets.Target']})", f"sum({ids['Sales']})"
    return {r[ids["Region"]]: (r[sales], r[target]) for r in found}


# ---------------------------------------------------------------- 條件與 Top N


def test_top_n_on_secondary_dimension(tmp_path: Path, folder: Path) -> None:
    """依 SUM(Target) 取前 1 個年份（在次要資料源中計算：2023 的 1200 大於 2024 的 1149）：目標只合計 2023 年
    的列；連結值沒有對應到通過篩選的資料的標記（South 只有 2024、North 沒有目標）不顯示。"""
    ir = parse(tmp_path, folder, filtered_sheet(top_years(1)), secondary_columns=YEAR_TEXT)
    assert ir.unsupported == []
    (flt,) = ir.worksheets[0].filters
    assert isinstance(flt, TopNFilter) and flt.field.field == fields(ir)["Targets.Year Text"]
    assert by_region(ir, folder) == {"East": (350.0, 300.0), "West": (800.0, 900.0)}
    # 後 1 個：2024
    ir = parse(
        tmp_path, folder, filtered_sheet(top_years(1, "bottom")), secondary_columns=YEAR_TEXT
    )
    assert by_region(ir, folder) == {"East": (350.0, 100.0), "South": (100.0, 50.0)}


def test_condition_on_secondary_dimension(tmp_path: Path, folder: Path) -> None:
    """條件 SUM([Target]) < 1150（在次要資料源中以各年份的目標合計判斷）：只有 2024 年。"""
    sheet = filtered_sheet(year_condition("SUM([Target]) &lt; 1150"))
    ir = parse(tmp_path, folder, sheet, secondary_columns=YEAR_TEXT)
    assert ir.unsupported == []
    (flt,) = ir.worksheets[0].filters
    assert isinstance(flt, ConditionFilter)
    assert by_region(ir, folder) == {"East": (350.0, 100.0), "South": (100.0, 50.0)}


def test_context_filter_on_secondary_is_flagged(tmp_path: Path, folder: Path) -> None:
    """次要資料源欄位的內容篩選：Tableau 只能把主要資料源欄位的篩選加入內容，列為不支援並說明。"""
    sheet = filtered_sheet(top_years(1, context=True))
    ir = parse(tmp_path, folder, sheet, secondary_columns=YEAR_TEXT)
    assert any("次要資料源的內容篩選尚不支援" in u.reason for u in ir.unsupported)
    assert any("加入內容" in u.reason for u in ir.unsupported)


# ---------------------------------------------------------------- 次要資料源的 LOD


def lod_sheet(
    secondary: list[str],
    view_extra: str = "",
    shown: tuple[str, ...] = ("usr:Calculation_share:qk", "avg:Calculation_peryear:qk"),
) -> str:
    deps = (
        f"<datasource-dependencies datasource='{TARGETS}'>{''.join(secondary)}"
        "</datasource-dependencies>"
    )
    encodings = "".join(f"<text column='{target_ref(inst)}' />" for inst in shown)
    return worksheet(
        "Lod",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=encodings,
        datasources=SOURCES,
        view_extra=deps + view_extra,
    )


def test_lod_in_secondary_calculation(tmp_path: Path, folder: Path) -> None:
    """次要資料源的計算欄位中的 LOD 在次要資料源的查詢中計算：FIXED 是全部目標的合計 2349（含主要資料源
    沒有的 Central），INCLUDE [Order Date] 是各連結值各日期的合計再平均（East：300 與 100 → 200）。

    Tableau 說明文件「Troubleshoot Data Blending」：用到次要資料源的 LOD 時不能有次要資料源的維度（含維度
    篩選），Tableau 也顯示錯誤；本平台同樣不給出結果（不是「尚不支援」）。"""
    ir = parse(tmp_path, folder, lod_sheet([SHARE, PER_YEAR]), secondary_columns=LOD_CALCS)
    assert ir.unsupported == []
    ids = fields(ir)
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    found = execute_query(folder / f"{ws.datasource}.duckdb", query).records()
    share = ids["Targets.Target Share"]
    per_year = f"avg({ids['Targets.Per Year']})"
    result = {r[ids["Region"]]: (r[share], r[per_year]) for r in found}
    assert result.keys() == {"East", "West", "South", "North"}
    assert result["East"] == (pytest.approx(400 / 2349), 200.0)
    assert result["West"] == (pytest.approx(900 / 2349), 900.0)
    assert result["South"] == (pytest.approx(50 / 2349), 50.0)
    assert result["North"] == (None, None)
    # 加上次要資料源的維度與維度篩選
    year = instance("[Calculation_year]", "None", YEAR)
    sheet = lod_sheet([SHARE, PER_YEAR, year], year_filter(["2024"]))
    ir = parse(tmp_path, folder, sheet, secondary_columns=LOD_CALCS)
    assert ir.unsupported == []
    with pytest.raises(QueryError, match="Tableau 也不能這樣混合") as info:
        worksheet_sql(ir, ir.worksheets[0].id)
    assert not info.value.unsupported


def test_lod_in_secondary_table_calculation(tmp_path: Path, folder: Path) -> None:
    """次要資料源的 Table Calculation 計算欄位中的 LOD（公式在主要資料源的查詢中展開）：其中的彙總
    MIN({FIXED : SUM([Target])}) 是外部彙總，LOD 與它一起在次要資料源的查詢中計算（2349）；WINDOW_SUM 在
    混合後的結果上計算（預設的「表格（橫向）」：地區在列上，每一列各自一個範圍）。North 沒有目標：Null。"""
    sheet = lod_sheet([WINDOW_SHARE], shown=("usr:Calculation_wshare:qk",))
    ir = parse(tmp_path, folder, sheet, secondary_columns=LOD_CALCS)
    assert ir.unsupported == []
    ids = fields(ir)
    ws = ir.worksheets[0]
    (tc,) = ws.table_calcs
    found = execute_query(folder / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id)).records()
    share = f"{ids['Targets.Window Share']}#{tc.id}"
    result = {r[ids["Region"]]: r[share] for r in found}
    assert result == {
        "East": pytest.approx(400 / 2349),
        "South": pytest.approx(50 / 2349),
        "West": pytest.approx(900 / 2349),
        "North": None,
    }


# ---------------------------------------------------------------- 全域篩選


def shared_sheet(name: str) -> str:
    year = instance("[Calculation_year]", "None", YEAR)
    deps = (
        f"<datasource-dependencies datasource='{TARGETS}'>{TARGET}{year}</datasource-dependencies>"
    )
    return worksheet(
        name,
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
        datasources=SOURCES,
        view_extra=deps + f"<slices><column>{target_ref(YEAR[1:-1])}</column></slices>",
    )


def test_secondary_filter_applied_to_all_sheets_of_the_primary(
    tmp_path: Path, folder: Path
) -> None:
    """混合時「套用到所有使用此資料源的工作表」：Tableau 依工作表的主要資料源套用（說明文件「Apply Filters
    to Multiple Worksheets」），篩選存在主要資料源的 <shared-view>，欄位是次要資料源的欄位。兩張以 Orders
    為主要資料源的工作表都以次要資料源的篩選篩選，且是同一篩選群組。"""
    year = instance("[Calculation_year]", "None", YEAR)
    shared = f"""
    <shared-views>
      <shared-view name='{DS}'>
        <datasources>{SOURCES}</datasources>
        <datasource-dependencies datasource='{TARGETS}'>{year}</datasource-dependencies>
        {year_filter(["2024"])}
      </shared-view>
    </shared-views>"""
    xml = b.workbook(
        b.extract_datasource() + targets_datasource(YEAR_TEXT),
        worksheets=shared_sheet("One") + shared_sheet("Two"),
        extra=shared,
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == []
    ids = fields(ir)
    groups = set()
    for ws in ir.worksheets:
        (flt,) = ws.filters
        assert isinstance(flt, CategoricalFilter)
        assert (flt.field.field, flt.values) == (ids["Targets.Year Text"], ["2024"])
        groups.add(flt.group)
    assert groups == {f"shared:{ir.datasources[0].id}:{ids['Targets.Year Text']}"}
    parse(tmp_path, folder, filtered_sheet(""), secondary_columns=YEAR_TEXT)  # 建立資料檔
    one = ir.model_copy(update={"worksheets": ir.worksheets[:1]})
    assert by_region(one, folder) == {"East": (350.0, 100.0), "South": (100.0, 50.0)}


def test_global_filter_of_the_secondary_source_is_flagged(tmp_path: Path, folder: Path) -> None:
    """次要資料源自己的全域篩選（存在它的 <shared-view>）：Tableau 依工作表的主要資料源套用全域篩選，不會
    套用到以它為次要資料源的工作表；篩選架上卻列出時無法確認 Tableau 的處理，列為不支援並說明。"""
    year = instance("[Calculation_year]", "None", YEAR)
    shared = f"""
    <shared-views>
      <shared-view name='{DS}'>
        <datasources><datasource caption='Orders' name='{DS}' /></datasources>
      </shared-view>
      <shared-view name='{TARGETS}'>
        <datasources><datasource caption='Targets' name='{TARGETS}' /></datasources>
        <datasource-dependencies datasource='{TARGETS}'>{year}</datasource-dependencies>
        {year_filter(["2024"])}
      </shared-view>
    </shared-views>"""
    xml = b.workbook(
        b.extract_datasource() + targets_datasource(YEAR_TEXT),
        worksheets=shared_sheet("One"),
        extra=shared,
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert any("全域篩選依工作表的主要資料源套用" in u.reason for u in ir.unsupported)
