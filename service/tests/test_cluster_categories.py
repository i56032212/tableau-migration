"""叢集的類別變數：編輯器存入 ClusterSpec.categories，/render 的叢集與描述叢集；另外輸出的 ATTR 欄不出現在
資料表與完整資料中（見 tmig.sqlgen.query 的 CLUSTER_PREFIX）。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_editor import client, column, put, render, sheet, use  # noqa: F401


def test_cluster_with_categorical_variable(client: TestClient) -> None:  # noqa: F811
    """各子類別的利潤加上類別變數 Category（不在檢視中；每個子類別只屬於一個類別）分三群：描述叢集列出
    ATTR(Category)，資料表沒有另外輸出的欄。"""
    url = "/api/edit/demo/analytics-fields"
    cluster = client.post(url, json={"datasource": "ds_orders", "kind": "cluster"}).json()["field"]
    ws = sheet(client, "ws_sub_category_profit")
    ws["marks"]["color"] = use(cluster["id"])
    ws["cluster"] = {
        "field": cluster["id"],
        "measures": [ws["columns"][0]],
        "categories": [use("f_category")],
        "count": 3,
    }
    saved = put(client, ws)
    assert saved["cluster"]["categories"] == [use("f_category")]
    data = render(client, ws["id"])
    assert data["unsupported"] is None, data
    described = data["describe"]["cluster"]
    assert described["categories"] == ["ATTR(Category)"]
    assert (described["count"], described["unclustered"]) == (3, 0)
    assert not any(k.startswith("__") for k in data["table"]["keys"])
    assert {str(v) for v in column(data["table"], cluster["id"])} == {"1", "2", "3"}
    shown = {v for r in data["spec"]["data"]["values"] for v in r.values() if isinstance(v, str)}
    assert {"Cluster 1", "Cluster 2", "Cluster 3"} <= shown
    assert "__cluster" not in str(data["spec"])
    # 下載的摘要資料也沒有另外輸出的欄
    summary = client.post(
        f"/api/workbooks/demo/worksheets/{ws['id']}/download",
        json={"kind": "summary", "format": "csv"},
    )
    assert summary.status_code == 200, summary.text
    assert "__cluster" not in summary.content.decode("utf-8-sig")
    assert "Cluster" in summary.content.decode("utf-8-sig")
