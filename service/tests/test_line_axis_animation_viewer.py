"""折線、區域與座標軸的動畫（Tableau 的「格式＞動畫」）的端到端：檢視器裡改篩選之後，同一條線的形狀從原本的點
內插到新的點（路徑的 d）、連續座標軸上同一個值的刻度文字移到新的位置，比例尺之外的刻度移出去並淡出；區域圖依序
播放時消失的系列先淡出、之後形狀才改變。系統設定減少動態效果時不動。動畫在一半時暫停量位置（與
test_mark_animation_viewer.py 相同的做法）。

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
TOKEN = "line-axis-animation-e2e-token-0123456789"


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


def use(
    field: str, agg: str | None = None, date: dict[str, Any] | None = None, continuous: bool = False
) -> dict[str, Any]:
    return {
        "field": field,
        "agg": agg,
        "date": date,
        "continuous": continuous or agg is not None,
        "table_calc": None,
    }


def sheet_with_filter(engine: str, name: str, mark: str, filtered: str) -> str:
    """各區域每一季的 SUM(Sales)（mark：line 或 area，色彩是 Region），filtered 的篩選顯示成勾選清單。"""
    created = call(engine, "/api/edit/demo/worksheets", {"name": name})["worksheet"]
    body = {
        **created,
        "rows": [use("f_sales", "sum")],
        "columns": [use("f_order_date", None, {"kind": "trunc", "unit": "quarter"}, True)],
    }
    body["marks"] = {**created["marks"], "type": mark, "color": use("f_region")}
    body["filters"] = [
        {
            "kind": "categorical",
            "id": "flt_anim",
            "field": use(filtered),
            "stage": "dimension",
            "mode": "all",
            "values": [],
            "ranges": [],
            "group": None,
            "latest": False,
            "source_ref": "/editor",
        }
    ]
    body["controls"] = [
        {
            "kind": "filter",
            "field": use(filtered),
            "parameter": None,
            "control": "multi_list",
            "relevant": False,
        }
    ]
    call(engine, f"/api/edit/demo/worksheets/{created['id']}", {"worksheet": body}, method="PUT")
    return str(created["id"])


# 各系列（色彩的值）的線或區域：路徑目前的形狀（動畫中是內插的形狀）第一個點在畫面上的位置
SERIES = """(mark) => {
  const out = {};
  document.querySelectorAll(`.single-sheet .chart g.mark-${mark}.role-mark > path`).forEach((p) => {
    const it = p.__data__;
    const group = it && it.mark && it.mark.group && it.mark.group.datum;
    const name = group && Object.entries(group).find(([k]) => k.endsWith('_label'));
    if (!name) return;
    const nums = (getComputedStyle(p).d.match(/-?\\d+(\\.\\d+)?(e[-+]?\\d+)?/g) || []).map(Number);
    const pt = new DOMPoint(nums[0], nums[1]).matrixTransform(p.getScreenCTM());
    out[name[1]] = [pt.x, pt.y];
  });
  return out;
}"""

# 縱軸的刻度文字：文字 → 畫面上的位置（中心的 y）
LABELS = """() => {
  const out = {};
  document.querySelectorAll('.single-sheet .chart svg g.role-axis-label > text').forEach((t) => {
    const r = t.getBoundingClientRect();
    if (/^[\\d,]+$/.test(t.textContent.trim())) out[t.textContent.trim()] = r.top + r.height / 2;
  });
  return out;
}"""

# 進行中的動畫暫停在 at 毫秒；回傳各動畫的對象與時間
PAUSE_AT = """([at]) => {
  const list = document.getAnimations().filter((a) => a.effect && a.effect.target && a.effect.target.closest && a.effect.target.closest('.chart'));
  list.forEach((a) => { a.pause(); a.currentTime = at; });
  return list.map((a) => {
    const t = a.effect.target;
    const frames = a.effect.getKeyframes();
    return {
      tag: t.tagName,
      layer: (t.parentElement && t.parentElement.getAttribute('class')) || '',
      ghost: Boolean(t.closest('.mark-ghosts')),
      text: t.tagName === 'text' ? t.textContent : (t.querySelector && t.querySelector('text') ? t.querySelector('text').textContent : null),
      delay: a.effect.getTiming().delay,
      duration: a.effect.getTiming().duration,
      props: Object.keys(frames[0] || {}).filter((k) => !['offset', 'easing', 'composite', 'computedOffset'].includes(k)),
      opacity: Number(getComputedStyle(t).opacity),
    };
  });
}"""


def between(value: float, a: float, b: float, margin: float = 1.0) -> bool:
    return min(a, b) + margin < value < max(a, b) - margin


def test_lines_and_axis_move_to_the_filtered_values(engine: str) -> None:
    """折線（同時、1 秒）：取消勾選 Technology 之後，各區域的線的形狀從原本的點內插到新的點——一半時第一個點在原本與
    新的位置之間；縱軸的刻度文字 20,000 移到新的位置（一半時在兩者之間），新的比例尺之外的 90,000 移出去並淡出。
    結束之後沒有殘留。系統設定減少動態效果時再改篩選，線與座標軸都不動。"""
    from playwright.sync_api import sync_playwright

    call(
        engine,
        "/api/edit/demo/animation",
        {"animation": {"on": True, "duration": 1, "style": "simultaneous"}},
        method="PUT",
    )
    sheet = sheet_with_filter(engine, "animated lines", "line", "f_category")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/{sheet}")
        page.wait_for_selector(".single-sheet .chart[data-drawn='true']")
        page.wait_for_timeout(300)
        before_lines = page.evaluate(SERIES, "line")
        before_labels = page.evaluate(LABELS)
        assert set(before_lines) == {"Central", "East", "South", "West"}
        assert "90,000" in before_labels and "20,000" in before_labels

        page.locator(".sheet-controls").get_by_label("Technology").uncheck()
        page.wait_for_selector(".single-sheet .chart[data-animating='true']", timeout=10000)
        timing = page.evaluate(PAUSE_AT, [500])
        morphing = [t for t in timing if t["tag"] == "path" and "mark-line" in t["layer"]]
        assert len(morphing) == 4 and all(
            t["props"] == ["d"] and t["delay"] == 0 and t["duration"] == 1000 for t in morphing
        ), morphing
        middle_lines = page.evaluate(SERIES, "line")
        middle_labels = page.evaluate(LABELS)
        fading = [t for t in timing if t["ghost"] and t["text"] == "90,000"]
        assert fading and 0.05 < fading[0]["opacity"] < 0.95, fading

        page.evaluate("document.getAnimations().forEach((a) => a.finish())")
        page.wait_for_function("!document.querySelector('.single-sheet .chart[data-animating]')")
        assert page.locator(".mark-ghosts").count() == 0
        after_lines = page.evaluate(SERIES, "line")
        after_labels = page.evaluate(LABELS)
        assert "90,000" not in after_labels
        # 線：每一條的第一個點在一半時位於原本與新的位置之間（移動最多的那一條至少差 10 像素）
        moved = max(before_lines, key=lambda k: abs(before_lines[k][1] - after_lines[k][1]))
        assert abs(before_lines[moved][1] - after_lines[moved][1]) > 10
        for name in before_lines:
            low, high = sorted((before_lines[name][1], after_lines[name][1]))
            assert low - 0.5 <= middle_lines[name][1] <= high + 0.5, (
                name,
                before_lines,
                middle_lines,
                after_lines,
            )
        assert between(middle_lines[moved][1], before_lines[moved][1], after_lines[moved][1])
        # 座標軸：20,000 的刻度文字在兩個位置之間
        assert abs(before_labels["20,000"] - after_labels["20,000"]) > 10
        assert between(middle_labels["20,000"], before_labels["20,000"], after_labels["20,000"])

        # 減少動態效果：不動
        page.emulate_media(reduced_motion="reduce")
        page.locator(".sheet-controls").get_by_label("Technology").check()
        # 新的圖（比例尺回到有 90,000 的刻度）畫好之後：沒有任何動畫
        page.wait_for_function(f"(() => ({LABELS})()['90,000'] !== undefined)()", timeout=30000)
        page.wait_for_timeout(300)
        assert page.evaluate(PAUSE_AT, [0]) == []
        assert page.evaluate(SERIES, "line") == before_lines
        assert errors == []
        browser.close()


def test_area_series_fade_out_then_morph_in_sequence(engine: str) -> None:
    """堆疊的區域（依序、1 秒）：取消勾選 West 之後，West 的區域在第一段淡出（原本的樣子疊在圖上），其餘區域的形狀
    與座標軸在第二段改變——第二段的一半時區域的頂端在原本與新的位置之間。"""
    from playwright.sync_api import sync_playwright

    call(
        engine,
        "/api/edit/demo/animation",
        {"animation": {"on": True, "duration": 1, "style": "sequential"}},
        method="PUT",
    )
    sheet = sheet_with_filter(engine, "animated areas", "area", "f_region")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{engine}/?token={TOKEN}")
        page.goto(f"{engine}/w/demo/s/{sheet}")
        page.wait_for_selector(".single-sheet .chart[data-drawn='true']")
        page.wait_for_timeout(300)
        before = page.evaluate(SERIES, "area")
        assert set(before) == {"Central", "East", "South", "West"}

        page.locator(".sheet-controls").get_by_label("West").uncheck()
        page.wait_for_selector(".single-sheet .chart[data-animating='true']", timeout=10000)
        timing = page.evaluate(PAUSE_AT, [1500])
        morphing = [t for t in timing if t["tag"] == "path" and "mark-area" in t["layer"]]
        assert len(morphing) == 3 and all(
            t["props"] == ["d"] and t["delay"] == 1000 for t in morphing
        ), morphing
        gone = [t for t in timing if t["ghost"] and t["delay"] == 0 and t["props"] == ["opacity"]]
        assert gone, timing  # West 的區域在第一段淡出
        axis = [t for t in timing if "role-axis-label" in t["layer"] and not t["ghost"]]
        assert axis and all(t["delay"] == 1000 for t in axis)
        middle = page.evaluate(SERIES, "area")
        assert "West" not in middle

        page.evaluate("document.getAnimations().forEach((a) => a.finish())")
        page.wait_for_function("!document.querySelector('.single-sheet .chart[data-animating]')")
        assert page.locator(".mark-ghosts").count() == 0
        after = page.evaluate(SERIES, "area")
        moved = max(after, key=lambda k: abs(before[k][1] - after[k][1]))
        assert abs(before[moved][1] - after[moved][1]) > 10
        assert between(middle[moved][1], before[moved][1], after[moved][1])
        assert errors == []
        browser.close()
