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


def use(field: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": field,
        "agg": None,
        "date": None,
        "continuous": False,
        "table_calc": None,
        **extra,
    }


def blank(client: TestClient, **parts: Any) -> dict[str, Any]:
    """把 Sales by Region 換成指定的欄列架（沒有篩選、排序、標記卡上的欄位與 Table Calculation）。"""
    ws = sheet(client, "ws_sales_by_region")
    marks = {
        **ws["marks"],
        "color": None,
        "size": None,
        "label": [],
        "detail": [],
        "tooltip": [],
        "color_palette": None,
    }
    ws.update(filters=[], sorts=[], table_calcs=[], panes=[], subtotals=[], totals=[], marks=marks)
    ws.update(parts)
    return ws


def put(client: TestClient, ws: dict[str, Any]) -> dict[str, Any]:
    response = client.put(f"/api/edit/demo/worksheets/{ws['id']}", json={"worksheet": ws})
    assert response.status_code == 200, response.text
    return response.json()["worksheet"]


def column(table: dict[str, Any], key: str) -> list[Any]:
    index = table["keys"].index(key)
    return [row[index] for row in table["raw"]]


def test_sort_dialog_options(client: TestClient) -> None:
    """排序對話框：手動排列成員、依另一個欄位的彙總、資料來源順序。"""
    sales = use("f_sales", agg="sum", continuous=True)
    ws = blank(client, rows=[use("f_region")], columns=[sales])
    manual = ["West", "Central", "South", "East"]
    ws["sorts"] = [
        {"field": "f_region", "kind": "manual", "order": "asc", "by": None, "manual_values": manual}
    ]
    put(client, ws)
    assert column(render(client, "ws_sales_by_region")["table"], "f_region") == manual
    # 依不在檢視中的欄位（利潤的平均）遞減
    by = use("f_profit", agg="avg", continuous=True)
    ws["sorts"] = [
        {"field": "f_region", "kind": "by_field", "order": "desc", "by": by, "manual_values": []}
    ]
    put(client, ws)
    regions = column(render(client, "ws_sales_by_region")["table"], "f_region")
    ws["columns"] = [by]
    ws["sorts"] = []
    put(client, ws)
    table = render(client, "ws_sales_by_region")["table"]
    profit = dict(zip(column(table, "f_region"), column(table, "avg(f_profit)"), strict=True))
    assert regions == sorted(profit, key=lambda r: -float(profit[r]))
    # 資料來源順序
    ws["sorts"] = [
        {
            "field": "f_region",
            "kind": "data_source",
            "order": "asc",
            "by": None,
            "manual_values": [],
        }
    ]
    put(client, ws)
    assert sorted(column(render(client, "ws_sales_by_region")["table"], "f_region")) == sorted(
        manual
    )


def test_date_table_calcs_from_editor(client: TestClient) -> None:
    """編輯器的「年初至今總計」「年成長率」「複合成長率」與表計算對話框的選項（相對於、計算層級、重新開始間隔）。"""
    year = use("f_order_date", date={"kind": "part", "unit": "year"})
    quarter = use("f_order_date", date={"kind": "part", "unit": "quarter"})
    sales = use("f_sales", agg="sum", continuous=True)

    def calc(calc_id: str, kind: str, options: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "id": calc_id,
            "type": kind,
            "preset": "specific",
            "addressing": ["f_order_date"],
            "instances": [year, quarter],
            "level": None,
            "restart": None,
            "inferred": False,
            "options": options,
            "nested": [],
            "source_ref": "/editor",
            **extra,
        }

    ws = blank(client, rows=[year, quarter], columns=[sales])
    put(client, ws)
    table = render(client, "ws_sales_by_region")["table"]
    keys = [
        (int(y), int(q))
        for y, q in zip(
            column(table, "year(f_order_date)"),
            column(table, "quarter(f_order_date)"),
            strict=True,
        )
    ]
    base = dict(zip(keys, [float(v) for v in column(table, "sum(f_sales)")], strict=True))
    assert len({y for y, _ in keys}) >= 2 and len(keys) >= 8

    def values(tc: dict[str, Any]) -> dict[tuple[Any, Any], Any]:
        edited = dict(ws)
        edited["table_calcs"] = [tc]
        edited["columns"] = [{**sales, "table_calc": tc["id"]}]
        put(client, edited)
        found = render(client, "ws_sales_by_region")["table"]
        return dict(zip(keys, column(found, f"sum(f_sales)#{tc['id']}"), strict=True))

    # 年初至今總計：每一年重新累計
    ytd = values(calc("tc_ytd", "running_total", {"aggregation": "sum"}, restart=year))
    for (y, q), value in ytd.items():
        expected = sum(v for (y2, q2), v in base.items() if y2 == y and q2 <= q)
        assert float(value) == pytest.approx(expected)
    # 年成長率：每一季與前一年的同一季比較（計算層級是年）
    yoy = values(calc("tc_yoy", "percent_difference", {"offset": -1}, level=year))
    for (y, q), value in yoy.items():
        previous = base.get((y - 1, q))
        if previous is None:
            assert value in (None, "")  # 第一年沒有前一年
        else:
            assert float(value) == pytest.approx((base[(y, q)] - previous) / abs(previous))
    # 複合成長率：相對於第一個，POWER(目前 / 第一個, 1 / 相隔的標記數) - 1
    first = base[keys[0]]
    compound = values(
        calc("tc_cagr", "percent_difference", {"relative_to": "first", "compounded": True})
    )
    for n, key in enumerate(keys[1:], start=1):
        assert float(compound[key]) == pytest.approx((base[key] / first) ** (1 / n) - 1)
    # 相對於最後一個的差異
    last = values(calc("tc_last", "difference", {"relative_to": "last"}))
    for key in keys:
        assert float(last[key]) == pytest.approx(base[key] - base[keys[-1]])


def test_wildcard_filter_and_annotations(client: TestClient) -> None:
    """萬用字元篩選（與 Tableau 寫進 .twb 的公式相同）與註解（標記、點、區域）。"""
    sales = use("f_sales", agg="sum", continuous=True)
    ws = blank(client, rows=[use("f_region")], columns=[sales])

    def wildcard(condition: str) -> list[Any]:
        ws["filters"] = [
            {
                "kind": "condition",
                "id": "flt_region_wildcard",
                "field": use("f_region"),
                "stage": "dimension",
                "condition": condition,
                "mode": "all",
                "values": [],
                "group": None,
                "source_ref": "/editor",
            }
        ]
        put(client, ws)
        return sorted(column(render(client, "ws_sales_by_region")["table"], "f_region"))

    assert wildcard("CONTAINS(LOWER([Region]), LOWER('ST'))") == ["East", "West"]
    assert wildcard("STARTSWITH(LOWER([Region]), LOWER('c'))") == ["Central"]
    assert wildcard("ENDSWITH(LOWER([Region]), LOWER('TH'))") == ["South"]
    assert wildcard("LOWER([Region]) = LOWER('east')") == ["East"]
    assert wildcard("ISNULL([Region]) OR (NOT(CONTAINS(LOWER([Region]), LOWER('st'))))") == [
        "Central",
        "South",
    ]
    # 註解：標記（East）、點與區域（Sales 的座標軸上的值）
    ws["filters"] = []
    point = {"x": 300000, "x_axis": sales, "y": None, "y_axis": None, "mark": None}
    ws["annotations"] = [
        {
            "kind": "mark",
            "text": [{"text": "東區最高"}],
            "point": {
                "x": None,
                "x_axis": None,
                "y": None,
                "y_axis": None,
                "mark": {"f_region": "East"},
            },
            "offset": [40, -40],
        },
        {"kind": "point", "text": [{"text": "目標"}], "point": point, "offset": [40, -40]},
        {
            "kind": "area",
            "text": [{"text": "達標範圍"}],
            "top_left": point,
            "bottom_right": {**point, "x": 600000},
            "offset": [0, 0],
        },
    ]
    saved = put(client, ws)
    assert [a["kind"] for a in saved["annotations"]] == ["mark", "point", "area"]
    rendered = render(client, "ws_sales_by_region")
    assert rendered.get("unsupported") in (None, [])
    import json

    text = json.dumps(rendered["spec"], ensure_ascii=False)
    assert all(label in text for label in ("東區最高", "目標", "達標範圍"))


