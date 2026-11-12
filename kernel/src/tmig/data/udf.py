"""DuckDB 中以 Python 計算的函式：

- Tableau 的正規表示式：Tableau 以 ICU 執行，DuckDB 以 RE2 執行。RE2 不支援、ICU 支援的語法
  （前後查看、反向參照、原子群組、佔有量詞）改以 Python 的 re 計算（這些語法的寫法與 ICU 相同），
  公式編譯時只有樣式用到這些語法才呼叫（見 tmig.sqlgen.expr）
- 空間函式（MAKEPOINT、MAKELINE、DISTANCE、BUFFER、AREA 與彙總 COLLECT）：空間資料是 WKT 文字，
  計算見 tmig.geo.spatial（WGS84 橢球的大地測量）與 tmig.geo.projection（MAKEPOINT 的 SRID）

同一個 DuckDB 檔的連線共用一個資料庫實體：函式不能重複加入，加入函式的連線關閉後其他連線再用會當掉。
所以用到這些函式的查詢以 attached() 另開自己的記憶體資料庫、唯讀附加資料檔再加入函式；記憶體資料庫
（每次 duckdb.connect() 都是新的）可以直接 register()。"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb

try:
    from duckdb.func import SPECIAL as HANDLES_NULLS  # DuckDB 2
except ImportError:  # pragma: no cover - DuckDB 1.x
    from duckdb.functional import FunctionNullHandling  # type: ignore[import-not-found]

    HANDLES_NULLS = FunctionNullHandling.SPECIAL

MATCH = "tmig_regexp_match"
EXTRACT = "tmig_regexp_extract"
REPLACE = "tmig_regexp_replace"
REGEX_FUNCTIONS = (MATCH, EXTRACT, REPLACE)
GEO_COLLECT = "tmig_geo_collect"
GEO_POINT = "tmig_geo_point"
GEO_POINT_SRID = "tmig_geo_point_srid"
GEO_LINE = "tmig_geo_makeline"
GEO_DISTANCE = "tmig_geo_distance"
GEO_BUFFER = "tmig_geo_buffer"
GEO_AREA = "tmig_geo_area"
SPATIAL_FUNCTIONS = (
    GEO_COLLECT,
    GEO_POINT,
    GEO_POINT_SRID,
    GEO_LINE,
    GEO_DISTANCE,
    GEO_BUFFER,
    GEO_AREA,
)
FUNCTIONS = REGEX_FUNCTIONS + SPATIAL_FUNCTIONS


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


@lru_cache(maxsize=256)
def _replacement(text: str) -> tuple[str | int, ...]:
    """ICU 的取代字串（$1 參照群組、\\ 跳脫下一個字元）→ 文字與群組編號的序列。"""
    parts: list[str | int] = []
    literal: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            literal.append(text[i + 1])
            i += 2
            continue
        if char == "$" and i + 1 < len(text) and text[i + 1].isdigit():
            j = i + 1
            while j < len(text) and text[j].isdigit():
                j += 1
            if literal:
                parts.append("".join(literal))
                literal = []
            parts.append(int(text[i + 1 : j]))
            i = j
            continue
        literal.append(char)
        i += 1
    if literal:
        parts.append("".join(literal))
    return tuple(parts)


def regexp_match(text: str, pattern: str) -> bool:
    """部分符合（與 REGEXP_MATCH 相同）。"""
    return _compiled(pattern).search(text) is not None


def regexp_extract(text: str | None, pattern: str | None, group: int | None) -> str | None:
    """第一個符合處的群組；沒有符合時為 Null，群組沒有參與符合時為空字串（與 RE2 的寫法相同）。"""
    if text is None or pattern is None or group is None:
        return None
    found = _compiled(pattern).search(text)
    if found is None:
        return None
    value = found.group(group)
    return "" if value is None else value


def regexp_replace(text: str, pattern: str, replacement: str) -> str:
    """取代所有符合處；取代字串以 ICU 的寫法（$1 參照群組）。"""
    parts = _replacement(replacement)

    def swap(found: re.Match[str]) -> str:
        return "".join(
            part if isinstance(part, str) else (found.group(part) or "") for part in parts
        )

    return _compiled(pattern).sub(swap, text)


def _safe(func: Callable[..., Any]) -> Callable[..., Any]:
    """空間資料寫法不正確（WKT 無法解析）時為 Null，不讓整個查詢失敗（保留原函式的參數個數，
    DuckDB 依它檢查）。"""

    @functools.wraps(func)
    def wrapped(*args: Any) -> Any:
        from tmig.geo.spatial import SpatialError

        try:
            return func(*args)
        except SpatialError:
            return None

    return wrapped


def _point_srid(x: float, y: float, srid: int) -> str | None:
    from tmig.geo.projection import make_point_srid

    return make_point_srid(x, y, srid)


def register(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """在連線上加入正規表示式與空間函式（Null 的參數得 Null）。"""
    from tmig.geo import spatial

    con.create_function(MATCH, regexp_match, ["VARCHAR", "VARCHAR"], "BOOLEAN")
    # 沒有符合時傳回 Null：Null 的參數自己處理
    con.create_function(
        EXTRACT,
        regexp_extract,
        ["VARCHAR", "VARCHAR", "BIGINT"],
        "VARCHAR",
        null_handling=HANDLES_NULLS,
    )
    con.create_function(REPLACE, regexp_replace, ["VARCHAR", "VARCHAR", "VARCHAR"], "VARCHAR")
    # 空間函式：不是點、不認得的單位或 SRID 等也傳回 Null，Null 的參數由函式自己處理
    spatial_functions: list[tuple[str, Callable[..., Any], list[Any], str]] = [
        (GEO_COLLECT, spatial.collect, ["VARCHAR[]"], "VARCHAR"),
        (GEO_POINT, spatial.make_point, ["DOUBLE", "DOUBLE"], "VARCHAR"),
        (GEO_POINT_SRID, _point_srid, ["DOUBLE", "DOUBLE", "BIGINT"], "VARCHAR"),
        (GEO_LINE, spatial.make_line, ["VARCHAR", "VARCHAR"], "VARCHAR"),
        (GEO_DISTANCE, spatial.distance, ["VARCHAR", "VARCHAR", "VARCHAR"], "DOUBLE"),
        (GEO_BUFFER, spatial.buffer, ["VARCHAR", "DOUBLE", "VARCHAR"], "VARCHAR"),
        (GEO_AREA, spatial.area, ["VARCHAR", "VARCHAR"], "DOUBLE"),
    ]
    for name, func, parameters, result in spatial_functions:
        con.create_function(name, _safe(func), parameters, result, null_handling=HANDLES_NULLS)
    return con


ATTACHED = "data"


def needed(sql: str) -> bool:
    """SQL 用到這些函式（正規表示式只有樣式用到 RE2 不支援的語法時才會；空間資料的查詢）。"""
    lowered = sql.lower()
    return any(name in lowered for name in FUNCTIONS)


def attached(path: str | Path) -> duckdb.DuckDBPyConnection:
    """自己的記憶體資料庫：唯讀附加資料檔（設為預設的資料庫），再加入函式。"""
    con = duckdb.connect()
    try:
        quoted = "'" + str(path).replace("'", "''") + "'"
        con.execute(f'ATTACH {quoted} AS "{ATTACHED}" (READ_ONLY)')
        con.execute(f'USE "{ATTACHED}"')
        register(con)
    except BaseException:
        con.close()
        raise
    return con


__all__ = [
    "EXTRACT",
    "FUNCTIONS",
    "GEO_AREA",
    "GEO_BUFFER",
    "GEO_COLLECT",
    "GEO_DISTANCE",
    "GEO_LINE",
    "GEO_POINT",
    "GEO_POINT_SRID",
    "MATCH",
    "REGEX_FUNCTIONS",
    "REPLACE",
    "SPATIAL_FUNCTIONS",
    "attached",
    "needed",
    "register",
]
