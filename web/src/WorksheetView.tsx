import type React from "react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import embed, { type Result } from "vega-embed";
import { api } from "./api";
import type { FitMode, Mark, QueryState, RenderResponse, Scalar, TooltipSheetInfo } from "./types";

/** 標記上的互動：點選（含空白處，mark 為 null）、滑過、移開。座標為視窗座標。 */
export interface MarkEvent {
  kind: "click" | "hover" | "leave";
  mark: Mark | null;
  /** 標記的維度與量值（網址動作代入用） */
  values?: Record<string, Scalar> | null;
  additive: boolean;
  x: number;
  y: number;
}

interface Props {
  workbook: string;
  worksheet: string;
  state: QueryState;
  hideLegend?: boolean;
  showTitle?: boolean;
  /** dashboard 區塊中的顯示大小；沒有時依工作表本身的設定（usermeta.fit） */
  fit?: FitMode | null;
  onRendered?: (response: RenderResponse) => void;
  /** 有篩選動作以這張工作表為來源時才傳入 */
  onMarks?: (event: MarkEvent) => void;
  /** 目前選取的標記：其餘標記淡化 */
  selected?: Mark[];
  /** 醒目提示動作與醒目提示控制項：欄位值相同的標記醒目、其餘淡化（工作表上沒有的欄位不比對） */
  highlighted?: Mark[] | null;
  /** 編輯器：工作表的定義改變後遞增，重新查詢 */
  revision?: number;
  /** 階層的切入（檢視器）：一組階層（最上層的欄位鍵）改成切入 delta 層；沒有傳入時不顯示切入的按鈕 */
  onDrill?: (key: string, delta: number) => void;
  /** 工具提示中的工作表（Viz in Tooltip）的查詢狀態；沒有傳入時工具提示只有文字（工具提示中的工作表本身也不再巢狀） */
  tooltipState?: (target: string, mark: Mark) => QueryState;
}

/** 滑過標記時的工具提示：位置（視窗座標）、Vega 算好的欄位與值、標記的維度值。 */
interface Tip {
  x: number;
  y: number;
  value: unknown;
  mark: Mark | null;
}

/** 工具提示中的工作表的狀態更新前等待的時間（滑過許多標記時不必每個都查詢） */
const TOOLTIP_DELAY = 150;

/**
 * 有工具提示中的工作表時的工具提示（取代 Vega 的預設工具提示）：標記的欄位與值，下方是以標記的值篩選的
 * 各張工作表（不超過設定的最大寬、高）。不攔截滑鼠，放不下時移到游標的另一側。
 */
