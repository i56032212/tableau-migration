"""標題中的欄位值：查詢服務回傳的標題代入整張工作表計算的值（量值是合計，維度只有一個值時顯示它）。"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.ir import TextRun, load_ir
from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
SHEET = "ws_sales_by_region"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    root = tmp_path_factory.mktemp("titles")
    target = root / "titles"
    target.mkdir()
    for name in ("superstore_demo.twbx", "cases.yaml"):
        shutil.copy(DEMO / name, target / name)
    shutil.copytree(DEMO / "data", target / "data")
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
            ws = ws.model_copy(update={"title": title})
        worksheets.append(ws)
    ir = ir.model_copy(update={"worksheets": worksheets})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
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
