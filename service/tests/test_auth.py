"""身分驗證（P2）：查詢服務要求登入者身分，查詢依登入者套用列層級權限（P1）。

以示範報表加上一個列層級權限的資料源篩選（Region 對應到負責人，Managers 群組看全部）驗證四種模式：
none（不驗證）、dev（切換使用者）、proxy（反向代理的標頭）與 oidc（以假的身分提供者跑完整的授權碼流程）。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from conftest import DIRECTORY

from tmig_service import create_app
from tmig_service.auth import (
    SESSION_COOKIE,
    Authenticator,
    AuthError,
    AuthSettings,
    OidcSettings,
    ProxySettings,
    SessionCodec,
    load_auth,
    safe_next,
)

ROOT = Path(__file__).resolve().parents[2]
WB = "/api/workbooks/secure"
SHEET = f"{WB}/worksheets/ws_sales_by_region/render"
ALL_REGIONS = {"Central", "East", "West"}
"""看得到全部時：工作表有 Top N（前 3 名）篩選，South 排第 4。"""


def regions(response: httpx.Response) -> set[str]:
    assert response.status_code == 200, response.text
    return {row[0] for row in response.json()["table"]["rows"]}


def app(fixtures: Path, settings: AuthSettings | Authenticator) -> TestClient:
    return TestClient(create_app(fixtures, ROOT / "no-web-dist", auth=settings))


# ---------------------------------------------------------------- none


def test_none_mode(fixtures: Path) -> None:
    client = app(fixtures, AuthSettings())
    assert client.get("/api/me").json() == {"mode": "none", "user": None}
    response = client.post(SHEET)
    assert response.status_code == 422
    assert "需要登入者的身分" in response.json()["detail"]
    assert client.post("/auth/dev", json={"username": "alice"}).status_code == 404
    assert client.get("/auth/login").status_code == 404


# ---------------------------------------------------------------- dev


def test_dev_mode_switches_users(fixtures: Path) -> None:
    client = app(fixtures, AuthSettings(mode="dev", directory=DIRECTORY))
    me = client.get("/api/me").json()
    assert me == {"mode": "dev", "user": None, "users": ["alice", "bob", "carol", "dave"]}
    assert client.get("/api/workbooks").status_code == 401
    assert client.post(SHEET).status_code == 401

    response = client.post("/auth/dev", json={"username": "alice"})
    assert response.json()["user"]["full_name"] == "Alice Wang"
    assert client.get("/api/me").json()["user"]["username"] == "alice"
    assert regions(client.post(SHEET)) == {"East"}
    client.post("/auth/dev", json={"username": "carol"})
    assert regions(client.post(SHEET)) == {"Central", "South"}
    client.post("/auth/dev", json={"username": "dave"})
    assert regions(client.post(SHEET)) == ALL_REGIONS
    assert client.post("/auth/dev", json={"username": "mallory"}).status_code == 403

    client.post("/auth/logout")
    assert client.get("/api/me").json()["user"] is None


def test_url_state_cannot_widen_rls(fixtures: Path) -> None:
    """網址參數或篩選狀態改不了資料源篩選：鍵指向權限欄位也一樣。"""
    client = app(fixtures, AuthSettings(mode="dev", directory=DIRECTORY))
    client.post("/auth/dev", json={"username": "alice"})
    state = {"filters": {"f_visible": {"mode": "all"}, "f_region": {"mode": "all"}}}
    response = client.post(SHEET, json=state)
    assert regions(response) == {"East"}
    assert "f_visible" in response.json()["skipped"]


def test_domain_values_follow_user(fixtures: Path) -> None:
    client = app(fixtures, AuthSettings(mode="dev", directory=DIRECTORY))
    client.post("/auth/dev", json={"username": "bob"})
    values = client.post(f"{WB}/fields/f_category/domain").json()["values"]
    assert values == ["Furniture", "Office Supplies", "Technology"]
    # 篩選控制項之外的欄位不列出可選值
    assert client.post(f"{WB}/fields/f_region/domain").status_code == 403


def test_dev_users_from_settings(fixtures: Path) -> None:
    settings = AuthSettings(mode="dev", dev_users=["bob"], require_directory=False)
    client = app(fixtures, settings)
    assert client.get("/api/me").json()["users"] == ["bob"]
    client.post("/auth/dev", json={"username": "bob"})
    me = client.get("/api/me").json()["user"]
    assert me == {"username": "bob", "full_name": None, "domain": None, "groups": []}
    assert regions(client.post(SHEET)) == {"West"}


# ---------------------------------------------------------------- proxy


def proxy_client(fixtures: Path, host: str, **settings: Any) -> TestClient:
    auth = AuthSettings(
        mode="proxy",
        directory=settings.pop("directory", DIRECTORY),
        proxy=ProxySettings(
            user_header="X-Forwarded-User",
            groups_header="X-Forwarded-Groups",
            trusted_proxies=[ipaddress.ip_network("10.0.0.0/8")],
        ),
        **settings,
    )
    return TestClient(create_app(fixtures, ROOT / "no-web-dist", auth=auth), client=(host, 50000))


def test_proxy_mode(fixtures: Path) -> None:
    client = proxy_client(fixtures, "10.1.2.3", strip_domain=True)
    headers = {"X-Forwarded-User": "CORP\\Alice"}
    assert client.get("/api/me", headers=headers).json()["user"]["username"] == "alice"
    assert regions(client.post(SHEET, headers=headers)) == {"East"}
    # 目錄中的群組為準：標頭的群組不能讓人變成 Managers
    forged = {"X-Forwarded-User": "carol", "X-Forwarded-Groups": "Managers"}
    assert regions(client.post(SHEET, headers=forged)) == {"Central", "South"}
    # email 也能對應
    by_email = {"X-Forwarded-User": "alice.wang@example.com"}
    assert client.get("/api/me", headers=by_email).json()["user"]["username"] == "alice"
    assert client.post(SHEET).status_code == 401
    response = client.post(SHEET, headers={"X-Forwarded-User": "mallory"})
    assert response.status_code == 403
    assert "不在使用者目錄中" in response.json()["detail"]


def test_proxy_rejects_untrusted_peer(fixtures: Path) -> None:
    client = proxy_client(fixtures, "192.168.1.5")
    response = client.post(SHEET, headers={"X-Forwarded-User": "dave"})
    assert response.status_code == 401
    assert "信任的反向代理" in response.json()["detail"]
    assert client.get("/api/me", headers={"X-Forwarded-User": "dave"}).json()["user"] is None


def test_proxy_without_directory_uses_header_groups(fixtures: Path) -> None:
    client = proxy_client(fixtures, "10.0.0.1", directory=None, require_directory=False)
    headers = {"X-Forwarded-User": "zoe", "X-Forwarded-Groups": "Sales, Managers"}
    me = client.get("/api/me", headers=headers).json()["user"]
    assert me["groups"] == ["Sales", "Managers"]
    assert regions(client.post(SHEET, headers=headers)) == ALL_REGIONS


# ---------------------------------------------------------------- oidc

ISSUER = "https://idp.example.com/realms/corp"
CLIENT_ID = "tmig"
CLIENT_SECRET = "s3cret-value-of-the-test-client-0123"


class FakeIdP:
    """假的 OpenID Connect 身分提供者：探索文件、JWKS、權杖端點（檢查 PKCE 與用戶端密鑰）。"""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
        self.codes: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, Any] = {}
        self.overrides: dict[str, Any] = {}
        self.header: dict[str, Any] = {"kid": "k1"}

    def authorize(self, location: str, claims: dict[str, Any]) -> tuple[str, str]:
        """模擬使用者在身分提供者登入：記下 nonce 與 PKCE，回傳（code, state）。"""
        query = parse_qs(urlsplit(location).query)
        assert query["client_id"] == [CLIENT_ID]
        assert query["code_challenge_method"] == ["S256"]
        code = f"code-{len(self.codes)}"
        self.codes[code] = {
            "nonce": query["nonce"][0],
            "challenge": query["code_challenge"][0],
            "claims": claims,
        }
        return code, query["state"][0]

    def id_token(self, pending: dict[str, Any]) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "subject-1",
            "iat": now,
            "exp": now + 300,
            "nonce": pending["nonce"],
            **pending["claims"],
            **self.overrides,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers=self.header)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/certs",
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        if url == f"{ISSUER}/certs":
            return httpx.Response(200, json=self.jwks)
        if url == f"{ISSUER}/token":
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("Authorization") != f"Basic {expected}":
                return httpx.Response(401, json={"error": "invalid_client"})
            form = parse_qs(request.content.decode())
            pending = self.codes.pop(form["code"][0], None)
            if pending is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != pending["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "detail": "PKCE"})
            return httpx.Response(200, json={"id_token": self.id_token(pending)})
        return httpx.Response(404)


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


def oidc_client(
    fixtures: Path, idp: FakeIdP, clock: list[float] | None = None
) -> tuple[TestClient, Authenticator]:
    settings = AuthSettings(
        mode="oidc",
        directory=DIRECTORY,
        strip_domain=True,
        session_secret=b"x" * 32,
        oidc=OidcSettings(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri="http://testserver/auth/callback",
        ),
    )
    now = clock if clock is not None else [time.time()]
    auth = Authenticator(
        settings, httpx.Client(transport=httpx.MockTransport(idp.handler)), lambda: now[0]
    )
    return app(fixtures, auth), auth


def sign_in(
    client: TestClient, idp: FakeIdP, claims: dict[str, Any], next_page: str = "/w/secure"
) -> httpx.Response:
    start = client.get("/auth/login", params={"next": next_page}, follow_redirects=False)
    assert start.status_code == 302
    location = start.headers["location"]
    assert location.startswith(f"{ISSUER}/auth?")
    code, state = idp.authorize(location, claims)
    return client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )


def test_oidc_login_flow(fixtures: Path, idp: FakeIdP) -> None:
    client, _ = oidc_client(fixtures, idp)
    me = client.get("/api/me").json()
    assert me == {"mode": "oidc", "user": None, "login": "/auth/login"}
    assert client.post(SHEET).status_code == 401

    done = sign_in(client, idp, {"preferred_username": "alice.wang@example.com", "name": "A"})
    assert done.status_code == 303
    assert done.headers["location"] == "/w/secure"
    # 依 email 對應到目錄中的 alice；名稱與群組以目錄為準
    user = client.get("/api/me").json()["user"]
    assert user == {
        "username": "alice",
        "full_name": "Alice Wang",
        "domain": "local",
        "groups": ["All Users"],
    }
    assert regions(client.post(SHEET)) == {"East"}

    client.post("/auth/logout")
    assert client.post(SHEET).status_code == 401


def test_oidc_strip_domain_and_groups(fixtures: Path, idp: FakeIdP) -> None:
    client, _ = oidc_client(fixtures, idp)
    sign_in(client, idp, {"preferred_username": "dave@corp.example.com", "groups": ["x"]})
    assert client.get("/api/me").json()["user"]["groups"] == ["All Users", "Managers"]
    assert regions(client.post(SHEET)) == ALL_REGIONS


def test_oidc_rejections(fixtures: Path, idp: FakeIdP) -> None:
    client, _ = oidc_client(fixtures, idp)
    claims = {"preferred_username": "bob"}

    # state 不符
    start = client.get("/auth/login", follow_redirects=False)
    code, _ = idp.authorize(start.headers["location"], claims)
    bad_state = client.get("/auth/callback", params={"code": code, "state": "forged"})
    assert bad_state.status_code == 400

    # aud 不是這個用戶端
    idp.overrides = {"aud": "other-client"}
    assert sign_in(client, idp, claims).status_code == 401
    # nonce 不符（重放別次登入的 ID Token）
    idp.overrides = {"nonce": "replayed"}
    response = sign_in(client, idp, claims)
    assert response.status_code == 401 and "nonce" in response.json()["detail"]
    # 過期
    idp.overrides = {"exp": int(time.time()) - 3600}
    assert sign_in(client, idp, claims).status_code == 401
    # 不在目錄中的帳號
    idp.overrides = {}
    response = sign_in(client, idp, {"preferred_username": "mallory"})
    assert response.status_code == 403
    assert client.get("/api/me").json()["user"] is None


def test_oidc_rejects_symmetric_tokens(fixtures: Path, idp: FakeIdP) -> None:
    """以用戶端密鑰簽的 HS256 權杖一律不接受（避免以公開的金鑰偽造）。"""
    _, auth = oidc_client(fixtures, idp)
    assert auth.oidc is not None
    token = jwt.encode(
        {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "exp": int(time.time()) + 60},
        CLIENT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(Exception, match="不接受的簽章演算法"):
        auth.oidc.verify(token, "n")


def test_oidc_session_expiry_and_tampering(fixtures: Path, idp: FakeIdP) -> None:
    clock = [time.time()]
    client, _ = oidc_client(fixtures, idp, clock)
    sign_in(client, idp, {"preferred_username": "carol"})
    assert regions(client.post(SHEET)) == {"Central", "South"}

    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie is not None
    payload, signature = cookie.split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    forged = base64.urlsafe_b64encode(json.dumps({**data, "login": "dave"}).encode()).rstrip(b"=")
    client.cookies.set(SESSION_COOKIE, f"{forged.decode()}.{signature}")
    assert client.post(SHEET).status_code == 401

    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.post(SHEET).status_code == 200
    clock[0] += 9 * 3600  # 工作階段預設 8 小時
    assert client.post(SHEET).status_code == 401


def test_open_redirect_is_blocked(fixtures: Path, idp: FakeIdP) -> None:
    client, _ = oidc_client(fixtures, idp)
    done = sign_in(client, idp, {"preferred_username": "bob"}, next_page="//evil.example.com/x")
    assert done.headers["location"] == "/"
    assert safe_next("https://evil.example.com") == "/"
    assert safe_next("/\\evil") == "/"
    assert safe_next("/w/secure?case=a") == "/w/secure?case=a"


# ---------------------------------------------------------------- 設定檔


@pytest.fixture
def config_dir(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "directory.json").write_text(DIRECTORY.model_dump_json(), encoding="utf-8")
    yield tmp_path


def write(path: Path, text: str) -> Path:
    file = path / "auth.toml"
    file.write_text(text, encoding="utf-8")
    return file


def test_load_auth_oidc(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMIG_SESSION_SECRET", "z" * 40)
    monkeypatch.setenv("TMIG_OIDC_SECRET", "client-secret")
    file = write(
        config_dir,
        """
