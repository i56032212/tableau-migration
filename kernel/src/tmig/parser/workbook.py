"""解析器入口：.twb／.twbx → IR v0.3（A2–A6、H1–H4）。

遇到不認得或 v0 不支援的元素一律寫進 ``unsupported``，不猜；
刻意略過、不影響數字的元素寫進 ``ignored``。
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree
from pydantic import ValidationError

from tmig.ir.models import IR, IR_VERSION, Datasource, DatasourceSettings, Workbook
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

        for action in children(child(root, "actions"), "action"):
            command = child(action, "command")
            raw = command.get("command", "") if command is not None else ""
            if not raw and child(action, "link") is not None:
                raw = "tsc:url"
            kind = ACTION_KINDS.get(raw, raw or "未知")
            caption = action.get("caption") or action.get("name", "")
            if raw == "tsc:brush":
                ctx.ignore(action, f"醒目提示動作「{caption}」（不影響數字）")
            else:
                ctx.flag(action, f"Dashboard action「{caption}」（{kind}）v0 不支援")

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
                unsupported=ctx.unsupported,
                ignored=ctx.ignored,
            )
        except ValidationError as exc:
            raise WorkbookParseError(f"解析結果不是合法的 IR：{exc}") from exc

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
