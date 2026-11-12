"""編輯報表（桌面版編輯器）：在 IR 上新增、修改工作表與計算欄位，檢查後回傳新的 IR。

編輯器在畫面上操作工作表（欄列架、標記卡、篩選、排序、Table Calculation），每次變更把整張工作表送回；
這裡檢查工作表本身（pydantic）與跨實體的參照（tmig.ir.integrity），通過後換掉 IR 中的那一張。
計算欄位以公式引擎分析：型別、計算層級與語法樹；角色與 Tableau 相同（彙總、數字為量值，其他為維度）。

所有函式都不改動傳入的 IR，回傳新的 IR。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from tmig.formula.analyze import Analyzer
from tmig.formula.ast import FieldRef, to_json
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.types import FormulaSemanticError
from tmig.ir import (
    IR,
    Action,
    Blend,
    BlendLink,
    Calc,
    Dashboard,
    DataField,
    Datasource,
    FieldUse,
    Hierarchy,
    Marks,
    MemberAlias,
    Parameter,
    Size,
    Story,
    StoryPoint,
    TupleFilter,
    Worksheet,
    Zone,
    iter_zones,
)
from tmig.ir.integrity import check_references
from tmig.ir.models import GeoAlias, GeoRole, GeoSettings
from tmig.ir.scope import DatasourceScope
from tmig.render.palettes import PALETTES

EDITOR_REF = "/editor"
"""編輯器建立的實體的 source_ref（不是來自 .twb）。"""
NUMERIC = ("integer", "real")


class EditError(ValueError):
    """編輯的內容不正確（例如欄位不存在、公式有誤）。"""


@dataclass(frozen=True)
class FormulaCheck:
    """公式的分析結果。"""

    datatype: str
    level: Literal["row", "aggregate", "lod", "table_calc"]
    role: Literal["dimension", "measure"]
    calc: Calc


def catalog(ir: IR) -> dict[str, Any]:
    """編輯器的資料窗格：各資料源的欄位（維度、量值、計算欄位、集合）、階層與參數；另附色盤（色彩對話框）。"""
    return {
        "palettes": [
            {"name": name, "kind": palette.kind, "colors": list(palette.colors)}
            for name, palette in PALETTES.items()
        ],
        "datasources": [
            {
                "id": ds.id,
                "caption": ds.caption,
                "fields": [_field_entry(f) for f in ds.fields],
                "hierarchies": [h.model_dump(mode="json") for h in ds.hierarchies],
                "geo": ds.geo.model_dump(mode="json") if ds.geo is not None else None,
            }
            for ds in ir.datasources
        ],
        "parameters": [
            {
                "id": p.id,
                "caption": p.caption,
                "name": p.name,
                "datatype": p.datatype,
                "value": p.value,
                "allowed": p.allowed.model_dump(mode="json"),
                "aliases": [a.model_dump(mode="json") for a in p.aliases],
                "value_field": p.value_field,
                "domain_field": p.domain_field,
            }
            for p in ir.parameters
        ],
    }


def _field_entry(f: DataField) -> dict[str, Any]:
    kind = "set" if f.set is not None else "calc" if f.calc is not None else "column"
    return {
        "id": f.id,
        "name": f.name,
        "caption": f.caption,
        "role": f.role,
        "datatype": f.datatype,
        "kind": kind,
        "formula": f.calc.formula if f.calc is not None else None,
        "level": f.calc.level if f.calc is not None else None,
        "default_agg": f.default_agg,
        "hidden": f.hidden,
        "geo_role": f.geo_role,
        "geo_aliases": [a.model_dump(mode="json") for a in f.geo_aliases],
    }


GEO_ROLES: tuple[GeoRole, ...] = (
    "country",
    "state",
    "county",
    "city",
    "zip",
    "latitude",
    "longitude",
)
COORDINATES = ("latitude", "longitude")


def set_geography(
    ir: IR,
    datasource: str,
    field_id: str,
    role: GeoRole | None,
    aliases: list[GeoAlias] | None = None,
) -> IR:
    """資料窗格的「地理角色」（None 取消）與「編輯位置」中這個欄位的值指定的地名（aliases 為 None 時不變）。
    國家、州、郡、城市、郵遞區號是文字或整數的維度；緯度、經度是數字的欄位。"""
    ds = _datasource(ir, datasource)
    found = next((f for f in ds.fields if f.id == field_id), None)
    if found is None:
        raise EditError(f"找不到欄位 {field_id}")
    if role is not None:
        if role not in GEO_ROLES:
            raise EditError(f"不支援的地理角色 {role}")
        if role in COORDINATES and found.datatype not in ("integer", "real"):
            raise EditError(f"「{found.caption}」不是數字，不能當作緯度、經度")
        if role not in COORDINATES and found.datatype not in ("string", "integer"):
            raise EditError(f"「{found.caption}」不是文字或整數，不能當作地名")
    kept = list(found.geo_aliases) if aliases is None else list(aliases)
    seen: set[str] = set()
    for alias in kept:
        if not alias.value.strip() or not alias.location.strip():
            raise EditError("編輯位置的對應需要原始值與地名")
        if alias.value in seen:
            raise EditError(f"「{alias.value}」重複指定")
        seen.add(alias.value)
    updated = found.model_copy(
        update={
            "geo_role": role,
            "geo_aliases": kept if role is not None and role not in COORDINATES else [],
        }
    )
    fields = [updated if f.id == field_id else f for f in ds.fields]
    return _replace_datasource(ir, ds.model_copy(update={"fields": fields}))


def set_geo_defaults(ir: IR, datasource: str, country: str | None, state: str | None) -> IR:
    """「編輯位置」的預設國家與州（檢視中沒有國家、州的欄位時比對的範圍）。"""
    ds = _datasource(ir, datasource)
    country = (country or "").strip() or None
    state = (state or "").strip() or None
    geo = GeoSettings(country=country, state=state) if country or state else None
    return _replace_datasource(ir, ds.model_copy(update={"geo": geo}))


def check_formula(
    ir: IR, datasource: str, formula: str, field_id: str | None = None
) -> FormulaCheck:
    """分析公式（在資料源的欄位與參數之中）；field_id 為編輯中的計算欄位（不能參照自己）。"""
    ds = _datasource(ir, datasource)
    if not formula.strip():
        raise EditError("公式是空的")
    probe_id = field_id or "f_editor_probe"
    existing = next((f for f in ds.fields if f.id == field_id), None)
    probe = DataField(
        id=probe_id,
        name=existing.name if existing is not None else "[__editor_probe]",
        caption=existing.caption if existing is not None else "__editor_probe",
        role="dimension",
        datatype="string",
        calc=Calc(formula=formula),
        source_ref=EDITOR_REF,
    )
    fields = [f for f in ds.fields if f.id != probe_id] + [probe]
    scope = DatasourceScope(ds.name, fields, ir.parameters)
    analyzer = Analyzer(scope)
    target = scope.target(probe_id)
    try:
        info = analyzer.target_info(target)
        node = analyzer.calc_ast(target)
    except (FormulaSyntaxError, FormulaSemanticError) as exc:
        message = getattr(exc, "message", str(exc))
        raise EditError(f"公式有誤：{message}") from exc

    def resolve(ref: FieldRef) -> dict[str, str] | None:
        found = analyzer.resolve(ref)
        return {"target": found.kind, "id": found.id}

    level: Literal["row", "aggregate", "lod", "table_calc"] = (
        "table_calc"
        if info.level == "tc"
        else "aggregate"
        if info.level == "agg"
        else "lod"
        if info.has_lod
        else "row"
    )
    datatype = "real" if info.datatype == "null" else info.datatype
    role: Literal["dimension", "measure"] = (
        "measure" if level in ("aggregate", "table_calc") or datatype in NUMERIC else "dimension"
    )
    return FormulaCheck(
        datatype, level, role, Calc(formula=formula, ast=to_json(node, resolve), level=level)
    )


def upsert_calc(
    ir: IR, datasource: str, caption: str, formula: str, field_id: str | None = None
) -> tuple[IR, DataField]:
    """新增或修改計算欄位：名稱不能與其他欄位重複；修改時保留 id 與內部名稱（工作表的參照不變）。"""
    ds = _datasource(ir, datasource)
    caption = caption.strip()
    if not caption:
        raise EditError("計算欄位需要名稱")
    if any(f.caption == caption and f.id != field_id for f in ds.fields):
        raise EditError(f"已經有名為「{caption}」的欄位")
    existing = next((f for f in ds.fields if f.id == field_id), None) if field_id else None
    if field_id is not None and (existing is None or existing.calc is None):
        raise EditError(f"找不到計算欄位 {field_id}")
    check = check_formula(ir, datasource, formula, field_id)
    if existing is not None:
        field = existing.model_copy(
            update={"caption": caption, "calc": check.calc, "datatype": check.datatype}
        )
        if existing.role != check.role and check.level in ("aggregate", "table_calc"):
            field = field.model_copy(update={"role": "measure"})
    else:
        field = DataField(
            id=_unique_id(ir, "f_" + _slug(caption)),
            name=f"[Calculation_{_next_calc_number(ds)}]",
            caption=caption,
            role=check.role,
            datatype=check.datatype,  # type: ignore[arg-type]
            calc=check.calc,
            source_ref=EDITOR_REF,
        )
    fields = [field if f.id == field.id else f for f in ds.fields]
    if existing is None:
        fields.append(field)
    hierarchies = ds.hierarchies
    if field.role == "measure":
        hierarchies = _without_fields(hierarchies, {field.id})  # 階層只能有維度
    updated = _replace_datasource(
        ir, ds.model_copy(update={"fields": fields, "hierarchies": hierarchies})
    )
    if existing is not None and existing.calc is not None and existing.calc.level != check.level:
        # 與 Tableau 相同：改成彙總的公式後膠囊變成 AGG(…)，改回列層級後量值的膠囊加上 SUM
        updated = _adapt_worksheets(updated, field, existing.calc.level, check.level)
    return _validated(updated, f"「{caption}」的變更與使用它的工作表不相容"), field


CLUSTER_NAMES = 25
"""叢集欄位的別名：0 為 Not Clustered，1 到 25 為 Cluster N（自動決定的叢集數最多 25 個）。"""


def add_analytics_field(
    ir: IR, datasource: str, kind: Literal["cluster", "forecast_indicator"]
) -> tuple[IR, DataField]:
    """分析的欄位（查詢之後才填值，見 tmig.planner.analytics）：叢集每次新增一個（公式為 0 的整數維度，
    別名為 Cluster N，與 Tableau 拖到檢視上的叢集相同）；Forecast Indicator 一個資料源只有一個
    （公式為 "Actual" 的字串維度，估計的列為 Estimate），已經有時直接回傳。"""
    ds = _datasource(ir, datasource)
    if kind == "forecast_indicator":
        found = next((f for f in ds.fields if f.name == "[Forecast Indicator]"), None)
        if found is not None:
            return ir, found
        name, caption, formula, aliases = (
            "[Forecast Indicator]",
            "Forecast Indicator",
            '"Actual"',
            [],
        )
    else:
        n = 1
        while any(f.name == f"[AdhocCluster:{n}]" for f in ds.fields):
            n += 1
        captions = {f.caption for f in ds.fields}
        caption = "Clusters" if "Clusters" not in captions else f"Clusters {n}"
        while caption in captions:
            n += 1
            caption = f"Clusters {n}"
        name, formula = f"[AdhocCluster:{n}]", "0"
        aliases = [MemberAlias(value=0, label="Not Clustered")] + [
            MemberAlias(value=i, label=f"Cluster {i}") for i in range(1, CLUSTER_NAMES + 1)
        ]
    check = check_formula(ir, datasource, formula)
    field = DataField(
        id=_unique_id(ir, "f_" + _slug(caption)),
        name=name,
        caption=caption,
        role="dimension",
        datatype=check.datatype,  # type: ignore[arg-type]
        calc=check.calc,
        aliases=aliases,
        source_ref=EDITOR_REF,
    )
    updated = _replace_datasource(ir, ds.model_copy(update={"fields": [*ds.fields, field]}))
    return _validated(updated, f"無法加入「{caption}」"), field


USE_KEYS = frozenset({"field", "agg", "date", "continuous", "table_calc"})


def _adapt_worksheets(ir: IR, field: DataField, old: str | None, new: str) -> IR:
    """計算欄位的層級改變後，調整使用它的工作表：彙總與 Table Calculation 層級不再加彙總；
    變成 Table Calculation 時加上公式的 Table Calculation（計算方向為表格（橫向）），不再是時移除。"""
    owners = {d.id for d in ir.datasources if field in d.fields}
    taken = _all_ids(ir)
    sheets = [
        _adapt_sheet(ir, ws, field, old, new, taken) if ws.datasource in owners else ws
        for ws in ir.worksheets
    ]
    return ir.model_copy(update={"worksheets": sheets})


def _adapt_sheet(
    ir: IR, ws: Worksheet, field: DataField, old: str | None, new: str, taken: set[str]
) -> Worksheet:
    """一張工作表上這個欄位的膠囊（見 _adapt_worksheets）；taken：報表中已用的 id（新的 id 加入其中）。"""
    data = ws.model_dump(mode="json")
    formula_ids = {tc["id"] for tc in data["table_calcs"] if tc["type"] == "formula"}
    aggregate = new in ("aggregate", "table_calc")
    created: list[str] = []
    dropped: set[str] = set()

    def fix(use: dict[str, Any]) -> dict[str, Any]:
        if use.get("field") != field.id:
            return use
        use = dict(use)
        if aggregate:
            use["agg"] = None
            use["date"] = None
        elif old in ("aggregate", "table_calc") and use.get("agg") is None:
            if field.role == "measure":
                use["agg"] = "sum" if field.datatype in NUMERIC else "attr"
        if new == "table_calc" and use.get("table_calc") is None:
            if not created:
                base, n = f"tc_formula_{field.id}"[:56], 2
                found = base
                while found in taken:
                    found, n = f"{base}_{n}", n + 1
                taken.add(found)
                created.append(found)
            use["table_calc"] = created[0]
        if old == "table_calc" and new != "table_calc" and use.get("table_calc") in formula_ids:
            dropped.add(use["table_calc"])
            use["table_calc"] = None
        return use

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            if USE_KEYS.issuperset(value.keys()) and "field" in value:
                return fix(value)
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    data = walk(data)
    data["table_calcs"] = [tc for tc in data["table_calcs"] if tc["id"] not in dropped]
    if created:
        data["table_calcs"].append(
            {
                "id": created[0],
                "type": "formula",
                "preset": "table_across",
                "addressing": _across(ir, Worksheet.model_validate(data)),
                "inferred": True,
                "source_ref": EDITOR_REF,
            }
        )
    return Worksheet.model_validate(data)


def _across(ir: IR, ws: Worksheet) -> list[str]:
    """表格（橫向）的定址：欄架上的維度，再來是標記卡上的維度（與解析時相同）。"""
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    levels = {f.id: f.calc.level for f in ds.fields if f.calc is not None}

    def dim(use: FieldUse) -> bool:
        if use.field in ("measure_names", "measure_values"):
            return False
        return (
            use.agg is None
            and use.table_calc is None
            and levels.get(use.field) not in ("aggregate", "table_calc")
        )

    ids: list[str] = []
    for use in [*ws.columns, *ws.marks.uses()]:
        if dim(use) and use.field not in ids:
            ids.append(use.field)
    return ids


def remove_calc(ir: IR, datasource: str, field_id: str) -> IR:
    """刪除計算欄位：工作表或其他計算欄位還在使用時不能刪。"""
    ds = _datasource(ir, datasource)
    field = next((f for f in ds.fields if f.id == field_id), None)
    if field is None or field.calc is None:
        raise EditError(f"找不到計算欄位 {field_id}")
    updated = _replace_datasource(
        ir,
        ds.model_copy(
            update={
                "fields": [f for f in ds.fields if f.id != field_id],
                # 與 Tableau 相同，刪除的欄位從階層中移除（階層沒有欄位時一起刪除）
                "hierarchies": _without_fields(ds.hierarchies, {field_id}),
            }
        ),
    )
    return _validated(updated, f"「{field.caption}」還在使用中，不能刪除")


def upsert_hierarchy(
    ir: IR, datasource: str, name: str, fields: list[str], original: str | None = None
) -> tuple[IR, Hierarchy | None]:
    """建立階層，或改變階層（original 為原本的名稱）的名稱與由上到下的欄位。

    與 Tableau 相同：階層只能有維度，一個欄位只能在一個階層中（加入這個階層的欄位從其他階層移除）；
    沒有欄位時刪除階層（回傳 None）。工作表不受影響，檢視器依新的階層切入。"""
    ds = _datasource(ir, datasource)
    name = name.strip()
    if not name:
        raise EditError("請輸入階層的名稱")
    if original is not None and all(h.name != original for h in ds.hierarchies):
        raise EditError(f"找不到階層「{original}」")
    if any(h.name == name and h.name != original for h in ds.hierarchies):
        raise EditError(f"已經有名為「{name}」的階層")
    known = {f.id: f for f in ds.fields}
    ids: list[str] = []
    for field_id in fields:
        field = known.get(field_id)
        if field is None:
            raise EditError(f"找不到欄位 {field_id}")
        level = field.calc.level if field.calc is not None else None
        if (
            field.role != "dimension"
            or field.set is not None
            or level in ("aggregate", "table_calc")
        ):
            raise EditError(f"「{field.caption}」不是維度，不能加入階層")
        if field_id not in ids:
            ids.append(field_id)
    created = Hierarchy(name=name, fields=ids) if ids else None
    hierarchies: list[Hierarchy] = []
    for hierarchy in _without_fields(ds.hierarchies, set(ids), keep=original):
        if hierarchy.name == original:
            if created is not None:
                hierarchies.append(created)  # 原本的位置
        else:
            hierarchies.append(hierarchy)
    if original is None and created is not None:
        hierarchies.append(created)
    updated = _replace_datasource(ir, ds.model_copy(update={"hierarchies": hierarchies}))
    return _validated(updated, f"階層「{name}」不正確"), created


def remove_hierarchy(ir: IR, datasource: str, name: str) -> IR:
    """刪除階層：欄位留在資料源中，工作表不受影響。"""
    ds = _datasource(ir, datasource)
    if all(h.name != name for h in ds.hierarchies):
        raise EditError(f"找不到階層「{name}」")
    hierarchies = [h for h in ds.hierarchies if h.name != name]
    return _replace_datasource(ir, ds.model_copy(update={"hierarchies": hierarchies}))


def _without_fields(
    hierarchies: list[Hierarchy], removed: set[str], keep: str | None = None
) -> list[Hierarchy]:
    """從各階層移除這些欄位；沒有欄位的階層一起移除（keep 為名稱的階層保留，由呼叫者換掉）。"""
    result: list[Hierarchy] = []
    for hierarchy in hierarchies:
        if hierarchy.name == keep:
            result.append(hierarchy)
            continue
        rest = [f for f in hierarchy.fields if f not in removed]
        if rest:
            result.append(
                hierarchy
                if rest == hierarchy.fields
                else Hierarchy(name=hierarchy.name, fields=rest)
            )
    return result


def replace_worksheet(ir: IR, worksheet: dict[str, Any] | Worksheet) -> tuple[IR, Worksheet]:
    """換掉 IR 中同 id 的工作表（編輯器的每次變更）。"""
    try:
        sheet = (
            worksheet if isinstance(worksheet, Worksheet) else Worksheet.model_validate(worksheet)
        )
    except ValidationError as exc:
        raise EditError(f"工作表的內容不正確：{_first_error(exc)}") from exc
    if not any(w.id == sheet.id for w in ir.worksheets):
        raise EditError(f"找不到工作表 {sheet.id}")
    if any(w.name == sheet.name and w.id != sheet.id for w in ir.worksheets):
        raise EditError(f"已經有名為「{sheet.name}」的工作表")
    sheet = _sync_blends(ir, sheet)
    updated = ir.model_copy(
        update={"worksheets": [sheet if w.id == sheet.id else w for w in ir.worksheets]}
    )
    return _validated(updated, "工作表的參照不正確"), sheet


def _sync_blends(ir: IR, sheet: Worksheet) -> Worksheet:
    """Data Blending：欄列架與標記卡上有其他資料源的欄位時，加上次要資料源與連結欄位；不再用到的
    次要資料源移除。

    與 Tableau 相同，工作表還沒有用到主要資料源的任何欄位時，第一個放上的欄位的資料源成為主要資料源。
    連結沿用其他工作表混合同一對資料源時的連結（例如來自 .twb 的自訂關聯），否則以名稱相同的維度
    自動連結。次要資料源的欄位只能是彙總的量值。"""
    owner = {f.id: ds.id for ds in ir.datasources for f in ds.fields}
    view = [*sheet.rows, *sheet.columns, *sheet.marks.uses()]
    view += [u for pane in sheet.panes for u in pane.marks.uses()]
    view += list(sheet.measure_values)
    used = [u for u in view if u.field in owner]
    referenced = _field_refs(sheet.model_dump(exclude={"blends"}))
    primary = sheet.datasource
    if used and not any(owner.get(f) == primary for f in referenced):
        primary = owner[used[0].field]
    order: list[str] = []
    for use in used:
        ds = owner[use.field]
        if ds != primary and ds not in order:
            order.append(ds)
    existing = {b.datasource: b for b in sheet.blends}
    blends: list[Blend] = []
    for ds_id in order:
        secondary = _datasource(ir, ds_id)
        for use in used:
            if owner[use.field] == ds_id and not _secondary_measure(secondary, use):
                caption = next(f.caption for f in secondary.fields if f.id == use.field)
                raise EditError(
                    f"「{caption}」是次要資料源「{secondary.caption}」的欄位：Data Blending 只能使用"
                    "次要資料源彙總的量值"
                )
        found = existing.get(ds_id) if primary == sheet.datasource else None
        found = found or _known_blend(ir, sheet.id, primary, ds_id)
        if found is None:
            links = _auto_links(_datasource(ir, primary), secondary)
            if not links:
                raise EditError(
                    f"資料源「{_datasource(ir, primary).caption}」與「{secondary.caption}」沒有名稱相同的"
                    "維度可以連結（Data Blending）"
                )
            found = Blend(datasource=ds_id, links=links)
        blends.append(found)
    if primary == sheet.datasource and blends == sheet.blends:
        return sheet
    return sheet.model_copy(update={"datasource": primary, "blends": blends})


def _field_refs(node: Any) -> set[str]:
    """工作表中所有欄位參照（FieldUse 與排序的 field、Table Calculation 的定址）。"""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "field" and isinstance(value, str):
                found.add(value)
            elif key == "addressing" and isinstance(value, list):
                found |= {v for v in value if isinstance(v, str)}
            else:
                found |= _field_refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _field_refs(item)
    return found


def _secondary_measure(ds: Datasource, use: FieldUse) -> bool:
    field = next((f for f in ds.fields if f.id == use.field), None)
    if field is None or use.table_calc is not None:
        return False
    level = field.calc.level if field.calc is not None else None
    if level == "table_calc":
        return False
    return use.agg is not None or level == "aggregate"


def _known_blend(ir: IR, sheet: str, primary: str, secondary: str) -> Blend | None:
    """其他工作表混合同一對資料源時的連結（例如 .twb 的自訂關聯）。"""
    for ws in ir.worksheets:
        if ws.id == sheet or ws.datasource != primary:
            continue
        for blend in ws.blends:
            if blend.datasource == secondary:
                return blend
    return None


def _auto_links(primary: Datasource, secondary: Datasource) -> list[BlendLink]:
    """名稱相同的維度（與 Tableau 的自動連結相同）。"""

    def dimensions(ds: Datasource) -> dict[str, DataField]:
        return {
            f.caption: f
            for f in ds.fields
            if f.role == "dimension"
            and (f.calc is None or f.calc.level not in ("aggregate", "table_calc"))
        }

    others = dimensions(secondary)
    return [
        BlendLink(primary=FieldUse(field=f.id), secondary=FieldUse(field=others[caption].id))
        for caption, f in dimensions(primary).items()
        if caption in others
    ]


def add_worksheet(ir: IR, name: str | None, datasource: str | None) -> tuple[IR, Worksheet]:
    """新增空白的工作表（預設用第一個資料源）。"""
    if not ir.datasources:
        raise EditError("報表沒有資料源")
    ds = _datasource(ir, datasource) if datasource else ir.datasources[0]
    taken = {w.name for w in ir.worksheets}
    name = (name or "").strip() or _next_name(taken, "工作表")
    if name in taken:
        raise EditError(f"已經有名為「{name}」的工作表")
    sheet = Worksheet(
        id=_unique_id(ir, "ws_" + _slug(name)),
        name=name,
        datasource=ds.id,
        marks=Marks(type="automatic"),
        source_ref=EDITOR_REF,
    )
    updated = ir.model_copy(update={"worksheets": [*ir.worksheets, sheet]})
    return _validated(updated, "新增工作表失敗"), sheet


def duplicate_worksheet(ir: IR, worksheet: str) -> tuple[IR, Worksheet]:
    """複製工作表（名稱加上「(2)」等）。"""
    source = next((w for w in ir.worksheets if w.id == worksheet), None)
    if source is None:
        raise EditError(f"找不到工作表 {worksheet}")
    taken = {w.name for w in ir.worksheets}
    name = _next_name(taken, source.name, start=2, template="{base} ({n})")
    copy = source.model_copy(
        update={"id": _unique_id(ir, "ws_" + _slug(name)), "name": name}, deep=True
    )
    # Table Calculation 與篩選的 id 在整份報表中唯一：加上後綴
    copy = _reid(ir, copy)
    updated = ir.model_copy(update={"worksheets": [*ir.worksheets, copy]})
    return _validated(updated, "複製工作表失敗"), copy


def remove_worksheet(ir: IR, worksheet: str) -> IR:
    """刪除工作表：dashboard、Story 或動作還在使用時不能刪。"""
    sheet = next((w for w in ir.worksheets if w.id == worksheet), None)
    if sheet is None:
        raise EditError(f"找不到工作表 {worksheet}")
    remaining = [_without_tooltip(w, worksheet) for w in ir.worksheets if w.id != worksheet]
    updated = ir.model_copy(update={"worksheets": remaining})
    return _validated(updated, f"「{sheet.name}」還在 dashboard、Story 或動作中使用，不能刪除")


def _without_tooltip(sheet: Worksheet, removed: str) -> Worksheet:
    """工作表的工具提示中不再有刪除的工作表（與 Tableau 相同，工具提示的其他內容不變）。"""

    def strip(marks: Marks) -> Marks:
        kept = [t for t in marks.tooltip_sheets if t.worksheet != removed]
        return (
            marks
            if len(kept) == len(marks.tooltip_sheets)
            else marks.model_copy(update={"tooltip_sheets": kept})
        )

    marks = strip(sheet.marks)
    panes = [p.model_copy(update={"marks": strip(p.marks)}) for p in sheet.panes]
    if marks is sheet.marks and all(
        a.marks is b.marks for a, b in zip(panes, sheet.panes, strict=True)
    ):
        return sheet
    return sheet.model_copy(update={"marks": marks, "panes": panes})


def upsert_parameter(ir: IR, data: dict[str, Any]) -> tuple[IR, Parameter]:
    """新增或修改參數（名稱、型別、允許的值、目前值、別名、開啟報表時取值的欄位）：名稱不能重複；
    修改時保留 id 與內部名稱。"""
    parameter_id = data.get("id")
    existing = next((p for p in ir.parameters if p.id == parameter_id), None)
    if parameter_id is not None and existing is None:
        raise EditError(f"找不到參數 {parameter_id}")
    caption = str(data.get("caption") or "").strip()
    if not caption:
        raise EditError("參數需要名稱")
    if any(p.caption == caption and p.id != parameter_id for p in ir.parameters):
        raise EditError(f"已經有名為「{caption}」的參數")
    numbers = [
        int(m.group(1))
        for p in ir.parameters
        if (m := re.fullmatch(r"\[Parameter (\d+)\]", p.name)) is not None
    ]
    body = {
        "id": existing.id if existing else _unique_id(ir, "p_" + _slug(caption)),
        "name": existing.name if existing else f"[Parameter {max(numbers, default=0) + 1}]",
        "caption": caption,
        "datatype": data.get("datatype"),
        "allowed": data.get("allowed") or {"kind": "all"},
        "value": data.get("value"),
        "aliases": data.get("aliases") or [],
        # 動態參數：開啟報表時的目前值、允許值取自欄位
        "value_field": data.get("value_field"),
        "domain_field": data.get("domain_field"),
        "source_ref": existing.source_ref if existing else EDITOR_REF,
    }
    try:
        parameter = Parameter.model_validate(body)
    except ValidationError as exc:
        raise EditError(f"參數的內容不正確：{_first_error(exc)}") from exc
    allowed = parameter.allowed
    if allowed.kind == "list" and parameter.value not in allowed.values:  # type: ignore[union-attr]
        raise EditError("目前值必須是允許的值之一")
    if allowed.kind == "range" and isinstance(parameter.value, (int, float)):
        low, high = allowed.min, allowed.max  # type: ignore[union-attr]
        if (isinstance(low, (int, float)) and parameter.value < low) or (
            isinstance(high, (int, float)) and parameter.value > high
        ):
            raise EditError("目前值不在允許的範圍內")
    parameters = [parameter if p.id == parameter.id else p for p in ir.parameters]
    if existing is None:
        parameters.append(parameter)
    updated = ir.model_copy(update={"parameters": parameters})
    return _validated(updated, "參數的變更與報表不相容"), parameter


def remove_parameter(ir: IR, parameter_id: str) -> IR:
    """刪除參數：計算欄位、篩選、參考線、控制項或動作還在使用時不能刪。"""
    parameter = next((p for p in ir.parameters if p.id == parameter_id), None)
    if parameter is None:
        raise EditError(f"找不到參數 {parameter_id}")
    for ds in ir.datasources:
        for f in ds.fields:
            if f.calc is not None and _references_parameter(f.calc.ast, parameter_id):
                raise EditError(
                    f"「{parameter.caption}」還在計算欄位「{f.caption}」中使用，不能刪除"
                )
    text = parameter.name
    for ws in ir.worksheets:
        dumped = ws.model_dump_json()
        if text in dumped or f'"parameter":"{parameter_id}"' in dumped:
            raise EditError(f"「{parameter.caption}」還在工作表「{ws.name}」中使用，不能刪除")
    updated = ir.model_copy(
        update={"parameters": [p for p in ir.parameters if p.id != parameter_id]}
    )
    return _validated(updated, f"「{parameter.caption}」還在使用中，不能刪除")


def _references_parameter(node: Any, parameter_id: str) -> bool:
    if isinstance(node, dict):
        if node.get("target") == "param" and node.get("id") == parameter_id:
            return True
        return any(_references_parameter(v, parameter_id) for v in node.values())
    if isinstance(node, list):
        return any(_references_parameter(v, parameter_id) for v in node)
    return False


# ---------------------------------------------------------------- dashboard、Story 與動作


def outline(ir: IR) -> dict[str, Any]:
    """dashboard 與 Story 編輯器的概要：工作表（資料源、篩選與欄列架上的維度）、dashboard、Story、參數、
    動作與報表中用到的圖片。"""
    images = sorted(
        {
            path
            for db in ir.dashboards
            for zone in iter_zones(db.zones)
            for path in (
                zone.image,
                zone.button.image if zone.button else None,
                zone.button.image_hidden if zone.button else None,
            )
            if path
        }
    )
    return {
        "worksheets": [
            {
                "id": ws.id,
                "name": ws.name,
                "datasource": ws.datasource,
                "filters": [
                    {
                        "id": f.id,
                        "kind": f.kind,
                        "field": f.field.model_dump(mode="json"),
                        "label": _use_label(ir, ws, f.field),
                    }
                    for f in ws.filters
                    if not isinstance(f, TupleFilter)
                ],
                "dimensions": [u.model_dump(mode="json") for u in _view_dims(ir, ws)],
                "highlighters": [
                    {"field": u.model_dump(mode="json"), "label": _use_label(ir, ws, u)}
                    for u in _view_dims(ir, ws)
                ],
                "pages": _use_label(ir, ws, ws.pages) if ws.pages is not None else None,
                "legends": [
                    channel
                    for channel, use in (
                        ("color", ws.marks.color),
                        ("size", ws.marks.size),
                        ("shape", ws.marks.shape),
                    )
                    if use is not None
                ],
            }
            for ws in ir.worksheets
        ],
        "dashboards": [{"id": db.id, "name": db.name} for db in ir.dashboards],
        "stories": [{"id": st.id, "name": st.name} for st in ir.stories],
        "parameters": [
            {"id": p.id, "caption": p.caption, "datatype": p.datatype, "allowed": p.allowed.kind}
            for p in ir.parameters
        ],
        "actions": [a.model_dump(mode="json") for a in ir.actions],
        "images": images,
    }


def _use_label(ir: IR, ws: Worksheet, use: FieldUse) -> str:
    """欄位使用的顯示名稱（與膠囊相同，例如 YEAR(Order Date)、SUM(Sales)）。"""
    # 欄位 id 全報表唯一：Data Blending 的次要資料源的欄位也找得到
    field = next((f for d in ir.datasources for f in d.fields if f.id == use.field), None)
    text = field.caption if field is not None else use.field
    if use.date is not None:
        text = f"{use.date.unit.upper()}({text})"
    if use.agg is not None:
        text = f"{use.agg.upper()}({text})"
    return text


def _view_dims(ir: IR, ws: Worksheet) -> list[FieldUse]:
    """工作表檢視中的維度（欄列架、標記卡），依序、不重複：篩選動作預設以它們篩選目標。"""
    ds = next((d for d in ir.datasources if d.id == ws.datasource), None)
    levels = {f.id: f.calc.level for f in ds.fields if f.calc is not None} if ds else {}
    found: list[FieldUse] = []
    for use in [*ws.rows, *ws.columns, *ws.marks.uses()]:
        if use.field in ("measure_names", "measure_values") or use.agg is not None:
            continue
        if use.table_calc is not None or levels.get(use.field) in ("aggregate", "table_calc"):
            continue
        if use not in found:
            found.append(use)
    return found


def replace_dashboard(ir: IR, dashboard: dict[str, Any] | Dashboard) -> tuple[IR, Dashboard]:
    """換掉 IR 中同 id 的 dashboard（編輯器的每次變更）。"""
    try:
        board = (
            dashboard if isinstance(dashboard, Dashboard) else Dashboard.model_validate(dashboard)
        )
    except ValidationError as exc:
        raise EditError(f"dashboard 的內容不正確：{_first_error(exc)}") from exc
    if not any(d.id == board.id for d in ir.dashboards):
        raise EditError(f"找不到 dashboard {board.id}")
    if any(d.name == board.name and d.id != board.id for d in ir.dashboards):
        raise EditError(f"已經有名為「{board.name}」的 dashboard")
    updated = ir.model_copy(
        update={"dashboards": [board if d.id == board.id else d for d in ir.dashboards]}
    )
    return _validated(updated, "dashboard 的參照不正確"), board


def add_dashboard(
    ir: IR, name: str | None, width: int = 1000, height: int = 800
) -> tuple[IR, Dashboard]:
    """新增空白的 dashboard：最外層的容器中有一個垂直排列的容器（加入的工作表依序放在其中）。"""
    taken = {d.name for d in ir.dashboards} | {w.name for w in ir.worksheets}
    name = (name or "").strip() or _next_name(taken, "儀表板")
    if name in taken:
        raise EditError(f"已經有名為「{name}」的 dashboard 或工作表")
    flow = Zone(
        id="z2",
        kind="container",
        x=0,
        y=0,
        w=width,
        h=height,
        layout="vertical",
        source_ref=EDITOR_REF,
    )
    root = Zone(
        id="z1",
        kind="container",
        x=0,
        y=0,
        w=width,
        h=height,
        layout="tiled",
        children=[flow],
        source_ref=EDITOR_REF,
    )
    board = Dashboard(
        id=_unique_id(ir, "db_" + _slug(name)),
        name=name,
        size=Size(width=width, height=height),
        zones=[root],
        source_ref=EDITOR_REF,
    )
    updated = ir.model_copy(update={"dashboards": [*ir.dashboards, board]})
    return _validated(updated, "新增 dashboard 失敗"), board


def duplicate_dashboard(ir: IR, dashboard: str) -> tuple[IR, Dashboard]:
    """複製 dashboard（區塊 id 只需在 dashboard 中唯一，原樣保留）；只在這個 dashboard 上的動作不複製。"""
    source = next((d for d in ir.dashboards if d.id == dashboard), None)
    if source is None:
        raise EditError(f"找不到 dashboard {dashboard}")
    taken = {d.name for d in ir.dashboards} | {w.name for w in ir.worksheets}
    name = _next_name(taken, source.name, start=2, template="{base} ({n})")
    copy = source.model_copy(
        update={"id": _unique_id(ir, "db_" + _slug(name)), "name": name}, deep=True
    )
    updated = ir.model_copy(update={"dashboards": [*ir.dashboards, copy]})
    return _validated(updated, "複製 dashboard 失敗"), copy


def remove_dashboard(ir: IR, dashboard: str) -> IR:
    """刪除 dashboard：只在它上面執行的動作一併刪除；Story、按鈕或切換動作還在使用時不能刪。"""
    board = next((d for d in ir.dashboards if d.id == dashboard), None)
    if board is None:
        raise EditError(f"找不到 dashboard {dashboard}")
    removed = {a.id for a in ir.actions if a.dashboard == dashboard}
    updated = _without_actions(ir, removed).model_copy(
        update={"dashboards": [d for d in ir.dashboards if d.id != dashboard]}
    )
    for story in updated.stories:
        if any(point.dashboard == dashboard for point in story.points):
            raise EditError(f"「{board.name}」還在 Story「{story.name}」中使用，不能刪除")
    return _validated(updated, f"「{board.name}」還在按鈕或動作中使用，不能刪除")


def replace_story(ir: IR, story: dict[str, Any] | Story) -> tuple[IR, Story]:
    try:
        found = story if isinstance(story, Story) else Story.model_validate(story)
    except ValidationError as exc:
        raise EditError(f"Story 的內容不正確：{_first_error(exc)}") from exc
    if not any(s.id == found.id for s in ir.stories):
        raise EditError(f"找不到 Story {found.id}")
    updated = ir.model_copy(
        update={"stories": [found if s.id == found.id else s for s in ir.stories]}
    )
    return _validated(updated, "Story 的參照不正確"), found


def add_story(ir: IR, name: str | None) -> tuple[IR, Story]:
    """新增 Story：第一個故事點顯示第一個 dashboard（沒有時為第一張工作表）。"""
    taken = {s.name for s in ir.stories} | {d.name for d in ir.dashboards}
    taken |= {w.name for w in ir.worksheets}
    name = (name or "").strip() or _next_name(taken, "故事")
    if name in taken:
        raise EditError(f"已經有名為「{name}」的 Story、dashboard 或工作表")
    if ir.dashboards:
        first = StoryPoint(id="sp1", dashboard=ir.dashboards[0].id)
        size = ir.dashboards[0].size
    elif ir.worksheets:
        first = StoryPoint(id="sp1", worksheet=ir.worksheets[0].id)
        size = Size(width=1000, height=800)
    else:
        raise EditError("報表沒有 dashboard 或工作表")
    story = Story(
        id=_unique_id(ir, "story_" + _slug(name)),
        name=name,
        title=name,
        size=Size(width=size.width, height=size.height + 120),
        points=[first],
        source_ref=EDITOR_REF,
    )
    updated = ir.model_copy(update={"stories": [*ir.stories, story]})
    return _validated(updated, "新增 Story 失敗"), story


def remove_story(ir: IR, story: str) -> IR:
    found = next((s for s in ir.stories if s.id == story), None)
    if found is None:
        raise EditError(f"找不到 Story {story}")
    updated = ir.model_copy(update={"stories": [s for s in ir.stories if s.id != story]})
    return _validated(updated, f"刪除「{found.name}」失敗")


def upsert_action(ir: IR, data: dict[str, Any]) -> tuple[IR, Action]:
    """新增或修改動作。篩選動作在每個目標工作表上有一個指向它的多欄位篩選（TupleFilter）：
    以 fields（欄位鍵，空的表示來源工作表檢視中的所有維度）篩選，清除選取時依 clearing；
    不再是目標的工作表上的篩選移除。"""
    action_id = data.get("id")
    existing = next((a for a in ir.actions if a.id == action_id), None)
    if action_id is not None and existing is None:
        raise EditError(f"找不到動作 {action_id}")
    caption = str(data.get("caption") or "").strip()
    if not caption:
        raise EditError("動作需要名稱")
    fields = [str(k) for k in data.get("fields") or []]
    body = {k: v for k, v in data.items() if k != "fields"}
    new_id_value = existing.id if existing else _unique_id(ir, "act_" + _slug(caption))
    body.update(
        id=new_id_value,
        name=existing.name if existing else f"[Action_{new_id_value}]",
        caption=caption,
        source_ref=existing.source_ref if existing else EDITOR_REF,
    )
    try:
        action = Action.model_validate(body)
    except ValidationError as exc:
        raise EditError(f"動作的內容不正確：{_first_error(exc)}") from exc
    actions = [action if a.id == action.id else a for a in ir.actions]
    if existing is None:
        actions.append(action)
    updated = ir.model_copy(update={"actions": actions})
    updated = _sync_action_filters(updated, action, fields)
    return _validated(updated, "動作的參照不正確"), action


def _sync_action_filters(ir: IR, action: Action, keys: list[str]) -> IR:
    """篩選動作的目標工作表上指向它的篩選：加上缺少的、移除不再是目標的。"""
    from tmig.planner.state import use_key  # 避免循環匯入

    targets = set(action.targets) if action.kind == "filter" else set()
    mode: Literal["include", "exclude", "all"] = (
        "include" if action.clearing == "exclude_all" else "all"
    )
    sources = [w for w in ir.worksheets if w.id in action.sources]
    candidates: list[FieldUse] = []
    for source in sources:
        for use in _view_dims(ir, source):
            if use not in candidates:
                candidates.append(use)
    taken = _all_ids(ir)
    sheets: list[Worksheet] = []
    for ws in ir.worksheets:
        filters = list(ws.filters)
        mine = [f for f in filters if isinstance(f, TupleFilter) and f.action == action.id]
        if ws.id not in targets:
            filters = [f for f in filters if f not in mine]
        else:
            ds = next(d for d in ir.datasources if d.id == ws.datasource)
            ids = {f.id for f in ds.fields}
            chosen = [u for u in candidates if u.field in ids and (not keys or use_key(u) in keys)]
            if not chosen:
                raise EditError(f"目標工作表「{ws.name}」與來源工作表沒有共同的欄位")
            if mine:
                filters = [
                    f.model_copy(update={"fields": chosen, "mode": mode, "tuples": []})
                    if f in mine
                    else f
                    for f in filters
                ]
            else:
                base = f"flt_action_{_slug(action.caption)}"[:56]
                found, n = base, 2
                while found in taken:
                    found, n = f"{base}_{n}", n + 1
                taken.add(found)
                filters.append(
                    TupleFilter(
                        kind="tuple",
                        id=found,
                        fields=chosen,
                        stage="dimension",
                        mode=mode,
                        tuples=[],
                        action=action.id,
                        source_ref=EDITOR_REF,
                    )
                )
        sheets.append(ws.model_copy(update={"filters": filters}) if filters != ws.filters else ws)
    return ir.model_copy(update={"worksheets": sheets})


def remove_action(ir: IR, action: str) -> IR:
    """刪除動作與目標工作表上指向它的篩選、故事點記下的選取。"""
    if not any(a.id == action for a in ir.actions):
        raise EditError(f"找不到動作 {action}")
    return _validated(_without_actions(ir, {action}), "刪除動作失敗")


def _without_actions(ir: IR, removed: set[str]) -> IR:
    if not removed:
        return ir
    sheets = [
        ws.model_copy(
            update={
                "filters": [
                    f
                    for f in ws.filters
                    if not (isinstance(f, TupleFilter) and f.action in removed)
                ]
            }
        )
        for ws in ir.worksheets
    ]
    stories = [
        story.model_copy(
            update={
                "points": [
                    point.model_copy(
                        update={"actions": [a for a in point.actions if a.action not in removed]}
                    )
                    for point in story.points
                ]
            }
        )
        for story in ir.stories
    ]
    return ir.model_copy(
        update={
            "actions": [a for a in ir.actions if a.id not in removed],
            "worksheets": sheets,
            "stories": stories,
        }
    )


def new_id(ir: IR, prefix: str, text: str) -> str:
    """編輯器新增的實體（篩選、Table Calculation 等）的 id：prefix_文字，與報表中其他 id 不重複。"""
    return _unique_id(ir, f"{prefix}_{_slug(text)}")


# ---------------------------------------------------------------- 工具


def _datasource(ir: IR, datasource: str) -> Datasource:
    ds = next((d for d in ir.datasources if d.id == datasource), None)
    if ds is None:
        raise EditError(f"找不到資料源 {datasource}")
    return ds


def _replace_datasource(ir: IR, ds: Datasource) -> IR:
    return ir.model_copy(
        update={"datasources": [ds if d.id == ds.id else d for d in ir.datasources]}
    )


def _validated(ir: IR, message: str) -> IR:
    """重新驗證整份 IR（欄位、參照）；不通過時列出第一個問題。"""
    errors = check_references(ir)
    if errors:
        raise EditError(f"{message}：{errors[0]}")
    try:
        return IR.model_validate(ir.model_dump(mode="json"))
    except ValidationError as exc:
        raise EditError(f"{message}：{_first_error(exc)}") from exc


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    return f"{where}：{first.get('msg', '')}" if where else str(first.get("msg", ""))


def _all_ids(ir: IR) -> set[str]:
    ids: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "id" and isinstance(item, str):
                    ids.add(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(ir.model_dump(mode="json"))
    return ids


def _unique_id(ir: IR, base: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", base.lower())[:56].strip("_") or "x"
    if not base[0].isalpha():
        base = "x_" + base
    taken = _all_ids(ir)
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _slug(text: str) -> str:
    """id 用的文字：英數字保留，其他（含中文）換成底線；全部不是英數字時用 x。"""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "x"


def _next_name(taken: set[str], base: str, start: int = 1, template: str = "{base} {n}") -> str:
    n = start
    while template.format(base=base, n=n) in taken:
        n += 1
    return template.format(base=base, n=n)


def _next_calc_number(ds: Datasource) -> int:
    numbers = [
        int(m.group(1))
        for f in ds.fields
        if (m := re.fullmatch(r"\[Calculation_(\d+)\]", f.name)) is not None
    ]
    return max(numbers, default=0) + 1


def _reid(ir: IR, sheet: Worksheet) -> Worksheet:
    """複製的工作表：篩選、Table Calculation 換成新的 id（欄位實例上的參照一併換掉）。"""
    taken = _all_ids(ir)
    mapping: dict[str, str] = {}

    def fresh(old: str) -> str:
        n = 2
        while f"{old}_{n}" in taken or f"{old}_{n}" in mapping.values():
            n += 1
        mapping[old] = f"{old}_{n}"
        return mapping[old]

    for item in [*sheet.filters, *sheet.table_calcs]:
        fresh(item.id)
    data = sheet.model_dump(mode="json")

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (
                    mapping.get(v, v)
                    if k in ("id", "table_calc") and isinstance(v, str)
                    else walk(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    data = walk(data)
    data["id"] = sheet.id
    return Worksheet.model_validate(data)


__all__ = [
    "EditError",
    "FormulaCheck",
    "add_dashboard",
    "add_story",
    "add_worksheet",
    "catalog",
    "check_formula",
    "duplicate_dashboard",
    "duplicate_worksheet",
    "new_id",
    "outline",
    "remove_action",
    "remove_calc",
    "remove_dashboard",
    "remove_hierarchy",
    "remove_parameter",
    "remove_story",
    "remove_worksheet",
    "replace_dashboard",
    "replace_story",
    "replace_worksheet",
    "upsert_action",
    "upsert_calc",
    "upsert_hierarchy",
    "upsert_parameter",
]
