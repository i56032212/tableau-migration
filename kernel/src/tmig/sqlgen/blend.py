"""Data Blending（資料混合）：主要資料源的查詢，與各次要資料源依連結欄位彙總的查詢，以 LEFT JOIN 合併。

與 Tableau 相同：

- 主要資料源照常查詢（篩選、排序、LOD 都以主要資料源計算），次要資料源的量值不在其中
- 次要資料源以作用中的連結欄位（主要資料源的連結欄位在檢視中，見 BlendLink.secondary_for）分組，
  彙總次要資料源的量值；次要資料源自己的資料源篩選照樣套用，工作表上主要資料源的篩選不套用到次要資料源
- 以連結欄位 LEFT JOIN：主要資料源沒有的值不出現，次要資料源沒有的值為 Null；檢視中有連結欄位以外的
  維度時，次要資料源的值在這些維度上重複（以連結欄位的層級計算）
- 連結欄位的 Null 視為相等（與本平台以維度連接的其他地方相同；Tableau 的行為未確認）

次要資料源的萃取在執行前以唯讀附加（WorksheetSql.attach），資料表名稱前加上附加的名稱。尚不支援：
Live 連線與 Relationship 模型的次要資料源、Live 連線的主要資料源、次要資料源的 Table Calculation，
以及與總計一起使用。
"""

from __future__ import annotations

from tmig.ir.models import (
    IR,
    MEASURE_NAMES,
    MEASURE_VALUES,
    Blend,
    ExtractConnection,
    FieldUse,
    FileConnection,
    JoinRelation,
    LiveConnection,
    Marks,
    ModelRelation,
    PivotRelation,
    ProjectRelation,
    Relation,
    TableRelation,
    UnionRelation,
    Worksheet,
)
from tmig.planner.identity import Identity
from tmig.planner.state import QueryState, use_key
from tmig.sqlgen.query import (
    OutputColumn,
    QueryError,
    StageNote,
    WorksheetQuery,
    WorksheetSql,
    _find_worksheet,
)
from tmig.sqlgen.sql import cast, ident, null_safe_equal

ROW = "__blend_row"
"""主要資料源查詢的列號：合併後依它排回主要資料源的顯示順序。"""
PRIMARY = "__p"
NUMERIC = frozenset({"integer", "real"})
TEMPORAL = frozenset({"date", "datetime"})


