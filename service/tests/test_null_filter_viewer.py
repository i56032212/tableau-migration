"""「N 個 Null」指示器的選單（Tableau 檢視右下角的特殊值指示器）的端到端：與 Tableau 相同有「篩選資料」——
加入只留非 Null 值的篩選（篩選對話框「特殊值」的「非 Null 值」），Null 的標記與指示器都不見了，可以復原；
「在預設位置顯示資料」與「隱藏指示器」照舊，右鍵也開得出選單。
需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
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
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "null-filter-e2e-token-0123456789"


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


def call(
    base: str, path: str, body: dict[str, Any] | None = None, method: str | None = None
) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


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


def use(field: str, **extra: Any) -> dict[str, Any]:
    base = {"field": field, "agg": None, "date": None, "continuous": False, "table_calc": None}
    return {**base, **extra}


def test_null_indicator_filters_the_nulls(engine: str) -> None:
    """各區的「大區的業績」（業績不到 50 萬的區是 Null）畫在連續的座標軸上：指示器的選單依序是「篩選資料」
    「在預設位置顯示資料」「隱藏指示器」。「篩選資料」把這個量值加到篩選架、只留非 Null 值：查詢結果少了 Null
    的那幾列，指示器不見；復原（Ctrl+Z）拿掉篩選、指示器回來。右鍵點指示器也開出同一個選單。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        page.wait_for_timeout(300)

    made = call(
        engine,
        "/api/edit/demo/calcs",
        {
            "datasource": "ds_orders",
            "caption": "大區的業績",
            "formula": "IF SUM([Sales]) > 500000 THEN SUM([Sales]) END",
        },
    )["field"]
    created = call(engine, "/api/edit/demo/worksheets", {"name": "Nulls"})["worksheet"]
    sheet_id = created["id"]
    call(
        engine,
        f"/api/edit/demo/worksheets/{sheet_id}",
        {
            "worksheet": {
                **created,
                "rows": [use("f_region")],
                "columns": [use(made["id"], continuous=True)],
            }
        },
        method="PUT",
    )

    def current() -> dict[str, Any]:
        return call(engine, f"/api/edit/demo/worksheets/{sheet_id}")["worksheet"]

    def render() -> dict[str, Any]:
        return call(engine, f"/api/workbooks/demo/worksheets/{sheet_id}/render", {})

    before = render()
    at = before["table"]["keys"].index(made["id"])
    nulls = sum(1 for row in before["table"]["raw"] if row[at] in ("", None))
    total = len(before["table"]["raw"])
    assert 0 < nulls < total
    assert before["spec"]["usermeta"]["nulls"] == nulls

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1300, "height": 850})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{sheet_id}")
        settle(page)
        badge = page.locator(".editor .null-indicator")
        expect(badge).to_have_text(f"{nulls} 個 Null")
        badge.get_by_role("button").click()
        menu = badge.get_by_role("menuitem")
        expect(menu).to_have_text(["篩選資料", "在預設位置顯示資料", "隱藏指示器"])

        badge.get_by_role("menuitem", name="篩選資料").click()
        settle(page)
        saved = current()
        (added,) = [f for f in saved["filters"] if f["field"]["field"] == made["id"]]
        assert added["kind"] == "range"
        assert added["stage"] == "measure"
        assert (added["min"], added["max"]) == (None, None)
        assert (added["include_null"], added["only_null"]) == (False, False)
        # 量值本身沒有變（不是改成畫在 0），只是 Null 的列被篩掉
        assert saved["null_marks"] is None and saved["null_indicator"] is True
        after = render()
        assert len(after["table"]["raw"]) == total - nulls
        assert all(row[at] not in ("", None) for row in after["table"]["raw"])
        assert "nulls" not in after["spec"]["usermeta"]
        expect(page.locator(".editor .null-indicator")).to_have_count(0)
        # 篩選架上多了這個量值的膠囊
        expect(page.locator(".cards-pane [data-shelf='filters']")).to_contain_text("大區的業績")

        # 與其他編輯相同可以復原
        page.locator(".editor-canvas").click(position={"x": 5, "y": 5})
        page.keyboard.press("Control+z")
        settle(page)
        assert not [f for f in current()["filters"] if f["field"]["field"] == made["id"]]
        expect(badge).to_have_text(f"{nulls} 個 Null")

        # 右鍵也開出選單（Tableau 的「隱藏指示器」在右鍵選單）
        badge.click(button="right")
        expect(badge.get_by_role("menu")).to_be_visible()
        badge.get_by_role("menuitem", name="隱藏指示器").click()
        settle(page)
        assert current()["null_indicator"] is False
        expect(page.locator(".editor .null-indicator")).to_have_count(0)
        assert errors == []
        browser.close()
