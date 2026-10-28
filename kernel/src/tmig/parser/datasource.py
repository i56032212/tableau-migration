"""資料源、欄位、計算欄位、資料源篩選與參數（A2、A3）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from lxml import etree

from tmig.formula.analyze import Analyzer
from tmig.formula.ast import FieldRef, to_json
from tmig.formula.types import FormulaSemanticError
from tmig.ir.models import (
    Aggregation,
    Allowed,
    AllowedAll,
    AllowedList,
    AllowedRange,
    Calc,
    CalcLevel,
    DataField,
    ExtractConnection,
    FieldUse,
    Parameter,
    Unsupported,
    WeekDay,
)
from tmig.ir.scope import DatasourceScope
from tmig.parser.context import DATATYPES, DsState, ParseContext
from tmig.parser.fields import FilterParser, has_aliases, instance_key, use_from_instance
from tmig.parser.ids import strip_brackets
from tmig.parser.refs import qualified_parts
from tmig.parser.twbx import child, children, descendants, local_name, text_of, xpath
from tmig.parser.values import LiteralError, parse_literal

METADATA_AGG: dict[str, Aggregation] = {
    "Sum": "sum",
    "Avg": "avg",
    "Min": "min",
    "Max": "max",
    "Count": "count",
    "CountD": "countd",
    "Median": "median",
    "Attr": "attr",
    "Attribute": "attr",
    "Stdev": "stdev",
    "StdevP": "stdevp",
    "Var": "var",
    "VarP": "varp",
}
WEEK_DAYS: tuple[WeekDay, ...] = (
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
)
DS_CHILDREN = frozenset(
    {
        "column",
        "column-instance",
        "folder",
        "folders-common",
        "aliases",
        "connection",
        "layout",
        "semantic-values",
        "group",
        "style",
        "datasource-dependencies",
        "object-graph",
        "filter",
        "repository-location",
        "extract",
        "drill-paths",
        "default-sorts",
        "date-options",
        "field-sort-info",
    }
)
TABLE_ID_PREFIX = "[__tableau_internal_object_id__]."
"""物件模型中每張邏輯資料表的筆數欄位（Tableau 2020.2 起的「資料表 (計數)」）。"""


@dataclass(frozen=True)
class MetaColumn:
    element: etree._Element
    local: str
    remote: str
    local_type: str
    aggregation: str


# ---------------------------------------------------------------- 參數


def parse_parameters(ctx: ParseContext, ds_el: etree._Element) -> None:
    for column in children(ds_el, "column"):
        name = column.get("name", "")
        caption = column.get("caption") or strip_brackets(name)
        datatype = column.get("datatype", "")
        if datatype not in DATATYPES:
            ctx.flag(column, f"參數「{caption}」的型別 {datatype} v0 不支援")
            continue
        domain = column.get("param-domain-type", "any")
        try:
            value_text = column.get("value")
            if value_text is None:
                calc = child(column, "calculation")
                value_text = calc.get("formula") if calc is not None else None
            value = parse_literal(value_text, datatype)
            allowed = _allowed(column, domain, datatype)
        except LiteralError as exc:
            ctx.flag(column, f"參數「{caption}」的值無法解析：{exc}")
            continue
        if allowed is None:
            ctx.flag(column, f"參數「{caption}」的允許值類型 {domain} v0 不支援")
            continue
        param = Parameter(
            id=ctx.ids.make("p", caption, name),
            name=name,
            caption=caption,
            datatype=datatype,  # type: ignore[arg-type]
            allowed=allowed,
            value=value,
            source_ref=xpath(column),
        )
        ctx.parameters.append(param)
        ctx.params_by_name[name] = param


def _allowed(column: etree._Element, domain: str, datatype: str) -> Allowed | None:
    if domain == "any":
        return AllowedAll(kind="all")
    if domain == "range":
        rng = child(column, "range")
        if rng is None:
            return AllowedAll(kind="all")
        step_text = rng.get("granularity")
        step: int | float | None = None
        if step_text and datatype in ("integer", "real"):
            number = parse_literal(step_text, "real")
            assert isinstance(number, float)
            step = int(number) if number.is_integer() and datatype == "integer" else number
        return AllowedRange(
            kind="range",
            min=parse_literal(rng.get("min"), datatype) if rng.get("min") else None,
            max=parse_literal(rng.get("max"), datatype) if rng.get("max") else None,
            step=step,
        )
    if domain == "list":
        values = [
            parse_literal(m.get("value"), datatype)
            for m in descendants(child(column, "members"), "member")
        ]
        return AllowedList(kind="list", values=values) if values else None
    return None


# ---------------------------------------------------------------- 資料源


class DatasourceParser:
    def __init__(self, ctx: ParseContext, ds_el: etree._Element) -> None:
        self.ctx = ctx
        self.el = ds_el
        self.name = ds_el.get("name", "")
        self.caption = ds_el.get("caption") or self.name

    def parse(self) -> DsState | None:
        ctx, el = self.ctx, self.el
        conn = child(el, "connection")
        if conn is None:
            ctx.flag(el, f"資料源「{self.caption}」沒有連線設定")
            return None
        placeholder = ExtractConnection(kind="extract", hyper_file="(無)", table="(無)")
        published = conn.get("class") == "sqlproxy"
        if published:
            ctx.flag(el, f"資料源「{self.caption}」是 Published Data Source，v0 不支援")
            if not ctx.lenient:
                return None
        located = None if published else self._data_connection(conn)
        connection: ExtractConnection | None
        if located is None:
            if not ctx.lenient:
                return None
            data_conn, via_extract, connection = conn, False, placeholder
        else:
            data_conn, via_extract = located
            connection = self._connection(data_conn, conn, via_extract)
            if connection is None:
                if not ctx.lenient:
                    return None
                connection = placeholder
        graph = child(el, "object-graph")
        objects = children(child(graph, "objects"), "object")
        if len(objects) > 1:
            ctx.flag(
                graph if graph is not None else el,
                f"資料源「{self.caption}」是 Relationship 模型（{len(objects)} 張邏輯資料表），v0 不支援",
            )
            if not ctx.lenient:
                return None
        if via_extract and any(rel.get("type") == "text" for rel in descendants(conn, "relation")):
            ctx.ignore(conn, "來源為自訂 SQL；本階段以萃取快照驗證，不影響數字")

        state = DsState(
            element=el,
            name=self.name,
            id=ctx.ids.make("ds", self.caption, self.name),
            caption=self.caption,
            connection=connection,
            week_start=self._week_start(),
        )
        self._fields(state, conn, data_conn, via_extract)
        if not state.fields:
            ctx.flag(el, f"資料源「{self.caption}」沒有可用的欄位")
            return None
        analyze_calcs(ctx, state)
        state.instances = {
            instance_key(inst.get("name", "")): inst for inst in children(el, "column-instance")
        }
        self._filters(state)
        self._check_children()
        return state

    # ------------------------------------------------------------ 連線

    def _data_connection(self, conn: etree._Element) -> tuple[etree._Element, bool] | None:
        """找出指向 .hyper 的連線：嵌入式萃取，或直接以 .hyper 為來源。"""
        extract = child(self.el, "extract")
        if extract is not None and extract.get("enabled") != "false":
            data_conn = child(extract, "connection")
            if data_conn is None:
                self.ctx.flag(extract, f"資料源「{self.caption}」的萃取沒有連線設定")
                return None
            return data_conn, True
        named = [
            c for nc in descendants(conn, "named-connection") for c in children(nc, "connection")
        ]
        if conn.get("class") in ("hyper", "dataengine"):
            return conn, False
        if len(named) == 1 and named[0].get("class") in ("hyper", "dataengine"):
            return named[0], False
        reason = "萃取已停用（使用 Live 連線）" if extract is not None else "是 Live 連線"
        self.ctx.flag(
            self.el, f"資料源「{self.caption}」{reason}，資料不在 .twbx 內；v0 只支援嵌入式萃取"
        )
        return None

    def _connection(
        self, data_conn: etree._Element, conn: etree._Element, via_extract: bool
    ) -> ExtractConnection | None:
        ctx = self.ctx
        if data_conn.get("class") == "dataengine":
            ctx.flag(
                data_conn,
                f"資料源「{self.caption}」的萃取是舊版 .tde，需先在 Tableau 升級為 .hyper",
            )
            return None
        if data_conn.get("class") != "hyper":
            ctx.flag(
                data_conn, f"資料源「{self.caption}」的萃取連線類型 {data_conn.get('class')} 不支援"
            )
            return None
        dbname = data_conn.get("dbname", "")
        hyper_file = self._packaged(dbname)
        if hyper_file is None:
            ctx.flag(data_conn, f"資料源「{self.caption}」的萃取檔「{dbname}」不在 .twbx 內")
            return None
        relations = children(data_conn if via_extract else conn, "relation")
        tables = [r.get("table", "") for r in relations if r.get("type") == "table"]
        if len(relations) != 1 or len(tables) != 1:
            ctx.flag(
                data_conn,
                f"資料源「{self.caption}」的萃取不是單一資料表（多表萃取或資料表關聯），v0 不支援",
            )
            return None
        table = ".".join(qualified_parts(tables[0]))
        return ExtractConnection(kind="extract", hyper_file=hyper_file, table=table)

    def _packaged(self, dbname: str) -> str | None:
        path = dbname.replace("\\", "/")
        package = self.ctx.package
        if package.source.suffix.lower() != ".twbx":
            # .twb 不含資料，只能沿用原路徑
            return path or None
        names = {f.name.replace("\\", "/"): f.name for f in package.files}
        stripped = path.lstrip("./")
        if stripped in names:
            return names[stripped].replace("\\", "/")
        base = PurePosixPath(stripped).name.lower()
        matches = [n for n in names if PurePosixPath(n).name.lower() == base]
        if len(matches) == 1:
            return matches[0]
        if self.ctx.extracts_omitted:
            # 下載時刻意不含萃取檔：沿用原路徑，只盤點結構
            return stripped or None
        return None

    def _week_start(self) -> WeekDay:
        options = child(self.el, "date-options")
        value = (options.get("start-of-week") if options is not None else None) or "sunday"
        value = value.lower()
        return value if value in WEEK_DAYS else "sunday"  # type: ignore[return-value]

    # ------------------------------------------------------------ 欄位

    def _meta_columns(self, conn: etree._Element | None) -> list[MetaColumn]:
        result: list[MetaColumn] = []
        for record in children(child(conn, "metadata-records"), "metadata-record"):
            if record.get("class") != "column":
                continue
            local = text_of(child(record, "local-name"))
            if not local:
                continue
            result.append(
                MetaColumn(
                    element=record,
                    local=local,
                    remote=text_of(child(record, "remote-name")) or strip_brackets(local),
                    local_type=text_of(child(record, "local-type")),
                    aggregation=text_of(child(record, "aggregation")),
                )
            )
        return result

    def _fields(
        self,
        state: DsState,
        conn: etree._Element,
        data_conn: etree._Element,
        via_extract: bool,
    ) -> None:
        ctx = self.ctx
        extract_records = self._meta_columns(data_conn) if via_extract else []
        records = extract_records or self._meta_columns(conn)
        physical: dict[str, str] = {}
        if via_extract:
            for mapping in children(child(data_conn, "cols"), "map"):
                parts = qualified_parts(mapping.get("value", ""))
                if parts:
                    physical[mapping.get("key", "")] = parts[-1]
            if not physical:
                physical = {r.local: r.remote for r in extract_records}
        else:
            physical = {r.local: r.remote for r in records}

        columns = {c.get("name", ""): c for c in children(self.el, "column")}
        seen: set[str] = set()
        for record in records:
            if record.local in seen:
                continue
            seen.add(record.local)
            column = columns.get(record.local)
            if column is not None and child(column, "calculation") is not None:
                continue  # 實體化的計算欄位（例如 Number of Records）以公式為準
            datatype = (column.get("datatype") if column is not None else None) or record.local_type
            self._add_raw(state, record.local, datatype, column, record, physical)

        for name, column in columns.items():
            if name in seen and child(column, "calculation") is None:
                continue
            if child(column, "calculation") is not None:
                self._add_calc(state, column)
                continue
            if column.get("datatype") == "table" or name.startswith(TABLE_ID_PREFIX):
                self._add_table_count(state, name, column.get("caption"), column)
                continue
            if name.startswith("[:"):
                continue  # Measure Names 等虛擬欄位
            if not records:
                # 沒有 metadata-records 的舊檔：以 <column> 為準
                self._add_raw(state, name, column.get("datatype", ""), column, None, physical)
                continue
            ctx.flag(column, f"欄位 {name} 找不到資料來源")

        # 物件模型的邏輯資料表：沒有明寫 datatype='table' 欄位時，依 object-graph 補上筆數欄位
        graph = child(self.el, "object-graph")
        for obj in children(child(graph, "objects"), "object"):
            object_id = obj.get("id")
            if object_id:
                name = f"{TABLE_ID_PREFIX}[{object_id.replace(']', ']]')}]"
                self._add_table_count(state, name, obj.get("caption"), obj)

        for group in children(self.el, "group"):
            ctx.ignore(group, "群組或集合（工作表若有使用會另列為不支援）")

    def _add_raw(
        self,
        state: DsState,
        name: str,
        datatype: str,
        column: etree._Element | None,
        record: MetaColumn | None,
        physical: dict[str, str],
    ) -> None:
        anchor = column if column is not None else record.element if record is not None else self.el
        caption = (column.get("caption") if column is not None else None) or strip_brackets(name)
        if datatype not in DATATYPES:
            self.ctx.flag(anchor, f"欄位「{caption}」的型別 {datatype} v0 不支援")
            return
        if has_aliases(column):
            self.ctx.flag(anchor, f"欄位「{caption}」設有成員別名，會改變顯示值，v0 不支援")
        role = (column.get("role") if column is not None else None) or (
            "measure" if datatype in ("integer", "real") else "dimension"
        )
        agg_text = (column.get("aggregation") if column is not None else None) or (
            record.aggregation if record is not None else ""
        )
        default_agg = METADATA_AGG.get(agg_text) if role == "measure" else None
        state.add_field(
            DataField(
                id=self.ctx.ids.make("f", caption, name),
                name=name,
                caption=caption,
                role=role,  # type: ignore[arg-type]
                datatype=datatype,  # type: ignore[arg-type]
                default_agg=default_agg,
                format=column.get("default-format") if column is not None else None,
                hidden=column is not None and column.get("hidden") == "true",
                column=physical.get(name, strip_brackets(name)),
                calc=None,
                source_ref=xpath(anchor),
            )
        )

    def _add_table_count(
        self, state: DsState, name: str, caption: str | None, anchor: etree._Element
    ) -> None:
        """資料表的筆數欄位：以公式 1 表示，COUNT 後即為列數（單表萃取）。"""
        if name in state.by_name:
            return
        label = caption or strip_brackets(name.removeprefix(TABLE_ID_PREFIX))
        state.add_field(
            DataField(
                id=self.ctx.ids.make("f", f"{label} count", name),
                name=name,
                caption=label,
                role="measure",
                datatype="integer",
                default_agg="count",
                format=None,
                hidden=False,
                column=None,
                calc=Calc(formula="1"),
                source_ref=xpath(anchor),
            )
        )

    def _add_calc(self, state: DsState, column: etree._Element) -> None:
        add_calc_field(self.ctx, state, column)

    # ------------------------------------------------------------ 篩選

    def _filters(self, state: DsState) -> None:
        def resolve(ref: str) -> FieldUse | str:
            key = instance_key(ref)
            inst = state.instances.get(key)
            if inst is not None:
                return use_from_instance(state, inst)
            data_field = state.by_name.get(key)
            if data_field is None:
                return f"找不到 {ref}"
            return FieldUse(field=data_field.id)

        ds_filters = FilterParser(self.ctx, state, resolve, "datasource")
        for flt in children(self.el, "filter"):
            parsed = ds_filters.parse(flt)
            if parsed is not None:
                state.filters.append(parsed)
        extract = child(self.el, "extract")
        if extract is not None:
            extract_filters = FilterParser(self.ctx, state, resolve, "extract")
            for flt in descendants(extract, "filter"):
                parsed = extract_filters.parse(flt)
                if parsed is not None:
                    state.filters.append(parsed)

    def _check_children(self) -> None:
        for item in children(self.el):
            name = local_name(item)
            if name not in DS_CHILDREN:
                self.ctx.flag(item, f"資料源中的未知元素 <{name}>")


def add_calc_field(ctx: ParseContext, state: DsState, column: etree._Element) -> DataField | None:
    name = column.get("name", "")
    caption = column.get("caption") or strip_brackets(name)
    calc = child(column, "calculation")
    assert calc is not None
    calc_class = calc.get("class", "")
    if calc_class != "tableau":
        ctx.flag(column, f"計算欄位「{caption}」的類型 {calc_class}（群組、分組或集合）v0 不支援")
        return None
    formula = calc.get("formula")
    if not formula:
        ctx.flag(column, f"計算欄位「{caption}」沒有公式")
        return None
    datatype = column.get("datatype", "")
    if datatype not in DATATYPES:
        ctx.flag(column, f"計算欄位「{caption}」的型別 {datatype} v0 不支援")
        return None
    if has_aliases(column):
        ctx.flag(column, f"計算欄位「{caption}」設有成員別名，會改變顯示值，v0 不支援")
    role = column.get("role") or ("measure" if datatype in ("integer", "real") else "dimension")
    agg_text = column.get("aggregation", "")
    data_field = DataField(
        id=ctx.ids.make("f", caption, name),
        name=name,
        caption=caption,
        role=role,  # type: ignore[arg-type]
        datatype=datatype,  # type: ignore[arg-type]
        default_agg=METADATA_AGG.get(agg_text) if role == "measure" else None,
        format=column.get("default-format"),
        hidden=column.get("hidden") == "true",
        column=None,
        calc=Calc(formula=formula),
        source_ref=xpath(column),
    )
    state.add_field(data_field)
    return data_field


def _level(info_level: str, has_lod: bool) -> CalcLevel:
    if info_level == "tc":
        return "table_calc"
    if info_level == "agg":
        return "aggregate"
    return "lod" if has_lod else "row"


def analyze_calcs(ctx: ParseContext, state: DsState) -> None:
    """以公式引擎分析計算欄位：補上語法樹（附解析出的 id）與計算層級。"""
    scope = DatasourceScope(state.name, state.fields, ctx.parameters)
    analyzer = Analyzer(scope)
    state.analyzer = analyzer
    for data_field in state.fields:
        if data_field.calc is None or data_field.id in state.levels:
            continue
        target = scope.target(data_field.id)
        try:
            info = analyzer.target_info(target)
            node = analyzer.calc_ast(target)
        except FormulaSemanticError as exc:
            state.levels[data_field.id] = None
            ctx.unsupported.append(
                Unsupported(
                    source_ref=data_field.source_ref,
                    reason=f"計算欄位「{data_field.caption}」：{exc.message}"
                    + ("（v0 尚不支援）" if exc.unsupported else ""),
                    element="column",
                    xml=data_field.calc.formula,
                )
            )
            continue

        def resolve(ref: FieldRef) -> dict[str, str] | None:
            found = analyzer.resolve(ref)
            return {"target": found.kind, "id": found.id}

        level = _level(info.level, info.has_lod)
        state.levels[data_field.id] = level
        data_field.calc = Calc(
            formula=data_field.calc.formula, ast=to_json(node, resolve), level=level
        )
