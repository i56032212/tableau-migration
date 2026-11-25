"""伺服器版安裝內容的冒煙測試：以建置好的資料夾（或安裝後的目錄）實際執行，不用 repo 的程式與套件。

    uv run python deploy/windows/smoke.py deploy/windows/build/staging
    uv run python deploy/windows/smoke.py "C:\\Program Files\\TmigServer" --no-host   # 安裝後（服務已在跑）

檢查的項目：
1. 內附的 Python 載入每個資料庫驅動、Hyper API、GDAL／PROJ 與 Playwright
2. 服務主程式（console 模式）依 server.toml 啟動查詢服務；檢視器與 API 回應，示範報表的查詢結果
   （第一次查詢時由 .twbx 匯入萃取）與 repo 的程式在這台電腦上算的相同
3. 內附的 Chromium（訂閱的 PNG、PDF 用）開得起來，能把檢視器的頁面印成 PDF
4. 意外結束的程序會被重新啟動；停止時所有程序都結束（埠號不再接受連線）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
WORKSHEET = "ws_sales_by_region"
STATE = {
    "filters": {"f_category": {"mode": "include", "values": ["Furniture"]}},
    "parameters": {"p_parameter_1": 2},
}

# 資料庫驅動與原生程式庫（kernel/src/tmig/data/sources.py 用到的模組）
MODULES = [
    "psycopg",
    "oracledb",
    "databricks.sql",
    "pymssql",
    "redshift_connector",
    "pymysql",
    "snowflake.connector",
    "prestodb",
    "trino",
    "pyathena",
    "pyathena.arrow.cursor",
    "google.cloud.bigquery",
    "teradatasql:runtimestats",
    "impala.dbapi",
    "ibm_db_dbi",
    "hdbcli.dbapi",
    "tableauhyperapi",
    "pyogrio",
    "pyproj",
    "playwright.sync_api",
    "pypdf",
    "tmig.cli",
    "tmig_service.app",
    "tmig_service.subscriptions",
]

# 「模組:函式」是載入後再呼叫的函式：teradatasql 要到這時才載入原生程式庫
IMPORTS = """
import importlib, json, sys
result = {}
for name in sys.argv[1:]:
    try:
        module, _, function = name.partition(":")
        loaded = importlib.import_module(module)
        if function:
            getattr(loaded, function)()
        result[name] = "ok"
    except Exception as exc:  # noqa: BLE001
        result[name] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result, ensure_ascii=False))
"""

PDF = """
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(sys.argv[1], wait_until="networkidle")
    data = page.pdf()
    browser.close()
sys.stdout.write(f"{len(data)} {data[:5].decode('ascii', 'replace')}")
"""


class SmokeError(RuntimeError):
    pass


def _env(install: Path, temp: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        # 不讓 repo 的虛擬環境影響內附的 Python
        if key.upper() in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
            env.pop(key)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PLAYWRIGHT_BROWSERS_PATH": str(install / "ms-playwright"),
            "TMIG_WEB_DIST": str(install / "web" / "dist"),
            "TEMP": str(temp),
            "TMP": str(temp),
        }
    )
    return env


def _python(install: Path) -> Path:
    python = install / "python" / "python.exe"
    if not python.is_file():
        raise SmokeError(f"找不到 {python}")
    return python


def check_imports(install: Path, temp: Path) -> None:
    out = subprocess.run(
        [str(_python(install)), "-X", "utf8", "-s", "-c", IMPORTS, *MODULES],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(install, temp),
        cwd=temp,
        timeout=300,
    )
    if out.returncode != 0:
        raise SmokeError(f"載入模組失敗：{out.stderr[-2000:]}")
    result = json.loads(out.stdout.strip().splitlines()[-1])
    failed = {k: v for k, v in result.items() if v != "ok"}
    if failed:
        raise SmokeError("載入失敗的模組：" + json.dumps(failed, ensure_ascii=False, indent=1))
    print(f"  模組：{len(result)} 個都載入成功")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(url: str, body: Any = None, timeout: float = 120) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _wait_ready(base: str, host: subprocess.Popen[bytes], deadline: float) -> None:
    while time.monotonic() < deadline:
        if host.poll() is not None:
            raise SmokeError(f"服務主程式提早結束（結束碼 {host.returncode}）")
        try:
            if _request(f"{base}/api/workbooks", timeout=5)[0] == 200:
                return
        except OSError:
            time.sleep(1)
    raise SmokeError("查詢服務沒有在時間內就緒")


def _expected_table() -> Any:
    """repo 的程式在這台電腦上算的結果（比對安裝內容算的是否相同）。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from tmig_service.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        fixtures = Path(tmp) / "fixtures"
        _copy_fixture(fixtures / DEMO.name)
        client = TestClient(create_app(fixtures, Path(tmp) / "no-web"))
        response = client.post(
            f"/api/workbooks/{DEMO.name}/worksheets/{WORKSHEET}/render", json=STATE
        )
        response.raise_for_status()
        return response.json()["table"]


def _copy_fixture(target: Path) -> None:
    shutil.copytree(
        DEMO,
        target,
        ignore=shutil.ignore_patterns("*.duckdb", "*.duckdb.wal", "report", "compare"),
    )


