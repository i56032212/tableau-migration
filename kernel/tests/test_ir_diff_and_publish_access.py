"""兩版 IR 的差異（tmig.diff）與發布到 Project 的權限（Permissions.publish_access）。"""

from __future__ import annotations

from pathlib import Path

from tmig.access.permissions import Permissions
from tmig.diff import diff_ir
from tmig.planner.identity import Identity
from tmig.verify.run import load_fixture_ir

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"


def test_diff_lists_added_removed_and_changed_by_name() -> None:
    old = load_fixture_ir(DEMO)
    assert diff_ir(old, old) == []
    first, second, *rest = old.worksheets
    datasource = old.datasources[0]
    field = datasource.fields[0]
    renamed_field = field.model_copy(update={"caption": f"{field.caption}（新）"})
    new = old.model_copy(
        update={
            "worksheets": [first.model_copy(update={"name": "改過的名稱"}), *rest],
            "datasources": [
                datasource.model_copy(update={"fields": [renamed_field, *datasource.fields[1:]]}),
                *old.datasources[1:],
            ],
        }
    )
    changes = diff_ir(old, new)
    summary = [(c.kind, c.change, c.name) for c in changes]
    assert summary == [
        ("field", "changed", renamed_field.caption),
        ("worksheet", "changed", "改過的名稱"),
        ("worksheet", "removed", second.name),
    ]
    assert changes[0].parent == datasource.caption
    assert changes[2].describe() == f"刪除工作表「{second.name}」"
    added = diff_ir(new, old)
    assert ("worksheet", "added", second.name) in [(c.kind, c.change, c.name) for c in added]


PERMISSIONS = Permissions.model_validate(
    {
        "projects": [
            {
                "id": "root",
                "name": "Finance",
                "owner": "olivia",
                "content_permissions": "LockedToProject",
                "rules": [
                    {
                        "grantee": {"kind": "group", "name": "Analysts"},
                        "capabilities": {"Write": "Allow"},
                    },
                    {"grantee": {"kind": "user", "name": "dan"}, "capabilities": {"Write": "Deny"}},
                ],
            },
            {
                "id": "child",
                "name": "月報",
                "parent": "root",
                "rules": [
                    {"grantee": {"kind": "user", "name": "eve"}, "capabilities": {"Write": "Allow"}}
                ],
            },
            {
                "id": "open",
                "name": "Sandbox",
                "rules": [
                    {"grantee": {"kind": "user", "name": "eve"}, "capabilities": {"Write": "Allow"}}
                ],
            },
        ]
    }
)


def who(name: str, *groups: str, role: str | None = "Creator") -> Identity:
    return Identity(username=name, groups=groups, site_role=role)


def test_publish_access_follows_tableau_rules() -> None:
    allowed = PERMISSIONS.publish_access
    assert allowed(who("amy", "Analysts"), "root").allowed
    # 上層鎖定權限（套用到巢狀）：子 Project 用上層的規則
    assert allowed(who("amy", "Analysts"), "child").allowed
    assert not allowed(who("eve"), "child").allowed
    assert allowed(who("eve"), "open").allowed
    # 使用者本人的拒絕優先於群組的允許
    assert not allowed(who("dan", "Analysts"), "root").allowed
    # 擁有者與管理員
    assert allowed(who("olivia"), "child").allowed
    assert allowed(who("root-admin", role="SiteAdministratorCreator"), "open").allowed
    # 站台角色不能發布：Viewer、Explorer 即使規則允許也不行；未授權一律拒絕
    viewer = allowed(who("amy", "Analysts", role="Viewer"), "root")
    assert not viewer.allowed and "Viewer" in viewer.reason
    assert not allowed(who("amy", "Analysts", role="Explorer"), "root").allowed
    assert allowed(who("amy", "Analysts", role="ExplorerCanPublish"), "root").allowed
    assert not allowed(who("amy", "Analysts", role="Unlicensed"), "root").allowed
    # 沒有站台角色（沒有使用者目錄）時不限制角色
    assert allowed(who("amy", "Analysts", role=None), "root").allowed
    assert not allowed(who("amy", "Analysts"), "missing").allowed
