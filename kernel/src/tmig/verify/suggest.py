"""篩選組合建議（E1）：依 dashboard 上的篩選與參數，用資料挑出預設值以外的 3 組。

    tmig cases suggest fixtures/<報表> [--write]

- common_value：第一個類別篩選中筆數最多的值（最常被看的情況）
- date_boundary：日期欄位資料的最後一年（年度切換、跨年的邊界）
- parameter／combination：改一個參數，或同時設兩個篩選（交互作用）

建議只是起點：擁有者最常用的組合由人判斷，請檢查後再寫入。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tmig.data.executor import execute
from tmig.ir.models import IR, CategoricalFilter, DataField, FieldUse, Parameter, RangeFilter, Zone
from tmig.planner.identity import Identity
from tmig.planner.state import StateError
from tmig.sqlgen.query import QueryError, domain_sql
from tmig.verify.cases import CasesError, CaseSpec, case_state, load_cases
from tmig.verify.run import ensure_data, load_fixture_ir


@dataclass
class Candidate:
    use: FieldUse
    field: DataField
    datasource: str


def _zones(zones: list[Zone]) -> list[Zone]:
    result: list[Zone] = []
    for zone in zones:
        result.append(zone)
        result.extend(_zones(zone.children))
    return result


def _fields(ir: IR) -> dict[str, tuple[DataField, str]]:
    return {f.id: (f, ds.id) for ds in ir.datasources for f in ds.fields}


def candidates(ir: IR) -> list[Candidate]:
    """可以當網址參數的維度：dashboard 的篩選控制項優先，其次是工作表上的篩選與日期維度。"""
    fields = _fields(ir)
    sheets = {w.id: w for w in ir.worksheets}
    found: list[Candidate] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    def add(use: FieldUse | None) -> None:
        if use is None or use.agg is not None or use.table_calc is not None:
            return
        if use.date is not None and use.date.kind != "part":
            return  # 截斷日期（例如月份）沒有對應的網址參數寫法
        item = fields.get(use.field)
        if item is None or item[0].role != "dimension":
            return
        key = (use.field, use.date.kind if use.date else None, use.date.unit if use.date else None)
        if key in seen:
            return
        seen.add(key)
        found.append(Candidate(use=use, field=item[0], datasource=item[1]))

    for board in ir.dashboards:
        for zone in _zones(board.zones):
            if zone.kind == "filter":
                add(zone.field)
    for ws in ir.worksheets:
        for flt in ws.filters:
            if isinstance(flt, CategoricalFilter | RangeFilter) and flt.stage == "dimension":
                add(flt.field)
    for ws in sheets.values():
        for use in (*ws.rows, *ws.columns):
            item = fields.get(use.field)
            if item is not None and item[0].datatype in ("date", "datetime"):
                add(FieldUse(field=use.field, date=use.date) if use.date else use)
    return found


def param_name(candidate: Candidate) -> str:
    date = candidate.use.date
    return (
        f"{date.unit}({candidate.field.caption})" if date is not None else candidate.field.caption
    )


def url_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace(",", "\\,")


def _frequency(
    ir: IR, db: Path, candidate: Candidate, user: Identity | None
) -> list[tuple[Any, int]]:
    sql = domain_sql(ir, candidate.datasource, candidate.use, "frequency", user)
    return [(row[0], int(row[1])) for row in execute(db, sql).rows if row[0] is not None]


def _last_year(ir: IR, db: Path, candidate: Candidate, user: Identity | None) -> int | None:
    use = FieldUse(field=candidate.field.id)
    rows = execute(db, domain_sql(ir, candidate.datasource, use, "range", user)).rows
    high = rows[0][1] if rows else None
    if isinstance(high, dt.date):
        return high.year
    if isinstance(high, str) and re.match(r"^\d{4}-", high):
        return int(high[:4])
    return None


def _parameter_value(param: Parameter) -> Any:
    allowed = param.allowed
    if allowed.kind == "list":
        return next((v for v in allowed.values if v != param.value), None)
    if allowed.kind == "range":
        for value in (allowed.min, allowed.max):
            if value is not None and value != param.value:
                return value
    return None


def suggest_cases(fixture: Path) -> tuple[list[CaseSpec], list[str]]:
    """回傳（建議的篩選組合, 說明）。"""
    ir = load_fixture_ir(fixture)
    notes = ensure_data(fixture, ir)
    cases_path = fixture / "cases.yaml"
    # 列層級權限：可選值以 cases.yaml 的帳號查詢（與預期結果相同的可見範圍）
    user = load_cases(cases_path).user if cases_path.is_file() else None

    def database(ds_id: str) -> Path:
        return fixture / "data" / f"{ds_id}.duckdb"

    found = candidates(ir)
    categorical = [
        c
        for c in found
        if c.use.date is None and c.field.datatype in ("string", "integer", "boolean")
    ]
    dates = [c for c in found if c.field.datatype in ("date", "datetime")]
    frequencies: dict[int, list[tuple[Any, int]]] = {}

    def freq(index: int) -> list[tuple[Any, int]]:
        if index not in frequencies:
            candidate = categorical[index]
            try:
                frequencies[index] = _frequency(ir, database(candidate.datasource), candidate, user)
            except QueryError:
                frequencies[index] = []
        return frequencies[index]

    cases: list[CaseSpec] = []
    if categorical and freq(0):
        value, rows = freq(0)[0]
        name = param_name(categorical[0])
        cases.append(
            CaseSpec(
                name="common_value",
                url_params={name: url_value(value)},
                note=f"常用值：{name} 中筆數最多的值（{rows} 筆）",
            )
        )
    for candidate in dates:
        try:
            year = _last_year(ir, database(candidate.datasource), candidate, user)
        except QueryError:
            continue
        if year is not None:
            name = f"year({candidate.field.caption})"
            cases.append(
                CaseSpec(
                    name="date_boundary",
                    url_params={name: str(year)},
                    note=f"日期邊界：{candidate.field.caption} 資料的最後一年",
                )
            )
            break

    controlled = [z.parameter for b in ir.dashboards for z in _zones(b.zones) if z.parameter]
    params = sorted(ir.parameters, key=lambda p: (p.id not in controlled, p.caption))
    param = next((p for p in params if _parameter_value(p) is not None), None)
    other = 1 if len(categorical) > 1 else 0
    if param is not None:
        url_params: dict[str, str | int | float | bool] = {
            param.caption: url_value(_parameter_value(param))
        }
        note = f"參數：{param.caption} 改為非預設值"
        if categorical and freq(other):
            name = param_name(categorical[other])
            url_params = {name: url_value(freq(other)[0][0]), **url_params}
            note += f"，同時篩選 {name}"
        cases.append(CaseSpec(name="parameter", url_params=url_params, note=note))
    elif len(categorical) > 1 and freq(0) and freq(1):
        first, second = param_name(categorical[0]), param_name(categorical[1])
        rare = freq(0)[-1][0]
        cases.append(
            CaseSpec(
                name="combination",
                url_params={first: url_value(rare), second: url_value(freq(1)[0][0])},
                note=f"組合：{first} 筆數最少的值，加上 {second} 最常見的值",
            )
        )
    elif categorical and len(freq(0)) > 1:
        name = param_name(categorical[0])
        cases.append(
            CaseSpec(
                name="rare_value",
                url_params={name: url_value(freq(0)[-1][0])},
                note=f"少見值：{name} 中筆數最少的值",
            )
        )

    valid: list[CaseSpec] = []
    for case in cases:
        try:
            case_state(ir, case)
        except (CasesError, StateError) as exc:
            notes.append(f"略過 {case.name}：{exc}")
            continue
        valid.append(case)
    if not found:
        notes.append("找不到可當網址參數的篩選或日期欄位，請手動填寫")
    return valid, notes


def render_cases(cases: list[CaseSpec]) -> str:
    """cases.yaml 的 cases 清單項目（兩格縮排，可直接接在 cases: 之後）。"""
    lines: list[str] = []
    for case in cases:
        lines.append(f"  - name: {case.name}")
        lines.append("    url_params:")
        for key, value in case.url_params.items():
            lines.append(
                f"      {json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"
            )
        if case.note:
            lines.append(f"    note: {json.dumps(case.note, ensure_ascii=False)}")
    return "\n".join(lines) + ("\n" if lines else "")


def append_cases(path: Path, cases: list[CaseSpec]) -> list[str]:
    """把建議接在 cases.yaml 的 cases 清單後面（保留註解）；回傳實際寫入的名稱。"""
    text = re.sub(r"(?m)^cases:\s*\[\]\s*$", "cases:", path.read_text(encoding="utf-8"))
    tail = text.split("\ncases:", 1)
    if len(tail) != 2 or re.search(r"(?m)^[A-Za-z_][\w]*\s*:", tail[1]):
        raise CasesError(f"{path.name} 的 cases 不是最後一個欄位，請手動加入建議")
    existing = {c.name for c in load_cases(path).cases}
    new = [c for c in cases if c.name not in existing]
    if new:
        path.write_text(
            text.rstrip("\n") + "\n" + render_cases(new), encoding="utf-8", newline="\n"
        )
        load_cases(path)  # 確認寫入後仍是合法的 cases.yaml
    return [c.name for c in new]
