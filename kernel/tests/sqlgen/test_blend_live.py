"""Data Blending 的主要或次要資料源是 Live 連線：與 Tableau 相同，次要資料源的查詢先在自己的來源執行
（Live 連線在資料庫依連結欄位彙總），結果登記成資料表，併入主要資料源的查詢（主要資料源是 Live 連線時，
併入之前的部分在資料庫彙總，之後的部分在 DuckDB 計算）。

以臨時的 PostgreSQL 與 MySQL 實跑（沒有時略過；TMIG_REQUIRE_ENGINES=1 時改為失敗）：test_blend 的工作表
（依地區連結、在未連結的維度上重複、以年連結、自訂關聯、計算欄位、篩選、排序、Table Calculation、總計）
在各種組合（主要資料源 PostgreSQL、次要資料源 PostgreSQL 或 MySQL、萃取與 Live 互換）下，結果與兩個都是
萃取時相同。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from tmig.data.executor import ExecutionError, execute_query
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig, Executor
from tmig.ir import IR, LiveConnection, TableRelation
from tmig.sqlgen.query import worksheet_sql

from ..engines import mysql_connect, mysql_load, mysql_server, pg_connect, pg_load, postgres_cluster
from ..parser.builder import ref
from .test_blend import (
    AVG_TARGET,
    blended,
    fields,
    parse,
    relationship,
    target_ref,
)
from .test_query import CATEGORY, REGION, SALES, YEAR

PG_ID = "blend_pg"
MYSQL_ID = "blend_mysql"


def extract(folder: Path, ds: str) -> pa.Table:
    with duckdb.connect(str(folder / f"{ds}.duckdb"), read_only=True) as con:
        return con.sql('SELECT * FROM "Extract"."Extract"').to_arrow_table()


@pytest.fixture(scope="module")
def folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("blend_live")


@pytest.fixture(scope="module")
def runtime(folder: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[DataRuntime]:
    """訂單與目標都載入 PostgreSQL；目標另外載入 MySQL（資料與 test_blend 的萃取相同）。"""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    ir = parse(tmp_path_factory.mktemp("xml"), folder, plain_sheet())
    orders, targets = (d.id for d in ir.datasources)
    with postgres_cluster() as dsn, mysql_server() as mysql:
        admin = pg_connect(dsn)
        admin.execute("DROP DATABASE IF EXISTS blend")
        admin.execute("CREATE DATABASE blend")
        admin.close()
        con = pg_connect(make_conninfo(dsn, dbname="blend"))
        pg_load(con, orders, extract(folder, orders), "public")
        pg_load(con, targets, extract(folder, targets), "public")
        con.close()
        my = mysql_connect(mysql, "blend")
        mysql_load(my, targets, extract(folder, targets))
        my.close()
        info = conninfo_to_dict(dsn)
        params = {k: v for k, v in info.items() if k in ("host", "port", "user", "password")}
        configs = {
            PG_ID: ConnectionConfig(PG_ID, "postgres", {**params, "database": "blend"}),
            MYSQL_ID: ConnectionConfig(MYSQL_ID, "mysql", {**mysql, "database": "blend"}),
        }
        with DataRuntime(configs) as found:
            yield found


def plain_sheet() -> str:
    return blended(rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))


def live(ir: IR, index: int, connection_id: str, dialect: str, schema: str) -> IR:
    """第 index 個資料源改成 Live 連線（資料表為 <schema>.<資料源 id>，欄名與萃取相同）。"""
    ds = ir.datasources[index]
    connection = LiveConnection(
        kind="live",
        connection_id=connection_id,
        dbclass=dialect,
        dialect=dialect,  # type: ignore[arg-type]
        relation=TableRelation(kind="table", name=ds.id, table=[schema, ds.id]),
    )
    swapped = ds.model_copy(update={"connection": connection})
    return ir.model_copy(
        update={"datasources": [swapped if d.id == ds.id else d for d in ir.datasources]}
    )


COMBOS = {
    "主要 PostgreSQL、次要萃取": lambda ir: live(ir, 0, PG_ID, "postgres", "public"),
    "主要萃取、次要 PostgreSQL": lambda ir: live(ir, 1, PG_ID, "postgres", "public"),
    "都是 PostgreSQL": lambda ir: live(
        live(ir, 0, PG_ID, "postgres", "public"), 1, PG_ID, "postgres", "public"
    ),
    "主要 PostgreSQL、次要 MySQL": lambda ir: live(
        live(ir, 0, PG_ID, "postgres", "public"), 1, MYSQL_ID, "mysql", "blend"
    ),
    "主要萃取、次要 MySQL": lambda ir: live(ir, 1, MYSQL_ID, "mysql", "blend"),
}


class Recording:
    """記下送到資料庫的 SQL。"""

    def __init__(self, inner: Executor) -> None:
        self.inner = inner
        self.dialect = inner.dialect
        self.sqls: list[str] = []

    def fetch(self, sql: str) -> pa.Table:
        self.sqls.append(sql)
        return self.inner.fetch(sql)

    def close(self) -> None:
        self.inner.close()


def recording(runtime: DataRuntime, ir: IR) -> dict[str, Recording]:
    """各 Live 連線的執行器換成記下 SQL 的版本。"""
    found: dict[str, Recording] = {}
    for ds in ir.datasources:
        connection = ds.connection
        if isinstance(connection, LiveConnection) and connection.connection_id not in found:
            with runtime._executor(connection) as executor:  # pyright: ignore[reportPrivateUsage]
                inner = executor.inner if isinstance(executor, Recording) else executor
            wrapped = Recording(inner)
            runtime._executors[connection.connection_id] = wrapped  # pyright: ignore[reportPrivateUsage]
            found[connection.connection_id] = wrapped
    return found


def run_all(runtime: DataRuntime, ir: IR, folder: Path) -> list[dict[str, Any]]:
    """兩個都是萃取時的結果；各種 Live 的組合都要與它相同。"""
    ws = ir.worksheets[0]
    expected = execute_query(folder / f"{ws.datasource}.duckdb", worksheet_sql(ir, ws.id)).records()
    for name, convert in COMBOS.items():
        changed = convert(ir)
        query = worksheet_sql(changed, ws.id)
        assert [s.name for s in query.stages][:1] == ["__s0"], name
        primary = next(d for d in changed.datasources if d.id == ws.datasource)
        found = runtime.run(query, primary, folder / f"{primary.id}.duckdb").records()
        assert found == expected, name
    return expected


def test_secondary_measure_by_linked_region(
    runtime: DataRuntime, folder: Path, tmp_path: Path
) -> None:
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    target, sales = f"sum({ids['Targets.Target']})", f"sum({ids['Sales']})"
    expected = run_all(runtime, ir, folder)
    assert {r[ids["Region"]]: (r[sales], r[target]) for r in expected} == {
        "East": (350.0, 400.0),
        "North": (60.0, None),
        "South": (100.0, 50.0),
        "West": (800.0, 900.0),
    }
    # 次要資料源在 MySQL 依連結欄位（地區、Order Date 不在檢視中所以只有地區）彙總，主要資料源在 PostgreSQL 彙總
    changed = COMBOS["主要 PostgreSQL、次要 MySQL"](ir)
    recorders = recording(runtime, changed)
    ws = changed.worksheets[0]
    runtime.run(worksheet_sql(changed, ws.id), changed.datasources[0], folder / "unused.duckdb")
    (pg_sql,) = recorders[PG_ID].sqls
    (mysql_sql,) = recorders[MYSQL_ID].sqls
    assert "GROUP BY" in pg_sql and "SUM(" in pg_sql.upper()
    assert "FROM `blend`.`ds_targets`" in mysql_sql
    assert "SUM(`f_target`)" in mysql_sql and "GROUP BY 1" in mysql_sql


def test_secondary_values_repeat_over_unlinked_dimensions(
    runtime: DataRuntime, folder: Path, tmp_path: Path
) -> None:
    sheet = blended(
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        cols=ref("sum:Sales:qk"),
        instances=[REGION, CATEGORY, SALES],
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    expected = run_all(runtime, ir, folder)
    # 順序與主要資料源的查詢相同（地區、類別依字母）
    assert [r[ids["Region"]] for r in expected] == [
        "East",
        "East",
        "North",
        "South",
        "South",
        "West",
        "West",
    ]


def test_date_link_takes_the_derivation_of_the_view(
    runtime: DataRuntime, folder: Path, tmp_path: Path
) -> None:
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=ref("yr:Order Date:ok"),
        instances=[REGION, YEAR, SALES],
        encodings=(
            f"<text column='{target_ref('sum:Target:qk')}' />"
            f"<tooltip column='{ref('sum:Sales:qk')}' />"
        ),
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    expected = run_all(runtime, ir, folder)
    target, year = f"sum({ids['Targets.Target']})", f"year({ids['Order Date']})"
    assert {(r[ids["Region"]], r[year]): r[target] for r in expected}[("East", 2023)] == 300.0


def test_custom_relationship(runtime: DataRuntime, folder: Path, tmp_path: Path) -> None:
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=target_ref("avg:Target:qk"),
        secondary=[AVG_TARGET],
    )
    extra = relationship(ref("none:Region:nk"), target_ref("none:Area:nk"))
    ir = parse(tmp_path, folder, sheet, extra)
    ids = fields(ir)
    expected = run_all(runtime, ir, folder)
    target = f"avg({ids['Targets.Target']})"
    assert {r[ids["Region"]]: r[target] for r in expected} == {
        "East": 200.0,
        "North": None,
        "South": 50.0,
        "West": 900.0,
    }


def test_live_secondary_needs_the_runtime(folder: Path, tmp_path: Path) -> None:
    """次要資料源在資料庫中時，不能只以萃取的 DuckDB 執行；次要資料源的查詢列在 stages 中。"""
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
    )
    extracts = parse(tmp_path, folder, sheet)
    ir = live(extracts, 1, PG_ID, "postgres", "public")
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    (stage,) = query.stages
    assert (stage.name, stage.datasource.caption) == ("__s0", "Targets")
    assert stage.query.live and not query.live
    assert any("Data Blending" in line for note in query.plan for line in note.detail)
    with pytest.raises(ExecutionError, match="DataRuntime"):
        execute_query(folder / f"{ws.datasource}.duckdb", query)
    # 主要資料源是 Live 連線：次要資料源併入之後的部分不下推到資料庫
    primary = worksheet_sql(live(extracts, 0, PG_ID, "postgres", "public"), ws.id)
    joined = [cte for cte in primary.ctes if '"__s0"' in cte.body]
    assert joined and not any(cte.pushdown for cte in joined)
    assert any(cte.pushdown for cte in primary.ctes)


def test_calcs_and_secondary_dimensions(runtime: DataRuntime, folder: Path, tmp_path: Path) -> None:
    """引用次要資料源欄位的計算欄位（實績 ÷ 目標）與次要資料源的維度（ATTR，多個值時為 *）：各部分在各自的
    資料庫彙總，合併後再計算，與都是萃取時相同。"""
    from ..parser import builder as b
    from ..parser.builder import instance
    from .test_blend import ATTAINMENT, BLEND_CALCS, GAP

    year_text = b.calc(
        "Calculation_year", "Year Text", "STR(YEAR([Order Date]))", "string", "dimension"
    )
    year = instance("[Calculation_year]", "None", "[none:Calculation_year:nk]")
    sheet = blended(
        rows=f"({ref('none:Region:nk')} / {target_ref('none:Calculation_year:nk')})",
        cols=f"({ref('usr:Calculation_att:qk')} + {ref('usr:Calculation_gap:qk')})",
        instances=[REGION, ATTAINMENT, GAP],
        secondary=[year],
    )
    ir = parse(tmp_path, folder, sheet, columns=BLEND_CALCS, secondary_columns=year_text)
    assert ir.unsupported == []
    ids = fields(ir)
    expected = run_all(runtime, ir, folder)
    key = f"attr({ids['Targets.Year Text']})"
    assert {r[ids["Region"]]: (r[key], r[ids["Attainment"]], r[ids["Gap"]]) for r in expected} == {
        "East": ("*", 350.0 / 400.0, 50.0),
        "North": (None, None, -60.0),
        "South": ("2024", 2.0, 0.0),
        "West": ("2023", 800.0 / 900.0, 100.0),
    }


def test_secondary_filters_and_totals(runtime: DataRuntime, folder: Path, tmp_path: Path) -> None:
    """次要資料源的篩選（含 Null）與總計、小計：各種 Live 的組合都與都是萃取時相同。"""
    from tmig.sqlgen.query import totals_sql

    from .test_blend import (
        ATTAINMENT,
        BLEND_CALCS,
        TARGET,
        YEAR_TEXT,
        filtered_sheet,
        year_filter,
    )

    def totals(ir: IR, run: Any) -> list[list[dict[str, Any]]]:
        ws = ir.worksheets[0]
        main = worksheet_sql(ir, ws.id)
        marks = run(main)
        return [run(t.query).records() for t in totals_sql(ir, ws.id, main, None, None, marks)]

    def compare(ir: IR) -> list[list[dict[str, Any]]]:
        ws = ir.worksheets[0]
        db = folder / f"{ws.datasource}.duckdb"
        expected = totals(ir, lambda q: execute_query(db, q))
        assert expected and all(expected)
        for name, convert in COMBOS.items():
            changed = convert(ir)
            primary = next(d for d in changed.datasources if d.id == ws.datasource)
            assert totals(changed, lambda q, p=primary: runtime.run(q, p, db)) == expected, name
        return expected

    # 篩選 2024 年與 Null（North 沒有目標仍顯示，West 的目標都被篩選掉）
    sheet = filtered_sheet(year_filter(["2024", "%null%"]))
    ir = parse(tmp_path, folder, sheet, secondary_columns=YEAR_TEXT)
    ids = fields(ir)
    found = run_all(runtime, ir, folder)
    assert [r[ids["Region"]] for r in found] == ["East", "North", "South"]
    ws = ir.worksheets[0].model_copy(update={"totals": ["rows"]})
    ((grand,),) = compare(ir.model_copy(update={"worksheets": [ws]}))
    assert (grand[f"sum({ids['Sales']})"], grand[f"sum({ids['Targets.Target']})"]) == (510.0, 150.0)
    # 總計與各地區的小計、引用次要資料源欄位的計算欄位
    sheet = blended(
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        cols=f"({ref('sum:Sales:qk')} + {ref('usr:Calculation_att:qk')})",
        instances=[REGION, CATEGORY, SALES, ATTAINMENT],
        secondary=[TARGET],
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
    )
    ir = parse(tmp_path, folder, sheet, columns=BLEND_CALCS)
    region = ir.worksheets[0].rows[0]
    ws = ir.worksheets[0].model_copy(update={"totals": ["rows"], "subtotals": [region]})
    results = compare(ir.model_copy(update={"worksheets": [ws]}))
    assert sorted(len(rows) for rows in results) == [1, 4]


def test_measure_filters_sorts_and_table_calcs(
    runtime: DataRuntime, folder: Path, tmp_path: Path
) -> None:
    """次要資料源的量值篩選、依它排序、套用 Table Calculation（快速計算與次要資料源中的計算欄位），以及
    引用次要資料源欄位的計算欄位放在篩選與排序上：各種 Live 的組合都與都是萃取時相同。"""
    from .test_blend import (
        ATTAINMENT,
        BLEND_CALCS,
        RUNNING,
        SHARE,
        TARGET,
        TARGET_CALCS,
        by_field,
        down,
        measure_filter,
        measure_sheet,
        quick_target,
    )

    def regions(ir: IR) -> list[Any]:
        return [r[fields(ir)["Region"]] for r in run_all(runtime, ir, folder)]

    text = f"<text column='{target_ref('sum:Target:qk')}' />"
    # 量值篩選（目標 ≥ 100）與依目標排序（遞減）
    extra = measure_filter(target_ref("sum:Target:qk"), 100) + by_field(target_ref("sum:Target:qk"))
    assert regions(parse(tmp_path, folder, measure_sheet(encodings=text, view_extra=extra))) == [
        "West",
        "East",
    ]
    # 只放在排序依據上；排序的層級（地區）比連結欄位（地區、年）粗
    sheet = measure_sheet(
        cols=ref("yr:Order Date:ok"),
        instances=[REGION, YEAR, SALES],
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        view_extra=by_field(target_ref("sum:Target:qk")),
    )
    found = regions(parse(tmp_path, folder, sheet))
    assert list(dict.fromkeys(found)) == ["West", "East", "South", "North"]
    # 引用次要資料源欄位的計算欄位（達成率）放在篩選與排序上
    extra = measure_filter(ref("usr:Calculation_att:qk"), 0.88) + by_field(
        ref("usr:Calculation_att:qk")
    )
    sheet = measure_sheet(instances=[REGION, SALES, ATTAINMENT], view_extra=extra)
    assert regions(parse(tmp_path, folder, sheet, columns=BLEND_CALCS)) == ["South", "West"]

    # Table Calculation：累計、占比、排名，以及以累計的結果篩選
    secondary = [
        TARGET,
        quick_target("CumTotal", "cum"),
        quick_target("PctTotal", "pcto"),
        quick_target("Rank", "rank", " rank-options='Competition,Descending'"),
    ]
    encodings = (
        f"<text column='{target_ref('cum:sum:Target:qk')}' />"
        f"<tooltip column='{target_ref('pcto:sum:Target:qk')}' />"
        f"<tooltip column='{target_ref('rank:sum:Target:qk')}' />"
    )
    ir = parse(tmp_path, folder, down(encodings, secondary))
    ids = fields(ir)
    running = next(tc.id for tc in ir.worksheets[0].table_calcs if tc.type == "running_total")
    key = f"sum({ids['Targets.Target']})#{running}"
    assert {r[ids["Region"]]: r[key] for r in run_all(runtime, ir, folder)} == {
        "East": 400.0,
        "North": 400.0,
        "South": 450.0,
        "West": 1350.0,
    }
    flt = measure_filter(target_ref("cum:sum:Target:qk"), 450)
    assert regions(parse(tmp_path, folder, down(encodings, secondary, view_extra=flt))) == [
        "South",
        "West",
    ]
    # 次要資料源中的 Table Calculation 計算欄位
    encodings = (
        f"<text column='{target_ref('usr:Calculation_run:qk')}' />"
        f"<tooltip column='{target_ref('usr:Calculation_share:qk')}' />"
    )
    ir = parse(tmp_path, folder, down(encodings, [RUNNING, SHARE]), secondary_columns=TARGET_CALCS)
    assert ir.unsupported == []
    assert len(run_all(runtime, ir, folder)) == 4
