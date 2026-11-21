"""畫圖的附註（RenderResult.notes、查詢服務 /render 回應的 notes）分成兩種。

- 作者要的東西沒有畫出來、算不出來，或畫得與設定的不同（「趨勢線的資料不足，未畫出」「預測：…估計不出來」
  「甘特條…以長條呈現」）：看報表的人要知道圖上為什麼少了東西——檢視器在工作表上標示，點了列出原因
- 單純的說明（計算的方式與 Tableau 可能有些微差異、畫法上的選擇）：圖上沒有少東西。編輯器列出來給作者看，
  檢視器不顯示（每張有預測的圖都會有，只是雜訊）

分法：下面列出的是單純的說明，**沒有列出的一律當成第一種**——新加的附註忘了分類時會顯示出來（多一則訊息），
不會把「沒有畫出來」藏起來。文字與產生附註的地方（tmig.render.vegalite）相同；那邊改了文字而這裡沒有跟著
改時，那一則會變成第一種，kernel/tests/render/test_notes.py 會指出來。
"""

from __future__ import annotations

from collections.abc import Iterable

INFORMATIONAL: frozenset[str] = frozenset(
    {
        "叢集以平台的 k-means 計算（與 Tableau 相同的做法），分群的結果與編號可能與 Tableau 不同",
        "預測的估計值以平台的指數平滑計算（與 Tableau 相同的模型），可能與 Tableau 有些微差異",
        "Measure Values 在文字上、欄列架上沒有 Measure Names：各量值分開排列，不疊在同一格",
    }
)
"""單純的說明：圖上沒有少東西。"""
INFORMATIONAL_PREFIXES: tuple[str, ...] = ("設定的列高（",)
"""文字中帶著數字的單純說明，以開頭比對：設定的列高放不下多行的文字時列高加大（「設定的列高（24 像素）
放不下多行的文字，列高加大到 34 像素…」）——文字都畫出來了，只是列比設定的高。"""


def warnings(notes: Iterable[str]) -> list[str]:
    """附註中「作者要的東西沒有畫出來、算不出來或畫得不同」的那些（依原本的順序，不重複）。"""
    return list(
        dict.fromkeys(
            note
            for note in notes
            if note not in INFORMATIONAL and not note.startswith(INFORMATIONAL_PREFIXES)
        )
    )


__all__ = ["INFORMATIONAL", "INFORMATIONAL_PREFIXES", "warnings"]
