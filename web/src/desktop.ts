// 桌面版（D0）：本機引擎提供開啟、儲存、關閉的 API；選檔對話框由桌面版外殼（Tauri）提供。
// 在瀏覽器中開發時沒有外殼，改以輸入完整路徑代替對話框。外殼的選單呼叫 window.tmigDesktop。
import { api } from "./api";
import { datasourceApi, type InspectedTable } from "./editor/datasourceApi";
import type { DesktopDocument } from "./types";

interface TauriGlobal {
  core: { invoke<T>(command: string, args?: Record<string, unknown>): Promise<T> };
}

type Go = (to: string) => void;

/** 伺服器的對話框（登入、從伺服器開啟、發布報表或資料源）：選單與按鈕發出事件，App 顯示對話框。 */
export type DesktopDialog =
  | { kind: "sign-in" }
  | { kind: "open-server" }
  | { kind: "publish"; workbook: string }
  | { kind: "publish-datasource"; workbook: string; datasource?: string }
  /** 新增報表或資料源：選擇資料檔中的資料表（workbook 為 null 時是新的報表） */
  | { kind: "choose-table"; title: string; path: string; tables: InspectedTable[]; workbook: string | null }
  /** 連線到伺服器（資料庫）：workbook 為 null 時建立新的報表，否則加入新的資料源 */
  | { kind: "connect-server"; workbook: string | null };
export const DIALOG_EVENT = "tmig:desktop-dialog";
/** 報表的定義在檢視器之外改變了（例如報表改用發布的資料源）：畫面重新載入報表。 */
export const CHANGED_EVENT = "tmig:workbook-changed";

export function openDialog(dialog: DesktopDialog): void {
  window.dispatchEvent(new CustomEvent<DesktopDialog>(DIALOG_EVENT, { detail: dialog }));
}

export function workbookChanged(workbook: string): void {
  window.dispatchEvent(new CustomEvent<string>(CHANGED_EVENT, { detail: workbook }));
}

