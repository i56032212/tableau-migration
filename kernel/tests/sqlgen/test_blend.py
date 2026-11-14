"""Data Blending：次要資料源依連結欄位彙總後併到主要資料源的結果。

主要資料源為 test_query 的訂單（East 350、West 800、South 100、North 60）；次要資料源為各地區、
各年的目標（TARGETS），Central 只在次要資料源中、North 沒有目標。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest

from tmig.data.executor import execute_query
from tmig.ir import IR, Blend, BlendLink, CategoricalFilter, FieldUse
from tmig.ir.integrity import check_references, iter_zones
from tmig.parser.workbook import parse_workbook
from tmig.planner.state import use_key
from tmig.render.vegalite import build_spec
from tmig.sqlgen.query import QueryError, totals_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import DS, instance, ref, worksheet
from .test_query import CATEGORY, REGION, ROWS, SALES, YEAR

TARGETS = "federated.targets"
TARGET_COLUMNS = [
    ("Region", "string", "Count"),
    ("Area", "string", "Count"),
    ("Order Date", "date", "Year"),
    ("Target", "real", "Sum"),
]
TARGET_ROWS = [
    ("East", "East", dt.date(2023, 1, 1), 300.0),
    ("East", "East", dt.date(2024, 1, 1), 100.0),
    ("West", "West", dt.date(2023, 1, 1), 900.0),
    ("South", "South", dt.date(2024, 1, 1), 50.0),
    ("Central", "Central", dt.date(2024, 1, 1), 999.0),
]
TARGET = instance("[Target]", "Sum", "[sum:Target:qk]", "quantitative")
AVG_TARGET = instance("[Target]", "Avg", "[avg:Target:qk]", "quantitative")
TARGET_REGION = instance("[Region]", "None", "[none:Region:nk]")


def target_ref(inst: str) -> str:
    return f"[{TARGETS}].[{inst}]"


def targets_datasource(columns: str = "") -> str:
    def records(parent: str) -> str:
        return (
            "<metadata-records>"
            + "".join(
                f"""<metadata-record class='column'>
              <remote-name>{name}</remote-name><remote-type>129</remote-type>
              <local-name>[{name}]</local-name><parent-name>{parent}</parent-name>
              <ordinal>{i}</ordinal><local-type>{datatype}</local-type>
              <aggregation>{agg}</aggregation>
            </metadata-record>"""
                for i, (name, datatype, agg) in enumerate(TARGET_COLUMNS)
            )
            + "</metadata-records>"
        )

    return f"""
    <datasource caption='Targets' inline='true' name='{TARGETS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='src' name='textscan.2'>
            <connection class='textscan' directory='C:/data' filename='targets.csv' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.2' name='targets.csv' table='[targets#csv]' type='table' />
        {records("[targets.csv]")}
      </connection>
      <extract count='-1' enabled='true' units='records'>
        <connection class='hyper' dbname='Data/Extracts/targets.hyper' schema='Extract' tablename='Extract'>
          <relation name='Extract' table='[Extract].[Extract]' type='table' />
          {records("[Extract]")}
        </connection>
      </extract>
      <aliases enabled='yes' />
      {columns}
    </datasource>"""


def blended(
    rows: str,
    cols: str = "",
    instances: list[str] | None = None,
    secondary: list[str] | None = None,
    encodings: str = "",
    table_extra: str = "",
) -> str:
    deps = "".join(secondary or [TARGET])
    sources = f"<datasource caption='Orders' name='{DS}' /><datasource caption='Targets' name='{TARGETS}' />"
    return worksheet(
        "Blend",
        instances or [REGION, SALES],
        rows=rows,
        cols=cols,
        encodings=encodings,
        datasources=sources,
        view_extra=f"<datasource-dependencies datasource='{TARGETS}'>{deps}</datasource-dependencies>",
        table_extra=table_extra,
    )


def relationship(primary: str, secondary: str) -> str:
    return f"""
    <datasource-relationships>
      <datasource-dependencies datasource='{DS}'>{REGION}</datasource-dependencies>
      <datasource-dependencies datasource='{TARGETS}'>
        {instance("[Area]", "None", "[none:Area:nk]")}
      </datasource-dependencies>
      <datasource-relationship source='{DS}' target='{TARGETS}'>
        <column-mapping><map key='{primary}' value='{secondary}' /></column-mapping>
      </datasource-relationship>
    </datasource-relationships>"""


@pytest.fixture(scope="module")
def folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """主要與次要資料源的 DuckDB 檔放在同一目錄，以資料源 id 為檔名（與報表目錄的 data/ 相同）。"""
    return tmp_path_factory.mktemp("blend")


def parse(
    tmp_path: Path,
    folder: Path,
    sheet: str,
    extra: str = "",
    columns: str = "",
    secondary_columns: str = "",
) -> IR:
    xml = b.workbook(
        b.extract_datasource(columns) + targets_datasource(secondary_columns),
        worksheets=sheet,
        extra=extra,
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    for ds, rows, table_columns in (
        (ir.datasources[0].id, ROWS, None),
        (ir.datasources[1].id, TARGET_ROWS, TARGET_COLUMNS),
    ):
        path = folder / f"{ds}.duckdb"
        if path.exists():
            continue
        with duckdb.connect(str(path)) as con:
            con.execute('CREATE SCHEMA "Extract"')
            if table_columns is None:
                con.execute(
                    'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Category" VARCHAR, '
                    '"Order ID" VARCHAR, "Order Date" DATE, "Sales" DOUBLE, "Profit" DOUBLE, '
                    '"Quantity" BIGINT)'
                )
            else:
                con.execute(
                    'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Area" VARCHAR, '
                    '"Order Date" DATE, "Target" DOUBLE)'
                )
            marks = ", ".join("?" for _ in rows[0])
            con.executemany(f'INSERT INTO "Extract"."Extract" VALUES ({marks})', rows)
    return ir


def run(ir: IR, folder: Path) -> list[dict[str, object]]:
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    result = execute_query(folder / f"{ws.datasource}.duckdb", query)
    return result.records()


def fields(ir: IR) -> dict[str, str]:
    """欄位名稱 → id（兩個資料源都有 Region 時取次要資料源的加上「Targets.」）。"""
    found: dict[str, str] = {}
    for n, ds in enumerate(ir.datasources):
        for f in ds.fields:
            found[f.caption if n == 0 else f"Targets.{f.caption}"] = f.id
    return found


def test_secondary_measure_by_linked_region(tmp_path: Path, folder: Path) -> None:
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    ws = ir.worksheets[0]
    assert check_references(ir) == []
    # 自動連結：兩個資料源同名的維度（Region、Order Date）
    assert ws.blends == [
        Blend(
            datasource=ir.datasources[1].id,
            links=[
                BlendLink(
                    primary=FieldUse(field=ids["Region"]),
                    secondary=FieldUse(field=ids["Targets.Region"]),
                ),
                BlendLink(
                    primary=FieldUse(field=ids["Order Date"]),
                    secondary=FieldUse(field=ids["Targets.Order Date"]),
                ),
            ],
        )
    ]
    assert ws.marks.label == [FieldUse(field=ids["Targets.Target"], agg="sum", continuous=True)]
    target = f"sum({ids['Targets.Target']})"
    sales = f"sum({ids['Sales']})"
    rows = {r[ids["Region"]]: (r[sales], r[target]) for r in run(ir, folder)}
    # Central 只在次要資料源中，不出現；North 沒有目標，為 Null
    assert rows == {
        "East": (350.0, 400.0),
        "North": (60.0, None),
        "South": (100.0, 50.0),
        "West": (800.0, 900.0),
    }


def test_secondary_values_repeat_over_unlinked_dimensions(tmp_path: Path, folder: Path) -> None:
    """檢視中有 Category（次要資料源沒有）：目標以地區計算，在各類別重複。"""
    sheet = blended(
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        cols=ref("sum:Sales:qk"),
        instances=[REGION, CATEGORY, SALES],
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    target = f"sum({ids['Targets.Target']})"
    records = run(ir, folder)
    east = [(r[ids["Category"]], r[target]) for r in records if r[ids["Region"]] == "East"]
    assert east == [("Furniture", 400.0), ("Technology", 400.0)]
    # 順序與主要資料源的查詢相同（地區、類別依字母）
    assert [r[ids["Region"]] for r in records] == [
        "East",
        "East",
        "North",
        "South",
        "South",
        "West",
        "West",
    ]


def test_date_link_takes_the_derivation_of_the_view(tmp_path: Path, folder: Path) -> None:
    """檢視中是 YEAR(Order Date)：次要資料源也以年分組（地區與年兩個連結都作用）。"""
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
    target = f"sum({ids['Targets.Target']})"
    year = f"year({ids['Order Date']})"
    got = {(r[ids["Region"]], r[year]): r[target] for r in run(ir, folder)}
    assert got == {
        ("East", 2023): 300.0,
        ("North", 2024): None,
        ("South", 2024): 50.0,
        ("West", 2023): 900.0,
        ("West", 2024): None,
    }


def test_custom_relationship(tmp_path: Path, folder: Path) -> None:
    """自訂關聯：主要資料源的 Region 對應次要資料源的 Area（只用自訂的對應，不再自動連結）。"""
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=target_ref("avg:Target:qk"),
        secondary=[AVG_TARGET],
    )
    extra = relationship(ref("none:Region:nk"), target_ref("none:Area:nk"))
    ir = parse(tmp_path, folder, sheet, extra)
    ids = fields(ir)
    ws = ir.worksheets[0]
    assert [(link.primary.field, link.secondary.field) for link in ws.blends[0].links] == [
        (ids["Region"], ids["Targets.Area"])
    ]
    target = f"avg({ids['Targets.Target']})"
    rows = {r[ids["Region"]]: r[target] for r in run(ir, folder)}
    assert rows == {"East": 200.0, "North": None, "South": 50.0, "West": 900.0}


def test_render_uses_the_secondary_caption(tmp_path: Path, folder: Path) -> None:
    sheet = blended(rows=ref("none:Region:nk"), cols=target_ref("sum:Target:qk"))
    ir = parse(tmp_path, folder, sheet)
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    result = execute_query(folder / f"{ws.datasource}.duckdb", query)
    rendered = build_spec(ir, ws.id, query.columns, result)
    assert rendered.spec is not None, rendered.unsupported
    assert rendered.spec["encoding"]["x"]["title"] == "Target"
    assert "Data Blending" in query.sql


def test_unsupported_blends_are_flagged(tmp_path: Path, folder: Path) -> None:
    # 檢視中沒有連結欄位
    sheet = blended(
        rows=ref("none:Category:nk"),
        cols=target_ref("sum:Target:qk"),
        instances=[CATEGORY],
    )
    xml = b.workbook(b.extract_datasource() + targets_datasource(), worksheets=sheet)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert any("沒有作用中的連結" in u.reason for u in ir.unsupported)
    assert ir.worksheets == []


def totals_of(ir: IR, folder: Path, state: object = None) -> dict[tuple[str, ...], list[dict]]:
    """各種總計（去掉的維度）→ 總計的各列。"""
    ws = ir.worksheets[0]
    db = folder / f"{ws.datasource}.duckdb"
    main = worksheet_sql(ir, ws.id, state)  # type: ignore[arg-type]
    marks = execute_query(db, main)
    found = {}
    for total in totals_sql(ir, ws.id, main, state, None, marks):  # type: ignore[arg-type]
        found[tuple(total.dropped)] = execute_query(db, total.query).records()
    return found


def test_totals_with_blending(tmp_path: Path, folder: Path) -> None:
    """總計與小計：主要資料源的量值在總計的層級重新彙總；次要資料源的量值在連結欄位的層級算出後，依總計範圍內
    出現的連結值再彙總（加總、平均等加總型的彙總）；引用次要資料源欄位的計算欄位以總計的各部分計算。
    （Tableau 的說明文件沒有寫明，依公開資料推定。）"""
    avg = instance("[Target]", "Avg", "[avg:Target:qk]", "quantitative")
    sheet = blended(
        rows=f"({ref('none:Region:nk')} / {ref('none:Category:nk')})",
        cols=f"({ref('sum:Sales:qk')} + {ref('usr:Calculation_att:qk')})",
        instances=[REGION, CATEGORY, SALES, ATTAINMENT],
        secondary=[TARGET, avg],
        encodings=(
            f"<text column='{target_ref('sum:Target:qk')}' />"
            f"<tooltip column='{target_ref('avg:Target:qk')}' />"
        ),
    )
    ir = parse(tmp_path, folder, sheet, columns=BLEND_CALCS)
    assert ir.unsupported == []
    ids = fields(ir)
    region, category = ir.worksheets[0].rows
    ws = ir.worksheets[0].model_copy(update={"totals": ["rows"], "subtotals": [region]})
    ir = ir.model_copy(update={"worksheets": [ws]})
    sales, target = f"sum({ids['Sales']})", f"sum({ids['Targets.Target']})"
    average, attainment = f"avg({ids['Targets.Target']})", ids["Attainment"]

    def values(record: dict) -> tuple:
        return (record[sales], record[target], record[average], record[attainment])

    totals = totals_of(ir, folder)
    # 總計：目標是各地區（連結欄位）的目標再加總，不因類別重複；Central 只在次要資料源中，不算
    (grand,) = totals[(use_key(region), use_key(category))]
    assert values(grand) == (1310.0, 1350.0, 1350.0 / 4, 1310.0 / 1350.0)
    # 各地區的小計
    subtotals = {r[ids["Region"]]: values(r) for r in totals[(use_key(category),)]}
    assert subtotals == {
        "East": (350.0, 400.0, 200.0, 0.875),
        "North": (60.0, None, None, None),
        "South": (100.0, 50.0, 50.0, 2.0),
        "West": (800.0, 900.0, 900.0, 800.0 / 900.0),
    }
    # 非加總型的彙總（中位數等）不能由各連結值的結果再彙總
    median = FieldUse(field=ids["Targets.Target"], agg="median", continuous=True)
    other = ws.model_copy(update={"marks": ws.marks.model_copy(update={"label": [median]})})
    with pytest.raises(QueryError, match="總計、小計一起使用尚不支援"):
        totals_of(ir.model_copy(update={"worksheets": [other]}), folder)


def test_totals_with_secondary_filter(tmp_path: Path, folder: Path) -> None:
    """次要資料源的篩選與總計：總計只合計顯示的標記。"""
    sheet = filtered_sheet(year_filter(["2024"]))
    ir = parse(tmp_path, folder, sheet, secondary_columns=YEAR_TEXT)
    ids = fields(ir)
    ws = ir.worksheets[0].model_copy(update={"totals": ["rows"]})
    ir = ir.model_copy(update={"worksheets": [ws]})
    ((grand,),) = totals_of(ir, folder).values()
    assert (grand[f"sum({ids['Sales']})"], grand[f"sum({ids['Targets.Target']})"]) == (450.0, 150.0)


def test_editor_adds_and_removes_blends(tmp_path: Path, folder: Path) -> None:
    """編輯器：放上次要資料源的量值即混合（以名稱相同的維度連結），移走後不再混合。"""
    from tmig.edit import EditError, replace_worksheet

    sheet = worksheet(
        "Plain", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    ws = ir.worksheets[0]
    assert ws.blends == []
    target = FieldUse(field=ids["Targets.Target"], agg="sum", continuous=True)
    edited = ws.model_copy(update={"marks": ws.marks.model_copy(update={"label": [target]})})
    ir, saved = replace_worksheet(ir, edited)
    assert [b.datasource for b in saved.blends] == [ir.datasources[1].id]
    assert [(link.primary.field, link.secondary.field) for link in saved.blends[0].links] == [
        (ids["Region"], ids["Targets.Region"]),
        (ids["Order Date"], ids["Targets.Order Date"]),
    ]
    rows = {r[ids["Region"]]: r[f"sum({ids['Targets.Target']})"] for r in run(ir, folder)}
    assert rows == {"East": 400.0, "North": None, "South": 50.0, "West": 900.0}

    # 移走次要資料源的量值：不再混合
    ir, saved = replace_worksheet(ir, ws)
    assert saved.blends == []

    # 次要資料源的維度：以 ATTR 彙總（與 Tableau 相同）
    region = FieldUse(field=ids["Targets.Region"])
    area = FieldUse(field=ids["Targets.Area"])
    ir2, saved = replace_worksheet(ir, ws.model_copy(update={"rows": [*ws.rows, area]}))
    assert (saved.rows[-1].field, saved.rows[-1].agg) == (ids["Targets.Area"], "attr")
    assert [b.datasource for b in saved.blends] == [ir.datasources[1].id]
    found = {r[ids["Region"]]: r[f"attr({ids['Targets.Area']})"] for r in run(ir2, folder)}
    assert found == {"East": "East", "North": None, "South": "South", "West": "West"}
    assert EditError is not None

    # 空白的工作表放上其他資料源的欄位：那個資料源成為主要資料源（不混合）
    empty = ws.model_copy(
        update={"rows": [region], "columns": [], "marks": ws.marks.model_copy(update={"label": []})}
    )
    ir, saved = replace_worksheet(ir, empty)
    assert (saved.datasource, saved.blends) == (ir.datasources[1].id, [])


# ---------------------------------------------------------------- 手動設定的連結


def overrides(kind: str, *columns: str) -> str:
    items = "".join(f"<column>[{TARGETS}].[{c}]</column>" for c in columns)
    return f"<join-lod-{kind}-overrides>{items}</join-lod-{kind}-overrides>"


def test_deactivated_link_is_not_used(tmp_path: Path, folder: Path) -> None:
    """取消作用的連結（<join-lod-exclude-overrides>，Order Date）：只以地區連結，目標以地區彙總、在各年重複。"""
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=ref("yr:Order Date:ok"),
        instances=[REGION, YEAR, SALES],
        encodings=(
            f"<text column='{target_ref('sum:Target:qk')}' />"
            f"<tooltip column='{ref('sum:Sales:qk')}' />"
        ),
        table_extra=overrides("exclude", "Order Date"),
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    assert [link.secondary.field for link in ir.worksheets[0].blends[0].links] == [
        ids["Targets.Region"]
    ]
    target = f"sum({ids['Targets.Target']})"
    year = f"year({ids['Order Date']})"
    got = {(r[ids["Region"]], r[year]): r[target] for r in run(ir, folder)}
    assert got == {
        ("East", 2023): 400.0,
        ("North", 2024): None,
        ("South", 2024): 50.0,
        ("West", 2023): 900.0,
        ("West", 2024): 900.0,
    }


def test_link_activated_outside_the_view(tmp_path: Path, folder: Path) -> None:
    """手動設為作用中的連結（<join-lod-include-overrides>）：在檢視中時與自動相同；不在檢視中尚不支援。"""
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=target_ref("sum:Target:qk"),
        table_extra=overrides("include", "Region"),
    )
    ir = parse(tmp_path, folder, sheet)
    assert ir.unsupported == []
    assert len(ir.worksheets[0].blends) == 1

    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=target_ref("sum:Target:qk"),
        table_extra=overrides("include", "Order Date"),
    )
    xml = b.workbook(b.extract_datasource() + targets_datasource(), worksheets=sheet)
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert any("不在檢視中卻設為作用中" in u.reason for u in ir.unsupported)
    assert ir.worksheets == []


# ---------------------------------------------------------------- 跨資料源篩選

SOURCES = (
    f"<datasource caption='Orders' name='{DS}' /><datasource caption='Targets' name='{TARGETS}' />"
)
TARGET_DEPS = (
    f"<datasource-dependencies datasource='{TARGETS}'>{TARGET_REGION}</datasource-dependencies>"
)


def cross_filter(values: list[str], group: str = "", target: bool = True) -> str:
    """Targets 的 Region 的篩選，套用到相關資料源：<target field> 是 Orders 的 Region。"""
    members = "".join(
        f"<groupfilter function='member' level='[none:Region:nk]' member='&quot;{v}&quot;' />"
        for v in values
    )
    attrs = f" filter-group='{group}'" if group else ""
    found = f"<target field='{ref('none:Region:nk')}' />" if target else ""
    return f"""<filter class='categorical' column='{target_ref("none:Region:nk")}'{attrs}>
      <groupfilter function='union' user:ui-enumeration='inclusive'>{members}</groupfilter>
      {found}
    </filter>"""


def orders_sheet(name: str, view_extra: str) -> str:
    return worksheet(
        name,
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        datasources=SOURCES,
        view_extra=TARGET_DEPS + view_extra,
    )


def test_cross_datasource_filter(tmp_path: Path, folder: Path) -> None:
    """其他資料源欄位的篩選（套用到選取的工作表、使用相關資料源）：以 <target field> 對應的主要資料源欄位
    篩選；同一欄位不設限的篩選由它取代。"""
    unrestricted = f"""<filter class='categorical' column='{ref("none:Region:nk")}'>
      <groupfilter function='level-members' level='[none:Region:nk]' user:ui-enumeration='all' />
    </filter>"""
    sheet = orders_sheet("Cross", unrestricted + cross_filter(["East", "West"], group="2"))
    ir = parse(tmp_path, folder, sheet)
    assert ir.unsupported == []
    ids = fields(ir)
    ws = ir.worksheets[0]
    assert ws.blends == []
    (flt,) = ws.filters
    assert isinstance(flt, CategoricalFilter)
    assert (flt.field.field, flt.mode, flt.values, flt.group) == (
        ids["Region"],
        "include",
        ["East", "West"],
        "group:2",
    )
    assert {r[ids["Region"]] for r in run(ir, folder)} == {"East", "West"}

    # 沒有對應到主要資料源的欄位：次要資料源自己的篩選（見 test_secondary_filters）
    sheet = orders_sheet("Secondary", cross_filter(["East"], target=False))
    ir = parse(tmp_path, folder, sheet)
    assert ir.unsupported == []
    ws = ir.worksheets[0]
    assert [blend.datasource for blend in ws.blends] == [ir.datasources[1].id]
    assert [f.field.field for f in ws.filters if isinstance(f, CategoricalFilter)] == [
        fields(ir)["Targets.Region"]
    ]
    # 檢視中沒有次要資料源的欄位：只留下連結欄位的值有對應到通過篩選的資料的標記
    assert {r[ids["Region"]] for r in run(ir, folder)} == {"East"}


def test_related_global_filter_and_control(tmp_path: Path, folder: Path) -> None:
    """全域篩選「套用到所有使用相關資料源的工作表」：存在主要資料源的 <shared-view>，工作表的篩選架列出
    其他資料源的欄位；與該資料源自己的工作表同一篩選群組。dashboard 上的控制項以它篩選。"""
    shared = f"""
    <shared-views>
      <shared-view name='{DS}'>
        <datasources><datasource caption='Orders' name='{DS}' /></datasources>
        <datasource-dependencies datasource='{DS}'>{REGION}</datasource-dependencies>
        {TARGET_DEPS}
        {cross_filter(["East"])}
      </shared-view>
      <shared-view name='{TARGETS}'>
        <datasources><datasource caption='Targets' name='{TARGETS}' /></datasources>
        {TARGET_DEPS}
        <filter class='categorical' column='{target_ref("none:Region:nk")}'>
          <groupfilter function='member' level='[none:Region:nk]' member='&quot;East&quot;' />
          <target field='{target_ref("none:Region:nk")}' />
        </filter>
      </shared-view>
    </shared-views>"""
    slices = f"<slices><column>{target_ref('none:Region:nk')}</column></slices>"
    orders = orders_sheet("Orders", slices)
    targets = worksheet(
        "Targets",
        [],
        rows=target_ref("none:Region:nk"),
        cols=target_ref("sum:Target:qk"),
        datasources=f"<datasource caption='Targets' name='{TARGETS}' />",
        view_extra=(
            f"<datasource-dependencies datasource='{TARGETS}'>{TARGET_REGION}{TARGET}"
            f"</datasource-dependencies>{slices}"
        ),
    )
    board = f"""
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='50000' id='2' name='Orders' w='100000' x='0' y='0' />
          <zone h='50000' id='3' mode='checkdropdown' name='Orders' param='{target_ref("none:Region:nk")}' type-v2='filter' w='100000' x='0' y='50000' />
        </zone>
      </zones>
    </dashboard>"""
    xml = b.workbook(
        b.extract_datasource() + targets_datasource(),
        worksheets=orders + targets,
        dashboards=board,
        extra=shared,
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == []
    assert check_references(ir) == []
    ids = fields(ir)
    by_name = {w.name: w for w in ir.worksheets}
    group = f"shared:{ir.datasources[1].id}:{ids['Targets.Region']}"
    (on_orders,) = by_name["Orders"].filters
    (on_targets,) = by_name["Targets"].filters
    assert isinstance(on_orders, CategoricalFilter) and isinstance(on_targets, CategoricalFilter)
    assert (on_orders.field.field, on_orders.values, on_orders.group) == (
        ids["Region"],
        ["East"],
        group,
    )
    assert (on_targets.field.field, on_targets.values, on_targets.group) == (
        ids["Targets.Region"],
        ["East"],
        group,
    )
    control = next(z for z in iter_zones(ir.dashboards[0].zones) if z.kind == "filter")
    assert control.worksheet == by_name["Orders"].id
    assert control.field == FieldUse(field=ids["Region"])


ATTAINMENT = instance("[Calculation_att]", "User", "[usr:Calculation_att:qk]", "quantitative")
GAP = instance("[Calculation_gap]", "User", "[usr:Calculation_gap:qk]", "quantitative")
BLEND_CALCS = (
    b.calc("Calculation_att", "Attainment", f"SUM([Sales]) / SUM([{TARGETS}].[Target])")
    + b.calc("Calculation_profit", "Margin", "SUM([Profit]) / SUM([Sales])")
    # 引用其他計算欄位（主要資料源的彙總、另一個混合的計算欄位）、參數以外的常數與 IF、ZN
    + b.calc(
        "Calculation_gap",
        "Gap",
        f"IF [Calculation_att] >= 1 THEN 0 ELSE ZN(SUM([{TARGETS}].[Target])) - SUM([Sales]) END"
        " + 0 * [Calculation_profit]",
    )
)


def test_calc_referencing_secondary_fields(tmp_path: Path, folder: Path) -> None:
    """主要資料源的計算欄位引用次要資料源的欄位（實績 ÷ 目標）：各資料源各自彙總，合併後再計算。"""
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=f"({ref('usr:Calculation_att:qk')} + {ref('usr:Calculation_gap:qk')})",
        instances=[REGION, ATTAINMENT, GAP],
    )
    ir = parse(tmp_path, folder, sheet, columns=BLEND_CALCS)
    assert ir.unsupported == []
    assert check_references(ir) == []
    ids = fields(ir)
    ws = ir.worksheets[0]
    # 檢視中沒有直接放次要資料源的欄位，仍是 Data Blending
    assert [blend.datasource for blend in ws.blends] == [ir.datasources[1].id]
    attainment = next(f for f in ir.datasources[0].fields if f.caption == "Attainment")
    assert attainment.calc is not None and attainment.calc.level == "aggregate"
    rows = {r[ids["Region"]]: (r[ids["Attainment"]], r[ids["Gap"]]) for r in run(ir, folder)}
    assert rows == {
        "East": (350.0 / 400.0, 50.0),
        "North": (None, -60.0),  # 沒有目標：比值為 Null，ZN 把目標當成 0
        "South": (2.0, 0.0),
        "West": (800.0 / 900.0, 100.0),
    }
    query = worksheet_sql(ir, ws.id)
    assert [c.key for c in query.columns][:3] == [ids["Region"], ids["Attainment"], ids["Gap"]]
    assert all("blend_part_" not in c.key for c in query.columns)
    rendered = build_spec(
        ir, ws.id, query.columns, execute_query(folder / f"{ws.datasource}.duckdb", query)
    )
    assert rendered.spec is not None, rendered.unsupported


def test_calc_mixing_datasources_inside_an_aggregate_is_unsupported(
    tmp_path: Path, folder: Path
) -> None:
    calcs = b.calc("Calculation_att", "Bad", f"SUM([Sales] * [{TARGETS}].[Target])")
    sheet = blended(
        rows=ref("none:Region:nk"),
        cols=ref("usr:Calculation_att:qk"),
        instances=[REGION, ATTAINMENT],
    )
    ir = parse(tmp_path, folder, sheet, columns=calcs)
    with pytest.raises(QueryError, match="同時用到主要與次要資料源"):
        worksheet_sql(ir, ir.worksheets[0].id)


def test_secondary_dimensions_are_attributes(tmp_path: Path, folder: Path) -> None:
    """次要資料源的維度：與 Tableau 相同以 ATTR 彙總，連結欄位的一個值對到一個值時顯示它，多個值時顯示 *，
    沒有對應的列時是 Null；不增加標記。"""
    year_text = b.calc(
        "Calculation_year", "Year Text", "STR(YEAR([Order Date]))", "string", "dimension"
    )
    area = instance("[Area]", "None", "[none:Area:nk]")
    year = instance("[Calculation_year]", "None", "[none:Calculation_year:nk]")
    date = instance("[Order Date]", "None", "[none:Order Date:ok]", "ordinal")
    sheet = blended(
        rows=(
            f"({ref('none:Region:nk')} / {target_ref('none:Area:nk')} / "
            f"{target_ref('none:Calculation_year:nk')} / {target_ref('none:Order Date:ok')})"
        ),
        cols=ref("sum:Sales:qk"),
        secondary=[area, year, date],
    )
    ir = parse(tmp_path, folder, sheet, secondary_columns=year_text)
    assert ir.unsupported == []
    ids = fields(ir)
    ws = ir.worksheets[0]
    assert [(u.field, u.agg) for u in ws.rows[1:]] == [
        (ids["Targets.Area"], "attr"),
        (ids["Targets.Year Text"], "attr"),
        (ids["Targets.Order Date"], "attr"),
    ]
    keys = [f"attr({ids[name]})" for name in ("Targets.Area", "Targets.Year Text")]
    date_key = f"attr({ids['Targets.Order Date']})"
    rows = {r[ids["Region"]]: (r[keys[0]], r[keys[1]], r[date_key]) for r in run(ir, folder)}
    assert rows == {
        "East": ("East", "*", None),  # 2023 與 2024 兩年的目標：文字顯示 *，日期等其他型別為 Null
        "North": (None, None, None),
        "South": ("South", "2024", dt.date(2024, 1, 1)),
        "West": ("West", "2023", dt.date(2023, 1, 1)),
    }
    query = worksheet_sql(ir, ws.id)
    rendered = build_spec(
        ir, ws.id, query.columns, execute_query(folder / f"{ws.datasource}.duckdb", query)
    )
    assert rendered.spec is not None, rendered.unsupported
    column = next(c for c, key in rendered.spec["usermeta"]["measures"].items() if key == keys[1])
    labels = {v["c0"]: v[f"{column}_label"] for v in rendered.spec["data"]["values"]}
    assert labels["East"] == "*" and labels["West"] == "2023"


def test_editor_calc_referencing_another_datasource(tmp_path: Path, folder: Path) -> None:
    """編輯器：計算欄位以 [資料源的顯示名稱].[欄位] 引用其他資料源的欄位，放到檢視上即混合。"""
    from tmig.edit import EditError, replace_worksheet, upsert_calc

    sheet = worksheet(
        "Plain", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk")
    )
    ir = parse(tmp_path, folder, sheet)
    ids = fields(ir)
    primary = ir.datasources[0].id
    ir, created = upsert_calc(ir, primary, "Attainment", "SUM([Sales]) / SUM([Targets].[Target])")
    assert created.calc is not None and created.calc.level == "aggregate"
    # 次要資料源的欄位要先彙總
    with pytest.raises(EditError, match="公式有誤"):
        upsert_calc(ir, primary, "Bad", "SUM([Sales]) / [Targets].[Target]")
    ws = ir.worksheets[0]
    edited = ws.model_copy(update={"columns": [FieldUse(field=created.id, continuous=True)]})
    ir, saved = replace_worksheet(ir, edited)
    assert [blend.datasource for blend in saved.blends] == [ir.datasources[1].id]
    rows = {r[ids["Region"]]: r[created.id] for r in run(ir, folder)}
    assert rows == {"East": 0.875, "North": None, "South": 2.0, "West": 800.0 / 900.0}
    # 移走後不再混合
    ir, saved = replace_worksheet(ir, ws)
    assert saved.blends == []


YEAR_TEXT = b.calc(
    "Calculation_year", "Year Text", "STR(YEAR([Order Date]))", "string", "dimension"
)


def year_filter(members: list[str], exclude: bool = False) -> str:
    """次要資料源（Targets）的 Year Text 的篩選；%null% 是 Null。"""
    level = "[none:Calculation_year:nk]"
    items = "".join(
        f"<groupfilter function='member' level='{level}' member='{m}' />"
        for m in (m if m == "%null%" else f"&quot;{m}&quot;" for m in members)
    )
    column = target_ref("none:Calculation_year:nk")
    if exclude:
        inner = (
            "<groupfilter function='except' user:ui-enumeration='exclusive'>"
            f"<groupfilter function='level-members' level='{level}' />{items}</groupfilter>"
        )
    else:
        inner = (
            f"<groupfilter function='union' user:ui-enumeration='inclusive'>{items}</groupfilter>"
        )
    return f"<filter class='categorical' column='{column}'>{inner}</filter>"


def filtered_sheet(view_extra: str) -> str:
    year = instance("[Calculation_year]", "None", "[none:Calculation_year:nk]")
    deps = (
        f"<datasource-dependencies datasource='{TARGETS}'>{TARGET}{year}</datasource-dependencies>"
    )
    return worksheet(
        "Filtered",
        [REGION, SALES],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{target_ref('sum:Target:qk')}' />",
        datasources=SOURCES,
        view_extra=deps + view_extra,
    )


def test_secondary_filters(tmp_path: Path, folder: Path) -> None:
    """次要資料源的維度篩選：篩選次要資料源的資料（目標只合計通過篩選的列）；主要資料源中連結欄位的值沒有
    對應到通過篩選的資料時，那些標記不顯示。篩選包含 Null 時，次要資料源完全沒有對應資料的標記仍顯示。
    （Tableau 的說明文件沒有寫明，依公開資料推定。）"""
    from tmig.planner.state import FilterValue, QueryState

    def result(view_extra: str, state: QueryState | None = None) -> dict[object, tuple]:
        ir = parse(tmp_path, folder, filtered_sheet(view_extra), secondary_columns=YEAR_TEXT)
        assert ir.unsupported == [] and check_references(ir) == []
        ids = fields(ir)
        ws = ir.worksheets[0]
        query = worksheet_sql(ir, ws.id, state)
        found = execute_query(folder / f"{ws.datasource}.duckdb", query).records()
        target, sales = f"sum({ids['Targets.Target']})", f"sum({ids['Sales']})"
        return {r[ids["Region"]]: (r[sales], r[target]) for r in found}

    # 2024 年的目標：East 只合計 2024 年的 100；West 只有 2023 年的目標、North 沒有目標，不顯示
    assert result(year_filter(["2024"])) == {"East": (350.0, 100.0), "South": (100.0, 50.0)}
    # 包含 Null：次要資料源完全沒有對應資料的 North 仍顯示；West 的資料都被篩選掉，不顯示
    with_null = {"East": (350.0, 100.0), "North": (60.0, None), "South": (100.0, 50.0)}
    assert result(year_filter(["2024", "%null%"])) == with_null
    # 排除 2023（沒有排除 Null）
    assert result(year_filter(["2023"], exclude=True)) == with_null
    # 篩選控制項改成 2023
    ir = parse(tmp_path, folder, filtered_sheet(year_filter(["2024"])), secondary_columns=YEAR_TEXT)
    key = fields(ir)["Targets.Year Text"]
    state = QueryState(filters={key: FilterValue(mode="include", values=["2023"])})
    assert result(year_filter(["2024"]), state) == {"East": (350.0, 300.0), "West": (800.0, 900.0)}
    query = worksheet_sql(ir, ir.worksheets[0].id, state)
    assert query.state.applied == [key] and query.state.skipped == []
    # 全部（不設限）：與沒有篩選相同
    everything = QueryState(filters={key: FilterValue(mode="all")})
    assert result(year_filter(["2024"]), everything) == {
        "East": (350.0, 400.0),
        "North": (60.0, None),
        "South": (100.0, 50.0),
        "West": (800.0, 900.0),
    }


def test_secondary_filter_control_on_dashboard(tmp_path: Path, folder: Path) -> None:
    """dashboard 上次要資料源的篩選控制項：控制工作表上次要資料源的篩選（欄位是次要資料源的欄位）。"""
    sheet = filtered_sheet(year_filter(["2024"]))
    column = target_ref("none:Calculation_year:nk")
    board = f"""
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='50000' id='2' name='Filtered' w='100000' x='0' y='0' />
          <zone h='50000' id='3' mode='checkdropdown' name='Filtered' param='{column}' type-v2='filter' w='100000' x='0' y='50000' />
        </zone>
      </zones>
    </dashboard>"""
    xml = b.workbook(
        b.extract_datasource() + targets_datasource(YEAR_TEXT), worksheets=sheet, dashboards=board
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == []
    assert check_references(ir) == []
    ids = fields(ir)
    control = next(z for z in iter_zones(ir.dashboards[0].zones) if z.kind == "filter")
    assert control.worksheet == ir.worksheets[0].id
    assert control.field == FieldUse(field=ids["Targets.Year Text"])
    # 控制項的可選值來自次要資料源
    from tmig.data.executor import execute
    from tmig.sqlgen.query import domain_sql

    parse(tmp_path, folder, sheet, secondary_columns=YEAR_TEXT)  # 建立資料檔
    secondary = ir.datasources[1].id
    assert control.field is not None
    sql = domain_sql(ir, secondary, control.field, "values")
    assert [r[0] for r in execute(folder / f"{secondary}.duckdb", sql).rows] == ["2023", "2024"]
