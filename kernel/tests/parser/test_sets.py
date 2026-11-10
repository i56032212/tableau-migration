"""集合（Set）：<group user:ui-builder='filter-group'> → 布林欄位（In／Out）。

寫法取自公開報表：Assignment 6、Practice Activity Number 6（Top 5 的州，IN/OUT 放在顏色，欄位實例上
有別名 In top 5／Not in Top 5）、Logshark 的 Prep（Top N 的數量是參數，集合放在篩選架上）、
document-api-python 的 filtering.twb（固定成員，層級寫成欄位實例 [none:…:nk]）。
"""

from __future__ import annotations

from pathlib import Path

from tmig.ir import IR, CategoricalFilter, FieldUse, ParamRef

from . import builder as b
from .builder import ref, worksheet
from .test_extensions import BASIC, parse, reasons


def top_n(
    name: str,
    count: str = "2",
    end: str = "top",
    direction: str = "DESC",
    by: str = "SUM([Sales])",
    extra: str = "",
) -> str:
    return (
        f"<group caption='{name}' name='[{name}]' name-style='unqualified' user:ui-builder='filter-group'>"
        f"<groupfilter count='{count}' end='{end}' function='end' units='records' user:ui-marker='end'{extra}>"
        f"<groupfilter direction='{direction}' expression='{by}' function='order' user:ui-marker='order'>"
        "<groupfilter function='level-members' level='[Region]' user:ui-enumeration='all' />"
        "</groupfilter></groupfilter></group>"
    )


def group(name: str, body: str, builder: str = "filter-group") -> str:
    return (
        f"<group caption='{name}' name='[{name}]' name-style='unqualified' user:ui-builder='{builder}'>"
        f"{body}</group>"
    )


def member(level: str, value: str) -> str:
    return f"<groupfilter function='member' level='{level}' member='&quot;{value}&quot;' />"


def workbook(groups: str, sheet: str = "") -> str:
    return b.workbook(b.extract_datasource(extra=groups), worksheets=sheet)


def field(ir: IR, caption: str):
    return next(f for f in ir.datasources[0].fields if f.caption == caption)


def test_top_n_set(tmp_path: Path) -> None:
    ir = parse(tmp_path, workbook(top_n("Top Regions")))
    assert reasons(ir) == []
    found = field(ir, "Top Regions")
    assert (found.id, found.datatype, found.role, found.calc, found.column) == (
        "f_top_regions",
        "boolean",
        "dimension",
        None,
        None,
    )
    assert found.set is not None
    assert found.set.levels == [FieldUse(field="f_region")]
    rule = found.set.rule
    assert rule.kind == "top_n"
    assert (rule.direction, rule.count, rule.by, rule.mode) == ("top", 2, "SUM([Sales])", "all")
    # 值顯示為 In／Out
    assert [(a.value, a.label) for a in found.aliases] == [(True, "In"), (False, "Out")]


def test_top_n_set_variants(tmp_path: Path) -> None:
    groups = "".join(
        [
            top_n("By Param", count="[Parameters].[Parameter 1]"),
            top_n("Bottom", end="bottom"),
            top_n("Smallest", direction="ASC"),
        ]
    )
    ir = parse(tmp_path, workbook(groups))
    assert reasons(ir) == []
    by_param = field(ir, "By Param").set
    assert by_param is not None and by_param.rule.kind == "top_n"
    assert by_param.rule.count == ParamRef(parameter="p_top_n")
    # 依排序方向與取的一端：bottom＋DESC、top＋ASC 都是取最小的 N 個
    for name in ("Bottom", "Smallest"):
        found = field(ir, name).set
        assert found is not None and found.rule.kind == "top_n"
        assert found.rule.direction == "bottom"


def test_condition_set(tmp_path: Path) -> None:
    body = (
        "<groupfilter expression='SUM([Profit]) &gt; 0' function='filter' user:ui-marker='filter'>"
        "<groupfilter function='level-members' level='[Region]' /></groupfilter>"
    )
    ir = parse(tmp_path, workbook(group("Profitable", body)))
    assert reasons(ir) == []
    found = field(ir, "Profitable").set
    assert found is not None and found.rule.kind == "condition"
    assert (found.rule.condition, found.rule.mode) == ("SUM([Profit]) > 0", "all")


