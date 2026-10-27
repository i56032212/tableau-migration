"""tools/tableau_server：以本機假 Tableau Server（REST API）跑盤點與下載的完整流程。"""

from __future__ import annotations

import http.server
import io
import json
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from tmig.verify.cases import load_cases

from .loader import ROOT, load_tool

tableau_server = load_tool("tools/tableau_server/tableau_server.py", "tableau_server")

DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"
PILOT = ROOT / "kernel" / "tests" / "census" / "data" / "pilot.twb"
API = "3.15"
TOKEN = "tok-1"
SITE_ID = "site-1"


def demo_bytes(include_extract: bool) -> bytes:
    if include_extract:
        return DEMO.read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(DEMO) as source, zipfile.ZipFile(buffer, "w") as out:
        for info in source.infolist():
            if not info.filename.endswith(".hyper"):
                out.writestr(info, source.read(info.filename))
    return buffer.getvalue()


WORKBOOKS: list[dict[str, Any]] = [
    {
        "id": "wb-1",
        "name": "銷售總覽",
        "contentUrl": "SalesOverview",
        "webpageUrl": "https://tableau.example.com/#/site/sales/workbooks/1",
        "updatedAt": "2026-10-01T00:00:00Z",
        "hasExtracts": "true",
        "project": {"id": "p-1", "name": "Sales"},
        "owner": {"id": "u-1", "name": "amy"},
    },
    {
        "id": "wb-2",
        "name": "財務月報",
        "contentUrl": "Finance",
        "updatedAt": "2026-09-01T00:00:00Z",
        "project": {"id": "p-2", "name": "Finance"},
        "owner": {"id": "u-2", "name": "bob"},
    },
    {
        "id": "wb-3",
        "name": "Ops.v2",
        "contentUrl": "Opsv2",
        "updatedAt": "2026-08-01T00:00:00Z",
        "project": {"id": "p-3", "name": "Ops"},
        "owner": {"id": "u-3", "name": "cat"},
    },
]

VIEWS: list[dict[str, Any]] = [
    {
        "name": "Sales by Region",
        "viewUrlName": "SalesbyRegion",
        "workbook": {"id": "wb-1"},
        "count": 100,
    },
    {
        "name": "Monthly Profit",
        "viewUrlName": "MonthlyProfit",
        "workbook": {"id": "wb-1"},
        "count": 50,
    },
    {
        "name": "Sub-Category Profit",
        "contentUrl": "SalesOverview/sheets/Sub-CategoryProfit",
        "workbook": {"id": "wb-1"},
        "count": 30,
    },
    {"name": "Overview", "viewUrlName": "Overview", "workbook": {"id": "wb-3"}, "count": 5},
]


