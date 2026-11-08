// 桌面版（D0）：本機引擎提供開啟、儲存、關閉的 API；選檔對話框由桌面版外殼（Tauri）提供。
// 在瀏覽器中開發時沒有外殼，改以輸入完整路徑代替對話框。外殼的選單呼叫 window.tmigDesktop。
import { api } from "./api";
import type { DesktopDocument } from "./types";

interface TauriGlobal {
  core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> };
}

type Go = (to: string) => void;

function shell(): TauriGlobal | undefined {
  return (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
}

/** 選擇要開啟的檔案；取消時為 null。 */
export async function pickOpenPath(): Promise<string | null> {
  const tauri = shell();
  if (tauri) {
    try {
      return await tauri.core.invoke<string | null>("pick_open_path");
    } catch (e) {
      console.warn("選檔對話框無法使用，改以輸入路徑", e);
    }
  }
  const path = window.prompt("要開啟的檔案（.twbx、.twb 或 .tmigx）的完整路徑");
  return path?.trim() || null;
}

/** 選擇報表套件的存檔位置；取消時為 null。 */
export async function pickSavePath(defaultPath: string): Promise<string | null> {
  const tauri = shell();
  if (tauri) {
    try {
      return await tauri.core.invoke<string | null>("pick_save_path", { defaultPath });
    } catch (e) {
      console.warn("存檔對話框無法使用，改以輸入路徑", e);
    }
  }
  const path = window.prompt("報表套件（.tmigx）要存到哪裡？請輸入完整路徑", defaultPath);
  return path?.trim() || null;
}

/** 預設的存檔位置：與原始檔同一個資料夾、同一個檔名，副檔名換成 .tmigx。 */
export function defaultPackagePath(doc: DesktopDocument): string {
  const cut = Math.max(doc.source.lastIndexOf("/"), doc.source.lastIndexOf("\\"));
  const folder = doc.source.slice(0, cut + 1);
  const name = doc.source.slice(cut + 1).replace(/\.(twbx|twb|tmigx)$/i, "");
  return `${folder}${name || doc.name}.tmigx`;
}

/** 畫面右下角的短訊息。 */
export function toast(message: string, kind: "info" | "error" = "info"): void {
  const element = document.createElement("div");
  element.className = `toast ${kind}`;
  element.setAttribute("role", kind === "error" ? "alert" : "status");
  element.textContent = message;
  document.body.appendChild(element);
  window.setTimeout(() => element.remove(), kind === "error" ? 8000 : 4000);
}

export async function openDocument(go: Go): Promise<void> {
  const path = await pickOpenPath();
  if (!path) return;
  try {
    const result = await api.desktopOpen(path);
    if (result.unsupported_count > 0) {
      toast(`「${result.document.name}」有 ${result.unsupported_count} 個元素轉不過來，畫面上不會顯示`, "error");
    }
    go(`/w/${encodeURIComponent(result.document.id)}`);
  } catch (e) {
    toast(`無法開啟：${(e as Error).message}`, "error");
  }
}

/** 儲存成報表套件：已存過的直接覆蓋同一個檔案；另存新檔或還沒存過時先選位置。 */
export async function saveDocument(workbook: string, saveAs: boolean): Promise<void> {
  try {
    const doc = (await api.desktopDocuments()).find((d) => d.id === workbook);
    if (!doc) {
      toast("找不到這份報表", "error");
      return;
    }
    let target = saveAs ? null : doc.saved;
    if (!target) {
      target = await pickSavePath(doc.saved ?? defaultPackagePath(doc));
      if (!target) return;
      if (!/\.tmigx$/i.test(target)) target += ".tmigx";
    }
    const result = await api.desktopSave(workbook, target);
    toast(`已儲存：${result.path}`);
  } catch (e) {
    toast(`無法儲存：${(e as Error).message}`, "error");
  }
}

export async function closeDocument(workbook: string, go: Go): Promise<void> {
  try {
    await api.desktopClose(workbook);
    go("/");
  } catch (e) {
    toast(`無法關閉：${(e as Error).message}`, "error");
  }
}

/** 目前畫面上的報表（網址 /w/<報表>/…）。 */
export function currentWorkbook(): string | null {
  const parts = window.location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  return parts[0] === "w" && parts[1] ? parts[1] : null;
}

/** 外殼的選單（開啟、儲存、另存新檔、關閉報表）呼叫的入口。 */
export function installMenuCommands(go: Go): void {
  const withWorkbook = (run: (workbook: string) => Promise<void>) => () => {
    const workbook = currentWorkbook();
    if (workbook) return run(workbook);
    toast("請先開啟一份報表");
    return Promise.resolve();
  };
  (window as unknown as { tmigDesktop?: Record<string, () => Promise<void>> }).tmigDesktop = {
    open: () => openDocument(go),
    save: withWorkbook((wb) => saveDocument(wb, false)),
    saveAs: withWorkbook((wb) => saveDocument(wb, true)),
    close: withWorkbook((wb) => closeDocument(wb, go)),
  };
}
