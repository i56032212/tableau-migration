"""別名：查詢服務的資料表顯示成員別名，原始值留給計算比對；IR 帶出參數的「顯示為」給檢視器。"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
WB = "/api/workbooks/superstore_features"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(ROOT / "fixtures", ROOT / "no-web-dist"))


def test_data_table_shows_member_aliases(client: TestClient) -> None:
    data = client.post(f"{WB}/worksheets/ws_sales_by_ship_speed/render").json()
    # 群組「其他」的別名：顯示為 Standard，原始值仍是 Other
    assert [row[0] for row in data["table"]["rows"]] == ["Fast", "Standard"]
    assert [row[0] for row in data["table"]["raw"]] == ["Fast", "Other"]
    labels = {v["c0"]: v["c0_label"] for v in data["spec"]["data"]["values"]}
    assert labels == {"Fast": "Fast", "Other": "Standard"}


def test_ir_carries_aliases_for_the_viewer(client: TestClient) -> None:
    ir = client.get(f"{WB}/ir").json()
    (param,) = ir["parameters"]
    assert [(a["value"], a["label"]) for a in param["aliases"]] == [
        ("Central", "中區"),
        ("East", "東區"),
        ("South", "南區"),
        ("West", "西區"),
    ]
    kpis = next(w for w in ir["worksheets"] if w["id"] == "ws_segment_kpis")
    assert [a["label"] for a in kpis["measure_aliases"]] == ["訂單數"]
