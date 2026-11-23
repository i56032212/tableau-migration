"""叢集對話框的變數（Tableau 的 Clusters 對話框）的端到端：對話框不擋住編輯器、開啟時換到資料窗格，從資料窗格
把維度拖進「變數」成為類別變數（ATTR），存進 ClusterSpec.categories；日期與檢視以外的量值不能加入（說明原因），
Table Calculation 不在預設的變數中；變數拖出變數區（或按 ×）移除。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import shutil
import sys
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from editor_ui import side_tab

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "cluster-variables-e2e-token-0123456789"


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


@pytest.fixture()
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


def category(field: str) -> dict[str, Any]:
    return {"field": field, "agg": None, "date": None, "continuous": False, "table_calc": None}


def test_drag_dimensions_into_cluster_variables(engine: str) -> None:
    """各子類別的利潤：「＋ 叢集…」開啟的對話框沒有遮住編輯器的背景、側欄換到資料窗格；變數預設是檢視中的量值
    SUM(Profit)，不含 Table Calculation（標籤上的「利潤排名」）。從資料窗格拖 Category 進變數區成為
    ATTR(Category)；拖 Order Date（日期）、Sales（不在檢視中的量值）不加入並說明原因。叢集數 3、確定之後
    叢集的類別變數存進工作表，描述叢集列出 ATTR(Category)；關閉之後回到分析分頁。再開啟時把 ATTR(Category)
    拖出變數區就移除，確定之後沒有類別變數。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def stored() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sub_category_profit")["worksheet"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sub_category_profit")
        settle(page)
        side_tab(page, "分析")
        page.get_by_role("button", name="＋ 叢集…").click()
        dialog = page.locator(".cluster-dialog")
        dialog.wait_for()
        # 不擋住編輯器：沒有背景遮罩，側欄換到資料窗格
        assert page.locator(".modal-backdrop").count() == 0
        expect(
            page.locator(".side-tabs").get_by_role("tab", name="資料", exact=True)
        ).to_have_attribute("aria-selected", "true")
        variables = dialog.locator(".cluster-variables")
        pills = variables.locator("li.pill .pill-text")
        expect(pills).to_have_text(["SUM(Profit)", "平均訂單利潤"])

        pane = page.locator(".data-pane")
        pane.locator('li[data-field="f_category"]').drag_to(variables)
        expect(pills).to_have_text(["SUM(Profit)", "平均訂單利潤", "ATTR(Category)"])
        pane.locator('li[data-field="f_order_date"]').drag_to(variables)
        expect(dialog.get_by_role("alert")).to_contain_text("日期")
        pane.locator('li[data-field="f_sales"]').drag_to(variables)
        expect(dialog.get_by_role("alert")).to_contain_text("不在檢視中")
        expect(pills).to_have_count(3)

        dialog.get_by_label("自動決定叢集數").uncheck()
        dialog.get_by_label("叢集數", exact=True).fill("3")
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        settle(page)
        cluster = stored()["cluster"]
        assert cluster["categories"] == [category("f_category")]
        assert cluster["measures"][0]["field"] == "f_profit"
        assert cluster["count"] == 3
        rendered = call(engine, "/api/workbooks/demo/worksheets/ws_sub_category_profit/render", {})
        assert rendered["unsupported"] is None, rendered["unsupported"]
        assert rendered["describe"]["cluster"]["categories"] == ["ATTR(Category)"]
        # 關閉之後回到開啟前的分析分頁
        page.get_by_role("button", name="叢集 ✓").click()
        dialog.wait_for()
        expect(pills).to_have_text(["SUM(Profit)", "平均訂單利潤", "ATTR(Category)"])
        # 拖出變數區（放在不接受拖放的標題上）就移除
        variables.locator('li.pill[data-variable="attr:f_category"]').drag_to(
            page.locator(".floating-dialog header h2")
        )
        expect(pills).to_have_text(["SUM(Profit)", "平均訂單利潤"])
        # × 也可以移除
        variables.get_by_role("button", name="移除變數 平均訂單利潤").click()
        expect(pills).to_have_text(["SUM(Profit)"])
        dialog.get_by_role("button", name="確定").click()
        dialog.wait_for(state="detached")
        settle(page)
        deadline = time.monotonic() + 10
        while stored()["cluster"]["categories"]:
            assert time.monotonic() < deadline
            time.sleep(0.2)
        assert [m["field"] for m in stored()["cluster"]["measures"]] == ["f_profit"]
        assert errors == []
        browser.close()
