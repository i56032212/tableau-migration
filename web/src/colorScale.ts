/**
 * 圖例的顏色：依工作表規格中的色彩比例尺（後端 tmig.render.colors 產生）算出各值的顏色。
 *
 * - 離散：scale.domain 為顯示值、scale.range 為對應的顏色
 * - 連續：domain 與 range 一一對應的分段線性（RGB 內插，超出範圍用兩端的顏色），
 *   或 type 為 quantize 的分段色彩（範圍等分成 range 的段數）
 */

export type ColorScale = {
  type?: string;
  domain?: unknown[];
  range?: string[];
};

/** Tableau 10（沒有色彩比例尺時的預設，與 Tableau 的「自動」相同）。 */
export const TABLEAU10 = [
  "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
  "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
];

/** 離散值的顏色：比例尺中有對應的值時用它，否則依出現順序取 Tableau 10。 */
export function discreteColor(scale: ColorScale | undefined, value: string, index: number): string {
  const domain = scale?.domain;
  const range = scale?.range;
  if (domain && range) {
    const at = domain.findIndex((d) => String(d) === value);
    if (at >= 0 && at < range.length) return range[at];
  }
  return TABLEAU10[index % TABLEAU10.length];
}

function rgb(color: string): [number, number, number] {
  const hex = color.replace("#", "").slice(0, 6);
  return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
}

function hex(values: number[]): string {
  return "#" + values.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("");
}

function mix(a: string, b: string, t: number): string {
  const from = rgb(a);
  const to = rgb(b);
  return hex(from.map((v, i) => v + (to[i] - v) * t));
}

/** 連續值的顏色。 */
export function continuousColor(scale: ColorScale, value: number): string {
  const range = scale.range ?? [];
  const domain = (scale.domain ?? []).map(Number);
  if (range.length === 0) return TABLEAU10[0];
  if (domain.length < 2 || range.length === 1) return range[0];
  const low = domain[0];
  const high = domain[domain.length - 1];
  if (scale.type === "quantize") {
    const index = Math.floor(((value - low) / (high - low || 1)) * range.length);
    return range[Math.max(0, Math.min(range.length - 1, index))];
  }
  // 比例尺的每個顏色對應 domain 中的一個值；兩者長度不同時，顏色平均分布在範圍內
  const stops =
    domain.length === range.length
      ? domain
      : range.map((_, i) => low + ((high - low) * i) / (range.length - 1));
  if (value <= stops[0]) return range[0];
  for (let i = 0; i < stops.length - 1; i++) {
    if (value <= stops[i + 1]) {
      const span = stops[i + 1] - stops[i];
      return mix(range[i], range[i + 1], span > 0 ? (value - stops[i]) / span : 0);
    }
  }
  return range[range.length - 1];
}

/** 圖例的色帶（CSS 漸層）：從 low 到 high 的顏色；分段色彩以實色的區塊呈現。 */
export function rampBackground(scale: ColorScale, low: number, high: number): string {
  const count = 24;
  const at = (i: number) => continuousColor(scale, low + ((high - low) * i) / (count - 1));
  if (scale.type === "quantize") {
    const stops: string[] = [];
    for (let i = 0; i < count; i++) {
      const color = continuousColor(scale, low + ((high - low) * (i + 0.5)) / count);
      stops.push(`${color} ${((i / count) * 100).toFixed(2)}%`, `${color} ${(((i + 1) / count) * 100).toFixed(2)}%`);
    }
    return `linear-gradient(to right, ${stops.join(", ")})`;
  }
  return `linear-gradient(to right, ${Array.from({ length: count }, (_, i) => at(i)).join(", ")})`;
}
