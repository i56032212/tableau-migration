"""從 Tableau Server 抓取預期結果（E1）：每張工作表 × 每組篩選的 .csv 與 .png。

第一次使用（在能連到 Tableau Server 的電腦上）：

    uv run --with playwright playwright install chromium
    uv run --with playwright python tools/fetch_expected/fetch_expected.py fixtures/<報表> --login

--login 會開啟瀏覽器，請手動登入 Tableau Server，回到終端機按 Enter 後開始抓取。
登入狀態存在瀏覽器設定檔（預設 ~/.tmig/tableau-profile），之後不用再加 --login。

寫入：

- fixtures/<報表>/expected/<工作表 id>/<篩選組合>.csv、.png
- fixtures/<報表>/expected/fetch-log.json：每個檔案的網址、HTTP 狀態與抓取時間
- cases.yaml 的 fetched_at

注意：一律用工作表網址抓 CSV（dashboard 網址的 .csv 只會給字母序第一張工作表）；
同一份報表的 .twbx 與預期檔必須在同一次萃取更新之間取得。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

DEFAULT_PROFILE = Path.home() / ".tmig" / "tableau-profile"
TIMEOUT_MS = 180_000


class FetchError(RuntimeError):
    pass


def normalize_views_url(url: str) -> str:
    """瀏覽器網址列的 https://server/#/site/<site>/views/<wb> 轉成 https://server/t/<site>/views/<wb>。"""
    url = url.strip().rstrip("/")
    match = re.match(r"^(https?://[^/#]+)/#/site/([^/]+)/views/([^/?#]+)", url)
    if match:
        return f"{match.group(1)}/t/{match.group(2)}/views/{match.group(3)}"
    match = re.match(r"^(https?://[^/#]+)/#/views/([^/?#]+)", url)
    if match:
        return f"{match.group(1)}/views/{match.group(2)}"
    return url


def view_url(views_url: str, view: str, params: dict[str, Any], ext: str) -> str:
    """工作表網址：名稱與值以百分比編碼，但保留多值用的逗號、跳脫用的反斜線與日期部分的括號。"""
    base = normalize_views_url(views_url)
    query = "&".join(
        f"{quote(str(name), safe='()')}={quote(_text(value), safe=',' + chr(92))}"
        for name, value in params.items()
    )
    return f"{base}/{quote(view, safe='')}.{ext}" + (f"?{query}" if query else "")


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def update_fetched_at(cases_path: Path, timestamp: str) -> None:
    """只改 fetched_at 那一行，保留 cases.yaml 的註解與排版。"""
    text = cases_path.read_text(encoding="utf-8")
    line = f"fetched_at: {timestamp}"
    updated, count = re.subn(r"(?m)^fetched_at:.*$", line, text)
    if count == 0:
        updated = text.rstrip("\n") + f"\n{line}\n"
    cases_path.write_text(updated, encoding="utf-8", newline="\n")


def plan(fixture: Path) -> list[tuple[str, str, str, Path]]:
    """要抓的檔案：(網址, 工作表 id, 篩選組合, 存放路徑)。"""
    cases = yaml.safe_load((fixture / "cases.yaml").read_text(encoding="utf-8"))
    views_url = cases.get("tableau_views_url")
    if not views_url:
        raise FetchError(
            "cases.yaml 沒有 tableau_views_url，例如 https://tableau.example.com/t/<site>/views/<workbook>"
        )
    jobs = []
    for sheet in cases["sheets"]:
        for case in cases["cases"]:
            params = case.get("url_params") or {}
            for ext in ("csv", "png"):
                target = fixture / "expected" / sheet["worksheet"] / f"{case['name']}.{ext}"
                jobs.append(
                    (
                        view_url(views_url, sheet["view"], params, ext),
                        sheet["worksheet"],
                        case["name"],
                        target,
                    )
                )
    return jobs


def fetch(fixture: Path, profile: Path, login: bool) -> int:
    from playwright.sync_api import sync_playwright

    jobs = plan(fixture)
    log: list[dict[str, Any]] = []
    failures = 0
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile), headless=not login, accept_downloads=True
        )
        try:
            if login:
                page = context.new_page()
                page.goto(jobs[0][0].rsplit("/", 1)[0])
                input("請在開啟的瀏覽器登入 Tableau Server，完成後回到這裡按 Enter：")
            for url, worksheet, case, target in jobs:
                response = context.request.get(url, timeout=TIMEOUT_MS)
                content_type = response.headers.get("content-type", "")
                entry = {
                    "url": url,
                    "worksheet": worksheet,
                    "case": case,
                    "file": str(target.relative_to(fixture)),
                    "status": response.status,
                    "content_type": content_type,
                    "fetched_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
                }
                log.append(entry)
                if not response.ok or "text/html" in content_type:
                    failures += 1
                    reason = (
                        "可能未登入或登入已失效，請加 --login 重試"
                        if "text/html" in content_type
                        else f"HTTP {response.status}"
                    )
                    print(
                        f"失敗  {worksheet} {case} {target.suffix}：{reason}（{url}）",
                        file=sys.stderr,
                    )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.body())
                print(f"已抓取 {target.relative_to(fixture)}")
        finally:
            context.close()
    (fixture / "expected").mkdir(parents=True, exist_ok=True)
    (fixture / "expected" / "fetch-log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    if failures == 0:
        update_fetched_at(
            fixture / "cases.yaml", dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
        )
    print(f"共 {len(jobs)} 個檔案，失敗 {failures} 個")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="從 Tableau Server 抓取預期 CSV 與 PNG")
    parser.add_argument("fixture", type=Path, help="fixture 目錄，例如 fixtures/sales_overview")
    parser.add_argument(
        "--profile", type=Path, default=DEFAULT_PROFILE, help="瀏覽器設定檔目錄（保存登入狀態）"
    )
    parser.add_argument("--login", action="store_true", help="開啟瀏覽器手動登入後再抓取")
    parser.add_argument("--dry-run", action="store_true", help="只列出要抓的網址")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            for url, _, _, target in plan(args.fixture):
                print(f"{target.relative_to(args.fixture)}  ←  {url}")
            return 0
        return fetch(args.fixture, args.profile, args.login)
    except FetchError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
