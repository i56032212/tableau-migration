"""查詢服務的四支 API（F2）：只接受篩選與參數狀態，從不接受 SQL。"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
WB = "/api/workbooks/superstore_demo"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(ROOT / "fixtures", ROOT / "no-web-dist"))


def test_list_workbooks(client: TestClient) -> None:
    demo, features = client.get("/api/workbooks").json()
    assert features["id"] == "superstore_features"
    assert features["unsupported"] == 0
    assert demo["id"] == "superstore_demo"
    assert demo["dashboards"] == [{"id": "db_ee909b0c", "name": "銷售總覽"}]
    assert len(demo["worksheets"]) == 4
    assert demo["unsupported"] == 0


def test_ir(client: TestClient) -> None:
    ir = client.get(f"{WB}/ir").json()
    assert ir["ir_version"] == "0.3"
    assert client.get("/api/workbooks/nope/ir").status_code == 404


def test_render_with_state(client: TestClient) -> None:
    body = {
        "filters": {"f_category": {"mode": "include", "values": ["Furniture"]}},
        "parameters": {"p_parameter_1": 2},
    }
    data = client.post(f"{WB}/worksheets/ws_sales_by_region/render", json=body).json()
    assert data["rows"] == 2
    assert data["applied"] == ["f_category"]
    assert data["spec"]["$schema"].endswith("/v6.json")
    assert data["table"]["headers"][:3] == ["Region", "Category", "Sales"]
    assert {row[0] for row in data["table"]["rows"]} == {"West", "East"}


def test_render_with_filter_action_selection(client: TestClient) -> None:
    """篩選動作：檢視器以 usermeta.dimensions 把點選的標記轉成欄位鍵，放進狀態的 actions。"""
    features = "/api/workbooks/superstore_features"
    source = client.post(f"{features}/worksheets/ws_region_picker/render").json()
    dimensions = source["spec"]["usermeta"]["dimensions"]
    assert dimensions == {"c0": "f_region"}
    east = next(r for r in source["spec"]["data"]["values"] if r["c0"] == "East")
    mark = {key: east[name] for name, key in dimensions.items()}

    url = f"{features}/worksheets/ws_category_detail/render"
    assert client.post(url).json()["rows"] == 3  # 沒有選取：顯示全部
    selected = client.post(url, json={"actions": {"act_filter_category_detail": [mark]}}).json()
    everything = client.post(url).json()
    assert selected["rows"] == 3
    assert selected["table"]["raw"] != everything["table"]["raw"]

    url = f"{features}/worksheets/ws_segment_detail/render"
    assert client.post(url).json()["rows"] == 0  # 沒有選取：清除時排除所有值
    state = {"actions": {"act_filter_segment_detail": [mark, {"f_region": "West"}]}}
    assert client.post(url, json=state).json()["rows"] == 3
    # 選取的值必須是 JSON 純量
    bad = {"actions": {"act_filter_segment_detail": [{"f_region": ["East"]}]}}
    assert client.post(url, json=bad).status_code == 422


def test_relative_date_defaults_to_cases_today(client: TestClient) -> None:
    """相對日期篩選的基準日：沒有指定時用 cases.yaml 的 today（與計算比對相同）。"""
    url = "/api/workbooks/superstore_features/worksheets/ws_recent_sales_by_category/render"
    assert client.post(url).json()["rows"] == 3
    # 指定的基準日在資料之後：最近 3 個月沒有資料
    assert client.post(url, json={"today": "2030-01-15"}).json()["rows"] == 0
    assert client.post(url, json={"today": "2030/01/15"}).status_code == 422
    case = client.get("/api/workbooks/superstore_features/cases").json()[0]
    assert case["state"]["today"] == "2024-12-31"


def test_render_default_and_worksheet_by_name(client: TestClient) -> None:
    by_id = client.post(f"{WB}/worksheets/ws_monthly_profit/render").json()
    by_name = client.post(f"{WB}/worksheets/Monthly%20Profit/render", json={}).json()
    assert by_id["rows"] == by_name["rows"] == 23


@pytest.mark.parametrize(
    "body",
    [
        {"sql": "SELECT 1"},
        {"filters": {}, "query": "DROP TABLE x"},
        {"filters": {"f_category": {"mode": "include", "values": ["x"], "sql": "1=1"}}},
    ],
)
def test_render_rejects_anything_but_state(client: TestClient, body: dict) -> None:
    assert client.post(f"{WB}/worksheets/ws_sales_by_region/render", json=body).status_code == 422


def test_render_errors(client: TestClient) -> None:
    assert client.post(f"{WB}/worksheets/nope/render", json={}).status_code == 422
    bad_param = {"parameters": {"p_missing": 1}}
    assert (
        client.post(f"{WB}/worksheets/ws_sales_by_region/render", json=bad_param).status_code == 422
    )
    bad_key = {"filters": {"sum(f_sales": {"values": [1]}}}
    assert (
        client.post(f"{WB}/worksheets/ws_sales_by_region/render", json=bad_key).status_code == 422
    )


def test_domain(client: TestClient) -> None:
    assert client.post(f"{WB}/fields/f_segment/domain").json() == {
        "kind": "values",
        "values": ["Consumer", "Corporate", "Home Office"],
    }
    assert client.post(f"{WB}/fields/year(f_order_date)/domain").json()["values"] == [
        2021,
        2022,
        2023,
        2024,
    ]
    assert client.post(f"{WB}/fields/f_order_date/domain").json() == {
        "kind": "range",
        "min": "2021-01-03",
        "max": "2024-12-30",
    }
    assert (
        client.post(f"{WB}/fields/f_sales/domain", json={"kind": "range"}).json()["kind"] == "range"
    )
    assert client.post(f"{WB}/fields/f_nope/domain").status_code == 404
    assert client.post(f"{WB}/fields/f_category/domain", json={"sql": "x"}).status_code == 422


def test_cases(client: TestClient) -> None:
    cases = client.get(f"{WB}/cases").json()
    assert [c["name"] for c in cases] == [
        "default",
        "office_supplies",
        "year_2024",
        "consumer_top2",
    ]
    assert cases[3]["state"] == {
        "filters": {
            "f_segment": {
                "mode": "include",
                "values": ["Consumer"],
                "min": None,
                "max": None,
                "include_null": None,
            }
        },
        "parameters": {"p_parameter_1": 2},
        "actions": {},
        "today": None,
    }
