"""解析器入口：.twb／.twbx → IR v0.3（A2–A6、H1–H4）。

遇到不認得或 v0 不支援的元素一律寫進 ``unsupported``，不猜；
刻意略過、不影響數字的元素寫進 ``ignored``。
"""

from __future__ import annotations

import re
from pathlib import Path

from lxml import etree
from pydantic import ValidationError

from tmig.ir.integrity import iter_zones
from tmig.ir.models import (
    IR,
    IR_VERSION,
    Action,
    Dashboard,
    Datasource,
    DatasourceSettings,
    FieldUse,
    FitMode,
    ParameterAction,
    Scalar,
    SetAction,
    TupleFilter,
    Unsupported,
    UrlAction,
    UrlField,
    Workbook,
    Worksheet,
)
from tmig.parser.context import DsState, ParseContext, SharedView
from tmig.parser.dashboard import DashboardParser
from tmig.parser.datasource import DatasourceParser, add_calc_field, analyze_calcs, parse_parameters
from tmig.parser.fields import instance_key, use_from_name
from tmig.parser.refs import qualified_parts
from tmig.parser.story import StoryParser
from tmig.parser.twbx import (
    WorkbookPackage,
    child,
    children,
    load_workbook_package,
    local_name,
    text_of,
    xpath,
)
from tmig.parser.values import LiteralError, parse_literal
from tmig.parser.worksheet import SheetParser
from tmig.planner.state import use_key

FITS: dict[str, FitMode] = {
    "entire-view": "entire_view",
    "fit-width": "fit_width",
    "fit-height": "fit_height",
}


def fit_mode(viewpoint: etree._Element | None) -> FitMode:
    """<viewpoint> 的 <zoom type> → IR 的 FitMode；沒有 zoom（或自訂的縮放百分比）為標準。"""
    zoom = child(viewpoint, "zoom")
    return FITS.get(zoom.get("type", "") if zoom is not None else "", "standard")


WORKBOOK_CHILDREN = frozenset(
    {
        "document-format-change-manifest",
        "preferences",
        "repository-location",
        "style",
        "style-theme",
        "datasources",
        "worksheets",
        "dashboards",
        "windows",
        "thumbnails",
        "actions",
        "external",
        "shared-views",
    }
)
SHARED_VIEW_CHILDREN = frozenset({"datasources", "datasource-dependencies", "filter"})
ACTION_KINDS = {"tsc:brush": "醒目提示", "tsc:tsl-filter": "篩選", "tsc:url": "網址"}


SET_ACTION_CLEARING = {
    "": "keep",
    "keep": "keep",
    "show-all": "add_all",
    "show-none": "remove_all",
    "remove-all": "remove_all",
}
"""集合動作清除選取時的行為（selection-clear-set-option；依公開資料推定，沒寫時保留集合的成員）。"""
PARAM_ACTION_AGGS = {
    "attr": "attr",
    "sum": "sum",
    "average": "avg",
    "min": "min",
    "max": "max",
    "count": "count",
    "count-d": "countd",
}


def worksheets_ds(worksheets: list[Worksheet], sheet_id: str) -> str | None:
    return next((ws.datasource for ws in worksheets if ws.id == sheet_id), None)


URL_PLACEHOLDER = re.compile(r"<([^<>]+)>")
URL_AGGS: dict[str, str] = {
    "sum": "sum",
    "avg": "avg",
    "min": "min",
    "max": "max",
    "count": "count",
    "countd": "countd",
    "median": "median",
    "attr": "attr",
}


def _url_key(ctx: ParseContext, ds: DsState, text: str) -> str | None:
    """網址中的欄位參照 → 欄位鍵：維度為欄位 id；量值加上彙總（沒寫時用預設彙總）；參數為 param:<id>。"""
    inner = text.split("~", 1)[0].strip()
    agg: str | None = None
    wrapped = re.fullmatch(r"([A-Za-z]+)\((.*)\)", inner)
    if wrapped and wrapped.group(1).lower() in URL_AGGS:
        agg, inner = URL_AGGS[wrapped.group(1).lower()], wrapped.group(2).strip()
    if inner.startswith("[Parameters]") or inner.startswith("Parameters."):
        param = ctx.parameter(inner.replace("Parameters.", "[Parameters].", 1))
        return f"param:{param.id}" if param is not None else None
    name = instance_key(inner) if inner.startswith("[") else f"[{inner}]"
    found = ds.by_name.get(name) or next((f for f in ds.fields if f"[{f.caption}]" == name), None)
    if found is None:
        return None
    level = ds.levels.get(found.id)
    if found.role == "measure" and level not in ("aggregate", "table_calc"):
        return f"{agg or found.default_agg or 'sum'}({found.id})"
    return found.id


