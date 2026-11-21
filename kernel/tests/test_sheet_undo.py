"""復原刪除的工作表、dashboard、Story（tmig.sheet_undo）：剛刪除就復原時整份 IR 與刪除之前完全相同。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tmig.edit import (
    EditError,
    add_dashboard,
    add_story,
    add_worksheet,
    duplicate_worksheet,
    new_id,
    remove_worksheet,
    replace_story,
    replace_worksheet,
    reserving,
    upsert_action,
)
from tmig.ir import IR, TooltipSheet, load_ir
from tmig.sheet_undo import (
    COLLECTIONS,
    Removed,
    SheetKind,
    remove_sheet,
    reserved_by,
    restore_sheet,
)

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "golden.ir.json"
FEATURES = ROOT / "fixtures" / "superstore_features" / "golden.ir.json"


@pytest.fixture
def ir() -> IR:
    return load_ir(DEMO)


def dump(ir: IR) -> dict[str, Any]:
    return ir.model_dump(mode="json")


def over_the_wire(removed: Removed) -> dict[str, Any]:
    """紀錄交給編輯器、復原時再送回來：經過 JSON。"""
    return json.loads(json.dumps(removed.model_dump(mode="json"), ensure_ascii=False))


def with_tooltip(ir: IR, worksheet: str, targets: list[str], pane: bool = False) -> IR:
    """worksheet 的工具提示（pane：最後一張標記卡的）放進 targets 這幾張工作表。"""
    sheet = next(w for w in ir.worksheets if w.id == worksheet)
    sheets = [TooltipSheet(worksheet=t, max_width=200 + i) for i, t in enumerate(targets)]
    if pane:
        last = sheet.panes[-1]
        marks = last.marks.model_copy(update={"tooltip_sheets": sheets})
        panes = [*sheet.panes[:-1], last.model_copy(update={"marks": marks})]
        return replace_worksheet(ir, sheet.model_copy(update={"panes": panes}))[0]
    marks = sheet.marks.model_copy(update={"tooltip_sheets": sheets})
    return replace_worksheet(ir, sheet.model_copy(update={"marks": marks}))[0]


@pytest.mark.parametrize("path", [DEMO, FEATURES], ids=["demo", "features"])
def test_restoring_right_after_a_delete_gives_back_the_same_report(path: Path) -> None:
    """兩份示範報表裡每一個刪得掉的工作表、dashboard、Story：刪除後復原，整份 IR 與刪除之前完全相同
    （id、名稱、定義、位置，連帶移除的動作與篩選）；再刪一次（重做）與第一次刪除的結果相同。"""
    ir = load_ir(path)
    before = dump(ir)
    restored: dict[str, int] = {}
    side_effects = 0
    kinds: tuple[SheetKind, ...] = ("worksheet", "dashboard", "story")
    for kind in kinds:
        for item in getattr(ir, COLLECTIONS[kind]):
            try:
                deleted, removed = remove_sheet(ir, kind, item.id)
            except EditError:
                continue  # 還在 dashboard、Story 或動作中使用，不能刪
            assert (removed.kind, removed.id, removed.name) == (kind, item.id, item.name)
            # 刪除只會讓清單少掉項目：每一項都是放回原本的位置（不是整個換掉）
            assert all(c.op == "insert" for c in removed.changes)
            assert all(x.id != item.id for x in getattr(deleted, COLLECTIONS[kind]))
            back, record = restore_sheet(deleted, over_the_wire(removed))
            assert dump(back) == before, f"{kind} {item.id}"
            assert record == removed
            again, _ = remove_sheet(back, kind, item.id)
            assert dump(again) == dump(deleted)
            restored[kind] = restored.get(kind, 0) + 1
            side_effects += len(removed.changes) > 1
    if path == FEATURES:
        # 功能報表：三種都測到，也測到刪除時連帶移除動作的 dashboard
        assert set(restored) == set(kinds), restored
        assert side_effects > 0


def test_restore_puts_the_sheet_back_in_its_place_with_the_tooltips_that_showed_it() -> None:
    """刪除工作表時其他工作表的工具提示不再有它：復原時放回工具提示中原本的位置（工作表的標記卡與多張
    標記卡中的一張），工作表回到分頁列原本的位置。"""
    ir = load_ir(FEATURES)
    ir, first = add_worksheet(ir, "Tooltip A", None)
    ir, second = add_worksheet(ir, "Tooltip B", None)
    ir, _ = add_worksheet(ir, "Last", None)
    plain = next(w for w in ir.worksheets if not w.panes and w.datasource == first.datasource)
    ir = with_tooltip(ir, plain.id, [second.id, first.id])
    paned = next(w for w in ir.worksheets if len(w.panes) > 1)
    ir = with_tooltip(ir, paned.id, [first.id, second.id], pane=True)
    before = dump(ir)
    order = [w.id for w in ir.worksheets]

    deleted, removed = remove_sheet(ir, "worksheet", first.id)
    host = next(w for w in deleted.worksheets if w.id == plain.id)
    assert [t.worksheet for t in host.marks.tooltip_sheets] == [second.id]
    card = next(w for w in deleted.worksheets if w.id == paned.id).panes[-1]
    assert [t.worksheet for t in card.marks.tooltip_sheets] == [second.id]
    # 紀錄：工作表自己，加上兩個工具提示裡的它
    assert [c.op for c in removed.changes] == ["insert"] * 3
    assert removed.changes[0].path == ["worksheets"]
    assert removed.changes[0].index == order.index(first.id)

    back, _ = restore_sheet(deleted, over_the_wire(removed))
    assert [w.id for w in back.worksheets] == order
    assert dump(back) == before


def test_restore_puts_back_the_actions_of_a_deleted_dashboard(ir: IR) -> None:
    """刪除 dashboard 時只在它上面執行的動作、目標工作表上指向動作的篩選、故事點記下的選取一併移除：
    復原時都放回原本的位置。"""
    ir, board = add_dashboard(ir, "動作測試")
    ir, _ = add_dashboard(ir, "後面的 dashboard")
    body = {
        "kind": "filter",
        "activation": "select",
        "dashboard": board.id,
        "sources": ["ws_sales_by_region"],
        "clearing": "show_all",
        "fields": ["f_region"],
    }
    ir, one = upsert_action(
        ir, {**body, "caption": "篩選一", "targets": ["ws_region_share_by_year"]}
    )
    ir, _ = upsert_action(ir, {**body, "caption": "別的 dashboard", "dashboard": "db_ee909b0c"})
    ir, two = upsert_action(
        ir, {**body, "caption": "篩選二", "targets": ["ws_sub_category_profit"]}
    )
    ir, story = add_story(ir, "故事")
    data = story.model_dump(mode="json")
    data["points"][0]["actions"] = [
        {"action": two.id, "marks": [{"f_region": "West"}]},
        {"action": one.id, "marks": [{"f_region": "East"}]},
    ]
    ir, _ = replace_story(ir, data)
    before = dump(ir)

    deleted, removed = remove_sheet(ir, "dashboard", board.id)
    assert [a.caption for a in deleted.actions] == ["別的 dashboard"]
    assert deleted.stories[0].points[0].actions == []
    touched = {str(c.path[0]) for c in removed.changes}
    assert touched == {"dashboards", "actions", "worksheets", "stories"}

    back, _ = restore_sheet(deleted, over_the_wire(removed))
    assert [d.id for d in back.dashboards] == [d.id for d in ir.dashboards]
    assert [a.caption for a in back.actions] == ["篩選一", "別的 dashboard", "篩選二"]
    assert dump(back) == before


def test_restore_after_the_report_changed(ir: IR) -> None:
    """刪除之後又改過報表才復原：物件放回原本的位置（清單變短時放在最後），連帶的東西所在的工作表已經
    不在時略過；放回去會讓報表不正確（同名、id 已經被用掉）時 EditError，不動報表。"""
    ir, host = add_worksheet(ir, "Host", None)
    ir, target = add_worksheet(ir, "Target", None)
    ir, _ = add_worksheet(ir, "Later", None)
    ir = with_tooltip(ir, host.id, [target.id])
    ir = with_tooltip(ir, "ws_sales_by_region", [target.id])
    deleted, removed = remove_sheet(ir, "worksheet", target.id)

    # 後來又刪了兩張工作表（其中一張的工具提示原本有它）：放在最後，另一張的工具提示照樣放回
    shorter = remove_worksheet(deleted, host.id)
    shorter = remove_worksheet(shorter, next(w.id for w in shorter.worksheets if w.name == "Later"))
    back, _ = restore_sheet(shorter, removed)
    assert back.worksheets[-1].id == target.id
    kept = next(w for w in back.worksheets if w.id == "ws_sales_by_region")
    assert [t.worksheet for t in kept.marks.tooltip_sheets] == [target.id]

    # 後來新增了同名的工作表（id 也相同）：不能放回去
    taken, again = add_worksheet(deleted, "Target", None)
    assert again.id == target.id
    with pytest.raises(EditError, match="已經在報表中"):
        restore_sheet(taken, removed)
    renamed, _ = replace_worksheet(taken, again.model_copy(update={"name": "Other"}))
    with pytest.raises(EditError, match="已經在報表中"):
        restore_sheet(renamed, removed)
    other, elsewhere = add_worksheet(deleted, "Elsewhere", None)
    same_name, _ = replace_worksheet(other, elsewhere.model_copy(update={"name": "Target"}))
    with pytest.raises(EditError, match="同名"):
        restore_sheet(same_name, removed)
    # 已經復原過了
    done, _ = restore_sheet(deleted, removed)
    with pytest.raises(EditError, match="已經在報表中"):
        restore_sheet(done, removed)


def test_restore_rejects_records_that_reach_outside_the_sheets(ir: IR) -> None:
    """送回來的紀錄只能放回工作表、dashboard、Story 與動作：動到資料源等其他部分、或第一項不是刪除的物件的
    紀錄不接受。"""
    ir, sheet = add_worksheet(ir, "Scratch", None)
    deleted, removed = remove_sheet(ir, "worksheet", sheet.id)
    record = over_the_wire(removed)

    outside = json.loads(json.dumps(record))
    outside["changes"].append(
        {"op": "set", "path": ["datasources", {"id": "ds_orders"}, "caption"], "value": "x"}
    )
    with pytest.raises(EditError, match="只能放回"):
        restore_sheet(deleted, outside)
    swapped = json.loads(json.dumps(record))
    swapped["changes"][0]["path"] = ["dashboards"]
    with pytest.raises(EditError, match="第一項不是刪除的物件"):
        restore_sheet(deleted, swapped)
    renamed = json.loads(json.dumps(record))
    renamed["name"] = "Another"
    with pytest.raises(EditError, match="第一項不是刪除的物件"):
        restore_sheet(deleted, renamed)
    with pytest.raises(EditError, match="紀錄不正確"):
        restore_sheet(deleted, {"kind": "worksheet", "id": sheet.id})
    broken = json.loads(json.dumps(record))
    broken["changes"][0]["value"]["rows"] = [{"field": "f_nope"}]
    with pytest.raises(EditError, match="無法復原刪除的工作表「Scratch」"):
        restore_sheet(deleted, broken)
    # 原本的紀錄照樣可以復原
    back, _ = restore_sheet(deleted, record)
    assert dump(back) == dump(ir)


def test_new_things_do_not_take_the_ids_and_names_of_a_restorable_sheet(ir: IR) -> None:
    """刪除之後還可以復原的工作表：之後新增的東西不占用它的 id 與名稱（reserving）。id 由名稱產生——
    「工作表 1」刪除後再新增又是「工作表 1」、id 也相同，別的工作表上同一個欄位的新篩選也會拿到它的篩選的
    id——沒有保留的話，刪除的那一張就放不回去。"""
    ir, sheet = add_worksheet(ir, None, None)
    assert (sheet.name, sheet.id) == ("工作表 1", "ws_1")
    data = sheet.model_dump(mode="json")
    filter_id = new_id(ir, "flt", "Region")
    data["filters"] = [
        {
            "kind": "categorical",
            "id": filter_id,
            "field": {"field": "f_region"},
            "stage": "dimension",
            "mode": "include",
            "values": ["East"],
            "source_ref": "/editor",
        }
    ]
    ir, sheet = replace_worksheet(ir, data)
    ir, copy = duplicate_worksheet(ir, sheet.id)
    assert copy.name == "工作表 1 (2)"
    deleted, removed = remove_sheet(ir, "worksheet", sheet.id)
    deleted, removed_copy = remove_sheet(deleted, "worksheet", copy.id)
    reserved = reserved_by(removed) | reserved_by(removed_copy)
    assert {"工作表 1", "ws_1", filter_id, "工作表 1 (2)", copy.id} <= reserved

    # 沒有保留：新的工作表又是「工作表 1」（ws_1），原本的那一張放不回去
    clash, again = add_worksheet(deleted, None, None)
    assert (again.name, again.id) == ("工作表 1", "ws_1")
    with pytest.raises(EditError, match="已經在報表中"):
        restore_sheet(clash, removed)

    # 保留：新的工作表、複製的工作表與新的篩選都避開；兩張都放得回去
    with reserving(reserved):
        later, fresh = add_worksheet(deleted, None, None)
        assert (fresh.name, fresh.id) == ("工作表 2", "ws_2")
        assert new_id(later, "flt", "Region") not in reserved
        assert new_id(later, "ws", "1") == "ws_1_3"  # ws_1 與複製的 ws_1_2 都保留著
        # 使用者自己輸入的名稱不受限制（之後復原時會說明同名）
        named, _ = add_worksheet(later, "工作表 1", None)
    assert new_id(later, "flt", "Region") == filter_id  # 離開之後不再保留
    # 依刪除的相反順序復原（編輯器的 Ctrl+Z）：各自回到原本的位置
    back, _ = restore_sheet(later, removed_copy)
    back, _ = restore_sheet(back, removed)
    assert [w.name for w in back.worksheets][-3:] == ["工作表 1", "工作表 1 (2)", "工作表 2"]
    with pytest.raises(EditError, match="同名"):
        restore_sheet(named, removed)
