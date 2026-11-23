"""檢視器的端到端：故事點記下的欄列架上加上的欄位、Top N 篩選的變更、量值的範圍篩選與移除的篩選（第三批）
在故事點上畫出，切換到沒有記下狀態的故事點時回到工作表本身。

示範報表的「Sales by Region」：Top 3（參數）的地區、只有 Furniture 與 Technology。故事點：Top 2、移除類別的
篩選、列架上加上 Category、只留 SUM(Sales) 大於 50,000 的標記。
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

import pytest
import uvicorn

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("TMIG_WEB_DIST") or ROOT / "web" / "dist")
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "story-deltas-e2e-token-0123456789"
REGIONS = ("Central", "East", "South", "West")
CATEGORIES = ("Furniture", "Office Supplies", "Technology")


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


def test_story_point_shows_added_field_and_changed_filters(engine: str) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    story = call(engine, "/api/edit/demo/stories", {"name": "Tour"})["story"]
    top_two = {
        "kind": "top_n",
        "id": "flt_story_top",
        "field": use("f_region"),
        "stage": "top_n",
        "direction": "top",
        "count": 2,
        "by": "SUM([Sales])",
        "source_ref": "story",
    }
    big = {
        "kind": "range",
        "id": "flt_story_big",
        "field": use("f_sales", agg="sum", continuous=True),
        "stage": "measure",
        "min": 50000,
        "source_ref": "story",
    }
    story["points"] = [
        {
            "id": "sp1",
            "caption": "前兩名",
            "worksheet": "ws_sales_by_region",
            "sheets": [
                {
                    "worksheet": "ws_sales_by_region",
                    "added_rows": [{"use": use("f_category"), "index": 1}],
                    "filters": [top_two, big],
                    "removed_filters": ["flt_category"],
                }
            ],
        },
        {"id": "sp2", "caption": "原本", "worksheet": "ws_sales_by_region"},
    ]
    call(engine, f"/api/edit/demo/stories/{story['id']}", {"story": story}, "PUT")

    def labels(page: Page) -> set[str]:
        """列的標題（座標軸的標籤）中的地區與類別（圖例另外有類別的名稱）。"""
        page.wait_for_selector('.story-point .worksheet[data-loading="false"]', timeout=30000)
        page.wait_for_timeout(300)
        found = page.evaluate(
            """() => [...document.querySelectorAll('.story-point svg text')]
                 .filter((t) => !t.closest('.role-legend'))
                 .map((t) => t.textContent ?? '')"""
        )
        return {t for t in found if t in (*REGIONS, *CATEGORIES)}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/story/{story['id']}")
        expect(page.locator('.story-point[data-point="sp1"]')).to_be_visible(timeout=30000)
        first = labels(page)
        # Top 2 的地區（West、East），三個類別都在（移除了類別的篩選），Category 加在列架上
        assert first & set(REGIONS) == {"East", "West"}, first
        assert first & set(CATEGORIES) == set(CATEGORIES), first

        page.get_by_role("button", name="原本").click()
        expect(page.locator('.story-point[data-point="sp2"]')).to_be_visible()
        second = labels(page)
        # 工作表本身：Top 3 的地區，列架上只有 Region
        assert len(second & set(REGIONS)) == 3 and not second & set(CATEGORIES), second
        assert errors == []
        browser.close()
