"""列印與匯出（續）的端到端：頁面架的「所有頁面」依序印每一頁；分頁時某一頁從多層列標題的一組中間開始，那一頁
的第一列也寫上外層的名稱；在區塊內捲動的工作表單獨檢視時，匯出的圖片、PowerPoint 與訂閱的圖片是整張工作表；
dashboard 匯出的圖片照畫面上的樣子（區塊捲到哪裡就是哪一段）。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import base64
import io
import shutil
import struct
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tmig.edit import add_dashboard, add_worksheet, replace_dashboard, replace_worksheet
from tmig.ir import load_ir
from tmig_service.background import running_server

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"
DEMO = ROOT / "fixtures" / "superstore_demo"


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


def use(field: str, agg: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "agg": agg,
        "date": None,
        "continuous": agg is not None,
        "table_calc": None,
    }


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, str], Path]]:
    """示範報表加上：Long Table（Sub-Category × Region，68 列，標準大小、在區塊內上下捲動）、Nested Table（Category、
    Sub-Category、Region 三層列標題，68 列）、Region Pages（頁面架是 Region 的 Category 文字表），與只放 Long Table
    一張工作表、比它矮的 dashboard（Scroll Board，區塊內上下捲動）。"""
    root = tmp_path_factory.mktemp("print_export_more")
    target = root / "demo"
    target.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", target / "superstore_demo.twbx")
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")
    sheets: dict[str, str] = {}
    for name, rows, pages in (
        ("Long Table", [use("f_sub_category"), use("f_region")], None),
        ("Nested Table", [use("f_category"), use("f_sub_category"), use("f_region")], None),
        ("Region Pages", [use("f_category")], use("f_region")),
    ):
        ir, created = add_worksheet(ir, name, None)
        data = created.model_dump(mode="json")
        data["rows"] = rows
        data["marks"]["label"] = [use("f_sales", "sum")]
        data["fit"] = "standard"
        data["pages"] = pages
        ir, saved = replace_worksheet(ir, data)
        sheets[name] = saved.id
    ir, board = add_dashboard(ir, "Scroll Board", 700, 420)
    data = board.model_dump(mode="json")
    flow = data["zones"][0]["children"][0]
    flow["children"] = [
        {
            "id": "z3",
            "kind": "worksheet",
            "x": 0,
            "y": 0,
            "w": 700,
            "h": 420,
            "worksheet": sheets["Long Table"],
            "source_ref": flow["source_ref"],
        }
    ]
    ir, board = replace_dashboard(ir, data)
    sheets["Scroll Board"] = board.id
    (target / "golden.ir.json").write_text(
        ir.model_dump_json(indent=2), encoding="utf-8", newline="\n"
    )
    with running_server(root, DIST) as url:
        yield url, sheets, root


def pdf_pages(pdf: bytes) -> list[str]:
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


CELLS = """sheet => [...document.querySelectorAll(`[data-worksheet='${sheet}'] .chart svg text`)]
  .filter(t => !t.closest('.frozen') && /^[0-9][0-9,.]*$/.test(t.textContent.trim()))
  .map(t => t.textContent.trim())"""


def open_view(page: Any, url: str, kind: str, view: str, sheet: str) -> None:
    page.goto(f"{url}/w/demo/{kind}/{view}")
    page.wait_for_selector(f"[data-worksheet='{sheet}'] .chart[data-drawn='true']")
    page.wait_for_selector(f"[data-worksheet='{sheet}'][data-updating='false']")
    page.wait_for_timeout(300)


def print_with(page: Any, **options: Any) -> None:
    """匯出＞PDF（列印）：頁面設定填好之後按「列印」（瀏覽器的列印對話框換成記下有沒有呼叫）。"""
    # 運算式的結果是函式時 Playwright 會呼叫它：包成一個函式，換掉之後不會先被呼叫一次
    page.evaluate(
        "() => { window.__printed = false; window.print = () => { window.__printed = true; }; }"
    )
    page.get_by_label("匯出").select_option("pdf")
    setup = page.locator(".page-setup")
    setup.get_by_label("紙張").select_option("A4")
    setup.get_by_label("方向").select_option(options.get("orientation", "portrait"))
    setup.get_by_label("列印縮放").select_option("auto")
    for label in options.get("radios", []):
        setup.get_by_label(label).check()
    setup.get_by_role("button", name="列印").click()
    page.wait_for_function("window.__printed === true", timeout=60000)


def test_pages_shelf_prints_all_pages(server: tuple[str, dict[str, str], Path]) -> None:
    """頁面架是 Region 的文字表：頁面設定的「頁面架」選「所有頁面」時，四個地區依序各印一頁（每一頁是那個地區的
    數字），印完頁面控制項回到原本的頁；選「目前的頁面」時只印目前這一頁。"""
    from playwright.sync_api import sync_playwright

    url, sheets, _ = server
    sheet = sheets["Region Pages"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_view(page, url, "s", sheet, sheet)
        control = page.locator(".page-control").get_by_label("目前的頁")
        shown = "s => s.selectedIndex >= 0 ? s.options[s.selectedIndex].text : ''"
        page.wait_for_function(
            f"({shown})(document.querySelector('.page-control select')) === 'Central'"
        )
        page.wait_for_selector(f"[data-worksheet='{sheet}'][data-updating='false']")
        draws = (
            f"Number(document.querySelector(\"[data-worksheet='{sheet}'] .chart\").dataset.draws)"
        )

        def show(region: str) -> list[str]:
            """切換到這一頁、等新的圖畫好，回傳各格的數字。"""
            before = page.evaluate(draws)
            control.select_option(label=region)
            page.wait_for_function(
                f"""() => {{
                  const c = document.querySelector("[data-worksheet='{sheet}'] .chart");
                  return c && c.dataset.drawn === 'true' && c.closest('.worksheet').dataset.updating === 'false'
                    && Number(c.dataset.draws) > {before};
                }}"""
            )
            return page.evaluate(CELLS, sheet)

        regions = ["Central", "East", "South", "West"]
        cells = {region: show(region) for region in ["East", "South", "West", "Central"]}
        assert all(len(values) == 3 for values in cells.values())
        assert len({tuple(v) for v in cells.values()}) == 4  # 各地區的數字不同
        assert show("East") == cells["East"]  # 目前的頁：East

        print_with(page, radios=["所有頁面"])
        pages = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pages) == 4
        for content, region in zip(pages, regions, strict=True):
            assert all(value in content for value in cells[region]), (region, content)
        # 印完回到原本的頁
        page.wait_for_selector(f"[data-worksheet='{sheet}'][data-updating='false']")
        assert control.evaluate(shown) == "East"
        for _ in range(100):
            if page.evaluate(CELLS, sheet) == cells["East"]:
                break
            page.wait_for_timeout(100)
        assert page.evaluate(CELLS, sheet) == cells["East"]

        page.evaluate("window.dispatchEvent(new Event('afterprint'))")
        print_with(page, radios=["目前的頁面"])
        pages = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pages) == 1
        assert all(value in pages[0] for value in cells["East"])
        assert errors == []
        browser.close()


# 原圖的每一列（最內層列標題的定位點在 SVG 中的 y、各層的名稱）與資料區的上下緣：在畫面上量（列印的樣式下原圖
# 不顯示，量不到位置）
ROWS = """sheet => {
  const chart = document.querySelector(`[data-worksheet='${sheet}'] .chart`);
  const svg = chart.querySelector(':scope > svg, :scope > .frozen-host > svg');
  const [, plotY, , plotH] = chart.dataset.plot.split(',').map(Number);
  const layers = [...svg.querySelectorAll('g.mark-text.role-mark')].filter((g) => /__deco__header\\d+_marks/.test(g.getAttribute('class')));
  const level = (g) => Number(/__deco__header(\\d+)/.exec(g.getAttribute('class'))[1]);
  const inner = layers.reduce((a, b) => (level(a) > level(b) ? a : b));
  const rows = [...inner.querySelectorAll(':scope > text')].map((t) => ({ y: t.getCTM().f, path: t.__data__.datum.rows_dim.split('｜') }));
  return { rows, top: plotY, bottom: plotY + plotH };
}"""

# 列印的版面上，每一頁的列標題那一塊（原圖的 x 從 0 開始、從資料區中間開始的區域）：這一塊的第一列（定位點落在
# 這一塊裡、最上面的那一列）的各層名稱，與這一塊裡看得到的文字
HEADER_PIECES = """({ rows, top: plotY, bottom: plotBottom }) => {
  const out = [];
  document.querySelectorAll('.print-page').forEach((pageElement, i) => {
    const pieces = [...pageElement.querySelectorAll('svg[data-region]')].map((s) => ({ s, r: s.dataset.region.split(',').map(Number) }));
    const header = pieces.find((p) => p.r[0] === 0 && p.r[1] > plotY + 1 && p.r[1] < plotBottom - 1);
    if (!header) return;
    const first = rows.filter((r) => r.y >= header.r[1] && r.y < header.r[3]).sort((a, b) => a.y - b.y)[0];
    const box = header.s.getBoundingClientRect();
    const texts = [...header.s.querySelectorAll('text')]
      .filter((t) => { const b = t.getBoundingClientRect(); return t.textContent.trim() && b.height > 0 && b.top >= box.top - 1 && b.bottom <= box.bottom + 1; })
      .map((t) => t.textContent.trim());
    out.push({ page: i + 1, first: first ? first.path : null, texts });
  });
  return out;
}"""


def test_page_starting_mid_group_repeats_outer_headers(
    server: tuple[str, dict[str, str], Path],
) -> None:
    """三層列標題（Category、Sub-Category、Region）的 68 列文字表以 A4 直向分頁印：第二頁以後從 Office Supplies
    那一組的中間開始，那一頁的第一列也寫著外層的 Category 與 Sub-Category 的名稱（原本只有那一組的第一列有）。"""
    from playwright.sync_api import sync_playwright

    url, sheets, _ = server
    sheet = sheets["Nested Table"]
    with sync_playwright() as p:
        browser = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_view(page, url, "s", sheet, sheet)
        rows = page.evaluate(ROWS, sheet)
        page.evaluate(
            "window.tmigPageSetup({paper: 'A4', orientation: 'portrait', scaling: 'auto'})"
        )
        page.emulate_media(media="print")
        pieces = page.evaluate(HEADER_PIECES, rows)
        page.emulate_media(media="null")
        assert len(pieces) >= 1
        middle = [p for p in pieces if p["first"] and p["first"][2] != "Central"]
        assert middle, pieces  # 至少有一頁從 Sub-Category 的一組中間開始
        for piece in pieces:
            outer, sub, _ = piece["first"]
            assert outer in piece["texts"], piece
            assert sub in piece["texts"], piece
        pdf = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pdf) == len(pieces) + 1
        for content, piece in zip(pdf[1:], pieces, strict=True):
            assert piece["first"][0] in content and piece["first"][1] in content
        assert errors == []
        browser.close()


def test_image_and_powerpoint_have_the_whole_sheet(
    server: tuple[str, dict[str, str], Path],
) -> None:
    """Long Table 在區塊內上下捲動：匯出的圖片（PNG，2 倍解析度）與 PowerPoint 裡的圖片都有整張工作表——高度至少是
    整張圖（68 列）的高度，原本只有區塊裡看得到的那一段。"""
    from playwright.sync_api import sync_playwright

    url, sheets, _ = server
    sheet = sheets["Long Table"]
    with sync_playwright() as p:
        browser = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
        page = browser.new_page(viewport={"width": 1400, "height": 900}, accept_downloads=True)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_view(page, url, "s", sheet, sheet)
        chart = page.locator(f"[data-worksheet='{sheet}'] .chart")
        size = chart.evaluate(
            "c => { const s = c.querySelector('svg'); return [Number(s.getAttribute('width')), Number(s.getAttribute('height')), c.clientHeight]; }"
        )
        assert size[1] > size[2] + 200  # 圖比區塊高很多（在區塊內捲動）
        with page.expect_download() as download:
            page.get_by_label("匯出").select_option("image")
        png = Path(download.value.path()).read_bytes()
        width, height = png_size(png)
        assert height / 2 >= size[1]
        assert width / 2 >= size[0]

        with page.expect_download() as download:
            page.get_by_label("匯出").select_option("powerpoint")
        with zipfile.ZipFile(Path(download.value.path())) as pptx:
            image = pptx.read("ppt/media/image1.png")
        assert png_size(image)[1] / 2 >= size[1]
        assert errors == []
        browser.close()


# 三張圖（data URL）的平均差異：匯出的圖縮成與截圖相同的大小後，與兩張截圖逐點比較灰階
DIFFERENCES = """async ([exported, scrolled, top]) => {
  const load = (src) => new Promise((resolve, reject) => { const i = new Image(); i.onload = () => resolve(i); i.onerror = reject; i.src = src; });
  const [a, b, c] = await Promise.all([load(exported), load(scrolled), load(top)]);
  const w = b.naturalWidth, h = b.naturalHeight;
  const pixels = (image) => {
    const canvas = document.createElement('canvas'); canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h); ctx.drawImage(image, 0, 0, w, h);
    return ctx.getImageData(0, 0, w, h).data;
  };
  const [pa, pb, pc] = [pixels(a), pixels(b), pixels(c)];
  const gray = (p, i) => (p[i] + p[i + 1] + p[i + 2]) / 3;
  let toScrolled = 0, toTop = 0;
  for (let i = 0; i < pa.length; i += 4) { toScrolled += Math.abs(gray(pa, i) - gray(pb, i)); toTop += Math.abs(gray(pa, i) - gray(pc, i)); }
  const n = pa.length / 4;
  return [toScrolled / n, toTop / n, a.naturalWidth, a.naturalHeight, w, h];
}"""


def test_dashboard_image_shows_the_scrolled_part(server: tuple[str, dict[str, str], Path]) -> None:
    """dashboard 上的 Long Table 在區塊內捲到最下面：匯出的圖片與畫面上（捲到最下面、欄標題固定在上方）的樣子相近，
    與捲到最上面的樣子差得多——原本不管捲到哪裡都畫最上面那一段。"""
    from playwright.sync_api import sync_playwright

    url, sheets, _ = server
    sheet = sheets["Long Table"]
    board = sheets["Scroll Board"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900}, accept_downloads=True)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_view(page, url, "d", board, sheet)
        chart = page.locator(f"[data-worksheet='{sheet}'] .chart")
        assert chart.evaluate("c => c.scrollHeight > c.clientHeight + 200")
        dashboard = page.locator(".page .dashboard")
        chart.evaluate("c => { c.scrollTop = 0; }")
        page.wait_for_timeout(200)
        top = dashboard.screenshot()
        chart.evaluate("c => { c.scrollTop = c.scrollHeight; }")
        page.wait_for_timeout(200)
        scrolled = dashboard.screenshot()
        with page.expect_download() as download:
            page.get_by_label("匯出").select_option("image")
        exported = Path(download.value.path()).read_bytes()

        def url_of(data: bytes) -> str:
            return "data:image/png;base64," + base64.b64encode(data).decode()

        to_scrolled, to_top, *sizes = page.evaluate(
            DIFFERENCES, [url_of(exported), url_of(scrolled), url_of(top)]
        )
        assert abs(sizes[0] / 2 - sizes[2]) <= 2 and abs(sizes[1] / 2 - sizes[3]) <= 2, sizes
        assert to_scrolled < to_top / 2, (to_scrolled, to_top)
        assert errors == []
        browser.close()


def test_subscription_png_has_the_whole_sheet(server: tuple[str, dict[str, str], Path]) -> None:
    """訂閱的圖片（以訂閱者的身分在 Chromium 開啟、截圖）：在區塊內捲動的 Long Table 是整張的（高度超過訂閱用的
    1000 像素高的視窗，原本只有視窗裡看得到的那一段）；dashboard 照畫面上的樣子（大小與 dashboard 相同）。"""
    from tmig.access.directory import Directory, DirectoryUser
    from tmig.access.permissions import Permissions
    from tmig_service.auth import AuthSettings
    from tmig_service.subscriptions import Renderer

    _, sheets, fixtures = server
    auth = AuthSettings(
        mode="oidc",
        directory=Directory(
            users=[
                DirectoryUser(
                    name="dave", email="dave@example.com", groups=["All Users"], site_role="Viewer"
                )
            ]
        ),
        permissions=Permissions.model_validate(
            {
                "workbooks": [
                    {
                        "id": "w1",
                        "name": "superstore_demo",
                        "rules": [
                            {
                                "grantee": {"kind": "group", "name": "All Users"},
                                "capabilities": {"Read": "Allow"},
                            }
                        ],
                    }
                ]
            }
        ),
    )
    renderer = Renderer(fixtures, auth, DIST)
    try:
        sheet, board = renderer.render(
            "dave",
            [
                ("Long Table", f"/w/demo/s/{sheets['Long Table']}"),
                ("Scroll Board", f"/w/demo/d/{sheets['Scroll Board']}"),
            ],
            png=True,
            pdf=True,
            paper="A4",
        )
    finally:
        renderer.close()
    assert sheet.png is not None and board.png is not None and sheet.pdf is not None
    assert png_size(sheet.png)[1] > 68 * 20 > 1000
    width, height = png_size(board.png)
    assert abs(width - 700) <= 30 and abs(height - 420) <= 30
    # 之後產生的 PDF 不受影響：整張縮到一頁
    assert len(pdf_pages(sheet.pdf)) == 1
