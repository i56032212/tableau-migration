"""第三階段 P1：使用者函式與列層級權限。

以 test_query 的 9 列資料（East 350、West 800、South 100、North 60）驗證：

- 資料源篩選以使用者函式限制可見的列，不同登入者看到不同的列；沒有身分時查詢失敗
- 工作表上的權限篩選不接受網址參數；報表為它放了篩選控制項時才可以調整（與 Tableau 相同）
- 權限計算用到參數時，改參數與 Tableau 相同會改變結果
- Tableau 的使用者篩選（伺服器 → 建立使用者篩選，identity-set 集合）
- ISMEMBEROF 的規則（不分大小寫、All Users、網址編碼）、使用者屬性
- 轉譯到 PostgreSQL 與 Oracle 後在來源資料庫執行（兩段式）
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from lxml import etree

from tmig.data.executor import execute
from tmig.data.live import execute_live
from tmig.data.sources import Executor
from tmig.ir import IR, FieldUse
from tmig.ir.models import LiveConnection, TableRelation
from tmig.parser.groups import GroupError, identity_set_formula
from tmig.parser.workbook import parse_workbook
from tmig.planner.identity import Identity, protected_fields, user_dependent_fields
from tmig.planner.state import FilterValue, QueryState
from tmig.sqlgen.query import QueryError, domain_sql, worksheet_sql
from tmig.verify.cases import CasesFile

from ..engines import (
    OracleEmulator,
    as_live,
    pg_connect,
    pg_load,
    postgres_cluster,
    postgres_executor,
)
from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet
from .test_query import CATEGORY, REGION, ROWS, SALES

OWNER = calc(
    "Owner",
    "Owner",
    'CASE [Region] WHEN "East" THEN "alice" WHEN "West" THEN "bob" ELSE "carol" END',
    "string",
    "dimension",
)
MINE = calc(
    "Mine", "Mine", '[Owner] = USERNAME() OR ISMEMBEROF("Managers")', "boolean", "dimension"
)
VISIBLE = calc(
    "Visible",
    "Visible",
    "[Region] = [Parameters].[Parameter 2] OR [Owner] = USERNAME()",
    "boolean",
    "dimension",
)
MINE_INSTANCE = instance("[Mine]", "None", "[none:Mine:nk]")


def ds_filter(field: str) -> str:
    return (
        f"<filter class='categorical' column='[{field}]'>"
        f"<groupfilter function='member' level='[{field}]' member='true' /></filter>"
    )


MINE_FILTER = (
    f"<filter class='categorical' column='{ref('none:Mine:nk')}'>"
    "<groupfilter function='member' level='[none:Mine:nk]' member='true' /></filter>"
)

USER_FILTER = """
      <group caption='User Filter 1' name='[User Filter 1]' name-style='unqualified' user:ui-builder='identity-set'>
        <groupfilter function='intersection'>
          <groupfilter function='level-members' level='[Region]' />
          <groupfilter function='union'>
            <groupfilter expression='false' function='filter'>
              <groupfilter function='level-members' level='[Region]' />
            </groupfilter>
            <groupfilter expression='ISMEMBEROF(&apos;East Team&apos;)' function='filter'>
              <groupfilter function='member' level='[Region]' member='&quot;East&quot;' />
            </groupfilter>
            <groupfilter expression='ISUSERNAME(&apos;bob&apos;)' function='filter'>
              <groupfilter function='union'>
                <groupfilter function='member' level='[Region]' member='&quot;West&quot;' />
                <groupfilter function='member' level='[Region]' member='&quot;South&quot;' />
              </groupfilter>
            </groupfilter>
            <groupfilter expression='ISMEMBEROF(&apos;National&apos;)' function='filter'>
              <groupfilter function='level-members' level='[Region]' />
            </groupfilter>
          </groupfilter>
        </groupfilter>
      </group>"""
USER_FILTER_ON_SHEET = f"<filter class='categorical' column='[{b.DS}].[User Filter 1]' />"

ALICE = Identity(username="alice")
BOB = Identity(username="bob", full_name="Bob Chen")
CAROL = Identity(username="carol")
MANAGER = Identity(username="dave", groups=("managers",))


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "orders.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE SCHEMA "Extract"')
        con.execute(
            'CREATE TABLE "Extract"."Extract" ("Region" VARCHAR, "Category" VARCHAR, "Order ID" VARCHAR, '
            '"Order Date" DATE, "Sales" DOUBLE, "Profit" DOUBLE, "Quantity" BIGINT)'
        )
        con.executemany('INSERT INTO "Extract"."Extract" VALUES (?, ?, ?, ?, ?, ?, ?)', ROWS)
    return path


def build(
    tmp_path: Path,
    columns: str = "",
    ds_extra: str = "",
    view_extra: str = "",
    dashboards: str = "",
) -> IR:
    sheet = worksheet(
        "S",
        [REGION, CATEGORY, SALES, MINE_INSTANCE],
        rows=ref("none:Region:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=view_extra,
    )
    xml = b.workbook(
        b.extract_datasource(columns=columns, extra=ds_extra),
        worksheets=sheet,
        dashboards=dashboards,
    )
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    return ir


def by_region(
    ir: IR, db: Path, user: Identity | None, state: QueryState | None = None
) -> dict[str, float]:
    query = worksheet_sql(ir, ir.worksheets[0].id, state, user)
    rows = execute(db, query.sql).records()
    return {r["f_region"]: r["sum(f_sales)"] for r in rows}


ALL = {"East": 350.0, "West": 800.0, "South": 100.0, "North": 60.0}


# ---------------------------------------------------------------- 資料源篩選


def test_datasource_filter_by_user(tmp_path: Path, db: Path) -> None:
    ir = build(tmp_path, OWNER + MINE, ds_filter("Mine"))
    assert by_region(ir, db, ALICE) == {"East": 350.0}
    assert by_region(ir, db, BOB) == {"West": 800.0}
    assert by_region(ir, db, CAROL) == {"South": 100.0, "North": 60.0}
    assert by_region(ir, db, MANAGER) == ALL  # 群組不分大小寫
    assert by_region(ir, db, Identity(username="eve")) == {}


def test_user_literals_in_sql(tmp_path: Path) -> None:
    ir = build(tmp_path, OWNER + MINE, ds_filter("Mine"))
    sql = worksheet_sql(ir, ir.worksheets[0].id, user=Identity(username="o'neil")).sql
    assert "'o''neil'" in sql  # 使用者名稱是字串常值（跳脫單引號），不是參數或欄位


def test_missing_identity_fails(tmp_path: Path) -> None:
    ir = build(tmp_path, OWNER + MINE, ds_filter("Mine"))
    with pytest.raises(QueryError, match="需要登入者的身分") as caught:
        worksheet_sql(ir, ir.worksheets[0].id)
    assert not caught.value.unsupported


def test_domain_values_respect_rls(tmp_path: Path, db: Path) -> None:
    """篩選控制項的可選值也套用資料源篩選（看不到的成員不會出現在清單中）。"""
    ir = build(tmp_path, OWNER + MINE, ds_filter("Mine"))
    ds = ir.datasources[0].id
    region = next(f for f in ir.datasources[0].fields if f.caption == "Region")
    sql = domain_sql(ir, ds, FieldUse(field=region.id), "values", CAROL)
    assert sorted(r[0] for r in execute(db, sql).rows) == ["North", "South"]
    with pytest.raises(QueryError, match="需要登入者的身分"):
        domain_sql(ir, ds, FieldUse(field=region.id), "values")


# ---------------------------------------------------------------- 工作表上的權限篩選


def test_url_params_cannot_change_rls_filter(tmp_path: Path, db: Path) -> None:
    ir = build(tmp_path, OWNER + MINE, view_extra=MINE_FILTER)
    ds = ir.datasources[0]
    assert user_dependent_fields(ds) == {"f_mine"}
    assert protected_fields(ir, ds) == {"f_mine"}
    state = QueryState(filters={"f_mine": FilterValue(mode="all")})
    query = worksheet_sql(ir, ir.worksheets[0].id, state, ALICE)
    assert query.state.skipped == ["f_mine"]
    assert by_region(ir, db, ALICE, state) == {"East": 350.0}
    # 改成看別人的列也一樣不接受
    state = QueryState(filters={"f_mine": FilterValue(mode="include", values=[False])})
    assert by_region(ir, db, ALICE, state) == {"East": 350.0}
    # 其他欄位的篩選照常套用，只會再縮小範圍
    state = QueryState(filters={"f_region": FilterValue(mode="include", values=["East", "West"])})
    assert by_region(ir, db, CAROL, state) == {}


FILTER_CONTROL = f"""
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='50000' id='2' name='S' w='100000' x='0' y='0' />
          <zone h='50000' id='3' mode='checkdropdown' name='S' param='{ref("none:Mine:nk")}' type-v2='filter' w='100000' x='0' y='50000' />
        </zone>
      </zones>
    </dashboard>"""


def test_filter_control_makes_user_field_adjustable(tmp_path: Path, db: Path) -> None:
    """報表為這個欄位放了篩選控制項（例如「只看我的」開關）：與 Tableau 相同，使用者可以調整。"""
    ir = build(tmp_path, OWNER + MINE, view_extra=MINE_FILTER, dashboards=FILTER_CONTROL)
    assert protected_fields(ir, ir.datasources[0]) == set()
    assert by_region(ir, db, ALICE) == {"East": 350.0}
    state = QueryState(filters={"f_mine": FilterValue(mode="all")})
    assert by_region(ir, db, ALICE, state) == ALL


def test_parameters_in_rls_behave_like_tableau(tmp_path: Path, db: Path) -> None:
    """權限計算中的參數：與 Tableau 相同，使用者改參數會改變結果（功能盤點會提醒）。"""
    ir = build(tmp_path, OWNER + VISIBLE, ds_filter("Visible"))
    param = next(p for p in ir.parameters if p.caption == "Region Pick")
    assert by_region(ir, db, CAROL) == {"East": 350.0, "South": 100.0, "North": 60.0}
    state = QueryState(parameters={param.id: "West"})
    assert by_region(ir, db, CAROL, state) == {"West": 800.0, "South": 100.0, "North": 60.0}


# ---------------------------------------------------------------- Tableau 的使用者篩選


def test_user_filter_set(tmp_path: Path, db: Path) -> None:
    ir = build(tmp_path, ds_extra=USER_FILTER, view_extra=USER_FILTER_ON_SHEET)
    ds = ir.datasources[0]
    user_filter = next(f for f in ds.fields if f.caption == "User Filter 1")
    assert user_filter.datatype == "boolean" and user_filter.calc is not None
    assert protected_fields(ir, ds) == {user_filter.id}
    sheet_filter = ir.worksheets[0].filters[0]
    assert sheet_filter.kind == "categorical" and sheet_filter.values == [True]

    assert by_region(ir, db, Identity(username="amy", groups=("east team",))) == {"East": 350.0}
    assert by_region(ir, db, BOB) == {"West": 800.0, "South": 100.0}
    assert by_region(ir, db, Identity(username="zoe", groups=("National",))) == ALL
    assert by_region(ir, db, Identity(username="nobody")) == {}
    state = QueryState(filters={user_filter.id: FilterValue(mode="all")})
    assert by_region(ir, db, BOB, state) == {"West": 800.0, "South": 100.0}


def test_user_filter_as_datasource_filter(tmp_path: Path, db: Path) -> None:
    ds_level = "<filter class='categorical' column='[User Filter 1]' />"
    ir = build(tmp_path, ds_extra=USER_FILTER + ds_level)
    assert ir.datasources[0].filters[0].stage == "datasource"
    assert by_region(ir, db, BOB) == {"West": 800.0, "South": 100.0}


def _group(xml: str) -> etree._Element:
    return etree.fromstring(
        f"<group xmlns:user='http://www.tableausoftware.com/xml/user' name='[G]'>{xml}</group>"
    )


def test_identity_set_formula_shapes() -> None:
    null_member = _group(
        "<groupfilter expression='ISMEMBEROF(\"a\")' function='filter'><groupfilter function='union'>"
        "<groupfilter function='member' level='[Region]' member='%null%' />"
        "<groupfilter function='member' level='[Region]' member='&quot;East&quot;' />"
        "<groupfilter function='member' level='[Region]' member='&quot;Say &quot;&quot;hi&quot;&quot;&quot;' />"
        "</groupfilter></groupfilter>"
    )
    assert identity_set_formula(null_member, "string") == (
        'ISMEMBEROF("All Users") AND (((ISMEMBEROF("a")) AND (ISNULL([Region]) OR '
        '(NOT ISNULL([Region]) AND [Region] IN ("East", "Say ""hi""")))))'
    )
    numbers = _group(
        "<groupfilter function='except'><groupfilter function='level-members' level='[Year]' />"
        "<groupfilter expression='ISUSERNAME(\"bob\")' function='filter'>"
        "<groupfilter function='member' level='[Year]' member='2024' /></groupfilter></groupfilter>"
    )
    assert identity_set_formula(numbers, "integer") == (
        'ISMEMBEROF("All Users") AND '
        '((TRUE AND NOT ((ISUSERNAME("bob")) AND (NOT ISNULL([Year]) AND [Year] = 2024))))'
    )
    nobody = _group(
        "<groupfilter function='intersection'><groupfilter function='level-members' level='[R]' />"
        "<groupfilter function='union'><groupfilter expression='false' function='filter'>"
        "<groupfilter function='level-members' level='[R]' /></groupfilter></groupfilter></groupfilter>"
    )
    # 沒有對應任何使用者：仍然用到使用者函式，屬於列層級權限
    assert identity_set_formula(nobody, "string") == (
        'ISMEMBEROF("All Users") AND ((TRUE AND ((false) AND TRUE)))'
    )
    condition = _group(
        "<groupfilter expression='SUM([Sales]) &gt; 100' function='filter'>"
        "<groupfilter function='level-members' level='[Region]' /></groupfilter>"
    )
    with pytest.raises(GroupError, match="條件集合"):
        identity_set_formula(condition, "string")
    two_levels = _group(
        "<groupfilter function='union'><groupfilter function='member' level='[A]' member='1' />"
        "<groupfilter function='member' level='[B]' member='1' /></groupfilter>"
    )
    with pytest.raises(GroupError, match="一個欄位"):
        identity_set_formula(two_levels, "integer")


