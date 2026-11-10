import zipfile
from pathlib import Path

import pytest

from tmig.census import CensusReport, render_markdown, run_census

DATA = Path(__file__).parent / "data"
PILOT = DATA / "pilot.twb"
PROBLEMS = DATA / "problems.twb"
HYPER = "Data/Extracts/federated_0abc123.hyper"


def statuses(report: CensusReport) -> dict[str, str]:
    return {check.key: check.status for check in report.checks}


def detail(report: CensusReport, key: str) -> str:
    return next(check.detail for check in report.checks if check.key == key)


@pytest.fixture(scope="module")
def pilot() -> CensusReport:
    return run_census(PILOT)


@pytest.fixture(scope="module")
def problems() -> CensusReport:
    return run_census(PROBLEMS)


def make_twbx(tmp_path: Path, extract: str | None) -> Path:
    path = tmp_path / "pilot.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pilot.twb", PILOT.read_bytes())
        if extract:
            archive.writestr(extract, b"\0" * 4096)
    return path


# ---------------------------------------------------------------- 符合條件的報表


def test_pilot_passes_every_check(pilot: CensusReport) -> None:
    assert set(statuses(pilot).values()) == {"pass"}
    assert detail(pilot, "viewer_identity") == "沒有 Live 連線（萃取的資料不依賴檢視者的帳號）"
    assert pilot.grade == "B"
    assert pilot.grade_reasons == [
        "LOD：FIXED 1",
        "Table Calculation：工作表上 1 處、計算欄位 2 個",
    ]


def test_pilot_datasource(pilot: CensusReport) -> None:
    (ds,) = pilot.datasources
    assert ds.caption == "Orders"
    assert ds.connection_classes == ["excel-direct"]
    assert ds.data_in_hyper
    assert ds.extract_files == [HYPER]
    assert ds.extract_tables == ["[Extract].[Extract]"]
    assert ds.source_relation_types == ["join", "table"]
    assert (ds.field_count, ds.calc_count) == (9, 4)
    assert "來源為 join，萃取存成單表" in detail(pilot, "single_table")


def test_pilot_formulas(pilot: CensusReport) -> None:
    by_field = {f.field: f for f in pilot.formulas}
    assert set(by_field) == {"Profit Ratio", "Region Sales", "Running Sales", "Top Region?"}
    assert by_field["Region Sales"].lod == ["FIXED"]
    assert by_field["Top Region?"].table_calc_functions == ["RANK"]
    assert pilot.functions == {"SUM": 4, "IIF": 1, "RANK": 1, "RUNNING_SUM": 1}
    assert pilot.functions_outside_v0 == []


def test_pilot_worksheets(pilot: CensusReport) -> None:
    sales, monthly = pilot.worksheets
    assert sales.mark_types == ["Bar"]
    assert sales.encodings == ["color", "text", "tooltip"]
    assert [(f.kind, f.detail) for f in sales.filters] == [
        ("categorical", "2 個值，inclusive"),
        ("top_n", "top 10"),
    ]
    assert [(c.type, c.ordering_type) for c in sales.table_calcs] == [("CumTotal", "Rows")]
    assert sales.sorts == ["computed"]
    assert [f.kind for f in monthly.filters] == ["range", "measure_range"]
    assert not monthly.blending


def test_pilot_dashboard_and_parameters(pilot: CensusReport) -> None:
    (board,) = pilot.dashboards
    assert board.sizing == "fixed"
    assert board.worksheets == ["Sales by Region", "Monthly Profit"]
    assert board.zone_types == {
        "color": 1,
        "filter": 1,
        "layout-basic": 1,
        "paramctrl": 1,
        "title": 1,
        "viz": 2,
    }
    (param,) = pilot.parameters
    assert (param.caption, param.datatype, param.domain, param.value) == (
        "Top N",
        "integer",
        "range",
        "10",
    )


def test_twb_input_is_marked_unpackaged(pilot: CensusReport) -> None:
    assert not pilot.packaged
    assert "正式盤點請用 .twbx" in detail(pilot, "data_access")


def test_twbx_with_extract(tmp_path: Path) -> None:
    report = run_census(make_twbx(tmp_path, HYPER))
    assert report.packaged
    assert statuses(report)["data_access"] == "pass"
    assert [f.name for f in report.files] == [HYPER]


def test_twbx_missing_extract_file(tmp_path: Path) -> None:
    report = run_census(make_twbx(tmp_path, None))
    assert statuses(report)["data_access"] == "fail"
    assert "萃取檔不在 .twbx 內" in detail(report, "data_access")


def test_twbx_extract_matched_by_file_name(tmp_path: Path) -> None:
    report = run_census(make_twbx(tmp_path, "Data/Extracts/FEDERATED_0abc123.hyper"))
    assert statuses(report)["data_access"] == "pass"


# ---------------------------------------------------------------- 不符合條件的報表


def test_problems_fail_the_right_checks(problems: CensusReport) -> None:
    # 多個資料源已支援（各工作表查詢自己的資料源），不再是起步條件
    assert statuses(problems) == {
        "data_access": "fail",
        "single_table": "pass",
        "viewer_identity": "pass",  # Oracle 以帳號密碼連線，沒有 Initial SQL
        "no_rls": "warn",  # 列層級權限已支援（P1），提醒驗證時設定 user
        "no_blending": "fail",
        "fixed_dashboards": "warn",
        "v0_functions": "pass",
        "v0_features": "warn",
    }
    assert problems.grade == "C"
    # Story 已支援，不再是 C 級的原因
    assert problems.grade_reasons[:3] == ["Data Blending", "自訂 SQL", "地圖"]


