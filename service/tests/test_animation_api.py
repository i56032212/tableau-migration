"""動畫（格式＞動畫）的編輯 API：活頁簿預設的讀寫、工作表上的設定（工作表的 PUT）、「全部重設」，以及沒有設定時
不寫進報表（golden IR 與舊的報表檔不因新欄位而改變）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tmig.ir import load_ir
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "animation-api-token-0123456789abcdef"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, None, username="amy")
    client = TestClient(app, headers={TOKEN_HEADER: TOKEN}, base_url="http://127.0.0.1")
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    ok(client.post("/api/desktop/open", json={"path": str(source)}))
    return client


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def test_workbook_default_and_sheet_settings(client: TestClient) -> None:
    """活頁簿預設：沒有設定時是 null；存進去之後讀得回來。工作表的設定隨工作表的 PUT 存下；「全部重設」拿掉各
    工作表的設定，活頁簿預設回到 null。持續時間超過 10 秒、不認得的樣式回應 422。"""
    assert ok(client.get("/api/edit/demo/animation")) == {"animation": None}
    saved = ok(
        client.put(
            "/api/edit/demo/animation",
            json={"animation": {"on": True, "duration": 0.5, "style": "sequential"}},
        )
    )
    assert saved["animation"] == {"on": True, "duration": 0.5, "style": "sequential"}
    assert ok(client.get("/api/edit/demo/animation"))["animation"]["style"] == "sequential"

    sheet = ok(client.post("/api/edit/demo/worksheets", json={"name": "Animated"}))["worksheet"]
    assert "animation" not in sheet  # 沒有設定時不寫
    sheet["animation"] = {"on": False}
    stored = ok(client.put(f"/api/edit/demo/worksheets/{sheet['id']}", json={"worksheet": sheet}))
    assert stored["worksheet"]["animation"] == {"on": False, "duration": None, "style": None}

    assert (
        client.put("/api/edit/demo/animation", json={"animation": {"duration": 11}}).status_code
        == 422
    )
    assert (
        client.put("/api/edit/demo/animation", json={"animation": {"style": "bounce"}}).status_code
        == 422
    )

    ok(client.put("/api/edit/demo/animation", json={"animation": None, "reset_sheets": True}))
    assert ok(client.get("/api/edit/demo/animation")) == {"animation": None}
    again = ok(client.get(f"/api/edit/demo/worksheets/{sheet['id']}"))["worksheet"]
    assert "animation" not in again


def test_unset_animation_is_not_written(tmp_path: Path) -> None:
    """沒有設定動畫的報表寫出的 IR 裡沒有 animation（golden IR 不變）；設定了的寫進去、讀得回來。"""
    from tmig.ir import dumps_ir
    from tmig.ir.models import Animation

    ir = load_ir(ROOT / "fixtures" / "superstore_demo" / "golden.ir.json")
    text = dumps_ir(ir)
    assert '"animation"' not in text
    assert text == (ROOT / "fixtures" / "superstore_demo" / "golden.ir.json").read_text(
        encoding="utf-8"
    )
    book = ir.workbook.model_copy(update={"animation": Animation(on=True, duration=1.0)})
    changed = ir.model_copy(update={"workbook": book})
    path = tmp_path / "ir.json"
    path.write_text(dumps_ir(changed), encoding="utf-8", newline="\n")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["workbook"]["animation"] == {"on": True, "duration": 1.0, "style": None}
    assert load_ir(path).workbook.animation == Animation(on=True, duration=1.0)
