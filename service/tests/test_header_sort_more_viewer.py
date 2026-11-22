"""在檢視上拖曳標題排序（續）的端到端：桌面版編輯器裡拖曳 Measure Names 的標題或色彩圖例的項目改變量值的順序
（Measure Values 卡上的順序跟著改），拖曳分格（窗格）的標題改成手動排序，以及在標題上按右鍵的「清除排序」。
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
TOKEN = "header-sort-more-e2e-token-0123456789"


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


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp_path = tmp_path_factory.mktemp("header_sort_more")
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


def use(field: str, agg: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "agg": agg,
        "date": None,
        "continuous": agg is not None or field == "measure_values",
        "table_calc": None,
    }


MEASURES = [use("f_sales", "sum"), use("f_profit", "sum"), use("f_quantity", "sum")]


def make_sheet(engine: str, name: str, **shelves: Any) -> str:
    created = call(engine, "/api/edit/demo/worksheets", {"name": name})["worksheet"]
    body = {
        **created,
        "rows": shelves.get("rows", []),
        "columns": shelves.get("columns", []),
        "measure_values": shelves.get("measure_values", []),
        "sorts": shelves.get("sorts", []),
    }
    body["marks"] = {
        **created["marks"],
        "label": shelves.get("label", []),
        "color": shelves.get("color"),
    }
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, method="PUT")
    return str(created["id"])


def saved(engine: str, sheet: str) -> dict[str, Any]:
    return call(engine, f"/api/edit/demo/worksheets/{sheet}")["worksheet"]


# 檢視上的文字（依位置由上而下、由左而右）
TEXTS = """([selector, axis]) => {
  const svg = document.querySelector('.editor .chart > svg, .editor .chart > .frozen-host > svg');
  return [...svg.querySelectorAll(selector)]
    .map((t) => { const r = t.getBoundingClientRect(); return [t.textContent.trim(), axis === 'x' ? r.left : r.top]; })
    .filter(([text]) => text !== '')
    .sort((a, b) => a[1] - b[1])
    .map(([text]) => text);
}"""

BOX = """([selector, text]) => {
  const svg = document.querySelector('.editor .chart > svg, .editor .chart > .frozen-host > svg');
  const found = [...svg.querySelectorAll(selector)].find((t) => t.textContent.trim() === text);
  const r = found.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, width: r.width, height: r.height };
}"""

AXIS_LABELS = "g.role-axis-label > text"
LEGEND = "g.role-legend-label > text"
PANE_TITLES = "g.role-title-text > text"
CARD = '.card[data-shelf="measure_values"] .pill'


def open_editor(page: Any, engine: str, sheet: str) -> None:
    page.goto(f"{engine}/w/demo/edit/{sheet}")
    settle(page)


def settle(page: Any) -> None:
    page.wait_for_selector('.editor[data-saving="false"]')
    page.wait_for_selector(".editor .worksheet[data-loading='false']", timeout=30000)
    page.wait_for_selector(".editor .chart[data-drawn='true'] svg", timeout=30000)
    page.wait_for_timeout(300)


def drag(page: Any, selector: str, member: str, target: str, axis: str) -> list[Any]:
    """把 member 拖到 target 之前；回傳拖曳中標示放下位置的線（left、top、寬、高）。"""
    start = page.evaluate(BOX, [selector, member])
    goal = page.evaluate(BOX, [selector, target])
    if axis == "y":
        end = (start["x"], goal["y"] - goal["height"] / 2 - 2)
    else:
        end = (goal["x"] - goal["width"] / 2 - 2, start["y"])
    page.mouse.move(start["x"], start["y"])
    page.mouse.down()
    page.mouse.move((start["x"] + end[0]) / 2, (start["y"] + end[1]) / 2, steps=4)
    page.mouse.move(end[0], end[1], steps=4)
    line = page.locator(".header-drop-line").evaluate_all(
        "ls => ls.map((l) => { const r = l.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })"
    )
    page.mouse.up()
    page.wait_for_timeout(300)
    settle(page)
    return line


def card(page: Any) -> list[str]:
    """Measure Values 卡上的膠囊（名稱，不含選單的按鈕）。"""
    return [text.split("\n")[0].strip() for text in page.locator(CARD).all_inner_texts()]


def fields(sheet: dict[str, Any]) -> list[str]:
    return [u["field"] for u in sheet["measure_values"]]


def test_drag_measure_names_reorders_the_measures(engine: str) -> None:
    """Measure Names 在欄上的文字表（Sales、Profit、Quantity）：把 Quantity 的欄標題拖到 Sales 之前——量值的順序
    改成 Quantity、Sales、Profit（Measure Values 卡上的順序跟著改，不另外加排序），欄跟著重排；Ctrl+Z 復原。
    Measure Names 在色彩上時，在圖例上把 Profit 拖到最上面：量值的順序是 Profit、Sales、Quantity，圖例跟著改。"""
    from playwright.sync_api import sync_playwright

    names = ["Sales", "Profit", "Quantity"]
    crosstab = make_sheet(
        engine,
        "names crosstab",
        rows=[use("f_category")],
        columns=[use("measure_names")],
        label=[use("measure_values")],
        measure_values=MEASURES,
    )
    legend = make_sheet(
        engine,
        "names legend",
        rows=[use("measure_values")],
        columns=[use("f_region")],
        color=use("measure_names"),
        measure_values=MEASURES,
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        open_editor(page, engine, crosstab)
        headers = [t for t in page.evaluate(TEXTS, [AXIS_LABELS, "x"]) if t in names]
        assert headers == names
        assert card(page) == [
            "SUM(Sales)",
            "SUM(Profit)",
            "SUM(Quantity)",
        ]
        line = drag(page, AXIS_LABELS, "Quantity", "Sales", "x")
        assert len(line) == 1 and line[0][2] <= 4 and line[0][3] > 20  # 直的粗線
        after = saved(engine, crosstab)
        assert fields(after) == ["f_quantity", "f_sales", "f_profit"]
        assert after["sorts"] == []
        assert [t for t in page.evaluate(TEXTS, [AXIS_LABELS, "x"]) if t in names] == [
            "Quantity",
            "Sales",
            "Profit",
        ]
        assert card(page) == [
            "SUM(Quantity)",
            "SUM(Sales)",
            "SUM(Profit)",
        ]
        # 與其他編輯相同可以復原
        page.locator(".editor-canvas").click(position={"x": 5, "y": 5})
        page.keyboard.press("Control+z")
        settle(page)
        assert fields(saved(engine, crosstab)) == ["f_sales", "f_profit", "f_quantity"]
        assert [t for t in page.evaluate(TEXTS, [AXIS_LABELS, "x"]) if t in names] == names

        open_editor(page, engine, legend)
        assert page.evaluate(TEXTS, [LEGEND, "y"]) == names
        drag(page, LEGEND, "Profit", "Sales", "y")
        assert fields(saved(engine, legend)) == ["f_profit", "f_sales", "f_quantity"]
        assert page.evaluate(TEXTS, [LEGEND, "y"]) == ["Profit", "Sales", "Quantity"]
        assert errors == []
        browser.close()


def test_drag_pane_titles_sets_a_manual_sort(engine: str) -> None:
    """分格的標題：欄是 Region 與 SUM(Sales)（每個地區一格）時，把 West 的標題拖到 Central 之前——Region 的排序
    改成手動、順序是 West、Central、East、South，各格跟著重排。列是 Category 與 SUM(Sales) 時，把 Technology 的
    標題拖到最上面：Category 的排序是 Technology、Furniture、Office Supplies。"""
    from playwright.sync_api import sync_playwright

    across = make_sheet(
        engine,
        "panes across",
        rows=[use("f_category")],
        columns=[use("f_region"), use("f_sales", "sum")],
    )
    down = make_sheet(
        engine,
        "panes down",
        rows=[use("f_category"), use("f_sales", "sum")],
        columns=[use("f_region")],
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        open_editor(page, engine, across)
        assert page.evaluate(TEXTS, [PANE_TITLES, "x"]) == ["Central", "East", "South", "West"]
        line = drag(page, PANE_TITLES, "West", "Central", "x")
        assert len(line) == 1 and line[0][2] <= 4
        (sort,) = saved(engine, across)["sorts"]
        assert (sort["field"], sort["kind"], sort["manual_values"]) == (
            "f_region",
            "manual",
            ["West", "Central", "East", "South"],
        )
        assert page.evaluate(TEXTS, [PANE_TITLES, "x"]) == ["West", "Central", "East", "South"]

        open_editor(page, engine, down)
        assert page.evaluate(TEXTS, [PANE_TITLES, "y"]) == [
            "Furniture",
            "Office Supplies",
            "Technology",
        ]
        drag(page, PANE_TITLES, "Technology", "Furniture", "y")
        (sort,) = saved(engine, down)["sorts"]
        assert (sort["field"], sort["kind"], sort["manual_values"]) == (
            "f_category",
            "manual",
            ["Technology", "Furniture", "Office Supplies"],
        )
        assert page.evaluate(TEXTS, [PANE_TITLES, "y"]) == [
            "Technology",
            "Furniture",
            "Office Supplies",
        ]
        assert errors == []
        browser.close()


def test_header_menu_clears_the_sort(engine: str) -> None:
    """長條圖的 Sub-Category 依 SUM(Sales) 遞減排序：在列標題 Chairs 上按右鍵，選單的「清除排序」可以按——按了之後
    排序拿掉、列回到字母順序；再按右鍵時「清除排序」不能按。在標題以外的地方按右鍵不開這個選單。"""
    from playwright.sync_api import sync_playwright

    by_sales = {
        "field": "f_sub_category",
        "kind": "by_field",
        "order": "desc",
        "by": use("f_sales", "sum"),
        "nested": False,
        "manual_values": [],
    }
    sheet = make_sheet(
        engine,
        "header menu",
        rows=[use("f_sub_category")],
        columns=[use("f_sales", "sum")],
        sorts=[by_sales],
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        open_editor(page, engine, sheet)
        rows = [t for t in page.evaluate(TEXTS, [AXIS_LABELS, "y"]) if t[0].isalpha()]
        assert rows[:2] == ["Phones", "Chairs"]

        label = page.evaluate(BOX, [AXIS_LABELS, "Chairs"])
        page.mouse.click(label["x"], label["y"], button="right")
        menu = page.locator(".popup-menu")
        item = menu.get_by_role("menuitem", name="清除排序")
        assert item.get_attribute("aria-disabled") is None
        item.click()
        settle(page)
        assert saved(engine, sheet)["sorts"] == []
        rows = [t for t in page.evaluate(TEXTS, [AXIS_LABELS, "y"]) if t[0].isalpha()]
        assert rows[:3] == ["Accessories", "Appliances", "Art"]

        label = page.evaluate(BOX, [AXIS_LABELS, "Chairs"])
        page.mouse.click(label["x"], label["y"], button="right")
        assert (
            menu.get_by_role("menuitem", name="清除排序").get_attribute("aria-disabled") == "true"
        )
        page.keyboard.press("Escape")
        assert page.locator(".popup-menu").count() == 0

        # 標題以外（圖的空白處）：沒有這個選單
        chart = page.locator(".editor .chart").bounding_box()
        assert chart is not None
        page.mouse.click(
            chart["x"] + chart["width"] - 20, chart["y"] + chart["height"] / 2, button="right"
        )
        assert page.locator(".popup-menu").count() == 0
        assert errors == []
        browser.close()
