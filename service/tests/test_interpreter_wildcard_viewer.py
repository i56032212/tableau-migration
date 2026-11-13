"""資料源分頁的資料解譯器與萬用字元聯集（端到端）：Excel 的工作表上面有標題、說明的列時勾選「使用資料
解譯器」；「萬用字元聯集…」以資料夾與檔名的模式接上其他檔案。需要建置好的檢視器（web/dist）與 Playwright 的
Chromium；缺少時略過。"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from openpyxl import Workbook

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
TOKEN = "interpreter-e2e-token-0123456789"


def chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not (DIST / "index.html").is_file() or not chromium(),
        reason="需要建置好的檢視器（web/dist）與 Playwright 的 Chromium",
    ),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


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
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


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


def report(path: Path, year: int, rows: list[tuple[str, float]]) -> Path:
    """上面有標題與說明的列（要用資料解譯器），第 4 列是標題列。"""
    book = Workbook()
    ws = book.active
    assert ws is not None
    ws.title = "Sales"
    ws["A1"] = f"Sales report {year}"
    ws["A2"] = "Figures in USD"
    ws.append([])
    ws.cell(4, 1, "Region")
    ws.cell(4, 2, "Sales")
    for i, (region, sales) in enumerate(rows, 5):
        ws.cell(i, 1, region)
        ws.cell(i, 2, sales)
    book.save(path)
    return path


def test_interpreter_and_wildcard_union(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    folder = tmp_path / "reports"
    folder.mkdir()
    first = report(folder / "Sales 2023.xlsx", 2023, [("East", 10), ("West", 20)])
    report(folder / "Sales 2024.xlsx", 2024, [("East", 5)])
    report(folder / "Budget.xlsx", 2024, [("East", 999)])
    created = call(engine, "/api/desktop/new", {"path": str(first), "table": "Sales"})
    workbook, datasource = created["document"]["id"], created["datasource"]
    wb = urllib.parse.quote(workbook)

    def model() -> Any:
        return call(engine, f"/api/edit/{wb}/datasources/{datasource}/model")

    def render(rows: str, measure: str) -> dict[str, str]:
        sheet = call(engine, f"/api/edit/{wb}/worksheets/ws_1")["worksheet"]
        sheet.update(rows=[{"field": rows}], columns=[{"field": measure, "agg": "sum"}])
        call(engine, f"/api/edit/{wb}/worksheets/ws_1", {"worksheet": sheet}, "PUT")
        body = call(engine, f"/api/workbooks/{wb}/worksheets/ws_1/render", {})
        return {row[0]: row[1] for row in body["table"]["rows"]}

    # 沒有資料解譯器時第一列（報表標題）被當成欄名
    assert "Sales report 2023" in {f["caption"] for f in model()["fields"]}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/{wb}/edit-ds/{datasource}")
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        # 勾選後重新檢查檔案（套用完成時才打勾）
        page.get_by_label("使用資料解譯器").click()
        page.wait_for_function(
            "document.querySelector('.dse-interpreter input')?.checked === true", timeout=30000
        )
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        page.wait_for_function(
            "[...document.querySelectorAll('.dse-field-grid input.dse-field-caption')]"
            ".some(i => i.value === 'Region')",
            timeout=30000,
        )
        fields = {f["caption"]: f["id"] for f in model()["fields"]}
        assert set(fields) == {"Region", "Sales"}
        assert render(fields["Region"], fields["Sales"]) == {"East": "10", "West": "20"}
        # 萬用字元聯集：Sales *.xlsx（Budget.xlsx 不算）
        page.get_by_role("button", name="萬用字元聯集…").click()
        dialog = page.get_by_role("dialog", name="萬用字元聯集：Sales 2023.xlsx")
        assert dialog.get_by_label("檔名的模式").input_value() == "Sales *.xlsx"
        dialog.get_by_label("搜尋的資料夾").fill(str(folder))
        dialog.get_by_role("button", name="聯集").click()
        dialog.wait_for(state="detached")
        page.wait_for_selector('.ds-editor[data-busy="false"]')
        page.get_by_text("萬用字元聯集「Sales *.xlsx」").wait_for()
        relation = model()["logical"][0]["relation"]
        assert relation["kind"] == "union" and relation["wildcard"]["files"] == "Sales *.xlsx"
        fields = {f["caption"]: f["id"] for f in model()["fields"]}
        assert {"Path", "Sheet"} <= set(fields)
        assert render(fields["Region"], fields["Sales"]) == {"East": "15", "West": "20"}
        paths = render(fields["Path"], fields["Sales"])
        assert paths == {str(first): "30", str(folder / "Sales 2024.xlsx"): "5"}
        assert errors == []
        browser.close()
