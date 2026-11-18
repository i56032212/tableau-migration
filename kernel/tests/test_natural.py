"""自然排序（tmig.natural）：字串中的數字當成數值比較；SQL 與 Python 的結果一致。"""

from __future__ import annotations

import duckdb

from tmig.natural import DIGITS, natural_key, natural_sql

MEMBERS = [
    "TXN-20261118-Cha-10",
    "TXN-20261118-Cha-2",
    "TXN-20261118-Cha-0",
    "TXN-20261118-Cou-3",
    "Item 7x",
    "Item 007x",
    "A10b2",
    "A10b10",
    "10",
    "9",
    "a",
    "B",
    "",
    "no digits",
]


def test_numbers_inside_text_are_compared_as_numbers() -> None:
    """Tableau 的資料源順序：2 在 10 之前（逐字比較時 10 在 2 之前）；沒有數字的字串照舊逐字比較。"""
    ids = [f"TXN-20261118-Cha-{n}" for n in (10, 2, 0, 40, 8)]
    assert sorted(ids, key=natural_key) == [f"TXN-20261118-Cha-{n}" for n in (0, 2, 8, 10, 40)]
    assert sorted(ids)[1] == "TXN-20261118-Cha-10"  # 逐字比較
    assert sorted(["b", "a", "B"], key=natural_key) == ["B", "a", "b"]
    assert natural_key("no digits") == "no digits"
    assert natural_key("x7") == "x" + "7".zfill(DIGITS)


def test_sql_and_python_agree() -> None:
    """查詢的排序（DuckDB）與 Python 的排序用同一個規則：結果相同；同鍵（07 與 7）時依字串，Null 在最後。"""
    con = duckdb.connect()
    con.execute("create table t(s varchar)")
    con.executemany("insert into t values (?)", [(m,) for m in [*MEMBERS, None]])
    key = natural_sql("s")
    rows = con.execute(
        f"select s from t order by {key} asc nulls last, s asc nulls last"
    ).fetchall()
    assert [r[0] for r in rows] == [*sorted(MEMBERS, key=lambda m: (natural_key(m), m)), None]
    assert [r[0] for r in rows][:4] == ["", "9", "10", "A10b2"]
