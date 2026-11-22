"""色彩（Tableau 的「編輯色彩」與標記卡的色彩）：解析與呈現，寫法取自公開報表（HR Dashboards、
Practice Day 1、TabMon、Snow Sports 等）。

測試資料見 tests/sqlgen/test_query.py：各區的 SUM(Sales) East 350、North 60、South 100、West 800；
各區、類別的 SUM(Profit) East／Furniture 5、East／Technology 40、North／Office 6、South／Furniture 8、
South／Office 2、West／Furniture 30、West／Technology -10。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tmig.ir import IR, ColorAssignment, ColorPalette
from tmig.ir.models import Scalar, Sort
from tmig.render.colors import continuous_scale, discrete_scale
from tmig.render.palettes import DEFAULT_REGULAR, PALETTES, interpolate, sample

from ..parser.builder import DS, instance, ref, worksheet
from .test_text import CATEGORY, PROFIT, REGION, SALES, db, parse, render

__all__ = ["db"]  # pytest fixture

ORDERS = instance("[Order ID]", "CountD", "[ctd:Order ID:qk]", "quantitative")
QUANTITY = instance("[Quantity]", "None", "[none:Quantity:ok]", "ordinal")
DATASOURCE_STYLE = f"""
<style>
  <style-rule element='mark'>
    <encoding attr='color' field='[none:Region:nk]' type='palette' palette='tableau-10'>
      <map to='#FF0000'><bucket>"East"</bucket></map>
      <map to='#00ff00'><bucket>"West"</bucket></map>
      <map to='#0000ff'><bucket>%null%</bucket></map>
    </encoding>
    <encoding attr='color' field='[:Measure Names]' type='palette'>
      <map to='#101e47'><bucket>"[{DS}].[sum:Sales:qk]"</bucket></map>
      <map to='#bd8459'><bucket>"[{DS}].[ctd:Order ID:qk]"</bucket></map>
      <map to='#123456'><bucket>"[{DS}].[sum:Quantity:qk]"</bucket></map>
    </encoding>
    <encoding attr='color' field='[none:Quantity:ok]' type='palette'>
      <map to='#111111'><bucket>1</bucket></map>
      <map to='#222222'><bucket>2.5</bucket></map>
      <map to='#333333'><bucket>true</bucket></map>
      <map to='#444444'><bucket>#2024-01-01#</bucket></map>
      <map to='#555555'><bucket>%all%</bucket></map>
    </encoding>
  </style-rule>
