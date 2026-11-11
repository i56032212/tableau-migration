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


def test_export_image_and_powerpoint(engine: str, tmp_path: Path) -> None:
    """匯出：畫面上的 dashboard 畫成 PNG，PowerPoint 是一張投影片放這張圖（桌面版以存檔對話框問位置）。"""
    from playwright.sync_api import Dialog, expect, sync_playwright

    answers = [str(tmp_path / "dashboard.png"), str(tmp_path / "dashboard")]

    def answer(dialog: Dialog) -> None:
        dialog.accept(answers.pop(0))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("dialog", answer)
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        page.wait_for_function(
            "(() => { const items = Array.from(document.querySelectorAll('.worksheet'));"
            " return items.length > 0 && items.every(e => e.dataset.loading === 'false'); })()"
        )
        page.get_by_label("匯出").select_option("image")
        expect(page.locator(".toast")).to_contain_text("已匯出")
        png = (tmp_path / "dashboard.png").read_bytes()
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        assert (width, height) == (2400, 1600)  # 1200×800 的 dashboard，兩倍解析度
        page.locator(".toast").evaluate("element => element.remove()")
        page.get_by_label("匯出").select_option("powerpoint")
        expect(page.locator(".toast")).to_contain_text("已匯出")
        browser.close()
    with zipfile.ZipFile(tmp_path / "dashboard.pptx") as archive:
        names = set(archive.namelist())
        assert {"ppt/presentation.xml", "ppt/slides/slide1.xml", "ppt/media/image1.png"} <= names
        assert archive.read("ppt/media/image1.png").startswith(b"\x89PNG")
        assert archive.testzip() is None


def test_pages_and_disaggregation(engine: str) -> None:
    """頁面架：拖到「頁面」後出現頁面控制項，切換頁只顯示那一頁；檢視器也能切換。
    彙總量值關閉後量值不彙總（每一列資料一個標記）。"""
    from playwright.sync_api import Page, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        page.locator('.data-pane [data-field="f_order_date"]').drag_to(
            page.locator('[data-drop="pages"]')
        )
        control = page.locator(".editor [data-page-control]")
        control.wait_for()
        settle(page)
        # 沒有存過的頁：從第一頁開始，並存回工作表
        page.wait_for_function(
            "() => document.querySelector('.editor [data-page-control] select')?.value === '0'"
        )
        settle(page)
        years = control.locator("option").all_inner_texts()
        assert len(years) >= 2 and years == sorted(years)
        saved = sheet()
        assert saved["pages"]["date"] == {"kind": "part", "unit": "year"}
        assert str(saved["page"]) == years[0]
        control.get_by_role("button", name="下一頁").click()
        settle(page)
        assert str(sheet()["page"]) == years[1]

        # 彙總量值關閉：SUM(Sales) 變成不彙總的 Sales
        page.get_by_label("彙總量值").uncheck()
        settle(page)
        saved = sheet()
        assert saved["disaggregated"] is True
        assert saved["columns"][0]["agg"] is None
        page.get_by_label("彙總量值").check()
        settle(page)
        assert sheet()["columns"][0]["agg"] == "sum"

        # 檢視器：頁面控制項切換頁
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        viewer = page.locator(".single-sheet [data-page-control]")
        viewer.wait_for()
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        # 存下的頁（第二頁）在工作表載入後才套用到控制項
        page.wait_for_function(
            "() => document.querySelector('.single-sheet [data-page-control] select')?.value === '1'"
        )
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            viewer.get_by_role("button", name="上一頁").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["pages"] == {"ws_sales_by_region": int(years[0])}
        assert errors == []
        browser.close()


