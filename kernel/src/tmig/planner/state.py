"""篩選與參數狀態：查詢服務的請求、cases.yaml 的網址參數，都先轉成這個格式。

欄位鍵（field key）：

- ``f_region``：欄位本身
- ``year(f_order_date)``：日期部分（year、quarter、month、week、weekday、day、hour、minute、second）
- ``trunc_month(f_order_date)``：日期截斷
- ``sum(f_sales)``：彙總；Table Calculation 另加 ``#<tc id>``，例如 ``sum(f_sales)#tc_running``

查詢結果的欄名也用同一套欄位鍵。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tmig.ir.models import (
    IR,
    CategoricalFilter,
    DateDerivation,
    FieldUse,
    Filter,
    RangeFilter,
    Scalar,
    TopNFilter,
    TupleFilter,
    Worksheet,
)
from tmig.planner.identity import protected_fields

DATE_PARTS = ("year", "quarter", "month", "week", "weekday", "day", "hour", "minute", "second")
TRUNC_UNITS = ("year", "quarter", "month", "week", "day", "hour", "minute", "second")
_KEY = re.compile(r"^(?:(?P<fn>[a-z_]+)\()?(?P<field>[a-z][a-z0-9_]*)\)?$")


class StateError(ValueError):
    pass


class FilterValue(BaseModel):
    """一個欄位的篩選狀態：類別篩選給 mode 與 values，範圍篩選給 min、max。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["include", "exclude", "all"] | None = None
    values: list[Scalar] = Field(default_factory=list)
    min: Scalar = None
    max: Scalar = None
    include_null: bool | None = None

    @property
    def is_range(self) -> bool:
        return self.mode is None and (self.min is not None or self.max is not None)


