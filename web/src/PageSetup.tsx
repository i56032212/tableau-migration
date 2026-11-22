// 列印的頁面設定（Tableau 的「檔案＞頁面設定」）：紙張、方向、邊界、縮放，要不要印標題、說明與圖例，以及比一頁大的
// 工作表怎麼分頁（在每頁中重複標題和圖例、在窗格邊界分頁）。設定記在瀏覽器（localStorage）；按「列印」時以 @page
// 與列印用的樣式套用（單獨檢視的工作表另外分好頁，見 printPages.ts），再開啟瀏覽器的列印（可以另存 PDF）。

import { useState } from "react";
import { alreadyPaged, clearPages, paginateSheet } from "./printPages";
import { Modal } from "./Server";

export interface PageSetup {
  paper: keyof typeof PAPERS | keyof typeof MORE_PAPERS;
  orientation: "portrait" | "landscape";
  /** 邊界（公釐） */
  margin: number;
  /**
   * 縮放：auto 自動（填滿區塊的圖縮到紙張之內，固定大小的照原尺寸分頁印）；page 縮到一頁之內；width 符合頁寬；
   * percent 自訂的比例；fit 適合於橫向 across 頁、縱向 down 頁（Tableau 的「適合於」）
   */
  scaling: "auto" | "page" | "width" | "percent" | "fit";
  percent: number;
  across: number;
  down: number;
  title: boolean;
  caption: boolean;
  legends: boolean;
  /** 在每頁中重複標題和圖例（工作表分成多頁時） */
  repeat: boolean;
  /** 在窗格邊界分頁（不在列、欄的中間分頁） */
  breakPanes: boolean;
}

/** 紙張的寬、高（公釐，直向） */
export const PAPERS = {
  A4: [210, 297],
  A3: [297, 420],
  Letter: [215.9, 279.4],
  Legal: [215.9, 355.6],
  Tabloid: [279.4, 431.8],
} as const;

/** 訂閱可以選、但「頁面設定」對話框沒有列出的紙張（寬、高，公釐，直向） */
const MORE_PAPERS = {
  A5: [148, 210],
  Ledger: [279.4, 431.8],
} as const;

/** CSS 的 @page size 認得的紙張名稱 */
const CSS_PAPERS: ReadonlySet<string> = new Set(["A3", "A4", "A5", "Letter", "Legal", "Ledger"]);

function paperSize(name: PageSetup["paper"]): readonly [number, number] {
  return name in PAPERS ? PAPERS[name as keyof typeof PAPERS] : MORE_PAPERS[name as keyof typeof MORE_PAPERS];
}

const STORE = "tmig.page-setup";
const STYLE_ID = "tmig-print-setup";
const DEFAULTS: PageSetup = {
  paper: "A4",
  orientation: "landscape",
  margin: 10,
  scaling: "page",
  percent: 100,
  across: 1,
  down: 1,
  title: true,
  caption: true,
  legends: true,
  repeat: true,
  breakPanes: true,
};

