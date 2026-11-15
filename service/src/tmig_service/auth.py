"""身分驗證（第三階段 P2）：查詢服務與檢視器要求登入者的身分，查詢依此套用列層級權限（P1）。

設定檔為 TOML（環境變數 TMIG_AUTH，或 ``tmig serve --auth``）；沒有設定檔時為 none 模式。

    [auth]
    mode = "oidc"                         # none、dev、proxy、oidc
    directory = "directory.json"          # Tableau Server 的使用者與群組（相對於設定檔；見 tmig.access）
    permissions = "permissions.json"      # Project、workbook、view 的權限（P3；見 tmig_service.access）
    strip_domain = true                   # alice@corp.example.com、CORP\\alice → alice
    session_secret = "${TMIG_SESSION_SECRET}"

    [auth.proxy]
    user_header = "X-Forwarded-User"
    groups_header = "X-Forwarded-Groups"  # 選填，逗號分隔
    trusted_proxies = ["127.0.0.1", "10.0.0.0/8"]

    [auth.oidc]
    issuer = "https://login.example.com/realms/corp"
    client_id = "tmig"
    client_secret = "${TMIG_OIDC_CLIENT_SECRET}"
    redirect_uri = "https://bi.example.com/auth/callback"

模式：

- none：不驗證（本機檢視與示範）；用到使用者函式的報表無法查詢
- dev：開發模式，檢視器可以切換使用者（目錄中的使用者或 [auth.dev] 的 users）；不可用於正式環境
- proxy：公司的 SSO 閘道（反向代理）驗證後以標頭傳入帳號；只信任來自 trusted_proxies 的連線
- oidc：查詢服務自己以 OpenID Connect 登入（授權碼＋PKCE），以簽章的 cookie 維持工作階段

有目錄時，登入的帳號依名稱對應到目錄中的使用者（USERNAME() 與 ISMEMBEROF() 以目錄為準），
不在目錄中的帳號拒絕（require_directory = false 時改用標頭或宣告中的群組）。
密鑰（session_secret、client_secret）只能寫成 ${環境變數}，不寫進設定檔。

桌面版（D0）：在瀏覽器中核准後拿到權杖（見 tmig_service.desktop_auth），請求帶 ``Authorization: Bearer``，
dev、proxy、oidc 模式都接受；proxy 模式下反向代理要放行帶這個標頭的請求。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict

from tmig.access.directory import Directory, DirectoryError, load_directory, login_name
from tmig.access.permissions import ADMIN_ROLES, Permissions, PermissionsError, load_permissions
from tmig.data import initial_sql
from tmig.data.sources import ENV_REF
from tmig.planner.identity import Identity

AUTH_ENV = "TMIG_AUTH"
SESSION_COOKIE = "tmig_session"
LOGIN_COOKIE = "tmig_login"
MODES = ("none", "dev", "proxy", "oidc")
"""設定檔可用的模式。"""
Mode = Literal["none", "dev", "proxy", "oidc", "internal", "desktop"]
"""internal：只接受以伺服器金鑰簽章的工作階段 cookie，沒有登入入口；訂閱（Q3）以訂閱者的身分產生檔案時，
由排程器啟動只在本機的查詢服務使用，不能寫在設定檔中。
desktop：桌面版的本機引擎，登入者固定為作業系統的帳號（見 tmig_service.desktop），不能寫在設定檔中。"""
SIGNING_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")
"""ID Token 接受的簽章演算法：只接受非對稱金鑰（不接受 none 與以 client_secret 簽的 HS256）。"""


class AuthError(RuntimeError):
    """身分驗證的設定錯誤（訊息不含密鑰）。"""


# ---------------------------------------------------------------- 設定


@dataclass
class ProxySettings:
    user_header: str = "X-Forwarded-User"
    groups_header: str | None = None
    name_header: str | None = None
    trusted_proxies: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(
        default_factory=lambda: [ipaddress.ip_network("127.0.0.1/32")]
    )

    def trusts(self, host: str | None) -> bool:
        if not host:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self.trusted_proxies)


@dataclass
class OidcSettings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str] = field(default_factory=lambda: ["openid", "profile", "email"])
    username_claim: str = "preferred_username"
    groups_claim: str | None = "groups"
    name_claim: str | None = "name"


@dataclass
class AuthSettings:
    mode: Mode = "none"
    directory: Directory | None = None
    require_directory: bool = True
    strip_domain: bool = False
    default_domain: str | None = None
    session_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    session_hours: float = 8.0
    secure_cookie: bool = False
    proxy: ProxySettings | None = None
    oidc: OidcSettings | None = None
    dev_users: list[str] = field(default_factory=list)
    permissions: Permissions | None = None
    """Tableau Server 匯出的權限（P3）；沒有時只要求登入，不限制看哪些報表。"""


def _secret(section: dict[str, Any], key: str, where: str) -> str | None:
    """密鑰只能寫成 ${環境變數}；缺少環境變數時報錯（訊息不含任何值）。"""
    value = section.get(key)
    if value is None:
        return None
    match = ENV_REF.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise AuthError(f"{where} 的 {key} 不能直接寫在設定檔中，請改用 ${{環境變數}}")
    name = match.group(1)
    resolved = os.environ.get(name)
    if not resolved:
        raise AuthError(f"{where} 的 {key} 需要的環境變數 {name} 未設定")
    return resolved


def _strings(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AuthError(f"{where} 必須是字串清單")
    return list(value)


def auth_path(path: str | Path | None = None) -> Path | None:
    if path is not None:
        return Path(path)
    value = os.environ.get(AUTH_ENV)
    return Path(value) if value else None


def load_auth(path: str | Path | None = None) -> AuthSettings:
    """讀取身分驗證的設定檔；沒有指定、也沒有環境變數 TMIG_AUTH 時為 none 模式。"""
    file = auth_path(path)
    if file is None:
        return AuthSettings()
    if not file.is_file():
        raise AuthError(f"找不到身分驗證設定檔 {file}")
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise AuthError(f"身分驗證設定檔 {file} 格式錯誤：{exc}") from exc
    section = data.get("auth")
    if not isinstance(section, dict):
        raise AuthError(f"身分驗證設定檔 {file} 缺少 [auth]")
    mode = section.get("mode", "none")
    if mode not in MODES:
        raise AuthError(f"[auth] 的 mode 必須是 {'、'.join(MODES)} 之一（目前為 {mode!r}）")
    settings = AuthSettings(mode=mode)
    directory = section.get("directory")
    if directory is not None:
        try:
            settings.directory = load_directory(file.parent / str(directory))
        except DirectoryError as exc:
            raise AuthError(str(exc)) from exc
    permissions = section.get("permissions")
    if permissions is not None:
        if mode == "none":
            raise AuthError("[auth] 設定了 permissions，但 mode 為 none：權限需要登入者的身分")
        try:
            settings.permissions = load_permissions(file.parent / str(permissions))
        except PermissionsError as exc:
            raise AuthError(str(exc)) from exc
    settings.require_directory = bool(section.get("require_directory", True))
    settings.strip_domain = bool(section.get("strip_domain", False))
    settings.default_domain = section.get("default_domain")
    settings.session_hours = float(section.get("session_hours", 8))
    secret = _secret(section, "session_secret", "[auth]")
    if secret is not None:
        if len(secret) < 32:
            raise AuthError("[auth] 的 session_secret 至少要 32 個字元")
        settings.session_secret = secret.encode("utf-8")
    elif mode == "oidc":
        raise AuthError("oidc 模式需要 [auth] 的 session_secret（工作階段 cookie 的簽章金鑰）")

    if mode == "proxy":
        proxy = section.get("proxy") or {}
        if not isinstance(proxy, dict):
            raise AuthError("[auth.proxy] 格式錯誤")
        try:
            networks = [
                ipaddress.ip_network(n, strict=False)
                for n in _strings(proxy.get("trusted_proxies", ["127.0.0.1"]), "trusted_proxies")
            ]
        except ValueError as exc:
            raise AuthError(f"[auth.proxy] 的 trusted_proxies 格式錯誤：{exc}") from exc
        settings.proxy = ProxySettings(
            user_header=str(proxy.get("user_header", "X-Forwarded-User")),
            groups_header=proxy.get("groups_header"),
            name_header=proxy.get("name_header"),
            trusted_proxies=networks,
        )
    if mode == "oidc":
        oidc = section.get("oidc") or {}
        if not isinstance(oidc, dict):
            raise AuthError("[auth.oidc] 格式錯誤")
        missing = [k for k in ("issuer", "client_id", "redirect_uri") if not oidc.get(k)]
        if missing:
            raise AuthError(f"[auth.oidc] 缺少 {'、'.join(missing)}")
        client_secret = _secret(oidc, "client_secret", "[auth.oidc]")
        if client_secret is None:
            raise AuthError("[auth.oidc] 缺少 client_secret")
        redirect_uri = str(oidc["redirect_uri"])
        settings.oidc = OidcSettings(
            issuer=str(oidc["issuer"]).rstrip("/"),
            client_id=str(oidc["client_id"]),
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=_strings(oidc.get("scopes"), "scopes") or ["openid", "profile", "email"],
            username_claim=str(oidc.get("username_claim", "preferred_username")),
            groups_claim=oidc.get("groups_claim", "groups"),
            name_claim=oidc.get("name_claim", "name"),
        )
        settings.secure_cookie = redirect_uri.startswith("https://")
    if mode == "dev":
        settings.dev_users = _strings(section.get("dev", {}).get("users"), "[auth.dev] users")
        if not settings.dev_users and settings.directory is None:
            raise AuthError("dev 模式需要使用者目錄（directory）或 [auth.dev] 的 users")
    settings.secure_cookie = bool(section.get("secure_cookie", settings.secure_cookie))
    return settings


# ---------------------------------------------------------------- 簽章的 cookie


class SessionCodec:
    """以 HMAC-SHA256 簽章的 JSON（base64url）：內容可讀但不能竄改，附到期時間。"""

    def __init__(self, secret: bytes, clock: Callable[[], float] = time.time) -> None:
        self.secret = secret
        self.clock = clock

    def _sign(self, payload: bytes) -> str:
        digest = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def dumps(self, data: dict[str, Any], seconds: float) -> str:
        body = {**data, "exp": int(self.clock() + seconds)}
        payload = base64.urlsafe_b64encode(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        ).rstrip(b"=")
        return f"{payload.decode()}.{self._sign(payload)}"

    def loads(self, token: str | None) -> dict[str, Any] | None:
        if not token or token.count(".") != 1:
            return None
        payload, signature = token.split(".")
        if not hmac.compare_digest(self._sign(payload.encode()), signature):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except ValueError:
            return None
        if not isinstance(data, dict) or float(data.get("exp", 0)) < self.clock():
            return None
        return data


def safe_next(target: str | None) -> str:
    """登入後回到的頁面：只接受本站的相對路徑（避免被導向其他網站）。"""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


# ---------------------------------------------------------------- OpenID Connect


class OidcClient:
    """授權碼＋PKCE：探索文件、權杖交換與 ID Token 驗章（JWKS 快取，遇到新的 kid 時重新取得）。"""

    def __init__(self, settings: OidcSettings, http: httpx.Client, clock: Callable[[], float]):
        self.settings = settings
        self.http = http
        self.clock = clock
        self._metadata: dict[str, Any] | None = None
        self._keys: dict[str, Any] = {}

    def metadata(self) -> dict[str, Any]:
        if self._metadata is not None:
            return self._metadata
        url = f"{self.settings.issuer}/.well-known/openid-configuration"
        response = self.http.get(url)
        if response.status_code != 200:
            raise HTTPException(502, f"無法取得 OIDC 設定（HTTP {response.status_code}）")
        data: dict[str, Any] = response.json()
        if str(data.get("issuer", "")).rstrip("/") != self.settings.issuer:
            raise HTTPException(502, "OIDC 設定的 issuer 與設定檔不同")
        self._metadata = data
        return data

    def authorize_url(self, state: str, nonce: str, verifier: str) -> str:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(self.settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.metadata()['authorization_endpoint']}?{query}"

    def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        meta = self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "code_verifier": verifier,
        }
        methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if "client_secret_basic" in methods:
            credentials = (self.settings.client_id, self.settings.client_secret)
            response = self.http.post(meta["token_endpoint"], data=form, auth=credentials)
        else:
            form |= {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            }
            response = self.http.post(meta["token_endpoint"], data=form)
        if response.status_code != 200:
            raise HTTPException(401, f"登入失敗：權杖交換被拒（HTTP {response.status_code}）")
        return response.json()

    def _key(self, kid: str | None, refresh: bool = False) -> Any:
        if refresh or not self._keys:
            response = self.http.get(self.metadata()["jwks_uri"])
            if response.status_code != 200:
                raise HTTPException(502, f"無法取得 OIDC 金鑰（HTTP {response.status_code}）")
            self._keys = {}
            for item in response.json().get("keys", []):
                try:
                    self._keys[item.get("kid") or ""] = jwt.PyJWK(item).key
                except jwt.PyJWKError:
                    continue  # 不支援的金鑰類型（例如加密用的金鑰）
        if kid in self._keys:
            return self._keys[kid]
        if kid is None and len(self._keys) == 1:
            return next(iter(self._keys.values()))
        if not refresh:
            return self._key(kid, refresh=True)
        raise HTTPException(401, "登入失敗：ID Token 的簽章金鑰不明")

    def verify(self, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
            algorithm = header.get("alg")
            if algorithm not in SIGNING_ALGORITHMS:
                raise HTTPException(401, f"登入失敗：不接受的簽章演算法 {algorithm}")
            claims = jwt.decode(
                id_token,
                key=self._key(header.get("kid")),
                algorithms=[algorithm],
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(401, f"登入失敗：ID Token 無效（{exc}）") from exc
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise HTTPException(401, "登入失敗：ID Token 的 nonce 不符")
        return claims


# ---------------------------------------------------------------- 驗證


class SiteSource(Protocol):
    """站台資料（tmig_service.site.SiteStore）：使用者目錄。"""

    def directory(self) -> Directory: ...


class TokenIdentity(Protocol):
    """桌面版的權杖對應的登入者（tmig_service.desktop_auth.TokenEntry）。"""

    @property
    def id(self) -> str: ...
    @property
    def login(self) -> str: ...
    @property
    def groups(self) -> list[str]: ...
    @property
    def name(self) -> str | None: ...


class BearerTokens(Protocol):
    def verify(self, token: str) -> TokenIdentity | None: ...


def bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


class SessionUser(BaseModel):
    """工作階段 cookie 的內容：登入的帳號，與標頭或宣告中的群組、名稱（沒有目錄時使用）。"""

    model_config = ConfigDict(extra="ignore")

    login: str
    groups: list[str] = []
    name: str | None = None


class Authenticator:
    def __init__(
        self,
        settings: AuthSettings,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.codec = SessionCodec(settings.session_secret, clock)
        self.oidc = (
            OidcClient(settings.oidc, http or httpx.Client(timeout=30), clock)
            if settings.oidc is not None
            else None
        )
        self.tokens: BearerTokens | None = None
        """桌面版的權杖（有報表庫時由查詢服務設定）。"""
        self.site: SiteSource | None = None
        """站台資料（有報表庫時由查詢服務設定）：使用者目錄以它為準，修改後立即生效。"""

    def directory(self) -> Directory | None:
        if self.site is not None:
            return self.site.directory()
        return self.settings.directory

    @property
    def mode(self) -> Mode:
        return self.settings.mode

    def sql_session(self, identity: Identity | None, workbook: str = "") -> initial_sql.Session:
        """連線的 Initial SQL 代入參數的環境（tmig.data.initial_sql）：這位登入者在伺服器上看這份報表。"""
        if identity is None:
            return initial_sql.server_session(workbook=workbook)
        return initial_sql.server_session(identity.username, identity.domain, workbook)

    def resolve(self, login: str, groups: Sequence[str] = (), name: str | None = None) -> Identity:
        """登入的帳號 → 身分：有目錄時以目錄為準，否則用標頭或宣告中的群組。"""
        directory = self.directory()
        if directory is not None:
            user = directory.find(login)
            if user is None and self.settings.strip_domain:
                user = directory.find(login_name(login, True))
            if user is not None:
                return user.identity()
            if self.settings.require_directory:
                raise HTTPException(
                    403, f"帳號 {login} 不在使用者目錄中（Tableau Server 的使用者）"
                )
        username = login_name(login, self.settings.strip_domain)
        if not username:
            raise HTTPException(401, "沒有登入的帳號")
        return Identity(
            username=username,
            full_name=name,
            domain=self.settings.default_domain,
            groups=tuple(g for g in groups if g),
        )

    def identity(self, request: Request) -> Identity | None:
        """目前請求的登入者；none 模式為 None，其他模式沒有登入時回應 401。"""
        mode = self.mode
        if mode == "none":
            return None
        if self.tokens is not None and mode in MODES:
            bearer = bearer_token(request)
            if bearer is not None:
                entry = self.tokens.verify(bearer)
                if entry is None:
                    raise HTTPException(401, "桌面版的登入已失效或已登出，請重新登入")
                request.state.desktop_token = entry.id
                return self.resolve(entry.login, entry.groups, entry.name)
        if mode == "proxy":
            return self._proxy(request)
        session = self.codec.loads(request.cookies.get(SESSION_COOKIE))
        if session is None:
            message = {"oidc": "請先登入", "dev": "請選擇使用者（開發模式）"}.get(
                mode, "沒有登入者"
            )
            raise HTTPException(401, message)
        user = SessionUser.model_validate(session)
        return self.resolve(user.login, user.groups, user.name)

    def _proxy(self, request: Request) -> Identity:
        proxy = self.settings.proxy
        assert proxy is not None
        host = request.client.host if request.client else None
        if not proxy.trusts(host):
            raise HTTPException(401, "請求不是經由信任的反向代理傳入，不接受身分標頭")
        login = request.headers.get(proxy.user_header)
        if not login:
            raise HTTPException(401, f"反向代理沒有傳入 {proxy.user_header}")
        groups: list[str] = []
        if proxy.groups_header:
            groups = [g.strip() for g in request.headers.get(proxy.groups_header, "").split(",")]
        name = request.headers.get(proxy.name_header) if proxy.name_header else None
        return self.resolve(login, groups, name)

    def dev_users(self) -> list[str]:
        if self.settings.dev_users:
            return list(self.settings.dev_users)
        directory = self.directory()
        return [u.name for u in directory.users] if directory is not None else []

    def session_token(self, login: str, seconds: float | None = None) -> str:
        """工作階段 cookie 的值（訂閱以訂閱者的身分開啟檢視器時使用）。"""
        hours = self.settings.session_hours * 3600
        return self.codec.dumps(SessionUser(login=login).model_dump(), seconds or hours)

    def session_cookie(self, response: Response, user: SessionUser) -> None:
        seconds = self.settings.session_hours * 3600
        response.set_cookie(
            SESSION_COOKIE,
            self.codec.dumps(user.model_dump(), seconds),
            max_age=int(seconds),
            httponly=True,
            samesite="lax",
            secure=self.settings.secure_cookie,
            path="/",
        )


def user_json(identity: Identity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "username": identity.username,
        "full_name": identity.full_name,
        "domain": identity.domain,
        "groups": list(identity.groups),
        "site_role": identity.site_role,
        "admin": (identity.site_role or "") in ADMIN_ROLES,
    }


class DevLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str


def auth_router(auth: Authenticator) -> APIRouter:
    router = APIRouter()

    @router.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        """目前的登入者與身分驗證模式；沒有登入時 user 為 null（檢視器依此顯示登入或切換使用者）。"""
        try:
            identity = auth.identity(request)
        except HTTPException as exc:
            if exc.status_code not in (401, 403):
                raise
            identity = None
        result: dict[str, Any] = {"mode": auth.mode, "user": user_json(identity)}
        if auth.mode == "oidc":
            result["login"] = "/auth/login"
        if auth.mode == "dev":
            result["users"] = auth.dev_users()
        return result

    @router.post("/auth/dev")
    def dev_login(body: DevLogin) -> Response:
        if auth.mode != "dev":
            raise HTTPException(404, "只有開發模式可以切換使用者")
        if body.username not in auth.dev_users():
            raise HTTPException(403, f"開發模式沒有使用者 {body.username}")
        identity = auth.resolve(body.username)
        response = JSONResponse({"user": user_json(identity)})
        auth.session_cookie(response, SessionUser(login=body.username))
        return response

    @router.get("/auth/login")
    def login(next: str | None = None) -> Response:
        if auth.oidc is None:
            raise HTTPException(404, "只有 oidc 模式需要登入")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        response = RedirectResponse(auth.oidc.authorize_url(state, nonce, verifier), 302)
        payload = {"state": state, "nonce": nonce, "verifier": verifier, "next": safe_next(next)}
        response.set_cookie(
            LOGIN_COOKIE,
            auth.codec.dumps(payload, 600),
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=auth.settings.secure_cookie,
            path="/auth/",
        )
        return response

    @router.get("/auth/callback")
    def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        if auth.oidc is None or auth.settings.oidc is None:
            raise HTTPException(404, "只有 oidc 模式需要登入")
        if error:
            raise HTTPException(401, f"登入失敗：{error}")
        pending = auth.codec.loads(request.cookies.get(LOGIN_COOKIE))
        if pending is None or not code or not state:
            raise HTTPException(400, "登入逾時或不完整，請重新登入")
        if not hmac.compare_digest(str(pending["state"]), state):
            raise HTTPException(400, "登入的 state 不符，請重新登入")
        tokens = auth.oidc.exchange(code, str(pending["verifier"]))
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise HTTPException(401, "登入失敗：沒有 ID Token")
        claims = auth.oidc.verify(id_token, str(pending["nonce"]))
        settings = auth.settings.oidc
        login_value = claims.get(settings.username_claim)
        if not isinstance(login_value, str) or not login_value:
            raise HTTPException(401, f"登入失敗：ID Token 沒有 {settings.username_claim}")
        raw_groups = claims.get(settings.groups_claim) if settings.groups_claim else None
        groups = [str(g) for g in raw_groups] if isinstance(raw_groups, list) else []
        name = claims.get(settings.name_claim) if settings.name_claim else None
        user = SessionUser(
            login=login_value, groups=groups, name=name if isinstance(name, str) else None
        )
        auth.resolve(user.login, user.groups, user.name)  # 不在目錄中的帳號在這裡就拒絕
        response = RedirectResponse(safe_next(str(pending.get("next") or "/")), 303)
        auth.session_cookie(response, user)
        response.delete_cookie(LOGIN_COOKIE, path="/auth/")
        return response

    @router.post("/auth/logout")
    def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router


__all__ = [
    "AUTH_ENV",
    "AuthError",
    "AuthSettings",
    "Authenticator",
    "BearerTokens",
    "OidcSettings",
    "ProxySettings",
    "SessionCodec",
    "auth_router",
    "load_auth",
    "safe_next",
]
