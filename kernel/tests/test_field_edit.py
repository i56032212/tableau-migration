"""資料窗格的欄位選單（tmig.field_edit）：複製、別名、群組、數值分組、集合、預設屬性與刪除；每一步匯入
後以 DuckDB 查詢，與手算的結果比對。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tmig.authoring import inspect_file, new_workbook, update_field
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_sources
from tmig.edit import EditError, remove_calc, replace_worksheet, upsert_calc, upsert_parameter
from tmig.field_edit import (
    duplicate_field,
    remove_field,
    set_aliases,
    set_field_properties,
    upsert_bins,
    upsert_group,
    upsert_set,
)
from tmig.ir import IR, MemberAlias, ParamRef, SetCondition, SetMembers, SetTop
from tmig.ir.integrity import check_references
from tmig.sqlgen.query import worksheet_sql

ORDERS = (
    "Region,Category,Sales,Quantity\n"
    "East,Furniture,100.5,2\n"
    "West,Office,200,1\n"
    "East,Office,50.25,3\n"
    "South,Tech,75,1\n"
    ",Tech,10,1\n"
)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[IR, Path, Path]:
    path = tmp_path / "orders.csv"
    path.write_text(ORDERS, encoding="utf-8")
    sources = tmp_path / "sources"
    (sources / "Data").mkdir(parents=True)
    shutil.copy(path, sources / "Data" / "orders.csv")
    ir = new_workbook("Orders", inspect_file(path), None, "Data/orders.csv")
    return ir, sources, tmp_path / "data"


def ids(ir: IR) -> dict[str, str]:
    return {f.caption: f.id for f in ir.datasources[0].fields}


def field(ir: IR, caption: str) -> Any:
    return next(f for f in ir.datasources[0].fields if f.caption == caption)


def place(ir: IR, rows: str, measure: str, agg: str = "sum") -> IR:
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[{"field": rows}], columns=[{"field": measure, "agg": agg}])
    return replace_worksheet(ir, sheet)[0]


def query(
    ir: IR, sources: Path, out: Path, rows: str, measure: str, agg: str = "sum"
) -> dict[Any, Any]:
    assert check_references(ir) == []
    ir = place(ir, rows, measure, agg)
    if out.exists():
        shutil.rmtree(out)
    ds = ir.datasources[0].id
    import_sources(sources, out, ir)
    result = execute_query(out / f"{ds}.duckdb", worksheet_sql(ir, ir.worksheets[0].id))
    return {row[0]: row[1] for row in result.rows}


def test_duplicate_and_properties(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    ir, copy = duplicate_field(ir, ds, f["Sales"])
    # 與 Tableau 相同：一般欄位的複本是公式為 [欄位] 的計算欄位，角色與彙總照樣帶著
    assert copy.caption == "Sales (copy)" and copy.calc is not None
    assert copy.calc.formula == "[Sales]" and (copy.role, copy.default_agg) == ("measure", "sum")
    assert query(ir, sources, out, f["Region"], copy.id) == {
        "East": 150.75,
        "West": 200,
        "South": 75,
        None: 10,
    }
    ir, again = duplicate_field(ir, ds, f["Sales"])
    assert again.caption == "Sales (copy) 2"
    ir, region = duplicate_field(ir, ds, f["Region"])
    assert (region.role, region.datatype) == ("dimension", "string")
    # 預設屬性：格式與註解（空白為清除）
    ir = set_field_properties(ir, ds, f["Sales"], format='c"$"#,##0.00', comment=" 含稅 ")
    assert (field(ir, "Sales").format, field(ir, "Sales").comment) == ('c"$"#,##0.00', "含稅")
    ir = set_field_properties(ir, ds, f["Sales"], comment="")
    assert field(ir, "Sales").comment is None and field(ir, "Sales").format == 'c"$"#,##0.00'


def test_aliases(setup: tuple[IR, Path, Path]) -> None:
    ir, _, _ = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    aliases = [
        MemberAlias(value="East", label="東部"),
        MemberAlias(value="West", label=" "),  # 空白的別名：不設定
        MemberAlias(value=None, label="未填"),
    ]
    ir = set_aliases(ir, ds, f["Region"], aliases)
    assert [(a.value, a.label) for a in field(ir, "Region").aliases] == [
        ("East", "東部"),
        (None, "未填"),
    ]
    with pytest.raises(EditError, match="不是「Region」的值"):
        set_aliases(ir, ds, f["Region"], [MemberAlias(value=1, label="一")])
    with pytest.raises(EditError, match="不是離散的維度"):
        set_aliases(ir, ds, f["Sales"], [MemberAlias(value=1.0, label="一")])
    with pytest.raises(EditError, match="重複"):
        set_aliases(ir, ds, f["Region"], [MemberAlias(value="East", label="A")] * 2)


def test_groups(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    ir, group = upsert_group(ir, ds, f["Region"], "", [("Coasts", ["East", "West"])])
    assert group.caption == "Region (group)" and group.group is not None
    assert group.group.source == f["Region"] and group.group.other is None
    # 其餘成員維持原值（Null 仍是 Null）
    assert query(ir, sources, out, group.id, f["Sales"]) == {
        "Coasts": 350.75,
        "South": 75,
        None: 10,
    }
    # 修改：Null 也歸入一組，其餘成員歸入「其他」；id 與內部名稱不變
    ir, edited = upsert_group(
        ir,
        ds,
        f["Region"],
        "地區群組",
        [("Coasts", ["East", "West"]), ("未知", [None])],
        other="Other",
        field_id=group.id,
    )
    assert (edited.id, edited.name, edited.caption) == (group.id, group.name, "地區群組")
    assert query(ir, sources, out, group.id, f["Sales"]) == {
        "Coasts": 350.75,
        "Other": 75,
        "未知": 10,
    }
    with pytest.raises(EditError, match="已經在群組"):
        upsert_group(ir, ds, f["Region"], "x", [("A", ["East"]), ("B", ["East"])])
    with pytest.raises(EditError, match="不是維度"):
        upsert_group(ir, ds, f["Sales"], "x", [("A", [1.0])])
    # 數字的維度：成員依數字比對，維持原值的轉成文字
    ir = update_field(ir, ds, f["Quantity"], role="dimension")
    ir, quantity = upsert_group(ir, ds, f["Quantity"], "", [("Few", [1, 2])])
    assert query(ir, sources, out, quantity.id, f["Sales"]) == {"Few": 385.5, "3": 50.25}
    # 用到的群組不能刪；公式以顯示名稱參照時也算用到
    ir, calc = upsert_calc(ir, ds, "Coast flag", '[地區群組] = "Coasts"')
    with pytest.raises(EditError, match="Coast flag"):
        remove_field(ir, ds, group.id)
    ir = remove_calc(ir, ds, calc.id)
    with pytest.raises(EditError, match="用到"):
        remove_field(place(ir, group.id, f["Sales"]), ds, group.id)
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(rows=[], columns=[])
    ir = remove_field(replace_worksheet(ir, sheet)[0], ds, group.id)
    assert "地區群組" not in ids(ir)
    with pytest.raises(EditError, match="只能隱藏"):
        remove_field(ir, ds, f["Region"])


def test_bins(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    ir, bins = upsert_bins(ir, ds, f["Sales"], "", 100)
    assert bins.caption == "Sales (bin)" and (bins.role, bins.datatype) == ("dimension", "integer")
    assert bins.bin is not None and bins.bin.size == 100
    # 值為所在區間的下限：0–99.99 為 0、100–199.99 為 100、200 為 200
    assert query(ir, sources, out, bins.id, f["Quantity"]) == {0: 5, 100: 2, 200: 1}
    # 大小以參數決定
    ir, parameter = upsert_parameter(
        ir,
        {
            "caption": "Bin size",
            "datatype": "integer",
            "value": 60,
            "allowed": {"kind": "all"},
        },
    )
    ir, edited = upsert_bins(ir, ds, f["Sales"], "", ParamRef(parameter=parameter.id), bins.id)
    assert edited.id == bins.id
    # 60 一格：10、50.25 為 0，75、100.5 為 60，200 為 180
    assert query(ir, sources, out, bins.id, f["Quantity"]) == {0: 4, 60: 3, 180: 1}
    with pytest.raises(EditError, match="大於 0"):
        upsert_bins(ir, ds, f["Sales"], "x", 0)
    with pytest.raises(EditError, match="不是列層級的數字欄位"):
        upsert_bins(ir, ds, f["Region"], "x", 10)


def test_sets(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    members = SetMembers(kind="members", mode="include", members=[["East"], ["South"]])
    ir, fixed = upsert_set(ir, ds, [f["Region"]], "", members)
    assert fixed.caption == "Region Set" and fixed.datatype == "boolean"
    assert [a.label for a in fixed.aliases] == ["In", "Out"]
    assert query(ir, sources, out, fixed.id, f["Sales"]) == {True: 225.75, False: 210}
    # 排除：所選以外的成員在集合中（Null 不在所選中，所以在集合中）
    excluded = members.model_copy(update={"mode": "exclude"})
    ir, _ = upsert_set(ir, ds, [f["Region"]], "", excluded, fixed.id)
    assert query(ir, sources, out, fixed.id, f["Sales"]) == {True: 210, False: 225.75}
    # 條件：SUM([Sales]) > 100 的地區
    condition = SetCondition(kind="condition", condition="SUM([Sales]) > 100")
    ir, big = upsert_set(ir, ds, [f["Region"]], "Big regions", condition)
    assert big.calc is None
    assert query(ir, sources, out, big.id, f["Sales"]) == {True: 350.75, False: 85}
    # Top 1 依 SUM([Sales])
    top = SetTop(kind="top_n", direction="top", count=1, by="SUM([Sales])")
    ir, best = upsert_set(ir, ds, [f["Region"]], "Top region", top)
    assert query(ir, sources, out, best.id, f["Sales"]) == {True: 200, False: 235.75}
    # 多個維度的成員組合（檢視上選取的標記）
    pairs = SetMembers(kind="members", members=[["East", "Office"], ["South", "Tech"]])
    ir, combo = upsert_set(ir, ds, [f["Region"], f["Category"]], "Pairs", pairs)
    assert query(ir, sources, out, combo.id, f["Sales"]) == {True: 125.25, False: 310.5}
    with pytest.raises(EditError, match="必須是彙總"):
        upsert_set(
            ir, ds, [f["Region"]], "x", SetCondition(kind="condition", condition="[Sales] > 1")
        )
    with pytest.raises(EditError, match="只能有一個維度"):
        upsert_set(ir, ds, [f["Region"], f["Category"]], "x", top)
    with pytest.raises(EditError, match="不是維度"):
        upsert_set(ir, ds, [f["Sales"]], "x", members)
    # 複製集合：定義照樣帶著
    ir, copy = duplicate_field(ir, ds, big.id)
    assert copy.set == big.set and copy.caption == "Big regions (copy)"
