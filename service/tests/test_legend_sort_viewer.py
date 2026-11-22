"""檢視器的端到端：色彩欄位依欄位的值排序（Region 依 SUM(Sales) 遞減）時，工作表上的圖例與 dashboard 的色彩
圖例區塊都依這個順序列出成員，各成員的顏色不變。需要建置好的檢視器（web/dist，可以用 TMIG_WEB_DIST 指定
另一份）與 Playwright 的 Chromium；缺少時略過。"""

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

import pytest
import uvicorn

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("TMIG_WEB_DIST") or ROOT / "web" / "dist")
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "legend-sort-e2e-token-0123456789"


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


SALES = use("f_sales", agg="sum", continuous=True)


def sheet(base: str, name: str, **fields: Any) -> str:
    created = call(base, "/api/edit/demo/worksheets", {"name": name})["worksheet"]
    body = {**created, **fields}
    if "marks" in fields:
        body["marks"] = {**created["marks"], **fields["marks"]}
    call(base, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, "PUT")
    return created["id"]


def test_legends_follow_the_sort_by_field(engine: str) -> None:
    from playwright.sync_api import expect, sync_playwright

    # 各區的銷售額（決定預期的順序）
    totals = sheet(engine, "Totals", rows=[use("f_region")], columns=[SALES])
    plain = call(engine, f"/api/workbooks/demo/worksheets/{totals}/render", {})["spec"]
    region, sales = plain["encoding"]["y"]["field"], plain["encoding"]["x"]["field"]
    by_sales = {row[region]: row[sales] for row in plain["data"]["values"]}
    expected = sorted(by_sales, key=lambda r: by_sales[r], reverse=True)
    assert expected != sorted(expected)  # 與成員的順序不同，才看得出圖例有沒有跟著排序

    stacked = sheet(
        engine,
        "Stacked",
        rows=[use("f_category")],
        columns=[SALES],
        marks={"type": "bar", "color": use("f_region")},
        sorts=[{"field": "f_region", "kind": "by_field", "order": "desc", "by": SALES}],
    )
    spec = call(engine, f"/api/workbooks/demo/worksheets/{stacked}/render", {})["spec"]
    scale = spec["encoding"]["color"]["scale"]
    assert scale["domain"] == expected
    board = call(engine, "/api/edit/demo/dashboards", {"name": "Legend Board"})["dashboard"]
    board["zones"][0]["children"][0]["children"] = [
        {"id": "z3", "kind": "worksheet", "x": 4, "y": 4, "w": 700, "h": 600, "worksheet": stacked,
         "source_ref": "/editor"},
        {"id": "z4", "kind": "legend", "x": 704, "y": 4, "w": 292, "h": 600, "worksheet": stacked,
         "legend": "color", "source_ref": "/editor"},
    ]  # fmt: skip
    call(engine, f"/api/edit/demo/dashboards/{board['id']}", {"dashboard": board}, "PUT")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        # 工作表上的圖例（Vega 的圖例）
        page.goto(f"{engine}/w/demo/s/{stacked}")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]', timeout=30000)
        labels = page.evaluate(
            """() => [...document.querySelectorAll('.single-sheet svg .role-legend-label text')]
                 .map((t) => t.textContent)"""
        )
        # 凍結的標題、列印用的複本等另外畫的圖例：只看第一次出現的順序
        assert list(dict.fromkeys(labels)) == expected
        # dashboard 的色彩圖例區塊
        page.goto(f"{engine}/w/demo/d/{board['id']}")
        legend = page.locator('.dashboard [data-zone="z4"] .legend-item')
        expect(legend).to_have_text(expected, timeout=30000)
        swatches = page.evaluate(
            """() => [...document.querySelectorAll('.dashboard [data-zone="z4"] .legend-item .swatch')]
                 .map((s) => getComputedStyle(s).backgroundColor)"""
        )
        colors = dict(zip(scale["domain"], scale["range"], strict=True))
        wanted = [
            "rgb({}, {}, {})".format(*(int(colors[r][i : i + 2], 16) for i in (1, 3, 5)))
            for r in expected
        ]
        assert swatches == wanted
        assert errors == []
        browser.close()