def blend_sql(
    ir: IR, worksheet: str, state: QueryState | None = None, user: Identity | None = None
) -> WorksheetSql:
    ws = _find_worksheet(ir, worksheet)
    datasources = {d.id: d for d in ir.datasources}
    owners: dict[str, Blend] = {}
    for blend in ws.blends:
        for data_field in datasources[blend.datasource].fields:
            owners[data_field.id] = blend
    primary_ws = _primary_worksheet(ws, owners)
    primary_ir = _with(ir, primary_ws)
    view = _view_uses(ws)
    used = _unique([u for u in view if u.field in owners])
    if not used:
        return WorksheetQuery(primary_ir, ws.id, state, user).build()
    for use in used:
        if use.table_calc is not None:
            raise QueryError(
                "Data Blending：次要資料源的欄位套用 Table Calculation 尚不支援", unsupported=True
            )
    if isinstance(datasources[ws.datasource].connection, LiveConnection):
        raise QueryError("Data Blending：主要資料源為 Live 連線尚不支援", unsupported=True)

    query = WorksheetQuery(primary_ir, ws.id, state, user)
    query.row_number = ROW
    main = query.build()
    dims = [c for c in main.columns if c.role == "dimension" and c.use is not None]
    ctes = [f"{ident(PRIMARY)} AS (\n{main.sql.rstrip()}\n)"]
    joins: list[str] = []
    attach: dict[str, str] = {}
    extra: list[tuple[str, OutputColumn]] = []
    notes: list[str] = []
    for n, blend in enumerate(ws.blends):
        uses = [u for u in used if owners[u.field] is blend]
        if not uses:
            continue
        secondary = datasources[blend.datasource]
        pairs: list[tuple[OutputColumn, FieldUse]] = []
        for column in dims:
            assert column.use is not None
            for link in blend.links:
                found = link.secondary_for(column.use)
                if found is not None and all(use_key(found) != use_key(f) for _, f in pairs):
                    pairs.append((column, found))
        if not pairs:
            raise QueryError(
                f"Data Blending：檢視中沒有與次要資料源「{secondary.caption}」的連結欄位",
                unsupported=True,
            )
        connection = secondary.connection
        alias = f"__blend{n}"
        if isinstance(connection, ExtractConnection) and connection.model is None:
            moved = connection.model_copy(update={"table": f"{alias}.{connection.table}"})
        elif isinstance(connection, FileConnection) and not isinstance(
            connection.relation, ModelRelation
        ):
            # 檔案型資料源：關聯樹中的資料表加上附加的名稱
            moved = connection.model_copy(
                update={"relation": _attached_relation(connection.relation, alias)}
            )
        else:
            raise QueryError(
                f"Data Blending：次要資料源「{secondary.caption}」為 Live 連線或 Relationship 模型，"
                "尚不支援",
                unsupported=True,
            )
        attached = secondary.model_copy(update={"connection": moved})
        holder = _secondary_worksheet(ws, secondary.id, [f for _, f in pairs], uses)
        sub = ir.model_copy(
            update={
                "datasources": [attached if d.id == secondary.id else d for d in ir.datasources],
                "worksheets": [holder],
            }
        )
        built = WorksheetQuery(sub, holder.id, state, user).build()
        name = f"__s{n}"
        ctes.append(f"{ident(name)} AS (\n{built.sql.rstrip()}\n)")
        types = {c.key: c.datatype for c in built.columns}
        conditions = [
            _link_condition(
                f"{ident(PRIMARY)}.{ident(column.key)}",
                column.datatype,
                f"{ident(name)}.{ident(use_key(found))}",
                types[use_key(found)],
            )
            for column, found in pairs
        ]
        joins.append(f"LEFT JOIN {ident(name)} ON {' AND '.join(conditions)}")
        attach[alias] = secondary.id
        for column in built.columns:
            if column.role == "measure":
                extra.append((name, column))
        notes.append(
            f"次要資料源「{secondary.caption}」以 {', '.join(use_key(f) for _, f in pairs)} 彙總後"
            "依連結欄位 LEFT JOIN"
        )

    shown = [c for c in main.columns if c.role != "sort"]
    sorts = [c for c in main.columns if c.role == "sort" and c.key != ROW]
    outputs = [f"{ident(PRIMARY)}.{ident(c.key)}" for c in shown]
    outputs += [f"{ident(name)}.{ident(c.key)}" for name, c in extra]
    outputs += [f"{ident(PRIMARY)}.{ident(c.key)}" for c in sorts]
    select = (
        f"SELECT {', '.join(outputs)}\nFROM {ident(PRIMARY)}\n"
        + "\n".join(joins)
        + f"\nORDER BY {ident(PRIMARY)}.{ident(ROW)}"
    )
    sql = (
        "WITH "
        + ",\n".join(ctes)
        + "\n-- Data Blending：依連結欄位合併次要資料源\n"
        + select
        + "\n"
    )
    plan = [*main.plan, StageNote(8, [], [f"Data Blending：{text}" for text in notes])]
    return WorksheetSql(
        worksheet=ws.id,
        sql=sql,
        columns=[*shown, *(c for _, c in extra), *sorts],
        plan=plan,
        state=main.state,
        main=select,
        guards=main.guards,
        attach=attach,
    )


