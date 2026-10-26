"""讀取 .twb／.twbx，以及走訪 .twb XML 的共用工具。

.twb 的兩個特性需要統一處理：

- 功能旗標標籤：較新版本會把元素寫成 ``_.fcp.<功能>.<true|false>...<標籤>``。
  ``true`` 版本是目前生效的寫法，``false`` 版本是給舊版讀的替代內容，走訪時一律略過。
- 命名空間屬性：``user:ui-enumeration`` 這類屬性在新版帶命名空間、舊版沒有，
  一律以不含命名空間的名稱讀取。
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

MAX_TWB_BYTES = 512 * 1024 * 1024

_FCP = re.compile(r"^_\.fcp\.(?P<feature>[^.]+)\.(?P<state>true|false)\.\.\.(?P<name>.+)$")


class WorkbookFormatError(ValueError):
    """檔案不是可讀取的 .twb／.twbx。"""


@dataclass(frozen=True)
class PackagedFile:
    """.twbx 內除了 .twb 以外的檔案。"""

    name: str
    size: int


@dataclass(frozen=True)
class WorkbookPackage:
    source: Path
    twb_name: str
    root: etree._Element
    files: tuple[PackagedFile, ...] = ()


def load_workbook_package(path: str | Path) -> WorkbookPackage:
    """讀取 .twb 或 .twbx；.twbx 內必須剛好有一個 .twb。"""
    source = Path(path)
    if not source.is_file():
        raise WorkbookFormatError(f"找不到檔案：{source}")
    suffix = source.suffix.lower()
    if suffix == ".twb":
        return WorkbookPackage(source, source.name, _parse(source.read_bytes(), source.name))
    if suffix != ".twbx":
        raise WorkbookFormatError(f"不支援的副檔名「{source.suffix}」，只接受 .twb 與 .twbx")
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise WorkbookFormatError(f"{source.name} 不是有效的 .twbx（zip）檔") from exc
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        twbs = [info for info in members if info.filename.lower().endswith(".twb")]
        if not twbs:
            raise WorkbookFormatError(f"{source.name} 內找不到 .twb")
        if len(twbs) > 1:
            names = "、".join(info.filename for info in twbs)
            raise WorkbookFormatError(f"{source.name} 內有多個 .twb：{names}")
        twb = twbs[0]
        if twb.file_size > MAX_TWB_BYTES:
            raise WorkbookFormatError(f"{twb.filename} 超過 {MAX_TWB_BYTES // 1024 // 1024} MB")
        root = _parse(archive.read(twb), twb.filename)
        files = tuple(
            PackagedFile(info.filename, info.file_size) for info in members if info is not twb
        )
    return WorkbookPackage(source, twb.filename, root, files)


def _parse(data: bytes, name: str) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise WorkbookFormatError(f"{name} 不是有效的 XML：{exc}") from exc
    if local_name(root) != "workbook":
        raise WorkbookFormatError(f"{name} 的根元素不是 <workbook>")
    return root


def _strip(name: str) -> str:
    name = name.rsplit("}", 1)[-1]
    match = _FCP.match(name)
    return match.group("name") if match else name


def local_name(element: etree._Element) -> str:
    """不含命名空間與功能旗標前綴的標籤名稱；註解等非元素節點回傳空字串。"""
    tag = element.tag
    return _strip(tag) if isinstance(tag, str) else ""


def is_active(element: etree._Element) -> bool:
    """是否為生效的元素：排除註解節點，以及功能旗標為 false 的替代內容。"""
    tag = element.tag
    if not isinstance(tag, str):
        return False
    match = _FCP.match(tag.rsplit("}", 1)[-1])
    return not (match and match.group("state") == "false")


def children(element: etree._Element | None, name: str | None = None) -> list[etree._Element]:
    if element is None:
        return []
    return [c for c in element if is_active(c) and (name is None or local_name(c) == name)]


def child(element: etree._Element | None, name: str) -> etree._Element | None:
    found = children(element, name)
    return found[0] if found else None


def descendants(
    element: etree._Element | None, name: str | None = None
) -> Iterator[etree._Element]:
    """依文件順序走訪後代，略過停用的子樹。"""
    if element is None:
        return
    for c in element:
        if not is_active(c):
            continue
        if name is None or local_name(c) == name:
            yield c
        yield from descendants(c, name)


def attr(element: etree._Element, name: str) -> str | None:
    """讀取屬性，忽略命名空間與功能旗標前綴。"""
    value = element.get(name)
    if value is not None:
        return value
    for key, val in element.attrib.items():
        if _strip(str(key)) == name:
            return str(val)
    return None


def text_of(element: etree._Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def xpath(element: etree._Element) -> str:
    """元素在 .twb 中的 XPath，作為 IR 的 source_ref。"""
    return element.getroottree().getpath(element)
