"""空間資料的地圖（檢視器與桌面版編輯器）：Shapefile 等空間檔案的形狀、MAKEPOINT 等計算欄位畫成地圖；
編輯器中連按兩下空間欄位與 Tableau 相同建立地圖（COLLECT 為標記的形狀，Latitude／Longitude (generated)
放到欄列架）。需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from tmig_service.background import free_port, running_server
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
sys.path.insert(0, str(ROOT / "kernel"))
from tests.data.spatial_book import write_spatial_twbx  # noqa: E402  # pyright: ignore[reportMissingImports]

TOKEN = "spatial-e2e-token-0123456789"


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


@pytest.fixture(scope="module")
def book(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("tableauhyperapi")
    return write_spatial_twbx(tmp_path_factory.mktemp("spatial-book"))


def test_spatial_maps_in_viewer(book: Path, tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    fixtures = tmp_path / "fixtures"
    (fixtures / "spatial").mkdir(parents=True)
    (fixtures / "spatial" / "spatial.twbx").write_bytes(book.read_bytes())
    with running_server(fixtures, DIST) as url:
        ir = json.loads(urllib.request.urlopen(f"{url}/api/workbooks/spatial/ir").read())
        ids = {w["name"]: w["id"] for w in ir["worksheets"]}
        request = urllib.request.Request(
            f"{url}/api/workbooks/spatial/worksheets/{ids['County Map']}/render",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        body = json.loads(urllib.request.urlopen(request).read())
        # 資料表不列出形狀（WKT），規格中也沒有
        assert body["table"]["headers"] == ["Name", "Population"]
        assert "POLYGON((" not in json.dumps(body)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1000, "height": 760})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            expected = {"County Map": 21, "Store Map": 4, "Buffers": 5, "Routes": 4}
            for name, marks in expected.items():
                page.goto(f"{url}/w/spatial/s/{ids[name]}")
                page.wait_for_selector('.worksheet [data-drawn="true"]', timeout=60000)
                # 資料的圖層（底圖以外）：多邊形、點與線各自的 SVG 元素
                count = page.evaluate(
                    """() => [...document.querySelectorAll('.worksheet svg g.mark-shape, .worksheet svg g.mark-symbol')]
                        .filter((g) => !(g.getAttribute('class') || '').includes('__deco'))
                        .map((g) => g.querySelectorAll('path').length)
                        .reduce((a, b) => a + b, 0)"""
                )
                assert count >= marks, (name, count)
            assert errors == []
            browser.close()


@pytest.fixture
def engine(book: Path, tmp_path: Path) -> Iterator[str]:
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
    source = tmp_path / "spatial.twbx"
    source.write_bytes(book.read_bytes())
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


def test_double_click_geometry_builds_map(engine: str) -> None:
    from playwright.sync_api import sync_playwright

    created = call(
        engine, "/api/edit/spatial/worksheets", {"name": "Shapes", "datasource": "ds_counties"}
    )["worksheet"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/spatial/edit/{created['id']}")
        page.wait_for_selector('.editor[data-saving="false"]')
        geometry = page.locator('.data-pane li[data-field="f_geometry"]')
        assert "🗺" in geometry.inner_text()
        geometry.dblclick()
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)
        saved = call(engine, f"/api/edit/spatial/worksheets/{created['id']}")["worksheet"]
        assert [u["field"] for u in saved["rows"]] == ["latitude_generated"]
        assert [u["field"] for u in saved["columns"]] == ["longitude_generated"]
        assert saved["marks"]["geometry"]["field"] == "f_geometry"
        assert saved["marks"]["geometry"]["agg"] == "collect"
        assert saved["marks"]["detail"] == []
        # 標記卡的「空間」格顯示 COLLECT(Geometry)
        pill = page.locator('[data-drop="marks.geometry"] .pill-text')
        assert "COLLECT(Geometry)" in pill.inner_text()
        page.locator('.data-pane li[data-field="f_name"]').drag_to(
            page.locator('[data-drop="marks.detail"]')
        )
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_function(
            "document.querySelectorAll('.editor .worksheet svg g.mark-shape path').length >= 21",
            timeout=30000,
        )
        assert errors == []
        browser.close()


BOX_JS = """() => {
  const paths = [...document.querySelectorAll('.worksheet svg g.mark-shape')]
    .filter((g) => !(g.getAttribute('class') || '').includes('__deco'))
    .flatMap((g) => [...g.querySelectorAll('path')]);
  const boxes = paths.map((p) => p.getBoundingClientRect()).filter((r) => r.width > 0);
  const widths = boxes.map((r) => r.width).sort((a, b) => a - b);
  // 中位數的寬度（放大後部分形狀被裁到畫面之外，不能以全部的範圍比較）與最左邊形狀的位置
  return { width: widths[Math.floor(widths.length / 2)], left: Math.min(...boxes.map((r) => r.left)) };
}"""


def test_map_pan_and_zoom(book: Path, tmp_path: Path) -> None:
    """地圖的平移、縮放（檢視器）：滾輪以游標為中心放大、拖曳平移、工具列的「重設地圖」；停止操作後以畫面中的
    範圍重新繪製（查詢狀態帶著 viewport），拖曳後放開滑鼠不會選取標記。"""
    from playwright.sync_api import sync_playwright

    fixtures = tmp_path / "fixtures"
    (fixtures / "spatial").mkdir(parents=True)
    (fixtures / "spatial" / "spatial.twbx").write_bytes(book.read_bytes())
    with running_server(fixtures, DIST) as url:
        ir = json.loads(urllib.request.urlopen(f"{url}/api/workbooks/spatial/ir").read())
        ids = {w["name"]: w["id"] for w in ir["worksheets"]}
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1000, "height": 760})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            bodies: list[dict[str, Any]] = []
            page.on(
                "request",
                lambda r: (
                    bodies.append(json.loads(r.post_data or "{}"))
                    if r.url.endswith("/render") and r.method == "POST"
                    else None
                ),
            )
            page.goto(f"{url}/w/spatial/s/{ids['County Map']}")
            page.wait_for_selector('.worksheet [data-drawn="true"]', timeout=60000)
            page.wait_for_selector('.chart[data-zoomable="true"]')
            toolbar = page.get_by_role("toolbar", name="地圖工具列")
            assert toolbar.get_by_role("button", name="放大").is_visible()
            before = page.evaluate(BOX_JS)
            chart = page.locator(".chart svg").bounding_box()
            assert chart is not None
            cx, cy = chart["x"] + chart["width"] / 2, chart["y"] + chart["height"] / 2
            # 滾輪放大：形狀立即變大；停止後以新的範圍重新查詢、繪製
            count = len(bodies)
            page.mouse.move(cx, cy)
            page.mouse.wheel(0, -500)
            page.wait_for_function(f"({BOX_JS})().width > {before['width'] * 1.5}", timeout=10000)
            page.wait_for_function(
                f"document.querySelector('.worksheet').dataset.loading === 'false' && {count} < 99",
                timeout=10000,
            )
            page.wait_for_timeout(1200)
            page.wait_for_selector('.worksheet [data-drawn="true"]', timeout=60000)
            zoomed = [b for b in bodies[count:] if b.get("viewport")]
            assert zoomed, bodies[count:]
            box = zoomed[-1]["viewport"]
            assert box["east"] > box["west"] and box["north"] > box["south"]
            after = page.evaluate(BOX_JS)
            assert after["width"] > before["width"] * 1.5
            # 拖曳平移：形狀跟著往右移（最左邊的形狀可能是另一個，只比較方向與距離的下限）
            page.mouse.move(cx, cy)
            page.mouse.down()
            page.mouse.move(cx + 120, cy, steps=6)
            page.mouse.up()
            page.wait_for_function(f"({BOX_JS})().left > {after['left']} + 60", timeout=10000)
            # 拖曳後放開不是點選（沒有選取的標記）
            assert page.locator(".worksheet-meta .selection").count() == 0
            page.wait_for_timeout(1200)
            page.wait_for_selector('.worksheet [data-drawn="true"]', timeout=60000)
            # 重設地圖：回到依標記自動縮放的範圍
            count = len(bodies)
            toolbar.get_by_role("button", name="重設地圖").click()
            page.wait_for_function(
                f"Math.abs(({BOX_JS})().width - {before['width']}) < 1", timeout=20000
            )
            assert any(not b.get("viewport") for b in bodies[count:])
            assert errors == []
            browser.close()


def test_editor_saves_map_viewport(engine: str) -> None:
    """編輯器中平移、縮放地圖：與 Tableau Desktop 相同存進工作表（map.viewport），「重設地圖」改回自動縮放。"""
    from playwright.sync_api import sync_playwright

    created = call(
        engine, "/api/edit/spatial/worksheets", {"name": "Zoom", "datasource": "ds_counties"}
    )["worksheet"]
    sheet = call(engine, f"/api/edit/spatial/worksheets/{created['id']}")["worksheet"]
    sheet.update(
        rows=[{"field": "latitude_generated"}],
        columns=[{"field": "longitude_generated"}],
    )
    sheet["marks"].update(type="automatic", geometry={"field": "f_geometry", "agg": "collect"})
    request = urllib.request.Request(
        f"{engine}/api/edit/spatial/worksheets/{created['id']}",
        data=json.dumps({"worksheet": sheet}).encode(),
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(request, timeout=60).read()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/spatial/edit/{created['id']}")
        page.wait_for_selector('.editor .chart[data-zoomable="true"]', timeout=30000)
        chart = page.locator(".editor .chart svg").bounding_box()
        assert chart is not None
        page.mouse.move(chart["x"] + chart["width"] / 2, chart["y"] + chart["height"] / 2)
        page.mouse.wheel(0, -400)

        def saved() -> Any:
            return call(engine, f"/api/edit/spatial/worksheets/{created['id']}")["worksheet"]

        deadline = time.monotonic() + 20
        while (saved().get("map") or {}).get("viewport") is None:
            assert time.monotonic() < deadline, "平移、縮放後沒有存進工作表"
            time.sleep(0.2)
        viewport = saved()["map"]["viewport"]
        assert viewport["east"] - viewport["west"] < 10
        page.wait_for_selector('.editor[data-saving="false"]')
        page.get_by_role("toolbar", name="地圖工具列").get_by_role(
            "button", name="重設地圖"
        ).click()
        deadline = time.monotonic() + 20
        while (saved().get("map") or {}).get("viewport") is not None:
            assert time.monotonic() < deadline, "重設地圖後沒有改回自動縮放"
            time.sleep(0.2)
        assert errors == []
        browser.close()
