"""別名：成員別名、Measure Names 的別名與參數的「顯示為」。

與 Tableau 相同，別名只改變顯示（標題、標籤、圖例、工具提示、控制項）；篩選、排序與計算仍用原始值。
XML 的寫法取自公開報表（股市分析的 Company、HR 人資的 Education Level、geocoding 的布林計算欄位、
各作品集參數清單的 <aliases> 與 <member alias=…>）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data.executor import execute
from tmig.ir import IR, FieldUse, MemberAlias, TextRun
from tmig.parser.workbook import parse_workbook
from tmig.render.aliases import member_aliases, parameter_label
from tmig.render.vegalite import build_spec, worksheet_title
from tmig.sqlgen.query import worksheet_sql
from tmig.verify.compare import compare
from tmig.verify.mapping import SheetMapping

from ..parser import builder as b
from ..parser.builder import DS, instance, ref, worksheet
from ..sqlgen.test_query import ROWS
from .test_vegalite import validator

REGION = instance("[Region]", "None", "[none:Region:nk]")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
PROFIT = instance("[Profit]", "Sum", "[sum:Profit:qk]", "quantitative")
ORDERS = instance("[Order ID]", "CountD", "[ctd:Order ID:qk]", "quantitative")
REGION_ALIASES = (
    "<column datatype='string' name='[Region]' role='dimension' type='nominal'>"
    "<aliases><alias key='&quot;East&quot;' value='東區' /><alias key='&quot;West&quot;' value='西區' />"
    "<alias key='%null%' value='（未分區）' /></aliases></column>"
)
PARAMS = """
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      <column alias='前 5 名' caption='Top N' datatype='integer' name='[Parameter 1]' param-domain-type='list' role='measure' type='quantitative' value='5'>
        <calculation class='tableau' formula='5' />
        <aliases>
          <alias key='3' value='前 3 名' />
          <alias key='5' value='前 5 名' />
        </aliases>
        <members>
          <member alias='前 3 名' value='3' />
          <member alias='前 5 名' value='5' />
        </members>
      </column>
      <column caption='Size' datatype='real' name='[Parameter 2]' param-domain-type='list' role='measure' type='quantitative' value='2.'>
        <calculation class='tableau' formula='2.' />
        <members>
          <member alias='Small' value='1.' />
          <member alias='Large' value='2.' />
        </members>
      </column>
    </datasource>"""


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


def parse(tmp_path: Path, sheets: str = "", columns: str = REGION_ALIASES) -> IR:
    xml = b.workbook(
        PARAMS + b.extract_datasource(columns=columns), worksheets=sheets, params=False
    )
    return parse_workbook(b.write_twb(tmp_path, xml))


def render(ir: IR, db: Path) -> Any:
    ws = ir.worksheets[0]
    query = worksheet_sql(ir, ws.id)
    rendered = build_spec(ir, ws.id, query.columns, execute(db, query.sql))
    assert rendered.spec is not None, rendered.unsupported
    assert [e.message for e in validator().iter_errors(rendered.spec)] == []
    return rendered


def bars() -> str:
    return worksheet("Sales", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))


# ---------------------------------------------------------------- 解析


def test_member_aliases_are_parsed(tmp_path: Path) -> None:
    columns = REGION_ALIASES + (
        "<column datatype='integer' name='[Quantity]' role='measure' type='quantitative'>"
        "<aliases><alias key='1' value='一件' /></aliases></column>"
        + b.calc(
            "Calculation_1", "Profitable?", "SUM([Profit]) > 0", "boolean", "dimension"
        ).replace(
            "</column>",
            "<aliases><alias key='false' value='Unprofitable' />"
            "<alias key='true' value='Profitable' /></aliases></column>",
        )
    )
    ir = parse(tmp_path, columns=columns)
    assert ir.unsupported == []
    fields = {f.caption: f for f in ir.datasources[0].fields}
    assert fields["Region"].aliases == [
        MemberAlias(value="East", label="東區"),
        MemberAlias(value="West", label="西區"),
        MemberAlias(value=None, label="（未分區）"),
    ]
    assert fields["Quantity"].aliases == [MemberAlias(value=1, label="一件")]
    assert fields["Profitable?"].aliases == [
        MemberAlias(value=False, label="Unprofitable"),
        MemberAlias(value=True, label="Profitable"),
    ]


def test_unreadable_alias_is_flagged(tmp_path: Path) -> None:
    columns = (
        "<column datatype='integer' name='[Quantity]' role='measure' type='quantitative'>"
        "<aliases><alias key='&quot;one&quot;x' value='一件' /></aliases></column>"
    )
    ir = parse(tmp_path, columns=columns)
    (reason,) = [u.reason for u in ir.unsupported]
    assert reason.startswith('欄位「Quantity」的別名「"one"x」無法解析')


def test_parameter_aliases(tmp_path: Path) -> None:
    """參數的「顯示為」：<aliases>，或清單中各 <member alias=…>。"""
    ir = parse(tmp_path)
    top, size = ir.parameters
    assert top.aliases == [
        MemberAlias(value=3, label="前 3 名"),
        MemberAlias(value=5, label="前 5 名"),
    ]
    assert size.aliases == [
        MemberAlias(value=1.0, label="Small"),
        MemberAlias(value=2.0, label="Large"),
    ]
    assert parameter_label(top, 5) == "前 5 名"
    assert parameter_label(top, 7) is None


def test_measure_names_aliases(tmp_path: Path, db: Path) -> None:
    """Measure Names 的別名在資料源中（以欄位實例為鍵），只改變 Measure Names 的顯示。"""
    sheet = worksheet(
        "KPIs",
        [REGION, SALES, ORDERS],
        rows=ref("none:Region:nk"),
        cols=f"[{DS}].[:Measure Names]",
        mark="Text",
        encodings=f"<text column='[{DS}].[Multiple Values]' />",
        view_extra=(
            f"<filter class='categorical' column='[{DS}].[:Measure Names]'>"
            "<groupfilter function='union' user:op='manual'>"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('sum:Sales:qk')}&quot;' />"
            f"<groupfilter function='member' level='[:Measure Names]' member='&quot;{ref('ctd:Order ID:qk')}&quot;' />"
            "</groupfilter></filter>"
        ),
    )
    columns = (
        "<column datatype='string' name='[:Measure Names]' role='dimension' type='nominal'>"
        f"<aliases><alias key='&quot;{ref('ctd:Order ID:qk')}&quot;' value='訂單數' /></aliases>"
        "</column>"
    )
    ir = parse(tmp_path, sheet, columns=columns)
    assert ir.unsupported == [], [u.reason for u in ir.unsupported]
    ws = ir.worksheets[0]
    (alias,) = ws.measure_aliases
    assert alias.label == "訂單數" and alias.use.agg == "countd"
    values = render(ir, db).spec["data"]["values"]
    east = [v["__mn"] for v in values if v["c0"] == "East"]
    assert east == ["Sales", "訂單數"]


# ---------------------------------------------------------------- 呈現


def test_aliases_change_only_the_display(tmp_path: Path, db: Path) -> None:
    ir = parse(tmp_path, bars())
    rendered = render(ir, db)
    values = rendered.spec["data"]["values"]
    labels = {v["c0"]: v["c0_label"] for v in values}
    # 原始值留給篩選與動作；顯示文字（座標軸、工具提示）是別名，沒有別名的成員照原樣
    assert labels == {"East": "東區", "West": "西區", "South": "South", "North": "North"}
    assert rendered.spec["encoding"]["y"]["field"] == "c0_label"
    assert {"field": "c0_label", "title": "Region"} in rendered.spec["encoding"]["tooltip"]
    # 排序仍依原始值（查詢的順序）
    assert [v["c0"] for v in values] == ["East", "North", "South", "West"]


def test_null_alias_shows_in_custom_label(tmp_path: Path, db: Path) -> None:
    """Null 的欄位在自訂標籤中不顯示，但 Null 設有別名時顯示別名。"""
    calc = b.calc(
        "Calculation_2",
        "Zone",
        'IF [Region] = "North" THEN NULL ELSE [Region] END',
        "string",
        "dimension",
    ).replace("</column>", "<aliases><alias key='%null%' value='（未分區）' /></aliases></column>")
    zone = instance("[Calculation_2]", "None", "[none:Calculation_2:nk]")
    label = (
        "<customized-label><formatted-text>"
        f"<run>&lt;{ref('none:Calculation_2:nk')}&gt;: &lt;{ref('sum:Sales:qk')}&gt;</run>"
        "</formatted-text></customized-label>"
    )
    sheet = worksheet(
        "Zones",
        [zone, SALES],
        rows=ref("none:Calculation_2:nk"),
        cols=ref("sum:Sales:qk"),
        encodings=f"<text column='{ref('sum:Sales:qk')}' />",
        pane_extra=label,
    )
    ir = parse(tmp_path, sheet, columns=calc)
    values = render(ir, db).spec["data"]["values"]
    texts = {v["c0_label"]: v["__text0"] for v in values}
    assert texts["（未分區）"] == "（未分區）: 60"
    assert texts["East"] == "East: 350"


def test_parameter_alias_in_title(tmp_path: Path) -> None:
    titled = bars().replace(
        "<table>",
        "<layout-options><title><formatted-text>"
        "<run>Sales（&lt;[Parameters].[Parameter 1]&gt;）</run>"
        "</formatted-text></title></layout-options><table>",
    )
    ir = parse(tmp_path, titled)
    ws = ir.worksheets[0]
    top = ir.parameters[0]
    assert ws.title == [TextRun(text="Sales（"), TextRun(parameter=top.id), TextRun(text="）")]
    assert worksheet_title(ir, ws) == "Sales（前 5 名）"
    assert worksheet_title(ir, ws, {top.id: 3}) == "Sales（前 3 名）"
    # 沒有別名的值照原樣顯示
    assert worksheet_title(ir, ws, {top.id: 7}) == "Sales（7）"


def test_member_aliases_only_for_discrete_members(tmp_path: Path) -> None:
    ir = parse(tmp_path)
    region = next(f for f in ir.datasources[0].fields if f.caption == "Region")
    assert member_aliases(region, FieldUse(field=region.id)) == {
        "East": "東區",
        "West": "西區",
        None: "（未分區）",
    }
    for use in (
        FieldUse(field=region.id, agg="count"),
        FieldUse(field=region.id, continuous=True),
        FieldUse(field=region.id, table_calc="tc1"),
    ):
        assert member_aliases(region, use) == {}


# ---------------------------------------------------------------- 計算比對


def test_comparison_accepts_aliases_or_original_values(tmp_path: Path, db: Path) -> None:
    """Tableau 匯出的資料中維度可能是別名：比對時換回原始值；是原始值時照常比對。"""
    ir = parse(tmp_path, bars())
    query = worksheet_sql(ir, ir.worksheets[0].id)
    result = execute(db, query.sql)
    mapping = SheetMapping(columns={"Region": "f_region", "Sales": "sum(f_sales)"})
    aliases = {"f_region": {"東區": "East", "西區": "West", "（未分區）": None}}
    aliased = "Region,Sales\n東區,350\nNorth,60\nSouth,100\n西區,800\n"
    original = "Region,Sales\nEast,350\nNorth,60\nSouth,100\nWest,800\n"
    assert compare(aliased, result, query.columns, mapping, aliases).ok
    assert compare(original, result, query.columns, mapping, aliases).ok
    # 沒有提供別名時，別名對不到原始值
    assert not compare(aliased, result, query.columns, mapping).ok
