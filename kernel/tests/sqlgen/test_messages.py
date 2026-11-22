"""查詢錯誤（QueryError）的文字：與畫面上的寫法相同，不寫內部的代號。

QueryError 的文字會顯示給使用者：檢視器上「尚不支援」的原因、編輯器的錯誤提示（422 的 detail）、/render 的
附註（「各標記卡無法另外查詢…」「顯示空的列／欄：查不到成員（…）」）。2026-11-22 以前寫的是篩選的 id
（「Top N 篩選 dsf_top 不能逐列套用」）、欄位鍵與輸出欄名（「sum(f_sales) 是列層級的值…」）、Table Calculation
的 id 與公式裡的內部名稱（[Calculation_1]）；現在與附註相同（見 tests/render/test_names.py）：欄位是膠囊上的
寫法（SUM(Sales)）、篩選是它的欄位、Table Calculation 是它的膠囊、公式是公式編輯器上的寫法。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tmig.ir import IR, FieldUse, TableCalc, Worksheet
from tmig.ir.models import ConditionFilter, ParamRef, TopNFilter
from tmig.parser.workbook import parse_workbook
from tmig.sqlgen.query import QueryError, domain_sql, worksheet_sql

from ..parser import builder as b
from ..parser.builder import calc, instance, ref, worksheet

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tmig"
REGION = instance("[Region]", "None", "[none:Region:nk]")
SALES = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
SUM_SALES = FieldUse(field="f_sales", agg="sum", continuous=True)
INTERNAL = re.compile(
    r"\bf_[a-z_0-9]+\b|\b(flt|dsf|tc)_|\b(sum|avg|min|max)\(|Calculation_\d|Parameter \d"
)
"""錯誤訊息裡不該出現的內部代號：欄位 id（f_sales）、篩選與 Table Calculation 的 id、輸出欄名
（sum(f_sales)）、計算欄位與參數的內部名稱（[Calculation_1]、[Parameter 1]）。"""


@pytest.fixture
def ir(tmp_path: Path) -> IR:
    """長條圖：列架上是 Region，欄架上是 SUM(Sales)；資料源有彙總的計算欄位 Profit Ratio。"""
    ratio = calc("Calculation_1", "Profit Ratio", "SUM([Profit]) / SUM([Sales])")
    sheet = worksheet("Bars", [REGION, SALES], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    xml = b.workbook(b.extract_datasource(columns=ratio), worksheets=sheet)
    parsed = parse_workbook(b.write_twb(tmp_path, xml))
    assert parsed.unsupported == [], [u.reason for u in parsed.unsupported]
    return parsed


def failure(ir: IR, **changes: object) -> str:
    """工作表改掉幾個欄位之後產生查詢的錯誤訊息。"""
    sheet: Worksheet = ir.worksheets[0].model_copy(update=changes)
    patched = ir.model_copy(update={"worksheets": [sheet]})
    with pytest.raises(QueryError) as caught:
        worksheet_sql(patched, sheet.id)
    message = str(caught.value)
    assert not INTERNAL.search(message), message
    return message


def condition(formula: str) -> ConditionFilter:
    return ConditionFilter(
        kind="condition",
        id="flt_cond",
        field=FieldUse(field="f_region"),
        stage="dimension",
        condition=formula,
        source_ref="test",
    )


def top(by: str = "SUM([Sales])", count: int | ParamRef = 2) -> TopNFilter:
    return TopNFilter(
        kind="top_n",
        id="dsf_top",
        field=FieldUse(field="f_region"),
        stage="top_n",
        direction="top",
        count=count,
        by=by,
        source_ref="test",
    )


def test_filter_errors_name_the_filter_by_its_field(ir: IR) -> None:
    """篩選：以篩選的欄位稱呼（與篩選架上的膠囊相同），公式寫成公式編輯器上的寫法（顯示名稱）。"""
    broken = failure(ir, filters=[condition("[Calculation_1] >")])
    assert broken.startswith("條件篩選「Region」的公式「[Profit Ratio] >」：")
    assert failure(ir, filters=[top(by="[Sales]")]) == "Top N 篩選「Region」的排名依據必須是彙總"
    # 參數決定的數量：參數的名稱，不是內部名稱
    text = next(p for p in ir.parameters if p.datatype == "string")
    by_text = failure(ir, filters=[top(count=ParamRef(parameter=text.id))])
    assert by_text == f"Top N 篩選「Region」的參數「{text.caption}」的值 'East' 不是整數"

    # 資料源篩選（逐列套用）：篩選控制項的可選值（編輯器的資料源篩選對話框）
    ranked = top().model_copy(update={"stage": "datasource"})
    source = ir.datasources[0].model_copy(update={"filters": [ranked]})
    patched = ir.model_copy(update={"datasources": [source]})
    with pytest.raises(QueryError) as caught:
        domain_sql(patched, source.id, FieldUse(field="f_region"), "values")
    assert str(caught.value) == "Top N 篩選「Region」不能逐列套用"
    with pytest.raises(QueryError) as caught:
        domain_sql(patched, source.id, FieldUse(field="f_removed"), "values")
    assert str(caught.value) == "資料源「Orders」沒有這個欄位（可能已經刪除）"


def test_measure_errors_name_the_pill(ir: IR) -> None:
    """量值與 Table Calculation：以膠囊上的寫法稱呼（SUM(Sales)、Region △），不寫欄位鍵與 id。"""
    ratio = next(f for f in ir.datasources[0].fields if f.caption == "Profit Ratio")
    again = FieldUse(field=ratio.id, agg="sum", continuous=True)
    assert failure(ir, columns=[again]) == (
        "Profit Ratio 已經是彙總，不能再彙總（SUM(Profit Ratio)）"
    )
    gone = SUM_SALES.model_copy(update={"table_calc": "tc_gone"})
    assert failure(ir, columns=[gone]) == "SUM(Sales) 的 Table Calculation 設定不存在"

    # 差異計算的比較對象是檢視中沒有的欄位
    difference = TableCalc(
        id="tc_diff",
        type="difference",
        preset="table_down",
        options={"relative_to": "member", "member_field": "f_category", "member": "Office"},
        source_ref="test",
    )
    diff = SUM_SALES.model_copy(update={"table_calc": "tc_diff"})
    assert failure(ir, columns=[diff], table_calcs=[difference]) == (
        "差異計算的比較欄位「Category」不在檢視中"
    )

    # 列層級的欄位做累計（沒有彙總）：膠囊上的寫法（快速 Table Calculation 的後面有「△」）
    running = TableCalc(
        id="tc_running", type="running_total", preset="table_down", source_ref="test"
    )
    row_level = FieldUse(field="f_sales", continuous=True, table_calc="tc_running")
    message = failure(ir, columns=[row_level], table_calcs=[running])
    assert message.startswith("Sales △"), message


def test_query_errors_do_not_interpolate_internal_ids() -> None:
    """查詢與執行的錯誤（QueryError、ExecutionError）的文字裡代入的值不能是內部的代號：篩選與 Table
    Calculation 的 id（flt.id、use.table_calc）、欄位 id 與欄位鍵（use.field、m.key、key）、彙總與種類的代號
    （use.agg、tc.type）、公式的原文（flt.condition、flt.by：內部名稱，要經過 readable）。稱呼東西時用
    WorksheetQuery 的 pill、filter_name、caption 與 readable（見 tmig.render.names）。

    檢查 tmig.sqlgen 與 tmig.data 裡每一個 QueryError(…)、ExecutionError(…) 的 f-string；「內部錯誤：」開頭的
    （程式的錯誤，不是使用者能處理的）不檢查。資料庫自己的錯誤訊息（{exc}）照原樣附上，不在這裡檢查。"""
    forbidden = re.compile(
        r"(\.(id|key|field|table_calc|agg|type|formula|condition|by|parameter)|_id|^(key|ref|missing))$"
    )
    found: list[str] = []
    for folder in ("sqlgen", "data"):
        for path in sorted((SOURCE / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name not in ("QueryError", "ExecutionError") or not node.args:
                    continue
                text = node.args[0]
                if not isinstance(text, ast.JoinedStr):
                    continue
                head = text.values[0] if text.values else None
                if isinstance(head, ast.Constant) and str(head.value).startswith("內部錯誤"):
                    continue
                for part in ast.walk(text):
                    if isinstance(part, ast.FormattedValue):
                        expression = ast.unparse(part.value)
                        if forbidden.search(expression):
                            found.append(f"{path.name}:{node.lineno} {{{expression}}}")
    assert found == [], found
