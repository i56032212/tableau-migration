"""Data Blending（資料混合）：主要資料源的查詢，與各次要資料源依連結欄位彙總的查詢，以 LEFT JOIN 合併。

與 Tableau 相同：

- 主要資料源照常查詢（篩選、排序、LOD 都以主要資料源計算），次要資料源的量值不在其中
- 次要資料源以作用中的連結欄位（主要資料源的連結欄位在檢視中，見 BlendLink.secondary_for）分組，
  彙總次要資料源的量值；次要資料源自己的資料源篩選照樣套用，工作表上主要資料源的篩選不套用到次要資料源
- 以連結欄位 LEFT JOIN：主要資料源沒有的值不出現，次要資料源沒有的值為 Null；檢視中有連結欄位以外的
  維度時，次要資料源的值在這些維度上重複（以連結欄位的層級計算）
- 連結欄位的 Null 視為相等（與本平台以維度連接的其他地方相同；Tableau 的行為未確認）

都在本機的資料源（萃取、檔案、跨資料庫的 join）在同一段 DuckDB 查詢中混合：次要資料源的萃取在執行前以唯讀
附加（WorksheetSql.attach），資料表名稱前加上附加的名稱；次要資料源是跨資料庫的 join（檔案＋資料庫、多個
資料庫）時，資料庫的資料表在查詢時取回（只取次要資料源的查詢用到的欄），與匯入的檔案一起成為附加的資料表
（WorksheetSql.federated，見 tmig.data.federated.attach_federated）。

主要或次要資料源是 Live 連線、或次要資料源是 Relationship 模型時，與 Tableau 相同各資料源分開查詢
（WorksheetSql.stages：Live 連線在各自的資料庫彙總，Relationship 模型依模型的規則），結果在本機依連結欄位
合併（tmig.data.executor.combine，由 tmig.data.runtime.DataRuntime 執行）。

主要資料源中引用次要資料源欄位的計算欄位（例如 SUM([Sales]) / SUM([Targets].[Target])）拆成各資料源各自彙總的
部分與合併後的運算（_CalcSplit）；次要資料源的維度以 ATTR 彙總（文字欄位對到多個值時為 *）。

尚不支援：次要資料源的篩選與 Table Calculation，以及與總計一起使用。
"""

from __future__ import annotations

from dataclasses import dataclass

