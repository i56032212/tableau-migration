"""公式測試用的假資料源。"""

from __future__ import annotations

from tmig.formula.analyze import Analyzer, Target
from tmig.formula.types import DataType

BASE: dict[str, tuple[DataType, str]] = {
    "Sales": ("real", "measure"),
    "Profit": ("real", "measure"),
    "Discount": ("real", "measure"),
    "Quantity": ("integer", "measure"),
    "Region": ("string", "dimension"),
    "Category": ("string", "dimension"),
    "Customer": ("string", "dimension"),
    "Order ID": ("string", "dimension"),
    "Order Date": ("date", "dimension"),
    "Ship Time": ("datetime", "dimension"),
    "Returned": ("boolean", "dimension"),
}

CALCS: dict[str, tuple[DataType, str]] = {
    "Profit Ratio": ("real", "SUM([Profit]) / SUM([Sales])"),
    "Region Sales": ("real", "{FIXED [Region] : SUM([Sales])}"),
    "Margin": ("real", "[Profit] / [Sales]"),
    "Share of Region": ("real", "[Sales] / [Region Sales]"),
    "Loop A": ("integer", "[Loop B] + 1"),
    "Loop B": ("integer", "[Loop A]"),
    "Running Sales": ("real", "RUNNING_SUM(SUM([Sales]))"),
}

PARAMS: dict[str, tuple[str, DataType]] = {"Parameter 1": ("Top N", "integer")}


def slug(name: str) -> str:
    return "f_" + "".join(c if c.isalnum() else "_" for c in name.lower())


class FakeScope:
    def lookup(self, parts: tuple[str, ...]) -> Target | None:
        if len(parts) == 2 and parts[0] == "Parameters":
            for name, (caption, datatype) in PARAMS.items():
                if parts[1] in (name, caption):
                    return Target("param", "p_" + slug(name)[2:], datatype, "param")
            return None
        if len(parts) != 1:
            return None
        name = parts[0]
        if name in BASE:
            datatype, role = BASE[name]
            return Target("field", slug(name), datatype, role)  # type: ignore[arg-type]
        if name in CALCS:
            datatype, formula = CALCS[name]
            return Target("field", slug(name), datatype, "measure", formula)
        return None


def analyzer() -> Analyzer:
    return Analyzer(FakeScope())