def test_apply_filter_to_worksheets(client: TestClient) -> None:
    """套用到工作表：選取的工作表上各有一個相同、同群組的篩選；改了內容一起改；改回僅此工作表時移除。"""
    ws = sheet(client, "ws_sales_by_region")
    category = next(f for f in ws["filters"] if f["field"]["field"] == "f_category")
    category.update(mode="include", values=["Furniture"])
    put(client, ws)
    base = f"/api/edit/demo/worksheets/ws_sales_by_region/filters/{category['id']}/apply"
    listed = client.get(base).json()["worksheets"]
    assert {w["id"] for w in listed} == {
        "ws_monthly_profit",
        "ws_region_share_by_year",
        "ws_sub_category_profit",
    }
    assert not any(w["applied"] for w in listed)
    applied = client.post(base, json={"targets": ["ws_monthly_profit", "ws_sub_category_profit"]})
    assert applied.status_code == 200, applied.text
    group = next(f for f in applied.json()["worksheet"]["filters"] if f["id"] == category["id"])[
        "group"
    ]
    assert group == f"group:{category['id']}"

    def grouped(worksheet: str) -> list[dict[str, Any]]:
        return [f for f in sheet(client, worksheet)["filters"] if f.get("group") == group]

    for target in ("ws_monthly_profit", "ws_sub_category_profit"):
        (copy,) = grouped(target)
        assert (copy["field"]["field"], copy["mode"], copy["values"]) == (
            "f_category",
            "include",
            ["Furniture"],
        )
        assert copy["id"] != category["id"]
    assert grouped("ws_region_share_by_year") == []
    assert {w["id"] for w in client.get(base).json()["worksheets"] if w["applied"]} == {
        "ws_monthly_profit",
        "ws_sub_category_profit",
    }
    # 檢視的結果：Sub-Category Profit 只剩 Furniture 的子類別
    table = render(client, "ws_sub_category_profit")["table"]
    assert set(column(table, "f_sub_category")) == {"Bookcases", "Chairs", "Furnishings", "Tables"}
    # 在這張工作表上改了篩選：其他工作表上的一起改
    ws = sheet(client, "ws_sales_by_region")
    next(f for f in ws["filters"] if f["id"] == category["id"]).update(values=["Technology"])
    put(client, ws)
    assert all(
        grouped(t)[0]["values"] == ["Technology"]
        for t in ("ws_monthly_profit", "ws_sub_category_profit")
    )
    # 改成只套用到 Monthly Profit：Sub-Category Profit 上的移除
    assert client.post(base, json={"targets": ["ws_monthly_profit"]}).status_code == 200
    assert len(grouped("ws_monthly_profit")) == 1 and grouped("ws_sub_category_profit") == []
    # 僅此工作表：都移除，篩選不再有群組
    only = client.post(base, json={"targets": []}).json()["worksheet"]
    assert next(f for f in only["filters"] if f["id"] == category["id"])["group"] is None
    assert grouped("ws_monthly_profit") == []
    # 動作產生的篩選、不存在的篩選
    assert client.get(base.replace(category["id"], "flt_nope")).status_code == 404
    assert (
        client.post(base.replace(category["id"], "flt_nope"), json={"targets": []}).status_code
        == 422
    )


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


def test_analytics_fields_api(client: TestClient) -> None:
    """分析的欄位：新的叢集欄位放到色彩上依量值分組（以 Cluster N 顯示）；Forecast Indicator 只有一個。"""
    url = "/api/edit/demo/analytics-fields"
    cluster = client.post(url, json={"datasource": "ds_orders", "kind": "cluster"}).json()["field"]
    first = client.post(url, json={"datasource": "ds_orders", "kind": "forecast_indicator"})
    again = client.post(url, json={"datasource": "ds_orders", "kind": "forecast_indicator"})
    assert first.json()["field"]["id"] == again.json()["field"]["id"]
    bad = client.post(url, json={"datasource": "ds_orders", "kind": "outlier"})
    assert bad.status_code == 422
    ws = sheet(client, "ws_sub_category_profit")
    ws["marks"]["color"] = {
        "field": cluster["id"],
        "agg": None,
        "date": None,
        "continuous": False,
        "table_calc": None,
    }
    ws["cluster"] = {"field": cluster["id"], "measures": [ws["columns"][0]], "count": 2}
    saved = client.put(f"/api/edit/demo/worksheets/{ws['id']}", json={"worksheet": ws})
    assert saved.status_code == 200, saved.text
    spec = render(client, ws["id"])["spec"]
    shown = {v for r in spec["data"]["values"] for v in r.values() if isinstance(v, str)}
    assert {"Cluster 1", "Cluster 2"} <= shown


def test_hierarchies_api(client: TestClient) -> None:
    """資料窗格建立、改變與刪除階層；檢視器依新的階層切入。"""
    url = "/api/edit/demo/datasources/ds_orders/hierarchies"
    response = client.put(url, json={"name": "Geo", "fields": ["f_region", "f_state_province"]})
    assert response.status_code == 200, response.text
    assert response.json()["hierarchy"] == {
        "name": "Geo",
        "fields": ["f_region", "f_state_province"],
    }
    catalog = client.get("/api/edit/demo/catalog").json()
    assert catalog["datasources"][0]["hierarchies"] == [
        {"name": "Geo", "fields": ["f_region", "f_state_province"]}
    ]
    # Sales by Region 的 Region 可以向下切入到 State/Province
    data = render(client, "ws_sales_by_region")
    assert [(d["key"], d["next"]) for d in data["drill"]] == [("f_region", "State/Province")]
    url_render = "/api/workbooks/demo/worksheets/ws_sales_by_region/render"
    drilled = client.post(url_render, json={"drill": {"f_region": 1}}).json()
    assert "State/Province" in drilled["table"]["headers"]
    assert drilled["rows"] > data["rows"]
    bad = client.put(url, json={"name": "Bad", "fields": ["f_sales"]})
    assert bad.status_code == 422 and "不是維度" in bad.json()["detail"]
    renamed = client.put(
        url, json={"name": "Location", "fields": ["f_region", "f_city"], "original": "Geo"}
    )
    assert renamed.json()["hierarchy"]["name"] == "Location"
    assert client.delete(url, params={"name": "Location"}).json() == {"ok": True}
    assert client.get("/api/edit/demo/catalog").json()["datasources"][0]["hierarchies"] == []
    assert client.delete(url, params={"name": "Location"}).status_code == 422
    assert render(client, "ws_sales_by_region")["drill"] == []


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


