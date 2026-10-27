from __future__ import annotations

import json
from pathlib import Path

import pytest

from tmig.cli import EXIT_UNSUPPORTED, main

from .parser import builder as b

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"


def test_parse_demo_to_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "ir.json"
    assert main(["parse", str(DEMO), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ir_version"] == "0.2"
    assert "unsupported 為空" in capsys.readouterr().err


def test_parse_reports_unsupported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = b.write_twb(tmp_path, b.workbook(extra="<shiny-new-feature />"))
    assert main(["parse", str(path)]) == EXIT_UNSUPPORTED
    captured = capsys.readouterr()
    assert json.loads(captured.out)["unsupported"]
    assert "shiny-new-feature" in captured.err


def test_parse_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = b.write_twb(
        tmp_path, b.workbook("<datasource name='x'><connection class='sqlproxy' /></datasource>")
    )
    assert main(["parse", str(path)]) == 1
    assert "Published Data Source" in capsys.readouterr().err
