from __future__ import annotations

import datetime as dt

from tmig.data.executor import QueryResult
from tmig.ir.models import DateDerivation, FieldUse
from tmig.sqlgen.query import OutputColumn
from tmig.verify.compare import compare, read_csv
from tmig.verify.mapping import SheetMapping

COLUMNS = [
    OutputColumn("f_region", "dimension", "string", FieldUse(field="f_region")),
    OutputColumn(
        "year(f_order_date)",
        "dimension",
        "integer",
        FieldUse(field="f_order_date", date=DateDerivation(kind="part", unit="year")),
    ),
    OutputColumn("sum(f_sales)", "measure", "real", FieldUse(field="f_sales", agg="sum")),
    OutputColumn("__sort(f_region)", "sort", "real", None),
]
RESULT = QueryResult(
    columns=["f_region", "year(f_order_date)", "sum(f_sales)", "__sort(f_region)"],
    rows=[("East", 2023, 1234.5678, 1.0), ("West", 2023, 0.1234, 2.0), ("West", 2024, None, 2.0)],
)
MAPPING = SheetMapping(
    columns={
        "Region": "f_region",
        "Year of Order Date": "year(f_order_date)",
        "Sales": "sum(f_sales)",
    }
)


def run(csv_text: str, mapping: SheetMapping = MAPPING):
    return compare(csv_text, RESULT, COLUMNS, mapping)


def test_exact_match_in_any_row_order() -> None:
    result = run(
        "Region,Year of Order Date,Sales\nWest,2024,\nWest,2023,0.1234\nEast,2023,1234.5678\n"
    )
    assert result.ok, result


def test_relative_tolerance() -> None:
    assert run(
        "Region,Year of Order Date,Sales\nEast,2023,1234.5679\nWest,2023,0.1234\nWest,2024,\n"
    ).ok
    bad = run("Region,Year of Order Date,Sales\nEast,2023,1234.6\nWest,2023,0.1234\nWest,2024,\n")
    assert not bad.ok
    (cell,) = bad.cells
    assert (cell.row, cell.column, cell.expected) == (
        {"f_region": "East", "year(f_order_date)": 2023.0},
        "sum(f_sales)",
        "1234.6",
    )


def test_display_formatted_values_use_display_precision() -> None:
    text = (
        'Region,Year of Order Date,Sales\nEast,2023,"1,234.57"\nWest,2023,12.3%\nWest,2024,Null\n'
    )
    assert run(text).ok
    raw = SheetMapping(columns=MAPPING.columns, numeric="raw")
    assert not run(text, raw).ok


def test_missing_and_extra_rows() -> None:
    result = run("Region,Year of Order Date,Sales\nEast,2023,1234.5678\nSouth,2023,1\n")
    assert result.missing_rows == [{"f_region": "South", "year(f_order_date)": 2023.0}]
    assert len(result.extra_rows) == 2
    assert not result.ok


def test_null_mismatch() -> None:
    result = run("Region,Year of Order Date,Sales\nEast,2023,1234.5678\nWest,2023,\nWest,2024,5\n")
    assert {c.note for c in result.cells} == {"一邊是 NULL"}


def test_mapping_errors() -> None:
    result = run("Region,Year,Sales\nEast,2023,1\n")
    assert any("Year" in e for e in result.errors)
    partial = SheetMapping(columns={"Region": "f_region", "Sales": "sum(f_sales)"})
    result = run("Region,Sales\nEast,1\n", partial)
    assert any("year(f_order_date)" in e for e in result.errors)
    ignored = SheetMapping(
        columns={"Region": "f_region", "Sales": "sum(f_sales)"}, ignore=["year(f_order_date)"]
    )
    errors = run("Region,Sales\nEast,1\n", ignored).errors
    assert not any("沒有對應的 CSV 欄位" in e for e in errors)
    # 忽略維度後，查詢結果的維度組合不再唯一，要明確報錯
    assert any("重複的維度組合" in e for e in errors)


def test_measure_names_values_long_format() -> None:
    headers, rows = read_csv(
        "Region,Measure Names,Measure Values\nEast,Sales,1\nEast,Profit,2\nWest,Sales,3\n"
    )
    assert headers == ["Region", "Sales", "Profit"]
    assert rows == [["East", "1", "2"], ["West", "3", ""]]


def test_bom_and_dates() -> None:
    headers, rows = read_csv("﻿A,B\n1,2\n\n")
    assert headers == ["A", "B"] and rows == [["1", "2"]]
    date_col = OutputColumn(
        "trunc_month(f_order_date)", "dimension", "date", FieldUse(field="f_order_date")
    )
    result = compare(
        "Month,Sales\nJanuary 2024,5\n",
        QueryResult(
            columns=["trunc_month(f_order_date)", "sum(f_sales)"], rows=[(dt.date(2024, 1, 1), 5.0)]
        ),
        [date_col, COLUMNS[2]],
        SheetMapping(columns={"Month": "trunc_month(f_order_date)", "Sales": "sum(f_sales)"}),
    )
    assert result.ok