class FakeServer(http.server.BaseHTTPRequestHandler):
    calls: ClassVar[list[str]] = []
    secret: ClassVar[str] = "s3cret"

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'name="tableau_workbook"; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("X-Tableau-Auth") != TOKEN:
            self._json(401, {"error": {"code": "401002", "summary": "Unauthorized Access"}})
            return False
        return True

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        FakeServer.calls.append(f"POST {path}")
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == f"/api/{API}/auth/signin":
            creds = body.get("credentials", {})
            if (
                creds.get("personalAccessTokenName") != "tmig"
                or creds.get("personalAccessTokenSecret") != FakeServer.secret
                or creds.get("site", {}).get("contentUrl") != "sales"
            ):
                self._json(401, {"error": {"code": "401001", "summary": "Signin Error"}})
                return
            self._json(
                200,
                {
                    "credentials": {
                        "token": TOKEN,
                        "site": {"id": SITE_ID, "contentUrl": "sales"},
                        "user": {"id": "u-0"},
                    }
                },
            )
        elif path == f"/api/{API}/auth/signout":
            self.send_response(204)
            self.end_headers()
        else:
            self._json(404, {"error": {"code": "404000"}})

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        FakeServer.calls.append(f"GET {path}?{parts.query}")
        if path == "/api/2.4/serverinfo":
            self._json(
                200,
                {"serverInfo": {"productVersion": {"value": "2022.1.5"}, "restApiVersion": API}},
            )
            return
        if not self._authorized():
            return
        site = f"/api/{API}/sites/{SITE_ID}/"
        if not path.startswith(site):
            self._json(404, {"error": {"code": "404000"}})
            return
        rest = path.removeprefix(site)
        if rest == "workbooks":
            size = int(query["pageSize"][0])
            page = int(query["pageNumber"][0])
            items = WORKBOOKS[(page - 1) * size : page * size]
            self._json(
                200,
                {
                    "pagination": {
                        "pageNumber": str(page),
                        "pageSize": str(size),
                        "totalAvailable": str(len(WORKBOOKS)),
                    },
                    "workbooks": {"workbook": items} if items else {},
                },
            )
        elif rest == "views":
            assert query.get("includeUsageStatistics") == ["true"]
            views = [
                {
                    **{k: v for k, v in view.items() if k != "count"},
                    "usage": {"totalViewCount": str(view["count"])},
                }
                for view in VIEWS
            ]
            self._json(
                200,
                {
                    "pagination": {
                        "pageNumber": "1",
                        "pageSize": "2",
                        "totalAvailable": str(len(views)),
                    },
                    "views": {"view": views[:2] if query["pageNumber"] == ["1"] else views[2:]},
                },
            )
        elif rest.endswith("/connections"):
            wb_id = rest.split("/")[1]
            kinds = {"wb-1": "hyper", "wb-2": "sqlproxy", "wb-3": "oracle"}
            self._json(200, {"connections": {"connection": [{"id": "c", "type": kinds[wb_id]}]}})
        elif rest.endswith("/views"):
            wb_id = rest.split("/")[1]
            views = [v for v in VIEWS if v["workbook"]["id"] == wb_id]
            self._json(200, {"views": {"view": views}})
        elif rest.endswith("/content"):
            wb_id = rest.split("/")[1]
            include = query.get("includeExtract") == ["True"]
            if wb_id == "wb-1":
                self._file(demo_bytes(include), "application/octet-stream", "Sales Overview.twbx")
            elif wb_id == "wb-3":
                self._file(PILOT.read_bytes(), "application/xml", "Ops.v2.twb")
            else:
                self._json(
                    403,
                    {"error": {"code": "403019", "summary": "Forbidden", "detail": "no ExportXml"}},
                )
        else:
            self._json(404, {"error": {"code": "404000"}})

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    FakeServer.calls = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "s3cret")


def auth(server: str) -> list[str]:
    return ["--server", server, "--site", "sales", "--pat-name", "tmig", "--page-size", "2"]


def downloads() -> list[str]:
    return [c for c in FakeServer.calls if c.startswith("GET") and "/content?" in c]


def test_inventory_end_to_end(server: str, tmp_path: Path) -> None:
    out = tmp_path / "inventory"
    assert tableau_server.main(["inventory", *auth(server), "--out", str(out)]) == 0

    meta = json.loads((out / "server.json").read_text(encoding="utf-8"))
    assert meta["site"] == "sales"
    assert meta["extracts_omitted"] is True
    entries = {w["name"]: w for w in meta["workbooks"]}
    sales = entries["銷售總覽"]
    assert sales["file"] == "workbooks/Sales/銷售總覽.twbx"
    assert sales["views"] == 180
    assert sales["sheets"] == ["Sales by Region", "Monthly Profit", "Sub-Category Profit"]
    assert sales["connections"] == ["hyper"]
    assert sales["has_extracts"] is True
    assert entries["Ops.v2"]["file"] == "workbooks/Ops/Ops.v2.twb"
    finance = entries["財務月報"]
    assert finance["file"] is None
    assert "403019" in finance["download_error"] and "下載／另存新檔" in finance["download_error"]
    assert (out / sales["file"]).is_file()
    assert "includeExtract=False" in downloads()[0]
    assert f"POST /api/{API}/auth/signout" in FakeServer.calls

    markdown = (out / "inventory.md").read_text(encoding="utf-8")
    assert "（site：sales）" in markdown
    b_section = markdown.split("## B 級候選", 1)[1].split("##", 1)[0]
    assert b_section.index("銷售總覽") < b_section.index("Ops.v2")
    assert "| 銷售總覽 | Sales | 180 |" in b_section
    assert "沒有下載權限" in markdown

    # 第二次：沒有更新的報表沿用上次下載的檔案
    FakeServer.calls = []
    assert tableau_server.main(["inventory", *auth(server), "--out", str(out)]) == 0
    assert [c.split("/workbooks/")[1].split("/")[0] for c in downloads()] == ["wb-2"]
    assert tableau_server.main(["inventory", *auth(server), "--out", str(out), "--refresh"]) == 0
    assert len(downloads()) == 4


