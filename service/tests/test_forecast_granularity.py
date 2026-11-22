"""預測選「自動」、檢視以年、SUM(Sales)：示範報表只有 4 年（2021–2024）的資料，與 Tableau 相同改以季估計再
加總回年（見 kernel/tests/sqlgen/test_forecast_granularity.py）；描述預測寫出實際使用的細度。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_editor import blank, client, put, render, use  # noqa: F401


def test_yearly_forecast_is_estimated_by_quarters(client: TestClient) -> None:  # noqa: F811
    year = use("f_order_date", date={"kind": "trunc", "unit": "year"}, continuous=True)
    sales = use("f_sales", agg="sum", continuous=True)
    ws = blank(client, rows=[sales], columns=[year])
    ws["marks"]["type"] = "line"
    ws["forecast"] = {"measures": [sales]}
    put(client, ws)
    data = render(client, ws["id"])
    assert data["unsupported"] is None, data
    described = data["describe"]["forecast"]
    assert (described["unit"], described["view_unit"]) == ("quarter", "year")
    (model,) = described["models"]
    assert model["period"] == 4 and model["observations"] == 15  # 16 季，忽略最後 1 季
    # 2024 年（含忽略的第 4 季）與之後的 5 年是估計的期
    values = data["spec"]["data"]["values"]
    assert len(values) == 4 + 5
