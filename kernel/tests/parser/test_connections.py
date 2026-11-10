"""第三階段 N1：Live 連線與關聯樹的解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.ir import IR, ExtractConnection, JoinRelation, LiveConnection, SqlRelation, TableRelation
from tmig.parser.connections import connection_id
from tmig.parser.workbook import parse_workbook

from . import builder as b
from .builder import DS, instance, ref, worksheet

ORACLE = (
    "<connection authentication='username-password' class='oracle' one-time-sql='' port='1521' "
    "schema='SALES' server='ora.example.com' service='ORCLPDB' username='reader' />"
)
DATABRICKS = (
    "<connection authentication='auth-pass' class='databricks' dbname='main' port='443' "
    "schema='retail' server='adb-123.azuredatabricks.net' username='token' "
    "v-http-path='/sql/1.0/warehouses/abc' />"
)


def live_datasource(
    connection: str, relation: str, parent: str = "[ORDERS]", extra: str = ""
) -> str:
    return f"""
    <datasource caption='Orders' inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='db' name='db.1'>{connection}</named-connection>
        </named-connections>
        {relation}
        {b.metadata(parent)}
      </connection>
      <aliases enabled='yes' />
      {extra}
    </datasource>"""


def sheet() -> str:
    return worksheet(
        "Sales",
        [
            instance("[Region]", "None", "[none:Region:nk]"),
            instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative"),
        ],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
    )


def parse(tmp_path: Path, datasource: str, **kwargs: bool) -> IR:
    xml = b.workbook(datasource=datasource, worksheets=sheet())
    return parse_workbook(b.write_twb(tmp_path, xml), **kwargs)


def reasons(ir: IR) -> list[str]:
    return [u.reason for u in ir.unsupported]


def test_oracle_table(tmp_path: Path) -> None:
    relation = "<relation connection='db.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />"
    ir = parse(tmp_path, live_datasource(ORACLE, relation))
    assert reasons(ir) == []
    (ds,) = ir.datasources
    conn = ds.connection
    assert isinstance(conn, LiveConnection)
    assert (conn.dbclass, conn.dialect, conn.server, conn.port) == (
        "oracle",
        "oracle",
        "ora.example.com",
        1521,
    )
    assert (conn.database, conn.schema_name) == ("ORCLPDB", "SALES")
    assert conn.connection_id == "oracle_ora_example_com_orclpdb"
    assert conn.relation == TableRelation(kind="table", name="ORDERS", table=["SALES", "ORDERS"])
    region = next(f for f in ds.fields if f.id == "f_region")
    assert (region.table, region.column) == ("ORDERS", "Region")
    # 憑證不進 IR
    assert "reader" not in conn.model_dump_json()


def test_databricks_custom_sql(tmp_path: Path) -> None:
    relation = (
        "<relation connection='db.1' name='Custom SQL Query' type='text'>"
        "SELECT * FROM main.retail.orders WHERE order_date &gt;= '2024-01-01'</relation>"
    )
    ir = parse(tmp_path, live_datasource(DATABRICKS, relation, "[Custom SQL Query]"))
    assert reasons(ir) == []
    conn = ir.datasources[0].connection
    assert isinstance(conn, LiveConnection)
    assert (conn.dialect, conn.database, conn.schema_name, conn.http_path) == (
        "databricks",
        "main",
        "retail",
        "/sql/1.0/warehouses/abc",
    )
    assert isinstance(conn.relation, SqlRelation)
    assert conn.relation.sql.endswith("order_date >= '2024-01-01'")


def test_presto_and_athena(tmp_path: Path) -> None:
    """Presto 連線（Trino、Starburst、PrestoDB）的資料庫是 catalog；Athena 的區域在主機名稱中。"""
    table = "<relation connection='db.1' name='orders' table='[sales].[orders]' type='table' />"
    presto = (
        "<connection authentication='ldap' catalog='hive' class='presto' port='8443' "
        "schema='sales' server='trino.example.com' sslmode='require' username='reader' />"
    )
    ir = parse(tmp_path, live_datasource(presto, table))
    assert reasons(ir) == []
    conn = ir.datasources[0].connection
    assert isinstance(conn, LiveConnection)
    assert (conn.dialect, conn.server, conn.port, conn.database, conn.schema_name) == (
        "trino",
        "trino.example.com",
        8443,
        "hive",
        "sales",
    )
    assert conn.connection_id == "presto_trino_example_com_hive"
    athena = (
        "<connection class='athena' port='443' schema='sales' "
        "server='athena.us-east-1.amazonaws.com' username='reader' />"
    )
    ir = parse(tmp_path, live_datasource(athena, table))
    assert reasons(ir) == []
    conn = ir.datasources[0].connection
    assert isinstance(conn, LiveConnection)
    assert (conn.dialect, conn.server, conn.schema_name) == (
        "athena",
        "athena.us-east-1.amazonaws.com",
        "sales",
    )


def test_bigquery(tmp_path: Path) -> None:
    """BigQuery 的專案記為資料庫、資料集記為結構描述。"""
    table = "<relation connection='db.1' name='orders' table='[sales].[orders]' type='table' />"
    bigquery = (
        "<connection CATALOG='sales-project' EXECCATALOG='billing-project' SCHEMA='sales' "
        "class='bigquery' connection-dialect='google-sql' project='sales-project' schema='sales' />"
    )
    ir = parse(tmp_path, live_datasource(bigquery, table))
    assert reasons(ir) == []
    conn = ir.datasources[0].connection
    assert isinstance(conn, LiveConnection)
    assert (conn.dialect, conn.database, conn.schema_name) == ("bigquery", "sales-project", "sales")
    assert conn.relation == TableRelation(kind="table", name="orders", table=["sales", "orders"])


def test_join_tree(tmp_path: Path) -> None:
    relation = """
      <relation join='left' type='join'>
        <clause type='join'>
          <expression op='AND'>
            <expression op='='>
              <expression op='[ORDERS].[Region]' />
              <expression op='[PEOPLE].[Region]' />
            </expression>
            <expression op='='>
              <expression op='1' />
              <expression op='[PEOPLE].[Active]' />
            </expression>
          </expression>
        </clause>
        <relation connection='db.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />
        <relation connection='db.1' name='PEOPLE' table='[SALES].[PEOPLE]' type='table' />
      </relation>"""
    ir = parse(tmp_path, live_datasource(ORACLE, relation))
    assert reasons(ir) == []
    conn = ir.datasources[0].connection
    assert isinstance(conn, LiveConnection)
    join = conn.relation
    assert isinstance(join, JoinRelation)
    assert join.join == "left"
    first, second = join.on
    assert (first.left.table, first.left.column, first.right.table) == (
        "ORDERS",
        "Region",
        "PEOPLE",
    )
    assert (second.left.literal, second.right.column) == (1, "Active")


def test_extract_records_its_source(tmp_path: Path) -> None:
    datasource = (
        b.extract_datasource()
        .replace(
            "<connection class='textscan' directory='C:/data' filename='orders.csv' />", ORACLE
        )
        .replace("table='[orders#csv]'", "table='[SALES].[ORDERS]'")
    )
    ir = parse(tmp_path, datasource)
    conn = ir.datasources[0].connection
    assert isinstance(conn, ExtractConnection)
    assert conn.source is not None
    assert (conn.source.dialect, conn.source.schema_name) == ("oracle", "SALES")


def test_unsupported_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table = "<relation connection='db.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />"
    # 沒有萃取的 Excel：檔案型資料源（匯入時讀進 DuckDB，見 tests/data/test_file_sources.py）
    excel = "<connection class='excel-direct' filename='orders.xlsx' />"
    ir = parse(tmp_path, live_datasource(excel, table), lenient=True)
    assert ir.datasources[0].connection.kind == "file"
    # 資料解譯器整理過的工作表與原始檔案的版面不同
    cleaned = "<connection class='excel-direct' cleaning='yes' filename='orders.xlsx' />"
    ir = parse(tmp_path, live_datasource(cleaned, table), lenient=True)
    assert any("資料解譯器" in r for r in reasons(ir))

    unknown = "<connection class='exasol' server='x' />"
    ir = parse(tmp_path, live_datasource(unknown, table), lenient=True)
    assert any("連線類型 exasol 尚不支援" in r for r in reasons(ir))

    # 認得的連線類型，但還沒有方言轉譯與執行器（以去掉 Teradata 的清單模擬）
    from tmig.parser import connections

    monkeypatch.setattr(connections, "EXECUTABLE", connections.EXECUTABLE - {"teradata"})
    teradata = "<connection class='teradata' server='crm.example.com' dbname='crm' />"
    ir = parse(tmp_path, live_datasource(teradata, table), lenient=True)
    assert any("連線類型 teradata 尚無 SQL 轉譯與執行器" in r for r in reasons(ir))
    conn = ir.datasources[0].connection
    assert isinstance(conn, LiveConnection) and conn.dialect == "teradata"
    monkeypatch.undo()

    # SQL Server、MySQL、Snowflake、Teradata 已有轉譯與執行器
    for dbclass, dialect in (
        ("sqlserver", "tsql"),
        ("mysql", "mysql"),
        ("snowflake", "snowflake"),
        ("teradata", "teradata"),
    ):
        named = f"<connection class='{dbclass}' server='db.example.com' dbname='crm' />"
        ir = parse(tmp_path, live_datasource(named, table), lenient=True)
        assert not any("尚無 SQL 轉譯與執行器" in r for r in reasons(ir))
        conn = ir.datasources[0].connection
        assert isinstance(conn, LiveConnection) and conn.dialect == dialect

    union = "<relation type='union'><relation name='A' table='[A]' type='table' /></relation>"
    ir = parse(tmp_path, live_datasource(ORACLE, union), lenient=True)
    assert any("資料表關聯方式 union 尚不支援" in r for r in reasons(ir))

    calc_join = """
      <relation join='inner' type='join'>
        <clause type='join'>
          <expression op='='>
            <expression op='LOWER'><expression op='[A].[x]' /></expression>
            <expression op='[B].[y]' />
          </expression>
        </clause>
        <relation name='A' table='[A]' type='table' />
        <relation name='B' table='[B]' type='table' />
      </relation>"""
    ir = parse(tmp_path, live_datasource(ORACLE, calc_join), lenient=True)
    assert any("join 條件不是欄位比較" in r for r in reasons(ir))

    two = live_datasource(ORACLE, table).replace(
        "</named-connections>",
        f"<named-connection caption='db2' name='db.2'>{DATABRICKS}</named-connection></named-connections>",
    )
    ir = parse(tmp_path, two, lenient=True)
    assert any("跨資料庫的關聯" in r for r in reasons(ir))


def test_viewer_identity_settings_are_unsupported(tmp_path: Path) -> None:
    """依檢視者的身分查詢資料庫的 Live 連線：平台以服務帳號連線、不執行 Initial SQL，列為不支援。"""
    table = "<relation connection='db.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />"
    vpd = ORACLE.replace(
        "one-time-sql=''",
        "one-time-sql='BEGIN DBMS_SESSION.SET_IDENTIFIER([TableauServerUser]); END;'",
    )
    for lenient in (False, True):
        ir = parse(tmp_path, live_datasource(vpd, table), lenient=lenient)
        assert reasons(ir) == [
            "資料源「Orders」的 Initial SQL 用到 [TableauServerUser]，資料庫可能依檢視者的身分"
            "過濾資料；平台不會執行 Initial SQL，需改用列層級權限（使用者函式或使用者篩選）"
        ]

    nls = ORACLE.replace("one-time-sql=''", "one-time-sql='ALTER SESSION SET NLS_SORT = BINARY'")
    ir = parse(tmp_path, live_datasource(nls, table), lenient=True)
    assert reasons(ir) == ["資料源「Orders」有 Initial SQL，平台尚不會執行"]

    prompt = ORACLE.replace("/>", "workgroup-auth-mode='prompt' />")
    ir = parse(tmp_path, live_datasource(prompt, table), lenient=True)
    assert reasons(ir) == [
        "資料源「Orders」在 Tableau Server 上要求檢視者輸入自己的資料庫帳號；"
        "平台以唯讀服務帳號連線，資料庫端依人不同的權限不會生效"
    ]

    # 嵌入帳號密碼（as-is）、Kerberos 與 OAuth 不擋（功能盤點列為需注意）
    for attrs in ("workgroup-auth-mode='as-is'", "authentication='kerberos'"):
        conn = ORACLE.replace("authentication='username-password'", "").replace("/>", f"{attrs} />")
        assert reasons(parse(tmp_path, live_datasource(conn, table), lenient=True)) == []


def test_extract_source_initial_sql_is_not_flagged(tmp_path: Path) -> None:
    """萃取的來源連線只在更新萃取時使用：檢視時讀萃取檔，Initial SQL 不影響解析。"""
    source = ORACLE.replace("one-time-sql=''", "one-time-sql='EXEC AS [TableauServerUser]'")
    datasource = (
        b.extract_datasource()
        .replace(
            "<connection class='textscan' directory='C:/data' filename='orders.csv' />", source
        )
        .replace("table='[orders#csv]'", "table='[SALES].[ORDERS]'")
    )
    ir = parse(tmp_path, datasource)
    assert reasons(ir) == []


def test_initial_sql_users() -> None:
    from tmig.parser.connections import initial_sql_users

    assert initial_sql_users("EXECUTE AS USER = [TableauServerUser] WITH NO REVERT") == [
        "[TableauServerUser]"
    ]
    assert initial_sql_users("SET x = [tableauserveruserfull]; SET y = [TableauApp]") == [
        "[TableauServerUserFull]"
    ]
    assert initial_sql_users("SELECT [WorkbookName]") == []


def test_executable_dialects_match_translation() -> None:
    from tmig.parser.connections import EXECUTABLE
    from tmig.sqlgen.dialect import SUPPORTED

    assert SUPPORTED - {"duckdb"} == EXECUTABLE


def test_connection_id() -> None:
    assert connection_id("postgres", "localhost", "workgroup") == "postgres_localhost_workgroup"
    long = connection_id("databricks", "adb-" + "1" * 80 + ".net", "main")
    assert long.startswith("databricks_") and len(long) <= 22
