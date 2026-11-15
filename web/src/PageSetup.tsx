// 列印的頁面設定（Tableau 的「檔案＞頁面設定」）：紙張、方向、邊界、縮放，以及要不要印標題、說明與圖例。
// 設定記在瀏覽器（localStorage）；按「列印」時以 @page 與列印用的樣式套用，再開啟瀏覽器的列印（可以另存 PDF）。

import { useState } from "react";
import { Modal } from "./Server";

export interface PageSetup {
  paper: keyof typeof PAPERS;
  orientation: "portrait" | "landscape";
  /** 邊界（公釐） */
  margin: number;
  /** 縮放：auto 不縮放；page 縮到一頁之內；width 符合頁寬；percent 自訂的比例 */
  scaling: "auto" | "page" | "width" | "percent";
  percent: number;
  title: boolean;
  caption: boolean;
  legends: boolean;
}

/** 紙張的寬、高（公釐，直向） */
export const PAPERS = {
  A4: [210, 297],
  A3: [297, 420],
  Letter: [215.9, 279.4],
  Legal: [215.9, 355.6],
  Tabloid: [279.4, 431.8],
} as const;

const STORE = "tmig.page-setup";
const STYLE_ID = "tmig-print-setup";
const DEFAULTS: PageSetup = { paper: "A4", orientation: "landscape", margin: 10, scaling: "page", percent: 100, title: true, caption: true, legends: true };

export function loadPageSetup(): PageSetup {
  try {
    const stored = JSON.parse(window.localStorage.getItem(STORE) ?? "null") as Partial<PageSetup> | null;
    const merged = { ...DEFAULTS, ...(stored ?? {}) };
    return merged.paper in PAPERS ? merged : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

/** 列印的縮放比例：內容的寬高（像素）放進紙張可以印的範圍（96 dpi）。 */
export function printScale(setup: PageSetup, width: number, height: number): number {
  if (setup.scaling === "auto") return 1;
  if (setup.scaling === "percent") return Math.max(0.1, Math.min(4, setup.percent / 100));
  const [short, long] = PAPERS[setup.paper];
  const [paperW, paperH] = setup.orientation === "portrait" ? [short, long] : [long, short];
  const px = (mm: number) => ((mm - 2 * setup.margin) * 96) / 25.4;
  const byWidth = px(paperW) / Math.max(width, 1);
  const byHeight = px(paperH) / Math.max(height, 1);
  return Math.max(0.05, setup.scaling === "width" ? byWidth : Math.min(byWidth, byHeight));
}

/** 列印用的樣式（@page 與 @media print）：寫進文件的 <style>，下次列印前換掉。 */
export function printStyles(setup: PageSetup, scale: number): string {
  const hidden = [
    ...(setup.title ? [] : [".worksheet-header", ".story-title"]),
    ...(setup.caption ? [] : [".worksheet-caption"]),
    ...(setup.legends ? [] : [".role-legend", ".zone-legend"]),
  ];
  return [
    `@page { size: ${setup.paper} ${setup.orientation}; margin: ${setup.margin}mm; }`,
    "@media print {",
    "  .page { padding: 0 !important; overflow: visible !important; }",
    `  .page .dashboard, .page .single-sheet, .page .story { zoom: ${scale.toFixed(4)}; }`,
    hidden.length > 0 ? `  ${hidden.join(", ")} { display: none !important; }` : "",
    "}",
  ]
    .filter(Boolean)
    .join("\n");
}

export function applyPageSetup(setup: PageSetup, element: HTMLElement | null): number {
  const box = element?.getBoundingClientRect();
  const scale = printScale(setup, box?.width ?? 0, box?.height ?? 0);
  let style = document.getElementById(STYLE_ID) as HTMLStyleElement | null;
  if (!style) {
    style = document.createElement("style");
    style.id = STYLE_ID;
    document.head.appendChild(style);
  }
  style.textContent = printStyles(setup, scale);
  return scale;
}

export function PageSetupDialog({ target, onClose }: { target: () => HTMLElement | null; onClose: () => void }) {
  const [setup, setSetup] = useState<PageSetup>(loadPageSetup);
  const set = (patch: Partial<PageSetup>) => setSetup((current) => ({ ...current, ...patch }));
  const print = () => {
    try {
      window.localStorage.setItem(STORE, JSON.stringify(setup));
    } catch {
      // 不能用 localStorage 時只在這次列印有效
    }
    applyPageSetup(setup, target());
    onClose();
    // 等對話框關閉後再開啟列印
    window.setTimeout(() => window.print(), 50);
  };
  return (
    <Modal title="頁面設定" onClose={onClose}>
      <div className="page-setup">
        <div className="row">
          <label className="inline">
            紙張
            <select aria-label="紙張" value={setup.paper} onChange={(e) => set({ paper: e.target.value as PageSetup["paper"] })}>
              {Object.keys(PAPERS).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="inline">
            方向
            <select aria-label="方向" value={setup.orientation} onChange={(e) => set({ orientation: e.target.value as PageSetup["orientation"] })}>
              <option value="portrait">直向</option>
              <option value="landscape">橫向</option>
            </select>
          </label>
          <label className="inline">
            邊界（公釐）
            <input
              type="number"
              min={0}
              max={50}
              aria-label="邊界"
              className="narrow-number"
              value={setup.margin}
              onChange={(e) => set({ margin: Math.max(0, Math.min(50, Number(e.target.value) || 0)) })}
            />
          </label>
        </div>
        <div className="row">
          <label className="inline">
            列印縮放
            <select aria-label="列印縮放" value={setup.scaling} onChange={(e) => set({ scaling: e.target.value as PageSetup["scaling"] })}>
              <option value="auto">自動（不縮放）</option>
              <option value="page">符合一頁</option>
              <option value="width">符合頁寬</option>
              <option value="percent">縮放比例</option>
            </select>
          </label>
          {setup.scaling === "percent" && (
            <label className="inline">
              <input
                type="number"
                min={10}
                max={400}
                aria-label="縮放比例"
                className="narrow-number"
                value={setup.percent}
                onChange={(e) => set({ percent: Math.max(10, Math.min(400, Number(e.target.value) || 100)) })}
              />{" "}
              %
            </label>
          )}
        </div>
        <fieldset>
          <legend>顯示</legend>
          <label className="inline">
            <input type="checkbox" checked={setup.title} onChange={(e) => set({ title: e.target.checked })} /> 標題
          </label>
          <label className="inline">
            <input type="checkbox" checked={setup.caption} onChange={(e) => set({ caption: e.target.checked })} /> 說明
          </label>
          <label className="inline">
            <input type="checkbox" checked={setup.legends} onChange={(e) => set({ legends: e.target.checked })} /> 圖例
          </label>
        </fieldset>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={print}>
            列印
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}
