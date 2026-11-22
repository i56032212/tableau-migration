"""列印與 PDF 的分頁（Tableau Desktop 的列印：比一頁大的工作表分成多頁印）的端到端：在區塊內捲動的工作表
整張印出來（原本只印區塊裡看得到的那一段），頁的順序先往下、再往右，「在每頁中重複標題和圖例」時每一頁都有
欄、列標題，「在窗格邊界分頁」時分頁不切過儲存格的文字；「適合於」橫向、縱向的頁數；不經過頁面設定、直接以
瀏覽器列印（Ctrl+P）時也分頁。

需要建置好的檢視器（web/dist）與 Playwright 的 Chromium；缺少時略過。"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tmig.edit import add_worksheet, replace_worksheet
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
    """示範報表加上兩張標準大小的文字表：Long Table（Sub-Category × Region，68 列，比區塊高、在區塊內上下捲動）與
    Big Table（各州 × 各 Sub-Category，比 A4 直向的紙寬也比紙高）。"""
    root = tmp_path_factory.mktemp("print_pages")
    target = root / "demo"
    target.mkdir()
    shutil.copy(DEMO / "superstore_demo.twbx", target / "superstore_demo.twbx")
    shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")
    sheets: dict[str, str] = {}
    for name, rows, columns in (
        ("Long Table", [use("f_sub_category"), use("f_region")], []),
        ("Big Table", [use("f_state_province")], [use("f_sub_category")]),
    ):
        ir, created = add_worksheet(ir, name, None)
        data = created.model_dump(mode="json")
        data["rows"] = rows
        data["columns"] = columns
        data["marks"]["label"] = [use("f_sales", "sum")]
        data["fit"] = "standard"
        ir, saved = replace_worksheet(ir, data)
        sheets[name] = saved.id
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
    with running_server(root, DIST) as url:
        yield url, sheets, root


def pdf_pages(pdf: bytes) -> list[str]:
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]


CELLS = """sheet => [...document.querySelectorAll(`[data-worksheet='${sheet}'] .chart svg text`)]
  .filter(t => !t.closest('.frozen') && /^[0-9][0-9,.]*$/.test(t.textContent.trim()))
  .map(t => t.textContent.trim())"""

# 列印的版面上，分頁的地方（資料區中間的上、下邊）切到的文字：每一塊 SVG 記著它是原圖的哪個區域（data-region），
# 資料區（.chart 的 data-plot）的上下緣與標題、座標軸相接，不算分頁
CUT_TEXTS = """sheet => {
  const [, plotY, , plotH] = document.querySelector(`[data-worksheet='${sheet}'] .chart`).dataset.plot.split(',').map(Number);
  const inner = (y) => y > plotY + 1 && y < plotY + plotH - 1;
  const cut = [];
  document.querySelectorAll('.print-page svg[data-region]').forEach((piece) => {
    const [, top, , bottom] = piece.dataset.region.split(',').map(Number);
    const box = piece.getBoundingClientRect();
    piece.querySelectorAll('text').forEach((t) => {
      const r = t.getBoundingClientRect();
      if (r.height === 0 || !t.textContent.trim()) return;
      if ((inner(top) && r.top < box.top - 0.5) || (inner(bottom) && r.bottom > box.bottom + 0.5)) cut.push(t.textContent.trim());
    });
  });
  return cut;
}"""


def open_sheet(page: Any, url: str, sheet: str) -> None:
    page.goto(f"{url}/w/demo/s/{sheet}")
    page.wait_for_selector(f"[data-worksheet='{sheet}'] .chart[data-drawn='true']")
    page.wait_for_timeout(300)


def print_with(page: Any, **options: Any) -> None:
    """匯出＞PDF（列印）：頁面設定填好之後按「列印」（瀏覽器的列印對話框換成記下有沒有呼叫）。"""
    # 包在函式裡：運算式的結果是函式時 Playwright 會呼叫它，__printed 一開始就是 true，等列印的那一步就沒有作用
    page.evaluate(
        "() => { window.__printed = false; window.print = () => { window.__printed = true; }; }"
    )
    page.get_by_label("匯出").select_option("pdf")
    setup = page.locator(".page-setup")
    setup.get_by_label("紙張").select_option(options.get("paper", "A4"))
    setup.get_by_label("方向").select_option(options.get("orientation", "portrait"))
    setup.get_by_label("列印縮放").select_option(options.get("scaling", "auto"))
    for label, value in options.get("numbers", {}).items():
        setup.get_by_label(label, exact=True).fill(str(value))
    for label, checked in options.get("checks", {}).items():
        setup.get_by_label(label).set_checked(checked)
    setup.get_by_role("button", name="列印").click()
    page.wait_for_function("window.__printed === true")


def test_scrolling_sheet_prints_on_several_pages(server: tuple[str, dict[str, str], Path]) -> None:
    """Long Table 在標準大小的區塊內上下捲動，畫面上只看得到一部分；以頁面設定「自動」、A4 直向列印：整張表分成兩頁
    以上，68 個儲存格的值全部印出來（原本只印區塊裡看得到的二十幾列）；每一頁都有欄標題（Sub-Category、
    Region），分頁不切過文字。關掉「在每頁中重複標題和圖例」時只有第一頁有欄標題。"""
    from playwright.sync_api import sync_playwright

    url, sheets, _ = server
    sheet = sheets["Long Table"]
    with sync_playwright() as p:
        browser = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_sheet(page, url, sheet)
        chart = page.locator(f"[data-worksheet='{sheet}'] .chart")
        assert chart.evaluate("c => c.scrollHeight > c.clientHeight + 100")
        cells = page.evaluate(CELLS, sheet)
        assert len(cells) == 68

        print_with(page)
        pages = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pages) >= 2
        text = "\n".join(pages)
        missing = [value for value in cells if value not in text]
        assert missing == [], f"PDF 裡少了 {len(missing)} 個儲存格的值"
        # 標題只在第一頁
        assert pages[0].count("Long Table") == 1 and all(
            "Long Table" not in content for content in pages[1:]
        )
        for content in pages:
            assert "Sub-Category" in content and "Region" in content
        # 分頁落在列與列之間：每一塊裡的文字都完整
        page.evaluate(
            "window.tmigPageSetup({paper: 'A4', orientation: 'portrait', scaling: 'auto'})"
        )
        page.emulate_media(media="print")
        assert page.locator(".print-page").count() == len(pages)
        assert page.evaluate(CUT_TEXTS, sheet) == []
        page.emulate_media(media="null")

        # 不重複標題：之後的頁沒有欄標題
        print_with(page, checks={"在每頁中重複標題和圖例": False})
        pages = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pages) >= 2 and "Sub-Category" in pages[0]
        assert all("Sub-Category" not in content for content in pages[1:])
        assert errors == []
        browser.close()


def test_page_order_and_fit_to_pages(server: tuple[str, dict[str, str], Path]) -> None:
    """Big Table 比 A4 橫向的紙寬也比紙高：先往下、再往右——第一頁是最上面的州與最左邊的幾欄，第二頁是同樣的幾欄、
    下面的州；換到右邊的幾欄時又從最上面的州開始，列標題（州名）每一頁都有。「適合於」橫向 1 頁、縱向 1 頁時
    整張表縮到一頁。頁面設定記在瀏覽器：直接以瀏覽器列印（不經過頁面設定）時也照它分頁。"""
    from playwright.sync_api import sync_playwright

    url, sheets, _ = server
    sheet = sheets["Big Table"]
    with sync_playwright() as p:
        browser = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_sheet(page, url, sheet)
        cells = page.evaluate(CELLS, sheet)
        print_with(page, orientation="landscape")
        pages = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pages) >= 4
        text = "\n".join(pages)
        assert [value for value in cells if value not in text] == []
        # 第一欄的頁：Accessories 在、Tables（最右邊）不在；往下一頁還是同樣的欄
        assert "Alabama" in pages[0] and "Accessories" in pages[0] and "Tables" not in pages[0]
        assert "Accessories" in pages[1] and "Alabama" not in pages[1] and "Wyoming" in pages[1]
        # 換到右邊的欄：又從最上面的州開始（列標題重複），最後一頁有 Tables 與 Wyoming
        later = pages[2:]
        assert "Alabama" in later[0] and "Accessories" not in later[0]
        assert "Tables" in pages[-1] and "Wyoming" in pages[-1]

        # 適合於橫向 1 頁、縱向 1 頁：一頁
        print_with(
            page, orientation="landscape", scaling="fit", numbers={"橫向的頁數": 1, "縱向的頁數": 1}
        )
        pages = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pages) == 1
        assert [value for value in cells if value not in pages[0]] == []

        # 直接以瀏覽器列印（Ctrl+P）：beforeprint 依記下的頁面設定（適合於 1×1）分頁；印完拿掉分好的頁
        page.evaluate("window.dispatchEvent(new Event('afterprint'))")
        assert page.locator(".print-page").count() == 0
        page.reload()
        open_sheet(page, url, sheet)
        pages = pdf_pages(page.pdf(prefer_css_page_size=True))
        assert len(pages) == 1 and "Wyoming" in pages[0] and "Alabama" in pages[0]
        assert page.locator(".print-page").count() == 0
        assert errors == []
        browser.close()


def test_subscription_pdf_has_the_whole_sheet(server: tuple[str, dict[str, str], Path]) -> None:
    """訂閱的 PDF（以訂閱者的身分在 Chromium 開啟、套用「符合一頁」的列印樣式）：在區塊內捲動的 Long Table
    整張縮到一頁，68 個儲存格的值都在（原本只有剛開啟時區塊裡看得到的那一段）；訂閱的圖片照舊是畫面上的樣子。"""
    from playwright.sync_api import sync_playwright

    from tmig.access.directory import Directory, DirectoryUser
    from tmig.access.permissions import Permissions
    from tmig_service.auth import AuthSettings
    from tmig_service.subscriptions import Renderer

    url, sheets, fixtures = server
    sheet = sheets["Long Table"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        open_sheet(page, url, sheet)
        cells = page.evaluate(CELLS, sheet)
        browser.close()
    assert len(cells) == 68
    # 以訂閱者的身分查詢：使用者目錄與這份報表的檢視權限
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
        (item,) = renderer.render(
            "dave", [("Long Table", f"/w/demo/s/{sheet}")], png=True, pdf=True, paper="A4"
        )
    finally:
        renderer.close()
    assert item.pdf is not None and item.png is not None
    pages = pdf_pages(item.pdf)
    assert len(pages) == 1
    assert [value for value in cells if value not in pages[0]] == []
