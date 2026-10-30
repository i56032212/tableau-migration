"""tools/tableau_server：以本機假 Tableau Server（REST API）跑盤點與下載的完整流程。"""

from __future__ import annotations

import csv
import http.server
import io
import json
import sys
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from tmig.verify.cases import CasesFile, load_cases

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
        "showTabs": "false",
        "project": {"id": "p-3", "name": "Ops"},
        "owner": {"id": "u-3", "name": "cat"},
    },
]

VIEWS: list[dict[str, Any]] = [
    {
        "id": "v-1",
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
    {
        "id": "v-4",
        "name": "Overview",
        "viewUrlName": "Overview",
        "workbook": {"id": "wb-3"},
        "count": 5,
    },
]


DATASOURCES: list[dict[str, Any]] = [
    {
        "id": "ds-1",
        "name": "OrdersPDS",
        "contentUrl": "OrdersPDS",
        "updatedAt": "2026-10-01T00:00:00Z",
    }
]
PUBLISHED: dict[bool, bytes] = {}
"""Published Data Source 的 .tdsx：含萃取（True）與不含萃取（False）。"""
WORKBOOK_CONNECTIONS: dict[str, list[dict[str, Any]]] = {
    "wb-1": [{"id": "c", "type": "hyper", "embedPassword": False}],
    "wb-2": [{"id": "c", "type": "sqlproxy", "embedPassword": False}],
    # 萃取的來源連線沒有嵌入密碼：檢視時讀萃取檔，不影響起步條件
    "wb-3": [
        {
            "id": "c",
            "type": "oracle",
            "embedPassword": False,
            "userName": "scott",
            "datasource": {"id": "d-3", "name": "Orders"},
        }
    ],
    "wb-4": [{"id": "c", "type": "sqlproxy", "embedPassword": False}],
    "wb-5": [{"id": "c", "type": "hyper", "embedPassword": False}],
}
DATASOURCE_CONNECTIONS: dict[str, list[dict[str, Any]]] = {
    "ds-1": [{"id": "pc", "type": "oracle", "embedPassword": True, "userName": "reader"}]
}
"""Published Data Source 自己的連線。"""
PUBLISHED_WORKBOOK: list[bytes] = [b""]
RLS_WORKBOOK: list[bytes] = [b""]
"""用到使用者函式（列層級權限）的報表。"""
CURRENT_USER = {"id": "u-0", "name": "amy", "fullName": "Amy Lin", "domain": {"name": "local"}}
SITE_USERS = [
    {**CURRENT_USER, "email": "amy@example.com", "siteRole": "Viewer"},
    {
        "id": "u-1",
        "name": "bob",
        "fullName": "Bob Chen",
        "siteRole": "Explorer",
        "domain": {"name": "CORP"},
    },
    {"id": "u-2", "name": "Cat", "siteRole": "Unlicensed"},
]
GROUP_MEMBERS = {"g-1": ["u-0", "u-1", "u-2"], "g-2": ["u-1"], "g-3": ["u-0"]}
PROJECTS = [
    {"id": "p-1", "name": "Sales", "contentPermissions": "LockedToProject", "owner": {"id": "u-0"}},
    {
        "id": "p-2",
        "name": "Finance",
        "contentPermissions": "ManagedByOwner",
        "owner": {"id": "u-0"},
    },
    {"id": "p-3", "name": "Ops", "parentProjectId": "p-2", "owner": {"id": "u-0"}},
]


def caps(**modes: str) -> dict[str, Any]:
    return {"capability": [{"name": k, "mode": v} for k, v in modes.items()]}


SCHEDULES = [
    {
        "id": "s-1",
        "name": "每天 6 點",
        "state": "Active",
        "priority": "50",
        "type": "Extract",
        "frequency": "Daily",
    },
    {
        "id": "s-2",
        "name": "週一報表",
        "state": "Active",
        "type": "Subscription",
        "frequency": "Weekly",
    },
    {"id": "s-3", "name": "壞掉的", "state": "Suspended", "type": "Extract", "frequency": "Yearly"},
]
SCHEDULE_DETAILS = {
    "s-1": {"frequencyDetails": {"start": "06:00:00"}},
    "s-2": {
        "frequencyDetails": {
            "start": "08:00:00",
            "intervals": {"interval": [{"weekDay": "Monday"}]},
        }
    },
    "s-3": {"frequencyDetails": {"start": "01:00:00"}},
}
TASKS = [
    {
        "extractRefresh": {
            "id": "t-1",
            "priority": "50",
            "consecutiveFailedCount": "2",
            "type": "RefreshExtractTask",
            "schedule": {"id": "s-1"},
            "datasource": {"id": "ds-1"},
        }
    },
    {
        "extractRefresh": {
            "id": "t-2",
            "type": "IncrementExtractTask",
            "schedule": {"id": "s-1"},
            "workbook": {"id": "wb-1"},
        }
    },
    {
        # Tableau Cloud：排程寫在工作裡
        "extractRefresh": {
            "id": "t-3",
            "schedule": {
                "frequency": "Hourly",
                "frequencyDetails": {
                    "start": "00:00:00",
                    "end": "00:00:00",
                    "intervals": {"interval": [{"hours": "4"}]},
                },
            },
            "workbook": {"id": "wb-1"},
        }
    },
]
SUBSCRIPTIONS = [
    {
        "id": "sub-1",
        "subject": "週報",
        "attachImage": "true",
        "attachPdf": "true",
        "pageOrientation": "Landscape",
        "pageSizeOption": "A4",
        "suspended": "false",
        "content": {"id": "v-1", "type": "View", "sendIfViewEmpty": "false"},
        "schedule": {"id": "s-2", "name": "週一報表"},
        "user": {"id": "u-0", "name": "amy"},
    },
    {
        "id": "sub-2",
        "subject": "整份報表",
        "content": {"id": "wb-1", "type": "Workbook"},
        "schedule": {"id": "s-2"},
        "user": {"id": "u-1"},
    },
]

PERMISSIONS: dict[str, list[dict[str, Any]]] = {
    "projects/p-1/permissions": [
        {"group": {"id": "g-3"}, "capabilities": caps(ProjectLeader="Allow")}
    ],
    "projects/p-1/default-permissions/workbooks": [
        {"group": {"id": "g-1"}, "capabilities": caps(Read="Allow", ExportXml="Deny")}
    ],
    "workbooks/wb-1/permissions": [
        {"group": {"id": "g-1"}, "capabilities": caps(Read="Allow", ExportXml="Deny")}
    ],
    "workbooks/wb-2/permissions": [
        {"group": {"id": "g-2"}, "capabilities": caps(Read="Allow")},
        {"user": {"id": "u-1"}, "capabilities": caps(Read="Deny")},
        {"user": {"id": "u-gone"}, "capabilities": caps(Read="Allow")},
    ],
    "workbooks/wb-3/permissions": [{"group": {"id": "g-1"}, "capabilities": caps(Read="Allow")}],
    "views/v-4/permissions": [{"user": {"id": "u-2"}, "capabilities": caps(Read="Allow")}],
}
CURRENT_GROUPS = [
    {"id": "g-1", "name": "All Users", "domain": {"name": "local"}},
    {"id": "g-2", "name": "Sales", "domain": {"name": "corp.example.com"}},
    {"id": "g-3", "name": "Managers", "domain": {"name": "local"}},
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
        if path == f"/api/{API}/schedules":
            self._json(
                200,
                {
                    "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "3"},
                    "schedules": {"schedule": SCHEDULES},
                },
            )
            return
        if path.startswith(f"/api/{API}/schedules/"):
            sid = path.rsplit("/", 1)[1]
            base = next(x for x in SCHEDULES if x["id"] == sid)
            self._json(200, {"schedule": {**base, **SCHEDULE_DETAILS[sid]}})
            return
        site = f"/api/{API}/sites/{SITE_ID}/"
        if not path.startswith(site):
            self._json(404, {"error": {"code": "404000"}})
            return
        rest = path.removeprefix(site)
        if rest == "users/u-0":
            self._json(200, {"user": CURRENT_USER})
            return
        if rest == "users":
            if query.get("fields") == ["_all_"]:
                users = SITE_USERS
            else:
                users = [
                    {k: v for k, v in u.items() if k in ("id", "name", "siteRole")}
                    for u in SITE_USERS
                ]
            self._json(
                200,
                {
                    "pagination": {
                        "pageNumber": "1",
                        "pageSize": "100",
                        "totalAvailable": str(len(users)),
                    },
                    "users": {"user": users},
                },
            )
            return
        if rest == "tasks/extractRefreshes":
            self._json(200, {"tasks": {"task": TASKS}})
            return
        if rest == "subscriptions":
            self._json(
                200,
                {
                    "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "2"},
                    "subscriptions": {"subscription": SUBSCRIPTIONS},
                },
            )
            return
        if rest == "projects":
            self._json(
                200,
                {
                    "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "3"},
                    "projects": {"project": PROJECTS},
                },
            )
            return
        if rest.endswith("/permissions") or "/default-permissions/" in rest:
            self._json(200, {"permissions": {"granteeCapabilities": PERMISSIONS.get(rest, [])}})
            return
        if rest == "groups":
            self._json(
                200,
                {
                    "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "3"},
                    "groups": {"group": CURRENT_GROUPS},
                },
            )
            return
        if rest.startswith("groups/") and rest.endswith("/users"):
            members = GROUP_MEMBERS[rest.split("/")[1]]
            self._json(
                200,
                {
                    "pagination": {
                        "pageNumber": "1",
                        "pageSize": "100",
                        "totalAvailable": str(len(members)),
                    },
                    "users": {"user": [{"id": m} for m in members]} if members else {},
                },
            )
            return
        if rest == "users/u-0/groups":
            self._json(
                200,
                {
                    "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "3"},
                    "groups": {"group": CURRENT_GROUPS},
                },
            )
            return
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
        elif rest == "datasources":
            self._json(
                200,
                {
                    "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "1"},
                    "datasources": {"datasource": DATASOURCES},
                },
            )
        elif rest.startswith("datasources/") and rest.endswith("/content"):
            include = query.get("includeExtract") == ["True"]
            self._file(PUBLISHED[include], "application/octet-stream", "OrdersPDS.tdsx")
        elif rest.startswith("datasources/") and rest.endswith("/connections"):
            items = DATASOURCE_CONNECTIONS[rest.split("/")[1]]
            self._json(200, {"connections": {"connection": items}})
        elif rest.endswith("/connections"):
            items = WORKBOOK_CONNECTIONS[rest.split("/")[1]]
            self._json(200, {"connections": {"connection": items}})
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
            elif wb_id == "wb-4":
                self._file(PUBLISHED_WORKBOOK[0], "application/xml", "Orders PDS.twb")
            elif wb_id == "wb-5":
                self._file(RLS_WORKBOOK[0], "application/xml", "Secure.twb")
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
    # 連線設定只記類型與是否嵌入密碼，不記帳號
    assert entries["Ops.v2"]["connection_details"] == [
        {
            "type": "oracle",
            "datasource": "Orders",
            "published_datasource": None,
            "embed_password": False,
            "authentication": None,
        }
    ]
    assert "scott" not in (out / "server.json").read_text(encoding="utf-8")
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


# ---------------------------------------------------------------- Published Data Source（N8）


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """伺服器上多一份連到 Published Data Source 的報表，與資料源 OrdersPDS。"""
    from ..parser import builder as b
    from ..parser import test_published as pds

    hyper = tmp_path / "pds.hyper"
    pds.make_hyper(hyper, tmp_path)
    PUBLISHED[True] = pds.write_tdsx(tmp_path / "with.tdsx", pds.tds(), hyper).read_bytes()
    PUBLISHED[False] = pds.write_tdsx(tmp_path / "without.tdsx", pds.tds(), None).read_bytes()
    xml = b.workbook(datasource=pds.sqlproxy_datasource(), worksheets=pds.sheet())
    PUBLISHED_WORKBOOK[0] = xml.encode("utf-8")
    workbook = {
        "id": "wb-4",
        "name": "Orders PDS",
        "contentUrl": "OrdersPDSReport",
        "updatedAt": "2026-10-02T00:00:00Z",
        "project": {"id": "p-1", "name": "Sales"},
        "owner": {"id": "u-1", "name": "amy"},
    }
    monkeypatch.setattr(sys.modules[__name__], "WORKBOOKS", [*WORKBOOKS, workbook])


def test_fetch_merges_published_datasource(
    server: str, tmp_path: Path, published: None, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fixtures" / "orders_pds"
    args = ["fetch", *auth(server), "--workbook", "OrdersPDSReport", "--fixture", str(fixture)]
    assert tableau_server.main(args) == 0
    out = capsys.readouterr().out
    assert "已併入 Published Data Source「Orders (Published)」" in out
    assert "起步條件全部符合" in out
    assert (fixture / "original" / "orders_pds.twb").is_file()
    assert (fixture / "published" / "OrdersPDS.tdsx").is_file()
    with zipfile.ZipFile(fixture / "orders_pds.twbx") as archive:
        assert "Data/Published/OrdersPDS/federated_1.hyper" in archive.namelist()
    calls = [c for c in FakeServer.calls if "/datasources/ds-1/content" in c]
    assert len(calls) == 1 and "includeExtract=True" in calls[0]
    cases = load_cases(fixture / "cases.yaml")
    assert [s.worksheet for s in cases.sheets] == []  # 這份報表的工作表沒有發佈成 view


def test_inventory_merges_published_definitions(
    server: str, tmp_path: Path, published: None
) -> None:
    out = tmp_path / "inventory"
    assert tableau_server.main(["inventory", *auth(server), "--out", str(out)]) == 0
    meta = json.loads((out / "server.json").read_text(encoding="utf-8"))
    entry = next(w for w in meta["workbooks"] if w["id"] == "wb-4")
    assert entry["file"] == "workbooks/Sales/Orders PDS.twbx"
    assert entry["published"] == [
        "已併入 Published Data Source「Orders (Published)」（下載時不含萃取檔）"
    ]
    assert (out / "original" / "Sales" / "Orders PDS.twb").is_file()
    calls = [c for c in FakeServer.calls if "/datasources/ds-1/content" in c]
    assert len(calls) == 1 and "includeExtract=False" in calls[0]
    # 資料源自己的連線（嵌入了密碼）
    assert entry["connection_details"] == [
        {
            "type": "oracle",
            "datasource": "OrdersPDS",
            "published_datasource": "Orders (Published)",
            "embed_password": True,
            "authentication": None,
        }
    ]
    rows = (out / "inventory.csv").read_text(encoding="utf-8-sig")
    line = next(r for r in rows.splitlines() if r.startswith("Orders PDS,"))
    assert "Published Data Source" not in line
    assert "連線不依賴檢視者的身分" not in line
    # 第二次：資料源沒有更新時不重新下載
    FakeServer.calls = []
    assert tableau_server.main(["inventory", *auth(server), "--out", str(out), "--refresh"]) == 0
    assert not [c for c in FakeServer.calls if "/datasources/ds-1/content" in c]


def test_inventory_flags_prompted_published_datasource(
    server: str, tmp_path: Path, published: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live 的 Published Data Source 在伺服器上沒有嵌入密碼：檢視者用自己的資料庫帳號，不符合起步條件。"""
    from tmig.census.collect import IDENTITY_TITLE

    from ..parser import test_published as pds

    live = pds.write_tdsx(tmp_path / "live.tdsx", pds.tds(extract=False), None).read_bytes()
    monkeypatch.setitem(PUBLISHED, False, live)
    monkeypatch.setitem(
        DATASOURCE_CONNECTIONS, "ds-1", [{"id": "pc", "type": "oracle", "embedPassword": False}]
    )
    out = tmp_path / "inventory"
    assert tableau_server.main(["inventory", *auth(server), "--out", str(out)]) == 0
    with (out / "inventory.csv").open(encoding="utf-8-sig", newline="") as fh:
        row = next(r for r in csv.DictReader(fh) if r["名稱"] == "Orders PDS")
    assert row["起步狀態"] == "不符合起步條件"
    assert row["不符合的起步條件"] == IDENTITY_TITLE
    detail = (out / row["說明"]).read_text(encoding="utf-8")
    assert "「Orders (Published)」在 Tableau Server 上沒有嵌入密碼" in detail
    # 第二次（報表沒有更新、沿用上次的檔案）仍重新讀取資料源的連線設定
    FakeServer.calls = []
    assert tableau_server.main(["inventory", *auth(server), "--out", str(out)]) == 0
    assert [c for c in FakeServer.calls if "/datasources/ds-1/connections" in c]


def test_published_sources_in_merged_workbook(tmp_path: Path) -> None:
    """報表中的 Published Data Source：還沒併入的 sqlproxy 連線，或已併入、保留 repository-location 的資料源。"""
    from tmig.parser.published import merge_published

    from ..parser import test_published as pds

    report = pds.published_report(tmp_path, extract=False)
    assert tableau_server.published_sources(report) == [("Orders (Published)", "OrdersPDS")]
    merged = merge_published(report, tmp_path / "published", tmp_path / "merged.twbx").out
    assert tableau_server.published_sources(merged) == [("Orders (Published)", "OrdersPDS")]


# ---------------------------------------------------------------- 列層級權限（P1）


@pytest.fixture
def secure(monkeypatch: pytest.MonkeyPatch) -> None:
    """伺服器上多一份資料源篩選用到 USERNAME() 的報表。"""
    from ..parser import builder as b

    mine = b.calc("Mine", "Mine", "[Region] = USERNAME()", "boolean", "dimension")
    flt = (
        "<filter class='categorical' column='[Mine]'>"
        "<groupfilter function='member' level='[Mine]' member='true' /></filter>"
    )
    sheet = b.worksheet(
        "S", [b.instance("[Region]", "None", "[none:Region:nk]")], rows=b.ref("none:Region:nk")
    )
    xml = b.workbook(b.extract_datasource(columns=mine, extra=flt), worksheets=sheet)
    RLS_WORKBOOK[0] = xml.encode("utf-8")
    workbook = {
        "id": "wb-5",
        "name": "Secure",
        "contentUrl": "Secure",
        "updatedAt": "2026-10-03T00:00:00Z",
        "project": {"id": "p-1", "name": "Sales"},
        "owner": {"id": "u-1", "name": "amy"},
    }
    monkeypatch.setattr(sys.modules[__name__], "WORKBOOKS", [*WORKBOOKS, workbook])


def test_fetch_writes_user_for_rls(
    server: str, tmp_path: Path, secure: None, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fixtures" / "secure"
    args = ["fetch", *auth(server), "--workbook", "Secure", "--fixture", str(fixture)]
    assert tableau_server.main(args) == 0
    out = capsys.readouterr().out
    assert "cases.yaml 的 user 為 amy（3 個群組）" in out
    cases = load_cases(fixture / "cases.yaml")
    assert cases.user is not None
    assert cases.user.username == "amy"
    assert cases.user.full_name == "Amy Lin"
    assert cases.user.domain == "local"
    assert cases.user.groups == ("All Users", "Managers", "corp.example.com\\Sales")
    assert cases.user.member_of("corp.example.com\\sales")


def test_starter_cases_user_template() -> None:
    text = tableau_server.starter_cases("wb", "u", [], [], [], [], rls=True, user=None)
    assert "# user:" in text and "#   username: 帳號" in text
    assert load_cases_text(text).user is None
    plain = tableau_server.starter_cases("wb", "u", [], [], [], [])
    assert "user" not in yaml.safe_load(plain)


def load_cases_text(text: str) -> CasesFile:
    return CasesFile.model_validate(yaml.safe_load(text))


def test_directory_export(server: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tmig.access.directory import load_directory

    out = tmp_path / "directory.json"
    assert tableau_server.main(["directory", *auth(server), "--out", str(out)]) == 0
    assert "已匯出 3 個使用者、3 個群組" in capsys.readouterr().out
    directory = load_directory(out)
    assert directory.source is not None and "site：sales" in directory.source
    assert [u.name for u in directory.users] == ["amy", "bob", "Cat"]
    amy = directory.find("AMY")
    assert amy is not None
    assert amy.identity().groups == ("All Users", "Managers")
    assert amy.email == "amy@example.com" and amy.site_role == "Viewer"
    bob = directory.find("CORP\\bob")
    assert bob is not None and bob.domain == "CORP"
    assert bob.groups == ["All Users", "corp.example.com\\Sales"]
    assert directory.find("amy@example.com") is amy


def test_permissions_export(
    server: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tmig.access.permissions import load_permissions
    from tmig.planner.identity import Identity

    out = tmp_path / "permissions.json"
    assert tableau_server.main(["permissions", *auth(server), "--out", str(out)]) == 0
    assert "已匯出 3 個 Project、3 份報表" in capsys.readouterr().out
    permissions = load_permissions(out)
    sales = permissions.project("p-1")
    assert sales is not None and sales.owner == "amy"
    assert sales.content_permissions == "LockedToProject"
    assert sales.rules[0].grantee.name == "Managers"
    ops = permissions.project("p-3")
    assert ops is not None and ops.parent == "p-2" and ops.content_permissions == "ManagedByOwner"
    finance = permissions.workbook("Finance")
    assert finance is not None
    # 找不到的使用者（已刪除的帳號）略過；使用者帶網域
    assert [(r.grantee.kind, r.grantee.name, r.grantee.domain) for r in finance.rules] == [
        ("group", "corp.example.com\\Sales", None),
        ("user", "bob", "CORP"),
    ]
    opsv2 = permissions.workbook("Opsv2")
    assert opsv2 is not None and opsv2.show_tabs is False
    assert [(v.name, v.rules[0].grantee.name) for v in opsv2.views] == [("Overview", "Cat")]
    overview = permissions.workbook("SalesOverview")
    assert overview is not None and overview.show_tabs is True and overview.views == []

    cat = Identity(username="Cat", groups=("All Users",), site_role="Viewer")
    bob = Identity(
        username="bob",
        domain="CORP",
        groups=("All Users", "corp.example.com\\Sales"),
        site_role="Explorer",
    )
    assert permissions.access(cat, opsv2).views is None  # 擁有者看得到全部
    zed = Identity(username="zed", groups=("All Users",), site_role="Viewer")
    denied = permissions.access(zed, opsv2)  # 可以看報表，但沒有任何 view 的權限
    assert not denied.allowed and denied.views == frozenset()
    assert not permissions.access(bob, finance).allowed  # 本人的拒絕優先於群組的允許
    assert permissions.access(bob, overview).allowed


def test_schedules_export(server: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import datetime as dt

    from tmig.schedule.model import load_schedules

    out = tmp_path / "schedules.json"
    args = ["schedules", *auth(server), "--out", str(out), "--timezone", "Asia/Taipei"]
    assert tableau_server.main(args) == 0
    printed = capsys.readouterr().out
    assert "已匯出 4 個排程、3 個萃取更新工作、2 個訂閱" in printed
    assert "排程「壞掉的」不支援的排程頻率 'Yearly'" in printed
    data = load_schedules(out)
    assert data.timezone == "Asia/Taipei"
    daily = data.schedule("s-1")
    assert daily is not None and daily.frequency is not None
    assert daily.frequency.describe() == "每天 06:00"
    monday = data.schedule("s-2")
    assert monday is not None and monday.frequency is not None
    assert (
        monday.frequency.next_run(
            dt.datetime(2026, 11, 3, tzinfo=dt.UTC), data.timezone
        ).isoformat()
        == "2026-11-09T08:00:00+08:00"
    )
    broken = data.schedule("s-3")
    assert broken is not None and broken.frequency is None and broken.state == "Suspended"

    full, incremental, cloud = data.extract_refreshes
    assert (full.target_kind, full.content_url, full.refresh) == ("datasource", "OrdersPDS", "full")
    assert full.consecutive_failures == 2 and full.priority == 50
    assert (incremental.target_kind, incremental.content_url, incremental.refresh) == (
        "workbook",
        "SalesOverview",
        "incremental",
    )
    cloud_schedule = data.schedule(cloud.schedule)
    assert cloud_schedule is not None and cloud_schedule.frequency is not None
    assert cloud_schedule.frequency.describe() == "每天 00:00–24:00 每 4 小時"

    view_sub, workbook_sub = data.subscriptions
    assert view_sub.user == "amy" and view_sub.content_kind == "view"
    assert (view_sub.workbook, view_sub.view, view_sub.view_url_name) == (
        "SalesOverview",
        "Sales by Region",
        "SalesbyRegion",
    )
    assert view_sub.attach_pdf and view_sub.attach_image and not view_sub.send_if_view_empty
    assert (view_sub.page_orientation, view_sub.page_size) == ("Landscape", "A4")
    assert workbook_sub.user == "bob" and workbook_sub.user_domain == "CORP"
    assert workbook_sub.content_kind == "workbook" and workbook_sub.workbook == "SalesOverview"
    assert workbook_sub.view is None and workbook_sub.attach_image and not workbook_sub.attach_pdf


def test_schedules_requires_known_timezone(
    server: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "schedules",
        *auth(server),
        "--out",
        str(tmp_path / "s.json"),
        "--timezone",
        "Mars/Base",
    ]
    assert tableau_server.main(args) == 1
    assert "不認得的時區" in capsys.readouterr().err
    assert tableau_server.main(["schedules", *auth(server), "--out", str(tmp_path / "s.json")]) == 0
    assert "請以 --timezone 指定" in capsys.readouterr().out
