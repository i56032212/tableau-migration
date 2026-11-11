"""tmig 命令列。

tmig census <.twb／.twbx>... [--out-dir 目錄]   功能盤點與起步條件檢查
tmig inventory <資料夾>... [--out-dir 目錄]     批次盤點：總表、功能使用統計與起步候選
tmig parse <.twbx> [--out IR 檔]                解析成 IR，列出不支援的元素
tmig import-hyper <.twbx> [--out-dir 目錄]      萃取匯入 DuckDB 並自檢
tmig sql <IR 檔> <工作表> [--case 名稱] [--user 帳號]    產生的 SQL（除錯用）
tmig query <IR 檔> <工作表> [--case 名稱] [--user 帳號]  執行查詢，輸出 CSV
tmig cases suggest <fixture 目錄> [--write]      依資料建議 3 組篩選組合
tmig mapping suggest <fixture 目錄>              產生 CSV 欄名對照建議
tmig verify <fixture 目錄> [--out 報告]          計算比對，寫出差異報告
tmig connections template <報表或資料夾>...      產生 Live 連線的設定檔範本（憑證寫成環境變數）
tmig connections check <報表或資料夾>...         檢查 Live 連線的設定並測試連線
tmig extract refresh <報表> [--incremental]       從來源資料庫更新萃取的 DuckDB 檔（完整或增量）
tmig extract compare <.twbx> [--data-dir 目錄]    新萃取與 Tableau 萃取比對（列數、總和、最小值、最大值）
tmig published merge <報表> [--published 目錄]     把 Published Data Source 的 .tdsx 併入報表
tmig schedule list|tick|run|serve <排程檔>         排程器：萃取更新與訂閱（Q2、Q3）
tmig schedule list|tick|run|serve --store <報表庫>  排程器：報表庫中（站台管理的）排程、萃取更新與訂閱
tmig schedule import <排程檔> --store <報表庫>      把 Tableau 匯出的排程檔匯入報表庫（之後在站台管理中修改）
tmig usage summary <使用紀錄目錄> [--days 90]      每份報表的檢視次數、使用者數與最後一次檢視（R1）
tmig site import|init|show --store <報表庫>         站台資料（Project、使用者、群組與權限）：從 Tableau 匯入或新建
tmig serve [--fixtures 目錄] [--port 8000] [--auth 設定檔]  啟動查詢服務與檢視器
tmig desktop serve [--workspace 目錄] [--port 0]   桌面版的本機引擎（由桌面版外殼啟動）
tmig ir schema [--out 檔案]                     輸出 IR 的 JSON Schema
tmig ir validate <IR 檔>...                     驗證 IR JSON 檔
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tmig import __version__
from tmig.census import render_markdown, run_census
from tmig.ir import IR, dumps_ir, has_local_data, ir_json_schema_text, load_ir
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


def _cmd_inventory(args: argparse.Namespace) -> int:
    from tmig.census.inventory import READINESS_TEXT, run_inventory

    paths: list[Path] = list(args.paths)
    if not paths:
        if args.server_meta is None:
            print(
                "失敗：請給 .twb／.twbx 或資料夾，或以 --server-meta 指定 server.json",
                file=sys.stderr,
            )
            return 1
        paths = [args.server_meta.parent]
    try:
        result = run_inventory(
            paths,
            args.out_dir,
            server_meta=args.server_meta,
            extracts_omitted=True if args.extracts_omitted else None,
            progress=lambda message: print(message, file=sys.stderr),
        )
    except (FileNotFoundError, ValidationError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    if not result.rows:
        print("失敗：沒有找到 .twb／.twbx", file=sys.stderr)
        return 1
    counts = Counter(row.readiness for row in result.rows)
    summary = "、".join(
        f"{text} {counts[key]}" for key, text in READINESS_TEXT.items() if counts[key]
    )
    a, b = len(result.candidates("A")), len(result.candidates("B"))
    print(f"{len(result.rows)} 份報表：{summary}；起步候選 A 級 {a}、B 級 {b}")
    print(f"→ {args.out_dir / 'inventory.md'}")
    return 0


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
        status = "通過" if result.ok else "失敗"
        for i, table in enumerate(result.tables):
            checks = table.checks
            bad = [c for c in checks if not c.ok]
            print(
                (f"{status}  {result.datasource}：" if i == 0 else "      ")
                + f"{table.schema}.{table.table} {table.rows} 列、"
                f"{len(table.columns)} 欄，自檢 {len(checks)} 項"
                + (f"有 {len(bad)} 項不一致" if bad else "全部一致")
                + (f" → {out_dir / result.duckdb}" if i == len(result.tables) - 1 else "")
            )
            for check in bad:
                print(
                    f"      {check.column or '(表)'} 的 {check.stat}：Hyper {check.hyper}，DuckDB {check.duckdb}",
                    file=sys.stderr,
                )
            for warning in table.warnings:
                print(f"      注意：{warning}", file=sys.stderr)
        for name in result.missing_columns:
            print(f"      IR 欄位 {name} 不在萃取資料表中", file=sys.stderr)
        failed += 0 if result.ok else 1
    return 1 if failed else 0


def _query_inputs(args: argparse.Namespace):
    """讀 IR，並把 --case 轉成篩選狀態；登入者的身分取自 cases.yaml 的 user，或 --user、--group。"""
    from tmig.planner.identity import Identity
    from tmig.planner.state import QueryState
    from tmig.verify.cases import case_state, load_cases

    ir = load_ir(args.ir)
    state = QueryState()
    user: Identity | None = None
    cases_path = args.cases or args.ir.parent / "cases.yaml"
    if args.case:
        spec = load_cases(cases_path)
        state = case_state(ir, spec.case(args.case), spec.as_of())
        user = spec.user
    elif cases_path.is_file():
        # 沒有指定篩選組合時，相對日期篩選仍以 cases.yaml 的基準日計算
        spec = load_cases(cases_path)
        state = QueryState(today=spec.as_of())
        user = spec.user
    if args.user:
        user = Identity(username=args.user, groups=tuple(args.group or ()))
    elif args.group:
        if user is None:
            raise ValueError("--group 需要搭配 --user（或 cases.yaml 的 user）")
        user = user.model_copy(update={"groups": tuple(args.group)})
    return ir, state, user


def _cmd_sql(args: argparse.Namespace) -> int:
    from tmig.planner.state import StateError
    from tmig.sqlgen.query import QueryError, worksheet_sql
    from tmig.verify.cases import CasesError

    try:
        ir, state, user = _query_inputs(args)
        query = worksheet_sql(ir, args.worksheet, state, user)
    except (
        FileNotFoundError,
        ValueError,
        ValidationError,
        CasesError,
        QueryError,
        StateError,
    ) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    sys.stdout.write(query.sql)
    if query.state.skipped:
        print(f"-- 不適用這張工作表的篩選：{'、'.join(query.state.skipped)}", file=sys.stderr)
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    from tmig.data.executor import ExecutionError
    from tmig.data.runtime import DataRuntime
    from tmig.data.sources import SourceConnectionError
    from tmig.planner.state import StateError
    from tmig.sqlgen.query import QueryError, worksheet_sql
    from tmig.verify.cases import CasesError
    from tmig.verify.run import ensure_data

    try:
        ir, state, user = _query_inputs(args)
        query = worksheet_sql(ir, args.worksheet, state, user)
        ws = next(w for w in ir.worksheets if w.id == query.worksheet)
        datasource = next(d for d in ir.datasources if d.id == ws.datasource)

        def database() -> Path:
            if args.db is not None:
                return args.db
            # 與 tmig verify 相同：缺 DuckDB 檔時由同目錄的 .twbx 匯入
            for note in ensure_data(args.ir.parent, ir):
                print(note, file=sys.stderr)
            return args.ir.parent / "data" / f"{ws.datasource}.duckdb"

        # Live 連線依連線設定檔（TMIG_CONNECTIONS）連到來源資料庫
        with DataRuntime() as runtime:
            result = runtime.run(query, datasource, database)
    except (
        FileNotFoundError,
        ValueError,
        ValidationError,
        CasesError,
        QueryError,
        StateError,
        ExecutionError,
        SourceConnectionError,
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
            query = worksheet_sql(ir, sheet.worksheet, user=cases.user)
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


def _cmd_cases_suggest(args: argparse.Namespace) -> int:
    from tmig.data.executor import ExecutionError
    from tmig.verify.cases import CasesError
    from tmig.verify.suggest import append_cases, render_cases, suggest_cases

    try:
        cases, notes = suggest_cases(args.fixture)
        for note in notes:
            print(note, file=sys.stderr)
        if not cases:
            print("沒有可建議的篩選組合", file=sys.stderr)
            return 1
        if not args.write:
            sys.stdout.write(render_cases(cases))
            print("確認後加 --write 寫入 cases.yaml", file=sys.stderr)
            return 0
        written = append_cases(args.fixture / "cases.yaml", cases)
    except (FileNotFoundError, CasesError, ExecutionError, ValidationError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    if written:
        print(f"已加入 {'、'.join(written)} → {args.fixture / 'cases.yaml'}")
    else:
        print("cases.yaml 已有同名的篩選組合，沒有變更")
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


def _connections_found(args: argparse.Namespace):
    from tmig.data.connection_check import collect, load_workbook, workbook_paths

    irs = []
    for path in workbook_paths(args.paths):
        try:
            irs.append(load_workbook(path))
        except (WorkbookFormatError, WorkbookParseError, ValidationError, OSError) as exc:
            print(f"略過  {path}：{exc}", file=sys.stderr)
    return collect(irs)


def _cmd_connections_template(args: argparse.Namespace) -> int:
    from tmig.data.connection_check import template

    found = _connections_found(args)
    if not found:
        print("沒有找到 Live 連線", file=sys.stderr)
        return 1
    text = template(found)
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"{len(found)} 個連線 → {args.out}", file=sys.stderr)
    return 0


def _cmd_connections_check(args: argparse.Namespace) -> int:
    from tmig.data.connection_check import check
    from tmig.data.sources import SourceConnectionError, config_path, load_connections

    found = _connections_found(args)
    if not found:
        print("沒有找到 Live 連線", file=sys.stderr)
        return 1
    try:
        configs = load_connections(args.config)
    except SourceConnectionError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    path = config_path(args.config)
    print(f"連線設定檔：{path}" + ("" if path.is_file() else "（不存在）"))
    statuses = check(found, configs, connect=not args.no_connect)
    for status in statuses:
        connection = status.found.connection
        mark = "通過" if status.ok else "失敗"
        where = "／".join(p for p in (connection.server, connection.database) if p)
        print(
            f"{mark}  {connection.connection_id}（{connection.dbclass}，{where or '未標示主機'}）：{status.message}"
        )
        print(f"      報表：{'、'.join(status.found.workbooks)}")
    bad = sum(1 for s in statuses if not s.ok)
    print(f"共 {len(statuses)} 個連線：通過 {len(statuses) - bad}、失敗 {bad}")
    return 1 if bad else 0


def _extract_inputs(path: Path) -> tuple[IR, Path]:
    """萃取更新的輸入：.twb／.twbx、IR 檔或 fixture 目錄 → （IR, 預設的資料目錄）。"""
    from tmig.verify.run import load_fixture_ir

    if path.is_dir():
        return load_fixture_ir(path), path / "data"
    if path.name.endswith(".json"):
        return load_ir(path), path.parent / "data"
    return parse_workbook(path), path.parent / "data"


def _cmd_extract_refresh(args: argparse.Namespace) -> int:
    from tmig.data.extract_refresh import RefreshError, refresh_extract, refresh_plan
    from tmig.data.sources import SourceConnectionError, load_connections, open_executor

    try:
        ir, default_dir = _extract_inputs(args.path)
    except (WorkbookFormatError, WorkbookParseError, ValidationError, OSError) as exc:
        print(f"失敗  {args.path}：{exc}", file=sys.stderr)
        return 1
    out_dir = args.out_dir or default_dir
    targets = [
        ds
        for ds in ir.datasources
        if has_local_data(ds.connection) and (args.datasource is None or ds.id == args.datasource)
    ]
    if not targets:
        print("沒有萃取或檔案型的資料源", file=sys.stderr)
        return 1
    try:
        configs = load_connections(args.config)
    except SourceConnectionError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    failed = 0
    for ds in targets:
        out_path = out_dir / f"{ds.id}.duckdb"
        try:
            plan = refresh_plan(ir, ds.id)
            executor = open_executor(plan.source, configs)
            try:
                result = refresh_extract(
                    ir, ds.id, executor, out_path, incremental=args.incremental
                )
            finally:
                executor.close()
        except (RefreshError, SourceConnectionError) as exc:
            print(f"失敗  {ds.id}：{exc}", file=sys.stderr)
            failed += 1
            continue
        tables = "、".join(
            f"{t.schema}.{t.table} {t.rows} 列"
            + (f"（新增 {t.added}）" if result.mode == "incremental" else "")
            for t in result.tables
        )
        mode = "增量更新" if result.mode == "incremental" else "完整更新"
        print(f"通過  {ds.id}：{mode}，{tables} → {out_path}")
    return 1 if failed else 0


def _cmd_extract_compare(args: argparse.Namespace) -> int:
    import json
    import tempfile

    from tmig.data.extract_refresh import RefreshError, compare_extract
    from tmig.data.hyper_import import HyperImportError, import_workbook

    if args.path.suffix.lower() != ".twbx":
        print("失敗：資料比對需要 .twbx（內含 Tableau 的萃取 .hyper）", file=sys.stderr)
        return 1
    try:
        ir = parse_workbook(args.path)
    except (WorkbookFormatError, WorkbookParseError) as exc:
        print(f"失敗  {args.path}：{exc}", file=sys.stderr)
        return 1
    data_dir = args.data_dir or args.path.parent / "data"
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        try:
            imports = import_workbook(args.path, Path(tmp), ir)
        except HyperImportError as exc:
            print(f"失敗  {args.path}：{exc}", file=sys.stderr)
            return 1
        for item in imports:
            if args.datasource is not None and item.datasource != args.datasource:
                continue
            refreshed = data_dir / f"{item.datasource}.duckdb"
            if not refreshed.is_file():
                print(
                    f"失敗  {item.datasource}：找不到 {refreshed}（請先 tmig extract refresh）",
                    file=sys.stderr,
                )
                failed += 1
                continue
            try:
                result = compare_extract(ir, item.datasource, refreshed, Path(tmp) / item.duckdb)
            except RefreshError as exc:
                print(f"失敗  {item.datasource}：{exc}", file=sys.stderr)
                failed += 1
                continue
            report = data_dir / f"{item.datasource}.compare.json"
            report.write_text(
                json.dumps(result.to_json(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            for table in result.tables:
                bad = [c for c in table.checks if not c.ok]
                status = "一致" if not bad else f"有 {len(bad)} 項不一致"
                print(
                    f"{'通過' if not bad else '失敗'}  {item.datasource}：{table.schema}.{table.table}"
                    f" 比對 {len(table.checks)} 項{status}"
                )
                for check in bad:
                    print(
                        f"      {check.column or '(表)'} 的 {check.stat}：Tableau {check.reference}，"
                        f"新萃取 {check.refreshed}",
                        file=sys.stderr,
                    )
                if table.missing:
                    print(f"      注意：新萃取沒有 {'、'.join(table.missing)}（IR 沒有用到）")
            failed += 0 if result.ok else 1
            print(f"      報告 → {report}")
    return 1 if failed else 0


def _cmd_published_merge(args: argparse.Namespace) -> int:
    from tmig.parser.published import PublishedError, merge_published

    source: Path = args.path
    folder: Path = args.published or source.parent / "published"
    out: Path = args.out or source.with_name(f"{source.stem}.merged.twbx")
    try:
        result = merge_published(source, folder, out)
    except (PublishedError, WorkbookFormatError) as exc:
        print(f"失敗  {source}：{exc}", file=sys.stderr)
        return 1
    if not result.merged and not result.missing:
        print("報表沒有 Published Data Source，不需要併入")
    for item in result.merged:
        detail = f"，萃取 {item.hyper}" if item.hyper else ""
        note = f"（{item.note}）" if item.note else ""
        print(f"併入  {item.ref.caption}（{item.ref.content_url}）← {item.file.name}{detail}{note}")
    for ref in result.missing:
        print(
            f"缺少  {ref.caption}（{ref.content_url}）：{folder} 中找不到對應的 .tdsx／.tds",
            file=sys.stderr,
        )
    print(f"→ {result.out}")
    return 0 if result.ok else 1


def _schedule_source(args: argparse.Namespace):
    """報表庫模式（--store）：排程資料、站台資料與每次檢查時重新規劃的工作。"""
    from tmig.schedule.model import ScheduleError

    if args.schedules is not None:
        raise ScheduleError("排程檔與 --store 二選一（報表庫模式讀報表庫中的排程）")
    try:
        from tmig_service.schedules import ScheduleStore, StoreJobs
        from tmig_service.site import SiteStore
        from tmig_service.store import ReportStore
    except ImportError as exc:
        raise ValueError(
            "--store 需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync"
        ) from exc
    schedules = ScheduleStore(args.store)
    site = SiteStore(args.store)
    source = StoreJobs(
        schedules, ReportStore(args.store), site if site.exists() else None, args.fixtures
    )

    def jobs() -> tuple[list[Any], str | None]:
        planned, timezone = source()
        return planned, args.timezone or timezone

    return schedules, site if site.exists() else None, jobs, source


def _schedule_inputs(args: argparse.Namespace):
    """排程檔（或報表庫）、報表目錄與工作；沒有時區時報錯。"""
    from tmig.catalog import Catalog
    from tmig.schedule.model import ScheduleError, load_schedules
    from tmig.schedule.runner import plan_jobs

    if getattr(args, "store", None) is not None:
        store_schedules, _, jobs, _ = _schedule_source(args)
        planned, timezone = jobs()
        if not timezone:
            raise ScheduleError(
                "報表庫的排程沒有時區：在站台管理的「排程」中設定，或以 --timezone 指定"
            )
        return store_schedules.data(), timezone, planned
    if args.schedules is None:
        raise ScheduleError("需要排程檔，或以 --store 指定報表庫")
    schedules = load_schedules(args.schedules)
    timezone = args.timezone or schedules.timezone
    if not timezone:
        raise ScheduleError(
            "排程檔沒有時區：以 --timezone 指定 Tableau Server 的時區（例如 Asia/Taipei）"
        )
    catalog = Catalog.load(args.fixtures)
    return schedules, timezone, plan_jobs(schedules, catalog)


def _scheduler(args: argparse.Namespace):
    """（工作, 排程器, 結束時的清理）。"""
    import datetime as dt

    from tmig.schedule.model import DEFAULT_WORKERS, MAX_WORKERS
    from tmig.schedule.runner import Handler, JobKind, Scheduler, extract_handler, utc_now

    store_mode = getattr(args, "store", None) is not None
    timezone: str = "UTC"
    jobs: list[Any] = []
    limit: int | None = getattr(args, "workers", None)
    if limit is not None and not 1 <= limit <= MAX_WORKERS:
        raise ValueError(f"--workers 必須是 1 到 {MAX_WORKERS}")
    if not store_mode:
        file_schedules, timezone, jobs = _schedule_inputs(args)
        limit = limit or file_schedules.workers
    clock = utc_now
    if getattr(args, "now", None):
        fixed = dt.datetime.fromisoformat(args.now)
        if fixed.tzinfo is None:
            raise ValueError("--now 必須含時區，例如 2026-11-02T06:00:00+08:00")

        def clock() -> dt.datetime:
            return fixed

    handlers: dict[JobKind, Handler] = {"extract": extract_handler(args.config)}
    subscription = _subscription_handler(args)
    if subscription is not None:
        handlers["subscription"] = subscription
    cleanup = subscription.close if subscription is not None else (lambda: None)
    if store_mode:
        # 報表庫模式：每次檢查時重讀排程資料（還沒有時區時只處理立即執行）；連續失敗時暫停並寄信通知
        from tmig_service.alerts import AlertChecker, AlertStore
        from tmig_service.schedules import FailurePolicy

        store_schedules, site, source, store_jobs = _schedule_source(args)
        planned, found = source()
        mail = subscription.mail if subscription is not None else _mail_settings(args)
        policy = FailurePolicy(store_schedules, site, mail, threshold=args.suspend_after)
        # 資料警示：萃取更新成功後與每小時檢查；收件人以自己的身分查詢
        alerts = AlertChecker(
            AlertStore(args.store),
            site,
            store_jobs.catalog,
            lambda: source()[0],
            mail,
            renderer=subscription.renderer if subscription is not None else None,
            clock=clock,
            threshold=args.suspend_after,
        )
        # 同時執行的上限：--workers，或站台管理「排程」中的設定（每次檢查時重讀）
        override = limit
        scheduler = Scheduler(
            planned,
            found or "UTC",
            handlers,
            store_schedules.state_dir,
            clock,
            source=source,
            on_result=policy,
            after_tick=alerts,
            extras={"alert": alerts.run_request},
            workers=lambda: override or store_schedules.data().workers,
        )
        return planned, scheduler, cleanup
    state_dir = args.state_dir or args.schedules.parent / "schedule-state"
    scheduler = Scheduler(
        jobs, timezone, handlers, state_dir, clock, workers=limit or DEFAULT_WORKERS
    )
    return jobs, scheduler, cleanup


def _mail_settings(args: argparse.Namespace):
    if not getattr(args, "mail", None):
        return None
    from tmig_service.subscriptions import load_mail

    return load_mail(args.mail)


def _subscription_handler(args: argparse.Namespace):
    """訂閱的處理程式（Q3）：需要郵件設定檔（--mail）與身分驗證設定檔中的使用者目錄（--auth 或 TMIG_AUTH；
    報表庫模式用站台資料）；沒有郵件設定時為 None（訂閱的工作記為失敗）。"""
    if not getattr(args, "mail", None):
        return None
    try:
        from tmig_service.auth import AuthSettings, load_auth
        from tmig_service.store import ReportStore
        from tmig_service.subscriptions import Renderer, SubscriptionHandler, load_mail
    except ImportError as exc:
        raise ValueError("訂閱需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync") from exc
    mail = load_mail(args.mail)
    store = getattr(args, "store", None)
    auth = load_auth(args.auth) if args.auth or store is None else AuthSettings()
    renderer = Renderer(
        args.fixtures,
        auth,
        getattr(args, "web", None),
        store=ReportStore(store) if store is not None else None,
    )
    return SubscriptionHandler(renderer, mail)


def _cmd_schedule_list(args: argparse.Namespace) -> int:
    import datetime as dt

    from tmig.schedule.model import ScheduleError
    from tmig.schedule.runner import last_runs

    try:
        _, timezone, jobs = _schedule_inputs(args)
        now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.UTC)
    except (ScheduleError, FileNotFoundError, ValueError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    if args.store is not None:
        state_dir = args.store / "schedule-state"
    else:
        state_dir = args.state_dir or args.schedules.parent / "schedule-state"
    last = last_runs(state_dir)
    for job in sorted(jobs, key=lambda j: (not j.runnable, j.id)):
        schedule = job.schedule
        when = (
            schedule.frequency.describe()
            if schedule is not None and schedule.frequency is not None
            else "—"
        )
        print(f"{job.id}  {job.label()}")
        print(f"    排程：{schedule.name if schedule else '—'}（{when}）")
        if job.runnable:
            assert schedule is not None and schedule.frequency is not None
            upcoming = schedule.frequency.next_run(now, timezone)
            print(
                f"    下次：{upcoming.isoformat()}；對象：{'、'.join(t.describe() for t in job.targets)}"
            )
        else:
            print(f"    不執行：{job.problem}")
        if job.id in last:
            item = last[job.id]
            print(f"    上次：{item.get('finished')} {item.get('status')} {item.get('detail')}")
    runnable = sum(1 for j in jobs if j.runnable)
    print(f"共 {len(jobs)} 個工作，可執行 {runnable} 個")
    return 0


def _cmd_schedule_tick(args: argparse.Namespace) -> int:
    from tmig.schedule.model import ScheduleError

    try:
        _, scheduler, cleanup = _scheduler(args)
    except (ScheduleError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    try:
        records = scheduler.tick()
    finally:
        cleanup()
    for record in records:
        print(f"{'通過' if record.status == 'ok' else '失敗'}  {record.job}：{record.detail}")
    if not records:
        print("沒有到期的工作")
    return 1 if any(r.status != "ok" for r in records) else 0


def _cmd_schedule_run(args: argparse.Namespace) -> int:
    from tmig.schedule.model import ScheduleError

    try:
        jobs, scheduler, cleanup = _scheduler(args)
    except (ScheduleError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    job = next((j for j in jobs if j.id == args.job), None)
    if job is None:
        cleanup()
        print(f"失敗：找不到工作 {args.job}", file=sys.stderr)
        return 1
    if not job.runnable and not job.suspended:
        cleanup()
        print(f"失敗：{job.id} 不能執行：{job.problem}", file=sys.stderr)
        return 1
    try:
        record = scheduler.run(job, trigger="manual", requested_by="（指令）")
    finally:
        cleanup()
    print(f"{'通過' if record.status == 'ok' else '失敗'}  {record.job}：{record.detail}")
    return 0 if record.status == "ok" else 1


def _cmd_schedule_serve(args: argparse.Namespace) -> int:
    from tmig.schedule.model import ScheduleError

    try:
        jobs, scheduler, cleanup = _scheduler(args)
    except (ScheduleError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    print(
        f"排程器：{sum(1 for j in jobs if j.runnable)} 個工作，同時最多執行 {scheduler.workers} 個，"
        f"狀態在 {scheduler.state_dir}"
    )
    try:
        scheduler.serve(
            on_record=lambda r: print(
                f"{r.finished} {'通過' if r.status == 'ok' else '失敗'}  {r.job}：{r.detail}",
                flush=True,
            ),
            # 報表庫模式：每 5 秒檢查一次站台管理排入的「立即執行」
            poll=5.0 if args.store is not None else 60.0,
        )
    except KeyboardInterrupt:
        print("排程器已停止")
    finally:
        cleanup()
    return 0


def _cmd_schedule_import(args: argparse.Namespace) -> int:
    from tmig.schedule.model import ScheduleError, load_schedules

    try:
        from tmig_service.schedules import ScheduleStore
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    try:
        data = load_schedules(args.schedules)
        if args.timezone:
            data.timezone = args.timezone
        if not data.timezone:
            raise ScheduleError(
                "排程檔沒有時區：以 --timezone 指定 Tableau Server 的時區（例如 Asia/Taipei）"
            )
        store = ScheduleStore(args.store)
        store.initialize(data, actor="（匯入）", replace=args.replace)
    except ScheduleError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    print(
        f"已匯入：{len(data.schedules)} 個排程、{len(data.extract_refreshes)} 個萃取更新工作、"
        f"{len(data.subscriptions)} 個訂閱 → {store.path}"
    )
    return 0


def _cmd_usage_summary(args: argparse.Namespace) -> int:
    import csv
    import datetime as dt

    try:
        from tmig_service.usage import UsageLog, summarize
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    from tmig.catalog import report_directories
    from tmig.verify.run import load_fixture_ir

    if args.days < 1:
        print("失敗：--days 至少為 1", file=sys.stderr)
        return 1
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days)
    names: dict[str, str] = {}
    if args.fixtures is not None:
        names = {
            d.name: load_fixture_ir(d).workbook.name for d in report_directories(args.fixtures)
        }
    usage = summarize(UsageLog(args.usage_dir).visits(since), names)
    print(f"最近 {args.days} 天（{since.date()} 起）")
    print("| 報表 | 檢視次數 | 使用者數 | 最後一次檢視 |")
    print("| --- | ---: | ---: | --- |")
    for item in usage:
        last = item.last.astimezone().strftime("%Y-%m-%d %H:%M") if item.last else "—"
        print(f"| {item.name}（{item.view}） | {item.visits} | {len(item.users)} | {last} |")
    unused = [u for u in usage if u.visits == 0]
    if unused:
        print(f"期間內沒有檢視的報表 {len(unused)} 份：{'、'.join(u.name for u in unused)}")
    if args.csv is not None:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(
                ["報表", "報表名稱", "view", "檢視次數", "使用者數", "最後一次檢視（UTC）"]
            )
            for item in usage:
                writer.writerow(
                    [
                        item.view,
                        item.name,
                        "",
                        item.visits,
                        len(item.users),
                        item.last.isoformat() if item.last else "",
                    ]
                )
                for view in item.views.values():
                    writer.writerow(
                        [
                            item.view,
                            item.name,
                            view.view,
                            view.visits,
                            len(view.users),
                            view.last.isoformat() if view.last else "",
                        ]
                    )
        print(f"→ {args.csv}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        from tmig_service import serve
        from tmig_service.auth import AuthError
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    print(f"查詢服務：http://{args.host}:{args.port}（讀取 {args.fixtures}）")
    try:
        serve(args.fixtures, args.host, args.port, args.web, args.auth, args.usage_dir, args.store)
    except AuthError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    return 0


def _site_store(args: argparse.Namespace) -> Any:
    from tmig_service.site import SiteStore

    return SiteStore(args.store)


def _cmd_site_import(args: argparse.Namespace) -> int:
    """Tableau Server 匯出的使用者目錄與權限 → 報表庫的站台資料。"""
    try:
        from tmig.access.directory import DirectoryError, load_directory
        from tmig.access.permissions import PermissionsError, load_permissions
        from tmig_service.site import SiteError, from_exports
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    if args.directory is None and args.permissions is None:
        print("失敗：至少要給 --directory 或 --permissions", file=sys.stderr)
        return 1
    try:
        directory = load_directory(args.directory) if args.directory else None
        permissions = load_permissions(args.permissions) if args.permissions else None
        data = from_exports(permissions, directory)
        _site_store(args).initialize(data, actor="tmig site import", replace=args.replace)
    except (DirectoryError, PermissionsError, SiteError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    print(
        f"站台資料：{len(data.projects)} 個 Project、{len(data.workbooks)} 份 workbook 的權限、"
        f"{len(data.users)} 位使用者、{len(data.groups)} 個群組 → {args.store / 'site.json'}"
    )
    return 0


def _cmd_site_init(args: argparse.Namespace) -> int:
    """沒有 Tableau 可以匯入時新建站台：一個「預設」Project 與一位站台管理員。"""
    try:
        from tmig.access.directory import DirectoryUser
        from tmig.access.permissions import Grantee, ProjectEntry, Rule
        from tmig_service.site import ALL_USERS, SiteData, SiteError, now_text
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    everyone = Grantee(kind="group", name=ALL_USERS)
    data = SiteData(
        source="tmig site init",
        imported_at=now_text(),
        projects=[
            ProjectEntry(
                id="default",
                name="預設",
                owner=args.admin,
                rules=[Rule(grantee=everyone, capabilities={"Read": "Allow"})],
                workbook_defaults=[
                    Rule(grantee=everyone, capabilities={"Read": "Allow", "Filter": "Allow"})
                ],
            )
        ],
        users=[
            DirectoryUser(
                name=args.admin,
                email=args.email,
                site_role="SiteAdministratorCreator",
                groups=[ALL_USERS],
            )
        ],
    )
    try:
        _site_store(args).initialize(data, actor="tmig site init", replace=args.replace)
    except SiteError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    print(f"站台資料：預設 Project 與站台管理員 {args.admin} → {args.store / 'site.json'}")
    return 0


def _cmd_site_show(args: argparse.Namespace) -> int:
    try:
        from tmig_service.site import SiteError
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    try:
        data = _site_store(args).data()
    except SiteError as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    print(
        f"來源：{data.source or '（未記錄）'}；匯入：{data.imported_at}；最後修改：{data.updated_at or '—'}"
    )
    print(
        f"Project {len(data.projects)} 個、workbook 權限 {len(data.workbooks)} 份、使用者 {len(data.users)} 位、群組 {len(data.groups)} 個"
    )
    admins = [
        u.name
        for u in data.users
        if (u.site_role or "").startswith(("SiteAdministrator", "Server"))
    ]
    print(f"站台管理員：{'、'.join(admins) or '（沒有）'}")
    return 0


def _cmd_desktop_serve(args: argparse.Namespace) -> int:
    try:
        from tmig_service.desktop import default_workspace, serve_desktop
    except ImportError:
        print("失敗：需要查詢服務套件 tmig-service；在 repo 根目錄執行 uv sync", file=sys.stderr)
        return 1
    serve_desktop(
        args.workspace or default_workspace(),
        port=args.port,
        web_dist=args.web,
        clean=args.clean,
        exit_with_parent=args.exit_with_parent,
    )
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

    inventory = commands.add_parser("inventory", help="批次盤點：總表、功能使用統計與起步候選")
    inventory.add_argument(
        "paths", nargs="*", type=Path, metavar="資料夾或檔案", help="含 .twb／.twbx 的資料夾或檔案"
    )
    inventory.add_argument(
        "--out-dir", type=Path, default=Path("inventory"), help="輸出目錄（預設 inventory）"
    )
    inventory.add_argument(
        "--server-meta",
        type=Path,
        help="tools/tableau_server 產生的 server.json（Project、檢視次數）",
    )
    inventory.add_argument(
        "--extracts-omitted",
        action="store_true",
        help=".twbx 下載時不含萃取檔（只盤點結構）；缺萃取檔不算不符合起步條件",
    )
    inventory.set_defaults(handler=_cmd_inventory)

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
        sub.add_argument(
            "--user",
            help="以這個 Tableau 使用者名稱查詢（列層級權限）；預設為 cases.yaml 的 user",
        )
        sub.add_argument(
            "--group",
            action="append",
            metavar="群組",
            help="登入者所屬的群組（ISMEMBEROF），可重複；指定時取代 cases.yaml 的群組",
        )
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

    cases_parser = commands.add_parser("cases", help="cases.yaml 篩選組合")
    cases_commands = cases_parser.add_subparsers(
        dest="cases_command", required=True, metavar="子指令"
    )
    cases_suggest = cases_commands.add_parser(
        "suggest", help="依 dashboard 的篩選與參數，用資料建議 3 組篩選組合"
    )
    cases_suggest.add_argument("fixture", type=Path, metavar="fixture 目錄")
    cases_suggest.add_argument("--write", action="store_true", help="接在 cases.yaml 的 cases 後面")
    cases_suggest.set_defaults(handler=_cmd_cases_suggest)

    verify = commands.add_parser("verify", help="計算比對：每張工作表 × 每組篩選，寫出差異報告")
    verify.add_argument("fixture", type=Path, metavar="fixture 目錄")
    verify.add_argument("--out", type=Path, help="差異報告路徑；預設為 <fixture>/report/calc.json")
    verify.set_defaults(handler=_cmd_verify)

    connections_parser = commands.add_parser("connections", help="Live 連線的設定與測試")
    connections_commands = connections_parser.add_subparsers(
        dest="connections_command", required=True, metavar="子指令"
    )
    conn_template = connections_commands.add_parser(
        "template", help="產生連線設定檔範本（帳號、密碼、權杖寫成 ${環境變數}）"
    )
    conn_template.add_argument(
        "paths", nargs="+", type=Path, metavar="報表或資料夾", help=".twb／.twbx、IR 檔或資料夾"
    )
    conn_template.add_argument("--out", type=Path, help="寫入檔案；省略時印到標準輸出")
    conn_template.set_defaults(handler=_cmd_connections_template)
    conn_check = connections_commands.add_parser(
        "check", help="檢查連線設定與環境變數，並以唯讀查詢測試連線"
    )
    conn_check.add_argument(
        "paths", nargs="+", type=Path, metavar="報表或資料夾", help=".twb／.twbx、IR 檔或資料夾"
    )
    conn_check.add_argument(
        "--config",
        type=Path,
        help="連線設定檔；預設為環境變數 TMIG_CONNECTIONS 或 connections.toml",
    )
    conn_check.add_argument("--no-connect", action="store_true", help="只檢查設定，不實際連線")
    conn_check.set_defaults(handler=_cmd_connections_check)

    extract_parser = commands.add_parser(
        "extract", help="萃取層：從來源更新萃取、與 Tableau 萃取比對"
    )
    extract_commands = extract_parser.add_subparsers(
        dest="extract_command", required=True, metavar="子指令"
    )
    refresh = extract_commands.add_parser("refresh", help="從來源資料庫更新萃取的 DuckDB 檔")
    refresh.add_argument(
        "path", type=Path, metavar="報表", help=".twb／.twbx、IR 檔或 fixture 目錄"
    )
    refresh.add_argument("--out-dir", type=Path, help="DuckDB 檔的目錄；預設為報表旁的 data")
    refresh.add_argument("--datasource", help="只更新這個資料源（id）")
    refresh.add_argument(
        "--incremental", action="store_true", help="增量更新：只附加增量欄位大於現有最大值的列"
    )
    refresh.add_argument(
        "--config",
        type=Path,
        help="連線設定檔；預設為環境變數 TMIG_CONNECTIONS 或 connections.toml",
    )
    refresh.set_defaults(handler=_cmd_extract_refresh)
    compare = extract_commands.add_parser(
        "compare", help="新萃取與 Tableau 萃取（.twbx 內的 .hyper）比對列數、總和、最小值、最大值"
    )
    compare.add_argument("path", type=Path, metavar=".twbx")
    compare.add_argument("--data-dir", type=Path, help="新萃取的目錄；預設為報表旁的 data")
    compare.add_argument("--datasource", help="只比對這個資料源（id）")
    compare.set_defaults(handler=_cmd_extract_compare)

    published_parser = commands.add_parser("published", help="Published Data Source")
    published_commands = published_parser.add_subparsers(
        dest="published_command", required=True, metavar="子指令"
    )
    merge = published_commands.add_parser(
        "merge", help="把 Published Data Source 的 .tdsx 併入報表，寫出自含的 .twbx"
    )
    merge.add_argument("path", type=Path, metavar="報表", help=".twb／.twbx")
    merge.add_argument(
        "--published", type=Path, help=".tdsx／.tds 所在的目錄；預設為報表旁的 published"
    )
    merge.add_argument("--out", type=Path, help="輸出的 .twbx；預設為 <報表>.merged.twbx")
    merge.set_defaults(handler=_cmd_published_merge)

    schedule_parser = commands.add_parser("schedule", help="排程器：萃取更新與訂閱")
    schedule_commands = schedule_parser.add_subparsers(
        dest="schedule_command", required=True, metavar="子指令"
    )
    for name, handler, help_text in (
        ("list", _cmd_schedule_list, "列出工作、下次執行時間、對應的報表與上次的結果"),
        ("tick", _cmd_schedule_tick, "執行一次到期的工作（給作業系統的排程呼叫）"),
        ("run", _cmd_schedule_run, "立即執行一個工作"),
        ("serve", _cmd_schedule_serve, "常駐執行"),
    ):
        sub = schedule_commands.add_parser(name, help=help_text)
        sub.add_argument(
            "schedules",
            type=Path,
            nargs="?",
            metavar="排程檔",
            help="tools/tableau_server 匯出的 schedules.json（與 --store 二選一）",
        )
        sub.add_argument(
            "--store",
            type=Path,
            help="報表庫：讀站台管理中的排程（每次檢查時重讀；狀態在報表庫的 schedule-state/）",
        )
        sub.add_argument(
            "--fixtures", type=Path, default=Path("fixtures"), help="報表目錄（預設 fixtures）"
        )
        sub.add_argument("--timezone", help="Tableau Server 的時區；預設為排程檔中的時區")
        sub.add_argument(
            "--state-dir", type=Path, help="狀態與執行紀錄的目錄；預設為排程檔旁的 schedule-state"
        )
        sub.add_argument("--config", type=Path, help="連線設定檔；預設讀環境變數 TMIG_CONNECTIONS")
        if name != "list":
            sub.add_argument(
                "--mail", type=Path, help="郵件設定檔（訂閱；見 tmig_service.subscriptions）"
            )
            sub.add_argument(
                "--auth",
                type=Path,
                help="身分驗證設定檔（訂閱者的使用者目錄與權限）；預設讀 TMIG_AUTH",
            )
            sub.add_argument("--web", type=Path, help="檢視器的建置目錄；預設為 web/dist")
            sub.add_argument(
                "--suspend-after",
                type=int,
                default=5,
                help="報表庫模式：連續失敗幾次後暫停工作（預設 5，與 Tableau Server 相同；0 為不暫停）",
            )
        if name in ("tick", "serve"):
            sub.add_argument(
                "--workers",
                type=int,
                help="同時執行的工作數上限（1 為一個接一個）；預設為排程資料中的設定，沒有設定時 2",
            )
        if name in ("list", "tick"):
            sub.add_argument("--now", help="以這個時間計算（ISO 格式含時區；測試用）")
        if name == "run":
            sub.add_argument("--job", required=True, help="工作 id，例如 extract:<id>")
        sub.set_defaults(handler=handler)
    schedule_import = schedule_commands.add_parser(
        "import", help="把 Tableau Server 匯出的排程檔匯入報表庫（之後在站台管理中修改）"
    )
    schedule_import.add_argument("schedules", type=Path, metavar="排程檔")
    schedule_import.add_argument("--store", type=Path, required=True, help="報表庫")
    schedule_import.add_argument("--timezone", help="Tableau Server 的時區；預設為排程檔中的時區")
    schedule_import.add_argument(
        "--replace", action="store_true", help="報表庫中已經有排程時取代（重新匯入）"
    )
    schedule_import.set_defaults(handler=_cmd_schedule_import)

    site_parser = commands.add_parser("site", help="站台資料：Project、使用者、群組與權限")
    site_commands = site_parser.add_subparsers(dest="site_command", required=True, metavar="子指令")
    site_import = site_commands.add_parser(
        "import", help="從 Tableau Server 匯出的使用者目錄與權限建立站台資料"
    )
    site_import.add_argument(
        "--directory", type=Path, help="tools/tableau_server 匯出的 directory.json"
    )
    site_import.add_argument(
        "--permissions", type=Path, help="tools/tableau_server 匯出的 permissions.json"
    )
    site_import.add_argument(
        "--replace", action="store_true", help="取代已經存在的站台資料（之前的修改會不見）"
    )
    site_import.set_defaults(handler=_cmd_site_import)
    site_init = site_commands.add_parser("init", help="新建站台：預設 Project 與一位站台管理員")
    site_init.add_argument("--admin", required=True, help="站台管理員的使用者名稱（登入的帳號）")
    site_init.add_argument("--email")
    site_init.add_argument("--replace", action="store_true", help="取代已經存在的站台資料")
    site_init.set_defaults(handler=_cmd_site_init)
    site_show = site_commands.add_parser("show", help="站台資料的摘要")
    site_show.set_defaults(handler=_cmd_site_show)
    for sub in (site_import, site_init, site_show):
        sub.add_argument(
            "--store", type=Path, required=True, help="報表庫的目錄（tmig serve --store）"
        )

    serve = commands.add_parser("serve", help="啟動查詢服務與檢視器")
    serve.add_argument(
        "--fixtures", type=Path, default=Path("fixtures"), help="報表目錄（預設 fixtures）"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--web", type=Path, help="檢視器的建置目錄；預設為 web/dist")
    serve.add_argument(
        "--auth", type=Path, help="身分驗證設定檔（TOML）；預設讀環境變數 TMIG_AUTH，沒有時不驗證"
    )
    serve.add_argument(
        "--usage-dir",
        type=Path,
        help="使用紀錄的目錄；預設讀環境變數 TMIG_USAGE_DIR，沒有時為報表目錄旁的 usage",
    )
    serve.add_argument(
        "--store",
        type=Path,
        help="報表庫（發布的報表、每一版與桌面版的登入）；預設讀環境變數 TMIG_STORE，"
        "沒有時為報表目錄旁的 store",
    )
    serve.set_defaults(handler=_cmd_serve)

    desktop_parser = commands.add_parser("desktop", help="桌面版")
    desktop_commands = desktop_parser.add_subparsers(
        dest="desktop_command", required=True, metavar="子指令"
    )
    desktop_serve = desktop_commands.add_parser(
        "serve",
        help="桌面版的本機引擎：只接受本機、帶權杖的連線；就緒時在標準輸出印一行 JSON（埠號與權杖）",
    )
    desktop_serve.add_argument(
        "--workspace", type=Path, help="工作區（開啟的檔案放這裡）；預設為使用者的應用程式資料夾"
    )
    desktop_serve.add_argument("--port", type=int, default=0, help="埠號；0（預設）為自動選一個")
    desktop_serve.add_argument("--web", type=Path, help="檢視器的建置目錄；預設為 web/dist")
    desktop_serve.add_argument("--clean", action="store_true", help="啟動前清空工作區")
    desktop_serve.add_argument(
        "--exit-with-parent",
        action="store_true",
        help="標準輸入關閉時結束（由桌面版外殼啟動時使用）",
    )
    desktop_serve.set_defaults(handler=_cmd_desktop_serve)

    usage_parser = commands.add_parser("usage", help="使用紀錄")
    usage_commands = usage_parser.add_subparsers(
        dest="usage_command", required=True, metavar="子指令"
    )
    summary = usage_commands.add_parser(
        "summary", help="每份報表的檢視次數、使用者數與最後一次檢視（含沒有檢視的報表）"
    )
    summary.add_argument("usage_dir", type=Path, metavar="使用紀錄目錄")
    summary.add_argument("--fixtures", type=Path, help="報表目錄：列出期間內沒有檢視的報表")
    summary.add_argument("--days", type=int, default=90, help="統計最近幾天（預設 90）")
    summary.add_argument("--csv", type=Path, help="另外寫成 CSV（Excel 可直接開）")
    summary.set_defaults(handler=_cmd_usage_summary)

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
