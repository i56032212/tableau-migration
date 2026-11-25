"""檢視器的位置：--web 沒有指定時讀環境變數 TMIG_WEB_DIST（安裝版的伺服器），沒有時為 repo 的 web/dist。"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig_service.app import create_app
from tmig_service.paths import REPO_ROOT, WEB_DIST_ENV, default_web_dist

FIXTURES = REPO_ROOT / "fixtures"


def _dist(root: Path, marker: str) -> Path:
    root.mkdir(parents=True)
    (root / "index.html").write_text(f"<html>{marker}</html>", encoding="utf-8")
    (root / "app.js").write_text(f"// {marker}", encoding="utf-8")
    return root


def test_default_is_the_repo_build_without_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WEB_DIST_ENV, raising=False)
    assert default_web_dist() == REPO_ROOT / "web" / "dist"


def test_viewer_comes_from_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = _dist(tmp_path / "installed" / "web" / "dist", "安裝版")
    monkeypatch.setenv(WEB_DIST_ENV, str(installed))
    client = TestClient(create_app(FIXTURES))
    assert "安裝版" in client.get("/").text
    assert "安裝版" in client.get("/app.js").text
    # 檢視器的網址（前端的路由）也回同一個 index.html；API 不受影響
    assert "安裝版" in client.get("/views/superstore_demo").text
    assert client.get("/api/workbooks").status_code == 200


def test_web_option_wins_over_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WEB_DIST_ENV, str(_dist(tmp_path / "env", "環境變數")))
    client = TestClient(create_app(FIXTURES, _dist(tmp_path / "option", "指令")))
    assert "指令" in client.get("/").text


def test_subscriptions_use_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """訂閱以檢視器產生 PNG、PDF：沒有指定 --web 時也用環境變數的位置（找不到時的訊息寫出那個位置）。"""
    from tmig.access.directory import Directory, DirectoryUser
    from tmig_service.auth import AuthSettings
    from tmig_service.subscriptions import Renderer, SubscriptionError

    missing = tmp_path / "missing-dist"
    monkeypatch.setenv(WEB_DIST_ENV, str(missing))
    directory = Directory(users=[DirectoryUser(name="alice", email="alice@example.com")])
    renderer = Renderer(FIXTURES, AuthSettings(mode="oidc", directory=directory))
    with pytest.raises(SubscriptionError, match="missing-dist"):
        renderer._start()  # pyright: ignore[reportPrivateUsage]
