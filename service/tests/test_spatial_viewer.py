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