def _command(action: etree._Element) -> str:
    command = child(action, "command")
    raw = command.get("command", "") if command is not None else ""
    if not raw and child(action, "link") is not None:
        raw = "tsc:url"
    return raw


class WorkbookParseError(ValueError):
    """解析結果無法組成合法的 IR（通常是解析器本身的缺陷）。"""


class NoDatasourceError(WorkbookParseError):
    """沒有可移轉的資料源（例如都是尚不支援的連線）；unsupported 為原因，盤點時列為需先補功能。"""

    def __init__(self, message: str, unsupported: list[Unsupported]) -> None:
        super().__init__(message)
        self.unsupported = unsupported


def parse_workbook(
    path: str | Path, *, lenient: bool = False, extracts_omitted: bool = False
) -> IR:
    """解析 .twb／.twbx。

    - lenient：非嵌入式萃取的資料源也照樣解析（連線以佔位值表示）
    - extracts_omitted：.twbx 下載時不含萃取檔（只盤點結構），缺萃取檔不列為不支援
    """
    package = load_workbook_package(path)
    return WorkbookParser(package, lenient=lenient, extracts_omitted=extracts_omitted).parse()


class WorkbookParser:
    def __init__(
        self, package: WorkbookPackage, *, lenient: bool = False, extracts_omitted: bool = False
    ) -> None:
        self.package = package
        self.ctx = ParseContext(package, lenient=lenient, extracts_omitted=extracts_omitted)

    def parse(self) -> IR:
        ctx, root = self.ctx, self.package.root
        for item in children(root):
            tag = local_name(item)
            if tag == "mapsources":
                ctx.flag(item, "地圖 v0 不支援")
            elif tag not in WORKBOOK_CHILDREN:
                ctx.flag(item, f"workbook 中的未知元素 <{tag}>")

        stem = Path(self.package.twb_name).stem
        workbook = Workbook(
            id=ctx.ids.make("wb", stem),
            name=stem,
            tableau_version=root.get("version"),
            source_ref="/workbook",
        )

        sources = children(child(root, "datasources"), "datasource")
        for ds_el in sources:
            if ds_el.get("name") == "Parameters":
                parse_parameters(ctx, ds_el)
        for ds_el in sources:
            if ds_el.get("name") == "Parameters":
                continue
            state = DatasourceParser(ctx, ds_el).parse()
            if state is not None:
                ctx.datasources[state.name] = state

        self._shared_views(child(root, "shared-views"))
        # 篩選動作的 id 先配發：工作表上的動作篩選要參照它
        for action in children(child(root, "actions"), "action"):
            if _command(action) == "tsc:tsl-filter":
                name = action.get("name", "")
                ctx.action_ids[name] = ctx.ids.make("act", action.get("caption") or name, name)

        self._fits(child(root, "windows"))
        worksheets = []
        for ws_el in children(child(root, "worksheets"), "worksheet"):
            sheet = SheetParser(ctx, ws_el).parse()
            if sheet is not None:
                worksheets.append(sheet)

        for window in children(child(root, "windows"), "window"):
            simple_id = child(window, "simple-id")
            uuid = simple_id.get("uuid", "") if simple_id is not None else ""
            if uuid:
                ctx.windows[uuid.strip("{}").upper()] = (
                    window.get("class", ""),
                    window.get("name", ""),
                )
        dashboards = []
        story_elements = []
        for db_el in children(child(root, "dashboards"), "dashboard"):
            if db_el.get("type") == "storyboard":
                story_elements.append(db_el)  # 故事點會參照 dashboard 與動作：最後才解析
                continue
            board = DashboardParser(ctx, db_el).parse()
            if board is not None:
                dashboards.append(board)
        parsed = {d.id for d in dashboards}
        ctx.story_names = {el.get("name", "") for el in story_elements}
        names = {board_id: name for name, board_id in ctx.dashboard_ids.items()}
        for element, zone in ctx.navigations:
            assert zone.button is not None
            target = zone.button.target_dashboard
            if target in parsed:
                continue
            name = names.get(target or "")
            if name is not None and name in ctx.story_names:
                # 目標是 Story（Story 的視窗類別也是 dashboard）
                zone.button = zone.button.model_copy(
                    update={"target_dashboard": None, "target_story": ctx.story_id(name)}
                )
                continue
            ctx.flag(element, "導覽按鈕的目標 dashboard 無法移轉")
            zone.kind, zone.button = "blank", None

        actions = self._actions(child(root, "actions"), worksheets, dashboards)
        stories = [
            story
            for el in story_elements
            if (story := StoryParser(ctx, el, worksheets, dashboards, actions).parse()) is not None
        ]
        # 目標是 Story 的按鈕與切換動作：Story 沒有轉成 IR 時不能移轉
        parsed_stories = {story.id for story in stories}
        for element, zone in ctx.navigations:
            story_target = zone.button.target_story if zone.button is not None else None
            if story_target is not None and story_target not in parsed_stories:
                ctx.flag(element, "導覽按鈕的目標 Story 無法移轉")
                zone.kind, zone.button = "blank", None
        dropped = [a for a in actions if a.target_story and a.target_story not in parsed_stories]
        for act in dropped:
            ctx.unsupported.append(
                Unsupported(
                    source_ref=act.source_ref,
                    reason=f"切換動作「{act.caption}」的目標 Story 無法移轉",
                    element="nav-action",
                )
            )
        actions = [a for a in actions if a not in dropped]

        if not ctx.datasources:
            reasons = "；".join(u.reason for u in ctx.unsupported) or "workbook 沒有資料源"
            raise NoDatasourceError(f"沒有可移轉的資料源：{reasons}", list(ctx.unsupported))

        datasources = [
            Datasource(
                id=state.id,
                name=state.name,
                caption=state.caption,
                connection=state.connection,
                settings=DatasourceSettings(week_start=state.week_start),
                fields=state.fields,
                filters=state.filters,
                published=state.published,
                source_ref=state.element.getroottree().getpath(state.element),
            )
            for state in ctx.datasources.values()
        ]
        try:
            return IR(
                ir_version=IR_VERSION,  # type: ignore[arg-type]
                workbook=workbook,
                datasources=datasources,
                parameters=ctx.parameters,
                worksheets=worksheets,
                dashboards=dashboards,
                actions=actions,
                stories=stories,
                unsupported=ctx.unsupported,
                ignored=ctx.ignored,
            )
        except ValidationError as exc:
            raise WorkbookParseError(f"解析結果不是合法的 IR：{exc}") from exc

    def _fits(self, windows: etree._Element | None) -> None:
        """工作表的顯示大小：工作表視窗的 <viewpoint>，與 dashboard 視窗中各工作表的 <viewpoint>
        （<zoom type='entire-view'>、'fit-width'、'fit-height'；沒有 zoom 為標準）。"""
        for window in children(windows, "window"):
            name = window.get("name", "")
            if window.get("class") == "worksheet":
                self.ctx.sheet_fits[name] = fit_mode(child(window, "viewpoint"))
            elif window.get("class") == "dashboard":
                self.ctx.dashboard_fits[name] = {
                    vp.get("name", ""): fit_mode(vp)
                    for vp in children(child(window, "viewpoints"), "viewpoint")
                }

    def _actions(
        self,
        element: etree._Element | None,
        worksheets: list[Worksheet],
        dashboards: list[Dashboard],
    ) -> list[Action]:
        """篩選動作轉成 IR；醒目提示記在 ignored；網址動作等其餘種類列為不支援。"""
        ctx = self.ctx
        by_name = {state.name: state.id for state in ctx.sheets.values()}
        sheet_ds = {ws.id: ws.datasource for ws in worksheets}
        result: list[Action] = []
        board_ids = {d.name: d.id for d in dashboards}
        self._link_stale_filters(element, worksheets, dashboards, by_name)
        for item in children(element):
            tag = local_name(item)
            if tag in ("datasources", "datasource-dependencies"):
                continue  # 動作用到的欄位清單
            if tag in ("edit-parameter-action", "nav-action"):
                found = (
                    self._parameter_action(item, by_name, dashboards, worksheets)
                    if tag == "edit-parameter-action"
                    else self._nav_action(item, by_name, dashboards, worksheets)
                )
                if found is not None:
                    result.append(found)
                continue
            if tag == "edit-group-action":
                found = self._set_action(item, by_name, dashboards, worksheets)
                if found is not None:
                    result.append(found)
                continue
            if tag != "action":
                ctx.flag(item, f"動作清單中的未知元素 <{tag}>")
                continue
            action = item
            raw = _command(action)
            kind = ACTION_KINDS.get(raw, raw or "未知")
            caption = action.get("caption") or action.get("name", "")
            if raw == "tsc:brush":
                ctx.ignore(action, f"醒目提示動作「{caption}」（不影響數字）")
                continue
            if raw not in ("tsc:tsl-filter", "tsc:url"):
                ctx.flag(action, f"Dashboard action「{caption}」（{kind}）v0 不支援")
                continue
            activation_el = child(action, "activation")
            activation = {"on-select": "select", "on-hover": "hover"}.get(
                activation_el.get("type", "") if activation_el is not None else "", "menu"
            )
            source_el = child(action, "source")
            sources = self._action_sources(source_el, by_name, dashboards, worksheets)
            if not sources:
                ctx.flag(action, f"{kind}動作「{caption}」的來源工作表無法移轉")
                continue
            dashboard = (
                board_ids.get(source_el.get("dashboard", "")) if source_el is not None else None
            )
            if raw == "tsc:url":
                link = child(action, "link")
                assert link is not None
                url_action = self._url_action(
                    action, link, caption, activation, dashboard, sources, sheet_ds
                )
                if url_action is not None:
                    result.append(url_action)
                continue
            action_id = ctx.action_ids[action.get("name", "")]
            params = {
                p.get("name"): p.get("value") for p in children(child(action, "command"), "param")
            }
            target_dashboard = board_ids.get(params.get("target") or "")
            if target_dashboard == dashboard:
                target_dashboard = None
            on_empty = params.get("on-empty")
            clearing = {None: "show_all", "none": "exclude_all", "assign": "keep"}.get(on_empty)
            if clearing is None:
                ctx.flag(action, f"篩選動作「{caption}」清除選取的方式 {on_empty} v0 不支援")
                continue
            targets = [
                ws.id
                for ws in worksheets
                if any(isinstance(f, TupleFilter) and f.action == action_id for f in ws.filters)
            ]
            source_ds = {sheet_ds[s] for s in sources}
            if any(sheet_ds[t] not in source_ds for t in targets):
                ctx.flag(action, f"篩選動作「{caption}」跨資料源，v0 不支援")
                continue
            result.append(
                Action(
                    id=action_id,
                    name=action.get("name", ""),
                    caption=caption,
                    kind="filter",
                    activation=activation,  # type: ignore[arg-type]
                    dashboard=dashboard,
                    sources=sources,
                    targets=targets,
                    target_dashboard=target_dashboard,
                    clearing=clearing,  # type: ignore[arg-type]
                    single_select=(params.get("single-select") or "").lower() == "true",
                    source_ref=xpath(action),
                )
            )
        # 動作沒有轉成 IR 時，工作表上參照它的篩選不再連到動作（維持 .twb 中的預設狀態）
        kept = {a.id for a in result}
        for ws in worksheets:
            ws.filters = [
                f.model_copy(update={"action": None})
                if isinstance(f, TupleFilter) and f.action is not None and f.action not in kept
                else f
                for f in ws.filters
            ]
        return result

    def _link_stale_filters(
        self,
        element: etree._Element | None,
        worksheets: list[Worksheet],
        dashboards: list[Dashboard],
        by_name: dict[str, str],
    ) -> None:
        """篩選動作的篩選參照已刪除的動作時（刪掉動作再重建，工作表上留下舊動作的篩選），
        Tableau 照樣把篩選套用在工作表上，之後執行的篩選動作寫入同一個欄位、取代它。

        會寫入這張工作表的篩選動作只有一個時連到它（選取時以選取取代，沒有選取時維持原本的值）；
        沒有或有多個時維持靜態篩選。可能寫入的工作表：目標 dashboard 上的工作表，扣掉 exclude
        參數列出的工作表與動作的來源工作表（Tableau 不以動作篩選來源工作表本身）。"""
        stale = self.ctx.stale_action_filters
        if not stale:
            return
        board_sheets = {
            d.name: {z.worksheet for z in iter_zones(d.zones) if z.kind == "worksheet"}
            for d in dashboards
        }
        scopes: dict[str, set[str | None]] = {}
        for item in children(element, "action"):
            if _command(item) != "tsc:tsl-filter":
                continue
            params = {
                p.get("name"): p.get("value") or ""
                for p in children(child(item, "command"), "param")
            }
            target = params.get("target") or ""
            scope: set[str | None] = set(board_sheets.get(target, set()))
            if target in by_name:
                scope = {by_name[target]}
            scope -= {by_name.get(n) for n in (params.get("exclude") or "").split(",")}
            scope -= set(
                self._action_sources(child(item, "source"), by_name, dashboards, worksheets)
            )
            action_id = self.ctx.action_ids.get(item.get("name", ""))
            if action_id is not None:
                scopes[action_id] = scope
        for sheet_name, filter_id in stale:
            sheet_id = by_name.get(sheet_name)
            candidates = [a for a, scope in scopes.items() if sheet_id in scope]
            if len(candidates) != 1:
                continue
            for ws in worksheets:
                if ws.id == sheet_id:
                    ws.filters = [
                        f.model_copy(update={"action": candidates[0]})
                        if isinstance(f, TupleFilter) and f.id == filter_id
                        else f
                        for f in ws.filters
                    ]

    def _url_action(
        self,
        action: etree._Element,
        link: etree._Element,
        caption: str,
        activation: str,
        dashboard: str | None,
        sources: list[str],
        sheet_ds: dict[str, str],
    ) -> Action | None:
        """網址動作：網址中的 <[欄位]> 對應到欄位鍵，檢視器以選取的標記代入。"""
        ctx = self.ctx
        expression = link.get("expression", "")
        if not expression:
            ctx.flag(action, f"網址動作「{caption}」沒有網址")
            return None
        ds = next(
            (state for state in ctx.datasources.values() if state.id == sheet_ds.get(sources[0])),
            None,
        )
        fields: list[UrlField] = []
        for match in URL_PLACEHOLDER.finditer(expression):
            key = _url_key(ctx, ds, match.group(1)) if ds is not None else None
            if key is None:
                ctx.ignore(
                    action, f"網址動作「{caption}」的 {match.group(0)} 找不到欄位，代入空字串"
                )
                continue
            if all(f.placeholder != match.group(0) for f in fields):
                fields.append(UrlField(placeholder=match.group(0), key=key))
        target = text_of(child(link, "url-action-type"))
        if target == "specific-zone":
            ctx.ignore(
                action, f"網址動作「{caption}」原本開在 dashboard 的網頁物件中，新平台在新分頁開啟"
            )
        return Action(
            id=ctx.ids.make("act", caption or action.get("name", ""), action.get("name", "")),
            name=action.get("name", "") or caption,
            caption=caption,
            kind="url",
            url=UrlAction(
                template=expression,
                fields=fields,
                escape=link.get("url-escape", "true") != "false",
                delimiter=link.get("delimiter") or ",",
            ),
            activation=activation,  # type: ignore[arg-type]
            dashboard=dashboard,
            sources=sources,
            source_ref=xpath(action),
        )

    def _new_action_common(
        self,
        item: etree._Element,
        by_name: dict[str, str],
        dashboards: list[Dashboard],
        worksheets: list[Worksheet],
        label: str,
    ) -> tuple[str, str, str | None, list[str], dict[str, str]] | None:
        """參數與切換動作共用：說明、觸發方式、所屬 dashboard、來源工作表、params。"""
        caption = item.get("caption") or item.get("name", "")
        activation_el = child(item, "activation")
        activation = {"on-select": "select", "on-hover": "hover"}.get(
            activation_el.get("type", "") if activation_el is not None else "", "menu"
        )
        source_el = child(item, "source")
        sources = self._action_sources(source_el, by_name, dashboards, worksheets)
        if not sources:
            self.ctx.flag(item, f"{label}「{caption}」的來源工作表無法移轉")
            return None
        board_ids = {d.name: d.id for d in dashboards}
        dashboard = board_ids.get(source_el.get("dashboard", "")) if source_el is not None else None
        params = {
            p.get("name", ""): p.get("value", "") for p in children(child(item, "params"), "param")
        }
        return caption, activation, dashboard, sources, params

    def _set_action(
        self,
        item: etree._Element,
        by_name: dict[str, str],
        dashboards: list[Dashboard],
        worksheets: list[Worksheet],
    ) -> Action | None:
        """集合動作：以選取的標記改變 target-group 的成員（固定成員的集合）。

        XML 依公開資料推定：取代、加入或移除寫在 add-or-remove-marks（功能旗標 GroupActionAddRemove），
        清除選取時的行為寫在 selection-clear-set-option（show-all＝加入所有值）。"""
        ctx = self.ctx
        common = self._new_action_common(item, by_name, dashboards, worksheets, "集合動作")
        if common is None:
            return None
        caption, activation, dashboard, sources, params = common
        target = params.get("target-group", "")
        parts = qualified_parts(target)
        ds = ctx.datasources.get(parts[0]) if len(parts) == 2 else None
        data_field = (
            ds.by_name.get("[" + parts[1].replace("]", "]]") + "]") if ds is not None else None
        )
        if (
            ds is None
            or data_field is None
            or data_field.set is None
            or data_field.set.rule.kind != "members"
        ):
            ctx.flag(
                item,
                f"集合動作「{caption}」的目標 {target or '（未指定）'} 不是可以移轉的固定成員集合",
            )
            return None
        sheet_ds = {ws.id: ws.datasource for ws in worksheets}
        if any(sheet_ds.get(s) != ds.id for s in sources):
            ctx.flag(item, f"集合動作「{caption}」的來源工作表與集合在不同的資料源，v0 不支援")
            return None
        marks = next((c for c in children(item) if local_name(c) == "add-or-remove-marks"), None)
        behavior = (marks.get("value") if marks is not None else None) or "assign"
        clearing = SET_ACTION_CLEARING.get(params.get("selection-clear-set-option", ""))
        if behavior not in ("assign", "add", "remove") or clearing is None:
            ctx.flag(
                item,
                f"集合動作「{caption}」的執行方式 {behavior}、清除方式 "
                f"{params.get('selection-clear-set-option')} v0 不支援",
            )
            return None
        return Action(
            id=ctx.ids.make("act", caption or item.get("name", ""), item.get("name", "")),
            name=item.get("name", "") or caption,
            caption=caption,
            kind="set",
            set_action=SetAction(
                target=data_field.id,
                behavior=behavior,  # type: ignore[arg-type]
                clearing=clearing,  # type: ignore[arg-type]
            ),
            activation=activation,  # type: ignore[arg-type]
            dashboard=dashboard,
            sources=sources,
            source_ref=xpath(item),
        )

    def _parameter_action(
        self,
        item: etree._Element,
        by_name: dict[str, str],
        dashboards: list[Dashboard],
        worksheets: list[Worksheet],
    ) -> Action | None:
        """參數動作：source-field 的值設給 target-parameter。"""
        ctx = self.ctx
        common = self._new_action_common(item, by_name, dashboards, worksheets, "參數動作")
        if common is None:
            return None
        caption, activation, dashboard, sources, params = common
        param = ctx.parameter(params.get("target-parameter", ""))
        if param is None:
            ctx.flag(
                item, f"參數動作「{caption}」的目標參數 {params.get('target-parameter')} 找不到"
            )
            return None
        source_ref = params.get("source-field", "")
        ds = next(
            (s for s in ctx.datasources.values() if s.id == worksheets_ds(worksheets, sources[0])),
            None,
        )
        use = use_from_name(ds, instance_key(source_ref)) if ds is not None else "沒有資料源"
        if isinstance(use, str) and ds is not None:
            found = ds.by_name.get(instance_key(source_ref))
            use = FieldUse(field=found.id) if found is not None else use
        if isinstance(use, str):
            ctx.flag(item, f"參數動作「{caption}」的來源欄位 {source_ref} 無法解析：{use}")
            return None
        agg_el = child(item, "agg-type")
        agg_text = agg_el.get("type", "attr") if agg_el is not None else "attr"
        aggregation = PARAM_ACTION_AGGS.get(agg_text)
        if aggregation is None:
            ctx.flag(item, f"參數動作「{caption}」的彙總方式 {agg_text} v0 不支援")
            return None
        clear_el = child(item, "clear-option")
        clearing = "keep"
        clear_value: Scalar = None
        if clear_el is not None and clear_el.get("type") == "assign-fixed-value":
            clearing = "assign"
            try:
                clear_value = parse_literal(clear_el.get("value"), param.datatype)
            except LiteralError as exc:
                ctx.flag(item, f"參數動作「{caption}」清除時的值無法解析：{exc}")
                return None
        return Action(
            id=ctx.ids.make("act", caption or item.get("name", ""), item.get("name", "")),
            name=item.get("name", "") or caption,
            caption=caption,
            kind="parameter",
            parameter=ParameterAction(
                parameter=param.id,
                field=use_key(use),
                aggregation=aggregation,  # type: ignore[arg-type]
                clearing=clearing,  # type: ignore[arg-type]
                clear_value=clear_value,
            ),
            activation=activation,  # type: ignore[arg-type]
            dashboard=dashboard,
            sources=sources,
            source_ref=xpath(item),
        )

    def _nav_action(
        self,
        item: etree._Element,
        by_name: dict[str, str],
        dashboards: list[Dashboard],
        worksheets: list[Worksheet],
    ) -> Action | None:
        """切換動作（前往工作表）：切換到 target（較新的版本為 sheet）指定的 dashboard 或工作表。"""
        ctx = self.ctx
        common = self._new_action_common(item, by_name, dashboards, worksheets, "切換動作")
        if common is None:
            return None
        caption, activation, dashboard, sources, params = common
        target = params.get("target") or params.get("sheet") or ""
        board = next((d.id for d in dashboards if d.name == target), None)
        sheet = by_name.get(target) if board is None else None
        story = (
            ctx.story_id(target)
            if board is None and sheet is None and target in ctx.story_names
            else None
        )
        if board is None and sheet is None and story is None:
            ctx.flag(item, f"切換動作「{caption}」的目標 {target or '（未指定）'} 找不到")
            return None
        return Action(
            id=ctx.ids.make("act", caption or item.get("name", ""), item.get("name", "")),
            name=item.get("name", "") or caption,
            caption=caption,
            kind="navigate",
            activation=activation,  # type: ignore[arg-type]
            dashboard=dashboard,
            sources=sources,
            target_dashboard=board,
            target_worksheet=sheet,
            target_story=story,
            source_ref=xpath(item),
        )

    def _action_sources(
        self,
        source: etree._Element | None,
        by_name: dict[str, str],
        dashboards: list[Dashboard],
        worksheets: list[Worksheet],
    ) -> list[str]:
        """來源：指定工作表；或 dashboard 上的所有工作表；或使用該資料源的所有工作表；或所有工作表
        （type='all'）；都扣掉 <exclude-sheet>。"""
        if source is None:
            return []
        if source.get("worksheet"):
            found = by_name.get(source.get("worksheet", ""))
            return [found] if found else []
        excluded = {by_name.get(e.get("name", "")) for e in children(source, "exclude-sheet")}
        if source.get("type") == "datasource":
            ds = self.ctx.datasources.get(source.get("datasource", ""))
            found_ids = [ws.id for ws in worksheets if ds is not None and ws.datasource == ds.id]
        elif source.get("type") == "all" and not source.get("dashboard"):
            found_ids = [ws.id for ws in worksheets]
        else:
            board = next((d for d in dashboards if d.name == source.get("dashboard")), None)
            if board is None:
                return []
            found_ids = []
            for z in iter_zones(board.zones):
                if z.kind == "worksheet" and z.worksheet and z.worksheet not in found_ids:
                    found_ids.append(z.worksheet)
        return [sid for sid in found_ids if sid not in excluded]

    def _shared_views(self, element: etree._Element | None) -> None:
        """全域篩選：「套用到所有使用此資料源的工作表」的篩選存在 <shared-views>，工作表只在篩選架列出欄位。"""
        ctx = self.ctx
        for view in children(element, "shared-view"):
            name = view.get("name", "")
            for item in children(view):
                if local_name(item) not in SHARED_VIEW_CHILDREN:
                    ctx.flag(item, f"全域篩選中的未知元素 <{local_name(item)}>")
            ds = ctx.datasources.get(name)
            if ds is None:
                continue  # 資料源本身無法移轉時已列為不支援
            instances: dict[str, etree._Element] = {}
            added = False
            for dep in children(view, "datasource-dependencies"):
                if dep.get("datasource") != name:
                    continue
                for column in children(dep, "column"):
                    if column.get("name", "") in ds.by_name or child(column, "calculation") is None:
                        continue
                    if add_calc_field(ctx, ds, column) is not None:
                        added = True
                for inst in children(dep, "column-instance"):
                    instances[instance_key(inst.get("name", ""))] = inst
            if added:
                analyze_calcs(ctx, ds)
            filters = {instance_key(f.get("column", "")): f for f in children(view, "filter")}
            ctx.shared_views[name] = SharedView(element=view, instances=instances, filters=filters)
