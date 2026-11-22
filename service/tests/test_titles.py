"""標題中的欄位值：查詢服務回傳的標題代入整張工作表計算的值（量值是合計，維度只有一個值時顯示它）。"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.data.executor import execute_query
from tmig.ir import IR, Filter, RangeFilter, TextRun, load_ir
from tmig.planner.state import use_key
from tmig.sqlgen.query import worksheet_sql
from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
SHEET = "ws_sales_by_region"


def titled(extra: list[Filter] | None = None) -> IR:
    """示範報表的 Sales 依地區長條圖，標題為 Total Sales <SUM(Sales)> in <Region>；extra 為加上的篩選。"""
    ir = load_ir(DEMO / "golden.ir.json")
    worksheets = []
    for ws in ir.worksheets:
        if ws.id == SHEET:
            region, sales = ws.rows[0], ws.columns[0]
            title = [
                TextRun(text="Total Sales "),
                TextRun(use=sales),
                TextRun(text=" in "),
                TextRun(use=region),
            ]
            ws = ws.model_copy(update={"title": title, "filters": [*ws.filters, *(extra or [])]})
        worksheets.append(ws)
    return ir.model_copy(update={"worksheets": worksheets})


def serve(root: Path, name: str, ir: IR) -> None:
    target = root / name
    target.mkdir()
    for item in ("superstore_demo.twbx", "cases.yaml"):
        shutil.copy(DEMO / item, target / item)
    shutil.copytree(DEMO / "data", target / "data")
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    root = tmp_path_factory.mktemp("titles")
    serve(root, "titles", titled())
    # 同一張圖加上 SUM(Sales) 的量值篩選：下限在銷售最高與第二高的標記之間（只留下最高的標記）
    ir = titled()
    ws = next(w for w in ir.worksheets if w.id == SHEET)
    result = execute_query(DEMO / "data" / f"{ws.datasource}.duckdb", worksheet_sql(ir, SHEET))
    column = result.columns.index(use_key(ws.columns[0]))
    sales = sorted(float(row[column]) for row in result.rows)
    at_least = RangeFilter(
        kind="range",
        id="flt_sales_at_least",
        field=ws.columns[0],
        stage="measure",
        min=(sales[-2] + sales[-1]) / 2,
        source_ref="test",
    )
    serve(root, "filtered", titled([at_least]))
    return TestClient(create_app(root, ROOT / "no-web-dist"))


def test_title_values_follow_the_view(client: TestClient) -> None:
    data = client.post(f"/api/workbooks/titles/worksheets/{SHEET}/render").json()
    assert data["unsupported"] is None
    # Sales 依地區的長條圖：標題中的 SUM(Sales) 是各區相加，Region 有多個值時顯示 *
    total = sum(float(row[2]) for row in data["table"]["raw"])
    values = {v["c0"] for v in data["spec"]["data"]["values"]}
    assert len(values) > 1
    head, tail = data["title"].split(" in ")
    assert tail == "*"
    assert float(head.removeprefix("Total Sales ").replace(",", "")) == pytest.approx(total, abs=1)

    # 篩選到一個地區：Region 顯示它，SUM(Sales) 只算這一區
    region = next(iter(sorted(values)))
    body = {"filters": {"f_region": {"mode": "include", "values": [region]}}}
    data = client.post(f"/api/workbooks/titles/worksheets/{SHEET}/render", json=body).json()
    assert data["title"].endswith(f" in {region}")
    one = sum(float(row[2]) for row in data["table"]["raw"])
    head = data["title"].split(" in ")[0]
    assert float(head.removeprefix("Total Sales ").replace(",", "")) == pytest.approx(one, abs=1)


def test_title_values_with_measure_filter(client: TestClient) -> None:
    """與量值篩選一起使用：標題中的 SUM(Sales) 只計算通過篩選的標記（只剩銷售最高的標記：它的值，
    Region 也只有它的地區），不是全部地區的合計。"""
    data = client.post(f"/api/workbooks/filtered/worksheets/{SHEET}/render").json()
    assert data["unsupported"] is None
    (row,) = data["table"]["raw"]  # 地區、…、SUM(Sales)（與上一個測試相同的欄位順序）
    head, tail = data["title"].split(" in ")
    assert tail == row[0]
    assert float(head.removeprefix("Total Sales ").replace(",", "")) == pytest.approx(
        float(row[2]), abs=1
    )
