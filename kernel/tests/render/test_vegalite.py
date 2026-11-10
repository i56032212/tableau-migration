"""Vega-Lite 規格（F1）：各 fixture 每張工作表的規格快照，且符合 Vega-Lite v6 Schema。

規格中的資料值以浮點數加總，最後幾位可能因累加順序不同而變動，所以快照只比對結構與列數。
更新快照：TMIG_UPDATE_SNAPSHOTS=1 uv run pytest kernel/tests/render
"""

from __future__ import annotations

import gzip
import json
import os
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from tmig.data.executor import QueryResult, execute
from tmig.ir import load_ir
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import pane_sql, worksheet_sql
from tmig.verify.run import ensure_data

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "fixtures" / "superstore_demo"
SCHEMA = Path(__file__).parent / "data" / "vega-lite-v6.5.0.schema.json.gz"
GOLDEN = sorted((ROOT / "fixtures").glob("*/golden.ir.json"))
CASES = [(path.parent, ws.id) for path in GOLDEN for ws in load_ir(path).worksheets]


@cache
def validator() -> jsonschema.Draft7Validator:
    return jsonschema.Draft7Validator(json.loads(gzip.decompress(SCHEMA.read_bytes())))


@cache
def fixture_ir(directory: Path):
    loaded = load_ir(directory / "golden.ir.json")
    ensure_data(directory, loaded)
    return loaded


@pytest.fixture(scope="module")
def ir():
    return fixture_ir(DEMO)


def structure(spec: dict[str, Any]) -> dict[str, Any]:
    """快照只記資料的列數與欄位（含各標記卡自己的資料）。"""
    copy = json.loads(json.dumps(spec))

    def compact(node: Any) -> None:
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, dict) and isinstance(data.get("values"), list):
                values = data["values"]
                node["data"] = {
                    "values": f"{len(values)} 列",
                    "fields": sorted(values[0]) if values else [],
                }
            for key, value in node.items():
                if key != "data":
                    compact(value)
        elif isinstance(node, list):
            for item in node:
                compact(item)

    compact(copy)
    return copy


@pytest.mark.parametrize(("directory", "worksheet"), CASES, ids=[f"{d.name}/{w}" for d, w in CASES])
def test_spec_snapshot_and_schema(directory: Path, worksheet: str) -> None:
    ir = fixture_ir(directory)
    query = worksheet_sql(ir, worksheet)
    sheet = next(w for w in ir.worksheets if w.id == worksheet)
    database = directory / "data" / f"{sheet.datasource}.duckdb"
    result = execute(database, query.sql)
    panes = {
        i: (pane.columns, execute(database, pane.sql))
        for i, pane in pane_sql(ir, worksheet, query).items()
    }
    rendered = build_spec(ir, worksheet, query.columns, result, None, panes or None)
    assert rendered.spec is not None
    errors = [e.message for e in validator().iter_errors(rendered.spec)]
    assert errors == []
    snapshot = directory / "specs" / f"{worksheet}.vl.json"
    text = json.dumps(structure(rendered.spec), ensure_ascii=False, indent=2) + "\n"
    if os.environ.get("TMIG_UPDATE_SNAPSHOTS") == "1":
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_text(text, encoding="utf-8", newline="\n")
    assert snapshot.is_file(), f"缺少快照 {snapshot}"
    assert text == snapshot.read_text(encoding="utf-8"), f"{worksheet} 的規格變了；審核後更新快照"


def test_marks_and_labels(ir) -> None:
    def render(worksheet: str) -> dict[str, Any]:
        query = worksheet_sql(ir, worksheet)
        rendered = build_spec(
            ir, worksheet, query.columns, execute(DEMO / "data" / "ds_orders.duckdb", query.sql)
        )
        assert rendered.spec is not None
        return rendered.spec

    bars = render("ws_sales_by_region")
    assert bars["layer"][0]["mark"]["type"] == "bar"
    assert bars["encoding"]["y"]["sort"] is None  # 依查詢結果的排序
    assert bars["layer"][1]["encoding"]["x"]["stack"] == "zero"
    line = render("ws_monthly_profit")
    assert line["mark"]["type"] == "line"
    assert line["encoding"]["x"]["timeUnit"] == "utcyearmonth"
    table = render("ws_region_share_by_year")
    assert table["mark"]["type"] == "text"
    assert table["data"]["values"][0]["c2_label"].endswith("%")


def test_unsupported_mark(ir) -> None:
    sheet = ir.worksheets[0].model_copy(deep=True)
    sheet.marks.type = "map"
    patched = ir.model_copy(update={"worksheets": [sheet, *ir.worksheets[1:]]})
    query = worksheet_sql(patched, sheet.id)
    rendered = build_spec(
        patched,
        sheet.id,
        query.columns,
        QueryResult(columns=[c.key for c in query.columns], rows=[]),
    )
    assert rendered.spec is None
    assert rendered.unsupported == "標記類型 map 尚未支援"
