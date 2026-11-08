"""桌面版登入伺服器（D0）：沿用伺服器的登入方式（SSO 閘道、OIDC、開發模式），在瀏覽器中核准後發給桌面版一組權杖。

流程（RFC 8252 原生應用程式的作法：系統瀏覽器＋本機回呼網址＋PKCE）：

1. 桌面版開啟系統瀏覽器到 ``/desktop/authorize?redirect_uri=http://127.0.0.1:<埠號>/…&state=…&code_challenge=…``
2. 使用者以平常的方式登入伺服器（oidc 模式沒有登入時先導到登入頁），在核准頁按「允許」
3. 伺服器導回桌面版的本機網址，帶一次性的授權碼（2 分鐘內有效）
4. 桌面版以授權碼與 PKCE 的 code_verifier 換得權杖（``POST /api/desktop-auth/token``）
5. 之後桌面版的請求帶 ``Authorization: Bearer <權杖>``，身分與在瀏覽器中登入的相同

- 回呼網址只接受本機（127.0.0.1、localhost、[::1]）的 http：授權碼不會送到別的電腦
- 核准頁不能被嵌入其他網頁（防點擊劫持），核准的表單帶簽章的票證（防跨站請求）
- 權杖：伺服器只存雜湊值；預設 30 天到期；使用者可以在檢視器撤銷，桌面版登出時撤銷自己的權杖
- 有使用者目錄時每次請求都以目錄判斷群組與站台角色；沒有目錄時用核准當時的群組

API：``GET/POST /desktop/authorize``、``POST /api/desktop-auth/token``、
``GET /api/desktop-auth/tokens``（自己的權杖）、``DELETE /api/desktop-auth/tokens/{id}``（id 為 current 時撤銷目前請求的權杖）
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import html
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig_service.auth import Authenticator

TOKEN_PREFIX = "tmigd_"
CODE_SECONDS = 120
TICKET_SECONDS = 600
TOKEN_DAYS = 30
TOKEN_DAYS_ENV = "TMIG_DESKTOP_TOKEN_DAYS"
LAST_USED_SECONDS = 3600
"""最後使用時間每小時最多寫一次，不是每個請求都寫檔。"""
VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43}")
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def token_days() -> int:
    value = os.environ.get(TOKEN_DAYS_ENV, "")
    return int(value) if value.isdigit() and int(value) > 0 else TOKEN_DAYS


def challenge_of(verifier: str) -> str:
    """PKCE 的 S256：base64url(sha256(verifier))，不含補齊的 =。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def loopback_uri(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in LOOPBACK
        and port is not None
        and not parts.username
        and not parts.password
        and not parts.fragment
    )


