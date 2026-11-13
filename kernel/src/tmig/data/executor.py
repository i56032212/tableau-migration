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
from tmig.sqlgen.query import Guard, OutputColumn, QueryError, WorksheetSql
from tmig.sqlgen.sql import ident, string


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
    attach: dict[str, Path] | None = None,
    prepare: Callable[[duckdb.DuckDBPyConnection], None] | None = None,
) -> QueryResult:
    """attach：先以唯讀附加的其他 DuckDB 檔（附加的名稱 → 檔案），例如 Data Blending 的次要資料源；
    prepare：執行前在連線上的準備（例如跨資料庫的次要資料源，見 execute_query）。"""
    path = Path(db_path)
    for item in [path, *(attach or {}).values()]:
        if not item.is_file():
            raise ExecutionError(f"找不到資料檔 {item}；請先執行 tmig import-hyper")
    try:
        # 用到 ICU 的正規表示式（或要在預設的資料庫之外建立資料表）時另開記憶體資料庫附加資料檔
        # （見 tmig.data.udf）
        connection = (
            udf.attached(path)
            if udf.needed(sql) or prepare is not None
            else duckdb.connect(str(path), read_only=True)
        )
        with connection as con:
            for alias, other in (attach or {}).items():
                con.execute(f"ATTACH {string(str(other))} AS {ident(alias)} (READ_ONLY)")
            if prepare is not None:
                prepare(con)
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
    fetch: Callable[..., Any] | None = None,
    cache: Any = None,
) -> QueryResult:
    """執行一張工作表的查詢（萃取的 DuckDB 檔）：先做查詢附帶的檢查，再執行查詢。

    Data Blending 的次要資料源是同一目錄中以資料源 id 為檔名的 DuckDB 檔；次要資料源是跨資料庫的 join 時，
    資料庫的資料表以 fetch 取回（tmig.data.federated.attach_federated，cache 為取回的資料的快取）。"""
    if query.stages:
        raise ExecutionError(
            "Data Blending 的資料源有 Live 連線（或 Relationship 模型），要以 DataRuntime 分開執行"
        )
    for guard in query.guards:
        check_guard(guard, execute(db_path, guard.sql).rows)
    folder = Path(db_path).parent
    if query.federated:
        from tmig.data.federated import attach_secondaries

        def prepare(con: duckdb.DuckDBPyConnection) -> None:
            attach_secondaries(con, folder, query, fetch, cache)

        return finish(query, execute(db_path, query.sql, None, prepare))
    attach = {alias: folder / f"{ds}.duckdb" for alias, ds in query.attach.items()}
    return finish(query, execute(db_path, query.sql, attach))


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


def combine(
    query: WorksheetSql, parts: dict[str, tuple[list[OutputColumn], QueryResult]]
) -> QueryResult:
    """Data Blending 分開執行的各段（WorksheetSql.stages）：結果以各段的名稱交給記憶體中的 DuckDB，
    執行合併的 SELECT（query.main，依連結欄位 LEFT JOIN）。"""
    con = duckdb.connect()
    try:
        for name, (columns, result) in parts.items():
            con.register(name, result_table(columns, result))
        cursor = con.execute(query.main)
        description = cursor.description or []
        rows = cursor.fetchall()
    except duckdb.Error as exc:
        raise ExecutionError(f"Data Blending 合併失敗：{exc}") from exc
    finally:
        con.close()
    return finish(
        query, QueryResult(columns=[d[0] for d in description], rows=[tuple(r) for r in rows])
    )


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
