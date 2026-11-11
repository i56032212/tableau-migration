"""階層的切入（drill down／up）：檢視器上展開或收合階層，不改報表的定義。

- 資料源的階層（Tableau 的 drill-path）：由上到下的維度，例如 Category → Sub-Category → Product Name
- 日期的階層：年 → 季 → 月 → 日（日期時間再到時、分、秒）。與 Tableau 相同，日期部分（YEAR 得到 2025）
  向下切入時在後面加入下一層的日期部分；日期值（截斷到年、月……）改成下一層的截斷
- 欄、列架上一串相連的階層欄位（例如 Category 後面接著 Sub-Category）是一組，以這組最上層的欄位鍵記下
  切入的層數：正數向下多顯示幾層，負數收合存檔時原本顯示的下層
- 只加入階層中定義的欄位（同一個資料源）；與 Tableau 相同，檢視者可以切入作者定義的階層
"""

from __future__ import annotations

from dataclasses import dataclass

from tmig.ir.models import IR, DataField, Datasource, DateDerivation, FieldUse, Worksheet
from tmig.planner.state import use_key

DATE_PARTS = ("year", "quarter", "month", "day")
TIME_PARTS = ("hour", "minute", "second")


@dataclass(frozen=True)
class Chain:
    """架上的一組階層欄位：最上層的欄位、它在階層中的位置、存檔時顯示的下層數、最多可以顯示的下層數與
    切入後顯示的下層數。"""

    shelf: str
    head: FieldUse
    levels: list[FieldUse]
    """這個階層由上到下的各層（日期為同一個欄位的各個日期部分或截斷）。"""
    position: int
    saved: int
    deepest: int
    """最多可以向下顯示的層數：到階層的最下層為止，或停在架上已經另外有的下層之前（不重複加入同一個欄位）。"""
    shown: int

    @property
    def key(self) -> str:
        return use_key(self.head)

    @property
    def replaces(self) -> bool:
        """日期值（截斷）只有一個膠囊：切入時改成下一層的截斷，不另外加入膠囊。"""
        return self.head.date is not None and self.head.date.kind == "trunc"


def _levels(
    ds: Datasource, fields: dict[str, DataField], use: FieldUse
) -> tuple[list[FieldUse], int] | None:
    """use 所在的階層（由上到下的各層）與它是第幾層；不在任何階層中時為 None。"""
    if use.agg is not None or use.table_calc is not None:
        return None
    if use.date is not None:
        field = fields.get(use.field)
        units: list[str] = list(DATE_PARTS)
        if field is not None and field.datatype == "datetime":
            units += list(TIME_PARTS)
        if use.date.unit not in units:
            return None  # 週、星期等不在日期的階層中
        chain = [
            FieldUse(
                field=use.field,
                date=DateDerivation(kind=use.date.kind, unit=unit),  # type: ignore[arg-type]
                continuous=use.continuous,
            )
            for unit in units
        ]
        return chain, units.index(use.date.unit)
    for hierarchy in ds.hierarchies:
        if use.field in hierarchy.fields:
            chain = [FieldUse(field=f) for f in hierarchy.fields]
            return chain, hierarchy.fields.index(use.field)
    return None


def _same(a: FieldUse, b: FieldUse) -> bool:
    return use_key(a) == use_key(b)


