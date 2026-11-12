"""地名資料的查詢：比對鍵（正規化的名稱、縮寫、代碼）→ 地點。資料是 data/ 下的 Parquet 檔，以 DuckDB
直接查詢（不整份載入記憶體）。"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import duckdb

from tmig.geo import DATA

ROLES = ("country", "state", "county", "city", "zip")
"""地理角色，由粗到細。"""


@dataclass(frozen=True)
class Place:
    id: str
    role: str
    country: str | None
    """國家（Natural Earth 的 ADM0_A3，例如 USA、TWN）。"""
    admin1: str | None
    """所屬的州、省（地點 id）；州本身為自己。"""
    name: str
    lat: float
    lon: float
    west: float
    south: float
    east: float
    north: float
    shape: str | None
    """形狀檔（data/shapes/ 下，不含副檔名），沒有形狀的為 None。"""
    feature: str | None
    """形狀檔中的 id。"""
    rank: float | None
    """底圖標籤出現的縮放層級（越小越早出現）。"""
    population: int | None = None


COLUMNS = (
    "id, role, country, admin1, name, lat, lon, west, south, east, north, shape, feature, rank, "
    "population"
)


class Gazetteer:
    def __init__(self, directory: Path = DATA) -> None:
        self.directory = directory
        self._con: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if self._con is None:
                con = duckdb.connect()
                places = (self.directory / "places.parquet").as_posix().replace("'", "''")
                names = (self.directory / "names.parquet").as_posix().replace("'", "''")
                con.execute(f"CREATE VIEW places AS SELECT * FROM read_parquet('{places}')")
                con.execute(f"CREATE VIEW names AS SELECT * FROM read_parquet('{names}')")
                self._con = con
            return self._con.cursor()

    def candidates(self, role: str, keys: Iterable[str]) -> dict[str, list[tuple[int, Place]]]:
        """比對鍵 → 這個角色的地點與比對的優先順序（0 名稱、1 代碼與縮寫、2 別名與其他語言的名稱），
        依優先順序、人口由多到少排列。"""
        wanted = sorted({k for k in keys if k})
        if not wanted:
            return {}
        found: dict[str, list[tuple[int, Place]]] = {}
        columns = ", ".join("p." + c.strip() for c in COLUMNS.split(","))
        cursor = self._cursor()
        try:
            rows = cursor.execute(
                f"SELECT n.norm, min(n.priority) AS priority, {columns} "
                "FROM names n JOIN places p ON p.id = n.id "
                "WHERE n.norm IN (SELECT unnest(?)) AND p.role = ? "
                f"GROUP BY n.norm, {columns} "
                "ORDER BY n.norm, priority, p.population DESC NULLS LAST, p.id",
                [wanted, role],
            ).fetchall()
        finally:
            cursor.close()
        for key, priority, *values in rows:
            found.setdefault(key, []).append((int(priority), Place(*values)))
        return found

    def place(self, place_id: str) -> Place | None:
        cursor = self._cursor()
        try:
            row = cursor.execute(
                f"SELECT {COLUMNS} FROM places WHERE id = ?", [place_id]
            ).fetchone()
        finally:
            cursor.close()
        return Place(*row) if row else None

    def within(
        self,
        role: str,
        west: float,
        south: float,
        east: float,
        north: float,
        max_rank: float,
        limit: int = 200,
        countries: Iterable[str] | None = None,
    ) -> list[Place]:
        """底圖的標籤：範圍內、出現層級不超過 max_rank 的地點（重要的在前）。"""
        query = (
            f"SELECT {COLUMNS} FROM places WHERE role = ? AND rank IS NOT NULL AND rank <= ? "
            "AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        )
        params: list[object] = [role, max_rank, south, north, west, east]
        chosen = sorted(set(countries or []))
        if chosen:
            query += " AND country IN (SELECT unnest(?))"
            params.append(chosen)
        query += " ORDER BY rank, population DESC NULLS LAST, name LIMIT ?"
        params.append(limit)
        cursor = self._cursor()
        try:
            rows = cursor.execute(query, params).fetchall()
        finally:
            cursor.close()
        return [Place(*row) for row in rows]

    def overlapping(
        self, role: str, boxes: Iterable[tuple[float, float, float, float]]
    ) -> list[Place]:
        """範圍（west, south, east, north，經度在 ±180 之內、west ≤ east）與任一 box 重疊的地點
        （例如畫面中的國家：畫出它們的州界）。"""
        cursor = self._cursor()
        found: dict[str, Place] = {}
        try:
            for west, south, east, north in boxes:
                # 地點本身跨越 180 度經線時 west > east：分成兩段比對
                rows = cursor.execute(
                    f"SELECT {COLUMNS} FROM places WHERE role = ? AND south <= ? AND north >= ? "
                    "AND ((west <= east AND west <= ? AND east >= ?) "
                    "OR (west > east AND (west <= ? OR east >= ?)))",
                    [role, north, south, east, west, east, west],
                ).fetchall()
                for row in rows:
                    place = Place(*row)
                    found.setdefault(place.id, place)
        finally:
            cursor.close()
        return sorted(found.values(), key=lambda p: p.id)


@lru_cache(maxsize=1)
def gazetteer() -> Gazetteer:
    """共用的地名資料（每個程序一份）。"""
    return Gazetteer()
