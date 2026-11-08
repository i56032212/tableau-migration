"""兩版報表定義（IR）的差異：比較伺服器上的兩個版本、發布時伺服器上已有較新的版本時列出改了什麼。

只比較作者看得到的元素（資料源、欄位、參數、工作表、dashboard、動作、Story），依 id 對應：
新增、刪除，或內容不同（任何屬性不同都算，例如公式、篩選、版面）。結果只有名稱，不含公式等定義的內容，
只能檢視報表的人也可以看。資料源的變動不含它的欄位（欄位各自列出）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from tmig.ir import IR

Kind = Literal["datasource", "field", "parameter", "worksheet", "dashboard", "action", "story"]
ChangeKind = Literal["added", "removed", "changed"]
ORDER: tuple[Kind, ...] = (
    "datasource",
    "field",
    "parameter",
    "worksheet",
    "dashboard",
    "action",
    "story",
)
KIND_LABELS: dict[Kind, str] = {
    "datasource": "資料源",
    "field": "欄位",
    "parameter": "參數",
    "worksheet": "工作表",
    "dashboard": "dashboard",
    "action": "動作",
    "story": "Story",
}
VERBS: dict[ChangeKind, str] = {"added": "新增", "removed": "刪除", "changed": "修改"}


@dataclass(frozen=True)
class Change:
    kind: Kind
    id: str
    name: str
    change: ChangeKind
    parent: str | None = None
    """欄位所屬的資料源名稱。"""

    def describe(self) -> str:
        where = f"（{self.parent}）" if self.parent else ""
        return f"{VERBS[self.change]}{KIND_LABELS[self.kind]}「{self.name}」{where}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "text": self.describe()}


Element = tuple[str, str | None, Any]
"""名稱、所屬的資料源名稱、內容（比較用）。"""


def _elements(ir: IR) -> dict[tuple[Kind, str], Element]:
    found: dict[tuple[Kind, str], Element] = {}
    for ds in ir.datasources:
        found[("datasource", ds.id)] = (
            ds.caption,
            None,
            ds.model_dump(mode="json", exclude={"fields"}),
        )
        for field in ds.fields:
            found[("field", field.id)] = (field.caption, ds.caption, field.model_dump(mode="json"))
    for parameter in ir.parameters:
        found[("parameter", parameter.id)] = (
            parameter.caption,
            None,
            parameter.model_dump(mode="json"),
        )
    for sheet in ir.worksheets:
        found[("worksheet", sheet.id)] = (sheet.name, None, sheet.model_dump(mode="json"))
    for board in ir.dashboards:
        found[("dashboard", board.id)] = (board.name, None, board.model_dump(mode="json"))
    for action in ir.actions:
        found[("action", action.id)] = (action.caption, None, action.model_dump(mode="json"))
    for story in ir.stories:
        found[("story", story.id)] = (story.name, None, story.model_dump(mode="json"))
    return found


def diff_ir(old: IR, new: IR) -> list[Change]:
    """old → new 的變動：依元素種類，同種類內依新版本中的順序（刪除的排在最後）。"""
    before, after = _elements(old), _elements(new)
    changes: list[Change] = []
    for kind in ORDER:
        for key, (name, parent, content) in after.items():
            if key[0] != kind:
                continue
            if key not in before:
                changes.append(Change(kind, key[1], name, "added", parent))
            elif before[key][2] != content:
                changes.append(Change(kind, key[1], name, "changed", parent))
        for key, (name, parent, _) in before.items():
            if key[0] == kind and key not in after:
                changes.append(Change(kind, key[1], name, "removed", parent))
    return changes


__all__ = ["KIND_LABELS", "Change", "diff_ir"]
