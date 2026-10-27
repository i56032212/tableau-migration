"""組出測試用的最小 .twb：一個嵌入式萃取資料源，加上指定的工作表、dashboard 等片段。"""

from __future__ import annotations

import zipfile
from pathlib import Path

DS = "federated.test01"

COLUMNS = [
    ("Region", "string", "Count"),
    ("Category", "string", "Count"),
    ("Order ID", "string", "Count"),
    ("Order Date", "date", "Year"),
    ("Sales", "real", "Sum"),
    ("Profit", "real", "Sum"),
    ("Quantity", "integer", "Sum"),
]

PARAMS = """
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      <column caption='Top N' datatype='integer' name='[Parameter 1]' param-domain-type='range' role='measure' type='quantitative' value='5'>
        <calculation class='tableau' formula='5' />
        <range granularity='1' max='10' min='1' />
      </column>
      <column caption='Region Pick' datatype='string' name='[Parameter 2]' param-domain-type='list' role='measure' type='nominal' value='&quot;East&quot;'>
        <calculation class='tableau' formula='&quot;East&quot;' />
        <members>
          <member value='&quot;East&quot;' />
          <member value='&quot;West&quot;' />
        </members>
      </column>
      <column caption='Start' datatype='date' name='[Parameter 3]' param-domain-type='any' role='measure' type='quantitative' value='#2024-01-01#'>
        <calculation class='tableau' formula='#2024-01-01#' />
      </column>
    </datasource>"""


def metadata(parent: str = "[Extract]", rename: dict[str, str] | None = None) -> str:
    rename = rename or {}
    records = []
    for i, (name, datatype, agg) in enumerate(COLUMNS):
        records.append(
            f"""<metadata-record class='column'>
              <remote-name>{rename.get(name, name)}</remote-name>
              <remote-type>129</remote-type>
              <local-name>[{name}]</local-name>
              <parent-name>{parent}</parent-name>
              <ordinal>{i}</ordinal>
              <local-type>{datatype}</local-type>
              <aggregation>{agg}</aggregation>
            </metadata-record>"""
        )
    return "<metadata-records>" + "\n".join(records) + "</metadata-records>"


def extract_datasource(
    columns: str = "",
    extra: str = "",
    extract_inner: str | None = None,
    name: str = DS,
    caption: str = "Orders",
) -> str:
    inner = extract_inner if extract_inner is not None else metadata()
    return f"""
    <datasource caption='{caption}' inline='true' name='{name}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='src' name='textscan.1'>
            <connection class='textscan' directory='C:/data' filename='orders.csv' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.1' name='orders.csv' table='[orders#csv]' type='table' />
        {metadata("[orders.csv]")}
      </connection>
      <extract count='-1' enabled='true' units='records'>
        <connection class='hyper' dbname='Data/Extracts/test.hyper' schema='Extract' tablename='Extract'>
          <relation name='Extract' table='[Extract].[Extract]' type='table' />
          {inner}
        </connection>
      </extract>
      <aliases enabled='yes' />
      {columns}
      {extra}
    </datasource>"""


def calc(
    name: str, caption: str, formula: str, datatype: str = "real", role: str = "measure"
) -> str:
    from xml.sax.saxutils import quoteattr

    return (
        f"<column caption={quoteattr(caption)} datatype='{datatype}' name='[{name}]' role='{role}' type='quantitative'>"
        f"<calculation class='tableau' formula={quoteattr(formula)} /></column>"
    )


def instance(
    column: str, derivation: str, name: str, kind: str = "nominal", table_calc: str = ""
) -> str:
    from xml.sax.saxutils import quoteattr

    head = (
        f"<column-instance column={quoteattr(column)} derivation='{derivation}' "
        f"name={quoteattr(name)} pivot='key' type='{kind}'"
    )
    return head + (f">{table_calc}</column-instance>" if table_calc else " />")


def ref(inst: str) -> str:
    return f"[{DS}].[{inst}]"


def worksheet(
    name: str,
    instances: list[str],
    rows: str = "",
    cols: str = "",
    mark: str = "Bar",
    encodings: str = "",
    view_extra: str = "",
    pane_extra: str = "",
    panes_extra: str = "",
    datasources: str = "",
    table_extra: str = "",
    deps_extra: str = "",
) -> str:
    from xml.sax.saxutils import escape

    sources = datasources or f"<datasource caption='Orders' name='{DS}' />"
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources>{sources}</datasources>
          <datasource-dependencies datasource='{DS}'>
            {deps_extra}
            {"".join(instances)}
          </datasource-dependencies>
          {view_extra}
          <aggregation value='true' />
        </view>
        <style />
        <panes>
          <pane>
            <view><breakdown value='auto' /></view>
            <mark class='{mark}' />
            <encodings>{encodings}</encodings>
            {pane_extra}
          </pane>
          {panes_extra}
        </panes>
        <rows>{escape(rows)}</rows>
        <cols>{escape(cols)}</cols>
        {table_extra}
      </table>
    </worksheet>"""


def workbook(
    datasource: str | None = None,
    worksheets: str = "",
    dashboards: str = "",
    actions: str = "",
    params: bool = True,
    extra: str = "",
) -> str:
    ds = datasource if datasource is not None else extract_datasource()
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.2.0' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences />
  <datasources>
    {PARAMS if params else ""}
    {ds}
  </datasources>
  <worksheets>{worksheets}</worksheets>
  <dashboards>{dashboards}</dashboards>
  {f"<actions>{actions}</actions>" if actions else ""}
  {extra}
  <windows />
</workbook>
"""


def write_twb(tmp_path: Path, xml: str, name: str = "report.twb") -> Path:
    path = tmp_path / name
    path.write_text(xml, encoding="utf-8")
    return path


def write_twbx(
    tmp_path: Path, xml: str, files: dict[str, bytes], name: str = "report.twbx"
) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("report.twb", xml.encode("utf-8"))
        for member, data in files.items():
            archive.writestr(member, data)
    return path
