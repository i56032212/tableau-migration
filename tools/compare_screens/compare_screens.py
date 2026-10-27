"""並列截圖頁（F5）：每組篩選一頁，左邊是 Tableau 的 .png，右邊是新平台的截圖，供擁有者目視簽核。

    uv run python tools/compare_screens/compare_screens.py fixtures/<報表>

- 沒給 --url 時，自動在背景啟動查詢服務（需要已建置的檢視器 web/dist）
- Tableau 的截圖來自 fetch-expected（expected/<工作表>/<篩選組合>.png）；沒有時顯示「尚未抓取」
- 有 report/calc.json（tmig verify 的結果）時，一併標示每項計算比對是否通過

輸出 fixtures/<報表>/compare/index.html，以及每組篩選一頁；用瀏覽器直接開啟即可。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

ROOT = Path(__file__).resolve().parents[2]
STATUS_TEXT = {"pass": "計算一致", "fail": "計算不一致", "unsupported": "尚不支援"}

STYLE = """
body { font-family: system-ui, "Noto Sans TC", "Microsoft JhengHei", sans-serif; margin: 24px; color: #1f2328; background: #f3f4f6; }
h1 { font-size: 20px; } h2 { font-size: 16px; margin: 0 0 8px; }
a { color: #4c78a8; }
.params { font-size: 13px; color: #4b5563; margin-bottom: 16px; }
.pair { background: #fff; border: 1px solid #d9dde3; border-radius: 6px; padding: 12px; margin-bottom: 16px; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.cols figure { margin: 0; } .cols figcaption { font-size: 12px; color: #6b7280; margin-bottom: 4px; }
.cols img { width: 100%; border: 1px solid #eef0f3; background: #fff; }
.missing { display: flex; align-items: center; justify-content: center; min-height: 160px; border: 1px dashed #d9dde3; color: #6b7280; font-size: 13px; background: #fafafa; }
.badge { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 10px; margin-left: 8px; vertical-align: middle; }
.badge.pass { background: #dcfce7; color: #166534; } .badge.fail { background: #fee2e2; color: #991b1b; } .badge.unsupported { background: #fef3c7; color: #92400e; }
table { border-collapse: collapse; background: #fff; } th, td { border: 1px solid #d9dde3; padding: 6px 10px; font-size: 13px; text-align: left; }
.note { font-size: 13px; color: #92400e; }
"""


def page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>\n"
    )


def load_report(fixture: Path) -> dict[str, dict[str, Any]]:
    path = fixture / "report" / "calc.json"
    if not path.is_file():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    return {c["check_id"]: c for c in report.get("checks", [])}


def capture(
    base_url: str, workbook: str, fixture: Path, cases: dict[str, Any], dashboards: list[str]
) -> None:
    from playwright.sync_api import sync_playwright

    screens = fixture / "compare" / "screens"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000}, device_scale_factor=1
        )
        page_ = context.new_page()
        for case in cases["cases"]:
            name = case["name"]
            for sheet in cases["sheets"]:
                url = f"{base_url}/w/{quote(workbook)}/s/{quote(sheet['worksheet'])}?case={quote(name)}"
                page_.goto(url)
                page_.wait_for_selector(".worksheet[data-loading='false']", timeout=60_000)
                page_.wait_for_timeout(400)
                target = screens / name / f"{sheet['worksheet']}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                page_.locator(".single-sheet").screenshot(path=str(target))
            for board in dashboards:
                url = f"{base_url}/w/{quote(workbook)}/d/{quote(board)}?case={quote(name)}"
                page_.goto(url)
                page_.wait_for_selector(".dashboard", timeout=60_000)
                page_.wait_for_function(
                    "Array.from(document.querySelectorAll('.worksheet')).every(e => e.dataset.loading === 'false')",
                    timeout=60_000,
                )
                page_.wait_for_timeout(400)
                target = screens / name / f"dashboard-{board}.png"
                page_.locator(".dashboard").screenshot(path=str(target))
        browser.close()


def build_pages(
    fixture: Path, cases: dict[str, Any], names: dict[str, str], dashboards: dict[str, str]
) -> Path:
    out = fixture / "compare"
    report = load_report(fixture)
    rows = []
    for case in cases["cases"]:
        name = case["name"]
        params = case.get("url_params") or {}
        params_text = "、".join(f"{k}={v}" for k, v in params.items()) or "（預設篩選）"
        parts = [
            '<p><a href="index.html">← 全部篩選組合</a></p>',
            f"<h1>篩選組合：{html.escape(name)}</h1>",
            f'<div class="params">網址參數：{html.escape(params_text)}'
            + (f"；{html.escape(str(case['note']))}" if case.get("note") else "")
            + "</div>",
        ]
        statuses = []
        for sheet in cases["sheets"]:
            ws = sheet["worksheet"]
            check = report.get(f"{ws}:{name}")
            status = check["status"] if check else None
            statuses.append(status)
            badge = f'<span class="badge {status}">{STATUS_TEXT[status]}</span>' if status else ""
            tableau_png = fixture / "expected" / ws / f"{name}.png"
            left = (
                f'<img src="../expected/{quote(ws)}/{quote(name)}.png" alt="Tableau">'
                if tableau_png.is_file()
                else '<div class="missing">尚未抓取 Tableau 截圖（tools/fetch_expected）</div>'
            )
            right = f'<img src="screens/{quote(name)}/{quote(ws)}.png" alt="新平台">'
            diff = (
                f'<p class="note">{html.escape(str(check["diff"]))}</p>'
                if check and check["status"] != "pass"
                else ""
            )
            parts.append(
                f'<section class="pair"><h2>{html.escape(str(names.get(ws, ws)))}{badge}</h2>{diff}'
                f'<div class="cols"><figure><figcaption>Tableau</figcaption>{left}</figure>'
                f"<figure><figcaption>新平台</figcaption>{right}</figure></div></section>"
            )
        for board_id, board_name in dashboards.items():
            parts.append(
                f'<section class="pair"><h2>dashboard：{html.escape(board_name)}</h2>'
                f'<img style="max-width:100%;border:1px solid #eef0f3" src="screens/{quote(name)}/dashboard-{quote(board_id)}.png" alt="dashboard"></section>'
            )
        (out / f"{name}.html").write_text(
            page(f"並列比對：{name}", "".join(parts)), encoding="utf-8"
        )
        cells = "".join(
            f"<td>{f'<span class="badge {s}">{STATUS_TEXT[s]}</span>' if s else '—'}</td>"
            for s in statuses
        )
        rows.append(
            f'<tr><td><a href="{quote(name)}.html">{html.escape(name)}</a></td><td>{html.escape(params_text)}</td>{cells}</tr>'
        )
    headers = "".join(
        f"<th>{html.escape(str(names.get(s['worksheet'], s['worksheet'])))}</th>"
        for s in cases["sheets"]
    )
    index = (
        f"<h1>並列比對：{html.escape(cases['workbook'])}</h1>"
        "<p>每組篩選一頁：左邊是 Tableau 的截圖，右邊是新平台。請逐頁確認沒有資訊落差後簽核。</p>"
        f"<table><tr><th>篩選組合</th><th>網址參數</th>{headers}</tr>{''.join(rows)}</table>"
    )
    target = out / "index.html"
    target.write_text(page("並列比對", index), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="產生 Tableau 截圖與新平台截圖的並列頁")
    parser.add_argument("fixture", type=Path, help="fixture 目錄，例如 fixtures/sales_overview")
    parser.add_argument("--url", help="已啟動的查詢服務網址；省略時自動在背景啟動")
    parser.add_argument("--web", type=Path, default=ROOT / "web" / "dist", help="檢視器的建置目錄")
    args = parser.parse_args(argv)
    fixture: Path = args.fixture.resolve()
    cases = yaml.safe_load((fixture / "cases.yaml").read_text(encoding="utf-8"))

    from tmig.verify.run import load_fixture_ir

    ir = load_fixture_ir(fixture)
    names = {w.id: w.name for w in ir.worksheets}
    dashboards = {d.id: d.name for d in ir.dashboards}
    if args.url is None and not (args.web / "index.html").is_file():
        print(
            f"失敗：找不到檢視器 {args.web}；先執行 cd web && pnpm install && pnpm build",
            file=sys.stderr,
        )
        return 1
    if args.url is None:
        from tmig_service.background import running_server

        server = running_server(fixture.parent, args.web)
    else:
        server = nullcontext(args.url.rstrip("/"))
    with server as base_url:
        capture(base_url, fixture.name, fixture, cases, list(dashboards))
    index = build_pages(fixture, cases, names, dashboards)
    print(f"已產生 {index}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
