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
from tmig.ir import IR, Calc, DataField, Datasource, FieldUse, Marks, Parameter, Worksheet
from tmig.ir.integrity import check_references
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
    """編輯器的資料窗格：各資料源的欄位（維度、量值、計算欄位、集合）與參數；另附色盤（色彩對話框）。"""
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
    }


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
    updated = _replace_datasource(ir, ds.model_copy(update={"fields": fields}))
    if existing is not None and existing.calc is not None and existing.calc.level != check.level:
        # 與 Tableau 相同：改成彙總的公式後膠囊變成 AGG(…)，改回列層級後量值的膠囊加上 SUM
        updated = _adapt_worksheets(updated, field, existing.calc.level, check.level)
    return _validated(updated, f"「{caption}」的變更與使用它的工作表不相容"), field


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
        ir, ds.model_copy(update={"fields": [f for f in ds.fields if f.id != field_id]})
    )
    return _validated(updated, f"「{field.caption}」還在使用中，不能刪除")


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
    updated = ir.model_copy(
        update={"worksheets": [sheet if w.id == sheet.id else w for w in ir.worksheets]}
    )
    return _validated(updated, "工作表的參照不正確"), sheet


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
    updated = ir.model_copy(update={"worksheets": [w for w in ir.worksheets if w.id != worksheet]})
    return _validated(updated, f"「{sheet.name}」還在 dashboard、Story 或動作中使用，不能刪除")


def upsert_parameter(ir: IR, data: dict[str, Any]) -> tuple[IR, Parameter]:
    """新增或修改參數（名稱、型別、允許的值、目前值、別名）：名稱不能重複；修改時保留 id 與內部名稱。"""
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
    "add_worksheet",
    "catalog",
    "check_formula",
    "duplicate_worksheet",
    "new_id",
    "remove_calc",
    "remove_parameter",
    "remove_worksheet",
    "replace_worksheet",
    "upsert_calc",
    "upsert_parameter",
]
