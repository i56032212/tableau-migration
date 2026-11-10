"""窗格（小倍數）：與量值的座標軸同一架的離散維度把圖分成窗格，每個窗格只畫自己的資料、共用刻度。

例如列為 Region、SUM(Sales)，欄為連續的月份時，每個地區一張折線圖由上往下排，只有最下面一張畫橫軸的刻度。
測試資料見 test_text.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..parser.builder import ref, worksheet
from .test_formats import MONTH
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, parse, render
from .test_trends import scatter, trendline

__all__ = ["db"]  # pytest fixture


def filters(chart: dict[str, Any]) -> list[str]:
    return [t["filter"] for t in chart.get("transform", []) if "filter" in t]


def test_rows_split_into_panes(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "Panes",
        [REGION, MONTH, SALES],
        rows=f"({ref('none:Region:nk')} / {ref('sum:Sales:qk')})",
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
    )
    spec = render(parse(tmp_path, sheet), db).spec
    panes = spec["vconcat"]
    # 每個地區一個窗格（依資料的順序），篩選自己的地區，標題寫在左側；共用刻度
    titles = [p["title"]["text"] for p in panes]
    assert titles == ["East", "North", "South", "West"]
    assert all(p["title"]["orient"] == "left" for p in panes)
    assert [filters(p) for p in panes] == [[f'datum["c0"] === "{t}"'] for t in titles]
    assert spec["resolve"]["scale"] == {
        "x": "shared",
        "y": "shared",
        "color": "shared",
        "size": "shared",
    }
    # 只有最下面的窗格畫橫軸的刻度
    x_axes = [p["encoding"]["x"].get("axis", {}) for p in panes]
    assert all(axis.get("labels") is False for axis in x_axes[:-1])
    assert x_axes[-1].get("labels") is not False


def test_columns_split_into_panes(tmp_path: Path, db: Path) -> None:
    sheet = worksheet(
        "Bars",
        [REGION, CATEGORY, SALES],
        rows=ref("none:Region:nk"),
        cols=f"({ref('none:Category:nk')} / {ref('sum:Sales:qk')})",
        mark="Bar",
    )
    spec = render(parse(tmp_path, sheet), db).spec
    panes = spec["hconcat"]
    assert len(panes) == 3
    assert [p["title"]["text"] for p in panes] == ["Furniture", "Office", "Technology"]
    assert all(p["title"]["orient"] == "top" for p in panes)
    # 縱軸（地區）只在最左邊的窗格畫刻度
    assert panes[0]["encoding"]["y"].get("axis", {}).get("labels") is not False
    assert all(p["encoding"]["y"]["axis"]["labels"] is False for p in panes[1:])


def test_scatter_panes_with_trend_lines(tmp_path: Path, db: Path) -> None:
    """散佈圖的列上加上 Category：每個類別一個窗格，趨勢線也只畫在自己的窗格。"""
    sheet = scatter(trendline(), color=False).replace(
        f"<rows>{ref('sum:Profit:qk')}</rows>",
        f"<rows>({ref('none:Category:nk')} / {ref('sum:Profit:qk')})</rows>",
    )
    sheet = sheet.replace("</datasource-dependencies>", f"{CATEGORY}</datasource-dependencies>", 1)
    spec = render(parse(tmp_path, sheet), db).spec
    panes = spec["vconcat"]
    assert len(panes) == 3
    for pane in panes:
        trend = next(layer for layer in pane["layer"] if layer.get("name") == "__ref_trend")
        assert trend["transform"][0]["filter"] == filters(pane)[0]


def test_no_panes_when_the_dimension_is_the_axis(tmp_path: Path, db: Path) -> None:
    """列為 Region、欄為 SUM(Sales)：地區是縱軸的標題，不分窗格。"""
    sheet = worksheet(
        "Bars",
        [REGION, SALES, PROFIT],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        mark="Bar",
    )
    spec = render(parse(tmp_path, sheet), db).spec
    assert "vconcat" not in spec and "hconcat" not in spec
