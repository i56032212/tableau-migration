"""合併欄位（Combined Field）：多個維度的值組合成一個維度，成員與 Tableau 相同寫成「值, 值」。

測試資料見 test_query.py。
"""

from __future__ import annotations

from ..parser.builder import instance, ref
from .test_query import Run, db, region_sheet, run

__all__ = ["db", "run"]  # pytest fixtures

GROUP = (
    "<group name='[Category &amp; Region (Combined)]' name-style='unqualified' "
    "user:ui-builder='nest-group'><groupfilter function='crossjoin'>"
    "<groupfilter function='level-members' level='[Category]' />"
    "<groupfilter function='level-members' level='[Region]' />"
    "</groupfilter></group>"
)
COMBINED = instance(
    "[Category & Region (Combined)]", "None", "[none:Category & Region (Combined):nk]"
)


def test_combined_field(run: Run) -> None:
    sheet = region_sheet([COMBINED], rows=ref("none:Category & Region (Combined):nk"))
    rows = run(sheet, ds_extra=GROUP)
    got = {r["f_category_region_combined"]: r["sum(f_sales)"] for r in rows}
    assert got == {
        "Furniture, East": 150,
        "Furniture, South": 80,
        "Furniture, West": 300,
        "Office, North": 60,
        "Office, South": 20,
        "Technology, East": 200,
        "Technology, West": 500,
    }