def chains(ir: IR, worksheet: Worksheet, drill: dict[str, int] | None = None) -> list[Chain]:
    """存檔的工作表欄、列架上可以切入的階層（每組一個），與依 drill 切入後顯示的層數。"""
    ds = next((d for d in ir.datasources if d.id == worksheet.datasource), None)
    if ds is None:
        return []
    fields = {f.id: f for f in ds.fields}
    present = {use_key(u) for u in (*worksheet.rows, *worksheet.columns)}
    found: list[Chain] = []
    for shelf, uses in (("rows", worksheet.rows), ("columns", worksheet.columns)):
        i = 0
        while i < len(uses):
            use = uses[i]
            levels = _levels(ds, fields, use)
            if levels is None:
                i += 1
                continue
            chain, position = levels
            replaces = use.date is not None and use.date.kind == "trunc"
            saved = 0
            if not replaces:
                # 離散的膠囊：後面緊接著的下一層、再下一層……是同一組（存檔時已經顯示的下層）
                while (
                    i + 1 + saved < len(uses)
                    and position + 1 + saved < len(chain)
                    and _same(uses[i + 1 + saved], chain[position + 1 + saved])
                ):
                    saved += 1
            deepest = saved
            while position + 1 + deepest < len(chain) and (
                replaces or use_key(chain[position + 1 + deepest]) not in present
            ):
                deepest += 1
            delta = (drill or {}).get(use_key(use), 0)
            shown = max(0, min(deepest, delta if replaces else saved + delta))
            found.append(Chain(shelf, use, chain, position, saved, deepest, shown))
            i += 1 + saved
    return found


def apply_drill(ir: IR, worksheet_id: str, drill: dict[str, int]) -> IR:
    """以切入後的欄、列架取代這張工作表（其他工作表不變）；沒有切入時回傳原本的 IR。"""
    if not drill:
        return ir
    sheet = next((w for w in ir.worksheets if w.id == worksheet_id), None)
    if sheet is None:
        return ir
    found = chains(ir, sheet, drill)
    if all(c.shown == (0 if c.replaces else c.saved) for c in found):
        return ir
    shelves: dict[str, list[FieldUse]] = {"rows": [], "columns": []}
    by_head = {(c.shelf, id(c.head)): c for c in found}
    for shelf, uses in (("rows", sheet.rows), ("columns", sheet.columns)):
        out = shelves[shelf]
        i = 0
        while i < len(uses):
            use = uses[i]
            chain = by_head.get((shelf, id(use)))
            if chain is None:
                out.append(use)
                i += 1
                continue
            if chain.replaces:
                out.append(chain.levels[chain.position + chain.shown])
                i += 1
                continue
            out.append(use)
            for j in range(chain.shown):
                out.append(
                    uses[i + 1 + j] if j < chain.saved else chain.levels[chain.position + 1 + j]
                )
            i += 1 + chain.saved
    changed = sheet.model_copy(update={"rows": shelves["rows"], "columns": shelves["columns"]})
    return ir.model_copy(
        update={"worksheets": [changed if w.id == sheet.id else w for w in ir.worksheets]}
    )


def use_label(ir: IR, use: FieldUse) -> str:
    """膠囊上的名稱，例如 Category、YEAR(Order Date)。"""
    field = next((f for d in ir.datasources for f in d.fields if f.id == use.field), None)
    text = field.caption if field is not None else use.field
    return f"{use.date.unit.upper()}({text})" if use.date is not None else text


def drill_info(ir: IR, worksheet: Worksheet, drill: dict[str, int]) -> list[dict[str, object]]:
    """檢視器的切入控制項：每組階層的鍵、最上層與下一層的顯示名稱、可以向下或向上。"""
    result: list[dict[str, object]] = []
    for chain in chains(ir, worksheet, drill):
        if chain.deepest == 0:
            continue  # 已經是最下層（或下一層已經在架上）：不能切入
        index = chain.position + chain.shown
        result.append(
            {
                "key": chain.key,
                "shelf": chain.shelf,
                "label": use_label(ir, chain.levels[index]),
                "head": use_label(ir, chain.head),
                "next": use_label(ir, chain.levels[index + 1])
                if chain.shown < chain.deepest
                else None,
                # 實際切入的層數（要求的層數超出範圍時以實際顯示的為準）：檢視器以它加減一
                "delta": chain.shown - (0 if chain.replaces else chain.saved),
                "can_down": chain.shown < chain.deepest,
                "can_up": chain.shown > 0,
            }
        )
    return result


__all__ = ["Chain", "apply_drill", "chains", "drill_info", "use_label"]