def check_host(install: Path, temp: Path) -> None:
    data = temp / "data"
    _copy_fixture(data / "workbooks" / DEMO.name)
    port = _free_port()
    (data / "server.toml").write_text(
        "\n".join(
            [
                "[environment]",
                "TMIG_SMOKE = 'ok'",
                "",
                "[[process]]",
                'name = "server"',
                f'args = ["serve", "--host", "127.0.0.1", "--port", "{port}", '
                "\"--fixtures\", '{data}\\workbooks', \"--store\", '{data}\\store', "
                "\"--usage-dir\", '{data}\\usage']",
                "",
                "# 立刻結束的程序：服務主程式應該記下並重新啟動它",
                "[[process]]",
                'name = "exits"',
                'args = ["--help"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    host_exe = install / "tmig-server-host.exe"
    check = subprocess.run(
        [str(host_exe), "check", "--config", str(data / "server.toml")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if check.returncode != 0 or "程序 server" not in check.stdout:
        raise SmokeError(f"check 失敗：{check.stdout}{check.stderr}")
    # 主控台的輸出寫到檔案（接到管線又沒有人讀時，緩衝區滿了服務主程式會卡住）
    console = temp / "host-console.txt"
    with console.open("wb") as out:
        host = subprocess.Popen(
            [str(host_exe), "console", "--config", str(data / "server.toml")],
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=_env(install, temp),
        )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base, host, time.monotonic() + 180)
        status, index = _request(f"{base}/views/{DEMO.name}")
        if status != 200 or b"<html" not in index.lower():
            raise SmokeError("檢視器的頁面沒有回應")
        workbooks = json.loads(_request(f"{base}/api/workbooks")[1])
        if DEMO.name not in json.dumps(workbooks):
            raise SmokeError(f"報表清單中沒有 {DEMO.name}")
        started = time.monotonic()
        rendered = json.loads(
            _request(f"{base}/api/workbooks/{DEMO.name}/worksheets/{WORKSHEET}/render", STATE)[1]
        )
        print(f"  查詢服務：第一次查詢（含萃取匯入）{time.monotonic() - started:.1f} 秒")
        expected = _expected_table()
        if rendered["table"] != expected:
            raise SmokeError("查詢結果與 repo 的程式算的不同")
        print(f"  查詢結果與 repo 的程式相同（{rendered['rows']} 列）")
        pdf = subprocess.run(
            [str(_python(install)), "-X", "utf8", "-s", "-c", PDF, f"{base}/views/{DEMO.name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_env(install, temp),
            timeout=300,
        )
        if pdf.returncode != 0 or "%PDF" not in pdf.stdout:
            raise SmokeError(f"Chromium 沒有印出 PDF：{pdf.stdout}{pdf.stderr[-2000:]}")
        print(f"  Chromium：印出 PDF（{pdf.stdout.split()[0]} 位元組）")
        deadline = time.monotonic() + 60
        host_log = data / "logs" / "host.log"
        while time.monotonic() < deadline:
            text = host_log.read_text(encoding="utf-8") if host_log.exists() else ""
            if text.count("啟動 exits") >= 2 and "exits 意外結束（結束碼 0），5 秒後" in text:
                break
            time.sleep(1)
        else:
            raise SmokeError("立刻結束的程序沒有被重新啟動")
        print("  意外結束的程序會重新啟動")
    finally:
        # console 模式在標準輸入關閉時結束
        if host.stdin is not None:
            host.stdin.close()
        try:
            host.wait(timeout=60)
        except subprocess.TimeoutExpired:
            host.kill()
            host.wait()
            raise SmokeError("服務主程式沒有在 60 秒內結束") from None
    text = console.read_text(encoding="utf-8", errors="replace")
    host_text = (data / "logs" / "host.log").read_text(encoding="utf-8")
    if host.returncode != 0 or "所有程序已結束" not in text or "所有程序已結束" not in host_text:
        raise SmokeError(f"服務主程式沒有正常結束（{host.returncode}）：{text[-2000:]}")
    with socket.socket() as s:
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise SmokeError("停止之後查詢服務還在接受連線")
    server_log = (data / "logs" / "server.log").read_text(encoding="utf-8")
    if "Uvicorn running" not in server_log and "Application startup complete" not in server_log:
        raise SmokeError("server.log 中沒有查詢服務的啟動訊息")
    print("  停止時所有程序都結束；記錄檔寫在 logs\\")


def check_running(install: Path, temp: Path, url: str) -> None:
    """安裝後：服務已在跑，只檢查 API 與 Chromium。"""
    status, _ = _request(f"{url}/api/workbooks", timeout=30)
    if status != 200:
        raise SmokeError(f"{url} 沒有回應")
    pdf = subprocess.run(
        [str(_python(install)), "-X", "utf8", "-s", "-c", PDF, f"{url}/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(install, temp),
        timeout=300,
    )
    if pdf.returncode != 0 or "%PDF" not in pdf.stdout:
        raise SmokeError(f"Chromium 沒有印出 PDF：{pdf.stderr[-2000:]}")
    print(f"  {url} 回應；Chromium 印出 PDF")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("install", type=Path, help="建置好的資料夾或安裝目錄")
    parser.add_argument(
        "--no-host", action="store_true", help="不啟動服務主程式（安裝後服務已在跑時）"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="--no-host 時的伺服器網址")
    args = parser.parse_args(argv)
    install = args.install.resolve()
    temp = Path(tempfile.mkdtemp(prefix="tmig-server-smoke-"))
    try:
        print(f"冒煙測試：{install}")
        check_imports(install, temp)
        if args.no_host:
            check_running(install, temp, args.url)
        else:
            check_host(install, temp)
    except SmokeError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print("通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
