"""權限（P3）：依 Tableau 的權限評估規則判斷登入者看不看得到報表與 view。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.access.permissions import (
    Grantee,
    Permissions,
    PermissionsError,
    ProjectEntry,
    Rule,
    ViewEntry,
    WorkbookEntry,
    decide,
    load_permissions,
)
from tmig.planner.identity import Identity


def rule(kind: str, name: str, domain: str | None = None, **capabilities: str) -> Rule:
    return Rule.model_validate(
        {"grantee": {"kind": kind, "name": name, "domain": domain}, "capabilities": capabilities}
    )


def user(name: str, *groups: str, role: str = "Viewer", domain: str | None = None) -> Identity:
    return Identity(username=name, groups=("All Users", *groups), site_role=role, domain=domain)


def test_rule_precedence() -> None:
    sales = rule("group", "Sales", Read="Allow")
    contractors = rule("group", "Contractors", Read="Deny")
    amy_allow = rule("user", "Amy", Read="Allow")
    amy_deny = rule("user", "amy", Read="Deny")
    amy = user("amy", "Sales", "Contractors")
    assert decide([sales], amy, "Read")
    assert not decide([sales, contractors], amy, "Read")  # 任一群組拒絕就拒絕
    assert decide([sales, contractors, amy_allow], amy, "Read")  # 本人的規則優先於群組
    assert not decide([sales, amy_deny], amy, "Read")
    assert not decide([], amy, "Read")  # 沒有設定：拒絕
    assert not decide([sales], amy, "ExportXml")
    assert decide(
        [rule("group", "all users", Read="Allow")], user("x"), "Read"
    )  # 每個人都在 All Users
    # 使用者規則寫了網域時，網域也要相符
    assert not decide(
        [rule("user", "amy", "CORP", Read="Allow")], user("amy", domain="local"), "Read"
    )
    assert decide([rule("user", "amy", "corp", Read="Allow")], user("amy", domain="CORP"), "Read")


@pytest.fixture
def permissions() -> Permissions:
    return Permissions(
        projects=[
            ProjectEntry(
                id="root",
                name="Sales",
                owner="olga",
                content_permissions="LockedToProject",
                rules=[rule("group", "Sales Leads", ProjectLeader="Allow")],
                workbook_defaults=[rule("group", "Sales", Read="Allow")],
            ),
            ProjectEntry(id="child", name="EMEA", parent="root"),
            ProjectEntry(
                id="open",
                name="Open",
                content_permissions="LockedToProjectWithoutNested",
                workbook_defaults=[rule("group", "All Users", Read="Allow")],
            ),
            ProjectEntry(id="nested", name="Nested", parent="open"),
        ],
        workbooks=[
            WorkbookEntry(
                id="w1",
                name="Pipeline",
                content_url="Pipeline",
                project="child",
                owner="wendy",
                rules=[rule("group", "All Users", Read="Allow")],
            ),
            WorkbookEntry(
                id="w2",
                name="Board",
                content_url="Board",
                project="nested",
                show_tabs=False,
                rules=[rule("group", "Finance", Read="Allow", ExportXml="Allow")],
                views=[
                    ViewEntry(
                        id="v1", name="Summary", rules=[rule("group", "Finance", Read="Allow")]
                    ),
                    ViewEntry(id="v2", name="Detail", rules=[rule("user", "fred", Read="Allow")]),
                ],
            ),
            WorkbookEntry(id="w3", name="Dup", project="open"),
            WorkbookEntry(id="w4", name="Dup", project="open"),
        ],
    )


def test_locked_project_uses_defaults(permissions: Permissions) -> None:
    pipeline = permissions.workbook("Pipeline")
    assert pipeline is not None
    # 上層 Project 鎖定權限：巢狀 Project 中的 workbook 也用上層的 workbook 預設權限，自己的規則無效
    assert [r.grantee.name for r in permissions.workbook_rules(pipeline)] == ["Sales"]
    assert permissions.access(user("sam", "Sales"), pipeline).allowed
    assert not permissions.access(user("ann"), pipeline).allowed
    # LockedToProjectWithoutNested 不套用到巢狀的 Project
    board = permissions.workbook("Board")
    assert board is not None
    assert [r.grantee.name for r in permissions.workbook_rules(board)] == ["Finance"]


def test_owners_leaders_and_admins(permissions: Permissions) -> None:
    pipeline = permissions.workbook("Pipeline")
    assert pipeline is not None
    assert permissions.access(user("wendy"), pipeline).allowed  # workbook 的擁有者
    assert permissions.access(user("OLGA"), pipeline).allowed  # 上層 Project 的擁有者
    assert permissions.access(user("lee", "Sales Leads"), pipeline).allowed  # 上層的 Project Leader
    assert permissions.access(user("root", role="SiteAdministratorCreator"), pipeline).allowed
    unlicensed = permissions.access(user("wendy", role="Unlicensed"), pipeline)
    assert not unlicensed.allowed and "Unlicensed" in unlicensed.reason


def test_view_permissions(permissions: Permissions) -> None:
    board = permissions.workbook("Board")
    assert board is not None
    finance = permissions.access(user("fin", "Finance"), board)
    assert finance.allowed and finance.views == frozenset({"Summary"})
    fred = permissions.access(user("fred", "Finance"), board)
    assert fred.views == frozenset({"Summary", "Detail"})
    assert not permissions.access(user("fred"), board).allowed  # 沒有 workbook 的檢視權限
    assert permissions.access(user("fin", "Finance"), board, "ExportXml").allowed


def test_workbook_lookup(permissions: Permissions) -> None:
    assert permissions.workbook("Pipeline") is not None
    assert permissions.workbook(None, "Pipeline") is not None
    assert permissions.workbook("nope", "Board") is not None  # 網址名稱找不到時以名稱
    assert permissions.workbook(None, "Dup") is None  # 名稱重複：無法判斷
    assert permissions.workbook(None, None) is None


def test_load_permissions(tmp_path: Path, permissions: Permissions) -> None:
    path = tmp_path / "permissions.json"
    path.write_text(permissions.model_dump_json(), encoding="utf-8")
    assert load_permissions(path) == permissions
    with pytest.raises(PermissionsError, match="找不到權限檔"):
        load_permissions(tmp_path / "none.json")
    path.write_text('{"workbooks": [{"id": "x"}]}', encoding="utf-8")
    with pytest.raises(PermissionsError, match="格式錯誤"):
        load_permissions(path)
    assert Grantee(kind="group", name="Sales") == Grantee(kind="group", name="Sales")
