"""建立擴充階段的功能報表 fixtures/superstore_features/superstore_features.twbx。

    uv run python scripts/build_features_fixture.py

資料與資料源沿用示範報表（scripts/build_demo_fixture.py），.twb 依真實 Tableau 檔案的結構手寫，
每加入一項擴充功能就在這裡補上對應的工作表或 dashboard 設定：

- H1 全域篩選（<shared-views>）與篩選群組（filter-group）：Region 為全域篩選，Segment 篩選套用到兩張工作表
- I1 自動尺寸的 dashboard：右側篩選欄固定 212 像素寬、一個工作表不顯示標題、一個隱藏的文字區塊
- J1 Measure Names／Values：文字表（Segment KPIs，手動排序與篩選順序不同）、折線（Monthly Sales vs Profit）
- J2 同一架多個量值與雙軸：Sales and Profit by Year（長條＋折線，第二個軸）
- J3 圓餅：Category Mix（角度為銷售額、標籤含合計百分比）
- H4 排除選取的標記：Region Category Matrix 排除 (West, Furniture)、(East, Technology) 兩個值組合
- L1 篩選動作：點選 Region Picker 的地區篩選 Category Detail（清除時顯示全部）
  與 Segment Detail（清除時不顯示任何值），都在「篩選動作」dashboard 上；另有一個醒目提示動作
- L2 網址動作：Region Picker 的選單中開啟以地區與銷售額代入的網址；「參數動作」dashboard 上點選地區設定參數
  Selected Region（Selected Region Sales 依參數計算），選單中切換到「篩選動作」dashboard
- K1 群組與數值分組：Region (group)（East、West 合為 Coasts，其餘維持原值）、Ship Speed
  （First Class、Same Day 合為 Fast，其餘歸入 Other）、Quantity (bin)（每 3 一格，顯示缺少的值），
  在「群組與分組」dashboard 上
- K4 篩選條件：相對日期（最近 3 個月）、彙總條件（總銷售額大於 5000 的客戶）、萬用字元
  （客戶名稱包含 son）、在一般選取中取 Top N（排除 Phones 後的前 3 名子類別），在「篩選條件」dashboard 上
- K3 快速 Table Calculation：Sales Growth by Year 的差異與差異百分比（與前一年比較），在「年度成長」dashboard 上
- J4 參考線：Sub-Category Sales 每個類別（窗格）的平均線與整張表的目標常數線；Region Sales Bands 的中位數
  與平均 ± 1 個母體標準差，在「參考線」dashboard 上（只影響圖表，不影響數字）
"""

from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_demo_fixture as demo

ROOT = demo.ROOT
FIXTURE = ROOT / "fixtures" / "superstore_features"
TWBX = FIXTURE / "superstore_features.twbx"
DS = demo.DS
ref = demo.ref
attr = demo.attr
instance = demo.instance
member = demo.member
dependency = demo.dependency

DASHBOARD_W, DASHBOARD_H = 1000, 700
SEGMENT_GROUP = "5"
"""Segment 篩選套用到「Sales by Segment」與「Profit by Ship Mode」兩張工作表（套用到選取的工作表）。"""


def zone_box(x: int, y: int, w: int, h: int) -> str:
    def u(px: int, total: int) -> int:
        return round(px * 100000 / total)

    return (
        f"h='{u(h, DASHBOARD_H)}' w='{u(w, DASHBOARD_W)}' "
        f"x='{u(x, DASHBOARD_W)}' y='{u(y, DASHBOARD_H)}'"
    )


def pane(mark: str, encodings: str = "", extra: str = "") -> str:
    body = f"\n            <encodings>\n{encodings}\n            </encodings>" if encodings else ""
    return f"""          <pane selection-relaxation-option='selection-relaxation-allow'{extra}>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class={attr(mark)} />{body}
          </pane>"""


def worksheet(
    name: str,
    uuid: str,
    deps: str,
    view_extra: str,
    panes: str,
    rows: str,
    cols: str,
) -> str:
    return f"""    <worksheet name={attr(name)}>
      <table>
        <view>
          <datasources>
            <datasource caption='Orders' name='{DS}' />
          </datasources>
{deps}
{view_extra}
          <aggregation value='true' />
        </view>
        <style />
        <panes>
{panes}
        </panes>
        <rows>{escape(rows)}</rows>
        <cols>{escape(cols)}</cols>
      </table>
      <simple-id uuid='{{{uuid}}}' />
    </worksheet>"""


def slices(*instances: str) -> str:
    lines = "\n".join(f"            <column>{escape(ref(i))}</column>" for i in instances)
    return f"          <slices>\n{lines}\n          </slices>"


def segment_filter() -> str:
    return f"""          <filter class='categorical' column={attr(ref("none:Segment:nk"))} filter-group='{SEGMENT_GROUP}'>
            <groupfilter function='union' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate'>
              {member("[none:Segment:nk]", "Consumer")}
              {member("[none:Segment:nk]", "Corporate")}
            </groupfilter>
          </filter>"""


def shared_views() -> str:
    """全域篩選：Region 排除 Central；Ship Mode 為全部。"""
    return f"""  <shared-views>
    <shared-view name='{DS}'>
      <datasources>
        <datasource caption='Orders' name='{DS}' />
      </datasources>
      <datasource-dependencies datasource='{DS}'>
        <column datatype='string' name='[Region]' role='dimension' type='nominal' />
        <column datatype='string' name='[Ship Mode]' role='dimension' type='nominal' />
        <column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />
        <column-instance column='[Ship Mode]' derivation='None' name='[none:Ship Mode:nk]' pivot='key' type='nominal' />
      </datasource-dependencies>
      <filter class='categorical' column={attr(ref("none:Region:nk"))}>
        <groupfilter function='except' user:ui-domain='database' user:ui-enumeration='exclusive' user:ui-marker='enumerate'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
          {member("[none:Region:nk]", "Central")}
        </groupfilter>
      </filter>
      <filter class='categorical' column={attr(ref("none:Ship Mode:nk"))}>
        <groupfilter function='level-members' level='[none:Ship Mode:nk]' user:ui-enumeration='all' user:ui-marker='enumerate' />
      </filter>
    </shared-view>
  </shared-views>"""


