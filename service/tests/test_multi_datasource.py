"""多個資料源的報表（各工作表用各自的資料源）：解析、功能盤點、萃取匯入、查詢、網址參數與查詢服務。

Orders（訂單）與 Targets（目標）是兩個萃取資料源，各有自己的 .hyper；dashboard 同時放兩張工作表。
Data Blending（同一張工作表混用多個資料源）仍不支援，另由功能盤點檢查。
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.census import run_census
from tmig.data.hyper_import import import_workbook
from tmig.parser.workbook import parse_workbook
from tmig.verify.cases import CaseSpec, case_state
from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
ORDERS = "federated.orders"
TARGETS = "federated.targets"
ORDER_COLUMNS = [
    ("Region", "string", "Count"),
    ("Category", "string", "Count"),
    ("Sales", "real", "Sum"),
]
TARGET_COLUMNS = [("Region", "string", "Count"), ("Target", "real", "Sum")]
ORDER_ROWS = [
    ["East", "Tech", 100.0],
    ["East", "Office", 50.0],
    ["West", "Tech", 70.0],
    ["Central", "Office", 30.0],
]
TARGET_ROWS = [["East", 120.0], ["West", 60.0], ["South", 40.0]]


def records(parent: str, columns: list[tuple[str, str, str]]) -> str:
    items = [
        f"""<metadata-record class='column'>
          <remote-name>{name}</remote-name><remote-type>129</remote-type>
          <local-name>[{name}]</local-name><parent-name>{parent}</parent-name>
          <ordinal>{i}</ordinal><local-type>{datatype}</local-type><aggregation>{agg}</aggregation>
        </metadata-record>"""
        for i, (name, datatype, agg) in enumerate(columns)
    ]
    return "<metadata-records>" + "".join(items) + "</metadata-records>"


def datasource(name: str, caption: str, columns: list[tuple[str, str, str]]) -> str:
    stem = caption.lower()
    return f"""
    <datasource caption='{caption}' inline='true' name='{name}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='src' name='textscan.{stem}'>
            <connection class='textscan' directory='C:/data' filename='{stem}.csv' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.{stem}' name='{stem}.csv' table='[{stem}#csv]' type='table' />
        {records(f"[{stem}.csv]", columns)}
      </connection>
      <extract count='-1' enabled='true' units='records'>
        <connection class='hyper' dbname='Data/Extracts/{stem}.hyper' schema='Extract' tablename='Extract'>
          <relation name='Extract' table='[Extract].[Extract]' type='table' />
          {records("[Extract]", columns)}
        </connection>
      </extract>
      <aliases enabled='yes' />
    </datasource>"""


def worksheet(name: str, ds: str, caption: str, measure: str) -> str:
    return f"""
    <worksheet name='{name}'>
      <table>
        <view>
          <datasources><datasource caption='{caption}' name='{ds}' /></datasources>
          <datasource-dependencies datasource='{ds}'>
            <column-instance column='[Region]' derivation='None' name='[none:Region:nk]' pivot='key' type='nominal' />
            <column-instance column='[{measure}]' derivation='Sum' name='[sum:{measure}:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style />
        <panes><pane><view><breakdown value='auto' /></view><mark class='Bar' /><encodings /></pane></panes>
        <rows>[{ds}].[none:Region:nk]</rows>
        <cols>[{ds}].[sum:{measure}:qk]</cols>
      </table>
    </worksheet>"""


WORKBOOK = f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.2.0' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences />
  <datasources>
    {datasource(ORDERS, "Orders", ORDER_COLUMNS)}
    {datasource(TARGETS, "Targets", TARGET_COLUMNS)}
  </datasources>
  <worksheets>
    {worksheet("Sales by Region", ORDERS, "Orders", "Sales")}
    {worksheet("Targets by Region", TARGETS, "Targets", "Target")}
  </worksheets>
  <dashboards>
    <dashboard name='Overview'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='50000' id='2' name='Sales by Region' w='100000' x='0' y='0' />
          <zone h='50000' id='3' name='Targets by Region' w='100000' x='0' y='50000' />
        </zone>
      </zones>
    </dashboard>
  </dashboards>
  <windows />
</workbook>
"""


def make_hyper(path: Path, columns: list[tuple[str, str, str]], rows: list[list[Any]]) -> None:
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

    types = {"string": SqlType.text(), "real": SqlType.double()}
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(path.parent)}
        ) as hyper,
        Connection(hyper.endpoint, path, CreateMode.CREATE_AND_REPLACE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        definition = TableDefinition(
            TableName("Extract", "Extract"),
            [TableDefinition.Column(name, types[datatype]) for name, datatype, _ in columns],
        )
        conn.catalog.create_table(definition)
        with Inserter(conn, definition) as inserter:
            inserter.add_rows(rows)
            inserter.execute()


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import zipfile

    work = tmp_path_factory.mktemp("multi_build")
    make_hyper(work / "orders.hyper", ORDER_COLUMNS, ORDER_ROWS)
    make_hyper(work / "targets.hyper", TARGET_COLUMNS, TARGET_ROWS)
    root = tmp_path_factory.mktemp("reports")
    report = root / "multi"
    report.mkdir()
    with zipfile.ZipFile(report / "multi.twbx", "w") as archive:
        archive.writestr("multi.twb", WORKBOOK.encode("utf-8"))
        for stem in ("orders", "targets"):
            archive.write(work / f"{stem}.hyper", f"Data/Extracts/{stem}.hyper")
    return root


def rows(data: dict[str, Any]) -> dict[str, str]:
    """表格的列（顯示值，依 Tableau 的預設格式）。"""
    return {row[0]: row[1] for row in data["table"]["rows"]}


def test_multiple_datasources_end_to_end(reports: Path) -> None:
    twbx = reports / "multi" / "multi.twbx"

    # 功能盤點：多個資料源不再是起步條件，每張工作表只用一個資料源
    census = run_census(twbx)
    assert {c.key: c.status for c in census.checks if c.status != "pass"} == {}
    assert "single_datasource" not in {c.key for c in census.checks}

    # 解析：兩個資料源，各工作表對應自己的資料源
    ir = parse_workbook(twbx)
    assert ir.unsupported == []
    orders, targets = ir.datasources
    assert [ws.datasource for ws in ir.worksheets] == [orders.id, targets.id]
    (board,) = ir.dashboards
    assert {z.worksheet for z in board.zones[0].children} == {ws.id for ws in ir.worksheets}

    # 萃取匯入：每個資料源一個 DuckDB 檔
    results = import_workbook(twbx, reports / "multi" / "data", ir)
    assert [r.ok for r in results] == [True, True]
    assert sorted(p.name for p in (reports / "multi" / "data").glob("*.duckdb")) == sorted(
        [f"{orders.id}.duckdb", f"{targets.id}.duckdb"]
    )
    (reports / "multi" / "golden.ir.json").write_text(
        ir.model_dump_json(indent=2), encoding="utf-8"
    )

    client = TestClient(create_app(reports, ROOT / "no-web-dist"))
    (listed,) = client.get("/api/workbooks").json()
    assert listed["dashboards"] == [{"id": board.id, "name": "Overview"}]
    assert len(listed["worksheets"]) == 2

    base = "/api/workbooks/multi/worksheets"
    sales_id, targets_id = (ws.id for ws in ir.worksheets)
    assert rows(client.post(f"{base}/{sales_id}/render").json()) == {
        "Central": "30",
        "East": "150",
        "West": "70",
    }
    assert rows(client.post(f"{base}/{targets_id}/render").json()) == {
        "East": "120",
        "South": "40",
        "West": "60",
    }

    # 網址參數（與 Tableau 相同）套用到兩個資料源的 Region：各工作表只套用自己資料源的欄位
    state = case_state(ir, CaseSpec(name="east", url_params={"Region": "East"}))
    body = {
        "filters": {key: value.model_dump() for key, value in state.filters.items()},
        "parameters": {},
    }
    assert len(body["filters"]) == 2
    sales = client.post(f"{base}/{sales_id}/render", json=body).json()
    assert rows(sales) == {"East": "150"}
    target = client.post(f"{base}/{targets_id}/render", json=body).json()
    assert rows(target) == {"East": "120"}
    assert len(sales["applied"]) == len(target["applied"]) == 1
