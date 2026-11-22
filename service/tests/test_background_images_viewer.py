"""編輯器的端到端：「地圖＞背景圖片…」新增一張背景圖片（選擇圖片檔、X／Y 欄位與左右、上下的值、淡化），
圖片畫在散布圖的標記之下、對齊座標軸的範圍；取消「在這張工作表上顯示」之後不畫；檢視器照樣畫出。
需要建置好的檢視器（web/dist，可以用 TMIG_WEB_DIST 指定另一份）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

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
from editor_ui import menubar

from tmig_service.background import free_port
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("TMIG_WEB_DIST") or ROOT / "web" / "dist")
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "background-e2e-token-0123456789"


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


def test_background_image_dialog_and_view(engine: str, tmp_path: Path) -> None:
    from playwright.sync_api import Page, expect, sync_playwright

    created = call(engine, "/api/edit/demo/worksheets", {"name": "Floor"})["worksheet"]
    body = {
        **created,
        "rows": [use("f_profit", agg="sum", continuous=True)],
        "columns": [use("f_sales", agg="sum", continuous=True)],
        "marks": {**created["marks"], "type": "circle", "detail": [use("f_region")]},
    }
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, "PUT")
    picture = tmp_path / "floor.png"
    picture.write_bytes(png(40, 20, (255, 0, 0, 255)))

    def settle(page: Page) -> None:
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector('.editor .worksheet[data-loading="false"]', timeout=30000)

    def drawn(page: Page, scope: str) -> list[dict[str, Any]]:
        """畫出的背景圖片（Vega 的 image 標記）與圖的繪圖區。"""
        return page.evaluate(
            """(scope) => [...document.querySelectorAll(`${scope} svg g.mark-image image`)].map((node) => ({
                 href: (node.getAttribute('href') || node.getAttribute('xlink:href') || '').slice(0, 22),
                 width: Number(node.getAttribute('width')),
                 height: Number(node.getAttribute('height')),
                 opacity: node.getAttribute('opacity') ?? node.parentElement?.getAttribute('opacity'),
               }))""",
            scope,
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{created['id']}")
        settle(page)
        assert drawn(page, ".editor .worksheet") == []
        menubar(page, "地圖", "背景圖片…")
        dialog = page.locator(".background-images-dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="新增圖片…").click()
        form = page.locator(".background-image-form")
        form.get_by_label("背景圖片的名稱").fill("樓層")
        form.get_by_label("背景圖片的檔案").set_input_files(str(picture))
        form.get_by_label("X 欄位").select_option(label="Sales")
        form.get_by_label("左").fill("0")
        form.get_by_label("右").fill("100000")
        form.get_by_label("Y 欄位").select_option(label="Profit")
        form.get_by_label("上").fill("40000")
        form.get_by_label("下").fill("-10000")
        form.get_by_label("淡化").fill("40")
        form.get_by_label("鎖定外觀比例").uncheck()
        form.get_by_role("button", name="確定").click()
        expect(dialog.locator("[data-image='樓層']")).to_be_visible()
        dialog.get_by_role("button", name="確定").click()
        page.wait_for_timeout(500)
        settle(page)

        catalog = call(engine, "/api/edit/demo/catalog")["datasources"][0]
        (image,) = catalog["background_images"]
        assert (image["name"], image["x"], image["y"], image["washout"], image["lock_aspect"]) == (
            "樓層",
            "f_sales",
            "f_profit",
            0.4,
            False,
        )
        assert image["image"] == "Image/floor.png"
        page.wait_for_function(
            "() => document.querySelectorAll('.editor .worksheet svg g.mark-image image').length > 0",
            timeout=30000,
        )
        (shown,) = drawn(page, ".editor .worksheet")
        assert shown["href"].startswith("data:image/png")
        assert shown["width"] > 50 and shown["height"] > 50
        assert shown["opacity"] in ("0.6", 0.6)
        # 背景圖片在標記之下：image 的群組在圓點的前面
        order = page.evaluate(
            """() => [...document.querySelectorAll('.editor .worksheet svg g.mark-image, .editor .worksheet svg g.mark-symbol')]
                 .map((g) => g.classList.contains('mark-image') ? 'image' : 'marks')"""
        )
        assert order[0] == "image" and "marks" in order, order

        # 這張工作表停用：不畫
        menubar(page, "地圖", "背景圖片…")
        dialog = page.locator(".background-images-dialog")
        dialog.get_by_label("在這張工作表上顯示「樓層」").uncheck()
        dialog.get_by_role("button", name="確定").click()
        page.wait_for_timeout(500)
        settle(page)
        sheet = call(engine, f"/api/edit/demo/worksheets/{created['id']}")["worksheet"]
        assert sheet["background_images_off"] == [image["id"]]
        page.wait_for_function(
            "() => document.querySelectorAll('.editor .worksheet svg g.mark-image image').length === 0",
            timeout=30000,
        )

        # 檢視器：再啟用之後照樣畫出
        sheet["background_images_off"] = []
        call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": sheet}, "PUT")
        page.goto(f"{engine}/w/demo/s/{created['id']}")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]', timeout=30000)
        page.wait_for_function(
            "() => document.querySelectorAll('.single-sheet svg g.mark-image image').length > 0",
            timeout=30000,
        )
        assert drawn(page, ".single-sheet")[0]["href"].startswith("data:image/png")
        assert errors == []
        browser.close()
