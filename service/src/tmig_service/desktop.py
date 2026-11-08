"""桌面版的本機引擎（D0）：桌面版外殼在作者的電腦上啟動的查詢服務。

外殼執行 ``tmig desktop serve``，從標準輸出讀第一行 JSON（``{"event": "ready", "port": …,
"token": …}``）後，以 ``http://127.0.0.1:<埠號>/?token=<權杖>`` 開啟檢視器。

- 只接受本機：只綁 127.0.0.1；Host 標頭必須是 127.0.0.1 或 localhost（防 DNS rebinding）
- 權杖：每次啟動產生一組。檢視器第一次帶 ?token= 進來時換成 cookie（HttpOnly、SameSite=Lax：外殼從自己的
  啟動畫面切換到引擎是跨站的導覽，Strict 的 cookie 在轉址後的請求中不會送出），
  之後每個請求都要帶這個 cookie 或標頭 X-Tmig-Token；其他網頁與程式拿不到權杖就不能呼叫
- 身分：作業系統的登入帳號（USERNAME()），沒有群組；ISMEMBEROF() 為否，與 Tableau Desktop
  沒有登入伺服器時相同
- 工作區：每個開啟的檔案複製（或解開）到工作區的一個子目錄，結構與 fixtures 相同，
  檢視與查詢沿用查詢服務的 API；原始檔不會被鎖住或改動
- 開啟：Tableau 的 .twbx、.twb（以解析器轉成 IR），或平台的報表套件（.tmigx）
- 儲存：報表套件（.tmigx，見 tmig.package）
- 外殼以 --exit-with-parent 啟動時，標準輸入關閉（外殼結束或異常終止）引擎就結束

API（都在 /api/desktop 下）：

- ``GET  /documents``：開啟中的檔案
- ``POST /open``：``{"path": "C:/…/銷售.twbx"}`` → 開啟，回傳文件與轉不過來的元素
- ``POST /save``：``{"workbook": "<id>", "path": "C:/…/銷售.tmigx"}`` → 存成報表套件
- ``POST /close``：``{"workbook": "<id>"}`` → 關閉並刪除工作區中的複本
"""

from __future__ import annotations

import getpass
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from tmig.package import SUFFIX, PackageError, read_package, write_package
from tmig.parser.ids import slugify
from tmig.planner.identity import Identity
from tmig.verify.run import ensure_data
from tmig_service.app import Workspace, create_app
from tmig_service.auth import Authenticator, AuthSettings

TOKEN_COOKIE = "tmig_desktop"
TOKEN_HEADER = "x-tmig-token"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
DOCUMENT_FILE = ".document.json"
Kind = Literal["twbx", "twb", "package"]
KINDS: dict[str, Kind] = {".twbx": "twbx", ".twb": "twb", SUFFIX: "package"}
UNSUPPORTED_SHOWN = 20
"""開啟時列出的不支援元素數量上限（其餘只計數）。"""


def default_workspace() -> Path:
    """工作區的預設位置：Windows 為 %LOCALAPPDATA%\\tmig\\workspace，其他為 ~/.local/share/tmig/workspace。"""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "tmig" / "workspace"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "tmig" / "workspace"


def local_username() -> str:
    try:
        return getpass.getuser() or "local"
    except (OSError, KeyError):
        return "local"


# ---------------------------------------------------------------- 文件


@dataclass
class Document:
    id: str
    name: str
    """報表名稱（IR 中的 workbook 名稱）。"""
    source: str
    """開啟的檔案。"""
    kind: Kind
    saved: str | None = None
    """最近一次存成的報表套件；「儲存」直接寫到這裡。"""


