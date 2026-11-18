// 隨檢視器提供的字型（見 fonts.css）：畫圖之前先載入報表用到的字型。
//
// Vega 排版時以畫布量文字的寬度：字型還沒載入時量到的是後備字型的寬度，載入之後文字換成真正的字型，
// 標籤的位置、重疊的判斷與截斷就不對了。所以規格中用到隨附的字型時，等它載入再畫（最多等 1.5 秒，
// 載入失敗或瀏覽器不支援時照常畫）。

import "./fonts.css";

/** 隨檢視器提供的字型名稱與要載入的字重、樣式。 */
const BUNDLED: Record<string, string[]> = {
  Roboto: ["400", "italic 400", "500", "700", "italic 700"],
};

const pending = new Map<string, Promise<void>>();

function load(family: string): Promise<void> {
  let found = pending.get(family);
  if (!found) {
    const fonts = typeof document !== "undefined" ? document.fonts : undefined;
    const faces = BUNDLED[family] ?? [];
    const loading = fonts ? Promise.all(faces.map((face) => fonts.load(`${face} 12px "${family}"`))) : Promise.resolve([]);
    const timeout = new Promise<void>((resolve) => window.setTimeout(resolve, 1500));
    found = Promise.race([loading.then(() => undefined), timeout]).catch(() => undefined);
    pending.set(family, found);
  }
  return found;
}

/** 等規格（或任何含字型名稱的文字）中用到的隨附字型載入。 */
export function bundledFontsReady(text: string): Promise<void> {
  // 只比對名稱：傳進來的常是 JSON（引號已經跳脫），不能連引號一起比
  const used = Object.keys(BUNDLED).filter((family) => text.includes(family));
  return Promise.all(used.map(load)).then(() => undefined);
}