def _iso(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 權杖


class TokenEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    digest: str = Field(description="權杖的 SHA-256（不存權杖本身）")
    login: str
    groups: list[str] = Field(default_factory=list)
    name: str | None = None
    label: str = ""
    created_at: float
    expires_at: float
    last_used_at: float | None = None


class TokenFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: list[TokenEntry] = Field(default_factory=list)


class TokenStore:
    """桌面版的權杖（JSON 檔，先寫暫存檔再取代）；過期的權杖在下次寫檔時清掉。"""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: tuple[int, list[TokenEntry]] | None = None

    def _load(self) -> list[TokenEntry]:
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return []
        if self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        try:
            entries = TokenFile.model_validate_json(self.path.read_bytes()).tokens
        except (OSError, ValidationError):
            entries = []
        self._cache = (stamp, entries)
        return entries

    def _save(self, entries: list[TokenEntry]) -> None:
        now = self.clock()
        kept = [e for e in entries if e.expires_at > now]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(TokenFile(tokens=kept).model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        self._cache = None

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(
        self, login: str, groups: list[str], name: str | None, label: str, days: int
    ) -> tuple[TokenEntry, str]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = self.clock()
        entry = TokenEntry(
            id=secrets.token_hex(6),
            digest=self.digest(token),
            login=login,
            groups=groups,
            name=name,
            label=label[:100],
            created_at=now,
            expires_at=now + days * 86400,
        )
        with self._lock:
            self._save([*self._load(), entry])
        return entry, token

    def verify(self, token: str) -> TokenEntry | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        wanted = self.digest(token)
        now = self.clock()
        with self._lock:
            entries = self._load()
            found = next((e for e in entries if hmac.compare_digest(e.digest, wanted)), None)
            if found is None or found.expires_at <= now:
                return None
            if found.last_used_at is None or now - found.last_used_at > LAST_USED_SECONDS:
                updated = found.model_copy(update={"last_used_at": now})
                self._save([updated if e.id == found.id else e for e in entries])
                return updated
            return found

    def owned(self, login: str) -> list[TokenEntry]:
        now = self.clock()
        return [e for e in self._load() if e.login == login and e.expires_at > now]

    def revoke(self, token_id: str, login: str) -> bool:
        with self._lock:
            entries = self._load()
            kept = [e for e in entries if not (e.id == token_id and e.login == login)]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True


# ---------------------------------------------------------------- 頁面

PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
        "form-action 'self' http://127.0.0.1:* http://localhost:* http://[::1]:*"
    ),
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,"Microsoft JhengHei",sans-serif;background:#f3f5f8;color:#1f2933;margin:0}}
main{{max-width:460px;margin:12vh auto;background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:28px 32px}}
h1{{font-size:20px;margin:0 0 12px}} p{{line-height:1.6}} .who{{font-weight:600}}
.actions{{display:flex;gap:12px;margin-top:24px}}
button{{font:inherit;padding:8px 18px;border-radius:6px;border:1px solid #9aa5b1;background:#fff;cursor:pointer}}
button.primary{{background:#2f5d8a;border-color:#2f5d8a;color:#fff}}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>""",
        status_code=status,
        headers=PAGE_HEADERS,
    )


def message(title: str, text: str, status: int) -> HTMLResponse:
    return page(title, f"<p>{html.escape(text)}</p>", status)


@dataclass
class PendingCode:
    login: str
    groups: list[str]
    name: str | None
    label: str
    challenge: str
    redirect_uri: str
    expires: float


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    code_verifier: str
    redirect_uri: str


def _with_query(uri: str, values: dict[str, str]) -> str:
    return f"{uri}{'&' if '?' in uri else '?'}{urlencode(values)}"


def desktop_auth_router(auth: Authenticator, tokens: TokenStore) -> APIRouter:
    router = APIRouter()
    codes: dict[str, PendingCode] = {}
    lock = threading.Lock()

    def sign_in_disabled() -> bool:
        return auth.mode in ("none", "internal", "desktop")

    @router.get("/desktop/authorize", include_in_schema=False)
    def authorize(
        request: Request,
        redirect_uri: str = "",
        state: str = "",
        code_challenge: str = "",
        code_challenge_method: str = "",
        label: str = "",
    ) -> Response:
        if sign_in_disabled():
            return message(
                "桌面版登入", "這台伺服器沒有設定身分驗證：桌面版不能登入，也不能發布報表。", 400
            )
        if not loopback_uri(redirect_uri):
            return message("桌面版登入", "回呼網址不正確：只接受這台電腦本機的網址。", 400)
        if not state or len(state) > 256 or code_challenge_method != "S256":
            return message("桌面版登入", "登入的參數不完整，請從桌面版重新登入。", 400)
        if not CHALLENGE.fullmatch(code_challenge):
            return message("桌面版登入", "登入的參數不正確，請從桌面版重新登入。", 400)
        if request.headers.get("authorization"):
            return message("桌面版登入", "請在瀏覽器中開啟這個網址。", 400)
        try:
            identity = auth.identity(request)
        except HTTPException as exc:
            if exc.status_code == 401 and auth.mode == "oidc":
                target = f"{request.url.path}?{request.url.query}"
                return RedirectResponse("/auth/login?" + urlencode({"next": target}), 302)
            if exc.status_code == 401 and auth.mode == "dev":
                return page(
                    "桌面版登入",
                    '<p>開發模式：請先在<a href="/" target="_blank" rel="noopener">檢視器</a>選擇使用者，'
                    "再回到這個分頁重新整理。</p>",
                    401,
                )
            return message("桌面版登入", str(exc.detail), exc.status_code)
        assert identity is not None
        ticket = auth.codec.dumps(
            {
                "kind": "desktop-authorize",
                "login": identity.username,
                "redirect_uri": redirect_uri,
                "state": state,
                "challenge": code_challenge,
                "label": label[:100],
            },
            TICKET_SECONDS,
        )
        who = html.escape(identity.full_name or identity.username)
        device = f"（{html.escape(label[:100])}）" if label else ""
        return page(
            "允許報表桌面版登入？",
            f"""<p>報表桌面版{device}要以 <span class="who">{who}</span> 的身分登入這台伺服器，
用來從伺服器開啟報表與發布報表。</p>
<p>只有在你自己剛從桌面版按「登入伺服器」時才按「允許」。</p>
<form method="post" action="/desktop/authorize">
<input type="hidden" name="ticket" value="{html.escape(ticket)}">
<div class="actions"><button class="primary" name="decision" value="allow">允許</button>
<button name="decision" value="deny">拒絕</button></div></form>""",
        )

    @router.post("/desktop/authorize", include_in_schema=False)
    async def approve(request: Request) -> Response:
        if sign_in_disabled():
            return message("桌面版登入", "這台伺服器沒有設定身分驗證。", 400)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        ticket = auth.codec.loads((form.get("ticket") or [""])[0])
        if ticket is None or ticket.get("kind") != "desktop-authorize":
            return message("桌面版登入", "核准頁已經逾時，請從桌面版重新登入。", 400)
        try:
            identity = auth.identity(request)
        except HTTPException as exc:
            return message("桌面版登入", str(exc.detail), exc.status_code)
        if identity is None or identity.username != ticket.get("login"):
            return message("桌面版登入", "登入的使用者已經不同，請從桌面版重新登入。", 403)
        redirect_uri, state = str(ticket["redirect_uri"]), str(ticket["state"])
        if (form.get("decision") or [""])[0] != "allow":
            return RedirectResponse(
                _with_query(redirect_uri, {"error": "access_denied", "state": state}), 303
            )
        code = secrets.token_urlsafe(32)
        now = time.time()
        with lock:
            for key in [k for k, v in codes.items() if v.expires <= now]:
                del codes[key]
            codes[code] = PendingCode(
                login=identity.username,
                groups=list(identity.groups),
                name=identity.full_name,
                label=str(ticket.get("label") or ""),
                challenge=str(ticket["challenge"]),
                redirect_uri=redirect_uri,
                expires=now + CODE_SECONDS,
            )
        return RedirectResponse(_with_query(redirect_uri, {"code": code, "state": state}), 303)

    @router.post("/api/desktop-auth/token")
    def token(body: TokenRequest) -> dict[str, Any]:
        with lock:
            pending = codes.pop(body.code, None)
        invalid = HTTPException(400, "授權碼無效或已逾時，請從桌面版重新登入")
        if pending is None or pending.expires <= time.time():
            raise invalid
        if pending.redirect_uri != body.redirect_uri or not VERIFIER.fullmatch(body.code_verifier):
            raise invalid
        if not hmac.compare_digest(challenge_of(body.code_verifier), pending.challenge):
            raise invalid
        entry, value = tokens.create(
            pending.login, pending.groups, pending.name, pending.label, token_days()
        )
        return {
            "token": value,
            "username": entry.login,
            "name": entry.name,
            "expires_at": _iso(entry.expires_at),
        }

    def require_user(request: Request) -> str:
        identity = auth.identity(request)
        if identity is None:
            raise HTTPException(401, "沒有登入者")
        return identity.username

    @router.get("/api/desktop-auth/tokens")
    def my_tokens(request: Request) -> list[dict[str, Any]]:
        login = require_user(request)
        current = getattr(request.state, "desktop_token", None)
        return [
            {
                "id": e.id,
                "label": e.label,
                "created_at": _iso(e.created_at),
                "expires_at": _iso(e.expires_at),
                "last_used_at": _iso(e.last_used_at) if e.last_used_at else None,
                "current": e.id == current,
            }
            for e in sorted(tokens.owned(login), key=lambda e: e.created_at, reverse=True)
        ]

    @router.delete("/api/desktop-auth/tokens/{token_id}")
    def revoke(token_id: str, request: Request) -> dict[str, bool]:
        login = require_user(request)
        if token_id == "current":
            token_id = getattr(request.state, "desktop_token", None) or ""
        if not token_id or not tokens.revoke(token_id, login):
            raise HTTPException(404, "找不到這個桌面版的登入")
        return {"ok": True}

    return router


__all__ = [
    "TOKEN_PREFIX",
    "TokenEntry",
    "TokenStore",
    "challenge_of",
    "desktop_auth_router",
    "loopback_uri",
]
