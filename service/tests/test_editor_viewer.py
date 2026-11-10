"""桌面版編輯器的端到端：拖放欄位、膠囊選單、快速 Table Calculation、復原、篩選、計算欄位、新增工作表、
雙軸與存檔。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
import zipfile
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
TOKEN = "editor-e2e-token-0123456789"


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


def call(base: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def test_edit_worksheet_in_viewer(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(
            '.editor .worksheet[data-loading="false"], .editor-canvas .placeholder', timeout=30000
        )

    def pills(page: Page, shelf: str) -> list[str]:
        return [
            t.replace("▾", "").strip()
            for t in page.locator(f'[data-shelf="{shelf}"] .pill .pill-text').all_inner_texts()
        ]

    def menu(page: Page, *labels: str) -> None:
        for i, label in enumerate(labels):
            item = page.locator(".popup-menu li[role=menuitem] > .menu-label", has_text=label).first
            if i < len(labels) - 1:
                item.hover()
            else:
                item.click()

    def table(worksheet: str) -> dict[str, Any]:
        return call(engine, f"/api/workbooks/demo/worksheets/{worksheet}/render", {})["table"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.get_by_role("button", name="編輯工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        assert pills(page, "rows") == ["Region"]
        assert pills(page, "columns") == ["SUM(Sales)"]

        # 拖放：Segment 放到列
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="rows"]')
        )
        settle(page)
        assert pills(page, "rows") == ["Region", "Segment"]
        data = table("ws_sales_by_region")
        assert "f_segment" in data["keys"]

        # 膠囊選單：彙總改成平均，再加上快速 Table Calculation（累計）
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Sales)").click()
        menu(page, "量值", "平均")
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales)"]
        page.locator('[data-shelf="columns"] .pill', has_text="AVG(Sales)").click()
        menu(page, "快速表計算", "累計")
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales) △"]
        assert any(k.startswith("avg(f_sales)#tc_") for k in table("ws_sales_by_region")["keys"])

        # 復原兩次、重做一次
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        assert pills(page, "columns") == ["SUM(Sales)"]
        page.get_by_role("button", name="↷ 重做").click()
        settle(page)
        assert pills(page, "columns") == ["AVG(Sales)"]
        assert "avg(f_sales)" in table("ws_sales_by_region")["keys"]

        # 篩選：排除 Corporate
        page.locator('.data-pane [data-field="f_segment"]').drag_to(
            page.locator('[data-drop="filters"]')
        )
        page.wait_for_selector(".filter-dialog .value-list li")
        page.locator(".value-list label", has_text="Corporate").locator("input").uncheck()
        page.locator(".filter-dialog").get_by_role("button", name="確定").click()
        # 對話框先取得篩選的 id 再套用：關閉後才開始存檔
        expect(page.locator(".filter-dialog")).to_have_count(0)
        expect(page.locator('[data-shelf="filters"] .pill', has_text="Segment")).to_be_visible()
        settle(page)
        data = table("ws_sales_by_region")
        segment = data["keys"].index("f_segment")
        assert {row[segment] for row in data["raw"]} == {"Consumer", "Home Office"}

        # 計算欄位：建立後放到色彩
        page.get_by_role("button", name="建立計算欄位…").click()
        page.get_by_label("計算欄位名稱").fill("Profit Ratio")
        page.get_by_label("公式").fill("SUM([Profit]) / SUM([Sales])")
        expect(page.locator(".calc-status")).to_contain_text("計算有效")
        page.locator(".calc-dialog").get_by_role("button", name="確定").click()
        field = page.locator(".data-pane .field", has_text="Profit Ratio")
        expect(field).to_be_visible()
        field.drag_to(page.locator('[data-drop="marks.color"]'))
        settle(page)
        assert pills(page, "marks")[0] == "Profit Ratio"

        # 新增工作表：連按兩下加入欄位、改名
        page.get_by_role("button", name="新增工作表").click()
        page.wait_for_url(f"{engine}/w/demo/edit/ws_1")
        page.wait_for_selector(".editor-canvas .placeholder")
        page.locator('.data-pane [data-field="f_category"]').dblclick()
        settle(page)
        page.locator('.data-pane [data-field="f_sales"]').dblclick()
        settle(page)
        assert (pills(page, "rows"), pills(page, "columns")) == (["Category"], ["SUM(Sales)"])
        assert len(table("ws_1")["rows"]) == 3
        name = page.get_by_label("工作表名稱")
        name.fill("類別銷售")
        name.press("Enter")
        settle(page)
        expect(page.locator(".toolbar select")).to_contain_text("工作表：類別銷售")

        # 雙軸：Profit 與 Sales 重疊，Measure Names 放到色彩
        page.locator('.data-pane [data-field="f_profit"]').drag_to(
            page.locator('[data-drop="columns"]')
        )
        settle(page)
        page.locator('[data-shelf="columns"] .pill', has_text="SUM(Profit)").click()
        menu(page, "雙軸")
        settle(page)
        expect(page.locator(".mark-tabs button")).to_have_count(2)
        sheet = call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]
        assert sheet["panes"][0]["dual"] is True
        assert sheet["marks"]["color"]["field"] == "measure_names"

        # 工作表的格式：列的格線、背景
        page.get_by_role("button", name="格式…").click()
        page.locator(".format-dialog fieldset", has_text="列").get_by_label("格線").uncheck()
        page.locator(".format-dialog").get_by_role("button", name="確定").click()
        settle(page)
        sheet = call(engine, "/api/edit/demo/worksheets/ws_1")["worksheet"]
        assert sheet["row_style"]["grid"] is False and sheet["column_style"]["grid"] is True

        # 存檔後變更寫進報表套件
        target = tmp_path / "edited.tmigx"
        assert call(engine, "/api/desktop/documents")[0]["modified"] is True
        call(engine, "/api/desktop/save", {"workbook": "demo", "path": str(target)})
        with zipfile.ZipFile(target) as archive:
            saved = json.loads(archive.read("report.ir.json"))
        names = [w["name"] for w in saved["worksheets"]]
        assert "類別銷售" in names
        # 完成編輯：回到工作表的檢視
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/s/ws_1")
        page.wait_for_selector(".single-sheet .worksheet svg")
        assert errors == []
        browser.close()


def test_edit_dashboard_and_story(engine: str) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('[data-saving="false"]')
        page.wait_for_timeout(300)

    def board() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]

    def zone_ids(zones: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for zone in zones:
            found.append(zone["id"])
            found.extend(zone_ids(zone["children"]))
        return found

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        page.get_by_role("button", name="編輯 dashboard").click()
        page.wait_for_url(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector(".dash-editor .dashboard [data-zone] svg", timeout=30000)

        # 選取工作表區塊後移除（版面配置與畫面都選得到）
        page.locator('[data-tree-zone="z10"]').click()
        expect(page.locator('[data-zone-props="z10"]')).to_be_visible()
        page.locator(".dash-props").get_by_role("button", name="移除").click()
        settle(page)
        assert "z10" not in zone_ids(board()["zones"])

        # 以浮動方式加入文字，拖曳移動
        page.get_by_label("以浮動方式加入").check()
        page.locator(".object-buttons").get_by_role("button", name="文字").click()
        settle(page)
        floating = board()["zones"][1]
        assert floating["kind"] == "text"
        page.get_by_label("文字", exact=True).fill("說明")
        page.locator(".dash-props .prop-title").click()
        settle(page)
        selected = page.locator(".dash-zone.selected")
        box = selected.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + 20, box["y"] + 20)
        page.mouse.down()
        page.mouse.move(box["x"] + 220, box["y"] + 120, steps=6)
        page.mouse.up()
        settle(page)
        moved = board()["zones"][1]
        assert moved["text"] == "說明" and moved["x"] > floating["x"] and moved["y"] > floating["y"]

        # 工作表拖到區塊上：並排加入（分割那個區塊）
        page.get_by_label("以浮動方式加入").uncheck()
        page.locator('.dash-side [data-sheet="ws_sub_category_profit"]').drag_to(
            page.locator('[data-overlay="z9"]')
        )
        settle(page)
        sheets = [z for z in zone_ids(board()["zones"])]
        assert len(sheets) == len(set(sheets))
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(4)

        # 復原（加入的工作表）再重做
        page.get_by_role("button", name="↶ 復原").click()
        settle(page)
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(3)
        page.get_by_role("button", name="↷ 重做").click()
        settle(page)
        expect(page.locator(".dash-editor .dashboard [data-zone] .worksheet")).to_have_count(4)

        # 動作：點選 Sales by Region 的標記篩選 Region Share by Year
        page.get_by_role("button", name="動作…").click()
        page.get_by_role("button", name="新增動作").click()
        page.get_by_label("動作名稱").fill("依區域篩選")
        page.locator('[aria-label="來源工作表"] label', has_text="Sales by Region").locator(
            "input"
        ).check()
        page.locator('[aria-label="目標工作表"] label', has_text="Region Share by Year").locator(
            "input"
        ).check()
        page.locator(".action-form").get_by_role("button", name="確定").click()
        expect(page.locator(".modal table.list")).to_contain_text("依區域篩選")
        page.locator(".modal .dialog-actions").get_by_role("button", name="關閉").click()

        # dashboard 的大小
        width = page.get_by_label("dashboard 的寬度")
        page.locator(".dash-canvas").click(position={"x": 5, "y": 5})
        width.fill("1300")
        width.press("Tab")
        settle(page)
        assert board()["size"]["width"] == 1300

        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/d/db_ee909b0c")
        page.wait_for_selector(".dashboard [data-zone] svg")
        actions = call(engine, "/api/edit/demo/outline")["actions"]
        assert [a["caption"] for a in actions] == ["依區域篩選"]

        # Story：新增後編輯故事點
        story = call(engine, "/api/edit/demo/stories", {})["story"]
        page.goto(f"{engine}/w/demo/edit-story/{story['id']}")
        page.wait_for_selector(
            ".story-editor .story-preview .dashboard [data-zone] svg", timeout=30000
        )
        caption = page.get_by_label("故事點 1 的說明")
        caption.fill("全公司概況")
        caption.press("Tab")
        settle(page)
        page.get_by_label("新增故事點").select_option("s:ws_monthly_profit")
        settle(page)
        saved = call(engine, f"/api/edit/demo/stories/{story['id']}")["story"]
        assert [p["caption"] for p in saved["points"]] == ["全公司概況", ""]
        assert saved["points"][1]["worksheet"] == "ws_monthly_profit"
        page.get_by_role("button", name="完成").click()
        page.wait_for_url(f"{engine}/w/demo/story/{story['id']}")
        expect(page.locator(".story-points button.active")).to_have_text("全公司概況")
        assert errors == []
        browser.close()