def test_member_sets_become_formulas(tmp_path: Path) -> None:
    groups = "".join(
        [
            group(
                "Coasts",
                "<groupfilter function='union'>"
                + member("[Region]", "East")
                + member("[none:Region:nk]", "West")
                + "</groupfilter>",
            ),
            group(
                "Not East",
                "<groupfilter function='except'><groupfilter function='level-members' level='[Region]' />"
                + member("[Region]", "East")
                + "</groupfilter>",
            ),
            group("Empty", "<groupfilter function='empty-level' member='[Region]' />"),
            group(
                "Pairs",
                "<groupfilter function='union'><groupfilter function='crossjoin'>"
                + member("[Region]", "East")
                + member("[Category]", "Furniture")
                + "</groupfilter><groupfilter function='crossjoin'>"
                + member("[Region]", "West")
                + member("[Category]", "Technology")
                + "</groupfilter></groupfilter>",
            ),
            group(
                "Recent",
                "<groupfilter function='member' level='[yr:Order Date:ok]' member='2024' />",
            ),
        ]
    )
    ir = parse(tmp_path, workbook(groups))
    assert reasons(ir) == []
    coasts = field(ir, "Coasts")
    assert coasts.calc is not None and coasts.set is not None
    assert coasts.calc.formula == '(NOT ISNULL([Region]) AND [Region] IN ("East", "West"))'
    assert coasts.set.rule.kind == "members"
    assert (coasts.set.rule.mode, coasts.set.rule.members) == ("include", [["East"], ["West"]])

    not_east = field(ir, "Not East")
    assert not_east.calc is not None and not_east.set is not None
    assert not_east.calc.formula == 'NOT ((NOT ISNULL([Region]) AND [Region] = "East"))'
    assert not_east.set.rule.kind == "members" and not_east.set.rule.mode == "exclude"

    empty = field(ir, "Empty")
    assert empty.calc is not None and empty.calc.formula == "FALSE"

    pairs = field(ir, "Pairs")
    assert pairs.set is not None and pairs.calc is not None
    assert [u.field for u in pairs.set.levels] == ["f_region", "f_category"]
    assert pairs.set.rule.kind == "members"
    assert pairs.set.rule.members == [["East", "Furniture"], ["West", "Technology"]]
    assert " OR " in pairs.calc.formula and '[Category] = "Technology"' in pairs.calc.formula

    recent = field(ir, "Recent")
    assert recent.calc is not None and recent.set is not None
    assert recent.calc.formula == "(NOT ISNULL(YEAR([Order Date])) AND YEAR([Order Date]) = 2024)"
    assert recent.set.levels[0].date is not None