class Documents:
    """工作區中開啟的檔案：每份一個子目錄，裡面有 .document.json 記下來源。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    def list(self) -> list[Document]:
        if not self.root.is_dir():
            return []
        found = []
        for directory in sorted(self.root.iterdir()):
            document = self._read(directory)
            if document is not None:
                found.append(document)
        return found

    def get(self, document_id: str) -> Document:
        directory = self.root / document_id
        document = self._read(directory) if _plain_name(document_id) else None
        if document is None:
            raise HTTPException(404, f"沒有開啟中的報表「{document_id}」")
        return document

    def create(self, source: Path, kind: Kind) -> tuple[Document, Path]:
        """在工作區建立新文件的目錄（名稱取自檔名，重複時加序號）。"""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            base = slugify(source.stem)[:48].strip("_") or "report"
            document_id, n = base, 1
            while (self.root / document_id).exists():
                n += 1
                document_id = f"{base}_{n}"
            directory = self.root / document_id
            directory.mkdir()
            document = Document(document_id, source.stem, str(source), kind)
            self._write(directory, document)
        return document, directory

    def update(self, document: Document) -> None:
        self._write(self.root / document.id, document)

    def remove(self, document_id: str) -> None:
        self.get(document_id)
        shutil.rmtree(self.root / document_id, ignore_errors=True)

    def find_source(self, source: Path) -> Document | None:
        for document in self.list():
            if Path(document.source) == source:
                return document
        return None

    @staticmethod
    def _read(directory: Path) -> Document | None:
        path = directory / DOCUMENT_FILE
        if not path.is_file():
            return None
        try:
            return Document(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _write(directory: Path, document: Document) -> None:
        (directory / DOCUMENT_FILE).write_text(
            json.dumps(asdict(document), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _plain_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def _absolute_file(raw: str, what: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, f"{what}必須是完整路徑：{raw}")
    return path


# ---------------------------------------------------------------- API


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbook: str
    path: str


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbook: str


def desktop_router(documents: Documents) -> APIRouter:
    router = APIRouter(prefix="/api/desktop")

    def workspace(request: Request) -> Workspace:
        return request.app.state.workspace

    @router.get("/documents")
    def list_documents() -> list[dict[str, Any]]:
        return [asdict(d) for d in documents.list()]

    @router.post("/open")
    def open_document(body: OpenRequest, request: Request) -> dict[str, Any]:
        """開啟檔案：.twbx、.twb 複製到工作區後解析；報表套件解到工作區。同一個檔案已開啟時直接回傳。"""
        source = _absolute_file(body.path, "要開啟的檔案")
        kind = KINDS.get(source.suffix.lower())
        if kind is None:
            raise HTTPException(422, f"只能開啟 .twbx、.twb 或 {SUFFIX}：{source.name}")
        if not source.is_file():
            raise HTTPException(404, f"找不到檔案：{source}")
        existing = documents.find_source(source)
        if existing is not None:
            return _opened(existing, workspace(request), already_open=True)
        document, directory = documents.create(source, kind)
        try:
            if kind == "package":
                read_package(source, directory)
            else:
                shutil.copy2(source, directory / source.name)
            loaded = workspace(request).get(document.id)
        except PackageError as exc:
            documents.remove(document.id)
            raise HTTPException(422, str(exc)) from exc
        except HTTPException:
            documents.remove(document.id)
            raise
        except Exception as exc:
            documents.remove(document.id)
            workspace(request).forget(document.id)
            raise HTTPException(422, f"無法開啟 {source.name}：{exc}") from exc
        document.name = loaded.ir.workbook.name
        documents.update(document)
        return _opened(document, workspace(request), already_open=False)

    @router.post("/save")
    def save_document(body: SaveRequest, request: Request) -> dict[str, Any]:
        """存成報表套件；萃取的資料還沒匯入時先由 .twbx 匯入。"""
        document = documents.get(body.workbook)
        target = _absolute_file(body.path, "儲存的位置")
        if target.suffix.lower() != SUFFIX:
            raise HTTPException(422, f"報表套件的副檔名必須是 {SUFFIX}：{target.name}")
        if not target.parent.is_dir():
            raise HTTPException(422, f"資料夾不存在：{target.parent}")
        loaded = workspace(request).get(document.id)
        try:
            ensure_data(loaded.directory, loaded.ir)
            info = write_package(target, loaded.ir, loaded.directory)
        except (PackageError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"無法寫入 {target}：{exc}") from exc
        document.saved = str(target)
        documents.update(document)
        return {
            "document": asdict(document),
            "path": str(info.path),
            "size": info.size,
            "datasources": info.datasources,
            "files": info.files,
            "missing_files": info.missing_files,
        }

    @router.post("/close")
    def close_document(body: CloseRequest, request: Request) -> dict[str, bool]:
        documents.remove(body.workbook)
        workspace(request).forget(body.workbook)
        return {"ok": True}

    return router


def _opened(document: Document, workspace: Workspace, already_open: bool) -> dict[str, Any]:
    ir = workspace.get(document.id).ir
    return {
        "document": asdict(document),
        "already_open": already_open,
        "unsupported_count": len(ir.unsupported),
        "unsupported": [item.reason for item in ir.unsupported[:UNSUPPORTED_SHOWN]],
    }


# ---------------------------------------------------------------- 身分與權杖


class DesktopAuthenticator(Authenticator):
    """登入者固定為作業系統的帳號。"""

    def __init__(self, username: str) -> None:
        super().__init__(AuthSettings(mode="desktop"))
        self._identity = Identity(username=username)

    def identity(self, request: Request) -> Identity | None:
        return self._identity


class DesktopGuard:
    """只接受本機、帶權杖的請求；第一次以 ?token= 進來時換成 cookie 並去掉網址中的權杖。"""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    def _valid(self, value: str | None) -> bool:
        return value is not None and hmac.compare_digest(value.encode(), self.token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        host = (request.headers.get("host") or "").lower()
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        response: Response
        if hostname not in LOCAL_HOSTS:
            response = PlainTextResponse("只接受本機的連線", 403)
        elif "token" in request.query_params:
            if self._valid(request.query_params.get("token")):
                rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
                url = request.url.path + (f"?{urlencode(rest)}" if rest else "")
                response = RedirectResponse(url, 303)
                response.set_cookie(
                    TOKEN_COOKIE, self.token.decode(), httponly=True, samesite="lax", path="/"
                )
            else:
                response = PlainTextResponse("權杖不正確，請從桌面版開啟", 401)
        elif self._valid(request.cookies.get(TOKEN_COOKIE)) or self._valid(
            request.headers.get(TOKEN_HEADER)
        ):
            await self.app(scope, receive, send)
            return
        else:
            response = PlainTextResponse("請從桌面版開啟", 401)
        await response(scope, receive, send)


def create_desktop_app(
    workspace: Path,
    token: str,
    web_dist: Path | None = None,
    username: str | None = None,
) -> FastAPI:
    documents = Documents(workspace)
    app = create_app(
        workspace,
        web_dist,
        auth=DesktopAuthenticator(username or local_username()),
        routers=[desktop_router(documents)],
    )
    app.state.documents = documents
    app.add_middleware(DesktopGuard, token=token)
    return app


# ---------------------------------------------------------------- 啟動


def serve_desktop(
    workspace: Path,
    *,
    port: int = 0,
    web_dist: Path | None = None,
    clean: bool = False,
    exit_with_parent: bool = False,
) -> None:
    """啟動本機引擎；就緒時在標準輸出印一行 JSON（埠號與權杖），之後不再寫標準輸出。"""
    import uvicorn

    if clean and workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    actual = listener.getsockname()[1]
    app = create_desktop_app(workspace, token, web_dist)
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False)
    )
    if exit_with_parent:

        def watch() -> None:
            # 外殼關閉標準輸入（結束或異常終止）時結束引擎
            for _ in sys.stdin:
                pass
            server.should_exit = True

        threading.Thread(target=watch, name="parent-watch", daemon=True).start()
    sys.stdout.write(json.dumps({"event": "ready", "port": actual, "token": token}) + "\n")
    sys.stdout.flush()
    server.run(sockets=[listener])


__all__ = [
    "Document",
    "Documents",
    "create_desktop_app",
    "default_workspace",
    "desktop_router",
    "serve_desktop",
]