def test_url_action_target_is_a_web_object(client: TestClient) -> None:
    """網址動作的目標：dashboard 上的網頁物件（可以是空白的）；網頁物件移除後改回預設。"""
    board = client.get("/api/edit/demo/dashboards/db_ee909b0c").json()["dashboard"]
    web = {
        "id": "z900",
        "kind": "web",
        "x": 600,
        "y": 400,
        "w": 300,
        "h": 200,
        "source_ref": "/editor",
    }
    board["zones"].append(web)
    saved = client.put("/api/edit/demo/dashboards/db_ee909b0c", json={"dashboard": board})
    assert saved.status_code == 200, saved.text

    def url_action(**url: object) -> dict[str, object]:
        return {
            "action": {
                "caption": "開啟區域頁",
                "kind": "url",
                "activation": "select",
                "dashboard": "db_ee909b0c",
                "sources": ["ws_sales_by_region"],
                "url": {
                    "template": "https://example.com/region?name=<Region>",
                    "fields": [{"placeholder": "<Region>", "key": "f_region"}],
                    **url,
                },
            }
        }

    created = client.post("/api/edit/demo/actions", json=url_action(target="zone", zone="z900"))
    assert created.status_code == 200, created.text
    action = created.json()["action"]
    assert (action["url"]["target"], action["url"]["zone"]) == ("zone", "z900")
    # 指定的區塊不是這個 dashboard 上的網頁物件
    refused = client.post("/api/edit/demo/actions", json=url_action(target="zone", zone="z1"))
    assert refused.status_code == 422 and "網頁物件" in refused.json()["detail"]
    # zone 與 target 要一致
    assert client.post("/api/edit/demo/actions", json=url_action(target="zone")).status_code == 422
    # 沒有寫目標：預設（有網頁物件時載入其中，否則新分頁）
    default = client.post("/api/edit/demo/actions", json=url_action()).json()["action"]
    assert (default["url"]["target"], default["url"]["zone"]) == ("auto", None)
    # 移除網頁物件：指向它的動作改回預設
    board["zones"] = [z for z in board["zones"] if z["id"] != "z900"]
    saved = client.put("/api/edit/demo/dashboards/db_ee909b0c", json={"dashboard": board})
    assert saved.status_code == 200, saved.text
    actions = {a["id"]: a for a in client.get("/api/workbooks/demo/ir").json()["actions"]}
    assert (actions[action["id"]]["url"]["target"], actions[action["id"]]["url"]["zone"]) == (
        "auto",
        None,
    )


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


def test_geography_and_map_api(client: TestClient) -> None:
    """資料窗格指定地理角色後，Latitude／Longitude (generated) 在欄列架上的工作表畫成地圖；底圖與形狀
    以 /api/geo/<版本>/ 取得（不需登入，可以長期快取）。"""
    url = "/api/edit/demo/datasources/ds_orders/fields/f_city/geography"
    response = client.put(url, json={"role": "city", "aliases": []})
    assert response.status_code == 200, response.text
    bad = client.put(
        "/api/edit/demo/datasources/ds_orders/fields/f_sales/geography", json={"role": "state"}
    )
    assert bad.status_code == 422
    defaults = client.put(
        "/api/edit/demo/datasources/ds_orders/geography", json={"country": "United States"}
    )
    assert defaults.status_code == 200, defaults.text
    catalog = client.get("/api/edit/demo/catalog").json()["datasources"][0]
    assert catalog["geo"] == {"country": "United States", "state": None}
    roles = {f["id"]: f["geo_role"] for f in catalog["fields"] if f["geo_role"]}
    assert roles == {"f_city": "city", "f_state_province": "state", "f_postal_code": "zip"}

    created = client.post("/api/edit/demo/worksheets", json={"name": "Map"}).json()["worksheet"]

    def use(field: str, continuous: bool = False, agg: str | None = None) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": None,
            "continuous": continuous,
            "table_calc": None,
        }

    created["rows"] = [use("latitude_generated", True)]
    created["columns"] = [use("longitude_generated", True)]
    created["marks"]["type"] = "map"
    created["marks"]["detail"] = [use("f_state_province")]
    created["marks"]["color"] = use("f_sales", True, "sum")
    created["map"] = {"style": "dark", "washout": 0.2, "layers": {"cities": True}}
    saved = client.put(f"/api/edit/demo/worksheets/{created['id']}", json={"worksheet": created})
    assert saved.status_code == 200, saved.text
    assert saved.json()["worksheet"]["map"]["style"] == "dark"
    data = render(client, created["id"])
    spec = data["spec"]
    assert data["mark"] == "map" and spec["projection"]["type"] == "mercator"
    lookups = [
        layer["transform"][1]["from"]["data"]["url"]
        for layer in spec["layer"]
        if layer.get("mark", {}).get("type") == "geoshape" and "transform" in layer
    ]
    assert lookups and lookups[0].endswith("/admin1/USA.json")
    assert all(r["__lon0"] is not None for r in spec["data"]["values"])

    shape = client.get(lookups[0], headers={"Accept-Encoding": "gzip"})
    assert shape.status_code == 200
    assert shape.headers["cache-control"].startswith("public")
    assert set(shape.json()["objects"]) == {"admin1", "borders"}
    for path in (
        "/api/geo/x/../../places.parquet",
        "/api/geo/x/nothing.json",
        "/api/geo/x/manifest",
    ):
        assert client.get(path).status_code == 404


