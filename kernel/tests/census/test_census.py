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
    assert "正式盤點請用 .twbx" in detail(pilot, "embedded_extract")


def test_twbx_with_extract(tmp_path: Path) -> None:
    report = run_census(make_twbx(tmp_path, HYPER))
    assert report.packaged
    assert statuses(report)["embedded_extract"] == "pass"
    assert [f.name for f in report.files] == [HYPER]


def test_twbx_missing_extract_file(tmp_path: Path) -> None:
    report = run_census(make_twbx(tmp_path, None))
    assert statuses(report)["embedded_extract"] == "fail"
    assert "萃取檔不在 .twbx 內" in detail(report, "embedded_extract")


def test_twbx_extract_matched_by_file_name(tmp_path: Path) -> None:
    report = run_census(make_twbx(tmp_path, "Data/Extracts/FEDERATED_0abc123.hyper"))
    assert statuses(report)["embedded_extract"] == "pass"


# ---------------------------------------------------------------- 不符合條件的報表


def test_problems_fail_the_right_checks(problems: CensusReport) -> None:
    assert statuses(problems) == {
        "single_datasource": "fail",
        "embedded_extract": "fail",
        "single_table": "fail",
        "no_rls": "fail",
        "no_blending": "fail",
        "fixed_dashboards": "warn",
        "v0_functions": "warn",
        "v0_features": "warn",
    }
    assert problems.grade == "C"
    assert problems.grade_reasons[:4] == ["Data Blending", "自訂 SQL", "Story", "地圖"]


def test_problems_details(problems: CensusReport) -> None:
    assert "「Targets」是 Published Data Source" in detail(problems, "embedded_extract")
    assert "「Sales (Oracle)」是 Live 連線" in detail(problems, "embedded_extract")
    assert "Relationship 模型有 2 張邏輯資料表" in detail(problems, "single_table")
    assert (
        detail(problems, "no_rls") == "ISMEMBEROF（Sales (Oracle) 的資料源篩選）；USERNAME（Mine）"
    )
    assert "「Auto」為 automatic" in detail(problems, "fixed_dashboards")
    assert problems.functions_outside_v0 == ["USERNAME"]
    assert problems.lod == {"INCLUDE": 1, "TABLE": 1}


def test_problems_features(problems: CensusReport) -> None:
    features = detail(problems, "v0_features")
    for expected in [
        "相對日期篩選（Blended）",
        "條件篩選（Blended）",
        "參考線或趨勢線（Blended）",
        "地圖（Map）",
        "Story（Story 1）",
        "群組、集合或分組（categorical-bin 1、group 1）",
        "Dashboard action（tsc:url-action 1、url 1）",
    ]:
        assert expected in features


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


# ---------------------------------------------------------------- 報告


def test_markdown_report(pilot: CensusReport, problems: CensusReport) -> None:
    text = render_markdown(pilot)
    assert text.startswith("# 功能盤點：pilot.twb")
    assert "- 起步條件：全部符合" in text
    assert "| 函式 | 用到的計算欄位數 | v0 函式集 |" in text
    assert "| Overview | fixed 1200×800 |" in text
    bad = render_markdown(problems)
    assert "- 起步條件：不符合 5 項" in bad
    assert 'IF [REGION] IN ("North", "South") THEN ATTR([REGION]) ELSE "[Other]" END' in bad


def test_markdown_escapes_pipes_and_newlines(pilot: CensusReport) -> None:
    report = pilot.model_copy(deep=True)
    report.formulas[0].formula = "IF [a] THEN 'x|y'\nEND"
    assert "IF [a] THEN 'x\\|y' END" in render_markdown(report)


def test_json_roundtrip(pilot: CensusReport) -> None:
    again = CensusReport.model_validate_json(pilot.model_dump_json())
    assert again == pilot
