"""登入者的身分與列層級權限（第三階段 P1）。

使用者函式（USERNAME、FULLNAME、USERDOMAIN、ISMEMBEROF、ISUSERNAME、ISFULLNAME、USERATTRIBUTE、
USERATTRIBUTEINCLUDES）在產生 SQL 時以登入者的身分代入常值。身分由查詢服務的身分驗證（P2）或
cases.yaml 的 user 提供，不從網址參數或篩選狀態取得；沒有身分時查詢失敗（不會當成看得到全部）。

列層級權限的欄位：用到使用者函式的欄位（含間接參照與 Tableau 的使用者篩選集合）。

- 資料源篩選本來就不受狀態影響
- 工作表上這些欄位的篩選不接受網址參數與篩選控制項的變更（Tableau 的網址參數可以改，是常見的漏洞）；
  例外是報表本身就為這個欄位放了篩選控制項（例如「只看我的客戶」開關），表示作者有意讓使用者調整
- 篩選動作的選取與其他篩選一律以 AND 套用，不會放寬權限，維持與 Tableau 相同
- 參數與 Tableau 相同，使用者可以改；權限計算用到參數時由功能盤點提醒（使用者改參數就能改變可見範圍）
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus

from pydantic import BaseModel, ConfigDict, Field

from tmig.ir.integrity import iter_zones
from tmig.ir.models import IR, Datasource

USER_CALL = re.compile(
    r"\b(USERNAME|FULLNAME|USERDOMAIN|ISMEMBEROF|ISUSERNAME|ISFULLNAME|USERATTRIBUTE|"
    r"USERATTRIBUTEINCLUDES)\s*\(",
    re.IGNORECASE,
)
COMMENT = re.compile(r"//[^\n]*")
ALL_USERS = "all users"
"""Tableau 內建的群組，每個登入者都屬於它。"""


class Identity(BaseModel):
    """登入者，對應 Tableau Server 上的使用者。

    群組名稱與 ISMEMBEROF 的引數比對：本機群組寫名稱，Active Directory 群組寫「網域\\名稱」
    （例如 ``domain.lan\\Sales``，與 Tableau 的寫法相同）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(
        min_length=1, description="USERNAME() 的值（Tableau Server 上的使用者名稱）"
    )
    full_name: str | None = Field(default=None, description="FULLNAME() 的值")
    domain: str | None = Field(default=None, description="USERDOMAIN() 的值")
    groups: tuple[str, ...] = Field(default=(), description="所屬群組，ISMEMBEROF() 依此判斷")
    attributes: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="使用者屬性（USERATTRIBUTE、USERATTRIBUTEINCLUDES；來自 SSO 的宣告）",
    )
    site_role: str | None = Field(
        default=None,
        description="Tableau 的站台角色（權限判斷用，例如 Viewer、SiteAdministratorCreator）",
    )

    def member_of(self, group: str) -> bool:
        """ISMEMBEROF：不分大小寫；引數依 Tableau 的規則先做網址解碼（例如 USERS%2B 代表 USERS+）。"""
        name = unquote_plus(group).casefold()
        return name == ALL_USERS or name in {g.casefold() for g in self.groups}

    def is_username(self, name: str) -> bool:
        return name.casefold() == self.username.casefold()

    def is_full_name(self, name: str) -> bool:
        return self.full_name is not None and name.casefold() == self.full_name.casefold()

    def attribute(self, name: str) -> tuple[str, ...]:
        return self.attributes.get(name, ())


def uses_user_functions(formula: str) -> bool:
    return USER_CALL.search(COMMENT.sub("", formula)) is not None


def user_dependent_fields(ds: Datasource) -> set[str]:
    """用到使用者函式的計算欄位 id（含參照這些欄位的計算欄位）。"""
    calcs = {f.id: f for f in ds.fields if f.calc is not None}
    dependent = {
        fid
        for fid, f in calcs.items()
        if f.calc is not None and uses_user_functions(f.calc.formula)
    }
    changed = True
    while changed:
        changed = False
        names = [calcs[fid].name for fid in dependent]
        for fid, data_field in calcs.items():
            if fid in dependent or data_field.calc is None:
                continue
            formula = COMMENT.sub("", data_field.calc.formula)
            if any(name in formula for name in names):
                dependent.add(fid)
                changed = True
    return dependent


def adjustable_fields(ir: IR, ds: Datasource) -> set[str]:
    """報表為使用者放了篩選控制項的欄位 id（任何 dashboard 上屬於這個資料源工作表的篩選區塊）。"""
    sheets = {w.id for w in ir.worksheets if w.datasource == ds.id}
    return {
        zone.field.field
        for dashboard in ir.dashboards
        for zone in iter_zones(dashboard.zones)
        if zone.kind == "filter" and zone.worksheet in sheets and zone.field is not None
    }


def protected_fields(ir: IR, ds: Datasource) -> set[str]:
    """列層級權限的欄位：用到使用者函式、且報表沒有為它放篩選控制項。"""
    dependent = user_dependent_fields(ds)
    if not dependent:
        return dependent
    return dependent - adjustable_fields(ir, ds)


__all__ = [
    "Identity",
    "adjustable_fields",
    "protected_fields",
    "user_dependent_fields",
    "uses_user_functions",
]
