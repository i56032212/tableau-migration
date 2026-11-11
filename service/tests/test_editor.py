"""桌面版編輯器的 API（/api/edit）：變更寫回開啟中的報表，檢視器立即以新的定義查詢；存檔後保留。"""

from __future__ import annotations

import shutil
import warnings
import zipfile
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "editor-token-1234567890"
LOCAL = "http://127.0.0.1:8123"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, ROOT / "no-web-dist", username="amy")
    client = TestClient(app, base_url=LOCAL, headers={TOKEN_HEADER: TOKEN})
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    response = client.post("/api/desktop/open", json={"path": str(source)})
    assert response.status_code == 200, response.text
    return client


def sheet(client: TestClient, worksheet: str) -> dict[str, Any]:
    response = client.get(f"/api/edit/demo/worksheets/{worksheet}")
    assert response.status_code == 200, response.text
    return response.json()["worksheet"]


def render(client: TestClient, worksheet: str) -> dict[str, Any]:
    response = client.post(f"/api/workbooks/demo/worksheets/{worksheet}/render")
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog(client: TestClient) -> None:
    data = client.get("/api/edit/demo/catalog").json()
    captions = {f["caption"] for f in data["datasources"][0]["fields"]}
    assert {"Sales", "Region", "Order Date"} <= captions
    assert data["parameters"] and data["palettes"]


def test_edit_worksheet_and_render(client: TestClient) -> None:
    ws = sheet(client, "ws_sales_by_region")
    # 列改成 Segment、拿掉篩選
    ws["rows"] = [
        {"field": "f_segment", "agg": None, "date": None, "continuous": False, "table_calc": None}
    ]
    ws["filters"] = []
    ws["sorts"] = []
    response = client.put("/api/edit/demo/worksheets/ws_sales_by_region", json={"worksheet": ws})
    assert response.status_code == 200, response.text
    table = render(client, "ws_sales_by_region")["table"]
    assert sorted({row[0] for row in table["rows"]}) == ["Consumer", "Corporate", "Home Office"]
    # 檢視器的報表定義也是新的
    ir = client.get("/api/workbooks/demo/ir").json()
    edited = next(w for w in ir["worksheets"] if w["id"] == "ws_sales_by_region")
    assert edited["rows"][0]["field"] == "f_segment"
    # 不正確的內容：422，報表不變
    ws["rows"] = [
        {"field": "f_nope", "agg": None, "date": None, "continuous": False, "table_calc": None}
    ]
    bad = client.put("/api/edit/demo/worksheets/ws_sales_by_region", json={"worksheet": ws})
    assert bad.status_code == 422 and "f_nope" in bad.json()["detail"]
    assert sheet(client, "ws_sales_by_region")["rows"][0]["field"] == "f_segment"
    # id 與網址不符
    assert (
        client.put(
            "/api/edit/demo/worksheets/ws_monthly_profit", json={"worksheet": ws}
        ).status_code
        == 422
    )


def test_quick_table_calc_from_editor(client: TestClient) -> None:
    ws = sheet(client, "ws_sales_by_region")
    ws["filters"] = []
    ws["sorts"] = []
    ws["table_calcs"] = ws["table_calcs"] + [
        {
            "id": "tc_running",
            "type": "running_total",
            "preset": "table_down",
            "addressing": ["f_region"],
            "instances": [],
            "level": None,
            "restart": None,
            "inferred": False,
            "options": {"aggregation": "sum"},
            "nested": [],
            "source_ref": "/editor",
        }
    ]
    ws["columns"][0]["table_calc"] = "tc_running"
    response = client.put("/api/edit/demo/worksheets/ws_sales_by_region", json={"worksheet": ws})
    assert response.status_code == 200, response.text
    table = render(client, "ws_sales_by_region")["table"]
    column = table["keys"].index("sum(f_sales)#tc_running")
    category = table["keys"].index("f_category")
    # 每個類別（色彩）各自沿著 Region 累計：每段遞增
    for name in {row[category] for row in table["raw"]}:
        values = [float(row[column]) for row in table["raw"] if row[category] == name]
        assert values == sorted(values) and len(values) > 1


def test_add_duplicate_delete_worksheets(client: TestClient) -> None:
    created = client.post("/api/edit/demo/worksheets", json={}).json()["worksheet"]
    assert created["name"] == "工作表 1" and created["rows"] == []
    copy = client.post("/api/edit/demo/worksheets/ws_monthly_profit/duplicate").json()["worksheet"]
    assert copy["name"] == "Monthly Profit (2)"
    assert render(client, copy["id"])["rows"] > 0
    assert client.delete(f"/api/edit/demo/worksheets/{copy['id']}").status_code == 200
    names = [w["name"] for w in client.get("/api/workbooks/demo/ir").json()["worksheets"]]
    assert "Monthly Profit (2)" not in names and "工作表 1" in names
    # dashboard 上的工作表不能刪
    refused = client.delete("/api/edit/demo/worksheets/ws_sales_by_region")
    assert refused.status_code == 422 and "不能刪除" in refused.json()["detail"]


