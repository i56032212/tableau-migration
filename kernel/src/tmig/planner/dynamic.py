"""動態參數（Tableau 2020.1 起）：開啟報表時，參數的目前值取自欄位的值（「開啟活頁簿時的值」），
允許值的清單或範圍取自欄位的值（「開啟活頁簿時」重新整理）。

- 以資料源篩選、萃取篩選與列層級權限後的資料計算（與工作表的篩選無關），與一張只把欄位放在列上的工作表相同
- 目前值：欄位只有一個值時取代存檔時的值；多個值或沒有值時維持存檔時的值（Tableau 這時無法選擇預設值）
- 允許值：清單為不重複的值（由小到大）；範圍為最小值到最大值（間距不變）；沒有值時維持存檔時的允許值
- 查詢失敗（例如 Live 連線不通）時維持存檔時的值，記在 notes
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from tmig.ir.models import (
    IR,
    AllowedList,
    AllowedRange,
    Datasource,
    FieldUse,
    Marks,
    Parameter,
    Scalar,
    Worksheet,
)
from tmig.planner.identity import Identity
from tmig.planner.state import QueryState, use_key

HOLDER = "dynamic_parameter"

Runner = Callable[[Any, Datasource], Any]
"""執行查詢：(WorksheetSql, 資料源) → QueryResult（有 columns 與 rows）。"""


def dynamic_fields(ir: IR) -> list[str]:
    """動態參數用到的欄位 id（同一個欄位只查一次）。"""
    found: list[str] = []
    for param in ir.parameters:
        for field_id in (param.value_field, param.domain_field):
            if field_id is not None and field_id not in found:
                found.append(field_id)
    return found


def _datasource_of(ir: IR, field_id: str) -> Datasource | None:
    return next((d for d in ir.datasources if any(f.id == field_id for f in d.fields)), None)


def field_values(ir: IR, field_id: str, run: Runner, user: Identity | None = None) -> list[Any]:
    """欄位的不重複值：一張只把這個欄位放在列上的工作表（資料源篩選、列層級權限照常套用）。"""
    from tmig.sqlgen.query import worksheet_sql

    datasource = _datasource_of(ir, field_id)
    if datasource is None:
        raise ValueError(f"找不到欄位「{field_id}」")
    use = FieldUse(field=field_id)
    holder = Worksheet(
        id=HOLDER,
        name="動態參數",
        datasource=datasource.id,
        rows=[use],
        marks=Marks(type="automatic"),
        source_ref="dynamic-parameter",
    )
    sub = ir.model_copy(update={"worksheets": [holder]})
    query = worksheet_sql(sub, HOLDER, QueryState(), user)
    result = run(query, datasource)
    key = use_key(use)
    index = list(result.columns).index(key)
    return [row[index] for row in result.rows]


def _scalar(value: Any, datatype: str) -> Scalar:
    """查詢結果的值 → 參數的值（與 .twb 的常值相同的表示：日期為 ISO 字串）。"""
    if value is None:
        return None
    if datatype == "date":
        if isinstance(value, dt.datetime):
            return value.date().isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        return str(value)[:10]
    if datatype == "datetime":
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=None).isoformat(timespec="seconds")
        if isinstance(value, dt.date):
            return f"{value.isoformat()}T00:00:00"
        return str(value)
    if datatype == "integer":
        number = float(value) if isinstance(value, Decimal) else value
        if isinstance(number, float) and number.is_integer():
            return int(number)
        return number if isinstance(number, int | float) else str(number)
    if datatype == "real":
        return float(value) if isinstance(value, int | float | Decimal) else value
    if datatype == "boolean":
        return bool(value)
    return str(value)


def _sort_key(value: Scalar) -> tuple[int, Any]:
    return (0, value) if not isinstance(value, str) else (1, value)


def parameter_values(
    ir: IR, field_id: str, datatype: str | None, run: Runner, user: Identity | None = None
) -> list[Scalar]:
    """欄位目前的不重複值（由小到大、不含 Null），轉成參數的型別；datatype 省略時依欄位的型別。"""
    if datatype is None:
        field = next((f for d in ir.datasources for f in d.fields if f.id == field_id), None)
        datatype = field.datatype if field is not None else "string"
    found: list[Scalar] = []
    for raw in field_values(ir, field_id, run, user):
        value = _scalar(raw, datatype)
        if value is not None and value not in found:
            found.append(value)
    found.sort(key=_sort_key)
    return found


def resolve_parameters(ir: IR, run: Runner, user: Identity | None = None) -> tuple[IR, list[str]]:
    """以資料的值設定動態參數；沒有動態參數時回傳原本的 IR。"""
    fields = dynamic_fields(ir)
    if not fields:
        return ir, []
    notes: list[str] = []
    values: dict[str, list[Any]] = {}
    captions = {f.id: f.caption for d in ir.datasources for f in d.fields}
    for field_id in fields:
        try:
            values[field_id] = field_values(ir, field_id, run, user)
        except Exception as exc:  # 查詢失敗：維持存檔時的值
            notes.append(
                f"動態參數用到的欄位「{captions.get(field_id, field_id)}」查詢失敗，維持存檔時的值：{exc}"
            )
    parameters: list[Parameter] = []
    for param in ir.parameters:
        update: dict[str, Any] = {}
        if param.value_field in values:
            found: list[Scalar] = []
            for raw in values[param.value_field]:
                value = _scalar(raw, param.datatype)
                if value is not None and value not in found:
                    found.append(value)
            if len(found) == 1:
                update["value"] = found[0]
            else:
                notes.append(
                    f"動態參數「{param.caption}」的目前值欄位有 {len(found)} 個值，維持存檔時的值"
                )
        if param.domain_field in values:
            members: list[Scalar] = []
            for raw in values[param.domain_field]:
                value = _scalar(raw, param.datatype)
                if value is not None and value not in members:
                    members.append(value)
            members.sort(key=_sort_key)
            if not members:
                notes.append(f"動態參數「{param.caption}」的允許值欄位沒有值，維持存檔時的允許值")
            elif isinstance(param.allowed, AllowedList):
                update["allowed"] = AllowedList(kind="list", values=members)
            elif isinstance(param.allowed, AllowedRange):
                update["allowed"] = param.allowed.model_copy(
                    update={"min": members[0], "max": members[-1]}
                )
        parameters.append(param.model_copy(update=update) if update else param)
    return ir.model_copy(update={"parameters": parameters}), notes


__all__ = ["dynamic_fields", "field_values", "parameter_values", "resolve_parameters"]
