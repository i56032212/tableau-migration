"""使用紀錄（R1）：查詢服務記錄每次檢視的使用者、報表與時間；統計每份報表的使用量。"""

from __future__ import annotations

import datetime as dt
import json
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from conftest import DIRECTORY

from tmig.access.directory import Directory, DirectoryUser
from tmig.access.permissions import Permissions
from tmig.cli import main
from tmig_service import create_app
from tmig_service.auth import AuthSettings
from tmig_service.usage import UsageLog, Visit

ROOT = Path(__file__).resolve().parents[2]
WB = "/api/workbooks/secure"
BOARD = "db_ee909b0c"


def client(fixtures: Path, usage: Path | None, **auth: object) -> TestClient:
    settings = AuthSettings(**auth) if auth else AuthSettings()  # type: ignore[arg-type]
    return TestClient(create_app(fixtures, ROOT / "no-web-dist", auth=settings, usage_dir=usage))


def lines(usage: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for path in sorted(usage.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_visits_are_recorded_with_the_signed_in_user(fixtures: Path, tmp_path: Path) -> None:
    usage = tmp_path / "usage"
    c = client(fixtures, usage, mode="dev", directory=DIRECTORY)
    assert c.post(f"{WB}/views/{BOARD}/visit").status_code == 401  # 沒有登入
    c.post("/auth/dev", json={"username": "alice"})
    assert c.post(f"{WB}/views/{BOARD}/visit").status_code == 204
    assert c.post(f"{WB}/views/ws_monthly_profit/visit").status_code == 204
    c.post("/auth/dev", json={"username": "bob"})
    assert c.post(f"{WB}/views/{BOARD}/visit").status_code == 204
    assert c.post(f"{WB}/views/nope/visit").status_code == 404

    records = lines(usage)
    assert [(r["user"], r["view"], r["kind"]) for r in records] == [
        ("alice", "銷售總覽", "dashboard"),
        ("alice", "Monthly Profit", "worksheet"),
        ("bob", "銷售總覽", "dashboard"),
    ]
    assert set(records[0]) == {
        "time",
        "user",
        "workbook",
        "workbook_name",
        "view",
        "view_id",
        "kind",
    }

    summary = c.get("/api/usage").json()
    assert summary["recording"] is True
    (secure,) = summary["workbooks"]
    assert (secure["workbook"], secure["name"], secure["visits"], secure["users"]) == (
        "secure",
        "superstore_demo",
        3,
        2,
    )
    assert [(v["view"], v["visits"], v["users"]) for v in secure["views"]] == [
        ("銷售總覽", 2, 2),
        ("Monthly Profit", 1, 1),
    ]


def test_old_visits_and_unused_workbooks(fixtures: Path, tmp_path: Path) -> None:
    usage = tmp_path / "usage"
    log = UsageLog(usage)
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=200)
    log.record(Visit(old, "alice", "secure", "superstore_demo", "銷售總覽", BOARD, "dashboard"))
    c = client(fixtures, usage)
    (secure,) = c.get("/api/usage", params={"days": 90}).json()["workbooks"]
    assert secure["visits"] == 0 and secure["last"] is None  # 期間內沒有檢視：廢棄候選
    (secure,) = c.get("/api/usage", params={"days": 365}).json()["workbooks"]
    assert secure["visits"] == 1
    assert c.get("/api/usage", params={"days": 0}).status_code == 422
    # 沒有登入（none 模式）：使用者記為 null
    assert c.post(f"{WB}/views/{BOARD}/visit").status_code == 204
    assert lines(usage)[-1]["user"] is None


def test_permissions(fixtures: Path, tmp_path: Path) -> None:
    usage = tmp_path / "usage"
    directory = Directory(
        users=[
            DirectoryUser(name="alice", groups=["All Users"], site_role="Viewer"),
            DirectoryUser(name="carol", groups=["All Users"], site_role="Viewer"),
            DirectoryUser(name="root", groups=["All Users"], site_role="SiteAdministratorExplorer"),
        ]
    )
    granted = Permissions.model_validate(
        {
            "workbooks": [
                {
                    "id": "w1",
                    "name": "superstore_demo",
                    "rules": [
                        {
                            "grantee": {"kind": "user", "name": "alice"},
                            "capabilities": {"Read": "Allow"},
                        }
                    ],
                }
            ]
        }
    )
    c = client(fixtures, usage, mode="dev", directory=directory, permissions=granted)
    c.post("/auth/dev", json={"username": "carol"})
    assert c.post(f"{WB}/views/{BOARD}/visit").status_code == 404  # 沒有權限：不記錄
    assert c.get("/api/usage").status_code == 403
    c.post("/auth/dev", json={"username": "alice"})
    assert c.post(f"{WB}/views/{BOARD}/visit").status_code == 204
    assert c.get("/api/usage").status_code == 403  # 使用紀錄只有管理員可以看
    c.post("/auth/dev", json={"username": "root"})
    (secure,) = c.get("/api/usage").json()["workbooks"]
    assert secure["visits"] == 1 and secure["users"] == 1
    assert [r["user"] for r in lines(usage)] == ["alice"]


def test_recording_disabled(fixtures: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMIG_USAGE_DIR", raising=False)
    c = client(fixtures, None)
    assert c.post(f"{WB}/views/{BOARD}/visit").status_code == 204
    assert c.get("/api/usage").json()["recording"] is False


def test_cli_summary(fixtures: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    usage = tmp_path / "usage"
    log = UsageLog(usage)
    now = dt.datetime.now(dt.UTC)
    for user in ("alice", "bob", "alice"):
        log.record(Visit(now, user, "secure", "superstore_demo", "銷售總覽", BOARD, "dashboard"))
    other = tmp_path / "fixtures"
    other.mkdir()
    import shutil

    shutil.copytree(fixtures / "secure", other / "secure")
    shutil.copytree(fixtures / "secure", other / "unused")
    out = tmp_path / "usage.csv"
    assert main(["usage", "summary", str(usage), "--fixtures", str(other), "--csv", str(out)]) == 0
    text = capsys.readouterr().out
    assert "| superstore_demo（secure） | 3 | 2 |" in text
    assert "期間內沒有檢視的報表 1 份：superstore_demo" in text
    rows = out.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("報表,報表名稱,view,檢視次數")
    assert any(r.startswith("secure,superstore_demo,銷售總覽,3,2,") for r in rows)