def test_calculated_fields(client: TestClient) -> None:
    ok = client.post(
        "/api/edit/demo/formula",
        json={"datasource": "ds_orders", "formula": "SUM([Profit])/SUM([Sales])"},
    ).json()
    assert ok == {"ok": True, "datatype": "real", "level": "aggregate", "role": "measure"}
    bad = client.post(
        "/api/edit/demo/formula", json={"datasource": "ds_orders", "formula": "SUM([X])"}
    )
    assert bad.json()["ok"] is False and "公式有誤" in bad.json()["message"]
    field = client.post(
        "/api/edit/demo/calcs",
        json={
            "datasource": "ds_orders",
            "caption": "Ratio",
            "formula": "SUM([Profit])/SUM([Sales])",
        },
    ).json()["field"]
    assert field["id"] == "f_ratio"
    ws = sheet(client, "ws_sales_by_region")
    ws["filters"], ws["sorts"] = [], []
    ws["columns"] = [
        {"field": "f_ratio", "agg": None, "date": None, "continuous": True, "table_calc": None}
    ]
    assert (
        client.put(
            "/api/edit/demo/worksheets/ws_sales_by_region", json={"worksheet": ws}
        ).status_code
        == 200
    )
    table = render(client, "ws_sales_by_region")["table"]
    column = table["keys"].index("f_ratio")
    assert all(-1 < float(r[column]) < 1 for r in table["raw"])
    # 使用中不能刪
    refused = client.delete("/api/edit/demo/datasources/ds_orders/calcs/f_ratio")
    assert refused.status_code == 422


def test_domain_for_editor_filters(client: TestClient) -> None:
    values = client.post("/api/edit/demo/domain", json={"use": {"field": "f_segment"}}).json()
    assert values == {"kind": "values", "values": ["Consumer", "Corporate", "Home Office"]}
    years = client.post(
        "/api/edit/demo/domain",
        json={"use": {"field": "f_order_date", "date": {"kind": "part", "unit": "year"}}},
    ).json()
    assert years["kind"] == "values" and all(isinstance(y, int) for y in years["values"])
    dates = client.post("/api/edit/demo/domain", json={"use": {"field": "f_order_date"}}).json()
    assert dates["kind"] == "range" and str(dates["min"]) < str(dates["max"])
    # 量值：工作表詳細程度上的 SUM(Sales) 範圍
    measure = client.post(
        "/api/edit/demo/domain",
        json={
            "use": {"field": "f_sales", "agg": "sum", "continuous": True},
            "worksheet": "ws_sales_by_region",
        },
    ).json()
    assert measure["kind"] == "range" and 0 < measure["min"] <= measure["max"]


def test_parameters_api(client: TestClient) -> None:
    created = client.post(
        "/api/edit/demo/parameters",
        json={
            "parameter": {
                "caption": "Threshold",
                "datatype": "real",
                "allowed": {"kind": "all"},
                "value": 0.5,
            }
        },
    )
    assert created.status_code == 200, created.text
    parameter = created.json()["parameter"]
    assert parameter["id"] == "p_threshold"
    listed = client.get("/api/edit/demo/catalog").json()["parameters"]
    assert any(p["id"] == "p_threshold" for p in listed)
    assert client.delete("/api/edit/demo/parameters/p_threshold").status_code == 200
    assert client.delete("/api/edit/demo/parameters/p_parameter_1").status_code == 422


def test_edits_are_saved_in_package(client: TestClient, tmp_path: Path) -> None:
    created = client.post("/api/edit/demo/worksheets", json={"name": "新的工作表"}).json()[
        "worksheet"
    ]
    created["columns"] = [
        {"field": "f_sales", "agg": "sum", "date": None, "continuous": True, "table_calc": None}
    ]
    assert (
        client.put(
            f"/api/edit/demo/worksheets/{created['id']}", json={"worksheet": created}
        ).status_code
        == 200
    )
    documents = client.get("/api/desktop/documents").json()
    assert documents[0]["modified"] is True
    target = tmp_path / "saved.tmigx"
    assert (
        client.post("/api/desktop/save", json={"workbook": "demo", "path": str(target)}).status_code
        == 200
    )
    assert client.get("/api/desktop/documents").json()[0]["modified"] is False
    with zipfile.ZipFile(target) as archive:
        assert "新的工作表" in archive.read("report.ir.json").decode("utf-8")


