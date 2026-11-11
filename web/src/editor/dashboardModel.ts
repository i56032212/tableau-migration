// dashboard 編輯器的區塊操作：加入、移除、移動、重新排列（與檢視器的版面演算法相同，見 ../layout.ts）。
// 區塊的座標是 dashboard 參考尺寸上的像素；水平、垂直容器的子區塊沿排列方向依序排列，固定大小的子區塊不變，
// 其餘依原本的比例分配；tiled 容器的子區塊依比例縮放。最外層的第一個區塊以外都是浮動的區塊。

import type { Dashboard, Device, DeviceLayout, Zone, ZoneStyle } from "../types";
import { EDITOR_REF } from "./model";

export const TILED_STYLE: ZoneStyle = {
  background: null,
  border: "none",
  border_width: 0,
  border_color: null,
  margin: [4, 4, 4, 4],
  padding: [0, 0, 0, 0],
};

export type NewZone = Omit<Zone, "id" | "x" | "y" | "w" | "h" | "children" | "source_ref"> & { children?: Zone[] };

export function walkZones(zones: Zone[], visit: (zone: Zone, parent: Zone | null, depth: number) => void, parent: Zone | null = null, depth = 0) {
  for (const zone of zones) {
    visit(zone, parent, depth);
    walkZones(zone.children, visit, zone, depth + 1);
  }
}

export function findZone(dashboard: Dashboard, id: string): { zone: Zone; parent: Zone | null } | null {
  let found: { zone: Zone; parent: Zone | null } | null = null;
  walkZones(dashboard.zones, (zone, parent) => {
    if (zone.id === id) found = { zone, parent };
  });
  return found;
}

export function nextZoneId(dashboard: Dashboard): string {
  let max = 0;
  walkZones(dashboard.zones, (zone) => {
    const m = /^z(\d+)$/.exec(zone.id);
    if (m) max = Math.max(max, Number(m[1]));
  });
  return `z${max + 1}`;
}

/** 浮動的區塊：最外層的第一個區塊（並排的版面）以外。 */
export function isFloating(dashboard: Dashboard, id: string): boolean {
  return dashboard.zones.slice(1).some((z) => z.id === id);
}

const isFlow = (zone: Zone) => zone.kind === "container" && (zone.layout === "horizontal" || zone.layout === "vertical");

/** 新區塊預設放入的容器：選取的區塊（是水平或垂直容器時）或它所在的容器；沒有時為第一個水平或垂直容器。 */
export function targetContainer(dashboard: Dashboard, selected: string | null): Zone | null {
  if (selected) {
    const found = findZone(dashboard, selected);
    if (found && isFlow(found.zone)) return found.zone;
    if (found?.parent && found.parent.kind === "container") return found.parent;
  }
  let first: Zone | null = null;
  walkZones(dashboard.zones.slice(0, 1), (zone) => {
    if (!first && isFlow(zone)) first = zone;
  });
  return first ?? dashboard.zones[0] ?? null;
}

const clampInt = (n: number) => Math.max(0, Math.round(n));

/** 依新的方框排列一個區塊與其中的子區塊。 */
export function placeZone(zone: Zone, box: { x: number; y: number; w: number; h: number }): Zone {
  const placed = { ...zone, x: clampInt(box.x), y: clampInt(box.y), w: clampInt(box.w), h: clampInt(box.h) };
  if (zone.kind !== "container" || zone.children.length === 0) return placed;
  if (isFlow(zone) && isRegular(zone)) return flow(placed, zone);
  return scaleChildren(placed, zone);
}

/** tiled 容器（或子區塊重疊、不依序的水平或垂直容器，與檢視器相同）：子區塊依比例縮放。 */
function scaleChildren(placed: Zone, zone: Zone): Zone {
  const rx = zone.w ? placed.w / zone.w : 0;
  const ry = zone.h ? placed.h / zone.h : 0;
  return {
    ...placed,
    children: zone.children.map((c) =>
      placeZone(c, { x: placed.x + (c.x - zone.x) * rx, y: placed.y + (c.y - zone.y) * ry, w: c.w * rx, h: c.h * ry }),
    ),
  };
}

