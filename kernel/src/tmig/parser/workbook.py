"""解析器入口：.twb／.twbx → IR v0.3（A2–A6、H1–H4）。

遇到不認得或 v0 不支援的元素一律寫進 ``unsupported``，不猜；
刻意略過、不影響數字的元素寫進 ``ignored``。
"""

from __future__ import annotations

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
    TupleFilter,
    Workbook,
    Worksheet,
)
from tmig.parser.context import ParseContext, SharedView
from tmig.parser.dashboard import DashboardParser
from tmig.parser.datasource import DatasourceParser, add_calc_field, analyze_calcs, parse_parameters
from tmig.parser.fields import instance_key
from tmig.parser.twbx import (
    WorkbookPackage,
    child,
    children,
    load_workbook_package,
    local_name,
    xpath,
)
from tmig.parser.worksheet import SheetParser

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


def _command(action: etree._Element) -> str:
    command = child(action, "command")
    raw = command.get("command", "") if command is not None else ""
    if not raw and child(action, "link") is not None:
        raw = "tsc:url"
    return raw


class WorkbookParseError(ValueError):
    """解析結果無法組成合法的 IR（通常是解析器本身的缺陷）。"""


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

        worksheets = []
        for ws_el in children(child(root, "worksheets"), "worksheet"):
            sheet = SheetParser(ctx, ws_el).parse()
            if sheet is not None:
                worksheets.append(sheet)

        dashboards = []
        for db_el in children(child(root, "dashboards"), "dashboard"):
            board = DashboardParser(ctx, db_el).parse()
            if board is not None:
                dashboards.append(board)

        actions = self._actions(child(root, "actions"), worksheets, dashboards)

        if not ctx.datasources:
            reasons = "；".join(u.reason for u in ctx.unsupported) or "workbook 沒有資料源"
            raise WorkbookParseError(f"沒有可移轉的資料源：{reasons}")

        datasources = [
            Datasource(
                id=state.id,
                name=state.name,
                caption=state.caption,
                connection=state.connection,
                settings=DatasourceSettings(week_start=state.week_start),
                fields=state.fields,
                filters=state.filters,
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
                unsupported=ctx.unsupported,
                ignored=ctx.ignored,
            )
        except ValidationError as exc:
            raise WorkbookParseError(f"解析結果不是合法的 IR：{exc}") from exc

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
        for action in children(element, "action"):
            raw = _command(action)
            kind = ACTION_KINDS.get(raw, raw or "未知")
            caption = action.get("caption") or action.get("name", "")
            if raw == "tsc:brush":
                ctx.ignore(action, f"醒目提示動作「{caption}」（不影響數字）")
                continue
            if raw != "tsc:tsl-filter":
                ctx.flag(action, f"Dashboard action「{caption}」（{kind}）v0 不支援")
                continue
            action_id = ctx.action_ids[action.get("name", "")]
            activation_el = child(action, "activation")
            activation = {"on-select": "select", "on-hover": "hover"}.get(
                activation_el.get("type", "") if activation_el is not None else "", "menu"
            )
            source_el = child(action, "source")
            sources = self._action_sources(source_el, by_name, dashboards, worksheets)
            if not sources:
                ctx.flag(action, f"篩選動作「{caption}」的來源工作表無法移轉")
                continue
            board_ids = {d.name: d.id for d in dashboards}
            dashboard = (
                board_ids.get(source_el.get("dashboard", "")) if source_el is not None else None
            )
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

    def _action_sources(
        self,
        source: etree._Element | None,
        by_name: dict[str, str],
        dashboards: list[Dashboard],
        worksheets: list[Worksheet],
    ) -> list[str]:
        """來源：指定工作表；或 dashboard 上的所有工作表；或使用該資料源的所有工作表（扣掉 <exclude-sheet>）。"""
        if source is None:
            return []
        if source.get("worksheet"):
            found = by_name.get(source.get("worksheet", ""))
            return [found] if found else []
        excluded = {by_name.get(e.get("name", "")) for e in children(source, "exclude-sheet")}
        if source.get("type") == "datasource":
            ds = self.ctx.datasources.get(source.get("datasource", ""))
            found_ids = [ws.id for ws in worksheets if ds is not None and ws.datasource == ds.id]
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
