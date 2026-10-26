import json
from pathlib import Path

import pytest

from tmig.cli import main

DATA = Path(__file__).parent / "census" / "data"


def test_census_prints_markdown(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["census", str(DATA / "pilot.twb")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 功能盤點：pilot.twb")


def test_census_writes_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = [str(DATA / "pilot.twb"), str(DATA / "problems.twb")]
    assert main(["census", *paths, "--out-dir", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "pilot.twb：B 級，起步條件符合" in out
    assert "problems.twb：C 級，不符合：" in out
    report = json.loads((tmp_path / "out" / "pilot.census.json").read_text(encoding="utf-8"))
    assert report["grade"] == "B"
    markdown = (tmp_path / "out" / "problems.census.md").read_text(encoding="utf-8")
    assert markdown.startswith("# 功能盤點：problems.twb")


def test_census_bad_file_keeps_going(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.twbx"
    broken.write_bytes(b"nope")
    assert main(["census", str(broken), str(DATA / "pilot.twb")]) == 1
    captured = capsys.readouterr()
    assert "失敗" in captured.err
    assert "# 功能盤點：pilot.twb" in captured.out
