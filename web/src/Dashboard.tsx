import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { fileUrl } from "./api";
import { type ColorScale, discreteColor, rampBackground } from "./colorScale";
import { FilterControl, ParameterControl } from "./Controls";
import { cssColor, displaySize, hasZoneBox, layoutDashboard, zoneBoxStyle } from "./layout";
import {
  actionUrl,
  applySetActions,
  fieldKey,
  nextSelection,
  parameterValue,
  runActions,
  sourceActions,
  stateFor,
  type ViewState,
} from "./state";
import type {
  Action,
  Button,
  CaseInfo,
  Dashboard,
  FieldFilter,
  FilterValue,
  IR,
  Mark,
  Parameter,
  RenderResponse,
  Scalar,
  SetValue,
  TextStyle,
  Zone,
  ZoneTextRun,
} from "./types";
import { type MarkEvent, visitSpecs, WorksheetView } from "./WorksheetView";

// Vega 預設的類別色（與 Vega-Lite 的 nominal 顏色相同）

const isFieldFilter = (f: { kind: string }): f is FieldFilter => f.kind !== "top_n" && f.kind !== "tuple";
const NO_SETS: Record<string, SetValue> = {};

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
  /** 篩選動作的目標是另一個 dashboard、或切換動作時切換過去 */
  onNavigate: (target: { dashboard?: string | null; worksheet?: string | null }) => void;
  cases: CaseInfo[];
}

interface ActionMenu {
  worksheet: string;
  marks: Mark[];
  /** 點選的標記的維度與量值（網址動作代入用） */
  values: Record<string, Scalar>[];
  actions: Action[];
  x: number;
  y: number;
}

