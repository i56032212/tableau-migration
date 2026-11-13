"""桌面版的「連線到伺服器」：作者在連線對話框輸入的資料庫連線。

- 連線的設定（類型、主機、連接埠、資料庫、帳號與其他設定）存在設定資料夾的 connections.toml（與伺服器的
  連線設定檔相同的格式，見 tmig.data.sources）；**不含密碼**：密碼寫成 ${TMIG_DB_<連線代號>}，執行時只在
  本機引擎的記憶體中，勾選「記住密碼」時另存在認證管理員（Windows 的 Credential Manager），下次啟動時
  讀回；也可以改用作者自己設定的環境變數（${變數名稱}）
- 報表的 IR 只記連線本身（LiveConnection，以 connection_id 對應設定），發布到伺服器後由伺服器的連線
  設定檔提供伺服器用的帳號（建議唯讀的服務帳號）
- API（/api/desktop/…）：connectors、connections（列出、建立並測試、登入、刪除）、資料表與欄位
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from tmig.data.catalog import (
    CONNECTORS,
    CatalogError,
    list_tables,
    sql_columns,
    table_columns,
)
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig, SourceConnectionError
from tmig.ir import FileColumn, LiveConnection
from tmig.parser.connections import connection_id
from tmig_service.credentials import CredentialStore

SETTINGS_FILE = "connections.toml"
SECRET_TARGET = "tmig-desktop-db:"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def secret_env(connection: str) -> str:
    """密碼在本機引擎中的環境變數名稱（只在記憶體中）。"""
    return "TMIG_DB_" + re.sub(r"[^A-Z0-9_]", "_", connection.upper())


class ConnectionBody(BaseModel):
    """連線對話框：類型、主機、連接埠、資料庫、結構描述、帳號、密碼（或放密碼的環境變數）與其他設定。"""

    model_config = ConfigDict(extra="forbid")
    dbclass: str
    server: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    user: str | None = None
    password: str | None = Field(default=None, repr=False)
    password_env: str | None = None
    remember: bool = False
    extras: dict[str, str] = {}


class SignInBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(repr=False)
    remember: bool = False


class ColumnsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: list[str] | None = None
    sql: str | None = None


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


class DesktopConnections:
    """連線設定檔（不含密碼）與記住的密碼。"""

    def __init__(self, path: Path, credentials: CredentialStore) -> None:
        self.path = path
        self.credentials = credentials

    # ------------------------------------------------------------ 檔案

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"connections": {}, "desktop": {}}
        try:
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {"connections": {}, "desktop": {}}
        data.setdefault("connections", {})
        data.setdefault("desktop", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        lines = [
            "# 桌面版建立的資料庫連線（不含密碼：密碼只在記憶體或認證管理員中）",
            "",
        ]
        for section in ("connections", "desktop"):
            for key, values in sorted(data.get(section, {}).items()):
                lines.append(f"[{section}.{key}]")
                for name, value in values.items():
                    if value is not None:
                        lines.append(f"{name} = {_toml_value(value)}")
                lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(self.path)

    # ------------------------------------------------------------ 讀取

    def configs(self) -> dict[str, ConnectionConfig]:
        data = self._load()
        return {
            key: ConnectionConfig(key, str(raw.get("driver", "")), dict(raw))
            for key, raw in data["connections"].items()
            if isinstance(raw, dict)
        }

    def connection(self, key: str) -> LiveConnection:
        """連線本身（沒有資料表），新資料源的 LiveConnection 以它為起點。"""
        meta = self._load()["desktop"].get(key)
        if not isinstance(meta, dict):
            raise HTTPException(404, f"找不到連線 {key}")
        dbclass = str(meta.get("dbclass", ""))
        connector = CONNECTORS.get(dbclass)
        port = meta.get("port")
        return LiveConnection(
            kind="live",
            connection_id=key,
            dbclass=dbclass,
            dialect=connector.dialect if connector else None,  # type: ignore[arg-type]
            server=meta.get("server") or None,
            port=int(port) if isinstance(port, int) else None,
            database=meta.get("database") or None,
            schema_name=meta.get("schema") or None,
            http_path=meta.get("http_path") or None,
        )

    def entries(self) -> list[dict[str, Any]]:
        data = self._load()
        result: list[dict[str, Any]] = []
        for key, meta in sorted(data["desktop"].items()):
            raw = data["connections"].get(key, {})
            connector = CONNECTORS.get(str(meta.get("dbclass", "")))
            missing = ConnectionConfig(key, str(raw.get("driver", "")), dict(raw)).missing_env()
            result.append(
                {
                    "id": key,
                    "dbclass": meta.get("dbclass"),
                    "label": connector.label if connector else meta.get("dbclass"),
                    "server": meta.get("server"),
                    "port": meta.get("port"),
                    "database": meta.get("database"),
                    "schema": meta.get("schema"),
                    "user": raw.get("user"),
                    "signed_in": not missing,
                }
            )
        return result

    # ------------------------------------------------------------ 變更

    def register(self, body: ConnectionBody) -> tuple[LiveConnection, ConnectionConfig]:
        connector = CONNECTORS.get(body.dbclass)
        if connector is None:
            raise HTTPException(422, f"不支援的連線類型 {body.dbclass}")
        server = (body.server or "").strip() or None
        database = (body.database or "").strip() or None
        if connector.dbclass not in ("bigquery", "athena") and not server:
            raise HTTPException(422, "請輸入伺服器")
        if connector.database and connector.dbclass in ("oracle", "bigquery") and not database:
            raise HTTPException(422, f"請輸入{connector.database}")
        key = connection_id(connector.dbclass, server, database)
        raw: dict[str, Any] = {"driver": connector.driver}
        if body.user:
            raw["user"] = body.user.strip()
        allowed = {k for k, _ in connector.extras}
        for name, value in body.extras.items():
            if name not in allowed:
                raise HTTPException(422, f"{connector.label} 沒有設定 {name}")
            if value.strip():
                raw[name] = value.strip()
        if connector.secret:
            if body.password_env:
                if not ENV_NAME.match(body.password_env):
                    raise HTTPException(422, "環境變數的名稱只能有英文字母、數字與底線")
                raw[connector.secret] = "${" + body.password_env + "}"
            else:
                raw[connector.secret] = "${" + secret_env(key) + "}"
                if body.password is not None:
                    self._keep(key, body.user or "", body.password, body.remember)
        data = self._load()
        data["connections"][key] = {k: v for k, v in raw.items() if v is not None}
        data["desktop"][key] = {
            "dbclass": connector.dbclass,
            "server": server,
            "port": body.port or connector.port,
            "database": database,
            "schema": (body.schema_name or "").strip() or None,
            "http_path": body.extras.get("http_path") or None,
        }
        self._save(data)
        return self.connection(key), ConnectionConfig(
            key, connector.driver, data["connections"][key]
        )

    def sign_in(self, key: str, password: str, remember: bool) -> None:
        raw = self._load()["connections"].get(key)
        if not isinstance(raw, dict):
            raise HTTPException(404, f"找不到連線 {key}")
        self._keep(key, str(raw.get("user", "")), password, remember)

    def remove(self, key: str) -> None:
        data = self._load()
        data["connections"].pop(key, None)
        data["desktop"].pop(key, None)
        self._save(data)
        self.credentials.delete(SECRET_TARGET + key)
        os.environ.pop(secret_env(key), None)

    def restore(self) -> None:
        """啟動時讀回記住的密碼（放進本機引擎的記憶體）。"""
        for key in self._load()["desktop"]:
            name = secret_env(key)
            if name in os.environ:
                continue
            try:
                secret = self.credentials.get(SECRET_TARGET + key)
            except OSError:
                secret = None
            if secret is not None:
                os.environ[name] = secret

    def _keep(self, key: str, user: str, password: str, remember: bool) -> None:
        os.environ[secret_env(key)] = password
        if remember:
            self.credentials.set(SECRET_TARGET + key, user, password)
        else:
            self.credentials.delete(SECRET_TARGET + key)


def connections_router(
    registry_of: Callable[[Request], DesktopConnections],
) -> APIRouter:
    router = APIRouter(prefix="/api/desktop")

    def runtime(request: Request) -> DataRuntime:
        return request.app.state.runtime

    def fetch_with(request: Request, connection: LiveConnection) -> Any:
        class _Fetcher:
            def fetch(self, sql: str) -> Any:
                return runtime(request).fetch(connection, sql)

        return _Fetcher()

    @router.get("/connectors")
    def get_connectors() -> dict[str, Any]:
        return {"connectors": [c.to_json() for c in CONNECTORS.values()]}

    @router.get("/connections")
    def get_connections(request: Request) -> dict[str, Any]:
        return {"connections": registry_of(request).entries()}

    @router.post("/connections")
    def post_connection(body: ConnectionBody, request: Request) -> dict[str, Any]:
        """建立（或更新）連線並測試：成功時回傳連線與預設的結構描述（或資料庫）中的資料表。"""
        registry = registry_of(request)
        connection, config = registry.register(body)
        runtime(request).add_configs({connection.connection_id: config})
        missing = config.missing_env()
        if missing:
            raise HTTPException(
                422, f"連線需要的環境變數未設定：{'、'.join(missing)}（或改為輸入密碼）"
            )
        tables = _tables(request, connection, connection.schema_name)
        entry = next(e for e in registry.entries() if e["id"] == connection.connection_id)
        return {"connection": entry, "tables": tables}

    @router.post("/connections/{key}/sign-in")
    def sign_in(key: str, body: SignInBody, request: Request) -> dict[str, Any]:
        registry = registry_of(request)
        registry.sign_in(key, body.password, body.remember)
        runtime(request).add_configs({k: v for k, v in registry.configs().items() if k == key})
        connection = registry.connection(key)
        _tables(request, connection, connection.schema_name)  # 測試連線
        return {"connection": next(e for e in registry.entries() if e["id"] == key)}

    @router.delete("/connections/{key}")
    def delete_connection(key: str, request: Request) -> dict[str, bool]:
        registry_of(request).remove(key)
        return {"ok": True}

    @router.get("/connections/{key}/tables")
    def get_tables(key: str, request: Request, schema: str | None = None) -> dict[str, Any]:
        connection = registry_of(request).connection(key)
        return {"tables": _tables(request, connection, schema or connection.schema_name)}

    @router.post("/connections/{key}/columns")
    def post_columns(key: str, body: ColumnsBody, request: Request) -> dict[str, Any]:
        connection = registry_of(request).connection(key)
        columns = remote_columns(request, connection, body.table, body.sql)
        return {"columns": [c.model_dump(mode="json") for c in columns]}

    def _tables(request: Request, connection: LiveConnection, schema: str | None) -> list[Any]:
        dialect = connection.dialect
        if dialect is None:
            raise HTTPException(422, f"不支援的連線類型 {connection.dbclass}")
        try:
            found = list_tables(
                fetch_with(request, connection), dialect, connection.database, schema
            )
        except CatalogError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceConnectionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(422, f"無法列出資料表：{exc}") from exc
        return [t.to_json() for t in found]

    return router


def remote_columns(
    request: Request, connection: LiveConnection, table: list[str] | None, sql: str | None
) -> list[FileColumn]:
    """資料表或自訂 SQL 的欄位（在來源資料庫查詢）。"""
    runtime: DataRuntime = request.app.state.runtime

    class _Fetcher:
        def fetch(self, sql: str) -> Any:
            return runtime.fetch(connection, sql)

    dialect = connection.dialect
    if dialect is None:
        raise HTTPException(422, f"不支援的連線類型 {connection.dbclass}")
    if (table is None) == (sql is None):
        raise HTTPException(422, "要指定資料表或自訂 SQL 其中之一")
    try:
        if table is not None:
            return table_columns(_Fetcher(), dialect, table)
        return sql_columns(_Fetcher(), dialect, sql or "")
    except CatalogError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SourceConnectionError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"查詢失敗：{exc}") from exc


__all__ = [
    "SECRET_TARGET",
    "SETTINGS_FILE",
    "ConnectionBody",
    "DesktopConnections",
    "connections_router",
    "remote_columns",
    "secret_env",
]
