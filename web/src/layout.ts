// dashboard 版面：與 kernel/src/tmig/render/layout.py 相同的演算法（端到端測試確認兩者一致）。
//
// - 固定尺寸的 dashboard 使用 IR 的像素座標
// - 自動與範圍尺寸：最外層與 tiled 容器依比例縮放；horizontal／vertical 容器沿排列方向分配，
//   固定大小的區塊維持像素大小、間距不變、其餘依比例；另一個方向依比例縮放；隱藏的區塊不占空間

import type { Dashboard, Zone } from "./types";

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** dashboard 在可用空間 width×height 中的顯示尺寸。 */
export function displaySize(dashboard: Dashboard, width: number, height: number): [number, number] {
  if (dashboard.sizing === "fixed" || !dashboard.sizing) return [dashboard.size.width, dashboard.size.height];
  let w = width;
  let h = height;
  if (dashboard.sizing === "range") {
    if (dashboard.min_size) {
      w = Math.max(w, dashboard.min_size.width);
      h = Math.max(h, dashboard.min_size.height);
    }
    if (dashboard.max_size) {
      w = Math.min(w, dashboard.max_size.width);
      h = Math.min(h, dashboard.max_size.height);
    }
  }
  return [w, h];
}

export function layoutDashboard(dashboard: Dashboard, width: number, height: number): Map<string, Box> {
  let w = width;
  let h = height;
  if (dashboard.sizing === "fixed" || !dashboard.sizing) {
    w = dashboard.size.width;
    h = dashboard.size.height;
  }
  const sx = w / dashboard.size.width;
  const sy = h / dashboard.size.height;
  const result = new Map<string, Box>();
  for (const zone of dashboard.zones) {
    place(zone, { x: zone.x * sx, y: zone.y * sy, w: zone.w * sx, h: zone.h * sy }, result);
  }
  return result;
}

function place(zone: Zone, box: Box, result: Map<string, Box>) {
  result.set(zone.id, box);
  if (zone.kind !== "container" || zone.children.length === 0) return;
  if (zone.layout === "horizontal" || zone.layout === "vertical") {
    flow(zone, box, result);
    return;
  }
  for (const child of zone.children) place(child, scaled(child, zone, box), result);
}

const ratio = (next: number, old: number) => (old ? next / old : 0);

function scaled(child: Zone, parent: Zone, box: Box): Box {
  const rx = ratio(box.w, parent.w);
  const ry = ratio(box.h, parent.h);
  return {
    x: box.x + (child.x - parent.x) * rx,
    y: box.y + (child.y - parent.y) * ry,
    w: child.w * rx,
    h: child.h * ry,
  };
}

function flow(zone: Zone, box: Box, result: Map<string, Box>) {
  const horizontal = zone.layout === "horizontal";
  for (const child of zone.children) {
    if (child.hidden) place(child, { x: box.x, y: box.y, w: 0, h: 0 }, result);
  }
  const visible = zone.children.filter((c) => !c.hidden);
  if (visible.length === 0) return;
  const start = (z: Zone) => (horizontal ? z.x : z.y);
  const span = (z: Zone) => (horizontal ? z.w : z.h);
  visible.sort((a, b) => start(a) - start(b));
  const parentStart = start(zone);
  const parentSpan = span(zone);
  const newStart = horizontal ? box.x : box.y;
  const newSpan = horizontal ? box.w : box.h;
  const gaps: number[] = [];
  let previousEnd = parentStart;
  for (const child of visible) {
    gaps.push(start(child) - previousEnd);
    previousEnd = start(child) + span(child);
  }
  const trailing = parentStart + parentSpan - previousEnd;
  if (Math.min(...gaps) < 0 || trailing < 0) {
    // 子區塊重疊或超出容器：退回依比例縮放
    for (const child of visible) place(child, scaled(child, zone, box), result);
    return;
  }
  const fixedTotal = visible.reduce((sum, c) => sum + (c.fixed ?? 0), 0);
  const freeRef = visible.reduce((sum, c) => sum + (c.fixed === null || c.fixed === undefined ? span(c) : 0), 0);
  const freeNew = newSpan - fixedTotal - gaps.reduce((a, b) => a + b, 0) - trailing;
  const scale = freeRef ? Math.max(freeNew, 0) / freeRef : 0;
  const cross = horizontal ? ratio(box.h, zone.h) : ratio(box.w, zone.w);
  let cursor = newStart;
  visible.forEach((child, i) => {
    cursor += gaps[i];
    const length = child.fixed !== null && child.fixed !== undefined ? child.fixed : span(child) * scale;
    const childBox = horizontal
      ? { x: cursor, y: box.y + (child.y - zone.y) * cross, w: length, h: child.h * cross }
      : { x: box.x + (child.x - zone.x) * cross, y: cursor, w: child.w * cross, h: length };
    place(child, childBox, result);
    cursor += length;
  });
}
