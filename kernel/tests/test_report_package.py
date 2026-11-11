"""報表套件（.tmigx）的格式檢查：版本、成員路徑、萃取資料與圖片。"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from tmig.ir import load_ir
from tmig.package import (
    DATASOURCE_KIND,
    PackageError,
    datasource_ir,
    member_path,
    package_kind,
    parameters_used,
    read_package,
    write_package,
)

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "fixtures" / "superstore_demo"
FEATURES = ROOT / "fixtures" / "superstore_features"


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    """示範報表的目錄（golden.ir.json＋data/），當作要存成套件的報表。"""
    directory = tmp_path / "demo"
    directory.mkdir()
    shutil.copy(DEMO / "golden.ir.json", directory / "golden.ir.json")
    shutil.copytree(DEMO / "data", directory / "data")
    return directory


def test_round_trip(demo: Path, tmp_path: Path) -> None:
    ir = load_ir(demo / "golden.ir.json")
    info = write_package(tmp_path / "demo.tmigx", ir, demo)
    assert info.datasources == ["ds_orders"]
    assert info.size == (tmp_path / "demo.tmigx").stat().st_size
    assert not (tmp_path / "demo.tmigx.tmp").exists()
    opened = tmp_path / "opened"
    assert read_package(tmp_path / "demo.tmigx", opened) == ir
    assert (opened / "golden.ir.json").read_text(encoding="utf-8") == (
        demo / "golden.ir.json"
    ).read_text(encoding="utf-8")
    assert (opened / "data" / "ds_orders.duckdb").read_bytes() == (
        demo / "data" / "ds_orders.duckdb"
    ).read_bytes()


def test_datasource_package(demo: Path, tmp_path: Path) -> None:
    """資料源套件：只有一個資料源與它用到的參數，沒有工作表；manifest 的 kind 為 datasource。"""
    features = load_ir(FEATURES / "golden.ir.json")
    orders = next(d for d in features.datasources if d.id == "ds_orders")
    assert parameters_used(features, orders) == {"p_selected_region"}
    single = datasource_ir(features, "ds_orders")
    assert [d.id for d in single.datasources] == ["ds_orders"]
    assert [p.id for p in single.parameters] == ["p_selected_region"]
    assert single.worksheets == [] and single.dashboards == [] and single.actions == []
    assert single.workbook.name == orders.caption
    with pytest.raises(PackageError, match="nope"):
        datasource_ir(features, "nope")

    ir = load_ir(demo / "golden.ir.json")
    target = tmp_path / "orders.tmigds"
    write_package(target, datasource_ir(ir, "ds_orders"), demo, kind=DATASOURCE_KIND)
    assert package_kind(target) == "datasource"
    opened = tmp_path / "opened"
    assert read_package(target, opened).datasources == ir.datasources
    assert (opened / "data" / "ds_orders.duckdb").is_file()
    with pytest.raises(PackageError, match="只能有一個資料源"):
        write_package(tmp_path / "bad.tmigds", ir, demo, kind=DATASOURCE_KIND)
    # 報表套件與不是平台的檔案
    write_package(tmp_path / "demo.tmigx", ir, demo)
    assert package_kind(tmp_path / "demo.tmigx") == "report"
    (tmp_path / "x.tdsx").write_bytes(b"not a zip")
    assert package_kind(tmp_path / "x.tdsx") is None


def test_extract_data_is_required(demo: Path, tmp_path: Path) -> None:
    ir = load_ir(demo / "golden.ir.json")
    shutil.rmtree(demo / "data")
    with pytest.raises(PackageError, match="ds_orders"):
        write_package(tmp_path / "demo.tmigx", ir, demo)
    assert not (tmp_path / "demo.tmigx").exists()


def _package(path: Path, manifest: object, members: dict[str, bytes] | None = None) -> Path:
    ir = (DEMO / "golden.ir.json").read_bytes()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("report.ir.json", ir)
        for name, content in (members or {}).items():
            archive.writestr(name, content)
    return path


def test_rejects_other_formats_and_newer_versions(tmp_path: Path) -> None:
    other = _package(tmp_path / "a.tmigx", {"format": "zip", "version": 1})
    with pytest.raises(PackageError, match="格式不是 tmigx"):
        read_package(other, tmp_path / "a")
    newer = _package(tmp_path / "b.tmigx", {"format": "tmigx", "version": 99})
    with pytest.raises(PackageError, match="請更新桌面版"):
        read_package(newer, tmp_path / "b")
    bad = _package(tmp_path / "c.tmigx", {"format": "tmigx", "version": True})
    with pytest.raises(PackageError, match="格式版本不正確"):
        read_package(bad, tmp_path / "c")


def test_members_cannot_escape_the_report_folder(tmp_path: Path) -> None:
    """zip slip：../、絕對路徑與磁碟代號一律略過；不認得的資料檔也略過。"""
    package = _package(
        tmp_path / "evil.tmigx",
        {"format": "tmigx", "version": 1},
        {
            "files/../../escaped.png": b"x",
            "files//etc/passwd.png": b"x",
            "files/C:/windows/evil.png": b"x",
            "files/Image/ok.png": b"ok",
            "data/../escaped.duckdb": b"x",
            "data/not_a_datasource.duckdb": b"x",
        },
    )
    target = tmp_path / "opened"
    read_package(package, target)
    assert (target / "files" / "Image" / "ok.png").read_bytes() == b"ok"
    assert not list(target.rglob("passwd.png"))  # files/ 之後是絕對路徑：略過
    assert not (tmp_path / "escaped.png").exists()
    assert not (tmp_path / "escaped.duckdb").exists()
    assert not (target / "data" / "not_a_datasource.duckdb").exists()
    assert not list(target.rglob("evil.png"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Image/a.png", "Image/a.png"),
        ("Image\\a.png", "Image/a.png"),
        ("./Image//a.png", "Image/a.png"),
        ("../a.png", None),
        ("Image/../../a.png", None),
        ("/abs/a.png", None),
        ("C:/a.png", None),
        ("", None),
    ],
)
def test_member_path(raw: str, expected: str | None) -> None:
    result = member_path(raw)
    assert (str(result) if result is not None else None) == expected
