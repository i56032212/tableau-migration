// 在檢視上拖曳標題或圖例的項目排序（Tableau：「select a header in a viz or on a legend and drag it to the correct
// location」，放下的位置以一條粗黑線標示）：編輯器裡按住離散的列標題、欄標題或色彩（形狀）圖例的項目拖到另一個
// 位置，這個欄位的排序改成手動，順序是畫面上的順序、拖曳的成員移到放下的位置。
// 認得的標題：單一維度的座標軸標籤、多層的列標題與欄標題（每一層）、多層欄標題合併成一個標籤時的最內層，以及
// 類別的圖例；分格（facet）的標題與 Measure Names 不能拖曳。

import type { Scalar } from "./types";

/** 多層標題合併成一個值時的分隔字元（tmig.render.vegalite 的 SEPARATOR） */
const SEPARATOR = "｜";
/** 按下之後移動超過這麼多像素才算拖曳（只是點一下不改排序） */
const THRESHOLD = 4;

export interface HeaderMove {
  /** 欄位鍵（usermeta.dimensions 的值，例如 f_region、year(f_order_date)） */
  key: string;
  /** 拖曳的成員 */
  value: Scalar;
  /** 拖曳之後這個欄位的成員順序：畫面上的順序，拖曳的成員移到放下的位置 */
  values: Scalar[];
  where: "rows" | "columns" | "legend";
}

interface Header {
  element: Element;
  key: string;
  value: Scalar;
  where: HeaderMove["where"];
  /** 沿著哪個方向排列（列標題、直的圖例是 y） */
  axis: "x" | "y";
  /** 外層的成員（多層標題中只在同一組裡移動） */
  group: string;
}

type Records = Record<string, unknown>[];
type Spec = Record<string, any>;

/** 主要的編碼（圖層的第一層有 x、y 的那一層）。 */
function mainEncoding(spec: Spec): Record<string, any> {
  if (spec.encoding) return spec.encoding;
  const layer = (spec.layer ?? []).find((l: Spec) => l.encoding?.x || l.encoding?.y);
  return layer?.encoding ?? {};
}

/** 編碼的欄位（c0 或 c0_label）→ 維度的資料欄（c0）；不是維度時為 null。 */
function columnOf(field: unknown, dimensions: Record<string, string>): string | null {
  if (typeof field !== "string") return null;
  if (field in dimensions) return field;
  if (field.endsWith("_label") && field.slice(0, -6) in dimensions) return field.slice(0, -6);
  return null;
}

const shown = (record: Record<string, unknown>, column: string) => String(record[`${column}_label`] ?? record[column] ?? "");

/**
 * 多層標題（rows_dim、cols_dim 是各層的標籤以 SEPARATOR 串起來）的第 level 層是哪個資料欄：每一筆資料的第 level
 * 段都與它的標籤相同的那一欄。
 */
function levelColumn(records: Records, composite: string, level: number, dimensions: Record<string, string>): string | null {
  const rows = records.filter((r) => r && typeof r[composite] === "string");
  if (rows.length === 0) return null;
  return (
    Object.keys(dimensions).find((column) =>
      rows.every((r) => String(r[composite]).split(SEPARATOR)[level] === shown(r, column)),
    ) ?? null
  );
}

interface SceneItem {
  datum?: Record<string, unknown>;
  mark?: { name?: string; role?: string; group?: SceneItem & { mark?: { role?: string; group?: SceneItem } } };
}

const itemOf = (element: Element) => (element as unknown as { __data__?: SceneItem }).__data__;

