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
from tmig.ir import IR, ExtractConnection, LiveConnection, TableRelation, load_ir
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
