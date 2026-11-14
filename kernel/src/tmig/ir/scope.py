"""把資料源的欄位與參數包成公式分析用的 Scope。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tmig.formula.analyze import Analyzer, Target
from tmig.formula.ast import Call, FieldRef, Lod, Node, children, map_children
from tmig.formula.functions import EXTERNAL, lookup
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.formula.types import Level
from tmig.formula.unparse import to_formula
from tmig.ir.models import IR, DataField, Datasource, Parameter


def _strip(name: str) -> str:
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


TABLE_OWNER = "__tableau_internal_object_id__"
"""邏輯資料表的筆數欄位在公式中的前綴（[__tableau_internal_object_id__].[資料表 id]）。"""


FOREIGN_LEVELS: dict[str | None, Level] = {
    None: "row",
    "row": "row",
    "lod": "row",
    "aggregate": "agg",
}
"""其他資料源的欄位在公式中的層級。Table Calculation 的計算欄位不在其中：它在混合後的結果上計算，公式改寫成
以 [資料源].[欄位] 引用後，在引用它的資料源中分析（見 ForeignSource.qualified）。"""


@dataclass(frozen=True)
class ForeignSource:
    """其他資料源（Data Blending 的次要資料源）：公式以 [資料源].[欄位] 引用它的欄位（資料源以內部名稱或
    顯示名稱書寫）。levels：計算欄位 id → 計算層級（省略時依欄位的 calc.level）。"""

    name: str
    caption: str
    fields: Sequence[DataField]
    levels: Mapping[str, str | None] | None = None

    def targets(self) -> dict[str, Target]:
        found: dict[str, Target] = {}
        captions: dict[str, Target] = {}
        for field in self.fields:
            if field.calc is None:
                level: str | None = None
            elif self.levels is not None:
                if self.levels.get(field.id) is None:
                    continue  # 公式無法分析
                level = self.levels[field.id]
            else:
                level = field.calc.level
                if level is None:
                    continue
            if level == "table_calc":
                formula = self.qualified(field)
                if formula is None:
                    continue
                target = Target("field", field.id, field.datatype, field.role, formula)
            elif level in FOREIGN_LEVELS:
                target = Target(
                    "field", field.id, field.datatype, field.role, None, FOREIGN_LEVELS[level]
                )
            else:
                continue
            found[_strip(field.name)] = target
            captions.setdefault(field.caption, target)
        return {**captions, **found}

    def qualified(self, field: DataField) -> str | None:
        """Table Calculation 計算欄位的公式，欄位改以 [資料源].[欄位] 引用（參數以 [Parameters].[參數]）：
        Table Calculation 在混合後的結果上計算，所以公式在引用它的資料源中展開。其中的彙總仍在這個資料源計算：
        單一的彙總（SUM([Target])）照一般的寫法；彙總之上還有運算的運算式（ZN(SUM([Target]))、
        SUM([A]) / SUM([B])）整段包成 __EXTERNAL__(…)，在這個資料源依連結欄位算好（沒有對應資料的標記是
        Null，與 Tableau 相同）。TOTAL 的參數在較粗的層級計算，不包（各彙總分別再彙總）。公式無法解析時為 None。"""
        if field.calc is None:
            return None
        try:
            tree = parse_formula(field.calc.formula)
        except FormulaSyntaxError:
            return None
        owner = _strip(self.name)
        names = {_strip(f.name): f for f in self.fields}
        captions: dict[str, DataField] = {}
        for item in self.fields:
            captions.setdefault(item.caption, item)

        def own(node: FieldRef) -> DataField | None:
            parts = node.parts
            if len(parts) == 2 and parts[0] in (owner, self.caption):
                parts = parts[1:]
            if len(parts) != 1:
                return None
            return names.get(parts[0]) or captions.get(parts[0])

        def level(item: DataField) -> str | None:
            if item.calc is None:
                return "row"
            return self.levels.get(item.id) if self.levels is not None else item.calc.level

        def kind(node: Node) -> str | None:
            """彙總函式或彙總層級的計算欄位為 aggregate，Table Calculation 的為 table_calc。"""
            if isinstance(node, Call):
                spec = lookup(node.name, len(node.args))
                return spec.kind if spec is not None and spec.kind != "row" else None
            if isinstance(node, FieldRef):
                found = own(node)
                found_level = level(found) if found is not None else None
                return found_level if found_level in ("aggregate", "table_calc") else None
            return None

        def has(node: Node, wanted: str) -> bool:
            if kind(node) == wanted:
                return True
            if isinstance(node, Lod) or kind(node) == "aggregate":
                return False  # LOD 與彙總的內部是列層級
            return any(has(child, wanted) for child in children(node))

        def plain(node: Node) -> Node:
            if not isinstance(node, FieldRef):
                return map_children(node, plain)
            parts = node.parts
            if len(parts) == 2 and parts[0] not in (owner, self.caption):
                return node  # 參數（[Parameters].[…]）或其他資料源的欄位
            found = own(node)
            if found is None:
                # 不是欄位：參數（以顯示名稱引用）
                return FieldRef(node.pos, ("Parameters", parts[-1]))
            return FieldRef(node.pos, (owner, _strip(found.name)))

        def visit(node: Node, total: bool = False) -> Node:
            if not has(node, "table_calc"):
                done = plain(node)
                if total or kind(node) == "aggregate" or not has(node, "aggregate"):
                    return done
                return Call(node.pos, EXTERNAL, (done,))
            if isinstance(node, FieldRef):
                return plain(node)  # 另一個 Table Calculation 計算欄位
            inside = isinstance(node, Call) and node.name == "TOTAL"
            return map_children(node, lambda child: visit(child, total or inside))

        return to_formula(visit(tree))


class DatasourceScope:
    """公式中的參照：[欄位內部名稱或顯示名稱]、[Parameters].[參數內部名稱或顯示名稱]、[資料源].[欄位]
    （others 中其他資料源的欄位，見 ForeignSource）。"""

    def __init__(
        self,
        ds_name: str,
        fields: Sequence[DataField],
        parameters: Sequence[Parameter],
        others: Sequence[ForeignSource] = (),
    ) -> None:
        self.ds_name = _strip(ds_name)
        self._others: dict[str, dict[str, Target]] = {}
        self.foreign: dict[str, str] = {}
        """其他資料源的欄位 id → 資料源的內部名稱。"""
        for other in others:
            targets = other.targets()
            for key in (other.caption, _strip(other.name)):
                if key != self.ds_name:
                    self._others[key] = targets
            for target in targets.values():
                self.foreign[target.id] = other.name
        self._fields: dict[str, Target] = {}
        self._captions: dict[str, Target] = {}
        self._by_id: dict[str, Target] = {}
        for field in fields:
            target = Target(
                "field",
                field.id,
                field.datatype,
                field.role,
                field.calc.formula if field.calc is not None else None,
            )
            self._fields[_strip(field.name)] = target
            self._captions.setdefault(field.caption, target)
            self._by_id[field.id] = target
        self._params: dict[str, Target] = {}
        for param in parameters:
            target = Target("param", param.id, param.datatype, "param")
            self._params[_strip(param.name)] = target
            self._params.setdefault(param.caption, target)
            self._by_id[param.id] = target
        for targets in self._others.values():
            for target in targets.values():
                self._by_id.setdefault(target.id, target)

    @classmethod
    def for_datasource(
        cls,
        datasource: Datasource,
        parameters: Sequence[Parameter],
        others: Sequence[Datasource] = (),
    ) -> DatasourceScope:
        """others：公式可以引用的其他資料源（Data Blending）。"""
        return cls(
            datasource.name,
            datasource.fields,
            parameters,
            [ForeignSource(d.name, d.caption, d.fields) for d in others if d.id != datasource.id],
        )

    def lookup(self, parts: tuple[str, ...]) -> Target | None:
        if len(parts) == 2:
            owner, name = parts
            if owner == "Parameters":
                return self._params.get(name)
            if owner == TABLE_OWNER:
                # 邏輯資料表的筆數欄位，例如 COUNT([__tableau_internal_object_id__].[Orders_A1B2…])
                return self._fields.get(_strip(f"[{TABLE_OWNER}].[{name.replace(']', ']]')}]"))
            if owner != self.ds_name:
                return self._others.get(owner, {}).get(name)
            parts = (name,)
        if len(parts) != 1:
            return None
        name = parts[0]
        return self._fields.get(name) or self._captions.get(name) or self._params.get(name)

    def target(self, target_id: str) -> Target:
        return self._by_id[target_id]


def calc_references(fields: Mapping[str, DataField], field_id: str) -> set[str]:
    """計算欄位引用的其他資料源欄位（id）：語法樹（calc.ast）中不在 fields 裡的欄位，引用的計算欄位一併展開。
    Data Blending 中引用次要資料源欄位的計算欄位用。"""
    found: set[str] = set()
    seen: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, list):
            for value in item:
                visit(value)
        elif isinstance(item, dict):
            if item.get("node") == "field" and item.get("target") == "field":
                expand(str(item.get("id")))
            for value in item.values():
                visit(value)

    def expand(current: str) -> None:
        if current in seen:
            return
        seen.add(current)
        data_field = fields.get(current)
        if data_field is None:
            found.add(current)
        elif data_field.calc is not None and data_field.calc.ast is not None:
            visit(data_field.calc.ast)

    expand(field_id)
    found.discard(field_id)
    return found


def analyzer_for(ir: IR, datasource_id: str) -> Analyzer:
    datasource = next(ds for ds in ir.datasources if ds.id == datasource_id)
    return Analyzer(DatasourceScope.for_datasource(datasource, ir.parameters, ir.datasources))
