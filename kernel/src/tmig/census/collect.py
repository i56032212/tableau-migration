"""功能盤點：讀 .twb XML，列出報表用到的功能，並檢查是否符合起步條件。

只讀 .twb，不讀資料；.twbx 內的萃取檔只列出名稱與大小。
"""

from __future__ import annotations

from collections import Counter
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
    TableCalcInfo,
    WorksheetInfo,
)
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
SUPPORTED_ACTIONS = frozenset({"filter", "highlight", "url", "navigate", "parameter"})
"""擴充階段已支援（L1、L2）或不影響數字的動作種類。"""
MAP_MARKS = frozenset({"Map", "Multipolygon"})
DATA_FILE_CLASSES = frozenset({"hyper", "dataengine"})


def run_census(path: str | Path, *, extracts_omitted: bool = False) -> CensusReport:
    """盤點一份 .twb／.twbx。

    extracts_omitted：.twbx 下載時刻意不含萃取檔（只盤點結構），缺萃取檔不算不符合起步條件。
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
    report.checks = _checks(report, extracts_omitted=extracts_omitted)
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
    for flt in ds_filters:
        for group in descendants(flt, "groupfilter"):
            expression = group.get("expression")
            if expression:
                filter_users.update(scan_formula(expression).user_functions)

    info = DatasourceInfo(
        name=name,
        caption=caption,
        published=conn is not None and conn.get("class") == "sqlproxy",
        connection_classes=classes,
        data_in_hyper=data_in_hyper,
        extract_enabled=extract_enabled,
        extract_files=extract_files,
        extract_tables=extract_tables,
        source_relation_types=relation_types,
        logical_tables=logical_tables,
        custom_sql="text" in relation_types,
        field_count=len(columns),
        calc_count=sum(classes_used.values()),
        # 篩選動作、排除選取、工具提示產生的隱藏群組（user:auto-column）已在擴充階段支援
        groups_or_sets=sum(1 for g in children(ds, "group") if attr(g, "auto-column") is None),
        datasource_filters=len(ds_filters),
        extract_filters=len(list(descendants(extract, "filter"))),
        filter_user_functions=sorted(filter_users),
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
        blending=len(names) > 1,
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


def _quote(names: list[str]) -> str:
    return "、".join(f"「{name}」" for name in names)


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").lower()


def _checks(r: CensusReport, *, extracts_omitted: bool = False) -> list[Check]:
    checks: list[Check] = []
    dss = r.datasources

    if len(dss) == 1:
        checks.append(
            Check(
                key="single_datasource",
                title="單一資料源",
                status="pass",
                detail=_quote([dss[0].caption]),
            )
        )
    else:
        detail = (
            "沒有資料源" if not dss else f"有 {len(dss)} 個：{_quote([d.caption for d in dss])}"
        )
        checks.append(
            Check(key="single_datasource", title="單一資料源", status="fail", detail=detail)
        )

    packaged = r.packaged
    packaged_paths = {_norm(f.name) for f in r.files}
    packaged_names = {PurePath(_norm(f.name)).name for f in r.files}
    problems: list[str] = []
    for ds in dss:
        missing = [
            f
            for f in ds.extract_files
            if _norm(f) not in packaged_paths and PurePath(_norm(f)).name not in packaged_names
        ]
        if ds.published:
            problems.append(f"「{ds.caption}」是 Published Data Source")
        elif not ds.data_in_hyper:
            problems.append(f"「{ds.caption}」是 Live 連線，資料不在 .twbx 內")
        elif any(f.lower().endswith(".tde") for f in ds.extract_files):
            problems.append(f"「{ds.caption}」的萃取是舊版 .tde，需先在 Tableau 升級為 .hyper")
        elif packaged and missing and not extracts_omitted:
            problems.append(f"「{ds.caption}」的萃取檔不在 .twbx 內：{'、'.join(missing)}")
    ok_detail = "；".join(f"「{d.caption}」：{'、'.join(d.extract_files) or '萃取'}" for d in dss)
    if dss and packaged and extracts_omitted:
        ok_detail += "（下載時未含萃取檔，萃取檔是否齊全待下載正式檔確認）"
    if dss and not packaged:
        ok_detail += "（.twb 不含資料，正式盤點請用 .twbx）"
    checks.append(
        Check(
            key="embedded_extract",
            title="資料在 .twbx 的萃取檔內",
            status="fail" if problems or not dss else "pass",
            detail="；".join(problems) or ok_detail,
        )
    )

    problems = []
    notes: list[str] = []
    for ds in dss:
        if ds.logical_tables > 1:
            problems.append(
                f"「{ds.caption}」的 Relationship 模型有 {ds.logical_tables} 張邏輯資料表"
            )
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
            title="單表萃取",
            status="fail" if problems or not dss else "pass",
            detail="；".join(problems) or "；".join(notes),
        )
    )

    users: dict[str, set[str]] = {}
    for formula in r.formulas:
        for fn in formula.user_functions:
            users.setdefault(fn, set()).add(formula.field)
    for ds in dss:
        for fn in ds.filter_user_functions:
            users.setdefault(fn, set()).add(f"{ds.caption} 的資料源篩選")
    if users:
        detail = "；".join(
            f"{fn}（{'、'.join(sorted(where))}）" for fn, where in sorted(users.items())
        )
        checks.append(
            Check(key="no_rls", title="無列層級權限（RLS）", status="fail", detail=detail)
        )
    else:
        checks.append(
            Check(key="no_rls", title="無列層級權限（RLS）", status="pass", detail="沒有使用者函式")
        )

    blended = [w.name for w in r.worksheets if w.blending]
    checks.append(
        Check(
            key="no_blending",
            title="無 Data Blending",
            status="fail" if blended else "pass",
            detail=f"用到 Blending 的工作表：{_quote(blended)}"
            if blended
            else "每張工作表只用一個資料源",
        )
    )

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
    add("Story", [d.name for d in r.dashboards if d.story])
    # 群組（categorical-bin）與數值分組（bin）已在擴充階段支援（K1）
    special = {
        k: v for k, v in r.calc_classes.items() if k not in ("tableau", "bin", "categorical-bin")
    }
    groups = sum(d.groups_or_sets for d in r.datasources)
    if special or groups:
        parts = [f"{k} {v}" for k, v in special.items()] + ([f"group {groups}"] if groups else [])
        features.append(f"集合等特殊欄位（{'、'.join(parts)}）")
    # 篩選、網址、參數與切換動作已在擴充階段支援（L1、L2），醒目提示不影響數字
    kinds = Counter(a.kind for a in r.actions if a.kind not in SUPPORTED_ACTIONS)
    if kinds:
        features.append(
            f"Dashboard action（{'、'.join(f'{k} {v}' for k, v in sorted(kinds.items()))}）"
        )
    return features


Grade = Literal["A", "B", "C"]


def _grade(r: CensusReport) -> tuple[Grade, list[str]]:
    c_reasons: list[str] = []
    if any(w.blending for w in r.worksheets):
        c_reasons.append("Data Blending")
    if any(d.custom_sql for d in r.datasources):
        c_reasons.append("自訂 SQL")
    if any(d.story for d in r.dashboards):
        c_reasons.append("Story")
    if any(w.maps for w in r.worksheets):
        c_reasons.append("地圖")

    b_reasons: list[str] = []
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
