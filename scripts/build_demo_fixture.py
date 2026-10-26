"""建立示範報表 fixtures/superstore_demo/superstore_demo.twbx。

    uv run python scripts/build_demo_fixture.py

步驟：讀 source/orders.csv.gz → 以 Tableau Hyper API 建立萃取（schema Extract、table Extract，
與 Tableau 萃取相同）→ 產生仿真 .twb → 打包成 .twbx。

.twb 依照真實 Tableau 2024.2 檔案的結構手寫，涵蓋起步階段要驗證的功能：
資料源篩選（排除加拿大的 200 列）、維度篩選、Top N（參數）、計算排序、快速 Table Calculation、彙總計算、
FIXED 與 INCLUDE LOD、日期截斷與日期部分、量值篩選、Table Calculation 篩選、
固定尺寸 dashboard 與篩選、參數、圖例控制項。計算欄位與參數使用中文名稱。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from tableauhyperapi import (
    Connection,
    CreateMode,
    HyperProcess,
    Inserter,
    SqlType,
    TableDefinition,
    TableName,
    Telemetry,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "superstore_demo"
SOURCE = FIXTURE / "source" / "orders.csv.gz"
TWBX = FIXTURE / "superstore_demo.twbx"

DS = "federated.0s2xs1a1g5wq1h17oo8mq0b6k5ry"
HYPER_PATH = f"Data/Extracts/{DS.replace('.', '_')}.hyper"

# 欄位：名稱、Hyper 型別、Tableau 型別、Tableau remote-type、預設彙總
COLUMNS: list[tuple[str, str, str, int, str]] = [
    ("Row ID", "big_int", "integer", 20, "Sum"),
    ("Order ID", "text", "string", 129, "Count"),
    ("Order Date", "date", "date", 133, "Year"),
    ("Ship Date", "date", "date", 133, "Year"),
    ("Ship Mode", "text", "string", 129, "Count"),
    ("Customer ID", "text", "string", 129, "Count"),
    ("Customer Name", "text", "string", 129, "Count"),
    ("Segment", "text", "string", 129, "Count"),
    ("Country/Region", "text", "string", 129, "Count"),
    ("City", "text", "string", 129, "Count"),
    ("State/Province", "text", "string", 129, "Count"),
    ("Postal Code", "text", "string", 129, "Count"),
    ("Region", "text", "string", 129, "Count"),
    ("Product ID", "text", "string", 129, "Count"),
    ("Category", "text", "string", 129, "Count"),
    ("Sub-Category", "text", "string", 129, "Count"),
    ("Product Name", "text", "string", 129, "Count"),
    ("Sales", "double", "real", 5, "Sum"),
    ("Quantity", "big_int", "integer", 20, "Sum"),
    ("Discount", "double", "real", 5, "Sum"),
    ("Profit", "double", "real", 5, "Sum"),
]

# 計算欄位：內部名稱、顯示名稱、型別、角色、公式、格式
CALC_PROFIT_RATIO = "[Calculation_1368249927221915648]"
CALC_REGION_SALES = "[Calculation_2049003526124437504]"
CALC_REGION_SHARE = "[Calculation_2049003526124994561]"
CALC_PROFIT_RANK = "[Calculation_5418932014720188416]"
CALC_AVG_ORDER_PROFIT = "[Calculation_5418932014720745473]"
CALCS: list[tuple[str, str, str, str, str, str | None]] = [
    (CALC_PROFIT_RATIO, "利潤率", "real", "measure", "SUM([Profit])/SUM([Sales])", "p0.0%"),
    (
        CALC_REGION_SALES,
        "區域總銷售",
        "real",
        "measure",
        "{FIXED [Region] : SUM([Sales])}",
        'c"$"#,##0',
    ),
    (
        CALC_REGION_SHARE,
        "區域占比",
        "real",
        "measure",
        "// 該年度銷售占區域全部銷售的比例\r\nSUM([Sales]) / SUM(" + CALC_REGION_SALES + ")",
        "p0.0%",
    ),
    (CALC_PROFIT_RANK, "利潤排名", "integer", "measure", "RANK(SUM([Profit]))", None),
    (
        CALC_AVG_ORDER_PROFIT,
        "平均訂單利潤",
        "real",
        "measure",
        "AVG({INCLUDE [Order ID] : SUM([Profit])})",
        'c"$"#,##0.00',
    ),
]

DASHBOARD_W, DASHBOARD_H = 1200, 800


def attr(value: str) -> str:
    """以單引號包住的 XML 屬性值。"""
    return "'" + escape(value, {"'": "&apos;", '"': "&quot;"}) + "'"


def ref(instance: str) -> str:
    return f"[{DS}].[{instance}]"


# ---------------------------------------------------------------- Hyper


def read_rows() -> list[list[object]]:
    with gzip.open(SOURCE, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == [c[0] for c in COLUMNS], header
        rows: list[list[object]] = []
        for raw in reader:
            row: list[object] = []
            for (_, hyper_type, *_), value in zip(COLUMNS, raw, strict=True):
                if value == "":
                    row.append(None)
                elif hyper_type == "big_int":
                    row.append(int(value))
                elif hyper_type == "double":
                    row.append(float(value))
                elif hyper_type == "date":
                    row.append(dt.date.fromisoformat(value))
                else:
                    row.append(value)
            rows.append(row)
    return rows


def build_hyper(rows: list[list[object]], path: Path, log_dir: Path) -> None:
    types = {
        "big_int": SqlType.big_int(),
        "double": SqlType.double(),
        "date": SqlType.date(),
        "text": SqlType.text(),
    }
    table = TableDefinition(
        TableName("Extract", "Extract"),
        [TableDefinition.Column(name, types[hyper_type]) for name, hyper_type, *_ in COLUMNS],
    )
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        conn.catalog.create_table(table)
        with Inserter(conn, table) as inserter:
            inserter.add_rows(rows)
            inserter.execute()


# ---------------------------------------------------------------- .twb


def metadata_records(parent: str, family: str | None = None, indent: str = "          ") -> str:
    """欄位的 metadata-record。萃取連線內的版本 parent 為 [Extract]，並以 family 記錄原始資料表。"""
    out = []
    family_line = f"\n{indent}  <family>{escape(family)}</family>" if family else ""
    for ordinal, (name, _, datatype, remote_type, aggregation) in enumerate(COLUMNS):
        out.append(
            f"""{indent}<metadata-record class='column'>
{indent}  <remote-name>{escape(name)}</remote-name>
{indent}  <remote-type>{remote_type}</remote-type>
{indent}  <local-name>{escape(f"[{name}]")}</local-name>
{indent}  <parent-name>{escape(parent)}</parent-name>
{indent}  <remote-alias>{escape(name)}</remote-alias>
{indent}  <ordinal>{ordinal}</ordinal>{family_line}
{indent}  <local-type>{datatype}</local-type>
{indent}  <aggregation>{aggregation}</aggregation>
{indent}  <contains-null>true</contains-null>
{indent}</metadata-record>"""
        )
    return "\n".join(out)


def calc_columns(indent: str = "      ") -> str:
    out = []
    for name, caption, datatype, role, formula, fmt in CALCS:
        fmt_attr = f" default-format={attr(fmt)}" if fmt else ""
        out.append(
            f"{indent}<column caption={attr(caption)} datatype={attr(datatype)}{fmt_attr} "
            f"name={attr(name)} role={attr(role)} type='quantitative'>\n"
            f"{indent}  <calculation class='tableau' formula={attr(formula)} />\n"
            f"{indent}</column>"
        )
    return "\n".join(out)


def calc_column(name: str, indent: str) -> str:
    for calc_name, caption, datatype, role, formula, fmt in CALCS:
        if calc_name == name:
            fmt_attr = f" default-format={attr(fmt)}" if fmt else ""
            return (
                f"{indent}<column caption={attr(caption)} datatype={attr(datatype)}{fmt_attr} "
                f"name={attr(name)} role={attr(role)} type='quantitative'>\n"
                f"{indent}  <calculation class='tableau' formula={attr(formula)} />\n"
                f"{indent}</column>"
            )
    raise KeyError(name)


PARAMETER = """    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      <column caption='前 N 名' datatype='integer' name='[Parameter 1]' param-domain-type='range' role='measure' type='quantitative' value='3'>
        <calculation class='tableau' formula='3' />
        <range granularity='1' max='4' min='1' />
      </column>
    </datasource>"""

PARAMETER_DEPENDENCY = """          <datasource-dependencies datasource='Parameters'>
            <column caption='前 N 名' datatype='integer' name='[Parameter 1]' param-domain-type='range' role='measure' type='quantitative' value='3'>
              <calculation class='tableau' formula='3' />
              <range granularity='1' max='4' min='1' />
            </column>
          </datasource-dependencies>"""


def datasource(rows: int) -> str:
    cols_map = "\n".join(
        f"          <map key={attr(f'[{name}]')} value={attr(f'[orders.csv].[{name}]')} />"
        for name, *_ in sorted(COLUMNS)
    )
    csv_columns = "\n".join(
        f"            <column datatype={attr(datatype)} name={attr(name)} ordinal='{i}' />"
        for i, (name, _, datatype, _, _) in enumerate(COLUMNS)
    )
    return f"""    <datasource caption='Orders' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='orders' name='textscan.1q2w3e4r5t6y7u8i9o0p'>
            <connection class='textscan' directory='C:/Data/Superstore' filename='orders.csv' password='' server='' />
          </named-connection>
        </named-connections>
        <_.fcp.ObjectModelEncapsulateLegacy.false...relation connection='textscan.1q2w3e4r5t6y7u8i9o0p' name='orders.csv' table='[orders#csv]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
{csv_columns}
          </columns>
        </_.fcp.ObjectModelEncapsulateLegacy.false...relation>
        <_.fcp.ObjectModelEncapsulateLegacy.true...relation connection='textscan.1q2w3e4r5t6y7u8i9o0p' name='orders.csv' table='[orders#csv]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
{csv_columns}
          </columns>
        </_.fcp.ObjectModelEncapsulateLegacy.true...relation>
        <cols>
{cols_map}
        </cols>
        <metadata-records>
{metadata_records("[orders.csv]")}
        </metadata-records>
      </connection>
      <extract count='-1' enabled='true' object-id='' units='records'>
        <connection access_mode='readonly' authentication='auth-none' author-locale='en_US' class='hyper' dbname='{HYPER_PATH}' default-settings='hyper' schema='Extract' sslmode='' tablename='Extract' update-time='10/20/2026 09:00:00 AM' username='tableau-internal-user'>
          <_.fcp.ObjectModelEncapsulateLegacy.false...relation name='Extract' table='[Extract].[Extract]' type='table' />
          <_.fcp.ObjectModelEncapsulateLegacy.true...relation name='Extract' table='[Extract].[Extract]' type='table' />
          <refresh>
            <refresh-event add-from-file-path='orders' increment-value='%null%' refresh-type='create' rows-inserted='{rows}' timestamp-start='2026-10-20 09:00:00.000' />
          </refresh>
          <metadata-records>
{metadata_records("[Extract]", "orders.csv", "            ")}
          </metadata-records>
        </connection>
      </extract>
      <aliases enabled='yes' />
{calc_columns()}
      <column datatype='string' name='[Postal Code]' role='dimension' semantic-role='[ZipCode].[Name]' type='nominal' />
      <column datatype='integer' hidden='true' name='[Row ID]' role='dimension' type='ordinal' />
      <column datatype='string' name='[State/Province]' role='dimension' semantic-role='[State].[Name]' type='nominal' />
      <column-instance column='[Country/Region]' derivation='None' name='[none:Country/Region:nk]' pivot='key' type='nominal' />
      <filter class='categorical' column='[none:Country/Region:nk]' filter-group='2'>
        <groupfilter function='member' level='[none:Country/Region:nk]' member='&quot;United States&quot;' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />
      </filter>
      <layout dim-ordering='alphabetic' measure-ordering='alphabetic' show-structure='true' />
      <semantic-values>
        <semantic-value key='[Country].[Name]' value='&quot;United States&quot;' />
      </semantic-values>
    </datasource>"""


def dependency(
    columns: list[tuple[str, str, str, str]], instances: list[str], calcs: list[str]
) -> str:
    lines = [f"          <datasource-dependencies datasource='{DS}'>"]
    for name in calcs:
        lines.append(calc_column(name, "            "))
    for name, datatype, role, kind in columns:
        lines.append(
            f"            <column datatype={attr(datatype)} name={attr(name)} role={attr(role)} type={attr(kind)} />"
        )
    lines += [f"            {line}" for line in instances]
    lines.append("          </datasource-dependencies>")
    return "\n".join(lines)


def instance(
    column: str, derivation: str, name: str, kind: str, table_calc: str | None = None
) -> str:
    head = f"<column-instance column={attr(column)} derivation={attr(derivation)} name={attr(name)} pivot='key' type={attr(kind)}"
    if table_calc is None:
        return head + " />"
    return head + f">\n              {table_calc}\n            </column-instance>"


def member(level: str, value: str) -> str:
    return f"<groupfilter function='member' level={attr(level)} member={attr(chr(34) + value + chr(34))} />"


def worksheet(
    name: str,
    uuid: str,
    deps: str,
    view_extra: str,
    mark: str,
    encodings: str,
    rows: str,
    cols: str,
    params: bool = False,
) -> str:
    sources = f"            <datasource caption='Orders' name='{DS}' />"
    if params:
        sources += "\n            <datasource name='Parameters' />"
    param_dep = PARAMETER_DEPENDENCY + "\n" if params else ""
    return f"""    <worksheet name={attr(name)}>
      <table>
        <view>
          <datasources>
{sources}
          </datasources>
{param_dep}{deps}
{view_extra}
          <aggregation value='true' />
        </view>
        <style />
        <panes>
          <pane selection-relaxation-option='selection-relaxation-allow'>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class={attr(mark)} />
            <encodings>
{encodings}
            </encodings>
          </pane>
        </panes>
        <rows>{escape(rows)}</rows>
        <cols>{escape(cols)}</cols>
      </table>
      <simple-id uuid='{{{uuid}}}' />
    </worksheet>"""


def worksheets() -> str:
    sales_by_region = worksheet(
        "Sales by Region",
        "6C5B1F0E-1A2B-4C3D-8E9F-000000000001",
        dependency(
            [
                ("[Category]", "string", "dimension", "nominal"),
                ("[Region]", "string", "dimension", "nominal"),
                ("[Sales]", "real", "measure", "quantitative"),
            ],
            [
                instance("[Category]", "None", "[none:Category:nk]", "nominal"),
                instance("[Region]", "None", "[none:Region:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
                instance(
                    "[Sales]",
                    "Sum",
                    "[cum:sum:Sales:qk]",
                    "quantitative",
                    "<table-calc aggregation='Sum' ordering-type='Rows' type='CumTotal' />",
                ),
            ],
            [],
        ),
        f"""          <filter class='categorical' column={attr(ref("none:Category:nk"))}>
            <groupfilter function='union' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate'>
              {member("[none:Category:nk]", "Furniture")}
              {member("[none:Category:nk]", "Technology")}
            </groupfilter>
          </filter>
          <filter class='categorical' column={attr(ref("none:Region:nk"))}>
            <groupfilter count='[Parameters].[Parameter 1]' end='top' function='end' units='records' user:ui-marker='end' user:ui-top-by-field='true'>
              <groupfilter direction='DESC' expression='SUM([Sales])' function='order' user:ui-marker='order'>
                <groupfilter function='level-members' level='[none:Region:nk]' user:ui-enumeration='all' user:ui-marker='enumerate' />
              </groupfilter>
            </groupfilter>
          </filter>
          <sort class='computed' column={attr(ref("none:Region:nk"))} direction='DESC' using={attr(ref("sum:Sales:qk"))} />
          <slices>
            <column>{escape(ref("none:Category:nk"))}</column>
            <column>{escape(ref("none:Region:nk"))}</column>
          </slices>""",
        "Bar",
        f"""              <color column={attr(ref("none:Category:nk"))} />
              <text column={attr(ref("sum:Sales:qk"))} />
              <tooltip column={attr(ref("cum:sum:Sales:qk"))} />""",
        ref("none:Region:nk"),
        ref("sum:Sales:qk"),
        params=True,
    )
    monthly_profit = worksheet(
        "Monthly Profit",
        "6C5B1F0E-1A2B-4C3D-8E9F-000000000002",
        dependency(
            [
                ("[Order Date]", "date", "dimension", "ordinal"),
                ("[Profit]", "real", "measure", "quantitative"),
                ("[Sales]", "real", "measure", "quantitative"),
            ],
            [
                instance(
                    CALC_PROFIT_RATIO,
                    "User",
                    "[usr:Calculation_1368249927221915648:qk]",
                    "quantitative",
                ),
                instance("[Order Date]", "None", "[none:Order Date:qk]", "quantitative"),
                instance("[Order Date]", "Month-Trunc", "[tmn:Order Date:qk]", "quantitative"),
                instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            ],
            [CALC_PROFIT_RATIO],
        ),
        f"""          <filter class='quantitative' column={attr(ref("none:Order Date:qk"))} included-values='in-range'>
            <min>#2023-01-01#</min>
            <max>#2024-12-31#</max>
          </filter>
          <filter class='quantitative' column={attr(ref("sum:Sales:qk"))} included-values='in-range'>
            <min>20000</min>
          </filter>
          <slices>
            <column>{escape(ref("none:Order Date:qk"))}</column>
            <column>{escape(ref("sum:Sales:qk"))}</column>
          </slices>""",
        "Line",
        f"""              <tooltip column={attr(ref("usr:Calculation_1368249927221915648:qk"))} />""",
        ref("sum:Profit:qk"),
        ref("tmn:Order Date:qk"),
    )
    region_share = worksheet(
        "Region Share by Year",
        "6C5B1F0E-1A2B-4C3D-8E9F-000000000003",
        dependency(
            [
                ("[Order Date]", "date", "dimension", "ordinal"),
                ("[Region]", "string", "dimension", "nominal"),
                ("[Sales]", "real", "measure", "quantitative"),
                ("[Segment]", "string", "dimension", "nominal"),
            ],
            [
                instance(
                    CALC_REGION_SHARE,
                    "User",
                    "[usr:Calculation_2049003526124994561:qk]",
                    "quantitative",
                ),
                instance("[Order Date]", "Year", "[yr:Order Date:ok]", "ordinal"),
                instance("[Region]", "None", "[none:Region:nk]", "nominal"),
                instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
                instance("[Segment]", "None", "[none:Segment:nk]", "nominal"),
            ],
            [CALC_REGION_SALES, CALC_REGION_SHARE],
        ),
        f"""          <filter class='categorical' column={attr(ref("none:Segment:nk"))}>
            <groupfilter function='union' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate'>
              {member("[none:Segment:nk]", "Consumer")}
              {member("[none:Segment:nk]", "Corporate")}
            </groupfilter>
          </filter>
          <slices>
            <column>{escape(ref("none:Segment:nk"))}</column>
          </slices>""",
        "Text",
        f"""              <text column={attr(ref("usr:Calculation_2049003526124994561:qk"))} />
              <tooltip column={attr(ref("sum:Sales:qk"))} />""",
        ref("none:Region:nk"),
        ref("yr:Order Date:ok"),
    )
    sub_category = worksheet(
        "Sub-Category Profit",
        "6C5B1F0E-1A2B-4C3D-8E9F-000000000004",
        dependency(
            [
                ("[Order ID]", "string", "dimension", "nominal"),
                ("[Profit]", "real", "measure", "quantitative"),
                ("[Sub-Category]", "string", "dimension", "nominal"),
            ],
            [
                instance(
                    CALC_AVG_ORDER_PROFIT,
                    "User",
                    "[usr:Calculation_5418932014720745473:qk]",
                    "quantitative",
                ),
                instance(
                    CALC_PROFIT_RANK,
                    "User",
                    "[usr:Calculation_5418932014720188416:qk]",
                    "quantitative",
                    "<table-calc ordering-type='Rows' />",
                ),
                instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative"),
                instance("[Sub-Category]", "None", "[none:Sub-Category:nk]", "nominal"),
            ],
            [CALC_AVG_ORDER_PROFIT, CALC_PROFIT_RANK],
        ),
        f"""          <filter class='quantitative' column={attr(ref("usr:Calculation_5418932014720188416:qk"))} included-values='in-range'>
            <max>10</max>
          </filter>
          <sort class='computed' column={attr(ref("none:Sub-Category:nk"))} direction='DESC' using={attr(ref("sum:Profit:qk"))} />
          <slices>
            <column>{escape(ref("usr:Calculation_5418932014720188416:qk"))}</column>
          </slices>""",
        "Bar",
        f"""              <text column={attr(ref("usr:Calculation_5418932014720188416:qk"))} />
              <tooltip column={attr(ref("usr:Calculation_5418932014720745473:qk"))} />""",
        ref("none:Sub-Category:nk"),
        ref("sum:Profit:qk"),
    )
    return "\n".join([sales_by_region, monthly_profit, region_share, sub_category])


def zone_box(x: int, y: int, w: int, h: int) -> str:
    """像素座標換成 Tableau 的 100000 分之一單位。"""

    def u(px: int, total: int) -> int:
        return round(px * 100000 / total)

    return (
        f"h='{u(h, DASHBOARD_H)}' w='{u(w, DASHBOARD_W)}' "
        f"x='{u(x, DASHBOARD_W)}' y='{u(y, DASHBOARD_H)}'"
    )


def dashboard() -> str:
    return f"""    <dashboard name='銷售總覽'>
      <style />
      <size maxheight='{DASHBOARD_H}' maxwidth='{DASHBOARD_W}' minheight='{DASHBOARD_H}' minwidth='{DASHBOARD_W}' sizing-mode='fixed' />
      <zones>
        <zone {zone_box(0, 0, 1200, 800)} id='3' type-v2='layout-basic'>
          <zone {zone_box(8, 8, 1184, 784)} id='4' param='horz' type-v2='layout-flow'>
            <zone {zone_box(8, 8, 944, 784)} id='5' param='vert' type-v2='layout-flow'>
              <zone {zone_box(8, 8, 944, 40)} id='6' type-v2='title' />
              <zone {zone_box(8, 56, 468, 364)} id='7' name='Sales by Region' />
              <zone {zone_box(484, 56, 468, 364)} id='8' name='Monthly Profit' />
              <zone {zone_box(8, 428, 468, 364)} id='9' name='Region Share by Year' />
              <zone {zone_box(484, 428, 468, 364)} id='10' name='Sub-Category Profit' />
            </zone>
            <zone {zone_box(960, 56, 232, 736)} id='11' param='vert' type-v2='layout-flow'>
              <zone {zone_box(960, 56, 232, 96)} id='12' mode='checkdropdown' name='Sales by Region' param={attr(ref("none:Category:nk"))} type-v2='filter' />
              <zone {zone_box(960, 160, 232, 72)} id='13' mode='slider' param='[Parameters].[Parameter 1]' type-v2='paramctrl' />
              <zone {zone_box(960, 240, 232, 96)} id='14' mode='checkdropdown' name='Region Share by Year' param={attr(ref("none:Segment:nk"))} type-v2='filter' />
              <zone {zone_box(960, 344, 232, 120)} id='15' name='Sales by Region' pane-specification-id='0' param={attr(ref("none:Category:nk"))} type-v2='color' />
              <zone {zone_box(960, 472, 232, 320)} id='16' type-v2='empty' />
            </zone>
          </zone>
        </zone>
      </zones>
      <simple-id uuid='{{6C5B1F0E-1A2B-4C3D-8E9F-000000000010}}' />
    </dashboard>"""


def twb(rows: int) -> str:
    return f"""<?xml version='1.0' encoding='utf-8' ?>

<!-- build 20242.24.0613.0805                               -->
<workbook original-version='18.1' source-build='2024.2.0 (20242.24.0613.0805)' source-platform='win' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <document-format-change-manifest>
    <AnimationOnByDefault />
    <MarkAnimation />
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
{PARAMETER}
{datasource(rows)}
  </datasources>
  <worksheets>
{worksheets()}
  </worksheets>
  <dashboards>
{dashboard()}
  </dashboards>
  <windows source-height='30'>
    <window class='dashboard' maximized='true' name='銷售總覽'>
      <viewpoints />
      <active id='-1' />
      <simple-id uuid='{{6C5B1F0E-1A2B-4C3D-8E9F-000000000020}}' />
    </window>
  </windows>
</workbook>
"""


def main() -> None:
    rows = read_rows()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        hyper = work / "extract.hyper"
        build_hyper(rows, hyper, work)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in (
                ("superstore_demo.twb", twb(len(rows)).encode("utf-8")),
                (HYPER_PATH, hyper.read_bytes()),
            ):
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 20, 9, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, payload)
        TWBX.write_bytes(data.getvalue())
    print(f"已寫入 {TWBX.relative_to(ROOT)}（{len(rows)} 列，{TWBX.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
