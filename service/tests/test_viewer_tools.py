"""檢視者的工具：下載交叉資料表（Excel、CSV）、摘要與完整資料、自訂檢視、我的最愛、最近檢視與留言。"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import shutil
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tmig.access.directory import Directory
from tmig.access.permissions import Permissions
from tmig.ir import load_ir
from tmig_service.app import create_app
from tmig_service.auth import AuthSettings
from tmig_service.downloads import excel_value, summary_value
from tmig_service.store import ReportStore
from tmig_service.usage import UsageLog, Visit

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
SHEET = "ws_sales_by_region"
IR = load_ir(DEMO / "golden.ir.json")
BOARD = IR.dashboards[0].id


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fixtures")
    target = root / "superstore_demo"
    target.mkdir()
    for name in ("golden.ir.json", "cases.yaml"):
        shutil.copy(DEMO / name, target / name)
    shutil.copytree(DEMO / "data", target / "data")
    return root


DIRECTORY = {
    "users": [
        {"name": "amy", "site_role": "SiteAdministratorCreator", "groups": ["All Users"]},
        {"name": "bob", "site_role": "Viewer", "groups": ["All Users"]},
        {"name": "carol", "site_role": "Explorer", "groups": ["All Users", "Analysts"]},
    ]
}
PERMISSIONS = {
    "workbooks": [
        {
            "id": "w1",
            "name": "superstore_demo",
            "owner": "amy",
            "rules": [
                {
                    "grantee": {"kind": "group", "name": "All Users"},
                    "capabilities": {
                        "Read": "Allow",
                        "ViewComments": "Allow",
                        "ExportImage": "Allow",
                    },
                },
                {
                    "grantee": {"kind": "group", "name": "Analysts"},
                    "capabilities": {
                        "ExportData": "Allow",
                        "ViewUnderlyingData": "Allow",
                        "ShareView": "Allow",
                        "AddComment": "Allow",
                    },
                },
            ],
        }
    ]
}


def make_app(fixtures: Path, tmp_path: Path, usage: Path | None = None) -> FastAPI:
    settings = AuthSettings(
        mode="dev",
        directory=Directory.model_validate(DIRECTORY),
        permissions=Permissions.model_validate(PERMISSIONS),
    )
    return create_app(
        fixtures,
        tmp_path / "no-web",
        auth=settings,
        store=ReportStore(tmp_path / "store"),
        usage_dir=usage,
    )


def login(app: FastAPI, user: str) -> TestClient:
    client = TestClient(app)
    assert client.post("/auth/dev", json={"username": user}).status_code == 200
    return client


def test_excel_values() -> None:
    assert excel_value("1,234") == (1234, "#,##0")
    assert excel_value("-1,234.50") == (-1234.5, "#,##0.00")
    assert excel_value("(1,234)") == (-1234, "#,##0;(#,##0)")
    assert excel_value("12.5%") == (0.125, "0.0%")
    assert excel_value("$1,000") == (1000, '"$"#,##0')
    assert excel_value("1.2K") == ("1.2K", None)
    assert excel_value("2024-01-01") == ("2024-01-01", None)
    assert excel_value("East") == ("East", None)


def test_downloads(fixtures: Path, tmp_path: Path) -> None:
    app = make_app(fixtures, tmp_path)
    carol = login(app, "carol")
    access = carol.get("/api/workbooks/superstore_demo/access").json()
    assert (
        access["export_data"]
        and access["underlying_data"]
        and access["share_view"]
        and access["store"]
    )
    rendered = carol.post(
        f"/api/workbooks/superstore_demo/worksheets/{SHEET}/render", json={}
    ).json()
    table = rendered["table"]

    # 交叉資料表（Excel）：與檢視器的資料表相同，數字存成數字並套用格式
    response = carol.post(
        f"/api/workbooks/superstore_demo/worksheets/{SHEET}/download",
        json={"kind": "crosstab", "format": "xlsx", "state": {}},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "Sales%20by%20Region.xlsx" in response.headers["content-disposition"]
    sheet = load_workbook(io.BytesIO(response.content)).active
    assert sheet is not None
    rows = list(sheet.iter_rows(values_only=True))
    assert list(rows[0]) == table["headers"]
    assert len(rows) == len(table["rows"]) + 1
    numeric = [i for i, cell in enumerate(table["rows"][0]) if excel_value(cell)[1] is not None]
    assert numeric and all(isinstance(rows[1][i], int | float) for i in numeric)

    # 交叉資料表（CSV）：UTF-8 含 BOM，值與畫面相同
    text = carol.post(
        f"/api/workbooks/superstore_demo/worksheets/{SHEET}/download",
        json={"kind": "crosstab", "format": "csv", "state": {}},
    ).content.decode("utf-8")
    assert text.startswith("﻿")
    parsed = list(csv.reader(io.StringIO(text[1:])))
    assert parsed[0] == table["headers"] and parsed[1:] == table["rows"]

    # 摘要資料：未格式化的值
    summary = carol.post(
        f"/api/workbooks/superstore_demo/worksheets/{SHEET}/download",
        json={"kind": "summary", "format": "csv", "state": {}},
    ).content.decode("utf-8")
    # 兩次查詢的浮點數加總順序可能不同（DuckDB 平行彙總）：數字以相對誤差比較
    got = list(csv.reader(io.StringIO(summary[1:])))[1:]
    assert len(got) == len(table["raw"])
    for row, want in zip(got, table["raw"], strict=True):
        for cell, expected_cell in zip(row, want, strict=True):
            try:
                assert math.isclose(float(cell), float(expected_cell), rel_tol=1e-9)
            except ValueError:
                assert cell == expected_cell

    # 完整資料：資料源篩選、維度篩選與 Top N 之後的基礎資料列
    with duckdb.connect(
        str(fixtures / "superstore_demo" / "data" / "ds_orders.duckdb"), read_only=True
    ) as con:
        expected = con.execute(
            """SELECT count(*) FROM "Extract"."Extract" WHERE "Country/Region" = 'United States'
               AND "Category" IN ('Furniture', 'Technology') AND "Region" IN (
                 SELECT "Region" FROM "Extract"."Extract" WHERE "Country/Region" = 'United States'
                 GROUP BY 1 ORDER BY sum("Sales") DESC LIMIT 3)"""
        ).fetchone()
    assert expected is not None
    preview = carol.post(
        f"/api/workbooks/superstore_demo/worksheets/{SHEET}/underlying",
        json={"state": {}, "limit": 50},
    ).json()
    assert len(preview["rows"]) == 50 and preview["truncated"] is True
    assert "Region" in preview["columns"] and not any(
        c.startswith("__") for c in preview["columns"]
    )
    full = carol.post(
        f"/api/workbooks/superstore_demo/worksheets/{SHEET}/download",
        json={"kind": "underlying", "format": "csv", "state": {}},
    ).content.decode("utf-8")
    assert len(list(csv.reader(io.StringIO(full[1:])))) - 1 == expected[0]
    # 篩選改變時完整資料跟著改變
    east = carol.post(
        f"/api/workbooks/superstore_demo/worksheets/{SHEET}/underlying",
        json={
            "state": {"filters": {"f_category": {"mode": "include", "values": ["Furniture"]}}},
            "limit": 100000,
        },
    ).json()
    assert 0 < len(east["rows"]) < expected[0]

    # 沒有「下載摘要資料」的權限：不能下載交叉資料表；沒有「檢視基礎資料」：不能看完整資料
    bob = login(app, "bob")
    denied = bob.get("/api/workbooks/superstore_demo/access").json()
    assert denied["export_data"] is False and denied["underlying_data"] is False and denied["store"]
    assert (
        bob.post(
            f"/api/workbooks/superstore_demo/worksheets/{SHEET}/download",
            json={"kind": "crosstab", "format": "csv", "state": {}},
        ).status_code
        == 403
    )
    assert (
        bob.post(
            f"/api/workbooks/superstore_demo/worksheets/{SHEET}/underlying", json={"state": {}}
        ).status_code
        == 403
    )


def test_custom_views(fixtures: Path, tmp_path: Path) -> None:
    app = make_app(fixtures, tmp_path)
    bob = login(app, "bob")
    carol = login(app, "carol")
    base = f"/api/workbooks/superstore_demo/views/{BOARD}/custom-views"
    state = {
        "sheetFilters": {SHEET: {"f_category": {"values": ["Furniture"]}}},
        "parameters": {},
        "actions": {},
        "selections": {},
    }
    # 沒有「分享自訂檢視」的權限：只能存自己的
    assert bob.post(base, json={"name": "家具", "state": state, "public": True}).status_code == 403
    mine = bob.post(base, json={"name": "家具", "state": state, "make_default": True})
    assert mine.status_code == 201, mine.text
    listed = bob.get(base).json()
    assert [v["name"] for v in listed["views"]] == ["家具"] and listed["default"] == mine.json()[
        "id"
    ]
    assert listed["can_share"] is False
    # 同名時取代
    again = bob.post(base, json={"name": "家具", "state": {**state, "parameters": {"p": 1}}}).json()
    assert again["id"] == mine.json()["id"] and again["state"]["parameters"] == {"p": 1}
    # 別人看不到私人的；公開的看得到
    assert carol.get(base).json()["views"] == []
    shared = carol.post(base, json={"name": "分析", "state": state, "public": True}).json()
    assert [v["name"] for v in bob.get(base).json()["views"]] == ["家具", "分析"]
    assert bob.patch(f"/api/custom-views/{shared['id']}", json={"name": "x"}).status_code == 403
    assert bob.delete(f"/api/custom-views/{shared['id']}").status_code == 403
    # 預設檢視可以是別人公開的
    assert (
        bob.put(
            f"/api/workbooks/superstore_demo/views/{BOARD}/default-view",
            json={"custom_view": shared["id"]},
        ).status_code
        == 200
    )
    assert bob.get(base).json()["default"] == shared["id"]
    assert carol.delete(f"/api/custom-views/{shared['id']}").status_code == 204
    assert bob.get(base).json()["default"] is None
    assert bob.get("/api/workbooks/superstore_demo/views/nope/custom-views").status_code == 404


def test_favorites_comments_and_recents(fixtures: Path, tmp_path: Path) -> None:
    usage = tmp_path / "usage"
    app = make_app(fixtures, tmp_path, usage)
    bob = login(app, "bob")
    carol = login(app, "carol")
    # 我的最愛
    added = bob.put("/api/favorites", json={"report": "superstore_demo", "view": BOARD}).json()
    assert [(f["report_name"], f["view_name"]) for f in added] == [
        ("superstore_demo", IR.dashboards[0].name)
    ]
    assert (
        bob.put("/api/favorites", json={"report": "superstore_demo", "view": "nope"}).status_code
        == 404
    )
    assert carol.get("/api/favorites").json() == []
    assert (
        bob.put(
            "/api/favorites", json={"report": "superstore_demo", "view": BOARD, "on": False}
        ).json()
        == []
    )

    # 留言：要「新增留言」才能留言，留言的人可以刪除
    comments = f"/api/workbooks/superstore_demo/views/{BOARD}/comments"
    assert bob.get(comments).json() == {"can_add": False, "comments": []}
    assert bob.post(comments, json={"text": "這個月的數字？"}).status_code == 403
    posted = carol.post(comments, json={"text": "華北的數字偏低"})
    assert posted.status_code == 201
    listed = bob.get(comments).json()["comments"]
    assert [(c["user"], c["text"], c["can_delete"]) for c in listed] == [
        ("carol", "華北的數字偏低", False)
    ]
    assert bob.delete(f"/api/comments/{posted.json()['id']}").status_code == 403
    amy = login(app, "amy")  # 報表的擁有者（也是管理員）可以刪除
    assert amy.get(comments).json()["comments"][0]["can_delete"] is True
    assert carol.delete(f"/api/comments/{posted.json()['id']}").status_code == 204

    # 最近檢視：依使用紀錄，最新的在前、同一個 view 只列一次
    log = UsageLog(usage)
    now = dt.datetime.now(dt.UTC)
    for minutes, view, name in (
        (30, SHEET, "Sales by Region"),
        (20, BOARD, IR.dashboards[0].name),
        (10, SHEET, "Sales by Region"),
    ):
        log.record(
            Visit(
                time=now - dt.timedelta(minutes=minutes),
                user="bob",
                workbook="superstore_demo",
                workbook_name="superstore_demo",
                view=name,
                view_id=view,
                kind="worksheet" if view == SHEET else "dashboard",
            )
        )
    recents = bob.get("/api/recents").json()
    assert [r["view"] for r in recents] == [SHEET, BOARD]
    assert carol.get("/api/recents").json() == []


def test_summary_values_have_no_binary_float_tail() -> None:
    """摘要資料的值：二進位浮點數去掉加總留下的尾數（稽核 C5：畫面上是 909,370，CSV 裡是
    909369.9110000005）——取相差不到一兆分之一的十進位數字中位數最少的；本來就有這麼多位數的值不變。
    Decimal 是精確的值，與整數、文字、日期一樣原樣寫出。"""
    assert summary_value(909369.9110000005) == "909369.911"
    assert summary_value(0.1 + 0.2) == "0.3"
    assert summary_value(-0.30000000000000004) == "-0.3"
    assert summary_value(2297200.0) == "2297200.0"
    assert summary_value(-1234.5) == "-1234.5"
    assert summary_value(123456789.1234) == "123456789.1234"  # 13 位有效數字都是真的
    assert summary_value(0.000123456789012) == "0.000123456789012"
    assert summary_value(1e21) == "1e+21" and summary_value(0.0) == "0.0"
    assert summary_value(Decimal("123456789012345.6789")) == "123456789012345.6789"
    assert summary_value(12345678901234567890) == "12345678901234567890"
    assert summary_value("909369.9110000005") == "909369.9110000005"  # 文字欄不動
    assert summary_value(None) == "" and summary_value(True) == "true"
    assert summary_value(dt.date(2024, 1, 31)) == "2024-01-31"
    assert summary_value(float("nan")) == "nan"


def test_summary_csv_has_no_float_tail(fixtures: Path, tmp_path: Path) -> None:
    """摘要資料的 CSV：量值與查詢的值相同（到 12 位有效數字），但沒有二進位浮點數的尾數；檢視器與計算比對
    用的 table.raw 仍是完整精度。"""
    carol = login(make_app(fixtures, tmp_path), "carol")
    base = f"/api/workbooks/superstore_demo/worksheets/{SHEET}"
    table = carol.post(f"{base}/render", json={}).json()["table"]
    text = carol.post(
        f"{base}/download", json={"kind": "summary", "format": "csv", "state": {}}
    ).content.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text[1:])))
    assert rows[0] == table["headers"] and len(rows) - 1 == len(table["raw"])
    measures = [i for i, role in enumerate(table["roles"]) if role == "measure"]
    assert measures
    for row, raw in zip(rows[1:], table["raw"], strict=True):
        for index, (cell, full) in enumerate(zip(row, raw, strict=True)):
            if index not in measures:
                assert cell == full
                continue
            # 與查詢的值相同（兩次查詢的加總順序可能不同，所以以去掉尾數之後的值比較）
            assert cell == summary_value(float(full))
            assert math.isclose(float(cell), float(full), rel_tol=1e-12)
            # 來源資料是小數 4 位以內的金額：總和的小數也在 4 位以內
            assert len(cell.partition(".")[2]) <= 4, cell
    # 檢視器與計算比對用的值不變：完整精度
    again = carol.post(f"{base}/render", json={}).json()["table"]
    assert all(
        cell == repr(float(cell)) for row in again["raw"] for cell in (row[m] for m in measures)
    )
