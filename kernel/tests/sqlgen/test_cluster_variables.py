"""叢集：取消彙總量值時每一列資料分叢集，以及以維度（類別變數）分群。

Tableau 2021.2 說明文件「Find Clusters in Data」
（https://help.tableau.com/v2021.2/pro/desktop/en-us/clustering.htm ，2026-11-23 查閱）：檢視中的每個標記分到
一個叢集；維度以 ATTR 彙總，超過 25 個值的類別變數不用，ATTR 是 * 的標記不分叢集（Not Clustered）。類別
怎麼換成距離沒有寫；Tableau 的產品經理在部落格說明以多重對應分析（MCA）換成數值的距離
（https://boraberan.wordpress.com/2016/07/19/understanding-clustering-in-tableau-10/ ，同日查閱），細節（保留
幾個維度、要不要正規化）是推定的（見 tmig.planner.cluster）。

測試資料見 test_query：訂單 O1 150、O2 200（East）、O3 300、O4 500（West）、O5 80、O6 20（South）、O7 60
（North）；East、West、South 各有兩個類別，North 只有 Office。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tmig.data.executor import execute_query
from tmig.ir import IR
from tmig.parser.workbook import parse_workbook
from tmig.planner.analytics import Analytics, ClusterPlan, apply
from tmig.planner.cluster import cluster
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import worksheet_sql

from ..parser import builder as b
from ..parser.builder import instance, ref, worksheet
from .test_query import CATEGORY, REGION, ROWS, SALES, db

__all__ = ["db"]  # pytest fixture

CLUSTER = instance("[AdhocCluster:1]", "None", "[none:AdhocCluster:1:ok]", "ordinal")
CLUSTER_COLUMN = (
    "<column caption='Clusters' datatype='integer' hidden='true' is-adhoc-cluster='true' "
    "name='[AdhocCluster:1]' parent-model='[AnalyticModel:{test}]' role='dimension' type='ordinal'>"
    "<aliases><alias key='0' value='Not Clustered' /><alias key='1' value='Cluster 1' />"
    "<alias key='2' value='Cluster 2' /></aliases></column>"
)
RAW_SALES = instance("[Sales]", "None", "[none:Sales:qk]", "quantitative")
RAW_PROFIT = instance("[Profit]", "None", "[none:Profit:qk]", "quantitative")
ORDER = instance("[Order ID]", "None", "[none:Order ID:nk]")
ATTR_CATEGORY = instance("[Category]", "Attribute", "[attr:Category:nk]")


def model(fields: list[str], count: int, disaggregate: bool = False) -> str:
    analysis = "".join(f"<field name='[{b.DS}].[{f}]' />" for f in fields)
    flag = "true" if disaggregate else "false"
    return f"""
  <analytic-model>
    <name><field name='[AnalyticModel:{{test}}]' /></name>
    <analytic-model-specifications class='cluster-specifications'>
      <analysis-fields>{analysis}</analysis-fields>
      <filter-fields /><lod-fields />
      <number-of-desired-clusters>{count}</number-of-desired-clusters>
      <is-dissagregate>{flag}</is-dissagregate>
    </analytic-model-specifications>
    <result-fields />
  </analytic-model>"""


def parse(tmp_path: Path, sheet: str, fields: list[str], count: int, disaggregate: bool) -> IR:
    extra = model(fields, count, disaggregate)
    xml = b.workbook(b.extract_datasource(extra=extra), worksheets=sheet)
    return parse_workbook(b.write_twb(tmp_path, xml))


def clusters_of(ir: IR, db: Path, key: str) -> dict[object, int]:
    ws = ir.worksheets[0]
    result = execute_query(db, worksheet_sql(ir, ws.id))
    return {r[key]: r["f_clusters"] for r in result.records()}


# ---------------------------------------------------------------- 取消彙總量值


def scatter(aggregated: bool = False) -> str:
    sheet = worksheet(
        "Scatter",
        [REGION, RAW_SALES, RAW_PROFIT, CLUSTER],
        rows=ref("none:Profit:qk"),
        cols=ref("none:Sales:qk"),
        mark="Circle",
        encodings=f"<color column='{ref('none:AdhocCluster:1:ok')}' />",
        deps_extra=CLUSTER_COLUMN,
    )
    if aggregated:
        return sheet
    return sheet.replace("<aggregation value='true' />", "<aggregation value='false' />")


def test_disaggregated_clusters_each_row(tmp_path: Path, db: Path) -> None:
    """取消彙總量值：每一列資料是一個標記，以每一列的 Sales、Profit 分兩群（300、400、200 的三列與其餘六列；
    O1 的兩列 100／10 與 50／-5 是兩個標記）。"""
    ir = parse(tmp_path, scatter(), ["none:Sales:qk", "none:Profit:qk"], 2, disaggregate=True)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    assert ws.disaggregated and ws.cluster is not None
    assert [m.field for m in ws.cluster.measures] == ["f_sales", "f_profit"]
    query = worksheet_sql(ir, ws.id)
    result = execute_query(db, query)
    records = result.records()
    assert len(records) == len(ROWS)
    big = {(r["f_sales"], r["f_profit"]) for r in records if r["f_clusters"] == 2}
    assert big == {(200.0, 40.0), (300.0, 30.0), (400.0, -20.0)}
    assert all(r["f_clusters"] == 1 for r in records if (r["f_sales"], r["f_profit"]) not in big)
    # 與直接以每一列的值分群相同
    expected = cluster([[r[4], r[5]] for r in ROWS], 2)
    by_value = {(r[4], r[5]): label for r, label in zip(ROWS, expected, strict=True)}
    assert all(by_value[(r["f_sales"], r["f_profit"])] == r["f_clusters"] for r in records)
    rendered = build_spec(ir, ws.id, query.columns, result)
    assert rendered.spec is not None


def test_disaggregate_flag_must_match_the_view(tmp_path: Path) -> None:
    """叢集建立時是取消彙總、檢視目前是彙總（或相反）：Tableau 怎麼重新分群沒有文件，列為不支援並說明。"""
    ir = parse(tmp_path, scatter(aggregated=True), ["none:Sales:qk"], 2, disaggregate=True)
    assert any("「彙總量值」設定與檢視目前的（彙總）不同" in u.reason for u in ir.unsupported), [
        u.reason for u in ir.unsupported
    ]


# ---------------------------------------------------------------- 類別變數


def region_sheet(rows: str, instances: list[str]) -> str:
    return worksheet(
        "Regions",
        [REGION, CATEGORY, ORDER, SALES, ATTR_CATEGORY, CLUSTER, *instances],
        rows=rows,
        cols=ref("sum:Sales:qk"),
        encodings=f"<color column='{ref('none:AdhocCluster:1:ok')}' />",
        deps_extra=CLUSTER_COLUMN,
    )


@pytest.mark.parametrize("field", ["none:Category:nk", "attr:Category:nk"])
def test_attr_star_is_not_clustered(tmp_path: Path, db: Path, field: str) -> None:
    """類別變數 Category 不在檢視中（以 ATTR 在各區彙總）：East、West、South 各有兩個類別（ATTR 是 *），與
    Tableau 相同不分叢集（0）；只有 North（Office）分到叢集。"""
    ir = parse(tmp_path, region_sheet(ref("none:Region:nk"), []), ["sum:Sales:qk", field], 2, False)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    assert ws.cluster is not None
    assert [c.field for c in ws.cluster.categories] == ["f_category"]
    assert clusters_of(ir, db, "f_region") == {"East": 0, "West": 0, "South": 0, "North": 1}
    # 另外輸出的 ATTR 不畫出（role 為 sort、名稱以 __ 開頭）
    query = worksheet_sql(ir, ws.id)
    hidden = [c for c in query.columns if c.key.startswith("__cluster:")]
    assert [(c.key, c.role) for c in hidden] == [("__cluster:attr(f_category)", "sort")]
    result = execute_query(db, query)
    assert result.records()[0]["__cluster:attr(f_category)"] is None
    rendered = build_spec(ir, ws.id, query.columns, result)
    assert rendered.spec is not None
    assert "__cluster" not in str(rendered.spec)
    assert query.analytics is not None
    assert query.analytics.details["cluster"]["unclustered"] == 3
    assert query.analytics.details["cluster"]["categories"] == ["ATTR(Category)"]


def test_categorical_variable_separates_regions(tmp_path: Path, db: Path) -> None:
    """各訂單的銷售加上類別變數 Region（不在檢視中；每張訂單只有一區）分四群：類別之間的距離使同一區的訂單
    在同一群（只看銷售時 O4 500 自成一群）。"""
    rows = ref("none:Order ID:nk")
    ir = parse(tmp_path, region_sheet(rows, []), ["sum:Sales:qk", "none:Region:nk"], 4, False)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    got = clusters_of(ir, db, "f_order_id")
    groups = {frozenset(o for o, c in got.items() if c == label) for label in set(got.values())}
    assert groups == {
        frozenset({"O1", "O2"}),
        frozenset({"O3", "O4"}),
        frozenset({"O5", "O6"}),
        frozenset({"O7"}),
    }
    # 與直接計算相同（銷售與各訂單的區）
    sales = {"O1": 150, "O2": 200, "O3": 300, "O4": 500, "O5": 80, "O6": 20, "O7": 60}
    region = {"O1": "East", "O2": "East", "O3": "West", "O4": "West", "O5": "South"}
    region |= {"O6": "South", "O7": "North"}
    orders = sorted(sales)
    expected = cluster([[sales[o]] for o in orders], 4, [[region[o]] for o in orders])
    assert [got[o] for o in orders] == expected
    numeric_only = cluster([[sales[o]] for o in orders], 4)
    assert numeric_only != expected


def test_categorical_variable_in_the_view(tmp_path: Path, db: Path) -> None:
    """類別變數也在檢視中（Region / Category 的列）：直接用維度的欄，不另外輸出。"""
    rows = f"{ref('none:Region:nk')} / {ref('none:Category:nk')}"
    ir = parse(tmp_path, region_sheet(rows, []), ["sum:Sales:qk", "none:Category:nk"], 2, False)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    assert not [c for c in query.columns if c.key.startswith("__cluster:")]
    records = execute_query(db, query).records()
    marks = [(r["f_region"], r["f_category"], r["sum(f_sales)"]) for r in records]
    expected = cluster([[s] for _, _, s in marks], 2, [[c] for _, c, _ in marks])
    assert [r["f_clusters"] for r in records] == expected


def test_only_categorical_variables_are_flagged(tmp_path: Path) -> None:
    ir = parse(tmp_path, region_sheet(ref("none:Region:nk"), []), ["none:Category:nk"], 2, False)
    assert any("只有維度（類別變數）" in u.reason for u in ir.unsupported)


# ---------------------------------------------------------------- 演算法


def test_mca_distances_are_chi_square_distances() -> None:
    """MCA 的主座標之間的距離即指標矩陣的卡方距離：d² = (n / Q)·Σ_j (z_ij − z_i'j)² / n_j。"""
    from tmig.planner.cluster import mca

    rows = [("a", "x"), ("a", "y"), ("b", "x"), ("c", "y"), ("a", "x"), ("b", "y")]
    coordinates = mca(rows)
    n, q = len(rows), 2
    counts: dict[tuple[int, str], int] = {}
    for row in rows:
        for v, value in enumerate(row):
            counts[(v, value)] = counts.get((v, value), 0) + 1
    for i in range(n):
        for j in range(n):
            expected = 0.0
            for v in range(q):
                if rows[i][v] != rows[j][v]:
                    expected += n / q * (1 / counts[(v, rows[i][v])] + 1 / counts[(v, rows[j][v])])
            got = float(((coordinates[i] - coordinates[j]) ** 2).sum())
            assert got == pytest.approx(expected)
    # 非零的維度數最多是各變數的值數合計減去變數數（3 + 2 − 2）
    assert coordinates.shape == (n, 3)


def test_categorical_variable_with_more_than_25_values_is_ignored() -> None:
    """超過 25 個值的類別變數不用（與只用量值分群相同），描述叢集中列出它。"""
    values = [float(v) for v in [1, 2, 3, 10, 11, 12, 30, 31, 32] * 3 + [50]]
    rows = [(f"m{i}", v, 0) for i, v in enumerate(values)]
    plan = Analytics(
        cluster=ClusterPlan("cluster", ["value"], 3, categories=["name"], captions={"name": "Name"})
    )
    _, out = apply(plan, ["name", "value", "cluster"], rows)
    assert [r[2] for r in out] == cluster([[v] for v in values], 3)
    assert plan.details["cluster"]["ignored"] == ["Name"]
    assert plan.details["cluster"]["categories"] == []
    # 25 個值時照常使用
    few = [(f"m{i % 25}", v, 0) for i, v in enumerate(values)]
    plan = Analytics(cluster=ClusterPlan("cluster", ["value"], 3, categories=["name"]))
    _, out = apply(plan, ["name", "value", "cluster"], few)
    assert plan.details["cluster"]["ignored"] == []
    groups = [[r[0]] for r in few]
    assert [r[2] for r in out] == cluster([[v] for v in values], 3, groups)


def test_no_valid_marks() -> None:
    """所有標記都不分叢集（量值都是 Null）：不出錯，全部是 0。"""
    plan = Analytics(cluster=ClusterPlan("cluster", ["value"], 2))
    _, out = apply(plan, ["value", "cluster"], [(None, 0), (None, 0)])
    assert [r[1] for r in out] == [0, 0]
    from tmig.planner.cluster import mca

    assert np.asarray(mca([])).shape == (0, 0)
