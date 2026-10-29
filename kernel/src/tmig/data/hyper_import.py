"""Hyper 匯入（C1）：把 .twbx 內的 .hyper 原封不動搬進 DuckDB。

一個資料源一個 DuckDB 檔：單表萃取是一張資料表；Relationship 模型（N6）的萃取每張邏輯資料表
一張資料表，全部匯入同一個檔，各自做匯入自檢。

計算比對時兩邊必須用同一份資料，所以：

- 型別一一對應；遇到沒有定義對應的型別直接報錯，不轉成文字
- 由 Hyper 匯出 Parquet，再由 DuckDB 以明確的型別讀入
- 匯入自檢：列數、每欄非空值數，數值欄的總和、最小值、最大值，日期與文字欄的最小值、最大值，
  兩邊逐一比對，不一致即失敗。浮點數總和因累加順序不同，允許 1e-9 的相對誤差
- 文字欄若使用非二進位定序（例如不分大小寫），DuckDB 的比較結果可能與 Tableau 不同，列為警告
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from tmig.ir.models import IR, DataField, ExtractConnection, TableRelation
from tmig.parser.workbook import parse_workbook

NUMERIC_TAGS = frozenset({"BIG_INT", "INT", "SMALL_INT", "NUMERIC", "DOUBLE", "FLOAT"})
ORDERED_TAGS = frozenset({"DATE", "TIMESTAMP", "TIMESTAMP_TZ", "TIME", "TEXT", "VARCHAR", "CHAR"})
DOUBLE_TAGS = frozenset({"DOUBLE", "FLOAT"})
SUM_REL_TOL = 1e-9


class HyperImportError(RuntimeError):
    pass


@dataclass
class ColumnInfo:
    name: str
    hyper_type: str
    duckdb_type: str
    collation: str | None


@dataclass
class StatCheck:
    column: str | None
    stat: str
    hyper: str | None
    duckdb: str | None
    ok: bool


@dataclass
class TableImport:
    schema: str
    table: str
    rows: int
    columns: list[ColumnInfo]
    checks: list[StatCheck]
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


@dataclass
class DatasourceImport:
    datasource: str
    hyper_file: str
    hyper_sha256: str
    duckdb: str
    table: TableImport
    other_tables: list[str]
    missing_columns: list[str]
    extra_tables: list[TableImport] = field(default_factory=list)
    """Relationship 模型的其他邏輯資料表（table 是第一張）。"""

    @property
    def tables(self) -> list[TableImport]:
        return [self.table, *self.extra_tables]

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables) and not self.missing_columns

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["table"]["ok"] = self.table.ok
        if self.extra_tables:
            for item, table in zip(data["extra_tables"], self.extra_tables, strict=True):
                item["ok"] = table.ok
        else:
            del data["extra_tables"]
        return data


def duckdb_type(sql_type: Any) -> str:
    """Hyper 的 SqlType → DuckDB 型別；沒有定義對應時丟出 HyperImportError。"""
    tag = sql_type.tag.name
    simple = {
        "BOOL": "BOOLEAN",
        "SMALL_INT": "SMALLINT",
        "INT": "INTEGER",
        "BIG_INT": "BIGINT",
        "DOUBLE": "DOUBLE",
        "FLOAT": "FLOAT",
        "TEXT": "VARCHAR",
        "VARCHAR": "VARCHAR",
        "CHAR": "VARCHAR",
        "JSON": "VARCHAR",
        "DATE": "DATE",
        "TIME": "TIME",
        "TIMESTAMP": "TIMESTAMP",
        "TIMESTAMP_TZ": "TIMESTAMPTZ",
        "INTERVAL": "INTERVAL",
    }
    if tag in simple:
        return simple[tag]
    if tag == "NUMERIC":
        return f"DECIMAL({sql_type.precision}, {sql_type.scale})"
    raise HyperImportError(f"Hyper 型別 {sql_type} 沒有定義對應的 DuckDB 型別")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _stat_sql(
    columns: list[ColumnInfo], tags: dict[str, str], table: str
) -> tuple[str, list[tuple[str | None, str]]]:
    parts = ["COUNT(*)"]
    labels: list[tuple[str | None, str]] = [(None, "rows")]
    for col in columns:
        q = _quote(col.name)
        tag = tags[col.name]
        parts.append(f"COUNT({q})")
        labels.append((col.name, "non_null"))
        if tag in NUMERIC_TAGS:
            parts += [f"SUM({q})", f"MIN({q})", f"MAX({q})"]
            labels += [(col.name, "sum"), (col.name, "min"), (col.name, "max")]
        elif tag in ORDERED_TAGS and (col.collation in (None, "", "binary")):
            parts += [f"MIN({q})", f"MAX({q})"]
            labels += [(col.name, "min"), (col.name, "max")]
    return f"SELECT {', '.join(parts)} FROM {table}", labels


def _plain(value: Any) -> Any:
    """Hyper 與 DuckDB 的值統一成可比較的 Python 值。"""
    if value is None or isinstance(value, (bool, int, float, Decimal, str)):
        return value
    for method in ("to_datetime", "to_date", "to_time"):
        convert = getattr(value, method, None)
        if convert is not None:
            return convert().isoformat()
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return str(value)


def _same(stat: str, tag: str, left: Any, right: Any) -> bool:
    a, b = _plain(left), _plain(right)
    if a is None or b is None:
        return a is None and b is None
    if stat == "sum" and tag in DOUBLE_TAGS:
        return math.isclose(float(a), float(b), rel_tol=SUM_REL_TOL, abs_tol=SUM_REL_TOL)
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        return Decimal(str(a)) == Decimal(str(b))
    return str(a) == str(b)


def _text(value: Any) -> str | None:
    plain = _plain(value)
    return None if plain is None else str(plain)


def import_datasource_hyper(
    hyper_path: Path, table_ref: str, out_path: Path, log_dir: Path
) -> tuple[TableImport, list[str]]:
    """把一個 .hyper 中指定的資料表匯入 out_path（DuckDB 檔），並做匯入自檢。"""
    parts = table_ref.split(".")
    if len(parts) != 2:
        raise HyperImportError(f"資料表名稱「{table_ref}」應為 schema.table")
    (table,), others = import_hyper_tables(hyper_path, [(parts[0], parts[1])], out_path, log_dir)
    return table, others


def import_hyper_tables(
    hyper_path: Path, refs: list[tuple[str, str]], out_path: Path, log_dir: Path
) -> tuple[list[TableImport], list[str]]:
    """把 .hyper 中的多張資料表（schema、table）匯入同一個 DuckDB 檔，各自做匯入自檢。

    回傳各資料表的匯入結果，與 .hyper 中沒有匯入的其他資料表。
    """
    from tableauhyperapi import (
        Connection,
        CreateMode,
        HyperProcess,
        TableName,
        Telemetry,
        escape_string_literal,
    )

    exported: list[tuple[str, str, list[ColumnInfo], dict[str, str], str, Any, Path]] = []
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, str(hyper_path), CreateMode.NONE) as conn,
    ):
        names = [
            (s.name.unescaped, t.name.unescaped)
            for s in conn.catalog.get_schema_names()
            for t in conn.catalog.get_table_names(s)
        ]
        listed = "、".join(f"{a}.{b}" for a, b in names)
        for i, (schema, table) in enumerate(refs):
            name = TableName(schema, table)
            if not conn.catalog.has_table(name):
                raise HyperImportError(f".hyper 內找不到資料表 {schema}.{table}（有：{listed}）")
            definition = conn.catalog.get_table_definition(name)
            columns: list[ColumnInfo] = []
            tags: dict[str, str] = {}
            for col in definition.columns:
                col_name = col.name.unescaped
                columns.append(
                    ColumnInfo(
                        name=col_name,
                        hyper_type=str(col.type),
                        duckdb_type=duckdb_type(col.type),
                        collation=col.collation or None,
                    )
                )
                tags[col_name] = col.type.tag.name
            hyper_table = f"{_quote(schema)}.{_quote(table)}"
            sql, labels = _stat_sql(columns, tags, hyper_table)
            hyper_stats = conn.execute_list_query(sql)[0]
            parquet = log_dir / f"table{i}.parquet"
            conn.execute_command(
                f"COPY {hyper_table} TO {escape_string_literal(str(parquet))} WITH (FORMAT PARQUET)"
            )
            exported.append((schema, table, columns, tags, sql, (labels, hyper_stats), parquet))

    tmp_out = out_path.with_name(out_path.name + ".tmp")
    tmp_out.unlink(missing_ok=True)
    results: list[TableImport] = []
    with duckdb.connect(str(tmp_out)) as con:
        for schema in dict.fromkeys(item[0] for item in exported):
            con.execute(f"CREATE SCHEMA {_quote(schema)}")
        for schema, table, columns, tags, sql, (labels, hyper_stats), parquet in exported:
            hyper_table = f"{_quote(schema)}.{_quote(table)}"
            select = ", ".join(
                f"CAST({_quote(c.name)} AS {c.duckdb_type}) AS {_quote(c.name)}" for c in columns
            )
            con.execute(
                f"CREATE TABLE {hyper_table} AS SELECT {select} FROM read_parquet(?)",
                [str(parquet)],
            )
            duck_stats = con.execute(sql).fetchone()
            assert duck_stats is not None
            checks = [
                StatCheck(
                    column=column,
                    stat=stat,
                    hyper=_text(h),
                    duckdb=_text(d),
                    ok=_same(stat, tags.get(column or "", ""), h, d),
                )
                for (column, stat), h, d in zip(labels, hyper_stats, duck_stats, strict=True)
            ]
            warnings = [
                f"欄位「{c.name}」使用定序 {c.collation}，文字比較與排序可能與 DuckDB 不同"
                for c in columns
                if c.collation not in (None, "", "binary")
            ]
            results.append(
                TableImport(
                    schema=schema,
                    table=table,
                    rows=int(str(hyper_stats[0])),
                    columns=columns,
                    checks=checks,
                    warnings=warnings,
                )
            )
    os.replace(tmp_out, out_path)
    imported = set(refs)
    return results, [f"{a}.{b}" for a, b in names if (a, b) not in imported]


def extract_table_refs(connection: ExtractConnection) -> list[tuple[str, str]]:
    """萃取要匯入的資料表：單表萃取一張；Relationship 模型每張邏輯資料表一張。"""
    if connection.model is None:
        parts = connection.table.split(".")
        if len(parts) != 2:
            raise HyperImportError(f"資料表名稱「{connection.table}」應為 schema.table")
        return [(parts[0], parts[1])]
    refs: list[tuple[str, str]] = []
    for logical in connection.model.tables:
        relation = logical.relation
        if not isinstance(relation, TableRelation) or len(relation.table) != 2:
            raise HyperImportError(f"邏輯資料表「{logical.caption}」的萃取資料表應為 schema.table")
        refs.append((relation.table[0], relation.table[1]))
    return refs


def _missing_columns(
    connection: ExtractConnection, fields: list[DataField], tables: list[TableImport]
) -> list[str]:
    """IR 的實體欄位不在萃取資料表中（Relationship 模型依欄位所屬的邏輯資料表檢查）。"""
    if connection.model is None:
        names = {c.name for c in tables[0].columns}
        return sorted(
            f"{f.id}（{f.column}）"
            for f in fields
            if f.column is not None and f.column not in names
        )
    by_table = {
        logical.id: {c.name for c in table.columns}
        for logical, table in zip(connection.model.tables, tables, strict=True)
    }
    return sorted(
        f"{f.id}（{f.column}）"
        for f in fields
        if f.column is not None and f.column not in by_table.get(f.table or "", set())
    )


def import_workbook(
    twbx: str | Path, out_dir: str | Path, ir: IR | None = None
) -> list[DatasourceImport]:
    """匯入 .twbx 內每個資料源的萃取：<out_dir>/<資料源 id>.duckdb 與 .import.json。"""
    twbx_path = Path(twbx)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    workbook_ir = ir if ir is not None else parse_workbook(twbx_path)
    results: list[DatasourceImport] = []
    with zipfile.ZipFile(twbx_path) as archive, tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for ds in workbook_ir.datasources:
            if not isinstance(ds.connection, ExtractConnection):
                continue  # Live 連線的資料在來源資料庫，不在 .twbx 內
            member = ds.connection.hyper_file
            try:
                data = archive.read(member)
            except KeyError as exc:
                raise HyperImportError(f"{twbx_path.name} 內找不到 {member}") from exc
            hyper_path = work / f"{ds.id}.hyper"
            hyper_path.write_bytes(data)
            log_dir = work / ds.id
            log_dir.mkdir()
            out_path = target / f"{ds.id}.duckdb"
            refs = extract_table_refs(ds.connection)
            tables, others = import_hyper_tables(hyper_path, refs, out_path, log_dir)
            result = DatasourceImport(
                datasource=ds.id,
                hyper_file=member,
                hyper_sha256=hashlib.sha256(data).hexdigest(),
                duckdb=out_path.name,
                table=tables[0],
                other_tables=others,
                missing_columns=_missing_columns(ds.connection, ds.fields, tables),
                extra_tables=tables[1:],
            )
            # 重新匯入快照：先前從來源更新的紀錄不再適用
            (target / f"{ds.id}.refresh.json").unlink(missing_ok=True)
            report = target / f"{ds.id}.import.json"
            report.write_text(
                json.dumps(result.to_json(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            results.append(result)
    return results
