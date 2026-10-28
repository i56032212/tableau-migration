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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

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
    Worksheet,
)
from tmig.planner.state import use_key
from tmig.render.format import (
    PERCENT_TABLE_CALCS,
    d3_format,
    display_format,
    format_value,
    parse_format,
)
from tmig.sqlgen.query import OutputColumn

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
}
VL_MARKS = {
    "bar": "bar",
    "line": "line",
    "area": "area",
    "circle": "circle",
    "square": "square",
    "shape": "point",
}
ROW = "__row"
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
    if use.date is not None:
        name = f"{use.date.unit.upper()}({name})"
    elif use.agg is not None and AGG_LABELS[use.agg]:
        name = f"{AGG_LABELS[use.agg]}({name})"
    if use.table_calc is not None:
        tc = next((t for t in ws.table_calcs if t.id == use.table_calc), None)
        if tc is not None and tc.type in TC_LABELS:
            name = f"{name}（{TC_LABELS[tc.type]}）"
    return name


class _Builder:
    """一張工作表的規格；channels 以欄位鍵（Measure Names／Values 以其特殊 id）為鍵。"""

    def __init__(self, ws: Worksheet, channels: dict[str, Channel]) -> None:
        self.ws = ws
        self.channels = channels
        self.notes: list[str] = []

    def ch(self, use: FieldUse) -> Channel:
        return self.channels[use.field if use.field in SPECIAL_FIELDS else use_key(use)]

    def is_measure(self, use: FieldUse) -> bool:
        return self.ch(use).role == "measure"

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
        number = d3_format(parse_format(ch.format)) if formatted else None
        if number:
            encoding["axis"] = {"format": number}
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
            mark = "text"
            y = self.axis_dim(rows_dims, "rows_dim")
            x = self.axis_dim(cols_dims, "cols_dim")
            if y is not None:
                encoding["y"] = {**y, "axis": {"orient": "left", "title": y.get("title")}}
            if x is not None:
                encoding["x"] = {
                    **x,
                    "axis": {"orient": "top", "labelAngle": 0, "title": x.get("title")},
                }
            text_use = marks.label[0] if marks.label else None
            if text_use is None:
                measures = [c for c in self.channels.values() if c.role == "measure"]
                text_use = measures[0].use if measures else None
            vl_mark = {"type": "text", "tooltip": True}
            if text_use is not None:
                encoding["text"] = {"field": self.ch(text_use).label, "type": "nominal"}
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
            if marks.label:
                label_field = self.ch(marks.label[0]).label
                label_type = vl_type

        self.common(encoding, marks)
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
                )
            if mark in UNSUPPORTED_MARKS or mark == "pie":
                self.notes.append(f"{self.ch(use).title} 的標記類型 {mark} 尚未支援，以長條呈現")
                mark = "bar"
            if axis == "rows":
                body, kind = self.unit(marks, mark, rows_dims, cols_dims, [use], cols_meas)
            else:
                body, kind = self.unit(marks, mark, rows_dims, cols_dims, rows_meas, [use])
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
        if not marks.label or rows_dims or cols_dims:
            return {"mark": mark, "encoding": encoding}
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
) -> RenderResult:
    ws = next(w for w in ir.worksheets if w.id == worksheet)
    ds = next(d for d in ir.datasources if d.id == ws.datasource)
    fields = {f.id: f for f in ds.fields}
    index = {key: i for i, key in enumerate(result.columns)}

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
            format=display_format(data_field.format, _table_calc_type(ws, column.use)),
        )
    uses = [*ws.rows, *ws.columns, *ws.marks.uses()]
    uses += [u for pane in ws.panes for u in pane.marks.uses()]
    long = bool(ws.measure_values) and any(u.field in SPECIAL_FIELDS for u in uses)
    builder = _Builder(ws, channels)

    # ---------------------------------------------------------- 資料
    values: list[dict[str, Any]] = []
    for row in result.rows:
        record: dict[str, Any] = {}
        for key, ch in channels.items():
            raw = row[index[key]]
            use = ch.use
            trunc = use.date.unit if use.date is not None and use.date.kind == "trunc" else None
            record[ch.name] = _json_value(raw)
            if use.date is not None and use.date.kind == "part" and raw is not None:
                record[ch.label] = str(raw)
            else:
                record[ch.label] = format_value(raw, ch.datatype, ch.format, trunc)
        values.append(record)
    if long:
        values = _fold(values, [builder.ch(u) for u in ws.measure_values])
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

    rows_dims = [u for u in ws.rows if not builder.is_measure(u)]
    rows_meas = [u for u in ws.rows if builder.is_measure(u)]
    cols_dims = [u for u in ws.columns if not builder.is_measure(u)]
    cols_meas = [u for u in ws.columns if builder.is_measure(u)]
    composites: dict[str, list[FieldUse]] = {}
    if len(rows_dims) > 1:
        composites["rows_dim"] = rows_dims
    if len(cols_dims) > 1:
        composites["cols_dim"] = cols_dims
    pie_marks = _pie_marks(ws)
    for i, record in enumerate(values):
        # 列號：檢視器選取標記時，以它找回未經 Vega 解析的原始值；圓餅各塊也依它排列
        record[ROW] = i
        for name, members in composites.items():
            record[name] = SEPARATOR.join(record[builder.ch(u).label] for u in members)

    mark = ws.marks.type
    if pie_marks is not None:
        body = builder.pie(pie_marks, rows_dims, cols_dims)
        mark = "pie"
    elif len(rows_meas) > 1 or len(cols_meas) > 1:
        body, mark = builder.multi(rows_dims, cols_dims, rows_meas, cols_meas)
    else:
        if mark == "automatic":
            mark = _infer_mark(rows_dims, cols_dims, rows_meas, cols_meas)
        if mark in UNSUPPORTED_MARKS:
            return RenderResult(None, len(result.rows), mark, f"標記類型 {mark} 尚未支援")
        body, mark = builder.unit(ws.marks, mark, rows_dims, cols_dims, rows_meas, cols_meas)

    spec: dict[str, Any] = {
        "$schema": SCHEMA,
        "usermeta": {
            "worksheet": ws.id,
            "name": ws.name,
            "rows": len(values),
            "mark": mark,
            # 篩選動作：標記的維度欄（資料欄名 → 欄位鍵）
            "dimensions": {
                ch.name: key
                for key, ch in channels.items()
                if ch.role == "dimension" and ch.use.field not in SPECIAL_FIELDS
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
    spec.update(body)
    return RenderResult(spec, len(values), mark, None, builder.notes)


def _table_calc_type(ws: Worksheet, use: FieldUse) -> str | None:
    if use.table_calc is None:
        return None
    tc = next((t for t in ws.table_calcs if t.id == use.table_calc), None)
    return tc.type if tc is not None else None


def _fold(values: list[dict[str, Any]], measures: list[Channel]) -> list[dict[str, Any]]:
    """寬表 → 長表：每個量值一列，加上 Measure Names（名稱、順序）與 Measure Values（值、文字）。"""
    long: list[dict[str, Any]] = []
    for record in values:
        for i, ch in enumerate(measures):
            item = dict(record)
            item["__mn"] = ch.title
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
    mark: dict[str, Any] = {"type": "text", "fontSize": 10}
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
    return {"field": ch.label, "type": "ordinal", "sort": None, "title": ch.title}


def _infer_mark(
    rows_dims: list[FieldUse],
    cols_dims: list[FieldUse],
    rows_meas: list[FieldUse],
    cols_meas: list[FieldUse],
) -> str:
    """Tableau 的「自動」標記：沒有量值在欄列架上為文字表；連續日期為折線；其餘為長條。"""
    if not rows_meas and not cols_meas:
        return "text"
    if rows_meas and cols_meas:
        return "circle"
    for use in rows_dims + cols_dims:
        if use.date is not None and use.continuous:
            return "line"
    return "bar"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return float(value)
