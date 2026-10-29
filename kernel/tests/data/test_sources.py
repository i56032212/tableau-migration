"""第三階段 N3、N4：連線設定檔、執行器、執行環境與 tmig connections。

不含真實憑證：Live 連線以 DuckDB 檔（driver = "duckdb"）模擬來源資料庫。
"""

from __future__ import annotations

import datetime as dt
import shutil
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from tmig.cli import main
from tmig.data.connection_check import check, collect, template
from tmig.data.executor import execute
from tmig.data.runtime import DataRuntime
from tmig.data.sources import (
    ConnectionConfig,
    SourceConnectionError,
    load_connections,
    normalize_arrow,
    rows_to_arrow,
)
from tmig.ir import IR, FieldUse, LiveConnection, TableRelation, dumps_ir, load_ir
from tmig.sqlgen.query import domain_sql, worksheet_sql
from tmig.verify.run import ensure_data, verify_fixture

ROOT = Path(__file__).resolve().parents[3]
FEATURES = ROOT / "fixtures" / "superstore_features"


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 設定檔


def test_config_expands_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(
        tmp_path / "connections.toml",
        """
[connections.sales]
driver = "oracle"
user = "${TMIG_TEST_USER}"
password = "${TMIG_TEST_PASSWORD}"
host = "ora-replica.example.com"
""",
    )
    monkeypatch.delenv("TMIG_TEST_USER", raising=False)
    monkeypatch.delenv("TMIG_TEST_PASSWORD", raising=False)
    sales = load_connections(config)["sales"]
    assert sales.driver == "oracle"
    assert sales.missing_env() == ["TMIG_TEST_PASSWORD", "TMIG_TEST_USER"]
    with pytest.raises(SourceConnectionError, match="TMIG_TEST_PASSWORD"):
        sales.resolved()
    monkeypatch.setenv("TMIG_TEST_USER", "reader")
    monkeypatch.setenv("TMIG_TEST_PASSWORD", "s3cret")
    resolved = sales.resolved()
    assert (resolved["user"], resolved["password"]) == ("reader", "s3cret")
    assert resolved["host"] == "ora-replica.example.com"
    # 設定檔的原文不含任何值
    assert "s3cret" not in repr(sales.raw)


def test_config_rejects_literal_secrets(tmp_path: Path) -> None:
    config = write_config(
        tmp_path / "connections.toml",
        '[connections.sales]\ndriver = "postgres"\npassword = "s3cret"\n',
    )
    with pytest.raises(SourceConnectionError, match="不能直接寫在設定檔"):
        load_connections(config)


def test_config_errors(tmp_path: Path) -> None:
    assert load_connections(tmp_path / "missing.toml") == {}
    config = write_config(tmp_path / "bad.toml", '[connections.x]\ndriver = "mssql"\n')
    with pytest.raises(SourceConnectionError, match="driver"):
        load_connections(config)


def test_config_path_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_config(
        tmp_path / "elsewhere.toml",
        f'[connections.local]\ndriver = "duckdb"\npath = "{tmp_path}"\n',
    )
    monkeypatch.setenv("TMIG_CONNECTIONS", str(config))
    assert list(load_connections()) == ["local"]


# ---------------------------------------------------------------- Arrow