GLOBAL_COLUMNS = [
    ("[Region]", "string", "dimension", "nominal"),
    ("[Ship Mode]", "string", "dimension", "nominal"),
]
GLOBAL_INSTANCES = [
    instance("[Region]", "None", "[none:Region:nk]", "nominal"),
    instance("[Ship Mode]", "None", "[none:Ship Mode:nk]", "nominal"),
]


# 篩選動作與排除選取產生的隱藏群組欄位（在資料源中，工作表的篩選與篩選架參照它們）
ACTION_REGION = "Action (Region)"
EXCLUSIONS = "Exclusions (Region,Category)"
ACTION_CATEGORY = "[Action1_7D6C2A1F2B3C4D5E9F0A000000000101]"
ACTION_SEGMENT = "[Action2_7D6C2A1F2B3C4D5E9F0A000000000102]"
ACTION_BRUSH = "[Action3_7D6C2A1F2B3C4D5E9F0A000000000103]"
ACTION_URL = "[Action4_7D6C2A1F2B3C4D5E9F0A000000000104]"
DATASOURCE_GROUPS = f"""      <group caption='{ACTION_REGION}' hidden='true' name='[{ACTION_REGION}]' name-style='unqualified' user:auto-column='sheet_link'>
        <groupfilter function='crossjoin'>
          <groupfilter function='level-members' level='[Region]' />
        </groupfilter>
      </group>
      <group hidden='true' name='[{EXCLUSIONS}]' name-style='unqualified' user:auto-column='exclude'>
        <groupfilter function='crossjoin'>
          <groupfilter function='level-members' level='[none:Region:nk]' />
          <groupfilter function='level-members' level='[none:Category:nk]' />
        </groupfilter>
      </group>"""

# 參數動作：點選地區設定參數，計算欄位依參數計算
SELECTED_CALC = "[Calculation_7D6C2A1F00000001]"
PARAMETERS = """    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      <column caption='Selected Region' datatype='string' name='[Parameter 1]' param-domain-type='list' role='measure' type='nominal' value='&quot;East&quot;'>
        <calculation class='tableau' formula='&quot;East&quot;' />
        <members>
          <member value='&quot;Central&quot;' />
          <member value='&quot;East&quot;' />
          <member value='&quot;South&quot;' />
          <member value='&quot;West&quot;' />
        </members>
      </column>
    </datasource>"""
SELECTED_COLUMN = f"""      <column caption='Selected Region Sales' datatype='real' name='{SELECTED_CALC}' role='measure' type='quantitative'>
        <calculation class='tableau' formula='IF [Region] = [Parameters].[Parameter 1] THEN [Sales] ELSE 0 END' />
      </column>"""

# 群組與數值分組（計算欄位的 categorical-bin、bin 類型）
REGION_GROUP = """      <column caption='Region (group)' datatype='string' name='[Region (group)]' role='dimension' type='nominal'>
        <calculation class='categorical-bin' column='[Region]' new-bin='true'>
          <bin default-name='false' value='&quot;Coasts&quot;'>
            <value>&quot;East&quot;</value>
            <value>&quot;West&quot;</value>
          </bin>
        </calculation>
      </column>"""
SHIP_SPEED = """      <column caption='Ship Speed' datatype='string' name='[Ship Mode (group)]' role='dimension' type='nominal'>
        <calculation class='categorical-bin' column='[Ship Mode]' default='&quot;Other&quot;' new-bin='true'>
          <bin default-name='false' value='&quot;Fast&quot;'>
            <value>&quot;First Class&quot;</value>
            <value>&quot;Same Day&quot;</value>
          </bin>
        </calculation>
      </column>"""
QUANTITY_BIN = """      <column aggregation='None' caption='Quantity (bin)' datatype='integer' name='[Quantity (bin)]' role='dimension' type='ordinal'>
        <calculation class='bin' decimals='0' formula='[Quantity]' peg='0' size='3' />
      </column>"""


def worksheets() -> str:
    sales_by_segment = worksheet(
        "Sales by Segment",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000001",
        dependency(
            [
                *GLOBAL_COLUMNS,
                ("[Sales]", "real", "measure", "quantitative"),
                ("[Segment]", "string", "dimension", "nominal"),
            ],
            [
                *GLOBAL_INSTANCES,
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
                instance("[Segment]", "None", "[none:Segment:nk]", "nominal"),
            ],
            [],
        ),
        segment_filter() + "\n" + slices("none:Segment:nk", "none:Region:nk", "none:Ship Mode:nk"),
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Segment:nk"),
        ref("sum:Sales:qk"),
    )
    profit_by_ship_mode = worksheet(
        "Profit by Ship Mode",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000002",
        dependency(
            [
                *GLOBAL_COLUMNS,
                ("[Profit]", "real", "measure", "quantitative"),
                ("[Segment]", "string", "dimension", "nominal"),
            ],
            [
                *GLOBAL_INSTANCES,
                instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
                instance("[Segment]", "None", "[none:Segment:nk]", "nominal"),
            ],
            [],
        ),
        segment_filter() + "\n" + slices("none:Segment:nk", "none:Region:nk", "none:Ship Mode:nk"),
        pane("Text", f"              <text column={attr(ref('sum:Profit:qk'))} />"),
        ref("none:Ship Mode:nk"),
        "",
    )
    orders_by_region = worksheet(
        "Orders by Region",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000003",
        dependency(
            [
                *GLOBAL_COLUMNS,
                ("[Order ID]", "string", "dimension", "nominal"),
            ],
            [
                *GLOBAL_INSTANCES,
                instance("[Order ID]", "CountD", "[ctd:Order ID:qk]", "quantitative"),
            ],
            [],
        ),
        slices("none:Region:nk", "none:Ship Mode:nk"),
        pane("Text", f"              <text column={attr(ref('ctd:Order ID:qk'))} />"),
        ref("none:Region:nk"),
        "",
    )
    return "\n".join(
        [
            sales_by_segment,
            profit_by_ship_mode,
            orders_by_region,
            segment_kpis(),
            monthly_sales_vs_profit(),
            sales_and_profit_by_year(),
            category_mix(),
            region_picker(),
            category_detail(),
            segment_detail(),
            region_category_matrix(),
            sales_by_region_group(),
            sales_by_ship_speed(),
            sales_by_quantity_bin(),
            recent_sales(),
            big_customers(),
            son_customers(),
            top_sub_categories(),
            sales_growth(),
            selected_region_sales(),
            sub_category_sales(),
            region_sales_bands(),
        ]
    )