export function loadPageSetup(): PageSetup {
  try {
    const stored = JSON.parse(window.localStorage.getItem(STORE) ?? "null") as Partial<PageSetup> | null;
    const merged = { ...DEFAULTS, ...(stored ?? {}) };
    return merged.paper in PAPERS || merged.paper in MORE_PAPERS ? merged : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

/** 紙張扣掉邊界後可以印的寬、高（CSS 像素，96 dpi；少算一點，捨入的誤差不會把一頁擠成兩頁）。 */
export function printableSize(setup: PageSetup): [number, number] {
  const [short, long] = paperSize(setup.paper);
  const [paperW, paperH] = setup.orientation === "portrait" ? [short, long] : [long, short];
  const px = (mm: number) => Math.floor(((mm - 2 * setup.margin) * 96) / 25.4) - 1;
  return [px(paperW), px(paperH)];
}

/**
 * 列印的縮放比例（dashboard、Story：整個縮放成一張，不分頁）：內容的寬高（像素）放進紙張可以印的範圍。
 * 「適合於」在這裡只縮小到橫向、縱向的頁數之內。
 */
export function printScale(setup: PageSetup, width: number, height: number): number {
  if (setup.scaling === "auto") return 1;
  if (setup.scaling === "percent") return Math.max(0.1, Math.min(4, setup.percent / 100));
  const [pageW, pageH] = printableSize(setup);
  const byWidth = pageW / Math.max(width, 1);
  const byHeight = pageH / Math.max(height, 1);
  if (setup.scaling === "fit") return Math.max(0.05, Math.min(1, byWidth * Math.max(1, setup.across), byHeight * Math.max(1, setup.down)));
  return Math.max(0.05, setup.scaling === "width" ? byWidth : Math.min(byWidth, byHeight));
}

/** 列印用的樣式（@page 與 @media print）：寫進文件的 <style>，下次列印前換掉。 */
export function printStyles(setup: PageSetup, scale: number): string {
  const hidden = [
    ...(setup.title ? [] : [".worksheet-header", ".story-title"]),
    ...(setup.caption ? [] : [".worksheet-caption"]),
    ...(setup.legends ? [] : [".role-legend", ".zone-legend"]),
  ];
  // CSS 認得的紙張名稱直接寫；不認得的（Tabloid）寫出寬高，否則整條 @page 規則無效、照預設的紙張印
  const [short, long] = paperSize(setup.paper);
  const size = CSS_PAPERS.has(setup.paper)
    ? `${setup.paper} ${setup.orientation}`
    : setup.orientation === "portrait"
      ? `${short}mm ${long}mm`
      : `${long}mm ${short}mm`;
  return [
    `@page { size: ${size}; margin: ${setup.margin}mm; }`,
    "@media print {",
    "  .page { padding: 0 !important; overflow: visible !important; }",
    `  .page .dashboard, .page .single-sheet, .page .story { zoom: ${scale.toFixed(4)}; }`,
    hidden.length > 0 ? `  ${hidden.join(", ")} { display: none !important; }` : "",
    "}",
  ]
    .filter(Boolean)
    .join("\n");
}

/** 最近一次套用的頁面設定：直接以瀏覽器列印（Ctrl+P）時沿用 */
let lastSetup: PageSetup | null = null;

/**
 * 套用頁面設定，回傳縮放的比例。單獨檢視的工作表分頁印（整張工作表，包括在區塊內捲動時看不到的部分）；
 * dashboard 與 Story 整個縮放成一張。
 */
export function applyPageSetup(setup: PageSetup, element: HTMLElement | null): number {
  clearPages();
  const [pageWidth, pageHeight] = printableSize(setup);
  const paged = element?.matches(".single-sheet") ? paginateSheet(element, { ...setup, pageWidth, pageHeight }) : null;
  const box = element?.getBoundingClientRect();
  const scale = paged ? paged.scale : printScale(setup, box?.width ?? 0, box?.height ?? 0);
  let style = document.getElementById(STYLE_ID) as HTMLStyleElement | null;
  if (!style) {
    style = document.createElement("style");
    style.id = STYLE_ID;
    document.head.appendChild(style);
  }
  style.textContent = printStyles(setup, scale);
  lastSetup = setup;
  return scale;
}

/** 列印的對象：畫面上的 dashboard、工作表或 Story。 */
export const PRINT_TARGET = ".page .dashboard, .page .single-sheet, .page .story";

declare global {
  interface Window {
    /**
     * 訂閱的 PDF（tmig_service.subscriptions 以 Chromium 列印畫面）：套用與「頁面設定」相同的列印樣式——
     * 紙張、方向、邊界與縮放（預設縮放到一頁之內），回傳縮放的比例。沒有這一步時 Chromium 照畫面的大小印，
     * 比紙寬的 dashboard 右邊會被裁掉。
     */
    tmigPageSetup?: (setup: Partial<PageSetup>) => number;
  }
}

if (typeof window !== "undefined") {
  window.tmigPageSetup = (setup) => {
    const merged = { ...DEFAULTS, ...setup };
    const known = merged.paper in PAPERS || merged.paper in MORE_PAPERS;
    return applyPageSetup(known ? merged : { ...merged, paper: DEFAULTS.paper }, document.querySelector<HTMLElement>(PRINT_TARGET));
  };
  // 直接以瀏覽器列印（Ctrl+P、瀏覽器的選單）時也分頁：依最近一次的（或記下的）頁面設定；頁面設定的「列印」剛分好、
  // 圖也沒有重畫過的不再分一次。印完拿掉分好的頁（畫面上本來就看不到）
  window.addEventListener("beforeprint", () => {
    const target = document.querySelector<HTMLElement>(PRINT_TARGET);
    if (!alreadyPaged(target)) applyPageSetup(lastSetup ?? loadPageSetup(), target);
  });
  window.addEventListener("afterprint", () => clearPages());
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
              <option value="auto">自動</option>
              <option value="page">符合一頁</option>
              <option value="width">符合頁寬</option>
              <option value="percent">縮放比例</option>
              <option value="fit">適合於</option>
            </select>
          </label>
          {setup.scaling === "fit" && (
            <>
              <label className="inline">
                橫向
                <input
                  type="number"
                  min={1}
                  max={50}
                  aria-label="橫向的頁數"
                  className="narrow-number"
                  value={setup.across}
                  onChange={(e) => set({ across: Math.max(1, Math.min(50, Math.round(Number(e.target.value)) || 1)) })}
                />{" "}
                頁
              </label>
              <label className="inline">
                縱向
                <input
                  type="number"
                  min={1}
                  max={50}
                  aria-label="縱向的頁數"
                  className="narrow-number"
                  value={setup.down}
                  onChange={(e) => set({ down: Math.max(1, Math.min(50, Math.round(Number(e.target.value)) || 1)) })}
                />{" "}
                頁
              </label>
            </>
          )}
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
        <fieldset>
          <legend>標題和分頁（工作表）</legend>
          <label className="inline" title="工作表分成多頁印時，每一頁都印欄、列標題、座標軸與圖例">
            <input type="checkbox" checked={setup.repeat} onChange={(e) => set({ repeat: e.target.checked })} /> 在每頁中重複標題和圖例
          </label>
          <label className="inline" title="分頁落在列與列、欄與欄之間，不切過儲存格與標記">
            <input type="checkbox" checked={setup.breakPanes} onChange={(e) => set({ breakPanes: e.target.checked })} /> 在窗格邊界分頁
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
