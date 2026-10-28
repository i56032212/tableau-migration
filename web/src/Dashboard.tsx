import { useCallback, useEffect, useMemo, useState } from "react";
import { FilterControl, ParameterControl } from "./Controls";
import { fieldKey, stateFor } from "./state";
import type { CaseInfo, Dashboard, FilterValue, IR, RenderResponse, Scalar, Zone } from "./types";
import { WorksheetView } from "./WorksheetView";

// Vega 預設的類別色（與 Vega-Lite 的 nominal 顏色相同）
const TABLEAU10 = ["#4c78a8", "#f58518", "#e45756", "#72b7b2", "#54a24b", "#eeca3b", "#b279a2", "#ff9da6", "#9d755d", "#bab0ac"];

export interface ViewState {
  sheetFilters: Record<string, Record<string, FilterValue>>;
  parameters: Record<string, Scalar>;
}

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
    ?.filters.find((f) => f.kind !== "top_n" && fieldKey(f.field) === key);
  if (own?.group) {
    for (const sheet of ir.worksheets) {
      for (const f of sheet.filters) {
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
  if (!info) return { sheetFilters: {}, parameters: {} };
  // 網址參數的語意：套用到每一張用到該欄位的工作表
  const sheetFilters: Record<string, Record<string, FilterValue>> = {};
  for (const sheet of ir.worksheets) sheetFilters[sheet.id] = { ...info.state.filters };
  return { sheetFilters, parameters: { ...info.state.parameters } };
}

interface Props {
  workbook: string;
  ir: IR;
  dashboard: Dashboard;
  view: ViewState;
  onView: (view: ViewState) => void;
  cases: CaseInfo[];
}

export function DashboardView({ workbook, ir, dashboard, view, onView }: Props) {
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

  const flat: Zone[] = [];
  const collect = (zones: Zone[]) =>
    zones.forEach((z) => {
      flat.push(z);
      collect(z.children);
    });
  collect(dashboard.zones);

  return (
    <div className="dashboard" style={{ width: dashboard.size.width, height: dashboard.size.height }}>
      {flat
        .filter((z) => z.kind !== "container")
        .map((zone) => (
          <div
            key={zone.id}
            className={`zone zone-${zone.kind}`}
            data-zone={zone.id}
            style={{ left: zone.x, top: zone.y, width: zone.w, height: zone.h }}
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
                setFilter={setFilter}
                setParameter={setParameter}
              />
            )}
          </div>
        ))}
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
  setFilter: (worksheet: string, key: string, value: FilterValue) => void;
  setParameter: (id: string, value: Scalar) => void;
}

function ZoneContent({ zone, workbook, ir, dashboard, view, renders, hideLegend, onRendered, setFilter, setParameter }: ZoneProps) {
  switch (zone.kind) {
    case "worksheet":
      return (
        <WorksheetView
          workbook={workbook}
          worksheet={zone.worksheet!}
          state={stateFor(zone.worksheet!, view.sheetFilters, view.parameters)}
          hideLegend={hideLegend}
          onRendered={onRendered}
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
