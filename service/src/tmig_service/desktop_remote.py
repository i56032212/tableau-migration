"""桌面版連到伺服器（D0）：登入、從伺服器開啟、發布。本機引擎代替檢視器呼叫伺服器，權杖只在引擎中。

- ``GET  /api/desktop/server``：目前的伺服器與登入狀態
- ``POST /api/desktop/server/signin`` ``{"url": "https://bi.example.com"}``：開啟系統瀏覽器到伺服器的核准頁；
  使用者按「允許」後伺服器導回本機引擎的 ``/desktop-auth/callback``，引擎換得權杖存到認證管理員
- ``POST /api/desktop/server/signout``：在伺服器撤銷權杖，並從認證管理員刪除
- ``GET  /api/desktop/server/projects``、``GET /api/desktop/server/reports``：伺服器上的 Project 與報表
- ``POST /api/desktop/server/open`` ``{"report": "<id>", "version": 3}``：下載並開啟（記下版本，省略為最新版）
- ``POST /api/desktop/server/browse`` ``{"path": "/w/<id>"}``：在系統瀏覽器開啟伺服器上的網頁
- ``POST /api/desktop/server/publish``：先在本機檢查，存成報表套件上傳；伺服器的回應（同名、衝突、
  檢查沒有通過）原樣轉給檢視器。發布後記下伺服器上的報表與版本：下一次發布預設覆蓋同一份，
  並以這一版為基準判斷伺服器上是否已有別人發布的新版本
- ``GET  /api/desktop/server/datasources?workbook=<id>``：報表的資料源，與已經連到的伺服器上的資料源
- ``POST /api/desktop/server/publish-datasource``：發布報表中的一個資料源（與 Tableau Desktop 的「發布資料源」
  相同）：存成資料源套件（定義、用到的參數與萃取）上傳；``link`` 時報表改用發布的資料源（之後發布這份報表，
  伺服器上的報表直接使用伺服器上的資料源，萃取更新一次每份報表都看到新的資料）

設定檔（desktop-server.json）只記伺服器網址與帳號，權杖存在認證管理員（見 tmig_service.credentials）。
連線用作業系統的憑證存放區（truststore）：公司內部 CA 簽發的伺服器憑證不必另外設定。
代理伺服器依系統設定（Windows 的網際網路選項或 HTTPS_PROXY 等環境變數），但本機的網址與例外清單
（Windows 的 ProxyOverride，例如 ``*.corp.local;<local>``，或 NO_PROXY）中的伺服器直接連線。
"""

from __future__ import annotations

import contextlib
import html
import json
import secrets
import socket
import ssl
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from tmig.ir import IR, ExtractConnection
from tmig.ir.models import PublishedSource
from tmig.package import (
    DATASOURCE_KIND,
    PackageError,
    datasource_ir,
    read_package,
    write_package,
)
from tmig.verify.run import ensure_data
from tmig_service.app import Workspace
from tmig_service.checks import check_report, errors
from tmig_service.credentials import CredentialStore, target_for
from tmig_service.desktop_auth import challenge_of

CALLBACK_PATH = "/desktop-auth/callback"
SETTINGS_FILE = "desktop-server.json"
SIGN_IN_SECONDS = 600
MEDIA_TYPE = "application/vnd.tmig.tmigx"
DATASOURCE_MEDIA_TYPE = "application/vnd.tmig.tmigds"


LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def bypass_proxy(url: str) -> bool:
    """不經過代理伺服器：本機的網址，與系統的例外清單中的伺服器。

    Python 在 Windows 上會讀網際網路選項中固定的代理伺服器，但不會套用它的例外清單；
    不另外判斷時，連到本機或內部的伺服器也會送到公司的代理伺服器而失敗。
    """
    host = (urlsplit(url).hostname or "").lower()
    if host in LOOPBACK_HOSTS or host.startswith("127."):
        return True
    try:
        return bool(urllib.request.proxy_bypass(host))
    except (OSError, ValueError):
        return False


def http_client(url: str) -> httpx.Client:
    """連到伺服器的 HTTP 用戶端：作業系統的憑證存放區；例外清單中的伺服器不經過代理伺服器。"""
    import truststore

    return httpx.Client(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        timeout=httpx.Timeout(600, connect=15),
        trust_env=not bypass_proxy(url),
    )


