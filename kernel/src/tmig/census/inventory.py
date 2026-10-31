"""批次盤點：一批 .twb／.twbx 的總表、功能使用統計與起步候選（S2、SDD「盤點與使用率」）。

    tmig inventory <資料夾或檔案>... --out-dir <目錄> [--server-meta server.json]

每份報表跑功能盤點（census）與寬鬆模式解析，彙整成：

- ``inventory.md``：總覽、起步候選（A 級與 B 級）、最常見的不支援項目
- ``inventory.csv``：一份報表一列（UTF-8 BOM，Excel 可直接開）
- ``features.csv``：每個功能用到的報表數與次數，決定之後先支援哪些功能
- ``census/<報表>.md``：單份報表的盤點與解析結果

``server.json`` 由 ``tools/tableau_server`` 從 Tableau Server 產生，提供 Project、擁有者與檢視次數。
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tmig.census.collect import features_outside_v0, run_census
from tmig.census.model import CensusReport, ServerConnection
from tmig.census.report import render_markdown
from tmig.ir.models import IR, Unsupported
from tmig.parser.workbook import NoDatasourceError, parse_workbook
from tmig.planner.identity import Identity
from tmig.sqlgen.query import QueryError, worksheet_sql

INVENTORY_USER = Identity(username="inventory")
"""試產生 SQL 用的身分：用到使用者函式的工作表也能產生查詢（只檢查能不能查，不執行）。"""

WORKBOOK_SUFFIXES = frozenset({".twb", ".twbx"})
SIMPLE_MARKS = frozenset({"Automatic", "Bar", "Line", "Text", "Area", "Circle", "Square", "Shape"})
TOP_CANDIDATES = 10
TOP_FEATURES = 25

Readiness = Literal["ready", "needs_features", "not_eligible", "error"]
READINESS_TEXT: dict[Readiness, str] = {
    "ready": "可直接開始",
    "needs_features": "需先補功能",
    "not_eligible": "不符合起步條件",
    "error": "讀取失敗",
}


class ServerWorkbook(BaseModel):
    """server.json 中的一份 workbook（來自 Tableau Server REST API）。"""

    model_config = ConfigDict(extra="ignore")

    file: str | None = Field(default=None, description="下載的檔案，相對於 server.json 所在目錄")
    id: str
    name: str
    content_url: str | None = None
    project: str | None = None
    owner: str | None = None
    views: int | None = Field(default=None, description="所有 view 的檢視次數合計")
    sheets: list[str] = Field(default_factory=list, description="已發佈的 view 名稱")
    updated_at: str | None = None
    url: str | None = None
    has_extracts: bool | None = None
    connections: list[str] = Field(
        default_factory=list, description="連線類型，例如 oracle、sqlproxy"
    )
    connection_details: list[ServerConnection] = Field(
        default_factory=list,
        description="各連線是否嵌入密碼（含報表連到的 Published Data Source 的連線）",
    )
    download_error: str | None = None


class ServerMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server: str
    site: str = ""
    fetched_at: str | None = None
    extracts_omitted: bool = Field(default=True, description="下載時是否刻意不含萃取檔")
    workbooks: list[ServerWorkbook] = Field(default_factory=list)


class InventoryRow(BaseModel):
    file: str = Field(description="檔案路徑；沒有下載時為空字串")
    name: str
    project: str | None = None
    owner: str | None = None
    views: int | None = None
    updated_at: str | None = None
    url: str | None = None
    tableau_version: str | None = None
    grade: Literal["A", "B", "C"] | None = None
    grade_reasons: list[str] = Field(default_factory=list)
    readiness: Readiness
    candidate: Literal["A", "B"] | None = Field(default=None, description="起步候選的類別")
    failed_checks: list[str] = Field(default_factory=list)
    warned_checks: list[str] = Field(default_factory=list)
    datasources: list[str] = Field(
        default_factory=list, description="例如 嵌入式萃取、Live（oracle）"
    )
    worksheets: int = 0
    dashboards: int = 0
    calc_fields: int = 0
    lod: int = 0
    table_calcs: int = 0
    marks: list[str] = Field(default_factory=list)
    functions_outside_v0: list[str] = Field(default_factory=list)
    unsupported: int = 0
    unsupported_top: list[str] = Field(default_factory=list, description="最常見的不支援項目")
    detail: str | None = Field(default=None, description="census/ 下的單份報表說明")
    error: str | None = None


@dataclass
class FeatureStat:
    workbooks: set[str] = field(default_factory=set)
    occurrences: int = 0


@dataclass
class InventoryResult:
    rows: list[InventoryRow]
    features: dict[tuple[str, str], FeatureStat]
    out_dir: Path
    source: str | None = None

    def candidates(self, kind: Literal["A", "B"]) -> list[InventoryRow]:
        rows = [r for r in self.rows if r.candidate == kind]
        return sorted(rows, key=_candidate_key)


def _candidate_key(row: InventoryRow) -> tuple[int, int, int, str]:
    return (row.unsupported, len(row.warned_checks), -(row.views or 0), row.name)


# ---------------------------------------------------------------- 單份報表


def find_workbooks(paths: Iterable[str | Path]) -> list[Path]:
    """展開資料夾（含子資料夾），只取 .twb／.twbx；結果排序並去重。"""
    found: dict[Path, None] = {}
    for item in paths:
        path = Path(item)
        if path.is_dir():
            for sub in sorted(path.rglob("*")):
                if sub.is_file() and sub.suffix.lower() in WORKBOOK_SUFFIXES:
                    found[sub.resolve()] = None
        elif path.suffix.lower() in WORKBOOK_SUFFIXES:
            found[path.resolve()] = None
    return sorted(found)


def unsupported_kind(entry: Unsupported) -> str:
    """不支援項目的類別：把原因中的名稱（「…」）與數字抽掉，同類的項目才能合計。"""
    reason = re.sub(r"「[^」]*」", "「…」", entry.reason)
    # 數字換成 N（v0、utf8 這類字母後的數字保留）
    return re.sub(r"(?<![A-Za-z])\d+(?:\.\d+)?", "N", reason)


def _datasource_kinds(report: CensusReport) -> list[str]:
    kinds = []
    for ds in report.datasources:
        if ds.published:
            kinds.append("Published Data Source")
        elif ds.data_in_hyper:
            kinds.append("嵌入式萃取")
        else:
            classes = "、".join(c for c in ds.connection_classes if c) or "未知"
            kinds.append(f"Live（{classes}）")
    return kinds


def _candidate(report: CensusReport, readiness: Readiness) -> Literal["A", "B"] | None:
    if readiness not in ("ready", "needs_features"):
        return None
    if report.grade == "A":
        marks = {m for w in report.worksheets for m in w.mark_types}
        return "A" if marks <= SIMPLE_MARKS else None
    if report.grade == "B":
        fixed = report.lod.get("FIXED", 0) > 0
        table_calc = any(w.table_calcs for w in report.worksheets) or any(
            f.table_calc_functions for f in report.formulas
        )
        return "B" if fixed or table_calc else None
    return None


def inventory_workbook(
    path: Path,
    *,
    extracts_omitted: bool = False,
    server_connections: list[ServerConnection] | None = None,
) -> tuple[InventoryRow, CensusReport | None, list[Unsupported]]:
    """盤點一份報表：census＋寬鬆模式解析。任何錯誤都記在列上，不中斷批次。

    server_connections：Tableau Server 上這份報表的連線設定，檢查檢視者是否用自己的資料庫帳號連線。
    """
    name = path.stem
    try:
        report = run_census(
            path, extracts_omitted=extracts_omitted, server_connections=server_connections or ()
        )
    except Exception as exc:  # 批次盤點：一份讀不了不影響其他報表
        return (
            InventoryRow(file=str(path), name=name, readiness="error", error=_error(exc)),
            None,
            [],
        )
    unsupported: list[Unsupported] = []
    parse_error = None
    try:
        ir = parse_workbook(path, lenient=True, extracts_omitted=extracts_omitted)
        unsupported = [*ir.unsupported, *query_unsupported(ir)]
    except NoDatasourceError as exc:
        # 沒有可移轉的資料源（例如都是尚無執行器的連線）：原因列為不支援，需先補功能
        unsupported = exc.unsupported
        if not unsupported:
            parse_error = _error(exc)
    except Exception as exc:  # 解析器還沒處理的結構：記錄後繼續
        parse_error = _error(exc)

    failed = [c.title for c in report.checks if c.status == "fail"]
    warned = [c.title for c in report.checks if c.status == "warn"]
    readiness: Readiness
    if failed:
        # 不符合起步條件的報表，寬鬆解析失敗（例如全是 Live 連線）只是附帶說明
        readiness = "not_eligible"
    elif parse_error is not None:
        readiness = "error"
    elif unsupported:
        readiness = "needs_features"
    else:
        readiness = "ready"
    kinds = Counter(unsupported_kind(u) for u in unsupported)
    row = InventoryRow(
        file=str(path),
        name=name,
        tableau_version=report.tableau_version,
        grade=report.grade,
        grade_reasons=report.grade_reasons,
        readiness=readiness,
        candidate=_candidate(report, readiness),
        failed_checks=failed,
        warned_checks=warned,
        datasources=_datasource_kinds(report),
        worksheets=len(report.worksheets),
        dashboards=sum(1 for d in report.dashboards if not d.story),
        calc_fields=len(report.formulas),
        lod=sum(report.lod.values()),
        table_calcs=sum(len(w.table_calcs) for w in report.worksheets)
        + sum(1 for f in report.formulas if f.table_calc_functions),
        marks=sorted({m for w in report.worksheets for m in w.mark_types}),
        functions_outside_v0=report.functions_outside_v0,
        unsupported=len(unsupported),
        unsupported_top=[f"{k}（{n}）" for k, n in kinds.most_common(3)],
        error=parse_error,
    )
    return row, report, unsupported


def query_unsupported(ir: IR) -> list[Unsupported]:
    """查詢時才發現的不支援（例如巢狀 Table Calculation）：試產生每張工作表的 SQL（不執行）。"""
    found: list[Unsupported] = []
    for ws in ir.worksheets:
        try:
            worksheet_sql(ir, ws.id, user=INVENTORY_USER)
        except QueryError as exc:
            if exc.unsupported:
                found.append(
                    Unsupported(
                        source_ref=ws.source_ref,
                        reason=f"工作表「{ws.name}」：{exc}",
                        element="worksheet",
                    )
                )
        except Exception:  # 盤點：產生 SQL 的其他問題留給解析與驗證，不中斷批次
            continue
    return found


def _error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# ---------------------------------------------------------------- 功能使用統計


def collect_features(
    key: str,
    report: CensusReport | None,
    unsupported: list[Unsupported],
    row: InventoryRow,
    stats: dict[tuple[str, str], FeatureStat],
) -> None:
    def add(category: str, item: str, count: int = 1) -> None:
        stat = stats.setdefault((category, item), FeatureStat())
        stat.workbooks.add(key)
        stat.occurrences += count

    if row.error is not None and report is None:
        add("讀取失敗", row.error.split(":")[0])
        return
    if row.error is not None:
        add("解析失敗", row.error.split(":")[0])
    for title in row.failed_checks:
        add("起步條件不符", title)
    for kind in row.datasources:
        add("資料源", kind)
    for entry in unsupported:
        add("不支援（解析）", unsupported_kind(entry))
    if report is None:
        return
    for name, count in report.functions.items():
        add("函式", name, count)
    for kind, count in report.lod.items():
        add("LOD", kind, count)
    for formula in report.formulas:
        for fn in formula.table_calc_functions:
            add("Table Calculation 函式", fn)
    for ws in report.worksheets:
        for mark in ws.mark_types:
            add("標記", mark)
        for flt in ws.filters:
            add("篩選", flt.kind + ("（內容）" if flt.context else ""))
        for calc in ws.table_calcs:
            add("快速 Table Calculation", calc.type or "（計算欄位）")
    for db in report.dashboards:
        for zone, count in db.zone_types.items():
            add("dashboard 區塊", zone, count)
    for action in report.actions:
        add("動作", action.kind)
    for feature in features_outside_v0(report):
        add("IR v0 未涵蓋", feature.split("（", 1)[0])


# ---------------------------------------------------------------- 批次


def load_server_meta(path: Path) -> ServerMeta:
    return ServerMeta.model_validate_json(path.read_text(encoding="utf-8"))


def run_inventory(
    paths: Iterable[str | Path],
    out_dir: Path,
    *,
    server_meta: Path | None = None,
    extracts_omitted: bool | None = None,
    progress: Callable[[str], None] | None = None,
) -> InventoryResult:
    meta = load_server_meta(server_meta) if server_meta is not None else None
    by_file: dict[Path, ServerWorkbook] = {}
    missing: list[ServerWorkbook] = []
    if meta is not None:
        assert server_meta is not None
        base = server_meta.parent
        for wb in meta.workbooks:
            if wb.file:
                by_file[(base / wb.file).resolve()] = wb
            else:
                missing.append(wb)
    omitted = (
        extracts_omitted if extracts_omitted is not None else bool(meta and meta.extracts_omitted)
    )

    files = find_workbooks(paths)
    out_dir.mkdir(parents=True, exist_ok=True)
    detail_dir = out_dir / "census"
    detail_dir.mkdir(exist_ok=True)
    rows: list[InventoryRow] = []
    stats: dict[tuple[str, str], FeatureStat] = {}
    used_names: set[str] = set()
    for i, path in enumerate(files, start=1):
        server = by_file.get(path)
        row, report, unsupported = inventory_workbook(
            path,
            extracts_omitted=omitted,
            server_connections=server.connection_details if server is not None else None,
        )
        if server is not None:
            row.name = server.name
            row.project = server.project
            row.owner = server.owner
            row.views = server.views
            row.updated_at = server.updated_at
            row.url = server.url
        row.file = _relative(path, out_dir)
        detail = _detail_name(row, used_names)
        (detail_dir / detail).write_text(
            _detail_markdown(row, report, unsupported), encoding="utf-8", newline="\n"
        )
        row.detail = f"census/{detail}"
        collect_features(str(path), report, unsupported, row, stats)
        rows.append(row)
        if progress is not None:
            grade = f"{row.grade} 級，" if row.grade else ""
            progress(f"[{i}/{len(files)}] {row.name}：{grade}{READINESS_TEXT[row.readiness]}")
    for wb in missing:
        row = InventoryRow(
            file="",
            name=wb.name,
            project=wb.project,
            owner=wb.owner,
            views=wb.views,
            updated_at=wb.updated_at,
            url=wb.url,
            readiness="error",
            error=wb.download_error or "沒有下載",
        )
        collect_features(wb.id, None, [], row, stats)
        rows.append(row)

    source = f"{meta.server}（site：{meta.site or '預設'}）" if meta is not None else None
    result = InventoryResult(rows=rows, features=stats, out_dir=out_dir, source=source)
    write_inventory(result)
    return result


def _relative(path: Path, out_dir: Path) -> str:
    for base in (out_dir.resolve(), Path.cwd()):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return str(path)


def _detail_name(row: InventoryRow, used: set[str]) -> str:
    stem = re.sub(r'[\\/:*?"<>|\s]+', "_", "__".join(p for p in (row.project, row.name) if p))
    stem = stem.strip("._") or "workbook"
    name, n = f"{stem}.md", 2
    while name.lower() in used:
        name, n = f"{stem}_{n}.md", n + 1
    used.add(name.lower())
    return name


def _detail_markdown(
    row: InventoryRow, report: CensusReport | None, unsupported: list[Unsupported]
) -> str:
    lines: list[str] = []
    if report is not None:
        lines.append(render_markdown(report).rstrip("\n"))
    else:
        lines += [f"# 功能盤點：{row.name}", ""]
    lines += ["", "## 解析結果（寬鬆模式）", ""]
    if row.error:
        lines.append(f"- 解析失敗：{row.error}")
    elif not unsupported:
        lines.append("- 沒有不支援的元素")
    else:
        lines.append(f"- 不支援的元素 {len(unsupported)} 筆：")
        lines.append("")
        lines.append("| 原因 | 元素 | 位置 |")
        lines.append("| --- | --- | --- |")
        for entry in unsupported:
            cells = (entry.reason, entry.element or "—", entry.source_ref)
            lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


def _cell(text: object) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|").strip() or "—"


# ---------------------------------------------------------------- 輸出

INVENTORY_COLUMNS: list[tuple[str, Callable[[InventoryRow], object]]] = [
    ("名稱", lambda r: r.name),
    ("Project", lambda r: r.project or ""),
    ("擁有者", lambda r: r.owner or ""),
    ("檢視次數", lambda r: "" if r.views is None else r.views),
    ("起步狀態", lambda r: READINESS_TEXT[r.readiness]),
    ("起步候選", lambda r: r.candidate or ""),
    ("分級", lambda r: r.grade or ""),
    ("分級原因", lambda r: "；".join(r.grade_reasons)),
    ("不符合的起步條件", lambda r: "、".join(r.failed_checks)),
    ("需注意的起步條件", lambda r: "、".join(r.warned_checks)),
    ("不支援數", lambda r: r.unsupported),
    ("主要不支援項目", lambda r: "；".join(r.unsupported_top)),
    ("資料源", lambda r: "、".join(r.datasources)),
    ("工作表", lambda r: r.worksheets),
    ("dashboard", lambda r: r.dashboards),
    ("計算欄位", lambda r: r.calc_fields),
    ("LOD", lambda r: r.lod),
    ("Table Calculation", lambda r: r.table_calcs),
    ("標記類型", lambda r: "、".join(r.marks)),
    ("v0 以外的函式", lambda r: "、".join(r.functions_outside_v0)),
    ("Tableau 版本", lambda r: r.tableau_version or ""),
    ("更新時間", lambda r: r.updated_at or ""),
    ("網址", lambda r: r.url or ""),
    ("檔案", lambda r: r.file),
    ("說明", lambda r: r.detail or ""),
    ("錯誤", lambda r: r.error or ""),
]


def write_inventory(result: InventoryResult) -> None:
    out = result.out_dir
    with (out / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([name for name, _ in INVENTORY_COLUMNS])
        for row in sorted(result.rows, key=_row_order):
            writer.writerow([get(row) for _, get in INVENTORY_COLUMNS])
    with (out / "features.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["類別", "項目", "報表數", "次數"])
        for (category, item), stat in sorted(
            result.features.items(), key=lambda kv: (kv[0][0], -len(kv[1].workbooks), kv[0][1])
        ):
            writer.writerow([category, item, len(stat.workbooks), stat.occurrences])
    (out / "inventory.md").write_text(render_inventory(result), encoding="utf-8", newline="\n")


def _row_order(row: InventoryRow) -> tuple[int, str, int, str]:
    order = {"ready": 0, "needs_features": 1, "not_eligible": 2, "error": 3}[row.readiness]
    return (order, row.candidate or "Z", row.unsupported, row.name)


def _top(
    result: InventoryResult, category: str, limit: int = TOP_FEATURES
) -> list[tuple[str, int, int]]:
    items = [
        (item, len(stat.workbooks), stat.occurrences)
        for (cat, item), stat in result.features.items()
        if cat == category
    ]
    return sorted(items, key=lambda x: (-x[1], -x[2], x[0]))[:limit]


def render_inventory(result: InventoryResult) -> str:
    rows = result.rows
    total = len(rows)
    count = Counter(r.readiness for r in rows)
    grades = Counter(r.grade for r in rows if r.grade)
    eligible = count["ready"] + count["needs_features"]
    now = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = ["# 報表盤點", ""]
    lines.append(f"- 盤點時間：{now}" + (f"；來源：{result.source}" if result.source else ""))
    lines.append(
        f"- 報表：{total} 份" + (f"（讀取失敗 {count['error']} 份）" if count["error"] else "")
    )
    lines.append("- 分級：" + "、".join(f"{g} 級 {grades.get(g, 0)} 份" for g in ("A", "B", "C")))
    lines.append(
        f"- 符合起步條件：{eligible} 份；其中解析後沒有不支援元素、可以直接開始：{count['ready']} 份"
    )
    lines.append("")
    lines.append(
        "起步條件：資料在 .twbx 的萃取（單表或 Relationship 模型）內或為可連線的資料庫（Live）、"
        "沒有 Data Blending、連線不依賴檢視者的身分（檢視者用自己的資料庫帳號、Initial SQL 帶入登入者時"
        "不符合；Windows 整合驗證、Kerberos、OAuth、用到列層級權限、dashboard 不是固定尺寸時列為需注意）。"
        "候選依「不支援數少 → 注意事項少 → 檢視次數多」排序。"
    )

    groups: list[tuple[Literal["A", "B"], str]] = [
        ("A", "A 級候選（一般計算、長條或折線）"),
        ("B", "B 級候選（含 FIXED LOD 或 Table Calculation）"),
    ]
    for kind, title in groups:
        lines += ["", f"## {title}", ""]
        picks = result.candidates(kind)[:TOP_CANDIDATES]
        if not picks:
            lines.append("沒有符合的報表。")
            continue
        lines += [
            "| 報表 | Project | 檢視次數 | 不支援 | 需注意 | 說明 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in picks:
            todo = "；".join(r.unsupported_top) if r.unsupported else "無"
            lines.append(
                "| "
                + " | ".join(
                    _cell(v)
                    for v in (
                        r.name,
                        r.project or "—",
                        "—" if r.views is None else r.views,
                        todo,
                        "、".join(r.warned_checks) or "無",
                        f"[{r.detail}]({r.detail})" if r.detail else "—",
                    )
                )
                + " |"
            )

    sections = [
        ("最常見的不支援項目", "不支援（解析）", "項目"),
        ("起步條件不符合的原因", "起步條件不符", "條件"),
        ("資料源類型", "資料源", "類型"),
        ("IR v0 未涵蓋的功能", "IR v0 未涵蓋", "功能"),
        ("v0 以外的函式", None, "函式"),
    ]
    for heading, category, label in sections:
        if category is None:
            fns = Counter(fn for r in rows for fn in r.functions_outside_v0)
            items = [(fn, n, n) for fn, n in fns.most_common(TOP_FEATURES)]
        else:
            items = _top(result, category)
        lines += ["", f"## {heading}", ""]
        if not items:
            lines.append("沒有。")
            continue
        lines += [f"| {label} | 報表數 | 次數 |", "| --- | --- | --- |"]
        lines += [f"| {_cell(item)} | {n} | {occ} |" for item, n, occ in items]

    failed = [r for r in rows if r.readiness == "error"]
    if failed:
        lines += ["", "## 讀取或解析失敗", "", "| 報表 | 檔案 | 錯誤 |", "| --- | --- | --- |"]
        lines += [
            f"| {_cell(r.name)} | {_cell(r.file or '—')} | {_cell(r.error)} |" for r in failed
        ]
    lines += [
        "",
        "完整清單見 inventory.csv；各功能的使用統計見 features.csv；單份報表的細節見 census/。",
        "",
    ]
    return "\n".join(lines)