/** 水平或垂直容器中，看得到的子區塊依陣列的順序沿排列方向排列、互不重疊（新加入、還沒有大小的不算）。 */
export function isRegular(zone: Zone): boolean {
  const horizontal = zone.layout === "horizontal";
  const placed = zone.children.filter((c) => !c.hidden && c.w > 0 && c.h > 0);
  let end = -Infinity;
  for (const child of placed) {
    const start = horizontal ? child.x : child.y;
    if (start < end - 1) return false;
    end = start + (horizontal ? child.w : child.h);
  }
  return true;
}

/** 水平或垂直容器：子區塊依陣列的順序沿排列方向排列（固定大小的不變，其餘依原本的大小比例分配）。 */
function flow(placed: Zone, original: Zone): Zone {
  const horizontal = placed.layout === "horizontal";
  const start = (z: { x: number; y: number }) => (horizontal ? z.x : z.y);
  const span = (z: { w: number; h: number }) => (horizontal ? z.w : z.h);
  const crossStart = (z: { x: number; y: number }) => (horizontal ? z.y : z.x);
  const crossSpan = (z: { w: number; h: number }) => (horizontal ? z.h : z.w);
  const visible = original.children.filter((c) => !c.hidden);
  // 原本的間距與內縮（沿用；子區塊重疊時為 0）
  const sorted = [...visible].sort((a, b) => start(a) - start(b));
  const gaps: number[] = [];
  for (let i = 1; i < sorted.length; i++) gaps.push(start(sorted[i]) - (start(sorted[i - 1]) + span(sorted[i - 1])));
  const gap = gaps.length > 0 ? Math.max(0, gaps.sort((a, b) => a - b)[Math.floor(gaps.length / 2)]) : 0;
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  const lead = first ? Math.max(0, start(first) - start(original)) : 0;
  const trail = last ? Math.max(0, start(original) + span(original) - start(last) - span(last)) : 0;
  const crossLead = first ? Math.max(0, crossStart(first) - crossStart(original)) : 0;
  const crossTrail = first ? Math.max(0, crossStart(original) + crossSpan(original) - crossStart(first) - crossSpan(first)) : 0;
  const fixedTotal = visible.reduce((sum, c) => sum + (c.fixed ?? 0), 0);
  const free = visible.filter((c) => c.fixed === null || c.fixed === undefined);
  const known = free.filter((c) => span(c) > 0);
  const average = known.length > 0 ? known.reduce((s, c) => s + span(c), 0) / known.length : 1;
  const weight = (c: Zone) => (span(c) > 0 ? span(c) : average);
  const totalWeight = free.reduce((s, c) => s + weight(c), 0) || 1;
  const available = Math.max(0, span(placed) - lead - trail - gap * Math.max(0, visible.length - 1) - fixedTotal);
  let cursor = start(placed) + lead;
  const cross0 = crossStart(placed) + crossLead;
  const cross1 = Math.max(0, crossSpan(placed) - crossLead - crossTrail);
  const children = original.children.map((child) => {
    if (child.hidden) return placeZone(child, { x: placed.x, y: placed.y, w: child.w, h: child.h });
    const length = child.fixed !== null && child.fixed !== undefined ? child.fixed : (available * weight(child)) / totalWeight;
    const box = horizontal ? { x: cursor, y: cross0, w: length, h: cross1 } : { x: cross0, y: cursor, w: cross1, h: length };
    cursor += length + gap;
    return placeZone(child, box);
  });
  return { ...placed, children };
}

/** 以 change 改變某個區塊（依 id），回傳新的 dashboard（不重新排列；需要時呼叫 relayoutParent）。 */
export function updateZone(dashboard: Dashboard, id: string, change: (zone: Zone) => Zone): Dashboard {
  const visit = (zones: Zone[]): Zone[] =>
    zones.map((zone) => (zone.id === id ? change(zone) : zone.children.length > 0 ? { ...zone, children: visit(zone.children) } : zone));
  return { ...dashboard, zones: visit(dashboard.zones) };
}

