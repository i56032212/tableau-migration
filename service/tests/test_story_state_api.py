"""故事點記下的工作表狀態（排序、座標軸、註解、篩選動作的所有值）：檢視器查詢時帶著故事點（QueryState.story），
查詢服務以套用後的工作表查詢與繪圖；沒有帶著故事點時是工作表本身。

以功能報表的「銷售故事」為例：加上一個顯示 Sales by Segment 的故事點，記下 Segment 遞減的字母排序、
SUM(Sales) 座標軸的固定範圍與標題，與一個點的註解。
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

from tmig.ir import Annotation, FieldUse, Sort, StoryPoint, StorySheet, TextRun, load_ir, save_ir
from tmig.ir.models import AnnotationPoint, Axis
from tmig_service import create_app

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "fixtures" / "superstore_features"
WB = "/api/workbooks/features"
SALES = FieldUse(field="f_sales", agg="sum", continuous=True)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    root = tmp_path_factory.mktemp("story-states")
    target = root / "features"
    target.mkdir()
    for name in ("superstore_features.twbx", "cases.yaml"):
        shutil.copy(FEATURES / name, target / name)
    if (FEATURES / "data").is_dir():
        shutil.copytree(FEATURES / "data", target / "data")
    ir = load_ir(FEATURES / "golden.ir.json")
    story = ir.stories[0]
    point = StoryPoint(
        id="sp9",
        caption="依名稱遞減",
        worksheet="ws_sales_by_segment",
        sheets=[
            StorySheet(
                worksheet="ws_sales_by_segment",
                sorts=[Sort(field="f_segment", kind="alphabetic", order="desc")],
                axes=[Axis(use=SALES, shelf="columns", start=0, end=2000000, title="營收")],
                annotations=[
                    Annotation(
                        kind="point",
                        text=[TextRun(text="最高的一段")],
                        point=AnnotationPoint(x=1000000, x_axis=SALES),
                    )
                ],
            )
        ],
    )
    story.points.append(point)
    save_ir(ir, target / "golden.ir.json")
    return TestClient(create_app(root, ROOT / "no-web-dist"))


def segments(spec: dict[str, Any]) -> list[Any]:
    values: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, dict) and isinstance(data.get("values"), list):
                for row in data["values"]:
                    label = row.get("c0_label")
                    if isinstance(label, str) and label not in values:
                        values.append(label)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec)
    return values


def texts(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("title", "text") and isinstance(value, str):
                found.append(value)
            found += texts(value)
    elif isinstance(node, list):
        for item in node:
            found += texts(item)
    return found


def test_render_applies_the_story_point(client: TestClient) -> None:
    plain = client.post(f"{WB}/worksheets/ws_sales_by_segment/render", json={})
    assert plain.status_code == 200
    story_id = load_ir(FEATURES / "golden.ir.json").stories[0].id
    state = {"story": {"story": story_id, "point": "sp9"}}
    told = client.post(f"{WB}/worksheets/ws_sales_by_segment/render", json=state)
    assert told.status_code == 200, told.text
    before, after = segments(plain.json()["spec"]), segments(told.json()["spec"])
    assert before == sorted(before) and len(before) >= 2
    assert after == sorted(before, reverse=True)
    # 座標軸的標題與註解只在故事點中
    assert "營收" in texts(told.json()["spec"]) and "營收" not in texts(plain.json()["spec"])
    assert "最高的一段" in str(told.json()["spec"]) and "最高的一段" not in str(
        plain.json()["spec"]
    )


def test_unknown_story_point_is_rejected(client: TestClient) -> None:
    story_id = load_ir(FEATURES / "golden.ir.json").stories[0].id
    response = client.post(
        f"{WB}/worksheets/ws_sales_by_segment/render",
        json={"story": {"story": story_id, "point": "sp_missing"}},
    )
    assert response.status_code == 422
    assert "找不到故事點" in response.json()["detail"]
