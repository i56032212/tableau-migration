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
每個彙總只計算所屬部分的列（見 tmig.sqlgen.query 的 WorksheetQuery.owner_of）。

跨資料表的計算（同一個計算用到多張資料表的欄位，例如 SUM([Qty] * [Price])）：與 Tableau 相同，計算的
定義域是它的輸入的內部連接（Tableau 說明文件「How Analysis Works for Data Sources that Use Relationships」
的疑難排解：The domain of a calculation is the inner join of its inputs）——用到的資料表（與連接它們的
路徑上的資料表）以關聯內部連接，在連接後的每一列計算，沒有對應的列不參與。彙總的「量值表」因此是這幾張
資料表內部連接後的列（複合的量值表，以 compound_key 表示）：它是一部分，其他資料表照樣依關聯欄位去重後
左外連接、篩選照樣以半連接套用，與其他量值表的部分照樣依維度合併。
列層級的跨資料表計算（當維度、篩選或 LOD 的維度）在量值表的每一列計算：用到的資料表都有對應的列時才有
值，否則為 Null（定義域之外；量值表沒有對應的列照樣保留在 Null，與 Relationship 保留沒有對應的值相同）。
是否有對應的列以標記欄（PRESENT_PREFIX，見 ModelPlan.markers）判斷。

邏輯資料表內含 join（實體層）：邏輯資料表是 join 樹的子查詢，各欄以欄位 id 為欄名（各實體資料表的同名
欄位不衝突），之後與一般的資料表相同；join 在邏輯資料表之內先做，關聯再連接邏輯資料表（與 Tableau 相同）。

關聯計算（關聯條件的一邊是公式，DataField.relationship_calc）：在所屬的邏輯資料表上以公式計算的欄，
與實體欄位一樣用在連接條件與去重；公式只能用到這張資料表的實體欄位。

篩選用到的欄位不全在連接結果中時以半連接套用：連接結果中沒有的欄位所在的分支各取「有關聯的列」（沒有
關聯時是一列 Null），量值表的列保留「有一組這些列使條件成立者」；條件中量值表（與已在連接結果中）的
欄位直接取量值表這一列的值。同時用到量值表與其他資料表、或多個分支的欄位的篩選（跨資料表的計算）因此
與只用到一個分支的篩選相同處理。

尚不支援（查詢時列為不支援）：含 LOD 且只能以半連接套用的篩選（LOD 的值在量值表的每一列上，半連接中
其他資料表的列沒有這個值）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import product
from typing import NoReturn

from tmig.data import udf
from tmig.formula.analyze import Target
from tmig.formula.ast import Call, Lod
from tmig.ir.models import (
    DataField,
    Datasource,
    JoinRelation,
    LiveConnection,
    ModelRelation,
    PivotRelation,
    ProjectRelation,
    Relation,
    Relationship,
    SqlRelation,
    TableRelation,
    UnionRelation,
)
from tmig.sqlgen.source import (
    CUSTOM_SQL_PREFIX,
    datetime_columns_vary,
    from_clause,
    relation_aliases,
    sql_leaves,
    transform_select,
)
from tmig.sqlgen.sql import ident