/** 重新排列區塊所在的容器（例如改變了固定大小或隱藏之後）。 */
export function relayoutParent(dashboard: Dashboard, id: string): Dashboard {
  const found = findZone(dashboard, id);
  if (!found?.parent) return dashboard;
  return updateZone(dashboard, found.parent.id, (p) => placeZone(p, p));
}

/** 重新排列整個 dashboard（例如改變大小之後）。 */
export function relayout(dashboard: Dashboard): Dashboard {
  const [root, ...floating] = dashboard.zones;
  if (!root) return dashboard;
  const placedRoot = placeZone(root, { x: 0, y: 0, w: dashboard.size.width, h: dashboard.size.height });
  const clamp = (z: Zone) => {
    const w = Math.min(z.w, dashboard.size.width);
    const h = Math.min(z.h, dashboard.size.height);
    return placeZone(z, { x: Math.min(z.x, dashboard.size.width - w), y: Math.min(z.y, dashboard.size.height - h), w, h });
  };
  return { ...dashboard, zones: [placedRoot, ...floating.map(clamp)] };
}

/**
 * 加入並排的區塊：放到 container 中 after（區塊 id）之後，沒有 after 時放在最後。依序排列的水平或垂直容器
 * 重新分配空間；其他容器（tiled、子區塊重疊的）與 Tableau 拖放到區塊上相同，把 after（沒有時為最後一個）
 * 區塊分成兩半，新區塊占後一半；空的容器由新區塊填滿。
 */
export function insertTiled(dashboard: Dashboard, container: string, zone: NewZone, after: string | null): [Dashboard, string] {
  const id = nextZoneId(dashboard);
  const created: Zone = {
    ...zone,
    id,
    x: 0,
    y: 0,
    w: 0,
    h: 0,
    children: zone.children ?? [],
    source_ref: EDITOR_REF,
  } as Zone;
  const updated = updateZone(dashboard, container, (parent) => {
    const children = [...parent.children];
    const index = after ? children.findIndex((c) => c.id === after) : -1;
    const position = index >= 0 ? index + 1 : children.length;
    const visible = children.filter((c) => !c.hidden);
    if (visible.length === 0) {
      const pad = parent.style?.padding ?? [0, 0, 0, 0];
      children.splice(position, 0, placeZone(created, { x: parent.x + pad[3], y: parent.y + pad[0], w: parent.w - pad[1] - pad[3], h: parent.h - pad[0] - pad[2] }));
      return { ...parent, children };
    }
    if (isFlow(parent) && isRegular(parent)) {
      children.splice(position, 0, created);
      return placeZone({ ...parent, children }, parent);
    }
    // 分割：沿容器的排列方向（tiled 時沿較長的一邊）
    const near = index >= 0 ? children[index] : visible[visible.length - 1];
    const along = parent.layout === "horizontal" || (parent.layout !== "vertical" && near.w >= near.h) ? "w" : "h";
    const first = along === "w" ? { x: near.x, y: near.y, w: Math.floor(near.w / 2), h: near.h } : { x: near.x, y: near.y, w: near.w, h: Math.floor(near.h / 2) };
    const second =
      along === "w"
        ? { x: near.x + first.w, y: near.y, w: near.w - first.w, h: near.h }
        : { x: near.x, y: near.y + first.h, w: near.w, h: near.h - first.h };
    const nearIndex = children.indexOf(near);
    children[nearIndex] = placeZone(near, first);
    children.splice(nearIndex + 1, 0, placeZone(created, second));
    return { ...parent, children };
  });
  return [updated, id];
}

