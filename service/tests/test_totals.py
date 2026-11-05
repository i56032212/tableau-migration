"""總計（Grand Total）：檢視器的圖表與資料表加上總計；算不出與 Tableau 相同的數字時整張工作表列為不支援。

以示範報表為底：「Region Share by Year」（Region × 年的文字表）加上總計列與總計欄；
「Sales by Region」（工具提示有累計的 Table Calculation）也加上總計，應列為不支援、不默默省略。
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.ir import load_ir
from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
TOTALS = {"ws_region_share_by_year": ["rows", "columns"], "ws_sales_by_region": ["rows"]}


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    root = tmp_path_factory.mktemp("totals")
    target = root / "totals"
    target.mkdir()
    for name in ("superstore_demo.twbx", "cases.yaml"):
        shutil.copy(DEMO / name, target / name)
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")
    worksheets = [
        ws.model_copy(update={"totals": TOTALS[ws.id]}) if ws.id in TOTALS else ws
        for ws in ir.worksheets
    ]
    ir = ir.model_copy(update={"worksheets": worksheets})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
    return TestClient(create_app(root, ROOT / "no-web-dist"))


def test_totals_in_chart_and_data_table(client: TestClient) -> None:
    data = client.post("/api/workbooks/totals/worksheets/ws_region_share_by_year/render").json()
    assert data["unsupported"] is None
    values = data["spec"]["data"]["values"]
    marks = [v for v in values if not v.get("__total")]
    totals = [v for v in values if v.get("__total")]
    # 列數與計算比對的原始值只算工作表的標記
    assert data["rows"] == len(marks) == len(data["table"]["raw"])
    # 欄位：c0 Region、c1 年、c2 區域占比、c3 Sales（工具提示）
    years = sorted({v["c1"] for v in marks})
    regions = sorted({v["c0"] for v in marks})
    assert len(totals) == len(years) + len(regions) + 1
    # Sales 是 SUM：總計列（每一年）、總計欄（每一區）與右下角都等於各標記相加
    for year in years:
        (row,) = [t for t in totals if t["c0_label"] == "Grand Total" and t["c1"] == year]
        assert row["c3"] == pytest.approx(sum(v["c3"] for v in marks if v["c1"] == year))
    for region in regions:
        (column,) = [t for t in totals if t["c0"] == region and t["c1_label"] == "Grand Total"]
        assert column["c3"] == pytest.approx(sum(v["c3"] for v in marks if v["c0"] == region))
    corner = totals[-1]
    assert corner["c0_label"] == corner["c1_label"] == "Grand Total"
    assert corner["c3"] == pytest.approx(sum(v["c3"] for v in marks))
    # 資料表：工作表的標記之後是總計，去掉的維度顯示 Grand Total，數字的格式與其他列相同
    table = data["table"]["rows"]
    assert len(table) == len(marks) + len(totals)
    assert table[-1][:2] == ["Grand Total", "Grand Total"]
    assert table[-1][3] == corner["c3_label"]


def test_unsupported_totals_mark_the_sheet_unsupported(client: TestClient) -> None:
    data = client.post("/api/workbooks/totals/worksheets/ws_sales_by_region/render").json()
    assert data["spec"] is None
    assert data["unsupported"] == "總計中的 Table Calculation 尚不支援"
    assert data["name"] == "Sales by Region"


@pytest.fixture(scope="module")
def features(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    """功能報表的「Sub-Category Sales」（列架 Category／Sub-Category 的長條圖）加上 Category 的小計與總計。"""
    root = tmp_path_factory.mktemp("subtotals")
    source = ROOT / "fixtures" / "superstore_features"
    target = root / "subtotals"
    target.mkdir()
    for name in ("superstore_features.twbx", "cases.yaml"):
        shutil.copy(source / name, target / name)
    shutil.copytree(source / "data", target / "data")
    ir = load_ir(source / "golden.ir.json")
    worksheets = [
        ws.model_copy(update={"subtotals": ws.rows[:1], "totals": ["rows"]})
        if ws.id == "ws_sub_category_sales"
        else ws
        for ws in ir.worksheets
    ]
    ir = ir.model_copy(update={"worksheets": worksheets})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
    return TestClient(create_app(root, ROOT / "no-web-dist"))


def test_subtotals_in_chart_and_data_table(features: TestClient) -> None:
    data = features.post("/api/workbooks/subtotals/worksheets/ws_sub_category_sales/render").json()
    assert data["unsupported"] is None
    values = data["spec"]["data"]["values"]
    marks = [v for v in values if not v.get("__total")]
    assert data["rows"] == len(marks) == 17
    # 圖表：每個 Category 的長條之後是它的 Total（Sales 的合計），最後是 Grand Total
    categories = list(dict.fromkeys(v["c0"] for v in marks))
    expected: list[str] = []
    for category in categories:
        expected += [v["rows_dim"] for v in marks if v["c0"] == category]
        expected.append(f"{category}｜Total")
    assert [v["rows_dim"] for v in values] == [*expected, "Grand Total"]
    subtotals = {v["rows_dim"]: v["c2"] for v in values if v.get("__total")}
    for category in categories:
        total = sum(v["c2"] for v in marks if v["c0"] == category)
        assert subtotals[f"{category}｜Total"] == pytest.approx(total)
    assert subtotals["Grand Total"] == pytest.approx(sum(v["c2"] for v in marks))
    # 資料表與圖表的順序相同：小計緊接在各組之後，去掉的維度顯示 Total
    rows: list[list[str]] = []
    for category in categories:
        rows += [[v["c0_label"], v["c1_label"]] for v in marks if v["c0"] == category]
        rows.append([category, "Total"])
    assert [row[:2] for row in data["table"]["rows"]] == [*rows, ["Grand Total", "Grand Total"]]