PROFIT_RATIO = demo.CALC_PROFIT_RATIO


def measure_names(*instances: str, order: tuple[str, ...] | None = None) -> str:
    """Measure Names 的篩選（Measure Values 卡上的量值）與手動排序。"""
    members = "\n".join(f"              {member('[:Measure Names]', ref(i))}" for i in instances)
    buckets = "\n".join(
        f"              <bucket>{escape(chr(34) + ref(i) + chr(34))}</bucket>"
        for i in (order or instances)
    )
    return f"""          <filter class='categorical' column={attr(ref(":Measure Names"))}>
            <groupfilter function='union' user:op='manual'>
{members}
            </groupfilter>
          </filter>
          <manual-sort column={attr(ref(":Measure Names"))} direction='ASC'>
            <dictionary>
{buckets}
            </dictionary>
          </manual-sort>"""


def segment_kpis() -> str:
    measures = (
        "sum:Sales:qk",
        "sum:Profit:qk",
        "usr:Calculation_1368249927221915648:qk",
        "ctd:Order ID:qk",
    )
    order = (
        "sum:Sales:qk",
        "sum:Profit:qk",
        "ctd:Order ID:qk",
        "usr:Calculation_1368249927221915648:qk",
    )
    return worksheet(
        "Segment KPIs",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000004",
        dependency(
            [
                ("[Order ID]", "string", "dimension", "nominal"),
                ("[Profit]", "real", "measure", "quantitative"),
                ("[Sales]", "real", "measure", "quantitative"),
                ("[Segment]", "string", "dimension", "nominal"),
            ],
            [
                instance(
                    PROFIT_RATIO, "User", "[usr:Calculation_1368249927221915648:qk]", "quantitative"
                ),
                instance("[Order ID]", "CountD", "[ctd:Order ID:qk]", "quantitative"),
                instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
                instance("[Segment]", "None", "[none:Segment:nk]", "nominal"),
            ],
            [PROFIT_RATIO],
        ),
        measure_names(*measures, order=order) + "\n" + slices(":Measure Names"),
        pane("Automatic", f"              <text column={attr(ref('Multiple Values'))} />"),
        ref("none:Segment:nk"),
        ref(":Measure Names"),
    )


