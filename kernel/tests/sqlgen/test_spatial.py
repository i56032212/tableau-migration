"""空間函式的 SQL（MAKEPOINT、MAKELINE、DISTANCE、BUFFER、AREA 與彙總 COLLECT）：同一份萃取以 Hyper 的
tableau.geo_* 函式（Tableau 的引擎）計算，與匯入 DuckDB 後以本平台的 SQL 計算的結果相同。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tmig.data.executor import execute
from tmig.data.hyper_import import import_hyper_tables
from tmig.formula.analyze import Analyzer, Target
from tmig.formula.ast import Call, Lod
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.sqlgen.dialect import DialectError, to_dialect
from tmig.sqlgen.expr import ExprCompiler
from tmig.sqlgen.sql import ident

hyperapi = pytest.importorskip("tableauhyperapi")

ROWS = [
    [
        "Taipei 101",
        25.0339,
        121.5645,
        3826,
        "POLYGON((121.50 25.00, 121.60 25.00, 121.60 25.10, 121.50 25.10, 121.50 25.00))",
    ],
    [
        "Kaohsiung",
        22.6273,
        120.3014,
        3826,
        "POLYGON((120 22, 122 22, 122 25, 120 25, 120 22), (120.5 22.5, 121 22.5, 121 23, 120.5 23, 120.5 22.5))",
    ],
    [
        "Cape Town",
        -33.9,
        18.4,
        32734,
        "MULTIPOLYGON(((18 -34, 19 -34, 19 -33, 18 -34)), ((20 -34, 21 -34, 21 -33, 20 -34)))",
    ],
    ["NYC", 40.7, -74.0, 26918, "LINESTRING(-74 40, -73 41)"],
    ["Nothing", None, None, None, None],
]
FIELDS = {"Name": "string", "Lat": "real", "Lon": "real", "Srid": "integer", "Geometry": "spatial"}
TAIPEI = "25.0339::double precision, 121.5645::double precision"


class Scope:
    def lookup(self, parts: tuple[str, ...]) -> Target | None:
        if len(parts) == 1 and parts[0] in FIELDS:
            datatype = FIELDS[parts[0]]
            role = "dimension" if datatype == "string" else "measure"
            return Target("field", parts[0], datatype, role)  # type: ignore[arg-type]
        return None


class Env:
    week_start = "sunday"

    def column(self, target: Target) -> str:
        return "t." + ident(target.id)

    def param(self, target: Target) -> str:
        raise AssertionError

    def param_value(self, target: Target) -> object:
        raise AssertionError

    def lod(self, node: Lod) -> str:
        raise AssertionError

    def aggregate(self, node: Call) -> str:
        raise AssertionError

    def table_calc(self, node: Call) -> str:
        raise AssertionError


@pytest.fixture(scope="module")
def extract(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """.hyper（GEOGRAPHY 欄位）與匯入後的 DuckDB 檔。"""
    folder = tmp_path_factory.mktemp("spatial-sql")
    hyper_path = folder / "t.hyper"
    table = hyperapi.TableDefinition(
        hyperapi.TableName("Extract", "Extract"),
        [
            hyperapi.TableDefinition.Column("Name", hyperapi.SqlType.text()),
            hyperapi.TableDefinition.Column("Lat", hyperapi.SqlType.double()),
            hyperapi.TableDefinition.Column("Lon", hyperapi.SqlType.double()),
            hyperapi.TableDefinition.Column("Srid", hyperapi.SqlType.big_int()),
            hyperapi.TableDefinition.Column("Geometry", hyperapi.SqlType.geography()),
        ],
    )
    staged = [*table.columns[:4], hyperapi.TableDefinition.Column("wkt", hyperapi.SqlType.text())]
    mapping = [
        *(hyperapi.Inserter.ColumnMapping(c) for c in ("Name", "Lat", "Lon", "Srid")),
        hyperapi.Inserter.ColumnMapping("Geometry", 'CAST("wkt" AS GEOGRAPHY)'),
    ]
    with (
        hyperapi.HyperProcess(
            hyperapi.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
            parameters={"log_dir": str(folder)},
        ) as hyper,
        hyperapi.Connection(hyper.endpoint, str(hyper_path), hyperapi.CreateMode.CREATE) as conn,
    ):
        conn.catalog.create_schema("Extract")
        conn.catalog.create_table(table)
        with hyperapi.Inserter(conn, table, mapping, inserter_definition=staged) as inserter:
            inserter.add_rows(ROWS)
            inserter.execute()
    log = folder / "log"
    log.mkdir()
    (imported,), _ = import_hyper_tables(
        hyper_path, [("Extract", "Extract")], folder / "t.duckdb", log
    )
    assert imported.ok
    assert [(c.name, c.duckdb_type) for c in imported.columns][-1] == ("Geometry", "VARCHAR")
    return hyper_path, folder / "t.duckdb"


@pytest.fixture(scope="module")
def hyper(extract: tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory) -> Any:
    log = tmp_path_factory.mktemp("spatial-hyper-log")
    with (
        hyperapi.HyperProcess(
            hyperapi.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
            parameters={"log_dir": str(log)},
        ) as process,
        hyperapi.Connection(process.endpoint, str(extract[0])) as conn,
    ):
        yield conn


def ours(db: Path, formula: str, mode: str = "row", where: str = "") -> list[Any]:
    compiler = ExprCompiler(Analyzer(Scope()), Env())
    node = parse_formula(formula)
    sql = compiler.row(node) if mode == "row" else compiler.agg(node)
    order = "" if mode == "agg" else ' ORDER BY "Name"'
    rows = execute(db, f'SELECT {sql} FROM "Extract"."Extract" t {where}{order}').rows
    return [r[0] for r in rows]


def theirs(conn: Any, sql: str, where: str = "", order: bool = True) -> list[Any]:
    tail = ' ORDER BY "Name"' if order else ""
    return [
        r[0]
        for r in conn.execute_list_query(f'SELECT {sql} FROM "Extract"."Extract" {where}{tail}')
    ]


def numbers(text: str) -> list[float]:
    return [float(v) for v in re.findall(r"-?\d+\.\d+", text)]


CASES = [
    ("MAKEPOINT([Lat], [Lon])", 'CAST(tableau.geo_make_point("Lat", "Lon") AS TEXT)', "text"),
    (
        "MAKELINE(MAKEPOINT([Lat], [Lon]), MAKEPOINT(25.0339, 121.5645))",
        f'CAST(tableau.geo_make_line(tableau.geo_make_point("Lat", "Lon"), tableau.geo_make_point({TAIPEI})) AS TEXT)',
        "text",
    ),
    (
        "DISTANCE(MAKEPOINT([Lat], [Lon]), MAKEPOINT(25.0339, 121.5645), 'km')",
        f'tableau.geo_distance(tableau.geo_make_point("Lat", "Lon"), tableau.geo_make_point({TAIPEI}), \'km\')',
        "number",
    ),
    (
        "DISTANCE(MAKEPOINT([Lat], [Lon]), MAKEPOINT(25.0339, 121.5645), 'Miles')",
        f'tableau.geo_distance(tableau.geo_make_point("Lat", "Lon"), tableau.geo_make_point({TAIPEI}), \'Miles\')',
        "number",
    ),
    (
        "BUFFER(MAKEPOINT([Lat], [Lon]), 300, 'm')",
        'CAST(tableau.geo_buffer(tableau.geo_make_point("Lat", "Lon"), 300::double precision, \'m\') AS TEXT)',
        "shape",
    ),
    (
        "AREA(BUFFER(MAKEPOINT([Lat], [Lon]), 5, 'km'), 'km')",
        "tableau.geo_area(tableau.geo_buffer(tableau.geo_make_point(\"Lat\", \"Lon\"), 5::double precision, 'km'), 'km')",
        "number",
    ),
    ("AREA([Geometry], 'mi')", "tableau.geo_area(\"Geometry\", 'mi')", "number"),
    ("AREA([Geometry], 'ft')", "tableau.geo_area(\"Geometry\", 'ft')", "number"),
]


@pytest.mark.parametrize(("formula", "sql", "kind"), CASES)
def test_functions_match_hyper(
    extract: tuple[Path, Path], hyper: Any, formula: str, sql: str, kind: str
) -> None:
    found = ours(extract[1], formula)
    expected = theirs(hyper, sql)
    assert len(found) == len(expected) == len(ROWS)
    for a, b in zip(found, expected, strict=True):
        if b is None:
            assert a is None
        elif kind == "number":
            assert a == pytest.approx(b, rel=1e-6)
        elif kind == "shape":
            assert len(numbers(a)) == len(numbers(b))
            assert max(abs(x - y) for x, y in zip(numbers(a), numbers(b), strict=True)) <= 3e-7
        else:
            assert a == b


def test_collect_matches_hyper(extract: tuple[Path, Path], hyper: Any) -> None:
    where = "WHERE \"Name\" IN ('Taipei 101', 'Cape Town')"
    (found,) = ours(extract[1], "COLLECT([Geometry])", "agg", where)
    (expected,) = theirs(hyper, 'CAST(tableau.geo_collect("Geometry") AS TEXT)', where, order=False)
    # 多邊形的順序依資料列的順序（Tableau 也不保證）：比對座標
    assert found.startswith("MULTIPOLYGON(((") and expected.startswith("MULTIPOLYGON(((")
    assert sorted(numbers(found)) == sorted(numbers(expected))
    # 全是 Null 時為 Null；不同種的形狀合成 GEOMETRYCOLLECTION
    assert ours(extract[1], "COLLECT([Geometry])", "agg", "WHERE \"Name\" = 'Nothing'") == [None]
    (mixed,) = ours(
        extract[1], "COLLECT([Geometry])", "agg", "WHERE \"Name\" IN ('NYC', 'Cape Town')"
    )
    assert mixed.startswith("GEOMETRYCOLLECTION(")


def test_makepoint_with_srid(extract: tuple[Path, Path]) -> None:
    """MAKEPOINT(X, Y, SRID)：投影座標換成 WGS84 的點（Hyper 沒有這個函式，以投影公式驗算來回）。"""
    from tmig.geo.projection import crs_from_epsg

    crs = crs_from_epsg(3826)
    x, y = crs.forward([121.5645], [25.0339])
    found = ours(
        extract[1],
        f"MAKEPOINT({float(x[0])!r}, {float(y[0])!r}, 3826)",
        where="WHERE \"Name\" = 'NYC'",
    )
    assert found == ["POINT(121.5645000 25.0339000)"]
    unknown = ours(extract[1], "MAKEPOINT(1, 2, [Srid] + 900000)", where="WHERE \"Name\" = 'NYC'")
    assert unknown == [None]


@pytest.mark.parametrize(
    ("formula", "message"),
    [
        ("DISTANCE(MAKEPOINT([Lat], [Lon]), MAKEPOINT(1, 2), 'yards')", "單位"),
        ("AREA([Lat], 'km')", "空間資料"),
        ("MAKEPOINT([Name], [Lon])", "數字"),
        ("BUFFER([Geometry], 'a', 'km')", "數字"),
        ("COLLECT([Lat])", "空間資料"),
    ],
)
def test_invalid_formulas(formula: str, message: str) -> None:
    with pytest.raises(FormulaSemanticError, match=message):
        Analyzer(Scope()).analyze(parse_formula(formula))


def test_live_connections_do_not_translate_spatial() -> None:
    sql = ExprCompiler(Analyzer(Scope()), Env()).row(
        parse_formula("DISTANCE(MAKEPOINT([Lat], [Lon]), MAKEPOINT(1, 2), 'km')")
    )
    with pytest.raises(DialectError, match="空間資料"):
        to_dialect(f'SELECT {sql} AS v FROM "Extract"."Extract" t', "postgres")
