import zipfile
from pathlib import Path

import pytest
from lxml import etree

from tmig.parser.twbx import (
    WorkbookFormatError,
    attr,
    child,
    children,
    descendants,
    load_workbook_package,
    local_name,
    xpath,
)

DATA = Path(__file__).parent / "data"
PILOT = DATA / "pilot.twb"

FCP_XML = b"""<workbook xmlns:user='http://www.tableausoftware.com/xml/user'>
  <connection>
    <_.fcp.Feature.false...relation name='old' type='table' />
    <_.fcp.Feature.true...relation name='new' type='join'>
      <relation name='inner' type='table' />
    </_.fcp.Feature.true...relation>
    <_.fcp.Feature.false...wrapper><relation name='hidden' type='table' /></_.fcp.Feature.false...wrapper>
    <!-- comment -->
    <groupfilter user:ui-enumeration='inclusive' function='union' />
  </connection>
</workbook>"""


def make_twbx(tmp_path: Path, members: dict[str, bytes], name: str = "report.twbx") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return path


def test_fcp_elements_are_normalized_and_inactive_ones_skipped() -> None:
    root = etree.fromstring(FCP_XML)
    connection = child(root, "connection")
    relations = children(connection, "relation")
    assert [r.get("name") for r in relations] == ["new"]
    assert local_name(relations[0]) == "relation"
    assert [r.get("name") for r in descendants(root, "relation")] == ["new", "inner"]


def test_attr_ignores_namespace() -> None:
    root = etree.fromstring(FCP_XML)
    group = next(descendants(root, "groupfilter"))
    assert attr(group, "ui-enumeration") == "inclusive"
    assert attr(group, "function") == "union"
    assert attr(group, "missing") is None


def test_xpath() -> None:
    package = load_workbook_package(PILOT)
    worksheet = children(child(package.root, "worksheets"), "worksheet")[1]
    assert xpath(worksheet) == "/workbook/worksheets/worksheet[2]"


def test_loads_twb() -> None:
    package = load_workbook_package(PILOT)
    assert package.twb_name == "pilot.twb"
    assert package.files == ()


def test_loads_twbx_and_lists_other_files(tmp_path: Path) -> None:
    path = make_twbx(
        tmp_path,
        {
            "Sales.twb": PILOT.read_bytes(),
            "Data/Extracts/x.hyper": b"\0" * 2048,
            "Image/logo.png": b"png",
        },
    )
    package = load_workbook_package(path)
    assert package.twb_name == "Sales.twb"
    assert [(f.name, f.size) for f in package.files] == [
        ("Data/Extracts/x.hyper", 2048),
        ("Image/logo.png", 3),
    ]


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ({"Data/x.hyper": b""}, "找不到 .twb"),
        ({"a.twb": b"<workbook/>", "b.twb": b"<workbook/>"}, "多個 .twb"),
        ({"a.twb": b"<workbook>"}, "不是有效的 XML"),
        ({"a.twb": b"<datasource/>"}, "根元素不是 <workbook>"),
    ],
)
def test_bad_twbx(tmp_path: Path, members: dict[str, bytes], message: str) -> None:
    with pytest.raises(WorkbookFormatError, match=message):
        load_workbook_package(make_twbx(tmp_path, members))


def test_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "broken.twbx"
    path.write_bytes(b"not a zip")
    with pytest.raises(WorkbookFormatError, match=r"不是有效的 \.twbx"):
        load_workbook_package(path)


def test_wrong_suffix_and_missing_file(tmp_path: Path) -> None:
    other = tmp_path / "report.tds"
    other.write_text("<datasource/>", encoding="utf-8")
    with pytest.raises(WorkbookFormatError, match="不支援的副檔名"):
        load_workbook_package(other)
    with pytest.raises(WorkbookFormatError, match="找不到檔案"):
        load_workbook_package(tmp_path / "missing.twbx")


def test_external_entities_are_not_resolved(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    twb = tmp_path / "evil.twb"
    twb.write_text(
        f"<?xml version='1.0'?><!DOCTYPE workbook [<!ENTITY x SYSTEM '{secret.as_uri()}'>]>"
        "<workbook><datasources>&x;</datasources></workbook>",
        encoding="utf-8",
    )
    try:
        package = load_workbook_package(twb)
    except WorkbookFormatError:
        return
    assert "TOP-SECRET" not in etree.tostring(package.root, encoding="unicode")
