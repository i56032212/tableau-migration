"""直接送給資料庫的函式（RAWSQL_*、RAWSQLAGG_*，Tableau 的 pass-through functions）。

Tableau 把第 1 個參數的 SQL 原樣送給資料庫，其中的 %1、%2… 換成之後各個參數的 SQL（「Pass-Through
Functions (RAWSQL)」，help.tableau.com/v2021.2/pro/desktop/en-us/functions_functions_passthrough.htm）。
這段 SQL 是作者為那個資料庫寫的，只有在那個資料庫上執行時語意才與 Tableau 相同：

- 工作表的 SQL（DuckDB 方言）中寫成 tmig_rawsql_<型別>_<SQL 的十六進位>(參數…)：SQL 不寫成字串常值，
  SQLGlot 解析 DuckDB 的字串時會處理反斜線，原文不保證不變
- Live 連線轉成來源資料庫的方言時（tmig.sqlgen.dialect），參數先照常轉譯，再代入 SQL 原文；原文不經過轉譯
- 在平台的引擎（DuckDB）計算的部分不能有這種函式：萃取、檔案、跨資料庫的 join，以及 Live 連線中在 DuckDB
  執行的部分（Table Calculation 之後、空間函式）。Tableau 對萃取怎麼處理這些函式沒有文件，一律列為不支援
"""

from __future__ import annotations

import re

from tmig.formula.functions import RAWSQL_SLOT

PREFIX = "tmig_rawsql_"
TYPE_CODES = {"BOOL": "b", "DATE": "d", "DATETIME": "t", "INT": "i", "REAL": "r", "STR": "s"}
CALL = re.compile(r"\b" + PREFIX + r"([bdtirs])_([0-9a-f]*)\s*\(", re.IGNORECASE)
"""SQL 中的直接送給資料庫的函式。"""
NAME = re.compile(PREFIX + r"([bdtirs])_([0-9a-f]*)$", re.IGNORECASE)
RAW = re.compile(r"__tmig_raw_([0-9a-f]+)__")
"""轉譯後代入原文之前的位置（tmig.sqlgen.dialect 的 Rewriter）：原文的十六進位。"""


def function_name(name: str, template: str) -> str:
    """RAWSQL_REAL 等函式與它的 SQL → 工作表 SQL 中的函式名稱。"""
    kind = name.upper().split("_", 1)[1]
    return f"{PREFIX}{TYPE_CODES[kind]}_{template.encode('utf-8').hex()}"


def parse_name(name: str) -> tuple[str, str] | None:
    """工作表 SQL 中的函式名稱 →（型別代碼、SQL 原文）；不是直接送給資料庫的函式時為 None。"""
    found = NAME.match(name)
    if found is None:
        return None
    return found.group(1).lower(), bytes.fromhex(found.group(2)).decode("utf-8")


def substitute(template: str, args: list[str]) -> str:
    """SQL 原文中的 %1、%2… 換成各個參數（已轉成來源資料庫方言的 SQL）。"""

    from tmig.sqlgen.query import QueryError  # tmig.sqlgen.query 經由 expr 用到這裡：執行時才載入

    def swap(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if not 1 <= number <= len(args):
            raise QueryError(
                f"直接送給資料庫的 SQL 用到 %{number}，卻只有 {len(args)} 個參數", unsupported=True
            )
        return args[number - 1]

    return RAWSQL_SLOT.sub(swap, template)


def placeholder(text: str) -> str:
    """代入參數之後的原文 → 轉譯後的 SQL 中的位置（產生 SQL 文字之後才換回原文，見 restore）。"""
    return f"__tmig_raw_{text.encode('utf-8').hex()}__"


def restore(sql: str) -> str:
    """把轉譯後的 SQL 中的位置換回原文（外面加上括號）。參數中又有直接送給資料庫的函式時，換回的原文中還有
    位置：重複到沒有為止。"""
    for _ in range(100):
        replaced = RAW.sub(lambda m: "(" + bytes.fromhex(m.group(1)).decode("utf-8") + ")", sql)
        if replaced == sql:
            break
        sql = replaced
    return sql


def forbid_local(sql: str, where: str) -> None:
    """在平台的引擎（DuckDB）執行的 SQL 不能有直接送給資料庫的函式：列為不支援。"""
    from tmig.sqlgen.query import QueryError

    if CALL.search(sql):
        raise QueryError(
            f"直接送給資料庫的函式（RAWSQL_*、RAWSQLAGG_*）只能在 Live 連線的資料庫上計算；{where}在平台的引擎"
            "計算，Tableau 這時怎麼處理沒有文件，v0 不支援",
            unsupported=True,
        )
