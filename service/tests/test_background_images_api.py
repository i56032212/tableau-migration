"""背景圖片的編輯 API（PUT /api/edit/<報表>/datasources/<資料源>/background-images）：資料源的圖片整份取代，
新增的配發 id；工作表可以停用；檢視的規格畫出圖片的圖層；存成報表套件時帶著圖片；不正確的不接受。"""

from __future__ import annotations

import base64
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
TOKEN = "background-token-1234567890"
LOCAL = "http://127.0.0.1:8123"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


def use(field: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": field,
        "agg": None,
        "date": None,
        "continuous": False,
        "table_calc": None,
        **extra,
    }


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, ROOT / "no-web-dist", username="amy")
    client = TestClient(app, base_url=LOCAL, headers={TOKEN_HEADER: TOKEN})
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    assert client.post("/api/desktop/open", json={"path": str(source)}).status_code == 200
    return client


def scatter(client: TestClient) -> dict[str, Any]:
    """各區的 SUM(Sales)（欄）與 SUM(Profit)（列）。"""
    created = client.post("/api/edit/demo/worksheets", json={"name": "Floor"}).json()["worksheet"]
    body = {
        **created,
        "rows": [use("f_profit", agg="sum", continuous=True)],
        "columns": [use("f_sales", agg="sum", continuous=True)],
        "marks": {**created["marks"], "type": "circle", "detail": [use("f_region")]},
    }
    saved = client.put(f"/api/edit/demo/worksheets/{created['id']}", json={"worksheet": body})
    assert saved.status_code == 200, saved.text
    return client.get(f"/api/edit/demo/worksheets/{created['id']}").json()["worksheet"]


def image(**extra: Any) -> dict[str, Any]:
    return {
        "name": "Floor plan",
        "image": "Image/floor.png",
        "x": "f_sales",
        "left": 0,
        "right": 1000,
        "y": "f_profit",
        "top": 500,
        "bottom": -100,
        "washout": 0.3,
        "lock_aspect": True,
        "show_entire": False,
        "conditions": [],
        **extra,
    }


def layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        layer
        for layer in spec.get("layer", [])
        if str(layer.get("name", "")).startswith("__deco__bg")
    ]


def test_background_images_round_trip(client: TestClient, tmp_path: Path) -> None:
    sheet = scatter(client)
    content = base64.b64encode(PNG).decode()
    uploaded = client.post("/api/edit/demo/images", json={"name": "floor.png", "content": content})
    assert uploaded.json() == {"path": "Image/floor.png"}
    ds_id = client.get("/api/edit/demo/catalog").json()["datasources"][0]["id"]
    url = f"/api/edit/demo/datasources/{ds_id}/background-images"
    saved = client.put(url, json={"images": [image()]})
    assert saved.status_code == 200, saved.text
    (stored,) = saved.json()["images"]
    assert stored["id"] == "bg_floor_plan" and stored["washout"] == 0.3
    catalog = client.get("/api/edit/demo/catalog").json()["datasources"][0]
    assert [i["name"] for i in catalog["background_images"]] == ["Floor plan"]

    spec = client.post(f"/api/workbooks/demo/worksheets/{sheet['id']}/render").json()["spec"]
    (layer,) = layers(spec)
    assert layer["data"]["values"][0]["__bg_image0"] == "Image/floor.png"
    assert layer["mark"]["opacity"] == 0.7
    assert client.get("/api/workbooks/demo/files/Image/floor.png").content == PNG

    # 這張工作表停用：不畫
    off = {**sheet, "background_images_off": [stored["id"]]}
    put = client.put(f"/api/edit/demo/worksheets/{sheet['id']}", json={"worksheet": off})
    assert put.status_code == 200, put.text
    spec = client.post(f"/api/workbooks/demo/worksheets/{sheet['id']}/render").json()["spec"]
    assert layers(spec) == []

    # 存成報表套件時帶著圖片
    target = tmp_path / "with-background.tmigx"
    package = client.post("/api/desktop/save", json={"workbook": "demo", "path": str(target)})
    assert package.status_code == 200, package.text
    assert package.json()["files"] == ["Image/floor.png"]

    # 刪除圖片：工作表停用的清單一併拿掉
    assert client.put(url, json={"images": []}).status_code == 200
    again = client.get(f"/api/edit/demo/worksheets/{sheet['id']}").json()["worksheet"]
    assert again["background_images_off"] == []


def test_invalid_background_images_are_refused(client: TestClient) -> None:
    ds_id = client.get("/api/edit/demo/catalog").json()["datasources"][0]["id"]
    url = f"/api/edit/demo/datasources/{ds_id}/background-images"
    for bad, message in [
        (image(x="f_region"), "X 欄位必須是數值欄位"),
        (image(image="../floor.png"), "圖片檔"),
        (image(image="Image/floor.exe"), "圖片檔"),
        (image(left=5, right=5), "左右（上下）的值不能相同"),
        (image(conditions=[{"field": use("f_sales"), "value": 1}]), "條件必須是資料源中的維度"),
        (image(washout=2), "不正確"),
    ]:
        refused = client.put(url, json={"images": [bad]})
        assert refused.status_code == 422, bad
        assert message in refused.json()["detail"], refused.json()["detail"]
