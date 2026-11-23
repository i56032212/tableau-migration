"""預測模型函式 MODEL_QUANTILE、MODEL_PERCENTILE：以檢視的標記建立線性迴歸，與 numpy 手算的結果比較。

Tableau 2021.2 說明文件（2026-11-23 查閱）：預設的模型是線性迴歸（OLS，predictions_choosing_model.htm）；
每個標記一列、權重相同，維度以 ATTR 當作預測變數，「計算依據」只用來分割，每個分割至少 3 個標記
（predictions_overview.htm、predictions_compute_using.htm、predictions_resolve_errors.htm）；MODEL_QUANTILE 是
後驗預測分配的分位數、MODEL_PERCENTILE 是觀察值的累積機率（functions_functions_predictivemodeling.htm）。
後驗預測分配以無資訊的先驗計算（自由度 n − p 的 Student t，中心是 OLS 的預測值、尺度 s·√(1 + h)）是推定的
（見 tmig.planner.predictive）；分位數 0.5 一律是 OLS 的預測值。

測試資料見 test_query：各訂單（Order ID）的 Sales／Profit 合計為 O1 150／5、O2 200／40、O3 300／30、
O4 500／-10、O5 80／8、O6 20／2、O7 60／6；地區 O1、O2 East，O3、O4 West，O5、O6 South，O7 North；
類別 O1、O3、O5 Furniture，O2、O4 Technology，O6、O7 Office。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tmig.data.executor import execute_query
from tmig.parser.workbook import parse_workbook
from tmig.planner.predictive import ModelPlan, ModelPredictor, predict, t_cdf, t_ppf
from tmig.sqlgen.query import QueryError, worksheet_sql

from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet
from .test_query import db

__all__ = ["db"]  # pytest fixture

ORDERS = ["O1", "O2", "O3", "O4", "O5", "O6", "O7"]
SALES = [150.0, 200.0, 300.0, 500.0, 80.0, 20.0, 60.0]
PROFIT = [5.0, 40.0, 30.0, -10.0, 8.0, 2.0, 6.0]
REGION = ["East", "East", "West", "West", "South", "South", "North"]
CATEGORY = ["Furniture", "Technology", "Furniture", "Technology", "Furniture", "Office", "Office"]

ORDER_INST = instance("[Order ID]", "None", "[none:Order ID:nk]")
CATEGORY_INST = instance("[Category]", "None", "[none:Category:nk]")
SALES_INST = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")


def model_instance(n: int, table_calc: str = "<table-calc ordering-type='Columns' />") -> str:
    return instance(
        f"[Calculation_{n}]", "User", f"[usr:Calculation_{n}:qk]", "quantitative", table_calc
    )


def reference(
    y: list[float], columns: list[list[float]], q: float | None = None
) -> tuple[list[float], list[float], int]:
    """numpy 手算：OLS 的預測值、預測的尺度 s·√(1 + h) 與自由度。"""
    x = np.column_stack([np.ones(len(y)), *columns])
    beta, *_ = np.linalg.lstsq(x, np.array(y), rcond=None)
    fitted = x @ beta
    dof = len(y) - x.shape[1]
    s2 = float(((np.array(y) - fitted) ** 2).sum()) / dof
    inverse = np.linalg.inv(x.T @ x)
    scale = [math.sqrt(s2 * (1 + float(row @ inverse @ row))) for row in x]
    return [float(v) for v in fitted], scale, dof


def run(tmp_path: Path, db: Path, sheet: str, calcs: str) -> list[dict[str, Any]]:
    xml = b.workbook(b.extract_datasource(columns=calcs), worksheets=sheet)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    query = worksheet_sql(ir, ir.worksheets[0].id)
    result = execute_query(db, query)
    # 計算欄位的鍵（f_median#tc_formula_median）只留欄位的 id
    return [
        {k.split("#")[0]: v for k, v in r.items() if not k.startswith("__")}
        for r in result.records()
    ]


def order_sheet(
    *numbers: int,
    table_calc: str = "<table-calc ordering-type='Columns' />",
    rows: str | None = None,
) -> str:
    """每個訂單一個標記（Order ID 在列架上）；計算欄位 Calculation_<n> 放在文字上。"""
    return worksheet(
        "S",
        [ORDER_INST, CATEGORY_INST, SALES_INST, *(model_instance(n, table_calc) for n in numbers)],
        rows=rows or ref("none:Order ID:nk"),
        cols=ref("sum:Sales:qk"),
        mark="Circle",
        encodings="".join(f"<text column='{ref(f'usr:Calculation_{n}:qk')}' />" for n in numbers),
    )


# ---------------------------------------------------------------- 分配


def test_student_t_matches_closed_forms() -> None:
    """Student t 的累積機率與分位數：自由度 1（Cauchy）、2、3、4 有封閉的公式。"""
    for t in (-3.0, -0.5, 0.0, 0.7, 2.5, 12.0):
        assert t_cdf(t, 1) == pytest.approx(0.5 + math.atan(t) / math.pi, abs=1e-13)
        assert t_cdf(t, 2) == pytest.approx(0.5 + t / (2 * math.sqrt(2 + t * t)), abs=1e-13)
        u = t / math.sqrt(3)
        three = 0.5 + (u / (1 + u * u) + math.atan(u)) / math.pi
        assert t_cdf(t, 3) == pytest.approx(three, abs=1e-13)
    for q in (0.025, 0.1, 0.5, 0.8, 0.95, 0.999):
        assert t_ppf(q, 1) == pytest.approx(math.tan(math.pi * (q - 0.5)), rel=1e-11, abs=1e-12)
        two = (2 * q - 1) / math.sqrt(2 * q * (1 - q))
        assert t_ppf(q, 2) == pytest.approx(two, rel=1e-11, abs=1e-12)
        a = 4 * q * (1 - q)
        four = math.copysign(
            2 * math.sqrt(math.cos(math.acos(math.sqrt(a)) / 3) / math.sqrt(a) - 1), q - 0.5
        )
        assert t_ppf(q, 4) == pytest.approx(four, rel=1e-10, abs=1e-12)
    # 自由度大時接近常態分配：t(0.975, 10⁶) ≈ 1.959966
    assert t_ppf(0.975, 1e6) == pytest.approx(1.959966, abs=1e-5)


# ---------------------------------------------------------------- 計算


def test_linear_regression_against_numpy() -> None:
    """一個分割：OLS 的預測值（分位數 0.5）、其他分位數與觀察值的累積機率都與 numpy 手算相同。"""
    columns = ["y", "x"]
    rows = list(zip(SALES, PROFIT, strict=True))
    fitted, scale, dof = reference(SALES, [PROFIT])
    assert dof == 5
    median = ModelPlan("m", "MODEL_QUANTILE", "y", [ModelPredictor("x", "measure")], quantile=0.5)
    assert predict(median, columns, rows) == pytest.approx(fitted)
    upper = ModelPlan("m", "MODEL_QUANTILE", "y", [ModelPredictor("x", "measure")], quantile=0.9)
    expected = [f + t_ppf(0.9, dof) * s for f, s in zip(fitted, scale, strict=True)]
    assert predict(upper, columns, rows) == pytest.approx(expected)
    percentile = ModelPlan("m", "MODEL_PERCENTILE", "y", [ModelPredictor("x", "measure")])
    expected = [t_cdf((y - f) / s, dof) for y, f, s in zip(SALES, fitted, scale, strict=True)]
    assert predict(percentile, columns, rows) == pytest.approx(expected)


def test_categories_are_one_hot_and_nulls() -> None:
    """維度的預測變數換成各類別的指標（基準是第一個類別）；目標是 Null 的標記不進模型、照樣預測；量值的
    預測變數是 Null、或類別只出現在沒有目標的標記上時沒有結果（推定）。"""
    columns = ["y", "x", "c"]
    rows: list[tuple[Any, ...]] = [
        (150.0, 5.0, "A"),
        (200.0, 40.0, "B"),
        (300.0, 30.0, "A"),
        (500.0, -10.0, "B"),
        (80.0, 8.0, "A"),
        (20.0, 2.0, None),
        (60.0, 6.0, None),
        (None, 10.0, "B"),
        (None, None, "A"),
        (None, 3.0, "Z"),
    ]
    plan = ModelPlan(
        "m",
        "MODEL_QUANTILE",
        "y",
        [ModelPredictor("x", "measure"), ModelPredictor("c", "category")],
        quantile=0.5,
    )
    found = predict(plan, columns, rows)
    train = rows[:7]
    x = [r[1] for r in train]
    is_b = [1.0 if r[2] == "B" else 0.0 for r in train]
    is_null = [1.0 if r[2] is None else 0.0 for r in train]
    design = np.column_stack([np.ones(7), x, is_b, is_null])
    beta, *_ = np.linalg.lstsq(design, np.array([r[0] for r in train]), rcond=None)
    expected = [float(v) for v in design @ beta]
    expected.append(float(np.array([1.0, 10.0, 1.0, 0.0]) @ beta))
    assert found[:8] == pytest.approx(expected)
    assert found[8:] == [None, None]


def test_too_few_marks_and_redundant_predictors() -> None:
    """少於 3 個標記：沒有結果；重複的預測變數、預測變數與標記一對一：Tableau 會顯示錯誤。"""
    from tmig.planner.predictive import ModelError

    plan = ModelPlan("m", "MODEL_QUANTILE", "y", [ModelPredictor("x", "measure")], quantile=0.5)
    assert predict(plan, ["y", "x"], [(1.0, 2.0), (3.0, 4.0), (None, 5.0)]) == [None] * 3
    twice = ModelPlan(
        "m",
        "MODEL_PERCENTILE",
        "y",
        [ModelPredictor("x", "measure"), ModelPredictor("z", "measure")],
    )
    rows = [(1.0, 2.0, 4.0), (3.0, 1.0, 2.0), (2.0, 5.0, 10.0), (6.0, 3.0, 6.0)]
    with pytest.raises(ModelError, match="重複"):
        predict(twice, ["y", "x", "z"], rows)
    one_to_one = ModelPlan(
        "m", "MODEL_QUANTILE", "y", [ModelPredictor("c", "category")], quantile=0.5, caption="P"
    )
    with pytest.raises(ModelError, match="一對一"):
        predict(one_to_one, ["y", "c"], [(1.0, "a"), (2.0, "b"), (4.0, "c")])


def test_augmentation_adds_square_term() -> None:
    """augmentation=on 的量值加上 2 次項（與多項式的尺度無關）。"""
    columns = ["y", "x"]
    rows = list(zip(SALES, PROFIT, strict=True))
    plan = ModelPlan(
        "m", "MODEL_QUANTILE", "y", [ModelPredictor("x", "measure", square=True)], quantile=0.5
    )
    fitted, _, dof = reference(SALES, [PROFIT, [p * p for p in PROFIT]])
    assert dof == 4
    assert predict(plan, columns, rows) == pytest.approx(fitted)


# ---------------------------------------------------------------- 查詢


CALCS = "".join(
    [
        calc("Calculation_90", "Median", "MODEL_QUANTILE(0.5, SUM([Sales]), SUM([Profit]))"),
        calc(
            "Calculation_91",
            "Upper",
            'MODEL_QUANTILE("model=linear", 0.9, SUM([Sales]), SUM([Profit]))',
        ),
        calc("Calculation_92", "Pct", "MODEL_PERCENTILE(SUM([Sales]), SUM([Profit]))"),
        calc(
            "Calculation_93",
            "By Region",
            "MODEL_QUANTILE(0.5, SUM([Sales]), SUM([Profit]), ATTR([Region]))",
        ),
    ]
)


def test_view_marks_build_the_model(tmp_path: Path, db: Path) -> None:
    """每個訂單是一個標記：以 SUM(Profit)（與 ATTR(Region)）預測 SUM(Sales)，與 numpy 手算相同。"""
    sheet = order_sheet(90, 91, 92, 93)
    rows = run(tmp_path, db, sheet, CALCS)
    by_order = {r["f_order_id"]: r for r in rows}
    assert sorted(by_order) == ORDERS
    fitted, scale, dof = reference(SALES, [PROFIT])
    for i, order in enumerate(ORDERS):
        record = by_order[order]
        assert record["f_median"] == pytest.approx(fitted[i])
        assert record["f_upper"] == pytest.approx(fitted[i] + t_ppf(0.9, dof) * scale[i])
        assert record["f_pct"] == pytest.approx(t_cdf((SALES[i] - fitted[i]) / scale[i], dof))
    regions = sorted(set(REGION))
    dummies = [[1.0 if r == name else 0.0 for r in REGION] for name in regions[1:]]
    by_region, _, dof = reference(SALES, [PROFIT, *dummies])
    assert dof == 2
    assert [by_order[o]["f_by_region"] for o in ORDERS] == pytest.approx(by_region)


def test_compute_using_partitions(tmp_path: Path, db: Path) -> None:
    """計算依據是 Order ID：Category 分割，每個類別各自建立模型；少於 3 個標記的類別（Technology、Office）
    沒有結果（Null）。Furniture 的 3 個標記、1 個預測變數：自由度 1。"""
    order = f"[{b.DS}].[Order ID]"
    tc = f"<table-calc ordering-field='{order}' ordering-type='Field' />"
    sheet = order_sheet(
        90, 91, table_calc=tc, rows=f"({ref('none:Category:nk')} / {ref('none:Order ID:nk')})"
    )
    rows = run(tmp_path, db, sheet, CALCS)
    furniture = [i for i, c in enumerate(CATEGORY) if c == "Furniture"]
    fitted, scale, dof = reference([SALES[i] for i in furniture], [[PROFIT[i] for i in furniture]])
    assert dof == 1
    found = {r["f_order_id"]: (r["f_median"], r["f_upper"]) for r in rows}
    for j, i in enumerate(furniture):
        median, upper = found[ORDERS[i]]
        assert median == pytest.approx(fitted[j])
        assert upper == pytest.approx(fitted[j] + math.tan(math.pi * 0.4) * scale[j])
    assert all(found[o] == (None, None) for o in ("O2", "O4", "O6", "O7"))
    # 儲存格（每個標記自己一個分割）：沒有結果
    cell = order_sheet(90, table_calc="<table-calc ordering-type='CellInPane' />")
    assert all(r["f_median"] is None for r in run(tmp_path, db, cell, CALCS))


@pytest.mark.parametrize(
    ("formula", "reason"),
    [
        ('MODEL_QUANTILE("model=gp", 0.5, SUM([Sales]), ATTR([Order Date]))', "高斯過程迴歸"),
        (
            'MODEL_QUANTILE("model=rl, lambda=0.05", 0.5, SUM([Sales]), SUM([Profit]))',
            "正規化線性迴歸",
        ),
        (
            "MODEL_PERCENTILE(SUM([Sales]), ATTR(DATETRUNC('month', [Order Date])))",
            "日期的預測變數",
        ),
        ('MODEL_PERCENTILE(SUM([Sales]), "ordered", ATTR([Region]))', "ordered"),
        ("MODEL_QUANTILE(1, SUM([Sales]), SUM([Profit]))", "0 與 1 之間"),
        ("MODEL_QUANTILE(0.5, SUM([Sales]), INDEX())", "Table Calculation"),
    ],
)
def test_undocumented_models_are_unsupported(tmp_path: Path, formula: str, reason: str) -> None:
    """Tableau 沒有公開計算方式的模型與寫法：計算欄位列為不支援，說明原因。"""
    sheet = order_sheet(90)
    xml = b.workbook(
        b.extract_datasource(columns=calc("Calculation_90", "Model", formula)), worksheets=sheet
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert any(reason in u.reason for u in ir.unsupported), [u.reason for u in ir.unsupported]


def test_combined_or_numeric_dimension_is_unsupported(tmp_path: Path) -> None:
    """MODEL_* 與其他運算組合、數字的維度（ATTR(Quantity)）當作預測變數：查詢時列為不支援。"""
    for formula, reason in (
        ("MODEL_QUANTILE(0.5, SUM([Sales]), SUM([Profit])) - SUM([Sales])", "單獨作為計算欄位"),
        ("MODEL_QUANTILE(0.5, SUM([Sales]), ATTR([Quantity]))", "數字的維度"),
    ):
        sheet = order_sheet(90)
        xml = b.workbook(
            b.extract_datasource(columns=calc("Calculation_90", "Model", formula)),
            worksheets=sheet,
        )
        ir = parse_workbook(b.write_twb(tmp_path, xml))
        assert ir.unsupported == [], [u.reason for u in ir.unsupported]
        with pytest.raises(QueryError, match=reason) as caught:
            worksheet_sql(ir, ir.worksheets[0].id)
        assert caught.value.unsupported
