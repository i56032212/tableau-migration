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
    assert data["title"] == data["name"]  # 沒有自訂標題時以工作表名稱為標題
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


def test_render_with_drill(client: TestClient) -> None:
    """階層的切入：Category Detail 的 Category 向下切入到 Sub-Category；Sub-Category Sales 收合到 Category。
    數字與存檔時就那樣放在架上的工作表相同（合計不變）。"""
    features = "/api/workbooks/superstore_features"
    url = f"{features}/worksheets/ws_category_detail/render"
    saved = client.post(url).json()
    assert saved["drill"] == [
        {
            "key": "f_category",
            "shelf": "rows",
            "label": "Category",
            "head": "Category",
            "next": "Sub-Category",
            "delta": 0,
            "can_down": True,
            "can_up": False,
        }
    ]
    drilled = client.post(url, json={"drill": {"f_category": 1}}).json()
    assert drilled["table"]["headers"][:2] == ["Category", "Sub-Category"]
    assert drilled["rows"] == 17
    assert drilled["drill"][0]["label"] == "Sub-Category"
    assert drilled["drill"][0]["next"] == "Product Name"
    assert drilled["drill"][0]["can_up"] is True

    def total(table: dict, column: int) -> float:
        return sum(float(row[column]) for row in table["raw"])

    sales = saved["table"]["headers"].index("Sales")
    assert total(drilled["table"], sales + 1) == pytest.approx(total(saved["table"], sales))
    # 下載的交叉資料表也是切入後的
    download = client.post(
        f"{features}/worksheets/ws_category_detail/download",
        json={"kind": "crosstab", "format": "csv", "state": {"drill": {"f_category": 1}}},
    )
    assert download.status_code == 200
    assert download.content.decode("utf-8-sig").splitlines()[0].startswith("Category,Sub-Category")

    url = f"{features}/worksheets/ws_sub_category_sales/render"
    collapsed = client.post(url, json={"drill": {"f_category": -1}}).json()
    assert "Sub-Category" not in collapsed["table"]["headers"]
    assert collapsed["rows"] == 3
    assert collapsed["drill"][0]["can_up"] is False
    assert client.post(url, json={"drill": {"f_category": "down"}}).status_code == 422


def test_render_tooltip_sheet(client: TestClient) -> None:
    """工具提示中的工作表（Viz in Tooltip）：Sales by Segment 的工具提示中顯示 Category Mix，以滑過的
    Segment 篩選；直接檢視 Category Mix 時顯示全部。"""
    features = "/api/workbooks/superstore_features"
    source = client.post(f"{features}/worksheets/ws_sales_by_segment/render").json()
    assert source["tooltip_sheets"] == [
        {
            "worksheet": "ws_category_mix",
            "name": "Category Mix",
            "max_width": 320,
            "max_height": 240,
        }
    ]
    url = f"{features}/worksheets/ws_category_mix/render"
    everything = client.post(url).json()
    assert everything["tooltip_sheets"] == []

    def sales(data: dict) -> dict[str, float]:
        keys = data["table"]["keys"]
        category, total = keys.index("f_category"), keys.index("sum(f_sales)")
        return {row[category]: float(row[total]) for row in data["table"]["raw"]}

    segments = {}
    for segment in ("Consumer", "Corporate", "Home Office"):
        tip = {"source": "ws_sales_by_segment", "mark": {"f_segment": segment}}
        data = client.post(url, json={"tooltip": tip}).json()
        assert data["rows"] == 3
        segments[segment] = sales(data)
    # 三個區隔的合計等於全部
    for category, total in sales(everything).items():
        assert sum(s[category] for s in segments.values()) == pytest.approx(total)
    assert segments["Consumer"] != segments["Corporate"]
    # 不在工具提示中的組合不接受
    bad = {"tooltip": {"source": "ws_region_picker", "mark": {"f_region": "East"}}}
    assert client.post(url, json=bad).status_code == 422


