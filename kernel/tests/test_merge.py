"""匯入另一份報表的工作表（tmig.merge）：id 與名稱不重複、參照一併改掉、參數依名稱共用，匯入後的工作表
查詢的數字與來源報表相同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.data.executor import execute_query
from tmig.edit import EditError
from tmig.ir import IR, load_ir
from tmig.ir.integrity import check_references
from tmig.merge import import_from
from tmig.sqlgen.query import worksheet_sql
from tmig.verify.run import ensure_data

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
FEATURES = ROOT / "fixtures" / "superstore_features"


@pytest.fixture(scope="module")
def demo() -> IR:
    ir = load_ir(DEMO / "golden.ir.json")
    ensure_data(DEMO, ir)
    return ir


@pytest.fixture(scope="module")
def features() -> IR:
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    return ir


def rows(ir: IR, worksheet: str, database: Path) -> list[tuple[object, ...]]:
    """工作表的查詢結果（浮點數只比到 9 位有效數字：加總的順序不同時最後幾位可能不同）。"""
    found = execute_query(database, worksheet_sql(ir, worksheet)).rows
    rounded = [
        tuple(float(f"{v:.9g}") if isinstance(v, float) else v for v in row) for row in found
    ]
    return sorted(rounded, key=repr)


def test_import_selected_worksheets(demo: IR, features: IR) -> None:
    """只匯入選取的工作表：連同資料源與參數；兩份報表的 id（ds_orders、f_sales 等）相同，匯入的加上編號。"""
    chosen = ["ws_sales_by_segment", "ws_category_mix"]
    result = import_from(demo, features, chosen)
    ir = result.ir
    assert check_references(ir) == []
    assert result.datasources == {"ds_orders": "ds_orders_2"}
    assert len(result.worksheets) == 2 and not result.dashboards and not result.stories
    assert len(ir.worksheets) == len(demo.worksheets) + 2
    assert len(ir.dashboards) == len(demo.dashboards)
    imported = {w.id: w for w in ir.worksheets if w.id in result.worksheets}
    assert all(w.datasource == "ds_orders_2" for w in imported.values())
    # 原本的報表不變：資料源與欄位的 id 沒有被改
    assert [d.id for d in ir.datasources][: len(demo.datasources)] == [
        d.id for d in demo.datasources
    ]
    ids = {f.id for d in ir.datasources for f in d.fields}
    assert {"f_sales", "f_sales_2"} <= ids
    # 匯入的工作表查詢的數字與來源報表相同（資料是來源報表的資料源）
    source_db = FEATURES / "data" / "ds_orders.duckdb"
    for original, sheet in zip(chosen, result.worksheets, strict=True):
        assert rows(ir, sheet, source_db) == rows(features, original, source_db)
    # 沒有匯入的動作與工具提示中的工作表：參照拿掉
    segment = next(w for w in imported.values() if w.name.startswith("Sales by Segment"))
    assert all(getattr(f, "action", None) is None for f in segment.filters)
    assert [t.worksheet for t in segment.marks.tooltip_sheets] == [
        next(w.id for w in imported.values() if w.name.startswith("Category Mix"))
    ]
    with pytest.raises(EditError, match="沒有工作表"):
        import_from(demo, features, ["ws_nope"])
    with pytest.raises(EditError, match="沒有選取"):
        import_from(demo, features, [])


def test_import_everything(demo: IR, features: IR) -> None:
    """整份匯入：工作表、dashboard、Story、動作、資料源與參數；名稱相同的加上編號。"""
    result = import_from(demo, features)
    ir = result.ir
    assert check_references(ir) == []
    assert len(ir.worksheets) == len(demo.worksheets) + len(features.worksheets)
    assert len(ir.dashboards) == len(demo.dashboards) + len(features.dashboards)
    assert len(ir.stories) == len(demo.stories) + len(features.stories)
    assert len(ir.actions) == len(demo.actions) + len(features.actions)
    names = [x.name for x in [*ir.worksheets, *ir.dashboards, *ir.stories]]
    assert len(names) == len(set(names))
    assert len({d.caption for d in ir.datasources}) == len(ir.datasources)
    # dashboard 的區塊指向匯入後的工作表
    sheets = {w.id for w in ir.worksheets}
    for board in ir.dashboards:
        stack = list(board.zones)
        while stack:
            zone = stack.pop()
            assert zone.worksheet is None or zone.worksheet in sheets
            stack.extend(zone.children)
    # 再匯入一次：id 繼續編號，不會重複
    again = import_from(ir, features, ["ws_category_mix"])
    assert again.datasources == {"ds_orders": "ds_orders_3"}
    assert check_references(again.ir) == []


def test_parameters_are_shared_or_renamed(demo: IR, features: IR) -> None:
    """參數依名稱比對：兩份報表各自的 [Parameter 1] 不是同一個參數，匯入的改名、公式中的參照一併改掉；
    名稱與定義都相同時共用目標報表的參數。"""
    (theirs,) = features.parameters
    (mine,) = demo.parameters
    assert theirs.name == mine.name == "[Parameter 1]" and theirs.caption != mine.caption
    used = [
        w.id
        for w in features.worksheets
        if any(
            f.calc is not None and "[Parameters].[Parameter 1]" in f.calc.formula
            for d in features.datasources
            for f in d.fields
            if f.id in w.model_dump_json()
        )
    ][:1]
    assert used, "功能報表中有用到參數的計算欄位"
    first = import_from(demo, features, used)
    assert check_references(first.ir) == []
    added = [p for p in first.ir.parameters if p.id not in {mine.id}]
    assert [(p.name, p.caption) for p in added] == [("[Parameter 2]", theirs.caption)]
    imported = next(d for d in first.ir.datasources if d.id == first.datasources["ds_orders"])
    formulas = [f.calc.formula for f in imported.fields if f.calc is not None]
    assert any("[Parameters].[Parameter 2]" in text for text in formulas)
    assert not any("[Parameters].[Parameter 1]" in text for text in formulas)
    # 原本的報表中的公式不變
    original = next(d for d in first.ir.datasources if d.id == "ds_orders")
    assert [f.calc.formula for f in original.fields if f.calc is not None] == [
        f.calc.formula for f in demo.datasources[0].fields if f.calc is not None
    ]
    # 匯入後的數字與來源報表相同（參數的目前值也一起帶過來）
    source_db = FEATURES / "data" / "ds_orders.duckdb"
    assert rows(first.ir, first.worksheets[0], source_db) == rows(features, used[0], source_db)
    # 同一份報表再匯入一次：名稱、說明與定義都相同的參數共用
    second = import_from(features, features, used)
    assert second.shared_parameters == [theirs.id]
    assert len(second.ir.parameters) == 1