[auth]
mode = "oidc"
directory = "directory.json"
strip_domain = true
session_secret = "${TMIG_SESSION_SECRET}"

[auth.oidc]
issuer = "https://idp.example.com/realms/corp/"
client_id = "tmig"
client_secret = "${TMIG_OIDC_SECRET}"
redirect_uri = "https://bi.example.com/auth/callback"
""",
    )
    settings = load_auth(file)
    assert settings.mode == "oidc"
    assert settings.directory is not None and len(settings.directory.users) == 4
    assert settings.oidc is not None
    assert settings.oidc.issuer == "https://idp.example.com/realms/corp"
    assert settings.oidc.client_secret == "client-secret"
    assert settings.secure_cookie is True
    assert settings.session_secret == b"z" * 40


def test_load_auth_rejects_literal_secrets(config_dir: Path) -> None:
    file = write(
        config_dir,
        """
[auth]
mode = "dev"
directory = "directory.json"
session_secret = "this-is-a-literal-secret-that-is-long-enough"
""",
    )
    with pytest.raises(AuthError, match="不能直接寫在設定檔中") as caught:
        load_auth(file)
    assert "literal" not in str(caught.value)


def test_load_auth_missing_env(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMIG_MISSING_SECRET", raising=False)
    file = write(
        config_dir,
        """