def test_problems_details(problems: CensusReport) -> None:
    assert "「Targets」是 Published Data Source" in detail(problems, "data_access")
    # 連到資料庫的 Live 連線可以下推查詢（第三階段），不再列為問題
    assert "Sales (Oracle)" not in detail(problems, "data_access")
    # Relationship 模型（N6）不再列為問題；Published Data Source 的結構待下載後才知道
    assert (
        detail(problems, "single_table")
        == "「Sales (Oracle)」：Relationship 模型（2 張邏輯資料表，Live）"
    )
    assert detail(problems, "no_rls").split("；") == [
        "ISMEMBEROF（Sales (Oracle) 的資料源篩選、Visible）",
        "USERNAME（Mine）",
        "使用者篩選「User Filter 1」",
        "「Visible」用到參數 [Parameter 2]：使用者改參數就能改變可見範圍（與 Tableau 相同），"
        "請確認是否刻意如此",
        "驗證時在 cases.yaml 設定 user（抓取預期結果的帳號）",
    ]
    assert "「Auto」為 automatic" in detail(problems, "fixed_dashboards")
    assert problems.functions_outside_v0 == []  # 使用者函式已支援（P1）
    assert problems.datasources[0].user_filters == ["User Filter 1"]
    assert problems.lod == {"INCLUDE": 1, "TABLE": 1}


def test_problems_features(problems: CensusReport) -> None:
    features = detail(problems, "v0_features")
    assert "相對日期篩選" not in features and "條件篩選" not in features  # K4 已支援
    for expected in [
        "趨勢線（Blended）",  # 參考線（J4）已支援
        "地圖（Map）",
        "Dashboard action（tsc:url-action 1）",  # 網址動作（url）已支援
    ]:
        assert expected in features
    assert "Story" not in features  # 已支援
    assert "特殊欄位" not in features  # 集合（Top Customers）已支援


def test_actions(problems: CensusReport) -> None:
    assert [(a.caption, a.kind, a.source) for a in problems.actions] == [
        ("Filter 1", "filter", "Auto / Blended"),
        ("Highlight 1", "highlight", "Auto"),
        ("Open link", "tsc:url-action", "Auto"),
        ("Go to View", "url", "federated.oracle1"),
    ]


def test_device_layout_zones_are_not_counted(problems: CensusReport) -> None:
    auto = next(d for d in problems.dashboards if d.name == "Auto")
    assert auto.zone_types == {"layout-basic": 1, "viz": 1}
    assert auto.device_layouts


def test_problems_worksheet_shapes(problems: CensusReport) -> None:
    blended, map_sheet = problems.worksheets
    assert blended.datasources == ["federated.oracle1", "sqlproxy.42"]
    assert blended.pane_count == 2
    assert blended.mark_types == ["Bar", "Line"]
    assert blended.shelf_operators == ["(", ")", "+"]
    assert [f.kind for f in blended.filters] == ["relative_date", "condition", "categorical"]
    assert (blended.reference_lines, blended.trend_lines) == (1, 1)
    assert map_sheet.maps


def test_cross_datasource_filters_are_not_blending(tmp_path: Path) -> None:
    """工作表列出兩個資料源，但只以跨資料源篩選（對應到主要資料源的欄位）用到另一個：不是 Data Blending。"""
    from ..parser import builder as b
    from ..sqlgen.test_blend import (
        blended,
        cross_filter,
        orders_sheet,
        target_ref,
        targets_datasource,
    )

    sheets = orders_sheet("Cross", cross_filter(["East"], group="2"))
    xml = b.workbook(b.extract_datasource() + targets_datasource(), worksheets=sheets)
    report = run_census(b.write_twb(tmp_path, xml))
    (sheet,) = report.worksheets
    assert len(sheet.datasources) == 2
    assert not sheet.blending
    assert statuses(report)["no_blending"] == "pass"

    sheets = blended(rows=b.ref("none:Region:nk"), cols=target_ref("sum:Target:qk"))
    xml = b.workbook(b.extract_datasource() + targets_datasource(), worksheets=sheets)
    (sheet,) = run_census(b.write_twb(tmp_path, xml, "blended.twb")).worksheets
    assert sheet.blending


# ---------------------------------------------------------------- 報告


def test_markdown_report(pilot: CensusReport, problems: CensusReport) -> None:
    text = render_markdown(pilot)
    assert text.startswith("# 功能盤點：pilot.twb")
    assert "- 起步條件：全部符合" in text
    assert "| 函式 | 用到的計算欄位數 | v0 函式集 |" in text
    assert "| Overview | fixed 1200×800 |" in text
    bad = render_markdown(problems)
    assert "- 起步條件：不符合 2 項" in bad  # 列層級權限（P1）、多個資料源已支援
    assert 'IF [REGION] IN ("North", "South") THEN ATTR([REGION]) ELSE "[Other]" END' in bad


def test_markdown_escapes_pipes_and_newlines(pilot: CensusReport) -> None:
    report = pilot.model_copy(deep=True)
    report.formulas[0].formula = "IF [a] THEN 'x|y'\nEND"
    assert "IF [a] THEN 'x\\|y' END" in render_markdown(report)


def test_json_roundtrip(pilot: CensusReport) -> None:
    again = CensusReport.model_validate_json(pilot.model_dump_json())
    assert again == pilot
