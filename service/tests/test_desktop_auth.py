"""桌面版登入伺服器：瀏覽器中的核准頁、授權碼＋PKCE 換權杖、Bearer 驗證與撤銷（D0）。"""

from __future__ import annotations

import html
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.desktop_auth import challenge_of, loopback_uri
from tmig_service.store import ReportStore

REDIRECT = "http://127.0.0.1:5555/desktop-auth/callback"


def make_app(tmp_path: Path, settings: AuthSettings | None = None) -> FastAPI:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    settings = settings or AuthSettings(
        mode="dev", dev_users=["amy", "bob"], require_directory=False
    )
    return create_app(
        fixtures, tmp_path / "no-web", auth=settings, store=ReportStore(tmp_path / "store")
    )


def browser_for(app: FastAPI, user: str) -> TestClient:
    client = TestClient(app)
    assert client.post("/auth/dev", json={"username": user}).status_code == 200
    return client


def authorize_params(verifier: str, state: str = "s1") -> dict[str, str]:
    return {
        "redirect_uri": REDIRECT,
        "state": state,
        "code_challenge": challenge_of(verifier),
        "code_challenge_method": "S256",
        "label": "AMY-PC",
    }


def approve(browser: TestClient, verifier: str, decision: str = "allow") -> dict[str, list[str]]:
    page = browser.get("/desktop/authorize", params=authorize_params(verifier))
    assert page.status_code == 200, page.text
    match = re.search(r'name="ticket" value="([^"]+)"', page.text)
    assert match is not None
    response = browser.post(
        "/desktop/authorize",
        content=urlencode({"ticket": html.unescape(match.group(1)), "decision": decision}),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
    return parse_qs(location.query)


def test_sign_in_with_browser_approval(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    browser = browser_for(app, "amy")
    desktop = TestClient(app)
    verifier = secrets.token_urlsafe(48)

    page = browser.get("/desktop/authorize", params=authorize_params(verifier))
    assert "amy" in page.text and "AMY-PC" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

    # 授權碼只能用一次；code_verifier 不符時授權碼也作廢
    query = approve(browser, verifier)
    assert query["state"] == ["s1"]
    code = query["code"][0]
    exchange = {"code": code, "code_verifier": "x" * 43, "redirect_uri": REDIRECT}
    assert desktop.post("/api/desktop-auth/token", json=exchange).status_code == 400
    exchange["code_verifier"] = verifier
    assert desktop.post("/api/desktop-auth/token", json=exchange).status_code == 400

    code = approve(browser, verifier)["code"][0]
    other = {"code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:1/x"}
    assert desktop.post("/api/desktop-auth/token", json=other).status_code == 400

    code = approve(browser, verifier)["code"][0]
    exchange = {"code": code, "code_verifier": verifier, "redirect_uri": REDIRECT}
    granted = desktop.post("/api/desktop-auth/token", json=exchange)
    assert granted.status_code == 200, granted.text
    token = granted.json()["token"]
    assert granted.json()["username"] == "amy" and token.startswith("tmigd_")
    # 伺服器只存雜湊值
    assert token not in (tmp_path / "store" / "desktop-tokens.json").read_text(encoding="utf-8")

    headers = {"Authorization": f"Bearer {token}"}
    assert desktop.get("/api/me", headers=headers).json()["user"]["username"] == "amy"
    assert desktop.get("/api/reports", headers=headers).status_code == 200
    mine = desktop.get("/api/desktop-auth/tokens", headers=headers).json()
    assert [(t["label"], t["current"]) for t in mine] == [("AMY-PC", True)]
    assert [t["current"] for t in browser.get("/api/desktop-auth/tokens").json()] == [False]
    # 別人不能撤銷
    bob = browser_for(app, "bob")
    assert bob.delete(f"/api/desktop-auth/tokens/{mine[0]['id']}").status_code == 404
    # 桌面版登出：撤銷自己的權杖，之後就不能用
    assert desktop.delete("/api/desktop-auth/tokens/current", headers=headers).json() == {
        "ok": True
    }
    assert desktop.get("/api/me", headers=headers).json()["user"] is None
    assert desktop.get("/api/reports", headers=headers).status_code == 401
    assert desktop.get("/api/reports", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_denied_and_invalid_requests(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    browser = browser_for(app, "amy")
    verifier = secrets.token_urlsafe(48)
    denied = approve(browser, verifier, decision="deny")
    assert denied == {"error": ["access_denied"], "state": ["s1"]}

    # 回呼網址只接受本機的 http
    for uri in ("https://127.0.0.1:5/x", "http://evil.example:5/x", "http://127.0.0.1/x"):
        params = {**authorize_params(verifier), "redirect_uri": uri}
        assert browser.get("/desktop/authorize", params=params).status_code == 400
    assert loopback_uri("http://[::1]:8080/cb") and loopback_uri("http://localhost:1/cb")
    plain = {**authorize_params(verifier), "code_challenge_method": "plain"}
    assert browser.get("/desktop/authorize", params=plain).status_code == 400

    # 沒有登入（開發模式要先選使用者）
    anonymous = TestClient(app)
    page = anonymous.get("/desktop/authorize", params=authorize_params(verifier))
    assert page.status_code == 401 and "選擇使用者" in page.text

    # 竄改或別人的票證
    response = browser.post(
        "/desktop/authorize",
        content="ticket=forged&decision=allow",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    page = browser.get("/desktop/authorize", params=authorize_params(verifier))
    match = re.search(r'name="ticket" value="([^"]+)"', page.text)
    assert match is not None
    bob = browser_for(app, "bob")
    stolen = bob.post(
        "/desktop/authorize",
        content=urlencode({"ticket": html.unescape(match.group(1)), "decision": "allow"}),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert stolen.status_code == 403


def test_no_sign_in_without_authentication(tmp_path: Path) -> None:
    app = make_app(tmp_path, AuthSettings())
    verifier = secrets.token_urlsafe(48)
    page = TestClient(app).get("/desktop/authorize", params=authorize_params(verifier))
    assert page.status_code == 400 and "沒有設定身分驗證" in page.text
