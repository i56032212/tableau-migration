"""功能報表的預期結果：以純 Python 獨立計算（不呼叫 tmig 的規劃器、SQL 或 DuckDB）。

    uv run python scripts/features_oracle.py

寫出 fixtures/superstore_features/expected/<工作表 id>/<篩選組合>.csv。與 scripts/demo_oracle.py 相同，
它和 tmig 依據同一份對 Tableau 語意的理解，只能抓出實作錯誤，抓不出語意誤解。

篩選的語意：
- 資料源篩選：Country/Region 只保留 United States
- 全域篩選（<shared-views>）：Region 排除 Central，套用到篩選架上有 Region 的工作表
- 篩選群組：Segment 只保留 Consumer、Corporate，套用到 Sales by Segment 與 Profit by Ship Mode
- 網址參數取代同欄位的既有篩選（含全域篩選）；工作表沒有該欄位的篩選時新增一個
- Measure Names／Values 的工作表匯出為長表（Measure Names、Measure Values 兩欄），與 Tableau 相同
- 排除選取的標記（Region Category Matrix）：排除 (West, Furniture)、(East, Technology) 兩個值組合
- 篩選動作的目標是另一個篩選，不會被網址參數取代：沒有選取時，Category Detail 顯示全部，
  Segment Detail 不顯示任何值（動作設定為清除時排除所有值）；選取地區時兩者都只保留那些地區。
  篩選組合不含選取，選取的情況由 selection() 計算，供檢視器的端到端測試比對
- 群組：Region (group) 把 East、West 合為 Coasts，其餘維持原值；Ship Speed 把 First Class、Same Day
  合為 Fast，其餘歸入 Other。數值分組：Quantity (bin) 為 Quantity 所在區間（每 3 一格）的下限
- 相對日期：以 cases.yaml 的 today 為基準日，最近 3 個月為基準日所在月份與前 2 個月
- 彙總條件與 Top N 以其他維度篩選（含網址參數）之前的資料計算：總銷售額大於 5000 的客戶、
  排除 Phones 後銷售額前 3 名的子類別，都先以全部資料決定，再套用網址參數
- 差異與差異百分比：與前一年比較，差異百分比為差異除以前一年的絕對值；第一年沒有值（匯出為空白）
- Selected Region Sales：參數 Selected Region（預設 East）所選地區的銷售額，其他地區為 0
- 移動計算：依月份排序，移動平均為前兩個月與當月（有幾個月就平均幾個月）；移動總和為前後各一個月，
  前後不足一個月時為空值（只算有資料的月份）
- 各標記卡的詳細程度不同（甜甜圈、依客群分色的折線）時，匯出的資料以工作表上所有維度的組合計算：
  Category Donut 為各類別（MIN(0) 為 0）、Region Sales and Segment Profit 為各地區 × 客群
- 參考線只影響圖表：Sub-Category Sales、Region Sales Bands 的預期結果就是各標記的銷售額
- 集合：值為 In／Out。Top N 與條件集合以內容篩選後、維度篩選前的資料決定成員（這份報表沒有內容篩選，
  所以是全部資料；全域篩選與網址參數都是維度篩選，不影響成員）：Top Customers 為總銷售額前 10 名的客戶
  （同額時名稱較小者優先）、Profitable Sub-Categories 為總利潤大於 0 的子類別；Key Sub-Categories 為
  Chairs、Phones、Storage。集合放在篩選架上＝只保留 In；Key Sales 為重點子類別的銷售額
- 別名：匯出的資料中維度成員與 Measure Names 以別名顯示（Ship Speed 的 Other 為 Standard、
  Segment KPIs 的 CNTD(Order ID) 為「訂單數」）；參數的別名只影響控制項的顯示
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "superstore_features"
SOURCE = ROOT / "fixtures" / "superstore_demo" / "source" / "orders.csv.gz"
EXPECTED = FIXTURE / "expected"

Row = dict[str, Any]
Keep = Callable[[Row], bool]


def load_rows() -> list[Row]:
    rows = []
    with gzip.open(SOURCE, "rt", encoding="utf-8", newline="") as f:
        for raw in csv.DictReader(f):
            if raw["Country/Region"] != "United States":
                continue  # 資料源篩選
            rows.append(
                {
                    "Region": raw["Region"],
                    "Segment": raw["Segment"],
                    "Ship Mode": raw["Ship Mode"],
                    "Order ID": raw["Order ID"],
                    "Order Date": dt.date.fromisoformat(raw["Order Date"]),
                    "Category": raw["Category"],
                    "Sub-Category": raw["Sub-Category"],
                    "Customer Name": raw["Customer Name"],
                    "Quantity": int(raw["Quantity"]),
                    "Sales": float(raw["Sales"]),
                    "Profit": float(raw["Profit"]),
                }
            )
    return rows


class Case:
    def __init__(self, name: str, params: dict[str, Any]) -> None:
        self.name = name
        self.params = {k: [v.strip() for v in str(value).split(",")] for k, value in params.items()}
        unknown = set(self.params) - {"Region", "Segment", "Ship Mode"}
        if unknown:
            raise SystemExit(f"功能報表的 oracle 不認得網址參數：{unknown}")

    def keep(self, sheet_filters: dict[str, Keep]) -> Keep:
        """工作表的篩選（欄位 → 條件），網址參數取代同欄位的篩選或另外新增。"""
        filters = dict(sheet_filters)
        for field, values in self.params.items():
            filters[field] = lambda row, field=field, values=values: row[field] in values

        def keep(row: Row) -> bool:
            return all(check(row) for check in filters.values())

        return keep


def not_central(row: Row) -> bool:
    return row["Region"] != "Central"


def consumer_or_corporate(row: Row) -> bool:
    return row["Segment"] in ("Consumer", "Corporate")


def grouped(
    rows: list[Row], keep: Keep, dim: str, value: Callable[[list[Row]], Any]
) -> list[list[Any]]:
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if keep(row):
            groups[row[dim]].append(row)
    return [[key, value(items)] for key, items in sorted(groups.items())]


def sales_by_segment(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({"Segment": consumer_or_corporate, "Region": not_central})
    body = grouped(rows, keep, "Segment", lambda items: sum(r["Sales"] for r in items))
    return ["Segment", "Sales"], body


def profit_by_ship_mode(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({"Segment": consumer_or_corporate, "Region": not_central})
    body = grouped(rows, keep, "Ship Mode", lambda items: sum(r["Profit"] for r in items))
    return ["Ship Mode", "Profit"], body


def orders_by_region(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({"Region": not_central})
    body = grouped(rows, keep, "Region", lambda items: len({r["Order ID"] for r in items}))
    return ["Region", "Distinct count of Order ID"], body


def segment_kpis(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    """Measure Names／Values 文字表：匯出為長表（Measure Names、Measure Values）。"""
    keep = case.keep({})
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if keep(row):
            groups[row["Segment"]].append(row)
    body: list[list[Any]] = []
    for segment, items in sorted(groups.items()):
        sales = sum(r["Sales"] for r in items)
        profit = sum(r["Profit"] for r in items)
        orders = len({r["Order ID"] for r in items})
        for name, value in (
            ("Sales", sales),
            ("Profit", profit),
            ("訂單數", orders),
            ("利潤率", profit / sales),
        ):
            body.append([segment, name, value])
    return ["Segment", "Measure Names", "Measure Values"], body


def monthly_sales_vs_profit(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[dt.date, float] = defaultdict(float)
    profit: dict[dt.date, float] = defaultdict(float)
    for row in rows:
        if row["Order Date"].year != 2024 or not keep(row):
            continue
        month = row["Order Date"].replace(day=1)
        sales[month] += row["Sales"]
        profit[month] += row["Profit"]
    body: list[list[Any]] = []
    for month in sorted(sales):
        body.append([month.isoformat(), "Sales", sales[month]])
        body.append([month.isoformat(), "Profit", profit[month]])
    return ["Month of Order Date", "Measure Names", "Measure Values"], body


def sales_and_profit_by_year(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[int, float] = defaultdict(float)
    profit: dict[int, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            sales[row["Order Date"].year] += row["Sales"]
            profit[row["Order Date"].year] += row["Profit"]
    body = [[year, sales[year], profit[year]] for year in sorted(sales)]
    return ["Year of Order Date", "Sales", "Profit"], body


def category_mix(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[str, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            sales[row["Category"]] += row["Sales"]
    total = sum(sales.values())
    body = [[name, value, value / total] for name, value in sorted(sales.items())]
    return ["Category", "Sales", "% of Total Sales"], body


def region_picker(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    body = grouped(rows, case.keep({}), "Region", lambda items: sum(r["Sales"] for r in items))
    return ["Region", "Sales"], body


def action_keep(regions: list[str] | None) -> dict[str, Keep]:
    """篩選動作的篩選（鍵不是欄位名稱，所以網址參數不會取代它）。"""
    if regions is None:
        return {}
    return {"action": lambda row: row["Region"] in regions}


def category_detail(
    rows: list[Row], case: Case, regions: list[str] | None = None
) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep(action_keep(regions))
    body = grouped(rows, keep, "Category", lambda items: sum(r["Sales"] for r in items))
    return ["Category", "Sales"], body


def segment_detail(
    rows: list[Row], case: Case, regions: list[str] | None = None
) -> tuple[list[str], list[list[Any]]]:
    if not regions:
        return ["Segment", "Sales"], []  # 清除時排除所有值
    keep = case.keep(action_keep(regions))
    body = grouped(rows, keep, "Segment", lambda items: sum(r["Sales"] for r in items))
    return ["Segment", "Sales"], body


EXCLUDED = {("West", "Furniture"), ("East", "Technology")}


def region_category_matrix(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({"exclusions": lambda row: (row["Region"], row["Category"]) not in EXCLUDED})
    sales: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        if keep(row):
            sales[(row["Region"], row["Category"])] += row["Sales"]
    body = [[region, category, value] for (region, category), value in sorted(sales.items())]
    return ["Region", "Category", "Sales"], body


def selection(worksheet: str, regions: list[str] | None) -> tuple[list[str], list[list[Any]]]:
    """在 Region Picker 選取地區（None＝沒有選取）後，篩選動作目標工作表的預期結果（預設篩選組合）。"""
    compute = {"ws_category_detail": category_detail, "ws_segment_detail": segment_detail}[
        worksheet
    ]
    return compute(load_rows(), Case("selection", {}), regions)


def sales_by_region_group(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[str, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            group = "Coasts" if row["Region"] in ("East", "West") else row["Region"]
            sales[group] += row["Sales"]
    return ["Region (group)", "Sales"], [[k, v] for k, v in sorted(sales.items())]


def sales_by_ship_speed(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[str, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            # Other 的別名是 Standard（Tableau 匯出的資料以別名顯示）
            speed = "Fast" if row["Ship Mode"] in ("First Class", "Same Day") else "Standard"
            sales[speed] += row["Sales"]
    return ["Ship Speed", "Sales"], [[k, v] for k, v in sorted(sales.items())]


def sales_by_quantity_bin(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[int, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            sales[row["Quantity"] // 3 * 3] += row["Sales"]
    return ["Quantity (bin)", "Sales"], [[k, v] for k, v in sorted(sales.items())]


TODAY = dt.date(2024, 12, 31)


def recent_sales(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    """最近 3 個月：基準日所在月份的第一天往前 2 個月起，到該月底。"""
    start = dt.date(TODAY.year, TODAY.month, 1)
    for _ in range(2):
        start = (start - dt.timedelta(days=1)).replace(day=1)
    end = (dt.date(TODAY.year, TODAY.month, 28) + dt.timedelta(days=4)).replace(day=1)
    keep = case.keep({"Order Date": lambda row: start <= row["Order Date"] < end})
    body = grouped(rows, keep, "Category", lambda items: sum(r["Sales"] for r in items))
    return ["Category", "Sales"], body


def big_customers(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[row["Customer Name"]] += row["Sales"]  # 以網址參數之前的資料計算
    big = {name for name, total in totals.items() if total > 5000}
    keep = case.keep({"Customer Name": lambda row: row["Customer Name"] in big})
    body = grouped(rows, keep, "Segment", lambda items: len({r["Customer Name"] for r in items}))
    return ["Segment", "Distinct count of Customer Name"], body


def son_customers(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({"Customer Name": lambda row: "son" in row["Customer Name"].lower()})
    body = grouped(rows, keep, "Region", lambda items: sum(r["Sales"] for r in items))
    return ["Region", "Sales"], body


def top_sub_categories(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        if row["Sub-Category"] != "Phones":
            totals[row["Sub-Category"]] += row["Sales"]
    top = {name for name, _ in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:3]}
    keep = case.keep({"Sub-Category": lambda row: row["Sub-Category"] in top})
    body = grouped(rows, keep, "Sub-Category", lambda items: sum(r["Sales"] for r in items))
    return ["Sub-Category", "Sales"], body


def sales_growth(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[int, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            sales[row["Order Date"].year] += row["Sales"]
    body: list[list[Any]] = []
    previous: float | None = None
    for year in sorted(sales):
        diff = None if previous is None else sales[year] - previous
        pct = None if diff is None or not previous else diff / abs(previous)
        body.append([year, pct, diff, sales[year]])
        previous = sales[year]
    headers = ["Year of Order Date", "% Difference in Sales", "Difference in Sales", "Sales"]
    return headers, body


def selected_region_sales(
    rows: list[Row], case: Case, region: str = "East"
) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    body = grouped(
        rows,
        keep,
        "Category",
        lambda items: sum(r["Sales"] if r["Region"] == region else 0.0 for r in items),
    )
    return ["Category", "Selected Region Sales"], body


def parameter_action(region: str) -> tuple[list[str], list[list[Any]]]:
    """參數動作把 Selected Region 設成 region 後的預期結果（預設篩選組合）。"""
    return selected_region_sales(load_rows(), Case("parameter", {}), region)


def sub_category_sales(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        if keep(row):
            sales[(row["Category"], row["Sub-Category"])] += row["Sales"]
    return ["Category", "Sub-Category", "Sales"], [[*k, v] for k, v in sorted(sales.items())]


def region_sales_bands(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    body = grouped(rows, keep, "Region", lambda items: sum(r["Sales"] for r in items))
    return ["Region", "Sales"], body


def monthly_moving_average(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    sales: dict[dt.date, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            sales[row["Order Date"].replace(day=1)] += row["Sales"]
    months = sorted(sales)
    values = [sales[m] for m in months]
    body = []
    for i, month in enumerate(months):
        window = values[max(i - 2, 0) : i + 1]
        average = sum(window) / len(window)
        total = sum(values[i - 1 : i + 2]) if 0 < i < len(values) - 1 else None
        body.append([month.isoformat(), average, total, values[i]])
    return ["Month of Order Date", "Moving Average of Sales", "Moving Sum of Sales", "Sales"], body


def category_donut(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    body = grouped(rows, keep, "Category", lambda items: sum(r["Sales"] for r in items))
    return ["Category", "Zero", "Sales"], [[category, 0, sales] for category, sales in body]


def region_sales_segment_profit(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})
    totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        if keep(row):
            item = totals[(row["Region"], row["Segment"])]
            item[0] += row["Sales"]
            item[1] += row["Profit"]
    body = [[*key, sales, profit] for key, (sales, profit) in sorted(totals.items())]
    return ["Region", "Segment", "Sales", "Profit"], body


KEY_SUB_CATEGORIES = {"Chairs", "Phones", "Storage"}


def in_out(member: bool) -> str:
    return "In" if member else "Out"


def top_customers(rows: list[Row]) -> set[str]:
    """總銷售額前 10 名的客戶（全部資料；同額時名稱較小者優先）。"""
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[row["Customer Name"]] += row["Sales"]
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return {name for name, _ in ranked[:10]}


def top_customers_vs_others(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    top = top_customers(rows)
    keep = case.keep({"Region": not_central})
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if keep(row):
            groups[in_out(row["Customer Name"] in top)].append(row)
    body = [
        [key, sum(r["Sales"] for r in items), len({r["Customer Name"] for r in items})]
        for key, items in sorted(groups.items())
    ]
    return ["In/Out of Top Customers", "Sales", "Distinct count of Customer Name"], body


def profitable_sub_categories(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    profit: dict[str, float] = defaultdict(float)
    for row in rows:
        profit[row["Sub-Category"]] += row["Profit"]
    keep = case.keep({})
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        if keep(row):
            totals[row["Sub-Category"]] += row["Profit"]
    # IN/OUT 的別名：獲利（In）、虧損（Out）
    labels = {True: "獲利", False: "虧損"}
    body = [[name, labels[profit[name] > 0], value] for name, value in sorted(totals.items())]
    return ["Sub-Category", "In/Out of Profitable Sub-Categories", "Profit"], body


def key_sub_categories(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({"Key Sub-Categories": lambda row: row["Sub-Category"] in KEY_SUB_CATEGORIES})
    body = grouped(rows, keep, "Category", lambda items: sum(r["Sales"] for r in items))
    return ["Category", "Sales"], body


def key_sales_by_region(rows: list[Row], case: Case) -> tuple[list[str], list[list[Any]]]:
    keep = case.keep({})

    def key_sales(items: list[Row]) -> float | None:
        values = [r["Sales"] for r in items if r["Sub-Category"] in KEY_SUB_CATEGORIES]
        return sum(values) if values else None

    body = grouped(rows, keep, "Region", key_sales)
    return ["Region", "Key Sales"], body


SHEETS = {
    "ws_sales_by_segment": sales_by_segment,
    "ws_profit_by_ship_mode": profit_by_ship_mode,
    "ws_orders_by_region": orders_by_region,
    "ws_segment_kpis": segment_kpis,
    "ws_monthly_sales_vs_profit": monthly_sales_vs_profit,
    "ws_sales_and_profit_by_year": sales_and_profit_by_year,
    "ws_category_mix": category_mix,
    "ws_region_picker": region_picker,
    "ws_category_detail": category_detail,
    "ws_segment_detail": segment_detail,
    "ws_region_category_matrix": region_category_matrix,
    "ws_sales_by_region_group": sales_by_region_group,
    "ws_sales_by_ship_speed": sales_by_ship_speed,
    "ws_sales_by_quantity_bin": sales_by_quantity_bin,
    "ws_recent_sales_by_category": recent_sales,
    "ws_big_customers_by_segment": big_customers,
    "ws_son_customers_by_region": son_customers,
    "ws_top_sub_categories": top_sub_categories,
    "ws_sales_growth_by_year": sales_growth,
    "ws_selected_region_sales": selected_region_sales,
    "ws_sub_category_sales": sub_category_sales,
    "ws_region_sales_bands": region_sales_bands,
    "ws_monthly_moving_average": monthly_moving_average,
    "ws_category_donut": category_donut,
    "ws_region_sales_and_segment_profit": region_sales_segment_profit,
    "ws_top_customers_vs_others": top_customers_vs_others,
    "ws_profitable_sub_categories": profitable_sub_categories,
    "ws_key_sub_categories_by_category": key_sub_categories,
    "ws_key_sales_by_region": key_sales_by_region,
}


def text(value: Any) -> str:
    if value is None:
        return ""  # Tableau 匯出的空值
    return repr(value) if isinstance(value, float) else str(value)


def main() -> None:
    cases = yaml.safe_load((FIXTURE / "cases.yaml").read_text(encoding="utf-8"))
    rows = load_rows()
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
