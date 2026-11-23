"""檢視器的端到端：顯示集合中的成員（Show Members in Set）——集合直接放在列架上（沒有 IN/OUT 的欄位實例）時
列出集合的成員（銷售最高的兩個地區），列的欄位標籤是集合的名稱，不是 In／Out 兩列。

報表以示範報表加上一個 Top 2 地區的集合與一張工作表（寫法與 GitHub 上公開的 .twb 相同：列架上是
``[資料源].[集合]``，篩選架上有自動加上的 ``<filter class='categorical' column='[資料源].[集合]' />``）。
需要建置好的檢視器（web/dist，可以用 TMIG_WEB_DIST 指定另一份）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import os
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
DIST = Path(os.environ.get("TMIG_WEB_DIST") or ROOT / "web" / "dist")
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "set-members-e2e-token-0123456789"
DS = "federated.0s2xs1a1g5wq1h17oo8mq0b6k5ry"
TOP = (
    "<group caption='Top 2 Regions' name='[Top 2 Regions]' name-style='unqualified' "
    "user:ui-builder='filter-group'>"
    "<groupfilter count='2' end='top' function='end' units='records' user:ui-marker='end'>"
    "<groupfilter direction='DESC' expression='SUM([Sales])' function='order' user:ui-marker='order'>"
    "<groupfilter function='level-members' level='[Region]' user:ui-enumeration='all' />"
    "</groupfilter></groupfilter></group>"
)
SHEET = f"""
    <worksheet name='Top Regions'>
      <table>
        <view>
          <datasources><datasource caption='Orders' name='{DS}' /></datasources>
          <datasource-dependencies datasource='{DS}'>
            <column datatype='string' name='[Region]' role='dimension' type='nominal' />
            <column datatype='real' name='[Sales]' role='measure' type='quantitative' />
            <column-instance column='[Sales]' derivation='Sum' name='[sum:Sales:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
          <filter class='categorical' column='[{DS}].[Top 2 Regions]' />
          <slices><column>[{DS}].[Top 2 Regions]</column></slices>
          <aggregation value='true' />
        </view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Bar' /></pane></panes>
        <rows>[{DS}].[Top 2 Regions]</rows>
        <cols>[{DS}].[sum:Sales:qk]</cols>
      </table>
    </worksheet>
"""


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


def with_set_on_rows(target: Path) -> None:
    """示範報表加上集合與以成員顯示集合的工作表（其他內容不變）。"""
    with zipfile.ZipFile(DEMO) as source, zipfile.ZipFile(target, "w") as out:
        for item in source.infolist():
            data = source.read(item)
            if item.filename.endswith(".twb"):
                text = data.decode("utf-8")
                start = text.find("<datasource caption='Orders' inline='true'")
                end = text.find("</datasource>", start)
                text = text[:end] + TOP + text[end:]
                text = text.replace("</worksheets>", SHEET + "</worksheets>", 1)
                data = text.encode("utf-8")
            out.writestr(item, data)


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
    with_set_on_rows(source)
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


def test_set_on_rows_lists_its_members(engine: str) -> None:
    from playwright.sync_api import sync_playwright

    rendered = call(engine, "/api/workbooks/demo/worksheets/ws_top_regions/render", {})
    assert not rendered.get("unsupported"), rendered.get("unsupported")
    shown = [row[0] for row in rendered["table"]["rows"]]
    # Superstore 的銷售：West、East 最高（Central、South 不在集合中）
    assert sorted(shown) == ["East", "West"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/ws_top_regions")
        page.wait_for_selector('.single-sheet .worksheet[data-loading="false"]', timeout=30000)
        texts = page.evaluate(
            """() => [...document.querySelectorAll('.single-sheet svg text')].map((t) => t.textContent ?? '')"""
        )
        assert "East" in texts and "West" in texts, texts
        assert "Central" not in texts and "In" not in texts and "Out" not in texts, texts
        # 列的欄位標籤是集合的名稱
        assert "Top 2 Regions" in texts, texts
        assert errors == []
        browser.close()