def test_sheet_controls_and_relevant_values(client: TestClient) -> None:
    """工作表上的控制項（顯示篩選、顯示參數）與「只顯示相關的值」：可選值只列出套用其他篩選之後
    還有資料的值（自己的選取不算），檢視器目前的選取也算。"""
    ws = sheet(client, "ws_sales_by_region")
    category = next(f for f in ws["filters"] if f["field"]["field"] == "f_category")
    category.update(mode="include", values=["Furniture"])
    sub = {
        **category,
        "id": "flt_sub",
        "field": use("f_sub_category"),
        "mode": "include",
        "values": ["Chairs"],
    }
    ws["filters"] = [category, sub, *(f for f in ws["filters"] if f["id"] != category["id"])]
    ws["controls"] = [
        {
            "kind": "filter",
            "field": use("f_sub_category"),
            "parameter": None,
            "control": "multi_list",
            "relevant": True,
        },
        {
            "kind": "filter",
            "field": use("f_segment"),  # 工作表上沒有這個篩選：存檔時移除
            "parameter": None,
            "control": "multi_list",
            "relevant": False,
        },
    ]
    saved = put(client, ws)
    assert [c["field"]["field"] for c in saved["controls"]] == ["f_sub_category"]

    domain = "/api/workbooks/demo/fields/f_sub_category/domain"
    everything = client.post(domain).json()["values"]
    assert len(everything) == 17  # 工作表上有控制項的欄位可以列出可選值
    furniture = ["Bookcases", "Chairs", "Furnishings", "Tables"]
    relevant = client.post(domain, json={"worksheet": "ws_sales_by_region"})
    assert relevant.status_code == 200, relevant.text
    assert relevant.json()["values"] == furniture  # 自己的選取（Chairs）不算
    # 檢視器把 Category 改成 Technology
    state = {"filters": {"f_category": {"mode": "include", "values": ["Technology"]}}}
    changed = client.post(domain, json={"worksheet": "ws_sales_by_region", "state": state})
    assert changed.json()["values"] == ["Accessories", "Copiers", "Machines", "Phones"]
    assert client.post(domain, json={"worksheet": "ws_nope"}).status_code == 404

    # 移除篩選時控制項一併移除
    ws = sheet(client, "ws_sales_by_region")
    ws["filters"] = [f for f in ws["filters"] if f["id"] != "flt_sub"]
    assert put(client, ws)["controls"] == []


def test_datasource_filters(client: TestClient) -> None:
    """資料源篩選：逐列套用到資料源的所有工作表與可選值；萃取篩選不變；Top N 與量值不能當作資料源篩選。"""
    base = "/api/edit/demo/datasources/ds_orders/filters"
    original = client.get("/api/edit/demo/catalog").json()
    assert original["datasources"][0]["id"] == "ds_orders"
    (country,) = client.get(base).json()["filters"]  # .twb 原有的資料源篩選
    assert (country["field"]["field"], country["values"]) == ("f_country_region", ["United States"])
    region = {
        "kind": "categorical",
        "id": "dsf_region",
        "field": use("f_region"),
        "stage": "dimension",  # 一律存成資料源階段
        "mode": "include",
        "values": ["East", "West"],
        "ranges": [],
        "group": None,
        "source_ref": "/editor",
    }
    saved = client.put(base, json={"filters": [country, region]})
    assert saved.status_code == 200, saved.text
    assert [(f["id"], f["stage"]) for f in saved.json()["filters"]] == [
        ("flt_country_region", "datasource"),
        ("dsf_region", "datasource"),
    ]
    assert client.get(base).json()["filters"][1]["values"] == ["East", "West"]

    # 所有工作表只剩 East、West（Sales by Region 原本有 Top N 篩選，在資料源篩選之後）
    ws = blank(client, rows=[use("f_region")], columns=[use("f_sales", agg="sum", continuous=True)])
    put(client, ws)
    assert column(render(client, "ws_sales_by_region")["table"], "f_region") == ["East", "West"]
    # 可選值也只有通過資料源篩選的值；編輯這個資料源篩選時不套用它自己
    domain = {"use": use("f_region"), "kind": "values"}
    assert client.post("/api/edit/demo/domain", json=domain).json()["values"] == ["East", "West"]
    everything = client.post(
        "/api/edit/demo/domain", json={**domain, "exclude_filter": "dsf_region"}
    )
    assert everything.json()["values"] == ["Central", "East", "South", "West"]

    # 不能逐列套用的篩選
    top = {
        "kind": "top_n",
        "id": "dsf_top",
        "field": use("f_region"),
        "stage": "top_n",
        "direction": "top",
        "count": 2,
        "by": "SUM([Sales])",
        "mode": "all",
        "values": [],
        "source_ref": "/editor",
    }
    refused = client.put(base, json={"filters": [region, top]})
    assert refused.status_code == 422 and "Top N" in refused.json()["detail"]
    measure = {**region, "id": "dsf_sales", "field": use("f_sales", agg="sum", continuous=True)}
    refused = client.put(base, json={"filters": [measure]})
    assert refused.status_code == 422 and "彙總" in refused.json()["detail"]
    aggregate = {
        "kind": "condition",
        "id": "dsf_cond",
        "field": use("f_region"),
        "stage": "dimension",
        "condition": "SUM([Sales]) > 0",
        "mode": "all",
        "values": [],
        "group": None,
        "source_ref": "/editor",
    }
    refused = client.put(base, json={"filters": [aggregate]})
    assert refused.status_code == 422 and "不能當作資料源篩選" in refused.json()["detail"]
    assert client.put(base.replace("ds_orders", "ds_nope"), json={"filters": []}).status_code == 422
    # 失敗的不會留下：仍是原本的兩個篩選
    assert [f["id"] for f in client.get(base).json()["filters"]] == [
        "flt_country_region",
        "dsf_region",
    ]

    # 移除後恢復
    assert len(client.put(base, json={"filters": [country]}).json()["filters"]) == 1
    assert len(column(render(client, "ws_sales_by_region")["table"], "f_region")) == 4


def test_combined_sets(client: TestClient) -> None:
    """合併集合：同一個維度上兩個集合的聯集、交集與差集；來源的集合不能刪除，不同維度的集合不能合併。"""
    base = "/api/edit/demo/datasources/ds_orders"

    def make(caption: str, rule: dict[str, Any], level: str = "f_sub_category") -> dict[str, Any]:
        response = client.post(
            f"{base}/sets", json={"levels": [level], "caption": caption, "rule": rule}
        )
        assert response.status_code == 200, response.text
        return response.json()["field"]

    top = make("Top 5", {"kind": "top_n", "direction": "top", "count": 5, "by": "SUM([Sales])"})
    rich = make("Profitable", {"kind": "condition", "condition": "SUM([Profit]) > 30000"})
    region = make("East", {"kind": "members", "mode": "include", "members": [["East"]]}, "f_region")

    def combined(op: str, left: str = top["id"], right: str = rich["id"]) -> Any:
        rule = {"kind": "combined", "left": left, "right": right, "op": op}
        return client.post(
            f"{base}/sets", json={"levels": [], "caption": f"Combined {op}", "rule": rule}
        )

    fields = {}
    for op in ("union", "intersect", "left_only", "right_only"):
        response = combined(op)
        assert response.status_code == 200, response.text
        fields[op] = response.json()["field"]
    both = fields["intersect"]
    assert both["set"]["rule"] == {
        "kind": "combined",
        "left": top["id"],
        "right": rich["id"],
        "op": "intersect",
    }
    assert [u["field"] for u in both["set"]["levels"]] == ["f_sub_category"]
    assert both["calc"] is not None and both["datatype"] == "boolean"

    ws = blank(
        client,
        rows=[
            use("f_sub_category"),
            use(top["id"]),
            use(rich["id"]),
            *(use(f["id"]) for f in fields.values()),
        ],
        columns=[use("f_sales", agg="sum", continuous=True)],
    )
    put(client, ws)
    table = render(client, "ws_sales_by_region")["table"]

    def members(field_id: str) -> set[str]:
        return {
            name
            for name, inside in zip(
                column(table, "f_sub_category"), column(table, field_id), strict=True
            )
            if inside == "true"
        }

    a, b = members(top["id"]), members(rich["id"])
    assert len(a) == 5 and b and a != b
    assert members(fields["union"]["id"]) == a | b
    assert members(fields["intersect"]["id"]) == a & b
    assert members(fields["left_only"]["id"]) == a - b
    assert members(fields["right_only"]["id"]) == b - a

    # 不同維度、同一個集合、不是集合
    refused = combined("union", top["id"], region["id"])
    assert refused.status_code == 422 and "同一個維度" in refused.json()["detail"]
    refused = combined("union", top["id"], top["id"])
    assert refused.status_code == 422 and "不同的集合" in refused.json()["detail"]
    refused = combined("union", top["id"], "f_region")
    assert refused.status_code == 422 and "不是其他的集合" in refused.json()["detail"]
    # 來源的集合還在用：不能刪除
    removed = client.delete(f"{base}/calcs/{top['id']}")
    assert removed.status_code == 422, removed.text