def test_inventory_filters_and_no_download(server: str, tmp_path: Path) -> None:
    out = tmp_path / "inv"
    args = ["inventory", *auth(server), "--out", str(out), "--project", "Sales", "--no-download"]
    assert tableau_server.main(args) == 0
    meta = json.loads((out / "server.json").read_text(encoding="utf-8"))
    assert [w["name"] for w in meta["workbooks"]] == ["銷售總覽"]
    assert downloads() == []
    assert not (out / "inventory.md").exists()


def test_fetch_creates_fixture(
    server: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fixtures" / "sales_overview"
    args = ["fetch", *auth(server), "--workbook", "SalesOverview", "--fixture", str(fixture)]
    assert tableau_server.main(args) == 0
    twbx = fixture / "sales_overview.twbx"
    with zipfile.ZipFile(twbx) as archive:
        assert any(name.endswith(".hyper") for name in archive.namelist())
    assert "includeExtract=True" in downloads()[0]
    out = capsys.readouterr().out
    assert "起步條件全部符合" in out

    cases_path = fixture / "cases.yaml"
    cases = load_cases(cases_path)
    assert cases.workbook == "wb_superstore_demo"
    assert cases.tableau_views_url == f"{server}/#/site/sales/views/SalesOverview"
    assert [(s.worksheet, s.view) for s in cases.sheets] == [
        ("ws_sales_by_region", "SalesbyRegion"),
        ("ws_monthly_profit", "MonthlyProfit"),
        ("ws_sub_category_profit", "Sub-CategoryProfit"),
    ]
    assert [c.name for c in cases.cases] == ["default"]
    text = cases_path.read_text(encoding="utf-8")
    assert "# - worksheet: ws_region_share_by_year  # Region Share by Year" in text
    assert "參數：前 N 名" in text
    assert yaml.safe_load(text)["fetched_at"] is None

    # 已有 cases.yaml 時不覆蓋
    cases_path.write_text(text + "# 手動修改\n", encoding="utf-8")
    assert tableau_server.main(args) == 0
    assert cases_path.read_text(encoding="utf-8").endswith("# 手動修改\n")


def test_bad_secret(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TABLEAU_PAT_SECRET", "wrong")
    assert tableau_server.main(["inventory", *auth(server), "--out", str(tmp_path)]) == 1
    assert "登入失敗：HTTP 401" in capsys.readouterr().err


def test_requires_auth_method(capsys: pytest.CaptureFixture[str]) -> None:
    assert tableau_server.main(["inventory", "--server", "http://127.0.0.1:9"]) == 1
    assert "--pat-name" in capsys.readouterr().err


def test_find_workbook_ambiguous() -> None:
    books = [
        {"id": "1", "name": "月報", "project": {"name": "A"}},
        {"id": "2", "name": "月報", "project": {"name": "B"}},
    ]
    with pytest.raises(tableau_server.TableauError, match="--project"):
        tableau_server.find_workbook(books, "月報", None)
    assert tableau_server.find_workbook(books, "月報", "B")["id"] == "2"
    assert tableau_server.find_workbook(books, "1", None)["id"] == "1"
    with pytest.raises(tableau_server.TableauError, match="找不到"):
        tableau_server.find_workbook(books, "週報", None)


def test_safe_name() -> None:
    assert tableau_server.safe_name("CON") == "CON_"
    assert tableau_server.safe_name("a<b>:c?") == "a_b_c_"
    assert tableau_server.safe_name("報表. ") == "報表"
    assert tableau_server.safe_name("   ") == "workbook"
    assert len(tableau_server.safe_name("x" * 200)) == 80
