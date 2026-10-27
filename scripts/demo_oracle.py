"""示範報表的預期結果：以純 Python 獨立計算。

    uv run python scripts/demo_oracle.py

示範報表沒有 Tableau Server 可以抓預期結果，所以這支程式只用標準函式庫，
逐張工作表照 Tableau 的運算順序手寫計算，不呼叫 tmig 的規劃器、SQL 或 DuckDB，
寫出 fixtures/superstore_demo/expected/<工作表 id>/<篩選組合>.csv。

它與 tmig 依據同一份對 Tableau 語意的理解，所以只能抓出實作錯誤，抓不出語意誤解；
真實報表的預期結果一律由 fetch-expected 從 Tableau Server 抓取。

CSV 的欄名仿照 Tableau 匯出檢視資料的寫法（Year of Order Date、Running Sum of Sales），
數值以完整精度輸出，日期為 ISO 格式。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "superstore_demo"
SOURCE = FIXTURE / "source" / "orders.csv.gz"
EXPECTED = FIXTURE / "expected"

DEFAULT_TOP_N = 3  # 參數「前 N 名」的目前值


def load_rows() -> list[dict[str, Any]]:
    rows = []
    with gzip.open(SOURCE, "rt", encoding="utf-8", newline="") as f:
        for raw in csv.DictReader(f):
            rows.append(
                {
                    "Region": raw["Region"],
                    "Category": raw["Category"],
                    "Sub-Category": raw["Sub-Category"],
                    "Segment": raw["Segment"],
                    "Country/Region": raw["Country/Region"],
                    "Order ID": raw["Order ID"],
                    "Order Date": dt.date.fromisoformat(raw["Order Date"]),
                    "Sales": float(raw["Sales"]),
                    "Profit": float(raw["Profit"]),
                }
            )
    return rows


class Case:
    """cases.yaml 的一組網址參數，解讀成這份報表用到的篩選。"""

    def __init__(self, name: str, params: dict[str, Any]) -> None:
        self.name = name
        self.category = _values(params.get("Category"))
        self.segment = _values(params.get("Segment"))
        year = params.get("year(Order Date)")
        self.year = int(year) if year is not None else None
        top = params.get("前 N 名")
        self.top_n = int(top) if top is not None else DEFAULT_TOP_N
        unknown = set(params) - {"Category", "Segment", "year(Order Date)", "前 N 名"}
        if unknown:
            raise SystemExit(f"示範報表的 oracle 不認得網址參數：{unknown}")

    def keep(
        self,
        row: dict[str, Any],
        category: list[str] | None = None,
        segment: list[str] | None = None,
    ) -> bool:
        """網址參數的維度篩選；同欄位的工作表篩選會被網址參數取代。"""
        categories = self.category if self.category is not None else category
        segments = self.segment if self.segment is not None else segment
        if categories is not None and row["Category"] not in categories:
            return False
        if segments is not None and row["Segment"] not in segments:
            return False
        return not (self.year is not None and row["Order Date"].year != self.year)


def _values(value: Any) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in str(value).split(",")]


def sales_by_region(rows: list[dict[str, Any]], case: Case) -> tuple[list[str], list[list[Any]]]:
    # Top N 在維度篩選之前，以資料源篩選後的全部資料排名
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[row["Region"]] += row["Sales"]
    top = sorted(totals, key=lambda r: (-totals[r], r))[: case.top_n]
    kept = [
        r for r in rows if r["Region"] in top and case.keep(r, category=["Furniture", "Technology"])
    ]
    cells: dict[tuple[str, str], float] = defaultdict(float)
    region_sales: dict[str, float] = defaultdict(float)
    for row in kept:
        cells[(row["Region"], row["Category"])] += row["Sales"]
        region_sales[row["Region"]] += row["Sales"]
    order = sorted(region_sales, key=lambda r: (-region_sales[r], r))
    out = []
    for category in sorted({c for _, c in cells}):
        running = 0.0
        for region in order:
            if (region, category) not in cells:
                continue
            running += cells[(region, category)]
            out.append([region, category, cells[(region, category)], running])
    return ["Region", "Category", "Sales", "Running Sum of Sales"], out


def monthly_profit(rows: list[dict[str, Any]], case: Case) -> tuple[list[str], list[list[Any]]]:
    start, end = dt.date(2023, 1, 1), dt.date(2024, 12, 31)
    profit: dict[dt.date, float] = defaultdict(float)
    sales: dict[dt.date, float] = defaultdict(float)
    for row in rows:
        if not (start <= row["Order Date"] <= end) or not case.keep(row):
            continue
        month = row["Order Date"].replace(day=1)
        profit[month] += row["Profit"]
        sales[month] += row["Sales"]
    out = []
    for month in sorted(profit):
        if sales[month] < 20000:  # 量值篩選 SUM(Sales) ≥ 20000
            continue
        out.append([month.isoformat(), profit[month], profit[month] / sales[month]])
    return ["Month of Order Date", "Profit", "利潤率"], out


def region_share(rows: list[dict[str, Any]], case: Case) -> tuple[list[str], list[list[Any]]]:
    # FIXED [Region]：不受維度篩選影響
    fixed: dict[str, float] = defaultdict(float)
    for row in rows:
        fixed[row["Region"]] += row["Sales"]
    sales: dict[tuple[str, int], float] = defaultdict(float)
    for row in rows:
        if case.keep(row, segment=["Consumer", "Corporate"]):
            sales[(row["Region"], row["Order Date"].year)] += row["Sales"]
    out = [
        [region, year, value / fixed[region], value]
        for (region, year), value in sorted(sales.items())
    ]
    return ["Region", "Year of Order Date", "區域占比", "Sales"], out


def sub_category_profit(
    rows: list[dict[str, Any]], case: Case
) -> tuple[list[str], list[list[Any]]]:
    kept = [r for r in rows if case.keep(r)]
    profit: dict[str, float] = defaultdict(float)
    orders: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in kept:
        profit[row["Sub-Category"]] += row["Profit"]
        orders[row["Sub-Category"]][row["Order ID"]] += row["Profit"]
    out = []
    for name, value in profit.items():
        rank = 1 + sum(1 for other in profit.values() if other > value)
        if rank > 10:  # Table Calculation 篩選：利潤排名 ≤ 10
            continue
        per_order = list(orders[name].values())
        out.append([name, value, rank, sum(per_order) / len(per_order)])
    out.sort(key=lambda r: r[2])
    return ["Sub-Category", "Profit", "利潤排名", "平均訂單利潤"], out


SHEETS = {
    "ws_sales_by_region": sales_by_region,
    "ws_monthly_profit": monthly_profit,
    "ws_region_share_by_year": region_share,
    "ws_sub_category_profit": sub_category_profit,
}


def text(value: Any) -> str:
    if isinstance(value, float):
        return repr(value)
    return str(value)


def main() -> None:
    cases = yaml.safe_load((FIXTURE / "cases.yaml").read_text(encoding="utf-8"))
    # 資料源篩選：只保留美國
    rows = [r for r in load_rows() if r["Country/Region"] == "United States"]
    count = 0
    for sheet in cases["sheets"]:
        compute = SHEETS[sheet["worksheet"]]
        for spec in cases["cases"]:
            headers, body = compute(rows, Case(spec["name"], spec.get("url_params") or {}))
            path = EXPECTED / sheet["worksheet"] / f"{spec['name']}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, lineterminator="\n")
                writer.writerow(headers)
                writer.writerows([[text(v) for v in row] for row in body])
            count += 1
    print(f"已寫入 {count} 份預期結果 → {EXPECTED.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
