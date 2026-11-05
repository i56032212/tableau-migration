"""IR 的跨實體參照檢查。

規則：

- 除了區塊（zone）之外，所有 id 在整份 IR 中唯一；區塊 id 在所屬 dashboard 中唯一
- 工作表的欄位使用、篩選、排序、Table Calculation 只能參照該工作表資料源的欄位；
  欄列架與標記卡另可使用 Measure Names／Values（measure_names、measure_values）
- 參照的參數、工作表、Table Calculation 必須存在；按鈕的目標 dashboard、工作表必須存在，
  顯示或隱藏的區塊必須在同一個 dashboard 上
- Relationship 模型：邏輯資料表 id 不重複；關聯兩端的資料表存在、條件的欄位屬於該資料表；
  關聯連成一棵樹（不成環、每張資料表都連得到）；實體欄位都屬於模型中的某張資料表
- 萃取的更新資訊：來源欄位對應的欄位存在且是實體欄位、不重複；增量更新的欄位也是
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmig.ir.models import IR, Datasource, FieldUse, Filter, ModelRelation, Zone


def iter_zones(zones: list[Zone]) -> Iterator[Zone]:
    """依文件順序走訪區塊樹。"""
    for zone in zones:
        yield zone
        yield from iter_zones(zone.children)


def model_of(ds: Datasource) -> ModelRelation | None:
    """資料源的 Relationship 模型（Live 連線的關聯，或萃取的 model）。"""
    from tmig.ir.models import ExtractConnection, ModelRelation

    connection = ds.connection
    if isinstance(connection, ExtractConnection):
        return connection.model
    return connection.relation if isinstance(connection.relation, ModelRelation) else None


def _check_model(ds: Datasource, errors: list[str]) -> None:
    model = model_of(ds)
    if model is None:
        return
    where = f"datasources[{ds.id}].model"
    tables = [t.id for t in model.tables]
    if len(set(tables)) != len(tables):
        errors.append(f"{where}：邏輯資料表 id 重複")
    table_set = set(tables)
    fields = {f.id: f for f in ds.fields}
    parent = {t: t for t in tables}

    def find(t: str) -> str:
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    for i, rel in enumerate(model.relationships):
        if rel.left not in table_set or rel.right not in table_set:
            errors.append(
                f"{where}.relationships[{i}]：資料表「{rel.left}」或「{rel.right}」不存在"
            )
            continue
        for cond in rel.on:
            for side, fid in ((rel.left, cond.left), (rel.right, cond.right)):
                field = fields.get(fid)
                if field is None:
                    errors.append(f"{where}.relationships[{i}]：欄位「{fid}」不在資料源中")
                elif field.table != side:
                    errors.append(
                        f"{where}.relationships[{i}]：欄位「{fid}」不屬於資料表「{side}」"
                    )
        a, b = find(rel.left), find(rel.right)
        if a == b:
            errors.append(f"{where}.relationships[{i}]：關聯成環（Tableau 的關聯必須是一棵樹）")
        parent[a] = b
    if len({find(t) for t in tables}) > 1:
        errors.append(f"{where}：有邏輯資料表沒有連到其他資料表")
    for field in ds.fields:
        if field.column is not None and field.table not in table_set:
            errors.append(
                f"datasources[{ds.id}].fields[{field.id}]：所屬的資料表「{field.table}」不在模型中"
            )


def _check_refresh(ds: Datasource, errors: list[str]) -> None:
    from tmig.ir.models import ExtractConnection

    connection = ds.connection
    if not isinstance(connection, ExtractConnection) or connection.refresh is None:
        return
    where = f"datasources[{ds.id}].connection.refresh"
    physical = {f.id for f in ds.fields if f.column is not None}
    seen: set[str] = set()
    for column in connection.refresh.columns:
        if column.field not in physical:
            errors.append(f"{where}：欄位「{column.field}」不存在或不是實體欄位")
        if column.field in seen:
            errors.append(f"{where}：欄位「{column.field}」重複")
        seen.add(column.field)
    increment = connection.refresh.increment
    if increment is not None and increment not in physical:
        errors.append(f"{where}：增量更新的欄位「{increment}」不存在或不是實體欄位")


def check_references(ir: IR) -> list[str]:
    from tmig.ir.models import SPECIAL_FIELDS, ParamRef, TopNFilter, TupleFilter

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
        _check_model(ds, errors)
        _check_refresh(ds, errors)
    for prm in ir.parameters:
        claim(prm.id, f"parameters[{prm.id}]")
    param_ids = {prm.id for prm in ir.parameters}
    for ds in ir.datasources:
        by_id = {fld.id: fld for fld in ds.fields}
        for fld in ds.fields:
            if fld.set is None:
                continue
            where = f"datasources[{ds.id}].fields[{fld.id}].set"
            for use in fld.set.levels:
                level = by_id.get(use.field)
                if level is None:
                    errors.append(f"{where}：層級欄位「{use.field}」不在資料源中")
                elif level.set is not None or use.agg is not None or use.table_calc is not None:
                    errors.append(f"{where}：層級「{use.field}」必須是一般的維度")
            rule = fld.set.rule
            if rule.kind == "members":
                if any(len(m) != len(fld.set.levels) for m in rule.members):
                    errors.append(f"{where}：成員的值與層級的數量不同")
            elif len(fld.set.levels) != 1:
                errors.append(f"{where}：Top N 與條件集合只能有一個層級")
            if (
                rule.kind == "top_n"
                and isinstance(rule.count, ParamRef)
                and rule.count.parameter not in param_ids
            ):
                errors.append(f"{where}：參數「{rule.count.parameter}」不存在")
    for ws in ir.worksheets:
        claim(ws.id, f"worksheets[{ws.id}]")
        for flt in ws.filters:
            claim(flt.id, f"worksheets[{ws.id}].filters[{flt.id}]")
        for tc in ws.table_calcs:
            claim(tc.id, f"worksheets[{ws.id}].table_calcs[{tc.id}]")
    for db in ir.dashboards:
        claim(db.id, f"dashboards[{db.id}]")
    for story in ir.stories:
        claim(story.id, f"stories[{story.id}]")

    for act in ir.actions:
        claim(act.id, f"actions[{act.id}]")
    params = {prm.id for prm in ir.parameters}
    sheets = {ws.id: ws for ws in ir.worksheets}
    actions = {act.id for act in ir.actions}
    boards = {db.id for db in ir.dashboards}
    for act in ir.actions:
        for name, ids in (("sources", act.sources), ("targets", act.targets)):
            for sid in ids:
                if sid not in sheets:
                    errors.append(f"actions[{act.id}].{name}：工作表「{sid}」不存在")
        for name, did in (("dashboard", act.dashboard), ("target_dashboard", act.target_dashboard)):
            if did is not None and did not in boards:
                errors.append(f"actions[{act.id}].{name}：dashboard「{did}」不存在")
        if act.target_worksheet is not None and act.target_worksheet not in sheets:
            errors.append(
                f"actions[{act.id}].target_worksheet：工作表「{act.target_worksheet}」不存在"
            )
        if act.parameter is not None and act.parameter.parameter not in params:
            errors.append(f"actions[{act.id}].parameter：參數「{act.parameter.parameter}」不存在")
        if act.set_action is not None:
            target = next(
                (
                    (ds, fld)
                    for ds in ir.datasources
                    for fld in ds.fields
                    if fld.id == act.set_action.target
                ),
                None,
            )
            if target is None or target[1].set is None or target[1].set.rule.kind != "members":
                errors.append(
                    f"actions[{act.id}].set_action：集合「{act.set_action.target}」不存在或不是固定成員的集合"
                )
            elif any(sheets[s].datasource != target[0].id for s in act.sources if s in sheets):
                errors.append(f"actions[{act.id}].set_action：來源工作表與集合在不同的資料源")

    def check_use(
        use: FieldUse,
        ds_id: str,
        where: str,
        table_calcs: set[str] | None,
        special: bool = False,
    ) -> None:
        if special and use.field in SPECIAL_FIELDS:
            if use.agg is not None or use.date is not None or use.table_calc is not None:
                errors.append(
                    f"{where}：Measure Names／Values 不能彙總、日期衍生或套用 Table Calculation"
                )
            return
        if use.field not in fields_by_ds.get(ds_id, set()):
            errors.append(f"{where}：欄位「{use.field}」不在資料源「{ds_id}」中")
        if use.table_calc is None:
            return
        if table_calcs is None:
            errors.append(f"{where}：此處不能套用 Table Calculation")
        elif use.table_calc not in table_calcs:
            errors.append(f"{where}：Table Calculation「{use.table_calc}」不存在")

    def check_filter(flt: Filter, ds_id: str, where: str, table_calcs: set[str] | None) -> None:
        if isinstance(flt, TupleFilter):
            for i, use in enumerate(flt.fields):
                check_use(use, ds_id, f"{where}.fields[{i}]", None)
            if flt.action is not None and flt.action not in actions:
                errors.append(f"{where}.action：篩選動作「{flt.action}」不存在")
            return
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
        for name, uses in (("rows", ws.rows), ("columns", ws.columns)):
            for i, use in enumerate(uses):
                check_use(use, ws.datasource, f"{base}.{name}[{i}]", tcs, special=True)
        cards = [("marks", ws.marks)] + [
            (f"panes[{i}].marks", p.marks) for i, p in enumerate(ws.panes)
        ]
        for name, card in cards:
            for i, use in enumerate(card.uses()):
                check_use(use, ws.datasource, f"{base}.{name}[{i}]", tcs, special=True)
        for i, pane in enumerate(ws.panes):
            check_use(pane.measure, ws.datasource, f"{base}.panes[{i}].measure", tcs)
            if pane.measure not in (ws.rows if pane.axis == "rows" else ws.columns):
                errors.append(f"{base}.panes[{i}].measure：量值不在{pane.axis}架上")
        for i, use in enumerate(ws.measure_values):
            check_use(use, ws.datasource, f"{base}.measure_values[{i}]", tcs)
        for i, alias in enumerate(ws.measure_aliases):
            if alias.use not in ws.measure_values:
                errors.append(f"{base}.measure_aliases[{i}]：量值不在 Measure Values 卡上")
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
            for nested in tc.nested:
                for fid in [nested.field, *nested.addressing]:
                    if fid not in fields_by_ds[ws.datasource]:
                        errors.append(
                            f"{base}.table_calcs[{tc.id}].nested：欄位「{fid}」不在資料源「{ws.datasource}」中"
                        )
        for line in ws.reference_lines:
            where = f"{base}.reference_lines[{line.id}]"
            check_use(line.axis, ws.datasource, f"{where}.axis", tcs)
            if isinstance(line.value, ParamRef):
                if line.value.parameter not in params:
                    errors.append(f"{where}.value：參數「{line.value.parameter}」不存在")
            elif line.value is not None:
                check_use(line.value, ws.datasource, f"{where}.value", tcs)

    for db in ir.dashboards:
        seen: set[str] = set()
        zone_ids = {zone.id for zone in iter_zones(db.zones)}
        for zone in iter_zones(db.zones):
            where = f"dashboards[{db.id}].zones[{zone.id}]"
            if zone.id in seen:
                errors.append(f"{where}：區塊 id 重複")
            seen.add(zone.id)
            button = zone.button
            if button is not None:
                if button.target_dashboard is not None and button.target_dashboard not in boards:
                    errors.append(f"{where}.button：dashboard「{button.target_dashboard}」不存在")
                if button.target_worksheet is not None and button.target_worksheet not in sheets:
                    errors.append(f"{where}.button：工作表「{button.target_worksheet}」不存在")
                for target in button.zones:
                    if target not in zone_ids:
                        errors.append(f"{where}.button：區塊「{target}」不在此 dashboard 上")
            if zone.parameter is not None and zone.parameter not in params:
                errors.append(f"{where}：參數「{zone.parameter}」不存在")
            if zone.worksheet is None:
                continue
            sheet = sheets.get(zone.worksheet)
            if sheet is None:
                errors.append(f"{where}：工作表「{zone.worksheet}」不存在")
            elif zone.field is not None:
                check_use(zone.field, sheet.datasource, f"{where}.field", None)

    for story in ir.stories:
        point_ids = [point.id for point in story.points]
        if len(set(point_ids)) != len(point_ids):
            errors.append(f"stories[{story.id}].points：故事點的 id 重複")
        if story.active is not None and story.active not in point_ids:
            errors.append(f"stories[{story.id}].active：故事點「{story.active}」不存在")
        for point in story.points:
            where = f"stories[{story.id}].points[{point.id}]"
            if point.dashboard is not None and point.dashboard not in boards:
                errors.append(f"{where}：dashboard「{point.dashboard}」不存在")
            if point.worksheet is not None and point.worksheet not in sheets:
                errors.append(f"{where}：工作表「{point.worksheet}」不存在")
            for state in point.actions:
                if state.action not in actions:
                    errors.append(f"{where}.actions：動作「{state.action}」不存在")
            for item in [*point.filters, *point.selections]:
                if item.worksheet not in sheets:
                    errors.append(f"{where}：工作表「{item.worksheet}」不存在")
            for flt in point.filters:
                sheet = sheets.get(flt.worksheet)
                if sheet is not None:
                    check_use(flt.field, sheet.datasource, f"{where}.filters", None)

    return errors