def test_unknown_user_filter_shape_is_unsupported(tmp_path: Path) -> None:
    group = USER_FILTER.replace("function='intersection'", "function='crossjoin'")
    xml = b.workbook(b.extract_datasource(extra=group))
    ir = parse_workbook(b.write_twb(tmp_path, xml))
    assert [u.reason for u in ir.unsupported] == [
        "使用者篩選「User Filter 1」：集合的成員定義（function=crossjoin）不支援"
    ]


# ---------------------------------------------------------------- 函式的規則


def test_identity_rules() -> None:
    user = Identity(username="Alice", full_name="Alice Wang", groups=("Sales Team", "USERS+"))
    assert user.member_of("sales team")
    assert user.member_of("Sales+Team")  # 引數以網址編碼解讀：+ 是空白
    assert user.member_of("USERS%2B")
    assert not user.member_of("USERS+")
    assert user.member_of("All Users")  # 每個登入者都屬於 All Users
    assert user.is_username("alice") and not user.is_username("alice2")
    assert user.is_full_name("alice wang")
    assert not Identity(username="x").is_full_name("")


FUNCTIONS = calc(
    "Who",
    "Who",
    'USERNAME() + "|" + IFNULL(FULLNAME(), "-") + "|" + IFNULL(USERDOMAIN(), "-")',
    "string",
    "dimension",
)


