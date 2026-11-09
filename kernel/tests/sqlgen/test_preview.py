"""資料源分頁：資料預覽（原始列與列層級的計算欄位，套用資料源篩選與列層級權限）與資料源的摘要。

以 test_query 的 9 列資料（East 350、West 800、South 100、North 60）驗證。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmig.data.executor import execute
from tmig.ir import IR, load_ir
from tmig.render.datasource import datasource_summary, readable_formula
from tmig.sqlgen.query import QueryError, preview_sql

from ..parser import builder as b
from ..parser.builder import calc
from .test_query import db
from .test_relationships import model_ir
from .test_rls import CAROL, MINE, OWNER, build, ds_filter

__all__ = ["db", "model_ir"]  # pytest fixtures

ROOT = Path(__file__).resolve().parents[3]
CALCS = (
    calc("Margin", "Margin", "[Profit] / [Sales]", "real", "measure")
    + calc("Total Sales", "Total Sales", "SUM([Sales])", "real", "measure")
    + calc("Region Sales", "Region Sales", "{FIXED [Region] : SUM([Sales])}", "real", "measure")
    + calc("Big", "Big", "[Sales] > [Parameters].[Parameter 1] * 20", "boolean", "dimension")
)


def workbook(tmp_path: Path, calcs: str = CALCS) -> IR:
    from tmig.parser.workbook import parse_workbook

    xml = b.workbook(b.extract_datasource(columns=calcs))
    return parse_workbook(b.write_twb(tmp_path, xml))


def test_preview_rows_and_row_level_calcs(tmp_path: Path, db: Path) -> None:
    """原始欄位與列層級的計算欄位；彙總與 LOD 的計算欄位不列出；最多 limit 列。"""
    ir = workbook(tmp_path)
    preview = preview_sql(ir, ir.datasources[0].id, limit=4)
    captions = [c.caption for c in preview.columns]
    assert captions[:7] == [
        "Region",
        "Category",
        "Order ID",
        "Order Date",
        "Sales",
        "Profit",
        "Quantity",
    ]
    assert captions[7:] == ["Margin", "Big"]
    assert [c.calculated for c in preview.columns][-2:] == [True, True]
    result = execute(db, preview.sql)
    assert len(result.rows) == 4
    # 列層級的計算欄位逐列計算：Profit／Sales、Sales > 參數 × 20
    first = dict(zip(captions, result.rows[0], strict=True))
    assert first["Margin"] == pytest.approx(first["Profit"] / first["Sales"])
    assert first["Big"] == (first["Sales"] > 5 * 20)
    assert execute(db, preview.count_sql).rows == [(9,)]


def test_preview_applies_row_level_security(tmp_path: Path, db: Path) -> None:
    """與工作表相同套用資料源篩選與列層級權限：carol 只看得到 North、South；沒有身分時失敗。"""
    ir = build(tmp_path, OWNER + MINE, ds_filter("Mine"))
    ds = ir.datasources[0].id
    preview = preview_sql(ir, ds, user=CAROL)
    region = next(i for i, c in enumerate(preview.columns) if c.caption == "Region")
    rows = execute(db, preview.sql).rows
    assert sorted({r[region] for r in rows}) == ["North", "South"]
    assert execute(db, preview.count_sql).rows == [(3,)]
    with pytest.raises(QueryError, match="需要登入者的身分"):
        preview_sql(ir, ds)


def test_preview_one_logical_table_at_a_time(model_ir: IR) -> None:
    """Relationship 模型：一次預覽一張邏輯資料表，其他資料表的欄位不列出。"""
    ds = model_ir.datasources[0]
    first = preview_sql(model_ir, ds.id)
    assert first.table == ds.connection.relation.tables[0].id  # type: ignore[union-attr]
    for table in ds.connection.relation.tables:  # type: ignore[union-attr]
        preview = preview_sql(model_ir, ds.id, table.id)
        assert preview.table == table.id
        owners = {f.table for f in ds.fields if f.id in {c.field for c in preview.columns}}
        assert owners <= {table.id, None}, table.id
    with pytest.raises(QueryError, match="沒有資料表"):
        preview_sql(model_ir, ds.id, "nope")


def test_datasource_summary_of_an_extract() -> None:
    ir = load_ir(ROOT / "fixtures" / "superstore_features" / "golden.ir.json")
    summary = datasource_summary(ir.datasources[0], ir.parameters)
    assert summary["connection"]["kind"] == "extract"
    assert summary["connection"]["type"] == "Tableau 萃取（Hyper）"
    assert summary["model"] is False
    assert [t["physical"][0]["name"] for t in summary["tables"]] == ["Extract.Extract"]
    assert summary["filters"] == [
        {
            "id": "flt_country_region",
            "stage": "datasource",
            "text": "Country/Region：只保留 United States",
        }
    ]
    share = next(f for f in summary["fields"] if f["caption"] == "區域占比")
    # 公式中的內部名稱換成顯示名稱（與 Tableau 的公式編輯器相同）
    assert share["kind"] == "calc"
    assert "SUM([區域總銷售])" in share["formula"]
    assert "Calculation_" not in share["formula"]


def test_datasource_summary_of_a_relationship_model(model_ir: IR) -> None:
    summary = datasource_summary(model_ir.datasources[0])
    assert summary["model"] is True
    captions = [t["caption"] for t in summary["tables"]]
    assert len(captions) == len(model_ir.datasources[0].connection.relation.tables)  # type: ignore[union-attr]
    assert summary["relationships"]
    for rel in summary["relationships"]:
        assert rel["left"] in captions and rel["right"] in captions
        assert all(" = " in on for on in rel["on"])
    # 欄位所在的邏輯資料表以顯示名稱表示
    assert {f["table"] for f in summary["fields"] if f["kind"] == "column"} <= set(captions)


def test_readable_formula() -> None:
    names = {"[Calculation_1]": "利潤率", "[Odd]]Name]": "Odd]"}
    params = {"[Parameter 1]": "Top N"}
    formula = 'IF [Calculation_1] > [Parameters].[Parameter 1] THEN "[Calculation_1]" END'
    assert (
        readable_formula(formula, names, params)
        == 'IF [利潤率] > [Top N] THEN "[Calculation_1]" END'
    )
    assert readable_formula("[Odd]]Name] + [Unknown]", names, params) == "[Odd]]] + [Unknown]"