function shell(): TauriGlobal | undefined {
  return (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__;
}

let desktopMode = false;

/** 桌面版（本機引擎）：匯出等功能以存檔對話框與本機引擎存檔。 */
export function setDesktopMode(on: boolean): void {
  desktopMode = on;
}

export function isDesktop(): boolean {
  return desktopMode;
}

/** 在桌面版的外殼中（有外殼提供的對話框與指令）；瀏覽器中開啟本機引擎時為 false。 */
export function hasShell(): boolean {
  return shell() !== undefined;
}

/** 結束桌面版（外殼會一併結束本機引擎）。 */
export async function quitApp(): Promise<void> {
  try {
    await shell()?.core.invoke("quit_app");
  } catch (e) {
    toast(`無法結束：${(e as Error).message ?? e}`, "error");
  }
}

/** 「關於」：桌面版的名稱與版本。 */
export async function showAbout(): Promise<void> {
  try {
    const text = await shell()?.core.invoke<string>("about_text");
    if (text) toast(text);
  } catch (e) {
    toast(`無法取得版本：${(e as Error).message ?? e}`, "error");
  }
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

/** 選擇要連線的資料檔（Excel、文字檔、空間檔案）；取消時為 null。 */
export async function pickDataPath(): Promise<string | null> {
  const tauri = shell();
  if (tauri) {
    try {
      return await tauri.core.invoke<string | null>("pick_data_path");
    } catch (e) {
      console.warn("選檔對話框無法使用，改以輸入路徑", e);
    }
  }
  const path = window.prompt(
    "要連線的資料檔（.xlsx、.xls、.csv、.txt、.shp、.geojson、.kml、MapInfo 的 .tab、Esri File Geodatabase 的 .gdb 資料夾或 .gdb.zip 等）的完整路徑",
  );
  return path?.trim() || null;
}

/** 選擇資料夾（萬用字元聯集）；取消時為 null。 */
export async function pickFolderPath(): Promise<string | null> {
  const tauri = shell();
  if (tauri) {
    try {
      return await tauri.core.invoke<string | null>("pick_folder_path");
    } catch (e) {
      console.warn("選擇資料夾的對話框無法使用，改以輸入路徑", e);
    }
  }
  const path = window.prompt("要搜尋的資料夾的完整路徑");
  return path?.trim() || null;
}

/** 新增報表（Tableau 的「連線到資料」）：選擇資料檔，檔案中有多張資料表時選一張，建立後開啟資料源分頁。 */
export async function newWorkbook(go: Go): Promise<void> {
  const path = await pickDataPath();
  if (!path) return;
  try {
    const inspected = await datasourceApi.inspectNew(path);
    if (inspected.tables.length === 1) {
      await createWorkbook(path, inspected.tables[0].name, go);
      return;
    }
    openDialog({ kind: "choose-table", title: `新增報表：${inspected.name}`, path, tables: inspected.tables, workbook: null });
  } catch (e) {
    toast(`無法連線到資料：${(e as Error).message}`, "error");
  }
}

export async function createWorkbook(path: string, table: string, go: Go): Promise<boolean> {
  try {
    const result = await datasourceApi.newWorkbook(path, table);
    go(`/w/${encodeURIComponent(result.document.id)}/edit-ds/${encodeURIComponent(result.datasource)}`);
    return true;
  } catch (e) {
    toast(`無法建立報表：${(e as Error).message}`, "error");
    return false;
  }
}

/** 連線到伺服器（Tableau 的「連線＞到伺服器」）：資料庫的連線對話框；workbook 為 null 時建立新的報表。 */
export function connectServer(workbook: string | null): void {
  openDialog({ kind: "connect-server", workbook });
}

/** 報表加上新的資料源（Tableau 的「資料＞新增資料源」）。 */
export async function addDatasource(workbook: string, go: Go): Promise<void> {
  const path = await pickDataPath();
  if (!path) return;
  try {
    const inspected = await datasourceApi.inspect(workbook, path);
    if (inspected.tables.length === 1) {
      await addDatasourceFrom(workbook, path, inspected.tables[0].name, go);
      return;
    }
    openDialog({ kind: "choose-table", title: `新增資料源：${inspected.name}`, path, tables: inspected.tables, workbook });
  } catch (e) {
    toast(`無法連線到資料：${(e as Error).message}`, "error");
  }
}

export async function addDatasourceFrom(workbook: string, path: string, table: string, go: Go): Promise<boolean> {
  try {
    const result = await datasourceApi.addDatasource(workbook, path, table);
    workbookChanged(workbook);
    go(`/w/${encodeURIComponent(workbook)}/edit-ds/${encodeURIComponent(result.datasource)}`);
    return true;
  } catch (e) {
    toast(`無法新增資料源：${(e as Error).message}`, "error");
    return false;
  }
}

/** 選擇存檔位置（預設為報表套件；匯出時指定標題、副檔名與檔案類型）；取消時為 null。 */
export async function pickSavePath(
  defaultPath: string,
  options: { title?: string; extension?: string; label?: string } = {},
): Promise<string | null> {
  const tauri = shell();
  if (tauri) {
    try {
      return await tauri.core.invoke<string | null>("pick_save_path", { defaultPath, ...options });
    } catch (e) {
      console.warn("存檔對話框無法使用，改以輸入路徑", e);
    }
  }
  const what = options.label ? `${options.label}（.${options.extension}）` : "報表套件（.tmigx）";
  const path = window.prompt(`${what}要存到哪裡？請輸入完整路徑`, defaultPath);
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
    const doc = (await api.desktopDocuments()).find((d) => d.id === workbook);
    // 編輯過、還沒儲存：先確認（與 Tableau 關閉活頁簿時相同）
    if (doc?.modified && !window.confirm(`「${doc.name}」有尚未儲存的變更，仍要關閉嗎？`)) return;
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

/** 外殼的選單（新增報表、連線到伺服器、開啟、儲存、另存新檔、關閉報表；伺服器的登入、從伺服器開啟、發布報表與
 * 資料源、登出）呼叫的入口。 */
export function installMenuCommands(go: Go): void {
  const withWorkbook = (run: (workbook: string) => Promise<void>) => () => {
    const workbook = currentWorkbook();
    if (workbook) return run(workbook);
    toast("請先開啟一份報表");
    return Promise.resolve();
  };
  (window as unknown as { tmigDesktop?: Record<string, () => Promise<void>> }).tmigDesktop = {
    open: () => openDocument(go),
    newWorkbook: () => newWorkbook(go),
    connectServer: async () => connectServer(null),
    save: withWorkbook((wb) => saveDocument(wb, false)),
    saveAs: withWorkbook((wb) => saveDocument(wb, true)),
    close: withWorkbook((wb) => closeDocument(wb, go)),
    signIn: async () => openDialog({ kind: "sign-in" }),
    openFromServer: async () => openDialog({ kind: "open-server" }),
    publish: withWorkbook(async (wb) => openDialog({ kind: "publish", workbook: wb })),
    publishDatasource: withWorkbook(async (wb) => openDialog({ kind: "publish-datasource", workbook: wb })),
    signOut: () =>
      api
        .serverSignOut()
        .then(() => toast("已登出伺服器"))
        .catch((e: Error) => toast(`無法登出：${e.message}`, "error")),
  };
  installShortcuts();
}

let shortcutsInstalled = false;

/** 檔案指令的快捷鍵（原本由外殼的原生選單提供）：Ctrl+O 開啟、Ctrl+S 儲存、Ctrl+Shift+S 另存新檔、
 * Ctrl+W 關閉報表。 */
function installShortcuts(): void {
  if (shortcutsInstalled) return;
  shortcutsInstalled = true;
  window.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    const commands = (window as unknown as { tmigDesktop?: Record<string, () => Promise<void>> }).tmigDesktop;
    if (!commands) return;
    const key = event.key.toLowerCase();
    const command =
      key === "o" && !event.shiftKey
        ? commands.open
        : key === "s"
          ? event.shiftKey
            ? commands.saveAs
            : commands.save
          : key === "w" && !event.shiftKey
            ? commands.close
            : null;
    if (!command) return;
    event.preventDefault();
    void command();
  });
}

/** 登出伺服器（功能表共用）。 */
export function signOutServer(): void {
  void (window as unknown as { tmigDesktop?: Record<string, () => Promise<void>> }).tmigDesktop?.signOut();
}
