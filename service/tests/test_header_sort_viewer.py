"""在檢視上拖曳標題或圖例的項目排序（Tableau：把檢視或圖例上的標題拖到另一個位置就是手動排序）的端到端：
桌面版編輯器裡拖曳列標題、多層列標題的內層、欄標題與色彩圖例的項目，這個欄位的排序改成手動（原本的排序
換成畫面上的順序、拖曳的成員移到放下的位置），檢視跟著重畫，拖曳時有標示放下位置的線；與其他編輯相同可以
復原。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

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
TOKEN = "header-sort-e2e-token-0123456789"


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
    tmp_path = tmp_path_factory.mktemp("header_sort")
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
        "continuous": agg is not None,
        "table_calc": None,
    }


def make_sheet(engine: str, name: str, **shelves: Any) -> str:
    created = call(engine, "/api/edit/demo/worksheets", {"name": name})["worksheet"]
    body = {**created, "rows": shelves.get("rows", []), "columns": shelves.get("columns", [])}
    body["marks"] = {
        **created["marks"],
        "label": shelves.get("label", []),
        "color": shelves.get("color"),
    }
    body["sorts"] = shelves.get("sorts", [])
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, method="PUT")
    return str(created["id"])


def sorts(engine: str, sheet: str) -> list[dict[str, Any]]:
    return call(engine, f"/api/edit/demo/worksheets/{sheet}")["worksheet"]["sorts"]


SVG = ".editor .chart > svg, .editor .chart > .frozen-host > svg"

# 檢視上的文字（依位置由上而下、由左而右）：座標軸標籤、多層標題或圖例的標籤
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

ROW_LABELS = "g.role-axis-label > text"
ROW_HEADERS = 'g[class*="__deco__header1_marks"] > text'
LEGEND = "g.role-legend-label > text"


def open_editor(page: Any, engine: str, sheet: str) -> None:
    page.goto(f"{engine}/w/demo/edit/{sheet}")
    settle(page)


def settle(page: Any) -> None:
    page.wait_for_selector('.editor[data-saving="false"]')
    page.wait_for_selector(".editor .chart[data-drawn='true'] svg", timeout=30000)
    page.wait_for_timeout(300)


def drag(page: Any, selector: str, member: str, target: str, side: str, axis: str) -> list[Any]:
    """把 member 拖到 target 之前（before）或之後（after）；回傳拖曳中標示放下位置的線（left、top、寬、高）。"""
    start = page.evaluate(BOX, [selector, member])
    goal = page.evaluate(BOX, [selector, target])
    if axis == "y":
        end = (
            start["x"],
            goal["y"] + (goal["height"] / 2 + 2 if side == "after" else -goal["height"] / 2 - 2),
        )
    else:
        end = (
            goal["x"] + (goal["width"] / 2 + 2 if side == "after" else -goal["width"] / 2 - 2),
            start["y"],
        )
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


def test_drag_row_headers_sets_a_manual_sort(engine: str) -> None:
    """長條圖的列標題（Sub-Category）：把 Art 拖到 Chairs 之後——拖曳時在 Chairs 下緣有一條橫跨整張圖的線，
    放開後 Sub-Category 的排序是手動、Art 排在 Chairs 之後，檢視跟著重畫；Ctrl+Z 回到原本（沒有排序）。
    只是點一下標題不改排序。"""
    from playwright.sync_api import sync_playwright

    sheet = make_sheet(
        engine, "drag bars", rows=[use("f_sub_category")], columns=[use("f_sales", "sum")]
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        open_editor(page, engine, sheet)
        before = [t for t in page.evaluate(TEXTS, [ROW_LABELS, "y"]) if t[0].isalpha()]
        assert before[:6] == ["Accessories", "Appliances", "Art", "Binders", "Bookcases", "Chairs"]

        # 點一下（沒有移動）：不改排序
        label = page.evaluate(BOX, [ROW_LABELS, "Art"])
        page.mouse.click(label["x"], label["y"])
        page.wait_for_timeout(300)
        assert sorts(engine, sheet) == []

        line = drag(page, ROW_LABELS, "Art", "Chairs", "after", "y")
        assert len(line) == 1 and line[0][2] > 300 and line[0][3] <= 4  # 橫的粗線
        expect_order = [
            "Accessories",
            "Appliances",
            "Binders",
            "Bookcases",
            "Chairs",
            "Art",
            "Copiers",
        ]
        (saved,) = sorts(engine, sheet)
        assert saved["field"] == "f_sub_category" and saved["kind"] == "manual"
        assert saved["manual_values"][:7] == expect_order
        assert len(saved["manual_values"]) == 17
        after = [t for t in page.evaluate(TEXTS, [ROW_LABELS, "y"]) if t[0].isalpha()]
        assert after[:7] == expect_order
        assert page.locator(".header-drop-line").count() == 0  # 放開之後線就拿掉

        # 與其他編輯相同可以復原
        page.locator(".editor-canvas").click(position={"x": 5, "y": 5})
        page.keyboard.press("Control+z")
        settle(page)
        assert sorts(engine, sheet) == []
        restored = [t for t in page.evaluate(TEXTS, [ROW_LABELS, "y"]) if t[0].isalpha()]
        assert restored[:6] == before[:6]
        assert errors == []
        browser.close()


def test_drag_nested_header_replaces_the_existing_sort(engine: str) -> None:
    """文字表的列是 Category、Sub-Category，Sub-Category 原本依 SUM(Sales) 遞減排序：在 Furniture 那一組裡把
    Furnishings 拖到 Chairs 之前——Sub-Category 的排序換成手動，順序是原本畫面上的順序（各組由上而下）、
    Furnishings 移到 Chairs 之前；其他組的順序不變。"""
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
        "drag nested",
        rows=[use("f_category"), use("f_sub_category")],
        label=[use("f_sales", "sum")],
        sorts=[by_sales],
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        open_editor(page, engine, sheet)
        before = page.evaluate(TEXTS, [ROW_HEADERS, "y"])
        assert before[:4] == ["Chairs", "Tables", "Bookcases", "Furnishings"]
        drag(page, ROW_HEADERS, "Furnishings", "Chairs", "before", "y")
        (saved,) = sorts(engine, sheet)
        assert saved["kind"] == "manual" and saved["by"] is None and saved["nested"] is False
        assert saved["manual_values"] == ["Furnishings", *[m for m in before if m != "Furnishings"]]
        after = page.evaluate(TEXTS, [ROW_HEADERS, "y"])
        assert after[:4] == ["Furnishings", "Chairs", "Tables", "Bookcases"]
        assert after[4:] == before[4:]
        assert errors == []
        browser.close()


def test_drag_column_header_and_legend_item(engine: str) -> None:
    """欄標題：交叉表的 Region 把 West 拖到 Central 之前，欄的順序變成 West、Central、East、South（直的線）。
    圖例：列與色彩都是 Region 的長條圖，在色彩圖例上把 South 拖到最上面（Central 之前）——Region 的排序改成
    手動、South 在最前面，列（長條）的順序跟著改。"""
    from playwright.sync_api import sync_playwright

    crosstab = make_sheet(
        engine,
        "drag columns",
        rows=[use("f_category")],
        columns=[use("f_region")],
        label=[use("f_sales", "sum")],
    )
    legend = make_sheet(
        engine,
        "drag legend",
        rows=[use("f_region")],
        columns=[use("f_sales", "sum")],
        color=use("f_region"),
    )
    regions = ["Central", "East", "South", "West"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        open_editor(page, engine, crosstab)
        columns = "g.role-axis-label > text"
        assert [t for t in page.evaluate(TEXTS, [columns, "x"]) if t in regions] == regions
        line = drag(page, columns, "West", "Central", "before", "x")
        assert len(line) == 1 and line[0][2] <= 4 and line[0][3] > 20  # 直的粗線
        (saved,) = sorts(engine, crosstab)
        assert (saved["field"], saved["kind"], saved["manual_values"]) == (
            "f_region",
            "manual",
            ["West", "Central", "East", "South"],
        )
        assert [t for t in page.evaluate(TEXTS, [columns, "x"]) if t in regions] == [
            "West",
            "Central",
            "East",
            "South",
        ]

        open_editor(page, engine, legend)
        assert page.evaluate(TEXTS, [LEGEND, "y"]) == regions
        drag(page, LEGEND, "South", "Central", "before", "y")
        (saved,) = sorts(engine, legend)
        assert (saved["field"], saved["kind"], saved["manual_values"]) == (
            "f_region",
            "manual",
            ["South", "Central", "East", "West"],
        )
        rows = [t for t in page.evaluate(TEXTS, [ROW_LABELS, "y"]) if t in regions]
        assert rows == ["South", "Central", "East", "West"]
        assert errors == []
        browser.close()
