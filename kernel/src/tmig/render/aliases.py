"""別名：維度成員與參數值的顯示名稱。

與 Tableau 相同，別名只改變顯示（標題、標籤、圖例、工具提示、篩選控制項、參數控制項與標題中的參數值），
篩選、排序與計算仍用原始值。成員別名只用於離散維度的原始成員：彙總、日期衍生、Table Calculation
與連續的欄位沒有成員，不套用。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tmig.ir import DataField, FieldUse, MemberAlias, Parameter


def alias_key(value: Any) -> Any:
    """比對用的鍵：日期以 ISO 字串比對（IR 的常值是字串，查詢結果是 date、datetime）。"""
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def alias_map(aliases: list[MemberAlias]) -> dict[Any, str]:
    """原始值（比對鍵）→ 顯示名稱。"""
    return {alias_key(a.value): a.label for a in aliases}


def member_aliases(data_field: DataField, use: FieldUse) -> dict[Any, str]:
    """欄位這次使用時的成員別名；不適用時是空的。"""
    if (
        not data_field.aliases
        or use.agg is not None
        or use.date is not None
        or use.table_calc is not None
        or use.continuous
    ):
        return {}
    return alias_map(data_field.aliases)


def parameter_label(param: Parameter, value: Any) -> str | None:
    """參數值的顯示名稱（Tableau 的「顯示為」）；沒有別名時為 None。"""
    if not param.aliases:
        return None
    return alias_map(param.aliases).get(alias_key(value))


__all__ = ["alias_key", "alias_map", "member_aliases", "parameter_label"]
