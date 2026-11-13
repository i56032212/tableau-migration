"""打包後的本機引擎冒煙測試：啟動、開啟 .twbx（由 Hyper 匯入萃取）、查詢每張工作表、存成報表套件、
關閉（工作區的複本要刪得掉：Windows 上檔案還開著時刪不掉）、重新開啟報表套件，查詢結果的列數要相同；
確認連到伺服器的程式有打包進去（以作業系統憑證存放區的 HTTPS 用戶端連一個沒有服務的埠）；最後以 MapInfo 的
.mif（TWD67 二度分帶）建立報表，確認 GDAL（pyogrio）與基準面轉換的 PROJ（pyproj）的資料檔有打包進去。

    python desktop/engine/smoke.py desktop/engine/dist/tmig-engine/tmig-engine fixtures/superstore_demo/superstore_demo.twbx

只用標準函式庫，CI 在 Windows 上打包後直接執行。
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


def call(port: int, token: str, method: str, path: str, body: Any = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"X-Tmig-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        content = response.read()
    return json.loads(content) if path.startswith("/api/") else content


def call_status(port: int, token: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """與 call 相同，但錯誤時回傳狀態碼與 detail，不拋出例外。"""
    try:
        return 200, call(port, token, method, path, body)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}").get("detail")


def render_all(port: int, token: str, document: str) -> dict[str, int]:
    """每張工作表的列數。"""
    ir = call(port, token, "GET", f"/api/workbooks/{document}/ir")
    rows: dict[str, int] = {}
    for sheet in ir["worksheets"]:
        url = f"/api/workbooks/{document}/worksheets/{sheet['id']}/render"
        rows[sheet["name"]] = call(port, token, "POST", url, {})["rows"]
    return rows


def main(engine: str, workbook: str) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    engine = str(Path(engine).resolve())  # Windows：相對路徑與斜線一律換成完整路徑
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        workspace = work / "workspace"
        started = time.monotonic()
        process = subprocess.Popen(
            [
                engine,
                *("desktop", "serve", "--port", "0", "--clean", "--exit-with-parent"),
                *("--workspace", str(workspace)),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert process.stdout is not None and process.stdin is not None
        try:
            ready = json.loads(process.stdout.readline())
            port, token = ready["port"], ready["token"]
            print(f"引擎就緒：{time.monotonic() - started:.1f} 秒")
            page = call(port, token, "GET", "/")
            assert b'id="root"' in page, "沒有提供檢視器"

            source = work / "報表.twbx"
            shutil.copy(workbook, source)
            opened = call(port, token, "POST", "/api/desktop/open", {"path": str(source)})
            document = opened["document"]["id"]
            print(f"開啟：{opened['document']['name']}（不支援 {opened['unsupported_count']} 筆）")
            first = render_all(port, token, document)
            for name, rows in first.items():
                print(f"  {name}：{rows} 列")

            target = work / "報表.tmigx"
            saved = call(
                port,
                token,
                "POST",
                "/api/desktop/save",
                {"workbook": document, "path": str(target)},
            )
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
            assert "report.ir.json" in names and "manifest.json" in names
            print(f"存成報表套件：{saved['size']:,} bytes，萃取 {saved['datasources']}")

            call(port, token, "POST", "/api/desktop/close", {"workbook": document})
            assert not (workspace / document).exists(), "關閉後工作區的複本沒有刪掉"
            reopened = call(port, token, "POST", "/api/desktop/open", {"path": str(target)})
            again = render_all(port, token, reopened["document"]["id"])
            assert again == first, f"重新開啟報表套件後列數不同：{again} ≠ {first}"
            print(f"關閉後重新開啟報表套件：{len(again)} 張工作表的列數相同")

            server = call(port, token, "GET", "/api/desktop/server")
            assert server["signed_in"] is False
            status, detail = call_status(
                port, token, "POST", "/api/desktop/server/signin", {"url": "https://127.0.0.1:9"}
            )
            assert status == 502 and "連不上伺服器" in str(detail), (status, detail)
            print("連到伺服器的程式：已打包（連不上時回應 502）")

            # MapInfo（GDAL）與 TWD67 → WGS84（PROJ）：台北的點約在 121.5532°E、25.0352°N
            mif = work / "towns67.mif"
            mif.write_text(
                'Version 300\nCharset "Neutral"\nDelimiter ","\n'
                'CoordSys Earth Projection 8, 999, 2, -752, -358, -179, "m", 121, 0, 0.9999, 250000, 0\n'
                "Columns 1\n  town Char(10)\nData\n\nPoint 305000 2770000\n",
                encoding="utf-8",
            )
            mif.with_suffix(".mid").write_text('"Taipei"\n', encoding="utf-8")
            created = call(
                port, token, "POST", "/api/desktop/new", {"path": str(mif), "table": None}
            )
            preview = call(
                port,
                token,
                "GET",
                f"/api/workbooks/{created['document']['id']}/datasources/{created['datasource']}/preview",
            )
            cells = [str(c) for row in preview["rows"] for c in row]
            point = next((c for c in cells if c.startswith("POINT(121.553")), None)
            assert point is not None, cells
            print(f"MapInfo 與基準面轉換：{point}")
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                raise AssertionError("關閉標準輸入後引擎沒有結束") from None
    print("冒煙測試通過")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