function VizTooltip({
  tip,
  mark,
  sheets,
  workbook,
  stateFor,
}: {
  tip: Tip;
  mark: Mark | null;
  sheets: TooltipSheetInfo[];
  workbook: string;
  stateFor: (target: string, mark: Mark) => QueryState;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: tip.x + 12, top: tip.y + 12 });
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const { width, height } = element.getBoundingClientRect();
    let left = tip.x + 12;
    let top = tip.y + 12;
    if (left + width > window.innerWidth - 4) left = Math.max(4, tip.x - width - 12);
    if (top + height > window.innerHeight - 4) top = Math.max(4, tip.y - height - 12);
    setPosition((current) => (current.left === left && current.top === top ? current : { left, top }));
  }, [tip.x, tip.y, mark, sheets]);
  const lines =
    tip.value !== null && typeof tip.value === "object"
      ? Object.entries(tip.value as Record<string, unknown>)
      : [["", tip.value] as [string, unknown]];
  return createPortal(
    <div ref={ref} className="viz-tooltip" role="tooltip" style={position}>
      <table className="viz-tooltip-text">
        <tbody>
          {lines.map(([key, value]) => (
            <tr key={key}>
              {key && <td className="key">{key}</td>}
              <td>{String(value ?? "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {mark &&
        sheets.map((sheet) => (
          <div
            key={sheet.worksheet}
            className="viz-tooltip-sheet"
            data-tooltip-sheet={sheet.worksheet}
            style={{ width: sheet.max_width, height: sheet.max_height }}
          >
            <WorksheetView
              workbook={workbook}
              worksheet={sheet.worksheet}
              state={stateFor(sheet.worksheet, mark)}
              showTitle={false}
              fit="entire_view"
              hideLegend
            />
          </div>
        ))}
    </div>,
    document.body,
  );
}

type Spec = Record<string, any>;

/** 選取的標記醒目、其餘淡化：每個圖層加上依列號判斷的不透明度。 */
/** 依序走訪規格中的每一張圖（含圖層與多張圖）。 */
export function visitSpecs(spec: Spec, visit: (node: Spec) => void) {
  visit(spec);
  for (const key of ["layer", "vconcat", "hconcat", "concat"]) {
    if (Array.isArray(spec[key])) spec[key].forEach((child: Spec) => visitSpecs(child, visit));
  }
  if (spec.spec) visitSpecs(spec.spec, visit);
}

/** 參考線（後端以 __ref 為名稱前綴）與色帶、多層列標題等裝飾（__deco）的圖層，不參與選取與醒目提示。 */
const isReferenceLayer = (node: Spec) =>
  typeof node.name === "string" && (node.name.startsWith("__ref") || node.name.startsWith("__deco"));

function highlight(spec: Spec, rows: number[]) {
  const test = `indexof([${rows.join(",")}], datum.__row) >= 0`;
  visitSpecs(spec, (node) => {
    if (node.mark && !isReferenceLayer(node))
      node.encoding = { ...(node.encoding ?? {}), opacity: { condition: { test, value: 1 }, value: 0.25 } };
  });
}

/** 資料列 → 標記（維度欄位鍵 → 原始值）。 */
function markOf(record: Record<string, unknown>, dimensions: Record<string, string>): Mark {
  const mark: Mark = {};
  for (const [name, key] of Object.entries(dimensions)) mark[key] = (record[name] ?? null) as Scalar;
  return mark;
}

/** 分面（欄列架上的維度把圓餅分成多個小圖）：容器寬高不適用於分面，依小圖的數量平分區塊的寬高。 */
function sizeFacets(spec: Record<string, any>, width: number, height: number) {
  const column = spec.encoding?.column?.field;
  const row = spec.encoding?.row?.field;
  if (!column && !row) return;
  const values = (spec.data?.values ?? []) as Record<string, unknown>[];
  const count = (field?: string) => (field ? new Set(values.map((v) => v[field])).size || 1 : 1);
  const HEADER = 24; // 分面標題的高度
  const GAP = 16;
  spec.width = Math.max(Math.floor((width - 16) / count(column)) - GAP, 40);
  spec.height = Math.max(Math.floor((height - HEADER) / count(row)) - HEADER - GAP, 40);
  spec.autosize = { type: "pad" };
  // 甜甜圈的洞以容器寬高計算，分面的小圖中改用固定的大小
  const hole = Math.floor(Math.min(spec.width, spec.height) * 0.3);
  visitSpecs(spec, (node) => {
    if (node.mark?.innerRadius?.expr) node.mark = { ...node.mark, innerRadius: hole };
  });
}

/** 區塊放不下離散的欄時，預留給列標題與座標軸的寬度（像素）。 */
const AXIS_ALLOWANCE = 100;

/**
 * 依顯示大小設定圖的寬高（與 kernel 的 tmig.render.vegalite.apply_fit 相同的規則）：fit_width／
 * entire_view 時寬度填滿，fit_height／entire_view 時高度填滿；否則離散的列固定每一列的高度、
 * 設定了大小的欄與連續座標軸固定大小，離散的欄在區塊放不下時固定為依內容估計的寬度。
 * 回傳可以捲動的方向。
 */
export function applyFit(spec: Spec, fit: FitMode | null | undefined, availableWidth: number): { x: boolean; y: boolean } {
  const sizing = spec.usermeta?.sizing;
  if (!sizing) return { x: false, y: typeof spec.height === "object" };
  const mode: FitMode = fit ?? spec.usermeta?.fit ?? "standard";
  const rows = sizing.rows ?? {};
  const columns = sizing.columns ?? {};
  let width: unknown = "container";
  let height: unknown = "container";
  if (mode !== "fit_height" && mode !== "entire_view") {
    if (rows.step) height = { step: rows.step };
    else if (rows.size) height = rows.size;
  }
  if (mode !== "fit_width" && mode !== "entire_view") {
    if (columns.size) width = columns.size;
    else if (columns.step && (columns.fixed || columns.step * (columns.count ?? 0) > availableWidth - AXIS_ALLOWANCE))
      width = { step: columns.step };
  }
  spec.width = width;
  spec.height = height;
  const fillX = width === "container";
  const fillY = height === "container";
  spec.autosize = { type: fillX && fillY ? "fit" : fillX ? "fit-x" : fillY ? "fit-y" : "pad", contains: "padding" };
  return { x: !fillX, y: !fillY };
}

/**
 * 捲動時固定標題（與 Tableau 相同）：上下捲動時，圖上方（欄標題、欄位標籤）與下方（座標軸）的區域固定在區塊的
 * 上緣與下緣；左右捲動時，左側（列標題）固定在左緣。以繪好的 SVG 的複本裁出這些區域、以 sticky 疊在原圖上。
 */
export function freezeHeaders(
  element: HTMLElement,
  result: Result,
  scroll: { x: boolean; y: boolean },
  background?: string,
) {
  // vega-embed 把 SVG 直接放在區塊（.chart）中：另包一層與圖同寬高的容器，固定的區域才能隨捲動貼在邊緣
  const svg = element.querySelector(":scope > svg, :scope > .frozen-host > svg") as SVGSVGElement | null;
  if (!svg) return;
  let host = svg.parentElement as HTMLElement;
  host.querySelectorAll(":scope > .frozen").forEach((e) => e.remove());
  if (!scroll.x && !scroll.y) return;
  if (!host.classList.contains("frozen-host")) {
    const wrapper = document.createElement("div");
    wrapper.className = "frozen-host";
    host.insertBefore(wrapper, svg);
    wrapper.appendChild(svg);
    host = wrapper;
  }
  const [originX, originY] = result.view.origin();
  const width = Number(svg.getAttribute("width")) || svg.getBoundingClientRect().width;
  const height = Number(svg.getAttribute("height")) || svg.getBoundingClientRect().height;
  const below = height - originY - result.view.height();
  const fill =
    background && background !== "transparent" ? background : getComputedStyle(element).backgroundColor || "#ffffff";
  const solid = fill === "rgba(0, 0, 0, 0)" || fill === "transparent" ? "#ffffff" : fill;
  const box = (name: string, style: Record<string, string>, offsetX: number, offsetY: number) => {
    const div = document.createElement("div");
    div.className = `frozen ${name}`;
    div.setAttribute("aria-hidden", "true");
    Object.assign(div.style, { position: "sticky", overflow: "hidden", background: solid, pointerEvents: "none" }, style);
    const copy = svg.cloneNode(true) as SVGSVGElement;
    copy.removeAttribute("id");
    copy.style.display = "block";
    copy.style.marginLeft = `${-offsetX}px`;
    copy.style.marginTop = `${-offsetY}px`;
    div.appendChild(copy);
    return div;
  };
  if (scroll.y && originY > 0) {
    const top = box("frozen-top", { top: "0", height: `${originY}px`, width: `${width}px`, marginBottom: `${-originY}px`, zIndex: "2" }, 0, 0);
    host.insertBefore(top, svg);
  }
  if (scroll.x && originX > 0) {
    const left = box("frozen-left", { left: "0", width: `${originX}px`, height: `${height}px`, marginBottom: `${-height}px`, zIndex: "3" }, 0, 0);
    host.insertBefore(left, svg);
    if (scroll.y && originY > 0) {
      const corner = box("frozen-corner", { left: "0", top: "0", width: `${originX}px`, height: `${originY}px`, marginBottom: `${-originY}px`, zIndex: "4" }, 0, 0);
      host.insertBefore(corner, svg);
    }
  }
  if (scroll.y && below > 1) {
    const bottom = box("frozen-bottom", { bottom: "0", height: `${below}px`, width: `${width}px`, marginTop: `${-below}px`, zIndex: "2" }, 0, height - below);
    host.appendChild(bottom);
  }
}

/** 工作表標題的樣式：後端依標題的格式（沒有時為 Tableau 預設的 15pt）算出的字型、顏色與對齊。 */
function titleStyle(usermeta: Record<string, any>): React.CSSProperties {
  const font = usermeta.title_font as
    | { family: string; size: number; weight: number; italic: boolean; align?: "left" | "center" | "right" }
    | undefined;
  if (!font) return { color: usermeta.title_color };
  return {
    color: usermeta.title_color,
    fontFamily: font.family,
    fontSize: font.size,
    fontWeight: font.weight,
    fontStyle: font.italic ? "italic" : undefined,
    textAlign: font.align,
    flex: font.align && font.align !== "left" ? "1" : undefined,
  };
}

/**
 * 多張圖（同一架上有多個量值、窗格）無法自動填滿容器：依區塊大小平分每張圖的寬高。窗格（spacing 小）之間
 * 只有最下面（最左邊）一張畫刻度，留給刻度與窗格標題的空間比多個量值的圖少。
 */
function sizeConcat(spec: Record<string, any>, width: number, height: number) {
  const panes = typeof spec.spacing === "number";
  const AXIS = panes ? 80 : 110; // 座標軸標籤與標題約占的寬度
  const GAP = panes ? 6 : 28; // 每張圖之間的高度
  const TITLE = 90; // 窗格的列標題（左側）
  const rowTitles = (s: Record<string, any>) => (s.title && s.title.orient === "left" ? TITLE : 0);
  if (Array.isArray(spec.vconcat)) {
    const n = spec.vconcat.length;
    const h = Math.max(Math.floor((height - GAP * n - (panes ? 40 : 30)) / n), panes ? 30 : 60);
    spec.vconcat = spec.vconcat.map((s: Record<string, any>) => {
      if (Array.isArray(s.hconcat)) {
        const m = s.hconcat.length;
        const w = Math.max(Math.floor((width - AXIS - 40 - GAP * m) / m), 40);
        return { ...s, hconcat: s.hconcat.map((c: Record<string, any>) => ({ ...c, width: w, height: h })) };
      }
      return { ...s, width: Math.max(width - AXIS - rowTitles(s), 80), height: h };
    });
  }
  if (Array.isArray(spec.hconcat)) {
    const n = spec.hconcat.length;
    const w = Math.max(Math.floor((width - AXIS - (panes ? GAP * n : AXIS * (n - 1))) / n), 40);
    spec.hconcat = spec.hconcat.map((s: Record<string, any>) => ({ ...s, width: w, height: Math.max(height - 60, 60) }));
  }
}

/** 一張工作表：向查詢服務取 Vega-Lite 規格並繪製；可切換顯示資料表。 */
export function WorksheetView({
  workbook,
  worksheet,
  state,
  hideLegend,
  showTitle = true,
  fit,
  onRendered,
  onMarks,
  selected,
  highlighted,
  revision = 0,
  onDrill,
  tooltipState,
}: Props) {
  const chartRef = useRef<HTMLDivElement>(null);
  // 事件處理以 ref 取最新的回呼，圖表不必因父元件重繪而重建
  const onMarksRef = useRef(onMarks);
  onMarksRef.current = onMarks;
  const tooltipStateRef = useRef(tooltipState);
  tooltipStateRef.current = tooltipState;
  const [tip, setTip] = useState<Tip | null>(null);
  // 工具提示中的工作表以稍後的標記查詢（滑過許多標記時不必每個都查詢）
  const [tipMark, setTipMark] = useState<Mark | null>(null);
  const tipMarkKey = JSON.stringify(tip?.mark ?? null);
  useEffect(() => {
    const next = JSON.parse(tipMarkKey) as Mark | null;
    if (next === null) {
      setTipMark(null);
      return;
    }
    const timer = window.setTimeout(() => setTipMark(next), TOOLTIP_DELAY);
    return () => window.clearTimeout(timer);
  }, [tipMarkKey]);
  const interactive = onMarks !== undefined;
  const selectedKey = JSON.stringify(selected ?? []);
  const highlightKey = JSON.stringify(highlighted ?? null);
  const [response, setResponse] = useState<RenderResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showTable, setShowTable] = useState(false);
  const stateKey = JSON.stringify(state);
  const tooltipSheets = tooltipState ? (response?.tooltip_sheets ?? []) : [];
  const vizTooltips = tooltipSheets.length > 0;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .render(workbook, worksheet, JSON.parse(stateKey) as QueryState)
      .then((result) => {
        if (cancelled) return;
        setResponse(result);
        setError(null);
        onRendered?.(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // onRendered 只用來回報結果，不觸發重新查詢
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workbook, worksheet, stateKey, revision]);

  useEffect(() => {
    const element = chartRef.current;
    if (!element || !response?.spec || showTable) return;
    let view: Result | null = null;
    let disposed = false;
    let leaveTimer: number | undefined;
    const original = response.spec as Spec;
    const records = (original.data?.values ?? []) as Record<string, unknown>[];
    const dimensions = (original.usermeta?.dimensions ?? {}) as Record<string, string>;
    const measures = (original.usermeta?.measures ?? {}) as Record<string, string>;
    const spec = structuredClone(original) as Spec;
    if (hideLegend)
      visitSpecs(spec, (node) => {
        for (const channel of ["color", "size", "shape", "opacity"])
          if (node.encoding?.[channel]?.field) node.encoding[channel].legend = null;
      });
    const marks = JSON.parse(selectedKey) as Mark[];
    // 醒目提示：只比對這張工作表上有的欄位；完全沒有相同欄位的條件不影響這張工作表
    const present = new Set(Object.values(dimensions));
    const lit = ((JSON.parse(highlightKey) as Mark[] | null) ?? [])
      .map((h) => Object.fromEntries(Object.entries(h).filter(([k]) => present.has(k))) as Mark)
      .filter((h) => Object.keys(h).length > 0);
    if (marks.length || lit.length) {
      // 以原始值比對（Vega 會把日期欄解析成 Date，所以用回應中未經處理的資料）
      const rows = records
        .map((record, i) => [markOf(record, dimensions), i] as const)
        .filter(
          ([mark]) =>
            marks.some((m) => Object.keys(m).every((k) => !(k in mark) || mark[k] === m[k])) ||
            lit.some((h) => Object.keys(h).every((k) => mark[k] === h[k])),
        )
        .map(([, i]) => i);
      highlight(spec, rows);
    }
    sizeConcat(spec, element.clientWidth, element.clientHeight);
    sizeFacets(spec, element.clientWidth, element.clientHeight);
    // 固定大小的方向（例如標準大小下每一列固定高度）：放不下時在區塊內捲動
    const scroll = applyFit(spec, fit, element.clientWidth);
    element.dataset.scroll = scroll.x && scroll.y ? "both" : scroll.y ? "y" : scroll.x ? "x" : "false";
    // 工具提示中的工作表：以自己的工具提示取代 Vega 的（文字相同，下方加上以標記篩選的工作表）
    const tooltip = (_handler: unknown, event: MouseEvent, item: any, value: unknown) => {
      if (value === null || value === undefined || value === "") {
        setTip(null);
        return;
      }
      const row = item?.datum?.__row;
      const mark = typeof row === "number" && records[row] ? markOf(records[row], dimensions) : null;
      setTip({ x: event.clientX, y: event.clientY, value, mark });
    };
    embed(element, spec as any, { actions: false, renderer: "svg", ...(vizTooltips ? { tooltip } : {}) })
      .then((result) => {
        if (disposed) {
          result.finalize();
          return;
        }
        view = result;
        // 捲動時與 Tableau 相同，欄標題、座標軸與列標題固定在區塊邊緣
        freezeHeaders(element, result, scroll, (original.usermeta?.background as string | undefined) ?? undefined);
        if (!interactive) return;
        const markAt = (item: any): Mark | null | undefined => {
          const row = item?.datum?.__row;
          if (typeof row === "number" && records[row]) return markOf(records[row], dimensions);
          // 空白處（沒有項目或背景群組）＝清除選取；其餘（座標軸、圖例）不處理
          return !item || item.mark?.marktype === "group" ? null : undefined;
        };
        const emit = (kind: MarkEvent["kind"], event: any, mark: Mark | null, item?: any) => {
          const row = item?.datum?.__row;
          const values =
            mark && typeof row === "number" && records[row]
              ? { ...markOf(records[row], measures), ...mark }
              : null;
          onMarksRef.current?.({
            kind,
            mark,
            values,
            additive: Boolean(event?.ctrlKey || event?.metaKey || event?.shiftKey),
            x: event?.clientX ?? 0,
            y: event?.clientY ?? 0,
          });
        };
        result.view.addEventListener("click", (event: any, item: any) => {
          const mark = markAt(item);
          if (mark !== undefined) emit("click", event, mark, item);
        });
        result.view.addEventListener("mouseover", (event: any, item: any) => {
          const mark = markAt(item);
          if (!mark) return;
          window.clearTimeout(leaveTimer);
          emit("hover", event, mark);
        });
        result.view.addEventListener("mouseout", (event: any) => {
          window.clearTimeout(leaveTimer);
          leaveTimer = window.setTimeout(() => emit("leave", event, null), 250);
        });
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
    return () => {
      disposed = true;
      window.clearTimeout(leaveTimer);
      view?.finalize();
      setTip(null);
    };
  }, [response, hideLegend, showTable, interactive, selectedKey, highlightKey, fit, vizTooltips]);

  const title = response?.title ?? response?.name ?? worksheet;
  const count = selected?.length ?? 0;
  // 工作表的背景（含標題，可以透明）與標題的顏色
  const usermeta = (response?.spec as Spec | null | undefined)?.usermeta ?? {};
  const meta = (
    <span className="worksheet-meta">
      {count > 0 && (
        <button
          type="button"
          className="link selection"
          title="清除選取"
          onClick={() => onMarks?.({ kind: "click", mark: null, additive: false, x: 0, y: 0 })}
        >
          已選取 {count} 個標記 ✕
        </button>
      )}
      {onDrill &&
        !response?.unsupported &&
        (response?.drill ?? []).map((d) => (
          <span key={`${d.shelf}:${d.key}`} className="drill" data-drill={d.key}>
            {d.can_up && (
              <button
                type="button"
                className="link"
                title={`向上收合：${d.label}`}
                aria-label={`向上收合 ${d.label}`}
                onClick={() => onDrill(d.key, d.delta - 1)}
              >
                ⊟ {d.label}
              </button>
            )}
            {d.can_down && d.next && (
              <button
                type="button"
                className="link"
                title={`向下切入：${d.label} → ${d.next}`}
                aria-label={`向下切入到 ${d.next}`}
                onClick={() => onDrill(d.key, d.delta + 1)}
              >
                ⊞ {d.next}
              </button>
            )}
          </span>
        ))}
      {response && !response.unsupported && <span>{response.rows} 列</span>}
      {response?.table && (
        <button type="button" className="link" onClick={() => setShowTable(!showTable)}>
          {showTable ? "圖表" : "資料"}
        </button>
      )}
    </span>
  );
  return (
    <div
      className="worksheet"
      data-worksheet={worksheet}
      data-loading={loading ? "true" : "false"}
      data-interactive={interactive ? "true" : undefined}
      style={{ background: usermeta.background }}
    >
      {showTitle ? (
        <div className="worksheet-header">
          <span className="worksheet-title" style={titleStyle(usermeta)}>
            {title}
          </span>
          {meta}
        </div>
      ) : (
        // Tableau 不顯示標題時，列數與資料切換浮在右上角、滑過時才顯示，不占圖表空間；
        // 有選取的標記時一直顯示（可清除選取）
        <div className="worksheet-overlay" data-selected={count > 0 ? "true" : undefined}>
          {meta}
        </div>
      )}
      <div className="worksheet-body">
        {error && <div className="placeholder error">查詢失敗：{error}</div>}
        {!error && response?.unsupported && (
          <div className="placeholder unsupported">未支援：{response.unsupported}</div>
        )}
        {!error && !response?.unsupported && showTable && response?.table && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  {response.table.headers.map((h) => (
                    <th key={h}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {response.table.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j} className={/^[-\d$,.%()NT]+$/.test(cell) ? "num" : undefined}>
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!error && !response?.unsupported && !showTable && <div className="chart" ref={chartRef} />}
        {loading && !response && <div className="placeholder">載入中…</div>}
      </div>
      {tip && vizTooltips && (
        <VizTooltip
          tip={tip}
          mark={tipMark}
          sheets={tooltipSheets}
          workbook={workbook}
          stateFor={(target, mark) => tooltipStateRef.current?.(target, mark) ?? { filters: {}, parameters: {} }}
        />
      )}
    </div>
  );
}
