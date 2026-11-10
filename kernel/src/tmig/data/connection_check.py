"""tmig connections：盤點報表用到的 Live 連線、產生連線設定檔範本、測試連線（第三階段 N4）。

    tmig connections template <報表或資料夾>...   產生 connections.toml 範本（憑證寫成 ${環境變數}）
    tmig connections check <報表或資料夾>...      檢查設定與環境變數，並以唯讀查詢測試連線

輸入可以是 .twb／.twbx、IR 檔（*.ir.json），或含這些檔案的資料夾。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tmig.data.sources import (
    DRIVER_DIALECTS,
    ConnectionConfig,
    SourceConnectionError,
    open_executor,
    probe_sql,
)
from tmig.ir import IR, ExtractConnection, FileConnection, LiveConnection, load_ir
from tmig.parser.workbook import parse_workbook

DBCLASS_DRIVERS = {
    "oracle": "oracle",
    "databricks": "databricks",
    "spark": "databricks",
    "postgres": "postgres",
    "greenplum": "postgres",
    "sqlserver": "sqlserver",
    "azure_sqldb": "sqlserver",
    "azure_sql_dw": "sqlserver",
    "mysql": "mysql",
    "mariadb": "mysql",
    "snowflake": "snowflake",
}
"""Tableau 的連線類型 → 執行器（其他類型尚無執行器）。"""


@dataclass
class FoundConnection:
    connection: LiveConnection
    workbooks: list[str] = field(default_factory=list)


@dataclass
class ConnectionStatus:
    found: FoundConnection
    configured: bool
    missing_env: list[str]
    ok: bool
    message: str


def workbook_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    for path in paths:
        if path.is_dir():
            for pattern in ("*.twb", "*.twbx", "*.ir.json"):
                result += sorted(path.rglob(pattern))
        else:
            result.append(path)
    return result


def load_workbook(path: Path) -> IR:
    if path.name.endswith(".json"):
        return load_ir(path)
    return parse_workbook(path, lenient=True)


def collect(irs: Iterable[IR]) -> dict[str, FoundConnection]:
    """各報表的 Live 連線與萃取的來源（萃取層更新用），依 connection_id 合併。"""
    found: dict[str, FoundConnection] = {}
    for ir in irs:
        for ds in ir.datasources:
            connection = ds.connection
            label = ir.workbook.name
            if isinstance(connection, FileConnection):
                continue  # 檔案在 .twbx 內，不需要連線設定
            if isinstance(connection, ExtractConnection):
                if connection.source is None or connection.refresh is None:
                    continue
                connection, label = connection.source, f"{label}（萃取更新）"
            entry = found.setdefault(connection.connection_id, FoundConnection(connection))
            if label not in entry.workbooks:
                entry.workbooks.append(label)
    return dict(sorted(found.items()))


def env_name(connection_id: str, suffix: str) -> str:
    return f"TMIG_{connection_id.upper()}_{suffix}"


def template(found: dict[str, FoundConnection]) -> str:
    lines = [
        "# 連線設定檔（tmig connections template 產生）",
        "# 帳號、密碼、權杖一律寫成 ${環境變數}，由部署環境或密碼管理服務提供；請使用唯讀的服務帳號",
        "# 主機、連接埠、資料庫省略時取工作簿中的連線資訊；要改連其他主機（例如唯讀複本）時再填",
        "",
    ]
    for connection_id, entry in found.items():
        connection = entry.connection
        where = "／".join(
            p
            for p in (
                f"{connection.server}:{connection.port}" if connection.port else connection.server,
                connection.database,
            )
            if p
        )
        lines.append(
            f"# {connection.dbclass}：{where or '（未標示主機）'}（{'、'.join(entry.workbooks)}）"
        )
        driver = DBCLASS_DRIVERS.get(connection.dbclass)
        if driver is None:
            lines.append(
                f"# 連線類型 {connection.dbclass} 尚無執行器，這些報表暫時無法以 Live 連線查詢"
            )
            lines.append("")
            continue
        lines.append(f"[connections.{connection_id}]")
        lines.append(f'driver = "{driver}"')
        if driver == "databricks":
            lines.append(f'access_token = "${{{env_name(connection_id, "TOKEN")}}}"')
        else:
            lines.append(f'user = "${{{env_name(connection_id, "USER")}}}"')
            lines.append(f'password = "${{{env_name(connection_id, "PASSWORD")}}}"')
        if driver == "snowflake":
            lines.append('# warehouse = "…"   # 省略時用帳號的預設 warehouse')
            lines.append('# role = "…"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def check(
    found: dict[str, FoundConnection], configs: dict[str, ConnectionConfig], connect: bool = True
) -> list[ConnectionStatus]:
    statuses: list[ConnectionStatus] = []
    for connection_id, entry in found.items():
        config = configs.get(connection_id)
        if config is None:
            statuses.append(ConnectionStatus(entry, False, [], False, "連線設定檔中沒有這個連線"))
            continue
        missing = config.missing_env()
        if missing:
            statuses.append(
                ConnectionStatus(
                    entry, True, missing, False, f"環境變數未設定：{'、'.join(missing)}"
                )
            )
            continue
        if not connect:
            statuses.append(ConnectionStatus(entry, True, [], True, "設定齊全（未測試連線）"))
            continue
        statuses.append(_probe(entry, configs))
    return statuses


def _probe(entry: FoundConnection, configs: dict[str, ConnectionConfig]) -> ConnectionStatus:
    try:
        executor = open_executor(entry.connection, configs)
    except SourceConnectionError as exc:
        return ConnectionStatus(entry, True, [], False, str(exc))
    try:
        executor.fetch(
            probe_sql(DRIVER_DIALECTS.get(configs[entry.connection.connection_id].driver, ""))
        )
    except Exception as exc:
        return ConnectionStatus(entry, True, [], False, f"測試查詢失敗：{exc}")
    finally:
        executor.close()
    return ConnectionStatus(entry, True, [], True, "連線成功")


__all__ = [
    "ConnectionStatus",
    "FoundConnection",
    "check",
    "collect",
    "env_name",
    "load_workbook",
    "template",
    "workbook_paths",
]
