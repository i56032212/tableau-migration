"""桌面版的本機引擎（D0）：權杖、開啟 .twbx 與報表套件、存成報表套件、關閉。"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import warnings
import zipfile
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig_service.desktop import TOKEN_COOKIE, TOKEN_HEADER, create_desktop_app

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
FEATURES = ROOT / "fixtures" / "superstore_features" / "superstore_features.twbx"
TOKEN = "test-token-1234567890"
LOCAL = "http://127.0.0.1:8123"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, ROOT / "no-web-dist", username="amy")
    return TestClient(app, base_url=LOCAL, headers={TOKEN_HEADER: TOKEN})


@pytest.fixture
def user_files(tmp_path: Path) -> Path:
    """作者電腦上的檔案（開啟的是複本，原始檔不動）。"""
    folder = tmp_path / "我的報表"
    folder.mkdir()
    shutil.copy(DEMO, folder / "銷售示範.twbx")
    shutil.copy(FEATURES, folder / "features.twbx")
    return folder


def open_file(client: TestClient, path: Path) -> dict:
    response = client.post("/api/desktop/open", json={"path": str(path)})
    assert response.status_code == 200, response.text
    return response.json()


def render_all(client: TestClient, workbook: str) -> dict[str, dict[str, Any]]:
    ir = client.get(f"/api/workbooks/{workbook}/ir").json()
    results = {}
    for worksheet in ir["worksheets"]:
        url = f"/api/workbooks/{workbook}/worksheets/{worksheet['id']}/render"
        response = client.post(url)
        assert response.status_code == 200, (worksheet["id"], response.text)
        results[worksheet["id"]] = response.json()["table"]
    return results


def same_tables(left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]) -> bool:
    """顯示的表格相同；原始數值只容許浮點數加總順序造成的誤差（DuckDB 平行彙總的順序不固定）。"""
    assert left.keys() == right.keys()
    for name in left:
        a, b = left[name], right[name]
        assert a["headers"] == b["headers"] and a["rows"] == b["rows"], name
        for row_a, row_b in zip(a["raw"], b["raw"], strict=True):
            for x, y in zip(row_a, row_b, strict=True):
                assert x == y or _close(x, y), (name, x, y)
    return True


def _close(x: object, y: object) -> bool:
    try:
        fx, fy = float(str(x)), float(str(y))
    except ValueError:
        return False
    return math.isclose(fx, fy, rel_tol=1e-9, abs_tol=1e-9)


def test_requests_need_the_token(tmp_path: Path) -> None:
    app = create_desktop_app(tmp_path / "workspace", TOKEN, ROOT / "no-web-dist")
    anonymous = TestClient(app, base_url=LOCAL, follow_redirects=False)
    assert anonymous.get("/api/health").status_code == 401
    assert anonymous.get("/api/health", headers={TOKEN_HEADER: "wrong"}).status_code == 401
    assert anonymous.get("/?token=wrong").status_code == 401
    # 正確的權杖換成 cookie，網址去掉權杖但保留其他參數
    handshake = anonymous.get("/w/demo?token=" + TOKEN + "&case=East")
    assert handshake.status_code == 303
    assert handshake.headers["location"] == "/w/demo?case=East"
    cookie = handshake.headers["set-cookie"]
    assert cookie.startswith(f"{TOKEN_COOKIE}=") and "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    anonymous.cookies.set(TOKEN_COOKIE, TOKEN)
    assert anonymous.get("/api/health").status_code == 200
    assert anonymous.get("/api/health", headers={TOKEN_HEADER: TOKEN}).status_code == 200


def test_only_local_host_names_are_accepted(tmp_path: Path) -> None:
    """DNS rebinding：其他網域解析到 127.0.0.1 時 Host 標頭不是本機，一律拒絕。"""
    app = create_desktop_app(tmp_path / "workspace", TOKEN, ROOT / "no-web-dist")
    headers = {TOKEN_HEADER: TOKEN}
    assert (
        TestClient(app, base_url="http://evil.example")
        .get("/api/health", headers=headers)
        .status_code
        == 403
    )
    assert (
        TestClient(app, base_url="http://localhost:9000")
        .get("/api/health", headers=headers)
        .status_code
        == 200
    )


def test_identity_is_the_local_user(client: TestClient) -> None:
    me = client.get("/api/me").json()
    assert me["mode"] == "desktop"
    assert me["user"]["username"] == "amy"
    assert me["user"]["groups"] == []


def test_open_twbx_and_query(client: TestClient, user_files: Path) -> None:
    source = user_files / "銷售示範.twbx"
    before = source.read_bytes()
    opened = open_file(client, source)
    document = opened["document"]
    assert document["id"] == "report"  # 中文檔名轉不成英數字
    assert document["name"] == "superstore_demo"
    assert document["kind"] == "twbx"
    assert document["source"] == str(source)
    assert opened["unsupported_count"] == 0
    assert [w["id"] for w in client.get("/api/workbooks").json()] == ["report"]
    tables = render_all(client, "report")
    assert tables["ws_sales_by_region"]["rows"]
    assert source.read_bytes() == before
    # 同一個檔案再開一次：回傳已開啟的那一份
    again = open_file(client, source)
    assert again["already_open"] is True
    assert again["document"]["id"] == "report"
    assert len(client.get("/api/desktop/documents").json()) == 1


def test_save_package_then_reopen_gives_same_definition_and_numbers(
    client: TestClient, user_files: Path
) -> None:
    opened = open_file(client, user_files / "銷售示範.twbx")["document"]
    target = user_files / "銷售示範.tmigx"
    saved = client.post("/api/desktop/save", json={"workbook": opened["id"], "path": str(target)})
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["path"] == str(target)
    assert result["datasources"] == ["ds_orders"]
    assert result["document"]["saved"] == str(target)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"manifest.json", "report.ir.json", "data/ds_orders.duckdb"} <= names
    assert manifest["format"] == "tmigx" and manifest["version"] == 1
    assert manifest["name"] == "superstore_demo"

    reopened = open_file(client, target)["document"]
    assert reopened["kind"] == "package"
    assert reopened["id"] != opened["id"]
    first = client.get(f"/api/workbooks/{opened['id']}/ir").json()
    second = client.get(f"/api/workbooks/{reopened['id']}/ir").json()
    assert first == second
    assert same_tables(render_all(client, opened["id"]), render_all(client, reopened["id"]))


def test_package_keeps_the_images(client: TestClient, user_files: Path) -> None:
    """圖片區塊與圖片按鈕的圖片存進套件，重新開啟後照樣提供。"""
    opened = open_file(client, user_files / "features.twbx")["document"]
    ir = client.get(f"/api/workbooks/{opened['id']}/ir").json()
    images = sorted(
        {
            path
            for dashboard in ir["dashboards"]
            for zone in _zones(dashboard["zones"])
            for path in (
                zone.get("image"),
                (zone.get("button") or {}).get("image"),
                (zone.get("button") or {}).get("image_hidden"),
            )
            if path
        }
    )
    assert images
    target = user_files / "features.tmigx"
    result = client.post(
        "/api/desktop/save", json={"workbook": opened["id"], "path": str(target)}
    ).json()
    assert result["files"] == images
    assert result["missing_files"] == []
    reopened = open_file(client, target)["document"]
    for path in images:
        original = client.get(f"/api/workbooks/{opened['id']}/files/{path}")
        copy = client.get(f"/api/workbooks/{reopened['id']}/files/{path}")
        assert copy.status_code == 200
        assert copy.content == original.content


def _zones(zones: list[dict]) -> list[dict]:
    found = []
    for zone in zones:
        found.append(zone)
        found.extend(_zones(zone.get("children", [])))
    return found


def test_save_again_and_close(client: TestClient, user_files: Path) -> None:
    opened = open_file(client, user_files / "銷售示範.twbx")["document"]
    bad = client.post(
        "/api/desktop/save",
        json={"workbook": opened["id"], "path": str(user_files / "out.twbx")},
    )
    assert bad.status_code == 422
    missing_folder = client.post(
        "/api/desktop/save",
        json={"workbook": opened["id"], "path": str(user_files / "nope" / "out.tmigx")},
    )
    assert missing_folder.status_code == 422
    closed = client.post("/api/desktop/close", json={"workbook": opened["id"]})
    assert closed.json() == {"ok": True}
    assert client.get("/api/desktop/documents").json() == []
    assert client.get(f"/api/workbooks/{opened['id']}/ir").status_code == 404
    assert client.post("/api/desktop/close", json={"workbook": opened["id"]}).status_code == 404


def test_open_rejects_bad_files(client: TestClient, tmp_path: Path) -> None:
    def status(path: str) -> int:
        return client.post("/api/desktop/open", json={"path": path}).status_code

    assert status("relative/report.twbx") == 422
    assert status(str(tmp_path / "missing.twbx")) == 404
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert status(str(tmp_path / "notes.txt")) == 422
    (tmp_path / "broken.tmigx").write_bytes(b"not a zip")
    assert status(str(tmp_path / "broken.tmigx")) == 422
    (tmp_path / "broken.twbx").write_bytes(b"not a zip")
    assert status(str(tmp_path / "broken.twbx")) == 422
    # 開啟失敗不留下工作區的複本
    assert client.get("/api/desktop/documents").json() == []
    assert client.get("/api/workbooks").json() == []


def test_engine_prints_port_and_token_then_serves(tmp_path: Path) -> None:
    """外殼啟動引擎：讀第一行 JSON 取得埠號與權杖；關閉標準輸入後引擎結束。"""
    env = {**os.environ, "PYTHONUTF8": "1"}
    command = [sys.executable, "-m", "tmig.cli", "desktop", "serve", "--port", "0"]
    command += ["--workspace", str(tmp_path / "ws"), "--clean", "--exit-with-parent"]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdin is not None
        ready = json.loads(process.stdout.readline())
        assert ready["event"] == "ready" and ready["port"] > 0 and len(ready["token"]) >= 32
        request = urllib.request.Request(
            f"http://127.0.0.1:{ready['port']}/api/health", headers={"X-Tmig-Token": ready["token"]}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert json.loads(response.read()) == {"status": "ok"}
        process.stdin.close()
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        assert process.poll() is not None, "關閉標準輸入後引擎沒有結束"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
