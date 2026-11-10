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


def test_config_rejects_literal_aws_secrets(tmp_path: Path) -> None:
    config = write_config(
        tmp_path / "connections.toml",
        '[connections.lake]\ndriver = "athena"\nsecret_access_key = "abc"\n',
    )
    with pytest.raises(SourceConnectionError, match="secret_access_key 不能直接寫在設定檔"):
        load_connections(config)


def test_trino_executor_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """有密碼時以 HTTPS 的基本驗證連線；catalog、schema 取工作簿中的連線資訊；工作階段為 UTC。"""
    import trino

    from tmig.data.sources import TrinoExecutor
    from tmig.ir import LiveConnection

    calls: list[dict[str, object]] = []

    def connect(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(trino.dbapi, "connect", connect)
    connection = LiveConnection(
        kind="live",
        connection_id="presto_x",
        dbclass="presto",
        dialect="trino",
        server="trino.example.com",
        database="hive",
        schema_name="sales",
    )
    TrinoExecutor.open(connection, {"user": "reader"})
    TrinoExecutor.open(connection, {"user": "reader", "password": "pw", "catalog": "iceberg"})
    plain, secure = calls
    assert (plain["http_scheme"], plain["port"], plain["catalog"], plain["schema"]) == (
        "http",
        8080,
        "hive",
        "sales",
    )
    assert plain["timezone"] == "UTC" and "auth" not in plain
    assert (secure["http_scheme"], secure["port"], secure["catalog"]) == ("https", 443, "iceberg")
    assert isinstance(secure["auth"], trino.auth.BasicAuthentication)


def test_athena_executor_options() -> None:
    from tmig.data.sources import AthenaExecutor, _athena_region
    from tmig.ir import LiveConnection

    assert _athena_region("athena.ap-northeast-1.amazonaws.com") == "ap-northeast-1"
    assert _athena_region("example.com") is None
    connection = LiveConnection(
        kind="live",
        connection_id="athena_x",
        dbclass="athena",
        dialect="athena",
        server="athena.us-east-1.amazonaws.com",
    )
    with pytest.raises(SourceConnectionError, match="s3_staging_dir 或 work_group"):
        AthenaExecutor.open(connection, {})


def test_bigquery_executor_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """專案取工作簿的連線資訊、預設資料集讓自訂 SQL 可以省略專案；可限制每次查詢掃描的位元組。"""
    bigquery = pytest.importorskip("google.cloud.bigquery")

    from tmig.data.sources import BigQueryExecutor
    from tmig.ir import LiveConnection

    calls: list[dict[str, object]] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(bigquery, "Client", Client)
    connection = LiveConnection(
        kind="live",
        connection_id="bigquery_x",
        dbclass="bigquery",
        dialect="bigquery",
        database="sales-project",
        schema_name="sales",
    )
    executor = BigQueryExecutor.open(connection, {"maximum_bytes_billed": "1000"})
    assert calls[0] == {"project": "sales-project"}
    assert executor.job_config.maximum_bytes_billed == 1000
    assert executor.job_config.default_dataset.dataset_id == "sales"
    executor = BigQueryExecutor.open(
        connection, {"project": "billing", "endpoint": "http://x:9050"}
    )
    assert calls[1]["project"] == "billing" and "client_options" in calls[1]
    assert executor.job_config.default_dataset.project == "billing"
    with pytest.raises(SourceConnectionError, match="缺少專案"):
        BigQueryExecutor.open(connection.model_copy(update={"database": None}), {})


def test_hiveserver2_executor_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """HiveServer2 協定：有密碼時為 LDAP、沒有時為 PLAIN；工作階段設為 UTC（Hive 2 等沒有這個設定時略過）。"""
    import impala.dbapi

    from tmig.data.sources import HiveServer2Executor
    from tmig.ir import LiveConnection

    calls: list[dict[str, object]] = []
    statements: list[str] = []
    refuse: set[str] = set()

    class Cursor:
        def execute(self, sql: str) -> None:
            statements.append(sql)
            if sql in refuse:
                raise RuntimeError("hive configuration hive.local.time.zone does not exists")

        def close(self) -> None:
            pass

    class Connection:
        closed = False

        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            Connection.closed = True

    def connect(**kwargs: object) -> Connection:
        calls.append(kwargs)
        return Connection()

    monkeypatch.setattr(impala.dbapi, "connect", connect)

    def live(dbclass: str, dialect: str, port: int | None = None) -> LiveConnection:
        return LiveConnection(
            kind="live",
            connection_id=f"{dbclass}_x",
            dbclass=dbclass,
            dialect=dialect,  # type: ignore[arg-type]
            server="hadoop.example.com",
            port=port,
            schema_name="sales",
        )

    hive = HiveServer2Executor.open(
        live("hadoophive", "hive"), {"user": "u", "password": "p"}, "hive"
    )
    assert hive.dialect == "hive"
    assert calls[0] == {
        "host": "hadoop.example.com",
        "port": 10000,
        "database": "sales",
        "auth_mechanism": "LDAP",
        "user": "u",
        "password": "p",
        "use_ssl": False,
    }
    impala_executor = HiveServer2Executor.open(live("hadoophive", "impala"), {}, "impala")
    assert impala_executor.dialect == "impala"
    assert (calls[1]["port"], calls[1]["auth_mechanism"], calls[1]["password"]) == (
        21050,
        "PLAIN",
        "tmig",
    )
    HiveServer2Executor.open(live("spark", "spark", 10001), {"http_path": "cliservice"}, "spark")
    assert (calls[2]["port"], calls[2]["use_http_transport"], calls[2]["http_path"]) == (
        10001,
        True,
        "cliservice",
    )
    assert statements == [
        "SET hive.local.time.zone=UTC",
        "SET TIMEZONE=UTC",
        "SET spark.sql.session.timeZone=UTC",
    ]
    # Hive 2 沒有 hive.local.time.zone：照樣連線；Spark 設定失敗時不連線
    refuse.update({"SET hive.local.time.zone=UTC", "SET spark.sql.session.timeZone=UTC"})
    assert HiveServer2Executor.open(live("hadoophive", "hive"), {}, "hive").dialect == "hive"
    with pytest.raises(SourceConnectionError, match="無法設定 spark"):
        HiveServer2Executor.open(live("spark", "spark"), {}, "spark")
    assert Connection.closed
    with pytest.raises(SourceConnectionError, match="缺少主機"):
        HiveServer2Executor.open(
            live("spark", "spark").model_copy(update={"server": None}), {}, "spark"
        )


def test_db2_and_hana_executor_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """Db2：連線字串不含帳密（另外傳給驅動）、可加 SSL；HANA：連接埠預設 30015、HANA Cloud 預設加密。"""
    import sys
    import types

    from tmig.data.sources import Db2Executor, HanaExecutor
    from tmig.ir import LiveConnection

    calls: list[tuple[object, ...]] = []
    db2 = types.ModuleType("ibm_db_dbi")
    db2.connect = lambda *args: calls.append(args) or object()  # type: ignore[attr-defined]
    hdbcli = types.ModuleType("hdbcli")
    dbapi = types.ModuleType("hdbcli.dbapi")
    dbapi.connect = lambda **kwargs: calls.append((kwargs,)) or object()  # type: ignore[attr-defined]
    hdbcli.dbapi = dbapi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ibm_db_dbi", db2)
    monkeypatch.setitem(sys.modules, "hdbcli", hdbcli)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", dbapi)

    def live(dbclass: str, dialect: str, server: str, database: str | None) -> LiveConnection:
        return LiveConnection(
            kind="live",
            connection_id=f"{dbclass}_x",
            dbclass=dbclass,
            dialect=dialect,  # type: ignore[arg-type]
            server=server,
            database=database,
        )

    connection = live("db2", "db2", "db2.example.com", "SAMPLE")
    Db2Executor.open(connection, {"user": "u", "password": "p;w", "ssl": "true"})
    assert calls[0] == (
        "DATABASE=SAMPLE;HOSTNAME=db2.example.com;PORT=50000;PROTOCOL=TCPIP;SECURITY=SSL;",
        "u",
        "p;w",
    )
    with pytest.raises(SourceConnectionError, match="缺少主機或資料庫"):
        Db2Executor.open(connection.model_copy(update={"database": None}), {})
    HanaExecutor.open(
        live("saphana", "hana", "hana.example.com", None), {"user": "u", "database": "HXE"}
    )
    assert calls[1] == (
        {
            "address": "hana.example.com",
            "port": 30015,
            "user": "u",
            "encrypt": False,
            "sslValidateCertificate": True,
            "databaseName": "HXE",
        },
    )
    HanaExecutor.open(live("saphana", "hana", "x.hanacloud.ondemand.com", None), {"port": 443})
    assert calls[2][0]["encrypt"] is True and calls[2][0]["port"] == 443  # type: ignore[index]


def test_config_errors(tmp_path: Path) -> None:
    assert load_connections(tmp_path / "missing.toml") == {}
    config = write_config(tmp_path / "bad.toml", '[connections.x]\ndriver = "mssql"\n')
    with pytest.raises(SourceConnectionError, match="driver"):
        load_connections(config)


def test_config_path_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_config(
        tmp_path / "elsewhere.toml",
        f'[connections.local]\ndriver = "duckdb"\npath = "{tmp_path.as_posix()}"\n',
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
    assert report.summary == {"pass": 124, "fail": 0, "unsupported": 0}
    _, report = verify_fixture(fixture, DataRuntime({}))
    assert report.summary["fail"] == 124
    assert "連線設定檔中沒有連線 sales_duckdb" in report.checks[0].diff


# ---------------------------------------------------------------- tmig connections


def connections_ir(tmp_path: Path) -> Path:
    """三個資料源：Oracle、Databricks 與尚無執行器的 Teradata。"""
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
            connection_id="crm_td",
            dbclass="teradata",
            dialect="teradata",
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
        update={
            "datasources": datasources,
            "worksheets": [],
            "dashboards": [],
            "actions": [],
            "stories": [],
        }
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
    assert '[connections.crm_td]\ndriver = "teradata"\nuser = "${TMIG_CRM_TD_USER}"' in text
    # 範本可以直接當成設定檔讀取（憑證都是環境變數）
    out = tmp_path / "connections.toml"
    assert main(["connections", "template", str(path), "--out", str(out)]) == 0
    assert set(load_connections(out)) == {"sales_ora", "lake", "crm_td"}


def test_connections_template_for_other_engines(tmp_path: Path) -> None:
    from tmig.data.connection_check import FoundConnection, driver_for, template
    from tmig.ir import LiveConnection

    def found(connection_id: str, dbclass: str, dialect: str, server: str) -> FoundConnection:
        connection = LiveConnection(
            kind="live",
            connection_id=connection_id,
            dbclass=dbclass,
            dialect=dialect,  # type: ignore[arg-type]
            server=server,
        )
        return FoundConnection(connection, ["報表"])

    entries = {
        "presto_x": found("presto_x", "presto", "trino", "trino.example.com"),
        "athena_x": found("athena_x", "athena", "athena", "athena.us-east-1.amazonaws.com"),
        "bigquery_x": found("bigquery_x", "bigquery", "bigquery", ""),
        "spark_x": found("spark_x", "spark", "spark", "spark.example.com"),
        "spark_db": found("spark_db", "spark", "spark", "adb-1.azuredatabricks.net"),
        "hive_x": found("hive_x", "hadoophive", "hive", "hive.example.com"),
        "impala_x": found("impala_x", "hadoophive", "impala", "impala.example.com"),
        "db2_x": found("db2_x", "db2", "db2", "db2.example.com"),
        "hana_x": found("hana_x", "saphana", "hana", "hana.example.com"),
    }
    # Spark SQL 連到 Databricks 時用 Databricks 的驅動；Cloudera Hadoop 連到 Impala 時用 Impala 的規則
    assert {k: driver_for(v.connection) for k, v in entries.items()} == {
        "presto_x": "trino",
        "athena_x": "athena",
        "bigquery_x": "bigquery",
        "spark_x": "spark",
        "spark_db": "databricks",
        "hive_x": "hive",
        "impala_x": "impala",
        "db2_x": "db2",
        "hana_x": "hana",
    }
    text = template(entries)
    assert '[connections.bigquery_x]\ndriver = "bigquery"\n# credentials_path' in text
    assert '[connections.presto_x]\ndriver = "trino"\nuser = "${TMIG_PRESTO_X_USER}"' in text
    assert '[connections.athena_x]\ndriver = "athena"\ns3_staging_dir = "s3://…/"' in text
    assert 'secret_access_key = "${TMIG_ATHENA_X_SECRET_ACCESS_KEY}"' in text
    assert '[connections.hive_x]\ndriver = "hive"\nuser = "${TMIG_HIVE_X_USER}"' in text
    assert '[connections.impala_x]\ndriver = "impala"\nuser = "${TMIG_IMPALA_X_USER}"' in text
    assert (
        '[connections.spark_db]\ndriver = "databricks"\naccess_token = "${TMIG_SPARK_DB_TOKEN}"'
        in text
    )
    assert text.count('# auth_mechanism = "LDAP"') == 3
    config = write_config(tmp_path / "connections.toml", text)
    assert {c.driver for c in load_connections(config).values()} == {
        "trino",
        "athena",
        "bigquery",
        "spark",
        "databricks",
        "hive",
        "impala",
        "db2",
        "hana",
    }


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
    assert "失敗  crm_td（teradata，crm.example.com）：連線設定檔中沒有這個連線" in out
    assert "共 3 個連線：通過 1、失敗 2" in out


def test_connection_statuses_without_connecting(tmp_path: Path) -> None:
    found = collect([load_ir(connections_ir(tmp_path))])
    assert list(found) == ["crm_td", "lake", "sales_ora"]
    statuses = check(found, duckdb_configs("sales_ora"), connect=False)
    assert [s.ok for s in statuses] == [False, False, True]
    assert statuses[2].message == "設定齊全（未測試連線）"
    assert "[connections.sales_ora]" in template(found)