def test_nested_sort(client: TestClient) -> None:
    """巢狀排序：Sub-Category 在每個 Region 之內各自依業績排序；不是巢狀時各 Region 的順序相同（依全部資料）。"""
    sales = use("f_sales", agg="sum", continuous=True)
    ws = blank(client, rows=[use("f_region"), use("f_sub_category")], columns=[sales])

    def by_region(nested: bool) -> dict[str, list[tuple[str, float]]]:
        ws["sorts"] = [
            {
                "field": "f_sub_category",
                "kind": "by_field",
                "order": "desc",
                "by": sales,
                "nested": nested,
                "manual_values": [],
            }
        ]
        saved = put(client, ws)
        assert saved["sorts"][0]["nested"] is nested
        table = render(client, "ws_sales_by_region")["table"]
        groups: dict[str, list[tuple[str, float]]] = {}
        for region, sub, value in zip(
            column(table, "f_region"),
            column(table, "f_sub_category"),
            column(table, "sum(f_sales)"),
            strict=True,
        ):
            groups.setdefault(region, []).append((sub, float(value)))
        return groups

    nested = by_region(True)
    assert list(nested) == ["Central", "East", "South", "West"]
    for rows in nested.values():
        values = [v for _, v in rows]
        assert values == sorted(values, reverse=True)  # 每個 Region 之內由大到小
    flat = by_region(False)
    orders = [[sub for sub, _ in rows] for rows in flat.values()]
    assert all(order == orders[0] for order in orders)  # 各 Region 的順序相同
    assert any(
        [v for _, v in rows] != sorted((v for _, v in rows), reverse=True) for rows in flat.values()
    )
    # 只有依欄位排序可以是巢狀
    ws["sorts"] = [
        {
            "field": "f_sub_category",
            "kind": "alphabetic",
            "order": "asc",
            "by": None,
            "nested": True,
            "manual_values": [],
        }
    ]
    refused = client.put("/api/edit/demo/worksheets/ws_sales_by_region", json={"worksheet": ws})
    assert refused.status_code == 422


def test_extract_settings(client: TestClient, tmp_path: Path) -> None:
    """萃取設定：即時改成萃取（立即從來源建立資料）、萃取篩選與立即更新、增量更新的欄位、改回即時。
    來源資料庫以 DuckDB 檔模擬。"""
    from tmig.data.sources import ConnectionConfig
    from tmig.ir import LiveConnection, TableRelation

    app: Any = client.app
    space = app.state.workspace
    base = "/api/edit/demo/datasources/ds_orders"
    # .twbx 的萃取沒有來源資料庫的資訊：不能切換
    info = client.get(f"{base}/extract").json()
    assert (info["mode"], info["switchable"]) == ("extract", False)
    refused = client.post(f"{base}/extract/mode", json={"mode": "live"})
    assert refused.status_code == 422 and "來源無法解析" in refused.json()["detail"]

    # 改成即時連線的報表：來源是萃取資料的一份複本
    def sales() -> list[Any]:
        values = column(render(client, "ws_sales_by_region")["table"], "sum(f_sales)")
        return [pytest.approx(float(v)) for v in values]

    before = sales()
    loaded = space.get("demo")
    source = tmp_path / "source.duckdb"
    shutil.copy(loaded.directory / "data" / "ds_orders.duckdb", source)
    connection = LiveConnection(
        kind="live",
        connection_id="sales_duckdb",
        dbclass="oracle",
        dialect="oracle",
        relation=TableRelation(kind="table", name="Orders", table=["Extract", "Extract"]),
    )
    live = loaded.ir.model_copy(
        update={
            "datasources": [
                ds.model_copy(update={"connection": connection}) for ds in loaded.ir.datasources
            ]
        }
    )
    space.save_ir("demo", live)
    info = client.get(f"{base}/extract").json()
    assert (info["mode"], info["switchable"], info["refreshed"]) == ("live", True, None)
    # 還沒有連線設定：建立萃取失敗，仍是即時連線
    failed = client.post(f"{base}/extract/mode", json={"mode": "extract"})
    assert failed.status_code == 503, failed.text
    assert client.get(f"{base}/extract").json()["mode"] == "live"
    config = ConnectionConfig("sales_duckdb", "duckdb", {"driver": "duckdb", "path": str(source)})
    app.state.runtime.add_configs({"sales_duckdb": config})
    assert sales() == before

    # 即時 → 萃取：立即從來源建立
    switched = client.post(f"{base}/extract/mode", json={"mode": "extract"})
    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert (body["mode"], body["refreshable"]) == ("extract", True)
    assert body["refreshed"] and body["result"]["tables"][0]["rows"] > 0
    assert sales() == before

    # 萃取篩選：下次更新時生效
    extract_filter = {
        "kind": "categorical",
        "id": "xf_region",
        "field": use("f_region"),
        "stage": "dimension",
        "mode": "include",
        "values": ["East"],
        "ranges": [],
        "group": None,
        "source_ref": "/editor",
    }
    saved = client.put(f"{base}/filters?stage=extract", json={"filters": [extract_filter]})
    assert saved.status_code == 200, saved.text
    assert [f["stage"] for f in saved.json()["filters"]] == ["extract"]
    assert client.get(f"{base}/filters?stage=extract").json()["filters"][0]["id"] == "xf_region"
    assert len(client.get(f"{base}/filters").json()["filters"]) == 1  # 資料源篩選不變
    ws = blank(client, rows=[use("f_region")], columns=[use("f_sales", agg="sum", continuous=True)])
    put(client, ws)
    assert len(column(render(client, "ws_sales_by_region")["table"], "f_region")) == 4
    refreshed = client.post(f"{base}/extract/refresh", json={"incremental": False})
    assert refreshed.status_code == 200, refreshed.text
    assert column(render(client, "ws_sales_by_region")["table"], "f_region") == ["East"]

    # 增量更新的欄位
    incremental = client.post(f"{base}/extract/refresh", json={"incremental": True})
    assert incremental.status_code == 422 and "增量更新" in incremental.json()["detail"]
    assert (
        client.put(f"{base}/extract", json={"increment": "f_order_date"}).json()["increment"]
        == "f_order_date"
    )
    incremental = client.post(f"{base}/extract/refresh", json={"incremental": True})
    assert incremental.status_code == 200, incremental.text
    assert incremental.json()["result"]["mode"] == "incremental"
    assert client.put(f"{base}/extract", json={"increment": "f_region"}).status_code == 422

    # 萃取 → 即時：直接查詢來源（萃取篩選不再套用）
    back = client.post(f"{base}/extract/mode", json={"mode": "live"})
    assert back.status_code == 200 and back.json()["mode"] == "live"
    assert len(column(render(client, "ws_sales_by_region")["table"], "f_region")) == 4


