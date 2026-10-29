"""cases.yaml：每張工作表要比對的篩選組合，以及網址參數 → 篩選狀態的轉換（E1、E3）。

網址參數的規則與 Tableau 相同：

- 名稱為欄位的顯示名稱，大小寫須相符；與參數的顯示名稱相同時設定參數
- 日期部分寫成 ``year(Order Date)``（函式名稱不分大小寫）
- 多個值以逗號分隔，值裡的逗號寫成 ``\\,``
- 日期寫成 ``yyyy-mm-dd`` 或 ``yyyy-mm-dd hh:mm:ss``
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tmig.ir.models import IR, DataField, Scalar
from tmig.parser.values import LiteralError, parse_literal
from tmig.planner.identity import Identity
from tmig.planner.state import DATE_PARTS, FilterValue, QueryState

_PART = re.compile(r"^(?P<fn>[A-Za-z]+)\((?P<name>.+)\)$")


class CasesError(ValueError):
    pass


class SheetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worksheet: str = Field(description="IR 的工作表 id")
    view: str = Field(description="Tableau 網址中的工作表名稱，例如 SalesbyRegion")


class CaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    url_params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    note: str | None = None


class CasesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbook: str
    tableau_views_url: str | None = None
    fetched_at: str | None = None
    today: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="相對日期篩選的基準日；沒有時取 fetched_at 的日期（Tableau 匯出預期結果的那一天）",
    )
    user: Identity | None = Field(
        default=None,
        description="抓取預期結果的 Tableau 帳號；報表用到使用者函式（列層級權限）時，以這個身分查詢",
    )
    sheets: list[SheetSpec]
    cases: list[CaseSpec]

    def as_of(self) -> str | None:
        """相對日期篩選的基準日：today，或抓取預期結果的日期。"""
        if self.today is not None:
            return self.today
        if self.fetched_at and re.match(r"^\d{4}-\d{2}-\d{2}", self.fetched_at):
            return self.fetched_at[:10]
        return None

    def case(self, name: str) -> CaseSpec:
        for case in self.cases:
            if case.name == name:
                return case
        raise CasesError(
            f"找不到篩選組合「{name}」；可用的有：{'、'.join(c.name for c in self.cases)}"
        )


def load_cases(path: str | Path) -> CasesFile:
    source = Path(path)
    if not source.is_file():
        raise CasesError(f"找不到 {source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    try:
        return CasesFile.model_validate(data)
    except ValueError as exc:
        raise CasesError(f"{source.name} 格式錯誤：{exc}") from exc


def split_values(text: str) -> list[str]:
    """以逗號分隔多個值；\\, 是值裡的逗號。"""
    values: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == ",":
            current.append(",")
            i += 2
            continue
        if text[i] == ",":
            values.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(text[i])
        i += 1
    values.append("".join(current).strip())
    return values


def _value(text: str, datatype: str) -> Scalar:
    if datatype == "string":
        return text
    try:
        return parse_literal(text, datatype)
    except LiteralError as exc:
        raise CasesError(f"值「{text}」不是 {datatype}：{exc}") from exc


def case_state(ir: IR, case: CaseSpec, today: str | None = None) -> QueryState:
    """網址參數 → 篩選與參數狀態；today 為相對日期篩選的基準日（見 CasesFile.as_of）。"""
    fields: dict[str, DataField] = {}
    for ds in ir.datasources:
        for item in ds.fields:
            fields.setdefault(item.caption, item)
    params = {p.caption: p for p in ir.parameters}
    state = QueryState(today=today)
    for name, raw in case.url_params.items():
        text = (
            raw if isinstance(raw, str) else str(raw).lower() if isinstance(raw, bool) else str(raw)
        )
        if name in params:
            param = params[name]
            state.parameters[param.id] = _value(text, param.datatype)
            continue
        match = _PART.match(name)
        if match and match.group("fn").lower() in DATE_PARTS and match.group("name") in fields:
            part = match.group("fn").lower()
            item = fields[match.group("name")]
            values = [_value(v, "integer") for v in split_values(text)]
            state.filters[f"{part}({item.id})"] = FilterValue(mode="include", values=values)
            continue
        if name not in fields:
            known = "、".join(sorted(fields))
            raise CasesError(
                f"篩選組合「{case.name}」的網址參數「{name}」不是欄位或參數名稱（大小寫須相符）；欄位有：{known}"
            )
        item = fields[name]
        values = [_value(v, item.datatype) for v in split_values(text)]
        state.filters[item.id] = FilterValue(mode="include", values=values)
    return state
