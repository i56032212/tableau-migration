"""預測結果（量值膠囊的「預測結果」）與描述預測的摘要經過編輯器的 API（/api/edit）與 render 的回應。"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "forecast-results-token-0123456789"
LOCAL = "http://127.0.0.1:8123"
PROFIT = {"field": "f_profit", "agg": "sum", "date": None, "continuous": True, "table_calc": None}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, ROOT / "no-web-dist", username="amy")
    client = TestClient(app, base_url=LOCAL, headers={TOKEN_HEADER: TOKEN})
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    response = client.post("/api/desktop/open", json={"path": str(source)})
    assert response.status_code == 200, response.text
    return client


def put(client: TestClient, body: dict[str, Any]) -> Any:
    return client.put(f"/api/edit/demo/worksheets/{body['id']}", json={"worksheet": body})


def render(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/workbooks/demo/worksheets/ws_monthly_profit/render")
    assert response.status_code == 200, response.text
    return response.json()


def test_forecast_results_through_the_editor_api(client: TestClient) -> None:
    """Monthly Profit：列上是 SUM(Profit) 的實際值與預測值與上層預測區間，工具提示上是品質。上層預測區間與品質
    只有估計的月份有值（實際值的月份是 Null），標題寫出結果類型；描述預測只列一個模型，含摘要。"""
    body = client.get("/api/edit/demo/worksheets/ws_monthly_profit").json()["worksheet"]
    upper, quality = {**PROFIT, "forecast": "upper"}, {**PROFIT, "forecast": "quality"}
    body["rows"] = [PROFIT, upper]
    body["marks"]["tooltip"] = [*body["marks"]["tooltip"], quality]
    body["forecast"] = {"measures": [PROFIT, upper, quality], "length": "next", "size": 6}
    body["forecast"]["unit"] = "month"
    response = put(client, body)
    assert response.status_code == 200, response.text
    saved = client.get("/api/edit/demo/worksheets/ws_monthly_profit").json()["worksheet"]
    assert [u.get("forecast") for u in saved["rows"]] == [None, "upper"]

    rendered = render(client)
    table = rendered["table"]
    keys = table["keys"]
    assert {"sum(f_profit)", "sum(f_profit)~upper", "sum(f_profit)~quality"} <= set(keys)
    headers = dict(zip(keys, table["headers"], strict=False))
    assert headers["sum(f_profit)~upper"] == "Profit（上層預測區間）"
    assert headers["sum(f_profit)~quality"] == "Profit（品質）"
    value, high = keys.index("sum(f_profit)"), keys.index("sum(f_profit)~upper")
    score = keys.index("sum(f_profit)~quality")
    raw = table["raw"]  # 原始值的文字，Null 是空字串
    filled = [row for row in raw if row[high] != ""]
    # 忽略的最後一個月與之後的 6 個月
    assert len(filled) == 7
    assert all(float(row[high]) > float(row[value]) for row in filled)
    assert {row[score] for row in filled} == {filled[0][score]}
    assert all(row[high] == "" and row[score] == "" for row in raw if row not in filled)
    models = rendered["describe"]["forecast"]["models"]
    assert [m["measure"] for m in models] == ["sum(f_profit)"]
    summary = models[0]
    assert summary["initial"] == pytest.approx(float(filled[0][value]))
    assert summary["initial_precision"] > 0
    assert summary["change"] == pytest.approx(float(filled[-1][value]) - float(filled[0][value]))
    assert summary["contribution_trend"] + summary["contribution_season"] == pytest.approx(100)


def test_forecast_result_needs_a_forecast(client: TestClient) -> None:
    """沒有預測的工作表上選了預測結果：422（膠囊會顯示實際值，名稱卻是結果類型），報表不變；
    預測結果不能用在日期上。"""
    body = client.get("/api/edit/demo/worksheets/ws_monthly_profit").json()["worksheet"]
    body["rows"] = [{**PROFIT, "forecast": "trend"}]
    response = put(client, body)
    assert response.status_code == 422 and "預測結果" in response.json()["detail"]
    saved = client.get("/api/edit/demo/worksheets/ws_monthly_profit").json()["worksheet"]
    assert saved["rows"][0].get("forecast") is None
    body["rows"] = [PROFIT]
    body["columns"][0] = {**body["columns"][0], "forecast": "trend"}
    body["forecast"] = {"measures": [PROFIT]}
    assert put(client, body).status_code == 422
