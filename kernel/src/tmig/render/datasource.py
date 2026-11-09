"""資料源分頁（Tableau Desktop 的「資料來源」分頁）：連線、資料表與 join、Relationship 模型的關聯、
欄位與資料源篩選的摘要，給檢視器顯示。資料預覽的查詢見 tmig.sqlgen.query.preview_sql。

IR 本來就不含帳號、密碼或權杖；摘要只給可以取得完整報表定義的人（見 tmig_service.access）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from tmig.ir.models import (
    CategoricalFilter,
    ConditionFilter,
    DataField,
    Datasource,
    ExtractConnection,
    FieldUse,
    Filter,
    JoinRelation,
    JoinTerm,
    LiveConnection,
    ModelRelation,
    Parameter,
    RangeFilter,
    Relation,
    RelativeDateFilter,
    Scalar,
    SqlRelation,
    TableRelation,
    TopNFilter,
    TupleFilter,
)

DATABASES = {
    "hyper": "Tableau 萃取（Hyper）",
    "postgres": "PostgreSQL",
    "sqlserver": "Microsoft SQL Server",
    "oracle": "Oracle",
    "mysql": "MySQL",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "spark": "Spark SQL",
    "redshift": "Amazon Redshift",
    "bigquery": "Google BigQuery",
    "teradata": "Teradata",
    "excel-direct": "Microsoft Excel",
    "textscan": "文字檔",
    "sqlproxy": "Tableau Server 資料源",
}
"""Tableau 的連線類型（dbclass）→ 顯示名稱；沒有列出的顯示原文。"""
JOINS = {"inner": "內部", "left": "左側", "right": "右側", "full": "完整外部"}
PERIODS = {
    "year": "年",
    "quarter": "季",
    "month": "月",
    "week": "週",
    "day": "天",
    "hour": "小時",
    "minute": "分鐘",
    "second": "秒",
}
AGGREGATIONS = {
    "sum": "SUM",
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
VALUES_SHOWN = 10
"""篩選的說明中最多列出幾個值。"""
NAME = re.compile(r"(\[Parameters\]\.)?\[((?:[^\]]|\]\])+)\]")
"""公式中的欄位與參數參照（內部名稱），例如 [Calculation_123]、[Parameters].[Parameter 1]。"""


def datasource_summary(ds: Datasource, parameters: Sequence[Parameter] = ()) -> dict[str, Any]:
    """一個資料源的摘要：連線、資料表（Relationship 模型為邏輯資料表與關聯）、欄位與篩選。
    計算欄位的公式與 Tableau 的公式編輯器相同，欄位與參數以顯示名稱呈現。"""
    fields = {f.id: f for f in ds.fields}
    names = {f.name: f.caption for f in ds.fields}
    params = {p.name: p.caption for p in parameters}
    model = _model(ds)
    tables = (
        [
            {
                "id": table.id,
                "caption": table.caption,
                "physical": _physical(table.relation),
                "joins": _joins(table.relation),
            }
            for table in model.tables
        ]
        if model is not None
        else _single(ds)
    )
    relationships = (
        [
            {
                "left": _table_caption(model, rel.left),
                "right": _table_caption(model, rel.right),
                "on": [
                    f"{_caption(fields, c.left)} {c.op} {_caption(fields, c.right)}" for c in rel.on
                ],
            }
            for rel in model.relationships
        ]
        if model is not None
        else []
    )
    return {
        "id": ds.id,
        "caption": ds.caption,
        "name": ds.name,
        "connection": _connection(ds.connection),
        "published": ds.published.model_dump(mode="json") if ds.published else None,
        "model": model is not None,
        "tables": tables,
        "relationships": relationships,
        "fields": [_field(f, model, names, params) for f in ds.fields],
        "filters": [
            {"id": flt.id, "stage": flt.stage, "text": describe_filter(flt, fields)}
            for flt in ds.filters
        ],
    }


def describe_filter(flt: Filter, fields: dict[str, DataField]) -> str:
    """篩選的說明（例如「Region：只保留 East、West」）。"""
    if isinstance(flt, TupleFilter):
        names = "、".join(use_caption(u, fields) for u in flt.fields)
        if flt.mode == "all":
            return f"{names}：全部"
        verb = "只保留" if flt.mode == "include" else "排除"
        return f"{names}：{verb} {len(flt.tuples)} 組值"
    name = use_caption(flt.field, fields)
    if isinstance(flt, CategoricalFilter):
        if flt.mode == "all":
            return f"{name}：全部"
        values = [_value(v) for v in flt.values] + [
            f"{_value(r.start)}～{_value(r.end)}" for r in flt.ranges
        ]
        verb = "只保留" if flt.mode == "include" else "排除"
        shown = "、".join(values[:VALUES_SHOWN])
        more = f" 等 {len(values)} 個值" if len(values) > VALUES_SHOWN else ""
        return f"{name}：{verb} {shown or '（沒有值）'}{more}"
    if isinstance(flt, RangeFilter):
        if flt.min is not None and flt.max is not None:
            text = f"{_value(flt.min)} 到 {_value(flt.max)}"
        elif flt.min is not None:
            text = f"至少 {_value(flt.min)}"
        elif flt.max is not None:
            text = f"最多 {_value(flt.max)}"
        else:
            text = "全部"
        return f"{name}：{text}" + ("（含 Null）" if flt.include_null else "")
    if isinstance(flt, TopNFilter):
        direction = "前" if flt.direction == "top" else "後"
        count = flt.count if isinstance(flt.count, int) else f"參數 {flt.count.parameter}"
        return f"{name}：依 {flt.by} 的{direction} {count} 名"
    if isinstance(flt, ConditionFilter):
        return f"{name}：條件 {flt.condition}"
    assert isinstance(flt, RelativeDateFilter)
    unit = PERIODS.get(flt.period, flt.period)
    return f"{name}：相對日期（以{unit}計，{flt.first} 到 {flt.last}）"


def use_caption(use: FieldUse, fields: dict[str, DataField]) -> str:
    """欄位使用的名稱，與 Tableau 膠囊的寫法相同（YEAR(Order Date)、SUM(Sales)）。"""
    data_field = fields.get(use.field)
    name = data_field.caption if data_field is not None else use.field
    if use.date is not None:
        name = f"{use.date.unit.upper()}({name})"
    if use.agg is not None:
        name = f"{AGGREGATIONS.get(use.agg, use.agg.upper())}({name})"
    return name


def _model(ds: Datasource) -> ModelRelation | None:
    connection = ds.connection
    if isinstance(connection, ExtractConnection):
        return connection.model
    relation = connection.relation
    return relation if isinstance(relation, ModelRelation) else None


def _single(ds: Datasource) -> list[dict[str, Any]]:
    """單一資料表或 join 樹：一張（實體的資料表組成的）資料表。"""
    connection = ds.connection
    if isinstance(connection, ExtractConnection):
        source = connection.source.relation if connection.source is not None else None
        if source is not None and not isinstance(source, ModelRelation):
            return [
                {
                    "id": None,
                    "caption": ds.caption,
                    "physical": _physical(source),
                    "joins": _joins(source),
                }
            ]
        return [
            {
                "id": None,
                "caption": ds.caption,
                "physical": [{"name": connection.table, "source": connection.table, "sql": None}],
                "joins": [],
            }
        ]
    relation = connection.relation
    if relation is None or isinstance(relation, ModelRelation):
        return []
    return [
        {
            "id": None,
            "caption": ds.caption,
            "physical": _physical(relation),
            "joins": _joins(relation),
        }
    ]


def _physical(relation: Relation) -> list[dict[str, Any]]:
    """join 樹中的實體資料表與自訂 SQL。"""
    if isinstance(relation, JoinRelation):
        return _physical(relation.left) + _physical(relation.right)
    if isinstance(relation, TableRelation):
        return [{"name": relation.name, "source": ".".join(relation.table), "sql": None}]
    assert isinstance(relation, SqlRelation)
    return [{"name": relation.name, "source": "自訂 SQL", "sql": relation.sql}]


def _joins(relation: Relation) -> list[dict[str, Any]]:
    """join 樹中的每個 join：類型、兩邊的資料表與條件。"""
    if not isinstance(relation, JoinRelation):
        return []
    left = [t["name"] for t in _physical(relation.left)]
    right = [t["name"] for t in _physical(relation.right)]
    own = {
        "type": relation.join,
        "label": f"{JOINS[relation.join]} join",
        "left": left,
        "right": right,
        "on": [f"{_term(c.left)} {c.op} {_term(c.right)}" for c in relation.on],
    }
    return [*_joins(relation.left), *_joins(relation.right), own]


def _term(term: JoinTerm) -> str:
    if term.table is not None and term.column is not None:
        return f"{term.table}.{term.column}"
    return _value(term.literal)


def _connection(connection: ExtractConnection | LiveConnection) -> dict[str, Any]:
    if isinstance(connection, ExtractConnection):
        refresh = connection.refresh
        return {
            "kind": "extract",
            "type": DATABASES["hyper"],
            "file": connection.hyper_file,
            "table": connection.table,
            "source": _live(connection.source) if connection.source is not None else None,
            "refresh": None
            if refresh is None
            else ("增量更新" if refresh.increment is not None else "完整更新"),
            "row_limit": refresh.row_limit if refresh is not None else None,
        }
    return _live(connection)


def _live(connection: LiveConnection) -> dict[str, Any]:
    return {
        "kind": "live",
        "type": DATABASES.get(connection.dbclass, connection.dbclass),
        "dbclass": connection.dbclass,
        "connection_id": connection.connection_id,
        "server": connection.server,
        "port": connection.port,
        "database": connection.database,
        "schema": connection.schema_name,
        "http_path": connection.http_path,
        "authentication": connection.authentication,
        "supported": connection.dialect is not None,
    }


def readable_formula(formula: str, names: dict[str, str], params: dict[str, str]) -> str:
    """公式中的內部名稱換成顯示名稱（[Calculation_123] → [利潤率]、[Parameters].[Parameter 1] →
    [Top N]）；字串中的文字與找不到的名稱不變。"""
    parts = re.split(r"(\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*')", formula)

    def name(match: re.Match[str]) -> str:
        lookup = params if match.group(1) else names
        caption = lookup.get(f"[{match.group(2)}]")
        if caption is None:
            return match.group(0)
        return "[" + caption.replace("]", "]]") + "]"

    return "".join(p if i % 2 else NAME.sub(name, p) for i, p in enumerate(parts))


def _field(
    data_field: DataField,
    model: ModelRelation | None,
    names: dict[str, str],
    params: dict[str, str],
) -> dict[str, Any]:
    kind = (
        "set" if data_field.set is not None else "calc" if data_field.calc is not None else "column"
    )
    table = data_field.table
    if model is not None and table is not None:
        table = _table_caption(model, table)
    return {
        "id": data_field.id,
        "caption": data_field.caption,
        "name": data_field.name,
        "role": data_field.role,
        "datatype": data_field.datatype,
        "kind": kind,
        "table": table,
        "column": data_field.column,
        "formula": readable_formula(data_field.calc.formula, names, params)
        if data_field.calc is not None
        else None,
        "default_agg": data_field.default_agg,
        "format": data_field.format,
        "hidden": data_field.hidden,
        "aliases": len(data_field.aliases),
    }


def _table_caption(model: ModelRelation, table_id: str) -> str:
    return next((t.caption for t in model.tables if t.id == table_id), table_id)


def _caption(fields: dict[str, DataField], field_id: str) -> str:
    data_field = fields.get(field_id)
    return data_field.caption if data_field is not None else field_id


def _value(value: Scalar) -> str:
    if value is None:
        return "Null"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


__all__ = ["datasource_summary", "describe_filter", "readable_formula", "use_caption"]
