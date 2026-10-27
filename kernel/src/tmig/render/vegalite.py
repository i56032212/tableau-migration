"""Vega-Lite 規格產生（F1）：IR 工作表＋查詢結果 → Vega-Lite v6 規格（資料內嵌）。

目標是資訊等價：同樣的標記類型、欄位對應、排序、顏色與標籤，字型等細部樣式不強求一致。

- 長條、折線、面積、圓點、方塊：量值在欄架為橫向、在列架為直向；兩邊都有量值時畫成散佈圖
- 文字表（交叉表）：列架維度為 y、欄架維度為 x，標籤量值為文字
- 同一架上有多個維度時，合併成「外層｜內層」的單一座標，不做分面
- 離散維度依查詢結果的順序排列（查詢已套用工作表的排序）
- 標籤與工具提示的文字依 IR 的 Tableau 格式字串在後端先格式化
- 圓餅、甘特、地圖等尚未支援的標記回傳 None，由檢視器顯示「未支援」
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tmig.data.executor import QueryResult
from tmig.ir.models import IR, DataField, FieldUse, Worksheet
from tmig.planner.state import use_key
from tmig.render.format import d3_format, format_value, parse_format
from tmig.sqlgen.query import OutputColumn

SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
SEPARATOR = "｜"
UNSUPPORTED_MARKS = {"pie", "gantt", "map", "polygon"}
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
}


@dataclass
class Channel:
    """資料中的一個欄位：原始值欄與顯示文字欄。"""

    name: str
    label: str
    title: str
    column: OutputColumn
    data_field: DataField


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
        name = f"c{n}"
        channels[column.key] = Channel(
            name=name,
            label=f"{name}_label",
            title=caption(fields[column.use.field], column.use, ws),
            column=column,
            data_field=fields[column.use.field],
        )

    def is_measure(use: FieldUse) -> bool:
        return channels[use_key(use)].column.role == "measure"

    rows_dims = [u for u in ws.rows if not is_measure(u)]
    rows_meas = [u for u in ws.rows if is_measure(u)]
    cols_dims = [u for u in ws.columns if not is_measure(u)]
    cols_meas = [u for u in ws.columns if is_measure(u)]
    mark = ws.marks.type
    notes: list[str] = []
    if mark == "automatic":
        mark = _infer_mark(rows_dims, cols_dims, rows_meas, cols_meas, channels)
    if mark in UNSUPPORTED_MARKS:
        return RenderResult(None, len(result.rows), mark, f"標記類型 {mark} 尚未支援")
    if len(rows_meas) > 1 or len(cols_meas) > 1:
        notes.append("同一架上有多個量值，只畫第一個")

    # ---------------------------------------------------------- 資料
    composites: dict[str, list[FieldUse]] = {}
    if len(rows_dims) > 1:
        composites["rows_dim"] = rows_dims
    if len(cols_dims) > 1:
        composites["cols_dim"] = cols_dims
    values: list[dict[str, Any]] = []
    for row in result.rows:
        record: dict[str, Any] = {}
        for key, ch in channels.items():
            raw = row[index[key]]
            use = ch.column.use
            assert use is not None
            trunc = use.date.unit if use.date is not None and use.date.kind == "trunc" else None
            record[ch.name] = _json_value(raw)
            if use.date is not None and use.date.kind == "part" and raw is not None:
                record[ch.label] = str(raw)
            else:
                record[ch.label] = format_value(
                    raw, ch.column.datatype, ch.data_field.format, trunc
                )
        for name, uses in composites.items():
            record[name] = SEPARATOR.join(record[channels[use_key(u)].label] for u in uses)
        values.append(record)

    def axis_dim(uses: list[FieldUse], composite: str) -> dict[str, Any] | None:
        if not uses:
            return None
        if len(uses) > 1:
            title = SEPARATOR.join(channels[use_key(u)].title for u in uses)
            return {"field": composite, "type": "ordinal", "sort": None, "title": title}
        return _dimension(channels[use_key(uses[0])])

    def axis_measure(use: FieldUse) -> dict[str, Any]:
        ch = channels[use_key(use)]
        encoding: dict[str, Any] = {"field": ch.name, "type": "quantitative", "title": ch.title}
        number = d3_format(parse_format(ch.data_field.format)) if not use.table_calc else None
        if number:
            encoding["axis"] = {"format": number}
        return encoding

    encoding: dict[str, Any] = {}
    layers: list[dict[str, Any]] = []
    label_field: str | None = None
    label_type = ""
    vl_mark: dict[str, Any]
    if mark == "text" or not (rows_meas or cols_meas):
        mark = "text"
        y = axis_dim(rows_dims, "rows_dim")
        x = axis_dim(cols_dims, "cols_dim")
        if y is not None:
            encoding["y"] = {**y, "axis": {"orient": "left", "title": y.get("title")}}
        if x is not None:
            encoding["x"] = {
                **x,
                "axis": {"orient": "top", "labelAngle": 0, "title": x.get("title")},
            }
        text_use = ws.marks.label[0] if ws.marks.label else None
        if text_use is None:
            measures = [c for c in channels.values() if c.column.role == "measure"]
            text_use = measures[0].column.use if measures else None
        vl_mark = {"type": "text", "tooltip": True}
        if text_use is not None:
            encoding["text"] = {"field": channels[use_key(text_use)].label, "type": "nominal"}
        else:
            encoding["text"] = {"value": "Abc"}
    else:
        vl_type = {
            "bar": "bar",
            "line": "line",
            "area": "area",
            "circle": "circle",
            "square": "square",
        }.get(mark, "point")
        if rows_meas and cols_meas:
            encoding["x"] = axis_measure(cols_meas[0])
            encoding["y"] = axis_measure(rows_meas[0])
            vl_type = "point" if vl_type in ("bar", "line", "area") else vl_type
            for dims in (rows_dims, cols_dims):
                for use in dims:
                    encoding.setdefault("detail", []).append(
                        {"field": channels[use_key(use)].name, "type": "nominal"}
                    )
        elif cols_meas:
            encoding["x"] = axis_measure(cols_meas[0])
            y = axis_dim(rows_dims, "rows_dim")
            if y is not None:
                encoding["y"] = y
            for use in cols_dims:
                encoding.setdefault("detail", []).append(
                    {"field": channels[use_key(use)].name, "type": "nominal"}
                )
        else:
            encoding["y"] = axis_measure(rows_meas[0])
            x = axis_dim(cols_dims, "cols_dim")
            if x is not None:
                encoding["x"] = x
            for use in rows_dims:
                encoding.setdefault("detail", []).append(
                    {"field": channels[use_key(use)].name, "type": "nominal"}
                )
        vl_mark = {"type": vl_type, "tooltip": True}
        if vl_type in ("line", "area"):
            vl_mark["point"] = True
        if ws.marks.label:
            label_field = channels[use_key(ws.marks.label[0])].label
            label_type = vl_type

    if ws.marks.color is not None:
        ch = channels[use_key(ws.marks.color)]
        if ch.column.role == "measure":
            encoding["color"] = {"field": ch.name, "type": "quantitative", "title": ch.title}
        else:
            encoding["color"] = {
                "field": ch.label,
                "type": "nominal",
                "title": ch.title,
                "sort": None,
            }
    if ws.marks.size is not None:
        ch = channels[use_key(ws.marks.size)]
        encoding["size"] = {"field": ch.name, "type": "quantitative", "title": ch.title}
    for use in ws.marks.detail:
        ch = channels[use_key(use)]
        if ch.column.role == "dimension":
            encoding.setdefault("detail", []).append({"field": ch.name, "type": "nominal"})
    encoding["tooltip"] = [{"field": ch.label, "title": ch.title} for ch in channels.values()]
    if label_field is not None:
        layers.append(_label_layer(encoding, label_type, label_field, ws))

    spec: dict[str, Any] = {
        "$schema": SCHEMA,
        "usermeta": {"worksheet": ws.id, "name": ws.name, "rows": len(values), "mark": mark},
        "data": {"values": values},
        "width": "container",
        "height": "container",
        "autosize": {"type": "fit", "contains": "padding"},
        "config": {"view": {"stroke": None}, "axis": {"labelLimit": 160}},
    }
    if layers:
        spec["encoding"] = encoding
        spec["layer"] = [{"mark": vl_mark}, *layers]
    else:
        spec["mark"] = vl_mark
        spec["encoding"] = encoding
    return RenderResult(spec, len(values), mark, None, notes)


def _label_layer(
    encoding: dict[str, Any], vl_type: str, field_name: str, ws: Worksheet
) -> dict[str, Any]:
    """標籤：長條在尾端（堆疊時在各段內側），其他標記在點的上方。"""
    stacked = vl_type == "bar" and ws.marks.color is not None
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
    use = ch.column.use
    assert use is not None
    if use.date is not None and use.date.kind == "trunc" and use.continuous:
        return {
            "field": ch.name,
            "type": "temporal",
            "timeUnit": TIME_UNITS[use.date.unit],
            "title": ch.title,
        }
    if use.continuous and ch.column.datatype in ("integer", "real"):
        return {"field": ch.name, "type": "quantitative", "title": ch.title}
    return {"field": ch.label, "type": "ordinal", "sort": None, "title": ch.title}


def _infer_mark(
    rows_dims: list[FieldUse],
    cols_dims: list[FieldUse],
    rows_meas: list[FieldUse],
    cols_meas: list[FieldUse],
    channels: dict[str, Channel],
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
