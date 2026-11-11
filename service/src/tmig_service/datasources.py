"""伺服器上的 Published Data Source（與 Tableau Server 相同）：發布 .tdsx、權限、萃取更新，報表共用同一份資料。

    <報表庫>/datasources/<id>/
    ├── datasource.json        名稱、網址名稱、Project、擁有者、權限、每一版的紀錄
    ├── versions/<n>.tdsx      每一版的 .tdsx（發布後不再改動）
    └── current/<n>/           目前的版本：golden.ir.json（只有這個資料源的報表）與 data/<資料源>.duckdb

- 發布：上傳 .tdsx（或 .tds）→ 以 N8 的方式（tmig.parser.published）併入一份只有這個資料源的報表 →
  解析、匯入萃取 → 存成新的版本；同一個網址名稱再發布為新版本（覆蓋）
- 報表連到這個資料源（IR 的 Datasource.published.content_url 相同、萃取取自 .tdsx）時，查詢服務改用這裡的萃取：
  萃取更新一次，每份用到它的報表都看到新的資料
- 權限：Tableau 的資料源能力：Read（檢視）、Connect（連線）、ExportXml（下載）、Write（覆蓋）、Delete（刪除）、
  ChangePermissions（設定權限）；第一次發布時取自 Project 的預設（All Users 可以檢視與連線）
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import secrets
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tmig.access.permissions import Grantee, Rule
from tmig.ir import IR, Datasource, ExtractConnection, save_ir

RECORD = "datasource.json"
CONTENT_URL = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}")
DATASOURCE_CAPABILITIES = {
    "Read": "檢視",
    "Connect": "連線",
    "ExportXml": "下載",
    "Write": "覆蓋",
    "Delete": "刪除",
    "ChangePermissions": "設定權限",
}
KEEP_UNPACKED = 2
DEFAULT_RULES = [
    Rule(
        grantee=Grantee(kind="group", name="All Users"),
        capabilities={"Read": "Allow", "Connect": "Allow"},
    )
]
"""第一次發布時的預設權限（Project 沒有設定時）：與 Tableau 預設的「Default」Project 相同。"""


class DatasourceError(ValueError):
    pass


def now_text() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class DatasourceVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    published_at: str
    publisher: str
    comment: str = ""
    size: int
    sha256: str


class DatasourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    content_url: str = Field(
        description="網址名稱：報表中 Published Data Source 的 repository-location id"
    )
    project: str
    owner: str
    created_at: str
    rules: list[Rule] = Field(default_factory=list)
    datasource: str = Field(description="IR 中資料源的 id")
    connection: str = Field(description="連線的種類（萃取、Live 的資料庫）")
    extract: bool = False
    refreshed_at: str | None = Field(default=None, description="萃取最後更新的時間")
    versions: list[DatasourceVersion] = Field(min_length=1)

    @property
    def latest(self) -> DatasourceVersion:
        return self.versions[-1]


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "", text.replace(" ", ""))
    return cleaned[:64] or "datasource"


def _content_url(tdsx: Path) -> str | None:
    """.tdsx（或 .tds）中資料源自己的 repository-location id。"""
    from lxml import etree

    try:
        if zipfile.is_zipfile(tdsx):
            with zipfile.ZipFile(tdsx) as archive:
                name = next((n for n in archive.namelist() if n.lower().endswith(".tds")), None)
                if name is None:
                    raise DatasourceError(".tdsx 中沒有 .tds")
                xml = archive.read(name)
        else:
            xml = tdsx.read_bytes()
        root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True))
    except (OSError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        raise DatasourceError(f"無法讀取資料源：{exc}") from exc
    if root.tag != "datasource":
        raise DatasourceError("不是 Tableau 的資料源（.tds）")
    location = root.find("repository-location")
    found = location.get("id") if location is not None else None
    if not found:
        return None
    # 網址名稱也是檔名與目錄名稱：只接受 Tableau 的寫法（英數字、底線、連字號、點，不以點開頭）
    if not CONTENT_URL.fullmatch(found):
        raise DatasourceError(f"資料源的網址名稱「{found}」不正確")
    return found


def _synthetic_workbook(content_url: str, caption: str) -> str:
    """只有一個連到 Published Data Source 的資料源的報表（N8 併入 .tdsx 用）。"""
    from xml.sax.saxutils import quoteattr

    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2021.2.7' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences />
  <datasources>
    <datasource caption={quoteattr(caption)} inline='true' name='sqlproxy.tmig' version='18.1'>
      <repository-location id={quoteattr(content_url)} path='/datasources' revision='1.0' />
      <connection channel='https' class='sqlproxy' dbname={quoteattr(content_url)} port='443' server='tmig' username=''>
        <relation name='sqlproxy' table='[sqlproxy]' type='table' />
      </connection>
    </datasource>
  </datasources>
  <worksheets />
  <dashboards />
  <windows />
</workbook>
"""


