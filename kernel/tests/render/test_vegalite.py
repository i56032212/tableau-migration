"""Vega-Lite 規格（F1）：示範報表每張工作表的規格快照，且符合 Vega-Lite v6 Schema。

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
from tmig.sqlgen.query import worksheet_sql
from tmig.verify.run import ensure_data

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "fixtures" / "superstore_demo"
SCHEMA = Path(__file__).parent / "data" / "vega-lite-v6.5.0.schema.json.gz"
SHEETS = [
    "ws_sales_by_region",
    "ws_monthly_profit",
    "ws_region_share_by_year",
    "ws_sub_category_profit",
]


@cache
def validator() -> jsonschema.Draft7Validator:
    return jsonschema.Draft7Validator(json.loads(gzip.decompress(SCHEMA.read_bytes())))


@pytest.fixture(scope="module")
def ir():
    loaded = load_ir(DEMO / "golden.ir.json")
    ensure_data(DEMO, loaded)
    return loaded


def structure(spec: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(spec))
    values = copy["data"]["values"]
    copy["data"] = {"values": f"{len(values)} 列", "fields": sorted(values[0]) if values else []}
    return copy


@pytest.mark.parametrize("worksheet", SHEETS)
def test_spec_snapshot_and_schema(ir, worksheet: str) -> None:
    query = worksheet_sql(ir, worksheet)
    result = execute(DEMO / "data" / "ds_orders.duckdb", query.sql)
    rendered = build_spec(ir, worksheet, query.columns, result)
    assert rendered.spec is not None
    errors = [e.message for e in validator().iter_errors(rendered.spec)]
    assert errors == []
    snapshot = DEMO / "specs" / f"{worksheet}.vl.json"
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
    sheet.marks.type = "pie"
    patched = ir.model_copy(update={"worksheets": [sheet, *ir.worksheets[1:]]})
    query = worksheet_sql(patched, sheet.id)
    rendered = build_spec(
        patched,
        sheet.id,
        query.columns,
        QueryResult(columns=[c.key for c in query.columns], rows=[]),
    )
    assert rendered.spec is None
    assert rendered.unsupported == "標記類型 pie 尚未支援"
