"""Published Data Source（第三階段 N8）：把 .tdsx 的資料源定義併入報表。

報表連到 Published Data Source 時，.twb 只有一個 sqlproxy 連線（指向 Tableau Server）與欄位的快取：
資料源的連線、計算欄位的公式、萃取都在伺服器上。下載資料源的 .tdsx（或 .tds）後，
merge_published 產生一份自含的 .twbx，之後的解析、萃取匯入、計算比對都與嵌入式資料源相同：

- 資料源的內容換成 .tds 的定義（連線、欄位、計算欄位、萃取、物件模型），名稱維持報表中的
  sqlproxy 名稱，工作表的參照不用改
- 報表本地的修改保留：報表自己加的計算欄位、群組、欄位實例；欄位的顯示名稱、角色、隱藏與格式等覆寫
- 報表本地若另外建了萃取（萃取 Published Data Source），保留報表的萃取（資料已在 .twbx 內）
- .tdsx 內的萃取檔（.hyper）放進新的 .twbx：Data/Published/<資料源網址名稱>/<檔名>，連線改指向這裡
- repository-location 保留：解析時記在 IR 的 Datasource.published

.tdsx 依資料源在伺服器上的網址名稱（repository-location 的 id，或 sqlproxy 連線的 dbname）對應：
檔名（不含副檔名）相同，或 .tds 自己的 repository-location id 相同。
"""

from __future__ import annotations

import copy
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from lxml import etree

from tmig.parser.twbx import (
    WorkbookFormatError,
    child,
    children,
    load_workbook_package,
    local_name,
    normalize_flag_attributes,
    text_of,
)

OVERRIDES = ("caption", "role", "type", "hidden", "default-format", "semantic-role", "aggregation")
"""報表對 Published Data Source 欄位的覆寫：顯示名稱、角色、連續／離散、隱藏、格式。"""
LOCAL_CHILDREN = ("column-instance", "group", "drill-paths", "default-sorts", "folder")
"""報表本地另外加的元素（依名稱沒有重複時加入）。"""
DROPPED = ("document-format-change-manifest",)


class PublishedError(ValueError):
    """.tdsx 無法讀取或無法併入。"""


@dataclass
class PublishedRef:
    """報表中連到 Published Data Source 的資料源。"""

    name: str
    """報表中的資料源名稱（sqlproxy.xxx）。"""
    caption: str
    content_url: str
    site: str | None
    server: str | None


@dataclass
class MergedSource:
    ref: PublishedRef
    file: Path
    hyper: str | None
    """新 .twbx 中的萃取檔；Live 連線或下載時不含萃取時為 None。"""
    note: str = ""


@dataclass
class MergeResult:
    out: Path
    merged: list[MergedSource] = field(default_factory=list)
    missing: list[PublishedRef] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def published_refs(root: etree._Element) -> list[PublishedRef]:
    """報表中的 Published Data Source（sqlproxy 連線）。"""
    refs: list[PublishedRef] = []
    for ds in children(child(root, "datasources"), "datasource"):
        conn = child(ds, "connection")
        if conn is None or conn.get("class") != "sqlproxy":
            continue
        location = child(ds, "repository-location")
        content_url = (location.get("id") if location is not None else None) or conn.get(
            "dbname", ""
        )
        name = ds.get("name", "")
        refs.append(
            PublishedRef(
                name=name,
                caption=ds.get("caption") or name,
                content_url=content_url,
                site=(location.get("site") if location is not None else None) or None,
                server=conn.get("server") or None,
            )
        )
    return refs


def workbook_refs(path: str | Path) -> list[PublishedRef]:
    return published_refs(load_workbook_package(path).root)


# ---------------------------------------------------------------- .tdsx


@dataclass
class _Tds:
    path: Path
    root: etree._Element
    hypers: dict[str, bytes | None]
    """.tdsx 內的萃取檔（路徑 → 內容；不讀內容時為 None）。"""


def _read_tds(path: Path, *, data: bool) -> _Tds:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    try:
        if path.suffix.lower() == ".tds":
            root = etree.fromstring(path.read_bytes(), parser)
            normalize_flag_attributes(root)
            return _Tds(path, root, {})
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            tds = [n for n in names if n.lower().endswith(".tds") and not n.startswith("__MACOSX")]
            if len(tds) != 1:
                raise PublishedError(f"{path.name} 內應剛好有一個 .tds（有 {len(tds)} 個）")
            root = etree.fromstring(archive.read(tds[0]), parser)
            normalize_flag_attributes(root)
            hypers = {
                n: (archive.read(n) if data else None)
                for n in names
                if n.lower().endswith(".hyper")
            }
    except (zipfile.BadZipFile, OSError) as exc:
        raise PublishedError(f"{path.name} 無法讀取：{exc}") from exc
    except etree.XMLSyntaxError as exc:
        raise PublishedError(f"{path.name} 的 .tds 不是有效的 XML：{exc}") from exc
    if local_name(root) != "datasource":
        raise PublishedError(f"{path.name} 的根元素不是 <datasource>")
    return _Tds(path, root, hypers)


