"""畫圖的附註的分類（tmig.render.notes）：單純的說明，與「作者要的東西沒有畫出來、算不出來」。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tmig.render.notes import INFORMATIONAL, INFORMATIONAL_PREFIXES, warnings

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tmig"


def test_unlisted_notes_are_warnings() -> None:
    """沒有列為單純說明的附註一律當成「沒有畫出來、算不出來」：忘了分類時會顯示出來，不會被藏起來。"""
    caveat = "預測的估計值以平台的指數平滑計算（與 Tableau 相同的模型），可能與 Tableau 有些微差異"
    missing = "預測：扣掉忽略的最後 1 期之後，資料不到兩期，估計不出來"
    assert caveat in INFORMATIONAL
    assert warnings([caveat]) == []
    assert warnings([caveat, missing, "趨勢線的資料不足，未畫出", missing]) == [
        missing,
        "趨勢線的資料不足，未畫出",
    ]
    assert warnings(["以後新加的附註"]) == ["以後新加的附註"]


def test_informational_notes_match_the_text_the_renderer_writes() -> None:
    """單純的說明以文字比對：每一則都要與產生附註的程式裡的文字一字不差（那邊改了文字，這裡要跟著改，
    否則那一則會被當成「沒有畫出來」顯示給看報表的人）。"""
    written = "\n".join(
        path.read_text(encoding="utf-8")
        for folder in ("render", "planner")
        for path in sorted((SOURCE / folder).glob("*.py"))
        if path.name != "notes.py"
    )
    # 程式裡較長的文字會折成相鄰的字串常值：去掉引號與換行之後比對
    flattened = "".join(written.split()).replace('""', "")
    for note in INFORMATIONAL:
        assert "".join(note.split()) in flattened, note
    for prefix in INFORMATIONAL_PREFIXES:
        assert "".join(prefix.split()) in flattened, prefix


def test_notes_do_not_interpolate_internal_ids() -> None:
    """附註是給使用者看的（檢視器與編輯器都會列出來）：文字裡代入的值不能是內部的代號——參考線的編號
    （line.id）、欄位鍵與輸出欄名（.key、columns[j]）、標記類型與樣式的代號（marks.type、options.style、
    model.code）。要稱呼東西時用 tmig.render.names（膠囊上的寫法、選單上的名稱）。

    這裡檢查產生附註的程式：每一個寫進附註的 f-string 代入的運算式。"""
    forbidden = re.compile(r"(\.(id|key|code|type|style|field)|\bcolumns\[[^\]]*\]|^mark)$")
    found: list[str] = []
    for folder in ("render", "planner"):
        for path in sorted((SOURCE / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                target = (
                    ast.unparse(func.value)
                    if isinstance(func, ast.Attribute) and func.attr in ("append", "extend")
                    else ""
                )
                writes = target.endswith("notes") or (
                    isinstance(func, ast.Name) and func.id == "_note"
                )
                if not writes:
                    continue
                for part in ast.walk(node):
                    if isinstance(part, ast.FormattedValue):
                        expression = ast.unparse(part.value)
                        if forbidden.search(expression):
                            found.append(f"{path.name}:{node.lineno} {{{expression}}}")
    assert found == []


def test_taller_rows_for_multi_line_text_are_informational() -> None:
    """設定的列高放不下多行的文字時列高加大：文字都畫出來了，不算「沒有畫出來」（文字中的數字會變，
    以開頭比對）。"""
    note = "設定的列高（24 像素）放不下多行的文字，列高加大到 34 像素（Tableau 的畫法未查證）"
    assert note.startswith(INFORMATIONAL_PREFIXES)
    assert warnings([note, "趨勢線的資料不足，未畫出"]) == ["趨勢線的資料不足，未畫出"]
