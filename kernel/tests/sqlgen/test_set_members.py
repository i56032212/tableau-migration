"""顯示集合中的成員（Show Members in Set）：集合直接放在欄列架或標記卡上（沒有 IN/OUT 的欄位實例）時列出
集合所在維度的成員，並以自動加上的「在集合中」篩選只留集合的成員。

Tableau 2021.2 說明文件「Create Sets」
（https://help.tableau.com/v2021.2/pro/desktop/en-us/sortgroup_sets_create.htm ，2026-11-23 查閱）。.twb 的寫法
取自 GitHub 上公開的 .twb（Fin_Complaints_Analysis 的 Top 5 Sub Product 等）：架上是 ``[資料源].[集合]``，篩選架上
有 ``<filter class='categorical' column='[資料源].[集合]' />``，座標軸的排序是
``<shelf-sort-v2 dimension-to-sort='[資料源].[集合]' …>``。

測試資料見 test_query：各區銷售 East 350、West 800、South 100、North 60；2023 年 650、2024 年 660。
"""

from __future__ import annotations

from pathlib import Path

from tmig.parser.workbook import parse_workbook
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from ..parser.test_extensions import reasons
from ..parser.test_sets import group, member, top_n
from .test_query import REGION, SALES, Run, db, run

__all__ = ["db", "run"]  # pytest fixture

TOP = top_n("Top Regions")
YEARS = group(
    "Recent", "<groupfilter function='member' level='[yr:Order Date:ok]' member='2024' />"
)
PAIRS = group(
    "Pairs",
    "<groupfilter function='union'><groupfilter function='crossjoin'>"
    + member("[Region]", "East")
    + member("[Category]", "Furniture")
    + "</groupfilter></groupfilter>",
)
SORT = (
    "<shelf-sorts><shelf-sort-v2 dimension-to-sort='"
    + ref("Top Regions")
    + "' direction='ASC' is-on-innermost-dimension='true' measure-to-sort-by='"
    + ref("sum:Sales:qk")
    + "' shelf='rows' /></shelf-sorts>"
)


def set_filter(name: str) -> str:
    """「顯示集合中的成員」自動加上的篩選（沒有 groupfilter：在集合中）。"""
    return f"<filter class='categorical' column='{ref(name)}' />"


def sheet(rows: str, view_extra: str = "", encodings: str = "") -> str:
    return worksheet(
        "S",
        [REGION, SALES, instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal")],
        rows=rows,
        cols=ref("sum:Sales:qk"),
        view_extra=view_extra,
        encodings=encodings,
    )


def test_top_n_set_lists_its_members(tmp_path: Path, run: Run) -> None:
    """Top 2 的地區：列出 West、East（不是 In／Out 兩列）；標題是集合的名稱；座標軸的排序依成員排序。"""
    xml = b.workbook(
        b.extract_datasource(extra=TOP),
        worksheets=sheet(ref("Top Regions"), set_filter("Top Regions") + SORT),
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert reasons(ir) == []
    ws = ir.worksheets[0]
    (use,) = ws.rows
    shown = next(f for f in ir.datasources[0].fields if f.id == use.field)
    assert (shown.caption, shown.datatype, shown.role, shown.hidden) == (
        "Top Regions",
        "string",
        "dimension",
        True,
    )
    assert shown.calc is not None and shown.calc.formula == "[Region]"
    # 自動加上的篩選是集合本身（在集合中）；排序改成依列出的成員
    (flt,) = ws.filters
    assert flt.kind == "categorical" and flt.field.field == "f_top_regions" and flt.values == [True]
    assert [(s.field, s.kind, s.order) for s in ws.sorts] == [(use.field, "by_field", "asc")]
    rows = run(sheet(ref("Top Regions"), set_filter("Top Regions") + SORT), ds_extra=TOP)
    assert [(r["f_top_regions_members"], r["sum(f_sales)"]) for r in rows] == [
        ("East", 350),
        ("West", 800),
    ]
    # 標題（列的欄位標籤）是集合的名稱
    from tmig.data.executor import execute_query

    query = worksheet_sql(ir, ws.id)
    result = execute_query(run.db, query)
    rendered = build_spec(ir, ws.id, query.columns, result)
    assert "Top Regions" in str(rendered.spec)


def test_members_without_the_set_filter_and_on_marks(run: Run) -> None:
    """移除了自動加上的篩選：列出維度的所有成員（推定）；集合放在色彩上：每個成員一個顏色。"""
    rows = run(sheet(ref("Top Regions")), ds_extra=TOP)
    assert {r["f_top_regions_members"] for r in rows} == {"East", "West", "South", "North"}
    colored = run(
        sheet(
            ref("none:Region:nk"),
            set_filter("Top Regions"),
            encodings=f"<color column='{ref('Top Regions')}' />",
        ),
        ds_extra=TOP,
    )
    assert {(r["f_region"], r["f_top_regions_members"]) for r in colored} == {
        ("East", "East"),
        ("West", "West"),
    }


def test_date_part_set_members(run: Run) -> None:
    """日期部分的集合（YEAR(Order Date) 是 2024）：成員是年份。"""
    rows = run(sheet(ref("Recent"), set_filter("Recent")), ds_extra=YEARS)
    assert [(r["year(f_recent_members)"], r["sum(f_sales)"]) for r in rows] == [(2024, 660)]


def test_multi_level_set_members_are_unsupported(tmp_path: Path) -> None:
    """多個維度的集合以成員顯示：各維度的值怎麼組合成一個成員沒有寫明，列為不支援。"""
    xml = b.workbook(
        b.extract_datasource(extra=PAIRS),
        worksheets=sheet(ref("Pairs"), set_filter("Pairs")),
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert any(
        "集合「Pairs」以成員顯示（顯示集合中的成員）：多個維度的集合尚不支援" in r
        for r in reasons(ir)
    ), reasons(ir)