def test_set_controls(client: TestClient) -> None:
    """集合控制項：工作表上的控制項（顯示集合）與 dashboard 的集合區塊；集合所在維度的值可以列出，
    檢視器改變成員後 In／Out 跟著改變。"""
    base = "/api/edit/demo/datasources/ds_orders"
    made = client.post(
        f"{base}/sets",
        json={
            "levels": ["f_region"],
            "caption": "My Regions",
            "rule": {"kind": "members", "mode": "include", "members": [["East"]]},
        },
    )
    assert made.status_code == 200, made.text
    set_id = made.json()["field"]["id"]
    outline = client.get("/api/edit/demo/outline").json()
    assert {"id": set_id, "caption": "My Regions"} in outline["sets"]

    ws = blank(
        client,
        rows=[use("f_region"), use(set_id)],
        columns=[use("f_sales", agg="sum", continuous=True)],
    )
    ws["controls"] = [
        {
            "kind": "set",
            "field": use(set_id),
            "parameter": None,
            "control": "multi_list",
            "relevant": False,
        },
        {
            "kind": "set",
            "field": use("f_region"),
            "parameter": None,
            "control": "multi_list",
            "relevant": False,
        },
    ]
    saved = put(client, ws)
    assert [c["field"]["field"] for c in saved["controls"]] == [set_id]  # 不是集合的欄位移除

    def inside(state: dict[str, Any] | None = None) -> dict[str, str]:
        response = client.post(
            "/api/workbooks/demo/worksheets/ws_sales_by_region/render", json=state or {}
        )
        assert response.status_code == 200, response.text
        table = response.json()["table"]
        return dict(zip(column(table, "f_region"), column(table, set_id), strict=True))

    assert inside() == {"Central": "false", "East": "true", "South": "false", "West": "false"}
    # 集合所在維度的值可以列出（控制項的可選值）
    domain = client.post("/api/workbooks/demo/fields/f_region/domain")
    assert domain.status_code == 200 and domain.json()["values"] == [
        "Central",
        "East",
        "South",
        "West",
    ]
    # 檢視器改變成員：South、West
    changed = inside({"sets": {set_id: {"mode": "include", "members": [["South"], ["West"]]}}})
    assert changed == {"Central": "false", "East": "false", "South": "true", "West": "true"}

    # dashboard 的集合區塊
    board = client.get("/api/edit/demo/dashboards/db_ee909b0c").json()["dashboard"]
    zone = {
        "id": "z900",
        "kind": "set",
        "x": 10,
        "y": 10,
        "w": 200,
        "h": 160,
        "field": use(set_id),
        "children": [],
        "source_ref": "/editor",
    }
    board["zones"].append(zone)
    stored = client.put("/api/edit/demo/dashboards/db_ee909b0c", json={"dashboard": board})
    assert stored.status_code == 200, stored.text
    bad = {**zone, "id": "z901", "field": use("f_region")}
    board["zones"].append(bad)
    refused = client.put("/api/edit/demo/dashboards/db_ee909b0c", json={"dashboard": board})
    assert refused.status_code == 422 and "集合控制項" in refused.json()["detail"]


def test_secondary_table_calculation(client: TestClient) -> None:
    """次要計算：累計之後再做合計百分比、差異或排名；次要計算可以有自己的計算方向。"""
    sales = use("f_sales", agg="sum", continuous=True)

    def calc(secondary: dict[str, Any] | None, addressing: list[str]) -> dict[str, Any]:
        return {
            "id": "tc_run",
            "type": "running_total",
            "preset": "specific",
            "addressing": addressing,
            "instances": [],
            "level": None,
            "restart": None,
            "inferred": False,
            "options": {"aggregation": "sum"},
            "nested": [],
            "secondary": secondary,
            "source_ref": "/editor",
        }

    def values(ws: dict[str, Any]) -> tuple[dict[str, Any], str]:
        put(client, ws)
        response = render(client, "ws_sales_by_region")
        assert response["unsupported"] is None, response
        return response["table"], "sum(f_sales)#tc_run"

    # 一個維度：累計之後的合計百分比＝累計 ÷ 各累計值的總和
    ws = blank(client, rows=[use("f_region")], columns=[sales, {**sales, "table_calc": "tc_run"}])
    ws["table_calcs"] = [
        calc({"type": "percent_of_total", "addressing": [], "options": {}}, ["f_region"])
    ]
    table, key = values(ws)
    raw = [float(v) for v in column(table, "sum(f_sales)")]
    running = [sum(raw[: i + 1]) for i in range(len(raw))]
    got = [float(v) for v in column(table, key)]
    assert got == pytest.approx([r / sum(running) for r in running])
    assert table["rows"][0][table["keys"].index(key)].endswith("%")  # 以百分比顯示
    # 差異：與上一個累計值的差＝這一列的業績；第一列沒有比較的對象
    ws["table_calcs"] = [
        calc({"type": "difference", "addressing": [], "options": {}}, ["f_region"])
    ]
    table, key = values(ws)
    got_text = column(table, key)
    assert got_text[0] == "" and [float(v) for v in got_text[1:]] == pytest.approx(raw[1:])

    # 兩個維度：累計沿著 Category（每個 Region 重新開始），排名沿著 Region（每個 Category 各自排名）
    ws = blank(
        client,
        rows=[use("f_region"), use("f_category")],
        columns=[sales, {**sales, "table_calc": "tc_run"}],
    )
    ws["table_calcs"] = [
        calc(
            {"type": "rank", "addressing": ["f_region"], "options": {"direction": "desc"}},
            ["f_category"],
        )
    ]
    table, key = values(ws)
    cells: dict[tuple[str, str], float] = {}
    for region, category, value in zip(
        column(table, "f_region"),
        column(table, "f_category"),
        column(table, "sum(f_sales)"),
        strict=True,
    ):
        cells[region, category] = float(value)
    regions = list(dict.fromkeys(r for r, _ in cells))
    categories = list(dict.fromkeys(c for _, c in cells))
    cumulative = {
        (r, c): sum(cells[r, x] for x in categories[: i + 1])
        for r in regions
        for i, c in enumerate(categories)
    }
    expected = {
        (r, c): 1 + sum(cumulative[o, c] > cumulative[r, c] for o in regions) for r, c in cumulative
    }
    got_rank = {
        (r, c): int(float(v))
        for r, c, v in zip(
            column(table, "f_region"), column(table, "f_category"), column(table, key), strict=True
        )
    }
    assert got_rank == expected
    # 只有累計與移動計算可以有次要計算
    ws["table_calcs"] = [
        {**calc({"type": "rank", "addressing": [], "options": {}}, ["f_category"]), "type": "rank"}
    ]
    refused = client.put("/api/edit/demo/worksheets/ws_sales_by_region", json={"worksheet": ws})
    assert refused.status_code == 422


