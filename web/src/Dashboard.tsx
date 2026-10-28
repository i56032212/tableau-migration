import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { FilterControl, ParameterControl } from "./Controls";
import { displaySize, layoutDashboard } from "./layout";
import { fieldKey, nextSelection, runActions, sourceActions, stateFor, type ViewState } from "./state";
import type { Action, CaseInfo, Dashboard, FieldFilter, FilterValue, IR, Mark, RenderResponse, Scalar, Zone } from "./types";
import { type MarkEvent, WorksheetView } from "./WorksheetView";

// Vega 預設的類別色（與 Vega-Lite 的 nominal 顏色相同）
const TABLEAU10 = ["#4c78a8", "#f58518", "#e45756", "#72b7b2", "#54a24b", "#eeca3b", "#b279a2", "#ff9da6", "#9d755d", "#bab0ac"];

const isFieldFilter = (f: { kind: string }): f is FieldFilter => f.kind !== "top_n" && f.kind !== "tuple";

/** 篩選控制項的變更：同一篩選群組（套用到選取的工作表、全域篩選）的工作表一起更新。 */
export function applyFilter(
  ir: IR,
  sheetFilters: Record<string, Record<string, FilterValue>>,
  worksheet: string,
  key: string,
  value: FilterValue,
): Record<string, Record<string, FilterValue>> {
  const targets: [string, string][] = [[worksheet, key]];
  const own = ir.worksheets
    .find((w) => w.id === worksheet)
    ?.filters.filter(isFieldFilter)
    .find((f) => fieldKey(f.field) === key);
  if (own?.group) {
    for (const sheet of ir.worksheets) {
      for (const f of sheet.filters.filter(isFieldFilter)) {
        if (f.group === own.group && !(sheet.id === worksheet && fieldKey(f.field) === key)) {
          targets.push([sheet.id, fieldKey(f.field)]);
        }
      }
    }
  }
  const next = { ...sheetFilters };
  for (const [sheet, field] of targets) next[sheet] = { ...(next[sheet] ?? {}), [field]: value };
  return next;
}

export function caseState(ir: IR, info: CaseInfo | undefined): ViewState {
  if (!info) return { sheetFilters: {}, parameters: {}, actions: {}, selections: {} };
  // 網址參數的語意：套用到每一張用到該欄位的工作表
  const sheetFilters: Record<string, Record<string, FilterValue>> = {};
  for (const sheet of ir.worksheets) sheetFilters[sheet.id] = { ...info.state.filters };
  return {
    sheetFilters,
    parameters: { ...info.state.parameters },
    actions: {},
    selections: {},
    today: info.state.today ?? null,
  };
}

interface Props {
  workbook: string;
  ir: IR;
  dashboard: Dashboard;
  view: ViewState;
  onView: (view: ViewState) => void;
  /** 篩選動作的目標是另一個 dashboard 時切換過去 */
  onNavigate: (dashboard: string) => void;
  cases: CaseInfo[];
}

interface ActionMenu {
  worksheet: string;
  marks: Mark[];
  actions: Action[];
  x: number;
  y: number;
}

