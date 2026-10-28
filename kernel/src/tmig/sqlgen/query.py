"""工作表查詢：依 Tableau 的運算順序產生 DuckDB SQL（D1–D4）。

運算順序與 CTE 的對應：

1. 萃取篩選與資料源篩選  ``src``（萃取篩選在建立萃取時已套用，不重複）
2. 內容篩選  ``ctx_pre`` → ``ctx``（含內容 Top N）
3. FIXED LOD、Top N  ``lod_*``、``ctx_ext``、``topn_*``
4. 維度篩選  ``dimf``
5. INCLUDE／EXCLUDE LOD  ``lod_*``、``dimf_ext``
6. 彙總、量值篩選  ``agg_*``、``meas``、``mf``
7. Table Calculation 與合計  ``tc``
8. Table Calculation 篩選  最後的 SELECT

1–6 段可以下推到來源資料庫；7、8 段一律在 DuckDB 執行。

LOD 的彙總語意：彙總的參數若只含 LOD（不含列層級欄位），在「檢視維度 ∪ LOD 維度」的組合上
去重後再彙總（例如 AVG({INCLUDE [Order ID] : SUM([Profit])}) 是各訂單的平均）；
若同時含列層級欄位，LOD 值併到每一列後逐列彙總。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from tmig.formula.analyze import Analyzer, Target
from tmig.formula.ast import (
    Binary,
    Call,
    FieldRef,
    If,
    LiteralNode,
    Lod,
    Node,
    children,
    strip_positions,
)
from tmig.formula.functions import lookup
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.ir.models import (
    IR,
    SPECIAL_FIELDS,
    CategoricalFilter,
    ConditionFilter,
    DataField,
    FieldUse,
    Filter,
    Marks,
    ParamRef,
    RangeFilter,
    RelativeDateFilter,
    Scalar,
    Sort,
    TableCalc,
    TopNFilter,
    TupleFilter,
    Worksheet,
)
from tmig.ir.scope import DatasourceScope
from tmig.planner.state import EffectiveState, QueryState, apply_state, use_key
from tmig.sqlgen.expr import ExprCompiler, interval, week_offset
from tmig.sqlgen.sql import ident, literal

AGG_FUNCTIONS = {
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
    "countd": "COUNTD",
    "median": "MEDIAN",
    "attr": "ATTR",
    "stdev": "STDEV",
    "stdevp": "STDEVP",
    "var": "VAR",
    "varp": "VARP",
}
RANK_FUNCTIONS = {
    "competition": "RANK",
    "dense": "RANK_DENSE",
    "modified": "RANK_MODIFIED",
    "unique": "RANK_UNIQUE",
}
WINDOW_AGG = {
    "SUM": "sum",
    "AVG": "avg",
    "MIN": "min",
    "MAX": "max",
    "COUNT": "count",
    "MEDIAN": "median",
    "STDEV": "stddev_samp",
    "STDEVP": "stddev_pop",
    "VAR": "var_samp",
    "VARP": "var_pop",
    "CORR": "corr",
    "COVAR": "covar_samp",
    "COVARP": "covar_pop",
}
STAGES = {
    1: "萃取篩選與資料源篩選",
    2: "內容篩選",
    3: "FIXED LOD、Top N",
    4: "維度篩選",
    5: "INCLUDE／EXCLUDE LOD",
    6: "彙總與量值篩選",
    7: "Table Calculation 與合計",
    8: "Table Calculation 篩選",
}
VALUE_COL = "__v"
PUSHDOWN_BOUNDARY = 6
"""這一段以前可以交給來源資料庫；之後一律在 DuckDB 執行。"""


class QueryError(ValueError):
    """工作表無法產生查詢。unsupported 為 True 表示 Tableau 允許、但本平台尚未支援。"""

    def __init__(self, message: str, *, unsupported: bool = False) -> None:
        super().__init__(message)
        self.unsupported = unsupported


@dataclass
class Dim:
    key: str
    use: FieldUse
    col: str


@dataclass
class Measure:
    key: str
    use: FieldUse
    node: Node
    table_calc: TableCalc | None
    output: bool
    col: str
    is_tc: bool


@dataclass
class LodSpec:
    node: Lod
    col: str


@dataclass
class OutputColumn:
    key: str
    role: Literal["dimension", "measure", "sort"]
    datatype: str
    use: FieldUse | None


@dataclass
class StageNote:
    stage: int
    ctes: list[str]
    detail: list[str]


@dataclass
class WorksheetSql:
    worksheet: str
    sql: str
    columns: list[OutputColumn]
    plan: list[StageNote]
    state: EffectiveState


@dataclass
class _Window:
    partition: list[str]
    order: list[str]


def _strip(name: str) -> str:
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


def _unique[T](items: list[T]) -> list[T]:
    seen: list[T] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


class _RowEnv:
    """列層級的編譯環境：欄位為實體欄位，LOD 為已併入的欄位。"""

    def __init__(self, query: WorksheetQuery, alias: str | None = None) -> None:
        self.query = query
        self.alias = alias
        self.week_start = query.week_start

    def _qualify(self, name: str) -> str:
        return f"{ident(self.alias)}.{ident(name)}" if self.alias else ident(name)

    def column(self, target: Target) -> str:
        data_field = self.query.fields[target.id]
        if data_field.column is None:
            raise QueryError(f"欄位 {data_field.caption} 沒有實體欄位")
        return self._qualify(data_field.column)

    def param(self, target: Target) -> str:
        return self.query.param_sql(target.id)

    def lod(self, node: Lod) -> str:
        spec = self.query.lods.get(node)
        if spec is None or node not in self.query.attached:
            raise QueryError(
                "LOD 運算式在這個運算階段還不能使用（例如以 INCLUDE／EXCLUDE 作維度篩選）",
                unsupported=True,
            )
        return self._qualify(spec.col)

    def aggregate(self, node: Call) -> str:
        raise QueryError(f"{node.name} 不能出現在列層級的位置")

    def table_calc(self, node: Call) -> str:
        raise QueryError(f"{node.name} 不能出現在列層級的位置")


class _PostEnv:
    """彙總之後的編譯環境：彙總節點換成欄位；Table Calculation 換成視窗函式。"""

    def __init__(
        self,
        query: WorksheetQuery,
        agg_cols: dict[Call, str],
        window: _Window | None = None,
        totals: dict[Call, str] | None = None,
    ) -> None:
        self.query = query
        self.agg_cols = agg_cols
        self.window = window
        self.totals = totals or {}
        self.week_start = query.week_start

    def column(self, target: Target) -> str:
        raise QueryError(f"列層級欄位 {target.id} 必須彙總")

    def param(self, target: Target) -> str:
        return self.query.param_sql(target.id)

    def lod(self, node: Lod) -> str:
        raise QueryError("LOD 運算式必須彙總")

    def aggregate(self, node: Call) -> str:
        col = self.agg_cols.get(self.query.canon(node))  # type: ignore[arg-type]
        if col is None:
            raise QueryError(f"內部錯誤：彙總 {node.name} 沒有對應的欄位")
        return ident(col)

    def table_calc(self, node: Call) -> str:
        if self.window is None:
            raise QueryError("尚不支援巢狀 Table Calculation", unsupported=True)
        return self.query.window_sql(node, self)


class WorksheetQuery:
    """一張工作表在某個篩選狀態下的查詢。"""

    def __init__(self, ir: IR, worksheet: str, state: QueryState | None = None) -> None:
        self.ir = ir
        self.worksheet = _find_worksheet(ir, worksheet)
        self.datasource = next(ds for ds in ir.datasources if ds.id == self.worksheet.datasource)
        self.state = apply_state(ir, self.worksheet, state or QueryState())
        self.scope = DatasourceScope(self.datasource.name, self.datasource.fields, ir.parameters)
        self.analyzer = Analyzer(self.scope)
        self.fields: dict[str, DataField] = {f.id: f for f in self.datasource.fields}
        self.params = {p.id: p for p in ir.parameters}
        self.week_start = self.datasource.settings.week_start
        self.ctes: list[tuple[str, str]] = []
        self.plan: dict[int, StageNote] = {n: StageNote(n, [], []) for n in STAGES}
        self.lods: dict[Lod, LodSpec] = {}
        self.cond_nodes: dict[str, Node] = {}
        """條件篩選 id → 條件的語法樹。"""
        self.attached: set[Lod] = set()
        self.row = ExprCompiler(self.analyzer, _RowEnv(self))
        self._names: dict[str, int] = {}
        self._canon: dict[Node, Node] = {}
        self._sort_ctes: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------ 工具

    def canon(self, node: Node) -> Node:
        """去掉位置資訊的語法樹，讓不同公式中相同的彙總只算一次。"""
        found = self._canon.get(node)
        if found is None:
            found = strip_positions(node)
            self._canon[node] = found
        return found

    def name(self, prefix: str) -> str:
        n = self._names.get(prefix, 0)
        self._names[prefix] = n + 1
        return f"{prefix}_{n}" if n or prefix.startswith("_") else prefix

    def cte(self, stage: int, name: str, body: str) -> str:
        self.ctes.append((name, body))
        self.plan[stage].ctes.append(name)
        return name

    def note(self, stage: int, text: str) -> None:
        self.plan[stage].detail.append(text)

    def param_sql(self, param_id: str) -> str:
        param = self.params[param_id]
        return literal(self.state.parameters.get(param_id, param.value), param.datatype)

    def compiler(self, env: object) -> ExprCompiler:
        return ExprCompiler(self.analyzer, env)  # type: ignore[arg-type]

    def field_node(self, field_id: str) -> FieldRef:
        return FieldRef(0, (_strip(self.fields[field_id].name),))

    def level(self, field_id: str) -> str:
        try:
            return self.analyzer.target_info(self.scope.target(field_id)).level
        except FormulaSemanticError as exc:
            raise QueryError(
                f"計算欄位 {self.fields[field_id].caption}：{exc.message}",
                unsupported=exc.unsupported,
            ) from exc

    def is_measure(self, use: FieldUse) -> bool:
        return (
            use.agg is not None
            or use.table_calc is not None
            or self.level(use.field) in ("agg", "tc")
        )

    def datatype(self, node: Node) -> str:
        try:
            return self.analyzer.analyze(node).datatype
        except FormulaSemanticError as exc:
            raise QueryError(exc.message, unsupported=exc.unsupported) from exc

    def dim_sql(self, use: FieldUse, compiler: ExprCompiler | None = None) -> str:
        comp = compiler or self.row
        node = self.field_node(use.field)
        base = self._compile(lambda: comp.row(node))
        if use.date is None:
            return base
        ws = week_offset(self.week_start)
        if use.date.kind == "part":
            return comp.date_part(use.date.unit, base, ws)
        return comp.date_trunc(use.date.unit, base, self.datatype(node), ws)

    def dim_datatype(self, use: FieldUse) -> str:
        if use.date is not None:
            if use.date.kind == "part":
                return "integer"
            return self.datatype(self.field_node(use.field))
        return self.datatype(self.field_node(use.field))

    def measure_node(self, use: FieldUse) -> Node:
        ref = self.field_node(use.field)
        if use.agg is not None:
            if self.level(use.field) in ("agg", "tc"):
                raise QueryError(
                    f"{self.fields[use.field].caption} 已經是彙總，不能再套用 {use.agg}"
                )
            return Call(0, AGG_FUNCTIONS[use.agg], (ref,))
        return ref

    def _compile(self, fn: Callable[[], str]) -> str:
        try:
            return fn()
        except FormulaSemanticError as exc:
            raise QueryError(exc.message, unsupported=exc.unsupported) from exc

    # ------------------------------------------------------------ 語法樹走訪

    def expand(self, node: Node) -> Iterator[Node]:
        """前序走訪；計算欄位會走進公式，但不走進 LOD 內部。"""
        yield node
        if isinstance(node, FieldRef):
            target = self._resolve(node)
            if target.kind == "field" and target.formula is not None:
                yield from self.expand(self.analyzer.calc_ast(target))
            return
        if isinstance(node, Lod):
            return
        for item in children(node):
            yield from self.expand(item)

    def _resolve(self, node: FieldRef) -> Target:
        try:
            return self.analyzer.resolve(node)
        except FormulaSemanticError as exc:
            raise QueryError(exc.message) from exc

    def aggregates_in(self, node: Node) -> list[Call]:
        found: list[Call] = []

        def visit(item: Node) -> None:
            if isinstance(item, Call):
                spec = lookup(item.name, len(item.args))
                if spec is not None and spec.kind == "aggregate":
                    found.append(item)
                    return
            if isinstance(item, FieldRef):
                target = self._resolve(item)
                if target.kind == "field" and target.formula is not None:
                    visit(self.analyzer.calc_ast(target))
                return
            if isinstance(item, Lod):
                return
            for sub in children(item):
                visit(sub)

        visit(node)
        unique: dict[Node, Call] = {}
        for item in found:
            unique.setdefault(self.canon(item), item)
        return list(unique.values())

    def table_calcs_in(self, node: Node) -> list[Call]:
        found: list[Call] = []
        for item in self.expand(node):
            if isinstance(item, Call):
                spec = lookup(item.name, len(item.args))
                if spec is not None and spec.kind == "table_calc":
                    found.append(item)
        return found

    def lods_in(self, node: Node) -> list[Lod]:
        return _unique([n for n in self.expand(node) if isinstance(n, Lod)])

    def has_row_fields(self, node: Node) -> bool:
        for item in self.expand(node):
            if isinstance(item, FieldRef):
                target = self._resolve(item)
                if target.kind == "field" and target.formula is None:
                    return True
        return False

    def register_lods(self, node: Node) -> None:
        for lod in self.lods_in(node):
            if lod in self.lods:
                continue
            for dim in lod.dims:
                if self.lods_in(dim):
                    raise QueryError("LOD 的維度本身含 LOD，尚不支援", unsupported=True)
            self.lods[lod] = LodSpec(lod, f"__lod_{len(self.lods)}")

    # ------------------------------------------------------------ 主流程

    def build(self) -> WorksheetSql:
        ws = self.worksheet
        dims, measures = self._view_fields()
        filters = self._filters()
        stage = {
            s: [f for f in filters if f.stage == s]
            for s in (
                "extract",
                "datasource",
                "context",
                "top_n",
                "dimension",
                "measure",
                "table_calc",
            )
        }

        # 篩選中的量值也要算，但不輸出
        value_filters = [
            f for f in stage["measure"] + stage["table_calc"] if not isinstance(f, TupleFilter)
        ]
        for flt in value_filters:
            key = use_key(flt.field)
            if not any(m.key == key for m in measures):
                measures.append(self._measure(flt.field, key, output=False, index=len(measures)))
        sorts = {s.field: s for s in ws.sorts}
        for m in measures:
            self._check_measure(m)
        for node in [self.field_node(d.use.field) for d in dims] + [m.node for m in measures]:
            self.register_lods(node)
        for flt in stage["datasource"] + stage["context"] + stage["dimension"]:
            for use in flt.fields if isinstance(flt, TupleFilter) else [flt.field]:
                self.register_lods(self.field_node(use.field))
        top_nodes = {
            flt.id: self._top_by(flt)
            for flt in stage["top_n"] + stage["context"]
            if isinstance(flt, TopNFilter)
        }
        for node in top_nodes.values():
            self.register_lods(node)
        # 條件篩選：列層級的條件逐列判斷；彙總條件與 Top N 一樣先算出符合的成員
        for flt in filters:
            if isinstance(flt, ConditionFilter):
                self.cond_nodes[flt.id] = self._condition_node(flt)
                self.register_lods(self.cond_nodes[flt.id])
        agg_conds = {
            fid
            for fid, node in self.cond_nodes.items()
            if self.analyzer.analyze(node).level == "agg"
        }

        def member_set(flt: Filter) -> bool:
            return isinstance(flt, TopNFilter) or flt.id in agg_conds

        for sort in sorts.values():
            if sort.by is not None:
                self.register_lods(self.measure_node(sort.by))

        fixed = [s for s in self.lods.values() if s.node.kind == "fixed"]
        relative = [s for s in self.lods.values() if s.node.kind != "fixed"]

        # 1. 資料源篩選
        for flt in stage["extract"]:
            self.note(1, f"萃取篩選 {flt.id} 已在建立萃取時套用，不重複")
        table = ".".join(ident(p) for p in self.datasource.connection.table.split("."))
        conds = [self._row_condition(f) for f in stage["datasource"]]
        self.cte(1, "src", f"SELECT * FROM {table}{_where(conds)}")
        for flt in stage["datasource"]:
            self.note(1, f"資料源篩選 {flt.id}")

        # 2. 內容篩選（內容 Top N 與彙總條件以其他內容篩選後的資料計算）
        ctx_dims = [f for f in stage["context"] if not member_set(f)]
        ctx_top = [f for f in stage["context"] if member_set(f)]
        source = "src"
        if ctx_dims or ctx_top:
            conds = [self._row_condition(f) for f in ctx_dims]
            source = self.cte(2, "ctx_pre", f"SELECT * FROM src{_where(conds)}")
            top_conds = [self._member_set(f, top_nodes, source, 2) for f in ctx_top]
            source = self.cte(2, "ctx", f"SELECT * FROM {source}{_where(top_conds)}")
            for flt in stage["context"]:
                self.note(2, f"內容篩選 {flt.id}")

        # 3. FIXED LOD、Top N 與彙總條件（以內容篩選後、其他維度篩選前的資料計算）
        source = self._attach(fixed, source, "ctx_ext", 3, view_dims=None)
        top_conds = [
            self._member_set(f, top_nodes, source, 3)
            for f in stage["top_n"] + stage["dimension"]
            if member_set(f)
        ]

        # 4. 維度篩選（「全部」的篩選沒有條件，不另建 CTE）
        dimension = [f for f in stage["dimension"] if not member_set(f)]
        conds = [c for c in [self._row_condition(f) for f in dimension] + top_conds if c]
        if conds:
            source = self.cte(4, "dimf", f"SELECT * FROM {source}{_where(conds)}")
            for flt in stage["dimension"]:
                self.note(4, f"維度篩選 {flt.id}")

        # 5. INCLUDE／EXCLUDE LOD
        source = self._attach(relative, source, "dimf_ext", 5, view_dims=dims)
        rows_cte = source

        # 6. 彙總與量值篩選
        level = [self.dim_sql(d.use) for d in dims]
        agg_nodes = list(
            {self.canon(a): a for m in measures for a in self.aggregates_in(m.node)}.values()
        )
        agg_name, agg_cols, keys = self.aggregate("agg", agg_nodes, level, rows_cte, 6, dims)
        for d, key in zip(dims, keys, strict=True):
            d.col = key
        post = self.compiler(_PostEnv(self, agg_cols))
        plain = [m for m in measures if not m.is_tc]
        selects = ["*"] + [
            f"{self._compile(lambda m=m: post.post(m.node))} AS {ident(m.col)}" for m in plain
        ]
        meas = self.cte(6, "meas", f"SELECT {', '.join(selects)} FROM {agg_name}")
        by_key = {m.key: m for m in measures}
        conds = [
            c
            for c in (
                self._value_condition(f, by_key[use_key(f.field)].col)
                for f in value_filters
                if f.stage == "measure"
            )
            if c
        ]
        marks = meas
        if conds:
            marks = self.cte(6, "mf", f"SELECT * FROM {meas}{_where(conds)}")
            for flt in stage["measure"]:
                self.note(6, f"量值篩選 {flt.id}")

        # 7. Table Calculation 與合計
        sort_cols = self._sort_keys(dims, sorts, rows_cte)
        final = self._table_calcs(dims, measures, marks, rows_cte, agg_cols, sort_cols)

        # 8. Table Calculation 篩選與輸出
        conds = [
            self._value_condition(f, by_key[use_key(f.field)].col)
            for f in value_filters
            if f.stage == "table_calc"
        ]
        for flt in stage["table_calc"]:
            self.note(8, f"Table Calculation 篩選 {flt.id}")
        outputs: list[str] = []
        columns: list[OutputColumn] = []
        for d in dims:
            outputs.append(f"{ident(d.col)} AS {ident(d.key)}")
            columns.append(OutputColumn(d.key, "dimension", self.dim_datatype(d.use), d.use))
        for m in measures:
            if m.output:
                outputs.append(f"{ident(m.col)} AS {ident(m.key)}")
                columns.append(OutputColumn(m.key, "measure", self.datatype(m.node), m.use))
        for d in dims:
            if d.key in sort_cols:
                name = f"__sort({d.key})"
                outputs.append(f"{ident(sort_cols[d.key][0])} AS {ident(name)}")
                columns.append(OutputColumn(name, "sort", "real", None))
        order = []
        for d in dims:
            if d.key in sort_cols:
                order.append(sort_cols[d.key][1])
            order.append(self._natural_order(d, sorts.get(d.use.field)))
        order_sql = f"\nORDER BY {', '.join(order)}" if order else ""
        main = f"SELECT {', '.join(outputs)}\nFROM {final}{_where(conds)}{order_sql}"
        return WorksheetSql(
            worksheet=ws.id,
            sql=self._render(main),
            columns=columns,
            plan=[self.plan[n] for n in STAGES if self.plan[n].ctes or self.plan[n].detail],
            state=self.state,
        )

    # ------------------------------------------------------------ 檢視欄位

    def _view_fields(self) -> tuple[list[Dim], list[Measure]]:
        """檢視中的維度與量值：欄列架、各標記卡、Measure Values 卡（Measure Names／Values 本身不查詢）。"""
        ws = self.worksheet
        uses: list[FieldUse] = [*ws.rows, *ws.columns, *ws.marks.uses()]
        for pane in ws.panes:
            uses += pane.marks.uses()
        uses += ws.measure_values
        dims: list[Dim] = []
        measures: list[Measure] = []
        seen: set[str] = set()
        for use in uses:
            key = use_key(use)
            if key in seen or use.field in SPECIAL_FIELDS:
                continue
            seen.add(key)
            if self.is_measure(use):
                measures.append(self._measure(use, key, output=True, index=len(measures)))
            else:
                dims.append(Dim(key=key, use=use, col=""))
        return dims, measures

    def _measure(self, use: FieldUse, key: str, *, output: bool, index: int) -> Measure:
        base = self.measure_node(use)
        tc = None
        node = base
        if use.table_calc is not None:
            tc = next((t for t in self.worksheet.table_calcs if t.id == use.table_calc), None)
            if tc is None:
                raise QueryError(f"Table Calculation {use.table_calc} 不存在")
            node = self._quick_calc(tc, base)
        is_tc = bool(self.table_calcs_in(node))
        if is_tc and tc is None:
            raise QueryError(f"{key} 含 Table Calculation 函式，卻沒有計算方向設定")
        return Measure(
            key=key,
            use=use,
            node=node,
            table_calc=tc,
            output=output,
            col=f"__m{index}",
            is_tc=is_tc,
        )

    def _quick_calc(self, tc: TableCalc, base: Node) -> Node:
        if tc.type == "formula":
            return base
        if tc.type == "running_total":
            agg = str(tc.options.get("aggregation", "sum")).upper()
            return Call(0, f"RUNNING_{agg}", (base,))
        if tc.type == "percent_of_total":
            return Binary(0, "/", base, Call(0, "TOTAL", (base,)))
        if tc.type == "rank":
            name = RANK_FUNCTIONS[str(tc.options.get("rank_type", "competition"))]
            direction = str(tc.options.get("direction", "desc"))
            return Call(0, name, (base, LiteralNode(0, "string", direction)))
        if tc.type in ("difference", "percent_difference"):
            # 與 Tableau 轉成計算欄位時相同：ZN(x) - LOOKUP(ZN(x), -1)，百分比再除以 ABS(LOOKUP(...))
            offset = tc.options.get("offset", -1)
            assert isinstance(offset, int)
            zn = Call(0, "ZN", (base,))
            other = Call(0, "LOOKUP", (zn, LiteralNode(0, "integer", offset)))
            diff = Binary(0, "-", zn, other)
            if tc.type == "difference":
                return diff
            return Binary(0, "/", diff, Call(0, "ABS", (other,)))
        if tc.type == "moving":
            # 與 Tableau 轉成計算欄位時相同：WINDOW_AVG(x, -2, 0)；視窗超出分割範圍時取現有的標記，
            # 設定「不足時為 Null」則為 Null
            agg = str(tc.options.get("aggregation", "avg")).upper()
            start, end = tc.options.get("start", -2), tc.options.get("end", 0)
            assert isinstance(start, int) and isinstance(end, int)
            low, high = LiteralNode(0, "integer", start), LiteralNode(0, "integer", end)
            window = Call(0, f"WINDOW_{agg}", (base, low, high))
            if not tc.options.get("null_if_incomplete"):
                return window
            complete = Binary(
                0,
                "AND",
                Binary(0, "<=", Call(0, "FIRST", ()), low),
                Binary(0, ">=", Call(0, "LAST", ()), high),
            )
            return If(0, ((complete, window),), None)
        raise QueryError(f"快速 Table Calculation 類型 {tc.type} 尚不支援", unsupported=True)

    def _check_measure(self, m: Measure) -> None:
        try:
            info = self.analyzer.analyze(m.node)
        except FormulaSemanticError as exc:
            raise QueryError(f"{m.key}：{exc.message}", unsupported=exc.unsupported) from exc
        if info.level == "row":
            raise QueryError(f"{m.key} 是列層級的值，作量值時必須彙總")

    # ------------------------------------------------------------ 篩選

    def _filters(self) -> list[Filter]:
        return [*self.datasource.filters, *self.state.filters]

    def _row_condition(self, flt: Filter) -> str:
        """維度上的篩選條件，在列層級計算。"""
        if isinstance(flt, TupleFilter):
            return self._tuple_condition(flt)
        if self.is_measure(flt.field):
            raise QueryError(f"篩選 {flt.id} 位於維度階段，欄位卻是量值")
        value = self.dim_sql(flt.field)
        datatype = self.dim_datatype(flt.field)
        if isinstance(flt, CategoricalFilter):
            return _categorical(value, flt, datatype)
        if isinstance(flt, RangeFilter):
            return _range(value, flt, datatype)
        if isinstance(flt, RelativeDateFilter):
            return self._relative(flt, value)
        if isinstance(flt, ConditionFilter):
            node = self.cond_nodes[flt.id]
            cond = self._compile(lambda: self.row.row(node))
            selected = _members(value, flt.mode, flt.values, datatype)
            return " AND ".join(c for c in (f"({cond})", selected) if c)
        raise QueryError(f"篩選 {flt.id} 的種類不支援在此階段")

    def _relative(self, flt: RelativeDateFilter, value: str) -> str:
        """相對日期：基準日所在期間的 first 到 last 個期間（含），不含之後的期間。"""
        if flt.anchor is not None:
            anchor = literal(flt.anchor, "date")
        elif self.state.today is not None:
            anchor = literal(self.state.today, "date")
        else:
            anchor = "current_date"
        ws = week_offset(self.datasource.settings.week_start)
        base = self.row.date_trunc(flt.period, anchor, "date", ws)
        start = f"({base} + {interval(flt.period, str(flt.first))})"
        end = f"({base} + {interval(flt.period, str(flt.last + 1))})"
        parts = [f"{value} >= {start}", f"{value} < {end}"]
        if not flt.include_future:
            parts.append(f"{value} < ({anchor} + INTERVAL 1 DAY)")
        cond = " AND ".join(parts)
        return f"(({cond}) OR {value} IS NULL)" if flt.include_null else f"({cond})"

    def _condition_node(self, flt: ConditionFilter) -> Node:
        try:
            node = parse_formula(flt.condition)
            info = self.analyzer.analyze(node)
        except (FormulaSyntaxError, FormulaSemanticError) as exc:
            raise QueryError(f"條件篩選 {flt.id} 的公式「{flt.condition}」：{exc}") from exc
        if info.level == "tc":
            raise QueryError(f"條件篩選 {flt.id} 含 Table Calculation，尚不支援", unsupported=True)
        if info.level == "agg" and (
            any(lod.kind != "fixed" for lod in self.lods_in(node))
            or any(lod.kind != "fixed" for a in self.aggregates_in(node) for lod in self.lods_in(a))
        ):
            raise QueryError("條件篩選含 INCLUDE／EXCLUDE LOD，尚不支援", unsupported=True)
        return node

    def _member_set(self, flt: Filter, top_nodes: dict[str, Node], source: str, stage: int) -> str:
        if isinstance(flt, TopNFilter):
            return self._top_n(flt, top_nodes[flt.id], source, stage)
        assert isinstance(flt, ConditionFilter)
        node = self.cond_nodes[flt.id]
        dim = self.dim_sql(flt.field)
        agg_name, agg_cols, keys = self.aggregate(
            "cond_agg", self.aggregates_in(node), [dim], source, stage, None
        )
        value = self._compile(lambda: self.compiler(_PostEnv(self, agg_cols)).post(node))
        name = self.name("cond")
        self.cte(
            stage,
            name,
            f"SELECT {ident(keys[0])} AS {ident('__member')} FROM {agg_name} WHERE {value}",
        )
        self.note(stage, f"條件篩選 {flt.id}：{flt.condition}")
        member = f"EXISTS (SELECT 1 FROM {name} WHERE {name}.{ident('__member')} IS NOT DISTINCT FROM {dim})"
        selected = _members(dim, flt.mode, flt.values, self.dim_datatype(flt.field))
        return f"({member} AND {selected})" if selected else member

    def _tuple_condition(self, flt: TupleFilter) -> str:
        """多欄位篩選：值組合以 OR 連接，組合內各欄位以 AND 連接（NULL 視為一個值）。"""
        if flt.mode == "all":
            return ""
        if any(self.is_measure(u) for u in flt.fields):
            raise QueryError(f"多欄位篩選 {flt.id} 的欄位必須是維度")
        dims = [(self.dim_sql(u), self.dim_datatype(u)) for u in flt.fields]
        if flt.tuples:
            groups = [
                " AND ".join(
                    f"{sql} IS NOT DISTINCT FROM {_lit(v, datatype)}"
                    for (sql, datatype), v in zip(dims, values, strict=True)
                )
                for values in flt.tuples
            ]
            member = "(" + " OR ".join(f"({g})" for g in groups) + ")"
        else:
            member = "FALSE"
        return f"({member})" if flt.mode == "include" else f"(NOT {member})"

    def _value_condition(self, flt: Filter, col: str) -> str:
        value = ident(col)
        if isinstance(flt, RangeFilter):
            datatype = "real"
            return _range(value, flt, datatype)
        if isinstance(flt, CategoricalFilter):
            # 離散的彙總計算欄位：每個標記的值是否在清單中
            numeric = all(isinstance(v, int | float) for v in flt.values if v is not None)
            return _categorical(value, flt, "real" if numeric else "string")
        raise QueryError(f"量值篩選 {flt.id} 只支援範圍與類別篩選", unsupported=True)

    def _top_by(self, flt: TopNFilter) -> Node:
        try:
            node = parse_formula(flt.by)
            info = self.analyzer.analyze(node)
        except (FormulaSyntaxError, FormulaSemanticError) as exc:
            raise QueryError(f"Top N 篩選 {flt.id} 的排名依據「{flt.by}」：{exc}") from exc
        if info.level != "agg":
            raise QueryError(f"Top N 篩選 {flt.id} 的排名依據必須是彙總")
        if any(lod.kind != "fixed" for lod in self.lods_in(node)) or any(
            lod.kind != "fixed" for a in self.aggregates_in(node) for lod in self.lods_in(a)
        ):
            raise QueryError("Top N 的排名依據含 INCLUDE／EXCLUDE LOD，尚不支援", unsupported=True)
        return node

    def _top_n(self, flt: TopNFilter, by: Node, source: str, stage: int) -> str:
        count = flt.count
        if isinstance(count, ParamRef):
            value = self.state.parameters.get(count.parameter)
            if not isinstance(value, int) or isinstance(value, bool):
                raise QueryError(f"Top N 篩選 {flt.id} 的參數值 {value!r} 不是整數")
            count = value
        if count < 0:
            raise QueryError(f"Top N 篩選 {flt.id} 的數量不能是負數")
        dim = self.dim_sql(flt.field)
        selected = _members(dim, flt.mode, flt.values, self.dim_datatype(flt.field))
        if selected:
            # 先選取的成員（篩選對話框的「一般」頁）中再排名
            source = self.cte(
                stage, self.name("topn_src"), f"SELECT * FROM {source} WHERE {selected}"
            )
        nodes = self.aggregates_in(by)
        agg_name, agg_cols, keys = self.aggregate("topn_agg", nodes, [dim], source, stage, None)
        value = self._compile(lambda: self.compiler(_PostEnv(self, agg_cols)).post(by))
        direction = "DESC" if flt.direction == "top" else "ASC"
        name = self.name("topn")
        body = (
            f"SELECT {ident(keys[0])} AS {ident('__member')} FROM {agg_name} "
            f"ORDER BY {value} {direction} NULLS LAST, {ident(keys[0])} ASC NULLS LAST LIMIT {count}"
        )
        self.cte(stage, name, body)
        self.note(
            stage,
            f"Top N {flt.id}：依 {flt.by} 取{'前' if flt.direction == 'top' else '後'} {count} 名",
        )
        return f"EXISTS (SELECT 1 FROM {name} WHERE {name}.{ident('__member')} IS NOT DISTINCT FROM {dim})"

    # ------------------------------------------------------------ LOD

    def _lod_dims(
        self, spec: LodSpec, view_dims: list[Dim] | None, compiler: ExprCompiler
    ) -> list[str]:
        lod = spec.node
        if lod.kind == "fixed":
            return [self._compile(lambda d=d: compiler.row(d)) for d in lod.dims]
        if view_dims is None:
            raise QueryError("INCLUDE／EXCLUDE LOD 只能用在檢視的量值", unsupported=True)
        if lod.kind == "include":
            sqls = [self.dim_sql(d.use, compiler) for d in view_dims]
            for dim in lod.dims:
                sql = self._compile(lambda dim=dim: compiler.row(dim))
                if sql not in sqls:
                    sqls.append(sql)
            return sqls
        excluded = {self._resolve(d).id for d in lod.dims}
        return [self.dim_sql(d.use, compiler) for d in view_dims if d.use.field not in excluded]

    def _attach(
        self, specs: list[LodSpec], source: str, name: str, stage: int, view_dims: list[Dim] | None
    ) -> str:
        """計算一組 LOD，併回每一列。"""
        if not specs:
            return source
        joins: list[str] = []
        selects = ["r.*"]
        row_r = self.compiler(_RowEnv(self, "r"))
        for spec in specs:
            dims = self._lod_dims(spec, view_dims, self.row)
            inner = self._compile(lambda spec=spec: self.row.agg(spec.node.expr))
            lod_name = self.name("lod")
            keys = [f"__d{i}" for i in range(len(dims))]
            cols = [f"{sql} AS {ident(k)}" for sql, k in zip(dims, keys, strict=True)]
            group = f" GROUP BY {', '.join(str(i + 1) for i in range(len(dims)))}" if dims else ""
            cols.append(f"{inner} AS {ident(VALUE_COL)}")
            self.cte(stage, lod_name, f"SELECT {', '.join(cols)} FROM {source}{group}")
            alias = f"l{len(joins)}"
            r_dims = self._lod_dims(spec, view_dims, row_r)
            on = (
                " AND ".join(
                    f"{sql} IS NOT DISTINCT FROM {alias}.{ident(k)}"
                    for sql, k in zip(r_dims, keys, strict=True)
                )
                or "TRUE"
            )
            joins.append(f"LEFT JOIN {lod_name} {alias} ON {on}")
            selects.append(f"{alias}.{ident(VALUE_COL)} AS {ident(spec.col)}")
            kind = {"fixed": "FIXED", "include": "INCLUDE", "exclude": "EXCLUDE"}[spec.node.kind]
            self.note(stage, f"{kind} LOD → {spec.col}")
        self.cte(stage, name, f"SELECT {', '.join(selects)} FROM {source} r {' '.join(joins)}")
        self.attached.update(s.node for s in specs)
        return name

    # ------------------------------------------------------------ 彙總

    def aggregate(
        self,
        prefix: str,
        nodes: list[Call],
        level: list[str],
        source: str,
        stage: int,
        view_dims: list[Dim] | None,
    ) -> tuple[str, dict[Call, str], list[str]]:
        """在指定層級彙總。回傳 CTE 名稱、彙總節點 → 欄名、層級鍵欄名。"""
        keys = [f"__k{i}" for i in range(len(level))]
        cols: dict[Call, str] = {}
        row_nodes = [n for n in nodes if not self._lod_only(n)]
        combo_nodes = [n for n in nodes if self._lod_only(n)]
        selects = [f"{sql} AS {ident(k)}" for sql, k in zip(level, keys, strict=True)]
        for node in row_nodes:
            col = f"__a{len(cols)}"
            selects.append(f"{self._compile(lambda node=node: self.row.agg(node))} AS {ident(col)}")
            cols[self.canon(node)] = col  # type: ignore[index]
        if not selects:
            selects.append(f"count(*) AS {ident('__n')}")
        group = f" GROUP BY {', '.join(str(i + 1) for i in range(len(level)))}" if level else ""
        base = self.cte(
            stage, self.name(prefix), f"SELECT {', '.join(selects)} FROM {source}{group}"
        )
        if not combo_nodes:
            return base, cols, keys

        groups: dict[tuple[str, ...], list[Call]] = {}
        for node in combo_nodes:
            extra: list[str] = []
            for lod in (lod for arg in node.args for lod in self.lods_in(arg)):
                spec = self.lods[lod]
                for sql in self._lod_dims(spec, view_dims, self.row):
                    if sql not in level and sql not in extra:
                        extra.append(sql)
            groups.setdefault(tuple(extra), []).append(node)
        joins: list[str] = []
        picks: list[str] = []
        for group_key, group_nodes in groups.items():
            g_sql = [*level, *group_key]
            g_keys = [f"__g{i}" for i in range(len(g_sql))]
            lod_cols = _unique(
                [
                    self.lods[lod].col
                    for n in group_nodes
                    for arg in n.args
                    for lod in self.lods_in(arg)
                ]
            )
            combo_cols = [f"{sql} AS {ident(k)}" for sql, k in zip(g_sql, g_keys, strict=True)]
            combo_cols += [ident(c) for c in lod_cols]
            combos = self.cte(
                stage, self.name("combos"), f"SELECT DISTINCT {', '.join(combo_cols)} FROM {source}"
            )
            combo_selects = [
                f"{ident(g)} AS {ident(k)}" for g, k in zip(g_keys, keys, strict=False)
            ]
            for node in group_nodes:
                col = f"__a{len(cols)}"
                combo_selects.append(
                    f"{self._compile(lambda node=node: self.row.agg(node))} AS {ident(col)}"
                )
                cols[self.canon(node)] = col  # type: ignore[index]
                picks.append(f"c{len(joins)}.{ident(col)}")
            combo_group = (
                f" GROUP BY {', '.join(str(i + 1) for i in range(len(level)))}" if level else ""
            )
            cagg = self.cte(
                stage,
                self.name("combo_agg"),
                f"SELECT {', '.join(combo_selects)} FROM {combos}{combo_group}",
            )
            alias = f"c{len(joins)}"
            on = (
                " AND ".join(f"b.{ident(k)} IS NOT DISTINCT FROM {alias}.{ident(k)}" for k in keys)
                or "TRUE"
            )
            joins.append(f"LEFT JOIN {cagg} {alias} ON {on}")
        joined = self.cte(
            stage,
            self.name(prefix),
            f"SELECT b.*, {', '.join(picks)} FROM {base} b {' '.join(joins)}",
        )
        return joined, cols, keys

    def _lod_only(self, node: Call) -> bool:
        """彙總的參數只含 LOD（與常值、參數），不含列層級欄位（CORR 等有兩個參數）。"""
        if not node.args:
            return False
        return any(self.lods_in(arg) for arg in node.args) and not any(
            self.has_row_fields(arg) for arg in node.args
        )

    # ------------------------------------------------------------ 排序與 Table Calculation

    def _sort_keys(
        self, dims: list[Dim], sorts: dict[str, Sort], rows_cte: str
    ) -> dict[str, tuple[str, str]]:
        """依量值排序的維度：在該維度層級計算排序值。回傳 維度鍵 → (欄名, ORDER BY 片段)。"""
        result: dict[str, tuple[str, str]] = {}
        for d in dims:
            sort = sorts.get(d.use.field)
            if sort is None or sort.kind != "by_field" or sort.by is None:
                continue
            node = self.measure_node(sort.by)
            nodes = self.aggregates_in(node)
            agg_name, agg_cols, keys = self.aggregate(
                "sort_agg", nodes, [self.dim_sql(d.use)], rows_cte, 7, None
            )
            value = self._compile(
                lambda node=node, agg_cols=agg_cols: self.compiler(_PostEnv(self, agg_cols)).post(
                    node
                )
            )
            name = self.name("sort")
            self.cte(
                7,
                name,
                f"SELECT {ident(keys[0])} AS {ident('__key')}, {value} AS {ident(VALUE_COL)} FROM {agg_name}",
            )
            col = f"__sort{len(result)}"
            direction = "DESC" if sort.order == "desc" else "ASC"
            result[d.key] = (col, f"{ident(col)} {direction} NULLS LAST")
            self._sort_ctes[d.key] = (name, col)
        return result

    def _natural_order(self, d: Dim, sort: Sort | None) -> str:
        col = ident(d.col)
        if sort is not None and sort.kind == "manual" and sort.manual_values:
            datatype = self.dim_datatype(d.use)
            cases = " ".join(
                f"WHEN {col} IS NOT DISTINCT FROM {literal(v, datatype)} THEN {i}"
                for i, v in enumerate(sort.manual_values)
            )
            return f"(CASE {cases} ELSE {len(sort.manual_values)} END), {col} ASC NULLS LAST"
        if sort is not None and sort.kind == "alphabetic" and sort.order == "desc":
            return f"{col} DESC NULLS LAST"
        return f"{col} ASC NULLS LAST"

    def _window(
        self,
        tc: TableCalc,
        dims: list[Dim],
        sorts: dict[str, Sort],
        sort_cols: dict[str, tuple[str, str]],
    ) -> _Window:
        addressing: list[Dim] = []
        for field_id in tc.addressing:
            addressing += [d for d in dims if d.use.field == field_id and d not in addressing]
        partition = [ident(d.col) for d in dims if d not in addressing]
        order: list[str] = []
        for d in addressing:
            if d.key in sort_cols:
                order.append(sort_cols[d.key][1])
            order.append(self._natural_order(d, sorts.get(d.use.field)))
        return _Window(partition=partition, order=order)

    def _table_calcs(
        self,
        dims: list[Dim],
        measures: list[Measure],
        marks: str,
        rows_cte: str,
        agg_cols: dict[Call, str],
        sort_cols: dict[str, tuple[str, str]],
    ) -> str:
        tc_measures = [m for m in measures if m.is_tc]
        base = marks
        sorted_dims = [d for d in dims if d.key in self._sort_ctes]
        if sorted_dims:
            joins = []
            selects = ["m.*"]
            for i, d in enumerate(sorted_dims):
                cte, col = self._sort_ctes[d.key]
                joins.append(
                    f"LEFT JOIN {cte} s{i} ON s{i}.{ident('__key')} IS NOT DISTINCT FROM m.{ident(d.col)}"
                )
                selects.append(f"s{i}.{ident(VALUE_COL)} AS {ident(col)}")
            base = self.cte(
                7, "sorted", f"SELECT {', '.join(selects)} FROM {marks} m {' '.join(joins)}"
            )
        if not tc_measures:
            return base
        sorts = {s.field: s for s in self.worksheet.sorts}
        selects = ["m.*"]
        joins: list[str] = []
        expressions: list[str] = []
        marks_rows: str | None = None
        for m in tc_measures:
            assert m.table_calc is not None
            window = self._window(m.table_calc, dims, sorts, sort_cols)
            totals: dict[Call, str] = {}
            for node in self.table_calcs_in(m.node):
                if node.name != "TOTAL" or node in totals:
                    continue
                if marks_rows is None:
                    marks_rows = self._marks_rows(dims, marks, rows_cte)
                partition_dims = [d for d in dims if ident(d.col) in window.partition]
                total_col, join = self._total(node, partition_dims, marks_rows, len(joins))
                totals[node] = total_col
                joins.append(join)
                selects.append(f"t{len(joins) - 1}.{ident(VALUE_COL)} AS {ident(total_col)}")
            env = _PostEnv(self, agg_cols, window, totals)
            sql = self._compile(lambda m=m, env=env: self.compiler(env).post(m.node))
            expressions.append(f"{sql} AS {ident(m.col)}")
            tc = m.table_calc
            self.note(
                7,
                f"{m.key}：{tc.type}，計算方向 {tc.preset}"
                + ("（推定）" if tc.inferred else "")
                + f"，分割 {len(window.partition)} 個維度、定址 {len(dims) - len(window.partition)} 個維度",
            )
        if joins:
            # 視窗函式不能直接引用同一層 SELECT 剛併入的欄位，先併入合計再計算
            base = self.cte(
                7, "totals", f"SELECT {', '.join(selects)} FROM {base} m {' '.join(joins)}"
            )
        return self.cte(7, "tc", f"SELECT m.*, {', '.join(expressions)} FROM {base} m")

    def _marks_rows(self, dims: list[Dim], marks: str, rows_cte: str) -> str:
        """量值篩選後仍留在檢視中的標記所對應的列（TOTAL 依這些列計算）。"""
        row_r = self.compiler(_RowEnv(self, "r"))
        on = (
            " AND ".join(
                f"k.{ident(d.col)} IS NOT DISTINCT FROM {self.dim_sql(d.use, row_r)}" for d in dims
            )
            or "TRUE"
        )
        return self.cte(
            7,
            "marks_rows",
            f"SELECT r.* FROM {rows_cte} r WHERE EXISTS (SELECT 1 FROM {marks} k WHERE {on})",
        )

    def _total(self, node: Call, partition: list[Dim], rows: str, index: int) -> tuple[str, str]:
        expr = node.args[0]
        nodes = self.aggregates_in(expr)
        level = [self.dim_sql(d.use) for d in partition]
        agg_name, agg_cols, keys = self.aggregate("total_agg", nodes, level, rows, 7, None)
        value = self._compile(lambda: self.compiler(_PostEnv(self, agg_cols)).post(expr))
        key_cols = [f"{ident(k)}" for k in keys]
        name = self.name("total")
        key_cols.append(f"{value} AS {ident(VALUE_COL)}")
        self.cte(7, name, f"SELECT {', '.join(key_cols)} FROM {agg_name}")
        alias = f"t{index}"
        on = (
            " AND ".join(
                f"{alias}.{ident(k)} IS NOT DISTINCT FROM m.{ident(d.col)}"
                for k, d in zip(keys, partition, strict=True)
            )
            or "TRUE"
        )
        return f"__total{index}", f"LEFT JOIN {name} {alias} ON {on}"

    def window_sql(self, node: Call, env: _PostEnv) -> str:
        window = env.window
        assert window is not None
        inner = self.compiler(_PostEnv(self, env.agg_cols, None, env.totals))
        name = node.name
        partition = f"PARTITION BY {', '.join(window.partition)}" if window.partition else ""
        order = f"ORDER BY {', '.join(window.order)}" if window.order else ""

        def over(frame: str = "", ordered: bool = True) -> str:
            parts = [partition, order if ordered else "", frame if ordered and order else ""]
            return f"OVER ({' '.join(p for p in parts if p)})"

        def arg(i: int) -> str:
            return self._compile(lambda: inner.post(node.args[i]))

        if name == "TOTAL":
            col = env.totals.get(node)
            if col is None:
                raise QueryError("內部錯誤：TOTAL 沒有對應的欄位")
            return ident(col)
        if name.startswith("RUNNING_"):
            fn = WINDOW_AGG[name.removeprefix("RUNNING_")]
            sql = f"{fn}({arg(0)}) {over('ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW')}"
            return self._integer(sql, node) if fn in ("sum", "count") else sql
        if name.startswith("WINDOW_"):
            base = name.removeprefix("WINDOW_")
            if base == "PERCENTILE":
                p = node.args[1]
                assert isinstance(p, LiteralNode)
                value, fn, bounds = (
                    f"quantile_cont({arg(0)}, {float(p.value)!r})",
                    "",
                    node.args[2:],
                )
            elif base in ("CORR", "COVAR", "COVARP"):
                fn = WINDOW_AGG[base]
                value, bounds = f"{fn}({arg(0)}, {arg(1)})", node.args[2:]
            else:
                fn = WINDOW_AGG[base]
                value, bounds = f"{fn}({arg(0)})", node.args[1:]
            if not bounds:
                sql = f"{value} OVER ({partition})" if partition else f"{value} OVER ()"
            else:
                start = _bound(bounds[0], "start")
                end = _bound(bounds[1], "end")
                sql = f"{value} {over(f'ROWS BETWEEN {start} AND {end}')}"
            return self._integer(sql, node) if fn in ("sum", "count") else sql
        if name == "INDEX":
            return f"row_number() {over()}"
        if name == "FIRST":
            return f"(1 - row_number() {over()})"
        if name == "LAST":
            size = f"count(*) OVER ({partition})" if partition else "count(*) OVER ()"
            return f"({size} - row_number() {over()})"
        if name == "SIZE":
            return f"count(*) OVER ({partition})" if partition else "count(*) OVER ()"
        if name.startswith("RANK"):
            value = arg(0)
            direction = "DESC"
            if len(node.args) == 2:
                literal_node = node.args[1]
                if (
                    isinstance(literal_node, LiteralNode)
                    and str(literal_node.value).lower() == "asc"
                ):
                    direction = "ASC"
            parts = [*window.partition, f"(({value}) IS NULL)"]
            rank_partition = f"PARTITION BY {', '.join(parts)}"
            ordering = f"ORDER BY {value} {direction}"
            if name == "RANK":
                fn = f"rank() OVER ({rank_partition} {ordering})"
            elif name == "RANK_DENSE":
                fn = f"dense_rank() OVER ({rank_partition} {ordering})"
            elif name == "RANK_UNIQUE":
                tiebreak = f", {', '.join(window.order)}" if window.order else ""
                fn = f"row_number() OVER ({rank_partition} {ordering}{tiebreak})"
            else:
                fn = f"count(*) OVER ({rank_partition} {ordering} RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
            return f"(CASE WHEN ({value}) IS NULL THEN NULL ELSE CAST({fn} AS BIGINT) END)"
        if name == "LOOKUP":
            offset = node.args[1]
            assert isinstance(offset, LiteralNode)
            n = int(offset.value)
            if n == 0:
                return arg(0)
            fn = "lag" if n < 0 else "lead"
            return f"{fn}({arg(0)}, {abs(n)}) {over()}"
        raise QueryError(f"Table Calculation 函式 {name} 尚不支援", unsupported=True)

    def _integer(self, sql: str, node: Call) -> str:
        if self.datatype(node) == "integer":
            return f"CAST({sql} AS BIGINT)"
        return sql

    # ------------------------------------------------------------ 輸出

    def _render(self, main: str) -> str:
        lines = ["WITH"]
        stage_of = {name: n for n, note in self.plan.items() for name in note.ctes}
        current = 0
        parts: list[str] = []
        for name, body in self.ctes:
            stage = stage_of[name]
            header = ""
            if stage != current:
                if current <= PUSHDOWN_BOUNDARY < stage:
                    header += "-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行\n"
                header += f"-- 第 {stage} 段：{STAGES[stage]}\n"
                current = stage
            parts.append(f"{header}{ident(name)} AS (\n  {body}\n)")
        lines.append(",\n".join(parts))
        tail = ""
        if current <= PUSHDOWN_BOUNDARY:
            tail = "-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行\n"
        return "\n".join(lines) + f"\n{tail}-- 第 8 段：{STAGES[8]}與輸出\n{main}\n"


# ---------------------------------------------------------------- 條件


def _where(conds: list[str]) -> str:
    conds = [c for c in conds if c]
    return f" WHERE {' AND '.join(conds)}" if conds else ""


def _categorical(value: str, flt: CategoricalFilter, datatype: str) -> str:
    return _members(value, flt.mode, flt.values, datatype)


def _members(value: str, mode: str, values: list[Scalar], datatype: str) -> str:
    """成員選取：include 只保留、exclude 排除列出的值（Null 也可以列出）；all 沒有條件。"""
    if mode == "all":
        return ""
    non_null = [v for v in values if v is not None]
    has_null = len(non_null) != len(values)
    items = ", ".join(_lit(v, datatype) for v in non_null)
    member = f"{value} IN ({items})" if non_null else "FALSE"
    if mode == "include":
        return f"({member} OR {value} IS NULL)" if has_null else f"({member})"
    if has_null:
        return f"({value} IS NOT NULL AND NOT ({member}))"
    return f"({value} IS NULL OR NOT ({member}))"


def _range(value: str, flt: RangeFilter, datatype: str) -> str:
    parts = []
    if flt.min is not None:
        parts.append(f"{value} >= {_lit(flt.min, datatype)}")
    if flt.max is not None:
        parts.append(f"{value} <= {_lit(flt.max, datatype)}")
    if not parts:
        return "" if flt.include_null else f"({value} IS NOT NULL)"
    cond = " AND ".join(parts)
    return f"(({cond}) OR {value} IS NULL)" if flt.include_null else f"({cond})"


def _lit(value: Scalar, datatype: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return literal(value, "boolean")
    if datatype in ("integer", "real") and isinstance(value, (int, float)):
        return literal(value, "integer" if isinstance(value, int) else "real")
    if datatype in ("date", "datetime") and isinstance(value, str):
        return literal(value, "datetime" if "T" in value or " " in value else "date")
    if isinstance(value, (int, float)):
        return literal(value, "integer" if isinstance(value, int) else "real")
    return literal(value, "string")


def _bound(node: Node, side: Literal["start", "end"]) -> str:
    if isinstance(node, Call) and node.name in ("FIRST", "LAST") and not node.args:
        return "UNBOUNDED PRECEDING" if node.name == "FIRST" else "UNBOUNDED FOLLOWING"
    if isinstance(node, LiteralNode) and node.kind == "integer":
        n = int(node.value)
        if n == 0:
            return "CURRENT ROW"
        return f"{-n} PRECEDING" if n < 0 else f"{n} FOLLOWING"
    raise QueryError(
        f"視窗{('起點' if side == 'start' else '終點')}只支援整數常值、FIRST() 或 LAST()",
        unsupported=True,
    )


DOMAIN_LIMIT = 2000


def domain_sql(
    ir: IR, datasource_id: str, use: FieldUse, kind: Literal["values", "range", "frequency"]
) -> str:
    """篩選控制項的可選值：資料源篩選後的不重複值、最小值與最大值，或各值的筆數（多到少）。"""
    datasource = next((ds for ds in ir.datasources if ds.id == datasource_id), None)
    if datasource is None:
        raise QueryError(f"找不到資料源「{datasource_id}」")
    if not any(f.id == use.field for f in datasource.fields):
        raise QueryError(f"資料源「{datasource_id}」沒有欄位「{use.field}」")
    holder = Worksheet(
        id="domain",
        name="domain",
        datasource=datasource_id,
        marks=Marks(type="automatic"),
        source_ref="domain",
    )
    query = WorksheetQuery(ir.model_copy(update={"worksheets": [holder]}), "domain")
    if query.is_measure(use):
        raise QueryError("量值沒有可選值")
    query.register_lods(query.field_node(use.field))
    if query.lods:
        raise QueryError("含 LOD 的欄位尚不支援列出可選值", unsupported=True)
    value = query.dim_sql(use)
    conds = [query._row_condition(f) for f in datasource.filters if f.stage == "datasource"]
    table = ".".join(ident(p) for p in datasource.connection.table.split("."))
    if kind == "range":
        return f"SELECT min({value}) AS {ident('min')}, max({value}) AS {ident('max')} FROM {table}{_where(conds)}"
    if kind == "frequency":
        return (
            f"SELECT {value} AS {ident('value')}, count(*) AS {ident('rows')} FROM {table}{_where(conds)} "
            f"GROUP BY 1 ORDER BY 2 DESC, 1 NULLS LAST LIMIT {DOMAIN_LIMIT}"
        )
    return (
        f"SELECT DISTINCT {value} AS {ident('value')} FROM {table}{_where(conds)} "
        f"ORDER BY 1 NULLS LAST LIMIT {DOMAIN_LIMIT}"
    )


def _find_worksheet(ir: IR, key: str) -> Worksheet:
    for ws in ir.worksheets:
        if key in (ws.id, ws.name):
            return ws
    names = "、".join(f"{ws.id}（{ws.name}）" for ws in ir.worksheets)
    raise QueryError(f"找不到工作表「{key}」；可用的工作表：{names}")


def worksheet_sql(ir: IR, worksheet: str, state: QueryState | None = None) -> WorksheetSql:
    return WorksheetQuery(ir, worksheet, state).build()


__all__ = [
    "PUSHDOWN_BOUNDARY",
    "STAGES",
    "OutputColumn",
    "QueryError",
    "StageNote",
    "WorksheetQuery",
    "WorksheetSql",
    "domain_sql",
    "worksheet_sql",
]
