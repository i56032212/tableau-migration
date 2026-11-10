"""來源資料庫的連線設定與執行器（第三階段 N3、N4）。

憑證不進 IR、不進 git。部署環境以連線設定檔（TOML；路徑由環境變數 TMIG_CONNECTIONS 指定，
預設為目前目錄的 connections.toml）依 connection_id 提供連線方式：

    [connections.oracle_ora_example_com_orclpdb]
    driver = "oracle"                  # oracle、sqlserver、mysql、snowflake、postgres、redshift、databricks、duckdb
    user = "${ORA_SALES_USER}"
    password = "${ORA_SALES_PASSWORD}"
    # host、port、service 省略時取工作簿中的連線資訊

    [connections.databricks_adb_123_azuredatabricks_net_main]
    driver = "databricks"
    access_token = "${DATABRICKS_TOKEN}"

    [connections.sqlserver_sqlsrv01_salesdb]
    driver = "sqlserver"
    user = "${MSSQL_SALES_USER}"
    password = "${MSSQL_SALES_PASSWORD}"

    [connections.snowflake_acme_snowflakecomputing_com_sales]
    driver = "snowflake"
    user = "${SNOWFLAKE_USER}"
    password = "${SNOWFLAKE_PASSWORD}"
    warehouse = "REPORTING_WH"         # account 省略時取主機名稱的第一段

    [connections.presto_trino_example_com_hive]
    driver = "trino"                   # Trino、Starburst；PrestoDB 用 presto
    user = "${TRINO_USER}"
    password = "${TRINO_PASSWORD}"     # 有密碼時以 HTTPS 連線

    [connections.athena_athena_us_east_1_amazonaws_com]
    driver = "athena"
    s3_staging_dir = "s3://query-results-bucket/tableau/"
    # 省略 access_key_id、secret_access_key 時用 AWS 預設的憑證（環境變數、設定檔或 IAM 角色）

    [connections.bigquery_sales_project]
    driver = "bigquery"
    credentials_path = "/run/secrets/bigquery-reader.json"   # 省略時用 Application Default Credentials
    maximum_bytes_billed = 10000000000                       # 每次查詢最多掃描的位元組（可省略）

- 密碼、權杖一律寫成 ${環境變數}，不能直接寫在設定檔中；環境變數由部署環境或密碼管理服務注入
- 建議使用唯讀的服務帳號；PostgreSQL 與 MySQL 的工作階段另外設為唯讀
- 執行器回傳 Arrow 資料表；小數（DECIMAL／NUMBER）轉成浮點數或整數、含時區的時間轉成 UTC 的
  無時區時間，與萃取匯入 DuckDB 後的型別一致
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tomllib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import duckdb
import pyarrow as pa

from tmig.ir.models import LiveConnection

CONFIG_ENV = "TMIG_CONNECTIONS"
DEFAULT_CONFIG = "connections.toml"
DRIVERS = (
    "oracle",
    "postgres",
    "redshift",
    "databricks",
    "sqlserver",
    "mysql",
    "snowflake",
    "trino",
    "presto",
    "athena",
    "bigquery",
    "duckdb",
)
DRIVER_DIALECTS = {
    "oracle": "oracle",
    "postgres": "postgres",
    "redshift": "redshift",
    "databricks": "databricks",
    "sqlserver": "tsql",
    "mysql": "mysql",
    "snowflake": "snowflake",
    "trino": "trino",
    "presto": "trino",
    "athena": "athena",
    "bigquery": "bigquery",
    "duckdb": "duckdb",
}
SECRET_KEYS = frozenset(
    {"password", "access_token", "token", "client_secret", "secret_access_key", "session_token"}
)
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SourceConnectionError(RuntimeError):
    """連線設定不完整，或無法連上來源資料庫。"""


class Executor(Protocol):
    """在來源資料庫執行一段（已轉成該方言的）SQL，回傳 Arrow 資料表。"""

    dialect: str

    def fetch(self, sql: str) -> pa.Table: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------- 設定檔


@dataclass
class ConnectionConfig:
    connection_id: str
    driver: str
    raw: dict[str, Any] = field(default_factory=dict)
    """設定檔中的原文（${環境變數} 尚未展開）。"""

    def missing_env(self) -> list[str]:
        names: list[str] = []
        for value in self.raw.values():
            if isinstance(value, str):
                names += [n for n in ENV_REF.findall(value) if n not in os.environ]
        return sorted(set(names))

    def resolved(self) -> dict[str, Any]:
        """展開 ${環境變數}；缺少環境變數時報錯（訊息不含任何值）。"""
        missing = self.missing_env()
        if missing:
            raise SourceConnectionError(
                f"連線 {self.connection_id} 需要的環境變數未設定：{'、'.join(missing)}"
            )
        return {
            key: ENV_REF.sub(lambda m: os.environ[m.group(1)], value)
            if isinstance(value, str)
            else value
            for key, value in self.raw.items()
        }


def config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def load_connections(path: str | Path | None = None) -> dict[str, ConnectionConfig]:
    """讀取連線設定檔；檔案不存在時回傳空的設定（Live 連線一律無法執行）。"""
    file = config_path(path)
    if not file.is_file():
        return {}
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SourceConnectionError(f"連線設定檔 {file} 格式錯誤：{exc}") from exc
    section = data.get("connections", {})
    if not isinstance(section, dict):
        raise SourceConnectionError(f"連線設定檔 {file} 的 [connections] 格式錯誤")
    configs: dict[str, ConnectionConfig] = {}
    for connection_id, raw in section.items():
        if not isinstance(raw, dict):
            raise SourceConnectionError(f"連線 {connection_id} 的設定格式錯誤")
        driver = raw.get("driver")
        if driver not in DRIVERS:
            raise SourceConnectionError(
                f"連線 {connection_id} 的 driver 必須是 {'、'.join(DRIVERS)} 之一（目前為 {driver!r}）"
            )
        for key, value in raw.items():
            if key in SECRET_KEYS and not (isinstance(value, str) and ENV_REF.fullmatch(value)):
                raise SourceConnectionError(
                    f"連線 {connection_id} 的 {key} 不能直接寫在設定檔中，請改用 ${{環境變數}}"
                )
        configs[connection_id] = ConnectionConfig(connection_id, driver, dict(raw))
    return configs


# ---------------------------------------------------------------- 開啟執行器


def open_executor(connection: LiveConnection, configs: dict[str, ConnectionConfig]) -> Executor:
    config = configs.get(connection.connection_id)
    if config is None:
        raise SourceConnectionError(
            f"連線設定檔中沒有連線 {connection.connection_id}（{connection.dbclass}"
            f"{'，' + connection.server if connection.server else ''}）"
        )
    params = config.resolved()
    if config.driver == "duckdb":
        path = params.get("path")
        if not path:
            raise SourceConnectionError(f"連線 {config.connection_id} 缺少 path")
        return DuckDBExecutor(Path(path))
    if config.driver == "postgres":
        return PostgresExecutor.open(connection, params)
    if config.driver == "redshift":
        return RedshiftExecutor.open(connection, params)
    if config.driver == "oracle":
        return OracleExecutor.open(connection, params)
    if config.driver == "sqlserver":
        return SqlServerExecutor.open(connection, params)
    if config.driver == "mysql":
        return MySQLExecutor.open(connection, params)
    if config.driver == "snowflake":
        return SnowflakeExecutor.open(connection, params)
    if config.driver in ("trino", "presto"):
        return TrinoExecutor.open(connection, params, presto=config.driver == "presto")
    if config.driver == "athena":
        return AthenaExecutor.open(connection, params)
    if config.driver == "bigquery":
        return BigQueryExecutor.open(connection, params)
    return DatabricksExecutor.open(connection, params)


@contextmanager
def executor_for(
    connection: LiveConnection, configs: dict[str, ConnectionConfig]
) -> Iterator[Executor]:
    executor = open_executor(connection, configs)
    try:
        yield executor
    finally:
        executor.close()


def probe_sql(dialect: str) -> str:
    """測試連線用的查詢。"""
    return "SELECT 1 FROM DUAL" if dialect == "oracle" else "SELECT 1"


# ---------------------------------------------------------------- 執行器


class DuckDBExecutor:
    """DuckDB 檔（測試，或已匯入 DuckDB 的資料）：以唯讀模式開啟。"""

    dialect = "duckdb"

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise SourceConnectionError(f"找不到 DuckDB 檔 {path}")
        self.con = duckdb.connect(str(path), read_only=True)

    def fetch(self, sql: str) -> pa.Table:
        relation = self.con.sql(sql)
        # DuckDB 2.0 為 to_arrow_table；1.x 為 arrow
        method = getattr(relation, "to_arrow_table", None) or relation.arrow
        return method()

    def close(self) -> None:
        self.con.close()


class PostgresExecutor:
    """PostgreSQL（psycopg 3）：工作階段設為唯讀。"""

    dialect = "postgres"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> PostgresExecutor:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - 依安裝的驅動
            raise SourceConnectionError("未安裝 PostgreSQL 驅動：uv sync --extra postgres") from exc
        kwargs: dict[str, Any] = {
            "host": params.get("host") or connection.server,
            "port": params.get("port") or connection.port,
            "dbname": params.get("database") or connection.database,
            "user": params.get("user"),
            "password": params.get("password"),
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            con = psycopg.connect(params.get("dsn", ""), autocommit=True, **kwargs)
            con.execute("SET default_transaction_read_only = on")
            if params.get("timezone"):
                con.execute("SELECT set_config('TimeZone', %s, false)", [params["timezone"]])
        except psycopg.Error as exc:
            raise SourceConnectionError(
                f"無法連上 PostgreSQL（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:
        with self.con.cursor() as cursor:
            cursor.execute(sql)
            description = cursor.description or []
            names = [d.name for d in description]
            scales = [_pg_scale(d) for d in description]
            rows = cursor.fetchall()
        return rows_to_arrow(names, rows, scales)

    def close(self) -> None:
        self.con.close()


def _pg_scale(column: Any) -> int | None:
    scale = getattr(column, "scale", None)
    return scale if isinstance(scale, int) else None


ORACLE_SESSION = (
    "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS' "
    "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6' "
    "NLS_NUMERIC_CHARACTERS = '.,' NLS_DATE_LANGUAGE = 'ENGLISH'"
)


class OracleExecutor:
    """Oracle（python-oracledb 的 thin 模式，不需要 Oracle Client）。尚未實機驗證。"""

    dialect = "oracle"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> OracleExecutor:
        try:
            import oracledb  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError("未安裝 Oracle 驅動：uv sync --extra oracle") from exc
        dsn = params.get("dsn")
        if not dsn:
            host = params.get("host") or connection.server
            port = params.get("port") or connection.port or 1521
            service = params.get("service") or connection.database
            if not host or not service:
                raise SourceConnectionError(f"連線 {connection.connection_id} 缺少主機或服務名稱")
            dsn = f"{host}:{port}/{service}"
        try:
            con = oracledb.connect(
                user=params.get("user"), password=params.get("password"), dsn=dsn
            )
            with con.cursor() as cursor:
                cursor.execute(ORACLE_SESSION)
        except oracledb.Error as exc:  # pragma: no cover
            raise SourceConnectionError(
                f"無法連上 Oracle（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:  # pragma: no cover - 需要 Oracle
        import oracledb  # pyright: ignore[reportMissingImports]

        def handler(cursor: Any, metadata: Any) -> Any:
            if metadata.type_code is oracledb.DB_TYPE_NUMBER:
                if metadata.scale == 0 and (metadata.precision or 0) > 0:
                    return cursor.var(int, arraysize=cursor.arraysize)
                return cursor.var(float, arraysize=cursor.arraysize)
            if metadata.type_code in (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB):
                return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
            return None

        with self.con.cursor() as cursor:
            cursor.outputtypehandler = handler
            cursor.arraysize = 5000
            cursor.execute(sql)
            names = [d[0] for d in cursor.description or []]
            rows = cursor.fetchall()
        return rows_to_arrow(names, rows)

    def close(self) -> None:  # pragma: no cover
        self.con.close()


class DatabricksExecutor:
    """Databricks SQL（databricks-sql-connector）：工作階段的時區設為 UTC（可在設定檔以 timezone 改）。"""

    dialect = "databricks"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> DatabricksExecutor:
        try:
            from databricks import sql as dbsql  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError(
                "未安裝 Databricks 驅動：uv sync --extra databricks"
            ) from exc
        host = params.get("host") or connection.server
        http_path = params.get("http_path") or connection.http_path
        if not host or not http_path:
            raise SourceConnectionError(f"連線 {connection.connection_id} 缺少主機或 HTTP 路徑")
        try:  # pragma: no cover - 需要 Databricks
            con = dbsql.connect(
                server_hostname=host,
                http_path=http_path,
                access_token=params.get("access_token"),
                catalog=params.get("catalog") or connection.database,
                schema=params.get("schema") or connection.schema_name,
                session_configuration={
                    "spark.sql.session.timeZone": params.get("timezone") or "UTC"
                },
            )
        except Exception as exc:  # pragma: no cover
            raise SourceConnectionError(
                f"無法連上 Databricks（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:  # pragma: no cover - 需要 Databricks
        with self.con.cursor() as cursor:
            cursor.execute(sql)
            table = cursor.fetchall_arrow()
        return normalize_arrow(table)

    def close(self) -> None:  # pragma: no cover
        self.con.close()


class SqlServerExecutor:
    """SQL Server 與 Azure SQL Database（pymssql）。工作階段設為英文（月份與星期名稱）、
    星期日為一週的第一天。SQL Server 沒有唯讀的工作階段設定：請使用唯讀的服務帳號。尚未實機驗證。"""

    dialect = "tsql"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> SqlServerExecutor:
        try:
            import pymssql  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError(
                "未安裝 SQL Server 驅動：uv sync --extra sqlserver"
            ) from exc
        host = params.get("host") or connection.server
        if not host:
            raise SourceConnectionError(f"連線 {connection.connection_id} 缺少主機")
        try:  # pragma: no cover - 需要 SQL Server
            con = pymssql.connect(
                server=host,
                port=str(params.get("port") or connection.port or 1433),
                user=params.get("user"),
                password=params.get("password"),
                database=params.get("database") or connection.database or "",
                charset="UTF-8",
                autocommit=True,
                login_timeout=int(params.get("login_timeout", 30)),
            )
            with con.cursor() as cursor:
                cursor.execute("SET LANGUAGE us_english; SET DATEFIRST 7")
        except Exception as exc:  # pragma: no cover
            raise SourceConnectionError(
                f"無法連上 SQL Server（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:  # pragma: no cover - 需要 SQL Server
        with self.con.cursor() as cursor:
            cursor.execute(sql)
            names = [d[0] for d in cursor.description or []]
            rows = cursor.fetchall()
        return rows_to_arrow(names, rows)

    def close(self) -> None:  # pragma: no cover
        self.con.close()


class RedshiftExecutor:
    """Amazon Redshift（redshift_connector）：工作階段的時區設為 UTC。尚未實機驗證。

    設定檔可以用 iam = true 與 cluster_identifier、db_user 等以 IAM 身分連線（依 redshift_connector 的參數）。
    """

    dialect = "redshift"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> RedshiftExecutor:
        try:
            import redshift_connector  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError("未安裝 Redshift 驅動：uv sync --extra redshift") from exc
        kwargs: dict[str, Any] = {
            "host": params.get("host") or connection.server,
            "port": int(params.get("port") or connection.port or 5439),
            "database": params.get("database") or connection.database,
            "user": params.get("user"),
            "password": params.get("password"),
        }
        for key in (
            "iam",
            "cluster_identifier",
            "db_user",
            "region",
            "access_key_id",
            "secret_access_key",
            "profile",
            "ssl",
            "sslmode",
        ):
            if key in params:
                kwargs[key] = params[key]
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:  # pragma: no cover - 需要 Redshift
            con = redshift_connector.connect(**kwargs)
            con.autocommit = True
            cursor = con.cursor()
            cursor.execute(f"SET timezone TO '{params.get('timezone') or 'UTC'}'")
            cursor.close()
        except Exception as exc:  # pragma: no cover
            raise SourceConnectionError(
                f"無法連上 Redshift（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:  # pragma: no cover - 需要 Redshift
        cursor = self.con.cursor()
        try:
            cursor.execute(sql)
            names = [d[0] for d in cursor.description or []]
            rows = cursor.fetchall()
        finally:
            cursor.close()
        return rows_to_arrow(names, rows)

    def close(self) -> None:  # pragma: no cover
        self.con.close()


class MySQLExecutor:
    """MySQL 8.0 以上（PyMySQL）：工作階段設為唯讀、時區為 UTC、月份與星期名稱為英文。"""

    dialect = "mysql"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> MySQLExecutor:
        try:
            import pymysql  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError("未安裝 MySQL 驅動：uv sync --extra mysql") from exc
        kwargs: dict[str, Any] = {
            "host": params.get("host") or connection.server,
            "port": int(params.get("port") or connection.port or 3306),
            "user": params.get("user"),
            "password": params.get("password") or "",
            "database": params.get("database") or connection.database,
            "unix_socket": params.get("socket"),
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            con = pymysql.connect(charset="utf8mb4", autocommit=True, **kwargs)
            with con.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("SET time_zone = '+00:00'")
                cursor.execute("SET lc_time_names = 'en_US'")
        except pymysql.Error as exc:
            raise SourceConnectionError(
                f"無法連上 MySQL（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:
        with self.con.cursor() as cursor:
            cursor.execute(sql)
            names = [d[0] for d in cursor.description or []]
            rows = [tuple(_mysql_value(v) for v in row) for row in cursor.fetchall()]
        return rows_to_arrow(names, rows)

    def close(self) -> None:
        self.con.close()


def _mysql_value(value: Any) -> Any:
    """BIT(1) 欄位（布林）以位元組傳回：轉成布林。"""
    if isinstance(value, bytes) and len(value) == 1:
        return value != b"\x00"
    return value


class SnowflakeExecutor:
    """Snowflake（snowflake-connector-python，以 Arrow 取回）：工作階段的時區設為 UTC。尚未實機驗證。"""

    dialect = "snowflake"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> SnowflakeExecutor:
        try:
            import snowflake.connector  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError("未安裝 Snowflake 驅動：uv sync --extra snowflake") from exc
        server = connection.server or ""
        account = params.get("account") or server.removesuffix(".snowflakecomputing.com")
        if not account:
            raise SourceConnectionError(f"連線 {connection.connection_id} 缺少帳戶（account）")
        kwargs: dict[str, Any] = {
            "account": account,
            "user": params.get("user"),
            "password": params.get("password"),
            "authenticator": params.get("authenticator"),
            "token": params.get("token"),
            "warehouse": params.get("warehouse"),
            "database": params.get("database") or connection.database,
            "schema": params.get("schema") or connection.schema_name,
            "role": params.get("role"),
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:  # pragma: no cover - 需要 Snowflake
            con = snowflake.connector.connect(
                session_parameters={"TIMEZONE": params.get("timezone") or "UTC"}, **kwargs
            )
        except Exception as exc:  # pragma: no cover
            raise SourceConnectionError(
                f"無法連上 Snowflake（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:  # pragma: no cover - 需要 Snowflake
        cursor = self.con.cursor()
        try:
            cursor.execute(sql)
            table = cursor.fetch_arrow_all()
            if table is None:
                # 沒有資料列時不回傳 Arrow：依欄名建立空的資料表
                names = [d[0] for d in cursor.description or []]
                return pa.table({name: pa.array([], pa.null()) for name in names})
        finally:
            cursor.close()
        return normalize_arrow(table)

    def close(self) -> None:  # pragma: no cover
        self.con.close()


class TrinoExecutor:
    """Trino、Starburst（trino 用戶端）與 PrestoDB（presto-python-client）：工作階段的時區設為 UTC。

    有密碼時以 HTTP 基本驗證（必須是 HTTPS）、有 access_token 時以 JWT 連線；catalog、schema 省略時
    取工作簿中的連線資訊。Trino 沒有唯讀的工作階段設定：請使用唯讀的服務帳號。
    """

    dialect = "trino"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(
        cls, connection: LiveConnection, params: dict[str, Any], presto: bool = False
    ) -> TrinoExecutor:
        try:
            if presto:
                import prestodb as client  # pyright: ignore[reportMissingImports]
            else:
                import trino as client  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            extra = "presto" if presto else "trino"
            raise SourceConnectionError(
                f"未安裝 {'PrestoDB' if presto else 'Trino'} 驅動：uv sync --extra {extra}"
            ) from exc
        host = params.get("host") or connection.server
        if not host:
            raise SourceConnectionError(f"連線 {connection.connection_id} 缺少主機")
        user = params.get("user") or "tmig"
        auth = None
        if params.get("password"):
            auth = client.auth.BasicAuthentication(user, params["password"])
        elif params.get("access_token") and not presto:
            auth = client.auth.JWTAuthentication(params["access_token"])
        scheme = params.get("http_scheme") or ("https" if auth is not None else "http")
        port = int(params.get("port") or connection.port or (443 if scheme == "https" else 8080))
        timezone = params.get("timezone") or "UTC"
        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "user": user,
            "catalog": params.get("catalog") or connection.database,
            "schema": params.get("schema") or connection.schema_name,
            "http_scheme": scheme,
            "source": "tmig",
        }
        if auth is not None:
            kwargs["auth"] = auth
        if presto:
            kwargs["http_headers"] = {"X-Presto-Time-Zone": timezone}
        else:
            kwargs["timezone"] = timezone
            if "verify" in params:
                kwargs["verify"] = params["verify"]
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            con = client.dbapi.connect(**kwargs)
        except Exception as exc:  # pragma: no cover
            raise SourceConnectionError(
                f"無法連上 {'PrestoDB' if presto else 'Trino'}（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:
        cursor = self.con.cursor()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
            description = cursor.description or []
        finally:
            cursor.close()
        names = [d[0] for d in description]
        scales = [d[5] if len(d) > 5 and isinstance(d[5], int) else None for d in description]
        return rows_to_arrow(names, rows, scales)

    def close(self) -> None:
        self.con.close()


class AthenaExecutor:
    """Amazon Athena（PyAthena，以 Arrow 取回）。尚未實機驗證。

    查詢結果寫到 s3_staging_dir（或工作群組 work_group 設定的位置）；AWS 憑證省略時用預設的來源
    （環境變數、設定檔 profile 或 IAM 角色），區域省略時取主機名稱 athena.<區域>.amazonaws.com。
    """

    dialect = "athena"

    def __init__(self, con: Any) -> None:
        self.con = con

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> AthenaExecutor:
        if not params.get("s3_staging_dir") and not params.get("work_group"):
            raise SourceConnectionError(
                f"連線 {connection.connection_id} 需要 s3_staging_dir 或 work_group（查詢結果的位置）"
            )
        try:
            from pyathena import connect  # pyright: ignore[reportMissingImports]
            from pyathena.arrow.cursor import ArrowCursor  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError("未安裝 Athena 驅動：uv sync --extra athena") from exc
        region = params.get("region") or _athena_region(connection.server)
        kwargs: dict[str, Any] = {
            "s3_staging_dir": params.get("s3_staging_dir"),
            "work_group": params.get("work_group"),
            "region_name": region,
            "schema_name": params.get("schema") or connection.schema_name or "default",
            "catalog_name": params.get("catalog") or "AwsDataCatalog",
            "aws_access_key_id": params.get("access_key_id"),
            "aws_secret_access_key": params.get("secret_access_key"),
            "aws_session_token": params.get("session_token"),
            "profile_name": params.get("profile"),
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:  # pragma: no cover - 需要 AWS
            con = connect(cursor_class=ArrowCursor, **kwargs)
        except Exception as exc:  # pragma: no cover
            raise SourceConnectionError(
                f"無法連上 Athena（{connection.connection_id}）：{exc}"
            ) from exc
        return cls(con)

    def fetch(self, sql: str) -> pa.Table:  # pragma: no cover - 需要 AWS
        cursor = self.con.cursor()
        try:
            cursor.execute(sql)
            table = cursor.as_arrow()
        finally:
            cursor.close()
        return normalize_arrow(table)

    def close(self) -> None:  # pragma: no cover
        self.con.close()


class BigQueryExecutor:
    """Google BigQuery（google-cloud-bigquery，以 Arrow 取回）。

    憑證：credentials_path（服務帳號金鑰檔的路徑；檔案由部署環境提供，不進 git）或 Application Default
    Credentials（gcloud、工作負載身分）。計費的專案 project 省略時取工作簿的專案；maximum_bytes_billed
    限制每次查詢掃描的資料量。endpoint 只給模擬器或私人端點用（不驗證身分）。BigQuery 的工作階段時區預設為 UTC。
    請使用只有讀取權限（BigQuery Data Viewer、Job User）的服務帳號。
    """

    dialect = "bigquery"

    def __init__(self, client: Any, job_config: Any) -> None:
        self.client = client
        self.job_config = job_config

    @classmethod
    def open(cls, connection: LiveConnection, params: dict[str, Any]) -> BigQueryExecutor:
        try:
            from google.cloud import bigquery  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise SourceConnectionError("未安裝 BigQuery 驅動：uv sync --extra bigquery") from exc
        project = params.get("project") or connection.database
        if not project:
            raise SourceConnectionError(f"連線 {connection.connection_id} 缺少專案（project）")
        kwargs: dict[str, Any] = {"project": project}
        try:
            if params.get("endpoint"):
                from google.api_core.client_options import (  # pyright: ignore[reportMissingImports]
                    ClientOptions,
                )
                from google.auth.credentials import (  # pyright: ignore[reportMissingImports]
                    AnonymousCredentials,
                )

                kwargs["client_options"] = ClientOptions(api_endpoint=params["endpoint"])
                kwargs["credentials"] = AnonymousCredentials()
            elif params.get("credentials_path"):
                from google.oauth2 import service_account  # pyright: ignore[reportMissingImports]

                kwargs["credentials"] = service_account.Credentials.from_service_account_file(
                    params["credentials_path"]
                )
            if params.get("location"):
                kwargs["location"] = params["location"]
            client = bigquery.Client(**kwargs)
        except Exception as exc:  # pragma: no cover - 依環境的憑證
            raise SourceConnectionError(
                f"無法連上 BigQuery（{connection.connection_id}）：{exc}"
            ) from exc
        job_config = bigquery.QueryJobConfig(use_legacy_sql=False)
        if params.get("maximum_bytes_billed"):
            job_config.maximum_bytes_billed = int(params["maximum_bytes_billed"])
        dataset = params.get("dataset") or connection.schema_name
        if dataset:
            # 自訂 SQL 中沒有寫專案與資料集的資料表
            job_config.default_dataset = dataset if "." in dataset else f"{project}.{dataset}"
        return cls(client, job_config)

    def fetch(self, sql: str) -> pa.Table:
        job = self.client.query(sql, job_config=self.job_config)
        return normalize_arrow(job.to_arrow(create_bqstorage_client=False))

    def close(self) -> None:
        self.client.close()


def _athena_region(server: str | None) -> str | None:
    """athena.us-east-1.amazonaws.com → us-east-1。"""
    match = re.match(r"athena[.-]([a-z0-9-]+)\.amazonaws\.com", server or "", re.IGNORECASE)
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------- Arrow


def rows_to_arrow(
    names: Sequence[str], rows: Sequence[Sequence[Any]], scales: Sequence[int | None] | None = None
) -> pa.Table:
    """DB-API 的結果列 → Arrow：小數轉成浮點數（宣告的小數位數為 0 且都是整數時轉成整數）、
    含時區的時間轉成 UTC 的無時區時間。"""
    columns: list[pa.Array] = []
    for index, _name in enumerate(names):
        values = [row[index] for row in rows]
        scale = scales[index] if scales is not None else None
        columns.append(_column(values, scale))
    return pa.Table.from_arrays(columns, names=list(names))


def _column(values: list[Any], scale: int | None) -> pa.Array:
    if any(isinstance(v, Decimal) for v in values):
        if scale == 0:
            return pa.array([None if v is None else int(v) for v in values], pa.int64())
        return pa.array([None if v is None else float(v) for v in values], pa.float64())
    if any(isinstance(v, dt.datetime) and v.tzinfo is not None for v in values):
        return pa.array([_naive_utc(v) for v in values], pa.timestamp("us"))
    return pa.array(values)


def _naive_utc(value: Any) -> Any:
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def normalize_arrow(table: pa.Table) -> pa.Table:
    """驅動直接回傳的 Arrow：小數轉成浮點數（小數位數為 0 時轉成整數）、含時區的時間轉成 UTC。"""
    columns: list[pa.ChunkedArray] = []
    for column in table.columns:
        kind = column.type
        if pa.types.is_decimal(kind):
            target = pa.int64() if kind.scale == 0 and kind.precision <= 18 else pa.float64()
            column = column.cast(target)
        elif pa.types.is_timestamp(kind) and kind.tz is not None:
            column = column.cast(pa.timestamp(kind.unit, tz="UTC")).cast(pa.timestamp(kind.unit))
        columns.append(column)
    return pa.Table.from_arrays(columns, names=table.column_names)


__all__ = [
    "CONFIG_ENV",
    "DRIVER_DIALECTS",
    "AthenaExecutor",
    "BigQueryExecutor",
    "ConnectionConfig",
    "DatabricksExecutor",
    "DuckDBExecutor",
    "Executor",
    "MySQLExecutor",
    "OracleExecutor",
    "PostgresExecutor",
    "RedshiftExecutor",
    "SnowflakeExecutor",
    "SourceConnectionError",
    "SqlServerExecutor",
    "TrinoExecutor",
    "config_path",
    "executor_for",
    "load_connections",
    "normalize_arrow",
    "open_executor",
    "probe_sql",
    "rows_to_arrow",
]
