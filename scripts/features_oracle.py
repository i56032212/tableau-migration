"""功能報表的預期結果：以純 Python 獨立計算（不呼叫 tmig 的規劃器、SQL 或 DuckDB）。

    uv run python scripts/features_oracle.py

寫出 fixtures/superstore_features/expected/<工作表 id>/<篩選組合>.csv。與 scripts/demo_oracle.py 相同，
它和 tmig 依據同一份對 Tableau 語意的理解，只能抓出實作錯誤，抓不出語意誤解。

篩選的語意：
- 資料源篩選：Country/Region 只保留 United States
- 全域篩選（<shared-views>）：Region 排除 Central，套用到篩選架上有 Region 的工作表
- 篩選群組：Segment 只保留 Consumer、Corporate，套用到 Sales by Segment 與 Profit by Ship Mode
- 網址參數取代同欄位的既有篩選（含全域篩選）；工作表沒有該欄位的篩選時新增一個
"""

from __future__ import annotations

import csv
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


SHEETS = {
    "ws_sales_by_segment": sales_by_segment,
    "ws_profit_by_ship_mode": profit_by_ship_mode,
    "ws_orders_by_region": orders_by_region,
}


def text(value: Any) -> str:
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
