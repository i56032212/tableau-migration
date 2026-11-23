// 標記的動畫（Tableau 2020.1 起的「格式＞動畫」）：篩選、排序、頁面、參數與動作改變檢視之後，標記從原本的位置
// 移到新的位置。Vega 沒有轉場：重畫之前記下每個標記（依它的維度值認得同一個標記）在畫面上的外框與顏色，新的圖
// 畫好之後，同一個標記先以 transform 放回原本的位置、再移到新的位置（FLIP）；新出現的標記淡入，消失的標記以
// 原本的樣子淡出。同時：三者一起；依序：先淡出、再移動、最後淡入（Tableau：「fades out marks, moves and sorts
// them, then fades them in」），每一段各占設定的持續時間（推定）。
// - 折線與區域（整條是一個路徑）：依系列（色彩等分開線的維度，與所在的窗格）認得同一條線、依標記的身分認得線上的
//   同一個點，路徑的形狀（d）從原本的點內插到新的點；新出現的點從原本的線上相鄰的點長出來、消失的點縮到新的線上
//   相鄰的點。新出現的線淡入、消失的線淡出
// - 連續的座標軸（數字、日期；Tableau 說明中的「axis animation」）：同一個值的刻度、刻度文字與格線移到新的位置；
//   新出現的從原本的比例尺上的位置移過來並淡入，消失的移到新的比例尺上的位置並淡出。離散的座標軸（標題）與
//   Tableau 相同不動
// 與 Tableau 相同不做動畫的：地圖（形狀）、圓餅、文字標記、標題、參考線與趨勢線（Tableau 也不做）；標記的標籤在
// 動畫結束前淡入。

/** IR 的動畫設定（活頁簿預設或一張工作表；工作表上沒有設定的項目沿用活頁簿的預設） */
export interface AnimationSetting {
  on?: boolean | null;
  /** 秒 */
  duration?: number | null;
  style?: "simultaneous" | "sequential" | null;
}

/** 一張工作表實際的動畫：持續時間（毫秒）與方式 */
export interface MarkAnimation {
  duration: number;
  style: "simultaneous" | "sequential";
}

/** 沒有設定持續時間時（秒；Tableau 的說明沒有寫預設的長度，推定） */
export const DEFAULT_DURATION = 0.3;

/** 這張工作表的動畫：工作表的設定優先、沒有的沿用活頁簿的預設；關掉時為 null。 */
export function effectiveAnimation(workbook?: AnimationSetting | null, sheet?: AnimationSetting | null): MarkAnimation | null {
  const on = sheet?.on ?? workbook?.on ?? false;
  if (!on) return null;
  const seconds = sheet?.duration ?? workbook?.duration ?? DEFAULT_DURATION;
  return {
    duration: Math.round(Math.max(0, Math.min(10, seconds)) * 1000),
    style: sheet?.style ?? workbook?.style ?? "simultaneous",
  };
}

/** 報表中這張工作表的動畫（IR 的活頁簿預設與工作表的設定）。 */
export function animationFor(
  ir: { workbook: { animation?: AnimationSetting | null }; worksheets: { id: string; animation?: AnimationSetting | null }[] } | null | undefined,
  worksheet: string,
): MarkAnimation | null {
  if (!ir) return null;
  return effectiveAnimation(ir.workbook.animation, ir.worksheets.find((w) => w.id === worksheet)?.animation);
}

/** 系統設定減少動態效果時不做動畫 */
export function motionAllowed(): boolean {
  try {
    return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return true;
  }
}

/** Vega 場景圖的項目（畫好的 SVG 元素的 __data__） */
interface SceneItem {
  datum?: Record<string, unknown>;
  mark?: { name?: string; marktype?: string; items?: SceneItem[]; group?: SceneItem };
  x?: number;
  y?: number;
  x2?: number;
  y2?: number;
  interpolate?: string;
  orient?: string;
}

/** 一個標記的身分（同一個標記重畫前後相同）；不做動畫的標記為 null */
export type MarkKey = (item: SceneItem) => string | null;

interface Snapshot {
  /** 外框（相對於圖的區塊的內容，含捲動） */
  box: DOMRect;
  fill: string | null;
  /** 淡出用：原本的元素的複本與它在 SVG 中的變換 */
  copy: Element;
  matrix: DOMMatrix | null;
}

