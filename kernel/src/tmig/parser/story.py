"""Story（故事）：<dashboard type='storyboard'> 中依序切換的故事點。

每個故事點顯示一個 dashboard 或工作表（captured-sheet），並記下與工作表預設狀態不同的地方：
篩選動作的選取、工作表篩選的值與選取的標記。擷取故事點時記在 <capturedDeltas>；擷取後又改過、
沒有按「更新」時，改過的狀態記在 <currentDeltas>，開啟報表時顯示的是它。寫法取自公開報表
（ASIA Cup Match Overview、Practice Activity Number 4、Assignment 4）。認不得的狀態列為不支援
（故事點會顯示成不同的畫面，不能默默略過）。
"""

from __future__ import annotations

from lxml import etree

from tmig.ir.models import (
    Action,
    Dashboard,
    FieldUse,
    Mark,
    Scalar,
    Size,
    Story,
    StoryAction,
    StoryFilter,
    StoryPoint,
    StorySelection,
    TupleFilter,
    Worksheet,
)
from tmig.parser.context import ParseContext, SheetState
from tmig.parser.dashboard import _runs, _size
from tmig.parser.fields import instance_key, use_from_instance
from tmig.parser.twbx import child, children, descendants, local_name, text_of, xpath
from tmig.parser.values import LiteralError, parse_literal
from tmig.planner.state import use_key

UI_ACTION = "{http://www.tableausoftware.com/xml/user}ui-action-filter"
NAVIGATION = {"caption": "caption", "number": "number", "dots": "dots"}


