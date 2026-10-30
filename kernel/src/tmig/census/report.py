"""把盤點報告轉成給人看的 Markdown。"""

from __future__ import annotations

import re
from pathlib import PurePath

from tmig.census.model import CensusReport, DatasourceInfo
from tmig.formula.catalog import in_v0
from tmig.parser.refs import qualified_parts

STATUS = {"pass": "通過", "warn": "注意", "fail": "不符合"}
FORMULA_PREVIEW = 120


def _cell(text: object) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip()
    return value.replace("|", "\\|") or "—"


def _short(column: str) -> str:
    parts = qualified_parts(column)
    return parts[-1] if parts else column


def _size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024:.1f} KB"


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines


def _connection_notes(d: DatasourceInfo) -> str:
    """連線欄的補充：驗證方式、Tableau Server 的驗證設定、Initial SQL。"""
    notes = [f"驗證：{a}" for a in d.authentication if a != "auth-none"]
    notes += [f"Tableau Server：{m}" for m in d.server_auth_modes]
    if d.initial_sql:
        users = "、".join(d.initial_sql_user)
        notes.append(f"Initial SQL（{users}）" if users else "Initial SQL")
    return f"（{'；'.join(notes)}）" if notes else ""


def render_markdown(r: CensusReport) -> str:
    lines: list[str] = [f"# 功能盤點：{PurePath(r.file).name}", ""]
    lines.append(f"- Tableau 版本：{r.tableau_version or '未知'}")
    lines.append(f"- 分級：{r.grade}（{'；'.join(r.grade_reasons)}）")
    failed = [c.title for c in r.checks if c.status == "fail"]
    warned = [c.title for c in r.checks if c.status == "warn"]
    if failed:
        lines.append(f"- 起步條件：不符合 {len(failed)} 項（{'、'.join(failed)}）")
    elif warned:
        lines.append(f"- 起步條件：符合，但有 {len(warned)} 項需注意（{'、'.join(warned)}）")
    else:
        lines.append("- 起步條件：全部符合")

    lines += ["", "## 起步條件", ""]
    lines += _table(
        ["檢查", "結果", "說明"], [[c.title, STATUS[c.status], c.detail] for c in r.checks]
    )

    lines += ["", "## 資料源", ""]
    lines += _table(
        ["資料源", "連線", "萃取檔", "萃取資料表", "來源關聯", "欄位", "計算欄位"],
        [
            [
                d.caption,
                ("Published" if d.published else "、".join(d.connection_classes))
                + _connection_notes(d),
                "、".join(d.extract_files),
                "、".join(d.extract_tables),
                "、".join(d.source_relation_types)
                + (f"（{d.logical_tables} 張邏輯表）" if d.logical_tables > 1 else ""),
                d.field_count,
                d.calc_count,
            ]
            for d in r.datasources
        ],
    )

    if r.files:
        lines += ["", "## .twbx 內的檔案", ""]
        lines += _table(["檔案", "大小"], [[f.name, _size(f.size)] for f in r.files])
        big = [f.name for f in r.files if f.size > 100 * 1024 * 1024]
        if big:
            lines += ["", f"超過 100 MB，需用 Git LFS：{'、'.join(big)}"]

    lines += ["", "## 公式函式", ""]
    if r.functions:
        lines += _table(
            ["函式", "用到的計算欄位數", "v0 函式集"],
            [[name, count, "有" if in_v0(name) else "沒有"] for name, count in r.functions.items()],
        )
    else:
        lines.append("沒有呼叫任何函式。")

    lines += ["", f"## 計算欄位（{len(r.formulas)}）", ""]
    if r.formulas:
        rows: list[list[object]] = []
        for f in r.formulas:
            traits = list(f.lod) + f.table_calc_functions + f.user_functions
            preview = (
                f.formula
                if len(f.formula) <= FORMULA_PREVIEW
                else f.formula[:FORMULA_PREVIEW] + "…"
            )
            rows.append(
                [f.datasource, f.field, "、".join(traits), "、".join(f.outside_v0), preview]
            )
        lines += _table(["資料源", "欄位", "LOD／Table Calc／使用者函式", "不在 v0", "公式"], rows)
    else:
        lines.append("沒有計算欄位。")

    lines += ["", f"## 工作表（{len(r.worksheets)}）", ""]
    rows = []
    for w in r.worksheets:
        others: list[str] = []
        if w.measure_names_values:
            others.append("Measure Names／Values")
        if w.pane_count > 1 or {"+", "*"} & set(w.shelf_operators):
            others.append("雙軸或合併軸")
        if w.reference_lines or w.trend_lines:
            others.append(f"參考線 {w.reference_lines}、趨勢線 {w.trend_lines}")
        if w.maps:
            others.append("地圖")
        if w.blending:
            others.append("Blending")
        filters = [
            f"{_short(f.column)} {f.kind}{'（context）' if f.context else ''}" for f in w.filters
        ]
        calcs = [
            f"{_short(c.column)} {c.type or 'formula'}／{c.ordering_type or '預設'}"
            for c in w.table_calcs
        ]
        rows.append(
            [
                w.name,
                "、".join(w.mark_types),
                "、".join(w.encodings),
                "；".join(filters),
                "；".join(calcs),
                "、".join(others),
            ]
        )
    lines += _table(["工作表", "標記", "編碼", "篩選", "Table Calculation", "其他"], rows)

    if r.dashboards:
        lines += ["", f"## Dashboard（{len(r.dashboards)}）", ""]
        lines += _table(
            ["名稱", "尺寸", "區塊類型", "工作表"],
            [
                [
                    d.name + ("（Story）" if d.story else ""),
                    d.sizing
                    + (
                        f" {d.size.get('maxwidth', '?')}×{d.size.get('maxheight', '?')}"
                        if d.size
                        else ""
                    ),
                    "、".join(f"{k} {v}" for k, v in d.zone_types.items()),
                    "、".join(d.worksheets),
                ]
                for d in r.dashboards
            ],
        )

    if r.parameters:
        lines += ["", f"## 參數（{len(r.parameters)}）", ""]
        lines += _table(
            ["參數", "型別", "值域", "目前值"],
            [[p.caption, p.datatype, p.domain, p.value or ""] for p in r.parameters],
        )

    if r.actions:
        lines += ["", f"## 動作（{len(r.actions)}）", ""]
        lines += _table(
            ["名稱", "類型", "觸發", "來源"],
            [[a.caption, a.kind, a.activation or "", a.source] for a in r.actions],
        )

    lines += ["", "完整資料（含 XML 標籤清單）見同名的 .census.json。", ""]
    return "\n".join(lines)