def test_dashboard_story_and_action_api(client: TestClient) -> None:
    outline = client.get("/api/edit/demo/outline").json()
    assert {w["id"] for w in outline["worksheets"]} >= {"ws_sales_by_region", "ws_monthly_profit"}
    created = client.post("/api/edit/demo/dashboards", json={"name": "新儀表板"}).json()[
        "dashboard"
    ]
    flow = created["zones"][0]["children"][0]
    flow["children"] = [
        {
            "id": "z3",
            "kind": "worksheet",
            "x": 4,
            "y": 4,
            "w": 992,
            "h": 792,
            "worksheet": "ws_monthly_profit",
            "source_ref": "/editor",
        }
    ]
    saved = client.put(f"/api/edit/demo/dashboards/{created['id']}", json={"dashboard": created})
    assert saved.status_code == 200, saved.text
    ir = client.get("/api/workbooks/demo/ir").json()
    board = next(d for d in ir["dashboards"] if d["id"] == created["id"])
    assert board["zones"][0]["children"][0]["children"][0]["worksheet"] == "ws_monthly_profit"
    # Story：新增、加入故事點
    story = client.post("/api/edit/demo/stories", json={}).json()["story"]
    story["points"].append({"id": "sp2", "caption": "新儀表板", "dashboard": created["id"]})
    assert (
        client.put(f"/api/edit/demo/stories/{story['id']}", json={"story": story}).status_code
        == 200
    )
    # Story 用到的 dashboard 不能刪
    refused = client.delete(f"/api/edit/demo/dashboards/{created['id']}")
    assert refused.status_code == 422 and "Story" in refused.json()["detail"]
    assert client.delete(f"/api/edit/demo/stories/{story['id']}").status_code == 200
    assert client.delete(f"/api/edit/demo/dashboards/{created['id']}").status_code == 200
    # 篩選動作：目標工作表加上指向它的篩選，檢視器依選取的標記篩選
    action = client.post(
        "/api/edit/demo/actions",
        json={
            "action": {
                "caption": "依區域",
                "kind": "filter",
                "activation": "select",
                "dashboard": "db_ee909b0c",
                "sources": ["ws_sales_by_region"],
                "targets": ["ws_region_share_by_year"],
                "clearing": "show_all",
                "fields": ["f_region"],
            }
        },
    )
    assert action.status_code == 200, action.text
    action_id = action.json()["action"]["id"]
    state = {"filters": {}, "parameters": {}, "actions": {action_id: [{"f_region": "West"}]}}
    rendered = client.post(
        "/api/workbooks/demo/worksheets/ws_region_share_by_year/render", json=state
    ).json()["table"]
    region = rendered["keys"].index("f_region")
    assert {row[region] for row in rendered["raw"]} == {"West"}
    assert client.delete(f"/api/edit/demo/actions/{action_id}").status_code == 200
    sheet = client.get("/api/edit/demo/worksheets/ws_region_share_by_year").json()["worksheet"]
    assert all(f.get("action") != action_id for f in sheet["filters"])
    # 醒目提示動作：目標工作表與比對的欄位（顯示名稱），不在目標工作表上加篩選
    highlight = client.post(
        "/api/edit/demo/actions",
        json={
            "action": {
                "caption": "醒目提示區域",
                "kind": "highlight",
                "activation": "hover",
                "dashboard": "db_ee909b0c",
                "sources": ["ws_sales_by_region"],
                "targets": ["ws_sales_by_region", "ws_region_share_by_year"],
                "highlight": {"fields": ["Region"]},
            }
        },
    )
    assert highlight.status_code == 200, highlight.text
    saved_action = highlight.json()["action"]
    assert (saved_action["kind"], saved_action["highlight"]) == (
        "highlight",
        {"fields": ["Region"]},
    )
    sheet = client.get("/api/edit/demo/worksheets/ws_region_share_by_year").json()["worksheet"]
    assert all(f.get("action") != saved_action["id"] for f in sheet["filters"])
    missing = client.post(
        "/api/edit/demo/actions",
        json={
            "action": {
                "caption": "x",
                "kind": "highlight",
                "activation": "select",
                "sources": ["ws_sales_by_region"],
            }
        },
    )
    assert missing.status_code == 422  # 醒目提示動作要有 highlight


def test_desktop_export_writes_the_file(client: TestClient, tmp_path: Path) -> None:
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    target = tmp_path / "匯出.png"
    response = client.post(
        "/api/desktop/export",
        json={"path": str(target), "content": base64.b64encode(png).decode()},
    )
    assert response.status_code == 200, response.text
    assert target.read_bytes() == png
    # 副檔名、資料夾與內容不正確時拒絕
    bad = client.post(
        "/api/desktop/export",
        json={"path": str(tmp_path / "x.exe"), "content": base64.b64encode(png).decode()},
    )
    assert bad.status_code == 422
    missing = client.post(
        "/api/desktop/export",
        json={"path": str(tmp_path / "nope" / "x.png"), "content": base64.b64encode(png).decode()},
    )
    assert missing.status_code == 422
    garbage = client.post("/api/desktop/export", json={"path": str(target), "content": "%%%"})
    assert garbage.status_code == 422
