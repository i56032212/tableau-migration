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
from tmig.planner.state import QueryState
from tmig.render.vegalite import LABEL_SEPARATION, build_spec
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


def sort_conflicts(spec: dict[str, Any]) -> list[str]:
    """離散的座標（x、y）在共用同一個比例尺的各圖層中排序（sort）不一致的地方；都一致時是空的。

    Vega-Lite 把各圖層的定義域合併成一個比例尺：只要有一層沒有寫 sort（預設是依值遞增），或各層的 sort
    不同，合併後就改成依名稱排列——查詢排好的順序（主圖的 sort: null）不見了。主圖上疊的每一個圖層
    （註解、參考線、標籤、色帶、表頭…）在離散的座標上都要帶著與主圖相同的 sort。"""
    found: list[str] = []

    def leaves(node: dict[str, Any], inherited: dict[str, Any], path: str) -> Any:
        encoding = {**inherited, **(node.get("encoding") or {})}
        if "layer" in node:
            for i, child in enumerate(node["layer"]):
                yield from leaves(child, encoding, f"{path}.layer[{i}]")
        elif "mark" in node:
            yield f"{path}（{node.get('name', '')}）", encoding

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        for key in ("vconcat", "hconcat", "concat"):
            for i, child in enumerate(node.get(key, [])):
                visit(child, f"{path}.{key}[{i}]")
        if "spec" in node:
            visit(node["spec"], f"{path}.spec")
        if "layer" not in node:
            return
        independent = (node.get("resolve") or {}).get("scale") or {}
        for channel in ("x", "y"):
            if independent.get(channel) == "independent":
                # 各層各自的比例尺（雙軸）：每一層自己檢查
                for i, child in enumerate(node["layer"]):
                    if "layer" in child:
                        visit({"layer": child["layer"]}, f"{path}.layer[{i}]")
                continue
            sorts: dict[str, list[str]] = {}
            for where, encoding in leaves(node, {}, path):
                enc = encoding.get(channel)
                if (
                    isinstance(enc, dict)
                    and enc.get("type") in ("nominal", "ordinal")
                    and "field" in enc
                ):
                    sort = json.dumps(enc["sort"], ensure_ascii=False) if "sort" in enc else "沒有"
                    sorts.setdefault(sort, []).append(where)
            if len(sorts) > 1:
                found.append(f"{path} 的 {channel}：{sorts}")

    visit(spec, "spec")
    return found


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
    # 主圖上疊的圖層（註解、參考線、標籤…）不改掉離散座標的排序
    assert sort_conflicts(rendered.spec) == []
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
    # 檢視器上顯示的原因：標記類型寫標記卡選單上的名稱（原本是「標記類型 map 尚未支援」）
    assert rendered.unsupported == "標記類型「地圖」尚未支援"


def test_view_without_marks_is_blank(ir) -> None:
    """沒有任何標記（例如篩選之後沒有資料）：空白的檢視，不畫座標軸、表頭與格線（稽核 B33：原本畫出座標軸與
    一個「0.000000」的刻度）；檢視器畫標題與背景用的資料仍然附上。"""
    state = QueryState.model_validate(
        {"filters": {"f_category": {"mode": "include", "values": ["沒有這個類別"]}}}
    )
    query = worksheet_sql(ir, "ws_sales_by_region", state)
    result = execute(DEMO / "data" / "ds_orders.duckdb", query.sql)
    assert result.rows == []
    rendered = build_spec(ir, "ws_sales_by_region", query.columns, result)
    assert rendered.spec is not None and rendered.rows == 0 and rendered.unsupported is None
    spec = rendered.spec
    assert spec["data"] == {"values": []}
    assert not {"encoding", "layer", "vconcat", "hconcat", "facet"} & spec.keys()
    assert spec["usermeta"]["name"] == "Sales by Region" and spec["usermeta"]["rows"] == 0
    assert {"title_font", "title_color", "dimensions", "measures"} <= spec["usermeta"].keys()
    assert "sizing" not in spec["usermeta"]
    assert [e.message for e in validator().iter_errors(spec)] == []
    # 有資料時照常畫出座標軸
    full = worksheet_sql(ir, "ws_sales_by_region")
    drawn = build_spec(
        ir,
        "ws_sales_by_region",
        full.columns,
        execute(DEMO / "data" / "ds_orders.duckdb", full.sql),
    )
    assert drawn.spec is not None and drawn.rows > 0 and "encoding" in drawn.spec


def test_axis_labels_keep_a_gap(ir) -> None:
    """相鄰的刻度文字至少相隔幾個像素，不到時 Vega 每隔一個省略（稽核 B32：Vega 的預設是 0，兩個文字只差
    0.02 像素也都留著，看起來是「90,000100,000」）。"""
    query = worksheet_sql(ir, "ws_sales_by_region")
    rendered = build_spec(
        ir,
        "ws_sales_by_region",
        query.columns,
        execute(DEMO / "data" / "ds_orders.duckdb", query.sql),
    )
    assert rendered.spec is not None
    assert rendered.spec["config"]["axis"]["labelSeparation"] == LABEL_SEPARATION
    assert LABEL_SEPARATION >= 3