def _attached_relation(relation: Relation, alias: str) -> Relation:
    """關聯樹中的資料表改成附加的 DuckDB 檔中的資料表（<alias>.Files.<name>）。"""
    if isinstance(relation, JoinRelation):
        return relation.model_copy(
            update={
                "left": _attached_relation(relation.left, alias),
                "right": _attached_relation(relation.right, alias),
            }
        )
    if isinstance(relation, TableRelation):
        return relation.model_copy(update={"table": [alias, *relation.table]})
    if isinstance(relation, PivotRelation):
        return relation.model_copy(update={"source": _attached_relation(relation.source, alias)})
    if isinstance(relation, ProjectRelation):
        return relation.model_copy(update={"source": _attached_relation(relation.source, alias)})
    if isinstance(relation, UnionRelation):
        parts = [
            part.model_copy(update={"relation": _attached_relation(part.relation, alias)})
            for part in relation.parts
        ]
        return relation.model_copy(update={"parts": parts})
    return relation


def _link_condition(left: str, left_type: str, right: str, right_type: str) -> str:
    """連結欄位相等（Null 視為相等）；型別不同且不能直接比較時都轉成字串比較。"""
    comparable = (
        left_type == right_type
        or {left_type, right_type} <= NUMERIC
        or {left_type, right_type} <= TEMPORAL
    )
    if not comparable:
        left, right = cast(left, "string"), cast(right, "string")
    return null_safe_equal(left, right)


def _view_uses(ws: Worksheet) -> list[FieldUse]:
    uses = [*ws.rows, *ws.columns, *ws.marks.uses()]
    uses += [u for pane in ws.panes for u in pane.marks.uses()]
    return uses + list(ws.measure_values)


def _unique(uses: list[FieldUse]) -> list[FieldUse]:
    seen: set[str] = set()
    result: list[FieldUse] = []
    for use in uses:
        key = use_key(use)
        if key not in seen and use.field not in (MEASURE_NAMES, MEASURE_VALUES):
            seen.add(key)
            result.append(use)
    return result


def _primary_worksheet(ws: Worksheet, owners: dict[str, Blend]) -> Worksheet:
    """去掉次要資料源欄位的工作表（主要資料源的查詢）。"""

    def keep(uses: list[FieldUse]) -> list[FieldUse]:
        return [u for u in uses if u.field not in owners]

    def card(marks: Marks) -> Marks:
        single = {
            name: None
            for name in ("color", "size", "shape", "angle", "path")
            if getattr(marks, name) is not None and getattr(marks, name).field in owners
        }
        return marks.model_copy(
            update={
                **single,
                "label": keep(marks.label),
                "detail": keep(marks.detail),
                "tooltip": keep(marks.tooltip),
            }
        )

    panes = [
        p.model_copy(update={"marks": card(p.marks)})
        for p in ws.panes
        if p.measure.field not in owners
    ]
    values = keep(ws.measure_values)
    return ws.model_copy(
        update={
            "rows": keep(ws.rows),
            "columns": keep(ws.columns),
            "marks": card(ws.marks),
            "panes": panes,
            "measure_values": values,
            "measure_aliases": [a for a in ws.measure_aliases if a.use in values],
            "blends": [],
        }
    )


def _secondary_worksheet(
    ws: Worksheet, datasource: str, links: list[FieldUse], measures: list[FieldUse]
) -> Worksheet:
    """次要資料源的查詢：連結欄位為維度、次要資料源的量值在標籤上；沒有工作表的篩選與排序。"""
    return ws.model_copy(
        update={
            "id": f"{ws.id}__blend",
            "datasource": datasource,
            "rows": links,
            "columns": [],
            "marks": Marks(type="text", label=measures),
            "panes": [],
            "measure_values": [],
            "measure_aliases": [],
            "filters": [],
            "sorts": [],
            "table_calcs": [],
            "reference_lines": [],
            "totals": [],
            "subtotals": [],
            "visual_totals": [],
            "title": [],
            "blends": [],
        }
    )


def _with(ir: IR, ws: Worksheet) -> IR:
    return ir.model_copy(update={"worksheets": [ws if w.id == ws.id else w for w in ir.worksheets]})


__all__ = ["blend_sql"]
