"""mapping.yaml：預期 CSV 的欄名 → 查詢結果的欄位鍵（E2）。

Tableau 匯出 CSV 的欄名用顯示名稱（例如 ``Sales``、``Year of Order Date``、
``Running Sum of Sales``），各版本與語系的寫法不完全相同，所以先自動產生對照建議，
人工確認後把 ``confirmed`` 改成 true 再提交。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tmig.ir.models import IR, FieldUse, TableCalc
from tmig.planner.state import use_key
from tmig.sqlgen.query import OutputColumn

NumericMode = Literal["auto", "raw", "display"]


class MappingError(ValueError):
    pass


class SheetMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: dict[str, str | None] = Field(
        default_factory=dict, description="CSV 欄名 → 欄位鍵；值為 null 表示這個 CSV 欄位不比對"
    )
    ignore: list[str] = Field(default_factory=list, description="查詢結果中不比對的欄位鍵")
    numeric: NumericMode = Field(
        default="auto",
        description="auto：帶格式（千分位、百分比等）的值以顯示精度比對，其餘以相對誤差 1e-6；"
        "raw：一律相對誤差；display：一律以顯示精度比對",
    )


class MappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: bool = False
    sheets: dict[str, SheetMapping] = Field(default_factory=dict)


def load_mapping(path: str | Path) -> MappingFile:
    source = Path(path)
    if not source.is_file():
        raise MappingError(f"找不到 {source}；先執行 tmig mapping suggest 產生建議")
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    try:
        return MappingFile.model_validate(data)
    except ValueError as exc:
        raise MappingError(f"{source.name} 格式錯誤：{exc}") from exc


AGG_LABELS: dict[str, list[str]] = {
    "sum": ["{c}", "SUM({c})", "Sum of {c}"],
    "avg": ["AVG({c})", "Avg. {c}", "Average of {c}"],
    "min": ["MIN({c})", "Min. {c}", "Minimum of {c}"],
    "max": ["MAX({c})", "Max. {c}", "Maximum of {c}"],
    "count": ["CNT({c})", "COUNT({c})", "Count of {c}"],
    "countd": [
        "CNTD({c})",
        "COUNTD({c})",
        "Count Distinct of {c}",
        "Distinct count of {c}",
        "Count (Distinct) of {c}",
    ],
    "median": ["MEDIAN({c})", "Median of {c}", "Median {c}"],
    "attr": ["ATTR({c})", "{c}"],
    "stdev": ["STDEV({c})", "Std. Dev. of {c}"],
    "stdevp": ["STDEVP({c})", "Std. Dev. (Pop.) of {c}"],
    "var": ["VAR({c})", "Variance of {c}"],
    "varp": ["VARP({c})", "Variance (Pop.) of {c}"],
    # 百分位數：Tableau 匯出的欄名未確認（依膠囊的寫法推定）
    "pct5": ["PCT5({c})", "Pct5 {c}", "Percentile 5 of {c}"],
    "pct10": ["PCT10({c})", "Pct10 {c}", "Percentile 10 of {c}"],
    "pct25": ["PCT25({c})", "Pct25 {c}", "Percentile 25 of {c}"],
    "pct50": ["PCT50({c})", "Pct50 {c}", "Percentile 50 of {c}"],
    "pct75": ["PCT75({c})", "Pct75 {c}", "Percentile 75 of {c}"],
    "pct90": ["PCT90({c})", "Pct90 {c}", "Percentile 90 of {c}"],
    "pct95": ["PCT95({c})", "Pct95 {c}", "Percentile 95 of {c}"],
}
AGG_WORDS = {"sum": "Sum", "avg": "Average", "min": "Minimum", "max": "Maximum"}


def _labels(ir: IR, use: FieldUse, table_calcs: list[TableCalc]) -> list[str]:
    data_field = next(f for ds in ir.datasources for f in ds.fields if f.id == use.field)
    caption = data_field.caption
    if data_field.set is not None and use.agg is None:
        # 集合的 IN/OUT：匯出的欄名寫法依版本而異（依公開資料推定）
        return [
            f"In/Out of {caption}",
            f"In / Out of {caption}",
            f"IN/OUT({caption})",
            caption,
        ]
    if use.date is not None:
        unit = use.date.unit
        word = unit.capitalize()
        labels = [f"{word} of {caption}", f"{unit.upper()}({caption})", f"{unit}({caption})"]
        if use.date.kind == "trunc" and unit == "month":
            labels.append(f"Month, Year of {caption}")
        return labels
    base = (
        [p.format(c=caption) for p in AGG_LABELS[use.agg]]
        if use.agg
        else [caption, f"AGG({caption})"]
    )
    if use.table_calc is None:
        return base
    tc = next((t for t in table_calcs if t.id == use.table_calc), None)
    if tc is None or tc.type == "formula":
        return base
    agg_word = AGG_WORDS.get(use.agg or "sum", "Sum")
    if tc.type == "running_total":
        running = str(tc.options.get("aggregation", "sum"))
        return [
            f"Running {AGG_WORDS.get(running, 'Sum')} of {caption}",
            f"Running Total of {caption}",
        ]
    if tc.type == "percent_of_total":
        return [
            f"% of Total {caption}",
            f"% of Total {agg_word} of {caption}",
            f"Percent of Total {caption}",
        ]
    if tc.type == "rank":
        return [f"Rank of {caption}", f"Rank of {agg_word} of {caption}"]
    if tc.type == "difference":
        return [
            f"Difference in {caption}",
            f"Difference in {agg_word} of {caption}",
            f"Difference from {caption}",
        ]
    if tc.type == "moving":
        word = AGG_WORDS.get(str(tc.options.get("aggregation", "avg")), "Average")
        return [f"Moving {word} of {caption}", f"Moving {word} of {agg_word} of {caption}"]
    if tc.type == "percent_difference":
        return [
            f"% Difference in {caption}",
            f"% Difference in {agg_word} of {caption}",
            f"Percent Difference in {caption}",
        ]
    return base


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def suggest_sheet(
    ir: IR,
    worksheet: str,
    columns: list[OutputColumn],
    headers: list[str] | None,
) -> tuple[SheetMapping, list[str]]:
    """依欄位的顯示名稱推測對照；回傳對照與無法對應的 CSV 欄名。"""
    sheet = next(w for w in ir.worksheets if w.id == worksheet)
    outputs = [c for c in columns if c.role != "sort" and c.use is not None]
    candidates = {
        c.key: _labels(ir, c.use, sheet.table_calcs) for c in outputs if c.use is not None
    }
    # Measure Names 的別名：Tableau 匯出的資料以別名為量值的欄名
    for alias in sheet.measure_aliases:
        key = use_key(alias.use)
        if key in candidates:
            candidates[key].insert(0, alias.label)
    mapping = SheetMapping()
    unmatched: list[str] = []
    if headers is None:
        for column in outputs:
            mapping.columns[candidates[column.key][0]] = column.key
        return mapping, unmatched
    used: set[str] = set()
    for header in headers:
        match = next(
            (
                key
                for key, labels in candidates.items()
                if key not in used and any(_norm(label) == _norm(header) for label in labels)
            ),
            None,
        )
        if match is None:
            unmatched.append(header)
            mapping.columns[header] = None
        else:
            used.add(match)
            mapping.columns[header] = match
    mapping.ignore = [c.key for c in outputs if c.key not in used]
    return mapping, unmatched


def csv_headers(path: Path) -> list[str]:
    """預期 CSV 的欄名；Measure Names／Values 長表先轉成寬表（每個量值一欄）。"""
    from tmig.verify.compare import read_csv

    headers, _ = read_csv(path.read_text(encoding="utf-8-sig"))
    return headers


def dump_mapping(mapping: MappingFile, notes: dict[str, list[str]]) -> str:
    lines = [
        "# CSV 欄名 → 查詢結果的欄位鍵（tmig mapping suggest 產生）",
        "# 人工確認每一列後，把 confirmed 改成 true 再提交；值為 null 的 CSV 欄位不比對",
        "# numeric：auto＝帶格式的值以顯示精度比對、其餘以相對誤差 1e-6；raw；display",
        f"confirmed: {'true' if mapping.confirmed else 'false'}",
        "sheets:",
    ]
    for sheet_id, sheet in mapping.sheets.items():
        lines.append(f"  {sheet_id}:")
        for note in notes.get(sheet_id, []):
            lines.append(f"    # {note}")
        lines.append("    columns:")
        for header, key in sheet.columns.items():
            value = "null" if key is None else _yaml_str(key)
            lines.append(f"      {_yaml_str(header)}: {value}")
        ignore = ", ".join(_yaml_str(k) for k in sheet.ignore)
        lines.append(f"    ignore: [{ignore}]")
        lines.append(f"    numeric: {sheet.numeric}")
    return "\n".join(lines) + "\n"


def _yaml_str(text: str) -> str:
    dumped = yaml.safe_dump(text, allow_unicode=True, default_style=None).strip()
    return dumped.removesuffix("\n...").removesuffix("...").strip()
