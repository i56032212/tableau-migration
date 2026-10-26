"""一鍵檢查：ruff lint、ruff 格式、pyright、pytest。

用法（在 repo 根目錄）：

    uv run python scripts/check.py          # 只檢查
    uv run python scripts/check.py --fix    # 先自動修正 lint 與格式，再檢查

任何一步失敗，結束碼為 1；所有步驟都會跑完，方便一次看到全部問題。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FIX_STEPS: list[tuple[str, list[str]]] = [
    ("ruff 自動修正", [sys.executable, "-m", "ruff", "check", "--fix", "."]),
    ("ruff 自動排版", [sys.executable, "-m", "ruff", "format", "."]),
]

CHECK_STEPS: list[tuple[str, list[str]]] = [
    ("ruff lint", [sys.executable, "-m", "ruff", "check", "."]),
    ("ruff 格式", [sys.executable, "-m", "ruff", "format", "--check", "."]),
    ("pyright 型別", [sys.executable, "-m", "pyright"]),
    ("pytest 測試", [sys.executable, "-m", "pytest", "-q"]),
]


def run(steps: list[tuple[str, list[str]]]) -> list[tuple[str, bool, float]]:
    results: list[tuple[str, bool, float]] = []
    for name, cmd in steps:
        print(f"\n=== {name} ===", flush=True)
        start = time.perf_counter()
        ok = subprocess.run(cmd, cwd=ROOT, check=False).returncode == 0
        results.append((name, ok, time.perf_counter() - start))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="一鍵檢查")
    parser.add_argument("--fix", action="store_true", help="先自動修正 lint 與格式")
    args = parser.parse_args()

    if args.fix:
        run(FIX_STEPS)
    results = run(CHECK_STEPS)

    print("\n=== 結果 ===")
    for name, ok, seconds in results:
        print(f"{'通過' if ok else '失敗'}  {name}（{seconds:.1f} 秒）")
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"\n{len(failed)} 項失敗：{'、'.join(failed)}")
        return 1
    print("\n全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
