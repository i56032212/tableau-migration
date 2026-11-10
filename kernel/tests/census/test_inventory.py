import csv
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from tmig.census.inventory import (
    find_workbooks,
    inventory_workbook,
    run_inventory,
    unsupported_kind,
)
from tmig.cli import main
from tmig.ir.models import Unsupported

DATA = Path(__file__).parent / "data"
ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "fixtures" / "superstore_demo" / "superstore_demo.twbx"


def demo_without_extract(target: Path) -> Path:
    """示範報表去掉萃取檔：模擬以 includeExtract=false 下載的 .twbx。"""
    with zipfile.ZipFile(DEMO) as source, zipfile.ZipFile(target, "w") as out:
        for info in source.infolist():
            if not info.filename.endswith(".hyper"):
                out.writestr(info, source.read(info.filename))
    return target


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "workbooks"
    (root / "Sales").mkdir(parents=True)
    (root / "Finance").mkdir()
    shutil.copy(DEMO, root / "Sales" / "superstore_demo.twbx")
    shutil.copy(DATA / "pilot.twb", root / "Sales" / "pilot.twb")
    shutil.copy(DATA / "problems.twb", root / "Finance" / "problems.twb")
    (root / "Finance" / "broken.twbx").write_bytes(b"not a zip")
    (root / "Finance" / "notes.txt").write_text("略過", encoding="utf-8")
    return root


def test_find_workbooks_recurses_and_filters(folder: Path) -> None:
    names = [p.name for p in find_workbooks([folder, folder / "Sales" / "pilot.twb"])]
    assert names == ["broken.twbx", "problems.twb", "pilot.twb", "superstore_demo.twbx"]


