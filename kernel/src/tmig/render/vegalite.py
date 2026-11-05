"""Vega-Lite 規格產生（F1、J1–J3）：IR 工作表＋查詢結果 → Vega-Lite v6 規格（資料內嵌）。

目標是資訊等價：同樣的標記類型、欄位對應、排序、顏色與標籤，字型等細部樣式不強求一致。

- 長條、折線、面積、圓點、方塊、形狀：量值在欄架為橫向、在列架為直向；兩邊都有量值時畫成散佈圖
- 文字表（交叉表）：列架維度為 y、欄架維度為 x，標籤量值為文字
- 同一架上有多個維度時，合併成「外層｜內層」的單一座標，不做分面
- 同一架上有多個量值：每個量值一張圖上下（列架）或左右（欄架）排列；雙軸的第二個量值與前一個
  重疊，兩個軸各自的刻度；每個量值使用自己的標記卡
- Measure Names／Values：查詢結果是寬表（每個量值一欄），這裡轉成長表（每個量值一列），
  Measure Names 依 Measure Values 卡的順序，Measure Values 的文字依各量值自己的格式
- 圓餅：角度為 wedge-size 的量值；欄列架上的量值只是撐出版面（例如 MIN(0)），不畫成軸
- 離散維度依查詢結果的順序排列（查詢已套用工作表的排序）
- 標籤與工具提示的文字依 IR 的 Tableau 格式字串在後端先格式化
- 甘特、地圖等尚未支援的標記回傳 None，由檢視器顯示「未支援」
- 參考線（J4）：值在後端依範圍（整張表、窗格、儲存格）以標記的值計算；離散軸上以每個標記位置的
  短線（tick）連成參考線，連續軸或沒有另一軸時畫成橫跨整張圖的線；只有一條時加上標籤
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeGuard

from tmig.data.executor import QueryResult
from tmig.ir.models import (
    IR,
    MEASURE_NAMES,
    MEASURE_VALUES,
    SPECIAL_FIELDS,
    DataField,
    FieldUse,
    Marks,
    Pane,
    ParamRef,
    ReferenceLine,
    Scalar,
    TextRun,
    Worksheet,
)
from tmig.planner.state import use_key
from tmig.render.aliases import alias_key, member_aliases, parameter_label
from tmig.render.format import (
    PERCENT_TABLE_CALCS,
    axis_format,
    display_format,
    format_value,
    parse_format,
)
from tmig.sqlgen.query import GRAND_TOTAL, OutputColumn, TitleQueries, TotalQuery

SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
SEPARATOR = "｜"
UNSUPPORTED_MARKS = {"gantt", "map", "polygon"}
TIME_UNITS = {
    "year": "utcyear",
    "quarter": "utcyearquarter",
    "month": "utcyearmonth",
    "week": "utcyearmonthdate",
    "day": "utcyearmonthdate",
    "hour": "utcyearmonthdatehours",
    "minute": "utcyearmonthdatehoursminutes",
    "second": "utcyearmonthdatehoursminutesseconds",
}
AGG_LABELS = {
    "sum": "",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count": "CNT",
    "countd": "CNTD",
    "median": "MEDIAN",
    "attr": "ATTR",
    "stdev": "STDEV",
    "stdevp": "STDEVP",
    "var": "VAR",
    "varp": "VARP",
}
TC_LABELS = {
    "running_total": "累計",
    "percent_of_total": "合計百分比",
    "rank": "排名",
    "difference": "差異",
    "percent_difference": "差異百分比",
    "moving": "移動計算",
}
MOVING_LABELS = {"avg": "移動平均", "sum": "移動總和", "min": "移動最小值", "max": "移動最大值"}
VL_MARKS = {
    "bar": "bar",
    "line": "line",
    "area": "area",
    "circle": "circle",
    "square": "square",
    "shape": "point",
}
ROW = "__row"
TOTAL = "__total"
"""總計列的標記（資料中為 True）。"""
TEXT_ROW_HEIGHT = 22
"""文字表每一列的高度（像素）：與 Tableau 相同固定列高，列數多時在區塊內捲動。"""
TEXT_COLOR_RANGE = ["#6baed6", "#08306b"]
MEASURE_NAMES_TITLE = "Measure Names"
MEASURE_VALUES_TITLE = "Measure Values"


@dataclass
class Channel:
    """資料中的一個欄位：原始值欄與顯示文字欄。"""

    name: str
    label: str
    title: str
    role: Literal["dimension", "measure"]
    datatype: str
    use: FieldUse
    format: str | None = None
    aliases: dict[Any, str] = field(default_factory=dict)
    """成員別名（原始值的比對鍵 → 顯示名稱）；只有離散維度的原始成員有。"""


@dataclass
class RenderResult:
    spec: dict[str, Any] | None
    rows: int
    mark: str
    unsupported: str | None = None
    notes: list[str] = field(default_factory=list)


def caption(ir_field: DataField, use: FieldUse, ws: Worksheet) -> str:
    """類似 Tableau 欄位膠囊上的名稱。"""
    name = ir_field.caption
    if ir_field.set is not None and use.agg is None:
        name = f"IN/OUT({name})"
    elif use.date is not None:
        name = f"{use.date.unit.upper()}({name})"
    elif use.agg is not None and AGG_LABELS[use.agg]:
        name = f"{AGG_LABELS[use.agg]}({name})"
    if use.table_calc is not None:
        tc = next((t for t in ws.table_calcs if t.id == use.table_calc), None)
        if tc is not None and tc.type in TC_LABELS:
            label = TC_LABELS[tc.type]
            if tc.type == "moving":
                label = MOVING_LABELS.get(str(tc.options.get("aggregation")), label)
            name = f"{name}（{label}）"
    return name


class _Builder:
    """一張工作表的規格；channels 以欄位鍵（Measure Names／Values 以其特殊 id）為鍵。"""

    def __init__(
        self,
        ws: Worksheet,
        channels: dict[str, Channel],
        pane_values: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.ws = ws
        self.channels = channels
        self.notes: list[str] = []
        self.pane_values = pane_values or {}
        """詳細程度較粗的標記卡（ws.panes 的索引）各自的資料列（J3）。"""
        self.rows = list(ws.rows)
        self.columns = list(ws.columns)
        """畫出的欄列架（見 build_spec：沒有對應量值的 Measure Names 不畫）。"""
        self.label_fields: dict[int, str] = {}
        """有自訂標籤文字的標記卡（id(Marks)）→ 資料中組好的標籤欄。"""

    def label_field(self, marks: Marks) -> str | None:
        """標記的標籤欄：自訂的標籤文字，或標籤上第一個欄位的顯示文字。"""
        if id(marks) in self.label_fields:
            return self.label_fields[id(marks)]
        return self.ch(marks.label[0]).label if marks.label else None

    def shown(self, use: FieldUse) -> bool:
        return use.field not in SPECIAL_FIELDS or use.field in self.channels

    def with_pane_data(self, body: dict[str, Any], pane: Pane | None) -> dict[str, Any]:
        """標記卡有自己的資料時，這張圖改用它；工具提示只列出資料中有的欄位。"""
        index = next((i for i, p in enumerate(self.ws.panes) if p is pane), None)
        if index is None or index not in self.pane_values:
            return body
        records = self.pane_values[index]
        present = set(records[0]) if records else set()
        encoding = body.get("encoding")
        if encoding is not None and "tooltip" in encoding:
            encoding["tooltip"] = [t for t in encoding["tooltip"] if t["field"] in present]
        return {**body, "data": {"values": records}}

    def ch(self, use: FieldUse) -> Channel:
        return self.channels[use.field if use.field in SPECIAL_FIELDS else use_key(use)]

    def is_measure(self, use: FieldUse) -> bool:
        return self.ch(use).role == "measure"

    def is_continuous_date(self, use: FieldUse) -> bool:
        """連續的完整日期（不取日期部分或截斷）。"""
        ch = self.channels.get(use_key(use))
        return use.continuous and ch is not None and ch.datatype in ("date", "datetime")

    def is_axis(self, use: FieldUse) -> bool:
        """欄列架上畫成座標軸的量值：連續的量值與 Measure Values。"""
        return self.is_measure(use) and (use.continuous or use.field in SPECIAL_FIELDS)

    # ------------------------------------------------------------ 座標

    def axis_dim(self, uses: list[FieldUse], composite: str) -> dict[str, Any] | None:
        if not uses:
            return None
        if len(uses) > 1:
            title = SEPARATOR.join(self.ch(u).title for u in uses)
            return {"field": composite, "type": "ordinal", "sort": None, "title": title}
        return _dimension(self.ch(uses[0]))

    def axis_measure(self, use: FieldUse) -> dict[str, Any]:
        ch = self.ch(use)
        encoding: dict[str, Any] = {"field": ch.name, "type": "quantitative", "title": ch.title}
        # Table Calculation 的軸只在百分比類時套用格式（累計、排名等沿用自動刻度）
        formatted = not use.table_calc or _table_calc_type(self.ws, use) in PERCENT_TABLE_CALCS
        axis = axis_format(parse_format(ch.format)) if formatted else None
        if axis:
            encoding["axis"] = axis
        return encoding

    # ------------------------------------------------------------ 單一圖

    def unit(
        self,
        marks: Marks,
        mark: str,
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> tuple[dict[str, Any], str]:
        """一張圖的 mark、encoding（與標籤圖層）。回傳規格片段與實際的標記類型。"""
        encoding: dict[str, Any] = {}
        vl_mark: dict[str, Any]
        label_field: str | None = None
        label_type = ""
        if mark == "text" or not (rows_meas or cols_meas):
            # 方塊標記、只有維度並以顏色區分：醒目提示表（色塊加上文字）
            highlight = mark == "square" and marks.color is not None
            mark = "square" if highlight else "text"
            y = self.axis_dim(rows_dims, "rows_dim")
            x = self.axis_dim(cols_dims, "cols_dim")
            if y is not None:
                encoding["y"] = {**y, "axis": {"orient": "left", "title": y.get("title")}}
            if x is not None:
                encoding["x"] = {
                    **x,
                    "axis": {"orient": "top", "labelAngle": 0, "title": x.get("title")},
                }
            text_field = self.label_field(marks)
            if text_field is None:
                measures = [c for c in self.channels.values() if c.role == "measure"]
                text_field = measures[0].label if measures else None
            vl_mark = {"type": "text", "tooltip": True, "lineBreak": "\n"}
            if text_field is not None:
                encoding["text"] = {"field": text_field, "type": "nominal"}
            else:
                encoding["text"] = {"value": "Abc"}
        else:
            vl_type = VL_MARKS.get(mark, "point")
            if rows_meas and cols_meas:
                encoding["x"] = self.axis_measure(cols_meas[0])
                encoding["y"] = self.axis_measure(rows_meas[0])
                vl_type = "point" if vl_type in ("bar", "line", "area") else vl_type
                for dims in (rows_dims, cols_dims):
                    for use in dims:
                        encoding.setdefault("detail", []).append(
                            {"field": self.ch(use).name, "type": "nominal"}
                        )
            elif cols_meas:
                encoding["x"] = self.axis_measure(cols_meas[0])
                y = self.axis_dim(rows_dims, "rows_dim")
                if y is not None:
                    encoding["y"] = y
                for use in cols_dims:
                    encoding.setdefault("detail", []).append(
                        {"field": self.ch(use).name, "type": "nominal"}
                    )
            else:
                encoding["y"] = self.axis_measure(rows_meas[0])
                x = self.axis_dim(cols_dims, "cols_dim")
                if x is not None:
                    encoding["x"] = x
                for use in rows_dims:
                    encoding.setdefault("detail", []).append(
                        {"field": self.ch(use).name, "type": "nominal"}
                    )
            vl_mark = {"type": vl_type, "tooltip": True}
            if vl_type in ("line", "area"):
                vl_mark["point"] = True
            if mark == "shape":
                vl_mark["filled"] = True
            if marks.label or id(marks) in self.label_fields:
                label_field = self.label_field(marks)
                label_type = vl_type

        self.common(encoding, marks)
        if mark == "square":
            text = {
                "mark": vl_mark,
                "encoding": {"text": encoding.pop("text"), "color": {"value": "#1f2328"}},
            }
            return {
                "encoding": encoding,
                "layer": [{"mark": {"type": "rect", "tooltip": True}}, text],
            }, mark
        if vl_mark["type"] == "text" and encoding.get("color", {}).get("type") == "quantitative":
            # 文字依量值著色：用較深的色階，淺色的值才看得清楚
            encoding["color"]["scale"] = {"range": TEXT_COLOR_RANGE}
        if marks.path is not None and vl_mark["type"] in ("line", "area"):
            encoding["order"] = {"field": self.ch(marks.path).name}
        if label_field is not None:
            return {
                "encoding": encoding,
                "layer": [
                    {"mark": vl_mark},
                    _label_layer(encoding, label_type, label_field, marks.color is not None),
                ],
            }, mark
        return {"mark": vl_mark, "encoding": encoding}, mark

    def common(self, encoding: dict[str, Any], marks: Marks) -> None:
        """顏色、大小、形狀、詳細資料與工具提示。"""
        if marks.color is not None:
            ch = self.ch(marks.color)
            if ch.role == "measure":
                encoding["color"] = {"field": ch.name, "type": "quantitative", "title": ch.title}
            else:
                encoding["color"] = {
                    "field": ch.label,
                    "type": "nominal",
                    "title": ch.title,
                    "sort": None,
                }
        if marks.size is not None:
            ch = self.ch(marks.size)
            encoding["size"] = {"field": ch.name, "type": "quantitative", "title": ch.title}
        if marks.shape is not None:
            ch = self.ch(marks.shape)
            encoding["shape"] = {"field": ch.label, "type": "nominal", "title": ch.title}
        for use in marks.detail:
            ch = self.ch(use)
            if ch.role == "dimension":
                encoding.setdefault("detail", []).append({"field": ch.name, "type": "nominal"})
        encoding["tooltip"] = [
            {"field": ch.label, "title": ch.title} for ch in self.channels.values()
        ]

    # ------------------------------------------------------------ 多個量值

    def multi(
        self,
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
        rows_meas: list[FieldUse],
        cols_meas: list[FieldUse],
    ) -> tuple[dict[str, Any], str]:
        """同一架上有多個量值：每個量值一張圖；雙軸的量值與前一張重疊。"""
        axis: Literal["rows", "columns"] = "rows" if len(rows_meas) > 1 else "columns"
        if rows_meas and cols_meas and len(rows_meas) > 1 and len(cols_meas) > 1:
            self.notes.append("欄架與列架都有多個量值，只畫欄架的第一個量值")
            cols_meas = cols_meas[:1]
        measures = rows_meas if axis == "rows" else cols_meas
        remaining = list(self.ws.panes)
        charts: list[dict[str, Any]] = []
        kinds: list[str] = []
        for use in measures:
            pane = _take_pane(remaining, use, axis)
            marks = pane.marks if pane is not None else self.ws.marks
            mark = marks.type
            if mark == "automatic":
                mark = _infer_mark(
                    rows_dims,
                    cols_dims,
                    [use] if axis == "rows" else rows_meas,
                    [use] if axis == "columns" else cols_meas,
                    self.is_continuous_date,
                )
            if mark in UNSUPPORTED_MARKS or mark == "pie":
                self.notes.append(f"{self.ch(use).title} 的標記類型 {mark} 尚未支援，以長條呈現")
                mark = "bar"
            if axis == "rows":
                body, kind = self.unit(marks, mark, rows_dims, cols_dims, [use], cols_meas)
            else:
                body, kind = self.unit(marks, mark, rows_dims, cols_dims, rows_meas, [use])
            body = self.with_pane_data(body, pane)
            kinds.append(kind)
            if pane is not None and pane.dual and charts:
                channel = "y" if axis == "rows" else "x"
                previous = charts.pop()
                layers = (
                    previous["layer"]
                    if "layer" in previous and "resolve" in previous
                    else [previous]
                )
                charts.append(
                    {
                        "layer": [*layers, body],
                        "resolve": {"scale": {channel: "independent"}},
                    }
                )
            else:
                charts.append(body)
        if len(charts) == 1:
            return charts[0], "+".join(kinds)
        return ({"vconcat": charts} if axis == "rows" else {"hconcat": charts}), "+".join(kinds)

    # ------------------------------------------------------------ 圓餅

    def pie(
        self, marks: Marks, rows_dims: list[FieldUse], cols_dims: list[FieldUse]
    ) -> dict[str, Any]:
        """圓餅：角度為 wedge-size，顏色為顏色欄位；欄列架上的維度以分面呈現。"""
        encoding: dict[str, Any] = {}
        if marks.angle is not None:
            encoding["theta"] = {
                "field": self.ch(marks.angle).name,
                "type": "quantitative",
                "stack": True,
            }
        else:
            encoding["theta"] = {"value": 1}
        self.common(encoding, marks)
        if marks.color is not None and encoding["color"]["type"] == "nominal":
            encoding["order"] = {"field": ROW}
        if rows_dims:
            encoding["row"] = self._facet(rows_dims, "rows_dim")
        if cols_dims:
            encoding["column"] = self._facet(cols_dims, "cols_dim")
        mark: dict[str, Any] = {"type": "arc", "tooltip": True}
        has_label = bool(marks.label) or id(marks) in self.label_fields
        if not has_label or rows_dims or cols_dims:
            return {"mark": mark, "encoding": encoding}
        if id(marks) in self.label_fields:
            text = "{" + self.label_fields[id(marks)] + "}"
        else:
            text = "\n".join("{" + self.ch(u).label + "}" for u in marks.label)
        label = {
            "mark": {
                "type": "text",
                "fontSize": 10,
                "lineBreak": "\n",
                "radius": {"expr": "min(width, height) * 0.42"},
            },
            "encoding": {
                "text": {"field": "__pie_label"},
                "color": {"value": "#1f2328"},
                "theta": encoding["theta"],
                **({"order": encoding["order"]} if "order" in encoding else {}),
            },
            "transform": [{"calculate": _template(text), "as": "__pie_label"}],
        }
        return {"encoding": encoding, "layer": [{"mark": mark}, label]}

    def donut(
        self,
        body: dict[str, Any],
        values: list[dict[str, Any]],
        rows_dims: list[FieldUse],
        cols_dims: list[FieldUse],
    ) -> dict[str, Any]:
        """甜甜圈（J3）：圓餅與疊在上面（雙軸）、詳細程度較粗的標記卡；
        後者畫成中間的洞，它的標籤（通常是合計）寫在中間。"""
        holes = [(i, p) for i, p in enumerate(self.ws.panes) if i > 0 and p.dual]
        if not holes:
            return body
        for layer in [body, *body.get("layer", [])]:
            mark = layer.get("mark")
            if isinstance(mark, dict) and mark.get("type") == "arc":
                mark["innerRadius"] = {"expr": "min(width, height) * 0.3"}
            elif isinstance(mark, dict) and mark.get("type") == "text" and "radius" in mark:
                mark["radius"] = {"expr": "min(width, height) * 0.4"}
        if rows_dims or cols_dims:
            self.notes.append("分面的甜甜圈圖中間的標籤尚未畫出（不影響數字）")
            return body
        lines: list[str] = []
        for index, pane in holes:
            records = self.pane_values.get(index, values)
            fields = (
                [self.label_fields[id(pane.marks)]]
                if id(pane.marks) in self.label_fields
                else [self.ch(use).label for use in pane.marks.label]
            )
            for name in fields:
                for record in records:
                    text = record.get(name)
                    if text not in (None, "") and str(text) not in lines:
                        lines.append(str(text))
        if not lines:
            return body
        center = {
            "data": {"values": [{}]},
            "mark": {
                "type": "text",
                "x": {"expr": "width / 2"},
                "y": {"expr": "height / 2"},
                "lineBreak": "\n",
                "fontSize": 14,
                "fontWeight": "bold",
                "color": "#1f2328",
            },
            "encoding": {"text": {"value": "\n".join(lines)}},
        }
        return {"layer": [body, center]}

    def _facet(self, uses: list[FieldUse], composite: str) -> dict[str, Any]:
        if len(uses) > 1:
            title = SEPARATOR.join(self.ch(u).title for u in uses)
            return {"field": composite, "type": "ordinal", "sort": None, "title": title}
        ch = self.ch(uses[0])
        return {"field": ch.label, "type": "ordinal", "sort": None, "title": ch.title}


def build_spec(
    ir: IR,
    worksheet: str,
    columns: list[OutputColumn],
    result: QueryResult,
    parameters: dict[str, Scalar] | None = None,
    panes: dict[int, tuple[list[OutputColumn], QueryResult]] | None = None,
    totals: list[tuple[TotalQuery, QueryResult]] | None = None,
) -> RenderResult:
    """parameters：查詢時的參數值（參數的參考線用）；省略時為 IR 的預設值。
    panes：詳細程度較粗的標記卡各自的查詢結果（tmig.sqlgen.query.pane_sql），以 ws.panes 的索引為鍵。
    totals：總計的查詢結果（tmig.sqlgen.query.totals_sql），附加在資料最後；去掉的維度顯示為 Grand Total。"""
    ws = next(w for w in ir.worksheets if w.id == worksheet)
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    fields = {f.id: f for f in ds.fields}

    channels: dict[str, Channel] = {}
    for n, column in enumerate(c for c in columns if c.role != "sort" and c.use is not None):
        assert column.use is not None
        data_field = fields[column.use.field]
        channels[column.key] = Channel(
            name=f"c{n}",
            label=f"c{n}_label",
            title=caption(data_field, column.use, ws),
            role="measure" if column.role == "measure" else "dimension",
            datatype=column.datatype,
            use=column.use,
            format=value_format(ws, data_field, column.use),
            aliases=member_aliases(data_field, column.use) if column.role == "dimension" else {},
        )
    uses = [*ws.rows, *ws.columns, *ws.marks.uses()]
    uses += [u for pane in ws.panes for u in pane.marks.uses()]
    long = bool(ws.measure_values) and any(u.field in SPECIAL_FIELDS for u in uses)
    builder = _Builder(ws, channels)

    # ---------------------------------------------------------- 資料
    values = _records(channels, result)
    for total, total_result in totals or []:
        for record in _records(channels, total_result):
            for key in total.dropped:
                if key in channels:
                    record[channels[key].name] = None
                    record[channels[key].label] = GRAND_TOTAL
            record[TOTAL] = True
            values.append(record)
    pane_values = {
        i: _records(channels, pane_result) for i, (_, pane_result) in (panes or {}).items()
    }
    if long and pane_values:
        builder.notes.append("用到 Measure Values 時，各標記卡一律以工作表的詳細程度呈現")
        pane_values = {}
    if totals and pane_values:
        # 總計只依工作表的詳細程度查詢：較粗的標記卡另外查詢的資料中沒有總計
        builder.notes.append("有總計時，各標記卡一律以工作表的詳細程度呈現")
        pane_values = {}
    builder.pane_values = pane_values
    if long:
        names = {use_key(a.use): a.label for a in ws.measure_aliases}
        values = _fold(values, [builder.ch(u) for u in ws.measure_values], names)
        channels[MEASURE_NAMES] = Channel(
            "__mn",
            "__mn",
            MEASURE_NAMES_TITLE,
            "dimension",
            "string",
            FieldUse(field=MEASURE_NAMES),
        )
        channels[MEASURE_VALUES] = Channel(
            "__mv",
            "__mv_label",
            MEASURE_VALUES_TITLE,
            "measure",
            "real",
            FieldUse(field=MEASURE_VALUES),
        )

    params = {p.id: p.value for p in ir.parameters} | (parameters or {})
    cards = [(None, ws.marks), *enumerate(p.marks for p in ws.panes)]
    for n, (index, marks) in enumerate(cards):
        if not marks.label_text:
            continue
        name = f"__text{n}"
        builder.label_fields[id(marks)] = name
        targets = [values]
        if index is not None and index in pane_values:
            targets = [pane_values[index]]
        for records in targets:
            for record in records:
                record[name] = compose_text(marks.label_text, ir, params, channels, record)

    # Measure Names 沒有對應的量值（Measure Values 卡是空的）時不畫：Tableau 只顯示空白的欄標題
    builder.rows = [u for u in ws.rows if builder.shown(u)]
    builder.columns = [u for u in ws.columns if builder.shown(u)]
    if len(builder.rows) + len(builder.columns) < len(ws.rows) + len(ws.columns):
        builder.notes.append("Measure Names 沒有對應的量值（Measure Values 卡是空的），不顯示")
    # 欄列架上離散（藍色）的量值是標題、不是座標軸（例如以量值的值排成表格、INDEX() 的排名）
    rows_dims = [u for u in builder.rows if not builder.is_axis(u)]
    rows_meas = [u for u in builder.rows if builder.is_axis(u)]
    cols_dims = [u for u in builder.columns if not builder.is_axis(u)]
    cols_meas = [u for u in builder.columns if builder.is_axis(u)]
    composites: dict[str, list[FieldUse]] = {}
    if len(rows_dims) > 1:
        composites["rows_dim"] = rows_dims
    if len(cols_dims) > 1:
        composites["cols_dim"] = cols_dims
    pie_marks = _pie_marks(ws)
    for i, record in enumerate(values):
        # 列號：檢視器選取標記時，以它找回未經 Vega 解析的原始值；圓餅各塊也依它排列；總計不參與選取
        record[ROW] = -1 if record.get(TOTAL) else i
        for name, members in composites.items():
            labels = [record[builder.ch(u).label] for u in members]
            whole = record.get(TOTAL) and all(label == GRAND_TOTAL for label in labels)
            record[name] = GRAND_TOTAL if whole else SEPARATOR.join(labels)
    for records in pane_values.values():
        for record in records:
            record[ROW] = -1  # 較粗的標記卡不參與選取
            for name, members in composites.items():
                record[name] = SEPARATOR.join(
                    str(record.get(builder.ch(u).label, "")) for u in members
                )

    mark = ws.marks.type
    if pie_marks is not None:
        body = builder.pie(pie_marks, rows_dims, cols_dims)
        if len(ws.panes) > 1:
            body = builder.donut(body, values, rows_dims, cols_dims)
        mark = "pie"
    elif len(rows_meas) > 1 or len(cols_meas) > 1:
        body, mark = builder.multi(rows_dims, cols_dims, rows_meas, cols_meas)
    else:
        if mark == "automatic":
            mark = _infer_mark(
                rows_dims, cols_dims, rows_meas, cols_meas, builder.is_continuous_date
            )
        if mark in UNSUPPORTED_MARKS:
            return RenderResult(None, len(result.rows), mark, f"標記類型 {mark} 尚未支援")
        body, mark = builder.unit(ws.marks, mark, rows_dims, cols_dims, rows_meas, cols_meas)

    for channel in ("row", "column"):
        # 分面依資料的順序（查詢已依排序設定排好）；Vega-Lite 的分面不指定時會依字母排序
        facet = body.get("encoding", {}).get(channel)
        if isinstance(facet, dict) and facet.get("sort") is None:
            facet["sort"] = list(dict.fromkeys(v.get(facet["field"]) for v in values))

    if ws.reference_lines:
        params = {p.id: p.value for p in ir.parameters} | (parameters or {})
        body = _with_reference_lines(builder, body, values, params)

    # 列數不含總計（與工作表的查詢、計算比對的筆數相同）
    marks_count = sum(1 for v in values if not v.get(TOTAL))
    spec: dict[str, Any] = {
        "$schema": SCHEMA,
        "usermeta": {
            "worksheet": ws.id,
            "name": ws.name,
            "rows": marks_count,
            "mark": mark,
            # 篩選與網址動作：標記的維度欄與量值欄（資料欄名 → 欄位鍵）
            "dimensions": {
                ch.name: key
                for key, ch in channels.items()
                if ch.role == "dimension" and ch.use.field not in SPECIAL_FIELDS
            },
            "measures": {
                ch.name: key
                for key, ch in channels.items()
                if ch.role == "measure" and ch.use.field not in SPECIAL_FIELDS
            },
        },
        "data": {"values": values},
        "width": "container",
        "height": "container",
        "autosize": {"type": "fit", "contains": "padding"},
        "config": {"view": {"stroke": None}, "axis": {"labelLimit": 160}},
    }
    if "vconcat" in body or "hconcat" in body:
        # 多張圖無法自動填滿容器，由檢視器依區塊大小設定每張圖的寬高
        del spec["width"], spec["height"]
        spec["autosize"] = {"type": "pad"}
    elif mark in ("text", "square") and rows_dims:
        # 文字表與醒目提示表：固定列高，列數多時由檢視器在區塊內捲動
        spec["height"] = {"step": TEXT_ROW_HEIGHT}
        spec["autosize"] = {"type": "fit-x", "contains": "padding"}
    spec.update(body)
    if totals and not ws.totals_colored:
        _exclude_totals_from_color(spec, values, mark)
    return RenderResult(spec, marks_count, mark, None, builder.notes)


def value_format(ws: Worksheet, data_field: DataField, use: FieldUse) -> str | None:
    """欄位值的顯示格式：工作表上設定的格式優先（Measure Values 的格式套用到其中每個量值），
    其次是欄位的預設格式（百分比類的快速 Table Calculation 預設以百分比顯示）。"""
    key = use_key(use)
    sheet_format = ws.formats.get(key)
    if sheet_format is None and any(use_key(u) == key for u in ws.measure_values):
        sheet_format = ws.formats.get(MEASURE_VALUES)
    return sheet_format or display_format(data_field.format, _table_calc_type(ws, use))


def compose_text(
    runs: list[TextRun],
    ir: IR,
    params: dict[str, Scalar],
    channels: dict[str, Channel] | None = None,
    record: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """自訂文字（標籤、標題）：文字、參數的值與欄位值依序串接；Null 的欄位不顯示（與 Tableau 相同）。

    欄位值取自這個標記（channels、record），或已算好的顯示文字（fields：欄位鍵 → 文字，標題用）。"""
    parts: list[str] = []
    for run in runs:
        if run.text is not None:
            parts.append(run.text)
        elif run.parameter is not None:
            param = next((p for p in ir.parameters if p.id == run.parameter), None)
            value = params.get(run.parameter)
            if param is not None and value is not None:
                label = parameter_label(param, value)
                parts.append(label if label is not None else format_value(value, param.datatype))
        elif run.use is not None and fields is not None:
            parts.append(fields.get(use_key(run.use), ""))
        elif run.use is not None and channels is not None and record is not None:
            key = run.use.field if run.use.field in SPECIAL_FIELDS else use_key(run.use)
            ch = channels.get(key)
            # Null 的欄位不顯示，除非 Null 設有別名
            if ch is not None and (record.get(ch.name) is not None or None in ch.aliases):
                parts.append(str(record.get(ch.label, "")))
    return "".join(parts)


def worksheet_title(
    ir: IR,
    ws: Worksheet,
    parameters: dict[str, Scalar] | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """工作表的標題：自訂標題（代入目前的參數值與標題中的欄位值，見 title_values），沒有時為工作表名稱。"""
    if not ws.title:
        return ws.name
    params = {p.id: p.value for p in ir.parameters} | (parameters or {})
    return compose_text(ws.title, ir, params, fields=fields or {}) or ws.name


MULTIPLE_VALUES = "*"
"""標題中的維度有多個值時顯示的文字（與 Tableau 的 ATTR 相同）。"""


def title_values(
    ir: IR,
    ws: Worksheet,
    queries: TitleQueries,
    measures: QueryResult | None,
    dimensions: dict[str, QueryResult],
    main: tuple[list[OutputColumn], QueryResult] | None = None,
) -> dict[str, str]:
    """標題中欄位值的顯示文字（欄位鍵 → 文字）：量值依格式顯示；維度只有一個值時顯示它（成員別名、
    日期部分照樣套用），多個值時為 *；Null 與沒有資料時不顯示。
    main：工作表查詢的欄位與結果（標題中在檢視中的維度直接取它的值）。"""
    fields = {f.id: f for d in ir.datasources for f in d.fields}
    texts: dict[str, str] = {}
    if queries.measures is not None and measures is not None and measures.rows:
        index = {key: i for i, key in enumerate(measures.columns)}
        row = measures.rows[0]
        for column in queries.measures.columns:
            use = column.use
            if use is None or column.key not in index or row[index[column.key]] is None:
                continue
            fmt = value_format(ws, fields[use.field], use)
            texts[column.key] = format_value(row[index[column.key]], column.datatype, fmt)
    sources: dict[str, tuple[OutputColumn, QueryResult]] = {}
    for key, query in queries.dimensions.items():
        column = next((c for c in query.columns if c.key == key), None)
        if column is not None and key in dimensions:
            sources[key] = (column, dimensions[key])
    if main is not None:
        main_columns, main_result = main
        for column in main_columns:
            if column.key in queries.from_main:
                sources[column.key] = (column, main_result)
    for key, (column, result) in sources.items():
        if column.use is None or key not in result.columns:
            continue
        position = result.columns.index(key)
        values = list(dict.fromkeys(row[position] for row in result.rows))
        if len(values) > 1:
            texts[key] = MULTIPLE_VALUES
            continue
        if not values or values[0] is None:
            continue
        value, use = values[0], column.use
        alias = member_aliases(fields[use.field], use).get(alias_key(value))
        if alias is not None:
            texts[key] = alias
        elif use.date is not None and use.date.kind == "part":
            texts[key] = str(value)
        else:
            trunc = use.date.unit if use.date is not None else None
            fmt = value_format(ws, fields[use.field], use)
            texts[key] = format_value(value, column.datatype, fmt, trunc)
    return texts


def _exclude_totals_from_color(
    spec: dict[str, Any], values: list[dict[str, Any]], mark: str
) -> None:
    """與 Tableau 的預設相同，依量值著色時總計不上色、也不影響色階的範圍（醒目提示表最明顯）。"""
    color = spec.get("encoding", {}).get("color")
    if not isinstance(color, dict) or color.get("type") != "quantitative":
        return
    numbers = [
        v[color["field"]] for v in values if not v.get(TOTAL) and _is_number(v.get(color["field"]))
    ]
    if numbers:
        color["scale"] = {**color.get("scale", {}), "domain": [min(numbers), max(numbers)]}
    color["condition"] = {
        "test": f"datum['{TOTAL}'] === true",
        "value": "transparent" if mark == "square" else "#1f2328",
    }


def _records(channels: dict[str, Channel], result: QueryResult) -> list[dict[str, Any]]:
    """查詢結果 → 資料列：原始值欄與顯示文字欄；只轉換結果中有的欄位。"""
    index = {key: i for i, key in enumerate(result.columns)}
    present = [(key, ch) for key, ch in channels.items() if key in index]
    records: list[dict[str, Any]] = []
    for row in result.rows:
        record: dict[str, Any] = {}
        for key, ch in present:
            raw = row[index[key]]
            use = ch.use
            trunc = use.date.unit if use.date is not None and use.date.kind == "trunc" else None
            record[ch.name] = _json_value(raw)
            alias = ch.aliases.get(alias_key(raw)) if ch.aliases else None
            if alias is not None:
                record[ch.label] = alias
            elif use.date is not None and use.date.kind == "part" and raw is not None:
                record[ch.label] = str(raw)
            else:
                record[ch.label] = format_value(raw, ch.datatype, ch.format, trunc)
        records.append(record)
    return records


COMPUTATION_NAMES = {
    "constant": "Constant",
    "average": "Average",
    "sum": "Sum",
    "total": "Total",
    "min": "Minimum",
    "max": "Maximum",
    "median": "Median",
    "percentile": "Percentile",
    "stdev": "Std. Dev.",
}
SCOPE_NAMES = {"table": "Table", "pane": "Pane", "cell": "Cell"}
REF_COLOR = "#444"
REF = "__ref"
"""參考線的資料欄與圖層名稱前綴；檢視器選取標記時略過這些圖層。"""


def _with_reference_lines(
    builder: _Builder,
    body: dict[str, Any],
    values: list[dict[str, Any]],
    params: dict[str, Scalar],
) -> dict[str, Any]:
    """參考線圖層：每一列資料寫入所屬範圍的參考值（__ref…），再以 rule 或 tick 畫出。

    範圍：整張表；窗格＝欄列架上的離散維度，但各架最內層若是離散維度則不算（它切出的是儲存格）；
    儲存格＝欄列架上所有的離散維度。
    """
    ws, notes = builder.ws, builder.notes
    encoding = body.get("encoding") if "mark" in body or "layer" in body else None
    if encoding is None:
        notes.append("多個量值、圓餅等圖表上的參考線尚未畫出（不影響數字）")
        return body
    scopes = _scope_keys(builder)
    layers: list[dict[str, Any]] = []
    for n, line in enumerate(ws.reference_lines):
        axis = builder.channels.get(use_key(line.axis))
        position = next(
            (
                c
                for c in ("x", "y")
                if axis is not None and encoding.get(c, {}).get("field") == axis.name
            ),
            None,
        )
        if axis is None or position is None:
            notes.append(f"參考線 {line.id} 的量值不在座標軸上，沒有畫出")
            continue
        source = _reference_source(line, builder.channels, params)
        if source is None:
            notes.append(f"參考線 {line.id} 的值不在查詢結果中，沒有畫出")
            continue
        if line.formula == "total" and isinstance(source, str):
            agg = line.value.agg if isinstance(line.value, FieldUse) else None
            if agg not in ("sum", "count", "min", "max"):
                notes.append(f"參考線 {line.id} 的合計需要重新彙總整體資料，沒有畫出")
                continue
        group_keys = scopes[line.scope]
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for record in values:
            if record.get(TOTAL):
                continue  # 總計的列與欄不畫參考線，也不納入計算
            groups.setdefault(tuple(record.get(k) for k in group_keys), []).append(record)
        params_of_line: list[float | None] = (
            list(line.factors)
            if line.formula == "stdev"
            else list(line.percentiles)
            if line.formula == "percentile"
            else [None]
        )
        names = [f"{REF}{n}_{i}" for i in range(len(params_of_line))]
        first: list[float] = []
        for members in groups.values():
            numbers = (
                [float(v) for m in members if _is_number(v := m.get(source))]
                if isinstance(source, str)
                else []
            )
            for name, extra in zip(names, params_of_line, strict=True):
                value = _reference_value(line, numbers, source, extra)
                if value is not None and name == names[0]:
                    first.append(value)
                for m in members:
                    m[name] = value
        other = "y" if position == "x" else "x"
        other_enc = encoding.get(other)
        title = encoding[position].get("title")
        per_mark = (
            len(groups) > 1
            and other_enc is not None
            and other_enc.get("type") in ("nominal", "ordinal")
        )
        for name in names:
            measure = {"field": name, "type": "quantitative", "title": title}
            if per_mark:
                # 離散軸：每個標記位置畫一段短線，同一範圍的短線連成該範圍的參考線
                layers.append(
                    {
                        "name": name,
                        "transform": [{"calculate": "-1", "as": ROW}],
                        "mark": {"type": "tick", "color": REF_COLOR, "thickness": 1.5},
                        "encoding": {position: measure, other: other_enc},
                    }
                )
                continue
            rule: dict[str, Any] = {position: {**measure, "aggregate": "max"}}
            if len(groups) > 1:
                rule["detail"] = [{"field": k, "type": "nominal"} for k in group_keys]
            layers.append(
                {
                    "name": name,
                    "mark": {"type": "rule", "color": REF_COLOR, "strokeDash": [5, 3]},
                    "encoding": rule,
                }
            )
        label = _reference_label(line, axis, first)
        if label and len(groups) == 1 and len(names) == 1:
            place: dict[str, Any] = (
                {"y": {"value": 0}, "align": "left", "dx": 3, "dy": 0}
                if position == "x"
                else {"x": {"value": "width"}, "align": "right", "dx": 0, "dy": -3}
            )
            layers.append(
                {
                    "name": f"{REF}{n}_label",
                    "mark": {
                        "type": "text",
                        "color": REF_COLOR,
                        "align": place.pop("align"),
                        "baseline": "bottom" if position == "y" else "top",
                        "dx": place.pop("dx"),
                        "dy": place.pop("dy"),
                    },
                    "encoding": {
                        position: {
                            "field": names[0],
                            "type": "quantitative",
                            "aggregate": "max",
                            "title": title,
                        },
                        **place,
                        "text": {"value": label},
                    },
                }
            )
    if not layers:
        return body
    return {"layer": [body, *layers]}


def _scope_keys(builder: _Builder) -> dict[str, list[str]]:
    """參考線範圍 → 分組用的資料欄名。"""
    pane: list[FieldUse] = []
    cell: list[FieldUse] = []
    for shelf in (builder.rows, builder.columns):
        discrete = [u for u in shelf if not builder.is_measure(u) and not u.continuous]
        cell += discrete
        innermost_discrete = bool(shelf) and bool(discrete) and shelf[-1] is discrete[-1]
        pane += discrete[:-1] if innermost_discrete else discrete

    def names(uses: list[FieldUse]) -> list[str]:
        return [builder.ch(u).name for u in uses if u.field not in SPECIAL_FIELDS]

    return {"table": [], "pane": names(pane), "cell": names(cell)}


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _reference_source(
    line: ReferenceLine, channels: dict[str, Channel], params: dict[str, Scalar]
) -> str | float | None:
    """參考線的值來源：資料欄名（以標記的值計算），或常數、參數的值。"""
    if isinstance(line.value, ParamRef):
        value = params.get(line.value.parameter)
        return float(value) if _is_number(value) else None
    if line.formula == "constant" or line.value is None:
        return float(line.constant) if _is_number(line.constant) else None
    ch = channels.get(use_key(line.value))
    return ch.name if ch is not None else None


def _reference_value(
    line: ReferenceLine, numbers: list[float], source: str | float, extra: float | None
) -> float | None:
    """一個範圍的參考值；extra 為百分位數或標準差倍數。"""
    if not isinstance(source, str):
        return source
    if not numbers:
        return None
    formula = line.formula
    agg = line.value.agg if isinstance(line.value, FieldUse) else None
    if formula == "average":
        return statistics.fmean(numbers)
    if formula == "sum" or (formula == "total" and agg in ("sum", "count")):
        return math.fsum(numbers)
    if formula == "min" or (formula == "total" and agg == "min"):
        return min(numbers)
    if formula == "max" or (formula == "total" and agg == "max"):
        return max(numbers)
    if formula == "median":
        return statistics.median(numbers)
    if formula == "percentile":
        ordered = sorted(numbers)
        position = (len(ordered) - 1) * (50.0 if extra is None else extra) / 100
        low = math.floor(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    if formula == "stdev":
        if len(numbers) < (2 if line.sample else 1):
            return None
        sd = statistics.stdev(numbers) if line.sample else statistics.pstdev(numbers)
        return statistics.fmean(numbers) + (extra or 0.0) * sd
    return None


def _reference_label(line: ReferenceLine, axis: Channel, values: list[float]) -> str:
    """只有一條線時的標籤；自動與值都顯示依量值格式化的值。"""
    if line.label == "none" or not values:
        return ""
    text = format_value(values[0], "real", axis.format, None)
    computation = COMPUTATION_NAMES[line.formula]
    if line.label == "computation":
        return computation
    if line.label == "custom":
        return (
            (line.label_text or "")
            .replace("<Value>", text)
            .replace("<Computation>", computation)
            .replace("<Scope>", SCOPE_NAMES[line.scope])
        )
    return text


def _table_calc_type(ws: Worksheet, use: FieldUse) -> str | None:
    if use.table_calc is None:
        return None
    tc = next((t for t in ws.table_calcs if t.id == use.table_calc), None)
    return tc.type if tc is not None else None


def _fold(
    values: list[dict[str, Any]], measures: list[Channel], names: dict[str, str]
) -> list[dict[str, Any]]:
    """寬表 → 長表：每個量值一列，加上 Measure Names（名稱、順序）與 Measure Values（值、文字）。
    names：Measure Names 的別名（量值的欄位鍵 → 顯示名稱）。"""
    long: list[dict[str, Any]] = []
    for record in values:
        for i, ch in enumerate(measures):
            item = dict(record)
            item["__mn"] = names.get(use_key(ch.use), ch.title)
            item["__mn_i"] = i
            item["__mv"] = record[ch.name]
            item["__mv_label"] = record[ch.label]
            long.append(item)
    return long


def _take_pane(remaining: list[Pane], use: FieldUse, axis: str) -> Pane | None:
    for pane in remaining:
        if pane.axis == axis and pane.measure == use:
            remaining.remove(pane)
            return pane
    return None


def _pie_marks(ws: Worksheet) -> Marks | None:
    """圓餅的標記卡：共用標記卡或第一張個別標記卡是圓餅時。"""
    if ws.panes:
        first = ws.panes[0].marks
        if first.type == "pie":
            return first
        return None
    return ws.marks if ws.marks.type == "pie" else None


def _template(text: str) -> str:
    """{c0_label}｜{c1_label} → Vega 運算式。"""
    parts: list[str] = []
    rest = text
    while rest:
        start = rest.find("{")
        if start < 0:
            parts.append(repr(rest))
            break
        if start:
            parts.append(repr(rest[:start]))
        end = rest.index("}", start)
        parts.append(f"datum['{rest[start + 1 : end]}']")
        rest = rest[end + 1 :]
    return " + ".join(parts) if parts else "''"


def _label_layer(
    encoding: dict[str, Any], vl_type: str, field_name: str, colored: bool
) -> dict[str, Any]:
    """標籤：長條在尾端（堆疊時在各段內側），其他標記在點的上方。"""
    stacked = vl_type == "bar" and colored
    horizontal = encoding.get("x", {}).get("type") == "quantitative"
    mark: dict[str, Any] = {"type": "text", "fontSize": 10, "lineBreak": "\n"}
    layer: dict[str, Any] = {"text": {"field": field_name}, "color": {"value": "#1f2328"}}
    if vl_type == "bar" and horizontal:
        mark.update(align="right", dx=-3) if stacked else mark.update(align="left", dx=3)
        if stacked:
            layer["x"] = {**encoding["x"], "stack": "zero"}
    elif vl_type == "bar":
        mark.update(baseline="top", dy=3) if stacked else mark.update(baseline="bottom", dy=-3)
        if stacked:
            layer["y"] = {**encoding["y"], "stack": "zero"}
    else:
        mark.update(baseline="bottom", dy=-4)
    if stacked and "color" in encoding:
        # 文字改成白色，但仍以顏色欄位分組，才能與長條同樣堆疊
        layer["color"] = {"value": "#ffffff"}
        layer["detail"] = {"field": encoding["color"]["field"], "type": encoding["color"]["type"]}
    return {"mark": mark, "encoding": layer}


def _dimension(ch: Channel) -> dict[str, Any]:
    use = ch.use
    if use.date is not None and use.date.kind == "trunc" and use.continuous:
        return {
            "field": ch.name,
            "type": "temporal",
            "timeUnit": TIME_UNITS[use.date.unit],
            "title": ch.title,
        }
    if use.continuous and ch.datatype in ("integer", "real"):
        return {"field": ch.name, "type": "quantitative", "title": ch.title}
    if use.continuous and ch.datatype == "date":
        # 連續的完整日期（不取日期部分或截斷）：時間軸；日期字串以 UTC 解讀，避免時區差一天
        return {
            "field": ch.name,
            "type": "temporal",
            "timeUnit": "utcyearmonthdate",
            "title": ch.title,
        }
    if use.continuous and ch.datatype == "datetime":
        return {"field": ch.name, "type": "temporal", "scale": {"type": "utc"}, "title": ch.title}
    return {"field": ch.label, "type": "ordinal", "sort": None, "title": ch.title}


def _infer_mark(
    rows_dims: list[FieldUse],
    cols_dims: list[FieldUse],
    rows_meas: list[FieldUse],
    cols_meas: list[FieldUse],
    is_continuous_date: Callable[[FieldUse], bool] = lambda use: False,
) -> str:
    """Tableau 的「自動」標記：沒有量值在欄列架上為文字表；連續日期為折線；其餘為長條。"""
    if not rows_meas and not cols_meas:
        return "text"
    if rows_meas and cols_meas:
        return "circle"
    for use in rows_dims + cols_dims:
        if (use.date is not None and use.continuous) or is_continuous_date(use):
            return "line"
    return "bar"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return float(value)