</style>"""


def colored_bars(field: str = "none:Region:nk", mark: str = "Bar", **extra: Any) -> str:
    return worksheet(
        "Bars",
        [REGION, SALES, QUANTITY],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        mark=mark,
        encodings=f"<color column='{ref(field)}' />",
        **extra,
    )


def profit_matrix(style: str = "") -> str:
    """各區、類別的利潤以顏色呈現（方塊）；style：工作表 <style> 的內容。"""
    sheet = worksheet(
        "Matrix",
        [REGION, CATEGORY, PROFIT],
        rows=ref("none:Region:nk"),
        cols=ref("none:Category:nk"),
        mark="Square",
        encodings=f"<color column='{ref('sum:Profit:qk')}' />",
    )
    return sheet.replace(
        "<style />", f"<style><style-rule element='mark'>{style}</style-rule></style>"
    )


def color_scale(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["encoding"]["color"]["scale"]


def parse_styled(tmp_path: Path, sheet: str, columns: str = "") -> IR:
    return parse(tmp_path, sheet, columns + DATASOURCE_STYLE)


# ---------------------------------------------------------------- 解析


def test_discrete_colors_from_datasource_style(tmp_path: Path) -> None:
    """資料源 <style> 中指定給各值的顏色（所有工作表共用），顏色統一為小寫。"""
    ir = parse_styled(tmp_path, colored_bars())
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    assert ir.worksheets[0].marks.color_palette == ColorPalette(
        kind="discrete",
        palette="tableau-10",
        assignments=[
            ColorAssignment(value="East", color="#ff0000"),
            ColorAssignment(value="West", color="#00ff00"),
            ColorAssignment(value=None, color="#0000ff"),
        ],
    )
    # 數值、布林、日期；Tableau 的特殊值（%all%）略過
    quantity = parse_styled(tmp_path, colored_bars("none:Quantity:ok"))
    palette = quantity.worksheets[0].marks.color_palette
    assert palette is not None
    assert [(a.value, a.color) for a in palette.assignments] == [
        (1, "#111111"),
        (2.5, "#222222"),
        (True, "#333333"),
        ("2024-01-01", "#444444"),
    ]


def test_continuous_palette_from_worksheet_style(tmp_path: Path) -> None:
    """工作表 <style> 的連續色盤：內建色盤與各項設定（TabMon 的寫法）。"""
    style = (
        f"<encoding attr='color' field='{ref('sum:Profit:qk')}' palette='tableau-red-blue' "
        "type='interpolated' reverse='true' num-steps='5' min='-20.0' max='80.0' center='40.0' "
        "symmetric='false' />"
    )
    ir = parse(tmp_path, profit_matrix(style))
    assert ir.worksheets[0].marks.color_palette == ColorPalette(
        kind="continuous",
        palette="tableau-red-blue",
        reverse=True,
        steps=5,
        start=-20.0,
        end=80.0,
        center=40.0,
        full_range=True,
    )
    custom = (
        f"<encoding attr='color' field='{ref('sum:Profit:qk')}' type='custom-interpolated'>"
        "<color-palette custom='true' name='' type='ordered-diverging'>"
        "<color>#AA0000</color><color>#f5f5f5</color><color>#0000aa</color>"
        "</color-palette></encoding>"
    )
    palette = parse(tmp_path, profit_matrix(custom)).worksheets[0].marks.color_palette
    assert palette == ColorPalette(
        kind="continuous", colors=["#aa0000", "#f5f5f5", "#0000aa"], diverging=True
    )


def test_mark_color_opacity_and_border_are_parsed(tmp_path: Path) -> None:
    pane_style = (
        "<style><style-rule element='mark'>"
        "<format attr='mark-color' value='#101E47' />"
        "<format attr='mark-transparency' value='128' />"
        "<format attr='has-stroke' value='true' />"
        "<format attr='stroke-color' value='#ffffff' />"
        "</style-rule></style>"
    )
    sheet = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        pane_extra=pane_style,
    )
    marks = parse(tmp_path, sheet).worksheets[0].marks
    assert (marks.mark_color, marks.opacity, marks.stroke) == ("#101e47", 0.502, "#ffffff")
    # 不透明（255）與沒有框線（has-stroke='false'）時不記錄
    opaque = pane_style.replace("'128'", "'255'").replace("value='true'", "value='false'")
    marks = parse(tmp_path, sheet.replace(pane_style, opaque)).worksheets[0].marks
    assert (marks.mark_color, marks.opacity, marks.stroke) == ("#101e47", None, None)


# ---------------------------------------------------------------- 離散


def test_discrete_colors_follow_assignments_then_palette(tmp_path: Path, db: Path) -> None:
    """指定的值用指定的顏色；其餘依色盤（tableau-10：Tableau 10 以前的預設色盤）的順序。"""
    spec = render(parse_styled(tmp_path, colored_bars()), db).spec
    scale = color_scale(spec)
    assert scale["domain"] == ["East", "North", "South", "West"]
    classic = PALETTES["tableau-10"].colors
    assert scale["range"] == ["#ff0000", classic[1], classic[2], "#00ff00"]


def test_legend_follows_manual_and_alphabetic_sorts(tmp_path: Path, db: Path) -> None:
    """色彩欄位有手動排序（例如在檢視或圖例上拖曳成員）或遞減的字母排序時，圖例依這個順序排列、堆疊的長條
    依它堆疊，各成員的顏色不變（Tableau：在圖例上排序也改變標記的順序）。依欄位的值排序時仍依成員排列。"""
    ir = parse_styled(tmp_path, colored_bars())
    ws = ir.worksheets[0]
    region = ws.rows[0].field
    member = render(ir, db).spec
    colors = dict(zip(color_scale(member)["domain"], color_scale(member)["range"], strict=True))
    assert "order" not in member["encoding"]

    def with_sort(sort: Sort) -> dict[str, Any]:
        sheet = ws.model_copy(update={"sorts": [sort]})
        return render(ir.model_copy(update={"worksheets": [sheet]}), db).spec

    manual = with_sort(
        Sort(field=region, kind="manual", manual_values=["West", "East", "South", "North"])
    )
    scale = color_scale(manual)
    assert scale["domain"] == ["West", "East", "South", "North"]
    assert scale["range"] == [colors[label] for label in scale["domain"]]
    assert manual["encoding"]["order"] == {"field": "__legend_order", "type": "quantitative"}
    stacked = {r["__legend_order"] for r in manual["data"]["values"] if "__legend_order" in r}
    assert stacked == {0, 1, 2, 3}

    descending = color_scale(with_sort(Sort(field=region, kind="alphabetic", order="desc")))
    assert descending["domain"] == ["West", "South", "North", "East"]
    assert descending["range"] == [colors[label] for label in descending["domain"]]

    by_field = with_sort(Sort(field=region, kind="by_field", order="desc", by=ws.columns[0]))
    assert color_scale(by_field)["domain"] == ["East", "North", "South", "West"]


def test_measure_names_colors(tmp_path: Path, db: Path) -> None:
    """Measure Names：指定給量值（欄位實例）的顏色，對應到顯示名稱（含別名）；不在工作表的量值略過。"""
    sheet = worksheet(
        "KPIs",
        [REGION, SALES, ORDERS],
        rows=ref("none:Region:nk"),
        cols=f"[{DS}].[:Measure Names]",
        mark="Square",
        encodings=(
            f"<text column='[{DS}].[Multiple Values]' /><color column='[{DS}].[:Measure Names]' />"
        ),
        view_extra=(
            f"<filter class='categorical' column='[{DS}].[:Measure Names]'>"
            "<groupfilter function='union' user:op='manual'>"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('sum:Sales:qk')}&quot;' />"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('ctd:Order ID:qk')}&quot;' />"
            "</groupfilter></filter>"
        ),
    )
    alias = (
        "<column datatype='string' name='[:Measure Names]' role='dimension' type='nominal'>"
        f"<aliases><alias key='&quot;{ref('ctd:Order ID:qk')}&quot;' value='訂單數' /></aliases>"
        "</column>"
    )
    ir = parse_styled(tmp_path, sheet, alias)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    palette = ir.worksheets[0].marks.color_palette
    assert palette is not None
    assert [(a.value, a.color) for a in palette.assignments] == [
        ("sum(f_sales)", "#101e47"),
        ("countd(f_order_id)", "#bd8459"),
    ]
    scale = color_scale(render(ir, db).spec)
    assert scale == {"domain": ["Sales", "訂單數"], "range": ["#101e47", "#bd8459"]}


def test_discrete_fallback_from_sequential_palette() -> None:
    """以連續色盤指定給離散欄位、又沒有指定顏色的值：從色盤平均取色。"""
    setting = ColorPalette(kind="discrete", palette="blue_10_0")
    scale = discrete_scale(setting, [("a", "a"), ("b", "b"), ("c", "c"), ("a", "a")])
    blue = PALETTES["blue_10_0"].colors
    assert scale == {
        "domain": ["a", "b", "c"],
        "range": [blue[0], interpolate(blue, 0.5), blue[-1]],
    }
    assert discrete_scale(None, [("a", "a")]) is None


def test_discrete_colors_follow_member_order_not_view_order() -> None:
    """與 Tableau 相同，顏色屬於成員、不隨工作表的排序改變：依原值排序後依序取色（數字由小到大、文字不分
    大小寫、Null 在最後），圖例也依這個順序；集合的「內」在「外」之前；Measure Names 依量值的順序。"""
    automatic = ColorPalette(kind="discrete", palette=DEFAULT_REGULAR)
    colors = PALETTES[DEFAULT_REGULAR].colors
    # 依銷售排序的長條：West、East、Central（資料的順序）
    scale = discrete_scale(automatic, [("West", "West"), ("east", "east"), ("Central", "Central")])
    assert scale == {"domain": ["Central", "east", "West"], "range": list(colors[:3])}
    # 叢集編號（別名 Cluster N）依編號，不依別名的字母順序
    clusters = [("Cluster 3", 3), ("Cluster 10", 10), ("Cluster 1", 1), ("Not Clustered", 0)]
    scale = discrete_scale(automatic, clusters)
    assert scale is not None
    assert scale["domain"] == ["Not Clustered", "Cluster 1", "Cluster 3", "Cluster 10"]
    scale = discrete_scale(
        automatic, [("Out", False), ("In", True), ("Null", None)], members_first=True
    )
    assert scale is not None and scale["domain"] == ["In", "Out", "Null"]
    keys: dict[Scalar, Any] = {"sum(f_sales)": "Sales", "sum(f_profit)": "Profit"}
    scale = discrete_scale(automatic, [("Sales", "Sales"), ("Profit", "Profit")], keys)
    assert scale is not None and scale["domain"] == ["Sales", "Profit"]


# ---------------------------------------------------------------- 連續


def test_automatic_continuous_palettes(tmp_path: Path, db: Path) -> None:
    """沒有設定時與 Tableau 相同：都是正數用 Blue（最小值到最大值），有正有負用 Orange-Blue Diverging，
    以 0 為中心、兩邊對稱（離中心較遠的一端用到最深的顏色）。"""
    sales = colored_bars("sum:Sales:qk")
    scale = color_scale(render(parse(tmp_path, sales), db).spec)
    blue = PALETTES["blue_10_0"].colors
    assert scale["range"] == list(blue)
    assert (scale["domain"][0], scale["domain"][1], scale["domain"][-1]) == (60, 134, 800)
    scale = color_scale(render(parse(tmp_path, profit_matrix()), db).spec)
    diverging = PALETTES["orange_blue_diverging_10_0"].colors
    assert scale["range"] == list(diverging)
    middle = len(diverging) // 2
    assert (scale["domain"][0], scale["domain"][middle], scale["domain"][-1]) == (-40, 0, 40)


def test_named_palette_with_steps_and_range(tmp_path: Path, db: Path) -> None:
    """內建色盤：反轉、分段（等分成 4 段，每段的顏色從色盤平均取出）、指定範圍的起訖。"""
    style = (
        f"<encoding attr='color' field='{ref('sum:Profit:qk')}' palette='red_10_0' "
        "type='interpolated' reverse='true' num-steps='4' min='0.0' max='100.0' />"
    )
    scale = color_scale(render(parse(tmp_path, profit_matrix(style)), db).spec)
    red = PALETTES["red_10_0"].colors[::-1]
    assert scale == {"type": "quantize", "domain": [0.0, 100.0], "range": sample(red, 4)}
    assert (scale["range"][0], scale["range"][-1]) == ("#ae123a", "#ffbeb2")


@pytest.mark.parametrize(
    ("symmetric", "expected"),
    [
        # 預設：以中心 5 兩邊對稱，離中心較遠的一端（40，相距 35）用到最深的顏色
        ("", (-30, 5, 40)),
        # Use Full Color Range：兩端都用到最深的顏色
        ("symmetric='false'", (-10, 5, 40)),
    ],
)
def test_diverging_center_and_full_range(
    tmp_path: Path, db: Path, symmetric: str, expected: tuple[float, float, float]
) -> None:
    style = (
        f"<encoding attr='color' field='{ref('sum:Profit:qk')}' palette='red_green_diverging_10_0' "
        f"type='interpolated' center='5' {symmetric} />"
    )
    scale = color_scale(render(parse(tmp_path, profit_matrix(style)), db).spec)
    colors = PALETTES["red_green_diverging_10_0"].colors
    assert scale["range"] == list(colors)
    middle = len(colors) // 2
    assert (scale["domain"][0], scale["domain"][middle], scale["domain"][-1]) == expected


def test_custom_palette(tmp_path: Path, db: Path) -> None:
    style = (
        f"<encoding attr='color' field='{ref('sum:Sales:qk')}' type='custom-interpolated'>"
        "<color-palette custom='true' name='' type='ordered-sequential'>"
        "<color>#f1f1f1</color><color>#03c4a1</color></color-palette></encoding>"
    )
    sheet = colored_bars("sum:Sales:qk").replace(
        "<style />", f"<style><style-rule element='mark'>{style}</style-rule></style>"
    )
    scale = color_scale(render(parse(tmp_path, sheet), db).spec)
    assert scale == {
        "domain": [60, 800],
        "range": ["#f1f1f1", "#03c4a1"],
        "interpolate": "rgb",
        "clamp": True,
    }


def test_continuous_scale_edge_cases() -> None:
    # 沒有資料：只給色盤的兩端
    assert continuous_scale(None, []) == {"range": ["#b9ddf1", "#2a5783"], "interpolate": "rgb"}
    # 只有一個值：範圍至少 1
    single = continuous_scale(None, [5.0])
    assert (single["domain"][0], single["domain"][-1]) == (5, 6)
    # 認不得的色盤依自動色盤
    unknown = ColorPalette(kind="continuous", palette="no_such_palette")
    assert continuous_scale(unknown, [1.0, 2.0])["range"] == list(PALETTES["blue_10_0"].colors)


def test_palette_sampling() -> None:
    assert interpolate(("#000000", "#ffffff"), 0.5) == "#808080"
    assert sample(("#000000", "#ffffff"), 3) == ["#000000", "#808080", "#ffffff"]
    assert sample(("#000000", "#ffffff"), 1) == ["#808080"]
    # 每個色盤都由小到大、顏色寫法一致
    for name, palette in PALETTES.items():
        assert palette.colors, name
        assert all(len(c) == 7 and c.startswith("#") and c == c.lower() for c in palette.colors)


# ---------------------------------------------------------------- 標記卡的色彩


def pane_style(*formats: tuple[str, str]) -> str:
    items = "".join(f"<format attr='{a}' value='{v}' />" for a, v in formats)
    return f"<style><style-rule element='mark'>{items}</style-rule></style>"


def test_mark_color_opacity_and_border_render(tmp_path: Path, db: Path) -> None:
    style = pane_style(
        ("mark-color", "#101e47"),
        ("mark-transparency", "128"),
        ("has-stroke", "true"),
        ("stroke-color", "#ffffff"),
    )
    bars = worksheet(
        "Bars",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        pane_extra=style,
    )
    mark = render(parse(tmp_path, bars), db).spec["mark"]
    assert mark == {
        "type": "bar",
        "tooltip": True,
        "color": "#101e47",
        "opacity": 0.502,
        "stroke": "#ffffff",
        "strokeWidth": 1,
        "height": {"band": 0.75},  # Tableau 預設的長條粗細
    }
    # 面積：框線以折線疊在上緣；與 Tableau 相同不畫出點（透明的點仍可停留、點選）
    area = pane_style(
        ("mark-color", "#bd8459"),
        ("mark-transparency", "45"),
        ("has-stroke", "true"),
        ("stroke-color", "#bd8459"),
    )
    sheet = worksheet(
        "Area",
        [instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative"), SALES],
        rows=ref("sum:Sales:qk"),
        cols=ref("tmn:Order Date:qk"),
        mark="Area",
        pane_extra=area,
    )
    mark = render(parse(tmp_path, sheet), db).spec["mark"]
    assert mark["color"] == "#bd8459" and mark["opacity"] == 0.1765
    assert mark["line"] == {"color": "#bd8459"} and mark["point"] == "transparent"


def test_mark_color_ignored_with_color_field(tmp_path: Path, db: Path) -> None:
    """有顏色欄位時依欄位著色，標記卡的色彩不套用。"""
    sheet = colored_bars(pane_extra=pane_style(("mark-color", "#101e47")))
    spec = render(parse(tmp_path, sheet), db).spec
    assert "color" not in spec["mark"]
    assert spec["encoding"]["color"]["field"].endswith("_label")


def test_text_and_pie_mark_colors(tmp_path: Path, db: Path) -> None:
    style = pane_style(("mark-color", "#bd8459"))
    kpi = worksheet(
        "KPI",
        [SALES],
        mark="Text",
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=style,
    )
    assert render(parse(tmp_path, kpi), db).spec["mark"]["color"] == "#bd8459"
    pie = worksheet(
        "Pie",
        [SALES],
        mark="Pie",
        encodings=f"<wedge-size column='{ref('sum:Sales:qk')}' />",
        pane_extra=style,
    )
    assert render(parse(tmp_path, pie), db).spec["mark"]["color"] == "#bd8459"
