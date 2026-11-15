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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tmig.ir.models import (
    IR,
    CategoricalFilter,
    DateDerivation,
    FieldUse,
    Filter,
    MapViewport,
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


class TooltipMark(BaseModel):
    """工具提示中的工作表（Viz in Tooltip）：來源工作表與滑過的標記。"""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, description="來源工作表 id（工具提示中放了這張工作表）")
    mark: dict[str, Scalar] = Field(description="滑過的標記：來源工作表的欄位鍵 → 值")


class MarkSelection(BaseModel):
    """工具提示的命令按鈕：只保留（keep）或排除（exclude）選取的標記，等同以標記的維度值篩選這張工作表。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["keep", "exclude"]
    marks: list[dict[str, Scalar]] = Field(
        min_length=1, max_length=10000, description="選取的標記：這張工作表的欄位鍵 → 值"
    )


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
    pages: dict[str, Scalar] = Field(
        default_factory=dict,
        description="頁面架目前的頁：工作表 id → 頁面欄位的值；沒有列出的工作表為存檔時的頁",
    )
    drill: dict[str, int] = Field(
        default_factory=dict,
        description="階層的切入（見 tmig.planner.drill）：欄、列架上一組階層欄位最上層的欄位鍵 → "
        "向下多顯示的層數；負數收合存檔時原本顯示的下層",
    )
    tooltip: TooltipMark | None = Field(
        default=None,
        description="在另一張工作表的工具提示中顯示（Viz in Tooltip）：以滑過的標記篩選這張工作表",
    )
    viewport: MapViewport | None = Field(
        default=None,
        description="地圖平移、縮放後的顯示範圍（檢視器的地圖工具列、滾輪與拖曳）：只影響地圖的繪製"
        "（底圖的細節與地名依新的縮放層級），不影響查詢",
    )
    marks: list[MarkSelection] = Field(
        default_factory=list,
        max_length=50,
        description="工具提示的「只保留」「排除」：依序套用在這張工作表上（各自是一個維度篩選）",
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
    if state.tooltip is not None:
        # 工具提示中的工作表：以滑過的標記篩選，取代 Tableau 自動產生的工具提示篩選
        filters = [f for f in filters if not (isinstance(f, TupleFilter) and f.tooltip)]
        tip = _tooltip_filter(ir, worksheet, state.tooltip)
        if tip is not None:
            filters.append(tip)
    if worksheet.pages is not None:
        # 頁面架：只顯示目前這一頁（等同這個值的維度篩選）；不知道是哪一頁時不篩選
        page = state.pages.get(worksheet.id, worksheet.page)
        if worksheet.id in state.pages or worksheet.page is not None:
            # 顯示歷程：之前的幾頁也查詢（with_page_history 填入），畫成淡化的標記或軌跡
            history = worksheet.page_history.previous if worksheet.page_history is not None else []
            shown = [*(v for v in history if v != page), page]
            filters.append(
                _new_filter("page", worksheet.pages, FilterValue(mode="include", values=shown))
            )
    for n, selection in enumerate(state.marks, start=1):
        filters.extend(_mark_filters(n, selection, field_ids - protected))
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
                if not isinstance(flt, TupleFilter)
                and _same_dimension(flt.field, use)
                # Top N 篩選的控制項：類別的選取改變 Top N 篩選排名前的選取
                and not (isinstance(flt, TopNFilter) and value.is_range)
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


def _tooltip_filter(ir: IR, worksheet: Worksheet, tooltip: TooltipMark) -> TupleFilter | None:
    """滑過的標記 → 這張工作表的篩選：工具提示設定的欄位（沒有設定時為標記的所有維度）中、這張工作表的
    資料源也有的欄位（不同資料源依欄位名稱對應，與 Tableau 相同）；一個都沒有時不篩選。"""
    source = next((w for w in ir.worksheets if w.id == tooltip.source), None)
    cards = [source.marks, *(p.marks for p in source.panes)] if source is not None else []
    entry = next((t for m in cards for t in m.tooltip_sheets if t.worksheet == worksheet.id), None)
    if source is None or entry is None:
        raise StateError(f"工作表「{worksheet.name}」不在「{tooltip.source}」的工具提示中")
    origin = next(d for d in ir.datasources if d.id == source.datasource)
    target = next(d for d in ir.datasources if d.id == worksheet.datasource)
    known = {f.id: f for f in origin.fields}
    ids = {f.id for f in target.fields}
    by_caption = {f.caption: f.id for f in target.fields}
    keys = [use_key(u) for u in entry.fields] if entry.fields is not None else list(tooltip.mark)
    uses: list[FieldUse] = []
    row: list[Scalar] = []
    for key in keys:
        if key not in tooltip.mark:
            continue
        try:
            use = parse_key(key)
        except StateError:
            continue  # 量值等不是維度的欄位鍵
        field = known.get(use.field)
        if field is None:
            continue  # Measure Names 等
        mapped = use.field if origin.id == target.id else by_caption.get(field.caption)
        if mapped is None or mapped not in ids:
            continue
        uses.append(use.model_copy(update={"field": mapped}))
        row.append(tooltip.mark[key])
    if not uses:
        return None
    return TupleFilter(
        kind="tuple",
        id="tooltip",
        fields=uses,
        stage="dimension",
        mode="include",
        tuples=[row],
        source_ref="state:tooltip",
    )


def _mark_filters(n: int, selection: MarkSelection, fields: set[str]) -> list[TupleFilter]:
    """只保留／排除選取的標記：以每個標記都有的維度（這張工作表的資料源中的欄位）的值組合篩選；
    量值等不是維度的欄位鍵略過，一個維度都沒有時不篩選。

    選取的標記沒有共同的維度時（地圖的選取工具同時選到不同標記圖層的標記，例如州與城市），依各自的維度
    分組、每組一個篩選：都要符合（只保留是各組的交集，排除是各組都排除）。"""

    def dimension(key: str) -> FieldUse | None:
        try:
            use = parse_key(key)
        except StateError:
            return None
        return use if use.field in fields else None

    marks = selection.marks
    shared = [
        key for key in marks[0] if all(key in mark for mark in marks) and dimension(key) is not None
    ]
    groups: dict[tuple[str, ...], list[dict[str, Scalar]]] = {}
    if shared:
        groups[tuple(shared)] = list(marks)
    else:
        for mark in marks:
            keys = tuple(sorted(key for key in mark if dimension(key) is not None))
            if keys:
                groups.setdefault(keys, []).append(mark)
    filters: list[TupleFilter] = []
    for g, (keys, members) in enumerate(groups.items(), start=1):
        tuples: list[list[Scalar]] = []
        for mark in members:
            row = [mark[key] for key in keys]
            if row not in tuples:
                tuples.append(row)
        uses = [use for use in map(dimension, keys) if use is not None]
        filters.append(
            TupleFilter(
                kind="tuple",
                id=f"marks_{n}" if g == 1 else f"marks_{n}_{g}",
                fields=uses,
                stage="dimension",
                mode="include" if selection.mode == "keep" else "exclude",
                tuples=tuples,
                source_ref="state:marks",
            )
        )
    return filters


def _with_selection(flt: Filter, state: QueryState) -> Filter:
    """篩選動作：有選取時，以選取的標記中目標工作表也有的欄位篩選；沒有選取時維持預設（清除時的狀態）。

    選取與其他篩選（含列層級權限）以 AND 套用，只會縮小範圍。"""
    if not isinstance(flt, TupleFilter) or flt.action is None:
        return flt
    marks = state.actions.get(flt.action) or []
    if not marks:
        return flt
    # 選取的標記以來源工作表的欄位為鍵；跨資料源的動作以對應的欄位取值
    sources = flt.source_fields or [u.field for u in flt.fields]
    keys = [
        use_key(u.model_copy(update={"field": s})) for u, s in zip(flt.fields, sources, strict=True)
    ]
    shared = [i for i, k in enumerate(keys) if all(k in mark for mark in marks)]
    if not shared:
        return flt
    fields = [flt.fields[i] for i in shared]
    tuples: list[list[Scalar]] = []
    for mark in marks:
        row = [mark[keys[i]] for i in shared]
        if row not in tuples:
            tuples.append(row)
    return flt.model_copy(
        update={"fields": fields, "mode": "include", "tuples": tuples, "source_fields": None}
    )


def _replace(existing: Filter, value: FilterValue) -> Filter:
    assert not isinstance(existing, TupleFilter)
    if isinstance(existing, TopNFilter):
        # 排名前先選取的成員（篩選對話框的「一般」頁）；Top N 本身不變
        mode = value.mode or "include"
        return existing.model_copy(
            update={"mode": mode, "values": [] if mode == "all" else list(value.values)}
        )
    if value.is_range:
        if isinstance(existing, RangeFilter):
            return existing.model_copy(
                update={
                    "min": value.min,
                    "max": value.max,
                    "include_null": existing.include_null
                    if value.include_null is None
                    else value.include_null,
                    # 檢視者改了範圍：不再是「只保留 Null 值」
                    "only_null": existing.only_null and value.min is None and value.max is None,
                }
            )
        return _new_filter(existing.id, existing.field, value, existing.stage)
    if isinstance(existing, CategoricalFilter):
        mode = value.mode or "include"
        return existing.model_copy(
            update={
                "mode": mode,
                "values": [] if mode == "all" else list(value.values),
                "ranges": [],
            }
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


def with_page_history(ir: IR, worksheet: str, state: QueryState, pages: list[Scalar]) -> IR:
    """頁面的顯示歷程：pages 是頁面欄位依序的所有值。目前這一頁之前的幾頁記在 page_history.previous
    （查詢時一併取回），頁面欄位加到詳細資料（每一頁各自的標記），目前的頁記在 page。
    沒有設定顯示歷程、目前是第一頁或找不到目前的頁時原樣回傳。"""
    ws = next((w for w in ir.worksheets if w.id == worksheet), None)
    if ws is None or ws.pages is None or ws.page_history is None:
        return ir
    page = state.pages.get(worksheet, ws.page)
    at = next((i for i, value in enumerate(pages) if value == page), -1)
    if at <= 0:
        return ir
    length = ws.page_history.length
    previous = pages[max(0, at - length) if length else 0 : at]
    update: dict[str, Any] = {
        "page": page,
        "page_history": ws.page_history.model_copy(update={"previous": previous}),
    }
    key = use_key(ws.pages)
    view = [*ws.rows, *ws.columns, *ws.marks.uses()]
    for pane in ws.panes:
        view += pane.marks.uses()
    if not any(use_key(u) == key for u in view):
        update["marks"] = ws.marks.model_copy(update={"detail": [*ws.marks.detail, ws.pages]})
    changed = ws.model_copy(update=update)
    return ir.model_copy(
        update={"worksheets": [changed if w.id == worksheet else w for w in ir.worksheets]}
    )


def with_viewport(ir: IR, worksheet: str, viewport: MapViewport | None) -> IR:
    """地圖工作表改以 viewport 為顯示範圍（檢視器平移、縮放之後重新繪製）；None 時原樣。"""
    if viewport is None:
        return ir
    from tmig.ir.models import MapOptions

    sheets = [
        ws.model_copy(
            update={"map": (ws.map or MapOptions()).model_copy(update={"viewport": viewport})}
        )
        if ws.id == worksheet
        else ws
        for ws in ir.worksheets
    ]
    return ir.model_copy(update={"worksheets": sheets})
