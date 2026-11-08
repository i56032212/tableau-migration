"""桌面版連到伺服器：登入（瀏覽器核准）、發布、伺服器上有新版本時的衝突、從伺服器開啟、登出（D0）。

伺服器與桌面版的本機引擎都在同一個行程中執行：引擎以 TestClient 連到伺服器，
系統瀏覽器由測試代替（以 amy 登入伺服器、按「允許」、照伺服器的導向回到引擎）。
"""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.credentials import MemoryCredentials, target_for
from tmig_service.desktop import create_desktop_app
from tmig_service.store import ReportStore

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
TOKEN = "t" * 43
ENGINE = "http://127.0.0.1:5555"
SERVER = "http://testserver"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def server_app(tmp_path: Path, settings: AuthSettings | None = None) -> FastAPI:
    fixtures = tmp_path / "server-fixtures"
    fixtures.mkdir(exist_ok=True)
    settings = settings or AuthSettings(
        mode="dev", dev_users=["amy", "bob"], require_directory=False
    )
    return create_app(
        fixtures, tmp_path / "no-web", auth=settings, store=ReportStore(tmp_path / "store")
    )


def engine_app(
    tmp_path: Path, server: FastAPI, opened: list[str]
) -> tuple[FastAPI, MemoryCredentials]:
    credentials = MemoryCredentials()
    app = create_desktop_app(
        tmp_path / "workspace",
        TOKEN,
        username="amy",
        settings_dir=tmp_path / "settings",
        credentials=credentials,
        opener=opened.append,
        client_factory=lambda: TestClient(server),
    )
    return app, credentials


