"""地理資料：Tableau 地理角色（國家、州／省、縣、城市、郵遞區號）的比對、形狀與底圖。

- names：名稱的正規化（比對前後一致）
- topojson：TopoJSON 的解碼
- gazetteer：地名資料（data/places.parquet、data/names.parquet）的查詢
- geocode：依檢視中的地理欄位找出每個標記的位置（與 Tableau 相同，以最細的地理角色為準，較粗的角色與
  資料源的預設國家、州作為範圍）
- extent：自動縮放的範圍（可能跨越 180 度經線）與麥卡托投影

資料由 tools/geodata/build.py 產生，來源與授權見 NOTICE.md（Natural Earth、GeoNames、美國人口普查局）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
"""地名資料與形狀（TopoJSON）的目錄。"""
SHAPES = DATA / "shapes"
"""形狀：countries-110m／50m／10m.json、admin1/<國家>.json、us-counties.json。檢視器以 /api/geo/ 取得。"""


@lru_cache(maxsize=1)
def version() -> str:
    """形狀檔的版本（data/manifest.json）：檢視器取用形狀的網址含版本，資料更新後不沿用瀏覽器快取。"""
    try:
        return str(json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return "dev"
