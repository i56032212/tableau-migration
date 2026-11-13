// 地圖的平移與縮放（Tableau 的地圖工具列與滾輪縮放、拖曳平移）：Vega-Lite 編譯出的投影以 fit 自動縮放到
// 標記的範圍（伺服器的 maps.py），這裡把 fit 換成同樣的縮放與位移（與 d3 的 fitSize 相同的計算），再乘上
// 使用者的縮放與平移（tmig_view：{z, x, y}）。與 Tableau 相同：滾輪以游標為中心縮放、拖曳平移、連按兩下放大
// （Shift 連按兩下縮小），工具列的「重設地圖」回到依標記自動縮放的範圍。停止操作後回報目前的顯示範圍（經緯度），
// 檢視器以它重新繪製（底圖的細節、州界與地名依新的縮放層級）。

/* eslint-disable @typescript-eslint/no-explicit-any */

export const VIEW_SIGNAL = "tmig_view";
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4096;

/** 地圖的顯示範圍（度）。 */
export interface Viewport {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** 平移、縮放的狀態：縮放倍數與位移（像素）。 */
export interface ZoomState {
  z: number;
  x: number;
  y: number;
}

/** 投影的換算：rotate 為投影的旋轉（經度），k、tx、ty 為縮放 1 時的縮放與位移（與 fit 相同）。 */
export interface MapFit {
  rotate: number;
  k: number;
  tx: number;
  ty: number;
  width: number;
  height: number;
}

const RAD = Math.PI / 180;

function wrap(lon: number): number {
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

/** 麥卡托投影（d3 的 geoMercatorRaw，y 向下為正）在縮放 1、沒有位移時的座標。 */
function raw(lon: number, lat: number, rotate: number): [number, number] {
  const clamped = Math.max(-89.999, Math.min(89.999, lat));
  return [wrap(lon + rotate) * RAD, -Math.log(Math.tan(Math.PI / 4 + (clamped * RAD) / 2))];
}

/**
 * Vega 規格（vega-embed 的 patch）：只有一張（沒有分格）以 fit 自動縮放的麥卡托地圖時，換成可以平移、縮放的
 * 投影；回傳是否換了。
 */
export function zoomableMap(vg: any): boolean {
  const projections = vg?.projections;
  if (!Array.isArray(projections) || projections.length !== 1) return false;
  const projection = projections[0];
  const coords = projection?.fit?.geometry?.coordinates;
  if (projection?.type !== "mercator" || projection?.fit?.geometry?.type !== "MultiPoint" || !Array.isArray(coords) || coords.length < 2)
    return false;
  // 分格的地圖（facet）在子群組中，不換
  if ((vg.marks ?? []).some((m: any) => m.type === "group" && m.from?.facet)) return false;
  const rotate = Array.isArray(projection.rotate) ? Number(projection.rotate[0]) || 0 : 0;
  const points = coords.map(([lon, lat]: [number, number]) => raw(lon, lat, rotate));
  const xs = points.map((p: number[]) => p[0]);
  const ys = points.map((p: number[]) => p[1]);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(...ys);
  const y1 = Math.max(...ys);
  const spanX = Math.max(x1 - x0, 1e-12);
  const spanY = Math.max(y1 - y0, 1e-12);
  const z = `${VIEW_SIGNAL}.z`;
  const factor = "pow(1.0015, -event.deltaY * pow(16, event.deltaMode))";
  const zoomed = `clamp(${z} * ${factor}, ${MIN_ZOOM}, ${MAX_ZOOM})`;
  vg.signals = [
    ...(vg.signals ?? []),
    { name: "tmig_k", update: `min(width / ${spanX}, height / ${spanY})` },
    { name: "tmig_tx", update: `(width - tmig_k * ${x0 + x1}) / 2` },
    { name: "tmig_ty", update: `(height - tmig_k * ${y0 + y1}) / 2` },
    { name: "tmig_down", value: null, on: [{ events: "mousedown", update: "[x(), y()]" }] },
    { name: "tmig_start", value: { z: 1, x: 0, y: 0 }, on: [{ events: "mousedown", update: VIEW_SIGNAL }] },
    {
      name: VIEW_SIGNAL,
      value: { z: 1, x: 0, y: 0 },
      on: [
        {
          events: "[mousedown, window:mouseup] > window:mousemove!",
          update: `tmig_down ? {z: tmig_start.z, x: tmig_start.x + x() - tmig_down[0], y: tmig_start.y + y() - tmig_down[1]} : ${VIEW_SIGNAL}`,
        },
        {
          events: "wheel!",
          force: true,
          update:
            `{z: ${zoomed}, x: x() - (${zoomed} / ${z}) * (x() - ${VIEW_SIGNAL}.x), ` +
            `y: y() - (${zoomed} / ${z}) * (y() - ${VIEW_SIGNAL}.y)}`,
        },
        {
          events: "dblclick",
          update:
            `{z: clamp(${z} * (event.shiftKey ? 0.5 : 2), ${MIN_ZOOM}, ${MAX_ZOOM}), ` +
            `x: x() - (clamp(${z} * (event.shiftKey ? 0.5 : 2), ${MIN_ZOOM}, ${MAX_ZOOM}) / ${z}) * (x() - ${VIEW_SIGNAL}.x), ` +
            `y: y() - (clamp(${z} * (event.shiftKey ? 0.5 : 2), ${MIN_ZOOM}, ${MAX_ZOOM}) / ${z}) * (y() - ${VIEW_SIGNAL}.y)}`,
        },
      ],
    },
    { name: "tmig_rotate", value: rotate },
  ];
  delete projection.fit;
  projection.scale = { signal: `tmig_k * ${z}` };
  projection.translate = { signal: `[tmig_tx * ${z} + ${VIEW_SIGNAL}.x, tmig_ty * ${z} + ${VIEW_SIGNAL}.y]` };
  vg.usermeta = { ...(vg.usermeta ?? {}), tmig_zoomable: true };
  return true;
}

/** 以 (cx, cy) 為中心把縮放倍數乘上 factor。 */
export function zoomAt(state: ZoomState, factor: number, cx: number, cy: number): ZoomState {
  const z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, state.z * factor));
  const f = z / state.z;
  return { z, x: cx - f * (cx - state.x), y: cy - f * (cy - state.y) };
}

