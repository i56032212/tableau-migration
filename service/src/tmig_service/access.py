"""權限（第三階段 P3）：查詢服務依 Tableau Server 匯出的權限決定登入者看得到哪些報表與 view。

設定檔的 [auth] 加上 ``permissions = "permissions.json"``（tools/tableau_server 的 permissions 匯出）後：

- 報表清單只列出有檢視權限（Read）的報表；沒有權限的報表一律回應 404（不透露報表存在）
- workbook 不以分頁顯示工作表時，只能看有權限的 view（dashboard 中的工作表跟著 dashboard，
  Story 的故事點顯示的 dashboard 與工作表跟著 Story）
- 報表的定義（IR）只給有「下載 workbook」權限（ExportXml）的人完整版本；其他人拿到的版本不含連線、
  計算公式、資料源篩選與原始 XML（檢視器只需要版面、欄位名稱與篩選）
- 報表對應到 Tableau 的 workbook：cases.yaml 的 tableau_views_url 中的網址名稱，其次是報表名稱；
  權限檔中找不到的報表只有管理員看得到
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tmig.access.permissions import ADMIN_ROLES, Access, Permissions, WorkbookEntry
from tmig.catalog import workbook_url_name
from tmig.ir import IR, iter_zones
from tmig.planner.identity import Identity


@dataclass(frozen=True)
class Grant:
    """登入者對一份報表的存取。"""

    allowed: bool
    views: frozenset[str] | None = None
    """可以看的 view 名稱；None 表示全部。"""
    full_definition: bool = True
    """可以取得完整的報表定義（下載 workbook 的權限）。"""

    def dashboard_allowed(self, name: str) -> bool:
        return self.views is None or name in self.views

    def story_allowed(self, name: str) -> bool:
        return self.views is None or name in self.views

    def story_dashboards(self, ir: IR) -> set[str]:
        """可以看的 Story 的故事點顯示的 dashboard id（與 dashboard 中的工作表跟著 dashboard 相同，
        跟著 Story 開放；報表清單只列出 Story 本身）。"""
        return {
            point.dashboard
            for story in ir.stories
            if self.story_allowed(story.name)
            for point in story.points
            if point.dashboard is not None
        }

    def worksheet_allowed(self, ir: IR, worksheet_id: str) -> bool:
        """工作表本身是可以看的 view，或在可以看的 dashboard、Story 中。"""
        if self.views is None:
            return True
        sheet = next(
            (w for w in ir.worksheets if w.id == worksheet_id or w.name == worksheet_id), None
        )
        if sheet is None:
            return False
        if sheet.name in self.views:
            return True
        in_story = self.story_dashboards(ir)
        if any(
            point.worksheet == sheet.id
            for story in ir.stories
            if self.story_allowed(story.name)
            for point in story.points
        ):
            return True
        return any(
            zone.worksheet == sheet.id
            for board in ir.dashboards
            if board.name in self.views or board.id in in_story
            for zone in iter_zones(board.zones)
        )


OPEN = Grant(allowed=True)


class Authorizer:
    def __init__(self, permissions: Permissions | None) -> None:
        self.permissions = permissions

    def entry(self, directory: Path, ir: IR) -> WorkbookEntry | None:
        assert self.permissions is not None
        return self.permissions.workbook(workbook_url_name(directory), ir.workbook.name)

    def grant(self, user: Identity | None, directory: Path, ir: IR) -> Grant:
        """沒有權限檔時不限制（只要求登入）；有權限檔時依 Tableau 的規則判斷。"""
        if self.permissions is None or user is None:
            return OPEN
        entry = self.entry(directory, ir)
        if entry is None:
            admin = (user.site_role or "") in ADMIN_ROLES
            return Grant(allowed=admin, full_definition=admin)
        access: Access = self.permissions.access(user, entry, "Read")
        if not access.allowed:
            return Grant(allowed=False)
        download = self.permissions.access(user, entry, "ExportXml").allowed
        return Grant(allowed=True, views=access.views, full_definition=download)


def viewer_ir(ir: IR, grant: Grant) -> dict[str, Any]:
    """依權限給檢視器的報表定義：沒有下載權限時去掉連線、公式、資料源篩選與原始 XML，
    只能看部分 view 時去掉其他的 dashboard 與工作表。"""
    data = ir.model_dump(mode="json")
    if grant.views is not None:
        boards = {b.id for b in ir.dashboards if grant.dashboard_allowed(b.name)}
        boards |= grant.story_dashboards(ir)
        sheets = {w.id for w in ir.worksheets if grant.worksheet_allowed(ir, w.id)}
        data["dashboards"] = [d for d in data["dashboards"] if d["id"] in boards]
        data["worksheets"] = [w for w in data["worksheets"] if w["id"] in sheets]
        data["stories"] = [s for s in data["stories"] if grant.story_allowed(s["name"])]
    if grant.full_definition:
        return data
    for ds in data["datasources"]:
        ds["connection"] = {"kind": ds["connection"].get("kind")}
        ds["filters"] = []
        for field in ds["fields"]:
            field["calc"] = None
            field["column"] = None
            field["table"] = None
    for item in data["unsupported"]:
        item["xml"] = None
    data["ignored"] = []
    return data


__all__ = ["OPEN", "Authorizer", "Grant", "viewer_ir"]