@pytest.mark.parametrize(
    ("formula", "user", "expected"),
    [
        ('ISUSERNAME("BOB")', BOB, True),
        ('ISFULLNAME("bob chen")', BOB, True),
        ('ISFULLNAME("bob chen")', ALICE, False),
        ('ISMEMBEROF("managers")', MANAGER, True),
        ('USERNAME() = "bob"', BOB, True),
        ('FULLNAME() = "Bob Chen"', BOB, True),
        ("ISNULL(FULLNAME())", ALICE, True),
        ("ISNULL(USERDOMAIN())", Identity(username="u", domain="corp"), False),
        (
            'USERATTRIBUTE("region") = "East"',
            Identity(username="u", attributes={"region": ("East", "West")}),
            True,
        ),
        ('ISNULL(USERATTRIBUTE("region"))', ALICE, True),
        (
            'USERATTRIBUTEINCLUDES("region", "West")',
            Identity(username="u", attributes={"region": ("East", "West")}),
            True,
        ),
        (
            'USERATTRIBUTEINCLUDES("region", "west")',
            Identity(username="u", attributes={"region": ("East", "West")}),
            False,
        ),
    ],
)
def test_function_values(
    tmp_path: Path, db: Path, formula: str, user: Identity, expected: bool
) -> None:
    columns = calc("Test", "Test", formula, "boolean", "dimension")
    ir = build(tmp_path, columns, ds_filter("Test"))
    assert by_region(ir, db, user) == (ALL if expected else {})


