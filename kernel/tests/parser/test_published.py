"""第三階段 N8：Published Data Source 的 .tdsx 併入報表。

報表連到 Tableau Server 上的「OrdersPDS」（sqlproxy）：.twb 只有欄位的快取；資料源的連線（Oracle）、
計算欄位 Profit Ratio 的公式與萃取都在 .tdsx 內。報表本地另外加了計算欄位 Double Sales，
並把 Sales 改名為 Sales Amount。併入後的 .twbx 與嵌入式資料源相同，可以解析、匯入萃取與查詢。
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

import pytest
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

from tmig.cli import main
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_workbook
from tmig.ir import ExtractConnection, LiveConnection, PublishedSource
from tmig.parser.published import PublishedError, merge_published, workbook_refs
from tmig.parser.workbook import NoDatasourceError, parse_workbook
from tmig.sqlgen.query import worksheet_sql

from . import builder as b
from .builder import DS, instance, ref, worksheet

ROWS = [
    ["East", "Tech", "O1", dt.date(2024, 1, 1), 100.0, 20.0, 1],
    ["East", "Office", "O2", dt.date(2024, 1, 2), 50.0, 5.0, 2],
    ["West", "Tech", "O3", dt.date(2024, 1, 3), 70.0, -7.0, 3],
]
ORACLE = (
    "<connection authentication='username-password' class='oracle' port='1521' "
    "schema='SALES' server='ora.example.com' service='ORCLPDB' />"
)
PROFIT_RATIO = (
    "<column caption='Profit Ratio' datatype='real' name='[Calculation_1]' role='measure' "
    "type='quantitative'><calculation class='tableau' formula='SUM([Profit]) / SUM([Sales])' />"
    "</column>"
)
LOCATION = (
    "<repository-location derived-from='https://tableau.example.com/t/sales/datasources/OrdersPDS"
    "?rev=1.0' id='OrdersPDS' path='/t/sales/datasources' revision='1.0' site='sales' />"
)


def tds(extract: bool = True) -> str:
    """Tableau Server 上的資料源定義（.tds）：Oracle 的 ORDERS，萃取在 .tdsx 內。"""
    extract_xml = (
        f"""
  <extract count='-1' enabled='true' units='records'>
    <connection class='hyper' dbname='Data/Extracts/federated_1.hyper' schema='Extract' tablename='Extract'>
      <relation name='Extract' table='[Extract].[Extract]' type='table' />
      {b.metadata()}
    </connection>
  </extract>"""
        if extract
        else ""
    )
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='OrdersPDS' inline='true' source-platform='win' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <document-format-change-manifest><SortTagCleanup /></document-format-change-manifest>
  {LOCATION}
  <connection class='federated'>
    <named-connections>
      <named-connection caption='db' name='oracle.1'>{ORACLE}</named-connection>
    </named-connections>
    <relation connection='oracle.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />
    {b.metadata("[ORDERS]")}
  </connection>
  {extract_xml}
  <aliases enabled='yes' />
  {PROFIT_RATIO}
</datasource>
"""


def sqlproxy_datasource(local_extract: str = "") -> str:
    """報表中的 Published Data Source：sqlproxy 連線與欄位的快取，加上報表本地的修改。"""
    return f"""
    <datasource caption='Orders (Published)' inline='true' name='{DS}' version='18.1'>
      {LOCATION}
      <connection channel='https' class='sqlproxy' dbname='OrdersPDS' port='443' server='tableau.example.com' username=''>
        <relation name='sqlproxy' table='[sqlproxy]' type='table' />
        {b.metadata("[sqlproxy]")}
      </connection>
      {local_extract}
      <aliases enabled='yes' />
      <column caption='Profit Ratio' datatype='real' name='[Calculation_1]' role='measure' type='quantitative' />
      <column caption='Sales Amount' datatype='real' name='[Sales]' role='measure' type='quantitative' />
      {b.calc("Calculation_9", "Double Sales", "SUM([Sales]) * 2")}
    </datasource>"""


def sheet() -> str:
    return worksheet(
        "By Region",
        [
            instance("[Region]", "None", "[none:Region:nk]"),
            instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
            instance("[Calculation_1]", "User", "[usr:Calculation_1:qk]", "quantitative"),
            instance("[Calculation_9]", "User", "[usr:Calculation_9:qk]", "quantitative"),
        ],
        rows=ref("none:Region:nk"),
        cols=" + ".join(
            ref(n) for n in ("sum:Sales:qk", "usr:Calculation_1:qk", "usr:Calculation_9:qk")
        ),
    )


def make_hyper(path: Path, log_dir: Path) -> None:
    types = {
        "string": SqlType.text(),
        "date": SqlType.date(),
        "real": SqlType.double(),
        "integer": SqlType.big_int(),
    }
    definition = TableDefinition(
        TableName("Extract", "Extract"),
        [TableDefinition.Column(name, types[kind]) for name, kind, _ in b.COLUMNS],
    )
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        conn.catalog.create_table(definition)
        with Inserter(conn, definition) as inserter:
            inserter.add_rows(ROWS)
            inserter.execute()


