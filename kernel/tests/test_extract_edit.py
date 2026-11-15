"""萃取設定的編輯：即時 ↔ 萃取的切換、萃取篩選與增量更新（DuckDB 檔模擬來源資料庫）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.data.executor import execute
from tmig.data.extract_refresh import refresh_extract
from tmig.data.sources import DuckDBExecutor
from tmig.edit import EditError, set_datasource_filters
from tmig.extract_edit import (
    extract_info,
    set_extract_increment,
    switch_to_extract,
    switch_to_live,
)
from tmig.ir import IR, ExtractConnection, LiveConnection, TableRelation, TupleFilter, load_ir
from tmig.sqlgen.query import worksheet_sql
from tmig.verify.run import ensure_data

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "fixtures" / "superstore_features"
DATABASE = FEATURES / "data" / "ds_orders.duckdb"
SHEET = "ws_sales_by_segment"


@pytest.fixture(scope="module")
def original() -> IR:
    ir = load_ir(FEATURES / "golden.ir.json")
    ensure_data(FEATURES, ir)
    return ir


@pytest.fixture
def live(original: IR) -> IR:
    """功能報表的資料源改成即時連線（連到萃取的 DuckDB 檔）。"""
    connection = LiveConnection(
        kind="live",
        connection_id="sales_duckdb",
        dbclass="oracle",
        dialect="oracle",
        relation=TableRelation(kind="table", name="Orders", table=["Extract", "Extract"]),
    )
    return original.model_copy(
        update={
            "datasources": [
                ds.model_copy(update={"connection": connection}) for ds in original.datasources
            ]
        }
    )


def rows(ir: IR, database: Path) -> list[tuple[object, ...]]:
    return execute(database, worksheet_sql(ir, SHEET).sql).rows


def test_live_to_extract_and_back(original: IR, live: IR, tmp_path: Path) -> None:
    info = extract_info(live, "ds_orders")
    assert (info["mode"], info["switchable"], info["refreshable"]) == ("live", True, False)
    extracted = switch_to_extract(live, "ds_orders")
    connection = extracted.datasources[0].connection
    assert isinstance(connection, ExtractConnection)
    assert connection.source == live.datasources[0].connection and connection.refresh is not None
    physical = [f for f in live.datasources[0].fields if f.column is not None]
    assert [(c.field, c.column) for c in connection.refresh.columns] == [
        (f.id, f.column) for f in physical
    ]
    assert extract_info(extracted, "ds_orders")["mode"] == "extract"

    # 從來源建立萃取：工作表的結果與原本的萃取相同
    out = tmp_path / "ds_orders.duckdb"
    result = refresh_extract(extracted, "ds_orders", DuckDBExecutor(DATABASE), out)
    assert result.mode == "full" and result.tables[0].rows > 0
    assert rows(extracted, out) == rows(original, DATABASE)

    # 萃取篩選：下次更新時套用在來源的資料上
    with_filter, saved = set_datasource_filters(
        extracted,
        "ds_orders",
        [
            {
                "kind": "categorical",
                "id": "xf_segment",
                "field": {"field": "f_segment"},
                "stage": "dimension",
                "mode": "include",
                "values": ["Consumer"],
                "source_ref": "/editor",
            }
        ],
        "extract",
    )
    assert [(f.id, f.stage) for f in saved] == [("xf_segment", "extract")]
    assert [f["id"] for f in extract_info(with_filter, "ds_orders")["filters"]] == ["xf_segment"]
    refresh_extract(with_filter, "ds_orders", DuckDBExecutor(DATABASE), out)
    segments = {r[0] for r in rows(with_filter, out)}
    assert segments == {"Consumer"}

    # 增量更新的欄位
    incremental = set_extract_increment(with_filter, "ds_orders", "f_order_date")
    assert extract_info(incremental, "ds_orders")["increment"] == "f_order_date"
    again = refresh_extract(
        incremental, "ds_orders", DuckDBExecutor(DATABASE), out, incremental=True
    )
    assert again.mode == "incremental" and again.tables[0].added == 0  # 來源沒有更新的列
    with pytest.raises(EditError, match="數字或日期"):
        set_extract_increment(with_filter, "ds_orders", "f_segment")
    assert set_extract_increment(incremental, "ds_orders", None) == with_filter

    # 改回即時：與原本的即時連線相同（萃取篩選留著）
    back = switch_to_live(extracted, "ds_orders")
    assert back == live
    kept = switch_to_live(with_filter, "ds_orders")
    assert [f.stage for f in kept.datasources[0].filters if f.id == "xf_segment"] == ["extract"]
    assert kept.datasources[0].connection == live.datasources[0].connection


def test_switch_is_refused_when_the_source_is_unknown(original: IR, live: IR) -> None:
    # .twbx 的萃取沒有來源資料庫的資訊
    info = extract_info(original, "ds_orders")
    assert (info["mode"], info["switchable"]) == ("extract", False)
    assert "來源無法解析" in info["reason"]
    with pytest.raises(EditError, match="來源無法解析"):
        switch_to_live(original, "ds_orders")
    with pytest.raises(EditError, match="不是即時連線"):
        switch_to_extract(original, "ds_orders")
    with pytest.raises(EditError, match="不是萃取"):
        switch_to_live(live, "ds_orders")
    with pytest.raises(EditError, match="不是可以從來源更新的萃取"):
        set_extract_increment(original, "ds_orders", "f_order_date")


def test_top_rows_and_aggregate_for_visible_dimensions(live: IR, tmp_path: Path) -> None:
    """萃取只取前 N 筆；彙總可見維度的資料：依沒有隱藏的維度分組，量值先彙總，隱藏的欄位是 Null。"""
    from tmig.extract_edit import set_extract_options

    extracted = switch_to_extract(live, "ds_orders")
    out = tmp_path / "ds_orders.duckdb"
    full = refresh_extract(extracted, "ds_orders", DuckDBExecutor(DATABASE), out)
    expected = {r[0]: r[1:] for r in rows(extracted, out)}

    limited = set_extract_options(extracted, "ds_orders", top_rows=10)
    info = extract_info(limited, "ds_orders")
    assert (info["top_rows"], info["aggregate"], info["can_aggregate"]) == (10, False, True)
    result = refresh_extract(limited, "ds_orders", DuckDBExecutor(DATABASE), out)
    assert result.tables[0].rows == 10 < full.tables[0].rows

    # 只留下工作表用到的欄位（其餘隱藏），再彙總：列數是可見維度的組合數，工作表的數字不變
    sheet = next(w for w in extracted.worksheets if w.id == SHEET)
    used = {u.field for u in [*sheet.rows, *sheet.columns, *sheet.marks.uses()]}
    used |= {f.field.field for f in sheet.filters if not isinstance(f, TupleFilter)}
    ds = extracted.datasources[0]
    hidden = ds.model_copy(
        update={"fields": [f.model_copy(update={"hidden": f.id not in used}) for f in ds.fields]}
    )
    trimmed = extracted.model_copy(update={"datasources": [hidden]})
    aggregated = set_extract_options(trimmed, "ds_orders", aggregate=True)
    result = refresh_extract(aggregated, "ds_orders", DuckDBExecutor(DATABASE), out)
    assert 0 < result.tables[0].rows < full.tables[0].rows / 10
    got = {r[0]: r[1:] for r in rows(aggregated, out)}
    assert got.keys() == expected.keys()
    for key, values in expected.items():
        assert got[key] == pytest.approx(values)
    # 隱藏的欄位在萃取中是 Null
    hidden_column = next(f.column for f in hidden.fields if f.hidden and f.column is not None)
    assert execute(out, f'SELECT count("{hidden_column}") FROM "Extract"."Extract"').rows == [(0,)]

    with pytest.raises(EditError, match="不能增量更新"):
        set_extract_increment(aggregated, "ds_orders", "f_order_date")
    incremental = set_extract_increment(extracted, "ds_orders", "f_order_date")
    with pytest.raises(EditError, match="增量更新的萃取不能彙總"):
        set_extract_options(incremental, "ds_orders", aggregate=True)
    with pytest.raises(EditError, match="正整數"):
        set_extract_options(extracted, "ds_orders", top_rows=0)


def test_relationship_model_live_to_extract_and_back(tmp_path: Path) -> None:
    """Relationship 模型的資料源：每張邏輯資料表一張萃取資料表，關聯照舊；改回即時連線後與原本相同。"""
    import duckdb

    from tmig.authoring import add_live_table, new_live_workbook
    from tmig.data.catalog import table_columns
    from tmig.edit import replace_worksheet
    from tmig.ir import ModelRelation

    database = tmp_path / "sales.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE SCHEMA crm")
        con.execute("CREATE TABLE crm.orders (order_id VARCHAR, region VARCHAR, sales DOUBLE)")
        con.execute(
            "INSERT INTO crm.orders VALUES ('O1', 'East', 100.5), ('O2', 'West', 200), "
            "('O3', 'East', 50.25), ('O4', 'South', 75)"
        )
        con.execute("CREATE TABLE crm.people (region VARCHAR, manager VARCHAR)")
        con.execute(
            "INSERT INTO crm.people VALUES ('East', 'Ann'), ('West', 'Bob'), ('North', 'Cid')"
        )
        con.execute("CREATE TABLE crm.returns (order_id VARCHAR, returned VARCHAR)")
        con.execute("INSERT INTO crm.returns VALUES ('O1', 'Yes'), ('O4', 'Yes')")
    source = LiveConnection(
        kind="live",
        connection_id="duckdb_sales",
        dbclass="duckdb",
        dialect="duckdb",
        database="sales",
    )
    executor = DuckDBExecutor(database)
    columns = {
        name: table_columns(executor, "duckdb", ["crm", name])
        for name in ("orders", "people", "returns")
    }
    ir = new_live_workbook("Sales", source, ["crm", "orders"], None, columns["orders"])
    ds = ir.datasources[0].id

    def ids(current: IR) -> dict[str, str]:
        return {f.caption: f.id for f in current.datasources[0].fields}

    ir = add_live_table(
        ir, ds, ["crm", "people"], None, columns["people"], "relationship",
        on=[(ids(ir)["region"], "=", "region")],
    )  # fmt: skip
    model = ir.datasources[0].connection.relation  # type: ignore[union-attr]
    assert isinstance(model, ModelRelation)
    ir = add_live_table(
        ir, ds, ["crm", "returns"], None, columns["returns"], "join",
        target=model.tables[0].id, join="left", on=[(ids(ir)["order_id"], "=", "order_id")],
    )  # fmt: skip
    f = ids(ir)

    def by(current: IR, dimension: str, path: Path) -> dict[object, object]:
        sheet = current.worksheets[0].model_dump(mode="json")
        sheet.update(rows=[{"field": dimension}], columns=[{"field": f["sales"], "agg": "sum"}])
        placed, ws = replace_worksheet(current, sheet)
        return dict(execute(path, worksheet_sql(placed, ws.id).sql).rows)

    info = extract_info(ir, ds)
    assert (info["mode"], info["switchable"]) == ("live", True)
    extracted = switch_to_extract(ir, ds)
    connection = extracted.datasources[0].connection
    assert isinstance(connection, ExtractConnection) and connection.model is not None
    assert [t.relation.table for t in connection.model.tables] == [  # type: ignore[union-attr]
        ["Extract", t.id] for t in model.tables
    ]
    assert connection.model.relationships == model.relationships
    info = extract_info(extracted, ds)
    assert (info["mode"], info["switchable"], info["can_aggregate"]) == ("extract", True, False)

    out = tmp_path / "ds_sales.duckdb"  # 檔名不能是 extract（與萃取的 schema 同名，DuckDB 分不清）
    result = refresh_extract(extracted, ds, executor, out)
    executor.close()
    assert {(t.table, t.rows) for t in result.tables} == {
        (model.tables[0].id, 4),
        (model.tables[1].id, 3),
    }
    # 萃取的結果與即時連線相同：關聯（各資料表各自彙總）與邏輯資料表中的 join 都保留
    assert by(extracted, f["manager"], out) == {
        "Ann": 150.75,
        "Bob": 200.0,
        "Cid": None,
        None: 75.0,
    }
    assert by(extracted, f["returned"], out) == {"Yes": 175.5, None: 250.25}
    assert by(extracted, f["manager"], out) == by(ir, f["manager"], database)
    # 改回即時連線：與原本的資料源相同
    assert switch_to_live(extracted, ds) == ir