def find_published(ref: PublishedRef, directory: Path) -> Path | None:
    """目錄中對應的 .tdsx／.tds：檔名相同，或 .tds 的 repository-location id 相同。"""
    if not directory.is_dir():
        return None
    candidates = sorted(
        p for p in directory.iterdir() if p.suffix.lower() in (".tdsx", ".tds") and p.is_file()
    )
    for path in candidates:
        if path.stem == ref.content_url:
            return path
    for path in candidates:
        try:
            tds = _read_tds(path, data=False)
        except PublishedError:
            continue
        location = child(tds.root, "repository-location")
        if location is not None and location.get("id") == ref.content_url:
            return path
    return None


# ---------------------------------------------------------------- 併入


def merge_published(workbook: str | Path, directory: str | Path, out: str | Path) -> MergeResult:
    """把 directory 中的 .tdsx／.tds 併入報表，寫出自含的 .twbx（out）。

    找不到 .tdsx 的資料源維持原狀（解析時仍列為不支援），列在 MergeResult.missing。
    """
    source, folder, target = Path(workbook), Path(directory), Path(out)
    if target.suffix.lower() != ".twbx":
        raise PublishedError("輸出必須是 .twbx（萃取檔要放在報表內）")
    if target.resolve() == source.resolve():
        raise PublishedError("輸出不能覆蓋原本的報表")
    package = load_workbook_package(source)
    root = copy.deepcopy(package.root)
    result = MergeResult(out=target)
    added: dict[str, bytes] = {}
    datasources = child(root, "datasources")
    for ref in published_refs(root):
        found = find_published(ref, folder)
        if found is None:
            result.missing.append(ref)
            continue
        tds = _read_tds(found, data=True)
        element = next(
            ds for ds in children(datasources, "datasource") if ds.get("name") == ref.name
        )
        merged, hyper, note = _merge(element, tds, ref)
        assert datasources is not None
        datasources.replace(element, merged)
        if hyper is not None:
            member, data = hyper
            if data is not None:
                added[member] = data
        result.merged.append(
            MergedSource(ref, found, hyper[0] if hyper is not None else None, note)
        )
    _write(source, package.twb_name, root, added, target)
    return result


def _merge(
    workbook_ds: etree._Element, tds: _Tds, ref: PublishedRef
) -> tuple[etree._Element, tuple[str, bytes | None] | None, str]:
    merged = copy.deepcopy(tds.root)
    for key in ("formatted-name", "source-platform"):
        if key in merged.attrib:
            del merged.attrib[key]
    merged.set("name", ref.name)
    merged.set("caption", ref.caption)
    merged.set("inline", "true")
    for item in list(merged):
        if isinstance(item.tag, str) and local_name(item) in DROPPED:
            merged.remove(item)
    # repository-location 以報表中的為準（指向伺服器上的資料源），沒有時沿用 .tds 的
    local_location = child(workbook_ds, "repository-location")
    existing = child(merged, "repository-location")
    if local_location is not None:
        if existing is not None:
            merged.remove(existing)
        merged.insert(0, copy.deepcopy(local_location))

    # 欄位：.tds 的定義（含計算欄位的公式），再套上報表的覆寫；報表本地的計算欄位加在後面
    columns = {c.get("name"): c for c in children(merged, "column")}
    physical = {
        text_of(child(record, "local-name"))
        for record in merged.iter()
        if isinstance(record.tag, str) and local_name(record) == "metadata-record"
    }
    last = _last(merged, "column")
    for column in children(workbook_ds, "column"):
        name = column.get("name")
        target = columns.get(name)
        if target is None:
            if child(column, "calculation") is not None or name in physical:
                # 報表本地的計算欄位，或報表對實體欄位的覆寫（.tds 沒有這個欄位的 <column>）
                last = _insert_after(merged, last, copy.deepcopy(column))
            continue
        for key in OVERRIDES:
            value = column.get(key)
            if value is not None:
                target.set(key, value)
        aliases = child(column, "aliases")
        if aliases is not None and child(target, "aliases") is None:
            target.append(copy.deepcopy(aliases))
    for kind in LOCAL_CHILDREN:
        names = {c.get("name") for c in children(merged, kind)}
        for item in children(workbook_ds, kind):
            if item.get("name") not in names:
                last = _insert_after(merged, last, copy.deepcopy(item))

    # 萃取：報表本地的萃取優先（資料已在 .twbx 內）；否則用 .tdsx 的萃取或直接連 .hyper 的連線
    local_extract = child(workbook_ds, "extract")
    if local_extract is not None and local_extract.get("enabled") != "false":
        existing_extract = child(merged, "extract")
        if existing_extract is not None:
            merged.replace(existing_extract, copy.deepcopy(local_extract))
        else:
            merged.append(copy.deepcopy(local_extract))
        return merged, None, "沿用報表本地的萃取"
    connections = _hyper_connections(merged)
    if not connections:
        return merged, None, "Live 連線"
    member: str | None = None
    data: bytes | None = None
    for conn in connections:
        path = _locate(conn.get("dbname", ""), tds)
        if path is None and tds.hypers:
            raise PublishedError(f"{tds.path.name} 內找不到萃取檔 {conn.get('dbname')}")
        name = PurePosixPath(path or conn.get("dbname", "").replace("\\", "/")).name
        member = f"Data/Published/{_safe(ref.content_url)}/{name}"
        conn.set("dbname", member)
        if path is not None:
            data = tds.hypers.get(path)
    note = "" if data is not None else "下載時不含萃取檔"
    return merged, (member, data) if member is not None else None, note


