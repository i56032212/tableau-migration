"""Data Blending（資料混合）：次要資料源的彙總在它自己的資料源算好，再併進主要資料源的查詢。

與 Tableau 相同：

- 主要資料源照常查詢（篩選、排序、LOD 都以主要資料源計算）
- 次要資料源以作用中的連結欄位（主要資料源的連結欄位在檢視中，見 BlendLink.secondary_for）分組，
  彙總次要資料源的量值；次要資料源自己的資料源篩選照樣套用，工作表上主要資料源的篩選不套用到次要資料源
- 以連結欄位 LEFT JOIN：主要資料源沒有的值不出現，次要資料源沒有的值為 Null；檢視中有連結欄位以外的
  維度時，次要資料源的值在這些維度上重複（以連結欄位的層級計算）
- 連結欄位的 Null 視為相等（與本平台以維度連接的其他地方相同；Tableau 的行為未確認）

做法（「外部彙總」）：主要資料源的查詢（tmig.sqlgen.query.WorksheetQuery）中，只用到次要資料源欄位的彙總
（SUM([Targets].[Target])、次要資料源的量值與 ATTR 的維度）不從主要資料源的資料列計算，而是在每次彙總之後
（檢視的層級、排序的層級、TOTAL 與總計的層級）從次要資料源的結果併入：

- 次要資料源的查詢（WorksheetSql.stages，__s0、__s1…）在它自己的來源執行（萃取、檔案、跨資料庫的 join，
  或 Live 連線的資料庫），依連結欄位分組算出這些彙總；結果以同名的資料表交給執行主要資料源查詢的 DuckDB
- 彙總的層級含所有連結欄位時（檢視的層級）直接以連結欄位 LEFT JOIN；較粗的層級（依欄位排序、TOTAL、總計與
  小計）先取出該層級中出現的連結值，再把各連結值的結果再彙總（加總、計數、最小、最大、平均與 ATTR；相異計數、
  中位數等非加總型的彙總不能這樣算，列為不支援，Tableau 在混合時同樣有限制）
- 併入之後的各段（量值的運算、量值篩選、排序、Table Calculation、總計）與沒有混合時相同，所以引用次要資料源
  欄位的計算欄位（SUM([Sales]) / SUM([Targets].[Target])）、次要資料源的量值篩選與 Table Calculation 都照
  一般的流程計算；ZN(SUM([Targets].[Target])) 在次要資料源沒有對應的列時是 0
- 次要資料源的維度以 ATTR 彙總（文字欄位對到多個值時為 *）

次要資料源的維度篩選（工作表的 filters 中欄位是次要資料源欄位的篩選）套用在次要資料源的查詢；主要資料源中連結
欄位的值沒有對應到通過篩選的資料時，那些標記不顯示（檢視層級的彙總改為 INNER JOIN），篩選包含 Null 時保留次要
資料源完全沒有對應資料的標記（另外查出不篩選時有哪些連結值，__u）。這一項與總計再彙總的算法，Tableau 的說明
文件沒有寫明，依公開資料推定。

次要資料源中定義的 Table Calculation 計算欄位（RUNNING_SUM(SUM([Target])) 等）在混合後的結果上計算：公式改寫成
以 [資料源].[欄位] 引用（tmig.ir.scope.ForeignSource.qualified），在主要資料源的查詢中展開，其中的彙總仍是
外部彙總；彙總之上還有運算的運算式（ZN(SUM([Target]))）整段在次要資料源計算（EXTERNAL_REF），所以沒有對應
資料的標記是 Null，與主要資料源的計算欄位（混合後才計算，ZN 得到 0）不同。

尚不支援：同一個彙總中同時用到主要與次要資料源的欄位、LOD 運算式中次要資料源的欄位、次要資料源的內容篩選、
條件篩選與 Top N 篩選。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from tmig.formula.ast import Call, FieldRef, Node, map_children, walk
from tmig.formula.functions import EXTERNAL
from tmig.formula.unparse import to_formula
from tmig.ir.models import (
    Blend,
    Calc,
    CategoricalFilter,
    DataField,
    Datasource,
    FieldUse,
    Filter,
    LiveConnection,
    Marks,
    TupleFilter,
    Worksheet,
)
from tmig.planner.state import EffectiveState, QueryState, apply_state, use_key
from tmig.sqlgen.sql import cast, ident, null_safe_equal

if TYPE_CHECKING:
    from tmig.sqlgen.query import BlendStage, WorksheetQuery

NUMERIC = frozenset({"integer", "real"})
TEMPORAL = frozenset({"date", "datetime"})
EXTERNAL_REF = EXTERNAL
"""整段在次要資料源計算的彙總運算式在語法樹中的代表：Call(EXTERNAL_REF, (運算式,))，當作一個外部彙總。
直接引用次要資料源的彙總計算欄位時是 Call(EXTERNAL_REF, (欄位,))；次要資料源的 Table Calculation 計算欄位
中的彙總運算式（ZN(SUM([Target])) 等）也是（見 tmig.ir.scope.ForeignSource.qualified）。"""
TOTAL_RULES = {
    "SUM": "sum",
    "COUNT": "sum",
    "MIN": "min",
    "MAX": "max",
    "ATTR": "attr",
    "AVG": "avg",
}
"""次要資料源的彙總在較粗的層級由各連結值的結果再彙總的方式（加總型的彙總）。"""


@dataclass
class External:
    """一個外部彙總：次要資料源查詢中的欄（暫時的計算欄位 id）與再彙總的方式。"""

    node: Call
    column: str
    formula: str
    datatype: str
    rule: str | None
    """sum、min、max、avg、attr、star（文字的 ATTR，多個值時為 *）；None 為不能再彙總。"""


@dataclass
class Source:
    """一個次要資料源。"""

    index: int
    blend: Blend
    datasource: Datasource
    filters: list[Filter]
    """次要資料源的維度篩選（工作表的篩選中欄位是它的欄位的）。"""
    pairs: list[tuple[FieldUse, FieldUse]] = field(default_factory=list)
    """作用中的連結：（主要資料源在檢視中的維度、次要資料源對應的欄位）。"""
    externals: dict[Node, External] = field(default_factory=dict)
    mode: str = "left"
    """left：沒有篩選；inner：只留下有對應到通過篩選的資料的標記；null：另外保留次要資料源完全沒有對應
    資料的標記（篩選包含 Null）。"""
    effective: EffectiveState | None = None
    used: bool = False

    @property
    def name(self) -> str:
        return f"__s{self.index}"

    @property
    def unfiltered(self) -> str:
        return f"__u{self.index}"


class Blending:
    """一張工作表的 Data Blending：哪些彙總是外部彙總、怎麼併入，以及次要資料源的查詢。"""

    def __init__(self, query: WorksheetQuery, sources: list[tuple[Blend, Datasource]]) -> None:
        self.query = query
        ws = query.worksheet
        self.sources: list[Source] = []
        for index, (blend, datasource) in enumerate(sources):
            ids = {f.id for f in datasource.fields}
            # 次要資料源的維度篩選（量值與 Table Calculation 的篩選以混合後的值篩選，留在主要的查詢）
            own: list[Filter] = [
                f
                for f in ws.filters
                if not isinstance(f, TupleFilter)
                and f.field.field in ids
                and f.stage not in ("measure", "table_calc")
            ]
            self.sources.append(Source(index, blend, datasource, own))
        self.owner = {f.id: s for s in self.sources for f in s.datasource.fields}
        self._sides: dict[Node, set[int]] = {}
        self._types: dict[int, WorksheetQuery] = {}
        for source in self.sources:
            self._restriction(source)

    # ------------------------------------------------------------ 連結

    def activate(self, dims: list[FieldUse]) -> None:
        """檢視的維度決定作用中的連結（總計的查詢依原本的檢視）。"""
        for source in self.sources:
            source.pairs = []
            for use in dims:
                for link in source.blend.links:
                    found = link.secondary_for(use)
                    if found is not None and all(
                        use_key(found) != use_key(f) for _, f in source.pairs
                    ):
                        source.pairs.append((use, found))

    def _holder(self, source: Source, measures: list[FieldUse], filters: list[Filter]) -> Worksheet:
        links = [found for _, found in source.pairs]
        return _secondary_worksheet(
            self.query.worksheet, source.datasource.id, links, measures, filters
        )

    def _restriction(self, source: Source) -> None:
        """次要資料源的篩選（套用目前的狀態後）是否限制主要資料源的標記。"""
        from tmig.sqlgen.query import QueryError

        query = self.query
        holder = self._holder(source, [], source.filters)
        sub = query.ir.model_copy(update={"worksheets": [holder]})
        try:
            source.effective = apply_state(sub, holder, query.query_state or QueryState())
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        ids = {f.id for f in source.datasource.fields}
        restricting = [
            f
            for f in source.effective.filters
            if not isinstance(f, TupleFilter)
            and f.field.field in ids
            and not (isinstance(f, CategoricalFilter) and f.mode == "all")
        ]
        if not restricting:
            source.mode = "left"
        elif all(_admits_null(f) for f in restricting):
            source.mode = "null"
        else:
            source.mode = "inner"

    # ------------------------------------------------------------ 外部彙總

    def sides(self, node: Node) -> set[int]:
        """運算式用到的資料源：次要資料源的索引，主要資料源為 -1；引用的計算欄位一併展開。"""
        key = self.query.canon(node)
        found = self._sides.get(key)
        if found is not None:
            return found
        found = set()
        for item in walk(node):
            if not isinstance(item, FieldRef):
                continue
            target = self.query._resolve(item)  # pyright: ignore[reportPrivateUsage]
            if target.kind == "param":
                continue
            source = self.owner.get(target.id)
            if source is not None:
                found.add(source.index)
            elif target.formula is not None:
                found |= self.sides(self.query.analyzer.calc_ast(target))
            else:
                found.add(-1)
        self._sides[key] = found
        return found

    def source_of(self, node: Call) -> Source | None:
        """彙總只用到一個次要資料源的欄位時，那個次要資料源（外部彙總）；主要資料源的彙總為 None。"""
        from tmig.sqlgen.query import QueryError

        sides = self.sides(node)
        if not sides or sides == {-1}:
            return None
        if len(sides) > 1:
            raise QueryError(
                f"Data Blending：{node.name} 中同時用到主要與次要資料源（或多個次要資料源）的欄位"
                "（各資料源的欄位要各自彙總），尚不支援",
                unsupported=True,
            )
        return self.sources[next(iter(sides))]

    def check_lod(self, node: Node) -> None:
        from tmig.sqlgen.query import QueryError

        if any(side != -1 for side in self.sides(node)):
            raise QueryError(
                "Data Blending：LOD 運算式中用到次要資料源的欄位，尚不支援", unsupported=True
            )

    def _external(self, source: Source, node: Call) -> External:
        key = self.query.canon(node)
        found = source.externals.get(key)
        if found is not None:
            return found
        query = self.query
        local = self._localize(node)
        column = f"blend_x{source.index}_{len(source.externals)}"
        if node.name == EXTERNAL_REF:
            # 整段運算式在次要資料源計算（不能由各連結值的結果再彙總）
            inner = to_formula(local.args[0])  # type: ignore[union-attr]
            found = External(node, column, inner, query.datatype(node.args[0]), None)
        else:
            datatype = query.datatype(node)
            rule = TOTAL_RULES.get(node.name)
            formula = to_formula(local)
            if node.name == "ATTR" and datatype == "string":
                # 文字的 ATTR：連結欄位的一個值對到多個值時顯示 *（與 Tableau 相同）
                own = to_formula(local.args[0])  # type: ignore[union-attr]
                formula = f"IF MIN({own}) = MAX({own}) THEN MIN({own}) ELSEIF COUNT({own}) > 0 THEN '*' END"
                rule = "star"
            found = External(node, column, formula, datatype, rule)
        source.externals[key] = found
        return found

    def _localize(self, node: Node) -> Node:
        """次要資料源的運算式：[資料源].[欄位] 改成它自己的欄位名稱（在次要資料源中計算）。"""
        if isinstance(node, FieldRef):
            target = self.query._resolve(node)  # pyright: ignore[reportPrivateUsage]
            source = self.owner.get(target.id)
            if source is not None:
                name = next(f.name for f in source.datasource.fields if f.id == target.id)
                return FieldRef(node.pos, (_strip_brackets(name),))
            return node
        return map_children(node, self._localize)

    def _link_types(self, source: Source) -> list[tuple[str, str]]:
        """各連結欄位的型別（主要資料源、次要資料源）。"""
        from tmig.sqlgen.query import WorksheetQuery

        query = self.query
        other = self._types.get(source.index)
        if other is None:
            holder = self._holder(source, [], [])
            sub = query.ir.model_copy(update={"worksheets": [holder]})
            other = WorksheetQuery(sub, holder.id, None, query.user)
            self._types[source.index] = other
        return [
            (query.dim_datatype(primary), other.dim_datatype(found))
            for primary, found in source.pairs
        ]

    def attach(
        self,
        prefix: str,
        name: str,
        cols: dict[Call, str],
        keys: list[str],
        nodes: list[Call],
        level: list[str],
        rows: str,
        stage: int,
        pushdown: bool | None,
        restrict: bool,
    ) -> str:
        """在 name（已在 level 的層級彙總，層級鍵為 keys）併入外部彙總 nodes，回傳新的 CTE 名稱；cols 補上各
        外部彙總的欄。restrict：檢視的層級，次要資料源的篩選限制顯示的標記。"""
        from tmig.sqlgen.query import QueryError

        query = self.query
        joins: list[str] = []
        picks: list[str] = []
        where: list[str] = []
        for source in self.sources:
            own = [n for n in nodes if self.source_of(n) is source]
            restricted = restrict and source.mode != "left"
            if not own and not restricted:
                continue
            if not source.pairs:
                raise QueryError(
                    f"Data Blending：檢視中沒有與次要資料源「{source.datasource.caption}」的連結欄位",
                    unsupported=True,
                )
            source.used = True
            externals = [self._external(source, n) for n in own]
            types = self._link_types(source)
            link_sql = [query.dim_sql(primary) for primary, _ in source.pairs]
            alias = f"s{source.index}"

            def conditions(
                left: list[str],
                right: str,
                source: Source = source,
                types: list[tuple[str, str]] = types,
            ) -> str:
                return " AND ".join(
                    _link_condition(column, primary, f"{right}.{ident(use_key(found))}", secondary)
                    for column, (_, found), (primary, secondary) in zip(
                        left, source.pairs, types, strict=True
                    )
                )

            if all(sql in level for sql in link_sql):
                # 這個層級含所有連結欄位：直接以連結欄位連接
                left = [f"b.{ident(keys[level.index(sql)])}" for sql in link_sql]
                kind = "INNER" if restricted and source.mode == "inner" else "LEFT"
                if restricted and source.mode == "null":
                    hit = ident("__hit")
                    other = f"u{source.index}"
                    joins.append(
                        f"LEFT JOIN (SELECT *, TRUE AS {hit} FROM {ident(source.name)}) {alias} "
                        f"ON {conditions(left, alias)}"
                    )
                    joins.append(
                        f"LEFT JOIN (SELECT *, TRUE AS {hit} FROM {ident(source.unfiltered)}) "
                        f"{other} ON {conditions(left, other)}"
                    )
                    where.append(f"({alias}.{hit} IS NOT NULL OR {other}.{hit} IS NULL)")
                else:
                    joins.append(
                        f"{kind} JOIN {ident(source.name)} {alias} ON {conditions(left, alias)}"
                    )
                for external in externals:
                    col = f"__a{len(cols)}"
                    cols[query.canon(external.node)] = col  # type: ignore[index]
                    picks.append(f"{alias}.{ident(external.column)} AS {ident(col)}")
                continue
            if not externals:
                continue  # 較粗的層級不限制標記（總計以顯示的標記限定，見 totals_sql）
            # 較粗的層級：這個層級中出現的連結值，各連結值的結果再彙總
            for external in externals:
                if external.rule is None:
                    raise QueryError(
                        f"Data Blending：次要資料源「{source.datasource.caption}」的 {external.formula} "
                        "在比連結欄位粗的層級（總計、小計、依欄位排序、TOTAL）不能由各連結值的結果再彙總"
                        "（只支援加總、計數、最小、最大、平均與 ATTR），尚不支援",
                        unsupported=True,
                    )
            link_keys = [f"__l{i}" for i in range(len(link_sql))]
            selects = [f"{sql} AS {ident(k)}" for sql, k in zip(level, keys, strict=True)]
            selects += [f"{sql} AS {ident(k)}" for sql, k in zip(link_sql, link_keys, strict=True)]
            pairs = query.cte(
                stage,
                query.name(f"{prefix}_links"),
                f"SELECT DISTINCT {', '.join(selects)} FROM {rows}",
                pushdown,
            )
            items = [f"d.{ident(k)}" for k in keys]
            items += [
                f"{_reaggregate(e.rule or '', e.column, alias)} AS {ident(e.column)}"
                for e in externals
            ]
            group = f" GROUP BY {', '.join(str(i + 1) for i in range(len(keys)))}" if keys else ""
            left = [f"d.{ident(k)}" for k in link_keys]
            again = query.cte(
                stage,
                query.name(f"{prefix}_blend"),
                f"SELECT {', '.join(items)} FROM {pairs} d LEFT JOIN {ident(source.name)} {alias} "
                f"ON {conditions(left, alias)}{group}",
                False,
            )
            on = (
                " AND ".join(
                    null_safe_equal(f"b.{ident(k)}", f"t{source.index}.{ident(k)}") for k in keys
                )
                or "TRUE"
            )
            joins.append(f"LEFT JOIN {again} t{source.index} ON {on}")
            for external in externals:
                col = f"__a{len(cols)}"
                cols[query.canon(external.node)] = col  # type: ignore[index]
                picks.append(f"t{source.index}.{ident(external.column)} AS {ident(col)}")
        if not joins:
            return name
        return query.cte(
            stage,
            query.name(f"{prefix}_x"),
            f"SELECT {', '.join(['b.*', *picks])} FROM {name} b {' '.join(joins)}"
            + (f" WHERE {' AND '.join(where)}" if where else ""),
            False,
        )

    # ------------------------------------------------------------ 次要資料源的查詢

    def stages(self) -> list[BlendStage]:
        """用到的次要資料源各自的查詢（依連結欄位彙總外部彙總），在主要資料源的查詢產生之後呼叫。"""
        from tmig.sqlgen.query import BlendStage, WorksheetQuery

        query = self.query
        found: list[BlendStage] = []
        for source in self.sources:
            if not source.used:
                continue
            fields: list[DataField] = []
            measures: list[FieldUse] = []
            for external in source.externals.values():
                formulas = {external.column: (external.formula, external.datatype)}
                if external.rule == "avg":
                    # 平均在較粗的層級以加總與筆數再彙總
                    argument = to_formula(self._localize(external.node.args[0]))
                    formulas[f"{external.column}_sum"] = (f"SUM({argument})", "real")
                    formulas[f"{external.column}_count"] = (f"COUNT({argument})", "integer")
                for column, (formula, datatype) in formulas.items():
                    fields.append(
                        DataField(
                            id=column,
                            name=f"[{column}]",
                            caption=column,
                            role="measure",
                            datatype=datatype,  # type: ignore[arg-type]
                            calc=Calc(formula=formula, level="aggregate"),
                            source_ref="/blend",
                        )
                    )
                    measures.append(FieldUse(field=column, continuous=datatype != "string"))
            secondary = source.datasource.model_copy(
                update={"fields": [*source.datasource.fields, *fields]}
            )
            ir = query.ir.model_copy(
                update={
                    "datasources": [
                        secondary if d.id == secondary.id else d for d in query.ir.datasources
                    ]
                }
            )
            holder = self._holder(source, measures, source.filters)
            built = WorksheetQuery(
                ir.model_copy(update={"worksheets": [holder]}),
                holder.id,
                query.query_state,
                query.user,
            ).build()
            found.append(BlendStage(source.name, secondary, built))
            where = "資料庫" if isinstance(secondary.connection, LiveConnection) else "資料"
            keys = ", ".join(use_key(f) for _, f in source.pairs)
            query.note(
                6,
                f"Data Blending：次要資料源「{secondary.caption}」在它的{where}以 {keys} 彙總"
                f"（{source.name}），依連結欄位併入"
                + ("；它的篩選限制顯示的標記" if source.mode != "left" else ""),
            )
            if source.mode == "null":
                # 篩選包含 Null：另外查出次要資料源（不篩選）有哪些連結值
                plain = self._holder(source, [], []).model_copy(update={"id": "blend_holder_all"})
                state = query.query_state
                unfiltered = state.model_copy(update={"filters": {}}) if state is not None else None
                everything = WorksheetQuery(
                    ir.model_copy(update={"worksheets": [plain]}), plain.id, unfiltered, query.user
                ).build()
                found.append(BlendStage(source.unfiltered, secondary, everything))
        return found

    def state(self, primary: EffectiveState) -> EffectiveState:
        """主要資料源套用的狀態加上各次要資料源的篩選（套用狀態後）。"""
        filters = list(primary.filters)
        applied = list(primary.applied)
        for source in self.sources:
            if source.effective is None:
                continue
            ids = {f.id for f in source.datasource.fields}
            filters += [
                f
                for f in source.effective.filters
                if f not in filters and not isinstance(f, TupleFilter) and f.field.field in ids
            ]
            applied += [key for key in source.effective.applied if key not in applied]
        skipped = [key for key in primary.skipped if key not in applied]
        return replace(primary, filters=filters, applied=applied, skipped=skipped)


def _admits_null(flt: Filter) -> bool:
    """篩選保留 Null（次要資料源沒有對應資料的標記仍顯示）。"""
    if isinstance(flt, CategoricalFilter):
        return (None in flt.values) if flt.mode == "include" else (None not in flt.values)
    return bool(getattr(flt, "include_null", False) or getattr(flt, "only_null", False))


def _reaggregate(rule: str, column: str, source: str) -> str:
    """連結欄位的層級算出的一欄，依範圍內的連結值再彙總。"""
    value = f"{source}.{ident(column)}"
    if rule in ("sum", "min", "max"):
        return f"{rule}({value})"
    if rule == "avg":
        total = f"{source}.{ident(column + '_sum')}"
        count = f"{source}.{ident(column + '_count')}"
        return f"(sum({total}) / NULLIF(sum({count}), 0))"
    if rule == "star":
        return (
            f"(CASE WHEN min({value}) = max({value}) THEN min({value}) "
            f"WHEN count({value}) > 0 THEN '*' END)"
        )
    return f"(CASE WHEN min({value}) IS NOT DISTINCT FROM max({value}) THEN min({value}) END)"


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


def _secondary_worksheet(
    ws: Worksheet,
    datasource: str,
    links: list[FieldUse],
    measures: list[FieldUse],
    filters: list[Filter],
) -> Worksheet:
    """次要資料源的查詢：連結欄位為維度、次要資料源的量值在標籤上；篩選只有次要資料源自己的篩選，沒有排序。"""
    return Worksheet(
        id="blend_holder",
        name=f"{ws.name}（次要資料源）",
        datasource=datasource,
        rows=links,
        marks=Marks(type="text", label=measures),
        filters=filters,
        source_ref=ws.source_ref,
    )


def _strip_brackets(name: str) -> str:
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


__all__ = ["EXTERNAL_REF", "Blending"]
