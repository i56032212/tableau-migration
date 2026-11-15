import type React from "react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import embed, { type Result } from "vega-embed";
import { api } from "./api";
import { fitOf, VIEW_SIGNAL, type Viewport, visibleBox, zoomableMap, zoomAt, type ZoomState } from "./mapZoom";
import { nextSelection, sameMark } from "./state";
import type { FitMode, Mark, QueryState, RenderResponse, Scalar, TextStyle, TooltipSheetInfo } from "./types";

/** 標記上的互動：點選（含空白處，mark 為 null）、滑過、移開。座標為視窗座標。 */
export interface MarkEvent {
  kind: "click" | "hover" | "leave";
  mark: Mark | null;
  /** 標記的維度與量值（網址動作代入用） */
  values?: Record<string, Scalar> | null;
  additive: boolean;
  x: number;
  y: number;
  /** 依類別選取（工具提示中點維度的值）：以這些標記取代整個選取 */
  marks?: Mark[];
}

interface Props {
  workbook: string;
  worksheet: string;
  state: QueryState;
  hideLegend?: boolean;
  showTitle?: boolean;
  /** 是否顯示說明（Caption）；省略時依工作表本身的設定 */
  showCaption?: boolean;
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
  /**
   * 地圖平移、縮放之後的顯示範圍（null 為重設地圖、依標記自動縮放）：傳入時由呼叫端處理（編輯器存進工作表）；
   * 沒有傳入時只在這個檢視中（以新的範圍重新繪製）
   */
  onViewport?: (viewport: Viewport | null) => void;
  /**
   * 工具提示的命令按鈕（檢視器）：只保留或排除選取的標記，clear 為清除這張工作表的只保留與排除。
   * 沒有傳入時點選標記不會釘住工具提示。
   */
  onKeep?: (mode: "keep" | "exclude" | "clear", marks: Mark[]) => void;
  /** 點選標記後另有動作選單（在點選處的右下方展開）時：釘住的工具提示放在點選處的上方，不擋住選單 */
  pinAbove?: boolean;
  /** 編輯器：拖曳標記或點的註解文字之後，第 index 個註解的文字移動了 dx、dy 像素 */
  onAnnotationMove?: (index: number, dx: number, dy: number) => void;
}

/** 停止平移、縮放之後多久以新的範圍重新繪製（毫秒） */
const VIEWPORT_DELAY = 600;

/** 滑過標記時的工具提示：位置（視窗座標）、Vega 算好的欄位與值、標記的維度值。 */
interface Tip {
  x: number;
  y: number;
  value: unknown;
  mark: Mark | null;
  /** 自訂的工具提示文字（依滑過的標記組成）；沒有時列出 value 的各欄位 */
  rich?: RichPart[] | null;
}

/** 自訂工具提示的一段文字；key、raw：這一段是維度的值（依類別選取用） */
interface RichPart {
  text: string;
  style?: TextStyle;
  key?: string;
  raw?: Scalar;
}

type TooltipRun = { text?: string; column?: string; value?: string; null?: boolean; style?: TextStyle };

/** 釘住的工具提示（點選標記之後）：命令按鈕與依類別選取 */
interface PinActions {
  count: number;
  commands: boolean;
  /** 工具提示各列的標題 → 維度的欄位鍵；null 為不允許依類別選取 */
  titles: Record<string, string> | null;
  /** 放在點選處的上方（點選處的右下方有動作選單） */
  above?: boolean;
  onKeep: (mode: "keep" | "exclude") => void;
  onCategory: (key: string, value: Scalar) => void;
}

type SelectTool = "rect" | "radius" | "lasso";
/** 地圖工具列的選取工具（Tableau 地圖工具列的矩形、半徑與套索選取） */
const SELECT_TOOLS: [SelectTool, string, string][] = [
  ["rect", "▭", "矩形選取"],
  ["radius", "◯", "半徑選取"],
  ["lasso", "✎", "套索選取"],
];