PART_COL = "__part"
"""量值來自多張資料表時，base 的每一列屬於哪一部分（量值表在 ModelPlan.parts 中的索引）。"""
ONE_COL = "__one"
"""沒有實體欄位的資料表以常數 1 代表一列。"""
COMPOUND_SEP = "\x1f"
"""複合量值表（跨資料表的計算）的鍵中分隔各資料表 id 的字元（XML 的屬性值中不會出現）。"""
PRESENT_PREFIX = "__in_"
"""標記欄的前綴：base 的這一列有該資料表的列時為 1，沒有時為 Null（跨資料表的計算只在都有時計算）。
不用布林值：Oracle 等資料庫沒有布林型別。"""
MARKER_TYPE = "INTEGER"
FLIP = {"=": "=", "!=": "!=", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
NULL_TYPES = {
    "string": "VARCHAR",
    "integer": "BIGINT",
    "real": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "spatial": "VARCHAR",
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
    owners: dict[str, None] = field(default_factory=dict)
    """彙總所屬的量值表（依出現的順序；跨資料表的計算是複合的量值表，見 ModelPlanner.compound_key）。"""
    present: set[str] = field(default_factory=set)
    """列層級的跨資料表計算用到的資料表：要知道量值表的每一列是否有它們的列（見 ModelPlan.markers）。"""

    def add_field(self, data_field: DataField, aggregated: bool, owner: str | None = None) -> None:
        self.fields[data_field.id] = self.fields.get(data_field.id, False) or aggregated
        if not aggregated:
            self.rows.add(data_field.id)
        elif owner is not None:
            self.owned.setdefault(owner, set()).add(data_field.id)
        if data_field.table is not None:
            self.add_table(data_field.table, aggregated, owner)

    def add_table(self, table: str, aggregated: bool, owner: str | None = None) -> None:
        self.tables[table] = self.tables.get(table, False) or aggregated
        if aggregated and owner is not None:
            self.owners.setdefault(owner, None)

    def merge(self, other: Usage) -> None:
        for fid, aggregated in other.fields.items():
            self.fields[fid] = self.fields.get(fid, False) or aggregated
        for table, aggregated in other.tables.items():
            self.add_table(table, aggregated)
        self.rows |= other.rows
        for owner, fields in other.owned.items():
            self.owned.setdefault(owner, set()).update(fields)
        for owner in other.owners:
            self.owners.setdefault(owner, None)
        self.present |= other.present


@dataclass
class RowFilter:
    """列層級的篩選（資料源、內容、維度篩選）用到的欄位與資料表；條件在各運算階段才產生。"""

    id: str
    fields: set[str]
    tables: set[str]
    lod: bool = False
    """條件含 LOD 運算式。"""


@dataclass
class SemiSide:
    """半連接中的一個分支。"""

    source: str
    """分支的子查詢（別名 s；兩個以上的分支時為 s0、s1…，以欄位 id 為欄名）。"""
    keys: str
    """關聯條件：量值表的欄位（外層的列）與分支的欄位。"""
    nulls: str
    """分支的欄位都是 Null 的一列（與 source 相同的別名）。"""


@dataclass
class SemiJoin:
    """欄位不全在連接結果中的篩選：量值表的列保留「關聯到的列中有符合條件者」；沒有關聯到任何列時，
    該分支的欄位視為 Null 判斷條件（與左外連接後再篩選相同，例如排除某些值時保留沒有對應的列）。
    用到多個分支時，各分支的列（或沒有關聯時的一列 Null）的每一種組合都可以使條件成立；條件中的其他
    欄位（量值表的、已在連接結果中的）取外層這一列的值。"""

    sides: list[SemiSide]

    def render(self, condition: str) -> str:
        if len(self.sides) == 1:
            (side,) = self.sides
            matched = f"SELECT 1 FROM {side.source} WHERE {side.keys}"
            return (
                f"(EXISTS ({matched} AND ({condition})) OR (NOT EXISTS ({matched}) "
                f"AND EXISTS (SELECT 1 FROM {side.nulls} WHERE {condition})))"
            )
        # 每個分支「有關聯」或「沒有關聯（一列 Null）」的每一種組合；關聯條件都只參照外層的一列
        # （不在 FROM 的子查詢中參照外層，各資料庫都能執行）
        terms: list[str] = []
        for matched in product((True, False), repeat=len(self.sides)):
            sources = [s.source if m else s.nulls for s, m in zip(self.sides, matched, strict=True)]
            keys = [s.keys for s, m in zip(self.sides, matched, strict=True) if m]
            where = " AND ".join([*keys, f"({condition})"])
            parts = [f"EXISTS (SELECT 1 FROM {', '.join(sources)} WHERE {where})"]
            parts += [
                f"NOT EXISTS (SELECT 1 FROM {s.source} WHERE {s.keys})"
                for s, m in zip(self.sides, matched, strict=True)
                if not m
            ]
            terms.append("(" + " AND ".join(parts) + ")")
        return "(" + " OR ".join(terms) + ")"


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
    """量值表的 id；跨資料表的計算是複合的鍵（見 ModelPlanner.compound_key、members）。"""
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
    markers: dict[str, str] = field(default_factory=dict)
    """列層級的跨資料表計算用到的資料表 → base（與 domain）中的標記欄：這一列有該資料表的列時為 1
    （量值表本身一律為 1；左外連接沒有對應時為 Null）。只有維度時都是內部連接，沒有標記欄。"""


class ModelPlanner:
    def __init__(self, ds: Datasource, model: ModelRelation) -> None:
        self.ds = ds
        self.model = model
        self.tables = {t.id: t for t in model.tables}
        # 自訂 SQL 的佔位資料表依邏輯資料表的順序（內含 join 時依 join 樹中的順序）編號（與
        # model_custom_sql 相同），不論這次查詢用到哪幾張資料表，編號都不變
        self.custom_index: dict[str, int] = {}
        count = 0
        for logical in model.tables:
            self.custom_index[logical.id] = count
            count += len(sql_leaves(logical.relation))
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
        # 關聯計算：關聯條件用到的計算欄位，在所屬的邏輯資料表上計算
        linked = {fid for rel in model.relationships for c in rel.on for fid in (c.left, c.right)}
        self.computed: dict[str, list[DataField]] = {t.id: [] for t in model.tables}
        for data_field in ds.fields:
            if (
                data_field.id in linked
                and data_field.column is None
                and data_field.calc is not None
                and data_field.table in self.computed
            ):
                self.computed[data_field.table].append(data_field)
        self.present: set[str] = set()
        """這次查詢需要標記欄的資料表（Usage.present）。"""

    def caption(self, table: str) -> str:
        return self.tables[table].caption

    def names(self, tables: list[str]) -> str:
        return "、".join(f"「{self.caption(t)}」" for t in tables)

    # ------------------------------------------------------------ 跨資料表的計算

    def compound_key(self, tables: list[str]) -> str:
        """一個計算用到的資料表 → 它的量值表：一張時是那張資料表；多張時是連接它們的最小子樹（含路徑上
        的資料表，依模型中的順序）內部連接後的列，以 COMPOUND_SEP 相接的鍵表示。"""
        if len(tables) == 1:
            return tables[0]
        tree = self._tree(tables[0], tables)
        inside = {tables[0], *tree}
        return COMPOUND_SEP.join(t.id for t in self.model.tables if t.id in inside)

    @staticmethod
    def members(owner: str) -> list[str]:
        """量值表（或複合的量值表）包含的資料表。"""
        return owner.split(COMPOUND_SEP)

    def label(self, owner: str) -> str:
        """量值表的稱呼：「Orders」；複合的量值表是「Orders」與「Products」的內部連接。"""
        tables = self.members(owner)
        if len(tables) == 1:
            return f"「{self.caption(tables[0])}」"
        return f"{self.names(tables[:-1])}與「{self.caption(tables[-1])}」的內部連接"

    def marker(self, table: str) -> str:
        return PRESENT_PREFIX + self.alias[table]

    # ------------------------------------------------------------ 規劃

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
        self.present = set(usage.present)
        wanted = set(usage.tables) | {t for f in filters for t in f.tables} | self.present
        wanted |= {t for owner in usage.owners for t in self.members(owner)}
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
        measures = self._owners(usage, used)
        if not measures:
            return self._dimensions(used)
        plan = (
            self._measure(measures[0], used, list(usage.fields), filters)
            if len(measures) == 1
            else self._parts(measures, used, usage, filters)
        )
        plan.markers = {t: self.marker(t) for t in used if t in self.present}
        if dims is not None:
            # 檢視的彙總所屬的每張量值表都在維度的分支之外，才補上沒有量值的成員
            targets = [t for t in measures if view and t in view] or measures
            domains = {self.domain(t, dims, others or set()) for t in targets}
            if None not in domains:
                (plan.domain,) = domains
                plan.notes.append("維度的成員另以維度表查詢，沒有量值的成員也列出")
        return plan

    def _owners(self, usage: Usage, used: list[str]) -> list[str]:
        """各部分的量值表，依模型中資料表的順序（複合的量值表排在它的第一張資料表之後）。"""
        owners = list(usage.owners)
        covered = {t for owner in owners for t in self.members(owner)}
        owners += [t for t in used if usage.tables.get(t) and t not in covered]
        rank = {t.id: i for i, t in enumerate(self.model.tables)}
        return sorted(owners, key=lambda o: [rank[t] for t in self.members(o)])

    def domain(self, measure: str, dims: set[str], others: set[str]) -> str | None:
        """Tableau 在檢視有量值時另外查詢維度的成員，與量值的結果合併：維度表中沒有量值的成員也列出
        （量值為 Null）。維度與篩選用到的資料表都在量值表以外的同一個分支時，這些成員才可能不在量值的
        結果中；否則（例如維度含量值表的欄位）維度的組合都來自量值表的列，回傳 None。
        複合的量值表（跨資料表的計算）：內部連接後的列不含其中各資料表的所有列，維度在哪張資料表都
        可能有不在結果中的成員，一律另外查詢（推定：與換成其他量值時列出的成員相同）。"""
        roots = self.members(measure)
        tables = dims | others
        chosen = [t.id for t in self.model.tables if t.id in tables]
        if dims and len(roots) > 1:
            return self._dimensions(chosen, markers=True).from_sql
        if not dims or tables & set(roots):
            return None
        tree = self._tree(roots, [*roots, *tables])
        children = [c for c, (p, _) in tree.items() if p in roots]
        if not any(tables <= set(self._subtree(c, tree)) for c in children):
            return None
        return self._dimensions(chosen, markers=True).from_sql

    # ------------------------------------------------------------ 樹

    def _tree(self, root: str | list[str], used: list[str]) -> dict[str, tuple[str, Relationship]]:
        """以 root（複合的量值表時為其中各資料表）為根，連接 used 的最小子樹：子資料表 → （父資料表、
        關聯）。根本身不在結果中。"""
        roots = [root] if isinstance(root, str) else root
        parent: dict[str, tuple[str, Relationship] | None] = dict.fromkeys(roots)
        queue = deque(roots)
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
            while node not in roots and node not in tree:
                link = parent[node]
                assert link is not None
                tree[node] = link
                node = link[0]
        return tree

    def _order(self, root: str | list[str], tree: dict[str, tuple[str, Relationship]]) -> list[str]:
        """由根往外的順序（父資料表在子資料表之前；同一層依模型中資料表的順序）。"""
        rank = {t.id: i for i, t in enumerate(self.model.tables)}
        order = [root] if isinstance(root, str) else list(root)
        queue = deque(order)
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
            left = self._col(parent, p_field)
            if child_alias is not None:
                right = f"{ident(child_alias)}.{ident(c_field.id)}"
            else:
                right = self._col(child, c_field)
            parts.append(f"{left} {op} {right}")
        return " AND ".join(parts)

    def _col(self, table: str, data_field: DataField) -> str:
        """邏輯資料表的實體欄位；內含 join 的資料表（子查詢）以欄位 id 為欄名。關聯計算為公式的 SQL。"""
        if data_field.column is None:
            return self._computed(table, data_field)
        joined = isinstance(self.tables[table].relation, JoinRelation)
        name = data_field.id if joined else data_field.column
        return f"{ident(self.alias[table])}.{ident(name)}"

    def _computed(self, table: str, data_field: DataField) -> str:
        """關聯計算的 SQL：公式中的欄位是這張邏輯資料表的實體欄位。"""
        from tmig.formula.analyze import Analyzer
        from tmig.formula.lexer import FormulaSyntaxError
        from tmig.formula.parser import parse_formula
        from tmig.formula.types import FormulaSemanticError
        from tmig.ir.scope import DatasourceScope
        from tmig.sqlgen.expr import ExprCompiler

        if data_field.calc is None or data_field.table != table:
            raise ModelError(f"「{data_field.caption}」不是「{self.caption(table)}」的欄位")
        try:
            node = parse_formula(data_field.calc.formula)
            analyzer = Analyzer(DatasourceScope.for_datasource(self.ds, []))
            return "(" + ExprCompiler(analyzer, _LinkEnv(self, table)).row(node) + ")"
        except (FormulaSyntaxError, FormulaSemanticError) as exc:
            raise ModelError(f"關聯計算「{data_field.calc.formula}」：{exc}") from exc

    # ------------------------------------------------------------ SQL

    def _relation(self, table: str) -> str:
        relation: Relation = self.tables[table].relation
        alias = ident(self.alias[table])
        if isinstance(relation, TableRelation):
            return f"{'.'.join(ident(p) for p in relation.table)} AS {alias}"
        if isinstance(relation, SqlRelation):
            placeholder = CUSTOM_SQL_PREFIX + str(self.custom_index[table])
            return f"{ident(placeholder)} AS {alias}"
        if isinstance(relation, PivotRelation | UnionRelation | ProjectRelation):
            # 樞紐、聯集與 project：子查詢依欄名選出這張邏輯資料表的欄位
            needed = {str(f.column): f.datatype for f in self.columns[table]}
            custom: list[str] = [""] * self.custom_index[table]
            return f"({transform_select(relation, custom, needed)}) AS {alias}"
        assert isinstance(relation, JoinRelation)
        return f"({self._join_sql(table, relation)}) AS {alias}"

    def _join_sql(self, table: str, relation: JoinRelation) -> str:
        """內含 join 的邏輯資料表：join 樹的子查詢，各欄以欄位 id 為欄名。"""
        fields = self.columns[table]
        missing = [f.caption for f in fields if f.physical_table is None]
        if missing:
            raise ModelError(
                f"邏輯資料表「{self.caption(table)}」的欄位「{missing[0]}」沒有標明所屬的實體資料表"
            )
        columns = {(str(f.physical_table), str(f.column)): f.datatype for f in fields}
        # 之前的資料表的自訂 SQL 佔位：join 樹中的自訂 SQL 接著編號
        custom: list[str] = [""] * self.custom_index[table]
        from_sql = from_clause(relation, custom, columns, self._live)
        aliases = relation_aliases(relation)
        items = [
            f"{ident(aliases.get(str(f.physical_table), str(f.physical_table)))}."
            f"{ident(str(f.column))} AS {ident(f.id)}"
            for f in fields
        ] or [f"1 AS {ident(ONE_COL)}"]
        return f"SELECT {', '.join(items)} FROM {from_sql}"

    @property
    def _live(self) -> bool:
        return isinstance(self.ds.connection, LiveConnection)

    def _items(self, tables: list[str]) -> list[tuple[str, str]]:
        """資料表的實體欄位：（欄位 id、SQL）。BigQuery 的日期時間欄位轉成沒有時區的時間（見
        tmig.sqlgen.source.datetime_columns_vary）；Live 連線的空間欄位在來源資料庫轉成 WKT。"""
        cast = datetime_columns_vary(self.ds.connection)
        items: list[tuple[str, str]] = []
        for t in tables:
            for f in self.columns[t]:
                sql = self._col(t, f)
                if cast and f.datatype == "datetime":
                    sql = f"CAST({sql} AS TIMESTAMP)"
                if self._live and f.datatype == "spatial":
                    sql = f"{udf.GEO_WKT}({sql})"
                items.append((f.id, sql))
            items += [(f.id, self._computed(t, f)) for f in self.computed[t]]
        return items or [(ONE_COL, "1")]

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
        """分支（子資料表與它之下用到的資料表）的實體欄位，以欄位 id 為欄名；需要標記欄的資料表另有
        標記欄（子資料表一律為 1，其下的資料表依左外連接是否有對應）。"""
        subtree = set(self._subtree(child, tree))
        inner_tree = {t: link for t, link in tree.items() if t in subtree and t != child}
        order = self._order(child, inner_tree)
        markers = [
            f"{self._matched(t, inner_tree)} AS {ident(self.marker(t))}"
            for t in order
            if t in self.present
        ]
        return (
            f"SELECT {', '.join([*self._projections(order), *markers])} "
            f"FROM {self._joined(child, inner_tree, 'LEFT')}"
        )

    def _matched(self, table: str, tree: dict[str, tuple[str, Relationship]]) -> str:
        """左外連接的結果中這張資料表是否有對應的列：關聯條件中它的欄位不是 Null（等號與不等號的條件
        都不會因 Null 成立）時為 1，否則為 Null。根一律為 1。"""
        if table not in tree:
            return "1"
        parent, rel = tree[table]
        _, _, c_field = self._pairs(parent, rel)[0]
        return f"(CASE WHEN {self._col(table, c_field)} IS NOT NULL THEN 1 END)"

    def _dimensions(self, used: list[str], markers: bool = False) -> ModelPlan:
        """只有維度：各資料表之間存在的組合（內部連接）；篩選直接套用在連接後的列。markers：另選出
        標記欄（都為 1；補上維度成員的查詢與工作表的查詢共用編譯好的條件）。"""
        root = used[0]
        tree = self._tree(root, used)
        order = self._order(root, tree)
        items = self._projections(order)
        if markers:
            items += [f"1 AS {ident(self.marker(t))}" for t in order if t in self.present]
        sql = f"SELECT {', '.join(items)} FROM {self._joined(root, tree, 'INNER')}"
        notes = [f"Relationship 模型：{self.names(order)}以內部連接取存在的組合"]
        return ModelPlan(sql, order, [], notes)

    def _measure(
        self, measure: str, used: list[str], needed: list[str], filters: list[RowFilter]
    ) -> ModelPlan:
        """量值只來自一張資料表（或一個跨資料表的計算的內部連接）。"""
        built = self._part(measure, used, needed, filters)
        selects = [f"{sql} AS {ident(alias)}" for alias, sql in built.items]
        notes = [f"Relationship 模型：量值在{self.label(measure)}彙總", *built.notes]
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
            f"Relationship 模型：量值來自{'、'.join(self.label(m) for m in measures)}，"
            "各自在所屬的資料表彙總，再依檢視的維度合併（完整外部連接）"
        ]
        for measure in measures:
            owned = usage.owned.get(measure, set())
            needed = [fid for fid in usage.fields if fid in usage.rows or fid in owned]
            part = self._part(measure, used, needed, filters)
            built.append(part)
            tables += [t for t in part.order if t not in tables]
            notes += [f"{self.label(measure)}：{text}" for text in part.notes]
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
        欄位不全在連接結果中的篩選以半連接套用。複合的量值表（跨資料表的計算）先以關聯內部連接
        其中的各資料表（不去重：計算在內部連接後的每一列進行），其他資料表接在其中各自相連的資料表上。"""
        roots = self.members(measure)
        tree = self._tree(roots, used)
        order = self._order(roots, tree)
        children = [c for c in order[len(roots) :] if tree[c][0] in roots]
        subtrees = {c: self._subtree(c, tree) for c in children}
        items = self._items(roots)
        in_base = {f.id for t in roots for f in [*self.columns[t], *self.computed[t]]}
        joins: list[str] = []
        branches: list[Branch] = []
        notes: list[str] = []
        flags: dict[str, str] = {t: "1" for t in roots if t in self.present}
        """需要標記欄的資料表 → 在 base 中的 SQL。"""
        for child in children:
            subtree = subtrees[child]
            parent, rel = tree[child]
            needed = [
                self.fields[fid]
                for fid in fields
                if self.fields[fid].table in subtree and self.fields[fid].column is not None
            ]
            if not needed:
                continue  # 只用在篩選：以半連接套用
            keys = [c for _, _, c in self._pairs(parent, rel)]
            output = list({f.id: f for f in [*keys, *needed]}.values())
            marked = [t for t in subtree if t in self.present]
            alias = f"b{len(branches)}"
            columns = [ident(f.id) for f in output] + [ident(self.marker(t)) for t in marked]
            distinct = (
                f"SELECT DISTINCT {', '.join(columns)} "
                f"FROM ({self._inner(child, tree)}) AS {ident('s')}"
            )
            joins.append(
                f"LEFT JOIN ({distinct}) AS {ident(alias)} ON {self._on(parent, child, rel, alias)}"
            )
            items += [(f.id, f"{ident(alias)}.{ident(f.id)}") for f in needed]
            flags |= {t: f"{ident(alias)}.{ident(self.marker(t))}" for t in marked}
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
        # 標記欄：沒有連接的分支（只用在篩選，半連接中另有標記欄）為 Null
        items += [
            (self.marker(t.id), flags.get(t.id, f"CAST(NULL AS {MARKER_TYPE})"))
            for t in self.model.tables
            if t.id in self.present
        ]
        semi: dict[str, SemiJoin] = {}
        for flt in filters:
            if flt.fields <= in_base:
                continue  # 欄位都在連接結果中：直接套用
            if flt.lod:
                raise ModelError(
                    "Relationship 模型中含 LOD 運算式、又用到不在檢視中的其他資料表欄位的篩選尚不支援"
                )
            # 連接結果中沒有的欄位所在的分支：各取有關聯的列（沒有時一列 Null）
            missing = {self.fields[f].table for f in flt.fields - in_base if f in self.fields}
            owners = [c for c in children if missing & set(subtrees[c])]
            if not owners:
                raise ModelError("Relationship 模型中篩選用到的欄位不在這次查詢的資料表中")
            sides: list[SemiSide] = []
            for owner in owners:
                alias = ident("s" if len(owners) == 1 else f"s{len(sides)}")
                parent, rel = tree[owner]
                keys_sql = " AND ".join(
                    f"{ident(p.id)} {op} {alias}.{ident(c.id)}"
                    for p, op, c in self._pairs(parent, rel)
                )
                nulls = [
                    f"CAST(NULL AS {NULL_TYPES[f.datatype]}) AS {ident(f.id)}"
                    for t in subtrees[owner]
                    for f in self.columns[t]
                ] + [
                    f"CAST(NULL AS {MARKER_TYPE}) AS {ident(self.marker(t))}"
                    for t in subtrees[owner]
                    if t in self.present
                ]
                sides.append(
                    SemiSide(
                        f"({self._inner(owner, tree)}) AS {alias}",
                        keys_sql,
                        f"(SELECT {', '.join(nulls) or f'NULL AS {ident(ONE_COL)}'}) AS {alias}",
                    )
                )
            semi[flt.id] = SemiJoin(sides)
            names = "、".join(self.names(subtrees[o]) for o in owners)
            only = len(owners) == 1 and flt.tables <= set(subtrees[owners[0]])
            notes.append(
                f"篩選 {flt.id} {'只' if only else ''}用到{names}：保留有關聯且符合條件的列"
            )
        if len(roots) == 1:
            from_clause = " ".join([self._relation(measure), *joins])
        else:
            inner = self._joined(roots[0], self._tree(roots[0], roots), "INNER")
            from_clause = " ".join([inner, *joins])
            notes.insert(
                0, f"{self.names(roots)}以關聯內部連接（跨資料表的計算在連接後的每一列計算）"
            )
        return _Built(Part(measure, semi, branches), items, from_clause, order, notes)


@dataclass
class _Built:
    """一部分的 SQL 片段：選出的欄位（欄位 id、SQL）與 FROM 子句。"""

    part: Part
    items: list[tuple[str, str]]
    from_clause: str
    order: list[str]
    notes: list[str]


class _LinkEnv:
    """關聯計算的公式：欄位是邏輯資料表的實體欄位；不能用參數、彙總、LOD 與 Table Calculation。"""

    week_start = "sunday"

    def __init__(self, planner: ModelPlanner, table: str) -> None:
        self.planner = planner
        self.table = table

    def column(self, target: Target) -> str:
        found = self.planner.fields.get(target.id)
        if found is None or found.table != self.table or found.column is None:
            caption = found.caption if found is not None else target.id
            raise ModelError(
                f"關聯計算只能用到「{self.planner.caption(self.table)}」的欄位（「{caption}」不是）"
            )
        return self.planner._col(self.table, found)

    def _reject(self) -> NoReturn:
        raise ModelError("關聯計算不能用到參數、彙總、LOD 或 Table Calculation")

    def param(self, target: Target) -> str:
        self._reject()

    def param_value(self, target: Target) -> object:
        self._reject()

    def lod(self, node: Lod) -> str:
        self._reject()

    def aggregate(self, node: Call) -> str:
        self._reject()

    def table_calc(self, node: Call) -> str:
        self._reject()


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
    "SemiSide",
    "Usage",
]
