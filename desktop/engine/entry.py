"""桌面版本機引擎的進入點（PyInstaller 打包用）：等同 tmig 指令，另外指定檢視器的位置。

打包後檢視器在封裝內的 web/dist；開發時（沒有打包）用 repo 的 web/dist。
`tmig-engine --check-modules <模組>…` 是冒煙測試用的：檢查資料庫驅動等模組有沒有打包進去。
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def check_modules(names: list[str]) -> int:
    """冒煙測試用：載入每個模組（資料庫驅動等），以 JSON 印出結果；都載入成功時結束碼為 0。
    「模組:函式」在載入後再呼叫那個函式（沒有參數），例如要到呼叫時才載入原生程式庫的驅動。"""
    import importlib
    import json

    result: dict[str, str] = {}
    for name in names:
        try:
            module, _, function = name.partition(":")
            loaded = importlib.import_module(module)
            if function:
                getattr(loaded, function)()
            result[name] = "ok"
        except Exception as exc:  # 打包缺了檔案時各種例外都有可能
            result[name] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(v == "ok" for v in result.values()) else 1


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--check-modules"]:
        return check_modules(argv[1:])
    from tmig.cli import main as tmig_main

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    if argv[:2] == ["desktop", "serve"] and "--web" not in argv:
        argv += ["--web", str(base / "web" / "dist")]
    return tmig_main(argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