def test_user_attribute_includes_field(tmp_path: Path, db: Path) -> None:
    columns = calc(
        "Allowed", "Allowed", 'USERATTRIBUTEINCLUDES("region", [Region])', "boolean", "dimension"
    )
    ir = build(tmp_path, columns, ds_filter("Allowed"))
    user = Identity(username="u", attributes={"region": ("East", "North")})
    assert by_region(ir, db, user) == {"East": 350.0, "North": 60.0}
    assert by_region(ir, db, ALICE) == {}


def test_group_name_must_be_literal(tmp_path: Path) -> None:
    columns = calc("Bad", "Bad", "ISMEMBEROF([Region])", "boolean", "dimension")
    ir = build(tmp_path, columns, ds_filter("Bad"))
    with pytest.raises(QueryError, match="必須是字串常值"):
        worksheet_sql(ir, ir.worksheets[0].id, user=ALICE)


def test_cases_file_user() -> None:
    cases = CasesFile.model_validate(
        {
            "workbook": "w",
            "user": {"username": "alice", "groups": ["Sales"]},
            "sheets": [],
            "cases": [],
        }
    )
    assert cases.user == Identity(username="alice", groups=("Sales",))


# ---------------------------------------------------------------- 來源資料庫（兩段式）

TABLE = pa.table(
    {
        "Region": [r[0] for r in ROWS],
        "Category": [r[1] for r in ROWS],
        "Order ID": [r[2] for r in ROWS],
        "Order Date": [r[3] for r in ROWS],
        "Sales": [r[4] for r in ROWS],
        "Profit": [r[5] for r in ROWS],
        "Quantity": [r[6] for r in ROWS],
    }
)