def monthly_sales_vs_profit() -> str:
    return worksheet(
        "Monthly Sales vs Profit",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000005",
        dependency(
            [
                ("[Order Date]", "date", "dimension", "ordinal"),
                ("[Profit]", "real", "measure", "quantitative"),
                ("[Sales]", "real", "measure", "quantitative"),
            ],
            [
                instance("[Order Date]", "None", "[none:Order Date:qk]", "quantitative"),
                instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative"),
                instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        measure_names("sum:Sales:qk", "sum:Profit:qk")
        + f"""
          <filter class='quantitative' column={attr(ref("none:Order Date:qk"))} included-values='in-range'>
            <min>#2024-01-01#</min>
            <max>#2024-12-31#</max>
          </filter>
"""
        + slices(":Measure Names", "none:Order Date:qk"),
        pane("Line", f"              <color column={attr(ref(':Measure Names'))} />"),
        ref("Multiple Values"),
        ref("tmn:Order Date:qk"),
    )


def sales_and_profit_by_year() -> str:
    """雙軸：Sales 為長條（第一個軸），Profit 為折線（第二個軸）。"""
    panes = "\n".join(
        [
            pane("Automatic"),
            pane(
                "Bar",
                f"              <text column={attr(ref('sum:Sales:qk'))} />",
                f" id='1' y-axis-name={attr(ref('sum:Sales:qk'))}",
            ),
            pane(
                "Line",
                "",
                f" id='2' y-axis-name={attr(ref('sum:Profit:qk'))} y-index='1'",
            ),
        ]
    )
    return worksheet(
        "Sales and Profit by Year",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000006",
        dependency(
            [
                ("[Order Date]", "date", "dimension", "ordinal"),
                ("[Profit]", "real", "measure", "quantitative"),
                ("[Sales]", "real", "measure", "quantitative"),
            ],
            [
                instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal"),
                instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        panes,
        f"({ref('sum:Sales:qk')} + {ref('sum:Profit:qk')})",
        ref("yr:Order Date:ok"),
    )


def category_mix() -> str:
    """圓餅：顏色為 Category，角度為 SUM(Sales)，標籤為類別與合計百分比。"""
    return worksheet(
        "Category Mix",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000007",
        dependency(
            [
                ("[Category]", "string", "dimension", "nominal"),
                ("[Sales]", "real", "measure", "quantitative"),
            ],
            [
                instance("[Category]", "None", "[none:Category:nk]", "nominal"),
                instance(
                    "[Sales]",
                    "Sum",
                    "[pcto:sum:Sales:qk]",
                    "quantitative",
                    "<table-calc ordering-type='Rows' type='PctTotal' />",
                ),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        pane(
            "Pie",
            "\n".join(
                [
                    f"              <color column={attr(ref('none:Category:nk'))} />",
                    f"              <wedge-size column={attr(ref('sum:Sales:qk'))} />",
                    f"              <text column={attr(ref('none:Category:nk'))} />",
                    f"              <text column={attr(ref('pcto:sum:Sales:qk'))} />",
                ]
            ),
        ),
        "",
        "",
    )


REGION_SALES_COLUMNS = [
    ("[Region]", "string", "dimension", "nominal"),
    ("[Sales]", "real", "measure", "quantitative"),
]


def region_picker() -> str:
    """篩選動作的來源：依地區的銷售額長條圖。"""
    return worksheet(
        "Region Picker",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000008",
        dependency(
            REGION_SALES_COLUMNS,
            [
                instance("[Region]", "None", "[none:Region:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Region:nk"),
        ref("sum:Sales:qk"),
    )


def action_filter(action: str, show_all: bool) -> str:
    """篩選動作在目標工作表上的篩選：沒有選取時顯示全部（level-members）或不顯示任何值（empty-level）。"""
    if show_all:
        attrs = "user:ui-enumeration='all' user:ui-marker='enumerate'"
        inner = "<groupfilter function='level-members' level='[Region]' />"
    else:
        attrs = (
            "user:ui-domain='relevant' user:ui-enumeration='inclusive' user:ui-marker='enumerate'"
        )
        inner = "<groupfilter function='empty-level' member='[Region]' />"
    return f"""          <filter class='categorical' column={attr(ref(ACTION_REGION))}>
            <groupfilter function='crossjoin' user:ui-action-filter={attr(action)} {attrs}>
              {inner}
            </groupfilter>
          </filter>
{slices(ACTION_REGION)}"""


def detail(name: str, uuid: str, dimension: str, action: str, show_all: bool) -> str:
    return worksheet(
        name,
        uuid,
        dependency(
            [(f"[{dimension}]", "string", "dimension", "nominal"), REGION_SALES_COLUMNS[1]],
            [
                instance(f"[{dimension}]", "None", f"[none:{dimension}:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        action_filter(action, show_all),
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref(f"none:{dimension}:nk"),
        ref("sum:Sales:qk"),
    )


def category_detail() -> str:
    return detail(
        "Category Detail", "7D6C2A1F-2B3C-4D5E-9F0A-000000000009", "Category", ACTION_CATEGORY, True
    )


def segment_detail() -> str:
    return detail(
        "Segment Detail", "7D6C2A1F-2B3C-4D5E-9F0A-00000000000A", "Segment", ACTION_SEGMENT, False
    )


def region_category_matrix() -> str:
    """排除選取的標記：Region × Category 文字表，排除兩個值組合。"""

    def combo(region: str, category: str) -> str:
        return f"""                <groupfilter function='crossjoin'>
                  {member("[none:Region:nk]", region)}
                  {member("[none:Category:nk]", category)}
                </groupfilter>"""

    exclusions = f"""          <filter class='categorical' column={attr(ref(EXCLUSIONS))}>
            <groupfilter function='except' user:ui-domain='database' user:ui-enumeration='exclusive' user:ui-marker='enumerate'>
              <groupfilter function='crossjoin'>
                <groupfilter function='level-members' level='[none:Region:nk]' />
                <groupfilter function='level-members' level='[none:Category:nk]' />
              </groupfilter>
              <groupfilter function='union'>
{combo("West", "Furniture")}
{combo("East", "Technology")}
              </groupfilter>
            </groupfilter>
          </filter>
{slices(EXCLUSIONS)}"""
    return worksheet(
        "Region Category Matrix",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000000B",
        dependency(
            [("[Category]", "string", "dimension", "nominal"), *REGION_SALES_COLUMNS],
            [
                instance("[Category]", "None", "[none:Category:nk]", "nominal"),
                instance("[Region]", "None", "[none:Region:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        exclusions,
        pane("Text", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Region:nk"),
        ref("none:Category:nk"),
    )


def sales_by_region_group() -> str:
    """群組：East、West 合為 Coasts，沒有列入群組的成員維持原值。"""
    return worksheet(
        "Sales by Region Group",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000000C",
        dependency(
            [REGION_SALES_COLUMNS[1]],
            [
                instance("[Region (group)]", "None", "[none:Region (group):nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ).replace(
            "          </datasource-dependencies>",
            REGION_GROUP + "\n          </datasource-dependencies>",
        ),
        "",
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Region (group):nk"),
        ref("sum:Sales:qk"),
    )


def sales_by_ship_speed() -> str:
    """群組含「其他」：First Class、Same Day 合為 Fast，其餘歸入 Other。"""
    return worksheet(
        "Sales by Ship Speed",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000000D",
        dependency(
            [REGION_SALES_COLUMNS[1]],
            [
                instance("[Ship Mode (group)]", "None", "[none:Ship Mode (group):nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Ship Mode (group):nk"),
        ref("sum:Sales:qk"),
    )


def sales_by_quantity_bin() -> str:
    """數值分組：Quantity 每 3 一格，值為區間下限；顯示缺少的值（不影響數字）。"""
    return worksheet(
        "Sales by Quantity Bin",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000000E",
        dependency(
            [REGION_SALES_COLUMNS[1]],
            [
                instance("[Quantity (bin)]", "None", "[none:Quantity (bin):ok]", "ordinal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        pane("Bar"),
        ref("sum:Sales:qk"),
        ref("none:Quantity (bin):ok"),
    ).replace(
        "        <rows>",
        f"        <show-full-range>\n          <column>{escape(ref('Quantity (bin)'))}</column>\n        </show-full-range>\n        <rows>",
        1,
    )


def recent_sales() -> str:
    """相對日期：Order Date 最近 3 個月（基準日見 cases.yaml 的 today）。"""
    flt = f"""          <filter class='relative-date' column={attr(ref("none:Order Date:qk"))} first-period='-2' include-future='true' include-null='false' last-period='0' period-type-v2='month' />
{slices("none:Order Date:qk")}"""
    return worksheet(
        "Recent Sales by Category",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000000F",
        dependency(
            [
                ("[Category]", "string", "dimension", "nominal"),
                ("[Order Date]", "date", "dimension", "ordinal"),
                REGION_SALES_COLUMNS[1],
            ],
            [
                instance("[Category]", "None", "[none:Category:nk]", "nominal"),
                instance("[Order Date]", "None", "[none:Order Date:qk]", "quantitative"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        flt,
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Category:nk"),
        ref("sum:Sales:qk"),
    )


def customer_filter(expression: str, extra: str = "") -> str:
    return f"""          <filter class='categorical' column={attr(ref("none:Customer Name:nk"))}>
            <groupfilter expression={attr(expression)} function='filter' user:ui-enumeration='inclusive'{extra}>
              <groupfilter function='level-members' level='[none:Customer Name:nk]' user:ui-enumeration='all' user:ui-marker='enumerate' />
            </groupfilter>
          </filter>
{slices("none:Customer Name:nk")}"""


def big_customers() -> str:
    """彙總條件：總銷售額大於 5000 的客戶（以其他維度篩選前的資料計算），依客群計數。"""
    return worksheet(
        "Big Customers by Segment",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000014",
        dependency(
            [
                ("[Customer Name]", "string", "dimension", "nominal"),
                ("[Segment]", "string", "dimension", "nominal"),
                REGION_SALES_COLUMNS[1],
            ],
            [
                instance("[Customer Name]", "CountD", "[ctd:Customer Name:qk]", "quantitative"),
                instance("[Customer Name]", "None", "[none:Customer Name:nk]", "nominal"),
                instance("[Segment]", "None", "[none:Segment:nk]", "nominal"),
            ],
            [],
        ),
        customer_filter("SUM([Sales]) > 5000"),
        pane("Bar", f"              <text column={attr(ref('ctd:Customer Name:qk'))} />"),
        ref("none:Segment:nk"),
        ref("ctd:Customer Name:qk"),
    )


def son_customers() -> str:
    """萬用字元：客戶名稱包含 son（不分大小寫）。"""
    return worksheet(
        "Son Customers by Region",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000015",
        dependency(
            [("[Customer Name]", "string", "dimension", "nominal"), *REGION_SALES_COLUMNS],
            [
                instance("[Customer Name]", "None", "[none:Customer Name:nk]", "nominal"),
                instance("[Region]", "None", "[none:Region:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        customer_filter(
            "CONTAINS(LOWER([Customer Name]), LOWER('son'))",
            " user:ui-pattern_text='son' user:ui-pattern_type='contains'",
        ),
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Region:nk"),
        ref("sum:Sales:qk"),
    )


def top_sub_categories() -> str:
    """Top N 搭配一般選取：排除 Phones 後，銷售額前 3 名的子類別。"""
    flt = f"""          <filter class='categorical' column={attr(ref("none:Sub-Category:nk"))}>
            <groupfilter count='3' end='top' function='end' units='records' user:ui-marker='end' user:ui-top-by-field='true'>
              <groupfilter direction='DESC' expression='SUM([Sales])' function='order' user:ui-marker='order'>
                <groupfilter function='except' user:ui-domain='database' user:ui-enumeration='exclusive' user:ui-marker='enumerate'>
                  <groupfilter function='level-members' level='[none:Sub-Category:nk]' />
                  {member("[none:Sub-Category:nk]", "Phones")}
                </groupfilter>
              </groupfilter>
            </groupfilter>
          </filter>
{slices("none:Sub-Category:nk")}"""
    return worksheet(
        "Top Sub-Categories",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000016",
        dependency(
            [("[Sub-Category]", "string", "dimension", "nominal"), REGION_SALES_COLUMNS[1]],
            [
                instance("[Sub-Category]", "None", "[none:Sub-Category:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        flt,
        pane("Bar", f"              <text column={attr(ref('sum:Sales:qk'))} />"),
        ref("none:Sub-Category:nk"),
        ref("sum:Sales:qk"),
    )


def sales_growth() -> str:
    """差異與差異百分比：與前一年比較（Table (down)，沿著列上的年份）。"""

    def diff(kind: str, prefix: str) -> str:
        return instance(
            "[Sales]",
            "Sum",
            f"[{prefix}:sum:Sales:qk]",
            "quantitative",
            f"<table-calc diff-options='Relative' ordering-type='Columns' type='{kind}'>"
            "<address><value>-1</value></address></table-calc>",
        )

    encodings = "\n".join(
        [
            f"              <text column={attr(ref('diff:sum:Sales:qk'))} />",
            f"              <tooltip column={attr(ref('sum:Sales:qk'))} />",
        ]
    )
    return worksheet(
        "Sales Growth by Year",
        "7D6C2A1F-2B3C-4D5E-9F0A-000000000018",
        dependency(
            [("[Order Date]", "date", "dimension", "ordinal"), REGION_SALES_COLUMNS[1]],
            [
                instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal"),
                diff("PctDiff", "pcdf"),
                diff("Difference", "diff"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        pane("Bar", encodings),
        ref("yr:Order Date:ok"),
        ref("pcdf:sum:Sales:qk"),
    )


def selected_region_sales() -> str:
    """依參數 Selected Region 計算的銷售額（參數動作的目標）。"""
    params = PARAMETERS.replace(
        "    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>\n      <aliases enabled='yes' />",
        "          <datasource-dependencies datasource='Parameters'>",
    ).replace("    </datasource>", "          </datasource-dependencies>")
    sheet = worksheet(
        "Selected Region Sales",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000001A",
        params
        + "\n"
        + dependency(
            [("[Category]", "string", "dimension", "nominal"), *REGION_SALES_COLUMNS],
            [
                instance("[Category]", "None", "[none:Category:nk]", "nominal"),
                instance(SELECTED_CALC, "Sum", f"[sum:{SELECTED_CALC[1:-1]}:qk]", "quantitative"),
            ],
            [],
        ).replace(
            "          </datasource-dependencies>",
            SELECTED_COLUMN + "\n          </datasource-dependencies>",
        ),
        "",
        pane(
            "Bar",
            f"              <text column={attr(ref('sum:' + SELECTED_CALC[1:-1] + ':qk'))} />",
        ),
        ref("none:Category:nk"),
        ref(f"sum:{SELECTED_CALC[1:-1]}:qk"),
    )
    return sheet.replace(
        f"            <datasource caption='Orders' name='{DS}' />",
        f"            <datasource caption='Orders' name='{DS}' />\n            <datasource name='Parameters' />",
        1,
    )


def reference_line(
    n: int, formula: str, scope: str, label_type: str, extra: str = "", values: tuple[str, ...] = ()
) -> str:
    """銷售額軸上的參考線（values 為 <reference-line-value> 的屬性，例如標準差倍數）。"""
    axis = attr(ref("sum:Sales:qk"))
    head = (
        f"            <reference-line axis-column={axis} enable-instant-analytics='false' "
        f"formula='{formula}' id='refline{n}' label-type='{label_type}'{extra} probability='95' "
        f"scope='{scope}' value-column={axis} z-order='{n + 1}'"
    )
    if not values:
        return head + " />"
    body = "\n".join(f"              <reference-line-value {v} />" for v in values)
    return f"{head}>\n{body}\n            </reference-line>"


def with_reference_lines(sheet: str, *lines: str) -> str:
    return sheet.replace("          </pane>", "\n".join(lines) + "\n          </pane>", 1)


def sub_category_sales() -> str:
    """參考線：每個類別（窗格；最內層的子類別切出的是儲存格）的平均，與整張表的目標常數線。"""
    sheet = worksheet(
        "Sub-Category Sales",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000001C",
        dependency(
            [
                ("[Category]", "string", "dimension", "nominal"),
                ("[Sub-Category]", "string", "dimension", "nominal"),
                REGION_SALES_COLUMNS[1],
            ],
            [
                instance("[Category]", "None", "[none:Category:nk]", "nominal"),
                instance("[Sub-Category]", "None", "[none:Sub-Category:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        pane("Bar"),
        f"({ref('none:Category:nk')} / {ref('none:Sub-Category:nk')})",
        ref("sum:Sales:qk"),
    )
    return with_reference_lines(
        sheet,
        reference_line(0, "average", "per-pane", "custom", f" label={attr('Avg <Value>')}"),
        reference_line(
            1, "constant", "per-table", "custom", f" label={attr('目標 <Value>')} value='100000'"
        ),
    )


def region_sales_bands() -> str:
    """參考線：中位數（顯示值）與平均 ± 1 個母體標準差（對稱，不顯示標籤）。"""
    sheet = worksheet(
        "Region Sales Bands",
        "7D6C2A1F-2B3C-4D5E-9F0A-00000000001D",
        dependency(
            REGION_SALES_COLUMNS,
            [
                instance("[Region]", "None", "[none:Region:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [],
        ),
        "",
        pane("Bar"),
        ref("sum:Sales:qk"),
        ref("none:Region:nk"),
    )
    return with_reference_lines(
        sheet,
        reference_line(0, "median", "per-table", "value"),
        reference_line(
            1,
            "stdev",
            "per-table",
            "none",
            " fill-above='false' fill-below='false' symmetric='true' type='population'",
            ("factor='1'",),
        ),
    )


ACTIONS_DASHBOARD = "篩選動作"
PARAMETER_DASHBOARD = "參數動作"


def actions() -> str:
    """兩個篩選動作（清除時顯示全部／不顯示任何值）與一個醒目提示動作。"""
    source = (
        f"<source dashboard={attr(ACTIONS_DASHBOARD)} type='sheet' worksheet='Region Picker' />"
    )
    return f"""  <actions>
    <action caption='Filter Category Detail' name={attr(ACTION_CATEGORY)}>
      <activation auto-clear='true' type='on-select' />
      {source}
      <command command='tsc:tsl-filter'>
        <param name='exclude' value='Region Picker,Segment Detail,Region Category Matrix' />
        <param name='special-fields' value='all' />
        <param name='target' value={attr(ACTIONS_DASHBOARD)} />
      </command>
    </action>
    <action caption='Filter Segment Detail' name={attr(ACTION_SEGMENT)}>
      <activation auto-clear='true' type='on-select' />
      {source}
      <command command='tsc:tsl-filter'>
        <param name='exclude' value='Region Picker,Category Detail,Region Category Matrix' />
        <param name='on-empty' value='none' />
        <param name='special-fields' value='all' />
        <param name='target' value={attr(ACTIONS_DASHBOARD)} />
      </command>
    </action>
    <action caption='Search Region' name={attr(ACTION_URL)}>
      {source}
      <link caption='Search Region' delimiter=',' escape='\\' expression={attr("https://example.com/search?region=<[Region]>&sales=<SUM([Sales])>")} include-null='true' multi-select='true' url-escape='true' />
    </action>
    <action caption='Highlight Region' name={attr(ACTION_BRUSH)}>
      <activation auto-clear='true' type='on-hover' />
      <source dashboard={attr(ACTIONS_DASHBOARD)} type='sheet' worksheet='Region Category Matrix' />
      <command command='tsc:brush'>
        <param name='special-fields' value='all' />
        <param name='target' value={attr(ACTIONS_DASHBOARD)} />
      </command>
    </action>
    <nav-action caption='Go to Filter Actions' name='[Action6_7D6C2A1F2B3C4D5E9F0A000000000106]'>
      <source dashboard={attr(PARAMETER_DASHBOARD)} type='sheet' worksheet='Selected Region Sales' />
      <params>
        <param name='target' value={attr(ACTIONS_DASHBOARD)} />
      </params>
    </nav-action>
    <edit-parameter-action caption='Set Selected Region' name='[Action5_7D6C2A1F2B3C4D5E9F0A000000000105]'>
      <activation type='on-select' />
      <source dashboard={attr(PARAMETER_DASHBOARD)} type='sheet' worksheet='Region Picker' />
      <params>
        <param name='source-field' value={attr(ref("none:Region:nk"))} />
        <param name='target-parameter' value='[Parameters].[Parameter 1]' />
      </params>
    </edit-parameter-action>
  </actions>"""


def actions_dashboard() -> str:
    """篩選動作（固定尺寸）：上方來源與顯示全部的目標，下方不顯示任何值的目標與排除選取的文字表。"""
    return f"""    <dashboard name={attr(ACTIONS_DASHBOARD)}>
      <style />
      <size maxheight='{DASHBOARD_H}' maxwidth='{DASHBOARD_W}' minheight='{DASHBOARD_H}' minwidth='{DASHBOARD_W}' sizing-mode='fixed' />
      <zones>
        <zone {zone_box(0, 0, 1000, 700)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 984, 684)} id='4' param='vert' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 984, 40)} id='5' type-v2='title' />
            <zone {zone_box(8, 56, 984, 312)} id='6' param='horz' type-v2='layout-flow'>
              <zone {zone_box(8, 56, 488, 312)} id='7' name='Region Picker' />
              <zone {zone_box(504, 56, 488, 312)} id='8' name='Category Detail' />
            </zone>
            <zone {zone_box(8, 376, 984, 316)} id='9' param='horz' type-v2='layout-flow'>
              <zone {zone_box(8, 376, 488, 316)} id='10' name='Segment Detail' />
              <zone {zone_box(504, 376, 488, 316)} id='11' name='Region Category Matrix' />
            </zone>
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-000000000012}}' />
    </dashboard>"""


def groups_dashboard() -> str:
    """群組與分組（固定尺寸）：上方兩個群組，下方數值分組。"""
    return f"""    <dashboard name='群組與分組'>
      <style />
      <size maxheight='{DASHBOARD_H}' maxwidth='{DASHBOARD_W}' minheight='{DASHBOARD_H}' minwidth='{DASHBOARD_W}' sizing-mode='fixed' />
      <zones>
        <zone {zone_box(0, 0, 1000, 700)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 984, 684)} id='4' param='vert' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 984, 40)} id='5' type-v2='title' />
            <zone {zone_box(8, 56, 984, 312)} id='6' param='horz' type-v2='layout-flow'>
              <zone {zone_box(8, 56, 488, 312)} id='7' name='Sales by Region Group' />
              <zone {zone_box(504, 56, 488, 312)} id='8' name='Sales by Ship Speed' />
            </zone>
            <zone {zone_box(8, 376, 984, 316)} id='9' name='Sales by Quantity Bin' />
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-000000000013}}' />
    </dashboard>"""


def conditions_dashboard() -> str:
    """篩選條件（固定尺寸）：四張工作表與相對日期、萬用字元的篩選控制項。"""
    return f"""    <dashboard name='篩選條件'>
      <style />
      <size maxheight='{DASHBOARD_H}' maxwidth='{DASHBOARD_W}' minheight='{DASHBOARD_H}' minwidth='{DASHBOARD_W}' sizing-mode='fixed' />
      <zones>
        <zone {zone_box(0, 0, 1000, 700)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 984, 684)} id='4' param='horz' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 772, 684)} id='5' param='vert' type-v2='layout-flow'>
              <zone {zone_box(8, 8, 772, 40)} id='6' type-v2='title' />
              <zone {zone_box(8, 56, 772, 312)} id='7' param='horz' type-v2='layout-flow'>
                <zone {zone_box(8, 56, 382, 312)} id='8' name='Recent Sales by Category' />
                <zone {zone_box(398, 56, 382, 312)} id='9' name='Big Customers by Segment' />
              </zone>
              <zone {zone_box(8, 376, 772, 316)} id='10' param='horz' type-v2='layout-flow'>
                <zone {zone_box(8, 376, 382, 316)} id='11' name='Son Customers by Region' />
                <zone {zone_box(398, 376, 382, 316)} id='12' name='Top Sub-Categories' />
              </zone>
            </zone>
            <zone {zone_box(788, 56, 204, 636)} fixed-size='204' id='13' is-fixed='true' param='vert' type-v2='layout-flow'>
              <zone {zone_box(788, 56, 204, 80)} id='14' mode='relevant' name='Recent Sales by Category' param={attr(ref("none:Order Date:qk"))} type-v2='filter' />
              <zone {zone_box(788, 144, 204, 80)} id='15' mode='pattern' name='Son Customers by Region' param={attr(ref("none:Customer Name:nk"))} type-v2='filter' />
              <zone {zone_box(788, 232, 204, 460)} id='16' type-v2='empty' />
            </zone>
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-000000000017}}' />
    </dashboard>"""


def growth_dashboard() -> str:
    return f"""    <dashboard name='年度成長'>
      <style />
      <size maxheight='{DASHBOARD_H}' maxwidth='{DASHBOARD_W}' minheight='{DASHBOARD_H}' minwidth='{DASHBOARD_W}' sizing-mode='fixed' />
      <zones>
        <zone {zone_box(0, 0, 1000, 700)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 984, 684)} id='4' param='vert' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 984, 40)} id='5' type-v2='title' />
            <zone {zone_box(8, 56, 984, 636)} id='6' name='Sales Growth by Year' />
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-000000000019}}' />
    </dashboard>"""


def parameter_dashboard() -> str:
    """參數動作：點選 Region Picker 的地區設定參數 Selected Region。"""
    return f"""    <dashboard name={attr(PARAMETER_DASHBOARD)}>
      <style />
      <size maxheight='{DASHBOARD_H}' maxwidth='{DASHBOARD_W}' minheight='{DASHBOARD_H}' minwidth='{DASHBOARD_W}' sizing-mode='fixed' />
      <zones>
        <zone {zone_box(0, 0, 1000, 700)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 984, 684)} id='4' param='vert' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 984, 40)} id='5' type-v2='title' />
            <zone {zone_box(8, 56, 984, 72)} id='9' mode='compact' param='[Parameters].[Parameter 1]' type-v2='paramctrl' />
            <zone {zone_box(8, 136, 984, 556)} id='6' param='horz' type-v2='layout-flow'>
              <zone {zone_box(8, 136, 488, 556)} id='7' name='Region Picker' />
              <zone {zone_box(504, 136, 488, 556)} id='8' name='Selected Region Sales' />
            </zone>
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-00000000001B}}' />
    </dashboard>"""


def reference_dashboard() -> str:
    return f"""    <dashboard name='參考線'>
      <style />
      <size maxheight='{DASHBOARD_H}' maxwidth='{DASHBOARD_W}' minheight='{DASHBOARD_H}' minwidth='{DASHBOARD_W}' sizing-mode='fixed' />
      <zones>
        <zone {zone_box(0, 0, 1000, 700)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 984, 684)} id='4' param='vert' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 984, 40)} id='5' type-v2='title' />
            <zone {zone_box(8, 56, 984, 636)} id='6' param='horz' type-v2='layout-flow'>
              <zone {zone_box(8, 56, 580, 636)} id='7' name='Sub-Category Sales' />
              <zone {zone_box(596, 56, 396, 636)} id='8' name='Region Sales Bands' />
            </zone>
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-00000000001E}}' />
    </dashboard>"""


def dashboard() -> str:
    return f"""    <dashboard name='功能總覽'>
      <style />
      <size sizing-mode='automatic' />
      <zones>
        <zone {zone_box(0, 0, 1000, 700)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 984, 684)} id='4' param='horz' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 764, 684)} id='5' param='vert' type-v2='layout-flow'>
              <zone {zone_box(8, 8, 764, 40)} id='6' type-v2='title' />
              <zone {zone_box(8, 56, 764, 316)} id='16' param='horz' type-v2='layout-flow'>
                <zone {zone_box(8, 56, 376, 316)} id='7' name='Sales by Segment' />
                <zone {zone_box(392, 56, 380, 316)} id='8' name='Profit by Ship Mode' />
              </zone>
              <zone {zone_box(8, 380, 764, 312)} id='9' name='Orders by Region' show-title='false' />
            </zone>
            <zone {zone_box(780, 56, 212, 636)} fixed-size='212' id='10' is-fixed='true' param='vert' type-v2='layout-flow'>
              <zone {zone_box(780, 56, 212, 96)} id='11' mode='checkdropdown' name='Sales by Segment' param={attr(ref("none:Segment:nk"))} type-v2='filter' />
              <zone {zone_box(780, 160, 212, 96)} id='12' mode='checkdropdown' name='Orders by Region' param={attr(ref("none:Region:nk"))} type-v2='filter' />
              <zone {zone_box(780, 264, 212, 96)} id='13' mode='checkdropdown' name='Profit by Ship Mode' param={attr(ref("none:Ship Mode:nk"))} type-v2='filter' />
              <zone {zone_box(780, 368, 212, 96)} fixed-size='96' hidden-by-user='true' id='15' is-fixed='true' type-v2='text'>
                <formatted-text>
                  <run>隱藏的說明文字</run>
                </formatted-text>
              </zone>
              <zone {zone_box(780, 368, 212, 324)} id='14' type-v2='empty' />
            </zone>
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-000000000010}}' />
    </dashboard>"""


def charts_dashboard() -> str:
    """擴充的圖表：Measure Names／Values、雙軸與圓餅（固定尺寸）。"""
    w, h = 1200, 800

    def box(x: int, y: int, bw: int, bh: int) -> str:
        return (
            f"h='{round(bh * 100000 / h)}' w='{round(bw * 100000 / w)}' "
            f"x='{round(x * 100000 / w)}' y='{round(y * 100000 / h)}'"
        )

    return f"""    <dashboard name='圖表擴充'>
      <style />
      <size maxheight='{h}' maxwidth='{w}' minheight='{h}' minwidth='{w}' sizing-mode='fixed' />
      <zones>
        <zone {box(0, 0, 1200, 800)} id='3' type-v2='layout-basic'>
          <zone {box(8, 8, 1184, 784)} id='4' param='vert' type-v2='layout-flow'>
            <zone {box(8, 8, 1184, 388)} id='5' param='horz' type-v2='layout-flow'>
              <zone {box(8, 8, 588, 388)} id='6' name='Segment KPIs' />
              <zone {box(604, 8, 588, 388)} id='7' name='Monthly Sales vs Profit' />
            </zone>
            <zone {box(8, 404, 1184, 388)} id='8' param='horz' type-v2='layout-flow'>
              <zone {box(8, 404, 588, 388)} id='9' name='Sales and Profit by Year' />
              <zone {box(604, 404, 588, 388)} id='10' name='Category Mix' />
            </zone>
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-000000000011}}' />
    </dashboard>"""


def twb(rows: int) -> str:
    return f"""<?xml version='1.0' encoding='utf-8' ?>

<!-- build 20242.24.0613.0805                               -->
<workbook original-version='18.1' source-build='2024.2.0 (20242.24.0613.0805)' source-platform='win' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <document-format-change-manifest>
    <ObjectModelEncapsulateLegacy />
    <_.fcp.ObjectModelEncapsulateLegacy.true...ObjectModelEncapsulateLegacy />
    <SheetIdentifierTracking />
    <WindowsPersistSimpleIdentifiers />
  </document-format-change-manifest>
  <preferences>
    <preference name='ui.encoding.shelf.height' value='24' />
    <preference name='ui.shelf.height' value='26' />
  </preferences>
  <datasources>
{PARAMETERS}
{demo.datasource(rows, "\n".join([REGION_GROUP, SHIP_SPEED, QUANTITY_BIN, SELECTED_COLUMN, DATASOURCE_GROUPS]))}
  </datasources>
{shared_views()}
{actions()}
  <worksheets>
{worksheets()}
  </worksheets>
  <dashboards>
{dashboard()}
{charts_dashboard()}
{actions_dashboard()}
{groups_dashboard()}
{conditions_dashboard()}
{growth_dashboard()}
{parameter_dashboard()}
{reference_dashboard()}
  </dashboards>
  <windows source-height='30'>
    <window class='dashboard' maximized='true' name='功能總覽'>
      <viewpoints />
      <active id='-1' />
      <simple-id uuid='{{7D6C2A1F-2B3C-4D5E-9F0A-000000000020}}' />
    </window>
  </windows>
</workbook>
"""


def main() -> None:
    rows = demo.read_rows()
    FIXTURE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        hyper = work / "extract.hyper"
        demo.build_hyper(rows, hyper, work)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in (
                ("superstore_features.twb", twb(len(rows)).encode("utf-8")),
                (demo.HYPER_PATH, hyper.read_bytes()),
            ):
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 28, 9, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, payload)
        TWBX.write_bytes(data.getvalue())
    print(f"已寫入 {TWBX.relative_to(ROOT)}（{len(rows)} 列，{TWBX.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
