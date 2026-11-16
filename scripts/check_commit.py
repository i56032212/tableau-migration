"""在另一個工作目錄（git worktree）跑某個 commit 的完整 pytest：主目錄可以繼續修改，不會互相影響。

完整的測試大約要 25 分鐘。先提交，再對那個 commit 跑；通過之後才推上去。

用法（在 repo 根目錄）：

    uv run python scripts/check_commit.py                    # 檢查 HEAD
    uv run python scripts/check_commit.py <commit> --log 檔案 # 輸出另存一份（跑在背景時用）
    uv run python scripts/check_commit.py --require-engines  # 沒有 PostgreSQL、MySQL 等時失敗（不略過）

工作目錄在 .worktrees/check（不進版控），第一次執行時建立，之後重複使用。測試用目前的 Python 環境
（.venv 的套件），但 tmig、tmig_service 取自工作目錄的原始碼。只跑 pytest：ruff 與 pyright 很快，
在主目錄以 scripts/check.py 檢查。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKTREE = ROOT / ".worktrees" / "check"


def git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=False, capture_output=True, text=True, encoding="utf-8"
    )


def prepare(commit: str) -> str | None:
    """把工作目錄切到 commit（沒有時建立）；回傳失敗的原因。"""
    resolved = git("rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved.returncode != 0:
        return f"找不到 commit {commit}"
    sha = resolved.stdout.strip()
    if not (WORKTREE / ".git").exists():
        WORKTREE.parent.mkdir(parents=True, exist_ok=True)
        added = git("worktree", "add", "--detach", str(WORKTREE), sha)
        return added.stderr.strip() if added.returncode != 0 else None
    # 上次測試留下的變更（例如重新產生的檔案）丟掉，再切到這個 commit
    git("checkout", "--", ".", cwd=WORKTREE)
    switched = git("checkout", "--detach", sha, cwd=WORKTREE)
    return switched.stderr.strip() if switched.returncode != 0 else None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="在另一個工作目錄跑某個 commit 的完整 pytest")
    parser.add_argument("commit", nargs="?", default="HEAD")
    parser.add_argument("--log", type=Path, help="pytest 的輸出另存到這個檔案")
    parser.add_argument(
        "--require-engines",
        action="store_true",
        help="需要外部引擎的測試（PostgreSQL、MySQL 等）沒有引擎時失敗，不略過",
    )
    args, extra = parser.parse_known_args()

    problem = prepare(args.commit)
    if problem is not None:
        print(f"失敗：{problem}", file=sys.stderr)
        return 2
    sources = [WORKTREE / "kernel" / "src", WORKTREE / "service" / "src"]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(str(p) for p in sources),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    if args.require_engines:
        env["TMIG_REQUIRE_ENGINES"] = "1"
    # 確認測到的是工作目錄的程式，不是主目錄安裝在環境中的那一份
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tmig, tmig_service; print(tmig.__file__, tmig_service.__file__)",
        ],
        cwd=WORKTREE,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    found = [Path(p).resolve() for p in probe.stdout.split()]
    if (
        probe.returncode != 0
        or len(found) != 2
        or not all(p.is_relative_to(WORKTREE.resolve()) for p in found)
    ):
        print(f"失敗：匯入的不是工作目錄的程式（{probe.stdout.strip() or probe.stderr.strip()}）")
        return 2
    sha = git("rev-parse", "--short", "HEAD", cwd=WORKTREE).stdout.strip()
    print(f"在 {WORKTREE.relative_to(ROOT).as_posix()} 測試 {sha}", flush=True)
    command = [sys.executable, "-m", "pytest", "-q", *extra]
    if args.log is None:
        code = subprocess.run(command, cwd=WORKTREE, env=env, check=False).returncode
    else:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("w", encoding="utf-8") as log:
            log.write(f"commit {sha}\n")
            log.flush()
            code = subprocess.run(
                command, cwd=WORKTREE, env=env, check=False, stdout=log, stderr=subprocess.STDOUT
            ).returncode
            log.write(f"exit {code}\n")
        print(f"結束碼 {code}，輸出在 {args.log}")
    return code


if __name__ == "__main__":
    sys.exit(main())