/** 加入浮動的區塊（畫在並排的版面之上）。 */
export function insertFloating(dashboard: Dashboard, zone: NewZone, box: { x: number; y: number; w: number; h: number }): [Dashboard, string] {
  const id = nextZoneId(dashboard);
  const w = Math.min(box.w, dashboard.size.width);
  const h = Math.min(box.h, dashboard.size.height);
  const created = placeZone(
    { ...zone, id, x: 0, y: 0, w: 0, h: 0, children: zone.children ?? [], source_ref: EDITOR_REF } as Zone,
    { x: Math.max(0, Math.min(box.x, dashboard.size.width - w)), y: Math.max(0, Math.min(box.y, dashboard.size.height - h)), w, h },
  );
  return [{ ...dashboard, zones: [...dashboard.zones, created] }, id];
}

/** 移除區塊（並排的版面本身不能移除）；按鈕上指向它的顯示切換一併移除。 */
export function removeZone(dashboard: Dashboard, id: string): Dashboard {
  if (dashboard.zones[0]?.id === id) return dashboard;
  if (dashboard.zones.slice(1).some((z) => z.id === id)) {
    return cleanButtons({ ...dashboard, zones: dashboard.zones.filter((z) => z.id !== id) });
  }
  const found = findZone(dashboard, id);
  if (!found?.parent) return dashboard;
  const parent = found.parent;
  // 依序排列的容器：其餘的子區塊分配空出的空間；其他容器保留原本的位置
  return cleanButtons(
    updateZone(dashboard, parent.id, (p) => {
      const rest = { ...p, children: p.children.filter((c) => c.id !== id) };
      return isFlow(p) && isRegular(p) ? placeZone(rest, rest) : rest;
    }),
  );
}

function cleanButtons(dashboard: Dashboard): Dashboard {
  const ids = new Set<string>();
  walkZones(dashboard.zones, (z) => ids.add(z.id));
  const fix = (zones: Zone[]): Zone[] =>
    zones
      .map((z) =>
        z.button?.action === "toggle" ? { ...z, button: { ...z.button, zones: z.button.zones.filter((t) => ids.has(t)) } } : z,
      )
      // 顯示切換的按鈕沒有目標時移除
      .filter((z) => !(z.button?.action === "toggle" && z.button.zones.length === 0))
      .map((z) => ({ ...z, children: fix(z.children) }));
  return { ...dashboard, zones: fix(dashboard.zones) };
}

/** 在所在的容器中往前（-1）或往後（+1）移動。 */
export function moveZone(dashboard: Dashboard, id: string, delta: number): Dashboard {
  if (isFloating(dashboard, id)) {
    // 浮動的區塊：改變上下的順序
    const zones = [...dashboard.zones];
    const index = zones.findIndex((z) => z.id === id);
    const target = Math.max(1, Math.min(zones.length - 1, index + delta));
    const [zone] = zones.splice(index, 1);
    zones.splice(target, 0, zone);
    return { ...dashboard, zones };
  }
  const found = findZone(dashboard, id);
  if (!found?.parent) return dashboard;
  return updateZone(dashboard, found.parent.id, (parent) => {
    const children = [...parent.children];
    const index = children.findIndex((c) => c.id === id);
    const target = Math.max(0, Math.min(children.length - 1, index + delta));
    if (target === index) return parent;
    if (!(isFlow(parent) && isRegular(parent))) {
      // 不依序排列的容器：與相鄰的區塊交換位置
      const a = children[index];
      const b = children[target];
      children[index] = placeZone(b, { x: a.x, y: a.y, w: a.w, h: a.h });
      children[target] = placeZone(a, { x: b.x, y: b.y, w: b.w, h: b.h });
      return { ...parent, children };
    }
    const [zone] = children.splice(index, 1);
    children.splice(target, 0, zone);
    return placeZone({ ...parent, children }, parent);
  });
}

