"""每張工作表的 SQL 快照（D1）：SQL 有任何變動都要人工審核。

更新快照：TMIG_UPDATE_SNAPSHOTS=1 uv run pytest kernel/tests/sqlgen/test_snapshots.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlglot

from tmig.ir import load_ir
from tmig.sqlgen.query import worksheet_sql

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = sorted((ROOT / "fixtures").glob("*/golden.ir.json"))
CASES = [(path, ws.id) for path in GOLDEN for ws in load_ir(path).worksheets]


@pytest.mark.parametrize(
    ("golden", "worksheet"), CASES, ids=[f"{p.parent.name}/{w}" for p, w in CASES]
)
def test_sql_snapshot(golden: Path, worksheet: str) -> None:
    sql = worksheet_sql(load_ir(golden), worksheet).sql
    snapshot = golden.parent / "sql" / f"{worksheet}.sql"
    if os.environ.get("TMIG_UPDATE_SNAPSHOTS") == "1":
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_text(sql, encoding="utf-8", newline="\n")
    assert snapshot.is_file(), f"缺少快照 {snapshot}；以 TMIG_UPDATE_SNAPSHOTS=1 產生後人工審核"
    assert sql == snapshot.read_text(encoding="utf-8"), f"{worksheet} 的 SQL 變了；審核後更新快照"
    # SQLGlot 能解析（之後以它轉成 Oracle、Databricks 等方言）
    sqlglot.parse_one(sql, read="duckdb")
