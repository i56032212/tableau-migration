"""合併集合（Tableau 的「建立合併集合」）：.twb 的寫法、IN/OUT、篩選架與計算欄位中的合併集合。

寫法取自 GitHub 上公開的 .twb：``<group caption=… delimiter=',' name='[Set 1]' name-style='unqualified'>``
之下 ``<groupfilter function='union'|'intersection'|'except'>`` 與兩個
``<groupfilter field='[集合]' function='reference' />``（Sets2、assignment_2、top 10 startup、28그룹과집합 等；
except 的「Top4~5」是 Top5 扣掉 Top3，所以是第一個集合扣掉第二個）。在檢視上選取標記建立的集合寫成
user:ui-builder='lasso-group'，與固定成員的集合相同。

測試資料見 test_query.py：各區銷售 East 350、West 800、South 100、North 60。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.ir import SetCombined
from tmig.ir.integrity import check_references

from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet
from ..parser.test_extensions import parse, reasons
from ..parser.test_sets import field, group, member, top_n
from .test_query import REGION, SALES, Run, db, run

__all__ = ["db", "run"]  # pytest fixture

COASTS = group(
    "Coasts",
    "<groupfilter function='union'>"
    + member("[Region]", "East")
    + member("[Region]", "West")
    + "</groupfilter>",
)
SOUTH_WEST = group(
    "South West",
    "<groupfilter function='union'>"
    + member("[none:Region:nk]", "West")
    + member("[none:Region:nk]", "South")
    + "</groupfilter>",
    builder="lasso-group",
)


def combined(name: str, function: str, left: str, right: str) -> str:
    return (
        f"<group caption='{name}' delimiter=',' name='[{name}]' name-style='unqualified'>"
        f"<groupfilter function='{function}'>"
        f"<groupfilter field='[{left}]' function='reference' />"
        f"<groupfilter field='[{right}]' function='reference' />"
        "</groupfilter></group>"
    )


# 合併集合寫在來源的集合之前（公開報表中常見），也可以合併另一個合併集合
GROUPS = (
    combined("Both", "union", "Coasts", "South West")
    + combined("Shared", "intersection", "Coasts", "South West")
    + combined("Coasts Only", "except", "Coasts", "South West")
    + combined("South West Only", "except", "South West", "Coasts")
    + combined("Shared or Top", "union", "Shared", "Top Regions")
    + COASTS
    + SOUTH_WEST
    + top_n("Top Regions", count="1")
)


def in_out(name: str) -> str:
    """各區的銷售額，合併集合的 IN/OUT 放在顏色。"""
    io = instance(f"[{name}]", "InOut", f"[io:{name}:nk]")
    return worksheet(
        "S",
        [REGION, SALES, io],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<color column='{ref(f'io:{name}:nk')}' />",
    )


def test_combined_sets_are_parsed(tmp_path: Path) -> None:
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extra=GROUPS)))
    assert reasons(ir) == []
    assert check_references(ir) == []
    coasts, south_west = field(ir, "Coasts"), field(ir, "South West")
    assert south_west.set is not None and south_west.set.rule.kind == "members"
    expected = {
        "Both": ("union", coasts.id, south_west.id, "[Coasts] OR [South West]"),
        "Shared": ("intersect", coasts.id, south_west.id, "[Coasts] AND [South West]"),
        "Coasts Only": ("left_only", coasts.id, south_west.id, "[Coasts] AND NOT [South West]"),
        "South West Only": (
            "left_only",
            south_west.id,
            coasts.id,
            "[South West] AND NOT [Coasts]",
        ),
    }
    for caption, (op, left, right, formula) in expected.items():
        found = field(ir, caption)
        assert found.set is not None and found.calc is not None
        assert found.set.rule == SetCombined.model_validate(
            {"kind": "combined", "left": left, "right": right, "op": op}
        )
        assert found.set.levels == coasts.set.levels if coasts.set else False
        assert (found.datatype, found.calc.formula) == ("boolean", formula)
        assert [a.label for a in found.aliases] == ["In", "Out"]
    nested = field(ir, "Shared or Top")
    assert nested.set is not None and nested.set.rule.kind == "combined"


@pytest.mark.parametrize(
    ("name", "members"),
    [
        ("Both", {"East", "West", "South"}),
        ("Shared", {"West"}),
        ("Coasts Only", {"East"}),
        ("South West Only", {"South"}),
        # 共同的成員（West）或銷售最高的一區（West）
        ("Shared or Top", {"West"}),
    ],
)
def test_combined_set_membership(run: Run, name: str, members: set[str]) -> None:
    rows = run(in_out(name), ds_extra=GROUPS)
    key = f"f_{name.lower().replace(' ', '_')}"
    assert {r["f_region"] for r in rows if r[key]} == members
    assert {r["f_region"] for r in rows if not r[key]} == {
        "East",
        "West",
        "South",
        "North",
    } - members


def test_combined_set_with_a_top_n_source(run: Run) -> None:
    """來源是 Top N 集合：銷售最高的兩區（West、East）中不在 South West 的（East）。"""
    groups = combined("Top Not SW", "except", "Top Regions", "South West") + SOUTH_WEST
    rows = run(in_out("Top Not SW"), ds_extra=groups + top_n("Top Regions"))
    assert {r["f_region"] for r in rows if r["f_top_not_sw"]} == {"East"}


def test_combined_set_on_the_filter_shelf_and_in_a_calc(run: Run) -> None:
    """放在篩選架上只保留 In；計算欄位中是布林值。"""
    sheet = worksheet(
        "S",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=f"<filter class='categorical' column='{ref('Both')}' />",
    )
    rows = run(sheet, ds_extra=GROUPS)
    assert sorted(r["f_region"] for r in rows) == ["East", "South", "West"]

    shared_sales = calc("Calculation_9", "Shared Sales", "IF [Shared] THEN [Sales] ELSE 0 END")
    key = "sum:Calculation_9:qk"
    sheet = worksheet(
        "S",
        [REGION, instance("[Calculation_9]", "Sum", f"[{key}]", "quantitative")],
        rows=ref("none:Region:nk"),
        cols=ref(key),
    )
    rows = run(sheet, calcs=shared_sales, ds_extra=GROUPS)
    assert {r["f_region"]: r["sum(f_shared_sales)"] for r in rows} == {
        "East": pytest.approx(0.0),
        "West": pytest.approx(800.0),
        "South": pytest.approx(0.0),
        "North": pytest.approx(0.0),
    }


def test_combined_sets_that_cannot_be_converted(tmp_path: Path) -> None:
    """來源不在同一個維度上、來源不存在，與其他寫法（例如不同維度的集合以 crossjoin 組合）：列為不支援
    （沒有用到的其他寫法略過）。"""
    categories = group("Furniture Set", member("[Category]", "Furniture"), builder="filter-group")
    odd = (
        "<group caption='Odd' delimiter=',' name='[Odd]' name-style='unqualified'>"
        "<groupfilter function='union'><groupfilter function='crossjoin'>"
        "<groupfilter field='[Coasts]' function='reference' />"
        "<groupfilter function='hierarchy-members' hierarchy='[io:Coasts:nk]' />"
        "</groupfilter><groupfilter field='[Coasts]' function='reference' /></groupfilter></group>"
    )
    groups = (
        COASTS
        + categories
        + combined("Mixed", "union", "Coasts", "Furniture Set")
        + combined("Ghost", "intersection", "Coasts", "Missing Set")
        + odd
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extra=groups)))
    assert reasons(ir) == [
        "合併集合「Mixed」的兩個集合（[Coasts]、[Furniture Set]）不在同一個維度上",
        "合併集合「Ghost」的來源集合（[Missing Set]）無法移轉或不是集合",
    ]
    assert any(u.reason.startswith("認不得的群組") for u in ir.ignored)


def test_census_counts_only_unsupported_groups(tmp_path: Path) -> None:
    """盤點：合併集合與選取標記建立的集合（lasso-group）已支援，不算在「群組或集合」中。"""
    from tmig.census import run_census

    other = "<group name='[Other]' name-style='unqualified' user:ui-builder='mystery' />"
    path = b.write_twb(tmp_path, b.workbook(b.extract_datasource(extra=GROUPS + other)))
    (info,) = [d for d in run_census(path).datasources if d.caption == "Orders"]
    assert info.groups_or_sets == 1


def test_set_shown_as_members_is_unsupported(tmp_path: Path) -> None:
    """集合直接放在架上（沒有 IN/OUT 的欄位實例）是「顯示集合中的成員」：列出成員、不是 In／Out，尚不支援。"""
    sheet = worksheet(
        "S",
        [REGION, SALES],
        rows=ref("Both"),
        cols=ref("sum:Sales:qk"),
        view_extra=f"<filter class='categorical' column='{ref('Both')}' />",
    )
    ir = parse(tmp_path, b.workbook(b.extract_datasource(extra=GROUPS), worksheets=sheet))
    assert reasons(ir) == ["rows 架：集合「Both」以成員顯示（顯示集合中的成員）v0 不支援"]
