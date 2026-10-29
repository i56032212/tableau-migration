"""查詢服務測試的共用資料：加上列層級權限的示範報表（P1、P2）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tmig.access.directory import Directory, DirectoryUser
from tmig.ir import load_ir
from tmig.ir.models import Calc, CategoricalFilter, DataField, FieldUse

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
OWNER = 'CASE [Region] WHEN "East" THEN "alice" WHEN "West" THEN "bob" ELSE "carol" END'
DIRECTORY = Directory(
    source="test",
    users=[
        DirectoryUser(
            name="alice",
            full_name="Alice Wang",
            domain="local",
            email="alice.wang@example.com",
            groups=["All Users"],
        ),
        DirectoryUser(name="bob", full_name="Bob Chen", domain="local", groups=["All Users"]),
        DirectoryUser(name="carol", domain="local", groups=["All Users"]),
        DirectoryUser(name="dave", domain="local", groups=["All Users", "Managers"]),
    ],
)
"""Tableau Server 的使用者目錄：dave 屬於 Managers 群組（看得到全部）。"""


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """報表目錄，內含 secure：示範報表加上資料源篩選 [Visible]（負責人是登入者，或屬於 Managers 群組）。

    負責人：East → alice、West → bob、其他 → carol。
    """
    root = tmp_path_factory.mktemp("fixtures")
    target = root / "secure"
    target.mkdir()
    for name in ("superstore_demo.twbx", "cases.yaml"):
        shutil.copy(DEMO / name, target / name)
    if (DEMO / "data").is_dir():
        shutil.copytree(DEMO / "data", target / "data")
    ir = load_ir(DEMO / "golden.ir.json")
    ds = ir.datasources[0]
    visible = DataField(
        id="f_visible",
        name="[Visible]",
        caption="Visible",
        role="dimension",
        datatype="boolean",
        calc=Calc(formula=f'{OWNER} = USERNAME() OR ISMEMBEROF("Managers")'),
        source_ref="test",
    )
    rls = CategoricalFilter(
        kind="categorical",
        id="flt_visible",
        field=FieldUse(field="f_visible"),
        stage="datasource",
        mode="include",
        values=[True],
        source_ref="test",
    )
    secured = ds.model_copy(update={"fields": [*ds.fields, visible], "filters": [*ds.filters, rls]})
    ir = ir.model_copy(update={"datasources": [secured]})
    (target / "golden.ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
    return root
