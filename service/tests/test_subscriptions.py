"""訂閱（Q3）端到端：以訂閱者的身分產生 PNG、PDF（列層級權限照套），以測試用的 SMTP 伺服器驗證寄送。

以加上列層級權限的示範報表（conftest 的 secure）為例：alice 只看得到 East，dave 屬於 Managers 看得到全部；
同一個 view 的訂閱，兩人收到的圖片不同。需要建置好的檢視器與 Playwright 的 Chromium，缺少時略過。
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import io
import socketserver
import threading
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from tmig.access.directory import Directory, DirectoryUser
from tmig.access.permissions import Permissions
from tmig.catalog import Catalog
from tmig.schedule.model import ScheduleFile
from tmig.schedule.runner import Job, Scheduler, plan_jobs
from tmig_service.auth import AuthSettings
from tmig_service.subscriptions import (
    MailSettings,
    Renderer,
    SubscriptionError,
    SubscriptionHandler,
    load_mail,
)

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "web" / "dist"


def chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file() or not chromium(),
    reason="需要建置好的檢視器（web/dist）與 Playwright 的 Chromium",
)

DIRECTORY = Directory(
    users=[
        DirectoryUser(
            name="alice", email="alice@example.com", groups=["All Users"], site_role="Viewer"
        ),
        DirectoryUser(
            name="dave",
            email="dave@example.com",
            groups=["All Users", "Managers"],
            site_role="Viewer",
        ),
        DirectoryUser(
            name="eve", email="eve@example.com", groups=["All Users"], site_role="Viewer"
        ),
        DirectoryUser(name="nomail", groups=["All Users", "Managers"], site_role="Viewer"),
        DirectoryUser(
            name="outsider", email="o@example.com", groups=["All Users"], site_role="Viewer"
        ),
    ]
)
PERMISSIONS = Permissions.model_validate(
    {
        "workbooks": [
            {
                "id": "w1",
                "name": "superstore_demo",
                "rules": [
                    {
                        "grantee": {"kind": "group", "name": "All Users"},
                        "capabilities": {"Read": "Allow"},
                    },
                    {
                        "grantee": {"kind": "user", "name": "outsider"},
                        "capabilities": {"Read": "Deny"},
                    },
                ],
            }
        ]
    }
)


class SmtpHandler(socketserver.StreamRequestHandler):
    """只處理 smtplib 用到的指令：EHLO、MAIL、RCPT、DATA、QUIT。"""

    messages: ClassVar[list[tuple[list[str], bytes]]] = []

    def reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        self.reply("220 test SMTP")
        recipients: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode().strip()
            upper = command.upper()
            if upper.startswith(("EHLO", "HELO")):
                self.reply("250-localhost")
                self.reply("250 8BITMIME")
            elif upper.startswith("MAIL FROM"):
                recipients = []
                self.reply("250 OK")
            elif upper.startswith("RCPT TO"):
                recipients.append(command.split(":", 1)[1].strip().strip("<>"))
                self.reply("250 OK")
            elif upper == "DATA":
                self.reply("354 End data with <CR><LF>.<CR><LF>")
                data = io.BytesIO()
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b".\n", b""):
                        break
                    data.write(chunk[1:] if chunk.startswith(b"..") else chunk)
                SmtpHandler.messages.append((recipients, data.getvalue()))
                self.reply("250 OK queued")
            elif upper == "QUIT":
                self.reply("221 Bye")
                return
            else:
                self.reply("250 OK")


@pytest.fixture
def smtp() -> Iterator[MailSettings]:
    SmtpHandler.messages = []
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SmtpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield MailSettings(
        host="127.0.0.1",
        port=server.server_address[1],
        starttls=False,
        sender="報表平台 <bi@example.com>",
        site_url="https://bi.example.com",
    )
    server.shutdown()
    server.server_close()


def schedules(*subscriptions: dict[str, Any]) -> ScheduleFile:
    return ScheduleFile.model_validate(
        {
            "timezone": "Asia/Taipei",
            "schedules": [
                {
                    "id": "s",
                    "name": "週一",
                    "frequency": {"kind": "weekly", "start": "08:00:00", "weekdays": ["Monday"]},
                }
            ],
            "subscriptions": list(subscriptions),
        }
    )


def subscription(sid: str, user: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "user": user,
        "subject": f"{user} 的週報",
        "schedule": "s",
        "content_kind": "view",
        "workbook_name": "superstore_demo",
        "view": "Sales by Region",
        **extra,
    }


def parts(raw: bytes) -> tuple[Message, list[bytes], list[bytes], str]:
    """（郵件, 內嵌圖片, PDF 附件, HTML 內文）。"""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    images: list[bytes] = []
    pdfs: list[bytes] = []
    html = ""
    for part in message.walk():
        kind = part.get_content_type()
        payload = part.get_payload(decode=True)
        if kind == "image/png" and isinstance(payload, bytes):
            images.append(payload)
        elif kind == "application/pdf" and isinstance(payload, bytes):
            pdfs.append(payload)
        elif kind == "text/html" and isinstance(payload, bytes):
            html = payload.decode("utf-8")
    return message, images, pdfs, html


@pytest.fixture
def handler(fixtures: Path, smtp: MailSettings) -> Iterator[SubscriptionHandler]:
    auth = AuthSettings(mode="oidc", directory=DIRECTORY, permissions=PERMISSIONS)
    renderer = Renderer(fixtures, auth, DIST)
    handler = SubscriptionHandler(renderer, smtp)
    yield handler
    handler.close()


def jobs(fixtures: Path, *subs: dict[str, Any]) -> dict[str, Job]:
    planned = plan_jobs(schedules(*subs), Catalog.load(fixtures))
    for job in planned:
        assert job.runnable, job.problem
    return {j.id: j for j in planned}


def test_subscribers_get_their_own_data(fixtures: Path, handler: SubscriptionHandler) -> None:
    planned = jobs(
        fixtures,
        subscription("a", "alice"),
        subscription("d", "dave", attach_pdf=True, page_orientation="Landscape", page_size="A4"),
    )
    renders: dict[str, Any] = {}
    original = handler.renderer.render

    def spy(user: str, views: list[tuple[str, str]], **kwargs: Any) -> Any:
        renders[user] = original(user, views, **kwargs)
        return renders[user]

    handler.renderer.render = spy  # type: ignore[method-assign]
    assert handler(planned["subscription:a"]) == "已寄給 alice@example.com（1 個 view；圖片）"
    assert handler(planned["subscription:d"]) == "已寄給 dave@example.com（1 個 view；圖片、PDF）"

    # 列層級權限照套：alice 只看得到 East，dave 看得到前 3 名
    regions = {u: {row[0] for row in r[0].tables["ws_sales_by_region"]} for u, r in renders.items()}
    assert regions == {"alice": {"East"}, "dave": {"Central", "East", "West"}}

    (to_alice, raw_alice), (to_dave, raw_dave) = SmtpHandler.messages
    assert to_alice == ["alice@example.com"] and to_dave == ["dave@example.com"]
    message, images, pdfs, html = parts(raw_alice)
    assert message["Subject"] == "alice 的週報"
    assert "報表平台" in str(message["From"])
    assert len(images) == 1 and images[0].startswith(b"\x89PNG") and pdfs == []
    assert "https://bi.example.com/w/secure/s/ws_sales_by_region" in html
    _, dave_images, dave_pdfs, _ = parts(raw_dave)
    assert dave_images[0] != images[0]  # 檔案內容依訂閱者不同
    (pdf,) = dave_pdfs
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    page = reader.pages[0]
    assert float(page.mediabox.width) > float(page.mediabox.height)  # 橫向


def test_workbook_subscription_merges_pdf(fixtures: Path, handler: SubscriptionHandler) -> None:
    planned = jobs(
        fixtures,
        {
            "id": "w",
            "user": "dave",
            "schedule": "s",
            "content_kind": "workbook",
            "workbook_name": "superstore_demo",
            "attach_image": False,
            "attach_pdf": True,
            "message": "本週的報表",
        },
    )
    assert handler(planned["subscription:w"]) == "已寄給 dave@example.com（1 個 view；PDF）"
    ((_, raw),) = SmtpHandler.messages
    message, images, pdfs, html = parts(raw)
    assert message["Subject"] == "superstore_demo：銷售總覽"
    assert images == [] and len(pdfs) == 1 and pdfs[0].startswith(b"%PDF")
    assert "本週的報表" in html


def test_empty_view_and_failures(fixtures: Path, handler: SubscriptionHandler) -> None:
    planned = jobs(
        fixtures,
        subscription("e", "eve", send_if_view_empty=False),
        subscription("n", "nomail"),
        subscription("o", "outsider"),
        subscription("g", "ghost"),
    )
    # eve 沒有負責的地區：看不到任何資料，訂閱設定為不寄送
    assert handler(planned["subscription:e"]) == "view 沒有資料，未寄送（eve）"
    with pytest.raises(SubscriptionError, match="沒有 email"):
        handler(planned["subscription:n"])
    with pytest.raises(SubscriptionError, match="無法開啟 view"):
        handler(planned["subscription:o"])  # 沒有這份報表的權限
    with pytest.raises(SubscriptionError, match="不在使用者目錄中"):
        handler(planned["subscription:g"])
    assert SmtpHandler.messages == []


def test_scheduler_runs_subscriptions(
    fixtures: Path, handler: SubscriptionHandler, tmp_path: Path
) -> None:
    planned = list(jobs(fixtures, subscription("a", "alice")).values())
    tpe = ZoneInfo("Asia/Taipei")
    now = [dt.datetime(2026, 11, 1, 12, 0, tzinfo=tpe)]
    scheduler = Scheduler(
        planned, "Asia/Taipei", {"subscription": handler}, tmp_path, lambda: now[0]
    )
    assert scheduler.tick() == []
    now[0] = dt.datetime(2026, 11, 2, 8, 0, tzinfo=tpe)  # 週一 8 點
    (record,) = scheduler.tick()
    assert record.status == "ok" and "alice@example.com" in record.detail
    assert len(SmtpHandler.messages) == 1


def test_load_mail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "mail.toml"
    config.write_text(
        '[smtp]\nhost = "smtp.example.com"\nsender = "bi@example.com"\nusername = "bot"\n'
        'password = "${TMIG_SMTP_PASSWORD}"\n[site]\nurl = "https://bi.example.com/"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TMIG_SMTP_PASSWORD", "pw")
    mail = load_mail(config)
    assert (mail.host, mail.port, mail.starttls, mail.password) == (
        "smtp.example.com",
        587,
        True,
        "pw",
    )
    assert mail.site_url == "https://bi.example.com"
    config.write_text('[smtp]\nhost = "h"\nsender = "s"\npassword = "literal"\n', encoding="utf-8")
    with pytest.raises(SubscriptionError, match="不能直接寫在設定檔中"):
        load_mail(config)


def test_merge_pdfs() -> None:
    from pypdf import PdfReader, PdfWriter

    from tmig_service.subscriptions import merge_pdfs

    def blank(pages: int) -> bytes:
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    merged = merge_pdfs([blank(1), blank(2)])
    assert len(PdfReader(io.BytesIO(merged)).pages) == 3


def test_cli_runs_subscription(fixtures: Path, smtp: MailSettings, tmp_path: Path) -> None:
    """命令列：tmig schedule run 以郵件設定檔與身分驗證設定檔（使用者目錄、權限）寄出訂閱。"""
    from tmig.cli import main

    (tmp_path / "directory.json").write_text(DIRECTORY.model_dump_json(), encoding="utf-8")
    (tmp_path / "permissions.json").write_text(PERMISSIONS.model_dump_json(), encoding="utf-8")
    auth = tmp_path / "auth.toml"
    auth.write_text(
        '[auth]\nmode = "dev"\ndirectory = "directory.json"\npermissions = "permissions.json"\n',
        encoding="utf-8",
    )
    mail = tmp_path / "mail.toml"
    mail.write_text(
        f'[smtp]\nhost = "127.0.0.1"\nport = {smtp.port}\nstarttls = false\nsender = "bi@example.com"\n',
        encoding="utf-8",
    )
    plan = tmp_path / "schedules.json"
    plan.write_text(schedules(subscription("a", "alice")).model_dump_json(), encoding="utf-8")
    args = [
        "schedule",
        "run",
        str(plan),
        "--fixtures",
        str(fixtures),
        "--mail",
        str(mail),
        "--auth",
        str(auth),
        "--web",
        str(DIST),
        "--job",
        "subscription:a",
    ]
    assert main(args) == 0
    ((recipients, _),) = SmtpHandler.messages
    assert recipients == ["alice@example.com"]


def test_example_mail_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMIG_SMTP_PASSWORD", "pw")
    mail = load_mail(ROOT / "service" / "mail.example.toml")
    assert mail.host == "smtp.example.com" and mail.starttls and not mail.use_ssl