[auth]
mode = "oidc"
session_secret = "${TMIG_MISSING_SECRET}"
""",
    )
    with pytest.raises(AuthError, match="TMIG_MISSING_SECRET 未設定"):
        load_auth(file)


def test_load_auth_proxy_and_dev(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = write(
        config_dir,
        """
[auth]
mode = "proxy"
directory = "directory.json"

[auth.proxy]
user_header = "X-Remote-User"
trusted_proxies = ["10.0.0.0/8", "127.0.0.1"]
""",
    )
    settings = load_auth(file)
    assert settings.proxy is not None
    assert settings.proxy.user_header == "X-Remote-User"
    assert settings.proxy.trusts("10.9.8.7") and settings.proxy.trusts("127.0.0.1")
    assert not settings.proxy.trusts("192.168.0.1") and not settings.proxy.trusts("testclient")

    other = config_dir / "other"
    other.mkdir()
    write(other, '[auth]\nmode = "dev"\n')
    with pytest.raises(AuthError, match="dev 模式需要使用者目錄"):
        load_auth(other / "auth.toml")
    write(other, '[auth]\nmode = "sso"\n')
    with pytest.raises(AuthError, match="mode 必須是"):
        load_auth(other / "auth.toml")
    monkeypatch.delenv("TMIG_AUTH", raising=False)
    assert load_auth().mode == "none"
    monkeypatch.setenv("TMIG_AUTH", str(file))
    assert load_auth().mode == "proxy"


def test_session_codec() -> None:
    now = [1000.0]
    codec = SessionCodec(b"k" * 32, lambda: now[0])
    token = codec.dumps({"login": "alice"}, 60)
    assert codec.loads(token) == {"login": "alice", "exp": 1060}
    assert SessionCodec(b"j" * 32, lambda: now[0]).loads(token) is None
    assert codec.loads(token + "x") is None
    assert codec.loads("garbage") is None
    now[0] = 1061
    assert codec.loads(token) is None


def test_example_config_loads(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """service/auth.example.toml 是合法的設定（環境變數齊全時）。"""
    example = (ROOT / "service" / "auth.example.toml").read_text(encoding="utf-8")
    (config_dir / "permissions.json").write_text('{"workbooks": []}', encoding="utf-8")
    monkeypatch.setenv("TMIG_SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("TMIG_OIDC_CLIENT_SECRET", "secret")
    settings = load_auth(write(config_dir, example))
    assert settings.mode == "oidc" and settings.strip_domain
    assert settings.oidc is not None and settings.oidc.username_claim == "preferred_username"
    assert settings.permissions is not None
