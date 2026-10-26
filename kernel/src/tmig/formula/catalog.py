"""公式函式分類：關鍵字、v0 函式集、Table Calculation 與使用者函式。

v0 函式集即 ``tmig.formula.functions`` 中已實作 SQL 對應的函式。功能盤點若發現
其他函式，先在 functions 與 SQL 產生器加入並補測試，這裡會自動納入。
"""

from __future__ import annotations

from tmig.formula.functions import implemented_names

KEYWORDS = frozenset(
    {
        "IF",
        "THEN",
        "ELSEIF",
        "ELSE",
        "END",
        "CASE",
        "WHEN",
        "AND",
        "OR",
        "NOT",
        "IN",
        "TRUE",
        "FALSE",
        "NULL",
        "FIXED",
        "INCLUDE",
        "EXCLUDE",
    }
)
"""語法關鍵字，後面接括號時也不是函式。"""

V0_KEYWORDS = KEYWORDS
"""本平台支援的關鍵字。"""

V0_FUNCTIONS: frozenset[str] = implemented_names()
"""本平台已實作 SQL 對應的函式，定義在 tmig.formula.functions。"""

LOD_KEYWORDS = frozenset({"FIXED", "INCLUDE", "EXCLUDE"})

USER_FUNCTIONS = frozenset(
    {
        "USERNAME",
        "FULLNAME",
        "USERDOMAIN",
        "ISMEMBEROF",
        "ISUSERNAME",
        "ISFULLNAME",
        "USERATTRIBUTE",
        "USERATTRIBUTEINCLUDES",
    }
)
"""使用者函式：出現在計算或資料源篩選中，通常代表列層級權限（RLS）。"""

_TABLE_CALC_NAMES = frozenset(
    {
        "FIRST",
        "LAST",
        "INDEX",
        "LOOKUP",
        "PREVIOUS_VALUE",
        "SIZE",
        "TOTAL",
        "RANK",
        "RANK_DENSE",
        "RANK_MODIFIED",
        "RANK_PERCENTILE",
        "RANK_UNIQUE",
    }
)
_TABLE_CALC_PREFIXES = ("RUNNING_", "WINDOW_", "SCRIPT_", "MODEL_")


def is_table_calc_function(name: str) -> bool:
    upper = name.upper()
    return upper in _TABLE_CALC_NAMES or upper.startswith(_TABLE_CALC_PREFIXES)


def is_user_function(name: str) -> bool:
    return name.upper() in USER_FUNCTIONS


def in_v0(name: str) -> bool:
    return name.upper() in V0_FUNCTIONS
