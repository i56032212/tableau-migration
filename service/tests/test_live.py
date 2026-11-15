"""第三階段 N3、N4：查詢服務以 Live 連線查詢（DuckDB 檔模擬來源資料庫，不含真實憑證）。"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig
from tmig.ir import LiveConnection, TableRelation, dumps_ir, load_ir
from tmig.verify.run import ensure_data
from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "fixtures" / "superstore_features"
DATABASE = FEATURES / "data" / "ds_orders.duckdb"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一份 Live 連線的報表：功能報表的資料源改成連到萃取的 DuckDB 檔。"""
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    connection = LiveConnection(
        kind="live",
        connection_id="sales_duckdb",
        dbclass="oracle",
        dialect="oracle",
        relation=TableRelation(kind="table", name="Orders", table=["Extract", "Extract"]),
    )
    live = ir.model_copy(
        update={
            "datasources": [
                ds.model_copy(update={"connection": connection}) for ds in ir.datasources
            ]
        }
    )
    root = tmp_path_factory.mktemp("fixtures")
    directory = root / "live"
    directory.mkdir()
    (directory / "golden.ir.json").write_text(dumps_ir(live), encoding="utf-8")
    shutil.copy(FEATURES / "cases.yaml", directory / "cases.yaml")
    return root


def runtime() -> DataRuntime:
    config = ConnectionConfig("sales_duckdb", "duckdb", {"driver": "duckdb", "path": str(DATABASE)})
    return DataRuntime({"sales_duckdb": config})


def test_render_live_worksheet(fixtures: Path) -> None:
    live = TestClient(create_app(fixtures, ROOT / "no-web-dist", runtime()))
    extract = TestClient(create_app(ROOT / "fixtures", ROOT / "no-web-dist"))
    for worksheet in ("ws_sales_by_segment", "ws_category_mix", "ws_monthly_moving_average"):
        a = live.post(f"/api/workbooks/live/worksheets/{worksheet}/render").json()
        b = extract.post(f"/api/workbooks/superstore_features/worksheets/{worksheet}/render").json()
        assert a["rows"] == b["rows"] > 0
        assert a["table"]["rows"] == b["table"]["rows"]
    values = live.post("/api/workbooks/live/fields/f_region/domain").json()
    assert values == {"kind": "values", "values": ["Central", "East", "South", "West"]}


def test_render_without_connection_settings(fixtures: Path) -> None:
    client = TestClient(create_app(fixtures, ROOT / "no-web-dist", DataRuntime({})))
    response = client.post("/api/workbooks/live/worksheets/ws_sales_by_segment/render")
    assert response.status_code == 503
    assert "連線設定檔中沒有連線 sales_duckdb" in response.json()["detail"]
    response = client.post("/api/workbooks/live/fields/f_region/domain")
    assert response.status_code == 503


def test_initial_sql_follows_the_signed_in_user(tmp_path: Path) -> None:
    """連線的 Initial SQL（Tableau 的「初始 SQL」）用到 [TableauServerUser]：連上來源時以登入者的帳號執行，
    每個登入者各自一條連線，看到的是自己那條連線上的結果。"""
    from conftest import DIRECTORY

    from tmig_service.auth import AuthSettings

    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    # Initial SQL 依登入者建立這條連線自己的檢視表；報表的資料表就是它
    connection = LiveConnection(
        kind="live",
        connection_id="sales_duckdb",
        dbclass="oracle",
        dialect="oracle",
        initial_sql=(
            'CREATE TEMP VIEW my_orders AS SELECT * FROM "Extract"."Extract" WHERE "Region" = '
            "CASE '[TableauServerUser]' WHEN 'alice' THEN 'East' WHEN 'bob' THEN 'West' END;\n"
            "CREATE TEMP TABLE who AS SELECT '[TableauServerUserFull]' AS me, "
            "'[TableauApp]' AS app, '[WorkbookName]' AS workbook"
        ),
        relation=TableRelation(kind="table", name="Orders", table=["my_orders"]),
    )
    live = ir.model_copy(
        update={
            "datasources": [
                ds.model_copy(update={"connection": connection}) for ds in ir.datasources
            ]
        }
    )
    directory = tmp_path / "personal"
    directory.mkdir()
    (directory / "golden.ir.json").write_text(dumps_ir(live), encoding="utf-8")
    data = runtime()
    app = create_app(
        tmp_path, ROOT / "no-web-dist", data, auth=AuthSettings(mode="dev", directory=DIRECTORY)
    )
    alice, bob = TestClient(app), TestClient(app)
    alice.post("/auth/dev", json={"username": "alice"})
    bob.post("/auth/dev", json={"username": "bob"})
    domain = "/api/workbooks/personal/fields/f_region/domain"
    assert alice.post(domain).json()["values"] == ["East"]
    assert bob.post(domain).json()["values"] == ["West"]
    assert alice.post(domain).json()["values"] == ["East"]  # 換回來仍是自己的連線
    sheet = "/api/workbooks/personal/worksheets/ws_sales_by_segment/render"
    mine, theirs = alice.post(sheet).json(), bob.post(sheet).json()
    assert mine["rows"] > 0 and theirs["rows"] > 0
    assert mine["table"]["rows"] != theirs["table"]["rows"]
    # 兩個登入者各自一條連線；其餘的參數：帶網域的帳號、應用程式與報表的名稱
    sessions = [key for key in data._executors if key.startswith("sales_duckdb")]  # pyright: ignore[reportPrivateUsage]
    assert len(sessions) == 2
    found = {
        tuple(row)
        for executor in data._executors.values()  # pyright: ignore[reportPrivateUsage]
        for row in executor.fetch("SELECT me, app, workbook FROM who").to_pylist()
        for row in [row.values()]
    }
    name = ir.workbook.name
    assert found == {
        ("local\\alice", "Tableau Server", name),
        ("local\\bob", "Tableau Server", name),
    }