def test_hierarchies_and_drill(engine: str) -> None:
    """資料窗格建立階層（Region → State/Province → City）；欄、列架上的膠囊以 ⊞ 向下切入、⊟ 收合；
    檢視器以工作表標題旁的按鈕切入，數字與存檔時就把下一層放在架上的工作表相同。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]

    def rows() -> list[str]:
        return [u["field"] for u in sheet()["rows"]]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        pane = page.locator(".data-pane")
        # Region 不在階層中：膠囊沒有切入按鈕
        assert page.locator('[data-shelf="rows"] .pill-drill').count() == 0
        region = pane.locator('[data-field="f_region"]')
        region.hover()
        region.get_by_role("button", name="以「Region」建立階層").click()
        dialog = page.locator(".hierarchy-dialog")
        expect(dialog.get_by_label("階層名稱")).to_have_value("Region 階層")
        dialog.get_by_label("階層名稱").fill("Location")
        dialog.get_by_label("加入階層的欄位").select_option(label="City")
        dialog.get_by_role("button", name="加入").click()
        dialog.get_by_label("加入階層的欄位").select_option(label="State/Province")
        dialog.get_by_role("button", name="加入").click()
        dialog.get_by_role("button", name="上移 State/Province").click()
        dialog.get_by_role("button", name="確定").click()
        group = pane.locator('[data-hierarchy="Location"]')
        group.wait_for()
        assert group.locator(".field .field-name").all_inner_texts() == [
            "Region",
            "State/Province",
            "City",
        ]
        catalog = call(engine, "/api/edit/demo/catalog")
        assert catalog["datasources"][0]["hierarchies"] == [
            {"name": "Location", "fields": ["f_region", "f_state_province", "f_city"]}
        ]

        # 膠囊的 ⊞：加入下一層；下一層膠囊再 ⊞ 加入 City；Region 的 ⊟ 收合兩層
        drill = page.locator('[data-shelf="rows"] .pill-drill')
        drill.first.wait_for()
        expect(drill.first).to_have_attribute("aria-label", "向下切入到 State/Province")
        drill.first.click()
        settle(page)
        assert rows() == ["f_region", "f_state_province"]
        expect(drill.first).to_have_attribute("aria-label", "收合 State/Province")
        drill.nth(1).click()
        settle(page)
        assert rows() == ["f_region", "f_state_province", "f_city"]
        drill.first.click()
        settle(page)
        assert rows() == ["f_region"]

        # 檢視器：標題旁的「向下切入到 State/Province」
        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        viewer = page.locator(".single-sheet .worksheet")
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            viewer.get_by_role("button", name="向下切入到 State/Province").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["drill"] == {"f_region": 1}
        data = response.value.json()
        assert data["table"]["headers"][:2] == ["Region", "State/Province"]
        expect(viewer.get_by_role("button", name="向上收合 State/Province")).to_be_visible()
        expect(viewer.get_by_role("button", name="向下切入到 City")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/render")) as response:
            viewer.get_by_role("button", name="向上收合 State/Province").click()
        body = json.loads(response.value.request.post_data or "{}")
        assert "drill" not in body
        assert response.value.json()["table"]["headers"][0] == "Region"

        # 資料窗格：從階層移除、刪除階層
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        city = group.locator('[data-field="f_city"]')
        city.hover()
        city.get_by_role("button", name="從階層移除「City」").click()
        expect(group.locator(".field")).to_have_count(2)
        name = group.locator(".hierarchy-name")
        name.hover()
        name.get_by_role("button", name="刪除階層 Location").click()
        expect(group).to_have_count(0)
        assert call(engine, "/api/edit/demo/catalog")["datasources"][0]["hierarchies"] == []
        assert page.locator('[data-shelf="rows"] .pill-drill').count() == 0
        assert errors == []
        browser.close()


def test_tooltip_sheet_in_editor(engine: str) -> None:
    """標記卡的「工具提示…」：加入工具提示中的工作表（以選定的欄位篩選）；檢視器滑過長條時工具提示中
    顯示以 Region 篩選的 Monthly Profit。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def sheet() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/worksheets/ws_sales_by_region")["worksheet"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        page.get_by_role("button", name="工具提示…").click()
        dialog = page.locator(".tooltip-dialog")
        dialog.get_by_role("button", name="加入工作表").click()
        dialog.get_by_label("工具提示中的工作表", exact=True).select_option(label="Monthly Profit")
        dialog.get_by_label("最大寬度").fill("280")
        dialog.get_by_label("選定的欄位").check()
        dialog.get_by_label("Region").check()
        dialog.get_by_role("button", name="確定").click()
        settle(page)
        tips = sheet()["marks"]["tooltip_sheets"]
        assert tips == [
            {
                "worksheet": "ws_monthly_profit",
                "max_width": 280,
                "max_height": 300,
                "fields": [
                    {
                        "field": "f_region",
                        "agg": None,
                        "date": None,
                        "continuous": False,
                        "table_calc": None,
                    }
                ],
            }
        ]

        page.goto(f"{engine}/w/demo/s/ws_sales_by_region")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]')
        # 圖表本身的長條（凍結的欄標題、座標軸是圖表的複本，也有同樣的標記）
        bar = page.locator(".single-sheet svg path[aria-roledescription='bar']:not(.frozen *)").last
        label = bar.get_attribute("aria-label") or ""
        region = label.split("Region: ")[1].split(";")[0]
        with page.expect_response(
            lambda r: "ws_monthly_profit" in r.url and r.url.endswith("/render")
        ) as response:
            bar.hover()
        body = json.loads(response.value.request.post_data or "{}")
        assert body["tooltip"]["source"] == "ws_sales_by_region"
        # 標記的所有維度都送出；只以選定的 Region 篩選
        assert body["tooltip"]["mark"]["f_region"] == region
        tooltip = page.locator(".viz-tooltip")
        expect(tooltip.locator("[data-tooltip-sheet='ws_monthly_profit'] svg")).to_have_count(1)
        west = response.value.json()
        everything = call(engine, "/api/workbooks/demo/worksheets/ws_monthly_profit/render", {})
        assert west["table"]["raw"] != everything["table"]["raw"]

        # 移除工具提示中的工作表
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)
        page.get_by_role("button", name="工具提示…").click()
        dialog.get_by_role("button", name="移除工具提示中的工作表").click()
        dialog.get_by_role("button", name="確定").click()
        settle(page)
        assert sheet()["marks"]["tooltip_sheets"] == []
        assert errors == []
        browser.close()


