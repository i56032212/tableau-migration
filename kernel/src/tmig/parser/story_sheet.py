"""故事點記下的工作表狀態（篩選、選取與參數之外）：<capturedDeltas><worksheet> 中的排序、欄列架、註解、
格式（座標軸、地圖的範圍）與頁面架目前的頁 → StorySheet、StoryPoint.pages。

寫法取自公開報表（GitHub 上公開的 .twb，見 README「已知限制與待驗證」的 Story）：

- 排序：<computed-sort>、<natural-sort> 等與工作表 <view> 中相同的排序元素（取代同一個欄位的排序）；
  <shelf-sort-deltas><clear-shelf-sorts><clear-shelf-sort-v2 dimension shelf> 清除欄列架的排序
- 欄列架：<rows>（<cols>、<columns>）<column delta-type='removed' field=…>，從架上移除這個欄位；
  <column delta-type='added' field=… index=…> 加在架上的這個位置（移除之後的位置，推定：樣本都是先移除
  再加在同一個位置，例如把週改成季）
- 註解：<annotations><annotation delta-type='added'|'edited'|'removed' id=…>（其餘與工作表的註解相同）
- 格式：<style delta-type='added'>，加在工作表 <style> 上的 style-rule：座標軸的範圍與標題、標題成員的
  文字方向、地圖的顯示範圍與「N 個 Null」的指示器
- 頁面架目前的頁：<current-page><multibucket><bucket>
- <datasource-dependencies>：其他狀態用到、工作表原本沒有的欄位實例

認不得的寫法列為不支援（故事點會顯示成不同的畫面）。
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable

from lxml import etree

from tmig.ir.models import (
    GENERATED_FIELDS,
    MEASURE_NAMES,
    MEASURE_VALUES,
    Annotation,
    Axis,
    FieldUse,
    Filter,
    MapViewport,
    Scalar,
    Sort,
    StorySheet,
    StoryShelfField,
    Worksheet,
)
from tmig.parser.context import ParseContext, SheetState
from tmig.parser.fields import bucket_values, instance_key, value_type
from tmig.parser.formats import worksheet_formats
from tmig.parser.maps import map_options
from tmig.parser.twbx import child, children, local_name, text_of
from tmig.parser.values import LiteralError, parse_literal
from tmig.parser.worksheet import SheetParser, _order
from tmig.planner.state import use_key

SORT_TAGS = frozenset({"sort", "computed-sort", "manual-sort", "natural-sort", "alphabetic-sort"})
STYLE_ELEMENTS = frozenset({"axis", "label"})
"""故事點的 <style> 差異中，依工作表的格式重新解析的 style-rule（座標軸、標題成員）。"""
INCLUDED_NULL = {"in-range": False, "in-range-or-null": True}
"""範圍篩選的 included-values：是否也保留 Null 值。"""

Flag = Callable[[etree._Element, str], None]


class SheetDelta:
    """一張工作表在一個故事點中的差異。flag(元素, 什麼)：記為不支援（訊息帶 Story 與故事點的名稱）。"""

    def __init__(
        self,
        ctx: ParseContext,
        sheet: SheetState,
        worksheet: Worksheet,
        element: etree._Element,
        flag: Flag,
    ) -> None:
        self.ctx = ctx
        self.sheet = sheet
        self.worksheet = worksheet
        self.element = element
        self.flag = flag
        self.parser = SheetParser(ctx, element)
        self.parser.ds = sheet.ds
        # 資料源層級的欄位實例（資料源篩選用）：故事點的篩選也會用到工作表上沒有的欄位實例
        self.parser.instances = {**sheet.ds.instances, **sheet.instances}
        self.parser.uses = dict(sheet.uses)
        self.sorts: list[Sort] = []
        self.cleared: list[str] = []
        self.removed: dict[str, list[FieldUse]] = {"rows": [], "cols": []}
        self.added: dict[str, list[StoryShelfField]] = {"rows": [], "cols": []}
        self.filters: list[Filter] = []
        """量值、Top N 與條件篩選（以工作表的寫法解析，見 tmig.parser.story）。"""
        self.removed_filters: list[str] = []
        self.annotations: list[Annotation] | None = None
        self.axes: list[Axis] = []
        self.viewport: MapViewport | None = None
        self.null_indicator: bool | None = None
        self.cleared_actions: list[str] = []
        self.page: tuple[bool, Scalar] = (False, None)
        self._annotations: etree._Element | None = None
        self._style: etree._Element | None = None
        self._workbook_style: etree._Element | None = None
        self._restyled = False

    # ------------------------------------------------------------ 共用

    def state(self) -> SheetState:
        """篩選與選取用的工作表狀態：加上 <datasource-dependencies> 差異中的欄位實例。"""
        return dataclasses.replace(self.sheet, instances=self.parser.instances)

    def resolve(self, ref: str | None, anchor: etree._Element, what: str) -> FieldUse | None:
        found = self.parser.resolve(ref) if ref else "缺少欄位"
        if isinstance(found, str):
            self.flag(anchor, f"{what}（{found}）")
            return None
        return found

    def result(self) -> StorySheet | None:
        """有差異時回傳 StorySheet（頁面架目前的頁另外記在 StoryPoint.pages，見 page）。"""
        if self._restyled and self._style is not None:
            self._formats(self._style, self._workbook_style)
        state = StorySheet(
            worksheet=self.worksheet.id,
            sorts=self.sorts,
            cleared_sorts=self.cleared,
            removed_rows=self.removed["rows"],
            removed_columns=self.removed["cols"],
            added_rows=self.added["rows"],
            added_columns=self.added["cols"],
            filters=self.filters,
            removed_filters=self.removed_filters,
            annotations=self.annotations,
            axes=self.axes,
            viewport=self.viewport,
            null_indicator=self.null_indicator,
            cleared_actions=self.cleared_actions,
        )
        empty = StorySheet(worksheet=self.worksheet.id)
        return None if state == empty else state

    # ------------------------------------------------------------ 欄位實例

    def dependencies(self, item: etree._Element) -> None:
        """<datasource-dependencies>：其他差異（篩選等）用到的欄位實例。"""
        if item.get("datasource") != self.sheet.ds.name:
            self.flag(item, f"其他資料源（{item.get('datasource')}）的欄位")
            return
        for entry in children(item):
            tag = local_name(entry)
            name = entry.get("name", "")
            if tag == "column-instance":
                self.parser.instances[instance_key(name)] = entry
            elif tag == "column" and (
                name in self.sheet.ds.by_name or name in self.sheet.ds.auto_groups
            ):
                continue
            else:
                self.flag(entry, f"欄位 {name}")

    # ------------------------------------------------------------ 排序

    def sort(self, item: etree._Element) -> None:
        tag = local_name(item)
        column = item.get("column", "")
        if instance_key(column) == "[:Measure Names]":
            self.flag(item, "Measure Names 的排序")
            return
        use = self.resolve(column, item, "排序的欄位")
        if use is None:
            return
        kind = item.get("class", "")
        if tag == "computed-sort":
            kind = "computed"
        elif tag == "manual-sort":
            kind = "manual"
        elif tag in ("natural-sort", "alphabetic-sort"):
            kind = "alphabetic"
        order = _order(item.get("direction"))
        sort: Sort | None = None
        if kind == "computed":
            by = self.resolve(item.get("using"), item, "排序依據")
            if by is None:
                return
            sort = Sort(field=use.field, kind="by_field", order=order, by=by)
        elif kind in ("alphabetic", "natural"):
            sort = Sort(field=use.field, kind="alphabetic", order=order)
        elif kind == "datasource":
            sort = Sort(field=use.field, kind="data_source", order=order)
        elif kind == "manual":
            field = self.sheet.ds.field_by_id(use.field)
            try:
                values = bucket_values(child(item, "dictionary"), value_type(field, use))
            except LiteralError:
                values = []
            if values:
                sort = Sort(field=use.field, kind="manual", order=order, manual_values=values)
        if sort is None:
            self.flag(item, f"排序（{tag} {kind}）")
            return
        self.sorts = [s for s in self.sorts if s.field != sort.field] + [sort]

    def clear_sorts(self, item: etree._Element) -> None:
        """<shelf-sort-deltas><clear-shelf-sorts><clear-shelf-sort-v2>：清除欄列架的排序（巢狀排序）。"""
        for part in children(item):
            if local_name(part) != "clear-shelf-sorts":
                self.flag(part, f"欄列架的排序 <{local_name(part)}>")
                continue
            for entry in children(part):
                if local_name(entry) != "clear-shelf-sort-v2":
                    self.flag(entry, f"欄列架的排序 <{local_name(entry)}>")
                    continue
                use = self.resolve(entry.get("dimension"), entry, "清除排序的欄位")
                if use is None:
                    continue
                if use.field not in self.cleared:
                    self.cleared.append(use.field)
                self.sorts = [s for s in self.sorts if not (s.field == use.field and s.nested)]

    # ------------------------------------------------------------ 欄列架

    def shelf(self, item: etree._Element) -> None:
        """<rows>／<cols>（<columns>）的差異：<column delta-type='removed' field=…> 從架上移除，
        <column delta-type='added' field=… index=…> 加在架上。"""
        shelf = "rows" if local_name(item) == "rows" else "cols"
        for entry in children(item):
            delta = entry.get("delta-type")
            if local_name(entry) != "column" or delta not in ("removed", "added"):
                self.flag(entry, f"欄列架的變更（{delta or local_name(entry)}）")
                continue
            if delta == "added":
                self._added(shelf, entry)
                continue
            use = self.resolve(entry.get("field"), entry, "從欄列架移除的欄位")
            if use is None:
                continue
            on_shelf = self.worksheet.rows if shelf == "rows" else self.worksheet.columns
            if all(use_key(u) != use_key(use) for u in on_shelf):
                self.ctx.ignore(entry, "故事點從欄列架移除的欄位已不在工作表上：略過")
                continue
            self.removed[shelf].append(use)

    def _added(self, shelf: str, entry: etree._Element) -> None:
        """加在欄列架上的欄位：工作表沒有的 Table Calculation 設定、Measure Names／Values 尚不支援。"""
        use = self.resolve(entry.get("field"), entry, "加在欄列架上的欄位")
        if use is None:
            return
        if use.field in (MEASURE_NAMES, MEASURE_VALUES):
            self.flag(entry, "加在欄列架上的 Measure Names／Measure Values")
            return
        key = instance_key(entry.get("field", ""))
        instance = self.parser.instances.get(key)
        new_calc = (
            key not in self.sheet.uses
            and instance is not None
            and child(instance, "table-calc") is not None
        )
        if new_calc or (
            use.table_calc is not None
            and all(t.id != use.table_calc for t in self.worksheet.table_calcs)
        ):
            self.flag(entry, "加在欄列架上、工作表沒有的 Table Calculation 欄位")
            return
        try:
            index = max(int(entry.get("index", "0")), 0)
        except ValueError:
            self.flag(entry, f"加在欄列架上的位置 {entry.get('index')}")
            return
        self.added[shelf].append(StoryShelfField(use=use, index=index))

    # ------------------------------------------------------------ 註解

    def annotation_deltas(self, item: etree._Element) -> None:
        """註解的差異：套在工作表的 <annotations>（或前一個差異的結果）上，再以工作表的寫法解析。"""
        if self._annotations is None:
            base = child(child(self.element, "table"), "annotations")
            self._annotations = (
                copy.deepcopy(base) if base is not None else etree.Element("annotations")
            )
        merged = self._annotations
        for entry in children(item):
            delta = entry.get("delta-type")
            if local_name(entry) != "annotation" or delta not in ("added", "edited", "removed"):
                self.flag(entry, f"註解的變更（{delta or local_name(entry)}）")
                continue
            existing = next(
                (a for a in children(merged, "annotation") if a.get("id") == entry.get("id")),
                None,
            )
            if delta == "removed":
                if existing is not None:
                    merged.remove(existing)
                continue
            clean = copy.deepcopy(entry)
            del clean.attrib["delta-type"]
            if existing is not None:
                existing.addprevious(clean)
                merged.remove(existing)
            else:
                # 修改的註解在工作表上找不到時當成新增的（推定）
                merged.append(clean)
        before = len(self.ctx.ignored)
        self.annotations = self.parser._annotations(merged)
        # 工作表本身的註解已經解析過一次：同樣的略過紀錄不再重複
        seen = {(u.reason, u.xml) for u in self.ctx.ignored[:before]}
        self.ctx.ignored[before:] = [
            u for u in self.ctx.ignored[before:] if (u.reason, u.xml) not in seen
        ]

    # ------------------------------------------------------------ 格式

    def style(self, item: etree._Element, workbook_style: etree._Element | None) -> None:
        """<style delta-type='added'>：加在工作表 <style> 上的 style-rule。座標軸與標題成員的設定以
        工作表的寫法重新解析，記下與工作表不同的座標軸；地圖的顯示範圍與 Null 值的指示器另外記下。"""
        if item.get("delta-type") != "added":
            self.flag(item, f"格式的變更（{item.get('delta-type')}）")
            return
        if self._style is None:
            base = child(child(self.element, "table"), "style")
            self._style = copy.deepcopy(base) if base is not None else etree.Element("style")
        merged = self._style
        for rule in children(item):
            element = rule.get("element")
            if local_name(rule) != "style-rule":
                self.flag(rule, f"格式 <{local_name(rule)}>")
                continue
            if element == "annotation":
                self.ctx.ignore(
                    rule, "故事點中註解的外觀（框線、底色）：與工作表上的註解相同，以預設的外觀畫出"
                )
                continue
            if element == "table":
                self._table_formats(rule)
                continue
            if element not in STYLE_ELEMENTS:
                self.flag(rule, f"格式（{element}）")
                continue
            target = next(
                (r for r in children(merged, "style-rule") if r.get("element") == element), None
            )
            if target is None:
                target = etree.SubElement(merged, "style-rule", element=element or "")
            for entry in children(rule):
                key = _format_key(entry)
                for old in children(target):
                    if _format_key(old) == key:
                        target.remove(old)
                target.append(copy.deepcopy(entry))
            self._restyled = True
        self._workbook_style = workbook_style

    def _table_formats(self, rule: etree._Element) -> None:
        for entry in children(rule):
            if local_name(entry) == "format" and entry.get("attr") == "show-null-value-warning":
                self.null_indicator = entry.get("value") != "false"
            else:
                self.flag(entry, f"工作表的格式（{entry.get('attr') or local_name(entry)}）")

    def _formats(self, style: etree._Element, workbook_style: etree._Element | None) -> None:
        ws = self.worksheet
        removed = {use_key(u) for shelf in self.removed.values() for u in shelf}
        rows = [u for u in ws.rows if use_key(u) not in removed]
        cols = [u for u in ws.columns if use_key(u) not in removed]
        rows += [a.use for a in self.added["rows"]]
        cols += [a.use for a in self.added["cols"]]
        formats = worksheet_formats(style, self.parser.resolve, rows, cols, workbook_style)
        original = {(use_key(a.use), a.shelf): a for a in ws.axes}
        self.axes = [a for a in formats.axes if original.get((use_key(a.use), a.shelf)) != a]
        if any(u.field in GENERATED_FIELDS for u in [*rows, *cols]):
            hide = ws.map.hide_unknown if ws.map is not None else False
            options = map_options(style, hide, ws.map.basemap if ws.map is not None else True)
            viewport = options.viewport if options is not None else None
            if viewport != (ws.map.viewport if ws.map is not None else None):
                self.viewport = viewport

    # ------------------------------------------------------------ 頁

    def current_page(self, item: etree._Element) -> None:
        """<current-page><multibucket><bucket>：頁面架目前的頁。"""
        use = self.worksheet.pages
        buckets = children(child(item, "multibucket"), "bucket")
        if use is None or len(buckets) != 1 or use.field == MEASURE_NAMES:
            self.flag(item, "頁面架目前的頁")
            return
        field = self.sheet.ds.field_by_id(use.field)
        try:
            self.page = (True, parse_literal(text_of(buckets[0]), value_type(field, use)))
        except LiteralError:
            self.flag(item, f"頁面架目前的頁 {text_of(buckets[0])}")


def _format_key(entry: etree._Element) -> tuple[str | None, ...]:
    """同一個設定的鍵：元素、屬性、欄位、範圍、軸的編號與 id（後加的取代先前的）。"""
    return (
        local_name(entry),
        entry.get("attr"),
        entry.get("field"),
        entry.get("scope"),
        entry.get("class"),
        entry.get("id"),
    )


__all__ = ["SORT_TAGS", "SheetDelta"]
