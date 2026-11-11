"""權限（第三階段 P3）：查詢服務依 Tableau Server 匯出的權限決定登入者看得到哪些報表與 view。

設定檔的 [auth] 加上 ``permissions = "permissions.json"``（tools/tableau_server 的 permissions 匯出）後：

- 報表清單只列出有檢視權限（Read）的報表；沒有權限的報表一律回應 404（不透露報表存在）
- workbook 不以分頁顯示工作表時，只能看有權限的 view（dashboard 中的工作表跟著 dashboard，
  Story 的故事點顯示的 dashboard 與工作表跟著 Story）
- 報表的定義（IR）只給有「下載 workbook」權限（ExportXml）的人完整版本；其他人拿到的版本不含連線、
  計算公式、資料源篩選與原始 XML（檢視器只需要版面、欄位名稱與篩選）
- 資料源分頁（連線、資料表、欄位與資料源篩選）與完整版本的定義相同要「下載 workbook」；其中的
  資料預覽（原始列）另外要「檢視基礎資料」（ViewUnderlyingData），一樣套用資料源篩選與列層級權限
- 報表對應到 Tableau 的 workbook：cases.yaml 的 tableau_views_url 中的網址名稱，其次是報表名稱；
  權限檔中找不到的報表只有管理員看得到
- 發布到報表庫的報表（D0）以報表庫記下的 Project、擁有者與權限判斷（見 Rights）；發布要 Project 的
  Publish，覆蓋要報表的 Overwrite（Write），從伺服器下載要 Download Workbook（ExportXml）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tmig.access.permissions import ADMIN_ROLES, Access, Permissions, ProjectEntry, WorkbookEntry
from tmig.catalog import workbook_url_name
from tmig.ir import IR, Worksheet, iter_zones
from tmig.planner.identity import Identity

if TYPE_CHECKING:
    from tmig_service.store import ReportRecord


@dataclass(frozen=True)
class Grant:
    """登入者對一份報表的存取。"""

    allowed: bool
    views: frozenset[str] | None = None
    """可以看的 view 名稱；None 表示全部。"""
    full_definition: bool = True
    """可以取得完整的報表定義（下載 workbook 的權限）。"""
    underlying_data: bool = True
    """可以檢視資料源的原始列（檢視基礎資料的權限）。"""
    capabilities: frozenset[str] | None = None
    """其他的能力（下載圖片／PDF、下載摘要資料、分享自訂檢視、檢視與新增留言）；None 表示全部。"""

    def can(self, capability: str) -> bool:
        if capability == "ExportXml":
            return self.full_definition
        if capability == "ViewUnderlyingData":
            return self.underlying_data
        return self.capabilities is None or capability in self.capabilities

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
        """工作表本身是可以看的 view，或在可以看的 dashboard、Story 中，或在這樣的工作表的工具提示中
        （Viz in Tooltip 的工作表常是隱藏的工作表）。"""
        if self.views is None:
            return True
        sheet = next(
            (w for w in ir.worksheets if w.id == worksheet_id or w.name == worksheet_id), None
        )
        if sheet is None:
            return False
        if self._shown(ir, sheet):
            return True
        return any(
            tip.worksheet == sheet.id and self._shown(ir, source)
            for source in ir.worksheets
            if source.id != sheet.id
            for marks in (source.marks, *(p.marks for p in source.panes))
            for tip in marks.tooltip_sheets
        )

    def _shown(self, ir: IR, sheet: Worksheet) -> bool:
        """工作表本身是可以看的 view，或在可以看的 dashboard、Story 中。"""
        assert self.views is not None
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
VIEWER_CAPABILITIES = ("ExportImage", "ExportData", "ShareView", "ViewComments", "AddComment")
"""檢視器上依權限顯示的功能（與 Tableau 的 workbook 能力相同）。"""

PermissionsSource = Permissions | Callable[[], Permissions | None] | None
"""權限：固定的（匯出的權限檔）或每次取用時才讀的（站台資料，修改後立即生效）。"""


def _current(source: PermissionsSource) -> Permissions | None:
    return source() if callable(source) else source


class Authorizer:
    def __init__(self, permissions: PermissionsSource) -> None:
        self._permissions = permissions

    @property
    def permissions(self) -> Permissions | None:
        return _current(self._permissions)

    def entry(self, directory: Path, ir: IR) -> WorkbookEntry | None:
        assert self.permissions is not None
        return self.permissions.workbook(workbook_url_name(directory), ir.workbook.name)

    def grant(
        self, user: Identity | None, directory: Path, ir: IR, entry: WorkbookEntry | None = None
    ) -> Grant:
        """沒有權限檔時不限制（只要求登入）；有權限檔時依 Tableau 的規則判斷。
        entry：報表庫的報表直接給它的權限，不依目錄名稱與報表名稱找。"""
        if self.permissions is None or user is None:
            return OPEN
        entry = entry or self.entry(directory, ir)
        if entry is None:
            admin = (user.site_role or "") in ADMIN_ROLES
            return Grant(allowed=admin, full_definition=admin, underlying_data=admin)
        access: Access = self.permissions.access(user, entry, "Read")
        if not access.allowed:
            return Grant(allowed=False)
        download = self.permissions.access(user, entry, "ExportXml").allowed
        underlying = self.permissions.access(user, entry, "ViewUnderlyingData").allowed
        capabilities = frozenset(
            c for c in VIEWER_CAPABILITIES if self.permissions.access(user, entry, c).allowed
        )
        return Grant(
            allowed=True,
            views=access.views,
            full_definition=download,
            underlying_data=underlying,
            capabilities=capabilities,
        )


DEFAULT_PROJECT = ProjectEntry(id="default", name="預設")
"""沒有權限檔時唯一的 Project（與 Tableau 站台的 Default 相同）。"""
LOGIN_REQUIRED = Access(allowed=False, reason="發布需要登入：伺服器沒有設定身分驗證時不能發布")
ALLOWED = Access(allowed=True)


class Rights:
    """報表庫的報表：誰能看、發布、覆蓋、下載。沒有權限檔時登入的人都可以；沒有身分驗證時不能發布。"""

    def __init__(self, permissions: PermissionsSource) -> None:
        self._permissions = permissions
        self.authorizer = Authorizer(permissions)

    @property
    def permissions(self) -> Permissions | None:
        return _current(self._permissions)

    def projects(self) -> list[ProjectEntry]:
        return self.permissions.projects if self.permissions is not None else [DEFAULT_PROJECT]

    def project(self, project_id: str) -> ProjectEntry | None:
        return next((p for p in self.projects() if p.id == project_id), None)

    def project_path(self, project_id: str) -> str:
        """Project 的完整名稱，例如「Sales / 華北」。"""
        if self.permissions is None:
            project = self.project(project_id)
            return project.name if project is not None else project_id
        chain = self.permissions.ancestors(project_id)
        return " / ".join(p.name for p in reversed(chain)) if chain else project_id

    def tableau_workbook(self, project_id: str, name: str) -> WorkbookEntry | None:
        """同一個 Project 中同名的 Tableau workbook：第一次發布時對應過去，沿用它的權限。"""
        if self.permissions is None:
            return None
        wanted = name.casefold()
        found = [
            w
            for w in self.permissions.workbooks
            if w.project == project_id and w.name.casefold() == wanted
        ]
        return found[0] if len(found) == 1 else None

    def entry(self, record: ReportRecord) -> WorkbookEntry:
        if self.permissions is not None and record.tableau_workbook:
            found = next(
                (w for w in self.permissions.workbooks if w.id == record.tableau_workbook), None
            )
            if found is not None:
                # Project 與擁有者以報表庫為準（可以在站台管理中移動、換擁有者）
                return found.model_copy(update={"project": record.project, "owner": record.owner})
        return WorkbookEntry(
            id=record.id,
            name=record.name,
            content_url=record.id,
            project=record.project,
            owner=record.owner,
            rules=record.rules,
        )

    def read(self, user: Identity | None, record: ReportRecord, directory: Path, ir: IR) -> Grant:
        if self.permissions is None:
            return OPEN
        return self.authorizer.grant(user, directory, ir, self.entry(record))

    def publish(self, user: Identity | None, project_id: str) -> Access:
        if user is None:
            return LOGIN_REQUIRED
        if self.permissions is None:
            if self.project(project_id) is None:
                return Access(allowed=False, reason="找不到 Project")
            return ALLOWED
        return self.permissions.publish_access(user, project_id)

    def overwrite(self, user: Identity | None, record: ReportRecord) -> Access:
        """覆蓋（發布新版本、回復舊版）：Project 的 Publish 加上報表的 Overwrite。"""
        publish = self.publish(user, record.project)
        if not publish.allowed or self.permissions is None or user is None:
            return publish
        access = self.permissions.access(user, self.entry(record), "Write")
        return access if not access.allowed else ALLOWED

    def takeover(self, user: Identity | None, workbook: WorkbookEntry, project_id: str) -> Access:
        """第一次發布同名的 Tableau workbook（之後沿用它的權限）：與在 Tableau 上覆蓋相同，
        要 Project 的 Publish 加上那份 workbook 的 Overwrite。"""
        publish = self.publish(user, project_id)
        if not publish.allowed or self.permissions is None or user is None:
            return publish
        entry = workbook.model_copy(update={"project": project_id})
        access = self.permissions.access(user, entry, "Write")
        return access if not access.allowed else ALLOWED

    def download(self, user: Identity | None, record: ReportRecord) -> Access:
        if self.permissions is None or user is None:
            return ALLOWED if self.permissions is None else LOGIN_REQUIRED
        return self.permissions.access(user, self.entry(record), "ExportXml")


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


__all__ = [
    "DEFAULT_PROJECT",
    "OPEN",
    "Authorizer",
    "Grant",
    "PermissionsSource",
    "Rights",
    "viewer_ir",
]
