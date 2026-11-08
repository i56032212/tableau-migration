"""報表庫（D0）：發布到伺服器的報表與每一版。

    <報表庫>/
    ├── reports/<報表 id>/
    │   ├── report.json          報表：名稱、Project、擁有者、權限、每一版的紀錄
    │   ├── versions/<n>.tmigx   每一版的報表套件，發布後不再改動
    │   └── current/<n>/         目前的版本解開的報表目錄（golden.ir.json、data/、files/），檢視與查詢讀這裡
    ├── uploads/                 上傳中的套件
    ├── staging/                 解開檢查中的套件
    └── desktop-tokens.json      桌面版登入的權杖（只存雜湊值，見 tmig_service.desktop_auth）

發布：上傳的套件先解到 staging/ 並檢查，通過後才配給版本號、搬進 versions/ 與 current/，最後才改
report.json（先寫暫存檔再取代）；寫到一半失敗不會留下壞掉的版本。回復舊版也是發布一個新版本，
舊的版本不會被改動或刪除。報表庫用的資料庫還沒決定之前（規格待確認事項第 12 項）存在這個資料夾。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import shutil
import threading
import zipfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig.access.permissions import Rule
from tmig.ir import IR
from tmig.package import REPORT, PackageError, read_package
from tmig.parser.ids import slugify

STORE_ENV = "TMIG_STORE"
RECORD = "report.json"
KEEP_UNPACKED = 2
"""解開的版本保留最近兩版：新版本發布時，正在讀前一版的請求不受影響。"""


class StoreError(ValueError):
    """報表庫的資料不正確（例如 report.json 壞掉）。"""


class VersionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    published_at: str
    publisher: str
    comment: str = ""
    size: int
    sha256: str
    restored_from: int | None = Field(default=None, description="回復舊版時，回復的是第幾版")


class ReportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    project: str
    owner: str
    created_at: str
    tableau_workbook: str | None = Field(
        default=None, description="對應的 Tableau workbook id：沿用它在 Tableau Server 上的權限"
    )
    rules: list[Rule] = Field(
        default_factory=list,
        description="報表的權限：第一次發布時取自 Project 的 workbook 預設權限（與 Tableau 相同）",
    )
    versions: list[VersionRecord] = Field(min_length=1)

    @property
    def latest(self) -> VersionRecord:
        return self.versions[-1]

    def version(self, number: int) -> VersionRecord | None:
        return next((v for v in self.versions if v.version == number), None)


class ConflictError(RuntimeError):
    """檢查之後又有人發布了新版本，或要建立的報表已經存在。"""

    def __init__(self, record: ReportRecord | None) -> None:
        super().__init__("報表在發布期間有了新的版本")
        self.record = record


@dataclass
class Staged:
    """解開並通過格式檢查的套件，還沒有版本號。"""

    package: Path
    directory: Path
    ir: IR
    size: int
    sha256: str


def now_text() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _plain(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


class ReportStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.reports = root / "reports"
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[int, ReportRecord]] = {}

    # ------------------------------------------------------------ 讀取

    def ids(self) -> list[str]:
        if not self.reports.is_dir():
            return []
        return sorted(p.name for p in self.reports.iterdir() if (p / RECORD).is_file())

    def get(self, report_id: str) -> ReportRecord | None:
        if not _plain(report_id):
            return None
        path = self.reports / report_id / RECORD
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._cache.get(report_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            record = ReportRecord.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise StoreError(f"報表庫中 {report_id} 的 {RECORD} 不正確：{exc}") from exc
        self._cache[report_id] = (stamp, record)
        return record

    def records(self) -> list[ReportRecord]:
        return [r for r in (self.get(i) for i in self.ids()) if r is not None]

    def find(self, project: str, name: str) -> ReportRecord | None:
        """同一個 Project 中同名的報表（名稱不分大小寫，與 Tableau 相同）。"""
        wanted = name.casefold()
        return next(
            (r for r in self.records() if r.project == project and r.name.casefold() == wanted),
            None,
        )

    def directory(self, record: ReportRecord) -> Path:
        """目前的版本解開的報表目錄。"""
        return self.reports / record.id / "current" / str(record.latest.version)

    def package(self, record: ReportRecord, version: int) -> Path:
        return self.reports / record.id / "versions" / f"{version}.tmigx"

    def read_ir(self, record: ReportRecord, version: int) -> IR:
        """某一版的報表定義（只讀套件中的 report.ir.json，不解開資料）。"""
        with zipfile.ZipFile(self.package(record, version)) as archive:
            return IR.model_validate_json(archive.read(REPORT))

    # ------------------------------------------------------------ 發布

    def upload_path(self) -> Path:
        uploads = self.root / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        return uploads / f"{secrets.token_hex(8)}.tmigx"

    def stage(self, package: Path) -> Staged:
        """解開上傳的套件（檢查格式、版本與成員路徑）；失敗時拋出 PackageError 並清掉解開的檔案。"""
        staging = self.root / "staging" / secrets.token_hex(8)
        try:
            ir = read_package(package, staging)
        except PackageError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        digest = hashlib.sha256()
        with package.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        return Staged(package, staging, ir, package.stat().st_size, digest.hexdigest())

    def discard(self, staged: Staged) -> None:
        shutil.rmtree(staged.directory, ignore_errors=True)
        staged.package.unlink(missing_ok=True)

    def new_id(self, name: str, taken: Collection[str] = ()) -> str:
        """新報表的 id（網址中的名稱）：取自報表名稱，重複時加序號；中文名稱為 report、report_2…。"""
        base = slugify(name)[:48].strip("_") or "report"
        candidate, n = base, 1
        while candidate in taken or (self.reports / candidate).exists():
            n += 1
            candidate = f"{base}_{n}"
        return candidate

    def commit(
        self,
        staged: Staged,
        *,
        report_id: str,
        name: str,
        project: str,
        owner: str,
        publisher: str,
        comment: str,
        expected: int | None,
        rules: Sequence[Rule] = (),
        tableau_workbook: str | None = None,
        restored_from: int | None = None,
    ) -> ReportRecord:
        """把檢查過的套件存成新的版本。expected：檢查時的最新版本（新報表為 None）；
        不符（之後又有人發布了，或報表已經存在）時拋出 ConflictError，套件不動。"""
        with self._lock:
            current = self.get(report_id)
            latest = current.latest.version if current is not None else None
            if latest != expected:
                raise ConflictError(current)
            number = (latest or 0) + 1
            folder = self.reports / report_id
            (folder / "versions").mkdir(parents=True, exist_ok=True)
            (folder / "current").mkdir(parents=True, exist_ok=True)
            os.replace(staged.package, folder / "versions" / f"{number}.tmigx")
            unpacked = folder / "current" / str(number)
            if unpacked.exists():  # 上次寫到一半失敗留下的
                shutil.rmtree(unpacked)
            os.replace(staged.directory, unpacked)
            version = VersionRecord(
                version=number,
                published_at=now_text(),
                publisher=publisher,
                comment=comment,
                size=staged.size,
                sha256=staged.sha256,
                restored_from=restored_from,
            )
            if current is None:
                record = ReportRecord(
                    id=report_id,
                    name=name,
                    project=project,
                    owner=owner,
                    created_at=version.published_at,
                    tableau_workbook=tableau_workbook,
                    rules=list(rules),
                    versions=[version],
                )
            else:
                record = current.model_copy(update={"versions": [*current.versions, version]})
            self._write(record)
            self._prune(folder / "current", number)
            return record

    def _write(self, record: ReportRecord) -> None:
        path = self.reports / record.id / RECORD
        temporary = path.with_name(f"{RECORD}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        self._cache.pop(record.id, None)

    @staticmethod
    def _prune(current: Path, latest: int) -> None:
        for folder in current.iterdir():
            if folder.name.isdigit() and int(folder.name) <= latest - KEEP_UNPACKED:
                shutil.rmtree(folder, ignore_errors=True)


__all__ = [
    "STORE_ENV",
    "ConflictError",
    "ReportRecord",
    "ReportStore",
    "Staged",
    "StoreError",
    "VersionRecord",
]
