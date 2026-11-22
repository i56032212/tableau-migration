"""Story（故事）：<dashboard type='storyboard'> 中依序切換的故事點。

每個故事點顯示一個 dashboard 或工作表（captured-sheet），並記下與工作表預設狀態不同的地方：
篩選動作的選取、工作表篩選的值（成員、所有的值、範圍）、選取的標記、參數值與改過的文字物件；排序、
欄列架、註解、格式與頁面架目前的頁見 tmig.parser.story_sheet。擷取故事點時記在 <capturedDeltas>；
擷取後又改過、沒有按「更新」時，改過的狀態記在 <currentDeltas>，開啟報表時顯示的是它。寫法取自公開報表
（ASIA Cup Match Overview、Practice Activity Number 4、Assignment 4 與 GitHub 上公開的 .twb）。認不得的
狀態列為不支援（故事點會顯示成不同的畫面，不能默默略過）。
"""

from __future__ import annotations

from typing import Literal

from lxml import etree

from tmig.ir.integrity import iter_zones
from tmig.ir.models import (
    SPECIAL_FIELDS,
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
    TextRun,
    TupleFilter,
    Worksheet,
)
from tmig.parser.context import ParseContext, SheetState
from tmig.parser.dashboard import _runs, _size, formatted_content
from tmig.parser.fields import instance_key, use_from_instance
from tmig.parser.story_sheet import INCLUDED_NULL, SORT_TAGS, SheetDelta
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
        self.boards = {d.id: d for d in dashboards}
        self.actions = {a.name: a.id for a in actions}
        self.elements = {
            w.get("name", ""): w
            for w in children(child(ctx.package.root, "worksheets"), "worksheet")
        }
        """工作表名稱 → <worksheet>（故事點的排序、註解與格式套在工作表原本的寫法上）。"""

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
            id=ctx.story_id(self.name),
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
            tag = local_name(sheet_el)
            if tag == "datasource":
                self._parameters(sheet_el, point, label)
                continue
            if tag == "dashboard":
                self._texts(sheet_el, point, label, target)
                continue
            if tag != "worksheet":
                self._unsupported(sheet_el, label, f"狀態 <{tag}>")
                continue
            self._worksheet(sheet_el, point, label)
        return point

    def _worksheet(self, sheet_el: etree._Element, point: StoryPoint, label: str) -> None:
        """一張工作表的差異：篩選、選取、醒目提示，以及排序、欄列架、註解、格式與頁（SheetDelta）。"""
        name = sheet_el.get("name", "")
        sheet = self.ctx.sheets.get(name)
        worksheet = self.sheets_by_id.get(sheet.id) if sheet is not None else None
        element = self.elements.get(name)
        if sheet is None or worksheet is None or element is None:
            self._unsupported(sheet_el, label, f"工作表「{name}」的狀態")
            return
        delta = SheetDelta(
            self.ctx,
            sheet,
            worksheet,
            element,
            lambda item, what: self._unsupported(item, label, what),
        )
        # 其他差異用到的欄位實例先登記
        for item in children(sheet_el, "datasource-dependencies"):
            delta.dependencies(item)
        state = delta.state()
        for item in children(sheet_el):
            tag = local_name(item)
            if tag == "datasource-dependencies":
                continue
            if tag == "filter":
                self._filter(item, state, point, label, delta)
            elif tag == "selection-collection":
                self._selection(item, state, point, label)
            elif tag == "highlight":
                self.ctx.ignore(item, "故事點記下的醒目提示（只影響醒目提示，不影響數字）")
            elif tag in SORT_TAGS:
                delta.sort(item)
            elif tag == "shelf-sort-deltas":
                delta.clear_sorts(item)
            elif tag in ("rows", "cols"):
                delta.shelf(item)
            elif tag == "annotations":
                delta.annotation_deltas(item)
            elif tag == "style":
                delta.style(item, child(self.ctx.package.root, "style"))
            elif tag == "current-page":
                delta.current_page(item)
            else:
                self._unsupported(item, label, f"狀態 <{tag}>")
        found = delta.result()
        if found is not None:
            point.sheets.append(found)
        paged, page = delta.page
        if paged:
            point.pages[worksheet.id] = page

    def _parameters(self, element: etree._Element, point: StoryPoint, label: str) -> None:
        """故事點記下的參數值：<datasource name='Parameters'><column name='[Parameter 1]' value='…'/>。"""
        if element.get("name") != "Parameters":
            self._unsupported(element, label, f"資料源「{element.get('name')}」的狀態")
            return
        for column in children(element, "column"):
            param = self.ctx.parameter(column.get("name", ""))
            if param is None:
                self._unsupported(column, label, f"參數 {column.get('name')} 的值")
                continue
            try:
                value = parse_literal(column.get("value", ""), param.datatype)
            except LiteralError:
                self._unsupported(
                    column, label, f"參數「{param.caption}」的值 {column.get('value')}"
                )
                continue
            point.parameters[param.id] = value

    def _texts(self, element: etree._Element, point: StoryPoint, label: str, target: str) -> None:
        """故事點改過的 dashboard 文字物件：<dashboard><zone id='5'><formatted-text>…。"""
        board = self.boards.get(point.dashboard or "")
        if board is None or element.get("name", target) != board.name:
            self._unsupported(element, label, f"dashboard「{element.get('name')}」的狀態")
            return
        ids = {z.id for z in iter_zones(board.zones)}
        for zone in children(element, "zone"):
            zone_id = f"z{zone.get('id', '')}"
            if zone_id not in ids:
                self.ctx.ignore(zone, "故事點改過的文字物件已不在 dashboard 上")
                continue
            text, content = formatted_content(self.ctx, zone, board.name)
            point.texts[zone_id] = content or ([TextRun(text=text)] if text else [])

    def _unsupported(self, element: etree._Element, label: str, what: str) -> None:
        self.ctx.flag(element, f"Story「{self.name}」的故事點「{label}」記下的{what}尚不支援")

    def _tuples(self, group: etree._Element | None) -> list[list[etree._Element]] | None:
        """groupfilter 的成員組合：member、由它們組成的 union，與 crossjoin（各部分的每一種組合，例如兩個
        欄位的篩選動作）；每個組合是各層級的 <groupfilter function='member'>。其他寫法回傳 None。"""
        if group is None:
            return None
        function = group.get("function")
        if function == "member":
            return [[group]]
        parts = children(group, "groupfilter")
        if not parts or function not in ("union", "crossjoin"):
            return None
        result: list[list[etree._Element]] = [] if function == "union" else [[]]
        for part in parts:
            found = self._tuples(part)
            if found is None:
                return None
            if function == "union":
                result += found
            else:
                result = [r + f for r in result for f in found]
        return result

    @staticmethod
    def _all(group: etree._Element | None) -> bool:
        """所有的值：level-members，或由它們組成的 crossjoin（多個欄位的篩選動作沒有選取）。"""
        if group is None:
            return False
        if group.get("function") == "level-members":
            return True
        parts = children(group, "groupfilter")
        return (
            group.get("function") == "crossjoin"
            and bool(parts)
            and all(p.get("function") == "level-members" for p in parts)
        )

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
        if use.field in SPECIAL_FIELDS:
            raise LiteralError(f"{use.field} 沒有成員的值")
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
        self,
        element: etree._Element,
        sheet: SheetState,
        point: StoryPoint,
        label: str,
        delta: SheetDelta,
    ) -> None:
        """記下的篩選：成員（保留或排除）、所有的值、範圍，與篩選動作的選取。"""
        if element.get("delta-type") is not None:
            self._unsupported(element, label, f"篩選的變更（{element.get('delta-type')}）")
            return
        if element.get("class") == "quantitative":
            self._range(element, sheet, point, label)
            return
        group = child(element, "groupfilter")
        nodes = list(group.iter("groupfilter")) if group is not None else []
        action_name = next((g.get(UI_ACTION) for g in nodes if g.get(UI_ACTION)), None)
        if (
            action_name is None
            and sheet.ds.auto_groups.get(instance_key(element.get("column", ""))) == "sheet_link"
        ):
            # 篩選動作的欄位（Action (…)）沒有寫動作的名稱：依寫入這張工作表同樣欄位的動作
            action_name = ""
        if action_name is not None:
            self._action_filter(element, group, action_name, sheet, point, label, delta)
            return
        use = self._level(sheet, element.get("column", ""))
        if use is None or use.agg is not None or use.table_calc is not None:
            self._unsupported(element, label, "篩選")
            return
        mode: Literal["include", "exclude"] = "include"
        tuples: list[list[etree._Element]] | None = []
        if self._all(group):
            mode = "exclude"  # 所有的值：排除空清單
        elif group is not None and group.get("function") == "except":
            # 排除：except(level-members, 成員)
            parts = children(group, "groupfilter")
            mode = "exclude"
            tuples = (
                self._tuples(parts[1])
                if len(parts) == 2 and parts[0].get("function") == "level-members"
                else None
            )
        else:
            tuples = self._tuples(group)
        if tuples is None or any(len(t) != 1 for t in tuples):
            self._unsupported(element, label, "篩選條件")
            return
        try:
            values = [self._value(sheet, use, t[0].get("member")) for t in tuples]
        except LiteralError:
            self._unsupported(element, label, "篩選的值")
            return
        point.filters.append(StoryFilter(worksheet=sheet.id, field=use, mode=mode, values=values))

    def _action_filter(
        self,
        element: etree._Element,
        group: etree._Element | None,
        name: str,
        sheet: SheetState,
        point: StoryPoint,
        label: str,
        delta: SheetDelta,
    ) -> None:
        """篩選動作的篩選：選取的標記（成員的組合），或所有的值（沒有選取）。"""
        if group is not None and self._all(group):
            levels = [self._level(sheet, g.get("level", "")) for g in group.iter("groupfilter")]
            uses = [u for u in levels if u is not None]
            action = self._action(name, sheet, uses)
            # 所有的值（沒有選取）：工作表上這個動作的篩選存檔時有值的話，故事點中改成所有的值
            # （StorySheet.cleared_actions）；存檔時就是所有的值時不必記
            worksheet = self.sheets_by_id.get(sheet.id)
            saved = next(
                (
                    f
                    for f in (worksheet.filters if worksheet is not None else [])
                    if isinstance(f, TupleFilter) and f.action == action
                ),
                None,
            )
            if saved is None:
                # 工作表上沒有這個動作的篩選：所有的值就是不篩選，畫面相同
                self.ctx.ignore(
                    element, "故事點記下的篩選動作沒有選取，工作表上也沒有這個篩選：略過"
                )
            elif (
                not (saved.mode == "all" or (saved.mode == "exclude" and not saved.tuples))
                and action is not None
                and action not in delta.cleared_actions
            ):
                delta.cleared_actions.append(action)
            return
        tuples = self._tuples(group)
        if not tuples:
            self._unsupported(element, label, "篩選動作")
            return
        levels = [self._level(sheet, m.get("level", "")) for m in tuples[0]]
        uses = [u for u in levels if u is not None]
        action = self._action(name, sheet, uses) if len(uses) == len(levels) else None
        if action is None:
            self._unsupported(element, label, "篩選動作")
            return
        marks: list[Mark] = []
        try:
            for members in tuples:
                mark: Mark = {}
                for member in members:
                    use = self._level(sheet, member.get("level", ""))
                    if use is None:
                        self._unsupported(element, label, "篩選動作")
                        return
                    mark[use_key(use)] = self._value(sheet, use, member.get("member"))
                if mark not in marks:
                    marks.append(mark)
        except LiteralError:
            self._unsupported(element, label, "篩選動作的值")
            return
        self._add_action(element, point, StoryAction(action=action, marks=marks), label)

    def _range(
        self, element: etree._Element, sheet: SheetState, point: StoryPoint, label: str
    ) -> None:
        """範圍篩選（<filter class='quantitative' included-values=…><min>…<max>…）：維度（含連續的日期）
        的範圍；量值（彙總）的篩選與檢視者的篩選相同，不能由故事點改變，列為不支援。"""
        use = self._level(sheet, element.get("column", ""))
        if use is None or use.agg is not None or use.table_calc is not None:
            self._unsupported(element, label, "篩選")
            return
        included = element.get("included-values")
        if included == "all":
            point.filters.append(
                StoryFilter(worksheet=sheet.id, field=use, mode="exclude", values=[])
            )
            return
        low, high = child(element, "min"), child(element, "max")
        if included not in INCLUDED_NULL or (low is None and high is None):
            self._unsupported(element, label, f"範圍篩選（{included}）")
            return
        try:
            start = self._value(sheet, use, text_of(low)) if low is not None else None
            end = self._value(sheet, use, text_of(high)) if high is not None else None
        except LiteralError:
            self._unsupported(element, label, "範圍篩選的值")
            return
        point.filters.append(
            StoryFilter(
                worksheet=sheet.id,
                field=use,
                mode="range",
                min=start,
                max=end,
                include_null=INCLUDED_NULL[included],
            )
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
        """記下的選取：<tuple-selection>（標記的欄位與值）或 <node-selection>（標題的成員）；只選取座標軸
        （沒有成員）時不選取標記。"""
        marks: list[Mark] = []
        for node in children(element):
            tag = local_name(node)
            found = (
                self._tuple_marks(node, sheet)
                if tag == "tuple-selection"
                else self._node_marks(node, sheet)
                if tag == "node-selection"
                else None
            )
            if found is None:
                self._unsupported(element, label, "選取")
                return
            marks += [m for m in found if m not in marks]
        if marks:
            point.selections.append(StorySelection(worksheet=sheet.id, marks=marks))

    def _tuple_marks(self, node: etree._Element, sheet: SheetState) -> list[Mark] | None:
        """<tuple-selection><tuple-reference>：欄位（<columns>）與值（<tuple>）；標記以其中的維度表示
        （量值與連續的欄位不用來找標記，與標記的註解相同）。"""
        marks: list[Mark] = []
        for reference in children(node, "tuple-reference"):
            columns = children(child(child(reference, "tuple-descriptor"), "columns"), "field")
            values = children(child(reference, "tuple"), "value")
            if not columns or len(columns) != len(values):
                return None
            mark: Mark = {}
            for column, value in zip(columns, values, strict=True):
                use = self._level(sheet, text_of(column))
                if use is None:
                    return None
                if (
                    use.agg is not None
                    or use.table_calc is not None
                    or use.continuous
                    or use.field in SPECIAL_FIELDS
                ):
                    continue
                try:
                    mark[use_key(use)] = self._value(sheet, use, text_of(value))
                except LiteralError:
                    return None
            if not mark:
                return None
            if mark not in marks:
                marks.append(mark)
        return marks

    def _node_marks(self, node: etree._Element, sheet: SheetState) -> list[Mark] | None:
        """<node-selection>：標題的成員（<node-reference> 的欄位與值）。"""
        marks: list[Mark] = []
        for reference in descendants(node, "node-reference"):
            refs = [text_of(f) for f in children(child(reference, "fields"), "field")]
            buckets = [text_of(b) for b in children(child(reference, "multibucket"), "bucket")]
            if not refs and child(reference, "axis") is not None:
                self.ctx.ignore(reference, "故事點記下的座標軸選取（不選取標記）")
                continue
            levels = [self._level(sheet, ref) for ref in refs]
            uses = [u for u in levels if u is not None]
            if not refs or len(refs) != len(buckets) or len(uses) != len(levels):
                return None
            try:
                mark: Mark = {
                    use_key(use): self._value(sheet, use, bucket)
                    for use, bucket in zip(uses, buckets, strict=True)
                }
            except LiteralError:
                return None
            if mark not in marks:
                marks.append(mark)
        return marks


__all__ = ["StoryParser"]
