"""tmig 命令列。

tmig census <.twb／.twbx>... [--out-dir 目錄]   功能盤點與起步條件檢查
tmig parse <.twbx> [--out IR 檔]                解析成 IR，列出不支援的元素
tmig import-hyper <.twbx> [--out-dir 目錄]      萃取匯入 DuckDB 並自檢
tmig sql <IR 檔> <工作表> [--case 名稱]          產生的 SQL（除錯用）
tmig query <IR 檔> <工作表> [--case 名稱]        執行查詢，輸出 CSV
tmig mapping suggest <fixture 目錄>              產生 CSV 欄名對照建議
tmig verify <fixture 目錄> [--out 報告]          計算比對，寫出差異報告
tmig serve [--fixtures 目錄] [--port 8000]       啟動查詢服務與檢視器
tmig ir schema [--out 檔案]                     輸出 IR 的 JSON Schema
tmig ir validate <IR 檔>...                     驗證 IR JSON 檔
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from tmig import __version__
from tmig.census import render_markdown, run_census
from tmig.ir import dumps_ir, ir_json_schema_text, load_ir
from tmig.parser.twbx import WorkbookFormatError
from tmig.parser.workbook import WorkbookParseError, parse_workbook

EXIT_UNSUPPORTED = 3
"""tmig parse 成功產生 IR，但有不支援的元素。"""


def _cmd_census(args: argparse.Namespace) -> int:
    failed = 0
    for path in args.paths:
        try:
            report = run_census(path)
        except WorkbookFormatError as exc:
            print(f"失敗  {path}：{exc}", file=sys.stderr)
            failed += 1
            continue
        markdown = render_markdown(report)
        if args.out_dir is None:
            sys.stdout.write(markdown)
            continue
        args.out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(path).stem
        md_path = args.out_dir / f"{stem}.census.md"
        json_path = args.out_dir / f"{stem}.census.json"
        md_path.write_text(markdown, encoding="utf-8", newline="\n")
        json_path.write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        failed_checks = [c.title for c in report.checks if c.status == "fail"]
        summary = f"不符合：{'、'.join(failed_checks)}" if failed_checks else "起步條件符合"
        print(f"{path}：{report.grade} 級，{summary} → {md_path}")
    return 1 if failed else 0


def _cmd_parse(args: argparse.Namespace) -> int:
    try:
        ir = parse_workbook(args.path, lenient=args.lenient)
    except (WorkbookFormatError, WorkbookParseError) as exc:
        print(f"失敗  {args.path}：{exc}", file=sys.stderr)
        return 1
    text = dumps_ir(ir)
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"解析完成：{len(ir.datasources)} 個資料源、{len(ir.worksheets)} 張工作表、"
        f"{len(ir.dashboards)} 個 dashboard、{len(ir.parameters)} 個參數"
        + (f" → {args.out}" if args.out is not None else ""),
        file=sys.stderr,
    )
    if ir.ignored:
        print(f"略過 {len(ir.ignored)} 筆不影響數字的元素（見 IR 的 ignored）", file=sys.stderr)
    if not ir.unsupported:
        print("unsupported 為空，可以往下移轉", file=sys.stderr)
        return 0
    print(f"不支援 {len(ir.unsupported)} 筆，報表需人工處理：", file=sys.stderr)
    for item in ir.unsupported:
        print(f"  - {item.reason}（{item.source_ref}）", file=sys.stderr)
    return EXIT_UNSUPPORTED


def _cmd_import_hyper(args: argparse.Namespace) -> int:
    from tmig.data.hyper_import import HyperImportError, import_workbook

    out_dir = args.out_dir or args.path.parent / "data"
    try:
        results = import_workbook(args.path, out_dir)
    except (WorkbookFormatError, WorkbookParseError, HyperImportError) as exc:
        print(f"失敗  {args.path}：{exc}", file=sys.stderr)
        return 1
    failed = 0
    for result in results:
        table = result.table
        checks = table.checks
        bad = [c for c in checks if not c.ok]
        status = "通過" if result.ok else "失敗"
        print(
            f"{status}  {result.datasource}：{table.schema}.{table.table} {table.rows} 列、"
            f"{len(table.columns)} 欄，自檢 {len(checks)} 項"
            + (f"有 {len(bad)} 項不一致" if bad else "全部一致")
            + f" → {out_dir / result.duckdb}"
        )
        for check in bad:
            print(
                f"      {check.column or '(表)'} 的 {check.stat}：Hyper {check.hyper}，DuckDB {check.duckdb}",
                file=sys.stderr,
            )
        for name in result.missing_columns:
            print(f"      IR 欄位 {name} 不在萃取資料表中", file=sys.stderr)
        for warning in table.warnings:
            print(f"      注意：{warning}", file=sys.stderr)
        failed += 0 if result.ok else 1
    return 1 if failed else 0


def _query_inputs(args: argparse.Namespace):
    """讀 IR，並把 --case 轉成篩選狀態。"""
    from tmig.planner.state import QueryState
    from tmig.verify.cases import case_state, load_cases

    ir = load_ir(args.ir)
    state = QueryState()
    if args.case:
        cases_path = args.cases or args.ir.parent / "cases.yaml"
        state = case_state(ir, load_cases(cases_path).case(args.case))
    return ir, state


def _cmd_sql(args: argparse.Namespace) -> int:
    from tmig.planner.state import StateError
    from tmig.sqlgen.query import QueryError, worksheet_sql
    from tmig.verify.cases import CasesError

    try:
        ir, state = _query_inputs(args)
        query = worksheet_sql(ir, args.worksheet, state)
    except (FileNotFoundError, ValidationError, CasesError, QueryError, StateError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    sys.stdout.write(query.sql)
    if query.state.skipped:
        print(f"-- 不適用這張工作表的篩選：{'、'.join(query.state.skipped)}", file=sys.stderr)
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    from tmig.data.executor import ExecutionError, execute
    from tmig.planner.state import StateError
    from tmig.sqlgen.query import QueryError, worksheet_sql
    from tmig.verify.cases import CasesError
    from tmig.verify.run import ensure_data

    try:
        ir, state = _query_inputs(args)
        query = worksheet_sql(ir, args.worksheet, state)
        ws = next(w for w in ir.worksheets if w.id == query.worksheet)
        db = args.db
        if db is None:
            # 與 tmig verify 相同：缺 DuckDB 檔時由同目錄的 .twbx 匯入
            for note in ensure_data(args.ir.parent, ir):
                print(note, file=sys.stderr)
            db = args.ir.parent / "data" / f"{ws.datasource}.duckdb"
        result = execute(db, query.sql)
    except (
        FileNotFoundError,
        ValidationError,
        CasesError,
        QueryError,
        StateError,
        ExecutionError,
    ) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    text = result.to_csv()
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"{len(result.rows)} 列 → {args.out}", file=sys.stderr)
    return 0


def _cmd_mapping_suggest(args: argparse.Namespace) -> int:
    from tmig.sqlgen.query import QueryError, worksheet_sql
    from tmig.verify.cases import CasesError, load_cases
    from tmig.verify.mapping import MappingFile, csv_headers, dump_mapping, suggest_sheet
    from tmig.verify.run import load_fixture_ir

    root: Path = args.fixture
    target = root / "mapping.yaml"
    if target.exists() and not args.force:
        print(f"失敗：{target} 已存在；確定要覆蓋請加 --force", file=sys.stderr)
        return 1
    try:
        ir = load_fixture_ir(root)
        cases = load_cases(root / "cases.yaml")
        mapping = MappingFile()
        notes: dict[str, list[str]] = {}
        for sheet in cases.sheets:
            query = worksheet_sql(ir, sheet.worksheet)
            files = sorted((root / "expected" / sheet.worksheet).glob("*.csv"))
            headers = csv_headers(files[0]) if files else None
            sheet_mapping, unmatched = suggest_sheet(ir, sheet.worksheet, query.columns, headers)
            mapping.sheets[sheet.worksheet] = sheet_mapping
            sheet_notes = notes.setdefault(sheet.worksheet, [])
            if headers is None:
                sheet_notes.append("還沒有預期 CSV，左邊的欄名是推測值；抓取後重新產生")
            if unmatched:
                sheet_notes.append(f"無法自動對應：{'、'.join(unmatched)}；請填入欄位鍵或保留 null")
            if sheet_mapping.ignore:
                sheet_notes.append(
                    f"查詢結果中找不到 CSV 欄位的：{'、'.join(sheet_mapping.ignore)}"
                )
            status = "需人工處理" if unmatched or sheet_mapping.ignore else "全部對應"
            print(f"{sheet.worksheet}：{len(sheet_mapping.columns)} 欄，{status}")
    except (FileNotFoundError, ValidationError, CasesError, QueryError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    target.write_text(dump_mapping(mapping, notes), encoding="utf-8", newline="\n")
    print(f"已寫入 {target}；人工確認後把 confirmed 改成 true")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from tmig.data.executor import ExecutionError
    from tmig.data.hyper_import import HyperImportError
    from tmig.verify.cases import CasesError
    from tmig.verify.mapping import MappingError
    from tmig.verify.run import verify_fixture, write_report

    root: Path = args.fixture
    try:
        ir, report = verify_fixture(root)
    except (
        FileNotFoundError,
        ValidationError,
        CasesError,
        MappingError,
        ExecutionError,
        HyperImportError,
    ) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    out = args.out or root / "report" / "calc.json"
    write_report(ir, report, out)
    names = {w.id: w.name for w in ir.worksheets}
    width = max((len(c.worksheet) for c in report.checks), default=0)
    for check in report.checks:
        mark = {"pass": "通過", "fail": "失敗", "unsupported": "不支援"}[check.status]
        print(f"{mark}  {check.worksheet:<{width}}  {check.case:<16} {check.diff}")
    for note in report.notes:
        print(f"注意：{note}")
    summary = report.summary
    print(
        f"共 {len(report.checks)} 項：通過 {summary['pass']}、失敗 {summary['fail']}、"
        f"不支援 {summary['unsupported']} → {out}"
    )
    print(report.owner_summary(names))
    return 0 if report.ok else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        from tmig_service import serve
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    print(f"查詢服務：http://{args.host}:{args.port}（讀取 {args.fixtures}）")
    serve(args.fixtures, args.host, args.port, args.web)
    return 0


def _cmd_ir_schema(args: argparse.Namespace) -> int:
    text = ir_json_schema_text()
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"已寫入 {args.out}")
    return 0


def _cmd_ir_validate(args: argparse.Namespace) -> int:
    failed = 0
    for path in args.paths:
        try:
            ir = load_ir(path)
        except FileNotFoundError:
            print(f"失敗  {path}：找不到檔案", file=sys.stderr)
            failed += 1
        except ValidationError as exc:
            print(f"失敗  {path}：{exc.error_count()} 個錯誤", file=sys.stderr)
            for err in exc.errors():
                where = ".".join(str(part) for part in err["loc"]) or "(根)"
                print(f"      {where}：{err['msg']}", file=sys.stderr)
            failed += 1
        else:
            note = f"，{len(ir.unsupported)} 筆 unsupported" if ir.unsupported else ""
            print(
                f"通過  {path}（{len(ir.worksheets)} 張工作表、{len(ir.dashboards)} 個 dashboard{note}）"
            )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmig", description="Tableau 移轉工具")
    parser.add_argument("--version", action="version", version=f"tmig {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="指令")

    census = commands.add_parser("census", help="功能盤點：列出報表用到的功能與起步條件")
    census.add_argument("paths", nargs="+", type=Path, metavar=".twb／.twbx")
    census.add_argument(
        "--out-dir",
        type=Path,
        help="寫入 <檔名>.census.md 與 .census.json；省略時把 Markdown 印到標準輸出",
    )
    census.set_defaults(handler=_cmd_census)

    parse = commands.add_parser("parse", help="解析 .twbx 成 IR，列出不支援的元素")
    parse.add_argument("path", type=Path, metavar=".twbx")
    parse.add_argument("--out", type=Path, help="寫入 IR 檔；省略時印到標準輸出")
    parse.add_argument(
        "--lenient",
        action="store_true",
        help="資料源不是嵌入式萃取時仍解析欄位與工作表（盤點用，產生的 IR 不能查詢）",
    )
    parse.set_defaults(handler=_cmd_parse)

    imp = commands.add_parser("import-hyper", help="把 .twbx 內的萃取匯入 DuckDB 並自檢")
    imp.add_argument("path", type=Path, metavar=".twbx")
    imp.add_argument(
        "--out-dir", type=Path, help="DuckDB 檔的目錄；預設為 .twbx 所在目錄下的 data/"
    )
    imp.set_defaults(handler=_cmd_import_hyper)

    for name, handler, help_text in (
        ("sql", _cmd_sql, "產生工作表的 SQL（除錯用）"),
        ("query", _cmd_query, "執行工作表查詢，輸出 CSV"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("ir", type=Path, metavar="IR 檔")
        sub.add_argument("worksheet", metavar="工作表", help="工作表 id 或名稱")
        sub.add_argument("--case", help="cases.yaml 中的篩選組合名稱；省略時用工作表的預設篩選")
        sub.add_argument("--cases", type=Path, help="cases.yaml 路徑；預設為 IR 檔所在目錄")
        if name == "query":
            sub.add_argument(
                "--db",
                type=Path,
                help="DuckDB 檔；預設為 IR 檔所在目錄下的 data/<資料源 id>.duckdb",
            )
            sub.add_argument("--out", type=Path, help="寫入 CSV 檔；省略時印到標準輸出")
        sub.set_defaults(handler=handler)

    mapping_parser = commands.add_parser("mapping", help="預期 CSV 欄名對照")
    mapping_commands = mapping_parser.add_subparsers(
        dest="mapping_command", required=True, metavar="子指令"
    )
    suggest = mapping_commands.add_parser("suggest", help="依 IR 與預期 CSV 產生 mapping.yaml 建議")
    suggest.add_argument("fixture", type=Path, metavar="fixture 目錄")
    suggest.add_argument("--force", action="store_true", help="覆蓋既有的 mapping.yaml")
    suggest.set_defaults(handler=_cmd_mapping_suggest)

    verify = commands.add_parser("verify", help="計算比對：每張工作表 × 每組篩選，寫出差異報告")
    verify.add_argument("fixture", type=Path, metavar="fixture 目錄")
    verify.add_argument("--out", type=Path, help="差異報告路徑；預設為 <fixture>/report/calc.json")
    verify.set_defaults(handler=_cmd_verify)

    serve = commands.add_parser("serve", help="啟動查詢服務與檢視器")
    serve.add_argument(
        "--fixtures", type=Path, default=Path("fixtures"), help="報表目錄（預設 fixtures）"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--web", type=Path, help="檢視器的建置目錄；預設為 web/dist")
    serve.set_defaults(handler=_cmd_serve)

    ir_parser = commands.add_parser("ir", help="IR 工具")
    ir_commands = ir_parser.add_subparsers(dest="ir_command", required=True, metavar="子指令")
    schema = ir_commands.add_parser("schema", help="輸出 IR 的 JSON Schema")
    schema.add_argument("--out", type=Path, help="寫入檔案；省略時印到標準輸出")
    schema.set_defaults(handler=_cmd_ir_schema)
    validate = ir_commands.add_parser("validate", help="驗證 IR JSON 檔")
    validate.add_argument("paths", nargs="+", type=Path, metavar="IR 檔")
    validate.set_defaults(handler=_cmd_ir_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 主控台的編碼可能無法顯示所有字元，改以替代字元輸出而不是中斷
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    return args.handler(args)


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
