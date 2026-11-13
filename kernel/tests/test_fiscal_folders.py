"""會計年度、資料窗格的資料夾與合併欄位（tmig.field_edit 與查詢）：

- 會計年度：資料源的起始月（日期屬性）與日期欄位自訂的起始月；年、季（部分與截斷）與週數依會計年度計算，
  月與日不變；年、季的標籤加上 FY（Tableau 說明文件「會計日期」的例子：四月起算時 2004 年 6 月 1 日是
  FY 2005 的 Q1、2004 年 4 月 1 日是第 1 週；五月起算時年的截斷是 2004 年 5 月 1 日；七月起算時季的截斷
  是 2004 年 4 月 1 日）
- 資料夾：建立、移入與移出、改名、刪除；移除的欄位一併從資料夾拿掉
- 合併欄位：兩個以上的維度，值為「值, 值」
以 CSV 建立報表後匯入，以 DuckDB 查詢比對手算的結果。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from tmig.authoring import inspect_file, new_workbook
from tmig.data.executor import execute_query
from tmig.data.hyper_import import import_sources
from tmig.edit import EditError, catalog, replace_worksheet
from tmig.field_edit import (
    create_combined,
    create_folder,
    move_to_folder,
    remove_field,
    remove_folder,
    rename_folder,
    set_date_properties,
    set_field_properties,
)
from tmig.ir import IR
from tmig.ir.integrity import check_references
from tmig.render.format import fiscal_label
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

ROWS = [
    ("2004-03-31", "East", "Chair", 1),
    ("2004-04-01", "East", "Desk", 2),
    ("2004-06-15", "West", "Chair", 4),
    ("2004-07-01", "West", None, 8),
    ("2005-01-10", "East", "Lamp", 16),
    ("2005-04-02", "South", "Desk", 32),
]


@pytest.fixture
def workbook(tmp_path: Path) -> tuple[IR, Path]:
    csv = tmp_path / "orders.csv"
    lines = ["Order Date,Region,Product,Sales"]
    lines += [f"{d},{r},{p or ''},{s}" for d, r, p, s in ROWS]
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sources = tmp_path / "sources" / "Data"
    sources.mkdir(parents=True)
    (sources / "orders.csv").write_bytes(csv.read_bytes())
    ir = new_workbook("Fiscal", inspect_file(csv), None, "Data/orders.csv")
    return ir, tmp_path


def ids(ir: IR) -> dict[str, str]:
    return {f.caption: f.id for f in ir.datasources[0].fields}


def query(ir: IR, folder: Path, use: dict[str, Any], labels: bool = False) -> dict[Any, Any]:
    """use 為列上的欄位，量值為 SUM(Sales)；labels 時回傳標籤 → 值。"""
    assert check_references(ir) == []
    ds = ir.datasources[0]
    sheet = ir.worksheets[0].model_dump(mode="json")
    sheet.update(datasource=ds.id, rows=[use], columns=[{"field": ids(ir)["Sales"], "agg": "sum"}])
    ir, ws = replace_worksheet(ir, sheet)
    (result,) = import_sources(folder / "sources", folder / "data", ir, only=[ds.id])
    assert result.ok, result.to_json()
    built = worksheet_sql(ir, ws.id)
    found = execute_query(folder / "data" / f"{ds.id}.duckdb", built)
    if not labels:
        return {row[0]: row[1] for row in found.rows}
    spec = build_spec(ir, ws.id, built.columns, found).spec
    assert spec is not None
    values = spec["data"]["values"] if "values" in spec.get("data", {}) else spec["datasets"]
    if isinstance(values, dict):
        values = next(iter(values.values()))
    return {v["c0_label"]: v["c1"] for v in values if "c0_label" in v}


def fiscal_week(day: dt.date, start: int, week_start: int = 6) -> int:
    """會計年度的週數（手算）：會計年度第一天所在的週為第 1 週；week_start 為週起始日（Python 的
    weekday，6 為星期日）。"""
    year = day.year if day.month >= start else day.year - 1
    first = dt.date(year, start, 1)

    def week_of(d: dt.date) -> dt.date:
        return d - dt.timedelta(days=(d.weekday() - week_start) % 7)

    return (week_of(day) - week_of(first)).days // 7 + 1


def test_fiscal_year(workbook: tuple[IR, Path]) -> None:
    ir, folder = workbook
    ds = ir.datasources[0].id
    date = ids(ir)["Order Date"]
    ir = set_date_properties(ir, ds, fiscal_year_start=4)
    assert ir.datasources[0].settings.fiscal_year_start == 4

    def part(unit: str) -> dict[str, Any]:
        return {"field": date, "date": {"kind": "part", "unit": unit}}

    def trunc(unit: str) -> dict[str, Any]:
        return {"field": date, "date": {"kind": "trunc", "unit": unit}}

    # 年：以結束的年份命名（2004-06-15 是 FY 2005）
    assert query(ir, folder, part("year")) == {2004: 1, 2005: 30, 2006: 32}
    # 季：四月是第一季
    assert query(ir, folder, part("quarter")) == {1: 38, 2: 8, 4: 17}
    # 月不變
    assert query(ir, folder, part("month")) == {3: 1, 4: 34, 6: 4, 7: 8, 1: 16}
    # 週數：2004-04-01 是第 1 週
    expected: dict[int, int] = {}
    for day, _, _, sales in ROWS:
        week = fiscal_week(dt.date.fromisoformat(day), 4)
        expected[week] = expected.get(week, 0) + sales
    assert query(ir, folder, part("week")) == expected
    assert expected[1] == 34
    # 截斷：會計年度（季）的第一天
    assert query(ir, folder, trunc("year")) == {
        dt.date(2003, 4, 1): 1,
        dt.date(2004, 4, 1): 30,
        dt.date(2005, 4, 1): 32,
    }
    assert query(ir, folder, trunc("quarter")) == {
        dt.date(2004, 1, 1): 1,
        dt.date(2004, 4, 1): 6,
        dt.date(2004, 7, 1): 8,
        dt.date(2005, 1, 1): 16,
        dt.date(2005, 4, 1): 32,
    }
    # 月的截斷不變
    assert dt.date(2004, 6, 1) in query(ir, folder, trunc("month"))
    # 標籤加上 FY
    assert query(ir, folder, part("year"), labels=True) == {
        "FY 2004": 1,
        "FY 2005": 30,
        "FY 2006": 32,
    }
    assert query(ir, folder, trunc("quarter"), labels=True)["FY 2005 Q4"] == 16
    # 欄位自訂的起始月（七月）優先
    ir = set_field_properties(ir, ds, date, fiscal_year_start=7)
    assert query(ir, folder, part("year")) == {2004: 7, 2005: 56}
    # 沿用資料源
    ir = set_field_properties(ir, ds, date, fiscal_year_start=None)
    assert query(ir, folder, part("year")) == {2004: 1, 2005: 30, 2006: 32}
    with pytest.raises(EditError, match="不是日期"):
        set_field_properties(ir, ds, ids(ir)["Region"], fiscal_year_start=4)
    with pytest.raises(EditError, match="1 到 12"):
        set_date_properties(ir, ds, fiscal_year_start=13)
    # 日曆年：與原本相同
    ir = set_date_properties(ir, ds, fiscal_year_start=1)
    assert query(ir, folder, part("year")) == {2004: 15, 2005: 48}


def test_fiscal_labels_from_documentation() -> None:
    """Tableau 說明文件的例子。"""
    # 四月起算：2004 年 6 月 1 日的年是 FY 2005
    assert fiscal_label(2005, "part", "year", 4) == "FY 2005"
    # 五月起算：年的截斷是 2004 年 5 月 1 日，即 FY 2005
    assert fiscal_label(dt.date(2004, 5, 1), "trunc", "year", 5) == "FY 2005"
    # 七月起算：季的截斷是 2004 年 4 月 1 日，是 FY 2004 的第四季
    assert fiscal_label(dt.date(2004, 4, 1), "trunc", "quarter", 7) == "FY 2004 Q4"
    assert fiscal_label(dt.date(2004, 7, 1), "trunc", "quarter", 7) == "FY 2005 Q1"
    assert fiscal_label(2005, "part", "year", 1) is None
    assert fiscal_label(3, "part", "quarter", 4) is None


def test_folders(workbook: tuple[IR, Path]) -> None:
    ir, _ = workbook
    ds = ir.datasources[0].id
    f = ids(ir)
    ir = create_folder(ir, ds, "Where", [f["Region"]])
    ir = create_folder(ir, ds, "Amounts", [f["Sales"]])
    folders = ir.datasources[0].folders
    # 依名稱排序
    assert [(x.name, x.fields) for x in folders] == [
        ("Amounts", [f["Sales"]]),
        ("Where", [f["Region"]]),
    ]
    with pytest.raises(EditError, match="已經有"):
        create_folder(ir, ds, "where")
    ir = move_to_folder(ir, ds, [f["Product"], f["Region"]], "Amounts")
    assert [(x.name, x.fields) for x in ir.datasources[0].folders] == [
        ("Amounts", [f["Sales"], f["Product"], f["Region"]]),
        ("Where", []),
    ]
    ir = move_to_folder(ir, ds, [f["Sales"]], None)
    ir = rename_folder(ir, ds, "Amounts", "Details")
    shown = catalog(ir)["datasources"][0]["folders"]
    assert [(x["name"], x["fields"]) for x in shown] == [
        ("Details", [f["Product"], f["Region"]]),
        ("Where", []),
    ]
    # 刪除的欄位一併從資料夾拿掉
    ir, combined = create_combined(ir, ds, [f["Region"], f["Product"]])
    ir = move_to_folder(ir, ds, [combined.id], "Where")
    ir = remove_field(ir, ds, combined.id)
    assert ir.datasources[0].folders[1].fields == []
    ir = remove_folder(ir, ds, "Details")
    assert [x.name for x in ir.datasources[0].folders] == ["Where"]


def test_combined_field(workbook: tuple[IR, Path]) -> None:
    ir, folder = workbook
    ds = ir.datasources[0].id
    f = ids(ir)
    ir, combined = create_combined(ir, ds, [f["Region"], f["Product"]])
    assert combined.caption == "Region & Product (Combined)"
    assert combined.combined == [f["Region"], f["Product"]]
    assert query(ir, folder, {"field": combined.id}) == {
        "East, Chair": 1,
        "East, Desk": 2,
        "West, Chair": 4,
        "West, Null": 8,
        "East, Lamp": 16,
        "South, Desk": 32,
    }
    entry = next(x for x in catalog(ir)["datasources"][0]["fields"] if x["id"] == combined.id)
    assert entry["combined"] == [f["Region"], f["Product"]]
    with pytest.raises(EditError, match="兩個以上"):
        create_combined(ir, ds, [f["Region"]])
    with pytest.raises(EditError, match="不是維度"):
        create_combined(ir, ds, [f["Region"], f["Sales"]])
    # 組成的欄位不能刪除（合併欄位用到）
    with pytest.raises(EditError):
        remove_field(ir, ds, f["Region"])
