"""編輯器的智慧顯示（Tableau 的 Show Me，端到端）：依檢視中的欄位列出可用的圖表與建議的圖表，點選後
重新排列欄位，每一種都能查詢與繪製。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
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
TOKEN = "show-me-e2e-token-0123456789"
ORDERS = (
    "Order Date,State,Region,Category,Sales,Profit\n"
    "2024-01-05,California,West,Furniture,100.5,20\n"
    "2024-02-01,New York,East,Office,200,-5\n"
    "2024-02-20,Texas,Central,Office,50.25,7\n"
    "2024-03-09,Washington,West,Tech,75,30\n"
    "2024-03-15,Florida,South,Tech,310,60\n"
)


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


def test_show_me(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    path = tmp_path / "orders.csv"
    path.write_text(ORDERS, encoding="utf-8")
    created = call(engine, "/api/desktop/new", {"path": str(path)})
    workbook = created["document"]["id"]
    fields = call(engine, f"/api/edit/{workbook}/catalog")["datasources"][0]["fields"]
    ids = {f["caption"]: f["id"] for f in fields}

    def sheet() -> dict[str, Any]:
        return call(engine, f"/api/edit/{workbook}/worksheets/ws_1")["worksheet"]

    def place(rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> None:
        current = sheet()
        current.update(rows=rows, columns=columns, panes=[], reference_lines=[])
        current["marks"].update(
            type="automatic", color=None, size=None, label=[], detail=[], angle=None
        )
        call(engine, f"/api/edit/{workbook}/worksheets/ws_1", {"worksheet": current}, "PUT")

    def render() -> dict[str, Any]:
        return call(engine, f"/api/workbooks/{workbook}/worksheets/ws_1/render", {})

    sales = {"field": ids["Sales"], "agg": "sum", "continuous": True}
    profit = {"field": ids["Profit"], "agg": "sum", "continuous": True}
    region = {"field": ids["Region"]}
    category = {"field": ids["Category"]}
    state = {"field": ids["State"]}
    year = {"field": ids["Order Date"], "date": {"kind": "part", "unit": "year"}}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        def open_editor() -> Any:
            page.goto(f"{engine}/w/{workbook}/edit/ws_1")
            page.wait_for_selector('.editor[data-saving="false"]')
            page.get_by_role("button", name="智慧顯示").click()
            panel = page.get_by_role("dialog", name="智慧顯示")
            panel.wait_for()
            return panel

        def choose(panel: Any, chart: str) -> dict[str, Any]:
            panel.locator(f'[data-chart="{chart}"]').click()
            page.wait_for_selector('.editor[data-saving="false"]')
            page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
            page.wait_for_timeout(200)
            body = render()
            assert body.get("unsupported") in (None, ""), (chart, body.get("unsupported"))
            return sheet()

        # 一個維度、一個量值：建議水平長條；地圖、日期的圖不能用
        place([region], [sales])
        panel = open_editor()
        assert "recommended" in (panel.locator('[data-chart="hbar"]').get_attribute("class") or "")
        assert panel.locator('[data-chart="lines"]').is_disabled()
        assert panel.locator('[data-chart="scatter"]').is_disabled()
        current = choose(panel, "pie")
        assert current["marks"]["type"] == "pie"
        assert current["marks"]["color"]["field"] == ids["Region"]
        assert current["marks"]["angle"]["field"] == ids["Sales"]
        current = choose(panel, "text")
        assert current["marks"]["type"] == "text"
        assert [u["field"] for u in current["rows"]] == [ids["Region"]]
        assert [u["field"] for u in current["marks"]["label"]] == [ids["Sales"]]
        current = choose(panel, "treemap")
        assert (current["rows"], current["columns"], current["marks"]["type"]) == ([], [], "square")
        current = choose(panel, "side_bars")
        assert [u["field"] for u in current["columns"]] == [ids["Region"]]
        assert [u["field"] for u in current["rows"]] == [ids["Sales"]]
        # 兩個維度、兩個量值：散佈圖、子彈圖與文字表（Measure Names／Values）
        place([region, category], [sales, profit])
        panel = open_editor()
        current = choose(panel, "text")
        assert (
            current["measure_values"] and current["marks"]["label"][0]["field"] == "measure_values"
        )
        current = choose(panel, "scatter")
        assert [u["field"] for u in current["columns"]] == [ids["Sales"]]
        assert [u["field"] for u in current["rows"]] == [ids["Profit"]]
        current = choose(panel, "bullet")
        kinds = sorted(r["kind"] for r in current["reference_lines"])
        assert kinds == ["distribution", "line"]
        current = choose(panel, "boxplot")
        assert current["reference_lines"][0]["kind"] == "boxplot"
        current = choose(panel, "heatmap")
        assert current["marks"]["type"] == "square" and current["marks"]["size"] is not None
        # 日期：連續的線、雙重組合
        place([year], [sales, profit])
        panel = open_editor()
        assert "recommended" in (panel.locator('[data-chart="lines"]').get_attribute("class") or "")
        current = choose(panel, "lines")
        assert current["columns"][0]["continuous"] is True
        assert current["columns"][0]["date"]["kind"] == "trunc"
        current = choose(panel, "dual_combo")
        assert [p["marks"]["type"] for p in current["panes"]] == ["bar", "line"]
        # 地理欄位：符號地圖、填色地圖
        call(
            engine,
            f"/api/edit/{workbook}/datasources/{created['datasource']}/fields/{ids['State']}/geography",
            {"role": "state"},
            "PUT",
        )
        place([state], [sales])
        panel = open_editor()
        current = choose(panel, "filled_map")
        assert current["marks"]["geometry"] is not None
        current = choose(panel, "symbol_map")
        assert current["marks"]["type"] == "circle" and current["marks"]["geometry"] is None
        # 只有一個量值：直方圖（建立數值分組、CNT）
        place([], [sales])
        panel = open_editor()
        current = choose(panel, "histogram")
        catalog = call(engine, f"/api/edit/{workbook}/catalog")["datasources"][0]["fields"]
        bins = next(f for f in catalog if f["caption"] == "Sales (bin)")
        assert current["columns"][0]["field"] == bins["id"]
        assert current["rows"][0]["agg"] == "count"
        assert errors == []
        browser.close()