@pytest.fixture(scope="module", params=["postgres", "oracle"])
def source(request: pytest.FixtureRequest) -> Iterator[tuple[str, Executor]]:
    if request.param == "oracle":
        con = duckdb.connect()
        con.execute("CREATE SCHEMA SALES")
        con.register("rows", TABLE)
        con.execute('CREATE TABLE SALES."orders" AS SELECT * FROM "rows"')
        try:
            yield "oracle", OracleEmulator(con)
        finally:
            con.close()
        return
    with postgres_cluster() as dsn:
        con = pg_connect(dsn)
        pg_load(con, "orders", TABLE)
        try:
            yield "postgres", postgres_executor(con)
        finally:
            con.close()


def _live(ir: IR, dialect: str) -> IR:
    """萃取改成 Live 連線，連到來源資料庫的 orders 資料表（Oracle 在 SALES schema）。"""
    schema = ["SALES"] if dialect == "oracle" else []
    live = as_live(ir, dialect, schema)
    ds = live.datasources[0]
    connection = ds.connection
    assert isinstance(connection, LiveConnection) and isinstance(connection.relation, TableRelation)
    relation = connection.relation.model_copy(update={"table": [*schema, "orders"]})
    connection = connection.model_copy(update={"relation": relation})
    return live.model_copy(
        update={"datasources": [ds.model_copy(update={"connection": connection})]}
    )


def _remote(ir: IR, source: tuple[str, Executor], user: Identity) -> dict[str, Any]:
    dialect, executor = source
    query = worksheet_sql(_live(ir, dialect), ir.worksheets[0].id, None, user)
    rows = execute_live(query, executor).records()
    return {r["f_region"]: pytest.approx(r["sum(f_sales)"]) for r in rows}


def test_rls_pushdown(tmp_path: Path, source: tuple[str, Executor]) -> None:
    ir = build(tmp_path, OWNER + MINE, ds_filter("Mine"))
    assert _remote(ir, source, ALICE) == {"East": 350.0}
    assert _remote(ir, source, MANAGER) == ALL
    sets = build(tmp_path, ds_extra=USER_FILTER, view_extra=USER_FILTER_ON_SHEET)
    assert _remote(sets, source, BOB) == {"West": 800.0, "South": 100.0}
    assert _remote(sets, source, Identity(username="nobody")) == {}
