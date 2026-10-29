"""fetch-expected（E1）：網址組法，以及對本機假 Tableau 伺服器的完整抓取流程。"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlsplit

import pytest

from .loader import chromium_available, load_tool

fetch_expected = load_tool("tools/fetch_expected/fetch_expected.py", "fetch_expected")


def test_normalize_views_url() -> None:
    assert (
        fetch_expected.normalize_views_url(
            "https://tab.example.com/#/site/fin/views/Sales/Overview?:iid=1"
        )
        == "https://tab.example.com/t/fin/views/Sales"
    )
    assert (
        fetch_expected.normalize_views_url("https://tab.example.com/#/views/Sales")
        == "https://tab.example.com/views/Sales"
    )
    assert (
        fetch_expected.normalize_views_url("https://tab.example.com/t/fin/views/Sales/")
        == "https://tab.example.com/t/fin/views/Sales"
    )


def test_view_url_encoding() -> None:
    url = fetch_expected.view_url(
        "https://tab.example.com/t/fin/views/Sales",
        "Sub-CategoryProfit",
        {
            "Category": "Furniture,Office Supplies",
            "year(Order Date)": "2024",
            "前 N 名": 2,
            "Note": r"a\,b",
        },
        "csv",
    )
    assert url == (
        "https://tab.example.com/t/fin/views/Sales/Sub-CategoryProfit.csv"
        "?Category=Furniture,Office%20Supplies&year(Order%20Date)=2024"
        "&%E5%89%8D%20N%20%E5%90%8D=2&Note=a%5C,b".replace("%5C", "\\")
    )


def test_update_fetched_at_keeps_comments(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        "# 註解\nworkbook: wb\nfetched_at: null   # 由工具填入\nsheets: []\ncases: []\n",
        encoding="utf-8",
    )
    fetch_expected.update_fetched_at(path, "2026-10-28T00:00:00+00:00")
    assert path.read_text(encoding="utf-8") == (
        "# 註解\nworkbook: wb\nfetched_at: 2026-10-28T00:00:00+00:00\nsheets: []\ncases: []\n"
    )


def test_plan_requires_views_url(tmp_path: Path) -> None:
    (tmp_path / "cases.yaml").write_text(
        "workbook: wb\ntableau_views_url: null\nsheets: []\ncases: []\n", encoding="utf-8"
    )
    with pytest.raises(fetch_expected.FetchError, match="tableau_views_url"):
        fetch_expected.plan(tmp_path)


def test_user_check(tmp_path: Path) -> None:
    (tmp_path / "cases.yaml").write_text(
        "workbook: wb\nuser:\n  username: Amy\n  groups: [Sales]\nsheets: []\ncases: []\n",
        encoding="utf-8",
    )
    assert fetch_expected.cases_user(tmp_path) == "Amy"
    assert fetch_expected.check_user("Amy", "amy") is None
    assert fetch_expected.check_user(None, "bob") is None
    assert "請自行確認" in fetch_expected.check_user("Amy", None)
    with pytest.raises(fetch_expected.FetchError, match="瀏覽器登入的帳號是 bob"):
        fetch_expected.check_user("Amy", "bob")
    assert (
        fetch_expected.server_of("https://tab.example.com/#/site/fin/views/Sales")
        == "https://tab.example.com"
    )


class FakeTableau(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []
    user: ClassVar[str] = "amy"

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        FakeTableau.requests.append(unquote(self.path))
        if parts.path == "/api/2.4/serverinfo":
            body = json.dumps({"serverInfo": {"restApiVersion": "3.15"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "workgroup_session_id=sess-1; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parts.path == "/api/3.15/sessions/current":
            if self.headers.get("X-Tableau-Auth") != "sess-1":
                self._send(401, "application/json", b"{}")
                return
            body = json.dumps({"session": {"user": {"id": "u", "name": FakeTableau.user}}})
            self._send(200, "application/json", body.encode())
        elif parts.path.endswith(".csv"):
            body = f"Region,Sales\nEast,{len(parts.query)}\n".encode()
            self._send(200, "text/csv; charset=utf-8", body)
        elif parts.path.endswith(".png"):
            self._send(200, "image/png", b"\x89PNG fake")
        else:
            self._send(404, "text/html", b"<html>login</html>")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTableau)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.mark.skipif(not chromium_available(), reason="需要 Playwright 的 Chromium")
def test_fetch_against_fake_server(tmp_path: Path, server: str) -> None:
    fixture = tmp_path / "report"
    fixture.mkdir()
    (fixture / "cases.yaml").write_text(
        f"workbook: wb\ntableau_views_url: {server}/t/fin/views/Sales\nfetched_at: null\n"
        "sheets:\n  - worksheet: ws_a\n    view: SheetA\n"
        "cases:\n  - name: default\n    url_params: {}\n  - name: east\n    url_params:\n      Region: East\n",
        encoding="utf-8",
    )
    FakeTableau.requests = []
    assert fetch_expected.main([str(fixture), "--profile", str(tmp_path / "profile")]) == 0
    assert (fixture / "expected" / "ws_a" / "default.csv").read_text(
        encoding="utf-8"
    ) == "Region,Sales\nEast,0\n"
    assert (fixture / "expected" / "ws_a" / "east.csv").read_text(
        encoding="utf-8"
    ) == "Region,Sales\nEast,11\n"
    assert (fixture / "expected" / "ws_a" / "east.png").read_bytes().startswith(b"\x89PNG")
    assert "/t/fin/views/Sales/SheetA.csv?Region=East" in FakeTableau.requests
    log = json.loads((fixture / "expected" / "fetch-log.json").read_text(encoding="utf-8"))
    assert [entry["status"] for entry in log] == [200, 200, 200, 200]
    assert "fetched_at: 20" in (fixture / "cases.yaml").read_text(encoding="utf-8")


@pytest.mark.skipif(not chromium_available(), reason="需要 Playwright 的 Chromium")
def test_fetch_checks_rls_user(
    tmp_path: Path, server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "report"
    fixture.mkdir()
    (fixture / "cases.yaml").write_text(
        f"workbook: wb\ntableau_views_url: {server}/t/fin/views/Sales\nfetched_at: null\n"
        "user:\n  username: Amy\n  groups: []\n"
        "sheets:\n  - worksheet: ws_a\n    view: SheetA\n"
        "cases:\n  - name: default\n    url_params: {}\n",
        encoding="utf-8",
    )
    FakeTableau.user = "bob"
    try:
        assert fetch_expected.main([str(fixture), "--profile", str(tmp_path / "profile")]) == 1
        assert "瀏覽器登入的帳號是 bob" in capsys.readouterr().err
        assert not (fixture / "expected" / "ws_a" / "default.csv").exists()
        FakeTableau.user = "amy"
        assert fetch_expected.main([str(fixture), "--profile", str(tmp_path / "profile")]) == 0
        assert (fixture / "expected" / "ws_a" / "default.csv").is_file()
    finally:
        FakeTableau.user = "amy"
