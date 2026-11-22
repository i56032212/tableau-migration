// 列印與 PDF 的分頁（Tableau Desktop 的「檔案＞列印」：比一頁大的工作表分成多頁印）。單獨檢視的工作表依頁面設定的
// 紙張與縮放切成多頁：「在每頁中重複標題和圖例」時每一頁都有欄標題（上方）、列標題（左側）、座標軸與圖例，「在窗格
// 邊界分頁」時分頁落在列與列、欄與欄之間（不切過標記與文字），頁的順序是先往下、再往右。
// 圖是 Vega 畫好的整張 SVG——在區塊內捲動的工作表（標準大小放不下、整個檢視時列高保持）SVG 也是整張的，只是區塊裡
// 看得到一段——每一頁以這張 SVG 的複本裁出需要的區域（只複製落在區域裡的標記）。分好的頁放在 <body> 最後，畫面上
// 不顯示，列印時取代整個畫面（styles.css 的 .print-pages）。
// 某一頁從多層列標題的一組中間開始時（外層的名稱只寫在那一組的第一列），那一頁的第一列也寫上外層的名稱（推定：
// Tableau 的說明只寫「adds table row and column headers at the top of each printed page」）。
// 頁面架「所有頁面」時各頁依序接在後面（append，PageSetup.tsx 一頁一頁切換、等圖畫好之後分頁）。

/** 分頁時的設定：頁面設定加上每頁可以印的大小。 */
export interface Paging {
  /** 每頁可以印的寬、高（CSS 像素，扣掉邊界） */
  pageWidth: number;
  pageHeight: number;
  /** 縮放：auto 自動；page 符合一頁；width 符合頁寬；percent 自訂的比例；fit 適合於橫向 across、縱向 down 頁 */
  scaling: "auto" | "page" | "width" | "percent" | "fit";
  percent: number;
  across: number;
  down: number;
  title: boolean;
  caption: boolean;
  legends: boolean;
  /** 在每頁中重複標題和圖例 */
  repeat: boolean;
  /** 在窗格邊界分頁 */
  breakPanes: boolean;
}

const CONTAINER_ID = "tmig-print-pages";
/** 畫出東西的 SVG 元素：依外框決定放進哪幾頁 */
const LEAVES = new Set(["text", "path", "line", "rect", "circle", "ellipse", "polygon", "polyline", "image", "use", "foreignObject"]);
/** 整個複製的定義（剪裁路徑、漸層…）：每一頁都要有 */
const WHOLE = new Set(["defs", "clipPath", "linearGradient", "radialGradient", "pattern", "mask", "filter", "marker", "style", "title", "desc"]);
/** 「在窗格邊界分頁」：分頁的位置最多往回退一頁的這個比例（再退就直接切開） */
const BACKOFF = 0.4;

/** SVG 座標中的外框：左、上、右、下 */
type Box = [number, number, number, number];
type Range = [number, number];

/** 分好的頁是哪一張圖（SVG 元素與它的大小）分的：圖重畫之後（篩選、選取…）要重新分。held：頁面架的所有頁面
 * 分好之後圖已經換回目前的頁，印完（afterprint）之前不重新分 */
let builtFor: { sheet: Element; svg: Element; size: string; held?: boolean } | null = null;

/** 拿掉分好的頁（列印結束、或改印別的東西時）。 */
export function clearPages(): void {
  document.getElementById(CONTAINER_ID)?.remove();
  document.documentElement.classList.remove("tmig-paged");
  builtFor = null;
}

function chartSvg(sheet: Element): SVGSVGElement | null {
  return sheet.querySelector<SVGSVGElement>(".worksheet > .worksheet-body > .chart > svg, .worksheet > .worksheet-body > .chart > .frozen-host > svg");
}

/**
 * 這個區塊已經分好頁，而且圖在那之後沒有重畫（頁面設定的「列印」剛分好的，瀏覽器的 beforeprint 不用再分一次）。
 */
export function alreadyPaged(target: Element | null): boolean {
  if (!target || !builtFor || builtFor.sheet !== target || !document.getElementById(CONTAINER_ID)) return false;
  if (builtFor.held) return true;
  const svg = chartSvg(target);
  return svg === builtFor.svg && `${svg.getAttribute("width")}x${svg.getAttribute("height")}` === builtFor.size;
}

