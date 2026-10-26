"""IR 的跨實體參照檢查。

規則：

- 除了區塊（zone）之外，所有 id 在整份 IR 中唯一；區塊 id 在所屬 dashboard 中唯一
- 工作表的欄位使用、篩選、排序、Table Calculation 只能參照該工作表資料源的欄位
- 參照的參數、工作表、Table Calculation 必須存在
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmig.ir.models import IR, FieldUse, Filter, Zone


def iter_zones(zones: list[Zone]) -> Iterator[Zone]:
    """依文件順序走訪區塊樹。"""
    for zone in zones:
        yield zone
        yield from iter_zones(zone.children)


def check_references(ir: IR) -> list[str]:
    from tmig.ir.models import ParamRef, TopNFilter

    errors: list[str] = []
    owner: dict[str, str] = {}

    def claim(id_: str, where: str) -> None:
        if id_ in owner:
            errors.append(f"{where}：id「{id_}」與 {owner[id_]} 重複")
        else:
            owner[id_] = where

    claim(ir.workbook.id, "workbook")
    fields_by_ds: dict[str, set[str]] = {}
    for ds in ir.datasources:
        claim(ds.id, f"datasources[{ds.id}]")
        fields_by_ds[ds.id] = set()
        for fld in ds.fields:
            claim(fld.id, f"datasources[{ds.id}].fields[{fld.id}]")
            fields_by_ds[ds.id].add(fld.id)
        for flt in ds.filters:
            claim(flt.id, f"datasources[{ds.id}].filters[{flt.id}]")
    for prm in ir.parameters:
        claim(prm.id, f"parameters[{prm.id}]")
    for ws in ir.worksheets:
        claim(ws.id, f"worksheets[{ws.id}]")
        for flt in ws.filters:
            claim(flt.id, f"worksheets[{ws.id}].filters[{flt.id}]")
        for tc in ws.table_calcs:
            claim(tc.id, f"worksheets[{ws.id}].table_calcs[{tc.id}]")
    for db in ir.dashboards:
        claim(db.id, f"dashboards[{db.id}]")

    params = {prm.id for prm in ir.parameters}
    sheets = {ws.id: ws for ws in ir.worksheets}

    def check_use(use: FieldUse, ds_id: str, where: str, table_calcs: set[str] | None) -> None:
        if use.field not in fields_by_ds.get(ds_id, set()):
            errors.append(f"{where}：欄位「{use.field}」不在資料源「{ds_id}」中")
        if use.table_calc is None:
            return
        if table_calcs is None:
            errors.append(f"{where}：此處不能套用 Table Calculation")
        elif use.table_calc not in table_calcs:
            errors.append(f"{where}：Table Calculation「{use.table_calc}」不存在")

    def check_filter(flt: Filter, ds_id: str, where: str, table_calcs: set[str] | None) -> None:
        check_use(flt.field, ds_id, f"{where}.field", table_calcs)
        if (
            isinstance(flt, TopNFilter)
            and isinstance(flt.count, ParamRef)
            and flt.count.parameter not in params
        ):
            errors.append(f"{where}.count：參數「{flt.count.parameter}」不存在")

    for ds in ir.datasources:
        for flt in ds.filters:
            check_filter(flt, ds.id, f"datasources[{ds.id}].filters[{flt.id}]", None)

    for ws in ir.worksheets:
        base = f"worksheets[{ws.id}]"
        if ws.datasource not in fields_by_ds:
            errors.append(f"{base}.datasource：資料源「{ws.datasource}」不存在")
            continue
        tcs = {tc.id for tc in ws.table_calcs}
        shelves = [("rows", ws.rows), ("columns", ws.columns), ("marks.label", ws.marks.label)]
        shelves += [("marks.detail", ws.marks.detail), ("marks.tooltip", ws.marks.tooltip)]
        for name, uses in shelves:
            for i, use in enumerate(uses):
                check_use(use, ws.datasource, f"{base}.{name}[{i}]", tcs)
        for name, single in (("marks.color", ws.marks.color), ("marks.size", ws.marks.size)):
            if single is not None:
                check_use(single, ws.datasource, f"{base}.{name}", tcs)
        for flt in ws.filters:
            check_filter(flt, ws.datasource, f"{base}.filters[{flt.id}]", tcs)
        for i, srt in enumerate(ws.sorts):
            if srt.field not in fields_by_ds[ws.datasource]:
                errors.append(
                    f"{base}.sorts[{i}]：欄位「{srt.field}」不在資料源「{ws.datasource}」中"
                )
            if srt.by is not None:
                check_use(srt.by, ws.datasource, f"{base}.sorts[{i}].by", tcs)
        for tc in ws.table_calcs:
            for fid in tc.addressing:
                if fid not in fields_by_ds[ws.datasource]:
                    errors.append(
                        f"{base}.table_calcs[{tc.id}].addressing：欄位「{fid}」不在資料源「{ws.datasource}」中"
                    )

    for db in ir.dashboards:
        seen: set[str] = set()
        for zone in iter_zones(db.zones):
            where = f"dashboards[{db.id}].zones[{zone.id}]"
            if zone.id in seen:
                errors.append(f"{where}：區塊 id 重複")
            seen.add(zone.id)
            if zone.parameter is not None and zone.parameter not in params:
                errors.append(f"{where}：參數「{zone.parameter}」不存在")
            if zone.worksheet is None:
                continue
            sheet = sheets.get(zone.worksheet)
            if sheet is None:
                errors.append(f"{where}：工作表「{zone.worksheet}」不存在")
            elif zone.field is not None:
                check_use(zone.field, sheet.datasource, f"{where}.field", None)

    return errors