class SetValue(BaseModel):
    """集合動作改變後的集合成員（固定成員的集合）：每個成員是集合各層級的值。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["include", "exclude"] = Field(
        default="include", description="include：列出的成員在集合中；exclude：列出的以外在集合中"
    )
    members: list[list[Scalar]] = Field(default_factory=list, max_length=10000)


class QueryState(BaseModel):
    """篩選與參數的目前狀態。API 只接受這個格式，不接受 SQL。"""

    model_config = ConfigDict(extra="forbid")

    filters: dict[str, FilterValue] = Field(default_factory=dict)
    parameters: dict[str, Scalar] = Field(default_factory=dict)
    actions: dict[str, list[dict[str, Scalar]]] = Field(
        default_factory=dict,
        description="篩選動作的選取：動作 id → 選取的標記（欄位鍵 → 值）；沒有列出或為空表示沒有選取",
    )
    sets: dict[str, SetValue] = Field(
        default_factory=dict,
        description="集合動作改變後的成員：集合 id → 成員；沒有列出的集合維持原本的成員",
    )
    today: str | None = Field(
        default=None,
        description="相對日期篩選的基準日（yyyy-mm-dd）；未指定時為查詢時的今天",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


def use_key(use: FieldUse) -> str:
    """FieldUse → 欄位鍵。"""
    key = use.field
    if use.date is not None:
        name = use.date.unit if use.date.kind == "part" else f"trunc_{use.date.unit}"
        key = f"{name}({key})"
    if use.agg is not None:
        key = f"{use.agg}({key})"
    if use.table_calc is not None:
        key = f"{key}#{use.table_calc}"
    return key


def parse_key(key: str) -> FieldUse:
    """篩選狀態的欄位鍵 → FieldUse（只接受維度：欄位、日期部分、日期截斷）。"""
    match = _KEY.match(key.strip())
    if match is None:
        raise StateError(f"無法辨識的欄位鍵「{key}」")
    fn, field = match.group("fn"), match.group("field")
    if fn is None:
        return FieldUse(field=field)
    if fn in DATE_PARTS:
        return FieldUse(field=field, date=DateDerivation(kind="part", unit=fn))  # type: ignore[arg-type]
    if fn.startswith("trunc_") and fn.removeprefix("trunc_") in TRUNC_UNITS:
        unit = fn.removeprefix("trunc_")
        return FieldUse(field=field, date=DateDerivation(kind="trunc", unit=unit), continuous=True)  # type: ignore[arg-type]
    raise StateError(f"欄位鍵「{key}」的函式 {fn} 不支援")


def _same_dimension(a: FieldUse, b: FieldUse) -> bool:
    return a.field == b.field and a.date == b.date and a.agg is None and b.agg is None


@dataclass
class EffectiveState:
    """套用到某張工作表後的結果。"""

    filters: list[Filter]
    parameters: dict[str, Scalar]
    applied: list[str]
    """實際套用的篩選欄位鍵。"""
    skipped: list[str]
    """不適用這張工作表的篩選欄位鍵（欄位不在工作表的資料源中）。"""
    today: str | None = None
    """相對日期篩選的基準日；None 表示查詢時的今天。"""


def apply_state(ir: IR, worksheet: Worksheet, state: QueryState) -> EffectiveState:
    """把狀態套到工作表：同一欄位的既有篩選改值，沒有的就新增維度篩選（與 Tableau 網址參數相同）。

    列層級權限的欄位（用到使用者函式、報表沒有為它放篩選控制項）不接受狀態，列在 skipped
    （見 tmig.planner.identity）。
    """
    datasource = next(ds for ds in ir.datasources if ds.id == worksheet.datasource)
    field_ids = {f.id for f in datasource.fields}
    protected = protected_fields(ir, datasource)
    params = {p.id: p.value for p in ir.parameters}
    for param_id, value in state.parameters.items():
        if param_id not in params:
            raise StateError(f"參數「{param_id}」不存在")
        params[param_id] = value

    filters: list[Filter] = [_with_selection(f, state) for f in worksheet.filters]
    applied: list[str] = []
    skipped: list[str] = []
    for n, (key, value) in enumerate(state.filters.items(), start=1):
        use = parse_key(key)
        if use.field not in field_ids or use.field in protected:
            skipped.append(key)
            continue
        applied.append(key)
        index = next(
            (
                i
                for i, flt in enumerate(filters)
                if not isinstance(flt, TopNFilter | TupleFilter) and _same_dimension(flt.field, use)
            ),
            None,
        )
        if index is not None:
            filters[index] = _replace(filters[index], value)
            continue
        filters.append(_new_filter(f"state_{n}", use, value))
    return EffectiveState(
        filters=filters, parameters=params, applied=applied, skipped=skipped, today=state.today
    )


def _with_selection(flt: Filter, state: QueryState) -> Filter:
    """篩選動作：有選取時，以選取的標記中目標工作表也有的欄位篩選；沒有選取時維持預設（清除時的狀態）。

    選取與其他篩選（含列層級權限）以 AND 套用，只會縮小範圍。"""
    if not isinstance(flt, TupleFilter) or flt.action is None:
        return flt
    marks = state.actions.get(flt.action) or []
    if not marks:
        return flt
    keys = [use_key(u) for u in flt.fields]
    shared = [k for k in keys if all(k in mark for mark in marks)]
    if not shared:
        return flt
    fields = [u for u, k in zip(flt.fields, keys, strict=True) if k in shared]
    tuples: list[list[Scalar]] = []
    for mark in marks:
        row = [mark[k] for k in shared]
        if row not in tuples:
            tuples.append(row)
    return flt.model_copy(update={"fields": fields, "mode": "include", "tuples": tuples})


def _replace(existing: Filter, value: FilterValue) -> Filter:
    assert not isinstance(existing, TupleFilter | TopNFilter)
    if value.is_range:
        if isinstance(existing, RangeFilter):
            return existing.model_copy(
                update={
                    "min": value.min,
                    "max": value.max,
                    "include_null": existing.include_null
                    if value.include_null is None
                    else value.include_null,
                }
            )
        return _new_filter(existing.id, existing.field, value, existing.stage)
    if isinstance(existing, CategoricalFilter):
        mode = value.mode or "include"
        return existing.model_copy(
            update={"mode": mode, "values": [] if mode == "all" else list(value.values)}
        )
    return _new_filter(existing.id, existing.field, value, existing.stage)


def _new_filter(
    filter_id: str, use: FieldUse, value: FilterValue, stage: str = "dimension"
) -> Filter:
    source = f"state:{use_key(use)}"
    if value.is_range:
        return RangeFilter(
            kind="range",
            id=filter_id,
            field=use,
            stage=stage,  # type: ignore[arg-type]
            min=value.min,
            max=value.max,
            include_null=bool(value.include_null),
            source_ref=source,
        )
    mode = value.mode or "include"
    return CategoricalFilter(
        kind="categorical",
        id=filter_id,
        field=use,
        stage=stage,  # type: ignore[arg-type]
        mode=mode,
        values=[] if mode == "all" else list(value.values),
        source_ref=source,
    )
