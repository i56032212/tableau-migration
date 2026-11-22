"""檢視器的端到端：故事點記下的工作表狀態（排序、座標軸的標題、註解）在故事點上畫出，切換到沒有記下狀態的
故事點時回到工作表本身；記下的頁面架目前的頁也照樣顯示。需要建置好的檢視器（web/dist，可以用
TMIG_WEB_DIST 指定另一份）與 Playwright 的 Chromium；缺少時略過。"""

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
TOKEN = "story-states-e2e-token-0123456789"


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


PROFIT = {"field": "f_profit", "agg": "sum", "date": None, "continuous": True, "table_calc": None}


def test_story_point_shows_its_sorts_axis_title_and_annotation(engine: str) -> None:
    """Sub-Category Profit 的兩個故事點：第一個記下 Sub-Category 遞減的字母排序、SUM(Profit) 座標軸的標題與
    一個註解（從 Tableau 開啟的故事點的 StoryPoint.sheets），第二個沒有記下狀態。"""
    from playwright.sync_api import Page, expect, sync_playwright

    story = call(engine, "/api/edit/demo/stories", {"name": "Tour"})["story"]
    sheet_state = {
        "worksheet": "ws_sub_category_profit",
        "sorts": [{"field": "f_sub_category", "kind": "alphabetic", "order": "desc"}],
        "axes": [{"use": PROFIT, "shelf": "columns", "title": "利潤（故事點）"}],
        "annotations": [
            {
                "kind": "point",
                "text": [{"text": "故事點的註解"}],
                "point": {"x": 0, "x_axis": PROFIT},
            }
        ],
    }
    story["points"] = [
        {
            "id": "sp1",
            "caption": "遞減",
            "worksheet": "ws_sub_category_profit",
            "sheets": [sheet_state],
        },
        {"id": "sp2", "caption": "原本", "worksheet": "ws_sub_category_profit"},
    ]
    call(engine, f"/api/edit/demo/stories/{story['id']}", {"story": story}, "PUT")

    def labels(page: Page) -> list[str]:
        page.wait_for_selector('.story-point .worksheet[data-loading="false"]', timeout=30000)
        found = page.evaluate(
            """() => [...document.querySelectorAll('.story-point svg .role-axis-label text')]
                 .map((t) => t.textContent ?? '')"""
        )
        # 凍結的列標題另外畫一份：只留第一次出現的
        return list(dict.fromkeys(found))

    def chart_text(page: Page) -> str:
        return page.locator(".story-point .worksheet svg").first.inner_html()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/story/{story['id']}")
        expect(page.locator('.story-point[data-point="sp1"]')).to_be_visible(timeout=30000)
        first = labels(page)
        names = [
            t for t in first if t and not t.replace(",", "").replace(".", "").lstrip("-").isdigit()
        ]
        subcats = [
            t for t in names if t in ("Accessories", "Appliances", "Art", "Binders", "Tables")
        ]
        assert subcats == sorted(subcats, reverse=True) and len(subcats) >= 3, first
        assert "利潤（故事點）" in chart_text(page)
        assert "故事點的註解" in chart_text(page)

        page.get_by_role("button", name="原本").click()
        expect(page.locator('.story-point[data-point="sp2"]')).to_be_visible()
        page.wait_for_timeout(500)
        second = labels(page)
        subcats = [
            t for t in second if t in ("Accessories", "Appliances", "Art", "Binders", "Tables")
        ]
        assert subcats != sorted(subcats, reverse=True), second
        assert "利潤（故事點）" not in chart_text(page)
        assert "故事點的註解" not in chart_text(page)
        assert errors == []
        browser.close()
