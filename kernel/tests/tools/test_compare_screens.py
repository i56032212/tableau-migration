"""compare-screens（F5）：每組篩選一頁並列，含新平台截圖與計算比對結果。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tmig.verify.run import ensure_data, load_fixture_ir, verify_fixture, write_report

from .loader import ROOT, chromium_available, load_tool

compare_screens = load_tool("tools/compare_screens/compare_screens.py", "compare_screens")
DEMO = ROOT / "fixtures" / "superstore_demo"
DIST = ROOT / "web" / "dist"


@pytest.mark.skipif(
    not (DIST / "index.html").is_file() or not chromium_available(),
    reason="需要建置好的檢視器（web/dist）與 Playwright 的 Chromium",
)
def test_compare_pages(tmp_path: Path) -> None:
    ensure_data(DEMO, load_fixture_ir(DEMO))
    fixture = tmp_path / "fixtures" / "superstore_demo"
    shutil.copytree(
        DEMO, fixture, ignore=shutil.ignore_patterns("source", "*.twbx", "sql", "compare", "report")
    )
    # 假裝已抓到一張 Tableau 截圖
    (fixture / "expected" / "ws_monthly_profit" / "default.png").write_bytes(
        (ROOT / "web" / "dist" / "index.html").read_bytes()
    )
    ir, report = verify_fixture(fixture)
    write_report(ir, report, fixture / "report" / "calc.json")
    assert compare_screens.main([str(fixture)]) == 0
    out = fixture / "compare"
    index = (out / "index.html").read_text(encoding="utf-8")
    assert index.count("計算一致") == 16
    for case in ("default", "office_supplies", "year_2024", "consumer_top2"):
        html = (out / f"{case}.html").read_text(encoding="utf-8")
        assert html.count('class="pair"') == 5  # 4 張工作表＋1 個 dashboard
        for sheet in (
            "ws_sales_by_region",
            "ws_monthly_profit",
            "ws_region_share_by_year",
            "ws_sub_category_profit",
        ):
            png = out / "screens" / case / f"{sheet}.png"
            assert png.read_bytes()[:4] == b"\x89PNG"
    default = (out / "default.html").read_text(encoding="utf-8")
    assert "../expected/ws_monthly_profit/default.png" in default
    assert "尚未抓取 Tableau 截圖" in default


def test_dev_auth_from_cases_user() -> None:
    assert compare_screens.dev_auth({"cases": []}) is None
    settings = compare_screens.dev_auth(
        {"user": {"username": "amy", "groups": ["Sales"], "full_name": "Amy Lin"}}
    )
    assert settings.mode == "dev"
    assert settings.directory.find("AMY").identity().groups == ("Sales",)