/** 檢視上可以拖曳的標題與圖例項目。 */
export function headersOf(svg: SVGSVGElement, spec: Spec, records: Records, dimensions: Record<string, string>): Header[] {
  const encoding = mainEncoding(spec);
  const out: Header[] = [];
  const levels = new Map<string, string | null>();
  const levelOf = (composite: string, level: number) => {
    const id = `${composite}:${level}`;
    if (!levels.has(id)) levels.set(id, levelColumn(records, composite, level, dimensions));
    return levels.get(id) ?? null;
  };
  const rawOf = (column: string, label: unknown, composite?: string) => {
    const record = records.find((r) => r && (composite ? r[composite] === label : shown(r, column) === String(label) || r[column] === label));
    return record ? ((record[column] ?? null) as Scalar) : undefined;
  };

  // 座標軸的標籤：單一維度（c0_label），或多層合併成一個標籤（只拖曳最內層）
  svg.querySelectorAll("g.role-axis-label > text").forEach((element) => {
    const item = itemOf(element);
    const scale = item?.mark?.group?.datum?.scale;
    const value = item?.datum?.value;
    if ((scale !== "x" && scale !== "y") || value === undefined) return;
    const channel = encoding[scale];
    if (!channel || (channel.type !== "nominal" && channel.type !== "ordinal")) return;
    const where = scale === "y" ? "rows" : "columns";
    if (channel.field === "rows_dim" || channel.field === "cols_dim") {
      const parts = String(value).split(SEPARATOR);
      const column = levelOf(channel.field, parts.length - 1);
      const raw = column ? rawOf(column, value, channel.field) : undefined;
      if (column && raw !== undefined)
        out.push({ element, key: dimensions[column], value: raw, where, axis: scale, group: parts.slice(0, -1).join(SEPARATOR) });
      return;
    }
    const column = columnOf(channel.field, dimensions);
    const raw = column ? rawOf(column, value) : undefined;
    if (column && raw !== undefined) out.push({ element, key: dimensions[column], value: raw, where, axis: scale, group: "" });
  });

  // 多層的列標題、欄標題：每一層一個圖層（__deco__header0、__deco__colheader1…），項目的資料就是那一列資料
  svg.querySelectorAll("g.mark-text.role-mark").forEach((group) => {
    const found = /(?:^|\s)__deco__(header|colheader)(\d+)(?:_mid)?_marks(?:\s|$)/.exec(group.getAttribute("class") ?? "");
    if (!found) return;
    const composite = found[1] === "header" ? "rows_dim" : "cols_dim";
    const level = Number(found[2]);
    const column = levelOf(composite, level);
    if (!column) return;
    group.querySelectorAll(":scope > text").forEach((element) => {
      const datum = itemOf(element)?.datum;
      if (!datum || !element.textContent?.trim() || typeof datum[composite] !== "string") return;
      const parts = String(datum[composite]).split(SEPARATOR);
      out.push({
        element,
        key: dimensions[column],
        value: (datum[column] ?? null) as Scalar,
        where: composite === "rows_dim" ? "rows" : "columns",
        axis: composite === "rows_dim" ? "y" : "x",
        group: parts.slice(0, level).join(SEPARATOR),
      });
    });
  });

  // 類別的圖例（色彩、形狀）：項目的值是標籤；圖例的群組記著它的尺度（color、shape…）
  const legendItems: { element: Element; value: unknown; scale: string }[] = [];
  svg.querySelectorAll("g.role-legend-label > text, g.role-legend-symbol > path").forEach((element) => {
    const item = itemOf(element);
    let group = item?.mark?.group;
    while (group && group.mark?.role !== "legend") group = group.mark?.group;
    const scales = group?.datum?.scales as Record<string, string> | undefined;
    const scale = scales ? Object.values(scales)[0] : undefined;
    if (scale && item?.datum?.value !== undefined) legendItems.push({ element, value: item.datum.value, scale });
  });
  const legendBoxes = legendItems.map((l) => l.element.getBoundingClientRect());
  const spreadX = legendBoxes.length ? Math.max(...legendBoxes.map((b) => b.left)) - Math.min(...legendBoxes.map((b) => b.left)) : 0;
  const spreadY = legendBoxes.length ? Math.max(...legendBoxes.map((b) => b.top)) - Math.min(...legendBoxes.map((b) => b.top)) : 0;
  legendItems.forEach(({ element, value, scale }) => {
    const column = columnOf(encoding[scale]?.field, dimensions);
    const raw = column ? rawOf(column, value) : undefined;
    if (column && raw !== undefined)
      out.push({ element, key: dimensions[column], value: raw, where: "legend", axis: spreadX > spreadY ? "x" : "y", group: scale });
  });
  return out;
}

const center = (box: DOMRect, axis: "x" | "y") => (axis === "y" ? (box.top + box.bottom) / 2 : (box.left + box.right) / 2);

/** 放下的位置：同一組裡離指標最近的成員，放在它之前或之後。 */
function dropAt(dragged: Header, headers: Header[], x: number, y: number): { anchor: Header; after: boolean } | null {
  const point = dragged.axis === "y" ? y : x;
  let best: { anchor: Header; distance: number } | null = null;
  for (const header of headers) {
    if (header.key !== dragged.key || header.where !== dragged.where || header.group !== dragged.group) continue;
    const distance = Math.abs(center(header.element.getBoundingClientRect(), dragged.axis) - point);
    if (!best || distance < best.distance) best = { anchor: header, distance };
  }
  if (!best) return null;
  return { anchor: best.anchor, after: point > center(best.anchor.element.getBoundingClientRect(), dragged.axis) };
}

/** 畫面上這個欄位的成員順序（同一個成員在多組裡出現時以第一次出現的位置為準）。 */
function shownOrder(dragged: Header, headers: Header[]): Scalar[] {
  const same = headers.filter((h) => h.key === dragged.key && h.where === dragged.where);
  const positioned = same.map((h) => ({ value: h.value, at: center(h.element.getBoundingClientRect(), dragged.axis) }));
  positioned.sort((a, b) => a.at - b.at);
  const order: Scalar[] = [];
  for (const { value } of positioned) if (!order.some((v) => v === value)) order.push(value);
  return order;
}

/**
 * 指標下的標題。捲動時固定的標題（WorksheetView 的 freezeHeaders：原圖的複本疊在原圖上）蓋住的地方，以複本中
 * 對應的元素判斷（複本與原圖的元素順序相同）；複本的欄標題與原圖左右對齊、列標題上下對齊，排列的位置照樣以
 * 原圖計算。
 */
