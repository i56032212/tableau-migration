from __future__ import annotations

from pathlib import Path

import pytest

from tmig.cli import main

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "fixtures" / "superstore_demo" / "golden.ir.json"
DB = ROOT / "fixtures" / "superstore_demo" / "data" / "ds_orders.duckdb"


def test_sql_prints_query(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sql", str(GOLDEN), "Monthly Profit", "--case", "year_2024"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("WITH")
    assert "IN (2024)" in out


def test_sql_unknown_case(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sql", str(GOLDEN), "ws_monthly_profit", "--case", "nope"]) == 1
    assert "找不到篩選組合" in capsys.readouterr().err


@pytest.mark.skipif(not DB.is_file(), reason="需要先執行 tmig import-hyper")
def test_query_writes_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "result.csv"
    assert (
        main(
            [
                "query",
                str(GOLDEN),
                "ws_sales_by_region",
                "--case",
                "consumer_top2",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("f_region,f_category,sum(f_sales)")
    assert {line.split(",")[0] for line in lines[1:]} == {"West", "East"}


def test_query_missing_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["query", str(GOLDEN), "ws_sales_by_region", "--db", str(tmp_path / "none.duckdb")])
        == 1
    )
    assert "import-hyper" in capsys.readouterr().err
