"""伺服器版的 Windows 安裝檔（deploy/windows）：預設的 server.toml 與安裝程式的一致性。

實際的建置、冒煙測試與安裝在 Windows 上以 deploy/windows/build.py 進行（見 README「伺服器的安裝（Windows）」）；
服務主程式（Rust）的單元測試是 deploy/windows/host 的 cargo test。
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from tmig.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "deploy" / "windows"
DATA = r"C:\ProgramData\TmigServer"
INSTALL = r"C:\Program Files\TmigServer"


def _processes() -> list[dict[str, object]]:
    config = tomllib.loads((HERE / "server.toml").read_text(encoding="utf-8"))
    assert set(config) <= {"environment", "process"}
    return config["process"]


def _args(process: dict[str, object]) -> list[str]:
    args = process["args"]
    assert isinstance(args, list)
    return [str(a).replace("{data}", DATA).replace("{install}", INSTALL) for a in args]


@pytest.mark.parametrize("name", ["server", "scheduler"])
def test_default_processes_are_valid_tmig_commands(name: str) -> None:
    """預設的每個程序都是 tmig 認得的指令（參數改名時這裡會失敗）。"""
    process = next(p for p in _processes() if p["name"] == name)
    namespace = build_parser().parse_args(_args(process))
    assert namespace.handler is not None
    assert Path(str(namespace.fixtures)) == Path(DATA) / "workbooks"
    assert Path(str(namespace.store)) == Path(DATA) / "store"


def test_server_listens_only_locally_and_scheduler_is_off_by_default() -> None:
    """沒有設定身分驗證時連得到的人都能看所有的報表：預設只接受本機的連線，排程器預設不啟動。"""
    processes = {str(p["name"]): p for p in _processes()}
    server = build_parser().parse_args(_args(processes["server"]))
    assert server.host == "127.0.0.1"
    assert server.auth is None
    assert Path(str(server.usage_dir)) == Path(DATA) / "usage"
    assert processes["server"].get("enabled", True) is True
    assert processes["scheduler"].get("enabled") is False


def _build_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_windows_build", HERE / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_uninstaller_removes_everything_the_build_installs() -> None:
    script = (HERE / "installer.nsi").read_text(encoding="utf-8")
    uninstall = script[script.index('Section "Uninstall"') :]
    removed = set(re.findall(r'(?:RMDir /r|Delete) "\$INSTDIR[\\/]([^"\\/]+)"', uninstall))
    installed = set(_build_module().INSTALLED)
    assert installed <= removed, installed - removed
    # 升級時先刪掉的資料夾（避免留下舊版的套件）也都是安裝的內容
    install = script[: script.index('Section "Uninstall"')]
    replaced = set(re.findall(r'RMDir /r "\$INSTDIR[\\/]([^"\\/]+)"', install))
    assert replaced == {name for name in installed if "." not in name}


def test_command_wrapper_sets_the_same_locations_as_the_service() -> None:
    """tmig.cmd 與服務主程式給的檢視器、Chromium 位置相同（指令列執行訂閱時也找得到）。"""
    command = _build_module().CMD
    host = (HERE / "host" / "src" / "config.rs").read_text(encoding="utf-8")
    for variable in ("TMIG_WEB_DIST", "PLAYWRIGHT_BROWSERS_PATH", "PYTHONUTF8", "PYTHONNOUSERSITE"):
        assert f"{variable}=" in command
        assert f'"{variable}"' in host
    assert command.isascii()  # cmd.exe 以系統碼頁讀批次檔
