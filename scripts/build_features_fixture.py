"""建立擴充階段的功能報表 fixtures/superstore_features/superstore_features.twbx。

    uv run python scripts/build_features_fixture.py

資料與資料源沿用示範報表（scripts/build_demo_fixture.py），.twb 依真實 Tableau 檔案的結構手寫，
每加入一項擴充功能就在這裡補上對應的工作表或 dashboard 設定：

- H1 全域篩選（<shared-views>）與篩選群組（filter-group）：Region 為全域篩選，Segment 篩選套用到兩張工作表
- I1 自動尺寸的 dashboard：右側篩選欄固定 212 像素寬、一個工作表不顯示標題、一個隱藏的文字區塊
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
    return "\n".join([sales_by_segment, profit_by_ship_mode, orders_by_region])


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
{demo.datasource(rows)}
  </datasources>
{shared_views()}
  <worksheets>
{worksheets()}
  </worksheets>
  <dashboards>
{dashboard()}
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