from tmig.formula.analyze import Analyzer, Target
from tmig.formula.ast import Call, FieldRef, Lod, Node, map_children, walk
from tmig.formula.types import FormulaSemanticError
from tmig.formula.unparse import to_formula
from tmig.ir.models import (
    IR,
    MEASURE_NAMES,
    MEASURE_VALUES,
    Blend,
    Calc,
    DataField,
    Datasource,
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
    is_federated,
)
from tmig.ir.scope import DatasourceScope
from tmig.planner.identity import Identity
from tmig.planner.state import QueryState, use_key
from tmig.sqlgen.query import (
    BlendStage,
    FederatedBlend,
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
    primary = datasources[ws.datasource]
    view = _view_uses(ws)
    # 主要資料源中引用次要資料源欄位的計算欄位：拆成各資料源各自彙總的部分，合併後再計算
    split = _CalcSplit(ir, primary, owners, datasources)
    calcs = [u for u in _unique(view) if split.blended(u.field)]
    for use in calcs:
        if use.table_calc is not None or use.agg is not None or use.date is not None:
            raise QueryError(
                "Data Blending：引用次要資料源欄位的計算欄位不能再彙總或套用 Table Calculation",
                unsupported=True,
            )
        split.add(use)
    hidden = {u.field for u in calcs}
    primary_ws = _primary_worksheet(ws, {**owners, **dict.fromkeys(hidden, ws.blends[0])})
    primary_ws = split.primary_worksheet(primary_ws)
    if any(f'"{field_id}"' in primary_ws.model_dump_json() for field_id in hidden):
        raise QueryError(
            "Data Blending：引用次要資料源欄位的計算欄位只能放在欄列架與標記卡上"
            "（篩選、排序、參考線等尚不支援）",
            unsupported=True,
        )
    primary = split.primary
    primary_ir = _with(
        ir.model_copy(
            update={"datasources": [primary if d.id == primary.id else d for d in ir.datasources]}
        ),
        primary_ws,
    )
    used = _unique([u for u in view if u.field in owners])
    if not used and not calcs:
        return WorksheetQuery(primary_ir, ws.id, state, user).build()
    for use in used:
        if use.table_calc is not None:
            raise QueryError(
                "Data Blending：次要資料源的欄位套用 Table Calculation 尚不支援", unsupported=True
            )
    blends = [
        (n, blend, [u for u in used if owners[u.field] is blend], split.uses(blend.datasource))
        for n, blend in enumerate(ws.blends)
    ]
    blends = [(n, blend, uses, parts) for n, blend, uses, parts in blends if uses or parts]
    # 主要或次要資料源有 Live 連線（或次要資料源是 Relationship 模型）：各資料源分開查詢，結果在本機合併
    staged = isinstance(primary.connection, LiveConnection) or any(
        separate(datasources[blend.datasource]) for _, blend, _, _ in blends
    )

    query = WorksheetQuery(primary_ir, ws.id, state, user)
    query.row_number = ROW
    main = query.build()
    dims = [c for c in main.columns if c.role == "dimension" and c.use is not None]
    ctes = [f"{ident(PRIMARY)} AS (\n{main.sql.rstrip()}\n)"]
    stages = [BlendStage(PRIMARY, primary, main)] if staged else []
    joins: list[str] = []
    attach: dict[str, str] = {}
    federated: dict[str, FederatedBlend] = {}
    extra: list[tuple[str, OutputColumn]] = []
    notes: list[str] = []
    for n, blend, uses, parts in blends:
        secondary = split.secondary(blend.datasource)
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
        alias = f"__blend{n}"
        # 次要資料源的維度（ATTR）：連結欄位的一個值對到多個值時顯示 *（與 Tableau 相同；文字欄位）
        starred: dict[str, FieldUse] = {}
        by_id = {f.id: f for f in secondary.fields}
        for index, use in enumerate(uses):
            data_field = by_id[use.field]
            if use.agg != "attr" or use.date is not None or data_field.datatype != "string":
                continue
            own = f"[{_strip_brackets(data_field.name).replace(']', ']]')}]"
            star = DataField(
                id=f"blend_attr_{n}_{index}",
                name=f"[blend_attr_{n}_{index}]",
                caption=data_field.caption,
                role="measure",
                datatype="string",
                calc=Calc(
                    formula=f"IF MIN({own}) = MAX({own}) THEN MIN({own}) "
                    f"ELSEIF COUNT({own}) > 0 THEN '*' END",
                    level="aggregate",
                ),
                source_ref="/blend",
            )
            secondary = _with_field(secondary, star)
            uses[index] = FieldUse(field=star.id, continuous=False)
            starred[star.id] = use
        source = secondary if staged else _attached(secondary, alias)
        holder = _secondary_worksheet(ws, secondary.id, [f for _, f in pairs], [*uses, *parts])
        sub = ir.model_copy(
            update={
                "datasources": [source if d.id == secondary.id else d for d in ir.datasources],
                "worksheets": [holder],
            }
        )
        built = WorksheetQuery(sub, holder.id, state, user).build()
        name = f"__s{n}"
        connection = secondary.connection
        if staged:
            stages.append(BlendStage(name, secondary, built))
        else:
            if isinstance(connection, FileConnection) and is_federated(connection):
                # 跨資料庫的 join：資料庫的資料表在執行前取回，與匯入的檔案一起附加
                federated[alias] = FederatedBlend(
                    connection=connection, sql=built.sql, parameters=built.parameters
                )
            ctes.append(f"{ident(name)} AS (\n{built.sql.rstrip()}\n)")
            attach[alias] = secondary.id
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
        part_keys = {use_key(u) for u in parts}
        for column in built.columns:
            located = f"{ident(name)}.{ident(column.key)}"
            if column.key in part_keys:
                split.located(column.key, name)
            elif column.key in starred:
                original = starred[column.key]
                shown_as = OutputColumn(use_key(original), column.role, "string", original)
                extra.append((located, shown_as))
            elif column.role == "measure":
                extra.append((located, column))
        where = ""
        if staged:
            where = "在它的資料庫" if isinstance(connection, LiveConnection) else "另外查詢、"
        notes.append(
            f"次要資料源「{secondary.caption}」{where}以 {', '.join(use_key(f) for _, f in pairs)} "
            "彙總後依連結欄位 LEFT JOIN"
        )

    for key in split.primary_keys():
        split.located(key, PRIMARY)
    shown = [c for c in main.columns if c.role != "sort" and c.key not in split.primary_keys()]
    sorts = [c for c in main.columns if c.role == "sort" and c.key != ROW]
    computed = split.outputs(query)
    outputs = [f"{ident(PRIMARY)}.{ident(c.key)}" for c in shown]
    outputs += [f"{located} AS {ident(c.key)}" for located, c in extra]
    outputs += [f"{sql} AS {ident(c.key)}" for sql, c in computed]
    outputs += [f"{ident(PRIMARY)}.{ident(c.key)}" for c in sorts]
    if computed:
        notes.append(
            "引用次要資料源欄位的計算欄位："
            + "、".join(f"「{data_field.caption}」" for _, data_field, _ in split.calcs)
            + " 各資料源各自彙總後，在合併的結果上計算"
        )
    select = (
        f"SELECT {', '.join(outputs)}\nFROM {ident(PRIMARY)}\n"
        + "\n".join(joins)
        + f"\nORDER BY {ident(PRIMARY)}.{ident(ROW)}"
    )
    columns = [*shown, *(c for _, c in extra), *(c for _, c in computed), *sorts]
    if staged:
        notes.insert(
            0,
            "各資料源分開查詢（Live 連線在各自的資料庫），結果在本機依連結欄位合併（與 Tableau 相同）",
        )
        plan = [*main.plan, StageNote(8, [], [f"Data Blending：{text}" for text in notes])]
        text = "\n".join(
            f"-- {stage.name}：{stage.datasource.caption}\n{stage.query.sql.rstrip()}\n"
            for stage in stages
        )
        return WorksheetSql(
            worksheet=ws.id,
            sql=f"{text}-- Data Blending：依連結欄位合併各資料源的結果\n{select}\n",
            columns=columns,
            plan=plan,
            state=main.state,
            main=select,
            stages=stages,
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
        columns=columns,
        plan=plan,
        state=main.state,
        main=select,
        guards=main.guards,
        attach=attach,
        federated=federated,
    )


PART_PREFIX = "blend_part_"
"""引用次要資料源欄位的計算欄位拆出的部分（各資料源各自彙總的運算式）：暫時的計算欄位 id 的前綴。"""


@dataclass
class _Part:
    datasource: str
    field: DataField
    use: FieldUse
    source: str | None = None
    """合併時所在的查詢（__p 或 __s0…）。"""


class _CalcSplit:
    """主要資料源中引用次要資料源欄位的計算欄位（例如 SUM([Sales]) / SUM([Targets].[Target])）：與 Tableau 相同，
    彙總的部分在各自的資料源計算（主要資料源依檢視的層級，次要資料源依連結欄位的層級），合併後再算其餘的運算。

    公式拆成只用到一個資料源的彙總運算式（成為那個資料源暫時的計算欄位，隨它的查詢算出）與外面的運算
    （四則、IF、ZN 等，在合併的 SELECT 中以各部分的欄算出）。同一個彙總中同時用到兩個資料源的欄位不支援。"""

    def __init__(
        self,
        ir: IR,
        primary: Datasource,
        owners: dict[str, Blend],
        datasources: dict[str, Datasource],
    ) -> None:
        self.owners = owners
        self.datasources = dict(datasources)
        self.primary = primary
        self.own = {f.id: f for f in primary.fields}
        others = [datasources[b] for b in dict.fromkeys(o.datasource for o in owners.values())]
        self.scope = DatasourceScope.for_datasource(primary, ir.parameters, others)
        self.analyzer = Analyzer(self.scope)
        self.parts: dict[tuple[str, str], _Part] = {}
        self.calcs: list[tuple[FieldUse, DataField, Node]] = []
        self._blended: dict[str, bool] = {}

    # ------------------------------------------------------------ 判斷

    def blended(self, field_id: str) -> bool:
        """主要資料源的計算欄位（展開引用的計算欄位後）用到次要資料源的欄位。"""
        found = self._blended.get(field_id)
        if found is None:
            data_field = self.own.get(field_id)
            found = False
            if data_field is not None and data_field.calc is not None:
                try:
                    node = self.analyzer.calc_ast(self.scope.target(field_id))
                    found = any(side != self.primary.id for side in self._sides(node))
                except (FormulaSemanticError, KeyError):
                    found = False
            self._blended[field_id] = found
        return found

    def _sides(self, node: Node) -> set[str]:
        """運算式用到的資料源（id）；引用的計算欄位一併展開。"""
        found: set[str] = set()
        for item in walk(node):
            if not isinstance(item, FieldRef):
                continue
            target = self.analyzer.resolve(item)
            if target.kind == "param":
                continue
            if target.id in self.owners:
                found.add(self.owners[target.id].datasource)
            elif target.formula is not None:
                found |= self._sides(self.analyzer.calc_ast(target))
            else:
                found.add(self.primary.id)
        return found

    # ------------------------------------------------------------ 拆開

    def add(self, use: FieldUse) -> None:
        data_field = self.own[use.field]
        try:
            node = self._split(self.analyzer.calc_ast(self.scope.target(use.field)))
        except FormulaSemanticError as exc:
            raise QueryError(f"計算欄位「{data_field.caption}」：{exc.message}") from exc
        self.calcs.append((use, data_field, node))

    def _split(self, node: Node) -> Node:
        sides = self._sides(node)
        info = self.analyzer.analyze(node)
        if not sides or info.level == "const":
            return node
        if info.level == "tc":
            raise QueryError(
                "Data Blending：引用次要資料源欄位的計算欄位中的 Table Calculation 尚不支援",
                unsupported=True,
            )
        if len(sides) == 1:
            (side,) = sides
            if info.level == "agg":
                # 次要資料源只算彙總本身：外面的運算（例如 ZN）在合併後計算，次要資料源沒有對應的列時
                # ZN(SUM([Targets].[Target])) 是 0（與 Tableau 相同）
                whole = (
                    side == self.primary.id or isinstance(node, FieldRef) or self._aggregate(node)
                )
                return (
                    self._part(side, node, info.datatype)
                    if whole
                    else map_children(node, self._split)
                )
            if side != self.primary.id:
                raise QueryError(
                    "Data Blending：計算欄位中次要資料源的欄位要先彙總（例如 SUM、ATTR）"
                )
            return node
        if isinstance(node, FieldRef):
            # 引用的計算欄位本身同時用到兩個資料源：展開
            return self._split(self.analyzer.calc_ast(self.analyzer.resolve(node)))
        if isinstance(node, Lod):
            raise QueryError(
                "Data Blending：LOD 運算式中同時用到主要與次要資料源的欄位，尚不支援",
                unsupported=True,
            )
        if self._aggregate(node):
            assert isinstance(node, Call)
            raise QueryError(
                f"Data Blending：{node.name} 中同時用到主要與次要資料源的欄位"
                "（各資料源的欄位要各自彙總），尚不支援",
                unsupported=True,
            )
        return map_children(node, self._split)

    def _aggregate(self, node: Node) -> bool:
        """彙總函式的呼叫（參數不是彙總的運算式）。"""
        return isinstance(node, Call) and not any(
            self.analyzer.analyze(arg).level == "agg" for arg in node.args
        )

    def _part(self, side: str, node: Node, datatype: str) -> Node:
        if side != self.primary.id:
            node = self._localize(node)
        text = to_formula(node)
        part = self.parts.get((side, text))
        if part is None:
            field_id = f"{PART_PREFIX}{len(self.parts)}"
            data_field = DataField(
                id=field_id,
                name=f"[{field_id}]",
                caption=field_id,
                role="measure",
                datatype=datatype,  # type: ignore[arg-type]
                calc=Calc(formula=text, level="aggregate"),
                source_ref="/blend",
            )
            part = _Part(side, data_field, FieldUse(field=field_id, continuous=True))
            self.parts[(side, text)] = part
            if side == self.primary.id:
                self.primary = _with_field(self.primary, data_field)
            else:
                self.datasources[side] = _with_field(self.datasources[side], data_field)
        return FieldRef(node.pos, (part.field.id,))

    def _localize(self, node: Node) -> Node:
        """次要資料源的運算式：[資料源].[欄位] 改成它自己的欄位名稱（在次要資料源中計算）。"""
        if isinstance(node, FieldRef):
            target = self.analyzer.resolve(node)
            if target.id in self.owners:
                secondary = self.datasources[self.owners[target.id].datasource]
                name = next(f.name for f in secondary.fields if f.id == target.id)
                return FieldRef(node.pos, (_strip_brackets(name),))
            return node
        return map_children(node, self._localize)

    # ------------------------------------------------------------ 各資料源的查詢

    def secondary(self, datasource: str) -> Datasource:
        return self.datasources[datasource]

    def uses(self, datasource: str) -> list[FieldUse]:
        return [p.use for p in self.parts.values() if p.datasource == datasource]

    def primary_keys(self) -> set[str]:
        return {use_key(u) for u in self.uses(self.primary.id)}

    def primary_worksheet(self, ws: Worksheet) -> Worksheet:
        """主要資料源的查詢另外算出它的各部分（放在工具提示上：不影響檢視的層級）。"""
        parts = self.uses(self.primary.id)
        if not parts:
            return ws
        marks = ws.marks.model_copy(update={"tooltip": [*ws.marks.tooltip, *parts]})
        return ws.model_copy(update={"marks": marks})

    def located(self, key: str, source: str) -> None:
        for part in self.parts.values():
            if use_key(part.use) == key:
                part.source = source

    # ------------------------------------------------------------ 合併後的運算

    def outputs(self, query: WorksheetQuery) -> list[tuple[str, OutputColumn]]:
        """各計算欄位在合併的 SELECT 中的 SQL 與輸出欄。"""
        from tmig.sqlgen.expr import ExprCompiler

        if not self.calcs:
            return []
        missing = [
            p.field.calc.formula for p in self.parts.values() if p.source is None and p.field.calc
        ]
        if missing:
            raise QueryError(
                f"Data Blending：計算欄位的部分 {missing[0]} 沒有算出", unsupported=True
            )
        compiler = ExprCompiler(Analyzer(_MergeScope(self)), _MergeEnv(self, query))  # type: ignore[arg-type]
        found: list[tuple[str, OutputColumn]] = []
        for use, data_field, node in self.calcs:
            try:
                sql = compiler.row(node)
                datatype = compiler.analyzer.analyze(node).datatype
            except FormulaSemanticError as exc:
                raise QueryError(f"計算欄位「{data_field.caption}」：{exc.message}") from exc
            column = OutputColumn(
                key=use_key(use),
                role="measure",
                datatype="real" if datatype == "null" else datatype,
                use=use,
            )
            found.append((sql, column))
        return found


class _MergeScope:
    """合併後的運算：各部分是合併結果中的欄，其餘（參數）依主要資料源。"""

    def __init__(self, split: _CalcSplit) -> None:
        self.split = split
        self.parts = {p.field.id: p for p in split.parts.values()}

    def lookup(self, parts: tuple[str, ...]) -> Target | None:
        if len(parts) == 1 and parts[0] in self.parts:
            data_field = self.parts[parts[0]].field
            return Target("field", data_field.id, data_field.datatype, "measure")
        found = self.split.scope.lookup(parts)
        return found if found is not None and found.kind == "param" else None


class _MergeEnv:
    def __init__(self, split: _CalcSplit, query: WorksheetQuery) -> None:
        self.parts = {p.field.id: p for p in split.parts.values()}
        self.query = query
        self.week_start = query.week_start

    def column(self, target: Target) -> str:
        part = self.parts[target.id]
        assert part.source is not None
        return f"{ident(part.source)}.{ident(use_key(part.use))}"

    def param(self, target: Target) -> str:
        return self.query.param_sql(target.id)

    def param_value(self, target: Target) -> object:
        return self.query.param_value(target.id)

    def lod(self, node: object) -> str:
        raise QueryError("Data Blending：合併後不能計算 LOD 運算式")

    def aggregate(self, node: object) -> str:
        raise QueryError("Data Blending：合併後不能再彙總")

    def table_calc(self, node: object) -> str:
        raise QueryError("Data Blending：合併後的 Table Calculation 尚不支援", unsupported=True)


def _with_field(datasource: Datasource, data_field: DataField) -> Datasource:
    return datasource.model_copy(update={"fields": [*datasource.fields, data_field]})


def _strip_brackets(name: str) -> str:
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


def separate(datasource: Datasource) -> bool:
    """次要資料源要分開查詢：Live 連線（在它的資料庫彙總）與 Relationship 模型（依模型的規則各自查詢）。"""
    connection = datasource.connection
    if isinstance(connection, LiveConnection):
        return True
    if isinstance(connection, ExtractConnection):
        return connection.model is not None
    return isinstance(connection.relation, ModelRelation)


def _attached(secondary: Datasource, alias: str) -> Datasource:
    """次要資料源的資料表改成附加的 DuckDB 檔中的資料表（與主要資料源在同一段查詢中）。"""
    connection = secondary.connection
    if isinstance(connection, ExtractConnection) and connection.model is None:
        moved: ExtractConnection | FileConnection = connection.model_copy(
            update={"table": f"{alias}.{connection.table}"}
        )
    elif (
        isinstance(connection, FileConnection)
        and connection.relation is not None
        and not isinstance(connection.relation, ModelRelation)
    ):
        # 檔案型資料源（含跨資料庫的 join）：關聯樹中的資料表加上附加的名稱
        moved = connection.model_copy(
            update={"relation": _attached_relation(connection.relation, alias)}
        )
    else:  # pragma: no cover - separate() 的資料源分開查詢
        raise QueryError(
            f"Data Blending：次要資料源「{secondary.caption}」不能與主要資料源一起查詢",
            unsupported=True,
        )
    return secondary.model_copy(update={"connection": moved})


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


__all__ = ["blend_sql", "separate"]
