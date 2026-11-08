"""發布前的檢查（D0）：桌面版先查一次，伺服器再查一次。

- 結構與參照完整（tmig.ir.check_references）
- 每個 Live 連線都有設定：伺服器上沒有就不能發布；桌面版沒有時只提醒，交給伺服器檢查
- 每張工作表都能查詢：以預設的篩選與參數，用發布者的身分（列層級權限的使用者函式）實際查一次

已知不支援的寫法（檢視器顯示「不支援」）只列為提醒，不擋發布，與開啟 Tableau 檔時相同。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from tmig.data.executor import ExecutionError
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError
from tmig.ir import IR, LiveConnection, check_references
from tmig.planner.identity import Identity
from tmig.planner.state import QueryState, StateError
from tmig.sqlgen.query import QueryError, worksheet_sql

Where = Literal["server", "desktop"]


@dataclass(frozen=True)
class Problem:
    level: Literal["error", "warning"]
    message: str
    worksheet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_report(
    ir: IR,
    directory: Path,
    runtime: DataRuntime,
    user: Identity | None,
    where: Where,
) -> list[Problem]:
    problems = [Problem("error", f"報表的結構不完整：{m}") for m in check_references(ir)]
    if problems:
        return problems
    configs = runtime.configs
    unavailable: set[str] = set()
    for ds in ir.datasources:
        connection = ds.connection
        if isinstance(connection, LiveConnection) and connection.connection_id not in configs:
            unavailable.add(ds.id)
            if where == "server":
                problems.append(
                    Problem(
                        "error",
                        f"伺服器上沒有連線「{connection.connection_id}」的設定（資料源「{ds.caption}」），"
                        "請管理員先在 connections.toml 加上這個連線",
                    )
                )
            else:
                problems.append(
                    Problem(
                        "warning",
                        f"桌面版沒有連線「{connection.connection_id}」的設定，"
                        f"資料源「{ds.caption}」的工作表交給伺服器檢查",
                    )
                )
    for sheet in ir.worksheets:
        if sheet.datasource in unavailable:
            continue
        datasource = next(d for d in ir.datasources if d.id == sheet.datasource)
        try:
            query = worksheet_sql(ir, sheet.id, QueryState(), user)
            runtime.run(query, datasource, directory / "data" / f"{sheet.datasource}.duckdb")
        except QueryError as exc:
            level: Literal["error", "warning"] = "warning" if exc.unsupported else "error"
            prefix = "不支援，檢視時不會顯示" if exc.unsupported else "無法查詢"
            problems.append(Problem(level, f"工作表「{sheet.name}」{prefix}：{exc}", sheet.name))
        except (StateError, ExecutionError) as exc:
            problems.append(Problem("error", f"工作表「{sheet.name}」無法查詢：{exc}", sheet.name))
        except SourceConnectionError as exc:
            level = "error" if where == "server" else "warning"
            problems.append(
                Problem(level, f"工作表「{sheet.name}」連不上資料庫：{exc}", sheet.name)
            )
    return problems


def errors(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.level == "error"]


__all__ = ["Problem", "check_report", "errors"]
