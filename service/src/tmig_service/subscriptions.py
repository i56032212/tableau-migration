"""訂閱（第三階段 Q3）：排程到了，以訂閱者的身分開啟檢視器，產生 view 或整份報表的 PNG、PDF，以 SMTP 寄出。

- 以訂閱者的身分查詢：列層級權限照套，權限（P3）也照套，沒有權限時這次訂閱失敗
- 只在本機啟動一個查詢服務（internal 模式：只接受以隨機金鑰簽章的工作階段，沒有登入入口），
  以 Chromium 開啟檢視器截圖與列印；與使用者看到的畫面相同
- view 的訂閱：該 dashboard 或工作表；整份報表的訂閱：每個 dashboard（沒有 dashboard 時為每張工作表）
- 郵件：主旨、訊息與每個 view 的連結，圖片內嵌在內文（attach_image），PDF 合併成一個附件（attach_pdf）；
  view 沒有資料且訂閱設定為不寄送時不寄（send_if_view_empty）
- 收件人為使用者目錄中訂閱者的 email

郵件設定檔（TOML；密碼只能寫成 ${環境變數}）：

    [smtp]
    host = "smtp.example.com"
    port = 587
    starttls = true
    username = "bi-bot"
    password = "${TMIG_SMTP_PASSWORD}"
    sender = "報表平台 <bi@example.com>"

    [site]
    url = "https://bi.example.com"   # 郵件中的連結
"""

from __future__ import annotations

import io
import os
import secrets
import smtplib
import ssl
import threading
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from tmig.catalog import Report
from tmig.data.sources import ENV_REF
from tmig.schedule.runner import Job
from tmig_service.auth import SESSION_COOKIE, Authenticator, AuthError, AuthSettings
from tmig_service.site import SiteStore
from tmig_service.store import ReportStore

PAPER = {
    "A3": "A3",
    "A4": "A4",
    "A5": "A5",
    "Legal": "Legal",
    "Letter": "Letter",
    "Ledger": "Ledger",
    "Tabloid": "Tabloid",
}
"""Tableau 的紙張大小 → Chromium 支援的名稱；其他（含 Unspecified）用 A4。"""
VIEWPORT = {"width": 1400, "height": 1000}
T = TypeVar("T")


class SubscriptionError(RuntimeError):
    pass


@dataclass
class MailSettings:
    host: str
    sender: str
    port: int = 587
    starttls: bool = True
    use_ssl: bool = False
    username: str | None = None
    password: str | None = None
    site_url: str | None = None
    timeout: float = 30.0


def load_mail(path: str | Path) -> MailSettings:
    file = Path(path)
    if not file.is_file():
        raise SubscriptionError(f"找不到郵件設定檔 {file}")
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SubscriptionError(f"郵件設定檔 {file} 格式錯誤：{exc}") from exc
    smtp = data.get("smtp")
    if not isinstance(smtp, dict) or not smtp.get("host") or not smtp.get("sender"):
        raise SubscriptionError(f"郵件設定檔 {file} 的 [smtp] 需要 host 與 sender")
    password = smtp.get("password")
    if password is not None:
        match = ENV_REF.fullmatch(password) if isinstance(password, str) else None
        if match is None:
            raise SubscriptionError("[smtp] 的 password 不能直接寫在設定檔中，請改用 ${環境變數}")
        password = os.environ.get(match.group(1))
        if not password:
            raise SubscriptionError(f"[smtp] 的 password 需要的環境變數 {match.group(1)} 未設定")
    site = data.get("site") or {}
    return MailSettings(
        host=str(smtp["host"]),
        sender=str(smtp["sender"]),
        port=int(smtp.get("port", 587)),
        starttls=bool(smtp.get("starttls", True)),
        use_ssl=bool(smtp.get("ssl", False)),
        username=smtp.get("username"),
        password=password,
        site_url=str(site["url"]).rstrip("/") if site.get("url") else None,
    )


# ---------------------------------------------------------------- 產生檔案