def test_ad_hoc_defaults_split_and_replace_references(client: TestClient) -> None:
    def use(field: str, agg: str | None = None, continuous: bool = False) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": None,
            "continuous": continuous,
            "table_calc": None,
        }

    def fields() -> dict[str, dict[str, Any]]:
        data = client.get("/api/edit/demo/catalog").json()
        return {f["id"]: f for f in data["datasources"][0]["fields"]}

    def put(ws: dict[str, Any]) -> dict[str, Any]:
        response = client.put(f"/api/edit/demo/worksheets/{ws['id']}", json={"worksheet": ws})
        assert response.status_code == 200, response.text
        return response.json()["worksheet"]

    base = "/api/edit/demo/datasources/ds_orders/fields"
    # 臨時計算：名稱是公式、資料窗格的資料帶著 ad_hoc；公式相同時回傳同一個欄位
    body = {"datasource": "ds_orders", "formula": "SUM([Sales]) / SUM([Quantity])", "ad_hoc": True}
    made = client.post("/api/edit/demo/calcs", json=body)
    assert made.status_code == 200, made.text
    adhoc = made.json()["field"]["id"]
    assert client.post("/api/edit/demo/calcs", json=body).json()["field"]["id"] == adhoc
    entry = fields()[adhoc]
    assert entry["ad_hoc"] is True and entry["caption"] == body["formula"]
    ws = sheet(client, "ws_sales_by_region")
    ws.update(rows=[use("f_region")], columns=[use(adhoc, continuous=True)], filters=[], sorts=[])
    ws.update(marks={"type": "bar"}, panes=[], table_calcs=[], reference_lines=[])
    put(ws)
    table = render(client, "ws_sales_by_region")["table"]
    # 每件金額：各區域的銷售額除以數量
    assert len(table["rows"]) == 4 and all(20 < float(r[1]) < 100 for r in table["rows"])
    bad = client.post(
        "/api/edit/demo/calcs", json={"datasource": "ds_orders", "formula": "SUM(", "ad_hoc": True}
    )
    assert bad.status_code == 422 and "公式有誤" in bad.text

    # 預設屬性：排序套用到用到這個欄位、沒有另外排序的工作表
    response = client.put(
        f"{base}/f_region/properties",
        json={"default_sort": {"kind": "alphabetic", "order": "desc"}},
    )
    assert response.status_code == 200, response.text
    ws = sheet(client, "ws_sales_by_region")
    assert [(s["field"], s["order"]) for s in ws["sorts"]] == [("f_region", "desc")]
    members = [r[0] for r in render(client, "ws_sales_by_region")["table"]["rows"]]
    assert members == sorted(members, reverse=True)
    response = client.put(f"{base}/f_region/properties", json={"default_sort": None})
    assert sheet(client, "ws_sales_by_region")["sorts"] == []
    assert client.put(f"{base}/f_sales/properties", json={"total_using": "avg"}).status_code == 200
    assert fields()["f_sales"]["total_using"] == "avg"
    wrong = client.put(f"{base}/f_sales/properties", json={"default_shapes": []})
    assert wrong.status_code == 422 and "只有維度" in wrong.text

    # 分割：建立「<名稱> - Split N」的計算欄位
    split = client.post(f"{base}/f_order_id/split", json={"separator": "-", "count": 2})
    assert split.status_code == 200, split.text
    parts = split.json()["fields"]
    assert [p["caption"] for p in parts] == ["Order ID - Split 1", "Order ID - Split 2"]
    ws = sheet(client, "ws_sales_by_region")
    ws.update(rows=[use(parts[0]["id"])], columns=[use("f_sales", "sum", True)])
    put(ws)
    prefixes = {r[0] for r in render(client, "ws_sales_by_region")["table"]["rows"]}
    assert prefixes and all(p and "-" not in p for p in prefixes)

    # 取代參照：工作表上的欄位換成另一個
    replaced = client.post(f"{base}/{parts[0]['id']}/replace", json={"replacement": "f_segment"})
    assert replaced.status_code == 200, replaced.text
    assert sheet(client, "ws_sales_by_region")["rows"][0]["field"] == "f_segment"
    same = client.post(f"{base}/f_segment/replace", json={"replacement": "f_sales"})
    assert same.status_code == 422 and "不能互相取代" in same.text

    # 變更資料類型：萃取的欄位在查詢時轉換，欄位的 id 不變；改回原本的型別時還原
    target = "f_row_id"
    changed = client.put(f"{base}/{target}", json={"datatype": "string"})
    assert changed.status_code == 200, changed.text
    entry = fields()[target]
    assert (entry["datatype"], entry["kind"]) == ("string", "calc") and entry["formula"].startswith(
        "STR("
    )
    ws = sheet(client, "ws_sales_by_region")
    ws.update(rows=[use(target)], columns=[use("f_sales", "sum", True)])
    put(ws)
    assert all(isinstance(r[0], str) for r in render(client, "ws_sales_by_region")["table"]["rows"])
    ws.update(rows=[use("f_region")])
    put(ws)
    assert client.put(f"{base}/{target}", json={"datatype": "integer"}).status_code == 200
    entry = fields()[target]
    assert (entry["datatype"], entry["kind"]) == ("integer", "column")