def test_inventory_rows_and_outputs(folder: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    messages: list[str] = []
    result = run_inventory([folder], out, progress=messages.append)
    rows = {r.name: r for r in result.rows}
    assert len(messages) == 4

    demo = rows["superstore_demo"]
    assert demo.readiness == "ready"
    assert demo.candidate == "B"
    assert demo.grade == "B"
    assert demo.unsupported == 0
    assert demo.datasources == ["嵌入式萃取"]
    assert demo.worksheets == 4 and demo.dashboards == 1

    # pilot.twb 不含資料：萃取檔不在檔案內，但 .twb 本來就不含，起步條件仍符合
    assert rows["pilot"].readiness == "ready"

    problems = rows["problems"]
    assert problems.readiness == "not_eligible"
    assert problems.candidate is None
    assert problems.failed_checks

    broken = rows["broken"]
    assert broken.readiness == "error"
    assert broken.error is not None and "WorkbookFormatError" in broken.error

    for name in ("inventory.md", "inventory.csv", "features.csv"):
        assert (out / name).is_file()
    assert demo.detail is not None
    detail = (out / demo.detail).read_text(encoding="utf-8")
    assert "## 解析結果（寬鬆模式）" in detail
    assert "沒有不支援的元素" in detail

    raw = (out / "inventory.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "Excel 需要 UTF-8 BOM 才能正確顯示中文"
    with (out / "inventory.csv").open(encoding="utf-8-sig", newline="") as fh:
        table = list(csv.DictReader(fh))
    assert [r["名稱"] for r in table][:2] == ["pilot", "superstore_demo"]
    assert table[0]["起步狀態"] == "可直接開始"

    with (out / "features.csv").open(encoding="utf-8-sig", newline="") as fh:
        features = {(r["類別"], r["項目"]): int(r["報表數"]) for r in csv.DictReader(fh)}
    assert features[("LOD", "FIXED")] >= 2
    assert features[("讀取失敗", "WorkbookFormatError")] == 1
    assert ("資料源", "嵌入式萃取") in features

    markdown = (out / "inventory.md").read_text(encoding="utf-8")
    assert "## B 級候選" in markdown
    assert "superstore_demo" in markdown
    assert "讀取或解析失敗" in markdown


def test_extracts_omitted(tmp_path: Path) -> None:
    path = demo_without_extract(tmp_path / "demo.twbx")
    strict, _, _ = inventory_workbook(path)
    assert strict.readiness == "not_eligible"
    assert "資料可以取得（萃取或 Live 連線）" in strict.failed_checks

    relaxed, report, unsupported = inventory_workbook(path, extracts_omitted=True)
    assert relaxed.readiness == "ready"
    assert relaxed.candidate == "B"
    assert unsupported == []
    assert report is not None
    check = next(c for c in report.checks if c.key == "data_access")
    assert "下載時未含萃取檔" in check.detail


def test_server_meta_joins_and_ranks(tmp_path: Path) -> None:
    downloads = tmp_path / "server"
    (downloads / "workbooks").mkdir(parents=True)
    demo_without_extract(downloads / "workbooks" / "a.twbx")
    demo_without_extract(downloads / "workbooks" / "b.twbx")
    meta = {
        "server": "https://tableau.example.com",
        "site": "sales",
        "extracts_omitted": True,
        "workbooks": [
            {
                "file": "workbooks/a.twbx",
                "id": "1",
                "name": "銷售總覽（舊）",
                "project": "Sales",
                "owner": "amy",
                "views": 12,
            },
            {
                "file": "workbooks/b.twbx",
                "id": "2",
                "name": "銷售總覽",
                "project": "Sales",
                "owner": "bob",
                "views": 340,
            },
            {
                "id": "3",
                "name": "財務月報",
                "project": "Finance",
                "views": 99,
                "download_error": "沒有下載權限（403019）",
            },
        ],
    }
    (downloads / "server.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "out"
    assert (
        main(["inventory", "--server-meta", str(downloads / "server.json"), "--out-dir", str(out)])
        == 0
    )
    result = run_inventory([downloads], out, server_meta=downloads / "server.json")
    rows = {r.name: r for r in result.rows}
    assert rows["銷售總覽"].project == "Sales"
    assert rows["銷售總覽"].views == 340
    assert rows["銷售總覽"].readiness == "ready", "server.json 標示下載時未含萃取"
    assert [r.name for r in result.candidates("B")] == ["銷售總覽", "銷售總覽（舊）"]
    missing = rows["財務月報"]
    assert missing.readiness == "error"
    assert missing.error == "沒有下載權限（403019）"
    markdown = (out / "inventory.md").read_text(encoding="utf-8")
    assert "https://tableau.example.com（site：sales）" in markdown
    assert markdown.index("| 銷售總覽 |") < markdown.index("| 銷售總覽（舊） |")


def test_server_connections_mark_viewer_credentials(tmp_path: Path) -> None:
    """伺服器上沒有嵌入密碼的 Live 連線：檢視者用自己的資料庫帳號，列為不符合起步條件。"""
    from tmig.census.collect import IDENTITY_TITLE

    from ..parser import builder as b
    from ..parser.test_connections import ORACLE, live_datasource, sheet

    downloads = tmp_path / "server"
    (downloads / "workbooks").mkdir(parents=True)
    table = "<relation connection='db.1' name='ORDERS' table='[SALES].[ORDERS]' type='table' />"
    xml = b.workbook(datasource=live_datasource(ORACLE, table), worksheets=sheet())
    workbooks = []
    for i, (name, embedded) in enumerate([("嵌入密碼", True), ("提示使用者", False)], start=1):
        (downloads / "workbooks" / f"{i}.twb").write_text(xml, encoding="utf-8")
        workbooks.append(
            {
                "file": f"workbooks/{i}.twb",
                "id": str(i),
                "name": name,
                "connections": ["oracle"],
                "connection_details": [
                    {"type": "oracle", "datasource": "Orders", "embed_password": embedded}
                ],
            }
        )
    meta = {"server": "https://tableau.example.com", "workbooks": workbooks}
    (downloads / "server.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "out"
    result = run_inventory([downloads], out, server_meta=downloads / "server.json")
    rows = {r.name: r for r in result.rows}
    assert rows["嵌入密碼"].readiness == "ready"
    prompted = rows["提示使用者"]
    assert prompted.readiness == "not_eligible"
    assert prompted.failed_checks == [IDENTITY_TITLE]
    assert prompted.detail is not None
    detail = (out / prompted.detail).read_text(encoding="utf-8")
    assert "「Orders」在 Tableau Server 上沒有嵌入密碼，檢視者用自己的資料庫帳號連線" in detail
    with (out / "features.csv").open(encoding="utf-8-sig", newline="") as fh:
        reasons = {r["項目"] for r in csv.DictReader(fh) if r["類別"] == "起步條件不符"}
    assert reasons == {IDENTITY_TITLE}


def test_query_time_unsupported_counts(tmp_path: Path) -> None:
    """解析時看不出、產生查詢時才知道的不支援（例如總計中的 Table Calculation）也列入盤點。"""
    from ..parser import builder as b
    from ..parser.builder import instance, ref, worksheet

    # 被參照的計算欄位中的 PREVIOUS_VALUE：產生查詢時才知道不支援
    calcs = b.calc("Calculation_2", "Carry", "PREVIOUS_VALUE(0) + SUM([Sales])") + b.calc(
        "Calculation_3", "Outer", "RUNNING_SUM([Calculation_2])"
    )
    sheet = worksheet(
        "Totals",
        [
            instance("[Region]", "None", "[none:Region:nk]"),
            instance("[Calculation_3]", "User", "[usr:Calculation_3:qk]", "quantitative"),
        ],
        rows=ref("none:Region:nk"),
        mark="Text",
        encodings=f"<text column='{ref('usr:Calculation_3:qk')}' />",
    )
    xml = b.workbook(b.extract_datasource(columns=calcs), worksheets=sheet)
    row, _, unsupported = inventory_workbook(b.write_twb(tmp_path, xml))
    reason = "PREVIOUS_VALUE 只支援在工作表上的 Table Calculation 計算欄位的公式中使用"
    assert [u.reason for u in unsupported] == [f"工作表「Totals」：{reason}"]
    assert row.readiness == "needs_features"
    assert row.unsupported_top == [f"工作表「…」：{reason}（1）"]

    # 巢狀 Table Calculation 已支援：不再列入
    calc = b.calc("Calculation_1", "Nested", "RUNNING_SUM(RANK(SUM([Sales])))")
    sheet = worksheet(
        "Nested",
        [
            instance("[Region]", "None", "[none:Region:nk]"),
            instance("[Calculation_1]", "User", "[usr:Calculation_1:qk]", "quantitative"),
        ],
        rows=ref("none:Region:nk"),
        cols=ref("usr:Calculation_1:qk"),
    )
    xml = b.workbook(b.extract_datasource(columns=calc), worksheets=sheet)
    row, _, unsupported = inventory_workbook(b.write_twb(tmp_path, xml))
    assert unsupported == [] and row.readiness == "ready"


def test_unsupported_kind_groups_similar_reasons() -> None:
    def entry(reason: str) -> Unsupported:
        return Unsupported(source_ref="/workbook", reason=reason)

    a = unsupported_kind(entry("欄位「Region」設有成員別名，會改變顯示值，v0 不支援"))
    b = unsupported_kind(entry("欄位「Segment」設有成員別名，會改變顯示值，v0 不支援"))
    assert a == b == "欄位「…」設有成員別名，會改變顯示值，v0 不支援"
    assert unsupported_kind(entry("dashboard「A」不是固定尺寸；暫以 1366×768 換算")) == (
        "dashboard「…」不是固定尺寸；暫以 N×N 換算"
    )


def test_cli_without_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inventory", "--out-dir", str(tmp_path)]) == 1
    assert "請給" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["inventory", str(empty), "--out-dir", str(tmp_path / "out")]) == 1
    assert "沒有找到" in capsys.readouterr().err
