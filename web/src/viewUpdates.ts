// 檢視的停止與繼續更新（Tableau：處理互動太久時出現可以停止的對話框；停止之後整個檢視暫停，像暫停自動更新
// 一樣——可以復原回到之前的狀態、繼續更新，或不更新繼續操作）。停止的是這一次互動：頁面上所有的檢視一起
// 停止（查詢中止、伺服器上的查詢中斷，回到最後畫好的圖）、一起繼續更新。

export interface UpdatingView {
  /** 停止：中止查詢、回到最後畫好的圖並暫停更新 */
  stop: () => void;
  /** 繼續更新 */
  resume: () => void;
}

const views = new Set<UpdatingView>();

/** 登記頁面上的一個檢視；回傳取消登記的函式。 */
export function registerView(view: UpdatingView): () => void {
  views.add(view);
  return () => {
    views.delete(view);
  };
}

/** 停止頁面上所有檢視的更新（「更新中」的停止）。 */
export function stopAllViews(): void {
  for (const view of [...views]) view.stop();
}

/** 頁面上所有暫停的檢視繼續更新。 */
export function resumeAllViews(): void {
  for (const view of [...views]) view.resume();
}

/** 查詢的代號（伺服器以它停止查詢）：32 個十六進位字元。不用 crypto.randomUUID：它只在安全的連線（HTTPS、本機）上有。 */
export function newQueryId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