@dataclass
class Rendered:
    """一個 view 的檔案與資料（每張工作表的列數與表格，供判斷是否沒有資料）。"""

    name: str
    path: str
    png: bytes | None = None
    pdf: bytes | None = None
    rows: dict[str, int] = field(default_factory=dict)
    tables: dict[str, list[list[str]]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not any(self.rows.values())


def subscription_views(report: Report, view: str | None) -> list[tuple[str, str]]:
    """訂閱的 view：（名稱, 檢視器中的路徑）。view 為 None 表示整份報表。"""
    base = f"/w/{quote(report.name)}"
    boards = [(d.name, f"{base}/d/{quote(d.id)}") for d in report.ir.dashboards]
    sheets = [(w.name, f"{base}/s/{quote(w.id)}") for w in report.ir.worksheets]
    if view is None:
        return boards or sheets
    found = [item for item in boards + sheets if item[0] == view]
    if not found:
        raise SubscriptionError(f"報表「{report.name}」沒有 view「{view}」")
    return found[:1]


class _Browser:
    """一個 Chromium：Playwright 的同步 API 只能在啟動它的執行緒中使用，每個瀏覽器有自己的執行緒。"""

    def __init__(self) -> None:
        self.thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tmig-render")
        self._stack: ExitStack | None = None
        self._browser: Any = None

    def call(self, work: Callable[[Any], T]) -> T:
        return self.thread.submit(self._call, work).result()

    def _call(self, work: Callable[[Any], T]) -> T:
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            stack = ExitStack()
            try:
                playwright = stack.enter_context(sync_playwright())
                self._browser = playwright.chromium.launch()
                stack.callback(self._browser.close)
            except Exception:
                stack.close()
                self._browser = None
                raise
            self._stack = stack
        return work(self._browser)

    def close(self) -> None:
        def stop() -> None:
            if self._stack is not None:
                self._stack.close()
            self._stack, self._browser = None, None

        try:
            self.thread.submit(stop).result()
        finally:
            self.thread.shutdown(wait=True)


class Renderer:
    """只在本機的查詢服務與 Chromium：第一次使用時啟動，close() 關閉。

    可以同時從多個執行緒呼叫 render（排程器同時執行多個訂閱）：每個呼叫用一個空著的 Chromium，
    沒有空著的時候再啟動一個（數量不超過同時執行的工作數）。

    store：報表庫（發布的報表也能訂閱；使用者目錄與權限以站台資料為準，修改後立即生效）。"""

    def __init__(
        self,
        fixtures: Path,
        auth: AuthSettings,
        web_dist: Path | None = None,
        store: ReportStore | None = None,
    ) -> None:
        site = SiteStore(store.root) if store is not None else None
        if auth.directory is None and not (site is not None and site.exists()):
            raise AuthError("訂閱需要使用者目錄（以訂閱者的身分查詢）")
        self.fixtures = fixtures
        self.web_dist = web_dist
        self.store = store
        settings = AuthSettings(
            mode="internal",
            directory=auth.directory,
            require_directory=True,
            strip_domain=auth.strip_domain,
            permissions=auth.permissions,
            session_secret=secrets.token_bytes(32),
        )
        self.authenticator = Authenticator(settings)
        if site is not None and site.exists():
            self.authenticator.site = site
        self._lock = threading.Lock()
        self._server: ExitStack | None = None
        self._browsers: list[_Browser] = []
        self._idle: list[_Browser] = []
        self.base_url = ""

    def _start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            from tmig_service.background import running_server

            stack = ExitStack()
            try:
                dist = self.web_dist or Path(__file__).resolve().parents[3] / "web" / "dist"
                if not (dist / "index.html").is_file():
                    raise SubscriptionError(
                        f"找不到檢視器 {dist}；先執行 cd web && pnpm install && pnpm build"
                    )
                self.base_url = stack.enter_context(
                    running_server(self.fixtures, dist, auth=self.authenticator, store=self.store)
                )
            except Exception:
                stack.close()
                raise
            self._server = stack

    @property
    def browsers(self) -> int:
        """啟動了幾個 Chromium。"""
        return len(self._browsers)

    def close(self) -> None:
        with self._lock:
            browsers, self._browsers, self._idle = self._browsers, [], []
            server, self._server = self._server, None
        for browser in browsers:
            browser.close()
        if server is not None:
            server.close()

    def render(
        self,
        user: str,
        views: list[tuple[str, str]],
        *,
        png: bool = True,
        pdf: bool = False,
        paper: str | None = None,
        landscape: bool = False,
    ) -> list[Rendered]:
        self._start()
        with self._lock:
            browser = self._idle.pop() if self._idle else None
            if browser is None:
                browser = _Browser()
                self._browsers.append(browser)
        healthy = False
        try:
            result = browser.call(
                lambda chromium: self._render(chromium, user, views, png, pdf, paper, landscape)
            )
            healthy = True
            return result
        except SubscriptionError:
            healthy = True  # view 的問題（不是瀏覽器的問題）：瀏覽器繼續使用
            raise
        finally:
            with self._lock:
                if browser in self._browsers:
                    if healthy:
                        self._idle.append(browser)
                    else:
                        # 出了問題的瀏覽器（例如 Chromium 結束了）：在背景關閉，下一次啟動新的
                        self._browsers.remove(browser)
                        threading.Thread(target=browser.close, daemon=True).start()

    def _render(
        self,
        chromium: Any,
        user: str,
        views: list[tuple[str, str]],
        png: bool,
        pdf: bool,
        paper: str | None,
        landscape: bool,
    ) -> list[Rendered]:
        context = chromium.new_context(viewport=VIEWPORT, device_scale_factor=1)
        try:
            context.add_cookies(
                [
                    {
                        "name": SESSION_COOKIE,
                        "value": self.authenticator.session_token(user, 900),
                        "url": self.base_url,
                    }
                ]
            )
            page = context.new_page()
            return [
                self._view(page, name, path, png, pdf, paper, landscape) for name, path in views
            ]
        finally:
            context.close()

    def _view(
        self,
        page: Any,
        name: str,
        path: str,
        png: bool,
        pdf: bool,
        paper: str | None,
        landscape: bool,
    ) -> Rendered:
        result = Rendered(name=name, path=path)

        def on_response(response: Any) -> None:
            if response.url.endswith("/render") and response.request.method == "POST":
                if not response.ok:
                    return
                data = response.json()
                result.rows[data["worksheet"]] = int(data.get("rows") or 0)
                result.tables[data["worksheet"]] = (data.get("table") or {}).get("rows") or []

        page.on("response", on_response)
        try:
            page.goto(self.base_url + path)
            page.wait_for_selector(".dashboard, .single-sheet, main.error", timeout=60_000)
            if page.locator("main.error").count():
                raise SubscriptionError(
                    f"無法開啟 view「{name}」：{page.locator('main.error').inner_text()}"
                )
            # 每張工作表都查詢完、畫好（地圖的形狀從 /api/geo/ 下載後才畫好）
            page.wait_for_function(
                "(() => { const items = Array.from(document.querySelectorAll('.worksheet'));"
                " return items.length > 0 && items.every(e => e.dataset.loading === 'false')"
                " && !document.querySelector('[data-drawn=\"false\"]'); })()",
                timeout=120_000,
            )
            page.wait_for_timeout(300)
            target = page.locator(".dashboard") if "/d/" in path else page.locator(".single-sheet")
            if png:
                result.png = target.screenshot()
            if pdf:
                result.pdf = page.pdf(
                    format=PAPER.get(paper or "", "A4"), landscape=landscape, print_background=True
                )
        finally:
            page.remove_listener("response", on_response)
        return result


def merge_pdfs(parts: list[bytes]) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for part in parts:
        writer.append(io.BytesIO(part))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------- 郵件


def build_message(
    job: Job, report: Report, rendered: list[Rendered], recipient: str, mail: MailSettings
) -> EmailMessage:
    sub = job.subscription
    assert sub is not None
    message = EmailMessage()
    title = sub.subject or f"{report.ir.workbook.name}：{'、'.join(r.name for r in rendered)}"
    message["Subject"] = title
    message["From"] = mail.sender
    message["To"] = recipient
    links = [(r.name, f"{mail.site_url}{r.path}" if mail.site_url else r.path) for r in rendered]
    text = [sub.message or "", *(f"{name}：{link}" for name, link in links)]
    message.set_content("\n".join(line for line in text if line) + "\n")
    images: list[tuple[str, bytes]] = []
    blocks = [f"<p>{escape(sub.message)}</p>" if sub.message else ""]
    for item, (name, link) in zip(rendered, links, strict=True):
        blocks.append(f'<h3><a href="{escape(link)}">{escape(name)}</a></h3>')
        if sub.attach_image and item.png is not None:
            cid = make_msgid(domain="tmig.local")
            images.append((cid, item.png))
            blocks.append(
                f'<a href="{escape(link)}"><img src="cid:{cid[1:-1]}" alt="{escape(name)}"></a>'
            )
    message.add_alternative("<html><body>" + "".join(blocks) + "</body></html>", subtype="html")
    html = message.get_body(("html",))
    assert isinstance(html, EmailMessage)
    for cid, data in images:
        html.add_related(data, maintype="image", subtype="png", cid=cid)
    pdfs = [r.pdf for r in rendered if r.pdf is not None]
    if sub.attach_pdf and pdfs:
        merged = pdfs[0] if len(pdfs) == 1 else merge_pdfs(pdfs)
        message.add_attachment(
            merged, maintype="application", subtype="pdf", filename=f"{report.ir.workbook.name}.pdf"
        )
    return message


def send_message(message: EmailMessage, mail: MailSettings) -> None:
    if mail.use_ssl:
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            mail.host, mail.port, timeout=mail.timeout, context=ssl.create_default_context()
        )
    else:
        client = smtplib.SMTP(mail.host, mail.port, timeout=mail.timeout)
    with client:
        if mail.starttls and not mail.use_ssl:
            client.starttls(context=ssl.create_default_context())
        if mail.username and mail.password:
            client.login(mail.username, mail.password)
        client.send_message(message)


