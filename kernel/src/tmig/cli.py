"""tmig 命令列。

tmig census <.twb／.twbx>... [--out-dir 目錄]   功能盤點與起步條件檢查
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
from tmig.ir import ir_json_schema_text, load_ir
from tmig.parser.twbx import WorkbookFormatError


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
