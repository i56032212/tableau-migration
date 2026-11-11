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


def measure_pane(pane_id: int, axis: str, mark: str, encodings: str, index: str = "") -> str:
    return (
        f"<pane id='{pane_id}' y-axis-name='{axis}'{index}><view><breakdown value='auto' /></view>"
        f"<mark class='{mark}' /><encodings>{encodings}</encodings></pane>"
    )


def test_dual_axis_colored_by_measure_names(tmp_path: Path, db: Path) -> None:
    """雙軸的兩個量值以 Measure Names 上色（沒有 Measure Values）：每個量值的圖層以常數欄位上色，共用圖例。"""
    color = f"<color column='{ref(':Measure Names')}' />"
    sheet = worksheet(
        "Dual",
        [MONTH, SALES, PROFIT],
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("tmn:Order Date:qk"),
        mark="Line",
        encodings=color,
        panes_extra=measure_pane(2, ref("sum:Sales:qk"), "Line", color)
        + measure_pane(3, ref("sum:Profit:qk"), "Line", color, " y-index='1'"),
    )
    spec = render(parse(tmp_path, sheet), db).spec
    sales, profit = spec["layer"]
    for layer, title in ((sales, "Sales"), (profit, "Profit")):
        assert {"calculate": f'"{title}"', "as": "__mn_name"} in layer["transform"]
        color_def = layer["encoding"]["color"]
        assert (color_def["field"], color_def["title"]) == ("__mn_name", "Measure Names")
        assert color_def["scale"]["domain"] == ["Sales", "Profit"]


def test_measure_values_with_own_card_and_dual_axis(tmp_path: Path, db: Path) -> None:
    """KPI 的折線：Measure Values 的軸有自己的標記卡（以 Measure Names 上色的折線），疊上另一個量值的圓點
    （雙軸、同步的座標軸）。"""
    values = ref("Multiple Values")
    names = f"<color column='{ref(':Measure Names')}' />"
    sheet = worksheet(
        "KPI",
        [MONTH, SALES, PROFIT],
        rows=f"({values} + {ref('sum:Profit:qk')})",
        cols=ref("tmn:Order Date:qk"),
        mark="Automatic",
        panes_extra=measure_pane(2, values, "Line", names)
        + measure_pane(3, ref("sum:Profit:qk"), "Circle", ""),
        view_extra=(
            f"<filter class='categorical' column='{ref(':Measure Names')}'>"
            "<groupfilter function='union'>"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('sum:Sales:qk')}&quot;' />"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('sum:Profit:qk')}&quot;' />"
            "</groupfilter></filter>"
        ),
    ).replace(
        "<style />",
        "<style><style-rule element='axis'>"
        f"<encoding attr='space' class='0' field='{ref('sum:Profit:qk')}' field-type='quantitative' "
        "fold='true' scope='rows' synchronized='true' type='space' />"
        "</style-rule></style>",
        1,
    )
    ir = parse(tmp_path, sheet)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert [(p.measure.field, p.dual, p.marks.type) for p in ws.panes] == [
        ("measure_values", False, "line"),
        ("f_profit", True, "circle"),
    ]
    spec = render(ir, db).spec
    # 同步的雙軸共用刻度：Measure Values 的折線（每個量值一條）與 Profit 的圓點疊在一起
    assert spec["resolve"] == {"scale": {"y": "shared"}}
    lines, dots = spec["layer"]
    assert lines["mark"]["type"] == "line" and lines["encoding"]["color"]["field"] == "__mn"
    assert dots["mark"]["type"] == "circle"
    assert {"filter": "!(datum.__mn_i > 0)"} in dots["transform"]


def test_dual_axis_colored_by_different_measures(tmp_path: Path, db: Path) -> None:
    """雙軸的兩張標記卡以不同的量值著色：各自的色階與圖例（共用時兩個色階會合併，顏色就錯了）。"""
    sheet = worksheet(
        "Dual",
        [CATEGORY, SALES, PROFIT],
        rows=f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        cols=ref("none:Category:nk"),
        panes_extra=measure_pane(
            2, ref("sum:Sales:qk"), "Bar", f"<color column='{ref('sum:Sales:qk')}' />"
        )
        + measure_pane(
            3,
            ref("sum:Profit:qk"),
            "Bar",
            f"<color column='{ref('sum:Profit:qk')}' />",
            " y-index='1'",
        ),
    )
    spec = render(parse(tmp_path, sheet), db).spec
    assert spec["resolve"]["scale"]["color"] == "independent"
    sales, profit = spec["layer"]
    assert sales["encoding"]["color"]["field"] != profit["encoding"]["color"]["field"]