def test_render_keep_only_and_exclude(client: TestClient) -> None:
    """工具提示的命令按鈕：只保留、排除選取的標記（等同以標記的維度值篩選），依序套用；量值的欄位鍵略過。"""
    url = "/api/workbooks/superstore_features/worksheets/ws_category_mix/render"

    def categories(marks: list[dict[str, object]]) -> list[str]:
        response = client.post(url, json={"marks": marks})
        assert response.status_code == 200, response.text
        table = response.json()["table"]
        return sorted(row[table["keys"].index("f_category")] for row in table["raw"])

    everything = categories([])
    assert everything == ["Furniture", "Office Supplies", "Technology"]
    furniture = {"f_category": "Furniture", "sum(f_sales)": 1.0}
    assert categories([{"mode": "keep", "marks": [furniture]}]) == ["Furniture"]
    assert categories([{"mode": "exclude", "marks": [furniture]}]) == [
        "Office Supplies",
        "Technology",
    ]
    two = [{"f_category": "Furniture"}, {"f_category": "Technology"}]
    assert categories([{"mode": "keep", "marks": two}]) == ["Furniture", "Technology"]
    # 先只保留兩個、再排除其中一個
    steps = [{"mode": "keep", "marks": two}, {"mode": "exclude", "marks": [furniture]}]
    assert categories(steps) == ["Technology"]

    # 沒有共同維度的標記（地圖的選取工具同時選到不同標記圖層的標記）：依各自的維度分組，都要符合
    def sales(marks: list[dict[str, object]]) -> dict[str, float]:
        table = client.post(url, json={"marks": marks}).json()["table"]
        at = table["keys"].index("f_category")
        value = next(i for i, role in enumerate(table["roles"]) if role == "measure")
        return {row[at]: float(row[value]) for row in table["raw"]}

    mixed = [{"f_category": "Furniture"}, {"f_segment": "Consumer"}]
    both = sales([{"mode": "keep", "marks": mixed}])
    assert list(both) == ["Furniture"]
    assert 0 < both["Furniture"] < sales([{"mode": "keep", "marks": [furniture]}])["Furniture"]
    without = sales([{"mode": "exclude", "marks": mixed}])
    assert sorted(without) == ["Office Supplies", "Technology"]
    assert without["Technology"] < sales([])["Technology"]
    # 沒有維度的標記不篩選；格式不對的不接受
    assert categories([{"mode": "keep", "marks": [{"sum(f_sales)": 1.0}]}]) == everything
    assert client.post(url, json={"marks": [{"mode": "keep", "marks": []}]}).status_code == 422
    assert (
        client.post(url, json={"marks": [{"mode": "only", "marks": [furniture]}]}).status_code
        == 422
    )


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
    features = "/api/workbooks/superstore_features"
    assert client.post(f"{features}/fields/year(f_order_date)/domain").json()["values"] == [
        2021,
        2022,
        2023,
        2024,
    ]
    assert client.post(f"{features}/fields/f_order_date/domain").json() == {
        "kind": "range",
        "min": "2021-01-03",
        "max": "2024-12-30",
    }
    assert (
        client.post(f"{WB}/fields/f_segment/domain", json={"kind": "values"}).json()["kind"]
        == "values"
    )
    assert client.post(f"{WB}/fields/f_nope/domain").status_code == 404
    assert client.post(f"{WB}/fields/f_category/domain", json={"sql": "x"}).status_code == 422
    # 報表上沒有篩選控制項的欄位不列出可選值（與 Tableau 相同，避免列舉其他欄位的值）
    response = client.post(f"{WB}/fields/f_sales/domain", json={"kind": "range"})
    assert response.status_code == 403
    assert "沒有篩選控制項" in response.json()["detail"]


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
        "sets": {},
        "today": None,
        "pages": {},
        "drill": {},
        "tooltip": None,
        "viewport": None,
        "marks": [],
    }


def test_package_images(client: TestClient) -> None:
    """圖片區塊與圖片按鈕：只提供 .twbx 內的圖片。"""
    features = "/api/workbooks/superstore_features"
    response = client.get(f"{features}/files/Image/logo.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert client.get(f"{features}/files/Image/missing.png").status_code == 404
    assert client.get(f"{features}/files/superstore_features.twb").status_code == 404
    assert client.get(f"{features}/files/Data/Extracts/x.hyper").status_code == 404
    assert client.get("/api/workbooks/nope/files/Image/logo.png").status_code == 404


def test_tooltip_sheet_is_allowed_with_its_source() -> None:
    """只能看部分 view 時，可以看的工作表的工具提示中的工作表也可以看（常是隱藏的工作表）。"""
    from tmig.ir import iter_zones, load_ir
    from tmig_service.access import Grant

    ir = load_ir(ROOT / "fixtures" / "superstore_features" / "golden.ir.json")
    board = next(
        d
        for d in ir.dashboards
        if any(z.worksheet == "ws_sales_by_segment" for z in iter_zones(d.zones))
    )
    assert all(z.worksheet != "ws_category_mix" for z in iter_zones(board.zones))
    grant = Grant(allowed=True, views=frozenset({board.name}))
    assert grant.worksheet_allowed(ir, "ws_sales_by_segment")
    assert grant.worksheet_allowed(ir, "ws_category_mix")
    assert not grant.worksheet_allowed(ir, "ws_region_picker")
    assert not Grant(allowed=True, views=frozenset({"沒有的 view"})).worksheet_allowed(
        ir, "ws_category_mix"
    )


def test_dynamic_parameters(tmp_path: Path) -> None:
    """動態參數：開啟報表時的目前值與允許值以目前的資料取值（檢視器的 IR 與查詢都用取值後的參數）。"""
    import shutil

    from tmig.edit import upsert_calc, upsert_parameter
    from tmig.ir import load_ir, save_ir

    fixtures = tmp_path / "fixtures"
    shutil.copytree(ROOT / "fixtures" / "superstore_demo", fixtures / "superstore_demo")
    golden = fixtures / "superstore_demo" / "golden.ir.json"
    ir = load_ir(golden)
    ir, latest = upsert_calc(ir, "ds_orders", "Latest", "{MAX([Order Date])}")
    ir, _ = upsert_parameter(
        ir,
        {
            "caption": "As Of",
            "datatype": "date",
            "value": "2020-01-01",
            "allowed": {"kind": "all"},
            "value_field": latest.id,
        },
    )
    ir, _ = upsert_parameter(
        ir,
        {
            "caption": "Region Choice",
            "datatype": "string",
            "value": "East",
            "allowed": {"kind": "list", "values": ["East"]},
            "domain_field": "f_region",
        },
    )
    save_ir(ir, golden)
    client = TestClient(create_app(fixtures, ROOT / "no-web-dist"))
    params = {p["caption"]: p for p in client.get(f"{WB}/ir").json()["parameters"]}
    assert params["As Of"]["value"] == "2024-12-30"
    assert params["Region Choice"]["allowed"]["values"] == ["Central", "East", "South", "West"]
    assert params["Region Choice"]["value"] == "East"
    # 報表本身（golden.ir.json）不變
    assert {p.caption: p.value for p in load_ir(golden).parameters}["As Of"] == "2020-01-01"