export function DashboardView({ workbook, ir, dashboard, view, onView, onNavigate }: Props) {
  const [renders, setRenders] = useState<Record<string, RenderResponse>>({});
  const unsupported = useMemo(() => new Map(ir.unsupported.map((u) => [u.source_ref, u.reason])), [ir]);
  const legendSheets = useMemo(() => {
    const sheets = new Set<string>();
    const walk = (zones: Zone[]) =>
      zones.forEach((z) => {
        if (z.kind === "legend" && z.legend === "color" && z.worksheet) sheets.add(z.worksheet);
        walk(z.children);
      });
    walk(dashboard.zones);
    return sheets;
  }, [dashboard]);

  const onRendered = useCallback((response: RenderResponse) => {
    setRenders((current) => ({ ...current, [response.worksheet]: response }));
  }, []);

  useEffect(() => setRenders({}), [workbook, dashboard.id]);

  const setFilter = (worksheet: string, key: string, value: FilterValue) =>
    onView({ ...view, sheetFilters: applyFilter(ir, view.sheetFilters, worksheet, key, value) });
  const setParameter = (id: string, value: Scalar) =>
    onView({ ...view, parameters: { ...view.parameters, [id]: value } });

  // 篩選動作：點選觸發「選取」與「選單」動作，滑過觸發「滑過」動作
  const [menu, setMenu] = useState<ActionMenu | null>(null);
  useEffect(() => setMenu(null), [workbook, dashboard.id]);
  const dashboardRef = useRef<HTMLDivElement>(null);
  const hasActions = (worksheet: string) =>
    (["select", "hover", "menu"] as const).some((a) => sourceActions(ir, worksheet, dashboard.id, a).length > 0);
  const onMarks = (worksheet: string, event: MarkEvent) => {
    if (event.kind === "click") {
      const marks = nextSelection(view.selections[worksheet] ?? [], event.mark, event.additive);
      const result = runActions(sourceActions(ir, worksheet, dashboard.id, "select"), marks, view.actions);
      onView({ ...view, actions: result.actions, selections: { ...view.selections, [worksheet]: marks } });
      const menuActions = sourceActions(ir, worksheet, dashboard.id, "menu");
      const origin = dashboardRef.current?.getBoundingClientRect();
      setMenu(
        marks.length > 0 && menuActions.length > 0 && origin
          ? { worksheet, marks, actions: menuActions, x: event.x - origin.left, y: event.y - origin.top }
          : null,
      );
      if (result.navigate && result.navigate !== dashboard.id) onNavigate(result.navigate);
      return;
    }
    const hover = sourceActions(ir, worksheet, dashboard.id, "hover");
    if (hover.length === 0) return;
    const marks = event.kind === "hover" && event.mark ? [event.mark] : [];
    const result = runActions(hover, marks, view.actions);
    if (JSON.stringify(result.actions) !== JSON.stringify(view.actions)) onView({ ...view, actions: result.actions });
  };
  const runMenu = (action: Action) => {
    if (!menu) return;
    const result = runActions([action], menu.marks, view.actions);
    onView({ ...view, actions: result.actions });
    setMenu(null);
    if (result.navigate && result.navigate !== dashboard.id) onNavigate(result.navigate);
  };

  // 自動與範圍尺寸：依可用空間重排；量到尺寸之前不繪製，避免工作表先以參考尺寸查詢與繪製
  const fixed = !dashboard.sizing || dashboard.sizing === "fixed";
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState<[number, number] | null>(null);
  useLayoutEffect(() => {
    if (fixed) return;
    const element = wrapperRef.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      const height = Math.max(window.innerHeight - rect.top - 24, 320);
      setAvailable([Math.floor(rect.width), Math.floor(height)]);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [fixed]);
  const [width, height] = fixed
    ? [dashboard.size.width, dashboard.size.height]
    : available
      ? displaySize(dashboard, available[0], available[1])
      : [0, 0];
  const boxes = useMemo(() => layoutDashboard(dashboard, width, height), [dashboard, width, height]);

  const flat: Zone[] = [];
  const collect = (zones: Zone[], hidden: boolean) =>
    zones.forEach((z) => {
      // 隱藏的區塊（含其中的子區塊）不繪製
      if (!hidden && !z.hidden) flat.push(z);
      collect(z.children, hidden || Boolean(z.hidden));
    });
  collect(dashboard.zones, false);
  const ready = fixed || available !== null;

  return (
    <div className="dashboard-wrapper" ref={wrapperRef}>
      <div className="dashboard" data-sizing={dashboard.sizing ?? "fixed"} style={{ width, height }} ref={dashboardRef}>
      {ready && flat
        .filter((z) => z.kind !== "container")
        .map((zone) => {
          const box = boxes.get(zone.id) ?? { x: zone.x, y: zone.y, w: zone.w, h: zone.h };
          return (
          <div
            key={zone.id}
            className={`zone zone-${zone.kind}`}
            data-zone={zone.id}
            style={{ left: box.x, top: box.y, width: box.w, height: box.h }}
          >
            {unsupported.has(zone.source_ref) ? (
              <div className="placeholder unsupported">未支援：{unsupported.get(zone.source_ref)}</div>
            ) : (
              <ZoneContent
                zone={zone}
                workbook={workbook}
                ir={ir}
                dashboard={dashboard}
                view={view}
                renders={renders}
                hideLegend={zone.worksheet !== null && legendSheets.has(zone.worksheet)}
                onRendered={onRendered}
                onMarks={zone.worksheet && hasActions(zone.worksheet) ? (e) => onMarks(zone.worksheet!, e) : undefined}
                setFilter={setFilter}
                setParameter={setParameter}
              />
            )}
          </div>
          );
        })}
      {menu && (
        <div className="action-menu" role="menu" style={{ left: menu.x, top: menu.y }}>
          {menu.actions.map((action) => (
            <button type="button" role="menuitem" key={action.id} onClick={() => runMenu(action)}>
              {action.caption}
            </button>
          ))}
          <button type="button" className="link" onClick={() => setMenu(null)}>
            關閉
          </button>
        </div>
      )}
      </div>
    </div>
  );
}

interface ZoneProps {
  zone: Zone;
  workbook: string;
  ir: IR;
  dashboard: Dashboard;
  view: ViewState;
  renders: Record<string, RenderResponse>;
  hideLegend: boolean;
  onRendered: (response: RenderResponse) => void;
  onMarks?: (event: MarkEvent) => void;
  setFilter: (worksheet: string, key: string, value: FilterValue) => void;
  setParameter: (id: string, value: Scalar) => void;
}

function ZoneContent({ zone, workbook, ir, dashboard, view, renders, hideLegend, onRendered, onMarks, setFilter, setParameter }: ZoneProps) {
  switch (zone.kind) {
    case "worksheet":
      return (
        <WorksheetView
          workbook={workbook}
          worksheet={zone.worksheet!}
          state={stateFor(ir, zone.worksheet!, view)}
          hideLegend={hideLegend}
          showTitle={zone.show_title !== false}
          onRendered={onRendered}
          onMarks={onMarks}
          selected={onMarks ? view.selections[zone.worksheet!] : undefined}
        />
      );
    case "filter":
      return (
        <FilterControl
          ir={ir}
          workbook={workbook}
          zone={zone}
          state={view.sheetFilters[zone.worksheet!]}
          onChange={(key, value) => setFilter(zone.worksheet!, key, value)}
        />
      );
    case "parameter": {
      const parameter = ir.parameters.find((p) => p.id === zone.parameter);
      if (!parameter) return <div className="placeholder unsupported">找不到參數 {zone.parameter}</div>;
      const value = zone.parameter! in view.parameters ? view.parameters[zone.parameter!] : parameter.value;
      return (
        <ParameterControl
          parameter={parameter}
          control={zone.control}
          value={value}
          onChange={(v) => setParameter(parameter.id, v)}
        />
      );
    }
    case "legend":
      return <Legend response={renders[zone.worksheet!]} kind={zone.legend} />;
    case "title":
      return <div className="title">{zone.text ?? dashboard.name}</div>;
    case "text":
      return <div className="text">{zone.text}</div>;
    case "image":
      return <div className="placeholder">圖片：{zone.image}</div>;
    default:
      return null;
  }
}

function Legend({ response, kind }: { response: RenderResponse | undefined; kind: string | null }) {
  const spec = response?.spec as Record<string, any> | undefined;
  const color = spec?.encoding?.color;
  if (!spec || !color) return <div className="legend" />;
  if (kind !== "color" || color.type !== "nominal") {
    return <div className="legend"><div className="legend-title">{color.title}</div></div>;
  }
  const values: string[] = [];
  for (const row of spec.data.values as Record<string, unknown>[]) {
    const label = String(row[color.field]);
    if (!values.includes(label)) values.push(label);
  }
  return (
    <div className="legend">
      <div className="legend-title">{color.title}</div>
      {values.map((v, i) => (
        <div className="legend-item" key={v}>
          <span className="swatch" style={{ background: TABLEAU10[i % TABLEAU10.length] }} />
          {v}
        </div>
      ))}
    </div>
  );
}