/** 區塊的說明（版面配置的樹狀清單）。 */
export function zoneLabel(zone: Zone, names: Map<string, string>): string {
  switch (zone.kind) {
    case "container":
      return zone.layout === "horizontal" ? "水平容器" : zone.layout === "vertical" ? "垂直容器" : "並排容器";
    case "worksheet":
      return names.get(zone.worksheet ?? "") ?? zone.worksheet ?? "工作表";
    case "filter":
      return `篩選：${names.get(zone.worksheet ?? "") ?? ""}`;
    case "parameter":
      return `參數：${names.get(`param:${zone.parameter ?? ""}`) ?? zone.parameter}`;
    case "legend":
      return `圖例：${names.get(zone.worksheet ?? "") ?? ""}`;
    case "page":
      return `頁面：${names.get(zone.worksheet ?? "") ?? ""}`;
    case "highlighter":
      return `醒目提示：${names.get(zone.worksheet ?? "") ?? ""}`;
    case "text":
      return `文字：${(zone.text ?? "").slice(0, 16)}`;
    case "image":
      return `圖片：${zone.image ?? ""}`;
    case "title":
      return "標題";
    case "blank":
      return "空白";
    case "button":
      return `按鈕：${zone.button?.caption ?? zone.button?.action ?? ""}`;
    case "web":
      return `網頁：${zone.url ?? ""}`;
    default:
      return zone.kind;
  }
}

// ---------------------------------------------------------------- 裝置專屬版面

export const DEVICE_NAMES: Record<Device, string> = { desktop: "桌面", tablet: "平板", phone: "手機" };
const PHONE_WIDTH = 375;

/** 編輯裝置版面：以版面的區塊與尺寸當作 dashboard 編輯（名稱、背景等其餘的設定是 dashboard 的）。 */
export function layoutDashboard(dashboard: Dashboard, layout: DeviceLayout): Dashboard {
  return { ...dashboard, zones: layout.zones, size: layout.size, sizing: "fixed", min_size: null, max_size: null, layouts: [] };
}

/** 裝置版面的編輯結果寫回 dashboard：版面的區塊與尺寸，其餘（名稱、背景）寫到 dashboard。 */
export function mergeLayout(dashboard: Dashboard, device: Device, edited: Dashboard): Dashboard {
  return {
    ...dashboard,
    name: edited.name,
    background: edited.background,
    layouts: (dashboard.layouts ?? []).map((l) => (l.device === device ? { ...l, zones: edited.zones, size: edited.size } : l)),
  };
}

/**
 * 新的裝置版面。手機：與 Tableau 自動產生的手機版面相同，預設版面上的物件由上到下排列（符合寬度、
 * 工作表 300 像素高）；平板、桌面：複製預設版面的區塊與尺寸。
 */
export function newDeviceLayout(dashboard: Dashboard, device: Device): DeviceLayout {
  if (device !== "phone") {
    return { device, sizing: "default", size: { ...dashboard.size }, auto_generated: false, zones: structuredClone(dashboard.zones) };
  }
  const items: Zone[] = [];
  walkZones(dashboard.zones, (zone) => {
    if (zone.kind !== "container" && zone.kind !== "blank" && !zone.hidden) items.push(zone);
  });
  const pad = 8;
  const inner = PHONE_WIDTH - 2 * pad;
  let y = pad;
  const children: Zone[] = items.map((zone) => {
    const h = zone.kind === "worksheet" ? 300 : Math.round(Math.min(Math.max(zone.h, 40), zone.kind === "text" ? 160 : 120));
    const placed: Zone = { ...structuredClone(zone), x: pad, y, w: inner, h, fixed: h, children: [] };
    y += h;
    return placed;
  });
  const height = Math.max(y + pad, 200);
  const base = { worksheet: null, field: null, parameter: null, control: null, legend: null, text: null, image: null, source_ref: EDITOR_REF };
  const ids = new Set(items.map((z) => z.id));
  let next = 1;
  const freshId = () => {
    while (ids.has(`z${next}`)) next += 1;
    ids.add(`z${next}`);
    return `z${next}`;
  };
  const flow: Zone = { ...base, id: freshId(), kind: "container", layout: "vertical", x: pad, y: pad, w: inner, h: height - 2 * pad, children };
  const root: Zone = { ...base, id: freshId(), kind: "container", layout: "tiled", x: 0, y: 0, w: PHONE_WIDTH, h: height, children: [flow] };
  return { device, sizing: "fit_width", size: { width: PHONE_WIDTH, height }, auto_generated: false, zones: [root] };
}