def test_dynamic_parameters_in_editor(engine: str) -> None:
    """參數對話框的「開啟報表時的值」與「開啟報表時取自欄位」：選了欄位就以目前的資料填入，存成動態參數；
    檢視器取得的參數是以資料取值後的。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    calc = call(
        engine,
        "/api/edit/demo/calcs",
        {"datasource": "ds_orders", "caption": "Latest", "formula": "{MAX([Order Date])}"},
    )["field"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/ws_sales_by_region")
        settle(page)

        page.locator("button[title='建立參數']").click()
        dialog = page.locator(".parameter-dialog")
        dialog.get_by_label("參數名稱").fill("As Of")
        dialog.get_by_label("參數的資料類型").select_option("date")
        dialog.get_by_label("開啟報表時的值").select_option(calc["id"])
        expect(dialog.get_by_label("參數的目前值")).to_have_value("2024-12-30")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)

        page.locator("button[title='建立參數']").click()
        dialog.get_by_label("參數名稱").fill("Region Choice")
        dialog.get_by_label("參數的資料類型").select_option("string")
        dialog.get_by_role("radio", name="清單").check()
        dialog.get_by_label("開啟報表時取自欄位").select_option("f_region")
        expect(dialog.get_by_label("允許的值清單")).to_have_value("Central\nEast\nSouth\nWest")
        expect(dialog.get_by_label("參數的目前值")).to_have_value("Central")
        dialog.get_by_role("button", name="確定").click()
        expect(dialog).to_have_count(0)

        catalog = call(engine, "/api/edit/demo/catalog")
        params = {p["caption"]: p for p in catalog["parameters"]}
        assert params["As Of"]["value_field"] == calc["id"]
        assert params["As Of"]["value"] == "2024-12-30"
        assert params["Region Choice"]["domain_field"] == "f_region"
        assert params["Region Choice"]["allowed"]["values"] == ["Central", "East", "South", "West"]
        viewer = {p["caption"]: p for p in call(engine, "/api/workbooks/demo/ir")["parameters"]}
        assert viewer["As Of"]["value"] == "2024-12-30"
        assert errors == []
        browser.close()


def test_device_layouts_in_dashboard_editor(engine: str) -> None:
    """dashboard 編輯器的裝置版面：新增手機版面（預設版面上的物件由上到下排列）、在手機版面上移除區塊
    只影響手機版面、刪除手機版面；檢視器在手機大小的畫面上用手機版面。"""
    from playwright.sync_api import Page, expect, sync_playwright

    def settle(page: Page) -> None:
        page.wait_for_selector('[data-saving="false"]')
        page.wait_for_timeout(300)

    def board() -> dict[str, Any]:
        return call(engine, "/api/edit/demo/dashboards/db_ee909b0c")["dashboard"]

    def sheets(zones: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for zone in zones:
            if zone["kind"] == "worksheet":
                found.append(zone["worksheet"])
            found.extend(sheets(zone["children"]))
        return found

    default_sheets = sheets(board()["zones"])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit-d/db_ee909b0c")
        page.wait_for_selector(".dash-editor .dashboard [data-zone] svg", timeout=30000)
        page.get_by_label("新增裝置版面").select_option("phone")
        settle(page)
        expect(page.get_by_label("裝置版面", exact=True)).to_have_value("phone")
        (phone,) = board()["layouts"]
        assert (phone["device"], phone["sizing"], phone["size"]["width"]) == (
            "phone",
            "fit_width",
            375,
        )
        assert sheets(phone["zones"]) == default_sheets
        stage = page.locator(".dash-stage")
        box = stage.bounding_box()
        assert box is not None and box["width"] <= 376
        # 手機版面上移除第一個工作表：只影響手機版面
        first = next(
            z for z in phone["zones"][0]["children"][0]["children"] if z["kind"] == "worksheet"
        )
        page.locator(f'[data-tree-zone="{first["id"]}"]').click()
        expect(page.locator(f'[data-zone-props="{first["id"]}"]')).to_be_visible()
        page.locator(".dash-props").get_by_role("button", name="移除").click()
        settle(page)
        saved = board()
        assert sheets(saved["layouts"][0]["zones"]) == default_sheets[1:]
        assert sheets(saved["zones"]) == default_sheets

        # 檢視器：手機大小的畫面用手機版面
        phone_page = browser.new_page(viewport={"width": 390, "height": 844})
        phone_page.goto(f"{engine}/?token={TOKEN}")
        phone_page.goto(f"{engine}/w/demo/d/db_ee909b0c")
        phone_page.wait_for_selector(
            ".dashboard[data-device='phone'] [data-zone] svg", timeout=30000
        )
        phone_page.close()

        page.get_by_role("button", name="刪除手機版面").click()
        settle(page)
        assert board()["layouts"] == []
        expect(page.get_by_label("裝置版面", exact=True)).to_have_value("default")
        assert errors == []
        browser.close()
