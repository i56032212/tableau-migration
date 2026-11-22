"""標記的動畫（Tableau 的「格式＞動畫」）的端到端：活頁簿預設或工作表開了動畫時，檢視器裡排除標記之後長條從原本
的位置移到新的位置、排除的長條淡出；沒有開的不動。桌面版編輯器的「格式＞動畫…」設定工作表與活頁簿預設（存進
報表），依序的動畫先淡出、再移動、最後淡入；「全部重設」改回預設。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import json
import shutil
import sys
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from editor_ui import menubar

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "mark-animation-e2e-token-0123456789"


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


@pytest.fixture()
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


def use(field: str, agg: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "agg": agg,
        "date": None,
        "continuous": agg is not None,
        "table_calc": None,
    }


def bar_chart(engine: str, name: str, animation: dict[str, Any] | None = None) -> str:
    """Sub-Category 依 SUM(Sales) 遞減排序的長條圖。"""
    created = call(engine, "/api/edit/demo/worksheets", {"name": name})["worksheet"]
    body = {**created, "rows": [use("f_sub_category")], "columns": [use("f_sales", "sum")]}
    body["sorts"] = [
        {
            "field": "f_sub_category",
            "kind": "by_field",
            "order": "desc",
            "by": use("f_sales", "sum"),
            "nested": False,
            "manual_values": [],
        }
    ]
    if animation is not None:
        body["animation"] = animation
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, method="PUT")
    return str(created["id"])


# 長條（有資料列的 rect 標記）：Sub-Category 的名稱 → 外框的上緣
BARS = """() => {
  const out = {};
  document.querySelectorAll('.chart > svg g.mark-rect.role-mark > path, .chart > .frozen-host > svg g.mark-rect.role-mark > path').forEach((p) => {
    const d = p.__data__ && p.__data__.datum;
    if (!d || typeof d.__row !== 'number') return;
    const name = Object.entries(d).find(([k]) => k.endsWith('_label') && typeof d[k] === 'string' && /^[A-Z][a-z]/.test(d[k]));
    if (name) out[name[1]] = p.getBoundingClientRect().top;
  });
  return out;
}"""

# 進行中的標記的動畫：暫停在 fraction 的位置（依序時 phase 是第幾段）
PAUSE = """([fraction]) => {
  const list = document.getAnimations();
  list.forEach((a) => { a.pause(); const t = a.effect.getTiming(); a.currentTime = Number(t.delay || 0) + Number(t.duration) * fraction; });
  return list.map((a) => ({ delay: a.effect.getTiming().delay, duration: a.effect.getTiming().duration, target: a.effect.target ? a.effect.target.tagName : null, ghost: Boolean(a.effect.target && a.effect.target.classList && a.effect.target.classList.contains('mark-ghosts')) }));
}"""


def exclude_top_bar(page: Any) -> dict[str, float]:
    """點最上面的長條（Phones）、按「✕ 排除」；回傳排除之前各長條的位置。"""
    before = page.evaluate(BARS)
    top = min(before, key=lambda k: before[k])
    assert top == "Phones"
    box = page.evaluate(
        """(name) => {
          const p = [...document.querySelectorAll('.chart svg g.mark-rect.role-mark > path')].find((e) => e.__data__ && e.__data__.datum && Object.values(e.__data__.datum).includes(name));
          const r = p.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2];
        }""",
        top,
    )
    page.mouse.click(box[0], box[1])
    pinned = page.locator(".viz-tooltip.pinned")
    pinned.wait_for()
    pinned.get_by_role("button", name="✕ 排除").click()
    return before


def test_viewer_animates_marks_when_the_workbook_turns_them_on(engine: str) -> None:
    """活頁簿預設開了動畫（1 秒、同時）：檢視器裡排除最上面的 Phones 之後，Chairs 從第二列移到第一列——動畫一半時在
    兩者中間——Phones 的長條以原本的樣子淡出，動畫結束之後在新的位置。另一張工作表自己關掉動畫：排除之後直接是
    新的位置，沒有動畫。"""
    from playwright.sync_api import sync_playwright

    saved = call(
        engine,
        "/api/edit/demo/animation",
        {"animation": {"on": True, "duration": 1, "style": "simultaneous"}},
        method="PUT",
    )
    assert saved["animation"] == {"on": True, "duration": 1.0, "style": "simultaneous"}
    assert call(engine, "/api/edit/demo/animation")["animation"]["on"] is True
    animated = bar_chart(engine, "animated bars")
    still = bar_chart(engine, "still bars", {"on": False})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")

        page.goto(f"{engine}/w/demo/s/{animated}")
        page.wait_for_selector(".single-sheet .chart[data-drawn='true']")
        page.wait_for_timeout(300)
        assert page.evaluate("document.getAnimations().length") == 0  # 第一次畫不動
        before = exclude_top_bar(page)
        page.wait_for_selector(".single-sheet .chart[data-animating='true']", timeout=10000)
        timing = page.evaluate(PAUSE, [0.5])
        assert any(t["ghost"] for t in timing)  # 排除的長條淡出
        assert any(
            t["target"] == "path" and t["delay"] == 0 and t["duration"] == 1000 for t in timing
        )
        middle = page.evaluate(BARS)
        assert "Phones" not in middle
        final_top = before["Phones"]
        assert final_top + 2 < middle["Chairs"] < before["Chairs"] - 2  # 在兩列的中間
        page.evaluate("document.getAnimations().forEach((a) => a.finish())")
        page.wait_for_function("!document.querySelector('.single-sheet .chart[data-animating]')")
        assert page.locator(".mark-ghosts").count() == 0
        after = page.evaluate(BARS)
        assert abs(after["Chairs"] - final_top) < 2

        page.goto(f"{engine}/w/demo/s/{still}")
        page.wait_for_selector(".single-sheet .chart[data-drawn='true']")
        page.wait_for_timeout(300)
        exclude_top_bar(page)
        page.wait_for_function(
            "(() => { const c = document.querySelector('.single-sheet .chart'); return c.dataset.drawn === 'true' && Number(c.dataset.draws) >= 2; })()"
        )
        assert page.evaluate("document.getAnimations().length") == 0
        assert page.locator(".single-sheet .chart[data-animating]").count() == 0
        assert errors == []
        browser.close()


def test_editor_format_animations(engine: str) -> None:
    """編輯器的「格式＞動畫…」：選取的工作表設為開、最慢（1 秒）、依序——存進工作表；在檢視上把列標題 Chairs 拖到
    最上面之後標記依序移動（移動的那一段在淡出之後開始）。活頁簿預設設為開之後存進報表；「全部重設」之後工作表與
    活頁簿預設都回到預設（關）。"""
    from playwright.sync_api import sync_playwright

    sheet = bar_chart(engine, "editor bars")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/edit/{sheet}")
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_selector(".editor .chart[data-drawn='true']", timeout=30000)

        menubar(page, "格式", "動畫…")
        dialog = page.locator(".animation-dialog")
        dialog.get_by_label("工作表的動畫").select_option("on")
        dialog.get_by_label("工作表的持續時間").select_option("1")
        dialog.get_by_label("工作表的樣式").select_option("sequential")
        dialog.get_by_role("button", name="確定").click()
        page.wait_for_selector('.editor[data-saving="false"]')
        page.wait_for_timeout(300)
        stored = call(engine, f"/api/edit/demo/worksheets/{sheet}")["worksheet"]
        assert stored["animation"] == {"on": True, "duration": 1.0, "style": "sequential"}
        assert call(engine, "/api/edit/demo/animation")["animation"] is None  # 活頁簿預設沒有改

        # 拖曳列標題 Chairs 到最上面（Phones 之前）：排序改成手動，標記依序移動
        page.wait_for_selector(".editor .chart[data-drawn='true']")
        boxes = page.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('.editor .chart svg g.role-axis-label > text')]
                 .map((t) => { const r = t.getBoundingClientRect(); return [t.textContent.trim(), [r.left + r.width / 2, r.top + r.height / 2, r.height]]; }))"""
        )
        start, goal = boxes["Chairs"], boxes["Phones"]
        page.mouse.move(start[0], start[1])
        page.mouse.down()
        page.mouse.move(start[0], (start[1] + goal[1]) / 2, steps=4)
        page.mouse.move(start[0], goal[1] - goal[2] / 2 - 2, steps=4)
        page.mouse.up()
        page.wait_for_selector(".editor .chart[data-animating='true']", timeout=15000)
        timing = page.evaluate(PAUSE, [0.5])
        moving = [t for t in timing if t["target"] == "path" and not t["ghost"]]
        assert moving and all(t["delay"] == 1000 for t in moving)  # 依序：移動在第二段
        page.evaluate("document.getAnimations().forEach((a) => a.finish())")
        page.wait_for_function("!document.querySelector('.editor .chart[data-animating]')")

        # 活頁簿預設：開
        menubar(page, "格式", "動畫…")
        dialog.get_by_label("活頁簿預設的動畫").select_option("on")
        dialog.get_by_role("button", name="確定").click()
        page.wait_for_function("() => !document.querySelector('.animation-dialog')")
        deadline = time.monotonic() + 10
        while call(engine, "/api/edit/demo/animation")["animation"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.2)
        assert call(engine, "/api/edit/demo/animation")["animation"]["on"] is True

        # 全部重設：工作表與活頁簿預設都回到預設
        menubar(page, "格式", "動畫…")
        dialog.get_by_role("button", name="全部重設").click()
        deadline = time.monotonic() + 10
        while (
            call(engine, f"/api/edit/demo/worksheets/{sheet}")["worksheet"].get("animation")
            is not None
            or call(engine, "/api/edit/demo/animation")["animation"] is not None
        ):
            assert time.monotonic() < deadline
            time.sleep(0.2)
        assert errors == []
        browser.close()