class SubscriptionHandler:
    """排程器的訂閱處理程式（tmig.schedule.runner 的 Handler）。"""

    def __init__(self, renderer: Renderer, mail: MailSettings) -> None:
        self.renderer = renderer
        self.mail = mail

    def __call__(self, job: Job) -> str:
        sub = job.subscription
        if sub is None or not job.targets:
            raise SubscriptionError(f"{job.id} 不是訂閱")
        report = job.targets[0].report
        directory = self.renderer.authenticator.directory()
        assert directory is not None
        user = directory.find(f"{sub.user_domain}\\{sub.user}" if sub.user_domain else sub.user)
        if user is None:
            user = directory.find(sub.user)
        if user is None:
            raise SubscriptionError(f"訂閱者 {sub.user} 不在使用者目錄中")
        if not user.email:
            raise SubscriptionError(f"訂閱者 {user.name} 在使用者目錄中沒有 email")
        views = subscription_views(report, sub.view if sub.content_kind == "view" else None)
        rendered = self.renderer.render(
            user.name,
            views,
            png=sub.attach_image or not sub.attach_pdf,
            pdf=sub.attach_pdf,
            paper=sub.page_size,
            landscape=(sub.page_orientation or "").lower() == "landscape",
        )
        if not sub.send_if_view_empty and all(r.empty for r in rendered):
            return f"view 沒有資料，未寄送（{user.name}）"
        send_message(build_message(job, report, rendered, user.email, self.mail), self.mail)
        kinds = "、".join(
            k for k, on in (("圖片", sub.attach_image), ("PDF", sub.attach_pdf)) if on
        )
        return f"已寄給 {user.email}（{len(rendered)} 個 view；{kinds or '連結'}）"

    def close(self) -> None:
        self.renderer.close()


__all__ = [
    "MailSettings",
    "Rendered",
    "Renderer",
    "SubscriptionError",
    "SubscriptionHandler",
    "build_message",
    "load_mail",
    "merge_pdfs",
    "send_message",
    "subscription_views",
]
