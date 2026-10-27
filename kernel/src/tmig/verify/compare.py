"""預期 CSV 與查詢結果的比對（E3）。

判定規則：

- 列數必須相同；以所有維度欄的值配對，缺少或多出的列都算不一致
- 數值：相對誤差 ≤ 1e-6（接近 0 時以絕對誤差 1e-9 為下限）；預期值帶顯示格式時，
  差距在顯示精度的一半以內視為一致（例如 12.3% 允許 ±0.05%）
- 文字與日期必須完全相同；NULL 只與 NULL 相同
- 預期 CSV 若為 Measure Names／Measure Values 的長表格式，先轉成寬表
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from typing import Any

from tmig.data.executor import QueryResult, format_value
from tmig.sqlgen.query import OutputColumn
from tmig.verify.mapping import NumericMode, SheetMapping
from tmig.verify.normalize import (
    NormalizeError,
    is_null,
    normalize_actual,
    parse_bool,
    parse_date,
    parse_number,
)

REL_TOL = 1e-6
ABS_TOL = 1e-9
MAX_DETAILS = 20
MEASURE_NAMES = ("Measure Names", "度量名稱", "メジャーネーム")
MEASURE_VALUES = ("Measure Values", "度量值", "メジャーバリュー")


@dataclass
class CellDiff:
    row: dict[str, Any]
    column: str
    expected: str
    actual: str
    note: str


@dataclass
class Comparison:
    expected_rows: int
    actual_rows: int
    errors: list[str] = field(default_factory=list)
    missing_rows: list[dict[str, Any]] = field(default_factory=list)
    extra_rows: list[dict[str, Any]] = field(default_factory=list)
    cells: list[CellDiff] = field(default_factory=list)
    tolerance: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.errors or self.missing_rows or self.extra_rows or self.cells) and (
            self.expected_rows == self.actual_rows
        )


def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return [], []
    headers, body = rows[0], [r for r in rows[1:] if any(cell.strip() for cell in r)]
    return _pivot(headers, body)


def _pivot(headers: list[str], body: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Measure Names／Measure Values 長表 → 寬表。"""
    names = next((h for h in headers if h in MEASURE_NAMES), None)
    values = next((h for h in headers if h in MEASURE_VALUES), None)
    if names is None or values is None:
        return headers, body
    ni, vi = headers.index(names), headers.index(values)
    keys = [i for i in range(len(headers)) if i not in (ni, vi)]
    measures: list[str] = []
    grouped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in body:
        key = tuple(row[i] for i in keys)
        measure = row[ni]
        if measure not in measures:
            measures.append(measure)
        grouped.setdefault(key, {})[measure] = row[vi]
    wide_headers = [headers[i] for i in keys] + measures
    wide = [list(key) + [values_.get(m, "") for m in measures] for key, values_ in grouped.items()]
    return wide_headers, wide


def _expected_value(text: str, datatype: str) -> Any:
    if is_null(text):
        return None
    if datatype in ("integer", "real"):
        return parse_number(text).value
    if datatype in ("date", "datetime"):
        return parse_date(text)
    if datatype == "boolean":
        return parse_bool(text)
    return text


def _numbers_match(text: str, actual: float, mode: NumericMode) -> tuple[bool, str | None]:
    parsed = parse_number(text)
    expected = parsed.value
    diff = abs(expected - actual)
    if diff <= max(REL_TOL * max(abs(expected), abs(actual)), ABS_TOL):
        return True, None
    display = mode == "display" or (mode == "auto" and parsed.formatted)
    if display and parsed.decimals is not None:
        half = 0.5 * 10 ** (-parsed.decimals)
        if diff <= half * (1 + 1e-9):
            return True, f"以顯示精度比對（±{half:g}）"
    relative = (
        diff / max(abs(expected), abs(actual)) if max(abs(expected), abs(actual)) else math.inf
    )
    return False, f"差 {diff:.6g}（相對 {relative:.3g}）"


def compare(
    expected_text: str,
    result: QueryResult,
    columns: list[OutputColumn],
    mapping: SheetMapping,
) -> Comparison:
    headers, body = read_csv(expected_text)
    comparison = Comparison(expected_rows=len(body), actual_rows=len(result.rows))
    info = {c.key: c for c in columns}
    index = {key: i for i, key in enumerate(result.columns)}

    mapped: list[tuple[int, str]] = []
    for i, header in enumerate(headers):
        if header not in mapping.columns:
            comparison.errors.append(f"CSV 欄位「{header}」沒有在 mapping.yaml 中對照")
            continue
        key = mapping.columns[header]
        if key is None:
            continue
        if key not in info or key not in index:
            comparison.errors.append(f"mapping.yaml 把「{header}」對到 {key}，查詢結果沒有這個欄位")
            continue
        mapped.append((i, key))
    covered = {key for _, key in mapped}
    for column in columns:
        if column.role != "sort" and column.key not in covered and column.key not in mapping.ignore:
            comparison.errors.append(f"查詢結果的 {column.key} 沒有對應的 CSV 欄位")
    if comparison.errors:
        return comparison

    dims = [(i, key) for i, key in mapped if info[key].role == "dimension"]
    measures = [(i, key) for i, key in mapped if info[key].role == "measure"]

    def expected_key(row: list[str]) -> tuple[Any, ...]:
        return tuple(_expected_value(row[i], info[key].datatype) for i, key in dims)

    def actual_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(normalize_actual(row[index[key]], info[key].datatype) for _, key in dims)

    try:
        expected_rows: dict[tuple[Any, ...], list[str]] = {}
        for row in body:
            key = expected_key(row)
            if key in expected_rows:
                comparison.errors.append(f"預期 CSV 有重複的維度組合 {key}")
            expected_rows[key] = row
    except NormalizeError as exc:
        comparison.errors.append(f"預期 CSV 的維度值無法解析：{exc}")
        return comparison
    actual_rows: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in result.rows:
        key = actual_key(row)
        if key in actual_rows:
            comparison.errors.append(f"查詢結果有重複的維度組合 {key}")
        actual_rows[key] = row

    names = [key for _, key in dims]
    for key in expected_rows:
        if key not in actual_rows:
            comparison.missing_rows.append(dict(zip(names, key, strict=True)))
    for key in actual_rows:
        if key not in expected_rows:
            comparison.extra_rows.append(dict(zip(names, key, strict=True)))

    notes: set[str] = set()
    for key, expected in expected_rows.items():
        actual = actual_rows.get(key)
        if actual is None:
            continue
        for i, column_key in measures:
            datatype = info[column_key].datatype
            text = expected[i] if i < len(expected) else ""
            value = actual[index[column_key]]
            ok, note = _cell_matches(text, value, datatype, mapping.numeric)
            if note and ok:
                notes.add(f"{column_key}：{note}")
            if not ok:
                comparison.cells.append(
                    CellDiff(
                        row=dict(zip(names, key, strict=True)),
                        column=column_key,
                        expected=text,
                        actual=format_value(value),
                        note=note or "",
                    )
                )
    comparison.tolerance = sorted(notes)
    return comparison


def _cell_matches(
    text: str, value: Any, datatype: str, mode: NumericMode
) -> tuple[bool, str | None]:
    if is_null(text) or value is None:
        if is_null(text) and value is None:
            return True, None
        return False, "一邊是 NULL"
    try:
        if datatype in ("integer", "real"):
            return _numbers_match(text, float(value), mode)
        expected = _expected_value(text, datatype)
    except NormalizeError as exc:
        return False, str(exc)
    actual = normalize_actual(value, datatype)
    return (expected == actual, None if expected == actual else "值不同")
