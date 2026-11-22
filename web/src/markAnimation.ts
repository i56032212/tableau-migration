// 標記的動畫（Tableau 2020.1 起的「格式＞動畫」）：篩選、排序、頁面、參數與動作改變檢視之後，標記從原本的位置
// 移到新的位置。Vega 沒有轉場：重畫之前記下每個標記（依它的維度值認得同一個標記）在畫面上的外框與顏色，新的圖
// 畫好之後，同一個標記先以 transform 放回原本的位置、再移到新的位置（FLIP）；新出現的標記淡入，消失的標記以
// 原本的樣子淡出。同時：三者一起；依序：先淡出、再移動、最後淡入（Tableau：「fades out marks, moves and sorts
// them, then fades them in」），每一段各占設定的持續時間（推定）。
// 與 Tableau 相同不做動畫的：地圖（形狀）、圓餅、文字標記、標題、參考線與趨勢線（Tableau 也不做）；折線與區域
// 本平台也不做（整條是一個路徑），標記的標籤在動畫結束前淡入。

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
  mark?: { name?: string; marktype?: string };
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

/** 重畫之前的標記：身分 → 位置與樣子 */
export type MarkSnapshot = Map<string, Snapshot>;

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

/** 重畫之前記下標記的位置與樣子。 */
export function snapshotMarks(chart: HTMLElement, keyOf: MarkKey): MarkSnapshot {
  const marks: MarkSnapshot = new Map();
  for (const [element, key] of movingMarks(chart, keyOf)) {
    const box = boxIn(chart, element);
    if (box.width === 0 && box.height === 0) continue;
    const copy = element.cloneNode(true) as Element;
    copy.removeAttribute("transform");
    marks.set(key, { box, fill: getComputedStyle(element).fill || null, copy, matrix: (element as SVGGraphicsElement).getCTM() });
  }
  return marks;
}

/** 元素本身的 transform 屬性只有平移時的 x、y；有其他變換時為 null（不移動它） */
function ownTranslate(element: Element): [number, number] | null {
  const value = (element.getAttribute("transform") ?? "").trim();
  if (value === "") return [0, 0];
  const found = /^translate\(\s*(-?[\d.e+-]+)(?:[\s,]+(-?[\d.e+-]+))?\s*\)$/.exec(value);
  return found ? [Number(found[1]), Number(found[2] ?? 0)] : null;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** 進行中的動畫：cancel 立刻停下（圖又要重畫時） */
export interface RunningAnimation {
  cancel: () => void;
  finished: Promise<void>;
}

/**
 * 新的圖畫好之後：與重畫之前的標記比對，移動、淡入、淡出。回傳進行中的動畫；沒有可以動的標記時為 null。
 * 進行中圖的區塊標著 data-animating。
 */
export function animateMarks(chart: HTMLElement, before: MarkSnapshot, keyOf: MarkKey, setting: MarkAnimation): RunningAnimation | null {
  const svg = mainSvg(chart);
  if (!svg || setting.duration <= 0 || before.size === 0 || !motionAllowed()) return null;
  const duration = setting.duration;
  const sequential = setting.style === "sequential";
  // 依序：淡出（0）、移動（1）、淡入（2）各占一段；同時：都從 0 開始
  const phase = (n: number) => (sequential ? n * duration : 0);
  const easing = "ease-in-out";
  const animations: Animation[] = [];
  const now = new Map(movingMarks(chart, keyOf).map(([element, key]) => [key, element] as const));

  for (const [key, element] of now) {
    const old = before.get(key);
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

  // 消失的標記：原本的樣子疊在圖上淡出
  const gone = [...before].filter(([key]) => !now.has(key));
  let ghost: SVGSVGElement | null = null;
  if (gone.length > 0) {
    ghost = document.createElementNS(SVG_NS, "svg");
    ghost.setAttribute("class", "mark-ghosts");
    ghost.setAttribute("aria-hidden", "true");
    const area = chart.getBoundingClientRect();
    const origin = svg.getBoundingClientRect();
    Object.assign(ghost.style, {
      position: "absolute",
      left: `${origin.left - area.left + chart.scrollLeft}px`,
      top: `${origin.top - area.top + chart.scrollTop}px`,
      width: `${origin.width}px`,
      height: `${origin.height}px`,
      overflow: "visible",
      pointerEvents: "none",
    });
    for (const [, old] of gone) {
      const group = document.createElementNS(SVG_NS, "g");
      if (old.matrix) {
        const m = old.matrix;
        group.setAttribute("transform", `matrix(${m.a},${m.b},${m.c},${m.d},${m.e},${m.f})`);
      }
      group.appendChild(old.copy);
      ghost.appendChild(group);
    }
    if (getComputedStyle(chart).position === "static") chart.style.position = "relative";
    chart.appendChild(ghost);
    animations.push(ghost.animate([{ opacity: 1 }, { opacity: 0 }], { duration, delay: phase(0), easing, fill: "forwards" }));
  }

  // 標記的標籤（文字的標記以外的文字圖層）：移動的時候不顯示，最後淡入
  svg.querySelectorAll("g.mark-text.role-mark").forEach((layer) => {
    const sample = layer.querySelector(":scope > text");
    const name = sample ? itemOf(sample)?.mark?.name ?? "" : "";
    if (!/label/i.test(name) && !/label/i.test(layer.getAttribute("class") ?? "")) return;
    animations.push(layer.animate([{ opacity: 0 }, { opacity: 0, offset: 0.6 }, { opacity: 1 }], { duration, delay: phase(sequential ? 2 : 0), easing: "linear", fill: "backwards" }));
  });

  if (animations.length === 0) {
    ghost?.remove();
    return null;
  }
  chart.dataset.animating = "true";
  const cleanup = () => {
    ghost?.remove();
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
