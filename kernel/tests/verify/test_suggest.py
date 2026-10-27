import shutil
from pathlib import Path

import pytest

from tmig.cli import main
from tmig.ir.models import DataField
from tmig.verify.cases import CasesError, CaseSpec, load_cases
from tmig.verify.suggest import append_cases, render_cases, suggest_cases, url_value

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "fixtures" / "superstore_demo"

STARTER = """# 起始檔
workbook: wb_superstore_demo
tableau_views_url: null
fetched_at: null
sheets:
  - worksheet: ws_sales_by_region
    view: SalesbyRegion
cases:
  - name: default
    url_params: {}
    note: 工作表的預設篩選
  # - name: common_value
"""


@pytest.fixture(scope="module")
def fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("suggest") / "superstore_demo"
    target.mkdir()
    for name in ("superstore_demo.twbx", "golden.ir.json"):
        shutil.copy(DEMO / name, target / name)
    (target / "cases.yaml").write_text(STARTER, encoding="utf-8")
    return target


def test_suggestions_follow_dashboard_controls(fixture: Path) -> None:
    cases, notes = suggest_cases(fixture)
    assert [(c.name, c.url_params) for c in cases] == [
        ("common_value", {"Category": "Office Supplies"}),
        ("date_boundary", {"year(Order Date)": "2024"}),
        ("parameter", {"Segment": "Consumer", "前 N 名": "1"}),
    ]
    assert cases[0].note is not None and "6026 筆" in cases[0].note
    assert any("匯入" in n for n in notes), "第一次會由 .twbx 匯入資料"


def test_append_keeps_comments_and_skips_existing(fixture: Path) -> None:
    path = fixture / "cases.yaml"
    cases, _ = suggest_cases(fixture)
    assert append_cases(path, cases) == ["common_value", "date_boundary", "parameter"]
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 起始檔")
    loaded = load_cases(path)
    assert [c.name for c in loaded.cases] == [
        "default",
        "common_value",
        "date_boundary",
        "parameter",
    ]
    assert loaded.case("parameter").url_params == {"Segment": "Consumer", "前 N 名": "1"}
    assert append_cases(path, cases) == []
    assert path.read_text(encoding="utf-8") == text


def test_append_requires_cases_last(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text("workbook: wb\nsheets: []\ncases: []\n", encoding="utf-8")
    case = CaseSpec(name="x", url_params={"Region": "East"})
    assert append_cases(path, [case]) == ["x"]
    assert [c.name for c in load_cases(path).cases] == ["x"]

    path.write_text("workbook: wb\ncases: []\nsheets: []\n", encoding="utf-8")
    with pytest.raises(CasesError, match="不是最後一個欄位"):
        append_cases(path, [case])


def test_render_and_url_values() -> None:
    import datetime as dt

    text = render_cases([CaseSpec(name="a", url_params={"Note": "a\\,b"}, note="說明")])
    assert text == '  - name: a\n    url_params:\n      "Note": "a\\\\,b"\n    note: "說明"\n'
    assert url_value("Furniture, Office") == "Furniture\\, Office"
    assert url_value(True) == "true"
    assert url_value(3.0) == "3"
    assert url_value(dt.date(2024, 1, 31)) == "2024-01-31"
    assert url_value(dt.datetime(2024, 1, 31, 8, 5)) == "2024-01-31 08:05:00"


def test_cli(fixture: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cases", "suggest", str(fixture)]) == 0
    captured = capsys.readouterr()
    assert '"Category": "Office Supplies"' in captured.out
    assert "--write" in captured.err


def test_param_name_for_date_part() -> None:
    from tmig.ir.models import DateDerivation, FieldUse
    from tmig.verify.suggest import Candidate, param_name

    field = DataField(
        id="f_order_date",
        name="[Order Date]",
        caption="Order Date",
        role="dimension",
        datatype="date",
        column="Order Date",
        source_ref="/x",
    )
    use = FieldUse(field="f_order_date", date=DateDerivation(kind="part", unit="year"))
    assert param_name(Candidate(use=use, field=field, datasource="ds")) == "year(Order Date)"
    assert param_name(
        Candidate(use=FieldUse(field="f_order_date"), field=field, datasource="ds")
    ) == ("Order Date")