def normalize_url(raw: str) -> str:
    """伺服器網址：去掉空白與結尾的斜線，沒有寫 http(s):// 時補 https://。"""
    text = raw.strip().rstrip("/")
    if not text:
        raise HTTPException(422, "請輸入伺服器的網址")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise HTTPException(422, f"伺服器的網址不正確：{raw}")
    return text


@dataclass
class ServerSettings:
    url: str | None = None
    username: str | None = None
    name: str | None = None


@dataclass
class PendingSignIn:
    url: str
    verifier: str
    redirect_uri: str
    started: float


class Remote:
    """伺服器的設定、權杖與進行中的登入。"""

    def __init__(
        self,
        settings_path: Path,
        credentials: CredentialStore,
        opener: Callable[[str], object] = webbrowser.open,
        client_factory: Callable[[str], httpx.Client] = http_client,
    ) -> None:
        self.settings_path = settings_path
        self.credentials = credentials
        self.opener = opener
        self.client_factory = client_factory
        self.pending: dict[str, PendingSignIn] = {}
        self.error: str | None = None
        self._clients: dict[str, httpx.Client] = {}
        self._lock = threading.Lock()

    def client(self, url: str) -> httpx.Client:
        """連到這台伺服器的用戶端（每台伺服器一個：是否經過代理伺服器依伺服器而定）。"""
        with self._lock:
            client = self._clients.get(url)
            if client is None:
                client = self._clients[url] = self.client_factory(url)
            return client

    def load(self) -> ServerSettings:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return ServerSettings(**data)
        except (OSError, ValueError, TypeError):
            return ServerSettings()

    def save(self, settings: ServerSettings) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def token(self, url: str) -> str | None:
        return self.credentials.get(target_for(url))

    def signed_in(self) -> tuple[ServerSettings, str]:
        settings = self.load()
        token = self.token(settings.url) if settings.url else None
        if settings.url is None or token is None:
            raise HTTPException(401, "還沒有登入伺服器")
        return settings, token

    def server(self) -> tuple[str, str, ServerSettings]:
        """登入中的伺服器網址、權杖與設定；沒有登入時回應 401。"""
        settings, token = self.signed_in()
        assert settings.url is not None
        return settings.url, token, settings

    def call(self, method: str, path: str, **options: Any) -> httpx.Response:
        url, token, settings = self.server()
        headers = {**options.pop("headers", {}), "Authorization": f"Bearer {token}"}
        try:
            response = self.client(url).request(method, f"{url}{path}", headers=headers, **options)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"連不上伺服器 {url}：{exc}") from exc
        if response.status_code == 401:
            self.forget(settings, "伺服器的登入已失效，請重新登入")
            raise HTTPException(401, "伺服器的登入已失效，請重新登入")
        return response

    def download(self, path: str, target: Path) -> httpx.Headers:
        """下載到檔案（報表套件可能很大，不整個放在記憶體中）。"""
        url, token, settings = self.server()
        try:
            with self.client(url).stream(
                "GET", f"{url}{path}", headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status_code == 401:
                    self.forget(settings, "伺服器的登入已失效，請重新登入")
                    raise HTTPException(401, "伺服器的登入已失效，請重新登入")
                if response.status_code != 200:
                    response.read()
                    relay(response)
                with target.open("wb") as sink:
                    for chunk in response.iter_bytes(1 << 20):
                        sink.write(chunk)
                return response.headers
        except httpx.HTTPError as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(502, f"連不上伺服器 {url}：{exc}") from exc

    def forget(self, settings: ServerSettings, error: str | None = None) -> None:
        if settings.url:
            self.credentials.delete(target_for(settings.url))
        self.save(ServerSettings(url=settings.url))
        self.error = error


def relay(response: httpx.Response) -> Any:
    """伺服器的回應：錯誤時把 detail（文字或 {error, message, …}）原樣轉給檢視器。"""
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        raise HTTPException(response.status_code, detail or response.text[:500] or "伺服器錯誤")
    return body


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,"Microsoft JhengHei",sans-serif;background:#f3f5f8;margin:0}}
main{{max-width:460px;margin:14vh auto;background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:28px 32px}}
h1{{font-size:20px;margin:0 0 12px}}p{{line-height:1.6}}</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></main></body></html>""",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


class SignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class OpenRemoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: str
    version: int | None = None


class BrowseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbook: str
    name: str = ""
    project: str | None = None
    comment: str = ""
    report: str | None = None
    """覆蓋伺服器上的哪一份報表；省略時依 Project 與名稱。"""
    overwrite: bool = False
    force: bool = False
    """伺服器上已有比基準更新的版本時仍要覆蓋。"""


class PublishDatasourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbook: str
    datasource: str
    """報表中資料源的 id。"""
    name: str = ""
    project: str | None = None
    comment: str = ""
    target: str | None = None
    """覆蓋伺服器上的哪一個資料源（id）；省略時依 Project 與名稱。"""
    overwrite: bool = False
    """同一個 Project 中已經有同名的資料源時覆蓋它。"""
    link: bool = True
    """報表改用發布的資料源（與 Tableau 的「更新活頁簿以使用發布的資料來源」相同）。"""


def link_datasource(ir: IR, datasource_id: str, content_url: str, version: int) -> IR:
    """報表的資料源改連到伺服器上的資料源：記下網址名稱（與併入 .tdsx 的報表相同，萃取取自伺服器）。
    資料在報表中仍有一份：桌面版照常查詢，伺服器上改用資料源的萃取。"""
    changed = []
    for datasource in ir.datasources:
        if datasource.id == datasource_id:
            connection = datasource.connection
            if isinstance(connection, ExtractConnection) and not connection.hyper_file.startswith(
                "Data/Published/"
            ):
                connection = connection.model_copy(
                    update={"hyper_file": f"Data/Published/{content_url}/{datasource.id}.hyper"}
                )
            datasource = datasource.model_copy(
                update={
                    "published": PublishedSource(content_url=content_url, revision=str(version)),
                    "connection": connection,
                }
            )
        changed.append(datasource)
    return IR.model_validate(
        {**ir.model_dump(mode="json"), "datasources": [d.model_dump(mode="json") for d in changed]}
    )


def remote_router(
    remote: Remote,
    documents: Any,
    opened: Callable[[Any, Workspace, bool], dict[str, Any]],
) -> APIRouter:
    """documents：tmig_service.desktop.Documents；opened：開啟後回傳給檢視器的內容。"""
    router = APIRouter()

    def workspace(request: Request) -> Workspace:
        return request.app.state.workspace

    @router.get("/api/desktop/server")
    def status() -> dict[str, Any]:
        settings = remote.load()
        now = time.time()
        for key in [k for k, p in remote.pending.items() if now - p.started > SIGN_IN_SECONDS]:
            del remote.pending[key]
        token = remote.token(settings.url) if settings.url else None
        return {
            "url": settings.url,
            "username": settings.username if token else None,
            "name": settings.name if token else None,
            "signed_in": token is not None and settings.username is not None,
            "pending": bool(remote.pending),
            "error": remote.error,
        }

    @router.post("/api/desktop/server/signin")
    def sign_in(body: SignInRequest, request: Request) -> dict[str, Any]:
        url = normalize_url(body.url)
        try:
            response = remote.client(url).get(f"{url}/api/me")
            me = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f"連不上伺服器 {url}：{exc}") from exc
        if not isinstance(me, dict) or "mode" not in me:
            raise HTTPException(502, f"{url} 不是報表平台的伺服器")
        if me["mode"] == "none":
            raise HTTPException(422, f"{url} 沒有設定身分驗證：不能登入，也不能發布報表")
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        redirect_uri = f"{str(request.base_url).rstrip('/')}{CALLBACK_PATH}"
        remote.pending = {state: PendingSignIn(url, verifier, redirect_uri, time.time())}
        remote.error = None
        authorize = f"{url}/desktop/authorize?" + urlencode(
            {
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge_of(verifier),
                "code_challenge_method": "S256",
                "label": socket.gethostname()[:100],
            }
        )
        remote.opener(authorize)
        return {"authorize_url": authorize}

    @router.get(CALLBACK_PATH, include_in_schema=False)
    def callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
        """伺服器核准後導回這裡（系統瀏覽器，沒有引擎的權杖；以 state 確認是這次的登入）。"""
        pending = remote.pending.pop(state, None) if state else None
        if pending is None or time.time() - pending.started > SIGN_IN_SECONDS:
            return _page(
                "登入失敗",
                "這次登入已經逾時，或不是從報表桌面版開始的；請回到桌面版重新登入。",
                400,
            )
        if error:
            remote.error = "在伺服器上按了「拒絕」，沒有登入"
            return _page("沒有登入", "已拒絕報表桌面版登入。可以關閉這個分頁。")
        try:
            response = remote.client(pending.url).post(
                f"{pending.url}/api/desktop-auth/token",
                json={
                    "code": code,
                    "code_verifier": pending.verifier,
                    "redirect_uri": pending.redirect_uri,
                },
            )
            data = relay(response)
        except (httpx.HTTPError, HTTPException) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            remote.error = f"登入失敗：{message}"
            return _page("登入失敗", f"{message}。請回到報表桌面版重新登入。", 502)
        remote.credentials.set(target_for(pending.url), data["username"], data["token"])
        remote.save(
            ServerSettings(url=pending.url, username=data["username"], name=data.get("name"))
        )
        remote.error = None
        return _page(
            "已登入",
            f"報表桌面版已以 {data['username']} 登入 {pending.url}。可以關閉這個分頁，回到桌面版。",
        )

    @router.post("/api/desktop/server/signout")
    def sign_out() -> dict[str, bool]:
        settings = remote.load()
        token = remote.token(settings.url) if settings.url else None
        if settings.url and token:
            # 連不上時伺服器上的權杖到期後自然失效
            with contextlib.suppress(httpx.HTTPError):
                remote.client(settings.url).delete(
                    f"{settings.url}/api/desktop-auth/tokens/current",
                    headers={"Authorization": f"Bearer {token}"},
                )
        remote.forget(settings)
        return {"ok": True}

    @router.post("/api/desktop/server/browse")
    def browse(body: BrowseRequest) -> dict[str, bool]:
        """在系統瀏覽器開啟伺服器上的網頁（例如發布後的報表）；只能開目前設定的伺服器。"""
        settings = remote.load()
        if not settings.url:
            raise HTTPException(409, "還沒有設定伺服器")
        if not body.path.startswith("/") or body.path.startswith("//") or "\\" in body.path:
            raise HTTPException(422, f"網址不正確：{body.path}")
        remote.opener(f"{settings.url}{body.path}")
        return {"ok": True}

    @router.get("/api/desktop/server/projects")
    def projects() -> Any:
        return relay(remote.call("GET", "/api/projects"))

    @router.get("/api/desktop/server/reports")
    def reports() -> Any:
        return relay(remote.call("GET", "/api/reports"))

    @router.post("/api/desktop/server/open")
    def open_remote(body: OpenRemoteRequest, request: Request) -> dict[str, Any]:
        settings, _ = remote.signed_in()
        for document in documents.list():
            server = document.server or {}
            if server.get("url") == settings.url and server.get("report") == body.report:
                return opened(document, workspace(request), True)
        version = str(body.version) if body.version is not None else "latest"
        path = f"/api/reports/{quote(body.report, safe='')}"
        detail = relay(remote.call("GET", path))
        document, directory = documents.create_named(
            detail["name"], f"{settings.url}{detail['url']}", "package"
        )
        download = directory / ".download.tmigx"
        try:
            headers = remote.download(f"{path}/versions/{version}/package", download)
            number = int(headers.get("x-tmig-version", "0") or 0)
            read_package(download, directory)
            loaded = workspace(request).get(document.id)
        except (PackageError, HTTPException) as exc:
            documents.remove(document.id)
            workspace(request).forget(document.id)
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            raise HTTPException(422, f"無法開啟伺服器上的報表：{message}") from exc
        finally:
            download.unlink(missing_ok=True)
        document.name = loaded.ir.workbook.name
        document.server = {
            "url": settings.url,
            "report": detail["id"],
            "version": number,
            "project": detail["project"],
            "project_name": detail["project_name"],
            "name": detail["name"],
        }
        documents.update(document)
        return opened(document, workspace(request), False)

    @router.post("/api/desktop/server/publish")
    def publish(body: PublishRequest, request: Request) -> dict[str, Any]:
        document = documents.get(body.workbook)
        settings, _ = remote.signed_in()
        loaded = workspace(request).get(document.id)
        identity = request.app.state.authenticator.identity(request)
        try:
            ensure_data(loaded.directory, loaded.ir)
        except (PackageError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc
        problems = check_report(
            loaded.ir, loaded.directory, request.app.state.runtime, identity, "desktop"
        )
        if errors(problems):
            raise HTTPException(
                422,
                {
                    "error": "checks",
                    "message": "桌面版的檢查沒有通過",
                    "problems": [p.to_dict() for p in problems],
                },
            )
        server = document.server or {}
        same = (
            server.get("url") == settings.url
            and body.report
            and server.get("report") == body.report
        )
        params: dict[str, Any] = {
            "name": body.name,
            "comment": body.comment,
            "overwrite": "true" if body.overwrite else "false",
            "force": "true" if body.force else "false",
        }
        if body.report:
            params["report"] = body.report
        elif body.project:
            params["project"] = body.project
        if same and server.get("version"):
            params["base_version"] = server["version"]
        package = documents.root / ".publish" / f"{secrets.token_hex(6)}.tmigx"
        package.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_package(package, loaded.ir, loaded.directory)
            with package.open("rb") as content:
                response = remote.call(
                    "POST",
                    "/api/reports/publish",
                    params=params,
                    content=content,
                    headers={"Content-Type": MEDIA_TYPE},
                )
        except PackageError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            package.unlink(missing_ok=True)
        result = relay(response)
        report = result["report"]
        document.server = {
            "url": settings.url,
            "report": report["id"],
            "version": result["version"]["version"],
            "project": report["project"],
            "project_name": report["project_name"],
            "name": report["name"],
        }
        documents.update(document)
        local = [p.to_dict() for p in problems]
        return {
            **result,
            "problems": local + result.get("problems", []),
            "address": f"{settings.url}{report['url']}",
        }

    # ------------------------------------------------------------ 資料源

    def server_datasources() -> list[dict[str, Any]]:
        found = relay(remote.call("GET", "/api/datasources"))
        return found if isinstance(found, list) else []

    @router.get("/api/desktop/server/datasources")
    def datasources(workbook: str, request: Request) -> dict[str, Any]:
        """報表的資料源；已經連到伺服器上的資料源（網址名稱相同、看得到）時一併回傳，可以覆蓋它。"""
        document = documents.get(workbook)
        loaded = workspace(request).get(document.id)
        remote.signed_in()
        published = {d["content_url"].casefold(): d for d in server_datasources()}
        items = []
        for datasource in loaded.ir.datasources:
            url = datasource.published.content_url if datasource.published else None
            items.append(
                {
                    "id": datasource.id,
                    "caption": datasource.caption,
                    "connection": datasource.connection.kind,
                    "content_url": url,
                    "server": published.get(url.casefold()) if url else None,
                }
            )
        return {"datasources": items}

    @router.post("/api/desktop/server/publish-datasource")
    def publish_datasource(body: PublishDatasourceRequest, request: Request) -> dict[str, Any]:
        document = documents.get(body.workbook)
        settings, _ = remote.signed_in()
        loaded = workspace(request).get(document.id)
        found = next((d for d in loaded.ir.datasources if d.id == body.datasource), None)
        if found is None:
            raise HTTPException(404, f"報表中沒有資料源「{body.datasource}」")
        try:
            ensure_data(loaded.directory, loaded.ir)
            single = datasource_ir(loaded.ir, found.id)
        except (PackageError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc
        params: dict[str, Any] = {
            "name": body.name.strip() or found.caption,
            "comment": body.comment,
            "overwrite": "true" if body.overwrite else "false",
        }
        if body.target:
            params["datasource"] = body.target
        elif body.project:
            params["project"] = body.project
        package = documents.root / ".publish" / f"{secrets.token_hex(6)}.tmigds"
        package.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_package(package, single, loaded.directory, kind=DATASOURCE_KIND)
            with package.open("rb") as content:
                response = remote.call(
                    "POST",
                    "/api/datasources/publish",
                    params=params,
                    content=content,
                    headers={"Content-Type": DATASOURCE_MEDIA_TYPE},
                )
        except PackageError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            package.unlink(missing_ok=True)
        record = relay(response)
        linked = False
        if body.link:
            updated = link_datasource(loaded.ir, found.id, record["content_url"], record["version"])
            workspace(request).save_ir(document.id, updated)
            documents.mark_modified(document.id)
            linked = True
        return {
            "datasource": record,
            "linked": linked,
            "address": f"{settings.url}/datasources",
        }

    return router


__all__ = [
    "CALLBACK_PATH",
    "Remote",
    "ServerSettings",
    "bypass_proxy",
    "link_datasource",
    "normalize_url",
    "remote_router",
]