class StoryParser:
    def __init__(
        self,
        ctx: ParseContext,
        element: etree._Element,
        worksheets: list[Worksheet],
        dashboards: list[Dashboard],
        actions: list[Action],
    ) -> None:
        self.ctx = ctx
        self.el = element
        self.name = element.get("name", "")
        self.worksheets = {w.name: w.id for w in worksheets}
        self.sheets_by_id = {w.id: w for w in worksheets}
        self.dashboards = {d.name: d.id for d in dashboards}
        self.actions = {a.name: a.id for a in actions}

    def parse(self) -> Story | None:
        ctx, el = self.ctx, self.el
        sizing = _size(child(el, "size"))
        flipboard = next(iter(descendants(el, "flipboard")), None)
        points: list[StoryPoint] = []
        for item in descendants(child(flipboard, "story-points"), "story-point"):
            point = self._point(item)
            if point is not None:
                points.append(point)
        if not points:
            ctx.flag(el, f"Story「{self.name}」沒有可以移轉的故事點")
            return None
        nav = flipboard.get("nav-type", "caption") if flipboard is not None else "caption"
        arrows = flipboard is None or flipboard.get("show-nav-arrows") != "false"
        active = f"sp{flipboard.get('active-id')}" if flipboard is not None else None
        return Story(
            id=ctx.ids.make("st", self.name),
            name=self.name,
            title=self._title(),
            size=Size(width=sizing.width, height=sizing.height),
            navigation=NAVIGATION.get(nav, "caption"),  # type: ignore[arg-type]
            arrows=arrows,
            points=points,
            active=active if any(p.id == active for p in points) else None,
            source_ref=xpath(el),
        )

    def _title(self) -> str | None:
        """有標題區塊時顯示標題：自訂的文字，沒有自訂時為 Story 的名稱（與 dashboard 的標題相同）。"""
        if not any(
            z.get("type-v2") == "title" for z in descendants(child(self.el, "zones"), "zone")
        ):
            return None
        title = child(child(self.el, "layout-options"), "title")
        return (_runs(title) if title is not None else None) or self.name

    # ------------------------------------------------------------ 故事點

    def _point(self, element: etree._Element) -> StoryPoint | None:
        target = element.get("captured-sheet", "")
        caption = element.get("caption", "")
        label = caption or target
        dashboard = self.dashboards.get(target)
        worksheet = None if dashboard is not None else self.worksheets.get(target)
        if dashboard is None and worksheet is None:
            self.ctx.flag(
                element, f"Story「{self.name}」的故事點「{label}」顯示的「{target}」無法移轉"
            )
            return None
        point = StoryPoint(
            id=f"sp{element.get('id', '0')}",
            caption=caption,
            dashboard=dashboard,
            worksheet=worksheet,
        )
        current = child(element, "currentDeltas")
        deltas = current if current is not None else child(element, "capturedDeltas")
        for sheet_el in children(deltas):
            if local_name(sheet_el) != "worksheet":
                self._unsupported(sheet_el, label, f"狀態 <{local_name(sheet_el)}>")
                continue
            sheet = self.ctx.sheets.get(sheet_el.get("name", ""))
            if sheet is None:
                self._unsupported(sheet_el, label, f"工作表「{sheet_el.get('name')}」的狀態")
                continue
            for item in children(sheet_el):
                tag = local_name(item)
                if tag == "filter":
                    self._filter(item, sheet, point, label)
                elif tag == "selection-collection":
                    self._selection(item, sheet, point, label)
                else:
                    self._unsupported(item, label, f"狀態 <{tag}>")
        return point

    def _unsupported(self, element: etree._Element, label: str, what: str) -> None:
        self.ctx.flag(element, f"Story「{self.name}」的故事點「{label}」記下的{what}尚不支援")

    def _members(self, group: etree._Element | None) -> list[etree._Element] | None:
        """groupfilter 的成員：單一 member，或由 member 組成的 union；其他寫法回傳 None。"""
        if group is None:
            return None
        if group.get("function") == "member":
            return [group]
        if group.get("function") == "union":
            items = children(group, "groupfilter")
            if items and all(g.get("function") == "member" for g in items):
                return items
        return None

    def _level(self, sheet: SheetState, level: str) -> FieldUse | None:
        """groupfilter 的 level（[Region]，或欄位實例 [none:Region:nk]）→ 欄位使用。"""
        key = instance_key(level)
        if key in sheet.uses:
            return sheet.uses[key]
        if key in sheet.instances:
            resolved = use_from_instance(sheet.ds, sheet.instances[key])
            return None if isinstance(resolved, str) else resolved
        data_field = sheet.ds.by_name.get(key)
        return FieldUse(field=data_field.id) if data_field is not None else None

    def _value(self, sheet: SheetState, use: FieldUse, text: str | None) -> Scalar:
        if use.date is not None and use.date.kind == "part":
            return parse_literal(text, "integer")
        return parse_literal(text, sheet.ds.field_by_id(use.field).datatype)

    def _action(self, name: str, sheet: SheetState, uses: list[FieldUse]) -> str | None:
        """篩選動作的 id：依名稱；動作已刪除時（工作表上留下舊動作的篩選），取目前寫入工作表上
        同樣欄位的動作（見 WorkbookParser._link_stale_filters）。"""
        if name in self.actions:
            return self.actions[name]
        keys = sorted(use_key(u) for u in uses)
        worksheet = self.sheets_by_id.get(sheet.id)
        found = {
            f.action
            for f in (worksheet.filters if worksheet is not None else [])
            if isinstance(f, TupleFilter)
            and f.action is not None
            and sorted(use_key(u) for u in f.fields) == keys
        }
        return found.pop() if len(found) == 1 else None

    def _filter(
        self, element: etree._Element, sheet: SheetState, point: StoryPoint, label: str
    ) -> None:
        members = self._members(child(element, "groupfilter"))
        if members is None:
            self._unsupported(element, label, "篩選條件")
            return
        action_name = next((m.get(UI_ACTION) for m in members if m.get(UI_ACTION)), None)
        try:
            if action_name is not None:
                levels = [self._level(sheet, m.get("level", "")) for m in members]
                uses = [u for u in levels if u is not None]
                action = (
                    self._action(action_name, sheet, uses) if len(uses) == len(levels) else None
                )
                if action is None:
                    self._unsupported(element, label, "篩選動作")
                    return
                marks: list[Mark] = []
                for member, use in zip(members, uses, strict=True):
                    mark = {use_key(use): self._value(sheet, use, member.get("member"))}
                    if mark not in marks:
                        marks.append(mark)
                self._add_action(element, point, StoryAction(action=action, marks=marks), label)
                return
            use = self._level(sheet, element.get("column", ""))
            if use is None or use.agg is not None or use.table_calc is not None:
                self._unsupported(element, label, "篩選")
                return
            values = [self._value(sheet, use, m.get("member")) for m in members]
        except LiteralError:
            self._unsupported(element, label, "篩選的值")
            return
        point.filters.append(
            StoryFilter(worksheet=sheet.id, field=use, mode="include", values=values)
        )

    def _add_action(
        self, element: etree._Element, point: StoryPoint, state: StoryAction, label: str
    ) -> None:
        """同一個篩選動作在每張目標工作表各記一次（值相同）：只留一筆。"""
        existing = next((a for a in point.actions if a.action == state.action), None)
        if existing is None:
            point.actions.append(state)
        elif existing.marks != state.marks:
            self._unsupported(element, label, "篩選動作（各工作表的選取不同）")

    def _selection(
        self, element: etree._Element, sheet: SheetState, point: StoryPoint, label: str
    ) -> None:
        if any(local_name(e) == "tuple-selection" for e in element.iter()):
            self._unsupported(element, label, "選取（值組合）")
            return
        marks: list[Mark] = []
        for node in descendants(element, "node-reference"):
            refs = [text_of(f) for f in children(child(node, "fields"), "field")]
            buckets = [text_of(b) for b in children(child(node, "multibucket"), "bucket")]
            levels = [self._level(sheet, ref) for ref in refs]
            uses = [u for u in levels if u is not None]
            if not refs or len(refs) != len(buckets) or len(uses) != len(levels):
                self._unsupported(element, label, "選取")
                return
            try:
                mark: Mark = {
                    use_key(use): self._value(sheet, use, bucket)
                    for use, bucket in zip(uses, buckets, strict=True)
                }
            except LiteralError:
                self._unsupported(element, label, "選取的值")
                return
            if mark not in marks:
                marks.append(mark)
        if marks:
            point.selections.append(StorySelection(worksheet=sheet.id, marks=marks))


__all__ = ["StoryParser"]
