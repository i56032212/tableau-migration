"""使用紀錄（第三階段 R1）：查詢服務記錄每次檢視的使用者、報表與時間，供報表管理與廢棄判定（不做完整稽核）。

- 檢視器每開啟一個 dashboard、工作表或 Story 送出一次檢視（切換篩選不算新的檢視，與 Tableau 的檢視次數相同）；
  使用者取自登入者的身分，不取自請求內容；沒有權限的檢視不記錄
- 紀錄逐行寫入 <目錄>/<年-月>.jsonl（UTC 時間、使用者、報表、view），不記錄 IP 等其他資訊
- 統計：每份報表與 view 的檢視次數、不重複使用者數與最後一次檢視的時間；期間內沒有檢視的報表也列出（廢棄候選）
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ViewKind = Literal["dashboard", "worksheet", "story"]


@dataclass(frozen=True)
class Visit:
    time: dt.datetime
    user: str | None
    workbook: str
    workbook_name: str
    view: str
    view_id: str
    kind: ViewKind

    def to_json(self) -> str:
        return json.dumps(
            {
                "time": self.time.astimezone(dt.UTC).isoformat(),
                "user": self.user,
                "workbook": self.workbook,
                "workbook_name": self.workbook_name,
                "view": self.view,
                "view_id": self.view_id,
                "kind": self.kind,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> Visit | None:
        try:
            data = json.loads(line)
            return cls(
                time=dt.datetime.fromisoformat(data["time"]),
                user=data.get("user"),
                workbook=str(data["workbook"]),
                workbook_name=str(data.get("workbook_name") or data["workbook"]),
                view=str(data.get("view") or ""),
                view_id=str(data.get("view_id") or ""),
                kind=data.get("kind") or "dashboard",
            )
        except (ValueError, KeyError, TypeError):
            return None


class UsageLog:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()

    def record(self, visit: Visit) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{visit.time.astimezone(dt.UTC):%Y-%m}.jsonl"
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(visit.to_json() + "\n")

    def visits(self, since: dt.datetime | None = None) -> Iterator[Visit]:
        if not self.directory.is_dir():
            return
        first = f"{since.astimezone(dt.UTC):%Y-%m}" if since is not None else ""
        for path in sorted(self.directory.glob("*.jsonl")):
            if path.stem < first:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                visit = Visit.from_json(line)
                if visit is not None and (since is None or visit.time >= since):
                    yield visit


@dataclass
class ViewUsage:
    view: str
    kind: str
    visits: int = 0
    users: set[str] = field(default_factory=set)
    last: dt.datetime | None = None

    def add(self, visit: Visit) -> None:
        self.visits += 1
        if visit.user:
            self.users.add(visit.user)
        if self.last is None or visit.time > self.last:
            self.last = visit.time

    def to_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "kind": self.kind,
            "visits": self.visits,
            "users": len(self.users),
            "last": self.last.isoformat() if self.last else None,
        }


@dataclass
class WorkbookUsage(ViewUsage):
    name: str = ""
    views: dict[str, ViewUsage] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workbook": self.view,
            "name": self.name,
            "visits": self.visits,
            "users": len(self.users),
            "last": self.last.isoformat() if self.last else None,
            "views": [v.to_dict() for v in sorted(self.views.values(), key=lambda v: -v.visits)],
        }


def summarize(
    visits: Iterable[Visit], workbooks: dict[str, str] | None = None
) -> list[WorkbookUsage]:
    """每份報表的使用量，檢視次數多的在前；workbooks（id → 名稱）中沒有檢視的報表也列出（次數 0）。"""
    result: dict[str, WorkbookUsage] = {
        wb: WorkbookUsage(view=wb, kind="workbook", name=name)
        for wb, name in (workbooks or {}).items()
    }
    for visit in visits:
        usage = result.setdefault(
            visit.workbook,
            WorkbookUsage(view=visit.workbook, kind="workbook", name=visit.workbook_name),
        )
        usage.add(visit)
        view = usage.views.setdefault(visit.view_id, ViewUsage(view=visit.view, kind=visit.kind))
        view.add(visit)
    return sorted(result.values(), key=lambda u: (-u.visits, u.view))


__all__ = ["UsageLog", "Visit", "WorkbookUsage", "summarize"]
