"""標題中的 Measure Names 與 Measure Values（推定，見 kernel/tests/render/test_title_measure_names.py）：
查詢服務回傳的標題代入量值的名稱與全部資料的合計；檢視中有多個量值時是 *。"""

from __future__ import annotations

import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from test_titles import DEMO, ROOT, SHEET, serve

from tmig.ir import IR, MEASURE_NAMES, MEASURE_VALUES, FieldUse, TextRun, load_ir
from tmig_service import create_app


def measure_table(count: int) -> IR:
    """示範報表的 Sales 依地區改成 Measure Values 文字表（前 count 個量值：SUM(Sales)、SUM(Profit)），
    標題為 <Measure Names>: <Measure Values>。"""
    ir = load_ir(DEMO / "golden.ir.json")
    worksheets = []
    for ws in ir.worksheets:
        if ws.id == SHEET:
            sales = ws.columns[0]
            profit = FieldUse(field="f_profit", agg="sum", continuous=True)
            names, values = FieldUse(field=MEASURE_NAMES), FieldUse(field=MEASURE_VALUES)
            title = [TextRun(use=names), TextRun(text=": "), TextRun(use=values)]
            marks = ws.marks.model_copy(update={"type": "text", "label": [values]})
            ws = ws.model_copy(
                update={
                    "columns": [names],
                    "marks": marks,
                    "measure_values": [sales, profit][:count],
                    "title": title,
                    "sorts": [],
                    "reference_lines": [],
                }
            )
        worksheets.append(ws)
    return ir.model_copy(update={"worksheets": worksheets})


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    root = tmp_path_factory.mktemp("title_measure_names")
    serve(root, "one", measure_table(1))
    serve(root, "two", measure_table(2))
    return TestClient(create_app(root, ROOT / "no-web-dist"))


def test_one_measure(client: TestClient) -> None:
    """一個量值：名稱是 Sales，值是各區相加。"""
    data = client.post(f"/api/workbooks/one/worksheets/{SHEET}/render").json()
    assert data["unsupported"] is None, data
    name, value = data["title"].split(": ")
    assert name == "Sales"
    total = sum(float(v["__mv"]) for v in data["spec"]["data"]["values"])
    assert float(value.replace(",", "")) == pytest.approx(total, abs=1)


def test_several_measures(client: TestClient) -> None:
    data = client.post(f"/api/workbooks/two/worksheets/{SHEET}/render").json()
    assert data["unsupported"] is None, data
    assert data["title"] == "*: *"
