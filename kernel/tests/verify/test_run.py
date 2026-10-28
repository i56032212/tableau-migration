"""tmig verify 端到端（E3）：示範與功能報表全數通過；人為修改的預期結果必須判為失敗。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tmig.cli import main
from tmig.verify.run import ensure_data, load_fixture_ir, verify_fixture

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "fixtures" / "superstore_demo"
FEATURES = ROOT / "fixtures" / "superstore_features"


@pytest.fixture(scope="module")
def demo_data() -> Path:
    ensure_data(DEMO, load_fixture_ir(DEMO))
    return DEMO / "data"


@pytest.fixture
def fixture_copy(tmp_path: Path, demo_data: Path) -> Path:
    target = tmp_path / "superstore_demo"
    shutil.copytree(
        DEMO,
        target,
        ignore=shutil.ignore_patterns("source", "*.twbx", "report", "sql"),
    )
    return target


def test_demo_passes(demo_data: Path) -> None:
    _, report = verify_fixture(DEMO)
    assert report.summary == {"pass": 16, "fail": 0, "unsupported": 0}
    assert report.ok


def test_features_pass() -> None:
    """功能報表（擴充階段的各項功能）的預期結果由 scripts/features_oracle.py 獨立計算。"""
    ensure_data(FEATURES, load_fixture_ir(FEATURES))
    _, report = verify_fixture(FEATURES)
    assert report.summary == {"pass": 76, "fail": 0, "unsupported": 0}
    assert report.ok


def test_modified_value_fails(fixture_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = fixture_copy / "expected" / "ws_sales_by_region" / "default.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    cells = lines[1].split(",")
    cells[2] = str(float(cells[2]) * 1.001)
    lines[1] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = fixture_copy / "report.json"
    assert main(["verify", str(fixture_copy), "--out", str(out)]) == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"] == {"pass": 15, "fail": 1, "unsupported": 0}
    failed = next(c for c in report["checks"] if c["status"] == "fail")
    assert failed["check_id"] == "ws_sales_by_region:default"
    assert failed["details"][0]["kind"] == "cell"
    assert failed["details"][0]["column"] == "sum(f_sales)"
    assert set(report) >= {
        "report_version",
        "workbook",
        "stage",
        "checks",
        "summary",
        "owner_summary",
    }
    assert "1 項不一致" in report["owner_summary"]


def test_removed_row_and_missing_file_fail(fixture_copy: Path) -> None:
    path = fixture_copy / "expected" / "ws_monthly_profit" / "year_2024.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    (fixture_copy / "expected" / "ws_sub_category_profit" / "default.csv").unlink()
    _, report = verify_fixture(fixture_copy)
    by_id = {c.check_id: c for c in report.checks}
    assert "多出 1 列" in by_id["ws_monthly_profit:year_2024"].diff
    assert "缺少預期結果" in by_id["ws_sub_category_profit:default"].diff
    assert report.summary["fail"] == 2


def test_unconfirmed_mapping_is_noted(fixture_copy: Path) -> None:
    mapping = fixture_copy / "mapping.yaml"
    mapping.write_text(
        mapping.read_text(encoding="utf-8").replace("confirmed: true", "confirmed: false"),
        encoding="utf-8",
    )
    _, report = verify_fixture(fixture_copy)
    assert any("尚未人工確認" in n for n in report.notes)


def test_mapping_suggest_matches_committed_mapping(fixture_copy: Path) -> None:
    (fixture_copy / "mapping.yaml").unlink()
    assert main(["mapping", "suggest", str(fixture_copy)]) == 0
    suggested = (fixture_copy / "mapping.yaml").read_text(encoding="utf-8")
    committed = (DEMO / "mapping.yaml").read_text(encoding="utf-8")
    assert suggested.replace("confirmed: false", "confirmed: true") == committed
    assert main(["mapping", "suggest", str(fixture_copy)]) == 1  # 已存在時不覆蓋
