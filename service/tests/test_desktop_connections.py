"""桌面版的「連線到伺服器」：在 PostgreSQL 上建立連線（密碼不寫進設定檔，記住時存在認證管理員）、列出
資料表、以資料表或自訂 SQL 建立報表與資料源、加入關聯、修改自訂 SQL，重新啟動後讀回記住的密碼。
需要 PostgreSQL 的執行檔（或 TMIG_TEST_POSTGRES）；沒有時略過。"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tmig_service.credentials import MemoryCredentials
from tmig_service.desktop import TOKEN_HEADER, create_desktop_app
from tmig_service.desktop_connections import SECRET_TARGET, secret_env

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kernel"))
from tests.engines import pg_connect, postgres_cluster  # noqa: E402  # pyright: ignore[reportMissingImports]

TOKEN = "desktop-connections-token-0123456789"


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    with postgres_cluster() as found:
        con = pg_connect(found)
        con.execute("DROP SCHEMA IF EXISTS crm CASCADE")
        con.execute("CREATE SCHEMA crm")
        con.execute(
            "CREATE TABLE crm.orders (order_id TEXT, region TEXT, order_date DATE, "
            "sales NUMERIC(10, 2), quantity INTEGER)"
        )
        con.execute(
            "INSERT INTO crm.orders VALUES ('O1', 'East', '2024-01-05', 100.5, 2), "
            "('O2', 'West', '2024-02-01', 200, 1), ('O3', 'East', '2024-03-09', 50.25, 3), "
            "('O4', 'South', '2024-04-02', 75, 1)"
        )
        con.execute("CREATE TABLE crm.people (region TEXT, manager TEXT)")
        con.execute(
            "INSERT INTO crm.people VALUES ('East', 'Ann'), ('West', 'Bob'), ('North', 'Cid')"
        )
        con.close()
        yield found


def settings(dsn: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in dsn.split())


def client_for(tmp_path: Path, credentials: MemoryCredentials) -> TestClient:
    app = create_desktop_app(
        tmp_path / "workspace",
        TOKEN,
        None,
        username="amy",
        settings_dir=tmp_path / "settings",
        credentials=credentials,
    )
    return TestClient(app, headers={TOKEN_HEADER: TOKEN}, base_url="http://127.0.0.1")


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def ids(client: TestClient, workbook: str, datasource: str) -> dict[str, str]:
    model = ok(client.get(f"/api/edit/{workbook}/datasources/{datasource}/model"))
    return {f["caption"]: f["id"] for f in model["fields"]}


def render(client: TestClient, workbook: str, datasource: str, rows: str, measure: str) -> Any:
    sheet = ok(client.get(f"/api/edit/{workbook}/worksheets/ws_1"))["worksheet"]
    sheet.update(
        datasource=datasource, rows=[{"field": rows}], columns=[{"field": measure, "agg": "sum"}]
    )
    ok(client.put(f"/api/edit/{workbook}/worksheets/ws_1", json={"worksheet": sheet}))
    body = ok(client.post(f"/api/workbooks/{workbook}/worksheets/ws_1/render", json={}))
    return {row[0]: row[1] for row in body["table"]["rows"]}


def test_connect_create_and_restore(dsn: str, tmp_path: Path) -> None:
    params = settings(dsn)
    credentials = MemoryCredentials()
    client = client_for(tmp_path, credentials)
    connectors = ok(client.get("/api/desktop/connectors"))["connectors"]
    assert any(c["dbclass"] == "postgres" and c["port"] == 5432 for c in connectors)
    secret = "s3cret-not-in-files"
    created = ok(
        client.post(
            "/api/desktop/connections",
            json={
                "dbclass": "postgres",
                "server": params["host"],
                "port": int(params["port"]),
                "database": params["dbname"],
                "schema": "crm",
                "user": params["user"],
                "password": secret,
                "remember": True,
            },
        )
    )
    key = created["connection"]["id"]
    assert created["connection"]["signed_in"] is True
    assert {(t["schema"], t["name"]) for t in created["tables"]} == {
        ("crm", "orders"),
        ("crm", "people"),
    }
    # 密碼不在設定檔中：設定檔以 ${環境變數} 參照，記住的密碼在認證管理員
    text = (tmp_path / "settings" / "connections.toml").read_text(encoding="utf-8")
    assert secret not in text and "${" + secret_env(key) + "}" in text
    assert credentials.get(SECRET_TARGET + key) == secret
    columns = ok(
        client.post(f"/api/desktop/connections/{key}/columns", json={"table": ["crm", "orders"]})
    )["columns"]
    assert [(c["name"], c["datatype"]) for c in columns] == [
        ("order_id", "string"),
        ("region", "string"),
        ("order_date", "date"),
        ("sales", "real"),
        ("quantity", "integer"),
    ]
    # 以資料表建立報表；資料留在 PostgreSQL（Live 連線）
    info = ok(
        client.post(
            "/api/desktop/new-live", json={"connection": key, "table_path": ["crm", "orders"]}
        )
    )
    workbook, ds = info["document"]["id"], info["datasource"]
    f = ids(client, workbook, ds)
    assert render(client, workbook, ds, f["region"], f["sales"]) == {
        "East": "150.75",
        "South": "75",
        "West": "200",
    }
    model = ok(client.get(f"/api/edit/{workbook}/datasources/{ds}/model"))
    assert model["editable"] is True and model["connection"]["connection_id"] == key
    remote = ok(client.get(f"/api/edit/{workbook}/datasources/{ds}/remote-tables"))["tables"]
    assert [t["name"] for t in remote] == ["orders", "people"]
    # 關聯：people
    ok(
        client.post(
            f"/api/edit/{workbook}/datasources/{ds}/tables",
            json={
                "table_path": ["crm", "people"],
                "how": "relationship",
                "on": [[f["region"], "=", "region"]],
            },
        )
    )
    f = ids(client, workbook, ds)
    by_manager = render(client, workbook, ds, f["manager"], f["sales"])
    assert by_manager == {"Ann": "150.75", "Bob": "200", "Cid": "Null", "Null": "75"}
    # 自訂 SQL 的資料源，再修改 SQL
    sql = "SELECT region, SUM(sales) AS total FROM crm.orders GROUP BY region"
    custom = ok(
        client.post(f"/api/edit/{workbook}/datasources", json={"connection": key, "sql": sql})
    )
    cds = custom["datasource"]
    f = ids(client, workbook, cds)
    assert render(client, workbook, cds, f["region"], f["total"])["East"] == "150.75"
    edited = sql.replace("AS total", "AS total, COUNT(*) AS n")
    ok(
        client.put(
            f"/api/edit/{workbook}/datasources/{cds}/custom-sql/Custom SQL Query",
            json={"sql": edited},
        )
    )
    f = ids(client, workbook, cds)
    assert render(client, workbook, cds, f["region"], f["n"]) == {
        "East": "2",
        "South": "1",
        "West": "1",
    }
    bad = client.put(
        f"/api/edit/{workbook}/datasources/{cds}/custom-sql/Custom SQL Query",
        json={"sql": "SELECT nope FROM crm.missing"},
    )
    assert bad.status_code == 422
    # 重新啟動：記憶體中的密碼不在了，從認證管理員讀回
    os.environ.pop(secret_env(key), None)
    restarted = client_for(tmp_path, credentials)
    listed = ok(restarted.get("/api/desktop/connections"))["connections"]
    assert [(c["id"], c["signed_in"]) for c in listed] == [(key, True)]
    tables = ok(restarted.get(f"/api/desktop/connections/{key}/tables"))["tables"]
    assert len(tables) == 2
    # 沒有記住的密碼：要登入
    os.environ.pop(secret_env(key), None)
    credentials.delete(SECRET_TARGET + key)
    fresh = client_for(tmp_path, credentials)
    assert ok(fresh.get("/api/desktop/connections"))["connections"][0]["signed_in"] is False
    failed = fresh.get(f"/api/desktop/connections/{key}/tables")
    assert failed.status_code == 503 and secret not in failed.text
    ok(fresh.post(f"/api/desktop/connections/{key}/sign-in", json={"password": secret}))
    assert len(ok(fresh.get(f"/api/desktop/connections/{key}/tables"))["tables"]) == 2
    ok(fresh.delete(f"/api/desktop/connections/{key}"))
    assert ok(fresh.get("/api/desktop/connections"))["connections"] == []