/** 折線、區域的一條線：線上各點（身分 → 圖的區塊內容中的座標；區域另有另一邊的座標） */
interface PathSnapshot {
  order: string[];
  points: Map<string, number[]>;
  copy: Element;
  matrix: DOMMatrix | null;
}

/** 連續座標軸上的一個刻度（刻度文字、刻度線或格線） */
interface TickSnapshot {
  value: number;
  /** 元素的原點在圖的區塊內容中的位置 */
  at: [number, number];
  copy: Element;
  matrix: DOMMatrix | null;
}

/** 連續的座標軸：方向與刻度（種類與值 → 位置） */
interface AxisSnapshot {
  horizontal: boolean;
  ticks: Map<string, TickSnapshot>;
}

/** 重畫之前的圖：標記（身分 → 位置與樣子）、折線與區域（系列 → 線上的點）、連續的座標軸 */
export interface MarkSnapshot {
  marks: Map<string, Snapshot>;
  paths: Map<string, PathSnapshot>;
  axes: Map<string, AxisSnapshot>;
}

/** 移動的標記：長條（rect）與點、方塊、形狀（symbol） */
const MOVING = new Set(["rect", "symbol"]);

const itemOf = (element: Element) => (element as unknown as { __data__?: SceneItem }).__data__;

function mainSvg(chart: HTMLElement): SVGSVGElement | null {
  return chart.querySelector<SVGSVGElement>(":scope > svg, :scope > .frozen-host > svg");
}

/** 圖的區塊內容中的外框（捲動也算進去：重畫前後捲動的位置不同也對得上） */
function boxIn(chart: HTMLElement, element: Element): DOMRect {
  const area = chart.getBoundingClientRect();
  const r = element.getBoundingClientRect();
  return new DOMRect(r.left - area.left + chart.scrollLeft, r.top - area.top + chart.scrollTop, r.width, r.height);
}

/** 元素自己的座標 → 圖的區塊內容中的座標 */
function toChart(chart: HTMLElement, element: Element, x: number, y: number): [number, number] {
  const matrix = (element as SVGGraphicsElement).getScreenCTM?.();
  if (!matrix) return [x, y];
  const p = new DOMPoint(x, y).matrixTransform(matrix);
  const area = chart.getBoundingClientRect();
  return [p.x - area.left + chart.scrollLeft, p.y - area.top + chart.scrollTop];
}

/** 圖的區塊內容中的座標 → 元素自己的座標 */
function fromChart(chart: HTMLElement, element: Element, x: number, y: number): [number, number] {
  const matrix = (element as SVGGraphicsElement).getScreenCTM?.();
  if (!matrix) return [x, y];
  const area = chart.getBoundingClientRect();
  const p = new DOMPoint(x + area.left - chart.scrollLeft, y + area.top - chart.scrollTop).matrixTransform(matrix.inverse());
  return [p.x, p.y];
}

/** 淡出用的複本：拿掉元素自己的 transform（含在 matrix 裡） */
function ghostOf(element: Element): { copy: Element; matrix: DOMMatrix | null } {
  const copy = element.cloneNode(true) as Element;
  copy.removeAttribute("transform");
  return { copy, matrix: (element as SVGGraphicsElement).getCTM?.() ?? null };
}

/** 會移動的標記元素與它的身分 */
function movingMarks(chart: HTMLElement, keyOf: MarkKey): [Element, string][] {
  const svg = mainSvg(chart);
  if (!svg) return [];
  const out: [Element, string][] = [];
  const seen = new Set<string>();
  svg.querySelectorAll("g.role-mark > path, g.role-mark > rect").forEach((element) => {
    const item = itemOf(element);
    if (!item || !MOVING.has(item.mark?.marktype ?? "")) return;
    const key = keyOf(item);
    if (key === null || seen.has(key)) return;
    seen.add(key);
    out.push([element, key]);
  });
  return out;
}

/** 線所在的系列：外層各群組（色彩等分開線的維度、窗格）的值 */
function seriesOf(group: SceneItem | undefined): string {
  const parts: string[] = [];
  for (let g = group, depth = 0; g && depth < 20; g = g.mark?.group, depth++) {
    const datum = g.datum ?? {};
    const keys = Object.keys(datum)
      .filter((k) => k !== "count" && !k.startsWith("__"))
      .sort();
    parts.push(JSON.stringify(keys.map((k) => [k, datum[k]])));
  }
  return parts.join("/");
}

