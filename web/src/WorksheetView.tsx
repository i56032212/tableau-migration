import { useEffect, useRef, useState } from "react";
import embed, { type Result } from "vega-embed";
import { api } from "./api";
import type { FitMode, Mark, QueryState, RenderResponse, Scalar } from "./types";

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

/** 多張圖（同一架上有多個量值）無法自動填滿容器：依區塊大小平分每張圖的寬高。 */
function sizeConcat(spec: Record<string, any>, width: number, height: number) {
  const AXIS = 110; // 座標軸標籤與標題約占的寬度
  const GAP = 28; // 每張圖之間與 x 軸標籤的高度
  if (Array.isArray(spec.vconcat)) {
    const n = spec.vconcat.length;
    const h = Math.max(Math.floor((height - GAP * n - 30) / n), 60);
    spec.vconcat = spec.vconcat.map((s: Record<string, any>) => ({ ...s, width: Math.max(width - AXIS, 80), height: h }));
  }
  if (Array.isArray(spec.hconcat)) {
    const n = spec.hconcat.length;
    const w = Math.max(Math.floor((width - AXIS * n) / n), 60);
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
}: Props) {
  const chartRef = useRef<HTMLDivElement>(null);
  // 事件處理以 ref 取最新的回呼，圖表不必因父元件重繪而重建
  const onMarksRef = useRef(onMarks);
  onMarksRef.current = onMarks;
  const interactive = onMarks !== undefined;
  const selectedKey = JSON.stringify(selected ?? []);
  const [response, setResponse] = useState<RenderResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showTable, setShowTable] = useState(false);
  const stateKey = JSON.stringify(state);

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
  }, [workbook, worksheet, stateKey]);

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
    if (marks.length) {
      // 以原始值比對（Vega 會把日期欄解析成 Date，所以用回應中未經處理的資料）
      const rows = records
        .map((record, i) => [markOf(record, dimensions), i] as const)
        .filter(([mark]) => marks.some((m) => Object.keys(m).every((k) => !(k in mark) || mark[k] === m[k])))
        .map(([, i]) => i);
      highlight(spec, rows);
    }
    sizeConcat(spec, element.clientWidth, element.clientHeight);
    sizeFacets(spec, element.clientWidth, element.clientHeight);
    // 固定大小的方向（例如標準大小下每一列固定高度）：放不下時在區塊內捲動
    const scroll = applyFit(spec, fit, element.clientWidth);
    element.dataset.scroll = scroll.x && scroll.y ? "both" : scroll.y ? "y" : scroll.x ? "x" : "false";
    embed(element, spec as any, { actions: false, renderer: "svg" })
      .then((result) => {
        if (disposed) {
          result.finalize();
          return;
        }
        view = result;
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
    };
  }, [response, hideLegend, showTable, interactive, selectedKey, fit]);

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
          <span className="worksheet-title" style={{ color: usermeta.title_color }}>
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
    </div>
  );
}