def read_datasource(tdsx: Path, caption: str, directory: Path) -> tuple[IR, str]:
    """.tdsx → 只有這個資料源的報表（directory 中的 golden.ir.json 與 data/），回傳 IR 與網址名稱。"""
    from tmig.parser.published import merge_published
    from tmig.verify.run import ensure_data, load_fixture_ir

    content_url = _content_url(tdsx) or slugify(tdsx.stem)
    with tempfile.TemporaryDirectory(prefix="tmig-pds-") as work:
        folder = Path(work)
        published = folder / "published"
        published.mkdir()
        suffix = ".tdsx" if zipfile.is_zipfile(tdsx) else ".tds"
        shutil.copyfile(tdsx, published / f"{content_url}{suffix}")
        twb = folder / "source.twb"
        twb.write_text(_synthetic_workbook(content_url, caption), encoding="utf-8")
        directory.mkdir(parents=True, exist_ok=True)
        merged = merge_published(twb, published, directory / "datasource.twbx")
        if not merged.ok:
            raise DatasourceError(f"無法讀取資料源 {content_url}")
    try:
        ir = load_fixture_ir(directory)
    except Exception as exc:
        raise DatasourceError(f"無法解析資料源：{exc}") from exc
    if not ir.datasources:
        raise DatasourceError("資料源中沒有欄位")
    ensure_data(directory, ir)
    save_ir(ir, directory / "golden.ir.json")
    return ir, content_url


def connection_kind(datasource: Datasource) -> str:
    connection = datasource.connection
    if isinstance(connection, ExtractConnection):
        source = connection.source
        return f"萃取（{source.dialect}）" if source is not None else "萃取"
    return str(getattr(connection, "dialect", None) or connection.kind)


class DatasourceStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "datasources"
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[int, DatasourceRecord]] = {}

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / RECORD).is_file())

    def get(self, datasource_id: str) -> DatasourceRecord | None:
        if (
            not datasource_id
            or "/" in datasource_id
            or "\\" in datasource_id
            or datasource_id.startswith(".")
        ):
            return None
        path = self.root / datasource_id / RECORD
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._cache.get(datasource_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            record = DatasourceRecord.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise DatasourceError(f"{path} 不正確：{exc}") from exc
        self._cache[datasource_id] = (stamp, record)
        return record

    def records(self) -> list[DatasourceRecord]:
        return [r for r in (self.get(i) for i in self.ids()) if r is not None]

    def find(self, content_url: str) -> DatasourceRecord | None:
        key = content_url.casefold()
        return next((r for r in self.records() if r.content_url.casefold() == key), None)

    def directory(self, record: DatasourceRecord) -> Path:
        return self.root / record.id / "current" / str(record.latest.version)

    def package(self, record: DatasourceRecord, version: int) -> Path:
        return self.root / record.id / "versions" / f"{version}.tdsx"

    def data_path(self, record: DatasourceRecord) -> Path:
        return self.directory(record) / "data" / f"{record.datasource}.duckdb"

    def ir(self, record: DatasourceRecord) -> IR:
        from tmig.ir import load_ir

        return load_ir(self.directory(record) / "golden.ir.json")

    def publish(
        self,
        upload: Path,
        *,
        name: str,
        project: str,
        owner: str,
        publisher: str,
        comment: str = "",
        rules: list[Rule] | None = None,
    ) -> DatasourceRecord:
        """發布（同一個網址名稱已經有時為新版本）。失敗時不留下任何檔案。"""
        staging = self.root.parent / "staging" / f"pds-{secrets.token_hex(6)}"
        try:
            ir, content_url = read_datasource(upload, name, staging)
            digest = hashlib.sha256(upload.read_bytes()).hexdigest()
            datasource = ir.datasources[0]
            with self._lock:
                current = self.find(content_url)
                record_id = current.id if current is not None else self._new_id(content_url)
                folder = self.root / record_id
                number = (current.latest.version if current is not None else 0) + 1
                (folder / "versions").mkdir(parents=True, exist_ok=True)
                (folder / "current").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(upload, folder / "versions" / f"{number}.tdsx")
                target = folder / "current" / str(number)
                if target.exists():
                    shutil.rmtree(target)
                os.replace(staging, target)
                version = DatasourceVersion(
                    version=number,
                    published_at=now_text(),
                    publisher=publisher,
                    comment=comment,
                    size=upload.stat().st_size,
                    sha256=digest,
                )
                extract = isinstance(datasource.connection, ExtractConnection)
                if current is None:
                    record = DatasourceRecord(
                        id=record_id,
                        name=name,
                        content_url=content_url,
                        project=project,
                        owner=owner,
                        created_at=version.published_at,
                        rules=list(rules) if rules is not None else list(DEFAULT_RULES),
                        datasource=datasource.id,
                        connection=connection_kind(datasource),
                        extract=extract,
                        refreshed_at=version.published_at if extract else None,
                        versions=[version],
                    )
                else:
                    record = current.model_copy(
                        update={
                            "datasource": datasource.id,
                            "connection": connection_kind(datasource),
                            "extract": extract,
                            "refreshed_at": version.published_at if extract else None,
                            "versions": [*current.versions, version],
                        }
                    )
                self._write(record)
                self._prune(folder / "current", number)
                return record
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def update(self, datasource_id: str, changes: dict[str, Any]) -> DatasourceRecord:
        with self._lock:
            current = self.get(datasource_id)
            if current is None:
                raise DatasourceError("找不到資料源")
            record = DatasourceRecord.model_validate({**current.model_dump(), **changes})
            self._write(record)
            return record

    def remove(self, datasource_id: str) -> Path:
        with self._lock:
            folder = self.root / datasource_id
            if self.get(datasource_id) is None:
                raise DatasourceError("找不到資料源")
            trash = self.root.parent / "trash"
            trash.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
            target = trash / f"datasource-{datasource_id}-{stamp}-{secrets.token_hex(2)}"
            os.replace(folder, target)
            self._cache.pop(datasource_id, None)
            return target

    def _new_id(self, content_url: str) -> str:
        base = slugify(content_url).lower()
        candidate, n = base, 1
        while (self.root / candidate).exists():
            n += 1
            candidate = f"{base}_{n}"
        return candidate

    def _write(self, record: DatasourceRecord) -> None:
        path = self.root / record.id / RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{RECORD}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        self._cache.pop(record.id, None)

    @staticmethod
    def _prune(current: Path, latest: int) -> None:
        for folder in current.iterdir():
            if folder.name.isdigit() and int(folder.name) <= latest - KEEP_UNPACKED:
                shutil.rmtree(folder, ignore_errors=True)


def uses_published(datasource: Datasource) -> str | None:
    """報表的資料源連到 Published Data Source、萃取取自 .tdsx 時，回傳網址名稱（改用伺服器上的資料源）。

    報表自己另外建的萃取（萃取 Published Data Source）不改：資料在報表裡。"""
    if datasource.published is None:
        return None
    connection = datasource.connection
    if isinstance(connection, ExtractConnection) and not connection.hyper_file.startswith(
        "Data/Published/"
    ):
        return None
    return datasource.published.content_url


__all__ = [
    "DATASOURCE_CAPABILITIES",
    "DatasourceError",
    "DatasourceRecord",
    "DatasourceStore",
    "read_datasource",
    "uses_published",
]
