"""直接以 .hyper 為來源、有 join 或多個 .hyper 檔的資料源（Logshark 的報表）：與檔案型資料源相同，
每張資料表匯入成 Files.<name>，join（含 join 計算）在本機執行。"""

from __future__ import annotations

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

from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_workbook
from tmig.ir import FileConnection, JoinRelation
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, worksheet

EVENTS = [(1, "begin", 10), (1, "end", 30), (2, "begin", 5), (2, "end", 9), (3, "begin", 1)]
KINDS = [(1, "Load"), (2, "Query")]
TYPES = {"integer": 20, "string": 130}


def make_hyper(
    path: Path, log_dir: Path, table: str, columns: list[tuple[str, str]], rows: list
) -> None:
    sql = {"integer": SqlType.big_int(), "string": SqlType.text()}
    definition = TableDefinition(
        TableName("public", table), [TableDefinition.Column(n, sql[t]) for n, t in columns]
    )
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
        ) as hyper,
        Connection(hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_table(definition)  # 新的 .hyper 已有 public 結構描述
        with Inserter(conn, definition) as inserter:
            inserter.add_rows(rows)
            inserter.execute()


def records(columns: list[tuple[str, str, str, str]]) -> str:
    """(local-name, remote-name, parent, type) → metadata-records。"""
    out = []
    for i, (local, remote, parent, datatype) in enumerate(columns):
        out.append(
            f"<metadata-record class='column'><remote-name>{remote}</remote-name>"
            f"<remote-type>{TYPES[datatype]}</remote-type><local-name>[{local}]</local-name>"
            f"<parent-name>[{parent}]</parent-name><ordinal>{i}</ordinal>"
            f"<local-type>{datatype}</local-type>"
            f"<aggregation>{'Sum' if datatype == 'integer' else 'Count'}</aggregation>"
            "</metadata-record>"
        )
    return "<metadata-records>" + "".join(out) + "</metadata-records>"


EVENT_COLUMNS = [
    ("Id", "Id", "Begin", "integer"),
    ("Type", "Type", "Begin", "string"),
    ("Ms", "Ms", "Begin", "integer"),
    ("Id (End)", "Id", "End", "integer"),
    ("Type (End)", "Type", "End", "string"),
    ("Ms (End)", "Ms", "End", "integer"),
]
JOIN_CALC = (
    "<expression op='='>"
    "<expression op='IF [Begin].[Type] = &quot;begin&quot; THEN &quot;Join&quot; "
    "ELSE &quot;Don&apos;t join left&quot; END' />"
    "<expression op='IF ([End].[Type] == &quot;end&quot;) THEN &quot;Join&quot; "
    "ELSE &quot;Don&apos;t join right&quot; END' />"
    "</expression>"
)


def datasource() -> str:
    relation = (
        "<relation join='left' type='join'><clause type='join'><expression op='='>"
        "<expression op='[Begin].[Id]' /><expression op='[Kinds].[Id]' /></expression></clause>"
        "<relation join='inner' type='join'><clause type='join'><expression op='AND'>"
        "<expression op='='><expression op='[Begin].[Id]' /><expression op='[End].[Id]' /></expression>"
        f"{JOIN_CALC}</expression></clause>"
        "<relation connection='hyper.events' name='Begin' table='[public].[Events]' type='table' />"
        "<relation connection='hyper.events' name='End' table='[public].[Events]' type='table' />"
        "</relation>"
        "<relation connection='hyper.kinds' name='Kinds' table='[public].[Kinds]' type='table' />"
        "</relation>"
    )
    kinds = [("Id (Kinds)", "Id", "Kinds", "integer"), ("Label", "Label", "Kinds", "string")]
    return f"""
    <datasource caption='Events' inline='true' name='{b.DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='Events' name='hyper.events'>
            <connection class='hyper' dbname='Data/Data/Events.hyper' schema='public' tablename='Events' />
          </named-connection>
          <named-connection caption='Kinds' name='hyper.kinds'>
            <connection class='hyper' dbname='Data/Data/Kinds.hyper' schema='public' tablename='Kinds' />
          </named-connection>
        </named-connections>
        {relation}
        {records(EVENT_COLUMNS + kinds)}
      </connection>
      <aliases enabled='yes' />
    </datasource>"""


@pytest.fixture(scope="module")
def twbx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp = tmp_path_factory.mktemp("hyper_files")
    events, kinds = tmp / "events.hyper", tmp / "kinds.hyper"
    make_hyper(
        events, tmp, "Events", [("Id", "integer"), ("Type", "string"), ("Ms", "integer")], EVENTS
    )
    make_hyper(kinds, tmp, "Kinds", [("Id", "integer"), ("Label", "string")], KINDS)
    sheet = worksheet(
        "Durations",
        [
            instance("[Label]", "None", "[none:Label:nk]"),
            instance("[Ms]", "Sum", "[sum:Ms:qk]", "quantitative"),
            instance("[Ms (End)]", "Sum", "[sum:Ms (End):qk]", "quantitative"),
        ],
        rows=b.ref("none:Label:nk"),
        cols=f"({b.ref('sum:Ms:qk')} + {b.ref('sum:Ms (End):qk')})",
    )
    xml = b.workbook(datasource(), worksheets=sheet)
    return b.write_twbx(
        tmp,
        xml,
        {
            "Data/Data/Events.hyper": events.read_bytes(),
            "Data/Data/Kinds.hyper": kinds.read_bytes(),
        },
    )


def test_joined_hyper_files_are_file_tables(twbx: Path) -> None:
    ir = parse_workbook(twbx)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    connection = ir.datasources[0].connection
    assert isinstance(connection, FileConnection)
    assert [(t.name, t.format, t.path, t.table) for t in connection.tables] == [
        ("Begin", "hyper", "Data/Data/Events.hyper", ["public", "Events"]),
        ("End", "hyper", "Data/Data/Events.hyper", ["public", "Events"]),
        ("Kinds", "hyper", "Data/Data/Kinds.hyper", ["public", "Kinds"]),
    ]
    inner = connection.relation
    assert isinstance(inner, JoinRelation) and isinstance(inner.left, JoinRelation)
    calc = inner.left.on[1]
    assert calc.left.formula is not None and calc.left.formula.startswith("IF [Begin].[Type]")


def test_join_calculation_runs_locally(twbx: Path, tmp_path: Path) -> None:
    ir = parse_workbook(twbx)
    results = import_workbook(twbx, tmp_path, ir)
    assert all(r.ok for r in results), [r.missing_columns for r in results]
    query = worksheet_sql(ir, ir.worksheets[0].id)
    rows = execute_query(tmp_path / f"{ir.datasources[0].id}.duckdb", query).records()
    # 每個 Id 只有開始與結束配成一對（join 計算排除 begin–begin、end–begin 等組合；Id 3 沒有結束）
    assert {r["f_label"]: (r["sum(f_ms)"], r["sum(f_ms_end)"]) for r in rows} == {
        "Load": (10, 30),
        "Query": (5, 9),
    }