export function DashboardView({ workbook, ir, dashboard, view, onView, onNavigate }: Props) {
  const [renders, setRenders] = useState<Record<string, RenderResponse>>({});
  const unsupported = useMemo(() => new Map(ir.unsupported.map((u) => [u.source_ref, u.reason])), [ir]);

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
  const parameterValues = () => {
    const values: Record<string, Scalar> = {};
    for (const p of ir.parameters) values[p.id] = p.id in view.parameters ? view.parameters[p.id] : p.value;
    return values;
  };
  /** 參數動作：回傳更新後的參數（沒有變化時回傳原物件）。 */
  const applyParameters = (actions: Action[], values: Record<string, Scalar>[], current: Record<string, Scalar>) => {
    let next = current;
    for (const action of actions) {
      if (action.kind !== "parameter" || !action.parameter) continue;
      const value = parameterValue(action, values);
      if (value !== undefined && next[action.parameter.parameter] !== value) {
        next = { ...next, [action.parameter.parameter]: value };
      }
    }
    return next;
  };
  const navigateTo = (actions: Action[]) => {
    for (const action of actions) {
      if (action.kind === "navigate") {
        onNavigate({ dashboard: action.target_dashboard, worksheet: action.target_worksheet });
        return;
      }
    }
  };
  const onMarks = (worksheet: string, event: MarkEvent) => {
    if (event.kind === "click") {
      const marks = nextSelection(view.selections[worksheet] ?? [], event.mark, event.additive);
      const selectActions = sourceActions(ir, worksheet, dashboard.id, "select");
      const result = runActions(selectActions, marks, view.actions);
      const values = marks.length > 0 && event.values ? [event.values] : [];
      onView({
        ...view,
        actions: result.actions,
        selections: { ...view.selections, [worksheet]: marks },
        parameters: applyParameters(selectActions, values, view.parameters),
        sets: applySetActions(ir, selectActions, marks, view.sets ?? {}),
      });
      // 選取時執行的網址動作：在新分頁開啟（點選的當下開啟，瀏覽器不會擋）
      for (const action of selectActions) {
        if (action.kind === "url" && values.length > 0) {
          window.open(actionUrl(action, values, parameterValues()), "_blank", "noopener");
        }
      }
      const menuActions = sourceActions(ir, worksheet, dashboard.id, "menu");
      const origin = dashboardRef.current?.getBoundingClientRect();
      setMenu(
        marks.length > 0 && menuActions.length > 0 && origin
          ? { worksheet, marks, values, actions: menuActions, x: event.x - origin.left, y: event.y - origin.top }
          : null,
      );
      if (result.navigate && result.navigate !== dashboard.id) onNavigate({ dashboard: result.navigate });
      else if (marks.length > 0) navigateTo(selectActions);
      return;
    }
    const hover = sourceActions(ir, worksheet, dashboard.id, "hover");
    if (hover.length === 0) return;
    const marks = event.kind === "hover" && event.mark ? [event.mark] : [];
    const values = event.kind === "hover" && event.values ? [event.values] : [];
    const result = runActions(hover, marks, view.actions);
    const parameters = applyParameters(hover, values, view.parameters);
    const sets = applySetActions(ir, hover, marks, view.sets ?? NO_SETS);
    if (
      JSON.stringify(result.actions) !== JSON.stringify(view.actions) ||
      parameters !== view.parameters ||
      sets !== (view.sets ?? NO_SETS)
    ) {
      onView({ ...view, actions: result.actions, parameters, sets });
    }
  };
  const runMenu = (action: Action) => {
    if (!menu) return;
    const result = runActions([action], menu.marks, view.actions);
    onView({
      ...view,
      actions: result.actions,
      parameters: applyParameters([action], menu.values, view.parameters),
      sets: applySetActions(ir, [action], menu.marks, view.sets ?? {}),
    });
    setMenu(null);
    if (result.navigate && result.navigate !== dashboard.id) onNavigate({ dashboard: result.navigate });
    else navigateTo([action]);
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

  // 顯示／隱藏按鈕切換過的區塊（相對於 IR 中的初始狀態）
  const [toggled, setToggled] = useState<Set<string>>(() => new Set());
  useEffect(() => setToggled(new Set()), [workbook, dashboard.id]);
  const isHidden = (zone: Zone) => Boolean(zone.hidden) !== toggled.has(zone.id);
  const onButton = (button: Button) => {
    if (button.action === "navigate") {
      onNavigate({ dashboard: button.target_dashboard, worksheet: button.target_worksheet });
    } else if (button.action === "toggle") {
      const next = new Set(toggled);
      for (const id of button.zones) {
        if (next.has(id)) next.delete(id);
        else next.add(id);
      }
      setToggled(next);
    } else if (button.export === "pdf") {
      window.print();
    }
  };
  const zoneById = new Map<string, Zone>();
  const index = (zones: Zone[]) =>
    zones.forEach((z) => {
      zoneById.set(z.id, z);
      index(z.children);
    });
  index(dashboard.zones);

  const flat: Zone[] = [];
  const collect = (zones: Zone[], hidden: boolean) =>
    zones.forEach((z) => {
      // 隱藏的區塊（含其中的子區塊）不繪製
      const self = hidden || isHidden(z);
      if (!self) flat.push(z);
      collect(z.children, self);
    });
  collect(dashboard.zones, false);
  const ready = fixed || available !== null;

  return (
    <div className="dashboard-wrapper" ref={wrapperRef}>
      <div
        className="dashboard"
        data-sizing={dashboard.sizing ?? "fixed"}
        style={{ width, height, background: cssColor(dashboard.background) }}
        ref={dashboardRef}
      >
      {ready && flat
        // 容器只畫背景與框線（在子區塊之下）；沒有的不畫
        .filter((z) => z.kind !== "container" || hasZoneBox(z.style))
        .map((zone) => {
          const box = boxes.get(zone.id) ?? { x: zone.x, y: zone.y, w: zone.w, h: zone.h };
          return (
          <div
            key={zone.id}
            className={`zone zone-${zone.kind}`}
            data-zone={zone.id}
            style={{ left: box.x, top: box.y, width: box.w, height: box.h }}
          >
            <div className="zone-box" style={zoneBoxStyle(zone.style)}>
            {zone.kind === "container" ? null : unsupported.has(zone.source_ref) ? (
              <div className="placeholder unsupported">未支援：{unsupported.get(zone.source_ref)}</div>
            ) : (
              <ZoneContent
                zone={zone}
                workbook={workbook}
                ir={ir}
                dashboard={dashboard}
                view={view}
                renders={renders}
                // 與 Tableau 相同，dashboard 上的工作表不畫圖例（圖例是另外的區塊）
                hideLegend
                onRendered={onRendered}
                onMarks={zone.worksheet && hasActions(zone.worksheet) ? (e) => onMarks(zone.worksheet!, e) : undefined}
                setFilter={setFilter}
                setParameter={setParameter}
                onButton={onButton}
                targetsShown={
                  zone.button?.action === "toggle"
                    ? zone.button.zones.some((id) => {
                        const target = zoneById.get(id);
                        return target !== undefined && !isHidden(target);
                      })
                    : true
                }
              />
            )}
            </div>
          </div>
          );
        })}
      {menu && (
        <div className="action-menu" role="menu" style={{ left: menu.x, top: menu.y }}>
          {menu.actions.map((action) =>
            action.kind === "url" ? (
              <a
                role="menuitem"
                key={action.id}
                href={actionUrl(action, menu.values, parameterValues())}
                target="_blank"
                rel="noopener noreferrer"
                onClick={() => setMenu(null)}
              >
                {action.caption || "開啟連結"}
              </a>
            ) : (
              <button type="button" role="menuitem" key={action.id} onClick={() => runMenu(action)}>
                {action.caption}
              </button>
            ),
          )}
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
  onButton: (button: Button) => void;
  /** 顯示／隱藏按鈕的區塊目前是否顯示 */
  targetsShown: boolean;
}

function ZoneContent({
  zone,
  workbook,
  ir,
  dashboard,
  view,
  renders,
  hideLegend,
  onRendered,
  onMarks,
  setFilter,
  setParameter,
  onButton,
  targetsShown,
}: ZoneProps) {
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
      return (
        <div className="title" style={richAlign(zone.content)}>
          {zone.content?.length ? (
            <RichText runs={zone.content} ir={ir} values={view.parameters} />
          ) : (
            (zone.text ?? dashboard.name)
          )}
        </div>
      );
    case "text":
      return (
        <div className="text" style={richAlign(zone.content)}>
          {zone.content?.length ? <RichText runs={zone.content} ir={ir} values={view.parameters} /> : zone.text}
        </div>
      );
    case "image":
      return <img className="zone-image" src={fileUrl(workbook, zone.image!)} alt="" />;
    case "web":
      // 只嵌入 http(s) 網頁；沙箱中的網頁不能存取檢視器
      return /^https?:\/\//i.test(zone.url ?? "") ? (
        <iframe
          className="zone-web"
          src={zone.url!}
          title={zone.url!}
          sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
          referrerPolicy="no-referrer"
        />
      ) : (
        <div className="placeholder">網頁：{zone.url}</div>
      );
    case "button":
      return <DashboardButton workbook={workbook} button={zone.button!} shown={targetsShown} onClick={onButton} />;
    default:
      return null;
  }
}

const EXPORT_LABELS = { pdf: "匯出 PDF", image: "匯出圖片", powerpoint: "匯出 PowerPoint" };

function DashboardButton({
  workbook,
  button,
  shown,
  onClick,
}: {
  workbook: string;
  button: Button;
  shown: boolean;
  onClick: (button: Button) => void;
}) {
  // 匯出圖片與 PowerPoint 新平台不支援（不影響數字）；PDF 以瀏覽器列印另存
  const disabled = button.action === "export" && button.export !== "pdf";
  const image = button.action === "toggle" && !shown && button.image_hidden ? button.image_hidden : button.image;
  const label = button.caption ?? (button.export ? EXPORT_LABELS[button.export] : null);
  const title = disabled ? "新平台不支援此匯出" : (button.tooltip ?? label ?? undefined);
  return (
    <button
      type="button"
      className={`dashboard-button${image ? " image" : ""}`}
      data-action={button.action}
      data-shown={button.action === "toggle" ? String(shown) : undefined}
      disabled={disabled}
      title={title}
      style={image ? undefined : { color: button.color ?? undefined, background: button.background ?? undefined }}
      onClick={() => onClick(button)}
    >
      {image ? <img src={fileUrl(workbook, image)} alt={label ?? title ?? ""} /> : (label ?? "按鈕")}
    </button>
  );
}

function Legend({ response, kind }: { response: RenderResponse | undefined; kind: string | null }) {
  const spec = response?.spec as Record<string, any> | undefined;
  // 參考線等圖層包住主圖時，顏色在第一個有顏色的圖層
  let color: Record<string, any> | undefined;
  if (spec)
    visitSpecs(spec, (node) => {
      color ??= node.encoding?.color;
    });
  if (!spec || !color) return <div className="legend" />;
  const scale = color.scale as ColorScale | undefined;
  const field = String(color.field);
  const rows = spec.data.values as Record<string, unknown>[];
  if (kind === "color" && color.type === "quantitative" && scale?.range?.length) {
    // 連續色彩：色帶與資料的最小值、最大值（總計不計入，與色階的範圍相同）
    const numbers = rows
      .filter((row) => !row.__total && typeof row[field] === "number")
      .map((row) => row[field] as number);
    if (numbers.length > 0) {
      const low = Math.min(...numbers);
      const high = Math.max(...numbers);
      const text = (value: number) => {
        const row = rows.find((r) => r[field] === value);
        const label = row?.[`${field}_label`];
        return label === undefined || label === null ? String(value) : String(label);
      };
      return (
        <div className="legend">
          <div className="legend-title">{color.title}</div>
          <div className="legend-ramp" style={{ background: rampBackground(scale, low, high) }} />
          <div className="legend-range">
            <span>{text(low)}</span>
            <span>{text(high)}</span>
          </div>
        </div>
      );
    }
  }
  if (kind !== "color" || color.type !== "nominal") {
    return <div className="legend"><div className="legend-title">{color.title}</div></div>;
  }
  const values: string[] = [];
  for (const row of rows) {
    const label = String(row[field]);
    if (!values.includes(label)) values.push(label);
  }
  return (
    <div className="legend">
      <div className="legend-title">{color.title}</div>
      {values.map((v, i) => (
        <div className="legend-item" key={v}>
          <span className="swatch" style={{ background: discreteColor(scale, v, i) }} />
          {v}
        </div>
      ))}
    </div>
  );
}

/** 參數顯示的值：有別名時用別名（例如 week → WEEK），否則為值本身。 */
export function parameterText(parameter: Parameter, value: Scalar | undefined): string {
  const current = value === undefined ? parameter.value : value;
  const alias = parameter.aliases?.find((a) => a.value === current);
  if (alias) return alias.label;
  return current === null || current === undefined ? "" : String(current);
}

const TABLEAU_WEIGHTS: Record<string, number> = {
  Light: 300,
  Book: 400,
  Regular: 400,
  Medium: 500,
  Semibold: 600,
  Bold: 700,
};

function runStyle(style: TextStyle | null | undefined): React.CSSProperties | undefined {
  if (!style) return undefined;
  const css: React.CSSProperties = {};
  if (style.bold) css.fontWeight = 700;
  if (style.italic) css.fontStyle = "italic";
  if (style.underline) css.textDecoration = "underline";
  if (style.size) css.fontSize = `${style.size}pt`;
  if (style.color) css.color = style.color;
  if (style.font) {
    // Tableau 自帶的字型（Tableau Bold、Tableau Medium…）以字重近似，其他字型照名稱
    const weight = /^Tableau (\w+)$/.exec(style.font)?.[1];
    if (weight && weight in TABLEAU_WEIGHTS) css.fontWeight = style.bold ? 700 : TABLEAU_WEIGHTS[weight];
    else css.fontFamily = `"${style.font}", sans-serif`;
  }
  return css;
}

/** 文字物件的段落對齊：取第一段有寫的對齊。 */
function richAlign(runs: ZoneTextRun[] | undefined): React.CSSProperties | undefined {
  const align = runs?.find((r) => r.style?.align)?.style?.align;
  return align ? { textAlign: align } : undefined;
}

/** 文字物件與標題：各段的格式，參數換成目前的值（與 Tableau 相同，切換參數時跟著變）。 */
function RichText({ runs, ir, values }: { runs: ZoneTextRun[]; ir: IR; values: Record<string, Scalar> }) {
  return (
    <>
      {runs.map((run, i) => {
        let text = run.text ?? "";
        if (run.parameter) {
          const parameter = ir.parameters.find((p) => p.id === run.parameter);
          text = parameter ? parameterText(parameter, values[run.parameter]) : "";
        }
        return (
          <span key={i} style={runStyle(run.style)}>
            {text}
          </span>
        );
      })}
    </>
  );
}
