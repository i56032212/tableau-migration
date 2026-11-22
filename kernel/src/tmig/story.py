"""故事點記下的工作表狀態（IR 的 StorySheet）套用到工作表：檢視器顯示故事點時，查詢服務以套用後的工作表
查詢與繪圖（QueryState.story）。

與 Tableau 相同，故事點記下的是與工作表的差異：工作表之後的修改（例如加上欄位、改了格式）也反映在故事點上；
故事點改過的排序、移除的欄位、座標軸與地圖的範圍蓋過工作表上同一個欄位的設定。
"""

from __future__ import annotations

from tmig.ir import IR, FieldUse, StorySheet, TupleFilter, Worksheet
from tmig.ir.models import MapOptions
from tmig.planner.state import StoryRef, use_key


class StoryStateError(ValueError):
    """故事點不存在，或沒有顯示這張工作表。"""


def story_sheet(ir: IR, worksheet: str, ref: StoryRef) -> StorySheet | None:
    """故事點中這張工作表記下的狀態；沒有記下時為 None。"""
    story = next((s for s in ir.stories if s.id == ref.story), None)
    point = next((p for p in story.points if p.id == ref.point), None) if story else None
    if story is None or point is None:
        raise StoryStateError(f"找不到故事點「{ref.point}」")
    return next((s for s in point.sheets if s.worksheet == worksheet), None)


def apply_story_sheet(sheet: Worksheet, state: StorySheet) -> Worksheet:
    """工作表套用故事點記下的狀態。"""
    rows = _without(sheet.rows, state.removed_rows)
    columns = _without(sheet.columns, state.removed_columns)
    replaced = {s.field for s in state.sorts}
    sorts = [
        s
        for s in sheet.sorts
        if s.field not in replaced and not (s.nested and s.field in state.cleared_sorts)
    ] + list(state.sorts)
    overrides = {(use_key(a.use), a.shelf): a for a in state.axes}
    axes = [overrides.pop((use_key(a.use), a.shelf), a) for a in sheet.axes]
    axes += list(overrides.values())
    update: dict[str, object] = {"rows": rows, "columns": columns, "sorts": sorts, "axes": axes}
    if state.annotations is not None:
        update["annotations"] = list(state.annotations)
    if state.viewport is not None:
        base = sheet.map if sheet.map is not None else MapOptions()
        update["map"] = base.model_copy(update={"viewport": state.viewport})
    if state.null_indicator is not None:
        update["null_indicator"] = state.null_indicator
    if state.cleared_actions:
        # 顯示所有值的篩選動作：沒有選取時不套用存檔時的值（選取標記時照常以選取的值篩選）
        update["filters"] = [
            f.model_copy(update={"mode": "all", "tuples": []})
            if isinstance(f, TupleFilter) and f.action in state.cleared_actions
            else f
            for f in sheet.filters
        ]
    return sheet.model_copy(update=update)


def apply_story(ir: IR, worksheet: str, ref: StoryRef | None) -> IR:
    """查詢這張工作表時套用故事點記下的狀態（ref 為 None 或故事點沒有記下這張工作表時原樣回傳）。"""
    if ref is None:
        return ir
    state = story_sheet(ir, worksheet, ref)
    if state is None:
        return ir
    sheets = [apply_story_sheet(w, state) if w.id == worksheet else w for w in ir.worksheets]
    return ir.model_copy(update={"worksheets": sheets})


def _without(uses: list[FieldUse], removed: list[FieldUse]) -> list[FieldUse]:
    """移除各個欄位（同一個欄位在架上出現多次時移除第一個）。"""
    result = list(uses)
    for use in removed:
        key = use_key(use)
        at = next((i for i, u in enumerate(result) if use_key(u) == key), None)
        if at is not None:
            del result[at]
    return result


__all__ = ["StoryStateError", "apply_story", "apply_story_sheet", "story_sheet"]
