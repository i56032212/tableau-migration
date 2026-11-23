"""檢視器的端到端：背景圖片畫在分格（列架上的 Region 把散布圖分成窗格）的每個窗格、同一架上兩個量值時
只畫在 Y 欄位的座標軸，以及背景地圖選「無」的地圖上（四角是經緯度）。比例尺的名稱依 Vega-Lite 為各窗格
產生的名稱（tmig.render.background.resolve_scales），名稱不對時 Vega 會丟出錯誤、整張圖畫不出來。
需要建置好的檢視器（web/dist，可以用 TMIG_WEB_DIST 指定另一份）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import threading
import time
import urllib.request
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("TMIG_WEB_DIST") or ROOT / "web" / "dist")
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "background-panes-e2e-token-0123456789"


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


def call(base: str, path: str, body: dict[str, Any] | None = None, method: str = "") -> Any:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """單色的 PNG 圖片。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgba) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def use(field: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": field,
        "agg": None,
        "date": None,
        "continuous": False,
        "table_calc": None,
        **extra,
    }


def sheet(engine: str, name: str, **update: Any) -> str:
    created = call(engine, "/api/edit/demo/worksheets", {"name": name})["worksheet"]
    marks = {**created["marks"], **update.pop("marks", {})}
    body = {**created, **update, "marks": marks}
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, "PUT")
    return created["id"]


def images(page: Any) -> list[dict[str, Any]]:
    """畫出的背景圖片（Vega 的 image 標記）。"""
    return page.evaluate(
        """() => [...document.querySelectorAll('.single-sheet svg g.mark-image image')].map((node) => ({
             href: (node.getAttribute('href') || node.getAttribute('xlink:href') || '').slice(0, 22),
             width: Number(node.getAttribute('width')),
             height: Number(node.getAttribute('height')),
           }))"""
    )


def test_background_images_on_panes_axes_and_a_map(engine: str) -> None:
    from playwright.sync_api import sync_playwright

    path = call(
        engine,
        "/api/edit/demo/images",
        {"name": "floor.png", "content": base64.b64encode(png(40, 20, (255, 0, 0, 255))).decode()},
    )["path"]
    profit = use("f_profit", agg="sum", continuous=True)
    sales = use("f_sales", agg="sum", continuous=True)
    panes = sheet(
        engine,
        "Panes",
        rows=[use("f_region"), profit],
        columns=[sales],
        marks={"type": "circle", "detail": [use("f_state_province")]},
    )
    two = sheet(
        engine,
        "Two axes",
        rows=[profit, use("f_quantity", agg="sum", continuous=True)],
        columns=[sales],
        marks={"type": "circle", "detail": [use("f_state_province")]},
    )
    lat = use("latitude_generated", continuous=True)
    lon = use("longitude_generated", continuous=True)
    on_map = sheet(
        engine,
        "Plain map",
        rows=[lat],
        columns=[lon],
        marks={"type": "circle", "detail": [use("f_state_province")]},
        map={"basemap": False},
    )
    floor = {
        "name": "樓層",
        "image": path,
        "x": "f_sales",
        "left": 0,
        "right": 200000,
        "y": "f_profit",
        "top": 40000,
        "bottom": -20000,
        "lock_aspect": False,
    }
    usa = {
        "name": "美國",
        "image": path,
        "x": "longitude_generated",
        "left": -125,
        "right": -66,
        "y": "latitude_generated",
        "top": 50,
        "bottom": 24,
        "lock_aspect": False,
    }
    call(
        engine,
        "/api/edit/demo/datasources/ds_orders/background-images",
        {"images": [floor, usa]},
        "PUT",
    )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"{engine}/?token={TOKEN}")

        def show(sheet_id: str, count: int) -> list[dict[str, Any]]:
            page.goto(f"{engine}/w/demo/s/{sheet_id}")
            page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]', timeout=30000)
            page.wait_for_function(
                f"() => document.querySelectorAll('.single-sheet svg g.mark-image image').length >= {count}",
                timeout=30000,
            )
            return images(page)

        # 分格：四個地區各一個窗格，每個窗格各自一張圖片
        drawn = show(panes, 4)
        assert len(drawn) == 4
        assert all(d["href"].startswith("data:image/png") and d["width"] > 20 for d in drawn)
        # 兩個量值：只有 Profit 的座標軸那一張
        drawn = show(two, 1)
        assert len(drawn) == 1 and drawn[0]["height"] > 20
        # 背景地圖選「無」的地圖：圖片的四角是經緯度
        drawn = show(on_map, 1)
        assert len(drawn) == 1 and drawn[0]["width"] > 100
        assert errors == []
        browser.close()
