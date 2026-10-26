"""公式函式目錄：v0 函式集與各類函式的分類。

v0 函式集見 Spec「公式引擎、查詢規劃與 SQL 產生」。功能盤點若發現其他函式，
先在這裡加入、再實作 SQL 對應，並同步更新 Spec。
"""

from __future__ import annotations

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

V0_KEYWORDS = KEYWORDS - {"IN"}
"""v0 支援的關鍵字；IN 運算子尚未納入。"""

V0_FUNCTIONS = frozenset(
    {
        # 邏輯
        "IIF",
        "ZN",
        "IFNULL",
        "ISNULL",
        # 彙總
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "COUNT",
        "COUNTD",
        # 日期
        "DATEPART",
        "DATETRUNC",
        "DATEADD",
        "DATEDIFF",
        "YEAR",
        "MONTH",
        # 字串
        "LEFT",
        "RIGHT",
        "MID",
        "LEN",
        "UPPER",
        "LOWER",
        "CONTAINS",
        # 型別轉換
        "INT",
        "FLOAT",
        "STR",
        "DATE",
        # Table Calculation
        "RUNNING_SUM",
        "WINDOW_SUM",
        "TOTAL",
        "INDEX",
        "RANK",
    }
)

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
