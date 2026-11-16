"""欄位的預設屬性（Tableau 資料窗格的「預設屬性」：色彩、形狀、排序、總計使用）套用到工作表。

與 Tableau 相同：工作表沒有另外設定時依欄位的預設，工作表上的設定優先。查詢與繪圖只看工作表，
所以預設在編輯時寫進工作表——工作表存回時補上缺的設定（``apply_field_defaults``），預設改變時
把仍沿用舊預設的工作表換成新的（``repush_field_defaults``）。
"""

from __future__ import annotations

from tmig.ir import IR, DataField, FieldUse, Marks, Sort, VisualTotal, Worksheet
from tmig.planner.state import use_key


def _has_defaults(field: DataField) -> bool:
    return bool(
        field.default_color is not None
        or field.default_shapes
        or field.default_sort is not None
        or field.total_using is not None
    )


def _plain_dimension(use: FieldUse, field: DataField) -> bool:
    """欄位本身的值（沒有彙總、日期衍生或表計算）當成離散的維度使用。"""
    return (
        field.role == "dimension"
        and use.agg is None
        and use.date is None
        and use.table_calc is None
        and not use.continuous
    )


def _marks_defaults(marks: Marks, fields: dict[str, DataField]) -> Marks:
    update: dict[str, object] = {}
    color = marks.color
    if color is not None and marks.color_palette is None:
        field = fields.get(color.field)
        palette = field.default_color if field is not None else None
        if palette is not None and field is not None:
            discrete = palette.kind == "discrete"
            # 離散的色彩是成員的顏色：只用在欄位本身的值；連續的色彩用在連續的量值
            if (discrete and _plain_dimension(color, field)) or (not discrete and color.continuous):
                update["color_palette"] = palette
    shape = marks.shape
    if shape is not None and not marks.shape_assignments:
        field = fields.get(shape.field)
        if field is not None and field.default_shapes and _plain_dimension(shape, field):
            update["shape_assignments"] = list(field.default_shapes)
    return marks.model_copy(update=update) if update else marks


def _view_uses(sheet: Worksheet) -> list[FieldUse]:
    uses = [*sheet.rows, *sheet.columns, *sheet.marks.uses(), *sheet.measure_values]
    for pane in sheet.panes:
        uses += pane.marks.uses()
    return uses


def apply_field_defaults(ir: IR, sheet: Worksheet) -> Worksheet:
    """工作表上沒有設定的色彩、形狀、排序與總計的計算方式，依欄位的預設屬性補上。"""
    fields = {f.id: f for d in ir.datasources for f in d.fields if _has_defaults(f)}
    if not fields:
        return sheet
    update: dict[str, object] = {}
    marks = _marks_defaults(sheet.marks, fields)
    if marks is not sheet.marks:
        update["marks"] = marks
    panes = [
        pane.model_copy(update={"marks": updated})
        if (updated := _marks_defaults(pane.marks, fields)) is not pane.marks
        else pane
        for pane in sheet.panes
    ]
    if any(new is not old for new, old in zip(panes, sheet.panes, strict=True)):
        update["panes"] = panes
    uses = _view_uses(sheet)
    sorted_fields = {s.field for s in sheet.sorts}
    sorts = list(sheet.sorts)
    for use in uses:
        field = fields.get(use.field)
        if (
            field is None
            or field.default_sort is None
            or use.field in sorted_fields
            or not _plain_dimension(use, field)
        ):
            continue
        sorted_fields.add(use.field)
        default = field.default_sort
        sorts.append(
            Sort(
                field=use.field,
                kind=default.kind,
                order=default.order,
                by=default.by,
                manual_values=list(default.manual_values),
            )
        )
    if len(sorts) != len(sheet.sorts):
        update["sorts"] = sorts
    if sheet.totals or sheet.subtotals:
        totals = list(sheet.visual_totals)
        known = {use_key(v.use) for v in totals}
        for use in uses:
            field = fields.get(use.field)
            if (
                field is None
                or field.total_using is None
                or use.table_calc is not None
                or use.agg is None
                or use_key(use) in known
            ):
                continue
            known.add(use_key(use))
            totals.append(VisualTotal(use=use, agg=field.total_using))
        if len(totals) != len(sheet.visual_totals):
            update["visual_totals"] = totals
    return sheet.model_copy(update=update) if update else sheet


def _strip_marks(marks: Marks, old: DataField) -> Marks:
    update: dict[str, object] = {}
    if (
        marks.color is not None
        and marks.color.field == old.id
        and marks.color_palette is not None
        and marks.color_palette == old.default_color
    ):
        update["color_palette"] = None
    if (
        marks.shape is not None
        and marks.shape.field == old.id
        and marks.shape_assignments
        and marks.shape_assignments == old.default_shapes
    ):
        update["shape_assignments"] = []
    return marks.model_copy(update=update) if update else marks


def _strip_defaults(sheet: Worksheet, old: DataField) -> Worksheet:
    """拿掉工作表上與欄位舊的預設相同的設定（沿用預設的工作表），之後再補上新的預設。"""
    update: dict[str, object] = {
        "marks": _strip_marks(sheet.marks, old),
        "panes": [p.model_copy(update={"marks": _strip_marks(p.marks, old)}) for p in sheet.panes],
    }
    if old.default_sort is not None:
        default = old.default_sort
        update["sorts"] = [
            s
            for s in sheet.sorts
            if not (
                s.field == old.id
                and s.kind == default.kind
                and s.order == default.order
                and s.manual_values == default.manual_values
                and s.by == default.by
                and not s.nested
            )
        ]
    if old.total_using is not None:
        update["visual_totals"] = [
            v
            for v in sheet.visual_totals
            if not (v.use.field == old.id and v.agg == old.total_using)
        ]
    return sheet.model_copy(update=update)


def repush_field_defaults(ir: IR, old: DataField) -> IR:
    """欄位的預設屬性改變之後（ir 中已經是新的欄位）：沿用舊預設的工作表改用新的預設。"""
    worksheets = [
        apply_field_defaults(ir, _strip_defaults(sheet, old))
        if any(use.field == old.id for use in _view_uses(sheet))
        else sheet
        for sheet in ir.worksheets
    ]
    return ir.model_copy(update={"worksheets": worksheets})


__all__ = ["apply_field_defaults", "repush_field_defaults"]
