"""自然排序：字串中的每一段數字當成一個數值比較（`Cha-2` 在 `Cha-10` 之前），其餘逐字比較。

Tableau 的維度沒有另外設定排序時是「資料源順序」，關聯式資料源（含萃取）通常是自然排序；排序對話框的
「字母順序」才是逐字比較、19 在 2 之前（Tableau 的說明：Sort Data in a Visualization，
https://help.tableau.com/current/pro/desktop/en-us/sortgroup_sorting_computed_howto.htm）。
Snow Sports 報表的畫面也是如此：同一天的交易編號 `…-Cha-0` 到 `…-Cha-40` 依 0、2、4、6、8、10 排列。

做法：把每一段數字補零到固定的位數，再依字串比較；SQL（DuckDB）與 Python 用同一個規則，結果一致。
超過 DIGITS 位的數字只比較最後 DIGITS 位（實際的編號不會這麼長）。只認 ASCII 的數字。
"""

from __future__ import annotations

import re

DIGITS = 20
"""每一段數字補零後的位數。"""

_RUN = re.compile(r"[0-9]+")


def natural_key(text: str) -> str:
    """自然排序的鍵：依這個字串比較就是自然排序。"""
    return _RUN.sub(lambda m: m.group().zfill(DIGITS)[-DIGITS:], text)


def natural_sql(column: str) -> str:
    """自然排序的鍵的 SQL（DuckDB 方言；column 是字串欄位的 SQL）：先在每一段數字前面補上 DIGITS 個 0，
    再把每一段數字裁成最後 DIGITS 位。只用在 DuckDB 執行的部分（輸出的排序與 Table Calculation 的順序），
    不下推到來源資料庫。"""
    padded = f"regexp_replace({column}, '([0-9]+)', '{'0' * DIGITS}\\1', 'g')"
    return f"regexp_replace({padded}, '[0-9]*([0-9]{{{DIGITS}}})', '\\1', 'g')"


__all__ = ["DIGITS", "natural_key", "natural_sql"]
