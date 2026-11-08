# PyInstaller 規格：把本機引擎（tmig、tmig_service、相依套件與檢視器）打包成一個資料夾 tmig-engine/。
#
#   uv run --with pyinstaller pyinstaller desktop/engine/tmig-engine.spec \
#       --distpath desktop/engine/dist --workpath desktop/engine/build --noconfirm
#
# 資料夾形式（不是單一檔案）：啟動時不必先解壓縮，開啟桌面版比較快。
# 訂閱（playwright）、Spark 與測試用的套件不打包。
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821（PyInstaller 提供 SPECPATH）
SKIPPED = ("subscriptions", ".tests")

datas = [(str(ROOT / "web" / "dist"), "web/dist")]
binaries = []
hiddenimports = []
# Hyper API 帶有 hyperd 與原生程式庫，全部帶上（萃取匯入 DuckDB 時用）
hyper_datas, hyper_binaries, hyper_imports = collect_all("tableauhyperapi")
datas += hyper_datas
binaries += hyper_binaries
hiddenimports += hyper_imports
# 延遲匯入（函式內 import）與 SQLGlot 的方言、uvicorn 的協定實作
for package in ("tmig", "tmig_service", "sqlglot", "uvicorn"):
    hiddenimports += collect_submodules(
        package, filter=lambda name: not any(part in name for part in SKIPPED)
    )

a = Analysis(  # noqa: F821
    [str(ROOT / "desktop" / "engine" / "entry.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["playwright", "pyspark", "pytest", "tkinter", "tmig_service.subscriptions"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    # UTF-8 模式（等同 python -X utf8）：Windows 的主控台與 open() 預設是系統碼頁（cp950、cp1252），
    # 中文訊息、未指定編碼的檔案在不同語系的電腦上結果會不同；外殼以 UTF-8 讀引擎的輸出
    [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="tmig-engine",
    console=True,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="tmig-engine",
)
