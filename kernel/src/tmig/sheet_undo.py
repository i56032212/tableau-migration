"""復原刪除的工作表、dashboard 或 Story（桌面版編輯器的 Ctrl+Z；Tableau 刪除工作表之後可以復原）。

刪除不只拿掉那一個物件（tmig.edit 的 remove_worksheet、remove_dashboard、remove_story）：刪除工作表時其他
工作表的工具提示不再有它，刪除 dashboard 時只在它上面執行的動作、目標工作表上指向這些動作的篩選與故事點
記下的選取一併移除。remove_sheet 比對刪除前後的 IR，把「少掉的東西」連同原本的位置記成一份紀錄
（Removed）；restore_sheet 依紀錄把它們放回去。

- 剛刪除就復原：整份 IR 與刪除之前完全相同（物件的 id、名稱、定義、在清單中的位置，以及連帶移除的東西）
- 刪除之後又改過報表才復原：放回原本的位置（清單變短時放在最後）；記下的位置所在的物件已經不在
  （例如那張工作表後來也刪了）時略過那一項。放回去之後整份 IR 重新檢查，不通過（例如同名的工作表、
  用到的欄位已經刪除）時 EditError，報表不變
- 還可以復原的期間，之後新增的東西不占用它的 id 與名稱（reserved_by 交給 tmig.edit.reserving），
  放回去時才不會與後來新增的工作表、篩選撞在一起

紀錄是 JSON（交給編輯器保存，復原時送回來）：送回來的紀錄只能動到工作表、dashboard、Story 與動作。
所有函式都不改動傳入的 IR，回傳新的 IR（與 tmig.edit 相同）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig.edit import (
    EditError,
    _first_error,
    _validated,
    remove_dashboard,
    remove_story,
    remove_worksheet,
)
from tmig.ir import IR

SheetKind = Literal["worksheet", "dashboard", "story"]

COLLECTIONS: dict[SheetKind, str] = {
    "worksheet": "worksheets",
    "dashboard": "dashboards",
    "story": "stories",
}
"""各種分頁在 IR 中的清單。"""
KIND_NAMES: dict[SheetKind, str] = {
    "worksheet": "工作表",
    "dashboard": "dashboard",
    "story": "Story",
}
RESTORABLE = ("worksheets", "dashboards", "stories", "actions")
"""刪除分頁會動到的部分；復原的紀錄只能放回這些地方（資料源、參數、報表的設定不在其中）。"""


class ItemRef(BaseModel):
    """路徑中清單裡的一個物件：以 id 指定（之後清單裡的順序變了也找得到）。"""

    model_config = ConfigDict(extra="forbid")
    id: str


class Change(BaseModel):
    """刪除時少掉的一樣東西。path 從 IR 的最上層走到它所在的清單（insert）或欄位（set）：字串是欄位名稱，
    ItemRef 是清單中有 id 的物件，整數是清單中沒有 id 的物件的位置。"""

    model_config = ConfigDict(extra="forbid")
    op: Literal["insert", "set"]
    path: list[str | int | ItemRef] = Field(min_length=1)
    index: int | None = Field(default=None, ge=0)
    """insert：原本在清單中的位置。"""
    value: Any = None
    """insert：少掉的那一項；set：原本的值。"""


class Removed(BaseModel):
    """刪除的工作表、dashboard 或 Story，與復原時要放回去的東西（第一項是它自己）。"""

    model_config = ConfigDict(extra="forbid")
    kind: SheetKind
    id: str
    name: str
    changes: list[Change] = Field(min_length=1)


def remove_sheet(ir: IR, kind: SheetKind, sheet: str) -> tuple[IR, Removed]:
    """刪除工作表、dashboard 或 Story（還在使用時 EditError，與 tmig.edit 相同），並回傳復原用的紀錄。"""
    found = next((item for item in getattr(ir, COLLECTIONS[kind]) if item.id == sheet), None)
    if kind == "worksheet":
        updated = remove_worksheet(ir, sheet)
    elif kind == "dashboard":
        updated = remove_dashboard(ir, sheet)
    else:
        updated = remove_story(ir, sheet)
    assert found is not None  # 找不到時上面已經 EditError
    changes: list[Change] = []
    _diff(ir.model_dump(mode="json"), updated.model_dump(mode="json"), [], changes)
    # 物件自己排在最前面：其他項目的路徑可能經過它所在的清單
    changes.sort(key=lambda c: c.path != [COLLECTIONS[kind]])
    return updated, Removed(kind=kind, id=found.id, name=found.name, changes=changes)


def restore_sheet(ir: IR, removed: Removed | dict[str, Any]) -> tuple[IR, Removed]:
    """把刪除的工作表、dashboard 或 Story 放回去（見模組的說明）；回傳新的 IR 與檢查過的紀錄。"""
    try:
        record = removed if isinstance(removed, Removed) else Removed.model_validate(removed)
    except ValidationError as exc:
        raise EditError(f"復原的紀錄不正確：{_first_error(exc)}") from exc
    label = f"{KIND_NAMES[record.kind]}「{record.name}」"
    collection = COLLECTIONS[record.kind]
    first = record.changes[0]
    own = first.value if isinstance(first.value, dict) else {}
    if (
        first.op != "insert"
        or first.path != [collection]
        or own.get("id") != record.id
        or own.get("name") != record.name
    ):
        raise EditError("復原的紀錄不正確：第一項不是刪除的物件")
    if any(c.path[0] not in RESTORABLE for c in record.changes):
        raise EditError("復原的紀錄不正確：只能放回工作表、dashboard、Story 與動作")
    existing = getattr(ir, collection)
    if any(item.id == record.id for item in existing):
        raise EditError(f"無法復原：{label}已經在報表中")
    if any(item.name == record.name for item in existing):
        raise EditError(
            f"無法復原刪除的{label}：報表中已經有同名的{KIND_NAMES[record.kind]}，"
            "先把它改成別的名稱"
        )
    data = ir.model_dump(mode="json")
    for change in record.changes:
        _apply(data, change)
    try:
        updated = IR.model_validate(data)
    except ValidationError as exc:
        raise EditError(f"無法復原刪除的{label}：{_first_error(exc)}") from exc
    return _validated(updated, f"無法復原刪除的{label}"), record


def reserved_by(removed: Removed) -> set[str]:
    """復原這份紀錄時要放回去的 id（工作表與它的篩選、Table Calculation，dashboard 的動作…）與它的名稱。
    還可以復原的期間，新增的東西不能占用它們（tmig.edit.reserving）：否則之後新增的工作表可能拿到同一個
    id（id 由名稱產生，「工作表 2」刪除後再新增又是「工作表 2」），刪除的那一張就放不回去了。"""
    found = {removed.name}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "id" and isinstance(item, str):
                    found.add(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for change in removed.changes:
        walk(change.value)
    return found


# ---------------------------------------------------------------- 比對與放回


def _ref(item: Any) -> ItemRef | None:
    """清單中有 id 的物件。"""
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return ItemRef(id=item["id"])
    return None


def _same(a: Any, b: Any) -> bool:
    """刪除前後是同一項：有 id 的物件比 id（內容可能少了東西），其餘要完全相同。"""
    first, second = _ref(a), _ref(b)
    if first is not None and second is not None:
        return first.id == second.id
    return bool(a == b)


def _diff(before: Any, after: Any, path: list[str | int | ItemRef], out: list[Change]) -> None:
    """刪除之後（after）比刪除之前（before）少掉的東西。清單中少掉的項目記成 insert（原本的位置與內容）；
    其他不同的地方（目前的刪除不會有）記成 set（原本的值）。"""
    if before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        for key, value in before.items():
            _diff(value, after[key], [*path, key], out)
        return
    if isinstance(before, list) and isinstance(after, list) and len(after) <= len(before):
        kept: list[tuple[int, Any, Any]] = []
        dropped: list[tuple[int, Any]] = []
        if len(after) == len(before):
            # 一樣長：沒有少掉項目，逐項比對裡面（沒有 id 的物件，例如各標記卡，依位置對應）
            if all(_ref(a) == _ref(b) for a, b in zip(before, after, strict=True)):
                kept = [(i, a, b) for i, (a, b) in enumerate(zip(before, after, strict=True))]
        else:
            position = 0
            for index, item in enumerate(before):
                if position < len(after) and _same(item, after[position]):
                    kept.append((index, item, after[position]))
                    position += 1
                else:
                    dropped.append((index, item))
            if position != len(after):
                kept = []
        if kept or not after:
            # 先放回這個清單少掉的項目，其餘項目裡面少掉的才依刪除之前的位置找得到
            out.extend(Change(op="insert", path=path, index=i, value=item) for i, item in dropped)
            for index, item, other in kept:
                _diff(item, other, [*path, _ref(item) or index], out)
            return
    out.append(Change(op="set", path=path, value=before))


def _apply(data: dict[str, Any], change: Change) -> None:
    """把一項放回 IR（JSON）；路徑上的物件已經不在時略過。"""
    target: Any = data
    steps = change.path if change.op == "insert" else change.path[:-1]
    for step in steps:
        if isinstance(step, str):
            if not isinstance(target, dict) or step not in target:
                return
            target = target[step]
        elif isinstance(step, int):
            if not isinstance(target, list) or step >= len(target):
                return
            target = target[step]
        else:
            if not isinstance(target, list):
                return
            target = next((item for item in target if _ref(item) == step), None)
            if target is None:
                return
    if change.op == "insert":
        if isinstance(target, list) and change.index is not None:
            target.insert(min(change.index, len(target)), change.value)
        return
    last = change.path[-1]
    if isinstance(last, str) and isinstance(target, dict) and last in target:
        target[last] = change.value


__all__ = [
    "Change",
    "ItemRef",
    "Removed",
    "SheetKind",
    "remove_sheet",
    "reserved_by",
    "restore_sheet",
]
