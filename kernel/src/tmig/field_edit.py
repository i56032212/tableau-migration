"""資料窗格的欄位選單（Tableau Desktop 在欄位上按右鍵的項目）：複製、別名、群組、數值分組、集合、
預設的數字格式與註解、刪除。

- 複製（duplicate_field）：與 Tableau 相同建立「<名稱> (copy)」：一般欄位成為公式為 [欄位] 的計算欄位，
  計算欄位、群組、數值分組與集合複製定義；角色、預設彙總、格式、別名、地理角色與註解照樣帶著
- 別名（set_aliases）：維度成員的顯示名稱（篩選、排序與計算仍用原始值）
- 群組（upsert_group）：成員對應到群組的名稱，其餘成員歸入「其他」（有指定時）或維持原值；與解析器的
  categorical-bin 相同以公式計算，定義記在 DataField.group 供「編輯群組」使用
- 數值分組（upsert_bins）：值為所在區間的下限，大小可以是參數（DataField.bin）
- 集合（upsert_set）：固定成員（包含或排除）、條件（彙總的布林公式）或 Top／Bottom N（DataField.set）
- 預設屬性（set_field_properties）：數字或日期的格式字串、註解、日期欄位的會計年度起始月
- 日期屬性（set_date_properties）：資料源的週起始日與會計年度起始月
- 合併欄位（create_combined）：兩個以上的維度組合成一個維度（Tableau 的「建立＞合併欄位」），值為各欄位的
  值以「, 」連接，名稱為「A & B (Combined)」
- 資料夾（create_folder、move_to_folder、rename_folder、remove_folder）：資料窗格中欄位的分組，資料夾依
  名稱排序；刪除資料夾時欄位回到最上層
- 刪除（remove_field）：計算欄位、群組、數值分組與集合；工作表、其他欄位、篩選或動作用到時不能刪

所有函式都不改動傳入的 IR，回傳新的 IR（與 tmig.edit 相同）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from tmig.edit import (
    EDITOR_REF,
    EditError,
    _datasource,
    _first_error,
    _next_calc_number,
    _replace_datasource,
    _slug,
    _unique_id,
    _validated,
    _without_fields,
    check_formula,
    check_unused,
    prune_ad_hoc,
    upsert_calc,
)
from tmig.field_defaults import repush_field_defaults
from tmig.ir import (
    IR,
    Calc,
    ColorPalette,
    DataField,
    Datasource,
    FieldBin,
    FieldFolder,
    FieldGroup,
    FieldSet,
    FieldSort,
    FieldUse,
    GroupItem,
    MemberAlias,
    ParamRef,
    SetCombined,
    SetCondition,
    SetMembers,
    SetTop,
    ShapeAssignment,
)
from tmig.ir.models import Scalar
from tmig.ir.setformula import (
    FormulaValueError,
    bin_expression,
    formula_literal,
    group_expression,
    members_condition,
    members_formula,
    set_levels,
)

IN_OUT = [MemberAlias(value=True, label="In"), MemberAlias(value=False, label="Out")]
"""集合的值在 Tableau 上顯示為 In／Out。"""
GROUPABLE = ("string", "integer", "real", "boolean", "date", "datetime")
"""可以建立群組、集合與別名的維度型別。"""
SetRule = SetMembers | SetCondition | SetTop | SetCombined


def _field(ds: Datasource, field_id: str) -> DataField:
    found = next((f for f in ds.fields if f.id == field_id), None)
    if found is None:
        raise EditError(f"找不到欄位 {field_id}")
    return found


def _caption(ds: Datasource, caption: str, default: str, field_id: str | None) -> str:
    """欄位的名稱：沒有輸入時用預設的名稱（重複時加上序號）；與其他欄位重複時退回。"""
    caption = caption.strip()
    taken = {f.caption.casefold() for f in ds.fields if f.id != field_id}
    if caption:
        if caption.casefold() in taken:
            raise EditError(f"已經有名為「{caption}」的欄位")
        return caption
    if default.casefold() not in taken:
        return default
    n = 2
    while f"{default} {n}".casefold() in taken:
        n += 1
    return f"{default} {n}"


def _new_name(ds: Datasource) -> str:
    return f"[Calculation_{_next_calc_number(ds)}]"


def _is_dimension(f: DataField) -> bool:
    level = f.calc.level if f.calc is not None else None
    return f.role == "dimension" and level not in ("aggregate", "table_calc")


def _check_value(field: DataField, value: Scalar) -> None:
    """成員的值符合欄位的型別（Null 一律可以）。"""
    if value is None:
        return
    datatype = field.datatype
    ok = (
        (datatype == "string" and isinstance(value, str))
        or (datatype == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (
            datatype == "real"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
        or (datatype == "boolean" and isinstance(value, bool))
        or (datatype in ("date", "datetime") and isinstance(value, str))
    )
    if not ok:
        raise EditError(f"「{value}」不是「{field.caption}」的值（{datatype}）")
    if datatype in ("date", "datetime"):
        try:
            formula_literal(value, datatype)
        except FormulaValueError as exc:
            raise EditError(str(exc)) from exc


def _put(ir: IR, ds: Datasource, field: DataField, message: str) -> IR:
    """新增（id 不在資料源中）或換掉欄位，重新驗證。"""
    fields = [field if f.id == field.id else f for f in ds.fields]
    if all(f.id != field.id for f in ds.fields):
        fields.append(field)
    return _validated(_replace_datasource(ir, ds.model_copy(update={"fields": fields})), message)


def _existing(ds: Datasource, field_id: str | None, attr: str, what: str) -> DataField | None:
    if field_id is None:
        return None
    found = _field(ds, field_id)
    if getattr(found, attr) is None:
        raise EditError(f"「{found.caption}」不是{what}")
    return found


# ---------------------------------------------------------------- 複製


def duplicate_field(ir: IR, datasource: str, field_id: str) -> tuple[IR, DataField]:
    """複製欄位（Tableau 的「複製」）：「<名稱> (copy)」，一般欄位成為公式為 [欄位] 的計算欄位。"""
    ds = _datasource(ir, datasource)
    source = _field(ds, field_id)
    caption = _caption(ds, "", f"{source.caption} (copy)", None)
    calc = source.calc
    if source.column is not None:
        calc = check_formula(ir, datasource, source.name).calc
    copy = source.model_copy(
        update={
            "id": _unique_id(ir, "f_" + _slug(caption)),
            "name": _new_name(ds),
            "caption": caption,
            "column": None,
            "table": None,
            "physical_table": None,
            "calc": calc,
            "hidden": False,
            "source_ref": EDITOR_REF,
        }
    )
    return _put(ir, ds, copy, f"無法複製「{source.caption}」"), copy


# ---------------------------------------------------------------- 別名


def set_aliases(ir: IR, datasource: str, field_id: str, aliases: Sequence[MemberAlias]) -> IR:
    """維度成員的別名（Tableau 的「別名…」）：只改變顯示，空白的別名表示不設定。"""
    ds = _datasource(ir, datasource)
    field = _field(ds, field_id)
    if not _is_dimension(field) or field.datatype not in ("string", "integer", "real", "boolean"):
        raise EditError(f"「{field.caption}」不是離散的維度，不能設定別名")
    kept: list[MemberAlias] = []
    seen: set[tuple[str, Scalar]] = set()
    for alias in aliases:
        if not alias.label.strip():
            continue
        _check_value(field, alias.value)
        key = (type(alias.value).__name__, alias.value)
        if key in seen:
            raise EditError(f"「{alias.value}」重複指定別名")
        seen.add(key)
        kept.append(MemberAlias(value=alias.value, label=alias.label))
    updated = field.model_copy(update={"aliases": kept})
    return _put(ir, ds, updated, f"「{field.caption}」的別名不正確")


# ---------------------------------------------------------------- 群組


def upsert_group(
    ir: IR,
    datasource: str,
    source_id: str,
    caption: str,
    groups: Sequence[tuple[str, Sequence[Scalar]]],
    other: str | None = None,
    field_id: str | None = None,
) -> tuple[IR, DataField]:
    """建立或修改群組（Tableau 的「建立＞群組」）：groups 為（群組名稱、成員），other 為「其他」的名稱
    （None 時其餘成員維持原值）。一個成員只能在一個群組中。"""
    ds = _datasource(ir, datasource)
    source = _field(ds, source_id)
    existing = _existing(ds, field_id, "group", "群組")
    if source.id == field_id:
        raise EditError("群組的來源不能是它自己")
    if source.set is not None or not _is_dimension(source) or source.datatype not in GROUPABLE:
        raise EditError(f"「{source.caption}」不是維度，不能建立群組")
    caption = _caption(ds, caption, f"{source.caption} (group)", field_id)
    items: list[GroupItem] = []
    names: set[str] = set()
    placed: dict[tuple[str, Scalar], str] = {}
    for name, members in groups:
        name = name.strip()
        if not name:
            raise EditError("群組需要名稱")
        if name.casefold() in names:
            raise EditError(f"群組「{name}」重複")
        names.add(name.casefold())
        if not members:
            raise EditError(f"群組「{name}」沒有成員")
        for member in members:
            _check_value(source, member)
            key = (type(member).__name__, member)
            if key in placed:
                raise EditError(f"「{member}」已經在群組「{placed[key]}」中")
            placed[key] = name
        items.append(GroupItem(name=name, members=list(members)))
    if other is not None:
        other = other.strip()
        if not other:
            raise EditError("「其他」需要名稱")
        if other.casefold() in names:
            raise EditError(f"「其他」的名稱「{other}」與群組重複")
    try:
        formula = group_expression(
            source.name, source.datatype, [(i.name, i.members) for i in items], other, "string"
        )
    except FormulaValueError as exc:
        raise EditError(str(exc)) from exc
    check = check_formula(ir, datasource, formula, field_id)
    definition = FieldGroup(source=source.id, groups=items, other=other)
    if existing is not None:
        field = existing.model_copy(
            update={"caption": caption, "calc": check.calc, "group": definition}
        )
    else:
        field = DataField(
            id=_unique_id(ir, "f_" + _slug(caption)),
            name=_new_name(ds),
            caption=caption,
            role="dimension",
            datatype="string",
            calc=check.calc,
            group=definition,
            source_ref=EDITOR_REF,
        )
    return _put(ir, ds, field, f"群組「{caption}」不正確"), field


# ---------------------------------------------------------------- 使用者篩選

USER_FUNCTIONS = {"user": "ISUSERNAME", "group": "ISMEMBEROF"}
"""使用者篩選中對象的種類 → 判斷登入者的函式（使用者名稱、群組）。"""


def create_user_filter(
    ir: IR,
    datasource: str,
    source_id: str,
    caption: str,
    entries: Sequence[tuple[str, str, Sequence[Scalar]]],
) -> tuple[IR, DataField]:
    """建立使用者篩選（Tableau 的「伺服器＞建立使用者篩選」）：entries 為（種類、名稱、看得到的成員），種類是
    user（使用者名稱）或 group（群組）。產生布林的計算欄位，登入者看得到的成員為 TRUE——與 .twb 的使用者篩選
    解析後的公式相同的形狀（tmig.parser.groups.identity_set_formula）：

        ISMEMBEROF("All Users") AND ((ISUSERNAME("amy") AND …成員…) OR (ISMEMBEROF("業務") AND …成員…))

    開頭的 ISMEMBEROF("All Users") 對每個登入者都成立，讓這個欄位一定屬於列層級權限。放到篩選架上選 True
    即依登入者篩選；之後要修改時編輯這個計算欄位的公式。"""
    ds = _datasource(ir, datasource)
    source = _field(ds, source_id)
    if source.set is not None or not _is_dimension(source) or source.datatype not in GROUPABLE:
        raise EditError(f"「{source.caption}」不是維度，不能建立使用者篩選")
    caption = _caption(ds, caption, f"{source.caption} 的使用者篩選", None)
    seen: set[tuple[str, str]] = set()
    branches: list[str] = []
    for kind, name, members in entries:
        name = name.strip()
        if kind not in USER_FUNCTIONS:
            raise EditError(f"不認得的對象種類 {kind}（要是 user 或 group）")
        if not name:
            raise EditError("使用者或群組需要名稱")
        key = (kind, name.casefold())
        if key in seen:
            raise EditError(f"「{name}」重複")
        seen.add(key)
        if not members:
            continue  # 沒有勾選成員：這個對象什麼都看不到，不必寫進公式
        for member in members:
            _check_value(source, member)
        try:
            who = f"{USER_FUNCTIONS[kind]}({formula_literal(name)})"
            visible = members_condition(list(members), source.name, source.datatype)
        except FormulaValueError as exc:
            raise EditError(str(exc)) from exc
        branches.append(f"(({who}) AND {visible})")
    if not branches:
        raise EditError("使用者篩選至少要有一個使用者或群組看得到成員")
    condition = branches[0] if len(branches) == 1 else "(" + " OR ".join(branches) + ")"
    formula = f'ISMEMBEROF("All Users") AND ({condition})'
    check = check_formula(ir, datasource, formula, None)
    field = DataField(
        id=_unique_id(ir, "f_" + _slug(caption)),
        name=_new_name(ds),
        caption=caption,
        role="dimension",
        datatype="boolean",
        calc=check.calc,
        source_ref=EDITOR_REF,
    )
    return _put(ir, ds, field, f"使用者篩選「{caption}」不正確"), field


# ---------------------------------------------------------------- 數值分組


def upsert_bins(
    ir: IR,
    datasource: str,
    source_id: str,
    caption: str,
    size: float | ParamRef,
    field_id: str | None = None,
) -> tuple[IR, DataField]:
    """建立或修改數值分組（Tableau 的「建立＞數值分組」）：值為所在區間的下限，大小可以是參數。"""
    ds = _datasource(ir, datasource)
    source = _field(ds, source_id)
    existing = _existing(ds, field_id, "bin", "數值分組")
    level = source.calc.level if source.calc is not None else None
    if (
        source.datatype not in ("integer", "real")
        or source.set is not None
        or level in ("aggregate", "table_calc")
    ):
        raise EditError(f"「{source.caption}」不是列層級的數字欄位，不能建立數值分組")
    caption = _caption(ds, caption, f"{source.caption} (bin)", field_id)
    if isinstance(size, ParamRef):
        parameter = next((p for p in ir.parameters if p.id == size.parameter), None)
        if parameter is None:
            raise EditError(f"找不到參數 {size.parameter}")
        if parameter.datatype not in ("integer", "real"):
            raise EditError(f"參數「{parameter.caption}」不是數字")
        text = f"[Parameters].{parameter.name}"
    else:
        if not math.isfinite(size) or size <= 0:
            raise EditError("分組的大小必須大於 0")
        text = formula_literal(int(size) if float(size).is_integer() else float(size))
    peg = existing.bin.peg if existing is not None and existing.bin is not None else 0.0
    check = check_formula(ir, datasource, bin_expression(source.name, text, peg), field_id)
    definition = FieldBin(source=source.id, size=size, peg=peg)
    datatype = "real" if check.datatype == "real" else "integer"
    if existing is not None:
        field = existing.model_copy(
            update={
                "caption": caption,
                "calc": check.calc,
                "bin": definition,
                "datatype": datatype,
            }
        )
    else:
        field = DataField(
            id=_unique_id(ir, "f_" + _slug(caption)),
            name=_new_name(ds),
            caption=caption,
            role="dimension",
            datatype=datatype,
            calc=check.calc,
            bin=definition,
            source_ref=EDITOR_REF,
        )
    return _put(ir, ds, field, f"數值分組「{caption}」不正確"), field


# ---------------------------------------------------------------- 集合


def upsert_set(
    ir: IR,
    datasource: str,
    levels: Sequence[str],
    caption: str,
    rule: SetRule,
    field_id: str | None = None,
) -> tuple[IR, DataField]:
    """建立或修改集合（Tableau 的「建立＞集合」）：固定成員（levels 可以有多個維度，成員為各層級的值
    組合）、條件（彙總的布林公式）或 Top／Bottom N（依彙總排名，只能一個層級）。"""
    ds = _datasource(ir, datasource)
    existing = _existing(ds, field_id, "set", "集合")
    sources: list[DataField] = []
    if isinstance(rule, SetCombined):
        # 合併集合：維度與來源的集合相同（兩個集合要在同一個維度上）
        sources = [_field(ds, rule.left), _field(ds, rule.right)]
        for source in sources:
            if source.set is None or source.id == field_id:
                raise EditError(f"「{source.caption}」不是其他的集合，不能合併")
        left_set, right_set = sources[0].set, sources[1].set
        assert left_set is not None and right_set is not None
        if left_set.levels != right_set.levels:
            raise EditError("兩個集合必須在同一個維度上才能合併")
        if rule.left == rule.right:
            raise EditError("請選擇兩個不同的集合")
        levels = [use.field for use in left_set.levels]
    if not levels:
        raise EditError("集合至少要一個維度")
    fields = [_field(ds, level) for level in levels]
    for level in fields:
        if (
            level.set is not None
            or not _is_dimension(level)
            or level.datatype not in GROUPABLE
            or level.id == field_id
        ):
            raise EditError(f"「{level.caption}」不是維度，不能建立集合")
    if len(set(levels)) != len(levels):
        raise EditError("集合的維度重複")
    caption = _caption(ds, caption, f"{fields[0].caption} Set", field_id)
    field_set = FieldSet(levels=[FieldUse(field=level) for level in levels], rule=rule)
    calc: Calc | None = None
    if isinstance(rule, SetMembers):
        for row in rule.members:
            if len(row) != len(fields):
                raise EditError("成員的值與集合的維度數量不同")
            for level, value in zip(fields, row, strict=True):
                _check_value(level, value)
        try:
            formula = members_formula(set_levels(ds.fields, field_set), rule.members, rule.mode)
        except FormulaValueError as exc:
            raise EditError(str(exc)) from exc
        calc = check_formula(ir, datasource, formula, field_id).calc
    elif isinstance(rule, SetCombined):
        left, right = (s.name if s.name.startswith("[") else f"[{s.name}]" for s in sources)
        formula = {
            "union": f"{left} OR {right}",
            "intersect": f"{left} AND {right}",
            "left_only": f"{left} AND NOT {right}",
            "right_only": f"{right} AND NOT {left}",
        }[rule.op]
        calc = check_formula(ir, datasource, formula, field_id).calc
    else:
        if len(levels) != 1:
            raise EditError("條件與 Top N 的集合只能有一個維度")
        for value in rule.values:
            _check_value(fields[0], value)
        text, what = (
            (rule.condition, "條件") if isinstance(rule, SetCondition) else (rule.by, "排名依據")
        )
        check = check_formula(ir, datasource, text)
        if check.level != "aggregate":
            raise EditError(f"集合的{what}必須是彙總，例如 SUM([Sales]) > 0")
        if isinstance(rule, SetCondition) and check.datatype != "boolean":
            raise EditError("集合的條件必須是布林值，例如 SUM([Profit]) > 0")
        if isinstance(rule, SetTop) and isinstance(rule.count, ParamRef):
            parameter = next((p for p in ir.parameters if p.id == rule.count.parameter), None)
            if parameter is None or parameter.datatype != "integer":
                raise EditError("Top N 的數量必須是整數的參數")
    if existing is not None:
        field = existing.model_copy(update={"caption": caption, "calc": calc, "set": field_set})
    else:
        field = DataField(
            id=_unique_id(ir, "f_" + _slug(caption)),
            name=_new_name(ds),
            caption=caption,
            role="dimension",
            datatype="boolean",
            calc=calc,
            set=field_set,
            aliases=list(IN_OUT),
            source_ref=EDITOR_REF,
        )
    updated = _put(ir, ds, field, f"集合「{caption}」不正確")
    return updated, field


# ---------------------------------------------------------------- 預設屬性、刪除


UNSET: Any = object()


def set_field_properties(
    ir: IR,
    datasource: str,
    field_id: str,
    *,
    format: Any = UNSET,
    comment: Any = UNSET,
    fiscal_year_start: Any = UNSET,
    default_color: Any = UNSET,
    default_shapes: Any = UNSET,
    default_sort: Any = UNSET,
    total_using: Any = UNSET,
) -> IR:
    """預設屬性（Tableau 的「預設屬性」）：數字或日期的格式字串（None 為自動）、註解（None 為沒有）、
    日期欄位的會計年度起始月（1 到 12；None 沿用資料源的設定），以及色彩（ColorPalette）、形狀
    （各成員的形狀）、排序（FieldSort）與總計使用（sum、avg、min、max；None 為自動）。後四項在工作表
    沒有另外設定時作用：沿用舊預設的工作表一併換成新的。"""
    ds = _datasource(ir, datasource)
    field = _field(ds, field_id)
    update: dict[str, Any] = {}
    try:
        if default_color is not UNSET:
            palette = None if default_color is None else ColorPalette.model_validate(default_color)
            if palette is not None and (palette.kind == "discrete") != (field.role == "dimension"):
                raise EditError("維度的預設色彩是各成員的顏色，量值的是連續的色盤")
            update["default_color"] = palette
        if default_shapes is not UNSET:
            if field.role != "dimension":
                raise EditError("只有維度可以設定預設的形狀")
            update["default_shapes"] = [
                ShapeAssignment.model_validate(item) for item in default_shapes or []
            ]
        if default_sort is not UNSET:
            if field.role != "dimension":
                raise EditError("只有維度可以設定預設的排序")
            sort = None if default_sort is None else FieldSort.model_validate(default_sort)
            if sort is not None and sort.by is not None:
                # 依欄位排序：依據是同一個資料源中的另一個欄位（量值要有彙總）
                by = next((f for f in ds.fields if f.id == sort.by.field), None)
                if by is None:
                    raise EditError(f"排序依據的欄位 {sort.by.field} 不在資料源「{ds.caption}」中")
                if by.id == field.id:
                    raise EditError("排序依據不能是這個欄位自己")
                aggregate = by.calc is not None and by.calc.level == "aggregate"
                if sort.by.agg is None and not aggregate:
                    raise EditError(f"排序依據「{by.caption}」要指定彙總方式")
                if sort.by.table_calc is not None:
                    raise EditError("排序依據不能是表計算")
            update["default_sort"] = sort
    except ValidationError as exc:
        raise EditError(f"「{field.caption}」的預設屬性不正確：{_first_error(exc)}") from exc
    if total_using is not UNSET:
        if field.role != "measure":
            raise EditError("只有量值可以設定總計的計算方式")
        if total_using not in (None, "sum", "avg", "min", "max"):
            raise EditError(f"不認得的總計計算方式 {total_using}")
        update["total_using"] = total_using
    if update:
        changed = field.model_copy(update=update)
        updated = _replace_datasource(
            ir,
            ds.model_copy(
                update={"fields": [changed if f.id == field.id else f for f in ds.fields]}
            ),
        )
        ir = _validated(
            repush_field_defaults(updated, field), f"「{field.caption}」的預設屬性不正確"
        )
        ds = _datasource(ir, datasource)
        field = _field(ds, field_id)
        update = {}
    if format is not UNSET:
        text = (format or "").strip() or None
        update["format"] = text
    if comment is not UNSET:
        text = (comment or "").strip() or None
        update["comment"] = text
    if fiscal_year_start is not UNSET:
        if field.datatype not in ("date", "datetime"):
            raise EditError(f"「{field.caption}」不是日期，沒有會計年度")
        _check_month(fiscal_year_start, allow_none=True)
        update["fiscal_year_start"] = fiscal_year_start
    if not update:
        return ir
    return _put(ir, ds, field.model_copy(update=update), f"「{field.caption}」的設定不正確")


WEEK_DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


def _check_month(value: Any, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 12:
        raise EditError(f"會計年度的起始月要是 1 到 12（不是 {value}）")


def set_date_properties(
    ir: IR, datasource: str, *, week_start: Any = UNSET, fiscal_year_start: Any = UNSET
) -> IR:
    """資料源的日期屬性（Tableau 的「日期屬性」）：週起始日與會計年度起始月（1 為一月，即日曆年）。"""
    ds = _datasource(ir, datasource)
    update: dict[str, Any] = {}
    if week_start is not UNSET:
        if week_start not in WEEK_DAYS:
            raise EditError(f"不認得的週起始日 {week_start}")
        update["week_start"] = week_start
    if fiscal_year_start is not UNSET:
        _check_month(fiscal_year_start)
        update["fiscal_year_start"] = fiscal_year_start
    if not update:
        return ir
    settings = ds.settings.model_copy(update=update)
    return _validated(
        _replace_datasource(ir, ds.model_copy(update={"settings": settings})), "日期屬性不正確"
    )


# ---------------------------------------------------------------- 分割


SPLIT_LIMIT = 10
"""分割最多建立的欄位數（與 Tableau 相同）。"""


def split_field(
    ir: IR,
    datasource: str,
    field_id: str,
    separator: str,
    count: int,
    from_end: bool = False,
) -> tuple[IR, list[DataField]]:
    """分割與自訂分割（Tableau 的「轉換＞分割／自訂分割」）：字串欄位依分隔符號分成 count 個計算欄位
    「<名稱> - Split N」，公式為 TRIM(SPLIT([欄位], "分隔符號", N))；from_end 時取最後的 count 段
    （N 為負數）。自動分割由編輯器依資料中的值決定分隔符號與段數後呼叫。"""
    ds = _datasource(ir, datasource)
    field = _field(ds, field_id)
    if field.datatype != "string":
        raise EditError(f"「{field.caption}」不是字串，不能分割")
    if not separator:
        raise EditError("分割需要分隔符號")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= SPLIT_LIMIT:
        raise EditError(f"分割的欄位數要是 1 到 {SPLIT_LIMIT}")
    ref = "[" + field.caption.replace("]", "]]") + "]"
    created: list[DataField] = []
    for n in range(1, count + 1):
        taken = {f.caption for f in _datasource(ir, datasource).fields}
        base = f"{field.caption} - Split {n}"
        caption, suffix = base, 2
        while caption in taken:
            caption = f"{base} ({suffix})"
            suffix += 1
        index = -n if from_end else n
        formula = f"TRIM( SPLIT( {ref}, {formula_literal(separator)}, {index} ) )"
        ir, made = upsert_calc(ir, datasource, caption, formula)
        created.append(made)
    return ir, created


# ---------------------------------------------------------------- 合併欄位


def create_combined(ir: IR, datasource: str, field_ids: Sequence[str]) -> tuple[IR, DataField]:
    """合併欄位（Tableau 的「建立＞合併欄位」）：兩個以上的維度，值為各欄位的值以「, 」連接（Null 顯示為
    Null，與解析器相同），名稱為「A & B (Combined)」。"""
    ds = _datasource(ir, datasource)
    parts = [_field(ds, field_id) for field_id in dict.fromkeys(field_ids)]
    if len(parts) < 2:
        raise EditError("合併欄位要選兩個以上的維度")
    for part in parts:
        if not _is_dimension(part) or part.datatype == "spatial" or part.set is not None:
            raise EditError(f"「{part.caption}」不是維度，不能合併")
    caption = _caption(ds, "", " & ".join(p.caption for p in parts) + " (Combined)", None)
    formula = ' + ", " + '.join(f'IFNULL(STR({p.name}), "Null")' for p in parts)
    check = check_formula(ir, datasource, formula)
    field = DataField(
        id=_unique_id(ir, "f_" + _slug(caption)),
        name="[" + caption.replace("]", "]]") + "]",
        caption=caption,
        role="dimension",
        datatype="string",
        calc=check.calc,
        combined=[p.id for p in parts],
        source_ref=EDITOR_REF,
    )
    return _put(ir, ds, field, f"無法建立「{caption}」"), field


# ---------------------------------------------------------------- 資料夾


def _folder_index(ds: Datasource, name: str) -> int:
    found = next((i for i, f in enumerate(ds.folders) if f.name == name), None)
    if found is None:
        raise EditError(f"找不到資料夾「{name}」")
    return found


def _sorted_folders(folders: list[FieldFolder]) -> list[FieldFolder]:
    """資料夾依名稱排序（與 Tableau 的資料窗格相同）。"""
    return sorted(folders, key=lambda f: (f.name.casefold(), f.role or ""))


def _with_folders(ir: IR, ds: Datasource, folders: list[FieldFolder]) -> IR:
    updated = ds.model_copy(update={"folders": _sorted_folders(folders)})
    return _validated(_replace_datasource(ir, updated), "資料夾的設定不正確")


def create_folder(ir: IR, datasource: str, name: str, field_ids: Sequence[str] = ()) -> IR:
    """建立資料夾（Tableau 的「資料夾＞建立資料夾」），選取的欄位移進去。"""
    ds = _datasource(ir, datasource)
    name = name.strip()
    if not name:
        raise EditError("資料夾需要名稱")
    if any(f.name.casefold() == name.casefold() for f in ds.folders):
        raise EditError(f"已經有名為「{name}」的資料夾")
    moving = [_field(ds, field_id).id for field_id in dict.fromkeys(field_ids)]
    folders = [
        f.model_copy(update={"fields": [x for x in f.fields if x not in moving]})
        for f in ds.folders
    ]
    return _with_folders(ir, ds, [*folders, FieldFolder(name=name, fields=moving)])


def move_to_folder(ir: IR, datasource: str, field_ids: Sequence[str], folder: str | None) -> IR:
    """欄位移到資料夾（Tableau 的「資料夾＞加入資料夾」、拖到資料夾的名稱上）；folder 為 None 時移出資料夾。"""
    ds = _datasource(ir, datasource)
    moving = [_field(ds, field_id).id for field_id in dict.fromkeys(field_ids)]
    folders = [
        f.model_copy(update={"fields": [x for x in f.fields if x not in moving]})
        for f in ds.folders
    ]
    if folder is not None:
        _folder_index(ds, folder)
        # 同名的資料夾分在維度與量值之下時（2020.2 以前），依欄位的角色放進對應的一個
        role = _field(ds, moving[0]).role if moving else None
        candidates = [i for i, f in enumerate(folders) if f.name == folder]
        index = next((i for i in candidates if folders[i].role in (None, role)), candidates[0])
        target = folders[index]
        folders[index] = target.model_copy(update={"fields": [*target.fields, *moving]})
    return _with_folders(ir, ds, folders)


def rename_folder(ir: IR, datasource: str, name: str, new_name: str) -> IR:
    ds = _datasource(ir, datasource)
    _folder_index(ds, name)
    new_name = new_name.strip()
    if not new_name:
        raise EditError("資料夾需要名稱")
    if any(f.name.casefold() == new_name.casefold() and f.name != name for f in ds.folders):
        raise EditError(f"已經有名為「{new_name}」的資料夾")
    # 同名分在維度、量值之下的資料夾（2020.2 以前）一起改名
    folders = [f.model_copy(update={"name": new_name}) if f.name == name else f for f in ds.folders]
    return _with_folders(ir, ds, folders)


def remove_folder(ir: IR, datasource: str, name: str) -> IR:
    """刪除資料夾：其中的欄位回到最上層（欄位本身不刪除）。"""
    ds = _datasource(ir, datasource)
    _folder_index(ds, name)
    return _with_folders(ir, ds, [f for f in ds.folders if f.name != name])


def remove_field(ir: IR, datasource: str, field_id: str) -> IR:
    """刪除計算欄位、群組、數值分組或集合（資料表中的欄位只能隱藏）；工作表、其他欄位、篩選或動作
    用到時退回並說明用到的地方。"""
    ir = prune_ad_hoc(ir, datasource)
    ds = _datasource(ir, datasource)
    field = _field(ds, field_id)
    if field.column is not None:
        raise EditError(f"「{field.caption}」是資料表中的欄位，只能隱藏")
    check_unused(ir, ds, [field])
    updated = ds.model_copy(
        update={
            "fields": [f for f in ds.fields if f.id != field_id],
            "hierarchies": _without_fields(ds.hierarchies, {field_id}),
        }
    )
    return _validated(_replace_datasource(ir, updated), f"「{field.caption}」還在使用中，不能刪除")


__all__ = [
    "IN_OUT",
    "create_user_filter",
    "duplicate_field",
    "remove_field",
    "set_aliases",
    "set_field_properties",
    "upsert_bins",
    "upsert_group",
    "upsert_set",
]