function headerAt(chart: HTMLElement, svg: SVGSVGElement, headers: Header[], x: number, y: number): Header | null {
  const within = (box: DOMRect, slack = 2) => x >= box.left - slack && x <= box.right + slack && y >= box.top - slack && y <= box.bottom + slack;
  const layers = Array.from(chart.querySelectorAll<HTMLElement>(".frozen"))
    .filter((layer) => within(layer.getBoundingClientRect(), 0))
    .sort((a, b) => Number(b.style.zIndex || 0) - Number(a.style.zIndex || 0));
  if (layers.length > 0) {
    const copy = layers[0].querySelector("svg");
    if (!copy) return null;
    const index = new Map<Element, number>();
    Array.from(svg.querySelectorAll("*")).forEach((element, i) => index.set(element, i));
    const copies = Array.from(copy.querySelectorAll("*"));
    return headers.find((h) => {
      const twin = copies[index.get(h.element) ?? -1];
      return twin !== undefined && within(twin.getBoundingClientRect());
    }) ?? null;
  }
  return headers.find((h) => within(h.element.getBoundingClientRect())) ?? null;
}

/**
 * 在圖上按下滑鼠時：按在可以拖曳的標題上就開始拖曳（回傳 true，呼叫端不再框選）。拖曳時以一條粗黑線標示放下的
 * 位置，放開時回報新的順序；只是點一下（沒有移動）不回報。chart 是圖的區塊（.chart，可能在裡面捲動）。
 */
export function startHeaderDrag(
  event: MouseEvent,
  chart: HTMLElement,
  svg: SVGSVGElement,
  spec: Spec,
  records: Records,
  dimensions: Record<string, string>,
  onMove: (move: HeaderMove) => void,
  onEnd: (dragged: boolean) => void,
): boolean {
  const headers = headersOf(svg, spec, records, dimensions);
  const hit = headers.length > 0 ? headerAt(chart, svg, headers, event.clientX, event.clientY) : null;
  if (!hit) return false;
  event.preventDefault();
  const startX = event.clientX;
  const startY = event.clientY;
  let dragging = false;
  let line: HTMLDivElement | null = null;
  let target: { anchor: Header; after: boolean } | null = null;
  // 線畫在圖的區塊裡看得到的範圍內（捲動時原圖比區塊大）
  const svgBox = svg.getBoundingClientRect();
  const view = chart.getBoundingClientRect();
  const frame = {
    left: Math.max(svgBox.left, view.left),
    right: Math.min(svgBox.right, view.left + chart.clientWidth),
    top: Math.max(svgBox.top, view.top),
    bottom: Math.min(svgBox.bottom, view.top + chart.clientHeight),
  };
  const siblings = headers.filter((h) => h.key === hit.key && h.where === hit.where && h.group === hit.group);
  const spanOf = () => {
    // 線的長度：列標題橫跨整張圖、欄標題縱貫整張圖，圖例只橫跨圖例的項目
    if (hit.where !== "legend") return hit.axis === "y" ? [frame.left, frame.right] : [frame.top, frame.bottom];
    const boxes = headers.filter((h) => h.where === "legend" && h.group === hit.group).map((h) => h.element.getBoundingClientRect());
    return hit.axis === "y" ? [Math.min(...boxes.map((b) => b.left)), Math.max(...boxes.map((b) => b.right))] : [Math.min(...boxes.map((b) => b.top)), Math.max(...boxes.map((b) => b.bottom))];
  };
  const move = (e: MouseEvent) => {
    if (!dragging && Math.abs(e.clientX - startX) + Math.abs(e.clientY - startY) < THRESHOLD) return;
    if (!dragging) {
      dragging = true;
      line = document.createElement("div");
      line.className = "header-drop-line";
      line.setAttribute("aria-hidden", "true");
      document.body.appendChild(line);
      document.body.classList.add("dragging-header");
    }
    target = dropAt(hit, siblings, e.clientX, e.clientY);
    if (!line || !target) return;
    const box = target.anchor.element.getBoundingClientRect();
    const [from, to] = spanOf();
    if (hit.axis === "y") {
      const at = target.after ? box.bottom + 1 : box.top - 1;
      Object.assign(line.style, { left: `${from}px`, top: `${at - 1}px`, width: `${to - from}px`, height: "3px" });
    } else {
      const at = target.after ? box.right + 1 : box.left - 1;
      Object.assign(line.style, { left: `${at - 1}px`, top: `${from}px`, width: "3px", height: `${to - from}px` });
    }
  };
  const up = (e: MouseEvent) => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    line?.remove();
    document.body.classList.remove("dragging-header");
    if (dragging) target = dropAt(hit, siblings, e.clientX, e.clientY);
    onEnd(dragging);
    if (!dragging || !target || target.anchor.value === hit.value) return;
    const order = shownOrder(hit, headers).filter((v) => v !== hit.value);
    const at = order.findIndex((v) => v === target!.anchor.value);
    if (at < 0) return;
    order.splice(target.after ? at + 1 : at, 0, hit.value);
    onMove({ key: hit.key, value: hit.value, values: order, where: hit.where });
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
  return true;
}
