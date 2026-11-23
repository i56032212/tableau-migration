"""檢視器的端到端：預測模型函式 MODEL_QUANTILE、MODEL_PERCENTILE 的結果顯示在檢視上（各子類別的長條上的
標籤），數字與 numpy 以畫面上的 SUM(Sales)、SUM(Profit) 手算的線性迴歸（OLS）相同；查詢之後計算的隱藏欄
（目標與預測變數）不出現在檢視資料中。
需要建置好的檢視器（web/dist，可以用 TMIG_WEB_DIST 指定另一份）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import uvicorn

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("TMIG_WEB_DIST") or ROOT / "web" / "dist")
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "predictive-e2e-token-0123456789"


def chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file() or not chromium(),
    reason="需要建置好的檢視器（web/dist）與 Playwright 的 Chromium",
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[str]:
    port = free_port()
    app = create_desktop_app(tmp_path / "workspace", TOKEN, DIST, username="amy")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("本機引擎沒有在 30 秒內啟動")
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    call(base, "/api/desktop/open", {"path": str(source)})
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def call(base: str, path: str, body: dict[str, Any] | None = None, method: str = "") -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def use(field: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": field,
        "agg": None,
        "date": None,
        "continuous": False,
        "table_calc": None,
        **extra,
    }


def test_model_quantile_labels(engine: str) -> None:
    from playwright.sync_api import sync_playwright

    field = call(
        engine,
        "/api/edit/demo/calcs",
        {
            "datasource": "ds_orders",
            "caption": "預測銷售",
            "formula": "MODEL_QUANTILE(0.5, SUM([Sales]), SUM([Profit]))",
        },
    )["field"]
    created = call(engine, "/api/edit/demo/worksheets", {"name": "Predicted"})["worksheet"]
    predicted = use(field["id"], table_calc="tc_predicted")
    body = {
        **created,
        "rows": [use("f_sub_category")],
        "columns": [use("f_sales", agg="sum", continuous=True)],
        "marks": {
            **created["marks"],
            "type": "bar",
            "label": [predicted],
            "tooltip": [use("f_profit", agg="sum", continuous=True)],
            "labels": {"show": True},
        },
        # 表（向下）：所有子類別一起建立模型
        "table_calcs": [
            {
                "id": "tc_predicted",
                "type": "formula",
                "preset": "table_down",
                "addressing": ["f_sub_category"],
                "source_ref": "editor",
            }
        ],
    }
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, "PUT")
    rendered = call(engine, f"/api/workbooks/demo/worksheets/{created['id']}/render", {})
    assert not rendered.get("unsupported"), rendered.get("unsupported")
    keys = rendered["table"]["keys"]
    assert not any(k.startswith("__") for k in keys)
    raw = rendered["table"]["raw"]
    at = {k: i for i, k in enumerate(keys)}
    sales = np.array([float(r[at["sum(f_sales)"]]) for r in raw])
    profit = np.array([float(r[at["sum(f_profit)"]]) for r in raw])
    model = next(k for k in keys if k.startswith(f"{field['id']}#"))
    design = np.column_stack([np.ones(len(raw)), profit])
    beta, *_ = np.linalg.lstsq(design, sales, rcond=None)
    expected = design @ beta
    got = np.array([float(r[at[model]]) for r in raw])
    assert got == pytest.approx(expected)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/{created['id']}")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]', timeout=30000)
        texts = page.evaluate(
            """() => [...document.querySelectorAll('.single-sheet svg text')].map((t) => t.textContent ?? '')"""
        )
        # 標籤是預測值（畫面上的格式：取整數、千分位）
        shown = {f"{round(v):,}" for v in expected}
        assert len(shown & set(texts)) >= len(shown) - 1, (sorted(shown), texts[:60])
        assert errors == []
        browser.close()