/** 選取的範圍（視窗座標）：矩形與半徑是起點與終點，套索是路徑上的各點；回傳判斷一個點在不在範圍內的函式。 */
function regionTest(tool: SelectTool, points: [number, number][]): (x: number, y: number) => boolean {
  const [[x0, y0], [x1, y1]] = [points[0], points[points.length - 1]];
  if (tool === "rect") {
    return (x, y) => x >= Math.min(x0, x1) && x <= Math.max(x0, x1) && y >= Math.min(y0, y1) && y <= Math.max(y0, y1);
  }
  if (tool === "radius") {
    const radius = Math.hypot(x1 - x0, y1 - y0);
    return (x, y) => Math.hypot(x - x0, y - y0) <= radius;
  }
  // 套索：射線法判斷點在多邊形內
  return (x, y) => {
    let inside = false;
    for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
      const [xi, yi] = points[i];
      const [xj, yj] = points[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  };
}

/**
 * 摘要卡（Tableau 的「工作表＞顯示摘要」）：檢視中各量值的筆數、總和、平均、最小值、最大值與中位數；
 * 選取標記時只算選取的標記。數字取自查詢結果的原始值。
 */
function SummaryCard({ table, marks }: { table: NonNullable<RenderResponse["table"]>; marks: Mark[] }) {
  const measures = table.keys.map((key, i) => ({ key, i })).filter(({ i }) => table.roles?.[i] === "measure");
  if (measures.length === 0) return null;
  const selected = (row: string[]) =>
    marks.length === 0 ||
    marks.some((mark) =>
      Object.entries(mark).every(([key, value]) => {
        const at = table.keys.indexOf(key);
        return at < 0 || row[at] === (value === null ? "" : String(value));
      }),
    );
  const rows = table.raw.filter(selected);
  const show = (value: number | null) => (value === null ? "" : value.toLocaleString("en-US", { maximumFractionDigits: 2 }));
  return (
    <table className="summary-card" aria-label="摘要">
      <caption>
        摘要{marks.length > 0 ? `（選取的 ${rows.length} 個標記）` : ""}
        <span className="muted"> 筆數：{rows.length}</span>
      </caption>
      <thead>
        <tr>
          <th />
          <th>總和</th>
          <th>平均</th>
          <th>最小值</th>
          <th>最大值</th>
          <th>中位數</th>
        </tr>
      </thead>
      <tbody>
        {measures.map(({ key, i }) => {
          const values = rows
            .map((row) => row[i])
            .filter((v) => v !== "" && v !== null && v !== undefined)
            .map(Number)
            .filter(Number.isFinite)
            .sort((a, b) => a - b);
          const n = values.length;
          const sum = values.reduce((a, b) => a + b, 0);
          const median = n === 0 ? null : n % 2 === 1 ? values[(n - 1) / 2] : (values[n / 2 - 1] + values[n / 2]) / 2;
          return (
            <tr key={key} data-measure={key}>
              <th>{table.headers[i] ?? key}</th>
              <td>{show(n ? sum : null)}</td>
              <td>{show(n ? sum / n : null)}</td>
              <td>{show(n ? values[0] : null)}</td>
              <td>{show(n ? values[n - 1] : null)}</td>
              <td>{show(median)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** 工具提示欄位清單中的記號（見 kernel 的 TOOLTIP_CARD）：這一層是第幾張標記卡，不顯示 */
const CARD_KEY = "__card:";
/** 「暫留時」的工具提示：停在標記上多久才顯示（毫秒） */
const HOVER_DELAY = 500;

/** 自訂的工具提示（spec.usermeta.tooltip，見 kernel 的 _tooltip_template）。 */
interface TooltipTemplate {
  show: boolean;
  runs: TooltipRun[];
  /** 出現的方式：hover 為停在標記上一會兒才顯示（留在原位）；省略為回應式 */
  mode?: "hover";
  /** 命令按鈕（只保留、排除）與依類別選取；省略為有 */
  commands?: boolean;
  select?: boolean;
  /** 各標記卡各自的文字（鍵是標記卡的順序）；沒有列出的標記卡用 runs */
  cards?: Record<string, TooltipRun[]>;
}

/** 以標記的資料組成自訂的工具提示：欄位是 Null 時不顯示（除非 Null 設有別名）。 */
function richTooltip(runs: TooltipRun[], record: Record<string, unknown>, dimensions: Record<string, string>): RichPart[] {
  const parts: RichPart[] = [];
  for (const run of runs) {
    if (run.text !== undefined) parts.push({ text: run.text, style: run.style });
    else if (run.column !== undefined && (run.null || record[run.value ?? run.column] != null)) {
      const key = run.value !== undefined ? dimensions[run.value] : undefined;
      parts.push({
        text: String(record[run.column] ?? ""),
        style: run.style,
        ...(key ? { key, raw: (record[run.value!] ?? null) as Scalar } : {}),
      });
    }
  }
  return parts;
}

/** 工具提示各列的標題 → 維度的欄位鍵（工具提示列的是顯示文字欄 cN_label，維度以原始值欄 cN 為鍵）。 */
function tooltipTitles(spec: Spec, dimensions: Record<string, string>): Record<string, string> {
  const titles: Record<string, string> = {};
  visitSpecs(spec, (node) => {
    const list = node.encoding?.tooltip;
    if (!Array.isArray(list)) return;
    for (const item of list) {
      const field = String(item.field ?? "");
      const key = dimensions[field] ?? dimensions[field.replace(/_label$/, "")];
      if (key && typeof item.title === "string") titles[item.title] = key;
    }
  });
  return titles;
}

function tooltipRunStyle(style: TextStyle | undefined): React.CSSProperties | undefined {
  if (!style) return undefined;
  return {
    fontWeight: style.bold ? 700 : undefined,
    fontStyle: style.italic ? "italic" : undefined,
    textDecoration: style.underline ? "underline" : undefined,
    fontSize: style.size ? `${style.size}pt` : undefined,
    color: style.color ?? undefined,
  };
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
  pinned,
}: {
  tip: Tip;
  mark: Mark | null;
  sheets: TooltipSheetInfo[];
  workbook: string;
  stateFor: (target: string, mark: Mark) => QueryState;
  /** 點選標記後釘住的工具提示：可以操作（命令按鈕、依類別選取） */
  pinned?: PinActions;
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
    if (pinned?.above) {
      // 動作選單在點選處的右下方：工具提示放上方；上方放不下時放左邊
      top = tip.y - height - 12;
      if (top < 4) {
        top = Math.max(4, tip.y - height / 2);
        left = Math.max(4, tip.x - width - 12);
      }
    }
    setPosition((current) => (current.left === left && current.top === top ? current : { left, top }));
  }, [tip.x, tip.y, mark, sheets, pinned?.above]);
  const lines = (
    tip.value !== null && typeof tip.value === "object"
      ? Object.entries(tip.value as Record<string, unknown>)
      : [["", tip.value] as [string, unknown]]
  ).filter(([key]) => !key.startsWith(CARD_KEY));
  // 依類別選取：釘住的工具提示中，維度的值可以點（選取這個值的所有標記）
  const category = (key: string | undefined, text: string, raw: Scalar | undefined) =>
    pinned?.titles && key !== undefined && raw !== undefined ? (
      <button type="button" className="link viz-tooltip-category" title="選取這個值的所有標記" onClick={() => pinned.onCategory(key, raw)}>
        {text}
      </button>
    ) : (
      text
    );
  return createPortal(
    <div ref={ref} className={pinned ? "viz-tooltip pinned" : "viz-tooltip"} role="tooltip" style={position} data-pinned={pinned ? "true" : "false"}>
      {pinned?.commands && (
        <div className="viz-tooltip-commands" role="toolbar" aria-label="標記的命令">
          <button type="button" onClick={() => pinned.onKeep("keep")}>
            ✓ 只保留
          </button>
          <button type="button" onClick={() => pinned.onKeep("exclude")}>
            ✕ 排除
          </button>
          {pinned.count > 1 && <span className="muted">{pinned.count} 個標記</span>}
        </div>
      )}
      {tip.rich ? (
        <div className="viz-tooltip-rich" style={{ textAlign: tip.rich.find((r) => r.style?.align)?.style?.align ?? undefined }}>
          {tip.rich.map((run, i) => (
            <span key={i} style={tooltipRunStyle(run.style)}>
              {category(run.key, run.text, run.raw)}
            </span>
          ))}
        </div>
      ) : (
        <table className="viz-tooltip-text">
          <tbody>
            {lines.map(([key, value]) => {
              const field = pinned?.titles?.[key];
              const raw = field !== undefined && tip.mark && field in tip.mark ? tip.mark[field] : undefined;
              return (
                <tr key={key}>
                  {key && <td className="key">{key}</td>}
                  <td>{category(field, String(value ?? ""), raw)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
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
    // 只標示選取（醒目提示）標記的標籤（後端以 __label_s 為名稱前綴、預設不顯示）
    // 選取標記時才顯示的下拉線（__drop_s）同樣只畫選取的標記
    if (typeof node.name === "string" && (node.name.startsWith("__label_s") || node.name.startsWith("__drop_s"))) node.transform = [{ filter: test }];
    if (node.mark && !isReferenceLayer(node))
      node.encoding = { ...(node.encoding ?? {}), opacity: { condition: { test, value: 1 }, value: 0.25 } };
  });
}

/** 重疊時隱藏的標記標籤（後端以 __label_c 為名稱前綴的圖層，每個標籤有 id：datum.__lid）的參數。 */
const LABEL_HIDDEN = "__label_hidden";

type Box = [number, number, number, number];

/**
 * 互相重疊的標記標籤（與 Tableau 相同，只顯示先畫的）：依畫好的位置，在每個群組（分面的每一格）中依資料
 * 的順序保留不與已保留的標籤重疊的標籤，回傳其餘標籤的 id。
 */
export function overlappingLabels(root: any): number[] {
  const hidden: number[] = [];
  const overlaps = (a: Box, b: Box) => a[0] < b[2] - 0.5 && b[0] < a[2] - 0.5 && a[1] < b[3] - 0.5 && b[1] < a[3] - 0.5;
  // 標籤在長條的長度方向（橫的長條為寬、直的為高）放得下
  const inside = (a: Box, b: Box, along: "x" | "y") =>
    along === "x" ? a[0] >= b[0] - 1 && a[2] <= b[2] + 1 : a[1] >= b[1] - 1 && a[3] <= b[3] + 1;
  const visit = (group: any) => {
    const labels: { id: number; box: Box; row: unknown; fit: "x" | "y" | null }[] = [];
    // 長條（堆疊的各段）：資料列號 → 畫好的範圍
    const bars = new Map<unknown, Box>();
    for (const mark of group?.items ?? []) {
      if (mark.marktype === "group") {
        for (const item of mark.items ?? []) visit(item);
        continue;
      }
      const name = String(mark.name ?? "");
      if (mark.marktype === "rect" && !name.startsWith("__")) {
        for (const item of mark.items ?? []) {
          const b = item.bounds;
          if (b && item.datum?.__row !== undefined) bars.set(item.datum.__row, [b.x1, b.y1, b.x2, b.y2]);
        }
        continue;
      }
      if (mark.marktype !== "text" || !(name.startsWith("__label_c") || name.startsWith("__label_i"))) continue;
      for (const item of mark.items ?? []) {
        const id = item.datum?.__lid;
        const b = item.bounds;
        const text = Array.isArray(item.text) ? item.text.join("") : item.text;
        if (typeof id !== "number" || !b || text === undefined || text === null || text === "" || b.x2 <= b.x1) continue;
        const fit = name.startsWith("__label_ix") ? "x" : name.startsWith("__label_iy") ? "y" : null;
        labels.push({ id, box: [b.x1, b.y1, b.x2, b.y2], row: item.datum?.__row, fit });
      }
    }
    // 堆疊等轉換會改變項目的順序：依資料的順序（標籤 id）決定先後
    labels.sort((a, b) => a.id - b.id);
    const kept: Box[] = [];
    for (const { id, box, row, fit } of labels) {
      // 堆疊長條各段內的標籤：放不下這一段時不顯示
      const bar = fit ? bars.get(row) : undefined;
      if ((fit && bar && !inside(box, bar, fit)) || kept.some((other) => overlaps(box, other))) hidden.push(id);
      else kept.push(box);
    }
  };
  for (const item of root?.items ?? []) visit(item);
  return hidden;
}

/** 隱藏重疊的標記標籤；圖的大小改變時（例如區塊縮放）重新計算。 */
function cullLabels(result: Result) {
  const view = result.view;
  let timer: number | undefined;
  let running = false;
  const run = async () => {
    if (running) return;
    running = true;
    try {
      view.signal(LABEL_HIDDEN, []);
      await view.runAsync();
      const hidden = overlappingLabels((view.scenegraph() as unknown as { root: unknown }).root);
      if (hidden.length) {
        view.signal(LABEL_HIDDEN, hidden);
        await view.runAsync();
      }
    } catch {
      // 圖已經移除（finalize）或沒有這個參數：略過
    } finally {
      running = false;
    }
  };
  view.addResizeListener(() => {
    window.clearTimeout(timer);
    timer = window.setTimeout(run, 100);
  });
  return run();
}

/** 資料列 → 標記（維度欄位鍵 → 原始值）。 */
function markOf(record: Record<string, unknown>, dimensions: Record<string, string>): Mark {
  const mark: Mark = {};
  // 另外查詢的標記卡（較粗的詳細程度、地圖的各標記圖層）的資料列只有它自己的欄位：沒有的欄位不列
  for (const [name, key] of Object.entries(dimensions)) if (name in record) mark[key] = (record[name] ?? null) as Scalar;
  return mark;
}

/**
 * 各標記的原始資料列（列號 __row → 資料列）：工作表的資料，以及另外查詢的標記卡、地圖的標記圖層自己的資料
 * （列號接在工作表的資料之後）。總計等不參與選取的列（列號 -1）不在其中，所以陣列中可能有空位。
 */
function rowRecords(spec: Spec): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  visitSpecs(spec, (node) => {
    const values = node.data?.values;
    if (!Array.isArray(values)) return;
    for (const record of values) {
      const row = record?.__row;
      if (typeof row === "number" && row >= 0 && records[row] === undefined) records[row] = record;
    }
  });
  return records;
}

/**
 * 分面（欄列架上的維度把圓餅分成多個小圖，encoding 的 row／column 或 facet）：容器寬高不適用於分面，
 * 依小圖的數量平分區塊的寬高（facet 時設定在內層的 spec）。
 */
function sizeFacets(spec: Record<string, any>, width: number, height: number) {
  const facet = spec.facet ?? spec.encoding ?? {};
  const column = facet.column?.field;
  const row = facet.row?.field;
  if (!column && !row) return;
  const values = (spec.data?.values ?? []) as Record<string, unknown>[];
  const count = (field?: string) => (field ? new Set(values.map((v) => v[field])).size || 1 : 1);
  const HEADER = 24; // 分面標題的高度
  const GAP = 16;
  const target = spec.facet && spec.spec ? spec.spec : spec;
  target.width = Math.max(Math.floor((width - 16) / count(column)) - GAP, 40);
  target.height = Math.max(Math.floor((height - HEADER) / count(row)) - HEADER - GAP, 40);
  spec.autosize = { type: "pad" };
  // 甜甜圈的洞以整張圖的寬高計算時（舊的規格），分面的小圖中改用固定的大小；以每一格的寬高
  // （child_width、child_height）計算的不變
  const hole = Math.floor(Math.min(target.width, target.height) * 0.3);
  visitSpecs(spec, (node) => {
    const expr = node.mark?.innerRadius?.expr;
    if (typeof expr === "string" && !expr.includes("child_")) node.mark = { ...node.mark, innerRadius: hole };
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
  showCaption,
  fit,
  onRendered,
  onMarks,
  selected,
  highlighted,
  revision = 0,
  onDrill,
  tooltipState,
  onViewport,
  onKeep,
  pinAbove,
  onAnnotationMove,
}: Props) {
  const chartRef = useRef<HTMLDivElement>(null);
  const onAnnotationMoveRef = useRef(onAnnotationMove);
  onAnnotationMoveRef.current = onAnnotationMove;
  const movable = onAnnotationMove !== undefined;
  const onKeepRef = useRef(onKeep);
  onKeepRef.current = onKeep;
  const canKeep = onKeep !== undefined;
  // 點選標記後釘住的工具提示（命令按鈕、依類別選取）；最近一次滑過的工具提示留著給它用
  const [pin, setPin] = useState<{ tip: Tip; marks: Mark[] } | null>(null);
  const lastTip = useRef<Tip | null>(null);
  const tipData = useRef<{ records: Record<string, unknown>[]; dimensions: Record<string, string>; titles: Record<string, string>; commands: boolean; select: boolean }>({
    records: [],
    dimensions: {},
    titles: {},
    commands: false,
    select: false,
  });
  const onViewportRef = useRef(onViewport);
  onViewportRef.current = onViewport;
  // 地圖的平移、縮放：檢視器中只在這個檢視（重新繪製時帶著顯示範圍）；編輯器由呼叫端存進工作表
  const [viewport, setViewport] = useState<Viewport | null>(null);
  const [zoomable, setZoomable] = useState(false);
  const viewRef = useRef<Result | null>(null);
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
  // 沒有動作的工作表：釘住工具提示的標記也醒目顯示
  const selectedKey = JSON.stringify(selected ?? pin?.marks ?? []);
  const highlightKey = JSON.stringify(highlighted ?? null);
  const [response, setResponse] = useState<RenderResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showTable, setShowTable] = useState(false);
  const stateKey = JSON.stringify(viewport && !onViewport ? { ...state, viewport } : state);
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
    const records = rowRecords(original);
    /** 工作表的資料的列數：列號在它之後的是另外查詢的標記卡（較粗的詳細程度、地圖的標記圖層）的資料列 */
    const ownFrom = Array.isArray(original.data?.values) ? original.data.values.length : 0;
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
            // 選取的標記中這張工作表有的欄位都要相同；資料列沒有那個欄位（別的標記圖層的標記）時不算
            marks.some((m) => Object.keys(m).every((k) => !present.has(k) || (k in mark && mark[k] === m[k]))) ||
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
    // 自訂的工具提示（文字與格式，或不顯示工具提示）也用自己的工具提示
    const custom = spec.usermeta?.tooltip as TooltipTemplate | undefined;
    // 命令按鈕與依類別選取（檢視器）：點選標記後工具提示釘住，可以操作
    const commands = canKeep && custom?.commands !== false;
    const select = canKeep && custom?.select !== false;
    const pinnable = (commands || select) && custom?.show !== false;
    tipData.current = { records, dimensions, titles: tooltipTitles(spec, dimensions), commands, select };
    let hoverTimer: number | undefined;
    let hoverItem: unknown = null;
    const tooltip = (_handler: unknown, event: MouseEvent, item: any, value: unknown) => {
      if (value === null || value === undefined || value === "" || custom?.show === false) {
        window.clearTimeout(hoverTimer);
        hoverItem = null;
        lastTip.current = null;
        setTip(null);
        return;
      }
      const row = item?.datum?.__row;
      const record = typeof row === "number" ? records[row] : undefined;
      const mark = record ? markOf(record, dimensions) : null;
      // 各標記卡各自的文字：這一層的工具提示欄位清單帶著標記卡的記號
      const card = typeof value === "object" ? Object.keys(value as object).find((k) => k.startsWith(CARD_KEY)) : undefined;
      // 另外查詢的標記卡沒有自己的文字時不套用工作表的文字（欄位不同），用它自己的欄位清單
      const own = typeof row === "number" && row >= ownFrom;
      const runs = (card !== undefined ? custom?.cards?.[card.slice(CARD_KEY.length)] : undefined) ?? (own ? [] : (custom?.runs ?? []));
      const rich = runs.length > 0 && record ? richTooltip(runs, record, dimensions) : null;
      const next: Tip = { x: event.clientX, y: event.clientY, value, mark, rich };
      lastTip.current = next;
      if (custom?.mode !== "hover") {
        setTip(next);
        return;
      }
      // 暫留時：停在同一個標記上一會兒才顯示，顯示之後留在原位
      if (item === hoverItem) return;
      hoverItem = item;
      window.clearTimeout(hoverTimer);
      setTip(null);
      hoverTimer = window.setTimeout(() => setTip(next), HOVER_DELAY);
    };
    // 畫好之前（例如地圖的形狀還在下載）標記為未完成：匯出、訂閱的截圖等它完成
    element.dataset.drawn = "false";
    // 地圖：投影改成可以平移、縮放（mapZoom）
    let zoomed = false;
    const patch = (vg: any) => {
      zoomed = zoomableMap(vg);
      return vg;
    };
    let viewTimer: number | undefined;
    /** 平移、縮放過（放開滑鼠時不當成點選標記） */
    let moved = false;
    embed(element, spec as any, { actions: false, renderer: "svg", patch, ...(vizTooltips || custom || pinnable ? { tooltip } : {}) })
      .then(async (result) => {
        // 重疊的標記標籤隱藏之後才算畫好（匯出、訂閱的截圖等它）
        if ((spec.params ?? []).some((p: Spec) => p.name === LABEL_HIDDEN)) await cullLabels(result);
        element.dataset.drawn = "true";
        if (disposed) {
          result.finalize();
          return;
        }
        view = result;
        viewRef.current = result;
        setZoomable(zoomed);
        element.dataset.zoomable = zoomed ? "true" : "false";
        if (zoomed) {
          result.view.addEventListener("mousedown", () => {
            moved = false;
          });
          result.view.addSignalListener(VIEW_SIGNAL, (_name: string, value: ZoomState) => {
            moved = true;
            window.clearTimeout(viewTimer);
            if (value.z === 1 && value.x === 0 && value.y === 0) return;
            // 停止操作後以畫面中的範圍重新繪製（底圖的細節、地名依新的縮放層級）
            viewTimer = window.setTimeout(() => {
              if (disposed) return;
              const box = visibleBox(fitOf(result.view), value);
              if (onViewportRef.current) onViewportRef.current(box);
              else setViewport(box);
            }, VIEWPORT_DELAY);
          });
        }
        // 捲動時與 Tableau 相同，欄標題、座標軸與列標題固定在區塊邊緣
        freezeHeaders(element, result, scroll, (original.usermeta?.background as string | undefined) ?? undefined);
        if (movable) {
          // 註解的文字可以拖曳（改變文字相對於標記或點的位置）；放開時回報移動的像素
          element.dataset.annotations = "movable";
          result.view.addEventListener("mousedown", (event: any, item: any) => {
            const found = /^__deco_annotation(\d+)_marks$/.exec(String(item?.mark?.name ?? ""));
            if (!found) return;
            event.preventDefault?.();
            const index = Number(found[1]);
            const startX = event.clientX;
            const startY = event.clientY;
            const node = (event.target as Element | null)?.closest?.("text") as SVGElement | null;
            const drag = (move: MouseEvent) => {
              if (node) node.style.transform = `translate(${move.clientX - startX}px, ${move.clientY - startY}px)`;
            };
            const drop = (up: MouseEvent) => {
              document.removeEventListener("mousemove", drag);
              document.removeEventListener("mouseup", drop);
              const dx = Math.round(up.clientX - startX);
              const dy = Math.round(up.clientY - startY);
              if (Math.abs(dx) + Math.abs(dy) > 2) {
                moved = true; // 拖曳之後放開滑鼠不算點選
                onAnnotationMoveRef.current?.(index, dx, dy);
              }
              // 位置由重新繪製的圖決定（區域的註解不能拖曳，放回原位）
              if (node) node.style.transform = "";
            };
            document.addEventListener("mousemove", drag);
            document.addEventListener("mouseup", drop);
          });
        }
        if (!interactive && !pinnable) return;
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
          if (moved) {
            // 拖曳平移之後放開滑鼠：不是點選
            moved = false;
            return;
          }
          const mark = markAt(item);
          if (mark === undefined) return;
          if (pinnable) {
            // 釘住這個標記的工具提示（按住 Ctrl／⌘／Shift 加入或移除標記）
            const additive = Boolean(event?.ctrlKey || event?.metaKey || event?.shiftKey);
            const last = lastTip.current;
            setPin((current) => {
              const marks = nextSelection(current?.marks ?? [], mark, additive);
              if (!mark || marks.length === 0) return null;
              const base = last?.mark && sameMark(last.mark, mark) ? last : { value: mark, mark, rich: null };
              return { tip: { ...base, x: event.clientX, y: event.clientY }, marks };
            });
          }
          if (interactive) emit("click", event, mark, item);
        });
        if (!interactive) return;
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
      .catch((err: unknown) => {
        element.dataset.drawn = "true";
        setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      disposed = true;
      window.clearTimeout(leaveTimer);
      window.clearTimeout(viewTimer);
      window.clearTimeout(hoverTimer);
      view?.finalize();
      viewRef.current = null;
      setTip(null);
    };
  }, [response, hideLegend, showTable, interactive, selectedKey, highlightKey, fit, vizTooltips, canKeep, movable]);

  // 地圖的選取工具（矩形、半徑、套索）：選了工具之後在圖上拖曳，範圍內的標記都選取
  const [tool, setTool] = useState<SelectTool | null>(null);
  const [region, setRegion] = useState<[number, number][] | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const startSelection = (event: React.MouseEvent<HTMLDivElement>) => {
    const body = bodyRef.current;
    const chart = chartRef.current;
    if (!tool || !body || !chart || event.button !== 0) return;
    event.preventDefault();
    const origin = body.getBoundingClientRect();
    const start: [number, number] = [event.clientX, event.clientY];
    let path: [number, number][] = [start];
    const local = (points: [number, number][]) => points.map(([x, y]) => [x - origin.left, y - origin.top] as [number, number]);
    const move = (e: MouseEvent) => {
      path = tool === "lasso" ? [...path, [e.clientX, e.clientY]] : [start, [e.clientX, e.clientY]];
      setRegion(local(path));
    };
    const up = (e: MouseEvent) => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      setRegion(null);
      const end: [number, number] = [e.clientX, e.clientY];
      const inside = regionTest(tool, tool === "lasso" ? path : [start, end]);
      const { records, dimensions } = tipData.current;
      const marks: Mark[] = [];
      // Vega 的 SVG 元素帶著場景圖的項目（__data__）；固定標題的複本沒有，不會重複計算
      chart.querySelectorAll<SVGGraphicsElement>("svg .role-mark > *").forEach((node) => {
        const row = (node as unknown as { __data__?: { datum?: { __row?: unknown } } }).__data__?.datum?.__row;
        if (typeof row !== "number" || row < 0 || !records[row]) return;
        const box = node.getBoundingClientRect();
        if (box.width === 0 && box.height === 0) return;
        if (!inside((box.left + box.right) / 2, (box.top + box.bottom) / 2)) return;
        const mark = markOf(records[row], dimensions);
        if (Object.keys(mark).length > 0 && !marks.some((m) => sameMark(m, mark))) marks.push(mark);
      });
      const pinnable = tipData.current.commands || tipData.current.select;
      setPin(
        marks.length > 0 && pinnable
          ? { tip: { x: end[0], y: end[1], value: { 選取: `${marks.length} 個標記` }, mark: null, rich: null }, marks }
          : null,
      );
      onMarksRef.current?.({ kind: "click", mark: marks[0] ?? null, marks, additive: false, x: end[0], y: end[1] });
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  };

  // 釘住的工具提示：資料重新查詢、按 Esc 或點別的地方時放開
  useEffect(() => setPin(null), [response]);
  const pinned = pin !== null;
  useEffect(() => {
    if (!pinned) return;
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent) {
        if (event.key === "Escape") setPin(null);
        return;
      }
      const target = event.target as Element | null;
      if (target?.closest(".viz-tooltip, .select-overlay") || (chartRef.current && target && chartRef.current.contains(target))) return;
      setPin(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [pinned]);
  /** 依類別選取：這個維度的值相同的所有標記 */
  const selectCategory = (key: string, value: Scalar) => {
    const { records, dimensions } = tipData.current;
    const marks: Mark[] = [];
    for (const record of records) {
      if (!record) continue; // 總計等不參與選取的列
      const mark = markOf(record, dimensions);
      if (mark[key] === value && !marks.some((m) => sameMark(m, mark))) marks.push(mark);
    }
    if (marks.length === 0 || !pin) return;
    setPin({ tip: pin.tip, marks });
    onMarksRef.current?.({ kind: "click", mark: marks[0], marks, additive: false, x: pin.tip.x, y: pin.tip.y });
  };

  /** 地圖工具列：以畫面中央放大、縮小，或重設地圖（依標記的位置自動縮放）。 */
  const zoomBy = (factor: number) => {
    const current = viewRef.current?.view;
    if (!current) return;
    const value = current.signal(VIEW_SIGNAL) as ZoomState;
    const next = zoomAt(value, factor, Number(current.signal("width")) / 2, Number(current.signal("height")) / 2);
    void current.signal(VIEW_SIGNAL, next).runAsync();
  };
  const resetMap = () => {
    const current = viewRef.current?.view;
    if (current) void current.signal(VIEW_SIGNAL, { z: 1, x: 0, y: 0 }).runAsync();
    if (onViewportRef.current) onViewportRef.current(null);
    else setViewport(null);
  };

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
          onClick={() => {
            setPin(null);
            onMarks?.({ kind: "click", mark: null, additive: false, x: 0, y: 0 });
          }}
        >
          已選取 {count} 個標記 ✕
        </button>
      )}
      {onKeep && (state.marks?.length ?? 0) > 0 && (
        <button type="button" className="link kept" title="清除只保留與排除" onClick={() => onKeep("clear", [])}>
          已只保留／排除 ✕
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
        <div className="worksheet-header" style={{ background: usermeta.title_background }}>
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
      <div className="worksheet-body" ref={bodyRef}>
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
        {zoomable && !showTable && !error && (
          <div className="map-tools" role="toolbar" aria-label="地圖工具列">
            <button type="button" title="放大（滾輪、連按兩下）" aria-label="放大" onClick={() => zoomBy(2)}>
              +
            </button>
            <button type="button" title="縮小（滾輪、Shift 連按兩下）" aria-label="縮小" onClick={() => zoomBy(0.5)}>
              −
            </button>
            <button type="button" title="重設地圖（依標記的位置自動縮放）" aria-label="重設地圖" onClick={resetMap}>
              ⌂
            </button>
            {(interactive || canKeep) &&
              SELECT_TOOLS.map(([kind, icon, label]) => (
                <button
                  key={kind}
                  type="button"
                  title={`${label}：在地圖上拖曳，範圍內的標記都選取；再按一次回到平移`}
                  aria-label={label}
                  aria-pressed={tool === kind}
                  className={tool === kind ? "active" : ""}
                  onClick={() => setTool(tool === kind ? null : kind)}
                >
                  {icon}
                </button>
              ))}
          </div>
        )}
        {tool && zoomable && !showTable && !error && (
          <div className="select-overlay" data-tool={tool} onMouseDown={startSelection}>
            {region && region.length > 1 && (
              <svg aria-hidden="true">
                {tool === "rect" && (
                  <rect
                    x={Math.min(region[0][0], region[1][0])}
                    y={Math.min(region[0][1], region[1][1])}
                    width={Math.abs(region[1][0] - region[0][0])}
                    height={Math.abs(region[1][1] - region[0][1])}
                  />
                )}
                {tool === "radius" && <circle cx={region[0][0]} cy={region[0][1]} r={Math.hypot(region[1][0] - region[0][0], region[1][1] - region[0][1])} />}
                {tool === "lasso" && <polygon points={region.map(([x, y]) => `${x},${y}`).join(" ")} />}
              </svg>
            )}
          </div>
        )}
        {loading && !response && <div className="placeholder">載入中…</div>}
      </div>
      {response?.caption && (showCaption ?? response.caption_shown === true) && (
        <div className="worksheet-caption" role="note" aria-label="說明">
          {response.caption}
        </div>
      )}
      {response?.summary_shown && response.table && !hideLegend && <SummaryCard table={response.table} marks={selected ?? pin?.marks ?? []} />}
      {pin ? (
        <VizTooltip
          tip={pin.tip}
          mark={null}
          sheets={[]}
          workbook={workbook}
          stateFor={() => ({ filters: {}, parameters: {} })}
          pinned={{
            count: pin.marks.length,
            commands: tipData.current.commands,
            titles: tipData.current.select ? tipData.current.titles : null,
            above: pinAbove,
            onKeep: (mode) => {
              onKeepRef.current?.(mode, pin.marks);
              setPin(null);
            },
            onCategory: selectCategory,
          }}
        />
      ) : (
        tip && (
          <VizTooltip
            tip={tip}
            mark={tipMark}
            sheets={tooltipSheets}
            workbook={workbook}
            stateFor={(target, mark) => tooltipStateRef.current?.(target, mark) ?? { filters: {}, parameters: {} }}
          />
        )
      )}
    </div>
  );
}