def test_rows_to_arrow() -> None:
    table = rows_to_arrow(
        ["count", "avg", "when", "name"],
        [
            (
                Decimal("3"),
                Decimal("1.25"),
                dt.datetime(2024, 1, 1, 8, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                "a",
            ),
            (None, None, None, None),
        ],
        [0, None, None, None],
    )
    assert table.schema.types == [pa.int64(), pa.float64(), pa.timestamp("us"), pa.string()]
    assert table.column("when").to_pylist() == [dt.datetime(2024, 1, 1, 0, 0), None]


def test_normalize_arrow() -> None:
    table = pa.table(
        {
            "n": pa.array([Decimal("12")], pa.decimal128(10, 0)),
            "x": pa.array([Decimal("1.5")], pa.decimal128(10, 2)),
            "t": pa.array([dt.datetime(2024, 1, 1, 8)], pa.timestamp("us", tz="Asia/Taipei")),
        }
    )
    out = normalize_arrow(table)
    assert out.schema.types == [pa.int64(), pa.float64(), pa.timestamp("us")]
    assert out.column("x").to_pylist() == [1.5]


# ---------------------------------------------------------------- 執行環境


def live_features(connection_id: str = "sales_duckdb") -> IR:
    """功能報表的資料源改成 Live 連線到萃取的 DuckDB 檔（"Extract"."Extract"）。"""
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    connection = LiveConnection(
        kind="live",
        connection_id=connection_id,
        dbclass="oracle",
        dialect="oracle",
        server="ora.example.com",
        database="ORCLPDB",
        relation=TableRelation(kind="table", name="Orders", table=["Extract", "Extract"]),
    )
    datasources = [ds.model_copy(update={"connection": connection}) for ds in ir.datasources]
    return ir.model_copy(update={"datasources": datasources})


def duckdb_configs(connection_id: str = "sales_duckdb") -> dict[str, ConnectionConfig]:
    path = FEATURES / "data" / "ds_orders.duckdb"
    return {
        connection_id: ConnectionConfig(
            connection_id, "duckdb", {"driver": "duckdb", "path": str(path)}
        )
    }


def test_runtime_runs_live_queries() -> None:
    live = live_features()
    extract = load_ir(FEATURES / "golden.ir.json")
    database = FEATURES / "data" / "ds_orders.duckdb"
    with DataRuntime(duckdb_configs()) as runtime:
        for worksheet in ("ws_sales_by_segment", "ws_category_mix", "ws_top_sub_categories"):
            query = worksheet_sql(live, worksheet)
            assert query.live
            result = runtime.run(query, live.datasources[0], database)
            expected = execute(database, worksheet_sql(extract, worksheet).sql)
            assert result.columns == expected.columns
            assert len(result.rows) == len(expected.rows)
        sql = domain_sql(live, live.datasources[0].id, FieldUse(field="f_region"), "values")
        values = runtime.run_sql(sql, live.datasources[0], database)
        assert [r[0] for r in values.rows] == ["Central", "East", "South", "West"]


def test_runtime_without_configuration() -> None:
    live = live_features()
    query = worksheet_sql(live, "ws_sales_by_segment")
    with DataRuntime({}) as runtime, pytest.raises(SourceConnectionError, match="sales_duckdb"):
        runtime.run(query, live.datasources[0], Path("unused.duckdb"))


def test_verify_live_fixture(tmp_path: Path) -> None:
    """Live 連線的報表也能做計算比對（結果與萃取時相同）。"""
    fixture = tmp_path / "live"
    fixture.mkdir()
    for name in ("cases.yaml", "mapping.yaml"):
        shutil.copy(FEATURES / name, fixture / name)
    shutil.copytree(FEATURES / "expected", fixture / "expected")
    (fixture / "golden.ir.json").write_text(dumps_ir(live_features()), encoding="utf-8")
    _, report = verify_fixture(fixture, DataRuntime(duckdb_configs()))
    assert report.summary == {"pass": 100, "fail": 0, "unsupported": 0}
    _, report = verify_fixture(fixture, DataRuntime({}))
    assert report.summary["fail"] == 100
    assert "連線設定檔中沒有連線 sales_duckdb" in report.checks[0].diff


# ---------------------------------------------------------------- tmig connections


def connections_ir(tmp_path: Path) -> Path:
    """三個資料源：Oracle、Databricks 與尚無執行器的 SQL Server。"""
    base = live_features()
    ds = base.datasources[0]
    column = next(f for f in ds.fields if f.column is not None)
    variants = [
        LiveConnection(
            kind="live",
            connection_id="sales_ora",
            dbclass="oracle",
            dialect="oracle",
            server="ora.example.com",
            port=1521,
            database="ORCLPDB",
        ),
        LiveConnection(
            kind="live",
            connection_id="lake",
            dbclass="databricks",
            dialect="databricks",
            server="adb-1.azuredatabricks.net",
            database="main",
        ),
        LiveConnection(
            kind="live",
            connection_id="crm_mssql",
            dbclass="sqlserver",
            dialect="tsql",
            server="crm.example.com",
        ),
    ]
    datasources = [
        ds.model_copy(
            update={
                "id": f"ds_{i}",
                "name": f"federated.{i}",
                "connection": c,
                "fields": [column.model_copy(update={"id": f"f_col_{i}", "name": f"[col {i}]"})],
                "filters": [],
            }
        )
        for i, c in enumerate(variants)
    ]
    ir = base.model_copy(
        update={"datasources": datasources, "worksheets": [], "dashboards": [], "actions": []}
    )
    path = tmp_path / "sales.ir.json"
    path.write_text(dumps_ir(ir), encoding="utf-8")
    return path


def test_connections_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = connections_ir(tmp_path)
    assert main(["connections", "template", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert '[connections.sales_ora]\ndriver = "oracle"\nuser = "${TMIG_SALES_ORA_USER}"' in text
    assert 'password = "${TMIG_SALES_ORA_PASSWORD}"' in text
    assert '[connections.lake]\ndriver = "databricks"\naccess_token = "${TMIG_LAKE_TOKEN}"' in text
    assert "連線類型 sqlserver 尚無執行器" in text
    # 範本可以直接當成設定檔讀取（憑證都是環境變數）
    out = tmp_path / "connections.toml"
    assert main(["connections", "template", str(path), "--out", str(out)]) == 0
    assert set(load_connections(out)) == {"sales_ora", "lake"}


def test_connections_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connections_ir(tmp_path)
    database = FEATURES / "data" / "ds_orders.duckdb"
    config = write_config(
        tmp_path / "connections.toml",
        f"""
[connections.sales_ora]
driver = "duckdb"
path = "{database.as_posix()}"

[connections.lake]
driver = "databricks"
access_token = "${{TMIG_TEST_MISSING_TOKEN}}"
""",
    )
    assert main(["connections", "check", str(tmp_path), "--config", str(config)]) == 1
    out = capsys.readouterr().out
    assert "通過  sales_ora（oracle，ora.example.com／ORCLPDB）：連線成功" in out
    assert (
        "失敗  lake（databricks，adb-1.azuredatabricks.net／main）：環境變數未設定：TMIG_TEST_MISSING_TOKEN"
        in out
    )
    assert "失敗  crm_mssql（sqlserver，crm.example.com）：連線設定檔中沒有這個連線" in out
    assert "共 3 個連線：通過 1、失敗 2" in out


def test_connection_statuses_without_connecting(tmp_path: Path) -> None:
    found = collect([load_ir(connections_ir(tmp_path))])
    assert list(found) == ["crm_mssql", "lake", "sales_ora"]
    statuses = check(found, duckdb_configs("sales_ora"), connect=False)
    assert [s.ok for s in statuses] == [False, False, True]
    assert statuses[2].message == "設定齊全（未測試連線）"
    assert "[connections.sales_ora]" in template(found)
