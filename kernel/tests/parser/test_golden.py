"""示範報表的 golden IR：解析結果必須與人工確認過的檔案完全相同（A6）。"""

from __future__ import annotations

from pathlib import Path

from tmig.ir import dumps_ir, load_ir
from tmig.parser.workbook import parse_workbook

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures"


def fixture_dirs() -> list[Path]:
    return sorted(p.parent for p in FIXTURES.glob("*/golden.ir.json"))


def test_golden_fixtures_exist() -> None:
    assert [p.name for p in fixture_dirs()] == ["superstore_demo"]


def test_parse_matches_golden() -> None:
    for directory in fixture_dirs():
        twbx = next(directory.glob("*.twbx"))
        golden = directory / "golden.ir.json"
        ir = parse_workbook(twbx)
        assert ir.unsupported == [], f"{directory.name} 有不支援的元素"
        assert dumps_ir(ir) == golden.read_text(encoding="utf-8"), (
            f"{directory.name} 的解析結果與 golden IR 不同；確認差異後以 tmig parse 重新產生"
        )
        load_ir(golden)
