"""tmig verify：每張工作表 × 每組篩選比對一次，寫出固定格式的差異報告（E3）。

fixture 目錄：

    <報表>.twbx          golden.ir.json      cases.yaml      mapping.yaml
    data/<資料源>.duckdb（缺少時自動由 .twbx 匯入）
    expected/<工作表 id>/<篩選組合>.csv
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tmig.data import initial_sql
from tmig.data.executor import ExecutionError
from tmig.data.runtime import DataRuntime
from tmig.data.sources import SourceConnectionError
from tmig.ir import IR, FileConnection, has_local_data, load_ir
from tmig.parser.workbook import parse_workbook
from tmig.planner.dynamic import resolve_parameters
from tmig.planner.state import StateError
from tmig.sqlgen.query import QueryError, worksheet_sql
from tmig.verify.cases import CasesError, CasesFile, case_state, load_cases
from tmig.verify.compare import MAX_DETAILS, Comparison, compare, dimension_aliases
from tmig.verify.mapping import MappingFile, load_mapping

REPORT_VERSION = "1.0"
Status = Literal["pass", "fail", "unsupported"]


@dataclass
class Check:
    check_id: str
    worksheet: str
    view: str
    case: str
    filters: dict[str, Any]
    status: Status
    expected: str
    actual: str
    diff: str
    cause_hint: str = "unknown"
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "view": self.view,
            "worksheet": self.worksheet,
            "case": self.case,
            "filters": self.filters,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "diff": self.diff,
            "cause_hint": self.cause_hint,
            "details": self.details,
        }


@dataclass
class VerifyReport:
    workbook: str
    ir_version: str
    checks: list[Check]
    notes: list[str]

    @property
    def summary(self) -> dict[str, int]:
        counts = {"pass": 0, "fail": 0, "unsupported": 0}
        for check in self.checks:
            counts[check.status] += 1
        return counts

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.status == "pass" for c in self.checks)

    def owner_summary(self, names: dict[str, str]) -> str:
        total = len(self.checks)
        sheets = len({c.worksheet for c in self.checks})
        cases = len({c.case for c in self.checks})
        head = f"{sheets} 張工作表 × {cases} 組篩選，共 {total} 項計算比對"
        failed = [c for c in self.checks if c.status != "pass"]
        if not failed:
            return f"{head}，數字全部與預期結果一致。"
        items = "；".join(
            f"{names.get(c.worksheet, c.worksheet)}（{c.case}）：{c.diff}" for c in failed[:5]
        )
        more = f"，另有 {len(failed) - 5} 項" if len(failed) > 5 else ""
        return f"{head}，{len(failed)} 項不一致或尚不支援：{items}{more}。"

    def to_json(self, ir: IR) -> dict[str, Any]:
        names = {w.id: w.name for w in ir.worksheets}
        return {
            "report_version": REPORT_VERSION,
            "workbook": {
                "id": self.workbook,
                "luid": None,
                "revision": None,
                "ir_version": self.ir_version,
            },
            "stage": "calc",
            "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            "checks": [c.to_json() for c in self.checks],
            "summary": self.summary,
            "owner_summary": self.owner_summary(names),
            "notes": self.notes,
        }


def load_fixture_ir(directory: Path) -> IR:
    golden = directory / "golden.ir.json"
    if golden.is_file():
        return load_ir(golden)
    workbook = next(directory.glob("*.twbx"), None) or next(directory.glob("*.twb"), None)
    if workbook is None:
        raise FileNotFoundError(f"{directory} 沒有 golden.ir.json，也沒有 .twbx 或 .twb")
    return parse_workbook(workbook)


def ensure_data(directory: Path, ir: IR) -> list[str]:
    """萃取的資料源缺少 DuckDB 檔時由 .twbx 匯入；回傳說明訊息（Live 連線不需要）。

    DuckDB 檔若是 tmig extract refresh 從來源更新的（有 <資料源>.refresh.json），不是 .twbx 內的快照，
    在說明中提醒：預期結果須在同一時間點從 Tableau 抓取。
    """
    data = directory / "data"
    extracts = [ds for ds in ir.datasources if has_local_data(ds.connection)]
    notes: list[str] = []
    for ds in extracts:
        refreshed = data / f"{ds.id}.refresh.json"
        if (data / f"{ds.id}.duckdb").is_file() and refreshed.is_file():
            try:
                when = json.loads(refreshed.read_text(encoding="utf-8")).get("finished", "")
            except (OSError, ValueError):
                when = ""
            notes.append(
                f"資料源 {ds.id} 的資料是從來源更新的萃取（{when or '時間不明'}），不是 .twbx 內的快照；"
                "預期結果須在同一時間點從 Tableau 抓取"
            )
    missing = [ds.id for ds in extracts if not (data / f"{ds.id}.duckdb").is_file()]
    if not missing:
        return notes
    from tmig.data.hyper_import import import_sources, import_workbook
    from tmig.package import SOURCES

    twbx = next(directory.glob("*.twbx"), None)
    files = [
        ds.id
        for ds in ir.datasources
        if ds.id in missing and isinstance(ds.connection, FileConnection)
    ]
    if twbx is None and (directory / SOURCES).is_dir() and files == missing:
        # 桌面版建立的檔案型資料源：檔案在報表目錄的 sources/
        results = import_sources(directory / SOURCES, data, ir, only=missing)
        bad = [r.datasource for r in results if not r.ok]
        if bad:
            raise ExecutionError(f"檔案匯入自檢失敗：{'、'.join(bad)}；見 data/*.import.json")
        return [*notes, f"已由報表中的檔案匯入 {'、'.join(missing)}"]
    if twbx is None:
        raise FileNotFoundError(
            f"{directory} 缺少萃取的資料（DuckDB 檔），也沒有 .twbx 可以匯入；"
            ".twb 不含萃取的資料，請改用 .twbx"
        )
    results = import_workbook(twbx, data, ir)
    bad = [r.datasource for r in results if not r.ok]
    if bad:
        raise ExecutionError(f"萃取匯入自檢失敗：{'、'.join(bad)}；見 data/*.import.json")
    return [*notes, f"已由 {twbx.name} 匯入 {'、'.join(missing)}"]


def verify_fixture(
    directory: str | Path, runtime: DataRuntime | None = None
) -> tuple[IR, VerifyReport]:
    """runtime：Live 連線的執行環境；省略時依連線設定檔（TMIG_CONNECTIONS）建立，比對完關閉。"""
    root = Path(directory)
    ir = load_fixture_ir(root)
    cases = load_cases(root / "cases.yaml")
    mapping = load_mapping(root / "mapping.yaml")
    notes = ensure_data(root, ir)
    if not mapping.confirmed:
        notes.append("mapping.yaml 尚未人工確認（confirmed: false）")
    if cases.fetched_at is None:
        notes.append("cases.yaml 沒有 fetched_at：預期結果不是由 fetch-expected 從 Tableau 抓取")
    owned = runtime is None
    active = runtime or DataRuntime()
    # 連線的 Initial SQL 以 cases.yaml 的登入者代入（與檢視時相同）
    session = initial_sql.server_session(
        cases.user.username if cases.user else "",
        cases.user.domain if cases.user else None,
        ir.workbook.name,
    )
    try:
        with initial_sql.use(session):
            # 動態參數：與 Tableau 開啟報表時相同，以目前的資料取值
            ir, dynamic_notes = resolve_parameters(
                ir,
                lambda query, ds: active.run(query, ds, root / "data" / f"{ds.id}.duckdb"),
                cases.user,
            )
            notes.extend(dynamic_notes)
            checks = [
                _check(root, ir, cases, mapping, sheet.worksheet, sheet.view, case.name, active)
                for sheet in cases.sheets
                for case in cases.cases
            ]
    finally:
        if owned:
            active.close()
    return ir, VerifyReport(
        workbook=ir.workbook.id, ir_version=ir.ir_version, checks=checks, notes=notes
    )


def _check(
    root: Path,
    ir: IR,
    cases: CasesFile,
    mapping: MappingFile,
    worksheet: str,
    view: str,
    case_name: str,
    runtime: DataRuntime,
) -> Check:
    case = cases.case(case_name)
    base = Check(
        check_id=f"{worksheet}:{case_name}",
        worksheet=worksheet,
        view=view,
        case=case_name,
        filters=dict(case.url_params),
        status="fail",
        expected="",
        actual="",
        diff="",
    )
    expected_path = root / "expected" / worksheet / f"{case_name}.csv"
    if not expected_path.is_file():
        base.diff = f"缺少預期結果 {expected_path.relative_to(root)}"
        return base
    sheet_mapping = mapping.sheets.get(worksheet)
    if sheet_mapping is None:
        base.diff = f"mapping.yaml 沒有工作表 {worksheet}"
        return base
    try:
        query = worksheet_sql(ir, worksheet, case_state(ir, case, cases.as_of()), cases.user)
        ws = next(w for w in ir.worksheets if w.id == worksheet)
        datasource = next(d for d in ir.datasources if d.id == ws.datasource)
        result = runtime.run(query, datasource, root / "data" / f"{ws.datasource}.duckdb")
    except QueryError as exc:
        base.status = "unsupported" if exc.unsupported else "fail"
        base.diff = str(exc)
        return base
    except (StateError, CasesError) as exc:
        base.diff = str(exc)
        return base
    except (ExecutionError, SourceConnectionError) as exc:
        base.diff = str(exc)
        return base
    comparison = compare(
        expected_path.read_text(encoding="utf-8-sig"),
        result,
        query.columns,
        sheet_mapping,
        dimension_aliases(ir, query.columns),
    )
    return _fill(base, comparison)


def _fill(check: Check, comparison: Comparison) -> Check:
    check.expected = f"{comparison.expected_rows} 列"
    check.actual = f"{comparison.actual_rows} 列"
    if comparison.ok:
        check.status = "pass"
        check.diff = "一致" + (
            f"（{'；'.join(comparison.tolerance)}）" if comparison.tolerance else ""
        )
        return check
    parts: list[str] = []
    if comparison.errors:
        parts += comparison.errors
    if comparison.expected_rows != comparison.actual_rows:
        parts.append(f"列數不同：預期 {comparison.expected_rows}，實際 {comparison.actual_rows}")
    if comparison.missing_rows:
        parts.append(f"缺少 {len(comparison.missing_rows)} 列")
    if comparison.extra_rows:
        parts.append(f"多出 {len(comparison.extra_rows)} 列")
    if comparison.cells:
        columns = sorted({c.column for c in comparison.cells})
        parts.append(f"{len(comparison.cells)} 格數值不同（{'、'.join(columns)}）")
    check.diff = "；".join(parts)
    details: list[dict[str, Any]] = []
    details += [{"kind": "missing_row", "row": _jsonable(r)} for r in comparison.missing_rows]
    details += [{"kind": "extra_row", "row": _jsonable(r)} for r in comparison.extra_rows]
    details += [
        {
            "kind": "cell",
            "row": _jsonable(c.row),
            "column": c.column,
            "expected": c.expected,
            "actual": c.actual,
            "note": c.note,
        }
        for c in comparison.cells
    ]
    check.details = details[:MAX_DETAILS]
    return check


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
        for k, v in row.items()
    }


def write_report(ir: IR, report: VerifyReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_json(ir), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
