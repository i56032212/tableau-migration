"""工作表查詢：依 Tableau 的運算順序產生 DuckDB SQL（D1–D4）。

運算順序與 CTE 的對應：

1. 萃取篩選與資料源篩選  ``src``（萃取篩選在建立萃取時已套用，不重複）
2. 內容篩選  ``ctx_pre`` → ``ctx``（含內容 Top N）
3. FIXED LOD、集合、Top N  ``lod_*``、``ctx_ext``、``sets_ext``、``topn_*``
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

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from tmig.formula.analyze import Analyzer, Target
from tmig.formula.ast import (
    Binary,
    Call,
    FieldRef,
    If,
    LiteralNode,
    Lod,
    Node,
    Unary,
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
    Calc,
    CategoricalFilter,
    ConditionFilter,
    DataField,
    Datasource,
    FieldUse,
    Filter,
    LiveConnection,
    Marks,
    NestedTableCalc,
    Pane,
    ParamRef,
    RangeFilter,
    RelativeDateFilter,
    Scalar,
    SetMembers,
    Sort,
    TableCalc,
    TopNFilter,
    TupleFilter,
    Worksheet,
)
from tmig.ir.scope import DatasourceScope
from tmig.ir.setformula import FormulaValueError, members_formula, set_levels
from tmig.planner.identity import Identity
from tmig.planner.state import EffectiveState, QueryState, SetValue, apply_state, use_key
from tmig.sqlgen.expr import ExprCompiler, interval, week_offset
from tmig.sqlgen.model import (
    PART_COL,
    Branch,
    ModelError,
    ModelPlan,
    ModelPlanner,
    RowFilter,
    Usage,
)
from tmig.sqlgen.source import SourceError, datasource_source
from tmig.sqlgen.sql import ident, literal

if TYPE_CHECKING:
    from tmig.data.executor import QueryResult

DUPLICATE_SAFE = frozenset({"MIN", "MAX", "COUNTD", "ATTR"})
"""同一列重複出現也不影響結果的彙總（Relationship 模型的一對多分支不需要檢查）。"""

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
    3: "FIXED LOD、集合、Top N",
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
    outer: LodSpec | None = None
    """外層的 LOD（巢狀時）：內層的 INCLUDE／EXCLUDE 相對於外層的維度，不是檢視的維度。"""
    inner: dict[Lod, LodSpec] = field(default_factory=dict)
    """運算式中的內層 LOD（同一個 LOD 用在不同的外層時各算一次）。"""

    def in_fixed(self) -> bool:
        """自己或外層有 FIXED：與 FIXED 一起在維度篩選前計算。"""
        spec: LodSpec | None = self
        while spec is not None:
            if spec.node.kind == "fixed":
                return True
            spec = spec.outer
        return False


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
class CteDef:
    """一段 CTE：名稱、內容、所屬的運算階段（1–8），與能否下推到來源資料庫。"""

    name: str
    body: str
    stage: int
    pushdown: bool
    """不含 Table Calculation：第 1–6 段，以及依來源資料計算的排序值與 TOTAL。"""


@dataclass
class Guard:
    """執行前的檢查：sql 回傳一個數字，大於 0 時查詢不能給出正確的數字（以 message 列為不支援）。"""

    sql: str
    message: str


@dataclass
class WorksheetSql:
    worksheet: str
    sql: str
    columns: list[OutputColumn]
    plan: list[StageNote]
    state: EffectiveState
    ctes: list[CteDef] = field(default_factory=list)
    """sql 的各段 CTE，供兩段式執行拆分（可下推的 CTE 在來源資料庫執行）。"""
    main: str = ""
    """最後的 SELECT（第 8 段的輸出），一律在 DuckDB 執行。"""
    live: bool = False
    custom_sql: list[str] = field(default_factory=list)
    """Live 連線的自訂 SQL 原文（SQL 中以佔位資料表表示）。"""
    parameters: dict[str, tuple[Scalar, str]] = field(default_factory=dict)
    """自訂 SQL 中參數的目前值：內部名稱（不含方括號）與顯示名稱 → （值、資料型別）。"""
    guards: list[Guard] = field(default_factory=list)
    """Relationship 模型：在比檢視粗的層級彙總時，確認其他資料表不會讓量值重複計算（見 tmig.sqlgen.model）。"""


@dataclass
class _Window:
    partition: list[str]
    order: list[str]


@dataclass
class _Nested:
    """巢狀 Table Calculation 中先算好的一段：被參照的 Table Calculation 計算欄位，或寫在另一個
    Table Calculation 函式參數中的 Table Calculation 函式。視窗函式不能巢狀，所以分層計算：
    depth 為它之前需要先算好的層數。"""

    col: str
    node: Node
    window: _Window
    depth: int
    fields: dict[str, str]
    """計算這一段時已經算好的計算欄位 id → 欄名。"""
    calls: dict[Node, str]
    """計算這一段時已經算好的 Table Calculation 函式（去掉位置資訊的語法樹）→ 欄名。"""


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
        self.user = query.user

    def _qualify(self, name: str) -> str:
        return f"{ident(self.alias)}.{ident(name)}" if self.alias else ident(name)

    def column(self, target: Target) -> str:
        data_field = self.query.fields[target.id]
        if data_field.set is not None and data_field.calc is None:
            return self._qualify(self.query.set_column(data_field))
        if data_field.column is None:
            raise QueryError(f"欄位 {data_field.caption} 沒有實體欄位")
        return self._qualify(self.query.source.column(data_field))

    def param(self, target: Target) -> str:
        return self.query.param_sql(target.id)

    def param_value(self, target: Target) -> object:
        return self.query.param_value(target.id)

    def lod(self, node: Lod) -> str:
        spec = self.query.lods.get(node)
        if spec is None or spec.col not in self.query.attached:
            raise QueryError(
                "LOD 運算式在這個運算階段還不能使用（例如以 INCLUDE／EXCLUDE 作維度篩選）",
                unsupported=True,
            )
        return self._qualify(spec.col)

    def part_condition(self, node: Call) -> str | None:
        """Relationship 模型中量值來自多張資料表：彙總只計算所屬部分的列（不需要限定時為 None）。"""
        return self.query.part_condition(node, self._qualify(PART_COL))

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
        totals: dict[Node, str] | None = None,
        fields: dict[str, str] | None = None,
        calls: dict[Node, str] | None = None,
    ) -> None:
        self.query = query
        self.agg_cols = agg_cols
        self.window = window
        self.totals = totals or {}
        """TOTAL（去掉位置資訊的語法樹）→ 已併入的合計欄。"""
        self.fields = fields or {}
        self.calls = calls or {}
        self.week_start = query.week_start
        self.user = query.user

    def calc_column(self, target: Target) -> str | None:
        """巢狀計算中先算好的 Table Calculation 計算欄位。"""
        col = self.fields.get(target.id)
        return ident(col) if col is not None else None

    def column(self, target: Target) -> str:
        raise QueryError(f"列層級欄位 {target.id} 必須彙總")

    def param(self, target: Target) -> str:
        return self.query.param_sql(target.id)

    def param_value(self, target: Target) -> object:
        return self.query.param_value(target.id)

    def lod(self, node: Lod) -> str:
        raise QueryError("LOD 運算式必須彙總")

    def aggregate(self, node: Call) -> str:
        col = self.agg_cols.get(self.query.canon(node))  # type: ignore[arg-type]
        if col is None:
            raise QueryError(f"內部錯誤：彙總 {node.name} 沒有對應的欄位")
        return ident(col)

    def table_calc(self, node: Call) -> str:
        key = self.query.canon(node)
        if node.name == "TOTAL":
            # TOTAL 依來源資料另外計算後併入，不是視窗函式：巢狀時也可以直接引用
            col = self.totals.get(key)
            if col is None:
                raise QueryError("內部錯誤：TOTAL 沒有對應的欄位")
            return ident(col)
        if key in self.calls:
            return ident(self.calls[key])
        if self.window is None:
            raise QueryError(
                f"這個位置不能計算 Table Calculation（{node.name}，例如依 Table Calculation 排序），尚不支援",
                unsupported=True,
            )
        return self.query.window_sql(node, self)


def with_set_values(datasource: Datasource, values: dict[str, SetValue]) -> Datasource:
    """集合動作改變後的成員：重新產生固定成員集合的公式（只影響這次查詢）。
    不在這個資料源的集合略過（檢視器把同一份狀態送給每張工作表）。"""
    fields: list[DataField] = []
    for data_field in datasource.fields:
        value = values.get(data_field.id)
        if value is None:
            fields.append(data_field)
            continue
        if data_field.set is None or data_field.set.rule.kind != "members":
            raise QueryError(f"「{data_field.caption}」不是固定成員的集合，不能由集合動作改變成員")
        try:
            formula = members_formula(
                set_levels(datasource.fields, data_field.set), value.members, value.mode
            )
        except FormulaValueError as exc:
            raise QueryError(f"集合「{data_field.caption}」的成員：{exc}") from exc
        rule = SetMembers(kind="members", mode=value.mode, members=value.members)
        fields.append(
            data_field.model_copy(
                update={
                    "calc": Calc(formula=formula),
                    "set": data_field.set.model_copy(update={"rule": rule}),
                }
            )
        )
    return datasource.model_copy(update={"fields": fields})


class WorksheetQuery:
    """一張工作表在某個篩選狀態下的查詢。"""

    def __init__(
        self,
        ir: IR,
        worksheet: str,
        state: QueryState | None = None,
        user: Identity | None = None,
    ) -> None:
        """user：登入者的身分，使用者函式依此代入（不從篩選狀態取得，見 tmig.planner.identity）。"""
        self.ir = ir
        self.user = user
        self.worksheet = _find_worksheet(ir, worksheet)
        self.datasource = next(ds for ds in ir.datasources if ds.id == self.worksheet.datasource)
        if state is not None and state.sets:
            self.datasource = with_set_values(self.datasource, state.sets)
        self.state = apply_state(ir, self.worksheet, state or QueryState())
        self.scope = DatasourceScope(self.datasource.name, self.datasource.fields, ir.parameters)
        self.analyzer = Analyzer(self.scope)
        self.fields: dict[str, DataField] = {f.id: f for f in self.datasource.fields}
        self.params = {p.id: p for p in ir.parameters}
        self.week_start = self.datasource.settings.week_start
        try:
            self.source = datasource_source(self.datasource)
        except SourceError as exc:
            raise QueryError(str(exc), unsupported=True) from exc
        self.ctes: list[tuple[str, str]] = []
        self.pushdown: dict[str, bool] = {}
        self.plan: dict[int, StageNote] = {n: StageNote(n, [], []) for n in STAGES}
        self.lods: dict[Lod, LodSpec] = {}
        self.cond_nodes: dict[str, Node] = {}
        """條件篩選 id → 條件的語法樹。"""
        self.attached: set[str] = set()
        """已併到每一列的 LOD 欄名。"""
        self._lod_count = 0
        self.sets: dict[str, str] = {}
        """Top N 與條件集合的欄位 id → 每一列是否在集合中的欄名（內容篩選後計算，見 _attach_sets）。"""
        self.attached_sets: set[str] = set()
        self.row = ExprCompiler(self.analyzer, _RowEnv(self))
        self._names: dict[str, int] = {}
        self._canon: dict[Node, Node] = {}
        self._sort_ctes: dict[str, tuple[str, str]] = {}
        self.model_plan: ModelPlan | None = None
        """Relationship 模型的連接方式（build 時決定）。"""
        self._guards: dict[tuple[int, int], list[str]] = {}
        """需要檢查的分支（ModelPlan.parts 與 Part.branches 的索引）→ 在哪些層級彙總。"""
        self._owners: dict[Node, str | None] = {}
        """彙總 → 所屬的量值表（見 owner_of）。"""
        self._agg_parts: list[int] = []
        """目前編譯的這組彙總用到哪些部分的列（量值來自多張資料表時，見 _parts_scope）。"""
        self._domain_conds: list[str] = []
        """列層級篩選的條件（直接套用）：補上維度成員時使用。"""
        self._previous: Callable[[Callable[[int], str]], str] | None = None
        """PREVIOUS_VALUE 的代換（見 _previous_value）：參數為編譯第 i 個引數的函式。"""
        self.recursive = False
        """是否有遞迴的 CTE（PREVIOUS_VALUE）：輸出時寫成 WITH RECURSIVE。"""

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

    def cte(self, stage: int, name: str, body: str, pushdown: bool | None = None) -> str:
        """新增一段 CTE。pushdown 預設依階段：第 1–6 段可下推；Table Calculation 一律在 DuckDB。"""
        self.ctes.append((name, body))
        self.pushdown[name] = stage <= PUSHDOWN_BOUNDARY if pushdown is None else pushdown
        self.plan[stage].ctes.append(name)
        return name

    def note(self, stage: int, text: str) -> None:
        self.plan[stage].detail.append(text)

    def param_value(self, param_id: str) -> object:
        return self.state.parameters.get(param_id, self.params[param_id].value)

    def param_sql(self, param_id: str) -> str:
        return literal(self.param_value(param_id), self.params[param_id].datatype)

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

    def sets_in(self, node: Node) -> list[str]:
        """用到的 Top N 與條件集合（含計算欄位與 LOD 之中的）。"""
        found: list[str] = []

        def visit(item: Node) -> None:
            if isinstance(item, FieldRef):
                target = self._resolve(item)
                if target.kind != "field":
                    return
                data_field = self.fields[target.id]
                if data_field.set is not None and data_field.calc is None:
                    if target.id not in found:
                        found.append(target.id)
                elif target.formula is not None:
                    visit(self.analyzer.calc_ast(target))
                return
            for sub in children(item):
                visit(sub)

        visit(node)
        return found

    def set_column(self, data_field: DataField) -> str:
        col = self.sets.get(data_field.id)
        if col is None or data_field.id not in self.attached_sets:
            raise QueryError(
                f"集合「{data_field.caption}」在這個運算階段還不能使用"
                "（例如用在資料源篩選、內容篩選或 FIXED LOD 中），尚不支援",
                unsupported=True,
            )
        return col

    def register_lods(self, node: Node) -> None:
        for field_id in self.sets_in(node):
            self.sets.setdefault(field_id, f"__set_{len(self.sets)}")
        for lod in self.lods_in(node):
            if lod not in self.lods:
                self.lods[lod] = self._lod_spec(lod, None)

    def _lod_spec(self, lod: Lod, outer: LodSpec | None) -> LodSpec:
        for dim in lod.dims:
            if self.lods_in(dim):
                raise QueryError("LOD 的維度本身含 LOD，尚不支援", unsupported=True)
        spec = LodSpec(lod, f"__lod_{self._lod_count}", outer)
        self._lod_count += 1
        for inner in self.lods_in(lod.expr):
            if inner not in spec.inner:
                spec.inner[inner] = self._lod_spec(inner, spec)
        return spec

    def _all_lods(self) -> list[LodSpec]:
        """所有 LOD，內層在外層之前。"""
        found: list[LodSpec] = []

        def visit(spec: LodSpec) -> None:
            for inner in spec.inner.values():
                visit(inner)
            found.append(spec)

        for spec in self.lods.values():
            visit(spec)
        return found

    @contextmanager
    def _lod_scope(self, spec: LodSpec) -> Iterator[None]:
        """編譯外層 LOD 的運算式時，其中的 LOD 指向內層各自的計算結果。"""
        saved = self.lods
        self.lods = {**saved, **spec.inner}
        try:
            yield
        finally:
            self.lods = saved

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
            if _noop(flt):
                continue  # 「全部」：沒有條件，不需要計算其中的 LOD
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

        # 巢狀的 LOD：外層或自己有 FIXED 的在維度篩選前計算，內層先算
        fixed = [s for s in self._all_lods() if s.in_fixed()]
        relative = [s for s in self._all_lods() if not s.in_fixed()]

        # Relationship 模型：依用到的欄位決定要查哪些邏輯資料表、怎麼連接
        base_sql = self.source.from_sql
        if self.source.model is not None:
            self.model_plan = self._plan_model(dims, measures, sorts, stage, top_nodes, member_set)
            base_sql = self.model_plan.from_sql
            self._agg_parts = list(range(len(self.model_plan.parts)))
            for text in self.model_plan.notes:
                self.note(1, text)

        # 1. 資料源篩選
        for flt in stage["extract"]:
            self.note(1, f"萃取篩選 {flt.id} 已在建立萃取時套用，不重複")
        conds = [self._filter_condition(f) for f in stage["datasource"]]
        self._domain_conds = [self._row_condition(f) for f in stage["datasource"]]
        if self.source.projected:
            # Live 連線與 Relationship 模型：先以欄位 id 為別名選出實體欄位（各資料表的同名欄位不衝突），
            # 再套用資料源篩選
            self.cte(1, "base", base_sql)
            self.cte(1, "src", f"SELECT * FROM base{_where(conds)}")
        else:
            self.cte(1, "src", f"SELECT * FROM {base_sql}{_where(conds)}")
        for flt in stage["datasource"]:
            self.note(1, f"資料源篩選 {flt.id}")

        # 2. 內容篩選（內容 Top N 與彙總條件以其他內容篩選後的資料計算）
        ctx_dims = [f for f in stage["context"] if not member_set(f)]
        ctx_top = [f for f in stage["context"] if member_set(f)]
        source = "src"
        if ctx_dims or ctx_top:
            conds = [self._filter_condition(f) for f in ctx_dims]
            source = self.cte(2, "ctx_pre", f"SELECT * FROM src{_where(conds)}")
            top_conds = [self._member_set(f, top_nodes, source, 2) for f in ctx_top]
            source = self.cte(2, "ctx", f"SELECT * FROM {source}{_where(top_conds)}")
            self._domain_conds += [self._row_condition(f) for f in ctx_dims] + top_conds
            for flt in stage["context"]:
                self.note(2, f"內容篩選 {flt.id}")

        # 3. FIXED LOD、集合、Top N 與彙總條件（以內容篩選後、其他維度篩選前的資料計算）
        source = self._attach(fixed, source, "ctx_ext", 3, view_dims=None)
        source = self._attach_sets(source, 3)
        top_conds = [
            self._member_set(f, top_nodes, source, 3)
            for f in stage["top_n"] + stage["dimension"]
            if member_set(f)
        ]

        # 4. 維度篩選（「全部」的篩選沒有條件，不另建 CTE）
        dimension = [f for f in stage["dimension"] if not member_set(f)]
        conds = [c for c in [self._filter_condition(f) for f in dimension] + top_conds if c]
        self._domain_conds += [self._row_condition(f) for f in dimension] + top_conds
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
        self._guard(self._covered([d.use for d in dims]), "檢視的彙總", agg_nodes)
        agg_name, agg_cols, keys = self.aggregate("agg", agg_nodes, level, rows_cte, 6, dims)
        if self.model_plan is not None and self.model_plan.domain is not None and dims:
            agg_name = self._domain_marks(agg_name, agg_cols, keys, dims)
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
        fragments: dict[str, list[str]] = {}
        for d in dims:
            fragments[d.key] = [sort_cols[d.key][1]] if d.key in sort_cols else []
            fragments[d.key].append(self._natural_order(d, sorts.get(d.use.field)))
        headers = self._column_headers(dims)
        if headers:
            rank = ", ".join(f for d in headers for f in fragments[d.key])
            outputs.append(f"DENSE_RANK() OVER (ORDER BY {rank}) AS {ident(COLUMN_ORDER)}")
            columns.append(OutputColumn(COLUMN_ORDER, "sort", "integer", None))
        order = [f for d in dims for f in fragments[d.key]]
        order_sql = f"\nORDER BY {', '.join(order)}" if order else ""
        main = f"SELECT {', '.join(outputs)}\nFROM {final}{_where(conds)}{order_sql}"
        stage_of = {name: n for n, note in self.plan.items() for name in note.ctes}
        return WorksheetSql(
            worksheet=ws.id,
            sql=self._render(main),
            columns=columns,
            plan=[self.plan[n] for n in STAGES if self.plan[n].ctes or self.plan[n].detail],
            state=self.state,
            ctes=[
                CteDef(name, body, stage_of[name], self.pushdown[name]) for name, body in self.ctes
            ],
            main=main,
            live=isinstance(self.datasource.connection, LiveConnection),
            custom_sql=list(self.source.custom_sql),
            parameters=custom_sql_parameters(self.ir, self.state.parameters)
            if self.source.custom_sql
            else {},
            guards=self._guard_list(),
        )

    # ------------------------------------------------------------ Relationship 模型

    def _plan_model(
        self,
        dims: list[Dim],
        measures: list[Measure],
        sorts: dict[str, Sort],
        stage: dict[str, list[Filter]],
        top_nodes: dict[str, Node],
        member_set: Callable[[Filter], bool],
    ) -> ModelPlan:
        """檢視用到的欄位（維度、量值、排序、LOD、Top N）與列層級篩選 → 要查的邏輯資料表與連接方式。"""
        usage = Usage()
        for d in dims:
            self.collect(self.field_node(d.use.field), usage)
        for m in measures:
            self.collect(m.node, usage)
        for sort in sorts.values():
            if sort.by is not None:
                self.collect(self.measure_node(sort.by), usage)
        filters: list[RowFilter] = []
        others: set[str] = set()
        for flt in stage["datasource"] + stage["context"] + stage["top_n"] + stage["dimension"]:
            if _noop(flt):
                continue
            nodes: list[Node] = [
                self.field_node(use.field)
                for use in (flt.fields if isinstance(flt, TupleFilter) else [flt.field])
            ]
            if flt.id in self.cond_nodes:
                nodes.append(self.cond_nodes[flt.id])
            if flt.id in top_nodes:
                nodes.append(top_nodes[flt.id])
            found = Usage()
            for node in nodes:
                self.collect(node, found)
            others |= set(found.tables)
            if member_set(flt):
                # Top N 與彙總條件以彙總選出成員：用到的欄位與檢視的欄位一樣看待
                usage.merge(found)
                continue
            if flt.stage == "top_n":
                continue
            has_lod = any(self.lods_in(node) for node in nodes)
            filters.append(RowFilter(flt.id, set(found.fields), set(found.tables), has_lod))
        # 檢視的維度用到的資料表（判斷是否要補上沒有量值的維度成員；維度含 LOD 時不補）
        dim_usage = Usage()
        dim_nodes = [self.field_node(d.use.field) for d in dims]
        for node in dim_nodes:
            self.collect(node, dim_usage)
        if any(self.lods_in(node) for node in dim_nodes) or any(f.lod for f in filters):
            return self.plan_model(usage, filters)
        view = {
            owner
            for m in measures
            for a in self.aggregates_in(m.node)
            if (owner := self.owner_of(a)) is not None
        }
        return self.plan_model(usage, filters, set(dim_usage.tables), others, view)

    def plan_model(
        self,
        usage: Usage,
        filters: list[RowFilter],
        dims: set[str] | None = None,
        others: set[str] | None = None,
        view: set[str] | None = None,
    ) -> ModelPlan:
        model = self.source.model
        assert model is not None
        try:
            return ModelPlanner(self.datasource, model).plan(usage, filters, dims, others, view)
        except ModelError as exc:
            raise QueryError(str(exc), unsupported=True) from exc

    def owner_of(self, node: Call) -> str | None:
        """Relationship 模型中彙總所屬的量值表：參數中實體欄位（與資料表的筆數欄位、集合的層級欄位）
        所在的邏輯資料表。只含 LOD、參數與常值時為 None（LOD 已併到每一列）；用到多張資料表的欄位時
        列為不支援（Tableau 以列層級連接這些資料表後再彙總）。"""
        if self.source.model is None:
            return None
        key = self.canon(node)
        if key in self._owners:
            return self._owners[key]
        tables: list[str] = []

        def visit(item: Node) -> None:
            if isinstance(item, FieldRef):
                target = self._resolve(item)
                if target.kind != "field":
                    return
                data_field = self.fields[target.id]
                if data_field.set is not None and data_field.calc is None:
                    for use in data_field.set.levels:
                        visit(self.field_node(use.field))
                    return
                if data_field.table is not None and data_field.table not in tables:
                    tables.append(data_field.table)
                if target.formula is not None:
                    visit(self.analyzer.calc_ast(target))
                return
            if isinstance(item, Lod):
                return
            for sub in children(item):
                visit(sub)

        for arg in node.args:
            visit(arg)
        if len(tables) > 1:
            captions = {t.id: t.caption for t in self.source.model.tables}
            names = "、".join(f"「{captions.get(t, t)}」" for t in tables)
            raise QueryError(
                f"Relationship 模型中 {node.name} 同時用到多張資料表（{names}）的欄位，尚不支援",
                unsupported=True,
            )
        owner = tables[0] if tables else None
        self._owners[key] = owner
        return owner

    def part_of(self, node: Call) -> int | None:
        """彙總所屬的部分（ModelPlan.parts 的索引）；不屬於任何量值表時為 None。"""
        plan = self.model_plan
        if plan is None or not plan.parts:
            return None
        owner = self.owner_of(node)
        if owner is None:
            return None
        for index, part in enumerate(plan.parts):
            if part.table == owner:
                return index
        raise QueryError(f"{node.name} 所屬的資料表不在這次查詢的量值表中")

    def part_condition(self, node: Call, column: str) -> str | None:
        """這組彙總用到兩個以上部分的列時，node 只計算所屬部分的列：條件（column 為部分的欄）。"""
        if len(self._agg_parts) < 2:
            return None
        index = self.part_of(node)
        return None if index is None else f"{column} = {index}"

    @contextmanager
    def _parts_scope(self, nodes: list[Call]) -> Iterator[str]:
        """量值來自多張資料表：一組彙總只用所屬各部分的列（與 Tableau 各量值表分別彙總再合併相同；
        都不屬於任何量值表時用全部的列）。產生限定部分的條件（全部時為空字串）。"""
        plan = self.model_plan
        if plan is None or len(plan.parts) < 2:
            yield ""
            return
        everything = list(range(len(plan.parts)))
        scope = sorted({i for n in nodes if (i := self.part_of(n)) is not None}) or everything
        saved = self._agg_parts
        self._agg_parts = scope
        try:
            if scope == everything:
                yield ""
            elif len(scope) == 1:
                yield f"{ident(PART_COL)} = {scope[0]}"
            else:
                yield f"{ident(PART_COL)} IN ({', '.join(str(i) for i in scope)})"
        finally:
            self._agg_parts = saved

    def _domain_marks(
        self, agg_name: str, agg_cols: dict[Call, str], keys: list[str], dims: list[Dim]
    ) -> str:
        """Relationship 模型：維度表中沒有量值的成員也是標記（量值為 Null），與 Tableau 另以維度查詢
        補上成員相同。成員套用同樣的列層級篩選（直接套用，欄位都在維度表中）。"""
        plan = self.model_plan
        assert plan is not None and plan.domain is not None
        base = self.cte(6, "dom_base", plan.domain)
        conds = [c for c in self._domain_conds if c]
        cols = [f"{self.dim_sql(d.use)} AS {ident(k)}" for d, k in zip(dims, keys, strict=True)]
        dom = self.cte(6, "dom", f"SELECT DISTINCT {', '.join(cols)} FROM {base}{_where(conds)}")
        same = " AND ".join(f"a.{ident(k)} IS NOT DISTINCT FROM d.{ident(k)}" for k in keys)
        picks = [f"d.{ident(k)}" for k in keys] + [f"NULL AS {ident(c)}" for c in agg_cols.values()]
        return self.cte(
            6,
            "agg_dom",
            f"SELECT * FROM {agg_name} UNION ALL SELECT {', '.join(picks)} FROM {dom} d "
            f"WHERE NOT EXISTS (SELECT 1 FROM {agg_name} a WHERE {same})",
        )

    def _filter_condition(self, flt: Filter) -> str:
        """列層級篩選的條件；Relationship 模型中只用到其他資料表欄位的篩選以半連接套用
        （量值來自多張資料表時，各部分有自己的半連接）。"""
        condition = self._row_condition(flt)
        plan = self.model_plan
        if plan is None or not plan.parts or not condition:
            return condition
        conds = [
            part.semi[flt.id].render(condition) if flt.id in part.semi else condition
            for part in plan.parts
        ]
        if len(set(conds)) == 1:
            return conds[0]
        column = ident(PART_COL)
        return "(" + " OR ".join(f"({column} = {i} AND ({c}))" for i, c in enumerate(conds)) + ")"

    def _covered(self, uses: list[FieldUse], refs: tuple[Node, ...] = ()) -> set[str]:
        """彙總層級中直接以原值分組的實體欄位（不含日期衍生、計算欄位與 LOD 維度中的運算式）。"""
        covered = {
            u.field
            for u in uses
            if u.date is None
            and self.fields[u.field].column is not None
            and self.fields[u.field].calc is None
        }
        for ref in refs:
            if not isinstance(ref, FieldRef):
                continue
            target = self._resolve(ref)
            raw = target.kind == "field" and target.formula is None
            if raw and self.fields[target.id].column is not None:
                covered.add(target.id)
        return covered

    def _guard(self, covered: set[str], what: str, nodes: list[Call]) -> None:
        """在某個層級彙總 nodes：層級沒有涵蓋某個分支用到的欄位時，量值表的列可能因一對多而重複計算，
        執行前檢查該分支（見 tmig.sqlgen.model）。不受重複影響的彙總（MIN、MAX、COUNTD、ATTR）、
        只含 LOD 或常值的彙總（不屬於任何量值表），以及分支只用到關聯欄位本身時，不需要檢查。"""
        plan = self.model_plan
        if plan is None:
            return
        parts = sorted(
            {
                i
                for node in nodes
                if node.name not in DUPLICATE_SAFE and (i := self.part_of(node)) is not None
            }
        )
        for p in parts:
            for i, branch in enumerate(plan.parts[p].branches):
                if set(branch.needed) <= covered:
                    continue
                if branch.equality and set(branch.needed) <= set(branch.keys):
                    continue  # 只用到關聯欄位本身：一個關聯值只對應一組欄位值
                if not branch.equality:
                    raise QueryError(
                        f"Relationship 模型中「{self._branch_names(branch)}」的關聯條件不是等號，"
                        f"{what}的層級不含它的欄位時尚不支援",
                        unsupported=True,
                    )
                whats = self._guards.setdefault((p, i), [])
                if what not in whats:
                    whats.append(what)

    def _branch_names(self, branch: Branch) -> str:
        model = self.source.model
        assert model is not None
        captions = {t.id: t.caption for t in model.tables}
        return "、".join(captions[t] for t in branch.tables)

    def _guard_list(self) -> list[Guard]:
        plan = self.model_plan
        if plan is None or not self._guards:
            return []
        guards: list[Guard] = []
        model = self.source.model
        assert model is not None
        captions = {t.id: t.caption for t in model.tables}
        for (p, i), whats in sorted(self._guards.items()):
            part = plan.parts[p]
            measure = captions[part.table]
            branch = part.branches[i]
            names = "、".join(self.fields[f].caption for f in branch.needed)
            guards.append(
                Guard(
                    sql=branch.guard_sql(),
                    message=(
                        f"Relationship 模型中「{self._branch_names(branch)}」對「{measure}」不是多對一"
                        f"（同一個關聯值對應多組 {names}）：{'、'.join(whats)}的層級不含這些欄位，"
                        "量值會重複計算，尚不支援"
                    ),
                )
            )
        return guards

    def collect(
        self, node: Node, usage: Usage, inside: bool = False, owner: str | None = None
    ) -> None:
        """公式用到的實體欄位與邏輯資料表；inside 為是否在彙總之中（量值所在的資料表），
        owner 為該彙總所屬的量值表（見 owner_of）。"""
        if isinstance(node, FieldRef):
            target = self._resolve(node)
            if target.kind != "field":
                return
            data_field = self.fields[target.id]
            if data_field.set is not None and data_field.calc is None:
                # Top N 與條件集合：層級的欄位（用在彙總中時與直接用到層級的欄位相同）；
                # 排名依據（條件）另外彙總
                for use in data_field.set.levels:
                    self.collect(self.field_node(use.field), usage, inside, owner)
                rule = data_field.set.rule
                if rule.kind != "members":
                    self.collect(
                        parse_formula(rule.by if rule.kind == "top_n" else rule.condition), usage
                    )
                return
            if data_field.column is None and data_field.table is not None:
                # 邏輯資料表的筆數欄位：SUM 即該資料表的列數
                usage.add_table(data_field.table, inside)
            if target.formula is not None:
                self.collect(self.analyzer.calc_ast(target), usage, inside, owner)
            elif data_field.column is not None:
                usage.add_field(data_field, inside, owner)
            return
        if isinstance(node, Lod):
            # LOD 的維度在列層級；運算式自己含彙總
            for dim in node.dims:
                self.collect(dim, usage)
            self.collect(node.expr, usage)
            return
        if isinstance(node, Call) and not inside:
            spec = lookup(node.name, len(node.args))
            if spec is not None and spec.kind == "aggregate":
                inside, owner = True, self.owner_of(node)
        for item in children(node):
            self.collect(item, usage, inside, owner)

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
        if tc.type == "percentile":
            direction = str(tc.options.get("direction", "asc"))
            return Call(0, "RANK_PERCENTILE", (base, LiteralNode(0, "string", direction)))
        if tc.type in ("difference", "percent_difference", "percent_from"):
            return self._difference(tc, base)
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

    def _difference(self, tc: TableCalc, base: Node) -> Node:
        """差異、差異百分比與百分比（Percent From）：與 Tableau 轉成計算欄位時相同，
        ZN(x) - LOOKUP(ZN(x), -1)、差異百分比再除以 ABS(LOOKUP(...))、百分比為 ZN(x) / LOOKUP(...)。

        比較對象：相對位置（offset）、第一個（LOOKUP(ZN(x), FIRST())）、最後一個，或定址欄位的某個成員
        （WINDOW_MAX(IF ATTR(欄位) = 成員 THEN ZN(x) END)）。複合成長率（compounded）：
        POWER(ZN(x) / 比較對象, ZN(1 / 距離)) - 1，距離為與比較對象相隔的標記數。"""
        zn = Call(0, "ZN", (base,))
        relative_to = tc.options.get("relative_to")
        distance: Node
        if relative_to in ("first", "last"):
            position = Call(0, "FIRST" if relative_to == "first" else "LAST", ())
            other: Node = Call(0, "LOOKUP", (zn, position))
            distance = Unary(0, "-", position)
        elif relative_to == "member":
            key = str(tc.options.get("member_field"))
            target = next((u for u in self._view_uses() if use_key(u) == key), None)
            if target is None:
                raise QueryError(f"差異計算的比較欄位 {key} 不在檢視中")
            member = (
                self._parameter_node(str(tc.options["parameter"]))
                if "parameter" in tc.options
                else _literal_node(tc.options.get("member"), self.dim_datatype(target))
            )
            test = Binary(0, "=", Call(0, "ATTR", (self.dim_node(target),)), member)
            other = Call(0, "WINDOW_MAX", (If(0, ((test, zn),), None),))
            at = Call(0, "WINDOW_MAX", (If(0, ((test, Call(0, "INDEX", ())),), None),))
            distance = Binary(0, "-", Call(0, "INDEX", ()), at)
        elif "parameter" in tc.options:
            # 相對位置由參數決定（例如與前 N 期比較）
            position = self._parameter_node(str(tc.options["parameter"]))
            other = Call(0, "LOOKUP", (zn, position))
            distance = Call(0, "ABS", (position,))
        else:
            offset = tc.options.get("offset", -1)
            assert isinstance(offset, int)
            other = Call(0, "LOOKUP", (zn, LiteralNode(0, "integer", offset)))
            distance = LiteralNode(0, "integer", abs(offset))
        if tc.options.get("compounded"):
            ratio = Binary(0, "/", zn, other)
            exponent = Call(0, "ZN", (Binary(0, "/", LiteralNode(0, "integer", 1), distance),))
            return Binary(0, "-", Call(0, "POWER", (ratio, exponent)), LiteralNode(0, "integer", 1))
        if tc.type == "percent_from":
            return Binary(0, "/", zn, other)
        diff = Binary(0, "-", zn, other)
        if tc.type == "difference":
            return diff
        return Binary(0, "/", diff, Call(0, "ABS", (other,)))

    def _parameter_node(self, ref: str) -> Node:
        """[Parameters].[名稱] → 公式的參數參照（查詢時代入目前的值）。"""
        try:
            return parse_formula(ref)
        except FormulaSyntaxError as exc:
            raise QueryError(f"參數參照 {ref} 無法解析：{exc}") from exc

    def _view_uses(self) -> list[FieldUse]:
        ws = self.worksheet
        uses = [*ws.rows, *ws.columns, *ws.marks.uses()]
        for pane in ws.panes:
            uses += pane.marks.uses()
        return uses

    def dim_node(self, use: FieldUse) -> Node:
        """維度（含日期部分、截斷）的運算式語法樹。"""
        ref = self.field_node(use.field)
        if use.date is None:
            return ref
        unit = LiteralNode(0, "string", use.date.unit)
        name = "DATEPART" if use.date.kind == "part" else "DATETRUNC"
        return Call(0, name, (unit, ref))

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

    def row_conditions(self, filters: list[Filter]) -> list[str]:
        """不經運算順序、直接逐列套用的篩選條件（篩選控制項的可選值、萃取篩選）。
        彙總條件與 Top N 需要先算出成員，這裡列為不支援。"""
        for flt in filters:
            if isinstance(flt, TopNFilter):
                raise QueryError(f"Top N 篩選 {flt.id} 不能逐列套用", unsupported=True)
            if isinstance(flt, ConditionFilter):
                node = self._condition_node(flt)
                if self.analyzer.analyze(node).level == "agg":
                    raise QueryError(f"彙總條件的篩選 {flt.id} 不能逐列套用", unsupported=True)
                self.cond_nodes[flt.id] = node
        return [self._row_condition(f) for f in filters]

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

    def _attach_sets(self, source: str, stage: int) -> str:
        """Top N 與條件集合：以內容篩選後的資料決定成員，每一列加上是否在集合中（TRUE／FALSE）。"""
        if not self.sets:
            return source
        columns: list[str] = []
        for field_id, col in self.sets.items():
            data_field = self.fields[field_id]
            columns.append(f"{self._set_member(data_field, source, stage)} AS {ident(col)}")
            self.note(stage, f"集合 {field_id}（{data_field.caption}）")
        name = self.cte(
            stage, self.name("sets_ext"), f"SELECT *, {', '.join(columns)} FROM {source}"
        )
        self.attached_sets.update(self.sets)
        return name

    def _set_member(self, data_field: DataField, source: str, stage: int) -> str:
        """集合的成員條件：與 Top N、彙總條件篩選相同的計算（集合沒有篩選的階段，以內容篩選後的資料計算）。"""
        assert data_field.set is not None
        rule = data_field.set.rule
        use = data_field.set.levels[0]
        if rule.kind == "top_n":
            top = TopNFilter(
                kind="top_n",
                id=data_field.id,
                field=use,
                stage="top_n",
                direction=rule.direction,
                count=rule.count,
                by=rule.by,
                mode=rule.mode,
                values=rule.values,
                source_ref=data_field.source_ref,
            )
            return self._top_n(top, self._top_by(top), source, stage)
        if rule.kind != "condition":
            raise QueryError(f"集合「{data_field.caption}」的成員應以公式計算")
        cond = ConditionFilter(
            kind="condition",
            id=data_field.id,
            field=use,
            stage="dimension",
            condition=rule.condition,
            mode=rule.mode,
            values=rule.values,
            source_ref=data_field.source_ref,
        )
        self.cond_nodes[cond.id] = self._condition_node(cond)
        if self.analyzer.analyze(self.cond_nodes[cond.id]).level != "agg":
            raise QueryError(f"集合「{data_field.caption}」的條件必須是彙總")
        return self._member_set(cond, {}, source, stage)

    def _member_set(self, flt: Filter, top_nodes: dict[str, Node], source: str, stage: int) -> str:
        if isinstance(flt, TopNFilter):
            return self._top_n(flt, top_nodes[flt.id], source, stage)
        assert isinstance(flt, ConditionFilter)
        node = self.cond_nodes[flt.id]
        nodes = self.aggregates_in(node)
        self._guard(self._covered([flt.field]), "彙總條件篩選", nodes)
        dim = self.dim_sql(flt.field)
        agg_name, agg_cols, keys = self.aggregate("cond_agg", nodes, [dim], source, stage, None)
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
        nodes = self.aggregates_in(by)
        self._guard(self._covered([flt.field]), "Top N 篩選", nodes)
        dim = self.dim_sql(flt.field)
        selected = _members(dim, flt.mode, flt.values, self.dim_datatype(flt.field))
        if selected:
            # 先選取的成員（篩選對話框的「一般」頁）中再排名
            source = self.cte(
                stage, self.name("topn_src"), f"SELECT * FROM {source} WHERE {selected}"
            )
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

    def _lod_levels(
        self, spec: LodSpec, view_dims: list[Dim] | None, compiler: ExprCompiler
    ) -> list[tuple[str, str | None]]:
        """LOD 的維度：SQL 與欄位 id（EXCLUDE 依欄位 id 去掉）。INCLUDE／EXCLUDE 相對於外層 LOD 的維度，
        沒有外層時相對於檢視的維度。"""
        lod = spec.node
        if lod.kind == "fixed":
            return [
                (self._compile(lambda d=d: compiler.row(d)), self._dim_field(d)) for d in lod.dims
            ]
        if spec.outer is not None:
            base = self._lod_levels(spec.outer, view_dims, compiler)
        elif view_dims is None:
            raise QueryError("INCLUDE／EXCLUDE LOD 只能用在檢視的量值", unsupported=True)
        else:
            base = [(self.dim_sql(d.use, compiler), d.use.field) for d in view_dims]
        if lod.kind == "include":
            levels = list(base)
            for dim in lod.dims:
                sql = self._compile(lambda dim=dim: compiler.row(dim))
                if all(sql != known for known, _ in levels):
                    levels.append((sql, self._dim_field(dim)))
            return levels
        # EXCLUDE：欄位依 id，運算式依 SQL 對應檢視（或外層 LOD）的維度；不在其中的維度沒有作用
        excluded = {self._dim_field(d) for d in lod.dims} - {None}
        expressions = {
            self._compile(lambda d=d: compiler.row(d))
            for d in lod.dims
            if self._dim_field(d) is None
        }
        return [(sql, fid) for sql, fid in base if fid not in excluded and sql not in expressions]

    def _dim_field(self, node: Node) -> str | None:
        return self._resolve(node).id if isinstance(node, FieldRef) else None

    def _lod_dims(
        self, spec: LodSpec, view_dims: list[Dim] | None, compiler: ExprCompiler
    ) -> list[str]:
        return [sql for sql, _ in self._lod_levels(spec, view_dims, compiler)]

    def _lod_covered(self, spec: LodSpec, view_dims: list[Dim] | None) -> set[str]:
        lod = spec.node
        if lod.kind == "fixed":
            return self._covered([], lod.dims)
        if spec.outer is not None:
            base = self._lod_covered(spec.outer, view_dims)
        elif view_dims is None:
            return self._covered([], lod.dims)
        else:
            base = self._covered([d.use for d in view_dims])
        if lod.kind == "include":
            return base | self._covered([], lod.dims)
        return base - {self._dim_field(d) for d in lod.dims}

    def _attach(
        self, specs: list[LodSpec], source: str, name: str, stage: int, view_dims: list[Dim] | None
    ) -> str:
        """計算一組 LOD，併回每一列。巢狀時分層：內層先併回，外層再以併好的資料計算。"""
        layers: list[list[LodSpec]] = []
        depth: dict[str, int] = {}
        for spec in specs:  # 內層在外層之前
            inner = [depth[s.col] for s in spec.inner.values() if s.col in depth]
            depth[spec.col] = max(inner, default=-1) + 1
            if depth[spec.col] == len(layers):
                layers.append([])
            layers[depth[spec.col]].append(spec)
        for i, layer in enumerate(layers):
            source = self._attach_layer(
                layer, source, name if i == 0 else f"{name}_{i}", stage, view_dims
            )
        return source

    def _attach_layer(
        self, specs: list[LodSpec], source: str, name: str, stage: int, view_dims: list[Dim] | None
    ) -> str:
        joins: list[str] = []
        selects = ["r.*"]
        row_r = self.compiler(_RowEnv(self, "r"))
        for spec in specs:
            kind = {"fixed": "FIXED", "include": "INCLUDE", "exclude": "EXCLUDE"}[spec.node.kind]
            self._guard(
                self._lod_covered(spec, view_dims),
                f"{kind} LOD",
                self.aggregates_in(spec.node.expr),
            )
            dims = self._lod_dims(spec, view_dims, self.row)
            lod_name, keys = self._lod_values(spec, dims, source, stage, view_dims)
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
            self.note(stage, f"{kind} LOD → {spec.col}")
        self.cte(stage, name, f"SELECT {', '.join(selects)} FROM {source} r {' '.join(joins)}")
        self.attached.update(s.col for s in specs)
        return name

    def _lod_values(
        self,
        spec: LodSpec,
        dims: list[str],
        source: str,
        stage: int,
        view_dims: list[Dim] | None,
    ) -> tuple[str, list[str]]:
        """LOD 在它的維度上的值（CTE 名稱與維度的欄名）。運算式中有內層 LOD 時與檢視的彙總相同：
        內層的值只含在彙總中時，每個（外層維度、內層維度）的組合算一次，不是每一列算一次。"""
        if not spec.inner:
            keys = [f"__d{i}" for i in range(len(dims))]
            with self._parts_scope(self.aggregates_in(spec.node.expr)) as only:
                inner = self._compile(lambda: self.row.agg(spec.node.expr))
            cols = [f"{sql} AS {ident(k)}" for sql, k in zip(dims, keys, strict=True)]
            where = f" WHERE {only}" if only else ""
            group = f" GROUP BY {', '.join(str(i + 1) for i in range(len(dims)))}" if dims else ""
            cols.append(f"{inner} AS {ident(VALUE_COL)}")
            lod_name = self.name("lod")
            self.cte(stage, lod_name, f"SELECT {', '.join(cols)} FROM {source}{where}{group}")
            return lod_name, keys
        with self._lod_scope(spec):
            nodes = self.aggregates_in(spec.node.expr)
            agg_name, agg_cols, keys = self.aggregate(
                "lod_agg", nodes, dims, source, stage, view_dims
            )
            post = self.compiler(_PostEnv(self, agg_cols))
            value = self._compile(lambda: post.post(spec.node.expr))
        picks = [ident(k) for k in keys] + [f"{value} AS {ident(VALUE_COL)}"]
        lod_name = self.name("lod")
        self.cte(stage, lod_name, f"SELECT {', '.join(picks)} FROM {agg_name}")
        return lod_name, keys

    # ------------------------------------------------------------ 彙總

    def aggregate(
        self,
        prefix: str,
        nodes: list[Call],
        level: list[str],
        source: str,
        stage: int,
        view_dims: list[Dim] | None,
        pushdown: bool | None = None,
    ) -> tuple[str, dict[Call, str], list[str]]:
        """在指定層級彙總。回傳 CTE 名稱、彙總節點 → 欄名、層級鍵欄名。

        Relationship 模型中量值來自多張資料表時，只用這些彙總所屬各部分的列（見 _parts_scope）。"""
        with self._parts_scope(nodes) as only:
            return self._aggregate(prefix, nodes, level, source, stage, view_dims, pushdown, only)

    def _aggregate(
        self,
        prefix: str,
        nodes: list[Call],
        level: list[str],
        source: str,
        stage: int,
        view_dims: list[Dim] | None,
        pushdown: bool | None,
        only: str,
    ) -> tuple[str, dict[Call, str], list[str]]:
        keys = [f"__k{i}" for i in range(len(level))]
        cols: dict[Call, str] = {}
        row_nodes = [n for n in nodes if not self._lod_only(n)]
        combo_nodes = [n for n in nodes if self._lod_only(n)]
        where = f" WHERE {only}" if only else ""
        selects = [f"{sql} AS {ident(k)}" for sql, k in zip(level, keys, strict=True)]
        for node in row_nodes:
            col = f"__a{len(cols)}"
            selects.append(f"{self._compile(lambda node=node: self.row.agg(node))} AS {ident(col)}")
            cols[self.canon(node)] = col  # type: ignore[index]
        if not selects:
            selects.append(f"count(*) AS {ident('__n')}")
        group = f" GROUP BY {', '.join(str(i + 1) for i in range(len(level)))}" if level else ""
        base = self.cte(
            stage,
            self.name(prefix),
            f"SELECT {', '.join(selects)} FROM {source}{where}{group}",
            pushdown,
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
                stage,
                self.name("combos"),
                f"SELECT DISTINCT {', '.join(combo_cols)} FROM {source}{where}",
                pushdown,
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
                pushdown,
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
            pushdown,
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
            self._guard(self._covered([d.use]), "依欄位排序", nodes)
            # 排序值依來源資料計算，不含 Table Calculation：可下推
            agg_name, agg_cols, keys = self.aggregate(
                "sort_agg", nodes, [self.dim_sql(d.use)], rows_cte, 7, None, pushdown=True
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
                pushdown=True,
            )
            col = f"__sort{len(result)}"
            direction = "DESC" if sort.order == "desc" else "ASC"
            result[d.key] = (col, f"{ident(col)} {direction} NULLS LAST")
            self._sort_ctes[d.key] = (name, col)
        return result

    def _column_headers(self, dims: list[Dim]) -> list[Dim]:
        """欄標題的維度（欄架上的離散維度），列架上也有離散維度時才回傳。資料依列優先排序，
        欄的順序因此另外以名次輸出（圖表的欄依它排列，第一列沒有的欄不會排到後面）；
        欄架上有 Measure Names 或離散的量值時不輸出（依資料的順序）。"""
        ws = self.worksheet
        by_key = {d.key: d for d in dims}
        rows = [u for u in ws.rows if not u.continuous and use_key(u) in by_key]
        cols = [u for u in ws.columns if not u.continuous]
        if not rows or not cols or any(use_key(u) not in by_key for u in cols):
            return []
        return [by_key[use_key(u)] for u in cols]

    def _natural_order(self, d: Dim, sort: Sort | None) -> str:
        col = ident(d.col)
        if sort is not None and sort.kind == "manual" and sort.manual_values:
            datatype = self.dim_datatype(d.use)
            cases = " ".join(
                f"WHEN {col} IS NOT DISTINCT FROM {literal(v, datatype)} THEN {i}"
                for i, v in enumerate(sort.manual_values)
            )
            return f"(CASE {cases} ELSE {len(sort.manual_values)} END), {col} ASC NULLS LAST"
        descending = sort is not None and sort.kind == "alphabetic" and sort.order == "desc"
        if self.fields[d.use.field].set is not None and d.use.agg is None:
            descending = not descending  # 集合的 IN/OUT：In（TRUE）在前，與 Tableau 相同
        return f"{col} {'DESC' if descending else 'ASC'} NULLS LAST"

    def _window(
        self,
        tc: TableCalc | NestedTableCalc,
        dims: list[Dim],
        sorts: dict[str, Sort],
        sort_cols: dict[str, tuple[str, str]],
    ) -> _Window:
        addressing: list[Dim] = []
        if tc.instances:
            # 只定址同一欄位的部分實例（例如窗格的計算只定址月、不含年）
            for key in (use_key(u) for u in tc.instances):
                addressing += [d for d in dims if d.key == key and d not in addressing]
        else:
            for field_id in tc.addressing:
                addressing += [d for d in dims if d.use.field == field_id and d not in addressing]
        if isinstance(tc, TableCalc):
            if tc.level is not None:
                # 計算層級：定址中比它更內層的維度改為分割（例如在年的層級比較，每一季各自與前一年比）
                hits = [i for i, d in enumerate(addressing) if _same_level(d, tc.level)]
                if hits:
                    addressing = addressing[: hits[-1] + 1]
            if tc.restart is not None:
                # 重新開始間隔：定址中它與它之前的維度改為分割（例如每年重新累計）
                hits = [i for i, d in enumerate(addressing) if _same_level(d, tc.restart)]
                if hits:
                    addressing = addressing[hits[-1] + 1 :]
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
                7,
                "sorted",
                f"SELECT {', '.join(selects)} FROM {marks} m {' '.join(joins)}",
                pushdown=True,
            )
        if not tc_measures:
            return base
        sorts = {s.field: s for s in self.worksheet.sorts}
        selects = ["m.*"]
        joins: list[str] = []
        expressions: list[str] = []
        recursive: list[tuple[Measure, _PostEnv, _Window]] = []
        layers: dict[int, list[str]] = {}
        marks_rows: str | None = None
        joined: dict[tuple[Node, tuple[str, ...]], str] = {}

        def totals_for(expr: Node, window: _Window, skip: dict[str, str]) -> dict[Node, str]:
            """這段運算式中的 TOTAL：依計算方向的分割在來源資料另外計算，併入後以欄位引用。"""
            nonlocal marks_rows
            found: dict[Node, str] = {}
            for node in self._totals_in(expr, skip):
                key = self.canon(node)
                slot = (key, tuple(window.partition))
                if slot not in joined:
                    if marks_rows is None:
                        marks_rows = self._marks_rows(dims, marks, rows_cte)
                    partition_dims = [d for d in dims if ident(d.col) in window.partition]
                    total_col, join = self._total(node, partition_dims, marks_rows, len(joins))
                    joins.append(join)
                    selects.append(f"t{len(joins) - 1}.{ident(VALUE_COL)} AS {ident(total_col)}")
                    joined[slot] = total_col
                found[key] = joined[slot]
            return found

        for m in tc_measures:
            assert m.table_calc is not None
            tc = m.table_calc
            window = self._window(tc, dims, sorts, sort_cols)
            nested_windows = {n.field: self._window(n, dims, sorts, sort_cols) for n in tc.nested}
            items: list[_Nested] = []
            root = m.use.field if tc.type == "formula" else None
            previous = self._has_previous(m.node)
            # PREVIOUS_VALUE 以遞迴的 CTE 逐列計算：其中的其他 Table Calculation 一律先算成欄位
            fields, calls, _ = self._prepare(
                m.node, window, root, nested_windows, items, inside=previous
            )
            for item in items:
                env = _PostEnv(
                    self,
                    agg_cols,
                    item.window,
                    totals_for(item.node, item.window, item.fields),
                    item.fields,
                    item.calls,
                )
                sql = self._compile(lambda item=item, env=env: self.compiler(env).post(item.node))
                layers.setdefault(item.depth, []).append(f"{sql} AS {ident(item.col)}")
            env = _PostEnv(
                self, agg_cols, window, totals_for(m.node, window, fields), fields, calls
            )
            if previous:
                recursive.append((m, env, window))
            else:
                sql = self._compile(lambda m=m, env=env: self.compiler(env).post(m.node))
                expressions.append(f"{sql} AS {ident(m.col)}")
            self.note(
                7,
                f"{m.key}：{tc.type}，計算方向 {tc.preset}"
                + ("（推定）" if tc.inferred else "")
                + f"，分割 {len(window.partition)} 個維度、定址 {len(dims) - len(window.partition)} 個維度"
                + (f"；巢狀的部分分 {max(i.depth for i in items) + 1} 層先算" if items else ""),
            )
        if joins:
            # 視窗函式不能直接引用同一層 SELECT 剛併入的欄位，先併入合計再計算
            base = self.cte(
                7,
                "totals",
                f"SELECT {', '.join(selects)} FROM {base} m {' '.join(joins)}",
                pushdown=True,
            )
        for depth in sorted(layers):
            # 視窗函式不能巢狀：內層先算成欄位，外層再引用
            base = self.cte(
                7, self.name("nested"), f"SELECT m.*, {', '.join(layers[depth])} FROM {base} m"
            )
        for m, env, window in recursive:
            base = self._previous_value(base, m, env, window)
        if not expressions:
            return base
        return self.cte(7, "tc", f"SELECT m.*, {', '.join(expressions)} FROM {base} m")

    def _has_previous(self, node: Node) -> bool:
        return any(isinstance(n, Call) and n.name == "PREVIOUS_VALUE" for n in self.expand(node))

    def _previous_value(self, base: str, m: Measure, env: _PostEnv, window: _Window) -> str:
        """PREVIOUS_VALUE：每個分割依計算方向的順序逐列計算（遞迴的 CTE）。第一列的 PREVIOUS_VALUE(x) 為 x，
        之後為這個計算前一列的值；其中的其他 Table Calculation 已先算成欄位。"""
        rn = ident("__pv_rn")
        partition = f"PARTITION BY {', '.join(window.partition)}" if window.partition else ""
        order = f"ORDER BY {', '.join(window.order)}" if window.order else ""
        numbered = self.cte(
            7,
            self.name("pv_rows"),
            f"SELECT m.*, row_number() OVER ({partition} {order}) AS {rn} FROM {base} m",
        )

        def compiled(previous: Callable[[Callable[[int], str]], str]) -> str:
            self._previous = previous
            try:
                return self._compile(lambda: self.compiler(env).post(m.node))
            finally:
                self._previous = None

        first = compiled(lambda arg: arg(0))
        step = compiled(lambda arg: f"r.{ident('__pv')}")
        name = self.name("pv")
        keys = [(k, ident(f"__pvk{i}")) for i, k in enumerate(window.partition)]
        anchor_cols = ", ".join([*(f"{k} AS {a}" for k, a in keys), rn])
        step_cols = ", ".join([*(f"m.{k}" for k, _ in keys), f"m.{rn}"])
        step_on = " AND ".join(
            [f"m.{rn} = r.{rn} + 1", *(f"m.{k} IS NOT DISTINCT FROM r.{a}" for k, a in keys)]
        )
        self.cte(
            7,
            name,
            f"SELECT {anchor_cols}, {first} AS {ident('__pv')} FROM {numbered} WHERE {rn} = 1 "
            f"UNION ALL SELECT {step_cols}, {step} FROM {numbered} m JOIN {name} r ON {step_on}",
        )
        self.recursive = True
        join_on = " AND ".join(
            [f"m.{rn} = r.{rn}", *(f"m.{k} IS NOT DISTINCT FROM r.{a}" for k, a in keys)]
        )
        return self.cte(
            7,
            self.name("pv_join"),
            f"SELECT m.* EXCLUDE ({rn}), r.{ident('__pv')} AS {ident(m.col)} "
            f"FROM {numbered} m LEFT JOIN {name} r ON {join_on}",
        )

    def _prepare(
        self,
        expr: Node,
        window: _Window,
        root: str | None,
        nested_windows: dict[str, _Window],
        items: list[_Nested],
        inside: bool = False,
    ) -> tuple[dict[str, str], dict[Node, str], int]:
        """找出運算式中必須先算好的巢狀部分，加入 items（依計算順序）。

        - 被參照的 Table Calculation 計算欄位：依它的巢狀計算方向（沒有設定時與外層相同）先算成欄位
        - Table Calculation 函式參數中的 Table Calculation 函式：依同一個計算方向先算成欄位
        回傳 (先算好的計算欄位, 先算好的函式, 需要的層數)。root 是工作表上這個欄位本身（不先算）。
        """
        fields: dict[str, str] = {}
        calls: dict[Node, str] = {}
        depth = 0

        def add(node: Node, item_window: _Window, item_root: str | None) -> str:
            nonlocal depth
            sub_fields, sub_calls, sub_depth = self._prepare(
                node, item_window, item_root, nested_windows, items
            )
            col = self.name("__nested")
            items.append(_Nested(col, node, item_window, sub_depth, sub_fields, sub_calls))
            depth = max(depth, sub_depth + 1)
            return col

        def visit(node: Node, inside: bool) -> None:
            if isinstance(node, FieldRef):
                target = self._resolve(node)
                if target.kind != "field" or target.formula is None:
                    return
                ast = self.analyzer.calc_ast(target)
                if target.id != root and self.level(target.id) == "tc":
                    if target.id not in fields:
                        item_window = nested_windows.get(target.id, window)
                        fields[target.id] = add(ast, item_window, target.id)
                    return
                visit(ast, inside)
                return
            if isinstance(node, Lod):
                return
            if isinstance(node, Call):
                spec = lookup(node.name, len(node.args))
                if spec is not None and spec.kind == "aggregate":
                    return
                if spec is not None and spec.kind == "table_calc":
                    if node.name == "TOTAL":
                        return
                    if node.name == "PREVIOUS_VALUE":
                        for arg in node.args:
                            visit(arg, True)
                        return
                    if inside:
                        key = self.canon(node)
                        if key not in calls:
                            calls[key] = add(node, window, root)
                        return
                    for arg in node.args:
                        visit(arg, True)
                    return
            for sub in children(node):
                visit(sub, inside)

        visit(expr, inside)
        return fields, calls, depth

    def _totals_in(self, expr: Node, skip: dict[str, str]) -> list[Call]:
        """運算式中的 TOTAL（走進計算欄位，但不走進已先算好的巢狀計算欄位與 LOD）。"""
        found: list[Call] = []

        def visit(node: Node) -> None:
            if isinstance(node, FieldRef):
                target = self._resolve(node)
                if target.kind == "field" and target.formula is not None and target.id not in skip:
                    visit(self.analyzer.calc_ast(target))
                return
            if isinstance(node, Lod):
                return
            if isinstance(node, Call) and node.name == "TOTAL":
                found.append(node)
                return
            for sub in children(node):
                visit(sub)

        visit(expr)
        return found

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
            pushdown=True,
        )

    def _total(self, node: Call, partition: list[Dim], rows: str, index: int) -> tuple[str, str]:
        expr = node.args[0]
        nodes = self.aggregates_in(expr)
        self._guard(self._covered([d.use for d in partition]), "TOTAL", nodes)
        level = [self.dim_sql(d.use) for d in partition]
        # TOTAL 依來源資料計算（不是加總各標記），不含視窗函式：可下推
        agg_name, agg_cols, keys = self.aggregate(
            "total_agg", nodes, level, rows, 7, None, pushdown=True
        )
        value = self._compile(lambda: self.compiler(_PostEnv(self, agg_cols)).post(expr))
        key_cols = [f"{ident(k)}" for k in keys]
        name = self.name("total")
        key_cols.append(f"{value} AS {ident(VALUE_COL)}")
        self.cte(7, name, f"SELECT {', '.join(key_cols)} FROM {agg_name}", pushdown=True)
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
        inner = self.compiler(_PostEnv(self, env.agg_cols, None, env.totals, env.fields, env.calls))
        name = node.name
        partition = f"PARTITION BY {', '.join(window.partition)}" if window.partition else ""
        order = f"ORDER BY {', '.join(window.order)}" if window.order else ""

        def over(frame: str = "", ordered: bool = True) -> str:
            parts = [partition, order if ordered else "", frame if ordered and order else ""]
            return f"OVER ({' '.join(p for p in parts if p)})"

        def arg(i: int) -> str:
            return self._compile(lambda: inner.post(node.args[i]))

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
            elif all(_static_bound(b) for b in bounds):
                start = _bound(bounds[0], "start")
                end = _bound(bounds[1], "end")
                sql = f"{value} {over(f'ROWS BETWEEN {start} AND {end}')}"
            else:
                sql = self._dynamic_window(value, bounds, over, inner)
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
        if name.startswith("RANK") and name != "RANK_PERCENTILE":
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
        if name == "PREVIOUS_VALUE":
            if self._previous is None:
                raise QueryError(
                    "PREVIOUS_VALUE 只支援在工作表上的 Table Calculation 計算欄位的公式中使用",
                    unsupported=True,
                )
            return self._previous(arg)
        if name == "RANK_PERCENTILE":
            # (不大於自己的值的個數 - 1) / (個數 - 1)；Null 不計入（與 Tableau 文件的例子相同：
            # 6、9、9、14 → 0、0.67、0.67、1）
            value = arg(0)
            direction = "ASC"
            literal_node = node.args[1] if len(node.args) == 2 else None
            if isinstance(literal_node, LiteralNode) and str(literal_node.value).lower() == "desc":
                direction = "DESC"
            parts = [*window.partition, f"(({value}) IS NULL)"]
            rank_partition = f"PARTITION BY {', '.join(parts)}"
            upto = (
                f"count(*) OVER ({rank_partition} ORDER BY {value} {direction} "
                "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
            )
            size = f"count(*) OVER ({rank_partition})"
            return (
                f"(CASE WHEN ({value}) IS NULL THEN NULL "
                f"ELSE CAST({upto} - 1 AS DOUBLE) / NULLIF({size} - 1, 0) END)"
            )
        if name == "LOOKUP":
            offset = node.args[1]
            if isinstance(offset, LiteralNode):
                n = int(offset.value)
                if n == 0:
                    return arg(0)
                fn = "lag" if n < 0 else "lead"
                return f"{fn}({arg(0)}, {abs(n)}) {over()}"
            # 位移為運算式（例如 FIRST()、LAST()、參數）：每一列各自的位移
            n_sql = arg(1)
            value = arg(0)
            return (
                f"(CASE WHEN ({n_sql}) IS NULL THEN NULL WHEN ({n_sql}) <= 0 "
                f"THEN lag({value}, CAST(greatest(-({n_sql}), 0) AS BIGINT)) {over()} "
                f"ELSE lead({value}, CAST(greatest({n_sql}, 0) AS BIGINT)) {over()} END)"
            )
        raise QueryError(f"Table Calculation 函式 {name} 尚不支援", unsupported=True)

    def _dynamic_window(
        self, value: str, bounds: Sequence[Node], over: Callable[[str], str], inner: ExprCompiler
    ) -> str:
        """視窗的起訖為運算式（例如 FIRST() + 1、參數）：依起訖的正負選用 PRECEDING／FOLLOWING 的組合
        （DuckDB 的列數必須為非負，逐列計算）；起點在終點之後時為 Null。"""

        def side(node: Node) -> tuple[str | None, str]:
            # (逐列的位移 SQL，或 None 表示無界)、無界的方向
            if isinstance(node, Call) and node.name in ("FIRST", "LAST") and not node.args:
                return None, node.name
            return self._compile(lambda: inner.post(node)), ""

        (s_sql, s_unbounded), (e_sql, e_unbounded) = side(bounds[0]), side(bounds[1])

        def frame(start_following: bool, end_following: bool) -> str:
            if s_sql is None:
                start = "UNBOUNDED PRECEDING" if s_unbounded == "FIRST" else "UNBOUNDED FOLLOWING"
            elif start_following:
                start = f"CAST(greatest({s_sql}, 0) AS BIGINT) FOLLOWING"
            else:
                start = f"CAST(greatest(-({s_sql}), 0) AS BIGINT) PRECEDING"
            if e_sql is None:
                end = "UNBOUNDED FOLLOWING" if e_unbounded == "LAST" else "UNBOUNDED PRECEDING"
            elif end_following:
                end = f"CAST(greatest({e_sql}, 0) AS BIGINT) FOLLOWING"
            else:
                end = f"CAST(greatest(-({e_sql}), 0) AS BIGINT) PRECEDING"
            return f"{value} {over(f'ROWS BETWEEN {start} AND {end}')}"

        s_neg = "TRUE" if s_sql is None else f"({s_sql}) <= 0"
        e_pos = "TRUE" if e_sql is None else f"({e_sql}) > 0"
        nulls = [f"({x}) IS NULL" for x in (s_sql, e_sql) if x is not None]
        cases = [f"WHEN {' OR '.join(nulls)} THEN NULL"] if nulls else []
        if e_sql is not None:
            cases.append(f"WHEN {s_neg} AND NOT {e_pos} THEN {frame(False, False)}")
        cases.append(f"WHEN {s_neg} AND {e_pos} THEN {frame(False, True)}")
        if s_sql is not None:
            cases.append(f"WHEN NOT {s_neg} AND {e_pos} THEN {frame(True, True)}")
        return f"(CASE {' '.join(cases)} ELSE NULL END)"

    def _integer(self, sql: str, node: Call) -> str:
        if self.datatype(node) == "integer":
            return f"CAST({sql} AS BIGINT)"
        return sql

    # ------------------------------------------------------------ 輸出

    def _render(self, main: str) -> str:
        boundary = "-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行\n"
        lines = ["WITH RECURSIVE" if self.recursive else "WITH"]
        stage_of = {name: n for n, note in self.plan.items() for name in note.ctes}
        current = 0
        pushed = True
        parts: list[str] = []
        for name, body in self.ctes:
            stage = stage_of[name]
            header = ""
            if pushed and not self.pushdown[name]:
                header += boundary
                pushed = False
            if stage != current:
                header += f"-- 第 {stage} 段：{STAGES[stage]}\n"
                current = stage
            parts.append(f"{header}{ident(name)} AS (\n  {body}\n)")
        lines.append(",\n".join(parts))
        tail = boundary if pushed else ""
        return "\n".join(lines) + f"\n{tail}-- 第 8 段：{STAGES[8]}與輸出\n{main}\n"


# ---------------------------------------------------------------- 條件


def _same_level(d: Dim, use: FieldUse) -> bool:
    """計算層級、重新開始間隔的欄位：欄位名稱比對所有實例，欄位實例（例如年）只比對這個實例。"""
    if use.date is None:
        return d.use.field == use.field
    return d.key == use_key(use)


def _literal_node(value: Scalar, datatype: str) -> Node:
    """查詢時代入的常值（差異計算的比較成員）。"""
    if value is None:
        return LiteralNode(0, "null", None)
    if isinstance(value, bool):
        return LiteralNode(0, "boolean", value)
    if isinstance(value, int) and datatype != "real":
        return LiteralNode(0, "integer", value)
    if isinstance(value, int | float):
        return LiteralNode(0, "real", float(value))
    if datatype in ("date", "datetime"):
        return LiteralNode(0, datatype, str(value))  # type: ignore[arg-type]
    return LiteralNode(0, "string", str(value))


def _noop(flt: Filter) -> bool:
    """沒有條件的篩選（「全部」）：不影響結果。"""
    if isinstance(flt, (CategoricalFilter, TupleFilter)):
        return flt.mode == "all"
    if isinstance(flt, RangeFilter):
        return flt.min is None and flt.max is None and flt.include_null
    return False


def _where(conds: list[str]) -> str:
    conds = [c for c in conds if c]
    return f" WHERE {' AND '.join(conds)}" if conds else ""


def _categorical(value: str, flt: CategoricalFilter, datatype: str) -> str:
    ranges = [(r.start, r.end) for r in flt.ranges]
    return _members(value, flt.mode, flt.values, datatype, ranges)


def _members(
    value: str,
    mode: str,
    values: list[Scalar],
    datatype: str,
    ranges: list[tuple[Scalar, Scalar]] | None = None,
) -> str:
    """成員選取：include 只保留、exclude 排除列出的值（Null 也可以列出）與連續的成員（含頭尾）；
    all 沒有條件。"""
    if mode == "all":
        return ""
    non_null = [v for v in values if v is not None]
    has_null = len(non_null) != len(values)
    items = ", ".join(_lit(v, datatype) for v in non_null)
    parts = [f"{value} IN ({items})"] if non_null else []
    parts += [
        f"({value} >= {_lit(start, datatype)} AND {value} <= {_lit(end, datatype)})"
        for start, end in ranges or []
    ]
    member = " OR ".join(parts) if parts else "FALSE"
    if len(parts) > 1:
        member = f"({member})"
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


def _static_bound(node: Node) -> bool:
    """整數常值或 FIRST()、LAST()：視窗的起訖可以直接寫成 ROWS BETWEEN。"""
    if isinstance(node, Call) and node.name in ("FIRST", "LAST") and not node.args:
        return True
    return isinstance(node, LiteralNode) and node.kind == "integer"


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
    ir: IR,
    datasource_id: str,
    use: FieldUse,
    kind: Literal["values", "range", "frequency"],
    user: Identity | None = None,
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
    query = WorksheetQuery(ir.model_copy(update={"worksheets": [holder]}), "domain", user=user)
    if query.is_measure(use):
        raise QueryError("量值沒有可選值")
    data_field = query.fields[use.field]
    if data_field.set is not None and data_field.calc is None:
        # Top N 與條件集合：可選值一律為 In、Out（成員要以內容篩選後的資料計算）
        if kind != "values":
            raise QueryError("集合只有 In、Out 兩個值", unsupported=True)
        return (
            f"SELECT {ident('value')} FROM (SELECT FALSE AS {ident('value')} "
            f"UNION ALL SELECT TRUE) AS t ORDER BY 1"
        )
    query.register_lods(query.field_node(use.field))
    if query.lods:
        raise QueryError("含 LOD 的欄位尚不支援列出可選值", unsupported=True)
    value = query.dim_sql(use)
    ds_filters = [f for f in datasource.filters if f.stage == "datasource"]
    conds = query.row_conditions(ds_filters)
    source = query.source
    from_sql = source.from_sql
    if source.model is not None:
        # Relationship 模型：這個欄位所在的資料表（資料源篩選用到其他資料表時，取存在的組合）
        usage = Usage()
        query.collect(query.field_node(use.field), usage)
        filters: list[RowFilter] = []
        for flt, cond in zip(ds_filters, conds, strict=True):
            found = Usage()
            for item in flt.fields if isinstance(flt, TupleFilter) else [flt.field]:
                query.collect(query.field_node(item.field), found)
            if flt.id in query.cond_nodes:
                query.collect(query.cond_nodes[flt.id], found)
            if cond:
                filters.append(RowFilter(flt.id, set(found.fields), set(found.tables)))
        # 只有維度：連接後的列都含篩選用到的欄位，篩選直接套用
        from_sql = query.plan_model(usage, filters).from_sql
    table = f"({from_sql}) AS base" if source.projected else from_sql
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


PREVIEW_LIMIT = 1000
"""資料源分頁預覽的列數上限（與 Tableau 資料源分頁的資料格預設相同）。"""


@dataclass
class PreviewColumn:
    """資料源預覽的一欄：實體欄位或列層級的計算欄位。"""

    field: str
    caption: str
    datatype: str
    role: str
    calculated: bool


@dataclass
class PreviewSql:
    """資料源預覽的查詢：前 limit 列（count_sql 為資料源篩選後的總列數）。"""

    sql: str
    count_sql: str
    columns: list[PreviewColumn]
    table: str | None
    """Relationship 模型中預覽的邏輯資料表 id；單一資料表或 join 樹時為 None。"""


def preview_sql(
    ir: IR,
    datasource_id: str,
    table: str | None = None,
    limit: int = PREVIEW_LIMIT,
    user: Identity | None = None,
) -> PreviewSql:
    """資料源分頁的資料預覽（Tableau 資料源分頁下方的資料格）：資料源的原始列與列層級的計算欄位，
    與工作表相同套用資料源篩選與列層級權限（以登入者的身分）。

    Relationship 模型一次預覽一張邏輯資料表（table；省略時為第一張），其他資料表的欄位不列出，
    篩選用到其他資料表時以半連接套用。含 LOD、彙總、Table Calculation 的計算欄位與 Top N、
    條件集合不在預覽中；隱藏的欄位不列出。"""
    datasource = next((ds for ds in ir.datasources if ds.id == datasource_id), None)
    if datasource is None:
        raise QueryError(f"找不到資料源「{datasource_id}」")
    holder = Worksheet(
        id="preview",
        name="preview",
        datasource=datasource_id,
        marks=Marks(type="automatic"),
        source_ref="preview",
    )
    query = WorksheetQuery(ir.model_copy(update={"worksheets": [holder]}), "preview", user=user)
    source = query.source
    chosen: str | None = None
    if source.model is not None:
        ids = [t.id for t in source.model.tables]
        chosen = table or ids[0]
        if chosen not in ids:
            raise QueryError(f"資料源「{datasource.caption}」沒有資料表「{table}」")
    columns: list[PreviewColumn] = []
    values: list[str] = []
    usage = Usage()
    for data_field in datasource.fields:
        if data_field.hidden or (data_field.set is not None and data_field.calc is None):
            continue
        node = query.field_node(data_field.id)
        if data_field.calc is not None:
            try:
                info = query.analyzer.analyze(node)
                lods = query.lods_in(node)
            except (FormulaSemanticError, QueryError):
                continue  # 無法分析的公式：不列在預覽中
            if info.level not in ("row", "const") or info.has_lod or lods:
                continue
        if source.model is not None:
            found = Usage()
            query.collect(node, found)
            if set(found.tables) - {chosen}:
                continue  # 其他資料表的欄位（或跨資料表的計算欄位）
            query.collect(node, usage)
        try:
            value = query.dim_sql(FieldUse(field=data_field.id))
        except QueryError:
            continue  # 尚不支援的函式：不列在預覽中
        values.append(f"{value} AS {ident(f'c{len(values)}')}")
        columns.append(
            PreviewColumn(
                field=data_field.id,
                caption=data_field.caption,
                datatype=data_field.datatype,
                role=data_field.role,
                calculated=data_field.calc is not None,
            )
        )
    if not values:
        raise QueryError(f"資料源「{datasource.caption}」沒有可以預覽的欄位")
    ds_filters = [f for f in datasource.filters if f.stage == "datasource"]
    conds = query.row_conditions(ds_filters)
    from_sql = source.from_sql
    if source.model is not None:
        assert chosen is not None
        usage.tables.setdefault(chosen, False)
        filters: list[RowFilter] = []
        for flt, cond in zip(ds_filters, conds, strict=True):
            found = Usage()
            for item in flt.fields if isinstance(flt, TupleFilter) else [flt.field]:
                query.collect(query.field_node(item.field), found)
            if flt.id in query.cond_nodes:
                query.collect(query.cond_nodes[flt.id], found)
            if cond:
                filters.append(RowFilter(flt.id, set(found.fields), set(found.tables)))
        from_sql = query.plan_model(usage, filters).from_sql
    rows = f"({from_sql}) AS base" if source.projected else from_sql
    where = _where(conds)
    return PreviewSql(
        sql=f"SELECT {', '.join(values)} FROM {rows}{where} LIMIT {max(1, int(limit))}",
        count_sql=f"SELECT count(*) AS {ident('rows')} FROM {rows}{where}",
        columns=columns,
        table=chosen,
    )


def custom_sql_parameters(
    ir: IR, values: dict[str, Scalar] | None = None
) -> dict[str, tuple[Scalar, str]]:
    """自訂 SQL 中參數（<[Parameters].[Parameter 1]>）的值；values 為參數 id → 目前值，省略時用預設值。"""
    result: dict[str, tuple[Scalar, str]] = {}
    for param in ir.parameters:
        value = (values or {}).get(param.id, param.value)
        result[_strip(param.name)] = (value, param.datatype)
        result.setdefault(param.caption, (value, param.datatype))
    return result


def _find_worksheet(ir: IR, key: str) -> Worksheet:
    for ws in ir.worksheets:
        if key in (ws.id, ws.name):
            return ws
    names = "、".join(f"{ws.id}（{ws.name}）" for ws in ir.worksheets)
    raise QueryError(f"找不到工作表「{key}」；可用的工作表：{names}")


def worksheet_sql(
    ir: IR, worksheet: str, state: QueryState | None = None, user: Identity | None = None
) -> WorksheetSql:
    return WorksheetQuery(ir, worksheet, state, user).build()


def pane_sql(
    ir: IR,
    worksheet: str,
    main: WorksheetSql,
    state: QueryState | None = None,
    user: Identity | None = None,
) -> dict[int, WorksheetSql]:
    """各量值的標記卡詳細程度不同時（J3，例如甜甜圈圖中間的合計），較粗的標記卡各自查詢。

    工作表的查詢（main）以所有維度的組合計算，是計算比對與資料表的依據；詳細程度較粗的標記卡
    （欄列架與它自己的標記卡上的維度比較少）以只含這些維度與這個量值的工作表另外查詢，供圖表使用。
    回傳 ws.panes 的索引 → 查詢。
    """
    ws = _find_worksheet(ir, worksheet)
    dims = {c.key for c in main.columns if c.role == "dimension"}
    if not ws.panes or not dims:
        return {}
    shelf = [u for u in [*ws.rows, *ws.columns] if use_key(u) in dims]
    result: dict[int, WorksheetSql] = {}
    for i, pane in enumerate(ws.panes):
        lod = {use_key(u) for u in [*shelf, *pane.marks.uses()] if use_key(u) in dims}
        if lod == dims:
            continue

        def keep(uses: list[FieldUse], axis: str, pane: Pane = pane) -> list[FieldUse]:
            if axis != pane.axis:
                return uses
            return [u for u in uses if use_key(u) in dims or u == pane.measure]

        virtual = ws.model_copy(
            update={
                "rows": keep(ws.rows, "rows"),
                "columns": keep(ws.columns, "columns"),
                "marks": pane.marks,
                "panes": [],
                "reference_lines": [],
            }
        )
        sub = ir.model_copy(
            update={"worksheets": [virtual if w.id == ws.id else w for w in ir.worksheets]}
        )
        result[i] = worksheet_sql(sub, ws.id, state, user)
    return result


COLUMN_ORDER = "__order(columns)"
"""欄標題的名次（DENSE_RANK，依欄架上離散維度自己的排序）：列與欄都有離散維度時才輸出。"""
GRAND_TOTAL = "Grand Total"
"""總計列與欄的標題（Tableau 英文介面的寫法）。"""
SUBTOTAL = "Total"
"""小計列與欄的標題（Tableau 英文介面的寫法）。"""


@dataclass
class TotalQuery:
    """一種總計（或小計）的查詢：shelves 為加上總計的架（rows、columns，或兩者＝交叉處），
    dropped 為去掉的維度（欄位鍵），這些欄位在總計列中顯示為 labels 的名稱（總計為「Grand Total」，
    小計為「Total」）。"""

    shelves: tuple[str, ...]
    dropped: list[str]
    query: WorksheetSql
    labels: dict[str, str] = field(default_factory=dict)
    group: list[str] | None = None
    """各架的小計放在哪一組之後：這些欄位鍵的顯示值相同的最後一筆之後（表頭依資料的順序排列，
    小計因此緊接在它的組之後）。列的小計以保留的列維度分組；欄的小計以列維度與保留的欄維度分組
    （資料先依列排序）。None 時放在最後：總計，以及列與欄交叉處（表頭已由各架的小計與總計帶出）。"""

    def label(self, key: str) -> str:
        return self.labels.get(key, GRAND_TOTAL)


def totals_sql(
    ir: IR,
    worksheet: str,
    main: WorksheetSql,
    state: QueryState | None = None,
    user: Identity | None = None,
    marks: QueryResult | None = None,
) -> list[TotalQuery]:
    """總計（Grand Total）的查詢：與 Tableau 的「自動」相同，在去掉該架上離散維度的詳細程度重新彙總
    底層資料（平均是所有資料列的平均、相異計數是整體的相異數，不是各列結果再加總）；篩選照樣套用。

    工作表的查詢（main）不含總計，計算比對以它為準；總計另外查詢，用於圖表與資料表的顯示。
    Table Calculation 套用到總計的值（在去掉維度後的詳細程度計算，例如總計列的合計百分比為 100%）。
    與量值篩選一起使用時，與 Tableau 相同只合計通過篩選的標記：marks 為工作表的查詢結果，總計限定在這些
    標記的維度組合（量值篩選不再套用）；Table Calculation 篩選只隱藏標記，不影響總計。
    """
    ws = _find_worksheet(ir, worksheet)
    if not ws.totals and not ws.subtotals:
        return []
    dims = {c.key for c in main.columns if c.role == "dimension"}
    headers = {
        "rows": [u for u in ws.rows if use_key(u) in dims and not u.continuous],
        "columns": [u for u in ws.columns if use_key(u) in dims and not u.continuous],
    }
    # 各架的選項：小計（去掉該維度之後的維度，依由外而內的順序）與總計（去掉架上所有離散維度）
    options: dict[str, list[tuple[list[FieldUse], str]]] = {}
    for shelf, uses in headers.items():
        found = [(uses[i + 1 :], SUBTOTAL) for i, u in enumerate(uses[:-1]) if u in ws.subtotals]
        if shelf in ws.totals and uses:
            found.append((uses, GRAND_TOTAL))
        options[shelf] = found
    if not options["rows"] and not options["columns"]:
        return []
    late = {f.id for f in main.state.filters if f.stage in ("measure", "table_calc")}
    own = {f.id for f in ws.filters}
    if late - own:
        raise QueryError("總計與工作表以外的量值篩選一起使用尚不支援", unsupported=True)
    measure_filtered = any(f.stage == "measure" for f in main.state.filters)
    restriction = _marks_filter(ws, main, marks) if measure_filtered else None
    # 列與欄的每一種組合（右下角等交叉處）；只有總計時與以前相同：列、欄、兩者
    combos: list[list[tuple[str, list[FieldUse], str]]] = []
    rows_opts: list[tuple[list[FieldUse], str] | None] = [*options["rows"], None]
    cols_opts: list[tuple[list[FieldUse], str] | None] = [None, *options["columns"]]
    for row in rows_opts:
        for col in cols_opts:
            combo = [("rows", *row)] if row is not None else []
            combo += [("columns", *col)] if col is not None else []
            if combo:
                combos.append(combo)
    combos.sort(key=lambda c: (len(c), [kind == GRAND_TOTAL for _, _, kind in c]))
    result: list[TotalQuery] = []
    for combo in combos:
        labels = {use_key(u): kind for _, uses, kind in combo for u in uses}
        dropped = list(labels)
        fields = {u.field for _, uses, _ in combo for u in uses}
        kept_filters = [f for f in ws.filters if f.id not in late]
        if restriction is not None:
            kept_filters.append(restriction)
        virtual = ws.model_copy(
            update={
                "rows": [u for u in ws.rows if use_key(u) not in dropped],
                "columns": [u for u in ws.columns if use_key(u) not in dropped],
                "sorts": [s for s in ws.sorts if s.field not in fields],
                "filters": kept_filters,
                "reference_lines": [],
                "totals": [],
                "subtotals": [],
            }
        )
        sub = ir.model_copy(
            update={"worksheets": [virtual if w.id == ws.id else w for w in ir.worksheets]}
        )
        group: list[str] | None = None
        if len(combo) == 1 and combo[0][2] == SUBTOTAL:
            kept = {
                shelf: [use_key(u) for u in uses if use_key(u) not in labels]
                for shelf, uses in headers.items()
            }
            group = kept["rows"] if combo[0][0] == "rows" else kept["rows"] + kept["columns"]
        result.append(
            TotalQuery(
                tuple(shelf for shelf, _, _ in combo),
                dropped,
                worksheet_sql(sub, ws.id, state, user),
                labels,
                group,
            )
        )
    return result


def _marks_filter(ws: Worksheet, main: WorksheetSql, marks: QueryResult | None) -> TupleFilter:
    """量值篩選後留下的標記：以標記的維度組合篩選底層資料（總計只合計這些標記）。marks 為 None 時
    （例如盤點只產生 SQL、不執行）不限定。"""
    dims = [c for c in main.columns if c.role == "dimension" and c.use is not None]
    tuples: list[list[Scalar]] = []
    if marks is not None:
        index = {key: i for i, key in enumerate(marks.columns)}
        seen: set[tuple[Scalar, ...]] = set()
        for row in marks.rows:
            values = tuple(_scalar(row[index[c.key]]) for c in dims if c.key in index)
            if values not in seen:
                seen.add(values)
                tuples.append(list(values))
    return TupleFilter(
        kind="tuple",
        id="marks_restriction",
        fields=[c.use for c in dims if c.use is not None],
        stage="dimension",
        mode="include" if marks is not None and dims else "all",
        tuples=tuples if marks is not None and dims else [],
        source_ref=ws.source_ref,
    )


def _scalar(value: Any) -> Scalar:
    """查詢結果的值 → IR 的常值（日期為 ISO 字串）。"""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if isoformat is not None else float(value)


def visual_totals(
    ws: Worksheet,
    main: WorksheetSql,
    marks: QueryResult,
    totals: list[tuple[TotalQuery, QueryResult]],
) -> list[tuple[TotalQuery, QueryResult]]:
    """總計的計算方式（Total Using）：這些量值的總計與小計改為範圍內各標記的值再加總（或平均、最小、
    最大）。範圍為總計保留的維度相同的標記（工作表的查詢結果，量值篩選後）。"""
    from tmig.data.executor import QueryResult  # 避免循環匯入（executor 也匯入本模組）

    if not ws.visual_totals:
        return totals
    aggs = {use_key(v.use): v.agg for v in ws.visual_totals}
    dims = [c.key for c in main.columns if c.role == "dimension"]
    index = {key: i for i, key in enumerate(marks.columns)}
    result: list[tuple[TotalQuery, QueryResult]] = []
    for total, rows in totals:
        kept = [d for d in dims if d not in total.dropped and d in index]
        groups: dict[tuple[Any, ...], dict[str, list[Any]]] = {}
        for row in marks.rows:
            group = groups.setdefault(tuple(row[index[d]] for d in kept), {})
            for key in aggs:
                if key in index and row[index[key]] is not None:
                    group.setdefault(key, []).append(row[index[key]])
        at = {key: i for i, key in enumerate(rows.columns)}
        changed: list[tuple[Any, ...]] = []
        for row in rows.rows:
            values = list(row)
            found = groups.get(tuple(row[at[d]] for d in kept if d in at), {})
            for key, agg in aggs.items():
                if key in at:
                    values[at[key]] = _visual_total(found.get(key, []), agg)
            changed.append(tuple(values))
        result.append((total, QueryResult(rows.columns, changed)))
    return result


def _visual_total(values: list[Any], agg: str) -> Any:
    if not values:
        return None
    if agg == "sum":
        return sum(values)
    if agg == "avg":
        return sum(values) / len(values)
    return min(values) if agg == "min" else max(values)


@dataclass
class TitleQueries:
    """標題中欄位值的查詢（與 Tableau 相同，以整張工作表計算）。"""

    measures: WorksheetSql | None
    """標題中的量值：全部資料（篩選之後）的彙總，一列。"""
    dimensions: dict[str, WorksheetSql]
    """標題中不在檢視中的維度（欄位鍵 → 只有這個維度的查詢）：只有一個值時顯示它，多個值時顯示 *。"""
    from_main: list[str] = field(default_factory=list)
    """標題中在檢視中的維度（欄位鍵）：直接取工作表查詢結果中的值（量值篩選也照樣生效）。"""


def title_sql(
    ir: IR,
    worksheet: str,
    main: WorksheetSql,
    state: QueryState | None = None,
    user: Identity | None = None,
) -> TitleQueries | None:
    """標題中的欄位值：量值在去掉所有維度的詳細程度彙總（例如 KPI 卡標題中的合計，與檢視的維度無關），
    維度查出篩選後的各個值。標題沒有欄位值時為 None。

    尚不支援：與量值篩選一起使用（Tableau 只計算通過篩選的標記）。
    """
    ws = _find_worksheet(ir, worksheet)
    uses: list[FieldUse] = []
    for run in ws.title:
        if run.use is not None and run.use not in uses:
            uses.append(run.use)
    if not uses:
        return None
    if any(u.table_calc is not None or u.field in SPECIAL_FIELDS for u in uses):
        raise QueryError("標題中的 Table Calculation 值尚不支援", unsupported=True)
    classifier = WorksheetQuery(ir, worksheet, state, user)
    measures = [u for u in uses if classifier.is_measure(u)]
    in_view = {c.key for c in main.columns if c.role == "dimension"}
    from_main = [use_key(u) for u in uses if not classifier.is_measure(u) and use_key(u) in in_view]
    dimensions = [u for u in uses if not classifier.is_measure(u) and use_key(u) not in from_main]
    if (measures or dimensions) and any(
        f.stage in ("measure", "table_calc") for f in main.state.filters
    ):
        raise QueryError("標題中的欄位值與量值篩選一起使用尚不支援", unsupported=True)
    empty = Marks(type=ws.marks.type)
    base = ws.model_copy(
        update={
            "rows": [],
            "columns": [],
            "marks": empty,
            "panes": [],
            "measure_values": [],
            "measure_aliases": [],
            "sorts": [],
            "reference_lines": [],
            "totals": [],
            "title": [],
        }
    )

    def query_for(virtual: Worksheet) -> WorksheetSql:
        sub = ir.model_copy(
            update={"worksheets": [virtual if w.id == ws.id else w for w in ir.worksheets]}
        )
        return worksheet_sql(sub, ws.id, state, user)

    measures_query = (
        query_for(base.model_copy(update={"marks": empty.model_copy(update={"label": measures})}))
        if measures
        else None
    )
    dimension_queries = {
        use_key(u): query_for(base.model_copy(update={"rows": [u]})) for u in dimensions
    }
    return TitleQueries(measures_query, dimension_queries, from_main)


__all__ = [
    "COLUMN_ORDER",
    "GRAND_TOTAL",
    "PREVIEW_LIMIT",
    "PUSHDOWN_BOUNDARY",
    "STAGES",
    "SUBTOTAL",
    "CteDef",
    "Guard",
    "OutputColumn",
    "PreviewColumn",
    "PreviewSql",
    "QueryError",
    "StageNote",
    "TitleQueries",
    "TotalQuery",
    "WorksheetQuery",
    "WorksheetSql",
    "custom_sql_parameters",
    "domain_sql",
    "pane_sql",
    "preview_sql",
    "title_sql",
    "totals_sql",
    "worksheet_sql",
]
