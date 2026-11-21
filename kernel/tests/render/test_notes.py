"""畫圖的附註的分類（tmig.render.notes）：單純的說明，與「作者要的東西沒有畫出來、算不出來」。"""

from __future__ import annotations

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


def test_taller_rows_for_multi_line_text_are_informational() -> None:
    """設定的列高放不下多行的文字時列高加大：文字都畫出來了，不算「沒有畫出來」（文字中的數字會變，
    以開頭比對）。"""
    note = "設定的列高（24 像素）放不下多行的文字，列高加大到 34 像素（Tableau 的畫法未查證）"
    assert note.startswith(INFORMATIONAL_PREFIXES)
    assert warnings([note, "趨勢線的資料不足，未畫出"]) == ["趨勢線的資料不足，未畫出"]