def test_filter_tabs_control_options_missing_values_and_table_layout(client: TestClient) -> None:
    def use(
        field: str, agg: str | None = None, continuous: bool = False, date: Any = None
    ) -> dict[str, Any]:
        return {
            "field": field,
            "agg": agg,
            "date": date,
            "continuous": continuous,
            "table_calc": None,
        }

    def put(ws: dict[str, Any]) -> dict[str, Any]:
        response = client.put(f"/api/edit/demo/worksheets/{ws['id']}", json={"worksheet": ws})
        assert response.status_code == 200, response.text
        return response.json()["worksheet"]

    def flt(
        filter_id: str, kind: str, field: dict[str, Any], stage: str, **more: Any
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "id": filter_id,
            "field": field,
            "stage": stage,
            "source_ref": "/editor",
            **more,
        }

    name = "ws_sales_by_region"
    sales = use("f_sales", "sum", True)
    base = sheet(client, name)
    base.update(rows=[use("f_region")], columns=[sales], filters=[], sorts=[], panes=[])
    base.update(marks={"type": "bar"}, table_calcs=[], reference_lines=[])
    put(base)
    totals = {r[0]: float(r[1].replace(",", "")) for r in render(client, name)["table"]["rows"]}
    assert len(totals) == 4
    ranked = sorted(totals, key=lambda region: totals[region])
    threshold = (totals[ranked[1]] + totals[ranked[2]]) / 2

    # 「至少」：只有下限的量值篩選；兩個地區被濾掉
    ws = sheet(client, name)
    ws["filters"] = [flt("flt_least", "range", sales, "measure", min=threshold, max=None)]
    put(ws)
    assert {r[0] for r in render(client, name)["table"]["rows"]} == set(ranked[2:])
    # 顯示空的列：被量值篩選濾掉的地區仍顯示標題，儲存格是特殊值的文字
    ws = sheet(client, name)
    ws.update(show_empty=["rows"], null_text="—")
    put(ws)
    rendered = render(client, name)
    rows = rendered["table"]["rows"]
    assert [r[0] for r in rows] == sorted(totals)  # 依值排序：補在依值應該在的位置
    assert {r[0] for r in rows if r[1] == "—"} == set(ranked[:2])
    records = rendered["spec"]["data"]["values"]
    assert sum(1 for record in records if record["c1"] is None) == 2

    # 「特殊值＞Null 值」：只保留 Null 的列（計算欄位在 East 是 Null）
    made = client.post(
        "/api/edit/demo/calcs",
        json={
            "datasource": "ds_orders",
            "caption": "Maybe Sales",
            "formula": 'IIF([Region] = "East", NULL, [Sales])',
        },
    )
    assert made.status_code == 200, made.text
    maybe = made.json()["field"]["id"]
    ws = sheet(client, name)
    ws.update(show_empty=[], null_text=None)
    ws["filters"] = [
        flt("flt_null", "range", use(maybe, continuous=True), "dimension", only_null=True)
    ]
    saved = put(ws)
    assert saved["filters"][0]["only_null"] is True
    assert [r[0] for r in render(client, name)["table"]["rows"]] == ["East"]
    wrong = dict(ws, filters=[dict(ws["filters"][0], min=1)])
    response = client.put(f"/api/edit/demo/worksheets/{name}", json={"worksheet": wrong})
    assert response.status_code == 422 and "不能有範圍" in response.text

    # 篩選控制項的自訂與「內容中的所有值」：可選值只套用內容篩選
    ws = sheet(client, name)
    ws["filters"] = [
        flt(
            "flt_cat",
            "categorical",
            use("f_category"),
            "context",
            mode="include",
            values=["Furniture"],
        ),
        flt("flt_sub", "categorical", use("f_sub_category"), "dimension", mode="all", values=[]),
        flt(
            "flt_region",
            "categorical",
            use("f_region"),
            "dimension",
            mode="include",
            values=["East"],
        ),
    ]
    options = {"context_values": True, "show_apply": True, "show_all": False}
    ws["controls"] = [
        {
            "kind": "filter",
            "field": use("f_sub_category"),
            "parameter": None,
            "control": "multi_list",
            "relevant": False,
            "options": options,
        }
    ]
    saved = put(ws)
    assert saved["controls"][0]["options"] == {
        "context_values": True,
        "show_all": False,
        "show_search": True,
        "show_exclude": False,
        "show_apply": True,
    }
    domain = "/api/workbooks/demo/fields/f_sub_category/domain"
    everything = client.post(domain, json={}).json()["values"]
    scoped = client.post(domain, json={"worksheet": name, "scope": "context"})
    assert scoped.status_code == 200, scoped.text
    furniture = scoped.json()["values"]
    assert len(everything) == 17 and len(furniture) == 4 and "Chairs" in furniture

    # 顯示遺漏的值：連續的日（截斷）在最小到最大之間每天一列，沒有資料的日子量值是空的
    day = use("f_order_date", continuous=True, date={"kind": "trunc", "unit": "day"})
    ws = sheet(client, name)
    ws.update(rows=[sales], columns=[day], filters=[], controls=[], marks={"type": "line"})
    put(ws)
    plain = render(client, name)["table"]["raw"]
    ws = sheet(client, name)
    ws.update(show_missing=[day], tooltip_excluded=[day])
    put(ws)
    rendered = render(client, name)
    padded = rendered["table"]["raw"]
    assert len(padded) > len(plain)
    from datetime import date

    first, last = date.fromisoformat(padded[0][0][:10]), date.fromisoformat(padded[-1][0][:10])
    assert len(padded) == (last - first).days + 1
    assert sum(1 for row in padded if row[1] in (None, "")) == len(padded) - len(plain)
    # 包含在工具提示中：取消之後工具提示不列出日期
    spec = rendered["spec"]
    tooltip = (
        spec["encoding"]["tooltip"]
        if "encoding" in spec
        else spec["layer"][0]["encoding"]["tooltip"]
    )
    assert [t["title"] for t in tooltip] == ["Sales"]
    # 膠囊移走之後（工作表換成別的欄位）設定仍要對得上檢視中的欄位：伺服器照存，檢視器忽略不在架上的
    assert sheet(client, name)["show_missing"] == [day]


def test_hidden_sheets_tab_color_and_duplicate_as_crosstab(client: TestClient) -> None:
    name = "ws_sales_by_region"
    listed = client.get("/api/workbooks").json()
    demo = next(w for w in listed if w["id"] == "demo")
    assert all(w["hidden"] is False for w in demo["worksheets"])
    ws = sheet(client, name)
    ws.update(hidden=True, tab_color="#ff8800")
    response = client.put(f"/api/edit/demo/worksheets/{name}", json={"worksheet": ws})
    assert response.status_code == 200, response.text
    saved = response.json()["worksheet"]
    assert (saved["hidden"], saved["tab_color"]) == (True, "#ff8800")
    demo = next(w for w in client.get("/api/workbooks").json() if w["id"] == "demo")
    assert [w["id"] for w in demo["worksheets"] if w["hidden"]] == [name]
    # 隱藏的工作表照樣可以查詢（dashboard 上仍在使用）
    assert render(client, name)["table"]["rows"]

    # 複製成交叉資料表：維度留在架上，量值以 Measure Names／Values 並列成文字表；不沿用隱藏與顏色
    original = render(client, name)
    made = client.post(f"/api/edit/demo/worksheets/{name}/crosstab")
    assert made.status_code == 200, made.text
    copy = made.json()["worksheet"]
    assert copy["name"] == "Sales by Region (交叉資料表)"
    assert (copy["hidden"], copy["tab_color"], copy["marks"]["type"]) == (False, None, "text")
    assert copy["columns"][-1]["field"] == "measure_names"
    assert [u["field"] for u in copy["marks"]["label"]] == ["measure_values"]
    assert len(copy["measure_values"]) >= 2 and copy["panes"] == []
    table = render(client, copy["id"])
    assert table["unsupported"] is None and table["mark"] == "text"
    # 詳細程度不變：資料表的列（各維度的組合）與原檢視相同，量值各一欄
    keys = table["table"]["keys"]
    assert keys[:2] == ["f_region", "f_category"] and len(keys) == 2 + len(copy["measure_values"])
    assert len(table["table"]["rows"]) == len(original["table"]["rows"])
    again = client.post(f"/api/edit/demo/worksheets/{name}/crosstab").json()["worksheet"]
    assert again["name"] == "Sales by Region (交叉資料表) (2)"
    assert client.post("/api/edit/demo/worksheets/nope/crosstab").status_code == 422
