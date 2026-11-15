"""把另一份報表的工作表匯入這份報表（Tableau 的「檔案＞匯入活頁簿」、書籤，以及在活頁簿之間複製貼上
工作表）。

- 整份匯入（worksheets 為 None）：所有的工作表、dashboard、Story 與動作，以及它們用到的資料源與參數
- 只匯入選取的工作表：連同它們用到的資料源（含 Data Blending 的次要資料源）與參數；指向沒有匯入的動作、
  工具提示中的工作表的參照拿掉

id 在整份報表中不能重複：與目標報表相同的 id 加上編號（f_sales → f_sales_2），所有的參照一併改掉。
名稱相同的工作表、dashboard、Story 與資料源加上「 (2)」。參數依名稱比對：兩份報表都有、名稱與定義都相同時
共用目標報表的參數；只是名稱相同時（例如兩份報表各自的 [Parameter 1]）匯入的參數改名，公式中的參照一併改掉。

資料（萃取的 DuckDB 檔、來源檔案）由呼叫端依回傳的資料源對照複製（見 tmig_service.editor）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tmig.edit import EditError, _all_ids, _validated
from tmig.ir import IR, Parameter

FREE_TEXT = frozenset(
    {
        "formula",
        "caption",
        "name",
        "text",
        "sql",
        "title",
        "condition",
        "by",
        "comment",
        "description",
        "source_ref",
        "alias",
        "label",
        "url",
        "path",
        "hyper_file",
        "column",
        "image",
        "image_hidden",
    }
)
"""不是 id 的文字（公式、名稱、說明、路徑等）：改 id 時不動。"""


@dataclass
class Imported:
    ir: IR
    datasources: dict[str, str] = field(default_factory=dict)
    """匯入的資料源：來源報表中的 id → 匯入後的 id（資料檔依它複製）。"""
    worksheets: list[str] = field(default_factory=list)
    dashboards: list[str] = field(default_factory=list)
    stories: list[str] = field(default_factory=list)
    shared_parameters: list[str] = field(default_factory=list)
    """與目標報表共用的參數（名稱與定義相同，沒有另外匯入）。"""

    def to_json(self) -> dict[str, Any]:
        return {
            "datasources": self.datasources,
            "worksheets": self.worksheets,
            "dashboards": self.dashboards,
            "stories": self.stories,
            "shared_parameters": self.shared_parameters,
        }


def import_from(target: IR, source: IR, worksheets: list[str] | None = None) -> Imported:
    """把 source 的工作表匯入 target；worksheets 為 None 時整份匯入（含 dashboard、Story 與動作）。"""
    everything = worksheets is None
    known = {w.id for w in source.worksheets}
    chosen = list(known) if worksheets is None else list(dict.fromkeys(worksheets))
    missing = [w for w in chosen if w not in known]
    if missing:
        raise EditError(f"來源報表中沒有工作表 {'、'.join(missing)}")
    if not chosen:
        raise EditError("沒有選取要匯入的工作表")
    sheets = [w for w in source.worksheets if w.id in chosen]
    needed = {w.datasource for w in sheets} | {b.datasource for w in sheets for b in w.blends}
    piece: dict[str, list[dict[str, Any]]] = {
        "datasources": [d.model_dump(mode="json") for d in source.datasources if d.id in needed],
        "worksheets": [w.model_dump(mode="json") for w in sheets],
        "dashboards": [d.model_dump(mode="json") for d in source.dashboards] if everything else [],
        "stories": [s.model_dump(mode="json") for s in source.stories] if everything else [],
        "actions": [a.model_dump(mode="json") for a in source.actions] if everything else [],
        "parameters": [],
    }
    if not everything:
        _detach(piece["worksheets"], set(chosen))

    # 參數：用到的才匯入。目標報表有名稱與定義都相同的參數時共用；只是名稱相同（例如兩份報表各自的
    # [Parameter 1]）時改名，公式中的參照（[Parameters].[…]）一併改掉
    mapping: dict[str, str] = {}
    shared: list[str] = []
    tokens: dict[str, str] = {}
    text = _dump(piece)
    existing = {(p.name, p.caption): p for p in target.parameters}
    names = {p.name for p in target.parameters}
    captions = {p.caption for p in target.parameters}
    for param in source.parameters:
        if not _mentions(text, param):
            continue
        same = existing.get((param.name, param.caption))
        if same is not None and _same_parameter(same, param):
            mapping[param.id] = same.id
            shared.append(same.id)
            continue
        item = param.model_dump(mode="json")
        if param.name in names:
            n = 1
            while f"[Parameter {n}]" in names:
                n += 1
            item["name"] = f"[Parameter {n}]"
            tokens[f"[Parameters].{param.name}"] = f"[Parameters].{item['name']}"
        if param.caption in captions:
            item["caption"] = _unique(captions, param.caption)
            tokens[f"[Parameters].[{param.caption}]"] = f"[Parameters].[{item['caption']}]"
        names.add(item["name"])
        captions.add(item["caption"])
        piece["parameters"].append(item)
    if tokens:
        piece = _retoken(piece, tokens)

    # 與目標報表相同的 id 加上編號
    taken = _all_ids(target)
    incoming = _ids_of(piece)
    for old in sorted(incoming):
        if old in mapping or old not in taken:
            continue
        n = 2
        while f"{old}_{n}" in taken or f"{old}_{n}" in incoming or f"{old}_{n}" in mapping.values():
            n += 1
        mapping[old] = f"{old}_{n}"
    if mapping:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])("
            + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
            + r")(?![A-Za-z0-9_])"
        )
        piece = _rename(piece, pattern, mapping)

    # 名稱：工作表、dashboard 與 Story 共用一個名稱空間；資料源的名稱各自不重複
    titles = {x.name for x in [*target.worksheets, *target.dashboards, *target.stories]}
    for kind in ("worksheets", "dashboards", "stories"):
        for item in piece[kind]:
            item["name"] = _unique(titles, item["name"])
            titles.add(item["name"])
    sources = {d.caption for d in target.datasources}
    for item in piece["datasources"]:
        item["caption"] = _unique(sources, item["caption"])
        sources.add(item["caption"])

    merged = target.model_dump(mode="json")
    for key, items in piece.items():
        merged[key] = [*merged.get(key, []), *items]
    try:
        ir = IR.model_validate(merged)
    except Exception as exc:  # pydantic 的 ValidationError：內容與目前的版本不相容
        raise EditError(f"匯入的內容不正確：{exc}") from exc
    ir = _validated(ir, "匯入後的報表不正確")
    return Imported(
        ir=ir,
        datasources={d.id: mapping.get(d.id, d.id) for d in source.datasources if d.id in needed},
        worksheets=[w["id"] for w in piece["worksheets"]],
        dashboards=[d["id"] for d in piece["dashboards"]],
        stories=[s["id"] for s in piece["stories"]],
        shared_parameters=shared,
    )


def _detach(sheets: list[dict[str, Any]], chosen: set[str]) -> None:
    """只匯入部分的工作表：指向沒有匯入的動作與工作表的參照拿掉（篩選動作的篩選留下目前的值）。"""
    for sheet in sheets:
        for flt in sheet.get("filters", []):
            if flt.get("action") is not None:
                flt["action"] = None
                flt["source_fields"] = None
        cards = [
            sheet.get("marks") or {},
            *((p.get("marks") or {}) for p in sheet.get("panes", [])),
        ]
        for marks in cards:
            kept = [t for t in marks.get("tooltip_sheets", []) if t.get("worksheet") in chosen]
            if "tooltip_sheets" in marks:
                marks["tooltip_sheets"] = kept


def _dump(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _mentions(text: str, param: Parameter) -> bool:
    """這段內容用到這個參數：id（篩選的 Top N、參考線、控制項等）或公式中的名稱。"""
    if re.search(rf"(?<![A-Za-z0-9_]){re.escape(param.id)}(?![A-Za-z0-9_])", text):
        return True
    names = {param.name.strip("[]"), param.caption}
    return any(_dump(f"[Parameters].[{name}]")[1:-1] in text for name in names)


def _same_parameter(a: Parameter, b: Parameter) -> bool:
    return a.datatype == b.datatype and a.allowed == b.allowed


def _retoken(value: Any, tokens: dict[str, str]) -> Any:
    """公式中對參數的參照（[Parameters].[名稱]）換成新的名稱。"""
    if isinstance(value, dict):
        return {k: _retoken(v, tokens) for k, v in value.items()}
    if isinstance(value, list):
        return [_retoken(item, tokens) for item in value]
    if isinstance(value, str) and "[Parameters]." in value:
        for old, new in tokens.items():
            value = value.replace(old, new)
    return value


def _ids_of(piece: dict[str, list[dict[str, Any]]]) -> set[str]:
    """匯入的內容中在整份報表不能重複的 id：資料源、欄位、資料源篩選、參數、工作表、工作表的篩選與
    表計算、dashboard、Story、動作（與 tmig.ir.integrity 檢查的相同；邏輯資料表、區塊的 id 不在其中）。"""
    found: set[str] = set()
    for ds in piece["datasources"]:
        found.add(ds["id"])
        found |= {f["id"] for f in ds.get("fields", [])}
        found |= {f["id"] for f in ds.get("filters", [])}
    for sheet in piece["worksheets"]:
        found.add(sheet["id"])
        found |= {f["id"] for f in sheet.get("filters", [])}
        found |= {t["id"] for t in sheet.get("table_calcs", [])}
    for kind in ("parameters", "dashboards", "stories", "actions"):
        found |= {item["id"] for item in piece[kind]}
    return found


def _rename(value: Any, pattern: re.Pattern[str], mapping: dict[str, str], key: str = "") -> Any:
    """把 id 換成新的 id：字串的值與字典的鍵（欄位鍵，例如 sum(f_sales)#tc_1）；公式、名稱等文字不動。"""
    if isinstance(value, dict):
        return {
            pattern.sub(lambda m: mapping[m.group(0)], k): _rename(v, pattern, mapping, k)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_rename(item, pattern, mapping, key) for item in value]
    if isinstance(value, str) and key not in FREE_TEXT:
        return pattern.sub(lambda m: mapping[m.group(0)], value)
    return value


def _unique(taken: set[str], name: str) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


__all__ = ["Imported", "import_from"]
