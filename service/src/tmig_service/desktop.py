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
- 新增：以一個資料檔（Excel、文字檔、空間檔案）建立新的報表（一個資料源與空白的工作表；資料源的編輯見
  tmig_service.datasource_editor）
- 儲存：報表套件（.tmigx，見 tmig.package）
- 外殼以 --exit-with-parent 啟動時，標準輸入關閉（外殼結束或異常終止）引擎就結束

API（都在 /api/desktop 下）：

- ``GET  /documents``：開啟中的檔案
- ``POST /open``：``{"path": "C:/…/銷售.twbx"}`` → 開啟，回傳文件與轉不過來的元素
- ``POST /inspect``：``{"path": "C:/…/銷售.xlsx"}`` → 資料檔中的資料表、欄名、型別與列數
- ``POST /new``：``{"path": "C:/…/銷售.xlsx", "table": "Orders"}`` → 以資料檔建立新的報表
- ``POST /save``：``{"workbook": "<id>", "path": "C:/…/銷售.tmigx"}`` → 存成報表套件
- ``POST /close``：``{"workbook": "<id>"}`` → 關閉並刪除工作區中的複本
- 伺服器（登入、從伺服器開啟、發布）：見 tmig_service.desktop_remote
"""

from __future__ import annotations

import base64
import getpass
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from tmig.data import initial_sql
from tmig.package import SUFFIX, PackageError, read_package, write_package
from tmig.parser.ids import slugify
from tmig.planner.identity import Identity
from tmig.verify.run import ensure_data
from tmig_service.app import Workspace, create_app
from tmig_service.auth import Authenticator, AuthSettings
from tmig_service.credentials import CredentialStore, default_credentials
from tmig_service.datasource_editor import datasource_editor_router
from tmig_service.desktop_connections import SETTINGS_FILE as CONNECTIONS_FILE
from tmig_service.desktop_connections import DesktopConnections, connections_router
from tmig_service.desktop_remote import (
    CALLBACK_PATH,
    SETTINGS_FILE,
    Remote,
    http_client,
    remote_router,
)
from tmig_service.editor import editor_router

TOKEN_COOKIE = "tmig_desktop"
TOKEN_HEADER = "x-tmig-token"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
DOCUMENT_FILE = ".document.json"
Kind = Literal["twbx", "twb", "package", "new"]
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
    server: dict[str, Any] | None = None
    """從伺服器開啟或發布到伺服器的報表：url、report、version（基準版本）、project、name。"""
    modified: bool = False
    """編輯器改過、還沒存成報表套件。"""


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
        return self.create_named(source.stem, str(source), kind)

    def create_named(self, stem: str, source: str, kind: Kind) -> tuple[Document, Path]:
        """stem：報表名稱（目錄名稱取自它）；source：開啟的檔案或伺服器上的網址。"""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            base = slugify(stem)[:48].strip("_") or "report"
            document_id, n = base, 1
            while (self.root / document_id).exists():
                n += 1
                document_id = f"{base}_{n}"
            directory = self.root / document_id
            directory.mkdir()
            document = Document(document_id, stem, source, kind)
            self._write(directory, document)
        return document, directory

    def update(self, document: Document) -> None:
        self._write(self.root / document.id, document)

    def mark_modified(self, document_id: str) -> None:
        """編輯器改過報表（關閉前提醒儲存）。"""
        with self._lock:
            document = self._read(self.root / document_id) if _plain_name(document_id) else None
            if document is not None and not document.modified:
                document.modified = True
                self._write(self.root / document_id, document)

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


class NewRequest(BaseModel):
    """以資料檔建立新的報表：path 為作者電腦上的檔案，table 為其中的資料表（Excel 的工作表等）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    table: str | None = None
    name: str | None = None


class NewLiveRequest(BaseModel):
    """連到資料庫的新報表：connection 為連線代號（連線對話框建立的），table_path 或 sql 二擇一。"""

    model_config = ConfigDict(extra="forbid")

    connection: str
    table_path: list[str] | None = None
    sql: str | None = None
    name: str | None = None


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbook: str
    path: str


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbook: str