def _hyper_connections(ds: etree._Element) -> list[etree._Element]:
    """指向 .hyper 的連線：萃取的連線，或直接以 .hyper 為來源的具名連線。"""
    result: list[etree._Element] = []
    extract = child(ds, "extract")
    if extract is not None and extract.get("enabled") != "false":
        conn = child(extract, "connection")
        if conn is not None and conn.get("class") in ("hyper", "dataengine"):
            result.append(conn)
        return result
    conn = child(ds, "connection")
    if conn is None:
        return result
    if conn.get("class") in ("hyper", "dataengine"):
        return [conn]
    for named in children(child(conn, "named-connections"), "named-connection"):
        for inner in children(named, "connection"):
            if inner.get("class") in ("hyper", "dataengine"):
                result.append(inner)
    return result


def _locate(dbname: str, tds: _Tds) -> str | None:
    """.tdsx 內對應的萃取檔：路徑相同、檔名相同，或只有一個萃取檔。"""
    if not tds.hypers:
        return None
    path = dbname.replace("\\", "/").lstrip("./")
    if path in tds.hypers:
        return path
    base = PurePosixPath(path).name.lower()
    matches = [n for n in tds.hypers if PurePosixPath(n).name.lower() == base]
    if len(matches) == 1:
        return matches[0]
    if len(tds.hypers) == 1:
        return next(iter(tds.hypers))
    return None


def _last(parent: etree._Element, name: str) -> etree._Element | None:
    found = children(parent, name)
    return found[-1] if found else None


def _insert_after(
    parent: etree._Element, anchor: etree._Element | None, item: etree._Element
) -> etree._Element:
    if anchor is None:
        parent.append(item)
    else:
        anchor.addnext(item)
    return item


def _safe(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("._") or "datasource"


def _write(
    source: Path, twb_name: str, root: etree._Element, added: dict[str, bytes], target: Path
) -> None:
    """新的 .twbx：原本的檔案（.twbx 時）、合併後的 .twb 與萃取檔；先寫暫存檔再替換。"""
    data = etree.tostring(root, xml_declaration=True, encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=target.name, suffix=".tmp", delete=False
    ) as handle:
        tmp = Path(handle.name)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            twb = PurePosixPath(twb_name).name if source.suffix.lower() == ".twb" else twb_name
            out.writestr(twb, data)
            if source.suffix.lower() == ".twbx":
                with zipfile.ZipFile(source) as original:
                    for info in original.infolist():
                        if info.is_dir() or info.filename == twb_name or info.filename in added:
                            continue
                        with original.open(info) as src, out.open(info.filename, "w") as dst:
                            shutil.copyfileobj(src, dst)
            for member, content in added.items():
                out.writestr(member, content)
        tmp.replace(target)
    except (OSError, zipfile.BadZipFile, WorkbookFormatError):
        tmp.unlink(missing_ok=True)
        raise


__all__ = [
    "MergeResult",
    "MergedSource",
    "PublishedError",
    "PublishedRef",
    "find_published",
    "merge_published",
    "published_refs",
    "workbook_refs",
]
