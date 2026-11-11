"""Story（故事）：報表清單與報表定義列出 Story；只能看 Story 時，故事點顯示的 dashboard 與工作表
跟著 Story 開放（與 dashboard 中的工作表跟著 dashboard 相同），報表清單只列出 Story 本身。

以功能報表的「銷售故事」為例：故事點顯示「篩選動作」dashboard（4 張工作表）與 Sales by Segment 工作表。
"""

from __future__ import annotations

import json
import shutil
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.access.directory import Directory, DirectoryUser
from tmig.access.permissions import Permissions
from tmig.ir import load_ir
from tmig_service import create_app
from tmig_service.auth import AuthSettings

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "fixtures" / "superstore_features"
WB = "/api/workbooks/features"
IR = load_ir(FEATURES / "golden.ir.json")
STORY = next(s for s in IR.stories if s.name == "銷售故事")
ACTIONS_BOARD = next(d for d in IR.dashboards if d.name == "篩選動作")
STORY_SHEETS = {
    "ws_region_picker",
    "ws_category_detail",
    "ws_segment_detail",
    "ws_region_category_matrix",
    "ws_sales_by_segment",
    "ws_category_mix",  # Sales by Segment 的工具提示中的工作表
}
DIRECTORY = Directory(
    users=[
        DirectoryUser(name="alice", groups=["All Users"], site_role="Viewer"),
        DirectoryUser(name="bob", groups=["All Users"], site_role="Viewer"),
    ]
)


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("stories")
    target = root / "features"
    target.mkdir()
    for name in ("superstore_features.twbx", "cases.yaml", "golden.ir.json"):
        shutil.copy(FEATURES / name, target / name)
    if (FEATURES / "data").is_dir():
        shutil.copytree(FEATURES / "data", target / "data")
    return root


def permissions() -> Permissions:
    """alice 只能看 Story，bob 只能看「圖表擴充」dashboard（不以分頁顯示工作表）。"""

    def read(user: str) -> list[dict[str, Any]]:
        return [{"grantee": {"kind": "user", "name": user}, "capabilities": {"Read": "Allow"}}]

    return Permissions.model_validate(
        {
            "projects": [{"id": "p1", "name": "Sales"}],
            "workbooks": [
                {
                    "id": "w1",
                    "name": "superstore_features",
                    "project": "p1",
                    "show_tabs": False,
                    "rules": read("alice") + read("bob"),
                    "views": [
                        {"id": "v1", "name": "銷售故事", "rules": read("alice")},
                        {"id": "v2", "name": "圖表擴充", "rules": read("bob")},
                    ],
                }
            ],
        }
    )


def test_stories_are_listed(fixtures: Path) -> None:
    c = TestClient(create_app(fixtures, ROOT / "no-web-dist"))
    [summary] = c.get("/api/workbooks").json()
    assert summary["stories"] == [{"id": STORY.id, "name": "銷售故事"}]
    ir = c.get(f"{WB}/ir").json()
    assert [s["id"] for s in ir["stories"]] == [STORY.id]


def test_story_permission_opens_its_points(fixtures: Path, tmp_path: Path) -> None:
    settings = AuthSettings(mode="dev", directory=DIRECTORY, permissions=permissions())
    c = TestClient(
        create_app(fixtures, ROOT / "no-web-dist", auth=settings, usage_dir=tmp_path / "usage")
    )
    assert c.post("/auth/dev", json={"username": "alice"}).status_code == 200
    [summary] = c.get("/api/workbooks").json()
    # 清單只列出 Story 本身（與 dashboard 中的工作表相同，故事點的 dashboard 跟著 Story 開放）
    assert (summary["dashboards"], summary["stories"]) == (
        [],
        [{"id": STORY.id, "name": "銷售故事"}],
    )
    assert {w["id"] for w in summary["worksheets"]} == STORY_SHEETS
    ir = c.get(f"{WB}/ir").json()
    assert [d["id"] for d in ir["dashboards"]] == [ACTIONS_BOARD.id]  # 畫故事點需要版面
    assert {w["id"] for w in ir["worksheets"]} == STORY_SHEETS
    assert [s["id"] for s in ir["stories"]] == [STORY.id]
    render = c.post(f"{WB}/worksheets/ws_category_detail/render")
    assert render.status_code == 200
    assert c.post(f"{WB}/worksheets/ws_segment_kpis/render").status_code == 404
    # 使用紀錄：開啟 Story 記一次（切換故事點不算）；單獨開啟故事點的 dashboard 也記一次
    assert c.post(f"{WB}/views/{STORY.id}/visit").status_code == 204
    assert c.post(f"{WB}/views/{ACTIONS_BOARD.id}/visit").status_code == 204
    assert c.post(f"{WB}/views/db_nope/visit").status_code == 404

    assert c.post("/auth/dev", json={"username": "bob"}).status_code == 200
    [summary] = c.get("/api/workbooks").json()
    assert summary["stories"] == []
    ir = c.get(f"{WB}/ir").json()
    assert ir["stories"] == [] and [d["name"] for d in ir["dashboards"]] == ["圖表擴充"]
    assert c.post(f"{WB}/worksheets/ws_category_detail/render").status_code == 404
    assert c.post(f"{WB}/views/{STORY.id}/visit").status_code == 404
    assert c.post(f"{WB}/views/{ACTIONS_BOARD.id}/visit").status_code == 404
    usage = (tmp_path / "usage").glob("*.jsonl")
    records = [
        json.loads(line) for path in usage for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [(r["user"], r["view"], r["kind"]) for r in records] == [
        ("alice", "銷售故事", "story"),
        ("alice", "篩選動作", "dashboard"),
    ]
