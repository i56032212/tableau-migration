"""Relationship 模型的查詢（N6）：依檢視用到的欄位決定要取哪些邏輯資料表、怎麼連接。

與 Tableau 的 Relationship 語意相同的部分：

- 只取檢視用到的資料表（欄列架、標記卡、排序、LOD、Top N、計算欄位展開後的實體欄位，以及篩選）；
  只用到一張資料表時就只查那一張
- 量值在自己的資料表（量值表）彙總，不因其他資料表的一對多而重複計算：其他資料表依
  「關聯欄位＋檢視用到的欄位」去重（DISTINCT）後左外連接，量值表的每一列對每一組維度值只算一次；
  沒有對應的列以 Null 呈現
- 篩選只用到其他資料表的欄位（不在檢視中）時，以半連接（EXISTS）在該篩選自己的運算階段套用：
  量值表只保留「有關聯、且關聯到的列中有符合條件者」，不參與去重；維度篩選因此仍在 FIXED LOD 之後
- 只有維度（沒有任何彙總）時，只顯示各資料表之間存在的組合（內部連接）
- 關聯必須是一棵樹；只用到部分資料表時，取連接它們的最短路徑上的資料表

一對多的分支：檢視的維度含其他資料表的欄位時，量值表的一列會對應到該欄位的每個值。在檢視的層級
（含這些欄位）彙總沒有問題；在不含這些欄位的層級彙總（FIXED LOD、排序、合計、Top N…）時，
同一列會重複計算。這種情形查詢時附上檢查（Branch.guard_sql）：執行前確認分支的關聯值都只對應
一組欄位值（星狀模型的維度表都是如此），否則列為不支援，不給出與 Tableau 不同的數字。
不受重複影響的彙總（MIN、MAX、COUNTD、ATTR）不需要檢查。

量值來自多張資料表（例如一張表的 COUNTD 與另一張表的 FIXED LOD）：與 Tableau 相同，每張量值表
各自是一部分（Part）——以它為量值表連接其他資料表、套用篩選（各部分的半連接不同）——各自在檢視的
層級彙總，再依維度合併（完整外部連接：任一部分有的維度值都列出，沒有該部分的列時它的量值為 Null）。
實作上 base 是各部分的 UNION ALL（PART_COL 標明每一列屬於哪一部分，其他部分的欄位為 Null），
每個彙總只計算所屬部分的列（見 tmig.sqlgen.query 的 WorksheetQuery.owner_of）；
一個彙總同時用到多張資料表的欄位時列為不支援。

尚不支援（查詢時列為不支援）：同一個彙總用到多張資料表的欄位、同時用到量值表與其他資料表
（或多個分支）欄位的篩選、含 LOD 且只能以半連接套用的篩選、邏輯資料表內含 join。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from tmig.ir.models import (
    DataField,
    Datasource,
    ModelRelation,
    Relation,
    Relationship,
    SqlRelation,
    TableRelation,
)
from tmig.sqlgen.source import CUSTOM_SQL_PREFIX
from tmig.sqlgen.sql import ident

PART_COL = "__part"
"""量值來自多張資料表時，base 的每一列屬於哪一部分（量值表在 ModelPlan.parts 中的索引）。"""
ONE_COL = "__one"
"""沒有實體欄位的資料表以常數 1 代表一列。"""
FLIP = {"=": "=", "!=": "!=", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
NULL_TYPES = {
    "string": "VARCHAR",
    "integer": "BIGINT",
    "real": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
}


class ModelError(ValueError):
    """Relationship 模型的查詢尚不支援（一律視為不支援）。"""


@dataclass
class Usage:
    """檢視用到的實體欄位與資料表；value 為是否在彙總中使用。"""

    fields: dict[str, bool] = field(default_factory=dict)
    tables: dict[str, bool] = field(default_factory=dict)
    rows: set[str] = field(default_factory=set)
    """在彙總之外（列層級：維度、LOD 的維度、集合的層級…）用到的欄位。"""
    owned: dict[str, set[str]] = field(default_factory=dict)
    """量值表 → 在它的彙總中用到的欄位。"""

    def add_field(self, data_field: DataField, aggregated: bool, owner: str | None = None) -> None:
        self.fields[data_field.id] = self.fields.get(data_field.id, False) or aggregated
        if not aggregated:
            self.rows.add(data_field.id)
        elif owner is not None:
            self.owned.setdefault(owner, set()).add(data_field.id)
        if data_field.table is not None:
            self.add_table(data_field.table, aggregated)

    def add_table(self, table: str, aggregated: bool) -> None:
        self.tables[table] = self.tables.get(table, False) or aggregated

    def merge(self, other: Usage) -> None:
        for fid, aggregated in other.fields.items():
            self.fields[fid] = self.fields.get(fid, False) or aggregated
        for table, aggregated in other.tables.items():
            self.add_table(table, aggregated)
        self.rows |= other.rows
        for owner, fields in other.owned.items():
            self.owned.setdefault(owner, set()).update(fields)


@dataclass
class RowFilter:
    """列層級的篩選（資料源、內容、維度篩選）用到的欄位與資料表；條件在各運算階段才產生。"""

    id: str
    fields: set[str]
    tables: set[str]
    lod: bool = False
    """條件含 LOD 運算式。"""


@dataclass
class SemiJoin:
    """只用到其他資料表欄位的篩選：量值表的列保留「關聯到的列中有符合條件者」；沒有關聯到任何列時，
    其他資料表的欄位視為 Null 判斷條件（與左外連接後再篩選相同，例如排除某些值時保留沒有對應的列）。"""

    source: str
    """分支的子查詢（別名 s，以欄位 id 為欄名）。"""
    keys: str
    """關聯條件：量值表的欄位（外層的列）與 s 的欄位。"""
    nulls: str
    """分支的欄位都是 Null 的一列（別名 s）。"""

    def render(self, condition: str) -> str:
        matched = f"SELECT 1 FROM {self.source} WHERE {self.keys}"
        return (
            f"(EXISTS ({matched} AND ({condition})) OR (NOT EXISTS ({matched}) "
            f"AND EXISTS (SELECT 1 FROM {self.nulls} WHERE {condition})))"
        )


@dataclass
class Branch:
    """與量值表左外連接的分支：子資料表與它之下的資料表，依關聯欄位與用到的欄位去重。"""

    tables: list[str]
    keys: list[str]
    """子資料表一側的關聯欄位 id。"""
    needed: list[str]
    """檢視用到的欄位 id。"""
    distinct: str
    """SELECT DISTINCT 關聯欄位與用到的欄位。"""
    equality: bool
    """關聯條件都是等號（才能以關聯欄位是否唯一判斷一對多）。"""

    def guard_sql(self) -> str:
        """同一個關聯值對應多組欄位值的個數；0 表示量值表的一列最多對應一列（不會重複計算）。"""
        cols = ", ".join(ident(k) for k in self.keys)
        not_null = " AND ".join(f"{ident(k)} IS NOT NULL" for k in self.keys)
        return (
            f"SELECT COUNT(*) AS {ident('__n')} FROM (SELECT {cols} FROM ({self.distinct}) AS "
            f"{ident('g')} WHERE {not_null} GROUP BY {cols} HAVING COUNT(*) > 1) AS {ident('d')}"
        )


@dataclass
class Part:
    """一張量值表：以它為根連接其他資料表（分支）、套用篩選（半連接）。"""

    table: str
    semi: dict[str, SemiJoin]
    """以半連接套用的篩選：篩選 id → 條件的外框。"""
    branches: list[Branch]


@dataclass
class ModelPlan:
    from_sql: str
    """base CTE 的完整 SELECT（以欄位 id 為別名）；量值來自多張資料表時為各部分的 UNION ALL。"""
    tables: list[str]
    """查詢用到的邏輯資料表（量值表或第一張在前）。"""
    parts: list[Part]
    """量值表（只有維度時沒有）；兩個以上時 base 有 PART_COL 欄。"""
    notes: list[str]
    domain: str | None = None
    """檢視的維度與篩選只用到量值表以外的同一個分支時，維度的所有成員（內部連接）：
    Tableau 另以維度查詢補上沒有量值的成員（量值為 Null），見 ModelPlanner.domain。"""


class ModelPlanner:
    def __init__(self, ds: Datasource, model: ModelRelation) -> None:
        self.ds = ds
        self.model = model
        self.tables = {t.id: t for t in model.tables}
        # 自訂 SQL 的佔位資料表依邏輯資料表的順序編號（與 model_custom_sql 相同），
        # 不論這次查詢用到哪幾張資料表，編號都不變
        sql_tables = [t.id for t in model.tables if isinstance(t.relation, SqlRelation)]
        self.custom_index = {table: i for i, table in enumerate(sql_tables)}
        self.alias = {t.id: f"t{i}" for i, t in enumerate(model.tables)}
        self.fields = {f.id: f for f in ds.fields}
        self.columns: dict[str, list[DataField]] = {t.id: [] for t in model.tables}
        for data_field in ds.fields:
            if data_field.column is not None and data_field.table in self.columns:
                self.columns[data_field.table].append(data_field)
        self.edges: dict[str, list[tuple[str, Relationship]]] = {t.id: [] for t in model.tables}
        for rel in model.relationships:
            self.edges[rel.left].append((rel.right, rel))
            self.edges[rel.right].append((rel.left, rel))

    def caption(self, table: str) -> str:
        return self.tables[table].caption

    def names(self, tables: list[str]) -> str:
        return "、".join(f"「{self.caption(t)}」" for t in tables)

    def plan(
        self,
        usage: Usage,
        filters: list[RowFilter],
        dims: set[str] | None = None,
        others: set[str] | None = None,
        view: set[str] | None = None,
    ) -> ModelPlan:
        """usage：檢視用到的欄位（不含只用在篩選的欄位）；filters：列層級的篩選。

        dims、others：檢視的維度用到的資料表，與篩選（含 Top N）用到的資料表，用來判斷是否要補上
        沒有量值的維度成員（省略時不補）。view：檢視層級的彙總所屬的量值表（省略或空集合時為全部）。
        """
        wanted = set(usage.tables) | {t for f in filters for t in f.tables}
        used = [t.id for t in self.model.tables if t.id in wanted]
        if not used:
            used = [self.model.tables[0].id]
        if len(used) == 1:
            (table,) = used
            return ModelPlan(
                from_sql=self._single(table),
                tables=used,
                parts=[Part(table, {}, [])] if usage.tables.get(table) else [],
                notes=[f"Relationship 模型：只用到「{self.caption(table)}」"],
            )
        measures = [t for t in used if usage.tables.get(t)]
        if not measures:
            return self._dimensions(used)
        plan = (
            self._measure(measures[0], used, list(usage.fields), filters)
            if len(measures) == 1
            else self._parts(measures, used, usage, filters)
        )
        if dims is not None:
            # 檢視的彙總所屬的每張量值表都在維度的分支之外，才補上沒有量值的成員
            targets = [t for t in measures if view and t in view] or measures
            domains = {self.domain(t, dims, others or set()) for t in targets}
            if None not in domains:
                (plan.domain,) = domains
                plan.notes.append("維度的成員另以維度表查詢，沒有量值的成員也列出")
        return plan

    def domain(self, measure: str, dims: set[str], others: set[str]) -> str | None:
        """Tableau 在檢視有量值時另外查詢維度的成員，與量值的結果合併：維度表中沒有量值的成員也列出
        （量值為 Null）。維度與篩選用到的資料表都在量值表以外的同一個分支時，這些成員才可能不在量值的
        結果中；否則（例如維度含量值表的欄位）維度的組合都來自量值表的列，回傳 None。"""
        tables = dims | others
        if not dims or measure in tables:
            return None
        tree = self._tree(measure, [measure, *tables])
        children = [c for c, (p, _) in tree.items() if p == measure]
        if not any(tables <= set(self._subtree(c, tree)) for c in children):
            return None
        return self._dimensions([t.id for t in self.model.tables if t.id in tables]).from_sql

    # ------------------------------------------------------------ 樹

    def _tree(self, root: str, used: list[str]) -> dict[str, tuple[str, Relationship]]:
        """以 root 為根，連接 used 的最小子樹：子資料表 → （父資料表、關聯）。"""
        parent: dict[str, tuple[str, Relationship] | None] = {root: None}
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for other, rel in self.edges[node]:
                if other not in parent:
                    parent[other] = (node, rel)
                    queue.append(other)
        missing = [t for t in used if t not in parent]
        if missing:
            raise ModelError(f"Relationship 模型中「{self.caption(missing[0])}」沒有連到其他資料表")
        tree: dict[str, tuple[str, Relationship]] = {}
        for table in used:
            node = table
            while node != root and node not in tree:
                link = parent[node]
                assert link is not None
                tree[node] = link
                node = link[0]
        return tree

    def _order(self, root: str, tree: dict[str, tuple[str, Relationship]]) -> list[str]:
        """由根往外的順序（父資料表在子資料表之前；同一層依模型中資料表的順序）。"""
        rank = {t.id: i for i, t in enumerate(self.model.tables)}
        order = [root]
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for child in sorted(
                (c for c, (p, _) in tree.items() if p == node), key=rank.__getitem__
            ):
                order.append(child)
                queue.append(child)
        return order

    def _subtree(self, child: str, tree: dict[str, tuple[str, Relationship]]) -> list[str]:
        return self._order(child, {t: link for t, link in tree.items() if t != child})

    def _pairs(self, parent: str, rel: Relationship) -> list[tuple[DataField, str, DataField]]:
        """關聯條件，一律寫成（父資料表的欄位、運算子、子資料表的欄位）。"""
        result: list[tuple[DataField, str, DataField]] = []
        for cond in rel.on:
            if rel.left == parent:
                result.append((self.fields[cond.left], cond.op, self.fields[cond.right]))
            else:
                result.append((self.fields[cond.right], FLIP[cond.op], self.fields[cond.left]))
        return result

    def _on(
        self, parent: str, child: str, rel: Relationship, child_alias: str | None = None
    ) -> str:
        """父資料表與子資料表的連接條件；child_alias 為子資料表的去重結果（以欄位 id 參照）。"""
        parts: list[str] = []
        for p_field, op, c_field in self._pairs(parent, rel):
            assert p_field.column is not None and c_field.column is not None
            left = f"{ident(self.alias[parent])}.{ident(p_field.column)}"
            if child_alias is not None:
                right = f"{ident(child_alias)}.{ident(c_field.id)}"
            else:
                right = f"{ident(self.alias[child])}.{ident(c_field.column)}"
            parts.append(f"{left} {op} {right}")
        return " AND ".join(parts)

    # ------------------------------------------------------------ SQL

    def _relation(self, table: str) -> str:
        relation: Relation = self.tables[table].relation
        alias = ident(self.alias[table])
        if isinstance(relation, TableRelation):
            return f"{'.'.join(ident(p) for p in relation.table)} AS {alias}"
        if isinstance(relation, SqlRelation):
            placeholder = CUSTOM_SQL_PREFIX + str(self.custom_index[table])
            return f"{ident(placeholder)} AS {alias}"
        raise ModelError(f"邏輯資料表「{self.caption(table)}」內含 join，尚不支援")

    def _items(self, tables: list[str]) -> list[tuple[str, str]]:
        """資料表的實體欄位：（欄位 id、SQL）。"""
        return [
            (f.id, f"{ident(self.alias[t])}.{ident(f.column or '')}")
            for t in tables
            for f in self.columns[t]
        ] or [(ONE_COL, "1")]

    def _projections(self, tables: list[str]) -> list[str]:
        return [f"{sql} AS {ident(alias)}" for alias, sql in self._items(tables)]

    def _null(self, alias: str) -> str:
        datatype = self.fields[alias].datatype if alias in self.fields else "integer"
        return f"CAST(NULL AS {NULL_TYPES[datatype]})"

    def _single(self, table: str) -> str:
        return f"SELECT {', '.join(self._projections([table]))} FROM {self._relation(table)}"

    def _joined(self, root: str, tree: dict[str, tuple[str, Relationship]], kind: str) -> str:
        order = self._order(root, tree)
        sql = self._relation(root)
        for table in order[1:]:
            parent, rel = tree[table]
            sql += f" {kind} JOIN {self._relation(table)} ON {self._on(parent, table, rel)}"
        return sql

    def _inner(self, child: str, tree: dict[str, tuple[str, Relationship]]) -> str:
        """分支（子資料表與它之下用到的資料表）的實體欄位，以欄位 id 為欄名。"""
        subtree = set(self._subtree(child, tree))
        inner_tree = {t: link for t, link in tree.items() if t in subtree and t != child}
        order = self._order(child, inner_tree)
        return (
            f"SELECT {', '.join(self._projections(order))} "
            f"FROM {self._joined(child, inner_tree, 'LEFT')}"
        )

    def _dimensions(self, used: list[str]) -> ModelPlan:
        """只有維度：各資料表之間存在的組合（內部連接）；篩選直接套用在連接後的列。"""
        root = used[0]
        tree = self._tree(root, used)
        order = self._order(root, tree)
        sql = (
            f"SELECT {', '.join(self._projections(order))} FROM {self._joined(root, tree, 'INNER')}"
        )
        notes = [f"Relationship 模型：{self.names(order)}以內部連接取存在的組合"]
        return ModelPlan(sql, order, [], notes)

    def _measure(
        self, measure: str, used: list[str], needed: list[str], filters: list[RowFilter]
    ) -> ModelPlan:
        """量值只來自一張資料表。"""
        built = self._part(measure, used, needed, filters)
        selects = [f"{sql} AS {ident(alias)}" for alias, sql in built.items]
        notes = [f"Relationship 模型：量值在「{self.caption(measure)}」彙總", *built.notes]
        return ModelPlan(
            f"SELECT {', '.join(selects)} FROM {built.from_clause}",
            built.order,
            [built.part],
            notes,
        )

    def _parts(
        self, measures: list[str], used: list[str], usage: Usage, filters: list[RowFilter]
    ) -> ModelPlan:
        """量值來自多張資料表：每張量值表一部分，以 UNION ALL 合成 base（其他部分的欄位為 Null）。
        各部分要有列層級用到的欄位（維度、LOD 的維度…）與自己的彙總用到的欄位。"""
        built: list[_Built] = []
        tables: list[str] = []
        notes = [
            f"Relationship 模型：量值來自{self.names(measures)}，各自在所屬的資料表彙總，"
            "再依檢視的維度合併（完整外部連接）"
        ]
        for measure in measures:
            owned = usage.owned.get(measure, set())
            needed = [fid for fid in usage.fields if fid in usage.rows or fid in owned]
            part = self._part(measure, used, needed, filters)
            built.append(part)
            tables += [t for t in part.order if t not in tables]
            notes += [f"「{self.caption(measure)}」：{text}" for text in part.notes]
        columns: list[str] = []
        for part in built:
            columns += [alias for alias, _ in part.items if alias not in columns]
        selects: list[str] = []
        for index, part in enumerate(built):
            items = dict(part.items)
            picks = [f"{index} AS {ident(PART_COL)}"] + [
                f"{items[alias] if alias in items else self._null(alias)} AS {ident(alias)}"
                for alias in columns
            ]
            selects.append(f"SELECT {', '.join(picks)} FROM {part.from_clause}")
        return ModelPlan(" UNION ALL ".join(selects), tables, [p.part for p in built], notes)

    def _part(
        self, measure: str, used: list[str], fields: list[str], filters: list[RowFilter]
    ) -> _Built:
        """以 measure 為量值表：其他資料表依關聯欄位與 fields 中的欄位去重後左外連接，
        只用到其他資料表欄位的篩選以半連接套用。"""
        tree = self._tree(measure, used)
        order = self._order(measure, tree)
        children = [c for c in order[1:] if tree[c][0] == measure]
        subtrees = {c: self._subtree(c, tree) for c in children}
        items = self._items([measure])
        in_base = {f.id for f in self.columns[measure]}
        joins: list[str] = []
        branches: list[Branch] = []
        notes: list[str] = []
        for child in children:
            subtree = subtrees[child]
            rel = tree[child][1]
            needed = [
                self.fields[fid]
                for fid in fields
                if self.fields[fid].table in subtree and self.fields[fid].column is not None
            ]
            if not needed:
                continue  # 只用在篩選：以半連接套用
            keys = [c for _, _, c in self._pairs(measure, rel)]
            output = list({f.id: f for f in [*keys, *needed]}.values())
            alias = f"b{len(branches)}"
            distinct = (
                f"SELECT DISTINCT {', '.join(ident(f.id) for f in output)} "
                f"FROM ({self._inner(child, tree)}) AS {ident('s')}"
            )
            joins.append(
                f"LEFT JOIN ({distinct}) AS {ident(alias)} "
                f"ON {self._on(measure, child, rel, alias)}"
            )
            items += [(f.id, f"{ident(alias)}.{ident(f.id)}") for f in needed]
            in_base |= {f.id for f in needed}
            branches.append(
                Branch(
                    tables=subtree,
                    keys=[k.id for k in keys],
                    needed=[f.id for f in needed],
                    distinct=distinct,
                    equality=all(c.op == "=" for c in rel.on),
                )
            )
            notes.append(f"{self.names(subtree)}依關聯欄位與用到的欄位去重後左外連接")
        semi: dict[str, SemiJoin] = {}
        for flt in filters:
            if flt.fields <= in_base:
                continue  # 欄位都在連接結果中：直接套用
            owner = next((c for c in children if flt.tables <= set(subtrees[c])), None)
            if owner is None:
                raise ModelError(
                    "Relationship 模型中同時用到量值表與其他資料表（或多個分支）欄位的篩選尚不支援"
                )
            if flt.lod:
                raise ModelError("Relationship 模型中其他資料表含 LOD 運算式的篩選尚不支援")
            keys_sql = " AND ".join(
                f"{ident(p.id)} {op} {ident('s')}.{ident(c.id)}"
                for p, op, c in self._pairs(measure, tree[owner][1])
            )
            nulls = (
                ", ".join(
                    f"CAST(NULL AS {NULL_TYPES[f.datatype]}) AS {ident(f.id)}"
                    for t in subtrees[owner]
                    for f in self.columns[t]
                )
                or f"NULL AS {ident(ONE_COL)}"
            )
            semi[flt.id] = SemiJoin(
                f"({self._inner(owner, tree)}) AS {ident('s')}",
                keys_sql,
                f"(SELECT {nulls}) AS {ident('s')}",
            )
            notes.append(
                f"篩選 {flt.id} 只用到{self.names(subtrees[owner])}：保留有關聯且符合條件的列"
            )
        from_clause = " ".join([self._relation(measure), *joins])
        return _Built(Part(measure, semi, branches), items, from_clause, order, notes)


@dataclass
class _Built:
    """一部分的 SQL 片段：選出的欄位（欄位 id、SQL）與 FROM 子句。"""

    part: Part
    items: list[tuple[str, str]]
    from_clause: str
    order: list[str]
    notes: list[str]


__all__ = [
    "ONE_COL",
    "PART_COL",
    "Branch",
    "ModelError",
    "ModelPlan",
    "ModelPlanner",
    "Part",
    "RowFilter",
    "SemiJoin",
    "Usage",
]
