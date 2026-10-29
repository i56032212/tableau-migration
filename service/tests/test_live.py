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
