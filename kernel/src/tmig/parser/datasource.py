"""資料源、欄位、計算欄位、資料源篩選與參數（A2、A3）。"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from lxml import etree

from tmig.formula.analyze import Analyzer
from tmig.formula.ast import FieldRef, to_json
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.formula.types import FormulaSemanticError
from tmig.ir.federation import single_database
from tmig.ir.models import (
    FILE_SCHEMA,
    LIVE_PATH_PREFIX,
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
    FieldBin,
    FieldFolder,
    FieldGroup,
    FieldUse,
    FileColumn,
    FileConnection,
    FileTable,
    GeoAlias,
    GeoRole,
    GeoSettings,
    GroupItem,
    Hierarchy,
    JoinRelation,
    LiveConnection,
    LogicalTable,
    MemberAlias,
    ModelRelation,
    Parameter,
    ParamRef,
    PivotRelation,
    ProjectRelation,
    PublishedSource,
    Relation,
    SourceColumn,
    SqlRelation,
    TableRelation,
    UnionPart,
    UnionRelation,
    Unsupported,
    WeekDay,
)
from tmig.ir.scope import DatasourceScope, ForeignSource
from tmig.ir.setformula import level_expr
from tmig.parser.colors import color_encodings, parse_color, parse_shapes, shape_encodings
from tmig.parser.connections import DIALECTS, EXECUTABLE, live_connection, parse_relation
from tmig.parser.context import DATATYPES, DsState, ParseContext
from tmig.parser.fields import (
    FilterParser,
    instance_key,
    member_aliases,
    use_from_instance,
    use_from_name,
)
from tmig.parser.groups import (
    BinDefinition,
    GroupDefinition,
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
from tmig.parser.sets import IN_OUT, Level, parse_set
from tmig.parser.twbx import attr, child, children, descendants, local_name, text_of, xpath
from tmig.parser.values import LiteralError, parse_literal
from tmig.parser.wildcard import PATH_ATTR as WILDCARD_PATH
from tmig.parser.wildcard import WildcardError
from tmig.parser.wildcard import expand as expand_wildcard

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
    "Collect": "collect",
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
        "analytic-model",
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
        "overridable-settings",
        "field-sort-info",
    }
)
MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
"""會計年度起始月在 <date-options fiscal-year-start> 中的寫法。"""
GEO_ROLES: dict[str, GeoRole] = {
    "Country": "country",
    "State": "state",
    "County": "county",
    "City": "city",
    "ZipCode": "zip",
}
"""地理角色（semantic-role 的第一段，例如 [State].[Name]、[Country].[ISO3166_2]）→ IR 的地理角色；
緯度、經度是 [Geographical].[Latitude]／[Longitude]。"""
FILE_SOURCES = frozenset({"excel-direct", "textscan", "hyper", "dataengine", "ogrdirect"})
"""檔案型連線（沒有萃取時匯入 DuckDB，見 FileConnection）。.hyper 只有一張資料表時與萃取相同，
有 join、多個 .hyper 檔或 Relationship 模型時是檔案型連線；ogrdirect 是空間檔案（Shapefile、GeoJSON、
KML、TopoJSON、Esri File Geodatabase、MapInfo 等）。"""
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
        if datatype not in DATATYPES or datatype == "spatial":
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
        ctx.param_columns[param.id] = column
        if attr(column, "default-value-field") or attr(column, "source-field"):
            ctx.dynamic_parameters.append((param, column))


def resolve_dynamic_parameters(ctx: ParseContext) -> None:
    """動態參數的欄位（[資料源].[欄位]）→ 欄位 id；所有資料源解析完後呼叫。找不到的欄位、彙總的計算欄位
    與型別不相容的欄位列為不支援（不能默默以存檔時的值計算）。"""
    for param, column in ctx.dynamic_parameters:
        for attribute, target in (
            ("default-value-field", "value_field"),
            ("source-field", "domain_field"),
        ):
            ref = attr(column, attribute)
            if not ref:
                continue
            found = _dynamic_field(ctx, ref)
            what = "目前值" if target == "value_field" else "允許值"
            if found is None:
                ctx.flag(
                    column, f"動態參數「{param.caption}」的{what}取自欄位 {ref}，找不到這個欄位"
                )
                continue
            level = found.calc.level if found.calc is not None else None
            if level in ("aggregate", "table_calc"):
                ctx.flag(
                    column,
                    f"動態參數「{param.caption}」的{what}取自彙總的計算欄位「{found.caption}」，"
                    "必須是與檢視無關的單一值（例如 FIXED 的 LOD 運算式）",
                )
                continue
            if not _compatible(found.datatype, param.datatype):
                ctx.flag(
                    column,
                    f"動態參數「{param.caption}」（{param.datatype}）的{what}取自型別不同的欄位"
                    f"「{found.caption}」（{found.datatype}）",
                )
                continue
            if target == "domain_field" and param.allowed.kind == "all":
                ctx.ignore(column, f"動態參數「{param.caption}」的允許值為全部：略過取值的欄位")
                continue
            setattr(param, target, found.id)


def _dynamic_field(ctx: ParseContext, ref: str) -> DataField | None:
    parts = qualified_parts(ref)
    if len(parts) == 2:
        state = ctx.datasources.get(parts[0])
        candidates = [state] if state is not None else []
    else:
        candidates = list(ctx.datasources.values())
    name = "[" + parts[-1].replace("]", "]]") + "]" if parts else ref
    for state in candidates:
        found = state.by_name.get(name)
        if found is not None:
            return found
    return None


def _compatible(field: str, parameter: str) -> bool:
    """欄位的值可以當參數的值：相同型別，或數字之間、日期之間。"""
    if field == parameter:
        return True
    numbers = {"integer", "real"}
    dates = {"date", "datetime"}
    return {field, parameter} <= numbers or {field, parameter} <= dates


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
        self.original_names: dict[str, str] = {}
        """變更了資料類型的欄位：隱藏的原始欄位名稱 → 原本的欄位名稱（萃取的 metadata-record 以它為名）。"""
        """Relationship 模型的邏輯資料表（N6）；不是模型時為 None。"""
        self.remote: dict[str, LiveConnection] = {}
        """跨資料庫的 join：資料庫的具名連線（名稱 → 沒有資料表的 LiveConnection）。"""

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
        is_file, files = (False, None) if published or located else self._files(conn)
        if is_file:
            # Excel、文字檔且沒有萃取：匯入時讀進 DuckDB
            if files is None and not ctx.lenient:
                return None
            data_conn, via_extract, connection = conn, False, files or placeholder
        elif located is None:
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
            fiscal_year_start=self._fiscal_year_start(),
            published=None if published else self._published(),
        )
        self._fields(state, conn, data_conn, via_extract)
        self._fiscal_fields(state)
        self._hierarchies(state)
        self._folders(state)
        self._geography(state)
        self._comments(state)
        if not state.fields:
            ctx.flag(el, f"資料源「{self.caption}」沒有可用的欄位")
            return None
        if self.model_tables is not None and not self._attach_model(state):
            return None
        if isinstance(state.connection, FileConnection):
            # 跨資料庫的 join 只用到同一個資料庫（例如沒有用到的連線）：一般的 Live 連線
            single = single_database(state.connection)
            if single is not None:
                state.connection = single
        if via_extract:
            self._attach_refresh(state, conn, data_conn)
        analyze_calcs(ctx, state)
        state.instances = {
            instance_key(inst.get("name", "")): inst for inst in children(el, "column-instance")
        }
        self._filters(state)
        self._default_marks(state)
        for model in children(self.el, "analytic-model"):
            name = child(child(model, "name"), "field")
            if name is not None and name.get("name"):
                state.analytic_models[name.get("name", "")] = model
        self._check_children()
        return state

    def _default_marks(self, state: DsState) -> None:
        """欄位預設的色彩與形狀（預設屬性；資料源 <style> 中欄位本身的編碼，例如 [none:Region:nk]）：
        編輯器新增的工作表用到這個欄位時與其他工作表同色。工作表各自的色彩仍由工作表解析（它的 <style>
        優先於資料源的），這裡只記下維度本身的離散色彩與形狀。"""
        style = child(self.el, "style")
        if style is None:
            return

        def target(key: str) -> DataField | None:
            inst = state.instances.get(key)
            if inst is None or inst.get("derivation", "None") != "None":
                return None
            found = state.by_name.get(inst.get("column", ""))
            return found if found is not None and found.role == "dimension" else None

        for key, enc in color_encodings(style).items():
            found = target(key)
            palette = parse_color(enc) if found is not None else None
            if found is not None and palette is not None and palette.kind == "discrete":
                found.default_color = palette
        for key, enc in shape_encodings(style).items():
            found = target(key)
            if found is not None:
                found.default_shapes = parse_shapes(enc)[0]

    def _comments(self, state: DsState) -> None:
        """欄位的註解（Tableau 的「預設屬性＞註解」，<column><desc>）：資料表中的欄位也可以有。"""
        for column in children(self.el, "column"):
            found = state.by_name.get(column.get("name", ""))
            if found is not None and found.comment is None:
                found.comment = field_comment(column)

    def _geography(self, state: DsState) -> None:
        """地理角色（<column semantic-role>）、「編輯位置」中指定的對應（欄位的 <semantic-values>）與
        資料源的預設國家、州（資料源的 <semantic-values>，%null% 表示沒有指定）。"""
        for column in children(self.el, "column"):
            role_ref = column.get("semantic-role")
            found = state.by_name.get(column.get("name", ""))
            if not role_ref or found is None:
                continue
            role = geo_role(role_ref)
            if role is None:
                self.ctx.ignore(
                    column,
                    f"欄位「{found.caption}」的地理角色 {role_ref} 尚未支援：放到地圖上時列為未知的位置",
                )
                continue
            found.geo_role = role
            for values in children(column, "semantic-values"):
                for item in children(values, "semantic-value"):
                    try:
                        key = parse_literal(item.get("key"), "string")
                        location = parse_literal(item.get("value"), "string")
                    except LiteralError:
                        key = location = None
                    if isinstance(key, str) and isinstance(location, str):
                        found.geo_aliases.append(GeoAlias(value=key, location=location))
                    else:
                        self.ctx.ignore(
                            item,
                            f"欄位「{found.caption}」編輯位置的對應 {item.get('key')} 無法辨識",
                        )
        defaults: dict[str, str] = {}
        for item in children(child(self.el, "semantic-values"), "semantic-value"):
            parts = qualified_parts(item.get("key", ""))
            role = GEO_ROLES.get(parts[0]) if parts else None
            try:
                value = parse_literal(item.get("value"), "string")
            except LiteralError:
                value = None
            if role in ("country", "state") and isinstance(value, str) and value:
                defaults[role] = value
        if defaults:
            state.geo = GeoSettings(country=defaults.get("country"), state=defaults.get("state"))

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
            # 直接以 .hyper 為來源：只有一張資料表時與萃取相同；join 或 Relationship 模型時是檔案型連線
            relations = children(conn, "relation")
            single = len(relations) == 1 and relations[0].get("type") == "table"
            if single and len(self.objects) <= 1:
                return named[0], False
        return None

    def _files(self, conn: etree._Element) -> tuple[bool, FileConnection | None]:
        """Excel、文字檔且沒有萃取（檔案型連線）：每張資料表的檔案與 Tableau 讀到的欄名、型別。
        回傳（是否為檔案型連線, 連線）；不支援的寫法列入 unsupported，連線為 None。

        跨資料庫的 join（多個具名連線，其中有資料庫）也是檔案型連線：資料庫的資料表與自訂 SQL 在查詢時
        取回、與檔案在本機合併（FileTable 的 format 為 live，見 tmig.data.federated）。"""
        named: dict[str, etree._Element] = {}
        for nc in descendants(conn, "named-connection"):
            for found in children(nc, "connection"):
                named[nc.get("name", "")] = found
        if not named:
            return False, None
        databases = {k: c for k, c in named.items() if c.get("class") not in FILE_SOURCES}
        if databases and len(named) == 1:
            return False, None  # 單一資料庫：Live 連線
        if databases and not self._remote_connections(databases):
            return True, None
        for found in named.values():
            if found.get("cleaning") == "yes" and found.get("class") != "excel-direct":
                self.ctx.flag(
                    found,
                    f"資料源「{self.caption}」在 {found.get('class')} 的檔案使用資料解譯器"
                    "（Data Interpreter），只支援 Excel",
                )
                return True, None
        if not self._expand_wildcards(conn, named):
            return True, None
        tables: dict[str, FileTable] = {}
        if len(self.objects) > 1:
            # Relationship 模型：每張邏輯資料表一個檔案中的資料表
            model_tables = live_tables(self.ctx, self.objects, self.caption)
            if model_tables is None:
                return True, None
            converted: list[LogicalTable] = []
            for logical, obj in zip(model_tables, self.objects, strict=True):
                assert obj.live is not None
                relation = self._file_relation(logical.relation, obj.live, named, tables)
                if relation is None:
                    return True, None
                converted.append(logical.model_copy(update={"relation": relation}))
            self.model_tables = converted
            first = converted[0].relation
            return True, FileConnection(kind="file", tables=list(tables.values()), relation=first)
        relations = children(conn, "relation")
        if len(relations) != 1:
            self.ctx.flag(conn, f"資料源「{self.caption}」的資料表關聯樹無法解析")
            return True, None
        tree = parse_relation(self.ctx, relations[0], self.caption)
        if tree is None:
            return True, None
        relation = self._file_relation(tree, relations[0], named, tables)
        if relation is None:
            return True, None
        return True, FileConnection(kind="file", tables=list(tables.values()), relation=relation)

    def _remote_connections(self, databases: dict[str, etree._Element]) -> bool:
        """跨資料庫的 join 中的資料庫連線（記在 self.remote）；有不支援的連線類型時列入 unsupported。"""
        for key, element in databases.items():
            live = live_connection(self.ctx, element, None, self.caption)
            if live is None or live.dialect is None or live.dialect not in EXECUTABLE:
                return False  # live_connection 已列入 unsupported
            self.remote[key] = live
        return True

    def _expand_wildcards(self, conn: etree._Element, named: dict[str, etree._Element]) -> bool:
        """萬用字元聯集（batch-union）展開成一般的聯集（見 tmig.parser.wildcard）：依 .twbx 中符合模式的檔案，
        每個檔案或工作表一張資料表；具名連線加進 named。無法展開時列入 unsupported，回傳 False。"""
        found = [r for r in conn.iter("relation") if r.get("type") == "batch-union"]
        for obj in self.objects:
            if obj.live is not None:
                found += [r for r in obj.live.iter("relation") if r.get("type") == "batch-union"]
        if not found:
            return True
        package = self.ctx.package
        if package.source.suffix.lower() != ".twbx":
            self.ctx.flag(
                found[0], f"資料源「{self.caption}」的萬用字元聯集需要 .twbx（含資料的報表）"
            )
            return False
        names = [f.name.replace("\\", "/") for f in package.files]
        archive = zipfile.ZipFile(package.source)
        try:
            for i, element in enumerate(dict.fromkeys(found)):
                source = named.get(element.get("connection", ""))
                if source is None and len(named) == 1:
                    source = next(iter(named.values()))
                if source is None:
                    self.ctx.flag(
                        element, f"資料源「{self.caption}」的萬用字元聯集找不到檔案的連線"
                    )
                    return False
                if source.get("class") == "textscan":
                    directory = (source.get("directory") or "").replace("\\", "/").rstrip("/")
                    filename = source.get("filename") or ""
                    anchor = self._packaged(f"{directory}/{filename}" if directory else filename)
                else:
                    anchor = self._packaged(source.get("filename") or "")
                if anchor is None:
                    self.ctx.flag(
                        source, f"資料源「{self.caption}」萬用字元聯集的檔案不在 .twbx 內"
                    )
                    return False
                try:
                    union, connections = expand_wildcard(
                        element,
                        source,
                        anchor,
                        names,
                        lambda name: archive.read(name),
                        f"wildcard{i}",
                    )
                except WildcardError as exc:
                    self.ctx.flag(element, f"資料源「{self.caption}」：{exc}")
                    return False
                parent = element.getparent()
                assert parent is not None
                parent.replace(element, union)
                for obj in self.objects:
                    if obj.live is element:
                        obj.live = union
                named.update(connections)
        finally:
            archive.close()
        return True

    def _file_relation(
        self,
        relation: Relation,
        element: etree._Element,
        named: dict[str, etree._Element],
        tables: dict[str, FileTable],
    ) -> Relation | None:
        """關聯樹中的資料表改成匯入後的資料表（Files.<name>），並記下各資料表的檔案。"""
        inner = [r for r in children(element) if local_name(r) == "relation"]
        if isinstance(relation, JoinRelation):
            left = self._file_relation(relation.left, inner[0], named, tables)
            right = self._file_relation(relation.right, inner[1], named, tables)
            if left is None or right is None:
                return None
            return relation.model_copy(update={"left": left, "right": right})
        if isinstance(relation, PivotRelation | ProjectRelation):
            source = self._file_relation(relation.source, inner[0], named, tables)
            return relation.model_copy(update={"source": source}) if source is not None else None
        if isinstance(relation, UnionRelation):
            parts: list[UnionPart] = []
            for part, part_el in zip(relation.parts, inner, strict=True):
                converted = self._file_relation(part.relation, part_el, named, tables)
                if converted is None:
                    return None
                found = tables[converted.name] if isinstance(converted, TableRelation) else None
                path = part_el.get(WILDCARD_PATH) or (
                    self._original_path(part_el, named)
                    if found and found.format == "text"
                    else None
                )
                parts.append(part.model_copy(update={"relation": converted, "path": path}))
            return relation.model_copy(update={"parts": parts})
        if isinstance(relation, SqlRelation) and element.get("connection", "") in self.remote:
            # 跨資料庫的 join：資料庫的自訂 SQL 在查詢時取回整段的結果
            columns = self._file_columns(relation.name, child(element, "columns"))
            if not columns:
                self.ctx.flag(
                    element, f"資料源「{self.caption}」的自訂 SQL「{relation.name}」沒有欄位資訊"
                )
                return None
            live = self.remote[element.get("connection", "")]
            tables[relation.name] = FileTable(
                name=relation.name,
                format="live",
                path=LIVE_PATH_PREFIX + live.connection_id,
                connection=live,
                sql=relation.sql,
                columns=columns,
            )
            return TableRelation(
                kind="table", name=relation.name, table=[FILE_SCHEMA, relation.name]
            )
        if not isinstance(relation, TableRelation):
            self.ctx.flag(element, f"資料源「{self.caption}」的檔案中有自訂 SQL，v0 不支援")
            return None
        found = self._file_table(relation.name, element, named)
        if found is None:
            return None
        tables[found.name] = found
        return TableRelation(kind="table", name=relation.name, table=[FILE_SCHEMA, relation.name])

    def _original_path(self, element: etree._Element, named: dict[str, etree._Element]) -> str:
        """文字檔在作者電腦上的路徑（聯集的 Path 欄的值）：連線的資料夾加上檔名。"""
        source = named.get(element.get("connection", ""))
        if source is None:
            source = next(iter(named.values()))
        table = strip_brackets(element.get("table", ""))
        base, sep, ext = table.rpartition("#")
        filename = f"{base}.{ext}" if sep else table
        directory = (source.get("directory") or "").rstrip("/\\")
        slash = "\\" if "\\" in directory else "/"
        return f"{directory}{slash}{filename}" if directory else filename

    def _file_table(
        self, name: str, element: etree._Element, named: dict[str, etree._Element]
    ) -> FileTable | None:
        source = named.get(element.get("connection", ""))
        if source is None and len(named) == 1:
            source = next(iter(named.values()))
        if source is None:
            self.ctx.flag(element, f"資料源「{self.caption}」的資料表「{name}」找不到檔案的連線")
            return None
        table = strip_brackets(element.get("table", ""))
        columns_el = child(element, "columns")
        columns = self._file_columns(name, columns_el)
        if not columns:
            self.ctx.flag(element, f"資料源「{self.caption}」的資料表「{name}」沒有欄位資訊")
            return None
        header = (columns_el.get("header") if columns_el is not None else None) != "no"
        live = self.remote.get(element.get("connection", ""))
        if live is not None:
            # 跨資料庫的 join：資料庫的資料表（欄依 metadata-record）在查詢時取回用到的欄
            parts = qualified_parts(element.get("table", ""))
            if not parts:
                self.ctx.flag(element, f"資料源「{self.caption}」的資料表 {table} 無法解析")
                return None
            return FileTable(
                name=name,
                format="live",
                path=LIVE_PATH_PREFIX + live.connection_id,
                connection=live,
                table=parts,
                columns=columns,
            )
        if source.get("class") in ("hyper", "dataengine"):
            dbname = (source.get("dbname") or "").replace("\\", "/")
            path = self._packaged(dbname)
            if path is None:
                self.ctx.flag(source, f"資料源「{self.caption}」的檔案「{dbname}」不在 .twbx 內")
                return None
            return FileTable(
                name=name,
                format="hyper",
                path=path,
                table=qualified_parts(element.get("table", "")) or None,
                columns=columns,
            )
        if source.get("class") == "textscan":
            # 文字檔：資料表名稱是檔名，副檔名前的點寫成 #（[orders#csv]）
            base, sep, ext = table.rpartition("#")
            filename = f"{base}.{ext}" if sep else table or source.get("filename", "")
            directory = (source.get("directory") or "").replace("\\", "/").rstrip("/")
            path = self._packaged(f"{directory}/{filename}" if directory else filename)
            if path is None:
                self.ctx.flag(source, f"資料源「{self.caption}」的檔案「{filename}」不在 .twbx 內")
                return None
            assert columns_el is not None
            separator = columns_el.get("separator") or ","
            return FileTable(
                name=name,
                format="text",
                path=path,
                header=header,
                separator="\t" if separator == "\\t" else separator,
                charset=columns_el.get("character-set") or "UTF-8",
                locale=columns_el.get("locale") or None,
                columns=columns,
            )
        if source.get("class") == "ogrdirect":
            # 空間檔案：資料表名稱是圖層（壓縮檔中的 Shapefile、TopoJSON 的物件、File Geodatabase 的
            # feature class）；File Geodatabase 可以是資料夾（.twbx 中是資料夾裡的各個檔案）
            filename = (source.get("filename") or "").replace("\\", "/").rstrip("/")
            directory = (source.get("directory") or "").replace("\\", "/").rstrip("/")
            joined = f"{directory}/{filename}" if directory and "/" not in filename else filename
            path = self._packaged_folder(joined) if joined.lower().endswith(".gdb") else None
            path = path or self._packaged(joined)
            if path is None:
                self.ctx.flag(source, f"資料源「{self.caption}」的檔案「{filename}」不在 .twbx 內")
                return None
            return FileTable(
                name=name, format="spatial", path=path, layer=table or None, columns=columns
            )
        filename = source.get("filename", "")
        path = self._packaged(filename)
        if path is None:
            self.ctx.flag(source, f"資料源「{self.caption}」的檔案「{filename}」不在 .twbx 內")
            return None
        grid = (columns_el.get("gridOrigin") or "") if columns_el is not None else ""
        cells = grid.split(":no:")[0].split(":yes:")[0] if grid else None
        return FileTable(
            name=name,
            format="excel",
            path=path,
            sheet=table[:-1] if table.endswith("$") else table,
            cells=cells if cells and ":" in cells else None,
            header=header,
            # 資料解譯器：範圍中依欄名找標題列（見 tmig.data.file_import.interpreted_header）
            interpreted=source.get("cleaning") == "yes",
            columns=columns,
        )

    def _file_columns(self, name: str, columns_el: etree._Element | None) -> list[FileColumn]:
        """檔案中各欄的欄名與型別：<columns> 中的 <column>，依 ordinal 排列；沒有時依 metadata-record。"""
        found: list[tuple[int, FileColumn]] = []
        for column in children(columns_el, "column"):
            datatype = column.get("datatype", "")
            ordinal = column.get("ordinal", "")
            if datatype not in DATATYPES or not ordinal.isdigit() or not column.get("name"):
                continue
            found.append((int(ordinal), FileColumn(name=column.get("name", ""), datatype=datatype)))  # type: ignore[arg-type]
        if not found:
            for record in self._meta_columns(child(self.el, "connection")):
                parent = text_of(child(record.element, "parent-name"))
                ordinal = text_of(child(record.element, "ordinal"))
                if strip_brackets(parent) != name or not ordinal.isdigit():
                    continue
                if record.local_type in DATATYPES:
                    found.append(
                        (int(ordinal), FileColumn(name=record.remote, datatype=record.local_type))  # type: ignore[arg-type]
                    )
        return [column for _, column in sorted(found, key=lambda item: item[0])]

    def _live(self, conn: etree._Element) -> LiveConnection | None:
        """Live 連線（N1）：資料在來源資料庫，查詢時下推；單一具名連線（多個連線為跨資料庫的 join，見
        _files）。"""
        named = children(child(conn, "named-connections"), "named-connection")
        data = [c for nc in named for c in children(nc, "connection")]
        if not data and conn.get("class") not in (None, "federated"):
            data = [conn]  # 舊版檔案：連線直接寫在 <connection> 上
        if len(data) != 1:
            self.ctx.flag(conn, f"資料源「{self.caption}」沒有可用的連線設定")
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

    def _packaged_folder(self, folder: str) -> str | None:
        """.twbx 內的資料夾（例如 File Geodatabase 的 .gdb）：路徑相同，或資料夾名稱相同的只有一個。"""
        package = self.ctx.package
        if package.source.suffix.lower() != ".twbx":
            return folder or None
        names = [f.name.replace("\\", "/") for f in package.files]
        wanted = folder.lstrip("./").lower()
        folders = {
            "/".join(parts[: i + 1])
            for n in names
            for parts in [n.split("/")]
            for i, part in enumerate(parts[:-1])
            if part.lower().endswith(".gdb")
        }
        exact = [f for f in folders if f.lower() == wanted]
        if exact:
            return exact[0]
        base = PurePosixPath(wanted).name
        same = [f for f in folders if PurePosixPath(f).name.lower() == base]
        return same[0] if len(same) == 1 else None

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

    def _date_options(self) -> etree._Element | None:
        """資料源的日期屬性：<date-options>；報表中已發布資料源的複本另寫在 <overridable-settings> 之下
        （資料源本身的設定優先）。"""
        found = child(self.el, "date-options")
        if found is None:
            found = child(child(self.el, "overridable-settings"), "date-options")
        return found

    def _week_start(self) -> WeekDay:
        options = self._date_options()
        value = (options.get("start-of-week") if options is not None else None) or "sunday"
        value = value.lower()
        return value if value in WEEK_DAYS else "sunday"  # type: ignore[return-value]

    def _fiscal_year_start(self) -> int:
        """會計年度起始月（<date-options fiscal-year-start='april'>，也接受 1 到 12）；無法辨識時列為不支援，
        不默默以日曆年計算。"""
        options = self._date_options()
        raw = ((options.get("fiscal-year-start") if options is not None else None) or "").strip()
        if not raw:
            return 1
        month = _month(raw)
        if month is None:
            assert options is not None
            self.ctx.flag(options, f"資料源「{self.caption}」的會計年度起始月 {raw} 無法辨識")
            return 1
        return month

    def _fiscal_fields(self, state: DsState) -> None:
        """日期欄位自訂的會計年度起始月（預設屬性，<column fiscal-year-start='4'>；0 為沿用資料源）。"""
        for column in children(self.el, "column"):
            raw = (column.get("fiscal-year-start") or "").strip()
            if raw in ("", "0"):
                continue
            found = state.by_name.get(column.get("name", ""))
            if found is None:
                continue
            month = _month(raw)
            if month is None:
                self.ctx.flag(column, f"欄位「{found.caption}」的會計年度起始月 {raw} 無法辨識")
                continue
            found.fiscal_year_start = month

    def _folders(self, state: DsState) -> None:
        """資料窗格的資料夾：2020.2 以前的 <folder name role='dimensions'>（分在維度或量值之下），與 2020.2
        起的 <folders-common><folder>；資料夾中的欄位（<folder-item name='[欄位]' type='field'>）找不到時
        略過，一個欄位只算在第一個資料夾。"""
        elements = [
            *children(self.el, "folder"),
            *children(child(self.el, "folders-common"), "folder"),
        ]
        roles = {"dimensions": "dimension", "measures": "measure"}
        seen: set[str] = set()
        for element in elements:
            name = (element.get("name") or "").strip()
            if not name:
                continue
            role = roles.get(element.get("role") or "")
            ids: list[str] = []
            for item in children(element, "folder-item"):
                if (item.get("type") or "field") != "field":
                    continue
                found = state.by_name.get(item.get("name", ""))
                if found is not None and found.id not in seen:
                    ids.append(found.id)
                    seen.add(found.id)
            existing = next((f for f in state.folders if (f.name, f.role) == (name, role)), None)
            if existing is not None:
                existing.fields.extend(ids)
            else:
                state.folders.append(FieldFolder(name=name, fields=ids, role=role))  # type: ignore[arg-type]

    def _hierarchies(self, state: DsState) -> None:
        """階層：<drill-paths><drill-path name='Product'><field>[Category]</field>…；找不到的欄位略過
        （例如隱藏後刪除的欄位），一個欄位只算在第一個階層。"""
        used: set[str] = set()
        for path in children(child(self.el, "drill-paths"), "drill-path"):
            name = path.get("name") or ""
            ids: list[str] = []
            for item in children(path, "field"):
                found = state.by_name.get(text_of(item))
                if found is not None and found.id not in used and found.id not in ids:
                    ids.append(found.id)
            if name and ids and all(h.name != name for h in state.hierarchies):
                state.hierarchies.append(Hierarchy(name=name, fields=ids))
                used.update(ids)

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
        # 來源（連線層）的型別：變更資料類型時萃取與來源資料表中仍是原本的型別
        source_types = {r.local: r.local_type for r in self._meta_columns(conn)}
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
            source_type = source_types.get(record.local, "")
            if (
                column is not None
                and column.get("datatype-customized") == "true"
                and source_type in DATATYPES
                and source_type != datatype
                and self._add_converted(state, column, datatype, source_type, record, physical)
            ):
                continue
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
            if via_extract and column.get("hidden") == "true":
                # 建立萃取時排除了隱藏的欄位（「隱藏所有未使用的欄位」）：萃取中沒有，Tableau 也不能用
                ctx.ignore(column, f"隱藏的欄位 {name} 不在萃取中：略過")
                continue
            if (
                not via_extract
                and self.model_tables is None
                and add_uncached_column(ctx, state, column)
            ):
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
            if attr(group, "ui-builder") == "nest-group":
                self._add_combined(state, group)
                continue
            ctx.ignore(group, "合併集合等群組（工作表若有使用會另列為不支援）")

    def _add_combined(self, state: DsState, group: etree._Element) -> None:
        """合併欄位（Combined Field）：多個維度的值組合成一個維度，成員與 Tableau 相同寫成「值, 值」
        （<groupfilter function='crossjoin'> 中各欄位的 level-members）；轉成字串的計算欄位。"""
        name = group.get("name", "")
        caption = group.get("caption") or strip_brackets(name)
        top = child(group, "groupfilter")
        levels = (
            [g.get("level", "") for g in children(top, "groupfilter")]
            if top is not None and top.get("function") == "crossjoin"
            else []
        )
        if len(levels) < 2 or any(level not in state.by_name for level in levels):
            self.ctx.flag(group, f"合併欄位「{caption}」的組成欄位（{'、'.join(levels)}）無法辨識")
            return
        formula = ' + ", " + '.join(f'IFNULL(STR({level}), "Null")' for level in levels)
        data_field = DataField(
            id=self.ctx.ids.make("f", caption, name),
            name=name,
            caption=caption,
            role="dimension",
            datatype="string",
            column=None,
            calc=Calc(formula=formula),
            combined=[state.by_name[level].id for level in levels],
            source_ref=xpath(group),
        )
        state.add_field(data_field)

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
        if data_field.datatype not in DATATYPES or data_field.datatype == "spatial":
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
        found = raw_field(
            self.ctx,
            name,
            datatype,
            column,
            record.aggregation if record is not None else "",
            physical.get(name, strip_brackets(name)),
            self._field_table(state, record),
            anchor,
        )
        if found is not None:
            found.physical_table = self._physical_table(record)
            state.add_field(found)

    def _physical_table(self, record: MetaColumn | None) -> str | None:
        """Relationship 模型中邏輯資料表內含 join 時，欄位所屬的實體資料表（metadata-record 的
        parent-name，即 join 樹中資料表的名稱）。"""
        if record is None or self.model_tables is None:
            return None
        logical = object_id_of(record.element)
        table = next((t for t in self.model_tables if t.id == logical), None)
        if table is None or not isinstance(table.relation, JoinRelation):
            return None
        return strip_brackets(text_of(child(record.element, "parent-name"))) or None

    def _field_table(self, state: DsState, record: MetaColumn | None) -> str | None:
        """欄位所屬的資料表：Live 連線為 metadata-record 的 parent-name；
        Relationship 模型為邏輯資料表（object id）。"""
        if record is None:
            return None
        if self.model_tables is not None:
            return object_id_of(record.element)
        if state.connection.kind not in ("live", "file"):
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
            record = records.get(self.original_names.get(data_field.name, data_field.name))
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
        # Relationship 模型中，筆數欄位屬於它的邏輯資料表（COUNT 為該表的列數）
        add_table_count(self.ctx, state, name, caption, anchor, self.model_tables is not None)

    def _add_calc(self, state: DsState, column: etree._Element) -> None:
        add_calc_field(self.ctx, state, column)

    def _add_converted(
        self,
        state: DsState,
        column: etree._Element,
        datatype: str,
        source_type: str,
        record: MetaColumn,
        physical: dict[str, str],
    ) -> bool:
        """變更了資料類型的欄位（datatype-customized）：萃取與來源資料表中仍是原本的型別，Tableau 查詢時
        才轉換（與型別轉換函式相同，例如數字轉日期是 1899-12-30 起算的天數、字串轉數字無法轉換時為 Null）。
        以隱藏的原始欄位加上轉換它的計算欄位表示；轉成布林值等尚不支援的轉換回傳 False（照原樣當成欄位）。"""
        convert = CONVERSIONS.get(datatype)
        if convert is None:
            return False
        name = column.get("name", "")
        caption = column.get("caption") or strip_brackets(name)
        source_name = f"[{strip_brackets(name)} (原始型別)]"
        if source_name in state.by_name:
            return False
        synthetic = etree.fromstring(etree.tostring(column))
        for old in synthetic.findall("calculation"):
            synthetic.remove(old)
        calc = etree.SubElement(synthetic, "calculation")
        calc.set("class", "tableau")
        calc.set("formula", f"{convert}({source_name})")
        created = add_calc_field(self.ctx, state, synthetic)
        if created is None:
            return False
        created.source_ref = xpath(column)
        role = "measure" if source_type in ("integer", "real") else "dimension"
        state.add_field(
            DataField(
                id=self.ctx.ids.make("f", f"{caption} source", source_name),
                name=source_name,
                caption=f"{caption}（原始型別）",
                role=role,  # type: ignore[arg-type]
                datatype=source_type,  # type: ignore[arg-type]
                default_agg="sum" if role == "measure" else None,
                format=None,
                hidden=True,
                column=physical.get(name, strip_brackets(name)),
                table=self._field_table(state, record),
                physical_table=self._physical_table(record),
                calc=None,
                source_ref=xpath(column),
            )
        )
        self.original_names[source_name] = name
        self.ctx.ignore(
            column,
            f"欄位「{caption}」變更了資料類型（{source_type} → {datatype}）：查詢時以 "
            f"{convert}() 轉換，與 Tableau 相同",
        )
        return True

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


def _month(raw: str) -> int | None:
    """月份的寫法：英文名稱（january）或 1 到 12。"""
    text = raw.strip().lower()
    if text in MONTH_NAMES:
        return MONTH_NAMES.index(text) + 1
    if text.isdigit() and 1 <= int(text) <= 12:
        return int(text)
    return None


def _source_datatype(state: DsState, source: str) -> str | None:
    """群組來源欄位的型別：已加入的欄位，或資料源中尚未處理的 <column>。"""
    found = state.by_name.get(source)
    if found is not None:
        return found.datatype
    for column in children(state.element, "column"):
        if column.get("name") == source:
            return column.get("datatype")
    return None


CONVERSIONS = {
    "date": "DATE",
    "datetime": "DATETIME",
    "integer": "INT",
    "real": "FLOAT",
    "string": "STR",
}
"""變更資料類型時 Tableau 的轉換（與型別轉換函式相同）。"""


def raw_field(
    ctx: ParseContext,
    name: str,
    datatype: str,
    column: etree._Element | None,
    aggregation: str,
    physical: str,
    table: str | None,
    anchor: etree._Element,
) -> DataField | None:
    """資料表中的一欄（非計算欄位）；型別不支援時列為不支援並回傳 None。"""
    caption = (column.get("caption") if column is not None else None) or strip_brackets(name)
    if datatype not in DATATYPES:
        ctx.flag(anchor, f"欄位「{caption}」的型別 {datatype} v0 不支援")
        return None
    aliases = member_aliases(ctx, column, datatype, f"欄位「{caption}」")
    role = (column.get("role") if column is not None else None) or (
        "measure" if datatype in ("integer", "real", "spatial") else "dimension"
    )
    if datatype == "spatial":
        role = "measure"  # 空間資料只能以 COLLECT 彙總放進檢視
    agg_text = (column.get("aggregation") if column is not None else None) or aggregation
    default_agg = METADATA_AGG.get(agg_text) if role == "measure" else None
    if datatype == "spatial":
        default_agg = "collect"
    return DataField(
        id=ctx.ids.make("f", caption, name),
        name=name,
        caption=caption,
        role=role,  # type: ignore[arg-type]
        datatype=datatype,  # type: ignore[arg-type]
        default_agg=default_agg,
        format=column.get("default-format") if column is not None else None,
        hidden=column is not None and column.get("hidden") == "true",
        column=physical,
        table=table,
        calc=None,
        aliases=aliases,
        source_ref=xpath(anchor),
    )


def geo_role(semantic_role: str) -> GeoRole | None:
    """Tableau 的地理角色（例如 [State].[Name]、[Geographical].[Latitude]）；不支援的角色為 None。"""
    parts = qualified_parts(semantic_role)
    if len(parts) == 2 and parts[0] == "Geographical":
        return {"Latitude": "latitude", "Longitude": "longitude"}.get(parts[1])  # type: ignore[return-value]
    return GEO_ROLES.get(parts[0]) if parts else None


def single_live_table(state: DsState) -> str | None:
    """Live 連線只有一張資料表（或一段自訂 SQL）時它的名稱；萃取、檔案、join 或 Relationship 模型為 None。"""
    connection = state.connection
    if not isinstance(connection, LiveConnection):
        return None
    relation = connection.relation
    if not isinstance(relation, TableRelation | SqlRelation):
        return None
    return relation.name


def add_uncached_column(ctx: ParseContext, state: DsState, column: etree._Element) -> bool:
    """Live 連線的欄位清單（metadata-records）是存檔時的快取，Tableau 連線時重新讀取來源資料表的欄位：
    不在快取中、宣告了型別的欄位（只有一張資料表時）以宣告的型別直接查詢；來源資料表沒有這一欄時與
    Tableau 相同查詢失敗。"""
    table = single_live_table(state)
    name = column.get("name", "")
    if table is None or not name or name in state.by_name:
        return False
    if column.get("datatype", "") not in DATATYPES or child(column, "calculation") is not None:
        return False
    found = raw_field(
        ctx, name, column.get("datatype", ""), column, "", strip_brackets(name), table, column
    )
    if found is None:
        return False
    if found.role == "measure" and found.default_agg is None:
        found.default_agg = "sum"
    ctx.ignore(
        column, f"欄位 {name} 不在存檔時快取的欄位清單中：以宣告的型別直接查詢來源資料表 {table}"
    )
    state.add_field(found)
    return True


def _synthetic_calc(
    ctx: ParseContext,
    state: DsState,
    name: str,
    caption: str,
    datatype: str,
    formula: str,
    source: etree._Element,
) -> DataField | None:
    """查詢之後才填值的欄位（叢集、Forecast Indicator）：公式為常數的計算欄位，沿用來源元素的別名。"""
    column = etree.Element("column")
    for key, value in (
        ("caption", caption),
        ("datatype", datatype),
        ("name", name),
        ("role", "dimension"),
        ("type", "ordinal" if datatype == "integer" else "nominal"),
    ):
        column.set(key, value)
    aliases = child(source, "aliases")
    if aliases is not None:
        column.append(etree.fromstring(etree.tostring(aliases)))
    calc = etree.SubElement(column, "calculation")
    calc.set("class", "tableau")
    calc.set("formula", formula)
    created = add_calc_field(ctx, state, column)
    if created is not None:
        created.source_ref = xpath(source)
    return created


def add_cluster_field(
    ctx: ParseContext, state: DsState, column: etree._Element
) -> DataField | None:
    """工作表上的叢集（is-adhoc-cluster）：整數的維度，查詢之後才填入叢集編號（ClusterSpec）。"""
    name = column.get("name", "")
    found = state.by_name.get(name)
    if found is not None:
        return found
    return _synthetic_calc(
        ctx, state, name, column.get("caption") or "Clusters", "integer", "0", column
    )


def add_forecast_indicator(
    ctx: ParseContext, state: DsState, inst: etree._Element
) -> DataField | None:
    """預測的 Forecast Indicator：實際值為 Actual，查詢之後補上的估計值為 Estimate（Forecast）。"""
    name = inst.get("column") or "[Forecast Indicator]"
    found = state.by_name.get(name)
    if found is not None:
        return found
    return _synthetic_calc(ctx, state, name, "Forecast Indicator", "string", '"Actual"', inst)


def add_table_count(
    ctx: ParseContext,
    state: DsState,
    name: str,
    caption: str | None,
    anchor: etree._Element,
    model: bool = False,
) -> None:
    """資料表的筆數欄位（「資料表 (計數)」）：以公式 1 表示，COUNT 後即為列數。model：Relationship 模型，
    欄位屬於它的邏輯資料表。"""
    if name in state.by_name:
        return
    label = caption or strip_brackets(name.removeprefix(TABLE_ID_PREFIX))
    table = None
    if model and name.startswith(TABLE_ID_PREFIX):
        table = strip_brackets(name.removeprefix(TABLE_ID_PREFIX))
    state.add_field(
        DataField(
            id=ctx.ids.make("f", f"{label} count", name),
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


def add_calc_field(ctx: ParseContext, state: DsState, column: etree._Element) -> DataField | None:
    name = column.get("name", "")
    caption = column.get("caption") or strip_brackets(name)
    calc = child(column, "calculation")
    assert calc is not None
    calc_class = calc.get("class", "")
    definition: GroupDefinition | BinDefinition | None = None
    if calc_class in GROUP_KINDS:
        # 群組與數值分組轉成等價的公式，之後與一般計算欄位相同；定義另外記下（編輯器的「編輯群組」）
        try:
            if calc_class == "categorical-bin":
                source = calc.get("column", "")
                formula, definition = group_formula(column, calc, _source_datatype(state, source))
            else:
                formula, definition = bin_formula(calc)
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
    role = column.get("role") or (
        "measure" if datatype in ("integer", "real", "spatial") else "dimension"
    )
    agg_text = column.get("aggregation", "")
    default_agg = METADATA_AGG.get(agg_text) if role == "measure" else None
    if datatype == "spatial":
        # 空間資料的計算欄位（MAKEPOINT 等）：與 Tableau 相同以 COLLECT 彙總
        role, default_agg = "measure", "collect"
    data_field = DataField(
        id=ctx.ids.make("f", caption, name),
        name=name,
        caption=caption,
        role=role,  # type: ignore[arg-type]
        datatype=datatype,  # type: ignore[arg-type]
        default_agg=default_agg,
        format=column.get("default-format"),
        hidden=column.get("hidden") == "true",
        column=None,
        calc=Calc(formula=formula),
        aliases=aliases,
        comment=field_comment(column),
        source_ref=xpath(column),
    )
    if definition is not None:
        resolved = _definition(ctx, state, definition)
        if resolved is not None:
            data_field = _with_definition(data_field, resolved)
        else:
            state.definitions[data_field.id] = definition  # 來源欄位在後面：分析公式前再對應
    state.add_field(data_field)
    return data_field


def field_comment(column: etree._Element) -> str | None:
    """欄位的註解（<desc><formatted-text><run>…）：各段文字接起來。"""
    desc = child(column, "desc")
    if desc is None:
        return None
    text = "".join(run.text or "" for run in desc.iter() if local_name(run) == "run").strip()
    return text or None


def _definition(
    ctx: ParseContext, state: DsState, definition: GroupDefinition | BinDefinition
) -> FieldGroup | FieldBin | None:
    """群組或數值分組的定義 → IR（來源欄位以 id 表示）；來源不是資料源中的欄位時為 None。"""
    if isinstance(definition, GroupDefinition):
        source = state.by_name.get(definition.source)
        if source is None or (definition.has_other and definition.other is None):
            return None
        groups = [GroupItem(name=name, members=members) for name, members in definition.groups]
        return FieldGroup(source=source.id, groups=groups, other=definition.other)
    source = state.by_name.get(definition.expression.strip())
    if source is None:
        return None
    if definition.size is not None:
        return FieldBin(source=source.id, size=definition.size, peg=definition.peg)
    parameter = ctx.parameter(definition.size_parameter or "")
    if parameter is None:
        return None
    return FieldBin(source=source.id, size=ParamRef(parameter=parameter.id), peg=definition.peg)


def _with_definition(data_field: DataField, definition: FieldGroup | FieldBin) -> DataField:
    key = "group" if isinstance(definition, FieldGroup) else "bin"
    return data_field.model_copy(update={key: definition})


def resolve_definitions(ctx: ParseContext, state: DsState) -> None:
    """來源欄位在群組、數值分組之後才定義時：所有欄位都加入後再對應定義。"""
    for field_id, definition in list(state.definitions.items()):
        resolved = _definition(ctx, state, definition)
        del state.definitions[field_id]
        if resolved is None:
            continue
        for i, data_field in enumerate(state.fields):
            if data_field.id == field_id:
                updated = _with_definition(data_field, resolved)
                state.fields[i] = updated
                state.by_name[updated.name] = updated
                break


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
    if rule.kind in ("members", "combined"):
        return
    assert rule.kind != "members" and rule.kind != "combined"
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


def _foreign_refs(state: DsState, formula: str, seen: set[str] | None = None) -> bool:
    """公式以 [資料源].[欄位] 引用其他資料源的欄位（直接引用，或經由引用的計算欄位）。"""
    from tmig.formula.ast import walk
    from tmig.ir.scope import TABLE_OWNER

    try:
        node = parse_formula(formula)
    except FormulaSyntaxError:
        return False
    seen = set() if seen is None else seen
    own = {"Parameters", TABLE_OWNER, state.name.strip("[]"), state.caption}
    captions: dict[str, DataField] = {}
    for item in state.fields:
        captions.setdefault(item.caption, item)
    for found in walk(node):
        if not isinstance(found, FieldRef):
            continue
        if len(found.parts) == 2 and found.parts[0] not in own:
            return True
        name = found.parts[-1]
        referenced = state.by_name.get("[" + name.replace("]", "]]") + "]") or captions.get(name)
        if referenced is None or referenced.calc is None or referenced.id in seen:
            continue
        seen.add(referenced.id)
        if _foreign_refs(state, referenced.calc.formula, seen):
            return True
    return False


def analyze_calcs(ctx: ParseContext, state: DsState) -> None:
    """以公式引擎分析計算欄位：補上語法樹（附解析出的 id）與計算層級。"""
    resolve_definitions(ctx, state)
    others = [
        ForeignSource(other.name, other.caption, other.fields, other.levels)
        for other in ctx.datasources.values()
        if other is not state
    ]
    scope = DatasourceScope(state.name, state.fields, ctx.parameters, others)
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
            if not ctx.datasources_parsed and _foreign_refs(state, data_field.calc.formula):
                continue  # 引用還沒解析的資料源：資料源都解析完後再分析
            state.levels[data_field.id] = None
            entry = Unsupported(
                source_ref=data_field.source_ref,
                reason=f"計算欄位「{data_field.caption}」：{exc.message}"
                + ("（v0 尚不支援）" if exc.unsupported else ""),
                element="column",
                xml=data_field.calc.formula,
            )
            ctx.unsupported.append(entry)
            ctx.broken_calcs.append((data_field.id, entry))
            continue

        def resolve(ref: FieldRef) -> dict[str, str] | None:
            found = analyzer.resolve(ref)
            return {"target": found.kind, "id": found.id}

        level = _level(info.level, info.has_lod)
        state.levels[data_field.id] = level
        data_field.calc = Calc(
            formula=data_field.calc.formula, ast=to_json(node, resolve), level=level
        )
