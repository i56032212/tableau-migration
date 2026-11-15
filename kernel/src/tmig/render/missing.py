"""顯示遺漏的值（Tableau 日期與數值分組膠囊選單的「顯示遺漏的值」）。

查詢結果只有資料中出現過的值；開啟「顯示遺漏的值」的欄位，在最小值到最大值之間補上沒有資料的成員
（日期的部分與截斷依單位、數值分組依分組的大小），標題與座標軸上看得到這些成員，標記是空的（量值為 Null，
折線在這裡斷開）。補上的列放在查詢結果中依排序應該在的位置，原有的列不動。

補上的範圍與 Tableau 相同是整個檢視的最小值到最大值；其他維度的每一種組合（每個窗格、每條線）各補一列。
表計算在資料庫中先算好，不含補上的列（Tableau 補上之後才算表計算）。
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Sequence
from typing import Any

from tmig.data.executor import QueryResult
from tmig.ir import IR, DataField, FieldUse, Worksheet
from tmig.ir.models import ParamRef, Scalar
from tmig.planner.state import use_key

MAX_PADDED = 20000
"""補上的列數上限；超過時不補（附註說明）。"""
COLUMN_ORDER = "__order(columns)"
"""欄標題名次的欄（見 tmig.sqlgen.query）：補上的欄標題取前後之間的名次。"""


def _add_months(value: dt.date, months: int) -> dt.date:
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    day = min(value.day, 28)
    return value.replace(year=year, month=month + 1, day=day)


def _step(value: Any, unit: str) -> Any:
    if unit == "year":
        return _add_months(value, 12)
    if unit == "quarter":
        return _add_months(value, 3)
    if unit == "month":
        return _add_months(value, 1)
    days = {"week": 7, "day": 1}.get(unit)
    if days is not None:
        return value + dt.timedelta(days=days)
    seconds = {"hour": 3600, "minute": 60, "second": 1}.get(unit)
    if seconds is not None and isinstance(value, dt.datetime):
        return value + dt.timedelta(seconds=seconds)
    return None


def missing_domain(
    use: FieldUse,
    field: DataField | None,
    present: Sequence[Any],
    parameters: dict[str, Scalar],
) -> list[Any] | None:
    """最小值到最大值之間所有的成員（由小到大）；這個欄位不能補（不是日期或數值分組）時為 None。"""
    values = [v for v in present if v is not None]
    if not values:
        return None
    if use.date is not None and use.date.kind == "part":
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return None
        return list(range(min(values), max(values) + 1))
    if use.date is not None and use.date.kind == "trunc":
        if not all(isinstance(v, dt.date) for v in values):
            return None
        low, high = min(values), max(values)
        found = [low]
        while found[-1] < high:
            following = _step(found[-1], use.date.unit)
            if following is None or len(found) > MAX_PADDED:
                return None
            found.append(following)
        return found
    if field is not None and field.bin is not None and use.agg is None:
        size: Any = field.bin.size
        if isinstance(size, ParamRef):
            size = parameters.get(size.parameter)
        numeric = all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)
        if not numeric or not isinstance(size, int | float) or size <= 0:
            return None
        low, high = min(values), max(values)
        count = round((high - low) / size)
        if count > MAX_PADDED:
            return None
        integral = all(isinstance(v, int) for v in values) and float(size).is_integer()
        steps = [low + i * size for i in range(count + 1)]
        return [int(v) for v in steps] if integral else steps
    return None


def _direction(blocks: list[list[tuple[Any, ...]]], at: int) -> str | None:
    """各段中這個欄位的值是由小到大（asc）還是由大到小（desc）；不是依值排序時為 None。"""
    found: str | None = None
    for block in blocks:
        seen: list[Any] = []
        for row in block:
            value = row[at]
            if value is not None and (not seen or seen[-1] != value):
                seen.append(value)
        if len(set(seen)) != len(seen):
            return None  # 同一個值不相鄰：不是依這個欄位排序
        for a, b in itertools.pairwise(seen):
            step = "asc" if a < b else "desc"
            if found is None:
                found = step
            elif found != step:
                return None
    return found or "asc"


def _pad(
    result: QueryResult,
    dims: list[int],
    at: int,
    domain: list[Any],
    emptied: list[int],
    column_dims: list[int],
) -> QueryResult | None:
    """補上 at 欄（dims 中的一欄，dims 為依排序先後的維度欄）在 domain 中沒有的值；回傳 None 表示
    列的順序不是預期的（外層維度相同的列不相鄰）或補的列太多，不補。"""
    position = dims.index(at)
    prefix, suffix = dims[:position], dims[position + 1 :]
    blocks: list[list[tuple[Any, ...]]] = []
    seen: set[tuple[Any, ...]] = set()
    current: tuple[Any, ...] | None = None
    for row in result.rows:
        key = tuple(row[i] for i in prefix)
        if key != current:
            if key in seen:
                return None
            seen.add(key)
            current = key
            blocks.append([])
        blocks[-1].append(row)
    direction = _direction(blocks, at)
    ordered = domain if direction != "desc" else domain[::-1]
    rank = {value: i for i, value in enumerate(ordered)}

    # 欄標題的名次：補上的欄標題排在同一段（外層欄維度相同）中前一個值之後
    order_at = result.columns.index(COLUMN_ORDER) if COLUMN_ORDER in result.columns else None
    in_columns = order_at is not None and at in column_dims
    col_prefix = column_dims[: column_dims.index(at)] if in_columns else []
    col_suffix = column_dims[column_dims.index(at) + 1 :] if in_columns else []
    ranks: dict[tuple[Any, ...], list[tuple[Any, float]]] = {}
    suffixes: dict[tuple[Any, ...], int] = {}
    if in_columns:
        assert order_at is not None
        for row in result.rows:
            if row[order_at] is None:
                continue
            ranks.setdefault(tuple(row[i] for i in col_prefix), []).append((row[at], row[order_at]))
            suffixes.setdefault(tuple(row[i] for i in col_suffix), len(suffixes))

    def column_rank(row: tuple[Any, ...], value: Any) -> float | None:
        found = ranks.get(tuple(row[i] for i in col_prefix))
        if not found:
            return None
        if direction is None:
            before = [r for _, r in found]
            base: float = max(before)
        else:
            before = [r for v, r in found if v is not None and rank.get(v, -1) < rank[value]]
            base = max(before) if before else min(r for _, r in found) - 1
        index = rank[value] * len(suffixes) + suffixes[tuple(row[i] for i in col_suffix)]
        return base + (index + 1) / (len(ordered) * len(suffixes) + 1)

    def padded(template: tuple[Any, ...], value: Any) -> tuple[Any, ...]:
        row = list(template)
        row[at] = value
        for i in emptied:
            row[i] = None
        if in_columns and order_at is not None:
            row[order_at] = column_rank(template, value)
        return tuple(row)

    rows: list[tuple[Any, ...]] = []
    added = 0
    for block in blocks:
        # 這一段中其他維度的組合（各窗格、各條線），依出現的順序；每一種各補一列
        templates: dict[tuple[Any, ...], tuple[Any, ...]] = {}
        groups: dict[Any, list[tuple[Any, ...]]] = {}
        for row in block:
            templates.setdefault(tuple(row[i] for i in suffix), row)
            groups.setdefault(row[at], []).append(row)
        added += sum(
            len(templates) - len({tuple(r[i] for i in suffix) for r in groups.get(value, [])})
            for value in ordered
        )
        if added > MAX_PADDED:
            return None

        def filled(value: Any, existing: list[tuple[Any, ...]], templates=templates) -> list[Any]:
            present = {tuple(r[i] for i in suffix) for r in existing}
            extra = [padded(t, value) for key, t in templates.items() if key not in present]
            return [*existing, *extra]

        if direction is None:
            # 不是依值排序（例如依量值或手動排序）：原有的照舊，遺漏的值接在這一段的最後
            for value, existing in groups.items():
                rows += filled(value, existing) if value in rank else existing
            for value in ordered:
                if value not in groups:
                    rows += filled(value, [])
            continue
        nulls = groups.pop(None, [])
        leading = bool(block) and block[0][at] is None
        if leading:
            rows += nulls
        for value in ordered:
            rows += filled(value, groups.pop(value, []))
        for existing in groups.values():  # 不在範圍內的值（不應該有）：照舊
            rows += existing
        if not leading:
            rows += nulls
    return QueryResult(columns=result.columns, rows=rows)


def pad_missing(
    ws: Worksheet,
    fields: dict[str, DataField],
    roles: dict[str, str],
    result: QueryResult,
    parameters: dict[str, Scalar],
    notes: list[str],
) -> QueryResult:
    """工作表上開啟「顯示遺漏的值」的欄位（ws.show_missing，要在欄列架上）補上遺漏的成員。

    roles：查詢結果各欄（欄位鍵）的角色（dimension、measure、sort）。"""
    shelves = [*ws.rows, *ws.columns]
    targets = [u for u in ws.show_missing if u in shelves and use_key(u) in result.columns]
    if not targets or not result.rows:
        return result
    index = {key: i for i, key in enumerate(result.columns)}
    on_shelves = [
        index[use_key(u)]
        for u in shelves
        if use_key(u) in index and roles.get(use_key(u)) == "dimension"
    ]
    others = [
        i for key, i in index.items() if roles.get(key) == "dimension" and i not in on_shelves
    ]
    dims = list(dict.fromkeys([*on_shelves, *others]))
    column_dims = [i for i in dict.fromkeys(index.get(use_key(u)) for u in ws.columns) if i in dims]
    # 補上的列：量值與其他算出來的欄是空的；欄標題的名次另外決定（列維度補上的列沿用同一欄的名次）
    emptied = [
        i
        for key, i in index.items()
        if roles.get(key, "sort") != "dimension" and i not in dims and key != COLUMN_ORDER
    ]
    for use in targets:
        at = index[use_key(use)]
        domain = missing_domain(
            use, fields.get(use.field), [row[at] for row in result.rows], parameters
        )
        if domain is None or at not in dims:
            continue
        padded = _pad(result, dims, at, domain, emptied, column_dims)
        if padded is None:
            notes.append("顯示遺漏的值：要補上的標記太多，或檢視不是依欄列架的順序排列，沒有補上")
            continue
        result = padded
    return result


def _order_key(value: Any) -> tuple[int, Any]:
    return (1, 0) if value is None else (0, value)


def pad_empty(
    result: QueryResult,
    keys: list[str],
    tuples: Sequence[tuple[Any, ...]],
    roles: dict[str, str],
    leading: bool,
) -> QueryResult:
    """顯示空的列／欄：keys（這一架上的維度，依架上的順序）在 tuples 中、但結果裡沒有的成員組合各補一列
    （其他維度沿用第一列的值，量值是空的）。leading：這一架的維度是排序最前面的（列架，或沒有列維度時的
    欄架）——原有的組合依值排序時補在依值應該在的位置，否則接在最後；不是最前面的（列、欄都有維度時的
    欄架）接在資料的最後，欄標題的名次決定它的位置。"""
    index = {key: i for i, key in enumerate(result.columns)}
    at = [index[k] for k in keys if k in index]
    if len(at) != len(keys) or not result.rows:
        return result
    groups: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for row in result.rows:
        groups.setdefault(tuple(row[i] for i in at), []).append(row)
    missing = [t for t in dict.fromkeys(tuple(t) for t in tuples) if t not in groups]
    if not missing or len(missing) > MAX_PADDED:
        return result
    emptied = [
        i
        for key, i in index.items()
        if roles.get(key, "sort") != "dimension" and key != COLUMN_ORDER
    ]
    order_at = index.get(COLUMN_ORDER)
    template = result.rows[0]

    def sort_key(t: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(_order_key(v) for v in t)

    existing = list(groups)
    try:
        natural = existing == sorted(existing, key=sort_key)
        merged = sorted([*existing, *missing], key=sort_key) if natural else [*existing, *missing]
    except TypeError:  # 型別不一致、不能比較：接在最後
        natural, merged = False, [*existing, *missing]

    ranks: dict[tuple[Any, ...], float] = {}
    if order_at is not None and not leading:
        known = {
            t: min(r[order_at] for r in rows if r[order_at] is not None)
            for t, rows in groups.items()
            if any(r[order_at] is not None for r in rows)
        }
        last = max(known.values(), default=0)
        previous: float | None = None
        run = 0
        for t in merged:
            if t in known:
                previous, run = known[t], 0
                continue
            run += 1
            # 依值排序時排在前一個欄標題之後（之間的名次），否則排在所有欄標題之後
            base = (
                previous
                if natural and previous is not None
                else (min(known.values(), default=1) - 1 if natural else last)
            )
            ranks[t] = base + run / (len(missing) + 1)

    def padded(t: tuple[Any, ...]) -> tuple[Any, ...]:
        row = list(template)
        for i, value in zip(at, t, strict=True):
            row[i] = value
        for i in emptied:
            row[i] = None
        if order_at is not None and t in ranks:
            row[order_at] = ranks[t]
        return tuple(row)

    if not leading:
        return QueryResult(result.columns, [*result.rows, *(padded(t) for t in missing)])
    contiguous = [
        t
        for i, row in enumerate(result.rows)
        if i == 0 or tuple(row[j] for j in at) != tuple(result.rows[i - 1][j] for j in at)
        for t in [tuple(row[j] for j in at)]
    ]
    if len(contiguous) != len(groups):
        # 同一個組合的列不相鄰（不是依這一架排序）：接在最後
        return QueryResult(result.columns, [*result.rows, *(padded(t) for t in missing)])
    rows: list[tuple[Any, ...]] = []
    for t in merged:
        rows += groups.get(t) or [padded(t)]
    return QueryResult(result.columns, rows)


def pad_result(
    ir: IR,
    ws: Worksheet,
    columns: Sequence[Any],
    result: QueryResult,
    parameters: dict[str, Scalar] | None = None,
    notes: list[str] | None = None,
) -> QueryResult:
    """工作表的查詢結果補上遺漏的值（沒有開啟「顯示遺漏的值」時原樣回傳）。columns：查詢的輸出欄
    （tmig.sqlgen.query.OutputColumn）；parameters：查詢時的參數值（數值分組的大小是參數時）。"""
    if not ws.show_missing:
        return result
    sources = {ws.datasource, *(b.datasource for b in ws.blends)}
    fields = {f.id: f for d in ir.datasources if d.id in sources for f in d.fields}
    roles = {c.key: c.role for c in columns}
    values = {p.id: p.value for p in ir.parameters} | (parameters or {})
    return pad_missing(ws, fields, roles, result, values, notes if notes is not None else [])


__all__ = ["missing_domain", "pad_empty", "pad_missing", "pad_result"]
