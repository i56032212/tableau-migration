"""建置伺服器版的 Windows 離線安裝檔（TmigServer_<版本>_x64-offline-setup.exe）。

    uv run python deploy/windows/build.py                 # 組出 deploy/windows/build/staging、冒煙測試、打包安裝檔
    uv run python deploy/windows/build.py --no-installer  # 只組資料夾與冒煙測試

安裝檔內含執行時需要的一切，安裝與執行都不必連網路：
- python\\        可搬移的 CPython（python-build-standalone，由 uv 安裝），以及 uv.lock 鎖定版本的所有相依套件與
                 所有資料庫驅動（kernel 的 optional-dependencies），tmig 與 tmig_service 本身
- ms-playwright\\ 訂閱產生 PNG、PDF 用的 Chromium（headless shell）
- web\\dist\\      檢視器
- tmig-server-host.exe  Windows 服務主程式（deploy/windows/host）
- tmig.cmd       指令列工具（與 README「指令」的 tmig 相同）
- config\\        server.toml 的預設內容與 auth、mail 設定檔的範本

建置時要連網路（uv 下載 Python 與套件、Playwright 下載 Chromium、cargo 下載 crate），需要 uv、Rust 與 NSIS
（沒有另外安裝時用 Tauri 建置桌面版時下載的 %LOCALAPPDATA%\\tauri\\NSIS）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "deploy" / "windows"
HOST = HERE / "host"

# 用不到的標準程式庫（測試、IDLE、Tk）：不放進安裝檔
PYTHON_SKIP = {
    "Lib/test",
    "Lib/idlelib",
    "Lib/turtledemo",
    "Lib/tkinter",
    "tcl",
    "DLLs/_tkinter.pyd",
    "DLLs/tcl86t.dll",
    "DLLs/tk86t.dll",
}

# 安裝目錄最上層的內容（installer.nsi 解除安裝時逐一刪除，兩邊要一致）
INSTALLED = (
    "python",
    "ms-playwright",
    "web",
    "config",
    "tmig-server-host.exe",
    "tmig.cmd",
    "NOTICE.md",
    "build-info.json",
)

CMD = (
    "@echo off\r\n"
    "setlocal\r\n"
    'set "PYTHONUTF8=1"\r\n'
    'set "PYTHONIOENCODING=utf-8"\r\n'
    'set "PYTHONNOUSERSITE=1"\r\n'
    'set "PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright"\r\n'
    'set "TMIG_WEB_DIST=%~dp0web\\dist"\r\n'
    '"%~dp0python\\python.exe" -X utf8 -s -m tmig.cli %*\r\n'
)


def run(command: list[str], **kwargs: object) -> None:
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in command), flush=True)
    subprocess.run(command, check=True, **kwargs)  # pyright: ignore[reportArgumentType, reportCallIssue]


def output(command: list[str]) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def version() -> str:
    data = tomllib.loads((ROOT / "service" / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def find_tool(name: str, candidates: list[Path]) -> Path:
    found = shutil.which(name)
    if found:
        return Path(found)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(f"找不到 {name}")


def python_runtime(uv: Path) -> Path:
    """uv 管理的 CPython（python-build-standalone）的資料夾；版本與 .python-version 相同。"""
    wanted = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    # 有些電腦上 uv 建不了次版本的連結（cpython-3.12-…），但 Python 本身已經裝好：只看下面找不找得到
    subprocess.run([str(uv), "python", "install", wanted], check=False)
    listed = json.loads(
        output(
            [
                str(uv),
                "python",
                "list",
                "--only-installed",
                "--managed-python",
                "--output-format",
                "json",
            ]
        )
    )
    found = [
        item
        for item in listed
        if item.get("implementation") == "cpython"
        and item.get("variant") == "default"
        and item.get("arch") == "x86_64"
        and str(item.get("version", "")).startswith(f"{wanted}.")
        and Path(item["path"]).is_file()
    ]
    if not found:
        raise SystemExit(f"uv 沒有裝好 CPython {wanted}")
    latest = max(found, key=lambda item: tuple(int(p) for p in item["version"].split(".")))
    print(f"Python：{latest['key']}")
    return Path(latest["path"]).parent


def copy_python(source: Path, target: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).resolve().relative_to(source.resolve())
        skipped = {n for n in names if (relative / n).as_posix() in PYTHON_SKIP}
        return skipped | {n for n in names if n == "__pycache__"}

    shutil.copytree(source, target, ignore=ignore)
    # uv 管理的 Python 標成 externally managed；這份是安裝檔專用的，直接把套件裝進去
    (target / "Lib" / "EXTERNALLY-MANAGED").unlink(missing_ok=True)


def install_packages(uv: Path, python: Path, work: Path) -> None:
    requirements = work / "requirements.txt"
    run(
        [
            str(uv),
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-workspace",
            "--all-packages",
            "--all-extras",
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT,
    )
    common = [str(uv), "pip", "install", "--python", str(python), "--link-mode", "copy"]
    run([*common, "--require-hashes", "--no-deps", "-r", str(requirements)], cwd=ROOT)
    run([*common, "--no-deps", str(ROOT / "kernel"), str(ROOT / "service")], cwd=ROOT)
    run([str(uv), "pip", "check", "--python", str(python)], cwd=ROOT)
    prune_foreign_binaries(python.parent / "Lib" / "site-packages")
    # 先編譯好：安裝目錄（Program Files）服務帳號不能寫，執行時不會再產生 .pyc；
    # unchecked-hash 不比對原始檔的時間，安裝時檔案時間改變也有效
    compiled = subprocess.run(
        [
            str(python),
            "-X",
            "utf8",
            "-m",
            "compileall",
            "-q",
            "-j",
            "0",
            "--invalidation-mode",
            "unchecked-hash",
            str(python.parent / "Lib"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if compiled.returncode != 0:
        # 少數套件附了其他 Python 版本的範例檔，編譯不過也不影響執行
        print("注意：有些檔案編譯不過（不影響執行）：")
        print("\n".join(compiled.stdout.splitlines()[:20]))


# 套件附帶的其他作業系統、CPU 的原生程式庫：64 位元的 Windows 用不到（teradatasql 依平台選一個載入）
FOREIGN_BINARIES = {
    "teradatasql": (
        "teradatasql.aix.so",
        "teradatasql.arm.fips.so",
        "teradatasql.arm.so",
        "teradatasql.dylib",
        "teradatasql.fips.so",
        "teradatasql.power.so",
        "teradatasql.so",
        "teradatasql.x86.dll",
        "teradatasql.x86.so",
    ),
}


def prune_foreign_binaries(site_packages: Path) -> None:
    for package, names in FOREIGN_BINARIES.items():
        for name in names:
            (site_packages / package / name).unlink(missing_ok=True)


def install_chromium(python: Path, target: Path) -> None:
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(target))
    run([str(python), "-m", "playwright", "install", "--only-shell", "chromium"], env=env)


def build_host(cargo: Path) -> Path:
    run([str(cargo), "build", "--release", "--locked"], cwd=HOST)
    return HOST / "target" / "release" / "tmig-server-host.exe"


def build_info(uv: Path, python: Path, staging: Path) -> dict[str, object]:
    commit = output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip()
    dirty = bool(output(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"]))
    packages = output([str(uv), "pip", "freeze", "--python", str(python)]).split()
    return {
        "version": version(),
        "commit": commit,
        "uncommitted_changes": dirty,
        "built": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": output([str(python), "-c", "import sys; print(sys.version)"]).strip(),
        "packages": packages,
        "chromium": sorted(p.name for p in (staging / "ms-playwright").iterdir() if p.is_dir()),
    }


def stage(staging: Path, work: Path) -> None:
    uv = find_tool("uv", [])
    cargo = find_tool("cargo", [Path.home() / ".cargo" / "bin" / "cargo.exe"])
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        raise SystemExit(
            "找不到 web/dist；先執行 cd web && pnpm install --frozen-lockfile && pnpm build"
        )
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    work.mkdir(parents=True, exist_ok=True)

    copy_python(python_runtime(uv), staging / "python")
    python = staging / "python" / "python.exe"
    install_packages(uv, python, work)
    install_chromium(python, staging / "ms-playwright")
    shutil.copytree(ROOT / "web" / "dist", staging / "web" / "dist")
    shutil.copy2(build_host(cargo), staging / "tmig-server-host.exe")
    (staging / "tmig.cmd").write_bytes(CMD.encode("ascii"))
    config = staging / "config"
    config.mkdir()
    shutil.copy2(HERE / "server.toml", config / "server.toml")
    shutil.copy2(ROOT / "service" / "auth.example.toml", config / "auth.example.toml")
    shutil.copy2(ROOT / "service" / "mail.example.toml", config / "mail.example.toml")
    shutil.copy2(ROOT / "NOTICE.md", staging / "NOTICE.md")
    info = build_info(uv, python, staging)
    (staging / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    contents = sorted(p.name for p in staging.iterdir())
    if contents != sorted(INSTALLED):
        raise SystemExit(f"組好的資料夾內容與 INSTALLED 不同：{contents}")


def makensis() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "tauri" / "NSIS"
    return find_tool("makensis", [local / "makensis.exe", local / "Bin" / "makensis.exe"])


def package(staging: Path, out: Path) -> Path:
    installer = out / f"TmigServer_{version()}_x64-offline-setup.exe"
    installer.unlink(missing_ok=True)
    run(
        [
            str(makensis()),
            "/V2",
            "/INPUTCHARSET",
            "UTF8",
            f"/DVERSION={version()}",
            f"/DSTAGING={staging}",
            f"/DOUTFILE={installer}",
            str(HERE / "installer.nsi"),
        ]
    )
    return installer


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--out", type=Path, default=HERE / "build", help="輸出的資料夾")
    parser.add_argument("--no-installer", action="store_true", help="不打包安裝檔")
    parser.add_argument("--reuse-staging", action="store_true", help="沿用已經組好的資料夾")
    parser.add_argument("--no-smoke", action="store_true", help="不跑冒煙測試")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("伺服器版的 Windows 安裝檔要在 Windows 上建置")
    out = args.out.resolve()
    staging = out / "staging"
    if not (args.reuse_staging and staging.is_dir()):
        stage(staging, out / "work")
    if not args.no_smoke:
        run([sys.executable, str(HERE / "smoke.py"), str(staging)])
    if args.no_installer:
        return 0
    installer = package(staging, out)
    size = installer.stat().st_size
    print(f"安裝檔：{installer}（{size:,} 位元組）")
    print(f"SHA-256：{sha256(installer)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