function plotBox(chart: HTMLElement, width: number, height: number): Box {
  // 資料區（座標軸與標題以外）：WorksheetView 畫好時記在 data-plot（左、上、寬、高）
  const [x, y, w, h] = (chart.dataset.plot ?? "").split(",").map(Number);
  if ([x, y, w, h].every(Number.isFinite) && w > 0 && h > 0 && x >= 0 && y >= 0 && x + w <= width + 1 && y + h <= height + 1)
    return [x, y, Math.min(width, x + w), Math.min(height, y + h)];
  return [0, 0, width, height];
}

/** 每個畫出東西的元素在 SVG 座標中的外框（看不到、沒有大小的不列）。 */
function leafBoxes(svg: SVGSVGElement, width: number, height: number): Map<Element, Box> {
  const origin = svg.getBoundingClientRect();
  const sx = origin.width > 0 ? width / origin.width : 1;
  const sy = origin.height > 0 ? height / origin.height : 1;
  const boxes = new Map<Element, Box>();
  svg.querySelectorAll("*").forEach((element) => {
    if (!LEAVES.has(element.localName) || element.closest("defs")) return;
    const r = element.getBoundingClientRect();
    if (r.width <= 0 && r.height <= 0) return;
    boxes.set(element, [(r.left - origin.left) * sx, (r.top - origin.top) * sy, (r.right - origin.left) * sx, (r.bottom - origin.top) * sy]);
  });
  return boxes;
}

/** 決定分頁位置時要避開的元素的外框：圖層的底色、格線、座標軸線與圖例不算（它們不是窗格裡的東西）。 */
function breakBoxes(boxes: Map<Element, Box>): Box[] {
  const out: Box[] = [];
  boxes.forEach((box, element) => {
    if (element.classList.contains("background") || element.closest(".role-axis-grid, .role-axis-domain, .role-legend")) return;
    out.push(box);
  });
  return out;
}

/**
 * 不能分頁的區間（標記、文字在 axis 方向上占的範圍，重疊的合併）：比 limit 長的（整條的折線、跨過整張圖的
 * 分隔線）不算——它們無論如何都會被切開。
 */
