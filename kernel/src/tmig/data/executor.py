"""執行器（C2）：以唯讀模式開啟 DuckDB 檔，執行 SQL 產生器的輸出。

結果欄名就是 SQL 產生器給的欄位鍵（以 IR 欄位 id 組成，例如 ``sum(f_sales)``）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb


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


def execute(db_path: str | Path, sql: str) -> QueryResult:
    path = Path(db_path)
    if not path.is_file():
        raise ExecutionError(f"找不到資料檔 {path}；請先執行 tmig import-hyper")
    try:
        with duckdb.connect(str(path), read_only=True) as con:
            cursor = con.execute(sql)
            description = cursor.description or []
            columns = [d[0] for d in description]
            rows = cursor.fetchall()
    except duckdb.Error as exc:
        raise ExecutionError(f"SQL 執行失敗：{exc}") from exc
    return QueryResult(columns=columns, rows=[tuple(r) for r in rows])