class ExportRequest(BaseModel):
    """匯出的圖片、PowerPoint、PDF、CSV 或 Excel：檢視器備好內容（base64），引擎寫到作者選的位置。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


# 檢視器會存的檔案：畫面的圖片與簡報（exporter.ts），以及資料與交叉資料表的 CSV、Excel（ViewerTools.tsx）
EXPORT_SUFFIXES = (".png", ".pptx", ".pdf", ".csv", ".xlsx")
EXPORT_LIMIT = 200 * 1024 * 1024


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
                # 先複製成暫存的檔名再換上：複製到一半時，其他請求（報表清單）不會讀到不完整的檔案
                partial = directory / f".{source.name}.part"
                shutil.copy2(source, partial)
                os.replace(partial, directory / source.name)
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

    @router.post("/inspect")
    def inspect_document_file(body: OpenRequest) -> dict[str, Any]:
        """新增報表前檢查資料檔：其中的資料表（Excel 的工作表等）、欄名、型別與列數。"""
        from tmig_service.datasource_editor import inspect, local_file

        return inspect(local_file(body.path)).to_json()

    @router.post("/new")
    def new_document(body: NewRequest, request: Request) -> dict[str, Any]:
        """新的報表：資料檔複製到報表目錄、建立資料源與空白的工作表、匯入資料。"""
        from tmig.authoring import new_workbook
        from tmig.data.hyper_import import import_sources
        from tmig.edit import EditError
        from tmig.ir import save_ir
        from tmig.package import SOURCES
        from tmig_service.datasource_editor import copy_source, inspect, local_file

        source = inspect(local_file(body.path))
        stem = (body.name or "").strip() or source.path.stem
        document, directory = documents.create_named(stem, str(source.path), "new")
        try:
            package_path = copy_source(directory, source)
            ir = new_workbook(stem, source, body.table, package_path)
            save_ir(ir, directory / "golden.ir.json")
            results = import_sources(directory / SOURCES, directory / "data", ir)
            bad = [r for r in results if not r.ok]
            if bad:
                raise HTTPException(422, f"匯入資料失敗：{'、'.join(bad[0].missing_columns)}")
            workspace(request).get(document.id)
        except EditError as exc:
            documents.remove(document.id)
            raise HTTPException(422, str(exc)) from exc
        except HTTPException:
            documents.remove(document.id)
            workspace(request).forget(document.id)
            raise
        except Exception as exc:
            documents.remove(document.id)
            workspace(request).forget(document.id)
            raise HTTPException(422, f"無法以 {source.path.name} 建立報表：{exc}") from exc
        document.name = ir.workbook.name
        document.modified = True
        documents.update(document)
        opened = _opened(document, workspace(request), already_open=False)
        opened["datasource"] = ir.datasources[0].id
        opened["worksheet"] = ir.worksheets[0].id
        return opened

    @router.post("/new-live")
    def new_live_document(body: NewLiveRequest, request: Request) -> dict[str, Any]:
        """連到資料庫的新報表（Tableau 的「連線到伺服器」）：一張資料表或自訂 SQL；資料留在資料庫中，
        查詢時下推（Live 連線）。"""
        from tmig.authoring import new_live_workbook
        from tmig.edit import EditError
        from tmig.ir import save_ir
        from tmig_service.datasource_editor import desktop_connection, remote_columns

        connection = desktop_connection(request, body.connection)
        columns = remote_columns(request, connection, body.table_path, body.sql)
        default = body.table_path[-1] if body.table_path else "自訂 SQL"
        stem = (body.name or "").strip() or default
        document, directory = documents.create_named(stem, connection.server or "", "new")
        try:
            ir = new_live_workbook(stem, connection, body.table_path, body.sql, columns)
            save_ir(ir, directory / "golden.ir.json")
            workspace(request).get(document.id)
        except EditError as exc:
            documents.remove(document.id)
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            documents.remove(document.id)
            workspace(request).forget(document.id)
            raise HTTPException(422, f"無法建立報表：{exc}") from exc
        document.name = ir.workbook.name
        document.modified = True
        documents.update(document)
        opened = _opened(document, workspace(request), already_open=False)
        opened["datasource"] = ir.datasources[0].id
        opened["worksheet"] = ir.worksheets[0].id
        return opened

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
        document.modified = False
        documents.update(document)
        return {
            "document": asdict(document),
            "path": str(info.path),
            "size": info.size,
            "datasources": info.datasources,
            "files": info.files,
            "missing_files": info.missing_files,
        }

    @router.post("/export")
    def export_file(body: ExportRequest) -> dict[str, Any]:
        target = _absolute_file(body.path, "匯出的位置")
        if target.suffix.lower() not in EXPORT_SUFFIXES:
            raise HTTPException(
                422, f"匯出的副檔名必須是 {'、'.join(EXPORT_SUFFIXES)}：{target.name}"
            )
        if not target.parent.is_dir():
            raise HTTPException(422, f"資料夾不存在：{target.parent}")
        try:
            content = base64.b64decode(body.content, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "匯出的內容不正確") from exc
        if len(content) > EXPORT_LIMIT:
            raise HTTPException(413, "匯出的檔案太大")
        try:
            target.write_bytes(content)
        except OSError as exc:
            raise HTTPException(500, f"無法寫入 {target}：{exc}") from exc
        return {"path": str(target), "size": len(content)}

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


@dataclass(frozen=True)
class DesktopSession(initial_sql.Session):
    """桌面版執行連線的 Initial SQL 的環境：[TableauServerUser] 是登入伺服器的帳號（沒有登入時是空的，
    與 Tableau Desktop 相同），用到時才查；不執行 <ServerOnly> 的部分。"""

    server_user: Callable[[], str] | None = None
    workbook_name: Callable[[], str] | None = None

    def value(self, name: str) -> str:
        key = name.lower()
        if key in initial_sql.USER_PARAMETERS:
            return self.server_user() if self.server_user is not None else ""
        if key == "workbookname" and self.workbook_name is not None:
            return self.workbook_name()
        return super().value(name)


class DesktopAuthenticator(Authenticator):
    """登入者固定為作業系統的帳號。"""

    def __init__(self, username: str, server_user: Callable[[], str] | None = None) -> None:
        super().__init__(AuthSettings(mode="desktop"))
        self._identity = Identity(username=username)
        self.server_user = server_user
        """登入伺服器的帳號（Initial SQL 的 [TableauServerUser]）；沒有登入時是空字串。"""

    def identity(self, request: Request) -> Identity | None:
        return self._identity

    def sql_session(
        self,
        identity: Identity | None,
        workbook: str = "",
        workbook_name: Callable[[], str] | None = None,
    ) -> initial_sql.Session:
        return DesktopSession(
            app=initial_sql.APP_DESKTOP,
            workbook=workbook,
            server=False,
            server_user=self.server_user,
            workbook_name=workbook_name,
        )


async def desktop_sql_session(request: Request) -> None:
    """桌面版的編輯 API 的每個請求：之後開啟的來源連線，Initial SQL 以桌面版的環境代入。

    是 async 的相依：設定留在這次請求的 context 中，之後在執行緒中執行的端點看得到。"""
    authenticator = request.app.state.authenticator
    workbook = request.path_params.get("workbook") or ""

    def name() -> str:
        try:
            return request.app.state.workspace.get(workbook).ir.workbook.name
        except HTTPException:
            return workbook

    session = authenticator.sql_session(None, workbook, name if workbook else None)
    initial_sql.set_session(session)


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
        elif scope["type"] == "http" and request.url.path == CALLBACK_PATH:
            # 伺服器核准登入後由系統瀏覽器導回：沒有引擎的權杖，以 state 確認是這次的登入
            await self.app(scope, receive, send)
            return
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
    *,
    settings_dir: Path | None = None,
    credentials: CredentialStore | None = None,
    opener: Callable[[str], object] = webbrowser.open,
    client_factory: Callable[[str], Any] = http_client,
) -> FastAPI:
    """settings_dir：伺服器設定（網址與帳號）的位置，預設為工作區的上一層；
    credentials：權杖存放處（預設為認證管理員）；opener：開啟系統瀏覽器（登入伺服器用）。"""
    documents = Documents(workspace)
    folder = settings_dir or workspace.parent
    store = credentials or default_credentials(folder)
    remote = Remote(folder / SETTINGS_FILE, store, opener, client_factory)
    connections = DesktopConnections(folder / CONNECTIONS_FILE, store)
    connections.restore()

    def server_user() -> str:
        """登入伺服器的帳號；沒有登入（沒有權杖）時是空字串。"""
        settings = remote.load()
        if not settings.url or not settings.username or remote.token(settings.url) is None:
            return ""
        return settings.username

    edit = APIRouter(dependencies=[Depends(desktop_sql_session)])
    for router in (
        desktop_router(documents),
        remote_router(remote, documents, _opened),
        editor_router(on_change=documents.mark_modified),
        datasource_editor_router(on_change=documents.mark_modified),
        connections_router(lambda request: request.app.state.connections),
    ):
        edit.include_router(router)
    app = create_app(
        workspace,
        web_dist,
        auth=DesktopAuthenticator(username or local_username(), server_user),
        routers=[edit],
    )
    app.state.documents = documents
    app.state.connections = connections
    app.state.runtime.add_configs(connections.configs())
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
