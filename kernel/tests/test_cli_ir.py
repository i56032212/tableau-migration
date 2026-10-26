import json
from pathlib import Path

import pytest

from tmig.cli import main

EXAMPLE = Path(__file__).parent / "ir" / "data" / "example.ir.json"


def test_validate_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ir", "validate", str(EXAMPLE)]) == 0
    assert "通過" in capsys.readouterr().out


def test_validate_reports_location(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    doc["worksheets"][0]["rows"][0]["field"] = "f_missing"
    bad = tmp_path / "bad.ir.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    assert main(["ir", "validate", str(EXAMPLE), str(bad)]) == 1
    err = capsys.readouterr().err
    assert "失敗" in err
    assert "f_missing" in err


def test_validate_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ir", "validate", str(tmp_path / "none.json")]) == 1
    assert "找不到檔案" in capsys.readouterr().err


def test_schema_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ir", "schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Tableau 移轉 IR v0.1"


def test_schema_to_file(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "ir.schema.json"
    assert main(["ir", "schema", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["$defs"]["Zone"]
