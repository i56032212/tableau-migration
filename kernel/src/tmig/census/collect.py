"""功能盤點：讀 .twb XML，列出報表用到的功能，並檢查是否符合起步條件。

只讀 .twb，不讀資料；.twbx 內的萃取檔只列出名稱與大小。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path, PurePath
from typing import Literal

from lxml import etree

from tmig.census.formula_scan import scan_formula
from tmig.census.model import (
    ActionInfo,
    CensusReport,
    Check,
    DashboardInfo,
    DatasourceInfo,
    FilterInfo,
    FormulaInfo,
    PackagedFileInfo,
    ParameterInfo,
    ServerConnection,
    TableCalcInfo,
    WorksheetInfo,
)
from tmig.parser.connections import FILE_CLASSES, INTEGRATED_AUTH, NO_AUTH, initial_sql_users
from tmig.parser.refs import parse_instance, qualified_parts, shelf_operators
from tmig.parser.twbx import (
    attr,
    child,
    children,
    descendants,
    load_workbook_package,
    local_name,
    text_of,
)

AGG_DERIVATIONS = frozenset(
    {
        "Sum",
        "Avg",
        "Min",
        "Max",
        "Count",
        "CountD",
        "Median",
        "Attribute",
        "Stdev",
        "StdevP",
        "Var",
        "VarP",
        "Percentile",
        "Collect",
    }
)
INSTANCE_PREFIXES = frozenset(
    {
        "none",
        "usr",
        "attr",
        "sum",
        "avg",
        "min",
        "max",
        "cnt",
        "ctd",
        "med",
        "std",
        "stp",
        "var",
        "vrp",
        "pcto",
        "pctd",
        "cum",
        "rank",
        "diff",
        "pdiff",
        "win",
        "yr",
        "qr",
        "mn",
        "wk",
        "dy",
        "hr",
        "mi",
        "sc",
        "tyr",
        "tqr",
        "tmn",
        "twk",
        "tdy",
        "thr",
        "tmi",
        "tsc",
        "md",
        "my",
        "mdy",
        "wd",
    }
)
AGG_PREFIXES = frozenset(
    {"sum", "avg", "min", "max", "cnt", "ctd", "med", "attr", "std", "stp", "var", "vrp"}
)
COMMANDS = {"tsc:brush": "highlight", "tsc:tsl-filter": "filter"}
NEW_ACTIONS = {
    "nav-action": "navigate",
    "edit-parameter-action": "parameter",
    "edit-group-action": "set",
}
SUPPORTED_ACTIONS = frozenset({"filter", "highlight", "url", "navigate", "parameter", "set"})
"""已支援（篩選、網址、參數、切換、集合）或不影響數字（醒目提示）的動作種類。"""
MAP_MARKS = frozenset({"Map", "Multipolygon"})
DATA_FILE_CLASSES = frozenset({"hyper", "dataengine"})


PARAMETER_REF = re.compile(r"\[Parameters\]\.(\[[^\]]+\])")
"""公式中的參數參照，例如 [Parameters].[Parameter 1]。"""


def run_census(
    path: str | Path,
    *,
    extracts_omitted: bool = False,
    server_connections: Sequence[ServerConnection] = (),
) -> CensusReport:
    """盤點一份 .twb／.twbx。

    extracts_omitted：.twbx 下載時刻意不含萃取檔（只盤點結構），缺萃取檔不算不符合起步條件。
    server_connections：Tableau Server 上這份報表的連線設定（是否嵌入密碼），檢查檢視者是否用自己的
    資料庫帳號連線；沒有時只依 .twb 判斷。
    """
    package = load_workbook_package(path)
    root = package.root

    datasources: list[DatasourceInfo] = []
    parameters: list[ParameterInfo] = []
    formulas: list[FormulaInfo] = []
    calc_classes: Counter[str] = Counter()
    for ds in children(child(root, "datasources"), "datasource"):
        if ds.get("name") == "Parameters":
            parameters.extend(_parameters(ds))
            continue
        info, ds_formulas, classes = _datasource(ds)
        datasources.append(info)
        formulas.extend(ds_formulas)
        calc_classes.update(classes)

    worksheets = [_worksheet(ws) for ws in children(child(root, "worksheets"), "worksheet")]
    dashboards = [_dashboard(db) for db in children(child(root, "dashboards"), "dashboard")]
    actions = [
        _action(act)
        for act in children(child(root, "actions"))
        if local_name(act) in ("action", *NEW_ACTIONS)
    ]

    functions: Counter[str] = Counter()
    lod: Counter[str] = Counter()
    for formula in formulas:
        functions.update(formula.functions)
        lod.update(formula.lod)
    outside_v0 = sorted({name for formula in formulas for name in formula.outside_v0})

    inventory: Counter[str] = Counter()
    for section in ("worksheets", "dashboards"):
        inventory.update(local_name(el) for el in descendants(child(root, section)))

    version = root.get("version")
    build = root.get("source-build")
    report = CensusReport(
        file=str(package.source),
        twb=package.twb_name,
        packaged=package.source.suffix.lower() == ".twbx",
        tableau_version=f"{version}（{build}）" if version and build else version or build,
        files=[PackagedFileInfo(name=f.name, size=f.size) for f in package.files],
        datasources=datasources,
        parameters=parameters,
        worksheets=worksheets,
        dashboards=dashboards,
        actions=actions,
        formulas=formulas,
        functions=dict(sorted(functions.items(), key=lambda item: (-item[1], item[0]))),
        functions_outside_v0=outside_v0,
        lod=dict(sorted(lod.items())),
        calc_classes=dict(sorted(calc_classes.items())),
        checks=[],
        grade="A",
        grade_reasons=[],
        element_inventory=dict(sorted(inventory.items(), key=lambda item: (-item[1], item[0]))),
    )
    report.checks = _checks(
        report, extracts_omitted=extracts_omitted, server_connections=server_connections
    )
    report.grade, report.grade_reasons = _grade(report)
    return report


# ---------------------------------------------------------------- 資料源與參數


def _tables(element: etree._Element | None) -> list[str]:
    tables = {
        rel.get("table", "")
        for rel in descendants(element, "relation")
        if rel.get("type") == "table" and rel.get("table")
    }
    return sorted(tables)


def _datasource(ds: etree._Element) -> tuple[DatasourceInfo, list[FormulaInfo], Counter[str]]:
    name = ds.get("name", "")
    caption = ds.get("caption") or name
    conn = child(ds, "connection")
    named = [c for nc in descendants(conn, "named-connection") for c in children(nc, "connection")]
    if named:
        classes = sorted({c.get("class", "") for c in named} - {""})
    else:
        classes = [conn.get("class", "")] if conn is not None and conn.get("class") else []

    extract = child(ds, "extract")
    extract_enabled = None if extract is None else extract.get("enabled") != "false"
    extract_files: list[str] = []
    extract_tables: list[str] = []
    data_named = [c for c in named if c.get("class") in DATA_FILE_CLASSES]
    if extract is not None and extract_enabled:
        extract_files = [
            c.get("dbname", "") for c in descendants(extract, "connection") if c.get("dbname")
        ]
        extract_tables = _tables(extract)
    elif data_named:
        extract_files = [c.get("dbname", "") for c in data_named if c.get("dbname")]
        extract_tables = _tables(conn)
    data_in_hyper = bool(extract_files) or bool(extract is not None and extract_enabled)
    data_files: list[str] = []
    if not data_in_hyper:
        for c in named:
            if c.get("class") == "excel-direct" and c.get("filename"):
                data_files.append(c.get("filename", ""))
            elif c.get("class") in ("textscan", "ogrdirect") and c.get("filename"):
                directory = (c.get("directory") or "").replace("\\", "/").rstrip("/")
                filename = c.get("filename", "")
                data_files.append(f"{directory}/{filename}" if directory else filename)

    relation_types = sorted({rel.get("type", "") for rel in descendants(conn, "relation")} - {""})
    graph = child(ds, "object-graph")
    logical_tables = len(children(child(graph, "objects"), "object"))

    formulas: list[FormulaInfo] = []
    classes_used: Counter[str] = Counter()
    columns = children(ds, "column")
    for column in columns:
        calc = child(column, "calculation")
        if calc is None:
            continue
        calc_class = calc.get("class", "")
        classes_used[calc_class] += 1
        formula = calc.get("formula")
        if calc_class != "tableau" or not formula:
            continue
        scan = scan_formula(formula)
        formulas.append(
            FormulaInfo(
                datasource=caption,
                field=column.get("caption") or column.get("name", "").strip("[]"),
                name=column.get("name", ""),
                formula=formula,
                functions=sorted(scan.functions),
                lod=list(scan.lod),
                table_calc_functions=scan.table_calc_functions,
                user_functions=scan.user_functions,
                outside_v0=scan.outside_v0,
                scan_errors=list(scan.errors),
            )
        )

    ds_filters = children(ds, "filter")
    filter_users: set[str] = set()
    filter_params: set[str] = set()
    for flt in ds_filters:
        for group in descendants(flt, "groupfilter"):
            expression = group.get("expression")
            if expression:
                found = scan_formula(expression).user_functions
                filter_users.update(found)
                if found:
                    filter_params.update(PARAMETER_REF.findall(expression))
    user_filters = [
        g.get("caption") or g.get("name", "").strip("[]")
        for g in children(ds, "group")
        if attr(g, "ui-builder") == "identity-set"
    ]

    # 來源連線的驗證方式與 Initial SQL（萃取的來源連線只在更新萃取時使用）
    sources = named or (
        [conn]
        if conn is not None and conn.get("class") not in (None, "", "federated", "sqlproxy")
        else []
    )
    initial_sql = [text for c in sources if (text := (c.get("one-time-sql") or "").strip())]
    initial_user = {name for text in initial_sql for name in initial_sql_users(text)}

    info = DatasourceInfo(
        name=name,
        caption=caption,
        published=conn is not None and conn.get("class") == "sqlproxy",
        connection_classes=classes,
        data_in_hyper=data_in_hyper,
        extract_enabled=extract_enabled,
        extract_files=extract_files,
        extract_tables=extract_tables,
        data_files=data_files,
        source_relation_types=relation_types,
        logical_tables=logical_tables,
        custom_sql="text" in relation_types,
        field_count=len(columns),
        calc_count=sum(classes_used.values()),
        # 篩選動作、排除選取、工具提示產生的隱藏群組（user:auto-column）已在擴充階段支援；
        # 使用者篩選（identity-set）與集合（filter-group）也已支援，剩下合併欄位等其他群組
        groups_or_sets=sum(
            1
            for g in children(ds, "group")
            if attr(g, "auto-column") is None
            and attr(g, "ui-builder") not in ("identity-set", "filter-group")
        ),
        user_filters=user_filters,
        datasource_filters=len(ds_filters),
        extract_filters=len(list(descendants(extract, "filter"))),
        filter_user_functions=sorted(filter_users),
        filter_parameters=sorted(filter_params),
        authentication=sorted({c.get("authentication", "") for c in sources} - {""}),
        server_auth_modes=sorted(
            {c.get("workgroup-auth-mode", "") for c in sources} - {"", "as-is"}
        ),
        initial_sql=bool(initial_sql),
        initial_sql_user=sorted(initial_user),
    )
    return info, formulas, classes_used


def _parameters(ds: etree._Element) -> list[ParameterInfo]:
    result: list[ParameterInfo] = []
    for column in children(ds, "column"):
        result.append(
            ParameterInfo(
                name=column.get("name", ""),
                caption=column.get("caption") or column.get("name", "").strip("[]"),
                datatype=column.get("datatype", ""),
                domain=column.get("param-domain-type", ""),
                value=column.get("value"),
            )
        )
    return result


# ---------------------------------------------------------------- 工作表


def _filter(flt: etree._Element, instances: dict[str, etree._Element]) -> FilterInfo:
    column = flt.get("column", "")
    kind_class = flt.get("class", "")
    groups = list(descendants(flt, "groupfilter"))
    functions = {group.get("function") for group in groups}
    detail = ""
    if kind_class == "relative-date":
        kind = "relative_date"
    elif kind_class == "quantitative":
        instance = instances.get(column)
        derivation = instance.get("derivation") if instance is not None else None
        if derivation in AGG_DERIVATIONS:
            kind = "measure_range"
        elif derivation == "User":
            kind = "calc_range"
        else:
            parts = qualified_parts(column)
            parsed = parse_instance(parts[-1], INSTANCE_PREFIXES) if parts else None
            is_agg = parsed is not None and bool(set(parsed.prefixes) & AGG_PREFIXES)
            kind = "measure_range" if is_agg else "range"
        included = flt.get("included-values")
        detail = f"included-values={included}" if included else ""
    elif kind_class == "categorical":
        if "end" in functions:
            kind = "top_n"
            top = next(group for group in groups if group.get("function") == "end")
            detail = f"{top.get('end', '')} {top.get('count', '')}".strip()
        elif "filter" in functions:
            kind = "condition"
        else:
            kind = "categorical"
            members = sum(1 for group in groups if group.get("function") == "member")
            enumeration = next(
                (attr(g, "ui-enumeration") for g in groups if attr(g, "ui-enumeration")), None
            )
            detail = f"{members} 個值" + (f"，{enumeration}" if enumeration else "")
    else:
        kind = kind_class or "unknown"
    return FilterInfo(column=column, kind=kind, context=flt.get("context") == "true", detail=detail)


def _worksheet(ws: etree._Element) -> WorksheetInfo:
    table = child(ws, "table")
    view = child(table, "view")
    names = [d.get("name", "") for d in children(child(view, "datasources"), "datasource")]
    names = [n for n in names if n != "Parameters"]

    instances: dict[str, etree._Element] = {}
    for dependency in children(view, "datasource-dependencies"):
        ds_name = dependency.get("datasource", "")
        for instance in children(dependency, "column-instance"):
            instances[f"[{ds_name}].{instance.get('name', '')}"] = instance
    table_calcs = []
    for instance in instances.values():
        calc = child(instance, "table-calc")
        if calc is not None:
            table_calcs.append(
                TableCalcInfo(
                    column=instance.get("name", ""),
                    type=calc.get("type"),
                    ordering_type=calc.get("ordering-type"),
                )
            )

    filters = [_filter(flt, instances) for flt in children(view, "filter")]
    panes = children(child(table, "panes"), "pane")
    marks = sorted(
        {mark.get("class", "") for pane in panes if (mark := child(pane, "mark")) is not None}
    )
    encodings = sorted(
        {local_name(e) for pane in panes for e in children(child(pane, "encodings"))}
    )
    rows = text_of(child(table, "rows"))
    cols = text_of(child(table, "cols"))
    used = [rows, cols, *(f.column for f in filters)]
    used += [e.get("column", "") for pane in panes for e in children(child(pane, "encodings"))]
    mnv = any(":Measure Names]" in text or "[Multiple Values]" in text for text in used)
    maps = bool(set(marks) & MAP_MARKS) or "(generated)]" in rows + cols
    return WorksheetInfo(
        name=ws.get("name", ""),
        datasources=names,
        blending=_blends(view, names, used),
        mark_types=marks,
        pane_count=len(panes),
        encodings=encodings,
        rows=rows,
        cols=cols,
        shelf_operators=sorted(shelf_operators(rows) | shelf_operators(cols)),
        measure_names_values=mnv,
        filters=filters,
        table_calcs=table_calcs,
        sorts=[s.get("class", "") for s in children(view, "sort")],
        reference_lines=len(list(descendants(table, "reference-line"))),
        trend_lines=len(list(descendants(table, "trend-line"))),
        maps=maps,
    )


# ---------------------------------------------------------------- dashboard 與動作


def _sizing(size: etree._Element | None) -> str:
    if size is None:
        return "automatic"
    mode = size.get("sizing-mode")
    if mode:
        return mode
    fixed_width = size.get("minwidth") and size.get("minwidth") == size.get("maxwidth")
    fixed_height = size.get("minheight") and size.get("minheight") == size.get("maxheight")
    return "fixed" if fixed_width and fixed_height else "range"


def _dashboard(db: etree._Element) -> DashboardInfo:
    size = child(db, "size")
    zone_types: Counter[str] = Counter()
    sheets: list[str] = []
    for zone in descendants(child(db, "zones"), "zone"):
        kind = attr(zone, "type-v2") or zone.get("type")
        if kind is None:
            kind = "viz" if zone.get("name") else "unknown"
        zone_types[kind] += 1
        if kind == "viz":
            sheets.append(zone.get("name", ""))
    return DashboardInfo(
        name=db.get("name", ""),
        story=db.get("type") == "storyboard",
        sizing=_sizing(size),
        size={str(k): str(v) for k, v in size.attrib.items()} if size is not None else {},
        zone_types=dict(sorted(zone_types.items())),
        worksheets=sheets,
        device_layouts=child(db, "devicelayouts") is not None,
    )


def _action(act: etree._Element) -> ActionInfo:
    activation = child(act, "activation")
    source = child(act, "source")
    command = child(act, "command")
    raw = command.get("command", "") if command is not None else ""
    if local_name(act) in NEW_ACTIONS:
        raw = NEW_ACTIONS[local_name(act)]
    elif not raw:
        # URL 動作沒有 <command>，只有 <link>
        raw = "url" if child(act, "link") is not None else "unknown"
    where = ""
    if source is not None:
        parts = (source.get("dashboard"), source.get("worksheet"), source.get("datasource"))
        where = " / ".join(v for v in parts if v)
    return ActionInfo(
        caption=act.get("caption") or act.get("name", ""),
        kind=COMMANDS.get(raw, raw),
        activation=activation.get("type") if activation is not None else None,
        source=where,
    )


# ---------------------------------------------------------------- 起步條件與分級


RELATION_TEXT = {
    "table": "資料表",
    "text": "自訂 SQL",
    "join": "join",
    "pivot": "樞紐",
    "union": "聯集",
    "project": "合併的欄位",
}


def _quote(names: list[str]) -> str:
    return "、".join(f"「{name}」" for name in names)


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").lower()


def _checks(
    r: CensusReport,
    *,
    extracts_omitted: bool = False,
    server_connections: Sequence[ServerConnection] = (),
) -> list[Check]:
    # 多個資料源（各工作表用各自的資料源）已支援：每張工作表查詢自己的資料源，
    # 同一張工作表混用多個資料源（Data Blending）另外檢查
    checks: list[Check] = []
    dss = r.datasources

    packaged = r.packaged
    packaged_paths = {_norm(f.name) for f in r.files}
    packaged_names = {PurePath(_norm(f.name)).name for f in r.files}
    problems: list[str] = []
    sources: list[str] = []
    for ds in dss:
        missing = [
            f
            for f in ds.extract_files
            if _norm(f) not in packaged_paths and PurePath(_norm(f)).name not in packaged_names
        ]
        if ds.published:
            problems.append(f"「{ds.caption}」是 Published Data Source（需先下載 .tdsx 併入報表）")
        elif not ds.data_in_hyper:
            # Live 連線（第三階段）：連到資料庫的可以下推查詢；Excel、文字檔在 .twbx 內時匯入資料庫。
            # 連線類型與關聯方式是否支援由解析器判斷（列入 unsupported）
            classes = [c for c in ds.connection_classes if c]
            files = [c for c in classes if c in FILE_CLASSES]
            absent = [
                f
                for f in ds.data_files
                if _norm(f) not in packaged_paths and PurePath(_norm(f)).name not in packaged_names
            ]
            if files and not set(files) <= {"excel-direct", "textscan", "ogrdirect"}:
                problems.append(f"「{ds.caption}」是連到檔案（{'、'.join(files)}）的 Live 連線")
            elif files and not packaged:
                problems.append(
                    f"「{ds.caption}」連到檔案（{'、'.join(ds.data_files)}），.twb 不含檔案，請用 .twbx"
                )
            elif files and absent:
                problems.append(f"「{ds.caption}」的檔案不在 .twbx 內：{'、'.join(absent)}")
            elif files:
                sources.append(
                    f"「{ds.caption}」：{'、'.join(ds.data_files)}（檔案，匯入時讀進資料庫）"
                )
            else:
                sources.append(
                    f"「{ds.caption}」：Live（{'、'.join(classes) or '未知'}），部署時需提供連線設定"
                )
        elif any(f.lower().endswith(".tde") for f in ds.extract_files):
            problems.append(f"「{ds.caption}」的萃取是舊版 .tde，需先在 Tableau 升級為 .hyper")
        elif packaged and missing and not extracts_omitted:
            problems.append(f"「{ds.caption}」的萃取檔不在 .twbx 內：{'、'.join(missing)}")
        else:
            sources.append(f"「{ds.caption}」：{'、'.join(ds.extract_files) or '萃取'}")
    ok_detail = "；".join(sources)
    extracts = [d for d in dss if d.data_in_hyper]
    if extracts and packaged and extracts_omitted:
        ok_detail += "（下載時未含萃取檔，萃取檔是否齊全待下載正式檔確認）"
    if extracts and not packaged:
        ok_detail += "（.twb 不含資料，正式盤點請用 .twbx）"
    checks.append(
        Check(
            key="data_access",
            title="資料可以取得（萃取或 Live 連線）",
            status="fail" if problems or not dss else "pass",
            detail="；".join(problems) or ok_detail or "沒有資料源",
        )
    )

    problems = []
    notes: list[str] = []
    for ds in dss:
        if ds.published:
            continue  # 結構在 Published Data Source 中（資料可以取得的檢查已列出）
        if ds.logical_tables > 1:
            # Relationship 模型（N6）：不支援的寫法（例如關聯條件含計算）由解析器列為不支援
            kind = "萃取" if ds.data_in_hyper else "Live"
            notes.append(
                f"「{ds.caption}」：Relationship 模型（{ds.logical_tables} 張邏輯資料表，{kind}）"
            )
        elif not ds.data_in_hyper:
            # Live：資料表、自訂 SQL 與 join 都下推到來源資料庫（其他關聯方式由解析器列為不支援）
            kinds = "、".join(RELATION_TEXT.get(k, k) for k in ds.source_relation_types)
            notes.append(f"「{ds.caption}」：Live 的{kinds or '資料表'}")
        elif len(ds.extract_tables) != 1:
            count = len(ds.extract_tables)
            problems.append(
                f"「{ds.caption}」有 {count} 張萃取資料表"
                if count
                else f"「{ds.caption}」找不到萃取資料表"
            )
        else:
            note = f"「{ds.caption}」：{ds.extract_tables[0]}"
            if "join" in ds.source_relation_types:
                note += "（來源為 join，萃取存成單表）"
            notes.append(note)
    checks.append(
        Check(
            key="single_table",
            title="單表萃取、Relationship 模型，或 Live 的資料表、自訂 SQL、join",
            status="fail" if problems or not dss else "pass",
            detail="；".join(problems) or "；".join(notes) or "沒有資料源",
        )
    )

    checks.append(identity_check(dss, server_connections))
    checks.append(_rls_check(r, dss))

    checks.append(_blending_check(r, dss))

    boards = [d for d in r.dashboards if not d.story]
    loose = [d for d in boards if d.sizing != "fixed"]
    if not boards:
        status, detail = "warn", "沒有 dashboard"
    elif loose:
        # 自動與範圍尺寸依視窗大小重排（I1），版面為近似，所以只列為需注意
        status = "warn"
        detail = "；".join(f"「{d.name}」為 {d.sizing}（依視窗大小重排）" for d in loose)
    else:
        sizes = "、".join(
            f"「{d.name}」{d.size.get('maxwidth', '?')}×{d.size.get('maxheight', '?')}"
            for d in boards
        )
        status, detail = "pass", sizes
    checks.append(
        Check(key="fixed_dashboards", title="dashboard 為固定尺寸", status=status, detail=detail)
    )

    if r.functions_outside_v0:
        detail = f"不在 v0 函式集：{'、'.join(r.functions_outside_v0)}；需補任務，或換一份報表"
        checks.append(
            Check(key="v0_functions", title="函式都在 v0 函式集", status="warn", detail=detail)
        )
    else:
        checks.append(
            Check(
                key="v0_functions",
                title="函式都在 v0 函式集",
                status="pass",
                detail="公式只用到 v0 函式集",
            )
        )

    features = features_outside_v0(r)
    checks.append(
        Check(
            key="v0_features",
            title="功能都在 IR v0 範圍內",
            status="warn" if features else "pass",
            detail="；".join(features) + "；需補 IR 與實作，或換一份報表"
            if features
            else "沒有發現 IR v0 未涵蓋的功能",
        )
    )
    return checks


IDENTITY_KEY = "viewer_identity"
IDENTITY_TITLE = "連線不依賴檢視者的身分（資料庫帳號、Initial SQL）"
SAME_DATA = "移轉後改用唯讀服務帳號連線，每個人會看到相同的資料"
INTEGRATED = "Windows 整合驗證或 Kerberos"


def viewer_auth(value: str | None) -> str | None:
    """驗證方式可能以檢視者自己的身分登入資料庫時，回傳名稱；否則 None。"""
    text = (value or "").lower()
    if any(key in text for key in INTEGRATED_AUTH):
        return INTEGRATED
    if "oauth" in text:
        return "OAuth"
    return None


def _match_connection(dss: list[DatasourceInfo], conn: ServerConnection) -> list[DatasourceInfo]:
    """伺服器上的連線屬於哪些資料源：先比資料源名稱，比不到再比連線類型。"""
    candidates = [d for d in dss if not d.published]
    target = (conn.published_datasource or conn.datasource or "").strip().casefold()
    if target:
        named = [d for d in candidates if target in (d.caption.casefold(), d.name.casefold())]
        if named:
            return named
    return [d for d in candidates if conn.type in d.connection_classes]


def identity_check(dss: list[DatasourceInfo], server: Sequence[ServerConnection] = ()) -> Check:
    """資料是否依檢視者的身分而不同：平台以部署環境提供的唯讀服務帳號連線、不執行 Initial SQL，
    原本靠資料庫端（檢視者自己的帳號、Initial SQL 帶入登入者）過濾的資料，移轉後每個人看到的都一樣。

    - 不符合：Live 連線的 Initial SQL 用到 [TableauServerUser]、要求檢視者輸入帳號（.twb 的
      workgroup-auth-mode=prompt，或伺服器上沒有嵌入密碼）、以檢視者的身分模擬（impersonation）
    - 需注意：Windows 整合驗證、Kerberos 或 OAuth（Tableau Server 可能以服務帳號或檢視者的身分連線）、
      其他 Initial SQL（平台不會執行）、萃取來源的 Initial SQL（更新萃取時不會執行）、
      伺服器上沒有嵌入密碼但無法確定屬於哪個資料源的連線

    伺服器的連線設定是目前的狀態：確認嵌入了密碼時，.twb 中記錄的 prompt 與 OAuth 不列。
    """
    failed: dict[str, None] = {}
    warned: dict[str, None] = {}
    prompted: set[str] = set()
    integrated: set[str] = set()
    oauth: dict[str, str] = {}
    """OAuth 連線的資料源 → 說明；伺服器上確認嵌入了登入資訊時不列。"""
    embedded: set[str] = set()
    """伺服器上確認嵌入了密碼的資料源（伺服器的設定是目前的狀態，優先於 .twb 中記錄的設定）。"""
    for conn in server:
        matched = _match_connection(dss, conn) if conn.embed_password else []
        if len(matched) == 1:
            embedded.add(matched[0].caption)
    for ds in dss:
        if ds.published:
            continue  # 連線在 Published Data Source 中（資料可以取得的檢查已列出）
        name = f"「{ds.caption}」"
        live = not ds.data_in_hyper
        if ds.initial_sql_user:
            params = "、".join(ds.initial_sql_user)
            if live:
                failed[
                    f"{name}的 Initial SQL 用到 {params}：資料庫依檢視者的身分過濾資料，"
                    f"平台不會執行 Initial SQL，{SAME_DATA}；需改用列層級權限（使用者函式或使用者篩選）"
                ] = None
            else:
                warned[
                    f"{name}的萃取來源有 Initial SQL 用到 {params}：平台更新萃取時不會執行，"
                    "更新後的資料可能與 Tableau 不同"
                ] = None
        elif ds.initial_sql:
            warned[
                f"{name}有 Initial SQL：平台不會執行，請確認查詢結果不依賴它（驗證比對會顯示差異）"
            ] = None
        if not live:
            continue  # 萃取：檢視時讀萃取檔，與檢視者的帳號無關
        for mode in ds.server_auth_modes:
            text = mode.lower()
            if text == "prompt":
                if ds.caption in embedded:
                    continue  # 之後在伺服器上嵌入了密碼
                prompted.add(ds.caption)
                failed[
                    f"{name}在 Tableau Server 上要求檢視者輸入自己的資料庫帳號，"
                    f"資料庫端的權限隨人不同；{SAME_DATA}"
                ] = None
            elif "imperson" in text:
                failed[
                    f"{name}以檢視者的身分模擬（{mode}）查詢資料庫，資料庫端的權限隨人不同；{SAME_DATA}"
                ] = None
            else:
                warned[
                    f"{name}在 Tableau Server 上的驗證設定為 {mode}：請確認是否以檢視者的身分連線"
                ] = None
        for auth in ds.authentication:
            kind = viewer_auth(auth)
            if kind == INTEGRATED:
                integrated.add(ds.caption)
                warned[
                    f"{name}以 {INTEGRATED}（{auth}）連線：Tableau Server 若設定了 Kerberos 委派，"
                    f"是以檢視者自己的帳號查詢，資料庫端的權限隨人不同，{SAME_DATA}；請與 DBA 確認"
                ] = None
            elif kind is not None:
                oauth[ds.caption] = (
                    f"{name}以 {kind}（{auth}）連線：Tableau Server 上若要求檢視者自己登入，"
                    f"資料庫端的權限隨人不同，{SAME_DATA}；是否嵌入登入資訊請以伺服器盤點確認"
                )

    for caption in embedded:
        oauth.pop(caption, None)  # 伺服器上嵌入了登入資訊
    for conn in server:
        if conn.type in FILE_CLASSES | {"sqlproxy"}:
            continue
        if conn.embed_password is not False or (conn.authentication or "").lower() in NO_AUTH:
            continue
        matched = _match_connection(dss, conn)
        live_matched = [d for d in matched if not d.data_in_hyper]
        if not live_matched:
            continue  # 萃取的來源連線：檢視時讀萃取檔
        if len(matched) > 1:
            label = conn.published_datasource or conn.datasource or conn.type
            warned[
                f"Tableau Server 上「{label}」的 {conn.type} 連線沒有嵌入密碼，無法確定是哪個資料源；"
                f"若是 Live 連線，檢視者用自己的資料庫帳號連線，{SAME_DATA}"
            ] = None
            continue
        ds = live_matched[0]
        if ds.caption in prompted:
            continue
        if viewer_auth(conn.authentication) == INTEGRATED or ds.caption in integrated:
            if ds.caption not in integrated:
                integrated.add(ds.caption)
                warned[
                    f"「{ds.caption}」在 Tableau Server 上以 {INTEGRATED} 連線：若設定了 Kerberos 委派，"
                    f"是以檢視者自己的帳號查詢，{SAME_DATA}；請與 DBA 確認"
                ] = None
            continue
        prompted.add(ds.caption)
        oauth.pop(ds.caption, None)
        failed[
            f"「{ds.caption}」在 Tableau Server 上沒有嵌入密碼，檢視者用自己的資料庫帳號連線，"
            f"資料庫端的權限隨人不同；{SAME_DATA}"
        ] = None
    for caption, message in oauth.items():
        if caption not in prompted:
            warned[message] = None

    if failed or warned:
        return Check(
            key=IDENTITY_KEY,
            title=IDENTITY_TITLE,
            status="fail" if failed else "warn",
            detail="；".join([*failed, *warned]),
        )
    if not any(not d.published and not d.data_in_hyper for d in dss):
        detail = "沒有 Live 連線（萃取的資料不依賴檢視者的帳號）"
    elif server:
        detail = "已比對 Tableau Server 的連線設定：Live 連線都嵌入了帳號密碼"
    else:
        detail = (
            "Live 連線沒有以檢視者身分連線的設定；是否嵌入密碼請以伺服器盤點"
            "（tools/tableau_server）確認"
        )
    return Check(key=IDENTITY_KEY, title=IDENTITY_TITLE, status="pass", detail=detail)


def _rls_check(r: CensusReport, dss: list[DatasourceInfo]) -> Check:
    """列層級權限（P1 已支援）：列出用到使用者函式的地方，提醒驗證與上線需要的設定。"""
    users: dict[str, set[str]] = {}
    params: dict[str, set[str]] = {}
    for formula in r.formulas:
        for fn in formula.user_functions:
            users.setdefault(fn, set()).add(formula.field)
        if formula.user_functions:
            for param in PARAMETER_REF.findall(formula.formula):
                params.setdefault(formula.field, set()).add(param)
    for ds in dss:
        where = f"{ds.caption} 的資料源篩選"
        for fn in ds.filter_user_functions:
            users.setdefault(fn, set()).add(where)
        for param in ds.filter_parameters:
            params.setdefault(where, set()).add(param)
    parts = [f"{fn}（{'、'.join(sorted(where))}）" for fn, where in sorted(users.items())]
    parts += [f"使用者篩選「{name}」" for ds in dss for name in ds.user_filters]
    if not parts:
        return Check(
            key="no_rls", title="列層級權限（RLS）", status="pass", detail="沒有使用者函式"
        )
    parts += [
        f"「{where}」用到參數 {'、'.join(sorted(names))}：使用者改參數就能改變可見範圍"
        "（與 Tableau 相同），請確認是否刻意如此"
        for where, names in sorted(params.items())
    ]
    parts.append("驗證時在 cases.yaml 設定 user（抓取預期結果的帳號）")
    return Check(key="no_rls", title="列層級權限（RLS）", status="warn", detail="；".join(parts))


def features_outside_v0(r: CensusReport) -> list[str]:
    """IR v0 未涵蓋的功能，每項寫成「功能（用到的工作表或 dashboard）」。"""
    features: list[str] = []

    def add(label: str, sheets: list[str]) -> None:
        if sheets:
            features.append(f"{label}（{'、'.join(sheets)}）")

    ws = r.worksheets
    # Measure Names／Values、多個量值與雙軸、自動尺寸、裝置專屬版面已在擴充階段支援（各標記卡的詳細程度
    # 不同時，解析時另列為不支援）
    # 相對日期與條件篩選已在擴充階段支援（K4）；參考線已支援（J4，只影響圖表，畫不出來的種類解析時記為略過）
    add("趨勢線", [w.name for w in ws if w.trend_lines])
    add("地圖", [w.name for w in ws if w.maps])
    # Story 已支援（故事點的篩選動作、篩選與選取）；認不得的狀態解析時另列為不支援
    # 群組（categorical-bin）與數值分組（bin）已在擴充階段支援（K1）
    special = {
        k: v for k, v in r.calc_classes.items() if k not in ("tableau", "bin", "categorical-bin")
    }
    groups = sum(d.groups_or_sets for d in r.datasources)
    if special or groups:
        parts = [f"{k} {v}" for k, v in special.items()] + ([f"group {groups}"] if groups else [])
        features.append(f"合併欄位等特殊欄位（{'、'.join(parts)}）")
    # 篩選、網址、參數、切換與集合動作已支援（L1、L2），醒目提示不影響數字
    kinds = Counter(a.kind for a in r.actions if a.kind not in SUPPORTED_ACTIONS)
    if kinds:
        features.append(
            f"Dashboard action（{'、'.join(f'{k} {v}' for k, v in sorted(kinds.items()))}）"
        )
    return features


Grade = Literal["A", "B", "C"]


def _blends(view: etree._Element | None, names: list[str], used: list[str]) -> bool:
    """工作表是否混合次要資料源（Data Blending）：欄列架、標記卡或 Measure Values 用到其他資料源的欄位，
    或以次要資料源的欄位篩選。對應到主要資料源欄位的跨資料源篩選（<target field>）不是混合。"""
    if len(names) < 2:
        return False
    texts = list(used)
    for flt in children(view, "filter"):
        column = flt.get("column", "")
        if column.endswith("[:Measure Names]"):
            texts += [g.get("member", "") for g in descendants(flt, "groupfilter")]
        target = child(flt, "target")
        if target is not None and target.get("field", "").startswith(f"[{names[0]}]."):
            texts = [t for t in texts if t != column]  # 跨資料源篩選
    return any(f"[{name}]." in text for name in names[1:] for text in texts)


def _blending_check(r: CensusReport, dss: list[DatasourceInfo]) -> Check:
    """Data Blending 已支援次要資料源只用量值的工作表：主要與次要資料源可以是萃取、檔案、跨資料庫的 join 或
    Live 連線（Live 連線與 Tableau 相同各自查詢後合併）；Published Data Source 要先下載 .tdsx 併入報表。"""
    blended = [w for w in r.worksheets if w.blending]
    title = "Data Blending（Published Data Source 須先併入報表）"
    if not blended:
        return Check(
            key="no_blending", title=title, status="pass", detail="每張工作表只用一個資料源"
        )
    by_name = {d.name: d for d in dss}
    published = sorted(
        {
            by_name[name].caption
            for w in blended
            for name in w.datasources
            if name in by_name and by_name[name].published
        }
    )
    detail = f"用到 Blending 的工作表：{_quote([w.name for w in blended])}"
    if published:
        detail += f"；Published Data Source：{_quote(published)}"
    else:
        detail += "（次要資料源只支援彙總的量值，解析時另列不支援的用法）"
    return Check(
        key="no_blending", title=title, status="fail" if published else "warn", detail=detail
    )


def _blending_supported(r: CensusReport) -> bool:
    return next((c.status for c in r.checks if c.key == "no_blending"), "pass") != "fail"


def _grade(r: CensusReport) -> tuple[Grade, list[str]]:
    c_reasons: list[str] = []
    blending = any(w.blending for w in r.worksheets)
    if blending and not _blending_supported(r):
        c_reasons.append("Data Blending")
    if any(d.custom_sql for d in r.datasources):
        c_reasons.append("自訂 SQL")
    if any(w.maps for w in r.worksheets):
        c_reasons.append("地圖")

    b_reasons: list[str] = []
    if blending and _blending_supported(r):
        b_reasons.append("Data Blending")
    if r.lod:
        b_reasons.append("LOD：" + "、".join(f"{k} {v}" for k, v in r.lod.items()))
    quick = sum(len(w.table_calcs) for w in r.worksheets)
    in_formulas = sum(1 for f in r.formulas if f.table_calc_functions)
    if quick or in_formulas:
        b_reasons.append(f"Table Calculation：工作表上 {quick} 處、計算欄位 {in_formulas} 個")

    if c_reasons:
        return "C", c_reasons + b_reasons
    if b_reasons:
        return "B", b_reasons
    return "A", ["只有一般計算"]