def write_tdsx(path: Path, tds_xml: str, hyper: Path | None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("OrdersPDS.tds", tds_xml.encode("utf-8"))
        if hyper is not None:
            archive.writestr("Data/Extracts/federated_1.hyper", hyper.read_bytes())
    return path


def published_report(tmp: Path, extract: bool = True, local_extract: str = "") -> Path:
    """原本的報表（.twb）與 published/OrdersPDS.tdsx。"""
    published = tmp / "published"
    published.mkdir(parents=True)
    hyper = None
    if extract:
        hyper = tmp / "pds.hyper"
        make_hyper(hyper, tmp)
    write_tdsx(published / "OrdersPDS.tdsx", tds(extract), hyper)
    xml = b.workbook(datasource=sqlproxy_datasource(local_extract), worksheets=sheet())
    return b.write_twb(tmp, xml, "report.twb")


def test_unmerged_report_is_unsupported(tmp_path: Path) -> None:
    report = published_report(tmp_path)
    (found,) = workbook_refs(report)
    assert (found.content_url, found.site, found.server) == (
        "OrdersPDS",
        "sales",
        "tableau.example.com",
    )
    with pytest.raises(NoDatasourceError) as info:
        parse_workbook(report)
    assert any("需先下載 .tdsx 併入報表" in u.reason for u in info.value.unsupported)


def test_merge_extract_published_source(tmp_path: Path) -> None:
    report = published_report(tmp_path)
    out = tmp_path / "merged" / "report.twbx"
    result = merge_published(report, tmp_path / "published", out)
    assert result.ok and result.missing == []
    (merged,) = result.merged
    assert merged.hyper == "Data/Published/OrdersPDS/federated_1.hyper"
    with zipfile.ZipFile(out) as archive:
        assert sorted(archive.namelist()) == [
            "Data/Published/OrdersPDS/federated_1.hyper",
            "report.twb",
        ]

    ir = parse_workbook(out)
    assert ir.unsupported == []
    (ds,) = ir.datasources
    assert ds.name == DS and ds.caption == "Orders (Published)"
    assert ds.published == PublishedSource(content_url="OrdersPDS", site="sales", revision="1.0")
    connection = ds.connection
    assert isinstance(connection, ExtractConnection)
    assert connection.hyper_file == merged.hyper
    # 萃取的來源是 .tds 的 Oracle 連線（萃取層可以從來源更新）
    assert isinstance(connection.source, LiveConnection) and connection.source.dbclass == "oracle"
    fields = {f.caption: f for f in ds.fields}
    # .tds 的計算欄位帶公式；報表本地的計算欄位與改名都保留
    ratio, double = fields["Profit Ratio"].calc, fields["Double Sales"].calc
    assert ratio is not None and ratio.formula == "SUM([Profit]) / SUM([Sales])"
    assert double is not None and double.formula == "SUM([Sales]) * 2"
    assert "Sales Amount" in fields and "Sales" not in fields

    (imported,) = import_workbook(out, tmp_path / "data", ir)
    assert imported.ok, imported.to_json()
    query = worksheet_sql(ir, "By Region")
    rows = execute_query(tmp_path / "data" / f"{ds.id}.duckdb", query).rows
    assert [(r[0], r[1], round(r[2], 6), r[3]) for r in rows] == [
        ("East", 150.0, round(25 / 150, 6), 300.0),
        ("West", 70.0, -0.1, 140.0),
    ]


def test_merge_live_published_source(tmp_path: Path) -> None:
    report = published_report(tmp_path, extract=False)
    out = tmp_path / "report.merged.twbx"
    result = merge_published(report, tmp_path / "published", out)
    (merged,) = result.merged
    assert merged.hyper is None and merged.note == "Live 連線"
    ir = parse_workbook(out)
    connection = ir.datasources[0].connection
    assert isinstance(connection, LiveConnection) and connection.dialect == "oracle"
    assert ir.datasources[0].published is not None


def test_local_extract_of_published_source_is_kept(tmp_path: Path) -> None:
    # 報表本地萃取了 Published Data Source：資料在 .twbx 內，沿用報表的萃取
    local = (
        "<extract count='-1' enabled='true' units='records'>"
        "<connection class='hyper' dbname='Data/Extracts/local.hyper' schema='Extract' tablename='Extract'>"
        f"<relation name='Extract' table='[Extract].[Extract]' type='table' />{b.metadata()}"
        "</connection></extract>"
    )
    report = published_report(tmp_path, local_extract=local)
    hyper = tmp_path / "local.hyper"
    make_hyper(hyper, tmp_path)
    twbx = b.write_twbx(
        tmp_path,
        report.read_text(encoding="utf-8"),
        {"Data/Extracts/local.hyper": hyper.read_bytes()},
    )
    out = tmp_path / "out.twbx"
    (merged,) = merge_published(twbx, tmp_path / "published", out).merged
    assert merged.note == "沿用報表本地的萃取" and merged.hyper is None
    with zipfile.ZipFile(out) as archive:
        assert "Data/Extracts/local.hyper" in archive.namelist()
        assert not any(n.startswith("Data/Published/") for n in archive.namelist())
    connection = parse_workbook(out).datasources[0].connection
    assert isinstance(connection, ExtractConnection)
    assert connection.hyper_file == "Data/Extracts/local.hyper"


def test_missing_published_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = published_report(tmp_path)
    empty = tmp_path / "nothing"
    empty.mkdir()
    out = tmp_path / "out.twbx"
    assert (
        main(["published", "merge", str(report), "--published", str(empty), "--out", str(out)]) == 1
    )
    assert "缺少  Orders (Published)（OrdersPDS）" in capsys.readouterr().err
    # 找得到時成功；.tdsx 依 .tds 內的 repository-location 對應，不一定要同名
    renamed = tmp_path / "published" / "something-else.tdsx"
    (tmp_path / "published" / "OrdersPDS.tdsx").rename(renamed)
    assert main(["published", "merge", str(report), "--out", str(out)]) == 0
    assert "併入  Orders (Published)（OrdersPDS）← something-else.tdsx" in capsys.readouterr().out


def test_merge_rejects_bad_output(tmp_path: Path) -> None:
    report = published_report(tmp_path)
    with pytest.raises(PublishedError, match=r"\.twbx"):
        merge_published(report, tmp_path / "published", tmp_path / "out.twb")