interface SeriesPath {
  element: SVGPathElement;
  key: string;
  area: boolean;
  interpolate: string;
  /** 線上的點：身分與元素自己的座標（區域：[x, y, x2, y2]） */
  points: [string, number[]][];
}

/** 折線與區域的線（每一條是一個路徑），以系列認得；點沒有身分的線（參考線等）不算 */
function seriesPaths(chart: HTMLElement, keyOf: MarkKey): SeriesPath[] {
  const svg = mainSvg(chart);
  if (!svg) return [];
  const out: SeriesPath[] = [];
  const seen = new Set<string>();
  svg.querySelectorAll<SVGPathElement>("g.mark-line.role-mark > path, g.mark-area.role-mark > path").forEach((element) => {
    const item = itemOf(element);
    const mark = item?.mark;
    if (!mark || (mark.marktype !== "line" && mark.marktype !== "area")) return;
    const area = mark.marktype === "area";
    const points: [string, number[]][] = [];
    for (const point of mark.items ?? []) {
      const key = keyOf(point);
      const x = Number(point.x);
      const y = Number(point.y);
      if (key === null || !Number.isFinite(x) || !Number.isFinite(y)) return;
      if (!area) {
        points.push([key, [x, y]]);
        continue;
      }
      // 區域：直的（y2 是基線）或橫的（x2 是基線）
      const horizontal = point.orient === "horizontal" || (typeof point.y2 !== "number" && typeof point.x2 === "number");
      const x2 = horizontal ? Number(point.x2) : x;
      const y2 = horizontal ? y : Number(point.y2 ?? y);
      points.push([key, [x, y, Number.isFinite(x2) ? x2 : x, Number.isFinite(y2) ? y2 : y]]);
    }
    if (points.length === 0) return;
    const key = `${mark.marktype}|${mark.name ?? ""}|${seriesOf(mark.group)}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ element, key, area, interpolate: (mark.items?.[0]?.interpolate ?? "linear") || "linear", points });
  });
  return out;
}

/** 刻度的值：數字或日期（毫秒）；離散的值（標題）為 null */
function tickValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (value instanceof Date && Number.isFinite(value.getTime())) return value.getTime();
  return null;
}

const TICK_PARTS: [string, string][] = [
  ["label", "g.role-axis-label > text"],
  ["tick", "g.role-axis-tick > line"],
  ["grid", "g.role-axis-grid > line"],
];

interface AxisElements {
  key: string;
  horizontal: boolean;
  ticks: { element: Element; key: string; value: number }[];
}

/** 連續的座標軸（刻度的值是數字或日期）：依在圖中的順序與方向認得 */
function continuousAxes(chart: HTMLElement): AxisElements[] {
  const svg = mainSvg(chart);
  if (!svg) return [];
  const out: AxisElements[] = [];
  let index = 0;
  svg.querySelectorAll("g.role-axis").forEach((axis) => {
    const ticks: AxisElements["ticks"] = [];
    for (const [part, selector] of TICK_PARTS) {
      axis.querySelectorAll(selector).forEach((element) => {
        const value = tickValue(itemOf(element)?.datum?.value);
        if (value !== null) ticks.push({ element, key: `${part}|${value}`, value });
      });
    }
    if (ticks.length === 0) return;
    // 方向：刻度的原點沿哪一個方向排開
    const at = ticks.map((t) => toChart(chart, t.element, 0, 0));
    const spread = (i: 0 | 1) => Math.max(...at.map((p) => p[i])) - Math.min(...at.map((p) => p[i]));
    const horizontal = spread(0) >= spread(1);
    out.push({ key: `${index++}|${horizontal ? "x" : "y"}`, horizontal, ticks });
  });
  return out;
}

/** 重畫之前記下標記、折線與區域、連續座標軸的位置與樣子。 */
export function snapshotMarks(chart: HTMLElement, keyOf: MarkKey): MarkSnapshot {
  const marks = new Map<string, Snapshot>();
  for (const [element, key] of movingMarks(chart, keyOf)) {
    const box = boxIn(chart, element);
    if (box.width === 0 && box.height === 0) continue;
    marks.set(key, { box, fill: getComputedStyle(element).fill || null, ...ghostOf(element) });
  }
  const paths = new Map<string, PathSnapshot>();
  for (const series of seriesPaths(chart, keyOf)) {
    const points = new Map<string, number[]>();
    for (const [key, p] of series.points) {
      const a = toChart(chart, series.element, p[0], p[1]);
      points.set(key, p.length === 4 ? [...a, ...toChart(chart, series.element, p[2], p[3])] : a);
    }
    paths.set(series.key, { order: series.points.map(([key]) => key), points, ...ghostOf(series.element) });
  }
  const axes = new Map<string, AxisSnapshot>();
  for (const axis of continuousAxes(chart)) {
    const ticks = new Map<string, TickSnapshot>();
    for (const tick of axis.ticks) ticks.set(tick.key, { value: tick.value, at: toChart(chart, tick.element, 0, 0), ...ghostOf(tick.element) });
    axes.set(axis.key, { horizontal: axis.horizontal, ticks });
  }
  return { marks, paths, axes };
}

/** 元素本身的 transform 屬性只有平移時的 x、y；有其他變換時為 null（不移動它） */
function ownTranslate(element: Element): [number, number] | null {
  const value = (element.getAttribute("transform") ?? "").trim();
  if (value === "") return [0, 0];
  const found = /^translate\(\s*(-?[\d.e+-]+)(?:[\s,]+(-?[\d.e+-]+))?\s*\)$/.exec(value);
  return found ? [Number(found[1]), Number(found[2] ?? 0)] : null;
}

/** 數字寫進路徑（去掉多餘的小數） */
const num = (n: number) => String(Math.round(n * 1000) / 1000);

/** 一段線經過的點（依內插方式補上階梯的轉角；與 Vega 的 step、step-before、step-after 相同） */
function linePoints(points: [number, number][], interpolate: string): [number, number][] {
  if (!interpolate.startsWith("step") || points.length < 2) return points;
  const out: [number, number][] = [points[0]];
  for (let i = 1; i < points.length; i++) {
    const [ax, ay] = points[i - 1];
    const [bx, by] = points[i];
    if (interpolate === "step-after") out.push([bx, ay]);
    else if (interpolate === "step-before") out.push([ax, by]);
    else out.push([(ax + bx) / 2, ay], [(ax + bx) / 2, by]);
    out.push([bx, by]);
  }
  return out;
}

/** 折線（[x, y]）或區域（[x, y, x2, y2]：先沿著線、再沿著基線回來）的路徑 */
function pathData(points: number[][], area: boolean, interpolate: string): string {
  const top = linePoints(
    points.map((p) => [p[0], p[1]]),
    interpolate,
  );
  const all = area
    ? [
        ...top,
        ...linePoints(
          points.map((p) => [p[2], p[3]] as [number, number]),
          interpolate,
        ).reverse(),
      ]
    : top;
  return all.map(([x, y], i) => `${i === 0 ? "M" : "L"}${num(x)},${num(y)}`).join("") + (area ? "Z" : "");
}

/**
 * 原本與新的線上的點一一配對（兩端的點數相同才能內插路徑）：依新的順序，原本的點中消失的插在它原本的位置。
 * 新出現的點從原本的線上前一個點（沒有時後一個）長出來；消失的點縮到新的線上前一個點（沒有時後一個）。
 */
function pairPoints(old: { order: string[]; points: Map<string, number[]> }, now: [string, number[]][]): { from: number[]; to: number[] }[] {
  const index = new Map(old.order.map((key, i) => [key, i]));
  const kept = new Set(now.map(([key]) => key));
  const entries: { from?: number[]; to?: number[] }[] = [];
  let next = 0;
  const flush = (until: number) => {
    for (; next < until; next++) {
      const key = old.order[next];
      if (!kept.has(key)) entries.push({ from: old.points.get(key) });
    }
  };
  for (const [key, point] of now) {
    const i = index.get(key);
    if (i !== undefined) {
      flush(i);
      next = Math.max(next, i + 1);
      entries.push({ from: old.points.get(key), to: point });
    } else entries.push({ to: point });
  }
  flush(old.order.length);
  const fill = (side: "from" | "to") => {
    const known = entries.map((e) => e[side]);
    for (let i = 0; i < entries.length; i++) {
      if (known[i]) continue;
      let found: number[] | undefined;
      for (let j = i - 1; j >= 0 && !found; j--) found = known[j];
      for (let j = i + 1; j < known.length && !found; j++) found = known[j];
      entries[i][side] = found;
    }
  };
  fill("from");
  fill("to");
  return entries.filter((e): e is { from: number[]; to: number[] } => Boolean(e.from && e.to));
}

/** 座標軸上某個值在一組刻度（值與沿著座標軸的位置）中的位置：相鄰兩個刻度之間線性內插、兩端外插 */
function positionOf(value: number, ticks: [number, number][]): number | null {
  const sorted = [...new Map(ticks.map(([v, p]) => [v, p])).entries()].sort((a, b) => a[0] - b[0]);
  if (sorted.length < 2) return null;
  let i = sorted.findIndex(([v]) => v >= value);
  if (i === -1) i = sorted.length - 1;
  else if (i === 0) i = 1;
  const [v0, p0] = sorted[i - 1];
  const [v1, p1] = sorted[i];
  return v1 === v0 ? p0 : p0 + ((value - v0) / (v1 - v0)) * (p1 - p0);
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** 進行中的動畫：cancel 立刻停下（圖又要重畫時） */
export interface RunningAnimation {
  cancel: () => void;
  finished: Promise<void>;
}

/** 淡出（以原本的樣子疊在圖上）的元素：複本、變換，與另外要移動的距離（圖的區塊內容中的像素） */
interface Ghost {
  copy: Element;
  matrix: DOMMatrix | null;
  move?: [number, number];
  /** 第幾段（依序時）：0 淡出、1 移動 */
  phase: 0 | 1;
}

/**
 * 新的圖畫好之後：與重畫之前的標記、線與座標軸比對，移動、淡入、淡出。回傳進行中的動畫；沒有可以動的東西時為
 * null。進行中圖的區塊標著 data-animating。
 */
export function animateMarks(chart: HTMLElement, before: MarkSnapshot, keyOf: MarkKey, setting: MarkAnimation): RunningAnimation | null {
  const svg = mainSvg(chart);
  const empty = before.marks.size === 0 && before.paths.size === 0 && before.axes.size === 0;
  if (!svg || setting.duration <= 0 || empty || !motionAllowed()) return null;
  const duration = setting.duration;
  const sequential = setting.style === "sequential";
  // 依序：淡出（0）、移動（1）、淡入（2）各占一段；同時：都從 0 開始
  const phase = (n: number) => (sequential ? n * duration : 0);
  const easing = "ease-in-out";
  const animations: Animation[] = [];
  const ghosts: Ghost[] = [];

  // 標記：移動（FLIP）、淡入；消失的淡出
  const now = new Map(movingMarks(chart, keyOf).map(([element, key]) => [key, element] as const));
  for (const [key, element] of now) {
    const old = before.marks.get(key);
    if (!old) {
      // 新出現的標記：淡入
      animations.push(element.animate([{ opacity: 0 }, { opacity: 1 }], { duration, delay: phase(2), easing, fill: "backwards" }));
      continue;
    }
    const own = ownTranslate(element);
    const box = boxIn(chart, element);
    if (!own || box.width === 0 || box.height === 0) continue;
    const local = (element as SVGGraphicsElement).getBBox();
    const sx = old.box.width / box.width;
    const sy = old.box.height / box.height;
    // 新的外框對應到原本的外框：先縮放（以元素自己的座標原點），再平移
    const tx = old.box.left - box.left + own[0] + local.x * (1 - sx);
    const ty = old.box.top - box.top + own[1] + local.y * (1 - sy);
    const end = `translate(${own[0]}px, ${own[1]}px)`;
    const start = `translate(${tx}px, ${ty}px) scale(${sx}, ${sy})`;
    if (Math.abs(tx - own[0]) < 0.5 && Math.abs(ty - own[1]) < 0.5 && Math.abs(sx - 1) < 0.01 && Math.abs(sy - 1) < 0.01 && old.fill === getComputedStyle(element).fill) continue;
    const fill = getComputedStyle(element).fill;
    const frames: Keyframe[] = [
      { transform: start, ...(old.fill && old.fill !== fill ? { fill: old.fill } : {}) },
      { transform: end, ...(old.fill && old.fill !== fill ? { fill } : {}) },
    ];
    animations.push(element.animate(frames, { duration, delay: phase(1), easing, fill: "backwards" }));
  }
  for (const [key, old] of before.marks) if (!now.has(key)) ghosts.push({ copy: old.copy, matrix: old.matrix, phase: 0 });

  // 折線與區域：同一條線的形狀從原本的點內插到新的點；新出現的線淡入、消失的線淡出
  const series = seriesPaths(chart, keyOf);
  const present = new Set(series.map((s) => s.key));
  for (const line of series) {
    const old = before.paths.get(line.key);
    if (!old) {
      animations.push(line.element.animate([{ opacity: 0 }, { opacity: 1 }], { duration, delay: phase(2), easing, fill: "backwards" }));
      continue;
    }
    // 原本的點換成新的路徑自己的座標
    const local = new Map<string, number[]>();
    for (const [key, p] of old.points) {
      const a = fromChart(chart, line.element, p[0], p[1]);
      local.set(key, p.length === 4 ? [...a, ...fromChart(chart, line.element, p[2], p[3])] : a);
    }
    const pairs = pairPoints({ order: old.order, points: local }, line.points);
    if (pairs.length === 0) continue;
    const from = pathData(
      pairs.map((p) => p.from),
      line.area,
      line.interpolate,
    );
    const to = pathData(
      pairs.map((p) => p.to),
      line.area,
      line.interpolate,
    );
    if (from === to) continue;
    animations.push(line.element.animate([{ d: `path("${from}")` }, { d: `path("${to}")` }], { duration, delay: phase(1), easing, fill: "backwards" }));
  }
  for (const [key, old] of before.paths) if (!present.has(key)) ghosts.push({ copy: old.copy, matrix: old.matrix, phase: 0 });

  // 連續的座標軸：同一個值的刻度移到新的位置；新出現的從原本的比例尺上的位置移過來並淡入，消失的移到新的
  // 比例尺上的位置並淡出
  for (const axis of continuousAxes(chart)) {
    const old = before.axes.get(axis.key);
    if (!old || old.horizontal !== axis.horizontal) continue;
    const along = axis.horizontal ? 0 : 1;
    const oldScale: [number, number][] = [...old.ticks.values()].map((t) => [t.value, t.at[along]]);
    const nowAt = new Map(axis.ticks.map((t) => [t.key, toChart(chart, t.element, 0, 0)] as const));
    const newScale: [number, number][] = axis.ticks.map((t) => [t.value, nowAt.get(t.key)![along]]);
    for (const tick of axis.ticks) {
      const own = ownTranslate(tick.element);
      const parent = tick.element.parentElement;
      if (!own || !parent) continue;
      const at = nowAt.get(tick.key)!;
      const was = old.ticks.get(tick.key);
      let start: [number, number];
      if (was) start = was.at;
      else {
        const position = positionOf(tick.value, oldScale);
        if (position === null) {
          animations.push(tick.element.animate([{ opacity: 0 }, { opacity: 1 }], { duration, delay: phase(1), easing, fill: "backwards" }));
          continue;
        }
        start = axis.horizontal ? [position, at[1]] : [at[0], position];
      }
      if (Math.abs(start[0] - at[0]) < 0.5 && Math.abs(start[1] - at[1]) < 0.5 && was) continue;
      // 元素的原點放回原本的位置：換算成父群組的座標（元素自己的 transform 是父群組中的平移）
      const [ox, oy] = fromChart(chart, parent, start[0], start[1]);
      const frames: Keyframe[] = [
        { transform: `translate(${ox}px, ${oy}px)`, ...(was ? {} : { opacity: 0 }) },
        { transform: `translate(${own[0]}px, ${own[1]}px)`, ...(was ? {} : { opacity: 1 }) },
      ];
      animations.push(tick.element.animate(frames, { duration, delay: phase(1), easing, fill: "backwards" }));
    }
    for (const [key, was] of old.ticks) {
      if (nowAt.has(key)) continue;
      const position = positionOf(was.value, newScale);
      const move: [number, number] | undefined = position === null ? undefined : axis.horizontal ? [position - was.at[0], 0] : [0, position - was.at[1]];
      ghosts.push({ copy: was.copy, matrix: was.matrix, move, phase: 1 });
    }
  }

  // 消失的標記、線與刻度：原本的樣子疊在圖上淡出。標記與線一起淡出（第一段）；刻度另外一層，各自移到新的比例尺
  // 上的位置並淡出（第二段）
  const layers: SVGSVGElement[] = [];
  const layer = (kind: string): SVGSVGElement => {
    const ghost = document.createElementNS(SVG_NS, "svg");
    ghost.setAttribute("class", `mark-ghosts ${kind}`);
    ghost.setAttribute("aria-hidden", "true");
    const area = chart.getBoundingClientRect();
    const origin = svg.getBoundingClientRect();
    Object.assign(ghost.style, {
      position: "absolute",
      left: `${origin.left - area.left + chart.scrollLeft}px`,
      top: `${origin.top - area.top + chart.scrollTop}px`,
      width: `${origin.width}px`,
      height: `${origin.height}px`,
      // 與圖相同裁在圖的範圍內（移出比例尺的刻度不畫到圖外）
      overflow: "hidden",
      pointerEvents: "none",
    });
    layers.push(ghost);
    return ghost;
  };
  const place = (target: SVGSVGElement, { copy, matrix }: Ghost) => {
    const group = document.createElementNS(SVG_NS, "g");
    if (matrix) group.setAttribute("transform", `matrix(${matrix.a},${matrix.b},${matrix.c},${matrix.d},${matrix.e},${matrix.f})`);
    group.appendChild(copy);
    target.appendChild(group);
  };
  const fading = ghosts.filter((g) => g.phase === 0);
  const ticks = ghosts.filter((g) => g.phase === 1);
  const marksLayer = fading.length > 0 ? layer("fading") : null;
  const ticksLayer = ticks.length > 0 ? layer("ticks") : null;
  if (marksLayer) for (const g of fading) place(marksLayer, g);
  if (ticksLayer)
    for (const g of ticks) {
      place(ticksLayer, g);
      const { copy, matrix, move } = g;
      // 複本在群組（原本的變換）的座標中移動：換算成那個座標的距離
      const sx = matrix && matrix.a !== 0 ? matrix.a : 1;
      const sy = matrix && matrix.d !== 0 ? matrix.d : 1;
      const [dx, dy] = move ?? [0, 0];
      animations.push(
        copy.animate(
          [
            { transform: "translate(0px, 0px)", opacity: 1 },
            { transform: `translate(${dx / sx}px, ${dy / sy}px)`, opacity: 0 },
          ],
          { duration, delay: phase(1), easing, fill: "forwards" },
        ),
      );
    }
  if (layers.length > 0) {
    if (getComputedStyle(chart).position === "static") chart.style.position = "relative";
    for (const ghost of layers) chart.appendChild(ghost);
  }
  if (marksLayer) animations.push(marksLayer.animate([{ opacity: 1 }, { opacity: 0 }], { duration, delay: phase(0), easing, fill: "forwards" }));

  // 標記的標籤（文字的標記以外的文字圖層）：移動的時候不顯示，最後淡入
  svg.querySelectorAll("g.mark-text.role-mark").forEach((layer) => {
    const sample = layer.querySelector(":scope > text");
    const name = sample ? itemOf(sample)?.mark?.name ?? "" : "";
    if (!/label/i.test(name) && !/label/i.test(layer.getAttribute("class") ?? "")) return;
    animations.push(layer.animate([{ opacity: 0 }, { opacity: 0, offset: 0.6 }, { opacity: 1 }], { duration, delay: phase(sequential ? 2 : 0), easing: "linear", fill: "backwards" }));
  });

  if (animations.length === 0) {
    layers.forEach((ghost) => ghost.remove());
    return null;
  }
  chart.dataset.animating = "true";
  const cleanup = () => {
    layers.forEach((ghost) => ghost.remove());
    delete chart.dataset.animating;
  };
  const finished = Promise.all(animations.map((a) => a.finished.catch(() => undefined))).then(cleanup);
  return {
    cancel: () => {
      animations.forEach((a) => a.cancel());
      cleanup();
    },
    finished,
  };
}
