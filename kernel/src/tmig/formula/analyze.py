"""公式語意分析：解析欄位參照、推導型別與計算層級。

層級規則與 Tableau 相同：

- 常值與參數可與任何層級搭配
- 彙總函式的參數必須是列層級；結果為彙總
- Table Calculation 函式的參數必須是彙總；結果為 Table Calculation
- 同一個運算式不能混用列層級與彙總
- LOD 運算式內部必須是彙總，外部視為列層級的值
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol

from tmig.formula.ast import (
    Binary,
    Call,
    Case,
    FieldRef,
    If,
    InList,
    LiteralNode,
    Lod,
    Node,
    Unary,
    walk,
)
from tmig.formula.functions import lookup
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.formula.types import (
    DataType,
    FormulaSemanticError,
    Info,
    Level,
    combine_levels,
    comparable,
    is_numeric,
    unify,
)


@dataclass(frozen=True)
class Target:
    """欄位參照解析後的對象。"""

    kind: Literal["field", "param"]
    id: str
    datatype: DataType
    role: Literal["dimension", "measure", "param"]
    formula: str | None = None
    """計算欄位的公式；一般欄位與參數為 None。"""


class Scope(Protocol):
    def lookup(self, parts: tuple[str, ...]) -> Target | None: ...


class Analyzer:
    """針對一個資料源分析公式；計算欄位的結果會快取。"""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self._infos: dict[Node, Info] = {}
        self._asts: dict[str, Node] = {}
        self._fields: dict[str, Info] = {}
        self._in_progress: list[str] = []

    # ------------------------------------------------------------ 欄位

    def resolve(self, ref: FieldRef) -> Target:
        target = self.scope.lookup(ref.parts)
        if target is None:
            raise FormulaSemanticError(f"找不到欄位 {ref.text}", ref.pos)
        return target

    def calc_ast(self, target: Target) -> Node:
        """計算欄位的語法樹（快取）。"""
        if target.formula is None:
            raise ValueError(f"{target.id} 不是計算欄位")
        cached = self._asts.get(target.id)
        if cached is None:
            try:
                cached = parse_formula(target.formula)
            except FormulaSyntaxError as exc:
                raise FormulaSemanticError(
                    f"計算欄位 {target.id} 的公式無法解析：{exc.message}", exc.pos
                ) from exc
            self._asts[target.id] = cached
        return cached

    def target_info(self, target: Target, pos: int = 0) -> Info:
        if target.kind == "param":
            return Info(target.datatype, "const")
        if target.formula is None:
            return Info(target.datatype, "row")
        cached = self._fields.get(target.id)
        if cached is not None:
            return cached
        if target.id in self._in_progress:
            chain = " → ".join([*self._in_progress, target.id])
            raise FormulaSemanticError(f"計算欄位循環參照：{chain}", pos)
        self._in_progress.append(target.id)
        try:
            info = self.analyze(self.calc_ast(target))
        finally:
            self._in_progress.pop()
        self._fields[target.id] = info
        return info

    # ------------------------------------------------------------ 分析

    def analyze(self, node: Node) -> Info:
        cached = self._infos.get(node)
        if cached is None:
            cached = self._analyze(node)
            self._infos[node] = cached
        return cached

    def _analyze(self, node: Node) -> Info:
        match node:
            case LiteralNode(kind=kind):
                return Info(kind, "const")
            case FieldRef():
                return self.target_info(self.resolve(node), node.pos)
            case Unary(op=op, operand=operand, pos=pos):
                inner = self.analyze(operand)
                if op == "-" and not is_numeric(inner.datatype):
                    raise FormulaSemanticError("負號只能用在數字", pos)
                if op == "NOT" and inner.datatype not in ("boolean", "null"):
                    raise FormulaSemanticError("NOT 只能用在布林值", pos)
                return Info(inner.datatype if op == "-" else "boolean", inner.level, inner.has_lod)
            case Binary():
                return self._binary(node)
            case InList(operand=operand, values=values, pos=pos):
                infos = [self.analyze(operand), *(self.analyze(v) for v in values)]
                for info in infos[1:]:
                    if not comparable(infos[0].datatype, info.datatype):
                        raise FormulaSemanticError("IN 的值與比較對象型別不同", pos)
                return Info("boolean", combine_levels([i.level for i in infos], pos), _lod(infos))
            case Call():
                return self._call(node)
            case If(branches=branches, else_=else_, pos=pos):
                conds = [self.analyze(c) for c, _ in branches]
                for cond in conds:
                    if cond.datatype not in ("boolean", "null"):
                        raise FormulaSemanticError("IF 的條件必須是布林值", pos)
                results = [self.analyze(t) for _, t in branches]
                if else_ is not None:
                    results.append(self.analyze(else_))
                datatype = unify([r.datatype for r in results], pos, "IF 的各分支")
                infos = conds + results
                return Info(datatype, combine_levels([i.level for i in infos], pos), _lod(infos))
            case Case(subject=subject, whens=whens, else_=else_, pos=pos):
                subject_info = self.analyze(subject)
                when_infos = [self.analyze(w) for w, _ in whens]
                for info in when_infos:
                    if not comparable(subject_info.datatype, info.datatype):
                        raise FormulaSemanticError("CASE 的 WHEN 值與比較對象型別不同", pos)
                results = [self.analyze(t) for _, t in whens]
                if else_ is not None:
                    results.append(self.analyze(else_))
                datatype = unify([r.datatype for r in results], pos, "CASE 的各分支")
                infos = [subject_info, *when_infos, *results]
                return Info(datatype, combine_levels([i.level for i in infos], pos), _lod(infos))
            case Lod():
                return self._lod(node)

    def _binary(self, node: Binary) -> Info:
        left = self.analyze(node.left)
        right = self.analyze(node.right)
        op, pos = node.op, node.pos
        level = combine_levels([left.level, right.level], pos)
        has_lod = left.has_lod or right.has_lod
        a, b = left.datatype, right.datatype
        datatype: DataType
        if op in ("AND", "OR"):
            if a not in ("boolean", "null") or b not in ("boolean", "null"):
                raise FormulaSemanticError(f"{op} 的兩邊都必須是布林值", pos)
            datatype = "boolean"
        elif op in ("=", "!=", "<", "<=", ">", ">="):
            if not comparable(a, b):
                raise FormulaSemanticError(f"無法比較 {a} 與 {b}", pos)
            datatype = "boolean"
        elif op == "+":
            if is_numeric(a) and is_numeric(b):
                datatype = unify([a, b], pos, "+")
            elif {a, b} <= {"string", "null"}:
                datatype = "string"
            elif a in ("date", "datetime") and is_numeric(b):
                datatype = a  # type: ignore[assignment]
            else:
                raise FormulaSemanticError(f"無法相加 {a} 與 {b}", pos)
        elif op == "-":
            if is_numeric(a) and is_numeric(b):
                datatype = unify([a, b], pos, "-")
            elif a in ("date", "datetime") and is_numeric(b):
                datatype = a  # type: ignore[assignment]
            elif a in ("date", "datetime") and b in ("date", "datetime"):
                # 日期相減為相差的天數：兩邊都是日期時為整數，含時間時為小數
                datatype = "integer" if a == b == "date" else "real"
            else:
                raise FormulaSemanticError(f"無法相減 {a} 與 {b}", pos)
        elif op in ("*", "%"):
            if not (is_numeric(a) and is_numeric(b)):
                raise FormulaSemanticError(f"{op} 只能用在數字", pos)
            datatype = unify([a, b], pos, op)
        elif op in ("/", "^"):
            if not (is_numeric(a) and is_numeric(b)):
                raise FormulaSemanticError(f"{op} 只能用在數字", pos)
            datatype = "real"
        else:
            raise FormulaSemanticError(f"不支援的運算子 {op}", pos, unsupported=True)
        return Info(datatype, level, has_lod)

    def _call(self, node: Call) -> Info:
        spec = lookup(node.name, len(node.args))
        if spec is None:
            raise FormulaSemanticError(f"尚不支援函式 {node.name}", node.pos, unsupported=True)
        count = len(node.args)
        if not spec.min_args <= count <= spec.max_args:
            expected = (
                str(spec.min_args)
                if spec.min_args == spec.max_args
                else f"{spec.min_args} 到 {spec.max_args}"
            )
            raise FormulaSemanticError(
                f"{node.name} 需要 {expected} 個參數，卻有 {count} 個", node.pos
            )
        infos = [self.analyze(arg) for arg in node.args]
        datatype = spec.check(infos, list(node.args), node.pos)
        has_lod = _lod(infos)
        level: Level
        if spec.kind == "aggregate":
            if infos and infos[0].level in ("agg", "tc"):
                raise FormulaSemanticError(f"{node.name} 的參數已經是彙總", node.pos)
            level = "agg"
        elif spec.kind == "table_calc":
            for info in infos:
                if info.level == "row":
                    raise FormulaSemanticError(f"{node.name} 的參數必須是彙總", node.pos)
            level = "tc"
        else:
            level = combine_levels([i.level for i in infos], node.pos)
        return Info(datatype, level, has_lod)

    def _lod(self, node: Lod) -> Info:
        for dim in node.dims:
            info = self.analyze(dim)
            if info.level != "row":
                raise FormulaSemanticError(f"LOD 的維度 {dim.text} 必須是列層級的欄位", dim.pos)
        inner = self.analyze(node.expr)
        if inner.level != "agg":
            raise FormulaSemanticError("LOD 運算式內部必須是彙總", node.pos)
        if inner.has_lod:
            raise FormulaSemanticError("尚不支援巢狀 LOD", node.pos, unsupported=True)
        return Info(inner.datatype, "row", True)

    # ------------------------------------------------------------ 走訪

    def expand(self, node: Node) -> Iterator[Node]:
        """前序走訪，遇到計算欄位參照時也走進該欄位的公式。"""
        for item in walk(node):
            yield item
            if isinstance(item, FieldRef):
                target = self.resolve(item)
                if target.kind == "field" and target.formula is not None:
                    yield from self.expand(self.calc_ast(target))


def _lod(infos: list[Info]) -> bool:
    return any(i.has_lod for i in infos)