def test_sets_on_a_worksheet(tmp_path: Path) -> None:
    """IN/OUT 放在列架上（欄位實例上的別名取代 In／Out）；集合放在篩選架上＝只保留 In。"""
    io = (
        "<column-instance column='[Top Regions]' derivation='InOut' name='[io:Top Regions:nk]' "
        "pivot='key' type='nominal'><aliases><alias key='false' value='Others' />"
        "<alias key='true' value='Top 2' /></aliases></column-instance>"
    )
    sheet = worksheet(
        "S",
        [*BASIC, io],
        rows=ref("io:Top Regions:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=f"<filter class='categorical' column='{ref('Coasts')}' />",
    )
    coasts = group(
        "Coasts",
        "<groupfilter function='union'>"
        + member("[Region]", "East")
        + member("[Region]", "West")
        + "</groupfilter>",
    )
    ir = parse(tmp_path, workbook(top_n("Top Regions") + coasts, sheet))
    assert reasons(ir) == []
    ws = ir.worksheets[0]
    assert ws.rows == [FieldUse(field="f_top_regions")]
    assert [(a.value, a.label) for a in field(ir, "Top Regions").aliases] == [
        (True, "Top 2"),
        (False, "Others"),
    ]
    (flt,) = ws.filters
    assert isinstance(flt, CategoricalFilter)
    assert (flt.field.field, flt.mode, flt.values) == ("f_coasts", "include", [True])


def test_unsupported_sets(tmp_path: Path) -> None:
    groups = "".join(
        [
            top_n("Percent", extra="").replace("units='records'", "units='percent'"),
            top_n("Row Level", by="[Sales]"),
            group("Odd", "<groupfilter function='intersection' />"),
            # 合併欄位（nest-group）不是集合：轉成字串的計算欄位
            group(
                "Combined",
                "<groupfilter function='crossjoin'><groupfilter function='level-members' level='[Region]' />"
                "<groupfilter function='level-members' level='[Category]' /></groupfilter>",
                builder="nest-group",
            ),
        ]
    )
    io = "<column-instance column='[Region]' derivation='InOut' name='[io:Region:nk]' pivot='key' type='nominal' />"
    sheet = worksheet("S", [*BASIC, io], rows=ref("io:Region:nk"), cols=ref("sum:Sales:qk"))
    ir = parse(tmp_path, workbook(groups, sheet))
    assert sorted(reasons(ir)) == sorted(
        [
            "集合「Percent」：Top N 集合的單位 percent 不支援",
            "集合「Row Level」的排名依據「[Sales]」：排名依據必須是彙總",
            "集合「Odd」：集合的成員定義（function=intersection）不支援",
            "rows 架：[Region] 不是集合，卻使用 IN/OUT",
        ]
    )
    combined = next(f for f in ir.datasources[0].fields if f.id == "f_combined")
    assert combined.calc is not None
    assert (
        combined.calc.formula
        == 'IFNULL(STR([Region]), "Null") + ", " + IFNULL(STR([Category]), "Null")'
    )


# ---------------------------------------------------------------- 集合動作


def set_action(
    target: str = "Coasts",
    behavior: str | None = "assign",
    clearing: str | None = "show-all",
    name: str = "[Action1]",
) -> str:
    marks = (
        f"<_.fcp.GroupActionAddRemove.true...add-or-remove-marks value='{behavior}' />"
        if behavior is not None
        else ""
    )
    clear = (
        f"<param name='selection-clear-set-option' value='{clearing}' />"
        if clearing is not None
        else ""
    )
    return (
        f"<edit-group-action caption='Pick {name}' name='{name}'><activation type='on-select' />"
        "<source dashboard='Board' type='sheet' worksheet='Source' />"
        f"{marks}<params>{clear}<param name='target-group' value='{ref(target)}' /></params>"
        "</edit-group-action>"
    )


BOARD = """
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones><zone h='100000' id='2' name='Source' w='100000' x='0' y='0' /></zones>
    </dashboard>"""
COASTS = group(
    "Coasts",
    "<groupfilter function='union'>"
    + member("[Region]", "East")
    + member("[Region]", "West")
    + "</groupfilter>",
)


def action_workbook(actions: str) -> str:
    sheet = worksheet("Source", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    return b.workbook(
        b.extract_datasource(extra=COASTS + top_n("Top Regions")),
        worksheets=sheet,
        dashboards=BOARD,
        actions=actions,
    )


def test_set_actions(tmp_path: Path) -> None:
    """公開範例 filtering.twb 的寫法：assign 與 show-all；另有加入、移除與保留（沒寫清除方式）。"""
    actions = "".join(
        [
            set_action(),
            set_action(behavior="add", clearing=None, name="[Action2]"),
            set_action(behavior="remove", clearing="show-none", name="[Action3]"),
            set_action(behavior=None, clearing="keep", name="[Action4]"),
        ]
    )
    ir = parse(tmp_path, action_workbook(actions))
    assert reasons(ir) == []
    found = [(a.kind, a.set_action, a.sources, a.dashboard, a.activation) for a in ir.actions]
    assert [(f[1].target, f[1].behavior, f[1].clearing) for f in found if f[1] is not None] == [
        ("f_coasts", "assign", "add_all"),
        ("f_coasts", "add", "keep"),
        ("f_coasts", "remove", "remove_all"),
        ("f_coasts", "assign", "keep"),
    ]
    assert {(k, tuple(s), d, a) for k, _, s, d, a in found} == {
        ("set", ("ws_source",), "db_board", "select")
    }


def test_unsupported_set_actions(tmp_path: Path) -> None:
    actions = "".join(
        [
            set_action(target="Top Regions"),
            set_action(target="Nope", name="[Action2]"),
            set_action(clearing="mystery", name="[Action3]"),
        ]
    )
    ir = parse(tmp_path, action_workbook(actions))
    assert reasons(ir) == [
        f"集合動作「Pick [Action1]」的目標 {ref('Top Regions')} 不是可以移轉的固定成員集合",
        f"集合動作「Pick [Action2]」的目標 {ref('Nope')} 不是可以移轉的固定成員集合",
        "集合動作「Pick [Action3]」的執行方式 assign、清除方式 mystery v0 不支援",
    ]
    assert ir.actions == []
