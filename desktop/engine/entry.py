"""桌面版本機引擎的進入點（PyInstaller 打包用）：等同 tmig 指令，另外指定檢視器的位置。

打包後檢視器在封裝內的 web/dist；開發時（沒有打包）用 repo 的 web/dist。
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def main() -> int:
    from tmig.cli import main as tmig_main

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    argv = sys.argv[1:]
    if argv[:2] == ["desktop", "serve"] and "--web" not in argv:
        argv += ["--web", str(base / "web" / "dist")]
    return tmig_main(argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
