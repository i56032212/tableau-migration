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


def test_query_writes_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 沒有 DuckDB 檔時會先由 .twbx 自動匯入
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
    assert DB.is_file()


def test_query_missing_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["query", str(GOLDEN), "ws_sales_by_region", "--db", str(tmp_path / "none.duckdb")])
        == 1
    )
    assert "import-hyper" in capsys.readouterr().err


def _rls_ir(tmp_path: Path) -> Path:
    """資料源篩選以 USERNAME() 與 ISMEMBEROF 限制可見的列（P1）。"""
    from tmig.ir import dumps_ir
    from tmig.parser.workbook import parse_workbook

    from .parser import builder as b

    owner = b.calc(
        "Mine", "Mine", '[Region] = USERNAME() OR ISMEMBEROF("All")', "boolean", "dimension"
    )
    flt = (
        "<filter class='categorical' column='[Mine]'>"
        "<groupfilter function='member' level='[Mine]' member='true' /></filter>"
    )
    sheet = b.worksheet(
        "S",
        [b.instance("[Region]", "None", "[none:Region:nk]")],
        rows=b.ref("none:Region:nk"),
    )
    xml = b.workbook(b.extract_datasource(columns=owner, extra=flt), worksheets=sheet)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    path = tmp_path / "ir.json"
    path.write_text(dumps_ir(ir), encoding="utf-8")
    return path


def test_sql_user_from_cases_and_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ir = _rls_ir(tmp_path)
    assert main(["sql", str(ir), "S"]) == 1
    assert "需要登入者的身分" in capsys.readouterr().err

    (tmp_path / "cases.yaml").write_text(
        "workbook: w\nuser:\n  username: East\n  groups: [Sales]\nsheets: []\ncases: []\n",
        encoding="utf-8",
    )
    assert main(["sql", str(ir), "S"]) == 0
    out = capsys.readouterr().out
    assert "'East'" in out and "OR FALSE" in out

    assert main(["sql", str(ir), "S", "--user", "West", "--group", "all"]) == 0
    out = capsys.readouterr().out
    assert "'West'" in out and "OR TRUE" in out

    assert main(["sql", str(ir), "S", "--group", "all"]) == 0  # 沿用 cases.yaml 的使用者
    out = capsys.readouterr().out
    assert "'East'" in out and "OR TRUE" in out

    (tmp_path / "cases.yaml").unlink()
    assert main(["sql", str(ir), "S", "--group", "all"]) == 1
    assert "--group 需要搭配 --user" in capsys.readouterr().err
