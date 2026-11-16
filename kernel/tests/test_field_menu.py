"""資料窗格欄位選單的其他項目：臨時計算、預設屬性（色彩、形狀、排序、總計使用）、分割、取代參照，
以及萃取欄位的變更資料類型；匯入後以 DuckDB 查詢，與手算的結果比對。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tmig.authoring import inspect_file, new_workbook, update_field
from tmig.data.executor import execute, execute_query
from tmig.data.hyper_import import import_sources
from tmig.edit import (
    EditError,
    catalog,
    replace_references,
    replace_worksheet,
    upsert_calc,
)
from tmig.field_edit import create_user_filter, set_field_properties, split_field
from tmig.ir import IR, load_ir
from tmig.ir.integrity import check_references
from tmig.planner.identity import Identity
from tmig.sqlgen.query import worksheet_sql
from tmig.verify.run import ensure_data

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "fixtures" / "superstore_features"

ORDERS = (
    "Region,Category,Code,Sales,Quantity\n"
    "East,Furniture,A-10-x,100.5,2\n"
    "West,Office,B-20-y,200,1\n"
    "East,Office,C-30,50.25,3\n"
    "South,Tech,D,75,1\n"
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


def place(ir: IR, **parts: Any) -> IR:
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(parts)
    return replace_worksheet(ir, sheet)[0]


def run(ir: IR, sources: Path, out: Path) -> list[tuple[Any, ...]]:
    assert check_references(ir) == []
    if out.exists():
        shutil.rmtree(out)
    import_sources(sources, out, ir)
    database = out / f"{ir.datasources[0].id}.duckdb"
    return execute_query(database, worksheet_sql(ir, ir.worksheets[0].id)).rows


def test_ad_hoc_calculation(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    formula = "SUM([Sales]) / SUM([Quantity])"
    ir, made = upsert_calc(ir, ds, "", f"  {formula} ", ad_hoc=True)
    # 臨時計算：名稱就是公式，資料窗格不顯示（catalog 帶著 ad_hoc，編輯器不列出）
    assert made.ad_hoc and made.caption == formula and made.calc is not None
    assert made.calc.level == "aggregate" and made.role == "measure"
    entry = next(e for e in catalog(ir)["datasources"][0]["fields"] if e["id"] == made.id)
    assert entry["ad_hoc"] is True
    # 公式相同的臨時計算只建立一個
    again_ir, again = upsert_calc(ir, ds, "", formula, ad_hoc=True)
    assert again.id == made.id and again_ir is ir
    ir = place(ir, rows=[{"field": f["Region"]}], columns=[{"field": made.id, "continuous": True}])
    assert dict(run(ir, sources, out)) == pytest.approx(
        {"East": 150.75 / 5, "West": 200.0, "South": 75.0}
    )
    # 列層級的臨時計算是量值時與一般的欄位相同加上彙總
    ir, doubled = upsert_calc(ir, ds, "", "[Sales] * 2", ad_hoc=True)
    assert (doubled.role, doubled.datatype) == ("measure", "real")
    # 在計算欄位對話框中命名之後成為資料窗格的計算欄位，工作表的參照不變
    ir, named = upsert_calc(ir, ds, "每件金額", formula, made.id)
    assert named.id == made.id and not named.ad_hoc and named.caption == "每件金額"
    with pytest.raises(EditError, match="公式有誤"):
        upsert_calc(ir, ds, "", "SUM([Nope])", ad_hoc=True)


def test_unused_ad_hoc_calculations_are_pruned(setup: tuple[IR, Path, Path]) -> None:
    ir, _, _ = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    # 沒有用到的臨時計算（膠囊拿掉之後留下的）不擋住欄位的變更：修改欄位之前清掉
    ir, doubled = upsert_calc(ir, ds, "", "[Sales] * 2", ad_hoc=True)
    ir = place(ir, rows=[{"field": f["Region"]}], columns=[{"field": doubled.id, "agg": "sum"}])
    with pytest.raises(EditError, match="用到「Sales」"):
        update_field(ir, ds, f["Sales"], role="dimension")
    ir = place(ir, columns=[])
    changed = update_field(ir, ds, f["Sales"], role="dimension")
    assert field(changed, "Sales").role == "dimension"
    assert not any(x.id == doubled.id for x in changed.datasources[0].fields)


def test_default_sort_by_field(setup: tuple[IR, Path, Path]) -> None:
    """預設排序「依欄位」：依另一個欄位的彙總值排序，排序依據不必在檢視上；預設改變時沿用它的工作表跟著改。
    各區的 SUM(Sales)：East 150.75、West 200、South 75；SUM(Quantity)：East 5、West 1、South 1。"""
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    by_sales = {"field": f["Sales"], "agg": "sum"}
    ir = set_field_properties(
        ir, ds, f["Region"], default_sort={"kind": "by_field", "order": "desc", "by": by_sales}
    )
    quantity = {"field": f["Quantity"], "agg": "sum", "continuous": True}
    ir = place(ir, rows=[{"field": f["Region"]}], columns=[quantity])
    (sort,) = ir.worksheets[0].sorts
    assert (sort.kind, sort.order, sort.nested) == ("by_field", "desc", False)
    assert sort.by is not None and (sort.by.field, sort.by.agg) == (f["Sales"], "sum")
    assert [r[0] for r in run(ir, sources, out)] == ["West", "East", "South"]
    # 預設改成遞增：沿用預設的工作表跟著改
    ir = set_field_properties(
        ir, ds, f["Region"], default_sort={"kind": "by_field", "order": "asc", "by": by_sales}
    )
    assert [(s.kind, s.order) for s in ir.worksheets[0].sorts] == [("by_field", "asc")]
    assert [r[0] for r in run(ir, sources, out)] == ["South", "East", "West"]
    # 拿掉預設：工作表回到沒有排序
    ir = set_field_properties(ir, ds, f["Region"], default_sort=None)
    assert ir.worksheets[0].sorts == []
    # 排序依據要是同一個資料源的另一個欄位、要有彙總；by 與 kind 要一致
    for bad, message in (
        ({"kind": "by_field", "by": {"field": "f_nope", "agg": "sum"}}, "不在資料源"),
        ({"kind": "by_field", "by": {"field": f["Region"], "agg": "count"}}, "自己"),
        ({"kind": "by_field", "by": {"field": f["Sales"]}}, "彙總方式"),
        ({"kind": "by_field"}, "預設屬性不正確"),
        ({"kind": "alphabetic", "by": by_sales}, "預設屬性不正確"),
    ):
        with pytest.raises(EditError, match=message):
            set_field_properties(ir, ds, f["Region"], default_sort=bad)


def test_default_sort_color_shape_and_total_using(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    region = {"field": f["Region"]}
    sales = {"field": f["Sales"], "agg": "sum", "continuous": True}
    ir = set_field_properties(
        ir, ds, f["Region"], default_sort={"kind": "alphabetic", "order": "desc"}
    )
    assert field(ir, "Region").default_sort.order == "desc"
    # 放到工作表上時沒有另外排序：依預設的排序
    ir = place(ir, rows=[region], columns=[sales])
    sheet = ir.worksheets[0]
    assert [(s.field, s.kind, s.order) for s in sheet.sorts] == [
        (f["Region"], "alphabetic", "desc")
    ]
    assert [r[0] for r in run(ir, sources, out)] == ["West", "South", "East"]
    # 預設改成手動排列：沿用預設的工作表跟著改
    ir = set_field_properties(
        ir,
        ds,
        f["Region"],
        default_sort={"kind": "manual", "manual_values": ["South", "East", "West"]},
    )
    assert ir.worksheets[0].sorts[0].kind == "manual"
    assert [r[0] for r in run(ir, sources, out)] == ["South", "East", "West"]
    # 工作表上另外排序之後，預設再改也不影響它
    data = ir.worksheets[0].model_dump(mode="json")
    data["sorts"] = [{"field": f["Region"], "kind": "alphabetic", "order": "asc"}]
    ir, _ = replace_worksheet(ir, data)
    ir = set_field_properties(ir, ds, f["Region"], default_sort=None)
    assert [(s.kind, s.order) for s in ir.worksheets[0].sorts] == [("alphabetic", "asc")]
    with pytest.raises(EditError, match="只有維度"):
        set_field_properties(ir, ds, f["Sales"], default_sort={"kind": "alphabetic"})

    # 色彩與形狀：欄位放到色彩、形狀上而工作表沒有設定時依預設
    palette = {
        "kind": "discrete",
        "palette": "tableau10_10_0",
        "assignments": [{"value": "East", "color": "#ff0000"}],
    }
    ir = set_field_properties(
        ir,
        ds,
        f["Region"],
        default_color=palette,
        default_shapes=[{"value": "East", "shape": "square"}],
    )
    data = ir.worksheets[0].model_dump(mode="json")
    data["marks"].update(color=region, shape=region, type="shape")
    ir, sheet = replace_worksheet(ir, data)
    assert sheet.marks.color_palette is not None
    assert sheet.marks.color_palette.assignments[0].color == "#ff0000"
    assert [(a.value, a.shape) for a in sheet.marks.shape_assignments] == [("East", "square")]
    # 預設的顏色改了，沿用它的工作表跟著改；清掉預設後工作表回到自動
    palette["assignments"] = [{"value": "East", "color": "#00ff00"}]
    ir = set_field_properties(ir, ds, f["Region"], default_color=palette)
    assert ir.worksheets[0].marks.color_palette.assignments[0].color == "#00ff00"  # type: ignore[union-attr]
    ir = set_field_properties(ir, ds, f["Region"], default_color=None, default_shapes=[])
    assert ir.worksheets[0].marks.color_palette is None
    assert ir.worksheets[0].marks.shape_assignments == []
    with pytest.raises(EditError, match="連續的色盤"):
        set_field_properties(ir, ds, f["Sales"], default_color=palette)

    # 總計使用：有總計的工作表上，量值的總計依預設的計算方式
    ir = set_field_properties(ir, ds, f["Sales"], total_using="avg")
    data = ir.worksheets[0].model_dump(mode="json")
    data["totals"] = ["rows"]
    ir, sheet = replace_worksheet(ir, data)
    assert [(v.use.field, v.agg) for v in sheet.visual_totals] == [(f["Sales"], "avg")]
    ir = set_field_properties(ir, ds, f["Sales"], total_using=None)
    assert ir.worksheets[0].visual_totals == []
    entry = next(e for e in catalog(ir)["datasources"][0]["fields"] if e["id"] == f["Region"])
    assert entry["default_sort"] is None and entry["default_shapes"] == []


def test_split(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    ir, made = split_field(ir, ds, f["Code"], "-", 2)
    assert [m.caption for m in made] == ["Code - Split 1", "Code - Split 2"]
    assert made[0].calc is not None and made[0].calc.formula == 'TRIM( SPLIT( [Code], "-", 1 ) )'
    ir = place(
        ir,
        rows=[{"field": made[0].id}, {"field": made[1].id}],
        columns=[{"field": f["Sales"], "agg": "sum", "continuous": True}],
    )
    assert [r[:2] for r in run(ir, sources, out)] == [
        ("A", "10"),
        ("B", "20"),
        ("C", "30"),
        ("D", ""),
    ]
    # 從最後面取：Split 1 是最後一段；名稱重複時加上編號
    ir, last = split_field(ir, ds, f["Code"], "-", 1, from_end=True)
    assert last[0].caption == "Code - Split 1 (2)"
    assert last[0].calc is not None and last[0].calc.formula.endswith(", -1 ) )")
    with pytest.raises(EditError, match="不是字串"):
        split_field(ir, ds, f["Sales"], "-", 1)
    with pytest.raises(EditError, match="1 到 10"):
        split_field(ir, ds, f["Code"], "-", 11)


def test_replace_references(setup: tuple[IR, Path, Path]) -> None:
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    ir, calc = upsert_calc(ir, ds, "Region Tag", '[Region] + "!"')
    ir = place(
        ir,
        rows=[{"field": f["Region"]}],
        columns=[{"field": f["Sales"], "agg": "sum", "continuous": True}],
        sorts=[{"field": f["Region"], "kind": "alphabetic", "order": "desc"}],
        filters=[
            {
                "id": "flt_region",
                "kind": "categorical",
                "field": {"field": f["Region"]},
                "stage": "dimension",
                "mode": "include",
                "values": ["East", "West"],
                "source_ref": "/editor",
            }
        ],
    )
    updated = replace_references(ir, ds, f["Region"], f["Category"])
    sheet = updated.worksheets[0]
    assert sheet.rows[0].field == f["Category"] and sheet.sorts[0].field == f["Category"]
    assert sheet.filters[0].field.field == f["Category"]  # type: ignore[union-attr]
    changed = next(x for x in updated.datasources[0].fields if x.id == calc.id)
    assert changed.calc is not None and changed.calc.formula == '[Category] + "!"'
    # 欄位本身保留；篩選的值照舊（East、West 不是 Category 的成員，結果是空的）
    assert field(updated, "Region").id == f["Region"]
    assert run(updated, sources, out) == []
    with pytest.raises(EditError, match="一個是維度、一個是量值"):
        replace_references(ir, ds, f["Region"], f["Sales"])
    with pytest.raises(EditError, match="另一個欄位"):
        replace_references(ir, ds, f["Region"], f["Region"])


def test_change_data_type_of_an_extract_column() -> None:
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    ds = ir.datasources[0]
    database = FEATURES / "data" / f"{ds.id}.duckdb"
    # 找一個工作表沒有用到的整數欄位（用到的欄位不能變更型別）
    found: tuple[Any, IR] | None = None
    for candidate in ds.fields:
        if candidate.column is None or candidate.datatype != "integer":
            continue
        try:
            found = (candidate, update_field(ir, ds.id, candidate.id, datatype="string"))
            break
        except EditError:
            continue
    assert found is not None
    target, with_text = found
    converted = next(f for f in with_text.datasources[0].fields if f.id == target.id)
    raw = next(f for f in with_text.datasources[0].fields if f.caption.endswith("（原始型別）"))
    # 萃取中的欄位仍是整數：以隱藏的原始欄位加上轉換的計算欄位表示，欄位的 id 不變
    assert converted.calc is not None and converted.calc.formula == f"STR({raw.name})"
    assert (converted.datatype, converted.role, converted.column) == ("string", "dimension", None)
    assert raw.hidden and raw.column == target.column and raw.datatype == "integer"
    sheet = with_text.worksheets[0].model_dump(mode="json")
    sheet.update(
        rows=[{"field": target.id}], columns=[], filters=[], sorts=[], table_calcs=[], panes=[]
    )
    sheet["marks"] = {"type": "text"}
    for key in ("subtotals", "totals", "visual_totals", "reference_lines", "annotations", "axes"):
        sheet[key] = []
    sheet.update(measure_values=[], measure_aliases=[], controls=[], percentages=None, blends=[])
    placed, _ = replace_worksheet(with_text, sheet)
    values = [
        r[0] for r in execute(database, worksheet_sql(placed, placed.worksheets[0].id).sql).rows
    ]
    assert values and all(isinstance(v, str) for v in values)
    # 再改成小數：公式換成 FLOAT；改回整數：還原成一般的欄位，隱藏的原始欄位移除
    as_real = update_field(with_text, ds.id, target.id, datatype="real")
    assert next(f for f in as_real.datasources[0].fields if f.id == target.id).calc.formula == (  # type: ignore[union-attr]
        f"FLOAT({raw.name})"
    )
    back = update_field(as_real, ds.id, target.id, datatype="integer")
    restored = next(f for f in back.datasources[0].fields if f.id == target.id)
    assert restored.calc is None and restored.column == target.column
    assert not any(f.caption.endswith("（原始型別）") for f in back.datasources[0].fields)
    with pytest.raises(EditError, match="不能轉換成"):
        update_field(ir, ds.id, target.id, datatype="boolean")


def test_create_user_filter(setup: tuple[IR, Path, Path]) -> None:
    """建立使用者篩選：各使用者或群組看得到的成員 → 布林的計算欄位（登入者看得到的為 TRUE）；放到篩選架上
    選 True 之後，不同的登入者查到不同的列。各區的 SUM(Sales)：East 150.75、West 200、South 75。"""
    ir, sources, out = setup
    ds = ir.datasources[0].id
    f = ids(ir)
    ir, made = create_user_filter(
        ir,
        ds,
        f["Region"],
        "",
        [
            ("user", "amy", ["East"]),
            ("group", "業務", ["West", "South"]),
            ("user", "nobody", []),  # 沒有成員：什麼都看不到，不寫進公式
        ],
    )
    assert (made.caption, made.role, made.datatype) == (
        "Region 的使用者篩選",
        "dimension",
        "boolean",
    )
    assert made.calc is not None
    formula = made.calc.formula
    assert formula.startswith('ISMEMBEROF("All Users") AND (')
    assert 'ISUSERNAME("amy")' in formula and 'ISMEMBEROF("業務")' in formula
    assert "nobody" not in formula
    ir = place(
        ir,
        rows=[{"field": f["Region"]}],
        columns=[{"field": f["Sales"], "agg": "sum", "continuous": True}],
        filters=[
            {
                "id": "flt_user",
                "kind": "categorical",
                "field": {"field": made.id},
                "stage": "dimension",
                "mode": "include",
                "values": [True],
                "source_ref": "/editor",
            }
        ],
    )
    assert check_references(ir) == []
    import_sources(sources, out, ir)
    database = out / f"{ir.datasources[0].id}.duckdb"

    def visible(user: Identity) -> dict[Any, Any]:
        query = worksheet_sql(ir, ir.worksheets[0].id, None, user)
        return dict(execute_query(database, query).rows)

    everyone = ("All Users",)
    assert visible(Identity(username="amy", groups=everyone)) == pytest.approx({"East": 150.75})
    assert visible(Identity(username="bob", groups=(*everyone, "業務"))) == pytest.approx(
        {"West": 200.0, "South": 75.0}
    )
    # 同時符合兩個條件：看得到兩邊的成員
    assert len(visible(Identity(username="amy", groups=(*everyone, "業務")))) == 3
    assert visible(Identity(username="nobody", groups=everyone)) == {}
    # 名稱重複時加上序號；名稱與種類要正確、成員的型別要符合、至少一個對象有成員
    ir, again = create_user_filter(ir, ds, f["Region"], "", [("user", "amy", ["West"])])
    assert again.caption == "Region 的使用者篩選 2"
    for entries, message in (
        ([("user", "amy", [])], "至少要有一個"),
        ([("user", " ", ["East"])], "需要名稱"),
        ([("role", "amy", ["East"])], "不認得的對象種類"),
        ([("user", "amy", ["East"]), ("user", "AMY", ["West"])], "重複"),
        ([("user", "amy", [1])], "East|Region|型別|不是"),
    ):
        with pytest.raises(EditError, match=message):
            create_user_filter(ir, ds, f["Region"], "", entries)
    with pytest.raises(EditError, match="不是維度"):
        create_user_filter(ir, ds, f["Sales"], "", [("user", "amy", [1.0])])
