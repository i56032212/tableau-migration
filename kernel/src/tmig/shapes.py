"""自訂的圖片形狀（Tableau 的自訂形狀調色盤，My Tableau Repository\\Shapes\\<調色盤>\\<檔名>）。

Tableau 把用到的圖片形狀存在活頁簿裡（依 Tableau 公布的 .twb 結構與公開報表）::

    <workbook>
      …
      <external>
        <shapes>
          <shape name='My Shapes/check_box.png'>iVBORw0KGgo…（base64）</shape>
          <shape name=':custom/batting.png'>…</shape>
        </shapes>
      </external>
    </workbook>

標記卡上固定的形狀寫在窗格的樣式（``<format attr='shape' value='My Shapes/check_box.png' />``），依欄位的
形狀寫在形狀編碼（``<map to='Healthcare/logo.png'>``）。

IR 中以報表 files/ 下的路徑表示：``Shapes/<調色盤>/<檔名>``（名稱開頭的冒號去掉）。圖片的來源依序是報表目錄的
files/（報表套件解開的、在編輯器加入的）與目錄中 .twbx／.twb 內嵌的圖片，與 dashboard 的圖片相同在用到時才讀。
"""

from __future__ import annotations

import base64
import binascii
import functools
import io
import zipfile
from pathlib import Path

from lxml import etree

from tmig.ir.models import SHAPE_FOLDER, SHAPE_IMAGE_SUFFIXES, shape_image_error

MAX_SHAPE_BYTES = 4 * 1024 * 1024
"""一張形狀圖片的大小上限（Tableau 建議 32×32 像素左右；太大的圖片不讀）。"""


def shape_path(name: str) -> str | None:
    """Tableau 的形狀名稱（例如 ``Arrows/1-1.png``、``:custom/batting.png``）→ IR 中的圖片路徑；
    內建的形狀（circle、:filled/circle）或不能當作圖片的名稱回傳 None。"""
    cleaned = name.strip().replace("\\", "/").lstrip(":")
    if not cleaned or not cleaned.lower().endswith(SHAPE_IMAGE_SUFFIXES):
        return None
    path = f"{SHAPE_FOLDER}/{cleaned}"
    return None if shape_image_error(path) is not None else path


def shape_names(root: etree._Element) -> dict[str, etree._Element]:
    """活頁簿內嵌的圖片形狀：IR 中的圖片路徑 → <shape> 元素。"""
    found: dict[str, etree._Element] = {}
    for external in root.iterchildren("external"):
        for shapes in external.iterchildren("shapes"):
            for shape in shapes.iterchildren("shape"):
                path = shape_path(shape.get("name") or "")
                if path is not None:
                    found.setdefault(path, shape)
    return found


def _decode(text: str | None) -> bytes | None:
    try:
        data = base64.b64decode("".join((text or "").split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if 0 < len(data) <= MAX_SHAPE_BYTES else None


@functools.lru_cache(maxsize=8)
def _embedded(source: str, stamp: tuple[int, int]) -> dict[str, bytes]:
    """一個 .twb／.twbx 內嵌的圖片形狀（stamp 為修改時間與大小：檔案換掉後重新讀取）。"""
    del stamp
    path = Path(source)
    try:
        if path.suffix.lower() == ".twbx":
            with zipfile.ZipFile(path) as archive:
                name = next(
                    (
                        n
                        for n in archive.namelist()
                        if n.lower().endswith(".twb")
                        and not n.startswith("__MACOSX/")
                        and not Path(n).name.startswith("._")
                    ),
                    None,
                )
                if name is None:
                    return {}
                data = archive.read(name)
        else:
            data = path.read_bytes()
    except (OSError, zipfile.BadZipFile):
        return {}
    found: dict[str, bytes] = {}
    try:
        # 只取 <shape>（圖片讀出後就清掉內容，base64 的文字不會留在記憶體）
        for _, element in etree.iterparse(
            io.BytesIO(data), tag="shape", resolve_entities=False, no_network=True, huge_tree=True
        ):
            parent = element.getparent()
            if parent is not None and parent.tag == "shapes":
                path_in_ir = shape_path(element.get("name") or "")
                content = _decode(element.text) if path_in_ir is not None else None
                if path_in_ir is not None and content is not None:
                    found.setdefault(path_in_ir, content)
            element.clear()
    except etree.XMLSyntaxError:
        return found
    return found


def embedded_shapes(directory: Path) -> dict[str, bytes]:
    """報表目錄中的 .twbx／.twb 內嵌的圖片形狀：IR 中的圖片路徑 → 圖片內容。"""
    found: dict[str, bytes] = {}
    for source in [*sorted(directory.glob("*.twbx")), *sorted(directory.glob("*.twb"))]:
        try:
            stat = source.stat()
        except OSError:
            continue
        for path, content in _embedded(str(source), (stat.st_mtime_ns, stat.st_size)).items():
            found.setdefault(path, content)
    return found


def read_shape(directory: Path, path: str) -> bytes | None:
    """報表目錄中 .twbx／.twb 內嵌的圖片形狀（files/ 下的由 tmig.package.read_report_file 先找）。"""
    if not path.startswith(f"{SHAPE_FOLDER}/") or shape_image_error(path) is not None:
        return None
    return embedded_shapes(directory).get(path)


def palettes(directory: Path) -> dict[str, list[str]]:
    """報表可以用的自訂形狀：調色盤（Shapes/ 下的資料夾名稱；直接放在 Shapes/ 下的歸在空字串）→ 圖片路徑，
    依名稱排序。來源是報表的 files/Shapes/ 與 .twbx／.twb 內嵌的圖片。"""
    paths: set[str] = set(embedded_shapes(directory))
    root = directory / "files" / SHAPE_FOLDER
    if root.is_dir():
        for file in root.rglob("*"):
            if file.is_file():
                path = f"{SHAPE_FOLDER}/{file.relative_to(root).as_posix()}"
                if shape_image_error(path) is None:
                    paths.add(path)
    found: dict[str, list[str]] = {}
    for path in sorted(paths, key=str.casefold):
        parts = path.split("/")
        found.setdefault("/".join(parts[1:-1]), []).append(path)
    return found


__all__ = [
    "MAX_SHAPE_BYTES",
    "embedded_shapes",
    "palettes",
    "read_shape",
    "shape_names",
    "shape_path",
]
