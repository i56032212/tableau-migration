import importlib

import tmig

SUBPACKAGES = ["ir", "parser", "census", "formula", "planner", "sqlgen", "data", "render", "verify"]


def test_version() -> None:
    assert tmig.__version__ == "0.1.0"


def test_subpackages_importable() -> None:
    for name in SUBPACKAGES:
        importlib.import_module(f"tmig.{name}")