/** 目前畫面中的範圍（經緯度）：畫面左上、右下兩角換回經緯度。 */
export function visibleBox(fit: MapFit, state: ZoomState): Viewport {
  const scale = fit.k * state.z;
  const tx = fit.tx * state.z + state.x;
  const ty = fit.ty * state.z + state.y;
  const lonAt = (sx: number) => ((sx - tx) / scale) / RAD - fit.rotate;
  const latAt = (sy: number) => (2 * Math.atan(Math.exp(-(sy - ty) / scale)) - Math.PI / 2) / RAD;
  let west = lonAt(0);
  let east = lonAt(fit.width);
  if (east - west >= 360) {
    west = -180;
    east = 180;
  } else {
    const center = wrap((west + east) / 2);
    const half = (east - west) / 2;
    west = center - half;
    east = center + half;
  }
  const clamp = (v: number) => Math.max(-85, Math.min(85, v));
  return { west, south: clamp(latAt(fit.height)), east, north: clamp(latAt(0)) };
}

/** 檢視上的換算參數（投影的縮放 1 時的值與畫面大小）。 */
export function fitOf(view: any): MapFit {
  return {
    rotate: Number(view.signal("tmig_rotate")) || 0,
    k: Number(view.signal("tmig_k")),
    tx: Number(view.signal("tmig_tx")),
    ty: Number(view.signal("tmig_ty")),
    width: Number(view.signal("width")),
    height: Number(view.signal("height")),
  };
}
