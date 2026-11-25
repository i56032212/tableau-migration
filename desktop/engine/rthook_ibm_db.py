"""PyInstaller 的執行期掛鉤：Db2 驅動（ibm_db）的 clidriver。

一般安裝時 ibm_db 以 site-packages 的 ibm_db_dll.pth 把 clidriver\bin 加進 DLL 的搜尋路徑；打包後不會讀 .pth，
改在這裡加（tmig-engine.spec 把 clidriver 整個資料夾放在封裝內）。
"""

import os
import sys

_base = getattr(sys, "_MEIPASS", None)
_home = os.path.join(_base, "clidriver") if _base else None
if _home and os.path.isdir(os.path.join(_home, "bin")) and hasattr(os, "add_dll_directory"):
    os.add_dll_directory(os.path.join(_home, "bin"))
    # clidriver 自己再載入的 DLL 與訊息檔
    os.environ.setdefault("IBM_DB_HOME", _home)
    os.environ["PATH"] = os.path.join(_home, "bin") + os.pathsep + os.environ.get("PATH", "")
