"""測試用的舊版 Excel 檔（.xls，BIFF8 放在 OLE2 複合文件中）：沒有寫 .xls 的套件可以用，這裡只寫 xlrd 讀得到的
最少的記錄——工作表（數字與文字儲存格）與活頁簿層級的具名範圍（NAME 記錄，公式是一個 tArea3d）。

格式依 Microsoft 的 [MS-XLS]（BIFF8 的記錄）與 [MS-CFB]（複合文件）。"""

from __future__ import annotations

import struct

Cell = str | float | int | None


def _record(kind: int, payload: bytes) -> bytes:
    return struct.pack("<HH", kind, len(payload)) + payload


def _bof(kind: int) -> bytes:
    # BIFF8（0x0600）；kind：0x0005 活頁簿、0x0010 工作表
    return _record(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0x0DBB, 0x07CC, 0, 0x06))


def _short_text(text: str) -> bytes:
    """1 位元組長度的字串（工作表與名稱用）：一律寫成 UTF-16（旗標 1）。"""
    return struct.pack("<BB", len(text), 1) + text.encode("utf-16-le")


def _sheet(rows: list[list[Cell]]) -> bytes:
    cells = b""
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, str):
                data = struct.pack("<HB", len(value), 1) + value.encode("utf-16-le")
                cells += _record(0x0204, struct.pack("<HHH", r, c, 0) + data)  # LABEL
            else:
                cells += _record(0x0203, struct.pack("<HHHd", r, c, 0, float(value)))  # NUMBER
    width = max((len(row) for row in rows), default=0)
    dimensions = _record(0x0200, struct.pack("<IIHHH", 0, len(rows), 0, width, 0))
    return _bof(0x0010) + dimensions + cells + _record(0x000A, b"")


def biff8(
    sheets: dict[str, list[list[Cell]]], names: dict[str, tuple[int, int, int, int, int]]
) -> bytes:
    """Workbook 資料流：sheets 是工作表名稱 → 各列的值；names 是名稱 →（工作表序號、起始列、結束列、起始欄、
    結束欄），都從 0 起算、含結束的那一列（欄）。"""
    titles = list(sheets)
    supbook = _record(0x01AE, struct.pack("<HH", len(titles), 0x0401))  # 本活頁簿的參照
    refs = struct.pack("<H", len(titles)) + b"".join(
        struct.pack("<HHH", 0, i, i) for i in range(len(titles))
    )
    externsheet = _record(0x0017, refs)
    name_records = b""
    for name, (sheet, r1, r2, c1, c2) in names.items():
        formula = struct.pack("<BHHHHH", 0x3B, sheet, r1, r2, c1, c2)  # tArea3d（絕對參照）
        head = struct.pack("<HBBHHHBBBB", 0, 0, len(name), len(formula), 0, 0, 0, 0, 0, 0)
        name_records += _record(0x0018, head + b"\x01" + name.encode("utf-16-le") + formula)

    def globals_with(offsets: list[int]) -> bytes:
        boundsheets = b"".join(
            _record(0x0085, struct.pack("<IBB", offset, 0, 0) + _short_text(title))
            for offset, title in zip(offsets, titles, strict=True)
        )
        return (
            _bof(0x0005)
            + _record(0x0042, struct.pack("<H", 1200))  # CODEPAGE：UTF-16
            + boundsheets
            + supbook
            + externsheet
            + name_records
            + _record(0x000A, b"")
        )

    bodies = [_sheet(sheets[t]) for t in titles]
    head = globals_with([0] * len(titles))
    offsets, position = [], len(head)
    for body in bodies:
        offsets.append(position)
        position += len(body)
    return globals_with(offsets) + b"".join(bodies)


def xls_bytes(
    sheets: dict[str, list[list[Cell]]],
    names: dict[str, tuple[int, int, int, int, int]] | None = None,
) -> bytes:
    """.xls 檔的內容：BIFF8 的 Workbook 資料流放在 OLE2 複合文件（512 位元組的磁區）中。資料流補到 4096 位元組
    以上，放在一般的磁區（不用迷你資料流）。"""
    stream = biff8(sheets, names or {})
    stream += b"\x00" * (max(4096, -(-len(stream) // 512) * 512) - len(stream))
    count = len(stream) // 512
    end, free, fat_mark = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
    directory_at = 1 + count
    fat = [fat_mark] + [i + 2 for i in range(count - 1)] + [end] + [end]
    fat += [free] * (128 - len(fat))
    header = (
        bytes.fromhex("D0CF11E0A1B11AE1")
        + b"\x00" * 16
        + struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6)
        + b"\x00" * 6
        + struct.pack("<IIIIIIIII", 0, 1, directory_at, 0, 4096, end, 0, end, 0)
        + struct.pack("<I", 0)
        + struct.pack("<I", free) * 108
    )

    def entry(name: str, kind: int, child: int, start: int, size: int) -> bytes:
        encoded = (name + "\x00").encode("utf-16-le") if name else b""
        return (
            encoded.ljust(64, b"\x00")
            + struct.pack("<HBB", len(encoded), kind, 1)
            + struct.pack("<III", free, free, child)
            + b"\x00" * 16
            + struct.pack("<I", 0)
            + b"\x00" * 16
            + struct.pack("<III", start, size, 0)
        )

    directory = (
        entry("Root Entry", 5, 1, end, 0)
        + entry("Workbook", 2, free, 1, len(stream))
        + entry("", 0, free, 0, 0) * 2
    )
    assert len(header) == 512 and len(directory) == 512
    return header + struct.pack("<128I", *fat) + stream + directory
