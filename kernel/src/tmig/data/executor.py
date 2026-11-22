"""執行器（C2）：以唯讀模式開啟 DuckDB 檔，執行 SQL 產生器的輸出。

結果欄名就是 SQL 產生器給的欄位鍵（以 IR 欄位 id 組成，例如 ``sum(f_sales)``）。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from tmig.data import udf
from tmig.ir.models import FileConnection, LiveConnection, is_federated
from tmig.sqlgen import rawsql
from tmig.sqlgen.query import BlendStage, Guard, OutputColumn, QueryError, WorksheetSql


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]

    def to_csv(self) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(self.columns)
        for row in self.rows:
            writer.writerow([format_value(v) for v in row])
        return buffer.getvalue()


def format_value(value: Any) -> str:
    """CSV 與 JSON 用的值：NULL 為空字串，日期為 ISO 格式，浮點數保留完整精度。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def execute(
    db_path: str | Path,
    sql: str,
    tables: dict[str, pa.Table] | None = None,
) -> QueryResult:
    """tables：執行前登記的資料表（名稱 → 資料），例如 Data Blending 的次要資料源的查詢結果。"""
    path = Path(db_path)
    if not path.is_file():
        raise ExecutionError(f"找不到資料檔 {path}；請先執行 tmig import-hyper")
    rawsql.forbid_local(sql, "萃取與檔案的資料")
    try:
        # 用到 ICU 的正規表示式（或要登記資料表）時另開記憶體資料庫附加資料檔（見 tmig.data.udf）
        connection = (
            udf.attached(path)
            if udf.needed(sql) or tables
            else duckdb.connect(str(path), read_only=True)
        )
        with connection as con:
            for name, table in (tables or {}).items():
                con.register(name, table)
            cursor = con.execute(sql)
            description = cursor.description or []
            columns = [d[0] for d in description]
            rows = cursor.fetchall()
    except duckdb.Error as exc:
        raise ExecutionError(f"SQL 執行失敗：{exc}") from exc
    return QueryResult(columns=columns, rows=[tuple(r) for r in rows])


def execute_query(
    db_path: str | Path,
    query: WorksheetSql,
    tables: dict[str, pa.Table] | None = None,
) -> QueryResult:
    """執行一張工作表的查詢（萃取的 DuckDB 檔）：先做查詢附帶的檢查，再執行查詢。

    Data Blending 的次要資料源的查詢（WorksheetSql.stages）先執行，結果登記成資料表：tables 省略時在同一
    目錄中以資料源 id 為檔名的 DuckDB 檔執行（萃取與檔案；次要資料源在資料庫中時要以 DataRuntime 執行）。"""
    if tables is None:
        folder = Path(db_path).parent
        tables = stage_tables(query, lambda stage: _local_stage(folder, stage))
    for guard in query.guards:
        check_guard(guard, execute(db_path, guard.sql).rows)
    return finish(query, execute(db_path, query.sql, tables))


def _local_stage(folder: Path, stage: BlendStage) -> QueryResult:
    connection = stage.datasource.connection
    if isinstance(connection, LiveConnection) or (
        isinstance(connection, FileConnection) and is_federated(connection)
    ):
        raise ExecutionError(
            f"Data Blending 的次要資料源「{stage.datasource.caption}」在資料庫中，要以 DataRuntime 執行"
        )
    return execute_query(folder / f"{stage.datasource.id}.duckdb", stage.query)


def stage_tables(
    query: WorksheetSql, run: Callable[[BlendStage], QueryResult]
) -> dict[str, pa.Table]:
    """Data Blending 的次要資料源的查詢結果（各段的名稱 → 資料表）。"""
    return {stage.name: result_table(stage.query.columns, run(stage)) for stage in query.stages}


ARROW_TYPES: dict[str, pa.DataType] = {
    "string": pa.string(),
    "integer": pa.int64(),
    "real": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "datetime": pa.timestamp("us"),
    "spatial": pa.string(),
}
"""輸出欄的型別 → Arrow 的型別（分開執行的結果交給 DuckDB 合併時，整欄都是 Null 的欄用）。"""


def result_table(columns: list[OutputColumn], result: QueryResult) -> pa.Table:
    """查詢結果 → Arrow 的資料表（型別依值判斷；DECIMAL 轉成浮點數，整欄 Null 時依輸出欄的型別）。"""
    declared = {c.key: c.datatype for c in columns}
    arrays: list[pa.Array] = []
    for index, name in enumerate(result.columns):
        values = [row[index] for row in result.rows]
        values = [float(v) if isinstance(v, Decimal) else v for v in values]
        if all(v is None for v in values):
            kind = ARROW_TYPES.get(declared.get(name, "string"), pa.string())
            arrays.append(pa.array(values, type=kind))
            continue
        try:
            arrays.append(pa.array(values))
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arrays.append(pa.array([None if v is None else str(v) for v in values]))
    return pa.Table.from_arrays(arrays, names=list(result.columns))


def finish(query: WorksheetSql, result: QueryResult) -> QueryResult:
    """查詢之後的分析：叢集編號、預測的估計值與預測區間（tmig.planner.analytics）。"""
    if query.analytics is None:
        return result
    from tmig.planner.analytics import apply

    columns, rows = apply(query.analytics, list(result.columns), list(result.rows))
    return QueryResult(columns=columns, rows=rows)


def check_guard(guard: Guard, rows: list[tuple[Any, ...]] | list[list[Any]]) -> None:
    """檢查的結果大於 0：這個查詢給不出與 Tableau 相同的數字，列為不支援。"""
    value = rows[0][0] if rows and rows[0] else 0
    if value is not None and int(value) > 0:
        raise QueryError(guard.message, unsupported=True)
