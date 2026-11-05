"""資料源、欄位、計算欄位、資料源篩選與參數（A2、A3）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from lxml import etree

from tmig.formula.analyze import Analyzer
from tmig.formula.ast import FieldRef, to_json
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.ir.models import (
    Aggregation,
    Allowed,
    AllowedAll,
    AllowedList,
    AllowedRange,
    Calc,
    CalcLevel,
    Connection,
    DataField,
    ExtractConnection,
    ExtractRefresh,
    FieldUse,
    LiveConnection,
    LogicalTable,
    MemberAlias,
    ModelRelation,
    Parameter,
    PublishedSource,
    SourceColumn,
    TableRelation,
    Unsupported,
    WeekDay,
)
from tmig.ir.scope import DatasourceScope
from tmig.parser.connections import DIALECTS, live_connection, parse_relation
from tmig.parser.context import DATATYPES, DsState, ParseContext
from tmig.parser.fields import (
    FilterParser,
    instance_key,
    member_aliases,
    use_from_instance,
    use_from_name,
)
from tmig.parser.groups import (
    GroupError,
    bin_formula,
    group_formula,
    identity_set_formula,
    identity_set_level,
)
from tmig.parser.ids import strip_brackets
from tmig.parser.model import (
    extract_tables,
    graph_objects,
    live_tables,
    object_id_of,
    relationships,
)
from tmig.parser.refs import qualified_parts
from tmig.parser.sets import IN_OUT, Level, level_expr, parse_set
from tmig.parser.twbx import attr, child, children, descendants, local_name, text_of, xpath
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
    family: str = ""
    """萃取的 metadata-record：來源關聯樹中的資料表。"""
    alias: str = ""
    """萃取的 metadata-record：來源的欄名（remote-alias）。"""


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
            aliases=_parameter_aliases(ctx, column, datatype, caption),
            source_ref=xpath(column),
        )
        ctx.parameters.append(param)
        ctx.params_by_name[name] = param


def _parameter_aliases(
    ctx: ParseContext, column: etree._Element, datatype: str, caption: str
) -> list[MemberAlias]:
    """參數值的顯示名稱：<aliases>，或清單中各 <member alias=…>（兩者 Tableau 都會寫，內容相同）。"""
    aliases = member_aliases(ctx, column, datatype, f"參數「{caption}」")
    if aliases:
        return aliases
    result: list[MemberAlias] = []
    for member in descendants(child(column, "members"), "member"):
        label = member.get("alias")
        if label is None:
            continue
        try:
            result.append(
                MemberAlias(value=parse_literal(member.get("value"), datatype), label=label)
            )
        except LiteralError as exc:
            ctx.flag(member, f"參數「{caption}」的別名「{member.get('value')}」無法解析：{exc}")
    return result


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
        self.objects = graph_objects(ds_el)
        self.model_tables: list[LogicalTable] | None = None
        """Relationship 模型的邏輯資料表（N6）；不是模型時為 None。"""

    def parse(self) -> DsState | None:
        ctx, el = self.ctx, self.el
        conn = child(el, "connection")
        if conn is None:
            ctx.flag(el, f"資料源「{self.caption}」沒有連線設定")
            return None
        placeholder = ExtractConnection(kind="extract", hyper_file="(無)", table="(無)")
        published = conn.get("class") == "sqlproxy"
        if published:
            ctx.flag(
                el,
                f"資料源「{self.caption}」是 Published Data Source，需先下載 .tdsx 併入報表"
                "（tools/tableau_server fetch 會自動下載併入，或 tmig published merge）",
            )
            if not ctx.lenient:
                return None
        located = None if published else self._data_connection(conn)
        connection: Connection | None
        if located is None:
            live = None if published else self._live(conn)
            if live is None and not ctx.lenient:
                return None
            data_conn, via_extract, connection = conn, False, live or placeholder
        else:
            data_conn, via_extract = located
            connection = self._connection(data_conn, conn, via_extract)
            if connection is None:
                if not ctx.lenient:
                    return None
                connection = placeholder
        if via_extract and any(rel.get("type") == "text" for rel in descendants(conn, "relation")):
            ctx.ignore(conn, "來源為自訂 SQL；本階段以萃取快照驗證，不影響數字")

        state = DsState(
            element=el,
            name=self.name,
            id=ctx.ids.make("ds", self.caption, self.name),
            caption=self.caption,
            connection=connection,
            week_start=self._week_start(),
            published=None if published else self._published(),
        )
        self._fields(state, conn, data_conn, via_extract)
        if not state.fields:
            ctx.flag(el, f"資料源「{self.caption}」沒有可用的欄位")
            return None
        if self.model_tables is not None and not self._attach_model(state):
            return None
        if via_extract:
            self._attach_refresh(state, conn, data_conn)
        analyze_calcs(ctx, state)
        state.instances = {
            instance_key(inst.get("name", "")): inst for inst in children(el, "column-instance")
        }
        self._filters(state)
        self._check_children()
        return state

    # ------------------------------------------------------------ 連線

    def _published(self) -> PublishedSource | None:
        """併入報表的 Published Data Source（N8）：repository-location 指向伺服器上的資料源。"""
        location = child(self.el, "repository-location")
        if location is None or not (location.get("path") or "").rstrip("/").endswith("datasources"):
            return None
        content_url = location.get("id") or ""
        if not content_url:
            return None
        return PublishedSource(
            content_url=content_url,
            site=location.get("site") or None,
            revision=location.get("revision") or None,
        )

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
        return None

    def _live(self, conn: etree._Element) -> LiveConnection | None:
        """Live 連線（N1）：資料在來源資料庫，查詢時下推；只支援單一具名連線（不跨資料庫）。"""
        named = children(child(conn, "named-connections"), "named-connection")
        data = [c for nc in named for c in children(nc, "connection")]
        if not data and conn.get("class") not in (None, "federated"):
            data = [conn]  # 舊版檔案：連線直接寫在 <connection> 上
        if len(data) != 1:
            self.ctx.flag(
                conn,
                f"資料源「{self.caption}」有 {len(data)} 個具名連線（跨資料庫的關聯）尚不支援",
            )
            return None
        if len(self.objects) > 1:
            # Relationship 模型：邏輯資料表在 object-graph，關聯待欄位解析後補上
            tables = live_tables(self.ctx, self.objects, self.caption)
            if tables is None:
                return None
            self.model_tables = tables
            return live_connection(self.ctx, data[0], None, self.caption)
        relations = children(conn, "relation")
        relation = relations[0] if len(relations) == 1 else None
        if len(relations) > 1:
            self.ctx.flag(conn, f"資料源「{self.caption}」有多個資料表關聯樹，尚不支援")
            return None
        return live_connection(self.ctx, data[0], relation, self.caption)

    def _extract_source(self, conn: etree._Element) -> LiveConnection | None:
        """萃取的來源資料庫（萃取層更新時使用）；解析不了時不影響萃取本身。"""
        named = [
            c for nc in descendants(conn, "named-connection") for c in children(nc, "connection")
        ]
        relations = children(conn, "relation")
        if len(named) != 1 or len(relations) != 1 or named[0].get("class") not in DIALECTS:
            return None
        probe = ParseContext(self.ctx.package, lenient=True)
        return live_connection(probe, named[0], relations[0], self.caption)

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
        if len(self.objects) > 1:
            # Relationship 模型的萃取：每張邏輯資料表一張 .hyper 資料表
            model_tables = extract_tables(ctx, self.objects, self.caption)
            if model_tables is None:
                return None
            self.model_tables = model_tables
            first = model_tables[0].relation
            assert isinstance(first, TableRelation)
            return ExtractConnection(
                kind="extract", hyper_file=hyper_file, table=".".join(first.table)
            )
        relations = children(data_conn if via_extract else conn, "relation")
        tables = [r.get("table", "") for r in relations if r.get("type") == "table"]
        if len(relations) != 1 or len(tables) != 1:
            ctx.flag(
                data_conn,
                f"資料源「{self.caption}」的萃取不是單一資料表（多表萃取或資料表關聯），v0 不支援",
            )
            return None
        table = ".".join(qualified_parts(tables[0]))
        source = self._extract_source(conn) if via_extract else None
        return ExtractConnection(kind="extract", hyper_file=hyper_file, table=table, source=source)

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
                    family=text_of(child(record, "family")),
                    alias=text_of(child(record, "remote-alias")),
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
            auto = attr(group, "auto-column")
            if auto in ("sheet_link", "include", "exclude"):
                # 篩選動作、工具提示中的工作表、排除與只保留：工作表上以多欄位篩選表示
                kind = "vit" if attr(group, "ui-vit-column") == "true" else auto
                state.auto_groups[group.get("name", "")] = kind
                continue
            if attr(group, "ui-builder") == "identity-set":
                self._add_identity_set(state, group)
                continue
            if attr(group, "ui-builder") == "filter-group":
                self._add_set(state, group)
                continue
            ctx.ignore(group, "合併欄位等群組（工作表若有使用會另列為不支援）")

    def _add_set(self, state: DsState, group: etree._Element) -> None:
        """集合 → 布林欄位（In／Out）：固定成員轉成公式，Top N 與條件集合在查詢時計算成員。"""
        name = group.get("name", "")
        caption = group.get("caption") or strip_brackets(name)
        try:
            field_set, formula = parse_set(
                group, lambda level: self._set_level(state, level), self.ctx.parameter
            )
        except (GroupError, LiteralError) as exc:
            self.ctx.flag(group, f"集合「{caption}」：{exc}")
            return
        data_field = DataField(
            id=self.ctx.ids.make("f", caption, name),
            name=name,
            caption=caption,
            role="dimension",
            datatype="boolean",
            column=None,
            calc=Calc(formula=formula) if formula is not None else None,
            set=field_set,
            aliases=list(IN_OUT),
            source_ref=xpath(group),
        )
        state.add_field(data_field)
        state.set_fields.add(data_field.id)

    def _set_level(self, state: DsState, name: str) -> Level:
        """集合的層級（[State]、[none:State:nk]、[yr:Order Date:ok]）。"""
        data_field = state.by_name.get(name)
        if data_field is not None:
            use = FieldUse(field=data_field.id)
        else:
            found = use_from_name(state, name)
            if isinstance(found, str):
                raise GroupError(f"層級 {name}：{found}")
            if found.agg is not None:
                raise GroupError(f"層級 {name} 不能是彙總")
            use = FieldUse(field=found.field, date=found.date)
            data_field = state.field_by_id(use.field)
        if data_field.set is not None:
            raise GroupError(f"層級 {name} 是集合")
        if data_field.datatype not in DATATYPES:
            raise GroupError(f"層級 {name} 的型別 {data_field.datatype} 不支援")
        part = use.date is not None and use.date.kind == "part"
        return Level(
            use=use,
            expr=level_expr(data_field.name, use),
            datatype="integer" if part else data_field.datatype,
        )

    def _add_identity_set(self, state: DsState, group: etree._Element) -> None:
        """Tableau 的使用者篩選（集合）→ 布林計算欄位：登入者看得到的成員為 TRUE（P1 列層級權限）。"""
        name = group.get("name", "")
        caption = group.get("caption") or strip_brackets(name)
        try:
            level = identity_set_level(group)
            datatype = _source_datatype(state, level)
            if datatype is None:
                raise GroupError(f"找不到層級欄位 {level}")
            formula = identity_set_formula(group, datatype)
        except (GroupError, LiteralError) as exc:
            self.ctx.flag(group, f"使用者篩選「{caption}」：{exc}")
            return
        data_field = DataField(
            id=self.ctx.ids.make("f", caption, name),
            name=name,
            caption=caption,
            role="dimension",
            datatype="boolean",
            column=None,
            calc=Calc(formula=formula),
            source_ref=xpath(group),
        )
        state.add_field(data_field)
        state.identity_sets.add(data_field.id)
        state.set_fields.add(data_field.id)

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
        aliases = member_aliases(self.ctx, column, datatype, f"欄位「{caption}」")
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
                table=self._field_table(state, record),
                calc=None,
                aliases=aliases,
                source_ref=xpath(anchor),
            )
        )

    def _field_table(self, state: DsState, record: MetaColumn | None) -> str | None:
        """欄位所屬的資料表：Live 連線為 metadata-record 的 parent-name；
        Relationship 模型為邏輯資料表（object id）。"""
        if record is None:
            return None
        if self.model_tables is not None:
            return object_id_of(record.element)
        if state.connection.kind != "live":
            return None
        parent = text_of(child(record.element, "parent-name"))
        return strip_brackets(parent) or None

    def _attach_model(self, state: DsState) -> bool:
        """欄位解析完成後組成 Relationship 模型；不支援時列入 unsupported。"""
        assert self.model_tables is not None
        ids = {t.id for t in self.model_tables}
        rels = relationships(self.ctx, self.el, state, ids, self.caption)
        if rels is None:
            return self.ctx.lenient
        stray = [f.caption for f in state.fields if f.column is not None and f.table not in ids]
        if stray:
            self.ctx.flag(
                self.el,
                f"資料源「{self.caption}」的欄位 {'、'.join(stray[:5])} 不屬於任何邏輯資料表",
            )
            return self.ctx.lenient
        model = ModelRelation(kind="model", tables=self.model_tables, relationships=rels)
        connection = state.connection
        if isinstance(connection, ExtractConnection):
            state.connection = connection.model_copy(update={"model": model})
        else:
            state.connection = connection.model_copy(update={"relation": model})
        return True

    def _attach_refresh(
        self, state: DsState, conn: etree._Element, data_conn: etree._Element
    ) -> None:
        """萃取的來源與各欄在來源的位置（萃取層更新用，N7）；解析不了時不影響萃取本身。"""
        connection = state.connection
        if not isinstance(connection, ExtractConnection):
            return
        source = connection.source
        if connection.model is not None:
            source = self._model_source(conn, connection.model)
        if source is None or source.dialect is None:
            return
        records = {r.local: r for r in self._meta_columns(data_conn)}
        columns: list[SourceColumn] = []
        for data_field in state.fields:
            if data_field.column is None or data_field.calc is not None:
                continue
            record = records.get(data_field.name)
            if record is None:
                return  # 萃取中找不到這一欄的來源
            columns.append(
                SourceColumn(
                    field=data_field.id,
                    relation=record.family or None,
                    column=record.alias or record.remote,
                )
            )
        if not columns:
            return
        refresh_el = child(data_conn, "refresh")
        increment = None
        if refresh_el is not None and refresh_el.get("incremental-updates") == "true":
            key = state.by_name.get(refresh_el.get("increment-key", ""))
            increment = key.id if key is not None and key.column is not None else None
        extract = child(self.el, "extract")
        count = extract.get("count", "-1") if extract is not None else "-1"
        limit = int(count) if count.lstrip("-").isdigit() and int(count) >= 0 else None
        refresh = ExtractRefresh(columns=columns, increment=increment, row_limit=limit)
        state.connection = connection.model_copy(update={"source": source, "refresh": refresh})

    def _model_source(self, conn: etree._Element, model: ModelRelation) -> LiveConnection | None:
        """Relationship 模型萃取的來源：每張邏輯資料表在來源資料庫的資料表（可含實體層的 join）。"""
        named = [
            c for nc in descendants(conn, "named-connection") for c in children(nc, "connection")
        ]
        if len(named) != 1 or named[0].get("class") not in DIALECTS:
            return None
        probe = ParseContext(self.ctx.package, lenient=True)
        tables: list[LogicalTable] = []
        for logical, obj in zip(model.tables, self.objects, strict=True):
            relation = (
                parse_relation(probe, obj.live, self.caption) if obj.live is not None else None
            )
            if relation is None:
                return None
            tables.append(logical.model_copy(update={"relation": relation}))
        source = live_connection(probe, named[0], None, self.caption)
        if source is None:
            return None
        return source.model_copy(update={"relation": model.model_copy(update={"tables": tables})})

    def _add_table_count(
        self, state: DsState, name: str, caption: str | None, anchor: etree._Element
    ) -> None:
        """資料表的筆數欄位：以公式 1 表示，COUNT 後即為列數（單表萃取）。"""
        if name in state.by_name:
            return
        label = caption or strip_brackets(name.removeprefix(TABLE_ID_PREFIX))
        # Relationship 模型中，筆數欄位屬於它的邏輯資料表（COUNT 為該表的列數）
        table = None
        if self.model_tables is not None and name.startswith(TABLE_ID_PREFIX):
            table = strip_brackets(name.removeprefix(TABLE_ID_PREFIX))
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
                table=table,
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


GROUP_KINDS = {"categorical-bin": "群組", "bin": "數值分組"}


def _source_datatype(state: DsState, source: str) -> str | None:
    """群組來源欄位的型別：已加入的欄位，或資料源中尚未處理的 <column>。"""
    found = state.by_name.get(source)
    if found is not None:
        return found.datatype
    for column in children(state.element, "column"):
        if column.get("name") == source:
            return column.get("datatype")
    return None


def add_calc_field(ctx: ParseContext, state: DsState, column: etree._Element) -> DataField | None:
    name = column.get("name", "")
    caption = column.get("caption") or strip_brackets(name)
    calc = child(column, "calculation")
    assert calc is not None
    calc_class = calc.get("class", "")
    if calc_class in GROUP_KINDS:
        # 群組與數值分組轉成等價的公式，之後與一般計算欄位相同
        try:
            if calc_class == "categorical-bin":
                source = calc.get("column", "")
                formula = group_formula(column, calc, _source_datatype(state, source))
            else:
                formula = bin_formula(calc)
        except (GroupError, LiteralError) as exc:
            ctx.flag(column, f"{GROUP_KINDS[calc_class]}「{caption}」：{exc}")
            return None
    elif calc_class != "tableau":
        ctx.flag(column, f"計算欄位「{caption}」的類型 {calc_class}（集合等）v0 不支援")
        return None
    else:
        formula = calc.get("formula")
    if not formula:
        ctx.flag(column, f"計算欄位「{caption}」沒有公式")
        return None
    datatype = column.get("datatype", "")
    if datatype not in DATATYPES:
        ctx.flag(column, f"計算欄位「{caption}」的型別 {datatype} v0 不支援")
        return None
    aliases = member_aliases(ctx, column, datatype, f"計算欄位「{caption}」")
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
        aliases=aliases,
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


def _check_set(
    ctx: ParseContext, state: DsState, analyzer: Analyzer, data_field: DataField
) -> None:
    """Top N 與條件集合：排名依據必須是彙總，條件必須是彙總的布林值（查詢時才計算成員）。"""
    if data_field.id in state.levels or data_field.set is None:
        return
    rule = data_field.set.rule
    if rule.kind == "members":
        return
    text, what = (rule.by, "排名依據") if rule.kind == "top_n" else (rule.condition, "條件")
    try:
        info = analyzer.analyze(parse_formula(text))
        if info.level != "agg":
            raise FormulaSemanticError(f"{what}必須是彙總")
        if rule.kind == "condition" and info.datatype not in ("boolean", "null"):
            raise FormulaSemanticError("條件必須是布林值")
    except (FormulaSyntaxError, FormulaSemanticError) as exc:
        state.levels[data_field.id] = None
        message = exc.message if isinstance(exc, FormulaSemanticError) else str(exc)
        unsupported = isinstance(exc, FormulaSemanticError) and exc.unsupported
        ctx.unsupported.append(
            Unsupported(
                source_ref=data_field.source_ref,
                reason=f"集合「{data_field.caption}」的{what}「{text}」：{message}"
                + ("（v0 尚不支援）" if unsupported else ""),
                element="group",
                xml=text,
            )
        )
        return
    state.levels[data_field.id] = "row"


def analyze_calcs(ctx: ParseContext, state: DsState) -> None:
    """以公式引擎分析計算欄位：補上語法樹（附解析出的 id）與計算層級。"""
    scope = DatasourceScope(state.name, state.fields, ctx.parameters)
    analyzer = Analyzer(scope)
    state.analyzer = analyzer
    for data_field in state.fields:
        if data_field.set is not None and data_field.calc is None:
            _check_set(ctx, state, analyzer, data_field)
            continue
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
