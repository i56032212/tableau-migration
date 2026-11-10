"""來源資料庫的連線設定與執行器（第三階段 N3、N4）。

憑證不進 IR、不進 git。部署環境以連線設定檔（TOML；路徑由環境變數 TMIG_CONNECTIONS 指定，
預設為目前目錄的 connections.toml）依 connection_id 提供連線方式：

    [connections.oracle_ora_example_com_orclpdb]
    driver = "oracle"                  # oracle、sqlserver、mysql、snowflake、postgres、databricks、duckdb
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
DRIVERS = ("oracle", "postgres", "databricks", "sqlserver", "mysql", "snowflake", "duckdb")
DRIVER_DIALECTS = {
    "oracle": "oracle",
    "postgres": "postgres",
    "databricks": "databricks",
    "sqlserver": "tsql",
    "mysql": "mysql",
    "snowflake": "snowflake",
    "duckdb": "duckdb",
}
SECRET_KEYS = frozenset({"password", "access_token", "token", "client_secret"})
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
    if config.driver == "oracle":
        return OracleExecutor.open(connection, params)
    if config.driver == "sqlserver":
        return SqlServerExecutor.open(connection, params)
    if config.driver == "mysql":
        return MySQLExecutor.open(connection, params)
    if config.driver == "snowflake":
        return SnowflakeExecutor.open(connection, params)
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
    "ConnectionConfig",
    "DatabricksExecutor",
    "DuckDBExecutor",
    "Executor",
    "MySQLExecutor",
    "OracleExecutor",
    "PostgresExecutor",
    "SnowflakeExecutor",
    "SourceConnectionError",
    "SqlServerExecutor",
    "config_path",
    "executor_for",
    "load_connections",
    "normalize_arrow",
    "open_executor",
    "probe_sql",
    "rows_to_arrow",
]