def browse(server: FastAPI, url: str, engine: FastAPI, decision: str = "allow") -> httpx.Response:
    """代替系統瀏覽器：以 amy 登入伺服器、在核准頁按下按鈕、照伺服器的導向回到本機引擎（不帶引擎的權杖）。"""
    browser = TestClient(server)
    assert browser.post("/auth/dev", json={"username": "amy"}).status_code == 200
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == SERVER
    page = browser.get(f"{parts.path}?{parts.query}")
    match = re.search(r'name="ticket" value="([^"]+)"', page.text)
    assert match is not None, page.text
    redirect = browser.post(
        "/desktop/authorize",
        content=urlencode({"ticket": html.unescape(match.group(1)), "decision": decision}),
        headers=FORM,
        follow_redirects=False,
    )
    location = urlsplit(redirect.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == ENGINE
    return TestClient(engine, base_url=ENGINE).get(f"{location.path}?{location.query}")


def test_sign_in_publish_open_and_sign_out(tmp_path: Path) -> None:
    server = server_app(tmp_path)
    opened: list[str] = []
    app, credentials = engine_app(tmp_path, server, opened)
    engine = TestClient(app, base_url=ENGINE, headers={"X-Tmig-Token": TOKEN})

    assert engine.get("/api/desktop/server").json()["signed_in"] is False
    assert engine.get("/api/desktop/server/reports").status_code == 401

    # 登入：開啟系統瀏覽器到伺服器的核准頁，按「允許」後回到引擎
    started = engine.post("/api/desktop/server/signin", json={"url": f"{SERVER}/"})
    assert started.status_code == 200, started.text
    assert opened == [started.json()["authorize_url"]]
    assert engine.get("/api/desktop/server").json()["pending"] is True
    done = browse(server, opened[-1], app)
    assert done.status_code == 200 and "已登入" in done.text
    status = engine.get("/api/desktop/server").json()
    assert (status["url"], status["username"], status["signed_in"], status["pending"]) == (
        SERVER,
        "amy",
        True,
        False,
    )
    token = credentials.get(target_for(SERVER))
    assert token is not None and token.startswith("tmigd_")
    assert token not in (tmp_path / "settings" / "desktop-server.json").read_text(encoding="utf-8")
    # 同一個核准不能再用一次
    assert TestClient(app, base_url=ENGINE).get(urlsplit(opened[-1]).path).status_code in (400, 401)

    # 開啟 .twbx，發布到伺服器
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    document = engine.post("/api/desktop/open", json={"path": str(source)}).json()["document"]["id"]
    projects = engine.get("/api/desktop/server/projects").json()
    assert [p["id"] for p in projects if p["can_publish"]] == ["default"]
    first = engine.post(
        "/api/desktop/server/publish",
        json={
            "workbook": document,
            "name": "Superstore Demo",
            "project": "default",
            "comment": "第一版",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"]["version"] == 1
    assert first.json()["address"] == f"{SERVER}/w/superstore_demo"
    documents = {d["id"]: d for d in engine.get("/api/desktop/documents").json()}
    assert documents[document]["server"]["report"] == "superstore_demo"
    assert documents[document]["server"]["version"] == 1
    # 同名再發布但沒有指定覆蓋：伺服器回應同名
    exists = engine.post(
        "/api/desktop/server/publish",
        json={"workbook": document, "name": "Superstore Demo", "project": "default"},
    )
    assert exists.status_code == 409 and exists.json()["detail"]["error"] == "exists"
    # 覆蓋同一份：以第 1 版為基準
    second = engine.post(
        "/api/desktop/server/publish", json={"workbook": document, "report": "superstore_demo"}
    )
    assert second.status_code == 200 and second.json()["version"]["version"] == 2

    # bob 在伺服器上回復第 1 版（第 3 版）；amy 的文件以第 2 版為基準 → 衝突，由作者決定覆蓋
    bob = TestClient(server)
    bob.post("/auth/dev", json={"username": "bob"})
    assert bob.post("/api/reports/superstore_demo/versions/1/restore", json={}).status_code == 200
    conflict = engine.post(
        "/api/desktop/server/publish", json={"workbook": document, "report": "superstore_demo"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "conflict"
    assert conflict.json()["detail"]["latest"]["publisher"] == "bob"
    forced = engine.post(
        "/api/desktop/server/publish",
        json={"workbook": document, "report": "superstore_demo", "force": True},
    )
    assert forced.status_code == 200 and forced.json()["version"]["version"] == 4

    # 從伺服器開啟某一版：記下版本，可以查詢
    reports = engine.get("/api/desktop/server/reports").json()
    assert [(r["id"], r["version"]) for r in reports] == [("superstore_demo", 4)]
    assert engine.post("/api/desktop/close", json={"workbook": document}).status_code == 200
    remote = engine.post(
        "/api/desktop/server/open", json={"report": "superstore_demo", "version": 1}
    )
    assert remote.status_code == 200, remote.text
    remote_document = remote.json()["document"]
    assert remote_document["server"]["version"] == 1
    assert remote_document["source"] == f"{SERVER}/w/superstore_demo"
    ir = engine.get(f"/api/workbooks/{remote_document['id']}/ir").json()
    sheet = ir["worksheets"][0]["id"]
    rendered = engine.post(
        f"/api/workbooks/{remote_document['id']}/worksheets/{sheet}/render", json={}
    )
    assert rendered.status_code == 200 and rendered.json()["rows"] > 0
    again = engine.post("/api/desktop/server/open", json={"report": "superstore_demo"})
    assert again.json()["already_open"] is True

    # 登出：在伺服器撤銷權杖，並從認證管理員刪除
    assert engine.post("/api/desktop/server/signout").json() == {"ok": True}
    assert credentials.get(target_for(SERVER)) is None
    assert engine.get("/api/desktop/server").json()["signed_in"] is False
    revoked = TestClient(server).get("/api/reports", headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401


def test_sign_in_failures(tmp_path: Path) -> None:
    server = server_app(tmp_path)
    opened: list[str] = []
    app, credentials = engine_app(tmp_path, server, opened)
    engine = TestClient(app, base_url=ENGINE, headers={"X-Tmig-Token": TOKEN})
    assert engine.post("/api/desktop/server/signin", json={"url": " "}).status_code == 422
    assert engine.post("/api/desktop/server/signin", json={"url": "ftp://x"}).status_code == 422

    # 在伺服器上按「拒絕」
    engine.post("/api/desktop/server/signin", json={"url": SERVER})
    page = browse(server, opened[-1], app, decision="deny")
    assert "拒絕" in page.text
    status = engine.get("/api/desktop/server").json()
    assert status["signed_in"] is False and "拒絕" in status["error"]

    # 不是這次登入的回呼
    stray = TestClient(app, base_url=ENGINE).get("/desktop-auth/callback?state=nope&code=x")
    assert stray.status_code == 400
    # 其他網址仍要引擎的權杖
    assert TestClient(app, base_url=ENGINE).get("/api/desktop/server").status_code == 401

    # 伺服器上撤銷了權杖：下一次呼叫時要求重新登入
    engine.post("/api/desktop/server/signin", json={"url": SERVER})
    browse(server, opened[-1], app)
    token = credentials.get(target_for(SERVER))
    TestClient(server).delete(
        "/api/desktop-auth/tokens/current", headers={"Authorization": f"Bearer {token}"}
    )
    assert engine.get("/api/desktop/server/reports").status_code == 401
    status = engine.get("/api/desktop/server").json()
    assert status["signed_in"] is False and "重新登入" in status["error"]


def test_server_without_authentication(tmp_path: Path) -> None:
    server = server_app(tmp_path, AuthSettings())
    app, _ = engine_app(tmp_path, server, [])
    engine = TestClient(app, base_url=ENGINE, headers={"X-Tmig-Token": TOKEN})
    response = engine.post("/api/desktop/server/signin", json={"url": SERVER})
    assert response.status_code == 422 and "沒有設定身分驗證" in response.json()["detail"]
