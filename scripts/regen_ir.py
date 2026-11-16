"""IR 的欄位異動之後，重新產生由程式產生的檔案：

- IR 的 JSON Schema（kernel/schema/ir-<版本>.schema.json）
- 範例 IR（kernel/tests/ir/data/example.ir.json：讀進來再寫出去，補上新欄位的預設值）
- 各 fixture 的 golden IR（fixtures/<報表>/golden.ir.json：重新解析同目錄的 <報表>.twbx）

用法（在 repo 根目錄）：

    uv run python scripts/regen_ir.py

產生後以 git diff 檢查差異再提交：除了新欄位之外不應該有其他變動。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tmig.cli import main as tmig
from tmig.ir import IR_VERSION, dumps_ir, load_ir

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    failed = 0
    schema = ROOT / "kernel" / "schema" / f"ir-{IR_VERSION}.schema.json"
    failed += tmig(["ir", "schema", "--out", str(schema)]) != 0
    example = ROOT / "kernel" / "tests" / "ir" / "data" / "example.ir.json"
    example.write_text(dumps_ir(load_ir(example)), encoding="utf-8", newline="\n")
    print(f"已寫入 {example.relative_to(ROOT).as_posix()}")
    for directory in sorted((ROOT / "fixtures").iterdir()):
        workbook = directory / f"{directory.name}.twbx"
        golden = directory / "golden.ir.json"
        if not workbook.is_file() or not golden.is_file():
            continue
        failed += tmig(["parse", str(workbook), "--out", str(golden)]) != 0
    if failed:
        print(f"{failed} 項失敗", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
