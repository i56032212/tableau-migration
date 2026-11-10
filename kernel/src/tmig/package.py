"""報表套件（.tmigx）：桌面版存檔、之後發布到伺服器用的單一檔案，相當於 Tableau 的 .twbx。

zip 檔，內容：

- ``manifest.json``：格式名稱（tmigx）、格式版本、IR 版本、報表名稱、建立時間、產生的程式版本
- ``report.ir.json``：報表定義（IR），與 golden.ir.json 相同的寫法
- ``data/<資料源 id>.duckdb``：萃取的資料（Live 連線的資料源沒有）
- ``files/<路徑>``：報表用到的圖片（圖片區塊、圖片按鈕），路徑與 IR 中記的相同

開啟時解到報表目錄：golden.ir.json、data/、files/，與 fixtures 的結構相同，查詢服務照常使用。
套件中沒有任何連線的帳號密碼（IR 本來就不記）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from tmig import __version__
from tmig.ir import IR, dumps_ir, has_local_data, iter_zones, save_ir

FORMAT = "tmigx"
FORMAT_VERSION = 1
SUFFIX = ".tmigx"
MANIFEST = "manifest.json"
REPORT = "report.ir.json"


class PackageError(ValueError):
    """報表套件不正確，或報表缺少存成套件需要的檔案。"""


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    size: int
    datasources: list[str]
    """套件中的萃取資料（資料源 id）。"""
    files: list[str]
    """套件中的圖片。"""
    missing_files: list[str]
    """IR 中有、但找不到的圖片（與 Tableau 相同，顯示時是空的）。"""


def image_paths(ir: IR) -> list[str]:
    """報表用到的圖片：圖片區塊、圖片按鈕（含切換按鈕隱藏時的圖片）。"""
    paths: set[str] = set()
    for dashboard in ir.dashboards:
        for zone in iter_zones(dashboard.zones):
            if zone.image:
                paths.add(zone.image)
            if zone.button is not None:
                paths.update(p for p in (zone.button.image, zone.button.image_hidden) if p)
    return sorted(paths)


def member_path(path: str) -> PurePosixPath | None:
    """套件內的相對路徑；絕對路徑、磁碟代號、.. 等會跳出目錄的寫法為 None。"""
    normalized = path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts or normalized.startswith("/") or ":" in parts[0] or ".." in parts:
        return None
    return PurePosixPath(*parts)


def local_file(directory: Path, path: str) -> Path | None:
    """報表目錄 files/ 下的檔案（路徑不安全或不存在時為 None）。"""
    relative = member_path(path)
    if relative is None:
        return None
    target = directory.joinpath("files", *relative.parts)
    return target if target.is_file() else None


def read_report_file(directory: Path, path: str) -> bytes | None:
    """報表目錄中的圖片：先找 files/（開啟的報表套件），再找 .twbx 內（反斜線視為斜線）。"""
    found = local_file(directory, path)
    if found is not None:
        return found.read_bytes()
    wanted = path.replace("\\", "/")
    for package in sorted(directory.glob("*.twbx")):
        with zipfile.ZipFile(package) as archive:
            names = {n.replace("\\", "/"): n for n in archive.namelist()}
            if wanted in names:
                return archive.read(names[wanted])
    return None


def extract_ids(ir: IR) -> list[str]:
    return [ds.id for ds in ir.datasources if has_local_data(ds.connection)]


def write_package(target: Path, ir: IR, directory: Path) -> PackageInfo:
    """把報表目錄存成報表套件；萃取的資料源必須已有 DuckDB 檔（見 tmig.verify.run.ensure_data）。

    先寫到同一目錄的暫存檔再取代，寫到一半失敗不會留下壞掉的套件。
    """
    extracts = extract_ids(ir)
    data = directory / "data"
    missing = [ds for ds in extracts if not (data / f"{ds}.duckdb").is_file()]
    if missing:
        raise PackageError(f"萃取的資料源 {'、'.join(missing)} 沒有資料檔")
    manifest = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "ir_version": ir.ir_version,
        "name": ir.workbook.name,
        "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "generator": f"tmig {__version__}",
    }
    included: list[str] = []
    absent: list[str] = []
    temporary = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            archive.writestr(REPORT, dumps_ir(ir))
            for ds in extracts:
                archive.write(data / f"{ds}.duckdb", f"data/{ds}.duckdb")
            for path in image_paths(ir):
                relative = member_path(path)
                content = read_report_file(directory, path) if relative is not None else None
                if relative is None or content is None:
                    absent.append(path)
                    continue
                archive.writestr(f"files/{relative}", content)
                included.append(path)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return PackageInfo(target, target.stat().st_size, extracts, included, absent)


def read_package(source: Path, directory: Path) -> IR:
    """檢查報表套件並解到報表目錄（golden.ir.json、data/、files/）；不認得的成員略過。"""
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError(f"{source.name} 不是報表套件：{exc}") from exc
    with archive:
        names = set(archive.namelist())
        if MANIFEST not in names or REPORT not in names:
            raise PackageError(f"{source.name} 不是報表套件：缺少 {MANIFEST} 或 {REPORT}")
        try:
            manifest = json.loads(archive.read(MANIFEST))
        except ValueError as exc:
            raise PackageError(f"{source.name} 的 {MANIFEST} 不是 JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise PackageError(f"{source.name} 不是報表套件（格式不是 {FORMAT}）")
        version = manifest.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise PackageError(f"{source.name} 的格式版本不正確：{version!r}")
        if version > FORMAT_VERSION:
            raise PackageError(
                f"{source.name} 的格式版本 {version} 比這個版本能讀的（{FORMAT_VERSION}）新，請更新桌面版"
            )
        try:
            ir = IR.model_validate_json(archive.read(REPORT))
        except ValidationError as exc:
            raise PackageError(
                f"{source.name} 的報表定義不正確：{exc.error_count()} 個錯誤"
            ) from exc
        directory.mkdir(parents=True, exist_ok=True)
        save_ir(ir, directory / "golden.ir.json")
        extracts = set(extract_ids(ir))
        for name in sorted(names):
            if name.endswith("/"):
                continue
            if name.startswith("data/"):
                ds = name.removeprefix("data/").removesuffix(".duckdb")
                if name == f"data/{ds}.duckdb" and ds in extracts:
                    _extract(archive, name, directory / "data" / f"{ds}.duckdb")
            elif name.startswith("files/"):
                relative = member_path(name.removeprefix("files/"))
                if relative is not None:
                    _extract(archive, name, directory.joinpath("files", *relative.parts))
    return ir


def _extract(archive: zipfile.ZipFile, name: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(name) as source, target.open("wb") as sink:
        shutil.copyfileobj(source, sink, 1 << 20)


__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "SUFFIX",
    "PackageError",
    "PackageInfo",
    "image_paths",
    "local_file",
    "member_path",
    "read_package",
    "read_report_file",
    "write_package",
]