function blockedRanges(boxes: Iterable<Box>, axis: 0 | 1, limit: number): Range[] {
  const spans: Range[] = [];
  for (const box of boxes) {
    const low = box[axis];
    const high = box[axis + 2];
    if (high - low > 0.5 && high - low < limit) spans.push([low, high]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  const merged: Range[] = [];
  for (const span of spans) {
    const last = merged[merged.length - 1];
    if (last && span[0] < last[1] - 0.5) last[1] = Math.max(last[1], span[1]);
    else merged.push([span[0], span[1]]);
  }
  return merged;
}

/**
 * 把 [start, end] 切成一段段，每段不超過 capacity(第幾段)。breakPanes 時分頁的位置落在不能分頁的區間之外
 * （往回退，最多退到這一段的 BACKOFF）。
 */
function slices(start: number, end: number, capacity: (index: number) => number, blocked: Range[] | null): Range[] {
  const out: Range[] = [];
  let from = start;
  for (let index = 0; from < end - 0.5 && index < 10_000; index++) {
    const room = Math.max(capacity(index), 24);
    let to = from + room;
    if (to >= end - 1) {
      out.push([from, end]);
      break;
    }
    const at = blocked ? blocked.findIndex((r) => r[0] < to - 0.5 && r[1] > to + 0.5) : -1;
    if (blocked && at >= 0 && blocked[at][0] > from + room * (1 - BACKOFF)) {
      // 退到前一個標記（文字）與這一個之間的空隙的中間：兩邊的字都不會被切到一點點
      const gap = Math.max(from, at > 0 ? blocked[at - 1][1] : from);
      to = gap < blocked[at][0] ? (gap + blocked[at][0]) / 2 : blocked[at][0];
    }
    out.push([from, to]);
    from = to;
  }
  return out;
}

/** 元素在不在區域裡：外框與區域相交（碰到邊而已的不算）。 */
function overlaps(box: Box, region: Box): boolean {
  return box[2] > region[0] + 0.5 && box[0] < region[2] - 0.5 && box[3] > region[1] + 0.5 && box[1] < region[3] - 0.5;
}

function inside(box: Box, region: Box): boolean {
  return box[0] >= region[0] - 0.5 && box[2] <= region[2] + 0.5 && box[1] >= region[1] - 0.5 && box[3] <= region[3] + 0.5;
}

function centered(box: Box, region: Box): boolean {
  const cx = (box[0] + box[2]) / 2;
  const cy = (box[1] + box[3]) / 2;
  return cx >= region[0] && cx <= region[2] && cy >= region[1] && cy <= region[3];
}

/** 另外複製、移到別處的元素（某一頁從一組中間開始時外層的列標題）：元素 → 移動的距離（x、y） */
type Carried = Map<Element, [number, number]>;

/** 這個區域裡的 SVG 內容：只複製 keep 留下的元素，群組照原樣（變換、剪裁）；carried 的元素移動之後也放進來。 */
function cloneWithin(source: Element, boxes: Map<Element, Box>, keep: (box: Box) => boolean, carried?: Carried): Element | null {
  const name = source.localName;
  if (WHOLE.has(name)) return source.cloneNode(true) as Element;
  if (LEAVES.has(name)) {
    const shift = carried?.get(source);
    if (shift) {
      const moved = document.createElementNS(SVG_NS, "g");
      moved.setAttribute("transform", `translate(${shift[0]},${shift[1]})`);
      moved.setAttribute("data-carried", "true");
      moved.appendChild(source.cloneNode(true));
      return moved;
    }
    const box = boxes.get(source);
    return box && keep(box) ? (source.cloneNode(true) as Element) : null;
  }
  const copy = source.cloneNode(false) as Element;
  for (const child of Array.from(source.children)) {
    const inner = cloneWithin(child, boxes, keep, carried);
    if (inner) copy.appendChild(inner);
  }
  return copy;
}

/** 多層標題合併成一個值時的分隔字元（tmig.render.vegalite 的 SEPARATOR） */
const SEPARATOR = "｜";

/**
 * 多層列標題的外層（__deco__header0、1…，最內層以外）：每一列都有一個文字元素，名稱只寫在每一組的第一列。回傳
 * 一個函式：資料區的一段（SVG 座標 top–bottom，一頁的列）從某一組的中間開始、那一組的名稱不在這一段裡時，名稱
 * 複製一份移到這一段的第一列（與那一列同一層的空白文字元素同一個位置）。
 */
function carriedRowHeaders(svg: SVGSVGElement): (top: number, bottom: number) => Carried {
  type Entry = { element: Element; x: number; y: number; group: string; named: boolean };
  const levels = new Map<number, Entry[]>();
  svg.querySelectorAll("g.mark-text.role-mark").forEach((layer) => {
    const found = /(?:^|\s)__deco__header(\d+)(?:_mid)?_marks(?:\s|$)/.exec(layer.getAttribute("class") ?? "");
    if (!found) return;
    const level = Number(found[1]);
    const entries = levels.get(level) ?? [];
    layer.querySelectorAll(":scope > text").forEach((element) => {
      const datum = (element as unknown as { __data__?: { datum?: Record<string, unknown> } }).__data__?.datum;
      // 文字的定位點（含它自己的 transform）在 SVG 座標中的位置
      const matrix = (element as SVGGraphicsElement).getCTM();
      if (!matrix || typeof datum?.rows_dim !== "string") return;
      const group = datum.rows_dim.split(SEPARATOR).slice(0, level + 1).join(SEPARATOR);
      entries.push({ element, x: matrix.e, y: matrix.f, group, named: Boolean(element.textContent?.trim()) });
    });
    levels.set(level, entries);
  });
  const innermost = Math.max(-1, ...levels.keys());
  return (top, bottom) => {
    const carried: Carried = new Map();
    levels.forEach((entries, level) => {
      if (level === innermost) return;
      const within = (entry: Entry) => entry.y >= top && entry.y < bottom;
      let first: Entry | null = null;
      for (const entry of entries) if (within(entry) && (first === null || entry.y < first.y)) first = entry;
      if (!first || entries.some((e) => e.group === first.group && e.named && within(e))) return;
      const label = entries.find((e) => e.group === first.group && e.named);
      if (label) carried.set(label.element, [first.x - label.x, first.y - label.y]);
    });
    return carried;
  };
}

/** 複本裡的 id（剪裁路徑、漸層）加上前置，參照跟著改：各頁的複本、畫面上的原圖互不相干。 */
function isolateIds(root: Element, prefix: string): void {
  const ids = new Map<string, string>();
  root.querySelectorAll("[id]").forEach((element) => {
    ids.set(element.id, prefix + element.id);
    element.id = prefix + element.id;
  });
  if (ids.size === 0) return;
  const rename = (value: string) => value.replace(/url\(\s*["']?#([^"')\s]+)["']?\s*\)/g, (whole, id: string) => (ids.has(id) ? `url(#${ids.get(id)})` : whole));
  root.querySelectorAll("*").forEach((element) => {
    for (const attr of Array.from(element.attributes)) {
      if (attr.value.includes("url(")) element.setAttribute(attr.name, rename(attr.value));
      else if (attr.localName === "href" && attr.value.startsWith("#") && ids.has(attr.value.slice(1)))
        element.setAttributeNS(attr.namespaceURI, attr.name, `#${ids.get(attr.value.slice(1))}`);
    }
  });
}

const SVG_NS = "http://www.w3.org/2000/svg";
let pieceCount = 0;

/**
 * SVG 的一塊區域（region，SVG 座標）放在頁上的 (left, top)。keep：要複製哪些元素（預設是與區域相交的）。
 */
function piece(
  svg: SVGSVGElement,
  region: Box,
  boxes: Map<Element, Box>,
  left: number,
  top: number,
  keep: (box: Box) => boolean = (box) => overlaps(box, region),
  carried?: Carried,
): SVGSVGElement {
  const out = document.createElementNS(SVG_NS, "svg");
  for (const attr of Array.from(svg.attributes)) {
    if (["width", "height", "viewBox", "id", "style"].includes(attr.name) || attr.name.startsWith("aria-") || attr.name === "role") continue;
    out.setAttribute(attr.name, attr.value);
  }
  const width = region[2] - region[0];
  const height = region[3] - region[1];
  out.setAttribute("width", String(width));
  out.setAttribute("height", String(height));
  out.setAttribute("viewBox", `${region[0]} ${region[1]} ${width} ${height}`);
  out.setAttribute("overflow", "hidden");
  out.setAttribute("aria-hidden", "true");
  // 這一塊是原圖的哪個區域（SVG 座標：左、上、右、下）
  out.dataset.region = region.map((n) => Math.round(n * 10) / 10).join(",");
  out.setAttribute("style", `position:absolute;left:${left}px;top:${top}px;display:block;overflow:hidden;font-family:${getComputedStyle(svg).fontFamily}`);
  for (const child of Array.from(svg.children)) {
    const inner = cloneWithin(child, boxes, keep, carried);
    if (inner) out.appendChild(inner);
  }
  isolateIds(out, `tmigp${++pieceCount}-`);
  return out;
}

/** 縮放的比例：content 是要印的內容（標題、圖、說明）的寬高，fills 是圖在這個方向填滿區塊（不是固定的格子大小）。 */
function scaleOf(paging: Paging, content: [number, number], fills: [boolean, boolean], bands: [number, number]): number {
  const byWidth = paging.pageWidth / Math.max(content[0], 1);
  const byHeight = paging.pageHeight / Math.max(content[1], 1);
  switch (paging.scaling) {
    case "percent":
      return Math.max(0.1, Math.min(4, paging.percent / 100));
    case "page":
      return Math.max(0.05, Math.min(byWidth, byHeight));
    case "width":
      return Math.max(0.05, byWidth);
    case "fit": {
      // 適合於橫向 across、縱向 down 頁：重複的標題每多一頁多占一次（只縮小，不放大）
      const across = Math.max(1, Math.round(paging.across));
      const down = Math.max(1, Math.round(paging.down));
      const wide = (across * paging.pageWidth) / Math.max(content[0] + (across - 1) * bands[0], 1);
      const tall = (down * paging.pageHeight) / Math.max(content[1] + (down - 1) * bands[1], 1);
      return Math.max(0.05, Math.min(1, wide, tall));
    }
    default:
      // 自動：填滿區塊的方向（大小跟著視窗走）縮到紙張之內，固定大小的方向（標準大小的列高、欄寬）照原尺寸、分頁印
      return Math.max(0.05, Math.min(1, fills[0] ? byWidth : 1, fills[1] ? byHeight : 1));
  }
}

/**
 * 單獨檢視的工作表（.single-sheet）分頁：建好列印用的頁、回傳縮放的比例與頁數；沒有畫好的圖（資料表、查詢失敗）
 * 時回傳 null（照原本的方式整個區塊縮放後列印）。append：接在已經分好的頁後面（頁面架的所有頁面，一頁一頁分），
 * hold：分好之後圖會換回目前的頁，印完之前不重新分。
 */
export function paginateSheet(
  sheet: HTMLElement,
  paging: Paging,
  options: { append?: boolean; hold?: boolean } = {},
): { scale: number; pages: number } | null {
  const previous = options.append ? document.getElementById(CONTAINER_ID) : null;
  if (!previous) clearPages();
  const worksheet = sheet.querySelector<HTMLElement>(".worksheet");
  const chart = worksheet?.querySelector<HTMLElement>(":scope > .worksheet-body > .chart");
  const svg = chartSvg(sheet);
  // 還沒畫好（查詢中、重畫到一半）的圖不分頁
  if (!worksheet || !chart || !svg || chart.dataset.drawn === "false") return null;
  const width = Number(svg.getAttribute("width")) || svg.getBoundingClientRect().width;
  const height = Number(svg.getAttribute("height")) || svg.getBoundingClientRect().height;
  if (!(width > 0 && height > 0)) return null;
  const plot = plotBox(chart, width, height);
  const [x0, y0, x1, y1] = plot;
  const boxes = leafBoxes(svg, width, height);
  const avoid = paging.breakPanes ? breakBoxes(boxes) : [];
  const carryRows = carriedRowHeaders(svg);

  // 標題（第一頁的最上面）、說明（最後一頁的最下面）、圖片形狀的圖例（圖的右邊）
  const header = paging.title ? worksheet.querySelector<HTMLElement>(":scope > .worksheet-header") : null;
  const caption = paging.caption ? worksheet.querySelector<HTMLElement>(":scope > .worksheet-caption") : null;
  const legend = paging.legends ? worksheet.querySelector<HTMLElement>(":scope > .worksheet-body > .shape-legend") : null;
  const titleHeight = header ? header.offsetHeight : 0;
  const captionHeight = caption ? caption.offsetHeight + 4 : 0;
  const legendWidth = legend ? legend.offsetWidth : 0;

  // 縮放：內容至少與畫面上的區塊一樣大（比區塊小的圖照原本的方式縮放，不會被放大好幾倍）
  const scroll = chart.dataset.scroll ?? "false";
  const fills: [boolean, boolean] = [scroll !== "x" && scroll !== "both", scroll !== "y" && scroll !== "both"];
  const content: [number, number] = [
    Math.max(width + legendWidth, worksheet.offsetWidth),
    Math.max(titleHeight + height + captionHeight, worksheet.offsetHeight),
  ];
  const left = x0;
  const right = width - x1 + legendWidth;
  const top = y0;
  const bottom = height - y1;
  let scale = scaleOf(paging, content, fills, paging.repeat ? [left + right, top + bottom] : [0, 0]);

  let columns: Range[] = [];
  let rows: Range[] = [];
  let repeatX = false;
  let repeatY = false;
  for (let attempt = 0; attempt < 30; attempt++) {
    const roomX = paging.pageWidth / scale - 1;
    const roomY = paging.pageHeight / scale - 1;
    // 重複的標題占掉大半頁時不重複（剩下的地方放不了幾欄、幾列）
    repeatX = paging.repeat && left + right < roomX * 0.6;
    repeatY = paging.repeat && top + bottom < (roomY - titleHeight) * 0.6;
    const blockedX = paging.breakPanes ? blockedRanges(avoid, 0, roomX) : null;
    const blockedY = paging.breakPanes ? blockedRanges(avoid, 1, roomY) : null;
    columns = repeatX
      ? slices(x0, x1, () => roomX - left - right, blockedX)
      : slices(0, width, () => roomX - legendWidth, blockedX);
    rows = repeatY
      ? slices(y0, y1, (i) => roomY - top - bottom - (i === 0 ? titleHeight : 0), blockedY)
      : slices(0, height, (i) => roomY - (i === 0 ? titleHeight : 0), blockedY);
    // 「適合於」「符合一頁」：分頁退到窗格邊界之後多出來的頁，再縮小一點
    const limitX = paging.scaling === "fit" ? Math.max(1, Math.round(paging.across)) : paging.scaling === "page" ? 1 : Infinity;
    const limitY = paging.scaling === "fit" ? Math.max(1, Math.round(paging.down)) : paging.scaling === "page" ? 1 : Infinity;
    if (columns.length <= limitX && rows.length <= limitY) break;
    scale *= 0.97;
  }

  const container = document.createElement("div");
  container.id = CONTAINER_ID;
  container.className = "print-pages";
  container.setAttribute("aria-hidden", "true");
  const background = getComputedStyle(worksheet).backgroundColor;
  const pages: HTMLElement[] = [];
  const newPage = () => {
    const page = document.createElement("section");
    page.className = "print-page";
    page.style.width = `${paging.pageWidth}px`;
    page.style.height = `${paging.pageHeight}px`;
    const body = document.createElement("div");
    body.className = "print-sheet";
    Object.assign(body.style, {
      zoom: scale.toFixed(4),
      width: `${paging.pageWidth / scale}px`,
      height: `${paging.pageHeight / scale}px`,
      background: background && background !== "rgba(0, 0, 0, 0)" ? background : "",
    });
    page.appendChild(body);
    pages.push(page);
    return body;
  };
  const roomY = paging.pageHeight / scale - 1;
  let usedOnLast = 0;
  // 先往下、再往右
  columns.forEach(([xs, xe], j) => {
    rows.forEach(([ys, ye], i) => {
      const body = newPage();
      let y = 0;
      if (i === 0 && j === 0 && header) {
        const copy = header.cloneNode(true) as HTMLElement;
        copy.querySelectorAll(".worksheet-meta").forEach((e) => e.remove());
        body.appendChild(copy);
        y = titleHeight;
      }
      // 重複的標題（列標題、欄標題、座標軸）與這一頁的資料區拼起來
      const slice: Box = [repeatX ? xs : Math.max(xs, x0), repeatY ? ys : Math.max(ys, y0), repeatX ? xe : Math.min(xe, x1), repeatY ? ye : Math.min(ye, y1)];
      const xParts: [number, number, boolean][] = repeatX ? [[0, x0, true], [xs, xe, false]] : [[xs, xe, false]];
      const yParts: [number, number, boolean][] = repeatY
        ? [[0, y0, true], [ys, ye, false], [y1, height, true]]
        : [[ys, ye, false]];
      let x = 0;
      for (const [ax, bx, bandX] of xParts) {
        if (bx - ax < 0.5) continue;
        let yy = y;
        for (const [ay, by, bandY] of yParts) {
          if (by - ay < 0.5) continue;
          const region: Box = [ax, ay, bx, by];
          // 重複的標題那幾塊：資料區裡、但不在這一頁的標記（例如最後一列的長條跨進座標軸一點點）不要
          const keep = bandX || bandY ? (box: Box) => overlaps(box, region) && (!centered(box, plot) || centered(box, slice)) : undefined;
          // 有列標題、從資料區中間開始的那一段：從一組的中間開始時，外層的名稱寫在這一頁的第一列
          const carried = !bandY && ax < x0 - 0.5 && ay > y0 + 0.5 ? carryRows(ay, by) : undefined;
          body.appendChild(piece(svg, region, boxes, x, yy, keep, carried));
          yy += by - ay;
        }
        x += bx - ax;
      }
      const viewHeight = yParts.reduce((sum, [a, b]) => sum + Math.max(0, b - a), 0);
      if (repeatX && width - x1 > 0.5) {
        // 圖例（Vega 畫在圖的右邊、靠上）：每一頁都從最上面開始
        const region: Box = [x1, 0, width, Math.min(height, Math.max(viewHeight, y1 - y0))];
        body.appendChild(piece(svg, region, boxes, x, y, (box) => inside(box, region)));
        x += width - x1;
      }
      if (legend && (repeatX || j === 0) && (repeatY || i === 0)) {
        const copy = legend.cloneNode(true) as HTMLElement;
        Object.assign(copy.style, { position: "absolute", left: `${x}px`, top: `${y}px`, width: `${legendWidth}px` });
        body.appendChild(copy);
      }
      usedOnLast = y + viewHeight;
    });
  });
  if (caption) {
    // 說明接在最後一頁的圖下面；放不下時另起一頁
    let body = pages[pages.length - 1].firstElementChild as HTMLElement;
    let y = usedOnLast;
    if (y + captionHeight > roomY + 1) {
      body = newPage();
      y = 0;
    }
    const copy = caption.cloneNode(true) as HTMLElement;
    Object.assign(copy.style, { position: "absolute", left: "0", top: `${y + 4}px`, width: `${Math.min(content[0], paging.pageWidth / scale)}px` });
    body.appendChild(copy);
  }
  const target = previous ?? container;
  const before = target.children.length;
  pages.forEach((page, i) => {
    page.dataset.page = String(before + i + 1);
    target.appendChild(page);
  });
  if (!previous) document.body.appendChild(container);
  document.documentElement.classList.add("tmig-paged");
  builtFor = { sheet, svg, size: `${svg.getAttribute("width")}x${svg.getAttribute("height")}`, held: options.hold };
  return { scale, pages: pages.length };
}
