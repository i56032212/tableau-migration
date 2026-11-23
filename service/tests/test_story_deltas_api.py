"""故事點記下的欄列架上加上的欄位、量值與 Top N 篩選、移除的篩選（第三批）：檢視器帶著故事點查詢時，查詢
服務以套用後的工作表查詢（tmig.story）；沒有帶著故事點時是工作表本身。

以功能報表為例：「Top Sub-Categories」的 Top 3（排除 Phones）在故事點中改成 Top 5、不排除；「Sales by
Segment」（只有 Consumer、Corporate，排除 Central）在故事點中移除 Segment 的篩選、在列架上加上 Region，
並只留 SUM(Sales) 大於 0 的標記。
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from tmig.ir import FieldUse, StoryPoint, StorySheet, StoryShelfField, load_ir, save_ir
from tmig.ir.models import RangeFilter, TopNFilter
from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "fixtures" / "superstore_features"
WB = "/api/workbooks/features"
SALES = FieldUse(field="f_sales", agg="sum", continuous=True)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    root = tmp_path_factory.mktemp("story-deltas")
    target = root / "features"
    target.mkdir()
    for name in ("superstore_features.twbx", "cases.yaml"):
        shutil.copy(FEATURES / name, target / name)
    if (FEATURES / "data").is_dir():
        shutil.copytree(FEATURES / "data", target / "data")
    ir = load_ir(FEATURES / "golden.ir.json")
    top = TopNFilter(
        kind="top_n",
        id="flt_story_top",
        field=FieldUse(field="f_sub_category"),
        stage="top_n",
        direction="top",
        count=5,
        by="SUM([Sales])",
        source_ref="story",
    )
    positive = RangeFilter(
        kind="range", id="flt_story_sales", field=SALES, stage="measure", min=0, source_ref="story"
    )
    ir.stories[0].points += [
        StoryPoint(
            id="sp8",
            caption="前五名",
            worksheet="ws_top_sub_categories",
            sheets=[StorySheet(worksheet="ws_top_sub_categories", filters=[top])],
        ),
        StoryPoint(
            id="sp9",
            caption="各區",
            worksheet="ws_sales_by_segment",
            sheets=[
                StorySheet(
                    worksheet="ws_sales_by_segment",
                    added_rows=[StoryShelfField(use=FieldUse(field="f_region"), index=1)],
                    filters=[positive],
                    removed_filters=["flt_segment"],
                )
            ],
        ),
    ]
    save_ir(ir, target / "golden.ir.json")
    return TestClient(create_app(root, ROOT / "no-web-dist"))


def records(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """規格中資料列最多的那份資料（標記）。"""
    found: list[list[dict[str, Any]]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, dict) and isinstance(data.get("values"), list):
                found.append(data["values"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec)
    return max(found, key=len) if found else []


def labels(spec: dict[str, Any], channel: str) -> set[str]:
    dims = spec["usermeta"]["dimensions"]
    column = next(c for c, key in dims.items() if key == channel)
    return {r[f"{column}_label"] for r in records(spec) if f"{column}_label" in r}


def render(client: TestClient, sheet: str, point: str | None = None) -> dict[str, Any]:
    story = load_ir(FEATURES / "golden.ir.json").stories[0].id
    state = {"story": {"story": story, "point": point}} if point else {}
    response = client.post(f"{WB}/worksheets/{sheet}/render", json=state)
    assert response.status_code == 200, response.text
    body = response.json()
    assert not body.get("unsupported"), body.get("unsupported")
    return body["spec"]


def test_story_top_n_change(client: TestClient) -> None:
    """Top 3（排除 Phones）在故事點中是 Top 5：多出兩個子類別、Phones 也在其中（銷售最高的子類別之一）。"""
    plain = labels(render(client, "ws_top_sub_categories"), "f_sub_category")
    told = labels(render(client, "ws_top_sub_categories", "sp8"), "f_sub_category")
    assert len(plain) == 3 and "Phones" not in plain
    assert len(told) == 5 and "Phones" in told and plain < told


def test_story_added_field_and_removed_filter(client: TestClient) -> None:
    """移除 Segment 的篩選（三個區隔都出現）、列架上加上 Region（Central 仍被排除）。"""
    plain = render(client, "ws_sales_by_segment")
    told = render(client, "ws_sales_by_segment", "sp9")
    assert labels(plain, "f_segment") == {"Consumer", "Corporate"}
    assert "f_region" not in plain["usermeta"]["dimensions"].values()
    assert labels(told, "f_segment") == {"Consumer", "Corporate", "Home Office"}
    assert labels(told, "f_region") == {"East", "South", "West"}
    assert len(records(told)) == 9
