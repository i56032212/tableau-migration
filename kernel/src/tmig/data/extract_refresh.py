"""萃取層（第三階段 N7）：從來源資料庫重建萃取的 DuckDB 檔，並與 Tableau 的萃取比對（階段二）。

查詢服務只讀 DuckDB 檔；萃取型資料源由萃取層定時從來源更新：

- 完整更新：依萃取各欄在來源的位置（ExtractConnection.refresh：metadata-record 的 family 與
  remote-alias）組出查詢、套用萃取篩選，轉成來源資料庫的方言執行；結果寫入新的暫存檔，全部完成後
  原子替換（os.replace），正在進行的查詢讀到的是舊檔或新檔，不會讀到一半；失敗時舊檔不變
- 增量更新：只取增量欄位（Tableau 的 increment-key）大於現有最大值的列，附加在現有資料之後，
  與 Tableau 的增量更新相同；同樣寫在暫存檔再替換
- 欄位型別沿用現有檔案（由 .hyper 匯入時決定，與 Tableau 的萃取一致）；沒有現有檔案時依 IR 的資料型別
- Relationship 模型的萃取：每張邏輯資料表各自從它在來源的資料表重建（同一個 DuckDB 檔）

資料比對（階段二，compare_extract）：新萃取與 Tableau 的萃取逐表比對列數，以及每欄的非空值數、
數值欄的總和、最小值、最大值，日期與文字欄的最小值、最大值。浮點數總和允許 1e-9 的相對誤差。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from tmig.data.hyper_import import cast_column
from tmig.data.sources import Executor
from tmig.formula.ast import FieldRef, walk
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.ir.models import (
    IR,
    DataField,
    Datasource,
    ExtractConnection,
    Filter,
    LiveConnection,
    Marks,
    ModelRelation,
    Relation,
    Scalar,
    SourceColumn,
    TableRelation,
    TupleFilter,
    Worksheet,
)
from tmig.sqlgen.dialect import ParameterValues, to_dialect
from tmig.sqlgen.query import QueryError, WorksheetQuery, custom_sql_parameters
from tmig.sqlgen.source import SourceError, datasource_source
from tmig.sqlgen.sql import SQL_TYPES, ident, literal

NUMERIC_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "REAL")
ORDERED_TYPES = ("DATE", "TIMESTAMP", "TIME", "VARCHAR")
SUM_REL_TOL = 1e-9


class RefreshError(RuntimeError):
    """萃取無法從來源更新（來源不明、只取部分資料、增量欄位不存在等）。"""


# ---------------------------------------------------------------- 計畫


@dataclass
class TablePlan:
    """萃取中的一張資料表：在來源資料庫執行的查詢（DuckDB 方言，含自訂 SQL 的佔位資料表）。"""

    schema: str
    table: str
    base: str
    """來源的實體欄位，以欄位 id 為別名（與 Live 連線的 base 相同）。"""
    columns: list[tuple[str, str]]
    """萃取中的欄名與欄位 id，依輸出順序。"""
    conditions: list[str]
    """萃取篩選。"""
    custom_sql: list[str] = field(default_factory=list)
    """自訂 SQL 原文，依佔位資料表的編號。"""
    increment: str | None = None
    """增量欄位在萃取中的欄名。"""
    increment_field: str | None = None
    datatypes: dict[str, str] = field(default_factory=dict)
    """萃取中的欄名 → IR 的資料型別。"""
    limit: int | None = None
    """只取前 N 筆（編輯器的設定）。"""
    aggregates: dict[str, str] | None = None
    """彙總可見維度的資料：欄位 id → 彙總函式（SUM、MIN、MAX）；None 為不彙總。其餘的欄位（nulls 除外）
    是分組的維度。"""
    nulls: set[str] = field(default_factory=set)
    """彙總時不保留的欄位（隱藏的欄位）：萃取中是 Null。"""

    def sql(self, after: Scalar | None = None) -> str:
        """after：增量更新時現有資料中增量欄位的最大值。"""
        conds = list(self.conditions)
        if after is not None:
            assert self.increment is not None and self.increment_field is not None
            datatype = self.datatypes.get(self.increment, "string")
            conds.append(f"{ident(self.increment_field)} > {literal(after, datatype)}")
        where = f" WHERE {' AND '.join(f'({c})' for c in conds)}" if conds else ""
        group = ""
        if self.aggregates is None:
            selects = ", ".join(f"{ident(fid)} AS {ident(name)}" for name, fid in self.columns)
        else:
            parts: list[str] = []
            keys: list[str] = []
            for name, fid in self.columns:
                if fid in self.nulls:
                    kind = SQL_TYPES[self.datatypes.get(name, "string")]
                    parts.append(f"CAST(NULL AS {kind}) AS {ident(name)}")
                elif fid in self.aggregates:
                    parts.append(f"{self.aggregates[fid]}({ident(fid)}) AS {ident(name)}")
                else:
                    parts.append(f"{ident(fid)} AS {ident(name)}")
                    keys.append(ident(fid))
            selects = ", ".join(parts)
            group = f" GROUP BY {', '.join(keys)}" if keys else ""
        limit = f" LIMIT {self.limit}" if self.limit is not None else ""
        return f"SELECT {selects} FROM ({self.base}) AS {ident('base')}{where}{group}{limit}"


@dataclass
class RefreshPlan:
    datasource: str
    source: LiveConnection
    tables: list[TablePlan]
    parameters: ParameterValues
    """自訂 SQL 中參數的值（預設值，與 Tableau 建立萃取時相同）。"""

    @property
    def incremental(self) -> bool:
        return any(t.increment is not None for t in self.tables)


def refresh_plan(ir: IR, datasource_id: str) -> RefreshPlan:
    """資料源的萃取在來源資料庫重建時要執行的查詢；不能更新時丟出 RefreshError。"""
    ds = next((d for d in ir.datasources if d.id == datasource_id), None)
    if ds is None:
        raise RefreshError(f"找不到資料源「{datasource_id}」")
    connection = ds.connection
    if not isinstance(connection, ExtractConnection):
        raise RefreshError(
            f"資料源「{ds.caption}」是 Live 連線，查詢時直接連來源資料庫，不需要更新萃取"
        )
    source, refresh = connection.source, connection.refresh
    if source is None or refresh is None or source.relation is None:
        raise RefreshError(
            f"資料源「{ds.caption}」的萃取來源無法解析（例如來源是檔案，或跨多個資料庫）"
        )
    if source.dialect is None:
        raise RefreshError(f"資料源「{ds.caption}」的來源連線類型 {source.dbclass} 尚不支援")
    if refresh.row_limit is not None:
        raise RefreshError(
            f"資料源「{ds.caption}」的萃取只取前 {refresh.row_limit} 筆（或抽樣），無法重現同樣的資料"
        )
    fields = {f.id: f for f in ds.fields}
    by_field = {c.field: c for c in refresh.columns}
    extract_filters = [f for f in ds.filters if f.stage == "extract"]
    tables: list[TablePlan] = []
    if connection.model is None:
        parts = connection.table.split(".")
        if len(parts) != 2:
            raise RefreshError(f"萃取的資料表名稱「{connection.table}」應為 schema.table")
        if isinstance(source.relation, ModelRelation):
            raise RefreshError(f"資料源「{ds.caption}」的萃取與來源的資料模型不一致")
        group = [fields[c.field] for c in refresh.columns]
        tables.append(
            _table(ir, ds, source, source.relation, parts, group, by_field, extract_filters)
        )
    else:
        if not isinstance(source.relation, ModelRelation):
            raise RefreshError(f"資料源「{ds.caption}」的萃取與來源的資料模型不一致")
        sources = {t.id: t.relation for t in source.relation.tables}
        placed: set[str] = set()
        for logical in connection.model.tables:
            target = logical.relation
            assert isinstance(target, TableRelation)
            group = [
                fields[c.field] for c in refresh.columns if fields[c.field].table == logical.id
            ]
            mine = [f for f in extract_filters if _filter_fields(f) <= {g.id for g in group}]
            placed |= {f.id for f in mine}
            tables.append(
                _table(ir, ds, source, sources[logical.id], target.table, group, by_field, mine)
            )
        stray = [f.id for f in extract_filters if f.id not in placed]
        if stray:
            raise RefreshError(
                f"資料源「{ds.caption}」的萃取篩選 {'、'.join(stray)} 跨多張邏輯資料表，尚不支援"
            )
    if refresh.increment is not None:
        owner = [t for t in tables if any(fid == refresh.increment for _, fid in t.columns)]
        for table in owner:
            name = next(n for n, fid in table.columns if fid == refresh.increment)
            table.increment, table.increment_field = name, refresh.increment
    if refresh.top_rows is not None:
        for table in tables:
            table.limit = refresh.top_rows
    if refresh.aggregate and connection.model is None:
        _aggregate(ds, tables[0])
    return RefreshPlan(
        datasource=ds.id, source=source, tables=tables, parameters=custom_sql_parameters(ir)
    )


AGGREGATED = {"sum": "SUM", "min": "MIN", "max": "MAX"}
"""彙總可見維度的資料時先彙總的量值：預設彙總是可以再彙總的（總和的總和仍是總和）；其他的預設彙總
（平均、計數等）先彙總之後再彙總會算錯，這些量值留在詳細資料（當作分組的維度）。"""


def _aggregate(ds: Datasource, table: TablePlan) -> None:
    """彙總可見維度的資料：依沒有隱藏的維度分組，量值依預設的彙總先彙總；隱藏的欄位不保留（Null），
    但查詢時還會用到的隱藏欄位仍當作維度保留——沒有隱藏的計算欄位與集合的公式用到的、集合的層級，
    以及資料源篩選（查詢時才套用）的欄位。"""
    referenced: set[str] = set()
    kept: set[str] = set()
    for other in ds.fields:
        if other.hidden:
            continue
        formulas = [other.calc.formula] if other.calc is not None else []
        if other.set is not None:
            kept |= {u.field for u in other.set.levels}
            rule = other.set.rule
            if rule.kind == "condition":
                formulas.append(rule.condition)
            elif rule.kind == "top_n":
                formulas.append(rule.by)
        for text in formulas:
            try:
                node = parse_formula(text)
            except FormulaSyntaxError:
                continue
            referenced |= {n.parts[-1] for n in walk(node) if isinstance(n, FieldRef)}
    for flt in ds.filters:
        if flt.stage != "extract":
            kept |= _filter_fields(flt)
    fields = {f.id: f for f in ds.fields}
    aggregates: dict[str, str] = {}
    nulls: set[str] = set()
    for _, fid in table.columns:
        data_field = fields[fid]
        used = (
            fid in kept
            or data_field.caption in referenced
            or data_field.name.strip("[]") in referenced
        )
        if data_field.hidden and not used:
            nulls.add(fid)
        elif data_field.role == "measure" and data_field.default_agg in AGGREGATED:
            aggregates[fid] = AGGREGATED[data_field.default_agg]
    table.aggregates, table.nulls = aggregates, nulls


def _filter_fields(flt: Filter) -> set[str]:
    uses = flt.fields if isinstance(flt, TupleFilter) else [flt.field]
    return {u.field for u in uses}


def _table(
    ir: IR,
    ds: Datasource,
    source: LiveConnection,
    relation: Relation,
    target: list[str],
    group: list[DataField],
    by_field: dict[str, SourceColumn],
    filters: list[Filter],
) -> TablePlan:
    """一張萃取資料表的查詢：把萃取各欄換成來源的欄位，視為一個 Live 資料源產生 base 與篩選條件。"""
    if len(target) != 2:
        raise RefreshError(f"萃取的資料表「{'.'.join(target)}」應為 schema.table")
    virtual_fields = [
        f.model_copy(update={"column": by_field[f.id].column, "table": by_field[f.id].relation})
        for f in group
    ]
    # 計算欄位（萃取篩選可能用到）保留；其他資料表的實體欄位不在這張資料表
    calcs = [f for f in ds.fields if f.column is None]
    virtual = ds.model_copy(
        update={
            "connection": source.model_copy(update={"relation": relation}),
            "fields": [*virtual_fields, *calcs],
            "filters": [],
        }
    )
    try:
        base = datasource_source(virtual)
    except SourceError as exc:
        raise RefreshError(f"資料源「{ds.caption}」的萃取來源：{exc}") from exc
    holder = Worksheet(
        id="refresh",
        name="refresh",
        datasource=ds.id,
        marks=Marks(type="automatic"),
        source_ref="refresh",
    )
    virtual_ir = ir.model_copy(update={"datasources": [virtual], "worksheets": [holder]})
    try:
        conditions = [c for c in WorksheetQuery(virtual_ir, "refresh").row_conditions(filters) if c]
    except QueryError as exc:
        raise RefreshError(f"資料源「{ds.caption}」的萃取篩選：{exc}") from exc
    assert all(f.column is not None for f in group)
    return TablePlan(
        schema=target[0],
        table=target[1],
        base=base.from_sql,
        columns=[(f.column or "", f.id) for f in group],
        conditions=conditions,
        custom_sql=list(base.custom_sql),
        datatypes={f.column or "": f.datatype for f in group},
    )


# ---------------------------------------------------------------- 更新


@dataclass
class TableRefresh:
    schema: str
    table: str
    rows: int
    added: int


@dataclass
class RefreshResult:
    datasource: str
    mode: str
    duckdb: str
    source: str
    started: str
    finished: str
    tables: list[TableRefresh]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def refresh_extract(
    ir: IR,
    datasource_id: str,
    executor: Executor,
    out_path: Path,
    *,
    incremental: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> RefreshResult:
    """從來源重建 out_path（萃取的 DuckDB 檔）；incremental 時只附加新的列。

    寫入暫存檔，全部完成後原子替換；任何一步失敗，out_path 維持原狀。
    """
    now = clock or (lambda: datetime.now(UTC))
    started = now()
    plan = refresh_plan(ir, datasource_id)
    existing = out_path if out_path.is_file() else None
    if incremental:
        if not plan.incremental:
            raise RefreshError(f"資料源「{datasource_id}」的萃取沒有設定增量更新的欄位")
        if existing is None:
            raise RefreshError(f"增量更新需要現有的萃取檔 {out_path}；請先完整更新或匯入 .hyper")
    types = _existing_types(existing, plan) if existing is not None else {}
    collations = _import_collations(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".refresh-tmp")
    wal = tmp.with_name(tmp.name + ".wal")
    tmp.unlink(missing_ok=True)
    wal.unlink(missing_ok=True)
    results: list[TableRefresh] = []
    try:
        if incremental:
            assert existing is not None
            shutil.copyfile(existing, tmp)
        with duckdb.connect(str(tmp)) as con:
            for table in plan.tables:
                results.append(
                    _write_table(con, plan, table, executor, types, incremental, collations)
                )
            con.execute("CHECKPOINT")
        _replace(tmp, out_path)
    finally:
        tmp.unlink(missing_ok=True)
        wal.unlink(missing_ok=True)
    source = plan.source
    result = RefreshResult(
        datasource=plan.datasource,
        mode="incremental" if incremental else "full",
        duckdb=out_path.name,
        source="／".join(p for p in (source.dbclass, source.server, source.database) if p),
        started=started.isoformat(),
        finished=now().isoformat(),
        tables=results,
    )
    report = out_path.with_name(f"{plan.datasource}.refresh.json")
    report.write_text(
        json.dumps(result.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def _write_table(
    con: duckdb.DuckDBPyConnection,
    plan: RefreshPlan,
    table: TablePlan,
    executor: Executor,
    types: dict[tuple[str, str], dict[str, str]],
    incremental: bool,
    collations: dict[tuple[str, str], dict[str, str]] | None = None,
) -> TableRefresh:
    target = f"{ident(table.schema)}.{ident(table.table)}"
    known = types.get((table.schema, table.table), {})
    after: Scalar | None = None
    before = 0
    if incremental:
        before = _count(con, target)
        if table.increment is not None:
            # 一律取完結果：DuckDB 的連線有未取完的結果時不能再做其他事
            (row,) = con.execute(f"SELECT max({ident(table.increment)}) FROM {target}").fetchall()
            after = row[0]
    sql = to_dialect(table.sql(after), executor.dialect, table.custom_sql, plan.parameters)
    try:
        rows = executor.fetch(sql)
    except Exception as exc:
        raise RefreshError(f"來源資料庫執行失敗（{table.schema}.{table.table}）：{exc}") from exc
    # 沿用匯入 .hyper 時的定序（大小寫不分的文字欄位，見 tmig.data.hyper_import.cast_column）
    collated = (collations or {}).get((table.schema, table.table), {})
    casts = ", ".join(
        cast_column(name, known.get(name) or SQL_TYPES[table.datatypes[name]], collated.get(name))
        for name, _ in table.columns
    )
    con.register("__tmig_rows", rows)
    try:
        if incremental and (table.increment is not None or before == 0):
            names = ", ".join(ident(name) for name, _ in table.columns)
            con.execute(f"INSERT INTO {target} ({names}) SELECT {casts} FROM __tmig_rows")
        else:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {ident(table.schema)}")
            con.execute(f"DROP TABLE IF EXISTS {target}")
            con.execute(f"CREATE TABLE {target} AS SELECT {casts} FROM __tmig_rows")
    finally:
        con.unregister("__tmig_rows")
    total = _count(con, target)
    return TableRefresh(table.schema, table.table, rows=total, added=total - before)


def _count(con: duckdb.DuckDBPyConnection, target: str) -> int:
    (row,) = con.execute(f"SELECT count(*) FROM {target}").fetchall()
    return int(row[0])


def _import_collations(out_path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """匯入 .hyper 時記下的各表各欄的定序（<資料源>.import.json；沒有時為空）。"""
    report = out_path.with_name(f"{out_path.stem}.import.json")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    tables = data.get("tables") or ([data["table"]] if data.get("table") else [])
    result: dict[tuple[str, str], dict[str, str]] = {}
    for table in tables:
        key = (str(table.get("schema")), str(table.get("table")))
        result[key] = {
            str(c["name"]): str(c["collation"])
            for c in table.get("columns", [])
            if c.get("collation")
        }
    return result


def _existing_types(path: Path, plan: RefreshPlan) -> dict[tuple[str, str], dict[str, str]]:
    """現有萃取檔各表各欄的型別（沿用 .hyper 匯入時的型別）。"""
    result: dict[tuple[str, str], dict[str, str]] = {}
    with duckdb.connect(str(path), read_only=True) as con:
        for table in plan.tables:
            rows = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ?",
                [table.schema, table.table],
            ).fetchall()
            result[(table.schema, table.table)] = {str(n): str(t) for n, t in rows}
    return result


def _replace(tmp: Path, target: Path, attempts: int = 20) -> None:
    """原子替換；Windows 上目標檔正被讀取時稍候重試。"""
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)


# ---------------------------------------------------------------- 比對（階段二）


@dataclass
class ColumnCheck:
    column: str | None
    stat: str
    reference: str | None
    refreshed: str | None
    ok: bool


@dataclass
class TableCompare:
    schema: str
    table: str
    checks: list[ColumnCheck]
    missing: list[str]
    """Tableau 萃取有、新萃取沒有的欄（IR 沒有用到的欄，不影響查詢）。"""

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


@dataclass
class CompareResult:
    datasource: str
    tables: list[TableCompare]

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        for item, table in zip(data["tables"], self.tables, strict=True):
            item["ok"] = table.ok
        return data


def compare_extract(ir: IR, datasource_id: str, refreshed: Path, reference: Path) -> CompareResult:
    """新萃取（refreshed）與 Tableau 萃取（reference，例如 .hyper 匯入的 DuckDB 檔）逐表比對。"""
    ds = next((d for d in ir.datasources if d.id == datasource_id), None)
    if ds is None or not isinstance(ds.connection, ExtractConnection):
        raise RefreshError(f"找不到萃取型的資料源「{datasource_id}」")
    connection = ds.connection
    targets: list[list[str]] = []
    if connection.model is None:
        targets.append(connection.table.split("."))
    else:
        for logical in connection.model.tables:
            assert isinstance(logical.relation, TableRelation)
            targets.append(logical.relation.table)
    tables: list[TableCompare] = []
    with (
        duckdb.connect(str(reference), read_only=True) as ref,
        duckdb.connect(str(refreshed), read_only=True) as new,
    ):
        for schema, table in targets:
            ref_cols = _columns(ref, schema, table)
            new_cols = _columns(new, schema, table)
            common = [(name, kind) for name, kind in ref_cols if name in dict(new_cols)]
            missing = [name for name, _ in ref_cols if name not in dict(new_cols)]
            sql, labels = _stat_sql(common, f"{ident(schema)}.{ident(table)}")
            (ref_row,) = ref.execute(sql).fetchall()
            (new_row,) = new.execute(sql).fetchall()
            kinds = dict(common)
            checks = [
                ColumnCheck(
                    column=column,
                    stat=stat,
                    reference=_text(a),
                    refreshed=_text(b),
                    ok=_same(stat, kinds.get(column or "", ""), a, b),
                )
                for (column, stat), a, b in zip(labels, ref_row, new_row, strict=True)
            ]
            tables.append(TableCompare(schema, table, checks, missing))
    return CompareResult(datasource=ds.id, tables=tables)


def _columns(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> list[tuple[str, str]]:
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, table],
    ).fetchall()
    if not rows:
        raise RefreshError(f"萃取檔中沒有資料表 {schema}.{table}")
    return [(str(n), str(t)) for n, t in rows]


def _stat_sql(
    columns: list[tuple[str, str]], table: str
) -> tuple[str, list[tuple[str | None, str]]]:
    parts = ["count(*)"]
    labels: list[tuple[str | None, str]] = [(None, "rows")]
    for name, kind in columns:
        q = ident(name)
        parts.append(f"count({q})")
        labels.append((name, "non_null"))
        if kind.startswith(NUMERIC_TYPES) or kind.startswith("DECIMAL"):
            parts += [f"sum({q})", f"min({q})", f"max({q})"]
            labels += [(name, "sum"), (name, "min"), (name, "max")]
        elif kind.startswith(ORDERED_TYPES):
            parts += [f"min({q})", f"max({q})"]
            labels += [(name, "min"), (name, "max")]
    return f"SELECT {', '.join(parts)} FROM {table}", labels


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, Decimal, str)):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if isoformat is not None else str(value)


def _text(value: Any) -> str | None:
    plain = _plain(value)
    return None if plain is None else str(plain)


def _same(stat: str, kind: str, left: Any, right: Any) -> bool:
    a, b = _plain(left), _plain(right)
    if a is None or b is None:
        return a is None and b is None
    if stat == "sum" and kind.startswith(("DOUBLE", "FLOAT", "REAL")):
        return math.isclose(float(a), float(b), rel_tol=SUM_REL_TOL, abs_tol=SUM_REL_TOL)
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        with contextlib.suppress(ArithmeticError):
            return Decimal(str(a)) == Decimal(str(b))
    return str(a) == str(b)


__all__ = [
    "CompareResult",
    "RefreshError",
    "RefreshPlan",
    "RefreshResult",
    "TableCompare",
    "TablePlan",
    "TableRefresh",
    "compare_extract",
    "refresh_extract",
    "refresh_plan",
]
